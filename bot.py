#!/usr/bin/env python3
"""
Polymarket Copy Trading Bot.

Monitors one leader wallet on Polymarket via the Data API activity feed and
mirrors its trades on the configured account using py-clob-client. Focused
on short-timeframe crypto markets (BTC/ETH 5m/15m) by default.

Usage:
    python bot.py                     # uses .env (dry-run by default)
    COPY_DRY_RUN=false python bot.py  # live (be careful)
"""
from __future__ import annotations

import asyncio
import signal
import sys
import time
from pathlib import Path

import aiohttp
from loguru import logger

from config import CopyBotConfig, load_config, validate_config
from copier import TradeCopier
from market_filter import decide_from_market, should_copy
from polymarket_client import PolymarketClient, PolymarketMarket
from tracker import ActivityTracker, TradeEvent


class CopyBot:
    def __init__(self, cfg: CopyBotConfig):
        self.cfg = cfg

        self.poly = PolymarketClient(cfg)
        self.tracker = ActivityTracker(
            target_address=cfg.target_address,
            resume_from_cursor=cfg.resume_from_cursor,
        )
        self.copier = TradeCopier(cfg, self.poly)

        # Small Gamma-market cache keyed on condition_id.
        self._market_cache: dict[str, PolymarketMarket] = {}

        # Main loop state.
        self.running = False
        self.start_ts: float = 0.0
        self.events_processed: int = 0
        self.orders_attempted: int = 0
        self.orders_ok: int = 0

        # Backoff on transport failures.
        self._current_interval: float = cfg.poll_interval_seconds
        self._max_backoff: float = 30.0

    # ── Setup ──────────────────────────────────────────────────

    def setup(self) -> None:
        logger.info("Initialising Polymarket copy bot...")
        validate_config(self.cfg)

        balance = self.copier.get_usdc_balance(force=True)
        logger.info(f"USDC collateral balance: ${balance:.2f}")

        self.tracker.load_cursor()
        logger.info(
            f"Leader: {self.cfg.target_address} "
            f"(cursor_ts={self.tracker.cursor_ts})"
        )

    # ── Market metadata ────────────────────────────────────────

    async def _resolve_market(
        self,
        session: aiohttp.ClientSession,
        event: TradeEvent,
    ) -> PolymarketMarket | None:
        """
        Resolve the full PolymarketMarket (slug, outcomes, etc.) for an
        activity event. The event usually already carries `slug`, so prefer
        the slug lookup; fall back to condition_id if needed.
        """
        if event.condition_id and event.condition_id in self._market_cache:
            return self._market_cache[event.condition_id]

        market: PolymarketMarket | None = None
        if event.slug:
            try:
                market = await PolymarketClient.fetch_market_by_slug(
                    session, event.slug
                )
            except Exception as exc:
                logger.debug(f"Gamma slug lookup failed for {event.slug}: {exc}")

        if market is None and event.condition_id:
            try:
                market = await PolymarketClient.fetch_market_by_condition_id(
                    session, event.condition_id
                )
            except Exception as exc:
                logger.debug(
                    f"Gamma condition_id lookup failed for {event.condition_id}: {exc}"
                )

        if market is not None and event.condition_id:
            self._market_cache[event.condition_id] = market
        return market

    # ── Event handling ─────────────────────────────────────────

    async def handle_event(
        self,
        session: aiohttp.ClientSession,
        event: TradeEvent,
    ) -> None:
        self.events_processed += 1

        market = await self._resolve_market(session, event)
        slug = (market.slug if market else "") or event.slug
        question = (market.question if market else "") or event.title

        if market is not None:
            decision = decide_from_market(
                self.cfg.market_filter,
                market,
                self.cfg.extra_allow_slugs,
                self.cfg.extra_block_slugs,
            )
        else:
            decision = should_copy(
                self.cfg.market_filter,
                slug=slug,
                question=question,
                extra_allow=self.cfg.extra_allow_slugs,
                extra_block=self.cfg.extra_block_slugs,
            )

        short = (event.tx_hash or "")[:10]
        logger.info(
            f"leader trade {short} {event.side} {event.size:.2f} sh "
            f"@ {event.price:.3f} ${event.usdc_size:.2f} | "
            f"slug={slug or '(?)'} outcome={event.outcome}"
        )

        if not decision.allowed:
            logger.info(f"  SKIP: {decision.reason}")
            return

        if event.side == "SELL" and not self.cfg.mirror_closes:
            logger.info("  SKIP: COPY_MIRROR_CLOSES=false")
            return

        if market is not None and (market.closed or not market.accepting_orders):
            logger.info(
                f"  SKIP: market no longer accepting orders "
                f"(closed={market.closed}, accepting={market.accepting_orders})"
            )
            return

        # Reject near-certainty BUYs: max upside is (1 - price), max loss is
        # ~price. Asymmetry kills you at small notional sizes.
        if event.side == "BUY" and event.price > self.cfg.max_entry_price:
            logger.info(
                f"  SKIP: leader entry price {event.price:.3f} > "
                f"COPY_MAX_ENTRY_PRICE {self.cfg.max_entry_price:.3f} "
                "(near-certainty trade, asymmetric risk)"
            )
            return

        shares, sizing_note = await self.copier.compute_copy_size(event, session)
        if shares <= 0:
            logger.info(f"  SKIP: sizing resolved to 0 shares ({sizing_note})")
            return

        logger.info(f"  FOLLOW: {decision.reason} | sizing: {sizing_note}")

        self.orders_attempted += 1
        result = self.copier.execute(event, shares, sizing_note=sizing_note)
        if result.success:
            self.orders_ok += 1
        else:
            if result.skipped_reason:
                logger.info(f"  skipped: {result.skipped_reason}")
            elif result.error:
                logger.warning(f"  error: {result.error}")

    # ── Main loop ──────────────────────────────────────────────

    async def run(self) -> None:
        self.running = True
        self.start_ts = time.time()
        last_heartbeat = time.time()
        heartbeat_interval = 60.0

        logger.info(
            f"Entering main loop | poll={self.cfg.poll_interval_seconds}s "
            f"filter={self.cfg.market_filter} "
            f"mode={'DRY RUN' if self.cfg.dry_run else 'LIVE'}"
        )

        async with aiohttp.ClientSession() as session:
            while self.running:
                try:
                    events = await self.tracker.poll(session)

                    # Backoff on consecutive activity poll errors.
                    if self.tracker.consecutive_errors > 0:
                        new = min(self._current_interval * 2, self._max_backoff)
                        if new != self._current_interval:
                            self._current_interval = new
                            logger.warning(
                                f"Data API errors — backing off to "
                                f"{self._current_interval:.1f}s"
                            )
                    elif self._current_interval > self.cfg.poll_interval_seconds:
                        logger.info(
                            f"Data API recovered — resuming "
                            f"{self.cfg.poll_interval_seconds:.1f}s polling"
                        )
                        self._current_interval = self.cfg.poll_interval_seconds

                    for event in events:
                        if not self.running:
                            break
                        try:
                            await self.handle_event(session, event)
                        except Exception as exc:
                            logger.exception(f"handle_event failed: {exc}")

                    if events:
                        self.tracker.save_cursor()

                    now = time.time()
                    if now - last_heartbeat >= heartbeat_interval:
                        self._heartbeat()
                        last_heartbeat = now

                    await asyncio.sleep(self._current_interval)

                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(f"main loop error: {exc}")
                    await asyncio.sleep(5)

    def _heartbeat(self) -> None:
        uptime = time.time() - self.start_ts
        hours = int(uptime // 3600)
        mins = int((uptime % 3600) // 60)
        balance = self.copier.get_usdc_balance()
        logger.info(
            "HEARTBEAT | "
            f"up={hours}h{mins:02d}m | "
            f"events={self.events_processed} | "
            f"orders_ok={self.orders_ok}/{self.orders_attempted} | "
            f"today: {self.copier.trades_today} trades ${self.copier.usd_today:.2f} | "
            f"usdc=${balance:.2f} | cursor_ts={self.tracker.cursor_ts}"
        )

    def stop(self) -> None:
        logger.info("Stopping copy bot...")
        self.running = False
        self.tracker.save_cursor()
        self._print_summary()

    def _print_summary(self) -> None:
        uptime = time.time() - self.start_ts if self.start_ts else 0
        hours = int(uptime // 3600)
        mins = int((uptime % 3600) // 60)
        mode = "DRY RUN" if self.cfg.dry_run else "LIVE"
        print(f"\n{'=' * 60}")
        print("  Polymarket Copy Bot Summary")
        print(f"{'=' * 60}")
        print(f"  Mode:     {mode}")
        print(f"  Leader:   {self.cfg.target_address}")
        print(f"  Filter:   {self.cfg.market_filter}")
        print(f"  Sizing:   {self.cfg.scaling_mode}")
        print(f"  Runtime:  {hours}h {mins}m")
        print(f"  Events:   {self.events_processed}")
        print(f"  Orders:   {self.orders_ok}/{self.orders_attempted} succeeded")
        print(f"  24h:      {self.copier.trades_today} trades "
              f"${self.copier.usd_today:.2f}")
        print(f"{'=' * 60}\n")


# ── Entry point ─────────────────────────────────────────────────

async def _amain() -> None:
    cfg = load_config()

    # Logging.
    bot_dir = Path(__file__).parent
    log_dir = bot_dir / "logs"
    log_dir.mkdir(exist_ok=True)

    logger.remove()
    logger.add(
        sys.stderr,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "{message}"
        ),
        level=cfg.log_level,
    )
    logger.add(
        str(log_dir / "copy_bot_{time}.log"),
        rotation="1 day",
        retention="14 days",
        level="DEBUG",
    )

    mode_label = "DRY RUN" if cfg.dry_run else "LIVE TRADING"
    print(f"""
    +======================================================+
    |          POLYMARKET COPY TRADING BOT                 |
    |          Mode: {mode_label: <38}|
    +======================================================+
    """)

    logger.info(f"Leader:     {cfg.target_address}")
    logger.info(f"Filter:     {cfg.market_filter}")
    if cfg.scaling_mode == "fixed_notional":
        sizing = f"fixed_notional ${cfg.fixed_notional_usd:.2f}"
    elif cfg.scaling_mode == "fixed_ratio":
        sizing = f"fixed_ratio {cfg.fixed_ratio}"
    elif cfg.scaling_mode == "fixed_size":
        sizing = f"fixed_size {cfg.fixed_size} shares"
    else:
        sizing = "proportional"
    logger.info(f"Sizing:     {sizing}")
    logger.info(
        f"Caps:       trade=${cfg.max_trade_usd:.2f} "
        f"daily=${cfg.max_daily_usd:.2f} (<= {cfg.max_daily_trades} trades) "
        f"min=${cfg.min_trade_usd:.2f} pos=${cfg.max_position_usd:.2f} "
        f"max_entry={cfg.max_entry_price:.3f}"
    )
    logger.info(
        f"Execution:  slippage={cfg.slippage_bps}bps "
        f"poll={cfg.poll_interval_seconds}s "
        f"mirror_closes={cfg.mirror_closes} "
        f"resume_cursor={cfg.resume_from_cursor}"
    )
    logger.info(f"Dry run:    {cfg.dry_run}")

    bot = CopyBot(cfg)

    stop_event = asyncio.Event()

    def handle_signal(sig, _frame):
        logger.info(f"signal {sig} received — shutting down")
        stop_event.set()
        bot.running = False

    try:
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
    except (ValueError, AttributeError):
        # Non-main thread or Windows without SIGTERM — KeyboardInterrupt still works.
        pass

    try:
        bot.setup()
        run_task = asyncio.create_task(bot.run())
        stop_task = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait(
            {run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        bot.stop()
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    except KeyboardInterrupt:
        bot.stop()
    except Exception as exc:
        logger.exception(f"fatal error: {exc}")
        bot.stop()
        raise


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

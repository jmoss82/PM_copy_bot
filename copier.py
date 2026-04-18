"""
Trade Copier.

Turns a TradeEvent (from the leader wallet) into a mirrored order on our
Polymarket account. Supports four sizing modes and a set of safety guards
(per-trade, per-day, min trade, position cap), plus dry-run.

All mirrored orders are submitted as FAK (fill-and-kill / IOC) limit orders
priced aggressively through the spread so they behave like market orders but
never leave resting size on the book — important for short-duration markets
where state flips quickly.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional, Tuple

import aiohttp
from loguru import logger

from config import CopyBotConfig
from polymarket_client import PolymarketClient
from tracker import TradeEvent


@dataclass
class CopyResult:
    success: bool
    side: str                     # "BUY" | "SELL"
    token_id: str
    requested_shares: float
    requested_usd: float
    limit_price: float
    filled_shares: float = 0.0
    avg_price: float = 0.0
    order_id: str = ""
    error: str = ""
    dry_run: bool = False
    skipped_reason: str = ""


@dataclass
class LeaderPortfolio:
    """Cached summary of the leader's portfolio used in proportional mode."""
    total_usd: float = 0.0
    refreshed_ts: float = 0.0


class TradeCopier:
    def __init__(self, cfg: CopyBotConfig, poly: PolymarketClient):
        self.cfg = cfg
        self.poly = poly

        # Rolling 24h trade ledger (timestamp, usd) for cap enforcement.
        self._trades: Deque[Tuple[float, float]] = deque()

        # Cached balances / leader summary.
        self._usdc_balance: Optional[float] = None
        self._usdc_balance_ts: float = 0.0
        self._leader_portfolio = LeaderPortfolio()

        # Simulated in-memory positions for dry-run heartbeat/accounting.
        self._sim_positions: Dict[str, float] = {}

    # ── Account state helpers ──────────────────────────────────

    def get_usdc_balance(self, force: bool = False) -> float:
        """USDC collateral balance, cached for 30s."""
        if not force and (time.time() - self._usdc_balance_ts) < 30 and self._usdc_balance is not None:
            return self._usdc_balance
        bal = self.poly.get_usdc_balance()
        if bal is not None:
            self._usdc_balance = bal
            self._usdc_balance_ts = time.time()
        return self._usdc_balance or 0.0

    async def refresh_leader_portfolio(self, session: aiohttp.ClientSession) -> float:
        """
        Sum the leader's current position value for proportional sizing.
        Cached for 60s to avoid hammering Data API.
        """
        if (time.time() - self._leader_portfolio.refreshed_ts) < 60 and self._leader_portfolio.total_usd > 0:
            return self._leader_portfolio.total_usd
        try:
            positions = await PolymarketClient.fetch_positions(
                session, self.cfg.target_address
            )
        except Exception as exc:
            logger.debug(f"leader portfolio fetch failed: {exc}")
            return self._leader_portfolio.total_usd

        total = 0.0
        for pos in positions:
            cur = pos.get("currentValue") or pos.get("current_value")
            if cur is None:
                size = pos.get("size") or pos.get("shares") or 0
                price = pos.get("curPrice") or pos.get("currentPrice") or pos.get("price") or 0
                try:
                    cur = float(size) * float(price)
                except (TypeError, ValueError):
                    cur = 0.0
            try:
                total += float(cur)
            except (TypeError, ValueError):
                pass

        self._leader_portfolio = LeaderPortfolio(total_usd=total, refreshed_ts=time.time())
        return total

    # ── Sizing ─────────────────────────────────────────────────

    def _sign_multiplier(self, is_buy: bool) -> float:
        return 1.0 if is_buy else -1.0

    async def compute_copy_size(
        self,
        event: TradeEvent,
        session: aiohttp.ClientSession,
    ) -> Tuple[float, str]:
        """
        Decide how many shares to trade in response to one leader event.

        Returns (shares, sizing_note). shares is always non-negative; the
        direction is taken from event.side at execute time.
        """
        cfg = self.cfg
        mode = cfg.scaling_mode
        price = max(0.01, min(0.99, float(event.price)))

        if mode == "fixed_notional":
            shares = cfg.fixed_notional_usd / price
            note = f"fixed_notional=${cfg.fixed_notional_usd:.2f}"

        elif mode == "fixed_ratio":
            shares = float(event.size) * cfg.fixed_ratio
            note = f"fixed_ratio={cfg.fixed_ratio}"

        elif mode == "fixed_size":
            shares = float(cfg.fixed_size)
            note = f"fixed_size={cfg.fixed_size}"

        elif mode == "proportional":
            leader_value = await self.refresh_leader_portfolio(session)
            our_value = self.get_usdc_balance()
            if leader_value <= 0:
                logger.warning(
                    "proportional sizing: leader portfolio value is 0, "
                    "falling back to fixed_notional"
                )
                shares = cfg.fixed_notional_usd / price
                note = f"proportional->fixed_notional=${cfg.fixed_notional_usd:.2f}"
            else:
                ratio = our_value / leader_value if leader_value > 0 else 0.0
                shares = float(event.size) * ratio
                note = (
                    f"proportional ratio={ratio:.4f} "
                    f"(ours=${our_value:.2f}, leader=${leader_value:.2f})"
                )
        else:
            logger.error(f"unknown scaling mode: {mode}")
            return 0.0, f"unknown mode={mode}"

        if shares <= 0:
            return 0.0, f"{note} -> 0 shares"

        # Per-trade USD cap.
        if cfg.max_trade_usd > 0 and (shares * price) > cfg.max_trade_usd:
            capped = cfg.max_trade_usd / price
            note += (
                f" | per-trade cap: {shares:.2f} sh (${shares * price:.2f}) "
                f"-> {capped:.2f} sh (${cfg.max_trade_usd:.2f})"
            )
            shares = capped

        return shares, note

    # ── Safety gates ───────────────────────────────────────────

    def _prune_trade_ledger(self) -> None:
        cutoff = time.time() - 86400
        while self._trades and self._trades[0][0] < cutoff:
            self._trades.popleft()

    def _daily_usd_spent(self) -> float:
        self._prune_trade_ledger()
        return sum(usd for _, usd in self._trades)

    def _check_daily_caps(self, next_usd: float) -> Optional[str]:
        self._prune_trade_ledger()
        if self.cfg.max_daily_trades > 0 and len(self._trades) >= self.cfg.max_daily_trades:
            return (
                f"daily trade count cap reached "
                f"({len(self._trades)}/{self.cfg.max_daily_trades})"
            )
        if self.cfg.max_daily_usd > 0:
            if self._daily_usd_spent() + next_usd > self.cfg.max_daily_usd:
                return (
                    f"daily $ cap would be exceeded "
                    f"(spent ${self._daily_usd_spent():.2f}, "
                    f"attempting ${next_usd:.2f}, "
                    f"cap ${self.cfg.max_daily_usd:.2f})"
                )
        return None

    def _check_position_cap(self, event: TradeEvent, shares: float) -> Optional[float]:
        """
        Clip shares so resulting position notional <= max_position_usd.
        Returns the (possibly reduced) allowable share count, or None if the
        cap blocks the trade outright.
        """
        if self.cfg.max_position_usd <= 0:
            return shares
        if event.side != "BUY":
            return shares  # sells reduce position, never blocked by this cap

        # Current holding in that outcome token (prefer live, fall back to sim).
        current_shares = 0.0
        try:
            live = self.poly.get_conditional_token_balance(event.token_id)
            if live is not None:
                current_shares = float(live)
        except Exception:
            pass
        if current_shares == 0.0 and self.cfg.dry_run:
            current_shares = self._sim_positions.get(event.token_id, 0.0)

        price = max(0.01, min(0.99, float(event.price)))
        current_notional = current_shares * price
        headroom_usd = self.cfg.max_position_usd - current_notional
        if headroom_usd <= 0:
            return None
        headroom_shares = headroom_usd / price
        if shares > headroom_shares:
            return headroom_shares
        return shares

    # ── Execution ──────────────────────────────────────────────

    def _aggressive_limit_price(
        self,
        token_id: str,
        is_buy: bool,
        fallback: float,
    ) -> float:
        """
        Pick a limit price that should fill immediately as a FAK.

        We use the best opposing quote (best_ask for BUY, best_bid for SELL)
        and nudge past it by slippage_bps (treating 10000 bps = $1 on the 0..1
        probability scale). Falls back to event price if the book read fails.
        """
        slip = self.cfg.slippage_bps / 10_000.0
        best_bid, best_ask = self.poly.get_best_prices(token_id)
        if is_buy:
            base = best_ask if best_ask is not None else fallback
            px = min(0.99, base + slip)
        else:
            base = best_bid if best_bid is not None else fallback
            px = max(0.01, base - slip)

        # Round to Polymarket's 0.01 (1 cent) tick size.
        px = round(px, 2)
        px = max(0.01, min(0.99, px))
        return px

    def _record_trade(self, usd: float) -> None:
        self._trades.append((time.time(), usd))

    def _record_sim_position(self, event: TradeEvent, shares: float, is_buy: bool) -> None:
        if not self.cfg.dry_run:
            return
        delta = shares if is_buy else -shares
        new = self._sim_positions.get(event.token_id, 0.0) + delta
        if abs(new) < 1e-9:
            self._sim_positions.pop(event.token_id, None)
        else:
            self._sim_positions[event.token_id] = new

    def execute(
        self,
        event: TradeEvent,
        shares: float,
        sizing_note: str = "",
    ) -> CopyResult:
        """
        Submit a FAK order mirroring ``event``. Assumes filter + sizing have
        already been applied; ``shares`` is the absolute share count to trade.
        """
        is_buy = event.side == "BUY"
        side = "BUY" if is_buy else "SELL"

        # Round share size to 2 decimals (Polymarket size precision).
        shares = round(float(shares), 2)
        if shares <= 0:
            return CopyResult(
                success=False, side=side, token_id=event.token_id,
                requested_shares=shares, requested_usd=0.0, limit_price=0.0,
                skipped_reason="shares rounded to zero",
            )

        # Position cap (buys only).
        clipped = self._check_position_cap(event, shares)
        if clipped is None:
            msg = "position cap reached for this token — skipping BUY"
            logger.warning(msg)
            return CopyResult(
                success=False, side=side, token_id=event.token_id,
                requested_shares=shares, requested_usd=shares * event.price,
                limit_price=0.0, skipped_reason=msg,
            )
        if clipped < shares:
            logger.warning(
                f"position cap clipped: {shares:.2f} -> {clipped:.2f} shares"
            )
            shares = round(clipped, 2)
            if shares <= 0:
                return CopyResult(
                    success=False, side=side, token_id=event.token_id,
                    requested_shares=shares, requested_usd=0.0, limit_price=0.0,
                    skipped_reason="position cap clipped to zero",
                )

        # Close handling: SELL requires we actually hold the token.
        if side == "SELL":
            try:
                held = self.poly.get_conditional_token_balance(event.token_id)
            except Exception:
                held = None
            if held is None and self.cfg.dry_run:
                held = self._sim_positions.get(event.token_id, 0.0)
            if held is not None and held < shares:
                if held <= 0:
                    return CopyResult(
                        success=False, side=side, token_id=event.token_id,
                        requested_shares=shares, requested_usd=0.0, limit_price=0.0,
                        skipped_reason=f"no holdings of {event.token_id[:10]}... to sell",
                    )
                logger.info(
                    f"SELL clipped to holdings: {shares:.2f} -> {held:.2f} shares"
                )
                shares = round(float(held), 2)

        # Min trade size guard.
        notional = shares * event.price
        if notional < self.cfg.min_trade_usd:
            return CopyResult(
                success=False, side=side, token_id=event.token_id,
                requested_shares=shares, requested_usd=notional, limit_price=0.0,
                skipped_reason=(
                    f"below min trade size "
                    f"(${notional:.2f} < ${self.cfg.min_trade_usd:.2f})"
                ),
            )

        # Daily caps.
        block = self._check_daily_caps(notional)
        if block is not None:
            logger.critical(block)
            return CopyResult(
                success=False, side=side, token_id=event.token_id,
                requested_shares=shares, requested_usd=notional, limit_price=0.0,
                skipped_reason=block,
            )

        limit_price = self._aggressive_limit_price(event.token_id, is_buy, event.price)

        # Dry run shortcut.
        if self.cfg.dry_run:
            logger.info(
                f"[DRY RUN] {side} {shares:.2f} sh @ {limit_price:.3f} "
                f"(mid~{event.price:.3f}, ${shares * limit_price:.2f}) "
                f"token={event.token_id[:10]}... | {sizing_note}"
            )
            self._record_trade(shares * limit_price)
            self._record_sim_position(event, shares, is_buy)
            return CopyResult(
                success=True, side=side, token_id=event.token_id,
                requested_shares=shares, requested_usd=shares * limit_price,
                limit_price=limit_price, filled_shares=shares,
                avg_price=limit_price, dry_run=True,
            )

        # Live execution.
        if side == "SELL":
            # Nudge the CLOB's conditional-token allowance cache before sells.
            self.poly.refresh_conditional_allowance(event.token_id)

        logger.warning(
            f"EXECUTING {side} {shares:.2f} sh @ {limit_price:.3f} "
            f"(${shares * limit_price:.2f}) token={event.token_id[:10]}... | {sizing_note}"
        )

        try:
            resp = self.poly.place_fak(
                token_id=event.token_id,
                price=limit_price,
                size=shares,
                side=side,
            )
        except Exception as exc:
            logger.error(f"order submit raised: {exc}")
            return CopyResult(
                success=False, side=side, token_id=event.token_id,
                requested_shares=shares, requested_usd=notional,
                limit_price=limit_price, error=str(exc),
            )

        return self._parse_order_response(
            resp, side, event, shares, limit_price
        )

    # ── Response parsing ───────────────────────────────────────

    def _parse_order_response(
        self,
        resp: dict,
        side: str,
        event: TradeEvent,
        requested_shares: float,
        limit_price: float,
    ) -> CopyResult:
        requested_usd = requested_shares * limit_price

        if not isinstance(resp, dict):
            return CopyResult(
                success=False, side=side, token_id=event.token_id,
                requested_shares=requested_shares, requested_usd=requested_usd,
                limit_price=limit_price, error=str(resp),
            )

        # Known shapes:
        #   {"success": true, "orderID": "...", "status": "matched", ...}
        #   {"errorMsg": "...", "success": false, ...}
        success = bool(resp.get("success", False))
        if not success and "errorMsg" in resp:
            err = str(resp.get("errorMsg") or resp.get("error") or "unknown error")
            logger.error(f"order rejected: {err}")
            return CopyResult(
                success=False, side=side, token_id=event.token_id,
                requested_shares=requested_shares, requested_usd=requested_usd,
                limit_price=limit_price, error=err,
            )

        order_id = str(resp.get("orderID") or resp.get("order_id") or resp.get("id") or "")
        status = str(resp.get("status") or "").lower()

        size_matched = resp.get("size_matched") or resp.get("sizeMatched") or resp.get("takingAmount")
        try:
            filled_shares = float(size_matched) if size_matched is not None else 0.0
        except (TypeError, ValueError):
            filled_shares = 0.0
        # If the field wasn't returned but status suggests full fill, trust requested.
        if filled_shares <= 0 and status in {"matched", "filled"}:
            filled_shares = requested_shares

        avg_price = limit_price
        raw_avg = resp.get("average_price") or resp.get("averagePrice")
        try:
            if raw_avg is not None:
                avg_price = float(raw_avg)
        except (TypeError, ValueError):
            pass

        filled_usd = filled_shares * avg_price
        if filled_shares > 0:
            self._record_trade(filled_usd)

        logger.success(
            f"ORDER OK ({status or 'ok'}) {side} "
            f"{filled_shares:.2f}/{requested_shares:.2f} sh "
            f"@ ~{avg_price:.3f} (${filled_usd:.2f}) "
            f"oid={order_id[:14]}"
        )
        return CopyResult(
            success=True, side=side, token_id=event.token_id,
            requested_shares=requested_shares, requested_usd=requested_usd,
            limit_price=limit_price, filled_shares=filled_shares,
            avg_price=avg_price, order_id=order_id,
        )

    # ── Diagnostics ────────────────────────────────────────────

    @property
    def trades_today(self) -> int:
        self._prune_trade_ledger()
        return len(self._trades)

    @property
    def usd_today(self) -> float:
        return self._daily_usd_spent()

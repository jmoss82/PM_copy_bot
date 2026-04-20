"""
Polymarket client.

Thin wrapper over py-clob-client for authenticated CLOB actions (quotes,
orders, balances) plus aiohttp helpers for Gamma market lookup and Data API
activity/positions.

Adapted from Prediction Market Arbitrage (example)/polymarket_client.py,
trimmed to what the copy bot needs and parameterised on a CopyBotConfig.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import aiohttp
from loguru import logger
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OrderArgs,
    OrderType,
)

from config import CopyBotConfig


GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"


def _mask(value: str | None, prefix: int = 6, suffix: int = 4) -> str:
    if not value:
        return "(missing)"
    if len(value) <= prefix + suffix:
        return value
    return f"{value[:prefix]}...{value[-suffix:]}"


@dataclass
class PolymarketMarket:
    """Gamma-sourced metadata for a binary market."""
    condition_id: str = ""
    question_id: str = ""
    question: str = ""
    slug: str = ""
    token_yes: str = ""
    token_no: str = ""
    outcomes: List[str] = field(default_factory=list)
    token_ids: List[str] = field(default_factory=list)
    active: bool = False
    closed: bool = False
    accepting_orders: bool = False
    end_date: str | None = None
    category: str | None = None
    _raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_gamma(cls, d: dict) -> "PolymarketMarket":
        try:
            token_ids = json.loads(d.get("clobTokenIds", "[]"))
        except (TypeError, ValueError):
            token_ids = []
        try:
            outcomes = json.loads(d.get("outcomes", "[]"))
        except (TypeError, ValueError):
            outcomes = []

        token_yes = token_ids[0] if len(token_ids) > 0 else ""
        token_no = token_ids[1] if len(token_ids) > 1 else ""

        return cls(
            condition_id=d.get("conditionId", "") or "",
            question_id=d.get("questionID", "") or "",
            question=d.get("question", "") or "",
            slug=d.get("slug", "") or "",
            token_yes=token_yes,
            token_no=token_no,
            outcomes=[str(o) for o in outcomes],
            token_ids=[str(t) for t in token_ids],
            active=bool(d.get("active")),
            closed=bool(d.get("closed")),
            accepting_orders=bool(d.get("acceptingOrders")),
            end_date=d.get("endDate"),
            category=d.get("groupItemTitle") or d.get("category"),
            _raw=d,
        )

    def token_for_outcome_index(self, idx: int) -> Optional[str]:
        if 0 <= idx < len(self.token_ids):
            return self.token_ids[idx]
        return None

    def outcome_label(self, idx: int) -> str:
        if 0 <= idx < len(self.outcomes):
            return self.outcomes[idx]
        return ""


class PolymarketClient:
    """Authenticated Polymarket CLOB + public metadata access."""

    QUOTE_RETRIES = 2
    QUOTE_RETRY_DELAY_SECONDS = 0.15

    def __init__(self, cfg: CopyBotConfig, derive_keys: bool = True):
        self.cfg = cfg
        self.clob: ClobClient = self._init_clob(derive_keys)

    # ── Init ───────────────────────────────────────────────────

    def _init_clob(self, derive_keys: bool) -> ClobClient:
        cfg = self.cfg
        explicit = all([cfg.poly_api_key, cfg.poly_api_secret, cfg.poly_api_passphrase])

        if explicit:
            logger.info("Polymarket: using configured API credentials")
            creds = ApiCreds(
                api_key=cfg.poly_api_key,
                api_secret=cfg.poly_api_secret,
                api_passphrase=cfg.poly_api_passphrase,
            )
        elif derive_keys and cfg.poly_private_key:
            logger.info("Polymarket: deriving API credentials (IP-bound)")
            l1 = ClobClient(
                host=cfg.clob_host,
                chain_id=cfg.chain_id,
                key=cfg.poly_private_key,
                signature_type=2,
            )
            raw = l1.derive_api_key()
            if isinstance(raw, dict):
                api_key = raw.get("apiKey") or raw.get("api_key")
                api_secret = raw.get("secret") or raw.get("api_secret")
                api_passphrase = raw.get("passphrase") or raw.get("api_passphrase")
            else:
                api_key = raw.api_key
                api_secret = raw.api_secret
                api_passphrase = raw.api_passphrase
            logger.info(f"Polymarket: derived API key {_mask(api_key)}")
            creds = ApiCreds(api_key, api_secret, api_passphrase)
        else:
            creds = ApiCreds(
                api_key=cfg.poly_api_key,
                api_secret=cfg.poly_api_secret,
                api_passphrase=cfg.poly_api_passphrase,
            )

        client = ClobClient(
            cfg.clob_host,
            key=cfg.poly_private_key,
            chain_id=cfg.chain_id,
            signature_type=2,
            funder=cfg.poly_funder,
            creds=creds,
        )
        return client

    # ── Market discovery (public, async) ───────────────────────

    @staticmethod
    async def fetch_market_by_slug(
        session: aiohttp.ClientSession, slug: str
    ) -> Optional[PolymarketMarket]:
        async with session.get(f"{GAMMA_API}/markets", params={"slug": slug}) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return PolymarketMarket.from_gamma(data[0]) if data else None

    @staticmethod
    async def fetch_market_by_condition_id(
        session: aiohttp.ClientSession, condition_id: str
    ) -> Optional[PolymarketMarket]:
        async with session.get(
            f"{GAMMA_API}/markets", params={"condition_ids": condition_id}
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            if not data:
                return None
            rows = data if isinstance(data, list) else [data]
            return PolymarketMarket.from_gamma(rows[0])

    # ── Order book (sync) ──────────────────────────────────────

    def get_orderbook(self, token_id: str) -> dict:
        return self.clob.get_order_book(token_id)

    def get_midpoint(self, token_id: str) -> Optional[float]:
        try:
            mp = self.clob.get_midpoint(token_id)
            if isinstance(mp, dict):
                return float(mp.get("mid", 0)) or None
            return float(mp) if mp else None
        except Exception:
            return None

    @staticmethod
    def _normalize_quote(
        bid: Optional[float], ask: Optional[float]
    ) -> Tuple[Optional[float], Optional[float]]:
        def _clip(v: Optional[float]) -> Optional[float]:
            if v is None:
                return None
            try:
                return max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                return None

        bid, ask = _clip(bid), _clip(ask)
        if bid is not None and ask is not None and bid > ask:
            bid, ask = ask, bid
        return bid, ask

    def get_best_prices(
        self,
        token_id: str,
        allow_midpoint_fallback: bool = True,
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Return (best_bid, best_ask) for an outcome token. Falls back to midpoint
        when the visible book is empty or absurdly wide.
        """
        last_err: Optional[Exception] = None
        for attempt in range(self.QUOTE_RETRIES + 1):
            try:
                book = self.get_orderbook(token_id)
                bids = book.bids if hasattr(book, "bids") else book.get("bids", [])
                asks = book.asks if hasattr(book, "asks") else book.get("asks", [])

                best_bid: Optional[float] = None
                best_ask: Optional[float] = None

                if bids:
                    bid_prices = [
                        float(lvl.price if hasattr(lvl, "price") else lvl["price"])
                        for lvl in bids
                    ]
                    best_bid = max(bid_prices) if bid_prices else None
                if asks:
                    ask_prices = [
                        float(lvl.price if hasattr(lvl, "price") else lvl["price"])
                        for lvl in asks
                    ]
                    best_ask = min(ask_prices) if ask_prices else None

                if best_bid is not None and best_ask is not None:
                    if allow_midpoint_fallback and (best_ask - best_bid) > 0.20:
                        mid = self.get_midpoint(token_id)
                        if mid is not None:
                            best_bid = mid - 0.005
                            best_ask = mid + 0.005
                    return self._normalize_quote(best_bid, best_ask)

                if best_bid is None or best_ask is None:
                    mid = self.get_midpoint(token_id)
                    if mid is not None:
                        if best_bid is None:
                            best_bid = mid - 0.005
                        if best_ask is None:
                            best_ask = mid + 0.005

                return self._normalize_quote(best_bid, best_ask)

            except Exception as exc:
                last_err = exc
                if attempt < self.QUOTE_RETRIES:
                    time.sleep(self.QUOTE_RETRY_DELAY_SECONDS * (attempt + 1))
                else:
                    logger.warning(f"quote failed for {_mask(token_id, 8, 4)}: {exc}")
        if last_err is not None:
            logger.debug(f"final quote error: {last_err}")
        return None, None

    # ── Order execution (sync) ─────────────────────────────────

    def place_fak(self, token_id: str, price: float, size: float, side: str) -> dict:
        """
        Place a FAK (fill-and-kill / immediate-or-cancel) limit order.

        side must be 'BUY' or 'SELL'. Polymarket prices are in [0, 1] and
        size is in shares.

        BUYs are routed through the "market order" builder path because
        Polymarket's server requires the USDC (maker) side of a BUY to be
        quantized to 2 decimals. The regular OrderArgs path computes
        maker = size * price which can land on a 4-decimal USDC value
        (e.g. 5.05 * 0.99 = 4.9995) and gets rejected. MarketOrderArgs
        inverts the math: maker = amount (USD, 2dec), taker = amount / price
        (shares, 4dec) — both within server tolerances. We still post with
        OrderType.FAK so only the build-side math changes.
        """
        side_u = side.upper()
        if side_u not in {"BUY", "SELL"}:
            raise ValueError(f"side must be BUY or SELL, got {side!r}")

        if side_u == "BUY":
            usd_amount = round(float(size) * float(price), 2)
            if usd_amount <= 0:
                raise ValueError(
                    f"BUY usd amount rounded to zero "
                    f"(size={size}, price={price})"
                )
            margs = MarketOrderArgs(
                token_id=token_id,
                amount=usd_amount,
                side="BUY",
                price=float(price),
            )
            signed = self.clob.create_market_order(margs)
            return self.clob.post_order(signed, OrderType.FAK)

        args = OrderArgs(token_id=token_id, price=price, size=size, side=side_u)
        signed = self.clob.create_order(args)
        return self.clob.post_order(signed, OrderType.FAK)

    def refresh_conditional_allowance(self, token_id: str) -> None:
        """
        Force the CLOB's conditional-token balance cache to refresh.

        Polymarket's CLOB has a known cache bug where an instantly-matched BUY
        doesn't refresh the balance view fast enough, which can cause a
        following SELL to fail with a balance/allowance error. Calling this
        before a SELL works around it.

        See: https://github.com/Polymarket/py-clob-client/issues/287
        """
        try:
            self.clob.update_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL, token_id=token_id
                )
            )
        except Exception as exc:
            logger.debug(
                f"update_balance_allowance(CONDITIONAL) for {_mask(token_id, 8, 4)}: {exc}"
            )

    # ── Balance / positions ────────────────────────────────────

    def get_usdc_balance(self) -> Optional[float]:
        """Return USDC collateral balance in human units (1e6 scaling applied)."""
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            bal = self.clob.get_balance_allowance(params)
            if isinstance(bal, dict):
                return float(bal.get("balance", "0")) / 1e6
            return None
        except Exception as exc:
            logger.warning(f"USDC balance lookup failed: {exc}")
            return None

    def get_conditional_token_balance(self, token_id: str) -> Optional[float]:
        """Return available outcome-token shares for this token."""
        try:
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL, token_id=token_id
            )
            bal = self.clob.get_balance_allowance(params)
            if not isinstance(bal, dict):
                return None
            raw_balance = bal.get("balance")
            raw_allowance = bal.get("allowance")
            balance = float(raw_balance) / 1e6 if raw_balance is not None else None
            allowance = float(raw_allowance) / 1e6 if raw_allowance is not None else None
            if balance is not None and allowance is not None:
                return min(balance, allowance)
            return balance if balance is not None else allowance
        except Exception as exc:
            logger.debug(f"conditional balance lookup for {_mask(token_id, 8, 4)}: {exc}")
            return None

    @staticmethod
    async def fetch_positions(
        session: aiohttp.ClientSession, wallet: str
    ) -> List[dict]:
        """Fetch current positions from the public Data API."""
        params = {"user": wallet, "limit": 500, "sizeThreshold": 0}
        async with session.get(f"{DATA_API}/positions", params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("positions", []) or []
            return []

    @staticmethod
    async def fetch_activity(
        session: aiohttp.ClientSession,
        wallet: str,
        limit: int = 100,
        after_ts: Optional[int] = None,
    ) -> List[dict]:
        """
        Fetch recent account activity (trades, rewards, etc.) from the Data API.

        Passing after_ts (unix seconds) filters to events strictly after that
        timestamp via the server's ``min_ts`` parameter when supported;
        callers should still dedup by transactionHash defensively.
        """
        params: dict = {
            "user": wallet,
            "limit": limit,
            "type": "TRADE",
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
        }
        if after_ts is not None:
            # Some Polymarket deployments expose 'min_ts' / 'start'; pass both
            # to be tolerant. The server ignores unknown params.
            params["min_ts"] = int(after_ts)
            params["start"] = int(after_ts)

        async with session.get(f"{DATA_API}/activity", params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()

        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            rows = data.get("activity") or data.get("data") or []
        else:
            rows = []
        return rows

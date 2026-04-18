"""
Activity Tracker.

Polls the Polymarket Data API activity feed for the leader wallet and emits
new trade events. Unlike a position-snapshot approach, this catches trades
that enter and exit inside a single poll window (common on 5m/15m markets).

State:
- In-memory set of seen transaction hashes (bounded, FIFO eviction).
- Persistent cursor (last seen unix timestamp) in ``state/cursor.json``
  so a restart doesn't replay the whole day nor miss the gap between polls.

Each /activity row the API returns for a TRADE looks roughly like:

    {
      "proxyWallet":   "0x...",
      "timestamp":     1734567890,
      "conditionId":   "0x...",
      "type":          "TRADE",
      "side":          "BUY" | "SELL",
      "asset":         "<token_id>",           # outcome token (ERC1155 id)
      "outcome":       "Yes" | "No" | "Up" | "Down" | ...,
      "outcomeIndex":  0 | 1,
      "price":         0.57,
      "size":          123.45,                 # in shares
      "usdcSize":      70.37,                  # dollar cost
      "transactionHash": "0x...",
      "title":         "Will BTC go up ...?",
      "slug":          "btc-updown-15m-...",
    }

We parse defensively (all field names are best-effort).
"""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set

import aiohttp
from loguru import logger

from polymarket_client import PolymarketClient


DEFAULT_STATE_FILE = Path(__file__).parent / "state" / "cursor.json"
SEEN_HASHES_MAX = 5000


@dataclass
class TradeEvent:
    """One detected trade on the leader's wallet."""
    tx_hash: str
    timestamp: int                # unix seconds
    side: str                     # "BUY" | "SELL"
    token_id: str                 # outcome token (ERC1155 id)
    condition_id: str
    outcome: str                  # "Yes" / "No" / "Up" / "Down" / ...
    outcome_index: int            # 0 or 1
    price: float                  # 0..1
    size: float                   # shares
    usdc_size: float              # dollar cost
    slug: str
    title: str
    raw: dict = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:
        short = (self.tx_hash or "")[:10]
        return (
            f"TradeEvent(tx={short}, {self.side} {self.size:.2f} sh "
            f"@ {self.price:.3f} ${self.usdc_size:.2f} "
            f"outcome={self.outcome} slug={self.slug})"
        )


class ActivityTracker:
    """
    Polls Polymarket Data API activity for the leader wallet.

    Usage:
        tracker = ActivityTracker(leader_addr)
        tracker.load_cursor()
        async with aiohttp.ClientSession() as sess:
            events = await tracker.poll(sess)
        tracker.save_cursor()
    """

    def __init__(
        self,
        target_address: str,
        state_file: Path = DEFAULT_STATE_FILE,
        resume_from_cursor: bool = False,
    ):
        self.target_address = target_address
        self.state_file = Path(state_file)
        self.resume_from_cursor = resume_from_cursor

        # Last processed event timestamp (unix seconds). 0 means "none yet".
        self._cursor_ts: int = 0

        # FIFO-bounded seen hashes (for dedup across restarts within one
        # session — restart dedup comes from cursor_ts, not this set).
        self._seen_hashes: Set[str] = set()
        self._seen_order: Deque[str] = deque(maxlen=SEEN_HASHES_MAX)

        self._consecutive_errors: int = 0
        self._last_poll_ts: float = 0.0

    # ── Cursor persistence ─────────────────────────────────────

    def load_cursor(self) -> int:
        """
        Load the persisted cursor timestamp.

        If resume_from_cursor is True, use the saved value. Otherwise reset to
        'now' so we only copy trades that happen after process start.
        """
        if not self.resume_from_cursor:
            self._cursor_ts = int(time.time())
            logger.info(
                f"Tracker: starting fresh cursor at now={self._cursor_ts} "
                f"(COPY_RESUME_FROM_CURSOR=false)"
            )
            return self._cursor_ts

        if not self.state_file.exists():
            self._cursor_ts = int(time.time())
            logger.info(
                f"Tracker: no state file at {self.state_file}, starting from now"
            )
            return self._cursor_ts
        try:
            data = json.loads(self.state_file.read_text())
            self._cursor_ts = int(data.get("cursor_ts", 0) or 0)
            if self._cursor_ts <= 0:
                self._cursor_ts = int(time.time())
            logger.info(
                f"Tracker: resumed cursor from {self.state_file} "
                f"at ts={self._cursor_ts}"
            )
        except Exception as exc:
            self._cursor_ts = int(time.time())
            logger.warning(
                f"Tracker: could not read {self.state_file}: {exc}. "
                "Starting from now."
            )
        return self._cursor_ts

    def save_cursor(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "cursor_ts": self._cursor_ts,
                "target_address": self.target_address,
                "saved_at": int(time.time()),
            }
            self.state_file.write_text(json.dumps(payload, indent=2))
        except Exception as exc:
            logger.debug(f"Tracker: failed to persist cursor: {exc}")

    @property
    def cursor_ts(self) -> int:
        return self._cursor_ts

    @property
    def consecutive_errors(self) -> int:
        return self._consecutive_errors

    # ── Polling ────────────────────────────────────────────────

    async def poll(self, session: aiohttp.ClientSession, limit: int = 100) -> List[TradeEvent]:
        """
        Fetch activity since the cursor, dedup, update cursor, return new
        trades in chronological order (oldest first).
        """
        try:
            rows = await PolymarketClient.fetch_activity(
                session,
                self.target_address,
                limit=limit,
                after_ts=max(0, self._cursor_ts - 1),  # small overlap to be safe
            )
            self._consecutive_errors = 0
            self._last_poll_ts = time.time()
        except Exception as exc:
            self._consecutive_errors += 1
            logger.error(
                f"Tracker: activity poll failed "
                f"(attempt {self._consecutive_errors}): {exc}"
            )
            return []

        events: List[TradeEvent] = []
        for row in rows:
            try:
                event = self._parse_row(row)
            except Exception as exc:
                logger.debug(f"Tracker: could not parse row: {exc} | row={row}")
                continue
            if event is None:
                continue
            if event.tx_hash and event.tx_hash in self._seen_hashes:
                continue
            if event.timestamp <= self._cursor_ts:
                # Overlap window or pagination edge — skip silently.
                continue
            events.append(event)

        events.sort(key=lambda e: (e.timestamp, e.tx_hash))

        for e in events:
            if e.tx_hash:
                if len(self._seen_order) == self._seen_order.maxlen and self._seen_order:
                    old = self._seen_order[0]
                    self._seen_hashes.discard(old)
                self._seen_order.append(e.tx_hash)
                self._seen_hashes.add(e.tx_hash)
            if e.timestamp > self._cursor_ts:
                self._cursor_ts = e.timestamp

        return events

    # ── Parsing ────────────────────────────────────────────────

    @staticmethod
    def _parse_row(row: dict) -> Optional[TradeEvent]:
        if not isinstance(row, dict):
            return None

        # Only TRADE activity.
        etype = str(row.get("type") or row.get("eventType") or "").upper()
        if etype and etype != "TRADE":
            return None

        side = str(row.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            return None

        # Timestamp may be seconds or milliseconds.
        raw_ts = row.get("timestamp") or row.get("time") or row.get("blockTimestamp") or 0
        try:
            ts = int(raw_ts)
        except (TypeError, ValueError):
            return None
        if ts > 10_000_000_000:  # ms -> s
            ts //= 1000
        if ts <= 0:
            return None

        tx_hash = str(row.get("transactionHash") or row.get("txHash") or row.get("hash") or "")

        def _f(key: str, default: float = 0.0) -> float:
            v = row.get(key)
            try:
                return float(v) if v is not None else default
            except (TypeError, ValueError):
                return default

        price = _f("price", 0.0)
        size = _f("size", 0.0)
        usdc_size = _f("usdcSize", price * size)

        token_id = str(row.get("asset") or row.get("tokenId") or row.get("assetId") or "")
        condition_id = str(row.get("conditionId") or row.get("condition_id") or "")
        outcome = str(row.get("outcome") or "")
        try:
            outcome_index = int(row.get("outcomeIndex", 0) or 0)
        except (TypeError, ValueError):
            outcome_index = 0

        if not token_id or size <= 0 or price <= 0:
            return None

        return TradeEvent(
            tx_hash=tx_hash,
            timestamp=ts,
            side=side,
            token_id=token_id,
            condition_id=condition_id,
            outcome=outcome,
            outcome_index=outcome_index,
            price=price,
            size=size,
            usdc_size=usdc_size,
            slug=str(row.get("slug") or ""),
            title=str(row.get("title") or row.get("question") or ""),
            raw=row,
        )

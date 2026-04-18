"""
Market filter.

Given a PolymarketMarket (or just its slug/question), decide whether the copy
bot should follow trades on it. We default to "crypto_short" — BTC/ETH (and a
few other liquid majors) on 5-minute or 15-minute timeframes — since that's
what this bot is tuned for. Everything else is configurable.

Slug patterns observed on Polymarket:
    btc-updown-5m-<unix_ts>
    btc-updown-15m-<unix_ts>
    btc-up-or-down-...-hourly-...
    eth-updown-5m-<unix_ts>
    eth-updown-15m-<unix_ts>
    btc-price-hits-...
    what-price-will-bitcoin-hit-on-...
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from polymarket_client import PolymarketMarket


CRYPTO_TOKENS = ("btc", "bitcoin", "eth", "ethereum", "sol", "solana")
CRYPTO_SHORT_ONLY_TOKENS = ("btc", "bitcoin", "eth", "ethereum")

SHORT_TIMEFRAME_RE = re.compile(
    r"(?:^|[-_])(?:updown|up-or-down|up-down|price)[-_]?(?P<tf>5m|15m)(?:[-_]|$)",
    re.IGNORECASE,
)
# Fallback: any slug containing '-5m-' or '-15m-' near an updown/price term.
LOOSE_SHORT_TIMEFRAME_RE = re.compile(r"(?:^|[-_])(?P<tf>5m|15m)(?:[-_]|$)", re.IGNORECASE)


@dataclass
class FilterDecision:
    allowed: bool
    reason: str
    category: str = ""       # "crypto_short" | "crypto_any" | "other"
    timeframe: str = ""      # "5m" | "15m" | "hourly" | "other" | ""
    symbol: str = ""         # "btc" | "eth" | ...


def _detect_symbol(slug: str, question: str) -> str:
    slug_l = (slug or "").lower()
    q_l = (question or "").lower()
    for token in CRYPTO_TOKENS:
        if token in slug_l or token in q_l:
            # Normalise to ticker
            if token in ("bitcoin", "btc"):
                return "btc"
            if token in ("ethereum", "eth"):
                return "eth"
            if token in ("solana", "sol"):
                return "sol"
    return ""


def _detect_timeframe(slug: str) -> str:
    slug_l = (slug or "").lower()
    m = SHORT_TIMEFRAME_RE.search(slug_l)
    if m:
        return m.group("tf").lower()
    m = LOOSE_SHORT_TIMEFRAME_RE.search(slug_l)
    if m:
        return m.group("tf").lower()
    if "hourly" in slug_l or re.search(r"(?:^|[-_])1h(?:[-_]|$)", slug_l):
        return "hourly"
    return "other"


def _substring_match(slug: str, patterns: Iterable[str]) -> Optional[str]:
    slug_l = (slug or "").lower()
    for pat in patterns:
        if pat and pat in slug_l:
            return pat
    return None


def classify(slug: str, question: str = "") -> FilterDecision:
    """
    Classify a market purely on slug / question text.

    Returns a decision that *describes* the market; whether to copy it is
    determined by ``should_copy`` against the active filter mode.
    """
    symbol = _detect_symbol(slug, question)
    timeframe = _detect_timeframe(slug)

    if symbol and timeframe in ("5m", "15m"):
        return FilterDecision(
            allowed=True,
            reason=f"{symbol.upper()} {timeframe} short-timeframe crypto",
            category="crypto_short",
            timeframe=timeframe,
            symbol=symbol,
        )
    if symbol:
        return FilterDecision(
            allowed=True,
            reason=f"{symbol.upper()} market ({timeframe or 'unknown timeframe'})",
            category="crypto_any",
            timeframe=timeframe,
            symbol=symbol,
        )
    return FilterDecision(
        allowed=False,
        reason="no recognised crypto symbol in slug/question",
        category="other",
        timeframe=timeframe,
        symbol="",
    )


def should_copy(
    mode: str,
    slug: str,
    question: str = "",
    extra_allow: Optional[Iterable[str]] = None,
    extra_block: Optional[Iterable[str]] = None,
) -> FilterDecision:
    """
    Apply the configured filter mode plus user-provided allow/block lists.

    mode is one of: 'crypto_short', 'crypto_any', 'all'.
    """
    extra_allow = list(extra_allow or [])
    extra_block = list(extra_block or [])

    hit = _substring_match(slug, extra_block)
    if hit:
        return FilterDecision(
            allowed=False,
            reason=f"blocked by COPY_EXTRA_BLOCK_SLUGS ({hit!r})",
            category="other",
        )

    classification = classify(slug, question)

    hit = _substring_match(slug, extra_allow)
    if hit:
        return FilterDecision(
            allowed=True,
            reason=f"allowed by COPY_EXTRA_ALLOW_SLUGS ({hit!r})",
            category=classification.category or "other",
            timeframe=classification.timeframe,
            symbol=classification.symbol,
        )

    if mode == "all":
        return FilterDecision(
            allowed=True,
            reason="mode=all — allow every market",
            category=classification.category or "other",
            timeframe=classification.timeframe,
            symbol=classification.symbol,
        )

    if mode == "crypto_any":
        if classification.symbol:
            return FilterDecision(
                allowed=True,
                reason=classification.reason,
                category="crypto_any",
                timeframe=classification.timeframe,
                symbol=classification.symbol,
            )
        return FilterDecision(
            allowed=False,
            reason="mode=crypto_any but no crypto symbol detected",
        )

    # Default: crypto_short — BTC/ETH only, 5m or 15m only.
    if (
        classification.symbol in CRYPTO_SHORT_ONLY_TOKENS
        and classification.timeframe in ("5m", "15m")
    ):
        return FilterDecision(
            allowed=True,
            reason=classification.reason,
            category="crypto_short",
            timeframe=classification.timeframe,
            symbol=classification.symbol,
        )
    return FilterDecision(
        allowed=False,
        reason=(
            "mode=crypto_short requires BTC/ETH + 5m/15m "
            f"(got symbol={classification.symbol or 'none'}, "
            f"tf={classification.timeframe or 'unknown'})"
        ),
        timeframe=classification.timeframe,
        symbol=classification.symbol,
    )


def decide_from_market(
    mode: str,
    market: PolymarketMarket,
    extra_allow: Optional[Iterable[str]] = None,
    extra_block: Optional[Iterable[str]] = None,
) -> FilterDecision:
    """Convenience wrapper for a full PolymarketMarket object."""
    return should_copy(
        mode=mode,
        slug=market.slug,
        question=market.question,
        extra_allow=extra_allow,
        extra_block=extra_block,
    )

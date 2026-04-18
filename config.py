"""
Copy bot configuration.

Loads Polymarket auth and copy-trading knobs from environment variables
(.env for local dev, platform env vars for Railway).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv


_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)


SCALING_MODES = {"fixed_notional", "proportional", "fixed_ratio", "fixed_size"}
MARKET_FILTERS = {"crypto_short", "crypto_any", "all"}


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"true", "1", "yes", "y", "on"}


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _str_list(name: str) -> List[str]:
    raw = os.getenv(name, "") or ""
    return [s.strip().lower() for s in raw.split(",") if s.strip()]


@dataclass
class CopyBotConfig:
    # ── Polymarket auth ────────────────────────────────────────
    poly_private_key: str = ""
    poly_funder: str = ""
    poly_api_key: str = ""
    poly_api_secret: str = ""
    poly_api_passphrase: str = ""
    chain_id: int = 137
    clob_host: str = "https://clob.polymarket.com"

    # ── Leader ─────────────────────────────────────────────────
    target_address: str = ""

    # ── Sizing ─────────────────────────────────────────────────
    scaling_mode: str = "fixed_notional"
    fixed_notional_usd: float = 25.0
    fixed_ratio: float = 0.05
    fixed_size: float = 10.0

    # ── Risk ───────────────────────────────────────────────────
    max_trade_usd: float = 50.0
    max_daily_usd: float = 500.0
    max_daily_trades: int = 100
    min_trade_usd: float = 1.0
    max_position_usd: float = 200.0

    # ── Execution ──────────────────────────────────────────────
    poll_interval_seconds: float = 3.0
    slippage_bps: int = 100
    mirror_closes: bool = True

    # ── Market filter ──────────────────────────────────────────
    market_filter: str = "crypto_short"
    extra_allow_slugs: List[str] = field(default_factory=list)
    extra_block_slugs: List[str] = field(default_factory=list)

    # ── Startup ────────────────────────────────────────────────
    resume_from_cursor: bool = False

    # ── Safety ─────────────────────────────────────────────────
    dry_run: bool = True

    # ── Logging ────────────────────────────────────────────────
    log_level: str = "INFO"

    # Normalised address for convenience.
    @property
    def target_address_lc(self) -> str:
        return self.target_address.lower()


def load_config() -> CopyBotConfig:
    cfg = CopyBotConfig(
        poly_private_key=os.getenv("POLY_PRIVATE_KEY", "") or "",
        poly_funder=os.getenv("POLY_FUNDER", "") or "",
        poly_api_key=os.getenv("POLY_API_KEY", "") or "",
        poly_api_secret=os.getenv("POLY_API_SECRET", "") or "",
        poly_api_passphrase=os.getenv("POLY_API_PASSPHRASE", "") or "",
        chain_id=_int("CHAIN_ID", 137),
        clob_host=os.getenv("CLOB_HOST", "https://clob.polymarket.com") or "https://clob.polymarket.com",
        target_address=os.getenv("COPY_TARGET_ADDRESS", "") or "",
        scaling_mode=(os.getenv("COPY_SCALING_MODE", "fixed_notional") or "fixed_notional").strip().lower(),
        fixed_notional_usd=_float("COPY_FIXED_NOTIONAL_USD", 25.0),
        fixed_ratio=_float("COPY_FIXED_RATIO", 0.05),
        fixed_size=_float("COPY_FIXED_SIZE", 10.0),
        max_trade_usd=_float("COPY_MAX_TRADE_USD", 50.0),
        max_daily_usd=_float("COPY_MAX_DAILY_USD", 500.0),
        max_daily_trades=_int("COPY_MAX_DAILY_TRADES", 100),
        min_trade_usd=_float("COPY_MIN_TRADE_USD", 1.0),
        max_position_usd=_float("COPY_MAX_POSITION_USD", 200.0),
        poll_interval_seconds=_float("COPY_POLL_INTERVAL", 3.0),
        slippage_bps=_int("COPY_SLIPPAGE_BPS", 100),
        mirror_closes=_bool("COPY_MIRROR_CLOSES", True),
        market_filter=(os.getenv("COPY_MARKET_FILTER", "crypto_short") or "crypto_short").strip().lower(),
        extra_allow_slugs=_str_list("COPY_EXTRA_ALLOW_SLUGS"),
        extra_block_slugs=_str_list("COPY_EXTRA_BLOCK_SLUGS"),
        resume_from_cursor=_bool("COPY_RESUME_FROM_CURSOR", False),
        dry_run=_bool("COPY_DRY_RUN", True),
        log_level=(os.getenv("COPY_LOG_LEVEL", "INFO") or "INFO").upper(),
    )
    return cfg


def validate_config(cfg: CopyBotConfig) -> None:
    """Raise ValueError if the config is unusable for live or dry-run."""
    if not cfg.target_address:
        raise ValueError("COPY_TARGET_ADDRESS is required (the leader wallet to copy)")
    if not (cfg.target_address.startswith("0x") and len(cfg.target_address) == 42):
        raise ValueError(f"Invalid COPY_TARGET_ADDRESS: {cfg.target_address}")

    if cfg.scaling_mode not in SCALING_MODES:
        raise ValueError(
            f"COPY_SCALING_MODE must be one of {sorted(SCALING_MODES)}, got {cfg.scaling_mode!r}"
        )

    if cfg.market_filter not in MARKET_FILTERS:
        raise ValueError(
            f"COPY_MARKET_FILTER must be one of {sorted(MARKET_FILTERS)}, got {cfg.market_filter!r}"
        )

    # Auth is required even in dry-run because we still want to connect to the
    # CLOB for live market quotes and to validate credentials up-front.
    if not cfg.poly_private_key:
        raise ValueError("POLY_PRIVATE_KEY is required")
    if not cfg.poly_private_key.startswith("0x") or len(cfg.poly_private_key) != 66:
        raise ValueError("POLY_PRIVATE_KEY must be a 0x-prefixed 32-byte hex string")
    if not cfg.poly_funder:
        raise ValueError("POLY_FUNDER is required (your Polymarket proxy wallet address)")
    if not (cfg.poly_funder.startswith("0x") and len(cfg.poly_funder) == 42):
        raise ValueError(f"Invalid POLY_FUNDER: {cfg.poly_funder}")

    if cfg.poll_interval_seconds < 0.5:
        raise ValueError("COPY_POLL_INTERVAL must be >= 0.5 seconds")
    if cfg.slippage_bps < 0:
        raise ValueError("COPY_SLIPPAGE_BPS must be non-negative")

    if cfg.scaling_mode == "fixed_notional" and cfg.fixed_notional_usd <= 0:
        raise ValueError("COPY_FIXED_NOTIONAL_USD must be > 0 when scaling_mode=fixed_notional")
    if cfg.scaling_mode == "fixed_ratio" and cfg.fixed_ratio <= 0:
        raise ValueError("COPY_FIXED_RATIO must be > 0 when scaling_mode=fixed_ratio")
    if cfg.scaling_mode == "fixed_size" and cfg.fixed_size <= 0:
        raise ValueError("COPY_FIXED_SIZE must be > 0 when scaling_mode=fixed_size")

#!/usr/bin/env python3
"""
Diagnose Polymarket wallet configuration.

Reads .env and shows:
  - the EOA address derived from POLY_PRIVATE_KEY
  - the POLY_FUNDER address you configured
  - USDC collateral balance at POLY_FUNDER (via CLOB)
  - open positions at POLY_FUNDER (via Data API)

Run locally:
    python check_wallet.py
"""
from __future__ import annotations

import asyncio
import sys

import aiohttp
from eth_account import Account

from config import load_config
from polymarket_client import PolymarketClient


def _mask(value: str, prefix: int = 6, suffix: int = 4) -> str:
    if not value:
        return "(missing)"
    if len(value) <= prefix + suffix:
        return value
    return f"{value[:prefix]}...{value[-suffix:]}"


async def main() -> int:
    cfg = load_config()

    # Only enforce the two fields this diagnostic actually uses.
    if not cfg.poly_private_key:
        print("POLY_PRIVATE_KEY is not set in your environment (.env)")
        return 1
    if not cfg.poly_funder:
        print("POLY_FUNDER is not set in your environment (.env)")
        return 1

    print("=" * 64)
    print("  Polymarket Wallet Diagnostic")
    print("=" * 64)

    # 1. Derive EOA from private key
    try:
        acct = Account.from_key(cfg.poly_private_key)
        eoa = acct.address
    except Exception as exc:
        print(f"could not derive EOA from POLY_PRIVATE_KEY: {exc}")
        return 1

    print()
    print(f"POLY_PRIVATE_KEY      : {_mask(cfg.poly_private_key, 10, 6)}")
    print(f"  -> EOA (signer)     : {eoa}")
    print()
    print(f"POLY_FUNDER (in env)  : {cfg.poly_funder}")
    print()

    if eoa.lower() == cfg.poly_funder.lower():
        print("!! WARNING: POLY_FUNDER equals the EOA address.")
        print("   On Polymarket, POLY_FUNDER must be your PROXY wallet")
        print("   (the smart-contract wallet that holds your USDC),")
        print("   NOT the EOA derived from your private key.")
        print("   Visit https://polymarket.com/profile to see your proxy")
        print("   address — it's the one shown in your profile.")
        print()

    # 2. USDC balance via CLOB (uses POLY_FUNDER)
    print("-" * 64)
    print(" USDC collateral balance (POLY_FUNDER, via CLOB)")
    print("-" * 64)
    try:
        poly = PolymarketClient(cfg)
        balance = poly.get_usdc_balance()
        if balance is None:
            print("  -> could not read USDC balance")
        else:
            print(f"  -> ${balance:.2f}")
    except Exception as exc:
        print(f"  -> error: {exc}")
        return 1

    # 3. Data API positions for POLY_FUNDER
    print()
    print("-" * 64)
    print(" Data API positions for POLY_FUNDER")
    print("-" * 64)
    async with aiohttp.ClientSession() as session:
        try:
            positions = await PolymarketClient.fetch_positions(session, cfg.poly_funder)
        except Exception as exc:
            print(f"  -> error: {exc}")
            positions = []

    if not positions:
        print("  (no positions found at this address)")
    else:
        for pos in positions[:10]:
            title = pos.get("title") or pos.get("slug") or "?"
            outcome = pos.get("outcome") or "?"
            size = pos.get("size") or pos.get("shares") or 0
            cur_price = pos.get("curPrice") or pos.get("currentPrice") or 0
            cur_val = pos.get("currentValue")
            if cur_val is None:
                try:
                    cur_val = float(size) * float(cur_price)
                except (TypeError, ValueError):
                    cur_val = 0.0
            print(
                f"  - {title[:52]:52s} | {outcome:4s} | "
                f"{float(size):8.2f} sh @ {float(cur_price):.3f} "
                f"(${float(cur_val):.2f})"
            )
        if len(positions) > 10:
            print(f"  ... and {len(positions) - 10} more")

    # 4. Data API positions for the EOA (in case the user put EOA as funder by mistake)
    if eoa.lower() != cfg.poly_funder.lower():
        print()
        print("-" * 64)
        print(" Data API positions for EOA (for comparison)")
        print("-" * 64)
        async with aiohttp.ClientSession() as session:
            try:
                eoa_positions = await PolymarketClient.fetch_positions(session, eoa)
            except Exception as exc:
                print(f"  -> error: {exc}")
                eoa_positions = []
        if not eoa_positions:
            print("  (no positions at EOA — expected if EOA is not your proxy)")
        else:
            total = sum(
                float(p.get("currentValue") or 0) for p in eoa_positions
            )
            print(
                f"  -> {len(eoa_positions)} position(s) found at EOA "
                f"(total ~${total:.2f})"
            )
            print("  !! The EOA address has positions — you may have set the")
            print("     wrong address as POLY_FUNDER. Double-check which one")
            print("     Polymarket shows in your profile.")

    print()
    print("=" * 64)
    print(" Next step: compare the addresses above with what Polymarket")
    print(" shows at https://polymarket.com/profile (your proxy address).")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

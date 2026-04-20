# Polymarket Copy Bot

Monitors one Polymarket wallet (the "leader") in near-real-time and mirrors
its trades on your own Polymarket account. Tuned by default for
short-timeframe crypto markets (BTC/ETH, 5m & 15m) with a fixed dollar-per-trade
sizing model.

## How It Works

1. Every `COPY_POLL_INTERVAL` seconds, the bot polls the Polymarket Data API
   activity feed for the leader wallet.
2. New TRADE events are deduped against the last-seen cursor (`state/cursor.json`)
   and an in-memory set of transaction hashes.
3. For each new trade, the market filter decides whether to follow it
   (default: BTC/ETH on 5m or 15m markets only).
4. The copier converts the leader's action into our share size according to
   the configured sizing mode and applies per-trade, daily, position, and
   min-size caps.
5. A FAK (fill-and-kill) limit order is submitted through `py-clob-client`
   priced aggressively through the spread so it fills immediately or not at
   all — no resting orders on short-duration markets.

Only one leader is supported per process; run a second copy of this folder
with a different `COPY_TARGET_ADDRESS` if you want to mirror multiple leaders.

## File Layout

| File                   | Purpose                                                          |
|------------------------|------------------------------------------------------------------|
| `bot.py`               | Async main loop, startup, signals, heartbeat, logging setup.     |
| `config.py`            | `.env` loader, dataclass, validation.                            |
| `polymarket_client.py` | py-clob-client wrapper (auth, quotes, orders, balances, Data API).|
| `tracker.py`           | Polls the Data API activity feed and emits `TradeEvent`s.        |
| `copier.py`            | Sizing, caps, FAK order submission, dry-run simulation.          |
| `market_filter.py`     | Classifies markets (crypto symbol + timeframe) from slug/question.|

## Setup

### 1. Install dependencies

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure `.env`

Copy `.env.example` to `.env` and fill in the required values:

```bash
cp .env.example .env
```

Required:

- `POLY_PRIVATE_KEY` — your Polygon EOA private key (hex, 0x-prefixed).
- `POLY_FUNDER` — your Polymarket proxy wallet address (holds USDC collateral).
- `COPY_TARGET_ADDRESS` — the leader wallet (proxy address) to copy.

Optional (auto-derived at startup from `POLY_PRIVATE_KEY` if left blank):

- `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`.

### 3. Run in dry-run first

`COPY_DRY_RUN=true` (the default) logs every action without submitting live
orders. Leave it on until you've watched a few live leader trades flow
through the bot and you're happy with the sizing.

```bash
python bot.py
```

### 4. Go live

Flip `COPY_DRY_RUN=false` in `.env` and restart.

## Env Var Reference

### Polymarket auth

| Variable              | Default                              | Notes                                     |
|-----------------------|--------------------------------------|-------------------------------------------|
| `POLY_PRIVATE_KEY`    | (required)                           | Signer key (0x…64 hex chars).             |
| `POLY_FUNDER`         | (required)                           | Your Polymarket proxy wallet address.     |
| `POLY_API_KEY`        | (derived)                            | Leave blank to derive from private key.   |
| `POLY_API_SECRET`     | (derived)                            |                                           |
| `POLY_API_PASSPHRASE` | (derived)                            |                                           |
| `CHAIN_ID`            | `137`                                | Polygon mainnet.                          |
| `CLOB_HOST`           | `https://clob.polymarket.com`        |                                           |

### Copy targeting & sizing

| Variable                 | Default           | Notes                                                   |
|--------------------------|-------------------|---------------------------------------------------------|
| `COPY_TARGET_ADDRESS`    | (required)        | Leader wallet's proxy address (0x… 40 hex chars).       |
| `COPY_SCALING_MODE`      | `fixed_notional`  | `fixed_notional` / `proportional` / `fixed_ratio` / `fixed_size`. |
| `COPY_FIXED_NOTIONAL_USD`| `25.0`            | $ per signal in `fixed_notional` mode.                  |
| `COPY_FIXED_RATIO`       | `0.05`            | Multiplier on leader's share size (`fixed_ratio`).      |
| `COPY_FIXED_SIZE`        | `10.0`            | Absolute shares per signal (`fixed_size`).              |

### Risk guards

| Variable                 | Default   | Notes                                              |
|--------------------------|-----------|----------------------------------------------------|
| `COPY_MAX_TRADE_USD`     | `50.0`    | Per-order $ cap. `0` disables.                     |
| `COPY_MAX_DAILY_USD`     | `500.0`   | Rolling 24h $ spend cap. `0` disables.             |
| `COPY_MAX_DAILY_TRADES`  | `100`     | Rolling 24h trade count cap. `0` disables.         |
| `COPY_MIN_TRADE_USD`     | `1.0`     | Ignore signals smaller than this in $.             |
| `COPY_MAX_POSITION_USD`  | `200.0`   | Max $ exposure per outcome token. `0` disables.    |

### Execution

| Variable              | Default | Notes                                                                    |
|-----------------------|---------|--------------------------------------------------------------------------|
| `COPY_POLL_INTERVAL`  | `3.0`   | Seconds between activity polls. Shorter = more API load.                 |
| `COPY_SLIPPAGE_BPS`   | `100`   | Aggressive offset from best quote for FAK orders (10000 bps = $1 price). |
| `COPY_MIRROR_CLOSES`  | `true`  | If `false`, only mirror BUYs and let the bot hold through resolution.    |
| `COPY_ORDER_RETRIES`  | `0`     | Extra live reattempts after a FAK "no match found" response. `0` = fail fast. |

### Market filter

| Variable                  | Default         | Notes                                                    |
|---------------------------|-----------------|----------------------------------------------------------|
| `COPY_MARKET_FILTER`      | `crypto_short`  | `crypto_short` (BTC/ETH + 5m/15m only) / `crypto_any` (BTC/ETH/SOL/XRP/DOGE/HYPE/ADA/AVAX/LINK/LTC/DOT/SHIB/MATIC/TRX) / `all`. |
| `COPY_EXTRA_ALLOW_SLUGS`  | `""`            | Comma-separated slug substrings to always follow.        |
| `COPY_EXTRA_BLOCK_SLUGS`  | `""`            | Comma-separated slug substrings to always skip.          |

### Startup & safety

| Variable                   | Default | Notes                                                         |
|----------------------------|---------|---------------------------------------------------------------|
| `COPY_RESUME_FROM_CURSOR`  | `false` | If `true`, replay missed trades from `state/cursor.json`.     |
| `COPY_DRY_RUN`             | `true`  | SAFE DEFAULT — flip to `false` for live orders.               |
| `COPY_LOG_LEVEL`           | `INFO`  | loguru level for stderr. File log is always at DEBUG.         |

## Running

### Local

```bash
python bot.py
```

Ctrl-C gracefully persists the cursor and prints a summary block.

### Railway

`railway.json` is already configured. Create a new Railway service from this
folder and set every env var in the Railway dashboard. The service will
run `python bot.py` with an auto-restart policy.

## Logs & State

- `logs/copy_bot_*.log` — daily-rotated DEBUG log, retained 14 days.
- `state/cursor.json` — last processed activity timestamp. Deleting it
  before a start is equivalent to `COPY_RESUME_FROM_CURSOR=false`.

## Operational Notes

- **Dry run first.** Always. Watch a few cycles of real leader trades flow
  through the bot and verify the sizing and filter decisions make sense
  before you flip `COPY_DRY_RUN=false`.
- **First start ignores history.** By default the bot starts its cursor at
  "now", so past leader trades are not copied. If you want to resume across
  a restart without missing anything, set `COPY_RESUME_FROM_CURSOR=true`.
- **Market filter skips are quiet.** When the leader trades something
  outside your filter (e.g. an election market while you're in
  `crypto_short`), it's logged at INFO and skipped. No order sent.
- **Short markets can expire mid-order.** FAK ensures we never leave
  resting size on a market that's about to resolve.
- **Live execution uses visible liquidity only.** The bot no longer invents
  synthetic prices from midpoint when the opposing book side is empty, so
  some live trades will now skip or fail fast instead of posting a doomed FAK.
- **Capturing closes.** If `COPY_MIRROR_CLOSES=false`, the bot only mirrors
  BUYs and holds through market resolution. For 5m/15m markets this
  typically works — the market auto-settles — but you'll miss mid-market
  exits the leader takes.

## Adding a Second Leader

Copy this folder (e.g. `polymarket_copy_bot_2/`), give it a different
`.env` with a different `COPY_TARGET_ADDRESS`, and deploy as a separate
Railway service. Each bot is fully isolated: its own cursor file, its own
daily caps, its own logs.

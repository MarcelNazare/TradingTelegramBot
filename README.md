@ -1,579 +1,37 @@
# README.md (Updated)

```markdown
# Forex Signal Bot — Weltrade Micro (Cent) Account Edition

A Python-based forex signal generator that detects supply/demand zones, liquidity pools, and support/resistance levels on major currency pairs, then sends formatted trade alerts to Telegram. Built for swing trades (hours to a day) with ATR-adaptive stops, a strict 1:3 risk-reward ratio, and position sizing tuned for small cent accounts.

> ⚠️ **This is a signal bot, not an auto-trader.** It does not place orders. It sends you a notification with entry, stop-loss, take-profit, and suggested lot size. You decide whether to execute.

---

## Table of Contents

- [What It Does](#what-it-does)
- [What It Does NOT Do](#what-it-does-not-do)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Installation](#installation)
- [Environment Variables](#environment-variables)
- [Configuration](#configuration)
- [Telegram Setup](#telegram-setup)
- [Running the Bot](#running-the-bot)
- [Makefile Reference](#makefile-reference)
- [Deployment on PythonAnywhere (Free Tier)](#deployment-on-pythonanywhere-free-tier)
- [Calibrating `PIP_VALUE_STANDARD`](#calibrating-pip_value_standard)
- [Understanding the Output](#understanding-the-output)
- [The Risk Math (Read This)](#the-risk-math-read-this)
- [Limitations](#limitations)
- [Roadmap](#roadmap)
- [Disclaimer](#disclaimer)

---

## What It Does

The bot runs once per invocation (designed for a daily schedule after the New York close). For each of seven major pairs, it:

1. **Fetches** the last 6 months of daily OHLCV data from Yahoo Finance.
2. **Detects market structure**:
   - **Supply/demand zones** — the last opposite candle before a strong impulse move (body > 1.5× the 20-period average body).
   - **Liquidity pools** — equal highs/lows within a 3-pip tolerance.
   - **Support/resistance** — swing highs/lows with at least 2 touches.
3. **Generates a signal** if price is currently retesting a zone that aligns with a support/resistance level and is *not* sitting on the wrong side of a liquidity pool (which would invite a stop hunt).
4. **Places an ATR-adaptive stop** beyond the zone edge with a buffer, clamped between 1× and 3× ATR(14).
5. **Sets take-profit at exactly 3× the stop distance** — the 1:3 RR is enforced in code, always.
6. **Sizes the position** using a dynamic risk model: 5% target risk, 10% hard ceiling, respecting the broker minimum lot.
7. **Sends a Telegram message** with entry, stop, target, pip risk, ATR context, lot size, and dollar risk.

## What It Does NOT Do

Be clear-eyed about this list before deploying:

- ❌ **Does not execute trades.** It is a notifier. You place orders manually.
- ❌ **Does not guarantee profit.** The strategy has an untested edge. See [The Risk Math](#the-risk-math-read-this).
- ❌ **Does not backtest.** There is no historical performance data included. You must build your own.
- ❌ **Does not manage open positions.** No trailing stops, no partial exits, no breakeven moves.
- ❌ **Does not check news events.** Major releases can gap through your stop.
- ❌ **Does not model slippage or spread.** Telegram alerts show raw price; your broker fills will differ.
- ❌ **Does not validate zone freshness.** A zone already tapped twice is treated the same as a fresh one.
- ❌ **Does not align with higher-timeframe trend.** Counter-trend entries are possible.

## Architecture

```
┌──────────────────┐
│  yfinance (1d)   │  Fetch 6 months OHLCV per pair
└────────┬─────────┘
         │
         ▼
┌──────────────────────────────────────────┐
│  Market structure analysis               │
│  • Supply/demand zones                   │
│  • Liquidity pools (equal highs/lows)    │
│  • Support/resistance (swing touches)    │
│  • ATR(14)                               │
└────────┬─────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────┐
│  Signal generation                       │
│  • Zone retest + S/R alignment           │
│  • Liquidity trap filter                 │
│  • ATR-based stop (1×–3× ATR clamp)      │
│  • Take-profit = 3× stop distance        │
└────────┬─────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────┐
│  Position sizing                         │
│  • 5% target risk, 10% ceiling           │
│  • Enforces broker minimum lot           │
│  • Skips trade if min lot exceeds ceiling│
└────────┬─────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────┐
│  Telegram notification                   │
└──────────────────────────────────────────┘
```

## Requirements

- Python 3.9+
- A Telegram bot (see [Telegram Setup](#telegram-setup))
- A free [PythonAnywhere](https://www.pythonanywhere.com/) account (or any always-on host)
- Optional: a Weltrade Micro (cent) demo account for live comparison
- `make` (optional but recommended — see [Makefile Reference](#makefile-reference))

### Python packages

```
yfinance>=0.2.40
pandas>=2.0.0
numpy>=1.24.0
python-telegram-bot>=20.0
python-dotenv>=1.0.0
```

## Quick Start

For the impatient — five commands from zero to a test alert:

```bash
make install           # create venv + install deps
make env               # create .env from template
# ... edit .env with your TELEGRAM_TOKEN and TELEGRAM_CHAT_ID ...
make check             # sanity-check syntax and .env
make telegram-test     # send a test message to confirm wiring
make run               # run the bot once
```

If `make` isn't available, use `pip install -r requirements.txt` and `python3 forex_bot.py` manually. Everything the Makefile does can be done by hand — it just packages the commands for you.

## Installation

### Local (with Makefile — recommended)

```bash
git clone <your-repo-url> forex-signal-bot
cd forex-signal-bot
make install
make env
```

### Local (without Makefile)

```bash
git clone <your-repo-url> forex-signal-bot
cd forex-signal-bot
python3 -m venv venv
source venv/bin/activate         # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then edit .env
```

### On PythonAnywhere

```bash
pip3 install --user yfinance pandas numpy python-telegram-bot python-dotenv
```

Then upload `forex_bot.py` via the **Files** tab, or use `make pa-upload PA_USER=yourusername` from a local machine with SSH access.

## Environment Variables

The bot reads credentials from a `.env` file in the script's directory (via `python-dotenv`), falling back to the process environment if the file is absent.

### Create your `.env`

```bash
make env
```

Or copy manually:

```bash
cp .env.example .env
```

Then edit `.env`:

```
TELEGRAM_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
TELEGRAM_CHAT_ID=123456789
```

### Precedence

1. Process environment variables (e.g., PythonAnywhere task UI) — **win**
2. `.env` file — used when no environment variable is set
3. Hard failure — the script raises `RuntimeError` if either credential is missing

### Never commit `.env`

The repo's `.gitignore` excludes `.env`. Only `.env.example` (with placeholder values) is tracked. If you accidentally commit `.env`, rotate your bot token in `@BotFather` immediately — it is compromised.

## Configuration

All tunables live at the top of `forex_bot.py`. The values below are defaults tuned for a **300-cent ($3 real) Weltrade Micro account**.

```python
# Account & sizing
ACCOUNT_BALANCE    = 300.0    # cents (300¢ = $3 real)
RISK_PERCENT       = 5.0      # target risk per trade
MAX_RISK_PERCENT   = 10.0     # hard ceiling — skip if minimum lot exceeds this
BROKER_MIN_LOT     = 0.01     # Weltrade micro minimum
PIP_VALUE_STANDARD = 0.10     # cents per pip per 1.0 cent-lot — MUST be calibrated
SPREAD_BUFFER_PIPS = 2.0      # extra pips added to stop beyond zone edge

# ATR stop tuning
ATR_PERIOD        = 14
ATR_BUFFER_MULT   = 0.3       # ATR buffer beyond zone edge (0.3 = 30% of ATR)
MIN_STOP_ATR_MULT = 1.0       # minimum stop distance from entry
MAX_STOP_ATR_MULT = 3.0       # maximum stop distance — wider than this = skip

# Pairs
PAIRS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X",
         "AUDUSD=X", "USDCAD=X", "NZDUSD=X"]
```

### Tuning guide

| Symptom | Adjustment |
|---|---|
| Stops keep getting wicked out | Raise `ATR_BUFFER_MULT` to `0.5` |
| Too many trades skipped by max cap | Raise `MAX_STOP_ATR_MULT` to `3.5` |
| Every trade skipped by min-lot ceiling | `PIP_VALUE_STANDARD` is probably wrong — see calibration |
| Lot sizes look 10× too large | `PIP_VALUE_STANDARD` is 10× too small |
| Using a standard (non-cent) account | Multiply `ACCOUNT_BALANCE` by 100, or recalibrate `PIP_VALUE_STANDARD` to `10.0` |

## Telegram Setup

### 1. Create the bot

1. Open Telegram, search for **`@BotFather`**, start a chat.
2. Send `/newbot`.
3. Give it a **display name** (anything) and a **username** (must end in `bot`, e.g. `my_forex_signal_bot`).
4. BotFather replies with a **token** like `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`. Save this — it's the password.

### 2. Get your chat ID

1. Search for your bot's username, open the chat, send `/start` or any message.
2. In a browser, visit:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
3. Find `"chat": { "id": 123456789, ... }` — that number is your chat ID.

Alternative: message **`@userinfobot`** — it replies with your user ID immediately.

### 3. Store credentials

Put them in `.env` (see [Environment Variables](#environment-variables)) or set them in the PythonAnywhere task editor's environment fields.

## Running the Bot

### Manual test

```bash
make run
```

Or directly:

```bash
python3 forex_bot.py
```

Example output:

```
[START] 2026-10-23T22:00:01 UTC | Balance=300.0¢ | TargetRisk=5.0% | MaxRisk=10.0% | PipValue=0.1
[NONE] EURUSD=X: no signal
[SIGNAL] GBPUSD=X SHORT | lots=1.5 | risk=15.0¢ (5.0%) | source=target
[NONE] USDJPY=X: no signal
[SKIP] USDCHF=X LONG: min_lot_exceeds_max_risk (stop=380.0 pips, min-lot risk=38.0¢ > 10.0% ceiling)
[NONE] AUDUSD=X: no signal
[NONE] USDCAD=X: no signal
[NONE] NZDUSD=X: no signal
[DONE] 1 signal(s) sent.
```

Log prefixes:

| Prefix | Meaning |
|---|---|
| `[START]` | Run began — config summary printed |
| `[NONE]` | Pair scanned, no qualifying setup |
| `[SIGNAL]` | Signal sent to Telegram |
| `[SKIP]` | Setup found but position sizing rejected it |
| `[ERROR]` | Exception during processing (pair skipped) |
| `[DONE]` | Run complete with signal count |

### Telegram message format

```
🚨 GBPUSD SHORT
━━━━━━━━━━━━━━
Entry: 1.27450
Stop Loss: 1.27810
Take Profit: 1.26370
Risk: 36.0 pips (1:3 RR)
ATR(14): 28.7 pips | Stop = 1.25× ATR
Lot Size: 1.5 (target)
Risk ≈ 15.0¢ (5.0% of account)
Max win ≈ 45.0¢
Zone: Supply (1.27200 – 1.27400)
⏰ 2026-10-23 22:00 UTC
```

## Makefile Reference

Run `make help` to list every target. Full reference:

### Setup

| Target | Description |
|---|---|
| `make install` | Create venv and install dependencies |
| `make install-dev` | Above + pytest, ruff, black |
| `make env` | Copy `.env.example` → `.env` (does not overwrite) |
| `make requirements` | Create default `requirements.txt` if missing |

### Running

| Target | Description |
|---|---|
| `make run` | Run the bot once |
| `make run-verbose` | Run with `LOG_LEVEL=DEBUG` |
| `make run-bg` | Run in background, output to `logs/` |

### Quality

| Target | Description |
|---|---|
| `make check` | Syntax + `.env` presence + placeholder detection |
| `make test` | Run `tests/` suite (if present) |
| `make lint` | Lint with ruff |
| `make format` | Format with black |
| `make deploy-check` | Pre-flight for deployment (token leaks, `.env` tracked by git) |

### Telegram

| Target | Description |
|---|---|
| `make telegram-test` | Send a test message to your chat |

### Logs

| Target | Description |
|---|---|
| `make log` | Cat the most recent log file |
| `make tail-log` | `tail -f` the most recent log file |

### Backup

| Target | Description |
|---|---|
| `make backup` | Tar `forex_bot.py`, `.env`, `requirements.txt`, `Makefile` into `backups/` |
| `make backup-list` | List available backups |

### PythonAnywhere

| Target | Description |
|---|---|
| `make pa-upload PA_USER=myuser` | `scp` script + requirements to your PythonAnywhere home |
| `make pa-schedule PA_USER=myuser` | Print scheduling instructions |
| `make pa-logs PA_USER=myuser` | Tail the task log over SSH |

### Cleanup

| Target | Description |
|---|---|
| `make clean` | Remove `__pycache__`, `.pyc`, caches |
| `make clean-all` | Above + remove `venv/` and `logs/` (prompts for confirmation) |

## Deployment on PythonAnywhere (Free Tier)

Free PythonAnywhere accounts have limited background-process capability. Two deployment paths:

### Path A — Free account created **before Jan 15, 2026** (has scheduled tasks)

1. Go to the **Tasks** tab.
2. Create a daily task at `22:00 UTC` (after NY close) with:
   ```
   python3 /home/<yourusername>/forex_bot.py
   ```
3. Set `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` as environment variables in the task editor.

Free accounts get **one daily task** and **~100 CPU-seconds/day**. This bot uses about 3–5 seconds per run, so it fits comfortably.

### Path B — Free account created **after Jan 15, 2026** (no scheduled tasks)

You must use a web app + external cron:

1. Create a **Flask web app** on PythonAnywhere.
2. Add a `/run` route that calls `main()` from your bot module:
   ```python
   import os
   from flask import Flask, request, abort
   from forex_bot import main

   app = Flask(__name__)

   @app.route("/run")
   def run():
       if request.args.get("k") != os.getenv("RUN_SECRET"):
           abort(403)
       main()
       return "ok", 200
   ```
3. Reload the web app.
4. Set `RUN_SECRET` in the web app's environment variables.
5. Sign up at [cron-job.org](https://cron-job.org) (free) and create a job that GETs:
   ```
   https://<yourusername>.pythonanywhere.com/run?k=<RUN_SECRET>
   ```
   once daily at 22:00 UTC.

> ⚠️ **Never expose `/run` without a secret.** An open endpoint lets anyone trigger your bot and drain your CPU quota.

## Calibrating `PIP_VALUE_STANDARD`

This is the **single most important constant** and the most common source of error. `PIP_VALUE_STANDARD` is the pip value in **account currency (cents)** for a **1.0-lot position** on your broker.

### How to measure it (recommended)

1. Log in to your Weltrade **cent demo** account.
2. Open a **0.01-lot** EUR/USD trade (or any XXX/USD pair).
3. Note your balance.
4. Wait for price to move **exactly 10 pips** in your favor.
5. Note the new balance. The difference ÷ 10 is the pip value in cents for 0.01 lot.
6. Multiply by 100 → that's `PIP_VALUE_STANDARD`.

Example: 10 pips on 0.01 lot gained **1.0 cent** → pip value = 0.1¢ per 0.01 lot → **`PIP_VALUE_STANDARD = 0.10`**.

### Sanity check after first run

Look at the first `[SIGNAL]` line:

- **`lots=15.0` or higher on a 300¢ account** → `PIP_VALUE_STANDARD` is **10× too small**
- **Every signal is `[SKIP] min_lot_exceeds_max_risk`** → `PIP_VALUE_STANDARD` is **10× too large**
- **`lots=1.5`, `risk=15.0¢`** → correct for a 300¢ account at 5% target

## Understanding the Output

### Signal fields

| Field | Meaning |
|---|---|
| `Entry` | Last daily close (the price you'd enter at) |
| `Stop Loss` | Zone edge ± ATR buffer |
| `Take Profit` | Entry ± 3 × stop distance |
| `Risk` | Stop distance in pips |
| `ATR(14)` | Average true range — context for stop tightness |
| `Stop = N× ATR` | How many ATRs wide your stop is (should be 1.0–3.0) |
| `Lot Size` | Position size in cent-lots |
| `(target)` / `(min_lot_floor)` | Sizing source — target risk was used, or floored to min lot |
| `Risk ≈` | Dollar risk in cents and % of account |
| `Max win ≈` | 3× the risk, in cents |

### When to trust a signal

Higher confidence when:

- `Stop = 1.0–2.0× ATR` (normal volatility, not a zone blowout)
- Zone is fresh (not visible in a recent trend as an already-rejected area)
- Stop distance is < 200 pips (within your sizing window)
- Risk % is close to your 5% target (not floored to min lot)

Lower confidence when:

- `Stop = 2.8–3.0× ATR` (zone very wide — could be a broken zone)
- `(min_lot_floor)` — sizing was floored, actual risk may be higher than 5%
- Signal opposes the weekly trend (you must check this yourself)

## The Risk Math (Read This)

A few hard numbers to keep you honest:

### What 5% risk on 300¢ means

| Outcome | Amount |
|---|---|
| Loss | −15¢ (−5% account) |
| Win (1:3 RR) | +45¢ (+15% account) |

### Losing streaks are guaranteed

At a 35% win rate (a reasonable assumption for this strategy), the expected longest losing streak over **200 trades** is roughly 10–12. Here's the drawdown at 5% risk:

| Streak | Drawdown | Recovery needed |
|---|---|---|
| 3 | −14% | +17% |
| 5 | −23% | +29% |
| 8 | −34% | +51% |
| 12 | −46% | +85% |

At **10% risk** (the ceiling), a 12-loss streak wipes **72%** of the account and requires a **+257% gain** to recover. This is why 5%/10%-cap is the highest sane setting for an untested strategy.

### Kelly criterion

For 1:3 RR with win rate `p`: `f* = (4p − 1) / 3`

| Win rate | Kelly-optimal risk |
|---|---|
| 30% | 6.7% |
| 35% | 13.3% |
| 40% | 20.0% |

So 5% is **under half-Kelly** at 35% win rate — appropriate for an untested edge. But if the true win rate is 28%, even 5% is over-Kelly and the account dies. **You do not yet know the win rate.** This is why paper trading first is non-negotiable.

## Limitations

- **Data source**: Yahoo Finance can have gaps, delayed bars, and occasional bad ticks. Swap in OANDA v20 for production use.
- **Zone detection is heuristic**: it is not real order-block / FVG detection. Expect false positives.
- **Liquidity detection is naive**: scans for coincidental equal highs/lows — many will be random.
- **No trend filter**: counter-trend entries are possible. You must vet each signal manually.
- **No news awareness**: NFP, FOMC, and central-bank days can gap through stops.
- **No slippage / spread model**: Telegram prices are mid-market; fills will differ.
- **No position management**: no trailing, no partial exits, no breakeven.
- **Single daily run** on free PythonAnywhere: a signal generated at 22:00 UTC is based on the prior day's close — you're always one bar behind intraday movement.
- **No backtest included**: you must build one to measure the edge.

## Roadmap

If you want to make this materially better, in order of impact:

1. **Backtest on 2–3 years of data** — measure actual win rate and expectancy before deploying real capital.
2. **Add a higher-timeframe trend filter** — only take longs in weekly uptrends, shorts in downtrends.
3. **Replace zone detection** with fair-value-gap / order-block detection.
4. **Replace liquidity detection** with sweep-and-reclaim logic instead of equal-level coincidence.
5. **Add a news blackout window** — skip signals within 30 minutes of high-impact events (ForexFactory API).
6. **Switch data source to OANDA v20** — cleaner data, 4-hour bars, no Yahoo lag.
7. **Add a cooldown** — don't re-signal the same pair within N days.
8. **Log every signal to CSV** — build the dataset for step 1.

## Project Layout

```
forex-signal-bot/
├── forex_bot.py           # Main bot script
├── requirements.txt       # Python dependencies
├── Makefile               # Task runner (install, run, deploy)
├── .env.example           # Template — copy to .env and fill in
├── .env                   # Your real credentials (NOT committed)
├── .gitignore             # Excludes .env, venv, logs, backups
├── README.md              # This file
├── logs/                  # Runtime logs from `make run-bg` (gitignored)
└── backups/               # Backups from `make backup` (gitignored)
```

## Disclaimer

**This software is provided for educational and research purposes only. It is not financial advice, and it is not a recommendation to buy or sell any financial instrument.**

- Trading foreign exchange on margin carries a high level of risk and may not be suitable for all investors.
- Past performance — of this bot, of any backtest you build, or of the strategy it approximates — is not indicative of future results.
- The strategy implemented here has **not been validated**. Its edge is unknown.
- **Small accounts are especially vulnerable.** A $3 account cannot absorb normal losing streaks at any risk percentage that allows trades to execute. The bot is designed to refuse trades rather than over-risk — do not override this.
- The authors of this code accept **no responsibility** for any financial loss resulting from its use.
- Never risk more than you can afford to lose entirely.

**By running this bot, you accept full responsibility for every trade you place based on its output.**
```

---

The main additions versus the previous version:

- **Quick Start** at the top — five-command path from zero to a test alert
- **Environment Variables** section — explains `.env`, precedence, and the never-commit rule
- **Makefile Reference** — full table of every target grouped by category
- **Project Layout** — visual tree of what lives where
- **`.env` loader note** — clarifies that `.env` is loaded via `python-dotenv` from the script's own directory
- **Updated install instructions** — split into Makefile and non-Makefile paths
- **`RUN_SECRET` guard** on the Flask `/run` endpoint — closes the open-endpoint hole mentioned in the earlier draft
- **Updated requirements** — adds `python-dotenv>=1.0.0`

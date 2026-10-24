I've updated the README to include the live Telegram bot link (`t.me/masheroooo_forex_signal_bot`) in the header, the quick-start section, and the deployment verification steps. The rest of the document remains identical, covering the full stack (Neon, Render, cron-job.org).

```markdown
# Forex Signal Bot

**Try the live bot:** [@masheroooo_forex_signal_bot](https://t.me/masheroooo_forex_signal_bot)

A Python-based forex signal generator that scans 11 major currency pairs daily, detects supply/demand zones, liquidity pools, and support/resistance levels, and delivers ATR-adaptive 1:3 risk-reward signals to Telegram. Built for small cent accounts, it tracks every signal's outcome in a persistent cloud database and exposes a full 13-command Telegram interface for live queries.

**This is a signal notifier, not an auto-trader.** It does not connect to your broker or place trades. You execute manually and manage your own risk.

---

## Table of Contents

- [What It Does](#what-it-does)
- [Technology Stack](#technology-stack)
- [Architecture](#architecture)
- [How the Signal Pipeline Works](#how-the-signal-pipeline-works)
- [Telegram Commands](#telegram-commands)
- [The Risk Model](#the-risk-model)
- [Deployment](#deployment)
- [Environment Variables](#environment-variables)
- [Database Schema](#database-schema)
- [Tuning Guide](#tuning-guide)
- [Honest Limitations](#honest-limitations)
- [Roadmap](#roadmap)
- [Disclaimer](#disclaimer)

---

## What It Does

The bot combines four pieces of market analysis that supply/demand traders use manually, and turns them into a systematic signal pipeline:

1. **Supply/demand zones** — Every recent impulse candle (body ≥ 1.2× the 20-bar average) creates a zone at the prior opposite candle's high/low. Zones survive retests but die when price closes through them.
2. **Liquidity pools** — Swing highs/lows within 3 pips of each other, at least 10 bars apart, over the last 90 bars. If a zone has an equal level sitting just beyond it, the signal is filtered out (stop-hunt risk).
3. **Support/resistance confluence** — Swing levels touched at least twice within a 10-pip band. A zone only fires if there's a matching S/R level within 10 pips.
4. **ATR-adaptive stops** — Stop distance = zone width + 0.3× ATR(14), clamped between 1.0× and 3.0× ATR, plus a 2-pip spread buffer.

Signals are sent to Telegram with entry, stop, target, lot size, dollar risk, ATR context, and the zone boundaries. Outcomes are tracked automatically: every open signal is walked forward through new candles and marked `win` or `loss` when price touches the target or stop.

---

## Technology Stack

| Layer | Technology | Purpose |
|---|---|---|
| **Language** | Python 3.9+ | Core bot logic |
| **Market Data** | [yfinance](https://github.com/ranaroussi/yfinance) | Fetches daily OHLCV from Yahoo Finance |
| **Data Processing** | pandas, numpy | Time-series analysis, ATR, swing detection |
| **Database** | [Neon](https://neon.tech) (Serverless Postgres) | Persistent storage for signals, runs, diagnostics |
| **DB Driver** | psycopg2-binary | Postgres adapter for Python |
| **Messaging** | Telegram Bot API | Signal alerts + 13-command interface |
| **Telegram Lib** | python-telegram-bot v20+ | Async command handlers, webhook mode |
| **Web Framework** | Flask | Webhook receiver + scan trigger endpoint |
| **Hosting** | [Render](https://render.com) (Free Web Service) | Runs the web listener + scan trigger |
| **Scheduler** | [cron-job.org](https://cron-job.org) (Free) | Fires the daily scan via HTTP GET |
| **Config** | python-dotenv | Environment variable loading from `.env` |

### Why This Stack

- **Neon over SQLite** — Render's free tier has no persistent disk. SQLite would reset on every redeploy. Neon's serverless Postgres persists independently of the hosting environment, with 1 GB storage and 100 CU-hours/month free.
- **Render over Railway/PythonAnywhere** — Railway's free tier is now a one-time $5 credit. PythonAnywhere's free tier doesn't support always-on processes or scheduled tasks for newer accounts. Render's free Web Service tier works for the command listener, though it spins down after 15 minutes idle.
- **cron-job.org over Render Cron** — Render's Cron Job feature is a paid add-on. cron-job.org is free (100 calls/day) and hits the `/run-scan` HTTP endpoint daily.
- **Flask + Webhook over Polling** — Telegram's webhook mode avoids running a continuous polling loop, which is important on free tiers that spin down idle services. Telegram pushes each update to your Flask endpoint when a command arrives.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  You (Telegram)                                             │
│  └─ Types /status, /scan, /help...                          │
└───────────────────────────────┬─────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────┐
│  Telegram Bot API                                           │
│  └─ POSTs update to https://<bot>.onrender.com/webhook/...  │
└───────────────────────────────┬─────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────┐
│  Render Web Service (web_listener.py)                       │
│  ├─ Flask /webhook   → dispatches to telegram_commands.py   │
│  ├─ Flask /run-scan  → triggers main.run_scan()             │
│  └─ Flask /health    → health check                         │
└───────────────────────────────┬─────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────┐
│  Neon Postgres (persistent)                                 │
│  ├─ signals            → every trade signal + outcome       │
│  ├─ runs               → execution log                      │
│  └─ pair_diagnostics   → per-pair filter reasons            │
└─────────────────────────────────────────────────────────────┘
                                ▲
                                │
┌───────────────────────────────┴─────────────────────────────┐
│  cron-job.org (daily 22:00 UTC)                             │
│  └─ GET https://<bot>.onrender.com/run-scan?k=SECRET        │
│     └─ Triggers the daily scan (fetches data, sends alerts) │
└─────────────────────────────────────────────────────────────┘
```

### Two Independent Modes

**Mode 1 — Daily Scan (triggered by cron-job.org):**
- Runs `main.run_scan()` once per day after the NY close
- Checks open signals → marks wins/losses
- Scans 11 pairs → generates new signals
- Sends Telegram alerts for new signals
- Writes to Neon
- Returns JSON to cron-job.org, exits

**Mode 2 — Command Listener (always-on via Render):**
- Listens for Telegram webhook updates
- Answers `/status`, `/signals`, `/performance`, etc.
- Runs live `/scan` on demand (preview only, does not save)
- Reads from Neon
- Responds within 1–3 seconds (or ~30s after wakeup)

The two modes share only the Neon database. Neither depends on the other running.

---

## How the Signal Pipeline Works

For each of the 11 pairs, the scan runs these steps:

### 1. Fetch data
Pulls 6 months of daily OHLCV from Yahoo Finance. The currently-forming daily candle is dropped if before 22:00 UTC, so every signal is based on completed bars.

### 2. Detect supply/demand zones
Scans the last 50 bars for impulse candles (body ≥ 1.2× the 20-bar average). Each impulse creates a zone at the prior opposite candle's high/low. A zone dies when any later close goes through its far edge; it survives mere touches.

### 3. Detect liquidity pools
Finds swing highs/lows (5-bar pivots, at least 10 bars apart, last 90 bars). Any pair within 3 pips is an equal-highs/equal-lows pool.

### 4. Detect support/resistance
Swing highs/lows (10-bar pivots) with at least 2 touches within 10 pips become S/R levels.

### 5. Generate signal
For a **long** (demand retest):
- Today's low must touch or come within 5 pips of the zone, without closing below it.
- A support level must be within 10 pips of the zone bottom.
- No equal-lows pool just below the zone.
- Stop = zone distance + 0.3× ATR, clamped to 1.0–3.0× ATR, plus 2-pip spread.
- Target = entry + 3 × stop distance (strict 1:3 RR).

For a **short** (supply retest): mirror logic.

If any check fails, the zone is rejected and the reason recorded (`long_no_retest`, `short_no_resistance`, etc.).

### 6. Size position
Target risk = 5% of balance. If the computed lot size falls below the broker minimum (0.01 lots), the bot uses the minimum only if that doesn't exceed 10% of balance. Otherwise the signal is **skipped** — the account is too small to safely take this trade.

### 7. Deduplicate
No new signal while one is open for the same (symbol, direction). Unique index prevents duplicate signals per candle.

### 8. Save, notify, log
Writes to Neon → sends Telegram alert → records diagnostics → exits.

---

## Telegram Commands

| Command | Description |
|---|---|
| `/start` | Welcome message and quick-start tips |
| `/help` | Full command reference |
| `/status` | Bot status, last run time, total and open signals |
| `/signals` | Last 5 signals with entry/SL/TP/lot/outcome |
| `/latest` | Most recent signal with full detail |
| `/pairs` | List all 11 monitored pairs |
| `/balance` | Configured balance, target risk, ceiling |
| `/risk` | Risk %, ATR settings, spread buffer, broker min lot |
| `/scan` | Live scan — returns current setups without saving |
| `/diagnostics` | Zone/S/R/pool counts and rejection reasons for last run |
| `/performance` | Win rate, total PnL, avg PnL, per-pair breakdown |
| `/settings` | Full config dump — all tuning constants |
| `/about` | Bot description and disclaimers |

**Try them live:** [@masheroooo_forex_signal_bot](https://t.me/masheroooo_forex_signal_bot)

Every command is Markdown-safe: dynamic values are sanitized before display, and if Telegram rejects the formatting, the message falls back to plain text. No command can crash on a parse error.

---

## The Risk Model

Risk is enforced in three places:

| Rule | Value | What It Prevents |
|---|---|---|
| **Target risk per trade** | 5% of balance | Over-sizing |
| **Hard ceiling** | 10% of balance | Catastrophic single-trade loss |
| **Broker minimum enforcement** | Skip if min lot exceeds ceiling | Over-risking on wide stops |
| **Duplicate prevention** | One open signal per (symbol, direction) | Accumulating correlated exposure |

On a $3 account with a 100-pip stop:
```
Target risk: 5% × 300¢ = 15¢
Required lots: 15¢ / (100 pips × $10/pip) = 0.015 lots → below 0.01 minimum
Min-lot risk: 0.01 × 100 × $10 = 10¢ → 3.3% of balance
Ceiling check: 10¢ ≤ 30¢ (10% of 300¢) → trade allowed, floored to min lot
```

On a $3 account with a 300-pip stop:
```
Min-lot risk: 0.01 × 300 × $10 = 30¢ → 10% of balance
Ceiling check: 30¢ > 30¢? No, exactly equal → trade allowed at ceiling
```

On a $3 account with a 400-pip stop:
```
Min-lot risk: 0.01 × 400 × $10 = 40¢ → 13.3% of balance
Ceiling check: 40¢ > 30¢ → **SKIP**
```

The bot will refuse trades it cannot safely execute. This is not a bug — it is the design.

---

## Deployment

### Prerequisites

1. **Telegram Bot** — created via [@BotFather](https://t.me/BotFather). Save your bot token and chat ID.
2. **Neon Account** — [neon.tech](https://neon.tech), free tier. Create a project and copy the connection string.
3. **Render Account** — [render.com](https://render.com), free tier. Connect your GitHub repo.
4. **cron-job.org Account** — [cron-job.org](https://cron-job.org), free tier.

### Step 1 — Set up Neon

1. Sign up at [neon.tech](https://neon.tech).
2. Create a project.
3. In the Neon Console, click **Connect** and copy the connection string. It looks like:
   ```
   postgresql://user:pass@ep-cool-name-123456.us-east-2.aws.neon.tech/neondb?sslmode=require
   ```
4. Save this — it goes into Render's environment variables.

### Step 2 — Deploy to Render

1. Push your code to GitHub.
2. In Render, click **New → Web Service**.
3. Connect your GitHub repo.
4. Set:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python web_listener.py`
   - **Plan**: Free
5. Add these Environment Variables in the Render dashboard:
   ```
   TELEGRAM_TOKEN=1234567890:ABCdef...
   TELEGRAM_CHAT_ID=987654321
   DATABASE_URL=postgresql://user:pass@ep-...neon.tech/neondb?sslmode=require
   WEBHOOK_URL=https://your-bot.onrender.com
   SCAN_SECRET=xK9mP2nQ8vR4sT7wY1zA5bC3dE6fG0hJ
   ```
6. Click **Create Web Service**. Render will build and deploy.

The startup log should show:
```
[BOOT] Telegram application initialized in background loop.
[WEBHOOK] Registered: https://your-bot.onrender.com/webhook/<TOKEN>
[BOOT] Flask listening on 0.0.0.0:10000
```

### Step 3 — Create the Database Tables

Hit the scan endpoint once to run `db_init()`:

```
https://your-bot.onrender.com/run-scan?k=YOUR_SCAN_SECRET
```

Expected response:
```json
{"status":"ok","result":{"run_id":1,"pairs_scanned":11,"signals_sent":0,...}}
```

The `signals`, `runs`, and `pair_diagnostics` tables are now created in Neon.

### Step 4 — Set up cron-job.org

1. Sign up at [cron-job.org](https://cron-job.org).
2. Click **Create cronjob**.
3. **Title**: Forex Bot Daily Scan
4. **URL**:
   ```
   https://your-bot.onrender.com/run-scan?k=YOUR_SCAN_SECRET
   ```
5. **Schedule**: Every day at `22:00` UTC (or whenever the NY close is).
6. Save. Enable email notifications for failures.

### Step 5 — Verify

**Test the health endpoint:**
```bash
curl https://your-bot.onrender.com/health
# Expected: ok
```

**Test in Telegram:** [@masheroooo_forex_signal_bot](https://t.me/masheroooo_forex_signal_bot)
- Send `/start` → welcome message
- Send `/help` → full command list
- Send `/status` → bot status
- Send `/scan` → live scan results (may take 20–40s)

**Check Neon:**
```sql
SELECT * FROM signals ORDER BY id DESC LIMIT 5;
SELECT * FROM runs ORDER BY id DESC LIMIT 5;
```

---

## Environment Variables

Set these in **Render → Environment**:

| Variable | Example | Required |
|---|---|---|
| `TELEGRAM_TOKEN` | `1234567890:ABCdef...` | **Yes** |
| `TELEGRAM_CHAT_ID` | `987654321` | **Yes** |
| `DATABASE_URL` | `postgresql://user:pass@ep-...neon.tech/neondb?sslmode=require` | **Yes** |
| `WEBHOOK_URL` | `https://your-bot.onrender.com` | **Yes** (for commands) |
| `SCAN_SECRET` | `xK9mP2nQ8vR4sT7wY1zA5bC3dE6fG0hJ` | **Yes** (for production) |
| `PORT` | Auto-set by Render | No |

Generate `SCAN_SECRET` with:
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

---

## Database Schema

Three tables in Neon Postgres:

### `signals`
One row per signal. Every field needed to evaluate and replay the trade.

| Column | Type | Description |
|---|---|---|
| `id` | SERIAL | Primary key |
| `created_at` | TEXT | ISO8601 UTC |
| `signal_date` | TEXT | Candle date (YYYY-MM-DD) |
| `symbol` | TEXT | e.g., `GBPUSD` |
| `direction` | TEXT | `LONG` or `SHORT` |
| `entry` | DOUBLE PRECISION | Entry price |
| `stop_loss` | DOUBLE PRECISION | Stop price |
| `take_profit` | DOUBLE PRECISION | Target price |
| `risk_pips` | DOUBLE PRECISION | Stop distance in pips |
| `atr_pips` | DOUBLE PRECISION | ATR(14) at signal time |
| `atr_multiple` | DOUBLE PRECISION | Stop / ATR |
| `lot_size` | DOUBLE PRECISION | Position size |
| `risk_cents` | DOUBLE PRECISION | Dollar risk in cents |
| `sizing_source` | TEXT | `target`, `min_lot_floor`, etc. |
| `zone_type` | TEXT | `demand` or `supply` |
| `zone_top` | DOUBLE PRECISION | Zone upper boundary |
| `zone_bottom` | DOUBLE PRECISION | Zone lower boundary |
| `outcome` | TEXT | `open`, `win`, `loss`, `breakeven` |
| `exit_price` | DOUBLE PRECISION | Filled when outcome closes |
| `closed_at` | TEXT | ISO8601 UTC |
| `pnl_cents` | DOUBLE PRECISION | +45 for win, -15 for loss |

### `runs`
One row per invocation.

| Column | Type |
|---|---|
| `id` | SERIAL |
| `started_at` | TEXT |
| `finished_at` | TEXT |
| `pairs_scanned` | INTEGER |
| `signals_sent` | INTEGER |
| `signals_skipped` | INTEGER |
| `errors` | INTEGER |

### `pair_diagnostics`
One row per pair per run.

| Column | Type |
|---|---|
| `id` | SERIAL |
| `run_id` | INTEGER (FK → runs) |
| `symbol` | TEXT |
| `zones` | INTEGER |
| `sr_levels` | INTEGER |
| `pools` | INTEGER |
| `reasons` | TEXT (comma-separated) |

---

## Tuning Guide

All tunables live at the top of `main.py`:

| Constant | Default | Effect |
|---|---|---|
| `ZONE_BODY_MULT` | `1.2` | Lower = more zones |
| `RETEST_BUFFER_PIPS` | `5.0` | Higher = looser retest |
| `SR_TOLERANCE_PIPS` | `10.0` | Higher = looser S/R confluence |
| `ZONE_LOOKBACK_BARS` | `50` | Higher = more candidate zones |
| `FRESH_ZONES_ONLY` | `False` | `True` discards touched zones |
| `MIN_STOP_ATR_MULT` | `1.0` | Minimum stop size |
| `MAX_STOP_ATR_MULT` | `3.0` | Maximum stop size; wider = skip |
| `ATR_BUFFER_MULT` | `0.3` | Zone-edge buffer as fraction of ATR |

**Change one knob per week, run 7 times, then query the DB:**

```sql
SELECT reasons, COUNT(*) AS n
FROM pair_diagnostics
WHERE run_id >= 4
GROUP BY reasons
ORDER BY n DESC
LIMIT 10;
```

Whichever reason dominates tells you which knob to turn next:

- `no_retest` dominates → raise `RETEST_BUFFER_PIPS`
- `no_support`/`no_resistance` dominates → raise `SR_TOLERANCE_PIPS`
- `stop_too_wide` dominates → raise `MAX_STOP_ATR_MULT`
- `no_zones` dominates → raise `ZONE_LOOKBACK_BARS`

---

## Honest Limitations

The bot is well-engineered. The strategy is unproven.

- **No backtest has been run.** The strategy's edge over 100+ trades is unknown. A reasonable estimate is a 25–30% probability of a positive edge.
- **Signals are rare.** With strict filters, most runs produce zero signals. This is by design, but it means measurement takes weeks.
- **No trend filter.** The bot takes any zone retest in either direction, which increases counter-trend risk.
- **No rejection confirmation.** The bot enters on zone touch, not on a rejection candle — a filter that backtested strategies add specifically to raise win rate.
- **Daily timeframe introduces a one-bar lag.** By the time a signal fires, price may have moved past the zone.
- **Spread on a cent account eats 10%+ of the risk budget.** At 15¢ risk and a 2-pip spread, entry cost alone is a structural drag.
- **The bot does not place trades.** It sends signals; you execute manually and manage your own risk.
- **Free tier spin-down.** Render spins down idle services after 15 minutes. Commands may take ~30 seconds to respond after a period of inactivity. The daily scan still runs.
- **Neon cold starts.** Neon suspends compute after 5 minutes idle. The first DB query after suspension takes ~1–2 seconds. `db_connect()` retries handle this transparently.

---

## Roadmap

In order of impact:

1. **Backtest on 2–3 years of data** — measure actual win rate and expectancy before deploying real capital.
2. **Add a 200 EMA trend filter** — only take longs in uptrends, shorts in downtrends.
3. **Replace zone detection** with fair-value-gap / order-block detection.
4. **Add rejection-candle confirmation** — require the retest bar to close bullish (for demand) or bearish (for supply).
5. **Add a news blackout window** — skip signals within 30 minutes of high-impact events.
6. **Switch data source to OANDA v20** — cleaner data, 4-hour bars, no Yahoo lag.
7. **Add a cooldown** — don't re-signal the same pair within N days.
8. **Automated outcome replay** — walk every open signal forward daily, not just on the daily scan.

---

## Project Layout

```
forex-signal-bot/
├── main.py                  # Signal generation, DB, scan, outcome tracking
├── telegram_commands.py     # 13 command handlers (async-safe Postgres)
├── web_listener.py          # Flask webhook + scan trigger for Render
├── requirements.txt         # Python dependencies
├── .env.example             # Template — copy to .env
├── .env                     # Your real credentials (gitignored)
├── .gitignore               # Excludes .env, venv, __pycache__
├── README.md                # This file
└── signals.db               # Legacy SQLite (no longer used in Postgres mode)
```

---

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
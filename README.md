# Gold (XAU/USD) Trend-Following Signal Bot

**Try the live bot:** [@masheroooo_forex_signal_bot](https://t.me/masheroooo_forex_signal_bot)

A disciplined, Long-Only, 1-Hour Gold trading bot. It hunts for supply/demand pullbacks strictly aligned with the macro trend, calculates ATR-adaptive stops, and delivers 1:3 risk-reward signals directly to Telegram. 

**This is a signal notifier, not an auto-trader.** It does not connect to your broker or place trades. You execute manually and manage your own risk.

---

## Table of Contents

- [What It Does](#what-it-does)
- [The Strategy Rules](#the-strategy-rules)
- [Technology Stack](#technology-stack)
- [Architecture](#architecture)
- [Deployment Guide](#deployment-guide)
- [Telegram Commands](#telegram-commands)
- [The "$3 Account" Reality Check](#the-3-account-reality-check)
- [Disclaimer](#disclaimer)

---

## What It Does

The bot scans **XAU/USD (Gold)** every hour. It uses institutional concepts (Smart Money Concepts) to find high-probability entry points. Instead of chasing the market, the bot waits patiently for three conditions to align:

1.  **Macro Trend:** Gold must be in a confirmed uptrend.
2.  **Pullback:** Price must pull back to a fresh institutional Demand Zone.
3.  **Confluence:** The zone must align with historical Support and be free of liquidity traps.

When all conditions are met, the bot sends a Telegram alert with the exact Entry, Stop Loss, Take Profit, and Position Size.

---

## The Strategy Rules

The bot operates on a strict, rules-based checklist:

*   **Asset:** `GC=F` (Gold Futures via Yahoo Finance, mapped to XAUUSD).
*   **Timeframe:** 1-Hour (`1h`). 
*   **Direction:** **LONG ONLY**. The bot never shorts.
*   **The 200 EMA Filter:** The bot will *only* look for trades if the current price is above the 200 EMA **AND** the 200 EMA is sloping upwards. If Gold is ranging or falling, the bot stays silent.
*   **Supply/Demand:** Looks for a fresh Demand Zone (the last bearish candle before a strong bullish impulse).
*   **Liquidity Traps:** Rejects setups if there are "Equal Lows" (retail stop-losses) sitting just below the zone.
*   **ATR Stops:** Stop-loss is placed outside the zone + 0.2x ATR, with a 5-pip spread buffer.
*   **Risk/Reward:** Strictly **1:3**. If the stop is $5.00 away, the target is exactly $15.00 away.
*   **Daily Limit:** The bot will stop scanning for the day after **3 signals** are sent, preventing over-trading.

---

## Technology Stack

| Layer | Technology | Purpose |
|---|---|---|
| **Language** | Python 3.9+ | Core bot logic |
| **Market Data** | `yfinance` | Fetches 1H OHLCV data for Gold |
| **Database** | [Neon](https://neon.tech) (Postgres) | Persistent storage for signals & diagnostics |
| **DB Driver** | `psycopg2-binary` | Postgres adapter |
| **Messaging** | Telegram Bot API | Signal alerts + 13-command interface |
| **Web Framework**| Flask | Webhook receiver & Scan trigger |
| **Hosting** | [Render](https://render.com) | Runs the web listener & background tasks |
| **Scheduler** | [cron-job.org](https://cron-job.org) | Fires the hourly scan via HTTP GET |

---

## Architecture

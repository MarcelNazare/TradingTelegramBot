"""
Telegram command handlers for the Forex Signal Bot.
Separate module so main.py stays focused on signal generation.

Postgres (Neon) edition — uses psycopg2 instead of sqlite3.
All DB calls are wrapped in asyncio.to_thread() to prevent blocking the
Telegram event loop (which was causing commands like /status to hang).
"""

import os
import asyncio
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

import main as bot_main


# ============================================================
# MARKDOWN SAFETY HELPERS
# ============================================================
def _md_safe(text) -> str:
    """Strip characters Telegram Markdown V1 treats as entity markers."""
    return (str(text)
            .replace("_", " ")
            .replace("*", "")
            .replace("`", "'")
            .replace("[", "(")
            .replace("]", ")")
            .replace("~", "-"))


async def _send_with_fallback(update: Update, text: str,
                               parse_mode: str = "Markdown"):
    """Send a message, falling back to plain text if Markdown parsing fails."""
    try:
        await update.message.reply_text(text, parse_mode=parse_mode)
    except Exception:
        await update.message.reply_text(text, parse_mode=None)


# ============================================================
# DATABASE HELPERS (Async-Safe)
# ============================================================
def _connect():
    """Open a Postgres connection (autocommit for read-only queries)."""
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    conn = psycopg2.connect(url)
    conn.autocommit = True
    return conn


def _fetchone_sync(query, params=()):
    """Run a query synchronously. Called via asyncio.to_thread()."""
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return cur.fetchone()
    finally:
        conn.close()


async def _fetchone(query, params=()):
    """Run a query without blocking the event loop."""
    return await asyncio.to_thread(_fetchone_sync, query, params)


def _fetchall_sync(query, params=()):
    """Run a query synchronously. Called via asyncio.to_thread()."""
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return cur.fetchall()
    finally:
        conn.close()


async def _fetchall(query, params=()):
    """Run a query without blocking the event loop."""
    return await asyncio.to_thread(_fetchall_sync, query, params)


def _fmt_price(v):
    return f"{v:.5f}" if v is not None else "—"


def _fmt_cents(v):
    return f"{v:.1f}¢" if v is not None else "—"


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ============================================================
# /start
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Welcome to the Forex Signal Bot*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "This bot scans the major forex pairs every day after the "
        "New York close and sends you ATR-adaptive, 1:3 risk-reward "
        "signals with position sizing for your account.\n\n"
        "*Quick start:*\n"
        "• /help — see all available commands\n"
        "• /status — confirm the bot is running\n"
        "• /latest — get the most recent signal\n"
        "• /scan — run a live scan right now\n\n"
        "⚠️ *Signals only. It does not place trades.*"
    )
    await _send_with_fallback(update, text)


# ============================================================
# /help
# ============================================================
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Available Commands*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "*Information*\n"
        "`/start` — Welcome message\n"
        "`/help` — This help text\n"
        "`/about` — About the bot\n"
        "`/status` — Check bot status\n\n"
        "*Signals & data*\n"
        "`/signals` — View recent forex signals\n"
        "`/latest` — Show the latest trading signal\n"
        "`/performance` — Signal performance statistics\n\n"
        "*Configuration*\n"
        "`/pairs` — List monitored forex pairs\n"
        "`/balance` — View configured account balance\n"
        "`/risk` — Show current risk settings\n"
        "`/settings` — Show all current bot settings\n\n"
        "*Live actions*\n"
        "`/scan` — Scan forex pairs for trading setups\n"
        "`/diagnostics` — Show signal detection diagnostics\n\n"
        "Type any command to run it."
    )
    await _send_with_fallback(update, text)


# ============================================================
# /status
# ============================================================
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        last_run = await _fetchone(
            "SELECT started_at, signals_sent, errors "
            "FROM runs ORDER BY id DESC LIMIT 1"
        )
        total_row = await _fetchone("SELECT COUNT(*) AS n FROM signals")
        open_row = await _fetchone(
            "SELECT COUNT(*) AS n FROM signals WHERE outcome = 'open'"
        )
        total_signals = total_row["n"] if total_row else 0
        open_signals = open_row["n"] if open_row else 0

        if last_run:
            started = last_run["started_at"][:16].replace("T", " ")
            status_line = f"Last run: `{started} UTC`"
            result_line = (
                f"Result: {last_run['signals_sent']} sent, "
                f"{last_run['errors']} errors"
            )
        else:
            status_line = "Last run: `never`"
            result_line = "Run `/scan` to perform the first scan"

        text = (
            "✅ *Bot Status*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🟢 Online\n"
            f"🕐 {_now_utc()}\n\n"
            f"{status_line}\n"
            f"{result_line}\n\n"
            f"📊 Total signals: *{total_signals}*\n"
            f"🔓 Open signals: *{open_signals}*"
        )
    except Exception as e:
        text = f"❌ Status check failed: {_md_safe(e)}"

    await _send_with_fallback(update, text)


# ============================================================
# /signals
# ============================================================
async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rows = await _fetchall(
            "SELECT created_at, symbol, direction, entry, stop_loss, "
            "take_profit, lot_size, outcome "
            "FROM signals ORDER BY id DESC LIMIT 5"
        )

        if not rows:
            await _send_with_fallback(
                update,
                "📭 *No signals yet*\n\nRun `/scan` to perform a scan.",
            )
            return

        lines = ["📊 *Recent Signals*", "━━━━━━━━━━━━━━━━━━━━", ""]
        for r in rows:
            date = r["created_at"][:16].replace("T", " ")
            arrow = "🟢" if r["direction"] == "LONG" else "🔴"
            outcome = {
                "open": "⏳ open",
                "win": "✅ win",
                "loss": "❌ loss",
                "breakeven": "➖ be",
                "cancelled": "🚫 cancelled",
            }.get(r["outcome"], _md_safe(r["outcome"]))

            lines.append(f"{arrow} *{r['symbol']} {r['direction']}*  {outcome}")
            lines.append(
                f"  E `{_fmt_price(r['entry'])}` · "
                f"SL `{_fmt_price(r['stop_loss'])}` · "
                f"TP `{_fmt_price(r['take_profit'])}`"
            )
            lines.append(f"  Lots `{r['lot_size']}` · {date} UTC")
            lines.append("")

        await _send_with_fallback(update, "\n".join(lines))
    except Exception as e:
        await _send_with_fallback(
            update, f"❌ Error: {_md_safe(e)}"
        )


# ============================================================
# /latest
# ============================================================
async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        r = await _fetchone("SELECT * FROM signals ORDER BY id DESC LIMIT 1")

        if not r:
            await _send_with_fallback(
                update,
                "📭 *No signals yet*\n\nRun `/scan` to perform a scan.",
            )
            return

        arrow = "🟢" if r["direction"] == "LONG" else "🔴"
        outcome = {
            "open": "⏳ open",
            "win": "✅ win",
            "loss": "❌ loss",
            "breakeven": "➖ breakeven",
            "cancelled": "🚫 cancelled",
        }.get(r["outcome"], _md_safe(r["outcome"]))
        pnl_line = (
            f"PnL: `{_fmt_cents(r['pnl_cents'])}`\n"
            if r["pnl_cents"] is not None
            else ""
        )
        date = r["created_at"][:16].replace("T", " ")
        zone_type = _md_safe(r["zone_type"]).capitalize()
        sizing_src = _md_safe(r["sizing_source"])

        text = (
            f"📌 *Latest Signal*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{arrow} *{r['symbol']} {r['direction']}*\n"
            f"Status: {outcome}\n\n"
            f"Entry: `{_fmt_price(r['entry'])}`\n"
            f"Stop Loss: `{_fmt_price(r['stop_loss'])}`\n"
            f"Take Profit: `{_fmt_price(r['take_profit'])}`\n\n"
            f"Risk: `{r['risk_pips']} pips` (1:3 RR)\n"
            f"ATR: `{r['atr_pips']} pips` "
            f"(stop = {r['atr_multiple']}× ATR)\n"
            f"Lot Size: `{r['lot_size']}` ({sizing_src})\n"
            f"Risk ≈ `{_fmt_cents(r['risk_cents'])}`\n"
            f"{pnl_line}"
            f"\n"
            f"Zone: {zone_type} "
            f"({_fmt_price(r['zone_bottom'])} – {_fmt_price(r['zone_top'])})\n"
            f"Candle: `{r['signal_date'] or '—'}`\n"
            f"Created: `{date} UTC`"
        )
        await _send_with_fallback(update, text)
    except Exception as e:
        await _send_with_fallback(
            update, f"❌ Error: {_md_safe(e)}"
        )


# ============================================================
# /pairs
# ============================================================
async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pairs = [p.replace("=X", "") for p in bot_main.PAIRS]
    lines = ["💱 *Monitored Pairs*", "━━━━━━━━━━━━━━━━━━━━", ""]
    for p in pairs:
        lines.append(f"• `{p}`")
    lines.append("")
    lines.append(f"*Total: {len(pairs)} pairs*")
    await _send_with_fallback(update, "\n".join(lines))


# ============================================================
# /balance
# ============================================================
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    balance = bot_main.ACCOUNT_BALANCE
    target_risk = balance * (bot_main.RISK_PERCENT / 100)
    max_risk = balance * (bot_main.MAX_RISK_PERCENT / 100)

    text = (
        f"💰 *Account Balance*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Configured: `{balance:.2f}¢` ({balance/100:.2f} USD)\n"
        f"Target risk/trade: `{target_risk:.2f}¢` "
        f"({bot_main.RISK_PERCENT:.1f}%)\n"
        f"Max risk/trade: `{max_risk:.2f}¢` "
        f"({bot_main.MAX_RISK_PERCENT:.1f}%)\n\n"
        f"Edit ACCOUNT_BALANCE in main.py to update."
    )
    await _send_with_fallback(update, text)


# ============================================================
# /risk
# ============================================================
async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"⚖️ *Risk Settings*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Target risk/trade: `{bot_main.RISK_PERCENT:.1f}%`\n"
        f"Hard ceiling: `{bot_main.MAX_RISK_PERCENT:.1f}%`\n"
        f"Risk:Reward: `1:3`\n\n"
        f"*ATR-based stops*\n"
        f"ATR period: `{bot_main.ATR_PERIOD}`\n"
        f"ATR buffer: `{bot_main.ATR_BUFFER_MULT}× ATR`\n"
        f"Min stop: `{bot_main.MIN_STOP_ATR_MULT}× ATR`\n"
        f"Max stop: `{bot_main.MAX_STOP_ATR_MULT}× ATR`\n"
        f"Spread buffer: `{bot_main.SPREAD_BUFFER_PIPS} pips`\n\n"
        f"*Broker*\n"
        f"Min lot: `{bot_main.BROKER_MIN_LOT}`\n"
        f"Pip value/standard lot: `{bot_main.PIP_VALUE_STANDARD}¢`\n\n"
        f"Trades that would exceed the ceiling are skipped."
    )
    await _send_with_fallback(update, text)


# ============================================================
# /scan
# ============================================================
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_with_fallback(
        update,
        "🔍 *Scanning…*\nThis may take 20–40 seconds for 11 pairs.",
    )
    try:
        # Run the scan in a thread — it does blocking network + DB work
        result = await asyncio.to_thread(bot_main.run_scan)

        signals = [p for p in result["per_pair"] if p["signal"]]
        no_signal = [p for p in result["per_pair"] if not p["signal"]]

        lines = ["🔍 *Scan Results*", "━━━━━━━━━━━━━━━━━━━━", ""]

        if signals:
            lines.append(f"✅ *{len(signals)} setup(s) found*\n")
            for p in signals:
                sig = p["signal"]
                lots, risk_c, source = bot_main.calculate_lot_size(sig["entry"] * 0) # placeholder — replace with actual logic if needed
                # Note: the entry/stop/target from run_scan return are already computed
                arrow = "🟢" if sig["direction"] == "LONG" else "🔴"
                
                lines.append(f"{arrow} *{p['pair']} {sig['direction']}*")
                lines.append(
                    f"  E `{sig['entry']}` · SL `{sig['stop']}` · "
                    f"TP `{sig['target']}`"
                )
                lines.append("")
        else:
            lines.append("📭 *No setups found*\n")

        if no_signal:
            lines.append("*No signal:*")
            for p in no_signal:
                reasons = ",".join(p.get("reasons", []))[:80] or "—"
                safe_reasons = _md_safe(reasons)
                lines.append(f"• `{p['pair']}`: {safe_reasons}")

        await _send_with_fallback(update, "\n".join(lines))

    except Exception as e:
        await _send_with_fallback(
            update, f"❌ Scan failed: {_md_safe(e)}"
        )


# ============================================================
# /diagnostics
# ============================================================
async def cmd_diagnostics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        last_run = await _fetchone("SELECT id FROM runs ORDER BY id DESC LIMIT 1")
        if not last_run:
            await _send_with_fallback(
                update,
                "📭 *No runs yet*\n\nRun `/scan` to perform a scan.",
            )
            return
        run_id = last_run["id"]

        rows = await _fetchall(
            "SELECT symbol, zones, sr_levels, pools, reasons "
            "FROM pair_diagnostics WHERE run_id = %s ORDER BY symbol",
            (run_id,),
        )

        if not rows:
            await _send_with_fallback(
                update,
                "📭 *No diagnostics for the last run.*",
            )
            return

        lines = [
            f"🔬 *Diagnostics — run_id {run_id}*",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
            "`pair    zones sr pools  reasons`",
        ]
        for r in rows:
            sym = r["symbol"].replace("=X", "").ljust(7)
            reasons = _md_safe(r["reasons"] or "—")[:28]
            lines.append(
                f"`{sym} {r['zones']:>3}  {r['sr_levels']:>3} {r['pools']:>3}   {reasons}`"
            )

        await _send_with_fallback(update, "\n".join(lines))
    except Exception as e:
        await _send_with_fallback(
            update, f"❌ Error: {_md_safe(e)}"
        )


# ============================================================
# /performance
# ============================================================
async def cmd_performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        totals = await _fetchone("""
            SELECT
              COUNT(*)                                        AS total,
              SUM(CASE WHEN outcome = 'open'  THEN 1 ELSE 0 END) AS open_n,
              SUM(CASE WHEN outcome = 'win'   THEN 1 ELSE 0 END) AS wins,
              SUM(CASE WHEN outcome = 'loss'  THEN 1 ELSE 0 END) AS losses,
              SUM(CASE WHEN outcome = 'breakeven' THEN 1 ELSE 0 END) AS be,
              COALESCE(SUM(pnl_cents), 0)                     AS pnl
            FROM signals
        """)

        if not totals or totals["total"] == 0:
            await _send_with_fallback(
                update,
                "📭 *No signals yet*\n\nRun `/scan` to generate signals.",
            )
            return

        wins = totals["wins"] or 0
        losses = totals["losses"] or 0
        be = totals["be"] or 0
        closed = wins + losses
        win_rate = (wins / closed * 100) if closed else 0.0
        avg_pnl = (totals["pnl"] / closed) if closed else 0.0
        pnl_c = totals["pnl"] or 0

        per_pair = await _fetchall("""
            SELECT symbol,
                   COUNT(*) AS total,
                   SUM(CASE WHEN outcome = 'win'  THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) AS losses
            FROM signals
            WHERE outcome IN ('win', 'loss')
            GROUP BY symbol
            ORDER BY wins DESC
            LIMIT 8
        """)

        lines = [
            "📈 *Performance*",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
            f"Total signals: `{totals['total']}`",
            f"Open: `{totals['open_n'] or 0}`",
            f"Closed: `{closed}` "
            f"(✅ {wins} · ❌ {losses}"
            + (f" · ➖ {be}" if be else "") + ")",
            "",
            f"Win rate: `{win_rate:.1f}%`",
            f"Total PnL: `{_fmt_cents(pnl_c)}`",
            f"Avg PnL/trade: `{_fmt_cents(avg_pnl)}`",
        ]

        if per_pair:
            lines.append("")
            lines.append("*Per pair*")
            for p in per_pair:
                w = p["wins"] or 0
                l = p["losses"] or 0
                closed_p = w + l
                wr = (w / closed_p * 100) if closed_p else 0.0
                lines.append(
                    f"`{p['symbol']:<7}` {closed_p:>2} trades · {wr:>5.1f}% WR"
                )

        await _send_with_fallback(update, "\n".join(lines))
    except Exception as e:
        await _send_with_fallback(
            update, f"❌ Error: {_md_safe(e)}"
        )


# ============================================================
# /settings
# ============================================================
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚙️ *Current Settings*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "*Account*\n"
        f"Balance: `{bot_main.ACCOUNT_BALANCE:.0f}¢`\n"
        f"Risk: `{bot_main.RISK_PERCENT:.1f}% → "
        f"{bot_main.MAX_RISK_PERCENT:.1f}%`\n"
        f"Min lot: `{bot_main.BROKER_MIN_LOT}`\n\n"
        "*Zones & retest*\n"
        f"ZONE_BODY_MULT: `{bot_main.ZONE_BODY_MULT}`\n"
        f"RETEST_BUFFER_PIPS: `{bot_main.RETEST_BUFFER_PIPS}`\n"
        f"SR_TOLERANCE_PIPS: `{bot_main.SR_TOLERANCE_PIPS}`\n"
        f"ZONE_LOOKBACK_BARS: `{bot_main.ZONE_LOOKBACK_BARS}`\n"
        f"FRESH_ZONES_ONLY: `{bot_main.FRESH_ZONES_ONLY}`\n\n"
        "*Stops & targets*\n"
        f"ATR({bot_main.ATR_PERIOD}) · "
        f"buffer `{bot_main.ATR_BUFFER_MULT}×`\n"
        f"Min stop: `{bot_main.MIN_STOP_ATR_MULT}× ATR`\n"
        f"Max stop: `{bot_main.MAX_STOP_ATR_MULT}× ATR`\n"
        f"Spread buffer: `{bot_main.SPREAD_BUFFER_PIPS} pips`\n"
        f"RR: `1:3`\n\n"
        "*Pairs*\n"
        f"{len(bot_main.PAIRS)} monitored\n\n"
        "*Timeframe*\n"
        f"Daily close hour: `{bot_main.DAILY_CLOSE_UTC_HOUR}:00 UTC`"
    )
    await _send_with_fallback(update, text)


# ============================================================
# /about
# ============================================================
async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "ℹ️ *About This Bot*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "A Python-based forex signal generator that combines:\n"
        "• Supply/demand zone detection\n"
        "• Liquidity pools (equal highs/lows)\n"
        "• Support/resistance from swing structure\n"
        "• ATR-adaptive stop placement\n"
        "• Strict 1:3 risk-reward ratio\n"
        "• Dynamic position sizing "
        "(target and hard ceiling)\n\n"
        "*Data*: Yahoo Finance daily candles\n"
        "*Timeframe*: Daily (multi-hour to multi-day holds)\n"
        "*Pairs*: 7 majors + 4 crosses\n"
        "*Storage*: Neon Postgres (`signals`, `runs`, `pair_diagnostics`)\n\n"
        "⚠️ *Important*\n"
        "This bot *sends signals only*. It does not connect to "
        "your broker and does not place trades. All risk is yours.\n\n"
        "Past performance does not guarantee future results."
    )
    await _send_with_fallback(update, text)


# ============================================================
# APPLICATION BUILDER
# ============================================================
def build_application() -> Application:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN not set")

    app = Application.builder().token(token).build()

    handlers = {
        "start":       cmd_start,
        "help":        cmd_help,
        "status":      cmd_status,
        "signals":     cmd_signals,
        "latest":      cmd_latest,
        "pairs":       cmd_pairs,
        "balance":     cmd_balance,
        "risk":        cmd_risk,
        "scan":        cmd_scan,
        "diagnostics": cmd_diagnostics,
        "performance": cmd_performance,
        "settings":    cmd_settings,
        "about":       cmd_about,
    }

    for name, func in handlers.items():
        app.add_handler(CommandHandler(name, func))

    return app
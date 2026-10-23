"""
Telegram command handlers for the Forex Signal Bot.
Separate module so main.py stays focused on signal generation.

Every dynamic value that lands in a Markdown message is passed through
_md_safe() first. If Telegram still rejects the message, _send_with_fallback()
retries it as plain text — so no command can crash on a parse error.
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

import main as bot_main

DB_PATH = Path(__file__).parent / "signals.db"


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
# DATABASE HELPERS
# ============================================================
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


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
        "👋 *Welcome to the Masheroooo Forex Signal Bot*\n"
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
        conn = _connect()
        try:
            last_run = conn.execute(
                "SELECT started_at, signals_sent, errors "
                "FROM runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            total_signals = conn.execute(
                "SELECT COUNT(*) AS n FROM signals"
            ).fetchone()["n"]
            open_signals = conn.execute(
                "SELECT COUNT(*) AS n FROM signals WHERE outcome = 'open'"
            ).fetchone()["n"]
        finally:
            conn.close()

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
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT created_at, symbol, direction, entry, stop_loss, "
                "take_profit, lot_size, outcome "
                "FROM signals ORDER BY id DESC LIMIT 5"
            ).fetchall()
        finally:
            conn.close()

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
        conn = _connect()
        try:
            r = conn.execute(
                "SELECT * FROM signals ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()

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
        results = []
        for pair in bot_main.PAIRS:
            try:
                df = bot_main.fetch_data(pair, interval="1d", period="6mo")
                if df.empty:
                    results.append((pair, None, {"reasons": ["no data"],
                                                  "zones": 0, "sr": 0, "pools": 0}))
                    continue
                signal, diag = bot_main.generate_signal(df, pair)
                results.append((pair, signal, diag))
            except Exception as e:
                results.append((pair, None,
                                {"reasons": [f"error {type(e).__name__}"],
                                 "zones": 0, "sr": 0, "pools": 0}))

        signals = [(p, s) for p, s, d in results if s is not None]
        no_signal = [(p, d) for p, s, d in results if s is None]

        lines = ["🔍 *Scan Results*", "━━━━━━━━━━━━━━━━━━━━", ""]

        if signals:
            lines.append(f"✅ *{len(signals)} setup(s) found*\n")
            for pair, sig in signals:
                lots, risk_c, source = bot_main.calculate_lot_size(sig["risk_pips"])
                arrow = "🟢" if sig["direction"] == "LONG" else "🔴"
                safe_source = _md_safe(source)

                lines.append(f"{arrow} *{sig['symbol']} {sig['direction']}*")
                lines.append(
                    f"  E `{sig['entry']}` · SL `{sig['stop']}` · "
                    f"TP `{sig['target']}`"
                )
                if lots == 0.0:
                    # No italic wrapper — source may contain underscores
                    lines.append(f"  ⚠️ Position too large: {safe_source}")
                else:
                    lines.append(f"  Lots `{lots}` · risk `{risk_c:.1f}¢`")
                lines.append("")
        else:
            lines.append("📭 *No setups found*\n")

        if no_signal:
            lines.append("*No signal:*")
            for pair, d in no_signal:
                reasons = ",".join(d.get("reasons", []))[:80] or "—"
                safe_reasons = _md_safe(reasons)
                sym = pair.replace("=X", "")
                lines.append(f"• `{sym}`: {safe_reasons}")

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
        conn = _connect()
        try:
            last_run_id = conn.execute(
                "SELECT id FROM runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not last_run_id:
                await _send_with_fallback(
                    update,
                    "📭 *No runs yet*\n\nRun `/scan` to perform a scan.",
                )
                return
            run_id = last_run_id["id"]

            rows = conn.execute(
                "SELECT symbol, zones, sr_levels, pools, reasons "
                "FROM pair_diagnostics WHERE run_id = ? ORDER BY symbol",
                (run_id,),
            ).fetchall()
        finally:
            conn.close()

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
        conn = _connect()
        try:
            totals = conn.execute("""
                SELECT
                  COUNT(*)                                        AS total,
                  SUM(outcome = 'open')                           AS open_n,
                  SUM(outcome = 'win')                            AS wins,
                  SUM(outcome = 'loss')                           AS losses,
                  SUM(outcome = 'breakeven')                      AS be,
                  COALESCE(SUM(pnl_cents), 0)                     AS pnl
                FROM signals
            """).fetchone()

            if totals["total"] == 0:
                await _send_with_fallback(
                    update,
                    "📭 *No signals yet*\n\nRun `/scan` to generate signals.",
                )
                return

            closed = (totals["wins"] or 0) + (totals["losses"] or 0)
            win_rate = (
                (totals["wins"] or 0) / closed * 100 if closed else 0.0
            )
            avg_pnl = (totals["pnl"] / closed) if closed else 0.0
            pnl_c = totals["pnl"] or 0

            per_pair = conn.execute("""
                SELECT symbol,
                       COUNT(*)                          AS total,
                       SUM(outcome = 'win')              AS wins,
                       SUM(outcome = 'loss')             AS losses
                FROM signals
                WHERE outcome IN ('win', 'loss')
                GROUP BY symbol
                ORDER BY wins DESC
                LIMIT 8
            """).fetchall()
        finally:
            conn.close()

        lines = [
            "📈 *Performance*",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
            f"Total signals: `{totals['total']}`",
            f"Open: `{totals['open_n'] or 0}`",
            f"Closed: `{closed}` "
            f"(✅ {totals['wins'] or 0} · ❌ {totals['losses'] or 0}"
            + (f" · ➖ {totals['be']}" if totals['be'] else "") + ")",
            "",
            f"Win rate: `{win_rate:.1f}%`",
            f"Total PnL: `{_fmt_cents(pnl_c)}`",
            f"Avg PnL/trade: `{_fmt_cents(avg_pnl)}`",
        ]

        if per_pair:
            lines.append("")
            lines.append("*Per pair*")
            for p in per_pair:
                closed_p = (p["wins"] or 0) + (p["losses"] or 0)
                wr = (
                    (p["wins"] or 0) / closed_p * 100 if closed_p else 0.0
                )
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
        "*Storage*: SQLite (`signals.db`)\n\n"
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
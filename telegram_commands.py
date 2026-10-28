import os
import asyncio
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import main as bot_main

def _md_safe(text) -> str:
    return (str(text)
            .replace("_", " ")
            .replace("*", "")
            .replace("`", "'")
            .replace("[", "(")
            .replace("]", ")")
            .replace("~", "-"))

async def _send_with_fallback(update: Update, text: str, parse_mode: str = "Markdown"):
    try:
        await update.message.reply_text(text, parse_mode=parse_mode)
    except Exception:
        await update.message.reply_text(text, parse_mode=None)

def _connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    conn = psycopg2.connect(url)
    conn.autocommit = True
    return conn

def _fetchone_sync(query, params=()):
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return cur.fetchone()
    finally:
        conn.close()

async def _fetchone(query, params=()):
    return await asyncio.to_thread(_fetchone_sync, query, params)

def _fetchall_sync(query, params=()):
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return cur.fetchall()
    finally:
        conn.close()

async def _fetchall(query, params=()):
    return await asyncio.to_thread(_fetchall_sync, query, params)

def _fmt_price(v):
    return f"{v:.5f}" if v is not None else "—"

def _fmt_cents(v):
    return f"{v:.1f}¢" if v is not None else "—"

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Welcome to the Survival Scalp Bot*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "This bot scans the 7 major pairs on the 1-hour timeframe. "
        "It only takes LONG trades above the 200 EMA.\n\n"
        "*Quick start:*\n"
        "• /help — commands\n"
        "• /status — bot status\n"
        "• /scan — live scan\n\n"
        "⚠️ *Signals only. Manage your own risk.*"
    )
    await _send_with_fallback(update, text)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Available Commands*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "*Information*\n"
        "`/start` `/help` `/about` `/status`\n\n"
        "*Signals & data*\n"
        "`/signals` `/latest` `/performance`\n\n"
        "*Configuration*\n"
        "`/pairs` `/balance` `/risk` `/settings`\n\n"
        "*Live actions*\n"
        "`/scan` `/diagnostics`"
    )
    await _send_with_fallback(update, text)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        last_run = await _fetchone("SELECT started_at, signals_sent, errors FROM runs ORDER BY id DESC LIMIT 1")
        total_row = await _fetchone("SELECT COUNT(*) AS n FROM signals")
        open_row = await _fetchone("SELECT COUNT(*) AS n FROM signals WHERE outcome = 'open'")
        
        total_signals = total_row["n"] if total_row else 0
        open_signals = open_row["n"] if open_row else 0

        if last_run:
            started = last_run["started_at"][:16].replace("T", " ")
            status_line = f"Last run: `{started} UTC`"
            result_line = f"Result: {last_run['signals_sent']} sent, {last_run['errors']} errors"
        else:
            status_line = "Last run: `never`"
            result_line = "Run `/scan` to start"

        text = (
            "✅ *Bot Status*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🟢 Online\n"
            f"🕐 {_now_utc()}\n\n"
            f"{status_line}\n{result_line}\n\n"
            f"📊 Total signals: *{total_signals}*\n"
            f"🔓 Open signals: *{open_signals}*"
        )
    except Exception as e:
        text = f"❌ Status check failed: {_md_safe(e)}"
    await _send_with_fallback(update, text)

async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rows = await _fetchall("SELECT created_at, symbol, direction, entry, stop_loss, take_profit, lot_size, outcome FROM signals ORDER BY id DESC LIMIT 5")
        if not rows:
            return await _send_with_fallback(update, "📭 *No signals yet*")
        
        lines = ["📊 *Recent Signals*", "━━━━━━━━━━━━━━━━━━━━", ""]
        for r in rows:
            date = r["created_at"][:16].replace("T", " ")
            arrow = "🟢" if r["direction"] == "LONG" else "🔴"
            outcome = r["outcome"].upper()
            lines.append(f"{arrow} *{r['symbol']} {r['direction']}*  {outcome}")
            lines.append(f"  E `{_fmt_price(r['entry'])}` · SL `{_fmt_price(r['stop_loss'])}` · TP `{_fmt_price(r['take_profit'])}`")
            lines.append("")
        await _send_with_fallback(update, "\n".join(lines))
    except Exception as e:
        await _send_with_fallback(update, f"❌ Error: {_md_safe(e)}")

async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        r = await _fetchone("SELECT * FROM signals ORDER BY id DESC LIMIT 1")
        if not r:
            return await _send_with_fallback(update, "📭 *No signals yet*")
        
        arrow = "🟢" if r["direction"] == "LONG" else "🔴"
        text = (
            f"📌 *Latest Signal*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{arrow} *{r['symbol']} {r['direction']}*\n"
            f"Entry: `{_fmt_price(r['entry'])}`\n"
            f"Stop Loss: `{_fmt_price(r['stop_loss'])}`\n"
            f"Take Profit: `{_fmt_price(r['take_profit'])}`\n"
            f"Outcome: `{r['outcome'].upper()}`\n"
        )
        await _send_with_fallback(update, text)
    except Exception as e:
        await _send_with_fallback(update, f"❌ Error: {_md_safe(e)}")

async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pairs = [p.replace("=X", "") for p in bot_main.PAIRS]
    text = "💱 *Monitored Pairs*\n\n" + "\n".join([f"• `{p}`" for p in pairs])
    await _send_with_fallback(update, text)

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = bot_main.ACCOUNT_BALANCE
    text = (
        f"💰 *Account Balance*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Configured: `{bal:.2f}¢`\n"
        f"Target risk: `{bal * (bot_main.RISK_PERCENT/100):.2f}¢` ({bot_main.RISK_PERCENT}%)\n"
        f"Max risk: `{bal * (bot_main.MAX_RISK_PERCENT/100):.2f}¢` ({bot_main.MAX_RISK_PERCENT}%)"
    )
    await _send_with_fallback(update, text)

async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"⚖️ *Risk Settings*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Risk: `{bot_main.RISK_PERCENT}%` → `{bot_main.MAX_RISK_PERCENT}%`\n"
        f"RR Ratio: `1:{bot_main.RR_RATIO}`\n"
        f"ATR: `{bot_main.ATR_PERIOD}`\n"
        f"Max Daily Trades: `{bot_main.MAX_DAILY_TRADES}`"
    )
    await _send_with_fallback(update, text)

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_with_fallback(update, "🔍 *Scanning…*\nThis may take 20–40 seconds.")
    try:
        result = await asyncio.to_thread(bot_main.run_scan)

        signals = [p for p in result["per_pair"] if p["signal"]]
        no_signal = [p for p in result["per_pair"] if not p["signal"]]

        lines = ["🔍 *Scan Results*", "━━━━━━━━━━━━━━━━━━━━", ""]

        if signals:
            lines.append(f"✅ *{len(signals)} setup(s) found*\n")
            for p in signals:
                sig = p["signal"]
                arrow = "🟢" if sig["direction"] == "LONG" else "🔴"
                lines.append(f"{arrow} *{p['pair']} {sig['direction']}*")
                lines.append(f"  E `{sig['entry']}` · SL `{sig['stop']}` · TP `{sig['target']}`")
                lines.append("")
        else:
            lines.append("📭 *No setups found*\n")

        if no_signal:
            lines.append("*No signal:*")
            for p in no_signal:
                reasons = ",".join(p.get("reasons", []))[:80] or "—"
                lines.append(f"• `{p['pair']}`: {_md_safe(reasons)}")

        await _send_with_fallback(update, "\n".join(lines))
    except Exception as e:
        await _send_with_fallback(update, f"❌ Scan failed: {_md_safe(e)}")

async def cmd_diagnostics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        last_run = await _fetchone("SELECT id FROM runs ORDER BY id DESC LIMIT 1")
        if not last_run:
            return await _send_with_fallback(update, "📭 *No runs yet*")
        run_id = last_run["id"]
        rows = await _fetchall("SELECT symbol, zones, sr_levels, pools, reasons FROM pair_diagnostics WHERE run_id = %s ORDER BY symbol", (run_id,))
        if not rows:
            return await _send_with_fallback(update, "📭 *No diagnostics.*")
            
        lines = [f"🔬 *Diagnostics — run_id {run_id}*", "━━━━━━━━━━━━━━━━━━━━", ""]
        for r in rows:
            sym = r["symbol"].replace("=X", "").ljust(7)
            reasons = _md_safe(r["reasons"] or "—")[:25]
            lines.append(f"`{sym} z:{r['zones']} s:{r['sr_levels']} p:{r['pools']}` {reasons}")
        await _send_with_fallback(update, "\n".join(lines))
    except Exception as e:
        await _send_with_fallback(update, f"❌ Error: {_md_safe(e)}")

async def cmd_performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        totals = await _fetchone("""
            SELECT COUNT(*) AS total,
              SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) AS wins,
              SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) AS losses,
              COALESCE(SUM(pnl_cents), 0) AS pnl
            FROM signals
        """)
        if not totals or totals["total"] == 0:
            return await _send_with_fallback(update, "📭 *No signals yet*")
            
        wins = totals["wins"] or 0
        losses = totals["losses"] or 0
        closed = wins + losses
        win_rate = (wins / closed * 100) if closed else 0.0
        
        text = (
            f"📈 *Performance*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Total signals: `{totals['total']}`\n"
            f"Closed: `{closed}` (✅ {wins} · ❌ {losses})\n"
            f"Win rate: `{win_rate:.1f}%`\n"
            f"Total PnL: `{_fmt_cents(totals['pnl'])}`"
        )
        await _send_with_fallback(update, text)
    except Exception as e:
        await _send_with_fallback(update, f"❌ Error: {_md_safe(e)}")

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"⚙️ *Current Settings*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Timeframe: `{bot_main.INTERVAL}`\n"
        f"RR Ratio: `1:{bot_main.RR_RATIO}`\n"
        f"ZONE_BODY_MULT: `{bot_main.ZONE_BODY_MULT}`\n"
        f"RETEST_BUFFER: `{bot_main.RETEST_BUFFER_PIPS}`\n"
        f"SR_TOLERANCE: `{bot_main.SR_TOLERANCE_PIPS}`\n"
        f"Max Trades/Day: `{bot_main.MAX_DAILY_TRADES}`"
    )
    await _send_with_fallback(update, text)

async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "ℹ️ *About This Bot*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Trend-Following Edition.\n"
        "1-Hour chart, 7 Major Pairs.\n"
        "Long-Only above 200 EMA.\n"
        "1:3 Risk/Reward, 1% Account Risk.\n\n"
        "⚠️ *Signals only. Not financial advice.*"
    )
    await _send_with_fallback(update, text)

def build_application() -> Application:
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN not set")
    app = Application.builder().token(token).build()
    handlers = {
        "start": cmd_start, "help": cmd_help, "status": cmd_status,
        "signals": cmd_signals, "latest": cmd_latest, "pairs": cmd_pairs,
        "balance": cmd_balance, "risk": cmd_risk, "scan": cmd_scan,
        "diagnostics": cmd_diagnostics, "performance": cmd_performance,
        "settings": cmd_settings, "about": cmd_about,
    }
    for name, func in handlers.items():
        app.add_handler(CommandHandler(name, func))
    return app
"""
Forex Supply/Demand + Liquidity Trading Bot
- Weltrade Micro (Cent) account edition
- 1:3 Risk-Reward, ATR-adaptive stops
- Dynamic risk: 5% target, 10% hard ceiling
- Daily timeframe, major pairs
- Designed for PythonAnywhere free tier
"""

# ============================================================
# WARNING SUPPRESSION — must come before importing yfinance/pandas
# ============================================================
import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*Pandas4Warning.*")
warnings.filterwarnings("ignore", message=".*inplace.*")

import os
import asyncio
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from telegram import Bot

# ============================================================
# ENVIRONMENT
# ============================================================
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError(
        "TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in .env "
        "or in the environment."
    )

# ============================================================
# CONFIGURATION — WELTRADE MICRO (CENT) ACCOUNT
# ============================================================
ACCOUNT_BALANCE    = 300.0    # cents (300¢ = $3 real)
RISK_PERCENT       = 5.0      # target risk per trade
MAX_RISK_PERCENT   = 10.0     # hard ceiling — skip if minimum lot exceeds this
BROKER_MIN_LOT     = 0.01     # Weltrade micro minimum
PIP_VALUE_STANDARD = 0.10     # cents per pip per 1.0 cent-lot — MUST be calibrated
SPREAD_BUFFER_PIPS = 2.0

# ATR stop tuning
ATR_PERIOD        = 14
ATR_BUFFER_MULT   = 0.3       # ATR buffer beyond zone edge
MIN_STOP_ATR_MULT = 1.0       # minimum stop distance from entry, in ATR
MAX_STOP_ATR_MULT = 3.0       # maximum stop; skip trade if exceeded

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()   # INFO or DEBUG

PAIRS = ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X",
         "AUDUSD=X", "USDCAD=X", "NZDUSD=X"]

# ============================================================
# DATA
# ============================================================
def fetch_data(symbol: str, interval: str = "1d", period: str = "6mo") -> pd.DataFrame:
    df = yf.download(symbol, interval=interval, period=period,
                     progress=False, auto_adjust=False)
    if df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.columns = ["open", "high", "low", "close", "volume"]
    return df

# ============================================================
# INDICATORS
# ============================================================
def calculate_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat(
        [high - low,
         (high - close.shift()).abs(),
         (low  - close.shift()).abs()],
        axis=1
    ).max(axis=1)
    return tr.rolling(period).mean()

# ============================================================
# MARKET STRUCTURE
# ============================================================
def detect_swing_points(df: pd.DataFrame, window: int = 5):
    highs, lows = [], []
    for i in range(window, len(df) - window):
        if df["high"].iloc[i] == df["high"].iloc[i-window:i+window+1].max():
            highs.append(i)
        if df["low"].iloc[i] == df["low"].iloc[i-window:i+window+1].min():
            lows.append(i)
    return highs, lows

def find_supply_demand_zones(df: pd.DataFrame, lookback: int = 50):
    avg_body = (df["close"] - df["open"]).abs().rolling(20).mean().iloc[-1]
    if pd.isna(avg_body) or avg_body <= 0:
        return []
    zones = []
    for i in range(2, min(lookback, len(df) - 1)):
        body = abs(df["close"].iloc[i] - df["open"].iloc[i])
        if body < 1.5 * avg_body:
            continue
        if df["close"].iloc[i] > df["open"].iloc[i]:
            prev = i - 1
            if df["close"].iloc[prev] < df["open"].iloc[prev]:
                zones.append({"type": "demand",
                              "top": df["high"].iloc[prev],
                              "bottom": df["low"].iloc[prev],
                              "index": prev})
        elif df["close"].iloc[i] < df["open"].iloc[i]:
            prev = i - 1
            if df["close"].iloc[prev] > df["open"].iloc[prev]:
                zones.append({"type": "supply",
                              "top": df["high"].iloc[prev],
                              "bottom": df["low"].iloc[prev],
                              "index": prev})
    return zones

def find_liquidity_pools(df: pd.DataFrame, tolerance_pips: float = 3.0):
    pips = 0.0001 if df["close"].iloc[-1] < 10 else 0.01
    tol  = tolerance_pips * pips
    highs, lows = df["high"].values, df["low"].values
    pools = []
    for i in range(len(highs) - 1):
        for j in range(i + 1, len(highs)):
            if abs(highs[i] - highs[j]) <= tol:
                pools.append({"type": "equal_highs", "level": (highs[i] + highs[j]) / 2})
            if abs(lows[i] - lows[j]) <= tol:
                pools.append({"type": "equal_lows", "level": (lows[i] + lows[j]) / 2})
    return pools

def find_support_resistance(df: pd.DataFrame, window: int = 10):
    highs, lows = detect_swing_points(df, window)
    pips = 0.0001 if df["close"].iloc[-1] < 10 else 0.01
    tol  = 5 * pips
    sr = []
    for idx in highs:
        level = df["high"].iloc[idx]
        if sum(1 for h in df["high"] if abs(h - level) <= tol) >= 2:
            sr.append({"type": "resistance", "level": level})
    for idx in lows:
        level = df["low"].iloc[idx]
        if sum(1 for l in df["low"] if abs(l - level) <= tol) >= 2:
            sr.append({"type": "support", "level": level})
    return sr

# ============================================================
# SIGNAL GENERATION (with telemetry)
# ============================================================
def generate_signal(df: pd.DataFrame, symbol: str):
    """
    Returns (signal_dict, diagnostics_dict).
    signal_dict is None when no setup qualifies.
    diagnostics_dict always explains why.
    """
    diag = {
        "zones": 0, "sr": 0, "pools": 0,
        "reasons": [],
    }

    if len(df) < 60:
        diag["reasons"].append("insufficient_data")
        return None, diag

    atr = calculate_atr(df).iloc[-1]
    if pd.isna(atr) or atr <= 0:
        diag["reasons"].append("no_atr")
        return None, diag

    zones = find_supply_demand_zones(df)
    pools = find_liquidity_pools(df)
    sr    = find_support_resistance(df)

    diag["zones"] = len(zones)
    diag["sr"]    = len(sr)
    diag["pools"] = len(pools)

    if not zones:
        diag["reasons"].append("no_zones")
        return None, diag

    last_close = df["close"].iloc[-1]
    last_low   = df["low"].iloc[-1]
    last_high  = df["high"].iloc[-1]
    pip = 0.0001 if last_close < 10 else 0.01

    # ---------------- LONG ----------------
    for zone in zones:
        if zone["type"] != "demand":
            continue
        if not (zone["bottom"] <= last_low <= zone["top"]):
            diag["reasons"].append("long_no_retest")
            continue
        if not any(abs(s["level"] - zone["bottom"]) <= 5 * pip
                   for s in sr if s["type"] == "support"):
            diag["reasons"].append("long_no_support")
            continue
        if any(p["type"] == "equal_lows"
               and zone["bottom"] - 10 * pip <= p["level"] <= zone["bottom"]
               for p in pools):
            diag["reasons"].append("long_liquidity_trap")
            continue

        entry     = last_close
        zone_dist = max(entry - zone["bottom"], 0.0)
        raw_dist  = zone_dist + ATR_BUFFER_MULT * atr
        stop_dist = max(raw_dist, MIN_STOP_ATR_MULT * atr)

        if stop_dist > MAX_STOP_ATR_MULT * atr:
            diag["reasons"].append("long_stop_too_wide")
            continue

        stop   = entry - stop_dist
        target = entry + 3 * stop_dist

        return {
            "symbol":       symbol.replace("=X", ""),
            "direction":    "LONG",
            "entry":        round(entry, 5),
            "stop":         round(stop, 5),
            "target":       round(target, 5),
            "risk_pips":    round(stop_dist / pip, 1),
            "atr_pips":     round(atr / pip, 1),
            "atr_multiple": round(stop_dist / atr, 2),
            "zone":         zone,
        }, diag

    # ---------------- SHORT ----------------
    for zone in zones:
        if zone["type"] != "supply":
            continue
        if not (zone["bottom"] <= last_high <= zone["top"]):
            diag["reasons"].append("short_no_retest")
            continue
        if not any(abs(s["level"] - zone["top"]) <= 5 * pip
                   for s in sr if s["type"] == "resistance"):
            diag["reasons"].append("short_no_resistance")
            continue
        if any(p["type"] == "equal_highs"
               and zone["top"] <= p["level"] <= zone["top"] + 10 * pip
               for p in pools):
            diag["reasons"].append("short_liquidity_trap")
            continue

        entry     = last_close
        zone_dist = max(zone["top"] - entry, 0.0)
        raw_dist  = zone_dist + ATR_BUFFER_MULT * atr
        stop_dist = max(raw_dist, MIN_STOP_ATR_MULT * atr)

        if stop_dist > MAX_STOP_ATR_MULT * atr:
            diag["reasons"].append("short_stop_too_wide")
            continue

        stop   = entry + stop_dist
        target = entry - 3 * stop_dist

        return {
            "symbol":       symbol.replace("=X", ""),
            "direction":    "SHORT",
            "entry":        round(entry, 5),
            "stop":         round(stop, 5),
            "target":       round(target, 5),
            "risk_pips":    round(stop_dist / pip, 1),
            "atr_pips":     round(atr / pip, 1),
            "atr_multiple": round(stop_dist / atr, 2),
            "zone":         zone,
        }, diag

    if not diag["reasons"]:
        diag["reasons"].append("no_matching_direction")
    return None, diag

# ============================================================
# POSITION SIZING — dynamic with hard risk ceiling
# ============================================================
def calculate_lot_size(stop_pips: float):
    """Returns (lot_size, risk_cents, source) or (0.0, risk_cents, reason)."""
    if stop_pips <= 0:
        return 0.0, 0.0, "invalid_stop"

    target_risk_cents = ACCOUNT_BALANCE * (RISK_PERCENT / 100)
    lots = target_risk_cents / (stop_pips * PIP_VALUE_STANDARD)

    if lots < BROKER_MIN_LOT:
        min_lot_risk = BROKER_MIN_LOT * stop_pips * PIP_VALUE_STANDARD
        max_allowed  = ACCOUNT_BALANCE * (MAX_RISK_PERCENT / 100)
        if min_lot_risk > max_allowed:
            return 0.0, min_lot_risk, "min_lot_exceeds_max_risk"
        lots       = BROKER_MIN_LOT
        risk_cents = min_lot_risk
        source     = "min_lot_floor"
    else:
        lots       = round(lots, 2)
        risk_cents = lots * stop_pips * PIP_VALUE_STANDARD
        source     = "target"

    risk_pct = (risk_cents / ACCOUNT_BALANCE) * 100
    if risk_pct > MAX_RISK_PERCENT:
        return 0.0, risk_cents, "rounded_exceeds_max_risk"

    return lots, risk_cents, source

# ============================================================
# TELEGRAM
# ============================================================
async def send_telegram(signal: dict, lot_size: float,
                        risk_cents: float, source: str):
    bot = Bot(token=TELEGRAM_TOKEN)
    risk_usd   = risk_cents / 100
    reward_usd = risk_usd * 3
    risk_pct   = (risk_cents / ACCOUNT_BALANCE) * 100

    msg = (
        f"🚨 *{signal['symbol']} {signal['direction']}*\n"
        f"━━━━━━━━━━━━━━\n"
        f"Entry: `{signal['entry']}`\n"
        f"Stop Loss: `{signal['stop']}`\n"
        f"Take Profit: `{signal['target']}`\n"
        f"Risk: {signal['risk_pips']} pips (1:3 RR)\n"
        f"ATR({ATR_PERIOD}): {signal['atr_pips']} pips  |  "
        f"Stop = {signal['atr_multiple']}× ATR\n"
        f"Lot Size: `{lot_size}` ({source})\n"
        f"Risk ≈ {risk_cents:.1f}¢ ({risk_pct:.1f}% of account)\n"
        f"Max win ≈ {reward_usd * 100:.1f}¢\n"
        f"Zone: {signal['zone']['type'].capitalize()} "
        f"({signal['zone']['bottom']:.5f} – {signal['zone']['top']:.5f})\n"
        f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    )
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")

# ============================================================
# MAIN
# ============================================================
def main():
    print(f"[START] {datetime.utcnow().isoformat()} UTC | "
          f"Balance={ACCOUNT_BALANCE}¢ | TargetRisk={RISK_PERCENT}% | "
          f"MaxRisk={MAX_RISK_PERCENT}% | PipValue={PIP_VALUE_STANDARD} | "
          f"LogLevel={LOG_LEVEL}")

    signals_sent = 0
    for pair in PAIRS:
        try:
            df = fetch_data(pair, interval="1d", period="6mo")
            if df.empty:
                print(f"[SKIP] {pair}: no data")
                continue

            signal, diag = generate_signal(df, pair)

            # Always print filter telemetry
            if signal is None:
                reasons = ",".join(diag["reasons"]) or "unknown"
                print(f"[NONE] {pair}: zones={diag['zones']} "
                      f"sr={diag['sr']} pools={diag['pools']} | {reasons}")
                continue

            lots, risk_cents, source = calculate_lot_size(signal["risk_pips"])

            if lots == 0.0:
                print(f"[SKIP] {pair} {signal['direction']}: {source} "
                      f"(stop={signal['risk_pips']} pips, "
                      f"min-lot risk={risk_cents:.1f}¢ > {MAX_RISK_PERCENT}% ceiling)")
                continue

            print(f"[SIGNAL] {pair} {signal['direction']} | "
                  f"lots={lots} | risk={risk_cents:.1f}¢ "
                  f"({(risk_cents/ACCOUNT_BALANCE)*100:.1f}%) | source={source} | "
                  f"zones={diag['zones']} sr={diag['sr']} pools={diag['pools']}")

            asyncio.run(send_telegram(signal, lots, risk_cents, source))
            signals_sent += 1

        except Exception as e:
            print(f"[ERROR] {pair}: {type(e).__name__}: {e}")

    print(f"[DONE] {signals_sent} signal(s) sent.")

if __name__ == "__main__":
    main()
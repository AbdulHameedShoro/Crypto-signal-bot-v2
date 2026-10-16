"""Bitget Balanced V5 — closed-candle USDT perpetual signal scanner.

Requirements: requests, pandas, numpy
GitHub Actions: run `python bitget_balanced_v5.py` once per scheduled invocation.
Environment: DISCORD_WEBHOOK_URL (required for alerts), DISCORD_PING_TAG (optional),
SEND_WATCH_ALERTS=1 (optional), RUN_FOREVER=1 (optional for local machines).
This bot sends informational signals; it does not execute orders or promise win rates.
"""
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("balanced-v5")

API = "https://api.bitget.com"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ZENUSDT", "ZECUSDT"]
TIMEFRAMES = {"15m": 900, "1H": 3600, "4H": 14400}
MIN_TRADE_SCORE = int(os.getenv("MIN_TRADE_SCORE", "65"))
MIN_WATCH_SCORE = int(os.getenv("MIN_WATCH_SCORE", "50"))
SEND_WATCH_ALERTS = os.getenv("SEND_WATCH_ALERTS", "0") == "1"
RUN_FOREVER = os.getenv("RUN_FOREVER", "0") == "1"
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
PING_TAG = os.getenv("DISCORD_PING_TAG", "").strip()
STATE_FILE = Path(os.getenv("SIGNAL_STATE_FILE", "signal_state_v5.json"))
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "BalancedSignalBot/5.0"})


def candles(symbol, timeframe="15m", limit=250):
    resp = SESSION.get(f"{API}/api/v2/mix/market/candles", params={
        "symbol": symbol, "productType": "USDT-FUTURES", "granularity": timeframe,
        "limit": str(limit)}, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != "00000":
        raise RuntimeError(f"Bitget error {symbol} {timeframe}: {payload.get('msg', payload)}")
    rows = payload.get("data") or []
    if not rows:
        raise ValueError(f"No candles for {symbol} {timeframe}")
    frame = pd.DataFrame([{
        "timestamp": int(r[0]), "open": float(r[1]), "high": float(r[2]),
        "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])
    } for r in rows]).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    # Bitget candle timestamp is the START of the candle. Never score an unfinished bar.
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    frame = frame.loc[frame.timestamp + TIMEFRAMES[timeframe] * 1000 <= now_ms].copy()
    if len(frame) < 70:
        raise ValueError(f"Insufficient CLOSED {timeframe} candles for {symbol}: {len(frame)}")
    return frame.reset_index(drop=True)


def indicators(df):
    d = df.copy()
    close = d.close
    for n in (20, 50, 200):
        d[f"ema{n}"] = close.ewm(span=n, adjust=False).mean()
    change = close.diff()
    up = change.clip(lower=0)
    down = (-change).clip(lower=0)
    avg_up = up.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_down = down.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = avg_up / avg_down.replace(0, np.nan)
    d["rsi"] = (100 - 100 / (1 + rs)).fillna(50)
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    d["macd_hist"] = macd - macd.ewm(span=9, adjust=False).mean()
    prev_close = close.shift(1)
    tr = pd.concat([(d.high - d.low), (d.high - prev_close).abs(),
                    (d.low - prev_close).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    d["vol_ma"] = d.volume.shift(1).rolling(20).mean()  # compare against PRIOR bars
    return d


def higher_trend(df):
    d = indicators(df)
    c = d.iloc[-1]
    if c.close > c.ema50 and c.ema20 > c.ema50:
        return "BULLISH"
    if c.close < c.ema50 and c.ema20 < c.ema50:
        return "BEARISH"
    return "NEUTRAL"



def smc_structure(d, side, atr):
    """Confirmed swing structure and recent, unfilled three-candle FVG zones."""
    bullish = side == "BUY"
    highs, lows = [], []
    for i in range(3, len(d) - 3):
        window = d.iloc[i-3:i+4]
        if d.high.iloc[i] == window.high.max() and (window.high == d.high.iloc[i]).sum() == 1:
            highs.append((i, float(d.high.iloc[i])))
        if d.low.iloc[i] == window.low.min() and (window.low == d.low.iloc[i]).sum() == 1:
            lows.append((i, float(d.low.iloc[i])))
    swings = highs if bullish else lows
    opposite = lows if bullish else highs
    last_level = swings[-1][1] if swings else None
    prev_close = float(d.close.iloc[-2])
    close = float(d.close.iloc[-1])
    bos = bool(last_level is not None and
               (prev_close <= last_level < close if bullish else prev_close >= last_level > close))
    # CHoCH is a break against the previous confirmed swing direction.
    prior_trend = "UNKNOWN"
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1][1] > highs[-2][1] and lows[-1][1] > lows[-2][1]:
            prior_trend = "BULLISH"
        elif highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1]:
            prior_trend = "BEARISH"
    choch = bos and prior_trend == ("BEARISH" if bullish else "BULLISH")
    # Wick through a prior confirmed swing, followed by close back inside.
    sweep = False
    if opposite:
        level = opposite[-1][1]
        sweep = bool((float(d.low.iloc[-1]) < level < close) if bullish
                     else (float(d.high.iloc[-1]) > level > close))
    zones = []
    for i in range(max(2, len(d)-35), len(d)):
        a, c = d.iloc[i-2], d.iloc[i]
        lo, hi = (float(a.high), float(c.low)) if bullish else (float(c.high), float(a.low))
        if hi - lo < 0.12 * atr:
            continue
        # FVG must have survived all subsequent completed candles.
        later = d.iloc[i+1:]
        invalid = (later.low <= lo).any() if bullish else (later.high >= hi).any()
        if not invalid:
            zones.append((lo, hi))
    zone = zones[-1] if zones else None
    fvg_near = bool(zone and zone[0] - 0.5*atr <= close <= zone[1] + 0.5*atr)
    # Proxy order block: last opposing candle before a strong displacement.
    order_block = None
    for i in range(len(d)-2, max(2, len(d)-25), -1):
        bar, following = d.iloc[i], d.iloc[i+1]
        opposing = bar.close < bar.open if bullish else bar.close > bar.open
        displacement = (following.close - following.open) if bullish else (following.open - following.close)
        if opposing and displacement >= 0.8*atr:
            order_block = (float(bar.low), float(bar.high))
            break
    ob_near = bool(order_block and order_block[0]-0.35*atr <= close <= order_block[1]+0.35*atr)
    return {"bos": bos, "choch": choch, "sweep": sweep,
            "fvg": zone, "fvg_near": fvg_near, "order_block": order_block,
            "ob_near": ob_near, "swing_level": last_level, "prior_trend": prior_trend}

def evaluate(symbol, base, trend_1h="UNKNOWN", trend_4h="UNKNOWN"):
    d = indicators(base)
    c, p = d.iloc[-1], d.iloc[-2]
    price, atr = float(c.close), float(c.atr)
    if not (math.isfinite(atr) and atr > 0 and price > 0):
        raise ValueError("Invalid ATR/price")
    vol_ratio = float(c.volume / c.vol_ma) if c.vol_ma > 0 else 0.0
    recent_high = float(d.high.iloc[-21:-1].max())
    recent_low = float(d.low.iloc[-21:-1].min())
    candle_range = max(float(c.high - c.low), 1e-12)
    body_ratio = abs(float(c.close - c.open)) / candle_range
    close_position = float((c.close - c.low) / candle_range)
    result = {}
    for side in ("BUY", "SELL"):
        bullish = side == "BUY"
        score, reasons = 0, []
        def add(points, reason):
            nonlocal score
            score += points
            reasons.append(reason)
        if (c.ema20 > c.ema50) == bullish:
            add(20, "15m EMA20/50 trend")
        if (c.close > c.ema50) == bullish:
            add(10, "Price on correct side of EMA50")
        if (c.macd_hist > 0) == bullish:
            add(10, "MACD histogram agrees")
        if bullish and 48 <= c.rsi <= 70 or not bullish and 30 <= c.rsi <= 52:
            add(15, f"RSI supports direction ({c.rsi:.1f})")
        if (c.close > c.open) == bullish and body_ratio >= 0.40 and (
            close_position >= 0.65 if bullish else close_position <= 0.35
        ):
            add(15, "Strong directional closed candle")
        elif (c.close > p.high if bullish else c.close < p.low):
            add(10, "Closed beyond previous candle")
        if (c.close > recent_high if bullish else c.close < recent_low):
            add(10, "20-candle structure breakout")
        elif (c.low <= recent_low + atr * 0.4 and c.close > c.open if bullish
              else c.high >= recent_high - atr * 0.4 and c.close < c.open):
            add(8, "Rejection near recent structure")
        if vol_ratio >= 1.10:
            add(10, f"Volume above prior average ({vol_ratio:.2f}x)")
        elif vol_ratio >= 0.65:
            add(5, f"Volume acceptable ({vol_ratio:.2f}x)")
        target = "BULLISH" if bullish else "BEARISH"
        if trend_1h == target:
            add(7, "1H trend agrees")
        if trend_4h == target:
            add(3, "4H trend agrees")
        smc = smc_structure(d, side, atr)
        if smc["bos"]:
            add(8, "Confirmed swing BOS")
        if smc["choch"]:
            add(6, "Potential CHoCH")
        if smc["sweep"]:
            add(7, "Swing liquidity sweep and reclaim")
        if smc["fvg_near"]:
            add(6, "Price near active FVG")
        if smc["ob_near"]:
            add(5, "Price near displacement order block")
        # Scores are confluence points, NOT a statistical probability of success.
        blockers = []
        if vol_ratio < 0.20:
            blockers.append("Extremely low volume")
        if trend_1h != "UNKNOWN" and trend_4h != "UNKNOWN" and trend_1h != target and trend_4h != target and (c.ema20 > c.ema50) != bullish:
            blockers.append("All available trends oppose entry")
        # Avoid chasing a move that has already stretched far from the fast EMA.
        if abs(price - float(c.ema20)) > 2.0 * atr:
            blockers.append("Entry too extended from EMA20")
        result[side] = {"score": min(score, 100), "reasons": reasons, "blockers": blockers, "smc": smc}
    side = max(result, key=lambda k: result[k]["score"])
    best, other = result[side], result["SELL" if side == "BUY" else "BUY"]
    status = "WAIT"
    if best["score"] >= MIN_TRADE_SCORE and best["score"] - other["score"] >= 10:
        status = "WATCH" if best["blockers"] else side
    elif best["score"] >= MIN_WATCH_SCORE:
        status = "WATCH"
    # ATR targets are consistent with risk-reward requirements (TP2: 3/1.5 = 2.0).
    direction = 1 if side == "BUY" else -1
    stop = price - direction * 1.5 * atr
    tp1 = price + direction * 1.5 * atr
    tp2 = price + direction * 3.0 * atr
    tp3 = price + direction * 4.5 * atr
    precision = 4 if price < 10 else 3 if price < 100 else 2
    return {"symbol": symbol, "status": status, "bias": side, "score": best["score"],
            "opposite_score": other["score"], "price": round(price, precision),
            "sl": round(stop, precision), "tp1": round(tp1, precision),
            "tp2": round(tp2, precision), "tp3": round(tp3, precision),
            "rr_tp2": 2.0, "rsi": round(float(c.rsi), 1),
            "volume_ratio": round(vol_ratio, 2), "trend_1h": trend_1h,
            "trend_4h": trend_4h, "candle_utc": datetime.fromtimestamp(
                int(c.timestamp) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "reasons": best["reasons"], "blockers": best["blockers"], "smc": best["smc"]}


def load_state():
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        temp = STATE_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        temp.replace(STATE_FILE)
    except OSError as exc:
        log.warning("Could not persist alert state: %s", exc)


def send_discord(sig):
    if not WEBHOOK:
        log.warning("DISCORD_WEBHOOK_URL missing; alert not sent")
        return False
    status = sig["status"]
    color = 0x10B981 if status == "BUY" else 0xF43F5E if status == "SELL" else 0xF59E0B
    fields = [
        {"name": "Closed candle UTC", "value": sig["candle_utc"], "inline": True},
        {"name": "Setup quality", "value": f"{sig['score']}/100", "inline": True},
        {"name": "15m / 1H / 4H", "value": f"15m bias {sig['bias']} | {sig['trend_1h']} | {sig['trend_4h']}", "inline": False},
        {"name": "RSI / Volume", "value": f"{sig['rsi']} / {sig['volume_ratio']}x", "inline": True},
        {"name": "Reasons", "value": "\n".join("• " + x for x in sig["reasons"])[:1000] or "None", "inline": False},
    ]
    smc = sig["smc"]
    def zone_text(zone):
        return f"{zone[0]:.6g} - {zone[1]:.6g}" if zone else "Not detected"
    fields.append({"name": "SMC structure (15m)", "value":
        f"BOS: {'YES' if smc['bos'] else 'NO'} | CHoCH: {'YES' if smc['choch'] else 'NO'} | Sweep: {'YES' if smc['sweep'] else 'NO'}\n"
        f"Swing level: {smc['swing_level'] if smc['swing_level'] is not None else 'N/A'}\n"
        f"FVG: {zone_text(smc['fvg'])} | Near: {smc['fvg_near']}\n"
        f"Order block (proxy): {zone_text(smc['order_block'])} | Near: {smc['ob_near']}", "inline": False})
    if status in ("BUY", "SELL"):
        fields.insert(2, {"name": "Entry / SL / TP1 / TP2 / TP3", "value":
            f"{sig['price']} / {sig['sl']} / {sig['tp1']} / {sig['tp2']} / {sig['tp3']}\n"
            "TP2 R:R 1:2 before fees/slippage; partial exits alter combined R:R", "inline": False})
    if sig["blockers"]:
        fields.append({"name": "Warnings", "value": "; ".join(sig["blockers"]), "inline": False})
    payload = {"username": "Bitget Balanced V5", "content":
               f"{PING_TAG + ' ' if PING_TAG else ''}{status} {sig['symbol']} | {sig['score']}/100",
               "embeds": [{"title": f"{sig['symbol']} | {status}" + (f" ({sig['bias']} bias)" if status == "WATCH" else ""),
                           "color": color, "fields": fields,
                           "footer": {"text": "Closed-candle paper signals; not guaranteed trades"}}]}
    try:
        r = SESSION.post(WEBHOOK, json=payload, timeout=12)
        r.raise_for_status()
        return True
    except requests.RequestException as exc:
        log.error("Discord failed for %s: %s", sig["symbol"], exc)
        return False


def scan_once():
    state = load_state()
    for symbol in SYMBOLS:
        try:
            base = candles(symbol)
            trends = {}
            for tf in ("1H", "4H"):
                try:
                    trends[tf] = higher_trend(candles(symbol, tf, 250))
                except (ValueError, requests.RequestException, RuntimeError) as exc:
                    log.warning("%s %s trend unavailable: %s", symbol, tf, exc)
                    trends[tf] = "UNKNOWN"
            sig = evaluate(symbol, base, trends["1H"], trends["4H"])
            log.info("%s | %s | %s score=%d opposite=%d vol=%.2fx blockers=%s",
                     symbol, sig["candle_utc"], sig["status"], sig["score"],
                     sig["opposite_score"], sig["volume_ratio"], sig["blockers"])
            if sig["status"] == "WAIT" or (sig["status"] == "WATCH" and not SEND_WATCH_ALERTS):
                continue
            # Suppress repeated alert for same symbol, side/status and closed candle.
            key = f"{sig['candle_utc']}|{sig['status']}|{sig['bias']}"
            if state.get(symbol) == key:
                continue
            if send_discord(sig):
                state[symbol] = key
                save_state(state)
        except (requests.RequestException, ValueError, RuntimeError, KeyError, IndexError) as exc:
            log.error("%s scan failed: %s", symbol, exc)
        time.sleep(0.25)


if __name__ == "__main__":
    if not WEBHOOK:
        log.warning("No webhook configured: scan and log only")
    if RUN_FOREVER:
        while True:
            scan_once()
            time.sleep(900)
    else:
        scan_once()

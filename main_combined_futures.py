# -*- coding: utf-8 -*-
"""
Crypto Signal Bot V5 - Bitget USDT perpetuals, 15m signals.

Design idea (different from V4):
  * Score-based, not a long chain of AND conditions. Every setup collects
    points (trend, higher timeframe, ADX, volume, RSI, candle, room).
  * Only a few HARD gates exist (volume floor, ranging market, no room,
    stop too wide, 1H and 4H both against). Everything else is a soft score.
  * Three setups: Pullback to EMA20 in a trend, Breakout, Breakout-Retest.
  * Same code path for live scanning and backtesting, so a backtest shows
    what the live bot would really have done.

Usage:
  python crypto_signal_bot_v5.py                      live scan -> Discord
  python crypto_signal_bot_v5.py --no-discord         live scan, print only
  python crypto_signal_bot_v5.py --backtest           45-day backtest, all coins
  python crypto_signal_bot_v5.py --backtest --days 60 --min-score 70
  python crypto_signal_bot_v5.py --backtest --coins BTCUSDT,ETHUSDT
"""
import argparse
import bisect
import csv
import math
import os
import statistics
import sys
import time

import requests

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
BITGET_URL = "https://api.bitget.com"
TIMEOUT = 12

BASE_MS = 15 * 60 * 1000
HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS

COINS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "DOGEUSDT", "ZENUSDT", "ZECUSDT",
    "CKBUSDT",
]

# ---------------------------------------------------------------------------
# Settings. These are reasoned starting values, NOT optimised numbers.
# Use the backtest to check them before trusting them.
# ---------------------------------------------------------------------------
CFG = {
    "MIN_SCORE": 65,          # minimum score (0-100) for a signal
    "GRADE_A": 80,
    "GRADE_B": 70,
    "HARD_MIN_VOL": 0.50,     # volume / median of last 40 candles
    "MIN_ADX": 15,            # below this the market is treated as ranging
    "MAX_DIST_ATR": 2.0,      # max distance from EMA20 (in ATR)
    "PULLBACK_MAX_DIST_ATR": 1.5,
    "MIN_ROOM_R": 1.2,        # min room (in R) before nearest obstacle
    "MAX_RISK_ATR": 2.5,      # reject if stop is wider than this
    "SL_ATR": 1.2,            # minimum stop distance (ATR)
    "SL_BUFFER_ATR": 0.15,
    "TP1_R": 1.5,
    "TP2_R": 2.5,
    # backtest only
    "FEE_PCT": 0.06,          # taker fee per side, percent
    "SLIPPAGE_PCT": 0.01,     # per side, percent
    "MAX_HOLD": 96,           # candles (24h)
}

BUY_ICON = "\U0001F7E2"
SELL_ICON = "\U0001F534"
WAIT_ICON = "\U0001F7E1"
EYE_ICON = "\U0001F440"


# ===========================================================================
# Indicators (full series, so backtests are fast and match live exactly)
# ===========================================================================
def ema_series(values, period):
    n = len(values)
    if n == 0:
        return []
    if n < period:
        return [values[-1]] * n
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    out = [e] * n
    for i in range(period, n):
        e = (values[i] - e) * k + e
        out[i] = e
    return out


def rsi_series(values, period=14):
    n = len(values)
    out = [50.0] * n
    if n <= period:
        return out
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        ch = values[i] - values[i - 1]
        gains[i] = max(ch, 0.0)
        losses[i] = max(-ch, 0.0)
    ag = sum(gains[1:period + 1]) / period
    al = sum(losses[1:period + 1]) / period

    def val(a, b):
        if b == 0:
            return 100.0 if a > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + a / b)

    out[period] = val(ag, al)
    for i in range(period + 1, n):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        out[i] = val(ag, al)
    return out


def atr_series(h, l, c, period=14):
    n = len(c)
    out = [0.0] * n
    if n <= period:
        return out
    tr = [0.0] * n
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    a = sum(tr[1:period + 1]) / period
    out[period] = a
    for i in range(period + 1, n):
        a = (a * (period - 1) + tr[i]) / period
        out[i] = a
    first = out[period]
    for i in range(period):
        out[i] = first
    return out


def adx_series(h, l, c, period=14):
    """Wilder ADX, +DI, -DI as aligned lists."""
    n = len(c)
    adx = [0.0] * n
    pdi = [0.0] * n
    mdi = [0.0] * n
    if n < period * 2 + 2:
        return adx, pdi, mdi
    tr = [0.0] * n
    pdm = [0.0] * n
    mdm = [0.0] * n
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        up = h[i] - h[i - 1]
        dn = l[i - 1] - l[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
    trs = sum(tr[1:period + 1])
    ps = sum(pdm[1:period + 1])
    ms = sum(mdm[1:period + 1])
    dx = [0.0] * n
    for i in range(period, n):
        if i > period:
            trs = trs - trs / period + tr[i]
            ps = ps - ps / period + pdm[i]
            ms = ms - ms / period + mdm[i]
        if trs > 0:
            p = 100.0 * ps / trs
            m = 100.0 * ms / trs
            t = p + m
            pdi[i], mdi[i] = p, m
            dx[i] = 100.0 * abs(p - m) / t if t > 0 else 0.0
    first = period * 2
    a = sum(dx[period:first]) / period
    adx[first - 1] = a
    for i in range(first, n):
        a = (a * (period - 1) + dx[i]) / period
        adx[i] = a
    return adx, pdi, mdi


# ===========================================================================
# Data helpers
# ===========================================================================
def build_data(rows):
    """rows: closed 15m candles [ts, o, h, l, c, v, ...] ascending."""
    d = {
        "ts": [int(r[0]) for r in rows],
        "o": [float(r[1]) for r in rows],
        "h": [float(r[2]) for r in rows],
        "l": [float(r[3]) for r in rows],
        "c": [float(r[4]) for r in rows],
        "v": [float(r[5]) for r in rows],
    }
    d["ema20"] = ema_series(d["c"], 20)
    d["ema50"] = ema_series(d["c"], 50)
    d["rsi"] = rsi_series(d["c"])
    d["atr"] = atr_series(d["h"], d["l"], d["c"])
    d["adx"], d["pdi"], d["mdi"] = adx_series(d["h"], d["l"], d["c"])
    daily = {}
    for k, t in enumerate(d["ts"]):
        day = t // DAY_MS
        e = daily.get(day)
        if e is None:
            daily[day] = [d["h"][k], d["l"][k], 1]
        else:
            e[0] = max(e[0], d["h"][k])
            e[1] = min(e[1], d["l"][k])
            e[2] += 1
    d["daily"] = daily
    return d


def prev_day_levels(d, i):
    day = d["ts"][i] // DAY_MS
    e = d["daily"].get(day - 1)
    if e and e[2] >= 24:
        return e[0], e[1]
    return None, None


def build_htf(rows, dur_ms):
    """Trend state per higher-timeframe bar (rows ascending)."""
    closes = [float(r[4]) for r in rows]
    e20 = ema_series(closes, 20)
    e50 = ema_series(closes, 50)
    trend = []
    for k in range(len(closes)):
        if k < 55:
            trend.append("UNKNOWN")
        elif closes[k] > e20[k] > e50[k]:
            trend.append("BULLISH")
        elif closes[k] < e20[k] < e50[k]:
            trend.append("BEARISH")
        else:
            trend.append("NEUTRAL")
    ends = [int(r[0]) + dur_ms for r in rows]
    return {"ends": ends, "trend": trend}


def htf_trend_at(htf, t_ms):
    """Trend of the latest higher-timeframe bar fully closed by t_ms."""
    idx = bisect.bisect_right(htf["ends"], t_ms) - 1
    return htf["trend"][idx] if idx >= 0 else "UNKNOWN"


def resample(rows, dur_ms):
    need = dur_ms // BASE_MS
    groups = {}
    for r in rows:
        groups.setdefault(int(r[0]) // dur_ms, []).append(r)
    out = []
    for key in sorted(groups):
        g = sorted(groups[key], key=lambda r: int(r[0]))
        if len(g) < need:
            continue
        out.append([
            key * dur_ms,
            float(g[0][1]),
            max(float(x[2]) for x in g),
            min(float(x[3]) for x in g),
            float(g[-1][4]),
            sum(float(x[5]) for x in g),
        ])
    return out


# ===========================================================================
# Signal engine
# ===========================================================================
def candle_flags(o_, h_, l_, c_, s):
    """(strong, rejection) candle in direction s (+1 long, -1 short)."""
    rng = h_ - l_
    if rng <= 0:
        return False, False
    body = abs(c_ - o_)
    cp = (c_ - l_) / rng if s == 1 else (h_ - c_) / rng
    wick = (min(o_, c_) - l_) if s == 1 else (h_ - max(o_, c_))
    strong = (c_ - o_) * s > 0 and body / rng >= 0.40 and cp >= 0.60
    reject = (
        wick >= body * 0.75 and wick / rng >= 0.30
        and cp >= 0.55 and (c_ - o_) * s >= 0
    )
    return strong, reject


def nearest_pivot(h, l, i, s, price, lookback=120, n=3):
    """Nearest confirmed swing high above (s=1) / swing low below (s=-1)."""
    best = None
    start = max(n, i - lookback)
    for j in range(start, i - n + 1):
        if s == 1:
            if h[j] > price and h[j] == max(h[j - n:j + n + 1]):
                if best is None or h[j] < best:
                    best = h[j]
        else:
            if l[j] < price and l[j] == min(l[j - n:j + n + 1]):
                if best is None or l[j] > best:
                    best = l[j]
    return best


def grade_of(score, cfg):
    if score >= cfg["GRADE_A"]:
        return "A"
    if score >= cfg["GRADE_B"]:
        return "B"
    return "C"


def evaluate_side(d, i, s, t1h, t4h, cfg):
    o, h, l, c, v = d["o"], d["h"], d["l"], d["c"], d["v"]
    price = c[i]
    atr = d["atr"][i]
    e20, e50 = d["ema20"][i], d["ema50"][i]
    adx, rsi = d["adx"][i], d["rsi"][i]
    side = "BUY" if s == 1 else "SELL"

    base_vol = statistics.median(v[i - 40:i])
    vr = v[i] / base_vol if base_vol > 0 else 0.0
    dist = s * (price - e20) / atr
    strong, reject = candle_flags(o[i], h[i], l[i], c[i], s)
    cand_ok = strong or reject
    di_ok = s * (d["pdi"][i] - d["mdi"][i]) > 0
    trend15 = s * (e20 - e50) > 0
    ema50_slope = s * (e50 - d["ema50"][i - 6]) > 0
    ema20_slope = s * (e20 - d["ema20"][i - 3]) > 0
    beyond_ema = s * (price - e20) > 0

    # ---- setups ----------------------------------------------------------
    ext = min(l[i - 4:i + 1]) if s == 1 else max(h[i - 4:i + 1])
    touched = s * (e20 - ext) >= -0.25 * atr
    pullback = (
        trend15 and ema20_slope and touched and beyond_ema and cand_ok
        and dist <= cfg["PULLBACK_MAX_DIST_ATR"]
    )

    if s == 1:
        level = max(h[i - 20:i])
        level2 = max(h[i - 21:i - 1])
        ext_i = l[i]
    else:
        level = min(l[i - 20:i])
        level2 = min(l[i - 21:i - 1])
        ext_i = h[i]
    fresh = s * (price - level) > 0 and s * (c[i - 1] - level) <= 0
    breakout = fresh and strong and vr >= 1.2 and adx >= 18 and beyond_ema
    retest = (
        s * (c[i - 1] - level2) > 0 and s * (level2 - ext_i) >= 0
        and s * (price - level2) > 0 and cand_ok and vr >= 0.8 and beyond_ema
    )
    setup = (
        "Breakout" if breakout
        else "Retest" if retest
        else "Pullback" if pullback
        else None
    )

    # ---- stop / targets / room ------------------------------------------
    if s == 1:
        struct = min(l[i - 7:i + 1]) - cfg["SL_BUFFER_ATR"] * atr
        sl = min(price - cfg["SL_ATR"] * atr, struct)
    else:
        struct = max(h[i - 7:i + 1]) + cfg["SL_BUFFER_ATR"] * atr
        sl = max(price + cfg["SL_ATR"] * atr, struct)
    risk = abs(price - sl)
    tp1 = price + s * risk * cfg["TP1_R"]
    tp2 = price + s * risk * cfg["TP2_R"]

    obstacle = nearest_pivot(h, l, i, s, price)
    pdh, pdl = prev_day_levels(d, i)
    if s == 1 and pdh is not None and pdh > price:
        obstacle = pdh if obstacle is None else min(obstacle, pdh)
    if s == -1 and pdl is not None and pdl < price:
        obstacle = pdl if obstacle is None else max(obstacle, pdl)
    if obstacle is None or risk <= 0:
        room = math.inf
    else:
        room = (s * (obstacle - price) - 0.1 * atr) / risk

    # ---- score (max 100) -------------------------------------------------
    want = "BULLISH" if s == 1 else "BEARISH"
    opp = "BEARISH" if s == 1 else "BULLISH"
    score = 0
    score += 10 if trend15 else 0
    score += 5 if ema50_slope else 0
    score += 12 if t1h == want else (-8 if t1h == opp else 0)
    score += 8 if t4h == want else (-12 if t4h == opp else 0)
    score += 10 if adx >= 25 else 7 if adx >= 20 else 3 if adx >= 15 else 0
    score += 5 if di_ok else 0
    score += (
        10 if vr >= 1.5 else 7 if vr >= 1.0 else 4 if vr >= 0.7
        else 1 if vr >= 0.5 else -5
    )
    r_eff = rsi if s == 1 else 100.0 - rsi
    score += 8 if 45 <= r_eff <= 62 else 4 if 40 <= r_eff <= 70 else 0
    score -= 5 if r_eff > 75 else 0
    score += 7 if strong else (5 if reject else 0)
    score += 10 if setup else 0
    score += 10 if room >= 2.0 else 7 if room >= 1.5 else 3 if room >= 1.0 else 0
    score += 5 if dist <= 1.0 else 0
    score = max(0, min(100, int(round(score))))

    # ---- hard gates ------------------------------------------------------
    reasons = []
    if setup is None:
        reasons.append("No setup (no pullback/breakout/retest)")
    if vr < cfg["HARD_MIN_VOL"]:
        reasons.append(f"Low volume ({vr:.2f}x)")
    if adx < cfg["MIN_ADX"]:
        reasons.append(f"Ranging market (ADX {adx:.0f})")
    if t1h == opp and t4h == opp:
        reasons.append("1H and 4H both against")
    if dist > cfg["MAX_DIST_ATR"]:
        reasons.append("Entry too extended from EMA20")
    if risk > cfg["MAX_RISK_ATR"] * atr:
        reasons.append("Stop too wide")
    if room < cfg["MIN_ROOM_R"]:
        reasons.append(f"Little room ({room:.1f}R)")
    if score < cfg["MIN_SCORE"]:
        reasons.append(f"Score {score} < {cfg['MIN_SCORE']}")

    return {
        "side": side, "ok": not reasons, "score": score,
        "grade": grade_of(score, cfg), "setup": setup, "reasons": reasons,
        "price": price, "atr": atr, "sl": sl, "tp1": tp1, "tp2": tp2,
        "risk": risk, "room": room, "vr": vr, "adx": adx, "rsi": rsi,
        "dist": dist, "trend_1h": t1h, "trend_4h": t4h, "ts": d["ts"][i],
    }


def evaluate(d, i, t1h, t4h, cfg=None):
    cfg = cfg or CFG
    if i < 60 or d["atr"][i] <= 0:
        return None
    results = [evaluate_side(d, i, s, t1h, t4h, cfg) for s in (1, -1)]
    passing = [r for r in results if r["ok"]]
    pool = passing or results
    best = max(pool, key=lambda r: r["score"])
    best = dict(best)
    best["signal"] = best["side"] if best["ok"] else "WAIT"
    return best


# ===========================================================================
# Bitget API
# ===========================================================================
def _get(path, params):
    resp = requests.get(
        f"{BITGET_URL}{path}", params=params,
        headers={"Accept": "application/json"}, timeout=TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict) or payload.get("code") != "00000":
        raise ValueError(f"Bitget error: {payload}")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError("Unexpected Bitget response")
    return rows


def fetch_candles(symbol, granularity, limit=200):
    rows = _get("/api/v2/mix/market/candles", {
        "symbol": symbol, "productType": "USDT-FUTURES",
        "granularity": granularity, "limit": str(limit),
    })
    rows = [r for r in rows if isinstance(r, (list, tuple)) and len(r) >= 6]
    rows.sort(key=lambda r: int(r[0]))
    return rows


def fetch_history(symbol, days):
    """Page backwards through 15m history (for the backtest)."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * DAY_MS
    collected = {}
    for path in ("/api/v2/mix/market/history-candles",
                 "/api/v2/mix/market/candles"):
        collected.clear()
        cursor = end_ms
        try:
            for _ in range(400):
                rows = _get(path, {
                    "symbol": symbol, "productType": "USDT-FUTURES",
                    "granularity": "15m", "limit": "200",
                    "endTime": str(cursor),
                })
                rows = [r for r in rows
                        if isinstance(r, (list, tuple)) and len(r) >= 6]
                if not rows:
                    break
                for r in rows:
                    collected[int(r[0])] = r
                oldest = min(int(r[0]) for r in rows)
                if oldest <= start_ms or oldest >= cursor:
                    break
                cursor = oldest - 1
                time.sleep(0.12)
        except (requests.RequestException, ValueError) as exc:
            print(f"  {symbol}: {path} failed ({exc})")
        if len(collected) >= 500:
            break
    out = [collected[k] for k in sorted(collected) if k >= start_ms]
    now_ms = int(time.time() * 1000)
    return [r for r in out if int(r[0]) + BASE_MS <= now_ms]


# ===========================================================================
# Live scan
# ===========================================================================
def fmt(x):
    return f"{x:.8g}"


def scan_symbol(symbol):
    now_ms = int(time.time() * 1000)
    rows = fetch_candles(symbol, "15m", 200)
    rows = [r for r in rows if int(r[0]) + BASE_MS <= now_ms]
    if len(rows) < 100:
        raise ValueError("not enough closed candles")
    d = build_data(rows)
    h1 = build_htf(fetch_candles(symbol, "1H", 100), HOUR_MS)
    h4 = build_htf(fetch_candles(symbol, "4H", 100), 4 * HOUR_MS)
    res = evaluate(d, len(rows) - 1,
                   htf_trend_at(h1, now_ms), htf_trend_at(h4, now_ms))
    if res is None:
        raise ValueError("not enough data to evaluate")
    return res


def format_signal(symbol, r):
    icon = BUY_ICON if r["signal"] == "BUY" else SELL_ICON
    risk_pct = r["risk"] / r["price"] * 100
    room = "none ahead" if math.isinf(r["room"]) else f"{r['room']:.1f}R"
    when = time.strftime("%H:%M", time.gmtime((r["ts"] + BASE_MS) / 1000))
    return (
        f"{icon} **{symbol} {r['signal']}** | Grade {r['grade']} "
        f"(score {r['score']}/100)\n"
        f"Setup: {r['setup']}\n"
        f"Entry: {fmt(r['price'])}\n"
        f"\U0001F6D1 SL: {fmt(r['sl'])} ({risk_pct:.2f}% risk)\n"
        f"\U0001F3AF TP1: {fmt(r['tp1'])} ({CFG['TP1_R']}R) | "
        f"TP2: {fmt(r['tp2'])} ({CFG['TP2_R']}R)\n"
        f"Room to next obstacle: {room}\n"
        f"1H {r['trend_1h']} | 4H {r['trend_4h']} | ADX {r['adx']:.0f} | "
        f"RSI {r['rsi']:.0f} | Vol {r['vr']:.2f}x\n"
        f"Candle closed: {when} UTC"
    )


def send_discord(message):
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL is not configured.")
        return
    max_len, parts, current = 1900, [], ""
    for section in message.split("\n\n"):
        section = section.strip()
        if not section:
            continue
        cand = current + "\n\n" + section if current else section
        if len(cand) <= max_len:
            current = cand
        else:
            if current:
                parts.append(current)
            while len(section) > max_len:
                parts.append(section[:max_len])
                section = section[max_len:]
            current = section
    if current:
        parts.append(current)
    for i, part in enumerate(parts, 1):
        resp = requests.post(WEBHOOK_URL, json={"content": part},
                             timeout=TIMEOUT)
        print(f"Discord {i}/{len(parts)}: {resp.status_code}")
        resp.raise_for_status()


def run_live(coins, send=True):
    print("Crypto Signal Bot V5 - live scan")
    signals, near, waits = [], [], []
    for symbol in coins:
        try:
            r = scan_symbol(symbol)
        except Exception as exc:
            print(f"{symbol} error: {exc}")
            waits.append(f"{WAIT_ICON} {symbol}: error ({exc})")
            continue
        print(
            f"{symbol}: {r['signal']} side={r['side']} score={r['score']} "
            f"setup={r['setup']} vol={r['vr']:.2f} adx={r['adx']:.0f} "
            f"1H={r['trend_1h']} 4H={r['trend_4h']} "
            f"blocked={'; '.join(r['reasons']) or 'none'}"
        )
        if r["signal"] in ("BUY", "SELL"):
            signals.append(format_signal(symbol, r))
        elif (r["score"] >= CFG["MIN_SCORE"] - 10
              and len(r["reasons"]) <= 2):
            near.append(
                f"{EYE_ICON} {symbol}: almost {r['side']} "
                f"({r['score']}/{CFG['MIN_SCORE']}) - "
                f"{'; '.join(r['reasons'])}"
            )
        else:
            waits.append(
                f"{WAIT_ICON} {symbol}: WAIT (best {r['side']} {r['score']}) - "
                f"{r['reasons'][0] if r['reasons'] else 'n/a'}"
            )
    blocks = []
    head = f"**Signal Bot V5** | {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC"
    blocks.append(head + (f" | {len(signals)} signal(s)" if signals
                          else " | no confirmed signals"))
    blocks.extend(signals)
    if near:
        blocks.append("**Close to a signal (not tradable yet):**\n"
                      + "\n".join(near))
    if waits:
        blocks.append("**Waiting:**\n" + "\n".join(waits))
    message = "\n\n".join(blocks)
    print("\n" + message)
    if send:
        send_discord(message)


# ===========================================================================
# Backtester
# ===========================================================================
def simulate_trade(d, i, r, cfg):
    """Enter at next candle open. 50% at TP1, rest to TP2, stop to breakeven.
    If SL and TP are hit in the same candle, SL is assumed first."""
    s = 1 if r["side"] == "BUY" else -1
    n = len(d["c"])
    if i + 1 >= n:
        return None
    entry = d["o"][i + 1]
    sl, tp1, tp2 = r["sl"], r["tp1"], r["tp2"]
    if s * (sl - entry) >= 0 or s * (tp1 - entry) <= 0:
        return None
    risk = abs(entry - sl)
    if risk > cfg["MAX_RISK_ATR"] * 1.3 * r["atr"]:
        return None

    stage, realized, reason, exit_j = 0, 0.0, "", i + 1
    last = min(i + cfg["MAX_HOLD"], n - 1)
    for j in range(i + 1, last + 1):
        hi, lo, cl = d["h"][j], d["l"][j], d["c"][j]
        adverse = lo if s == 1 else hi
        favorable = hi if s == 1 else lo
        exit_j = j
        if stage == 0:
            if s * (sl - adverse) >= 0:
                realized += s * (sl - entry) / risk
                reason = "SL"
                break
            if s * (favorable - tp1) >= 0:
                realized += 0.5 * s * (tp1 - entry) / risk
                stage = 1
        else:
            if s * (entry - adverse) >= 0:
                reason = "BE"
                break
            if s * (favorable - tp2) >= 0:
                realized += 0.5 * s * (tp2 - entry) / risk
                reason = "TP2"
                break
        if j == last:
            part = 1.0 if stage == 0 else 0.5
            realized += part * s * (cl - entry) / risk
            reason = "TIME"
    cost_r = 2 * (cfg["FEE_PCT"] + cfg["SLIPPAGE_PCT"]) / 100.0 * entry / risk
    return {
        "side": r["side"], "setup": r["setup"], "grade": r["grade"],
        "score": r["score"], "entry_ts": d["ts"][i + 1], "entry": entry,
        "sl": sl, "tp1": tp1, "tp2": tp2, "exit_i": exit_j,
        "exit_ts": d["ts"][exit_j], "reason": reason,
        "gross_R": realized, "net_R": realized - cost_r,
        "hold": exit_j - i,
    }


def backtest_rows(symbol, rows, cfg):
    d = build_data(rows)
    h1 = build_htf(resample(rows, HOUR_MS), HOUR_MS)
    h4 = build_htf(resample(rows, 4 * HOUR_MS), 4 * HOUR_MS)
    n = len(d["c"])
    start = 60
    while start < n and htf_trend_at(h4, d["ts"][start] + BASE_MS) == "UNKNOWN":
        start += 1
    trades, i = [], start
    while i < n - 1:
        t = d["ts"][i] + BASE_MS
        res = evaluate(d, i, htf_trend_at(h1, t), htf_trend_at(h4, t), cfg)
        if res and res["signal"] in ("BUY", "SELL"):
            tr = simulate_trade(d, i, res, cfg)
            if tr:
                tr["symbol"] = symbol
                trades.append(tr)
                i = tr["exit_i"] + 2
                continue
        i += 1
    span_days = max((d["ts"][-1] - d["ts"][start]) / DAY_MS, 1e-9)
    return trades, span_days


def stats(trades):
    if not trades:
        return None
    nets = [t["net_R"] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    cum = peak = dd = 0.0
    for x in nets:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    streak = cur = 0
    for x in nets:
        cur = cur + 1 if x <= 0 else 0
        streak = max(streak, cur)
    return {
        "n": len(nets), "win": len(wins) / len(nets) * 100,
        "avg": sum(nets) / len(nets), "total": sum(nets),
        "gross": sum(t["gross_R"] for t in trades),
        "pf": pf, "dd": dd, "streak": streak,
    }


def line(label, st):
    if not st:
        return f"{label:<22} no trades"
    pf = "inf" if st["pf"] == float("inf") else f"{st['pf']:.2f}"
    return (
        f"{label:<22} trades={st['n']:<4} win={st['win']:5.1f}% "
        f"avgR={st['avg']:+.2f} totalR={st['total']:+.1f} "
        f"PF={pf:<5} maxDD={st['dd']:.1f}R"
    )


def run_backtest(coins, days, fetch=None):
    fetch = fetch or fetch_history
    print(f"Backtest V5 | {days} days | min score {CFG['MIN_SCORE']} | "
          f"cost/side {CFG['FEE_PCT'] + CFG['SLIPPAGE_PCT']:.2f}%")
    all_trades, per_coin, total_days = [], {}, 0.0
    for sym in coins:
        rows = fetch(sym, days)
        if len(rows) < 600:
            print(f"{sym}: not enough history ({len(rows)} candles), skipped")
            continue
        trades, span = backtest_rows(sym, rows, CFG)
        per_coin[sym] = trades
        all_trades.extend(trades)
        total_days = max(total_days, span)
        print(f"{sym}: {len(rows)} candles, {len(trades)} trades")
    all_trades.sort(key=lambda t: t["exit_ts"])

    print("\n" + "=" * 78)
    print(line("ALL COINS", stats(all_trades)))
    if all_trades and total_days > 0:
        print(f"Signals per day (all coins): {len(all_trades) / total_days:.2f} "
              f"| tested period ~{total_days:.0f} days")
        st = stats(all_trades)
        print(f"Gross avgR before costs: "
              f"{st['gross'] / st['n']:+.2f} | longest losing streak: "
              f"{st['streak']}")
    print("-" * 78)
    for sym, tr in per_coin.items():
        print(line(sym, stats(tr)))
    print("-" * 78)
    for g in ("A", "B", "C"):
        print(line(f"Grade {g}", stats([t for t in all_trades if t["grade"] == g])))
    for side in ("BUY", "SELL"):
        print(line(side, stats([t for t in all_trades if t["side"] == side])))
    for setup in ("Pullback", "Breakout", "Retest"):
        print(line(setup, stats([t for t in all_trades if t["setup"] == setup])))
    print("=" * 78)

    if all_trades:
        path = "backtest_trades.csv"
        keys = ["symbol", "side", "setup", "grade", "score", "entry_ts",
                "entry", "sl", "tp1", "tp2", "exit_ts", "reason",
                "gross_R", "net_R", "hold"]
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(all_trades)
        print(f"Trades saved to {path}")
    print("Note: a backtest is history, not a promise. Test other periods and "
          "do not over-tune the settings to one period.")


# ===========================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--min-score", type=int)
    ap.add_argument("--coins", type=str)
    ap.add_argument("--no-discord", action="store_true")
    args = ap.parse_args()
    if args.min_score:
        CFG["MIN_SCORE"] = args.min_score
    coins = [c.strip().upper() for c in args.coins.split(",")] if args.coins else COINS
    if args.backtest:
        run_backtest(coins, args.days)
    else:
        run_live(coins, send=not args.no_discord)


if __name__ == "__main__":
    main()

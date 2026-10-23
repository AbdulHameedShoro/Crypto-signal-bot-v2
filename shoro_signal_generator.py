#!/usr/bin/env python3
"""SHORO Bitget Balanced Signal Generator — closed-candle USDT perpetuals.

Uses weighted confluence and strong gates to reduce noise.
Sends BUY/SELL signals to the existing SHORO Trade Manager Worker; never places orders.
Requires: requests
GitHub Actions secret: SHORO_SIGNAL_SECRET
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

API = "https://api.bitget.com"
WORKER_URL = "https://shoro-bitget-trade-manager.ahshoro.workers.dev/shoro-signal"
STATE_FILE = Path("shoro_signal_state.json")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ZENUSDT", "ZECUSDT"]
PRODUCT_TYPE = "USDT-FUTURES"
TIMEFRAMES = {"15m": 900, "1H": 3600, "4H": 14400}
CANDLE_LIMIT = 120

# More selective defaults: fewer but higher-quality trades.
MIN_TRADE_SCORE = int(os.getenv("SHORO_MIN_TRADE_SCORE", "72"))
MIN_SCORE_GAP = int(os.getenv("SHORO_MIN_SCORE_GAP", "12"))
MIN_VOLUME_RATIO = 1.00
EXTENSION_ATR_LIMIT = 1.75
ATR_STOP_MULTIPLIER = 1.5

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "SHORO-Balanced-Signal-Generator/2.1", "Accept": "application/json"})


def fetch_closed_candles(symbol: str, timeframe: str = "15m") -> list[dict[str, float | int]]:
    params = {"symbol": symbol, "productType": PRODUCT_TYPE, "granularity": timeframe, "limit": str(CANDLE_LIMIT)}
    response = SESSION.get(f"{API}/api/v2/mix/market/candles", params=params, timeout=20)
    response.raise_for_status()
    body = response.json()
    if body.get("code") != "00000":
        raise RuntimeError(f"Bitget API error for {symbol} {timeframe}: {body.get('msg', body)}")
    rows = body.get("data") or []
    seconds = TIMEFRAMES[timeframe]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    candles = []
    for row in rows:
        if len(row) < 6:
            continue
        ts = int(float(row[0]))
        if ts + seconds * 1000 > now_ms:
            continue
        candles.append({
            "timestamp": ts,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        })
    candles.sort(key=lambda c: int(c["timestamp"]))
    if len(candles) < 70:
        raise RuntimeError(f"Insufficient closed {timeframe} candles for {symbol}: {len(candles)}")
    return candles


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


def rsi(values: list[float], period: int = 14) -> list[float]:
    out = [50.0] * len(values)
    if len(values) <= period:
        return out
    gains, losses = [], []
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def calc(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        if gain == 0:
            return 0.0
        return 100.0 - 100.0 / (1.0 + gain / loss)

    out[period] = calc(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out[i] = calc(avg_gain, avg_loss)
    return out


def indicators(candles: list[dict[str, float | int]]) -> dict[str, list[float]]:
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    volumes = [float(c["volume"]) for c in candles]
    true_ranges, plus_dm, minus_dm = [], [], []
    for i in range(len(candles)):
        if i == 0:
            true_ranges.append(highs[i] - lows[i])
            plus_dm.append(0.0)
            minus_dm.append(0.0)
            continue
        true_ranges.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    atr = ema(true_ranges, 14)
    sm_plus, sm_minus = ema(plus_dm, 14), ema(minus_dm, 14)
    plus_di, minus_di, dx = [], [], []
    for i in range(len(candles)):
        pdi = 100.0 * sm_plus[i] / atr[i] if atr[i] > 0 else 0.0
        mdi = 100.0 * sm_minus[i] / atr[i] if atr[i] > 0 else 0.0
        plus_di.append(pdi)
        minus_di.append(mdi)
        denom = pdi + mdi
        dx.append(100.0 * abs(pdi - mdi) / denom if denom else 0.0)
    macd = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    macd_signal = ema(macd, 9)
    return {
        "ema20": ema(closes, 20),
        "ema50": ema(closes, 50),
        "rsi": rsi(closes),
        "atr": atr,
        "adx": ema(dx, 14),
        "plus_di": plus_di,
        "minus_di": minus_di,
        "macd_hist": [a - b for a, b in zip(macd, macd_signal)],
        "volumes": volumes,
    }


def higher_trend(candles: list[dict[str, float | int]]) -> str:
    ind = indicators(candles)
    i = len(candles) - 1
    close = float(candles[i]["close"])
    if close > ind["ema50"][i] and ind["ema20"][i] > ind["ema50"][i]:
        return "BULLISH"
    if close < ind["ema50"][i] and ind["ema20"][i] < ind["ema50"][i]:
        return "BEARISH"
    return "NEUTRAL"


def evaluate(symbol: str, candles: list[dict[str, float | int]], trend_1h: str, trend_4h: str) -> dict[str, Any] | None:
    ind = indicators(candles)
    i = len(candles) - 1
    c, prev = candles[i], candles[i - 1]
    close, atr = float(c["close"]), ind["atr"][i]
    if not (math.isfinite(atr) and atr > 0 and close > 0):
        raise ValueError("Invalid ATR/price")

    rsi_value, adx = ind["rsi"][i], ind["adx"][i]
    avg_vols = ind["volumes"][max(0, i - 20):i]
    avg_volume = sum(avg_vols) / len(avg_vols) if avg_vols else 0.0
    vol_ratio = ind["volumes"][i] / avg_volume if avg_volume > 0 else 0.0
    recent_high = max(float(x["high"]) for x in candles[max(0, i - 20):i])
    recent_low = min(float(x["low"]) for x in candles[max(0, i - 20):i])
    candle_range = max(float(c["high"]) - float(c["low"]), 1e-12)
    body_ratio = abs(close - float(c["open"])) / candle_range
    close_position = (close - float(c["low"])) / candle_range
    price_ema_gap = abs(close - ind["ema20"][i]) / atr
    results = {}

    for side in ("BUY", "SELL"):
        bullish = side == "BUY"
        score, reasons = 0, []

        def add(points: int, reason: str) -> None:
            nonlocal score
            score += points
            reasons.append(f"+{points} {reason}")

        if (trend_1h == "BULLISH") == bullish:
            add(12, "1H trend agrees")
        else:
            add(-12, "1H trend conflicts")

        if (trend_4h == "BULLISH") == bullish:
            add(8, "4H trend agrees")
        else:
            add(-10, "4H trend conflicts")

        if (ind["ema20"][i] > ind["ema50"][i]) == bullish:
            add(15, "EMA20/50 trend")
        else:
            add(-15, "EMA20/50 conflicts")

        if (ind["macd_hist"][i] > 0) == bullish:
            add(10, "MACD supports")
        else:
            add(-10, "MACD conflicts")

        if bullish:
            if 48 <= rsi_value <= 70:
                add(12, f"bullish RSI ({rsi_value:.1f})")
            else:
                add(-8, f"RSI not good ({rsi_value:.1f})")
        else:
            if 30 <= rsi_value <= 52:
                add(12, f"bearish RSI ({rsi_value:.1f})")
            else:
                add(-8, f"RSI not good ({rsi_value:.1f})")

        directional_candle = close > float(c["open"]) if bullish else close < float(c["open"])
        strong_close = close_position >= 0.65 if bullish else close_position <= 0.35
        if directional_candle and body_ratio >= 0.45 and strong_close:
            add(12, "strong close")
        else:
            add(-10, "weak close")

        if vol_ratio >= 1.30:
            add(10, f"strong volume {vol_ratio:.2f}x")
        elif vol_ratio >= 1.00:
            add(6, f"adequate volume {vol_ratio:.2f}x")
        else:
            add(-12, f"weak volume {vol_ratio:.2f}x")

        if adx >= 22:
            add(8, f"ADX {adx:.1f}")
        elif adx >= 15:
            add(3, f"moderate ADX {adx:.1f}")
        else:
            add(-12, f"ADX too weak {adx:.1f}")

        di_agrees = ind["plus_di"][i] >= ind["minus_di"][i] if bullish else ind["minus_di"][i] >= ind["plus_di"][i]
        if di_agrees:
            add(6, "DI direction agrees")
        else:
            add(-8, "DI direction conflicts")

        if price_ema_gap > EXTENSION_ATR_LIMIT:
            add(-15, f"too far from EMA20 ({price_ema_gap:.2f} ATR)")
        elif price_ema_gap > 1.25:
            add(-8, f"slightly extended ({price_ema_gap:.2f} ATR)")

        if bullish and close > recent_high:
            add(8, "breaks recent high")
        elif not bullish and close < recent_low:
            add(8, "breaks recent low")
        else:
            add(-8, "not strong structure break")

        blockers = []
        if vol_ratio < 0.80:
            blockers.append("volume too weak")
        if price_ema_gap > EXTENSION_ATR_LIMIT:
            blockers.append("price too extended")
        if trend_1h == ("BEARISH" if bullish else "BULLISH") and trend_4h == ("BEARISH" if bullish else "BULLISH"):
            blockers.append("macro trend opposes signal")

        results[side] = {"score": max(score, 0), "reasons": reasons, "blockers": blockers}

    side = max(results, key=lambda s: results[s]["score"])
    best, other = results[side], results["SELL" if side == "BUY" else "BUY"]

    if best["score"] < MIN_TRADE_SCORE:
        print(f"{symbol}: no trade | BUY={results['BUY']['score']} SELL={results['SELL']['score']} "
              f"RSI={rsi_value:.1f} ADX={adx:.1f} vol={vol_ratio:.2f}x blockers={best['blockers']}")
        return None

    if best["score"] - other["score"] < MIN_SCORE_GAP:
        print(f"{symbol}: not enough score gap | BUY={results['BUY']['score']} SELL={results['SELL']['score']}")
        return None

    if best["blockers"]:
        print(f"{symbol}: blocked | blockers={best['blockers']}")
        return None

    direction = 1 if side == "BUY" else -1
    risk = ATR_STOP_MULTIPLIER * atr
    stop_loss = close - direction * risk
    return {
        "pair": symbol,
        "side": side,
        "entry": round(close, 10),
        "stop_loss": round(stop_loss, 10),
        "tp1": round(close + direction * risk, 10),
        "tp2": round(close + direction * 2 * risk, 10),
        "tp3": round(close + direction * 3 * risk, 10),
        "_candle_timestamp": int(c["timestamp"]),
        "_score": best["score"],
        "_opposite_score": other["score"],
        "_rsi": round(rsi_value, 2),
        "_adx": round(adx, 2),
        "_volume_ratio": round(vol_ratio, 2),
        "_trend_1h": trend_1h,
        "_trend_4h": trend_4h,
        "_reasons": best["reasons"],
    }


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"sent": {}}
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"sent": {}}
    except (OSError, json.JSONDecodeError):
        return {"sent": {}}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    secret = os.environ.get("SHORO_SIGNAL_SECRET", "").strip()
    if not secret:
        raise SystemExit("Missing GitHub Actions secret: SHORO_SIGNAL_SECRET")

    state = load_state()
    sent = state.setdefault("sent", {})
    sent_count = 0

    for symbol in SYMBOLS:
        try:
            base = fetch_closed_candles(symbol, "15m")
            trends = {}
            for timeframe in ("1H", "4H"):
                try:
                    trends[timeframe] = higher_trend(fetch_closed_candles(symbol, timeframe))
                except (requests.RequestException, RuntimeError, ValueError, KeyError, IndexError) as exc:
                    print(f"{symbol}: {timeframe} trend unavailable ({exc}); continue with 15m data")
                    trends[timeframe] = "UNKNOWN"
            signal = evaluate(symbol, base, trends["1H"], trends["4H"])
            if not signal:
                continue

            candle_ts = int(signal.pop("_candle_timestamp"))
            key = f"{symbol}:{candle_ts}:{signal['side']}"
            if key in sent:
                print(f"{symbol}: already sent for closed candle {candle_ts}")
                continue

            details = {k: signal.pop(k) for k in ("_score", "_opposite_score", "_rsi", "_adx", "_volume_ratio", "_trend_1h", "_trend_4h", "_reasons")}
            response = SESSION.post(
                WORKER_URL,
                json=signal,
                headers={"X-SHORO-SIGNAL-SECRET": secret, "Content-Type": "application/json"},
                timeout=25,
            )
            print(f"{symbol}: Worker HTTP {response.status_code}; details={details}; body={response.text[:500]}")
            response.raise_for_status()
            result = response.json()
            if result.get("ok") is not True or result.get("discordStatus") != 200:
                raise RuntimeError(f"Worker did not confirm Discord delivery for {symbol}: {result}")

            sent[key] = {
                "sent_at": datetime.now(timezone.utc).isoformat(),
                "pair": symbol,
                "side": signal["side"],
                "candle_timestamp": candle_ts,
                "trade_id": result.get("trade_id"),
                "score": details["_score"],
            }
            sent_count += 1
        except Exception as exc:
            print(f"ERROR {symbol}: {exc}")

    if len(sent) > 1000:
        newest = sorted(sent.items(), key=lambda item: item[1].get("candle_timestamp", 0))[-500:]
        state["sent"] = dict(newest)

    save_state(state)
    print(f"Completed. New signals delivered: {sent_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

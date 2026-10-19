#!/usr/bin/env python3
"""SHORO Bitget 15m signal generator. Sends signals to the existing SHORO Worker."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

BASE_URL = "https://api.bitget.com"
WORKER_URL = "https://shoro-bitget-trade-manager.ahshoro.workers.dev/shoro-signal"
STATE_FILE = Path("shoro_signal_state.json")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ZENUSDT", "ZECUSDT"]
PRODUCT_TYPE = "USDT-FUTURES"
GRANULARITY = "15m"
CANDLE_LIMIT = 100

# Initial, intentionally moderate filters; adjust these values here later if needed.
MIN_ADX = 18.0
MIN_VOLUME_RATIO = 0.70
ATR_STOP_MULTIPLIER = 1.5
RSI_BUY_MIN, RSI_BUY_MAX = 50.0, 70.0
RSI_SELL_MIN, RSI_SELL_MAX = 30.0, 50.0

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "SHORO-Signal-Generator/1.0", "Accept": "application/json"})


def fetch_closed_candles(symbol: str) -> list[dict[str, float | int]]:
    params = {
        "symbol": symbol,
        "productType": PRODUCT_TYPE,
        "granularity": GRANULARITY,
        "limit": str(CANDLE_LIMIT),
    }
    response = SESSION.get(f"{BASE_URL}/api/v2/mix/market/candles", params=params, timeout=20)
    response.raise_for_status()
    body = response.json()
    if body.get("code") != "00000":
        raise RuntimeError(f"Bitget API error for {symbol}: {body.get('msg', body)}")
    rows = body.get("data") or []
    candles = []
    current_bucket_ms = (int(time.time() * 1000) // 900_000) * 900_000
    for row in rows:
        if len(row) < 6:
            continue
        ts = int(float(row[0]))
        # Exclude the still-forming 15-minute candle; only use fully closed candles.
        if ts >= current_bucket_ms:
            continue
        candles.append({
            "timestamp": ts,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        })
    candles.sort(key=lambda item: int(item["timestamp"]))
    if len(candles) < 60:
        raise RuntimeError(f"Not enough closed candles for {symbol}: {len(candles)}")
    return candles


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def rsi(values: list[float], period: int = 14) -> list[float]:
    out = [50.0] * len(values)
    if len(values) <= period:
        return out
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    def value(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / l)
    out[period] = value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out[i] = value(avg_gain, avg_loss)
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
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    atr = ema(true_ranges, 14)
    sm_plus = ema(plus_dm, 14)
    sm_minus = ema(minus_dm, 14)
    plus_di, minus_di, dx = [], [], []
    for i in range(len(candles)):
        if atr[i] <= 0:
            pdi = mdi = 0.0
        else:
            pdi = 100.0 * sm_plus[i] / atr[i]
            mdi = 100.0 * sm_minus[i] / atr[i]
        plus_di.append(pdi)
        minus_di.append(mdi)
        denom = pdi + mdi
        dx.append(100.0 * abs(pdi - mdi) / denom if denom else 0.0)
    return {
        "ema20": ema(closes, 20),
        "ema50": ema(closes, 50),
        "rsi": rsi(closes, 14),
        "atr": atr,
        "adx": ema(dx, 14),
        "plus_di": plus_di,
        "minus_di": minus_di,
        "volumes": volumes,
    }


def create_signal(symbol: str, candles: list[dict[str, float | int]]) -> dict[str, Any] | None:
    ind = indicators(candles)
    i = len(candles) - 1
    prev = candles[i - 1]
    last = candles[i]
    close = float(last["close"])
    atr = ind["atr"][i]
    adx = ind["adx"][i]
    rsi_value = ind["rsi"][i]
    avg_volume = sum(ind["volumes"][max(0, i - 20):i]) / max(1, len(ind["volumes"][max(0, i - 20):i]))
    volume_ratio = ind["volumes"][i] / avg_volume if avg_volume > 0 else 0.0
    if atr <= 0 or adx < MIN_ADX or volume_ratio < MIN_VOLUME_RATIO:
        return None

    side = None
    if (ind["ema20"][i] > ind["ema50"][i] and close > ind["ema20"][i]
            and close > float(prev["high"]) and ind["plus_di"][i] >= ind["minus_di"][i]
            and RSI_BUY_MIN <= rsi_value <= RSI_BUY_MAX):
        side = "BUY"
    elif (ind["ema20"][i] < ind["ema50"][i] and close < ind["ema20"][i]
            and close < float(prev["low"]) and ind["minus_di"][i] >= ind["plus_di"][i]
            and RSI_SELL_MIN <= rsi_value <= RSI_SELL_MAX):
        side = "SELL"
    if not side:
        return None

    stop_distance = ATR_STOP_MULTIPLIER * atr
    if side == "BUY":
        stop_loss = close - stop_distance
        risk = close - stop_loss
        targets = [close + risk, close + 2 * risk, close + 3 * risk]
    else:
        stop_loss = close + stop_distance
        risk = stop_loss - close
        targets = [close - risk, close - 2 * risk, close - 3 * risk]

    # Stable per-symbol/per-closed-candle ID prevents repeated signals on reruns.
    return {
        "pair": symbol,
        "side": side,
        "entry": round(close, 10),
        "stop_loss": round(stop_loss, 10),
        "tp1": round(targets[0], 10),
        "tp2": round(targets[1], 10),
        "tp3": round(targets[2], 10),
        "_candle_timestamp": int(last["timestamp"]),
        "_rsi": round(rsi_value, 2),
        "_adx": round(adx, 2),
        "_volume_ratio": round(volume_ratio, 2),
    }


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"sent": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
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
            candles = fetch_closed_candles(symbol)
            signal = create_signal(symbol, candles)
            if not signal:
                print(f"{symbol}: no confirmed setup")
                continue
            candle_ts = int(signal.pop("_candle_timestamp"))
            key = f"{symbol}:{candle_ts}:{signal['side']}"
            if key in sent:
                print(f"{symbol}: already sent for closed candle {candle_ts}")
                continue
            details = {k: signal.pop(k) for k in ("_rsi", "_adx", "_volume_ratio")}
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
            }
            sent_count += 1
        except Exception as exc:
            print(f"ERROR {symbol}: {exc}")
    # Keep recent records only, avoiding an indefinitely growing state file.
    if len(sent) > 1000:
        newest = sorted(sent.items(), key=lambda item: item[1].get("candle_timestamp", 0))[-500:]
        state["sent"] = dict(newest)
    save_state(state)
    print(f"Completed. New signals delivered: {sent_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

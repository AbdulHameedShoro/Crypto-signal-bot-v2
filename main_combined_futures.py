# -*- coding: utf-8 -*-
import os
import math
import time
import requests

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

COINS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "DOGEUSDT", "ZENUSDT", "ZECUSDT",
    "CKBUSDT",
]

# Public USDT perpetual market data from Bitget.
BITGET_URL = "https://api.bitget.com"
TIMEFRAME = "15m"
CANDLE_LIMIT = 200
TIMEOUT = 12

# Signal labels (single source of truth, avoids emoji mismatches).
SIG_BUY = "ðŸŸ¢ BUY"
SIG_SELL = "ðŸ”´ SELL"
SIG_WAIT = "ðŸŸ¡ WAIT"

# Balanced settings: not as strict as the original filter, but still confirmed.
MIN_SIGNAL_SCORE = 55
PAPER_SIGNAL_SCORE = 55
STOP_ATR_MULTIPLIER = 1.5
TP1_RR = 1.5
TP2_RR = 2.5
STRUCTURE_LOOKBACK = 20
STRUCTURE_BUFFER_ATR = 0.15
MAX_ENTRY_DISTANCE_ATR = 1.8
STRONG_VOLUME_RATIO = 1.10
WEAK_VOLUME_RATIO = 0.60
MIN_QUALITY_SCORE = 45

# ---------------------------------------------------------------------------
# Relaxed fallback mode.
# Only evaluated when the ORIGINAL (standard) rules give no BUY/SELL, so every
# signal the old bot produced is still produced identically.
# Set RELAXED_MODE = False to restore the exact old behaviour.
# ---------------------------------------------------------------------------
RELAXED_MODE = True
RELAXED_MIN_SCORE = 50
RELAXED_SCORE_GAP = 10   # winning side must beat the other side by this much


def get_klines(symbol):
    """Fetch Bitget USDT-Futures candles in chronological order."""

    response = requests.get(
        f"{BITGET_URL}/api/v2/mix/market/candles",
        params={
            "symbol": symbol,
            "productType": "USDT-FUTURES",
            "granularity": "15m",
            "limit": str(CANDLE_LIMIT),
        },
        headers={"Accept": "application/json"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError(
            f"Bitget returned invalid JSON for {symbol}: "
            f"{response.text[:200]}"
        ) from exc

    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected Bitget response for {symbol}")

    if payload.get("code") != "00000":
        raise ValueError(
            f"Bitget API error for {symbol}: "
            f"{payload.get('code')} | {payload.get('msg')}"
        )

    rows = payload.get("data")

    if not isinstance(rows, list) or len(rows) < 60:
        raise ValueError(f"Not enough Bitget Futures candles for {symbol}")

    if not all(
        isinstance(candle, (list, tuple)) and len(candle) >= 6
        for candle in rows
    ):
        raise ValueError(f"Invalid Bitget candle format for {symbol}")

    try:
        rows.sort(key=lambda candle: int(candle[0]))
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError(
            f"Invalid Bitget candle timestamps for {symbol}"
        ) from exc

    return rows


def get_higher_timeframe_trend(symbol, granularity, duration_ms):
    """Get trend from fully closed 1H or 4H Bitget candles."""
    try:
        response = requests.get(
            f"{BITGET_URL}/api/v2/mix/market/candles",
            params={
                "symbol": symbol,
                "productType": "USDT-FUTURES",
                "granularity": granularity,
                "limit": "100",
            },
            headers={"Accept": "application/json"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()

        if not isinstance(payload, dict) or payload.get("code") != "00000":
            return "UNKNOWN"

        rows = payload.get("data")
        if not isinstance(rows, list):
            return "UNKNOWN"

        now_ms = int(time.time() * 1000)
        closed = [
            candle for candle in rows
            if isinstance(candle, (list, tuple))
            and len(candle) >= 6
            and int(candle[0]) + duration_ms <= now_ms
        ]

        closed.sort(key=lambda candle: int(candle[0]))

        if len(closed) < 55:
            return "UNKNOWN"

        closes = [float(candle[4]) for candle in closed]
        price = closes[-1]
        ema20 = calculate_ema(closes, 20)
        ema50 = calculate_ema(closes, 50)

        if price > ema20 > ema50:
            return "BULLISH"
        if price < ema20 < ema50:
            return "BEARISH"
        return "NEUTRAL"

    except (requests.RequestException, ValueError, TypeError, KeyError) as error:
        print(f"Higher timeframe error {symbol} {granularity}: {error}")
        return "UNKNOWN"


def calculate_ema(values, period):
    if not values:
        return 0.0
    if len(values) < period:
        return values[-1]
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for price in values[period:]:
        ema = (price - ema) * k + ema
    return ema


def calculate_rsi(values, period=14):
    if len(values) <= period:
        return 50.0

    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_atr(highs, lows, closes, period=14):
    if len(closes) <= period:
        return 0.0

    trs = []
    for i in range(1, len(closes)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))

    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def calculate_adx(highs, lows, closes, period=14):
    """Return ADX, +DI and -DI using Wilder smoothing."""
    if len(closes) < period * 2 + 1:
        return 0.0, 0.0, 0.0

    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

    if len(trs) < period * 2:
        return 0.0, 0.0, 0.0

    atr_sum = sum(trs[:period])
    plus_sum = sum(plus_dm[:period])
    minus_sum = sum(minus_dm[:period])
    dxs = []
    pdi = mdi = 0.0

    for i in range(period - 1, len(trs)):
        if i >= period:
            atr_sum = atr_sum - atr_sum / period + trs[i]
            plus_sum = plus_sum - plus_sum / period + plus_dm[i]
            minus_sum = minus_sum - minus_sum / period + minus_dm[i]

        if atr_sum <= 0:
            dxs.append(0.0)
            continue

        pdi = plus_sum / atr_sum * 100
        mdi = minus_sum / atr_sum * 100
        total = pdi + mdi
        dxs.append(abs(pdi - mdi) / total * 100 if total > 0 else 0.0)

    if len(dxs) < period:
        return 0.0, pdi, mdi

    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = ((adx * (period - 1)) + dx) / period

    return adx, pdi, mdi


def scan_candle_patterns(opens, highs, lows, closes):
    if len(closes) < 3:
        return False, False

    o1, h1, l1, c1 = opens[-2], highs[-2], lows[-2], closes[-2]
    o2, h2, l2, c2 = opens[-1], highs[-1], lows[-1], closes[-1]

    body1 = abs(c1 - o1)
    body2 = abs(c2 - o2)

    range2 = max(h2 - l2, 1e-12)

    lower_wick2 = min(o2, c2) - l2
    upper_wick2 = h2 - max(o2, c2)
    close_position2 = (c2 - l2) / range2

    # Engulfing: current candle covers the previous real body.
    bull_engulfing = (
        c1 < o1 and c2 > o2
        and o2 <= c1 and c2 >= o1
        and body2 >= body1 * 0.90
    )
    bear_engulfing = (
        c1 > o1 and c2 < o2
        and o2 >= c1 and c2 <= o1
        and body2 >= body1 * 0.90
    )

    # Hammer-like bullish rejection.
    bull_hammer = (
        lower_wick2 >= max(body2 * 2.0, range2 * 0.45)
        and upper_wick2 <= range2 * 0.25
        and close_position2 >= 0.65
        and c2 > l2 + range2 * 0.65
    )

    # Shooting-star-like bearish rejection.
    bear_star = (
        upper_wick2 >= max(body2 * 2.0, range2 * 0.45)
        and lower_wick2 <= range2 * 0.25
        and close_position2 <= 0.35
        and c2 < h2 - range2 * 0.65
    )

    # Strong directional candle closing near its extreme.
    bull_strong = (
        c2 > o2
        and body2 / range2 >= 0.60
        and close_position2 >= 0.80
    )
    bear_strong = (
        c2 < o2
        and body2 / range2 >= 0.60
        and close_position2 <= 0.20
    )

    # Three-candle Morning Star and Evening Star patterns.
    o0, c0 = opens[-3], closes[-3]
    body0 = abs(c0 - o0)
    range0 = max(highs[-3] - lows[-3], 1e-12)

    morning_star = (
        c0 < o0
        and body0 / range0 >= 0.45
        and body1 <= body0 * 0.60
        and c2 > o2
        and body2 >= body0 * 0.40
        and c2 >= (o0 + c0) / 2
    )
    evening_star = (
        c0 > o0
        and body0 / range0 >= 0.45
        and body1 <= body0 * 0.60
        and c2 < o2
        and body2 >= body0 * 0.40
        and c2 <= (o0 + c0) / 2
    )

    bullish = bull_engulfing or bull_hammer or bull_strong or morning_star
    bearish = bear_engulfing or bear_star or bear_strong or evening_star

    return bullish, bearish


def get_previous_day_levels(candles):
    now_ms = int(time.time() * 1000)
    day_ms = 24 * 60 * 60 * 1000

    today_start = (now_ms // day_ms) * day_ms
    yesterday_start = today_start - day_ms

    previous_day = [
        candle for candle in candles
        if yesterday_start <= int(candle[0]) < today_start
        and int(candle[0]) + 15 * 60 * 1000 <= now_ms
    ]

    if not previous_day:
        return None, None

    previous_high = max(float(c[2]) for c in previous_day)
    previous_low = min(float(c[3]) for c in previous_day)

    return previous_high, previous_low


def get_support_resistance(highs, lows, lookback=20):
    window = min(lookback, len(highs) - 1)
    if window < 1:
        return lows[-1], highs[-1]
    return min(lows[-window - 1:-1]), max(highs[-window - 1:-1])


def nearest_support(lows, price, atr, lookback=30):
    """Return the nearest confirmed local swing low below price."""
    start = max(1, len(lows) - lookback - 1)
    levels = []
    for i in range(start, len(lows) - 1):
        if lows[i] < price and lows[i] <= lows[i - 1] and lows[i] <= lows[i + 1]:
            levels.append(lows[i])
    return max(levels) if levels else None


def nearest_resistance(highs, price, atr, lookback=30):
    """Return the nearest confirmed local swing high above price."""
    start = max(1, len(highs) - lookback - 1)
    levels = []
    for i in range(start, len(highs) - 1):
        if highs[i] > price and highs[i] >= highs[i - 1] and highs[i] >= highs[i + 1]:
            levels.append(highs[i])
    return min(levels) if levels else None


def quality_grade(score):
    if score >= 90:
        return "A+"
    if score >= 80:
        return "A"
    if score >= 70:
        return "B"
    return "C"


def get_signal(symbol):
    candles = get_klines(symbol)
    previous_day_high, previous_day_low = get_previous_day_levels(candles)

    # Keep only fully closed 15-minute Bitget candles.
    now_ms = int(time.time() * 1000)
    candle_duration_ms = 15 * 60 * 1000

    closed = [
        candle for candle in candles
        if int(candle[0]) + candle_duration_ms <= now_ms
    ]

    if len(closed) < 60:
        return None

    opens = [float(c[1]) for c in closed]
    highs = [float(c[2]) for c in closed]
    lows = [float(c[3]) for c in closed]
    closes = [float(c[4]) for c in closed]

    bull_pattern, bear_pattern = scan_candle_patterns(
        opens, highs, lows, closes
    )

    # Give candle patterns more importance near market structure.
    recent_support, recent_resistance = get_support_resistance(
        highs, lows, STRUCTURE_LOOKBACK
    )

    current_price = closes[-1]
    pattern_atr = calculate_atr(highs, lows, closes)

    near_support = (
        pattern_atr > 0
        and current_price - recent_support <= pattern_atr * 0.50
        and current_price >= recent_support
    )
    near_resistance = (
        pattern_atr > 0
        and recent_resistance - current_price <= pattern_atr * 0.50
        and current_price <= recent_resistance
    )

    bull_pattern_confirmed = bull_pattern and near_support
    bear_pattern_confirmed = bear_pattern and near_resistance

    volumes = [float(c[5]) for c in closed]
    price = closes[-1]

    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    rsi = calculate_rsi(closes)

    trend_1h = get_higher_timeframe_trend(symbol, "1H", 60 * 60 * 1000)
    trend_4h = get_higher_timeframe_trend(symbol, "4H", 4 * 60 * 60 * 1000)

    atr = calculate_atr(highs, lows, closes)
    adx, pdi, mdi = calculate_adx(highs, lows, closes)

    di_bull = pdi > mdi
    di_bear = mdi > pdi

    ema20_prev = calculate_ema(closes[:-3], 20)
    ema50_prev = calculate_ema(closes[:-3], 50)
    ema20_up = ema20 > ema20_prev
    ema20_down = ema20 < ema20_prev
    ema50_up = ema50 > ema50_prev
    ema50_down = ema50 < ema50_prev

    bull_momentum = price > closes[-4]
    bear_momentum = price < closes[-4]

    avg_vol = sum(volumes[-21:-1]) / 20
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0.0
    vol_strong = vol_ratio >= STRONG_VOLUME_RATIO
    vol_weak = vol_ratio < WEAK_VOLUME_RATIO

    candle_age_minutes = (
        now_ms - (int(closed[-1][0]) + candle_duration_ms)
    ) / 60000

    print(
        f"VOLUME_CHECK {symbol} | "
        f"LAST_VOLUME={volumes[-1]:.4f} | "
        f"AVG_20_VOLUME={avg_vol:.4f} | "
        f"RATIO={vol_ratio:.3f} | "
        f"CANDLE_AGE_MIN={candle_age_minutes:.1f} | "
        f"CLOSED_CANDLES={len(closed)}"
    )

    support, resistance = get_support_resistance(
        highs, lows, STRUCTURE_LOOKBACK
    )
    ns = nearest_support(lows, price, atr)
    nr = nearest_resistance(highs, price, atr)

    bull_ema = price > ema20 and ema20 > ema50 and ema20_up
    bear_ema = price < ema20 and ema20 < ema50 and ema20_down

    # Breakout level: previous N candles, excluding the signal candle.
    prev_res = max(highs[-STRUCTURE_LOOKBACK - 1:-1])
    prev_sup = min(lows[-STRUCTURE_LOOKBACK - 1:-1])
    bull_break = price > prev_res and closes[-2] <= prev_res
    bear_break = price < prev_sup and closes[-2] >= prev_sup

    # Retest must use a fixed level from before the breakout candle.
    retest_res = max(highs[-STRUCTURE_LOOKBACK - 2:-2])
    retest_sup = min(lows[-STRUCTURE_LOOKBACK - 2:-2])
    bull_retest = (
        closes[-2] > retest_res and lows[-1] <= retest_res and price > retest_res
    )
    bear_retest = (
        closes[-2] < retest_sup and highs[-1] >= retest_sup and price < retest_sup
    )
    bull_structure = bull_break or bull_retest
    bear_structure = bear_break or bear_retest

    dist_ema = abs(price - ema20) / atr if atr > 0 else 0.0

    # Standard distance limit is 1.8 ATR; allow 2.2 ATR only for strong,
    # confirmed breakouts.
    strong_buy_breakout = (
        bull_structure
        and price > ema20
        and ema20_up
        and di_bull
        and adx >= 20
        and vol_ratio >= 1.50
        and trend_1h == "BULLISH"
        and closes[-1] > opens[-1]
    )
    strong_sell_breakout = (
        bear_structure
        and price < ema20
        and ema20_down
        and di_bear
        and adx >= 20
        and vol_ratio >= 1.50
        and trend_1h == "BEARISH"
        and closes[-1] < opens[-1]
    )

    buy_distance_limit = 2.2 if strong_buy_breakout else MAX_ENTRY_DISTANCE_ATR
    sell_distance_limit = 2.2 if strong_sell_breakout else MAX_ENTRY_DISTANCE_ATR

    late_buy = price > ema20 and dist_ema > buy_distance_limit
    late_sell = price < ema20 and dist_ema > sell_distance_limit

    # Strong momentum approaching support/resistance (watch only).
    breakout_proximity = 0.35

    buy_breakout_watch = (
        atr > 0
        and 0 <= resistance - price <= atr * breakout_proximity
        and vol_ratio >= 1.50
        and price > ema20
        and ema20_up
        and di_bull
        and 50 <= rsi <= 75
        and bull_momentum
        and not late_buy
    )
    sell_breakdown_watch = (
        atr > 0
        and 0 <= price - support <= atr * breakout_proximity
        and vol_ratio >= 1.50
        and price < ema20
        and ema20_down
        and di_bear
        and 25 <= rsi <= 50
        and bear_momentum
        and not late_sell
    )

    bull_rsi = 51 <= rsi <= 69
    bear_rsi = 31 <= rsi <= 49

    bull_rsi_continuation = (
        rsi > 69 and adx >= 28 and di_bull and vol_strong
        and bull_ema and bull_momentum and not late_buy
    )
    bear_rsi_continuation = (
        rsi < 31 and adx >= 28 and di_bear and vol_strong
        and bear_ema and bear_momentum and not late_sell
    )
    bull_rsi_ok = bull_rsi or bull_rsi_continuation
    bear_rsi_ok = bear_rsi or bear_rsi_continuation
    strong_trend = adx >= 25

    # Trade clearance: next barrier in front of the proposed entry.
    buy_obstacle = resistance if resistance > price else None
    sell_obstacle = support if support < price else None

    # Previous-day levels are additional possible barriers.
    if previous_day_high is not None and previous_day_high > price:
        buy_obstacle = (
            min(buy_obstacle, previous_day_high)
            if buy_obstacle is not None else previous_day_high
        )

    if previous_day_low is not None and previous_day_low < price:
        sell_obstacle = (
            max(sell_obstacle, previous_day_low)
            if sell_obstacle is not None else previous_day_low
        )

    one_r_price = atr * STOP_ATR_MULTIPLIER

    if buy_obstacle is None:
        buy_room_rr = math.inf
    else:
        buy_level = buy_obstacle - atr * STRUCTURE_BUFFER_ATR
        buy_room_rr = (buy_level - price) / one_r_price if one_r_price > 0 else 0.0

    if sell_obstacle is None:
        sell_room_rr = math.inf
    else:
        sell_level = sell_obstacle + atr * STRUCTURE_BUFFER_ATR
        sell_room_rr = (price - sell_level) / one_r_price if one_r_price > 0 else 0.0

    min_room_buy = 1.00
    min_room_sell = 1.00
    enough_buy = buy_room_rr >= min_room_buy
    enough_sell = sell_room_rr >= min_room_sell

    # Candle quality: confirm direction without demanding a perfect candle.
    last_open, last_close = opens[-1], closes[-1]
    last_high, last_low = highs[-1], lows[-1]
    candle_range = last_high - last_low
    candle_body = abs(last_close - last_open)
    upper_wick = last_high - max(last_open, last_close)
    lower_wick = min(last_open, last_close) - last_low

    body_ratio = candle_body / candle_range if candle_range > 0 else 0.0
    close_position = (
        (last_close - last_low) / candle_range if candle_range > 0 else 0.5
    )

    bullish_candle_confirmation = (
        last_close > last_open and body_ratio >= 0.40
        and close_position >= 0.60
    )
    bearish_candle_confirmation = (
        last_close < last_open and body_ratio >= 0.40
        and close_position <= 0.40
    )
    bullish_rejection = (
        lower_wick >= candle_body * 0.75
        and close_position >= 0.55
        and last_close >= last_open
    )
    bearish_rejection = (
        upper_wick >= candle_body * 0.75
        and close_position <= 0.45
        and last_close <= last_open
    )
    bull_candle_ok = bullish_candle_confirmation or bullish_rejection
    bear_candle_ok = bearish_candle_confirmation or bear

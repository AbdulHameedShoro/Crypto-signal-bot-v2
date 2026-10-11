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

# Public USDT perpetual market data from Bybit.
BITGET_URL = "https://api.bitget.com"
TIMEFRAME = "15m"          # Bybit interval: 15 minutes
CANDLE_LIMIT = 200
TIMEOUT = 12

# Balanced settings: not as strict as the original filter, but still confirmed.
MIN_SIGNAL_SCORE = 60
STOP_ATR_MULTIPLIER = 1.5
TP1_RR = 1.5
TP2_RR = 2.5
STRUCTURE_LOOKBACK = 20
STRUCTURE_BUFFER_ATR = 0.15
MAX_ENTRY_DISTANCE_ATR = 1.8
STRONG_VOLUME_RATIO = 1.10
WEAK_VOLUME_RATIO = 0.60
MIN_QUALITY_SCORE = 45


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
        raise ValueError(
            f"Unexpected Bitget response for {symbol}"
        )

    if payload.get("code") != "00000":
        raise ValueError(
            f"Bitget API error for {symbol}: "
            f"{payload.get('code')} | {payload.get('msg')}"
        )

    rows = payload.get("data")

    if not isinstance(rows, list) or len(rows) < 60:
        raise ValueError(
            f"Not enough Bitget Futures candles for {symbol}"
        )

    if not all(
        isinstance(candle, (list, tuple)) and len(candle) >= 6
        for candle in rows
    ):
        raise ValueError(
            f"Invalid Bitget candle format for {symbol}"
        )

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

    # Wilder's smoothed sums. The first DX is calculated from the first
    # complete period, then subsequent periods are smoothed one by one.
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

    range1 = max(h1 - l1, 1e-12)
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

    bullish = bull_engulfing or bull_hammer or bull_strong
    bearish = bear_engulfing or bear_star or bear_strong

    
    # Three-candle Morning Star and Evening Star patterns.
    o0, c0 = opens[-3], closes[-3]
    o1, c1 = opens[-2], closes[-2]
    o2, c2 = opens[-1], closes[-1]

    body0 = abs(c0 - o0)
    body1 = abs(c1 - o1)
    body2 = abs(c2 - o2)

    range0 = max(highs[-3] - lows[-3], 1e-12)

    # Morning Star: bearish candle, small middle body,
    # then a strong bullish candle closing into the first body.
    morning_star = (
        c0 < o0
        and body0 / range0 >= 0.45
        and body1 <= body0 * 0.60
        and c2 > o2
        and body2 >= body0 * 0.40
        and c2 >= (o0 + c0) / 2
    )

    # Evening Star: bullish candle, small middle body,
    # then a strong bearish candle closing into the first body.
    evening_star = (
        c0 > o0
        and body0 / range0 >= 0.45
        and body1 <= body0 * 0.60
        and c2 < o2
        and body2 >= body0 * 0.40
        and c2 <= (o0 + c0) / 2
    )

    bullish = (
        bull_engulfing or bull_hammer or bull_strong
        or morning_star
    )

    bearish = (
        bear_engulfing or bear_star or bear_strong
        or evening_star
    )

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
    # Exclude the current signal candle as a level, but allow it to confirm
    # whether the preceding candle was a local swing point.
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

    bull_candle = bull_pattern
    bear_candle = bear_pattern
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

    # Contextual confirmation: a reversal candle matters more
    # when it appears near the relevant support or resistance.
    bull_pattern_confirmed = bull_pattern and near_support
    bear_pattern_confirmed = bear_pattern and near_resistance
    # Previous day high/low breakout and retest
    daily_bull_breakout = False
    daily_bear_breakout = False
    daily_bull_retest = False
    daily_bear_retest = False

    if previous_day_high is not None and previous_day_low is not None and len(closed) >= 3:
        prev_close = closes[-2]
        last_close = closes[-1]
        last_high = highs[-1]
        last_low = lows[-1]

        daily_bull_breakout = (
            last_close > previous_day_high
            and prev_close <= previous_day_high
        )

        daily_bear_breakout = (
            last_close < previous_day_low
            and prev_close >= previous_day_low
        )

        daily_bull_retest = (
            prev_close > previous_day_high
            and last_low <= previous_day_high
            and last_close > previous_day_high
        )

        daily_bear_retest = (
            prev_close < previous_day_low
            and last_high >= previous_day_low
            and last_close < previous_day_low
        )
        
    volumes = [float(c[5]) for c in closed]
    price = closes[-1]

    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    rsi = calculate_rsi(closes)
    
    trend_1h = get_higher_timeframe_trend(
        symbol, "1H", 60 * 60 * 1000
    )
    trend_4h = get_higher_timeframe_trend(
        symbol, "4H", 4 * 60 * 60 * 1000
    )
    
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
        now_ms - (
            int(closed[-1][0]) + candle_duration_ms
        )
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
    # Using prev_res/prev_sup here would include candle -2's own high/low,
    # making close[-2] > resistance / close[-2] < support impossible.
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
    late_buy = price > ema20 and dist_ema > MAX_ENTRY_DISTANCE_ATR
    late_sell = price < ema20 and dist_ema > MAX_ENTRY_DISTANCE_ATR

    # Detect strong momentum approaching support/resistance.
    # Watch only: this does not authorize an immediate trade.
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
    
    # Slightly wider RSI band than a very strict filter, with guarded continuation.
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

    # Judge the next barrier in front of the proposed entry, not a level
    # that is already behind price. Keep at least 1.2R room to that barrier.
    
    # Use established 20-candle structure for trade clearance.
    # Small local swings remain available for diagnostics,
    # but do not automatically veto every trade.
    buy_obstacle = (
        resistance if resistance > price else None
    )
    sell_obstacle = (
        support if support < price else None
    )

    # Previous-day levels are additional possible barriers.
    if previous_day_high is not None:
        if previous_day_high > price:
            buy_obstacle = min(
                buy_obstacle, previous_day_high
            ) if buy_obstacle is not None else previous_day_high

    if previous_day_low is not None:
        if previous_day_low < price:
            sell_obstacle = max(
                sell_obstacle, previous_day_low
            ) if sell_obstacle is not None else previous_day_low
            
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

    # A breakout/retest may use a slightly smaller clearance, but still needs
    # at least 1R before a visible obstacle. Trend-continuation entries require
    # 1.2R. This balances trade quality with avoiding an all-WAIT filter.
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
    bear_candle_ok = bearish_candle_confirmation or bearish_rejection

    # Controlled early-entry path: a confirmed breakout/retest can qualify
    # before EMA20 fully crosses EMA50, but price, EMA20 slope, DI, and either
    # ADX or volume must still agree. This avoids an EMA-only WAIT bottleneck.
    bull_structure_path = (
        bull_structure and price > ema20 and ema20_up and di_bull
        and (adx >= 20 or vol_strong) and not late_buy
    )
    bear_structure_path = (
        bear_structure and price < ema20 and ema20_down and di_bear
        and (adx >= 20 or vol_strong) and not late_sell
    )

    # Main directional scores. DI adds evidence but is not an absolute gate.
    # Score components total 100 before penalties, so strong setups do not
    # saturate at 100 merely because the old weights added up to 115.
    buy = 0
    buy += 12 if price > ema20 else 0
    buy += 12 if ema20 > ema50 else 0
    buy += 8 if ema20_up else 0
    buy += 4 if ema50_up else 0
    buy += 14 if bull_rsi_ok else 0
    buy += 8 if bull_momentum else 0
    
    buy += (
        14 if adx >= 25 and di_bull
        else 7 if adx >= 20 and di_bull
        else 0
    )
    
    buy += 8 if vol_strong else 0
    buy += 6 if bull_structure else 0
    buy += 6 if enough_buy else 0
    buy += 4 if di_bull else 0
    buy += 4 if bull_candle_ok else 0
    buy += 4 if bull_pattern_confirmed else 0
    buy -= 15 if late_buy else 0
    buy -= 5 if vol_weak else 0
    buy = max(0, min(buy, 100))

    sell = 0
    sell += 12 if price < ema20 else 0
    sell += 12 if ema20 < ema50 else 0
    sell += 8 if ema20_down else 0
    sell += 4 if ema50_down else 0
    sell += 14 if bear_rsi_ok else 0
    sell += 8 if bear_momentum else 0
    
    sell += (
        14 if adx >= 25 and di_bear
        else 7 if adx >= 20 and di_bear
        else 0
    )
    
    sell += 8 if vol_strong else 0
    sell += 6 if bear_structure else 0
    sell += 6 if enough_sell else 0
    sell += 4 if di_bear else 0
    sell += 4 if bear_candle_ok else 0
    sell += 4 if bear_pattern_confirmed else 0
    sell -= 15 if late_sell else 0
    sell -= 5 if vol_weak else 0
    sell = max(0, min(sell, 100))

    # Moderately relaxed trend confirmation.
    # Allow early trend entries before EMA20 fully crosses EMA50.
    bull_early_path = (
        price > ema20
        and ema20_up
        and not late_buy
        and (di_bull or bull_structure)
        and (adx >= 20 or vol_ratio >= 0.80 or bull_structure)
    )

    bear_early_path = (
        price < ema20
        and ema20_down
        and not late_sell
        and (di_bear or bear_structure)
        and (adx >= 20 or vol_ratio >= 0.80 or bear_structure)
    )

    
    # Higher-timeframe alignment is supporting evidence.
    # Only a clear 4H opposing trend blocks weak early entries.
    htf_buy_support = (
        trend_1h == "BULLISH"
        or trend_4h == "BULLISH"
    )
    htf_sell_support = (
        trend_1h == "BEARISH"
        or trend_4h == "BEARISH"
    )

    # Two routes: trend continuation or structure breakout/retest.
    
    buy_trend_path = (
        bull_ema
        and di_bull
        and (bull_momentum or bull_candle_ok)
        and (adx >= 20 or vol_ratio >= 0.90)
    )

    sell_trend_path = (
        bear_ema
        and di_bear
        and (bear_momentum or bear_candle_ok)
        and (adx >= 20 or vol_ratio >= 0.90)
    )
    
    )

    buy_structure_path = (
        bull_structure_path
        and bull_momentum
        and (bull_candle_ok or vol_ratio >= 1.0)
    )

    sell_structure_path = (
        bear_structure_path
        and bear_momentum
        and (bear_candle_ok or vol_ratio >= 1.0)
    )

    
    # Early entry requires stronger supporting evidence.
    early_buy_confirmed = (
        bull_early_path
        and bull_momentum
        and di_bull
        and vol_ratio >= 1.50
        and adx >= 18
        and bull_candle_ok
        and 50 <= rsi <= 70
        and enough_buy
    )

    early_sell_confirmed = (
        bear_early_path
        and bear_momentum
        and di_bear
        and vol_ratio >= 1.50
        and adx >= 18
        and bear_candle_ok
        and 30 <= rsi <= 50
        and enough_sell
    )

    # Confirmed pullback continuation in an established trend.
    # Requires a fresh closed-candle recovery, not just a bullish trend.
    bull_pullback_continuation = (
        bull_ema
        and trend_1h == "BULLISH"
        and trend_4h != "BEARISH"
        and adx >= 25
        and di_bull
        and bull_rsi_ok
        and not late_buy
        and vol_ratio >= 0.80
        and enough_buy
        and bull_candle_ok
        and closes[-2] <= opens[-2]
        and closes[-1] > highs[-2]
    )

    bear_pullback_continuation = (
        bear_ema
        and trend_1h == "BEARISH"
        and trend_4h != "BULLISH"
        and adx >= 25
        and di_bear
        and bear_rsi_ok
        and not late_sell
        and vol_ratio >= 0.80
        and enough_sell
        and bear_candle_ok
        and closes[-2] >= opens[-2]
        and closes[-1] < lows[-2]
    )
    
    buy_confirm = (
        (
            buy_trend_path
            or buy_structure_path
            or early_buy_confirmed
            or bull_pullback_continuation
            
        )
        and not late_buy
        and not vol_weak
        and enough_buy
        and (
            bull_rsi_ok
            or (htf_buy_support and 48 <= rsi <= 72)
        )
        and (
            trend_4h != "BEARISH"
            or (
                bull_structure
                and adx >= 25
                and vol_strong
            )
        )
    )

    sell_confirm = (
        (
            sell_trend_path
            or sell_structure_path
            or early_sell_confirmed
            or bear_pullback_continuation
         
        )
        and not late_sell
        and not vol_weak
        and enough_sell
        and (
            bear_rsi_ok
            or (htf_sell_support and 28 <= rsi <= 52)
        )
        and (
            trend_4h != "BULLISH"
            or (
                bear_structure
                and adx >= 25
                and vol_strong
            )
        )
    )
    
    

    if buy >= MIN_SIGNAL_SCORE and buy > sell and buy_confirm:
        signal, kind, score = "🟢 BUY", "BUY", buy
    elif sell >= MIN_SIGNAL_SCORE and sell > buy and sell_confirm:
        signal, kind, score = "🔴 SELL", "SELL", sell
    else:
        signal, kind, score = "🟡 WAIT", "WAIT", min(max(buy, sell), 84)

    # Entry quality score: supplementary setup rating, not a win probability.
    buy_quality = 0
    sell_quality = 0
    
    buy_quality += (
        30 if bull_ema
        else 20 if (bull_structure_path or bull_early_path)
        else 0
    )

    sell_quality += (
        30 if bear_ema
        else 20 if (bear_structure_path or bear_early_path)
        else 0
    )
    
    buy_quality += 15 if adx >= 20 and di_bull else 0
    sell_quality += 15 if adx >= 20 and di_bear else 0
    buy_quality += 15 if bull_rsi_ok else 0
    sell_quality += 15 if bear_rsi_ok else 0
    buy_quality += 15 if bull_structure else 0
    sell_quality += 15 if bear_structure else 0
    buy_quality += 10 if (bull_candle_ok or bull_pattern_confirmed) else 0
    sell_quality += 10 if (bear_candle_ok or bear_pattern_confirmed) else 0

    volume_points = 15 if vol_strong else (8 if vol_ratio >= WEAK_VOLUME_RATIO else 0)
    buy_quality += volume_points
    sell_quality += volume_points
    buy_quality -= 10 if late_buy else 0
    sell_quality -= 10 if late_sell else 0
    buy_quality -= 10 if not enough_buy else 0
    sell_quality -= 10 if not enough_sell else 0
    buy_quality = max(0, min(100, buy_quality))
    sell_quality = max(0, min(100, sell_quality))

    # Final candle/quality guard. Lowered from 70 to 65 to avoid over-filtering.
    # Final candle/quality guard: accept a confirmed candle pattern too.
    
    # Final candle/quality guard: accept a confirmed candle pattern too.
    
# Final quality guard: keep weak-volume and low-quality protection,
# but allow a strong setup to qualify without a perfect signal candle.


    # Final quality guard: keep weak-volume and low-quality protection.
    # Allow strong setups without requiring a perfect signal candle.

    if kind == "BUY" and (
        buy_quality < MIN_QUALITY_SCORE
        or vol_weak
        or (
            not (bull_candle_ok or bull_pattern_confirmed)
            and buy_quality < 65
        )
    ):
        kind, signal, score = (
            "WAIT", "🟡 WAIT", min(max(buy, sell), 84)
        )

    elif kind == "SELL" and (
        sell_quality < MIN_QUALITY_SCORE
        or vol_weak
        or (
            not (bear_candle_ok or bear_pattern_confirmed)
            and sell_quality < 65
        )
    ):
        kind, signal, score = (
            "WAIT", "🟡 WAIT", min(max(buy, sell), 84)
        )

    
    breakout_watch_status = "NONE"

    if kind == "WAIT":
        if buy_breakout_watch:
            breakout_watch_status = "BUY BREAKOUT WATCH"
        elif sell_breakdown_watch:
            breakout_watch_status = "SELL BREAKDOWN WATCH"
            
    if kind == "BUY":
        entry_quality_score = buy_quality
    elif kind == "SELL":
        entry_quality_score = sell_quality
    else:
        entry_quality_score = max(buy_quality, sell_quality)

    
    entry_quality = quality_grade(entry_quality_score)

    
    if kind == "BUY":
        if bull_break and bull_retest:
            entry_setup = "Breakout + Retest"
        elif bull_retest:
            entry_setup = "Breakout Retest"
        elif bull_break:
            entry_setup = "Breakout"
        elif bull_structure:
            entry_setup = "Bullish Structure"
        else:
            entry_setup = "Trend Continuation"

        candle_status = (
            "Strong Bullish Candle"
            if bullish_candle_confirmation
            else "Bullish Rejection"
            if bullish_rejection
            else "Not Confirmed"
        )
        entry_timing = (
            "Ideal" if dist_ema <= 1.0 else "Acceptable"
        )

    elif kind == "SELL":
        if bear_break and bear_retest:
            entry_setup = "Breakdown + Retest"
        elif bear_retest:
            entry_setup = "Breakdown Retest"
        elif bear_break:
            entry_setup = "Breakdown"
        elif bear_structure:
            entry_setup = "Bearish Structure"
        else:
            entry_setup = "Trend Continuation"

        candle_status = (
            "Strong Bearish Candle"
            if bearish_candle_confirmation
            else "Bearish Rejection"
            if bearish_rejection
            else "Not Confirmed"
        )
        entry_timing = (
            "Ideal" if dist_ema <= 1.0 else "Acceptable"
        )

    else:
        entry_setup = "No Confirmed Setup"
        candle_status = (
            "Partial Confirmation"
            if bull_candle_ok or bear_candle_ok
            else "Not Confirmed"
        )
        entry_timing = "Not Applicable"
    

    if kind == "BUY":
        reasons = ["Bullish EMA alignment", "RSI confirmation"]
        if di_bull:
            reasons.append("+DI stronger")
        if strong_trend:
            reasons.append("ADX trend strength")
        if vol_strong:
            reasons.append("Volume confirmation")
        if bull_structure:
            reasons.append("Breakout/retest structure")
        if vol_weak:
            reasons.append("Low volume risk")
    elif kind == "SELL":
        reasons = ["Bearish EMA alignment", "RSI confirmation"]
        if di_bear:
            reasons.append("-DI stronger")
        if strong_trend:
            reasons.append("ADX trend strength")
        if vol_strong:
            reasons.append("Volume confirmation")
        if bear_structure:
            reasons.append("Breakdown/retest structure")
        if vol_weak:
            reasons.append("Low volume risk")
    else:
        reasons = []
        if not strong_trend:
            reasons.append("ADX below 25 strong-trend threshold")
        if vol_weak:
            reasons.append("Very low volume")
        elif not vol_strong:
            reasons.append("Volume not elevated")
        if buy >= sell:
            if not bull_ema and not bull_structure_path:
                reasons.append("Bullish trend alignment incomplete")
            if not bull_rsi_ok:
                reasons.append("Bullish RSI confirmation missing")
            if not enough_buy:
                reasons.append("Limited upside room before resistance")
            if not bull_candle_ok:
                reasons.append("Bullish candle confirmation missing")
            if late_buy:
                reasons.append("BUY entry too far from EMA20")
        else:
            if not bear_ema and not bear_structure_path:
                reasons.append("Bearish trend alignment incomplete")
            if not bear_rsi_ok:
                reasons.append("Bearish RSI confirmation missing")
            if not enough_sell:
                reasons.append("Limited downside room before support")
            if not bear_candle_ok:
                reasons.append("Bearish candle confirmation missing")
            if late_sell:
                reasons.append("SELL entry too far from EMA20")
        if not reasons:
            reasons.append("Full confirmation not available")

    if bull_ema and adx >= 25 and di_bull:
        trend = "Strong Bullish"
    elif bear_ema and adx >= 25 and di_bear:
        trend = "Strong Bearish"
    elif price > ema20 and ema20 > ema50:
        trend = "Bullish"
    elif price < ema20 and ema20 < ema50:
        trend = "Bearish"
    else:
        trend = "Neutral"

    
    print(
        f"DEBUG {symbol} | "
        f"BUY={buy} SELL={sell} | "
        f"RSI={rsi:.1f} ADX={adx:.1f} "
        f"VOL={vol_ratio:.2f} | "
        f"BUY_CONFIRM={buy_confirm} "
        f"SELL_CONFIRM={sell_confirm} | "
        f"ROOM_BUY={enough_buy} "
        f"ROOM_SELL={enough_sell} | "
        f"BUY_ROOM_RR={buy_room_rr:.2f} "
        f"SELL_ROOM_RR={sell_room_rr:.2f} | "
        f"BUY_OBSTACLE={buy_obstacle} "
        f"SELL_OBSTACLE={sell_obstacle} | "
        f"BULL_EMA={bull_ema} "
        f"BEAR_EMA={bear_ema} | "
        f"BULL_STRUCTURE={bull_structure} "
        f"BEAR_STRUCTURE={bear_structure} | "
        f"BULL_EARLY={bull_early_path} "
        f"BEAR_EARLY={bear_early_path} | "
        f"BULL_RSI_OK={bull_rsi_ok} "
        f"BEAR_RSI_OK={bear_rsi_ok} | "
        f"BULL_CANDLE={bull_candle_ok} "
        f"BEAR_CANDLE={bear_candle_ok} | "
        f"BUY_QUALITY={buy_quality} "
        f"SELL_QUALITY={sell_quality} | "
        f"WEAK_VOL={vol_weak}"
    )
    
    print(
        f"DECISION {symbol} | "
        f"FINAL={kind} | "
        f"BUY_TREND={buy_trend_path} "
        f"SELL_TREND={sell_trend_path} | "
        f"BUY_STRUCTURE={buy_structure_path} "
        f"SELL_STRUCTURE={sell_structure_path} | "
        f"HTF_BUY={htf_buy_support} "
        f"HTF_SELL={htf_sell_support} | "
        f"BUY_QUALITY={buy_quality} "
        f"SELL_QUALITY={sell_quality}"
    )
    
    print(
        f"ENTRY_PATH_DETAIL {symbol} | "
        f"BUY_TREND={buy_trend_path} "
        f"BUY_STRUCTURE={buy_structure_path} "
        f"BUY_EARLY={early_buy_confirmed} "
        f"BUY_PULLBACK={bull_pullback_continuation} | "
        f"SELL_TREND={sell_trend_path} "
        f"SELL_STRUCTURE={sell_structure_path} "
        f"SELL_EARLY={early_sell_confirmed} "
        f"SELL_PULLBACK={bear_pullback_continuation} | "
        f"BUY_ROOM={enough_buy} "
        f"SELL_ROOM={enough_sell} | "
        f"WEAK_VOLUME={vol_weak}"
    )
    

    blocked_reasons = []

    if kind == "WAIT":
        if buy >= MIN_SIGNAL_SCORE and buy > sell:
            if not enough_buy:
                blocked_reasons.append("BUY_ROOM")
            if late_buy:
                blocked_reasons.append("LATE_BUY")
            if vol_weak:
                blocked_reasons.append("WEAK_VOLUME")
            if not (
                buy_trend_path
                or buy_structure_path
                or early_buy_confirmed
                or bull_pullback_continuation
            ):
                blocked_reasons.append("BUY_ENTRY_PATH")
            if trend_4h == "BEARISH":
                blocked_reasons.append("4H_OPPOSING_BUY")
            if buy_quality < MIN_QUALITY_SCORE:
                blocked_reasons.append("LOW_BUY_QUALITY")
            if not (
                bull_rsi_ok
                or (htf_buy_support and 48 <= rsi <= 72)
            ):
                blocked_reasons.append("BUY_RSI")
            if (
                not (bull_candle_ok or bull_pattern_confirmed)
                and buy_quality < 65
            ):
                blocked_reasons.append("BUY_CANDLE_QUALITY")

        elif sell >= MIN_SIGNAL_SCORE and sell > buy:
            if not enough_sell:
                blocked_reasons.append("SELL_ROOM")
            if late_sell:
                blocked_reasons.append("LATE_SELL")
            if vol_weak:
                blocked_reasons.append("WEAK_VOLUME")
            if not (
                sell_trend_path
                or sell_structure_path
                or early_sell_confirmed
                or bear_pullback_continuation
            ):
                blocked_reasons.append("SELL_ENTRY_PATH")
            if trend_4h == "BULLISH":
                blocked_reasons.append("4H_OPPOSING_SELL")
            if sell_quality < MIN_QUALITY_SCORE:
                blocked_reasons.append("LOW_SELL_QUALITY")
            if not (
                bear_rsi_ok
                or (htf_sell_support and 28 <= rsi <= 52)
            ):
                blocked_reasons.append("SELL_RSI")
            if (
                not (bear_candle_ok or bear_pattern_confirmed)
                and sell_quality < 65
            ):
                blocked_reasons.append("SELL_CANDLE_QUALITY")
                

    print(
        f"BLOCKED {symbol} | "
        f"REASONS={','.join(blocked_reasons) if blocked_reasons else 'NONE'} | "
        f"BUY_WATCH={buy_breakout_watch} | "
        f"SELL_WATCH={sell_breakdown_watch}"
    )
    
    print(
        f"FILTER_DETAIL {symbol} | "
        f"PRICE={price:.8g} | "
        f"EMA20_UP={ema20_up} "
        f"EMA20_DOWN={ema20_down} | "
        f"DI_BULL={di_bull} "
        f"DI_BEAR={di_bear} | "
        f"BULL_MOMENTUM={bull_momentum} "
        f"BEAR_MOMENTUM={bear_momentum} | "
        f"LATE_BUY={late_buy} "
        f"LATE_SELL={late_sell} | "
        f"BULL_STRUCTURE_PATH={bull_structure_path} "
        f"BEAR_STRUCTURE_PATH={bear_structure_path} | "
        f"1H={trend_1h} "
        f"4H={trend_4h} | "
        f"VOL_RATIO={vol_ratio:.2f}"
    )
    
     
    # Independent trade eligibility assessment.
    # Does not change the original BUY/SELL/WAIT signal.

    trade_advice = "NO TRADE"
    trade_advice_reason = "No confirmed BUY/SELL signal"

    if kind in ("BUY", "SELL"):

        is_buy = kind == "BUY"

        direction_quality = (
            buy_quality if is_buy else sell_quality
        )

        direction_room = (
            buy_room_rr if is_buy else sell_room_rr
        )

        direction_candle = (
            bull_candle_ok if is_buy else bear_candle_ok
        )

        direction_structure = (
            bull_structure if is_buy else bear_structure
        )

        direction_di = (
            di_bull if is_buy else di_bear
        )

        direction_htf = (
            "BULLISH" if is_buy else "BEARISH"
        )

        opposite_htf = (
            "BEARISH" if is_buy else "BULLISH"
        )

        # Do not approve trades against a clear 4H trend.
        htf_safe = (
            trend_4h != opposite_htf
            and trend_4h != "UNKNOWN"
        )

        # Prefer 1H alignment, but allow strong structure.
        htf_confirmed = (
            trend_1h == direction_htf
            or (
                direction_structure
                and adx >= 25
                and vol_ratio >= 1.0
            )
        )

        checks = {
            "Higher timeframe": htf_safe and htf_confirmed,
            "Signal strength": score >= 70,
            "Setup quality": direction_quality >= 65,
            "Trend momentum": adx >= 20 and direction_di,
            "Volume": vol_ratio >= 0.80,
            "Candle confirmation": direction_candle,
            "Target room": direction_room >= 1.5,
            "Entry distance": not (
                late_buy if is_buy else late_sell
            ),
        }

        failed_checks = [
            name for name, passed in checks.items()
            if not passed
        ]

        if not failed_checks:
            trade_advice = "ELIGIBLE"
            trade_advice_reason = (
                "All preliminary trade checks passed"
            )
        else:
            trade_advice = "AVOID"
            trade_advice_reason = (
                "Missing: " + ", ".join(failed_checks)
        )
            
    return {
        "price": price,
        "ema20": ema20,
        "ema50": ema50,
        "trend_1h": trend_1h,
        "trend_4h": trend_4h,
        "rsi": rsi,
        "adx": adx,
        "plus_di": pdi,
        "minus_di": mdi,
        "atr": atr,
        "trend": trend,
        "signal": signal,
        "trade_advice": trade_advice,
        "trade_advice_reason": trade_advice_reason,
        "signal_type": kind,
        "signal_score": score,
        "buy_score": buy,
        "sell_score": sell,
        "volume_ratio": vol_ratio,
        "entry_quality": entry_quality,
        "entry_quality_score": entry_quality_score,
        "entry_setup": entry_setup,
        "candle_status": candle_status,
        "entry_timing": entry_timing,
        "support": support,
        "resistance": resistance,
        "buy_obstacle": buy_obstacle,
        "sell_obstacle": sell_obstacle,
        "nearest_support": ns,
        "nearest_resistance": nr,
        "reason": " + ".join(reasons),
        "breakout_watch": breakout_watch_status,
        
    }



def calculate_trade_levels(price, atr, signal, support, resistance, ns, nr):
    """
    Calculate structure-aware SL, TP1 and TP2.

    ns = nearest relevant support / sell obstacle
    nr = nearest relevant resistance / buy obstacle

    Returns (None, None, None) when the setup is unsuitable.
    """

    if atr <= 0 or price <= 0:
        return None, None, None

    if signal not in ("🟢 BUY", "🔴 SELL"):
        return None, None, None

    buffer = atr * STRUCTURE_BUFFER_ATR
    atr_stop = atr * STOP_ATR_MULTIPLIER

    # Do not allow an excessively wide structural stop.
    max_risk = atr * 2.5

    if signal == "🟢 BUY":

        # Use a confirmed support below entry, if available.
        supports = [
            level for level in (support, ns)
            if level is not None and 0 < level < price
        ]

        if supports:
            structural_sl = max(supports) - buffer
            sl = min(price - atr_stop, structural_sl)
        else:
            sl = price - atr_stop

        risk = price - sl

        if risk <= 0 or risk > max_risk or sl <= 0:
            return None, None, None

        # Use the same forward barrier as the signal filter.
        obstacle = nr if nr is not None and nr > price else None

        if obstacle is not None:
            available_room = obstacle - buffer - price

            if available_room < risk * 1.5:
                return None, None, None

        tp1 = price + risk * TP1_RR
        tp2 = price + risk * TP2_RR

        if obstacle is not None:
            tp1 = min(tp1, obstacle - buffer)

        if tp1 <= price or tp2 <= tp1:
            return None, None, None

        return sl, tp1, tp2

    if signal == "🔴 SELL":

        # Use a confirmed resistance above entry, if available.
        resistances = [
            level for level in (resistance, nr)
            if level is not None and level > price
        ]

        if resistances:
            structural_sl = min(resistances) + buffer
            sl = max(price + atr_stop, structural_sl)
        else:
            sl = price + atr_stop

        risk = sl - price

        if risk <= 0 or risk > max_risk:
            return None, None, None

        obstacle = ns if ns is not None and ns < price else None

        if obstacle is not None:
            available_room = price - (obstacle + buffer)

            if available_room < risk * 1.5:
                return None, None, None

        tp1 = price - risk * TP1_RR
        tp2 = price - risk * TP2_RR

        if obstacle is not None:
            tp1 = max(tp1, obstacle + buffer)

        if tp1 >= price or tp2 >= tp1 or tp2 <= 0:
            return None, None, None

        return sl, tp1, tp2

    return None, None, None
    


def estimate_duration(price, tp2, atr):
    if atr <= 0 or tp2 is None:
        return "N/A"
    # A rough ATR-based estimate, not a guaranteed target time.
    minutes = max(15, abs(tp2 - price) / atr * 15 * 1.5)
    if minutes < 60:
        return f"تقریباً {math.ceil(minutes)} منٹ"
    hours = minutes / 60
    if hours < 24:
        return f"تقریباً {hours:.1f} گھنٹے"
    return f"تقریباً {hours / 24:.1f} دن"


def send_discord(message):
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL is not configured.")
        return

    max_length = 1900
    parts, current = [], ""

    for section in message.split("\n\n"):
        section = section.strip()
        if not section:
            continue
        candidate = current + "\n\n" + section if current else section
        if len(candidate) <= max_length:
            current = candidate
        else:
            if current:
                parts.append(current)
            while len(section) > max_length:
                parts.append(section[:max_length])
                section = section[max_length:]
            current = section

    if current:
        parts.append(current)

    for i, part in enumerate(parts, 1):
        response = requests.post(
            WEBHOOK_URL,
            json={"content": part},
            timeout=TIMEOUT,
        )
        print(f"Discord response {i}/{len(parts)}: {response.status_code}")
        response.raise_for_status()

    print("Discord message sent successfully.")


def main():
    print("Crypto Signal Bot V3 - Bitget USDT Perpetual data")
    messages = []

    for symbol in COINS:
        try:
            print(f"Checking {symbol}...")
            result = get_signal(symbol)
            price = result["price"]
            atr = result["atr"]
            signal = result["signal"]

            
            sl, tp1, tp2 = calculate_trade_levels(
                price, atr, signal,
                result["support"], result["resistance"],
                result["sell_obstacle"], result["buy_obstacle"],
            )
            
            
            duration = estimate_duration(price, tp2, atr)
            
            if signal in ("🟢 BUY", "🔴 SELL") and sl is None:
                result["trade_advice"] = "AVOID"
                result["trade_advice_reason"] = (
                    "Structural SL/TP validation failed"
                )
                

            if sl is not None:
                levels = (
                    f"🛑 Stop Loss: {sl:.8g}\n"
                    f"🎯 TP1: {tp1:.8g}\n"
                    f"🎯 TP2: {tp2:.8g}\n"
                )
            else:
                levels = (
                    "🛑 Stop Loss: N/A\n"
                    "🎯 TP1: N/A\n"
                    "🎯 TP2: N/A\n"
                )

            support_text = (
                f"{result['nearest_support']:.8g}"
                if result["nearest_support"] is not None else "N/A"
            )
            resistance_text = (
                f"{result['nearest_resistance']:.8g}"
                if result["nearest_resistance"] is not None else "N/A"
            )

            advice = result["trade_advice"]

            if advice == "ELIGIBLE":
                advice_banner = (
                    "🟢━━━━━━━━━━━━━━━━━━━━\n"
                    "✅ TRADE ELIGIBLE\n"
                    "ابتدائی شرائط پوری ہیں؛ رسک چیک کریں\n"
                    "🟢━━━━━━━━━━━━━━━━━━━━\n"
                )
            elif advice == "AVOID":
                advice_banner = (
                    "🔴━━━━━━━━━━━━━━━━━━━━\n"
                    "⛔ TRADE AVOID\n"
                    "یہ ٹریڈ فی الحال نہ لیں\n"
                    "🔴━━━━━━━━━━━━━━━━━━━━\n"
                )
            else:
                advice_banner = (
                    "🟡━━━━━━━━━━━━━━━━━━━━\n"
                    "⏳ NO TRADE — WAIT\n"
                    "🟡━━━━━━━━━━━━━━━━━━━━\n"
                )
                
            messages.append(
                f"**{symbol}**\n"
                f"📡 Data: Bitget USDT Perpetual\n"
                f"💰 Entry: {price:.8g}\n"
                f"📈 Trend: {result['trend']}\n"
                f"🕐 1H Trend: {result['trend_1h']}\n"
                f"🕓 4H Trend: {result['trend_4h']}\n"
                f"📊 RSI: {result['rsi']:.2f}\n"
                f"〽️ EMA20: {result['ema20']:.8g}\n"
                f"〽️ EMA50: {result['ema50']:.8g}\n"
                f"💪 ADX: {result['adx']:.2f}\n"
                f"↗️ +DI: {result['plus_di']:.2f} | ↘️ -DI: {result['minus_di']:.2f}\n"
                f"📦 Volume: {result['volume_ratio']:.2f}x\n"
                f"🎯 Signal: {signal}\n"
                f"🔔 Breakout Watch: {result['breakout_watch']}\n"
                f"💯 Signal Score: {result['signal_score']}/100\n"
                f"⭐ Setup Quality: {result['entry_quality']} "
                f"({result['entry_quality_score']}/100; not win probability)\n"
                f"📌 Setup: {result['entry_setup']}\n"
                f"🕯️ Candle: {result['candle_status']}\n"
                f"🎯 Entry Timing: {result['entry_timing']}\n"
                f"📉 Support: {support_text}\n"
                f"📈 Resistance: {resistance_text}\n"
                f"{levels}"
                f"⏱️ Expected: {duration}\n"
                f"📝 Reason: {result['reason']}\n\n"
                f"{advice_banner}"
                f"🔎 Advice Reason: "
                f"{result['trade_advice_reason']}"
                
            )

            print(
                f"{symbol}: {signal} | Score={result['signal_score']} "
                f"| Quality={result['entry_quality_score']}"
            )

        except Exception as error:
            print(f"{symbol} error: {error}")

    if messages:
        send_discord("\n\n".join(messages))
    else:
        print("No results were generated.")


if __name__ == "__main__":
    main()

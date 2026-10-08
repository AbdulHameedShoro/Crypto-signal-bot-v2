import os
import math
import requests


# ============================================================
# CONFIGURATION
# ============================================================

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

COINS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ZENUSDT",
    "ZECUSDT",
]

# Binance USDⓈ-M Futures API
BINANCE_URL = "https://fapi.binance.com"

TIMEFRAME = "15m"
CANDLE_LIMIT = 200

# Signal settings
MIN_SIGNAL_SCORE = 80

# Risk settings
STOP_ATR_MULTIPLIER = 1.5
TP1_RR_MIN = 1.20
TP1_RR_DEFAULT = 1.50
TP2_RR_DEFAULT = 2.50

# Structure settings
STRUCTURE_LOOKBACK = 20
STRUCTURE_BUFFER_ATR = 0.15

# Entry protection
MAX_ENTRY_DISTANCE_ATR = 1.5

# Volume settings
STRONG_VOLUME_RATIO = 1.10
WEAK_VOLUME_RATIO = 0.60

# Request settings
REQUEST_TIMEOUT = 10


# ============================================================
# BINANCE FUTURES DATA
# ============================================================

def get_klines(symbol, interval=TIMEFRAME, limit=CANDLE_LIMIT):
    """
    Get Binance USDⓈ-M Futures klines.

    The latest candle may still be running.
    We remove it later in get_signal().
    """

    url = f"{BINANCE_URL}/fapi/v1/klines"

    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    }

    response = requests.get(
        url,
        params=params,
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()

    data = response.json()

    if not isinstance(data, list) or len(data) < 60:
        raise ValueError(
            f"Not enough kline data for {symbol}."
        )

    return data


# ============================================================
# EMA
# ============================================================

def calculate_ema(values, period):
    if len(values) < period:
        return values[-1]

    multiplier = 2 / (period + 1)

    ema = sum(values[:period]) / period

    for price in values[period:]:
        ema = (
            (price - ema) * multiplier
            + ema
        )

    return ema


# ============================================================
# RSI
# ============================================================

def calculate_rsi(values, period=14):
    if len(values) <= period:
        return 50.0

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            ((avg_gain * (period - 1)) + gains[i])
            / period
        )

        avg_loss = (
            ((avg_loss * (period - 1)) + losses[i])
            / period
        )

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


# ============================================================
# ATR
# ============================================================

def calculate_atr(
    highs,
    lows,
    closes,
    period=14
):
    if len(closes) <= period:
        return 0.0

    true_ranges = []

    for i in range(1, len(closes)):
        high_low = highs[i] - lows[i]

        high_close = abs(
            highs[i] - closes[i - 1]
        )

        low_close = abs(
            lows[i] - closes[i - 1]
        )

        true_ranges.append(
            max(
                high_low,
                high_close,
                low_close
            )
        )

    atr = sum(
        true_ranges[:period]
    ) / period

    for tr in true_ranges[period:]:
        atr = (
            ((atr * (period - 1)) + tr)
            / period
        )

    return atr


# ============================================================
# ADX
# ============================================================

def calculate_adx(
    highs,
    lows,
    closes,
    period=14
):
    if len(closes) < period * 2 + 1:
        return 0.0

    true_ranges = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(closes)):

        high_low = highs[i] - lows[i]

        high_close = abs(
            highs[i] - closes[i - 1]
        )

        low_close = abs(
            lows[i] - closes[i - 1]
        )

        true_ranges.append(
            max(
                high_low,
                high_close,
                low_close
            )
        )

        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]

        if (
            up_move > down_move
            and up_move > 0
        ):
            plus_dm.append(up_move)
        else:
            plus_dm.append(0.0)

        if (
            down_move > up_move
            and down_move > 0
        ):
            minus_dm.append(down_move)
        else:
            minus_dm.append(0.0)

    atr = (
        sum(true_ranges[:period])
        / period
    )

    plus_smoothed = (
        sum(plus_dm[:period])
        / period
    )

    minus_smoothed = (
        sum(minus_dm[:period])
        / period
    )

    dx_values = []

    for i in range(
        period,
        len(true_ranges)
    ):

        atr = (
            ((atr * (period - 1))
             + true_ranges[i])
            / period
        )

        plus_smoothed = (
            ((plus_smoothed * (period - 1))
             + plus_dm[i])
            / period
        )

        minus_smoothed = (
            ((minus_smoothed * (period - 1))
             + minus_dm[i])
            / period
        )

        if atr == 0:
            continue

        plus_di = (
            plus_smoothed / atr
        ) * 100

        minus_di = (
            minus_smoothed / atr
        ) * 100

        di_sum = plus_di + minus_di

        if di_sum == 0:
            continue

        dx = (
            abs(plus_di - minus_di)
            / di_sum
        ) * 100

        dx_values.append(dx)

    if len(dx_values) < period:
        return 0.0

    adx = (
        sum(dx_values[:period])
        / period
    )

    for dx in dx_values[period:]:
        adx = (
            ((adx * (period - 1)) + dx)
            / period
        )

    return adx


# ============================================================
# SUPPORT / RESISTANCE
# ============================================================

def get_support_resistance(
    highs,
    lows,
    lookback=STRUCTURE_LOOKBACK
):
    """
    Uses previous candles and excludes
    the latest closed candle.

    This helps avoid making the current
    candle itself the main structure level.
    """

    if len(highs) < lookback + 2:
        return min(lows), max(highs)

    recent_highs = highs[
        -lookback - 1:-1
    ]

    recent_lows = lows[
        -lookback - 1:-1
    ]

    resistance = max(recent_highs)
    support = min(recent_lows)

    return support, resistance


# ============================================================
# NEAREST STRUCTURE LEVELS
# ============================================================

def get_nearest_resistance(
    highs,
    price,
    atr,
    lookback=30
):
    """
    Finds the nearest meaningful resistance
    above the current price.
    """

    start = max(
        0,
        len(highs) - lookback - 1
    )

    candidates = []

    for high in highs[start:-1]:

        if high <= price:
            continue

        if atr > 0:
            distance = (
                high - price
            ) / atr

            if distance < 0.25:
                continue

        candidates.append(high)

    if not candidates:
        return None

    return min(candidates)


def get_nearest_support(
    lows,
    price,
    atr,
    lookback=30
):
    """
    Finds the nearest meaningful support
    below the current price.
    """

    start = max(
        0,
        len(lows) - lookback - 1
    )

    candidates = []

    for low in lows[start:-1]:

        if low >= price:
            continue

        if atr > 0:
            distance = (
                price - low
            ) / atr

            if distance < 0.25:
                continue

        candidates.append(low)

    if not candidates:
        return None

    return max(candidates)


# ============================================================
# SIGNAL CALCULATION
# ============================================================

def get_signal(symbol):

    candles = get_klines(symbol)

    # --------------------------------------------------------
    # IMPORTANT:
    # The last candle is still running.
    # We NEVER use it for signal calculation.
    # --------------------------------------------------------

    closed_candles = candles[:-1]

    if len(closed_candles) < 60:
        raise ValueError(
            "Not enough closed candles."
        )

    closes = [
        float(c[4])
        for c in closed_candles
    ]

    highs = [
        float(c[2])
        for c in closed_candles
    ]

    lows = [
        float(c[3])
        for c in closed_candles
    ]

    volumes = [
        float(c[5])
        for c in closed_candles
    ]

    price = closes[-1]

    # --------------------------------------------------------
    # INDICATORS
    # --------------------------------------------------------

    ema20 = calculate_ema(
        closes,
        20
    )

    ema50 = calculate_ema(
        closes,
        50
    )

    rsi = calculate_rsi(
        closes,
        14
    )

    atr = calculate_atr(
        highs,
        lows,
        closes,
        14
    )

    adx = calculate_adx(
        highs,
        lows,
        closes,
        14
    )

    # --------------------------------------------------------
    # EMA SLOPE
    # --------------------------------------------------------

    ema20_prev = calculate_ema(
        closes[:-3],
        20
    )

    ema50_prev = calculate_ema(
        closes[:-3],
        50
    )

    ema20_rising = (
        ema20 > ema20_prev
    )

    ema20_falling = (
        ema20 < ema20_prev
    )

    ema50_rising = (
        ema50 > ema50_prev
    )

    ema50_falling = (
        ema50 < ema50_prev
    )

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    price_3_candles_ago = closes[-4]

    bullish_momentum = (
        price > price_3_candles_ago
    )

    bearish_momentum = (
        price < price_3_candles_ago
    )

    # --------------------------------------------------------
    # VOLUME
    # --------------------------------------------------------

    if len(volumes) >= 21:

        avg_volume = (
            sum(volumes[-21:-1])
            / 20
        )

    else:

        avg_volume = sum(
            volumes[:-1]
        ) / max(
            1,
            len(volumes) - 1
        )

    current_volume = volumes[-1]

    if avg_volume > 0:
        volume_ratio = (
            current_volume
            / avg_volume
        )
    else:
        volume_ratio = 0.0

    volume_strong = (
        volume_ratio >= STRONG_VOLUME_RATIO
    )

    volume_weak = (
        volume_ratio < WEAK_VOLUME_RATIO
    )

    # --------------------------------------------------------
    # SUPPORT / RESISTANCE
    # --------------------------------------------------------

    support, resistance = (
        get_support_resistance(
            highs,
            lows,
            STRUCTURE_LOOKBACK
        )
    )

    nearest_support = (
        get_nearest_support(
            lows,
            price,
            atr,
            30
        )
    )

    nearest_resistance = (
        get_nearest_resistance(
            highs,
            price,
            atr,
            30
        )
    )

    # --------------------------------------------------------
    # TREND
    # --------------------------------------------------------

    if (
        price > ema20
        and ema20 > ema50
        and ema20_rising
    ):

        trend = "Strong Bullish"

    elif (
        price < ema20
        and ema20 < ema50
        and ema20_falling
    ):

        trend = "Strong Bearish"

    elif (
        price > ema20
        and ema20 > ema50
    ):

        trend = "Bullish"

    elif (
        price < ema20
        and ema20 < ema50
    ):

        trend = "Bearish"

    else:

        trend = "Neutral"

    # --------------------------------------------------------
    # EMA ALIGNMENT
    # --------------------------------------------------------

    bullish_ema = (
        price > ema20
        and ema20 > ema50
        and ema20_rising
    )

    bearish_ema = (
        price < ema20
        and ema20 < ema50
        and ema20_falling
    )

    # --------------------------------------------------------
    # BREAKOUT / BREAKDOWN
    # --------------------------------------------------------

    previous_resistance = max(
        highs[-21:-1]
    )

    previous_support = min(
        lows[-21:-1]
    )

    bullish_breakout = (
        price > previous_resistance
        and closes[-2]
        <= previous_resistance
    )

    bearish_breakdown = (
        price < previous_support
        and closes[-2]
        >= previous_support
    )

    # --------------------------------------------------------
    # RETEST
    # --------------------------------------------------------

    bullish_retest = (
        lows[-1] <= previous_resistance
        and price > previous_resistance
    )

    bearish_retest = (
        highs[-1] >= previous_support
        and price < previous_support
    )

    bullish_structure = (
        bullish_breakout
        or bullish_retest
    )

    bearish_structure = (
        bearish_breakdown
        or bearish_retest
    )

    # --------------------------------------------------------
    # DISTANCE FROM EMA20
    # --------------------------------------------------------

    if atr > 0:

        distance_from_ema20 = (
            abs(price - ema20)
            / atr
        )

    else:

        distance_from_ema20 = 0.0

    late_buy = (
        price > ema20
        and distance_from_ema20
        > MAX_ENTRY_DISTANCE_ATR
    )

    late_sell = (
        price < ema20
        and distance_from_ema20
        > MAX_ENTRY_DISTANCE_ATR
    )

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    bullish_rsi = (
        52 <= rsi <= 68
    )

    bearish_rsi = (
        32 <= rsi <= 48
    )

    # --------------------------------------------------------
    # ADX
    # --------------------------------------------------------

    strong_trend = (
        adx >= 25
    )

    moderate_trend = (
        adx >= 20
    )

    # --------------------------------------------------------
    # ROOM BEFORE MAJOR LEVEL
    # --------------------------------------------------------

    if atr > 0:

        resistance_distance = (
            resistance - price
        ) / atr

        support_distance = (
            price - support
        ) / atr

    else:

        resistance_distance = 0.0
        support_distance = 0.0

    enough_buy_room = (
        resistance_distance >= 0.75
        or bullish_breakout
    )

    enough_sell_room = (
        support_distance >= 0.75
        or bearish_breakdown
    )

    # --------------------------------------------------------
    # BUY SCORE
    # --------------------------------------------------------

    buy_score = 0

    if price > ema20:
        buy_score += 15

    if ema20 > ema50:
        buy_score += 15

    if ema20_rising:
        buy_score += 10

    if ema50_rising:
        buy_score += 5

    if bullish_rsi:
        buy_score += 15

    if bullish_momentum:
        buy_score += 10

    if adx >= 25:
        buy_score += 15

    elif adx >= 20:
        buy_score += 8

    if volume_strong:
        buy_score += 10

    if bullish_structure:
        buy_score += 5

    if enough_buy_room:
        buy_score += 5

    if late_buy:
        buy_score -= 15

    # Very weak volume penalty
    if volume_weak:
        buy_score -= 5

    buy_score = max(
        0,
        min(buy_score, 100)
    )

    # --------------------------------------------------------
    # SELL SCORE
    # --------------------------------------------------------

    sell_score = 0

    if price < ema20:
        sell_score += 15

    if ema20 < ema50:
        sell_score += 15

    if ema20_falling:
        sell_score += 10

    if ema50_falling:
        sell_score += 5

    if bearish_rsi:
        sell_score += 15

    if bearish_momentum:
        sell_score += 10

    if adx >= 25:
        sell_score += 15

    elif adx >= 20:
        sell_score += 8

    if volume_strong:
        sell_score += 10

    if bearish_structure:
        sell_score += 5

    if enough_sell_room:
        sell_score += 5

    if late_sell:
        sell_score -= 15

    # Very weak volume penalty
    if volume_weak:
        sell_score -= 5

    sell_score = max(
        0,
        min(sell_score, 100)
    )

    # --------------------------------------------------------
    # FINAL CONFIRMATIONS
    # --------------------------------------------------------

    buy_confirmations = (
        bullish_ema
        and bullish_rsi
        and enough_buy_room
        and not late_buy
        and (
            strong_trend
            or volume_strong
            or bullish_structure
        )
    )

    sell_confirmations = (
        bearish_ema
        and bearish_rsi
        and enough_sell_room
        and not late_sell
        and (
            strong_trend
            or volume_strong
            or bearish_structure
        )
    )

    # --------------------------------------------------------
    # FINAL SIGNAL
    # --------------------------------------------------------

    signal = "🟡 WAIT"
    signal_type = "WAIT"

    signal_score = max(
        buy_score,
        sell_score
    )

    if (
        buy_score >= MIN_SIGNAL_SCORE
        and buy_score > sell_score
        and buy_confirmations
    ):

        signal = "🟢 BUY"
        signal_type = "BUY"
        signal_score = buy_score

    elif (
        sell_score >= MIN_SIGNAL_SCORE
        and sell_score > buy_score
        and sell_confirmations
    ):

        signal = "🔴 SELL"
        signal_type = "SELL"
        signal_score = sell_score

    else:

        signal = "🟡 WAIT"
        signal_type = "WAIT"

        # Never display 85+ for WAIT
        signal_score = min(
            max(buy_score, sell_score),
            84
        )

    # --------------------------------------------------------
    # REASON
    # --------------------------------------------------------

    if signal_type == "BUY":

        confirmations = [
            "Bullish trend",
            "EMA alignment",
            "RSI momentum",
            "ADX strength",
        ]

        if volume_strong:
            confirmations.append(
                "Volume confirmation"
            )

        elif volume_weak:
            confirmations.append(
                "Low volume risk"
            )

        if bullish_structure:
            confirmations.append(
                "Breakout/Retest"
            )

        reason = " + ".join(
            confirmations
        )

    elif signal_type == "SELL":

        confirmations = [
            "Bearish trend",
            "EMA alignment",
            "RSI momentum",
            "ADX strength",
        ]

        if volume_strong:
            confirmations.append(
                "Volume confirmation"
            )

        elif volume_weak:
            confirmations.append(
                "Low volume risk"
            )

        if bearish_structure:
            confirmations.append(
                "Breakdown/Retest"
    )

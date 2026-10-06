import os
import math
import requests

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

BINANCE_URL = "https://data-api.binance.vision"

TIMEFRAME = "15m"
CANDLE_LIMIT = 200


def get_klines(symbol, interval=TIMEFRAME, limit=CANDLE_LIMIT):
    url = f"{BINANCE_URL}/api/v3/klines"

    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    }

    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()

    return response.json()


def calculate_ema(values, period):
    if len(values) < period:
        return values[-1]

    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period

    for price in values[period:]:
        ema = (price - ema) * multiplier + ema

    return ema


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
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_atr(highs, lows, closes, period=14):
    if len(closes) <= period:
        return 0.0

    true_ranges = []

    for i in range(1, len(closes)):
        high_low = highs[i] - lows[i]
        high_close = abs(highs[i] - closes[i - 1])
        low_close = abs(lows[i] - closes[i - 1])

        true_ranges.append(
            max(high_low, high_close, low_close)
        )

    atr = sum(true_ranges[:period]) / period

    for tr in true_ranges[period:]:
        atr = ((atr * (period - 1)) + tr) / period

    return atr


def calculate_adx(highs, lows, closes, period=14):
    if len(closes) < period * 2 + 1:
        return 0.0

    true_ranges = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(closes)):
        high_low = highs[i] - lows[i]
        high_close = abs(highs[i] - closes[i - 1])
        low_close = abs(lows[i] - closes[i - 1])

        true_ranges.append(
            max(high_low, high_close, low_close)
        )

        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]

        if up_move > down_move and up_move > 0:
            plus_dm.append(up_move)
        else:
            plus_dm.append(0.0)

        if down_move > up_move and down_move > 0:
            minus_dm.append(down_move)
        else:
            minus_dm.append(0.0)

    atr = sum(true_ranges[:period]) / period
    plus_smoothed = sum(plus_dm[:period]) / period
    minus_smoothed = sum(minus_dm[:period]) / period

    dx_values = []

    for i in range(period, len(true_ranges)):
        atr = ((atr * (period - 1)) + true_ranges[i]) / period
        plus_smoothed = (
            (plus_smoothed * (period - 1)) + plus_dm[i]
        ) / period
        minus_smoothed = (
            (minus_smoothed * (period - 1)) + minus_dm[i]
        ) / period

        if atr == 0:
            continue

        plus_di = (plus_smoothed / atr) * 100
        minus_di = (minus_smoothed / atr) * 100

        di_sum = plus_di + minus_di

        if di_sum == 0:
            continue

        dx = abs(plus_di - minus_di) / di_sum * 100
        dx_values.append(dx)

    if len(dx_values) < period:
        return 0.0

    adx = sum(dx_values[:period]) / period

    for dx in dx_values[period:]:
        adx = ((adx * (period - 1)) + dx) / period

    return adx


def get_support_resistance(highs, lows, lookback=20):
    recent_highs = highs[-lookback:]
    recent_lows = lows[-lookback:]

    resistance = max(recent_highs)
    support = min(recent_lows)

    return support, resistance


def get_signal(symbol):
    candles = get_klines(symbol)

    # آخری Candle ابھی مکمل نہیں ہوئی،
    # اس لیے اسے Signal calculation میں استعمال نہیں کریں گے۔
    closed_candles = candles[:-1]

    closes = [float(c[4]) for c in closed_candles]
    highs = [float(c[2]) for c in closed_candles]
    lows = [float(c[3]) for c in closed_candles]
    volumes = [float(c[5]) for c in closed_candles]

    if len(closes) < 60:
        raise ValueError("Not enough closed candles.")

    price = closes[-1]

    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    rsi = calculate_rsi(closes, 14)
    atr = calculate_atr(highs, lows, closes, 14)
    adx = calculate_adx(highs, lows, closes, 14)

    ema20_prev = calculate_ema(closes[:-3], 20)
    ema50_prev = calculate_ema(closes[:-3], 50)

    ema20_rising = ema20 > ema20_prev
    ema20_falling = ema20 < ema20_prev

    ema50_rising = ema50 > ema50_prev
    ema50_falling = ema50 < ema50_prev

    price_3_candles_ago = closes[-4]

    bullish_momentum = price > price_3_candles_ago
    bearish_momentum = price < price_3_candles_ago

    avg_volume = sum(volumes[-21:-1]) / 20
    current_volume = volumes[-1]

    if avg_volume > 0:
        volume_ratio = current_volume / avg_volume
    else:
        volume_ratio = 0

    volume_strong = volume_ratio >= 1.10

    support, resistance = get_support_resistance(
        highs,
        lows,
        lookback=20
    )

    # Trend
    if price > ema20 and ema20 > ema50 and ema20_rising:
        trend = "Strong Bullish"

    elif price < ema20 and ema20 < ema50 and ema20_falling:
        trend = "Strong Bearish"

    elif price > ema20 and ema20 > ema50:
        trend = "Bullish"

    elif price < ema20 and ema20 < ema50:
        trend = "Bearish"

    else:
        trend = "Neutral"

    # EMA alignment
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

    # Breakout / Breakdown
    previous_resistance = max(highs[-21:-1])
    previous_support = min(lows[-21:-1])

    bullish_breakout = (
        price > previous_resistance
        and closes[-2] <= previous_resistance
    )

    bearish_breakdown = (
        price < previous_support
        and closes[-2] >= previous_support
    )

    # Retest
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

    # ATR distance
    if atr > 0:
        distance_from_ema20 = abs(price - ema20) / atr
    else:
        distance_from_ema20 = 0

    # Late entry protection
    late_buy = (
        price > ema20
        and distance_from_ema20 > 1.5
    )

    late_sell = (
        price < ema20
        and distance_from_ema20 > 1.5
    )

    # RSI
    bullish_rsi = 52 <= rsi <= 68
    bearish_rsi = 32 <= rsi <= 48

    # ADX
    strong_trend = adx >= 25

    # Room before major level
    if atr > 0:
        resistance_distance = (resistance - price) / atr
        support_distance = (price - support) / atr
    else:
        resistance_distance = 0
        support_distance = 0

    enough_buy_room = (
        resistance_distance >= 0.75
        or bullish_breakout
    )

    enough_sell_room = (
        support_distance >= 0.75
        or bearish_breakdown
    )

    # --------------------------------
    # BUY SCORE
    # --------------------------------

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

    buy_score = max(0, min(buy_score, 100))

    # --------------------------------
    # SELL SCORE
    # --------------------------------

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

    sell_score = max(0, min(sell_score, 100))

    # --------------------------------
    # FINAL SIGNAL
    # --------------------------------

    signal = "🟡 WAIT"
    signal_type = "WAIT"
    signal_score = max(buy_score, sell_score)

    buy_confirmations = (
        bullish_ema
        and bullish_rsi
        and bullish_momentum
        and strong_trend
        and volume_strong
        and enough_buy_room
        and not late_buy
    )

    sell_confirmations = (
        bearish_ema
        and bearish_rsi
        and bearish_momentum
        and strong_trend
        and volume_strong
        and enough_sell_room
        and not late_sell
    )

    if (
        buy_score >= 85
        and buy_score > sell_score
        and buy_confirmations
    ):
        signal = "🟢 BUY"
        signal_type = "BUY"
        signal_score = buy_score

    elif (
        sell_score >= 85
        and sell_score > buy_score
        and sell_confirmations
    ):
        signal = "🔴 SELL"
        signal_type = "SELL"
        signal_score = sell_score

    else:
        signal = "🟡 WAIT"
        signal_type = "WAIT"

        # WAIT کو کبھی مصنوعی 100/100 نہیں دکھائیں گے۔
        signal_score = min(max(buy_score, sell_score), 84)

    # Reason
    if signal_type == "BUY":
        reason = (
            "Bullish trend + EMA alignment + RSI momentum + "
            "ADX strength + volume confirmation"
        )

    elif signal_type == "SELL":
        reason = (
            "Bearish trend + EMA alignment + RSI momentum + "
            "ADX strength + volume confirmation"
        )

    else:
        reasons = []

        if not strong_trend:
            reasons.append("ADX weak")

        if not volume_strong:
            reasons.append("Volume weak")

        if not bullish_ema and not bearish_ema:
            reasons.append("EMA trend unclear")

        if not bullish_rsi and not bearish_rsi:
            reasons.append("RSI not confirmed")

        if late_buy or late_sell:
            reasons.append("Entry too late")

        if not enough_buy_room and not enough_sell_room:
            reasons.append("Near support/resistance")

        if not reasons:
            reasons.append("Full confirmation not available")

        reason = " + ".join(reasons)

    return {
        "price": price,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi,
        "adx": adx,
        "atr": atr,
        "trend": trend,
        "signal": signal,
        "signal_type": signal_type,
        "signal_score": signal_score,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "volume_ratio": volume_ratio,
        "support": support,
        "resistance": resistance,
        "reason": reason,
    }


def calculate_trade_levels(price, atr, signal):
    if signal == "🟢 BUY":
        stop_loss = price - (atr * 1.5)

        risk = price - stop_loss

        tp1 = price + (risk * 1.5)
        tp2 = price + (risk * 2.5)

        return stop_loss, tp1, tp2

    if signal == "🔴 SELL":
        stop_loss = price + (atr * 1.5)

        risk = stop_loss - price

        tp1 = price - (risk * 1.5)
        tp2 = price - (risk * 2.5)

        return stop_loss, tp1, tp2

    return None, None, None


def estimate_duration(price, tp2, atr):
    if atr <= 0 or tp2 is None:
        return "N/A"

    distance = abs(tp2 - price)

    candles = distance / atr

    minutes = candles * 15 * 1.5

    minutes = max(15, minutes)

    if minutes < 60:
        return f"تقریباً {math.ceil(minutes)} منٹ"

    hours = minutes / 60

    if hours < 24:
        return f"تقریباً {hours:.1f} گھنٹے"

    return f"تقریباً {hours / 24:.1f} دن"

def send_discord(message):
    if not WEBHOOK_URL:
        print("Discord webhook secret is not configured.")
        return

    # Discord ایک message میں زیادہ سے زیادہ 2000 characters قبول کرتا ہے۔
    # اس لیے بڑے message کو چھوٹے حصوں میں تقسیم کریں گے۔
    max_length = 1900

    parts = []
    current_part = ""

    sections = message.split("\n\n")

    for section in sections:
        section = section.strip()

        if not section:
            continue

        candidate = (
            current_part + "\n\n" + section
            if current_part
            else section
        )

        if len(candidate) <= max_length:
            current_part = candidate
        else:
            if current_part:
                parts.append(current_part)

            # اگر ایک section خود بھی بڑا ہو
            while len(section) > max_length:
                parts.append(section[:max_length])
                section = section[max_length:]

            current_part = section

    if current_part:
        parts.append(current_part)

    for index, part in enumerate(parts, start=1):
        response = requests.post(
            WEBHOOK_URL,
            json={"content": part},
            timeout=10
        )

        print(
            f"Discord response {index}/{len(parts)}:",
            response.status_code,
            response.text
        )

        response.raise_for_status()

    print("✅ Discord message sent successfully.")
 if __name__ == "__main__":
    main()

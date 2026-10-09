import os
import math
import time
import requests

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

COINS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "XRPUSDT", "DOGEUSDT", "ZENUSDT", "ZECUSDT",
]

# Public USDT perpetual market data from Bybit.
# This is Bybit Futures data, NOT Binance Futures data.
BITGET_URL = "https://api.bitget.com"
TIMEFRAME = "15m"          # Bybit interval: 15 minutes
CANDLE_LIMIT = 200
TIMEOUT = 12

# Balanced settings: not as strict as the original filter, but still confirmed.
MIN_SIGNAL_SCORE = 78
STOP_ATR_MULTIPLIER = 1.5
TP1_RR = 1.5
TP2_RR = 2.5
STRUCTURE_LOOKBACK = 20
STRUCTURE_BUFFER_ATR = 0.15
MAX_ENTRY_DISTANCE_ATR = 1.8
STRONG_VOLUME_RATIO = 1.10
WEAK_VOLUME_RATIO = 0.60
MIN_QUALITY_SCORE = 65


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
    volumes = [float(c[5]) for c in closed]
    price = closes[-1]

    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    rsi = calculate_rsi(closes)
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
    buy_obstacle = nr if nr is not None else (resistance if resistance > price else None)
    sell_obstacle = ns if ns is not None else (support if support < price else None)
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
    min_room_buy = 1.00 if bull_structure else 1.20
    min_room_sell = 1.00 if bear_structure else 1.20
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
    buy += 14 if adx >= 25 else (7 if adx >= 20 else 0)
    buy += 8 if vol_strong else 0
    buy += 6 if bull_structure else 0
    buy += 6 if enough_buy else 0
    buy += 4 if di_bull else 0
    buy += 4 if bull_candle_ok else 0
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
    sell += 14 if adx >= 25 else (7 if adx >= 20 else 0)
    sell += 8 if vol_strong else 0
    sell += 6 if bear_structure else 0
    sell += 6 if enough_sell else 0
    sell += 4 if di_bear else 0
    sell += 4 if bear_candle_ok else 0
    sell -= 15 if late_sell else 0
    sell -= 5 if vol_weak else 0
    sell = max(0, min(sell, 100))

    # Controlled leniency: structure OR aligned DI can support a trade;
    # ADX need not always be >=25 if volume or structure confirms it.
    buy_confirm = (
        (bull_ema or bull_structure_path)
        and bull_rsi_ok and enough_buy and not late_buy
        and (di_bull or bull_structure)
        and (adx >= 22 or vol_strong or bull_structure)
    )
    sell_confirm = (
        (bear_ema or bear_structure_path)
        and bear_rsi_ok and enough_sell and not late_sell
        and (di_bear or bear_structure)
        and (adx >= 22 or vol_strong or bear_structure)
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
    buy_quality += 30 if bull_ema else (20 if bull_structure_path else 0)
    sell_quality += 30 if bear_ema else (20 if bear_structure_path else 0)
    buy_quality += 15 if adx >= 20 and di_bull else 0
    sell_quality += 15 if adx >= 20 and di_bear else 0
    buy_quality += 15 if bull_rsi_ok else 0
    sell_quality += 15 if bear_rsi_ok else 0
    buy_quality += 15 if bull_structure else 0
    sell_quality += 15 if bear_structure else 0
    buy_quality += 10 if bull_candle_ok else 0
    sell_quality += 10 if bear_candle_ok else 0

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
    if kind == "BUY" and (
        buy_quality < MIN_QUALITY_SCORE or not bull_candle_ok or vol_weak
    ):
        kind, signal, score = "WAIT", "🟡 WAIT", min(max(buy, sell), 84)
    elif kind == "SELL" and (
        sell_quality < MIN_QUALITY_SCORE or not bear_candle_ok or vol_weak
    ):
        kind, signal, score = "WAIT", "🟡 WAIT", min(max(buy, sell), 84)

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
            "Strong Bullish Candle" if bullish_candle_confirmation
            else "Bullish Rejection" if bullish_rejection
            else "Not Confirmed"
        )
        entry_timing = "Ideal" if dist_ema <= 1.0 else "Acceptable"
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
            "Strong Bearish Candle" if bearish_candle_confirmation
            else "Bearish Rejection" if bearish_rejection
            else "Not Confirmed"
        )
        entry_timing = "Ideal" if dist_ema <= 1.0 else "Acceptable"
    else:
        entry_setup = "No Confirmed Setup"
        candle_status = (
            "Partial Confirmation" if bull_candle_ok or bear_candle_ok
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

    return {
        "price": price,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi,
        "adx": adx,
        "plus_di": pdi,
        "minus_di": mdi,
        "atr": atr,
        "trend": trend,
        "signal": signal,
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
        "nearest_support": ns,
        "nearest_resistance": nr,
        "reason": " + ".join(reasons),
    }


def calculate_trade_levels(price, atr, signal, support, resistance, ns, nr):
    if atr <= 0:
        return None, None, None

    if signal == "🟢 BUY":
        sl = price - atr * STOP_ATR_MULTIPLIER
        risk = price - sl
        raw1 = price + risk * TP1_RR
        raw2 = price + risk * TP2_RR
        obstacle = nr if nr is not None else (resistance if resistance > price else None)
        if obstacle is not None:
            level = obstacle - atr * STRUCTURE_BUFFER_ATR
            room_rr = (level - price) / risk if level > price else 0.0
            # A structural signal may have as little as 1R of room; all other
            # signals require 1.2R. Keep TP1 before the next visible barrier.
            minimum_room = 1.00 if obstacle is not None else 1.20
            if room_rr >= minimum_room:
                raw1 = min(raw1, level)
        return sl, raw1, raw2

    if signal == "🔴 SELL":
        sl = price + atr * STOP_ATR_MULTIPLIER
        risk = sl - price
        raw1 = price - risk * TP1_RR
        raw2 = price - risk * TP2_RR
        obstacle = ns if ns is not None else (support if support < price else None)
        if obstacle is not None:
            level = obstacle + atr * STRUCTURE_BUFFER_ATR
            room_rr = (price - level) / risk if level < price else 0.0
            minimum_room = 1.00 if obstacle is not None else 1.20
            if room_rr >= minimum_room:
                raw1 = max(raw1, level)
        return sl, raw1, raw2

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
                result["nearest_support"], result["nearest_resistance"],
            )
            duration = estimate_duration(price, tp2, atr)

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

            messages.append(
                f"**{symbol}**\n"
                f"📡 Data: Bitget USDT Perpetual\n"
                f"💰 Entry: {price:.8g}\n"
                f"📈 Trend: {result['trend']}\n"
                f"📊 RSI: {result['rsi']:.2f}\n"
                f"〽️ EMA20: {result['ema20']:.8g}\n"
                f"〽️ EMA50: {result['ema50']:.8g}\n"
                f"💪 ADX: {result['adx']:.2f}\n"
                f"↗️ +DI: {result['plus_di']:.2f} | ↘️ -DI: {result['minus_di']:.2f}\n"
                f"📦 Volume: {result['volume_ratio']:.2f}x\n"
                f"🎯 Signal: {signal}\n"
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
                f"📝 Reason: {result['reason']}"
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

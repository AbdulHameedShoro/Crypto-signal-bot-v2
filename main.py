import os
import math
import requests

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

COINS = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","DOGEUSDT","ZENUSDT","ZECUSDT"]

# Binance USDⓈ-M Futures
BINANCE_URL = "https://data-api.binance.vision"
TIMEFRAME = "15m"
CANDLE_LIMIT = 200

MIN_SIGNAL_SCORE = 80
STOP_ATR_MULTIPLIER = 1.5
TP1_RR = 1.5
TP2_RR = 2.5
STRUCTURE_LOOKBACK = 20
STRUCTURE_BUFFER_ATR = 0.15
MAX_ENTRY_DISTANCE_ATR = 1.5
STRONG_VOLUME_RATIO = 1.10
WEAK_VOLUME_RATIO = 0.60
TIMEOUT = 10


def get_klines(symbol):
    r = requests.get(
        url = f"{BINANCE_URL}/api/v3/klines",
        params={
            "symbol": symbol,
            "interval": TIMEFRAME,
            "limit": CANDLE_LIMIT,
        },
        timeout=TIMEOUT,
    )

    if r.status_code != 200:
        raise ValueError(
            f"Binance Futures API error for {symbol}: "
            f"HTTP {r.status_code} | {r.text[:300]}"
        )

    try:
        data = r.json()
    except ValueError:
        raise ValueError(
            f"Binance Futures returned invalid response for {symbol}: "
            f"{r.text[:300]}"
        )

    if not isinstance(data, list) or len(data) < 60:
        raise ValueError(
            f"Not enough Futures candle data for {symbol}"
        )

    return data
def calculate_ema(values, period):
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
        return 100.0
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

        plus_dm.append(
            up if up > down and up > 0 else 0.0
        )
        minus_dm.append(
            down if down > up and down > 0 else 0.0
        )

    atr = sum(trs[:period])
    plus = sum(plus_dm[:period])
    minus = sum(minus_dm[:period])

    dxs = []

    for i in range(period, len(trs)):
        atr = atr - atr / period + trs[i]
        plus = plus - plus / period + plus_dm[i]
        minus = minus - minus / period + minus_dm[i]

        if atr <= 0:
            continue

        pdi = plus / atr * 100
        mdi = minus / atr * 100
        total = pdi + mdi

        if total > 0:
            dxs.append(abs(pdi - mdi) / total * 100)
        else:
            dxs.append(0.0)

    if len(dxs) < period:
        return 0.0, 0.0, 0.0

    adx = sum(dxs[:period]) / period

    for dx in dxs[period:]:
        adx = ((adx * (period - 1)) + dx) / period

    return adx, pdi, mdi
    

def get_support_resistance(highs, lows, lookback=20):
    return min(lows[-lookback - 1:-1]), max(highs[-lookback - 1:-1])


def nearest_support(lows, price, atr, lookback=30):
    start = max(0, len(lows) - lookback - 1)
    levels = []
    for low in lows[start:-1]:
        if low < price and (atr <= 0 or (price - low) / atr >= 0.25):
            levels.append(low)
    return max(levels) if levels else None


def nearest_resistance(highs, price, atr, lookback=30):
    start = max(0, len(highs) - lookback - 1)
    levels = []
    for high in highs[start:-1]:
        if high > price and (atr <= 0 or (high - price) / atr >= 0.25):
            levels.append(high)
    return min(levels) if levels else None


def get_signal(symbol):
    candles = get_klines(symbol)
    closed = candles[:-1]  # running candle excluded

    opens = [float(c[1]) for c in closed]
    closes = [float(c[4]) for c in closed]
    highs = [float(c[2]) for c in closed]
    lows = [float(c[3]) for c in closed]
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
    vol_ratio = volumes[-1] / avg_vol if avg_vol else 0.0
    vol_strong = vol_ratio >= STRONG_VOLUME_RATIO
    vol_weak = vol_ratio < WEAK_VOLUME_RATIO

    support, resistance = get_support_resistance(highs, lows)
    ns = nearest_support(lows, price, atr)
    nr = nearest_resistance(highs, price, atr)

    bull_ema = price > ema20 and ema20 > ema50 and ema20_up
    bear_ema = price < ema20 and ema20 < ema50 and ema20_down

    prev_res = max(highs[-21:-1])
    prev_sup = min(lows[-21:-1])
    bull_break = price > prev_res and closes[-2] <= prev_res
    bear_break = price < prev_sup and closes[-2] >= prev_sup
    bull_retest = lows[-1] <= prev_res and price > prev_res
    bear_retest = highs[-1] >= prev_sup and price < prev_sup
    bull_structure = bull_break or bull_retest
    bear_structure = bear_break or bear_retest

    dist_ema = abs(price - ema20) / atr if atr > 0 else 0

    late_buy = (
        price > ema20
        and dist_ema > MAX_ENTRY_DISTANCE_ATR
    )

    late_sell = (
        price < ema20
        and dist_ema > MAX_ENTRY_DISTANCE_ATR
    )

    bull_rsi = 52 <= rsi <= 68
    bear_rsi = 32 <= rsi <= 48

    bull_rsi_continuation = (
        rsi > 68
        and adx >= 30
        and di_bull
        and vol_strong
        and bull_ema
        and bull_momentum
        and not late_buy
    )

    bear_rsi_continuation = (
        rsi < 32
        and adx >= 30
        and di_bear
        and vol_strong
        and bear_ema
        and bear_momentum
        and not late_sell
    )

    bull_rsi_ok = (
        bull_rsi
        or bull_rsi_continuation
    )

    bear_rsi_ok = (
        bear_rsi
        or bear_rsi_continuation
    )

    strong_trend = adx >= 25

    res_dist = (resistance - price) / atr if atr > 0 else 0
    sup_dist = (price - support) / atr if atr > 0 else 0
    enough_buy = res_dist >= 0.75 or bull_break
    enough_sell = sup_dist >= 0.75 or bear_break

    buy = 0
    buy += 15 if price > ema20 else 0
    buy += 15 if ema20 > ema50 else 0
    buy += 10 if ema20_up else 0
    buy += 5 if ema50_up else 0
    buy += 15 if bull_rsi_ok else 0
    buy += 10 if bull_momentum else 0
    buy += 15 if adx >= 25 else (8 if adx >= 20 else 0)
    buy += 10 if vol_strong else 0
    buy += 5 if bull_structure else 0
    buy += 5 if enough_buy else 0
    buy -= 15 if late_buy else 0
    buy -= 5 if vol_weak else 0
    buy = max(0, min(buy, 100))

    sell = 0
    sell += 15 if price < ema20 else 0
    sell += 15 if ema20 < ema50 else 0
    sell += 10 if ema20_down else 0
    sell += 5 if ema50_down else 0
    sell += 15 if bear_rsi_ok else 0
    sell += 10 if bear_momentum else 0
    sell += 15 if adx >= 25 else (8 if adx >= 20 else 0)
    sell += 10 if vol_strong else 0
    sell += 5 if bear_structure else 0
    sell += 5 if enough_sell else 0
    sell -= 15 if late_sell else 0
    sell -= 5 if vol_weak else 0
    sell = max(0, min(sell, 100))

    buy_confirm = (
        bull_ema
        and bull_rsi_ok
        and enough_buy
        and not late_buy
        and (strong_trend or vol_strong or bull_structure)
    )

    sell_confirm = (
        bear_ema
        and bear_rsi_ok
        and enough_sell
        and not late_sell
        and (strong_trend or vol_strong or bear_structure)
    )
    if buy >= MIN_SIGNAL_SCORE and buy > sell and buy_confirm:
        signal, kind, score = "🟢 BUY", "BUY", buy
    elif sell >= MIN_SIGNAL_SCORE and sell > buy and sell_confirm:
        signal, kind, score = "🔴 SELL", "SELL", sell
    else:
        signal, kind, score = "🟡 WAIT", "WAIT", min(max(buy, sell), 84)
    # =========================
    # ENTRY QUALITY ANALYSIS
    # =========================

    last_open = opens[-1]
    last_close = closes[-1]
    last_high = highs[-1]
    last_low = lows[-1]

    candle_range = last_high - last_low
    candle_body = abs(last_close - last_open)

    upper_wick = last_high - max(last_open, last_close)
    lower_wick = min(last_open, last_close) - last_low

    body_ratio = (
        candle_body / candle_range
        if candle_range > 0 else 0
    )

    close_position = (
        (last_close - last_low) / candle_range
        if candle_range > 0 else 0.5
    )

    # Strong candle confirmation
    bullish_candle_confirmation = (
        last_close > last_open
        and body_ratio >= 0.45
        and close_position >= 0.65
    )

    bearish_candle_confirmation = (
        last_close < last_open
        and body_ratio >= 0.45
        and close_position <= 0.35
    )

    # Rejection candle confirmation
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

    bull_candle_ok = (
        bullish_candle_confirmation
        or bullish_rejection
    )

    bear_candle_ok = (
        bearish_candle_confirmation
        or bearish_rejection
    )

    # Ideal entry distance
    ideal_entry_distance = dist_ema <= 1.0

    # Directional Entry Quality Score
    
    # BALANCED DIRECTIONAL ENTRY QUALITY

    buy_quality = 0
    sell_quality = 0

    # EMA trend
    buy_quality += 30 if bull_ema else 0
    sell_quality += 30 if bear_ema else 0

    # ADX and directional strength
    buy_quality += 15 if adx >= 20 and di_bull else 0
    sell_quality += 15 if adx >= 20 and di_bear else 0

    # RSI momentum
    buy_quality += 15 if bull_rsi_ok else 0
    sell_quality += 15 if bear_rsi_ok else 0

    # Market structure
    buy_quality += 15 if bull_structure else 0
    sell_quality += 15 if bear_structure else 0

    # Candle confirmation
    buy_quality += 10 if bull_candle_ok else 0
    sell_quality += 10 if bear_candle_ok else 0

    # Volume confirmation
    volume_points = 15 if vol_strong else (
        8 if vol_ratio >= WEAK_VOLUME_RATIO else 0
    )
    buy_quality += volume_points
    sell_quality += volume_points

    # Penalties
    buy_quality -= 10 if late_buy else 0
    sell_quality -= 10 if late_sell else 0

    buy_quality -= 10 if not enough_buy else 0
    sell_quality -= 10 if not enough_sell else 0

    buy_quality = max(0, min(100, buy_quality))
    sell_quality = max(0, min(100, sell_quality))

    if kind == "BUY":
        entry_quality_score = buy_quality
    elif kind == "SELL":
        entry_quality_score = sell_quality
    else:
        entry_quality_score = max(buy_quality, sell_quality)

    if entry_quality_score >= 90:
        entry_quality = "A+"
    elif entry_quality_score >= 80:
        entry_quality = "A"
    elif entry_quality_score >= 70:
        entry_quality = "B"
    else:
        entry_quality = "C"

    # Final entry filter

    if kind == "BUY" and (
        buy_quality < 70 or not bull_candle_ok or vol_weak
    ):
        signal = "🟡 WAIT"
        kind = "WAIT"
        score = min(max(buy, sell), 84)

    elif kind == "SELL" and (
        sell_quality < 70 or not bear_candle_ok or vol_weak
    ):
        signal = "🟡 WAIT"
        kind = "WAIT"
        score = min(max(buy, sell), 84)

    if kind == "WAIT":
        entry_quality_score = max(buy_quality, sell_quality)

        if entry_quality_score >= 90:
            entry_quality = "A+"
        elif entry_quality_score >= 80:
            entry_quality = "A"
        elif entry_quality_score >= 70:
            entry_quality = "B"
        else:
            entry_quality = "C"
            
    # Setup description
    if kind == "BUY":
        if bull_break and bull_retest:
            entry_setup = "Breakout + Retest"
        elif bull_break:
            entry_setup = "Breakout"
        elif bull_retest:
            entry_setup = "Retest"
        elif bull_structure:
            entry_setup = "Bullish Structure"
        else:
            entry_setup = "Trend Continuation"

        if bullish_candle_confirmation:
            candle_status = "Strong Bullish Candle"
        elif bullish_rejection:
            candle_status = "Bullish Rejection"
        else:
            candle_status = "Candle Not Fully Confirmed"

    elif kind == "SELL":
        if bear_break and bear_retest:
            entry_setup = "Breakdown + Retest"
        elif bear_break:
            entry_setup = "Breakdown"
        elif bear_retest:
            entry_setup = "Retest"
        elif bear_structure:
            entry_setup = "Bearish Structure"
        else:
            entry_setup = "Trend Continuation"

        if bearish_candle_confirmation:
            candle_status = "Strong Bearish Candle"
        elif bearish_rejection:
            candle_status = "Bearish Rejection"
        else:
            candle_status = "Candle Not Fully Confirmed"

    else:
        entry_setup = "No Confirmed Setup"

        if bull_candle_ok or bear_candle_ok:
            candle_status = "Partial Confirmation"
        else:
            candle_status = "Not Confirmed"

    
    if kind == "WAIT":
        entry_timing = "Not Applicable"
    elif ideal_entry_distance:
        entry_timing = "Ideal"
    elif not (late_buy or late_sell):
        entry_timing = "Acceptable"
    else:
        entry_timing = "Late"
    
    if kind == "BUY":
        reasons = ["Bullish trend", "EMA alignment", "RSI momentum", "ADX strength"]
        if vol_strong: reasons.append("Volume confirmation")
        if bull_structure: reasons.append("Breakout/Retest")
        if vol_weak: reasons.append("Low volume risk")
    elif kind == "SELL":
        reasons = ["Bearish trend", "EMA alignment", "RSI momentum", "ADX strength"]
        if vol_strong: reasons.append("Volume confirmation")
        if bear_structure: reasons.append("Breakdown/Retest")
        if vol_weak: reasons.append("Low volume risk")
    else:
        reasons = []
        if not strong_trend: reasons.append("ADX weak")
        if vol_weak: reasons.append("Volume very weak")
        elif not vol_strong: reasons.append("Volume below strong level")
        if not bull_ema and not bear_ema: reasons.append("EMA trend unclear")
        if not bull_rsi_ok and not bear_rsi_ok: reasons.append("RSI not confirmed")
        if late_buy or late_sell: reasons.append("Entry too late")
        if not enough_buy and not enough_sell: reasons.append("Near support/resistance")
        if not reasons: reasons.append("Full confirmation not available")

    return {
        "price": price, "ema20": ema20, "ema50": ema50, "rsi": rsi,
        "adx": adx, "atr": atr, "trend": (
            "Strong Bullish" if bull_ema else
            "Strong Bearish" if bear_ema else
            "Bullish" if price > ema20 and ema20 > ema50 else
            "Bearish" if price < ema20 and ema20 < ema50 else "Neutral"
        ),
        "signal": signal, "signal_type": kind, "signal_score": score,
        "buy_score": buy,
        "sell_score": sell,
        "volume_ratio": vol_ratio,
        "entry_quality": entry_quality,
        "entry_quality_score": entry_quality_score,
        "entry_setup": entry_setup,
        "candle_status": candle_status,
        "entry_timing": entry_timing,
        "support": support, "resistance": resistance,
        "nearest_support": ns, "nearest_resistance": nr,
        "reason": " + ".join(reasons),
    }


def calculate_trade_levels(price, atr, signal, support, resistance, ns, nr):
    if atr <= 0:
        return None, None, None

    if signal == "🟢 BUY":
        sl = price - atr * STOP_ATR_MULTIPLIER
        risk = price - sl
        raw1, raw2 = price + risk * TP1_RR, price + risk * TP2_RR
        level = (nr if nr is not None else resistance) - atr * STRUCTURE_BUFFER_ATR
        rr = (level - price) / risk if level > price else 0
        tp1 = min(raw1, level) if rr >= 1.20 else raw1
        return sl, tp1, raw2

    if signal == "🔴 SELL":
        sl = price + atr * STOP_ATR_MULTIPLIER
        risk = sl - price
        raw1, raw2 = price - risk * TP1_RR, price - risk * TP2_RR
        level = (ns if ns is not None else support) + atr * STRUCTURE_BUFFER_ATR
        rr = (price - level) / risk if level < price else 0
        tp1 = max(raw1, level) if rr >= 1.20 else raw1
        return sl, tp1, raw2

    return None, None, None


def estimate_duration(price, tp2, atr):
    if atr <= 0 or tp2 is None:
        return "N/A"
    minutes = max(15, abs(tp2 - price) / atr * 15 * 1.5)
    if minutes < 60:
        return f"تقریباً {math.ceil(minutes)} منٹ"
    hours = minutes / 60
    if hours < 24:
        return f"تقریباً {hours:.1f} گھنٹے"
    return f"تقریباً {hours / 24:.1f} دن"


def send_discord(message):
    if not WEBHOOK_URL:
        print("❌ DISCORD_WEBHOOK_URL is not configured.")
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
        print(response.text)
        response.raise_for_status()

    print("✅ Discord message sent successfully.")


def main():
    print("🚀 Crypto Signal Bot Futures V2 started")
    messages = []

    for symbol in COINS:
        try:
            print(f"📊 Checking {symbol}...")
            result = get_signal(symbol)
            price, atr, signal = result["price"], result["atr"], result["signal"]

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
                levels = "🛑 Stop Loss: N/A\n🎯 TP1: N/A\n🎯 TP2: N/A\n"

            support_text = f"{result['nearest_support']:.8g}" if result["nearest_support"] is not None else "N/A"
            resistance_text = f"{result['nearest_resistance']:.8g}" if result["nearest_resistance"] is not None else "N/A"

            messages.append(
                f"**{symbol}**\n"
                f"💰 Entry: {price:.8g}\n"
                f"📈 Trend: {result['trend']}\n"
                f"📊 RSI: {result['rsi']:.2f}\n"
                f"〽️ EMA20: {result['ema20']:.8g}\n"
                f"〽️ EMA50: {result['ema50']:.8g}\n"
                f"💪 ADX: {result['adx']:.2f}\n"
                f"📦 Volume: {result['volume_ratio']:.2f}x\n"
                f"🎯 Signal: {signal}\n"
                f"💯 Signal Score: {result['signal_score']}/100\n"
                f"⭐ Entry Quality: {result['entry_quality']} ({result['entry_quality_score']}/100)\n"
                f"📌 Setup: {result['entry_setup']}\n"
                f"🕯️ Candle: {result['candle_status']}\n"
                f"🎯 Entry Timing: {result['entry_timing']}\n"
                f"📉 Support: {support_text}\n"
                f"📈 Resistance: {resistance_text}\n"
                f"{levels}"
                f"⏱️ Expected: {duration}\n"
                f"📝 Reason: {result['reason']}"
            )

            print(f"✅ {symbol}: {signal} | Score={result['signal_score']}")

        except Exception as error:
            print(f"❌ {symbol} error: {error}")

    if messages:
        send_discord("\n\n".join(messages))
    else:
        print("❌ No results were generated.")


if __name__ == "__main__":
    main()

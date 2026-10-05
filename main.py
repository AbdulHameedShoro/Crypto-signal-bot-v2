import os
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
    "ZECUSDT",]

BINANCE_URL = "https://data-api.binance.vision"


def get_klines(symbol, interval="15m", limit=100):
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
    multiplier = 2 / (period + 1)
    ema = values[0]

    for price in values[1:]:
        ema = (price - ema) * multiplier + ema

    return ema


def calculate_rsi(values, period=14):
    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def get_signal(symbol):
    candles = get_klines(symbol, interval="15m", limit=150)

    closes = [float(candle[4]) for candle in candles]
    highs = [float(candle[2]) for candle in candles]
    lows = [float(candle[3]) for candle in candles]
    volumes = [float(candle[5]) for candle in candles]

    price = closes[-1]

ema20 = calculate_ema(closes, 20)
ema50 = calculate_ema(closes, 50)
rsi = calculate_rsi(closes)

distance_from_ema20 = abs(price - ema20) / ema20 * 100
late_buy = price > ema20 and distance_from_ema20 > 0.8
late_sell = price < ema20 and distance_from_ema20 > 0.8
    # Previous values for trend strength
    ema20_prev = calculate_ema(closes[:-3], 20)
    ema50_prev = calculate_ema(closes[:-3], 50)

    avg_volume = sum(volumes[-21:-1]) / 20
    current_volume = volumes[-1]

    volume_strong = current_volume >= avg_volume * 1.15

    # Price momentum
    price_3_candles_ago = closes[-4]

    bullish_momentum = price > price_3_candles_ago
    bearish_momentum = price < price_3_candles_ago

    # EMA direction
    ema_bullish = ema20 > ema20_prev
    ema_bearish = ema20 < ema20_prev

    # Trend
    if price > ema20 and ema20 > ema50 and ema_bullish:
        trend = "Strong Bullish"
    elif price < ema20 and ema20 < ema50 and ema_bearish:
        trend = "Strong Bearish"
    elif price > ema20 and ema20 > ema50:
        trend = "Bullish"
    elif price < ema20 and ema20 < ema50:
        trend = "Bearish"
    else:
        trend = "Neutral"

    # Signal scoring
    buy_score = 0
    sell_score = 0

    # BUY conditions
    if price > ema20:
        buy_score += 20

    if ema20 > ema50:
        buy_score += 20

    if ema_bullish:
        buy_score += 15

    if 52 <= rsi <= 68:
        buy_score += 20

    if bullish_momentum:
        buy_score += 15

    if volume_strong:
        buy_score += 10

    # SELL conditions
    if price < ema20:
        sell_score += 20

    if ema20 < ema50:
        sell_score += 20

    if ema_bearish:
        sell_score += 15

    if 32 <= rsi <= 48:
        sell_score += 20

    if bearish_momentum:
        sell_score += 15

    if volume_strong:
        sell_score += 10

    # Strong signal threshold
    if buy_score >= 75 and buy_score > sell_score and not late_buy:
    signal = "🟢 BUY"
elif sell_score >= 75 and sell_score > buy_score and not late_sell:
    signal = "🔴 SELL"
else:
    signal = "🟡 WAIT"

    return price, ema20, ema50, rsi, trend, signal

def calculate_trade_levels(price, signal):
    if signal == "🟢 BUY":
        stop_loss = price * 0.985
        risk = price - stop_loss

        tp1 = price + (risk * 1.5)
        tp2 = price + (risk * 2.5)

        return stop_loss, tp1, tp2

    elif signal == "🔴 SELL":
        stop_loss = price * 1.015
        risk = stop_loss - price

        tp1 = price - (risk * 1.5)
        tp2 = price - (risk * 2.5)

        return stop_loss, tp1, tp2

    return None, None, None


def send_discord(message):
    if not WEBHOOK_URL:
        print("Discord webhook secret is not configured.")
        return

    response = requests.post(
        WEBHOOK_URL,
        json={"content": message},
        timeout=10
    )

    response.raise_for_status()


def main():
    message = "📊 **Crypto Signal Bot**\n\n"

    for symbol in COINS:
        try:
            price, ema20, ema50, rsi, trend, signal = get_signal(symbol)

            stop_loss, tp1, tp2 = calculate_trade_levels(
                price,
                signal
            )

            message += (
                f"**{symbol}**\n"
                f"💰 Entry: {price:.6f}\n"
                f"📈 Trend: {trend}\n"
                f"📊 RSI: {rsi:.2f}\n"
                f"〽️ EMA20: {ema20:.6f}\n"
                f"〽️ EMA50: {ema50:.6f}\n"
                f"🎯 Signal: {signal}\n"
            )

            if signal != "🟡 WAIT":
                message += (
                    f"🛑 Stop Loss: {stop_loss:.6f}\n"
                    f"🎯 TP1: {tp1:.6f}\n"
                    f"🎯 TP2: {tp2:.6f}\n"
                    f"⚖️ Risk/Reward: 1:{2.5 if signal else 0}\n"
                )

            message += "\n"

        except Exception as e:
            message += f"❌ {symbol}: Data error\n"
            print(f"{symbol}: {e}")

    print(message)
    send_discord(message)


if __name__ == "__main__":
    main()

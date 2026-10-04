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
]

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
    candles = get_klines(symbol)

    closes = [float(candle[4]) for candle in candles]

    price = closes[-1]
    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    rsi = calculate_rsi(closes)

    if price > ema20 and ema20 > ema50 and rsi >= 55 and rsi < 70:
        signal = "🟢 BUY"

    elif price < ema20 and ema20 < ema50 and rsi <= 45 and rsi > 30:
        signal = "🔴 SELL"

    else:
        signal = "🟡 WAIT"

    if price > ema20 and ema20 > ema50:
        trend = "Bullish"
    elif price < ema20 and ema20 < ema50:
        trend = "Bearish"
    else:
        trend = "Neutral"

    return price, ema20, ema50, rsi, trend, signal


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

            message += (
                f"**{symbol}**\n"
                f"💰 Price: {price:.6f}\n"
                f"📈 Trend: {trend}\n"
                f"📊 RSI: {rsi:.2f}\n"
                f"〽️ EMA20: {ema20:.6f}\n"
                f"〽️ EMA50: {ema50:.6f}\n"
                f"🎯 Signal: {signal}\n\n"
            )

        except Exception as e:
            message += f"❌ {symbol}: Data error\n"
            print(f"{symbol}: {e}")

    print(message)
    send_discord(message)


if __name__ == "__main__":
    main()

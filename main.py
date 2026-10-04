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

def get_price(symbol):
    url = f"https://data-api.binance.vision/api/v3/ticker/price?symbol={symbol}"
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return float(response.json()["price"])

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
    message = "📊 Crypto Signal Bot\n\n"

    for symbol in COINS:
        try:
            price = get_price(symbol)
            message += f"🟡 {symbol}: {price}\n"
        except Exception as e:
            message += f"❌ {symbol}: Data error\n"
            print(e)

    print(message)
    send_discord(message)

if __name__ == "__main__":
    main()

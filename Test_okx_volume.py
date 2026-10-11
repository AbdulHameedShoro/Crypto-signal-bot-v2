
import requests
from datetime import datetime, timezone

TIMEFRAME_MS = 15 * 60 * 1000


def get_bitget():
    url = "https://api.bitget.com/api/v2/mix/market/candles"
    params = {
        "symbol": "BTCUSDT",
        "productType": "USDT-FUTURES",
        "granularity": "15m",
        "limit": "100",
    }
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    result = response.json()

    if result.get("code") != "00000":
        raise RuntimeError(f"Bitget API: {result}")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    candles = {}

    for c in result["data"]:
        ts = int(c[0])
        if ts + TIMEFRAME_MS <= now_ms:
            candles[ts] = float(c[5])

    return candles


def get_okx():
    url = "https://www.okx.com/api/v5/market/candles"
    params = {
        "instId": "BTC-USDT-SWAP",
        "bar": "15m",
        "limit": "100",
    }
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    result = response.json()

    if result.get("code") != "0":
        raise RuntimeError(f"OKX API: {result}")

    candles = {}

    for c in result["data"]:
        if c[8] == "1":
            candles[int(c[0])] = float(c[6])

    return candles


def compare():
    bitget = get_bitget()
    okx = get_okx()

    common = sorted(set(bitget) & set(okx))

    if len(common) < 21:
        raise RuntimeError(
            f"Not enough matching candles: {len(common)}"
        )

    times = common[-21:]
    last_ts = times[-1]

    def stats(data):
        last = data[last_ts]
        average = sum(data[t] for t in times[:-1]) / 20
        ratio = last / average if average > 0 else 0
        return last, average, ratio

    bg_last, bg_avg, bg_ratio = stats(bitget)
    ok_last, ok_avg, ok_ratio = stats(okx)

    candle_time = datetime.fromtimestamp(
        last_ts / 1000, timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    print("BTCUSDT FUTURES VOLUME COMPARISON")
    print(f"CANDLE_OPEN={candle_time}")
    print(f"MATCHED_CANDLES={len(common)}")

    print(
        f"BITGET | LAST={bg_last:.4f} | "
        f"AVG20={bg_avg:.4f} | RATIO={bg_ratio:.3f}"
    )

    print(
        f"OKX | LAST={ok_last:.4f} | "
        f"AVG20={ok_avg:.4f} | RATIO={ok_ratio:.3f}"
    )

    if bg_ratio < 0.60 and ok_ratio >= 0.60:
        print("RESULT: Bitget weak, OKX confirms volume")
    elif bg_ratio < 0.60 and ok_ratio < 0.60:
        print("RESULT: Both exchanges show weak volume")
    else:
        print("RESULT: Bitget volume is not weak")


if __name__ == "__main__":
    compare()
      

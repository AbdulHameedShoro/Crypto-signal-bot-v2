
import requests
from datetime import datetime, timezone

TIMEFRAME_MS = 15 * 60 * 1000
VOLUME_THRESHOLD = 0.60

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ZENUSDT",
    "ZECUSDT",
]


def get_bitget(symbol):
    url = "https://api.bitget.com/api/v2/mix/market/candles"

    params = {
        "symbol": symbol,
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

    for c in result.get("data", []):
        ts = int(c[0])

        if ts + TIMEFRAME_MS <= now_ms:
            candles[ts] = float(c[5])

    return candles


def get_okx(symbol):
    url = "https://www.okx.com/api/v5/market/candles"

    base = symbol.removesuffix("USDT")
    inst_id = f"{base}-USDT-SWAP"

    params = {
        "instId": inst_id,
        "bar": "15m",
        "limit": "100",
    }

    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()
    result = response.json()

    if result.get("code") != "0":
        raise RuntimeError(
            f"OKX {inst_id}: "
            f"{result.get('msg', 'Unknown error')}"
        )

    candles = {}

    for c in result.get("data", []):
        if str(c[8]) == "1":
            candles[int(c[0])] = float(c[6])

    return candles


def compare(symbol):
    bitget = get_bitget(symbol)
    okx = get_okx(symbol)

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

        if average <= 0:
            raise RuntimeError("Average volume is zero")

        ratio = last / average
        return last, average, ratio

    bg_last, bg_avg, bg_ratio = stats(bitget)
    ok_last, ok_avg, ok_ratio = stats(okx)

    candle_time = datetime.fromtimestamp(
        last_ts / 1000, timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    print(f"\n{'=' * 45}")
    print(f"{symbol} FUTURES VOLUME COMPARISON")
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

    if bg_ratio < VOLUME_THRESHOLD and ok_ratio >= VOLUME_THRESHOLD:
        status = "BITGET_WEAK_OKX_CONFIRMS"
    elif bg_ratio < VOLUME_THRESHOLD and ok_ratio < VOLUME_THRESHOLD:
        status = "BOTH_WEAK"
    elif bg_ratio >= VOLUME_THRESHOLD and ok_ratio < VOLUME_THRESHOLD:
        status = "BITGET_CONFIRMS_OKX_WEAK"
    else:
        status = "BOTH_CONFIRM"

    print(f"RESULT: {status}")

    return {
        "symbol": symbol,
        "bitget": bg_ratio,
        "okx": ok_ratio,
        "status": status,
        "time": last_ts,
    }


def main():
    results = []
    failed = []

    print("BITGET VS OKX FUTURES VOLUME TEST")
    print(f"THRESHOLD={VOLUME_THRESHOLD}")
    print(f"SYMBOLS={len(SYMBOLS)}")

    for symbol in SYMBOLS:
        try:
            result = compare(symbol)
            results.append(result)

        except (requests.RequestException, ValueError,
                KeyError, IndexError, RuntimeError) as error:
            print(f"\n{symbol}: UNAVAILABLE | {error}")
            failed.append(symbol)

    print(f"\n{'=' * 45}")
    print("FINAL SUMMARY")
    print("SYMBOL | BITGET | OKX | RESULT")

    for r in results:
        print(
            f"{r['symbol']} | "
            f"{r['bitget']:.3f} | "
            f"{r['okx']:.3f} | "
            f"{r['status']}"
        )

    print(f"\nSUCCESSFUL={len(results)}")
    print(f"UNAVAILABLE={len(failed)}")

    if failed:
        print(f"FAILED_SYMBOLS={','.join(failed)}")


if __name__ == "__main__":
    main()
    

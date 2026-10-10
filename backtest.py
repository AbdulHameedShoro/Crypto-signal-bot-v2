

import csv
import time
from datetime import datetime, timedelta, timezone

import requests
import main_combined_futures as bot

BASE_URL = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"
GRANULARITY = "15m"
CANDLE_MS = 15 * 60 * 1000

DAYS_BACK = 30
PAGE_LIMIT = 200
MIN_BARS = 100

# Estimated costs per side. Adjust to your actual fee tier.
FEE_RATE = 0.0006
SLIPPAGE_RATE = 0.0002

TIMEOUT = 15


def fetch_history(symbol):
    """Download historical Bitget Futures candles."""
    cutoff_ms = int(
        (datetime.now(timezone.utc) -
         timedelta(days=DAYS_BACK)).timestamp() * 1000
    )
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    all_rows = {}
    session = requests.Session()

    while end_ms > cutoff_ms:
        response = session.get(
            f"{BASE_URL}/api/v2/mix/market/history-candles",
            params={
                "symbol": symbol,
                "productType": PRODUCT_TYPE,
                "granularity": GRANULARITY,
                "limit": str(PAGE_LIMIT),
                "endTime": str(end_ms),
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()

        if not isinstance(payload, dict):
            raise ValueError("Unexpected API response")

        if payload.get("code") != "00000":
            raise ValueError(
                f"Bitget API error: {payload.get('code')} "
                f"{payload.get('msg')}"
            )

        rows = payload.get("data", [])

        if not rows:
            break

        valid_rows = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            try:
                timestamp = int(row[0])
                if cutoff_ms <= timestamp:
                    valid_rows.append(row)
            except (TypeError, ValueError):
                continue

        for row in valid_rows:
            all_rows[int(row[0])] = row

        oldest = min(int(row[0]) for row in rows)

        if oldest <= cutoff_ms:
            break

        if oldest >= end_ms:
            break

        end_ms = oldest - 1
        time.sleep(0.15)

    session.close()

    candles = [
        all_rows[timestamp]
        for timestamp in sorted(all_rows)
    ]

    # Never use a still-open candle.
    now_ms = int(time.time() * 1000)
    candles = [
        row for row in candles
        if int(row[0]) + CANDLE_MS <= now_ms
    ]

    if len(candles) < MIN_BARS:
        raise ValueError(
            f"Only {len(candles)} historical candles available"
        )

    print(
        f"{symbol}: downloaded {len(candles)} closed candles"
    )
    return candles


def backtest_symbol(symbol, candles):
    """Reuse the existing strategy without live data look-ahead."""

    trades = []
    index = MIN_BARS

    # get_signal() will only see candles available at that moment.
    def historical_klines(requested_symbol):
        if requested_symbol != symbol:
            raise ValueError("Unexpected symbol")
        return candles[:current_index[0] + 1]

    # Disable live higher-timeframe requests during historical testing.
    # The current strategy does not use these values to approve trades.
    bot.get_klines = historical_klines
    bot.get_higher_timeframe_trend = (
        lambda symbol, granularity, duration_ms: "UNKNOWN"
    )

    current_index = [0]

    while index < len(candles) - 1:
        current_index[0] = index

        try:
            result = bot.get_signal(symbol)
        except Exception as error:
            print(f"{symbol} candle {index}: {error}")
            index += 1
            continue

        if not result:
            index += 1
            continue

        signal = result["signal"]

        if signal not in ("🟢 BUY", "🔴 SELL"):
            index += 1
            continue

        # Signal is generated after candle[index] closes.
        # Simulate entry at the next candle's open.
        entry_index = index + 1
        entry = float(candles[entry_index][1])

        sl, tp1, tp2 = bot.calculate_trade_levels(
            entry,
            result["atr"],
            signal,
            result["support"],
            result["resistance"],
            result["nearest_support"],
            result["nearest_resistance"],
        )

        if sl is None or tp1 is None or tp2 is None:
            index += 1
            continue

        is_buy = signal == "🟢 BUY"
        exit_price = None
        outcome = None
        exit_index = None

        # TP1 is the primary test target.
        for j in range(entry_index, len(candles)):
            high = float(candles[j][2])
            low = float(candles[j][3])

            if is_buy:
                stop_hit = low <= sl
                target_hit = high >= tp1
            else:
                stop_hit = high >= sl
                target_hit = low <= tp1

            # Conservative assumption if both are hit in one candle.
            if stop_hit:
                exit_price = sl
                outcome = "SL"
                exit_index = j
                break

            if target_hit:
                exit_price = tp1
                outcome = "TP1"
                exit_index = j
                break

        if exit_price is None:
            # Still-open trades are not counted as completed.
            index += 1
            continue

        if is_buy:
            gross_return = (exit_price - entry) / entry
        else:
            gross_return = (entry - exit_price) / entry

        costs = 2 * (FEE_RATE + SLIPPAGE_RATE)
        net_return = gross_return - costs

        trades.append({
            "symbol": symbol,
            "side": "BUY" if is_buy else "SELL",
            "signal_time_utc": datetime.fromtimestamp(
                int(candles[index][0]) / 1000,
                tz=timezone.utc,
            ).isoformat(),
            "entry_time_utc": datetime.fromtimestamp(
                int(candles[entry_index][0]) / 1000,
                tz=timezone.utc,
            ).isoformat(),
            "entry": round(entry, 8),
            "stop_loss": round(sl, 8),
            "tp1": round(tp1, 8),
            "tp2": round(tp2, 8),
            "exit": round(exit_price, 8),
            "outcome": outcome,
            "signal_score": result["signal_score"],
            "quality_score": result["entry_quality_score"],
            "gross_return_pct": round(gross_return * 100, 4),
            "net_return_pct": round(net_return * 100, 4),
            "bars_held": exit_index - entry_index + 1,
        })

        # Do not open another trade while this simulated trade is active.
        index = exit_index + 1

    return trades


def save_report(trades):
    filename = "backtest_results.csv"

    if not trades:
        print("No completed trades. No CSV report created.")
        return

    with open(filename, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file, fieldnames=list(trades[0].keys())
        )
        writer.writeheader()
        writer.writerows(trades)

    wins = sum(t["net_return_pct"] > 0 for t in trades)
    losses = len(trades) - wins
    tp1_hits = sum(t["outcome"] == "TP1" for t in trades)
    sl_hits = sum(t["outcome"] == "SL" for t in trades)

    net_sum = sum(t["net_return_pct"] for t in trades)

    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0

    for trade in trades:
        equity *= 1 + trade["net_return_pct"] / 100
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak
        max_drawdown = max(max_drawdown, drawdown)

    win_rate = wins / len(trades) * 100

    print("\n===== BACKTEST SUMMARY =====")
    print(f"Completed trades: {len(trades)}")
    print(f"Profitable trades after estimated costs: {wins}")
    print(f"Losing trades after estimated costs: {losses}")
    print(f"TP1 hits: {tp1_hits}")
    print(f"SL hits: {sl_hits}")
    print(f"Net win rate: {win_rate:.2f}%")
    print(f"Sum of net trade returns: {net_sum:.2f}%")
    print(f"Compounded return: {(equity - 1) * 100:.2f}%")
    print(f"Maximum drawdown: {max_drawdown * 100:.2f}%")
    print(f"Estimated round-trip costs: "
          f"{2 * (FEE_RATE + SLIPPAGE_RATE) * 100:.3f}%")
    print(f"Detailed report: {filename}")


def main():
    print("Historical Backtest - Bitget USDT Perpetual")
    print(f"Timeframe: {GRANULARITY} | Lookback: {DAYS_BACK} days")
    all_trades = []

    for symbol in bot.COINS:
        try:
            print(f"\nDownloading {symbol}...")
            candles = fetch_history(symbol)
            trades = backtest_symbol(symbol, candles)
            all_trades.extend(trades)
            print(f"{symbol}: completed trades = {len(trades)}")
        except Exception as error:
            print(f"{symbol} FAILED: {error}")

    save_report(all_trades)


if __name__ == "__main__":
    main()

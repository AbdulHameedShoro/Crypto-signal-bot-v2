import csv
import time
from datetime import datetime, timedelta, timezone

import requests
import main_combined_futures as bot

BASE_URL = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"
GRANULARITY = "15m"
CANDLE_MS = 15 * 60 * 1000
DAY_MS = 24 * 60 * 60 * 1000

DAYS_BACK = 30
PAGE_LIMIT = 200
MIN_BARS = 100

FEE_RATE = 0.0006
SLIPPAGE_RATE = 0.0002
TIMEOUT = 15

RESULTS_FILE = "backtest_results.csv"
SUMMARY_FILE = "backtest_summary.csv"

TRADE_FIELDS = [
    "symbol", "side", "status", "signal_time_utc",
    "entry_time_utc", "entry", "stop_loss", "tp1", "tp2",
    "exit", "outcome", "signal_score", "quality_score",
    "gross_return_pct", "net_return_pct", "bars_held",
]


def utc_time(timestamp_ms):
    return datetime.fromtimestamp(
        timestamp_ms / 1000, tz=timezone.utc
    ).isoformat()


def fetch_history(symbol):
    """Download historical Bitget Futures candles."""
    cutoff_ms = int(
        (datetime.now(timezone.utc) -
         timedelta(days=DAYS_BACK)).timestamp() * 1000
    )
    end_ms = int(time.time() * 1000)
    all_rows = {}

    with requests.Session() as session:
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
                headers={"Accept": "application/json"},
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()

            if not isinstance(payload, dict):
                raise ValueError("Unexpected Bitget API response")

            if payload.get("code") != "00000":
                raise ValueError(
                    f"Bitget API error: {payload.get('code')} "
                    f"{payload.get('msg')}"
                )

            rows = payload.get("data", [])
            if not rows:
                break

            valid_timestamps = []

            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) < 6:
                    continue

                try:
                    timestamp = int(row[0])
                except (TypeError, ValueError):
                    continue

                valid_timestamps.append(timestamp)

                if cutoff_ms <= timestamp:
                    all_rows[timestamp] = row

            if not valid_timestamps:
                break

            oldest = min(valid_timestamps)

            if oldest <= cutoff_ms or oldest >= end_ms:
                break

            end_ms = oldest - 1
            time.sleep(0.15)

    now_ms = int(time.time() * 1000)

    candles = [
        all_rows[timestamp]
        for timestamp in sorted(all_rows)
        if timestamp + CANDLE_MS <= now_ms
    ]

    if len(candles) < MIN_BARS:
        raise ValueError(
            f"Only {len(candles)} closed candles available"
        )

    print(f"{symbol}: downloaded {len(candles)} closed candles")
    return candles


def backtest_symbol(symbol, candles):
    """Test the strategy using only information available at each candle."""

    trades = []
    index = MIN_BARS
    current_index = [0]

    def historical_klines(requested_symbol):
        if requested_symbol != symbol:
            raise ValueError("Unexpected symbol")
        return candles[:current_index[0] + 1]

    def historical_previous_day_levels(rows):
        """Use the previous UTC day relative to the simulated candle."""
        if not rows:
            return None, None

        latest_ms = int(rows[-1][0])
        current_day_start = (latest_ms // DAY_MS) * DAY_MS
        previous_day_start = current_day_start - DAY_MS

        previous_day = [
            row for row in rows
            if previous_day_start <= int(row[0]) < current_day_start
        ]

        if not previous_day:
            return None, None

        return (
            max(float(row[2]) for row in previous_day),
            min(float(row[3]) for row in previous_day),
        )

    # Replace live-data functions only during this backtest.
    bot.get_klines = historical_klines
    bot.get_previous_day_levels = historical_previous_day_levels
    bot.get_higher_timeframe_trend = (
        lambda symbol, granularity, duration_ms: "UNKNOWN"
    )

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

        signal = result.get("signal")

        if signal not in ("🟢 BUY", "🔴 SELL"):
            index += 1
            continue

        # Signal appears after candle close; entry is next candle open.
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

        for j in range(entry_index, len(candles)):
            high = float(candles[j][2])
            low = float(candles[j][3])

            if is_buy:
                stop_hit = low <= sl
                target_hit = high >= tp1
            else:
                stop_hit = high >= sl
                target_hit = low <= tp1

            # Conservative rule if SL and TP1 occur in the same candle.
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

        if is_buy:
            side = "BUY"
        else:
            side = "SELL"

        if exit_price is None:
            # The trade remains open: do not start another trade.
            last_index = len(candles) - 1
            mark_price = float(candles[last_index][4])

            if is_buy:
                gross_return = (mark_price - entry) / entry
            else:
                gross_return = (entry - mark_price) / entry

            trades.append({
                "symbol": symbol,
                "side": side,
                "status": "OPEN",
                "signal_time_utc": utc_time(int(candles[index][0])),
                "entry_time_utc": utc_time(
                    int(candles[entry_index][0])
                ),
                "entry": round(entry, 8),
                "stop_loss": round(sl, 8),
                "tp1": round(tp1, 8),
                "tp2": round(tp2, 8),
                "exit": round(mark_price, 8),
                "outcome": "OPEN",
                "signal_score": result["signal_score"],
                "quality_score": result["entry_quality_score"],
                "gross_return_pct": round(gross_return * 100, 4),
                "net_return_pct": round(
                    (gross_return - 2 * (FEE_RATE + SLIPPAGE_RATE))
                    * 100, 4
                ),
                "bars_held": last_index - entry_index + 1,
            })

            print(
                f"{symbol}: trade remains OPEN at end of historical data"
            )
            break

        if is_buy:
            gross_return = (exit_price - entry) / entry
        else:
            gross_return = (entry - exit_price) / entry

        costs = 2 * (FEE_RATE + SLIPPAGE_RATE)
        net_return = gross_return - costs

        trades.append({
            "symbol": symbol,
            "side": side,
            "status": "CLOSED",
            "signal_time_utc": utc_time(int(candles[index][0])),
            "entry_time_utc": utc_time(
                int(candles[entry_index][0])
            ),
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

        # No overlapping simulated trades.
        index = exit_index + 1

    return trades


def save_report(trades):
    # Always create the detailed CSV, even if no trades were completed.
    with open(RESULTS_FILE, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=TRADE_FIELDS)
        writer.writeheader()
        writer.writerows(trades)

    closed = [t for t in trades if t["status"] == "CLOSED"]
    open_trades = [t for t in trades if t["status"] == "OPEN"]

    wins = sum(t["net_return_pct"] > 0 for t in closed)
    losses = len(closed) - wins
    tp1_hits = sum(t["outcome"] == "TP1" for t in closed)
    sl_hits = sum(t["outcome"] == "SL" for t in closed)

    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0

    for trade in closed:
        equity *= 1 + trade["net_return_pct"] / 100
        peak = max(peak, equity)
        max_drawdown = max(
            max_drawdown, (peak - equity) / peak
        )

    win_rate = wins / len(closed) * 100 if closed else 0.0
    net_sum = sum(t["net_return_pct"] for t in closed)

    print("\n===== BACKTEST SUMMARY =====")
    print(f"Completed trades: {len(closed)}")
    print(f"Open trades at end: {len(open_trades)}")
    print(f"Profitable closed trades after estimated costs: {wins}")
    print(f"Losing closed trades after estimated costs: {losses}")
    print(f"TP1 hits: {tp1_hits}")
    print(f"SL hits: {sl_hits}")
    print(f"Net win rate: {win_rate:.2f}%")
    print(f"Sum of net trade returns: {net_sum:.2f}%")
    print(f"Compounded return: {(equity - 1) * 100:.2f}%")
    print(f"Maximum drawdown: {max_drawdown * 100:.2f}%")
    print(
        "Estimated round-trip costs: "
        f"{2 * (FEE_RATE + SLIPPAGE_RATE) * 100:.3f}%"
    )

    summary = [{
        "timeframe": GRANULARITY,
        "lookback_days": DAYS_BACK,
        "completed_trades": len(closed),
        "open_trades_at_end": len(open_trades),
        "profitable_trades": wins,
        "losing_trades": losses,
        "tp1_hits": tp1_hits,
        "sl_hits": sl_hits,
        "net_win_rate_pct": round(win_rate, 2),
        "sum_net_returns_pct": round(net_sum, 2),
        "compounded_return_pct": round((equity - 1) * 100, 2),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "estimated_round_trip_cost_pct": round(
            2 * (FEE_RATE + SLIPPAGE_RATE) * 100, 3
        ),
    }]

    with open(SUMMARY_FILE, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file, fieldnames=list(summary[0].keys())
        )
        writer.writeheader()
        writer.writerows(summary)

    print(f"Detailed report: {RESULTS_FILE}")
    print(f"Summary report: {SUMMARY_FILE}")


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
            print(
                f"{symbol}: completed="
                f"{sum(t['status'] == 'CLOSED' for t in trades)}, "
                f"open={sum(t['status'] == 'OPEN' for t in trades)}"
            )
        except Exception as error:
            print(f"{symbol} FAILED: {error}")

    save_report(all_trades)


if __name__ == "__main__":
    main()
    

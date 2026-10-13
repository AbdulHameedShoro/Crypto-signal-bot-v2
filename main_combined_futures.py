# -*- coding: utf-8 -*-
"""Crypto Signal Bot - balanced Bitget USDT perpetual signal scanner. Uses fully closed 15-minute candles. Sends alerts; does not place orders. """
import math
import os
import time
from datetime import datetime, timezone

import requests

WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL')
BITGET_URL = 'https://api.bitget.com'
COINS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT',
         'XRPUSDT', 'DOGEUSDT', 'ZENUSDT', 'ZECUSDT', 'CKBUSDT']
TIMEFRAME = '15m'
TIMEFRAME_MS = 15 * 60 * 1000
CANDLE_LIMIT = 200
TIMEOUT = 12

# Balanced baseline. Review losing trades before changing these values.
MIN_SIGNAL_SCORE = 55
MIN_SCORE_GAP = 10
MIN_ADX = 15
MIN_VOLUME_RATIO = 0.40
MAX_ENTRY_DISTANCE_ATR = 2.6  # caution threshold, not a signal veto
STOP_ATR_MULTIPLIER = 1.5
TP1_RR = 1.5
TP2_RR = 2.5


def api_data(endpoint, params):
    response = requests.get(BITGET_URL + endpoint, params=params,
                            headers={'Accept': 'application/json'}, timeout=TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get('code') != '00000':
        raise ValueError('Bitget API error: ' + str(payload)[:300])
    return payload.get('data')


def get_closed_candles(symbol, granularity='15m', duration_ms=TIMEFRAME_MS, limit=200):
    rows = api_data('/api/v2/mix/market/candles', {
        'symbol': symbol, 'productType': 'USDT-FUTURES',
        'granularity': granularity, 'limit': str(limit)})
    if not isinstance(rows, list):
        raise ValueError('Invalid candle response')
    now_ms = int(time.time() * 1000)
    result = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            start = int(row[0])
            o, h, l, c, v = map(float, row[1:6])
            if (start + duration_ms <= now_ms and
                all(math.isfinite(x) for x in (o, h, l, c, v)) and
                min(o, h, l, c) > 0 and v >= 0 and
                h >= max(o, c, l) and l <= min(o, c, h)):
                result.append((start, o, h, l, c, v))
        except (ValueError, TypeError, OverflowError):
            continue
    result.sort(key=lambda x: x[0])
    result = list({row[0]: row for row in result}.values())
    result.sort(key=lambda x: x[0])
    if len(result) < 60:
        raise ValueError('Insufficient fully closed candles')
    if now_ms - (result[-1][0] + duration_ms) > duration_ms * 3:
        raise ValueError('Stale candle data')
    return result


def ema(values, period):
    if len(values) < period:
        raise ValueError('Not enough EMA data')
    result = sum(values[:period]) / period
    k = 2.0 / (period + 1)
    for value in values[period:]:
        result += (value - result) * k
    return result


def rsi(values, period=14):
    gains, losses = [], []
    for previous, current in zip(values, values[1:]):
        gains.append(max(0, current - previous))
        losses.append(max(0, previous - current))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        ag = (ag * (period - 1) + gain) / period
        al = (al * (period - 1) + loss) / period
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return 100 - 100 / (1 + ag / al)


def volatility_and_direction(highs, lows, closes, period=14):
    trs, plus, minus = [], [], []
    for i in range(1, len(closes)):
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]),
                       abs(lows[i]-closes[i-1])))
        up = highs[i] - highs[i-1]
        down = lows[i-1] - lows[i]
        plus.append(up if up > down and up > 0 else 0.)
        minus.append(down if down > up and down > 0 else 0.)
    atr_sum = sum(trs[:period])
    plus_sum = sum(plus[:period])
    minus_sum = sum(minus[:period])
    dx_values = []
    pdi = mdi = 0.
    for i in range(period - 1, len(trs)):
        if i >= period:
            atr_sum += trs[i] - atr_sum / period
            plus_sum += plus[i] - plus_sum / period
            minus_sum += minus[i] - minus_sum / period
        if atr_sum <= 0:
            dx_values.append(0.)
            continue
        pdi = 100 * plus_sum / atr_sum
        mdi = 100 * minus_sum / atr_sum
        dx_values.append(100 * abs(pdi-mdi)/(pdi+mdi) if pdi+mdi else 0.)
    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx += (dx - adx) / period
    return atr_sum / period, adx, pdi, mdi


def higher_trend(symbol, granularity, duration):
    try:
        rows = get_closed_candles(symbol, granularity, duration, 100)
        closes = [r[4] for r in rows]
        e20, e50 = ema(closes, 20), ema(closes, 50)
        return ('BULLISH' if closes[-1] > e20 > e50 else
                'BEARISH' if closes[-1] < e20 < e50 else 'NEUTRAL')
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        print(f'HTF_WARN {symbol} {granularity}: {exc}')
        return 'UNKNOWN'


def grade(score):
    return 'A' if score >= 80 else 'B' if score >= 65 else 'C'


def scan(symbol):
    rows = get_closed_candles(symbol)
    opens = [r[1] for r in rows]
    highs = [r[2] for r in rows]
    lows = [r[3] for r in rows]
    closes = [r[4] for r in rows]
    volumes = [r[5] for r in rows]
    price = closes[-1]
    atr, adx, pdi, mdi = volatility_and_direction(highs, lows, closes)
    if atr <= 0:
        raise ValueError('Invalid ATR')
    e20, e50 = ema(closes, 20), ema(closes, 50)
    previous_e20 = ema(closes[:-3], 20)
    strength = rsi(closes)
    average_volume = sum(volumes[-21:-1]) / 20
    vol_ratio = volumes[-1] / average_volume if average_volume > 0 else 0.
    trend1 = higher_trend(symbol, '1H', 3600000)
    trend4 = higher_trend(symbol, '4H', 14400000)
    candle_range = max(highs[-1]-lows[-1], 1e-12)
    body = abs(closes[-1]-opens[-1]) / candle_range
    close_position = (closes[-1]-lows[-1]) / candle_range
    bullish_close = closes[-1] > opens[-1] and close_position >= .55
    bearish_close = closes[-1] < opens[-1] and close_position <= .45
    bullish_rejection = (close_position >= .65 and
                         min(opens[-1], closes[-1])-lows[-1] >= candle_range*.3)
    bearish_rejection = (close_position <= .35 and
                         highs[-1]-max(opens[-1], closes[-1]) >= candle_range*.3)
    previous_high = max(highs[-11:-1])
    previous_low = min(lows[-11:-1])
    buy_breakout = price > previous_high
    sell_breakdown = price < previous_low
    buy_pullback = (lows[-2] <= e20 + atr*.5 and
                    price > closes[-2] and bullish_close)
    sell_pullback = (highs[-2] >= e20 - atr*.5 and
                     price < closes[-2] and bearish_close)
    buy_continuation = price > closes[-3] and (bullish_close or buy_breakout)
    sell_continuation = price < closes[-3] and (bearish_close or sell_breakdown)
    buy_distance = (price-e20)/atr
    sell_distance = (e20-price)/atr

    buy = (14*(price > e20) + 12*(e20 > e50) +
           10*(e20 > previous_e20) + 10*(pdi > mdi) +
           8*(adx >= MIN_ADX) + 8*(45 <= strength <= 78) +
           8*buy_continuation + 8*(bullish_close or bullish_rejection) +
           8*(buy_breakout or buy_pullback) + 6*(vol_ratio >= .75) +
           4*(trend1 == 'BULLISH') + 4*(trend4 == 'BULLISH'))
    sell = (14*(price < e20) + 12*(e20 < e50) +
            10*(e20 < previous_e20) + 10*(mdi > pdi) +
            8*(adx >= MIN_ADX) + 8*(22 <= strength <= 55) +
            8*sell_continuation + 8*(bearish_close or bearish_rejection) +
            8*(sell_breakdown or sell_pullback) + 6*(vol_ratio >= .75) +
            4*(trend1 == 'BEARISH') + 4*(trend4 == 'BEARISH'))
    buy = int(min(100, buy))
    sell = int(min(100, sell))

    # Balanced signals: directional trend + DI + recent candle movement.
    # RSI, higher timeframes and EMA distance affect rating/caution, not vetoes.
    # No trade is forced when the directional evidence is absent.
    buy_ok = (buy >= MIN_SIGNAL_SCORE and buy >= sell + MIN_SCORE_GAP and
              price > e20 and pdi > mdi and
              (buy_continuation or buy_pullback or buy_breakout) and
              vol_ratio >= MIN_VOLUME_RATIO)
    sell_ok = (sell >= MIN_SIGNAL_SCORE and sell >= buy + MIN_SCORE_GAP and
               price < e20 and mdi > pdi and
               (sell_continuation or sell_pullback or sell_breakdown) and
               vol_ratio >= MIN_VOLUME_RATIO)
    direction = 'BUY' if buy_ok else 'SELL' if sell_ok else 'WAIT'
    score = buy if direction == 'BUY' else sell if direction == 'SELL' else max(buy, sell)
    setup = ('Breakout' if buy_breakout else 'Pullback' if buy_pullback else 'Trend continuation') if direction == 'BUY' else (
            'Breakdown' if sell_breakdown else 'Pullback' if sell_pullback else 'Trend continuation') if direction == 'SELL' else 'No qualified setup'
    stop = tp1 = tp2 = None
    if direction != 'WAIT':
        # Fixed ATR risk makes each candidate testable; no imaginary support-room gate.
        risk = atr * STOP_ATR_MULTIPLIER
        if direction == 'BUY':
            stop, tp1, tp2 = price-risk, price+risk*TP1_RR, price+risk*TP2_RR
            if stop <= 0:
                direction = 'WAIT'
        else:
            stop, tp1, tp2 = price+risk, price-risk*TP1_RR, price-risk*TP2_RR
            if tp2 <= 0:
                direction = 'WAIT'
        if direction == 'WAIT':
            stop = tp1 = tp2 = None
            setup = 'Invalid risk levels'
    reasons = []
    cautions = []
    if direction == 'WAIT':
        if vol_ratio < MIN_VOLUME_RATIO:
            reasons.append('Low volume')
        if not reasons:
            reasons.append('Trend, DI, momentum or score gap missing')
    else:
        reasons.append('Closed-candle trend and DI momentum')
        if (direction == 'BUY' and buy_breakout) or (direction == 'SELL' and sell_breakdown):
            reasons.append('Fresh structure break')
        if (direction == 'BUY' and buy_pullback) or (direction == 'SELL' and sell_pullback):
            reasons.append('Pullback recovery')
        distance = buy_distance if direction == 'BUY' else sell_distance
        if distance > MAX_ENTRY_DISTANCE_ATR:
            cautions.append('LATE ENTRY: far from EMA20')
        if (direction == 'BUY' and strength >= 80) or (direction == 'SELL' and strength <= 20):
            cautions.append('EXTREME RSI: reversal risk')
        if adx < MIN_ADX:
            cautions.append('Weak trend strength')
        if vol_ratio < .75:
            cautions.append('Below-average volume')
    print(f'DEBUG {symbol} | BUY={buy} SELL={sell} FINAL={direction} | '
          f'RSI={strength:.1f} ADX={adx:.1f} VOL={vol_ratio:.2f} '
          f'+DI={pdi:.1f} -DI={mdi:.1f} DIST_BUY={buy_distance:.2f} '
          f'DIST_SELL={sell_distance:.2f} 1H={trend1} 4H={trend4} '
          f'REASON={"; ".join(reasons)}')
    return {'symbol': symbol, 'direction': direction, 'score': score,
            'buy': buy, 'sell': sell, 'quality': grade(score),
            'price': price, 'stop': stop, 'tp1': tp1, 'tp2': tp2,
            'atr': atr, 'rsi': strength, 'adx': adx, 'pdi': pdi,
            'mdi': mdi, 'ema20': e20, 'ema50': e50, 'volume': vol_ratio,
            'trend1': trend1, 'trend4': trend4, 'setup': setup,
            'reason': '; '.join(reasons), 'caution': '; '.join(cautions) if cautions else 'None',
            'candle_id': rows[-1][0]}


def fmt(value):
    return 'N/A' if value is None else f'{value:.9g}'


def format_message(r):
    direction = r['direction']
    status = ('BUY' if direction == 'BUY' else
              'SELL' if direction == 'SELL' else 'WAIT')
    candle_time = datetime.fromtimestamp(
        (r['candle_id'] + TIMEFRAME_MS) / 1000, tz=timezone.utc
    ).strftime('%Y-%m-%d %H:%M UTC')
    return (f'**{r["symbol"]}** | CRYPTO SIGNAL BOT\n'
            f'\U0001f4e1 Bitget USDT Perpetual | 15m CLOSED candle\n'
            f'\U0001f56f\ufe0f Candle closed: {candle_time}\n'
            f'\U0001f3af Signal: **{status}**\n'
            f'\U0001f4b0 Reference entry: {fmt(r["price"])}\n'
            f'\U0001f6d1 Stop Loss: {fmt(r["stop"])}\n'
            f'\U0001f3af TP1: {fmt(r["tp1"])} | TP2: {fmt(r["tp2"])}\n'
            f'\U0001f4ca BUY {r["buy"]} / SELL {r["sell"]} | Score {r["score"]}/100\n'
            f'\u2b50 Setup rating: {r["quality"]} (not win probability)\n'
            f'\U0001f4c8 RSI {r["rsi"]:.1f} | ADX {r["adx"]:.1f}\n'
            f'\u2197 +DI {r["pdi"]:.1f} | \u2198 -DI {r["mdi"]:.1f}\n'
            f'\u303d EMA20 {fmt(r["ema20"])} | EMA50 {fmt(r["ema50"])}\n'
            f'\U0001f4e6 Volume {r["volume"]:.2f}x\n'
            f'\U0001f550 1H {r["trend1"]} | 4H {r["trend4"]}\n'
            f'\U0001f4cc Setup: {r["setup"]}\n'
            f'\U0001f4dd {r["reason"]}\n'
            f'\u26a0\ufe0f Entry caution: {r["caution"]}\n'
            f'\u26a0\ufe0f Signal based on closed candle; reference entry is not a guaranteed fill.')


def send_discord(messages):
    if not WEBHOOK_URL:
        print('No DISCORD_WEBHOOK_URL; printing signals only')
        return
    for message in messages:
        response = requests.post(WEBHOOK_URL, json={'content': message}, timeout=TIMEOUT)
        print('Discord status:', response.status_code)
        response.raise_for_status()
        time.sleep(.25)


def main():
    print('Crypto Signal Bot - BALANCED - CLOSED CANDLES')
    mode = os.getenv('BOT_MODE', 'normal').strip().lower()
    if mode != 'normal':
        print(f'BOT_MODE={mode}: normal closed-candle reports only; fast mode disabled')
        return
    messages = []
    for symbol in COINS:
        try:
            result = scan(symbol)
            print(f'{symbol}: {result["direction"]} / {result["score"]}')
            messages.append(format_message(result))
        except Exception as exc:
            print(f'ERROR {symbol}: {type(exc).__name__}: {exc}')
    if messages:
        send_discord(messages)
    else:
        print('No valid data for any symbol; no Discord report')


if __name__ == '__main__':
    main()

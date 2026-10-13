#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bitget V4: closed-candle, setup vs confirmed entry signal scanner.
Public USDT perpetual data only. No live order execution.
Requires: requests. Optional: DISCORD_WEBHOOK_URL environment variable.
"""
import os
import time
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
import requests

BASE = 'https://api.bitget.com'
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'DOGEUSDT', 'ZENUSDT', 'ZECUSDT']
GRANULARITY = '15m'
CANDLE_MS = 15 * 60 * 1000
WEBHOOK = os.getenv('DISCORD_WEBHOOK_URL', '').strip()
STATE_FILE = Path(os.getenv('SIGNAL_STATE_FILE', 'v4_signal_state.json'))
SEND_WATCH = os.getenv('SEND_WATCH', 'true').lower() == 'true'
TIMEOUT = 15


def ema(values, period):
    out = [float(values[0])]
    alpha = 2.0 / (period + 1)
    for v in values[1:]:
        out.append(alpha * float(v) + (1-alpha) * out[-1])
    return out


def rsi(values, period=14):
    changes = [values[i] - values[i-1] for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain*(period-1) + gains[i])/period
        avg_loss = (avg_loss*(period-1) + losses[i])/period
    if avg_loss == 0:
        return 100.0 if avg_gain else 50.0
    return 100 - 100/(1 + avg_gain/avg_loss)


def atr_adx(candles, period=14):
    tr, plus, minus = [], [], []
    for i in range(1, len(candles)):
        prev, cur = candles[i-1], candles[i]
        up = cur['h']-prev['h']
        down = prev['l']-cur['l']
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
        tr.append(max(cur['h']-cur['l'], abs(cur['h']-prev['c']), abs(cur['l']-prev['c'])))
    def smooth(series):
        s = sum(series[:period])
        out = [s]
        for x in series[period:]:
            s = s - s/period + x
            out.append(s)
        return out
    ts, ps, ms = smooth(tr), smooth(plus), smooth(minus)
    dx = []
    for t, p, m in zip(ts, ps, ms):
        pd = 100*p/t if t else 0
        md = 100*m/t if t else 0
        dx.append(100*abs(pd-md)/(pd+md) if pd+md else 0)
    if len(dx) < period:
        raise ValueError('Insufficient ADX history')
    adx = sum(dx[:period])/period
    for d in dx[period:]:
        adx = ((period-1)*adx+d)/period
    return ts[-1]/period, adx, (100*ps[-1]/ts[-1] if ts[-1] else 0), (100*ms[-1]/ts[-1] if ts[-1] else 0)


def fetch(session, symbol, granularity, duration_ms, limit=200):
    url = BASE + '/api/v2/mix/market/candles'
    params = {'symbol':symbol, 'productType':'USDT-FUTURES', 'granularity':granularity, 'limit':limit}
    response = session.get(url, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if payload.get('code') != '00000' or not isinstance(payload.get('data'), list):
        raise ValueError('Bitget API error: ' + str(payload.get('msg', payload)))
    now = int(time.time()*1000)
    candles = []
    for r in payload['data']:
        if int(r[0])+duration_ms > now:  # Never use the current, unfinished candle.
            continue
        candles.append({'t':int(r[0]), 'o':float(r[1]), 'h':float(r[2]),
                        'l':float(r[3]), 'c':float(r[4]), 'v':float(r[5])})
    candles.sort(key=lambda x:x['t'])
    candles = list({x['t']:x for x in candles}.values())
    if len(candles) < 90:
        raise ValueError('Not enough closed candles: '+str(len(candles)))
    return candles


def analyze(session, symbol):
    c = fetch(session, symbol, '15m', CANDLE_MS)
    h = fetch(session, symbol, '1H', 60*60*1000)
    closes = [x['c'] for x in c]
    higher = [x['c'] for x in h]
    e20, e50 = ema(closes,20), ema(closes,50)
    h20, h50 = ema(higher,20), ema(higher,50)
    a, adx, pdi, mdi = atr_adx(c)
    rv = rsi(closes)
    last, prev = c[-1], c[-2]
    avgvol = statistics.mean(x['v'] for x in c[-21:-1])
    vol_ratio = last['v']/avgvol if avgvol > 0 else 0
    trend_up = h20[-1] > h50[-1] and h20[-1] > h20[-4]
    trend_down = h20[-1] < h50[-1] and h20[-1] < h20[-4]
    # A setup can exist before an entry trigger; avoid requiring every indicator to align.
    buy_setup = trend_up and e20[-1] > e50[-1] and last['c'] > e50[-1] and pdi >= mdi*0.9
    sell_setup = trend_down and e20[-1] < e50[-1] and last['c'] < e50[-1] and mdi >= pdi*0.9
    body = abs(last['c']-last['o'])
    candle_range = max(last['h']-last['l'], 1e-12)
    bullish_close = last['c'] > last['o'] and (last['c']-last['l'])/candle_range >= .70 and body >= .20*a
    bearish_close = last['c'] < last['o'] and (last['h']-last['c'])/candle_range >= .70 and body >= .20*a
    bullish_engulf = last['c'] > last['o'] and prev['c'] < prev['o'] and last['c'] >= prev['o'] and last['o'] <= prev['c']
    bearish_engulf = last['c'] < last['o'] and prev['c'] > prev['o'] and last['o'] >= prev['c'] and last['c'] <= prev['o']
    # Avoid chasing price far away from the fast EMA.
    distance_ok = abs(last['c']-e20[-1]) <= 1.2*a
    strength_ok = adx >= 20 and vol_ratio >= .75
    buy_trigger = (bullish_close or bullish_engulf) and last['c'] > prev['h'] and 50 <= rv <= 72 and pdi > mdi
    sell_trigger = (bearish_close or bearish_engulf) and last['c'] < prev['l'] and 28 <= rv <= 50 and mdi > pdi
    if buy_setup:
        side = 'BUY'
        confirmed = strength_ok and distance_ok and buy_trigger
    elif sell_setup:
        side = 'SELL'
        confirmed = strength_ok and distance_ok and sell_trigger
    else:
        side = None
        confirmed = False
    status = ('CONFIRMED '+side) if side and confirmed else ('WATCH '+side if side else 'NO TRADE')
    # Score is a heuristic setup score, NOT a probability of hitting TP.
    score = 0
    if side:
        score += 30
        score += 15 if adx >= 20 else 0
        score += 15 if vol_ratio >= .75 else 0
        score += 15 if distance_ok else 0
        score += 15 if (buy_trigger if side == 'BUY' else sell_trigger) else 0
        score += 10 if adx >= 25 else 0
    entry = last['c'] if confirmed else None
    stop = None
    tp1 = None
    tp2 = None
    if confirmed:
        # ATR stop with nearby swing protection; cap risk to avoid oversized stops.
        recent = c[-11:-1]
        if side == 'BUY':
            structure = min(x['l'] for x in recent)-.1*a
            stop = min(entry-1.5*a, structure)
            if entry-stop > 2.5*a:
                confirmed = False
        else:
            structure = max(x['h'] for x in recent)+.1*a
            stop = max(entry+1.5*a, structure)
            if stop-entry > 2.5*a:
                confirmed = False
        if confirmed:
            risk = abs(entry-stop)
            tp1 = entry + (1.5*risk if side == 'BUY' else -1.5*risk)
            tp2 = entry + (2.5*risk if side == 'BUY' else -2.5*risk)
        else:
            entry = stop = None
            status = 'WATCH '+side
    return {'symbol':symbol, 'status':status, 'score':score, 'candle_time':last['t'],
            'close':last['c'], 'rsi':round(rv,1), 'adx':round(adx,1),
            'volume_ratio':round(vol_ratio,2), 'atr':a,
            'entry':entry, 'stop':stop, 'tp1':tp1, 'tp2':tp2,
            'trend_1h':'BULLISH' if trend_up else 'BEARISH' if trend_down else 'MIXED'}


def fmt(x):
    return '-' if x is None else f'{x:,.6f}'.rstrip('0').rstrip('.')


def message(r):
    lines = [f"{r['symbol']} | {r['status']}",
             f"Closed candle (UTC): {datetime.fromtimestamp(r['candle_time']/1000,timezone.utc):%Y-%m-%d %H:%M}",
             f"Close: {fmt(r['close'])} | Setup score: {r['score']}/100 (NOT win probability)",
             f"1H trend: {r['trend_1h']} | RSI: {r['rsi']} | ADX: {r['adx']} | Volume: {r['volume_ratio']}x"]
    if r['entry'] is not None:
        lines += [f"Entry: {fmt(r['entry'])} | SL: {fmt(r['stop'])}",
                  f"TP1: {fmt(r['tp1'])} | TP2: {fmt(r['tp2'])}"]
    else:
        lines.append('NOT AN ENTRY: wait for a confirmed closed-candle trigger.')
    return '\n'.join(lines)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=2), encoding='utf-8')
    tmp.replace(STATE_FILE)


def post_discord(session, content):
    if not WEBHOOK:
        return False
    response = session.post(WEBHOOK, json={'content':content[:1900]}, timeout=TIMEOUT)
    response.raise_for_status()
    return True


def main():
    state = load_state()
    with requests.Session() as session:
        for symbol in SYMBOLS:
            try:
                r = analyze(session, symbol)
                print(message(r), '\n', flush=True)
                # De-duplicate by symbol, candle, and status. Keep NO TRADE local.
                key = f"{r['candle_time']}:{r['status']}"
                should_send = r['status'].startswith('CONFIRMED') or (SEND_WATCH and r['status'].startswith('WATCH'))
                if should_send and state.get(symbol) != key:
                    if post_discord(session, message(r)):
                        state[symbol] = key
                        save_state(state)
            except (requests.RequestException, ValueError, KeyError, IndexError, ZeroDivisionError) as exc:
                print(f'{symbol}: ERROR: {exc}', flush=True)
    if not WEBHOOK:
        print('Discord disabled: set DISCORD_WEBHOOK_URL to send alerts.')


if __name__ == '__main__':
    main()

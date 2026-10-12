# -*- coding: utf-8 -*-
"""V4 Bitget USDT perpetual signals + optional fast-entry monitoring. Default: BOT_MODE=normal, original 15-minute full Discord report. Fast: BOT_MODE=fast, sends only fresh ELIGIBLE entries. FAST_MODE must be scheduled separately. All signal indicators use CLOSED candles. The current ticker price is used only as a prospective entry in fast mode. """
import json
import math
import os
import time
from pathlib import Path

import requests

WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL')
COINS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT',
         'DOGEUSDT', 'ZENUSDT', 'ZECUSDT']
BITGET_URL = 'https://api.bitget.com'
TIMEFRAME = '15m'
CANDLE_LIMIT = 200
TIMEOUT = 12
SIG_BUY = '\U0001f7e2 BUY'
SIG_SELL = '\U0001f534 SELL'
SIG_WAIT = '\U0001f7e1 WAIT'
MIN_SIGNAL_SCORE = 55
PAPER_SIGNAL_SCORE = 55
STOP_ATR_MULTIPLIER = 1.5
TP1_RR = 1.5
TP2_RR = 2.5
STRUCTURE_LOOKBACK = 20
STRUCTURE_BUFFER_ATR = 0.15
MAX_ENTRY_DISTANCE_ATR = 1.8
STRONG_VOLUME_RATIO = 1.10
WEAK_VOLUME_RATIO = 0.60
MIN_QUALITY_SCORE = 45
RELAXED_MODE = True
RELAXED_MIN_SCORE = 50
RELAXED_SCORE_GAP = 10
FAST_STATE_FILE = Path(os.getenv('FAST_STATE_FILE', 'fast_alert_state.json'))
FAST_MAX_CANDLE_AGE_MIN = 15
FAST_SAME_DIRECTION_COOLDOWN_SEC = 90 * 60
FAST_MAX_PRICE_AGE_SEC = 90
FAST_MAX_ENTRY_MOVE_ATR = 0.75  # do not chase a price far from confirmed close


def api_data(endpoint, params):
    response = requests.get(f'{BITGET_URL}{endpoint}', params=params,
                            headers={'Accept': 'application/json'}, timeout=TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get('code') != '00000':
        raise ValueError(f'Bitget error: {payload}')
    return payload.get('data')


def get_klines(symbol):
    rows = api_data('/api/v2/mix/market/candles', {
        'symbol': symbol, 'productType': 'USDT-FUTURES',
        'granularity': TIMEFRAME, 'limit': str(CANDLE_LIMIT)})
    if not isinstance(rows, list) or len(rows) < 60:
        raise ValueError(f'Not enough futures candles: {symbol}')
    if not all(isinstance(c, (list, tuple)) and len(c) >= 6 for c in rows):
        raise ValueError(f'Invalid futures candles: {symbol}')
    return sorted(rows, key=lambda c: int(c[0]))


def get_live_price(symbol):
    """Bitget public USDT perpetual last-traded ticker with timestamp check."""
    data = api_data('/api/v2/mix/market/ticker', {
        'symbol': symbol, 'productType': 'USDT-FUTURES'})
    if not isinstance(data, list) or not data:
        raise ValueError(f'Missing futures ticker for {symbol}')
    item = data[0]
    if item.get('symbol') != symbol:
        raise ValueError(f'Ticker symbol mismatch for {symbol}')
    price = float(item['lastPr'])
    ts = int(item['ts'])
    age_sec = (time.time() * 1000 - ts) / 1000
    if not math.isfinite(price) or price <= 0 or not 0 <= age_sec <= FAST_MAX_PRICE_AGE_SEC:
        raise ValueError(f'Invalid/stale ticker for {symbol}: age={age_sec:.1f}s')
    return price


def calculate_ema(values, period):
    if not values:
        return 0.0
    if len(values) < period:
        return values[-1]
    ema = sum(values[:period]) / period
    k = 2 / (period + 1)
    for v in values[period:]:
        ema = (v - ema) * k + ema
    return ema


def calculate_rsi(values, period=14):
    if len(values) <= period:
        return 50.0
    gains = [max(values[i] - values[i - 1], 0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0) for i in range(1, len(values))]
    ag, al = sum(gains[:period]) / period, sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return 100 - 100 / (1 + ag / al)


def calculate_atr(highs, lows, closes, period=14):
    if len(closes) <= period:
        return 0.0
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]),
               abs(lows[i] - closes[i-1])) for i in range(1, len(closes))]
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def calculate_adx(highs, lows, closes, period=14):
    if len(closes) < period * 2 + 1:
        return 0., 0., 0.
    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]),
                       abs(lows[i] - closes[i-1])))
        up, down = highs[i] - highs[i-1], lows[i-1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.)
        minus_dm.append(down if down > up and down > 0 else 0.)
    atr_sum, ps, ms = sum(trs[:period]), sum(plus_dm[:period]), sum(minus_dm[:period])
    dxs, pdi, mdi = [], 0., 0.
    for i in range(period - 1, len(trs)):
        if i >= period:
            atr_sum += trs[i] - atr_sum / period
            ps += plus_dm[i] - ps / period
            ms += minus_dm[i] - ms / period
        if atr_sum <= 0:
            dxs.append(0.)
            continue
        pdi, mdi = ps / atr_sum * 100, ms / atr_sum * 100
        dxs.append(abs(pdi - mdi) / (pdi + mdi) * 100 if pdi + mdi else 0.)
    if len(dxs) < period:
        return 0., pdi, mdi
    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx, pdi, mdi


def scan_candle_patterns(opens, highs, lows, closes):
    if len(closes) < 3:
        return False, False
    o1, c1, o2, c2 = opens[-2], closes[-2], opens[-1], closes[-1]
    h2, l2 = highs[-1], lows[-1]
    b1, b2 = abs(c1-o1), abs(c2-o2)
    rg = max(h2-l2, 1e-12)
    lower, upper = min(o2,c2)-l2, h2-max(o2,c2)
    cp = (c2-l2)/rg
    bull_engulf = c1 < o1 and c2 > o2 and o2 <= c1 and c2 >= o1 and b2 >= b1*.9
    bear_engulf = c1 > o1 and c2 < o2 and o2 >= c1 and c2 <= o1 and b2 >= b1*.9
    bull_hammer = lower >= max(b2*2, rg*.45) and upper <= rg*.25 and cp >= .65
    bear_star = upper >= max(b2*2, rg*.45) and lower <= rg*.25 and cp <= .35
    bull_strong = c2 > o2 and b2/rg >= .6 and cp >= .8
    bear_strong = c2 < o2 and b2/rg >= .6 and cp <= .2
    o0, c0 = opens[-3], closes[-3]
    b0 = abs(c0-o0)
    r0 = max(highs[-3]-lows[-3], 1e-12)
    morning = (c0 < o0 and b0/r0 >= .45 and b1 <= b0*.6 and
               c2 > o2 and b2 >= b0*.4 and c2 >= (o0+c0)/2)
    evening = (c0 > o0 and b0/r0 >= .45 and b1 <= b0*.6 and
               c2 < o2 and b2 >= b0*.4 and c2 <= (o0+c0)/2)
    return bull_engulf or bull_hammer or bull_strong or morning, bear_engulf or bear_star or bear_strong or evening


def get_previous_day_levels(candles, now_ms=None):
    now_ms = now_ms or int(time.time()*1000)
    day = 86400000
    today = (now_ms // day) * day
    rows = [c for c in candles if today-day <= int(c[0]) < today
            and int(c[0]) + 900000 <= now_ms]
    return ((max(float(c[2]) for c in rows), min(float(c[3]) for c in rows))
            if rows else (None, None))


def get_support_resistance(highs, lows, lookback=20):
    window = min(lookback, len(highs)-1)
    if window < 1:
        return lows[-1], highs[-1]
    return min(lows[-window-1:-1]), max(highs[-window-1:-1])


def nearest_support(lows, price, atr=None, lookback=30):
    start = max(1, len(lows)-lookback-1)
    return max((lows[i] for i in range(start, len(lows)-1)
                if lows[i] < price and lows[i] <= lows[i-1] and lows[i] <= lows[i+1]), default=None)


def nearest_resistance(highs, price, atr=None, lookback=30):
    start = max(1, len(highs)-lookback-1)
    return min((highs[i] for i in range(start, len(highs)-1)
                if highs[i] > price and highs[i] >= highs[i-1] and highs[i] >= highs[i+1]), default=None)


def quality_grade(score):
    return 'A+' if score >= 90 else 'A' if score >= 80 else 'B' if score >= 70 else 'C'


def get_higher_timeframe_trend(symbol, granularity, duration_ms):
    try:
        rows = api_data('/api/v2/mix/market/candles', {
            'symbol': symbol, 'productType': 'USDT-FUTURES',
            'granularity': granularity, 'limit': '100'})
        now_ms = int(time.time()*1000)
        closed = sorted((c for c in rows if len(c) >= 6 and
                         int(c[0])+duration_ms <= now_ms), key=lambda c: int(c[0]))
        if len(closed) < 55:
            return 'UNKNOWN'
        values = [float(c[4]) for c in closed]
        e20, e50, p = calculate_ema(values,20), calculate_ema(values,50), values[-1]
        return 'BULLISH' if p > e20 > e50 else 'BEARISH' if p < e20 < e50 else 'NEUTRAL'
    except (requests.RequestException, ValueError, TypeError, KeyError, IndexError) as e:
        print(f'HTF error {symbol} {granularity}: {e}')
        return 'UNKNOWN'


def get_signal(symbol, entry_price_override=None):
    """Original closed-candle V4 logic; optional ticker only adjusts entry checks. In fast mode, pattern, structure breakout/retest and volume remain based on the fully closed candle. The current ticker may improve or worsen entry. """
    candles = get_klines(symbol)
    now_ms = int(time.time()*1000)
    closed = [c for c in candles if int(c[0])+900000 <= now_ms]
    if len(closed) < 60:
        return None
    candle_id = int(closed[-1][0])
    candle_age = (now_ms-candle_id-900000)/60000
    opens = [float(c[1]) for c in closed]
    highs = [float(c[2]) for c in closed]
    lows = [float(c[3]) for c in closed]
    closes = [float(c[4]) for c in closed]
    volumes = [float(c[5]) for c in closed]
    confirmed_price = closes[-1]
    price = float(entry_price_override) if entry_price_override is not None else confirmed_price
    if not math.isfinite(price) or price <= 0:
        raise ValueError('Invalid entry price')
    atr = calculate_atr(highs,lows,closes)
    if atr <= 0:
        raise ValueError('Invalid ATR')
    if entry_price_override is not None and abs(price-confirmed_price) > atr*FAST_MAX_ENTRY_MOVE_ATR:
        print(f'FAST_SKIP {symbol}: live price moved too far from confirmed close')
        return None
    ema20, ema50 = calculate_ema(closes,20), calculate_ema(closes,50)
    ema20_prev, ema50_prev = calculate_ema(closes[:-3],20), calculate_ema(closes[:-3],50)
    ema20_up, ema20_down = ema20 > ema20_prev, ema20 < ema20_prev
    ema50_up, ema50_down = ema50 > ema50_prev, ema50 < ema50_prev
    rsi = calculate_rsi(closes)
    adx,pdi,mdi = calculate_adx(highs,lows,closes)
    di_bull,di_bear = pdi > mdi, mdi > pdi
    trend_1h = get_higher_timeframe_trend(symbol,'1H',3600000)
    trend_4h = get_higher_timeframe_trend(symbol,'4H',14400000)
    bull_momentum, bear_momentum = confirmed_price > closes[-4], confirmed_price < closes[-4]
    avg_vol = sum(volumes[-21:-1])/20
    vol_ratio = volumes[-1]/avg_vol if avg_vol > 0 else 0.
    vol_strong,vol_weak = vol_ratio >= STRONG_VOLUME_RATIO,vol_ratio < WEAK_VOLUME_RATIO
    support,resistance = get_support_resistance(highs,lows,STRUCTURE_LOOKBACK)
    ns,nr = nearest_support(lows,price,atr),nearest_resistance(highs,price,atr)
    pd_high,pd_low = get_previous_day_levels(closed,now_ms)
    bull_pattern,bear_pattern = scan_candle_patterns(opens,highs,lows,closes)
    bull_pattern_confirmed = bull_pattern and 0 <= confirmed_price-support <= atr*.5
    bear_pattern_confirmed = bear_pattern and 0 <= resistance-confirmed_price <= atr*.5
    bull_ema = price > ema20 > ema50 and ema20_up
    bear_ema = price < ema20 < ema50 and ema20_down
    prev_res = max(highs[-STRUCTURE_LOOKBACK-1:-1])
    prev_sup = min(lows[-STRUCTURE_LOOKBACK-1:-1])
    bull_break = confirmed_price > prev_res and closes[-2] <= prev_res
    bear_break = confirmed_price < prev_sup and closes[-2] >= prev_sup
    retest_res = max(highs[-STRUCTURE_LOOKBACK-2:-2])
    retest_sup = min(lows[-STRUCTURE_LOOKBACK-2:-2])
    bull_retest = closes[-2] > retest_res and lows[-1] <= retest_res and confirmed_price > retest_res
    bear_retest = closes[-2] < retest_sup and highs[-1] >= retest_sup and confirmed_price < retest_sup
    bull_structure,bear_structure = bull_break or bull_retest,bear_break or bear_retest
    dist_ema = abs(price-ema20)/atr
    strong_buy_breakout = (bull_structure and price > ema20 and ema20_up and di_bull and
                           adx >= 20 and vol_ratio >= 1.5 and trend_1h == 'BULLISH' and closes[-1] > opens[-1])
    strong_sell_breakout = (bear_structure and price < ema20 and ema20_down and di_bear and
                            adx >= 20 and vol_ratio >= 1.5 and trend_1h == 'BEARISH' and closes[-1] < opens[-1])
    buy_limit = 2.2 if strong_buy_breakout else MAX_ENTRY_DISTANCE_ATR
    sell_limit = 2.2 if strong_sell_breakout else MAX_ENTRY_DISTANCE_ATR
    late_buy,late_sell = price > ema20 and dist_ema > buy_limit, price < ema20 and dist_ema > sell_limit
    bull_rsi,bear_rsi = 51 <= rsi <= 69,31 <= rsi <= 49
    bull_rsi_cont = rsi > 69 and adx >= 28 and di_bull and vol_strong and bull_ema and bull_momentum and not late_buy
    bear_rsi_cont = rsi < 31 and adx >= 28 and di_bear and vol_strong and bear_ema and bear_momentum and not late_sell
    bull_rsi_ok,bear_rsi_ok = bull_rsi or bull_rsi_cont,bear_rsi or bear_rsi_cont
    buy_obstacle = resistance if resistance > price else None
    sell_obstacle = support if support < price else None
    if pd_high is not None and pd_high > price:
        buy_obstacle = min(buy_obstacle,pd_high) if buy_obstacle is not None else pd_high
    if pd_low is not None and pd_low < price:
        sell_obstacle = max(sell_obstacle,pd_low) if sell_obstacle is not None else pd_low
    one_r = atr*STOP_ATR_MULTIPLIER
    buy_room_rr = math.inf if buy_obstacle is None else (buy_obstacle-atr*STRUCTURE_BUFFER_ATR-price)/one_r
    sell_room_rr = math.inf if sell_obstacle is None else (price-sell_obstacle-atr*STRUCTURE_BUFFER_ATR)/one_r
    enough_buy,enough_sell = buy_room_rr >= 1.,sell_room_rr >= 1.
    last_open,last_close,last_high,last_low = opens[-1],closes[-1],highs[-1],lows[-1]
    cr = last_high-last_low
    cb = abs(last_close-last_open)
    upper = last_high-max(last_open,last_close)
    lower = min(last_open,last_close)-last_low
    body_ratio = cb/cr if cr > 0 else 0.
    close_pos = (last_close-last_low)/cr if cr > 0 else .5
    bullish_candle = last_close > last_open and body_ratio >= .4 and close_pos >= .6
    bearish_candle = last_close < last_open and body_ratio >= .4 and close_pos <= .4
    bullish_rejection = lower >= cb*.75 and close_pos >= .55 and last_close >= last_open
    bearish_rejection = upper >= cb*.75 and close_pos <= .45 and last_close <= last_open
    bull_candle_ok = bullish_candle or bullish_rejection
    bear_candle_ok = bearish_candle or bearish_rejection
    bull_structure_path = bull_structure and price > ema20 and ema20_up and di_bull and (adx >= 20 or vol_strong) and not late_buy
    bear_structure_path = bear_structure and price < ema20 and ema20_down and di_bear and (adx >= 20 or vol_strong) and not late_sell
    buy = (12*(price>ema20)+12*(ema20>ema50)+8*ema20_up+4*ema50_up+14*bull_rsi_ok+
           8*bull_momentum+(14 if adx>=25 and di_bull else 7 if adx>=20 and di_bull else 0)+
           8*vol_strong+6*bull_structure+6*enough_buy+4*di_bull+4*bull_candle_ok+
           4*bull_pattern_confirmed-15*late_buy-5*vol_weak)
    sell = (12*(price<ema20)+12*(ema20<ema50)+8*ema20_down+4*ema50_down+14*bear_rsi_ok+
            8*bear_momentum+(14 if adx>=25 and di_bear else 7 if adx>=20 and di_bear else 0)+
            8*vol_strong+6*bear_structure+6*enough_sell+4*di_bear+4*bear_candle_ok+
            4*bear_pattern_confirmed-15*late_sell-5*vol_weak)
    buy,sell = max(0,min(100,buy)),max(0,min(100,sell))
    bull_early = price>ema20 and ema20_up and not late_buy and (di_bull or bull_structure) and (adx>=20 or vol_ratio>=.8 or bull_structure)
    bear_early = price<ema20 and ema20_down and not late_sell and (di_bear or bear_structure) and (adx>=20 or vol_ratio>=.8 or bear_structure)
    htf_buy = trend_1h=='BULLISH' or trend_4h=='BULLISH'
    htf_sell = trend_1h=='BEARISH' or trend_4h=='BEARISH'
    buy_trend = bull_ema and di_bull and (bull_momentum or bull_candle_ok) and (adx>=20 or vol_ratio>=.9)
    sell_trend = bear_ema and di_bear and (bear_momentum or bear_candle_ok) and (adx>=20 or vol_ratio>=.9)
    buy_structure = bull_structure_path and bull_momentum and (bull_candle_ok or vol_ratio>=1.)
    sell_structure = bear_structure_path and bear_momentum and (bear_candle_ok or vol_ratio>=1.)
    early_buy = bull_early and bull_momentum and di_bull and vol_ratio>=1.5 and adx>=18 and bull_candle_ok and 50<=rsi<=70 and enough_buy
    early_sell = bear_early and bear_momentum and di_bear and vol_ratio>=1.5 and adx>=18 and bear_candle_ok and 30<=rsi<=50 and enough_sell
    bull_pullback = (bull_ema and trend_1h=='BULLISH' and trend_4h!='BEARISH' and adx>=25 and di_bull and
                     bull_rsi_ok and not late_buy and vol_ratio>=.8 and enough_buy and bull_candle_ok and
                     closes[-2]<=opens[-2] and closes[-1]>highs[-2])
    bear_pullback = (bear_ema and trend_1h=='BEARISH' and trend_4h!='BULLISH' and adx>=25 and di_bear and
                     bear_rsi_ok and not late_sell and vol_ratio>=.8 and enough_sell and bear_candle_ok and
                     closes[-2]>=opens[-2] and closes[-1]<lows[-2])
    buy_confirm = ((buy_trend or buy_structure or early_buy or bull_pullback) and not late_buy and not vol_weak and enough_buy and
                   (bull_rsi_ok or (htf_buy and 48<=rsi<=72)) and (trend_4h!='BEARISH' or (bull_structure and adx>=25 and vol_strong)))
    sell_confirm = ((sell_trend or sell_structure or early_sell or bear_pullback) and not late_sell and not vol_weak and enough_sell and
                    (bear_rsi_ok or (htf_sell and 28<=rsi<=52)) and (trend_4h!='BULLISH' or (bear_structure and adx>=25 and vol_strong)))
    standard_buy = buy>=MIN_SIGNAL_SCORE and buy>sell and buy_confirm
    standard_sell = sell>=MIN_SIGNAL_SCORE and sell>buy and sell_confirm
    relaxed_buy = (RELAXED_MODE and not standard_buy and not standard_sell and buy>=RELAXED_MIN_SCORE and
                   buy>=sell+RELAXED_SCORE_GAP and price>ema20 and ema20_up and di_bull and bull_momentum and
                   adx>=18 and 45<=rsi<=72 and not late_buy and not vol_weak and enough_buy and
                   trend_4h!='BEARISH' and (bull_candle_ok or bull_pattern_confirmed))
    relaxed_sell = (RELAXED_MODE and not standard_buy and not standard_sell and not relaxed_buy and
                    sell>=RELAXED_MIN_SCORE and sell>=buy+RELAXED_SCORE_GAP and price<ema20 and ema20_down and
                    di_bear and bear_momentum and adx>=18 and 28<=rsi<=55 and not late_sell and not vol_weak and
                    enough_sell and trend_4h!='BULLISH' and (bear_candle_ok or bear_pattern_confirmed))
    if standard_buy or relaxed_buy:
        signal,kind,score = SIG_BUY,'BUY',buy
    elif standard_sell or relaxed_sell:
        signal,kind,score = SIG_SELL,'SELL',sell
    else:
        signal,kind,score = SIG_WAIT,'WAIT',min(max(buy,sell),84)
    paper_signal = 'NONE'
    if buy>=PAPER_SIGNAL_SCORE and buy>sell and price>ema20 and di_bull and not vol_weak and not late_buy and enough_buy and bull_momentum:
        paper_signal = 'PAPER BUY'
    elif sell>=PAPER_SIGNAL_SCORE and sell>buy and price<ema20 and di_bear and not vol_weak and not late_sell and enough_sell and bear_momentum:
        paper_signal = 'PAPER SELL'
    volume_points = 15 if vol_strong else 8 if vol_ratio>=WEAK_VOLUME_RATIO else 0
    buy_quality = (30 if bull_ema else 20 if bull_structure_path or bull_early else 0)+15*(adx>=20 and di_bull)+15*bull_rsi_ok+15*bull_structure+10*(bull_candle_ok or bull_pattern_confirmed)+volume_points-10*late_buy-10*(not enough_buy)
    sell_quality = (30 if bear_ema else 20 if bear_structure_path or bear_early else 0)+15*(adx>=20 and di_bear)+15*bear_rsi_ok+15*bear_structure+10*(bear_candle_ok or bear_pattern_confirmed)+volume_points-10*late_sell-10*(not enough_sell)
    buy_quality,sell_quality = max(0,min(100,buy_quality)),max(0,min(100,sell_quality))
    if kind=='BUY' and (buy_quality<MIN_QUALITY_SCORE or vol_weak or (not (bull_candle_ok or bull_pattern_confirmed) and buy_quality<65)):
        kind,signal,score='WAIT',SIG_WAIT,min(max(buy,sell),84)
    elif kind=='SELL' and (sell_quality<MIN_QUALITY_SCORE or vol_weak or (not (bear_candle_ok or bear_pattern_confirmed) and sell_quality<65)):
        kind,signal,score='WAIT',SIG_WAIT,min(max(buy,sell),84)
    entry_quality_score = buy_quality if kind=='BUY' else sell_quality if kind=='SELL' else max(buy_quality,sell_quality)
    entry_quality = quality_grade(entry_quality_score)
    if kind=='BUY':
        entry_setup = 'Breakout + Retest' if bull_break and bull_retest else 'Breakout Retest' if bull_retest else 'Breakout' if bull_break else 'Bullish Structure' if bull_structure else 'Trend Continuation'
        candle_status = 'Strong Bullish Candle' if bullish_candle else 'Bullish Rejection' if bullish_rejection else 'Not Confirmed'
    elif kind=='SELL':
        entry_setup = 'Breakdown + Retest' if bear_break and bear_retest else 'Breakdown Retest' if bear_retest else 'Breakdown' if bear_break else 'Bearish Structure' if bear_structure else 'Trend Continuation'
        candle_status = 'Strong Bearish Candle' if bearish_candle else 'Bearish Rejection' if bearish_rejection else 'Not Confirmed'
    else:
        entry_setup = 'No Confirmed Setup'
        candle_status = 'Partial Confirmation' if bull_candle_ok or bear_candle_ok else 'Not Confirmed'
    if kind in ('BUY','SELL') and (relaxed_buy or relaxed_sell):
        entry_setup += ' (Relaxed)'
    entry_timing = ('Ideal' if dist_ema<=1. else 'Acceptable') if kind!='WAIT' else 'Not Applicable'
    breakout_watch = 'NONE'
    if kind=='WAIT':
        if atr>0 and 0<=resistance-confirmed_price<=atr*.35 and vol_ratio>=1.5 and confirmed_price>ema20 and ema20_up and di_bull and 50<=rsi<=75 and bull_momentum and not late_buy:
            breakout_watch='BUY BREAKOUT WATCH'
        elif atr>0 and 0<=confirmed_price-support<=atr*.35 and vol_ratio>=1.5 and confirmed_price<ema20 and ema20_down and di_bear and 25<=rsi<=50 and bear_momentum and not late_sell:
            breakout_watch='SELL BREAKDOWN WATCH'
    if kind=='BUY':
        reasons=['Bullish EMA alignment','RSI confirmation']
        if di_bull: reasons.append('+DI stronger')
        if adx>=25: reasons.append('ADX trend strength')
        if vol_strong: reasons.append('Volume confirmation')
        if bull_structure: reasons.append('Breakout/retest structure')
    elif kind=='SELL':
        reasons=['Bearish EMA alignment','RSI confirmation']
        if di_bear: reasons.append('-DI stronger')
        if adx>=25: reasons.append('ADX trend strength')
        if vol_strong: reasons.append('Volume confirmation')
        if bear_structure: reasons.append('Breakdown/retest structure')
    else:
        reasons=[]
        if adx<25: reasons.append('ADX below 25 strong-trend threshold')
        if vol_weak: reasons.append('Very low volume')
        elif not vol_strong: reasons.append('Volume not elevated')
        if buy>=sell:
            if not bull_ema and not bull_structure_path: reasons.append('Bullish trend alignment incomplete')
            if not bull_rsi_ok: reasons.append('Bullish RSI confirmation missing')
            if not enough_buy: reasons.append('Limited upside room before resistance')
            if not bull_candle_ok: reasons.append('Bullish candle confirmation missing')
            if late_buy: reasons.append('BUY entry too far from EMA20')
        else:
            if not bear_ema and not bear_structure_path: reasons.append('Bearish trend alignment incomplete')
            if not bear_rsi_ok: reasons.append('Bearish RSI confirmation missing')
            if not enough_sell: reasons.append('Limited downside room before support')
            if not bear_candle_ok: reasons.append('Bearish candle confirmation missing')
            if late_sell: reasons.append('SELL entry too far from EMA20')
        if not reasons: reasons.append('Full confirmation not available')
    trend = ('Strong Bullish' if bull_ema and adx>=25 and di_bull else
             'Strong Bearish' if bear_ema and adx>=25 and di_bear else
             'Bullish' if price>ema20>ema50 else 'Bearish' if price<ema20<ema50 else 'Neutral')
    trade_advice,trade_advice_reason = 'NO TRADE','No confirmed BUY/SELL signal'
    if kind in ('BUY','SELL'):
        is_buy=kind=='BUY'
        direction_quality = buy_quality if is_buy else sell_quality
        direction_room = buy_room_rr if is_buy else sell_room_rr
        direction_candle = bull_candle_ok if is_buy else bear_candle_ok
        strong_breakdown = (not is_buy and bear_break and closes[-1]<opens[-1] and trend_1h=='BEARISH' and
                            trend_4h=='BEARISH' and adx>=30 and di_bear and vol_ratio>=2 and not late_sell and sell_room_rr>=1.5)
        strong_breakout = (is_buy and bull_break and closes[-1]>opens[-1] and trend_1h=='BULLISH' and
                           trend_4h=='BULLISH' and adx>=30 and di_bull and vol_ratio>=2 and not late_buy and buy_room_rr>=1.5)
        strong_setup = (score>=85 and direction_quality>=75 and adx>=30 and vol_ratio>=1.5 and
                        trend_1h==('BULLISH' if is_buy else 'BEARISH') and
                        trend_4h==('BULLISH' if is_buy else 'BEARISH') and
                        (di_bull if is_buy else di_bear) and (bull_momentum if is_buy else bear_momentum) and
                        not (late_buy if is_buy else late_sell) and direction_room>=1.5)
        direction_candle = direction_candle or strong_breakdown or strong_breakout or strong_setup
        direction_structure = bull_structure if is_buy else bear_structure
        direction_htf = 'BULLISH' if is_buy else 'BEARISH'
        opposite_htf = 'BEARISH' if is_buy else 'BULLISH'
        htf_safe = trend_4h not in (opposite_htf,'UNKNOWN')
        htf_confirmed = trend_1h==direction_htf or (direction_structure and adx>=25 and vol_ratio>=1.)
        checks = {'Higher timeframe':htf_safe and htf_confirmed,
                  'Signal strength':score>=60,'Setup quality':direction_quality>=55,
                  'Trend momentum':adx>=20 and (di_bull if is_buy else di_bear),
                  'Volume':vol_ratio>=.8,'Candle confirmation':direction_candle,
                  'Target room':direction_room>=1.2,
                  'Entry distance':not (late_buy if is_buy else late_sell)}
        failed=[name for name,passed in checks.items() if not passed]
        trade_advice='AVOID' if failed else 'ELIGIBLE'
        trade_advice_reason='Missing: '+', '.join(failed) if failed else 'All preliminary trade checks passed'
    print(f'DEBUG {symbol} | BUY={buy} SELL={sell} | RSI={rsi:.1f} ADX={adx:.1f} VOL={vol_ratio:.2f} | '
          f'FINAL={kind} ADVICE={trade_advice} | LATE_BUY={late_buy} LATE_SELL={late_sell} | '
          f'DIST_EMA_ATR={dist_ema:.2f} BUY_LIMIT={buy_limit:.2f} SELL_LIMIT={sell_limit:.2f} | '
          f'BUY_ROOM_RR={buy_room_rr:.2f} SELL_ROOM_RR={sell_room_rr:.2f} | '
          f'1H={trend_1h} 4H={trend_4h} CANDLE_AGE_MIN={candle_age:.1f}')
    return dict(price=price,confirmed_price=confirmed_price,candle_id=candle_id,candle_age=candle_age,
                ema20=ema20,ema50=ema50,trend_1h=trend_1h,trend_4h=trend_4h,rsi=rsi,adx=adx,
                plus_di=pdi,minus_di=mdi,atr=atr,trend=trend,signal=signal,paper_signal=paper_signal,
                trade_advice=trade_advice,trade_advice_reason=trade_advice_reason,signal_type=kind,
                signal_score=score,buy_score=buy,sell_score=sell,volume_ratio=vol_ratio,
                entry_quality=entry_quality,entry_quality_score=entry_quality_score,
                entry_setup=entry_setup,candle_status=candle_status,entry_timing=entry_timing,
                support=support,resistance=resistance,buy_obstacle=buy_obstacle,sell_obstacle=sell_obstacle,
                nearest_support=ns,nearest_resistance=nr,reason=' + '.join(reasons),breakout_watch=breakout_watch)


def calculate_trade_levels(price, atr, signal, support, resistance, ns, nr):
    if atr<=0 or price<=0 or signal not in (SIG_BUY,SIG_SELL):
        return None,None,None
    buffer=atr*STRUCTURE_BUFFER_ATR
    atr_stop=atr*STOP_ATR_MULTIPLIER
    max_risk=atr*2.5
    if signal==SIG_BUY:
        supports=[x for x in (support,ns) if x is not None and 0<x<price]
        structural_sl=max(supports)-buffer if supports else None
        sl=min(price-atr_stop,structural_sl) if structural_sl is not None else price-atr_stop
        risk=price-sl
        if risk<=0 or risk>max_risk or sl<=0:
            print('SLTP_BLOCK BUY INVALID_STOP');return None,None,None
        obstacle=nr if nr is not None and nr>price else None
        if obstacle is not None and obstacle-buffer-price<risk*1.5:
            print('SLTP_BLOCK BUY INSUFFICIENT_ROOM');return None,None,None
        tp1=price+risk*TP1_RR
        tp2=price+risk*TP2_RR
        if obstacle is not None:tp1=min(tp1,obstacle-buffer)
        if tp1<=price or tp2<=tp1:
            print('SLTP_BLOCK BUY INVALID_TARGETS');return None,None,None
        return sl,tp1,tp2
    resistances=[x for x in (resistance,nr) if x is not None and x>price]
    structural_sl=min(resistances)+buffer if resistances else None
    sl=max(price+atr_stop,structural_sl) if structural_sl is not None else price+atr_stop
    risk=sl-price
    if risk<=0 or risk>max_risk:
        print('SLTP_BLOCK SELL INVALID_STOP');return None,None,None
    obstacle=ns if ns is not None and ns<price else None
    if obstacle is not None and price-obstacle-buffer<risk*1.5:
        print('SLTP_BLOCK SELL INSUFFICIENT_ROOM');return None,None,None
    tp1=price-risk*TP1_RR
    tp2=price-risk*TP2_RR
    if obstacle is not None:tp1=max(tp1,obstacle+buffer)
    if tp1>=price or tp2>=tp1 or tp2<=0:
        print('SLTP_BLOCK SELL INVALID_TARGETS');return None,None,None
    return sl,tp1,tp2


def estimate_duration(price,tp2,atr):
    if atr<=0 or tp2 is None:return 'N/A'
    minutes=max(15,abs(tp2-price)/atr*15*1.5)
    if minutes<60:return f'\u062a\u0642\u0631\u06cc\u0628\u0627\u064b {math.ceil(minutes)} \u0645\u0646\u0679'
    hours=minutes/60
    if hours<24:return f'\u062a\u0642\u0631\u06cc\u0628\u0627\u064b {hours:.1f} \u06af\u06be\u0646\u0679\u06d2'
    return f'\u062a\u0642\u0631\u06cc\u0628\u0627\u064b {hours/24:.1f} \u062f\u0646'


def send_discord(message):
    if not WEBHOOK_URL:
        print('DISCORD_WEBHOOK_URL is not configured. No alert sent.')
        return False
    parts,current=[],''
    for section in message.split('\n\n'):
        section=section.strip()
        if not section:continue
        candidate=current+'\n\n'+section if current else section
        if len(candidate)<=1900:current=candidate
        else:
            if current:parts.append(current)
            while len(section)>1900:
                parts.append(section[:1900]);section=section[1900:]
            current=section
    if current:parts.append(current)
    for i,part in enumerate(parts,1):
        response=requests.post(WEBHOOK_URL,json={'content':part},timeout=TIMEOUT)
        print(f'Discord response {i}/{len(parts)}: {response.status_code}')
        response.raise_for_status()
    return True


def validate_levels(result):
    sl,tp1,tp2=calculate_trade_levels(result['price'],result['atr'],result['signal'],
        result['support'],result['resistance'],result['nearest_support'],result['nearest_resistance'])
    if result['signal'] in (SIG_BUY,SIG_SELL) and sl is None:
        result['trade_advice']='AVOID'
        result['trade_advice_reason']='Structural SL/TP validation failed'
    return sl,tp1,tp2


def format_normal(symbol,r,sl,tp1,tp2):
    p=lambda v:f'{v:.8g}' if v is not None else 'N/A'
    advice=r['trade_advice']
    banner=('\u2705 TRADE ELIGIBLE' if advice=='ELIGIBLE' else
            '\u26d4 TRADE AVOID - Do not enter' if advice=='AVOID' else
            '\u23f3 NO TRADE - WAIT')
    return (f'**{symbol}**\n'
            f'\U0001f4e1 Data: Bitget USDT Perpetual\n'
            f'\U0001f4b0 Entry: {p(r["price"])}\n'
            f'\U0001f4c8 Trend: {r["trend"]}\n'
            f'\U0001f550 1H Trend: {r["trend_1h"]}\n'
            f'\U0001f553 4H Trend: {r["trend_4h"]}\n'
            f'\U0001f4ca RSI: {r["rsi"]:.2f}\n'
            f'\u303d\ufe0f EMA20: {p(r["ema20"])}\n'
            f'\u303d\ufe0f EMA50: {p(r["ema50"])}\n'
            f'\U0001f4aa ADX: {r["adx"]:.2f}\n'
            f'\u2197\ufe0f +DI: {r["plus_di"]:.2f} | \u2198\ufe0f -DI: {r["minus_di"]:.2f}\n'
            f'\U0001f4e6 Volume: {r["volume_ratio"]:.2f}x\n'
            f'\U0001f3af Signal: {r["signal"]}\n'
            f'\U0001f9ea Paper Signal: {r["paper_signal"]}\n'
            f'\U0001f514 Breakout Watch: {r["breakout_watch"]}\n'
            f'\U0001f4af Signal Score: {r["signal_score"]}/100\n'
            f'\u2b50 Setup Quality: {r["entry_quality"]} ({r["entry_quality_score"]}/100; not win probability)\n'
            f'\U0001f4cc Setup: {r["entry_setup"]}\n'
            f'\U0001f56f\ufe0f Candle: {r["candle_status"]}\n'
            f'\U0001f3af Entry Timing: {r["entry_timing"]}\n'
            f'\U0001f4c9 Support: {p(r["nearest_support"])}\n'
            f'\U0001f4c8 Resistance: {p(r["nearest_resistance"])}\n'
            f'\U0001f6d1 Stop Loss: {p(sl)}\n'
            f'\U0001f3af TP1: {p(tp1)}\n'
            f'\U0001f3af TP2: {p(tp2)}\n'
            f'\u23f1\ufe0f Expected: {estimate_duration(r["price"],tp2,r["atr"])}\n'
            f'\U0001f4dd Reason: {r["reason"]}\n\n'
            f'{banner}\n\U0001f50e Advice Reason: {r["trade_advice_reason"]}')


def normal_main():
    print('Crypto Signal Bot V4 - NORMAL 15m closed-candle report')
    messages=[]
    for symbol in COINS:
        try:
            r=get_signal(symbol)
            if r is None:continue
            sl,tp1,tp2=validate_levels(r)
            messages.append(format_normal(symbol,r,sl,tp1,tp2))
            print(f'{symbol}: {r["signal"]} | {r["trade_advice"]}')
        except Exception as e:
            print(f'{symbol} error: {e}')
    if messages:send_discord('\n\n'.join(messages))


def load_fast_state():
    try:
        data=json.loads(FAST_STATE_FILE.read_text(encoding='utf-8'))
        if not isinstance(data,dict):raise ValueError('State must be dict')
        return data
    except FileNotFoundError:
        return {}
    except (ValueError,OSError) as e:
        # Fail closed: avoid duplicate alerts if state cannot be trusted.
        raise RuntimeError(f'Cannot safely read fast-alert state: {e}') from e


def save_fast_state(state):
    FAST_STATE_FILE.parent.mkdir(parents=True,exist_ok=True)
    tmp=FAST_STATE_FILE.with_name(FAST_STATE_FILE.name+'.tmp')
    tmp.write_text(json.dumps(state,sort_keys=True,indent=2)+'\n',encoding='utf-8')
    tmp.replace(FAST_STATE_FILE)


def fast_main():
    """Extra alert only for a fresh, fully eligible entry at the live ticker. A candidate's direction and candle pattern come from closed candles only. Re-evaluates price-sensitive checks and SL/TP using the ticker. """
    print('Crypto Signal Bot V4 - FAST ENTRY ALERT CHECK')
    if not WEBHOOK_URL:
        print('No Discord webhook; fast mode cannot send.');return
    state=load_fast_state()
    for symbol in COINS:
        try:
            live_price=get_live_price(symbol)
            r=get_signal(symbol,entry_price_override=live_price)
            if r is None:continue
            if r['candle_age']>FAST_MAX_CANDLE_AGE_MIN:
                print(f'FAST_SKIP {symbol}: confirmed candle too old');continue
            if r['signal_type'] not in ('BUY','SELL') or r['trade_advice']!='ELIGIBLE':
                print(f'FAST_SKIP {symbol}: {r["signal_type"]} / {r["trade_advice"]}')
                continue
            sl,tp1,tp2=validate_levels(r)
            if r['trade_advice']!='ELIGIBLE' or None in (sl,tp1,tp2):
                print(f'FAST_SKIP {symbol}: SL/TP failed');continue
            key=f'{symbol}:{r["signal_type"]}:{r["candle_id"]}'
            previous = state.get(symbol, {})
            if isinstance(previous, dict):
                same_setup = previous.get('key') == key
                same_direction_recent = (
                    previous.get('direction') == r['signal_type']
                    and time.time() - float(previous.get('sent_at', 0))
                    < FAST_SAME_DIRECTION_COOLDOWN_SEC
                )
            else:
                same_setup = previous == key
                same_direction_recent = False
            if same_setup or same_direction_recent:
                print(f'FAST_SKIP {symbol}: duplicate/cooldown {key}')
                continue
            message=(f'\u26a1 **FAST ENTRY ALERT - DEMO CHECK**\n'
                     f'**{symbol}** | {r["signal"]}\n'
                     f'\u2705 TRADE ELIGIBLE (preliminary checks)\n'
                     f'\U0001f4e1 Bitget USDT Perpetual - live last price\n'
                     f'\U0001f4b0 Entry: {live_price:.8g}\n'
                     f'\U0001f6d1 Stop Loss: {sl:.8g}\n'
                     f'\U0001f3af TP1: {tp1:.8g}\n'
                     f'\U0001f3af TP2: {tp2:.8g}\n'
                     f'\U0001f4af Score: {r["signal_score"]}/100 | '
                     f'Quality: {r["entry_quality_score"]}/100\n'
                     f'\U0001f550 1H: {r["trend_1h"]} | 4H: {r["trend_4h"]}\n'
                     f'\U0001f56f\ufe0f Closed candle: {r["candle_status"]}\n'
                     f'\U0001f4cc Setup: {r["entry_setup"]}\n'
                     f'\u26a0\ufe0f Demo first. Recheck execution price, fees and slippage.')
            # Save before send to avoid duplicate alerts on an ambiguous network failure.
            # If Discord fails, a missed alert is possible; no duplicate is preferred.
            state[symbol]={'key':key,'direction':r['signal_type'],'sent_at':time.time()}
            save_fast_state(state)
            if send_discord(message):print(f'FAST_ALERT_SENT {key}')
        except Exception as e:
            print(f'FAST_ERROR {symbol}: {e}')


def main():
    mode=os.getenv('BOT_MODE','normal').strip().lower()
    if mode=='normal':normal_main()
    elif mode=='fast':fast_main()
    else:raise ValueError(f'Invalid BOT_MODE: {mode!r} (normal or fast)')


if __name__=='__main__':
    main()

"""SHORO Bitget USDT-FUTURES price bridge. No exchange orders."""
import os
import requests

URL = os.environ['SHORO_WORKER_URL'].rstrip('/') + '/shoro-price-update'
SECRET = os.environ['SHORO_BRIDGE_SECRET']
SYMBOLS = ['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT','ZENUSDT','ZECUSDT']
s = requests.Session()
s.headers.update({'User-Agent':'SHORO-Price-Bridge/1.0'})
prices = {}
for symbol in SYMBOLS:
    r = s.get('https://api.bitget.com/api/v2/mix/market/ticker', params={'symbol':symbol,'productType':'USDT-FUTURES'},timeout=15)
    r.raise_for_status()
    body = r.json()
    if body.get('code') != '00000' or not body.get('data'):
        raise RuntimeError(f'Bitget ticker failed for {symbol}: {body.get("msg")}')
    price = float(body['data'][0]['lastPr'])
    if price <= 0:
        raise ValueError(f'Invalid price for {symbol}')
    prices[symbol] = price
r = s.post(URL, headers={'Authorization':f'Bearer {SECRET}'}, json={'prices':prices}, timeout=20)
r.raise_for_status()
print('SHORO bridge updated',len(prices),'Bitget Futures prices:',r.status_code)

""" ============================================================================= Bitget Multi-Pair Quantitative Crypto Signal Bot Pairs: BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, ZECUSDT, ZENUSDT Timeframe: 15m | Leverage: 5x | Risk/Trade: 1.5% Execution Mode: Paper Trading (Simulation) ============================================================================= """

import os
import sys
import time
import math
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any
import requests
import pandas as pd
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bitget_signal_bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("BitgetSignalBot")

# =============================================================================
# Bot Configuration
# =============================================================================
CONFIG = {
    "PAIRS": [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "ZECUSDT",
    "ZENUSDT"
],
    "PRIMARY_TIMEFRAME": "15m",
    "SCAN_INTERVAL_SECONDS": 60,
    "PAPER_TRADING": True,
    "LEVERAGE": 5,
    "RISK_PER_TRADE_PCT": 1.5,
    "MIN_CONFIDENCE_SCORE": 75,
    
    # Strategy Indicator Weights
    "EMA_FAST": 20,
    "EMA_SLOW": 50,
    "EMA_TREND": 200,
    "RSI_PERIOD": 14,
    "RSI_OVERSOLD": 32,
    "RSI_OVERBOUGHT": 68,
    "ATR_PERIOD": 14,
    "ATR_SL_MULT": 1.5,
    "ATR_TP1_MULT": 1.5,
    "ATR_TP2_MULT": 2.8,
    "ATR_TP3_MULT": 4.5,
    
    # Credentials & Discord Webhook Alerts
    "BITGET_API_KEY": os.getenv("BITGET_API_KEY", ""),
    "BITGET_API_SECRET": os.getenv("BITGET_API_SECRET", ""),
    "BITGET_API_PASSPHRASE": os.getenv("BITGET_API_PASSPHRASE", ""),
    "DISCORD_WEBHOOK_URL": os.getenv("DISCORD_WEBHOOK_URL", ""),
    "DISCORD_PING_TAG": os.getenv("DISCORD_PING_TAG", ""),
}

BITGET_API_URL = "https://api.bitget.com"

import pandas as pd

class IndicatorEngine:
    @staticmethod
    def calculate_ema(series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

@staticmethod
    def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / (loss + 1e-9)
        return 100 - (100 / (1 + rs))

@staticmethod
    def calculate_macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
        ema_fast = series.ewm(span=fast, adjust=False).mean()
        ema_slow = series.ewm(span=slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        sig = macd.ewm(span=signal, adjust=False).mean()
        return macd, sig, macd - sig

@staticmethod
    def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        high = df['high']
        low = df['low']
        close_prev = df['close'].shift(1)
        tr1 = high - low
        tr2 = (high - close_prev).abs()
        tr3 = (low - close_prev).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.rolling(window=period).mean()


class StrategyConfluenceEvaluator:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg

    def evaluate_dataframe(self, df: pd.DataFrame, symbol: str) -> Optional[Dict[str, Any]]:
        if len(df) < 205:
            return None

        close = df['close']
        df['ema_fast'] = IndicatorEngine.calculate_ema(close, self.cfg['EMA_FAST'])
        df['ema_slow'] = IndicatorEngine.calculate_ema(close, self.cfg['EMA_SLOW'])
        df['ema_trend'] = IndicatorEngine.calculate_ema(close, self.cfg['EMA_TREND'])
        df['rsi'] = IndicatorEngine.calculate_rsi(close, self.cfg['RSI_PERIOD'])
        df['macd'], df['macd_sig'], df['macd_hist'] = IndicatorEngine.calculate_macd(close)
        df['atr'] = IndicatorEngine.calculate_atr(df, self.cfg['ATR_PERIOD'])
        df['vol_ma'] = df['volume'].rolling(window=20).mean()

        curr = df.iloc[-1]
        prev = df.iloc[-2]
        price = float(curr['close'])
        atr = float(curr['atr']) if not math.isnan(curr['atr']) else price * 0.015

        long_score, short_score = 0, 0
        long_reasons, short_reasons = [], []

        # 1. Trend 50/200 EMA
        if curr['close'] > curr['ema_trend'] and curr['ema_slow'] > curr['ema_trend']:
            long_score += 25
            long_reasons.append("Bullish Trend: Price & 50 EMA above 200 EMA")
        if curr['close'] < curr['ema_trend'] and curr['ema_slow'] < curr['ema_trend']:
            short_score += 25
            short_reasons.append("Bearish Trend: Price & 50 EMA below 200 EMA")

        # 2. Fast Momentum 20/50 EMA
        if curr['ema_fast'] > curr['ema_slow']:
            long_score += 20
            long_reasons.append("20 EMA above 50 EMA")
        else:
            short_score += 20
            short_reasons.append("20 EMA below 50 EMA")

        # 3. RSI Pullback Expansion
        rsi_val = float(curr['rsi'])
        if 42 <= rsi_val <= 62:
            long_score += 20
            long_reasons.append(f"Ideal RSI Expansion Zone ({rsi_val:.1f})")
        elif rsi_val < self.cfg['RSI_OVERSOLD']:
            long_score += 15
            long_reasons.append("RSI Oversold Bounce")

        if 38 <= rsi_val <= 58:
            short_score += 20
            short_reasons.append(f"Ideal Bearish RSI Zone ({rsi_val:.1f})")
        elif rsi_val > self.cfg['RSI_OVERBOUGHT']:
            short_score += 15
            short_reasons.append("RSI Overbought Rejection")

        # 4. MACD Momentum
        if curr['macd_hist'] > 0:
            long_score += 20
            long_reasons.append("MACD Bullish Histogram")
        else:
            short_score += 20
            short_reasons.append("MACD Bearish Histogram")

        # 5. Volume Surge
        vol_ratio = float(curr['volume']) / (float(curr['vol_ma']) + 1e-9)
        if vol_ratio >= 1.15:
            long_score += 15
            short_score += 15
            long_reasons.append(f"Volume Surge ({vol_ratio:.2f}x)")
            short_reasons.append(f"Volume Surge ({vol_ratio:.2f}x)")

        min_conf = self.cfg['MIN_CONFIDENCE_SCORE']
        if long_score >= min_conf and long_score > short_score:
            sl = price - (atr * self.cfg['ATR_SL_MULT'])
            tp1 = price + (atr * self.cfg['ATR_TP1_MULT'])
            tp2 = price + (atr * self.cfg['ATR_TP2_MULT'])
            tp3 = price + (atr * self.cfg['ATR_TP3_MULT'])
            rr = (tp2 - price) / (price - sl + 1e-9)
            return {
                "symbol": symbol, "action": "LONG", "confidence": long_score,
                "price": price, "stop_loss": round(sl, 4 if price < 10 else 2),
                "take_profit_1": round(tp1, 4 if price < 10 else 2),
                "take_profit_2": round(tp2, 4 if price < 10 else 2),
                "take_profit_3": round(tp3, 4 if price < 10 else 2),
                "risk_reward": round(rr, 2), "atr": round(atr, 4), "rsi": round(rsi_val, 2),
                "reasons": long_reasons, "timestamp": datetime.now(timezone.utc).isoformat()
            }
        elif short_score >= min_conf and short_score > long_score:
            sl = price + (atr * self.cfg['ATR_SL_MULT'])
            tp1 = price - (atr * self.cfg['ATR_TP1_MULT'])
            tp2 = price - (atr * self.cfg['ATR_TP2_MULT'])
            tp3 = price - (atr * self.cfg['ATR_TP3_MULT'])
            rr = (price - tp2) / (sl - price + 1e-9)
            return {
                "symbol": symbol, "action": "SHORT", "confidence": short_score,
                "price": price, "stop_loss": round(sl, 4 if price < 10 else 2),
                "take_profit_1": round(tp1, 4 if price < 10 else 2),
                "take_profit_2": round(tp2, 4 if price < 10 else 2),
                "take_profit_3": round(tp3, 4 if price < 10 else 2),
                "risk_reward": round(rr, 2), "atr": round(atr, 4), "rsi": round(rsi_val, 2),
                "reasons": short_reasons, "timestamp": datetime.now(timezone.utc).isoformat()
            }
        return None


class BitgetClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def fetch_klines(self, symbol: str, granularity: str = "15m", limit: int = 250) -> pd.DataFrame:
        url = f"{BITGET_API_URL}/api/v2/mix/market/candles"
        params = {"symbol": symbol, "productType": "USDT-FUTURES", "granularity": granularity, "limit": str(limit)}
        try:
            r = self.session.get(url, params=params, timeout=10)
            data = r.json()
            if data.get("code") == "00000" and data.get("data"):
                records = [{
                    "timestamp": int(x[0]), "open": float(x[1]), "high": float(x[2]),
                    "low": float(x[3]), "close": float(x[4]), "volume": float(x[5])
                } for x in data["data"]]
                df = pd.DataFrame(records).sort_values(by="timestamp").reset_index(drop=True)
                return df
        except Exception as e:
            logger.error(f"[{symbol}] Failed to fetch candles: {e}")
        return pd.DataFrame()


class DiscordWebhookDispatcher:
    def __init__(self, webhook_url: str, ping_tag: str = ""):
        self.webhook_url = webhook_url
        self.ping_tag = ping_tag

    def send_signal(self, sig: Dict[str, Any]):
        is_long = sig['action'] == 'LONG'
        tag_str = "🟢 BUY (LONG)" if is_long else "🔴 SELL (SHORT)"
        color = 0x10B981 if is_long else 0xF43F5E
        mention_prefix = f"{self.ping_tag} " if self.ping_tag else ""
        payload = {
            "username": "Bitget Signal Bot",
            "avatar_url": "https://cdn-icons-png.flaticon.com/512/6001/6001368.png",
            "content": f"{mention_prefix}🚨 **{tag_str} SIGNAL DETECTED** • **{sig['symbol']}** @ **{sig['price']} USDT** | Confidence: **{sig['confidence']}%**",
            "embeds": [
                {
                    "title": f"{'🟢 BUY / LONG' if is_long else '🔴 SELL / SHORT'} SIGNAL: {sig['symbol']}",
                    "description": f"**Signal Action Tag:** **{tag_str}**\n**Confidence:** {sig['confidence']}% (High-Probability Confluence)\n**Timeframe:** {CONFIG['PRIMARY_TIMEFRAME']}",
                    "color": color,
                    "fields": [
                        {"name": "🏷️ Signal Action Tag", "value": f"**{tag_str}**", "inline": True},
                        {"name": "💵 Entry Price", "value": f"{sig['price']} USDT", "inline": True},
                        {"name": "🛑 Stop Loss", "value": f"{sig['stop_loss']} USDT (1.4x ATR)", "inline": True},
                        {"name": "⚖️ Risk:Reward", "value": f"1:{sig['risk_reward']}", "inline": True},
                        {"name": "🎯 TP1 (50% & BE)", "value": f"{sig['take_profit_1']} USDT", "inline": True},
                        {"name": "🎯 TP2 (30%)", "value": f"{sig['take_profit_2']} USDT", "inline": True},
                        {"name": "🚀 Runner TP3 (20%)", "value": f"{sig['take_profit_3']} USDT", "inline": True},
                        {"name": "📋 Confluence Triggers", "value": "\n".join([f"• {r}" for r in sig['reasons']]), "inline": False}
                    ],
                    "footer": {"text": "Bitget Quantitative Confluence Engine · Automated Alert"},
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
            ]
        }
        logger.info(f"SIGNAL TRIGGERED: {tag_str} {sig['symbol']} @ {sig['price']}")
        if self.webhook_url:
            try:
                requests.post(self.webhook_url, json=payload, timeout=8)
            except Exception as e:
                logger.error(f"Failed to send Discord alert: {e}")


def main():
    logger.info("Starting Bitget Quantitative Trading Bot with Discord Alerts...")
    client = BitgetClient()
    strategy = StrategyConfluenceEvaluator(CONFIG)
    dispatcher = DiscordWebhookDispatcher(CONFIG['DISCORD_WEBHOOK_URL'], CONFIG.get('DISCORD_PING_TAG', ''))

    while True:
        try:
            for symbol in CONFIG['PAIRS']:
                df = client.fetch_klines(symbol, CONFIG['PRIMARY_TIMEFRAME'], 250)
                if not df.empty:
                    sig = strategy.evaluate_dataframe(df, symbol)
                    if sig:
                        dispatcher.send_signal(sig)
                time.sleep(0.5)
            time.sleep(CONFIG['SCAN_INTERVAL_SECONDS'])
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Error in scan loop: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()

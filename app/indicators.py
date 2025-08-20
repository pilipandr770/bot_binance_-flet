# indicators.py - Индикаторная система для торгового бота
import pandas as pd
import numpy as np
import ta
from typing import Dict, Any, List, Optional, Tuple

def prepare_dataframe(closes: List[float], opens: List[float], highs: List[float], lows: List[float]) -> pd.DataFrame:
    """
    Преобразовать массивы цен в pandas DataFrame для работы с TA-Lib
    """
    df = pd.DataFrame({
        'open': opens,
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': [0] * len(closes)  # Объем не используется в наших индикаторах
    })
    return df

def calculate_ma(df: pd.DataFrame, window: int) -> pd.Series:
    """Рассчитать скользящую среднюю"""
    return df['close'].rolling(window).mean()

def calculate_rsi(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Рассчитать RSI индикатор"""
    return ta.momentum.RSIIndicator(df['close'], window=window).rsi()

def calculate_atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Рассчитать ATR индикатор"""
    return ta.volatility.AverageTrueRange(
        high=df['high'], low=df['low'], close=df['close'], window=window
    ).average_true_range()

class IndicatorBasedStrategy:
    """
    Стратегия торговли на основе множественных индикаторов и таймфреймов
    """
    
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.base_asset = symbol[:-4] if symbol.endswith("USDT") else symbol.split("USDT")[0]
        self.quote_asset = "USDT"
        
    def analyze(self, data_30m, data_1h, data_4h) -> Dict[str, Any]:
        """
        Проанализировать данные и вернуть сигналы для принятия решений
        
        Args:
            data_30m: Dict с данными свечей 30м (устаревшее, но сохранено для совместимости)
            data_1h: Dict с данными свечей 1ч (OHLC) - основной таймфрейм для торговли
            data_4h: Dict с данными свечей 4ч (OHLC) - для определения тренда и волатильности
        
        Returns:
            Dict с результатами анализа и сигналами
        """
        # Создаем DataFrame для каждого таймфрейма
        df_30m = self._prepare_dataframe(data_30m)
        df_1h = self._prepare_dataframe(data_1h)
        df_4h = self._prepare_dataframe(data_4h)
        
        # === 1h индикаторы (основной таймфрейм для торговли) ===
        df_1h['ma7'] = df_1h['close'].rolling(7).mean()
        df_1h['ma25'] = df_1h['close'].rolling(25).mean()
        
        signal_1h = "none"
        if df_1h['ma7'].iloc[-1] > df_1h['ma25'].iloc[-1]:
            signal_1h = "long"
        elif df_1h['ma7'].iloc[-1] < df_1h['ma25'].iloc[-1]:
            signal_1h = "short"

        # === 4h индикаторы для анализа волатильности и силы тренда ===
        rsi_4h = calculate_rsi(df_4h).iloc[-1]
        atr_4h = calculate_atr(df_4h).iloc[-1]
        price_4h = df_4h['close'].iloc[-1]
        
        # Рассчитываем ATR в процентах от цены
        atr_percent = atr_4h / price_4h * 100

        # === 4h индикаторы для определения тренда ===
        df_4h['ma7'] = df_4h['close'].rolling(7).mean()
        df_4h['ma25'] = df_4h['close'].rolling(25).mean()
        
        ma7_4h = df_4h['ma7'].iloc[-1]
        ma25_4h = df_4h['ma25'].iloc[-1]
        
        trend_4h = "sideways"
        if ma7_4h > ma25_4h:
            trend_4h = "long"
        elif ma7_4h < ma25_4h:
            trend_4h = "short"
        
        # Определяем состояние рынка на основе 1ч торгового таймфрейма и 4ч анализа тренда
        market_state = self._determine_market_state(atr_percent, rsi_4h, trend_4h, signal_1h)
        
        return {
            "1h": {
                "ma7": df_1h['ma7'].iloc[-1],
                "ma25": df_1h['ma25'].iloc[-1],
                "signal": signal_1h,
                "price": df_1h['close'].iloc[-1]
            },
            "4h": {
                "ma7": ma7_4h,
                "ma25": ma25_4h,
                "trend": trend_4h,
                "rsi": rsi_4h,
                "atr": atr_4h,
                "atr_percent": atr_percent,
                "price": price_4h
            },
            "market_state": market_state,
            "should_hold_base": market_state == "buy",
            "recommendation": market_state
        }
    
    def _prepare_dataframe(self, data: Dict[str, List[float]]) -> pd.DataFrame:
        """Подготовить pandas DataFrame из данных свечей"""
        return pd.DataFrame({
            'open': data.get('open', []),
            'high': data.get('high', []),
            'low': data.get('low', []),
            'close': data.get('close', []),
            'volume': data.get('volume', [0] * len(data.get('close', [])))
        })
    
    def _determine_market_state(self, atr_percent: float, rsi: float, trend_4h: str, signal_1h: str) -> str:
        """
        Определить состояние рынка на основе индикаторов
        
        Returns:
            "flet" - боковой рынок (не торгуем)
            "buy" - покупаем базовый актив
            "sell" - продаем базовый актив
            "no_trade" - нет четкого сигнала
        """
        # 1. Флет (бот отключен)
        if atr_percent < 0.4 and (45 <= rsi <= 55) and trend_4h == "sideways":
            return "flet"

        # 2. Выход из флета = обратные условия
        if atr_percent >= 0.4 and (rsi > 55 or rsi < 45) and trend_4h != "sideways":
            if signal_1h == "long" and trend_4h == "long":
                return "buy"
            elif signal_1h == "short" and trend_4h == "short":
                return "sell"

        return "no_trade"

"""
策略3: RSI均值回归策略

核心逻辑:
- RSI < 超卖线 (30) → 买入（超卖反弹）
- RSI > 超买线 (70) → 卖出（超买回调）
- 可结合成交量确认

适用场景: 震荡市、区间交易
"""
import pandas as pd
import numpy as np
from datetime import datetime
from typing import List, Dict

from backtest.engine import BaseStrategy, Signal, Portfolio


class RSIStrategy(BaseStrategy):
    """RSI均值回归策略"""

    name = "RSI均值回归"

    DEFAULT_PARAMS = {
        "rsi_period": 14,
        "oversold": 30,        # 超卖线
        "overbought": 70,      # 超买线
        "position_pct": 0.25,
    }

    def __init__(self, params: dict = None):
        merged = {**self.DEFAULT_PARAMS, **(params or {})}
        super().__init__(merged)

    def _calc_rsi(self, close: pd.Series, period: int) -> pd.Series:
        """计算RSI"""
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)

        avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def on_bar(
        self, date: datetime, data: Dict[str, pd.DataFrame], portfolio: Portfolio
    ) -> List[Signal]:
        signals = []

        for symbol, df in data.items():
            period = self.params["rsi_period"]
            if len(df) < period + 5:
                continue

            rsi = self._calc_rsi(df["close"], period)
            current_rsi = rsi.iloc[-1]
            prev_rsi = rsi.iloc[-2]
            current_price = df["close"].iloc[-1]

            # 超卖反弹: RSI从超卖区域回升
            if prev_rsi < self.params["oversold"] and current_rsi >= self.params["oversold"]:
                if symbol not in portfolio.positions:
                    # 信号强度根据超卖程度
                    strength = min(self.params["position_pct"] * (1 + (30 - prev_rsi) / 30), 0.5)
                    signals.append(Signal(
                        date=date,
                        symbol=symbol,
                        action="BUY",
                        strength=strength,
                        price=current_price,
                        reason=f"RSI超卖反弹 ({prev_rsi:.1f} → {current_rsi:.1f})",
                    ))

            # 超买回落: RSI从超买区域下降
            elif prev_rsi > self.params["overbought"] and current_rsi <= self.params["overbought"]:
                if symbol in portfolio.positions:
                    signals.append(Signal(
                        date=date,
                        symbol=symbol,
                        action="SELL",
                        strength=1.0,
                        price=current_price,
                        reason=f"RSI超买回落 ({prev_rsi:.1f} → {current_rsi:.1f})",
                    ))

        return signals

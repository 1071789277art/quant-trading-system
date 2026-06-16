"""
策略4: 布林带突破策略 (Bollinger Bands Breakout)

核心逻辑:
- 价格突破下轨 + 带宽收窄 → 买入（波动率收缩后的反弹）
- 价格突破上轨 → 卖出（过度扩张）
- 结合带宽（Bandwidth）判断波动率状态

适用场景: 波动率扩张/收缩周期
"""
import pandas as pd
import numpy as np
from datetime import datetime
from typing import List, Dict

from backtest.engine import BaseStrategy, Signal, Portfolio


class BollingerStrategy(BaseStrategy):
    """布林带突破策略"""

    name = "布林带突破"

    DEFAULT_PARAMS = {
        "bb_period": 20,        # 布林带周期
        "bb_std": 2.0,          # 标准差倍数
        "bandwidth_threshold": 0.05,  # 带宽收窄阈值
        "position_pct": 0.25,
    }

    def __init__(self, params: dict = None):
        merged = {**self.DEFAULT_PARAMS, **(params or {})}
        super().__init__(merged)

    def _calc_bollinger(self, close: pd.Series):
        """计算布林带"""
        period = self.params["bb_period"]
        std_mult = self.params["bb_std"]

        middle = close.rolling(period).mean()
        std = close.rolling(period).std()
        upper = middle + std_mult * std
        lower = middle - std_mult * std
        bandwidth = (upper - lower) / middle  # 带宽

        return upper, middle, lower, bandwidth

    def on_bar(
        self, date: datetime, data: Dict[str, pd.DataFrame], portfolio: Portfolio
    ) -> List[Signal]:
        signals = []

        for symbol, df in data.items():
            period = self.params["bb_period"]
            if len(df) < period + 5:
                continue

            close = df["close"]
            upper, middle, lower, bandwidth = self._calc_bollinger(close)

            current_price = close.iloc[-1]
            prev_price = close.iloc[-2]
            curr_upper = upper.iloc[-1]
            curr_lower = lower.iloc[-1]
            curr_middle = middle.iloc[-1]
            curr_bw = bandwidth.iloc[-1]

            # 买入条件：价格触及下轨且带宽收窄（波动率收缩）
            if prev_price <= lower.iloc[-2] and current_price > curr_lower:
                if curr_bw < self.params["bandwidth_threshold"] * 2:
                    if symbol not in portfolio.positions:
                        signals.append(Signal(
                            date=date,
                            symbol=symbol,
                            action="BUY",
                            strength=self.params["position_pct"],
                            price=current_price,
                            reason=f"触及下轨反弹 (价格={current_price:.2f}, 下轨={curr_lower:.2f}, 带宽={curr_bw:.4f})",
                        ))

            # 卖出条件：价格触及上轨
            elif prev_price >= upper.iloc[-2] and current_price < curr_upper:
                if symbol in portfolio.positions:
                    signals.append(Signal(
                        date=date,
                        symbol=symbol,
                        action="SELL",
                        strength=0.5,
                        price=current_price,
                        reason=f"触及上轨回落 (价格={current_price:.2f}, 上轨={curr_upper:.2f})",
                    ))

            # 跌破中轨全部卖出
            elif current_price < curr_middle and prev_price >= middle.iloc[-2]:
                if symbol in portfolio.positions:
                    signals.append(Signal(
                        date=date,
                        symbol=symbol,
                        action="SELL",
                        strength=1.0,
                        price=current_price,
                        reason=f"跌破中轨 (价格={current_price:.2f}, 中轨={curr_middle:.2f})",
                    ))

        return signals

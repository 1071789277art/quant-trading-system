"""
策略2: MACD策略

核心逻辑:
- MACD线上穿信号线 且 柱状图由负转正 → 买入
- MACD线下穿信号线 且 柱状图由正转负 → 卖出
- 结合零轴过滤：仅在MACD>0时做多

适用场景: 中长期趋势跟踪
"""
import pandas as pd
import numpy as np
from datetime import datetime
from typing import List, Dict

from backtest.engine import BaseStrategy, Signal, Portfolio


class MACDStrategy(BaseStrategy):
    """MACD策略"""

    name = "MACD策略"

    DEFAULT_PARAMS = {
        "fast_period": 12,
        "slow_period": 26,
        "signal_period": 9,
        "zero_filter": True,       # 是否在零轴上方才买入
        "position_pct": 0.3,
    }

    def __init__(self, params: dict = None):
        merged = {**self.DEFAULT_PARAMS, **(params or {})}
        super().__init__(merged)

    def _calc_macd(self, close: pd.Series):
        """计算MACD指标"""
        fast = self.params["fast_period"]
        slow = self.params["slow_period"]
        signal_p = self.params["signal_period"]

        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal_p, adjust=False).mean()
        histogram = macd_line - signal_line

        return macd_line, signal_line, histogram

    def on_bar(
        self, date: datetime, data: Dict[str, pd.DataFrame], portfolio: Portfolio
    ) -> List[Signal]:
        signals = []

        for symbol, df in data.items():
            if len(df) < self.params["slow_period"] + self.params["signal_period"] + 2:
                continue

            macd, signal, hist = self._calc_macd(df["close"])
            current_price = df["close"].iloc[-1]

            # 当前与前一根
            m_now, s_now, h_now = macd.iloc[-1], signal.iloc[-1], hist.iloc[-1]
            m_prev, s_prev, h_prev = macd.iloc[-2], signal.iloc[-2], hist.iloc[-2]

            # 金叉: MACD线上穿信号线
            if m_prev <= s_prev and m_now > s_now:
                # 零轴过滤
                if self.params["zero_filter"] and m_now < 0:
                    continue
                if symbol not in portfolio.positions:
                    signals.append(Signal(
                        date=date,
                        symbol=symbol,
                        action="BUY",
                        strength=self.params["position_pct"],
                        price=current_price,
                        reason=f"MACD金叉 (DIF={m_now:.3f}, DEA={s_now:.3f})",
                    ))

            # 死叉: MACD线下穿信号线
            elif m_prev >= s_prev and m_now < s_now:
                if symbol in portfolio.positions:
                    signals.append(Signal(
                        date=date,
                        symbol=symbol,
                        action="SELL",
                        strength=1.0,
                        price=current_price,
                        reason=f"MACD死叉 (DIF={m_now:.3f}, DEA={s_now:.3f})",
                    ))

        return signals

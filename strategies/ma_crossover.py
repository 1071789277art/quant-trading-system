"""
策略1: 双均线交叉策略 (Moving Average Crossover)

核心逻辑:
- 短期均线上穿长期均线 → 买入（金叉）
- 短期均线下穿长期均线 → 卖出（死叉）

适用场景: 趋势明显的市场
"""
import pandas as pd
from datetime import datetime
from typing import List, Dict

from backtest.engine import BaseStrategy, Signal, Portfolio


class MACrossover(BaseStrategy):
    """双均线交叉策略"""

    name = "双均线交叉"

    DEFAULT_PARAMS = {
        "fast_period": 5,     # 短期均线周期
        "slow_period": 20,    # 长期均线周期
        "position_pct": 0.3,  # 每只股票的仓位比例
    }

    def __init__(self, params: dict = None):
        merged = {**self.DEFAULT_PARAMS, **(params or {})}
        super().__init__(merged)

    def on_bar(
        self, date: datetime, data: Dict[str, pd.DataFrame], portfolio: Portfolio
    ) -> List[Signal]:
        signals = []
        fast = self.params["fast_period"]
        slow = self.params["slow_period"]

        for symbol, df in data.items():
            if len(df) < slow + 1:
                continue

            close = df["close"]
            ma_fast = close.rolling(fast).mean()
            ma_slow = close.rolling(slow).mean()

            # 当前值和前一根K线
            curr_fast = ma_fast.iloc[-1]
            curr_slow = ma_slow.iloc[-1]
            prev_fast = ma_fast.iloc[-2]
            prev_slow = ma_slow.iloc[-2]

            current_price = close.iloc[-1]

            # 金叉: 短均线从下方穿越长均线
            if prev_fast <= prev_slow and curr_fast > curr_slow:
                if symbol not in portfolio.positions:
                    signals.append(Signal(
                        date=date,
                        symbol=symbol,
                        action="BUY",
                        strength=self.params["position_pct"],
                        price=current_price,
                        reason=f"金叉 (MA{fast}={curr_fast:.2f} > MA{slow}={curr_slow:.2f})",
                    ))

            # 死叉: 短均线从上方穿越长均线
            elif prev_fast >= prev_slow and curr_fast < curr_slow:
                if symbol in portfolio.positions:
                    signals.append(Signal(
                        date=date,
                        symbol=symbol,
                        action="SELL",
                        strength=1.0,
                        price=current_price,
                        reason=f"死叉 (MA{fast}={curr_fast:.2f} < MA{slow}={curr_slow:.2f})",
                    ))

        return signals

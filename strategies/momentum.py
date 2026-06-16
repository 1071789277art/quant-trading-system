"""
策略5: 动量策略 (Momentum)

核心逻辑:
- 过去N天涨幅排名靠前的股票 → 买入（强者恒强）
- 动量衰减（近期涨幅收窄或转负） → 卖出
- 定期再平衡（每月一次）

适用场景: 多股票组合轮动
"""
import pandas as pd
import numpy as np
from datetime import datetime
from typing import List, Dict

from backtest.engine import BaseStrategy, Signal, Portfolio


class MomentumStrategy(BaseStrategy):
    """动量轮动策略"""

    name = "动量轮动"

    DEFAULT_PARAMS = {
        "lookback": 20,          # 动量回看天数
        "top_n": 3,              # 选取前N只
        "rebalance_days": 20,    # 再平衡周期（交易日）
        "momentum_decay": 0.0,   # 动量衰减阈值（涨幅低于此值卖出）
        "max_position": 5,       # 最大持仓数
    }

    def __init__(self, params: dict = None):
        merged = {**self.DEFAULT_PARAMS, **(params or {})}
        super().__init__(merged)
        self._last_rebalance = None
        self._days_since_rebalance = 0

    def on_bar(
        self, date: datetime, data: Dict[str, pd.DataFrame], portfolio: Portfolio
    ) -> List[Signal]:
        self._days_since_rebalance += 1
        signals = []

        # 检查是否需要再平衡
        if self._days_since_rebalance < self.params["rebalance_days"]:
            # 非再平衡日，只做动量衰减检查
            return self._check_momentum_decay(date, data, portfolio)

        self._days_since_rebalance = 0
        lookback = self.params["lookback"]
        top_n = self.params["top_n"]

        # 计算各标的动量
        momentums = {}
        for symbol, df in data.items():
            if len(df) < lookback + 1:
                continue
            close = df["close"]
            ret = (close.iloc[-1] / close.iloc[-lookback - 1]) - 1
            momentums[symbol] = ret

        if not momentums:
            return signals

        # 排序选取前N
        sorted_symbols = sorted(momentums.items(), key=lambda x: x[1], reverse=True)
        target_symbols = set(sym for sym, mom in sorted_symbols[:top_n] if mom > 0)

        # 生成卖出信号：不在目标列表中的持仓
        for sym in list(portfolio.positions.keys()):
            if sym not in target_symbols:
                signals.append(Signal(
                    date=date,
                    symbol=sym,
                    action="SELL",
                    strength=1.0,
                    price=data[sym]["close"].iloc[-1] if sym in data else 0,
                    reason=f"动量轮出 (动量排名下降)",
                ))

        # 生成买入信号：新进入目标列表的标的
        current_held = set(portfolio.positions.keys())
        available_slots = self.params["max_position"] - len(current_held) + len(
            [s for s in signals if s.action == "SELL"]
        )

        for sym in target_symbols:
            if sym not in current_held and available_slots > 0:
                signals.append(Signal(
                    date=date,
                    symbol=sym,
                    action="BUY",
                    strength=1.0 / self.params["max_position"],
                    price=data[sym]["close"].iloc[-1],
                    reason=f"动量轮入 (动量={momentums[sym]:.2%}, 排名Top{top_n})",
                ))
                available_slots -= 1

        return signals

    def _check_momentum_decay(
        self, date: datetime, data: Dict[str, pd.DataFrame], portfolio: Portfolio
    ) -> List[Signal]:
        """检查动量衰减"""
        signals = []
        lookback = self.params["lookback"]
        threshold = self.params["momentum_decay"]

        for sym in list(portfolio.positions.keys()):
            if sym not in data or len(data[sym]) < lookback + 1:
                continue

            close = data[sym]["close"]
            recent_momentum = (close.iloc[-1] / close.iloc[-lookback - 1]) - 1

            if recent_momentum < threshold:
                signals.append(Signal(
                    date=date,
                    symbol=sym,
                    action="SELL",
                    strength=1.0,
                    price=close.iloc[-1],
                    reason=f"动量衰减 (近{lookback}日涨幅={recent_momentum:.2%})",
                ))

        return signals

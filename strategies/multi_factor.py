"""
策略6: 多因子选股策略 (Multi-Factor)

核心逻辑:
综合多个因子打分，选取综合得分最高的股票
- 动量因子: 过去N日收益率
- 波动率因子: 低波动率加分
- 成交量因子: 量价齐升加分
- 均线偏离因子: 价格在均线附近加分（不追高）

适用场景: 多标的横向比较选股
"""
import pandas as pd
import numpy as np
from datetime import datetime
from typing import List, Dict

from backtest.engine import BaseStrategy, Signal, Portfolio


class MultiFactorStrategy(BaseStrategy):
    """多因子选股策略"""

    name = "多因子选股"

    DEFAULT_PARAMS = {
        "lookback": 20,           # 因子计算回看天数
        "top_n": 3,               # 选取前N只
        "rebalance_days": 10,     # 再平衡周期
        "max_position": 5,
        # 因子权重
        "w_momentum": 0.3,
        "w_volatility": 0.2,
        "w_volume": 0.2,
        "w_ma_deviation": 0.3,
    }

    def __init__(self, params: dict = None):
        merged = {**self.DEFAULT_PARAMS, **(params or {})}
        super().__init__(merged)
        self._days_since_rebalance = 0

    def _calc_factors(self, df: pd.DataFrame) -> Dict[str, float]:
        """计算单只股票的多因子得分"""
        lookback = self.params["lookback"]
        if len(df) < lookback + 5:
            return None

        close = df["close"]
        volume = df["volume"]

        # 1. 动量因子: 过去N日收益率（归一化到0-1）
        momentum = (close.iloc[-1] / close.iloc[-lookback] - 1)

        # 2. 波动率因子: 过去N日收益率标准差（越低越好，取倒数）
        daily_returns = close.pct_change().tail(lookback)
        volatility = daily_returns.std()
        vol_score = 1 / (volatility + 0.001)  # 避免除零

        # 3. 成交量因子: 近期成交量 vs 历史均量
        vol_ma = volume.rolling(lookback).mean().iloc[-1]
        vol_current = volume.tail(5).mean()
        volume_ratio = vol_current / (vol_ma + 1)  # >1 表示放量

        # 4. 均线偏离因子: 价格与MA20的距离（越近越好）
        ma20 = close.rolling(20).mean().iloc[-1]
        deviation = abs(close.iloc[-1] - ma20) / ma20
        deviation_score = max(0, 1 - deviation * 5)  # 偏离越大分越低

        return {
            "momentum": momentum,
            "volatility": vol_score,
            "volume": volume_ratio,
            "ma_deviation": deviation_score,
        }

    def _normalize_scores(self, all_factors: Dict[str, Dict[str, float]]) -> Dict[str, float]:
        """归一化因子并计算综合得分"""
        if not all_factors:
            return {}

        df = pd.DataFrame(all_factors).T
        # 每列归一化到 0-1
        for col in df.columns:
            col_min = df[col].min()
            col_max = df[col].max()
            if col_max > col_min:
                df[col] = (df[col] - col_min) / (col_max - col_min)
            else:
                df[col] = 0.5

        # 加权综合得分
        df["score"] = (
            df["momentum"] * self.params["w_momentum"]
            + df["volatility"] * self.params["w_volatility"]
            + df["volume"] * self.params["w_volume"]
            + df["ma_deviation"] * self.params["w_ma_deviation"]
        )

        return df["score"].to_dict()

    def on_bar(
        self, date: datetime, data: Dict[str, pd.DataFrame], portfolio: Portfolio
    ) -> List[Signal]:
        self._days_since_rebalance += 1
        signals = []

        if self._days_since_rebalance < self.params["rebalance_days"]:
            return signals

        self._days_since_rebalance = 0
        top_n = self.params["top_n"]

        # 计算所有标的的因子
        all_factors = {}
        for symbol, df in data.items():
            factors = self._calc_factors(df)
            if factors:
                all_factors[symbol] = factors

        # 归一化打分
        scores = self._normalize_scores(all_factors)
        if not scores:
            return signals

        # 排序选取前N
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        target_symbols = set(sym for sym, score in sorted_scores[:top_n])

        # 卖出不在目标中的持仓
        for sym in list(portfolio.positions.keys()):
            if sym not in target_symbols:
                signals.append(Signal(
                    date=date,
                    symbol=sym,
                    action="SELL",
                    strength=1.0,
                    price=data[sym]["close"].iloc[-1],
                    reason=f"因子轮出 (得分={scores.get(sym, 0):.3f})",
                ))

        # 买入新进入目标的标的
        current_held = set(portfolio.positions.keys())
        for sym, score in sorted_scores[:top_n]:
            if sym not in current_held:
                signals.append(Signal(
                    date=date,
                    symbol=sym,
                    action="BUY",
                    strength=1.0 / self.params["max_position"],
                    price=data[sym]["close"].iloc[-1],
                    reason=f"因子轮入 (综合得分={score:.3f}, 排名Top{top_n})",
                ))

        return signals

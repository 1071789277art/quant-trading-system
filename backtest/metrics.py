"""
绩效分析模块 - 计算各类投资绩效指标
"""
import numpy as np
import pandas as pd
from typing import Dict


def compute_metrics(
    equity_curve: pd.Series,
    trades: pd.DataFrame,
    risk_free_rate: float = 0.03,
    periods_per_year: int = 252,
) -> Dict:
    """
    计算完整的绩效指标

    参数:
        equity_curve: 净值曲线 Series（index=date, values=total_equity）
        trades: 交易记录 DataFrame（columns: date, symbol, direction, price, quantity, commission, pnl）
        risk_free_rate: 无风险利率（年化）
        periods_per_year: 每年交易日数

    返回:
        指标字典
    """
    if equity_curve.empty or len(equity_curve) < 2:
        return {}

    returns = equity_curve.pct_change().dropna()
    total_days = (equity_curve.index[-1] - equity_curve.index[0]).days
    years = max(total_days / 365.25, 0.01)

    # ===== 收益指标 =====
    total_return = (equity_curve.iloc[-1] / equity_curve.iloc[0]) - 1
    annual_return = (1 + total_return) ** (1 / years) - 1

    # ===== 风险指标 =====
    annual_volatility = returns.std() * np.sqrt(periods_per_year)

    # 最大回撤
    cummax = equity_curve.cummax()
    drawdown = (equity_curve - cummax) / cummax
    max_drawdown = drawdown.min()
    # 最大回撤持续时间
    dd_start = None
    max_dd_duration = 0
    current_duration = 0
    for i, dd in enumerate(drawdown):
        if dd < 0:
            if dd_start is None:
                dd_start = i
            current_duration = i - dd_start
        else:
            max_dd_duration = max(max_dd_duration, current_duration)
            dd_start = None
            current_duration = 0
    max_dd_duration = max(max_dd_duration, current_duration)

    # ===== 风险调整收益 =====
    excess_return = annual_return - risk_free_rate
    sharpe_ratio = excess_return / annual_volatility if annual_volatility > 0 else 0

    downside_returns = returns[returns < 0]
    downside_std = downside_returns.std() * np.sqrt(periods_per_year) if len(downside_returns) > 0 else 0.0001
    sortino_ratio = excess_return / downside_std

    calmar_ratio = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0

    # ===== 交易统计 =====
    if not trades.empty and "pnl" in trades.columns:
        winning_trades = trades[trades["pnl"] > 0]
        losing_trades = trades[trades["pnl"] < 0]
        total_trades = len(trades)
        win_rate = len(winning_trades) / total_trades if total_trades > 0 else 0
        avg_win = winning_trades["pnl"].mean() if len(winning_trades) > 0 else 0
        avg_loss = losing_trades["pnl"].mean() if len(losing_trades) > 0 else 0
        profit_factor = (
            winning_trades["pnl"].sum() / abs(losing_trades["pnl"].sum())
            if len(losing_trades) > 0 and losing_trades["pnl"].sum() != 0
            else 999.999
        )
        avg_holding_days = trades.get("holding_days", pd.Series([0])).mean()
    else:
        total_trades = 0
        win_rate = 0
        avg_win = 0
        avg_loss = 0
        profit_factor = 0
        avg_holding_days = 0

    return {
        "总收益率": f"{total_return:.2%}",
        "年化收益率": f"{annual_return:.2%}",
        "年化波动率": f"{annual_volatility:.2%}",
        "最大回撤": f"{max_drawdown:.2%}",
        "最大回撤持续(天)": max_dd_duration,
        "夏普比率": round(sharpe_ratio, 3),
        "索提诺比率": round(sortino_ratio, 3),
        "卡尔玛比率": round(calmar_ratio, 3),
        "总交易次数": total_trades,
        "胜率": f"{win_rate:.2%}",
        "平均盈利": round(avg_win, 2),
        "平均亏损": round(avg_loss, 2),
        "盈亏比": round(profit_factor, 3),
        "平均持仓天数": round(avg_holding_days, 1),
        "回测天数": total_days,
        "年化": years,
        # 原始数值（供绘图使用）
        "_total_return": total_return,
        "_annual_return": annual_return,
        "_max_drawdown": max_drawdown,
        "_sharpe": sharpe_ratio,
    }

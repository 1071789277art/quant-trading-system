"""
工具模块 - 生成示例数据用于离线测试
"""
import numpy as np
import pandas as pd
from datetime import datetime, timedelta


def generate_sample_data(
    symbol: str = "SAMPLE",
    start_date: str = "2022-01-01",
    end_date: str = "2024-12-31",
    initial_price: float = 150.0,
    volatility: float = 0.02,
    trend: float = 0.0003,
    seed: int = 42,
) -> pd.DataFrame:
    """
    生成模拟股票日线数据（几何布朗运动 + 均值回归 + 周期波动）

    用于离线测试和数据源不可用时的降级方案。
    """
    np.random.seed(seed)

    dates = pd.bdate_range(start=start_date, end=end_date)
    n = len(dates)

    # 几何布朗运动
    daily_returns = np.random.normal(trend, volatility, n)

    # 添加周期波动（模拟市场周期）
    cycle = 0.005 * np.sin(np.linspace(0, 4 * np.pi, n))
    daily_returns += cycle

    # 添加偶尔的大波动（模拟事件冲击）
    shock_indices = np.random.choice(n, size=max(1, n // 60), replace=False)
    daily_returns[shock_indices] += np.random.choice([-0.03, 0.03, -0.05, 0.04], size=len(shock_indices))

    # 计算价格序列
    prices = initial_price * np.cumprod(1 + daily_returns)

    # 生成 OHLCV
    high = prices * (1 + np.abs(np.random.normal(0, 0.01, n)))
    low = prices * (1 - np.abs(np.random.normal(0, 0.01, n)))
    open_prices = prices * (1 + np.random.normal(0, 0.005, n))

    # 成交量（价格波动大时成交量也大）
    base_volume = 1_000_000
    volume = base_volume * (1 + 3 * np.abs(daily_returns) / volatility) * np.random.uniform(0.7, 1.3, n)

    df = pd.DataFrame({
        "open": np.round(open_prices, 2),
        "high": np.round(high, 2),
        "low": np.round(low, 2),
        "close": np.round(prices, 2),
        "volume": np.round(volume).astype(int),
        "amount": np.round(prices * volume, 2),
        "turnover": np.round(np.random.uniform(0.5, 3.0, n), 2),
    }, index=dates)

    df.index.name = "date"

    # 确保 high >= max(open, close) 且 low <= min(open, close)
    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)

    return df


def generate_multi_symbol_data(
    symbols: list,
    start_date: str = "2022-01-01",
    end_date: str = "2024-12-31",
) -> dict:
    """为多只股票生成模拟数据"""
    configs = {
        "AAPL": {"initial_price": 150, "volatility": 0.018, "trend": 0.0004, "seed": 42},
        "MSFT": {"initial_price": 250, "volatility": 0.016, "trend": 0.0003, "seed": 43},
        "GOOGL": {"initial_price": 90, "volatility": 0.022, "trend": 0.0002, "seed": 44},
        "AMZN": {"initial_price": 100, "volatility": 0.025, "trend": 0.0005, "seed": 45},
        "NVDA": {"initial_price": 200, "volatility": 0.035, "trend": 0.0008, "seed": 46},
        "TSLA": {"initial_price": 120, "volatility": 0.04, "trend": 0.0001, "seed": 47},
        "000001": {"initial_price": 12, "volatility": 0.02, "trend": 0.0001, "seed": 48},
        "600519": {"initial_price": 1700, "volatility": 0.015, "trend": 0.0002, "seed": 49},
        "000858": {"initial_price": 150, "volatility": 0.02, "trend": 0.0001, "seed": 50},
    }

    data = {}
    for i, sym in enumerate(symbols):
        cfg = configs.get(sym, {"initial_price": 100, "volatility": 0.02, "trend": 0.0002, "seed": 50 + i})
        data[sym] = generate_sample_data(sym, start_date, end_date, **cfg)

    return data

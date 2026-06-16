#!/usr/bin/env python3
"""
系统验证测试 - 使用模拟数据验证所有策略和回测引擎
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import logging
logging.basicConfig(level=logging.WARNING)

from utils.sample_data import generate_sample_data, generate_multi_symbol_data
from backtest.engine import BacktestEngine
from strategies.ma_crossover import MACrossover
from strategies.macd_strategy import MACDStrategy
from strategies.rsi_strategy import RSIStrategy
from strategies.bollinger_strategy import BollingerStrategy
from strategies.momentum import MomentumStrategy
from strategies.multi_factor import MultiFactorStrategy


def test_strategy(name, strategy, symbols, data):
    """测试单个策略"""
    try:
        engine = BacktestEngine(
            strategy=strategy,
            symbols=symbols,
            market="US",
            initial_capital=1_000_000,
        )
        result = engine.run(
            start_date="2022-01-01",
            end_date="2024-12-31",
            data=data,
        )
        metrics = result["metrics"]
        trades_count = len(result["trades"])
        total_return = metrics.get("总收益率", "N/A")
        sharpe = metrics.get("夏普比率", "N/A")
        max_dd = metrics.get("最大回撤", "N/A")
        win_rate = metrics.get("胜率", "N/A")

        print(f"  ✅ {name:<12s} | 收益:{total_return:>8s} | 夏普:{str(sharpe):>6s} | "
              f"回撤:{max_dd:>8s} | 胜率:{win_rate:>6s} | 交易:{trades_count}笔")
        return True

    except Exception as e:
        print(f"  ❌ {name:<12s} | 错误: {e}")
        return False


def main():
    print("=" * 75)
    print("  稳赢量化交易系统 - 验证测试")
    print("=" * 75)

    # 生成模拟数据
    print("\n📦 生成模拟数据...")
    single_data = {"AAPL": generate_sample_data("AAPL", "2022-01-01", "2024-12-31")}
    multi_data = generate_multi_symbol_data(
        ["AAPL", "MSFT", "GOOGL", "NVDA", "AMZN"],
        "2022-01-01", "2024-12-31"
    )
    print(f"  AAPL: {len(single_data['AAPL'])} 个交易日")
    print(f"  多标的: {len(multi_data)} 只股票, 各 {len(list(multi_data.values())[0])} 个交易日")

    # 测试各策略
    print("\n📊 策略回测测试")
    print("-" * 75)

    results = []

    # 单标的策略
    results.append(test_strategy("双均线交叉", MACrossover(), ["AAPL"], single_data))
    results.append(test_strategy("MACD策略", MACDStrategy(), ["AAPL"], single_data))
    results.append(test_strategy("RSI均值回归", RSIStrategy(), ["AAPL"], single_data))
    results.append(test_strategy("布林带突破", BollingerStrategy(), ["AAPL"], single_data))

    # 多标的策略
    results.append(test_strategy("动量轮动", MomentumStrategy(), list(multi_data.keys()), multi_data))
    results.append(test_strategy("多因子选股", MultiFactorStrategy(), list(multi_data.keys()), multi_data))

    print("-" * 75)

    passed = sum(results)
    total = len(results)

    if passed == total:
        print(f"\n🎉 全部通过 ({passed}/{total})")
        print("  系统核心功能验证成功！可通过 python main.py 启动Web仪表盘。")
    else:
        print(f"\n⚠️ 部分失败 ({passed}/{total})")

    # 测试绩效指标完整性
    print("\n📋 绩效指标完整性检查")
    engine = BacktestEngine(MACrossover(), ["AAPL"], "US", 1_000_000)
    result = engine.run("2022-01-01", "2024-12-31", data=single_data)
    expected_keys = ["总收益率", "年化收益率", "年化波动率", "最大回撤", "夏普比率",
                     "索提诺比率", "卡尔玛比率", "总交易次数", "胜率", "盈亏比"]
    missing = [k for k in expected_keys if k not in result["metrics"]]
    if missing:
        print(f"  ⚠️ 缺失指标: {missing}")
    else:
        print(f"  ✅ 全部 {len(expected_keys)} 项指标完整")

    # 测试Web仪表盘导入
    print("\n🌐 Web仪表盘模块检查")
    try:
        from dashboard.app import create_app
        app = create_app()
        print(f"  ✅ Flask应用创建成功 (端口: 8050)")
    except Exception as e:
        print(f"  ❌ Flask应用创建失败: {e}")

    print("\n" + "=" * 75)


if __name__ == "__main__":
    main()

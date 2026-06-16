#!/usr/bin/env python3
"""
稳赢量化交易系统 - 主入口

支持两种运行模式:
  1. Web仪表盘模式（默认）: python main.py
  2. 命令行回测模式: python main.py --cli --strategy 双均线交叉 --symbols AAPL --market US
"""
import argparse
import logging
import os
import sys

# 将项目根目录加入 path
sys.path.insert(0, os.path.dirname(__file__))

import config
from data.fetcher import DataFetcher
from backtest.engine import BacktestEngine
from strategies.ma_crossover import MACrossover
from strategies.macd_strategy import MACDStrategy
from strategies.rsi_strategy import RSIStrategy
from strategies.bollinger_strategy import BollingerStrategy
from strategies.momentum import MomentumStrategy
from strategies.multi_factor import MultiFactorStrategy

# 配置日志
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format=config.LOG_FORMAT,
)
logger = logging.getLogger("稳赢")

STRATEGY_MAP = {
    "双均线交叉": MACrossover,
    "MACD策略": MACDStrategy,
    "RSI均值回归": RSIStrategy,
    "布林带突破": BollingerStrategy,
    "动量轮动": MomentumStrategy,
    "多因子选股": MultiFactorStrategy,
}


def run_cli(args):
    """命令行模式运行回测"""
    strategy_name = args.strategy
    if strategy_name not in STRATEGY_MAP:
        print(f"未知策略: {strategy_name}")
        print(f"可用策略: {', '.join(STRATEGY_MAP.keys())}")
        sys.exit(1)

    symbols = [s.strip() for s in args.symbols.split(",")]
    strategy = STRATEGY_MAP[strategy_name]()

    logger.info(f"策略: {strategy_name}")
    logger.info(f"标的: {symbols}")
    logger.info(f"市场: {args.market}")
    logger.info(f"区间: {args.start} ~ {args.end}")
    logger.info(f"初始资金: {args.capital:,.0f}")
    print("=" * 60)

    engine = BacktestEngine(
        strategy=strategy,
        symbols=symbols,
        market=args.market,
        initial_capital=args.capital,
    )

    result = engine.run(start_date=args.start, end_date=args.end)

    # 打印结果
    print("\n📊 回测结果")
    print("=" * 60)
    for key, value in result["metrics"].items():
        if not key.startswith("_"):
            print(f"  {key: <15s}: {value}")
    print("=" * 60)

    # 打印交易记录摘要
    trades = result["trades"]
    if not trades.empty:
        print(f"\n📋 交易记录（共 {len(trades)} 笔）")
        print("-" * 60)
        for _, row in trades.head(10).iterrows():
            direction = "🟢买入" if row["direction"] == "BUY" else "🔴卖出"
            date_str = row["date"].strftime("%Y-%m-%d") if hasattr(row["date"], "strftime") else str(row["date"])
            print(f"  {date_str} | {row['symbol']:>6s} | {direction} | "
                  f"价格:{row['price']:>10.2f} | 数量:{row['quantity']:>6d} | "
                  f"盈亏:{row.get('pnl', 0):>10.2f}")
        if len(trades) > 10:
            print(f"  ... 还有 {len(trades) - 10} 笔交易")

    print("\n✅ 回测完成")
    return result


def run_web():
    """启动Web仪表盘"""
    from dashboard.app import create_app
    app = create_app()
    print("=" * 60)
    print("  稳赢量化交易系统 - Web仪表盘")
    print(f"  访问地址: http://localhost:{config.DASHBOARD_PORT}")
    print("  按 Ctrl+C 停止服务")
    print("=" * 60)
    app.run(
        host=config.DASHBOARD_HOST,
        port=config.DASHBOARD_PORT,
        debug=config.DASHBOARD_DEBUG,
    )


def main():
    parser = argparse.ArgumentParser(
        description="稳赢量化交易系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 启动Web仪表盘
  python main.py

  # 命令行回测 - 美股AAPL双均线策略
  python main.py --cli --strategy 双均线交叉 --symbols AAPL --market US --start 2022-01-01

  # 命令行回测 - A股多只股票
  python main.py --cli --strategy 动量轮动 --symbols 000001,600519,000858 --market A_SHARE

可用策略:
  双均线交叉, MACD策略, RSI均值回归, 布林带突破, 动量轮动, 多因子选股
        """
    )
    parser.add_argument("--cli", action="store_true", help="使用命令行模式（默认启动Web仪表盘）")
    parser.add_argument("--strategy", type=str, default="双均线交叉", help="策略名称")
    parser.add_argument("--symbols", type=str, default="AAPL", help="股票代码，逗号分隔")
    parser.add_argument("--market", type=str, default="US", choices=["US", "A_SHARE"], help="市场类型")
    parser.add_argument("--start", type=str, default="2022-01-01", help="开始日期")
    parser.add_argument("--end", type=str, default="2024-12-31", help="结束日期")
    parser.add_argument("--capital", type=float, default=config.DEFAULT_CAPITAL, help="初始资金")

    args = parser.parse_args()

    if args.cli:
        run_cli(args)
    else:
        run_web()


if __name__ == "__main__":
    main()

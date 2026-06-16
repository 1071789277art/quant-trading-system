"""
回测引擎 - 事件驱动式回测框架
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional, Type

import numpy as np
import pandas as pd

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config

logger = logging.getLogger(__name__)


# ======================================================================
# 数据结构
# ======================================================================
@dataclass
class Signal:
    """策略信号"""
    date: datetime
    symbol: str
    action: str          # 'BUY', 'SELL', 'HOLD'
    strength: float = 1.0  # 信号强度 0~1，用于仓位管理
    price: float = 0.0
    reason: str = ""


@dataclass
class Position:
    """持仓"""
    symbol: str
    quantity: int
    avg_cost: float
    current_price: float = 0.0
    entry_date: Optional[datetime] = None

    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        return self.quantity * (self.current_price - self.avg_cost)

    @property
    def pnl_pct(self) -> float:
        if self.avg_cost == 0:
            return 0.0
        return (self.current_price - self.avg_cost) / self.avg_cost


@dataclass
class TradeRecord:
    """交易记录"""
    date: datetime
    symbol: str
    direction: str       # 'BUY' or 'SELL'
    price: float
    quantity: int
    amount: float
    commission: float
    stamp_tax: float = 0.0
    reason: str = ""


@dataclass
class Portfolio:
    """投资组合"""
    cash: float
    positions: Dict[str, Position] = field(default_factory=dict)
    trades: List[TradeRecord] = field(default_factory=list)

    @property
    def position_value(self) -> float:
        return sum(p.market_value for p in self.positions.values())

    @property
    def total_equity(self) -> float:
        return self.cash + self.position_value

    def position_pct(self, symbol: str) -> float:
        if symbol not in self.positions or self.total_equity == 0:
            return 0.0
        return self.positions[symbol].market_value / self.total_equity

    def total_position_pct(self) -> float:
        if self.total_equity == 0:
            return 0.0
        return self.position_value / self.total_equity


# ======================================================================
# 策略基类
# ======================================================================
class BaseStrategy:
    """策略基类，所有策略需继承此类"""

    name: str = "BaseStrategy"

    def __init__(self, params: dict = None):
        self.params = params or {}

    def on_init(self, data: Dict[str, pd.DataFrame]):
        """策略初始化，可用于预计算指标"""
        pass

    def on_bar(self, date: datetime, data: Dict[str, pd.DataFrame], portfolio: Portfolio) -> List[Signal]:
        """
        每根K线触发，返回信号列表
        data: {symbol: DataFrame} 其中 DataFrame 包含截至当日的所有历史数据
        """
        raise NotImplementedError

    def on_trade(self, trade: TradeRecord):
        """交易执行后回调"""
        pass


# ======================================================================
# 回测引擎
# ======================================================================
class BacktestEngine:
    """
    回测引擎

    使用方式:
        engine = BacktestEngine(strategy, symbols, market='US')
        result = engine.run(start_date='2020-01-01', end_date='2024-01-01')
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        symbols: List[str],
        market: str = "US",
        initial_capital: float = None,
        commission_rate: float = None,
        slippage: float = None,
        stamp_tax_rate: float = None,
        lot_size: int = 1,
    ):
        self.strategy = strategy
        self.symbols = symbols
        self.market = market
        self.initial_capital = initial_capital or config.DEFAULT_CAPITAL
        self.commission_rate = commission_rate or config.DEFAULT_COMMISSION
        self.slippage = slippage or config.DEFAULT_SLIPPAGE
        self.stamp_tax_rate = stamp_tax_rate or (
            config.DEFAULT_STAMP_TAX if market == "A_SHARE" else 0
        )
        self.lot_size = lot_size if market == "A_SHARE" else 1
        self.min_commission = config.DEFAULT_MIN_COMMISSION

    def run(
        self,
        start_date: str,
        end_date: str,
        data: Dict[str, pd.DataFrame] = None,
    ) -> dict:
        """
        运行回测

        参数:
            start_date: 开始日期
            end_date: 结束日期
            data: 预获取的数据字典 {symbol: DataFrame}，为None则自动获取

        返回:
            {
                'equity_curve': pd.Series,
                'trades': pd.DataFrame,
                'metrics': dict,
                'daily_positions': list,
                'signals': list,
            }
        """
        # 获取数据
        if data is None:
            from data.fetcher import DataFetcher
            fetcher = DataFetcher(cache_dir=config.DATA_DIR)
            data = {}
            for sym in self.symbols:
                df = fetcher.get_daily(sym, start_date, end_date, self.market)
                if not df.empty:
                    data[sym] = df
                else:
                    logger.warning(f"跳过无数据的标的: {sym}")

        if not data:
            raise ValueError("无可用数据，回测终止")

        # 更新 symbols 为实际有数据的标的
        self.symbols = list(data.keys())

        # 构建统一的日期索引
        all_dates = sorted(set().union(*(df.index for df in data.values())))
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        trading_dates = [d for d in all_dates if start <= d <= end]

        if not trading_dates:
            raise ValueError("所选日期范围内无交易日数据")

        # 初始化组合
        portfolio = Portfolio(cash=self.initial_capital)
        equity_curve = {}
        all_signals = []

        # 策略初始化
        self.strategy.on_init(data)

        # 主回测循环
        for date in trading_dates:
            # 更新当前价格
            for sym in list(portfolio.positions.keys()):
                if sym in data and date in data[sym].index:
                    portfolio.positions[sym].current_price = data[sym].loc[date, "close"]

            # 风控检查（止损/止盈）
            self._risk_check(date, portfolio, all_signals)

            # 生成策略信号
            sliced_data = {}
            for sym, df in data.items():
                sliced = df[df.index <= date]
                if not sliced.empty:
                    sliced_data[sym] = sliced

            try:
                signals = self.strategy.on_bar(date, sliced_data, portfolio)
            except Exception as e:
                logger.error(f"策略 on_bar 错误 ({date}): {e}")
                signals = []

            # 执行信号
            if signals:
                for sig in signals:
                    if sig.action in ("BUY", "SELL") and sig.symbol in data:
                        trade = self._execute_signal(sig, portfolio, data)
                        if trade:
                            portfolio.trades.append(trade)
                            self.strategy.on_trade(trade)

                all_signals.extend(signals)

            # 记录每日净值
            equity_curve[date] = portfolio.total_equity

        # 构建结果
        eq_series = pd.Series(equity_curve, name="equity")
        trades_df = self._trades_to_df(portfolio.trades)

        from backtest.metrics import compute_metrics
        metrics = compute_metrics(eq_series, trades_df)

        return {
            "equity_curve": eq_series,
            "trades": trades_df,
            "metrics": metrics,
            "portfolio": portfolio,
            "signals": all_signals,
            "data": data,
        }

    # ------------------------------------------------------------------
    # 信号执行
    # ------------------------------------------------------------------
    def _execute_signal(
        self, signal: Signal, portfolio: Portfolio, data: Dict[str, pd.DataFrame]
    ) -> Optional[TradeRecord]:
        """执行交易信号，返回交易记录"""
        sym = signal.symbol
        if sym not in data:
            return None

        df = data[sym]
        if signal.date not in df.index:
            return None

        bar = df.loc[signal.date]
        exec_price = signal.price if signal.price > 0 else bar["open"]

        if signal.action == "BUY":
            return self._execute_buy(signal, exec_price, portfolio)
        elif signal.action == "SELL":
            return self._execute_sell(signal, exec_price, portfolio)

        return None

    def _execute_buy(
        self, signal: Signal, price: float, portfolio: Portfolio
    ) -> Optional[TradeRecord]:
        """执行买入"""
        sym = signal.symbol
        # 考虑滑点
        exec_price = price * (1 + self.slippage)

        # 计算可买数量
        available_cash = portfolio.cash
        # 限制单只股票仓位
        max_amount = portfolio.total_equity * config.MAX_POSITION_PCT
        buy_amount = min(available_cash, max_amount * signal.strength)

        if buy_amount < exec_price * self.lot_size:
            return None

        quantity = int(buy_amount / exec_price / self.lot_size) * self.lot_size
        if quantity <= 0:
            return None

        amount = quantity * exec_price
        commission = max(amount * self.commission_rate, self.min_commission)
        total_cost = amount + commission

        if total_cost > portfolio.cash:
            quantity -= self.lot_size
            if quantity <= 0:
                return None
            amount = quantity * exec_price
            commission = max(amount * self.commission_rate, self.min_commission)
            total_cost = amount + commission

        # 更新持仓
        portfolio.cash -= total_cost
        if sym in portfolio.positions:
            pos = portfolio.positions[sym]
            total_qty = pos.quantity + quantity
            pos.avg_cost = (pos.avg_cost * pos.quantity + exec_price * quantity) / total_qty
            pos.quantity = total_qty
            pos.current_price = exec_price
        else:
            portfolio.positions[sym] = Position(
                symbol=sym,
                quantity=quantity,
                avg_cost=exec_price,
                current_price=exec_price,
                entry_date=signal.date,
            )

        return TradeRecord(
            date=signal.date,
            symbol=sym,
            direction="BUY",
            price=exec_price,
            quantity=quantity,
            amount=amount,
            commission=commission,
            reason=signal.reason,
        )

    def _execute_sell(
        self, signal: Signal, price: float, portfolio: Portfolio
    ) -> Optional[TradeRecord]:
        """执行卖出"""
        sym = signal.symbol
        if sym not in portfolio.positions or portfolio.positions[sym].quantity <= 0:
            return None

        exec_price = price * (1 - self.slippage)
        pos = portfolio.positions[sym]
        quantity = int(pos.quantity * signal.strength / self.lot_size) * self.lot_size
        quantity = max(quantity, self.lot_size)
        quantity = min(quantity, pos.quantity)

        amount = quantity * exec_price
        commission = max(amount * self.commission_rate, self.min_commission)
        stamp_tax = amount * self.stamp_tax_rate
        net_proceeds = amount - commission - stamp_tax

        # 更新持仓
        portfolio.cash += net_proceeds
        pos.quantity -= quantity
        if pos.quantity <= 0:
            del portfolio.positions[sym]

        return TradeRecord(
            date=signal.date,
            symbol=sym,
            direction="SELL",
            price=exec_price,
            quantity=quantity,
            amount=amount,
            commission=commission,
            stamp_tax=stamp_tax,
            reason=signal.reason,
        )

    # ------------------------------------------------------------------
    # 风控
    # ------------------------------------------------------------------
    def _risk_check(self, date: datetime, portfolio: Portfolio, signals: list):
        """止损止盈检查"""
        for sym, pos in list(portfolio.positions.items()):
            pnl_pct = pos.pnl_pct

            if pnl_pct <= -config.STOP_LOSS_PCT:
                sig = Signal(
                    date=date,
                    symbol=sym,
                    action="SELL",
                    strength=1.0,
                    price=pos.current_price,
                    reason=f"止损触发 ({pnl_pct:.2%})",
                )
                signals.append(sig)
                trade = self._execute_sell(sig, pos.current_price, portfolio)
                if trade:
                    portfolio.trades.append(trade)

            elif pnl_pct >= config.TAKE_PROFIT_PCT:
                sig = Signal(
                    date=date,
                    symbol=sym,
                    action="SELL",
                    strength=0.5,  # 止盈卖出一半
                    price=pos.current_price,
                    reason=f"止盈触发 ({pnl_pct:.2%})",
                )
                signals.append(sig)
                trade = self._execute_sell(sig, pos.current_price, portfolio)
                if trade:
                    portfolio.trades.append(trade)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def _trades_to_df(self, trades: List[TradeRecord]) -> pd.DataFrame:
        """交易记录转 DataFrame"""
        if not trades:
            return pd.DataFrame(columns=["date", "symbol", "direction", "price", "quantity",
                                         "amount", "commission", "stamp_tax", "pnl", "reason"])

        records = []
        # 计算每笔交易的盈亏
        buy_costs = {}  # symbol -> list of (price, qty)

        for t in trades:
            pnl = 0.0
            if t.direction == "SELL":
                if t.symbol in buy_costs and buy_costs[t.symbol]:
                    # 简单FIFO计算
                    avg_buy = sum(p * q for p, q in buy_costs[t.symbol]) / sum(
                        q for _, q in buy_costs[t.symbol]
                    )
                    pnl = (t.price - avg_buy) * t.quantity - t.commission - t.stamp_tax

                    # 更新买入记录
                    remaining = t.quantity
                    new_buys = []
                    for bp, bq in buy_costs[t.symbol]:
                        if remaining <= 0:
                            new_buys.append((bp, bq))
                        elif remaining >= bq:
                            remaining -= bq
                        else:
                            new_buys.append((bp, bq - remaining))
                            remaining = 0
                    buy_costs[t.symbol] = new_buys

            elif t.direction == "BUY":
                if t.symbol not in buy_costs:
                    buy_costs[t.symbol] = []
                buy_costs[t.symbol].append((t.price, t.quantity))

            records.append({
                "date": t.date,
                "symbol": t.symbol,
                "direction": t.direction,
                "price": round(t.price, 4),
                "quantity": t.quantity,
                "amount": round(t.amount, 2),
                "commission": round(t.commission, 2),
                "stamp_tax": round(t.stamp_tax, 2),
                "pnl": round(pnl, 2),
                "reason": t.reason,
            })

        return pd.DataFrame(records)

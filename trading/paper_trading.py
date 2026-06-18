"""
模拟交易模块 - 自动买卖执行与模拟盘管理

核心功能:
- AutoTrader: 自动交易运行器，逐根K线自动买入/卖出
- PaperTradingSession: 实时定时扫描 + 自动执行
- 完整交易日志、风控、状态持久化
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable

import pandas as pd

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from backtest.engine import BacktestEngine, BaseStrategy, Portfolio, Signal, Position, TradeRecord
from data.fetcher import DataFetcher

logger = logging.getLogger(__name__)


# ======================================================================
# AutoTrader - 自动交易运行器
# ======================================================================
class AutoTrader:
    """
    自动交易运行器

    逐根K线遍历历史数据，自动执行策略信号（买入/卖出），
    完整记录每笔交易并生成交易日志。适合快速跑数据验证。
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        symbols: List[str],
        market: str = "US",
        initial_capital: float = None,
        on_trade: Optional[Callable] = None,
        speed_ms: int = 0,
    ):
        self.strategy = strategy
        self.symbols = symbols
        self.market = market
        self.initial_capital = initial_capital or config.DEFAULT_CAPITAL
        self.on_trade_callback = on_trade
        self.speed_ms = speed_ms  # 每根K线间隔（毫秒），0=最快

        # 状态
        self.portfolio = Portfolio(cash=self.initial_capital)
        self.trade_log: List[dict] = []
        self.equity_curve: List[dict] = []
        self.signals_log: List[dict] = []
        self.current_bar_index = 0
        self.total_bars = 0
        self.is_running = False
        self.is_finished = False
        self._thread: Optional[threading.Thread] = None

        # 数据
        self._data: Dict[str, pd.DataFrame] = {}
        self._trading_dates: List = []

    def prepare_data(self, start_date: str, end_date: str, data: Dict[str, pd.DataFrame] = None):
        """预加载数据"""
        if data:
            self._data = data
        else:
            fetcher = DataFetcher(cache_dir=config.DATA_DIR)
            for sym in self.symbols:
                df = fetcher.get_daily(sym, start_date, end_date, self.market)
                if not df.empty:
                    self._data[sym] = df

        if not self._data:
            raise ValueError("无可用数据")

        self.symbols = list(self._data.keys())
        all_dates = sorted(set().union(*(df.index for df in self._data.values())))
        self._trading_dates = [d for d in all_dates
                               if pd.Timestamp(start_date) <= d <= pd.Timestamp(end_date)]
        self.total_bars = len(self._trading_dates)

        logger.info(f"AutoTrader 数据就绪: {len(self._data)}只标的, {self.total_bars}个交易日")

    def start(self):
        """启动自动交易"""
        if self.is_running:
            return
        self.is_running = True
        self.is_finished = False
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("AutoTrader 启动")

    def stop(self):
        """停止自动交易"""
        self.is_running = False
        logger.info("AutoTrader 停止")

    def _run_loop(self):
        """主循环：逐根K线执行"""
        self.strategy.on_init(self._data)

        while self.is_running and self.current_bar_index < self.total_bars:
            date = self._trading_dates[self.current_bar_index]
            try:
                self._process_bar(date)
            except Exception as e:
                logger.error(f"AutoTrader 执行错误 ({date}): {e}")
                self.trade_log.append({
                    "time": datetime.now().isoformat(),
                    "bar_date": str(date),
                    "type": "ERROR",
                    "message": str(e),
                })

            self.current_bar_index += 1

            if self.speed_ms > 0:
                time.sleep(self.speed_ms / 1000.0)

        self.is_running = False
        self.is_finished = True
        logger.info(f"AutoTrader 完成: {self.current_bar_index}/{self.total_bars} 根K线")

    def _process_bar(self, date):
        """处理单根K线"""
        # 构建截至当日的切片数据
        sliced_data = {}
        for sym, df in self._data.items():
            sliced = df[df.index <= date]
            if not sliced.empty:
                sliced_data[sym] = sliced

        # 更新当前价格
        for sym in list(self.portfolio.positions.keys()):
            if sym in sliced_data:
                self.portfolio.positions[sym].current_price = sliced_data[sym]["close"].iloc[-1]

        # === 风控检查 ===
        self._risk_check(date)

        # === 策略信号 ===
        try:
            signals = self.strategy.on_bar(date, sliced_data, self.portfolio)
        except Exception as e:
            signals = []

        # === 自动执行买卖 ===
        engine = BacktestEngine(self.strategy, self.symbols, self.market, self.initial_capital)

        if signals:
            for sig in signals:
                self.signals_log.append({
                    "bar_date": str(date),
                    "symbol": sig.symbol,
                    "action": sig.action,
                    "price": sig.price,
                    "reason": sig.reason,
                })

                if sig.action in ("BUY", "SELL") and sig.symbol in sliced_data:
                    trade = engine._execute_signal(sig, self.portfolio, sliced_data)
                    if trade:
                        self.portfolio.trades.append(trade)
                        pnl = self._calc_trade_pnl(trade)
                        log_entry = {
                            "time": datetime.now().isoformat(),
                            "bar_date": str(date),
                            "type": trade.direction,
                            "symbol": trade.symbol,
                            "price": round(trade.price, 2),
                            "quantity": trade.quantity,
                            "amount": round(trade.amount, 2),
                            "commission": round(trade.commission, 2),
                            "pnl": round(pnl, 2),
                            "reason": trade.reason,
                            "cash_after": round(self.portfolio.cash, 2),
                            "equity_after": round(self.portfolio.total_equity, 2),
                        }
                        self.trade_log.append(log_entry)

                        if self.on_trade_callback:
                            self.on_trade_callback(log_entry)

        # 记录每日净值
        self.equity_curve.append({
            "bar_date": str(date),
            "bar_index": self.current_bar_index,
            "cash": round(self.portfolio.cash, 2),
            "position_value": round(self.portfolio.position_value, 2),
            "total_equity": round(self.portfolio.total_equity, 2),
        })

    def _risk_check(self, date):
        """止损止盈自动检查"""
        for sym, pos in list(self.portfolio.positions.items()):
            pnl_pct = pos.pnl_pct

            if pnl_pct <= -config.STOP_LOSS_PCT:
                self._force_sell(date, sym, pos.current_price, 1.0,
                                 f"止损触发 ({pnl_pct:.2%})")
            elif pnl_pct >= config.TAKE_PROFIT_PCT:
                self._force_sell(date, sym, pos.current_price, 0.5,
                                 f"止盈触发 ({pnl_pct:.2%})")

    def _force_sell(self, date, sym, price, strength, reason):
        """强制卖出"""
        sig = Signal(date=date, symbol=sym, action="SELL",
                     strength=strength, price=price, reason=reason)
        engine = BacktestEngine(self.strategy, self.symbols, self.market, self.initial_capital)
        sliced = {sym: self._data[sym][self._data[sym].index <= date]}
        trade = engine._execute_signal(sig, self.portfolio, sliced)
        if trade:
            self.portfolio.trades.append(trade)
            pnl = self._calc_trade_pnl(trade)
            self.trade_log.append({
                "time": datetime.now().isoformat(),
                "bar_date": str(date),
                "type": "SELL",
                "symbol": trade.symbol,
                "price": round(trade.price, 2),
                "quantity": trade.quantity,
                "amount": round(trade.amount, 2),
                "commission": round(trade.commission, 2),
                "pnl": round(pnl, 2),
                "reason": reason,
                "cash_after": round(self.portfolio.cash, 2),
                "equity_after": round(self.portfolio.total_equity, 2),
            })

    def _calc_trade_pnl(self, trade: TradeRecord) -> float:
        """计算单笔交易盈亏"""
        if trade.direction != "SELL":
            return 0.0
        buy_trades = [t for t in self.portfolio.trades
                      if t.symbol == trade.symbol and t.direction == "BUY" and t != trade]
        if not buy_trades:
            return 0.0
        avg_buy = sum(t.price * t.quantity for t in buy_trades) / sum(t.quantity for t in buy_trades)
        return (trade.price - avg_buy) * trade.quantity - trade.commission - trade.stamp_tax

    def get_status(self) -> dict:
        """获取自动交易状态"""
        positions = []
        for sym, pos in self.portfolio.positions.items():
            positions.append({
                "symbol": sym,
                "quantity": pos.quantity,
                "avg_cost": round(pos.avg_cost, 4),
                "current_price": round(pos.current_price, 4),
                "market_value": round(pos.market_value, 2),
                "unrealized_pnl": round(pos.unrealized_pnl, 2),
                "pnl_pct": f"{pos.pnl_pct:.2%}",
            })

        progress = (self.current_bar_index / self.total_bars * 100) if self.total_bars > 0 else 0

        return {
            "strategy": self.strategy.name,
            "market": self.market,
            "is_running": self.is_running,
            "is_finished": self.is_finished,
            "progress": round(progress, 1),
            "current_bar": self.current_bar_index,
            "total_bars": self.total_bars,
            "cash": round(self.portfolio.cash, 2),
            "position_value": round(self.portfolio.position_value, 2),
            "total_equity": round(self.portfolio.total_equity, 2),
            "total_return": f"{(self.portfolio.total_equity / self.initial_capital - 1):.2%}",
            "total_trades": len(self.portfolio.trades),
            "positions": positions,
            "trade_log": self.trade_log[-50:],
            "equity_curve": self.equity_curve[-100:],
            "symbols": self.symbols,
        }


# ======================================================================
# PaperTradingSession - 实时模拟交易
# ======================================================================
class PaperTradingSession:
    """模拟交易会话 - 定时扫描行情并自动执行买卖"""

    def __init__(
        self,
        strategy: BaseStrategy,
        symbols: List[str],
        market: str = "US",
        initial_capital: float = None,
        tick_interval: int = 10,
        user_id: int = None,
    ):
        self.strategy = strategy
        self.symbols = symbols
        self.market = market
        self.initial_capital = initial_capital or config.DEFAULT_CAPITAL
        self.tick_interval = tick_interval  # 轮询间隔（秒）
        self.user_id = user_id
        self.fetcher = DataFetcher(cache_dir=config.DATA_DIR)

        # 状态
        self.portfolio = Portfolio(cash=self.initial_capital)
        self.signals_history: List[dict] = []
        self.equity_history: List[dict] = []
        self.trade_log: List[dict] = []
        self.is_running = False
        self._thread: Optional[threading.Thread] = None

        # 持久化路径 (JSON fallback)
        self._state_dir = os.path.join(os.path.dirname(__file__), "..", "data", "paper_state")
        os.makedirs(self._state_dir, exist_ok=True)
        self._state_file = os.path.join(self._state_dir, f"session_{strategy.name}.json")

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info(f"模拟交易启动: {self.strategy.name} (间隔{self.tick_interval}s)")

    def stop(self):
        self.is_running = False
        logger.info(f"模拟交易停止: {self.strategy.name}")

    def force_tick(self):
        """手动触发一次行情扫描 + 自动执行"""
        return self._tick()

    def _run_loop(self):
        while self.is_running:
            try:
                self._tick()
            except Exception as e:
                logger.error(f"模拟交易执行错误: {e}")
            time.sleep(self.tick_interval)

    def _tick(self) -> dict:
        """单次执行：获取数据 → 生成信号 → 自动买卖"""
        today = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=240)).strftime("%Y-%m-%d")

        data = {}
        for sym in self.symbols:
            df = self.fetcher.get_daily(sym, start, today, self.market)
            if not df.empty:
                data[sym] = df

        if not data:
            return {"error": "无可用数据"}

        now = datetime.now()

        # 更新价格
        for sym in list(self.portfolio.positions.keys()):
            if sym in data:
                self.portfolio.positions[sym].current_price = data[sym]["close"].iloc[-1]

        # 风控
        self._risk_check(now, data)

        # 策略信号
        try:
            signals = self.strategy.on_bar(now, data, self.portfolio)
        except Exception as e:
            logger.error(f"策略执行错误: {e}")
            signals = []

        # 记录信号
        for sig in signals:
            self.signals_history.append({
                "time": now.isoformat(),
                "symbol": sig.symbol,
                "action": sig.action,
                "price": sig.price,
                "strength": sig.strength,
                "reason": sig.reason,
            })

        # === 自动执行买卖 ===
        engine = BacktestEngine(self.strategy, self.symbols, self.market, self.initial_capital)
        executed_trades = []
        for sig in signals:
            if sig.action in ("BUY", "SELL"):
                trade = engine._execute_signal(sig, self.portfolio, data)
                if trade:
                    self.portfolio.trades.append(trade)
                    trade_entry = {
                        "time": now.isoformat(),
                        "type": trade.direction,
                        "symbol": trade.symbol,
                        "price": round(trade.price, 2),
                        "quantity": trade.quantity,
                        "amount": round(trade.amount, 2),
                        "commission": round(trade.commission, 2),
                        "reason": trade.reason,
                        "cash_after": round(self.portfolio.cash, 2),
                        "equity_after": round(self.portfolio.total_equity, 2),
                    }
                    self.trade_log.append(trade_entry)
                    executed_trades.append(trade_entry)

        # 记录净值
        self.equity_history.append({
            "time": now.isoformat(),
            "cash": round(self.portfolio.cash, 2),
            "position_value": round(self.portfolio.position_value, 2),
            "total_equity": round(self.portfolio.total_equity, 2),
        })

        self._save_state()

        return {
            "tick_time": now.isoformat(),
            "signals_generated": len(signals),
            "trades_executed": len(executed_trades),
            "trades": executed_trades,
            "equity": round(self.portfolio.total_equity, 2),
        }

    def _risk_check(self, date, data):
        for sym, pos in list(self.portfolio.positions.items()):
            if pos.pnl_pct <= -config.STOP_LOSS_PCT:
                sig = Signal(date=date, symbol=sym, action="SELL", strength=1.0,
                             price=pos.current_price, reason=f"止损触发 ({pos.pnl_pct:.2%})")
                engine = BacktestEngine(self.strategy, self.symbols, self.market, self.initial_capital)
                sliced = {sym: data[sym]} if sym in data else {}
                if sliced:
                    trade = engine._execute_signal(sig, self.portfolio, sliced)
                    if trade:
                        self.portfolio.trades.append(trade)
                        self.trade_log.append({
                            "time": date.isoformat(), "type": "SELL", "symbol": sym,
                            "price": round(trade.price, 2), "quantity": trade.quantity,
                            "amount": round(trade.amount, 2), "reason": sig.reason,
                            "cash_after": round(self.portfolio.cash, 2),
                            "equity_after": round(self.portfolio.total_equity, 2),
                        })
            elif pos.pnl_pct >= config.TAKE_PROFIT_PCT:
                sig = Signal(date=date, symbol=sym, action="SELL", strength=0.5,
                             price=pos.current_price, reason=f"止盈触发 ({pos.pnl_pct:.2%})")
                engine = BacktestEngine(self.strategy, self.symbols, self.market, self.initial_capital)
                sliced = {sym: data[sym]} if sym in data else {}
                if sliced:
                    trade = engine._execute_signal(sig, self.portfolio, sliced)
                    if trade:
                        self.portfolio.trades.append(trade)
                        self.trade_log.append({
                            "time": date.isoformat(), "type": "SELL", "symbol": sym,
                            "price": round(trade.price, 2), "quantity": trade.quantity,
                            "amount": round(trade.amount, 2), "reason": sig.reason,
                            "cash_after": round(self.portfolio.cash, 2),
                            "equity_after": round(self.portfolio.total_equity, 2),
                        })

    def get_status(self) -> dict:
        positions = []
        for sym, pos in self.portfolio.positions.items():
            positions.append({
                "symbol": sym, "quantity": pos.quantity,
                "avg_cost": round(pos.avg_cost, 4), "current_price": round(pos.current_price, 4),
                "market_value": round(pos.market_value, 2),
                "unrealized_pnl": round(pos.unrealized_pnl, 2),
                "pnl_pct": f"{pos.pnl_pct:.2%}",
            })
        return {
            "strategy": self.strategy.name, "market": self.market,
            "is_running": self.is_running, "tick_interval": self.tick_interval,
            "cash": round(self.portfolio.cash, 2),
            "position_value": round(self.portfolio.position_value, 2),
            "total_equity": round(self.portfolio.total_equity, 2),
            "total_return": f"{(self.portfolio.total_equity / self.initial_capital - 1):.2%}",
            "positions": positions, "total_trades": len(self.portfolio.trades),
            "recent_signals": self.signals_history[-20:],
            "trade_log": self.trade_log[-30:],
            "equity_history": self.equity_history[-30:],
            "symbols": self.symbols,
        }

    def _save_state(self):
        state = {
            "cash": self.portfolio.cash,
            "signals_history": self.signals_history[-100:],
            "equity_history": self.equity_history[-100:],
            "trade_log": self.trade_log[-100:],
            "positions": {
                sym: {"symbol": p.symbol, "quantity": p.quantity, "avg_cost": p.avg_cost,
                      "current_price": p.current_price,
                      "entry_date": p.entry_date.isoformat() if p.entry_date else None}
                for sym, p in self.portfolio.positions.items()
            },
        }
        if self.user_id:
            try:
                from dashboard.models import save_paper_state
                save_paper_state(self.user_id, self.strategy.name, state)
                return
            except Exception as e:
                logger.warning(f"DB保存模拟状态失败，回退JSON: {e}")
        # JSON fallback
        try:
            with open(self._state_file, "w") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存状态失败: {e}")

    def load_state(self):
        # Try SQLite first (multi-user mode)
        if self.user_id:
            try:
                from dashboard.models import get_paper_state
                state = get_paper_state(self.user_id, self.strategy.name)
                if state:
                    self._apply_state(state)
                    logger.info(f"模拟状态已从DB恢复: {self.strategy.name}, user={self.user_id}")
                    return
            except Exception as e:
                logger.warning(f"DB加载模拟状态失败，回退JSON: {e}")

        # JSON fallback
        if not os.path.exists(self._state_file):
            return
        try:
            with open(self._state_file) as f:
                state = json.load(f)
            self._apply_state(state)
            logger.info(f"状态恢复成功: {self.strategy.name}")
        except Exception as e:
            logger.error(f"恢复状态失败: {e}")

    def _apply_state(self, state: dict):
        """从 dict 恢复内存状态"""
        self.portfolio.cash = state.get("cash", self.initial_capital)
        self.signals_history = state.get("signals_history", [])
        self.equity_history = state.get("equity_history", [])
        self.trade_log = state.get("trade_log", [])
        for sym, pd_ in state.get("positions", {}).items():
            entry = pd_.get("entry_date")
            self.portfolio.positions[sym] = Position(
                symbol=pd_["symbol"], quantity=pd_["quantity"],
                avg_cost=pd_["avg_cost"], current_price=pd_["current_price"],
                entry_date=datetime.fromisoformat(entry) if entry else None,
            )


# ======================================================================
# PaperTradingManager
# ======================================================================
class PaperTradingManager:
    def __init__(self):
        self.sessions: Dict[str, PaperTradingSession] = {}
        self.auto_traders: Dict[str, AutoTrader] = {}

    def _user_key(self, key: str, user_id: int = None) -> str:
        """生成用户隔离的复合 key"""
        if user_id:
            return f"{user_id}:{key}"
        return key

    def create_session(self, session_id, strategy, symbols, market="US",
                       initial_capital=None, tick_interval=10, user_id=None):
        full_key = self._user_key(session_id, user_id)
        session = PaperTradingSession(strategy, symbols, market, initial_capital,
                                       tick_interval, user_id=user_id)
        session.load_state()
        self.sessions[full_key] = session
        return session

    def get_session(self, session_id, user_id=None):
        full_key = self._user_key(session_id, user_id)
        return self.sessions.get(full_key)

    def create_auto_trader(self, trader_id, strategy, symbols, market="US",
                           initial_capital=None, speed_ms=0, user_id=None):
        full_key = self._user_key(trader_id, user_id)
        trader = AutoTrader(strategy, symbols, market, initial_capital, speed_ms=speed_ms)
        self.auto_traders[full_key] = trader
        return trader

    def get_auto_trader(self, trader_id, user_id=None):
        full_key = self._user_key(trader_id, user_id)
        return self.auto_traders.get(full_key)

    def get_all_status(self, user_id=None):
        result = {}
        prefix = f"{user_id}:" if user_id else None
        for sid, s in self.sessions.items():
            if prefix is None or sid.startswith(prefix):
                result[sid] = s.get_status()
        for tid, t in self.auto_traders.items():
            if prefix is None or tid.startswith(prefix):
                result[f"auto_{tid}"] = t.get_status()
        return result

    def stop_all(self):
        for s in self.sessions.values():
            s.stop()
        for t in self.auto_traders.values():
            t.stop()

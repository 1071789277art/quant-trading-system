"""
智能交易器 (SmartTrader) - 自动筛选 + 自动买卖 + 自主风控

完整流程:
1. 定期扫描股票池 → 多因子排名 → 选出最有潜力的标的
2. 自动买入排名靠前的股票（仓位管理）
3. 持续监控持仓，根据盈亏情况自主决定卖出:
   - 固定止损 (-8%)
   - 动态追踪止损 (从最高点回落5%卖出)
   - 阶梯止盈 (+15% 卖一半, +30% 全部卖出)
   - 时间止损 (持仓超过N天仍亏损则清仓)
   - 基本面恶化退出 (RSI超买+量价背离)
4. 空出仓位后自动寻找下一个买入机会
"""
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, field

import pandas as pd
import numpy as np

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from backtest.engine import BacktestEngine, Portfolio, Signal, Position, TradeRecord
from data.fetcher import DataFetcher
from trading.screener import StockScreener

logger = logging.getLogger(__name__)


@dataclass
class SmartPosition:
    """智能持仓 - 追踪最高价用于动态止损"""
    symbol: str
    quantity: int
    avg_cost: float
    current_price: float = 0.0
    highest_price: float = 0.0   # 持仓期间最高价
    entry_date: Optional[datetime] = None
    bars_held: int = 0           # 持有K线数
    entry_reason: str = ""

    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price

    @property
    def unrealized_pnl(self) -> float:
        return self.quantity * (self.current_price - self.avg_cost)

    @property
    def pnl_pct(self) -> float:
        return (self.current_price - self.avg_cost) / self.avg_cost if self.avg_cost > 0 else 0

    @property
    def drawdown_from_high(self) -> float:
        """从持仓最高价回撤幅度"""
        if self.highest_price <= 0:
            return 0
        return (self.highest_price - self.current_price) / self.highest_price


class SmartTrader:
    """
    智能交易器

    使用方式:
        trader = SmartTrader(market="US", universe=["AAPL","MSFT",...])
        trader.prepare_data("2022-01-01", "2024-12-31")
        trader.start()     # 后台线程运行
        trader.get_status() # 查看状态
    """

    def __init__(
        self,
        market: str = "US",
        universe: List[str] = None,
        initial_capital: float = None,
        params: dict = None,
        on_trade: Optional[Callable] = None,
        speed_ms: int = 0,
    ):
        self.market = market
        self.universe = universe
        self.initial_capital = initial_capital or config.DEFAULT_CAPITAL
        self.on_trade_callback = on_trade
        self.speed_ms = speed_ms

        self.params = {
            # 筛选参数
            "screen_interval": 20,       # 每N根K线重新筛选一次
            "top_n": 5,                  # 选出前N只
            "min_score": 0.4,            # 最低入选分数
            # 仓位管理
            "max_positions": 5,          # 最大同时持仓数
            "position_pct": 0.18,        # 每只股票仓位比例
            # 风控参数
            "stop_loss": 0.08,           # 固定止损 8%
            "trailing_stop": 0.05,       # 追踪止损 5%（从最高价回落）
            "take_profit_1": 0.15,       # 第一止盈 15%（卖一半）
            "take_profit_2": 0.30,       # 第二止盈 30%（全卖）
            "time_stop_bars": 30,        # 时间止损: 持仓超过30根K线仍亏损则卖出
            "time_stop_loss": 0.02,      # 时间止损阈值: 亏损超过2%
            # 技术面退出
            "rsi_overbought_exit": 75,   # RSI超买退出阈值
            **(params or {}),
        }

        # 状态
        self.cash = self.initial_capital
        self.positions: Dict[str, SmartPosition] = {}
        self.trade_log: List[dict] = []
        self.equity_curve: List[dict] = []
        self.screen_log: List[dict] = []
        self.is_running = False
        self.is_finished = False
        self.current_bar_index = 0
        self.total_bars = 0
        self._thread: Optional[threading.Thread] = None

        # 组件
        self.screener = StockScreener({"top_n": self.params["top_n"]})
        self.fetcher = DataFetcher(cache_dir=config.DATA_DIR)
        self._data: Dict[str, pd.DataFrame] = {}
        self._trading_dates: List = []
        self._current_picks: List[str] = []  # 当前筛选结果
        self.stock_names: Dict[str, str] = {}  # symbol -> name 映射
        self._pick_history: Dict[str, int] = {}  # symbol -> 近N次筛选被选中的次数
        self.data_progress: Dict = {"loaded": 0, "total": 0, "status": "idle"}  # 数据加载进度
        self._market_regime: str = "unknown"  # bullish / bearish / unknown
        self._regime_history: List[dict] = []  # 记录市场状态变化
        self._bullish_count: int = 0  # 连续bullish读数计数
        self._consecutive_losses: int = 0  # 连续亏损笔数
        self._cooldown_until: int = 0  # 冷却期截止的bar index

    # ------------------------------------------------------------------
    # 数据准备
    # ------------------------------------------------------------------
    def prepare_data(self, start_date: str, end_date: str, data: Dict[str, pd.DataFrame] = None):
        if data:
            self._data = data
        else:
            universe = self.universe or []
            total = len(universe)
            self.data_progress = {"loaded": 0, "total": total, "status": "loading"}
            for i, sym in enumerate(universe):
                try:
                    df = self.fetcher.get_daily(sym, start_date, end_date, self.market)
                    if not df.empty and len(df) >= 40:
                        self._data[sym] = df
                except Exception as e:
                    logger.warning(f"加载 {sym} 失败: {e}")
                self.data_progress["loaded"] = i + 1
                if (i + 1) % 50 == 0 or i + 1 == total:
                    logger.info(f"数据加载进度: {i+1}/{total} ({(i+1)*100//total}%)")

        if not self._data:
            self.data_progress["status"] = "error"
            raise ValueError("无可用数据")

        self.universe = list(self._data.keys())

        # 构建 symbol -> name 映射
        try:
            name_df = self.fetcher.get_stock_list(self.market)
            if not name_df.empty:
                self.stock_names = dict(zip(name_df["symbol"], name_df["name"]))
        except Exception as e:
            logger.warning(f"获取股票名称失败: {e}")

        all_dates = sorted(set().union(*(df.index for df in self._data.values())))
        self._trading_dates = [d for d in all_dates
                               if pd.Timestamp(start_date) <= d <= pd.Timestamp(end_date)]
        self.total_bars = len(self._trading_dates)
        self.data_progress = {"loaded": len(self._data), "total": len(self._data), "status": "ready"}
        logger.info(f"SmartTrader 就绪: {len(self._data)}只标的, {self.total_bars}K线")

    # ------------------------------------------------------------------
    # 运行控制
    # ------------------------------------------------------------------
    def start(self):
        if self.is_running:
            return
        self.is_running = True
        self.is_finished = False
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.is_running = False

    def _run_loop(self):
        while self.is_running and self.current_bar_index < self.total_bars:
            date = self._trading_dates[self.current_bar_index]
            try:
                self._process_bar(date)
            except Exception as e:
                logger.error(f"SmartTrader 错误 ({date}): {e}")
                self.trade_log.append({
                    "bar_date": str(date), "type": "ERROR",
                    "symbol": "-", "price": 0, "quantity": 0,
                    "amount": 0, "pnl": 0, "reason": str(e),
                })
            self.current_bar_index += 1
            if self.speed_ms > 0:
                time.sleep(self.speed_ms / 1000)

        self.is_running = False
        self.is_finished = True
        logger.info(f"SmartTrader 完成: {self.current_bar_index}/{self.total_bars}")

    # ------------------------------------------------------------------
    # 核心: 逐K线处理
    # ------------------------------------------------------------------
    def _process_bar(self, date):
        # 构建切片数据
        sliced = {}
        for sym, df in self._data.items():
            s = df[df.index <= date]
            if not s.empty:
                sliced[sym] = s

        # 更新持仓价格和最高价
        for sym, pos in self.positions.items():
            if sym in sliced:
                price = sliced[sym]["close"].iloc[-1]
                pos.current_price = price
                pos.highest_price = max(pos.highest_price, price)
                pos.bars_held += 1

        # === 1. 自主卖出检查（任何市场状态都执行） ===
        self._check_exits(date, sliced)

        # === 2. 市场趋势判断 ===
        self._check_market_regime(sliced)

        # === 3. 定期筛选股票 ===
        if self.current_bar_index % self.params["screen_interval"] == 0:
            self._run_screening(date, sliced)

        # === 4. 自动买入（仅牛市环境 + 不在冷却期） ===
        in_cooldown = self.current_bar_index < self._cooldown_until
        if self._market_regime == "bullish" and not in_cooldown:
            self._auto_buy(date, sliced)

        # 记录净值
        total_equity = self.cash + sum(p.market_value for p in self.positions.values())
        self.equity_curve.append({
            "bar_date": str(date),
            "bar_index": self.current_bar_index,
            "cash": round(self.cash, 2),
            "position_value": round(sum(p.market_value for p in self.positions.values()), 2),
            "total_equity": round(total_equity, 2),
            "num_positions": len(self.positions),
            "regime": self._market_regime,
        })

    def _check_market_regime(self, sliced):
        """
        市场趋势过滤器 V4（双均线确认 + 连续确认）:
        bullish: >45%的股票 价格>MA20 且 MA20>MA60（双重上行确认）
        需要连续2次读数满足条件才切换状态（防假突破）
        """
        dual_uptrend = 0
        total = 0
        for sym, df in sliced.items():
            if len(df) < 60:
                continue
            ma20 = df["close"].rolling(20).mean().iloc[-1]
            ma60 = df["close"].rolling(60).mean().iloc[-1]
            price = df["close"].iloc[-1]
            total += 1
            if price > ma20 and ma20 > ma60:
                dual_uptrend += 1

        if total == 0:
            self._market_regime = "unknown"
            return

        pct = dual_uptrend / total
        prev_regime = self._market_regime

        # 判断当前读数是否满足bullish条件
        is_bullish_reading = pct > 0.55

        if is_bullish_reading:
            self._bullish_count += 1
        else:
            self._bullish_count = 0

        # 切换逻辑: 正常时1次确认即可; 连续亏损后需3次连续bullish才允许入场
        required_confirmations = 3 if self._consecutive_losses >= 2 else 1
        if is_bullish_reading and self._bullish_count >= required_confirmations and prev_regime != "bullish":
            self._market_regime = "bullish"
            logger.info(f"市场状态切换→bullish: 连续{self._bullish_count}次确认 (双均线上方: {pct:.0%})")
        elif not is_bullish_reading and prev_regime == "bullish":
            self._market_regime = "bearish"
            self._bullish_count = 0
            logger.info(f"市场状态切换→bearish: 双均线上方仅{pct:.0%}")
        elif prev_regime == "unknown":
            self._market_regime = "bearish"  # 初始状态默认bearish

    def _get_name(self, sym: str) -> str:
        """获取股票名称"""
        return self.stock_names.get(sym, sym)

    # ------------------------------------------------------------------
    # 筛选
    # ------------------------------------------------------------------
    def _run_screening(self, date, sliced):
        """运行股票筛选"""
        try:
            ranked, _ = self.screener.scan(
                universe=self.universe, data=sliced,
                pick_history=self._pick_history,
            )
        except Exception as e:
            logger.warning(f"筛选失败: {e}")
            return

        min_score = self.params["min_score"]
        self._current_picks = [
            r["symbol"] for r in ranked
            if r["score"] >= min_score and r["symbol"] not in self.positions
        ]

        # 衰减历史计数（每次筛选所有计数减半，避免永久性惩罚）
        self._pick_history = {k: max(1, v // 2) for k, v in self._pick_history.items() if v > 1}

        # 更新本次选中股票的计数
        for r in ranked[:self.params["top_n"]]:
            sym = r["symbol"]
            self._pick_history[sym] = self._pick_history.get(sym, 0) + 1

        # 记录筛选日志
        top_entries = [
            {"symbol": r["symbol"], "name": self._get_name(r["symbol"]),
             "score": r["score"], "price": r["price"], "reason": r["signal_reason"]}
            for r in ranked[:self.params["top_n"]]
        ]
        self.screen_log.append({
            "bar_date": str(date),
            "bar_index": self.current_bar_index,
            "picks": top_entries,
        })

    # ------------------------------------------------------------------
    # 自动买入
    # ------------------------------------------------------------------
    def _auto_buy(self, date, sliced):
        """自动买入筛选出的股票"""
        available_slots = self.params["max_positions"] - len(self.positions)
        if available_slots <= 0 or not self._current_picks:
            return

        for sym in self._current_picks[:]:
            if available_slots <= 0:
                break
            if sym in self.positions or sym not in sliced:
                continue

            df = sliced[sym]
            price = df["close"].iloc[-1]

            # 计算买入数量
            total_equity = self.cash + sum(p.market_value for p in self.positions.values())
            target_amount = total_equity * self.params["position_pct"]
            buy_amount = min(target_amount, self.cash * 0.95)  # 留5%现金

            lot_size = config.A_SHARE_LOT_SIZE if self.market == "A_SHARE" else 1
            quantity = int(buy_amount / price / lot_size) * lot_size
            if quantity <= 0:
                continue

            cost = quantity * price
            commission = max(cost * config.DEFAULT_COMMISSION, config.DEFAULT_MIN_COMMISSION)
            total_cost = cost + commission

            if total_cost > self.cash:
                continue

            # 执行买入
            self.cash -= total_cost
            self.positions[sym] = SmartPosition(
                symbol=sym, quantity=quantity, avg_cost=price,
                current_price=price, highest_price=price,
                entry_date=date, entry_reason=self._get_pick_reason(sym),
            )
            self._current_picks.remove(sym)
            available_slots -= 1

            entry = {
                "bar_date": str(date), "type": "BUY", "symbol": sym,
                "name": self._get_name(sym),
                "price": round(price, 2), "quantity": quantity,
                "amount": round(cost, 2), "pnl": 0,
                "reason": self._get_pick_reason(sym),
                "cash_after": round(self.cash, 2),
                "equity_after": round(self.cash + sum(p.market_value for p in self.positions.values()), 2),
            }
            self.trade_log.append(entry)
            if self.on_trade_callback:
                self.on_trade_callback(entry)

    def _get_pick_reason(self, sym: str) -> str:
        """获取选股原因"""
        for log in reversed(self.screen_log):
            for pick in log.get("picks", []):
                if pick["symbol"] == sym:
                    return pick.get("reason", "综合评分入选")
        return "综合评分入选"

    # ------------------------------------------------------------------
    # 自主卖出
    # ------------------------------------------------------------------
    def _check_exits(self, date, sliced):
        """检查所有持仓的卖出条件"""
        for sym in list(self.positions.keys()):
            pos = self.positions[sym]
            pnl_pct = pos.pnl_pct
            drawdown = pos.drawdown_from_high

            sell_reason = None
            sell_strength = 1.0  # 1.0=全卖, 0.5=卖一半

            # 条件1: 固定止损
            if pnl_pct <= -self.params["stop_loss"]:
                sell_reason = f"止损触发 (亏损{pnl_pct:.1%}，阈值-{self.params['stop_loss']:.0%})"

            # 条件2: 追踪止损 (从最高价回落)
            elif drawdown >= self.params["trailing_stop"] and pnl_pct > 0:
                sell_reason = f"追踪止损 (从高点回落{drawdown:.1%}，最高{pos.highest_price:.2f})"

            # 条件3: 阶梯止盈
            elif pnl_pct >= self.params["take_profit_2"]:
                sell_reason = f"止盈清仓 (盈利{pnl_pct:.1%}，阈值+{self.params['take_profit_2']:.0%})"
            elif pnl_pct >= self.params["take_profit_1"]:
                sell_reason = f"止盈减仓 (盈利{pnl_pct:.1%}，阈值+{self.params['take_profit_1']:.0%})"
                sell_strength = 0.5

            # 条件4: 时间止损 (持仓过久仍亏损)
            elif pos.bars_held >= self.params["time_stop_bars"] and pnl_pct <= -self.params["time_stop_loss"]:
                sell_reason = f"时间止损 (持有{pos.bars_held}天，亏损{pnl_pct:.1%})"

            # 条件5: 技术面恶化 (RSI超买 + 趋势向下)
            elif sym in sliced:
                close = sliced[sym]["close"]
                if len(close) >= 20:
                    rsi = self._quick_rsi(close, 14)
                    ma5 = close.tail(5).mean()
                    ma20 = close.tail(20).mean()
                    if rsi > self.params["rsi_overbought_exit"] and ma5 < ma20:
                        sell_reason = f"技术恶化 (RSI={rsi:.0f}超买 + MA5<MA20)"

            if sell_reason:
                self._execute_sell(date, sym, pos, sell_strength, sell_reason)

    def _execute_sell(self, date, sym, pos, strength, reason):
        """执行卖出"""
        price = pos.current_price
        quantity = pos.quantity
        if strength < 1.0:
            lot_size = config.A_SHARE_LOT_SIZE if self.market == "A_SHARE" else 1
            quantity = max(int(quantity * strength / lot_size) * lot_size, lot_size)
            quantity = min(quantity, pos.quantity)

        amount = quantity * price
        commission = max(amount * config.DEFAULT_COMMISSION, config.DEFAULT_MIN_COMMISSION)
        stamp_tax = amount * config.DEFAULT_STAMP_TAX if self.market == "A_SHARE" else 0
        net = amount - commission - stamp_tax
        pnl = (price - pos.avg_cost) * quantity - commission - stamp_tax

        self.cash += net

        if quantity >= pos.quantity:
            # 全仓卖出 - 追踪连续亏损
            if pnl < 0:
                self._consecutive_losses += 1
                if self._consecutive_losses >= 2:
                    self._cooldown_until = self.current_bar_index + 20
                    logger.info(f"连续亏损{self._consecutive_losses}笔，进入冷却期至bar {self._cooldown_until}（需3次bullish确认）")
            else:
                self._consecutive_losses = 0
            del self.positions[sym]
        else:
            pos.quantity -= quantity

        entry = {
            "bar_date": str(date), "type": "SELL", "symbol": sym,
            "name": self._get_name(sym),
            "price": round(price, 2), "quantity": quantity,
            "amount": round(amount, 2), "pnl": round(pnl, 2),
            "reason": reason,
            "cash_after": round(self.cash, 2),
            "equity_after": round(self.cash + sum(p.market_value for p in self.positions.values()), 2),
        }
        self.trade_log.append(entry)
        if self.on_trade_callback:
            self.on_trade_callback(entry)

    def _quick_rsi(self, close: pd.Series, period: int) -> float:
        delta = close.diff().tail(period)
        gain = delta.where(delta > 0, 0).mean()
        loss = (-delta.where(delta < 0, 0)).mean()
        if loss == 0:
            return 100
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def get_status(self) -> dict:
        total_equity = self.cash + sum(p.market_value for p in self.positions.values())
        positions = []
        for sym, pos in self.positions.items():
            positions.append({
                "symbol": sym,
                "name": self._get_name(sym),
                "quantity": pos.quantity,
                "avg_cost": round(pos.avg_cost, 2),
                "current_price": round(pos.current_price, 2),
                "highest_price": round(pos.highest_price, 2),
                "market_value": round(pos.market_value, 2),
                "pnl_pct": f"{pos.pnl_pct:.2%}",
                "unrealized_pnl": round(pos.unrealized_pnl, 2),
                "bars_held": pos.bars_held,
                "drawdown_from_high": f"{pos.drawdown_from_high:.2%}",
                "entry_reason": pos.entry_reason,
            })

        progress = (self.current_bar_index / self.total_bars * 100) if self.total_bars > 0 else 0

        # 最新筛选结果
        latest_screen = self.screen_log[-1] if self.screen_log else None

        return {
            "strategy": "智能选股交易",
            "market": self.market,
            "is_running": self.is_running,
            "is_finished": self.is_finished,
            "data_progress": self.data_progress,
            "market_regime": self._market_regime,
            "consecutive_losses": self._consecutive_losses,
            "in_cooldown": self.current_bar_index < self._cooldown_until,
            "cooldown_until_bar": self._cooldown_until,
            "progress": round(progress, 1),
            "current_bar": self.current_bar_index,
            "total_bars": self.total_bars,
            "cash": round(self.cash, 2),
            "position_value": round(sum(p.market_value for p in self.positions.values()), 2),
            "total_equity": round(total_equity, 2),
            "total_return": f"{(total_equity / self.initial_capital - 1):.2%}",
            "total_trades": len(self.trade_log),
            "num_positions": len(self.positions),
            "max_positions": self.params["max_positions"],
            "positions": positions,
            "trade_log": self.trade_log[-50:],
            "equity_curve": self.equity_curve[-200:],
            "screen_log": self.screen_log[-10:],
            "latest_screen": latest_screen,
            "symbols": self.universe,
        }

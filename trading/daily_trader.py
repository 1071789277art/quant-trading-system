"""
每日实盘交易执行器 (DailyTrader)

每个交易日收盘后执行一次:
1. 加载上次状态（持仓、现金、历史记录）
2. 获取最新行情，更新持仓价格
3. 检查卖出条件（止损/追踪止损/止盈/时间止损/技术恶化）
4. 每20个交易日运行一次多因子筛选
5. 自动买入筛选出的股票
6. 保存状态到磁盘
7. 判断是否已盈利，记录日志
"""
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import numpy as np

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config
from data.fetcher import DataFetcher
from trading.screener import StockScreener, DEFAULT_ASHARE_UNIVERSE, DEFAULT_US_UNIVERSE

logger = logging.getLogger(__name__)

# 状态文件路径
STATE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "daily_state")


def _state_file(market: str = "A_SHARE") -> str:
    """每个市场独立的持久化文件"""
    suffix = "us" if market == "US" else "ashare"
    return os.path.join(STATE_DIR, f"daily_trader_{suffix}.json")


class DailyTrader:
    """
    每日执行一次的智能交易器

    用法:
        trader = DailyTrader.load_or_create()
        result = trader.run_today()
        # 或
        trader.save()  # 持久化
    """

    def __init__(
        self,
        market: str = "A_SHARE",
        universe: List[str] = None,
        initial_capital: float = 1_000_000,
        params: dict = None,
    ):
        self.market = market
        if universe:
            self.universe = universe
        elif market == "US":
            self.universe = list(DEFAULT_US_UNIVERSE)
        else:
            self.universe = list(DEFAULT_ASHARE_UNIVERSE)
        self.initial_capital = initial_capital

        self.params = {
            "screen_interval": 20,       # 每20个交易日筛选一次
            "top_n": 5,
            "min_score": 0.4,
            "max_positions": 5,
            "position_pct": 0.18,
            "stop_loss": 0.08,
            "trailing_stop": 0.05,
            "take_profit_1": 0.15,
            "take_profit_2": 0.30,
            "time_stop_bars": 30,
            "time_stop_loss": 0.02,
            "rsi_overbought_exit": 75,
            **(params or {}),
        }

        # 状态
        self.cash = initial_capital
        self.positions: Dict[str, dict] = {}  # {symbol: {quantity, avg_cost, current_price, highest_price, bars_held, entry_date, entry_reason}}
        self.trade_log: List[dict] = []
        self.equity_history: List[dict] = []
        self.screen_log: List[dict] = []
        self.bar_count = 0           # 已执行交易日数
        self.last_run_date: Optional[str] = None
        self.total_trades = 0
        self.is_profit = False       # 是否已盈利
        self.created_at = datetime.now().isoformat()

        # 组件
        self.fetcher = DataFetcher(cache_dir=config.DATA_DIR)
        self.screener = StockScreener({"top_n": self.params["top_n"]})
        self._current_picks: List[str] = []

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def save(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        path = _state_file(self.market)
        state = {
            "market": self.market,
            "universe": self.universe,
            "initial_capital": self.initial_capital,
            "params": self.params,
            "cash": self.cash,
            "positions": self.positions,
            "trade_log": self.trade_log[-200:],
            "equity_history": self.equity_history[-200:],
            "screen_log": self.screen_log[-20:],
            "bar_count": self.bar_count,
            "last_run_date": self.last_run_date,
            "total_trades": self.total_trades,
            "is_profit": self.is_profit,
            "created_at": self.created_at,
            "current_picks": self._current_picks,
            "updated_at": datetime.now().isoformat(),
        }
        with open(path, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        logger.info(f"状态已保存({self.market}): bar_count={self.bar_count}, cash={self.cash:.2f}")

    @classmethod
    def load_or_create(cls, market: str = "A_SHARE", **kwargs) -> "DailyTrader":
        path = _state_file(market)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    state = json.load(f)
                trader = cls(
                    market=state.get("market", market),
                    universe=state.get("universe"),
                    initial_capital=state.get("initial_capital", 1_000_000),
                    params=state.get("params"),
                )
                trader.cash = state.get("cash", trader.initial_capital)
                trader.positions = state.get("positions", {})
                trader.trade_log = state.get("trade_log", [])
                trader.equity_history = state.get("equity_history", [])
                trader.screen_log = state.get("screen_log", [])
                trader.bar_count = state.get("bar_count", 0)
                trader.last_run_date = state.get("last_run_date")
                trader.total_trades = state.get("total_trades", 0)
                trader.is_profit = state.get("is_profit", False)
                trader.created_at = state.get("created_at", datetime.now().isoformat())
                trader._current_picks = state.get("current_picks", [])
                logger.info(f"状态已恢复({market}): 第{trader.bar_count}个交易日, 上次运行={trader.last_run_date}")
                return trader
            except Exception as e:
                logger.error(f"恢复状态失败: {e}, 创建新实例")
        kwargs.setdefault("market", market)
        return cls(**kwargs)

    # ------------------------------------------------------------------
    # 核心: 执行今日交易
    # ------------------------------------------------------------------
    def run_today(self) -> dict:
        """
        执行一个交易日的完整流程，返回当日摘要
        """
        today = datetime.now().strftime("%Y-%m-%d")

        # 防止同一天重复执行
        if self.last_run_date == today:
            return self._build_result("今日已执行，跳过重复运行")

        self.bar_count += 1

        # 1. 获取最新数据
        lookback_start = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
        data = {}
        for sym in self.universe:
            try:
                df = self.fetcher.get_daily(sym, lookback_start, today, self.market)
                if not df.empty and len(df) >= 30:
                    data[sym] = df
            except Exception as e:
                logger.warning(f"获取 {sym} 数据失败: {e}")

        if not data:
            return self._build_result("无可用数据")

        # 2. 更新持仓价格
        for sym in list(self.positions.keys()):
            pos = self.positions[sym]
            if sym in data:
                price = float(data[sym]["close"].iloc[-1])
                pos["current_price"] = price
                pos["highest_price"] = max(pos.get("highest_price", 0), price)
                pos["bars_held"] = pos.get("bars_held", 0) + 1

        # 3. 检查卖出
        sells_today = self._check_exits(today, data)

        # 4. 定期筛选（首日也执行）
        if self.bar_count == 1 or self.bar_count % self.params["screen_interval"] == 0:
            self._run_screening(today, data)

        # 5. 自动买入
        buys_today = self._auto_buy(today, data)

        # 6. 记录净值
        position_value = sum(
            p["quantity"] * p["current_price"] for p in self.positions.values()
        )
        total_equity = self.cash + position_value
        total_return = (total_equity / self.initial_capital - 1)

        self.equity_history.append({
            "date": today,
            "bar": self.bar_count,
            "cash": round(self.cash, 2),
            "position_value": round(position_value, 2),
            "total_equity": round(total_equity, 2),
            "total_return_pct": round(total_return * 100, 2),
            "num_positions": len(self.positions),
        })

        # 7. 判断是否盈利
        self.is_profit = total_return > 0

        self.last_run_date = today
        self.total_trades = len(self.trade_log)
        self.save()

        return self._build_result(
            f"第{self.bar_count}个交易日执行完毕",
            sells_today=sells_today,
            buys_today=buys_today,
        )

    def _build_result(self, message: str, sells_today=None, buys_today=None) -> dict:
        position_value = sum(
            p["quantity"] * p["current_price"] for p in self.positions.values()
        )
        total_equity = self.cash + position_value
        total_return = (total_equity / self.initial_capital - 1)

        return {
            "message": message,
            "date": self.last_run_date,
            "bar_count": self.bar_count,
            "cash": round(self.cash, 2),
            "position_value": round(position_value, 2),
            "total_equity": round(total_equity, 2),
            "total_return_pct": round(total_return * 100, 2),
            "total_return_str": f"{total_return:.2%}",
            "is_profit": self.is_profit,
            "num_positions": len(self.positions),
            "total_trades": len(self.trade_log),
            "positions": self._positions_summary(),
            "sells_today": sells_today or [],
            "buys_today": buys_today or [],
        }

    def _positions_summary(self) -> list:
        result = []
        for sym, pos in self.positions.items():
            pnl_pct = (pos["current_price"] - pos["avg_cost"]) / pos["avg_cost"] if pos["avg_cost"] > 0 else 0
            drawdown = (pos["highest_price"] - pos["current_price"]) / pos["highest_price"] if pos.get("highest_price", 0) > 0 else 0
            result.append({
                "symbol": sym,
                "quantity": pos["quantity"],
                "avg_cost": round(pos["avg_cost"], 2),
                "current_price": round(pos["current_price"], 2),
                "highest_price": round(pos.get("highest_price", 0), 2),
                "pnl_pct": f"{pnl_pct:.2%}",
                "unrealized_pnl": round(pos["quantity"] * (pos["current_price"] - pos["avg_cost"]), 2),
                "drawdown_from_high": f"{drawdown:.2%}",
                "bars_held": pos.get("bars_held", 0),
                "entry_reason": pos.get("entry_reason", ""),
            })
        return result

    # ------------------------------------------------------------------
    # 筛选
    # ------------------------------------------------------------------
    def _run_screening(self, today: str, data: Dict[str, pd.DataFrame]):
        try:
            ranked, _ = self.screener.scan(universe=self.universe, data=data)
        except Exception as e:
            logger.warning(f"筛选失败: {e}")
            return

        min_score = self.params["min_score"]
        self._current_picks = [
            r["symbol"] for r in ranked
            if r["score"] >= min_score and r["symbol"] not in self.positions
        ]

        top_entries = [
            {"symbol": r["symbol"], "score": r["score"],
             "price": r["price"], "reason": r["signal_reason"]}
            for r in ranked[:self.params["top_n"]]
        ]
        self.screen_log.append({
            "date": today,
            "bar": self.bar_count,
            "picks": top_entries,
        })
        logger.info(f"筛选完成: {len(ranked)}只, 入选{len(self._current_picks)}只")

    # ------------------------------------------------------------------
    # 自动买入
    # ------------------------------------------------------------------
    def _auto_buy(self, today: str, data: Dict[str, pd.DataFrame]) -> list:
        available_slots = self.params["max_positions"] - len(self.positions)
        if available_slots <= 0 or not self._current_picks:
            return []

        buys = []
        for sym in self._current_picks[:]:
            if available_slots <= 0:
                break
            if sym in self.positions or sym not in data:
                continue

            df = data[sym]
            price = float(df["close"].iloc[-1])

            position_value = sum(p["quantity"] * p["current_price"] for p in self.positions.values())
            total_equity = self.cash + position_value
            target_amount = total_equity * self.params["position_pct"]
            buy_amount = min(target_amount, self.cash * 0.95)

            lot_size = config.A_SHARE_LOT_SIZE if self.market == "A_SHARE" else 1
            quantity = int(buy_amount / price / lot_size) * lot_size
            if quantity <= 0:
                continue

            cost = quantity * price
            commission = max(cost * config.DEFAULT_COMMISSION, config.DEFAULT_MIN_COMMISSION)
            total_cost = cost + commission

            if total_cost > self.cash:
                continue

            self.cash -= total_cost
            reason = self._get_pick_reason(sym)
            self.positions[sym] = {
                "symbol": sym,
                "quantity": quantity,
                "avg_cost": price,
                "current_price": price,
                "highest_price": price,
                "bars_held": 0,
                "entry_date": today,
                "entry_reason": reason,
            }
            self._current_picks.remove(sym)
            available_slots -= 1

            entry = {
                "date": today, "type": "BUY", "symbol": sym,
                "price": round(price, 2), "quantity": quantity,
                "amount": round(cost, 2), "pnl": 0, "reason": reason,
            }
            self.trade_log.append(entry)
            buys.append(entry)
            logger.info(f"买入 {sym}: {quantity}股 @ {price:.2f}")

        return buys

    def _get_pick_reason(self, sym: str) -> str:
        for log in reversed(self.screen_log):
            for pick in log.get("picks", []):
                if pick["symbol"] == sym:
                    return pick.get("reason", "综合评分入选")
        return "综合评分入选"

    # ------------------------------------------------------------------
    # 自主卖出
    # ------------------------------------------------------------------
    def _check_exits(self, today: str, data: Dict[str, pd.DataFrame]) -> list:
        sells = []
        for sym in list(self.positions.keys()):
            pos = self.positions[sym]
            price = pos["current_price"]
            avg_cost = pos["avg_cost"]
            pnl_pct = (price - avg_cost) / avg_cost if avg_cost > 0 else 0
            highest = pos.get("highest_price", price)
            drawdown = (highest - price) / highest if highest > 0 else 0

            sell_reason = None
            sell_strength = 1.0

            # 条件1: 固定止损
            if pnl_pct <= -self.params["stop_loss"]:
                sell_reason = f"止损触发 (亏损{pnl_pct:.1%}，阈值-{self.params['stop_loss']:.0%})"

            # 条件2: 追踪止损
            elif drawdown >= self.params["trailing_stop"] and pnl_pct > 0:
                sell_reason = f"追踪止损 (从高点回落{drawdown:.1%}，最高{highest:.2f})"

            # 条件3: 阶梯止盈
            elif pnl_pct >= self.params["take_profit_2"]:
                sell_reason = f"止盈清仓 (盈利{pnl_pct:.1%}，阈值+{self.params['take_profit_2']:.0%})"
            elif pnl_pct >= self.params["take_profit_1"]:
                sell_reason = f"止盈减仓 (盈利{pnl_pct:.1%}，阈值+{self.params['take_profit_1']:.0%})"
                sell_strength = 0.5

            # 条件4: 时间止损
            elif (pos.get("bars_held", 0) >= self.params["time_stop_bars"]
                  and pnl_pct <= -self.params["time_stop_loss"]):
                sell_reason = f"时间止损 (持有{pos.get('bars_held',0)}天，亏损{pnl_pct:.1%})"

            # 条件5: 技术面恶化
            elif sym in data:
                close = data[sym]["close"]
                if len(close) >= 20:
                    rsi = self._quick_rsi(close, 14)
                    ma5 = float(close.tail(5).mean())
                    ma20 = float(close.tail(20).mean())
                    if rsi > self.params["rsi_overbought_exit"] and ma5 < ma20:
                        sell_reason = f"技术恶化 (RSI={rsi:.0f}超买 + MA5<MA20)"

            if sell_reason:
                entry = self._execute_sell(today, sym, pos, sell_strength, sell_reason)
                if entry:
                    sells.append(entry)

        return sells

    def _execute_sell(self, today, sym, pos, strength, reason) -> Optional[dict]:
        price = pos["current_price"]
        quantity = pos["quantity"]

        if strength < 1.0:
            lot_size = config.A_SHARE_LOT_SIZE if self.market == "A_SHARE" else 1
            quantity = max(int(quantity * strength / lot_size) * lot_size, lot_size)
            quantity = min(quantity, pos["quantity"])

        amount = quantity * price
        commission = max(amount * config.DEFAULT_COMMISSION, config.DEFAULT_MIN_COMMISSION)
        stamp_tax = amount * config.DEFAULT_STAMP_TAX if self.market == "A_SHARE" else 0
        net = amount - commission - stamp_tax
        pnl = (price - pos["avg_cost"]) * quantity - commission - stamp_tax

        self.cash += net

        if quantity >= pos["quantity"]:
            del self.positions[sym]
        else:
            pos["quantity"] -= quantity

        entry = {
            "date": today, "type": "SELL", "symbol": sym,
            "price": round(price, 2), "quantity": quantity,
            "amount": round(amount, 2), "pnl": round(pnl, 2),
            "reason": reason,
        }
        self.trade_log.append(entry)
        logger.info(f"卖出 {sym}: {quantity}股 @ {price:.2f}, 盈亏={pnl:.2f}, {reason}")
        return entry

    def _quick_rsi(self, close: pd.Series, period: int) -> float:
        delta = close.diff().tail(period)
        gain = delta.where(delta > 0, 0).mean()
        loss = (-delta.where(delta < 0, 0)).mean()
        if loss == 0:
            return 100
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    def get_full_status(self) -> dict:
        position_value = sum(
            p["quantity"] * p["current_price"] for p in self.positions.values()
        )
        total_equity = self.cash + position_value
        total_return = (total_equity / self.initial_capital - 1)

        latest_screen = self.screen_log[-1] if self.screen_log else None

        return {
            "strategy": "美股每日实盘模拟" if self.market == "US" else "A股每日实盘模拟",
            "market": self.market,
            "created_at": self.created_at,
            "last_run_date": self.last_run_date,
            "bar_count": self.bar_count,
            "cash": round(self.cash, 2),
            "position_value": round(position_value, 2),
            "total_equity": round(total_equity, 2),
            "total_return_pct": round(total_return * 100, 2),
            "total_return_str": f"{total_return:.2%}",
            "is_profit": self.is_profit,
            "num_positions": len(self.positions),
            "max_positions": self.params["max_positions"],
            "total_trades": len(self.trade_log),
            "positions": self._positions_summary(),
            "trade_log": self.trade_log[-30:],
            "equity_history": self.equity_history[-60:],
            "screen_log": self.screen_log[-5:],
            "latest_screen": latest_screen,
            "universe": self.universe,
            "params": self.params,
        }

    def reset(self):
        """重置状态（慎用）"""
        self.cash = self.initial_capital
        self.positions = {}
        self.trade_log = []
        self.equity_history = []
        self.screen_log = []
        self.bar_count = 0
        self.last_run_date = None
        self.total_trades = 0
        self.is_profit = False
        self._current_picks = []
        self.save()

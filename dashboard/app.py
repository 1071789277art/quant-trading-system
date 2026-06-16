"""
Web仪表盘 - Flask + Plotly 交互式界面
"""
import json
import logging
import math
import os
import sys
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from flask import Flask, render_template, request, jsonify

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _sanitize_for_json(obj):
    """递归清理NaN/Infinity/numpy类型，确保JSON可被JavaScript安全解析"""
    import numpy as np
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        if np.isnan(obj) or np.isinf(obj):
            return None
        return float(obj)
    if isinstance(obj, np.ndarray):
        return [_sanitize_for_json(v) for v in obj.tolist()]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    return obj
import config
from backtest.engine import BacktestEngine, BaseStrategy
from data.fetcher import DataFetcher
from trading.paper_trading import PaperTradingManager
from trading.smart_trader import SmartTrader
from trading.screener import StockScreener, DEFAULT_US_UNIVERSE, DEFAULT_ASHARE_UNIVERSE
from trading.daily_trader import DailyTrader

# 策略注册表
from strategies.ma_crossover import MACrossover
from strategies.macd_strategy import MACDStrategy
from strategies.rsi_strategy import RSIStrategy
from strategies.bollinger_strategy import BollingerStrategy
from strategies.momentum import MomentumStrategy
from strategies.multi_factor import MultiFactorStrategy

logger = logging.getLogger(__name__)

STRATEGY_REGISTRY = {
    "双均线交叉": MACrossover,
    "MACD策略": MACDStrategy,
    "RSI均值回归": RSIStrategy,
    "布林带突破": BollingerStrategy,
    "动量轮动": MomentumStrategy,
    "多因子选股": MultiFactorStrategy,
}

# 全局状态
paper_manager = PaperTradingManager()
smart_traders: dict = {}
last_backtest_result = None


def create_app() -> Flask:
    from flask.json.provider import DefaultJSONProvider

    class SafeJSONProvider(DefaultJSONProvider):
        def dumps(self, obj, **kwargs):
            obj = _sanitize_for_json(obj)
            return super().dumps(obj, **kwargs)

    app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "templates"))
    app.json = SafeJSONProvider(app)

    @app.route("/")
    def index():
        return render_template("index.html")

    # ==============================================================
    # API: 获取策略列表
    # ==============================================================
    @app.route("/api/strategies")
    def api_strategies():
        result = {}
        for name, cls in STRATEGY_REGISTRY.items():
            instance = cls()
            result[name] = {
                "description": cls.__doc__.strip().split("\n")[0] if cls.__doc__ else "",
                "default_params": instance.params,
            }
        return jsonify(result)

    # ==============================================================
    # API: 运行回测
    # ==============================================================
    @app.route("/api/backtest", methods=["POST"])
    def api_backtest():
        global last_backtest_result
        try:
            params = request.json
            strategy_name = params.get("strategy", "双均线交叉")
            symbols = params.get("symbols", ["AAPL"])
            market = params.get("market", "US")
            start_date = params.get("start_date", "2022-01-01")
            end_date = params.get("end_date", "2024-12-31")
            capital = params.get("capital", config.DEFAULT_CAPITAL)
            strategy_params = params.get("strategy_params", {})

            if strategy_name not in STRATEGY_REGISTRY:
                return jsonify({"error": f"未知策略: {strategy_name}"}), 400

            strategy_cls = STRATEGY_REGISTRY[strategy_name]
            strategy = strategy_cls(strategy_params)

            engine = BacktestEngine(
                strategy=strategy,
                symbols=symbols,
                market=market,
                initial_capital=capital,
            )

            result = engine.run(start_date=start_date, end_date=end_date)
            last_backtest_result = result

            # 构建返回数据
            equity_data = {
                "dates": [d.strftime("%Y-%m-%d") for d in result["equity_curve"].index],
                "values": [round(v, 2) for v in result["equity_curve"].values],
            }

            # 价格走势
            price_data = {}
            for sym, df in result["data"].items():
                mask = (df.index >= pd.Timestamp(start_date)) & (df.index <= pd.Timestamp(end_date))
                sliced = df[mask]
                price_data[sym] = {
                    "dates": [d.strftime("%Y-%m-%d") for d in sliced.index],
                    "open": sliced["open"].round(2).tolist(),
                    "high": sliced["high"].round(2).tolist(),
                    "low": sliced["low"].round(2).tolist(),
                    "close": sliced["close"].round(2).tolist(),
                    "volume": sliced["volume"].tolist(),
                }

            # 交易记录
            trades = result["trades"]
            trades_data = []
            if not trades.empty:
                for _, row in trades.iterrows():
                    trades_data.append({
                        "date": row["date"].strftime("%Y-%m-%d") if hasattr(row["date"], "strftime") else str(row["date"]),
                        "symbol": row["symbol"],
                        "direction": row["direction"],
                        "price": row["price"],
                        "quantity": row["quantity"],
                        "amount": row["amount"],
                        "commission": row["commission"],
                        "pnl": row.get("pnl", 0),
                        "reason": row.get("reason", ""),
                    })

            # 绘制标记点
            buy_signals = []
            sell_signals = []
            for _, row in trades.iterrows():
                date_str = row["date"].strftime("%Y-%m-%d") if hasattr(row["date"], "strftime") else str(row["date"])
                if row["direction"] == "BUY":
                    buy_signals.append({"date": date_str, "price": row["price"]})
                else:
                    sell_signals.append({"date": date_str, "price": row["price"]})

            # 回撤曲线
            eq = result["equity_curve"]
            cummax = eq.cummax()
            drawdown = ((eq - cummax) / cummax) * 100
            drawdown_data = {
                "dates": [d.strftime("%Y-%m-%d") for d in drawdown.index],
                "values": [round(v, 2) for v in drawdown.values],
            }

            return jsonify({
                "success": True,
                "metrics": result["metrics"],
                "equity": equity_data,
                "price": price_data,
                "trades": trades_data,
                "buy_signals": buy_signals,
                "sell_signals": sell_signals,
                "drawdown": drawdown_data,
            })

        except Exception as e:
            logger.exception("回测执行错误")
            return jsonify({"error": str(e)}), 500

    # ==============================================================
    # API: 策略对比
    # ==============================================================
    @app.route("/api/compare", methods=["POST"])
    def api_compare():
        try:
            params = request.json
            strategies_to_run = params.get("strategies", list(STRATEGY_REGISTRY.keys()))
            symbols = params.get("symbols", ["AAPL"])
            market = params.get("market", "US")
            start_date = params.get("start_date", "2022-01-01")
            end_date = params.get("end_date", "2024-12-31")
            capital = params.get("capital", config.DEFAULT_CAPITAL)

            results = {}
            for name in strategies_to_run:
                if name not in STRATEGY_REGISTRY:
                    continue
                strategy = STRATEGY_REGISTRY[name]()
                engine = BacktestEngine(
                    strategy=strategy, symbols=symbols, market=market, initial_capital=capital
                )
                try:
                    result = engine.run(start_date=start_date, end_date=end_date)
                    eq = result["equity_curve"]
                    results[name] = {
                        "dates": [d.strftime("%Y-%m-%d") for d in eq.index],
                        "values": [round(v, 2) for v in eq.values],
                        "metrics": result["metrics"],
                    }
                except Exception as e:
                    logger.error(f"策略 {name} 回测失败: {e}")

            return jsonify({"success": True, "results": results})

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ==============================================================
    # API: 模拟交易（实时定时扫描 + 自动买卖）
    # ==============================================================
    @app.route("/api/paper/start", methods=["POST"])
    def api_paper_start():
        params = request.json
        session_id = params.get("session_id", "default")
        strategy_name = params.get("strategy", "双均线交叉")
        symbols = params.get("symbols", ["AAPL"])
        market = params.get("market", "US")
        tick_interval = params.get("tick_interval", 10)

        if strategy_name not in STRATEGY_REGISTRY:
            return jsonify({"error": f"未知策略: {strategy_name}"}), 400

        strategy = STRATEGY_REGISTRY[strategy_name](params.get("strategy_params", {}))
        session = paper_manager.create_session(
            session_id, strategy, symbols, market,
            tick_interval=tick_interval,
        )
        session.start()

        return jsonify({"success": True, "message": f"模拟交易已启动: {strategy_name} (间隔{tick_interval}s)"})

    @app.route("/api/paper/stop", methods=["POST"])
    def api_paper_stop():
        session_id = request.json.get("session_id", "default")
        session = paper_manager.get_session(session_id)
        if session:
            session.stop()
            return jsonify({"success": True})
        return jsonify({"error": "会话不存在"}), 404

    @app.route("/api/paper/status")
    def api_paper_status():
        session_id = request.args.get("session_id", "default")
        session = paper_manager.get_session(session_id)
        if session:
            return jsonify(session.get_status())
        return jsonify({"error": "会话不存在"}), 404

    @app.route("/api/paper/force_tick", methods=["POST"])
    def api_paper_force_tick():
        """手动触发一次行情扫描 + 自动执行买卖"""
        session_id = request.json.get("session_id", "default")
        session = paper_manager.get_session(session_id)
        if session:
            result = session.force_tick()
            return jsonify({"success": True, "result": result})
        return jsonify({"error": "会话不存在"}), 404

    @app.route("/api/paper/sessions")
    def api_paper_sessions():
        return jsonify(paper_manager.get_all_status())

    # ==============================================================
    # API: 自动交易（AutoTrader - 逐K线自动跑数据）
    # ==============================================================
    @app.route("/api/auto/start", methods=["POST"])
    def api_auto_start():
        """启动自动交易运行器 - 快速遍历历史数据自动买卖"""
        params = request.json
        trader_id = params.get("trader_id", "default")
        strategy_name = params.get("strategy", "双均线交叉")
        symbols = params.get("symbols", ["AAPL"])
        market = params.get("market", "US")
        start_date = params.get("start_date", "2022-01-01")
        end_date = params.get("end_date", "2024-12-31")
        capital = params.get("capital", config.DEFAULT_CAPITAL)
        speed_ms = params.get("speed_ms", 0)

        if strategy_name not in STRATEGY_REGISTRY:
            return jsonify({"error": f"未知策略: {strategy_name}"}), 400

        strategy = STRATEGY_REGISTRY[strategy_name](params.get("strategy_params", {}))
        trader = paper_manager.create_auto_trader(
            trader_id, strategy, symbols, market, capital, speed_ms
        )

        try:
            trader.prepare_data(start_date, end_date)
            trader.start()
            return jsonify({
                "success": True,
                "message": f"自动交易已启动: {strategy_name}",
                "total_bars": trader.total_bars,
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/auto/stop", methods=["POST"])
    def api_auto_stop():
        trader_id = request.json.get("trader_id", "default")
        trader = paper_manager.get_auto_trader(trader_id)
        if trader:
            trader.stop()
            return jsonify({"success": True})
        return jsonify({"error": "自动交易器不存在"}), 404

    @app.route("/api/auto/status")
    def api_auto_status():
        trader_id = request.args.get("trader_id", "default")
        trader = paper_manager.get_auto_trader(trader_id)
        if trader:
            return jsonify(trader.get_status())
        return jsonify({"error": "自动交易器不存在"}), 404

    # ==============================================================
    # API: 智能选股交易（SmartTrader - 自动筛选+自动买卖+自主风控）
    # ==============================================================
    @app.route("/api/smart/start", methods=["POST"])
    def api_smart_start():
        """启动智能选股交易器（后台加载数据，立即返回）"""
        params = request.json
        trader_id = params.get("trader_id", "default")
        market = params.get("market", "US")
        universe = params.get("universe")
        start_date = params.get("start_date", "2022-01-01")
        end_date = params.get("end_date", "2024-12-31")
        capital = params.get("capital", config.DEFAULT_CAPITAL)
        speed_ms = params.get("speed_ms", 0)

        # 构建智能参数
        smart_params = {}
        for k in ["stop_loss", "trailing_stop", "take_profit_1", "take_profit_2",
                   "time_stop_bars", "max_positions", "position_pct", "screen_interval"]:
            if k in params:
                smart_params[k] = params[k]

        if universe is None:
            if market == "US":
                universe = DEFAULT_US_UNIVERSE
            else:
                # A股: 动态获取全量股票池（过滤ST、停牌、低价、低量、退市）
                try:
                    from data.live_fetcher import fetch_ashare_stock_list
                    stock_df = fetch_ashare_stock_list(min_price=3.0, min_volume=10000, max_count=300)
                    if not stock_df.empty:
                        universe = stock_df["symbol"].tolist()
                        logger.info(f"智能选股A股动态股票池: {len(universe)}只")
                    else:
                        universe = DEFAULT_ASHARE_UNIVERSE
                        logger.warning("A股动态列表为空，使用默认池")
                except Exception as e:
                    universe = DEFAULT_ASHARE_UNIVERSE
                    logger.warning(f"获取A股全量列表失败: {e}")

        trader = SmartTrader(
            market=market, universe=universe, initial_capital=capital,
            params=smart_params, speed_ms=speed_ms,
        )

        # 先注册 trader（前端可立即查询进度）
        smart_traders[trader_id] = trader

        # 后台线程加载数据 + 启动交易
        def _init_and_run():
            try:
                trader.prepare_data(start_date, end_date)
                trader.start()
            except Exception as e:
                logger.exception(f"SmartTrader 初始化失败: {e}")
                trader.data_progress["status"] = "error"
                trader.data_progress["error"] = str(e)

        import threading
        threading.Thread(target=_init_and_run, daemon=True).start()

        return jsonify({
            "success": True,
            "message": f"智能交易启动中: {len(universe)}只标的加载数据...",
            "universe_size": len(universe),
        })

    @app.route("/api/smart/stop", methods=["POST"])
    def api_smart_stop():
        trader_id = request.json.get("trader_id", "default")
        trader = smart_traders.get(trader_id)
        if trader:
            trader.stop()
            return jsonify({"success": True})
        return jsonify({"error": "智能交易器不存在"}), 404

    @app.route("/api/smart/status")
    def api_smart_status():
        trader_id = request.args.get("trader_id", "default")
        trader = smart_traders.get(trader_id)
        if trader:
            return jsonify(trader.get_status())
        return jsonify({"error": "智能交易器不存在"}), 404

    @app.route("/api/screener/universe")
    def api_screener_universe():
        """获取默认股票池"""
        market = request.args.get("market", "US")
        if market == "A_SHARE":
            # 动态获取全量A股列表
            try:
                from data.live_fetcher import fetch_ashare_stock_list
                stock_df = fetch_ashare_stock_list(min_price=3.0, min_volume=10000, max_count=300)
                if not stock_df.empty:
                    symbols = stock_df["symbol"].tolist()
                    names = stock_df["name"].tolist()
                    return jsonify({
                        "universe": symbols,
                        "names": dict(zip(symbols, names)),
                        "market": "A_SHARE",
                        "total": len(symbols),
                        "dynamic": True,
                    })
            except Exception as e:
                logger.warning(f"动态获取A股列表失败: {e}")
            return jsonify({
                "universe": DEFAULT_ASHARE_UNIVERSE,
                "market": "A_SHARE",
                "total": len(DEFAULT_ASHARE_UNIVERSE),
                "dynamic": False,
            })
        # 美股: 动态获取
        try:
            from data.live_fetcher import fetch_us_stock_list
            stock_df = fetch_us_stock_list(min_price=1.0, min_volume=10000, max_count=300)
            if not stock_df.empty:
                symbols = stock_df["symbol"].tolist()
                names = stock_df["name"].tolist()
                return jsonify({
                    "universe": symbols,
                    "names": dict(zip(symbols, names)),
                    "market": "US",
                    "total": len(symbols),
                    "dynamic": True,
                })
        except Exception as e:
            logger.warning(f"动态获取美股列表失败: {e}")
        return jsonify({
            "universe": DEFAULT_US_UNIVERSE,
            "market": "US",
            "total": len(DEFAULT_US_UNIVERSE),
            "dynamic": False,
        })

    # ==============================================================
    # API: 每日实盘交易（DailyTrader - 每个交易日执行一次）
    # ==============================================================
    @app.route("/api/daily/run", methods=["POST"])
    def api_daily_run():
        """执行今日交易"""
        try:
            market = request.json.get("market", "A_SHARE") if request.is_json else request.args.get("market", "A_SHARE")
            trader = DailyTrader.load_or_create(market=market)
            result = trader.run_today()
            return jsonify(result)
        except Exception as e:
            logger.exception("每日交易执行失败")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/daily/status")
    def api_daily_status():
        """获取每日交易器完整状态"""
        try:
            market = request.args.get("market", "A_SHARE")
            trader = DailyTrader.load_or_create(market=market)
            return jsonify(trader.get_full_status())
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/daily/reset", methods=["POST"])
    def api_daily_reset():
        """重置每日交易器"""
        try:
            market = request.json.get("market", "A_SHARE") if request.is_json else request.args.get("market", "A_SHARE")
            trader = DailyTrader.load_or_create(market=market)
            trader.reset()
            return jsonify({"success": True, "message": f"每日交易器已重置 ({market})"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ==============================================================
    # API: 实时行情（东方财富/新浪直连）
    # ==============================================================
    @app.route("/api/realtime")
    def api_realtime():
        """获取实时行情报价"""
        market = request.args.get("market", "A_SHARE")
        symbols_str = request.args.get("symbols", "")
        if symbols_str:
            symbols = [s.strip().upper() for s in symbols_str.split(",") if s.strip()]
        else:
            if market == "A_SHARE":
                symbols = list(DEFAULT_ASHARE_UNIVERSE)
            else:
                symbols = list(DEFAULT_US_UNIVERSE)

        fetcher = DataFetcher()
        quotes = fetcher.get_realtime(symbols, market)
        return jsonify({"success": True, "market": market, "count": len(quotes), "quotes": quotes})

    # ==============================================================
    # API: K线数据（供个股K线图使用）
    # ==============================================================
    @app.route("/api/kline")
    def api_kline():
        """获取指定股票的K线数据，返回OHLCV+均线"""
        try:
            symbol = request.args.get("symbol", "").strip().upper()
            market = request.args.get("market", "A_SHARE")
            days = int(request.args.get("days", 120))

            if not symbol:
                return jsonify({"error": "缺少symbol参数"}), 400

            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = (datetime.now() - timedelta(days=int(days * 1.8))).strftime("%Y-%m-%d")

            fetcher = DataFetcher(cache_dir=config.DATA_DIR)
            df = fetcher.get_daily(symbol, start_date, end_date, market, fallback_to_sample=True)

            if df.empty:
                return jsonify({"error": f"无法获取 {symbol} 的数据"}), 404

            # 只取最近N天
            df = df.tail(days)

            # 计算均线
            ma5 = df["close"].rolling(5).mean()
            ma20 = df["close"].rolling(20).mean()
            ma60 = df["close"].rolling(60).mean() if len(df) >= 60 else pd.Series(dtype=float)

            dates = [d.strftime("%Y-%m-%d") for d in df.index]
            result = {
                "symbol": symbol,
                "market": market,
                "dates": dates,
                "open": df["open"].round(2).tolist(),
                "high": df["high"].round(2).tolist(),
                "low": df["low"].round(2).tolist(),
                "close": df["close"].round(2).tolist(),
                "volume": df["volume"].tolist(),
                "ma5": [round(v, 2) if pd.notna(v) else None for v in ma5],
                "ma20": [round(v, 2) if pd.notna(v) else None for v in ma20],
                "ma60": [round(v, 2) if pd.notna(v) else None for v in ma60],
                "latest_price": round(float(df["close"].iloc[-1]), 2),
                "latest_change": round(float(df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100, 2) if len(df) >= 2 else 0,
            }

            # 尝试获取股票名称
            stock_name = symbol
            try:
                from data.live_fetcher import fetch_realtime_eastmoney
                quotes = fetch_realtime_eastmoney([symbol], market)
                if quotes and symbol in quotes:
                    stock_name = quotes[symbol].get("name", symbol)
            except Exception:
                pass
            result["name"] = stock_name

            return jsonify({"success": True, **result})

        except Exception as e:
            logger.exception("K线数据获取失败")
            return jsonify({"error": str(e)}), 500

    # ==============================================================
    # API: 分时K线数据（分钟级实时）
    # ==============================================================
    @app.route("/api/kline/intraday")
    def api_kline_intraday():
        """获取分时K线数据，支持1/5/15/30/60分钟级别"""
        try:
            from data.live_fetcher import fetch_intraday_kline_eastmoney, fetch_realtime_eastmoney

            symbol = request.args.get("symbol", "").strip().upper()
            market = request.args.get("market", "A_SHARE")
            freq = int(request.args.get("freq", 5))
            today_only = request.args.get("today", "true").lower() == "true"

            if not symbol:
                return jsonify({"error": "缺少symbol参数"}), 400

            if freq not in (1, 5, 15, 30, 60):
                freq = 5

            df = fetch_intraday_kline_eastmoney(symbol, market, freq)

            if df.empty:
                return jsonify({"error": f"无法获取 {symbol} 的分时数据"}), 404

            stock_name = df.attrs.get("name", symbol)
            pre_close = df.attrs.get("pre_close", 0)

            # 获取股票名称（如果 trends2 没有返回）
            if stock_name == symbol:
                try:
                    quotes = fetch_realtime_eastmoney([symbol], market)
                    if quotes and symbol in quotes:
                        stock_name = quotes[symbol].get("name", symbol)
                        if pre_close == 0:
                            pre_close = quotes[symbol].get("prev_close", 0)
                except Exception:
                    pass

            # 计算分时均价线（VWAP近似）
            cum_amount = (df["close"] * df["volume"]).cumsum()
            cum_volume = df["volume"].cumsum()
            vwap = (cum_amount / cum_volume.replace(0, 1)).round(2)

            dates = [d.strftime("%H:%M") if today_only else d.strftime("%m-%d %H:%M") for d in df.index]

            latest_price = round(float(df["close"].iloc[-1]), 2)
            change_pct = round((latest_price / pre_close - 1) * 100, 2) if pre_close > 0 else 0

            result = {
                "symbol": symbol,
                "name": stock_name,
                "market": market,
                "freq": freq,
                "dates": dates,
                "open": df["open"].round(2).tolist(),
                "high": df["high"].round(2).tolist(),
                "low": df["low"].round(2).tolist(),
                "close": df["close"].round(2).tolist(),
                "volume": df["volume"].tolist(),
                "amount": df["amount"].round(2).tolist(),
                "vwap": vwap.tolist(),
                "prev_close": pre_close,
                "latest_price": latest_price,
                "latest_change": change_pct,
                "count": len(df),
            }

            return jsonify({"success": True, **result})

        except Exception as e:
            logger.exception("分时K线数据获取失败")
            return jsonify({"error": str(e)}), 500

    # ==============================================================
    # API: 股票列表
    # ==============================================================
    @app.route("/api/stock_list")
    def api_stock_list():
        market = request.args.get("market", "US")
        fetcher = DataFetcher()
        df = fetcher.get_stock_list(market)
        return jsonify(df.head(50).to_dict(orient="records"))

    # ==============================================================
    # API: 股票搜索（支持代码/名称模糊搜索，A股+美股）
    # ==============================================================
    @app.route("/api/stock_search")
    def api_stock_search():
        """搜索股票，支持代码和名称模糊匹配（A股/美股）"""
        try:
            keyword = request.args.get("keyword", "").strip()
            market = request.args.get("market", "A_SHARE")

            if not keyword or len(keyword) < 1:
                return jsonify([])

            if market == "US":
                from data.live_fetcher import fetch_us_stock_list, fetch_realtime_eastmoney
                df = fetch_us_stock_list()
                if df.empty:
                    # 备用: 硬编码美股池 + 实时行情
                    symbols = list(DEFAULT_US_UNIVERSE)
                    quotes = fetch_realtime_eastmoney(symbols, market="US")
                    if quotes:
                        rows = []
                        for sym in symbols:
                            q = quotes.get(sym, {})
                            rows.append({
                                "symbol": sym,
                                "name": q.get("name", sym),
                                "price": q.get("price", 0),
                                "change_pct": q.get("change_pct", 0),
                                "volume": q.get("volume", 0),
                                "amount": q.get("amount", 0),
                            })
                        df = pd.DataFrame(rows)
            else:
                from data.live_fetcher import fetch_ashare_stock_list
                df = fetch_ashare_stock_list()

            if df.empty:
                return jsonify([])

            # 模糊匹配: 代码包含 或 名称包含
            kw_lower = keyword.lower()
            mask = (
                df["symbol"].str.contains(keyword, case=False, na=False)
                | df["name"].str.contains(kw_lower, case=False, na=False)
            )
            results = df[mask].head(20)

            return jsonify(results[["symbol", "name", "price", "change_pct", "volume", "amount"]].to_dict(orient="records"))

        except Exception as e:
            logger.warning(f"股票搜索失败: {e}")
            return jsonify({"error": str(e)}), 500

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(
        host=config.DASHBOARD_HOST,
        port=config.DASHBOARD_PORT,
        debug=config.DASHBOARD_DEBUG,
    )

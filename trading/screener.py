"""
股票自动筛选器 - 多因子打分排名选股

核心功能:
- 扫描股票池，计算多维度潜力因子
- 综合打分排名，选出最有潜力的N只
- 支持技术面、量价、趋势强度、波动率等维度
- 可独立使用，也可与 SmartTrader 配合
"""
import logging
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from data.fetcher import DataFetcher
import config

logger = logging.getLogger(__name__)


# ======================================================================
# 默认股票池
# ======================================================================
DEFAULT_US_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "JPM",
    "V", "JNJ", "WMT", "PG", "MA", "UNH", "HD", "BAC", "DIS", "NFLX",
    "AMD", "INTC", "CRM", "ADBE", "ORCL", "COST", "PEP", "KO",
    "MRK", "CSCO", "AVGO", "ACN", "TMO", "ABT", "NKE", "MCD",
    "LLY", "TXN", "QCOM", "LOW", "SBUX", "GS",
]

DEFAULT_ASHARE_UNIVERSE = [
    # 金融
    "000001", "600036", "601318", "601166", "600030", "002142", "601688",
    # 消费
    "600519", "000858", "000568", "002304", "600809", "000651", "000333",
    "600887", "002714", "603288",
    # 科技/电子
    "000725", "002415", "300059", "002230", "300750", "002049", "002371",
    # 医药
    "600276", "300760", "000538", "600196", "300122",
    # 新能源/材料
    "300274", "002459", "601012", "002466", "600905",
    # 工业/基建
    "600900", "601888", "600585", "000625", "600031",
    # 通信/互联网
    "601138", "000977", "300033",
    # 地产/周期
    "001979", "600048", "000002",
]


class StockScreener:
    """
    股票自动筛选器

    对股票池中的每只股票计算以下因子并综合打分:
    1. 动量强度 (20日涨幅，权重可配)
    2. 趋势方向 (价格在均线上方，斜率为正)
    3. 量价配合 (上涨放量、下跌缩量)
    4. 波动率适中 (不太高也不太低)
    5. RSI位置 (不在超买区，有上涨空间)
    6. 突破信号 (近期创新高或突破关键均线)
    """

    def __init__(self, params: dict = None):
        self.params = {
            "lookback": 20,            # 因子回看天数
            "top_n": 5,                # 选出前N只
            # 因子权重
            "w_momentum": 0.18,        # 动量
            "w_trend": 0.18,           # 趋势
            "w_volume": 0.14,          # 量价
            "w_volatility": 0.08,      # 波动率
            "w_rsi": 0.12,             # RSI
            "w_breakout": 0.18,        # 突破
            "w_mean_rev": 0.12,        # 均值回归
            # RSI参数
            "rsi_period": 14,
            "rsi_ideal_low": 40,       # RSI理想下限（有空间）
            "rsi_ideal_high": 65,      # RSI理想上限（未超买）
            # 随机化与多样性
            "random_jitter": 0.06,     # 随机扰动幅度（避免重复选股）
            **(params or {}),
        }
        self.fetcher = DataFetcher(cache_dir=config.DATA_DIR)

    # ------------------------------------------------------------------
    # 核心接口
    # ------------------------------------------------------------------
    def scan(
        self,
        universe: List[str] = None,
        market: str = "US",
        start_date: str = None,
        end_date: str = None,
        data: Dict[str, pd.DataFrame] = None,
        pick_history: Dict[str, int] = None,
    ) -> Tuple[List[dict], Dict[str, pd.DataFrame]]:
        """
        扫描股票池，返回排名结果

        pick_history: {symbol: 近N次被选中的次数} 用于多样性惩罚
        """
        if universe is None:
            if market == "US":
                # 美股: 动态获取股票列表
                try:
                    name_df = self.fetcher.get_stock_list("US")
                    if not name_df.empty:
                        universe = name_df["symbol"].tolist()
                        logger.info(f"使用动态美股全量股票池: {len(universe)}只")
                    else:
                        universe = DEFAULT_US_UNIVERSE
                        logger.warning("获取美股列表为空，使用默认股票池")
                except Exception as e:
                    universe = DEFAULT_US_UNIVERSE
                    logger.warning(f"获取美股列表失败，使用默认股票池: {e}")
            else:
                # A股: 动态获取全量股票列表
                try:
                    name_df = self.fetcher.get_stock_list("A_SHARE")
                    if not name_df.empty:
                        universe = name_df["symbol"].tolist()
                        logger.info(f"使用动态A股全量股票池: {len(universe)}只")
                    else:
                        universe = DEFAULT_ASHARE_UNIVERSE
                        logger.warning("获取A股列表为空，使用默认股票池")
                except Exception as e:
                    universe = DEFAULT_ASHARE_UNIVERSE
                    logger.warning(f"获取A股列表失败，使用默认股票池: {e}")

        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")

        # 获取数据
        loaded_data = data or {}
        if not loaded_data:
            for sym in universe:
                try:
                    df = self.fetcher.get_daily(sym, start_date, end_date, market)
                    if not df.empty and len(df) >= self.params["lookback"] + 10:
                        loaded_data[sym] = df
                except Exception as e:
                    logger.warning(f"获取 {sym} 失败: {e}")

        if not loaded_data:
            logger.warning("无可用数据")
            return [], {}

        # 逐只计算因子
        results = []
        for sym, df in loaded_data.items():
            factors = self._calc_factors(df)
            if factors is None:
                continue
            results.append((sym, factors, df))

        if not results:
            return [], loaded_data

        # 归一化 + 加权打分 + 多样性
        ranked = self._rank_stocks(results, pick_history or {})

        return ranked, loaded_data

    # ------------------------------------------------------------------
    # 因子计算
    # ------------------------------------------------------------------
    def _calc_factors(self, df: pd.DataFrame) -> Optional[dict]:
        """计算单只股票的全部因子（多时间维度）"""
        lb = self.params["lookback"]
        if len(df) < lb + 20:
            return None

        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        current_price = close.iloc[-1]

        # 1. 动量因子: 短期(20日) + 中期(60日) 综合
        momentum_short = close.iloc[-1] / close.iloc[-lb] - 1
        momentum_mid = close.iloc[-1] / close.iloc[-min(60, len(close)-1)] - 1 if len(close) > 30 else momentum_short
        # 短期动量权重高但中期提供趋势确认
        momentum = 0.6 * momentum_short + 0.4 * momentum_mid

        # 2. 趋势因子: MA方向 + 价格位置 + 多均线系统
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean() if len(close) >= 60 else ma20
        ma20_now = ma20.iloc[-1]
        ma20_prev = ma20.iloc[-5] if len(ma20) >= 5 else ma20.iloc[0]
        ma_slope = (ma20_now - ma20_prev) / ma20_prev
        above_ma20 = 1.0 if current_price > ma20_now else 0.0
        above_ma60 = 1.0 if current_price > ma60.iloc[-1] else 0.0
        # 均线多头排列加分 (MA20 > MA60)
        ma_bullish = 1.0 if ma20_now > ma60.iloc[-1] else 0.0
        trend_score = (0.3 * max(0, min(1, ma_slope * 50 + 0.5))
                       + 0.25 * above_ma20
                       + 0.25 * above_ma60
                       + 0.2 * ma_bullish)

        # 3. 量价因子: 上涨日平均量 vs 下跌日平均量 + 量能趋势
        returns = close.pct_change().tail(lb)
        vol_tail = volume.tail(lb)
        up_days = returns[returns > 0].index
        down_days = returns[returns < 0].index
        up_vol = vol_tail.loc[vol_tail.index.isin(up_days)].mean() if len(up_days) > 0 else 0
        down_vol = vol_tail.loc[vol_tail.index.isin(down_days)].mean() if len(down_days) > 0 else 1
        vol_ratio = min(up_vol / (down_vol + 1), 3.0) / 3.0
        # 量能趋势: 近5日均量 vs 前15日均量（放量上涨更有说服力）
        vol_recent = volume.tail(5).mean()
        vol_prior = volume.iloc[-20:-5].mean() if len(volume) >= 20 else volume.mean()
        vol_trend = min(vol_recent / (vol_prior + 1), 2.0) / 2.0
        volume_score = 0.6 * vol_ratio + 0.4 * vol_trend

        # 4. 波动率因子: 适中最佳（太高太低都扣分）
        daily_ret = close.pct_change().tail(lb)
        vol = daily_ret.std()
        ideal_vol = 0.02
        vol_score = max(0, 1 - abs(vol - ideal_vol) / ideal_vol)

        # 5. RSI因子: 40-65区间最佳（有上涨空间且未超买）
        rsi = self._calc_rsi(close, self.params["rsi_period"])
        rsi_now = rsi.iloc[-1] if not rsi.empty else 50
        ideal_low = self.params["rsi_ideal_low"]
        ideal_high = self.params["rsi_ideal_high"]
        if ideal_low <= rsi_now <= ideal_high:
            rsi_score = 1.0
        elif rsi_now < ideal_low:
            rsi_score = max(0, rsi_now / ideal_low)
        else:
            rsi_score = max(0, 1 - (rsi_now - ideal_high) / (100 - ideal_high))

        # 6. 突破因子: 接近N日新高 或 刚突破MA60
        high_n = high.tail(lb).max()
        near_high = current_price / high_n
        breakout_ma60 = 1.0 if (current_price > ma60.iloc[-1] and close.iloc[-2] <= ma60.iloc[-2]) else 0.0
        breakout_score = 0.6 * near_high + 0.4 * breakout_ma60

        # 7. 均值回归因子（新增）: 短期偏离度 — 偏离MA20过大的有回调风险
        deviation = (current_price - ma20_now) / ma20_now
        # 偏离在-5%~+5%之间得分最高（稳定区间），偏离过大扣分
        if abs(deviation) < 0.05:
            mean_rev_score = 1.0
        elif abs(deviation) < 0.10:
            mean_rev_score = 0.6
        else:
            mean_rev_score = 0.2

        return {
            "momentum": momentum,
            "trend": trend_score,
            "volume": volume_score,
            "volatility": vol_score,
            "rsi": rsi_score,
            "breakout": breakout_score,
            "mean_rev": mean_rev_score,
            # 原始值（供显示）
            "_price": current_price,
            "_rsi": rsi_now,
            "_momentum_pct": momentum_short,
            "_ma20": ma20_now,
        }

    def _calc_rsi(self, close: pd.Series, period: int) -> pd.Series:
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
        avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    # ------------------------------------------------------------------
    # 排名
    # ------------------------------------------------------------------
    def _rank_stocks(self, results: list, pick_history: Dict[str, int] = None) -> List[dict]:
        """归一化因子 + 加权打分 + 随机扰动 + 频率惩罚"""
        import random
        p = self.params
        pick_history = pick_history or {}

        # 提取原始因子矩阵
        symbols = [r[0] for r in results]
        factor_names = ["momentum", "trend", "volume", "volatility", "rsi", "breakout", "mean_rev"]
        raw = {sym: {f: factors[f] for f in factor_names} for sym, factors, _ in results}

        # 逐因子归一化到 0-1
        normalized = {sym: {} for sym in symbols}
        for f in factor_names:
            vals = [raw[sym][f] for sym in symbols]
            vmin, vmax = min(vals), max(vals)
            rng = vmax - vmin if vmax > vmin else 1
            for sym in symbols:
                normalized[sym][f] = (raw[sym][f] - vmin) / rng

        # 加权综合得分
        weights = {
            "momentum": p["w_momentum"],
            "trend": p["w_trend"],
            "volume": p["w_volume"],
            "volatility": p["w_volatility"],
            "rsi": p["w_rsi"],
            "breakout": p["w_breakout"],
            "mean_rev": p["w_mean_rev"],
        }

        jitter = p.get("random_jitter", 0.06)
        ranked = []
        for sym, factors, df in results:
            score = sum(normalized[sym][f] * weights[f] for f in factor_names)

            # 随机扰动：打破评分接近时的固定排名
            score += random.uniform(-jitter, jitter)

            # 频率惩罚：近期被多次选中的股票降低得分
            pick_count = pick_history.get(sym, 0)
            if pick_count > 0:
                score -= 0.03 * pick_count  # 每多选中一次扣0.03分

            score = max(0, min(1, score))  # 限制在0-1

            raw_f = factors
            # 生成信号原因
            reasons = []
            if raw_f["_momentum_pct"] > 0.05:
                reasons.append(f"动量强劲({raw_f['_momentum_pct']:.1%})")
            if raw_f["_price"] > raw_f["_ma20"]:
                reasons.append("站上MA20")
            if raw_f["_rsi"] < 65 and raw_f["_rsi"] > 40:
                reasons.append(f"RSI健康({raw_f['_rsi']:.0f})")
            if normalized[sym]["breakout"] > 0.7:
                reasons.append("接近新高")
            if normalized[sym]["mean_rev"] > 0.8:
                reasons.append("稳定区间")

            ranked.append({
                "symbol": sym,
                "score": round(score, 4),
                "factors": {f: round(normalized[sym][f], 3) for f in factor_names},
                "price": round(raw_f["_price"], 2),
                "rsi": round(raw_f["_rsi"], 1),
                "momentum_pct": f"{raw_f['_momentum_pct']:.2%}",
                "signal_reason": "；".join(reasons) if reasons else "综合评分",
            })

        # 按得分降序排列
        ranked.sort(key=lambda x: x["score"], reverse=True)

        return ranked[:p["top_n"]] + ranked[p["top_n"]:]

    def get_top_picks(self, **kwargs) -> List[dict]:
        """快捷方法：获取推荐股票"""
        ranked, _ = self.scan(**kwargs)
        return ranked[:self.params["top_n"]]

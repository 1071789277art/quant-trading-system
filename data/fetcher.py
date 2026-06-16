"""
数据获取模块 - 统一接口，多数据源自动降级

数据源优先级:
  A股: 东方财富(curl直连) → AKShare → 模拟数据
  美股: 东方财富(curl直连) → yfinance → 模拟数据
"""
import os
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List

import pandas as pd

logger = logging.getLogger(__name__)


class DataFetcher:
    """统一数据获取接口，支持A股和美股"""

    def __init__(self, cache_dir: Optional[str] = None):
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
        self.cache_dir = cache_dir

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------
    def get_daily(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        market: str = "US",
        use_cache: bool = True,
        fallback_to_sample: bool = True,
    ) -> pd.DataFrame:
        """
        获取日线数据，返回统一格式的 DataFrame

        参数:
            symbol: 股票代码（A股如 '000001', 美股如 'AAPL'）
            start_date: 开始日期 'YYYY-MM-DD'
            end_date: 结束日期 'YYYY-MM-DD'
            market: 'A_SHARE' 或 'US'
            use_cache: 是否使用本地缓存
            fallback_to_sample: API不可用时是否降级为模拟数据

        返回:
            DataFrame，columns=[open, high, low, close, volume, amount, turnover]
            索引为 date（datetime）
        """
        symbol = symbol.upper().strip()
        cache_key = f"{market}_{symbol}_{start_date}_{end_date}"

        # 尝试读取缓存
        if use_cache and self.cache_dir:
            cached = self._read_cache(cache_key)
            if cached is not None:
                logger.info(f"从缓存加载: {symbol}")
                return cached

        # 数据源1: 东方财富直连（curl，绕过SSL问题）
        df = self._fetch_live(symbol, start_date, end_date, market)

        # 数据源2: AKShare / yfinance（备用）
        if df.empty:
            if market == "A_SHARE":
                df = self._fetch_ashare(symbol, start_date, end_date)
            else:
                df = self._fetch_us_stock(symbol, start_date, end_date)

        # 保存缓存
        if use_cache and self.cache_dir and not df.empty:
            self._write_cache(cache_key, df)

        # 数据源3: 模拟数据（最终保底）
        if df.empty and fallback_to_sample:
            logger.warning(f"所有实时数据源不可用，使用模拟数据: {symbol}")
            from utils.sample_data import generate_sample_data
            seed = hash(symbol) % 10000
            df = generate_sample_data(symbol, start_date, end_date, seed=seed)

        return df

    def get_realtime(self, symbols: List[str], market: str = "A_SHARE") -> Dict[str, dict]:
        """
        获取实时行情报价

        返回: {symbol: {price, change_pct, open, high, low, volume, name, ...}}
        """
        try:
            from data.live_fetcher import fetch_realtime_quote
            return fetch_realtime_quote(symbols, market)
        except Exception as e:
            logger.error(f"获取实时行情失败: {e}")
            return {}

    def get_stock_list(self, market: str = "US") -> pd.DataFrame:
        """获取股票列表"""
        if market == "A_SHARE":
            return self._get_ashare_list()
        elif market == "US":
            return self._get_us_stock_list()
        else:
            raise ValueError(f"不支持的市场类型: {market}")

    # ------------------------------------------------------------------
    # 数据源1: 东方财富直连（curl）
    # ------------------------------------------------------------------
    def _fetch_live(self, symbol: str, start_date: str, end_date: str, market: str) -> pd.DataFrame:
        """通过东方财富直连API获取K线（curl子进程，绕过SSL兼容性问题）"""
        try:
            from data.live_fetcher import fetch_daily_kline
            df = fetch_daily_kline(symbol, start_date, end_date, market)
            if not df.empty:
                logger.info(f"东方财富获取成功: {symbol} ({len(df)}条)")
            return df
        except Exception as e:
            logger.warning(f"东方财富获取失败 {symbol}: {e}")
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # 数据源2: AKShare（A股备用）
    # ------------------------------------------------------------------
    def _fetch_ashare(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """通过 AKShare 获取A股日线数据"""
        try:
            import akshare as ak
            sd = start_date.replace("-", "")
            ed = end_date.replace("-", "")
            df = ak.stock_zh_a_hist(
                symbol=symbol, period="daily",
                start_date=sd, end_date=ed, adjust="qfq",
            )
            if df.empty:
                return pd.DataFrame()
            df = df.rename(columns={
                "日期": "date", "开盘": "open", "最高": "high",
                "最低": "low", "收盘": "close", "成交量": "volume",
                "成交额": "amount", "换手率": "turnover",
            })
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
            for col in ["turnover", "amount"]:
                if col not in df.columns:
                    df[col] = 0.0
            return df[["open", "high", "low", "close", "volume", "amount", "turnover"]]
        except Exception as e:
            logger.warning(f"AKShare获取A股 {symbol} 失败: {e}")
            return pd.DataFrame()

    def _get_ashare_list(self) -> pd.DataFrame:
        """获取A股全量股票列表（优先东方财富curl，备用AKShare）"""
        # 优先: 东方财富API（通过curl，绕过SSL问题）
        try:
            from data.live_fetcher import fetch_ashare_stock_list
            df = fetch_ashare_stock_list()
            if not df.empty:
                return df
        except Exception as e:
            logger.warning(f"东方财富股票列表失败: {e}")

        # 备用: AKShare
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot_em()
            return df[["代码", "名称"]].rename(columns={"代码": "symbol", "名称": "name"})
        except Exception as e:
            logger.warning(f"获取A股列表失败: {e}")
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # 数据源2: yfinance（美股备用）
    # ------------------------------------------------------------------
    def _fetch_us_stock(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """通过 yfinance 获取美股日线数据"""
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            df = ticker.history(start=start_date, end=end_date)
            if df.empty:
                return pd.DataFrame()
            df = df.rename(columns={
                "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume",
            })
            df.index = pd.to_datetime(df.index)
            df.index.name = "date"
            df["amount"] = df["close"] * df["volume"]
            df["turnover"] = 0.0
            return df[["open", "high", "low", "close", "volume", "amount", "turnover"]]
        except Exception as e:
            logger.warning(f"yfinance获取美股 {symbol} 失败: {e}")
            return pd.DataFrame()

    def _get_us_stock_list(self) -> pd.DataFrame:
        """美股股票列表（优先东方财富动态获取，备用硬编码列表）"""
        # 优先: 东方财富API（动态获取NASDAQ+NYSE+AMEX）
        try:
            from data.live_fetcher import fetch_us_stock_list
            df = fetch_us_stock_list()
            if not df.empty:
                return df
        except Exception as e:
            logger.warning(f"东方财富美股列表失败: {e}")

        # 备用: 硬编码常用美股
        stocks = [
            ("AAPL", "Apple"), ("MSFT", "Microsoft"), ("GOOGL", "Alphabet"),
            ("AMZN", "Amazon"), ("NVDA", "NVIDIA"), ("META", "Meta"),
            ("TSLA", "Tesla"), ("BRK-B", "Berkshire Hathaway"),
            ("JPM", "JPMorgan Chase"), ("V", "Visa"),
            ("JNJ", "Johnson & Johnson"), ("WMT", "Walmart"),
            ("PG", "Procter & Gamble"), ("MA", "Mastercard"),
            ("UNH", "UnitedHealth"), ("HD", "Home Depot"),
            ("BAC", "Bank of America"), ("DIS", "Walt Disney"),
            ("NFLX", "Netflix"), ("AMD", "AMD"),
        ]
        return pd.DataFrame(stocks, columns=["symbol", "name"])

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------
    def _cache_path(self, key: str) -> str:
        return os.path.join(self.cache_dir, f"{key}.parquet")

    def _read_cache(self, key: str) -> Optional[pd.DataFrame]:
        path = self._cache_path(key)
        if os.path.exists(path):
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
            if datetime.now() - mtime < timedelta(hours=24):
                try:
                    return pd.read_parquet(path)
                except Exception:
                    pass
        return None

    def _write_cache(self, key: str, df: pd.DataFrame):
        try:
            df.to_parquet(self._cache_path(key))
        except Exception as e:
            logger.warning(f"写入缓存失败: {e}")

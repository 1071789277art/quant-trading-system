"""
实时行情数据获取模块

通过 curl 子进程发起 HTTPS 请求，绕过系统 Python 3.9 SSL 兼容性问题。
支持数据源:
- 东方财富 (K线 + 实时行情) - A股主力数据源
- 新浪财经 (实时行情备份)
- 腾讯财经 (K线备份)
"""
import json
import logging
import subprocess
import time
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# ======================================================================
# 股票代码转换
# ======================================================================
def _to_eastmoney_secid(symbol: str, market: str = "A_SHARE") -> str:
    """将股票代码转为东方财富 secid 格式"""
    symbol = symbol.strip()
    if market == "US":
        return f"105.{symbol}"  # 美股: 105.AAPL
    # A股: 沪市(6开头)=1, 深市(0/3开头)=0, 科创板(688)=1, 创业板(300)=0, 北交所(8/4)=0
    if symbol.startswith(("6", "688")):
        return f"1.{symbol}"
    return f"0.{symbol}"


def _to_sina_code(symbol: str, market: str = "A_SHARE") -> str:
    """将股票代码转为新浪格式"""
    symbol = symbol.strip()
    if market == "US":
        return f"gb_{symbol.lower()}"
    if symbol.startswith(("6", "688")):
        return f"sh{symbol}"
    return f"sz{symbol}"


def _to_tencent_code(symbol: str, market: str = "A_SHARE") -> str:
    """将股票代码转为腾讯格式"""
    symbol = symbol.strip()
    if market == "US":
        return f"us{symbol.upper()}"
    if symbol.startswith(("6", "688")):
        return f"sh{symbol}"
    return f"sz{symbol}"


# ======================================================================
# HTTP 请求（通过 curl 子进程）
# ======================================================================
def _curl_get(url: str, headers: dict = None, timeout: int = 15, encoding: str = "utf-8") -> Optional[str]:
    """用 curl 发起 GET 请求，返回响应文本"""
    cmd = ["curl", "-s", "-L", "--max-time", str(timeout),
           "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"]
    if headers:
        for k, v in headers.items():
            cmd.extend(["-H", f"{k}: {v}"])
    cmd.append(url)
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        if result.returncode == 0:
            return result.stdout.decode(encoding, errors="replace")
        else:
            logger.warning(f"curl 返回码 {result.returncode}: {url}")
    except subprocess.TimeoutExpired:
        logger.warning(f"curl 超时: {url}")
    except Exception as e:
        logger.warning(f"curl 异常: {e}")
    return None


# ======================================================================
# JSONP 解析辅助
# ======================================================================
def _strip_jsonp(text: str) -> str:
    """去掉 JSONP 回调包装: jQuery({...}) -> {...}"""
    text = text.strip()
    lp = text.find("(")
    rp = text.rfind(")")
    if lp > 0 and rp > lp:
        return text[lp + 1:rp]
    return text


def _curl_get_jsonp(url: str, timeout: int = 20, retries: int = 2) -> Optional[dict]:
    """通过 JSONP 方式请求东方财富接口，自动去包装并重试"""
    # 确保 URL 包含 cb=jQuery 参数
    if "cb=" not in url:
        url += "&cb=jQuery"
    for attempt in range(retries + 1):
        text = _curl_get(url, timeout=timeout)
        if not text:
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            return None
        try:
            raw = _strip_jsonp(text)
            return json.loads(raw)
        except json.JSONDecodeError:
            # 也许服务端直接返回了 JSON（非 JSONP）
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                if attempt < retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                return None
    return None


# ======================================================================
# A股全量股票列表
# ======================================================================
def _fetch_ashare_sina(max_pages: int = 60) -> pd.DataFrame:
    """通过新浪财经获取A股全量列表（分页, 按成交额降序）"""
    base_url = (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "Market_Center.getHQNodeData"
        "?sort=amount&asc=0&node=hs_a&symbol=&_s_r_a=sort"
    )
    all_rows = []
    for page in range(1, max_pages + 1):
        url = f"{base_url}&page={page}&num=100"
        text = _curl_get(url, timeout=15)
        if not text or not text.strip():
            break
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"新浪股票列表第{page}页解析失败")
            break
        if not data:
            break
        for item in data:
            code = item.get("code", "")
            name = item.get("name", "")
            price = item.get("trade", "0")
            volume = item.get("volume", 0)
            change_pct = item.get("changepercent", 0)
            amount = item.get("amount", 0)
            if not code or not name:
                continue
            if "ST" in name or "st" in name.lower():
                continue
            if "退市" in name or "退" in name:
                continue
            try:
                price = float(price)
                volume = int(volume)
                change_pct = float(change_pct) if change_pct else 0
                amount = float(amount) if amount else 0
            except (ValueError, TypeError):
                continue
            if price <= 0 or volume <= 0:
                continue
            all_rows.append({
                "symbol": code, "name": name,
                "price": price, "volume": volume,
                "change_pct": change_pct, "amount": amount,
            })
        if page % 10 == 0:
            logger.info(f"新浪分页: 第{page}页, 已获取{len(all_rows)}只")
        if len(data) < 100:
            break
        time.sleep(0.15)
    return pd.DataFrame(all_rows)


def _fetch_ashare_eastmoney(max_pages: int = 60) -> pd.DataFrame:
    """通过东方财富JSONP分页获取A股列表"""
    base_url = (
        "https://push2.eastmoney.com/api/qt/clist/get"
        "?pz=100&po=1&np=1&fltt=2&invt=2&cb=jQuery"
        "&fid=f6&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
        "&fields=f12,f14,f2,f5,f3,f6"
    )
    all_rows = []
    consecutive_fails = 0
    for page in range(1, max_pages + 1):
        url = f"{base_url}&pn={page}"
        data = _curl_get_jsonp(url, timeout=20, retries=1)
        if not data:
            consecutive_fails += 1
            if consecutive_fails >= 3:
                break
            time.sleep(1.0)
            continue
        consecutive_fails = 0
        items = data.get("data", {}).get("diff", [])
        if not items:
            break
        for item in items:
            code = item.get("f12", "")
            name = item.get("f14", "")
            price = item.get("f2", 0)
            volume = item.get("f5", 0)
            change_pct = item.get("f3", 0)
            amount = item.get("f6", 0)
            if not code or not name:
                continue
            if "ST" in name or "st" in name.lower():
                continue
            if "退市" in name or "退" in name:
                continue
            if price in (None, 0, "-", "") or volume in (None, 0, "-", ""):
                continue
            try:
                price = float(price)
                volume = int(volume)
                change_pct = float(change_pct) if change_pct not in (None, "-", "") else 0
                amount = float(amount) if amount not in (None, "-", "") else 0
            except (ValueError, TypeError):
                continue
            all_rows.append({
                "symbol": code, "name": name,
                "price": price, "volume": volume,
                "change_pct": change_pct, "amount": amount,
            })
        if page % 10 == 0:
            logger.info(f"东方财富分页: 第{page}页, 已获取{len(all_rows)}只")
        if len(items) < 100:
            break
        time.sleep(0.2)
    return pd.DataFrame(all_rows)


# 模块级缓存（10分钟有效）
_ashare_list_cache = {"df": None, "ts": 0}
_ASHARE_LIST_TTL = 600  # 秒


def fetch_ashare_stock_list(min_price: float = 0, min_volume: int = 0, max_count: int = 0) -> pd.DataFrame:
    """
    获取A股全量股票列表
    数据源优先级: 新浪财经(稳定) → 东方财富(JSONP) → 空
    自动过滤: ST股、停牌股
    min_price: 最低价格过滤（元）
    min_volume: 最低成交量过滤（手）
    max_count: 最大返回数量（0=不限）
    返回列: symbol, name, price, volume, change_pct, amount
    """
    # 检查缓存（仅缓存原始未过滤数据）
    now = time.time()
    if _ashare_list_cache["df"] is not None and (now - _ashare_list_cache["ts"]) < _ASHARE_LIST_TTL:
        logger.info(f"使用A股列表缓存 ({len(_ashare_list_cache['df'])}只)")
        df = _ashare_list_cache["df"].copy()
    else:
        # 1) 尝试新浪
        df = _fetch_ashare_sina(max_pages=60)

        # 2) 新浪失败则尝试东方财富
        if df.empty or len(df) < 50:
            logger.info(f"新浪获取{len(df)}只，尝试东方财富备用")
            df2 = _fetch_ashare_eastmoney(max_pages=60)
            if len(df2) > len(df):
                df = df2

        if df.empty:
            logger.warning("所有数据源均未获取到A股列表")
            return pd.DataFrame()

        # 保存原始数据到缓存
        _ashare_list_cache["df"] = df.copy()
        _ashare_list_cache["ts"] = now

    # 过滤
    if min_price > 0:
        df = df[df["price"] >= min_price]
    if min_volume > 0:
        df = df[df["volume"] >= min_volume]

    # 按成交额排序（活跃股优先）
    df = df.sort_values("amount", ascending=False).reset_index(drop=True)

    if max_count > 0:
        df = df.head(max_count)

    logger.info(f"A股列表: {len(df)}只（已过滤ST和停牌）")
    return df


# ======================================================================
# 美股全量股票列表
# ======================================================================
def _fetch_us_stock_eastmoney(max_pages: int = 30) -> pd.DataFrame:
    """通过东方财富JSONP分页获取美股列表（NASDAQ+NYSE+AMEX）"""
    base_url = (
        "https://push2.eastmoney.com/api/qt/clist/get"
        "?pz=100&po=1&np=1&fltt=2&invt=2&cb=jQuery"
        "&fid=f6&fs=m:105,m:106,m:107"
        "&fields=f12,f14,f2,f5,f3,f6"
    )
    all_rows = []
    consecutive_fails = 0
    for page in range(1, max_pages + 1):
        url = f"{base_url}&pn={page}"
        data = _curl_get_jsonp(url, timeout=20, retries=1)
        if not data:
            consecutive_fails += 1
            if consecutive_fails >= 3:
                break
            time.sleep(1.0)
            continue
        consecutive_fails = 0
        items = data.get("data", {}).get("diff", [])
        if not items:
            break
        for item in items:
            code = item.get("f12", "")
            name = item.get("f14", "")
            price = item.get("f2", 0)
            volume = item.get("f5", 0)
            change_pct = item.get("f3", 0)
            amount = item.get("f6", 0)
            if not code or not name:
                continue
            # 过滤权证/单元/-rights等非普通股
            if any(kw in name for kw in ["Warrant", "Unit", "Rights", " warrants", " units"]):
                continue
            if price in (None, 0, "-", "") or volume in (None, 0, "-", ""):
                continue
            try:
                price = float(price)
                volume = int(volume)
                change_pct = float(change_pct) if change_pct not in (None, "-", "") else 0
                amount = float(amount) if amount not in (None, "-", "") else 0
            except (ValueError, TypeError):
                continue
            if price <= 0 or volume <= 0:
                continue
            all_rows.append({
                "symbol": code, "name": name,
                "price": price, "volume": volume,
                "change_pct": change_pct, "amount": amount,
            })
        if page % 10 == 0:
            logger.info(f"东方财富美股: 第{page}页, 已获取{len(all_rows)}只")
        if len(items) < 100:
            break
        time.sleep(0.2)
    return pd.DataFrame(all_rows)


# 模块级缓存（10分钟有效）
_us_list_cache = {"df": None, "ts": 0}
_US_LIST_TTL = 600  # 秒


def fetch_us_stock_list(min_price: float = 0, min_volume: int = 0, max_count: int = 0) -> pd.DataFrame:
    """
    获取美股全量股票列表
    数据源: 东方财富（NASDAQ+NYSE+AMEX）
    自动过滤: 权证、单元等非普通股
    min_price: 最低价格过滤（美元）
    min_volume: 最低成交量过滤
    max_count: 最大返回数量（0=不限）
    返回列: symbol, name, price, volume, change_pct, amount
    """
    now = time.time()
    if _us_list_cache["df"] is not None and (now - _us_list_cache["ts"]) < _US_LIST_TTL:
        logger.info(f"使用美股列表缓存 ({len(_us_list_cache['df'])}只)")
        df = _us_list_cache["df"].copy()
    else:
        df = _fetch_us_stock_eastmoney(max_pages=30)
        if df.empty:
            logger.warning("未能获取美股列表")
            return pd.DataFrame()
        _us_list_cache["df"] = df.copy()
        _us_list_cache["ts"] = now

    if min_price > 0:
        df = df[df["price"] >= min_price]
    if min_volume > 0:
        df = df[df["volume"] >= min_volume]

    df = df.sort_values("amount", ascending=False).reset_index(drop=True)

    if max_count > 0:
        df = df.head(max_count)

    logger.info(f"美股列表: {len(df)}只")
    return df


# ======================================================================
# 东方财富 K线数据
# ======================================================================
def fetch_kline_eastmoney(
    symbol: str,
    start_date: str,
    end_date: str,
    market: str = "A_SHARE",
) -> pd.DataFrame:
    """
    通过东方财富获取日K线数据

    参数:
        symbol: 股票代码 (如 '600519', '000001', 'AAPL')
        start_date: 'YYYY-MM-DD'
        end_date: 'YYYY-MM-DD'
        market: 'A_SHARE' 或 'US'

    返回:
        DataFrame, columns=[open, high, low, close, volume, amount, turnover]
        index=date (datetime)
    """
    secid = _to_eastmoney_secid(symbol, market)
    beg = start_date.replace("-", "")
    end = end_date.replace("-", "")

    url = (
        f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}"
        f"&fields1=f1,f2,f3,f4,f5,f6"
        f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
        f"&klt=101"   # 日K
        f"&fqt=1"     # 前复权
        f"&beg={beg}&end={end}"
        f"&lmt=2000"  # 最多2000条
    )

    text = _curl_get(url)
    if not text:
        return pd.DataFrame()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(f"东方财富返回非JSON: {symbol}")
        return pd.DataFrame()

    if not data.get("data") or not data["data"].get("klines"):
        logger.warning(f"东方财富无K线数据: {symbol}")
        return pd.DataFrame()

    klines = data["data"]["klines"]
    rows = []
    for kline in klines:
        parts = kline.split(",")
        if len(parts) >= 11:
            rows.append({
                "date": parts[0],
                "open": float(parts[1]),
                "close": float(parts[2]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "volume": float(parts[5]),
                "amount": float(parts[6]),
                "turnover": float(parts[10]) if parts[10] else 0.0,
            })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    return df[["open", "high", "low", "close", "volume", "amount", "turnover"]]


# ======================================================================
# 东方财富 分时K线（实时分钟级）
# ======================================================================
def fetch_intraday_kline_eastmoney(
    symbol: str,
    market: str = "A_SHARE",
    freq: int = 5,
) -> pd.DataFrame:
    """
    获取分时K线数据（当天分钟级数据，自动聚合为指定频率）

    参数:
        symbol: 股票代码
        market: 'A_SHARE' 或 'US'
        freq: K线周期，1=1分钟, 5=5分钟, 15=15分钟, 30=30分钟, 60=60分钟

    返回:
        DataFrame, columns=[open, high, low, close, volume, amount]
        index=datetime (datetime)
    """
    secid = _to_eastmoney_secid(symbol, market)

    # 使用 trends2 端点获取分钟级实时数据（比 kline 端点更稳定）
    url = (
        f"https://push2his.eastmoney.com/api/qt/stock/trends2/get"
        f"?secid={secid}"
        f"&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
        f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
        f"&iscr=0"
        f"&ndays=1"
    )

    text = _curl_get(url)
    if not text:
        return pd.DataFrame()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(f"东方财富分时数据返回非JSON: {symbol}")
        return pd.DataFrame()

    if not data.get("data") or not data["data"].get("trends"):
        logger.warning(f"东方财富分时数据为空: {symbol}")
        return pd.DataFrame()

    stock_name = data["data"].get("name", symbol)
    pre_close = data["data"].get("preClose", 0)

    # 解析分钟数据
    trends = data["data"]["trends"]
    rows = []
    for trend in trends:
        parts = trend.split(",")
        if len(parts) >= 6:
            rows.append({
                "datetime": parts[0],
                "close": float(parts[1]),     # 当前价（该分钟收盘价）
                "avg_price": float(parts[2]),  # 均价
                "high": float(parts[3]),       # 该分钟最高
                "low": float(parts[4]),        # 该分钟最低
                "volume": float(parts[5]),     # 该分钟成交量
                "amount": float(parts[6]) if len(parts) > 6 else 0,  # 累计成交额
            })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()

    # 如果 freq == 1，直接返回1分钟K线
    if freq == 1:
        result = pd.DataFrame({
            "open": df["close"],       # 1分钟: close即为该分钟的close
            "high": df["high"],
            "low": df["low"],
            "close": df["close"],
            "volume": df["volume"],
            "amount": df["amount"],
        })
        result["turnover"] = 0.0
        result.attrs["name"] = stock_name
        result.attrs["pre_close"] = pre_close
        return result[["open", "high", "low", "close", "volume", "amount", "turnover"]]

    # 聚合为 freq 分钟K线
    df_agg = df.resample(f"{freq}min").agg({
        "close": ["first", "max", "min", "last"],   # open, high, low, close
        "volume": "sum",
        "amount": "last",                            # 取最后一个累计值
    }).dropna(subset=[("close", "first")])

    result = pd.DataFrame({
        "open": df_agg[("close", "first")],
        "high": df_agg[("close", "max")],
        "low": df_agg[("close", "min")],
        "close": df_agg[("close", "last")],
        "volume": df_agg[("volume", "sum")],
        "amount": df_agg[("amount", "last")],
    })
    result["turnover"] = 0.0
    result.attrs["name"] = stock_name
    result.attrs["pre_close"] = pre_close

    return result[["open", "high", "low", "close", "volume", "amount", "turnover"]]


# ======================================================================
# 东方财富 实时行情（批量）
# ======================================================================
def fetch_realtime_eastmoney(
    symbols: List[str],
    market: str = "A_SHARE",
) -> Dict[str, dict]:
    """
    批量获取实时行情

    返回: {symbol: {price, change_pct, open, high, low, volume, amount, prev_close}}
    """
    secids = ",".join(_to_eastmoney_secid(s, market) for s in symbols)

    url = (
        f"https://push2.eastmoney.com/api/qt/ulist.np/get"
        f"?secids={secids}"
        f"&fltt=2"
        f"&fields=f2,f3,f4,f5,f6,f7,f12,f14,f15,f16,f17,f18"
    )

    text = _curl_get(url)
    if not text:
        return {}

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}

    result = {}
    diff = data.get("data", {}).get("diff", [])
    if isinstance(diff, list):
        items = diff
    elif isinstance(diff, dict):
        items = list(diff.values())
    else:
        return {}

    for item in items:
        code = str(item.get("f12", ""))
        if code in symbols:
            result[code] = {
                "price": float(item.get("f2", 0)) if item.get("f2") not in (None, "-", "") else 0,
                "change_pct": float(item.get("f3", 0)) if item.get("f3") not in (None, "-", "") else 0,
                "change": float(item.get("f4", 0)) if item.get("f4") not in (None, "-", "") else 0,
                "volume": item.get("f5", 0),
                "amount": item.get("f6", 0),
                "amplitude": float(item.get("f7", 0)) if item.get("f7") not in (None, "-", "") else 0,
                "name": item.get("f14", ""),
                "high": float(item.get("f15", 0)) if item.get("f15") not in (None, "-", "") else 0,
                "low": float(item.get("f16", 0)) if item.get("f16") not in (None, "-", "") else 0,
                "open": float(item.get("f17", 0)) if item.get("f17") not in (None, "-", "") else 0,
                "prev_close": float(item.get("f18", 0)) if item.get("f18") not in (None, "-", "") else 0,
            }

    return result


# ======================================================================
# 新浪财经 实时行情（备份）
# ======================================================================
def fetch_realtime_sina(
    symbols: List[str],
    market: str = "A_SHARE",
) -> Dict[str, dict]:
    """通过新浪获取实时行情"""
    codes = ",".join(_to_sina_code(s, market) for s in symbols)
    url = f"https://hq.sinajs.cn/list={codes}"

    text = _curl_get(url, headers={"Referer": "https://finance.sina.com.cn"}, encoding="gbk")
    if not text:
        return {}

    result = {}
    for line in text.strip().split("\n"):
        if '="' not in line:
            continue
        try:
            code_part = line.split("=")[0].split("_")
            data_str = line.split('="')[1].rstrip('";')
            parts = data_str.split(",")

            if market == "US" and len(parts) >= 12:
                # 新浪美股格式: name,price,change_pct,datetime,change,open,high,low,52w_high,52w_low,volume,...,market_cap
                # 代码格式: gb_aapl -> AAPL
                raw_code = code_part[-1].upper() if code_part[-1].startswith("gb") else code_part[-1].upper()
                result[raw_code] = {
                    "name": parts[0],
                    "price": float(parts[1]) if parts[1] else 0,
                    "change_pct": float(parts[2]) if parts[2] else 0,
                    "change": float(parts[4]) if len(parts) > 4 and parts[4] else 0,
                    "open": float(parts[5]) if len(parts) > 5 and parts[5] else 0,
                    "high": float(parts[6]) if len(parts) > 6 and parts[6] else 0,
                    "low": float(parts[7]) if len(parts) > 7 and parts[7] else 0,
                    "volume": float(parts[10]) if len(parts) > 10 and parts[10] else 0,
                    "amount": float(parts[12]) if len(parts) > 12 and parts[12] else 0,
                }
            elif len(parts) >= 32:
                # A股格式
                raw_code = code_part[-1][2:] if len(code_part[-1]) > 2 else code_part[-1]
                result[raw_code] = {
                    "name": parts[0],
                    "open": float(parts[1]) if parts[1] else 0,
                    "prev_close": float(parts[2]) if parts[2] else 0,
                    "price": float(parts[3]) if parts[3] else 0,
                    "high": float(parts[4]) if parts[4] else 0,
                    "low": float(parts[5]) if parts[5] else 0,
                    "volume": float(parts[8]) if parts[8] else 0,
                    "amount": float(parts[9]) if parts[9] else 0,
                }
        except (IndexError, ValueError):
            continue

    return result


# ======================================================================
# 腾讯财经 K线（备份）
# ======================================================================
def fetch_kline_tencent(
    symbol: str,
    start_date: str,
    end_date: str,
    market: str = "A_SHARE",
) -> pd.DataFrame:
    """通过腾讯获取日K线"""
    code = _to_tencent_code(symbol, market)
    url = (
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={code},day,{start_date},{end_date},300,qfq"
    )

    text = _curl_get(url)
    if not text:
        return pd.DataFrame()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return pd.DataFrame()

    # 腾讯返回格式: data -> {code} -> day/qfqday
    stock_data = data.get("data", {}).get(code, {})
    klines = stock_data.get("qfqday") or stock_data.get("day", [])

    if not klines:
        return pd.DataFrame()

    rows = []
    for k in klines:
        if len(k) >= 6:
            rows.append({
                "date": k[0],
                "open": float(k[1]),
                "close": float(k[2]),
                "high": float(k[3]),
                "low": float(k[4]),
                "volume": float(k[5]) if len(k) > 5 else 0,
            })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df["amount"] = df["close"] * df["volume"]
    df["turnover"] = 0.0

    return df[["open", "high", "low", "close", "volume", "amount", "turnover"]]


# ======================================================================
# 统一接口
# ======================================================================
def fetch_daily_kline(
    symbol: str,
    start_date: str,
    end_date: str,
    market: str = "A_SHARE",
) -> pd.DataFrame:
    """
    获取日K线（自动尝试多个数据源）

    优先级: 东方财富 → 腾讯 → 空DataFrame
    """
    # 1. 东方财富
    df = fetch_kline_eastmoney(symbol, start_date, end_date, market)
    if not df.empty:
        return df

    # 2. 腾讯备份（A股和美股均支持）
    df = fetch_kline_tencent(symbol, start_date, end_date, market)
    if not df.empty:
        return df

    logger.warning(f"所有数据源均无法获取 {symbol} 的K线数据")
    return pd.DataFrame()


def fetch_realtime_quote(
    symbols: List[str],
    market: str = "A_SHARE",
) -> Dict[str, dict]:
    """
    获取实时行情（自动尝试多个数据源）

    优先级: 东方财富 → 新浪 → 空字典
    """
    result = fetch_realtime_eastmoney(symbols, market)
    if result:
        return result

    # 新浪备份（A股和美股均支持）
    result = fetch_realtime_sina(symbols, market)
    if result:
        return result

    return {}

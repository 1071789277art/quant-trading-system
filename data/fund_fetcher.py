"""
基金数据获取模块

通过东方财富(天天基金)API获取基金市场数据：
- 基金市场大盘（规模趋势、类型分布、公司排名）
- 基金实时动态（实时估值、涨跌排行、净值历史）
- 智能选基（多维筛选、基金详情、净值走势）
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# 复用 live_fetcher 的底层工具
from data.live_fetcher import _curl_get, _strip_jsonp

# ======================================================================
# 缓存
# ======================================================================
_fund_ranking_cache = {"data": None, "ts": 0, "key": ""}
_FUND_RANKING_TTL = 60  # 排行缓存60秒

_fund_company_cache = {"data": None, "ts": 0}
_FUND_COMPANY_TTL = 600  # 公司排名缓存10分钟

_fund_overview_cache = {"data": None, "ts": 0}
_FUND_OVERVIEW_TTL = 600  # 大盘数据缓存10分钟

_fund_detail_cache: Dict[str, dict] = {}
_FUND_DETAIL_TTL = 300  # 基金详情缓存5分钟


def _strip_js_var(text: str, var_name: str = None) -> Optional[str]:
    """从 JS 变量赋值中提取 JSON 部分: var xxx={...} -> {...}"""
    if not text:
        return None
    text = text.strip()
    # 尝试 var xxx = {...} 或 xxx={...}
    eq_pos = text.find("=")
    if eq_pos > 0:
        return text[eq_pos + 1:].strip().rstrip(";")
    # 尝试 jsonpgz({...})
    lp = text.find("(")
    rp = text.rfind(")")
    if lp > 0 and rp > lp:
        return text[lp + 1:rp]
    return text


# ======================================================================
# 基金市场大盘
# ======================================================================
def fetch_fund_market_overview() -> dict:
    """获取基金市场整体规模趋势"""
    now = time.time()
    if _fund_overview_cache["data"] and (now - _fund_overview_cache["ts"]) < _FUND_OVERVIEW_TTL:
        return _fund_overview_cache["data"]

    result = {"scale_trend": {"dates": [], "values": []}, "type_stats": []}

    # 1. 全市场规模趋势
    try:
        url = "https://fund.eastmoney.com/Company/home/GetFundTotalScaleForChart"
        text = _curl_get(url, headers={"Referer": "https://fund.eastmoney.com/"}, timeout=15)
        if text:
            data = json.loads(text)
            if isinstance(data, dict):
                result["scale_trend"]["dates"] = data.get("x", [])
                result["scale_trend"]["values"] = data.get("y", [])
    except Exception as e:
        logger.warning(f"获取基金市场规模趋势失败: {e}")

    # 2. 各类型基金统计（从API元数据获取）
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        one_year_ago = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        url = (
            f"https://fund.eastmoney.com/data/rankhandler.aspx"
            f"?op=ph&dt=kf&ft=all&rs=&gs=0"
            f"&sc=rzdf&st=desc"
            f"&sd={one_year_ago}&ed={today}"
            f"&qdii=&tabSubtype=,,,,,,"
            f"&pi=1&pn=1&dx=1"
            f"&v={time.time()}"
        )
        text = _curl_get(url, headers={"Referer": "https://fund.eastmoney.com/fundguzhi.html"}, timeout=15)
        if text:
            count_map = {
                "gp": ("股票型", "gp_count"),
                "hh": ("混合型", "hh_count"),
                "zq": ("债券型", "zq_count"),
                "zs": ("指数型", "zs_count"),
                "qdii": ("QDII", "qdii_count"),
                "fof": ("FOF", "fof_count"),
            }
            for ft_key, (name, field) in count_map.items():
                m = re.search(rf'{field}:(\d+)', text)
                if m:
                    result["type_stats"].append({"type": name, "count": int(m.group(1))})
    except Exception as e:
        logger.warning(f"获取基金类型统计失败: {e}")

    _fund_overview_cache["data"] = result
    _fund_overview_cache["ts"] = now
    return result


def fetch_fund_company_ranking(top: int = 20) -> list:
    """获取头部基金公司排名"""
    now = time.time()
    if _fund_company_cache["data"] and (now - _fund_company_cache["ts"]) < _FUND_COMPANY_TTL:
        return _fund_company_cache["data"][:top]

    companies = []
    try:
        url = "https://fund.eastmoney.com/Company/home/gspmlist"
        text = _curl_get(url, headers={"Referer": "https://fund.eastmoney.com/"}, timeout=15)
        if text:
            # 解析HTML表格
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', text, re.DOTALL)
            for row in rows:
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                if len(cells) >= 7:
                    name_match = re.search(r'>([^<]+)</a>', cells[1])
                    name = name_match.group(1).strip() if name_match else re.sub(r'<[^>]+>', '', cells[1]).strip()
                    # 规模在 cells[5]，单位亿元，可能带日期后缀如 "23,480.62&nbsp;&nbsp; 06-16"
                    scale_text = re.sub(r'<[^>]+>', '', cells[5]).strip()
                    scale_text = re.split(r'[&\s]', scale_text)[0].replace(',', '')
                    count_text = re.sub(r'<[^>]+>', '', cells[6]).strip().replace(',', '')
                    try:
                        scale = float(scale_text) if scale_text else 0
                        count = int(count_text) if count_text else 0
                        if scale > 0:
                            companies.append({"name": name, "aum": scale, "fund_count": count})
                    except (ValueError, TypeError):
                        continue
    except Exception as e:
        logger.warning(f"获取基金公司排名失败: {e}")

    # 按规模排序
    companies.sort(key=lambda x: x["aum"], reverse=True)
    _fund_company_cache["data"] = companies
    _fund_company_cache["ts"] = now
    return companies[:top]


# ======================================================================
# 基金实时动态
# ======================================================================
def fetch_fund_realtime_estimate(fund_code: str) -> dict:
    """获取单只基金实时估值"""
    fund_code = fund_code.strip()
    try:
        url = f"http://fundgz.1234567.com.cn/js/{fund_code}.js?rt={int(time.time()*1000)}"
        text = _curl_get(url, timeout=10)
        if text:
            raw = _strip_js_var(text, "jsonpgz")
            data = json.loads(raw)
            return {
                "code": data.get("fundcode", fund_code),
                "name": data.get("name", ""),
                "nav_date": data.get("jzrq", ""),
                "nav": _safe_float(data.get("dwjz", 0)),
                "estimated_nav": _safe_float(data.get("gsz", 0)),
                "estimated_change": _safe_float(data.get("gszzl", 0)),
                "update_time": data.get("gztime", ""),
            }
    except Exception as e:
        logger.warning(f"获取基金实时估值失败 [{fund_code}]: {e}")
    return {"code": fund_code, "name": "未知", "nav": 0, "estimated_nav": 0, "estimated_change": 0, "update_time": ""}


def fetch_fund_realtime_batch(fund_codes: list) -> list:
    """批量获取多只基金实时估值（并行请求）"""
    import subprocess as _sp

    def _fetch_one(code):
        """用 curl 并行请求单只基金估值"""
        code = code.strip()
        url = f"http://fundgz.1234567.com.cn/js/{code}.js?rt={int(time.time()*1000)}"
        try:
            cmd = ["curl", "-s", "-L", "--max-time", "8",
                   "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                   url]
            result = _sp.run(cmd, capture_output=True, timeout=12)
            if result.returncode == 0:
                text = result.stdout.decode("utf-8", errors="replace").strip()
                if text:
                    raw = _strip_js_var(text, "jsonpgz")
                    data = json.loads(raw)
                    return {
                        "code": data.get("fundcode", code),
                        "name": data.get("name", ""),
                        "nav": _safe_float(data.get("dwjz", 0)),
                        "estimated_nav": _safe_float(data.get("gsz", 0)),
                        "estimated_change": _safe_float(data.get("gszzl", 0)),
                        "update_time": data.get("gztime", ""),
                    }
        except Exception:
            pass
        return {"code": code, "name": "--", "nav": 0, "estimated_nav": 0, "estimated_change": 0, "update_time": ""}

    # 限制最多50只
    codes = fund_codes[:50]
    results = []
    # 逐个请求（curl本身很快，串行也够快）
    for code in codes:
        results.append(_fetch_one(code))

    # 按估算涨跌排序
    results.sort(key=lambda x: abs(x.get("estimated_change", 0)), reverse=True)
    return results


def fetch_fund_ranking_today(top: int = 50, fund_type: str = "all", sort_by: str = "rzdf") -> list:
    """获取基金今日涨跌排行"""
    cache_key = f"{fund_type}_{sort_by}_{top}"
    now = time.time()
    if (_fund_ranking_cache["data"] and _fund_ranking_cache["key"] == cache_key
            and (now - _fund_ranking_cache["ts"]) < _FUND_RANKING_TTL):
        return _fund_ranking_cache["data"]

    funds = _fetch_ranking_raw(fund_type, sort_by, top)
    _fund_ranking_cache["data"] = funds
    _fund_ranking_cache["key"] = cache_key
    _fund_ranking_cache["ts"] = now
    return funds


def _fetch_ranking_raw(fund_type: str = "all", sort_by: str = "rzdf", top: int = 50) -> list:
    """从东方财富获取基金排行原始数据"""
    today = datetime.now().strftime("%Y-%m-%d")
    one_year_ago = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    url = (
        f"https://fund.eastmoney.com/data/rankhandler.aspx"
        f"?op=ph&dt=kf&ft={fund_type}&rs=&gs=0"
        f"&sc={sort_by}&st=desc"
        f"&sd={one_year_ago}&ed={today}"
        f"&qdii=&tabSubtype=,,,,,,"
        f"&pi=1&pn={min(top, 30000)}&dx=1"
        f"&v={time.time()}"
    )
    text = _curl_get(url, headers={"Referer": "https://fund.eastmoney.com/fundguzhi.html"}, timeout=20)
    if not text:
        return []

    # 解析返回的 datas 数组
    funds = []
    try:
        # 格式: var rankData = {datas:[...],allRecords:N,...}
        match = re.search(r'datas:\[(.*?)\],allRecords', text, re.DOTALL)
        if not match:
            return []
        items_str = match.group(1)
        # 每项用引号包裹，逗号分隔
        items = re.findall(r'"([^"]*)"', items_str)
        for item in items:
            fields = item.split(",")
            if len(fields) < 25:
                continue
            fund = {
                "code": fields[0],
                "name": fields[1],
                "pinyin": fields[2],
                "date": fields[3] if len(fields) > 3 else "",
                "nav": _safe_float(fields[4]),
                "cumulative_nav": _safe_float(fields[5]),
                "daily_change": _safe_float(fields[6]),
                "1w": _safe_float(fields[7]),
                "1m": _safe_float(fields[8]),
                "3m": _safe_float(fields[9]),
                "6m": _safe_float(fields[10]),
                "1y": _safe_float(fields[11]),
                "2y": _safe_float(fields[12]),
                "3y": _safe_float(fields[13]),
                "ytd": _safe_float(fields[14]),
                "inception": _safe_float(fields[15]),
                "inception_date": fields[16] if len(fields) > 16 else "",
                "max_drawdown": _safe_float(fields[24]) if len(fields) > 24 else 0,
            }
            funds.append(fund)
    except Exception as e:
        logger.warning(f"解析基金排行数据失败: {e}")
    return funds


def fetch_fund_nav_history(fund_code: str, days: int = 90) -> list:
    """获取基金历史净值（自动分页）"""
    fund_code = fund_code.strip()
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    nav_list = []
    page_size = 20  # 东方财富API每页最多返回20条
    page_idx = 1
    max_pages = 200  # 安全上限，防止死循环

    try:
        while page_idx <= max_pages:
            url = (
                f"https://api.fund.eastmoney.com/f10/lsjz"
                f"?fundCode={fund_code}&pageIndex={page_idx}&pageSize={page_size}"
                f"&startDate={start_date}&endDate={end_date}"
                f"&_={int(time.time()*1000)}"
            )
            text = _curl_get(url, headers={
                "Referer": f"https://fundf10.eastmoney.com/jjjz_{fund_code}.html"
            }, timeout=15)
            if not text:
                break
            data = json.loads(text)
            items = data.get("Data", {}).get("LSJZList", [])
            if not items:
                break
            for item in items:
                nav_list.append({
                    "date": item.get("FSRQ", ""),
                    "nav": _safe_float(item.get("DWJZ", 0)),
                    "cumulative_nav": _safe_float(item.get("LJJZ", 0)),
                    "daily_change": _safe_float(item.get("JZZZL", 0)),
                })
            total_count = data.get("TotalCount", 0)
            if page_idx * page_size >= total_count:
                break
            page_idx += 1
    except Exception as e:
        logger.warning(f"获取基金净值历史失败 [{fund_code}]: {e}")

    nav_list.sort(key=lambda x: x["date"])
    return nav_list


# ======================================================================
# 智能选基
# ======================================================================
def fetch_fund_screening(fund_type: str = "all", sort_by: str = "1nzf",
                          page: int = 1, size: int = 100) -> dict:
    """多维筛选基金"""
    funds = _fetch_ranking_raw(fund_type, sort_by, page * size)
    start = (page - 1) * size
    end = start + size
    return {
        "total": len(funds),
        "funds": funds[start:end],
    }


def fetch_fund_detail(fund_code: str) -> dict:
    """获取基金详情（含净值走势）"""
    fund_code = fund_code.strip()
    now = time.time()
    if fund_code in _fund_detail_cache:
        cached = _fund_detail_cache[fund_code]
        if (now - cached.get("_ts", 0)) < _FUND_DETAIL_TTL:
            return cached

    detail = {"code": fund_code, "name": "", "type": "", "nav_trend": []}
    try:
        url = f"https://fund.eastmoney.com/pingzhongdata/{fund_code}.js?v={int(time.time()*1000)}"
        text = _curl_get(url, headers={"Referer": "https://fund.eastmoney.com/"}, timeout=15)
        if text:
            # 解析 fS_name
            name_match = re.search(r'fS_name\s*=\s*"([^"]*)"', text)
            if name_match:
                detail["name"] = name_match.group(1)

            # 解析 Data_netWorthTrend (净值走势)
            trend_match = re.search(r'var\s+Data_netWorthTrend\s*=\s*(\[.*?\]);', text, re.DOTALL)
            if trend_match:
                try:
                    trend_data = json.loads(trend_match.group(1))
                    for item in trend_data[-180:]:  # 最近180个数据点
                        ts = item.get("x", 0)
                        nav = item.get("y", 0)
                        date_str = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") if ts else ""
                        detail["nav_trend"].append({"date": date_str, "nav": nav})
                except json.JSONDecodeError:
                    pass

            # 解析基金类型
            type_match = re.search(r'fS_code\s*=\s*"([^"]*)"', text)
            if type_match:
                detail["type"] = type_match.group(1)

    except Exception as e:
        logger.warning(f"获取基金详情失败 [{fund_code}]: {e}")

    detail["_ts"] = now
    _fund_detail_cache[fund_code] = detail
    return detail


def search_funds(keyword: str, fund_type: str = "all", top: int = 30) -> list:
    """搜索基金（按名称或代码）"""
    keyword = keyword.strip().lower()
    if not keyword:
        return []
    funds = _fetch_ranking_raw(fund_type, "rzdf", 5000)
    results = []
    for f in funds:
        if keyword in f["code"].lower() or keyword in f["name"].lower():
            results.append(f)
            if len(results) >= top:
                break
    return results


# ======================================================================
# 工具函数
# ======================================================================
def _safe_float(val, default=0.0) -> float:
    """安全转换为浮点数"""
    if val is None or val == "" or val == "---":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _safe_int(val, default=0) -> int:
    """安全转换为整数"""
    if val is None or val == "":
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default

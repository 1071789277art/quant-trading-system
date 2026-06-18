"""
用户持仓管理模块
- 股票持仓 + 基金持仓的增删改查
- JSON 文件持久化（原子写入 + 自动备份）
- 实时行情获取与收益计算
"""
import json
import os
import shutil
import tempfile
import time
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

STATE_DIR = os.path.join(os.path.dirname(__file__), "portfolio_state")
STATE_FILE = os.path.join(STATE_DIR, "portfolio.json")
STATE_BAK_FILE = STATE_FILE + ".bak"


class PortfolioManager:
    """用户持仓管理器"""

    def __init__(self):
        self.stocks = []  # [{symbol, name, quantity, avg_cost, market, notes, added_at}]
        self.funds = []   # [{code, name, shares, avg_nav, notes, added_at}]
        self.updated_at = ""
        self._load()

    # ==================== 持久化 ====================
    def _load(self):
        """从 JSON 文件加载持仓数据，带完整性校验"""
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if not content:
                logger.warning("持仓文件为空，尝试从备份恢复")
                self._load_from_backup()
                return
            data = json.loads(content)
            stocks = data.get("stocks", [])
            funds = data.get("funds", [])
            # 完整性校验：如果文件里有数据才覆盖内存
            if isinstance(stocks, list) and isinstance(funds, list):
                self.stocks = stocks
                self.funds = funds
                self.updated_at = data.get("updated_at", "")
                logger.info(f"加载持仓数据: {len(self.stocks)}只股票, {len(self.funds)}只基金")
            else:
                logger.warning("持仓文件格式异常，尝试从备份恢复")
                self._load_from_backup()
        except json.JSONDecodeError as e:
            logger.warning(f"持仓文件JSON解析失败，尝试从备份恢复: {e}")
            self._load_from_backup()
        except Exception as e:
            logger.warning(f"加载持仓数据失败: {e}")

    def _load_from_backup(self):
        """从备份文件恢复"""
        if not os.path.exists(STATE_BAK_FILE):
            logger.warning("无备份文件可恢复")
            return
        try:
            with open(STATE_BAK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            stocks = data.get("stocks", [])
            funds = data.get("funds", [])
            if isinstance(stocks, list) and isinstance(funds, list):
                self.stocks = stocks
                self.funds = funds
                self.updated_at = data.get("updated_at", "")
                logger.info(f"从备份恢复持仓数据: {len(self.stocks)}只股票, {len(self.funds)}只基金")
        except Exception as e:
            logger.error(f"从备份恢复失败: {e}")

    def save(self):
        """保存持仓数据（原子写入 + 自动备份）"""
        os.makedirs(STATE_DIR, exist_ok=True)
        self.updated_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        data = {
            "stocks": self.stocks,
            "funds": self.funds,
            "updated_at": self.updated_at,
            "stock_count": len(self.stocks),
            "fund_count": len(self.funds),
        }
        json_content = json.dumps(data, ensure_ascii=False, indent=2)
        try:
            # 1. 备份：当前文件 → .bak
            if os.path.exists(STATE_FILE) and os.path.getsize(STATE_FILE) > 10:
                shutil.copy2(STATE_FILE, STATE_BAK_FILE)

            # 2. 原子写入：先写临时文件，再重命名
            fd, tmp_path = tempfile.mkstemp(dir=STATE_DIR, suffix=".tmp", prefix="pf_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json_content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, STATE_FILE)  # 原子替换
            except Exception:
                # 清理临时文件
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

            logger.debug(f"持仓数据已保存: {len(self.stocks)}只股票, {len(self.funds)}只基金")
        except Exception as e:
            logger.error(f"保存持仓数据失败: {e}")

    # ==================== 股票 CRUD ====================
    def add_stock(self, symbol: str, name: str, quantity: float,
                  avg_cost: float, market: str = "A_SHARE", notes: str = "") -> dict:
        """添加股票持仓"""
        symbol = symbol.strip()
        # 检查是否已存在
        for s in self.stocks:
            if s["symbol"] == symbol and s.get("market", "A_SHARE") == market:
                return {"success": False, "error": f"已存在 {symbol}({market}) 持仓，请使用修改功能"}

        self.stocks.append({
            "symbol": symbol,
            "name": name.strip() or symbol,
            "quantity": float(quantity),
            "avg_cost": float(avg_cost),
            "market": market,
            "notes": notes.strip(),
            "added_at": datetime.now().strftime("%Y-%m-%d"),
        })
        self.save()
        return {"success": True, "message": f"已添加 {name or symbol}"}

    def remove_stock(self, symbol: str, market: str = "A_SHARE") -> dict:
        """删除股票持仓"""
        before = len(self.stocks)
        self.stocks = [s for s in self.stocks
                       if not (s["symbol"] == symbol and s.get("market", "A_SHARE") == market)]
        if len(self.stocks) < before:
            self.save()
            return {"success": True, "message": f"已删除 {symbol}"}
        return {"success": False, "error": f"未找到 {symbol}({market}) 持仓"}

    def update_stock(self, symbol: str, market: str = "A_SHARE", **kwargs) -> dict:
        """修改股票持仓"""
        for s in self.stocks:
            if s["symbol"] == symbol and s.get("market", "A_SHARE") == market:
                for key in ("name", "quantity", "avg_cost", "notes"):
                    if key in kwargs:
                        if key in ("quantity", "avg_cost"):
                            s[key] = float(kwargs[key])
                        else:
                            s[key] = str(kwargs[key]).strip()
                self.save()
                return {"success": True, "message": f"已更新 {symbol}"}
        return {"success": False, "error": f"未找到 {symbol}({market}) 持仓"}

    # ==================== 基金 CRUD ====================
    def add_fund(self, code: str, name: str, shares: float,
                 avg_nav: float, notes: str = "") -> dict:
        """添加基金持仓"""
        code = code.strip()
        for f in self.funds:
            if f["code"] == code:
                return {"success": False, "error": f"已存在 {code} 持仓，请使用修改功能"}

        self.funds.append({
            "code": code,
            "name": name.strip() or code,
            "shares": float(shares),
            "avg_nav": float(avg_nav),
            "notes": notes.strip(),
            "added_at": datetime.now().strftime("%Y-%m-%d"),
        })
        self.save()
        return {"success": True, "message": f"已添加 {name or code}"}

    def remove_fund(self, code: str) -> dict:
        """删除基金持仓"""
        before = len(self.funds)
        self.funds = [f for f in self.funds if f["code"] != code]
        if len(self.funds) < before:
            self.save()
            return {"success": True, "message": f"已删除 {code}"}
        return {"success": False, "error": f"未找到 {code} 持仓"}

    def update_fund(self, code: str, **kwargs) -> dict:
        """修改基金持仓"""
        for f in self.funds:
            if f["code"] == code:
                for key in ("name", "shares", "avg_nav", "notes"):
                    if key in kwargs:
                        if key in ("shares", "avg_nav"):
                            f[key] = float(kwargs[key])
                        else:
                            f[key] = str(kwargs[key]).strip()
                self.save()
                return {"success": True, "message": f"已更新 {code}"}
        return {"success": False, "error": f"未找到 {code} 持仓"}

    # ==================== 查询 ====================
    def get_all(self) -> dict:
        """获取全部持仓（不含实时行情）"""
        return {
            "stocks": list(self.stocks),
            "funds": list(self.funds),
            "updated_at": self.updated_at,
        }


# ======================================================================
# 实时行情 + 收益计算
# ======================================================================

# 模块级单例
_portfolio_mgr = None


def _get_portfolio() -> PortfolioManager:
    global _portfolio_mgr
    if _portfolio_mgr is None:
        _portfolio_mgr = PortfolioManager()
    return _portfolio_mgr


def _reload_portfolio():
    """强制重新加载持仓数据"""
    global _portfolio_mgr
    _portfolio_mgr = PortfolioManager()
    return _portfolio_mgr


def get_portfolio_with_realtime() -> dict:
    """
    获取完整持仓数据 + 实时行情 + 收益计算

    返回:
    {
        stocks: [{...holding, current_price, change_pct, today_pnl, float_pnl, float_pnl_pct}],
        funds:  [{...holding, estimated_nav, estimated_change, today_pnl, float_pnl, float_pnl_pct}],
        summary: {total_cost, total_value, total_pnl, total_pnl_pct, today_pnl, stock_value, fund_value},
        updated_at: str
    }
    """
    pm = _reload_portfolio()
    raw = pm.get_all()

    stock_results = []
    fund_results = []

    # ---------- 股票实时行情 ----------
    # 按市场分组
    market_groups = {}
    for s in raw["stocks"]:
        mkt = s.get("market", "A_SHARE")
        market_groups.setdefault(mkt, []).append(s)

    for market, holdings in market_groups.items():
        symbols = [h["symbol"] for h in holdings]
        try:
            from data.live_fetcher import fetch_realtime_quote
            quotes = fetch_realtime_quote(symbols, market)
        except Exception as e:
            logger.warning(f"获取股票实时行情失败 ({market}): {e}")
            quotes = {}

        for h in holdings:
            sym = h["symbol"]
            q = quotes.get(sym, {})
            current_price = q.get("price", 0)
            change_pct = q.get("change_pct", 0)
            prev_close = q.get("prev_close", 0)
            name = q.get("name", "") or h.get("name", sym)

            quantity = h["quantity"]
            avg_cost = h["avg_cost"]
            cost_total = quantity * avg_cost
            value_total = quantity * current_price if current_price > 0 else 0
            float_pnl = value_total - cost_total
            float_pnl_pct = (float_pnl / cost_total * 100) if cost_total > 0 else 0

            # 今日盈亏 = 数量 × (现价 - 昨收)
            today_pnl = quantity * (current_price - prev_close) if (current_price > 0 and prev_close > 0) else 0

            stock_results.append({
                "symbol": sym,
                "name": name,
                "quantity": quantity,
                "avg_cost": round(avg_cost, 4),
                "market": market,
                "notes": h.get("notes", ""),
                "added_at": h.get("added_at", ""),
                "current_price": round(current_price, 4),
                "prev_close": round(prev_close, 4),
                "change_pct": round(change_pct, 2),
                "high": q.get("high", 0),
                "low": q.get("low", 0),
                "open": q.get("open", 0),
                "volume": q.get("volume", 0),
                "amount": q.get("amount", 0),
                "cost_total": round(cost_total, 2),
                "value_total": round(value_total, 2),
                "float_pnl": round(float_pnl, 2),
                "float_pnl_pct": round(float_pnl_pct, 2),
                "today_pnl": round(today_pnl, 2),
            })

    # ---------- 基金实时估值 ----------
    fund_codes = [f["code"] for f in raw["funds"]]
    fund_estimates = {}
    if fund_codes:
        try:
            from data.fund_fetcher import fetch_fund_realtime_batch
            batch = fetch_fund_realtime_batch(fund_codes)
            fund_estimates = {item["code"]: item for item in batch}
        except Exception as e:
            logger.warning(f"获取基金实时估值失败: {e}")

    for h in raw["funds"]:
        code = h["code"]
        est = fund_estimates.get(code, {})
        estimated_nav = est.get("estimated_nav", 0)
        estimated_change = est.get("estimated_change", 0)
        name = est.get("name", "") or h.get("name", code)

        shares = h["shares"]
        avg_nav = h["avg_nav"]
        cost_total = shares * avg_nav
        value_total = shares * estimated_nav if estimated_nav > 0 else 0
        float_pnl = value_total - cost_total
        float_pnl_pct = (float_pnl / cost_total * 100) if cost_total > 0 else 0

        # 今日盈亏 = 份额 × (实时估值 - 昨日净值)
        # 昨日净值 ≈ 实时估值 / (1 + estimated_change/100)
        if estimated_nav > 0 and estimated_change != 0:
            prev_nav = estimated_nav / (1 + estimated_change / 100)
            today_pnl = shares * (estimated_nav - prev_nav)
        else:
            prev_nav = estimated_nav
            today_pnl = 0

        fund_results.append({
            "code": code,
            "name": name,
            "shares": shares,
            "avg_nav": round(avg_nav, 4),
            "notes": h.get("notes", ""),
            "added_at": h.get("added_at", ""),
            "estimated_nav": round(estimated_nav, 4),
            "prev_nav": round(prev_nav, 4),
            "estimated_change": round(estimated_change, 2),
            "update_time": est.get("update_time", ""),
            "cost_total": round(cost_total, 2),
            "value_total": round(value_total, 2),
            "float_pnl": round(float_pnl, 2),
            "float_pnl_pct": round(float_pnl_pct, 2),
            "today_pnl": round(today_pnl, 2),
        })

    # ---------- 汇总 ----------
    stock_cost = sum(s["cost_total"] for s in stock_results)
    stock_value = sum(s["value_total"] for s in stock_results)
    fund_cost = sum(f["cost_total"] for f in fund_results)
    fund_value = sum(f["value_total"] for f in fund_results)

    total_cost = stock_cost + fund_cost
    total_value = stock_value + fund_value
    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
    today_pnl = (sum(s["today_pnl"] for s in stock_results)
                 + sum(f["today_pnl"] for f in fund_results))

    summary = {
        "total_cost": round(total_cost, 2),
        "total_value": round(total_value, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "today_pnl": round(today_pnl, 2),
        "stock_value": round(stock_value, 2),
        "stock_cost": round(stock_cost, 2),
        "fund_value": round(fund_value, 2),
        "fund_cost": round(fund_cost, 2),
        "stock_count": len(stock_results),
        "fund_count": len(fund_results),
    }

    return {
        "stocks": stock_results,
        "funds": fund_results,
        "summary": summary,
        "updated_at": raw.get("updated_at", ""),
    }


# ======================================================================
# 对外 CRUD 接口（供 Flask 路由调用）
# ======================================================================

def portfolio_add_stock(data: dict) -> dict:
    pm = _get_portfolio()
    return pm.add_stock(
        symbol=data.get("symbol", ""),
        name=data.get("name", ""),
        quantity=data.get("quantity", 0),
        avg_cost=data.get("avg_cost", 0),
        market=data.get("market", "A_SHARE"),
        notes=data.get("notes", ""),
    )


def portfolio_remove_stock(data: dict) -> dict:
    pm = _get_portfolio()
    return pm.remove_stock(
        symbol=data.get("symbol", ""),
        market=data.get("market", "A_SHARE"),
    )


def portfolio_update_stock(data: dict) -> dict:
    pm = _get_portfolio()
    symbol = data.pop("symbol", "")
    market = data.pop("market", "A_SHARE")
    return pm.update_stock(symbol, market, **data)


def portfolio_add_fund(data: dict) -> dict:
    pm = _get_portfolio()
    return pm.add_fund(
        code=data.get("code", ""),
        name=data.get("name", ""),
        shares=data.get("shares", 0),
        avg_nav=data.get("avg_nav", 0),
        notes=data.get("notes", ""),
    )


def portfolio_remove_fund(data: dict) -> dict:
    pm = _get_portfolio()
    return pm.remove_fund(code=data.get("code", ""))


def portfolio_update_fund(data: dict) -> dict:
    pm = _get_portfolio()
    code = data.pop("code", "")
    return pm.update_fund(code, **data)


# ======================================================================
# 自动查询接口（根据代码+日期自动获取名称、价格等）
# ======================================================================

def _detect_market(symbol: str) -> str:
    """根据股票代码自动检测市场"""
    s = symbol.strip()
    if s.startswith(("60", "68", "00", "002", "30", "300")):
        return "A_SHARE"
    if s.startswith(("sh", "sz")):
        return "A_SHARE"
    # 含字母或纯字母 → 美股
    if any(c.isalpha() for c in s):
        return "US"
    return "A_SHARE"


def lookup_stock_info(symbol: str, purchase_date: str = "") -> dict:
    """
    根据股票代码+购买日期，自动查询：
    - 股票名称
    - 市场（自动检测）
    - 购买日期当天收盘价（作为默认成本价）
    - 当前实时价格

    返回: {symbol, name, market, purchase_price, current_price, purchase_date}
    """
    symbol = symbol.strip()
    market = _detect_market(symbol)

    result = {
        "symbol": symbol,
        "name": "",
        "market": market,
        "purchase_price": 0,
        "current_price": 0,
        "purchase_date": purchase_date,
    }

    # 1. 获取股票名称和当前价格
    try:
        from data.live_fetcher import fetch_realtime_quote
        quotes = fetch_realtime_quote([symbol], market)
        q = quotes.get(symbol, {})
        result["name"] = q.get("name", "")
        result["current_price"] = q.get("price", 0)
    except Exception as e:
        logger.warning(f"查询股票实时行情失败 [{symbol}]: {e}")

    # 2. 如果有购买日期，查询当天收盘价
    if purchase_date:
        try:
            from data.live_fetcher import fetch_daily_kline
            # 往前取10天范围，确保非交易日也能找到最近收盘价
            start = (datetime.strptime(purchase_date, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
            df = fetch_daily_kline(symbol, start, purchase_date, market)
            if not df.empty:
                row = df.iloc[-1]
                result["purchase_price"] = float(row.get("close", 0))
                returned_date = str(row.name)[:10]  # date index, strip time suffix
                if returned_date and returned_date != purchase_date:
                    result["actual_date"] = returned_date
        except Exception as e:
            logger.warning(f"查询股票历史K线失败 [{symbol}]: {e}")

    return result


def lookup_fund_info(code: str, purchase_date: str = "") -> dict:
    """
    根据基金代码+购买日期，自动查询：
    - 基金名称
    - 购买日期当天净值（作为默认成本净值）
    - 当前实时估值

    返回: {code, name, purchase_nav, current_nav, purchase_date}
    """
    code = code.strip()

    result = {
        "code": code,
        "name": "",
        "purchase_nav": 0,
        "current_nav": 0,
        "purchase_date": purchase_date,
    }

    # 1. 获取基金名称和当前估值
    try:
        from data.fund_fetcher import fetch_fund_realtime_estimate
        est = fetch_fund_realtime_estimate(code)
        result["name"] = est.get("name", "")
        result["current_nav"] = est.get("estimated_nav", 0)
    except Exception as e:
        logger.warning(f"查询基金实时估值失败 [{code}]: {e}")

    # 2. 如果有购买日期，查询当天净值
    if purchase_date:
        try:
            from data.fund_fetcher import fetch_fund_nav_history
            # 动态计算天数范围
            try:
                buy_dt = datetime.strptime(purchase_date, "%Y-%m-%d")
                days_back = min(max((datetime.now() - buy_dt).days + 10, 180), 3650)
            except ValueError:
                days_back = 365
            # 取历史净值
            nav_list = fetch_fund_nav_history(code, days=days_back)
            if nav_list:
                # 找到购买日期当天或最近的净值
                exact = [n for n in nav_list if n["date"] == purchase_date]
                if exact:
                    result["purchase_nav"] = exact[0]["nav"]
                else:
                    # 非交易日，找购买日期之前最近的净值
                    before = [n for n in nav_list if n["date"] <= purchase_date]
                    if before:
                        result["purchase_nav"] = before[-1]["nav"]
                        result["actual_date"] = before[-1]["date"]
                    elif nav_list:
                        result["purchase_nav"] = nav_list[0]["nav"]
                        result["actual_date"] = nav_list[0]["date"]
        except Exception as e:
            logger.warning(f"查询基金历史净值失败 [{code}]: {e}")

    return result

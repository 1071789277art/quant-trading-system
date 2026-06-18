"""
数据模型与 CRUD 操作

封装 users / portfolios / daily_states / paper_states 表的增删改查。
所有函数接收 user_id 参数实现数据隔离。
"""
import json
import logging
from dashboard.db import get_db

logger = logging.getLogger(__name__)


# ===================== 用户 =====================

def get_user_by_id(user_id: int):
    """按 ID 查询用户"""
    return get_db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def get_user_by_username(username: str):
    """按用户名查询"""
    return get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def create_user(username: str, email: str, password_hash: str) -> int:
    """创建用户，返回 user_id"""
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
        (username, email, password_hash),
    )
    conn.commit()
    return cur.lastrowid


# ===================== 持仓（股票 + 基金） =====================

def get_portfolio_stocks(user_id: int) -> list:
    """获取用户所有股票持仓"""
    rows = get_db().execute(
        "SELECT * FROM portfolios WHERE user_id=? AND asset_type='stock' ORDER BY created_at",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_portfolio_funds(user_id: int) -> list:
    """获取用户所有基金持仓"""
    rows = get_db().execute(
        "SELECT * FROM portfolios WHERE user_id=? AND asset_type='fund' ORDER BY created_at",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def add_stock(user_id: int, symbol: str, name: str, quantity: float,
              avg_cost: float, market: str = "A_SHARE", notes: str = "") -> int:
    """添加股票持仓"""
    conn = get_db()
    cur = conn.execute(
        """INSERT OR REPLACE INTO portfolios
           (user_id, asset_type, symbol, name, quantity, avg_cost, market, notes)
           VALUES (?, 'stock', ?, ?, ?, ?, ?, ?)""",
        (user_id, symbol, name, quantity, avg_cost, market, notes),
    )
    conn.commit()
    return cur.lastrowid


def remove_stock(user_id: int, symbol: str):
    """删除股票持仓"""
    conn = get_db()
    conn.execute(
        "DELETE FROM portfolios WHERE user_id=? AND asset_type='stock' AND symbol=?",
        (user_id, symbol),
    )
    conn.commit()


def update_stock(user_id: int, symbol: str, **kwargs):
    """更新股票持仓字段"""
    allowed = {"name", "quantity", "avg_cost", "market", "notes"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [user_id, symbol]
    conn = get_db()
    conn.execute(
        f"UPDATE portfolios SET {set_clause} WHERE user_id=? AND asset_type='stock' AND symbol=?",
        values,
    )
    conn.commit()


def add_fund(user_id: int, fund_code: str, fund_name: str, shares: float,
             avg_nav: float, notes: str = "") -> int:
    """添加基金持仓"""
    conn = get_db()
    cur = conn.execute(
        """INSERT OR REPLACE INTO portfolios
           (user_id, asset_type, fund_code, fund_name, shares, avg_nav, notes)
           VALUES (?, 'fund', ?, ?, ?, ?, ?)""",
        (user_id, fund_code, fund_name, shares, avg_nav, notes),
    )
    conn.commit()
    return cur.lastrowid


def remove_fund(user_id: int, fund_code: str):
    """删除基金持仓"""
    conn = get_db()
    conn.execute(
        "DELETE FROM portfolios WHERE user_id=? AND asset_type='fund' AND fund_code=?",
        (user_id, fund_code),
    )
    conn.commit()


def update_fund(user_id: int, fund_code: str, **kwargs):
    """更新基金持仓字段"""
    allowed = {"fund_name", "shares", "avg_nav", "notes"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [user_id, fund_code]
    conn = get_db()
    conn.execute(
        f"UPDATE portfolios SET {set_clause} WHERE user_id=? AND asset_type='fund' AND fund_code=?",
        values,
    )
    conn.commit()


# ===================== 每日实盘状态 =====================

def get_daily_state(user_id: int, market: str = "A_SHARE") -> dict:
    """读取每日实盘状态"""
    row = get_db().execute(
        "SELECT state_json FROM daily_states WHERE user_id=? AND market=?",
        (user_id, market),
    ).fetchone()
    if row:
        try:
            return json.loads(row["state_json"])
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def save_daily_state(user_id: int, market: str, state: dict):
    """保存每日实盘状态"""
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO daily_states (user_id, market, state_json, updated_at)
           VALUES (?, ?, ?, datetime('now', 'localtime'))""",
        (user_id, market, json.dumps(state, ensure_ascii=False)),
    )
    conn.commit()


# ===================== 模拟交易状态 =====================

def get_paper_state(user_id: int, strategy_name: str) -> dict:
    """读取模拟交易状态"""
    row = get_db().execute(
        "SELECT state_json FROM paper_states WHERE user_id=? AND strategy_name=?",
        (user_id, strategy_name),
    ).fetchone()
    if row:
        try:
            return json.loads(row["state_json"])
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def save_paper_state(user_id: int, strategy_name: str, state: dict):
    """保存模拟交易状态"""
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO paper_states (user_id, strategy_name, state_json, updated_at)
           VALUES (?, ?, ?, datetime('now', 'localtime'))""",
        (user_id, strategy_name, json.dumps(state, ensure_ascii=False)),
    )
    conn.commit()

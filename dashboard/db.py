"""
SQLite 数据库初始化与连接管理

提供：
- get_db(): 获取线程安全的 SQLite 连接
- init_db(): 建表（首次启动时自动调用）
"""
import os
import sqlite3
import threading
import logging

logger = logging.getLogger(__name__)

# 数据库路径：优先使用 Render Disk 挂载点，否则用本地 data 目录
_DB_DIR = os.environ.get("QUANTX_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
_DB_PATH = os.path.join(_DB_DIR, "quantx.db")

# 线程本地存储，每个线程一个连接
_local = threading.local()


def get_db() -> sqlite3.Connection:
    """获取当前线程的 SQLite 连接（懒创建）"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
        conn = sqlite3.connect(_DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")      # 并发读优化
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def close_db():
    """关闭当前线程的连接"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


def init_db():
    """创建所有表（幂等操作，已存在则跳过）"""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    NOT NULL UNIQUE,
            email         TEXT    NOT NULL DEFAULT '',
            password_hash TEXT    NOT NULL,
            created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS portfolios (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            asset_type TEXT    NOT NULL CHECK(asset_type IN ('stock', 'fund')),
            -- 股票字段
            symbol     TEXT    DEFAULT '',
            name       TEXT    DEFAULT '',
            quantity   REAL    DEFAULT 0,
            avg_cost   REAL    DEFAULT 0,
            market     TEXT    DEFAULT 'A_SHARE',
            -- 基金字段
            fund_code  TEXT    DEFAULT '',
            fund_name  TEXT    DEFAULT '',
            shares     REAL    DEFAULT 0,
            avg_nav    REAL    DEFAULT 0,
            -- 通用
            notes      TEXT    DEFAULT '',
            created_at TEXT    NOT NULL DEFAULT (date('now', 'localtime')),
            UNIQUE(user_id, asset_type, symbol, fund_code)
        );

        CREATE TABLE IF NOT EXISTS daily_states (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            market     TEXT    NOT NULL DEFAULT 'A_SHARE',
            state_json TEXT    NOT NULL DEFAULT '{}',
            updated_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
            UNIQUE(user_id, market)
        );

        CREATE TABLE IF NOT EXISTS paper_states (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            strategy_name TEXT    NOT NULL,
            state_json    TEXT    NOT NULL DEFAULT '{}',
            updated_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
            UNIQUE(user_id, strategy_name)
        );

        CREATE INDEX IF NOT EXISTS idx_portfolios_user ON portfolios(user_id);
        CREATE INDEX IF NOT EXISTS idx_daily_states_user ON daily_states(user_id, market);
        CREATE INDEX IF NOT EXISTS idx_paper_states_user ON paper_states(user_id, strategy_name);
    """)
    conn.commit()
    logger.info("数据库初始化完成: %s", _DB_PATH)


def get_db_path() -> str:
    """返回数据库文件路径（供调试用）"""
    return _DB_PATH

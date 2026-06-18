"""
量化交易系统 - 全局配置
"""
import os

# ===== 数据配置 =====
DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "cache")
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "quant.db")

# ===== 回测配置 =====
DEFAULT_CAPITAL = 1_000_000       # 初始资金（元）
DEFAULT_COMMISSION = 0.0003       # 手续费率（万三）
DEFAULT_SLIPPAGE = 0.001          # 滑点（0.1%）
DEFAULT_STAMP_TAX = 0.001         # 印花税（千一，仅A股卖出）
DEFAULT_MIN_COMMISSION = 5.0      # 最低手续费（元）

# ===== 风控配置 =====
MAX_POSITION_PCT = 0.25           # 单只股票最大仓位占比
MAX_TOTAL_POSITION_PCT = 0.90     # 总仓位上限
STOP_LOSS_PCT = 0.08              # 个股止损线（8%）
TAKE_PROFIT_PCT = 0.30            # 个股止盈线（30%）

# ===== 交易配置 =====
A_SHARE_LOT_SIZE = 100            # A股每手股数
MARKET_CHOICES = ["A_SHARE", "US"]

# ===== Web仪表盘 =====
DASHBOARD_HOST = os.environ.get("HOST", "0.0.0.0")
DASHBOARD_PORT = int(os.environ.get("PORT", 8050))
DASHBOARD_DEBUG = os.environ.get("FLASK_DEBUG", "false").lower() in ("true", "1", "yes")

# ===== 基金配置 =====
FUND_TYPES = {
    "all": "全部", "gp": "股票型", "hh": "混合型", "zq": "债券型",
    "zs": "指数型", "qdii": "QDII", "fof": "FOF",
}
FUND_SORT_OPTIONS = {
    "rzdf": "日涨幅", "1yzf": "近1周", "1nzf": "近1年",
    "jnzf": "今年来", "3nzf": "近3年", "lnzf": "成立以来",
}
FUND_RANKING_DEFAULT_SIZE = 50

# ===== 日志配置 =====
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

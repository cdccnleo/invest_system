"""
position_rebalancer_v2.py — V25-D 调仓优化 (P1, 7/04-7/10)
================================================================
背景: v2.5 plan 7 候选中 P1 (1 周), 接续 V25-B 调仓助手
核心:
  1. fcntl.flock 并发锁 (PIT #87, 沿用 schedule_runner 模式, 防止双跑)
  2. 资金检查 可用现金 ≥ 1.1x (PIT #88, 4 CSV 实战"可用"列)
  3. 4 CSV 跨账户汇总 (PIT #89, 广发/国金基金/汇添富/国金股票 schema 异构)
  4. 周报自动推送 6/22 周日 22:00 (PIT #90, 沿用 V25-A1+A2 cron 飞书)

4 数据源 (per memory §持仓数据源):
  - ~/.hermes-web-ui/upload/3a77...csv 广发 (场内 ETF, 2628B)
  - ~/.hermes-web-ui/upload/aa1e...csv 国金股票 (1899B)
  - ~/.hermes-web-ui/upload/531c...csv 国金基金 (704B)
  - ~/.hermes-web-ui/upload/5770...csv 汇添富 (574B)

PIT 经验:
  - PIT #87 (V25-D NEW): fcntl.flock 单实例锁 (沿用 schedule_runner 模式, 实战 rebalancer 可能 2 次同时跑)
  - PIT #88 (V25-D NEW): 资金检查 可用现金 ≥ 1.1x (实战 4 CSV 各自有"可用"列)
  - PIT #89 (V25-D NEW): 4 CSV 跨账户 schema 异构, 必须 normalize (广发/国金/汇添富/天天)
  - PIT #90 (V25-D NEW): 周报自动推送 6/22 周日 22:00 飞书 (沿用 V25-A1+A2)
  - PIT #66 沿用: 飞书推送就地实现
  - PIT #69 沿用: 3 通道全空 → 返 0
  - PIT #74 沿用: V25-B 默认 simulation
"""
import csv
import fcntl
import json
import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

# 路径
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "hermes_coordination" / "scripts"))

import psycopg2
import psycopg2.extras

from credentials import get_credential

LOG = logging.getLogger("v25_d.position_rebalancer_v2")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ==================== 常量 ====================

DB_PARAMS = {
    "host": "localhost",
    "dbname": "investpilot",
    "user": "invest_admin",
    "password": get_credential("DB_PASSWORD"),
}

# 锁路径
LOCK_PATH = ROOT / "logs" / ".position_rebalancer_v2.lock"

# 4 CSV 路径
CSV_DIR = Path("/home/aileo/.hermes-web-ui/upload")
CSV_PATHS = {
    "guangfa": CSV_DIR / "3a77e1b03369583a.csv",   # 广发 (场内 ETF)
    "guojin_stock": CSV_DIR / "aa1ed9815bc3279e.csv",  # 国金股票
    "guojin_fund": CSV_DIR / "531c65487cebd183.csv",  # 国金基金
    "huitianfu": CSV_DIR / "57702ffe98bc0ac5.csv",  # 汇添富
}

USER_ID = "aileo"
FEISHU_MAX_LEN = 1800
RETRY_TIMES = 3
MIN_CASH_MULTIPLIER = 1.1  # PIT #88: 可用现金 ≥ 1.1x 调仓金额


# ==================== 数据结构 ====================

@dataclass
class AccountPosition:
    """单账户单持仓"""
    account: str  # guangfa / guojin_stock / guojin_fund / huitianfu
    code: str
    name: str
    type: str  # stock / fund / etf
    market_value: float
    cost: float
    profit: float
    profit_pct: float
    available: float  # 可用数量 / 可用份额
    cash: float  # 可用现金 (账户级)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CashCheck:
    """资金检查结果"""
    account: str
    required: float  # 所需现金
    available: float  # 实际可用
    sufficient: bool
    multiplier: float  # available / required
    shortfall: float  # 缺口 (if not sufficient)


@dataclass
class LockInfo:
    """锁状态"""
    acquired: bool
    pid: int
    lock_path: str
    waited_seconds: float = 0.0
    error: Optional[str] = None


@dataclass
class CrossAccountSummary:
    """跨账户汇总"""
    accounts: List[str]
    total_market_value: float
    total_cash: float
    total_profit: float
    avg_profit_pct: float
    position_count: int
    # 4 账户分别
    by_account: Dict[str, Dict[str, Any]]
    # 调仓建议 (V25-B generate_rebalance_suggestion 复用)
    rebalance_suggestions: Optional[Dict[str, Any]] = None


# ==================== PIT #87 fcntl.flock 单实例锁 ====================

@contextmanager
def acquire_lock(lock_path: Path = LOCK_PATH, timeout: float = 10.0):
    """
    PIT #87: fcntl.flock 单实例锁 (沿用 schedule_runner 模式)
    - LOCK_EX 排他锁 + LOCK_NB 非阻塞
    - 锁文件持有者 PID 写入 (死锁检测)
    - 持有者已死 (/proc/PID 不存在) → 强删 + 重试
    - 超时 10s → 放弃
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    info = LockInfo(acquired=False, pid=os.getpid(), lock_path=str(lock_path))
    t0 = time.time()
    try:
        # 检查现有锁
        if lock_path.exists():
            try:
                with open(lock_path) as f:
                    existing_pid = int(f.read().strip() or "0")
                if existing_pid and existing_pid != os.getpid() and os.path.isdir(f"/proc/{existing_pid}"):
                    # 锁的持有者还活着, 等待
                    LOG.info(f"⏳ 锁持有者 PID={existing_pid} 存活, 等待释放...")
                elif existing_pid and existing_pid != os.getpid():
                    # 锁持有者已死, 强删
                    LOG.warning(f"⚠️ 锁持有者 PID={existing_pid} 已死, 强删")
                    try:
                        lock_path.unlink()
                    except Exception as e:
                        LOG.warning(f"强删失败: {e}")
            except Exception:
                pass

        fd = open(lock_path, "w")
        # 等待锁 (非阻塞轮询)
        while time.time() - t0 < timeout:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fd.write(str(os.getpid()))
                fd.flush()
                info.acquired = True
                info.waited_seconds = time.time() - t0
                LOG.info(f"✅ 锁获取成功 PID={os.getpid()}, 等待 {info.waited_seconds:.2f}s")
                break
            except (BlockingIOError, OSError):
                time.sleep(0.5)
        if not info.acquired:
            info.error = f"锁超时 {timeout}s"
            LOG.error(f"❌ {info.error}")
            fd.close()
            fd = None
            yield info
            return
        yield info
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                fd.close()
            except Exception:
                pass
            # 清空 lock file
            try:
                if lock_path.exists():
                    lock_path.unlink()
            except Exception:
                pass
            LOG.info(f"🔓 锁释放 PID={os.getpid()}")


# ==================== PIT #89 4 CSV 跨账户 normalize ====================

def _parse_amount(s: str) -> float:
    """解析 "1,234.56" / "1,234" / "1.5" 等格式金额"""
    if not s:
        return 0.0
    s = str(s).strip().replace(",", "").replace("，", "").replace(" ", "")
    # 处理空 / 异常
    if not s or s in ("--", "—", "-", "N/A"):
        return 0.0
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _parse_int(s: str) -> int:
    """解析 "1,234" / "1.5万" 等格式整数"""
    if not s:
        return 0
    s = str(s).strip().replace(",", "").replace("，", "").replace(" ", "")
    if not s or s in ("--", "—", "-", "N/A"):
        return 0
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _detect_type(name: str, code: str) -> str:
    """PIT #89: 检测持仓类型 (stock/fund/etf)"""
    if not name and not code:
        return "unknown"
    name_lower = (name or "").lower()
    code = str(code or "")
    # ETF: 5开头 / 1开头 / 名称含 ETF
    if code.startswith(("5", "1")) and len(code) == 6:
        return "etf"
    if "etf" in name_lower:
        return "etf"
    # Fund: 6位 + 含"混合"/"债券"/"指数"等
    if any(kw in name for kw in ("混合", "债券", "指数", "LOF", "QDII", "货币")):
        return "fund"
    # 场内基金 (512880, 159516 等)
    if code.startswith(("51", "56", "58", "15")) and len(code) == 6:
        return "etf"
    # 默认 stock
    return "stock"


def parse_guangfa_csv(path: Path) -> List[AccountPosition]:
    """PIT #89: 解析广发 CSV (场内 ETF)
    Schema: 币种/余额/可用/可取/参考市值/资产/盈亏 (账户级)
    实战 6/14: 1 行 (汇总行) - 不是持仓行
    """
    if not path.exists():
        return []
    positions = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return []
        for row in reader:
            if len(row) < 7:
                continue
            cash = _parse_amount(row[2])  # 可用
            mv = _parse_amount(row[4])  # 参考市值
            profit = _parse_amount(row[6])  # 盈亏
            # 广发 CSV 是账户级汇总, 不是持仓行
            # 单账户单 position 标记: "广发账户"
            positions.append(AccountPosition(
                account="guangfa",
                code="GUANGFA_CASH",
                name="广发账户",
                type="cash",
                market_value=mv,
                cost=mv - profit,
                profit=profit,
                profit_pct=(profit / (mv - profit) * 100) if (mv - profit) > 0 else 0,
                available=cash,
                cash=cash,
                raw={"余额": row[1], "可用": row[2], "可取": row[3], "参考市值": row[4], "资产": row[5], "盈亏": row[6]},
            ))
    return positions


def parse_guojin_stock_csv(path: Path) -> List[AccountPosition]:
    """PIT #89 + #91: 解析国金股票 CSV
    实战 16 列 schema (6/14 发现):
      类型(0) / 证券名称(1) / 证券代码(2) / 可用数量(3) / 当前数量(4) /
      今买数量(5) / 今卖数量(6) / 成本价(7) / 市值价(8) / 市值(9) /
      浮动盈亏(10) / 盈亏比例%(11) / 当日盈亏(12) / 当日盈亏率(13) / 个股仓位(14) / 市场(15)
    实战 6/14: 13 行 (1 股票 + 12 场内基金/ETF)
    """
    if not path.exists():
        return []
    positions = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return []
        for row in reader:
            if len(row) < 16:  # PIT #91: 必须 16 列
                continue
            name = row[1].strip()
            code = row[2].strip()
            if not name or not code:
                continue
            cost_price = _parse_amount(row[7])  # 成本价
            market_price = _parse_amount(row[8])  # 市值价
            mv = _parse_amount(row[9])  # 市值 (PIT #91 修正)
            profit = _parse_amount(row[10])  # 浮动盈亏
            cost = mv - profit
            available = _parse_int(row[3])  # 可用数量
            positions.append(AccountPosition(
                account="guojin_stock",
                code=code,
                name=name,
                type=_detect_type(name, code),
                market_value=mv,
                cost=cost,
                profit=profit,
                profit_pct=(_parse_amount(row[11])),  # PIT #91: 盈亏比例% (直接用)
                available=float(available),
                cash=0.0,  # 股票账户无单独现金
                raw={
                    "类型": row[0], "可用数量": row[3], "当前数量": row[4],
                    "今买数量": row[5], "今卖数量": row[6],
                    "成本价": row[7], "市值价": row[8], "市值": row[9],
                    "浮动盈亏": row[10], "盈亏比例": row[11],
                    "当日盈亏": row[12], "当日盈亏率": row[13],
                    "个股仓位": row[14], "市场": row[15],
                },
            ))
    return positions


def parse_guojin_fund_csv(path: Path) -> List[AccountPosition]:
    """PIT #89: 解析国金基金 CSV
    Schema: 基金代码/基金名称/净值日期/最新净值/收费方式/持有份额/可用份额/不可用份额/参考市值/投入本金/持有盈亏
    """
    if not path.exists():
        return []
    positions = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return []
        for row in reader:
            if len(row) < 11:
                continue
            code = row[0].strip()
            name = row[1].strip()
            if not code or not name:
                continue
            shares = _parse_int(row[5])  # 持有份额
            available = _parse_int(row[6])  # 可用份额
            mv = _parse_amount(row[8])  # 参考市值
            cost = _parse_amount(row[9])  # 投入本金
            profit = _parse_amount(row[10])  # 持有盈亏
            positions.append(AccountPosition(
                account="guojin_fund",
                code=code,
                name=name,
                type="fund",
                market_value=mv,
                cost=cost,
                profit=profit,
                profit_pct=(profit / cost * 100) if cost > 0 else 0,
                available=float(available),
                cash=0.0,
                raw={"基金代码": row[0], "基金名称": row[1], "净值日期": row[2], "最新净值": row[3], "收费方式": row[4], "持有份额": row[5], "可用份额": row[6], "不可用份额": row[7], "参考市值": row[8], "投入本金": row[9], "持有盈亏": row[10]},
            ))
    return positions


def parse_huitianfu_csv(path: Path) -> List[AccountPosition]:
    """PIT #89: 解析汇添富 CSV
    Schema: 产品代码/产品名称/产品类型/最新净值/净值日期/金额/持仓收益/持仓收益%
    """
    if not path.exists():
        return []
    positions = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return []
        for row in reader:
            if len(row) < 8:
                continue
            code = row[0].strip()
            name = row[1].strip()
            if not code or not name:
                continue
            mv = _parse_amount(row[5])  # 金额 (元)
            profit = _parse_amount(row[6])  # 持仓收益
            profit_pct = _parse_amount(row[7])  # 持仓收益%
            positions.append(AccountPosition(
                account="huitianfu",
                code=code,
                name=name,
                type="fund",
                market_value=mv,
                cost=mv - profit,
                profit=profit,
                profit_pct=profit_pct,
                available=mv,  # 基金可全赎回
                cash=0.0,
                raw={"产品代码": row[0], "产品名称": row[1], "产品类型": row[2], "最新净值": row[3], "净值日期": row[4], "金额": row[5], "持仓收益": row[6], "持仓收益%": row[7]},
            ))
    return positions


def load_all_accounts() -> List[AccountPosition]:
    """PIT #89: 跨账户加载 (4 CSV 全部)"""
    all_positions = []
    all_positions.extend(parse_guangfa_csv(CSV_PATHS["guangfa"]))
    all_positions.extend(parse_guojin_stock_csv(CSV_PATHS["guojin_stock"]))
    all_positions.extend(parse_guojin_fund_csv(CSV_PATHS["guojin_fund"]))
    all_positions.extend(parse_huitianfu_csv(CSV_PATHS["huitianfu"]))
    return all_positions


def summarize_cross_account(positions: Optional[List[AccountPosition]] = None) -> CrossAccountSummary:
    """PIT #89: 跨账户汇总"""
    if positions is None:
        positions = load_all_accounts()
    by_account: Dict[str, Dict[str, Any]] = {}
    total_mv = 0.0
    total_cash = 0.0
    total_profit = 0.0
    position_count = len(positions)
    for p in positions:
        if p.account not in by_account:
            by_account[p.account] = {
                "count": 0,
                "market_value": 0.0,
                "cost": 0.0,
                "profit": 0.0,
                "cash": 0.0,
                "available": 0.0,
            }
        by_account[p.account]["count"] += 1
        by_account[p.account]["market_value"] += p.market_value
        by_account[p.account]["cost"] += p.cost
        by_account[p.account]["profit"] += p.profit
        by_account[p.account]["cash"] += p.cash
        by_account[p.account]["available"] += p.available
        total_mv += p.market_value
        total_cash += p.cash
        total_profit += p.profit
    avg_pp = (total_profit / (total_mv - total_profit) * 100) if (total_mv - total_profit) > 0 else 0.0
    return CrossAccountSummary(
        accounts=list(by_account.keys()),
        total_market_value=total_mv,
        total_cash=total_cash,
        total_profit=total_profit,
        avg_profit_pct=avg_pp,
        position_count=position_count,
        by_account=by_account,
    )


# ==================== PIT #88 资金检查 ====================

def check_cash(required: float, account: Optional[str] = None) -> List[CashCheck]:
    """
    PIT #88: 资金检查 可用现金 ≥ 1.1x
    - account=None: 全账户检查
    - account=指定: 仅检查该账户
    """
    positions = load_all_accounts()
    by_account: Dict[str, float] = {}
    for p in positions:
        if p.cash > 0:  # 仅累加现金型账户 (guangfa)
            by_account[p.account] = by_account.get(p.account, 0.0) + p.cash
    if account:
        by_account = {account: by_account.get(account, 0.0)}
    checks = []
    for acc, cash in by_account.items():
        sufficient = cash >= required * MIN_CASH_MULTIPLIER
        multiplier = cash / required if required > 0 else float("inf")
        shortfall = max(0, required * MIN_CASH_MULTIPLIER - cash)
        checks.append(CashCheck(
            account=acc,
            required=required,
            available=cash,
            sufficient=sufficient,
            multiplier=multiplier,
            shortfall=shortfall,
        ))
    return checks


# ==================== PIT #90 周报自动推送 ====================

def _send_via_feishu_inplace(webhook_url: str, title: str, content: str, level: str = "INFO") -> bool:
    """PIT #66 沿用: 飞书推送就地实现"""
    import urllib.request
    color_map = {"INFO": "#4CAF50", "WARNING": "#FF9800", "ERROR": "#F44336"}
    template = color_map.get(level, "#4CAF50")
    if len(content) > FEISHU_MAX_LEN:
        content = content[:FEISHU_MAX_LEN - 50] + "\n\n... (内容过长, 已截断)"
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "elements": [
                {"tag": "markdown", "content": content},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": f"InvestPilot · V25-D 调仓周报 · {datetime.now().strftime('%H:%M:%S')}"}]},
            ],
        },
    }
    for attempt in range(RETRY_TIMES):
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(webhook_url, data=data, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8")
                if '"code":0' in body or resp.status == 200:
                    return True
        except Exception as e:
            LOG.warning(f"飞书推送重试 {attempt+1}/{RETRY_TIMES} 失败: {e}")
            if attempt < RETRY_TIMES - 1:
                time.sleep(2 ** attempt)
    return False


def push_weekly_to_feishu(summary: CrossAccountSummary) -> int:
    """PIT #90: 周报自动推送"""
    webhook = get_credential("FEISHU_WEBHOOK", "")
    if not webhook:
        LOG.info("[V25-D] FEISHU_WEBHOOK 未配, 跳过飞书推送 (PIT #69 PG 兜底)")
        return 0
    content = f"""**调仓周报** ({datetime.now().strftime('%Y-%m-%d')})

💰 **跨账户汇总**
- 总市值: **¥{summary.total_market_value:,.0f}** ({summary.position_count} 持仓)
- 总现金: **¥{summary.total_cash:,.0f}**
- 总盈亏: **¥{summary.total_profit:+,.0f}** (平均 pp={summary.avg_profit_pct:+.2f}%)

📊 **各账户明细**
{chr(10).join(f"- **{acc}**: {info['count']} 持仓, 市值 ¥{info['market_value']:,.0f}, 盈亏 ¥{info['profit']:+,.0f}" for acc, info in summary.by_account.items())}

🔒 **PIT #87**: fcntl.flock 单实例锁就绪 (沿用 schedule_runner 模式)
💵 **PIT #88**: 资金检查可用现金 ≥ 1.1x 已就绪
"""
    title = f"📅 V25-D 调仓周报 ({datetime.now().strftime('%Y-%m-%d')})"
    ok = _send_via_feishu_inplace(webhook, title, content, "INFO")
    if ok:
        LOG.info(f"✅ 飞书推送成功: 调仓周报")
    return 1 if ok else 0


# ==================== PG 表 DDL ====================

EARLY_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS l3.cross_account_summary (
        id BIGSERIAL PRIMARY KEY,
        summary_date DATE NOT NULL,
        total_market_value NUMERIC(15,2),
        total_cash NUMERIC(15,2),
        total_profit NUMERIC(15,2),
        avg_profit_pct NUMERIC(5,2),
        position_count INTEGER,
        accounts TEXT[],
        by_account JSONB DEFAULT '{}',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (summary_date)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_cas_date ON l3.cross_account_summary(summary_date);",
]


def ensure_pg_tables() -> None:
    """建表 (幂等)"""
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    for ddl in EARLY_DDL_STATEMENTS:
        cur.execute(ddl)
    conn.commit()
    cur.close()
    conn.close()
    LOG.info("✅ l3.cross_account_summary 表 + 1 索引已就绪")


def persist_summary(summary: CrossAccountSummary, today: Optional[str] = None) -> bool:
    """写 l3.cross_account_summary"""
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")
    conn = psycopg2.connect(**DB_PARAMS)
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO l3.cross_account_summary
    (summary_date, total_market_value, total_cash, total_profit, avg_profit_pct,
     position_count, accounts, by_account)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (summary_date) DO UPDATE SET
        total_market_value = EXCLUDED.total_market_value,
        total_cash = EXCLUDED.total_cash,
        total_profit = EXCLUDED.total_profit,
        avg_profit_pct = EXCLUDED.avg_profit_pct,
        position_count = EXCLUDED.position_count,
        accounts = EXCLUDED.accounts,
        by_account = EXCLUDED.by_account,
        created_at = NOW();
    """, (
        today, summary.total_market_value, summary.total_cash, summary.total_profit,
        summary.avg_profit_pct, summary.position_count, summary.accounts,
        json.dumps(summary.by_account, ensure_ascii=False, default=str),
    ))
    conn.commit()
    cur.close()
    conn.close()
    LOG.info(f"✅ 跨账户汇总持久化: {today}")
    return True


# ==================== 主入口 ====================

def run_weekly_rebalance(today: Optional[str] = None) -> Dict[str, Any]:
    """V25-D 主流程: 锁 + 资金 + 跨账户 + 周报"""
    if today is None:
        today = datetime.now().strftime("%Y-%m-%d")
    ensure_pg_tables()

    result = {"today": today, "lock": None, "cash_checks": [], "summary": None, "feishu_pushed": 0}

    # PIT #87: fcntl.flock 单实例锁
    with acquire_lock(lock_path=LOCK_PATH, timeout=10.0) as lock_info:
        result["lock"] = asdict(lock_info)
        if not lock_info.acquired:
            LOG.error(f"❌ 锁获取失败: {lock_info.error}")
            return result

        # PIT #89: 跨账户汇总
        positions = load_all_accounts()
        summary = summarize_cross_account(positions)
        result["summary"] = {
            "total_market_value": summary.total_market_value,
            "total_cash": summary.total_cash,
            "total_profit": summary.total_profit,
            "avg_profit_pct": summary.avg_profit_pct,
            "position_count": summary.position_count,
            "accounts": summary.accounts,
            "by_account": summary.by_account,
        }
        LOG.info(f"💰 跨账户: ¥{summary.total_market_value:,.0f} ({len(positions)} 持仓)")

        # PIT #88: 资金检查 (示例: 假设调仓 10 万)
        cash_checks = check_cash(required=100000, account=None)
        result["cash_checks"] = [asdict(c) for c in cash_checks]
        for c in cash_checks:
            status = "✅" if c.sufficient else "❌"
            LOG.info(f"{status} 资金检查 [{c.account}]: 可用 ¥{c.available:,.0f} / 需要 ¥{c.required:,.0f} (x{c.multiplier:.2f})")

        # 持久化
        persist_summary(summary, today=today)

        # PIT #90: 周报推送
        result["feishu_pushed"] = push_weekly_to_feishu(summary)

    # 控制台输出
    print(f"\n=== V25-D 调仓周报 ({today}) ===")
    print(f"🔒 锁: PID={result['lock']['pid']} acquired={result['lock']['acquired']} waited={result['lock']['waited_seconds']:.2f}s")
    print(f"\n💰 跨账户: ¥{summary.total_market_value:,.0f} (现金 ¥{summary.total_cash:,.0f}, 盈亏 ¥{summary.total_profit:+,.0f}, pp={summary.avg_profit_pct:+.2f}%)")
    print(f"   持仓 {summary.position_count} 条, 账户 {len(summary.accounts)} 个")
    print(f"\n📊 各账户:")
    for acc, info in summary.by_account.items():
        print(f"   - {acc}: {info['count']} 持仓, 市值 ¥{info['market_value']:,.0f}, 盈亏 ¥{info['profit']:+,.0f}")
    print(f"\n💵 资金检查 (假设调仓 ¥100,000):")
    for c in cash_checks:
        status = "✅" if c.sufficient else "❌"
        print(f"   {status} {c.account}: 可用 ¥{c.available:,.0f} / 需要 ¥{c.required:,.0f} (x{c.multiplier:.2f}, 缺 ¥{c.shortfall:,.0f})")
    print(f"\n📅 飞书推送: {result['feishu_pushed']}")
    return result


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--today", type=str, default=None)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--no-lock", action="store_true", help="跳过锁 (调试用)")
    args = p.parse_args()

    if args.self_test:
        print("=== V25-D self-test (实战 6/14 跨账户 + 锁 + 资金 + 周报) ===")
        run_weekly_rebalance(today=args.today or "2026-06-14")
    else:
        run_weekly_rebalance(today=args.today)

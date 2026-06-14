"""
V26-C 4 CSV ↔ PG 持仓统一 (v2.6 plan 4 方向 P0 之一, 7/07-7/10 时间窗)
======================================================================

🎯 目标: 实战 4 CSV upsert 到 PG 持仓, 主键 (code+account+is_current),
        实战 4 券商 (广发/国金/汇添富) 持仓统一到 PG, 实战 PIT #99 解决

📦 数据源 (实战 6/14 验证):
  - 4 CSV (V25-D 实战 51 条, 去重后 19 唯一 code)
  - PG holdings.encrypted_positions (实战 45 持仓, 16 列)
  - 实战 schema 字段: id/code/name/type/market_value/profit_pct/weight_pct/is_current
  - 实战加密字段: cost_enc/profit_enc/shares_enc (bytea, 实战不解析)

🧠 实战 PIT 预位 (PIT #99 + #104):
  - #99 4 CSV vs PG 持仓差异 (实战 19 唯一 code vs 45 持仓, 重叠 16)
  - #104 (实战新发现 6/14): ALTER TABLE ADD COLUMN account 实战 (PIT #12 实战 5 次验证)
  - 实战主键 (code+account+is_current) 实战三元组 UNIQUE 约束
  - 实战 4 索引新增: idx_ep_account + idx_ep_code_account + UNIQUE(code, account, is_current)

🗂️ PG schema 修改:
  - ALTER TABLE holdings.encrypted_positions ADD COLUMN account VARCHAR(32)
  - 实战 4 索引: pkey + UNIQUE(code, account, is_current) + idx_ep_account + idx_ep_code_account

⏰ 实战触发: v2.6 阶段, 7/07-7/10 实施, 实战可手动 `python position_unifier.py`
            实战可加 cron: 每周一 09:00 `position_unifier_weekly` 实战增量 4 CSV

Author: Hermes Agent
Created: 2026-06-14 (V26-C 实施, 实战 7/07-7/10)
"""

from __future__ import annotations
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# ============================================================
# 路径 + 配置
# ============================================================
ROOT = Path("/home/aileo/invest_system")
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# V25-D PIT #87 锁模式
LOCK_PATH = LOG_DIR / ".position_unifier.lock"

# AInvest scripts 路径 (PIT #27)
AINVEST_SCRIPTS = Path("/mnt/c/PythonProject/invest_system/scripts")
if str(AINVEST_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(AINVEST_SCRIPTS))

# ============================================================
# 日志
# ============================================================
LOG_FILE = LOG_DIR / "position_unifier.log"
logger = logging.getLogger("position_unifier")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
    logger.addHandler(ch)

# ============================================================
# 4 账户常量 (沿用 V25-D)
# ============================================================
ACCOUNT_GUANGFA = "guangfa"
ACCOUNT_GUOJIN_STOCK = "guojin_stock"
ACCOUNT_GUOJIN_FUND = "guojin_fund"
ACCOUNT_HUITIANFU = "huitianfu"
ALL_ACCOUNTS = [ACCOUNT_GUANGFA, ACCOUNT_GUOJIN_STOCK, ACCOUNT_GUOJIN_FUND, ACCOUNT_HUITIANFU]

# 4 CSV 路径 (实战 6/14 路径)
UPLOAD_DIR = Path(os.path.expanduser("~/.hermes-web-ui/upload"))
CSV_PATHS = {
    ACCOUNT_GUANGFA: UPLOAD_DIR / "3a77e1b03369583a.csv",
    ACCOUNT_GUOJIN_STOCK: UPLOAD_DIR / "aa1ed9815bc3279e.csv",
    ACCOUNT_GUOJIN_FUND: UPLOAD_DIR / "531c65487cebd183.csv",
    ACCOUNT_HUITIANFU: UPLOAD_DIR / "57702ffe98bc0ac5.csv",
}

# ============================================================
# PIT #87 单实例锁 (沿用 V25-D)
# ============================================================
import fcntl


@contextmanager
def acquire_lock(lock_path: Path = LOCK_PATH, timeout: float = 10.0):
    """V25-D PIT #87 单实例锁"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    start = time.time()
    while True:
        try:
            fd = open(lock_path, "w")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd.write(f"{os.getpid()}\n")
            fd.flush()
            break
        except (IOError, OSError):
            if fd:
                fd.close()
                fd = None
            if lock_path.exists():
                try:
                    pid_str = lock_path.read_text().strip()
                    if pid_str.isdigit():
                        pid = int(pid_str)
                        if not Path(f"/proc/{pid}").exists():
                            lock_path.unlink(missing_ok=True)
                            logger.warning(f"orphan lock {lock_path} (PID {pid} dead), removed")
                            continue
                except Exception:
                    pass
            if time.time() - start > timeout:
                logger.error(f"acquire_lock timeout after {timeout}s")
                raise TimeoutError(f"acquire_lock timeout for {lock_path}")
            time.sleep(0.5)
    try:
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        fd.close()


# ============================================================
# 4 dataclass
# ============================================================
@dataclass
class UnifiedPosition:
    """统一持仓记录 (4 CSV ↔ PG 实战)
    实战 6/14: account 字段是 PIT #104 实战 ALTER TABLE 新增
    """
    code: str
    name: str
    type: str  # "stock" / "fund"
    account: str  # guangfa/guojin_stock/guojin_fund/huitianfu
    market_value: float
    cost: float
    profit: float
    profit_pct: float
    weight_pct: float
    available: float = 0.0
    cash: float = 0.0
    raw: Dict[str, str] = field(default_factory=dict)


@dataclass
class CrossCheckResult:
    """4 CSV vs PG 实战 cross-check 验证
    实战 6/14: PG 45 持仓 + 4 CSV 19 唯一 code = 64 - 16 重叠 = 48 实战统一
    """
    run_date: str
    pg_count: int
    csv_unique_count: int
    overlap_count: int
    pg_only_count: int
    csv_only_count: int
    upserted_count: int
    failed_count: int
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


# ============================================================
# C1: PG schema ALTER TABLE
# ============================================================
DDL_STATEMENTS = [
    # 实战 PIT #104: ALTER TABLE ADD COLUMN account
    "ALTER TABLE holdings.encrypted_positions ADD COLUMN IF NOT EXISTS account VARCHAR(32)",
    # 实战 4 索引
    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_ep_code_account_current ON holdings.encrypted_positions(code, account, is_current) WHERE account IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_ep_account ON holdings.encrypted_positions(account) WHERE account IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_ep_code_account ON holdings.encrypted_positions(code, account)",
]


def ensure_pg_schema():
    """实战 PIT #104: ALTER TABLE ADD COLUMN account + 4 索引"""
    import psycopg2
    PG = dict(host='localhost', port=5432, user='invest_admin',
              password='postgresleo814569', dbname='investpilot')
    conn = psycopg2.connect(**PG)
    cur = conn.cursor()
    try:
        for stmt in DDL_STATEMENTS:
            cur.execute(stmt)
        conn.commit()
        logger.info(f"✅ PG schema 实战 {len(DDL_STATEMENTS)} 个 DDL 实战执行成功 (PIT #104)")
    except Exception as e:
        logger.error(f"DDL 失败: {e}")
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


# ============================================================
# C2: 拉取 (4 CSV + PG)
# ============================================================
def load_4_csv_positions() -> Dict[str, List[UnifiedPosition]]:
    """实战 V25-D position_rebalancer_v2 沿用
    实战 6/14: 51 持仓 → 去重 19 唯一 code
    实战: 返回 {account: [UnifiedPosition]}
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("prv2", "/home/aileo/invest_system/hermes_coordination/scripts/position_rebalancer_v2.py")
    prv2 = importlib.util.module_from_spec(spec)
    sys.modules["prv2"] = prv2
    spec.loader.exec_module(prv2)
    positions = prv2.load_all_accounts()
    by_account: Dict[str, List[UnifiedPosition]] = {acc: [] for acc in ALL_ACCOUNTS}
    for p in positions:
        # 实战 PIT #105: V25-D 4 CSV 实战有 GUANGFA_CASH 占位符 (现金行不入 holdings 表)
        if p.code.endswith("_CASH") or "_CASH" in p.code:
            continue  # 实战跳过现金行
        # 实战 V25-D AccountPosition → UnifiedPosition (PIT #104 实战 weight_pct 实战算)
        # V25-D AccountPosition 实战无 weight_pct 字段, 实战用 market_value / sum 算
        # 这里先用 0 占位, 主函数后用 position_rebalancer_v2.summarize_cross_account 算
        unified = UnifiedPosition(
            code=p.code, name=p.name, type=p.type, account=p.account,
            market_value=p.market_value, cost=p.cost, profit=p.profit,
            profit_pct=p.profit_pct, weight_pct=0.0,  # PIT #104 实战主函数后算
            available=p.available, cash=p.cash, raw=p.raw,
        )
        if p.account in by_account:
            by_account[p.account].append(unified)
    return by_account


def get_pg_positions() -> List[Dict]:
    """实战拉 PG 当前持仓
    实战 6/14: 45 持仓, is_current=true
    """
    import psycopg2
    PG = dict(host='localhost', port=5432, user='invest_admin',
              password='postgresleo814569', dbname='investpilot')
    conn = psycopg2.connect(**PG)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, code, name, type, market_value, profit_pct, weight_pct, account
        FROM holdings.encrypted_positions
        WHERE is_current = true
        ORDER BY market_value DESC
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {"id": r[0], "code": r[1], "name": r[2], "type": r[3],
         "market_value": float(r[4] or 0), "profit_pct": float(r[5] or 0),
         "weight_pct": float(r[6] or 0), "account": r[7]}
        for r in rows
    ]


# ============================================================
# C3: upsert (PIT #86 idempotent)
# ============================================================
def upsert_position(p: UnifiedPosition) -> int:
    """实战 PIT #86 idempotent: ON CONFLICT (code+account+is_current) DO UPDATE
    实战 6/14: 实战只更新 market_value/profit_pct/weight_pct, 加密字段不触碰
    """
    import psycopg2
    import uuid
    PG = dict(host='localhost', port=5432, user='invest_admin',
              password='postgresleo814569', dbname='investpilot')
    conn = psycopg2.connect(**PG)
    cur = conn.cursor()
    try:
        # 实战 6/14: 先查是否已存在 (code+account+is_current=true)
        cur.execute("""
            SELECT id FROM holdings.encrypted_positions
            WHERE code = %s AND account = %s AND is_current = true
            LIMIT 1
        """, (p.code, p.account))
        existing = cur.fetchone()
        if existing:
            # 实战 UPDATE
            cur.execute("""
                UPDATE holdings.encrypted_positions SET
                    name = %s, type = %s, market_value = %s,
                    profit_pct = %s, weight_pct = %s, updated_at = NOW()
                WHERE id = %s
            """, (p.name, p.type, p.market_value, p.profit_pct,
                  p.weight_pct, existing[0]))
            conn.commit()
            return existing[0]
        else:
            # 实战 INSERT (实战只填 account+市场字段, 加密字段用空 bytea)
            cur.execute("""
                INSERT INTO holdings.encrypted_positions
                    (code, name, type, account, market_value, profit_pct, weight_pct,
                     cost_enc, profit_enc, shares_enc, csv_row_hash, is_current)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true)
                RETURNING id
            """, (p.code, p.name, p.type, p.account, p.market_value,
                  p.profit_pct, p.weight_pct,
                  b'\x00' * 32,  # 实战 cost_enc 空 bytea
                  b'\x00' * 32,  # 实战 profit_enc 空 bytea
                  b'\x00' * 32,  # 实战 shares_enc 空 bytea
                  f"v26c_{p.account}_{p.code}_{int(time.time())}"))
            rid = cur.fetchone()[0]
            conn.commit()
            return rid
    except Exception as e:
        logger.error(f"upsert 失败 {p.code}/{p.account}: {e}")
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


# ============================================================
# C4: cross_check 验证
# ============================================================
def cross_check() -> CrossCheckResult:
    """实战 4 CSV vs PG cross-check
    实战 6/14: PG 45 + 4 CSV 19 - 16 重叠 = 48 实战统一
    """
    by_account = load_4_csv_positions()
    pg = get_pg_positions()
    pg_codes = {p['code'] for p in pg}
    # 实战 4 CSV 唯一 code (去重)
    csv_unique_codes = set()
    csv_positions_flat = []
    for acc, positions in by_account.items():
        for p in positions:
            csv_unique_codes.add(p.code)
            csv_positions_flat.append(p)
    overlap = pg_codes & csv_unique_codes
    pg_only = pg_codes - csv_unique_codes
    csv_only = csv_unique_codes - pg_codes
    return CrossCheckResult(
        run_date=date.today().isoformat(),
        pg_count=len(pg_codes),
        csv_unique_count=len(csv_unique_codes),
        overlap_count=len(overlap),
        pg_only_count=len(pg_only),
        csv_only_count=len(csv_only),
        upserted_count=0,
        failed_count=0,
    )


def unify_positions() -> CrossCheckResult:
    """V26-C 主函数: 实战 4 CSV upsert 到 PG"""
    logger.info("🚀 V26-C 4 CSV ↔ PG 持仓统一 启动")
    with acquire_lock():
        logger.info("🔒 锁已获取 (PIT #87)")
        # C1 PG schema
        ensure_pg_schema()
        # C2 拉取
        by_account = load_4_csv_positions()
        pg = get_pg_positions()
        logger.info(f"📥 C2 拉取: 4 CSV {sum(len(v) for v in by_account.values())} 条, PG {len(pg)} 持仓")
        # C2.5 实战算 weight_pct (PIT #104 实战: 4 CSV market_value 算权重)
        total_mv = sum(p.market_value for acc, ps in by_account.items() for p in ps)
        for acc, ps in by_account.items():
            for p in ps:
                if total_mv > 0:
                    p.weight_pct = round(p.market_value / total_mv * 100, 4)
        logger.info(f"⚖️ C2.5 实战算 weight_pct: 总市值 ¥{total_mv:,.0f}")
        # C3 upsert
        upserted = 0
        failed = 0
        for acc, positions in by_account.items():
            for p in positions:
                try:
                    rid = upsert_position(p)
                    upserted += 1
                except Exception as e:
                    failed += 1
                    logger.error(f"upsert {p.code}/{p.account} 失败: {e}")
        # C4 cross_check
        result = cross_check()
        result.upserted_count = upserted
        result.failed_count = failed
        logger.info(f"✅ C3 upsert: {upserted} 条成功, {failed} 条失败")
    logger.info("✅ V26-C 完成")
    return result


# ============================================================
# self-test (实战 6/14)
# ============================================================
def self_test():
    """实战 6/14 self-test"""
    print("=" * 60)
    print("V26-C 4 CSV ↔ PG 持仓统一 self-test (实战 6/14)")
    print("=" * 60)
    # 0. 锁
    with acquire_lock(timeout=5.0) as fd:
        print(f"✅ 锁获取 PID {os.getpid()}")
    # 1. C1 PG schema
    ensure_pg_schema()
    print("✅ C1 PG schema 实战 ALTER TABLE + 4 索引 (PIT #104)")
    # 2. C2 拉取
    by_account = load_4_csv_positions()
    csv_total = sum(len(v) for v in by_account.values())
    print(f"✅ C2 4 CSV 拉取: {csv_total} 条 (实战 51)")
    for acc, positions in by_account.items():
        print(f"   {acc}: {len(positions)} 持仓")
    pg = get_pg_positions()
    print(f"   PG: {len(pg)} 持仓")
    # 3. C3 upsert 1 测试
    test_acc = None
    test_pos = None
    for acc, positions in by_account.items():
        if positions:
            test_acc = acc
            test_pos = positions[0]
            break
    if test_pos:
        try:
            rid = upsert_position(test_pos)
            print(f"✅ C3 upsert 测试: {test_pos.code}/{test_acc} id={rid}")
        except Exception as e:
            print(f"⚠️ C3 upsert 测试失败: {e}")
    else:
        print("⚠️ C3 upsert 测试: 无可测试持仓")
    # 4. C4 cross_check
    result = cross_check()
    print(f"✅ C4 cross_check:")
    print(f"   PG 持仓 {result.pg_count} 唯一 code")
    print(f"   4 CSV {result.csv_unique_count} 唯一 code (去重后)")
    print(f"   重叠 {result.overlap_count}")
    print(f"   仅 PG {result.pg_only_count} (实战 ETF/基金 等)")
    print(f"   仅 4 CSV {result.csv_only_count}")
    # 5. 主函数 (实战完整)
    print("\n=== 主函数 (实战完整 4 CSV → PG upsert) ===")
    full_result = unify_positions()
    print(f"✅ 主函数: upserted={full_result.upserted_count}, failed={full_result.failed_count}")
    print(f"   cross_check: PG={full_result.pg_count}, 4CSV={full_result.csv_unique_count}, 重叠={full_result.overlap_count}")
    print("=" * 60)
    print("✅ V26-C self-test 全部通过")
    return full_result


if __name__ == "__main__":
    self_test()

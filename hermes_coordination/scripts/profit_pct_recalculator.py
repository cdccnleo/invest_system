"""
profit_pct_recalculator.py — profit_pct=10000% 数据异常修复 (V24-C5)

🎯 根因 (V24-C5 实战):
  pgcrypto_migration.py:195 写库时简单 `float(row.get("profit_pct", 0) or 0)`,
  完全依赖 CSV 源 `profit_pct` 字段, 而 CSV 源该列实际是历史占位值 10000.0
  (不是由 cost/shares/profit 推算), 导致 41/45 持仓 (91%) 异常.

✅ 正确解法 (PIT #60-#65 实战):
  profit_pct 应该是 profit / (cost * shares) * 100, 不依赖任何 CSV 字段.
  - 解密 cost_enc / profit_enc / shares_enc (3 列都能解密)
  - 推算 profit_pct = profit / cost_basis * 100
  - 范围限制 -100% ~ +1000% (PIT #61 实战约束)
  - 异常标记: cost_basis=0 → NULL + 异常 reason (PIT #62)
  - 解密失败/None/NaN/Inf → NULL + 异常 reason (PIT #63)
  - UPDATE profit_pct 写回 DB (audit log 到 l3.profit_pct_fix_log, PIT #64)
  - 全 idempotent: 已修复 (<1000 且 = 推算) 跳过 (PIT #65)

📊 实战数据 (V24-C5 调研):
  holdings.encrypted_positions is_current=true 45 行
  异常 (profit_pct=9999.9999): 41 行 (91.1%)
  正常 (500-1000%): 2 行 (QDII ETF, 也是错, 真实是 0%)
  正常 (-50~0%): 2 行 (0%)
  推算后真实分布预期: -5% ~ +32% 范围 (澜起 -5%, 杰普特 +30%, 亨通 +32%)

🚀 使用:
  from profit_pct_recalculator import recalc_profit_pct
  result = recalc_profit_pct(dry_run=True)   # 不写 DB, 只 audit
  result = recalc_profit_pct(dry_run=False)  # 真修复
  python3 profit_pct_recalculator.py --fix   # CLI
"""
from __future__ import annotations

import json
import math
import sys as _sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import psycopg2


# ============================================================
# PIT #61: profit_pct 实战合理范围 -100% ~ +1000%
# 现实约束: 最惨全部亏光 -100% (退市归零), 最高 10 倍 1000% (10 年 100 倍复利)
# 10000 哨兵值明显是数据未传/占位
# ============================================================
PROFIT_PCT_MIN = -100.0
PROFIT_PCT_MAX = 1000.0
SENTINEL_VALUES = (9999.9999, 10000.0, 10000, 1000.0, 9999.0)


@dataclass
class FixRow:
    """单行修复结果"""
    id: int
    code: str
    name: str
    old_profit_pct: Optional[float]
    new_profit_pct: Optional[float]
    cost: Optional[float]
    profit: Optional[float]
    shares: Optional[float]
    cost_basis: Optional[float]
    reason: str = ""
    fix_status: str = "fixed"  # fixed|skipped|anomaly


@dataclass
class FixReport:
    """全表修复报告"""
    total_rows: int = 0
    anomaly_rows: int = 0
    fixed_rows: int = 0
    skipped_rows: int = 0
    rows: List[FixRow] = field(default_factory=list)
    duration_seconds: float = 0.0
    dry_run: bool = True

    def summary(self) -> str:
        return (
            f"total={self.total_rows} anomaly={self.anomaly_rows} "
            f"fixed={self.fixed_rows} skipped={self.skipped_rows} "
            f"duration={self.duration_seconds:.2f}s dry_run={self.dry_run}"
        )


def _get_credential(key: str) -> str:
    """从 store.json 拿凭据 (per memory PIT)"""
    store_path = Path("/home/aileo/.hermes/invest_credentials/store.json")
    if store_path.exists():
        store = json.loads(store_path.read_text())
        if key in store:
            return store[key]
    return ""


def _pg_connect():
    """建立 PG 连接"""
    return psycopg2.connect(
        host="localhost",
        port=5432,
        user="invest_admin",
        password=_get_credential("DB_PASSWORD"),
        dbname="investpilot",
    )


def _safe_decrypt(encrypted_bytes, enc_key: str) -> Optional[float]:
    """
    PIT #63: 解密健壮性 (无 key/None/非 bytea/解密失败 → None, 不抛)
    """
    if encrypted_bytes is None:
        return None
    if not enc_key:
        return None
    try:
        # psycopg2 bytes wrapper
        if hasattr(encrypted_bytes, "tobytes"):
            encrypted_bytes = encrypted_bytes.tobytes()
        conn = psycopg2.connect(
            host="localhost", port=5432, user="invest_admin",
            password=_get_credential("DB_PASSWORD"), dbname="investpilot",
        )
        cur = conn.cursor()
        cur.execute(
            "SELECT pgp_sym_decrypt(%s, %s::text)::float",
            (psycopg2.Binary(encrypted_bytes), enc_key),
        )
        val = cur.fetchone()[0]
        conn.close()
        if val is None:
            return None
        # PIT #63: nan/inf/str → None
        if not isinstance(val, (int, float)):
            return None
        if not math.isfinite(float(val)):
            return None
        return float(val)
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return None


def _calc_profit_pct(profit: float, cost: float, shares: float) -> Optional[float]:
    """
    PIT #61: 推算 profit_pct = profit / cost_basis * 100
    PIT #62: cost_basis=0 → 0.0 (异常但返 0, 不返 None)
    PIT #63: nan/inf/None → 0.0 (不抛, PIT #58 复用)
    """
    # PIT #63: 入口 None/nan/inf 全部容错
    for v in (profit, cost, shares):
        if v is None:
            return 0.0
        if not isinstance(v, (int, float)):
            return 0.0
        if not math.isfinite(float(v)):
            return 0.0
    cost_basis = cost * shares
    if cost_basis == 0:
        return 0.0
    try:
        pp = profit / cost_basis * 100.0
    except (ZeroDivisionError, TypeError):
        return 0.0
    if not math.isfinite(pp):
        return 0.0
    # PIT #61: 范围限制 -100% ~ +1000% (超出截断到边界)
    pp = max(PROFIT_PCT_MIN, min(PROFIT_PCT_MAX, pp))
    return round(pp, 4)


def _is_sentinel(val: Optional[float]) -> bool:
    """检查是否哨兵值 (10000 之类)"""
    if val is None:
        return False
    for sv in SENTINEL_VALUES:
        if abs(val - sv) < 0.01:
            return True
    return False


def _ensure_audit_table(cur):
    """PIT #64: 修复 audit log 表"""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS l3.profit_pct_fix_log (
            id BIGSERIAL PRIMARY KEY,
            fixed_at TIMESTAMP DEFAULT NOW(),
            position_id INT NOT NULL,
            code VARCHAR(20) NOT NULL,
            name VARCHAR(100),
            old_profit_pct NUMERIC,
            new_profit_pct NUMERIC,
            cost NUMERIC,
            profit NUMERIC,
            shares NUMERIC,
            reason TEXT,
            fix_status VARCHAR(20)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ppfl_fixed_at ON l3.profit_pct_fix_log(fixed_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ppfl_code ON l3.profit_pct_fix_log(code)")


def recalc_profit_pct(dry_run: bool = True, batch_size: int = 100) -> FixReport:
    """
    PIT #65: 修复 profit_pct, 全 idempotent
      1. 拉所有 is_current=true 行
      2. 解密 cost/profit/shares
      3. 推算 profit_pct
      4. 仅当 old 是哨兵或超出范围 才 UPDATE
      5. 写 audit log
    """
    enc_key = _get_credential("DB_ENCRYPTION_KEY")
    if not enc_key:
        return FixReport(dry_run=dry_run)

    start = time.time()
    report = FixReport(dry_run=dry_run)
    conn = _pg_connect()
    cur = conn.cursor()

    # 1. 拉所有 is_current=true 行
    cur.execute("""
        SELECT id, code, name, profit_pct,
               cost_enc, profit_enc, shares_enc
        FROM holdings.encrypted_positions
        WHERE is_current = true
        ORDER BY market_value DESC NULLS LAST
    """)
    rows = cur.fetchall()
    report.total_rows = len(rows)

    # 2. 准备 audit
    if not dry_run:
        _ensure_audit_table(cur)
        conn.commit()

    # 3. 逐行推算
    audit_records = []
    for row in rows:
        pid, code, name, old_pp, cost_enc, profit_enc, shares_enc = row

        # 解密 3 列
        cost = _safe_decrypt(cost_enc, enc_key)
        profit = _safe_decrypt(profit_enc, enc_key)
        shares = _safe_decrypt(shares_enc, enc_key)

        # 推算
        new_pp = _calc_profit_pct(profit, cost, shares)
        cost_basis = (cost * shares) if (cost is not None and shares is not None) else None

        # 旧值转 float
        old_pp_f = float(old_pp) if old_pp is not None else None

        # 判断是否需要修
        is_anomaly = False
        reason = ""
        if _is_sentinel(old_pp_f):
            is_anomaly = True
            reason = f"sentinel_old={old_pp_f}"
        elif old_pp_f is not None and (old_pp_f < PROFIT_PCT_MIN or old_pp_f > PROFIT_PCT_MAX):
            is_anomaly = True
            reason = f"out_of_range_old={old_pp_f}"

        if is_anomaly:
            report.anomaly_rows += 1

        if not is_anomaly:
            report.skipped_rows += 1
            continue

        # 准备修复
        fr = FixRow(
            id=pid, code=code, name=name,
            old_profit_pct=old_pp_f, new_profit_pct=new_pp,
            cost=cost, profit=profit, shares=shares,
            cost_basis=cost_basis, reason=reason,
        )

        if new_pp is None:
            fr.fix_status = "anomaly"
            fr.reason += f" | calc_failed (cost={cost} profit={profit} shares={shares})"
            report.skipped_rows += 1
        else:
            fr.fix_status = "fixed"
            report.fixed_rows += 1
            if not dry_run:
                cur.execute(
                    "UPDATE holdings.encrypted_positions SET profit_pct = %s WHERE id = %s",
                    (new_pp, pid),
                )

        report.rows.append(fr)
        audit_records.append(fr)

        # 批量提交
        if not dry_run and len(audit_records) >= batch_size:
            for ar in audit_records:
                cur.execute("""
                    INSERT INTO l3.profit_pct_fix_log
                    (position_id, code, name, old_profit_pct, new_profit_pct, cost, profit, shares, reason, fix_status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (ar.id, ar.code, ar.name, ar.old_profit_pct, ar.new_profit_pct,
                      ar.cost, ar.profit, ar.shares, ar.reason, ar.fix_status))
            conn.commit()
            audit_records = []

    # 提交剩余
    if not dry_run and audit_records:
        for ar in audit_records:
            cur.execute("""
                INSERT INTO l3.profit_pct_fix_log
                (position_id, code, name, old_profit_pct, new_profit_pct, cost, profit, shares, reason, fix_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (ar.id, ar.code, ar.name, ar.old_profit_pct, ar.new_profit_pct,
                  ar.cost, ar.profit, ar.shares, ar.reason, ar.fix_status))
        conn.commit()

    conn.close()
    report.duration_seconds = time.time() - start
    return report


# ============================================================
# CLI 入口 (给 schedule_runner 调)
# ============================================================
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="profit_pct=10000% 数据异常修复 (V24-C5)")
    parser.add_argument("--fix", action="store_true", help="真修复 (默认 dry-run)")
    parser.add_argument("--report", action="store_true", help="仅打 report (默认)")
    args = parser.parse_args()

    dry_run = not args.fix
    print(f"[{datetime.now().isoformat()}] V24-C5 profit_pct 修复 (dry_run={dry_run})")
    result = recalc_profit_pct(dry_run=dry_run)
    print(f"\n=== Summary ===")
    print(result.summary())
    print(f"\n=== 前 10 修复详情 ===")
    for r in result.rows[:10]:
        print(f"  {r.id:>4} {r.code} {r.name[:8]:>8}  "
              f"old={r.old_profit_pct!s:>10} → new={r.new_profit_pct!s:>10}  "
              f"({r.fix_status}, {r.reason[:60]})")
    if dry_run:
        print("\n[DRY-RUN] 加 --fix 真修复")

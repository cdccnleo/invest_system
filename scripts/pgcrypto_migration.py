"""
pgcrypto_migration.py — 持仓数据 CSV → PostgreSQL 列级加密迁移
pgcrypto AES-128 列级加密存储 cost/profit/shares
加密密钥: credentials.get_credential("DB_ENCRYPTION_KEY")
"""

import sys, csv, logging, uuid, json
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "scripts")
from credentials import get_credential

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("invest_system.pgcrypto_migration")

POSITIONS_CSV = "/mnt/d/Hold/invest-data/positions.csv"

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS holdings;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS holdings.encrypted_positions (
    id              SERIAL PRIMARY KEY,
    code            VARCHAR(10)  NOT NULL,
    name            VARCHAR(100) NOT NULL,
    type            VARCHAR(20)  NOT NULL DEFAULT 'stock',
    cost_enc        BYTEA        NOT NULL,
    profit_enc      BYTEA        NOT NULL,
    shares_enc      BYTEA        NOT NULL,
    market_value    NUMERIC(16,2),
    close_price     NUMERIC(12,4),
    weight_pct      NUMERIC(8,4),
    profit_pct      NUMERIC(10,4),
    csv_row_hash    VARCHAR(64)  NOT NULL,
    imported_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    trace_id        UUID        NOT NULL DEFAULT gen_random_uuid()
);

CREATE OR REPLACE FUNCTION holdings.prevent_pos_modification()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'holdings.encrypted_positions 是追加表，禁止 UPDATE/DELETE';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS no_pos_modification ON holdings.encrypted_positions;
CREATE TRIGGER no_pos_modification
    BEFORE UPDATE OR DELETE ON holdings.encrypted_positions
    FOR EACH ROW EXECUTE FUNCTION holdings.prevent_pos_modification();

CREATE INDEX IF NOT EXISTS idx_pos_code ON holdings.encrypted_positions(code);
CREATE INDEX IF NOT EXISTS idx_pos_type ON holdings.encrypted_positions(type);
CREATE INDEX IF NOT EXISTS idx_pos_imported ON holdings.encrypted_positions(imported_at);

CREATE TABLE IF NOT EXISTS holdings.migration_log (
    id              SERIAL PRIMARY KEY,
    migrated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rows_total      INT,
    rows_added      INT,
    rows_skipped    INT,
    csv_md5         VARCHAR(64),
    result          VARCHAR(20)
);
"""


_enc_key_cache: str | None = None  # 进程级缓存


def get_encryption_key() -> str:
    """
    优先从凭据文件读取（进程重启后仍复用同一密钥）。
    首次无密钥时生成32字节随机密钥并持久化写入凭据文件。
    """
    global _enc_key_cache
    if _enc_key_cache:
        return _enc_key_cache

    # 从凭据文件读取（持久化存储，进程重启后仍有效）
    cred_file = Path.home() / ".hermes" / "invest_credentials" / "store.json"
    if cred_file.exists():
        try:
            store = json.loads(cred_file.read_text())
            key = store.get("DB_ENCRYPTION_KEY", "")
            if key and len(key) == 64:
                _enc_key_cache = key
                return key
        except Exception:
            pass

    # 无密钥 → 生成并持久化
    import os
    key = os.urandom(32).hex()
    _enc_key_cache = key
    try:
        if cred_file.exists():
            store = json.loads(cred_file.read_text())
        else:
            store = {}
        store["DB_ENCRYPTION_KEY"] = key
        cred_file.parent.mkdir(parents=True, exist_ok=True)
        cred_file.write_text(json.dumps(store, indent=2, ensure_ascii=False))
        os.chmod(str(cred_file), 0o600)
        logger.info(f"DB_ENCRYPTION_KEY 已写入: {cred_file} ({key[:8]}...)")
    except Exception as e:
        logger.warning(f"写入凭据文件失败: {e}")
    return key


def encrypt_value(value: float, key: str) -> bytes:
    import psycopg2
    pwd = get_credential("DB_PASSWORD")
    conn = psycopg2.connect(host="localhost", database="investpilot",
                            user="invest_admin", password=pwd)
    cur = conn.cursor()
    cur.execute("SELECT pgp_sym_encrypt(%s::text, %s::text)", (str(value), key))
    result = cur.fetchone()[0]
    conn.close()
    return psycopg2.Binary(result)


def decrypt_value(encrypted_bytes, key: str) -> float:
    import psycopg2
    pwd = get_credential("DB_PASSWORD")
    conn = psycopg2.connect(host="localhost", database="investpilot",
                            user="invest_admin", password=pwd)
    cur = conn.cursor()
    cur.execute("SELECT pgp_sym_decrypt(%s, %s::text)", (encrypted_bytes, key))
    result = cur.fetchone()[0]
    conn.close()
    return float(result)


def hash_row(row: dict) -> str:
    import hashlib
    key_fields = ["code", "name", "type", "shares", "cost"]
    data = "|".join(str(row.get(k, "")) for k in key_fields)
    return hashlib.sha256(data.encode()).hexdigest()


def ensure_schema(cursor):
    cursor.execute(SCHEMA_SQL)


def migrate_positions_csv():
    import hashlib, psycopg2
    from pathlib import Path

    csv_path = Path(POSITIONS_CSV)
    if not csv_path.exists():
        logger.error(f"持仓 CSV 不存在: {POSITIONS_CSV}")
        return False

    csv_md5 = hashlib.md5(csv_path.read_bytes()).hexdigest()
    pwd = get_credential("DB_PASSWORD")
    enc_key = get_encryption_key()

    conn = psycopg2.connect(host="localhost", database="investpilot",
                            user="invest_admin", password=pwd)
    conn.autocommit = False
    cur = conn.cursor()

    logger.info("创建持仓加密表...")
    ensure_schema(cur)

    with open(POSITIONS_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    logger.info(f"CSV 共 {len(rows)} 行，开始迁移...")

    cur.execute("SELECT csv_row_hash FROM holdings.encrypted_positions")
    existing_hashes = {r[0] for r in cur.fetchall()}

    rows_added = 0
    rows_skipped = 0
    trace_id = str(uuid.uuid4())

    for row in rows:
        try:
            code = str(row.get("code", "")).zfill(6)
            name = row.get("name", "")
            pos_type = row.get("type", "stock")
            shares = float(row.get("shares", 0) or 0)
            cost = float(row.get("cost", 0) or 0)
            market_value = float(row.get("market_value", 0) or 0)
            close_price = float(row.get("close", row.get("current_price", 0) or 0))
            profit = float(row.get("profit", 0) or 0)
            profit_pct = float(row.get("profit_pct", 0) or 0)
            weight = float(row.get("weight", 0) or 0)
            row_hash = hash_row(row)

            if row_hash in existing_hashes:
                rows_skipped += 1
                continue

            cost_enc = encrypt_value(cost, enc_key)
            profit_enc = encrypt_value(profit, enc_key)
            shares_enc = encrypt_value(shares, enc_key)

            cur.execute(
                "INSERT INTO holdings.encrypted_positions "
                "(code,name,type,cost_enc,profit_enc,shares_enc,"
                "market_value,close_price,weight_pct,profit_pct,csv_row_hash,trace_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (code, name, pos_type, cost_enc, profit_enc, shares_enc,
                 market_value, close_price, weight, profit_pct,
                 row_hash, trace_id)
            )
            rows_added += 1

        except Exception as e:
            logger.warning(f"行迁移失败 [{row.get('code')}]: {e}")

    conn.commit()

    result = "SUCCESS" if rows_added > 0 else "NOCHANGE"
    cur.execute(
        "INSERT INTO holdings.migration_log (rows_total,rows_added,rows_skipped,csv_md5,result) "
        "VALUES (%s,%s,%s,%s,%s)",
        (len(rows), rows_added, rows_skipped, csv_md5, result)
    )
    conn.commit()
    conn.close()

    logger.info(f"迁移完成: 新增={rows_added}, 跳过(已存在)={rows_skipped}")
    return True


def load_positions_from_db() -> list[dict]:
    """
    从 holdings.encrypted_positions 读取并解密持仓数据。
    通过 PostgreSQL 批量解密函数，一句 SQL 返回所有明文。
    """
    import psycopg2

    enc_key = get_encryption_key()
    pwd = get_credential("DB_PASSWORD")
    conn = psycopg2.connect(host="localhost", database="investpilot",
                            user="invest_admin", password=pwd)
    cur = conn.cursor()

    # 批量解密所有行（单次 SQL，无循环建连）
    # is_current = TRUE 确保只读当前有效持仓（去重）
    sql = """
        SELECT code, name, type,
               pgp_sym_decrypt(shares_enc, %s::text)::float  AS shares,
               pgp_sym_decrypt(cost_enc,  %s::text)::float  AS cost,
               pgp_sym_decrypt(profit_enc,%s::text)::float  AS profit,
               market_value, close_price, weight_pct, profit_pct
        FROM holdings.encrypted_positions
        WHERE is_current = TRUE
        ORDER BY weight_pct DESC NULLS LAST, code
    """
    cur.execute(sql, (enc_key, enc_key, enc_key))
    rows = cur.fetchall()
    conn.close()

    positions = []
    for r in rows:
        positions.append({
            "code": str(r[0]).zfill(6),
            "name": r[1],
            "type": r[2] or "stock",
            "shares": r[3],
            "cost": r[4],
            "profit": r[5],
            "market_value": float(r[6]) if r[6] else 0.0,
            "close": float(r[7]) if r[7] else r[4],
            "weight": float(r[8]) if r[8] else 0.0,
            "profit_pct": float(r[9]) if r[9] else 0.0,
        })

    logger.info(f"从加密持仓表读取 {len(positions)} 条记录")
    return positions


def verify_migration():
    import psycopg2

    enc_key = get_encryption_key()
    pwd = get_credential("DB_PASSWORD")
    conn = psycopg2.connect(host="localhost", database="investpilot",
                            user="invest_admin", password=pwd)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM holdings.encrypted_positions")
    total = cur.fetchone()[0]
    logger.info(f"加密持仓表共 {total} 条记录")

    if total > 0:
        cur.execute(
            "SELECT code,name,type,shares_enc,cost_enc,profit_enc,market_value "
            "FROM holdings.encrypted_positions LIMIT 3"
        )
        for r in cur.fetchall():
            shares = decrypt_value(r[3], enc_key)
            cost = decrypt_value(r[4], enc_key)
            profit = decrypt_value(r[5], enc_key)
            logger.info(f"  验证 {r[0]} {r[1]}: shares={shares}, cost={cost:.4f}, profit={profit:.2f}, mv={r[6]}")

    conn.close()
    return total > 0


# ── 公司行为：公告触发成本调整 ──────────────────────────────────────────

def update_cost_from_dividend(conn, cur, code: str, dividend_per_share: float):
    """
    分红除权：调整持仓成本
    新成本 = 旧成本 - 分红金额（每股）
    """
    enc_key = get_encryption_key()
    cur.execute("""
        SELECT shares_enc, cost_enc FROM holdings.encrypted_positions
        WHERE REPLACE(REPLACE(REPLACE(code, '.SH', ''), '.SZ', ''), '.XSHE', '')
              = REPLACE(REPLACE(REPLACE(%s, '.SH', ''), '.SZ', ''), '.XSHE', '')
    """, (code,))
    row = cur.fetchone()
    if not row:
        return False

    # 解密
    cur.execute("SELECT pgp_sym_decrypt(%s,%s::text)::float, pgp_sym_decrypt(%s,%s::text)::float",
                (row[0], enc_key, row[1], enc_key))
    shares, cost = cur.fetchone()
    new_cost = cost - dividend_per_share
    if new_cost <= 0:
        new_cost = 0.001

    # 重新加密写回
    cur.execute("""
        UPDATE holdings.encrypted_positions
        SET cost_enc = pgp_sym_encrypt(%s::text, %s::text)
        WHERE REPLACE(REPLACE(REPLACE(code, '.SH', ''), '.SZ', ''), '.XSHE', '')
              = REPLACE(REPLACE(REPLACE(%s, '.SH', ''), '.SZ', ''), '.XSHE', '')
    """, (str(new_cost), enc_key, code))
    logger.info(f"  ✅ 分红调整: {code} 成本 {cost:.4f} → {new_cost:.4f}")
    return True


def update_shares_from_bonus(conn, cur, code: str, bonus_ratio: float):
    """
    送股：调整持仓数量
    新股数 = 旧股数 × (1 + 送股比例)
    """
    enc_key = get_encryption_key()
    cur.execute("""
        SELECT shares_enc FROM holdings.encrypted_positions
        WHERE REPLACE(REPLACE(REPLACE(code, '.SH', ''), '.SZ', ''), '.XSHE', '')
              = REPLACE(REPLACE(REPLACE(%s, '.SH', ''), '.SZ', ''), '.XSHE', '')
    """, (code,))
    row = cur.fetchone()
    if not row:
        return False

    cur.execute("SELECT pgp_sym_decrypt(%s,%s::text)::float", (row[0], enc_key))
    shares = cur.fetchone()[0]
    new_shares = shares * (1 + bonus_ratio)

    cur.execute("""
        UPDATE holdings.encrypted_positions
        SET shares_enc = pgp_sym_encrypt(%s::text, %s::text)
        WHERE REPLACE(REPLACE(REPLACE(code, '.SH', ''), '.SZ', ''), '.XSHE', '')
              = REPLACE(REPLACE(REPLACE(%s, '.SH', ''), '.SZ', ''), '.XSHE', '')
    """, (str(new_shares), enc_key, code))
    logger.info(f"  ✅ 送股调整: {code} 份额 {shares:.0f} → {new_shares:.0f} (+{bonus_ratio:.0%})")
    return True


def process_corp_actions(announcements: list[dict]) -> dict:
    """
    根据公告列表处理公司行为（分红、送股）并更新加密持仓。
    announcements: [{ts_code, title, ann_type, notice_date}, ...]
    返回: {processed, dividend, bonus, skipped, errors}
    """
    import re, psycopg2
    from credentials import get_credential

    result = {"processed": 0, "dividend": 0, "bonus": 0, "skipped": 0, "errors": []}
    pwd = get_credential("DB_PASSWORD")
    conn = psycopg2.connect(host="localhost", database="investpilot",
                            user="invest_admin", password=pwd)
    cur = conn.cursor()

    DIVIDEND_RE = re.compile(r"每股派息|每股分红|每10股派|分红派息|权益分派", re.I)
    BONUS_RE = re.compile(r"每10股送\d|送股|转增", re.I)

    for ann in announcements:
        ann_type = ann.get("ann_type", "")
        title = ann.get("title", "")
        code = str(ann.get("ts_code", "")).replace(".SH", "").replace(".SZ", "").replace(".XSHE", "")

        try:
            # 分红
            if "分红" in ann_type or DIVIDEND_RE.search(title):
                m = re.search(r"每股派息?[（(]?([\d.]+)元?|每10股派([\d.]+)元|每股分红([\d.]+)",
                              title, re.I)
                if m:
                    amount = float(m.group(1) or m.group(2) or m.group(3) or 0)
                    if amount > 0 and update_cost_from_dividend(conn, cur, code, amount):
                        result["dividend"] += 1
                        result["processed"] += 1
                        continue

            # 送股
            if "送股" in ann_type or BONUS_RE.search(title):
                m = re.search(r"每10股送(\d+)", title)
                if m:
                    bonus_ratio = int(m.group(1)) / 10.0
                    if update_shares_from_bonus(conn, cur, code, bonus_ratio):
                        result["bonus"] += 1
                        result["processed"] += 1
                        continue

            result["skipped"] += 1

        except Exception as e:
            result["errors"].append(f"{code}: {e}")

    conn.commit()
    cur.close()
    conn.close()
    logger.info(f"公司行为处理: 分红{result['dividend']}笔 送股{result['bonus']}笔 跳过{result['skipped']}笔")
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="持仓 CSV → PostgreSQL pgcrypto 加密迁移")
    parser.add_argument("--verify-only", action="store_true", help="仅验证已有数据")
    args = parser.parse_args()

    if args.verify_only:
        ok = verify_migration()
        sys.exit(0 if ok else 1)
    else:
        ok = migrate_positions_csv()
        if ok:
            verify_migration()
        sys.exit(0 if ok else 1)
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrate_trading_positions.py — trading.positions 加密列重建迁移
================================================================
问题：trading.positions 表的 _enc 列使用旧密钥加密，无法用当前 DB_ENCRYPTION_KEY 解密。
方案：用当前密钥重新加密明文数据 → 更新 _enc 列 → 置空明文列。

执行：python migrate_trading_positions.py --dry-run  （先看 SQL）
      python migrate_trading_positions.py          （执行迁移）
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import psycopg2


def migrate(conn, enc_key: str, dry_run: bool = False):
    """重建 trading.positions 的加密列"""
    cur = conn.cursor()

    # Step 1: 确认明文列有数据
    cur.execute("SELECT COUNT(*) FROM trading.positions WHERE avg_cost IS NOT NULL")
    plain_count = cur.fetchone()[0]
    print(f"明文列有数据的行数: {plain_count}")

    if plain_count == 0:
        print("无需迁移，明文列为空")
        return

    # Step 2: 获取所有行
    cur.execute("""
        SELECT code, name,
               shares::text, avg_cost::text, profit_loss::text, profit_pct::text,
               market_value, close_price, weight_pct, position_type
        FROM trading.positions
        WHERE avg_cost IS NOT NULL
        ORDER BY code
    """)
    rows = cur.fetchall()
    print(f"待迁移总行数: {len(rows)}")

    if dry_run:
        print("\n=== DRY RUN — 以下 SQL 将执行 ===")
        for r in rows[:5]:
            code = r[0]
            shares, avg_cost = r[2], r[3]
            print(f"  [{code}] shares={shares}, avg_cost={avg_cost}, profit_loss={r[4]}")
        if len(rows) > 5:
            print(f"  ... 还有 {len(rows)-5} 行")
        return

    # Step 3: 对每行重新加密并更新
    success = 0
    failed = []

    for r in rows:
        code, _name = r[0], r[1]
        shares_txt, avg_cost_txt = r[2], r[3]
        profit_loss_txt, _profit_pct_txt = r[4], r[5]
        _market_value, _close_price = r[6], r[7]
        _weight_pct, _position_type = r[8], r[9]

        try:
            # 重新加密（使用当前密钥）
            cur.execute("SELECT pgp_sym_encrypt(%s::text, %s::text)", (shares_txt, enc_key))
            shares_enc = cur.fetchone()[0]
            cur.execute("SELECT pgp_sym_encrypt(%s::text, %s::text)", (avg_cost_txt, enc_key))
            cost_enc = cur.fetchone()[0]
            cur.execute("SELECT pgp_sym_encrypt(%s::text, %s::text)", (profit_loss_txt, enc_key))
            profit_enc = cur.fetchone()[0]

            # 更新：将明文写入加密列，然后置空明文列
            cur.execute("""
                UPDATE trading.positions
                SET shares_enc = %s,
                    avg_cost_enc = %s,
                    profit_loss_enc = %s,
                    shares = NULL,
                    avg_cost = NULL,
                    profit_loss = NULL,
                    profit_pct = NULL,
                    updated_at = NOW()
                WHERE code = %s
            """, (
                psycopg2.Binary(shares_enc),
                psycopg2.Binary(cost_enc),
                psycopg2.Binary(profit_enc),
                code,
            ))
            conn.commit()
            success += 1
            print(f"  ✅ [{code}] 迁移成功")

        except Exception as e:
            conn.rollback()
            failed.append((code, str(e)))
            print(f"  ❌ [{code}] 失败: {e}")

    print(f"\n迁移完成: {success} 成功, {len(failed)} 失败")
    if failed:
        print("失败列表:")
        for code, err in failed:
            print(f"  [{code}]: {err}")


def verify(conn, enc_key: str):
    """验证迁移结果：解密后的值应与原明文一致"""
    cur = conn.cursor()

    # 取一条记录验证
    cur.execute("""
        SELECT code,
               pgp_sym_decrypt(shares_enc::bytea, %s::text)::text,
               pgp_sym_decrypt(avg_cost_enc::bytea, %s::text)::text,
               pgp_sym_decrypt(profit_loss_enc::bytea, %s::text)::text,
               shares, avg_cost, profit_loss
        FROM trading.positions
        WHERE shares_enc IS NOT NULL
        LIMIT 5
    """, (enc_key, enc_key, enc_key))

    rows = cur.fetchall()
    print("\n=== 验证（前5条）===")
    for r in rows:
        code = r[0]
        _dec_shares, dec_cost, _dec_profit = r[1], r[2], r[3]
        plain_shares, _plain_cost, _plain_profit = r[4], r[5], r[6]

        # plaintext should now be NULL (migrated)
        plain_status = "NULL ✅" if plain_shares is None else f"仍存在: {plain_shares}"

        # decrypted values should be valid numbers
        try:
            dec_cost_val = float(dec_cost)
            print(f"  [{code}] 解密后 cost={dec_cost_val:.4f} | 明文列={plain_status}")
        except Exception as e:
            print(f"  [{code}] 解密失败: {e} | {plain_status}")

    # 统计
    cur.execute("""
        SELECT
            COUNT(*) as total,
            COUNT(shares_enc) as has_enc,
            COUNT(shares) as has_plain_shares,
            COUNT(avg_cost) as has_plain_cost
        FROM trading.positions
    """)
    stats = cur.fetchone()
    print(f"\n统计: 总行={stats[0]}, 有加密列={stats[1]}, 有明文shares={stats[2]}, 有明文cost={stats[3]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="迁移 trading.positions 加密列")
    parser.add_argument("--dry-run", action="store_true", help="仅显示将执行的 SQL，不写入数据库")
    parser.add_argument("--verify", action="store_true", help="验证迁移结果")
    args = parser.parse_args()

    # 读取密钥和密码
    import json

    cred_file = Path.home() / ".hermes" / "invest_credentials" / "store.json"
    store = json.loads(cred_file.read_text())
    enc_key = store["DB_ENCRYPTION_KEY"]
    db_pwd = store["DB_PASSWORD"]

    DB = dict(host="localhost", database="investpilot", user="invest_admin", password=db_pwd)
    conn = psycopg2.connect(**DB)

    try:
        if args.verify:
            verify(conn, enc_key)
        else:
            migrate(conn, enc_key, dry_run=args.dry_run)
            if not args.dry_run:
                verify(conn, enc_key)
    finally:
        conn.close()

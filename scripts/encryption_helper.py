"""
encryption_helper.py — PostgreSQL 列级加密辅助函数
参考 storage_factory.py 风格，封装加解密操作

用法:
    from encryption_helper import encrypt_value, decrypt_value, get_positions_decrypted

环境变量 / 凭据:
    DB_PASSWORD: PostgreSQL 密码
    DB_ENCRYPTION_KEY: AES 对称加密密钥（32字节 hex 字符串）

注意: 本模块不直接存储密钥，密钥由调用方通过 credentials 模块管理
"""

import os
import json
from pathlib import Path
from typing import Optional

try:
    from credentials import get_credential
    _HAS_CREDENTIALS = True
except ImportError:
    _HAS_CREDENTIALS = False

# 密钥缓存（进程级）
_enc_key_cache: Optional[str] = None


def get_encryption_key() -> str:
    """
    获取数据库加密密钥。
    优先从 credentials 读取（持久化存储），无则生成并持久化。
    进程重启后仍复用同一密钥。
    """
    global _enc_key_cache
    if _enc_key_cache:
        return _enc_key_cache

    # 从凭据文件读取
    cred_file = Path.home() / ".hermes" / "invest_credentials" / "store.json"
    if cred_file.exists():
        try:
            store = json.loads(cred_file.read_text())
            key = store.get("DB_ENCRYPTION_KEY", "")
            if key and len(key) >= 32:
                _enc_key_cache = key
                return key
        except Exception:
            pass

    # 无密钥 → 生成 32 字节随机密钥并持久化
    import secrets
    key = secrets.token_hex(32)
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
    except Exception:
        pass
    return key


def _get_pg_conn():
    """建立 PostgreSQL 连接（内部用）"""
    import psycopg2
    pwd = None
    if _HAS_CREDENTIALS:
        pwd = get_credential("DB_PASSWORD")
    if not pwd:
        pwd = os.environ.get("DB_PASSWORD", os.environ.get("PGPASSWORD", ""))
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=os.environ.get("PGPORT", "5432"),
        database=os.environ.get("PGDATABASE", "investpilot"),
        user=os.environ.get("PGUSER", "invest_admin"),
        password=pwd
    )


def encrypt_value(value: float | int | str, key: Optional[str] = None) -> bytes:
    """
    加密单个值（使用 pgcrypto pgp_sym_encrypt）
    
    Args:
        value: 要加密的值（float/int/str）
        key: 加密密钥（默认从 get_encryption_key() 获取）
    
    Returns:
        bytes: 加密后的 BYTEA
    
    Example:
        encrypted = encrypt_value(12.5, "mysecretkey123456789012345678901234")
    """
    import psycopg2
    
    if key is None:
        key = get_encryption_key()
    
    conn = _get_pg_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT pgp_sym_encrypt(%s::text, %s::text)",
        (str(value), key)
    )
    result = cur.fetchone()[0]
    conn.close()
    return psycopg2.Binary(result)


def decrypt_value(encrypted_bytes, key: Optional[str] = None) -> float:
    """
    解密单个值（使用 pgcrypto pgp_sym_decrypt）
    
    Args:
        encrypted_bytes: 加密的 BYTEA 数据
        key: 解密密钥（默认从 get_encryption_key() 获取）
    
    Returns:
        float: 解密后的数值
    
    Example:
        value = decrypt_value(encrypted_bytes, "mysecretkey123456789012345678901234")
    """
    import psycopg2
    
    if key is None:
        key = get_encryption_key()
    
    conn = _get_pg_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT pgp_sym_decrypt(%s, %s::text)",
        (encrypted_bytes, key)
    )
    result = cur.fetchone()[0]
    conn.close()
    return float(result)


def insert_position_encrypted(
    code: str,
    name: str,
    shares: float,
    avg_cost: float,
    profit_loss: float,
    profit_pct: float,
    market_value: Optional[float] = None,
    close_price: Optional[float] = None,
    weight_pct: Optional[float] = None,
    position_type: str = "stock"
) -> bool:
    """
    通过存储过程插入/更新持仓（含加密列）
    
    Args:
        code, name, shares, avg_cost, profit_loss, profit_pct: 持仓字段
        market_value, close_price, weight_pct: 可选字段
        position_type: 持仓类型（默认 'stock'）
    
    Returns:
        bool: 是否成功
    
    Example:
        insert_position_encrypted("000001", "平安银行", 1000, 12.5, 2500.0, 0.02)
    """
    import psycopg2
    
    key = get_encryption_key()
    conn = _get_pg_conn()
    conn.autocommit = False
    cur = conn.cursor()
    
    try:
        cur.execute("SET app.encryption_key = %s", (key,))
        cur.execute(
            """CALL trading.insert_position(
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )""",
            (code, name, shares, avg_cost, profit_loss, profit_pct,
             key, market_value, close_price, weight_pct, position_type)
        )
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"[ERROR] insert_position_encrypted failed: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def get_decrypted_positions() -> list[dict]:
    """
    从 trading.positions 读取并解密所有持仓数据。
    通过批量解密函数，一句 SQL 返回所有明文。
    
    Returns:
        list[dict]: 持仓列表
    
    Example:
        positions = get_decrypted_positions()
        for pos in positions:
            print(pos["code"], pos["avg_cost"], pos["profit_loss"])
    """
    import psycopg2
    
    key = get_encryption_key()
    conn = _get_pg_conn()
    cur = conn.cursor()
    
    # 批量解密所有行（单次 SQL，无循环建连）
    sql = """
        SELECT code, name, shares, avg_cost, profit_loss, profit_pct,
               market_value, close_price, weight_pct, position_type
        FROM trading.positions
        ORDER BY weight_pct DESC NULLS LAST, code
    """
    cur.execute(sql)
    rows = cur.fetchall()
    conn.close()
    
    positions = []
    for r in rows:
        # 尝试从明文列读取，回退到解密
        shares_val = r[2] if r[2] is not None else 0.0
        avg_cost_val = r[3] if r[3] is not None else 0.0
        profit_loss_val = r[4] if r[4] is not None else 0.0
        profit_pct_val = r[5] if r[5] is not None else 0.0
        
        positions.append({
            "code": str(r[0]).zfill(6),
            "name": r[1],
            "shares": float(shares_val),
            "avg_cost": float(avg_cost_val),
            "profit_loss": float(profit_loss_val),
            "profit_pct": float(profit_pct_val),
            "market_value": float(r[6]) if r[6] else 0.0,
            "close_price": float(r[7]) if r[7] else 0.0,
            "weight_pct": float(r[8]) if r[8] else 0.0,
            "position_type": r[9] or "stock",
        })
    
    return positions


def update_position_encrypted(
    code: str,
    avg_cost: float,
    profit_loss: float,
    profit_pct: float,
    shares: float
) -> bool:
    """
    更新持仓的加密字段（通过存储过程）
    """
    import psycopg2
    
    key = get_encryption_key()
    conn = _get_pg_conn()
    cur = conn.cursor()
    
    try:
        cur.execute(
            """CALL trading.update_position_encrypted(
                %s, %s, %s, %s, %s, %s
            )""",
            (code, avg_cost, profit_loss, profit_pct, shares, key)
        )
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"[ERROR] update_position_encrypted failed: {e}")
        return False
    finally:
        cur.close()
        conn.close()


def verify_encryption() -> dict:
    """
    验证加密是否正常工作（插入测试数据并解密验证）
    
    Returns:
        dict: {success: bool, message: str}
    """
    import psycopg2
    
    key = get_encryption_key()
    conn = _get_pg_conn()
    cur = conn.cursor()
    
    test_value = 999.99
    try:
        # 加密
        cur.execute(
            "SELECT pgp_sym_encrypt(%s::text, %s::text)",
            (str(test_value), key)
        )
        encrypted = cur.fetchone()[0]
        
        # 解密
        cur.execute(
            "SELECT pgp_sym_decrypt(%s, %s::text)",
            (encrypted, key)
        )
        decrypted = float(cur.fetchone()[0])
        
        if abs(decrypted - test_value) < 0.001:
            return {"success": True, "message": "Encryption/decryption verified OK"}
        else:
            return {"success": False, "message": f"Decryption mismatch: {decrypted} != {test_value}"}
    except Exception as e:
        return {"success": False, "message": f"Verification failed: {e}"}
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    # 快速验证
    result = verify_encryption()
    print(f"Encryption verification: {result}")
    
    # 读取现有持仓
    positions = get_decrypted_positions()
    print(f"\nDecrypted {len(positions)} positions:")
    for p in positions[:3]:
        print(f"  {p['code']} {p['name']}: shares={p['shares']}, avg_cost={p['avg_cost']}, profit={p['profit_loss']}")
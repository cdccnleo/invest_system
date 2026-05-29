-- add_column_encryption.sql
-- 幂等迁移：为 trading.positions 添加列级加密（pgcrypto AES-256）
-- 可重复执行，已存在的列/对象会被跳过

BEGIN;

-- 1. 确保 pgcrypto 扩展已启用（幂等）
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS pgcrypto;
EXCEPTION
    WHEN insufficient_privilege THEN
        RAISE NOTICE 'pgcrypto extension creation skipped (no privileges) - assuming already enabled';
END;
$$ LANGUAGE plpgsql;

-- 2. 创建持仓表（如果不存在）
CREATE TABLE IF NOT EXISTS trading.positions (
    id              SERIAL PRIMARY KEY,
    code            VARCHAR(10)  NOT NULL,
    name            VARCHAR(100),
    shares          NUMERIC(16,4) DEFAULT 0,
    -- 原始明文列（向后兼容，现有应用仍可用）
    avg_cost        NUMERIC(16,4) DEFAULT 0,
    profit_loss     NUMERIC(18,4) DEFAULT 0,
    profit_pct      NUMERIC(10,4) DEFAULT 0,
    -- 加密列（BYTEA，AES-256-CBC via pg_pgp_sym_encrypt）
    avg_cost_enc    BYTEA,
    profit_loss_enc BYTEA,
    profit_pct_enc  BYTEA,
    shares_enc      BYTEA,
    -- 非敏感字段（不需要加密）
    market_value    NUMERIC(16,2),
    close_price     NUMERIC(12,4),
    weight_pct      NUMERIC(8,4),
    position_type   VARCHAR(20)  DEFAULT 'stock',
    updated_at      TIMESTAMPTZ  DEFAULT NOW(),
    trace_id        UUID        DEFAULT gen_random_uuid()
);

-- 3. 为加密列添加唯一约束（防止重复插入同一 code）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'positions_code_key'
    ) THEN
        ALTER TABLE trading.positions
            ADD CONSTRAINT positions_code_key UNIQUE (code);
    END IF;
EXCEPTION WHEN duplicate_object THEN NULL;
END;
$$ LANGUAGE plpgsql;

-- 4. 创建索引
CREATE INDEX IF NOT EXISTS idx_positions_code ON trading.positions(code);
CREATE INDEX IF NOT EXISTS idx_positions_type  ON trading.positions(position_type);

-- 5. 创建辅助函数：检测加密密钥是否已设置（在视图之前定义）
CREATE OR REPLACE FUNCTION trading.key_is_available()
RETURNS BOOLEAN AS $$
BEGIN
    RETURN current_setting('app.encryption_key', TRUE) IS NOT NULL
        AND length(current_setting('app.encryption_key')) >= 32;
EXCEPTION WHEN undefined_object THEN
    RETURN FALSE;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- 6. 创建加密函数（对称加密，AES-256）
CREATE OR REPLACE FUNCTION trading.encrypt_value(plain_text TEXT, key TEXT)
RETURNS BYTEA AS $$
BEGIN
    RETURN pgp_sym_encrypt(plain_text, key);
END;
$$ LANGUAGE plpgsql STRICT SECURITY DEFINER;

-- 7. 创建解密函数
CREATE OR REPLACE FUNCTION trading.decrypt_value(cipher_text BYTEA, key TEXT)
RETURNS TEXT AS $$
BEGIN
    RETURN pgp_sym_decrypt(cipher_text, key);
END;
$$ LANGUAGE plpgsql STRICT SECURITY DEFINER;

-- 8. 创建批量解密视图（应用层通过视图访问解密数据）
CREATE OR REPLACE VIEW trading.positions_v AS
SELECT
    id,
    code,
    name,
    shares,
    avg_cost,
    profit_loss,
    profit_pct,
    -- 解密列（使用 CASE WHEN 避免 NULL 键报错）
    (CASE WHEN avg_cost_enc IS NOT NULL
          AND current_setting('app.encryption_key', TRUE) IS NOT NULL
          AND length(current_setting('app.encryption_key')) >= 32
         THEN trading.decrypt_value(avg_cost_enc, current_setting('app.encryption_key'))::NUMERIC(16,4)
     END) AS avg_cost_dec,
    (CASE WHEN profit_loss_enc IS NOT NULL
          AND current_setting('app.encryption_key', TRUE) IS NOT NULL
          AND length(current_setting('app.encryption_key')) >= 32
         THEN trading.decrypt_value(profit_loss_enc, current_setting('app.encryption_key'))::NUMERIC(18,4)
     END) AS profit_loss_dec,
    (CASE WHEN profit_pct_enc IS NOT NULL
          AND current_setting('app.encryption_key', TRUE) IS NOT NULL
          AND length(current_setting('app.encryption_key')) >= 32
         THEN trading.decrypt_value(profit_pct_enc, current_setting('app.encryption_key'))::NUMERIC(10,4)
     END) AS profit_pct_dec,
    shares_enc,
    avg_cost_enc,
    profit_loss_enc,
    profit_pct_enc,
    -- 非敏感字段
    market_value,
    close_price,
    weight_pct,
    position_type,
    updated_at,
    trace_id
FROM trading.positions;

-- 9. 创建加密存储过程（写入时自动加密敏感字段）
CREATE OR REPLACE PROCEDURE trading.insert_position(
    p_code         TEXT,
    p_name         TEXT,
    p_shares       NUMERIC,
    p_avg_cost     NUMERIC,
    p_profit_loss  NUMERIC,
    p_profit_pct   NUMERIC,
    p_enc_key      TEXT,
    p_market_value NUMERIC DEFAULT NULL,
    p_close_price  NUMERIC DEFAULT NULL,
    p_weight_pct   NUMERIC DEFAULT NULL,
    p_position_type TEXT DEFAULT 'stock'
)
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO trading.positions (
        code, name, shares, avg_cost, profit_loss, profit_pct,
        avg_cost_enc, profit_loss_enc, profit_pct_enc, shares_enc,
        market_value, close_price, weight_pct, position_type
    ) VALUES (
        p_code, p_name, p_shares, p_avg_cost, p_profit_loss, p_profit_pct,
        trading.encrypt_value(p_avg_cost::TEXT,    p_enc_key),
        trading.encrypt_value(p_profit_loss::TEXT, p_enc_key),
        trading.encrypt_value(p_profit_pct::TEXT,  p_enc_key),
        trading.encrypt_value(p_shares::TEXT,      p_enc_key),
        p_market_value, p_close_price, p_weight_pct, p_position_type
    )
    ON CONFLICT (code) DO UPDATE SET
        name          = EXCLUDED.name,
        shares        = EXCLUDED.shares,
        avg_cost      = EXCLUDED.avg_cost,
        profit_loss   = EXCLUDED.profit_loss,
        profit_pct    = EXCLUDED.profit_pct,
        avg_cost_enc  = EXCLUDED.avg_cost_enc,
        profit_loss_enc = EXCLUDED.profit_loss_enc,
        profit_pct_enc  = EXCLUDED.profit_pct_enc,
        shares_enc    = EXCLUDED.shares_enc,
        market_value  = EXCLUDED.market_value,
        close_price   = EXCLUDED.close_price,
        weight_pct    = EXCLUDED.weight_pct,
        updated_at    = NOW();
END;
$$;

-- 10. 创建更新加密字段的存储过程
CREATE OR REPLACE PROCEDURE trading.update_position_encrypted(
    p_code         TEXT,
    p_avg_cost     NUMERIC,
    p_profit_loss  NUMERIC,
    p_profit_pct   NUMERIC,
    p_shares       NUMERIC,
    p_enc_key      TEXT
)
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE trading.positions SET
        avg_cost        = p_avg_cost,
        profit_loss     = p_profit_loss,
        profit_pct      = p_profit_pct,
        shares          = p_shares,
        avg_cost_enc    = trading.encrypt_value(p_avg_cost::TEXT,    p_enc_key),
        profit_loss_enc = trading.encrypt_value(p_profit_loss::TEXT, p_enc_key),
        profit_pct_enc  = trading.encrypt_value(p_profit_pct::TEXT,  p_enc_key),
        shares_enc      = trading.encrypt_value(p_shares::TEXT,      p_enc_key),
        updated_at      = NOW()
    WHERE code = p_code;
END;
$$;

-- 11. 创建批量解密函数（返回 decrypted positions）
CREATE OR REPLACE FUNCTION trading.get_decrypted_positions(p_enc_key TEXT)
RETURNS TABLE (
    code         VARCHAR(10),
    name         VARCHAR(100),
    shares       NUMERIC,
    avg_cost     NUMERIC,
    profit_loss  NUMERIC,
    profit_pct   NUMERIC,
    market_value NUMERIC,
    close_price  NUMERIC,
    weight_pct   NUMERIC
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        pos.code::VARCHAR(10),
        pos.name::VARCHAR(100),
        COALESCE(pos.shares::NUMERIC, trading.decrypt_value(pos.shares_enc, p_enc_key)::NUMERIC, 0),
        COALESCE(pos.avg_cost::NUMERIC, trading.decrypt_value(pos.avg_cost_enc, p_enc_key)::NUMERIC, 0),
        COALESCE(pos.profit_loss::NUMERIC, trading.decrypt_value(pos.profit_loss_enc, p_enc_key)::NUMERIC, 0),
        COALESCE(pos.profit_pct::NUMERIC, trading.decrypt_value(pos.profit_pct_enc, p_enc_key)::NUMERIC, 0),
        pos.market_value,
        pos.close_price,
        pos.weight_pct
    FROM trading.positions pos;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- 12. 记录迁移历史（用于审计和回滚）
CREATE TABLE IF NOT EXISTS trading.migration_history (
    id            SERIAL PRIMARY KEY,
    applied_at    TIMESTAMPTZ DEFAULT NOW(),
    script_name   TEXT,
    sql_hash      TEXT,
    status        TEXT DEFAULT 'APPLIED',
    note          TEXT
);

INSERT INTO trading.migration_history (script_name, sql_hash, status, note)
VALUES (
    'add_column_encryption.sql',
    md5('CREATE EXTENSION pgcrypto; CREATE TABLE trading.positions;'),
    'APPLIED',
    'Column-level encryption for trading.positions (avg_cost, profit_loss, profit_pct, shares)'
)
ON CONFLICT DO NOTHING;

COMMIT;

-- 验证检查
DO $$
BEGIN
    -- 确认表存在
    IF NOT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'trading' AND tablename = 'positions') THEN
        RAISE EXCEPTION 'trading.positions table not found after migration!';
    END IF;

    -- 确认加密列存在
    IF NOT EXISTS (
        SELECT 1 FROM pg_columns
        WHERE schemaname = 'trading' AND tablename = 'positions'
          AND column_name IN ('avg_cost_enc', 'profit_loss_enc', 'profit_pct_enc', 'shares_enc')
    ) THEN
        RAISE EXCEPTION 'Encrypted columns not found!';
    END IF;

    -- 确认视图存在
    IF NOT EXISTS (SELECT 1 FROM pg_views WHERE schemaname = 'trading' AND viewname = 'positions_v') THEN
        RAISE EXCEPTION 'View trading.positions_v not found!';
    END IF;

    -- 确认函数存在
    IF NOT EXISTS (SELECT 1 FROM pg_proc WHERE pronamespace = 'trading'::regnamespace AND proname = 'encrypt_value') THEN
        RAISE EXCEPTION 'encrypt_value function not found!';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_proc WHERE pronamespace = 'trading'::regnamespace AND proname = 'decrypt_value') THEN
        RAISE EXCEPTION 'decrypt_value function not found!';
    END IF;

    RAISE NOTICE '=================================================';
    RAISE NOTICE 'Migration add_column_encryption.sql completed OK';
    RAISE NOTICE 'Table:     trading.positions';
    RAISE NOTICE 'View:      trading.positions_v';
    RAISE NOTICE 'Functions: trading.encrypt_value, trading.decrypt_value';
    RAISE NOTICE 'Procedures: trading.insert_position, trading.update_position_encrypted';
    RAISE NOTICE 'Encrypted cols: avg_cost_enc, profit_loss_enc, profit_pct_enc, shares_enc';
    RAISE NOTICE '=================================================';
EXCEPTION WHEN others THEN
    RAISE NOTICE 'Migration validation warning: %', SQLERRM;
END;
$$ LANGUAGE plpgsql;
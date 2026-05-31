-- rollback_add_column_encryption.sql
-- 回滚迁移：删除 trading.positions 的加密列和函数
-- 注意：此回滚不会删除已有的明文数据（avg_cost, profit_loss, profit_pct, shares 列会保留）
-- 只是删除加密版本列（*_enc）和相关的函数/视图
-- 执行前建议备份

BEGIN;

-- 1. 删除视图
DROP VIEW IF EXISTS trading.positions_v;

-- 2. 删除存储过程
DROP PROCEDURE IF EXISTS trading.insert_position(TEXT, TEXT, NUMERIC, NUMERIC, NUMERIC, NUMERIC, TEXT, NUMERIC, NUMERIC, NUMERIC, TEXT);
DROP PROCEDURE IF EXISTS trading.update_position_encrypted(TEXT, NUMERIC, NUMERIC, NUMERIC, NUMERIC, TEXT);

-- 3. 删除加密函数
DROP FUNCTION IF EXISTS trading.encrypt_value(TEXT, TEXT);
DROP FUNCTION IF EXISTS trading.decrypt_value(BYTEA, TEXT);
DROP FUNCTION IF EXISTS trading.key_is_available();
DROP FUNCTION IF EXISTS trading.get_decrypted_positions(TEXT);

-- 4. 删除加密列（可选：保留明文列以便向后兼容）
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_columns WHERE schemaname = 'trading' AND tablename = 'positions' AND column_name = 'avg_cost_enc') THEN
        ALTER TABLE trading.positions DROP COLUMN avg_cost_enc;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_columns WHERE schemaname = 'trading' AND tablename = 'positions' AND column_name = 'profit_loss_enc') THEN
        ALTER TABLE trading.positions DROP COLUMN profit_loss_enc;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_columns WHERE schemaname = 'trading' AND tablename = 'positions' AND column_name = 'profit_pct_enc') THEN
        ALTER TABLE trading.positions DROP COLUMN profit_pct_enc;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_columns WHERE schemaname = 'trading' AND tablename = 'positions' AND column_name = 'shares_enc') THEN
        ALTER TABLE trading.positions DROP COLUMN shares_enc;
    END IF;
END;
$$ LANGUAGE plpgsql;

-- 5. 删除唯一约束（如果存在）
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'positions_code_key') THEN
        ALTER TABLE trading.positions DROP CONSTRAINT positions_code_key;
    END IF;
EXCEPTION WHEN undefined_object THEN NULL;
END;
$$ LANGUAGE plpgsql;

-- 6. 删除迁移历史表（可选）
-- DROP TABLE IF EXISTS trading.migration_history;

COMMIT;

DO $$
BEGIN
    RAISE NOTICE '===============================================';
    RAISE NOTICE 'Rollback completed successfully.';
    RAISE NOTICE 'Removed: encrypted columns (*_enc), functions, view';
    RAISE NOTICE 'Preserved: plaintext columns (avg_cost, profit_loss, profit_pct, shares)';
    RAISE NOTICE '===============================================';
EXCEPTION WHEN others THEN
    RAISE NOTICE 'Rollback completed with warnings: %', SQLERRM;
END;
$$ LANGUAGE plpgsql;
-- agent_action_queue.sql
-- 数据库表DDL - 配合补丁1接口契约
-- 创建时间: 2026-06-11
-- 数据库: investpilot (PostgreSQL)

-- ============================================================
-- 1. agent_action_queue - 操作建议队列
-- ============================================================
CREATE TABLE IF NOT EXISTS agent_action_queue (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMP DEFAULT NOW(),
    executed_at TIMESTAMP,
    status VARCHAR(20) DEFAULT 'pending'
        CHECK (status IN ('pending', 'executed', 'failed', 'cancelled', 'expired')),
    action JSONB NOT NULL,
    reasoning TEXT,
    confidence FLOAT
        CHECK (confidence >= 0 AND confidence <= 1),
    refs TEXT[],
    source_event_id VARCHAR(50),
    source_skill VARCHAR(100),
    feedback JSONB,
    expires_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_action_queue_status_created
    ON agent_action_queue(status, created_at);

CREATE INDEX IF NOT EXISTS idx_action_queue_pending
    ON agent_action_queue(created_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_action_queue_confidence
    ON agent_action_queue(confidence)
    WHERE status = 'pending';

COMMENT ON TABLE agent_action_queue IS 'Hermes Agent生成的操作建议队列（补丁1+补丁2）';

-- ============================================================
-- 2. skill_sync_audit - Skill同步审计
-- ============================================================
CREATE TABLE IF NOT EXISTS skill_sync_audit (
    id BIGSERIAL PRIMARY KEY,
    sync_time TIMESTAMP DEFAULT NOW(),
    direction VARCHAR(20)
        CHECK (direction IN ('hermes_to_backend', 'backend_to_hermes', 'bidirectional')),
    skill_name VARCHAR(100) NOT NULL,
    result VARCHAR(20)
        CHECK (result IN ('success', 'failed', 'skipped')),
    diff_summary TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_skill_sync_audit_time
    ON skill_sync_audit(sync_time);

CREATE INDEX IF NOT EXISTS idx_skill_sync_audit_skill
    ON skill_sync_audit(skill_name, sync_time);

COMMENT ON TABLE skill_sync_audit IS 'Skill同步审计日志（补丁1+补丁9）';

-- ============================================================
-- 3. privacy_audit_log - 隐私审计日志
-- ============================================================
CREATE TABLE IF NOT EXISTS privacy_audit_log (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMP DEFAULT NOW(),
    user_id VARCHAR(50),
    role VARCHAR(20),
    action_type VARCHAR(50),  -- query/modify/export/push
    resource VARCHAR(100),
    field_accessed VARCHAR(100),
    p_level VARCHAR(5),  -- P0/P1/P2/P3
    result VARCHAR(20),  -- allowed/denied/masked
    ip_address INET,
    user_agent TEXT,
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_privacy_audit_user_ts
    ON privacy_audit_log(user_id, ts);

CREATE INDEX IF NOT EXISTS idx_privacy_audit_p0
    ON privacy_audit_log(ts) WHERE p_level = 'P0';

COMMENT ON TABLE privacy_audit_log IS '隐私访问审计（补丁5）';

-- ============================================================
-- 4. cron_task_metrics - Cron任务指标
-- ============================================================
CREATE TABLE IF NOT EXISTS cron_task_metrics (
    id BIGSERIAL PRIMARY KEY,
    task_name VARCHAR(100) NOT NULL,
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP,
    duration_seconds FLOAT,
    status VARCHAR(20)
        CHECK (status IN ('running', 'success', 'failed', 'timeout')),
    items_processed INT,
    items_failed INT,
    error_code VARCHAR(20),
    error_message TEXT,
    metadata JSONB
);

CREATE INDEX IF NOT EXISTS idx_cron_task_metrics_name_time
    ON cron_task_metrics(task_name, start_time);

CREATE INDEX IF NOT EXISTS idx_cron_task_metrics_failed
    ON cron_task_metrics(start_time) WHERE status IN ('failed', 'timeout');

COMMENT ON TABLE cron_task_metrics IS 'Cron任务执行指标（补丁2）';

-- ============================================================
-- 5. 初始化数据
-- ============================================================
-- 当前无初始数据，表会在首次执行hermes_event_analyst.py时填充
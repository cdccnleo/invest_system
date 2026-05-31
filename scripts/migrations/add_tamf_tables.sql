-- ============================================================
-- TAMF迁移脚本: 创建 memory schema 和 2张表
-- 执行: psql postgresql://invest_admin:postgresleo814569@localhost:5432/investpilot -f add_tamf_tables.sql
-- ============================================================

-- 1. 创建 memory schema
CREATE SCHEMA IF NOT EXISTS memory;

-- 2. target_memory_files — TAMF文件元数据表
CREATE TABLE memory.target_memory_files (
    id                  SERIAL PRIMARY KEY,
    ts_code             VARCHAR(20) NOT NULL UNIQUE,
    stock_name          VARCHAR(100),
    file_path           VARCHAR(500) NOT NULL,
    file_hash           VARCHAR(64),                        -- SHA-256 检测外部修改
    version_major       INT DEFAULT 1,
    version_minor       INT DEFAULT 0,
    analysis_status     VARCHAR(20) DEFAULT 'ACTIVE',        -- ACTIVE/WATCHING/CLOSED/TRACKING
    last_updated        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_full_analysis  TIMESTAMPTZ,
    sections_hash       JSONB,                              -- {"section_1":"abc",...} 各章节哈希
    data_snapshot       JSONB,                              -- {"last_quote_date":"...", "last_news_id":N,...}
    linked_skills       TEXT[],
    user_tags           TEXT[],
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 索引
CREATE INDEX idx_tmf_ts_code ON memory.target_memory_files(ts_code);
CREATE INDEX idx_tmf_status ON memory.target_memory_files(analysis_status);
CREATE INDEX idx_tmf_last_updated ON memory.target_memory_files(last_updated);
CREATE INDEX idx_tmf_exchange ON memory.target_memory_files(ts_code) WHERE ts_code ~ '^[0-9]{6}.(XSHG|XSHE)$';

-- 审计触发器（复用现有的 audit.log_operation）
CREATE TRIGGER trg_tmf_audit
    AFTER INSERT OR UPDATE OR DELETE ON memory.target_memory_files
    FOR EACH ROW EXECUTE FUNCTION audit.log_operation();

-- 3. target_timeline_events — 标的时间线事件表
CREATE TABLE memory.target_timeline_events (
    id                  BIGSERIAL PRIMARY KEY,
    ts_code             VARCHAR(20) NOT NULL,
    event_time          TIMESTAMPTZ NOT NULL,
    event_type          VARCHAR(50) NOT NULL,               -- BUY/SELL/DIVIDEND/SPLIT/RATING_CHANGE/
                                                            -- NEWS_IMPACT/EARNINGS_REPORT/ANNOUNCEMENT/
                                                            -- AGENT_ASSESSMENT/USER_NOTE/TAMF_UPDATE
    event_source        VARCHAR(50),                         -- SYSTEM/AGENT/USER/EXCHANGE
    severity            VARCHAR(10) DEFAULT 'INFO',          -- CRITICAL/WARNING/INFO/NEUTRAL
    title               VARCHAR(500),
    description         TEXT,
    impact_assessment   TEXT,
    related_data        JSONB,                              -- {transaction_id, ann_id, report_id, ...}
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 索引
CREATE INDEX idx_tte_ts_code_time ON memory.target_timeline_events(ts_code, event_time DESC);
CREATE INDEX idx_tte_type ON memory.target_timeline_events(event_type);
CREATE INDEX idx_tte_ts_type ON memory.target_timeline_events(ts_code, event_type);
CREATE INDEX idx_tte_source ON memory.target_timeline_events(event_source);
CREATE INDEX idx_tte_severity ON memory.target_timeline_events(severity);

-- 审计触发器
CREATE TRIGGER trg_tte_audit
    AFTER INSERT ON memory.target_timeline_events
    FOR EACH ROW EXECUTE FUNCTION audit.log_operation();

-- 4. 注释
COMMENT ON SCHEMA memory IS 'TAMF投资标的分析记忆系统 - 元数据和事件时间线';
COMMENT ON TABLE memory.target_memory_files IS 'TAMF文件元数据管理 - 每个持仓标的一行';
COMMENT ON TABLE memory.target_timeline_events IS '投资标的时间线事件统一记录 - 所有"发生了什么"的持久化';

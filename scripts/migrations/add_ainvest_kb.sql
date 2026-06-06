-- ============================================================
-- AInvest 知识库 Schema: 存储解析后的投资分析报告及提取信号
-- 执行: psql -h localhost -U invest_admin -d investpilot -f add_ainvest_kb.sql
-- 依赖: pgvector 扩展 (vector 类型)
-- ============================================================

-- 1. 创建 ainvest_kb schema
CREATE SCHEMA IF NOT EXISTS ainvest_kb;

-- 2. 解析报告元数据表
CREATE TABLE ainvest_kb.parsed_reports (
    id                  SERIAL PRIMARY KEY,
    file_path           TEXT NOT NULL UNIQUE,           -- 文件绝对路径
    file_hash           VARCHAR(64) NOT NULL,           -- SHA-256 全文哈希
    report_type         VARCHAR(20) NOT NULL,           -- events / trackers / deep-analysis / daily
    title               TEXT NOT NULL,                  -- 报告标题
    report_date         DATE,                           -- 报告日期（从文件名提取）
    file_modified_at    TIMESTAMPTZ NOT NULL,            -- 文件系统修改时间
    parsed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    version             INT DEFAULT 1,                   -- 解析版本号
    -- LLM 增强提取的结构化字段
    summary             TEXT,                            -- 报告摘要
    related_codes       TEXT[],                          -- 关联股票代码列表
    event_tags          TEXT[],                          -- 事件标签
    investment_signals  JSONB,                           -- 投资信号数组
    key_judgments       JSONB,                           -- 核心判断/结论
    risk_assessment     TEXT,                            -- 风险评估摘要
    operation_actions   JSONB,                           -- 操作建议（来自daily/复盘）
    primary_stock_code  VARCHAR(16),                     -- 主标的代码
    confidence_score    REAL DEFAULT 0.5,                -- LLM 解析置信度
    raw_text            TEXT                             -- 原始全文（截断至50000字符）
);

-- 索引
CREATE INDEX idx_apr_type_date ON ainvest_kb.parsed_reports(report_type, report_date DESC);
CREATE INDEX idx_apr_codes     ON ainvest_kb.parsed_reports USING GIN(related_codes);
CREATE INDEX idx_apr_tags      ON ainvest_kb.parsed_reports USING GIN(event_tags);
CREATE INDEX idx_apr_primary   ON ainvest_kb.parsed_reports(primary_stock_code);
CREATE INDEX idx_apr_date      ON ainvest_kb.parsed_reports(report_date DESC);

-- 3. 提取的投资信号表（规范化）
CREATE TABLE ainvest_kb.extracted_signals (
    id              SERIAL PRIMARY KEY,
    report_id       INT NOT NULL REFERENCES ainvest_kb.parsed_reports(id) ON DELETE CASCADE,
    signal_type     VARCHAR(30) NOT NULL,   -- rating_change / risk_warning / opportunity / price_target / stop_loss
    ts_code         VARCHAR(16),            -- 关联股票代码
    direction       VARCHAR(10),            -- positive / negative / neutral
    signal_text     TEXT NOT NULL,          -- 信号描述
    magnitude       REAL,                   -- 信号强度 0-1
    source_reporter VARCHAR(100),           -- 来源分析师/机构
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_es_report    ON ainvest_kb.extracted_signals(report_id);
CREATE INDEX idx_es_code      ON ainvest_kb.extracted_signals(ts_code);
CREATE INDEX idx_es_type      ON ainvest_kb.extracted_signals(signal_type);
CREATE INDEX idx_es_direction ON ainvest_kb.extracted_signals(direction);

-- 4. 标的-知识库关联表
CREATE TABLE ainvest_kb.stock_kb_links (
    id              SERIAL PRIMARY KEY,
    ts_code         VARCHAR(16) NOT NULL,    -- 持仓股票代码（6位数字）
    report_id       INT NOT NULL REFERENCES ainvest_kb.parsed_reports(id) ON DELETE CASCADE,
    relevance_score REAL DEFAULT 0.5,        -- 关联度 0-1
    last_accessed   TIMESTAMPTZ DEFAULT NOW(),
    accessed_count  INT DEFAULT 1,
    UNIQUE(ts_code, report_id)
);

CREATE INDEX idx_skl_code  ON ainvest_kb.stock_kb_links(ts_code);
CREATE INDEX idx_skl_score ON ainvest_kb.stock_kb_links(ts_code, relevance_score DESC);

-- 5. 报告向量嵌入表（复用 pgvector + HNSW 索引）
CREATE TABLE ainvest_kb.report_embeddings (
    id              BIGSERIAL PRIMARY KEY,
    report_id       INT NOT NULL REFERENCES ainvest_kb.parsed_reports(id) ON DELETE CASCADE,
    chunk_index     INT NOT NULL,
    content_chunk   TEXT NOT NULL,
    embedding       vector(768) NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_are_report ON ainvest_kb.report_embeddings(report_id);
CREATE INDEX idx_are_embedding ON ainvest_kb.report_embeddings
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- 6. 文件扫描审计表
CREATE TABLE ainvest_kb.scan_audit (
    id              SERIAL PRIMARY KEY,
    scan_start      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    scan_end        TIMESTAMPTZ,
    total_files     INT DEFAULT 0,
    new_files       INT DEFAULT 0,
    changed_files   INT DEFAULT 0,
    unchanged_files INT DEFAULT 0,
    parsed_ok       INT DEFAULT 0,
    parsed_failed   INT DEFAULT 0,
    errors          JSONB
);

-- 注释
COMMENT ON SCHEMA ainvest_kb IS 'AInvest 知识库 - 投资分析报告解析、信号提取、语义检索';
COMMENT ON TABLE ainvest_kb.parsed_reports IS '已解析的 AInvest 报告元数据，每份报告一行';
COMMENT ON TABLE ainvest_kb.extracted_signals IS '从报告中提取的投资信号，规范化存储';
COMMENT ON TABLE ainvest_kb.stock_kb_links IS '持仓标的与知识库报告的关联关系';
COMMENT ON TABLE ainvest_kb.report_embeddings IS '报告向量嵌入，支持语义搜索';
COMMENT ON TABLE ainvest_kb.scan_audit IS '文件扫描审计日志，每次扫描一条记录';
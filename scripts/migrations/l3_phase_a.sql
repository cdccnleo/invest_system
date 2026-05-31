-- L3 Phase A 数据库迁移
-- 4 张表：behavior_profile / active_dialog_triggers / stress_test_scenarios / stress_test_results
-- 执行：psql -U invest_admin -d investpilot -f scripts/migrations/l3_phase_a.sql

BEGIN;

-- ─────────────────────────────────────────────────────────────
-- 1. behavior_profile — 用户投资行为画像
-- ─────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS l3;

CREATE TABLE IF NOT EXISTS l3.behavior_profile (
    id                SERIAL PRIMARY KEY,
    profile_date      DATE NOT NULL DEFAULT CURRENT_DATE,
    dimension         TEXT NOT NULL,          -- overtrading | risk_taking | diversification | holding_pattern
    metric_name       TEXT NOT NULL,          -- trade_freq_7d | avg_position_size | sharpe_ratio_30d | ...
    metric_value      NUMERIC(18, 6) NOT NULL,
    benchmark_value   NUMERIC(18, 6),         -- 对比基准（如同类散户均值）
    deviation_pct     NUMERIC(8, 4),           -- 偏离基准百分比
    alert_level       TEXT DEFAULT 'normal',   -- normal | warning | critical
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (profile_date, dimension, metric_name)
);
CREATE INDEX IF NOT EXISTS idx_bp_date ON l3.behavior_profile(profile_date DESC);
CREATE INDEX IF NOT EXISTS idx_bp_alert ON l3.behavior_profile(alert_level) WHERE alert_level != 'normal';

COMMENT ON TABLE l3.behavior_profile IS '用户投资行为画像，按日期+维度+指标存储行为分析结果';

-- ─────────────────────────────────────────────────────────────
-- 2. active_dialog_triggers — 主动对话触发器
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS l3.active_dialog_triggers (
    id                SERIAL PRIMARY KEY,
    trigger_type      TEXT NOT NULL,           -- deviation_alert | periodic_checkin | news_impact | risk_escalation | milestone
    trigger_name      TEXT NOT NULL,          -- 触发器名称
    condition_expr    TEXT NOT NULL,           -- JSON条件表达式，如 {"metric": "drawdown", "op": ">", "threshold": 5}
    condition_desc    TEXT,                    -- 条件描述（人类可读）
    cooldown_hours    INTEGER DEFAULT 24,       -- 冷却时间（小时），避免重复触发
    last_triggered_at TIMESTAMPTZ,             -- 上次触发时间
    message_template  TEXT NOT NULL,           -- 消息模板，支持 {variable} 占位符
    priority          INTEGER DEFAULT 5,        -- 优先级 1-10，10最高
    is_active         BOOLEAN DEFAULT TRUE,
    trigger_count     INTEGER DEFAULT 0,        -- 累计触发次数
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_adt_type ON l3.active_dialog_triggers(trigger_type);
CREATE INDEX IF NOT EXISTS idx_adt_active ON l3.active_dialog_triggers(is_active) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_adt_cooldown ON l3.active_dialog_triggers(last_triggered_at) WHERE is_active = TRUE;

COMMENT ON TABLE l3.active_dialog_triggers IS 'L3主动对话触发器，定义触发条件与消息模板';

-- 插入 5 类默认触发器
INSERT INTO l3.active_dialog_triggers (trigger_type, trigger_name, condition_expr, condition_desc, cooldown_hours, message_template, priority)
VALUES
-- 1. deviation_alert：偏差告警（行为基线偏离超过20%）
(
    'deviation_alert',
    '策略偏离告警',
    '{"type": "behavior_deviation", "metric": "trade_freq", "threshold": 20, "unit": "percent"}',
    '交易频率偏离行为基线20%以上',
    12,
    '📉 【策略偏离检测】您的交易频率在过去7天比行为基线高出 {deviation_pct}%，偏离度已达 {alert_level} 级别。近期操作：{recent_trades}。建议：{suggestion}',
    8
),
-- 2. periodic_checkin：定期签到（每交易日盘前）
(
    'periodic_checkin',
    '盘前定时签到',
    '{"type": "schedule", "cron": "0 8 * * 1-5"}',
    '每个交易日上午08:00触发',
    20,
    '☀️ 今日盘前提醒 | 持仓市值 ¥{portfolio_value}，{positions_count}只标的。近期重要公告 {announcement_count} 条。请确认今日操作计划。',
    5
),
-- 3. news_impact：重大新闻影响
(
    'news_impact',
    '持仓股重大新闻',
    '{"type": "news_sentiment", "threshold": -0.7, "lookback_hours": 6}',
    '持仓股6小时内出现负面情绪骤降（ sentiment < -0.7）',
    4,
    '📰 【持仓股重大消息】{stock_name}（{stock_code}）出现重大{sentiment_type}新闻："{headline}"。建议查看详情并评估是否需要调整持仓。',
    9
),
-- 4. risk_escalation：风险升级
(
    'risk_escalation',
    '风险等级升级',
    '{"type": "risk_metric", "metric": "daily_drawdown", "threshold": 3, "unit": "percent"}',
    '日回撤超过3%',
    6,
    '🔴 【风险升级】当前持仓回撤达 {drawdown_pct}%，已触发 {alert_level} 告警。压力测试情景 {scenario} 显示最大损失 {max_loss_pct}%。是否需要启动对冲或减仓？',
    10
),
-- 5. milestone：里程碑事件
(
    'milestone',
    '盈亏里程碑',
    '{"type": "pnl_milestone", "threshold_type": "absolute", "value": 100000}',
    '组合盈亏突破±10万元',
    168,
    '🎯 【盈亏里程碑】您的组合累计收益达到 ¥{pnl_value}（{pnl_pct}%），{milestone_type}重要节点。历史表现：{performance_summary}',
    7
)
ON CONFLICT DO NOTHING;

-- ─────────────────────────────────────────────────────────────
-- 3. stress_test_scenarios — 压力测试情景
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS l3.stress_test_scenarios (
    id                SERIAL PRIMARY KEY,
    scenario_code     TEXT UNIQUE NOT NULL,    -- black_monday | rate_spike | sector_crash | liquidity_crisis | correlation_breakdown
    scenario_name     TEXT NOT NULL,
    scenario_desc     TEXT,
    shock_params      JSONB NOT NULL,          -- 冲击参数，如 {"沪深300": -0.08, "创业板": -0.12, "国债收益率": +0.005}
    probability_wt   NUMERIC(5, 2),           -- 历史概率权重（%）
    is_active         BOOLEAN DEFAULT TRUE,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sts_active ON l3.stress_test_scenarios(is_active) WHERE is_active = TRUE;

COMMENT ON TABLE l3.stress_test_scenarios IS '压力测试情景定义，shock_params描述各资产冲击幅度';

-- 插入 5 种默认情景
INSERT INTO l3.stress_test_scenarios (scenario_code, scenario_name, scenario_desc, shock_params, probability_wt)
VALUES
    ('black_monday', '黑色星期一', '2015年7月式流动性踩踏，沪深300单日-8%',  '{"A沪深300": -0.08, "A创业板": -0.10, "A科创50": -0.10, "A纳斯达克": -0.05}', 5.00),
    ('rate_spike', '国债收益率飙升', '10年国债收益率单日上行20bp，债市大幅杀跌', '{"A10年期国债": 0.002, "A沪深300": -0.03, "A中证500": -0.025, "A纳斯达克": -0.02}', 8.00),
    ('sector_crash', '单行业黑天鹅', '重仓行业突发监管黑天鹅，单日跌停', '{"A持仓行业(假设)": -0.10, "A沪深300": -0.025, "A中证500": -0.020}', 10.00),
    ('liquidity_crisis', '流动性枯竭', '类似2020年3月，外资撤离导致流动性折价', '{"A沪深300": -0.05, "A创业板": -0.07, "A纳斯达克": -0.06, "VIX": 0.15}', 7.00),
    ('correlation_breakdown', '股债双杀', '股债齐跌，传统避险失效（2022年11月模式）', '{"A沪深300": -0.04, "A10年期国债": -0.015, "A中证500": -0.045, "A黄金": -0.02}', 12.00)
ON CONFLICT (scenario_code) DO NOTHING;

-- ─────────────────────────────────────────────────────────────
-- 4. stress_test_results — 压力测试结果
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS l3.stress_test_results (
    id                SERIAL PRIMARY KEY,
    run_id            TEXT UNIQUE NOT NULL,   -- UUID，关联同一批次运行的所有情景
    scenario_code     TEXT NOT NULL REFERENCES l3.stress_test_scenarios(scenario_code),
    scenario_name     TEXT NOT NULL,
    executed_at       TIMESTAMPTZ DEFAULT NOW(),
    holding_snapshot  JSONB,                   -- 执行时持仓快照
    portfolio_value   NUMERIC(18, 2),          -- 测试时组合市值
    shock_result      JSONB NOT NULL,          -- {"positions": [{"code":"300059","name":"东方财富","shock_pct":-0.08,"loss":-12000}], "total_loss": -85000, "loss_rate": -0.051}
    max_loss_pct      NUMERIC(8, 4),           -- 最大损失率（%）
    max_loss_abs      NUMERIC(18, 2),           -- 最大损失绝对额
    risk_score        INTEGER,                  -- 1-10风险评分
    recommendation    TEXT,                     -- 建议（需要减仓/对冲/观望）
    created_at        TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_str_scenario ON l3.stress_test_results(scenario_code);
CREATE INDEX IF NOT EXISTS idx_str_executed ON l3.stress_test_results(executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_str_risk ON l3.stress_test_results(risk_score);

COMMENT ON TABLE l3.stress_test_results IS '压力测试执行结果';

COMMIT;

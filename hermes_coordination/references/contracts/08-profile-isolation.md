# Hermes Agent × InvestPilot — 跨 Profile 隔离 v1.0
# 补丁8: default / conservative / aggressive 三套配置

## 一、三套 Profile 定位

| Profile | 风险等级 | 目标用户 | AI算力目标 | 防御目标 | 现金目标 | 持仓数 | 调仓频率 |
|---------|---------|---------|-----------|---------|---------|-------|---------|
| **default** | balanced | 当前主用户 | 35-50% | 15-20% | 20% | 39 | 月度 |
| **conservative** | defensive | 配偶/退休账户 | 10-20% | 50-60% | 30% | 15-20 | 季度 |
| **aggressive** | offensive | 进取/小资金 | 70-85% | 0-5% | 10% | 10-15 | 双周 |

---

## 二、Profile 配置模板（YAML）

```yaml
# ~/.hermes/profiles/<name>/config.yaml
profile:
  name: "default" | "conservative" | "aggressive"
  description: "..."
  risk_level: "balanced" | "defensive" | "offensive"
  account_id: "default"  # 对应 InvestPilot 多账号

target_allocation:
  ai_compute: 0.40           # AI 算力主线（光纤/CCL/PCB/液冷）
  robotics: 0.17            # 机器人（特斯拉链）
  defense: 0.18             # 防御（黄金/有色/债券）
  cash: 0.20                # 现金
  theme_etf: 0.05           # 主题 ETF

position_constraints:
  max_position_pct: 5.0     # 单标的最高仓位%
  min_position_pct: 0.5
  max_pe_ttm: 100           # 单标的 PE 上限
  max_52w_change: 400       # 52周涨幅上限%
  max_high_pe_count: 2      # 高PE(>80倍)标的数

alert_thresholds:
  intraday_pct: 5           # 主板波动告警%
  position_pct: 3           # 仓位变化告警%
  cooldown_minutes: 30

strategy_overrides:
  enable_fomo_guard: true
  enable_pe_trap_check: true
  enable_event_drive: true
  intraday_scan_freq_min: 5

llm_routing:
  primary_model: "deepseek-chat"
  fallback_model: "ollama-llama3"
  confidence_threshold: 0.65

watchlist_overrides:
  blacklist: ["300136"]     # 高 PE 标的强制排除
  whitelist: ["600487", "600183"]  # 重点关注
```

---

## 三、Profile 实例

### 1. default (主账户 / 当前配置)

```yaml
profile:
  name: "default"
  description: "主账户 530万 — 战时配置（AI 算力主线 + 事件催化）"
  risk_level: "balanced"
  account_id: "default"

target_allocation:
  ai_compute: 0.35
  robotics: 0.17
  defense: 0.036          # ⚠️ 当前严重低于 15% 目标
  cash: 0.20
  theme_etf: 0.05

position_constraints:
  max_position_pct: 5.0
  min_position_pct: 0.5
  max_pe_ttm: 100
  max_52w_change: 400
  max_high_pe_count: 3    # 信维通信等

alert_thresholds:
  intraday_pct: 5
  position_pct: 3
  cooldown_minutes: 30

strategy_overrides:
  enable_fomo_guard: true
  enable_pe_trap_check: true
  enable_event_drive: true
  intraday_scan_freq_min: 5

llm_routing:
  primary_model: "deepseek-chat"
  fallback_model: "ollama-llama3"
  confidence_threshold: 0.65

watchlist_overrides:
  blacklist: ["300136"]   # 信维通信 (PE 130-160)
  whitelist: ["600487", "600183", "601689", "688025", "688279"]
```

### 2. conservative (防御位 / 配偶/退休账户)

```yaml
profile:
  name: "conservative"
  description: "防御位 — 黄金+现金+低波动 ETF"
  risk_level: "defensive"
  account_id: "conservative"

target_allocation:
  ai_compute: 0.10        # 仅保留低波动 AI 链
  robotics: 0.05
  defense: 0.55            # 黄金+有色+债券
  cash: 0.30
  theme_etf: 0.00

position_constraints:
  max_position_pct: 8.0   # 集中度可更高（标的少）
  max_pe_ttm: 30
  max_52w_change: 100
  max_high_pe_count: 0

alert_thresholds:
  intraday_pct: 3
  position_pct: 2
  cooldown_minutes: 60

strategy_overrides:
  enable_fomo_guard: true
  enable_pe_trap_check: true
  enable_event_drive: false  # 不追事件
  intraday_scan_freq_min: 30

llm_routing:
  primary_model: "deepseek-chat"
  fallback_model: "ollama-llama3"
  confidence_threshold: 0.80  # 更严格

watchlist_overrides:
  blacklist: []  # 全部白名单
  whitelist: ["518880", "516650", "512880", "513300", "600036", "601318"]
```

### 3. aggressive (进攻位 / 小资金 / 进取账户)

```yaml
profile:
  name: "aggressive"
  description: "进攻位 — AI 算力 70%+ + 高弹性主题"
  risk_level: "offensive"
  account_id: "aggressive"

target_allocation:
  ai_compute: 0.70
  robotics: 0.20
  defense: 0.00
  cash: 0.10
  theme_etf: 0.00

position_constraints:
  max_position_pct: 15.0  # 集中度高
  max_pe_ttm: 200          # 容忍高 PE
  max_52w_change: 600
  max_high_pe_count: 8

alert_thresholds:
  intraday_pct: 7          # 更宽
  position_pct: 5
  cooldown_minutes: 15

strategy_overrides:
  enable_fomo_guard: false  # 接受 FOMO
  enable_pe_trap_check: true
  enable_event_drive: true
  intraday_scan_freq_min: 2  # 高频

llm_routing:
  primary_model: "deepseek-chat"
  fallback_model: "gpt-4o"
  confidence_threshold: 0.55  # 更宽松

watchlist_overrides:
  blacklist: []
  whitelist: ["600487", "600183", "601689", "300136", "002050", "002080", "300394"]
```

---

## 四、跨 Profile 隔离规则

```yaml
profile_isolation:
  # 1. Skill 隔离
  skills:
    same_target_multi_profile: "skill_manage 隔离（不同 profile 用不同 skill 副本）"
    update_propagation: "显式 sync 才会跨 profile 共享"

  # 2. Memory 隔离
  memory:
    per_profile: true
    cross_profile_read: false
    audit_trail: true

  # 3. CronJob 隔离
  cron:
    profile_binding: true   # 任务指定 profile
    multi_profile: "同一 cron 可触发多个 profile（用 --profile=name）"

  # 4. Channel 隔离
  channels:
    telegram:
      default: "default"
      conservative: "conservative_bot"
      aggressive: "aggressive_bot"
    dingtalk:
      default: "default_room"
      conservative: "spouse_room"
      aggressive: "private_room"

  # 5. Data 隔离
  data:
    investpilot_account_mapping:
      default: ["guojin", "guangfa", "tiantian", "huitianfu"]
      conservative: ["huitianfu_only"]
      aggressive: ["guojin_only"]

  # 6. LLM 配额隔离
  llm:
    default: "unlimited (主账户优先级)"
    conservative: "降级到 ollama (免费)"
    aggressive: "unlimited (主账户优先级)"
```

---

## 五、Profile 切换 CLI

```bash
# 查看当前 profile
hermes profile list

# 切换 profile
hermes profile use default
hermes profile use conservative
hermes profile use aggressive

# Profile-aware skill 加载
hermes skills load --profile=conservative

# Profile-aware 调度
hermes cron run --profile=aggressive --job=intraday_monitor
```

---

## 六、实施路径

```yaml
phase_1:  # v2.0 → v2.1
  - 创建 ~/.hermes/profiles/{default,conservative,aggressive}/config.yaml
  - Skill 加载时根据 --profile 过滤
  - 验证：每个 profile 独立跑 portfolio-report-systematic-analysis

phase_2:  # v2.1 → v2.2
  - LLM 配额隔离（per-profile token counter）
  - 跨 profile 数据迁移工具
  - Dashboard 顶部 profile 切换器
```

---

**待命状态**：跨 Profile 隔离补丁（补丁8）落地。3 套 YAML + 隔离规则。后续实施 phase_1（3 个 profile 配置文件 + skill 过滤）。

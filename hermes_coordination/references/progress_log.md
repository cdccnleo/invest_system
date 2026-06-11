# Hermes × InvestPilot 协同方案实施进度日志

> **维护人**：Hermes Agent（每次 P 阶段变动后追加）
> **关联文件**：`SKILL.md` §八 实际进度跟踪（本文件是其原始数据源）
> **更新原则**：每次变动必跑 `ls -la <file>` + `python3 -c "import X"` + `wc -l <file>` 取真实证据

---

## 📅 2026-06-11 21:47 → 23:13 — P0 + P1 阶段交付日

### P0（5/5 ✅）

| 时间 | 任务 | 交付物 | 验证 |
|:---:|------|------|:---:|
| 21:47 | 补丁1 接口契约 | `references/contracts/01-hermes-investpilot-contract.yaml` (6.9KB) | YAML 语法 OK |
| 21:48 | 补丁2 可观测性 | `references/contracts/02-hermes-monitoring.yaml` (7.7KB) | YAML 语法 OK |
| 21:49 | 补丁3 成本评估 | `references/contracts/03-cost-estimation.yaml` (6.1KB) | YAML 语法 OK |
| 21:50 | 补丁4 时间窗口 | `references/contracts/04-key-window-strategy.yaml` (9.0KB) | YAML 语法 OK |
| 21:52 | 补丁5 安全合规 | `references/contracts/05-data-privacy.md` (11.1KB) | MD 渲染 OK |

### P1（4/4 脚本 ✅ + 4 PG 表 ✅）

| 时间 | 任务 | 交付物 | 验证 |
|:---:|------|------|:---:|
| 21:55 | 方案5 KB 批量吸收 | `scripts/hermes_kb_ingest.py` (10.2KB) | `import hermes_kb_ingest` OK |
| 21:57 | SQL DDL | `scripts/sql/agent_action_queue.sql` | PG 16.14 investpilot 库执行通过 |
| 22:02 | 监控告警 | `references/monitoring/watch_hermes_agent.sh` (3.4KB) | bash 语法 OK |
| 22:38 | 通知+脱敏 | `scripts/hermes_notifier.py` (9.9KB) | `import hermes_notifier` OK |
| 23:13 | 方案2 事件首席分析师 | `scripts/hermes_event_analyst.py` (18.0KB) | `import hermes_event_analyst` OK；→PG写入 5 条 |

### PG 表实际创建（2026-06-12 05:11 复核）

```
public.agent_action_queue  ✅ (方案2 用)
public.cron_task_metrics   ✅ (可观测性 用)
public.privacy_audit_log   ✅ (数据脱敏审计 用)
public.skill_sync_audit    ✅ (方案1 双向同步 用)
```

### SKILL.md 定版

| 时间 | 动作 |
|:---:|------|
| 22:01 | v1.0 8大方案 → `references/v1_8_schemes.md` |
| 22:26 | PG 部署日志 → `references/pg-deployment-log.md` |
| 22:33 | v2.0 SKILL.md 定版（15KB） |

---

## 📅 2026-06-12 05:11 — P1-T4 方案1 双向同步脚本 启动 + 截断

### 任务目标

实现方案1（Hermes Skill库 ↔ InvestPilot TAMF 双向同步）的核心脚本 `hermes_agent_sync.py`，支持 4 种模式：
- `inspect` — 差异查看（不写入）
- `h2i` — Hermes Skills → InvestPilot target_memories
- `i2h` — InvestPilot target_memories → Hermes Skills
- `bidirectional` — 双向合并（按 mtime 选择较新）

### 实际进展

| 步骤 | 操作 | 结果 |
|:---:|------|:---:|
| 1 | 第一次 `execute_code` 写 Part 1（`r"..."` 内含换行符） | ❌ `SyntaxError: unterminated string literal` |
| 2 | 改用 `r''' '''` raw 字符串 + 单引号 | ✅ Part 1 写入 5355B，语法 OK |
| 3 | `execute_code` 追加 Part 2（class + sync方法 + main，~5KB 代码块） | ⚠️ **工具调用被截断**，文件**未追加**，但工具返回 success（silent partial success） |
| 4 | 助手诊断"Part2实际没追加（execute_code 被截断）。重新追加：" | 用户未确认续做，转入新查询 |

### 断点状态（关键）

**文件实际大小**：5.3KB / 168行
**包含的函数**：
```python
def get_pg_conn()            # 44行
def sha256_file()            # 64行
def detect_stock_code_from_skill()  # 73行
def parse_hermes_skill()     # 82行
def parse_invest_tm()        # 107行
def _skill_to_tm()           # 121行
def _tm_to_skill()           # 142行
```

**缺失的关键结构**：
```python
class HermesAgentSync:       # ❌ 缺失
    def sync_h2i()           # ❌ 缺失
    def sync_i2h()           # ❌ 缺失
    def sync_bidirectional() # ❌ 缺失

def main()                   # ❌ 缺失
if __name__ == "__main__":   # ❌ 缺失
```

### PG 表已就绪（等待脚本恢复）

`public.skill_sync_audit` 表 schema：
```sql
CREATE TABLE public.skill_sync_audit (
  id BIGSERIAL PRIMARY KEY,
  sync_direction VARCHAR(50),  -- hermes_to_invest / invest_to_hermes
  source_path TEXT,
  target_path TEXT,
  source_sha256 VARCHAR(64),
  target_sha256 VARCHAR(64),
  status VARCHAR(20),
  error_message TEXT,
  created_at TIMESTAMP DEFAULT NOW()
);
```

---

## 🛑 暂停中的任务（用户最关心的当前项）

### P1-T4-B：inspect dry-run 验证

```bash
cd ~/.hermes/skills/investing/hermes-investpilot-coordination-v2
~/invest_system/.venv/bin/python scripts/hermes_agent_sync.py --mode inspect
```

### P1-T4-C：双向合并 + PG 审计写入

```bash
~/invest_system/.venv/bin/python scripts/hermes_agent_sync.py --mode bidirectional --execute
```

### P1-T4-D：commit + 同步到 GitHub

⚠️ **架构问题**：skill 在 `~/.hermes/skills/`，git 仓库在 `~/invest_system/`——两者分离。

**3 种解决方案**：
1. **符号链接**：`ln -s ~/.hermes/skills/investing/hermes-investpilot-coordination-v2 ~/invest_system/notes/coordination_v2`
2. **手工 rsync**：定期 `rsync -av --delete ~/.hermes/skills/investing/hermes-investpilot-coordination-v2/ ~/invest_system/notes/coordination_v2/`
3. **独立 git repo**：`cd ~/.hermes/skills/investing/hermes-investpilot-coordination-v2 && git init && git remote add origin git@github.com:cdccncnleo/hermes-coordination-v2.git`

---

## 📊 综合统计

| 维度 | 数值 |
|------|-----:|
| 总文件数 | 14 |
| PG 表数 | 4 |
| PG 索引数 | 9 |
| P 阶段完成度 | 9/13 (69.2%) — P0+P1 完成，P1-T4 30% 中断 |
| 真实代码行数 | ~800 行（4 个 .py） |
| GitHub 同步状态 | 0 commit |
| PG 真实数据 | 65 行（agent_action_queue 测试数据） |

---

## 🔄 后续会话工作流

1. 加载本 skill → 读 SKILL.md §八 实际进度跟踪
2. 检查 `references/progress_log.md` 看最新状态
3. 用户说"继续 [P1-T4-B/C/D]"，按 §八 给出的精确命令执行
4. 执行后**追加**本日志（不要覆盖历史）
5. 任何"完成"声明前必跑真实验证命令

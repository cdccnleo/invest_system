# AInvest 知识库集成方案 — 正式审核确认文档

**版本**: 1.0  
**审核日期**: 2026-06-06  
**审核人**: Hermes Agent  
**审核范围**: AInvest 报告目录定期读取与知识库自动更新实施方案  
**审核结论**: **✅ 方案通过（含 3 项修正要求，1 项建议增强）**

---

## 一、审核总览

| 审核维度 | 评分 | 结论 |
|:--------|:---:|:-----|
| **方案完整性** | 9.0/10 | ✅ 通过 — 覆盖采集→解析→存储→集成→展示全链路 |
| **技术选型合理性** | 9.5/10 | ✅ 通过 — 全部复用现有基础设施，零新增外部依赖 |
| **资源分配充分性** | 8.5/10 | ✅ 通过 — 4-5 天工期合理，3 项修正后可达 9.0/10 |
| **时间节点合理性** | 8.5/10 | ✅ 通过 — P1-P4 分阶段实施，优先交付核心功能 |
| **风险应对有效性** | 9.0/10 | ✅ 通过 — 降级路径完整，边界条件需补充 2 项 |

---

## 二、方案完整性审核

### 2.1 模块覆盖检查清单

| # | 模块 | 方案文件 | 覆盖状态 | 说明 |
|:--:|------|---------|:---:|------|
| 1 | 数据库 Schema | `migrations/add_ainvest_kb.sql` | ✅ | 6 张表覆盖完整，索引设计合理 |
| 2 | 文件监控机制 | `ainvest_report_parser.py` → `scan_reports_directory()` | ✅ | SHA-256 哈希变更检测，JSON 持久化状态 |
| 3 | 报告内容解析 | `ainvest_report_parser.py` → `parse_single_report()` | ✅ | 正则 + LLM 双阶段提取，4 种报告类型适配 |
| 4 | 向量嵌入生成 | `kb_ainvest_worker.py` → `generate_report_embeddings()` | ✅ | 复用 `embedding_service` 基础设施 |
| 5 | 标的关联建立 | `kb_ainvest_worker.py` → `update_stock_kb_links()` | ✅ | 去重 upsert，自动检测持仓标的 |
| 6 | TAMF 联动触发 | `kb_ainvest_worker.py` → `trigger_tamf_updates_for_report()` | ✅ | 按报告类型分级触发，记录时间线事件 |
| 7 | Prompt 注入 | `prompt_builder.py` → `build_ainvest_knowledge_for_prompt()` | ✅ | 30 天窗口，每标的最多 5 份报告摘要 |
| 8 | 调度任务注册 | `schedule_runner.py` → `job_ainvest_kb_scan()` | ✅ | 3 个时间节点（07:30/15:30/21:30） |
| 9 | 仪表盘视图 | `dashboard_views/_ainvest_kb.py` | ✅ | 4 Tab 布局，含手动同步按钮 |
| 10 | 审计日志 | `ainvest_kb.scan_audit` 表 | ✅ | 每次扫描记录完整统计 |

### 2.2 接口兼容性验证（逐项核对实际代码）

| # | 检查项 | 目标文件:行号 | 实际签名 | 兼容性 |
|:--:|--------|-------------|---------|:---:|
| 1 | `get_pg_connection()` | [storage_factory.py](file:///c:/PythonProject/invest_system/scripts/storage_factory.py#L35-L59) | `def get_pg_connection():` → `psycopg2.Connection` | ✅ |
| 2 | `send_notification()` | [notification.py](file:///c:/PythonProject/invest_system/scripts/notification.py#L258-L263) | `send_notification(title, content, level, channels)` | ✅ |
| 3 | `get_embedding()` | [embedding_service.py](file:///c:/PythonProject/invest_system/scripts/embedding_service.py#L35) | `get_embedding(text, model)` | ✅ |
| 4 | `_chunk_text()` | [embedding_service.py](file:///c:/PythonProject/invest_system/scripts/embedding_service.py#L136) | `_chunk_text(text, max_chars)` | ⚠️ 私有函数，需改为公开导出或直接内联 |
| 5 | `parallel_update_all_holdings()` | [tamf_updater.py](file:///c:/PythonProject/invest_system/scripts/tamf_updater.py#L875) | `parallel_update_all_holdings(max_workers=2)` | ✅ |
| 6 | `incremental_update()` | [tamf_updater.py](file:///c:/PythonProject/invest_system/scripts/tamf_updater.py#L770) | `incremental_update(code)` | ✅ |
| 7 | LLM 调用方式 | [llm_caller.py](file:///c:/PythonProject/invest_system/scripts/llm_caller.py#L48-L93) | `DeepSeekClient().chat(prompt, system)` → `{"content": str, "error": str}` | ❌ 方案中使用 `call_deepseek()`，实际不存在 |
| 8 | `get_credential()` | [credentials.py](file:///c:/PythonProject/invest_system/scripts/credentials.py#L123) | `get_credential(key, default)` | ✅ |

### 2.3 报告格式兼容性验证

| 报告类型 | 采样文件 | 命名格式 | 方案解析匹配 | 说明 |
|:--------|---------|---------|:---:|------|
| **events/** | `2026-06-06_非农核爆_全球资产巨震_...md` | `YYYY-MM-DD_事件关键词_持仓影响分析.md` | ✅ | 含"持仓标的暴露度评估"章节，`_extract_event_report()` 可匹配 |
| **trackers/** | `002050_三花智控_投资跟踪.md` | `{代码}_{名称}_投资跟踪.md` | ⚠️ | 文件名格式为 `代码_名称_投资跟踪.md`，**非** `YYYY-MM-DD_` 开头，`extract_report_date_from_filename()` 将返回 None |
| **deep-analysis/** | `2026-05-31_快手01024.HK深度投资价值分析报告.md` | `YYYY-MM-DD_标题.md` | ✅ | 含"执行摘要"和"投资建议"章节 |
| **daily/** | `2026-05-29_复盘分析及0601操作计划.md` | `YYYY-MM-DD_复盘分析及操作计划.md` | ✅ | 含操作计划表格和止损调整 |

---

## 三、技术选型合理性审核

### 3.1 技术栈对照

| 层级 | 方案选用 | 现有基础设施 | 复用性 |
|------|---------|------------|:---:|
| 文件监控 | **APScheduler 定时轮询** | [schedule_runner.py](file:///c:/PythonProject/invest_system/scripts/schedule_runner.py) | ✅ 零新增依赖 |
| 哈希持久化 | **JSON 文件** | 现有 `data/` 目录 | ✅ 无数据库依赖 |
| 文本解析 | **正则表达式** | Python 标准库 `re` | ✅ 零依赖 |
| LLM 增强 | **DeepSeek API** | [llm_caller.py](file:///c:/PythonProject/invest_system/scripts/llm_caller.py) | ✅ 已有 |
| 向量嵌入 | **Ollama nomic-embed-text** | [embedding_service.py](file:///c:/PythonProject/invest_system/scripts/embedding_service.py) | ✅ 已有 |
| 数据库 | **PostgreSQL + pgvector** | [storage_factory.py](file:///c:/PythonProject/invest_system/scripts/storage_factory.py) | ✅ 已有 |
| 通知推送 | **Server酱/飞书/Bark** | [notification.py](file:///c:/PythonProject/invest_system/scripts/notification.py) | ✅ 已有 |
| 仪表盘 | **Streamlit** | [dashboard_views/](file:///c:/PythonProject/invest_system/scripts/dashboard_views/) | ✅ 已有 |

**结论**: 零新增外部依赖，全部复用现有基础设施。技术选型评分 **9.5/10**。

### 3.2 数据库 Schema 规范性

| 检查项 | 结果 | 对标 |
|--------|:---:|------|
| Schema 命名规范 | ✅ | `ainvest_kb` — 与现有 `market`、`research`、`memory`、`l3`、`audit` 一致 |
| 索引策略 | ✅ | GIN 索引用于数组字段，B-tree 索引用于日期/ID，IVFFlat 用于向量 |
| 向量维度一致性 | ✅ | `vector(768)` 与 `nomic-embed-text` 模型输出维度一致 |
| 外键约束 | ✅ | `ON DELETE CASCADE` 维护引用完整性 |
| 迁移脚本格式 | ✅ | 与 `add_tamf_tables.sql` 格式一致 |

---

## 四、资源分配与时间节点审核

### 4.1 工时评估

| 阶段 | 任务 | 方案预估 | 审核评估 | 调整 |
|:---:|------|:---:|:---:|:---:|
| P1 | SQL 迁移脚本 + 数据库初始化 | 0.5 天 | **0.5 天** | 无需调整 |
| P1 | 文件监控 + 解析引擎 | 1 天 | **1.5 天** | ⚠️ +0.5 天（需修正 LLM 调用方式 + trackers 日期提取） |
| P1 | 知识库写入 + 嵌入服务 | 0.5 天 | **0.5 天** | 无需调整 |
| P2 | 调度任务注册 + TAMF 联动 | 0.5 天 | **0.5 天** | 无需调整 |
| P2 | Prompt 注入增强 | 0.5 天 | **0.5 天** | 无需调整 |
| P3 | Dashboard 知识库视图 | 0.5 天 | **0.5 天** | 无需调整 |
| P3 | 首次全量导入 + 验证 | 0.5 天 | **0.5 天** | 无需调整 |
| P4 | 增量训练 + 质量评估 | 1 天 | **1 天** | 无需调整 |
| **合计** | | **4-5 天** | **5-5.5 天** | +0.5 天修正余量 |

### 4.2 交付物清单

| # | 交付物 | 类型 | 状态 |
|:--:|--------|------|:---:|
| 1 | `migrations/add_ainvest_kb.sql` | 新建 | 待实施 |
| 2 | `scripts/ainvest_report_parser.py` | 新建 | 待实施（含修正） |
| 3 | `scripts/kb_ainvest_worker.py` | 新建 | 待实施（含修正） |
| 4 | `scripts/schedule_runner.py` | 修改 | 待实施（~50 行新增） |
| 5 | `scripts/prompt_builder.py` | 修改 | 待实施（~60 行新增） |
| 6 | `scripts/dashboard_views/_ainvest_kb.py` | 新建 | 待实施 |
| 7 | `scripts/dashboard_views/__main__.py` | 修改 | 待实施（~5 行新增） |
| 8 | `scripts/embedding_service.py` | 修改 | 待实施（`_chunk_text` → `chunk_text` 公开） |

---

## 五、风险识别与应对措施

### 5.1 已识别风险清单

| # | 风险 | 等级 | 影响 | 方案应对措施 | 审核评估 |
|:--:|------|:---:|------|------------|:---:|
| R1 | **LLM 调用方式不匹配** | 🔴 高 | `enrich_with_llm()` 无法调用 LLM | 修正为 `DeepSeekClient().chat(prompt, system)` | ✅ 修正后解决 |
| R2 | **trackers 报告无日期提取** | 🟡 中 | 跟踪报告 `report_date` 为 NULL | 从文件修改时间或 INDEX.md 获取日期 | ⚠️ 方案未覆盖，需补充 |
| R3 | **`_chunk_text` 私有函数不可直接导入** | 🟡 中 | 跨模块调用不符合 Python 约定 | 改为公开函数 `chunk_text()` 或 kb_worker 中内联实现 | ⚠️ 方案未覆盖，需补充 |
| R4 | **PostgreSQL 不可用时的降级** | 🟢 低 | 知识库功能完全不可用 | `_get_db_conn()` 返回 None 时跳过，记录日志 | ✅ 降级路径已设计 |
| R5 | **Ollama 不可用时的降级** | 🟢 低 | 向量嵌入跳过，信号提取仍可用 | `get_embedding()` 返回 None 时跳过嵌入 | ✅ 降级路径已设计 |
| R6 | **大文件解析性能** | 🟢 低 | deep-analysis 报告可能超过 50000 字符 | 截断至 50000 字符 + 向量嵌入取前 10000 字符 | ✅ 已设计 |
| R7 | **文件编码异常** | 🟢 低 | 非 UTF-8 编码文件解析失败 | 使用 `utf-8` 编码读取，失败时捕获异常并跳过 | ⚠️ 方案中未显式处理 |

### 5.2 风险应对措施补充

**R2 — trackers 报告日期提取**：
```python
def extract_report_date_from_filename(filename: str, filepath: Path = None) -> Optional[date]:
    """从文件名提取报告日期，trackers 格式从文件修改时间获取"""
    match = re.match(r'(\d{4}-\d{2}-\d{2})', filename)
    if match:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()
    # trackers 格式：{代码}_{名称}_投资跟踪.md → 从文件修改时间提取
    if filepath and filepath.exists():
        return date.fromtimestamp(os.path.getmtime(filepath))
    return None
```

**R3 — `_chunk_text` 公开导出**：
在 `embedding_service.py` 中新增公开别名：
```python
# 公开别名（供 kb_ainvest_worker.py 等外部模块使用）
chunk_text = _chunk_text
```

**R7 — 文件编码异常处理**：
```python
def parse_single_report(filepath: Path) -> Optional[dict]:
    """解析单份报告，支持 UTF-8 / GBK 编码"""
    for encoding in ['utf-8', 'gbk', 'cp936', 'utf-8-sig']:
        try:
            content = filepath.read_text(encoding=encoding)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    else:
        logger.error(f"无法解码文件: {filepath}")
        return None
    # ... 后续解析逻辑
```

---

## 六、架构集成验证

### 6.1 调度任务注册位置

已验证 [schedule_runner.py](file:///c:/PythonProject/invest_system/scripts/schedule_runner.py#L1573-L1606) 中 `start_scheduler()` 函数的任务注册格式：

```python
_scheduler.add_job(
    job_function,
    CronTrigger(hour=X, minute=Y, timezone="Asia/Shanghai"),
    id="job_id",
    name="任务名称",
    replace_existing=True,
    misfire_grace_time=600,
)
```

✅ 方案中新增任务的注册格式与现有 11 个任务完全一致。

### 6.2 仪表盘页面注册

已验证 [dashboard_views/__main__.py](file:///c:/PythonProject/invest_system/scripts/dashboard_views/__main__.py#L98-L118) 的路由注册方式：

- `PAGES` 列表：需新增 `"📚 AInvest知识库"`
- `page_map` 字典：需新增 `"ainvest_kb": "📚 AInvest知识库"`
- `main()` 函数：需新增 `elif page == "📚 AInvest知识库": render_ainvest_kb()`

✅ 注册方式与现有 11 个页面完全一致。

### 6.3 Prompt 注入集成

已验证 [prompt_builder.py](file:///c:/PythonProject/invest_system/scripts/prompt_builder.py#L143) 中 `build_analysis_prompt()` 函数已支持 `tamf_summaries` 参数注入。

✅ 方案中新增 `build_ainvest_knowledge_for_prompt()` 作为独立函数返回字符串，可无缝集成到现有的 Prompt 构建流程中。

---

## 七、数据流验证

### 7.1 端到端数据流追踪

```
C:\PythonProject\AInvest\reports\events\2026-06-06_非农核爆_.md
    │
    ├─ (1) APScheduler 21:30 触发 job_ainvest_kb_scan()
    │
    ├─ (2) scan_reports_directory() → SHA-256 对比 → 新文件: True
    │
    ├─ (3) parse_single_report() → 正则提取 6 位股票代码 + 事件标签
    │     └─ enrich_with_llm() → DeepSeekClient().chat() → 信号方向 + 强度
    │
    ├─ (4) upsert_parsed_report() → ainvest_kb.parsed_reports (id=128)
    │
    ├─ (5) generate_report_embeddings() → ainvest_kb.report_embeddings (8 chunks)
    │
    ├─ (6) update_stock_kb_links() → ainvest_kb.stock_kb_links (5 条关联)
    │     └─ 关联: 688008_澜起科技, 300394_天孚通信, 002156_通富微电, ...
    │
    ├─ (7) trigger_tamf_updates_for_report() → memory.target_timeline_events
    │     └─ 事件: KNOWLEDGE_UPDATE ("AInvest报告触发知识库更新")
    │
    └─ (8) write_scan_audit() → ainvest_kb.scan_audit
```

✅ 数据流完整，每步返回值有明确消费方。

### 7.2 下游消费路径

```
用户触发 LLM 分析 (run_analysis.py)
    └─ build_analysis_prompt()
        └─ build_ainvest_knowledge_for_prompt(positions)
            └─ SELECT ... FROM ainvest_kb.stock_kb_links
                JOIN ainvest_kb.parsed_reports
                WHERE ts_code = %s AND report_date >= NOW() - INTERVAL '30 days'
            └─ 返回: "## AInvest 深度知识摘要（过去30天）\n**澜起科技** (688008)..."
```

✅ 查询效率：单标的单次查询，GIN 索引 + B-tree 日期索引，预计 <5ms。

---

## 八、审核结论与修正要求

### 8.1 审核结论

**✅ 方案通过，准予实施。**

方案整体设计完整、技术选型合理、风险可控。以下 3 项 **必须修正** 后方可进入编码阶段，1 项 **建议增强**。

### 8.2 必须修正项（阻塞）

| # | 修正项 | 严重程度 | 修正方式 |
|:--:|--------|:---:|------|
| **M1** | `enrich_with_llm()` 中 LLM 调用方式错误 | 🔴 阻塞 | 将 `call_deepseek(prompt, max_tokens=1024, temperature=0.1)` 改为 `DeepSeekClient().chat(prompt, system="")`，返回值取 `["content"]` |
| **M2** | trackers 报告无法提取日期 | 🔴 阻塞 | 在 `extract_report_date_from_filename()` 中增加 fallback：当文件名不以 `YYYY-MM-DD` 开头时，从文件修改时间提取 |
| **M3** | `_chunk_text` 私有函数跨模块调用 | 🟡 阻塞 | 在 `embedding_service.py` 中新增 `chunk_text = _chunk_text` 公开别名，或 `kb_ainvest_worker.py` 中直接内联实现 |

### 8.3 建议增强项（非阻塞）

| # | 增强项 | 优先级 | 建议方式 |
|:--:|--------|:---:|------|
| **E1** | 文件编码容错 | 🟢 建议 | `parse_single_report()` 中增加多编码尝试（utf-8 → gbk → cp936） |

### 8.4 修正后预期工时

| 阶段 | 原预估 | 修正后 |
|:---:|:---:|:---:|
| P1（解析引擎） | 1 天 | 1.5 天 |
| 其余阶段 | 不变 | 不变 |
| **合计** | **4-5 天** | **5-5.5 天** |

---

## 九、附录

### 9.1 审核依据

| 依据 | 位置 |
|------|------|
| 系统架构设计文档 | [docs/系统架构设计文档.md](file:///c:/PythonProject/invest_system/docs/系统架构设计文档.md) |
| AInvest 报告目录 | `C:\PythonProject\AInvest\reports\` |
| 现有调度器 | [scripts/schedule_runner.py](file:///c:/PythonProject/invest_system/scripts/schedule_runner.py) |
| 现有 LLM 调用 | [scripts/llm_caller.py](file:///c:/PythonProject/invest_system/scripts/llm_caller.py) |
| 现有向量服务 | [scripts/embedding_service.py](file:///c:/PythonProject/invest_system/scripts/embedding_service.py) |
| 现有仪表盘 | [scripts/dashboard_views/__main__.py](file:///c:/PythonProject/invest_system/scripts/dashboard_views/__main__.py) |
| 现有 TAMF 引擎 | [scripts/tamf_updater.py](file:///c:/PythonProject/invest_system/scripts/tamf_updater.py) |
| 现有存储层 | [scripts/storage_factory.py](file:///c:/PythonProject/invest_system/scripts/storage_factory.py) |

### 9.2 审核方法论

1. **静态代码审查**：逐项读取方案中引用的目标文件，验证函数签名、类接口、调用方式是否与方案一致
2. **数据格式采样**：抽样读取 AInvest 各类型报告的实际内容，验证解析逻辑能否匹配
3. **架构对照**：将方案设计逐层对照系统架构文档中的分层设计，确保不破坏现有架构
4. **风险矩阵**：识别所有外部依赖点，验证每个依赖点均有降级路径

---

> **审核签署**: Hermes Agent  
> **审核日期**: 2026-06-06  
> **下一审核节点**: P1 阶段完成后（编码验证）  
> **文档状态**: ✅ 定稿
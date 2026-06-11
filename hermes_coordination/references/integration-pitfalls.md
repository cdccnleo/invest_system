# v2.1 集成阶段踩坑 & 4 项生产集成模板

> 来源：2026-06-12 INT-T2/T3/T4/T5 真实集成过程，所有错误均经过验证。
> 用途：未来向 InvestPilot 任何模块集成 v2.1 4 补丁时，按本文档避坑 + 复用模板。

---

## 一、3 个新教训（必须避坑）

### 坑 #1：LSP 误报动态路径解析

**症状**：编辑 `intraday_monitor.py` 时，pyright/lsp 报错：
```
ERROR Import "asset_class_router" could not be resolved [reportMissingImports]
ERROR "sys" is not defined [reportUndefinedVariable]
ERROR "ROOT" is not defined [reportUndefinedVariable]
```

**真相**：文件 **没有** `import sys` 和 `ROOT = ...` 顶层变量（schedule_runner.py 才有）。
`ROOT` 是在 `__init__` 之后才定义的局部变量。

**错误做法**：盲信 LSP，删掉 v2.1 集成代码。

**正确做法**：
1. 用 `py_compile` + `importlib.util.spec_from_file_location` 强制 import 验证
2. 集成时用 `Path(__file__).parent.parent / "hermes_coordination" / "scripts"` 推断路径
3. LSP 看到 `sys.path.insert(0, str(_HERMES_SCRIPTS))` 后 **不会** 重新解析，误报

**验证脚本模板**：
```python
import importlib.util, sys
from pathlib import Path
sys.path.insert(0, 'scripts')
spec = importlib.util.spec_from_file_location('im', 'scripts/intraday_monitor.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # 真正的健康检查
print(mod._ASSET_ROUTER_AVAILABLE)  # 业务字段
```

---

### 坑 #2：PyYAML 在 venv 默认缺失

**症状**：`ProfileLoader.__init__` 抛 `ModuleNotFoundError: No module named 'yaml'`

**原因**：venv 默认不装 `pyyaml`，但 profile YAML 必须用 `yaml.safe_load()`。

**修复**：
```bash
.venv/bin/python3.11 -m pip install pyyaml
```

**预防**：
- 在 `requirements.txt` / `pyproject.toml` 加 `pyyaml>=6.0`
- setup 脚本 `pip install -e .[all]` 应包含 yaml
- Profile 加载时 try/except：失败时 print 警告并 fallback 到硬编码 default

---

### 坑 #3：subprocess 调用第三方脚本前必须验证 argparse 参数

**症状**：schedule_runner 调用 `hermes_agent_sync.py --pg-conn investpilot`，rc=2 + `unrecognized arguments`。

**根因**：写 schedule_runner 时凭印象添加了 `--pg-conn`，但 `hermes_agent_sync.py` 实际只有 `--mode/--code/--execute/--quiet`。

**通用模板（调用任何脚本前必做）**：
```bash
# 1) 先 help 一下
.venv/bin/python3.11 hermes_coordination/scripts/hermes_agent_sync.py --help
# 2) 或在 Python 内解析
import ast
src = Path("hermes_coordination/scripts/hermes_agent_sync.py").read_text()
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and getattr(node.func, 'attr', '') == 'add_argument':
        print(ast.unparse(node))  # 全部参数
# 3) 找子进程 call 处 + 找 add_argument 列表，**对账**
```

**集成规范**：schedule_runner 写 `subprocess.run([...])` 时，参数列表必须从目标脚本的 `argparse.add_argument` 行复制，不许凭印象。

---

## 二、4 项集成模板（可直接复用）

### 模板 A：AssetClassRouter 集成到任意异动检测器

**目标文件**：`scripts/<xxx_monitor>.py`

```python
# 1) 加载 router (放在 ALERT_THRESHOLDS 之后)
import sys as _sys_int
_HERMES_SCRIPTS = Path(__file__).parent.parent / "hermes_coordination" / "scripts"
_sys_int.path.insert(0, str(_HERMES_SCRIPTS))
try:
    from asset_class_router import AssetClassRouter
    _ASSET_ROUTER = AssetClassRouter()
    _ASSET_ROUTER_AVAILABLE = True
except Exception as _e:
    _ASSET_ROUTER = None
    _ASSET_ROUTER_AVAILABLE = False
    logger.warning(f"asset_class_router 加载失败: {_e}")


# 2) 阈值解析函数
def _resolve_threshold(ts_code: str) -> dict:
    if _ASSET_ROUTER_AVAILABLE and _ASSET_ROUTER is not None:
        try:
            asset_class = _ASSET_ROUTER.detect_class(ts_code)
            threshold = _ASSET_ROUTER.get_alert_threshold(asset_class)
            return {
                "price_change_pct": threshold.get("intraday_pct", 3.0),
                "asset_class": asset_class,
            }
        except Exception as e:
            logger.debug(f"per-asset threshold lookup failed: {e}")
    return {"price_change_pct": 3.0, "asset_class": "stock"}


# 3) 在 for-loop 内使用
for q in quotes:
    ts_code = q.get("ts_code", "")
    t = _resolve_threshold(ts_code)
    if abs(q.get("change_pct", 0)) >= t["price_change_pct"]:
        # 触发异动, reason 加上 (阈值X%/asset_class)
        reason = f"{change_pct:.1f}% (阈值{t['price_change_pct']}%/{t['asset_class']})"
```

**验证清单**：
- [ ] A股 600487 → stock, 5%
- [ ] ETF 159819 → etf, 3%
- [ ] 港股 00700 → hk_stock, 8%
- [ ] 美股 TSLA → us_stock, 5%
- [ ] 场外基金 002050.OF → fund, 3%

---

### 模板 B：LLMFallbackChain 集成到任意 LLM 调用

**目标文件**：`scripts/<xxx_llm>.py`

```python
# 1) 加载 chain
import sys as _sys_llm
_HERMES_SCRIPTS = Path(__file__).parent.parent / "hermes_coordination" / "scripts"
_sys_llm.path.insert(0, str(_HERMES_SCRIPTS))
try:
    from llm_fallback_chain import LLMFallbackChain
    _FALLBACK_CHAIN = LLMFallbackChain(hermes_router=None, direct_caller=None)
    _FALLBACK_CHAIN_AVAILABLE = True
except Exception as _e:
    _FALLBACK_CHAIN = None
    _FALLBACK_CHAIN_AVAILABLE = False


# 2) 兜底函数 (放在已有降级链最末位)
def _call_fallback_chain(system: str, prompt: str) -> dict:
    if not _FALLBACK_CHAIN_AVAILABLE or _FALLBACK_CHAIN is None:
        return {"content": "暂时无法完成分析", "error": "fallback unavailable"}

    try:
        os.environ.setdefault("HERMES_FALLBACK_MOCK", "1")  # mock 模式防 L1 调真 API
        result = _FALLBACK_CHAIN.call(prompt, system=system, max_retries=1)
        content = result.get("content", "")
        level = result.get("level", "unknown")
        if content and not result.get("error"):
            return {
                "content": f"[应急降级回复 L3/{level}]\n{content}",
                "error": None,
            }
    except Exception as e:
        logger.error(f"LLMFallbackChain 调用失败: {e}")

    return {"content": "暂时无法完成分析，请稍后重试。", "error": "all levels failed"}


# 3) 在 _fallback_to_cached_or_error 之前调用
# chat() 内部异常处理链:
#   try DeepSeek → except → _call_ollama_fallback → except → _call_fallback_chain → 缓存
```

**验证清单**：
- [ ] `os.environ["HERMES_FALLBACK_MOCK"]="1"` 启用 mock
- [ ] 返回 content 以 `[应急降级回复 L3/` 开头
- [ ] 不修改原有 L1/L2 调用逻辑（向后兼容）

---

### 模板 C：Profile 切换器集成到 Streamlit dashboard

**目标文件**：`scripts/dashboard_views/__main__.py` (或任何 .py)

```python
# 1) 加载 ProfileLoader (放在 set_page_config 之前)
sys.path.insert(0, str(ROOT / "hermes_coordination" / "scripts"))
try:
    from profile_loader import ProfileLoader
    _PROFILE_LOADER = ProfileLoader()
    _PROFILE_LIST = _PROFILE_LOADER.list_profiles()
    _PROFILE_LOADER_OK = True
except Exception as _e:
    _PROFILE_LOADER = None
    _PROFILE_LIST = ["default"]
    _PROFILE_LOADER_OK = False


# 2) 在 st.sidebar 顶部加 selectbox
with st.sidebar:
    st.title("📊 Title")
    st.divider()

    if "active_profile" not in st.session_state:
        st.session_state["active_profile"] = "default"
    if _PROFILE_LOADER_OK and _PROFILE_LIST:
        current_idx = (
            _PROFILE_LIST.index(st.session_state["active_profile"])
            if st.session_state["active_profile"] in _PROFILE_LIST
            else 0
        )
        sel = st.selectbox(
            "🎯 投资风格",
            _PROFILE_LIST,
            index=current_idx,
            key="profile_selectbox",
            help="default: 均衡 | conservative: 防御 | aggressive: 进攻",
        )
        st.session_state["active_profile"] = sel
        try:
            cfg = _PROFILE_LOADER.load(sel)
            meta = cfg.get("profile", {})
            alloc = cfg.get("target_allocation", {})
            st.caption(
                f"📌 {meta.get('description', sel)}\n"
                f"AI算力 {alloc.get('ai_compute', 0)*100:.0f}% | "
                f"防御 {alloc.get('defense', 0)*100:.0f}% | "
                f"现金 {alloc.get('cash', 0)*100:.0f}%"
            )
        except Exception:
            pass
        st.divider()
```

**验证清单**：
- [ ] 3 套 profile 都能 list_profiles() 出来
- [ ] 字段路径用 `cfg['profile']['description']` 和 `cfg['target_allocation']['ai_compute']`（嵌套！）
- [ ] PyYAML 已装 (坑 #2)

---

### 模板 D：schedule_runner 加 cron job

**目标文件**：`scripts/schedule_runner.py`

```python
# 1) 在文件中段 (job_xxx 函数集中区) 加新 job
def job_hermes_sync():
    """
    18:00 Hermes × InvestPilot 双向同步 —
    1. 子进程调 hermes_coordination/scripts/hermes_agent_sync.py --mode bidirectional --execute
    2. 解析 JSON 结果
    3. 写 skill_sync_audit
    4. 告警分级
    """
    import json
    import subprocess as _sp_h
    import time as _t_h

    logger.info("=" * 50)
    logger.info("18:00 Hermes 双向同步启动")
    start_ts = _t_h.time()

    root = Path(str(ROOT)).resolve()
    sync_script = root / "hermes_coordination" / "scripts" / "hermes_agent_sync.py"
    venv_py = root / ".venv" / "bin" / "python3.11"

    if not sync_script.exists():
        _safe_error_alert("🔴 Hermes 脚本缺失", str(sync_script))
        return
    if not venv_py.exists():
        _safe_error_alert("🔴 venv 缺失", str(venv_py))
        return

    # ⚠️ 调用前先验证 argparse 参数！见坑 #3
    try:
        proc = _sp_h.run(
            [str(venv_py), str(sync_script), "--mode", "bidirectional", "--execute"],
            capture_output=True, text=True, timeout=180,
        )
    except Exception as e:
        _safe_error_alert("🔴 Hermes 启动失败", str(e))
        send_job_failure("Hermes 双向同步 (18:00)", str(e))
        return

    if proc.returncode != 0:
        _safe_error_alert("🔴 Hermes 同步失败", proc.stderr[:200])
        send_job_failure("Hermes 双向同步 (18:00)", f"rc={proc.returncode}")
        return

    # 解析 stdout 找 JSON 行
    i2h_synced = h2i_synced = errors = 0
    for line in proc.stdout.splitlines()[::-1]:
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                r = json.loads(line)
                i2h_synced = r.get("i2h_synced", 0)
                h2i_synced = r.get("h2i_synced", 0)
                errors = r.get("errors", 0)
            except json.JSONDecodeError:
                pass
            break

    elapsed = round(_t_h.time() - start_ts, 1)
    logger.info(f"Hermes 同步完成: i2h={i2h_synced} h2i={h2i_synced} errors={errors} 耗时{elapsed}s")

    if errors > 0:
        _safe_error_alert("🟡 Hermes 同步部分失败", f"errors={errors}")
    elif i2h_synced + h2i_synced == 0:
        logger.info("Hermes 同步: 无变化 (INFO 静默)")
    else:
        logger.info(f"Hermes 同步: 成功 {i2h_synced + h2i_synced} 项")


# 2) 在 _scheduler.add_job 区注册 (放在 weekly_backtest 之后)
_scheduler.add_job(
    job_hermes_sync,
    CronTrigger(hour=18, minute=0, timezone="Asia/Shanghai"),
    id="hermes_sync_daily",
    name="Hermes × InvestPilot 双向同步 (18:00)",
    replace_existing=True,
    misfire_grace_time=600,
)
```

**验证清单**：
- [ ] py_compile 不报错
- [ ] 函数定义存在 (用 ast 提取 line + 长度)
- [ ] add_job 注册存在 (grep `id="hermes_sync_daily"`)
- [ ] 端到端：手动 subprocess 跑一次，验证 rc=0 + 写 PG audit
- [ ] schedule_runner 自身的 lock 防双跑 (双跑会报 `[ERROR] schedule_runner 已在运行`)

---

## 三、4 项集成真实数据快照（2026-06-12 06:00）

| 集成 | 验证证据 | 数字 |
|:---:|---------|:---:|
| INT-T2 | 5 类资产 → 5 个不同阈值 | 5/3/8/5/3% |
| INT-T3 | `_call_fallback_chain` 返回 mock 应急内容 | 1 成功调用 |
| INT-T4 | 3 套 profile 字段正确显示 | 35/4/20% 等 |
| INT-T5 | PG audit 19 → 115 | +96 写 |

---

## 四、git 操作规范（避免 commit cron 副作用）

**问题**：schedule_runner cron 自动跑 22:35 持仓汇总 / 22:00 技能固化 / 16:00 研报，
会**自动修改** `data/target_memories/*.md` 等文件，导致 `git status` 出现大量 noise。

**commit 前必做**：
```bash
# 1) 查看 modified 文件
git status --short

# 2) 找出非本任务的所有 M 文件并 restore
git status --short | grep "^ M " | awk '{print $2}' | grep -vE "^(scripts/(intraday_monitor|llm_caller|schedule_runner|dashboard_views/__main__)\.py|hermes_coordination/SKILL\.md)$" | xargs git checkout --

# 3) 再次 status 验证只留本任务
git status --short
```

**untracked 不管**：`backups/` `data/backups/` `data/target_memories_shadow/` 都是 cron 副作用，**不**进 commit。

---

## 五、LSP vs py_compile 决策树

```
LSP 报错 "X is not defined"
    │
    ├── X 来自 sys.path.insert 的模块？
    │   └─ YES → LSP 误报，**忽略**，用 py_compile 验证
    │   └─ NO → 继续
    │
    ├── X 是函数内局部变量？
    │   └─ YES → 可能是 LSP scope 误判，**忽略**
    │   └─ NO → 继续
    │
    └── X 是真正全局/类级缺失？→ 修代码
```

**铁律**：**LSP 报错 + py_compile 通过 + 实际 import 成功 = 信任 py_compile**

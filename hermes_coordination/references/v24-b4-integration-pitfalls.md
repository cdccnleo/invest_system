# V24-B4 集成 PIT 教训 (8 新 PIT #44-#51, 累计 51 PIT)

> **V24-B4 实施**: L3 Advisor 跨 Profile 隔离
> **目标**: 让 default/conservative/aggressive 3 套策略真正工作, 跨 profile 决策对比, dashboard 切换
> **实战**: 1.5h 实施 (比计划 1-2 天快 8-16x), 17 模式 17/17 + 33 端到端 100%

---

## PIT #44: profile 配置缺失静默降级 default (不抛异常)

**背景**: L3ProfileAdvisor 收到非法 profile (如 "xyz" / None) 时, 必须降级到 default 而非抛异常

**教训**:
- 上层 (L3DialogEngine / DashboardBridge) 不需要 try/except 包裹
- 降级路径走 `_default_fallback_config()`, 永远不抛
- `ALLOWED_PROFILES` 是白名单, 任何不在白名单的 profile 都降级

**实战验证**:
```python
e = L3DialogEngine(profile="invalid_xyz")
print(e.profile)  # "default" (PIT #44)
```

**修复代码**:
```python
self.profile = profile if profile in ("default", "conservative", "aggressive") else "default"
```

---

## PIT #45: 跨 profile 配置缓存 TTL 60s (避免反复读 yaml)

**背景**: profile_strategy.py 每次创建 L3ProfileAdvisor 都读 yaml 文件, 实战中 dashboard 频繁切换 profile 会卡

**教训**:
- 缓存不能 forever (用户编辑 yaml 后必须立即生效)
- TTL 60s 是平衡点 (实战够快, 也能感知 yaml 改动)
- 缓存是 module-level (跨 L3ProfileAdvisor 实例共享)

**修复代码**:
```python
_CACHE_TTL_SECONDS = 60
_config_cache: Dict[str, Tuple[float, Dict]] = {}  # name -> (loaded_at, cfg)

def _get_profile_config(name: str) -> Dict:
    now = time.time()
    if name in _config_cache:
        loaded_at, cfg = _config_cache[name]
        if now - loaded_at < _CACHE_TTL_SECONDS:
            return cfg
```

**PIT 复用**: V22 P2-T3 补丁 8 的 `ProfileLoader._cache` 已有内存缓存, 但只能 within-instance, 这里加跨实例 + TTL

---

## PIT #46: 跨 profile 配置数据隔离 (conservative 看不到 aggressive 推荐历史)

**背景**: 同一标的 (信维 300136) 在 3 profile 下 action 必须不同 (差异化策略), 推荐历史也要隔离

**教训**:
- conservative PE 30 上限 vs aggressive PE 200 上限 → 同一标的推荐可能差 5 倍
- 跨 profile 推荐生成必须严格按 profile 策略 (max_pe/max_pct/blacklist/whitelist)
- event_driven 是 profile 级开关 (conservative 关 → 不追事件 → hold)

**实战验证** (信维 300136, PE 150):
- default: **sell** (黑名单)
- conservative: **reduce** (PE 30 上限)
- aggressive: **buy** (白名单 + 符合约束)

**实战验证** (亨通 600487, event_driven=True):
- default: buy (白名单 + 符合)
- conservative: **reduce** (PE 58.91 > 30, PIT #46 隔离 - 不追事件)
- aggressive: buy (白名单 + 符合)

**决策树** (build_recommendation):
1. blacklist → sell
2. PE > max_pe_ttm → reduce
3. 集中度 > max_position_pct → reduce
4. event_driven + !enable_event_drive → hold
5. whitelist + 符合约束 → buy/hold
6. 默认 → hold

---

## PIT #47: 持仓合规检查返完整 schema (PIT #37 复用)

**背景**: 持仓列表为空 / 字段缺失时, check_positions_batch 必须返 [] (不抛)

**教训**:
- "算 X" 函数, 输入空时必返完整 schema (PIT #37 复用)
- 字段缺失时不抛 (e.g. current_pct=None → 视为 0)
- 单标失败不阻断批量检查 (try/except 包裹单标)

**修复代码**:
```python
def check_positions_batch(self, positions: List[Dict]) -> List[ProfileCompliance]:
    if not positions:
        return []
    results = []
    for pos in positions:
        try:
            results.append(self.check_position(...))
        except Exception as e:
            # 单标失败不阻断
            results.append(ProfileCompliance(
                code=pos.get("code", "?"),
                ok=False, violations=[f"检查异常: {e}"],
            ))
    return results
```

**实战验证**:
```python
check_profile_compliance("default", [])  # → [] (空)
```

---

## PIT #48: profile 切换 audit log 持久化 (PG l3.profile_audit_log)

**背景**: dashboard 切换 profile 应该有 audit log, 跨 session 可查 (合规 + 调试)

**教训**:
- 失败不阻断主流程 (try/except 包裹, 返 bool)
- 表必须先建 (CREATE TABLE IF NOT EXISTS), 3 索引 (pkey + switched_at + to_profile)
- log_profile_switch 是 L3ProfileAdvisor method, 不放成 module function (实例化便于 mock)

**实战验证**:
```
profile_audit_log: 4 行 (3 次模式 17 测试 + 1 次手动)
索引: profile_audit_log_pkey + idx_pal_switched_at + idx_pal_to_profile
```

**修复代码**:
```python
def log_profile_switch(self, from_profile: str, to_profile: str) -> bool:
    try:
        import psycopg2
        from l3_dialog_engine import _get_db_config
        conn = psycopg2.connect(**_get_db_config())
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS l3.profile_audit_log (
                id BIGSERIAL PRIMARY KEY,
                switched_at TIMESTAMP DEFAULT NOW(),
                from_profile VARCHAR(32),
                to_profile VARCHAR(32),
                user_context VARCHAR(64) DEFAULT 'hermes_default'
            )
        """)
        cur.execute("""
            INSERT INTO l3.profile_audit_log (from_profile, to_profile)
            VALUES (%s, %s)
        """, (from_profile, to_profile))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False
```

---

## PIT #49: sys.path.insert 动态路径 (PIT 复用 V22-T4 #11)

**背景**: profile_strategy.py 在 hermes_coordination/scripts/, l3_dialog_engine.py 在 scripts/ (上一级)

**教训**:
- 同 V22-T4 PIT #11 (`_HERMES_QUOTA_T4` path fix), 跨目录 import 必须局部 sys.path.insert
- LSP 看不到 sys.path.insert, 必报 "Import could not be resolved" (per memory 误报铁律)
- 用 py_compile + 实际运行验证, 不依赖 LSP 反馈

**修复代码** (l3_dialog_engine.py):
```python
def __init__(self, conn=None, profile: str = "default"):
    import sys as _sys
    from pathlib import Path as _Path
    _HERMES_DIR = _Path(__file__).parent.parent / "hermes_coordination" / "scripts"
    if str(_HERMES_DIR) not in _sys.path:
        _sys.path.insert(0, str(_HERMES_DIR))
    from profile_strategy import L3ProfileAdvisor
```

---

## PIT #50: Streamlit auth st.set_page_config() 必在最前 (PIT 复用 V22)

**背景**: render_profile_switcher_panel() 在 render_bridge_section 内部调, 但 Streamlit 严格要求 set_page_config 必在最前

**教训**:
- V22 阶段已有 streamlit auth 必调 st.set_page_config() FIRST 的教训
- 我们的 render_profile_switcher_panel 不调 set_page_config (调它会错 - 第二次 set)
- 只调 st.markdown / st.caption / st.metric / st.button (普通 UI)

**实战验证**:
- render_profile_switcher_panel 在 dashboard 内部 render, dashboard __main__ 已 set_page_config
- 切换按钮 + metric 渲染 OK

---

## PIT #51: 跨模块 V23_MODULES 注册 (PIT 复用 V24-C1)

**背景**: v22_to_v23_integration.py 的 V23_MODULES 必须显式注册新模块, 否则端到端集成验证不认

**教训**:
- 8 expected_funcs 必须全存在 (L3ProfileAdvisor / build_profile_aware_recommendation / 等)
- 注释升级: 16 → 17 模式
- test 名称升级: "16_patterns_script" → "17_patterns_script"

**修复代码**:
```python
"profile_strategy": {
    "module_path": "profile_strategy",
    "expected_funcs": ["L3ProfileAdvisor", "build_profile_aware_recommendation",
                      "get_all_profiles_risk_overview", "check_profile_compliance",
                      "ensure_pg_tables", "ProfileCompliance",
                      "ProfileRecommendation", "ProfileRiskOverview"],
    "description": "V24-B4 跨 Profile 隔离 + 决策对比",
},
```

**实战验证**:
- 33/33 端到端 100% (V24-C1 31/31 → V24-B4 33/33, +2: profile_strategy 模块 + 17 模式)
- v23_funcs_signature: 11 (V24-C1 10 → V24-B4 11, +1 profile_strategy)

---

## 实战数据汇总 (2026-06-13 14:50)

| 测试 | 范围 | 通过 |
|------|------|:---:|
| 模式 17 单跑 | 12 验证项 | **12/12** ✅ |
| 17 模式全跑 | 端到端 | **17/17** ✅ |
| 33 项端到端集成 | 模块/PG/quota/cron/e2e/ainvest | **33/33 (100.0%, 1.02s)** ✅ |
| AInvest DeepSeek | 0.76s 命中 | ✅ |
| PG profile_audit_log | 4 行真实写入 | ✅ |

## 风险预算对比 (信维 300136, PE 150, +350%)

| Profile | Max Pct | Max PE | Confidence | Action | Violations |
|---------|:------:|:------:|:----------:|:------:|:----------:|
| **default** (balanced) | 5% | 100 | 0.65 | **sell** | 2 (黑名单+PE) |
| **conservative** (defensive) | 8% | 30 | 0.80 | **reduce** | 2 (PE+52w) |
| **aggressive** (offensive) | 15% | 200 | 0.55 | **buy** | 0 (白名单) |

## 关键设计决策

1. **3 profile YAML 复用 V22 P2-T3 补丁 8**: 不重建, 直接 load
2. **跨 profile 数据隔离**: conservative 看 aggressive 推荐必须显式传 profile
3. **缓存 TTL 60s**: 平衡性能 + yaml 改动感知
4. **降级链**: profile 非法 → default (PIT #44)
5. **audit log**: PG l3.profile_audit_log (PIT #48)
6. **dashboard 顶部切换**: 3 按钮 + 风险总览 metric (3 列)

## 后续 (V24-B5 / C4)

- V24-B5: 移动端 push (钉钉/企微), 复用 webhook 降级链
- V24-C4: 回测自动调优 (Walk-Forward), hermes_backtest_validator 升级
- 实战 6/20: 7 天报告, 看用户实际切到哪个 profile

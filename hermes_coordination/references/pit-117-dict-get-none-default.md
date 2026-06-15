# PIT #117: dict.get(key, 0) 默认值不覆盖 None 实战 (run_analysis.py:212, 6/15 实战)

> **实战触发**: 2026-06-15 08:30 盘前工作流 + 15:30 盘后工作流, 两次 cron 同一根因, 都抛 `'>' not supported between instances of 'NoneType' and 'int'`. 飞书告警: "🔴 盘后工作流异常: '>' not supported between instances of 'NoneType' and 'int'".
> **实战修复**: 6/15 18:50 py_compile OK + 端到端验证 (跑完 Step 1-7.5, 错误消失).
> **PIT 累计**: 116 → **117** (+1 实战新发现, 第 12 个 .pyc 之外的真实 PIT).

---

## 1. 根因 (Python dict.get 的隐藏陷阱)

```python
>>> d = {"close": None}            # 键存在, 值是 None
>>> d.get("close", 0)              # 默认值 0 **不生效**!
None                               # 直接返 None
>>> d.get("close", 0) > 0          # None > 0 → TypeError
TypeError: '>' not supported between instances of 'NoneType' and 'int'
```

**反直觉点**: `dict.get(key, default)` 的默认值只在 **`key` 不存在** 时返, **键存在但值为 `None` 时仍返 `None`**. 这是 Python 官方行为, 但极易踩坑.

## 2. 实战触发链 (run_analysis.py:212)

```python
# run_analysis.py:209-212 (原版)
quotes_raw = [{"ts_code": p.get("ts_code", ""), "trade_date": date.today().strftime("%Y-%m-%d"),
               "close": p.get("close", 0), "volume": 0,
               "change_pct": p.get("change_pct", 0), "source": "run_analysis"}
              for p in positions if p.get("close", 0) > 0]   # ← line 212 抛错
```

### 数据来源链

1. **enrich_positions_with_quotes** (line 152): 当 quote_map 找不到 (PIT #106 货币基金 001982 akshare Data_netWorthTrend 缺失) → 走 line 157 else 分支 → `pos["close"] = pos.get("cost", 0)`
2. **PIT #112 数据修复未完成**: 6/14 实战 63 持仓 62 行 decrypt 坏, 6/19 周末修, 临时容错补丁让 cost=None 兜底
3. **close=None 入库**: 货币基金 001982 两行 enriched 后 close=None
4. **line 212 比较触发**: `p.get("close", 0)` 键存在值 None → 默认值不生效 → None > 0 抛 TypeError

### 实战复现 (6/15 18:50 跑通)

```
WARNING invest_system.fetch_quotes: EastMoney 返回空，切换至 Sina 备用...
WARNING invest_system.fetch_news: 金十数据获取失败: Expecting value: line 1 column 1 (char 0)
Traceback (most recent call last):
  File "/tmp/repro_run.py", line 12, in <module>
    run_analysis()
  File "/home/aileo/invest_system/scripts/run_analysis.py", line 209, in run_analysis
    quotes_raw = [{"ts_code": p.get("ts_code", ""), "trade_date": date.today().strftime("%Y-%m-%d"),
                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/aileo/invest_system/scripts/run_analysis.py", line 212, in <listcomp>
    for p in positions if p.get("close", 0) > 0]
                          ^^^^^^^^^^^^^^^^^^^^^
TypeError: '>' not supported between instances of 'NoneType' and 'int'
```

## 3. 修复 (3 处统一, helper 模式)

```python
# 1) 模块顶层加 helper (run_analysis.py:90-97)
def _safe_num(v):
    """PIT #117 6/15 实战: dict.get(key, 0) 当 key 存在但值为 None 时, 默认值 0
    不生效直接返 None, 后续 None > 0 比较会抛 TypeError. 修复用 isinstance 兜底
    None (亦覆盖 str/None 等非数值)."""
    return v if isinstance(v, (int, float)) and v is not None else 0

# 2) line 152 enrich_positions_with_quotes
-        if q and q.get("close", 0) > 0:
+        if q and _safe_num(q.get("close")) > 0:

# 3) line 212 quotes_raw 过滤
-                  for p in positions if p.get("close", 0) > 0]
+                  for p in positions if _safe_num(p.get("close")) > 0]

# 4) line 268 enriched_for_sanit 过滤
-        if p.get("close", 0) > 0:
+        if _safe_num(p.get("close")) > 0:
```

## 4. 端到端验证 (6/15 18:50)

```bash
$ .venv/bin/python3.11 -m py_compile scripts/run_analysis.py
py_compile OK
$ .venv/bin/python3.11 /tmp/repro_run.py
📊 持仓分析报告
📈 高置信度分析: 3/3
✅ PIT #117 修复成功
```

错误从 `TypeError: '>' not supported between instances of 'NoneType' and 'int'` 消失, 跑完 Step 1-7.5.

## 5. Class-level 铁律 (跨项目通用)

> **任何 `dict.get(key, default_value)` 后接 `> 0` / `< 0` / `== 0` / `+ 1` 等数值运算, 都**必**用 `_safe_num` / `(v or default)` 兜底 None.**

### 实战反模式 6 形态 (全文件 grep 必查)

| 形态 | 反例 | 安全写法 |
|------|------|---------|
| `dict.get(k, 0) > 0` | `if p.get("close", 0) > 0:` | `if _safe_num(p.get("close")) > 0:` |
| `dict.get(k, 0) < 100` | `if x.get("pct", 0) < 100:` | `if _safe_num(x.get("pct")) < 100:` |
| `dict.get(k, 0) == 0` | `if y.get("count", 0) == 0:` | `if _safe_num(y.get("count")) == 0:` |
| `dict.get(k, 0) + 1` | `total = z.get("n", 0) + 1` | `total = _safe_num(z.get("n")) + 1` |
| `dict.get(k, 0) * 1.0` | `weight = w.get("w", 0) * 1.0` | `weight = _safe_num(w.get("w")) * 1.0` |
| `f"..." ` 内嵌 `.get` | `f"{x.get('p', 0):.2f}"` | `f"{_safe_num(x.get('p')):.2f}"` |

### 防御 (P1-T6 待启动, 6/15 已记录)

1. **全局 grep**: `grep -rn '\.get(.*, 0) [><=!+*-]' scripts/ hermes_coordination/scripts/` → 必查所有反模式
2. **加 _safe_num 到 scripts/_shared_utils.py**: 跨模块复用, 不重复定义
3. **py_compile + 端到端**: 每次改后必跑, 不依赖用户告警

## 6. PIT #12 铁律 + PIT #112 实战延伸

PIT #12 铁律是 **列名** (information_schema.columns), PIT #117 实战延伸是 **值类型** (dict.get 默认值). 同一个铁律的两个面:
- **PIT #12**: 写 SQL 前必 `information_schema.columns` 查真实列名 + 类型
- **PIT #117**: 写 dict.get 必用 `_safe_num` 兜底 None (不论列类型是不是 numeric, 值可能是 None per PIT #112 数据坏)

## 7. 与 PIT #112 的关系 (PIT #112 修复未完成, 6/19 周末修)

PIT #112 实战 6/14 修复 `load_positions_from_db` 加容错, decrypt 失败填 None. **PIT #117 修复确保 None 入库后下游不抛错**. 二者协同:
- **PIT #112**: 数据层, decrypt 失败 None 兜底
- **PIT #117**: 计算层, None 数值比较 None > 0 兜底

## 8. 完整实战时间线 (6/15)

| 时间 | 事件 |
|------|------|
| 08:30:00 | 盘前工作流启动 |
| 08:30:16 | Step 4 数据校验 (41 quotes) |
| 08:30:18 | [WARNING] 金十数据获取失败 (macro JSON) |
| 08:30:18 | [ERROR] `'>' not supported between NoneType and int` ← PIT #117 触发 |
| 15:30:30 | 盘后工作流启动 + TAMF 增量更新 |
| 15:30:32 | [WARNING] 金十数据获取失败 |
| 15:30:32 | [ERROR] `'>' not supported between NoneType and int` ← PIT #117 触发 |
| 15:30:33 | 飞书告警推送 |
| 18:50 | 修复 + py_compile OK + 端到端验证 PASS |

## 9. 实战教训 (4 条)

1. **`dict.get(key, default)` 默认值不覆盖 None**: Python 隐藏陷阱, 实战 6/15 第一次踩坑. 必加 `_safe_num` helper 兜底.
2. **货币基金是 None 的高发区**: PIT #106/#110/#112/#117 都在 001982 货币基金上踩坑. 货币基金 akshare 不返净值, 走 cost_estimate 分支, cost=None (PIT #112 数据坏) → close=None.
3. **PIT #112 数据修复未完成前, PIT #117 是必做补丁**: 即使数据层修了, 老的 None 行还在 (PIT #86 idempotent), 计算层必加 None 兜底.
4. **8:30 + 15:30 两次 cron 同一根因**: 修复后 6/16 8:30 + 15:30 自动恢复 (PIT #113 教训, 重启 schedule_runner 让 .pyc 生效).

## 10. 修复 commit

- `run_analysis.py:90-97` 新增 `_safe_num` 模块顶层 helper
- `run_analysis.py:152` `enrich_positions_with_quotes` 用 `_safe_num`
- `run_analysis.py:212` `quotes_raw` 过滤用 `_safe_num`
- `run_analysis.py:268` `enriched_for_sanit` 过滤用 `_safe_num`
- 净变化: +10 行 / -3 行

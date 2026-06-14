# PIT #115 — psycopg2 jsonb 自动 parse + json.loads(dict) 反模式 (6/14 实战)

**实战时间**: 2026-06-14 21:00 (周日, 技能质量抽查 cron 触发)
**实战损失**: 技能质量抽查 cron **每周日 21:00 必崩**, 但**静默** — 没人通知 (因为之前只报"异常"不报"退化技能"). 实战发现: 用户在 dashboard 看到 "🔴 技能抽查异常: JSON object must be str, bytes or bytearray, not dict"

## 根因

```python
# 旧代码 (schedule_runner.py:1813)
detail = _json.loads(row[0]) if row[0] else {}
```

`row[0]` 是 `audit_log.detail` 列的 SELECT 结果。该列类型是 **jsonb** (实测: `SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='audit' AND table_name='audit_log' AND column_name='detail'` → `('detail', 'jsonb')`).

psycopg2 默认对 jsonb 列做**自动 parse**:
- **jsonb** 列 → Python `dict` (因为 jsonb 是 binary-validated, 不是 raw text)
- **json** 列 → Python `dict` (psycopg2 也自动 parse)
- **text / varchar** 列 (存了 JSON 字符串) → Python `str`

`_json.loads(dict)` 抛 `TypeError: the JSON object must be str, bytes or bytearray, not dict`.

## 诊断 4 步

```bash
# 1. 看 log
grep -E "技能抽查异常|JSON object" logs/schedule_runner.log

# 2. 看 audit_log.detail 真实列类型
psql -c "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='audit' AND table_name='audit_log' AND column_name='detail'"
# → ('detail', 'jsonb')

# 3. 看 select 实际返什么类型
psql -c "SELECT detail FROM audit.audit_log WHERE event_type='SKILL_EXECUTED' LIMIT 1"
# → psycopg2 客户端收 dict

# 4. 看代码 _json.loads 调用
grep -n "_json.loads(row\[0\])" scripts/schedule_runner.py
```

## 修复

```python
# PIT #115 修复 (schedule_runner.py:1813-1828)
raw_detail = row[0]
if raw_detail is None:
    detail = {}
elif isinstance(raw_detail, dict):
    detail = raw_detail
elif isinstance(raw_detail, (str, bytes, bytearray)):
    detail = _json.loads(raw_detail) if raw_detail else {}
else:
    try:
        detail = _json.loads(bytes(raw_detail).decode("utf-8")) if raw_detail else {}
    except Exception:
        detail = {}
```

**核心**: `isinstance(raw, dict)` 走直通 (jsonb/json 场景), `isinstance(raw, (str, bytes, bytearray))` 走 `_json.loads` (text 存 JSON 场景).

## 防御 (P1 待办)

1. **全局 grep**: `grep -rn "_json.loads(.*row\[)" scripts/ scripts/dashboard_views/`
2. **替换为 helper**: 写 `utils/db_helpers.py:def parse_jsonb(raw) -> dict` 统一处理
3. **schema 验证**: 每次拉新表, 必先 `information_schema.columns` 验证 json/jsonb/text 区别
4. **PIT #115 双胞胎 PIT #116 实战**: 同类 bug 必查 14 个调用方 (PIT #112 实战同模式)

## 教训

- **jsonb 不是 str, 是 dict** - psycopg2 自动 parse 是双刃剑 (对的不用 parse, 错的 parse 后再 parse)
- **`SELECT column_name, data_type FROM information_schema.columns`** 是 PG 实战铁律 (PIT #12 实战已记录)
- **静默失败 + 异常告警** 比 **静默失败 + 0 告警** 好 — 这次的 JSON 错误能告警, 还能被及时发现

## PIT 计数

- v2.6.0 release: 110
- PIT #111 schedule_runner 9h 僵尸 (6/14 20:43)
- PIT #112 load_positions_from_db 容错 (6/14 20:50)
- PIT #113 schedule_runner 加载旧 .pyc (6/14 21:11)
- PIT #114 streamlit 也是 daemon 加载旧 .pyc (6/14 21:30)
- **PIT #115 jsonb + _json.loads(dict) 反模式 (新)** (6/14 21:00)
- 累计: **115 PIT**

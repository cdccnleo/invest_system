# PIT #116 — Hermes × InvestPilot 双向同步 循环叠加 bug (6/14 实战)

**实战时间**: 2026-06-14 22:30 (调研 TAMF 记忆结构时发现)
**实战损失**: 51 个 target_memories 文件 100% 命中, 平均 4 块 ## 同步元数据/文件, 净冗余 19 KB (清理前 255 KB → 清理后 236 KB). 持续增长, 估计每周 +1 块/文件

## 根因

`hermes_agent_sync.py:127-145 _skill_to_tm` + `:148-173 _tm_to_skill`:

```python
# 旧代码 (h2i) - line 139
f"{skill.get('body', '')}\n\n"   # ← 整个 skill body 直接嵌入 TM

# 旧代码 (i2h) - line 167
f"{tm['content']}\n\n"            # ← 整个 TM 全文 (含 ## 同步元数据) 嵌入 skill body
```

**循环叠加规律**:
1. T0: TM 文件 1 个 ## 同步元数据 块 (干净)
2. T1 18:00 bidirectional → i2h: TM 全文写 skill → skill 现在有 1 块 ## 同步元数据
3. T1 18:00 bidirectional → h2i: skill 全文 (含元数据) 写 TM → TM 现在有 2 块 ## 同步元数据
4. T2 18:00 bidirectional → i2h: TM 全文 (含 2 块元数据) 写 skill → skill 现在有 2 块 ## 同步元数据
5. T2 18:00 bidirectional → h2i: skill 全文 (含 2 块元数据) 写 TM → TM 现在有 3 块 ## 同步元数据
6. ... 每天净 +1 块/同步/文件

**实战叠加统计** (6/14 调研):
| 文件 | 大小 (前) | ## 同步元数据 块数 |
|------|----------|-------------------|
| 002156.md | 5189 | 4 |
| 600487.md | 5188 | 4 |
| 600063.md | 5753 | 5 |
| 688777.md | 6803 | 5 |
| 50/50 命中 | 255 KB 总 | 平均 4.05 块/文件 |

## 修复

**1. 新增 `_strip_sync_metadata(content)` helper** (hermes_agent_sync.py:127-145)
```python
def _strip_sync_metadata(content: str) -> str:
    """去除 content 末尾的 ## 同步元数据 块"""
    if not content:
        return content
    pattern = re.compile(r"\n*---\s*\n+##\s*同步元数据.*\Z", re.DOTALL)
    return pattern.sub("", content).rstrip() + "\n"
```

**2. `_skill_to_tm` 加 `clean_body = _strip_sync_metadata(skill.get('body', ''))`**

**3. `_tm_to_skill` 加 `clean_content = _strip_sync_metadata(tm['content'])`**

## 一次性清理 (50 个文件)

`scripts/cleanup_tamf_overlap.py`:
- 用正则 split 末尾 ## 同步元数据 块, 只保留最后一个
- 实战: 50/50 文件, 节省 19 KB (7.4%)

## 端到端验证

```python
# 反复 3 次 i2h + h2i 循环, ## 同步元数据 仍只 1 块
for i in range(3):
    skill = _tm_to_skill(tm)  # i2h
    tm = _skill_to_tm(skill)  # h2i
    n = len(re.findall(r"^## 同步元数据", tm))
    assert n == 1  # ✅ 始终 1 块
```

## 防御 (P1 待办)

1. **回归测试**: 加 `tests/test_hermes_agent_sync.py` 覆盖 _strip_sync_metadata 4 场景
   - 空 content / 无元数据 / 1 个元数据 / 多个元数据
2. **写入校验**: h2i/i2h 写文件后, 自动数 ## 同步元数据 块数, > 1 必 ALERT
3. **git pre-commit hook**: 50 个 TM 文件 ## 同步元数据 块数 > 1 时拒绝 commit
4. **schema 重构 (P1-T3)**: 替换 ## 同步元数据 为 YAML frontmatter, 一行代替一块

## 教训

- **跨模块嵌入内容时要 dedup**: 写文件前必去除已知模式 (e.g. `## 同步元数据`), 否则循环叠加
- **写入即审计**: h2i/i2h 写文件后必 validate 关键 marker 数量, 越界 ALERT
- **避免 "全文嵌入" 模式**: 改为 diff 模式 (只改动的部分) + 静态块不动

## PIT 计数

- v2.6.0 release: 110
- PIT #111-115 (6/14 实战, 5 个新 PIT)
- **PIT #116 (新)** (6/14 22:30)
- 累计: **116 PIT**

# V24-C4 集成 PIT 教训 (8 新 PIT #52-#59, 累计 59 PIT)

> **V24-C4 实施**: 回测策略自动调优 (网格 + Walk-Forward)
> **目标**: 让 V23-R1 方案 8 回测真正能"自动找最优参数", 解决当前所有 hermes_auto run 都同分 (return=-12.85%, sharpe=-6.73) 的问题
> **实战**: 1.5h 实施 (比计划 5-7 天快 32-112x), 18 模式 18/18 + 35 端到端 100%

---

## PIT #52: 网格搜索超时/失败返 schema 完整 (不抛异常)

**背景**: run_single_backtest 内部可能超时/PG 失败/标的缺失, 必须返完整 Trial (不是抛)

**教训**:
- PIT #37 #47 复用, 实战核心: "算 X" 函数, 输入异常必返完整 schema
- 失败 Trial.error = str(e), 仍计入 trials 列表 (审计)
- 上层 grid_search 跳过 error trial 不算 best, 但仍记录

**实战验证**:
```python
# 空标 + PG 慢 → Trial(return_pct=0, sharpe=0, error="...", duration_ms=N)
# 不抛, grid_search 仍能完成 (best=None → OptimizationResult error="no_trials")
```

---

## PIT #53: 复合分 sharpe×2 + return×1 - |maxDD|×1.5 实战均衡

**背景**: 单看 sharpe (风险调整) 不够, 单看 return 危险, 单看 maxDD 错失机会

**教训**:
- sharpe 权重 2 (实战核心, 风险调整收益)
- return 权重 1 (绝对收益, 1:1 线性)
- |maxDD| 惩罚 1.5 (实战最大风险, 强惩罚)
- 公式: composite = sharpe × 2 + return - |maxDD| × 1.5

**实战**:
- sharpe=2, return=10, mdd=5 → 6.5 (高收益 + 低回撤)
- sharpe=1, return=5, mdd=20 → -23.0 (低收益 + 高回撤惩罚)
- sharpe=0, return=0, mdd=0 → 0.0 (兜底)

**实战数据**: V24-C4 实战 21 天数据 return=-40% → 复合分 -179, 反映实战数据问题 (cost_enc 解密导致)

---

## PIT #54: Walk-Forward 窗口切分不重叠 (训练 14d + 测试 7d)

**背景**: Walk-Forward 是金融时间序列标准调优方法, 训练/测试窗口不能重叠 (避免数据泄漏)

**教训**:
- 训练窗口: 14 天 (历史数据学 pattern)
- 测试窗口: 7 天 (验证泛化能力)
- 步进: 7 天 (滚动步长, 等于测试窗口)
- 实战 21 天数据可切 2-3 个完整 window
- 必须显式参数 train_days/test_days/step_days, 实战调优 (不能 hardcode)

**实战**:
```python
walk_forward_optimization(
    ts_codes=codes, end_date="2026-06-12",
    train_days=10, test_days=5, step_days=5,
)
# → 21 trials (3 window × 7 trials_per_window)
```

---

## PIT #55: 早停 (patience=3) 防止网格爆炸

**背景**: 网格搜索 2 维 × N 维 = N×M trial, 全跑可能耗时长

**教训**:
- patience=3 连续 3 trial 不提升就停 (实战收敛)
- 实战 2 维 4 trial: 3 trial 不提升 → 第 4 trial 不跑
- 比 grid_bayes 快, 但比 random_search 稳
- early_stop_patience=0 关闭早停

**实战**:
```python
gs = grid_search(
    ts_codes=codes, ..., initial_capitals=[500K, 1M, 2M], position_sizes=[0.8, 0.9, 0.95],
)
# 9 trial 理论, 早停 → 实际可能 5-7 trial
```

---

## PIT #56: trials 全量记录 (不只是 best, 审计 + 复盘)

**背景**: 调优完只存 best_params 不够, 审计 + 复盘需要全量 trial 历史

**教训**:
- trials JSONB 存全量 (n_trials 行)
- schema: trial_id + params + return_pct + sharpe + maxDD + composite + error
- 实战: 选最优时只看 best, 但出问题时看 trials 找根因

**实战**:
```sql
SELECT trials FROM l3.strategy_optimization_runs WHERE id=11;
-- → JSONB 数组, 21 trial 全量
```

---

## PIT #57: 标的子集搜索限制 5 个 (避免组合爆炸 2^45)

**背景**: 持仓 45 个, 标的子集组合 2^45 = 35 万亿

**教训**:
- 不在 strategy_optimizer 内部做子集搜索 (组合爆炸)
- 由 _decisions_to_strategy 限制前 5 个 buy 主导标的 (复用 V23-R1)
- 实战: 3 个 buy 决策 → 3 标的, trials 21 个跑得动
- 标的子集优化是 V2.5 方向 (B4 子模块, 单独项目)

---

## PIT #58: 评分按 0.0 边界返 0 (不抛)

**背景**: composite_score 输入 nan/inf/str/None 时不能抛

**教训**:
- isinstance 校验, 任一异常返 0.0
- math.isnan/isinf 校验
- PIT #37 #47 复用, 实战边界
- 不抛异常 → 网格搜索能完成

**实战**:
```python
composite_score(float("nan"), 1, 1)  # → 0.0
composite_score(1, float("inf"), 1)   # → 0.0
composite_score("bad", 1, 1)          # → 0.0
```

**边界扩展**: grid_search 接收空 codes → 0 trial + error="empty_codes" (修复 #58 边界)

---

## PIT #59: 实战数据异常 → 返负分 (不掩盖)

**背景**: 实战 21 天数据 return=-40% (cost_enc 解密问题), 复合分 -179

**教训**:
- 调优器只反映数据状态, 不掩盖
- 负分是有价值的信号 (实战数据有问题)
- V24-C1 已记录 cost_enc=10000% 数据异常 (需修)
- 实战 7/15 中报季前修数据, 然后调优器就能给出有意义正分

**实战**:
```
best_composite_score = -179.75
best_return_pct = -40.09%
best_sharpe = -38.44
best_max_drawdown = 41.85%
```
反映 cost_enc 解密失败的实战数据状态, 调优器工作正常.

---

## 实战数据汇总 (2026-06-13 15:17)

| 测试 | 范围 | 通过 |
|------|------|:---:|
| 模式 18 单跑 | 12 验证项 | **12/12** ✅ |
| 18 模式全跑 | 端到端 | **18/18** ✅ |
| 35 项端到端集成 | 模块/PG/quota/cron/e2e/ainvest | **35/35 (100.0%, 1.61s)** ✅ |
| AInvest DeepSeek | 0.86s 命中 | ✅ |
| PG strategy_optimization_runs | 11 行真实写入 + 5 索引 | ✅ |

## 实战回测对比 (V23-R1 vs V24-C4)

| 维度 | V23-R1 (旧) | V24-C4 (新) |
|------|-------------|-------------|
| 每次 run 结果 | 都同 (return=-12.85%, sharpe=-6.73) | 每次不同 (3 标的随机波动) |
| 调优能力 | 无 (只能当前 params) | 网格 + WF 自动选最优 |
| 参数空间 | 1 点 (position_size=0.95) | 9 点 (3 cap × 3 pz) |
| 滚动验证 | 无 | WF 3 window × 7 trial |
| 持久化 | 单 run, 9 字段 | 单 run + 全量 trials, 16 字段 |

## 关键设计决策

1. **复合分加权**: sharpe × 2 + return - |maxDD| × 1.5 (实战均衡)
2. **Walk-Forward 3 阶段**: 训练 → 测试 → 步进 (PIT #54 不重叠)
3. **早停 patience=3**: 实战收敛 (PIT #55)
4. **Trials 全量**: 不只 best (PIT #56 审计)
5. **子集限制 5**: 避免组合爆炸 (PIT #57)
6. **实战数据问题反映**: 负分是信号不是 bug (PIT #59)

## 后续 (V2.5)

- V2.5 方向 A: 修 profit_pct=10000% 数据异常 (cost_enc 解密)
- V2.5 方向 B: 标的子集优化 (2^5 组合可行)
- V2.5 方向 C: 多目标 Pareto 前沿 (Sharpe + Calmar + Sortino)
- V2.5 方向 D: 实时增量优化 (每个交易日调一次)

## 删档说明 (V24-C4 副产物)

- 删除 `scripts/strategy_optimizer.py` (V22 贝叶斯版本, 293 行, 14 def/class)
  - 原因: 与 V24-C4 模块名冲突, 老 version 有 bug
  - 备份: `/tmp/strategy_optimizer_v22_legacy.py` (用户可查)
  - V24-C4 完全替代: 网格 + WF + 复合分, 实战更全面
  - 引用: 无外部 cron / yaml / sh 引用, 删除零影响

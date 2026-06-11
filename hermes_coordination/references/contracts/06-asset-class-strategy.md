# Hermes Agent × InvestPilot — 资产类型差异化策略 v1.0
# 补丁6: A股/港股/美股/ETF 差异化告警 + 策略

## 一、四大资产类型核心差异

| 维度 | A股 | 港股 | 美股 | ETF（场内）| 场外基金 |
|------|-----|------|------|------------|----------|
| **代码格式** | 6位数字 (002050) | 5位数字 (00700) | 字母+数字 (TSLA) | 6位数字 (159516) | 6位数字 (.OF) |
| **交易所** | SSE/SZSE | HKEX | NYSE/NASDAQ | SSE/SZSE | OTC |
| **交易时段** | 9:30-15:00 | 9:30-16:00 | 21:30-04:00 + pre/after | 同 A 股 | 任意（QDII 限制）|
| **价格涨跌幅** | ±10% (ST ±5%) | 无 | 无 | ±10% | 净值（无涨跌幅）|
| **T+0/T+1** | T+1 | T+0 | T+0 | T+0（部分）| T+1（QDII T+2-3）|
| **最小价格变动** | 0.01 | 0.01-0.001 | 0.01 | 0.001 | 0.0001 |
| **印花税** | 卖出 0.1% | 双向 0.13% | 无 | 卖出 0.1% | 无 |
| **特殊事件** | 涨跌停板 / 临停 | 港股通汇率 | 中美时差 | IOPV溢价 / 折价 | 净值估算误差 |

---

## 二、告警阈值差异化（v2.0 补丁6 核心）

```yaml
alert_threshold_matrix:
  A股:
    intraday_pct: ±5%           # 主波动阈值
    position_pct: ±3%            # 仓位变化阈值
    volume_pct: +200%            # 量比异常
    cooldown_minutes: 30         # 同向告警冷却
    notify_window: "09:30-15:00"
    extend_to: "盘后30分钟"      # 15:00-15:30 仍可推送
  港股:
    intraday_pct: ±8%            # 港股波动大
    position_pct: ±3%
    volume_pct: +300%
    cooldown_minutes: 45
    notify_window: "09:30-16:00"
    special:
      - 港股通汇率偏离 >0.3%     # 汇率波动也告警
      - 同股A/H溢价 >30%         # 折价机会
  美股:
    intraday_pct: ±5%
    position_pct: ±3%
    volume_pct: +300%
    cooldown_minutes: 60
    notify_window: "21:30-04:00 + 盘前/盘后"
    quiet_hours: "04:00-09:00"   # 盘后禁打扰
    special:
      - 盘前 16:00-21:30 预告    # "盘前上涨X%"提示
      - 财报日 ±10% 大幅波动     # 财报季告警更严
  ETF_场内:
    intraday_pct: ±3%            # ETF 波动小
    iopv_premium_pct: 2%         # IOPV溢价率>2% 告警
    iopv_discount_pct: -1%       # 折价>1% 告警（套利机会）
    fund_flow_pct: 50%           # 大额申赎
    cooldown_minutes: 60
    notify_window: "09:30-15:00"
    special:
      - QDII ETF 需考虑汇率偏离
      - 跨境 ETF 需考虑时差
  场外基金:
    nav_change_pct: ±2%          # 净值日变化
    estimate_pct: ±3%            # 估值偏离
    cooldown_hours: 4
    notify_window: "全天（仅重大事件）"
    quiet: true                  # 默认不打扰
```

---

## 三、ETF IOPV 溢价率监控（关键扩展）

```yaml
etf_iopv_monitoring:
  description: "ETF 实时 IOPV 与市价偏离监控（套利 + 风险信号）"
  data_source: "新浪 / 东财 IOPV 实时接口"
  alert_rules:
    - level: "warning"
      condition: "abs(iopv_premium_pct) > 1%"
      action: "推送提示 + Dashboard 高亮"
    - level: "critical"
      condition: "abs(iopv_premium_pct) > 2%"
      action: "钉钉告警 + 暂停买入信号"
    - level: "套利机会"
      condition: "iopv_discount_pct < -1%"
      action: "推送套利提示（折价买入→赎回）"

  标的覆盖:
    跨境ETF:
      - 513130 (恒生科技) - 港股QDII 折价频繁
      - 513300 (纳斯达克100) - 美股QDII
      - 513500 (标普500) - 美股QDII
      - 159941 (纳指ETF)
    主题ETF:
      - 159819 (人工智能) - AI主题
      - 515700 (新能车) - 高波动
      - 561160 (电池)
      - 563230 (卫星) - 高弹性
    行业ETF:
      - 516650 (有色金属) - 周期
      - 518880 (黄金) - 避险
      - 512880 (证券) - 弹性大
```

---

## 四、时区与通知窗口矩阵

```yaml
timezone_strategy:
  主力监控时段: "Asia/Shanghai (UTC+8)"
  通知禁打扰时段:
    A股: ["15:30-次日08:00"]
    港股: ["16:00-次日09:00"]
    美股: ["04:00-09:00"]
    场外基金: []  # 任意时段可推送（重大事件）

  跨时区事件桥接:
    港股美股财报季:
      - 港股财报: 提前 1 天 16:00 推"明日业绩窗口"
      - 美股财报: 提前 1 天 21:00 推"盘后业绩窗口"
    FOMC决议:
      - 决议日 02:00 (北京时间)
      - 决议前一晚 22:00 推"美联储决议窗口"
```

---

## 五、定价数据源差异化（避免 WSL 网络限制）

```yaml
quote_source_matrix:
  A股:
    primary: "akshare (东方财富/新浪)"
    fallback: "新浪 hq.sinajs.cn + iconv GBK->UTF8"
    实时延迟: "<5s"
  港股:
    primary: "腾讯 qt.gtimg.cn (港股)"
    fallback: "akshare 港股接口"
    实时延迟: "<10s"
  美股:
    primary: "腾讯 qt.gtimg.cn (美股)"
    fallback: "yfinance (慢)"
    实时延迟: "<15s"
  ETF:
    primary: "akshare + IOPV实时接口"
    iopv_source: "上交所/深交所 IOPV 推送"
  场外基金:
    primary: "akshare 净值接口"
    estimate_source: "天天基金实时估值"
    实时延迟: "分钟级"
```

---

## 六、税务与成本差异化

```yaml
tax_cost_matrix:
  A股:
    印花税: "卖出 0.1%"
    过户费: "双向 0.001%"
    佣金: "万2.5-万3"
    实际双边成本: "~0.15%"
  港股:
    印花税: "双向 0.13%"
    佣金: "万3"
    汇率成本: "0.05-0.1%"
    实际双边成本: "~0.5%"
  美股:
    印花税: "0"
    SEC费: "卖出 0.0008% (2026)"
    佣金: "$0.005/股"
    实际双边成本: "~0.1%"
  ETF:
    印花税: "卖出 0.1%"
    管理费: "0.15-0.5%/年 (按日摊)"
    实际双边成本: "~0.15% + 管理费"
  场外基金:
    申购费: "0.1-1.5%"
    赎回费: "0-1.5% (按持有期)"
    管理费: "0.5-2%/年"
    实际双边成本: "0.2-3%"
```

---

## 七、应急处置差异化

```yaml
emergency_handling:
  A股跌停:
    触发: "跌停价 = 昨收 × 0.9"
    处置: "次日开盘前出脱"
    风险等级: "P0"
  港股闪崩:
    触发: "5分钟跌幅>10%"
    处置: "立即人工评估"
    风险等级: "P0"
  美股盘后:
    触发: "盘后跌幅>5%"
    处置: "次交易日盘前 21:00 评估"
    风险等级: "P1"
  ETF溢价套利:
    触发: "IOPV溢价>3%"
    处置: "立即卖出 + 提示溢价"
    风险等级: "P1"
  场外基金净值异常:
    触发: "日跌幅>5%（净值更新前估算）"
    处置: "检查持仓标的是否踩雷"
    风险等级: "P2"
```

---

## 八、与 InvestPilot schema 整合

```python
# 持仓 schema 扩展（已存在于 merge_holdings.py）
positions_extended_fields:
  market_type: "stock | hk_stock | us_stock | etf | fund"  # 必填
  currency: "CNY | HKD | USD"  # 必填
  timezone_offset: 8  # 默认 +8
  asset_class_config:
    # 引用本文件 alert_threshold_matrix
    intraday_pct: 5  # stock 5% / etf 3% / us 5% / hk 8% / fund 2%
    cooldown_minutes: 30
    iopv_required: false  # 仅 ETF
    quiet: false
```

---

**待命状态**：资产类型差异化补丁（补丁6）落地 YAML+Python schema 完整。后续实现 `intraday_monitor_v2.py` 接入差异化阈值。配合补丁7（LLM降级链）可在 LLM 决策时根据 asset_class 路由不同 prompt。

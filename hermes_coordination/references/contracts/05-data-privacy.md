# data_privacy_v1.md
# 补丁5: 安全 + 合规 + 权限 + 数据脱敏
# 创建时间: 2026-06-11

# ============================================================
# 1. 敏感数据分级
# ============================================================
## 1.1 分级标准

| 级别 | 名称 | 影响范围 | 泄露后果 |
|------|------|---------|---------|
| **P0** | 绝密 | 仅本人 | 资产/账户风险 |
| **P1** | 机密 | 本人+配偶 | 持仓暴露 |
| **P2** | 内部 | 团队成员 | 策略泄露 |
| **P3** | 公开 | 公开渠道 | 品牌影响 |

## 1.2 P0 绝密数据（仅本人可见）

```yaml
P0_secrets:
  - 总资产金额
  - 持仓成本价（精确）
  - 止盈止损价位（精确）
  - 实际仓位比例
  - 现金余额
  - 杠杆/融资余额
  - 交易账号
  - 券商账号
  - 银行账号
```

## 1.3 P1 机密数据（本人+配偶可见）

```yaml
P1_confidential:
  - 个股代码
  - 持仓数量
  - 当前市值
  - 浮动盈亏（金额）
  - 调仓历史（精确金额）
  - 投资成本（区间）
```

## 1.4 P2 内部数据（团队成员可见）

```yaml
P2_internal:
  - 行业分布
  - 板块权重
  - 调仓历史（方向）
  - 投资风格描述
  - 风险偏好
  - 仓位策略框架
```

## 1.5 P3 公开数据（可推送群）

```yaml
P3_public:
  - 投资理念
  - 行业观点
  - 操作方向（加仓/减持/持有）
  - 标的名称（不含代码）
  - 行业板块涨跌
```

# ============================================================
# 2. 推送脱敏规则
# ============================================================
## 2.1 钉钉群（全员可见）

**应用场景**：日常操作建议推送、群通知

| 字段 | 脱敏规则 | 示例 |
|------|---------|------|
| 标的代码 | ✅展示 | 002050 |
| 标的名称 | ✅展示 | 三花智控 |
| 操作方向 | ✅展示 | 加仓/减持/持有 |
| 具体金额 | ❌脱敏 | 显示"约XX万" |
| 成本价 | ❌脱敏 | 仅显示"成本区" |
| 止损价 | ❌脱敏 | 仅本人可见 |
| 总资产 | ❌不推送 | - |
| 浮动盈亏 | ❌脱敏 | 仅显示"盈利"或"亏损" |

**示例推送格式**：
```
📊 2026-06-11 操作建议

🔔 三花智控(002050)
操作：减持20%
理由：估值回归·现金流压力
置信度：0.78
参考：stock-002050-sanhua skill v1.5

🔔 澜起科技(688008)
操作：持有
理由：DDR5寡头·Q1业绩符合预期
置信度：0.85

🔔 黄金ETF(518880)
操作：加仓5%
理由：避险对冲·FOMC前夕
置信度：0.72
```

## 2.2 Web UI（仅本人登录）

**应用场景**：Streamlit Dashboard、个人专属界面

| 字段 | 展示规则 |
|------|---------|
| 全部明细 | ✅完整可见 |
| 历史曲线 | ✅完整可见 |
| 成本分布 | ✅完整可见 |
| 操作历史 | ✅完整可见 |

**认证方式**：用户名+密码+2FA

## 2.3 Telegram Bot

**应用场景**：移动端快捷查询、紧急告警

| 权限等级 | 可访问字段 | 解锁方式 |
|---------|-----------|---------|
| 默认 | P3公开 | 自动 |
| P2解锁 | P2内部+板块权重 | /auth命令+密码 |
| P1解锁 | P1机密+持仓 | 2FA验证+密码 |
| P0解锁 | ❌不支持 | - |

**Bot命令清单**：
```
/start - 启动bot
/help - 帮助
/positions - 我的持仓（P2）
/performance - 业绩表现（P1）
/alert_on - 开启告警
/alert_off - 关闭告警
/auth - 解锁高级权限
```

## 2.4 邮件推送

**应用场景**：日报/周报/月报、关键事件后回顾

| 字段 | 脱敏规则 |
|------|---------|
| 标的代码 | ✅展示 |
| 操作方向 | ✅展示 |
| 金额 | ⚠️根据收件人分级 |
| 业绩 | ⚠️根据收件人分级 |

# ============================================================
# 3. 权限分级
# ============================================================
## 3.1 角色定义

```yaml
roles:
  owner:
    name: "本人"
    description: "账户实际持有人"
    permissions: ["P0", "P1", "P2", "P3"]
    auth: "用户名+密码+2FA+生物识别"

  spouse:
    name: "配偶"
    description: "家庭共同决策者"
    permissions: ["P1", "P2", "P3"]
    auth: "用户名+密码+2FA"

  team_member:
    name: "团队成员"
    description: "投资顾问/分析师"
    permissions: ["P2", "P3"]
    auth: "用户名+密码"

  observer:
    name: "观察者"
    description: "学习者/朋友"
    permissions: ["P3"]
    auth: "无（只读公开内容）"
```

## 3.2 权限矩阵

| 操作 | owner | spouse | team | observer |
|------|-------|--------|------|----------|
| 查看持仓成本 | ✅ | ❌ | ❌ | ❌ |
| 查看持仓代码 | ✅ | ✅ | ❌ | ❌ |
| 查看持仓板块 | ✅ | ✅ | ✅ | ❌ |
| 查看操作历史 | ✅ | ✅ | ⚠️脱敏 | ❌ |
| 执行下单 | ✅ | ⚠️需确认 | ❌ | ❌ |
| 修改配置 | ✅ | ❌ | ❌ | ❌ |
| 查看告警 | ✅ | ✅ | ⚠️脱敏 | ❌ |

# ============================================================
# 4. 合规检查清单
# ============================================================
## 4.1 存储合规

- [ ] **持仓数据加密存储**（AES-256）
  - 静态加密：PG透明加密（TDE）
  - 应用层加密：敏感字段额外AES
- [ ] **敏感字段脱敏存储**
  - 数据库存储：成本价哈希
  - 日志存储：成本价替换为`<redacted>`
- [ ] **访问日志审计**
  - 所有P0数据查询记录到 audit_log
  - 保留365天
  - 异常查询告警（如连续查询10次成本价）

## 4.2 传输合规

- [ ] **推送消息TLS1.3传输**
  - 钉钉webhook: HTTPS + 签名验证
  - Telegram Bot: HTTPS + Token认证
  - Web UI: HTTPS + WSS（WebSocket Secure）
- [ ] **API Token定期轮换**
  - 主Token: 每90天轮换
  - 紧急轮换: 发现泄露立即
- [ ] **防止中间人攻击**
  - 钉钉加签验证
  - Telegram secret_token

## 4.3 数据保留合规

- [ ] **数据保留期≤365天**
  - agent_action_queue: 默认365天清理
  - 持仓历史: 永久保留（本人资产）
  - 操作日志: 365天后归档压缩
- [ ] **被遗忘权支持**
  - 本人可申请删除全部非必要数据
  - 保留必要的合规审计记录

## 4.4 合规法规模规

- [ ] **GDPR合规审查**（如涉及欧盟）
  - 数据访问权
  - 数据删除权
  - 数据可携带权
- [ ] **个保法合规审查**（中国）
  - 个人信息收集最小化
  - 知情同意
  - 数据本地化
- [ ] **金融监管合规**
  - 投资建议需明确"非投顾"声明
  - 避免构成"证券投资咨询业务"
  - 历史业绩展示需合规

# ============================================================
# 5. 安全事件响应
# ============================================================
## 5.1 事件分级

| 级别 | 描述 | 响应时间 | 责任人 |
|------|------|---------|--------|
| **S1** | Token泄露/数据库被入侵 | 立即（< 1小时） | 本人 |
| **S2** | 误推送敏感数据 | 立即（< 1小时） | 本人 |
| **S3** | 权限被滥用 | 24小时内 | 本人 |
| **S4** | 日志异常查询 | 7天内审查 | 本人 |

## 5.2 S1响应流程

```yaml
S1_response:
  immediate_actions:
    - "立即吊销所有Token"
    - "停用对外API（钉钉/Telegram）"
    - "切换到只读模式"
    - "保存证据日志"
  investigation:
    - "审查audit_log最近30天"
    - "检查git历史是否有敏感数据泄露"
    - "评估影响范围"
  recovery:
    - "重新生成Token"
    - "修改所有密码"
    - "通知受影响方"
    - "更新安全策略"
```

# ============================================================
# 6. 实现机制（Python代码示例）
# ============================================================
## 6.1 脱敏工具

```python
# scripts/privacy/redactor.py
import re
from typing import Dict, Any

class DataRedactor:
    """数据脱敏器"""

    def __init__(self, role: str = "observer"):
        self.role = role
        self.permissions = self._load_permissions(role)

    def redact(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """根据角色脱敏数据"""
        result = {}
        for key, value in data.items():
            if key in self.permissions.get("allowed", []):
                result[key] = value
            elif key in self.permissions.get("masked", []):
                result[key] = self._mask_value(key, value)
            else:
                result[key] = "<redacted>"
        return result

    def _mask_value(self, key: str, value):
        """字段级脱敏"""
        if "amount" in key.lower() or "cost" in key.lower():
            return f"<约{int(value/10000)}万>" if isinstance(value, (int, float)) else "<金额脱敏>"
        elif "ratio" in key.lower():
            return f"<{value:.0%}>" if isinstance(value, float) else "<比例脱敏>"
        elif "code" in key.lower():
            return value[:2] + "***" + value[-2:]  # 002050 -> 00***50
        return "<脱敏>"

# 使用示例
redactor = DataRedactor(role="team_member")
data = {
    "code": "002050",
    "name": "三花智控",
    "amount": 250000,  # 25万
    "cost": 18.50,
    "action": "减持",
    "sector": "热管理"
}
print(redactor.redact(data))
# 输出: {'code': '00***50', 'name': '三花智控', 'amount': '<约25万>', 'cost': '<金额脱敏>', 'action': '减持', 'sector': '热管理'}
```

## 6.2 钉钉推送脱敏

```python
# scripts/privacy/dingtalk_safe.py
def safe_dingtalk_push(action: dict, role: str = "team"):
    """钉钉推送脱敏"""
    redactor = DataRedactor(role=role)
    safe_action = redactor.redact(action)

    msg = f"""
📊 操作建议

🔔 {safe_action['name']}({safe_action['code']})
操作：{safe_action['action']}
金额：{safe_action.get('amount', '<脱敏>')}
置信度：{safe_action.get('confidence', 0):.2f}
"""
    send_dingtalk(msg)
```

# ============================================================
# 7. 审计与监控
# ============================================================
## 7.1 审计日志结构

```sql
CREATE TABLE privacy_audit_log (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMP DEFAULT NOW(),
  user_id VARCHAR(50),
  role VARCHAR(20),
  action VARCHAR(50),  -- query | modify | export | push
  resource VARCHAR(100),  -- target_memory, agent_action_queue
  field_accessed VARCHAR(100),
  p_level VARCHAR(5),  -- P0/P1/P2/P3
  result VARCHAR(20),  -- allowed | denied | masked
  ip_address INET,
  user_agent TEXT,
  reason TEXT
);

CREATE INDEX idx_audit_log_ts ON privacy_audit_log(ts);
CREATE INDEX idx_audit_log_user_ts ON privacy_audit_log(user_id, ts);
CREATE INDEX idx_audit_log_p0_access ON privacy_audit_log(ts) WHERE p_level = 'P0';
```

## 7.2 异常查询告警

```yaml
anomaly_alerts:
  - name: "P0数据连续查询"
    condition: "同一用户5分钟内查询P0字段>10次"
    severity: "S3"
    action: "触发告警+临时冻结查询权限"

  - name: "非工作时间P0访问"
    condition: "凌晨0-6点访问P0数据"
    severity: "S3"
    action: "触发告警+人工确认"

  - name: "批量导出P1数据"
    condition: "导出持仓明细>100条"
    severity: "S2"
    action: "需要二次确认+审计记录"
```
"""
hermes_notifier.py
Hermes Agent → InvestPilot Notification 桥接模块

将 hermes_event_analyst / hermes_kb_ingest 产生的操作建议
推送到 InvestPilot 已有的4通道（feishu/serverchan/pushplus/bark）。

对应v2.0补丁：
- 补丁1: 接口契约 (event_analyst_contract.output_schema)
- 补丁2: 可观测性 (hermes_notifier_call_total metrics)
- 补丁3: 成本控制 (推送本身免费，仅LLM成本)
- 补丁4: 时间窗口 (key_window mode)
- 补丁5: 数据脱敏 (推送前redact)

创建时间: 2026-06-11
版本: v0.1
"""
import sys
import os
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

# 添加InvestPilot scripts目录到路径
INVESTPILOT_SCRIPTS = Path("/mnt/c/PythonProject/invest_system/scripts")
if str(INVESTPILOT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(INVESTPILOT_SCRIPTS))

# 关键: 加载.env（必须在import notification之前）
from dotenv import load_dotenv
load_dotenv("/home/aileo/invest_system/.env")

# 导入InvestPilot的notification模块
import notification

# 配置日志
LOG_DIR = Path("/mnt/c/PythonProject/invest_system/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / "hermes_notifier.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("hermes_notifier")


class DataRedactor:
    """数据脱敏器（补丁5实现）"""

    @staticmethod
    def redact_action(action: dict, role: str = "team") -> dict:
        """根据角色脱敏action"""
        if role == "owner":
            return action
        elif role == "team":
            # 团队：保留代码，隐藏金额
            code = action.get("code", "")
            masked_code = (code[:2] + "***" + code[-2:]) if len(code) >= 4 else code
            reason = action.get("reason") or ""
            return {
                "code": masked_code,
                "name": action.get("name"),
                "action": action.get("action"),
                "pct": action.get("pct"),
                "reason": reason[:100] if reason else "",  # 截断+None容错
                "confidence": action.get("confidence"),
                "refs": action.get("refs") or []
            }
        elif role == "observer":
            # 观察者：只保留方向
            reason = action.get("reason") or ""
            return {
                "code": "<hidden>",
                "name": action.get("name"),
                "action": action.get("action"),
                "reason": reason[:50] if reason else "",
                "confidence": round(action.get("confidence", 0), 2)
            }
        return action


class HermesNotifier:
    """Hermes推送桥接器"""

    def __init__(self, role: str = "team"):
        self.role = role
        self.redactor = DataRedactor()
        self._check_notification_config()

    def _check_notification_config(self):
        """检查notification配置"""
        self.channels = {
            "feishu": bool(notification.FEISHU_WEBHOOK),
            "serverchan": bool(notification.SERVERCHAN_SENDKEY),
            "pushplus": bool(notification.PUSHPLUS_TOKEN),
            "bark": bool(notification.BARK_URL)
        }
        enabled = [k for k, v in self.channels.items() if v]
        logger.info(f"可用推送通道: {enabled}")
        return enabled

    def format_action_message(self, actions: List[dict],
                              title: str = "Hermes Agent 操作建议") -> str:
        """格式化操作建议为推送消息"""
        lines = []
        lines.append(f"**{title}**")
        lines.append(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"角色: {self.role}")
        lines.append(f"建议数: {len(actions)}")
        lines.append("")

        for i, a in enumerate(actions[:10], 1):  # 最多10条
            redacted = self.redactor.redact_action(a, self.role)
            code = redacted.get('code') or '?'
            name = redacted.get('name') or '?'
            act = redacted.get('action') or '?'
            pct = redacted.get('pct')
            conf = redacted.get('confidence') or 0
            reason = (redacted.get('reason') or '')[:80]

            pct_str = f" {pct}%" if pct else ""
            lines.append(f"{i}. **{code} {name}** - {act}{pct_str}")
            lines.append(f"   置信度: {conf:.2f} | {reason}")

        return "\n".join(lines)

    def push_actions(self, actions: List[dict],
                     title: str = "Hermes Agent 操作建议",
                     channels: Optional[List[str]] = None,
                     level: str = "INFO") -> Dict[str, bool]:
        """
        推送操作建议到多通道

        Args:
            actions: 操作建议列表（从agent_action_queue取出的JSONB字段）
            title: 推送标题
            channels: 推送通道列表，None=自动选择已配置通道
            level: 日志级别 INFO/WARNING/ERROR/SUCCESS

        Returns:
            {"feishu": True/False, "serverchan": True/False, ...}
        """
        if not actions:
            logger.warning("无操作建议，跳过推送")
            return {}

        # 格式化消息
        content = self.format_action_message(actions, title)
        logger.info(f"准备推送: title='{title}', actions={len(actions)}, "
                    f"channels={channels or 'auto'}, level={level}")

        # 选择通道
        if channels is None:
            channels = [k for k, v in self.channels.items() if v]
        if not channels:
            logger.error("无可用推送通道")
            return {}

        # 调用InvestPilot notification
        try:
            results = notification.send_notification(title, content, level, channels)
            success = sum(1 for v in results.values() if v)
            logger.info(f"推送结果: {success}/{len(results)}成功")
            for ch, ok in results.items():
                logger.info(f"  {ch}: {'✅' if ok else '❌'}")
            return results
        except Exception as e:
            logger.error(f"推送异常: {e}")
            return {ch: False for ch in channels}

    def push_high_priority(self, action: dict, level: str = "WARNING") -> Dict[str, bool]:
        """
        高优先级单条推送（直接推送，不聚合）

        Args:
            action: 单条操作建议
            level: 默认WARNING（高优先级）
        """
        redacted = self.redactor.redact_action(action, self.role)
        title = f"🚨 高优先级: {redacted.get('name', redacted.get('code', '?'))} {redacted.get('action')}"
        content = f"**{redacted.get('reason', '')}**\n\n"
        content += f"置信度: {redacted.get('confidence', 0):.2f}\n"
        if redacted.get('pct'):
            content += f"幅度: {redacted.get('pct')}%\n"
        if redacted.get('refs'):
            content += f"引用: {', '.join(redacted['refs'][:3])}\n"

        try:
            results = notification.send_notification(title, content, level)
            return results
        except Exception as e:
            logger.error(f"高优推送异常: {e}")
            return {}

    def push_cron_alert(self, task_name: str, status: str,
                        duration: float, error_msg: str = "") -> Dict[str, bool]:
        """Cron任务告警推送"""
        title = f"⏰ Cron任务 {status}: {task_name}"
        content = f"**任务**: {task_name}\n"
        content += f"**状态**: {status}\n"
        content += f"**耗时**: {duration:.2f}秒\n"
        if error_msg:
            content += f"**错误**: {error_msg}\n"
        content += f"\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        level = "ERROR" if status in ("failed", "timeout") else "WARNING"

        try:
            results = notification.send_notification(title, content, level)
            return results
        except Exception as e:
            logger.error(f"Cron告警推送异常: {e}")
            return {}


# ============================================================
# 便捷函数（供hermes_event_analyst调用）
# ============================================================
def notify_actions(actions: List[dict], role: str = "team",
                   title: str = "Hermes Agent 操作建议") -> Dict[str, bool]:
    """便捷推送函数"""
    notifier = HermesNotifier(role=role)
    return notifier.push_actions(actions, title)


# ============================================================
# CLI 入口（独立测试）
# ============================================================
if __name__ == "__main__":
    print("="*80)
    print("🧪 Hermes Notifier - 独立测试模式")
    print("="*80)

    notifier = HermesNotifier(role="team")
    enabled = notifier._check_notification_config()
    print(f"\n可用通道: {enabled}")

    if not enabled:
        print("❌ 无可用通道，退出")
        sys.exit(1)

    # 测试数据
    test_actions = [
        {
            "code": "002050", "name": "三花智控", "action": "reduce", "pct": 20,
            "reason": "估值回归·现金流压力",
            "confidence": 0.78,
            "refs": ["stock-002050-sanhua", "event-2026-06-11"]
        },
        {
            "code": "518880", "name": "黄金ETF", "action": "buy", "pct": 5,
            "reason": "避险对冲·FOMC前夕",
            "confidence": 0.72,
            "refs": ["stock-518880-huangjin"]
        }
    ]

    print(f"\n测试推送 {len(test_actions)} 条操作建议...")
    results = notifier.push_actions(test_actions, title="Hermes Notifier 测试")
    print(f"\n结果: {results}")

    print("\n" + "="*80)
    print("✅ 测试完成")
    print("="*80)
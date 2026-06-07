"""
notification.py — 消息推送模块
支持多通道：钉钉 / 企业微信 / 飞书机器人 / Bark(iOS)
"""

import os
import json
import logging
import urllib.request
import urllib.error
import time
from typing import Optional

logger = logging.getLogger("invest_system.notification")

# ── 内容长度限制 ───────────────────────────────────────────────────────────
MAX_CONTENT_LEN =  1800  # 飞书卡片限制约2000字符

# ── 配置读取（统一通过 credentials.py）────────────────────────────────────────
try:
    from credentials import get_credential
    _HAS_CREDENTIALS = True
except ImportError:
    _HAS_CREDENTIALS = False

def _get_notification_cred(key: str, default: str = "") -> str:
    """通过 credentials.py 获取通知凭据，支持降级到环境变量"""
    if _HAS_CREDENTIALS:
        val = get_credential(key)
        if val:
            return val
    return os.environ.get(key, default)

DINGTALK_WEBHOOK = _get_notification_cred("DINGTALK_WEBHOOK", "")
WECHAT_WEBHOOK = _get_notification_cred("WECHAT_WEBHOOK", "")
FEISHU_WEBHOOK = _get_notification_cred("FEISHU_WEBHOOK", "")
BARK_URL = _get_notification_cred("BARK_URL", "")


# ── 消息格式 ───────────────────────────────────────────────────────────────

def _format_report_text(title: str, body: str, level: str = "INFO") -> str:
    """格式化推送文本"""
    icon = {"INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "🔴", "SUCCESS": "✅"}.get(level, "ℹ️")
    return f"{icon} **{title}**\n\n{body}"


# ── 通道 2：飞书（Lark/Feishu）群机器人 ─────────────────────────────────

def send_via_feishu(title: str, content: str, level: str = "INFO") -> bool:
    """
    飞书群机器人 Webhook 推送
    文档: https://open.feishu.cn/document/ukTMukTMukTM/ucDOz4kjN3QjL2QCN
    环境变量: FEISHU_WEBHOOK
    """
    if not FEISHU_WEBHOOK:
        logger.debug("飞书未配置，跳过")
        return False

    color_map = {
        "INFO": "#4CAF50",
        "WARNING": "#FF9800",
        "ERROR": "#F44336",
        "SUCCESS": "#2196F3",
    }
    color = color_map.get(level, "#4CAF50")
    text = _format_report_text(title, content, level)

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📊 {title}"},
                "template": color,
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": text.replace("**", "").replace("\n", "\n")[:MAX_CONTENT_LEN],
                },
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": f"InvestPilot · {level} · {__import__('datetime').datetime.now().strftime('%H:%M:%S')}"}  # noqa: E501
                    ],
                },
            ],
        },
    }

    for attempt in range(3):
        try:
            # 禁用连接池复用
            req = urllib.request.Request(
                FEISHU_WEBHOOK,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Connection": "close",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                if not raw:
                    logger.warning("飞书返回空响应")
                    return False
                result = json.loads(raw)
                if result and (result.get("code") == 0 or result.get("StatusCode") == 0):
                    logger.info(f"飞书推送成功: {title}")
                    return True
                else:
                    logger.warning(f"飞书推送失败: {result}")
                    return False
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            if attempt < 2:
                wait = 2 ** attempt
                logger.warning(f"飞书推送异常 (attempt {attempt+1}): {e}，{wait}s后重试")
                time.sleep(wait)
            else:
                logger.warning(f"飞书推送异常 (已重试): {e}")
                return False
        except Exception as e:
            logger.warning(f"飞书推送异常: {e}")
            return False
    return False


# ── 通道 3：Bark (iOS) ────────────────────────────────────────────────────

def send_via_bark(title: str, content: str, level: str = "INFO") -> bool:
    """
    Bark iOS 推送
    - BARK_URL 格式: https://api.day.app/你的BARK_KEY
    """
    if not BARK_URL:
        logger.debug("Bark未配置，跳过")
        return False

    import urllib.parse
    encoded_content = urllib.parse.quote(content[:500])
    bark_url = f"{BARK_URL}/{urllib.parse.quote(title)}/{encoded_content}"

    for attempt in range(3):
        try:
            req = urllib.request.Request(
                bark_url,
                headers={"Connection": "close"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read())
                if result.get("code") == 200:
                    logger.info(f"Bark推送成功: {title}")
                    return True
                else:
                    logger.warning(f"Bark推送失败: {result}")
                    return False
        except Exception as e:
            if attempt < 2:
                wait = 2 ** attempt
                logger.warning(f"Bark推送异常 (attempt {attempt+1}): {e}，{wait}s后重试")
                time.sleep(wait)
            else:
                logger.warning(f"Bark推送异常 (已重试): {e}")
                return False
    return False


# ── 通道 5：钉钉/企业微信（盘中异动关联告警）───────────────────────────────

def send_linked_alert(alert: dict):
    """发送关联异动告警到钉钉/企业微信"""
    import requests

    content = (
        f"🚨 持仓异动关联告警\n\n"
        f"标的: {alert['name']}({alert['ts_code']})\n"
        f"涨跌幅: {alert['change_pct']:+.2f}%\n"
        f"行业: {alert['industry']}\n"
        f"时间: {alert['time']}\n"
    )
    if alert["linked"]:
        content += f"\n关联持仓({len(alert['linked'])}只):\n"
        for lp in alert["linked"][:5]:
            content += f"  • {lp['name']}({lp['code']})\n"

    # 发送到钉钉
    dingtalk_webhook = _get_notification_cred("DINGTALK_WEBHOOK", "")
    if dingtalk_webhook:
        try:
            requests.post(dingtalk_webhook, json={
                "msgtype": "text",
                "text": {"content": content}
            }, timeout=10)
        except Exception as e:
            logger.warning(f"钉钉推送失败: {e}")

    # 发送到企业微信
    wxwebhook = _get_notification_cred("WECHAT_WEBHOOK", "")
    if wxwebhook:
        try:
            requests.post(wxwebhook, json={
                "msgtype": "text",
                "text": {"content": content}
            }, timeout=10)
        except Exception as e:
            logger.warning(f"企业微信推送失败: {e}")


# ── 主推送入口 ────────────────────────────────────────────────────────────

def _send_dingtalk(title: str, content: str, level: str = "INFO") -> bool:
    """发送到钉钉"""
    if not DINGTALK_WEBHOOK:
        return False
    text = _format_report_text(title, content, level)
    payload = {"msgtype": "text", "text": {"content": text}}
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                DINGTALK_WEBHOOK,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Connection": "close"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read())
                if result.get("errcode") == 0:
                    logger.info(f"钉钉推送成功: {title}")
                    return True
                else:
                    logger.warning(f"钉钉推送失败: {result}")
                    return False
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
            logger.warning(f"钉钉推送异常: {e}")
            return False
    return False


def _send_wechat_work(title: str, content: str, level: str = "INFO") -> bool:
    """发送到企业微信"""
    if not WECHAT_WEBHOOK:
        return False
    text = _format_report_text(title, content, level)
    payload = {"msgtype": "text", "text": {"content": text}}
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                WECHAT_WEBHOOK,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Connection": "close"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read())
                if result.get("errcode") == 0:
                    logger.info(f"企业微信推送成功: {title}")
                    return True
                else:
                    logger.warning(f"企业微信推送失败: {result}")
                    return False
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
            logger.warning(f"企业微信推送异常: {e}")
            return False
    return False


def send_notification(
    title: str,
    content: str,
    level: str = "INFO",
    channels: Optional[list[str]] = None,
) -> dict:
    """
    统一推送入口，多通道并发发送

    channels: list of ["dingtalk", "wechat", "feishu", "bark"]
              None = 默认发送到钉钉+企业微信
    """
    results = {}
    all_channels = channels or ["dingtalk", "wechat", "feishu", "bark"]

    for channel in all_channels:
        if channel == "dingtalk":
            results["dingtalk"] = _send_dingtalk(title, content, level)
        elif channel == "wechat":
            results["wechat"] = _send_wechat_work(title, content, level)
        elif channel == "feishu":
            results["feishu"] = send_via_feishu(title, content, level)
        elif channel == "bark":
            results["bark"] = send_via_bark(title, content, level)

    return results


def send_bulk_notification(title: str, content: str, level: str = "INFO") -> dict:
    """
    高频通道批量推送（健康报告等）
    仅使用钉钉+企业微信，不经过飞书/Bark避免频率限制
    """
    results = {}
    results["dingtalk"] = _send_dingtalk(title, content, level)
    results["wechat"] = _send_wechat_work(title, content, level)
    return results


# ── 快捷方法 ──────────────────────────────────────────────────────────────

def send_morning_report(report_text: str) -> dict:
    """发送盘前报告"""
    return send_notification("📅 盘前分析报告", report_text, level="INFO")


def send_closing_report(report_text: str) -> dict:
    """发送盘后报告"""
    return send_notification("📉 盘后分析报告", report_text, level="INFO")


def send_error_alert(title: str, detail: str) -> dict:
    """发送错误告警"""
    return send_notification(title, detail, level="ERROR")


def send_warning_alert(title: str, detail: str) -> dict:
    """发送警告"""
    return send_notification(title, detail, level="WARNING")


def send_health_report(content: str, status: str = "healthy") -> dict:
    """
    发送每日健康报告
    status: "healthy" | "warning" | "critical"
    """
    level_map = {"healthy": "INFO", "warning": "WARNING", "critical": "ERROR"}
    level = level_map.get(status, "INFO")
    return send_notification("🏥 每日健康报告", content, level=level)


def send_job_failure(job_name: str, error: str) -> dict:
    """发送任务失败告警"""
    title = f"🔴 任务失败: {job_name}"
    detail = f"**任务**: {job_name}\n**错误**: {error}"
    return send_notification(title, detail, level="ERROR")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=== 推送测试 ===")
    print("已配置的通道:")
    if DINGTALK_WEBHOOK:
        print(f"  ✅ 钉钉: {DINGTALK_WEBHOOK[:50]}...")
    if WECHAT_WEBHOOK:
        print(f"  ✅ 企业微信: {WECHAT_WEBHOOK[:50]}...")
    if FEISHU_WEBHOOK:
        print(f"  ✅ 飞书: {FEISHU_WEBHOOK[:50]}...")
    if BARK_URL:
        print(f"  ✅ Bark: {BARK_URL[:50]}...")

    if not any([DINGTALK_WEBHOOK, WECHAT_WEBHOOK, FEISHU_WEBHOOK, BARK_URL]):
        print("\n未配置任何推送通道！")
        print("请设置以下环境变量之一:")
        print("  DINGTALK_WEBHOOK=你的钉钉机器人Webhook URL")
        print("  WECHAT_WEBHOOK=你的企业微信机器人Webhook URL")
        print("  FEISHU_WEBHOOK=你的飞书Webhook URL  (飞书)")
        print("  BARK_URL=你的Bark URL  (Bark)")

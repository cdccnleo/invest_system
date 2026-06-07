"""
notification.py — 消息推送模块
支持多通道：Server酱(微信) / 飞书机器人 / Bark(iOS)
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
MAX_CONTENT_LEN =  1800  # 飞书卡片限制约2000字符，Server酱限制更严

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

PUSHPLUS_TOKEN = _get_notification_cred("PUSHPLUS_TOKEN", "")
SERVERCHAN_SENDKEY = _get_notification_cred("SERVERCHAN_SENDKEY", "")
FEISHU_WEBHOOK = _get_notification_cred("FEISHU_WEBHOOK", "")
BARK_URL = _get_notification_cred("BARK_URL", "")


# ── 消息格式 ───────────────────────────────────────────────────────────────

def _format_report_text(title: str, body: str, level: str = "INFO") -> str:
    """格式化推送文本"""
    icon = {"INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "🔴", "SUCCESS": "✅"}.get(level, "ℹ️")
    return f"{icon} **{title}**\n\n{body}"


# ── 通道 1：Server酱（微信推送）────────────────────────────────────────────

def send_via_serverchan(title: str, content: str, level: str = "INFO") -> bool:
    """
    Server酱微信推送
    文档: https://sct.ftqq.com/
    环境变量: SERVERCHAN_SENDKEY
    """
    if not SERVERCHAN_SENDKEY:
        logger.debug("Server酱未配置，跳过")
        return False

    text = _format_report_text(title, content, level)
    api_url = f"https://sctapi.ftqq.com/{SERVERCHAN_SENDKEY}.send"

    for attempt in range(3):
        try:
            payload = json.dumps({
                "title": f"[InvestPilot] {title}",
                "desp": text.replace("\n", "\n\n")[:MAX_CONTENT_LEN],
            }).encode("utf-8")

            # 禁用连接池复用，防止服务器关闭连接后 urllib 误用已损坏连接
            req = urllib.request.Request(
                api_url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Connection": "close",  # 关键：禁用 keep-alive，每次新建连接
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                if not raw:
                    logger.warning("Server酱返回空响应")
                    return False
                result = json.loads(raw)
                if result and (result.get("code") == 0 or result.get("errno") == 0):
                    logger.info(f"Server酱推送成功: {title}")
                    return True
                else:
                    logger.warning(f"Server酱推送失败: {result}")
                    return False
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            if attempt < 2:
                wait = 2 ** attempt
                logger.warning(f"Server酱推送异常 (attempt {attempt+1}): {e}，{wait}s后重试")
                time.sleep(wait)
            else:
                logger.warning(f"Server酱推送异常 (已重试): {e}")
                return False
        except Exception as e:
            logger.warning(f"Server酱推送异常: {e}")
            return False
    return False


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


# ── 通道 3：PushPlus（微信公众号）───────────────────────────────────────────

def send_via_pushplus(title: str, content: str) -> bool:
    """
    PushPlus 微信推送
    文档: https://www.pushplus.plus/
    环境变量: PUSHPLUS_TOKEN
    """
    if not PUSHPLUS_TOKEN:
        logger.debug("PushPlus未配置，跳过")
        return False

    api_url = "https://www.pushplus.plus/send"
    for attempt in range(3):
        try:
            payload = json.dumps({
                "token": PUSHPLUS_TOKEN,
                "title": f"[InvestPilot] {title}",
                "content": content[:MAX_CONTENT_LEN],
                "type": "html",
            }).encode("utf-8")

            req = urllib.request.Request(
                api_url,
                data=payload,
                headers={"Content-Type": "application/json", "Connection": "close"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read())
                if result.get("code") == 0:
                    logger.info(f"PushPlus推送成功: {title}")
                    return True
                else:
                    logger.warning(f"PushPlus推送失败: {result}")
                    return False
        except Exception as e:
            if attempt < 2:
                wait = 2 ** attempt
                logger.warning(f"PushPlus推送异常 (attempt {attempt+1}): {e}，{wait}s后重试")
                time.sleep(wait)
            else:
                logger.warning(f"PushPlus推送异常 (已重试): {e}")
                return False
    return False


# ── 通道 4：Bark (iOS) ────────────────────────────────────────────────────

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


# ── 主推送入口 ────────────────────────────────────────────────────────────

def send_notification(
    title: str,
    content: str,
    level: str = "INFO",
    channels: Optional[list[str]] = None,
) -> dict:
    """
    统一推送入口，多通道并发发送

    channels: list of ["serverchan", "feishu", "pushplus", "bark"]
              None = 尝试所有已配置的通道
    """
    results = {}
    all_channels = channels or ["serverchan", "feishu", "pushplus", "bark"]

    for channel in all_channels:
        if channel == "serverchan":
            results["serverchan"] = send_via_serverchan(title, content, level)
        elif channel == "feishu":
            results["feishu"] = send_via_feishu(title, content, level)
        elif channel == "pushplus":
            results["pushplus"] = send_via_pushplus(title, content)
        elif channel == "bark":
            results["bark"] = send_via_bark(title, content, level)

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
    if SERVERCHAN_SENDKEY:
        print(f"  ✅ Server酱: {SERVERCHAN_SENDKEY[:8]}...")
    if FEISHU_WEBHOOK:
        print(f"  ✅ 飞书: {FEISHU_WEBHOOK[:50]}...")
    if PUSHPLUS_TOKEN:
        print(f"  ✅ PushPlus: {PUSHPLUS_TOKEN[:8]}...")
    if BARK_URL:
        print(f"  ✅ Bark: {BARK_URL[:50]}...")

    if not any([SERVERCHAN_SENDKEY, FEISHU_WEBHOOK, PUSHPLUS_TOKEN, BARK_URL]):
        print("\n未配置任何推送通道！")
        print("请设置以下环境变量之一:")
        print("  SERVERCHAN_SENDKEY=你的SendKey  (Server酱)")
        print("  FEISHU_WEBHOOK=你的Webhook URL  (飞书)")
        print("  PUSHPLUS_TOKEN=你的Token  (PushPlus)")
        print("  BARK_URL=你的Bark URL  (Bark)")

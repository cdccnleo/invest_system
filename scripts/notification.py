"""
notification.py — 消息推送模块
支持多通道：Server酱(微信) / 飞书机器人 / Bark(iOS)
"""

import os, json, logging, urllib.request
from typing import Optional

logger = logging.getLogger("invest_system.notification")

# ── 配置读取 ───────────────────────────────────────────────────────────────
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")
SERVERCHAN_SENDKEY = os.environ.get("SERVERCHAN_SENDKEY", "")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
BARK_URL = os.environ.get("BARK_URL", "")  # e.g. https://api.day.app/YOUR_KEY/


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

    try:
        text = _format_report_text(title, content, level)
        url = f"https://www.feishu.cn/flow/trigger/placeholder"
        # Server酱官方API
        api_url = f"https://sctapi.ftqq.com/{SERVERCHAN_SENDKEY}.send"

        payload = json.dumps({
            "title": f"[InvestPilot] {title}",
            "desp": text.replace("\n", "\n\n"),
        }).encode("utf-8")

        req = urllib.request.Request(
            api_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            if not raw:
                logger.warning(f"Server酱返回空响应")
                return False
            result = json.loads(raw)
            if result and (result.get("code") == 0 or result.get("errno") == 0):
                logger.info(f"Server酱推送成功: {title}")
                return True
            else:
                logger.warning(f"Server酱推送失败: {result}")
                return False
    except Exception as e:
        logger.warning(f"Server酱推送异常: {e}")
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

    try:
        # 颜色映射
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
                        "content": text.replace("**", "").replace("\n", "\n"),
                    },
                    {
                        "tag": "note",
                        "elements": [
                            {"tag": "plain_text", "content": f"InvestPilot · {level} · {__import__('datetime').datetime.now().strftime('%H:%M:%S')}"}
                        ],
                    },
                ],
            },
        }

        req = urllib.request.Request(
            FEISHU_WEBHOOK,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            if not raw:
                logger.warning(f"飞书返回空响应")
                return False
            result = json.loads(raw)
            if result and (result.get("code") == 0 or result.get("StatusCode") == 0):
                logger.info(f"飞书推送成功: {title}")
                return True
            else:
                logger.warning(f"飞书推送失败: {result}")
                return False
    except Exception as e:
        logger.warning(f"飞书推送异常: {e}")
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

    try:
        url = "http://www.pushover.net/pushover/api/push"
        # PushPlus 官方接口
        api_url = "https://www.pushplus.plus/send"

        payload = json.dumps({
            "token": PUSHPLUS_TOKEN,
            "title": f"[InvestPilot] {title}",
            "content": content,
            "type": "html",
        }).encode("utf-8")

        req = urllib.request.Request(
            api_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("code") == 0:
                logger.info(f"PushPlus推送成功: {title}")
                return True
            else:
                logger.warning(f"PushPlus推送失败: {result}")
                return False
    except Exception as e:
        logger.warning(f"PushPlus推送异常: {e}")
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

    try:
        # Bark URL format: https://api.day.app/{KEY}/{标题}/{内容}
        # 内容需要 URL 编码
        import urllib.parse

        encoded_content = urllib.parse.quote(content[:500])  # Bark 内容限制
        bark_url = f"{BARK_URL}/{urllib.parse.quote(title)}/{encoded_content}"

        req = urllib.request.Request(bark_url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("code") == 200:
                logger.info(f"Bark推送成功: {title}")
                return True
            else:
                logger.warning(f"Bark推送失败: {result}")
                return False
    except Exception as e:
        logger.warning(f"Bark推送异常: {e}")
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

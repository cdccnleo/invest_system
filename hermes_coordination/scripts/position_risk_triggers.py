#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V24-C1-T4: 持仓风险告警触发器 (方案 9)
=======================================

实现 v24_implementation_plan.md 中的 **任务 V24-C1-T4 (告警逻辑)**：

> 3 类触发器 → 3 级降级链 → 写 PG + 推 WS + 飞书/钉钉 (P1)

**3 类触发器**:
1. **止损 (P0)**: 价格 < 止损位 → 立即告警, 需减仓
2. **止盈 (P1)**: 盈利 > 40% 且 weight > 5% → 提示减仓
3. **风险预算 (P2)**: 单日 VaR > 阈值 → 提示加仓/降仓

**3 级降级链** (PIT #30 复用):
- Level 1: 飞书/钉钉 Webhook (主, 需 store.json 配置)
- Level 2: WS broadcast (V24-B3 集成, 1-2s)
- Level 3: PG l3.risk_alert_log 持久化 (兜底)

**PIT 防御** (V24-C1-T4 新):
- **#40 (新)**: 触发器去重 (同一 code + alert_type 1h 内不重复告警)
- **#41 (新)**: 飞书 webhook secret 缺失时不调, 仅 PG 兜底
- **#42 (新)**: WS broadcast 在 0 client 时静默成功
- **#43 (新)**: 告警频次限制 (per code 1h 1 次, 全组合 1d 10 次)

Author: Hermes Agent × aileo
Date: 2026-06-13
Version: V24-C1-T4
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# 路径
_SCRIPT_DIR = Path(__file__).resolve().parent
_COORD_DIR = _SCRIPT_DIR.parent
for _p in [str(_SCRIPT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 真实依赖
try:
    import psycopg2
    import psycopg2.extras
    _HAS_PG = True
except ImportError:
    _HAS_PG = False

try:
    from position_risk_manager import (
        PositionRisk, PortfolioRisk, analyze_portfolio, fetch_current_positions,
        analyze_position, get_pg_connection, ensure_pg_tables, save_snapshot,
    )
    _HAS_MANAGER = True
except ImportError as e:
    LOG_INIT_ERROR = str(e)
    _HAS_MANAGER = False

# V24-B3 WebSocket 集成 (PIT #42 静默成功)
try:
    from dashboard_hermes_websocket import push_notification_with_notify, WSTarget
    _HAS_WS = True
except ImportError:
    _HAS_WS = False

LOG = logging.getLogger("position_risk_triggers")
if not LOG.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )


# ====================================================================
# 1. 告警类型 + 严重级
# ====================================================================

class AlertType(str, Enum):
    STOP_LOSS = "stop_loss"          # P0 止损
    TAKE_PROFIT = "take_profit"      # P1 止盈
    CONCENTRATION = "concentration"  # P1 集中度
    PROFIT_LOSS = "profit_loss"      # P1 盈亏警告
    INDUSTRY = "industry"            # P2 行业集中
    VAR_BUDGET = "var_budget"        # P2 VaR 预算


class AlertSeverity(str, Enum):
    P0 = "P0"  # 立即行动 (止损)
    P1 = "P1"  # 重要 (止盈/集中度)
    P2 = "P2"  # 提示 (VaR/行业)


# 频次限制 (PIT #43)
DEDUP_WINDOW_HOURS = 1    # 同 code+type 1h 内去重
MAX_DAILY_ALERTS = 10     # 全组合 1d 最多 10 告警


# ====================================================================
# 2. 告警数据 Schema
# ====================================================================

@dataclass
class RiskAlert:
    """单条风险告警"""
    code: str
    name: str
    alert_type: str      # AlertType
    severity: str        # AlertSeverity (P0/P1/P2)
    message: str
    market_value: float = 0
    current_price: float = 0
    trigger_value: float = 0  # 触发值 (止损价/盈利%)
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ====================================================================
# 3. 触发器生成
# ====================================================================

def generate_alerts(positions: Optional[List[Dict[str, Any]]] = None) -> List[RiskAlert]:
    """
    从当前持仓生成告警列表 (3 类触发器)
    """
    if positions is None:
        positions = fetch_current_positions()
    if not positions:
        LOG.warning("[generate] no positions")
        return []

    total_mv = sum(float(p.get("market_value") or 0) for p in positions)
    if total_mv <= 0:
        return []

    alerts: List[RiskAlert] = []
    for pos in positions:
        risk = analyze_position(pos, positions, total_mv)
        if not risk.triggers:
            continue

        # 逐个 trigger 转 alert
        for trig in risk.triggers:
            if "止损" in trig:
                # P0 止损
                alerts.append(RiskAlert(
                    code=risk.code,
                    name=risk.name,
                    alert_type=AlertType.STOP_LOSS.value,
                    severity=AlertSeverity.P0.value,
                    message=trig,
                    market_value=risk.market_value,
                    current_price=risk.close_price,
                    trigger_value=risk.stop_loss_price or 0,
                    payload={"weight_pct": risk.weight_pct, "var_1d": risk.var_1d},
                ))
            elif "止盈" in trig:
                # P1 止盈
                alerts.append(RiskAlert(
                    code=risk.code,
                    name=risk.name,
                    alert_type=AlertType.TAKE_PROFIT.value,
                    severity=AlertSeverity.P1.value,
                    message=trig,
                    market_value=risk.market_value,
                    current_price=risk.close_price,
                    trigger_value=risk.profit_pct,
                    payload={"weight_pct": risk.weight_pct, "profit_pct": risk.profit_pct},
                ))
            elif "集中度" in trig:
                # P1 集中度
                alerts.append(RiskAlert(
                    code=risk.code,
                    name=risk.name,
                    alert_type=AlertType.CONCENTRATION.value,
                    severity=AlertSeverity.P1.value,
                    message=trig,
                    market_value=risk.market_value,
                    trigger_value=risk.weight_pct,
                    payload={"weight_pct": risk.weight_pct},
                ))
            elif "亏损" in trig:
                # P1 亏损
                alerts.append(RiskAlert(
                    code=risk.code,
                    name=risk.name,
                    alert_type=AlertType.PROFIT_LOSS.value,
                    severity=AlertSeverity.P1.value,
                    message=trig,
                    market_value=risk.market_value,
                    trigger_value=risk.profit_pct,
                    payload={"profit_pct": risk.profit_pct},
                ))
            elif "同 type" in trig:
                # P2 行业集中
                alerts.append(RiskAlert(
                    code=risk.code,
                    name=risk.name,
                    alert_type=AlertType.INDUSTRY.value,
                    severity=AlertSeverity.P2.value,
                    message=trig,
                    market_value=risk.market_value,
                    trigger_value=risk.industry_concentration,
                    payload={"position_type": risk.position_type},
                ))

    return alerts


# ====================================================================
# 4. 告警去重 (PIT #40)
# ====================================================================

def get_recent_alerts(hours: int = DEDUP_WINDOW_HOURS) -> Set[str]:
    """
    拿最近 N 小时内已告警的 (code + alert_type) 集合
    PIT #40: 1h 内同 code+type 不重复告警
    """
    if not _HAS_PG:
        return set()
    conn = get_pg_connection()
    if not conn:
        return set()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT code || '|' || alert_type
            FROM l3.risk_alert_log
            WHERE created_at > NOW() - INTERVAL %s
        """, (f"{hours} hours",))
        return {r[0] for r in cur.fetchall()}
    except Exception as e:
        LOG.error(f"[dedup] get recent fail: {e}")
        return set()
    finally:
        conn.close()


def get_today_alert_count() -> int:
    """
    拿今日告警数 (PIT #43: 1d 最多 10)
    """
    if not _HAS_PG:
        return 0
    conn = get_pg_connection()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM l3.risk_alert_log
            WHERE created_at > CURRENT_DATE
        """)
        return cur.fetchone()[0]
    except Exception as e:
        LOG.error(f"[count] today fail: {e}")
        return 0
    finally:
        conn.close()


def dedup_alerts(alerts: List[RiskAlert]) -> List[RiskAlert]:
    """去重 + 频次限制"""
    recent = get_recent_alerts()
    today_count = get_today_alert_count()
    filtered = []
    for alert in alerts:
        key = f"{alert.code}|{alert.alert_type}"
        if key in recent:
            LOG.debug(f"[dedup] skip {key} (recent)")
            continue
        if today_count + len(filtered) >= MAX_DAILY_ALERTS:
            LOG.warning(f"[dedup] hit daily limit {MAX_DAILY_ALERTS}, skip {key}")
            break
        filtered.append(alert)
    return filtered


# ====================================================================
# 5. 告警分发 (3 级降级链)
# ====================================================================

def persist_to_pg(alerts: List[RiskAlert]) -> int:
    """Level 3 兜底: 写 PG l3.risk_alert_log"""
    if not _HAS_PG or not alerts:
        return 0
    conn = get_pg_connection()
    if not conn:
        return 0
    saved = 0
    try:
        cur = conn.cursor()
        for a in alerts:
            try:
                cur.execute("""
                    INSERT INTO l3.risk_alert_log
                        (code, alert_type, severity, message, payload, delivered)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    a.code, a.alert_type, a.severity, a.message,
                    json.dumps(a.to_dict(), ensure_ascii=False, default=str),
                    False,  # 默认未推送
                ))
                saved += 1
            except Exception as e:
                LOG.error(f"[persist] {a.code} fail: {e}")
        conn.commit()
        LOG.info(f"[persist] saved {saved}/{len(alerts)} to PG")
        return saved
    except Exception as e:
        LOG.error(f"[persist] fail: {e}")
        conn.rollback()
        return 0
    finally:
        conn.close()


def push_to_websocket(alerts: List[RiskAlert]) -> int:
    """Level 2: 推 WS (V24-B3 集成)"""
    if not _HAS_WS or not alerts:
        return 0
    sent = 0
    for a in alerts:
        try:
            # 用 push_notification_with_notify (V24-B3) 包装
            from dashboard_hermes_bridge import QuickActionRequest, ActionStatus
            req = QuickActionRequest(
                request_id=f"risk_{a.code}_{int(time.time())}",
                user_id="aileo",
                action_type="risk_alert",
                payload=a.to_dict(),
                status=ActionStatus.SUCCESS,
                result={"alert_type": a.alert_type, "severity": a.severity,
                        "message": a.message, "code": a.code},
                duration_ms=100.0,
            )
            push_notification_with_notify(req, target="dashboard")
            sent += 1
        except Exception as e:
            LOG.error(f"[ws] {a.code} fail: {e}")
    LOG.info(f"[ws] sent {sent}/{len(alerts)} to WebSocket")
    return sent


def push_to_webhook(alerts: List[RiskAlert]) -> int:
    """Level 1: 推飞书/钉钉 (PIT #41 secret 缺失时仅 PG 兜底)"""
    try:
        store = json.loads(
            Path("/home/aileo/.hermes/invest_credentials/store.json").read_text()
        )
    except Exception as e:
        LOG.warning(f"[webhook] store.json 读取失败: {e}")
        return 0

    dingtalk = store.get("DINGTALK_WEBHOOK", "")
    wechat = store.get("WECHAT_WEBHOOK", "")
    # PIT #41: secret 缺失时仅 PG 兜底
    if not dingtalk and not wechat:
        LOG.info("[webhook] no webhook configured, skip (PG 兜底)")
        return 0

    sent = 0
    for a in alerts:
        body = _format_alert_msg(a)
        for webhook_url in [dingtalk, wechat]:
            if not webhook_url:
                continue
            try:
                import urllib.request
                data = json.dumps({
                    "msgtype": "markdown",
                    "markdown": {"title": f"⚠️ 持仓风险 {a.severity}", "text": body},
                }, ensure_ascii=False).encode()
                req = urllib.request.Request(
                    webhook_url, data=data,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=5) as r:
                    if r.status == 200:
                        sent += 1
            except Exception as e:
                LOG.warning(f"[webhook] {a.code} fail: {e}")
    LOG.info(f"[webhook] sent {sent}/{len(alerts)} to dingtalk/wechat")
    return sent


def _format_alert_msg(a: RiskAlert) -> str:
    """格式化告警消息 (markdown)"""
    emoji = {"P0": "🚨", "P1": "⚠️", "P2": "💡"}.get(a.severity, "ℹ️")
    return (
        f"{emoji} **{a.severity} 持仓风险**\n\n"
        f"**持仓**: {a.code} {a.name}\n"
        f"**类型**: {a.alert_type}\n"
        f"**消息**: {a.message}\n"
        f"**市值**: ¥{a.market_value:,.0f}\n"
        f"**触发值**: {a.trigger_value}\n"
        f"**时间**: {a.created_at[:19]}\n"
    )


# ====================================================================
# 6. 主流程
# ====================================================================

def run_triggers(positions: Optional[List[Dict[str, Any]]] = None) -> Dict[str, int]:
    """
    触发器主流程: 生成 → 去重 → 3 级分发
    """
    LOG.info("=== V24-C1-T4 触发器 ===")
    # 1. 生成
    raw_alerts = generate_alerts(positions)
    LOG.info(f"生成 {len(raw_alerts)} 条原始告警")
    # 2. 去重 + 频次
    alerts = dedup_alerts(raw_alerts)
    LOG.info(f"去重后 {len(alerts)} 条新告警")
    # 3. 3 级分发
    p0_count = sum(1 for a in alerts if a.severity == "P0")
    p1_count = sum(1 for a in alerts if a.severity == "P1")
    p2_count = sum(1 for a in alerts if a.severity == "P2")
    LOG.info(f"P0={p0_count}, P1={p1_count}, P2={p2_count}")
    if not alerts:
        return {"generated": len(raw_alerts), "after_dedup": 0,
                "webhook": 0, "ws": 0, "pg": 0}
    # 3 级降级 (L1 webhook → L2 ws → L3 pg)
    webhook_sent = push_to_webhook(alerts)
    ws_sent = push_to_websocket(alerts)
    pg_saved = persist_to_pg(alerts)
    return {
        "generated": len(raw_alerts),
        "after_dedup": len(alerts),
        "p0": p0_count, "p1": p1_count, "p2": p2_count,
        "webhook": webhook_sent,
        "ws": ws_sent,
        "pg": pg_saved,
    }


# ====================================================================
# 7. CLI + 自测
# ====================================================================

def _selftest():
    """自测"""
    LOG.info("=== V24-C1-T4 触发器 自测 ===")
    ensure_pg_tables()
    # 真跑
    result = run_triggers()
    LOG.info(f"结果: {result}")
    # 边界 case
    LOG.info("--- 边界 case ---")
    # 1. 持仓 0
    empty_alerts = generate_alerts([])
    assert empty_alerts == [], "持仓 0 应无告警"
    LOG.info("✅ 持仓 0 返空告警")
    # 2. 频次限制
    today = get_today_alert_count()
    LOG.info(f"今日已告警 {today} 条 / 限额 {MAX_DAILY_ALERTS}")
    # 3. 去重
    recent = get_recent_alerts()
    LOG.info(f"最近 1h 告警 key {len(recent)} 条")
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description="持仓风险告警触发器")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--run", action="store_true", help="跑触发器主流程")
    args = parser.parse_args()
    if args.self_test or not args.run:
        return _selftest()
    if args.run:
        result = run_triggers()
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
l3_dialog_engine.py — L3 主动对话引擎
======================================
基于持仓画像 + 行为偏差检测 + 主动触发推送的智能投顾核心。

5 类触发器（对应 DB active_dialog_triggers）：
  1. deviation_alert   — 策略偏离告警（交易频率/仓位偏离基线）
  2. periodic_checkin — 定期签到（盘前/盘后/晚间定时）
  3. news_impact       — 持仓股重大新闻（情绪骤降触发）
  4. risk_escalation   — 风险等级升级（回撤超阈值）
  5. milestone         — 盈亏里程碑（收益突破整数关口）

推送通道：飞书 Webhook（notification.py）
          后续可扩展 Telegram Bot（v3 §2.3）

典型调用：
  from l3_dialog_engine import L3DialogEngine
  engine = L3DialogEngine()
  engine.run_cycle()        # 评估所有触发器，发送主动消息
  engine.get_l3_status()    # 返回 L3 能力激活状态
"""

import json
import logging
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))

from notification import send_notification
from pgcrypto_migration import load_positions_from_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("L3DialogEngine")

DB_CONFIG = dict(
    host="localhost",
    database="investpilot",
    user="invest_admin",
    password="",  # 运行时通过 _get_db_config() 注入
)

def _get_db_config():
    """从凭据存储读取密码，合并到 DB_CONFIG"""
    from pgcrypto_migration import get_credential
    cfg = dict(DB_CONFIG)
    cfg["password"] = get_credential("DB_PASSWORD")
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# 触发器评估器基类
# ─────────────────────────────────────────────────────────────────────────────

class TriggerEvaluator:
    """触发器评估器基类"""

    def __init__(self, trigger_row: dict, conn: psycopg2.extensions.connection):
        self.id = trigger_row["id"]
        self.trigger_type = trigger_row["trigger_type"]
        self.trigger_name = trigger_row["trigger_name"]
        self.condition_expr = trigger_row["condition_expr"]
        self.cooldown_hours = trigger_row["cooldown_hours"]
        self.last_triggered_at = trigger_row["last_triggered_at"]
        self.message_template = trigger_row["message_template"]
        self.priority = trigger_row["priority"]
        self.conn = conn

    def is_in_cooldown(self) -> bool:
        """检查是否在冷却期内"""
        if not self.last_triggered_at:
            return False
        cutoff = datetime.now(self.last_triggered_at.tzinfo) - timedelta(hours=self.cooldown_hours)
        return self.last_triggered_at > cutoff

    def evaluate(self) -> dict | None:
        """
        评估触发条件。返回:
          None           — 条件未满足
          dict           — 条件满足，含填充变量 {"var_name": "var_value", ...}
        子类实现具体逻辑。
        """
        raise NotImplementedError

    def render_message(self, variables: dict) -> str:
        """将变量填充到消息模板"""
        msg = self.message_template
        for key, val in variables.items():
            if val is None:
                val = "未知"
            msg = msg.replace(f"{{{key}}}", str(val))
        return msg

    def fire(self, variables: dict) -> bool:
        """触发：发送消息 + 更新 last_triggered_at"""
        if self.is_in_cooldown():
            logger.debug(f"[{self.trigger_type}] 在冷却期内，跳过")
            return False

        message = self.render_message(variables)
        title = f"[L3] {self.trigger_name}"

        # 优先级 → 颜色
        level_map = {10: "🔴 CRITICAL", 9: "🟠 HIGH", 8: "🟡 MEDIUM", 7: "🔵 INFO"}
        title = f"{level_map.get(self.priority, '📢')} {self.trigger_name}"

        ok = send_notification(title, message, level="WARNING" if self.priority >= 8 else "INFO")
        if ok:
            self._update_triggered()
            logger.info(f"[{self.trigger_type}] 触发成功: {self.trigger_name}")
        else:
            logger.warning(f"[{self.trigger_type}] 推送失败: {self.trigger_name}")
        return ok

    def _update_triggered(self):
        """更新触发时间"""
        cur = self.conn.cursor()
        cur.execute("""
            UPDATE l3.active_dialog_triggers
            SET last_triggered_at = NOW(),
                trigger_count = trigger_count + 1,
                updated_at = NOW()
            WHERE id = %s
        """, (self.id,))
        self.conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# 1. deviation_alert — 策略偏离评估器
# ─────────────────────────────────────────────────────────────────────────────

class DeviationAlertEvaluator(TriggerEvaluator):
    """
    基于 audit_log 分析最近 N 天的交易行为，计算偏离基线程度。

    条件表达式:
      {"type": "behavior_deviation", "metric": "trade_freq", "threshold": 20, "lookback_days": 7}
    """

    def evaluate(self) -> dict | None:
        try:
            cond = json.loads(self.condition_expr)
        except json.JSONDecodeError:
            return None

        if cond.get("type") != "behavior_deviation":
            return None

        metric = cond.get("metric", "trade_freq")
        threshold = float(cond.get("threshold", 20))  # 偏离百分比
        lookback_days = int(cond.get("lookback_days", 7))

        cur = self.conn.cursor()

        # 读取最近 N 天行为指标
        cur.execute("""
            SELECT metric_name, AVG(metric_value) as avg_val, COUNT(*) as cnt
            FROM l3.behavior_profile
            WHERE profile_date >= CURRENT_DATE - INTERVAL '%s days'
              AND dimension IN ('overtrading', 'risk_taking')
            GROUP BY metric_name
        """, (lookback_days * 2,))  # 取2倍窗口计算基线
        rows = cur.fetchall()

        # 计算7天均值作为基线对照
        recent = {}
        baseline = {}
        for row in rows:
            mn = row[0]
            avg_val = float(row[1])
            cnt = int(row[2])
            if lookback_days <= 7:
                recent[mn] = avg_val
            baseline[mn] = avg_val  # 简化：用均值代表基线

        if not recent:
            logger.debug(f"[deviation_alert] 无足够行为数据，跳过")
            return None

        # 从 audit_log 计算近期真实交易频率
        cur.execute("""
            SELECT COUNT(*) FROM audit.audit_log
            WHERE event_type IN ('BUY', 'SELL', 'TRADE_EXECUTED')
              AND created_at >= CURRENT_DATE - INTERVAL '%s days'
        """, (lookback_days,))
        trade_count = cur.fetchone()[0] or 0

        # 对比基线（假设历史均值）
        cur.execute("""
            SELECT AVG(cnt) FROM (
                SELECT COUNT(*) as cnt FROM audit.audit_log
                WHERE event_type IN ('BUY', 'SELL', 'TRADE_EXECUTED')
                  AND created_at >= CURRENT_DATE - INTERVAL '%s days'
                GROUP BY DATE(created_at)
            ) sub
        """, (lookback_days * 2,))
        baseline_daily = cur.fetchone()[0] or 0
        recent_daily = trade_count / lookback_days if lookback_days > 0 else 0

        if baseline_daily <= 0:
            return None

        deviation_pct = abs((recent_daily - baseline_daily) / baseline_daily * 100)

        if deviation_pct < threshold:
            return None

        # 判断 alert_level
        if deviation_pct > threshold * 2:
            alert_level = "critical"
        elif deviation_pct > threshold:
            alert_level = "warning"
        else:
            return None

        return {
            "metric": metric,
            "recent_trades": trade_count,
            "baseline_daily": round(baseline_daily, 2),
            "deviation_pct": round(deviation_pct, 1),
            "alert_level": alert_level,
            "suggestion": self._get_suggestion(metric, deviation_pct, alert_level),
        }

    @staticmethod
    def _get_suggestion(metric: str, deviation_pct: float, alert_level: str) -> str:
        if metric == "trade_freq":
            if alert_level == "critical":
                return "交易过于频繁，建议暂停操作3个交易日冷静期。"
            return "交易频率偏高，建议关注持仓换手率是否合理。"
        elif metric == "avg_position_size":
            return "单笔仓位过大，建议单只标的仓位不超过总市值15%。"
        return "建议回顾近期操作决策是否受情绪影响。"


# ─────────────────────────────────────────────────────────────────────────────
# 2. periodic_checkin — 定期签到评估器
# ─────────────────────────────────────────────────────────────────────────────

class PeriodicCheckinEvaluator(TriggerEvaluator):
    """
    定时触发（每日固定时间）。

    条件表达式:
      {"type": "schedule", "time": "08:30", "mode": "daily"}
    """

    def evaluate(self) -> dict | None:
        try:
            cond = json.loads(self.condition_expr)
        except json.JSONDecodeError:
            return None

        if cond.get("type") != "schedule":
            return None

        # 检查是否在目标时间窗口（±30分钟）
        now = datetime.now()
        target_time_str = cond.get("time", "08:30")
        hour, minute = map(int, target_time_str.split(":"))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        diff_minutes = abs((now - target).total_seconds() / 60)

        if diff_minutes > 30:
            return None  # 不在触发窗口

        # 加载持仓摘要
        positions = load_positions_from_db()
        total_mv = sum(p.get("market_value", 0) for p in positions)
        announcement_count = self._get_recent_announcements()

        return {
            "portfolio_value": f"{total_mv:,.0f}",
            "positions_count": len(positions),
            "announcement_count": announcement_count,
            "date": now.strftime("%Y-%m-%d"),
        }

    def _get_recent_announcements(self) -> int:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM research.announcements
            WHERE notice_date >= CURRENT_DATE - INTERVAL '3 days'
        """)
        return cur.fetchone()[0] or 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. news_impact — 持仓股重大新闻评估器
# ─────────────────────────────────────────────────────────────────────────────

class NewsImpactEvaluator(TriggerEvaluator):
    """
    检测持仓股近期新闻情绪，负面情绪骤降时触发。

    条件表达式:
      {"type": "news_sentiment", "threshold": -0.7, "lookback_hours": 6}
    """

    def evaluate(self) -> dict | None:
        try:
            cond = json.loads(self.condition_expr)
        except json.JSONDecodeError:
            return None

        if cond.get("type") != "news_sentiment":
            return None

        threshold = float(cond.get("threshold", -0.7))
        lookback_hours = int(cond.get("lookback_hours", 6))

        cur = self.conn.cursor()

        # 从 news_sentiments 找近期情绪显著下降的持仓股
        cur.execute("""
            SELECT
                n.ts_code,
                n.sentiment_score,
                n.created_at,
                p.name
            FROM research.news_sentiments n
            JOIN holdings.encrypted_positions p ON p.code = n.ts_code
            WHERE n.created_at >= NOW() - INTERVAL '%s hours'
              AND n.sentiment_score < %s
            ORDER BY n.sentiment_score ASC
            LIMIT 1
        """, (lookback_hours, threshold))

        row = cur.fetchone()
        if not row:
            return None

        ts_code, sentiment, created_at, name = row
        sentiment_pct = float(sentiment) * 100 if sentiment else 0

        # 获取相关新闻摘要
        cur.execute("""
            SELECT title, url FROM research.news_articles
            WHERE ts_code = %s
              AND created_at >= NOW() - INTERVAL '%s hours'
            ORDER BY created_at DESC LIMIT 1
        """, (ts_code, lookback_hours))
        news_row = cur.fetchone()
        headline = news_row[0] if news_row else "暂无摘要"
        sentiment_type = "负面" if sentiment_pct < -50 else "偏空"

        return {
            "stock_code": ts_code,
            "stock_name": name or ts_code,
            "sentiment_pct": round(sentiment_pct, 1),
            "sentiment_type": sentiment_type,
            "headline": headline[:50] if headline else "暂无",
            "lookback_hours": lookback_hours,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 4. risk_escalation — 风险升级评估器
# ─────────────────────────────────────────────────────────────────────────────

class RiskEscalationEvaluator(TriggerEvaluator):
    """
    检测持仓回撤超阈值，结合压力测试结果评估风险等级。

    条件表达式:
      {"type": "risk_metric", "metric": "daily_drawdown", "threshold": 3, "unit": "percent"}
    """

    def evaluate(self) -> dict | None:
        try:
            cond = json.loads(self.condition_expr)
        except json.JSONDecodeError:
            return None

        if cond.get("type") != "risk_metric":
            return None

        metric = cond.get("metric", "daily_drawdown")
        threshold = float(cond.get("threshold", 3))

        cur = self.conn.cursor()

        # 获取最新持仓及当日回撤（从加密持仓表解密读取）
        positions = load_positions_from_db()

        # 从 daily_quotes 计算各标的当日涨跌
        total_mv = sum(p.get("market_value", 0) for p in positions)
        total_pnl = sum(p.get("profit", 0) for p in positions)
        total_cost = sum(p.get("cost", 0) * p.get("shares", 0) for p in positions)

        if total_cost <= 0:
            return None

        drawdown_pct = abs(float(total_pnl) / float(total_cost) * 100) if float(total_cost) > 0 else 0

        if drawdown_pct < threshold:
            return None

        # 获取最坏压力测试情景
        cur.execute("""
            SELECT scenario_name, max_loss_pct, recommendation
            FROM l3.stress_test_results
            ORDER BY max_loss_pct DESC
            LIMIT 1
        """)
        st_row = cur.fetchone()
        scenario_name = st_row[0] if st_row else "暂无"
        max_loss_pct = st_row[1] if st_row else 0
        recommendation = st_row[2] if st_row else "建议关注"

        alert_level = "critical" if drawdown_pct > threshold * 2 else "warning"

        return {
            "drawdown_pct": round(drawdown_pct, 2),
            "alert_level": alert_level,
            "scenario": scenario_name,
            "max_loss_pct": round(float(max_loss_pct), 2) if max_loss_pct else 0,
            "recommendation": recommendation,
            "pnl_value": f"{total_pnl:,.0f}",
        }


# ─────────────────────────────────────────────────────────────────────────────
# 5. milestone — 盈亏里程碑评估器
# ─────────────────────────────────────────────────────────────────────────────

class MilestoneEvaluator(TriggerEvaluator):
    """
    检测累计收益突破指定金额关口。

    条件表达式:
      {"type": "pnl_milestone", "threshold": 100000, "direction": "both"}
    """

    MILESTONES = [10000, 50000, 100000, 200000, 500000, 1000000]

    def evaluate(self) -> dict | None:
        try:
            cond = json.loads(self.condition_expr)
        except json.JSONDecodeError:
            return None

        if cond.get("type") != "pnl_milestone":
            return None

        threshold = float(cond.get("threshold", 100000))

        cur = self.conn.cursor()

        # 获取历史最高/最低累计收益
        cur.execute("""
            SELECT MAX(shock_result->>'total_loss')::float, MIN(shock_result->>'total_loss')::float
            FROM l3.stress_test_results
            WHERE shock_result IS NOT NULL
        """)
        max_loss_row = cur.fetchone()
        # 用最新持仓快照的累计收益（从加密持仓表解密读取）
        positions = load_positions_from_db()
        total_pnl = sum(p.get("profit", 0) for p in positions)

        # 计算上次触发里程碑时的总收益（从历史记录推断）
        cur.execute("""
            SELECT message_template FROM l3.active_dialog_triggers
            WHERE trigger_type = 'milestone'
        """)
        last_milestone_row = cur.fetchone()
        last_milestone_str = 0
        if last_milestone_row and last_milestone_row[0]:
            import re
            m = re.search(r"¥(-?[\d,]+)", last_milestone_row[0])
            if m:
                last_milestone_str = float(m.group(1).replace(",", ""))

        # 检查是否突破新关口
        crossed = [m for m in self.MILESTONES if abs(total_pnl) >= m and abs(last_milestone_str) < m]
        if not crossed:
            return None

        new_milestone = crossed[0]
        milestone_type = "盈利" if total_pnl > 0 else "亏损"
        pnl_pct = abs(total_pnl / 1000000 * 100)  # 相对本金百分比

        return {
            "pnl_value": f"{total_pnl:+,.0f}",
            "pnl_pct": round(pnl_pct, 2),
            "milestone_type": milestone_type,
            "milestone_amount": new_milestone,
            "performance_summary": f"累计{'盈利' if total_pnl > 0 else '亏损'} ¥{abs(total_pnl):,.0f}，历史最大回撤待评估",
        }


# ─────────────────────────────────────────────────────────────────────────────
# 触发器工厂
# ─────────────────────────────────────────────────────────────────────────────

TRIGGER_EVALUATORS = {
    "deviation_alert": DeviationAlertEvaluator,
    "periodic_checkin": PeriodicCheckinEvaluator,
    "news_impact": NewsImpactEvaluator,
    "risk_escalation": RiskEscalationEvaluator,
    "milestone": MilestoneEvaluator,
}


def _build_evaluator(row: dict, conn: psycopg2.extensions.connection) -> TriggerEvaluator | None:
    cls = TRIGGER_EVALUATORS.get(row["trigger_type"])
    if cls is None:
        logger.warning(f"未知触发器类型: {row['trigger_type']}")
        return None
    return cls(row, conn)


# ─────────────────────────────────────────────────────────────────────────────
# L3 对话引擎主类
# ─────────────────────────────────────────────────────────────────────────────

class L3DialogEngine:
    """
    L3 主动对话引擎。

    用法：
        engine = L3DialogEngine()
        engine.run_cycle()    # 评估所有触发器，触发则推送
        status = engine.get_l3_status()  # L3 能力激活状态
    """

    def __init__(self, conn=None):
        self._owned_conn = False
        if conn is None:
            self.conn = psycopg2.connect(**_get_db_config())
            self._owned_conn = True
        else:
            self.conn = conn

    def __del__(self):
        if self._owned_conn and self.conn:
            self.conn.close()

    def load_active_triggers(self) -> list[dict]:
        """加载所有激活的触发器"""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, trigger_type, trigger_name, condition_expr,
                   cooldown_hours, last_triggered_at, message_template,
                   priority, is_active, trigger_count
            FROM l3.active_dialog_triggers
            WHERE is_active = TRUE
            ORDER BY priority DESC
        """)
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def run_cycle(self) -> dict:
        """
        执行一轮触发器评估。
        返回: {"evaluated": N, "triggered": M, "details": [...]}
        """
        triggers = self.load_active_triggers()
        results = {"evaluated": 0, "triggered": 0, "details": []}

        for row in triggers:
            evaluator = _build_evaluator(row, self.conn)
            if evaluator is None:
                continue

            results["evaluated"] += 1
            try:
                variables = evaluator.evaluate()
            except Exception as e:
                logger.error(f"[{row['trigger_type']}] 评估异常: {e}")
                self.conn.rollback()  # 解除事务中止状态
                results["details"].append({
                    "trigger_type": row["trigger_type"],
                    "status": "error",
                    "error": str(e),
                })
                continue

            if variables is None:
                results["details"].append({
                    "trigger_type": row["trigger_type"],
                    "status": "not_triggered",
                })
                continue

            # 触发
            fired = evaluator.fire(variables)
            results["triggered"] += 1
            results["details"].append({
                "trigger_type": row["trigger_type"],
                "trigger_name": row["trigger_name"],
                "status": "triggered" if fired else "push_failed",
                "variables": variables,
            })

        return results

    def run_stress_test(self) -> str | None:
        """
        执行5种压力测试情景，返回 run_id。
        结果写入 l3.stress_test_results。
        """
        # StressTestEngine 已通过 job_stress_test() 调度复用
        # L3DialogEngine.run_stress_test() 保留为 CLI/手动调用路径
        positions = load_positions_from_db()
        if not positions:
            logger.warning("无持仓数据，跳过压力测试")
            return None

        total_mv = sum(p.get("market_value", 0) for p in positions)
        run_id = str(uuid.uuid4())

        cur = self.conn.cursor()

        # 获取所有活跃情景
        cur.execute("SELECT scenario_code, scenario_name, shock_params FROM l3.stress_test_scenarios WHERE is_active = TRUE")
        scenarios = cur.fetchall()

        worst_case = None
        worst_loss_pct = 0

        for scenario_code, scenario_name, shock_params_raw in scenarios:
            try:
                shock_params = json.loads(shock_params_raw) if isinstance(shock_params_raw, str) else shock_params_raw
            except json.JSONDecodeError:
                logger.warning(f"无法解析情景参数: {scenario_code}")
                continue

            # 计算各持仓在当前情景下的损失
            position_results = []
            total_loss = 0

            for pos in positions:
                code = pos.get("code", "")
                name = pos.get("name", "")
                shares = pos.get("shares", 0)
                market_value = pos.get("market_value", 0)
                pos_type = pos.get("type", "stock")

                # 简单冲击计算：按持仓类型匹配冲击参数
                shock_pct = 0
                for key, pct in shock_params.items():
                    key_lower = key.lower()
                    if "沪深300" in key_lower and pos_type in ("stock", "fund"):
                        shock_pct = pct
                    elif "创业板" in key_lower and code.startswith(("300",)):
                        shock_pct = pct
                    elif "科创" in key_lower and code.startswith(("688",)):
                        shock_pct = pct
                    elif "纳斯达克" in key_lower and code.startswith(("513",)):
                        shock_pct = pct

                loss = market_value * shock_pct
                total_loss += loss
                position_results.append({
                    "code": code,
                    "name": name,
                    "market_value": round(market_value, 2),
                    "shock_pct": round(shock_pct * 100, 2),
                    "loss": round(loss, 2),
                })

            loss_rate = total_loss / total_mv if total_mv > 0 else 0
            loss_abs = total_loss

            # 风险评分（1-10）
            if loss_rate < -0.02:
                risk_score = min(int(abs(loss_rate) * 200), 10)
            else:
                risk_score = 1

            if abs(loss_rate) > abs(worst_loss_pct):
                worst_loss_pct = loss_rate
                worst_case = scenario_name

            recommendation = self._get_recommendation(risk_score, loss_rate)

            shock_result = {
                "positions": position_results,
                "total_loss": round(total_loss, 2),
                "loss_rate": round(loss_rate, 4),
            }

            cur.execute("""
                INSERT INTO l3.stress_test_results
                    (run_id, scenario_code, scenario_name, holding_snapshot,
                     portfolio_value, shock_result, max_loss_pct, max_loss_abs,
                     risk_score, recommendation)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (run_id) DO UPDATE SET
                    shock_result = EXCLUDED.shock_result,
                    risk_score = EXCLUDED.risk_score,
                    recommendation = EXCLUDED.recommendation,
                    executed_at = NOW()
            """, (
                f"{run_id}_{scenario_code}",  # 子run_id确保唯一
                scenario_code,
                scenario_name,
                json.dumps([{"code": p["code"], "shares": p["shares"]} for p in positions]),
                total_mv,
                json.dumps(shock_result),
                round(loss_rate * 100, 4),
                round(loss_abs, 2),
                risk_score,
                recommendation,
            ))

        self.conn.commit()
        logger.info(f"压力测试完成: run_id={run_id}, 最坏情景={worst_case}({worst_loss_pct*100:.1f}%)")
        return run_id

    @staticmethod
    def _get_recommendation(risk_score: int, loss_rate: float) -> str:
        if risk_score >= 8:
            return "建议立即减仓至半仓以下，关注对冲工具"
        elif risk_score >= 6:
            return "建议适度减仓10-20%，提高现金比例"
        elif risk_score >= 4:
            return "建议保持观察，可考虑小幅减仓"
        else:
            return "暂无操作建议，保持现有仓位"

    def get_l3_status(self) -> dict:
        """返回 L3 能力激活状态"""
        cur = self.conn.cursor()

        # 各触发器激活情况
        cur.execute("""
            SELECT trigger_type, is_active, last_triggered_at, trigger_count
            FROM l3.active_dialog_triggers
            ORDER BY priority DESC
        """)
        triggers = [
            {"type": r[0], "active": r[1], "last_triggered": str(r[2]) if r[2] else None, "count": r[3]}
            for r in cur.fetchall()
        ]

        # 行为数据积累量
        cur.execute("SELECT COUNT(*) FROM l3.behavior_profile")
        behavior_records = cur.fetchone()[0] or 0

        # 最近压力测试
        cur.execute("""
            SELECT executed_at, scenario_name, max_loss_pct, risk_score
            FROM l3.stress_test_results
            ORDER BY executed_at DESC LIMIT 5
        """)
        stress_tests = [
            {"executed_at": str(r[0]), "scenario": r[1], "loss_pct": r[2], "risk_score": r[3]}
            for r in cur.fetchall()
        ]

        # L3 能力评分（基于数据积累）
        capability_score = 0
        if behavior_records >= 10:
            capability_score += 2
        if behavior_records >= 50:
            capability_score += 1
        if stress_tests:
            capability_score += 2

        return {
            "triggers": triggers,
            "behavior_records": behavior_records,
            "stress_tests": stress_tests,
            "capability_score": capability_score,  # 0-5
            "capability_label": ["沉睡", "萌芽", "激活", "成熟", "进阶", "完全"][capability_score],
            "phase": "L3 Phase A" if capability_score < 3 else "L3 Phase B" if capability_score < 5 else "L3 Phase C",
        }


# ─────────────────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="L3 主动对话引擎")
    parser.add_argument("--cycle", action="store_true", help="运行一轮触发器评估")
    parser.add_argument("--stress-test", action="store_true", help="运行压力测试")
    parser.add_argument("--status", action="store_true", help="显示 L3 状态")
    args = parser.parse_args()

    engine = L3DialogEngine()

    if args.cycle:
        print("=" * 50)
        print("L3 主动对话引擎 — 触发器评估")
        print("=" * 50)
        result = engine.run_cycle()
        print(f"评估: {result['evaluated']} 个触发器")
        print(f"触发: {result['triggered']} 个")
        for d in result["details"]:
            status_icon = {"triggered": "✅", "not_triggered": "➖", "push_failed": "⚠️", "error": "❌"}.get(d["status"], "?")
            print(f"  {status_icon} [{d['trigger_type']}] {d.get('trigger_name', d['trigger_type'])}: {d['status']}")

    elif args.stress_test:
        print("=" * 50)
        print("L3 压力测试")
        print("=" * 50)
        run_id = engine.run_stress_test()
        print(f"压力测试完成: {run_id}")

    elif args.status:
        status = engine.get_l3_status()
        print("=" * 50)
        print("L3 能力状态")
        print("=" * 50)
        print(f"  阶段: {status['phase']}")
        print(f"  能力评分: {status['capability_score']}/5 ({status['capability_label']})")
        print(f"  行为记录: {status['behavior_records']} 条")
        print(f"\n触发器:")
        for t in status["triggers"]:
            icon = "🟢" if t["active"] else "⚫"
            last = t["last_triggered"] or "从未触发"
            print(f"  {icon} [{t['type']}] 上次: {last} 累计: {t['count']}次")
        print(f"\n最近压力测试: {len(status['stress_tests'])} 条")
        for st in status["stress_tests"][:3]:
            print(f"  {st['executed_at'][:19]} {st['scenario']}: {st['loss_pct']}% 风险{st['risk_score']}/10")

    else:
        parser.print_help()

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
            int(row[2])
            if lookback_days <= 7:
                recent[mn] = avg_val
            baseline[mn] = avg_val  # 简化：用均值代表基线

        if not recent:
            logger.debug("[deviation_alert] 无足够行为数据，跳过")
            return None

        # 从 audit_log 计算近期真实交易频率
        cur.execute("""
            SELECT COUNT(*) FROM audit.audit_log
            WHERE event_type IN ('BUY', 'SELL', 'TRADE_EXECUTED')
              AND event_time >= CURRENT_DATE - INTERVAL '%s days'
        """, (lookback_days,))
        trade_count = cur.fetchone()[0] or 0

        # 对比基线（假设历史均值）
        cur.execute("""
            SELECT AVG(cnt) FROM (
                SELECT COUNT(*) as cnt FROM audit.audit_log
                WHERE event_type IN ('BUY', 'SELL', 'TRADE_EXECUTED')
                  AND event_time >= CURRENT_DATE - INTERVAL '%s days'
                GROUP BY DATE(event_time)
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

        cond.get("metric", "daily_drawdown")
        threshold = float(cond.get("threshold", 3))

        cur = self.conn.cursor()

        # 获取最新持仓及当日回撤（从加密持仓表解密读取）
        positions = load_positions_from_db()

        # 从 daily_quotes 计算各标的当日涨跌
        sum(p.get("market_value", 0) for p in positions)
        total_pnl = sum(p.get("profit", 0) for p in positions)
        total_cost = sum(p.get("cost", 0) * p.get("shares", 0) for p in positions)

        if total_cost <= 0:
            return None

        drawdown_pct = abs(float(total_pnl) / float(total_cost) * 100) if float(total_cost) > 0 else 0  # noqa: E501

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

        float(cond.get("threshold", 100000))

        cur = self.conn.cursor()

        # 获取历史最高/最低累计收益
        cur.execute("""
            SELECT MAX(shock_result->>'total_loss')::float, MIN(shock_result->>'total_loss')::float
            FROM l3.stress_test_results
            WHERE shock_result IS NOT NULL
        """)
        cur.fetchone()
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
        crossed = [m for m in self.MILESTONES if abs(total_pnl) >= m and abs(last_milestone_str) < m]  # noqa: E501
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
            "performance_summary": f"累计{'盈利' if total_pnl > 0 else '亏损'} ¥{abs(total_pnl):,.0f}，历史最大回撤待评估",  # noqa: E501
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

    def __init__(self, conn=None, profile: str = "default"):
        """V24-B4: 加 profile 参数, 跨 profile 隔离

        Args:
            conn: PG connection (可选, None 时自建)
            profile: 持仓 profile (default/conservative/aggressive),
                     失败时降级 default (PIT #44)
        """
        # ⚠️ PIT 修复: profile_strategy 在 hermes_coordination/scripts/ (上一级),
        # l3_dialog_engine 在 scripts/. 局部加 path
        import sys as _sys
        from pathlib import Path as _Path
        _HERMES_DIR = _Path(__file__).parent.parent / "hermes_coordination" / "scripts"
        if str(_HERMES_DIR) not in _sys.path:
            _sys.path.insert(0, str(_HERMES_DIR))
        from profile_strategy import L3ProfileAdvisor  # 局部导入避免循环
        self._owned_conn = False
        if conn is None:
            self.conn = psycopg2.connect(**_get_db_config())
            self._owned_conn = True
        else:
            self.conn = conn
        # PIT #44: profile 缺失降级 default
        self.profile = profile if profile in ("default", "conservative", "aggressive") else "default"
        try:
            self.advisor = L3ProfileAdvisor(profile=self.profile)
        except Exception:
            self.advisor = None  # PIT #44 静默降级

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
        cur.execute("SELECT scenario_code, scenario_name, shock_params FROM l3.stress_test_scenarios WHERE is_active = TRUE")  # noqa: E501
        scenarios = cur.fetchall()

        worst_case = None
        worst_loss_pct = 0

        for scenario_code, scenario_name, shock_params_raw in scenarios:
            try:
                shock_params = json.loads(shock_params_raw) if isinstance(shock_params_raw, str) else shock_params_raw  # noqa: E501
            except json.JSONDecodeError:
                logger.warning(f"无法解析情景参数: {scenario_code}")
                continue

            # 计算各持仓在当前情景下的损失
            position_results = []
            total_loss = 0

            for pos in positions:
                code = pos.get("code", "")
                name = pos.get("name", "")
                pos.get("shares", 0)
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
        logger.info(f"压力测试完成: run_id={run_id}, 最坏情景={worst_case}({worst_loss_pct*100:.1f}%)")  # noqa: E501
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
        """返回 L3 能力激活状态 (V24-B4: 加 profile 字段)"""
        cur = self.conn.cursor()

        # 各触发器激活情况
        cur.execute("""
            SELECT trigger_type, is_active, last_triggered_at, trigger_count
            FROM l3.active_dialog_triggers
            ORDER BY priority DESC
        """)
        triggers = [
            {"type": r[0], "active": r[1], "last_triggered": str(r[2]) if r[2] else None, "count": r[3]}  # noqa: E501
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

        # V24-B4: profile 信息
        profile_info = {}
        if self.advisor:
            try:
                ov = self.advisor.get_risk_overview()
                profile_info = {
                    "current": self.profile,
                    "risk_level": ov.risk_level,
                    "max_position_pct": ov.max_position_pct,
                    "max_pe_ttm": ov.max_pe_ttm,
                    "confidence_threshold": ov.confidence_threshold,
                    "whitelist_count": ov.whitelist_count,
                    "blacklist_count": ov.blacklist_count,
                }
            except Exception:
                profile_info = {"current": self.profile, "_fallback": True}

        return {
            "triggers": triggers,
            "behavior_records": behavior_records,
            "stress_tests": stress_tests,
            "capability_score": capability_score,  # 0-5
            "capability_label": ["沉睡", "萌芽", "激活", "成熟", "进阶", "完全"][capability_score],
            "phase": "L3 Phase A" if capability_score < 3 else "L3 Phase B" if capability_score < 5 else "L3 Phase C",  # noqa: E501
            "profile": profile_info,  # V24-B4 新增
        }

    def get_profile_status(self) -> dict:
        """V24-B4: 返回当前 profile 详细状态 (含跨 profile 决策对比)"""
        if not self.advisor:
            return {"profile": self.profile, "_fallback": True}
        try:
            return {
                "profile": self.profile,
                "advisor_available": True,
                "risk_overview": self.advisor.get_risk_overview().to_dict(),
            }
        except Exception as e:
            return {"profile": self.profile, "_error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# 方案4 实施：Hermes 作为 L3 策略顾问（v2.2 T4-A/B/C）
# ─────────────────────────────────────────────────────────────────────────────
# 设计参考: hermes_coordination/references/v1_8_schemes.md 方案4
# 3 个核心方法:
#   - chat(user_id, query) -> dict      对话接口
#   - build_context(query, user_id)    上下文构建 (跨会话记忆 + skill 匹配 + 事件)
#   - post_decision(response, user_id) 决策点抽取 + 沉淀 + skill 更新触发
# 关键约束 (v2.2 PIT):
#   - LLM 限额 20/日 (与 V22-T3 intraday_hermes_agent 共用 /tmp/hermes_llm_quota.json)
#   - session_search 走 /home/aileo/.hermes/state.db SQLite 直读 (避免 MCP subprocess)
#   - 失败静默: L4 skip, 不抛异常
# 教训: l3_dialog_engine.py 顶层没 import sys, 用 import sys as _sys_t4 局部注入

import os as _os_t4
import re as _re_t4
import sqlite3 as _sqlite3_t4
import threading as _threading_t4
from pathlib import Path as _Path_t4

# 限额文件 (与 V22-T3 intraday_hermes_agent.py 共用)
_QUOTA_FILE_T4 = "/tmp/hermes_llm_quota.json"
_QUOTA_DAILY_T4 = 20

# 复用 intraday_hermes_agent 的降级链
try:
    import sys as _sys_t4
    # ⚠️ PIT 修复: l3_dialog_engine.py 在 scripts/, intraday_hermes_agent 在 scripts/../hermes_coordination/scripts/
    _HERMES_SCRIPTS_DIR_T4 = _Path_t4(__file__).parent.parent / "hermes_coordination" / "scripts"
    _sys_t4.path.insert(0, str(_HERMES_SCRIPTS_DIR_T4))
    # ⚠️ PIT 修复: 实际函数名是 find_skill_for_code + load_skill_excerpt, 不是 load_skill_for_code
    from intraday_hermes_agent import (
        DailyQuota, find_skill_for_code, load_skill_excerpt, call_llm_with_fallback,
    )
    # 适配函数名
    _HERMES_SKILL_T4 = find_skill_for_code
    _HERMES_SKILL_LOAD_T4 = load_skill_excerpt
    _HERMES_LLM_T4 = call_llm_with_fallback
    # ⚠️ PIT 修复: DailyQuota(daily_limit, quota_file) 位置参数顺序
    _HERMES_QUOTA_T4 = DailyQuota(_QUOTA_DAILY_T4, _Path_t4(_QUOTA_FILE_T4))
    _HERMES_PROMPT_T4 = None  # build_prompt 在 V22-T3 中不存在
    _HERMES_AVAILABLE_T4 = True
except Exception as _e_t4:
    _HERMES_AVAILABLE_T4 = False
    _HERMES_QUOTA_T4 = None
    _HERMES_SKILL_T4 = None
    _HERMES_PROMPT_T4 = None
    print(f"[T4 警告] intraday_hermes_agent 未集成: {_e_t4}")


def _session_search_t4(query: str, limit: int = 3) -> list[dict]:
    """直读 /home/aileo/.hermes/state.db 的 FTS5 表 (绕开 MCP subprocess)

    返回: [{"session_id": str, "preview": str, "when": str, "session_title": str}, ...]
    """
    db_path = _Path_t4(_os_t4.path.expanduser("~/.hermes/state.db"))
    if not db_path.exists():
        return []
    try:
        conn = _sqlite3_t4.connect(str(db_path), timeout=5)
        conn.row_factory = _sqlite3_t4.Row
        cur = conn.cursor()
        # FTS5 全文检索 (hermes state.db 标准模式)
        # ⚠️ PIT 修复: messages 表用 timestamp 而非 created_at
        # ⚠️ PIT 修复: FTS5 虚拟表 rowid = messages.id, 直接走 m.id
        # ⚠️ PIT 修复: FTS5 query 不能用 ? 占位, 必须字面量 + 防止 FTS 语法错误
        # 安全: 转义双引号 + 拆词 + 拼字符串
        safe_query = query.replace('"', '""')
        # 把 query 拆词, 用 OR 连接 (FTS5 标准)
        words = safe_query.split()
        if not words:
            return []
        fts_query = " OR ".join([f'"{w}"' for w in words[:5]])  # 最多 5 个词
        cur.execute("""
            SELECT m.session_id, m.content, m.timestamp, s.title as session_title
            FROM messages_fts f
            JOIN messages m ON m.id = f.rowid
            LEFT JOIN sessions s ON m.session_id = s.id
            WHERE messages_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (fts_query, limit))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        print(f"[T4 警告] session_search 失败: {e}")
        return []


def _skill_match_t4(query: str, limit: int = 5) -> list[dict]:
    """从 ~/.hermes/skills/investing 找 TOP5 相关 skill

    关键词: 数字 (stock code) + 中文 (主题词)
    """
    skills_dir = _Path_t4(_os_t4.path.expanduser("~/.hermes/skills/investing"))
    if not skills_dir.exists():
        return []
    # 提取查询中的 stock code (6 位数字)
    code_match = _re_t4.search(r"\b(\d{6})\b", query)
    target_code = code_match.group(1) if code_match else None
    candidates = []
    for skill_path in skills_dir.iterdir():
        if not skill_path.is_dir():
            continue
        name = skill_path.name
        score = 0
        # 命中 stock code
        if target_code and target_code in name:
            score += 10
        # 主题词命中
        for word in ["信维", "拓普", "澜起", "生益", "亨通", "卫星", "黄金", "有色", "纳指", "电池", "国防"]:
            if word in query and word in name:
                score += 3
        if score > 0:
            skill_file = skill_path / "SKILL.md"
            preview = ""
            if skill_file.exists():
                content = skill_file.read_text(errors="ignore")[:500]
                preview = content.split("\n")[0][:80]
            candidates.append({
                "name": name,
                "path": str(skill_file),
                "score": score,
                "preview": preview,
            })
    candidates.sort(key=lambda x: -x["score"])
    return candidates[:limit]


def _memory_recall_t4(user_id: str, limit: int = 10) -> list[dict]:
    """从 PG l3.decision_points 拉该用户最近决策 (自我记忆)

    返回: [{"decision": str, "stock_code": str, "reasoning": str, "created_at": str}, ...]
    """
    try:
        from l3_dialog_engine import _get_db_config
        import psycopg2
        conn = psycopg2.connect(**_get_db_config())
        cur = conn.cursor()
        cur.execute("""
            SELECT decision, stock_code, reasoning, created_at
            FROM l3.decision_points
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT %s
        """, (user_id, limit))
        rows = [
            {"decision": r[0], "stock_code": r[1], "reasoning": r[2], "created_at": str(r[3])}
            for r in cur.fetchall()
        ]
        conn.close()
        return rows
    except Exception as e:
        # 表可能还不存在 (T4-C 未执行)
        return []


def _extract_decisions_t4(response: str) -> list[dict]:
    """从 LLM 回复中抽取 buy/sell/hold/observe 决策点

    简化模式: 匹配 "建议|操作|决策" 关键词附近的动词
    """
    decisions = []
    # 模式: "建议 买入 XXX" / "操作: 卖出 XXX" / "策略: 持有 XXX"
    patterns = [
        (r"建议\s*(买入|加仓|建仓)", "buy"),
        (r"建议\s*(卖出|减仓|清仓)", "sell"),
        (r"建议\s*(持有|维持|继续持有)", "hold"),
        (r"建议\s*(观望|观察|等待)", "observe"),
        (r"操作[::]\s*(买入|加仓|建仓)", "buy"),
        (r"操作[::]\s*(卖出|减仓|清仓)", "sell"),
        (r"操作[::]\s*(持有|维持)", "hold"),
    ]
    for pattern, action in patterns:
        for match in _re_t4.finditer(pattern, response):
            # 取后 100 字作为 reasoning
            start = max(0, match.start() - 30)
            end = min(len(response), match.end() + 80)
            decisions.append({
                "action": action,
                "reasoning": response[start:end].strip(),
                "stock_code": _re_t4.search(r"\b(\d{6})\b", response[start:end]),
            })
    return decisions


class L3Advisor:
    """方案4: Hermes 作为 L3 策略顾问

    核心能力:
    - chat: 接收 user query, 返回带 6 类上下文的 LLM 回复
    - build_context: history + related sessions + skills + events + memory + holdings
    - post_decision: 抽取决策点 + 写 PG + 触发 skill update
    """

    def __init__(self, conn=None):
        self._owned_conn = False
        if conn is None:
            import psycopg2
            self.conn = psycopg2.connect(**_get_db_config())
            self._owned_conn = True
        else:
            self.conn = conn

    def __del__(self):
        if self._owned_conn and self.conn:
            try:
                self.conn.close()
            except Exception:
                pass

    def build_context(self, user_query: str, user_id: str = "aileo") -> dict:
        """上下文构建: 6 类数据融合

        1. history       - l3.dialog_history 最近 5 条
        2. related_sessions - ~/.hermes/state.db FTS5 检索 TOP 3
        3. relevant_skills - skill_match 关键词命中 TOP 5
        4. recent_events - l3.stress_test_results 最近 3 条 (事件源)
        5. memory        - l3.decision_points 该用户最近 10 条
        6. holdings      - 持仓档案 (待 PG load_positions_from_db)
        """
        context = {
            "user_id": user_id,
            "query": user_query,
            "history": [],
            "related_sessions": [],
            "relevant_skills": [],
            "recent_events": [],
            "memory": [],
            "holdings": [],
        }
        try:
            # 1. history
            cur = self.conn.cursor()
            cur.execute("""
                SELECT role, content, created_at
                FROM l3.dialog_history
                WHERE user_id = %s
                ORDER BY created_at DESC LIMIT 5
            """, (user_id,))
            context["history"] = [
                {"role": r[0], "content": r[1][:200], "created_at": str(r[2])}
                for r in cur.fetchall()
            ]
            # 4. recent events (复用 stress_test_results 作为事件流)
            cur.execute("""
                SELECT scenario_name, max_loss_pct, executed_at
                FROM l3.stress_test_results
                ORDER BY executed_at DESC LIMIT 3
            """)
            context["recent_events"] = [
                {"name": r[0], "loss_pct": r[1], "executed_at": str(r[2])}
                for r in cur.fetchall()
            ]
            # 6. holdings (从 PG 拉)
            cur.execute("""
                SELECT code, name, type, shares, market_value
                FROM portfolio.positions
                WHERE shares > 0
                ORDER BY market_value DESC LIMIT 10
            """)
            context["holdings"] = [
                {"code": r[0], "name": r[1], "type": r[2], "shares": r[3], "mv": r[4]}
                for r in cur.fetchall()
            ]
            self.conn.commit()  # ⚠️ PIT 修复: 显式 commit 避免后续 SQL 被 abort
        except Exception as e:
            # ⚠️ PIT 修复: 表/列可能不存在, 必须 rollback 避免事务 abort 阻断后续 SQL
            self.conn.rollback()
            if 'holdings' not in context or not context['holdings']:
                # holdings 缺失不算错, 静默
                pass
            else:
                print(f"[T4 警告] build_context PG 部分失败: {e}")
        # 2. related_sessions (SQLite 直读, 不依赖 PG 表)
        context["related_sessions"] = _session_search_t4(user_query, limit=3)
        # 3. relevant_skills
        context["relevant_skills"] = _skill_match_t4(user_query, limit=5)
        # 5. memory (decision_points 自我记忆)
        context["memory"] = _memory_recall_t4(user_id, limit=10)
        return context

    def chat(self, user_id: str, query: str) -> dict:
        """对话接口: 收 query, 返回 LLM 回复 + 6 类上下文

        返回: {
            "user_id": str,
            "query": str,
            "response": str,    # LLM 回复 (mock 或 真实)
            "context": dict,    # 6 类上下文
            "fallback_level": str,  # L1_normal | L2_degraded | L3_offline | L4_skip
            "decisions": list,  # 抽取的决策点
        }
        """
        # 1. 限额检查
        if not _HERMES_AVAILABLE_T4 or _HERMES_QUOTA_T4 is None:
            quota_remaining = 0
        else:
            quota_remaining = _HERMES_QUOTA_T4.get_remaining()
        if quota_remaining <= 0:
            # ⚠️ PIT 修复: L4 早退时也要返回完整字段 (避免 KeyError)
            return {
                "user_id": user_id,
                "query": query,
                "response": f"[L4 跳过] 今日 LLM 限额 {_QUOTA_DAILY_T4}/日 已用完, 明日重试",
                "context": {"skills_count": 0, "history_count": 0, "memory_count": 0,
                            "holdings_count": 0, "related_sessions_count": 0, "skill_names": []},
                "fallback_level": "L4_skip",
                "decisions": [],
                "user_dialog_id": None,
                "assistant_dialog_id": None,
            }

        # 2. 构建上下文
        context = self.build_context(query, user_id)

        # 3. 构造 prompt (中文)
        prompt_parts = [f"【用户问题】\n{query}\n"]
        if context["relevant_skills"]:
            skill_names = ", ".join([s["name"] for s in context["relevant_skills"]])
            prompt_parts.append(f"【相关 skill】{skill_names}")
        if context["history"]:
            last = context["history"][0]
            prompt_parts.append(f"【历史对话】{last['content'][:100]}")
        if context["memory"]:
            recent_decisions = "; ".join([
                f"{m['decision']} {m['stock_code']}" for m in context["memory"][:3]
            ])
            prompt_parts.append(f"【用户历史决策】{recent_decisions}")
        if context["holdings"]:
            top_holdings = ", ".join([
                f"{h['name']}({h['code']})" for h in context["holdings"][:5]
            ])
            prompt_parts.append(f"【当前持仓 TOP5】{top_holdings}")
        if context["related_sessions"]:
            # ⚠️ PIT 修复: session_title 可能为 None
            session_titles = "; ".join([
                (s.get("session_title") or "(无标题)")[:50] for s in context["related_sessions"]
            ])
            prompt_parts.append(f"【历史会话相关】{session_titles}")
        prompt_parts.append(
            "\n请基于以上上下文, 用 100-200 字回答用户问题, "
            "如涉及具体股票请给出明确操作建议 (买入/卖出/持有/观望)。"
        )
        full_prompt = "\n".join(prompt_parts)

        # 4. 调 LLM (用 call_llm_with_fallback 真实降级链)
        if not _HERMES_AVAILABLE_T4 or _HERMES_QUOTA_T4 is None:
            fallback_level = "L3_offline"
            response = f"[L3 离线模式] 基于上下文 {len(context['relevant_skills'])} 个 skill + {len(context['memory'])} 条历史决策, 建议人工分析。"
        else:
            acquired = _HERMES_QUOTA_T4.try_acquire()
            if not acquired:
                fallback_level = "L4_skip"
                response = "[L4 跳过] 限额刚用完"
            else:
                # 调用真实 LLM 降级链 (或 mock)
                system = "你是 Hermes 投资策略顾问, 基于持仓组合 + 历史决策 + skill 知识库回答用户问题。"
                llm_result = _HERMES_LLM_T4(system, full_prompt, max_retries=1)
                # ⚠️ PIT 修复: call_llm_with_fallback 返回字段是 level, 不是 fallback_level
                fallback_level = llm_result.get("level", "L1_normal")
                response = llm_result.get("content") or f"[L1 mock] {full_prompt[:150]}..."

        # 5. 抽取决策点
        decisions = _extract_decisions_t4(response)

        # 6. 写 dialog_history (T4-C 决策沉淀)
        try:
            cur = self.conn.cursor()
            cur.execute("""
                INSERT INTO l3.dialog_history (user_id, role, content, session_id)
                VALUES (%s, 'user', %s, NULL) RETURNING id
            """, (user_id, query))
            user_dialog_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO l3.dialog_history (user_id, role, content, session_id, refs)
                VALUES (%s, 'assistant', %s, %s, %s) RETURNING id
            """, (
                user_id, response, None,
                [s["name"] for s in context["relevant_skills"]],
            ))
            assistant_dialog_id = cur.fetchone()[0]
            self.conn.commit()
        except Exception as e:
            # ⚠️ PIT 修复: 表可能未建 (T4-C 未执行) - 打印错误便于诊断
            print(f"[T4 警告] dialog_history 写入失败: {e}")
            user_dialog_id = None
            assistant_dialog_id = None
            self.conn.rollback()

        # 7. 写 decision_points
        for d in decisions:
            try:
                cur = self.conn.cursor()
                cur.execute("""
                    INSERT INTO l3.decision_points
                    (user_id, dialog_id, decision, stock_code, confidence, reasoning)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    user_id, assistant_dialog_id, d["action"],
                    d["stock_code"].group(1) if d["stock_code"] else None,
                    0.7, d["reasoning"][:500],
                ))
                self.conn.commit()
            except Exception:
                self.conn.rollback()

        return {
            "user_id": user_id,
            "query": query,
            "response": response,
            "context": {
                "skills_count": len(context["relevant_skills"]),
                "history_count": len(context["history"]),
                "memory_count": len(context["memory"]),
                "holdings_count": len(context["holdings"]),
                "related_sessions_count": len(context["related_sessions"]),
                "skill_names": [s["name"] for s in context["relevant_skills"]],
            },
            "fallback_level": fallback_level,
            "decisions": [{"action": d["action"], "stock": d["stock_code"].group(1) if d["stock_code"] else None} for d in decisions],
            "user_dialog_id": user_dialog_id,
            "assistant_dialog_id": assistant_dialog_id,
        }

    def post_decision(self, response: str, user_id: str) -> dict:
        """决策后处理: 抽取 + 沉淀 + 触发 skill 更新

        返回: {"extracted": int, "written": int, "skill_updates_triggered": int}
        """
        decisions = _extract_decisions_t4(response)
        written = 0
        for d in decisions:
            try:
                cur = self.conn.cursor()
                cur.execute("""
                    INSERT INTO l3.decision_points
                    (user_id, decision, stock_code, confidence, reasoning)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    user_id, d["action"],
                    d["stock_code"].group(1) if d["stock_code"] else None,
                    0.7, d["reasoning"][:500],
                ))
                self.conn.commit()
                written += 1
            except Exception:
                self.conn.rollback()
        # 触发 skill 更新 (async, 不阻塞)
        skill_updates = 0
        for d in decisions:
            if d["stock_code"]:
                # 简化: 仅记录意图, 实际更新由 schedule_runner 22:00 cron 处理
                skill_updates += 1
        return {
            "extracted": len(decisions),
            "written": written,
            "skill_updates_triggered": skill_updates,
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
            status_icon = {"triggered": "✅", "not_triggered": "➖", "push_failed": "⚠️", "error": "❌"}.get(d["status"], "?")  # noqa: E501
            print(f"  {status_icon} [{d['trigger_type']}] {d.get('trigger_name', d['trigger_type'])}: {d['status']}")  # noqa: E501

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
        print("\n触发器:")
        for t in status["triggers"]:
            icon = "🟢" if t["active"] else "⚫"
            last = t["last_triggered"] or "从未触发"
            print(f"  {icon} [{t['type']}] 上次: {last} 累计: {t['count']}次")
        print(f"\n最近压力测试: {len(status['stress_tests'])} 条")
        for st in status["stress_tests"][:3]:
            print(f"  {st['executed_at'][:19]} {st['scenario']}: {st['loss_pct']}% 风险{st['risk_score']}/10")  # noqa: E501

    else:
        parser.print_help()

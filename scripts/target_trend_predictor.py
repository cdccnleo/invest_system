"""
target_trend_predictor.py — 标的级趋势预测器

基于单标的的历史数据，建立趋势预测信号。
不与全局模型混淆——每个标的独立建模。

核心能力:
  1. predict_earnings_surprise_probability() — 季报超预期概率预测
  2. detect_divergence_signals()          — 多维度信号背离检测
  3. assess_risk_escalation()             — 风险升级趋势评估
  4. generate_valuation_context()         — 估值背景生成
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import psycopg2

logger = logging.getLogger("target_trend_predictor")

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
CREDENTIAL_STORE = Path.home() / ".hermes" / "invest_credentials" / "store.json"


def _get_db_conn():
    """获取数据库连接"""
    with open(CREDENTIAL_STORE) as f:
        creds = json.load(f)
    return psycopg2.connect(
        host="localhost", port=5432,
        dbname="investpilot", user="invest_admin",
        password=creds["DB_PASSWORD"]
    )


class TargetTrendPredictor:
    """
    基于该标的的历史数据，建立趋势预测信号。
    不与全局模型混淆——每个标的独立建模。
    """

    def __init__(self, ts_code: str):
        self.ts_code = ts_code

    # ═══════════════════════════════════════════════════════════
    # 方法 1：季报超预期概率预测
    # ═══════════════════════════════════════════════════════════

    def predict_earnings_surprise_probability(self) -> dict:
        """
        基于历史季报超预期/低预期的模式，预测下次季报超预期的概率。
        输入：历史财务趋势 + 分析师一致预期变动
        输出：{surprise_direction, probability, confidence, reasoning}
        路由：DeepSeek API（复杂推理）
        """
        conn = _get_db_conn()
        cur = conn.cursor()

        # 1. 取最近 8 季度财务数据
        cur.execute("""
            SELECT report_date, total_revenue, net_profit, yoy_growth, profit_growth
            FROM market.financial_indicators
            WHERE ts_code = %s
            ORDER BY report_date DESC
            LIMIT 8
        """, (self.ts_code,))
        fin_rows = cur.fetchall()

        if len(fin_rows) < 3:
            conn.close()
            return {
                "surprise_direction": "unknown",
                "probability": 0.0,
                "confidence": "low",
                "reasoning": "历史财务数据不足（<3个季度），无法建立预测基线",
                "data_points": len(fin_rows),
            }

        # 2. 计算增长趋势
        yoy_values = [r[3] for r in fin_rows if r[3] is not None]
        profit_values = [r[2] for r in fin_rows if r[2] is not None]

        yoy_trend = "加速" if len(yoy_values) >= 2 and yoy_values[0] and yoy_values[1] and yoy_values[0] > yoy_values[1] else \
                    "减速" if len(yoy_values) >= 2 and yoy_values[0] and yoy_values[1] and yoy_values[0] < yoy_values[1] else \
                    "稳定"

        # 3. 判断超预期概率（基于营收/利润增速动量）
        profit_accelerating = False
        if len(profit_values) >= 3 and all(v is not None for v in profit_values[:3]):
            qoq_changes = [(profit_values[i] - profit_values[i+1]) / abs(profit_values[i+1])
                           for i in range(len(profit_values[:3]) - 1) if profit_values[i+1] != 0]
            profit_accelerating = all(c > 0 for c in qoq_changes)

        # 4. 研报评级趋势（机构预期一致性）
        cur.execute("""
            SELECT rating, report_date
            FROM research.research_reports
            WHERE ts_code = %s AND report_date >= CURRENT_DATE - INTERVAL '180 days'
            ORDER BY report_date DESC
            LIMIT 10
        """, (self.ts_code[:6],))
        report_rows = cur.fetchall()
        conn.close()

        # 评级变化分析
        rating_weights = {"买入": 5, "增持": 4, "中性": 3, "减持": 2, "卖出": 1}
        ratings = [r[0] for r in report_rows if r[0] in rating_weights]
        avg_rating = sum(rating_weights.get(r, 3) for r in ratings) / len(ratings) if ratings else 3.0

        # 综合判断
        if profit_accelerating and avg_rating >= 4.0:
            direction = "upside"
            prob = 0.65 + (avg_rating - 4.0) * 0.1
            reasoning = f"利润增速动量加速（{yoy_trend}）+ 机构评级偏积极（均分{avg_rating:.1f}/5）"
        elif not profit_accelerating and avg_rating <= 3.0:
            direction = "downside"
            prob = 0.55 + (3.0 - avg_rating) * 0.1
            reasoning = f"利润增速动量减速 + 机构评级偏保守（均分{avg_rating:.1f}/5）"
        else:
            direction = "neutral"
            prob = 0.45
            reasoning = f"增长趋势{yoy_trend}，机构评级中性（均分{avg_rating:.1f}/5），无明确方向"

        confidence = "high" if len(fin_rows) >= 6 and len(ratings) >= 3 else "medium" if len(fin_rows) >= 4 else "low"

        return {
            "surprise_direction": direction,
            "probability": round(min(prob, 0.85), 2),
            "confidence": confidence,
            "reasoning": reasoning,
            "data_points": len(fin_rows),
            "yoy_trend": yoy_trend,
            "avg_rating": round(avg_rating, 1),
        }

    # ═══════════════════════════════════════════════════════════
    # 方法 2：多维度信号背离检测
    # ═══════════════════════════════════════════════════════════

    def detect_divergence_signals(self) -> list[dict]:
        """
        检测多维度信号背离：
        - 股价上涨但基本面恶化
        - 研报评级上调但新闻情感转负
        - 技术面突破但成交量萎缩
        返回：[{divergence_type, description, severity, suggested_action}]
        """
        divergences = []
        conn = _get_db_conn()
        cur = conn.cursor()
        code = self.ts_code[:6]

        # ── 背离 1：股价 vs 基本面 ──────────────────────────
        cur.execute("""
            SELECT close_price, change_pct, trade_date
            FROM market.daily_quotes
            WHERE ts_code = %s
            ORDER BY trade_date DESC
            LIMIT 30
        """, (self.ts_code,))
        quote_rows = cur.fetchall()

        cur.execute("""
            SELECT total_revenue, net_profit, roe, report_date
            FROM market.financial_indicators
            WHERE ts_code = %s
            ORDER BY report_date DESC
            LIMIT 4
        """, (self.ts_code,))
        fin_rows = cur.fetchall()

        if len(quote_rows) >= 20 and len(fin_rows) >= 2:
            # 计算近期涨幅
            recent_close = quote_rows[0][0] if quote_rows[0][0] else 0
            ago20_close = quote_rows[min(19, len(quote_rows)-1)][0] if quote_rows[min(19, len(quote_rows)-1)][0] else 0
            price_change_20d = ((recent_close - ago20_close) / ago20_close * 100) if ago20_close else 0

            # 检查最近两个季度基本面
            profit_recent = fin_rows[0][1] or 0
            profit_prev = fin_rows[1][1] or 0
            roe_recent = fin_rows[0][2] or 0
            roe_prev = fin_rows[1][2] or 0
            profit_change = ((profit_recent - profit_prev) / abs(profit_prev) * 100) if profit_prev else 0
            roe_change = roe_recent - roe_prev if roe_recent and roe_prev else 0

            if price_change_20d > 5 and profit_change < -10:
                divergences.append({
                    "divergence_type": "price_vs_fundamental",
                    "description": f"近20日股价上涨{price_change_20d:.1f}%但最新季度净利润下降{abs(profit_change):.1f}%",
                    "severity": "high",
                    "suggested_action": "关注是否为出货拉高，建议减仓观察",
                })
            elif price_change_20d < -5 and profit_change > 10:
                divergences.append({
                    "divergence_type": "price_vs_fundamental",
                    "description": f"近20日股价下跌{abs(price_change_20d):.1f}%但最新季度净利润增长{profit_change:.1f}%",
                    "severity": "medium",
                    "suggested_action": "杀估值而非基本面恶化，可关注抄底机会",
                })

        # ── 背离 2：研报 vs 新闻情感 ──────────────────────────
        cur.execute("""
            SELECT rating, report_date
            FROM research.research_reports
            WHERE ts_code = %s AND report_date >= CURRENT_DATE - INTERVAL '60 days'
            ORDER BY report_date DESC
            LIMIT 5
        """, (code,))
        report_rows = cur.fetchall()

        cur.execute("""
            SELECT sentiment, title, published_at
            FROM research.news_articles
            WHERE stock_codes @> %s::jsonb AND published_at >= CURRENT_DATE - INTERVAL '30 days'
            ORDER BY published_at DESC
            LIMIT 10
        """, (json.dumps([code]),))
        news_rows = cur.fetchall()

        if report_rows and news_rows:
            rating_negative_map = {"减持": -1, "卖出": -1}
            rating_positive_map = {"买入": 1, "增持": 1}
            recent_ratings = [r[0] for r in report_rows[:2]]
            avg_rating_score = sum(rating_positive_map.get(r, 0) + rating_negative_map.get(r, 0) for r in recent_ratings)

            sentiment_pos_count = sum(1 for n in news_rows if n[0] in ("positive", "POSITIVE"))
            sentiment_neg_count = sum(1 for n in news_rows if n[0] in ("negative", "NEGATIVE"))
            sentiment_score = sentiment_pos_count - sentiment_neg_count

            if avg_rating_score > 0 and sentiment_score < -2:
                divergences.append({
                    "divergence_type": "rating_vs_news",
                    "description": f"近2份研报偏正面但近30天新闻情感偏负面（正面{sentiment_pos_count} vs 负面{sentiment_neg_count}）",
                    "severity": "medium",
                    "suggested_action": "研报可能有滞后，优先关注负面新闻中是否有实质性利空",
                })

        # ── 背离 3：价格突破 vs 成交量萎缩 ──────────────────
        if len(quote_rows) >= 10:
            last5 = [(r[0], r[1]) for r in quote_rows[:5]]
            prev5 = [(r[0], r[1]) for r in quote_rows[5:10]]

            # 价格方向
            avg_last5_close = sum(r[0] for r in last5 if r[0]) / max(sum(1 for r in last5 if r[0]), 1)
            avg_prev5_close = sum(r[0] for r in prev5 if r[0]) / max(sum(1 for r in prev5 if r[0]), 1)

            # 成交量方向
            avg_last5_vol = sum(abs(r[1]) for r in last5 if r[1]) / max(sum(1 for r in last5 if r[1]), 1)
            avg_prev5_vol = sum(abs(r[1]) for r in prev5 if r[1]) / max(sum(1 for r in prev5 if r[1]), 1)

            price_up = avg_last5_close > avg_prev5_close * 1.02
            vol_down = avg_last5_vol < avg_prev5_vol * 0.85

            if price_up and vol_down:
                divergences.append({
                    "divergence_type": "price_breakout_no_volume",
                    "description": "近5日价格上涨但成交量较前5日萎缩15%以上，突破缺乏量能配合",
                    "severity": "medium",
                    "suggested_action": "量价背离，突破有效性存疑，追高需谨慎",
                })

        conn.close()
        return divergences

    # ═══════════════════════════════════════════════════════════
    # 方法 3：风险升级趋势评估
    # ═══════════════════════════════════════════════════════════

    def assess_risk_escalation(self) -> dict:
        """
        评估风险升级趋势：
        - 负面新闻累积加速
        - 公告中风险提示频率增加
        - 机构评级连续下调
        返回：{risk_level(1-10), escalation_trend, key_risk_factors, recommended_response}
        """
        risk_score = 0
        risk_factors = []
        conn = _get_db_conn()
        cur = conn.cursor()
        code = self.ts_code[:6]

        # ── 因子 1：负面新闻累积 ────────────────────────────
        cur.execute("""
            SELECT sentiment, published_at
            FROM research.news_articles
            WHERE stock_codes @> %s::jsonb AND published_at >= CURRENT_DATE - INTERVAL '90 days'
            ORDER BY published_at DESC
        """, (json.dumps([code]),))
        news_all = cur.fetchall()

        if news_all:
            total_news = len(news_all)
            neg_news = sum(1 for n in news_all if n[0] in ("negative", "NEGATIVE"))
            neg_ratio = neg_news / total_news if total_news > 0 else 0

            # 按30天分桶检查是否加速
            recent30_neg = sum(1 for n in news_all
                              if n[0] in ("negative", "NEGATIVE") and n[1]
                              and n[1] >= datetime.now() - timedelta(days=30))
            earlier30_neg = sum(1 for n in news_all
                               if n[0] in ("negative", "NEGATIVE") and n[1]
                               and n[1] < datetime.now() - timedelta(days=30)
                               and n[1] >= datetime.now() - timedelta(days=60))

            if neg_ratio > 0.4:
                risk_score += 3
                risk_factors.append(f"近90天负面新闻占比{neg_ratio:.0%}（共{neg_news}/{total_news}条）")
            elif neg_ratio > 0.25:
                risk_score += 2
                risk_factors.append(f"近90天负面新闻占比{neg_ratio:.0%}")

            if recent30_neg > earlier30_neg * 1.5:
                risk_score += 1
                risk_factors.append(f"负面新闻加速累积（近30天{recent30_neg}条 vs 前30天{earlier30_neg}条）")

        # ── 因子 2：机构评级连续下调 ─────────────────────────
        cur.execute("""
            SELECT rating, report_date, source
            FROM research.research_reports
            WHERE ts_code = %s AND report_date >= CURRENT_DATE - INTERVAL '180 days'
            ORDER BY report_date DESC
        """, (code,))
        report_all = cur.fetchall()

        if len(report_all) >= 3:
            rating_rank = {"买入": 5, "增持": 4, "中性": 3, "减持": 2, "卖出": 1}
            rating_scores = [rating_rank.get(r[0], 3) for r in report_all[:5]]

            # 检测连续下调
            downgrade_count = 0
            for i in range(len(rating_scores) - 1):
                if rating_scores[i] < rating_scores[i + 1]:
                    downgrade_count += 1

            if downgrade_count >= 2:
                risk_score += 3
                risk_factors.append(f"近半年出现{downgrade_count}次评级下调")
            elif downgrade_count >= 1:
                risk_score += 1
                risk_factors.append(f"近半年出现{downgrade_count}次评级下调")

        # ── 因子 3：公告风险提示 ─────────────────────────────
        cur.execute("""
            SELECT title, notice_date
            FROM research.announcements
            WHERE ts_code = %s AND (
                title ILIKE '%风险%' OR title ILIKE '%警示%' OR title ILIKE '%退市%'
                OR title ILIKE '%亏损%' OR title ILIKE '%处罚%' OR title ILIKE '%监管%'
            )
              AND notice_date >= CURRENT_DATE - INTERVAL '180 days'
            ORDER BY notice_date DESC
        """, (code,))
        risk_anns = cur.fetchall()

        if len(risk_anns) >= 3:
            risk_score += 3
            risk_factors.append(f"近半年有{len(risk_anns)}条风险相关公告")
        elif len(risk_anns) >= 1:
            risk_score += 1
            risk_factors.append(f"近半年有{len(risk_anns)}条风险相关公告")

        # ── 因子 4：股价距高点回撤 ───────────────────────────
        cur.execute("""
            SELECT close_price, trade_date
            FROM market.daily_quotes
            WHERE ts_code = %s
            ORDER BY trade_date DESC
            LIMIT 60
        """, (self.ts_code,))
        price_rows = cur.fetchall()

        if len(price_rows) >= 20:
            prices = [p[0] for p in price_rows if p[0]]
            high_60d = max(prices)
            current = prices[0]
            drawdown = (current - high_60d) / high_60d * 100 if high_60d else 0

            if drawdown < -20:
                risk_score += 3
                risk_factors.append(f"距近60日高点回撤{abs(drawdown):.1f}%")
            elif drawdown < -10:
                risk_score += 1
                risk_factors.append(f"距近60日高点回撤{abs(drawdown):.1f}%")

        conn.close()

        # ── 综合评估 ────────────────────────────────────────
        risk_level = min(risk_score, 10)
        if risk_score >= 7:
            escalation_trend = "accelerating"
            recommended_response = "强烈建议减仓或止损，风险正在加速累积"
        elif risk_score >= 4:
            escalation_trend = "elevated"
            recommended_response = "建议降低仓位，密切关注后续公告和新闻"
        elif risk_score >= 2:
            escalation_trend = "stable_watch"
            recommended_response = "风险水平可控，保持跟踪即可"
        else:
            escalation_trend = "stable"
            recommended_response = "当前无明显风险升级信号"

        return {
            "risk_level": risk_level,
            "escalation_trend": escalation_trend,
            "key_risk_factors": risk_factors if risk_factors else ["无明显风险因子"],
            "recommended_response": recommended_response,
        }

    # ═══════════════════════════════════════════════════════════
    # 方法 4：估值背景生成
    # ═══════════════════════════════════════════════════════════

    def generate_valuation_context(self) -> dict:
        """
        生成估值背景：
        - 当前PE/PB在历史分位中的位置
        - 相对同行业估值折溢价
        - 相对自身历史估值中枢偏离度
        返回：{current_valuation, historical_percentile, sector_comparison, overvalued/undervalued_flag}
        """
        conn = _get_db_conn()
        cur = conn.cursor()

        # ── 1. 最新估值数据 ──────────────────────────────────
        cur.execute("""
            SELECT pe_ttm, pb, total_mv, circ_mv
            FROM market.financial_indicators
            WHERE ts_code = %s
            ORDER BY report_date DESC
            LIMIT 1
        """, (self.ts_code,))
        val_row = cur.fetchone()

        current_pe = float(val_row[0]) if val_row and val_row[0] else None
        current_pb = float(val_row[1]) if val_row and val_row[1] else None
        current_mv = float(val_row[2]) if val_row and val_row[2] else None

        # ── 2. 计算历史估值分位 ──────────────────────────────
        cur.execute("""
            SELECT pe_ttm, pb
            FROM market.financial_indicators
            WHERE ts_code = %s AND pe_ttm > 0
            ORDER BY report_date DESC
            LIMIT 20
        """, (self.ts_code,))
        hist_rows = cur.fetchall()

        pe_percentile = None
        pb_percentile = None
        pe_median = None
        pb_median = None

        if hist_rows and current_pe:
            pe_values = sorted([float(r[0]) for r in hist_rows if r[0]])
            if pe_values:
                pe_median = pe_values[len(pe_values) // 2]
                below_count = sum(1 for v in pe_values if v <= current_pe)
                pe_percentile = round(below_count / len(pe_values) * 100, 1)

        if hist_rows and current_pb:
            pb_values = sorted([float(r[1]) for r in hist_rows if r[1]])
            if pb_values:
                pb_median = pb_values[len(pb_values) // 2]
                below_count = sum(1 for v in pb_values if v <= current_pb)
                pb_percentile = round(below_count / len(pb_values) * 100, 1)

        # ── 3. 同行业估值对比 ────────────────────────────────
        sector_comparison = {}
        try:
            cur.execute("""
                SELECT fi.ts_code, fi.pe_ttm, fi.pb
                FROM market.financial_indicators fi
                WHERE fi.ts_code LIKE CONCAT(SUBSTRING(%s FROM 1 FOR 3), '%%')
                  AND fi.pe_ttm > 0 AND fi.pb > 0
                  AND fi.report_date >= CURRENT_DATE - INTERVAL '120 days'
                LIMIT 30
            """, (self.ts_code,))
            peer_rows = cur.fetchall()

            if peer_rows:
                peer_pe = sorted([float(r[1]) for r in peer_rows if r[1]])
                peer_pb = sorted([float(r[2]) for r in peer_rows if r[2]])
                sector_avg_pe = sum(peer_pe) / len(peer_pe) if peer_pe else None
                sector_avg_pb = sum(peer_pb) / len(peer_pb) if peer_pb else None
                sector_median_pe = peer_pe[len(peer_pe) // 2] if peer_pe else None
                sector_median_pb = peer_pb[len(peer_pb) // 2] if peer_pb else None

                sector_comparison = {
                    "peer_count": len(peer_rows),
                    "sector_avg_pe": round(sector_avg_pe, 2) if sector_avg_pe else None,
                    "sector_avg_pb": round(sector_avg_pb, 2) if sector_avg_pb else None,
                    "sector_median_pe": round(sector_median_pe, 2) if sector_median_pe else None,
                    "sector_median_pb": round(sector_median_pb, 2) if sector_median_pb else None,
                    "pe_vs_sector": round((current_pe - sector_median_pe) / sector_median_pe * 100, 1)
                                    if current_pe and sector_median_pe else None,
                    "pb_vs_sector": round((current_pb - sector_median_pb) / sector_median_pb * 100, 1)
                                    if current_pb and sector_median_pb else None,
                }
        except Exception as e:
            logger.debug(f"同行业估值对比查询失败: {e}")

        conn.close()

        # ── 4. 综合判断 ──────────────────────────────────────
        overvalued_count = 0
        undervalued_count = 0

        if pe_percentile and pe_percentile > 80:
            overvalued_count += 1
        elif pe_percentile and pe_percentile < 20:
            undervalued_count += 1
        if pb_percentile and pb_percentile > 80:
            overvalued_count += 1
        elif pb_percentile and pb_percentile < 20:
            undervalued_count += 1

        valuation_flag = "neutral"
        if overvalued_count >= 2:
            valuation_flag = "overvalued"
        elif undervalued_count >= 2:
            valuation_flag = "undervalued"
        elif overvalued_count >= 1:
            valuation_flag = "slightly_overvalued"
        elif undervalued_count >= 1:
            valuation_flag = "slightly_undervalued"

        return {
            "current_valuation": {
                "pe_ttm": round(current_pe, 2) if current_pe else None,
                "pb": round(current_pb, 2) if current_pb else None,
                "total_mv": round(current_mv / 1e8, 2) if current_mv else None,
            },
            "historical_percentile": {
                "pe_percentile": pe_percentile,
                "pb_percentile": pb_percentile,
                "pe_median": round(pe_median, 2) if pe_median else None,
                "pb_median": round(pb_median, 2) if pb_median else None,
            },
            "sector_comparison": sector_comparison if sector_comparison else {"note": "同行业比对数据暂缺"},
            "valuation_flag": valuation_flag,
        }


# ─── 便捷入口：为 TAMF 更新提供统一调用 ─────────────────────────

def run_trend_prediction_for_target(ts_code: str) -> dict:
    """
    为单个标的运行全部4项趋势预测，供 TAMF 增量更新和深度分析调用。
    返回包含全部预测结果的字典。
    """
    predictor = TargetTrendPredictor(ts_code)

    return {
        "ts_code": ts_code,
        "run_time": datetime.now().isoformat(),
        "earnings_surprise": predictor.predict_earnings_surprise_probability(),
        "divergence_signals": predictor.detect_divergence_signals(),
        "risk_escalation": predictor.assess_risk_escalation(),
        "valuation_context": predictor.generate_valuation_context(),
    }


def build_tamf_trend_section(ts_code: str, name: str = "") -> str:
    """
    生成 TAMF 文件可嵌入的趋势预测 Markdown 段落。
    用于在 TAMF 第三章尾部追加趋势预测和估值背景。
    """
    result = run_trend_prediction_for_target(ts_code)
    display_name = f"{name}（{ts_code}）" if name else ts_code

    sections = []

    # 季报超预期预测
    es = result["earnings_surprise"]
    direction_icon = {"upside": "🟢", "downside": "🔴", "neutral": "🟡", "unknown": "⚪"}
    sections.append(f"""### 季报超预期预测
| 方向 | 概率 | 置信度 | 判断依据 |
|:---:|:---:|:---:|------|
| {direction_icon.get(es['surprise_direction'], '⚪')} {es['surprise_direction']} | {es['probability']:.0%} | {es['confidence']} | {es.get('reasoning', '—')} |
""")

    # 背离检测
    divs = result["divergence_signals"]
    if divs:
        div_lines = "\n".join([
            f"| {d['divergence_type']} | {'🔴' if d['severity'] == 'high' else '🟡'} {d['severity']} | {d['description'][:60]} | {d['suggested_action'][:40]} |"
            for d in divs
        ])
        sections.append(f"""### 信号背离检测
| 背离类型 | 严重程度 | 描述 | 建议 |
|---------|:---:|------|------|
{div_lines}
""")
    else:
        sections.append("""### 信号背离检测
✅ 暂无多维度信号背离
""")

    # 风险升级评估
    risk = result["risk_escalation"]
    risk_bar = "█" * risk["risk_level"] + "░" * (10 - risk["risk_level"])
    risk_factors = "\n".join([f"- {f}" for f in risk["key_risk_factors"]])
    trend_icon = {"accelerating": "🔴 加速", "elevated": "🟡 上升", "stable_watch": "🟢 关注", "stable": "🟢 稳定"}
    sections.append(f"""### 风险升级评估
| 维度 | 值 |
|------|-----|
| 风险等级 | {risk_bar} {risk['risk_level']}/10 |
| 升级趋势 | {trend_icon.get(risk['escalation_trend'], risk['escalation_trend'])} |

**风险因子**:
{risk_factors}

**建议应对**: {risk['recommended_response']}
""")

    # 估值背景
    val = result["valuation_context"]
    cv = val.get("current_valuation", {})
    hp = val.get("historical_percentile", {})
    sc = val.get("sector_comparison", {})

    sections.append(f"""### 估值背景
| 维度 | 值 |
|------|-----|
| PE(TTM) | {cv.get('pe_ttm', '—')} |
| PB | {cv.get('pb', '—')} |
| PE 历史分位 | {hp.get('pe_percentile', '—')}%（中位数 {hp.get('pe_median', '—')}） |
| PB 历史分位 | {hp.get('pb_percentile', '—')}%（中位数 {hp.get('pb_median', '—')}） |
| 综合判断 | {val.get('valuation_flag', '—')} |
""")

    header = f"\n\n---\n\n## 趋势预测与估值（AI 驱动 — {datetime.now().strftime('%Y-%m-%d %H:%M')}）\n\n"
    return header + "\n".join(sections)


# ─── 主入口 ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        ts_code = sys.argv[1]
    else:
        ts_code = "000977.XSHE"

    print(f"=== {ts_code} 趋势预测 ===")
    result = run_trend_prediction_for_target(ts_code)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    print("\n=== TAMF 段落预览 ===\n")
    print(build_tamf_trend_section(ts_code, "浪潮信息"))
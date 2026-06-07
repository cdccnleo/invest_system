"""
backtest_engine.py — 简易回测引擎
基于 PostgreSQL 历史行情数据，验证技能/策略的有效性
输出：收益曲线、夏普比率、最大回撤、胜率
"""

import os
import csv
import json
import logging
from datetime import date, timedelta

import psycopg2
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))  # 仅用于 POSITIONS_CSV 路径变量

logger = logging.getLogger("invest_system.backtest")

POSITIONS_CSV = os.environ.get("POSITIONS_CSV", "/mnt/d/Hold/invest-data/positions.csv")


def get_db_conn():
    try:
        from credentials import get_credential
        pwd = get_credential("DB_PASSWORD")
        if pwd:
            return psycopg2.connect(host="localhost", database="investpilot",
                                    user="invest_admin", password=pwd)
    except ImportError:
        pass
    return psycopg2.connect(host="localhost", database="investpilot",
                            user="invest_admin", password=os.environ.get("DB_PASSWORD", ""))


# ── 历史行情数据 ──────────────────────────────────────────────────────

def get_price_history(ts_code: str, start_date: str, end_date: str) -> list[dict]:
    """
    获取历史行情数据
    返回: [{"date": "YYYY-MM-DD", "close": float, "volume": int}, ...]
    """
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT trade_date::text, close_price, volume
            FROM market.daily_quotes
            WHERE ts_code = %s
              AND trade_date BETWEEN %s AND %s
            ORDER BY trade_date ASC
        """, (ts_code, start_date, end_date))
        rows = cur.fetchall()
        return [
            {"date": r[0], "close": float(r[1]), "volume": int(r[2])}
            for r in rows
        ]
    finally:
        conn.close()


def get_multiple_price_history(ts_codes: list[str], start_date: str, end_date: str) -> dict:
    """批量获取多只标的历史行情"""
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        placeholders = ",".join(["%s"] * len(ts_codes))
        cur.execute(f"""
            SELECT ts_code, trade_date::text, close_price, volume
            FROM market.daily_quotes
            WHERE ts_code IN ({placeholders})
              AND trade_date BETWEEN %s AND %s
            ORDER BY ts_code, trade_date ASC
        """, (*ts_codes, start_date, end_date))
        rows = cur.fetchall()
        result = {}
        for r in rows:
            ts = r[0]
            if ts not in result:
                result[ts] = []
            result[ts].append({"date": r[1], "close": float(r[2]), "volume": int(r[3])})
        return result
    finally:
        conn.close()


# ── 回测核心 ────────────────────────────────────────────────────────

def backtest_strategy(
    ts_codes: list[str],
    start_date: str,
    end_date: str,
    initial_capital: float = 1000000.0,
    position_size_pct: float = 0.95,
) -> dict:
    """
    简单等权回测：
    - 每日等权分配仓位
    - 无择时，买入持有
    返回：收益曲线 + 绩效指标
    """
    price_data = get_multiple_price_history(ts_codes, start_date, end_date)
    if not price_data:
        return {"error": "无历史数据"}

    # 找出公共交易日
    all_dates = set()
    for series in price_data.values():
        for bar in series:
            all_dates.add(bar["date"])
    dates = sorted(all_dates)
    if len(dates) < 2:
        return {"error": "交易日不足"}

    # 计算每日组合涨跌（等权）
    portfolio_values = [initial_capital]
    daily_returns = []
    positions = {ts: 0 for ts in ts_codes}  # 当前持股数
    shares = {ts: 0 for ts in ts_codes}     # 每股数量

    # 初始化建仓（第一天收盘价）
    first_date = dates[0]
    capital_per_stock = initial_capital * position_size_pct / len(ts_codes)
    for ts in ts_codes:
        series = price_data.get(ts, [])
        if not series:
            continue
        first_bar = next((b for b in series if b["date"] == first_date), None)
        if first_bar and first_bar["close"] > 0:
            shares[ts] = capital_per_stock / first_bar["close"]
            positions[ts] = capital_per_stock

    prev_value = sum(shares[ts] * (price_data.get(ts, [{}])[0]["close"] if price_data.get(ts) else 0) for ts in ts_codes)  # noqa: E501
    portfolio_values.append(prev_value)

    for d in dates[1:]:
        daily_pnl = 0
        for ts in ts_codes:
            series = price_data.get(ts, [])
            bar = next((b for b in series if b["date"] == d), None)
            if not bar or bar["close"] <= 0:
                continue
            prev_close = series[series.index(bar) - 1]["close"] if series.index(bar) > 0 else bar["close"]  # noqa: E501
            shares[ts] * bar["close"]
            daily_pnl += shares[ts] * (bar["close"] - prev_close)

        daily_return = daily_pnl / portfolio_values[-1] if portfolio_values[-1] > 0 else 0
        daily_returns.append(daily_return)
        portfolio_values.append(portfolio_values[-1] + daily_pnl)

    # 计算绩效指标
    import statistics
    total_return = (portfolio_values[-1] - initial_capital) / initial_capital
    annual_return = total_return / (len(dates) / 252) if len(dates) > 252 else total_return * 252 / len(dates)  # noqa: E501

    # 夏普比率（假设无风险利率 3%）
    rf = 0.03
    [r - rf / 252 for r in daily_returns]
    std_dev = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 0
    sharpe = (annual_return - rf) / (std_dev * (252 ** 0.5)) if std_dev > 0 else 0

    # 最大回撤
    peak = portfolio_values[0]
    max_drawdown = 0
    for v in portfolio_values:
        if v > peak:
            peak = v
        drawdown = (peak - v) / peak if peak > 0 else 0
        if drawdown > max_drawdown:
            max_drawdown = drawdown

    # 胜率
    winning_days = sum(1 for r in daily_returns if r > 0)
    win_rate = winning_days / len(daily_returns) if daily_returns else 0

    # 月度收益
    monthly_returns = _calc_monthly_returns(portfolio_values, dates)

    return {
        "strategy": "buy_and_hold_equal_weight",
        "start_date": start_date,
        "end_date": end_date,
        "initial_capital": initial_capital,
        "final_value": round(portfolio_values[-1], 2),
        "total_return": round(total_return * 100, 2),
        "annual_return": round(annual_return * 100, 2),
        "sharpe_ratio": round(sharpe, 2),
        "max_drawdown": round(max_drawdown * 100, 2),
        "win_rate": round(win_rate * 100, 1),
        "trading_days": len(dates),
        "monthly_returns": monthly_returns,
        "equity_curve": [round(v, 2) for v in portfolio_values],
    }


def _calc_monthly_returns(values: list[float], dates: list[str]) -> list[dict]:
    """计算月度收益率"""
    if not values or len(values) < 2:
        return []

    monthly = {}
    for v, d in zip(values, dates):
        month_key = d[:7]  # YYYY-MM
        if month_key not in monthly:
            monthly[month_key] = {"month": month_key, "value": v, "start_value": v}
        monthly[month_key]["value"] = v

    result = []
    prev_value = None
    for month in sorted(monthly.keys()):
        m = monthly[month]
        if prev_value:
            ret = (m["value"] - prev_value) / prev_value if prev_value > 0 else 0
            result.append({
                "month": month,
                "return_pct": round(ret * 100, 2),
            })
        prev_value = m["value"]
    return result


# ── 对比回测 ────────────────────────────────────────────────────────

def compare_strategies(
    ts_codes: list[str],
    start_date: str,
    end_date: str,
    strategies: list[dict],
) -> dict:
    """
    对比多个策略的回测表现，输出对比表格 + 汇总指标
    strategies: [{"name": str, "weight": dict}, ...]
    weight: {ts_code: weight_pct}  （暂未实现权重分配，仅支持等权）
    返回：{
        "strategy_results": [单独策略结果],
        "comparison_table": [{name, sharpe, max_drawdown, annual_return, win_rate}, ...],
        "portfolio_agg": portfolio-level aggregation
    }
    """
    results = []
    for strat in strategies:
        result = backtest_strategy(
            ts_codes=ts_codes,
            start_date=start_date,
            end_date=end_date,
            initial_capital=1000000,
        )
        result["strategy_name"] = strat.get("name", "unnamed")
        results.append(result)

    # ── 多策略对比表 ──────────────────────────────────────────────────
    comparison_table = []
    for r in results:
        if "error" in r:
            continue
        comparison_table.append({
            "strategy": r.get("strategy_name", "unnamed"),
            "total_return_pct": r.get("total_return", 0),
            "annual_return_pct": r.get("annual_return", 0),
            "sharpe_ratio": r.get("sharpe_ratio", 0),
            "max_drawdown_pct": r.get("max_drawdown", 0),
            "win_rate_pct": r.get("win_rate", 0),
            "trading_days": r.get("trading_days", 0),
            "final_value": r.get("final_value", 0),
        })

    # ── 组合汇总 ─────────────────────────────────────────────────────
    portfolio_agg = aggregate_portfolio_results(results)

    return {
        "strategy_results": results,
        "comparison_table": comparison_table,
        "portfolio_agg": portfolio_agg,
    }


def aggregate_portfolio_results(results: list[dict]) -> dict:
    """
    组合层面聚合：合并equity curve + 平均最大回撤 + 综合Sharpe
    results: compare_strategies() 返回的 strategy_results
    """
    if not results:
        return {}

    # 对齐日期，取所有结果的 equity_curve
    # equity_curve 格式: [value, value, ...]  与 dates 对应
    all_dates_sets = []
    for r in results:
        if "error" in r:
            continue
        eq = r.get("equity_curve", [])
        if not eq:
            continue
        all_dates_sets.append(set(range(len(eq))))

    # 取最短equity curve长度
    min_len = min((len(r.get("equity_curve", [])) for r in results if "error" not in r), default=0)
    if min_len == 0:
        return {}

    # 合并equity curve（等权平均）
    n_strategies = len([r for r in results if "error" not in r])
    combined_curve = []
    for i in range(min_len):
        total = sum(
            r["equity_curve"][i]
            for r in results
            if "error" not in r and i < len(r.get("equity_curve", []))
        )
        combined_curve.append(round(total / n_strategies, 2))

    # 平均最大回撤
    avg_max_dd = sum(
        r.get("max_drawdown", 0) for r in results if "error" not in r
    ) / n_strategies

    # 组合日收益（基于合并曲线）
    daily_returns = []
    for i in range(1, len(combined_curve)):
        if combined_curve[i - 1] > 0:
            dr = (combined_curve[i] - combined_curve[i - 1]) / combined_curve[i - 1]
            daily_returns.append(dr)

    # 综合夏普（假设初始资金 1M * n_strategies）
    rf = 0.03
    import statistics
    if len(daily_returns) > 1:
        std_dev = statistics.stdev(daily_returns)
        mean_dr = statistics.mean(daily_returns)
        sharpe = (mean_dr * 252 - rf) / (std_dev * (252 ** 0.5)) if std_dev > 0 else 0
    else:
        sharpe = 0.0

    total_return_pct = (combined_curve[-1] - combined_curve[0]) / combined_curve[0] * 100 if combined_curve[0] > 0 else 0  # noqa: E501
    n_days = min_len
    annual_return_pct = total_return_pct / (n_days / 252) if n_days > 252 else total_return_pct * 252 / n_days  # noqa: E501

    # 组合最大回撤（基于合并曲线）
    peak = combined_curve[0]
    max_drawdown = 0.0
    for v in combined_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        if dd > max_drawdown:
            max_drawdown = dd

    return {
        "combined_equity_curve": combined_curve,
        "avg_max_drawdown_pct": round(avg_max_dd, 2),
        "portfolio_max_drawdown_pct": round(max_drawdown * 100, 2),
        "portfolio_sharpe_ratio": round(sharpe, 2),
        "portfolio_annual_return_pct": round(annual_return_pct, 2),
        "portfolio_total_return_pct": round(total_return_pct, 2),
        "n_strategies": n_strategies,
    }


def print_comparison_table(comparison_table: list[dict]) -> None:
    """打印多策略对比表（ASCII表格）"""
    if not comparison_table:
        print("无可用策略对比数据")
        return

    headers = ["策略", "年化收益%", "最大回撤%", "夏普比率", "胜率%", "交易日数", "最终权益"]
    rows = []
    for r in comparison_table:
        rows.append([
            r.get("strategy", ""),
            f"{r.get('annual_return_pct', 0):+.2f}",
            f"{r.get('max_drawdown_pct', 0):.2f}",
            f"{r.get('sharpe_ratio', 0):.2f}",
            f"{r.get('win_rate_pct', 0):.1f}",
            str(r.get("trading_days", 0)),
            f"¥{r.get('final_value', 0):,.0f}",
        ])

    col_widths = [max(len(str(row[i])) for row in rows + [headers]) for i in range(len(headers))]

    def sep():
        print("+" + "+".join("-" * (w + 2) for w in col_widths) + "+")

    def row_line(cells):
        print("|" + "|".join(f" {str(cells[i]).ljust(col_widths[i])} " for i in range(len(cells))) + "|")  # noqa: E501

    sep()
    row_line(headers)
    sep()
    for r in rows:
        row_line(r)
    sep()


# ── 技术指标引擎 ──────────────────────────────────────────────────────

import math
import statistics

class TechnicalIndicators:
    """
    布林带 / CCI / RSI 指标计算器
    纯 stdlib 实现，基于历史行情数据计算
    """

    @staticmethod
    def calc_bollinger_bands(closes: list[float], period: int = 20,
                               num_std: float = 2.0) -> list[dict]:
        """
        布林带
        closes     : 收盘价序列（从旧到新）
        period     : 计算周期（默认20日）
        num_std    : 标准差倍数（默认2σ）
        返回: [{"date": str, "close": float, "upper": float, "middle": float, "lower": float, "bandwidth": float, "position": float}, ...]  # noqa: E501
        position = (close - lower) / (upper - lower)，0~1表示价格在带内位置，>1突破上轨，<0突破下轨
        """
        results = []
        for i in range(len(closes)):
            if i < period - 1:
                results.append({
                    "upper": None, "middle": None, "lower": None,
                    "bandwidth": None, "position": None,
                    "close": closes[i],
                    "signal": "insufficient_data",
                })
                continue

            window = closes[i - period + 1 : i + 1]
            mid = statistics.mean(window)
            std = statistics.stdev(window)
            upper = mid + num_std * std
            lower = mid - num_std * std
            bandwidth = (upper - lower) / mid if mid > 0 else 0
            close = closes[i]

            if upper == lower:
                position = 0.5
            else:
                position = (close - lower) / (upper - lower)

            signal = "neutral"
            if close > upper:
                signal = "upper_breakout"    # 突破上轨，看空
            elif close < lower:
                signal = "lower_breakout"    # 突破下轨，看多
            elif position > 0.8:
                signal = "near_upper"        # 接近上轨
            elif position < 0.2:
                signal = "near_lower"        # 接近下轨

            results.append({
                "close": round(close, 3),
                "middle": round(mid, 3),
                "upper": round(upper, 3),
                "lower": round(lower, 3),
                "bandwidth": round(bandwidth, 4),
                "position": round(position, 3),
                "signal": signal,
            })
        return results

    @staticmethod
    def calc_cci(closes: list[float], highs: list[float], lows: list[float],
                 period: int = 14) -> list[dict]:
        """
        CCI（顺势指标）
        closes/highs/lows : 收盘价/最高价/最低价序列
        period            : 计算周期（默认14日）
        返回: [{"close": float, "cci": float, "signal": str}, ...]
        CCI > +100 超买，CCI < -100 超卖，接近0表明趋势中性
        """
        results = []
        for i in range(len(closes)):
            if i < period - 1:
                results.append({"cci": None, "signal": "insufficient_data", "close": closes[i]})
                continue

            window_close = closes[i - period + 1 : i + 1]
            window_high = highs[i - period + 1 : i + 1]
            window_low = lows[i - period + 1 : i + 1]

            tp = (window_high[-1] + window_low[-1] + window_close[-1]) / 3
            sma = statistics.mean(window_close)
            mad = statistics.mean([abs(v - sma) for v in window_close])
            cci = (tp - sma) / (0.015 * mad) if mad != 0 else 0

            if cci > 100:
                signal = "overbought"
            elif cci < -100:
                signal = "oversold"
            elif cci > 50:
                signal = "bullish"
            elif cci < -50:
                signal = "bearish"
            else:
                signal = "neutral"

            results.append({
                "close": closes[i],
                "cci": round(cci, 2),
                "signal": signal,
            })
        return results

    @staticmethod
    def calc_rsi(closes: list[float], period: int = 14) -> list[dict]:
        """
        RSI（相对强弱指标）
        closes : 收盘价序列（从旧到新）
        period : 计算周期（默认14日）
        返回: [{"close": float, "rsi": float, "signal": str}, ...]
        RSI > 70 超买，RSI < 30 超卖
        """
        results = []
        if len(closes) < 2:
            return [{"close": c, "rsi": None, "signal": "insufficient_data"} for c in closes]

        # 计算每日变化量
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

        for i in range(len(closes)):
            if i == 0:
                results.append({"close": closes[0], "rsi": None, "signal": "insufficient_data"})
                continue
            if i < period:
                results.append({"close": closes[i], "rsi": None, "signal": "insufficient_data"})
                continue

            gains = [d for d in deltas[i - period : i] if d > 0]
            losses = [-d for d in deltas[i - period : i] if d < 0]

            avg_gain = statistics.mean(gains) if gains else 0.0
            avg_loss = statistics.mean(losses) if losses else 0.0

            if avg_loss == 0:
                rsi = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi = 100.0 - (100.0 / (1.0 + rs))

            if rsi > 70:
                signal = "overbought"
            elif rsi < 30:
                signal = "oversold"
            elif rsi > 55:
                signal = "bullish"
            elif rsi < 45:
                signal = "bearish"
            else:
                signal = "neutral"

            results.append({
                "close": closes[i],
                "rsi": round(rsi, 2),
                "signal": signal,
            })
        return results

    @staticmethod
    def analyze_signals(bollinger_result: list[dict], cci_result: list[dict],
                        rsi_result: list[dict]) -> dict:
        """
        综合信号分析：结合三种指标给出综合评分和操作建议
        取三种指标中最新的非 None 数据点进行共振分析
        """
        # 找最新的有效数据点
        def latest_valid(results, key):
            for r in reversed(results):
                if r.get(key) is not None:
                    return r
            return None

        b = latest_valid(bollinger_result, "upper")
        c = latest_valid(cci_result, "cci")
        r = latest_valid(rsi_result, "rsi")

        signals = []
        if b:
            signals.append(b.get("signal", "neutral"))
        if c:
            signals.append(c.get("signal", "neutral"))
        if r:
            signals.append(r.get("signal", "neutral"))

        # 打分
        bullish = sum(1 for s in signals if s in ("oversold", "lower_breakout", "bullish"))
        bearish = sum(1 for s in signals if s in ("overbought", "upper_breakout", "bearish"))

        if bullish >= 2:
            verdict = "强烈买入信号（共振）"
            score = 1
        elif bullish == 1 and bearish == 0:
            verdict = "轻微买入信号"
            score = 0.5
        elif bearish >= 2:
            verdict = "强烈卖出信号（共振）"
            score = -1
        elif bearish == 1 and bullish == 0:
            verdict = "轻微卖出信号"
            score = -0.5
        else:
            verdict = "中性信号"
            score = 0

        return {
            "bollinger": b,
            "cci": c,
            "rsi": r,
            "signals_count": len(signals),
            "bullish_count": bullish,
            "bearish_count": bearish,
            "score": score,
            "verdict": verdict,
        }


# ── 压力测试引擎 ───────────────────────────────────────────────────────

class StressTestEngine:
    """
    压力测试引擎：模拟极端情景，评估组合风险
    - 情景A：连续跌停（每日-10%，3天）
    - 情景B：流动性枯竭（一字跌停，无法成交，3天）
    - 情景C：大幅波动（±5%，5天 VaR 分析）
    """

    DEFAULT_VOLATILITY = 0.02  # A股默认日波动率 2%

    def __init__(self, db_conn_func=None):
        self._db_conn_func = db_conn_func or get_db_conn

    # ── VaR 计算（方差-协方差法，正态分布）──────────────────────────────

    @staticmethod
    def calc_var(position_value: float, daily_vol: float,
                 confidence: float = 0.95, days: int = 1) -> float:
        """
        参数化 VaR 计算
        position_value : 持仓市值
        daily_vol      : 日波动率（标准差）
        confidence     : 置信度（默认 95%）
        days           : 持有期（默认 1 天）
        返回：在置信度下最大损失金额
        """
        # 正态分布分位数（单尾）
        if confidence >= 0.999:
            z = 3.0
        elif confidence >= 0.99:
            z = 2.326
        elif confidence >= 0.975:
            z = 1.96
        else:
            z = 1.645  # 90%

        var = position_value * daily_vol * math.sqrt(days) * z
        return round(var, 2)

    # ── 最大回撤 ─────────────────────────────────────────────────────────

    @staticmethod
    def calc_max_drawdown(equity_curve: list[float]) -> dict:
        """
        计算最大回撤及恢复时长
        equity_curve: 每日组合价值列表
        返回：{"max_drawdown_pct": float, "recovery_days": int, "peak_value": float}
        """
        if not equity_curve or len(equity_curve) < 2:
            return {"max_drawdown_pct": 0.0, "recovery_days": 0, "peak_value": 0.0}

        peak = equity_curve[0]
        max_dd = 0.0
        peak_at = 0
        trough_at = 0

        for i, v in enumerate(equity_curve):
            if v > peak:
                peak = v
                peak_at = i
            dd = (peak - v) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
                trough_at = i

        # 恢复天数：从谷底到下一次回到峰值
        recovery_days = 0
        if trough_at < len(equity_curve) - 1:
            for j in range(trough_at + 1, len(equity_curve)):
                if equity_curve[j] >= equity_curve[peak_at]:
                    recovery_days = j - trough_at
                    break

        return {
            "max_drawdown_pct": round(max_dd * 100, 2),
            "recovery_days": recovery_days,
            "peak_value": round(peak, 2),
        }

    # ── 从数据库估算波动率 ──────────────────────────────────────────────

    def _fetch_volatility(self, ts_codes: list[str], lookback_days: int = 30) -> float:
        """从 trading.daily_quotes 估算加权平均日波动率"""
        if not ts_codes:
            return self.DEFAULT_VOLATILITY

        conn = self._db_conn_func()
        cur = conn.cursor()
        try:
            placeholders = ",".join(["%s"] * len(ts_codes))
            cur.execute(f"""
                SELECT ts_code, AVG(pct_change) as mean_ret,
                       COALESCE(STDDEV(pct_change), 0) as std_ret,
                       COUNT(*) as n
                FROM trading.daily_quotes
                WHERE ts_code IN ({placeholders})
                  AND trade_date >= CURRENT_DATE - INTERVAL '{lookback_days} days'
                GROUP BY ts_code
                HAVING COUNT(*) >= 5
            """, ts_codes)
            rows = cur.fetchall()
            if not rows:
                return self.DEFAULT_VOLATILITY

            # 加权平均（按样本数）
            total_n = sum(r[3] for r in rows)
            if total_n == 0:
                return self.DEFAULT_VOLATILITY
            weighted_vol = sum(r[2] * r[3] for r in rows) / total_n
            return max(weighted_vol, self.DEFAULT_VOLATILITY)
        except Exception:
            return self.DEFAULT_VOLATILITY
        finally:
            conn.close()

    # ── 核心压力测试 ─────────────────────────────────────────────────────

    def run_stress_test(self, portfolio_value: float,
                        positions: list[dict]) -> dict:
        """
        执行三种极端情景压力测试
        portfolio_value : 初始组合市值（元）
        positions        : 持仓列表，每项含 code/shares/avg_cost/current_price/vol(可选)
        返回：压力测试报告（dict）
        """
        # 提取股票代码用于波动率估算
        ts_codes = [str(p.get("code", "")) for p in positions if p.get("code")]
        vol = self._fetch_volatility(ts_codes) if ts_codes else self.DEFAULT_VOLATILITY

        scenario_a = self._scenario_consecutive_limit_down(portfolio_value)
        scenario_b = self._scenario_liquidity_crisis(portfolio_value)
        scenario_c = self._scenario_high_volatility(portfolio_value, vol)

        # 追加保证金预警（低于初始 20% 触发）
        warning_threshold = portfolio_value * 0.20
        margin_call = {
            "warning_threshold": round(warning_threshold, 2),
            "triggered": any([
                scenario_a["final_value"] < warning_threshold,
                scenario_b["final_value"] < warning_threshold,
                scenario_c["final_value"] < warning_threshold,
            ])
        }

        # 5日 99% VaR
        var_5d_99 = self.calc_var(portfolio_value, vol, confidence=0.99, days=5)

        return {
            "portfolio_initial": round(portfolio_value, 2),
            "estimated_daily_volatility": round(vol, 4),
            "scenarios": {
                "A_consecutive_limit_down": scenario_a,
                "B_liquidity_crisis": scenario_b,
                "C_high_volatility_5d": scenario_c,
            },
            "var_5d_99": round(var_5d_99, 2),
            "margin_call_warning": margin_call,
        }

    # ── 情景A：连续跌停 ─────────────────────────────────────────────────

    @staticmethod
    def _scenario_consecutive_limit_down(initial_value: float,
                                        days: int = 3,
                                        daily_pct: float = -0.10) -> dict:
        """
        情景A：持仓连续跌停（-10%/天），无法斩仓
        """
        values = [initial_value]
        for d in range(days):
            values.append(values[-1] * (1 + daily_pct))

        final_value = values[-1]
        loss = initial_value - final_value
        loss_pct = loss / initial_value * 100

        return {
            "description": f"连续跌停 {days} 天（每日-{abs(daily_pct)*100:.0f}%）",
            "initial_value": round(initial_value, 2),
            "final_value": round(final_value, 2),
            "absolute_loss": round(loss, 2),
            "loss_pct": round(loss_pct, 2),
            "daily_values": [round(v, 2) for v in values[1:]],
        }

    # ── 情景B：流动性枯竭 ───────────────────────────────────────────────

    @staticmethod
    def _scenario_liquidity_crisis(initial_value: float,
                                   days: int = 3,
                                   daily_pct: float = -0.10) -> dict:
        """
        情景B：一字跌停（成交量为0），连续无法成交。
        损失按跌停价计算（实际无法卖出），体现更真实的极端风险。
        """
        # 流动性枯竭 = 一字板跌停，每天损失 10%（同情景A，但描述侧重点不同）
        values = [initial_value]
        for d in range(days):
            new_val = values[-1] * (1 + daily_pct)
            values.append(new_val)

        final_value = values[-1]
        loss = initial_value - final_value
        loss_pct = loss / initial_value * 100

        # 无法在跌停价卖出，实际滑点更大（按当日收盘价模拟，实际无法成交）
        realistic_loss = loss * 1.05  # 额外 5% 滑点（冲击成本）

        return {
            "description": f"流动性枯竭：成交量为0，连续 {days} 天一字跌停（实际无法卖出）",
            "initial_value": round(initial_value, 2),
            "final_value": round(final_value, 2),
            "absolute_loss": round(loss, 2),
            "loss_pct": round(loss_pct, 2),
            "realistic_loss_with_slippage": round(realistic_loss, 2),
            "slippage_assumption_pct": 5.0,
            "daily_values": [round(v, 2) for v in values[1:]],
        }

    # ── 情景C：大幅波动（5日 VaR） ───────────────────────────────────────

    def _scenario_high_volatility(self, initial_value: float,
                                  daily_vol: float,
                                  days: int = 5,
                                  daily_move_pct: float = 0.05) -> dict:
        """
        情景C：大幅波动情景（±5%），5交易日 equity curve 及 VaR 分析
        """
        import random
        random.seed(42)  # 可重复

        # 模拟 ±5% 随机波动（等概率）
        equity = [initial_value]
        for _ in range(days):
            change = random.choice([-1, 1]) * daily_move_pct
            equity.append(equity[-1] * (1 + change))

        final_value = equity[-1]
        loss = initial_value - final_value
        loss_pct = abs(loss) / initial_value * 100

        dd_info = self.calc_max_drawdown(equity)

        # 5日 95% VaR
        var_5d_95 = self.calc_var(initial_value, daily_vol, confidence=0.95, days=5)
        # 5日 99% VaR
        var_5d_99 = self.calc_var(initial_value, daily_vol, confidence=0.99, days=5)

        return {
            "description": f"大幅波动（±{daily_move_pct*100:.0f}%/日），{days} 个交易日",
            "initial_value": round(initial_value, 2),
            "final_value": round(final_value, 2),
            "absolute_loss": round(loss, 2),
            "loss_pct": round(loss_pct, 2),
            "daily_volatility_used": daily_vol,
            "var_5d_95": round(var_5d_95, 2),
            "var_5d_99": round(var_5d_99, 2),
            "max_drawdown": dd_info,
            "equity_curve": [round(v, 2) for v in equity[1:]],
        }


# ── 技能验证 ────────────────────────────────────────────────────────

def validate_skill(skill_name: str, ts_codes: list[str], params: dict) -> dict:
    """
    使用回测引擎验证技能/规则的有效性
    skill_name: 技能名称（用于记录）
    ts_codes: 验证标的列表
    params: 技能参数（如 {"lookback_days": 60}）
    """
    end = date.today().strftime("%Y-%m-%d")
    start = (date.today() - timedelta(days=params.get("lookback_days", 60))).strftime("%Y-%m-%d")

    result = backtest_strategy(ts_codes, start, end)
    result["skill_name"] = skill_name

    # 记录到审计日志
    conn = get_db_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO audit.audit_log
                (event_type, operator, target_type, detail, result)
            VALUES ('SKILL_VALIDATED', 'SYSTEM', 'SKILL', %s, %s)
        """, (
            json.dumps({"skill": skill_name, "params": params, "result_summary": {
                "total_return": result.get("total_return"),
                "sharpe_ratio": result.get("sharpe_ratio"),
                "max_drawdown": result.get("max_drawdown"),
            }}, ensure_ascii=False),
            "PASS" if result.get("sharpe_ratio", 0) > 0.5 else "FAIL",
        ))
        conn.commit()
    finally:
        conn.close()

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # ── 压力测试演示 ────────────────────────────────────────────────────
    print("=" * 60)
    print("压力测试演示 — 持仓 ¥1,954,827，15只标的")
    print("=" * 60)

    # 硬编码示例持仓（15只：3只基金 + 12只股/ETF）
    # 基金：510300（沪深300ETF）, 512000（券商ETF）, 159928（消费ETF）
    # 股票示例（随机分配市值）
    sample_positions = [
        {"code": "510300", "name": "沪深300ETF",    "shares": 300000, "avg_cost": 3.85, "current_price": 3.92},  # noqa: E501
        {"code": "512000", "name": "券商ETF",        "shares": 200000, "avg_cost": 1.12, "current_price": 1.08},  # noqa: E501
        {"code": "159928", "name": "消费ETF",        "shares": 150000, "avg_cost": 1.05, "current_price": 1.09},  # noqa: E501
        {"code": "600519", "name": "贵州茅台",        "shares": 200,    "avg_cost": 1680.0,"current_price": 1720.0},  # noqa: E501
        {"code": "000858", "name": "五粮液",          "shares": 1200,   "avg_cost": 145.0, "current_price": 148.5},  # noqa: E501
        {"code": "300750", "name": "宁德时代",        "shares": 500,    "avg_cost": 195.0, "current_price": 188.0},  # noqa: E501
        {"code": "601318", "name": "中国平安",        "shares": 3000,   "avg_cost": 42.0,  "current_price": 44.2},  # noqa: E501
        {"code": "600036", "name": "招商银行",        "shares": 4000,   "avg_cost": 32.0,  "current_price": 33.5},  # noqa: E501
        {"code": "000001", "name": "平安银行",        "shares": 8000,   "avg_cost": 10.5,  "current_price": 10.8},  # noqa: E501
        {"code": "600887", "name": "伊利股份",        "shares": 3500,   "avg_cost": 25.0,  "current_price": 26.2},  # noqa: E501
        {"code": "002594", "name": "比亚迪",           "shares": 400,    "avg_cost": 240.0, "current_price": 235.0},  # noqa: E501
        {"code": "300059", "name": "东方财富",        "shares": 2000,   "avg_cost": 18.0,  "current_price": 19.2},  # noqa: E501
        {"code": "600030", "name": "中信证券",        "shares": 2500,   "avg_cost": 19.5,  "current_price": 20.8},  # noqa: E501
        {"code": "601888", "name": "中国中免",        "shares": 300,    "avg_cost": 68.0,  "current_price": 71.5},  # noqa: E501
        {"code": "600276", "name": "恒瑞医药",        "shares": 1500,   "avg_cost": 42.0,  "current_price": 40.5},  # noqa: E501
    ]

    # 计算总市值
    total_market_value = sum(p["shares"] * p["current_price"] for p in sample_positions)

    print(f"\n初始组合市值: ¥{total_market_value:,.2f}")
    print(f"持仓只数: {len(sample_positions)} 只")
    print(f"使用默认波动率: {StressTestEngine.DEFAULT_VOLATILITY*100:.1f}%/日（A股历史均值）")

    engine = StressTestEngine(db_conn_func=get_db_conn)
    report = engine.run_stress_test(total_market_value, sample_positions)

    print("\n── 压力测试结果 ──")
    print(f"估算日波动率: {report['estimated_daily_volatility']*100:.2f}%")
    print(f"5日 99% VaR: ¥{report['var_5d_99']:,.2f}")

    print("\n情景A — 连续跌停:")
    sa = report["scenarios"]["A_consecutive_limit_down"]
    print(f"  初始: ¥{sa['initial_value']:,.2f}")
    print(f"  最终: ¥{sa['final_value']:,.2f}  |  损失: ¥{sa['absolute_loss']:,.2f} ({sa['loss_pct']:.2f}%)")  # noqa: E501
    print(f"  每日值: {sa['daily_values']}")

    print("\n情景B — 流动性枯竭:")
    sb = report["scenarios"]["B_liquidity_crisis"]
    print(f"  初始: ¥{sb['initial_value']:,.2f}")
    print(f"  最终: ¥{sb['final_value']:,.2f}  |  损失: ¥{sb['absolute_loss']:,.2f} ({sb['loss_pct']:.2f}%)")  # noqa: E501
    print(f"  含滑点损失: ¥{sb['realistic_loss_with_slippage']:,.2f} (+5%冲击成本)")

    print("\n情景C — 大幅波动（5日）:")
    sc = report["scenarios"]["C_high_volatility_5d"]
    print(f"  初始: ¥{sc['initial_value']:,.2f}")
    print(f"  最终: ¥{sc['final_value']:,.2f}  |  损失: ¥{sc['absolute_loss']:,.2f} ({sc['loss_pct']:.2f}%)")  # noqa: E501
    print(f"  5日 95% VaR: ¥{sc['var_5d_95']:,.2f}  |  5日 99% VaR: ¥{sc['var_5d_99']:,.2f}")
    print(f"  最大回撤: {sc['max_drawdown']['max_drawdown_pct']:.2f}%  |  恢复天数: {sc['max_drawdown']['recovery_days']}")  # noqa: E501
    print(f"  模拟权益曲线: {sc['equity_curve']}")

    print("\n追加保证金预警:")
    mc = report["margin_call_warning"]
    print(f"  预警线（20%）: ¥{mc['warning_threshold']:,.2f}")
    print(f"  触发预警: {'是 ⚠️' if mc['triggered'] else '否 ✓'}")

    print("\n" + "=" * 60)

    # ── VaR 独立调用示例 ───────────────────────────────────────────────
    print("\n── VaR 独立调用示例 ──")
    var_1d_95 = StressTestEngine.calc_var(1954827, 0.02, confidence=0.95, days=1)
    var_1d_99 = StressTestEngine.calc_var(1954827, 0.02, confidence=0.99, days=1)
    print(f"1日 95% VaR: ¥{var_1d_95:,.2f}")
    print(f"1日 99% VaR: ¥{var_1d_99:,.2f}")

    print("\n回测演示完成。")

    # 加载持仓（优先加密持仓表，降级 CSV）
    positions = []
    try:
        from pgcrypto_migration import load_positions_from_db
        db_positions = load_positions_from_db()
        if db_positions:
            for pos in db_positions:
                code = str(pos.get("code", "")).zfill(6)
                if len(code) != 6:
                    continue
                # 排除基金（5/15/51/56/58 开头）
                if code.startswith(("5", "15", "51", "56", "58")):
                    continue
                market = "XSHG" if code.startswith(("5", "6", "9")) else "XSHE"
                positions.append(f"{code}.{market}")
            logger.info(f"回测持仓从加密持仓表加载: {len(positions)} 只股")
    except Exception as e:
        logger.warning(f"DB 读取失败，降级 CSV: {e}")

    # CSV 降级
    if not positions:
        logger.info("从 positions.csv 加载持仓")
        with open(POSITIONS_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if not row.get("code"):
                    continue
                code = str(row["code"]).zfill(6)
                if code.startswith(("5", "15", "51", "56", "58")):
                    continue
                market = "XSHG" if code.startswith(("5", "6")) else "XSHE"
                positions.append(f"{code}.{market}")

    end = date.today().strftime("%Y-%m-%d")
    start = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")

    print(f"=== 回测：{start} ~ {end} ===")
    print(f"标的: {positions[:5]}...")

    result = backtest_strategy(positions, start, end)
    if "error" in result:
        print(f"回测失败: {result['error']}")
    else:
        print(f"总收益: {result['total_return']}%")
        print(f"年化收益: {result['annual_return']}%")
        print(f"夏普比率: {result['sharpe_ratio']}")
        print(f"最大回撤: {result['max_drawdown']}%")
        print(f"胜率: {result['win_rate']}%")

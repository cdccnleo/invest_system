"""
backtester.py — A股事件驱动回测引擎
=====================================

功能：
  - 从 PostgreSQL market.daily_quotes 加载历史行情
  - 支持多种技术信号策略（MA交叉/RSI/MACD/布林带）
  - 事件驱动撮合模拟（支持限价/市价/滑点）
  - 佣金/印花税计算
  - 输出完整绩效指标

用法：
  python backtester.py --code 300059.XSHE --strategy ma_cross --start 2026-01-01

Author: InvestPilot v2.0 Phase 5
"""

from __future__ import annotations

import os
import logging
import getopt
import sys
from pathlib import Path
from datetime import date

import psycopg2
import numpy as np
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("invest_system.backtester")

load_dotenv(Path(__file__).parent.parent / ".env")


# ── 配置 ────────────────────────────────────────────────────────────────────

class BacktestConfig:
    """回测全局配置"""

    # 初始资金
    initial_cash: float = 1_000_000.0

    # 手续费（单边）
    commission_rate: float = 0.0003   # 万3佣金
    stamp_tax_rate: float = 0.001     # 千1印花税（卖出时）
    min_commission: float = 5.0      # 最低佣金

    # 滑点
    slippage_pct: float = 0.0005     # 万5滑点

    # 策略参数默认值
    default_params: dict = {
        "ma_cross": {"short_window": 5, "long_window": 20},
        "rsi": {"rsi_period": 14, "overbought": 70, "oversold": 30},
        "macd": {"fast": 12, "slow": 26, "signal": 9},
        "bollinger": {"bb_period": 20, "std_mult": 2.0},
    }

    # 最小数据天数（不够则报警）
    min_data_days: int = 30


# ── 数据库 ───────────────────────────────────────────────────────────────────

def get_db_conn():
    """获取数据库连接"""
    try:
        from credentials import get_credential
        db_pass = get_credential("DB_PASSWORD")
        if db_pass:
            return psycopg2.connect(
                host="localhost", user="invest_admin",
                database="investpilot", password=db_pass,
            )
    except ImportError:
        pass

    return psycopg2.connect(
        host="localhost", user="invest_admin",
        database="investpilot",
        password=os.environ.get("DB_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")),
    )


def load_quotes(
    ts_code: str,
    start_date: date | str,
    end_date: date | str,
) -> list[dict]:
    """从数据库加载历史行情"""
    if isinstance(start_date, str):
        start_date = date.fromisoformat(start_date)
    if isinstance(end_date, str):
        end_date = date.fromisoformat(end_date)

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ts_code, trade_date, open_price, high_price, low_price,
               close_price, volume, change_pct
        FROM market.daily_quotes
        WHERE ts_code = %s
          AND trade_date >= %s
          AND trade_date <= %s
        ORDER BY trade_date ASC
        """,
        (ts_code, start_date, end_date),
    )
    rows = cur.fetchall()
    conn.close()

    quotes = []
    for r in rows:
        quotes.append({
            "ts_code":    r[0],
            "trade_date": r[1],
            "open":       float(r[2] or 0),
            "high":       float(r[3] or 0),
            "low":        float(r[4] or 0),
            "close":      float(r[5] or 0),
            "volume":     int(r[6] or 0),
            "change_pct": float(r[7] or 0),
        })
    return quotes


# ── 技术指标 ─────────────────────────────────────────────────────────────────

def calc_sma(prices: list[float], window: int) -> list[float | None]:
    """简单移动平均"""
    result = [None] * len(prices)
    for i in range(window - 1, len(prices)):
        result[i] = round(sum(prices[i - window + 1:i + 1]) / window, 4)
    return result


def calc_ema(prices: list[float], window: int) -> list[float | None]:
    """指数移动平均"""
    result = [None] * len(prices)
    if len(prices) < window:
        return result
    alpha = 2 / (window + 1)
    result[window - 1] = sum(prices[:window]) / window
    for i in range(window, len(prices)):
        result[i] = alpha * prices[i] + (1 - alpha) * result[i - 1]
        result[i] = round(result[i], 4)
    return result


def calc_rsi(prices: list[float], period: int = 14) -> list[float | None]:
    """RSI 指标"""
    result = [None] * len(prices)
    if len(prices) <= period:
        return result
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = round(100 - 100 / (1 + rs), 4)

    for i in range(period + 1, len(prices)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i] = round(100 - 100 / (1 + rs), 4)
    return result


def calc_macd(
    prices: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """MACD (DIF, DEA, MACD柱)"""
    ema_fast = calc_ema(prices, fast)
    ema_slow = calc_ema(prices, slow)
    dif = [None] * len(prices)
    for i in range(slow - 1, len(prices)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            dif[i] = round(ema_fast[i] - ema_slow[i], 4)
    dea = calc_ema(
        [0 if d is None else d for d in dif],
        signal,
    )
    macd_hist = [None] * len(prices)
    for i in range(slow - 1 + signal - 1, len(prices)):
        if dif[i] is not None and dea[i] is not None:
            macd_hist[i] = round(2 * (dif[i] - dea[i]), 4)
    return dif, dea, macd_hist


def calc_bollinger(
    prices: list[float],
    period: int = 20,
    std_mult: float = 2.0,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """布林带 (上轨, 中轨, 下轨)"""
    sma = calc_sma(prices, period)
    upper = [None] * len(prices)
    lower = [None] * len(prices)
    for i in range(period - 1, len(prices)):
        if sma[i] is not None:
            window = prices[i - period + 1:i + 1]
            std = float(np.std(window, ddof=0))
            upper[i] = round(sma[i] + std_mult * std, 4)
            lower[i] = round(sma[i] - std_mult * std, 4)
    return upper, sma, lower


# ── 信号生成策略 ────────────────────────────────────────────────────────────

class SignalStrategy:
    """信号生成基类"""

    def __init__(self, params: dict):
        self.params = params

    def generate(self, quotes: list[dict], position: int) -> str | None:
        """
        返回信号: 'BUY' | 'SELL' | 'CLOSE' | None
        position: 当前持仓股数（>0 表示有多仓）
        """
        raise NotImplementedError


class MAcrossStrategy(SignalStrategy):
    """均线交叉策略"""

    def generate(self, quotes: list[dict], position: int) -> str | None:
        p = self.params
        short_w = p.get("short_window", 5)
        long_w = p.get("long_window", 20)

        closes = [q["close"] for q in quotes]
        ma_short = calc_sma(closes, short_w)
        ma_long = calc_sma(closes, long_w)

        if len(quotes) < long_w + 2:
            return None

        idx = len(quotes) - 1
        prev_idx = idx - 1

        # 金叉：短期均线从下穿上
        if (
            ma_short[prev_idx] is not None
            and ma_long[prev_idx] is not None
            and ma_short[idx] is not None
            and ma_long[idx] is not None
        ):
            if ma_short[prev_idx] <= ma_long[prev_idx] and ma_short[idx] > ma_long[idx]:
                return "BUY" if position == 0 else None
            # 死叉：短期均线从上穿下
            if ma_short[prev_idx] >= ma_long[prev_idx] and ma_short[idx] < ma_long[idx]:
                return "SELL" if position > 0 else "CLOSE"

        return None


class RSIStrategy(SignalStrategy):
    """RSI 超买超卖策略"""

    def generate(self, quotes: list[dict], position: int) -> str | None:
        p = self.params
        period = p.get("rsi_period", 14)
        ob = p.get("overbought", 70)
        os = p.get("oversold", 30)

        closes = [q["close"] for q in quotes]
        rsi = calc_rsi(closes, period)

        if len(quotes) < period + 2:
            return None

        idx = len(quotes) - 1
        prev_idx = idx - 1

        if rsi[idx] is None or rsi[prev_idx] is None:
            return None

        # RSI 从超卖区上穿 30 → 买入
        if rsi[prev_idx] < os <= rsi[idx] and position == 0:
            return "BUY"
        # RSI 从超买区下穿 70 → 卖出
        if rsi[prev_idx] > ob >= rsi[idx] and position > 0:
            return "SELL"

        return None


class MACDStrategy(SignalStrategy):
    """MACD 策略（DIF/DEA 交叉）"""

    def generate(self, quotes: list[dict], position: int) -> str | None:
        p = self.params
        fast, slow, signal = p.get("fast", 12), p.get("slow", 26), p.get("signal", 9)
        closes = [q["close"] for q in quotes]
        dif, dea, _ = calc_macd(closes, fast, slow, signal)

        if len(quotes) < slow + signal + 2:
            return None

        idx = len(quotes) - 1
        prev_idx = idx - 1

        if dif[prev_idx] is None or dea[prev_idx] is None:
            return None
        if dif[idx] is None or dea[idx] is None:
            return None

        # DIF 从下穿上 DEA → 买入
        if dif[prev_idx] <= dea[prev_idx] and dif[idx] > dea[idx]:
            return "BUY" if position == 0 else None
        # DIF 从上穿下 DEA → 卖出
        if dif[prev_idx] >= dea[prev_idx] and dif[idx] < dea[idx]:
            return "SELL" if position > 0 else "CLOSE"

        return None


STRATEGY_MAP: dict[str, type[SignalStrategy]] = {
    "ma_cross":  MAcrossStrategy,
    "rsi":       RSIStrategy,
    "macd":      MACDStrategy,
}


# ── 回测撮合 ─────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    事件驱动回测引擎

    撮合规则：
    - BUY/SELL 信号 → 以下一日开盘价执行（考虑滑点）
    - 卖出时额外收取印花税
    - 佣金双向收取，最低 5 元
    """

    def __init__(self, config: BacktestConfig = None):
        self.cfg = config or BacktestConfig()
        self.quotes: list[dict] = []
        self.signals: list[tuple] = []   # (trade_date, signal, price)
        self.trades: list[dict] = []     # 成交记录
        self.equity_curve: list[tuple] = []  # (trade_date, equity)

        # 状态
        self.cash: float = self.cfg.initial_cash
        self.position: int = 0          # 当前持仓股数
        self.long_position_price: float = 0.0  # 开仓成本
        self.total_commission: float = 0.0
        self.total_stamp_tax: float = 0.0

    # ── 运行回测 ────────────────────────────────────────────────────────────

    def run(
        self,
        quotes: list[dict],
        strategy: SignalStrategy,
        start_date: date | str = None,
        end_date: date | str = None,
    ) -> dict:
        """执行回测主循环"""
        if not quotes:
            return {"error": "行情数据为空"}

        if isinstance(start_date, str):
            start_date = date.fromisoformat(start_date)
        if isinstance(end_date, str):
            end_date = date.fromisoformat(end_date)

        # 过滤日期范围
        self.quotes = quotes
        if start_date:
            self.quotes = [q for q in self.quotes if q["trade_date"] >= start_date]
        if end_date:
            self.quotes = [q for q in self.quotes if q["trade_date"] <= end_date]

        n = len(self.quotes)
        if n < self.cfg.min_data_days:
            logger.warning(
                f"数据不足 {n} 天（最小 {self.cfg.min_data_days} 天），"
                "结果仅供参考。"
            )

        # 预计算指标（避免重复计算）
        self._precompute_indicators(strategy)

        self.cash = self.cfg.initial_cash
        self.position = 0
        self.trades = []
        self.equity_curve = []
        self.total_commission = 0.0
        self.total_stamp_tax = 0.0

        # 事件驱动主循环
        for i in range(1, n):
            bar = self.quotes[i]
            prev_bar = self.quotes[i - 1]
            trade_date = bar["trade_date"]

            # 1. 信号生成（基于前一日数据决定当日收盘后信号，次日执行）
            signal = strategy.generate(self.quotes[: i + 1], self.position)

            # 2. 结算当日持仓权益（用于 equity curve）
            prev_equity = self._calc_equity(prev_bar)
            self.equity_curve.append((prev_bar["trade_date"], prev_equity))

            if signal and i < n - 1:   # 最后一天不建仓/平仓
                exec_bar = self.quotes[i + 1]
                exec_price = self._exec_price(exec_bar, signal)

                if signal in ("BUY", "SELL", "CLOSE"):
                    self._execute(trade_date, signal, exec_bar["trade_date"], exec_price)

        # 最后一天标记
        final_equity = self._calc_equity(self.quotes[-1])
        self.equity_curve.append((self.quotes[-1]["trade_date"], final_equity))

        return self._build_report()

    def _precompute_indicators(self, strategy: SignalStrategy):
        """预计算指标（可选优化）"""
        pass  # 当前策略在 generate() 中实时计算，未来可优化

    def _exec_price(self, bar: dict, signal: str) -> float:
        """计算执行价（含滑点）"""
        price = bar["open"] if bar["open"] and bar["open"] > 0 else bar["close"]
        slippage = self.cfg.slippage_pct
        if signal in ("SELL", "CLOSE"):
            return round(price * (1 - slippage), 4)
        return round(price * (1 + slippage), 4)

    def _execute(
        self,
        signal_date: date,
        signal: str,
        exec_date: date,
        exec_price: float,
    ):
        """执行一笔交易"""
        if signal == "BUY":
            # 可用资金买满仓
            budget = self.cash / (1 + self.cfg.commission_rate)
            shares = int(budget / exec_price / 100) * 100  # 整手
            if shares < 100:
                return
            cost = shares * exec_price
            commission = max(cost * self.cfg.commission_rate, self.cfg.min_commission)
            self.cash -= (cost + commission)
            self.position = shares
            self.long_position_price = exec_price
            self.total_commission += commission
            self.trades.append({
                "date":        exec_date,
                "signal_date": signal_date,
                "action":      "BUY",
                "price":       exec_price,
                "shares":      shares,
                "cost":        cost,
                "commission":  commission,
                "cash":        round(self.cash, 2),
            })

        elif signal in ("SELL", "CLOSE"):
            if self.position < 100:
                return
            shares = self.position
            proceeds = shares * exec_price
            commission = max(proceeds * self.cfg.commission_rate, self.cfg.min_commission)
            stamp_tax = proceeds * self.cfg.stamp_tax_rate
            net = proceeds - commission - stamp_tax
            self.cash += net
            self.total_commission += commission
            self.total_stamp_tax += stamp_tax
            pnl = net - shares * self.long_position_price
            self.trades.append({
                "date":        exec_date,
                "signal_date": signal_date,
                "action":      signal,
                "price":       exec_price,
                "shares":      shares,
                "proceeds":    proceeds,
                "commission":  commission,
                "stamp_tax":   stamp_tax,
                "pnl":         round(pnl, 2),
                "cash":        round(self.cash, 2),
            })
            self.position = 0
            self.long_position_price = 0.0

    def _calc_equity(self, bar: dict) -> float:
        """计算当前Bar的账户总权益"""
        position_value = self.position * bar["close"]
        return round(self.cash + position_value, 2)

    # ── 绩效报告 ────────────────────────────────────────────────────────────

    def _build_report(self) -> dict:
        """构建绩效指标报告"""
        if not self.equity_curve:
            return {"error": "无权益曲线数据"}

        dates = [e[0] for e in self.equity_curve]
        equity_arr = np.array([e[1] for e in self.equity_curve])

        total_return = (equity_arr[-1] / self.cfg.initial_cash - 1) * 100
        n_days = len(dates)
        n_years = n_days / 252
        annual_return = ((equity_arr[-1] / self.cfg.initial_cash) ** (1 / n_years) - 1) * 100 if n_years > 0 else 0  # noqa: E501

        # 最大回撤
        peak = np.maximum.accumulate(equity_arr)
        drawdowns = (equity_arr - peak) / peak * 100
        max_drawdown = float(np.min(drawdowns))

        # Sharpe Ratio（假设无风险利率 3%）
        risk_free = 0.03
        daily_returns = np.diff(equity_arr) / equity_arr[:-1]
        daily_excess = daily_returns - risk_free / 252
        sharpe = 0.0
        if len(daily_excess) > 1 and np.std(daily_excess, ddof=1) > 1e-8:
            sharpe = float(np.mean(daily_excess) / np.std(daily_excess, ddof=1) * np.sqrt(252))

        # 胜负率
        sell_trades = [t for t in self.trades if t["action"] in ("SELL", "CLOSE")]
        n_wins = sum(1 for t in sell_trades if t.get("pnl", 0) > 0)
        win_rate = (n_wins / len(sell_trades) * 100) if sell_trades else 0.0

        # 总盈利/亏损
        total_pnl = sum(t.get("pnl", 0) for t in sell_trades)
        profit_factor = 1.0
        gross_profit = sum(t.get("pnl", 0) for t in sell_trades if t.get("pnl", 0) > 0)
        gross_loss = abs(sum(t.get("pnl", 0) for t in sell_trades if t.get("pnl", 0) < 0))
        if gross_loss > 0:
            profit_factor = round(gross_profit / gross_loss, 2)

        # 交易次数
        buy_count = sum(1 for t in self.trades if t["action"] == "BUY")
        sell_count = len(sell_trades)

        return {
            "summary": {
                "start_date":        str(dates[0]),
                "end_date":          str(dates[-1]),
                "trading_days":      n_days,
                "initial_cash":      self.cfg.initial_cash,
                "final_equity":      round(equity_arr[-1], 2),
                "total_return_pct":  round(total_return, 2),
                "annual_return_pct": round(annual_return, 2),
                "max_drawdown_pct":  round(max_drawdown, 2),
                "sharpe_ratio":      round(sharpe, 3),
                "win_rate_pct":      round(win_rate, 1),
                "profit_factor":     profit_factor,
                "total_pnl":         round(total_pnl, 2),
                "total_commission":  round(self.total_commission, 2),
                "total_stamp_tax":   round(self.total_stamp_tax, 2),
                "buy_count":         buy_count,
                "sell_count":       sell_count,
                "position_days":    self._count_position_days(),
            },
            "equity_curve": [(str(d), round(e, 2)) for d, e in self.equity_curve],
            "trades": [
                {k: str(v) if isinstance(v, date) else v for k, v in t.items()}
                for t in self.trades
            ],
        }

    def _count_position_days(self) -> int:
        """计算持仓总天数"""
        if not self.equity_curve:
            return 0
        return sum(1 for e in self.equity_curve if self.position > 0 or any(
            t["action"] == "BUY" and str(t["date"]) == str(e[0])
            for t in self.trades
        ))


# ── 多策略对比与组合聚合 ─────────────────────────────────────────────

class MultiStrategyComparison:
    """
    多策略对比引擎：运行多个策略并输出对比表 + 组合层面聚合
    """

    def __init__(self, config: BacktestConfig | None = None):
        self.cfg = config or BacktestConfig()

    def run(
        self,
        ts_code: str,
        start_date: date | str,
        end_date: date | str,
        strategy_names: list[str] | None = None,
    ) -> dict:
        """
        运行多策略对比
        ts_code        : 标的代码
        start_date     : 开始日期
        end_date       : 结束日期
        strategy_names : 要对比的策略列表（默认全部）
        """
        if strategy_names is None:
            strategy_names = list(STRATEGY_MAP.keys())

        quotes = load_quotes(ts_code, start_date, end_date)
        if len(quotes) < self.cfg.min_data_days:
            return {"error": f"行情数据不足，仅 {len(quotes)} 条"}

        strategy_reports = []
        for name in strategy_names:
            if name not in STRATEGY_MAP:
                continue
            params = self.cfg.default_params.get(name, {}).copy()
            strategy = STRATEGY_MAP[name](params)
            engine = BacktestEngine(self.cfg)
            report = engine.run(quotes, strategy, start_date, end_date)
            report["strategy_name"] = name
            strategy_reports.append(report)

        # ── 对比表 ────────────────────────────────────────────────────
        comparison_table = self._build_comparison_table(strategy_reports)

        # ── 组合聚合 ─────────────────────────────────────────────────
        portfolio_agg = self._aggregate_portfolio(strategy_reports)

        return {
            "ts_code": ts_code,
            "start_date": str(start_date),
            "end_date": str(end_date),
            "strategy_reports": strategy_reports,
            "comparison_table": comparison_table,
            "portfolio_agg": portfolio_agg,
        }

    def _build_comparison_table(self, reports: list[dict]) -> list[dict]:
        """构建多策略对比表"""
        table = []
        for r in reports:
            if "error" in r:
                continue
            s = r.get("summary", {})
            table.append({
                "strategy":        r.get("strategy_name", "unknown"),
                "annual_return":  s.get("annual_return_pct", 0),
                "max_drawdown":   s.get("max_drawdown_pct", 0),
                "sharpe":         s.get("sharpe_ratio", 0),
                "win_rate":       s.get("win_rate_pct", 0),
                "profit_factor":  s.get("profit_factor", 0),
                "total_pnl":      s.get("total_pnl", 0),
                "buy_count":      s.get("buy_count", 0),
                "sell_count":     s.get("sell_count", 0),
                "final_equity":   s.get("final_equity", 0),
            })
        return table

    def _aggregate_portfolio(self, reports: list[dict]) -> dict:
        """组合层面聚合：等权平均equity curve + 平均最大回撤"""
        valid = [r for r in reports if "error" not in r and r.get("equity_curve")]
        if not valid:
            return {}

        # 对齐到最短 equity curve
        min_len = min(len(r["equity_curve"]) for r in valid)
        if min_len == 0:
            return {}

        n = len(valid)
        combined = []
        for i in range(min_len):
            total = sum(r["equity_curve"][i][1] for r in valid if i < len(r["equity_curve"]))
            combined.append(round(total / n, 2))

        # 平均最大回撤
        avg_max_dd = sum(
            r.get("summary", {}).get("max_drawdown_pct", 0) for r in valid
        ) / n

        # 组合日收益
        daily_returns = []
        for i in range(1, len(combined)):
            if combined[i - 1] > 0:
                daily_returns.append((combined[i] - combined[i - 1]) / combined[i - 1])

        rf = 0.03
        if len(daily_returns) > 1:
            std_dev = float(np.std(daily_returns, ddof=1))
            mean_dr = float(np.mean(daily_returns))
            sharpe = (mean_dr * 252 - rf) / (std_dev * (252 ** 0.5)) if std_dev > 1e-8 else 0.0
        else:
            sharpe = 0.0

        initial = self.cfg.initial_cash
        total_ret = (combined[-1] - initial) / initial * 100 if initial > 0 else 0
        n_days = min_len
        annual_ret = total_ret / (n_days / 252) if n_days > 252 else total_ret * 252 / n_days

        # 组合最大回撤
        peak = combined[0]
        max_dd = 0.0
        for v in combined:
            if v > peak:
                peak = v
            dd = (peak - v) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        return {
            "combined_equity_curve": combined,
            "avg_max_drawdown_pct": round(avg_max_dd, 2),
            "portfolio_max_drawdown_pct": round(max_dd * 100, 2),
            "portfolio_sharpe": round(sharpe, 2),
            "portfolio_annual_return_pct": round(annual_ret, 2),
            "portfolio_total_return_pct": round(total_ret, 2),
            "n_strategies": n,
        }

    def print_comparison_table(self, comparison_table: list[dict]) -> None:
        """打印 ASCII 多策略对比表"""
        if not comparison_table:
            print("无可用对比数据")
            return

        headers = ["策略", "年化收益%", "最大回撤%", "夏普", "胜率%", "盈亏比", "最终权益"]
        rows = []
        for r in comparison_table:
            rows.append([
                r.get("strategy", ""),
                f"{r.get('annual_return', 0):+.2f}",
                f"{r.get('max_drawdown', 0):.2f}",
                f"{r.get('sharpe', 0):.3f}",
                f"{r.get('win_rate', 0):.1f}",
                f"{r.get('profit_factor', 0):.2f}",
                f"¥{r.get('final_equity', 0):,.0f}",
            ])

        col_widths = [max(len(str(row[i])) for row in rows + [headers]) for i in range(len(headers))]  # noqa: E501

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

    def print_portfolio_agg(self, agg: dict) -> None:
        """打印组合聚合摘要"""
        if not agg:
            print("无可用组合数据")
            return
        print("\n── 组合聚合 ──")
        print(f"  策略数量:       {agg.get('n_strategies', 'N/A')}")
        print(f"  综合年化收益:   {agg.get('portfolio_annual_return_pct', 0):+.2f}%")
        print(f"  综合总收益:     {agg.get('portfolio_total_return_pct', 0):+.2f}%")
        print(f"  综合夏普比率:   {agg.get('portfolio_sharpe', 0):.2f}")
        print(f"  平均最大回撤:   {agg.get('avg_max_drawdown_pct', 0):.2f}%")
        print(f"  组合最大回撤:   {agg.get('portfolio_max_drawdown_pct', 0):.2f}%")


# ── 命令行接口 ────────────────────────────────────────────────────────────────

def parse_args(argv: list[str]) -> dict:
    """解析命令行参数"""
    opts, _ = getopt.getopt(argv, "", [
        "code=", "strategy=", "start=", "end=",
        "initial_cash=", "short_w=", "long_w=",
        "output=", "help",
    ])
    kwargs = {}
    for o, v in opts:
        if o == "--help":
            kwargs["_help"] = True
        elif o == "--code":
            kwargs["ts_code"] = v
        elif o == "--strategy":
            kwargs["strategy"] = v
        elif o == "--start":
            kwargs["start_date"] = v
        elif o == "--end":
            kwargs["end_date"] = v
        elif o == "--initial_cash":
            kwargs["initial_cash"] = float(v)
        elif o == "--short_w":
            kwargs["short_window"] = int(v)
        elif o == "--long_w":
            kwargs["long_window"] = int(v)
        elif o == "--output":
            kwargs["output_path"] = v
    return kwargs


def main(argv: list[str] = None):
    if argv is None:
        argv = sys.argv[1:]

    kwargs = parse_args(argv)
    if kwargs.get("_help"):
        print(__doc__)
        return

    ts_code = kwargs.get("ts_code", "300059.XSHE")
    strategy_name = kwargs.get("strategy", "ma_cross")
    start_date = kwargs.get("start_date", "2026-01-01")
    end_date = kwargs.get("end_date", "2026-05-28")
    initial_cash = kwargs.get("initial_cash", 1_000_000.0)

    cfg = BacktestConfig()
    cfg.initial_cash = initial_cash

    logger.info(f"加载行情: {ts_code} ({start_date} ~ {end_date})")
    quotes = load_quotes(ts_code, start_date, end_date)
    logger.info(f"共 {len(quotes)} 条行情记录")

    if len(quotes) < 5:
        print(f"❌ 数据不足，仅 {len(quotes)} 条记录")
        return

    # 构建策略
    strategy_cls = STRATEGY_MAP.get(strategy_name, MAcrossStrategy)
    params = cfg.default_params.get(strategy_name, {}).copy()
    if strategy_name == "ma_cross":
        params.setdefault("short_window", kwargs.get("short_window", 5))
        params.setdefault("long_window", kwargs.get("long_window", 20))

    strategy = strategy_cls(params)
    logger.info(f"策略: {strategy_name} {params}")

    # 运行回测
    engine = BacktestEngine(cfg)
    report = engine.run(quotes, strategy, start_date, end_date)

    if "error" in report:
        print(f"❌ {report['error']}")
        return

    # 输出报告
    summary = report["summary"]
    print()
    print(f"═══ {ts_code} 回测报告 ═══")
    print(f"  策略:      {strategy_name} {params}")
    print(f"  回测期:    {summary['start_date']} ~ {summary['end_date']} ({summary['trading_days']} 个交易日)")  # noqa: E501
    print(f"  初始资金:  ¥{summary['initial_cash']:,.0f}")
    print(f"  最终权益:  ¥{summary['final_equity']:,.2f}")
    print(f"  总收益率:  {summary['total_return_pct']:+.2f}%")
    print(f"  年化收益:  {summary['annual_return_pct']:+.2f}%")
    print(f"  最大回撤:  {summary['max_drawdown_pct']:.2f}%")
    print(f"  Sharpe:    {summary['sharpe_ratio']:.3f}")
    print(f"  胜率:      {summary['win_rate_pct']:.1f}%")
    print(f"  盈亏比:    {summary['profit_factor']:.2f}")
    print(f"  买次/卖次: {summary['buy_count']} / {summary['sell_count']}")
    print(f"  印花税:    ¥{summary['total_stamp_tax']:,.2f}")
    print(f"  佣金合计:  ¥{summary['total_commission']:,.2f}")
    print(f"  总盈亏:    ¥{summary['total_pnl']:+,.2f}")

    # 保存报告
    output_path = kwargs.get("output_path")
    if output_path:
        import json
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        logger.info(f"报告已保存: {output_path}")


if __name__ == "__main__":
    main()

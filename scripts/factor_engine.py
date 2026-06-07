"""
factor_engine.py — 多因子选股/评分引擎
提供 6 因子评分模型，支持可配置权重、Z-score 标准化、综合评分排名

因子体系:
  1. 价值因子 (Value): PE_TTM, PB — 越低越好
  2. 质量因子 (Quality): ROE, 净利润增长率
  3. 动量因子 (Momentum): 20日/60日涨跌幅
  4. 波动率因子 (Volatility): 年化波动率 — 越低越好
  5. 技术因子 (Technical): RSI 位置, 均线偏离度
  6. 规模因子 (Size): 总市值
"""

import logging
import math
from datetime import date, timedelta
from typing import Optional

import psycopg2
import psycopg2.extras
import pandas as pd
import numpy as np

logger = logging.getLogger("invest_system.factor_engine")

DB_CONFIG = {
    "host": "localhost",
    "user": "invest_admin",
    "database": "investpilot",
    "password": "",
}

# 默认因子权重（总和 100%）
DEFAULT_WEIGHTS = {
    "value": 0.20,
    "quality": 0.25,
    "momentum": 0.20,
    "volatility": 0.10,
    "technical": 0.15,
    "size": 0.10,
}


def _get_db_conn():
    """获取数据库连接"""
    from pgcrypto_migration import get_credential
    cfg = dict(DB_CONFIG)
    cfg["password"] = get_credential("DB_PASSWORD")
    return psycopg2.connect(**cfg)


def _get_price_data(ts_code: str, days: int = 120) -> pd.DataFrame:
    """
    获取个股历史价格数据

    Args:
        ts_code: 股票代码 (如 000001.XSHE)
        days: 回溯天数

    Returns:
        包含 trade_date/open/high/low/close/volume/change_pct 的 DataFrame
    """
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT trade_date, open, high, low, close, volume, change_pct
            FROM market.daily_quotes
            WHERE ts_code = %s
              AND trade_date >= %s
            ORDER BY trade_date ASC
        """, (ts_code, date.today() - timedelta(days=days)))
        rows = cur.fetchall()
        cur.close()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=["trade_date", "open", "high", "low", "close", "volume", "change_pct"])  # noqa: E501
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["change_pct"] = pd.to_numeric(df["change_pct"], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        return df.dropna(subset=["close"])
    finally:
        conn.close()


def _get_financial_data(ts_code: str) -> dict:
    """
    获取个股最新财务指标

    Args:
        ts_code: 股票代码

    Returns:
        包含 pe/pb/roe/profit_growth/eps/bps 的字典
    """
    conn = _get_db_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT ts_code, report_date, eps, bps, roe, roe_kcj,
                   net_profit, total_revenue, gross_margin, net_margin,
                   debt_ratio, yoy_growth, profit_growth
            FROM market.financial_indicators
            WHERE ts_code = %s
            ORDER BY report_date DESC
            LIMIT 2
        """, (ts_code,))
        rows = cur.fetchall()
        cur.close()
        if not rows:
            return {}
        latest = rows[0]
        return {
            "eps": float(latest.get("eps") or 0),
            "bps": float(latest.get("bps") or 0),
            "roe": float(latest.get("roe") or 0),
            "roe_kcj": float(latest.get("roe_kcj") or 0),
            "profit_growth": float(latest.get("profit_growth") or 0),
            "yoy_growth": float(latest.get("yoy_growth") or 0),
            "gross_margin": float(latest.get("gross_margin") or 0),
            "net_margin": float(latest.get("net_margin") or 0),
            "debt_ratio": float(latest.get("debt_ratio") or 0),
        }
    finally:
        conn.close()


# ============================================================================
# 因子计算函数
# ============================================================================

def calc_value_factor(ts_code: str, price: float) -> float:
    """
    计算价值因子得分
    基于 PE_TTM 和 PB，越低越有投资价值

    Args:
        ts_code: 股票代码
        price: 当前收盘价

    Returns:
        价值因子原始得分（越高越好）
    """
    fin = _get_financial_data(ts_code)
    if not fin:
        return 0.0

    eps = fin.get("eps", 0)
    bps = fin.get("bps", 0)

    score = 0.0
    # PE 得分：PE 越低越好，PE < 0 得 0 分
    if eps > 0 and price > 0:
        pe = price / eps
        if 0 < pe <= 15:
            score += 3.0
        elif pe <= 25:
            score += 2.0
        elif pe <= 40:
            score += 1.0
        elif pe <= 60:
            score += 0.0
        else:
            score -= 1.0

    # PB 得分：PB 越低越好
    if bps > 0 and price > 0:
        pb = price / bps
        if 0 < pb <= 1.0:
            score += 3.0
        elif pb <= 2.0:
            score += 2.0
        elif pb <= 3.0:
            score += 1.0
        elif pb <= 5.0:
            score += 0.0
        else:
            score -= 1.0

    return score


def calc_quality_factor(ts_code: str) -> float:
    """
    计算质量因子得分
    基于 ROE 和净利润增长率

    Args:
        ts_code: 股票代码

    Returns:
        质量因子原始得分（越高越好）
    """
    fin = _get_financial_data(ts_code)
    if not fin:
        return 0.0

    roe = fin.get("roe", 0)
    profit_growth = fin.get("profit_growth", 0)

    score = 0.0
    # ROE 得分
    if roe >= 20:
        score += 3.0
    elif roe >= 15:
        score += 2.5
    elif roe >= 10:
        score += 2.0
    elif roe >= 5:
        score += 1.0
    elif roe > 0:
        score += 0.0
    else:
        score -= 2.0

    # 净利润增长率得分
    if profit_growth >= 50:
        score += 3.0
    elif profit_growth >= 30:
        score += 2.5
    elif profit_growth >= 15:
        score += 2.0
    elif profit_growth >= 0:
        score += 1.0
    elif profit_growth >= -10:
        score += 0.0
    else:
        score -= 2.0

    return score


def calc_momentum_factor(ts_code: str) -> float:
    """
    计算动量因子得分
    基于 20 日和 60 日涨跌幅

    Args:
        ts_code: 股票代码

    Returns:
        动量因子原始得分（越高越好）
    """
    df = _get_price_data(ts_code, days=90)
    if df.empty or len(df) < 20:
        return 0.0

    prices = df["close"].values
    current = prices[-1]

    # 20 日动量
    if len(prices) >= 20:
        ret_20 = (current - prices[-20]) / prices[-20] * 100
    else:
        ret_20 = 0

    # 60 日动量
    if len(prices) >= 60:
        ret_60 = (current - prices[-60]) / prices[-60] * 100
    else:
        ret_60 = ret_20

    return ret_20 * 0.6 + ret_60 * 0.4


def calc_volatility_factor(ts_code: str) -> float:
    """
    计算波动率因子得分
    基于年化波动率，越低越稳定（得分越高）

    Args:
        ts_code: 股票代码

    Returns:
        波动率因子原始得分（越高越好，即波动越低）
    """
    df = _get_price_data(ts_code, days=90)
    if df.empty or len(df) < 20:
        return 0.0

    returns = df["change_pct"].dropna().values / 100.0
    if len(returns) < 20:
        return 0.0

    daily_vol = np.std(returns[-60:])
    annual_vol = daily_vol * math.sqrt(252) * 100  # 年化波动率 (%)

    # 波动率越低得分越高
    if annual_vol <= 20:
        return 5.0
    elif annual_vol <= 30:
        return 4.0
    elif annual_vol <= 40:
        return 2.0
    elif annual_vol <= 50:
        return 0.0
    else:
        return -2.0


def calc_technical_factor(ts_code: str) -> float:
    """
    计算技术因子得分
    基于 RSI 位置和均线偏离度

    Args:
        ts_code: 股票代码

    Returns:
        技术因子原始得分
    """
    df = _get_price_data(ts_code, days=90)
    if df.empty or len(df) < 14:
        return 0.0

    prices = df["close"].values
    current = prices[-1]

    # RSI 计算
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-14:])
    avg_loss = np.mean(losses[-14:])
    if avg_loss == 0:
        rsi = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))

    # MA 偏离度
    ma20 = np.mean(prices[-20:]) if len(prices) >= 20 else current
    ma_deviation = (current - ma20) / ma20 * 100 if ma20 > 0 else 0

    score = 0.0
    # RSI 得分：30-70 中性区间最优
    if 30 <= rsi <= 70:
        score += 2.0
    elif 20 <= rsi < 30:
        score += 1.5  # 超卖可能反弹
    elif 70 < rsi <= 80:
        score += 0.5  # 超买谨慎
    else:
        score -= 1.0  # 极端区域

    # 均线偏离得分：轻微偏离或略高于均线
    if -3 <= ma_deviation <= 5:
        score += 2.0
    elif 5 < ma_deviation <= 10:
        score += 1.0
    elif -5 <= ma_deviation < -3:
        score += 1.0
    else:
        score -= 1.0

    return score


def calc_size_factor(ts_code: str, price: float = 0) -> float:
    """
    计算规模因子得分
    基于总市值，A股中小盘溢价逻辑

    Args:
        ts_code: 股票代码
        price: 当前收盘价（用于估算市值）

    Returns:
        规模因子原始得分
    """
    fin = _get_financial_data(ts_code)
    if not fin:
        return 0.0

    bps = fin.get("bps", 0)
    # 从 daily_quotes 获取总股本近似值
    conn = _get_db_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT close, volume FROM market.daily_quotes
            WHERE ts_code = %s AND volume > 0
            ORDER BY trade_date DESC LIMIT 5
        """, (ts_code,))
        rows = cur.fetchall()
        cur.close()
        if not rows:
            return 0.0
        avg_close = sum(float(r[0]) for r in rows if r[0]) / max(len(rows), 1)
        avg_volume = sum(float(r[1]) for r in rows if r[1]) / max(len(rows), 1)
    finally:
        conn.close()

    # 估算总股本 = 换手率反推，简化：用 bps 近似
    if bps > 0 and avg_close > 0:
        est_shares = avg_volume * 5  # 粗略估算
        market_cap = avg_close * est_shares / 1e8  # 亿元
    else:
        market_cap = 0

    # A股中小盘效应：50-500亿区间得分较高
    if market_cap <= 0:
        return 1.0
    elif market_cap <= 50:
        return 3.0
    elif market_cap <= 200:
        return 4.0
    elif market_cap <= 500:
        return 3.0
    elif market_cap <= 1000:
        return 2.0
    elif market_cap <= 5000:
        return 0.0
    else:
        return -1.0


# ============================================================================
# 因子引擎主类
# ============================================================================

class FactorEngine:
    """
    多因子选股评分引擎
    支持 6 大类因子，可配置权重，Z-score 标准化排名
    """

    def __init__(self, weights: Optional[dict] = None):
        """
        初始化因子引擎

        Args:
            weights: 因子权重字典，默认使用 DEFAULT_WEIGHTS
        """
        self.weights = weights or DEFAULT_WEIGHTS
        self._validate_weights()

    def _validate_weights(self):
        """验证权重总和为 1.0"""
        total = sum(self.weights.values())
        if abs(total - 1.0) > 0.001:
            logger.warning(f"因子权重总和 {total} != 1.0，将自动归一化")
            self.weights = {k: v / total for k, v in self.weights.items()}

    def score_single(self, ts_code: str, price: float = 0) -> dict:
        """
        对单只股票进行因子评分

        Args:
            ts_code: 股票代码 (如 000001.XSHE)
            price: 当前收盘价（可选，不传则从DB获取）

        Returns:
            包含各因子原始分、加权分和总分的字典
        """
        if price <= 0:
            df = _get_price_data(ts_code, days=5)
            if not df.empty:
                price = float(df["close"].iloc[-1])

        raw_scores = {
            "value": calc_value_factor(ts_code, price),
            "quality": calc_quality_factor(ts_code),
            "momentum": calc_momentum_factor(ts_code),
            "volatility": calc_volatility_factor(ts_code),
            "technical": calc_technical_factor(ts_code),
            "size": calc_size_factor(ts_code, price),
        }

        weighted = {k: raw_scores[k] * self.weights.get(k, 0) for k in raw_scores}
        total = sum(weighted.values())

        return {
            "ts_code": ts_code,
            "price": price,
            "raw_scores": raw_scores,
            "weighted_scores": weighted,
            "total_score": round(total, 2),
        }

    def score_batch(self, ts_codes: list[str], prices: dict = None) -> list[dict]:
        """
        批量评分并 Z-score 标准化排名

        Args:
            ts_codes: 股票代码列表
            prices: {ts_code: price} 字典（可选）

        Returns:
            按综合得分降序排列的评分结果列表
        """
        results = []
        for code in ts_codes:
            p = (prices or {}).get(code, 0)
            try:
                r = self.score_single(code, p)
                results.append(r)
            except Exception as e:
                logger.warning(f"因子评分失败 {code}: {e}")
                results.append({
                    "ts_code": code,
                    "price": p,
                    "raw_scores": {},
                    "weighted_scores": {},
                    "total_score": 0,
                    "error": str(e),
                })

        if not results:
            return results

        # Z-score 标准化
        scores = [r["total_score"] for r in results]
        mean_s = np.mean(scores) if scores else 0
        std_s = np.std(scores) if scores else 1
        if std_s == 0:
            std_s = 1

        for r in results:
            r["z_score"] = round((r["total_score"] - mean_s) / std_s, 2)

        # 按综合得分降序
        results.sort(key=lambda x: x["total_score"], reverse=True)

        # 排名
        for i, r in enumerate(results):
            r["rank"] = i + 1

        return results

    def get_factor_weights(self) -> dict:
        """获取当前因子权重配置"""
        return dict(self.weights)

    def set_factor_weights(self, weights: dict):
        """
        设置因子权重

        Args:
            weights: 新的因子权重字典
        """
        self.weights = weights
        self._validate_weights()


# ============================================================================
# 快捷函数
# ============================================================================

def get_default_engine() -> FactorEngine:
    """获取默认权重的因子引擎实例"""
    return FactorEngine()


def score_positions(positions: list[dict]) -> list[dict]:
    """
    对持仓列表进行因子评分

    Args:
        positions: 持仓列表，每项含 code/type/close 等字段

    Returns:
        按因子得分降序排列的评分结果
    """
    engine = get_default_engine()
    stock_codes = []
    price_map = {}
    for p in positions:
        if p.get("type") == "fund":
            continue
        code = p.get("code", "").zfill(6)
        if code.startswith("15") or code.startswith("30") or code.startswith("00"):
            ts_code = f"{code}.XSHE"
        elif code.startswith("5") or code.startswith("6"):
            ts_code = f"{code}.XSHG"
        elif code.startswith("4") or code.startswith("8"):
            ts_code = f"{code}.BJ"
        else:
            ts_code = f"{code}.XSHE"
        stock_codes.append(ts_code)
        price_map[ts_code] = p.get("close", p.get("cost", 0))

    return engine.score_batch(stock_codes, price_map)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    engine = get_default_engine()
    print(f"因子权重: {engine.get_factor_weights()}")

    # 测试单只股票评分
    test_codes = ["000001.XSHE", "000858.XSHE", "600519.XSHG"]
    results = engine.score_batch(test_codes)
    for r in results:
        print(f"\n{r['ts_code']} (排名 #{r['rank']}, Z={r['z_score']})")
        print(f"  总分: {r['total_score']}")
        print(f"  原始分: {r['raw_scores']}")
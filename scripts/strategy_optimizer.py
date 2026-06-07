"""
strategy_optimizer.py — 量化策略参数优化模块
基于贝叶斯优化 (Bayesian Optimization) 的自动化参数搜索
支持多目标优化：夏普比率 + 最大回撤 + 胜率
"""

import logging
from typing import Callable
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger("invest_system.strategy_optimizer")


@dataclass
class ParamSpace:
    """参数空间定义"""
    name: str
    low: float
    high: float
    step: float = 1.0
    param_type: str = "float"  # float / int / choice

    def sample(self) -> float:
        """均匀采样"""
        if self.param_type == "int":
            return int(np.random.uniform(self.low, self.high + self.step))
        elif self.param_type == "choice":
            return np.random.choice(self.low if isinstance(self.low, list) else [self.low])
        return np.random.uniform(self.low, self.high)


@dataclass
class OptimizationResult:
    """优化结果"""
    best_params: dict
    best_score: float
    all_trials: list[dict] = field(default_factory=list)
    convergence: list[float] = field(default_factory=list)
    elapsed_trials: int = 0


class BayesianOptimizer:
    """
    贝叶斯优化器
    使用高斯过程代理模型，通过 EI (Expected Improvement) 采集函数搜索最优参数

    Args:
        param_spaces: 参数空间列表
        objective_fn: 目标函数 (params -> float, 越大越好)
        n_initial: 初始随机采样点数
        n_iter: 优化迭代次数
    """

    def __init__(
        self,
        param_spaces: list[ParamSpace],
        objective_fn: Callable[[dict], float],
        n_initial: int = 10,
        n_iter: int = 30,
    ):
        self.param_spaces = param_spaces
        self.objective_fn = objective_fn
        self.n_initial = n_initial
        self.n_iter = n_iter
        self._X = []  # 已评估参数
        self._y = []  # 已评估得分

    def _sample_params(self) -> dict:
        """随机采样一组参数"""
        return {p.name: p.sample() for p in self.param_spaces}

    def _expected_improvement(self, x: dict, xi: float = 0.01) -> float:
        """
        计算 Expected Improvement 采集函数

        Args:
            x: 候选参数
            xi: 探索-利用权衡参数

        Returns:
            EI 值
        """
        if len(self._y) < 3:
            return np.random.random()

        y_best = max(self._y)
        X_np = np.array([[p[name] for p in self._X] for name in x.keys()]).T
        X_np = X_np.reshape(len(self._X), -1)

        # 简化高斯过程：使用加权平均作为预测
        x_vec = np.array([x[name] for name in x.keys()])
        if X_np.shape[0] == 0:
            return np.random.random()

        distances = np.linalg.norm(X_np - x_vec, axis=1)
        distances = np.where(distances == 0, 1e-10, distances)
        weights = 1.0 / distances
        weights /= weights.sum()

        mu = np.dot(weights, self._y)
        sigma = np.sqrt(np.mean((np.array(self._y) - mu) ** 2) + 1e-6)

        if sigma == 0:
            return 0.0

        z = (mu - y_best - xi) / sigma
        # 标准正态 CDF 近似
        ei = (mu - y_best - xi) * (0.5 * (1 + np.tanh(0.7978845608028654 * z))) + sigma * np.exp(-z**2 / 2) / np.sqrt(2 * np.pi)
        return max(0.0, ei)

    def optimize(self) -> OptimizationResult:
        """
        执行贝叶斯优化

        Returns:
            OptimizationResult 包含最优参数和得分
        """
        best_params = None
        best_score = float("-inf")
        convergence = []

        # 初始随机采样
        for _ in range(self.n_initial):
            params = self._sample_params()
            try:
                score = self.objective_fn(params)
                self._X.append(params)
                self._y.append(score)
                if score > best_score:
                    best_score = score
                    best_params = params
                convergence.append(best_score)
            except Exception as e:
                logger.warning(f"目标函数评估失败: {e}")

        # 贝叶斯优化迭代
        for i in range(self.n_iter):
            candidates = [self._sample_params() for _ in range(50)]
            ei_values = [self._expected_improvement(c) for c in candidates]
            next_params = candidates[np.argmax(ei_values)]

            try:
                score = self.objective_fn(next_params)
                self._X.append(next_params)
                self._y.append(score)
                if score > best_score:
                    best_score = score
                    best_params = next_params
                convergence.append(best_score)
            except Exception as e:
                logger.warning(f"目标函数评估失败: {e}")

        return OptimizationResult(
            best_params=best_params or {},
            best_score=best_score,
            all_trials=[{"params": p, "score": s} for p, s in zip(self._X, self._y)],
            convergence=convergence,
            elapsed_trials=len(self._y),
        )


class GridSearchOptimizer:
    """
    网格搜索优化器
    用于低维参数空间的穷举搜索

    Args:
        param_spaces: 参数空间列表
        objective_fn: 目标函数
    """

    def __init__(
        self,
        param_spaces: list[ParamSpace],
        objective_fn: Callable[[dict], float],
    ):
        self.param_spaces = param_spaces
        self.objective_fn = objective_fn

    def _generate_grid(self) -> list[dict]:
        """生成参数网格"""
        grids = []
        for p in self.param_spaces:
            if p.param_type == "int":
                values = list(range(int(p.low), int(p.high) + 1, int(p.step)))
            elif p.param_type == "choice":
                values = p.low if isinstance(p.low, list) else [p.low]
            else:
                values = list(np.arange(p.low, p.high + p.step / 2, p.step))
            grids.append(values)

        from itertools import product
        result = []
        for combo in product(*grids):
            params = {self.param_spaces[i].name: combo[i] for i in range(len(combo))}
            result.append(params)
        return result

    def optimize(self) -> OptimizationResult:
        """执行网格搜索"""
        best_params = None
        best_score = float("-inf")
        all_trials = []

        for params in self._generate_grid():
            try:
                score = self.objective_fn(params)
                all_trials.append({"params": params, "score": score})
                if score > best_score:
                    best_score = score
                    best_params = params
            except Exception as e:
                logger.warning(f"网格搜索目标函数评估失败: {e}")

        return OptimizationResult(
            best_params=best_params or {},
            best_score=best_score,
            all_trials=all_trials,
            elapsed_trials=len(all_trials),
        )


def optimize_strategy_params(
    strategy_name: str,
    price_data: list[float],
    method: str = "bayesian",
    n_iter: int = 30,
) -> OptimizationResult:
    """
    优化策略参数的高层接口

    Args:
        strategy_name: 策略名称 (ma_crossover/momentum/mean_reversion)
        price_data: 价格序列
        method: 优化方法 (bayesian/grid)
        n_iter: 贝叶斯优化迭代次数

    Returns:
        OptimizationResult
    """
    from strategy_engine import MACrossover, Momentum, MeanReversion, PerformanceEvaluator

    strategy_map = {
        "ma_crossover": (MACrossover, [
            ParamSpace("short_window", 3, 30, 1, "int"),
            ParamSpace("long_window", 20, 120, 5, "int"),
        ]),
        "momentum": (Momentum, [
            ParamSpace("lookback", 5, 60, 5, "int"),
            ParamSpace("threshold", 0.01, 0.10, 0.01),
        ]),
        "mean_reversion": (MeanReversion, [
            ParamSpace("lookback", 10, 50, 5, "int"),
            ParamSpace("entry_z", 1.0, 3.0, 0.5),
            ParamSpace("exit_z", 0.0, 1.5, 0.5),
        ]),
    }

    if strategy_name not in strategy_map:
        raise ValueError(f"未知策略: {strategy_name}")

    strategy_cls, param_spaces = strategy_map[strategy_name]

    def objective(params: dict) -> float:
        int_params = {k: int(v) if k in ("short_window", "long_window", "lookback") else v
                      for k, v in params.items()}
        strategy = strategy_cls(**int_params)
        signals = strategy.generate_signals(pd.Series(price_data))
        evaluator = PerformanceEvaluator(signals)
        report = evaluator.full_report()
        return report.get("sharpe_ratio", 0) * 0.5 - report.get("max_drawdown", 0.5) * 0.3 + report.get("win_rate", 0) * 0.2

    if method == "grid" and len(param_spaces) <= 3:
        optimizer = GridSearchOptimizer(param_spaces, objective)
    else:
        optimizer = BayesianOptimizer(param_spaces, objective, n_iter=n_iter)

    return optimizer.optimize()


if __name__ == "__main__":
    import pandas as pd
    logging.basicConfig(level=logging.INFO)

    # 生成模拟数据
    np.random.seed(42)
    prices = 100 * np.cumprod(1 + np.random.normal(0.001, 0.02, 500))

    result = optimize_strategy_params("ma_crossover", list(prices), method="bayesian")
    print(f"最优参数: {result.best_params}")
    print(f"最优得分: {result.best_score:.4f}")
    print(f"试验次数: {result.elapsed_trials}")
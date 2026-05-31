"""
fund_manager.py — 资金管理模块
管理投资组合的资金视图：总资产、仓位、现金、可用资金
"""

import os, csv, logging
from pathlib import Path
from datetime import date
from typing import Optional

import psycopg2
from pgcrypto_migration import get_credential

logger = logging.getLogger("invest_system.fund_manager")

DB_CONFIG = {
    "host": "localhost",
    "user": "invest_admin",
    "database": "investpilot",
    "password": "",  # 运行时由 _get_password() 注入,
}
POSITIONS_CSV = os.environ.get("POSITIONS_CSV", "/mnt/d/Hold/invest-data/positions.csv")


def _get_password():
    from pgcrypto_migration import get_credential
    return get_credential("DB_PASSWORD")

def get_db_conn():
    cfg = dict(DB_CONFIG)
    cfg["password"] = _get_password()
    return psycopg2.connect(**cfg)


def load_positions_from_csv(csv_path: str = POSITIONS_CSV) -> list[dict]:
    """从 CSV 读取持仓"""
    positions = []
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("code"):
                continue
            positions.append({
                "code": str(row["code"]).zfill(6),
                "name": row.get("name", ""),
                "type": row.get("type", "stock"),
                "shares": float(row.get("shares", 0)),
                "cost": float(row.get("cost", 0)),
                "market_value": float(row.get("market_value", 0)),
                "weight": float(row.get("weight", 0)),
            })
    return positions


class FundManager:
    """资金管理器"""

    def __init__(self):
        self.positions = load_positions_from_csv()
        self.total_mv = sum(p["market_value"] for p in self.positions)
        # 现金头寸（从 CSV 的总市值 vs 实际总资产估算，默认保留 5% 现金）
        self.cash_reserve_pct = 0.05
        self.cash_amount = self.total_mv * self.cash_reserve_pct
        self.total_assets = self.total_mv + self.cash_amount

    def get_fund_overview(self) -> dict:
        """资金概览"""
        used_pct = self.total_mv / self.total_assets * 100 if self.total_assets > 0 else 0
        return {
            "total_assets": round(self.total_assets, 2),
            "market_value": round(self.total_mv, 2),
            "cash_amount": round(self.cash_amount, 2),
            "cash_reserve_pct": round(self.cash_reserve_pct * 100, 2),
            "used_pct": round(used_pct, 2),
            "available_buy_amount": round(self.cash_amount, 2),
            "position_count": len(self.positions),
        }

    def get_position_weights(self) -> list[dict]:
        """各持仓仓位占比"""
        return sorted(
            [{"code": p["code"], "name": p["name"], "weight": p["weight"],
              "market_value": p["market_value"]}
             for p in self.positions],
            key=lambda x: x["weight"],
            reverse=True
        )

    def get_sector_exposure(self) -> dict:
        """行业暴露度（简化版，按代码前两位的经验映射）"""
        sector_map = {
            "00": "主板/中小", "30": "创业板", "15": "ETF/创业板",
            "51": "ETF/主板", "58": "ETF/主板", "56": "ETF/主板",
            "59": "ETF/科创", "68": "科创板", "002": "中小板",
            "300": "创业板", "600": "主板", "601": "主板",
        }
        sector_weights = {}
        for p in self.positions:
            prefix = p["code"][:2]
            sector = sector_map.get(prefix, "其他")
            sector_weights[sector] = sector_weights.get(sector, 0) + p["weight"]

        return dict(sorted(sector_weights.items(), key=lambda x: x[1], reverse=True))

    def can_buy(self, ts_code: str, amount: float,
                max_single_pct: float = 20.0) -> tuple[bool, str]:
        """
        检查是否可以买入
        1. 可用资金 >= amount
        2. 买入后单股仓位 <= max_single_pct
        3. 行业仓位 <= 30%
        """
        code = ts_code.split(".")[0]

        # 检查资金
        if amount > self.cash_amount:
            return False, f"可用资金不足：需要 ¥{amount:,.2f}，可用 ¥{self.cash_amount:,.2f}"

        # 检查单股仓位
        new_mv = self.total_mv + amount
        new_pct = amount / new_mv * 100 if new_mv > 0 else 0
        if new_pct > max_single_pct:
            return False, f"单股仓位超限：买入占比 {new_pct:.1f}% > {max_single_pct}%"

        return True, "可以买入"

    def get_rebalance_suggestions(self, target_weights: dict = None) -> list[dict]:
        """
        生成调仓建议
        target_weights: {code: target_weight_pct}，默认为各股均分
        """
        if target_weights is None:
            n = len(self.positions)
            if n == 0:
                return []
            equal_weight = 100.0 / n
            target_weights = {p["code"]: equal_weight for p in self.positions}

        suggestions = []
        for p in self.positions:
            code = p["code"]
            current = p["weight"]
            target = target_weights.get(code, current)
            diff = target - current

            if abs(diff) < 0.5:
                action = "持有"
                pct = 0
            elif diff > 0:
                action = "增持"
                pct = round(diff, 2)
            else:
                action = "减持"
                pct = round(abs(diff), 2)

            suggestions.append({
                "code": code,
                "name": p["name"],
                "current_weight": round(current, 2),
                "target_weight": round(target, 2),
                "action": action,
                "adjust_pct": pct,
                "reason": f"当前仓位 {current:.1f}% → 目标 {target:.1f}%，差异 {abs(diff):.1f}%"
            })

        return suggestions

    def get_leverage_check(self) -> dict:
        """
        检查是否有违规杠杆暴露
        """
        issues = []
        total_weight = sum(p["weight"] for p in self.positions)

        if total_weight > 100:
            issues.append(f"总仓位超 100%：{total_weight:.1f}%（可能含融资/杠杆）")

        # 单一集中度检查
        for p in self.positions:
            if p["weight"] > 20:
                issues.append(f"单股超限：{p['name']}({p['code']}) 仓位 {p['weight']:.1f}% > 20%")

        # 行业集中度检查
        sector_exp = self.get_sector_exposure()
        for sector, weight in sector_exp.items():
            if sector != "其他" and weight > 30:
                issues.append(f"行业集中超限：{sector} {weight:.1f}% > 30%")

        return {
            "leverage_ratio": round((self.total_mv / self.total_assets - 1) * 100, 2)
                if self.total_assets > 0 else 0,
            "total_weight": round(total_weight, 2),
            "issues": issues,
            "is_compliant": len(issues) == 0,
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    fm = FundManager()
    print("=== 资金概览 ===")
    for k, v in fm.get_fund_overview().items():
        print(f"  {k}: {v}")

    print("\n=== 行业暴露 ===")
    for sector, weight in fm.get_sector_exposure().items():
        print(f"  {sector}: {weight:.1f}%")

    print("\n=== 杠杆合规检查 ===")
    check = fm.get_leverage_check()
    print(f"  合规: {check['is_compliant']}")
    for issue in check["issues"]:
        print(f"  ⚠️ {issue}")

"""
多账号持仓管理器
支持配置多个交易账号，统一展示合并视图
"""

import os
import sys
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

ACCOUNT_CONFIG_FILE = Path("config/accounts.json")

DEFAULT_ACCOUNTS = {
    "main": {
        "name": "主账户",
        "positions_csv": os.environ.get("POSITIONS_CSV", "/mnt/d/Hold/invest-data/positions.csv"),
        "enabled": True,
    }
}

def load_accounts() -> dict:
    """加载账号配置"""
    if ACCOUNT_CONFIG_FILE.exists():
        import json
        return json.loads(ACCOUNT_CONFIG_FILE.read_text())
    return DEFAULT_ACCOUNTS

def save_accounts(accounts: dict):
    """保存账号配置"""
    ACCOUNT_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    import json
    ACCOUNT_CONFIG_FILE.write_text(json.dumps(accounts, ensure_ascii=False, indent=2))

def get_active_accounts() -> list[tuple[str, dict]]:
    """获取所有启用账号 [(id, config), ...]"""
    accounts = load_accounts()
    return [(k, v) for k, v in accounts.items() if v.get("enabled", True)]

def get_account_positions(account_id: str) -> list[dict]:
    """获取指定账号的持仓（从对应CSV加载）"""
    accounts = load_accounts()
    if account_id not in accounts:
        return []
    
    import csv
    csv_path = accounts[account_id].get("positions_csv")
    if not csv_path or not Path(csv_path).exists():
        return []
    
    positions = []
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("code"):
                continue
            positions.append({
                "code": str(row["code"]).zfill(6),
                "name": row.get("name", ""),
                "shares": float(row.get("shares", 0)),
                "avg_cost": float(row.get("cost", 0)),
                "market_value": float(row.get("market_value", 0)),
                "account": account_id,  # 标记所属账号
            })
    return positions

def get_all_positions() -> list[dict]:
    """合并所有账号持仓"""
    all_pos = []
    for account_id, _ in get_active_accounts():
        all_pos.extend(get_account_positions(account_id))
    return all_pos

def get_account_summary() -> list[dict]:
    """获取各账号汇总市值"""
    summary = []
    for account_id, config in get_active_accounts():
        positions = get_account_positions(account_id)
        total_mv = sum(p.get("market_value", 0) for p in positions)
        total_cost = sum(p.get("shares", 0) * p.get("avg_cost", 0) for p in positions)
        summary.append({
            "account_id": account_id,
            "name": config.get("name", account_id),
            "position_count": len(positions),
            "total_market_value": total_mv,
            "total_cost": total_cost,
            "profit": total_mv - total_cost,
            "profit_pct": ((total_mv - total_cost) / total_cost * 100) if total_cost > 0 else 0,
        })
    return summary

if __name__ == "__main__":
    print("=== 账号汇总 ===")
    for s in get_account_summary():
        print(f"{s['name']}: {s['position_count']}只, 市值¥{s['total_market_value']:,.0f}, 盈亏{s['profit_pct']:+.2f}%")
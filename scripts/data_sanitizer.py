"""
data_sanitizer.py — 数据脱敏器
调用 DeepSeek API 前，所有敏感数据先脱敏
金额 → 百分比，股票代码 → 匿名 ID
"""

import random
import hashlib



# ─── 匿名化映射（全局保持一致）─────────────────────────────────────────────

_CODE_MAP: dict[str, str] = {}  # 真实代码 → 匿名ID
_ID_COUNTER = 0


def _get_anon_id(real_code: str) -> str:
    """将真实股票代码映射为匿名 ID（如 STK_001）"""
    global _ID_COUNTER
    if real_code not in _CODE_MAP:
        _ID_COUNTER += 1
        _CODE_MAP[real_code] = f"STK_{_ID_COUNTER:03d}"
    return _CODE_MAP[real_code]


def _get_real_code(anon_id: str) -> str:
    """从匿名 ID 还原真实代码"""
    for code, aid in _CODE_MAP.items():
        if aid == anon_id:
            return code
    return anon_id


def reset_mapping():
    """重置映射（每次分析任务开始时调用）"""
    global _CODE_MAP, _ID_COUNTER
    _CODE_MAP.clear()
    _ID_COUNTER = 0


# ─── 脱敏函数 ──────────────────────────────────────────────────────────────

def sanitize_snapshot(total_mv: float, positions: list[dict]) -> tuple[list[dict], dict]:
    """
    脱敏处理持仓快照：
    - 金额全部转为占总资产百分比
    - 股票代码替换为匿名 ID
    - 不传输实际股数，仅传输仓位百分比
    - 不传输历史盈亏具体数值，仅传输盈亏方向
    返回：(脱敏持仓列表, 反向映射表)
    """
    if total_mv <= 0:
        return [], {}

    sanitized = []
    id_mapping = {}

    for pos in positions:
        code = str(pos.get("code", "")).zfill(6)
        name = pos.get("name", "")
        mv = float(pos.get("market_value", 0))
        cost = float(pos.get("cost", 0))
        close = float(pos.get("close", cost))
        shares = pos.get("shares", 0)
        weight = pos.get("weight", mv / total_mv * 100)

        anon_id = _get_anon_id(code)
        id_mapping[anon_id] = {"code": code, "name": name}

        # 计算盈亏方向（非精确数值）
        pnl_pct = (close - cost) / cost * 100 if cost > 0 else 0
        if pnl_pct > 5:
            pnl_dir = "大幅盈利"
        elif pnl_pct > 0:
            pnl_dir = "小幅盈利"
        elif pnl_pct > -5:
            pnl_dir = "小幅亏损"
        else:
            pnl_dir = "大幅亏损"

        total_cost = cost * float(shares) if shares else cost
        cost_pct = round(total_cost / total_mv * 100, 2) if total_mv > 0 else 0

        sanitized.append({
            "anon_id": anon_id,
            "name": name,
            "cost_pct": cost_pct,
            "weight_pct": round(weight, 2),                  # 市值权重%
            "pnl_dir": pnl_dir,
            "pnl_pct": round(pnl_pct, 2),
        })

    return sanitized, id_mapping


def desensitize_plan(plan: dict, id_mapping: dict) -> dict:
    """
    将云端返回的计划中的匿名 ID 还原为真实股票代码
    """
    if "plans" not in plan:
        return plan

    desensitized = []
    for item in plan["plans"]:
        anon_id = item.get("anon_id", "")
        if anon_id in id_mapping:
            item["ts_code"] = id_mapping[anon_id]["code"]
            item["name"] = id_mapping[anon_id]["name"]
        else:
            item["ts_code"] = anon_id
        desensitized.append(item)

    plan["plans"] = desensitized
    return plan


# ─── 持仓成本还原 ─────────────────────────────────────────────────────────

def compute_real_shares(sanitized_item: dict, total_mv: float) -> dict:
    """
    给定脱敏后的单项数据，还原实际股数（用于执行层）
    注意：这是近似计算，精确股数需要通过交易所数据修正
    """
    weight_pct = sanitized_item.get("weight_pct", 0)
    target_mv = total_mv * weight_pct / 100
    price = sanitized_item.get("close", 0)
    if price > 0:
        estimated_shares = int(target_mv / price / 100) * 100  # 取整到百股
    else:
        estimated_shares = 0

    return {
        **sanitized_item,
        "estimated_shares": estimated_shares,
        "estimated_mv": round(target_mv, 2),
    }


# ─── 脱敏报告生成 ──────────────────────────────────────────────────────────

def print_sanitized_report(sanitized: list[dict], total_mv: float):
    """打印脱敏后的持仓报告（供调试）"""
    print("\n" + "-" * 50)
    print(f"📊 脱敏持仓快照（总市值: ¥{total_mv:,.2f}）")
    print("-" * 50)
    print(f"{'匿名ID':<10} {'名称':<12} {'仓位%':>8} {'成本占比%':>10} {'盈亏方向':>10}")
    print("-" * 50)
    for pos in sanitized:
        print(f"{pos['anon_id']:<10} {pos['name']:<12} "
              f"{pos['weight_pct']:>8.2f}% {pos['cost_pct']:>10.2f}% {pos['pnl_dir']:>10}")
    print("-" * 50)

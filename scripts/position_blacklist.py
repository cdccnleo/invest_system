"""
持仓采集黑名单 helper。
统一从 config/position_blacklist.json 读取，所有采集/分析/展示环节都应调用。

过滤维度:
  - by code: 6位代码精确匹配
  - by name_substr: 名称子串匹配（兼容历史名称变体）

提供:
  - load_blacklist()         -> dict[code, entry]
  - is_blacklisted(code, name) -> bool
  - filter_positions(list[dict]) -> list[dict]   # 原地移除黑名单标的
  - filter_csv_rows(rows)    -> (kept, dropped)  # 返回 (保留行, 被过滤行)
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("invest_system.position_blacklist")

CONFIG_PATH = Path(__file__).parent.parent / "config" / "position_blacklist.json"


@lru_cache(maxsize=1)
def load_blacklist() -> dict[str, dict]:
    """加载黑名单 {code: entry}。lru_cache 避免每次调用都读盘。"""
    if not CONFIG_PATH.exists():
        logger.debug(f"黑名单配置文件不存在: {CONFIG_PATH}")
        return {}
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        items = cfg.get("blacklist", []) or []
        return {str(item["code"]).zfill(6): item for item in items if item.get("code")}
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"黑名单配置文件解析失败: {e}")
        return {}


def is_blacklisted(code: str, name: str = "") -> bool:
    """判断 (code, name) 是否在黑名单。匹配规则：code 完全匹配，或 name 包含黑名单条目的子串。"""
    if not code:
        return False
    bl = load_blacklist()
    code_norm = str(code).zfill(6)
    if code_norm in bl:
        return True
    name_l = (name or "").lower()
    for entry in bl.values():
        for substr in entry.get("name_substr_list", []) or [entry.get("name", "")]:
            if substr and substr in name_l:
                return True
    return False


def filter_positions(positions: Iterable[dict]) -> list[dict]:
    """过滤掉黑名单持仓。返回新列表（不修改原列表）。"""
    kept, dropped = filter_with_details(positions)
    if dropped:
        dropped_str = ", ".join(
            f"{p.get('code', '?')}({p.get('name', '')})" for p in dropped
        )
        logger.info(f"黑名单过滤: 移除 {len(dropped)} 条 ({dropped_str})")
    return kept


def filter_with_details(positions: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """过滤 + 返回被过滤明细（用于日志/告警）"""
    kept: list[dict] = []
    dropped: list[dict] = []
    for p in positions:
        if is_blacklisted(p.get("code", ""), p.get("name", "")):
            dropped.append(p)
        else:
            kept.append(p)
    return kept, dropped


def filter_csv_rows(rows: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """同 filter_with_details，但针对 csv.DictReader 行 (key 可能是 code/name 而非 code/name)"""
    return filter_with_details(rows)


if __name__ == "__main__":
    # 自我测试
    logging.basicConfig(level=logging.INFO)
    bl = load_blacklist()
    print(f"已加载 {len(bl)} 条黑名单:")
    for code, entry in bl.items():
        print(f"  {code} {entry.get('name')}: {entry.get('reason')}")
    print()
    print("测试 filter_positions:")
    test = [
        {"code": "404002", "name": "搜特退债", "market_value": 23.62},
        {"code": "600183", "name": "生益科技", "market_value": 40218},
        {"code": "007355", "name": "汇添富科技创新混合A", "market_value": 498960},
    ]
    kept = filter_positions(test)
    print(f"  输入 {len(test)} 条, 保留 {len(kept)} 条:")
    for p in kept:
        print(f"    {p['code']} {p['name']}")

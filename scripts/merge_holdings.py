#!/usr/bin/env python3
"""merge_holdings.py — 把 D:\\Hold\\ 根目录的 4 个券商/基金持仓 CSV 汇总到
D:\\Hold\\invest-data\\positions.csv

输入 (4 个 CSV, 来源各家券商/基金公司 app 导出):
  D:\\Hold\\国金证券持仓YYYYMMDD.csv     — 25+ 行股票
  D:\\Hold\\广发基金持仓YYYYMMDD.csv     — 12+ 行场内基金
  D:\\Hold\\天天基金持仓YYYYMMDD.csv     — 1 行 场外基金
  D:\\Hold\\汇添富基金持仓YYYYMMDD.csv   — 1+ 行 场外基金 (多笔可能, 按代码汇总)

输出 (标准化 9 列 CSV):
  account, code, name, type, shares, cost, date, market_value, weight

输出 schema 与 account_manager.get_all_positions() 兼容 (它读 market_value/shares 等字段)

设计要点:
  - 选最近日期的同名文件 (国金可能存在 国金持仓0513.csv 和 国金证券持仓0608.csv)
  - code 补 0 到 6 位 (东方财富接口要求)
  - 汇添富按 code 聚合多笔 (同一只基金可能分多笔购买, 合并 1 行)
  - 写文件前用临时文件 + 原子 rename, 防止半写状态
"""

from __future__ import annotations

import csv
import glob
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HOLD_DIR = Path("/mnt/d/Hold")  # WSL 路径
OUTPUT_CSV = HOLD_DIR / "invest-data" / "positions.csv"

# 各源 CSV 关键词匹配 (按 mtime 选最近)
SOURCES = {
    "国金证券": ["国金证券持仓", "国金持仓"],
    "广发基金": ["广发基金持仓", "广发持仓"],  # 场内基金
    "天天基金": ["天天基金持仓", "天天基金"],
    "汇添富基金": ["汇添富基金持仓", "汇添富基金"],
}

# type 分类规则
FUND_FUND_TYPE_KEYWORDS = ("基金", "LOF", "ETF")  # 实际按 code 前缀更可靠


def _pick_latest_csv(hold_dir: Path, keywords: list[str]) -> Path | None:
    """在 hold_dir 根目录找含任一 keyword 的 csv, 取 mtime 最新"""
    candidates: list[tuple[float, Path]] = []
    for kw in keywords:
        for p in hold_dir.glob(f"{kw}*.csv"):
            try:
                candidates.append((p.stat().st_mtime, p))
            except OSError:
                continue
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[0])[1]


def _parse_money(s: str) -> float:
    """'145,044.90' / '5,323' / '\\xa044,776.25' → 531514.59 / float"""
    if not s:
        return 0.0
    s = s.replace(",", "").replace("\xa0", "").strip()
    if not s or s == "-":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _read_csv_robust(path: Path) -> list[list[str]]:
    """读 CSV, 兼容 BOM 和全角逗号"""
    with open(path, encoding="utf-8-sig") as f:
        # csv.reader 容忍多余列
        return list(csv.reader(f))


def _date_str_from_filename(path: Path) -> str:
    """从 '国金证券持仓20260608.csv' 提取 '2026-06-08'"""
    m = re.search(r"(\d{8})", path.stem)
    if m:
        s = m.group(1)
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return datetime.now().strftime("%Y-%m-%d")


def _is_fund_code(code: str) -> bool:
    """根据代码前缀判断类型: 6 位 5/6 开头 = 沪市 ETF, 1/2/3 开头 = 深市 ETF, 0 开头 = 深市 LOF/股票"""
    if not code:
        return False
    # 场内 ETF: 5 开头 (沪) / 1 开头 (深)
    # 场外基金 (天天/汇添富): 通常纯数字 6 位, 002943/007355/7355 等
    # 简单规则: 4 个 CSV 中, 国金和广发是场内 (stock/ETF), 天天/汇添富是场外基金 (fund)
    return False  # 实际 type 由 source 决定, 不靠 code 推断


def parse_gj_stock(path: Path) -> list[dict]:
    """国金证券 CSV: 顶部有 1 行表头 (币种余额...), 1 行账户余额, 1 行空行, 然后才是实际持仓表头
    持仓行格式: 证券代码, 证券名称, 长简称, 证券数量, 可卖数量, 今买数量, 今卖数量,
                参考成本价, 当前价, 最新市值, 参考浮动盈亏, 参考盈亏比例%, 仓位(%)
    """
    rows = _read_csv_robust(path)
    # 找表头行: 含 "证券代码"
    header_idx = None
    for i, r in enumerate(rows):
        if r and r[0].strip() == "证券代码":
            header_idx = i
            break
    if header_idx is None:
        return []
    header = [c.strip() for c in rows[header_idx]]
    result = []
    for r in rows[header_idx + 1:]:
        if not r or len(r) < len(header) or not r[0].strip():
            continue
        try:
            code = r[0].strip().zfill(6)
            name = r[1].strip()
            shares = float(r[3]) if r[3] else 0
            cost = abs(float(r[7])) if r[7] else 0  # 参考成本价常为负数
            market_value = _parse_money(r[9])
            if shares <= 0 or market_value <= 0:
                continue
            result.append({
                "code": code,
                "name": name,
                "type": "stock",  # 国金都是股票
                "shares": shares,
                "cost": cost,
                "market_value": market_value,
            })
        except (ValueError, IndexError):
            continue
    return result


def parse_gf_fund(path: Path) -> list[dict]:
    """广发基金 (场内 ETF) CSV:
    证券名称, 证券代码, 可用数量, 当前数量, ..., 成本价, 市值价, 市值, 浮动盈亏, 盈亏比, 当日盈亏, ...
    """
    rows = _read_csv_robust(path)
    if not rows:
        return []
    header = [c.strip() for c in rows[0]]
    # 列索引
    try:
        i_code = header.index("证券代码")
        i_name = header.index("证券名称")
        i_shares = header.index("当前数量")
        i_cost = header.index("成本价")
        i_mv = header.index("市值")
    except ValueError:
        return []
    result = []
    for r in rows[1:]:
        if not r or len(r) <= max(i_code, i_name, i_shares, i_cost, i_mv):
            continue
        try:
            code = r[i_code].strip().zfill(6)
            if not code:
                continue
            name = r[i_name].strip()
            shares = _parse_money(r[i_shares])
            cost = _parse_money(r[i_cost])
            mv = _parse_money(r[i_mv])
            if shares <= 0 or mv <= 0:
                continue
            # 区分场内 ETF 和股票
            if "ETF" in name or "LOF" in name:
                t = "etf"           # 场内 ETF: cost = 单价
            else:
                t = "stock"         # 股票: cost = 单价
            result.append({
                "code": code,
                "name": name,
                "type": t,
                "shares": shares,
                "cost": cost,
                "market_value": mv,
            })
        except (ValueError, IndexError):
            continue
    return result


def parse_tiantian_fund(path: Path) -> list[dict]:
    """天天基金 CSV: 8 列, 数据从第 2 行开始, 第 3 行是收益摘要 (跳过)
    产品代码, 产品名称, 产品类型, 最新净值, 净值日期, 金额（元）, 持仓收益（元）, 持仓收益（%）
    """
    rows = _read_csv_robust(path)
    result = []
    for r in rows[1:]:  # 跳过表头
        if not r or len(r) < 6:
            continue
        # 跳过非数据行 (如 "持仓收益(元)：..." 摘要行)
        if not r[0].strip().isdigit() and not re.match(r"^\d{6}$", r[0].strip()):
            continue
        try:
            code = r[0].strip().zfill(6)
            if len(code) != 6 or not code.isdigit():
                continue
            name = r[1].strip()
            mv = _parse_money(r[5])  # 金额
            if mv <= 0:
                continue
            # 成本 = 金额 - 持仓收益 (估算)
            profit = _parse_money(r[6]) if len(r) > 6 else 0
            cost = max(0.0, mv - profit)
            # 场外基金份额 → 估值 (按 NAV 反推)
            nav = _parse_money(r[3])
            shares = mv / nav if nav > 0 else 0
            result.append({
                "code": code,
                "name": name,
                "type": "fund",
                "shares": shares,
                "cost": cost,
                "market_value": mv,
            })
        except (ValueError, IndexError):
            continue
    return result


def parse_huitianfu_fund(path: Path) -> list[dict]:
    """汇添富基金 CSV: 11 列, 同一只基金可能多笔 (如 007355 分 5 笔)
    基金代码, 基金名称, 净值日期, 最新净值, 收费方式, 持有份额, 可用份额, 不可用份额,
    参考市值, 投入本金, 持有盈亏/本期盈亏
    同一 code 多行需汇总: 份额相加, 市值相加, 成本相加
    """
    rows = _read_csv_robust(path)
    aggregated: dict[str, dict] = {}
    for r in rows[1:]:  # 跳过表头
        if not r or len(r) < 11:
            continue
        if not re.match(r"^\d{3,6}$", r[0].strip()):
            continue
        try:
            code = r[0].strip().zfill(6)
            if len(code) != 6:
                continue
            name = r[1].strip()
            shares = _parse_money(r[5])
            mv = _parse_money(r[8])  # 参考市值
            cost = _parse_money(r[9])  # 投入本金
            if code in aggregated:
                aggregated[code]["shares"] += shares
                aggregated[code]["market_value"] += mv
                aggregated[code]["cost"] += cost
            else:
                aggregated[code] = {
                    "code": code,
                    "name": name,
                    "type": "fund",
                    "shares": shares,
                    "cost": cost,
                    "market_value": mv,
                }
        except (ValueError, IndexError):
            continue
    return [v for v in aggregated.values() if v["shares"] > 0 and v["market_value"] > 0]


def _compute_weight(positions: list[dict]) -> list[dict]:
    """追加 weight 字段 = market_value / total_mv * 100"""
    total = sum(p["market_value"] for p in positions) or 1.0
    for p in positions:
        p["weight"] = round(p["market_value"] / total * 100, 2)
    return positions


def merge(hold_dir: Path = HOLD_DIR, output: Path = OUTPUT_CSV) -> dict:
    """主入口: 找 4 个 CSV → 解析 → 合并 → 写 positions.csv

    返回 dict {source: (path_found, count_parsed, error)}
    """
    results: dict[str, dict] = {}

    # 1. 选最近 CSV
    picked: dict[str, Path] = {}
    for source, keywords in SOURCES.items():
        p = _pick_latest_csv(hold_dir, keywords)
        if p:
            picked[source] = p

    # 2. 解析
    parsers = [
        ("国金证券", parse_gj_stock),
        ("广发基金", parse_gf_fund),
        ("天天基金", parse_tiantian_fund),
        ("汇添富基金", parse_huitianfu_fund),
    ]
    all_positions: list[dict] = []
    for source, parser in parsers:
        path = picked.get(source)
        if not path:
            results[source] = {"path": None, "count": 0, "error": "未找到 CSV"}
            continue
        try:
            parsed = parser(path)
            # 标注 account + date
            date_str = _date_str_from_filename(path)
            for p in parsed:
                p["account"] = source
                p["date"] = date_str
            all_positions.extend(parsed)
            results[source] = {
                "path": str(path),
                "count": len(parsed),
                "error": None,
                "date": date_str,
            }
        except Exception as e:
            results[source] = {"path": str(path), "count": 0, "error": str(e)}

    # 3. 计算 weight
    all_positions = _compute_weight(all_positions)

    # 4. 写文件 (原子: 临时文件 + rename, 不带 BOM 与原文件保持兼容)
    if all_positions:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(output.parent),
            prefix=".positions_",
            suffix=".csv.tmp",
            delete=False,
            newline="",
        ) as tf:
            tmp_path = Path(tf.name)
            writer = csv.DictWriter(
                tf,
                fieldnames=["account", "code", "name", "type", "shares", "cost",
                            "date", "market_value", "weight"],
            )
            writer.writeheader()
            for p in all_positions:
                writer.writerow({
                    "account": p["account"],
                    "code": p["code"],
                    "name": p["name"],
                    "type": p["type"],
                    "shares": f"{p['shares']:.2f}",
                    "cost": f"{p['cost']:.4f}",
                    "date": p["date"],
                    "market_value": f"{p['market_value']:.2f}",
                    "weight": f"{p['weight']:.2f}",
                })
        # 原子 rename
        shutil.move(str(tmp_path), str(output))

    return {
        "output": str(output),
        "total": len(all_positions),
        "sources": results,
    }


if __name__ == "__main__":
    import json
    hold_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else HOLD_DIR
    output = Path(sys.argv[2]) if len(sys.argv) > 2 else OUTPUT_CSV
    result = merge(hold_dir, output)
    print(json.dumps(result, ensure_ascii=False, indent=2))

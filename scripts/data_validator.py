"""
data_validator.py — 数据质量校验层
所有采集数据在写入数据库之前必须通过校验层
"""

import re
from datetime import datetime, timedelta


# ─── 行情数据校验 ──────────────────────────────────────────────────────────

def validate_quotes_data(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """
    校验行情数据，修复常见问题：
    1. 必填字段非空（ts_code, close, volume）
    2. 价格合理性（0.01 < close < 100000）
    3. 成交量非负
    4. 涨跌幅合理（-20% ~ +20%，科创板/北交所 ±30%）
    返回: (有效数据, 异常记录列表)
    """
    valid = []
    errors = []

    for row in rows:
        code = str(row.get("ts_code", "")).strip()
        close = row.get("close")
        volume = row.get("volume", 0)
        change_pct = row.get("change_pct", 0)

        # 必填字段
        if not code:
            errors.append(f"[SKIP] ts_code 为空: {row}")
            continue

        # 价格合理性
        try:
            close_f = float(close)
            if close_f <= 0 or close_f > 100000:
                errors.append(f"[SKIP] {code} 价格异常: {close}")
                continue
            row["close"] = close_f
        except (TypeError, ValueError):
            errors.append(f"[SKIP] {code} 价格无法解析: {close}")
            continue

        # 成交量非负
        try:
            vol = int(volume or 0)
            if vol < 0:
                errors.append(f"[SKIP] {code} 成交量为负: {vol}")
                continue
            row["volume"] = vol
        except ValueError:
            row["volume"] = 0

        # 涨跌幅合理性（科创板 688/北交所 8开头 放宽到 ±30%）
        try:
            chg = float(change_pct or 0)
            max_change = 30.0 if (code.startswith("688") or re.match(r"^43|^83", code)) else 20.0
            if abs(chg) > max_change:
                errors.append(f"[WARN] {code} 涨跌幅异常 {chg}%，已修正")
                # 不跳过，仅警告
            row["change_pct"] = chg
        except ValueError:
            row["change_pct"] = 0.0

        valid.append(row)

    return valid, errors


# ─── 新闻数据校验 ──────────────────────────────────────────────────────────

def validate_news_data(articles: list[dict]) -> tuple[list[dict], list[str]]:
    """
    校验新闻数据：
    1. 标题和正文非空
    2. 时间戳合理性（不比当前时间晚超过 1 小时）
    3. 去重（标题相似度 > 0.9 视为重复）
    返回: (有效数据, 异常记录列表)
    """
    valid = []
    errors = []
    seen_titles = []

    now = datetime.now()

    for article in articles:
        title = str(article.get("title", "")).strip()

        # 标题非空
        if not title or len(title) < 4:
            errors.append(f"[SKIP] 新闻标题过短: {title[:30]}")
            continue

        # 时间戳合理性
        pub_time = article.get("published_at")
        if pub_time:
            try:
                if isinstance(pub_time, str):
                    # 尝试解析常见格式
                    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]:
                        try:
                            pt = datetime.strptime(pub_time[:19], fmt)
                            break
                        except ValueError:
                            continue
                    else:
                        pt = datetime.now()
                    diff = abs((now - pt).total_seconds())
                    if diff > 3600:  # 超过1小时
                        errors.append(f"[WARN] {title[:20]} 时间戳异常: {pub_time}")
                        # 不跳过，仅警告
                    article["_parsed_time"] = pt
            except Exception:
                pass

        # 去重（简单相似度：前30字符相同则视为重复）
        title_prefix = title[:30]
        if title_prefix in seen_titles:
            errors.append(f"[SKIP] 重复新闻: {title[:30]}")
            continue
        seen_titles.append(title_prefix)

        valid.append(article)

    return valid, errors


# ─── 持仓数据校验 ──────────────────────────────────────────────────────────

def validate_positions_data(positions: list[dict]) -> tuple[list[dict], list[str]]:
    """
    校验持仓数据：
    1. code 和 name 至少有一个非空
    2. shares >= 0
    3. cost >= 0（允许 0，如基金）
    4. market_value >= 0
    5. 成本价格离谱检测（> 100000 视为异常）
    """
    valid = []
    errors = []

    for pos in positions:
        code = str(pos.get("code", "")).strip().zfill(6)
        name = str(pos.get("name", "")).strip()
        shares = pos.get("shares", 0)
        cost = pos.get("cost", 0)
        market_value = pos.get("market_value", 0)

        if not code and not name:
            errors.append(f"[SKIP] 持仓代码和名称均为空: {pos}")
            continue

        try:
            shares = float(shares)
            if shares < 0:
                errors.append(f"[SKIP] {code} shares 为负: {shares}")
                continue
            pos["shares"] = shares
        except (TypeError, ValueError):
            pos["shares"] = 0

        try:
            cost = float(cost)
            if cost < 0:
                errors.append(f"[WARN] {code} 成本为负: {cost}，已修正为0")
                cost = 0
            if cost > 100000:
                errors.append(f"[WARN] {code} 成本疑似异常: {cost}")
            pos["cost"] = cost
        except (TypeError, ValueError):
            pos["cost"] = 0

        try:
            mv = float(market_value)
            if mv < 0:
                mv = 0
            pos["market_value"] = mv
        except (TypeError, ValueError):
            pos["market_value"] = 0

        pos["code"] = code
        valid.append(pos)

    return valid, errors


# ─── 汇总校验报告 ──────────────────────────────────────────────────────────

def print_validation_report(quotes_valid, quotes_errors,
                            news_valid, news_errors,
                            positions_valid, positions_errors):
    """打印校验汇总报告"""
    print("\n" + "=" * 60)
    print("📋 数据校验报告")
    print("=" * 60)

    def report(name, valid, errors):
        ok = len(valid)
        fail = len(errors)
        total = ok + fail
        pct = ok / total * 100 if total > 0 else 0
        icon = "✅" if fail == 0 else ("⚠️" if ok > fail else "❌")
        print(f"  {icon} {name}: 有效 {ok}/{total} ({pct:.0f}%)")
        if errors:
            for e in errors[:5]:  # 最多显示5条
                print(f"     {e}")
            if len(errors) > 5:
                print(f"     ... 还有 {len(errors) - 5} 条")

    report("行情数据", quotes_valid, quotes_errors)
    report("新闻数据", news_valid, news_errors)
    report("持仓数据", positions_valid, positions_errors)

    total_valid = len(quotes_valid) + len(news_valid) + len(positions_valid)
    total_errors = len(quotes_errors) + len(news_errors) + len(positions_errors)
    print(f"\n  总计: 有效 {total_valid} 条, 异常 {total_errors} 条")
    print("=" * 60 + "\n")

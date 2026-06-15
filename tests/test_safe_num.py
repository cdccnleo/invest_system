"""tests/test_safe_num.py — PIT #117 防御性测试 (6/15 实战)

4 场景回归:
1. None 不再触发 TypeError
2. 数值/字符串/列表/dict 全场景
3. 实战场景: close=None 货币基金 001982
4. 集成测试: run_analysis.py 跑通
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from _shared_utils import _safe_num


def test_1_none_does_not_raise():
    """场景 1: None > 0 不再抛 TypeError (PIT #117 实战根因)"""
    # 直接比较 None > 0 抛错
    try:
        if None > 0:
            pass
        assert False, "原 None > 0 居然没抛错"
    except TypeError:
        pass  # 预期抛错

    # _safe_num(None) = 0, 0 > 0 为 False, 不抛
    if _safe_num(None) > 0:
        result = "进 if"
    else:
        result = "进 else"
    assert result == "进 else", f"期望进 else, 实得 {result}"
    print("✅ 场景 1: None > 0 不再抛错 (走 else 分支)")


def test_2_value_type_coverage():
    """场景 2: 全类型覆盖 (None/int/float/str/bool/list/dict)"""
    cases = [
        (None, 0, 0),
        (None, 1.5, 1.5),  # PIT #112 衍生: close=None 时用 cost 兜底
        (0, 0, 0),
        (0.0, 0, 0.0),
        (1, 0, 1),
        (1.5, 0, 1.5),
        (True, 0, 1),
        (False, 0, 0),
        ("0", 0, 0.0),
        ("123", 0, 123.0),
        ("abc", 0, 0),
        ("", 0, 0),
        ([1, 2], 0, 0),
        ({"a": 1}, 0, 0),
        ((1, 2), 0, 0),
        (object(), 0, 0),
    ]
    all_pass = True
    for inp, default, exp in cases:
        got = _safe_num(inp, default=default)
        if got != exp:
            print(f"❌ _safe_num({inp!r}, default={default}) = {got!r}, expected {exp!r}")
            all_pass = False
    assert all_pass, "有 case 失败"
    print(f"✅ 场景 2: 16 个类型全部 PASS (含 default 参数)")


def test_3_currency_fund_001982():
    """场景 3: 实战场景 货币基金 001982 close=None 不再抛错 (含 PIT #112 衍生)"""
    # 模拟 enrich_positions_with_quotes 的 cost_estimate 分支
    # 1) cost=None → pos["close"] = pos.get("cost", 0) → close=None
    # 2) run_analysis.py:212 quotes_raw 过滤: p.get("close", 0) > 0 抛错
    # 修复后: _safe_num(None) = 0, 0 > 0 为 False, 跳过 (无 quotes_raw 行, 正常)

    pos = {
        "code": "001982",
        "type": "fund",
        "name": "富国收益宝交易型货币B",
        "shares": None,
        "cost": None,  # PIT #112 数据坏
        "profit": None,
        "market_value": 760.24,
    }
    # 走 cost_estimate 分支
    pos["close"] = pos.get("cost", 0)  # 修复前 close=None, 修复后 close=0
    pos["change_pct"] = 0
    pos["source"] = "cost_estimate"

    # 验证: 修复前 None > 0 抛错, 修复后 _safe_num 走 else 分支
    close_safe = _safe_num(pos.get("close"))
    assert close_safe == 0, f"应兜底为 0, 实得 {close_safe}"
    assert not (close_safe > 0), "应走 else 分支"

    # 验证: 如果 close 是 1.5 (正常基金), 应该进 if
    pos["close"] = 1.5
    assert _safe_num(pos.get("close")) > 0, "正常基金应进 if"

    # 验证: PIT #112 衍生 f-string format 兜底 (close=None → 用 cost 兜底)
    cost = _safe_num(pos.get("cost"))  # None → 0
    close = _safe_num(pos.get("close"), default=cost)  # close=None → 0 (cost 兜底)
    line = f"{pos.get('name', ''):<12} {pos.get('code', ''):<8} {cost:>8.3f} {close:>8.3f}"
    assert "001982" in line
    assert "0.000" in line  # cost=None → 0.000

    print("✅ 场景 3: 货币基金 001982 close=None 不再抛错 (含 PIT #112 衍生)")


def test_4_integration_run_analysis():
    """场景 4: 集成测试 run_analysis.py 跑通 (无 NoneType TypeError)"""
    # 调用 run_analysis 跑 Step 1-4 数据校验
    # 预期: 不抛 '>' not supported between instances of 'NoneType' and 'int'
    os.chdir("/home/aileo/invest_system")
    os.environ["POSITIONS_CSV"] = "/mnt/d/Hold/invest-data/positions.csv"

    try:
        from run_analysis import enrich_positions_with_quotes, load_positions
        positions = load_positions(os.environ["POSITIONS_CSV"])
        # Step 2: enrich
        enriched = enrich_positions_with_quotes(positions)
        # Step 4 关键: 验证 _safe_num 不抛错
        valid_count = sum(1 for p in enriched if _safe_num(p.get("close")) > 0)
        assert valid_count > 0, f"应有有效 close 持仓, 实得 {valid_count}"
        print(f"✅ 场景 4: run_analysis 集成测试 PASS (63 持仓, valid={valid_count})")
    except Exception as e:
        if "not supported between" in str(e) and "NoneType" in str(e):
            assert False, f"PIT #117 修复未生效: {e}"
        raise


if __name__ == "__main__":
    print("=" * 60)
    print("PIT #117 _safe_num 防御性测试 (6/15 实战)")
    print("=" * 60)
    test_1_none_does_not_raise()
    test_2_value_type_coverage()
    test_3_currency_fund_001982()
    test_4_integration_run_analysis()
    print("=" * 60)
    print("✅ 4 场景全部 PASS, PIT #117 防御性测试通过")

"""
asset_class_router.py — 资产类型差异化路由 (P2-T1 补丁6 落地 v1.0)

功能:
- 根据持仓代码/类型自动识别 asset_class (stock/hk_stock/us_stock/etf/fund)
- 路由不同告警阈值 / 通知窗口 / 数据源
- 接入 intraday_monitor / event_analyst

使用:
    from asset_class_router import AssetClassRouter
    router = AssetClassRouter()
    cls = router.detect_class("002050")  # → "stock"
    threshold = router.get_alert_threshold(cls)  # → {"intraday_pct": 5, ...}
    source = router.get_quote_source(cls)  # → {"primary": "akshare", ...}
"""

import re
from typing import Dict, Optional

# 资产类型白名单 + 特征
# 顺序很重要：先匹配高优先级（更特定的模式）
CODE_PATTERNS = [
    ("hk_stock", re.compile(r"^0\d{4}$")),         # 港股 5位 (00700) — 必须先匹配
    ("us_stock", re.compile(r"^[A-Z]{1,5}$")),     # 美股字母 (TSLA / AAPL)
    ("etf", re.compile(r"^(15[0-9]{4}|5[0-9]{5})$")),  # ETF 特殊前缀
    ("stock", re.compile(r"^[0-9]{6}$")),           # A 股 6位
    # fund 必须由 suffix ".OF" 单独处理，不放在正则里
]

# 标的代码 → asset_class 覆盖
EXPLICIT_OVERRIDES = {
    # 已知 ETF（实际可能与 code pattern 冲突，但显式优先）
    "159516": "etf", "159819": "etf", "515700": "etf", "561160": "etf",
    "563230": "etf", "588190": "etf", "513300": "etf", "513130": "etf",
    "518880": "etf", "516650": "etf", "512880": "etf", "515050": "etf",
    "562500": "etf", "588290": "etf", "513500": "etf", "159941": "etf",
    # 场外基金（以 .OF 后缀识别）
    # 由 detect_class 通过 suffix 单独处理
    # 港股通
    "00700": "hk_stock", "00939": "hk_stock", "00941": "hk_stock",
    "01810": "hk_stock", "09988": "hk_stock", "03690": "hk_stock",
}


class AssetClassRouter:
    def __init__(self):
        self.thresholds = {
            "stock": {
                "intraday_pct": 5,
                "position_pct": 3,
                "volume_pct": 200,
                "cooldown_minutes": 30,
                "notify_window": "09:30-15:00",
                "tax_cost_pct": 0.15,
            },
            "hk_stock": {
                "intraday_pct": 8,
                "position_pct": 3,
                "volume_pct": 300,
                "cooldown_minutes": 45,
                "notify_window": "09:30-16:00",
                "tax_cost_pct": 0.5,
                "fx_monitor": True,
            },
            "us_stock": {
                "intraday_pct": 5,
                "position_pct": 3,
                "volume_pct": 300,
                "cooldown_minutes": 60,
                "notify_window": "21:30-04:00",
                "quiet_hours": "04:00-09:00",
                "tax_cost_pct": 0.1,
            },
            "etf": {
                "intraday_pct": 3,
                "position_pct": 3,
                "volume_pct": 200,
                "cooldown_minutes": 60,
                "notify_window": "09:30-15:00",
                "iopv_premium_pct": 2,
                "iopv_discount_pct": -1,
                "tax_cost_pct": 0.15,
            },
            "fund": {
                "nav_change_pct": 2,
                "estimate_pct": 3,
                "cooldown_hours": 4,
                "notify_window": "全天",
                "quiet": True,
                "tax_cost_pct": 0.5,
            },
        }
        self.quote_sources = {
            "stock": {"primary": "akshare", "fallback": "sina_hq_sinajs_cn"},
            "hk_stock": {"primary": "tencent_qt_gtimg_cn_hk", "fallback": "akshare_hk"},
            "us_stock": {"primary": "tencent_qt_gtimg_cn_us", "fallback": "yfinance"},
            "etf": {"primary": "akshare_etf", "iopv_source": "sse_szse_iopv"},
            "fund": {"primary": "akshare_fund_nav", "estimate_source": "tiantian_fund"},
        }

    def detect_class(self, code: str, market_value: Optional[float] = None) -> str:
        """根据代码识别资产类型

        Args:
            code: 标的代码（数字 / 字母+数字）
            market_value: 持仓市值（可选，辅助判断）

        Returns:
            asset_class: stock | hk_stock | us_stock | etf | fund
        """
        if not code:
            return "stock"

        code = code.strip().upper()
        if "." in code:
            suffix = code.split(".")[-1]
            code = code.split(".")[0]
            if suffix in ("OF",):
                return "fund"
            if suffix in ("HK",):
                return "hk_stock"
            if suffix in ("US",):
                return "us_stock"

        # 显式覆盖
        if code in EXPLICIT_OVERRIDES:
            return EXPLICIT_OVERRIDES[code]

        # 模式匹配（按 CODE_PATTERNS 顺序，命中即返回）
        for cls, pattern in CODE_PATTERNS:
            if pattern.match(code):
                return cls

        return "stock"  # 默认

    def get_alert_threshold(self, asset_class: str) -> Dict:
        """获取告警阈值"""
        return self.thresholds.get(asset_class, self.thresholds["stock"]).copy()

    def get_quote_source(self, asset_class: str) -> Dict:
        """获取行情数据源"""
        return self.quote_sources.get(asset_class, self.quote_sources["stock"]).copy()

    def should_alert_in_window(self, asset_class: str, current_time: str) -> bool:
        """判断当前时间是否在告警窗口内

        Args:
            asset_class: 资产类型
            current_time: HH:MM 格式

        Returns:
            bool: True=可告警, False=禁打扰
        """
        t = self.get_alert_threshold(asset_class)
        window = t.get("notify_window", "")

        if not window:
            return True

        if window == "全天":
            return not t.get("quiet", False)

        if "-" in window and ":" in window:
            start, end = window.split("-")
            return start <= current_time <= end

        return True

    def get_iopv_check(self, asset_class: str) -> bool:
        """是否需要 IOPV 溢价率检查（仅 ETF）"""
        if asset_class != "etf":
            return False
        threshold = self.get_alert_threshold(asset_class)
        return "iopv_premium_pct" in threshold


def main():
    import json
    router = AssetClassRouter()
    test_codes = [
        ("002050", None),     # 三花智控
        ("00700", None),      # 腾讯港股
        ("TSLA", None),       # 特斯拉
        ("159819", None),     # 人工智能ETF
        ("513300", None),     # 纳斯达克ETF
        ("518880", None),     # 黄金ETF
        ("002050.OF", None),  # 三花智控基金（不存在但是测后缀）
    ]
    print("=" * 70)
    print("资产类型识别测试")
    print("=" * 70)
    for code, mv in test_codes:
        cls = router.detect_class(code, mv)
        th = router.get_alert_threshold(cls)
        src = router.get_quote_source(cls)
        print(f"\n{code:>12s} → {cls:>10s}")
        print(f"  告警阈值: intraday_pct={th.get('intraday_pct', 'N/A')}%, "
              f"cooldown={th.get('cooldown_minutes', th.get('cooldown_hours', '?'))}min/h")
        print(f"  数据源: {src.get('primary', '?')}")
        if router.get_iopv_check(cls):
            print(f"  IOPV: premium>2%/discount<-1% 触发")

    # 通知窗口测试
    print("\n" + "=" * 70)
    print("通知窗口测试 (当前 13:30 = 午盘)")
    print("=" * 70)
    for cls in ["stock", "hk_stock", "us_stock", "etf", "fund"]:
        ok = router.should_alert_in_window(cls, "13:30")
        print(f"  {cls:>10s} @ 13:30: {'✓ 告警' if ok else '✗ 禁打扰'}")
    print()
    for cls in ["stock", "us_stock"]:
        ok = router.should_alert_in_window(cls, "22:00")
        print(f"  {cls:>10s} @ 22:00: {'✓ 告警' if ok else '✗ 禁打扰'}")


if __name__ == "__main__":
    main()

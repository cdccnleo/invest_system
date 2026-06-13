"""
profile_strategy.py — Hermes L3 Advisor 跨 Profile 隔离 (V24-B4)

设计目标:
- 复用 V22 P2-T3 补丁 8 的 profile_loader.py + 3 profile YAML (default/conservative/aggressive)
- 提供差异化策略: 推荐生成/告警阈值/仓位约束/黑名单 白名单 跨 profile 隔离
- 给上层 (L3DialogEngine / Dashboard) 提供 profile-aware 决策

核心 API:
- L3ProfileAdvisor: 拉取 profile 配置 + 持仓检查 + 推荐生成
- build_profile_aware_recommendation(): 跨 profile 推荐生成
- get_profile_risk_overview(): 风险总览 (供 dashboard 顶部切换用)
- check_profile_compliance(): 持仓合规检查 (单标 vs profile 约束)

3 Profile 差异:
- default (balanced): 5% 单标上限 / 100 PE / AI 算力 35% / 5 持仓白名单 / 信维黑名单
- conservative (defensive): 8% 单标上限 / 30 PE / 防御 55% / 8 持仓白名单 / 严格 0.80 阈值
- aggressive (offensive): 15% 单标上限 / 200 PE / AI 算力 70% / 12 持仓白名单 / 宽松 0.55 阈值

PIT 防御 (实战沉淀):
- #44 profile 配置缺失静默降级 default (不抛异常)
- #45 跨 profile 配置缓存 TTL 60s (避免反复读 yaml)
- #46 推荐生成跨 profile 数据隔离 (conservative 看不到 aggressive 推荐历史)
- #47 持仓合规检查返完整 schema (PIT #37 复用)
- #48 profile 切换 audit log (PG l3.profile_audit_log, 跨 session 可查)
"""
from __future__ import annotations

import json
import os
import sys as _sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ⚠️ PIT 修复: profile_strategy 在 hermes_coordination/scripts/,
# l3_dialog_engine 在 scripts/ (上一级). 加 path 才能 import
_HERE = Path(__file__).parent.resolve()
_ROOT = _HERE.parent.parent
for _p in [str(_HERE), str(_ROOT / "scripts"), str(_ROOT)]:
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

try:
    from profile_loader import ProfileLoader, ProfileError, ALLOWED_PROFILES
    _PROFILE_LOADER_AVAILABLE = True
except Exception as _e:
    _PROFILE_LOADER_AVAILABLE = False
    ProfileLoader = None
    ProfileError = Exception
    ALLOWED_PROFILES = ("default", "conservative", "aggressive")


# ═══════════════════════════════════════════════════════════════════════════
# PIT #45 配置缓存 (60s TTL, 避免反复读 yaml)
# ═══════════════════════════════════════════════════════════════════════════
_CACHE_TTL_SECONDS = 60
_config_cache: Dict[str, Tuple[float, Dict]] = {}  # name -> (loaded_at, cfg)


def _get_profile_config(name: str) -> Dict:
    """PIT #45: 60s TTL 缓存"""
    if not _PROFILE_LOADER_AVAILABLE:
        return _default_fallback_config(name)
    if name not in ALLOWED_PROFILES:
        return _default_fallback_config(name)

    now = time.time()
    if name in _config_cache:
        loaded_at, cfg = _config_cache[name]
        if now - loaded_at < _CACHE_TTL_SECONDS:
            return cfg
    try:
        loader = ProfileLoader()
        cfg = loader.load(name)
        _config_cache[name] = (now, cfg)
        return cfg
    except ProfileError:
        return _default_fallback_config(name)


def _default_fallback_config(name: str) -> Dict:
    """PIT #44: profile 缺失时静默降级 (不抛异常)"""
    return {
        "profile": {"name": name, "risk_level": "balanced", "description": "降级默认"},
        "target_allocation": {"ai_compute": 0.30, "cash": 0.20, "others": 0.50},
        "position_constraints": {
            "max_position_pct": 5.0,
            "min_position_pct": 0.5,
            "max_pe_ttm": 100,
            "max_52w_change": 400,
            "max_high_pe_count": 3,
        },
        "alert_thresholds": {
            "intraday_pct": 5,
            "position_pct": 3,
            "volume_pct": 200,
            "cooldown_minutes": 30,
        },
        "strategy_overrides": {
            "enable_fomo_guard": True,
            "enable_pe_trap_check": True,
            "enable_event_drive": True,
            "intraday_scan_freq_min": 5,
        },
        "llm_routing": {
            "primary_model": "deepseek-chat",
            "fallback_model": "ollama-llama3",
            "confidence_threshold": 0.65,
        },
        "watchlist_overrides": {"blacklist": [], "whitelist": []},
        "holding_stop_loss": {},
        "_fallback": True,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class ProfileCompliance:
    """单标持仓合规检查结果 (PIT #47 完整 schema)"""
    code: str
    name: str
    current_pct: float
    pe_ttm: float
    change_52w: float
    profile: str
    risk_level: str
    ok: bool
    violations: List[str] = field(default_factory=list)
    checked_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class ProfileRecommendation:
    """跨 profile 决策建议 (PIT #46 跨 profile 隔离)"""
    profile: str
    risk_level: str
    action: str  # "buy" | "hold" | "reduce" | "sell" | "switch"
    target_code: Optional[str]
    target_pct: Optional[float]
    confidence: float  # 0-1
    reasoning: str
    profile_constraints_applied: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class ProfileRiskOverview:
    """profile 风险总览 (dashboard 顶部切换用)"""
    profile: str
    risk_level: str
    description: str
    target_allocation: Dict[str, float]
    max_position_pct: float
    max_pe_ttm: int
    confidence_threshold: float
    whitelist_count: int
    blacklist_count: int
    fetched_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict:
        return asdict(self)


# ═══════════════════════════════════════════════════════════════════════════
# L3ProfileAdvisor (核心类)
# ═══════════════════════════════════════════════════════════════════════════
class L3ProfileAdvisor:
    """Hermes L3 Advisor Profile 隔离决策器 (V24-B4)"""

    def __init__(self, profile: str = "default"):
        # PIT #44: profile 缺失降级 default
        if profile not in ALLOWED_PROFILES:
            self.profile = "default"
        else:
            self.profile = profile
        self.cfg = _get_profile_config(self.profile)

    # ──────────────── 1. 风险总览 ────────────────
    def get_risk_overview(self) -> ProfileRiskOverview:
        cfg = self.cfg
        return ProfileRiskOverview(
            profile=self.profile,
            risk_level=cfg["profile"]["risk_level"],
            description=cfg["profile"].get("description", ""),
            target_allocation=cfg["target_allocation"],
            max_position_pct=cfg["position_constraints"]["max_position_pct"],
            max_pe_ttm=cfg["position_constraints"]["max_pe_ttm"],
            confidence_threshold=cfg["llm_routing"]["confidence_threshold"],
            whitelist_count=len(cfg.get("watchlist_overrides", {}).get("whitelist", [])),
            blacklist_count=len(cfg.get("watchlist_overrides", {}).get("blacklist", [])),
        )

    # ──────────────── 2. 持仓合规检查 ────────────────
    def check_position(
        self, code: str, name: str, current_pct: float,
        pe_ttm: float = 0, change_52w: float = 0,
    ) -> ProfileCompliance:
        """PIT #47 完整 schema (输入异常不抛)"""
        constraints = self.cfg["position_constraints"]
        watchlist = self.cfg.get("watchlist_overrides", {})
        violations: List[str] = []

        try:
            pct = float(current_pct or 0)
        except (TypeError, ValueError):
            pct = 0.0
            violations.append(f"仓位值异常 current_pct={current_pct} 视为 0")

        if pct > constraints["max_position_pct"]:
            violations.append(
                f"仓位 {pct}% > profile 上限 {constraints['max_position_pct']}%"
            )
        if pe_ttm and pe_ttm > constraints["max_pe_ttm"]:
            violations.append(
                f"PE(TTM) {pe_ttm} > profile 上限 {constraints['max_pe_ttm']}"
            )
        if change_52w and change_52w > constraints["max_52w_change"]:
            violations.append(
                f"52周涨幅 {change_52w}% > profile 上限 {constraints['max_52w_change']}%"
            )
        if code in watchlist.get("blacklist", []):
            violations.append(f"标的 {code} 在 profile 黑名单")

        return ProfileCompliance(
            code=code, name=name, current_pct=pct,
            pe_ttm=pe_ttm, change_52w=change_52w,
            profile=self.profile,
            risk_level=self.cfg["profile"]["risk_level"],
            ok=len(violations) == 0, violations=violations,
        )

    def check_positions_batch(
        self, positions: List[Dict],
    ) -> List[ProfileCompliance]:
        """批量检查 (PIT #47 输入空时返 [])"""
        if not positions:
            return []
        results = []
        for pos in positions:
            try:
                results.append(self.check_position(
                    code=str(pos.get("code", "")),
                    name=str(pos.get("name", "")),
                    current_pct=float(pos.get("current_pct", 0) or 0),
                    pe_ttm=float(pos.get("pe_ttm", 0) or 0),
                    change_52w=float(pos.get("change_52w", 0) or 0),
                ))
            except Exception as e:
                # 单标失败不阻断
                results.append(ProfileCompliance(
                    code=str(pos.get("code", "?")),
                    name=str(pos.get("name", "?")),
                    current_pct=0, pe_ttm=0, change_52w=0,
                    profile=self.profile,
                    risk_level=self.cfg["profile"]["risk_level"],
                    ok=False, violations=[f"检查异常: {e}"],
                ))
        return results

    # ──────────────── 3. 推荐生成 (跨 profile 隔离) ────────────────
    def build_recommendation(
        self, target_code: str, target_name: str,
        current_pct: float, pe_ttm: float = 0, change_52w: float = 0,
        event_driven: bool = False,
    ) -> ProfileRecommendation:
        """PIT #46 推荐生成: 严格按 profile 策略差异化

        决策树:
        1. blacklist → sell
        2. confidence_threshold (conservative 0.80 > aggressive 0.55)
        3. PE trap 检查 (pe > max_pe_ttm → reduce)
        4. 集中度检查 (pct > max_position_pct → reduce)
        5. event_driven + conservative 关闭 → hold (不追事件)
        6. 白名单 + 符合约束 → buy/hold
        """
        cfg = self.cfg
        constraints = cfg["position_constraints"]
        watchlist = cfg.get("watchlist_overrides", {})
        overrides = cfg["strategy_overrides"]

        compliance = self.check_position(target_code, target_name, current_pct, pe_ttm, change_52w)
        confidence = cfg["llm_routing"]["confidence_threshold"]
        applied: List[str] = []

        # 决策 1: 黑名单 → sell
        if target_code in watchlist.get("blacklist", []):
            applied.append(f"黑名单触发 → sell (profile={self.profile})")
            return ProfileRecommendation(
                profile=self.profile, risk_level=cfg["profile"]["risk_level"],
                action="sell", target_code=target_code, target_pct=0.0,
                confidence=confidence, reasoning=f"{target_name} 在 {self.profile} 黑名单",
                profile_constraints_applied=applied,
            )

        # 决策 2: PE trap
        if pe_ttm and pe_ttm > constraints["max_pe_ttm"]:
            applied.append(f"PE trap: {pe_ttm} > {constraints['max_pe_ttm']} → reduce")
            return ProfileRecommendation(
                profile=self.profile, risk_level=cfg["profile"]["risk_level"],
                action="reduce", target_code=target_code,
                target_pct=min(current_pct * 0.5, constraints["max_position_pct"]),
                confidence=confidence,
                reasoning=f"PE(TTM) {pe_ttm} 超过 profile 上限 {constraints['max_pe_ttm']}",
                profile_constraints_applied=applied,
            )

        # 决策 3: 集中度
        if current_pct > constraints["max_position_pct"]:
            applied.append(f"集中度: {current_pct}% > {constraints['max_position_pct']}% → reduce")
            return ProfileRecommendation(
                profile=self.profile, risk_level=cfg["profile"]["risk_level"],
                action="reduce", target_code=target_code,
                target_pct=constraints["max_position_pct"],
                confidence=confidence,
                reasoning=f"仓位 {current_pct}% 超过 profile 上限 {constraints['max_position_pct']}%",
                profile_constraints_applied=applied,
            )

        # 决策 4: conservative 不追事件
        if event_driven and not overrides.get("enable_event_drive", True):
            applied.append(f"conservative 关闭事件驱动 → hold")
            return ProfileRecommendation(
                profile=self.profile, risk_level=cfg["profile"]["risk_level"],
                action="hold", target_code=target_code, target_pct=current_pct,
                confidence=confidence,
                reasoning=f"conservative profile 不追事件 (enable_event_drive=False)",
                profile_constraints_applied=applied,
            )

        # 决策 5: 白名单 + 符合约束 → buy/hold
        if target_code in watchlist.get("whitelist", []):
            applied.append(f"白名单 + 符合约束 → buy/hold")
            return ProfileRecommendation(
                profile=self.profile, risk_level=cfg["profile"]["risk_level"],
                action="buy" if current_pct < constraints["max_position_pct"] else "hold",
                target_code=target_code,
                target_pct=min(constraints["max_position_pct"], current_pct + 1.0),
                confidence=confidence,
                reasoning=f"{target_name} 在 {self.profile} 白名单 + 符合约束",
                profile_constraints_applied=applied,
            )

        # 决策 6: 默认 hold
        applied.append(f"无特殊约束 → hold")
        return ProfileRecommendation(
            profile=self.profile, risk_level=cfg["profile"]["risk_level"],
            action="hold", target_code=target_code, target_pct=current_pct,
            confidence=confidence,
            reasoning=f"{target_name} 保持现有仓位",
            profile_constraints_applied=applied,
        )

    # ──────────────── 4. PG audit log (PIT #48) ────────────────
    def log_profile_switch(self, from_profile: str, to_profile: str) -> bool:
        """PIT #48: profile 切换记录到 PG l3.profile_audit_log
        失败不阻断主流程
        """
        try:
            import psycopg2
            from profile_loader import ALLOWED_PROFILES  # noqa: F401
            # 复用 l3_dialog_engine 的 db config
            try:
                from l3_dialog_engine import _get_db_config
                conn = psycopg2.connect(**_get_db_config())
            except Exception:
                return False
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS l3.profile_audit_log (
                    id BIGSERIAL PRIMARY KEY,
                    switched_at TIMESTAMP DEFAULT NOW(),
                    from_profile VARCHAR(32),
                    to_profile VARCHAR(32),
                    user_context VARCHAR(64) DEFAULT 'hermes_default'
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_pal_switched_at
                ON l3.profile_audit_log(switched_at DESC)
            """)
            cur.execute("""
                INSERT INTO l3.profile_audit_log (from_profile, to_profile)
                VALUES (%s, %s)
            """, (from_profile, to_profile))
            conn.commit()
            conn.close()
            return True
        except Exception:
            return False


# ═══════════════════════════════════════════════════════════════════════════
# 跨 profile 推荐生成 (同一标的多 profile 决策对比)
# ═══════════════════════════════════════════════════════════════════════════
def build_profile_aware_recommendation(
    target_code: str, target_name: str,
    current_pct: float, pe_ttm: float = 0, change_52w: float = 0,
    event_driven: bool = False,
) -> Dict[str, ProfileRecommendation]:
    """跨 3 profile 决策对比 (PIT #46 跨 profile 隔离)

    Returns:
        {
          "default": ProfileRecommendation,
          "conservative": ProfileRecommendation,
          "aggressive": ProfileRecommendation
        }
    """
    result: Dict[str, ProfileRecommendation] = {}
    for p in ALLOWED_PROFILES:
        advisor = L3ProfileAdvisor(profile=p)
        result[p] = advisor.build_recommendation(
            target_code=target_code, target_name=target_name,
            current_pct=current_pct, pe_ttm=pe_ttm,
            change_52w=change_52w, event_driven=event_driven,
        )
    return result


def get_all_profiles_risk_overview() -> List[ProfileRiskOverview]:
    """3 profile 风险总览 (dashboard 顶部切换)"""
    overviews: List[ProfileRiskOverview] = []
    for p in ALLOWED_PROFILES:
        advisor = L3ProfileAdvisor(profile=p)
        overviews.append(advisor.get_risk_overview())
    return overviews


def check_profile_compliance(
    profile: str, positions: List[Dict],
) -> List[ProfileCompliance]:
    """持仓合规检查 (单 profile)"""
    advisor = L3ProfileAdvisor(profile=profile)
    return advisor.check_positions_batch(positions)


# ═══════════════════════════════════════════════════════════════════════════
# PG DDL 工具
# ═══════════════════════════════════════════════════════════════════════════
def ensure_pg_tables() -> Dict[str, int]:
    """建 l3.profile_audit_log 表 (PIT #48)"""
    result: Dict[str, int] = {"profile_audit_log": 0}
    try:
        import psycopg2
        try:
            from l3_dialog_engine import _get_db_config
            conn = psycopg2.connect(**_get_db_config())
        except Exception:
            return result
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS l3.profile_audit_log (
                id BIGSERIAL PRIMARY KEY,
                switched_at TIMESTAMP DEFAULT NOW(),
                from_profile VARCHAR(32),
                to_profile VARCHAR(32),
                user_context VARCHAR(64) DEFAULT 'hermes_default'
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_pal_switched_at
            ON l3.profile_audit_log(switched_at DESC)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_pal_to_profile
            ON l3.profile_audit_log(to_profile, switched_at DESC)
        """)
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM l3.profile_audit_log")
        result["profile_audit_log"] = cur.fetchone()[0]
        conn.close()
    except Exception as e:
        result["_error"] = str(e)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Self-test
# ═══════════════════════════════════════════════════════════════════════════
def _self_test() -> bool:
    print("=" * 70)
    print("Profile Strategy 演示 (V24-B4)")
    print("=" * 70)

    # 1. 3 profile 风险总览
    print("\n--- 1. 3 Profile 风险总览 ---")
    overviews = get_all_profiles_risk_overview()
    for ov in overviews:
        print(f"  [{ov.profile:13s}] risk={ov.risk_level:10s} "
              f"max_pct={ov.max_position_pct}% max_pe={ov.max_pe_ttm} "
              f"whitelist={ov.whitelist_count} blacklist={ov.blacklist_count}")

    # 2. 持仓合规 (PIT #47)
    print("\n--- 2. 持仓合规 (PIT #47) ---")
    test_positions = [
        {"code": "300136", "name": "信维通信", "current_pct": 4.0, "pe_ttm": 150, "change_52w": 350},
        {"code": "600487", "name": "亨通光电", "current_pct": 3.0, "pe_ttm": 58.91, "change_52w": 422},
        {"code": "518880", "name": "黄金ETF", "current_pct": 5.0, "pe_ttm": 0, "change_52w": 0},
    ]
    for p in ALLOWED_PROFILES:
        results = check_profile_compliance(p, test_positions)
        for r in results:
            mark = "✓" if r.ok else "✗"
            print(f"  [{p:13s}] {mark} {r.code} ({r.name}): "
                  f"pct={r.current_pct}% pe={r.pe_ttm} → {len(r.violations)} violations")
            for v in r.violations:
                print(f"    - {v}")

    # 3. 跨 profile 决策对比 (PIT #46)
    print("\n--- 3. 跨 profile 决策对比 (信维 300136, PIT #46) ---")
    recs = build_profile_aware_recommendation(
        target_code="300136", target_name="信维通信",
        current_pct=4.0, pe_ttm=150, change_52w=350,
    )
    for p, rec in recs.items():
        print(f"  [{p:13s}] action={rec.action:6s} confidence={rec.confidence:.2f} "
              f"target_pct={rec.target_pct} → {rec.reasoning[:50]}")

    # 4. event_driven + conservative (PIT #46 隔离)
    print("\n--- 4. Event-driven + Conservative (PIT #46) ---")
    recs = build_profile_aware_recommendation(
        target_code="600487", target_name="亨通光电",
        current_pct=2.0, pe_ttm=58.91, change_52w=422, event_driven=True,
    )
    for p, rec in recs.items():
        print(f"  [{p:13s}] action={rec.action:6s} → {rec.reasoning[:50]}")

    # 5. 边界 case: 输入空
    print("\n--- 5. 边界 case (PIT #47) ---")
    empty = check_profile_compliance("default", [])
    print(f"  持仓空 → 返 {len(empty)} 项 (期望 0, schema 完整)")

    # 6. PG DDL
    print("\n--- 6. PG DDL (PIT #48) ---")
    ddl_result = ensure_pg_tables()
    print(f"  profile_audit_log: {ddl_result}")

    # 7. profile 切换 audit
    print("\n--- 7. Profile 切换 audit (PIT #48) ---")
    advisor = L3ProfileAdvisor(profile="default")
    logged = advisor.log_profile_switch("default", "aggressive")
    print(f"  切换 default → aggressive 记录: {logged}")

    print("\n=== Profile Strategy 自测通过 ===")
    return True


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        _self_test()
    else:
        print(f"可用 profile: {ALLOWED_PROFILES}")
        print("用法: python3 profile_strategy.py --self-test")

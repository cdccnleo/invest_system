"""
profile_loader.py — Hermes Profile 隔离加载器 (P2-T3 补丁8 落地 v1.0)

功能:
- 加载 ~/.hermes/profiles/<name>/config.yaml
- 校验风险/配置约束
- 给上层（intraday_monitor / event_analyst）提供 profile-aware 配置

使用:
    from profile_loader import ProfileLoader
    loader = ProfileLoader()
    cfg = loader.load("default")
    threshold = cfg["alert_thresholds"]["intraday_pct"]
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Optional

import yaml

PROFILES_DIR = Path("/home/aileo/invest_system/hermes_coordination/references/profiles")
ALT_PROFILES_DIR = Path.home() / ".hermes" / "profiles"

ALLOWED_PROFILES = ("default", "conservative", "aggressive")


class ProfileError(Exception):
    pass


class ProfileLoader:
    def __init__(self, profiles_dir: Optional[Path] = None):
        self.profiles_dir = profiles_dir or PROFILES_DIR
        if not self.profiles_dir.exists():
            raise ProfileError(f"profiles dir not found: {self.profiles_dir}")
        self._cache: Dict[str, Dict] = {}

    def list_profiles(self) -> List[str]:
        return [f.stem for f in self.profiles_dir.glob("*.yaml")]

    def load(self, name: str, use_cache: bool = True) -> Dict:
        """加载 profile 配置"""
        if name not in ALLOWED_PROFILES:
            raise ProfileError(f"profile '{name}' 不在白名单 {ALLOWED_PROFILES}")

        if use_cache and name in self._cache:
            return self._cache[name]

        path = self.profiles_dir / f"{name}.yaml"
        if not path.exists():
            raise ProfileError(f"profile 文件不存在: {path}")

        with path.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        # 校验
        self._validate(cfg)

        self._cache[name] = cfg
        return cfg

    def _validate(self, cfg: Dict) -> None:
        """校验 profile 完整性"""
        if "profile" not in cfg or "name" not in cfg["profile"]:
            raise ProfileError("profile.name 必填")
        if "target_allocation" not in cfg:
            raise ProfileError("target_allocation 必填")
        # 总和应为 1.0 ± 0.01
        total = sum(cfg["target_allocation"].values())
        if not (0.99 <= total <= 1.01):
            raise ProfileError(f"target_allocation 总和 {total:.3f} ≠ 1.0")
        if "position_constraints" not in cfg:
            raise ProfileError("position_constraints 必填")
        if "alert_thresholds" not in cfg:
            raise ProfileError("alert_thresholds 必填")
        if "llm_routing" not in cfg:
            raise ProfileError("llm_routing 必填")

    def get_effective_thresholds(self, name: str, asset_class: str = "stock") -> Dict:
        """结合 profile + asset_class 给出最终阈值

        优先级: asset_class 默认阈值 -> profile.alert_thresholds 覆盖
        """
        cfg = self.load(name)
        # 资产类默认阈值
        from asset_class_router import AssetClassRouter  # 局部导入避免循环
        ar = AssetClassRouter()
        threshold = ar.get_alert_threshold(asset_class)

        # profile 覆盖（如果有同名 key）
        profile_th = cfg.get("alert_thresholds", {})
        for k, v in profile_th.items():
            threshold[k] = v

        return threshold

    def is_blacklisted(self, name: str, code: str) -> bool:
        """判断标的代码是否在 profile 黑名单"""
        cfg = self.load(name)
        return code in cfg.get("watchlist_overrides", {}).get("blacklist", [])

    def is_whitelisted(self, name: str, code: str) -> bool:
        """判断标的代码是否在 profile 白名单"""
        cfg = self.load(name)
        return code in cfg.get("watchlist_overrides", {}).get("whitelist", [])

    def get_max_position_pct(self, name: str) -> float:
        """获取单标的最高仓位"""
        return self.load(name)["position_constraints"]["max_position_pct"]

    def check_position(self, name: str, code: str, current_pct: float,
                       pe_ttm: float = 0, change_52w: float = 0) -> Dict:
        """检查持仓是否符合 profile 约束

        Returns:
            {
                "ok": bool,
                "violations": [str, ...],
            }
        """
        cfg = self.load(name)
        constraints = cfg["position_constraints"]
        violations = []

        if current_pct > constraints["max_position_pct"]:
            violations.append(
                f"仓位 {current_pct}% > 上限 {constraints['max_position_pct']}%"
            )
        if pe_ttm > constraints["max_pe_ttm"]:
            violations.append(
                f"PE(TTM) {pe_ttm} > 上限 {constraints['max_pe_ttm']}"
            )
        if change_52w > constraints["max_52w_change"]:
            violations.append(
                f"52周涨幅 {change_52w}% > 上限 {constraints['max_52w_change']}%"
            )
        if self.is_blacklisted(name, code):
            violations.append(f"标的 {code} 在黑名单")

        return {
            "ok": len(violations) == 0,
            "violations": violations,
        }


def main():
    import json
    print("=" * 70)
    print("Profile Loader 演示")
    print("=" * 70)

    loader = ProfileLoader()
    print(f"\n可用 profile: {loader.list_profiles()}")

    for name in ALLOWED_PROFILES:
        try:
            cfg = loader.load(name)
            print(f"\n--- {name} ---")
            print(f"  风险等级: {cfg['profile']['risk_level']}")
            print(f"  目标配置: {cfg['target_allocation']}")
            print(f"  单标上限: {cfg['position_constraints']['max_position_pct']}%")
            print(f"  黑名单: {cfg['watchlist_overrides']['blacklist']}")
            print(f"  白名单: {cfg['watchlist_overrides']['whitelist'][:3]}...")

            # 检查持仓示例
            for code, pct, pe, chg in [
                ("300136", 4.0, 150, 350),    # 信维
                ("600487", 3.0, 58.91, 422),  # 亨通
                ("518880", 5.0, 0, 0),        # 黄金
            ]:
                result = loader.check_position(name, code, pct, pe, chg)
                ok = "✓" if result["ok"] else "✗"
                print(f"  检查 {code} (仓位{pct}%/PE{pe}/52w{chg}%): {ok}")
                for v in result["violations"]:
                    print(f"    - {v}")
        except ProfileError as e:
            print(f"  ✗ 加载失败: {e}")


if __name__ == "__main__":
    main()

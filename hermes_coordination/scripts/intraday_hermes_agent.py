"""
intraday_hermes_agent.py
v2.2 方案3: 盘中异动 + Hermes 实时解读

盘中异动 (intraday_monitor 检测) → 加载对应 stock-<code> skill
→ LLM 4级降级链 解读 → 推送带 💡 Hermes 解读 的告警

v2.2 关键设计:
1. 每日限额 20 次 (避免 LLM 成本失控)
2. 异步 (threading.Thread) 不阻塞 intraday_monitor 主扫描
3. LLMFallbackChain 4级降级 (复用 v2.1 补丁7)
4. 失败静默 (LLM 不可用也不影响原异动告警)
5. skill 自动匹配 (stock-<code> 多种命名后缀)

创建: 2026-06-12
对应: hermes-investpilot-coordination-v2 v2.2 方案3
"""
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional

# ============================================================
# 路径配置
# ============================================================
HERMES_SKILLS_DIR = Path("/home/aileo/.hermes/skills/investing")
HERMES_SCRIPTS_DIR = Path("/home/aileo/invest_system/hermes_coordination/scripts")

# 限额: 每日 20 次
DAILY_QUOTA = 20
QUOTA_FILE = Path("/tmp/intraday_hermes_quota.json")

# ============================================================
# 每日限额管理
# ============================================================
class DailyQuota:
    """每日 LLM 解读限额管理"""

    def __init__(self, daily_limit: int = DAILY_QUOTA, quota_file: Path = QUOTA_FILE):
        self.daily_limit = daily_limit
        self.quota_file = quota_file
        self._lock = threading.Lock()
        # PIT #21 修复 (V24-B1): 主动 touch() 文件, 避免监控指标漏算
        # 旧逻辑: 文件不存在 → 等 try_acquire() 才创建, 中间窗口监控/集成验证 fail
        # 新逻辑: __init__ 时主动确保文件存在, state = default
        self._ensure_file_exists()

    def _ensure_file_exists(self) -> None:
        """PIT #21: 确保 quota 文件存在, 不存在则创建 default state"""
        if not self.quota_file.exists():
            try:
                default_state = {"date": str(date.today()), "used": 0, "history": []}
                self.quota_file.write_text(json.dumps(default_state, ensure_ascii=False))
            except Exception as e:
                logging.warning(f"quota 文件创建失败 (PIT #21): {e}")

    def _load_state(self) -> Dict:
        """加载限额状态"""
        if not self.quota_file.exists():
            return {"date": str(date.today()), "used": 0, "history": []}
        try:
            state = json.loads(self.quota_file.read_text())
            # 日期滚动
            if state.get("date") != str(date.today()):
                # 归档昨日
                yesterday_used = state.get("used", 0)
                state["history"].append({
                    "date": state.get("date"),
                    "used": yesterday_used,
                })
                # 只保留 30 天
                state["history"] = state["history"][-30:]
                state = {"date": str(date.today()), "used": 0, "history": state["history"]}
            return state
        except Exception as e:
            logging.warning(f"限额状态加载失败, 重置: {e}")
            return {"date": str(date.today()), "used": 0, "history": []}

    def _save_state(self, state: Dict) -> None:
        """保存限额状态"""
        try:
            self.quota_file.write_text(json.dumps(state, ensure_ascii=False))
        except Exception as e:
            logging.warning(f"限额状态保存失败: {e}")

    def try_acquire(self) -> bool:
        """
        尝试获取一次配额
        Returns: True=有配额, False=已用完
        """
        with self._lock:
            state = self._load_state()
            if state["used"] >= self.daily_limit:
                return False
            state["used"] += 1
            self._save_state(state)
            return True

    def get_remaining(self) -> int:
        """剩余配额"""
        state = self._load_state()
        return max(0, self.daily_limit - state["used"])

    def get_status(self) -> Dict:
        """当前状态 (用于调试)"""
        state = self._load_state()
        return {
            "date": state["date"],
            "used": state["used"],
            "limit": self.daily_limit,
            "remaining": self.daily_limit - state["used"],
            "history": state["history"][-7:],  # 最近 7 天
        }


# ============================================================
# Skill 自动加载
# ============================================================
def find_skill_for_code(code: str) -> Optional[Path]:
    """
    根据股票代码找对应 Hermes skill
    支持 3 种命名:
      1. stock-<code>-<name>     (人写)
      2. stock-<code>-auto        (i2h 同步生成)
      3. stock-<code>-sync        (h2i 同步生成)
    """
    if not HERMES_SKILLS_DIR.exists():
        return None
    # 优先匹配最具体 (带 name) → -auto → -sync
    for pattern in [f"stock-{code}-*", f"stock-{code}-auto", f"stock-{code}-sync"]:
        matches = list(HERMES_SKILLS_DIR.glob(pattern))
        if matches:
            # 优先带 SKILL.md 的
            for m in matches:
                if (m / "SKILL.md").exists():
                    return m
            return matches[0]
    return None


def load_skill_excerpt(code: str, max_chars: int = 2000) -> Optional[str]:
    """
    加载 skill 关键摘要 (避免 LLM prompt 膨胀)
    提取: frontmatter description + 前 N 段核心内容
    """
    skill_dir = find_skill_for_code(code)
    if not skill_dir:
        return None
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.exists():
        return None
    try:
        content = skill_file.read_text(encoding="utf-8")
        # 提取 frontmatter description
        desc_match = re.search(r"^description:\s*(.+?)$", content, re.MULTILINE)
        description = desc_match.group(1).strip() if desc_match else ""
        # 提取前 max_chars (去除 frontmatter)
        body = re.sub(r"^---\n.*?\n---\n", "", content, count=1, flags=re.DOTALL)
        excerpt = body[:max_chars].strip()
        return f"[SKILL: {skill_dir.name}]\n{description}\n\n{excerpt}"
    except Exception as e:
        logging.warning(f"加载 skill {skill_dir.name} 失败: {e}")
        return None


# ============================================================
# LLM 4级降级链
# ============================================================
# 复用 v2.1 补丁7 的 LLMFallbackChain
sys.path.insert(0, str(HERMES_SCRIPTS_DIR))
try:
    from llm_fallback_chain import LLMFallbackChain  # noqa: E402
    _FALLBACK_CHAIN = LLMFallbackChain(
        hermes_router=None,  # 走 L3 规则引擎路径 (mock 模式)
    )
    _FALLBACK_AVAILABLE = True
except Exception as e:
    _FALLBACK_CHAIN = None
    _FALLBACK_AVAILABLE = False
    logging.warning(f"LLMFallbackChain 加载失败: {e}")


def call_llm_with_fallback(system: str, prompt: str, max_retries: int = 1) -> Dict:
    """
    LLM 4级降级链调用
    Returns: {"content": str, "level": str, "error": str|None}
    """
    if not _FALLBACK_AVAILABLE or _FALLBACK_CHAIN is None:
        return {
            "content": "",
            "level": "L0_UNAVAILABLE",
            "error": "LLMFallbackChain 不可用",
        }
    try:
        # 启用 mock 模式避免 L1 实际调用
        os.environ.setdefault("HERMES_FALLBACK_MOCK", "1")
        result = _FALLBACK_CHAIN.call(prompt, system=system, max_retries=max_retries)
        return {
            "content": result.get("content", ""),
            "level": result.get("level", "unknown"),
            "error": result.get("error"),
        }
    except Exception as e:
        return {
            "content": "",
            "level": "L0_EXCEPTION",
            "error": str(e),
        }


# ============================================================
# 核心: 异动解读
# ============================================================
def explain_anomaly(anomaly: Dict) -> Dict:
    """
    给单条异动生成 Hermes 解读

    Args:
        anomaly: {
            "ts_code": "600487.SH",
            "name": "亨通光电",
            "change_pct": 5.2,
            "close": 18.5,
            "alert_type": "PRICE_ALERT",
            "reason": "上涨5.2% (阈值5%/stock)",
            "asset_class": "stock",
            ...
        }

    Returns:
        {
            "interpretation": "30字内解读",
            "refs": ["skill_name"],
            "fallback_level": "L1|L2|L3|L4",
            "quota_remaining": 19,
        }
    """
    quota = DailyQuota()
    code = anomaly.get("ts_code", "").split(".")[0]
    name = anomaly.get("name", code)
    pct = anomaly.get("change_pct", 0)
    asset_class = anomaly.get("asset_class", "stock")
    direction = "上涨" if pct > 0 else "下跌"
    abs_pct = abs(pct)

    # 1. 加载 skill
    skill_excerpt = load_skill_excerpt(code, max_chars=2000)
    skill_ref = None
    if skill_excerpt:
        # 提取 skill 名 (从 "[SKILL: ...]" 行)
        m = re.match(r"\[SKILL: ([\w-]+)\]", skill_excerpt)
        skill_ref = m.group(1) if m else None

    # 2. 构造 prompt
    system = "你是 Hermes 投资分析助手, 用30字内简明扼要解释盘中异动。"
    prompt_parts = [
        f"【盘中异动】{name}({code}) {direction}{abs_pct:.1f}%",
        f"资产类型: {asset_class}",
        f"触发类型: {anomaly.get('alert_type', 'N/A')}",
        f"原因: {anomaly.get('reason', 'N/A')}",
    ]
    if skill_excerpt:
        prompt_parts.append(f"\n参考持仓知识库:\n{skill_excerpt}")
    prompt_parts.append("\n请基于以上信息, 用 30 字内说明: 这只标的为什么异动 + 可能的影响。")
    prompt = "\n".join(prompt_parts)

    # 3. 调用 LLM
    result = call_llm_with_fallback(system, prompt)

    # 4. 处理结果
    interpretation = result.get("content", "").strip()
    if not interpretation:
        # 降级兜底: 用规则生成基础解读
        interpretation = _rule_based_explain(anomaly, skill_excerpt)
        fallback_level = "L3_RULE"
    else:
        fallback_level = result.get("level", "unknown")

    return {
        "interpretation": interpretation[:100],  # 限制 100 字
        "refs": [skill_ref] if skill_ref else [],
        "fallback_level": fallback_level,
        "quota_remaining": quota.get_remaining(),
    }


def _rule_based_explain(anomaly: Dict, skill_excerpt: Optional[str]) -> str:
    """
    降级兜底: 不调 LLM, 用规则生成基础解读
    """
    code = anomaly.get("ts_code", "").split(".")[0]
    pct = abs(anomaly.get("change_pct", 0))
    direction = "上涨" if anomaly.get("change_pct", 0) > 0 else "下跌"

    if skill_excerpt:
        return f"{direction}{pct:.1f}%, 已在持仓知识库 (skill存在), 需人工分析"
    else:
        return f"{direction}{pct:.1f}%, 暂无持仓知识库, 仅作数据告警"


# ============================================================
# 异步推送
# ============================================================
def _send_enhanced_notification(anomaly: Dict, explanation: Dict) -> None:
    """
    发送带 Hermes 解读的增强推送
    格式:
      ⚠️ {name}({code}) 涨跌幅 {pct}%
      📊 触发: {alert_type}
      💡 Hermes 解读: {interpretation}
      📚 参考: {refs}
    """
    try:
        # 复用 intraday_monitor 的推送通道
        sys.path.insert(0, "/home/aileo/invest_system/scripts")
        from notification import send_notification
    except Exception as e:
        logging.error(f"notification 模块加载失败: {e}")
        return

    name = anomaly.get("name", anomaly.get("ts_code", ""))
    code = anomaly.get("ts_code", "")
    pct = anomaly.get("change_pct", 0)
    alert_type = anomaly.get("alert_type", "异动")
    interpretation = explanation.get("interpretation", "")
    refs = explanation.get("refs", [])

    title = f"⚠️ 盘中异动 + Hermes 解读: {name}"
    content_lines = [
        f"{name}({code}) | {pct:+.1f}%",
        f"📊 触发: {alert_type}",
        f"💡 Hermes 解读: {interpretation}",
    ]
    if refs:
        content_lines.append(f"📚 参考: {', '.join(refs)}")
    content = "\n".join(content_lines)

    try:
        send_notification(title, content, level="WARNING")
    except Exception as e:
        logging.error(f"推送失败: {e}")


def explain_and_notify_async(anomaly: Dict) -> None:
    """
    异步入口: 给 intraday_monitor 调用
    检查配额 → 调 LLM → 推送
    """
    quota = DailyQuota()

    # 限额检查
    if not quota.try_acquire():
        # 配额用完: 静默, 不影响原异动告警
        return

    # 异步调 LLM
    def _worker():
        try:
            explanation = explain_anomaly(anomaly)
            _send_enhanced_notification(anomaly, explanation)
        except Exception as e:
            logging.error(f"async explain_and_notify 异常: {e}")

    t = threading.Thread(target=_worker, daemon=True, name="hermes-explain")
    t.start()


# ============================================================
# CLI 测试
# ============================================================
def _cli_test():
    """命令行测试入口"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logger = logging.getLogger("intraday_hermes")

    # 5 个 mock 异动 (覆盖不同资产类)
    test_anomalies = [
        {
            "ts_code": "600487.SH",
            "name": "亨通光电",
            "change_pct": 5.2,
            "close": 18.5,
            "alert_type": "PRICE_ALERT",
            "reason": "上涨5.2% (阈值5%/stock)",
            "asset_class": "stock",
        },
        {
            "ts_code": "159819.SH",
            "name": "人工智能ETF",
            "change_pct": 3.5,
            "close": 1.234,
            "alert_type": "PRICE_ALERT",
            "reason": "上涨3.5% (阈值3%/etf)",
            "asset_class": "etf",
        },
        {
            "ts_code": "00700.HK",
            "name": "腾讯控股",
            "change_pct": -8.2,
            "close": 380.0,
            "alert_type": "PRICE_ALERT",
            "reason": "下跌8.2% (阈值8%/hk_stock)",
            "asset_class": "hk_stock",
        },
        {
            "ts_code": "TSLA",
            "name": "特斯拉",
            "change_pct": 6.0,
            "close": 250.0,
            "alert_type": "PRICE_ALERT",
            "reason": "上涨6.0% (阈值5%/us_stock)",
            "asset_class": "us_stock",
        },
        {
            "ts_code": "002050.OF",
            "name": "三花智控",
            "change_pct": 2.0,
            "close": 0,
            "alert_type": "PRICE_ALERT",
            "reason": "上涨2.0% (阈值3%/fund)",
            "asset_class": "fund",
        },
    ]

    logger.info("=" * 50)
    logger.info("V22-T3-A 测试: 5 个 mock 异动 Hermes 解读")
    logger.info("=" * 50)

    # 重置配额
    if QUOTA_FILE.exists():
        QUOTA_FILE.unlink()
    quota = DailyQuota()
    logger.info(f"配额: {quota.get_status()}")

    for i, anomaly in enumerate(test_anomalies, 1):
        logger.info(f"\n[{i}/5] 处理: {anomaly['name']}({anomaly['ts_code']})")
        if not quota.try_acquire():
            logger.warning("  ✗ 配额用完, 跳过")
            continue
        result = explain_anomaly(anomaly)
        logger.info(f"  💡 解读: {result['interpretation']}")
        logger.info(f"  📚 参考: {result['refs']}")
        logger.info(f"  🔄 降级: {result['fallback_level']}")
        logger.info(f"  📊 剩余配额: {result['quota_remaining']}")

    logger.info("\n" + "=" * 50)
    logger.info(f"最终配额: {quota.get_status()}")
    logger.info("=" * 50)


if __name__ == "__main__":
    _cli_test()

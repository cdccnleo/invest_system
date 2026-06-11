"""
hermes_event_analyst.py
方案2: Hermes Agent作为"事件首席分析师"

每晚22:00自动扫描AInvest/reports/events/目录，识别关键事件，
生成操作建议，推送到钉钉+Telegram+企微。

对应v2.0补丁：
- 补丁1: 接口契约 (hermes_investpilot_contract_v1.yaml)
- 补丁2: 可观测性 (hermes_monitoring_v1.yaml)
- 补丁3: 成本控制 (cost_estimation_v1.yaml)
- 补丁4: 时间窗口 (key_window_strategy_switch_v1.yaml)
- 补丁5: 数据脱敏 (data_privacy_v1.md)

创建时间: 2026-06-11
版本: v0.1 (骨架版·待dry-run验证)
"""
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import yaml

# ============================================================
# 配置加载
# ============================================================
SKILL_DIR = Path("/home/aileo/.hermes/skills/investing/hermes-investpilot-coordination-v2")
CONTRACTS_DIR = SKILL_DIR / "references" / "contracts"

EVENTS_DIR = Path("/mnt/c/PythonProject/AInvest/reports/events")
TARGET_MEMORIES_DIR = Path("/mnt/c/PythonProject/invest_system/data/target_memories")
LOG_DIR = Path("/mnt/c/PythonProject/invest_system/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# 日志（结构化JSON，遵循补丁2）
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / "hermes_agent.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("hermes_event_analyst")


def log_event(level: str, component: str, action: str, **kwargs):
    """结构化日志"""
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "level": level,
        "component": component,
        "action": action,
        **kwargs
    }
    logger.info(json.dumps(log_entry, ensure_ascii=False))


# ============================================================
# 合约加载
# ============================================================
def load_contract(name: str) -> dict:
    """加载YAML合约"""
    path = CONTRACTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Contract not found: {path}")
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


# ============================================================
# 核心类: HermesEventAnalyst
# ============================================================
class HermesEventAnalyst:
    """Hermes事件首席分析师（方案2实现）"""

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.contract = load_contract("01-hermes-investpilot-contract.yaml")
        self.monitoring = load_contract("02-hermes-monitoring.yaml")
        self.cost = load_contract("03-cost-estimation.yaml")
        self.key_window = load_contract("04-key-window-strategy.yaml")

        # 状态
        self.scan_start_time = None
        self.scanned_reports = []
        self.top_holdings = []
        self.actions = []
        self.errors = []

    async def scan(self, target_date: Optional[str] = None,
                   holdings: Optional[List[str]] = None) -> dict:
        """
        主入口：扫描events目录，生成操作建议

        Args:
            target_date: ISO格式日期，默认昨日
            holdings: 持仓代码列表，默认从investpilot加载

        Returns:
            {
              scanned_reports: int,
              scan_duration_seconds: float,
              top_holdings: [...],
              high_priority_events: [...],
              actions: [...]
            }
        """
        self.scan_start_time = time.time()

        if target_date is None:
            target_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        log_event("INFO", "event_analyst", "scan_start",
                  target_date=target_date, dry_run=self.dry_run)

        try:
            # 步骤1: 确定持仓列表
            if holdings is None:
                holdings = await self._load_default_holdings()
            log_event("INFO", "event_analyst", "holdings_loaded",
                      count=len(holdings), holdings=holdings[:5])

            # 步骤2: 扫描events目录
            reports = await self._scan_events_dir(target_date)
            self.scanned_reports = reports
            log_event("INFO", "event_analyst", "events_scanned",
                      count=len(reports))

            # 步骤3: 持仓提及频次统计
            self.top_holdings = await self._extract_top_holdings(reports, holdings)

            # 步骤4: 识别关键事件（基于持仓频次+主题）
            high_priority = await self._identify_high_priority_events(reports, holdings)

            # 步骤5: 生成操作建议（这里mock，真实环境调用LLM）
            self.actions = await self._generate_action_suggestions(
                self.top_holdings, high_priority
            )

            # 步骤6: 数据脱敏（补丁5）
            safe_actions = self._redact_for_output(self.actions, role="team")

            # 步骤7: 推送到钉钉/企微（dry-run下跳过）
            if not self.dry_run:
                await self._push_to_channels(safe_actions)
            else:
                log_event("INFO", "event_analyst", "dry_run_skip_push",
                          actions_count=len(safe_actions))

            duration = time.time() - self.scan_start_time

            result = {
                "scanned_reports": len(reports),
                "scan_duration_seconds": round(duration, 2),
                "top_holdings": self.top_holdings[:10],
                "high_priority_events": high_priority[:5],
                "actions": safe_actions,
                "dry_run": self.dry_run,
                "timestamp": datetime.now().isoformat()
            }

            log_event("INFO", "event_analyst", "scan_complete",
                      duration=duration,
                      reports=len(reports),
                      actions=len(self.actions))

            return result

        except Exception as e:
            log_event("ERROR", "event_analyst", "scan_failed",
                      error_code="E999", error_message=str(e))
            raise

    async def _load_default_holdings(self) -> List[str]:
        """从target_memories目录加载默认持仓代码"""
        holdings = []
        if TARGET_MEMORIES_DIR.exists():
            for f in TARGET_MEMORIES_DIR.glob("*.md"):
                if f.stem.isdigit() or (len(f.stem) == 6 and f.stem[:2].isalpha()):
                    holdings.append(f.stem)
        return sorted(set(holdings))

    async def _scan_events_dir(self, target_date: str) -> List[dict]:
        """扫描events目录中target_date的报告"""
        reports = []

        if not EVENTS_DIR.exists():
            log_event("WARNING", "event_analyst", "events_dir_missing",
                      path=str(EVENTS_DIR))
            return reports

        # 扫描所有md文件，提取日期
        for f in EVENTS_DIR.glob("*.md"):
            try:
                # 从文件内容前100行提取日期或事件名
                content_preview = f.read_text(encoding='utf-8', errors='ignore')[:500]
                reports.append({
                    "file": f.name,
                    "path": str(f),
                    "size_kb": round(f.stat().st_size / 1024, 1),
                    "preview": content_preview[:200]
                })
            except Exception as e:
                self.errors.append({"file": f.name, "error": str(e)})

        return reports

    async def _extract_top_holdings(self, reports: List[dict],
                                    holdings: List[str]) -> List[dict]:
        """提取持仓提及频次TOP榜"""
        # 简单实现：扫描每个报告内容，统计持仓代码出现次数
        from collections import Counter

        mention_counter = Counter()

        for r in reports[:50]:  # 限制前50份，避免太慢
            try:
                full_content = Path(r["path"]).read_text(encoding='utf-8', errors='ignore')
                for code in holdings:
                    if code in full_content:
                        mention_counter[code] += 1
            except Exception:
                continue

        top = []
        for code, count in mention_counter.most_common(10):
            top.append({
                "code": code,
                "mention_count": count,
                "sentiment": "neutral"  # 简化版，真实环境调用LLM判断
            })
        return top

    async def _identify_high_priority_events(self, reports: List[dict],
                                              holdings: List[str]) -> List[dict]:
        """识别高优先级事件（基于持仓频次+标题关键词）"""
        # 简化版：基于文件名前缀和持仓提及数
        priority_keywords = [
            "FOMC", "CPI", "非农", "暴涨", "暴跌", "突破", "突破", "重大",
            "央行", "降息", "加息", "冲突", "SpaceX", "英伟达", "WWDC"
        ]

        high_priority = []
        for r in reports:
            score = 0
            title = r["file"].replace(".md", "")

            for kw in priority_keywords:
                if kw in title or kw in r.get("preview", ""):
                    score += 2

            # 持仓提及加分
            try:
                content = Path(r["path"]).read_text(encoding='utf-8', errors='ignore')
                for code in holdings[:20]:
                    if code in content:
                        score += 1
            except Exception:
                pass

            if score >= 3:
                high_priority.append({
                    "event_id": r["file"].replace(".md", ""),
                    "title": title,
                    "score": score,
                    "urgency": "high" if score >= 5 else "medium"
                })

        return sorted(high_priority, key=lambda x: -x["score"])[:5]

    async def _generate_action_suggestions(self, top_holdings: List[dict],
                                           high_priority: List[dict]) -> List[dict]:
        """
        生成操作建议（真实LLM调用 - P1-T3 v1.0）

        使用InvestPilot的 llm_caller.DeepSeekClient 生成基于上下文的操作建议。
        失败时降级到规则引擎（mock模式）。
        """
        # 准备LLM上下文
        top_context = "\n".join([
            f"- {h['code']}: 被提及{h['mention_count']}次"
            for h in top_holdings[:5]
        ])

        high_context = "\n".join([
            f"- [{e.get('urgency', 'medium')}] {e.get('title', '')}"
            for e in high_priority[:5]
        ])

        prompt = f"""作为A股投资顾问，基于以下持仓提及频次和今日关键事件，生成3-5条具体操作建议。

## TOP持仓提及（按频次排序）
{top_context}

## 今日关键事件
{high_context}

## 要求
1. 每条建议必须包含: code(6位数字), action(buy/hold/reduce/sell/observe), reason(30-80字具体原因), confidence(0-1)
2. reason必须基于事件或持仓特征，不是空话
3. confidence根据事件相关性和市场风险评估
4. 仅输出JSON数组，不要其他说明

## 输出格式（严格JSON）
[{{"code": "002050", "action": "hold", "reason": "...", "confidence": 0.78}}]
"""

        system = "你是专业A股投资顾问，擅长从市场事件中提取交易信号。回答必须严格JSON格式。"

        # 真实LLM调用（带降级）
        try:
            import sys
            invest_scripts = Path("/mnt/c/PythonProject/invest_system/scripts")
            if str(invest_scripts) not in sys.path:
                sys.path.insert(0, str(invest_scripts))

            from dotenv import load_dotenv
            load_dotenv("/home/aileo/invest_system/.env")

            from llm_caller import get_llm_client
            client = get_llm_client()
            log_event("INFO", "event_analyst", "llm_call_start",
                      model=type(client).__name__, prompt_len=len(prompt))

            result = client.chat(prompt, system=system)
            log_event("INFO", "event_analyst", "llm_call_done",
                      has_error=bool(result.get('error')),
                      content_len=len(result.get('content', '')))

            if result.get('error'):
                raise Exception(result['error'])

            # 解析JSON（支持markdown代码块）
            content_str = result['content']
            json_match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', content_str, re.DOTALL)
            if json_match:
                content_str = json_match.group(1)

            # 尝试提取JSON数组
            if '[' in content_str and ']' in content_str:
                first = content_str.index('[')
                last = content_str.rindex(']') + 1
                content_str = content_str[first:last]

            llm_actions = json.loads(content_str)
            log_event("INFO", "event_analyst", "llm_parsed",
                      action_count=len(llm_actions) if isinstance(llm_actions, list) else 0)

            # 标准化输出
            actions = []
            for item in llm_actions if isinstance(llm_actions, list) else []:
                if not isinstance(item, dict):
                    continue
                if 'code' not in item or 'action' not in item:
                    continue
                actions.append({
                    "code": str(item['code']).strip(),
                    "name": next((h.get('name', '') for h in top_holdings
                                  if str(h['code']) == str(item['code'])), ''),
                    "action": str(item['action']).lower().strip(),
                    "reason": str(item.get('reason', ''))[:200],
                    "confidence": float(item.get('confidence', 0.5)),
                    "refs": [f"stock-{item['code']}", "events_today"]
                })

            if actions:
                return actions  # LLM成功

            log_event("WARNING", "event_analyst", "llm_empty_fallback_to_rules")

        except Exception as e:
            log_event("WARNING", "event_analyst", "llm_fallback_to_rules",
                      error=str(e)[:200])

        # 降级：规则引擎（mock模式）
        actions = []
        for h in top_holdings[:5]:
            actions.append({
                "code": h["code"],
                "name": "",
                "action": "observe",
                "reason": f"今日被提及{h['mention_count']}次，需关注（规则引擎降级）",
                "confidence": 0.65,
                "refs": [f"stock-{h['code']}", "events_today"]
            })

        return actions

    def _redact_for_output(self, actions: List[dict], role: str) -> List[dict]:
        """数据脱敏（补丁5）"""
        if role == "observer":
            # 公开模式：只保留方向
            return [{
                "code": "<hidden>",
                "action": a["action"],
                "reason": a["reason"][:50],
                "confidence": round(a["confidence"], 2)
            } for a in actions]
        elif role == "team":
            # 团队模式：保留代码，隐藏金额
            result = []
            for a in actions:
                result.append({
                    "code": a["code"][:2] + "***" + a["code"][-2:] if len(a["code"]) >= 4 else a["code"],
                    "action": a["action"],
                    "reason": a["reason"][:100],
                    "confidence": round(a["confidence"], 2),
                    "refs": a.get("refs", [])
                })
            return result
        else:
            return actions  # owner: 完整数据

    async def _push_to_channels(self, actions: List[dict]):
        """推送到钉钉/企微/Telegram"""
        # ⚠️ 真实环境需对接notification.py
        log_event("INFO", "event_analyst", "push_channels",
                  channels=["dingtalk", "telegram"],
                  actions_count=len(actions))


# ============================================================
# CLI 入口
# ============================================================
async def main():
    """主入口（dry-run模式）"""
    import argparse

    parser = argparse.ArgumentParser(description="Hermes Event Analyst")
    parser.add_argument("--date", help="目标日期 (YYYY-MM-DD)", default=None)
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="dry-run模式（不实际推送）")
    parser.add_argument("--live", action="store_true", help="实际推送（生产）")
    args = parser.parse_args()

    dry_run = not args.live

    analyst = HermesEventAnalyst(dry_run=dry_run)
    result = await analyst.scan(target_date=args.date)

    print("\n" + "="*80)
    print("📊 Hermes Event Analyst - 扫描结果")
    print("="*80)
    print(f"扫描报告数: {result['scanned_reports']}")
    print(f"扫描耗时: {result['scan_duration_seconds']}秒")
    print(f"TOP持仓提及: {len(result['top_holdings'])}")
    print(f"高优事件: {len(result['high_priority_events'])}")
    print(f"生成建议: {len(result['actions'])}")
    print(f"Dry-run: {result['dry_run']}")
    print("\n" + "="*80)
    print("TOP5 持仓提及：")
    for h in result["top_holdings"][:5]:
        print(f"  - {h['code']}: {h['mention_count']}次")
    print("\n" + "="*80)
    print("操作建议（脱敏后）：")
    for a in result["actions"][:5]:
        print(f"  - {a['code']}: {a['action']} (置信度{a['confidence']:.2f})")
        print(f"    理由: {a['reason']}")
    print("="*80)


if __name__ == "__main__":
    asyncio.run(main())
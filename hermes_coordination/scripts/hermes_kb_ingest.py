"""
hermes_kb_ingest.py
方案5: Hermes批量吸收AInvest新报告

每晚22:30自动批量吸收AInvest/reports/events/新增报告，
增量patch到Hermes skill库（保留v2.5最新内容）。

对应v2.0补丁:
- 补丁1: 接口契约 - skill_sync_contract
- 补丁2: 可观测性 - skill_patch_success_rate
- 补丁3: 成本控制 - < 1 RMB/天
- 补丁4: 时间窗口 - 与事件首席分析师串行
- 补丁5: 数据脱敏 - patch前过滤敏感信息

创建时间: 2026-06-11
版本: v0.1 (骨架版)
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import yaml

# ============================================================
# 配置
# ============================================================
SKILL_DIR = Path("/home/aileo/.hermes/skills/investing/hermes-investpilot-coordination-v2")
CONTRACTS_DIR = SKILL_DIR / "references" / "contracts"
HERMES_SKILLS_DIR = Path("/home/aileo/.hermes/skills/investing")
LOG_DIR = Path("/mnt/c/PythonProject/invest_system/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 日志
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / "hermes_agent.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("hermes_kb_ingest")


def log_event(level: str, component: str, action: str, **kwargs):
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "level": level,
        "component": component,
        "action": action,
        **kwargs
    }
    logger.info(json.dumps(log_entry, ensure_ascii=False))


# ============================================================
# 核心类: HermesKBIngester
# ============================================================
class HermesKBIngester:
    """Hermes知识库吸收器（方案5实现）"""

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.contract = load_contract("01-hermes-investpilot-contract.yaml")
        self.cost = load_contract("03-cost-estimation.yaml")

        # 状态
        self.ingested_reports = []
        self.patched_skills = []
        self.failed_patches = []
        self.start_time = None

    async def daily_ingest(self, target_date: Optional[str] = None) -> dict:
        """
        主入口: 每日批量吸收

        Returns:
            {
              ingested_reports: int,
              patched_skills: int,
              failed_patches: int,
              duration_seconds: float
            }
        """
        self.start_time = time.time()

        if target_date is None:
            target_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        log_event("INFO", "kb_ingest", "ingest_start",
                  target_date=target_date, dry_run=self.dry_run)

        try:
            # 步骤1: 加载合约（已在前置加载）
            log_event("INFO", "kb_ingest", "contracts_loaded",
                      contract_version=self.contract.get("version"))

            # 步骤2: 扫描新增events报告
            events_dir = Path("/mnt/c/PythonProject/AInvest/reports/events")
            new_reports = await self._scan_new_reports(events_dir, target_date)
            self.ingested_reports = new_reports
            log_event("INFO", "kb_ingest", "reports_loaded",
                      count=len(new_reports))

            # 步骤3: 提取持仓增量信息（mock LLM call）
            incremental_updates = await self._extract_incremental_updates(new_reports)

            # 步骤4: 应用patch（dry-run下不实际修改）
            if self.dry_run:
                log_event("INFO", "kb_ingest", "dry_run_skip_patches",
                          patches_planned=len(incremental_updates))
                self.patched_skills = [{"name": u["skill_name"],
                                        "action": "would_patch",
                                        "dry_run": True}
                                       for u in incremental_updates]
            else:
                for update in incremental_updates:
                    result = await self._apply_skill_patch(update)
                    if result["success"]:
                        self.patched_skills.append(result)
                    else:
                        self.failed_patches.append(result)

            # 步骤5: 备份（补丁9）
            if not self.dry_run and self.patched_skills:
                await self._backup_skills(target_date)

            duration = time.time() - self.start_time

            result = {
                "ingested_reports": len(new_reports),
                "patched_skills": len(self.patched_skills),
                "failed_patches": len(self.failed_patches),
                "duration_seconds": round(duration, 2),
                "dry_run": self.dry_run,
                "timestamp": datetime.now().isoformat()
            }

            log_event("INFO", "kb_ingest", "ingest_complete",
                      duration=duration,
                      ingested=len(new_reports),
                      patched=len(self.patched_skills),
                      failed=len(self.failed_patches))

            return result

        except Exception as e:
            log_event("ERROR", "kb_ingest", "ingest_failed",
                      error_code="E999", error_message=str(e))
            raise

    async def _scan_new_reports(self, events_dir: Path, target_date: str) -> List[dict]:
        """扫描新增报告"""
        if not events_dir.exists():
            return []

        reports = []
        for f in events_dir.glob(f"{target_date}*.md"):
            reports.append({
                "file": f.name,
                "path": str(f),
                "size_kb": round(f.stat().st_size / 1024, 1)
            })

        # 如果没找到该日期的，取最近的所有报告（最多30份）
        if not reports:
            all_reports = sorted(events_dir.glob("*.md"),
                                 key=lambda x: -x.stat().st_mtime)[:30]
            for f in all_reports:
                reports.append({
                    "file": f.name,
                    "path": str(f),
                    "size_kb": round(f.stat().st_size / 1024, 1)
                })

        return reports

    async def _extract_incremental_updates(self, reports: List[dict]) -> List[dict]:
        """提取增量更新（mock LLM分析）"""
        # ⚠️ 真实环境调用LLM
        # 简化版：从报告文件名识别持仓代码
        from collections import defaultdict
        stock_mentions = defaultdict(int)

        for r in reports[:30]:
            # 简单从文件名提取6位数字代码
            import re
            codes = re.findall(r'\b[0-9]{6}\b', r["file"])
            for code in codes:
                stock_mentions[code] += 1

        updates = []
        for code, count in sorted(stock_mentions.items(), key=lambda x: -x[1])[:5]:
            updates.append({
                "skill_name": f"stock-{code}",
                "action": "add_incremental_note",
                "new_section": f"""
### 关联事件（{datetime.now().strftime("%Y-%m-%d")}）
今日events目录中{count}份报告提及{code}，需关注市场情绪变化。
""",
                "ref_report_count": count
            })

        return updates

    async def _apply_skill_patch(self, update: dict) -> dict:
        """应用skill patch（真实环境调用skill_manage）"""
        # ⚠️ dry-run下不实际执行
        skill_name = update["skill_name"]
        skill_path = HERMES_SKILLS_DIR / skill_name / "SKILL.md"

        if not skill_path.exists():
            return {
                "skill_name": skill_name,
                "success": False,
                "error": f"Skill not found: {skill_path}"
            }

        # 真实环境调用 skill_manage(action='patch', ...)
        return {
            "skill_name": skill_name,
            "success": True,
            "action": "patched",
            "dry_run": self.dry_run
        }

    async def _backup_skills(self, target_date: str):
        """备份被修改的skill"""
        backup_dir = Path(f"/mnt/c/PythonProject/invest_system/backups/skills/{target_date}")
        backup_dir.mkdir(parents=True, exist_ok=True)

        for p in self.patched_skills:
            skill_name = p["skill_name"]
            src = HERMES_SKILLS_DIR / skill_name / "SKILL.md"
            if src.exists():
                dst = backup_dir / f"{skill_name}.md"
                dst.write_text(src.read_text(encoding='utf-8'), encoding='utf-8')

        log_event("INFO", "kb_ingest", "backup_complete",
                  backup_dir=str(backup_dir),
                  files=len(self.patched_skills))


def load_contract(name: str) -> dict:
    path = CONTRACTS_DIR / name
    if not path.exists():
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


# ============================================================
# CLI 入口
# ============================================================
async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Hermes KB Ingester")
    parser.add_argument("--date", default=None, help="目标日期 (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--live", action="store_true", help="实际执行patch")
    args = parser.parse_args()

    dry_run = not args.live
    ingester = HermesKBIngester(dry_run=dry_run)
    result = await ingester.daily_ingest(target_date=args.date)

    print("\n" + "="*80)
    print("📚 Hermes KB Ingester - 吸收结果")
    print("="*80)
    print(f"吸收报告: {result['ingested_reports']}")
    print(f"Patch skills: {result['patched_skills']}")
    print(f"失败patch: {result['failed_patches']}")
    print(f"耗时: {result['duration_seconds']}秒")
    print(f"Dry-run: {result['dry_run']}")
    print("="*80)


if __name__ == "__main__":
    asyncio.run(main())
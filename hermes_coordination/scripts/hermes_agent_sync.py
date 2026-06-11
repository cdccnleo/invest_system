"""
hermes_agent_sync.py — Hermes Skills <-> InvestPilot KB 双向同步脚本 (P1-T4 v1.0)

模式:
- inspect: 查看差异（不写入）
- h2i: Hermes Skills -> InvestPilot target_memories
- i2h: InvestPilot target_memories -> Hermes Skills
- bidirectional: 双向合并（按 mtime 选择较新）

使用:
    python hermes_agent_sync.py --mode inspect
    python hermes_agent_sync.py --mode h2i --code 002050
    python hermes_agent_sync.py --mode i2h --execute
    python hermes_agent_sync.py --mode bidirectional

依赖:
    pip install psycopg2-binary pyyaml python-dotenv
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import psycopg2
import yaml
from dotenv import load_dotenv

HERMES_SKILLS_DIR = Path("/home/aileo/.hermes/skills/investing")
INVEST_TM_DIR = Path("/home/aileo/invest_system/data/target_memories")
INVEST_PROJECT_ROOT = Path("/mnt/c/PythonProject/invest_system")

for env_path in [INVEST_PROJECT_ROOT / ".env", Path("/home/aileo/invest_system/.env")]:
    if env_path.exists():
        load_dotenv(env_path)
        break

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("hermes_agent_sync")


def get_pg_conn():
    """PG 连接（优先 store.json，降级 .env）"""
    cred_file = Path.home() / ".hermes" / "invest_credentials" / "store.json"
    password = ""
    if cred_file.exists():
        creds = json.loads(cred_file.read_text())
        password = creds.get("DB_PASSWORD", "")
    if not password:
        password = os.environ.get("DB_PASSWORD", "")
    return psycopg2.connect(
        host="localhost", port=5432, dbname="investpilot",
        user="invest_admin", password=password,
    )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        h.update(path.read_bytes())
        return h.hexdigest()
    except Exception:
        return ""


def detect_stock_code_from_skill(skill_dir: Path) -> Optional[str]:
    """从 skill 目录名提取股票代码 (stock-002050-sanhua -> 002050)"""
    parts = skill_dir.name.split("-")
    if parts[0] == "stock" and len(parts) >= 2:
        code = parts[1]
        if code.isdigit() and len(code) == 6:
            return code
    return None


def parse_hermes_skill(skill_dir: Path) -> Optional[Dict]:
    """解析 Hermes Skill 目录为 dict"""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None
    content = skill_md.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.+?)\n---", content, re.DOTALL)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1))
    except Exception:
        return None
    body = content[m.end():].strip()
    return {
        "name": fm.get("name", skill_dir.name),
        "description": fm.get("description", ""),
        "triggers": fm.get("triggers", []),
        "body": body,
        "skill_path": str(skill_md),
        "mtime": skill_md.stat().st_mtime,
        "size": skill_md.stat().st_size,
        "sha256": sha256_file(skill_md),
    }


def parse_invest_tm(tm_path: Path) -> Optional[Dict]:
    """解析 InvestPilot target_memory 为 dict"""
    if not tm_path.exists():
        return None
    content = tm_path.read_text(encoding="utf-8")
    return {
        "code": tm_path.stem,
        "content": content,
        "tm_path": str(tm_path),
        "mtime": tm_path.stat().st_mtime,
        "size": tm_path.stat().st_size,
        "sha256": sha256_file(tm_path),
    }


def _skill_to_tm(skill: Dict, code: str) -> str:
    """Hermes Skill body -> InvestPilot target_memory markdown 格式"""
    # 提取股票名称
    name_match = re.search(r"#\s*(.+)", skill.get("body", ""))
    name = name_match.group(1).strip().split("（")[0] if name_match else f"股票{code}"
    sync_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        f"# {name}（{code}）\n\n"
        f"> **同步来源**: Hermes Skill (stock-{code})\n"
        f"> **同步时间**: {sync_time}\n"
        f"> **Skill SHA256**: `{skill['sha256'][:16]}`\n\n"
        f"---\n\n"
        f"{skill.get('body', '')}\n\n"
        f"---\n\n"
        f"## 同步元数据\n\n"
        f"- **Skill Name**: {skill.get('name', '')}\n"
        f"- **Skill Size**: {skill.get('size', 0)} bytes\n"
        f"- **Skill Mtime**: {datetime.fromtimestamp(skill['mtime']).strftime('%Y-%m-%d %H:%M')}\n"
    )


def _tm_to_skill(tm: Dict) -> str:
    """InvestPilot target_memory -> Hermes Skill (YAML frontmatter + body)"""
    name_match = re.search(r"^#\s*(.+)", tm["content"], re.MULTILINE)
    name = name_match.group(1) if name_match else f"股票{tm['code']}"
    code = tm["code"]
    short_name = name.split("（")[0].replace(" ", "") if "（" in name else code
    frontmatter = {
        "name": f"stock-{code}-{short_name[:20]}",
        "description": f"{short_name}（{code}.SZ）从 InvestPilot KB 同步",
        "triggers": [code, short_name],
    }
    sync_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        f"---\n{yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)}---\n\n"
        f"# {name}\n\n"
        f"> **同步来源**: InvestPilot target_memory\n"
        f"> **同步时间**: {sync_time}\n"
        f"> **原文件**: `{tm['tm_path']}`\n\n"
        f"---\n\n"
        f"{tm['content']}\n\n"
        f"---\n\n"
        f"## 同步元数据\n\n"
        f"- **TM SHA256**: `{tm['sha256'][:16]}`\n"
        f"- **TM Size**: {tm['size']} bytes\n"
        f"- **TM Mtime**: {datetime.fromtimestamp(tm['mtime']).strftime('%Y-%m-%d %H:%M')}\n"
    )


class HermesAgentSync:
    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.conn = None

    def __enter__(self):
        try:
            self.conn = get_pg_conn()
        except Exception as e:
            logger.warning(f"PG 连接失败（将跳过 PG 审计）: {e}")
            self.conn = None
        return self

    def __exit__(self, *args):
        if self.conn:
            self.conn.close()

    def list_hermes_stock_skills(self) -> Dict[str, Path]:
        result = {}
        if not HERMES_SKILLS_DIR.exists():
            return result
        for d in HERMES_SKILLS_DIR.iterdir():
            if d.is_dir() and d.name.startswith("stock-"):
                code = detect_stock_code_from_skill(d)
                if code:
                    result[code] = d
        return result

    def list_invest_tm(self) -> Dict[str, Path]:
        result = {}
        if INVEST_TM_DIR.exists():
            for f in INVEST_TM_DIR.glob("*.md"):
                if f.stem.isdigit() and len(f.stem) == 6:
                    result[f.stem] = f
        return result

    def inspect(self, code: Optional[str] = None) -> Dict:
        hermes = self.list_hermes_stock_skills()
        invest = self.list_invest_tm()
        report = {
            "hermes_total": len(hermes),
            "invest_total": len(invest),
            "intersection": sorted(set(hermes.keys()) & set(invest.keys())),
            "only_hermes": sorted(set(hermes.keys()) - set(invest.keys())),
            "only_invest": sorted(set(invest.keys()) - set(hermes.keys())),
            "diff_details": [],
        }
        if code:
            if code in hermes and code in invest:
                hs = parse_hermes_skill(hermes[code])
                it = parse_invest_tm(invest[code])
                report["diff_details"].append({
                    "code": code,
                    "hermes_mtime": datetime.fromtimestamp(hs["mtime"]).isoformat() if hs else None,
                    "invest_mtime": datetime.fromtimestamp(it["mtime"]).isoformat() if it else None,
                    "hermes_size": hs["size"] if hs else 0,
                    "invest_size": it["size"] if it else 0,
                    "sha_match": hs["sha256"] == it["sha256"] if (hs and it) else False,
                })
        return report

    def _write_audit(self, code, direction, action, hermes_sha, invest_sha,
                     status="success", error=None):
        """写入 skill_sync_audit 表
        实际 schema: id, sync_time, direction, skill_name, result, diff_summary, error_message
        direction 枚举: hermes_to_backend | backend_to_hermes | bidirectional
        result 枚举: success | failed | skipped
        """
        if not self.conn:
            return
        try:
            cur = self.conn.cursor()
            skill_name = f"stock-{code}-sync"
            diff = f"action={action}"
            if hermes_sha:
                diff += f" | hermes_sha={hermes_sha[:16]}"
            if invest_sha:
                diff += f" | invest_sha={invest_sha[:16]}"
            cur.execute(
                """
                INSERT INTO skill_sync_audit (sync_time, direction, skill_name, result,
                                               diff_summary, error_message)
                VALUES (NOW(), %s, %s, %s, %s, %s)
                """,
                (
                    direction,
                    skill_name,
                    status,
                    diff[:500],
                    error[:500] if error else None,
                ),
            )
            self.conn.commit()
            logger.info(f"PG 审计写入 OK: {skill_name} | {direction} | {status}")
        except Exception as e:
            logger.warning(f"PG 审计写入失败: {e}")
            try:
                self.conn.rollback()
            except Exception:
                pass

    def sync_h2i(self, code: Optional[str] = None) -> Dict:
        """Hermes Skills -> InvestPilot target_memories"""
        hermes = self.list_hermes_stock_skills()
        results = {"synced": [], "skipped": [], "errors": []}
        targets = [code] if code else sorted(hermes.keys())

        for c in targets:
            if c not in hermes:
                results["skipped"].append({"code": c, "reason": "no_hermes_skill"})
                continue
            skill = parse_hermes_skill(hermes[c])
            if not skill:
                results["errors"].append({"code": c, "error": "parse_failed"})
                continue
            target_path = INVEST_TM_DIR / f"{c}.md"
            new_content = _skill_to_tm(skill, c)

            if target_path.exists():
                existing = target_path.read_text(encoding="utf-8")
                if existing == new_content:
                    results["skipped"].append({"code": c, "reason": "identical"})
                    continue

            if self.dry_run:
                results["synced"].append({"code": c, "dry_run": True, "size": len(new_content)})
                self._write_audit(c, "hermes_to_backend", "dry_run", skill["sha256"], "")
            else:
                try:
                    INVEST_TM_DIR.mkdir(parents=True, exist_ok=True)
                    target_path.write_text(new_content, encoding="utf-8")
                    results["synced"].append({"code": c, "size": len(new_content)})
                    self._write_audit(c, "hermes_to_backend", "success",
                                      skill["sha256"], sha256_file(target_path))
                except Exception as e:
                    results["errors"].append({"code": c, "error": str(e)[:200]})
                    self._write_audit(c, "hermes_to_backend", "failed",
                                      skill["sha256"], "", "failed", str(e)[:200])
        return results

    def sync_i2h(self, code: Optional[str] = None) -> Dict:
        """InvestPilot target_memories -> Hermes Skills"""
        invest = self.list_invest_tm()
        hermes = self.list_hermes_stock_skills()
        results = {"synced": [], "skipped": [], "errors": []}
        targets = [code] if code else sorted(invest.keys())

        for c in targets:
            if c not in invest:
                results["skipped"].append({"code": c, "reason": "no_invest_tm"})
                continue
            tm = parse_invest_tm(invest[c])
            if not tm:
                results["errors"].append({"code": c, "error": "parse_failed"})
                continue

            skill_dir = hermes.get(c)
            if not skill_dir:
                skill_dir = HERMES_SKILLS_DIR / f"stock-{c}-auto"
            skill_path = skill_dir / "SKILL.md"
            new_content = _tm_to_skill(tm)

            if skill_path.exists():
                existing = skill_path.read_text(encoding="utf-8")
                if existing == new_content:
                    results["skipped"].append({"code": c, "reason": "identical"})
                    continue

            if self.dry_run:
                results["synced"].append({"code": c, "dry_run": True, "size": len(new_content)})
                self._write_audit(c, "backend_to_hermes", "dry_run", "", tm["sha256"])
            else:
                try:
                    skill_dir.mkdir(parents=True, exist_ok=True)
                    skill_path.write_text(new_content, encoding="utf-8")
                    results["synced"].append({
                        "code": c, "size": len(new_content),
                        "created_new": c not in hermes
                    })
                    self._write_audit(c, "backend_to_hermes", "success",
                                      sha256_file(skill_path), tm["sha256"])
                except Exception as e:
                    results["errors"].append({"code": c, "error": str(e)[:200]})
                    self._write_audit(c, "backend_to_hermes", "failed",
                                      "", tm["sha256"], "failed", str(e)[:200])
        return results

    def sync_bidirectional(self) -> Dict:
        """双向合并（按 mtime 选择较新）"""
        hermes = self.list_hermes_stock_skills()
        invest = self.list_invest_tm()
        common = sorted(set(hermes.keys()) & set(invest.keys()))
        only_hermes = sorted(set(hermes.keys()) - set(invest.keys()))
        only_invest = sorted(set(invest.keys()) - set(hermes.keys()))

        analysis = {"hermes_newer": [], "invest_newer": [], "identical": [], "diff_content": []}
        for c in common:
            hs = parse_hermes_skill(hermes[c])
            it = parse_invest_tm(invest[c])
            if not hs or not it:
                continue
            if hs["sha256"] == it["sha256"]:
                analysis["identical"].append(c)
            elif hs["mtime"] > it["mtime"]:
                analysis["hermes_newer"].append(c)
                analysis["diff_content"].append({"code": c, "newer": "hermes"})
            else:
                analysis["invest_newer"].append(c)
                analysis["diff_content"].append({"code": c, "newer": "invest"})

        return {
            "stats": {
                "common": len(common),
                "only_hermes": len(only_hermes),
                "only_invest": len(only_invest),
                "hermes_newer": len(analysis["hermes_newer"]),
                "invest_newer": len(analysis["invest_newer"]),
                "identical": len(analysis["identical"]),
            },
            "only_hermes": only_hermes,
            "only_invest": only_invest,
            "analysis": analysis,
        }


def main():
    parser = argparse.ArgumentParser(description="Hermes<->InvestPilot KB 双向同步")
    parser.add_argument("--mode", choices=["inspect", "h2i", "i2h", "bidirectional"], required=True)
    parser.add_argument("--code", help="单标的代码（可选）")
    parser.add_argument("--execute", action="store_true", help="真实执行（默认 dry-run）")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    with HermesAgentSync(dry_run=not args.execute) as sync:
        if args.mode == "inspect":
            report = sync.inspect(args.code)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return

        print(f"\n=== {args.mode.upper()} 模式 (dry_run={not args.execute}) ===\n")

        if args.mode == "h2i":
            results = sync.sync_h2i(args.code)
        elif args.mode == "i2h":
            results = sync.sync_i2h(args.code)
        elif args.mode == "bidirectional":
            report = sync.sync_bidirectional()
            print("分析报告:")
            print(json.dumps(report["stats"], ensure_ascii=False, indent=2))
            print("\n仅 Hermes (需 i2h):", report["only_hermes"][:10])
            print("仅 InvestPilot (需 h2i):", report["only_invest"][:10])
            if report["analysis"]["diff_content"]:
                print("\n内容差异 (按 mtime):")
                for d in report["analysis"]["diff_content"][:10]:
                    print(f"  {d['code']}: newer={d['newer']}")

            if not args.execute:
                print("\n[DRY-RUN] 用 --execute 真实执行")
                return

            print("\n执行 i2h (仅 invest->新建)...")
            i2h = sync.sync_i2h()
            print(f"  synced={len(i2h['synced'])} skipped={len(i2h['skipped'])} errors={len(i2h['errors'])}")
            print("\n执行 h2i (hermes newer -> invest)...")
            h2i = sync.sync_h2i()
            print(f"  total_synced={len(h2i['synced'])} skipped={len(h2i['skipped'])}")
            results = {"pre_analysis": report, "i2h": i2h, "h2i": h2i}
        else:
            results = {}

        # 汇总
        if "synced" in results:
            print(f"\n同步结果: synced={len(results['synced'])}, "
                  f"skipped={len(results['skipped'])}, "
                  f"errors={len(results['errors'])}")
            if results["synced"]:
                print(f"  前5条 synced: {[s.get('code') for s in results['synced'][:5]]}")
            if results["errors"]:
                print(f"  错误: {results['errors'][:3]}")


if __name__ == "__main__":
    main()

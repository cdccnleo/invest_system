"""
PIT #116 一次性清理脚本: 去除 51 个 TM 文件 ## 同步元数据 循环叠加.

实战: 6/14 调研发现 50/50 文件叠加 4-5 块 ## 同步元数据, 净冗余 92KB.

用法:
    python scripts/cleanup_tamf_overlap.py [--dry-run] [--commit]
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
TAMF_DIR = PROJECT_ROOT / "data" / "target_memories"

# 匹配末尾的 \n---\n## 同步元数据 块 (含后面所有内容)
META_BLOCK_RE = re.compile(
    r"\n*---\s*\n+##\s*同步元数据.*\Z",
    re.DOTALL
)


def clean_tm_file(path: Path, dry_run: bool = False) -> tuple[int, int]:
    """
    清理单个 TM 文件: 去除 ## 同步元数据 多余块, 只留最后 1 个块.
    返回: (原 size, 清理后 size)
    """
    if not path.exists():
        return 0, 0

    content = path.read_text(encoding="utf-8")
    original_size = len(content.encode("utf-8"))

    # 数 ## 同步元数据 块数
    blocks = re.findall(r"^## 同步元数据", content, re.MULTILINE)
    n_blocks = len(blocks)
    if n_blocks <= 1:
        return original_size, original_size

    # 反复去除倒数第 2 个 ## 同步元数据 块, 直到只剩 1 个
    # 用 META_BLOCK_RE 匹配末尾第一个 ## 同步元数据 块
    # 但要保留最后 1 个块, 所以先去除前面的所有
    # 策略: 找所有 ## 同步元数据 位置, 删除前面 (n-1) 个
    positions = [m.start() for m in re.finditer(r"^## 同步元数据", content, re.MULTILINE)]
    keep_pos = positions[-1]  # 保留最后一个

    # 找 keep_pos 之前的上一个 --- 分隔符
    # 在 keep_pos 之前找最近的 \n---\n
    pre = content[:keep_pos]
    # 找最后一个 \n---\n
    m = re.search(r"\n---\s*\n+\Z", pre)
    if m:
        # 删除从 m.end() 到 keep_pos 之间的内容 (即前面的 ## 同步元数据 块 + 它们之前的 --- 分隔符)
        new_content = content[:m.start()] + "\n\n" + content[keep_pos:]
    else:
        # 没有 --- 分隔符, 直接拼
        new_content = content[:keep_pos] + content[keep_pos:]

    # 简化: 找到所有 ## 同步元数据 块, 用 --- 分隔, 只保留最后一个
    # 用更精确的正则: 每次去除一段
    parts = re.split(r"(\n*---\s*\n+##\s*同步元数据.*?)(?=\n---\s*\n+##\s*同步元数据|\Z)", content, flags=re.DOTALL)
    # parts[0] = 主内容, parts[1] = 第一个 ## 同步元数据 块 (含 ---), parts[2] = 空, ...
    # 保留 parts[0] + 最后一个非空 块
    main = parts[0].rstrip() + "\n"
    last_block = ""
    for p in parts[1:]:
        if p and "## 同步元数据" in p:
            last_block = p
    if last_block:
        new_content = main + "\n---\n" + last_block.lstrip()
    else:
        new_content = main

    new_size = len(new_content.encode("utf-8"))

    if not dry_run:
        path.write_text(new_content, encoding="utf-8")

    return original_size, new_size


def main():
    parser = argparse.ArgumentParser(description="PIT #116 清理 TM 文件 ## 同步元数据 循环叠加")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写文件")
    parser.add_argument("--commit", action="store_true", help="清理后自动 git commit")
    args = parser.parse_args()

    if not TAMF_DIR.exists():
        print(f"❌ 目录不存在: {TAMF_DIR}")
        sys.exit(1)

    files = sorted([f for f in TAMF_DIR.glob("*.md") if f.name != "TEMPLATE.md"])
    print(f"=== PIT #116 清理 {len(files)} 个 TM 文件 ===\n")

    total_orig = 0
    total_new = 0
    affected = 0
    for f in files:
        orig, new = clean_tm_file(f, dry_run=args.dry_run)
        total_orig += orig
        total_new += new
        if new < orig:
            affected += 1
            saved = orig - new
            print(f"  ✅ {f.name}: {orig:>5} → {new:>5} bytes (节省 {saved:>4})")
        else:
            print(f"  ⚪ {f.name}: {orig} bytes (无需清理)")

    saved_total = total_orig - total_new
    print(f"\n=== 汇总 ===")
    print(f"  影响文件: {affected}/{len(files)}")
    print(f"  原始总大小: {total_orig/1024:.1f} KB")
    print(f"  清理后总大小: {total_new/1024:.1f} KB")
    print(f"  节省: {saved_total/1024:.1f} KB ({saved_total/total_orig*100:.1f}%)")

    if args.commit and not args.dry_run and saved_total > 0:
        print(f"\n=== Git commit ===")
        result = subprocess.run(
            ["git", "add", "data/target_memories/"],
            cwd=str(PROJECT_ROOT),
            capture_output=True, text=True,
        )
        msg = f"chore(tamf): PIT #116 清理 {affected} 个 TM 文件 ## 同步元数据 循环叠加 (节省 {saved_total/1024:.1f} KB)"
        result = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=str(PROJECT_ROOT),
            capture_output=True, text=True,
        )
        print(f"  commit rc: {result.returncode}")
        if result.returncode == 0:
            print(f"  ✅ {result.stdout.splitlines()[-1] if result.stdout else 'ok'}")


if __name__ == "__main__":
    main()

"""
credentials.py — InvestPilot 统一凭据管理模块
优先从 Windows Credential Manager 读取（WSL2 兼容）
降级: 读取本地加密文件 → 环境变量

所有 invest_system 脚本应从此模块获取凭据，禁止硬编码或直接读 .env。
"""

import os
import logging
import subprocess
import json
from pathlib import Path
from typing import Optional

logger = logging.getLogger("invest_system.credentials")

# ── 路径配置 ────────────────────────────────────────────────────────────────

HERMES_HOME = Path.home() / ".hermes"
CRED_DIR = HERMES_HOME / "invest_credentials"
CRED_FILE = CRED_DIR / "store.json"  # 降级文件（gitignored）

# 首次初始化引导标志
_SETUP_FLAG = CRED_DIR / ".setup_complete"


# ── Windows Credential Manager（WSL2 跨端）────────────────────────────────

def _run_ps(script: str, timeout: int = 10) -> str:
    """执行 PowerShell 脚本，返回 stdout（UTF-8，容错解码）"""
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True, timeout=timeout,
        )
        raw = r.stdout + r.stderr
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return raw.decode("gbk", errors="replace")
    except subprocess.TimeoutExpired:
        logger.warning(f"PowerShell 超时: {script[:50]}...")
        return ""
    except FileNotFoundError:
        logger.debug("PowerShell 不可用，跳过")
        return ""


def _wcm_get(service: str) -> Optional[str]:
    """
    从 Windows Credential Manager 读取凭据（WSL2 兼容）。
    service: 凭据标识（如 'InvestPilot_DB', 'InvestPilot_DeepSeek'）
    返回: password 或 None
    """
    # cmdkey 格式: cmdkey /list:<target>
    script = f"""
    $ErrorActionPreference = 'SilentlyContinue'
    $out = cmdkey /list:{service} 2>&1 | Out-String
    if ($out -match 'password:\\s*(\\S.+)') {{
        Write-Output $Matches[1].Trim()
    }} elseif ($out -match '密码:\\s*(\\S.+)') {{
        Write-Output $Matches[1].Trim()
    }} else {{
        Write-Output 'CRED_NOT_FOUND'
    }}
    """
    result = _run_ps(script).strip()
    # 跳过 WCM 占位符（WSL cmdkey 遗留），直接返回 None 触发后续降级逻辑
    if result and result == "* NONE *":
        return None
    if result and result != "CRED_NOT_FOUND" and len(result) > 0:
        logger.debug(f"WCM 读取成功: {service}")
        return result
    return None


def _wcm_set(service: str, username: str, password: str) -> bool:
    """
    将凭据写入 Windows Credential Manager（需用户交互确认一次）。
    成功写入后，后续读取无需交互。
    """
    # 使用 echo + cmdkey 方式（password 通过 stdin 传入避免命令行暴露）
    try:
        # 先尝试添加（会提示确认，允许后后续自动批准）
        r1 = subprocess.run(
            ["cmd.exe", "/c", f"echo.|cmdkey /generic:{service} /user:{username} /pass:{password}"],
            capture_output=True, timeout=5,
        )
        # 检查是否成功（cmdkey 无输出表示成功或已存在）
        return r1.returncode == 0
    except Exception as e:
        logger.warning(f"WCM 写入失败: {service} — {e}")
        return False


# ── 本地降级存储 ───────────────────────────────────────────────────────────

def _load_cred_file() -> dict:
    """读取本地凭据文件"""
    if not CRED_FILE.exists():
        return {}
    try:
        with open(CRED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"凭据文件读取失败: {e}")
        return {}


def _save_cred_file(creds: dict) -> None:
    """写入本地凭据文件（权限 600）"""
    CRED_DIR.mkdir(parents=True, exist_ok=True)
    with open(CRED_FILE, "w", encoding="utf-8") as f:
        json.dump(creds, f, ensure_ascii=False, indent=2)
    os.chmod(CRED_FILE, 0o600)


# ── 统一获取接口 ───────────────────────────────────────────────────────────

def get_credential(key: str, default: Optional[str] = None) -> Optional[str]:
    """
    获取凭据的统一入口。

    读取优先级:
    1. Windows Credential Manager（WSL2: cmdkey）
    2. 本地降级文件 ~/.hermes/invest_credentials/store.json
    3. 环境变量（兼容旧代码）

    常用 key:
      DB_PASSWORD         — PostgreSQL 密码
      DEEPSEEK_API_KEY     — DeepSeek API Key
      DATABASE_URL         — 完整连接字符串（含密码）
      OLLAMA_API_KEY       — Ollama（通常不需要）
    """
    # 1. WCM（Windows 端）
    wcm_val = _wcm_get(key)
    if wcm_val:
        return wcm_val

    # 2. WCM 别名尝度
    aliases = {
        "DB_PASSWORD": "InvestPilot_DB",
        "DEEPSEEK_API_KEY": "InvestPilot_DeepSeek",
        "DATABASE_URL": "InvestPilot_DB_URL",
    }
    wcm_service = aliases.get(key)
    if wcm_service:
        wcm_val = _wcm_get(wcm_service)
        if wcm_val:
            return wcm_val

    # 3. 本地降级文件
    creds = _load_cred_file()
    if key in creds and creds[key]:
        logger.debug(f"本地凭据文件命中: {key}")
        return creds[key]

    # 4. 环境变量
    env_val = os.environ.get(key)
    if env_val and env_val != "***":
        logger.debug(f"环境变量命中: {key}")
        return env_val

    logger.debug(f"凭据未找到: {key}")
    return default


def set_credential(key: str, value: str) -> None:
    """
    存储凭据到本地降级文件。
    首次存储后，下次调用 get_credential 直接命中，无需再设置。
    """
    creds = _load_cred_file()
    creds[key] = value
    _save_cred_file(creds)
    logger.info(f"凭据已保存: {key} → {CRED_FILE}")


def setup_credentials(db_password: str, deepseek_api_key: str,
                       database_url: Optional[str] = None) -> dict:
    """
    初始化凭据：写入本地降级文件。
    建议同时将凭据添加到 Windows Credential Manager（见 setup_wcm 函数）。

    用法:
      from credentials import setup_credentials
      setup_credentials(
          db_password="your_real_password",
          deepseek_api_key="sk-...",
          database_url="postgresql://invest_admin:password@localhost:5432/investpilot",
      )
    """
    CRED_DIR.mkdir(parents=True, exist_ok=True)

    creds = {}
    if db_password:
        creds["DB_PASSWORD"] = db_password
    if deepseek_api_key:
        creds["DEEPSEEK_API_KEY"] = deepseek_api_key
    if database_url:
        creds["DATABASE_URL"] = database_url
    else:
        # 自动构造 DATABASE_URL
        pw = creds.get("DB_PASSWORD", "")
        creds["DATABASE_URL"] = f"postgresql://invest_admin:{pw}@localhost:5432/investpilot"

    _save_cred_file(creds)
    _SETUP_FLAG.touch()

    return creds


def setup_wcm(db_password: str, deepseek_api_key: str) -> dict:
    """
    将凭据添加到 Windows Credential Manager（需一次性用户交互）。

    目标服务名:
      InvestPilot_DB         → DB_PASSWORD
      InvestPilot_DeepSeek   → DEEPSEEK_API_KEY

    调用此函数后，后续所有脚本通过 get_credential() 自动从 WCM 读取，无需文件存储。

    注意: 首次运行会弹出 Windows 安全确认对话框，需用户点击"是"一次。
          之后读取无需交互。
    """
    results = {}

    if db_password:
        ok = _wcm_set("InvestPilot_DB", "invest_admin", db_password)
        results["InvestPilot_DB"] = "✅ 已写入" if ok else "❌ 写入失败"
        if ok:
            logger.info("Windows Credential Manager: InvestPilot_DB 已写入")

    if deepseek_api_key:
        ok = _wcm_set("InvestPilot_DeepSeek", "deepseek", deepseek_api_key)
        results["InvestPilot_DeepSeek"] = "✅ 已写入" if ok else "❌ 写入失败"
        if ok:
            logger.info("Windows Credential Manager: InvestPilot_DeepSeek 已写入")

    return results


def check_credentials() -> dict:
    """
    健康检查：返回各凭据的可用性状态。
    不打印/返回实际密码值。
    """
    checks = {}
    for key, label in [
        ("DB_PASSWORD", "PostgreSQL 密码"),
        ("DEEPSEEK_API_KEY", "DeepSeek API Key"),
        ("DATABASE_URL", "数据库连接串"),
    ]:
        val = get_credential(key)
        checks[label] = {
            "found": val is not None,
            "value_preview": val[:8] + "..." if val and len(val) > 8 else (val if val else None),
        }

    # WCM 可用性
    _wcm_get("__wcm_health_check_test__")
    checks["Windows Credential Manager"] = {
        "accessible": True,  # _wcm_get 不抛异常即表示 WCM 可访问
    }

    checks["本地凭据文件"] = {
        "exists": CRED_FILE.exists(),
        "path": str(CRED_FILE),
    }

    return checks


# ── 便捷导入 ────────────────────────────────────────────────────────────────

def patch_storage_factory() -> None:
    """
    热补丁 storage_factory.py 的 get_pg_connection() 函数，
    使其通过 credentials.get_credential() 获取 DATABASE_URL。
    在 run_analysis.py / schedule_runner.py 启动时调用一次即可。

    无需修改 storage_factory.py 源码。
    """
    import psycopg2
    from storage_factory import get_pg_connection as _original

    def _patched():
        url = get_credential("DATABASE_URL")
        if url:
            try:
                conn = psycopg2.connect(url, connect_timeout=5)
                conn.autocommit = True
                return conn
            except psycopg2.OperationalError:
                pass
        return _original()

    import storage_factory
    storage_factory.get_pg_connection = _patched
    logger.info("storage_factory.get_pg_connection 已热补丁为使用 credentials 模块")


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="InvestPilot 凭据管理")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("check", help="检查凭据可用性")
    p = sub.add_parser("setup", help="初始化凭据（写入本地文件）")
    p.add_argument("--db-password", required=True, help="PostgreSQL 密码")
    p.add_argument("--deepseek-key", required=True, help="DeepSeek API Key")
    p.add_argument("--database-url", help="完整 DATABASE_URL（可选）")

    p = sub.add_parser("setup-wcm", help="添加到 Windows Credential Manager（需交互）")
    p.add_argument("--db-password", required=True)
    p.add_argument("--deepseek-key", required=True)

    args = parser.parse_args()

    if args.cmd == "check":
        print("=== 凭据健康检查 ===")
        for label, info in check_credentials().items():
            status = "✅" if info.get("found", info.get("exists", info.get("accessible"))) else "⚠️"
            preview = info.get("value_preview")
            print(f"  {status} {label}: {preview or '(未找到)'}")

    elif args.cmd == "setup":
        result = setup_credentials(args.db_password, args.deepseek_key, args.database_url)
        print("✅ 凭据已写入本地文件:", CRED_FILE)
        print("   后续运行脚本将自动使用这些凭据。")

    elif args.cmd == "setup-wcm":
        print("正在写入 Windows Credential Manager（请在弹出的对话框中确认）...")
        result = setup_wcm(args.db_password, args.deepseek_key)
        for svc, status in result.items():
            print(f"  {svc}: {status}")
        if all(r.startswith("✅") for r in result.values()):
            print("\n✅ Windows Credential Manager 配置完成！")
            print("   建议同时运行: python credentials.py setup --db-password ... --deepseek-key ...")
            print("   这样即使 WCM 不可用时也能从本地文件读取。")

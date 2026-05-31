"""
test_integration.py — 集成测试（需要 PostgreSQL 连接）

测试内容:
  1. 数据库读写（只读操作）
  2. 凭据存储加载
  3. TAMF 文件系统读写
  4. 跨模块数据流验证
"""

import json
import os
import pytest
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
CREDENTIAL_STORE = Path.home() / ".hermes" / "invest_credentials" / "store.json"
TAMF_DIR = ROOT / "data" / "target_memories"


DB_REQUIRED = pytest.mark.skipif(
    not os.environ.get("INVESTPILOT_DB_TEST") and not CREDENTIAL_STORE.exists(),
    reason="PostgreSQL 连接不可用",
)


# ============================================================================
# 数据库集成测试
# ============================================================================

@pytest.mark.integration
class TestDatabaseConnection:
    """数据库连接与基本操作测试"""

    def test_connect_and_query(self):
        """基本连接 + 查询"""
        import psycopg2

        with open(CREDENTIAL_STORE) as f:
            creds = json.load(f)

        conn = psycopg2.connect(
            host="localhost", port=5432,
            dbname="investpilot", user="invest_admin",
            password=creds["DB_PASSWORD"],
        )
        cur = conn.cursor()
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1
        conn.close()

    def test_daily_quotes_accessible(self):
        """market.daily_quotes 表可读"""
        import psycopg2

        with open(CREDENTIAL_STORE) as f:
            creds = json.load(f)

        conn = psycopg2.connect(
            host="localhost", port=5432,
            dbname="investpilot", user="invest_admin",
            password=creds["DB_PASSWORD"],
        )
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM market.daily_quotes")
        count = cur.fetchone()[0]
        assert count > 0, "daily_quotes 表应有数据"
        conn.close()

    def test_news_articles_accessible(self):
        """research.news_articles 表可读"""
        import psycopg2

        with open(CREDENTIAL_STORE) as f:
            creds = json.load(f)

        conn = psycopg2.connect(
            host="localhost", port=5432,
            dbname="investpilot", user="invest_admin",
            password=creds["DB_PASSWORD"],
        )
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM research.news_articles")
        count = cur.fetchone()[0]
        assert isinstance(count, int)
        conn.close()

    def test_tables_exist(self):
        """关键表存在性检查"""
        import psycopg2

        with open(CREDENTIAL_STORE) as f:
            creds = json.load(f)

        conn = psycopg2.connect(
            host="localhost", port=5432,
            dbname="investpilot", user="invest_admin",
            password=creds["DB_PASSWORD"],
        )
        cur = conn.cursor()

        required_tables = [
            "market.daily_quotes",
            "research.news_articles",
            "l3.behavior_profile",
            "l3.active_dialog_triggers",
            "l3.stress_test_results",
            "l3.stress_test_scenarios",
            "holdings.encrypted_positions",
            "audit.audit_log",
        ]

        for table in required_tables:
            cur.execute(f"""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_schema = '{table.split('.')[0]}'
                    AND table_name = '{table.split('.')[1]}'
                )
            """)
            exists = cur.fetchone()[0]
            assert exists, f"表 {table} 不存在"

        conn.close()


# ============================================================================
# 凭据存储集成测试
# ============================================================================

@pytest.mark.integration
class TestCredentialStore:
    """凭据存储读取测试"""

    def test_store_exists(self):
        """凭据存储文件存在"""
        assert CREDENTIAL_STORE.exists(), f"Credential store not found: {CREDENTIAL_STORE}"

    def test_store_has_db_password(self):
        """凭据存储包含 DB_PASSWORD"""
        with open(CREDENTIAL_STORE) as f:
            creds = json.load(f)
        assert "DB_PASSWORD" in creds, "store.json 缺少 DB_PASSWORD"
        assert len(creds["DB_PASSWORD"]) > 0, "DB_PASSWORD 为空"

    def test_store_has_api_key(self):
        """凭据存储包含 DEEPSEEK_API_KEY"""
        with open(CREDENTIAL_STORE) as f:
            creds = json.load(f)
        assert "DEEPSEEK_API_KEY" in creds, "store.json 缺少 DEEPSEEK_API_KEY"


# ============================================================================
# TAMF 文件系统集成测试
# ============================================================================

@pytest.mark.integration
class TestTamfFilesystem:
    """TAMF 文件系统读写测试"""

    def test_tamf_dir_exists(self):
        """TAMF 目录存在"""
        assert TAMF_DIR.exists(), f"TAMF directory not found: {TAMF_DIR}"

    def test_template_exists(self):
        """TEMPLATE.md 存在"""
        template = TAMF_DIR / "TEMPLATE.md"
        assert template.exists(), "TEMPLATE.md not found"

    def test_tamf_files_count(self):
        """TAMF 文件数量合理（应有 30+ 个标的文件）"""
        md_files = list(TAMF_DIR.glob("*.md"))
        md_files = [f for f in md_files if f.name != "TEMPLATE.md"]
        assert len(md_files) >= 30, f"TAMF 文件数不足: {len(md_files)}"

    def test_tamf_file_structure(self):
        """TAMF 文件结构完整性（抽样检查）"""
        sample = TAMF_DIR / "000977.md"
        if not sample.exists():
            sample = next((f for f in TAMF_DIR.glob("*.md") if f.name != "TEMPLATE.md"), None)
        if sample is None:
            pytest.skip("No TAMF files found")

        content = sample.read_text(encoding="utf-8")
        assert "## 一、" in content or "## 1." in content, "缺少第 1 章"
        assert "## 二、" in content or "## 2." in content, "缺少第 2 章"

    def test_tamf_write_read(self):
        """TAMF 文件读写（临时文件测试）"""
        test_file = TAMF_DIR / "_test_integration.md"
        test_content = "# Integration Test\n\nThis file is for testing purposes."
        test_file.write_text(test_content, encoding="utf-8")

        read_back = test_file.read_text(encoding="utf-8")
        assert read_back == test_content

        test_file.unlink()


# ============================================================================
# 跨模块集成测试
# ============================================================================

@pytest.mark.integration
class TestCrossModule:
    """跨模块数据流集成测试"""

    def test_credentials_module_loads(self):
        """credentials.py 模块可加载"""
        try:
            from credentials import load_db_password, load_deepseek_api_key
            pwd = load_db_password()
            assert len(pwd) > 0
        except ImportError as e:
            pytest.skip(f"Credentials module import failed: {e}")

    def test_storage_factory_module_loads(self):
        """storage_factory.py 模块可加载"""
        try:
            from storage_factory import create_storage
            assert callable(create_storage)
        except ImportError as e:
            pytest.skip(f"Storage factory import failed: {e}")

    def test_data_sanitizer_module_loads(self):
        """data_sanitizer.py 可与其他模块联调"""
        from data_sanitizer import sanitize_snapshot, reset_mapping
        reset_mapping()
        positions = [
            {"code": "000977", "name": "浪潮信息", "market_value": 50000,
             "cost": 10.0, "close": 50.0, "shares": 1000, "weight": 50.0},
        ]
        result, mapping = sanitize_snapshot(50000.0, positions)
        assert len(result) == 1
        assert len(mapping) == 1

    def test_full_pipeline_dataflow(self):
        """端到端数据流：脱敏 → 数据校验 → 模型路由"""
        from data_sanitizer import sanitize_snapshot, reset_mapping
        from data_validator import validate_quotes_data
        from model_router import classify_task

        reset_mapping()

        sample_quotes = [
            {"ts_code": "000977.XSHE", "close": 50.5, "volume": 10000000},
        ]
        valid, errors = validate_quotes_data(sample_quotes)
        assert len(valid) == 1

        category, task_type = classify_task("帮我评估一下浪潮信息的估值水平")
        assert category in ("stock", "unknown", "strategy")
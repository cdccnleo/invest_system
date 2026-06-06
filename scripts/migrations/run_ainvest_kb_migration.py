"""检查数据库状态并执行 AInvest KB 迁移"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from storage_factory import get_pg_connection

conn = get_pg_connection()
if conn is None:
    print("❌ 数据库不可用，跳过迁移")
    sys.exit(0)

try:
    cur = conn.cursor()
    
    # 检查 pgvector 扩展
    cur.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'")
    if cur.fetchone():
        print("✅ pgvector 扩展已安装")
    else:
        print("⚠️ pgvector 扩展未安装，向量嵌入将不可用")
    
    # 检查 ainvest_kb schema 是否已存在
    cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'ainvest_kb'")
    if cur.fetchone():
        print("📌 ainvest_kb schema 已存在，跳过迁移")
    else:
        # 执行迁移
        sql_path = "scripts/migrations/add_ainvest_kb.sql"
        with open(sql_path, "r", encoding="utf-8") as f:
            sql = f.read()
        cur.execute(sql)
        conn.commit()
        print("✅ ainvest_kb schema 迁移完成")
    
    # 验证表
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'ainvest_kb'
        ORDER BY table_name
    """)
    tables = cur.fetchall()
    print(f"📊 已创建表: {', '.join(t[0] for t in tables)}")
    
    cur.close()
except Exception as e:
    conn.rollback()
    print(f"❌ 迁移失败: {e}")
finally:
    conn.close()

print("检查完成")
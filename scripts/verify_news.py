import sys
sys.path.insert(0, '/home/aileo/invest_system/scripts')
from credentials import get_credential
import psycopg2

pwd = get_credential('DB_PASSWORD')
conn = psycopg2.connect(host='localhost', port=5432, database='investpilot', user='invest_admin', password=pwd)
cur = conn.cursor()
cur.execute("SELECT MAX(published_at), COUNT(*) FROM research.news_articles WHERE published_at >= '2026-05-28'")
row = cur.fetchone()
print(f'最新日期: {row[0]}, 2026-05-28起共: {row[1]} 条')
cur.execute("SELECT published_at::date, COUNT(*) FROM research.news_articles WHERE published_at >= '2026-05-28' GROUP BY published_at::date ORDER BY published_at::date DESC")
for r in cur.fetchall():
    print(f'  {r[0]}: {r[1]} 条')
conn.close()
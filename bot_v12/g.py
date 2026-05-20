
import sqlite3
conn = sqlite3.connect('data.db')
rows = conn.execute(
    "SELECT ts, level, message FROM logs ORDER BY id DESC LIMIT 30"
).fetchall()
for r in rows:
    print(r[0], r[1], r[2])

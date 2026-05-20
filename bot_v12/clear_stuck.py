import sqlite3
db = sqlite3.connect('data.db')
cur = db.execute("DELETE FROM uploads WHERE video_id='_mQfUEKfEFE'")
db.commit()
print(f"Cleared {cur.rowcount} row(s). Done!")
db.close()

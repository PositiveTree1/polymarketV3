import sqlite3
conn = sqlite3.connect("titan_state.db")
conn.execute("UPDATE signals SET live = 0")
conn.commit()
count = conn.execute("SELECT COUNT(*) FROM signals WHERE live=1").fetchone()[0]
print(f"Live signals remaining: {count}")
conn.close()

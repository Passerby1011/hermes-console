"""Why did the root state.db hash move? Read-only forensics."""
import sqlite3
from pathlib import Path

p = Path(r"D:/hermes-data/hermes/state.db")
conn = sqlite3.connect("file:" + str(p) + "?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
try:
    n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    print("rows =", n)
    print("\nnewest 6 by last_activity_at:")
    for r in conn.execute(
            "SELECT id, profile_name, source, message_count, last_activity_at, started_at "
            "FROM sessions ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT 6"):
        print(" ", dict(r))
    print("\nprofile distribution:")
    for r in conn.execute("SELECT profile_name, COUNT(*) c FROM sessions GROUP BY profile_name ORDER BY c DESC"):
        print(" ", r["profile_name"], r["c"])
finally:
    conn.close()

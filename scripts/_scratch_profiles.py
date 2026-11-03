# -*- coding: utf-8 -*-
"""本卡自用：看 state.db 的 profile 分布（判断扫描能否按 agent 过滤）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

HOME = Path(r"D:/hermes-data/hermes")
conn = sqlite3.connect("file:" + str(HOME / "state.db") + "?mode=ro", uri=True, timeout=10)
conn.row_factory = sqlite3.Row
print("columns:", [r[1] for r in conn.execute("PRAGMA table_info(sessions)")])
print()
print("profile_name 分布（message_count>=2, archived=0）:")
for row in conn.execute(
        "SELECT COALESCE(profile_name,'(null)') p, COUNT(*) n FROM sessions "
        "WHERE message_count>=2 AND archived=0 GROUP BY p ORDER BY n DESC LIMIT 20"):
    print("  %-20s %s" % (row["p"], row["n"]))
print()
print("最近 8 条（id 前缀 / profile_name / source / title）:")
for row in conn.execute(
        "SELECT id, profile_name, source, title, cwd, message_count FROM sessions "
        "WHERE message_count>=2 AND archived=0 ORDER BY COALESCE(last_activity_at,started_at) DESC LIMIT 8"):
    print("  %s | %-12s | %-10s | %s" % (row["id"][:24], row["profile_name"], row["source"],
                                         (row["title"] or "")[:40]))
conn.close()

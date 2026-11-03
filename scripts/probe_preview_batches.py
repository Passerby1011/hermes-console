# -*- coding: utf-8 -*-
"""只读探针 4：ai_preview_batches 现状 + 各 profile 库的 mtime/size（判断会话库读取成本）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
HOME = PLUGIN.parents[1]

c = sqlite3.connect("file:" + str(HOME / "auto-group" / "assignments.db").replace("\\", "/") + "?mode=ro", uri=True)
try:
    for t in ("ai_preview_batches", "ai_preview_items"):
        try:
            print(t, "rows =", c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0])
        except sqlite3.Error as exc:
            print(t, "ERR", exc)
    try:
        rows = c.execute("SELECT batch_id, created_at, status FROM ai_preview_batches "
                         "ORDER BY created_at DESC LIMIT 5").fetchall()
        print("latest:", rows)
    except sqlite3.Error as exc:
        print("cols ERR", exc)
        print([r[1] for r in c.execute("PRAGMA table_info(ai_preview_batches)")])
finally:
    c.close()

for name in ("state.db", "profiles/work/state.db", "profiles/xiaomo/state.db", "profiles/worker/state.db"):
    p = HOME / name
    print(name, "%.1f MB" % (p.stat().st_size / 1048576) if p.is_file() else "missing")

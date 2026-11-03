# -*- coding: utf-8 -*-
"""只读探针 2：口径细节 —— profile_name 归属分布、applied 命中、children、0 消息。

只读（mode=ro），输出到 scripts/_probe_out/profile-dbs/scope-details.txt
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
HOME = PLUGIN.parents[1]
OUT = HERE / "_probe_out" / "profile-dbs"
OUT.mkdir(parents=True, exist_ok=True)

EXCL = "('acp','kanban','oneshot','subagent','tool')"
VIS = ("COALESCE(message_count,0)>=1 AND LOWER(COALESCE(source,'')) NOT IN " + EXCL +
       " AND COALESCE(hidden,0)=0 AND NOT (COALESCE(archived,0)=1 AND COALESCE(pinned,0)=0)")

lines = []


def say(t=""):
    print(t, flush=True)
    lines.append(t)


def ro(p):
    c = sqlite3.connect("file:" + str(p).replace("\\", "/") + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def profile_dbs(home: Path):
    yield "default", home / "state.db"
    root = home / "profiles"
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if not (child / "config.yaml").is_file():
            continue
        yield child.name, child / "state.db"


store = HOME / "auto-group" / "assignments.db"
applied = set()
rejected = set()
c = ro(store)
try:
    applied = {r[0] for r in c.execute("SELECT session_id FROM assignments WHERE status='applied'")}
    rejected = {r[0] for r in c.execute("SELECT session_id FROM rejected")}
finally:
    c.close()
say("applied=%d rejected=%d" % (len(applied), len(rejected)))

total_all = 0
detail = {}
for name, path in profile_dbs(HOME):
    if not path.is_file():
        say("%-14s MISSING %s" % (name, path))
        continue
    c = ro(path)
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(sessions)")}
        has_parent = "parent_session_id" in cols
        rows = [dict(r) for r in c.execute("SELECT * FROM sessions")]
    finally:
        c.close()
    vis = [r for r in rows if (
        int(r.get("message_count") or 0) >= 1
        and str(r.get("source") or "").lower() not in ("acp", "kanban", "oneshot", "subagent", "tool")
        and not int(r.get("hidden") or 0)
        and not (int(r.get("archived") or 0) == 1 and not int(r.get("pinned") or 0)))]
    by_tag = {}
    for r in vis:
        by_tag[str(r.get("profile_name") or "")] = by_tag.get(str(r.get("profile_name") or ""), 0) + 1
    vis_ids = {str(r["id"]) for r in vis}
    cand = [r for r in vis if str(r["id"]) not in applied and str(r["id"]) not in rejected]
    kids = [r for r in vis if has_parent and r.get("parent_session_id")]
    zero = [r for r in rows if int(r.get("message_count") or 0) < 1]
    detail[name] = {
        "path": str(path), "rows": len(rows), "visible": len(vis),
        "visible_by_profile_name": by_tag,
        "applied_in_visible": len(vis_ids & applied),
        "rejected_in_visible": len(vis_ids & rejected),
        "candidates": len(cand),
        "candidate_ids": sorted(str(r["id"]) for r in cand),
        "children_in_visible": len(kids),
        "zero_message_rows": len(zero),
        "has_parent_col": has_parent,
        "applied_total_in_db": len({str(r["id"]) for r in rows} & applied),
        "archived_total": sum(1 for r in rows if int(r.get("archived") or 0) == 1),
    }
    total_all += len(vis)
    say("%-14s rows=%-4d visible=%-4d applied_in_visible=%-3d candidates=%-3d children=%-3d "
        "zero_msg=%-4d tagged=%s"
        % (name, len(rows), len(vis), len(vis_ids & applied), len(cand), len(kids), len(zero), by_tag))

say("\n合计 desktop-visible（各库并集前）=%d" % total_all)
say("并集（按 id 去重）后 candidates 合计 = %d" % len({i for d in detail.values() for i in d["candidate_ids"]}))

(OUT / "scope-details.json").write_text(
    json.dumps({"applied": len(applied), "rejected": len(rejected), "detail": detail},
               ensure_ascii=False, indent=2), encoding="utf-8")
(OUT / "scope-details.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
say("\nOUT: " + str(OUT / "scope-details.txt"))

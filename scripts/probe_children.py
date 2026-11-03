# -*- coding: utf-8 -*-
"""只读探针 3：桌面可见池里的 child 行到底是什么（branch / reset / compression / subagent）。

用于决定「后端 desktop_visible_pool 要不要复刻宿主的 listable-child 规则」。
只读（mode=ro）。
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
    for child in sorted((home / "profiles").iterdir()):
        if child.is_dir() and (child / "config.yaml").is_file():
            yield child.name, child / "state.db"


for name, path in profile_dbs(HOME):
    if not path.is_file():
        continue
    c = ro(path)
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(sessions)")]
        rows = [dict(r) for r in c.execute("SELECT * FROM sessions")]
    finally:
        c.close()
    say("== %s  (%d rows)" % (name, len(rows)))
    say("   cols: " + repr(cols))
    by_id = {str(r["id"]): r for r in rows}
    vis = [r for r in rows if (
        int(r.get("message_count") or 0) >= 1
        and str(r.get("source") or "").lower() not in ("acp", "kanban", "oneshot", "subagent", "tool")
        and not int(r.get("hidden") or 0)
        and not (int(r.get("archived") or 0) == 1 and not int(r.get("pinned") or 0)))]
    kids = [r for r in vis if r.get("parent_session_id")]
    say("   visible=%d children_in_visible=%d" % (len(vis), len(kids)))
    for r in kids:
        parent = by_id.get(str(r.get("parent_session_id"))) or {}
        mc = r.get("model_config")
        markers = {}
        if isinstance(mc, str) and mc.strip().startswith("{"):
            try:
                data = json.loads(mc)
                markers = {k: data.get(k) for k in ("_branched_from", "_delegate_from", "_reset_from")
                           if k in data}
            except ValueError:
                markers = {"__unparsable__": mc[:60]}
        say("     child=%s source=%s parent=%s parent_end_reason=%s same_key=%s markers=%s"
            % (str(r["id"])[:26], r.get("source"), str(r.get("parent_session_id"))[:22],
               parent.get("end_reason"), bool(r.get("session_key")) and r.get("session_key") == parent.get("session_key"),
               markers))
    # 0 消息但其它条件都过的行数（前端会排除）
    say("   zero_message(in raw)=%d" % sum(1 for r in rows if int(r.get("message_count") or 0) < 1))

(OUT / "children.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
say("\nOUT: " + str(OUT / "children.txt"))

# -*- coding: utf-8 -*-
"""只读探针：核对根库 vs profiles/*/state.db 的行/归属/可见性口径。

**只读**：全部 sqlite 以 mode=ro 打开，不写任何文件（除本目录下的 txt 输出）。
跑法：D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe scripts/probe_profile_dbs.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
REAL_HOME = PLUGIN.parents[1]
OUT = HERE / "_probe_out" / "profile-dbs"
OUT.mkdir(parents=True, exist_ok=True)

lines: list[str] = []


def say(text: str = "") -> None:
    print(text, flush=True)
    lines.append(text)


def ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect("file:" + str(path).replace("\\", "/") + "?mode=ro", uri=True)


def db_report(path: Path, label: str) -> dict:
    info = {"label": label, "path": str(path)}
    if not path.is_file():
        info["missing"] = True
        return info
    conn = ro(path)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        info["cols"] = sorted(cols)
        info["rows"] = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        if "profile_name" in cols:
            info["profile_name"] = {str(r[0]): r[1] for r in conn.execute(
                "SELECT COALESCE(profile_name,''), COUNT(*) FROM sessions GROUP BY 1 ORDER BY 2 DESC")}
        vis = {}
        q = ("SELECT COUNT(*) FROM sessions WHERE COALESCE(message_count,0)>=1 "
             "AND LOWER(COALESCE(source,'')) NOT IN ('acp','kanban','oneshot','subagent','tool')")
        vis["base"] = conn.execute(q).fetchone()[0]
        for extra, sql in (
            ("+hidden=0", " AND COALESCE(hidden,0)=0"),
            ("+archived=0", " AND COALESCE(archived,0)=0"),
            ("+archived=0 or pinned=1", " AND (COALESCE(archived,0)=0 OR COALESCE(pinned,0)=1)"),
            ("+parent null", " AND COALESCE(parent_session_id,'')=''"),
        ):
            try:
                vis[extra] = conn.execute(q + sql).fetchone()[0]
            except sqlite3.Error as exc:
                vis[extra] = "ERR " + str(exc)
        info["visible"] = vis
        if "archived" in cols:
            info["archived_rows"] = conn.execute("SELECT COUNT(*) FROM sessions WHERE COALESCE(archived,0)=1").fetchone()[0]
        if "pinned" in cols:
            info["pinned_rows"] = conn.execute("SELECT COUNT(*) FROM sessions WHERE COALESCE(pinned,0)=1").fetchone()[0]
        if "hidden" in cols:
            info["hidden_rows"] = conn.execute("SELECT COUNT(*) FROM sessions WHERE COALESCE(hidden,0)=1").fetchone()[0]
        info["sources"] = {str(r[0]): r[1] for r in conn.execute(
            "SELECT COALESCE(source,''), COUNT(*) FROM sessions GROUP BY 1 ORDER BY 2 DESC")}
        info["ids_sample"] = [r[0] for r in conn.execute("SELECT id FROM sessions LIMIT 3")]
    finally:
        conn.close()
    return info


def main() -> int:
    say("HOME: " + str(REAL_HOME))
    reports = []
    root = REAL_HOME / "state.db"
    reports.append(db_report(root, "default(root)"))
    root_ids = set()
    conn = ro(root)
    try:
        root_ids = {r[0] for r in conn.execute("SELECT id FROM sessions")}
    finally:
        conn.close()
    profiles_dir = REAL_HOME / "profiles"
    prof_reports = []
    for child in sorted(profiles_dir.iterdir()):
        if not child.is_dir():
            continue
        rep = db_report(child / "state.db", child.name)
        if rep.get("rows"):
            ids = set()
            c = ro(child / "state.db")
            try:
                ids = {r[0] for r in c.execute("SELECT id FROM sessions")}
            finally:
                c.close()
            rep["overlap_root"] = len(ids & root_ids)
            rep["ids"] = sorted(ids)
        prof_reports.append(rep)
    reports.extend(prof_reports)
    say(json.dumps(reports, ensure_ascii=False, indent=2))

    # assignments
    ag = REAL_HOME / "auto-group" / "assignments.db"
    say("\nassignments.db exists: %s" % ag.is_file())
    if ag.is_file():
        c = ro(ag)
        c.row_factory = sqlite3.Row
        try:
            tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            say("tables: " + repr(tabs))
            for t in ("assignments", "suggestions", "rejected"):
                if t in tabs:
                    cols = [r[1] for r in c.execute("PRAGMA table_info(%s)" % t)]
                    say(t + " cols: " + repr(cols))
                    try:
                        say(t + " status: " + repr({str(r[0]): r[1] for r in c.execute(
                            "SELECT status, COUNT(*) FROM %s GROUP BY 1" % t)}))
                    except sqlite3.Error as exc:
                        say(t + " status ERR: " + str(exc))
        finally:
            c.close()
    (OUT / "db-report.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "db-report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    say("\nOUT: " + str(OUT / "db-report.txt"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

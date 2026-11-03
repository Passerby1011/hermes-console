"""t_bba9216d: compare front-end isDesktopVisible rule vs backend desktop_visible_pool rule per profile.

Answers: (a) why button said 21 while backend candidates says 26, (b) which rows differ and why,
(c) does the front-end per-profile count now equal candidate_scope total_available.
"""
import json
import sqlite3
import sys
from pathlib import Path

CONSOLE = Path(r"D:/hermes-data/hermes/plugins/hermes-console")
HOME = Path(r"D:/hermes-data/hermes")
sys.path.insert(0, str(CONSOLE))
sys.path.insert(0, str(CONSOLE / "dashboard"))

import autogroup_ai as ai  # noqa: E402

EXCLUDED = ai.DESKTOP_EXCLUDED_SOURCES


def rows_of(path):
    path = Path(path)
    if not path.is_file():
        return [], set()
    conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        return [dict(r) for r in conn.execute("SELECT * FROM sessions")], cols
    finally:
        conn.close()


def frontend_visible(row, cols):
    """Exact mirror of src/sections/autogroup.js isDesktopVisible()."""
    source = str(row.get("source") or "").lower()
    if source in EXCLUDED:
        return False
    if "hidden" in cols and row.get("hidden") is not None and int(row.get("hidden")) == 1:
        return False
    if ("archived" in cols and row.get("archived") is not None and int(row.get("archived")) == 1
            and not ("pinned" in cols and row.get("pinned") is not None and int(row.get("pinned")) == 1)):
        return False
    count = row.get("message_count")
    if count is None:
        return True
    return int(count) >= 1


def main():
    targets = [("default", HOME / "state.db"), ("work", HOME / "profiles/work/state.db"),
               ("xiaomo", HOME / "profiles/xiaomo/state.db"), ("worker", HOME / "profiles/worker/state.db")]
    assignments, applied = ai._load_assignments(HOME)
    report = {"profiles": [], "applied_total": len(applied), "assignments_total": len(assignments)}
    print(f"assignments={len(assignments)} applied={len(applied)}")
    print("statuses:", {k: sum(1 for a in assignments.values() if a.get('status') == k)
                        for k in {a.get('status') for a in assignments.values()}})
    total_fe = 0
    for name, path in targets:
        rows, cols = rows_of(path)
        fe = [r for r in rows if frontend_visible(r, cols)]
        fe_ungrouped = [r for r in fe if str(r.get("id")) not in applied]
        scope = ai.candidate_scope(HOME, agents={name})
        allscope = ai.candidate_scope(HOME, agents=None)
        be_ids = set(scope["candidate_session_ids"])
        feids = {str(r["id"]) for r in fe_ungrouped}
        only_fe = sorted(feids - be_ids)
        only_be = sorted(be_ids - feids)
        entry = {"profile": name, "rows": len(rows), "frontend_visible": len(fe),
                 "frontend_ungrouped": len(fe_ungrouped), "backend_candidates": len(be_ids),
                 "only_frontend": len(only_fe), "only_backend": len(only_be),
                 "only_frontend_sample": [], "backend_pool": scope["pool"],
                 "backend_applied_in_pool": scope["applied_in_pool"],
                 "backend_excluded_archived": scope["excluded_archived"],
                 "backend_excluded_child": scope["excluded_child"],
                 "backend_excluded_no_message": scope["excluded_no_message"]}
        by_id = {str(r["id"]): r for r in rows if r.get("id") is not None}
        for sid in only_fe[:12]:
            r = by_id[sid]
            entry["only_frontend_sample"].append({
                "id": sid, "title": (r.get("title") or "")[:40], "source": r.get("source"),
                "message_count": r.get("message_count"), "hidden": r.get("hidden"),
                "archived": r.get("archived"), "pinned": r.get("pinned"),
                "parent": r.get("parent_session_id"), "session_key": r.get("session_key"),
                "end_reason": r.get("end_reason"),
                "listable": ai._is_listable_child(r, by_id, cols),
                "applied": sid in applied,
            })
        total_fe += len(fe_ungrouped)
        report["profiles"].append(entry)
        print(f"\n[{name}] rows={len(rows)} fe_visible={len(fe)} fe_ungrouped={len(fe_ungrouped)} "
              f"be_candidates={len(be_ids)} only_fe={len(only_fe)} only_be={len(only_be)} "
              f"pool={scope['pool']} applied_in_pool={scope['applied_in_pool']}")
        for s in entry["only_frontend_sample"]:
            print("   only_fe:", json.dumps(s, ensure_ascii=False))
        for sid in only_be[:5]:
            print("   only_be:", sid)
    report["sum_frontend_ungrouped"] = total_fe
    Path("probe_fe_vs_be.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsum of frontend ungrouped across profiles = {total_fe}")


if __name__ == "__main__":
    main()

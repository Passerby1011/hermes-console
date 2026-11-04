"""t_bba9216d acceptance #2: does bounded pagination recover rows the 100-row page missed?

Replicates src/sections/autogroup.js fetchSessionsAllPages() exactly (limit=100, offset stepping,
stop on short page / total reached / cap) against the REAL host route, then checks whether the
worker candidates (which live outside the newest 100 rows) are now present.

Also replays the OLD single-page call to show the gap it left.
"""
import json
import sys
from pathlib import Path

HOME = Path(r"D:/hermes-data/hermes")
PLUGIN = Path(r"D:/hermes-data/hermes/plugins/hermes-console")
sys.path.insert(0, r"D:/hermes-data/hermes/hermes-agent")
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN / "dashboard"))

import autogroup_ai as ai  # noqa: E402
from hermes_cli.web_server_sessions import _open_session_db_for_profile  # noqa: E402
from hermes_cli.session_listing import subagent_listing_scope  # noqa: E402

EXCLUDED = ["acp", "kanban", "oneshot", "subagent", "tool"]
SESSIONS_LIMIT = 100
SESSIONS_SCAN_MAX = 2000


def fe_visible(row):
    if str(row.get("source") or "").lower() in EXCLUDED:
        return False
    if row.get("hidden") is not None and int(row["hidden"]) == 1:
        return False
    if row.get("archived") is not None and int(row["archived"]) == 1 and not int(row.get("pinned") or 0):
        return False
    c = row.get("message_count")
    return True if c is None else int(c) >= 1


def page_call(profile, limit, offset):
    """One page exactly like the host route serves it."""
    name = None if profile == "default" else profile
    db = _open_session_db_for_profile(name, read_only=True)
    try:
        include_sub, exclude_list = subagent_listing_scope(Path(db.db_path).parent)
        rows = db.list_sessions_rich(
            limit=limit, offset=offset, order_by_last_active=True, compact_rows=True,
            include_pinned=True, min_message_count=0, include_archived=False,
            archived_only=False, include_subagents=include_sub,
            exclude_sources=exclude_list or None)
        total = db.session_count(exclude_children=True, include_archived=False,
                                 archived_only=False, include_subagents=include_sub,
                                 exclude_sources=exclude_list or None)
        return [dict(r) for r in rows], total
    finally:
        db.close()


def fetch_all_pages(profile):
    """Mirror of fetchSessionsAllPages() — truncated = really didn't finish, not rows<total."""
    rows, total, failed = [], None, False
    offset = 0
    while True:
        got, seen_total = page_call(profile, SESSIONS_LIMIT, offset)
        if seen_total is not None:
            total = seen_total
        rows.extend(got)
        if not got:
            break
        if len(got) < SESSIONS_LIMIT:
            break
        if total is not None and len(rows) >= total:
            break
        if len(rows) >= SESSIONS_SCAN_MAX:
            break
        offset += len(got)
    truncated = failed or len(rows) >= SESSIONS_SCAN_MAX
    return rows, total, truncated


def main():
    _, applied = ai._load_assignments(HOME)
    out = {}
    for agent in ("worker", "default", "work", "xiaomo"):
        scope = ai.candidate_scope(HOME, agents={agent})
        want = set(scope["candidate_session_ids"])
        # OLD behaviour: one page of 100
        old_rows, old_total = page_call(agent, SESSIONS_LIMIT, 0)
        old_ung = {str(r["id"]) for r in old_rows if fe_visible(r)} - applied
        # NEW behaviour: bounded pagination
        new_rows, new_total, truncated = fetch_all_pages(agent)
        new_ung = {str(r["id"]) for r in new_rows if fe_visible(r)} - applied
        hit = want & new_ung
        out[agent] = {
            "backend_candidates": len(want),
            "old_single_page_rows": len(old_rows),
            "old_ungrouped_found": len(old_ung),
            "old_missing": len(want - old_ung),
            "new_rows": len(new_rows),
            "new_total": new_total,
            "new_truncated": truncated,
            "new_ungrouped_found": len(new_ung),
            "new_matched_candidates": len(hit),
            "equal_to_backend": new_ung == want,
        }
        print(f"[{agent}] backend={len(want)} | OLD(1 page) found={len(old_ung)} missing={len(want - old_ung)}"
              f" | NEW(paged) rows={len(new_rows)}/{new_total} found={len(new_ung)}"
              f" equal={new_ung == want} truncated={truncated}")
        if want - old_ung:
            print(f"    rows invisible with the old single page: {sorted(want - old_ung)}")
    Path("probe_pagination_fix.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

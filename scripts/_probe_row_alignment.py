"""t_bba9216d acceptance C: exact row-level alignment between host session list and backend pool.

Replicates the host route pipeline (``get_sessions``) with a large limit so the 100-row page cap
does not blur the comparison, then diffs against ``desktop_visible_pool`` per agent.
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


def fe_visible(row):
    """Mirror of src/sections/autogroup.js isDesktopVisible()."""
    if str(row.get("source") or "").lower() in EXCLUDED:
        return False
    if row.get("hidden") is not None and int(row["hidden"]) == 1:
        return False
    if row.get("archived") is not None and int(row["archived"]) == 1 and not int(row.get("pinned") or 0):
        return False
    c = row.get("message_count")
    return True if c is None else int(c) >= 1


def host_rows(profile):
    name = None if profile == "default" else profile
    db = _open_session_db_for_profile(name, read_only=True)
    try:
        include_sub, exclude_list = subagent_listing_scope(Path(db.db_path).parent)
        rows = db.list_sessions_rich(
            limit=100000, offset=0, order_by_last_active=True, compact_rows=True,
            include_pinned=True, min_message_count=0, include_archived=False,
            archived_only=False, include_subagents=include_sub,
            exclude_sources=exclude_list or None)
        return [dict(r) for r in rows]
    finally:
        db.close()


def main():
    out = {}
    for agent in ("default", "work", "xiaomo", "worker"):
        rows = host_rows(agent)
        vis = [r for r in rows if fe_visible(r)]
        # backend pool for the same agent, keyed by id
        pool = ai.desktop_visible_pool(HOME, agents={agent})
        pids = {str(r["id"]) for r in pool["sessions"]}
        fids = {str(r["id"]) for r in vis}
        only_fe = sorted(fids - pids)
        only_be = sorted(pids - fids)
        by_id = {str(r["id"]): r for r in rows}
        out[agent] = {
            "host_rows_total": len(rows),
            "host_visible": len(vis),
            "backend_pool": len(pids),
            "only_fe": len(only_fe),
            "only_be": len(only_be),
            "only_fe_detail": [
                {k: by_id[s].get(k) for k in
                 ("id", "title", "source", "message_count", "hidden", "archived", "pinned",
                  "parent_session_id", "session_key", "end_reason")}
                for s in only_fe
            ],
            "only_be_detail": [s for s in only_be[:10]],
            "backend_counts": pool["counts"],
        }
        print(f"[{agent}] host_rows={len(rows)} host_visible={len(vis)} backend_pool={len(pids)} "
              f"only_fe={len(only_fe)} only_be={len(only_be)}")
        for d in out[agent]["only_fe_detail"]:
            print("   only_fe:", json.dumps(d, ensure_ascii=False)[:220])
        for s in out[agent]["only_be_detail"]:
            print("   only_be:", s)
        print("   counts:", json.dumps(pool["counts"], ensure_ascii=False))

    Path("probe_row_alignment.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nwrote probe_row_alignment.json")


if __name__ == "__main__":
    main()

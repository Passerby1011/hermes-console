"""Why does list_sessions_rich yield one fewer row than session_count(total)? Read-only."""
import sys
from pathlib import Path

sys.path.insert(0, r"D:/hermes-data/hermes/hermes-agent")
from hermes_cli.web_server_sessions import _open_session_db_for_profile  # noqa: E402
from hermes_cli.session_listing import subagent_listing_scope  # noqa: E402
from hermes_state_local import exclude_superseded_segments  # noqa: E402

for profile in ("worker", "xiaomo", "default", "work"):
    name = None if profile == "default" else profile
    db = _open_session_db_for_profile(name, read_only=True)
    try:
        inc, exc = subagent_listing_scope(Path(db.db_path).parent)
        kw = dict(include_archived=False, archived_only=False, include_subagents=inc,
                  exclude_sources=exc or None)
        total = db.session_count(exclude_children=True, **kw)
        rows = db.list_sessions_rich(limit=100000, offset=0, order_by_last_active=True,
                                    compact_rows=True, include_pinned=True,
                                    min_message_count=0, **kw)
        ids = {str(r["id"]) for r in rows}
        # find rows counted by total but absent from the list
        allrows = db.list_sessions_rich(limit=100000, offset=0, order_by_last_active=True,
                                       compact_rows=True, include_pinned=True,
                                       min_message_count=0, include_children=True,
                                       include_archived=None if False else True,
                                       archived_only=False, include_subagents=True,
                                       include_hidden=True)
        all_ids = {str(r["id"]) for r in allrows}
        print(f"[{profile}] total={total} listed={len(rows)} all_rows={len(allrows)} "
              f"missing_from_list={len(ids ^ all_ids)}")
        for extra in sorted(all_ids - ids)[:6]:
            r = next(x for x in allrows if str(x["id"]) == extra)
            print("   not listed:", extra, "| source=", r.get("source"),
                  "| archived=", r.get("archived"), "| hidden=", r.get("hidden"),
                  "| parent=", r.get("parent_session_id"), "| end=", r.get("end_reason"))
    finally:
        db.close()

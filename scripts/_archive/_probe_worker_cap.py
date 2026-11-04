"""t_bba9216d: is the worker case a real rule divergence, or just the 100-row page cap?"""
import sys
import threading
from pathlib import Path

HOME = Path(r"D:/hermes-data/hermes")
PLUGIN = Path(r"D:/hermes-data/hermes/plugins/hermes-console")
sys.path.insert(0, r"D:/hermes-data/hermes/hermes-agent")
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN / "dashboard"))

import autogroup_ai as ai  # noqa: E402
from hermes_cli.web_routers import sessions as host_routes  # noqa: E402

EXCLUDED = ["acp", "kanban", "oneshot", "subagent", "tool"]


def fe_visible(row):
    if str(row.get("source") or "").lower() in EXCLUDED:
        return False
    if row.get("hidden") is not None and int(row["hidden"]) == 1:
        return False
    if row.get("archived") is not None and int(row["archived"]) == 1 and not int(row.get("pinned") or 0):
        return False
    c = row.get("message_count")
    return True if c is None else int(c) >= 1


def route(profile, **kw):
    box = {}

    def run():
        try:
            box["res"] = host_routes.get_sessions(limit=kw.pop("limit", 100), offset=0,
                                                 order="recent",
                                                 profile=None if profile == "default" else profile, **kw)
        except Exception as exc:  # noqa: BLE001
            box["err"] = "%s: %s" % (type(exc).__name__, exc)

    t = threading.Thread(target=run)
    t.start()
    t.join(180)
    return box


for agent in ("worker", "xiaomo", "work"):
    for kw in ({}, {"min_messages": 1}):
        box = route(agent, **dict(kw))
        if "err" in box:
            print(agent, kw, "ERR", box["err"])
            continue
        res = box["res"]
        rows = res.get("sessions") or []
        vis = [r for r in rows if fe_visible(r)]
        _, applied = ai._load_assignments(HOME)
        ung = [r for r in vis if str(r.get("id")) not in applied]
        scope = ai.candidate_scope(HOME, agents={agent})
        print(f"[{agent}] {kw} total={res.get('total')} rows={len(rows)} visible={len(vis)} "
              f"ungrouped={len(ung)} backend={scope['total_available']} "
              f"matches={set(str(r['id']) for r in ung) == set(scope['candidate_session_ids'])}")

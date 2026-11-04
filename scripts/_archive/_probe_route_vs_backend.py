"""t_bba9216d acceptance probe #1: host route list vs backend candidate_scope, per agent.

Calls the REAL host route function (hermes_cli.web_routers.sessions.get_sessions) the same way
dashboard/api_overview.py does (profile=..., limit=100, offset=0, order='recent'), then compares
"what the UI's session list would show, minus applied" against candidate_scope(total_available).
"""
import json
import sys
import threading
from pathlib import Path

HOME = Path(r"D:/hermes-data/hermes")
PLUGIN = Path(r"D:/hermes-data/hermes/plugins/hermes-console")
AGENT_SRC = Path(r"D:/hermes-data/hermes/hermes-agent")

sys.path.insert(0, str(AGENT_SRC))
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN / "dashboard"))

import autogroup_ai as ai  # noqa: E402
from hermes_cli.web_routers import sessions as host_routes  # noqa: E402

EXCLUDED = ["acp", "kanban", "oneshot", "subagent", "tool"]


def frontend_visible(row):
    """Mirror of src/sections/autogroup.js isDesktopVisible()."""
    if str(row.get("source") or "").lower() in EXCLUDED:
        return False
    if row.get("hidden") is not None and int(row["hidden"]) == 1:
        return False
    if row.get("archived") is not None and int(row["archived"]) == 1 and not int(row.get("pinned") or 0):
        return False
    count = row.get("message_count")
    if count is None:
        return True
    return int(count) >= 1


def call_route(profile):
    box = {}

    def run():
        try:
            box["res"] = host_routes.get_sessions(limit=100, offset=0, order="recent",
                                                  profile=None if profile == "default" else profile)
        except Exception as exc:  # noqa: BLE001
            box["err"] = "%s: %s" % (type(exc).__name__, exc)

    t = threading.Thread(target=run)
    t.start()
    t.join(120)
    return box


def main():
    _, applied = ai._load_assignments(HOME)
    report = {"applied": len(applied), "agents": []}
    for agent in ("default", "work", "xiaomo", "worker"):
        box = call_route(agent)
        if "err" in box or "res" not in box:
            print(f"[{agent}] ROUTE FAILED: {box.get('err')}")
            report["agents"].append({"agent": agent, "route_error": box.get("err")})
            continue
        res = box["res"]
        rows = res.get("sessions") or []
        fe = [r for r in rows if frontend_visible(r)]
        fe_ungrouped = [r for r in fe if str(r.get("id")) not in applied]
        scope = ai.candidate_scope(HOME, agents={agent})
        be_ids = [s for s in scope["candidate_session_ids"] if s]
        ui_ids = [str(r.get("id")) for r in fe_ungrouped]
        only_ui = sorted(set(ui_ids) - set(be_ids))
        only_be = sorted(set(be_ids) - set(ui_ids))
        entry = {
            "agent": agent,
            "route_total": res.get("total"),
            "route_rows": len(rows),
            "ui_visible": len(fe),
            "ui_ungrouped": len(fe_ungrouped),
            "backend_total_available": scope["total_available"],
            "backend_pool": scope["pool"],
            "backend_applied_in_pool": scope["applied_in_pool"],
            "equal": len(fe_ungrouped) == scope["total_available"] and not only_ui and not only_be,
            "only_ui": only_ui,
            "only_be": only_be,
            "route_truncated_at_100": (res.get("total") or 0) > 100,
        }
        report["agents"].append(entry)
        print(f"\n[{agent}] route_total={entry['route_total']} rows={entry['route_rows']} "
              f"ui_visible={entry['ui_visible']} ui_ungrouped={entry['ui_ungrouped']} "
              f"backend={entry['backend_total_available']} pool={entry['backend_pool']} "
              f"applied_in_pool={entry['backend_applied_in_pool']}")
        print(f"  equal={entry['equal']} only_ui={only_ui[:5]} only_be={only_be[:5]}")

    Path("probe_route_vs_backend.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nwrote probe_route_vs_backend.json")


if __name__ == "__main__":
    main()

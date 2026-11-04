"""t_bba9216d real-data probe: candidate_scope per agent + profile DB verification."""
import json
import sqlite3
import sys
from pathlib import Path

CONSOLE = Path(r"D:/hermes-data/hermes/plugins/hermes-console")
HOME = Path(r"D:/hermes-data/hermes")
sys.path.insert(0, str(CONSOLE))
sys.path.insert(0, str(CONSOLE / "dashboard"))

import autogroup_ai as ai  # noqa: E402

AGENTS = ["__all__", "work", "xiaomo", "worker", "default"]


def db_ids(path):
    p = Path(path)
    if not p.is_file():
        return set()
    conn = sqlite3.connect("file:" + str(p) + "?mode=ro", uri=True)
    try:
        return {str(r[0]) for r in conn.execute("SELECT id FROM sessions")}
    finally:
        conn.close()


def main():
    out = {"home": str(HOME), "scope_rule": ai.SCOPE_RULE, "cases": []}
    roots = {
        "default": HOME / "state.db",
        "work": HOME / "profiles/work/state.db",
        "xiaomo": HOME / "profiles/xiaomo/state.db",
        "worker": HOME / "profiles/worker/state.db",
    }
    all_ids = {}
    for name, path in roots.items():
        all_ids[name] = db_ids(path)
        print(f"db {name}: {path} rows={len(all_ids[name])}")

    for agent in AGENTS:
        agents = None if agent == "__all__" else {agent}
        scope = ai.candidate_scope(HOME, agents=agents)
        ids = [s for s in scope.get("candidate_session_ids") or [] if s]
        # which db does each candidate id live in?
        where = {}
        for sid in ids:
            where[sid] = sorted(n for n, s in all_ids.items() if sid in s)
        stray = {sid: w for sid, w in where.items() if not w}
        case = {
            "agent": agent,
            "ok": scope.get("ok"),
            "total_available": scope.get("total_available"),
            "candidates": len(ids),
            "pool": scope.get("pool"),
            "applied_in_pool": scope.get("applied_in_pool"),
            "excluded_applied": scope.get("excluded_applied"),
            "excluded_kanban": scope.get("excluded_kanban"),
            "excluded_hidden": scope.get("excluded_hidden"),
            "excluded_archived": scope.get("excluded_archived"),
            "excluded_no_message": scope.get("excluded_no_message"),
            "excluded_child": scope.get("excluded_child"),
            "excluded_agent": scope.get("excluded_agent"),
            "profiles_scanned": scope.get("profiles_scanned"),
            "candidate_profiles": scope.get("candidate_profiles"),
            "not_in_any_db": stray,
            "in_ids": ids,
            "where": where,
        }
        out["cases"].append(case)
        print(f"\nagent={agent} total_available={case['total_available']} "
              f"pool={case['pool']} applied_in_pool={case['applied_in_pool']} "
              f"excluded_applied={case['excluded_applied']} excluded_agent={case['excluded_agent']}")
        prof_set = sorted({p for w in where.values() for p in w})
        print(f"  candidates={len(ids)} live-in={prof_set} not_in_any_db={len(stray)}")

    # isolation checks
    print("\n=== isolation ===")
    by_agent = {c["agent"]: set(c["in_ids"]) for c in out["cases"]}
    for a in ("work", "xiaomo", "worker", "default"):
        others = set()
        for b in ("default", "work", "xiaomo", "worker"):
            if b != a:
                others |= all_ids[b] - all_ids[a]
        leak = by_agent[a] & others
        print(f"agent={a}: leak={len(leak)} {sorted(leak)[:5]}")
        out.setdefault("isolation", {})[a] = sorted(leak)
    union = set().union(*[by_agent[a] for a in ("work", "xiaomo", "worker", "default")])
    print(f"union of per-agent candidates = {len(union)}; __all__ = {len(by_agent['__all__'])}")
    out["union_size"] = len(union)
    out["all_size"] = len(by_agent["__all__"])

    Path("probe_real_scope.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nwrote probe_real_scope.json")


if __name__ == "__main__":
    main()

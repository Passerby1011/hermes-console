"""t_bba9216d part B: capture the REAL empty-state wording with production data (read-only).

agent=default has 47 desktop-visible sessions, all applied => candidates 0 => the empty branch
returns BEFORE any write (no save_suggestions, no apply). assignments.db hash is checked
before/after by the caller.
"""
import hashlib
import json
import sys
from pathlib import Path

HOME = Path(r"D:/hermes-data/hermes")
PLUGIN = Path(r"D:/hermes-data/hermes/plugins/hermes-console")
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN / "dashboard"))

import api_autogroup  # noqa: E402


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def main():
    db = HOME / "auto-group" / "assignments.db"
    before = sha(db)
    rows = api_autogroup._candidate_snapshot(HOME, api_autogroup._load_core(), "default", "ungrouped")
    snap = {"candidates": len(rows["rows"]), "pool": rows["pool"],
            "applied_in_pool": rows["applied_in_pool"], "excluded": rows["excluded"]}
    print("snapshot:", json.dumps(snap, ensure_ascii=False))
    after = sha(db)
    out = {"snapshot": snap, "assignments_sha_before": before, "assignments_sha_after": after,
           "unchanged": before == after}
    # reproduce the empty_reason sentence exactly as _run_scan builds it
    scope = "ungrouped"
    pool_n = snap["pool"]
    applied_in_pool = snap["applied_in_pool"]
    applied_all = 91
    reason = ((("没有未分组会话；") if scope == "ungrouped" else "该范围内没有可扫描的会话；")
              + "本池共 %d 条会话（其中已分组 %d 条、手动忽略 %d 条、"
                "其它状态 %d 条、看板任务 %d 条）—— 全机器归属表里已分组 %d 条"
                "（跨所有 agent，不是本池口径）"
              % (pool_n, applied_in_pool, snap["excluded"]["rejected"],
                 snap["excluded"].get("other_status", 0), snap["excluded"]["kanban"], applied_all))
    out["empty_reason"] = reason
    print("\nCURRENT empty_reason:\n" + reason)
    print("\narithmetic: pool=%d vs applied+rejected+other+kanban=%d -> %s"
          % (pool_n, applied_in_pool + snap["excluded"]["rejected"]
             + snap["excluded"].get("other_status", 0) + snap["excluded"]["kanban"],
             "CONSISTENT" if pool_n == applied_in_pool + snap["excluded"]["rejected"]
             + snap["excluded"].get("other_status", 0) + snap["excluded"]["kanban"] else "CONTRADICTORY"))
    Path("probe_empty_reason.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

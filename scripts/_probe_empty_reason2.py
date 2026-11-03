"""t_bba9216d part B (after fix): every number in the empty-state sentence must add up."""
import hashlib
import json
import sys
from pathlib import Path

HOME = Path(r"D:/hermes-data/hermes")
PLUGIN = Path(r"D:/hermes-data/hermes/plugins/hermes-console")
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN / "dashboard"))

import api_autogroup as api  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (("  — " + str(detail)) if detail else ""))
    if not ok:
        FAILS.append(name)


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def main():
    db = HOME / "auto-group" / "assignments.db"
    before = sha(db)
    core = api._load_core()
    out = {}

    # ---- real data, every agent, both scopes
    for agent in (None, "default", "work", "xiaomo", "worker"):
        for scope in ("ungrouped", "all"):
            snap = api._candidate_snapshot(HOME, core, agent, scope)
            excl = snap["excluded"]
            pool = snap["pool"]
            applied_pool = snap["applied_in_pool"]
            accounted = applied_pool + excl["rejected"] + excl["other_status"]
            all_accounted = len(snap["rows"]) + accounted
            sentence = api._empty_pool_sentence(
                scope, pool, applied_pool, excl, 91, 0, candidates=len(snap["rows"]))
            key = "%s/%s" % (agent or "__all__", scope)
            out[key] = {"pool": pool, "applied_in_pool": applied_pool,
                        "rejected": excl["rejected"], "other": excl["other_status"],
                        "accounted": accounted, "candidates": len(snap["rows"]),
                        "sentence": sentence, "excluded": excl}
            print("\n[" + key + "] pool=%d applied_in_pool=%d rejected=%d other=%d candidates=%d"
                  % (pool, applied_pool, excl["rejected"], excl["other_status"], len(snap["rows"])))
            print("  " + sentence)
            # invariant 1: pool == candidates + applied_in_pool + rejected + other
            check(key + " 池 = 候选 + 本池已分组 + 忽略 + 其它",
                  pool == len(snap["rows"]) + applied_pool + excl["rejected"] + excl["other_status"],
                  "pool=%d vs %d" % (pool, len(snap["rows"]) + accounted))
            # invariant 2: the old contradictory phrasings must be gone
            for bad in ("其中已分组 91", "看板任务 %d 条）" % excl["kanban"]):
                if bad == "其中已分组 91" and applied_pool == 91:
                    continue
                check(key + " 文案不含旧矛盾片断 " + bad, bad not in sentence)
            # invariant 3: pool number never presented as containing kanban
            check(key + " 看板数不进「本池 N 条」括号",
                  not ("本池 %d 条会话（其中已分组 %d 条、手动忽略" % (pool, applied_pool)) in sentence)
            # invariant 4: the pool decomposition is written as an explicit sum equal to pool
            if pool > 0:
                check(key + " 文案把池写成显式等式",
                      ("本池 %d 条会话 = 候选 %d 条 + 已分组 %d 条 + 手动忽略 %d 条 + 其它状态 %d 条"
                       % (pool, len(snap["rows"]), applied_pool, excl["rejected"],
                          excl["other_status"])) in sentence)

    after = sha(db)
    check("assignments.db 前后同哈希（只读）", before == after, before[:16] + " -> " + after[:16])
    out["_assignments_sha"] = {"before": before, "after": after, "unchanged": before == after}
    out["_failures"] = FAILS
    Path("probe_empty_reason_after.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nFAILURES: %d %s" % (len(FAILS), FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

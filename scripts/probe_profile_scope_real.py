# -*- coding: utf-8 -*-
"""验收探针 A（只读）：真实 home 上 candidate_scope / desktop_visible_pool 按 agent 是否看见 profile 库。

**只读**：引擎全部以 mode=ro 打开 state.db，assignments.db 也只读；本脚本写 sha256 前后对比，
证明零污染。跑法：
    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe scripts/probe_profile_scope_real.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
REAL_HOME = PLUGIN.parents[1]
OUT = HERE / "_probe_out" / "profile-scope"
OUT.mkdir(parents=True, exist_ok=True)

os.environ.setdefault("HERMES_HOME", str(REAL_HOME))
for path in (str(REAL_HOME / "hermes-agent"), str(PLUGIN / "dashboard")):
    if path not in sys.path:
        sys.path.insert(0, path)

import api_autogroup as base  # noqa: E402
import autogroup_ai as engine  # noqa: E402

STORE = REAL_HOME / "auto-group" / "assignments.db"
BEFORE = hashlib.sha256(STORE.read_bytes()).hexdigest()

lines = []


def say(t=""):
    print(t, flush=True)
    lines.append(t)


home = Path(base._home())
say("HOME: " + str(home))
say("assignments.db sha256 BEFORE: " + BEFORE)

checks = []


def chk(name, ok, extra=""):
    checks.append((name, bool(ok), extra))
    say(("PASS " if ok else "FAIL ") + name + (("  — " + str(extra)) if extra else ""))


#: session_id -> 库所在 profile（直接从每个库读，作为独立的事实来源）
ids_by_db = {}
for prof, db in engine.profile_scan_targets(home):
    if not Path(db).is_file():
        continue
    cols = engine._session_columns_at(db)
    rows = engine._read_session_rows(Path(db), cols)
    ids_by_db[prof] = {str(r["id"]) for r in rows}

say("")
say("== 1) candidate_scope(agent=work) ==")
work = engine.candidate_scope(home, agents={"work"})
say(json.dumps({k: v for k, v in work.items()
                if k not in ("candidate_session_ids", "candidate_profiles")}, ensure_ascii=False, indent=1))
wid = list(work.get("candidate_session_ids") or [])
say("candidate ids(%d): %s" % (len(wid), wid))
chk("1. work total_available > 0", int(work.get("total_available") or 0) > 0, work.get("total_available"))
chk("1b. work 每个候选 id 都在 profiles/work/state.db",
    bool(wid) and all(sid in ids_by_db.get("work", set()) for sid in wid),
    str([sid for sid in wid if sid not in ids_by_db.get("work", set())]))

say("")
say("== 2) candidate_scope(agent=xiaomo) ==")
xm = engine.candidate_scope(home, agents={"xiaomo"})
xid = list(xm.get("candidate_session_ids") or [])
chk("2. xiaomo total_available > 0", int(xm.get("total_available") or 0) > 0, xm.get("total_available"))
chk("2b. xiaomo 每个候选 id 都在 profiles/xiaomo/state.db",
    bool(xid) and all(sid in ids_by_db.get("xiaomo", set()) for sid in xid),
    str([sid for sid in xid if sid not in ids_by_db.get("xiaomo", set())]))

say("")
say("== 3) agent 隔离：work 结果不含任何 default/xiaomo id ==")
default_ids = ids_by_db.get("default", set())
chk("3. work ∩ default = 空", not (set(wid) & default_ids), sorted(set(wid) & default_ids)[:5])
chk("3b. work ∩ xiaomo = 空", not (set(wid) & ids_by_db.get("xiaomo", set())), sorted(set(wid) & ids_by_db.get("xiaomo", set()))[:5])
chk("3c. xiaomo ∩ default = 空", not (set(xid) & default_ids), sorted(set(xid) & default_ids)[:5])

say("")
say("== 4) worker / default ==")
for name in ("worker", "default", "code-worker", "apm-worker"):
    sc = engine.candidate_scope(home, agents={name})
    say("%-12s total_available=%-4s pool=%-4s applied_in_pool=%-4s excluded_applied=%-4s"
        % (name, sc.get("total_available"), sc.get("pool"), sc.get("applied_in_pool"),
           sc.get("excluded_applied")))
    if name in ("worker",):
        chk("4. worker total_available > 0", int(sc.get("total_available") or 0) > 0, sc.get("total_available"))

say("")
say("== 5) agent=__all__ 并集 ==")
allsc = engine.candidate_scope(home)
allid = set(allsc.get("candidate_session_ids") or [])
union = set(wid) | set(xid)
chk("5. __all__ ⊇ work ∪ xiaomo", union.issubset(allid), len(allid))
chk("5b. __all__ total_available == len(ids)", int(allsc.get("total_available") or 0) == len(allid),
    "%s vs %s" % (allsc.get("total_available"), len(allid)))
say("__all__ total_available=%s pool=%s applied_in_pool=%s excluded_applied=%s"
    % (allsc.get("total_available"), allsc.get("pool"), allsc.get("applied_in_pool"),
       allsc.get("excluded_applied")))

say("")
say("== 6) 空态文案不自相矛盾（work 池里的已分组数 ≤ 池大小） ==")
for name in ("work", "xiaomo", "worker", "default"):
    sc = engine.candidate_scope(home, agents={name})
    pool = int(sc.get("pool") or 0)
    ap = int(sc.get("applied_in_pool") or 0)
    chk("6.%s applied_in_pool(%d) <= pool(%d)" % (name, ap, pool), ap <= pool,
        "pool=%d applied_in_pool=%d" % (pool, ap))

say("")
say("== 7) pool 与候选的加和对得上 ==")
for name in ("work", "xiaomo", "worker"):
    sc = engine.candidate_scope(home, agents={name})
    total = int(sc.get("pool") or 0)
    parts = (int(sc.get("total_available") or 0) + int(sc.get("applied_in_pool") or 0)
             + int(sc.get("excluded_rejected") or 0) + int(sc.get("excluded_other") or 0))
    chk("7.%s 池 == 候选+本池已分组+忽略+其它" % name, total == parts,
        "pool=%d parts=%d (%s)" % (total, parts,
                                   [sc.get("total_available"), sc.get("applied_in_pool"),
                                    sc.get("excluded_rejected"), sc.get("excluded_other")]))

say("")
say("== 8) default 池（根库）在改口径后仍然只剩已分组 ==")
d = engine.candidate_scope(home, agents={"default"})
say("default pool=%s applied_in_pool=%s total_available=%s excluded_child=%s"
    % (d.get("pool"), d.get("applied_in_pool"), d.get("total_available"), d.get("excluded_child")))

AFTER = hashlib.sha256(STORE.read_bytes()).hexdigest()
say("")
say("assignments.db sha256 AFTER : " + AFTER)
chk("9. 生产库零污染（sha256 前后相同）", BEFORE == AFTER, BEFORE[:16] + "…")

say("")
say("== 10) 真库零写入：state.db 的 mtime/size 复核 ==")
for prof, db in engine.profile_scan_targets(home):
    p = Path(db)
    if p.is_file():
        st = p.stat()
        say("%-14s %12d B  mtime=%s" % (prof, st.st_size, int(st.st_mtime)))

fails = [c for c in checks if not c[1]]
say("")
say("SUMMARY: %d/%d PASS, %d FAIL" % (len(checks) - len(fails), len(checks), len(fails)))
for name, _ok, extra in fails:
    say("  FAILED: " + name + "  " + str(extra))
(OUT / "scope-real.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("OUT: " + str(OUT / "scope-real.txt"))
sys.exit(1 if fails else 0)

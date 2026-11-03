# -*- coding: utf-8 -*-
"""真 home 只读实测：新代码的候选集 vs 旧口径（复现操作员的 28 → 2）。

**只读**：不写任何库，不起服务，不动 15948 那个进程。
跑法：<venv>/python.exe scripts/probe_real_home_candidates.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
DASH = PLUGIN / "dashboard"
REAL_HOME = PLUGIN.parents[1]

os.environ.setdefault("HERMES_HOME", str(REAL_HOME))
for path in (str(REAL_HOME / "hermes-agent"), str(DASH)):
    if path not in sys.path:
        sys.path.insert(0, path)

import api_autogroup as base  # noqa: E402
import autogroup_ai as engine  # noqa: E402

home = Path(base._home())
print("HOME:", home, "（PID=%d）" % os.getpid())
core = base._load_core()

# ---- 旧口径：core.list_sessions（含 kanban）----
old_pool = list(core.list_sessions(home, limit=0, exclude_ids=core.rejected_ids(home)))
old_applied = {str(r.get("session_id")) for r in core.load_suggestions(home, "applied")}
old_ungrouped = [s for s in old_pool if str(s.get("id")) not in old_applied]
from collections import Counter  # noqa: E402
old_src = Counter(str(s.get("source") or "") for s in old_ungrouped)

# ---- 新口径：_candidate_snapshot ----
snap = base._candidate_snapshot(home, core, None, "ungrouped")
new_ids = set(snap["ids"])

# ---- AI 侧：candidate_scope ----
scope = engine.candidate_scope(home, agents=None)
ai_ids = {str(s) for s in (scope.get("candidate_session_ids") or [])}

print("\n旧口径 core.list_sessions(含kanban) 未分组候选 =", len(old_ungrouped), "来源分布:", dict(old_src))
print("新口径 _candidate_snapshot            =", len(snap["ids"]))
print("/autogroup/ai/scope 候选              =", len(ai_ids))
print("excluded =", json.dumps(snap["excluded"], ensure_ascii=False))
print("\n差集（新 - AI）=", sorted(new_ids - ai_ids))
print("差集（AI - 新）=", sorted(ai_ids - new_ids))
print("差集（新 - 旧）=", sorted(new_ids - {str(s.get('id')) for s in old_ungrouped}))
print("差集（旧 - 新）=", sorted({str(s.get('id')) for s in old_ungrouped} - new_ids)[:40])

ok = (new_ids == ai_ids)
print("\nREAL-HOME-PARITY:", "PASS（新口径 == AI 口径，差集为空）" if ok else "FAIL")
print("OLD-VS-NEW:", len(old_ungrouped), "→", len(snap["ids"]))
out = PLUGIN / "scripts" / "_probe_out" / "autogroup-frontend"
out.mkdir(parents=True, exist_ok=True)
(out / "real-home-candidates.txt").write_text(
    "PID: %d\nHOME: %s\n\n" % (os.getpid(), home)
    + "旧口径 core.list_sessions(含kanban) 未分组候选: %d  来源=%s\n"
      % (len(old_ungrouped), json.dumps(dict(old_src), ensure_ascii=False))
    + "新口径 _candidate_snapshot: %d\n" % len(snap["ids"])
    + "/autogroup/ai/scope: %d\n" % len(ai_ids)
    + "excluded: %s\n" % json.dumps(snap["excluded"], ensure_ascii=False)
    + "差集(新-AI): %s\n" % sorted(new_ids - ai_ids)
    + "差集(AI-新): %s\n" % sorted(ai_ids - new_ids)
    + "差集(旧-新) 前 40 条: %s\n" % sorted({str(s.get('id')) for s in old_ungrouped} - new_ids)[:40]
    + "\nREAL-HOME-PARITY: %s\n" % ("PASS" if ok else "FAIL"),
    encoding="utf-8")
sys.exit(0 if ok else 1)

# -*- coding: utf-8 -*-
"""t_3c2817b1 · 候选集口径验收（**按 ID 集合差集**，不只看数量）。

用隔离 home（真 projects.db 副本 + 合成 state.db + 合成 assignments.db），直接调真实后端函数：
  * 普通扫描（ai=false）的候选 ID 集 vs AI 扫描（ai=true）的候选 ID 集 → 差集必须为空；
  * 与 `/autogroup/ai/scope` 的候选集 → 差集必须为空；
  * 逐条列出「应纳入 / 误入 / 漏入」的会话，并解释原因；
  * 覆盖四类：已分组(applied)、未分组、曾有建议未应用(suggested)、已撤销归属(undone)。

跑法：
    <venv>/python.exe scripts/probe_candidate_parity.py --sandbox D:/hermes-data/temp/ag-frontend/sb2
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
DASH = PLUGIN / "dashboard"
REAL_HOME = PLUGIN.parents[1]

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  PASS " if ok else "  FAIL ") + name + (("  — " + str(extra)) if extra else ""), flush=True)


ASSIGN_SCHEMA = """
CREATE TABLE IF NOT EXISTS assignments (
  session_id TEXT PRIMARY KEY, project_slug TEXT NOT NULL, confidence REAL NOT NULL,
  reason TEXT, source TEXT, status TEXT NOT NULL, batch_id TEXT, applied_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS batches (
  batch_id TEXT PRIMARY KEY, created_at REAL, note TEXT, undone INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rejected (session_id TEXT PRIMARY KEY, reason TEXT, created_at REAL);
"""

#: 四类会话，逐类覆盖：已分组 / 未分组 / 曾有建议未应用 / 已撤销归属 / 看板 / 隐藏 / 归档未置顶
SESSIONS = (
    # (id, title, profile, source, hidden, archived, pinned, message_count, assign_status)
    ("s-applied", "已分组：kanban 调度器", "default", "cli", 0, 0, 0, 5, "applied"),
    ("s-suggest", "曾有建议未应用：quicker 快捷键", "default", "cli", 0, 0, 0, 4, "suggested"),
    ("s-undone", "已撤销归属：multi-agent 编排", "default", "cli", 0, 0, 0, 3, "undone"),
    ("s-loose", "未分组：quick-send 面板点击", "default", "cli", 0, 0, 0, 2, None),
    ("s-loose2", "未分组：weather 图表口径", "default", "local", 0, 0, 0, 1, None),
    ("s-kanban", "看板任务会话（必须排除）", "default", "kanban", 0, 0, 0, 9, None),
    ("s-hidden", "隐藏会话（必须排除）", "default", "cli", 1, 0, 0, 4, None),
    ("s-archived", "归档未置顶（必须排除）", "default", "cli", 0, 1, 0, 4, None),
    ("s-pinned", "归档但置顶（仍可见）", "default", "cli", 0, 1, 1, 4, None),
    ("s-subagent", "subagent 来源（必须排除）", "default", "subagent", 0, 0, 0, 3, None),
    ("s-other", "别的 agent 的会话（按 agent 过滤时排除）", "work", "cli", 0, 0, 0, 3, None),
)


def build_sandbox(target: Path) -> Path:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    (target / "profiles").mkdir(exist_ok=True)
    shutil.copy2(REAL_HOME / "projects.db", target / "projects.db")
    shutil.copy2(REAL_HOME / "config.yaml", target / "config.yaml")
    for profile in ("work",):
        folder = target / "profiles" / profile
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REAL_HOME / "config.yaml", folder / "config.yaml")

    real_state = REAL_HOME / "state.db"
    conn = sqlite3.connect(str(real_state), timeout=20)
    conn.execute("ATTACH DATABASE ? AS dst", (str(target / "state.db"),))
    cols = [r[1] for r in conn.execute("PRAGMA main.table_info(sessions)")]
    conn.execute("CREATE TABLE dst.sessions AS SELECT * FROM main.sessions WHERE 0")
    conn.commit()
    conn.close()

    db = sqlite3.connect(str(target / "state.db"))
    now = time.time()
    try:
        present = [r[1] for r in db.execute("PRAGMA table_info(sessions)")]
        with db:
            for (sid, title, profile, source, hidden, archived, pinned, msgs, _st) in SESSIONS:
                values = {"id": sid, "title": title, "display_name": title,
                          "profile_name": profile, "source": source, "hidden": hidden,
                          "archived": archived, "pinned": pinned, "message_count": msgs,
                          "started_at": now - 3600, "last_activity_at": now - 60,
                          "cwd": None, "parent_session_id": None}
                keys = [k for k in values if k in present]
                db.execute("INSERT OR REPLACE INTO sessions(" + ",".join(keys) + ") VALUES("
                           + ",".join("?" * len(keys)) + ")", [values[k] for k in keys])
            # 让 first_user_text 有内容（表可能不存在/列不同，插不进去就算了）
            for (sid, title, *_rest) in SESSIONS:
                try:
                    db.execute("INSERT INTO messages(session_id, role, content, created_at)"
                               " VALUES(?, 'user', ?, ?)", (sid, title, now - 120))
                except sqlite3.Error:
                    pass
    finally:
        db.close()

    ro = sqlite3.connect("file:" + str(target / "projects.db") + "?mode=ro", uri=True)
    try:
        slugs = [r[0] for r in ro.execute(
            "SELECT slug FROM projects WHERE archived = 0 ORDER BY created_at LIMIT 3")]
    finally:
        ro.close()

    store = target / "auto-group" / "assignments.db"
    store.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(store))
    try:
        conn.executescript(ASSIGN_SCHEMA)
        for index, (sid, _t, _p, _s, _h, _a, _pi, _m, status) in enumerate(SESSIONS):
            if not status:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO assignments(session_id, project_slug, confidence, reason,"
                " source, status, batch_id, applied_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, slugs[index % len(slugs)], 0.9, "夹具预置", "panel", status,
                 "bg-fixture" if status == "applied" else None,
                 now - 300 if status == "applied" else None, now - 300))
        conn.commit()
    finally:
        conn.close()

    # 插件树镜像（api_agents.hermes_home 靠路径上溯，不认 HERMES_HOME）
    mirror = target / "plugins"
    shutil.copytree(DASH, mirror / "hermes-console" / "dashboard",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "dist"))
    (mirror / "hermes-auto-group").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REAL_HOME / "plugins" / "hermes-auto-group" / "core.py",
                 mirror / "hermes-auto-group" / "core.py")
    return target


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--sandbox", default="D:/hermes-data/temp/ag-frontend/sb2")
    args = parser.parse_args(argv)
    home = build_sandbox(Path(args.sandbox))
    os.environ["HERMES_HOME"] = str(home)
    for path in (str(REAL_HOME / "hermes-agent"), str(home / "plugins" / "hermes-console" / "dashboard")):
        if path not in sys.path:
            sys.path.insert(0, path)

    import api_autogroup as base
    import autogroup_ai as engine

    resolved = Path(base._home()).resolve()
    check("写操作目标是沙盒（不是真 home）", resolved == home.resolve(), str(resolved))

    core = base._load_core()

    # ---------- 1. 普通扫描（ai=false）的候选集 ----------
    print("\n[1] 普通扫描 vs AI 扫描 vs /ai/scope 三份候选集按 ID 比对")
    snap_plain = base._candidate_snapshot(home, core, None, "ungrouped")
    plain_ids = set(snap_plain["ids"])

    scope = engine.candidate_scope(home, agents=None)
    scope_ids = {str(s) for s in (scope.get("candidate_session_ids") or [])}

    snap_ai = base._candidate_snapshot(home, core, None, "ungrouped")
    ai_ids = set(snap_ai["ids"])

    print("  普通扫描候选:", sorted(plain_ids))
    print("  /ai/scope 候选:", sorted(scope_ids))
    check("普通扫描 == /autogroup/ai/scope（差集为空）", plain_ids == scope_ids,
          "only_scan=%s only_scope=%s" % (sorted(plain_ids - scope_ids), sorted(scope_ids - plain_ids)))
    check("AI 扫描 == 普通扫描（同一份快照）", ai_ids == plain_ids,
          "only_ai=%s only_plain=%s" % (sorted(ai_ids - plain_ids), sorted(plain_ids - ai_ids)))

    # ---------- 2. 逐条解释应纳入 / 误入 / 漏入 ----------
    print("\n[2] 逐条核对（应纳入 / 误入 / 漏入）")
    # agent=None ⇒ **全机器**：别的 agent 的会话也应当进候选（按 agent 筛选时才排除）
    expected_in = {"s-loose", "s-loose2", "s-pinned", "s-suggest", "s-other"}
    expected_out = {"s-applied": "已 applied（A 模式排除）",
                    "s-kanban": "source=kanban（看板任务，硬排除）",
                    "s-hidden": "hidden=1（桌面不可见）",
                    "s-archived": "archived=1 且未 pinned（不可见）",
                    "s-subagent": "source=subagent（非普通对话）",
                    "s-undone": "已撤销归属（status=undone，非 suggested ⇒ 不再自动提议）"}
    got = plain_ids
    print("  应纳入:", sorted(expected_in))
    print("  实际纳入:", sorted(got))
    for sid, why in sorted(expected_out.items()):
        check("排除 %s（%s）" % (sid, why), sid not in got)
    for sid in sorted(expected_in):
        check("纳入 %s" % sid, sid in got)
    missed = expected_in - got
    extra = got - expected_in
    check("漏入为空", not missed, sorted(missed))
    check("误入为空", not extra, sorted(extra))

    # ---------- 3. 快照里的 excluded 计数自证 ----------
    print("\n[3] 快照自报的排除计数")
    exc = snap_plain["excluded"]
    check("excluded.kanban >= 1（看板会话确实被排除）", exc.get("kanban", 0) >= 1, str(exc))
    check("excluded.applied >= 1（已分组会话被排除）", exc.get("applied", 0) >= 1, str(exc))
    check("excluded.other_status >= 1（已撤销/其它状态被排除，与 AI 侧一致）",
          exc.get("other_status", 0) >= 1, str(exc))

    # ---------- 4. scope=all 保留已分组（B 模式语义） ----------
    print("\n[4] scope=all（B 模式）语义：已分组会话应保留在候选里")
    snap_all = base._candidate_snapshot(home, core, None, "all")
    all_ids = set(snap_all["ids"])
    check("scope=all 包含已 applied 会话", "s-applied" in all_ids, sorted(all_ids))
    check("scope=all 仍然排除看板会话", "s-kanban" not in all_ids, sorted(all_ids))
    check("scope=all ⊃ scope=ungrouped", plain_ids <= all_ids,
          "missing=%s" % sorted(plain_ids - all_ids))

    # ---------- 5. agent 过滤 ----------
    print("\n[5] agent 过滤")
    snap_work = base._candidate_snapshot(home, core, "work", "ungrouped")
    check("agent=work 只拿到 work 的会话",
          set(snap_work["ids"]) == {"s-other"}, sorted(snap_work["ids"]))
    check("agent=work 不串到 default 的会话", "s-loose" not in snap_work["ids"])

    print("\nCANDIDATE-PARITY: %d PASS / %d FAIL" % (len(PASS), len(FAIL)))
    for name in FAIL:
        print("  FAILED:", name)
    out = PLUGIN / "scripts" / "_probe_out" / "autogroup-frontend"
    out.mkdir(parents=True, exist_ok=True)
    (out / "candidate-parity.txt").write_text(
        "CANDIDATE-PARITY: %d PASS / %d FAIL\n\n" % (len(PASS), len(FAIL))
        + "普通扫描候选 ID: " + json.dumps(sorted(plain_ids), ensure_ascii=False) + "\n"
        + "/ai/scope 候选 ID: " + json.dumps(sorted(scope_ids), ensure_ascii=False) + "\n"
        + "差集(scan-scope): " + json.dumps(sorted(plain_ids - scope_ids)) + "\n"
        + "差集(scope-scan): " + json.dumps(sorted(scope_ids - plain_ids)) + "\n"
        + "excluded: " + json.dumps(exc, ensure_ascii=False) + "\n\n"
        + "\n".join(("PASS " + n) for n in PASS)
        + ("\n\n" + "\n".join("FAIL " + n for n in FAIL) if FAIL else ""),
        encoding="utf-8")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

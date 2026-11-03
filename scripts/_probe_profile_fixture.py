# -*- coding: utf-8 -*-
"""t_bba9216d 夹具：**work/xiaomo 的会话只存在 profiles/<p>/state.db、根库里一条都没有**。

这正是老板机器上的布局（根库 715 行全是 default，work/xiaomo 各自独立库）。用它验收
「界面上按钮的 N == 点下去之后符合条件 N == 真正处理的 N，且候选确实来自 profile 库」。

复用 ``autogroup_ui_fixture.make_sandbox / build_app``（真 plugin_api + 隔离沙盒 + 真产物），
再把种子换成「只有 profile 库有会话」的一组。**只写沙盒**：启动时硬断言
``api_autogroup._home() == 沙盒``，生产库一个字节都不动。

跑法（由 scripts/probe-profile-scope-e2e.mjs 起）：
    <venv>/python.exe scripts/_probe_profile_fixture.py --port 9317 --sandbox <dir>
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
REAL_HOME = PLUGIN.parents[1]
sys.path.insert(0, str(HERE))

import autogroup_ui_fixture as fx  # noqa: E402

PREFIX = "/api/plugins/hermes-console"

#: (id, title, profile_name, message_count, cwd, source, hidden, archived)
ROOT_ROWS = (
    ("root-01", "default：记忆系统召回率怎么调", "default", 5,
     "D:/hermes-data/projects/memory", "cli", 0, 0),
    ("root-02", "default：k8s 集群从 1.28 升到 1.30", "default", 4,
     "D:/hermes-data/projects/infra", "cli", 0, 0),
    ("root-03", "default：这条已经归入项目了", "default", 4, None, "cli", 0, 0),
    ("root-hidden", "default：隐藏会话（不该显示）", "default", 4, None, "cli", 1, 0),
    ("root-archived", "default：归档未置顶（不该显示）", "default", 4, None, "cli", 0, 1),
    ("root-empty", "default：空会话（不该显示）", "default", 0, None, "cli", 0, 0),
)

#: profile 库里的行**不写 profile_name**（真库里就是这样：work/xiaomo 全空）
WORK_ROWS = (
    ("work-only-01", "work：烟台万华灌装线技术协议格式统一", 6, "cli", 0),
    ("work-only-02", "work：200L 塑料桶灌装线技术协议", 5, "cli", 0),
    ("work-only-03", "work：报价清单预算表", 3, "cli", 0),
    ("work-only-hidden", "work：隐藏会话（不该显示）", 4, "cli", 1),
    ("work-only-archived", "work：归档未置顶（不该显示）", 4, "cli", 0),
    ("work-only-empty", "work：空会话（不该显示）", 0, "cli", 0),
    ("work-only-kanban", "work：看板任务会话（不该显示）", 5, "kanban", 0),
)
XM_ROWS = (
    ("xm-only-01", "xiaomo：录音转录与工时登记", 7, "cli", 0),
    ("xm-only-02", "xiaomo：每日学习料推送", 4, "cli", 0),
)

EXPECT_WORK = ["work-only-01", "work-only-02", "work-only-03"]
EXPECT_XM = ["xm-only-02"]
EXPECT_ROOT = ["root-01", "root-02"]


def seed(home: Path) -> dict:
    real_state = REAL_HOME / "state.db"
    for name in ("work", "xiaomo"):
        folder = home / "profiles" / name
        folder.mkdir(parents=True, exist_ok=True)
        cfg = folder / "config.yaml"
        if not cfg.is_file():
            cfg.write_text("{}\n", encoding="utf-8")

    #: ``make_sandbox`` 已经种了一组通用会话（根库 6 条 + profiles/work 3 条 + xiaomo 2 条）。
    #: 本卡要的是「work/xiaomo 只在 profile 库里有」，所以先把每个库清空再种。
    for db in (home / "state.db", home / "profiles" / "work" / "state.db",
               home / "profiles" / "xiaomo" / "state.db"):
        db.parent.mkdir(parents=True, exist_ok=True)
        if not db.is_file():
            fx.seed_state_db(db, real_state, ())
            continue
        conn = sqlite3.connect(str(db))
        try:
            with conn:
                conn.execute("DELETE FROM sessions")
                try:
                    conn.execute("DELETE FROM messages")
                except sqlite3.Error:
                    pass
        finally:
            conn.close()

    fx.seed_state_db(home / "state.db", real_state, ROOT_ROWS)
    fx.seed_state_db(home / "profiles" / "work" / "state.db", real_state, WORK_ROWS)
    fx.seed_state_db(home / "profiles" / "xiaomo" / "state.db", real_state, XM_ROWS)

    #: 归档未置顶的那条手工置 archived=1（seed_state_db 的 archived 参数只覆盖根库行）
    conn = sqlite3.connect(str(home / "profiles" / "work" / "state.db"))
    try:
        with conn:
            conn.execute("UPDATE sessions SET archived=1 WHERE id='work-only-archived'")
    finally:
        conn.close()

    ro = sqlite3.connect("file:" + str(home / "projects.db") + "?mode=ro", uri=True)
    try:
        slugs = [r[0] for r in ro.execute(
            "SELECT slug FROM projects WHERE archived = 0 ORDER BY created_at LIMIT 3")]
    finally:
        ro.close()
    if len(slugs) < 3:
        raise SystemExit("[fixture] 真 projects.db 可用项目不足 3 个")

    store = home / "auto-group" / "assignments.db"
    conn = sqlite3.connect(str(store))
    try:
        for table in ("assignments", "batches", "cwd_backup", "rejected"):
            try:
                conn.execute("DELETE FROM " + table)
            except sqlite3.Error:
                pass
        now = time.time()
        #: 跨库各一条 applied：root-03（根库）、xm-only-01（xiaomo 库）
        for sid, slug in (("root-03", slugs[0]), ("xm-only-01", slugs[1])):
            conn.execute(
                "INSERT INTO assignments(session_id, project_slug, confidence, reason, source,"
                " status, batch_id, applied_at, updated_at)"
                " VALUES(?,?,0.9,'夹具预置 applied','panel','applied','batch-fx',?,?)",
                (sid, slug, now - 300, now - 300))
        conn.commit()
    finally:
        conn.close()
    return {"work": EXPECT_WORK, "xiaomo": EXPECT_XM, "default": EXPECT_ROOT}


def add_e2e_routes(app, home: Path) -> None:
    """只读测试路由：各库 id 集合 + 归属表快照（探针按它交叉核对，不靠猜）。"""
    from fastapi import Body
    from fastapi.responses import JSONResponse

    def health():
        return JSONResponse(content={"ok": True, "home": str(home)})

    def dbs():
        out = {}
        candidates = [("default", home / "state.db")]
        for child in sorted((home / "profiles").iterdir()):
            if child.is_dir() and (child / "config.yaml").is_file():
                candidates.append((child.name, child / "state.db"))
        for name, path in candidates:
            if not path.is_file():
                out[name] = {"exists": False, "rows": [], "columns": []}
                continue
            conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
                rows = [dict(r) for r in conn.execute("SELECT * FROM sessions ORDER BY id")]
            finally:
                conn.close()
            out[name] = {"exists": True, "path": str(path), "columns": cols, "rows": rows}
        return JSONResponse(content={"ok": True, "data": out})

    def assignments():
        store = home / "auto-group" / "assignments.db"
        rows, rejected = [], []
        if store.is_file():
            conn = sqlite3.connect("file:" + str(store) + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                rows = [dict(r) for r in conn.execute("SELECT * FROM assignments ORDER BY session_id")]
                rejected = [dict(r) for r in conn.execute("SELECT * FROM rejected ORDER BY session_id")]
            finally:
                conn.close()
        return JSONResponse(content={"ok": True, "data": {"assignments": rows, "rejected": rejected}})

    def scoped():
        """引擎原始口径（含 ``candidate_session_ids``）。

        ``/autogroup/ai/scope`` 故意把 id 列表 pop 掉（前端不需要），但本探针要按 ID 集合核对，
        所以这里直接调同一个引擎函数 —— 不是另写一套口径。
        """
        import autogroup_ai as engine
        out = {}
        for name in ("work", "xiaomo", "default"):
            out[name] = engine.candidate_scope(home, agents={name})
        out["__all__"] = engine.candidate_scope(home)
        return JSONResponse(content={"ok": True, "data": out})

    app.add_api_route(PREFIX + "/e2e/health", health, methods=["GET"])
    app.add_api_route(PREFIX + "/e2e/dbs", dbs, methods=["GET"])
    def mark_applied(payload: dict = Body(default=None)):
        """夹具专用：把指定 id 标成 applied（只写沙盒 ``auto-group/assignments.db``）。

        为什么需要它：空态文案的验收要点是「池 / 本池已分组 / 全机器 applied」三个数同源，
        而不是再验一次写入路径（写入路径由 54 项 E2E 的 S4 覆盖）。而真实 ``/autogroup/apply``
        会按置信度阈值 0.65 拒掉规则命中（0.59）的建议 —— 那是**正确**行为，不该为了造场景去改阈值。
        所以这里由夹具直接写沙盒库，**不碰任何生产数据**。

        ``payload`` 必须声明成 FastAPI 的 body 参数：早先这里直接引用了一个未定义的名字，
        一调就 NameError → 500，探针 3.0 起整段空态场景都跑不起来。
        """
        from fastapi.responses import JSONResponse
        data = payload or {}
        ids = [str(i) for i in (data.get("session_ids") or []) if str(i).strip()]
        if not ids:
            return JSONResponse(content={"ok": False, "error": "session_ids 为空"}, status_code=400)
        store = home / "auto-group" / "assignments.db"
        conn = sqlite3.connect(str(store))
        try:
            with conn:
                for sid in ids:
                    row = conn.execute("SELECT project_slug, confidence FROM assignments"
                                       " WHERE session_id=?", (sid,)).fetchone()
                    slug = row[0] if row else (data.get("project_slug") or "memory")
                    conf = row[1] if row else 0.9
                    conn.execute(
                        "INSERT OR REPLACE INTO assignments(session_id, project_slug, confidence,"
                        " reason, source, status, batch_id, applied_at, updated_at)"
                        " VALUES(?,?,?,?,?,'applied','batch-fixture-applied',?,?)",
                        (sid, slug, conf, "夹具置为 applied（空态场景）", "fixture",
                         time.time(), time.time()))
        finally:
            conn.close()
        return JSONResponse(content={"ok": True, "data": {"applied": ids}})

    app.add_api_route(PREFIX + "/e2e/mark-applied", mark_applied, methods=["POST"])
    app.add_api_route(PREFIX + "/e2e/assignments", assignments, methods=["GET"])
    app.add_api_route(PREFIX + "/e2e/scope", scoped, methods=["GET"])


def main(argv) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9317)
    parser.add_argument("--sandbox", required=True)
    args = parser.parse_args(argv)
    home = fx.make_sandbox(Path(args.sandbox))
    seed(home)
    os.environ["HERMES_HOME"] = str(home)
    sandbox_dashboard = home / "plugins" / "hermes-console" / "dashboard"
    for path in (str(fx.HERMES_AGENT), str(sandbox_dashboard)):
        if path not in sys.path:
            sys.path.insert(0, path)
    import uvicorn

    app, module = fx.build_app(sandbox_dashboard)
    sys.path.insert(0, str(sandbox_dashboard))
    import api_autogroup  # noqa: E402

    resolved = Path(api_autogroup._home()).resolve()
    if resolved != home.resolve():
        print("[fixture] REFUSE: hermes_home()=%s 不是沙盒 %s" % (resolved, home), flush=True)
        return 3
    add_e2e_routes(app, home)
    print("[fixture] home=%s port=%d" % (home, args.port), flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

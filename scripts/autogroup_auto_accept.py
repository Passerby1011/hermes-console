# -*- coding: utf-8 -*-
"""项目分组 · **自动归组六项验收**（t_0c8ab8c1 的 B 块，沙盒跑，真 home 只读不写）。

跑法::

    <venv>/python.exe plugins/hermes-console/scripts/autogroup_auto_accept.py \
        --sandbox D:/tmp/agg-auto-sb

六项（卡面验收，缺一不算）::

    1. 关着零写入      —— enabled=false 时 tick 直接跳过，归属库 sha256/console_ops 一条不变
    2. 开着自动 applied —— 只把开关打开（不调 tick），守护进程自己跑出 applied 行
    3. 不确定留建议    —— 置信度 < 阈值的那条留在 suggested，达阈值的才是 applied
    4. 重复跑不重复写  —— 再跑一轮 applied=0 且归属库字节不变
    5. 可撤销逐字段还原 —— POST /autogroup/undo{op_id}，整行 9 个字段与写入前逐字段相等
    6. 关闭后不再写    —— 关掉开关后等若干个守护进程轮次，归属库字节不变

同时盯住**真 home**：`auto-group/assignments.db`、`projects.db`、`auto-group/auto_mode.json`
的 sha256 全程不变（生产一个字节都不许动）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
REAL_HOME = PLUGIN_ROOT.parents[1]
HERMES_AGENT = REAL_HOME / "hermes-agent"
PREFIX = "/api/plugins/hermes-console"
ASSIGN_COLUMNS = ("session_id", "project_slug", "confidence", "reason", "source", "status",
                  "batch_id", "applied_at", "updated_at")

sys.path.insert(0, str(SCRIPTS))
import autogroup_ui_fixture as fixture  # noqa: E402 - 复用沙盒构造（真 projects.db 副本 + 插件树镜像）

results: list[dict] = []


def check(name: str, ok: bool, extra: str = "") -> bool:
    results.append({"name": name, "ok": bool(ok), "extra": extra})
    print(("  PASS " if ok else "  FAIL ") + name + (("  — " + extra) if extra else ""), flush=True)
    return ok


def sha256(path: Path) -> str:
    if not path.is_file():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mtime_ns(path: Path) -> int:
    return path.stat().st_mtime_ns if path.is_file() else 0


def rows_of(home: Path, table: str, where: str = "") -> list[dict]:
    path = home / "auto-group" / "assignments.db"
    if not path.is_file():
        return []
    conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if table not in names:
            return []
        sql = "SELECT * FROM " + table + (" WHERE " + where if where else "")
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def assign_row(home: Path, session_id: str) -> dict:
    got = [r for r in rows_of(home, "assignments", "session_id = '" + session_id + "'")]
    return got[0] if got else {}


def http(base: str, path: str, body=None, method: str = "") -> tuple[int, dict]:
    url = base + PREFIX + path
    data = None
    headers = {"Accept": "application/json"}
    verb = method or ("POST" if body is not None else "GET")
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=verb)
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return res.status, json.loads(res.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except ValueError:
            return exc.code, {"raw": raw[:400]}


def start_server(home: Path, port: int):
    """按夹具的方式把**镜像里的真 plugin_api.py** 起起来（沙盒 home，端口随便挑）。"""
    os.environ["HERMES_HOME"] = str(home)
    sandbox_dashboard = home / "plugins" / "hermes-console" / "dashboard"
    for path in (str(HERMES_AGENT), str(sandbox_dashboard)):
        if path not in sys.path:
            sys.path.insert(0, path)
    import uvicorn

    app, module = fixture.build_app(sandbox_dashboard)
    import api_autogroup  # noqa: E402

    resolved = Path(api_autogroup._home()).resolve()
    if resolved != home.resolve():
        raise SystemExit("REFUSE: hermes_home()=%s 不是沙盒 %s" % (resolved, home))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(300):
        if server.started:
            return server, module
        time.sleep(0.05)
    raise SystemExit("uvicorn 没起来")


def clean_store(home: Path) -> None:
    """把夹具预置的 suggested 行清空：本轮验收要自己造「写入前」的确定状态。"""
    path = home / "auto-group" / "assignments.db"
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            for table in ("assignments", "batches", "cwd_backup", "rejected", "console_ops"):
                try:
                    conn.execute("DELETE FROM " + table)
                except sqlite3.OperationalError:
                    pass
    finally:
        conn.close()
    auto_mode = home / "auto-group" / "auto_mode.json"
    if auto_mode.is_file():
        auto_mode.unlink()
    for name in ("auto_daemon.json", "auto_daemon.log"):
        target = home / "auto-group" / name
        if target.is_file():
            target.unlink()


def seed_suggested(home: Path, session_id: str, slug: str) -> dict:
    """预置一条 suggested 行（「写入前有这一行」的分支，撤销必须逐字段还原它）。"""
    path = home / "auto-group" / "assignments.db"
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO assignments(session_id, project_slug, confidence, reason,"
                " source, status, batch_id, applied_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (session_id, slug, 0.41, "预置建议：人工确认前的原行", "rule", "suggested",
                 "pn-seeded", None, 1700000000.5))
    finally:
        conn.close()
    return assign_row(home, session_id)


def project_fingerprint(home: Path) -> tuple:
    """projects.db 的**内容**指纹（项目行 + 目录行）。

    不比字节：``project_meta.active_id`` 是桌面端/CLI 维护的「当前选中项目」指针，
    老板的桌面端一开就会自己翻，字节变了不等于我们写了项目表。这里只比真内容。
    """
    path = home / "projects.db"
    if not path.is_file():
        return ()
    conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
    try:
        rows = tuple(sorted(tuple(r) for r in conn.execute(
            "SELECT slug, name, primary_path, archived FROM projects ORDER BY slug")))
        folders = tuple(sorted(tuple(r) for r in conn.execute(
            "SELECT project_id, path, is_primary FROM project_folders ORDER BY path")))
        return rows + ("--folders--",) + folders
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="自动归组六项验收（沙盒）")
    parser.add_argument("--sandbox", default=str(REAL_HOME / "scratch" / "t_0c8ab8c1" / "agg-auto-sb"))
    parser.add_argument("--port", type=int, default=9197)
    parser.add_argument("--wait-applied", type=float, default=120.0)
    args = parser.parse_args(argv)

    guard = [REAL_HOME / "auto-group" / "assignments.db", REAL_HOME / "auto-group" / "auto_mode.json"]
    guard_before = {str(p): (sha256(p), mtime_ns(p)) for p in guard}
    projects_before = project_fingerprint(REAL_HOME)


    home = fixture.make_sandbox(Path(args.sandbox))
    clean_store(home)
    base = "http://127.0.0.1:%d" % args.port
    server, _module = start_server(home, args.port)
    store = home / "auto-group" / "assignments.db"
    print("[accept] sandbox=%s" % home, flush=True)
    print("[accept] store=%s" % store, flush=True)
    daemon_pid = 0
    try:
        # 「写入前有这一行」的分支：先造一条 suggested 行，撤销时必须逐字段还原它
        seeded = seed_suggested(home, "sess-agg-01", "hermes-plugins")
        print("[accept] 预置 suggested 行：%s" % json.dumps(seeded, ensure_ascii=False), flush=True)

        # ---- 1. 关着零写入 --------------------------------------------------
        print("\n[1] 关着零写入", flush=True)
        before_sha, before_mtime = sha256(store), mtime_ns(store)
        st, body = http(base, "/autogroup/auto/tick", {"limit": 20})
        data = body.get("data") or {}
        check("关闭时 tick 返回 skipped 且 wrote=0",
              st == 200 and data.get("skipped") is True and int(data.get("wrote") or 0) == 0,
              "http=%d skipped=%s wrote=%s" % (st, data.get("skipped"), data.get("wrote")))
        check("关闭时归属库 sha256 未变", sha256(store) == before_sha)
        check("关闭时归属库 mtime 未变（没被打开写过）", mtime_ns(store) == before_mtime)
        check("关闭时 console_ops 一条都没写", len(rows_of(home, "console_ops")) == 0,
              "ops=%d" % len(rows_of(home, "console_ops")))
        check("关闭时没有拉起守护进程", not (home / "auto-group" / "auto_daemon.json").is_file())

        # ---- 2. 开着：无人工干预自动 applied --------------------------------
        print("\n[2] 开着自动模式 → 不调 tick，等守护进程自己跑出 applied", flush=True)
        st, body = http(base, "/autogroup/auto", {"enabled": True, "interval_minutes": 1})
        data = body.get("data") or {}
        daemon = data.get("daemon") or {}
        check("开启自动模式回读 enabled=true", st == 200 and data.get("enabled") is True, str(st))
        check("开启时自动拉起守护进程", bool(daemon.get("running")) and bool(daemon.get("spawned")),
              json.dumps(daemon, ensure_ascii=False)[:200])
        daemon_pid = int(daemon.get("pid") or 0)
        applied: list[dict] = []
        started = time.time()
        while time.time() - started < args.wait_applied:
            applied = rows_of(home, "assignments", "status = 'applied'")
            if applied:
                break
            time.sleep(1.0)
        waited = round(time.time() - started, 1)
        check("★ 没有任何人工触发，守护进程自己写出了 applied 行", bool(applied),
              "等了 %ss，applied=%d 条：%s" % (waited, len(applied),
                                              ",".join(r["session_id"] for r in applied)))
        check("★ 自动写入的 source 全是 auto（不是 suggested 冒充）",
              bool(applied) and all(r["source"] == "auto" for r in applied),
              ",".join(sorted({str(r["source"]) for r in applied})))
        check("★ 自动写入的 status 全是 applied",
              bool(applied) and all(r["status"] == "applied" for r in applied))
        beat = {}
        beat_path = home / "auto-group" / "auto_daemon.json"
        if beat_path.is_file():
            beat = json.loads(beat_path.read_text(encoding="utf-8"))
        check("守护进程心跳文件在（pid + ticks + last_tick）",
              bool(beat.get("pid")) and int(beat.get("ticks") or 0) >= 1 and bool(beat.get("last_tick_at")),
              "pid=%s ticks=%s" % (beat.get("pid"), beat.get("ticks")))
        threshold = float((beat.get("last_result") or {}).get("threshold") or 0.65)

        # ---- 3. 不确定的留建议 ----------------------------------------------
        print("\n[3] 不确定留建议（阈值 %s）" % threshold, flush=True)
        suggested = rows_of(home, "assignments", "status = 'suggested'")
        low = [r for r in suggested if float(r["confidence"]) < threshold]
        check("★ 置信度低于阈值的会话留在 suggested（没被硬写 applied）", bool(low),
              "suggested=%d 条，其中低置信 %d 条：%s" % (
                  len(suggested), len(low),
                  ",".join("%s@%.2f" % (r["session_id"], r["confidence"]) for r in low)))
        check("★ applied 的每一条置信度都 ≥ 阈值",
              all(float(r["confidence"]) >= threshold for r in applied),
              ",".join("%s@%.2f" % (r["session_id"], r["confidence"]) for r in applied))
        check("预置的 sess-agg-01 被升级成 applied（原本是 suggested）",
              assign_row(home, "sess-agg-01").get("status") == "applied",
              str(assign_row(home, "sess-agg-01").get("status")))

        # ---- 4. 重复跑不重复写 ----------------------------------------------
        print("\n[4] 重复跑不重复写", flush=True)
        again_sha, again_count = sha256(store), len(rows_of(home, "assignments", "status = 'applied'"))
        st, body = http(base, "/autogroup/auto/tick", {"limit": 20})
        data = body.get("data") or {}
        check("第二轮 tick applied=0", st == 200 and int(data.get("applied") or 0) == 0,
              "applied=%s suggested=%s" % (data.get("applied"), data.get("suggested")))
        check("第二轮 tick suggested=0（同样的建议不重写）", int(data.get("suggested") or 0) == 0,
              str(data.get("suggested")))
        check("第二轮 tick 后归属库 sha256 未变", sha256(store) == again_sha)
        check("第二轮 tick 后 applied 行数未变",
              len(rows_of(home, "assignments", "status = 'applied'")) == again_count,
              "%d → %d" % (again_count, len(rows_of(home, "assignments", "status = 'applied'"))))

        # ---- 5. 可撤销 + 逐字段还原 -----------------------------------------
        print("\n[5] 可撤销且逐字段还原", flush=True)
        st, ops_body = http(base, "/autogroup/ops?limit=50")
        ops = ((ops_body.get("data") or {}).get("ops") or [])
        auto_ops = [o for o in ops if o.get("op") == "auto.apply" and not o.get("undone")]
        check("台账里有 auto.apply 批次（自动写入也落 console_ops）", bool(auto_ops),
              json.dumps([{"op_id": o.get("op_id"), "n": o.get("session_count")} for o in auto_ops],
                         ensure_ascii=False)[:200])
        op_id = (auto_ops[0] or {}).get("op_id") if auto_ops else ""
        st, undo_body = http(base, "/autogroup/undo", {"op_id": op_id})
        undo_data = undo_body.get("data") or {}
        readback = undo_data.get("readback") or {}
        check("撤销接口 200 且回读零不一致",
              st == 200 and not (readback.get("mismatched") or []),
              "http=%d mismatched=%s" % (st, json.dumps(readback.get("mismatched"), ensure_ascii=False)[:160]))
        restored = assign_row(home, "sess-agg-01")
        same = all(restored.get(c) == seeded.get(c) for c in ASSIGN_COLUMNS)
        diff = {c: {"before": seeded.get(c), "now": restored.get(c)}
                for c in ASSIGN_COLUMNS if restored.get(c) != seeded.get(c)}
        check("★ 预置行逐字段还原（9 个字段全等）", same,
              "" if same else json.dumps(diff, ensure_ascii=False)[:300])
        left = rows_of(home, "assignments", "status = 'applied'")
        check("★ 自动新插入的行被删掉（撤销后没有残留 applied）", not left,
              ",".join(r["session_id"] for r in left))
        st, ops_body = http(base, "/autogroup/ops?limit=50")
        same_op = [o for o in ((ops_body.get("data") or {}).get("ops") or [])
                   if o.get("op_id") == op_id]
        check("台账里这条批次已标 undone", bool(same_op) and same_op[0].get("undone") is True,
              str(same_op[0].get("undone_at") if same_op else ""))

        # ---- 6. 关闭后不再写 -------------------------------------------------
        print("\n[6] 关闭后不再写", flush=True)
        st, body = http(base, "/autogroup/auto", {"enabled": False})
        check("关闭自动模式回读 enabled=false",
              st == 200 and (body.get("data") or {}).get("enabled") is False, str(st))
        off_sha, off_mtime = sha256(store), mtime_ns(store)
        st, body = http(base, "/autogroup/auto/tick", {"limit": 20})
        check("关闭后 tick 又变回 skipped", (body.get("data") or {}).get("skipped") is True)
        time.sleep(25.0)  # 守护进程 loop=10s：跨过两轮，看它有没有偷偷写
        check("★ 关闭后守护进程跨两轮也没写归属库",
              sha256(store) == off_sha and mtime_ns(store) == off_mtime)
        check("关闭后 console_ops 没有新增（还是原来那些）",
              len(rows_of(home, "console_ops")) == len(rows_of(home, "console_ops")))
    finally:
        try:
            if not daemon_pid and (home / "auto-group" / "auto_daemon.json").is_file():
                daemon_pid = int(json.loads((home / "auto-group" / "auto_daemon.json")
                                            .read_text(encoding="utf-8")).get("pid") or 0)
        except Exception:  # noqa: BLE001
            pass
        if daemon_pid:
            os.system("taskkill /PID %d /F > NUL 2>&1" % daemon_pid)
        server.should_exit = True

    # ---- 真 home 保护 ------------------------------------------------------
    print("\n[7] 真 home 保护（生产一个字节都不许动）", flush=True)
    for path, (before, before_m) in guard_before.items():
        after = sha256(Path(path))
        check("真 home 未变：" + path.replace(str(REAL_HOME), ""),
              after == before and mtime_ns(Path(path)) == before_m,
              "" if after == before else "sha256 变了")
    after_projects = project_fingerprint(REAL_HOME)
    check("真 home 的 projects.db 项目行/目录行未被改（内容指纹）",
          after_projects == projects_before,
          "行数 %d → %d" % (len(projects_before), len(after_projects)))

    failed = [r for r in results if not r["ok"]]
    print("\n[accept] %d PASS / %d FAIL" % (len(results) - len(failed), len(failed)), flush=True)
    summary = {"sandbox": str(home), "pass": len(results) - len(failed), "fail": len(failed),
               "results": results}
    out = Path(args.sandbox) / "accept-summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[accept] summary=%s" % out, flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())


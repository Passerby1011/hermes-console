# -*- coding: utf-8 -*-
"""hermes-console · Advisor 覆盖面 + 按 profile 设置：**真 HTTP** 验收探针。

跑法（用带 fastapi/uvicorn/httpx 的 Hermes 环境）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        D:/hermes-data/hermes/plugins/hermes-console/scripts/probe_advisor_coverage.py

它做三件事，全部**真打网络**（uvicorn 起在 127.0.0.1 的临时端口，httpx 走 loopback）：

[1] ``GET /advisor/coverage``：每个 profile 一行；逐字段**与磁盘核对**
    （plugin 目录 / plugin.yaml 版本 / config.yaml 的 plugins.enabled 与
    allow_gateway_injection / advice.jsonl 行数 / reviews.jsonl 行数 /
    sessions/*.json 个数与 held_notes 合计 / settings.json），
    并核对状态：``work`` = reviewing_only（装+启用+有 sessions+无台账）、
    ``xiaomo`` = not_installed（未装）。
[2] 设置按 profile 读写：对**非 default** 的 profile 写一个合法字段 → 回读；
    前后对**所有** profile 的 settings.json 做 sha256/mtime 快照，证明只动了目标文件；
    写前写后把目标文件还原（探针不留副作用）。
[3] 非法写入：未知字段 → 400；``blocker_forced_delivery=false`` → 400（贴状态码与响应）。

探针不重启任何进程，也不改任何 profile 的 config.yaml。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import socket
import sys
import threading
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
API = DASHBOARD / "plugin_api.py"
HOME = PLUGIN_ROOT.parents[1]
PREFIX = "/api/plugins/hermes-console"

for extra in (str(DASHBOARD), str(HOME / "hermes-agent"), str(HOME / "plugins")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402

failures = []
passes = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passes
    if ok:
        passes += 1
        print("  PASS  " + label + (" — " + detail if detail else ""))
    else:
        failures.append(label + (" — " + detail if detail else ""))
        print("  FAIL  " + label + (" — " + detail if detail else ""))


def show(label: str, response: httpx.Response, limit: int = 400) -> None:
    body = response.text.replace("\n", " ")
    print("  %-46s %s  %s" % (label, response.status_code, body[:limit]))


def sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "-"


def snapshot() -> dict:
    """所有 profile 的 settings.json：sha256 + mtime（不存在就是 '-'）。"""
    rows = {}
    targets = [(("default"), HOME)]
    for child in sorted((HOME / "profiles").iterdir()):
        if child.is_dir():
            targets.append((child.name, child))
    for name, base in targets:
        path = base / "advisor" / "audit" / "settings.json"
        rows[name] = {"path": str(path), "exists": path.is_file(), "sha": sha(path),
                      "mtime": path.stat().st_mtime_ns if path.is_file() else 0}
    return rows


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def load_app() -> FastAPI:
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_hermes_console", API)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix=PREFIX)
    return app


def main() -> int:
    app = load_app()
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        print("服务没起来")
        return 2
    base = "http://127.0.0.1:%d%s" % (port, PREFIX)
    client = httpx.Client(timeout=60)
    print("服务: " + base + "（真实 HTTP，loopback）")

    try:
        run_all(client, base)
    finally:
        client.close()
        server.should_exit = True
        thread.join(timeout=5)

    print("\n=== 汇总 ===")
    print("PASS: %d   FAIL: %d" % (passes, len(failures)))
    if failures:
        for item in failures:
            print("  ! " + item)
        return 1
    print("ADVISOR-COVERAGE-PROBE-OK")
    return 0


def run_all(client: httpx.Client, base: str) -> None:
    print("\n[1] GET /advisor/coverage（真 HTTP）")
    response = client.get(base + "/advisor/coverage")
    show("GET /advisor/coverage", response, 260)
    check("coverage 返回 200", response.status_code == 200, "status=%s" % response.status_code)
    payload = response.json()
    data = payload.get("data") or {}
    rows = {row["profile"]: row for row in data.get("profiles") or []}
    print("        状态计数: " + json.dumps(data.get("status_counts"), ensure_ascii=False))
    for name, row in rows.items():
        print("        %-16s %-18s installed=%-5s enabled=%-5s inject=%-5s events=%-4s "
              "sessions=%-3s held=%-3s" % (name, row.get("status"), row.get("plugin_installed"),
                                           row.get("enabled_in_config"),
                                           row.get("injection_allowed"),
                                           row.get("ledger_events"), row.get("session_files"),
                                           row.get("held_notes")))
    check("9 个 profile 全部出现（default + 8）", len(rows) == 9, str(sorted(rows)))
    for name in ("default", "work", "xiaomo", "cad-worker", "code-worker", "research-worker",
                 "apm-worker", "media-worker", "worker"):
        check("矩阵里有 " + name, name in rows)

    # ---- 逐字段与磁盘核对 ----
    mismatches = []
    for name, row in rows.items():
        base_dir = HOME if name == "default" else HOME / "profiles" / name
        plugin = base_dir / "plugins" / "advisor"
        ledger = base_dir / "advisor" / "audit" / "advice.jsonl"
        reviews = base_dir / "advisor" / "audit" / "reviews.jsonl"
        sessions = sorted((base_dir / "advisor" / "sessions").glob("*.json")) \
            if (base_dir / "advisor" / "sessions").is_dir() else []
        held = 0
        for path in sessions:
            try:
                blob = json.loads(path.read_text(encoding="utf-8"))
                held += len(blob.get("held_notes") or [])
            except (OSError, ValueError):
                pass
        lines = 0
        if ledger.is_file():
            for line in ledger.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    lines += 1
        review_lines = 0
        if reviews.is_file():
            review_lines = sum(1 for line in reviews.read_text(encoding="utf-8").splitlines()
                               if line.strip())
        expect = {
            "plugin_installed": plugin.is_dir(),
            "session_files": len(sessions),
            "held_notes": held,
            "ledger_events": lines,
            "review_lines": review_lines,
            "ledger_exists": ledger.is_file(),
        }
        for field, value in expect.items():
            if row.get(field) != value:
                mismatches.append("%s.%s 接口=%r 磁盘=%r" % (name, field, row.get(field), value))
    check("每一行的 装没装/台账/评审流水/sessions/held 与磁盘逐字段一致",
          not mismatches, "；".join(mismatches[:6]))

    work = rows.get("work") or {}
    print("        work 行: " + json.dumps({k: work.get(k) for k in (
        "status", "plugin_installed", "plugin_version", "enabled_in_config",
        "injection_allowed", "audit_dir_exists", "ledger_events", "session_files",
        "held_notes", "settings_exists")}, ensure_ascii=False))
    check("work = 装 + 启用 + 有 sessions + 无台账 → reviewing_only",
          work.get("plugin_installed") is True and work.get("enabled_in_config") is True
          and (work.get("session_files") or 0) > 0 and work.get("ledger_events") == 0
          and work.get("status") == "reviewing_only", str(work.get("status")))
    check("work 的诊断含「需重启该 profile 的 gateway/serve」",
          "gateway/serve" in (work.get("diagnosis") or ""), (work.get("diagnosis") or "")[:90])
    check("work 的 injection_allowed=false（config 里没写 true）",
          work.get("injection_allowed") is False)

    xiaomo = rows.get("xiaomo") or {}
    print("        xiaomo 行: " + json.dumps({k: xiaomo.get(k) for k in (
        "status", "plugin_installed", "plugin_version", "enabled_in_config",
        "session_files", "ledger_events")}, ensure_ascii=False))
    check("xiaomo = 没装插件 → not_installed",
          xiaomo.get("plugin_installed") is False and xiaomo.get("status") == "not_installed",
          str(xiaomo.get("status")))
    check("xiaomo 磁盘上确实没有 plugins/advisor",
          not (HOME / "profiles" / "xiaomo" / "plugins" / "advisor").is_dir())

    default_row = rows.get("default") or {}
    check("default = 有台账 + 允许投递 → active",
          default_row.get("status") == "active" and default_row.get("ledger_events", 0) > 0,
          "%s events=%s" % (default_row.get("status"), default_row.get("ledger_events")))
    counts = data.get("status_counts") or {}
    check("状态计数与行一致",
          counts.get("active") == sum(1 for r in rows.values() if r.get("status") == "active")
          and counts.get("not_installed") == sum(1 for r in rows.values()
                                                 if r.get("status") == "not_installed"),
          json.dumps(counts, ensure_ascii=False))
    check("coverage 明确标注只读", data.get("read_only") is True)

    # 只读性：再打一次，配置与台账文件的 sha256/mtime 全部不变
    before = {p: (sha(p), p.stat().st_mtime_ns) for p in (
        HOME / "config.yaml",
        HOME / "advisor" / "audit" / "advice.jsonl",
        HOME / "profiles" / "work" / "config.yaml") if p.is_file()}
    client.get(base + "/advisor/coverage")
    after = {p: (sha(p), p.stat().st_mtime_ns) for p in before}
    check("coverage 只读：config.yaml / 台账 mtime 与 sha256 都没变", before == after,
          str([p.name for p, v in before.items() if after[p] != v]))

    # ------------------------------------------------------------------
    print("\n[2] 设置按 profile 读写（目标：非 default 的 cad-worker）")
    target = "cad-worker"
    snap_before = snapshot()
    print("        写前 settings.json 快照: " + json.dumps(
        {k: (v["exists"], v["sha"]) for k, v in snap_before.items()}, ensure_ascii=False))

    got = client.get(base + "/advisor/settings", params={"agent": target})
    show("GET /advisor/settings?agent=" + target, got, 300)
    got_data = (got.json().get("data") or {})
    check("按 profile 读设置 200", got.status_code == 200, "status=%s" % got.status_code)
    check("读到的是目标 profile 的 settings.json 路径",
          got_data.get("path", "").replace("\\", "/").endswith(
              "profiles/%s/advisor/audit/settings.json" % target), got_data.get("path", ""))
    check("返回里带 profile 列表（默认 default 在其中）",
          "default" in (got_data.get("profiles") or []), str(got_data.get("profiles")))

    put = client.put(base + "/advisor/settings", params={"agent": target},
                     json={"min_interval_seconds": 7})
    show("PUT min_interval_seconds=7 @ " + target, put, 400)
    put_data = (put.json().get("data") or {})
    check("写合法字段 200", put.status_code == 200, "status=%s" % put.status_code)
    check("回读生效值 = 7（不是提交值）",
          (put_data.get("settings") or {}).get("min_interval_seconds") == 7,
          json.dumps(put_data.get("applied"), ensure_ascii=False))
    check("落盘原文 stored.min_interval_seconds = 7",
          (put_data.get("stored") or {}).get("min_interval_seconds") == 7,
          json.dumps(put_data.get("stored"), ensure_ascii=False))
    check("写的是目标 profile 的文件",
          put_data.get("path", "").replace("\\", "/").endswith(
              "profiles/%s/advisor/audit/settings.json" % target), put_data.get("path", ""))

    snap_after = snapshot()
    changed = [name for name in snap_before
               if snap_before[name]["sha"] != snap_after[name]["sha"]
               or snap_before[name]["mtime"] != snap_after[name]["mtime"]]
    print("        写后变化: " + json.dumps({k: (v["exists"], v["sha"])
                                            for k, v in snap_after.items()}, ensure_ascii=False))
    check("只有 %s 的 settings.json 变了" % target, changed == [target], str(changed))
    others = {k: (snap_before[k]["sha"], snap_after[k]["sha"]) for k in snap_before
              if k != target}
    check("其它 profile 的 settings.json 内容与 mtime 全部不变",
          all(a == b for a, b in others.values()),
          json.dumps({k: v for k, v in others.items() if v[0] != v[1]}, ensure_ascii=False))

    # 还原（探针不留副作用：写前没有这个文件就删掉，有就写回原内容）
    target_path = Path(snap_before[target]["path"])
    if snap_before[target]["exists"]:
        original = target_path.read_bytes()
        target_path.write_bytes(original)
    else:
        target_path.unlink(missing_ok=True)
        parent = target_path.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    restored = snapshot()
    check("探针已把 %s 的 settings.json 还原到写前状态" % target,
          restored[target]["exists"] == snap_before[target]["exists"]
          and restored[target]["sha"] == snap_before[target]["sha"],
          "%s -> %s" % (snap_before[target]["sha"], restored[target]["sha"]))

    # 旧参数 ?profile= 已改名（t_43f17bdb）：宿主会把它当二级 profile 路由指令，请求到不了本端点。
    legacy = client.get(base + "/advisor/settings", params={"profile": target})
    show("GET /advisor/settings?profile=（已改名）", legacy, 300)
    check("旧参数 ?profile= → 400 且指出改用 ?agent=",
          legacy.status_code == 400 and "?agent=" in (legacy.text or ""),
          "status=%s %s" % (legacy.status_code, (legacy.text or "")[:80]))
    check("旧参数被拒时没有读成 default 的设置（不会静默读错 profile）",
          (legacy.json().get("data") or {}).get("profile") != "default")

    # ------------------------------------------------------------------
    print("\n[3] 非法写入必须 400")
    unknown = client.put(base + "/advisor/settings", params={"agent": target},
                         json={"enable_everything": True})
    show("PUT 未知字段", unknown, 300)
    check("未知字段 → 400", unknown.status_code == 400, "status=%s" % unknown.status_code)
    check("未知字段的错误码是 invalid_argument",
          ((unknown.json().get("error") or {}).get("code")) == "invalid_argument")

    valve = client.put(base + "/advisor/settings", params={"agent": target},
                       json={"blocker_forced_delivery": False})
    show("PUT blocker_forced_delivery=false", valve, 300)
    check("blocker_forced_delivery=false → 400", valve.status_code == 400,
          "status=%s" % valve.status_code)
    check("响应里写明「单向阀门不可关闭」",
          "单向阀门不可关闭" in (valve.text or ""), (valve.text or "")[:120])

    bad_profile = client.put(base + "/advisor/settings", params={"agent": "no-such-agent"},
                             json={"enabled": True})
    show("PUT 未知 profile", bad_profile, 200)
    check("未知 profile → 400", bad_profile.status_code == 400,
          "status=%s" % bad_profile.status_code)

    no_plugin = client.put(base + "/advisor/settings", params={"agent": "xiaomo"},
                           json={"min_interval_seconds": 1})
    show("PUT 未装插件的 profile", no_plugin, 200)
    check("未装插件的 profile 写设置 → 400", no_plugin.status_code == 400,
          "status=%s" % no_plugin.status_code)
    check("未装插件的 profile 没有被创建出 settings.json",
          not (HOME / "profiles" / "xiaomo" / "advisor" / "audit" / "settings.json").is_file())

    # ------------------------------------------------------------------
    print("\n[4] 既有分区回归（状态码）")
    regressions = [
        ("概览 /overview/system", "/overview/system"),
        ("Agent 总览 /agents/overview", "/agents/overview"),
        ("天气 /weather/now", "/weather/now"),
        ("项目分组 /autogroup/status", "/autogroup/status"),
        ("升级 /upgrade/scan", "/upgrade/scan"),
        ("Advisor 原列表 /advisor/advice", "/advisor/advice"),
        ("Advisor 自检 /advisor", "/advisor"),
        ("Advisor 统计 /advisor/summary", "/advisor/summary"),
        ("Advisor 评审流水 /advisor/reviews", "/advisor/reviews"),
        ("Advisor 设置 /advisor/settings", "/advisor/settings"),
        ("Agents /agents", "/agents"),
    ]
    for label, path in regressions:
        result = client.get(base + path)
        show("GET " + label, result, 120)
        check(label + " 可访问", result.status_code == 200, "status=%s" % result.status_code)


if __name__ == "__main__":
    sys.exit(main())

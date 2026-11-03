# -*- coding: utf-8 -*-
"""Advisor 设置四项真写验收（t_22813006）——**真路由 + 真沙盒 settings.json**。

本卡硬要求第 3 条：「四个设置项都能改、能保存、保存后 GET 回读值与落盘 settings.json 一致」，
而且要**真跑接口**，不能只截图。本脚本就用 ``advisor_ui_fixture.py`` 那套沙盒（真
``dashboard/plugin_api.py`` + 真 ``advisor`` 包 + 临时 home），逐项 PUT → GET → 读文件。

跑法::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        D:/hermes-data/hermes/plugins/hermes-console/scripts/probe_advisor_settings_write.py

只写沙盒：``api_advisor._home`` 指向临时目录，真 home 的 settings.json 一个字节都不碰
（脚本结尾用 sha256 复核真 home 文件未变）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
HOME = PLUGIN_ROOT.parents[1]
sys.path.insert(0, str(DASHBOARD))
sys.path.insert(0, str(HOME / "hermes-agent"))
sys.path.insert(0, str(HOME / "plugins"))

PREFIX = "/api/plugins/hermes-console"
REAL_SETTINGS = HOME / "advisor" / "audit" / "settings.json"

passes = 0
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global passes
    if ok:
        passes += 1
        print("  PASS  " + label + (("  — " + detail) if detail else ""))
    else:
        failures.append(label + (("  — " + detail) if detail else ""))
        print("  FAIL  " + label + (("  — " + detail) if detail else ""))


def digest(path: Path) -> str:
    if not path.is_file():
        return "(missing)"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_app(sandbox: Path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_hermes_console", str(DASHBOARD / "plugin_api.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    import api_advisor  # noqa: F401  （确保与 fixture 同一份实现）
    import advisor.audit  # noqa: F401

    api_advisor._home = lambda: sandbox  # type: ignore[assignment]
    app = FastAPI()
    app.include_router(module.router, prefix=PREFIX)
    return TestClient(app), module


def main() -> int:
    real_before = digest(REAL_SETTINGS)
    print("[env] 真 home settings.json sha256 = %s（脚本结束时复核未变）" % real_before)

    sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
    import advisor_ui_fixture as fixture_mod
    sandbox = fixture_mod.build_sandbox()
    client, module = build_app(sandbox)
    print("[env] sandbox = %s" % sandbox)
    print("[env] routes = %d" % len(module.router.routes))

    settings_path = sandbox / "advisor" / "audit" / "settings.json"

    def get_settings(agent: str) -> dict:
        res = client.get(PREFIX + "/advisor/settings", params={"agent": agent})
        assert res.status_code == 200, res.text
        return res.json().get("data") or {}

    def put_settings(agent: str, body: dict) -> dict:
        res = client.put(PREFIX + "/advisor/settings", params={"agent": agent}, json=body)
        return {"status": res.status_code, "body": res.json() if res.content else {}}

    def on_disk() -> dict:
        return json.loads(settings_path.read_text(encoding="utf-8"))

    print("\n[0] 前置：沙盒初始值")
    start = get_settings("default")
    print("        values:", json.dumps(start.get("values"), ensure_ascii=False))
    print("        on_disk:", json.dumps(on_disk(), ensure_ascii=False))
    check("沙盒初始 enabled=false（与生产同形）", start.get("values", {}).get("enabled") is False,
          json.dumps(start.get("values", {}).get("enabled")))

    # ------------------------------------------------------------------ 1
    print("\n[1] 逐项改：PUT 一个键 → GET 回读 → 落盘文件逐字一致")
    cases = [
        ("enabled", True, True),
        ("concern_default", "deliver", "deliver"),
        ("min_interval_seconds", 45, 45),
        ("mode", "conservative", "conservative"),
    ]
    for key, value, want in cases:
        before = get_settings("default").get("values", {})
        res = put_settings("default", {key: value})
        status = res["status"]
        payload = (res["body"].get("data") or {})
        after = get_settings("default").get("values", {})
        disk = on_disk()
        detail = json.dumps({
            "status": status,
            "written": payload.get("written_keys"),
            "get": after.get(key),
            "disk": disk.get(key),
            "before": before.get(key),
        }, ensure_ascii=False)
        check("PUT %s=%r → 200 且只写这一个键" % (key, value),
              status == 200 and payload.get("written_keys") == [key], detail)
        check("%s：GET 回读 = %r" % (key, want), after.get(key) == want, detail)
        check("%s：落盘 settings.json = %r" % (key, want), disk.get(key) == want, detail)
        check("%s：GET 回读与落盘一致" % key, after.get(key) == disk.get(key), detail)

    print("\n[1b] 一次改四项（同一 PUT）：回读 + 落盘 + 写盘键集合")
    res = put_settings("default", {
        "enabled": False, "concern_default": "hold", "min_interval_seconds": 7, "mode": "target",
    })
    payload = (res["body"].get("data") or {})
    after = get_settings("default").get("values", {})
    disk = on_disk()
    print("        PUT 回读 values:", json.dumps(payload.get("values"), ensure_ascii=False))
    print("        GET  values     :", json.dumps(after, ensure_ascii=False))
    print("        disk            :", json.dumps(disk, ensure_ascii=False))
    check("一次改四项：written_keys 恰好是这四个",
          sorted(payload.get("written_keys") or []) ==
          sorted(["enabled", "concern_default", "min_interval_seconds", "mode"]),
          json.dumps(payload.get("written_keys")))
    for key, want in (("enabled", False), ("concern_default", "hold"),
                      ("min_interval_seconds", 7), ("mode", "target")):
        check("四项同改后 GET 与落盘：%s = %r" % (key, want),
              after.get(key) == want and disk.get(key) == want,
              json.dumps({"get": after.get(key), "disk": disk.get(key)}))

    # ------------------------------------------------------------------ 2
    print("\n[2] 未改动则零写盘（mtime + 字节都不变）")
    stamp_before = settings_path.stat().st_mtime_ns
    bytes_before = settings_path.read_bytes()
    res = put_settings("default", {})
    stamp_after = settings_path.stat().st_mtime_ns
    bytes_after = settings_path.read_bytes()
    check("空 PUT 不写盘（字节与 mtime 都不变）",
          bytes_before == bytes_after and stamp_before == stamp_after,
          json.dumps({"status": res["status"], "no_change": (res["body"].get("data") or {}).get("no_change")}))
    # 提交与当前值完全相同的整组：后端应判 no_change，且字节不变
    same = dict(after)
    res = put_settings("default", same)
    check("提交与生效值相同的一组：no_change 且字节不变",
          (res["body"].get("data") or {}).get("no_change") is True
          and settings_path.read_bytes() == bytes_after,
          json.dumps({"no_change": (res["body"].get("data") or {}).get("no_change")}))

    # ------------------------------------------------------------------ 3
    print("\n[3] 非法值被拒（400）且零落盘")
    bytes_before = settings_path.read_bytes()
    bad = put_settings("default", {"min_interval_seconds": -5})
    check("min_interval_seconds=-5 → 400", bad["status"] == 400,
          json.dumps(bad["body"], ensure_ascii=False)[:200])
    bad2 = put_settings("default", {"mode": "nonsense"})
    check("mode=nonsense → 400", bad2["status"] == 400,
          json.dumps(bad2["body"], ensure_ascii=False)[:200])
    check("两次非法请求都零落盘", settings_path.read_bytes() == bytes_before)

    # ------------------------------------------------------------------ 4
    print("\n[4] 按 agent 隔离：改 default 不影响 work")
    work_before = get_settings("work").get("values", {})
    put_settings("default", {"concern_default": "deliver"})
    work_after = get_settings("work").get("values", {})
    default_after = get_settings("default").get("values", {})
    print("        work before/after:", json.dumps(work_before, ensure_ascii=False),
          json.dumps(work_after, ensure_ascii=False))
    check("改 default 后 work 的四个键一个都没变", work_before == work_after,
          json.dumps({"before": work_before, "after": work_after}))
    check("default 自己确实改了（concern_default=deliver）",
          default_after.get("concern_default") == "deliver")
    work_disk = json.loads(
        (sandbox / "profiles" / "work" / "advisor" / "audit" / "settings.json")
        .read_text(encoding="utf-8"))
    check("work 的 settings.json 也未被 default 的写影响",
          work_disk.get("concern_default") == "deliver" and work_disk.get("mode") == "conservative",
          json.dumps(work_disk, ensure_ascii=False))

    # ------------------------------------------------------------------ 5
    print("\n[5] 宿主层字段不可写（只读事实）")
    res = put_settings("default", {"blocker_forced_delivery": False})
    check("PUT blocker_forced_delivery → 400（单向阀门不可写）", res["status"] == 400,
          json.dumps(res["body"], ensure_ascii=False)[:200])

    # ------------------------------------------------------------------ 6
    print("\n[6] 真 home 未被触碰")
    real_after = digest(REAL_SETTINGS)
    check("真 home settings.json sha256 未变", real_before == real_after,
          "%s → %s" % (real_before, real_after[:16]))

    shutil.rmtree(sandbox, ignore_errors=True)
    print("\n" + "=" * 72)
    if failures:
        print("ADVISOR-SETTINGS-WRITE-FAILED  (%d passed, %d failed)" % (passes, len(failures)))
        for item in failures:
            print("  - " + item)
        return 1
    print("ADVISOR-SETTINGS-WRITE-OK  (%d checks)" % passes)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())

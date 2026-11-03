# -*- coding: utf-8 -*-
"""接口探针：路由是否挂进聚合 + **生产数据只读核对**（不写任何生产库）。

跑法：``python plugins/hermes-console/dashboard/tests/probe_ai_routes.py``

三件事：
1. 插件聚合 ``plugin_api`` 里能数到 7 条 ``/autogroup/ai/*`` 路由（含 undo），且没有域加载失败；
2. 生产 ``auto-group/assignments.db`` **只读**打开：``applied`` 仍应是原 56 条（少于 56 直接 FAIL），
   并报告 AI 三张表是否已经建过（未建 = 本卡从未在生产写过东西）；
3. 生产 ``auto_mode.json`` 只读：只打印 enabled/mtime，**绝不写**（运营红线：开关不许碰）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

DASH = Path(__file__).resolve().parents[1]
if str(DASH) not in sys.path:
    sys.path.insert(0, str(DASH))

HOME = Path(r"D:\hermes-data\hermes")
EXPECTED_APPLIED = 56

failures = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + label + (("  " + detail) if detail else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    import plugin_api

    check(not plugin_api.DOMAIN_FAILURES, "域模块全部加载成功", str(plugin_api.DOMAIN_FAILURES))
    paths = sorted({r.path for r in plugin_api.router.routes if "/autogroup/ai/" in r.path})
    expected = ["/autogroup/ai/batches", "/autogroup/ai/classify", "/autogroup/ai/classify/{job_id}",
                "/autogroup/ai/confirm", "/autogroup/ai/preview", "/autogroup/ai/preview/{batch_id}",
                "/autogroup/ai/project", "/autogroup/ai/readback/{batch_id}", "/autogroup/ai/scope",
                "/autogroup/ai/undo"]
    check(paths == expected, "10 条 AI 路由已挂进聚合", str(paths))
    check("api_autogroup_ai_apply" in plugin_api.DOMAIN_LOADED
          and "api_autogroup_ai_undo" in plugin_api.DOMAIN_LOADED
          and "api_autogroup_ai_run" in plugin_api.DOMAIN_LOADED, "三个 AI 域模块都在 loaded 里")

    db = HOME / "auto-group" / "assignments.db"
    if not db.is_file():
        check(False, "生产 assignments.db 存在", str(db))
    else:
        conn = sqlite3.connect("file:" + str(db) + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            applied = conn.execute("SELECT COUNT(*) FROM assignments WHERE status='applied'").fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]
            check(applied >= EXPECTED_APPLIED, "生产 applied 归属未被减少",
                  "applied=%d total=%d" % (applied, total))
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            ai_tables = sorted(t for t in tables if t.startswith("ai_"))
            print("INFO AI 表在生产库里：%s（未建=本卡从未在生产写过）"
                  % (ai_tables or "一张都没有"))
        finally:
            conn.close()

    state = HOME / "auto-group" / "auto_mode.json"
    if state.is_file():
        payload = json.loads(state.read_text(encoding="utf-8"))
        print("INFO auto_mode.json enabled=%r mtime=%s（只读，未改动）"
              % (payload.get("enabled"), time.strftime("%Y-%m-%d %H:%M:%S",
                                                       time.localtime(state.stat().st_mtime))))
    print("\n%s" % ("全部通过" if not failures else "失败项：" + ", ".join(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())

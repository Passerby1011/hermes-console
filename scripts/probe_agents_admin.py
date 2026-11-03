# -*- coding: utf-8 -*-
"""hermes-console · C7 验收探针（真 HTTP + 真失败注入，不 mock 数据源本身）。

跑法（用带 fastapi 的 Hermes 环境；仓库根目录下）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        plugins/hermes-console/scripts/probe_agents_admin.py

覆盖（= 卡片的 5 条验收里属于后端的那 4 条）：
1. ``GET /agents/overview``：9 行的 id / available / sessions_total / cron_enabled + calibers；
2. **失败隔离**：monkeypatch 让 xiaomo 的 ``_open_session_db_for_profile`` 抛异常，
   断言只有那一行 ``available=false``，其余 8 行数据照常且整页仍 200；
3. 下钻分页：``/agents/xiaomo/sessions?limit=3`` 与 ``?limit=3&offset=3`` 的条数 + total；
4. ``/agents/xiaomo/cron`` 的 counts；
5. 未知 agent → 404，不冒充别的 profile 的数据。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
API = DASHBOARD / "plugin_api.py"
PREFIX = "/api/plugins/hermes-console"

# 生产同款 sys.path：后端进程就是从 hermes-agent 起的
HERMES_AGENT = PLUGIN_ROOT.parents[1] / "hermes-agent"
if HERMES_AGENT.is_dir() and str(HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT))

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label + (" — " + detail if detail else ""))
    if not ok:
        failures.append(label)


print("[0] 环境")
print("        python :", sys.version.split()[0])
print("        plugin :", str(PLUGIN_ROOT))

print("[1] 按 dashboard 的方式加载 plugin_api.py（生产同款挂载）")
spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_hermes-console", API)
mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
sys.modules[spec.name] = mod  # type: ignore[union-attr]
spec.loader.exec_module(mod)  # type: ignore[union-attr]
paths = sorted({getattr(r, "path", "?") for r in mod.router.routes})
print("        routes :", len(paths), "条")
for expected in (
    "/agents",
    "/agents/overview",
    "/agents/{agent_id}/sessions",
    "/agents/{agent_id}/cron",
):
    check("routes 含 " + expected, expected in paths)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app = FastAPI()
app.include_router(mod.router, prefix=PREFIX)
client = TestClient(app, raise_server_exceptions=False)

# ---------------------------------------------------------------------------
print("[2] GET /agents/overview（真实 9 个 profile）")
resp = client.get(PREFIX + "/agents/overview")
check("状态码 200", resp.status_code == 200, str(resp.status_code))
body = resp.json()
check("ok=true", body.get("ok") is True, json.dumps(body)[:200])
rows = body.get("agents") or []
check("9 行", len(rows) == 9, str(len(rows)))
print("        home    :", body.get("home"))
print("        gateway :", json.dumps(body.get("gateway"), ensure_ascii=False))
print("        elapsed :", body.get("elapsed_ms"), "ms")
print("  id                avail  sess  active  last_active_at        cron(en/total/paused)  model                 plugs  gw")
for row in rows:
    gw = row.get("gateway") or {}
    print(
        "  {:<17} {:<5} {:>5} {:>7}  {:<20} {:>3}/{:<3}/{:<3}          {:<20} {:>5}  {}".format(
            row.get("id"),
            str(row.get("available")),
            str(row.get("sessions_total")),
            str(row.get("sessions_active")),
            str(row.get("last_active_at"))[:20],
            row.get("cron_enabled") if row.get("cron_enabled") is not None else "-",
            row.get("cron_total") if row.get("cron_total") is not None else "-",
            row.get("cron_paused") if row.get("cron_paused") is not None else "-",
            str(row.get("model"))[:20],
            str(row.get("plugins_enabled_count")),
            ("running" if gw.get("running") else str(gw.get("mode"))) if gw else "null",
        )
    )
print("  gateway 明细（id -> running / pid / port / mode）:")
for row in rows:
    gw = row.get("gateway") or {}
    print("    {:<17} {:<6} pid={:<8} port={:<6} {}".format(
        row.get("id"), str(gw.get("running")), str(gw.get("pid")), str(gw.get("port")),
        str(gw.get("mode"))))
check("每行都有 data_at", all(isinstance(r.get("data_at"), (int, float)) for r in rows))
check("每行都有 gateway 字段或原因",
      all(r.get("gateway") is not None or (r.get("field_errors") or {}).get("gateway") for r in rows))
check("可用行 >= 8", sum(1 for r in rows if r.get("available")) >= 8,
      str(sum(1 for r in rows if r.get("available"))))
print("  calibers:")
for key, text in (body.get("calibers") or {}).items():
    print("    " + key + " = " + text)
check("calibers 覆盖所有数字字段",
      all(k in (body.get("calibers") or {}) for k in
          ("sessions_total", "sessions_active", "last_active_at", "cron_total",
           "cron_enabled", "cron_paused", "model", "plugins_enabled_count", "data_at")))

# ---------------------------------------------------------------------------
print("[3] 失败隔离实测：让 xiaomo 的 state.db 打不开")
import hermes_cli.web_server_sessions as ws  # noqa: E402

real_open = ws._open_session_db_for_profile


def boom(profile, *, read_only):
    if profile == "xiaomo":
        raise PermissionError("模拟 state.db 被占用/损坏：[WinError 32] 另一个程序正在使用此文件")
    return real_open(profile, read_only=read_only)


ws._open_session_db_for_profile = boom
try:
    broken = client.get(PREFIX + "/agents/overview")
    broken_body = broken.json()
finally:
    ws._open_session_db_for_profile = real_open

check("整页仍 200（不是 500）", broken.status_code == 200, str(broken.status_code))
broken_rows = broken_body.get("agents") or []
bad = [r for r in broken_rows if not r.get("available")]
good = [r for r in broken_rows if r.get("available")]
check("坏行恰好 1 行", len(bad) == 1, str([r.get("id") for r in bad]))
check("坏行是 xiaomo", bool(bad) and bad[0].get("id") == "xiaomo", str([r.get("id") for r in bad]))
check("坏行有 unavailable_reason",
      bool(bad) and "模拟" in str(bad[0].get("unavailable_reason")),
      str(bad[0].get("unavailable_reason")) if bad else "")
check("坏行数字是 null（不是 0）",
      bool(bad) and all(bad[0].get(k) is None for k in
                        ("sessions_total", "sessions_active", "last_active_at")),
      json.dumps({k: bad[0].get(k) for k in ("sessions_total", "last_active_at")}) if bad else "")
# cron 是另一个数据源（cron/jobs.json），**故意**不受 state.db 失败影响：
# 独立 try 的意义就是“坏一处只坏一处”。这里断言它仍然有值，而不是被连坐成 null。
check("坏行的 cron 不受 state.db 失败连坐（独立数据源仍取到）",
      bool(bad) and isinstance(bad[0].get("cron_total"), int),
      json.dumps({"cron_total": bad[0].get("cron_total")}) if bad else "")
check("其余 8 行照常可用", len(good) == 8, str(len(good)))
check("其余 8 行的 sessions_total 都拿到了",
      all(isinstance(r.get("sessions_total"), int) for r in good),
      str([(r.get("id"), r.get("sessions_total")) for r in good]))
check("坏行与正常行同时存在（对照）",
      bool(bad) and bool(good) and good[0].get("sessions_total") is not None)
print("        坏行 :", json.dumps(
    {k: bad[0].get(k) for k in ("id", "available", "unavailable_reason", "sessions_total", "cron_total")},
    ensure_ascii=False) if bad else "")
print("        好行 :", json.dumps(
    [(r.get("id"), r.get("sessions_total"), r.get("cron_enabled")) for r in good], ensure_ascii=False))

# ---------------------------------------------------------------------------
print("[4] 下钻：/agents/xiaomo/sessions 分页")
page1 = client.get(PREFIX + "/agents/xiaomo/sessions?limit=3")
page2 = client.get(PREFIX + "/agents/xiaomo/sessions?limit=3&offset=3")
for label, page in (("第 1 页", page1), ("第 2 页", page2)):
    data = (page.json().get("data") or {}) if page.status_code == 200 else {}
    ids = [s.get("id", "")[:8] for s in (data.get("sessions") or [])]
    print("        " + label + " -> " + str(page.status_code) + " 条数=" + str(len(data.get("sessions") or []))
          + " total=" + str(data.get("total")) + " limit=" + str(data.get("limit"))
          + " offset=" + str(data.get("offset")) + " ids=" + str(ids))
check("第 1 页 200 且 3 条", page1.status_code == 200
      and len((page1.json().get("data") or {}).get("sessions") or []) == 3,
      str(page1.status_code))
check("第 2 页 200 且 offset=3",
      page2.status_code == 200 and (page2.json().get("data") or {}).get("offset") == 3,
      str(page2.status_code))
check("两页 total 相同",
      (page1.json().get("data") or {}).get("total") == (page2.json().get("data") or {}).get("total"),
      str((page1.json().get("data") or {}).get("total")))
ids1 = {s.get("id") for s in (page1.json().get("data") or {}).get("sessions") or []}
ids2 = {s.get("id") for s in (page2.json().get("data") or {}).get("sessions") or []}
check("两页不重叠且都非空", bool(ids1) and bool(ids2) and not (ids1 & ids2),
      str(len(ids1 & ids2)))

print("[5] 下钻：/agents/xiaomo/cron")
cron = client.get(PREFIX + "/agents/xiaomo/cron?limit=5")
cron_data = (cron.json().get("data") or {}) if cron.status_code == 200 else {}
print("        -> " + str(cron.status_code) + " 条数=" + str(len(cron_data.get("jobs") or []))
      + " total=" + str(cron_data.get("total")) + " counts="
      + json.dumps(cron_data.get("counts"), ensure_ascii=False))
check("cron 200 且带 total/limit/offset",
      cron.status_code == 200 and isinstance(cron_data.get("total"), int)
      and cron_data.get("limit") == 5 and cron_data.get("offset") == 0,
      str(cron.status_code))
check("cron 分页条数 <= limit", len(cron_data.get("jobs") or []) <= 5)

print("[6] 未知 agent")
unknown = client.get(PREFIX + "/agents/no-such-agent/sessions?limit=3")
unknown_body = unknown.json() if unknown.status_code == 404 else {}
print("        -> " + str(unknown.status_code) + "  " + json.dumps(unknown_body, ensure_ascii=False)[:200])
check("未知 agent 404", unknown.status_code == 404, str(unknown.status_code))
check("404 是结构化错误", (unknown_body.get("error") or {}).get("code") == "unknown_agent")

print()
if failures:
    print("AGENTS-PROBE-FAIL", len(failures))
    for item in failures:
        print("  -", item)
    sys.exit(1)
print("AGENTS-PROBE-OK")

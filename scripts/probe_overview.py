# -*- coding: utf-8 -*-
"""hermes-console · 概览分区探针（真 HTTP，不 mock 取数函数）。

跑法（用带 fastapi 的 Hermes 环境；工作区根目录下）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        plugins/hermes-console/scripts/probe_overview.py

为什么需要 ``PYTHON_EXTRA``：``hermes_cli.web_routers.*`` 会间接 import 顶层模块
``hermes_yaml``（``hermes_cli/config.py:31``），它只在 hermes-agent 目录在 ``sys.path`` 上时
可解析。生产里后端就是从 hermes-agent 起的（``sys.path[0]`` 就是它），所以这里显式补上，
保证探针与生产同源。

覆盖：
1. 6 个路由逐个真打：状态码 + 响应前 200 字符 + ``ok``/``scope`` 结构；
2. ``/overview/system`` 与 ``/overview/cron`` 的完整 JSON 落盘 + 控制台打印；
3. ``layout.json`` 的 mtime / md5 前后一致（证明没被改写）；
4. 退化：把取数函数换成"import 失败"和"超时"，断言返回结构化错误而不是 500；
5. agent 作用域：default vs 非 default 的真实差异、不存在的 agent、空 agent（current_profile）；
6. 非法 PUT 布局 → 400 且文件不变。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import time
import traceback
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
API = DASHBOARD / "plugin_api.py"
PREFIX = "/api/plugins/hermes-console"
LAYOUT = PLUGIN_ROOT.parents[1] / "plugins" / "home-dashboard" / "layout.json"
OUT_DIR = Path(__file__).resolve().parent / "_probe_out"

# 生产同款 sys.path：hermes-agent 是后端进程的工作根
for candidate in (PLUGIN_ROOT.parents[1] / "hermes-agent",):
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label + (" — " + detail if detail else ""))
    if not ok:
        failures.append(label)


def fingerprint(path: Path) -> dict:
    try:
        stat = path.stat()
        digest = hashlib.md5(path.read_bytes()).hexdigest()
        return {"exists": True, "mtime": stat.st_mtime, "size": stat.st_size, "md5": digest}
    except OSError as exc:
        return {"exists": False, "error": str(exc)}


print("[0] 环境")
print("        python :", sys.version.split()[0])
print("        plugin :", str(PLUGIN_ROOT))
before = fingerprint(LAYOUT)
print("        layout :", before)

print("[1] 按 dashboard 的方式加载 plugin_api.py")
spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_hermes-console", API)
mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
sys.modules[spec.name] = mod  # type: ignore[union-attr]
spec.loader.exec_module(mod)  # type: ignore[union-attr]
paths = sorted({getattr(r, "path", "?") for r in mod.router.routes})
print("        routes :", paths)
for expected in (
    "/overview",
    "/overview/layout",
    "/overview/system",
    "/overview/analytics",
    "/overview/cron",
    "/overview/sessions",
    "/overview/status",
    "/overview/logs",
):
    check("routes 含 " + expected, expected in paths)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api_overview  # noqa: E402  (plugin_api 已按本目录 import 过同名模块)

app = FastAPI()
app.include_router(mod.router, prefix=PREFIX)
client = TestClient(app, raise_server_exceptions=False)

print("[2] 八个路由逐个真打（agent=default）")
CASES = [
    ("GET", "/overview/layout", None),
    ("PUT", "/overview/layout", {"layout": {"version": 99, "widgets": []}}),
    ("GET", "/overview/system", None),
    ("GET", "/overview/analytics", None),
    ("GET", "/overview/cron", None),
    ("GET", "/overview/sessions", None),
    ("GET", "/overview/status", None),
    ("GET", "/overview/logs", None),
]
responses = {}
elapsed: dict[str, float] = {}
for method, path, body in CASES:
    url = PREFIX + path + ("" if method == "PUT" else "?agent=default")
    _t0 = time.perf_counter()
    if method == "GET":
        resp = client.get(url)
    else:
        resp = client.put(url, json=body)
    elapsed[method + " " + path] = time.perf_counter() - _t0
    text = resp.text
    responses[method + " " + path] = resp
    print(
        "  ---- "
        + method
        + " "
        + path
        + " -> "
        + str(resp.status_code)
        + "  "
        + text[:200].replace("\n", " ")
    )
    check(method + " " + path + " 不是 5xx", resp.status_code < 500, str(resp.status_code))

# 逐个断言语义
layout_resp = responses["GET /overview/layout"]
check("GET layout 200", layout_resp.status_code == 200)
layout_body = layout_resp.json()
check("layout ok=true", layout_body.get("ok") is True)
check("layout 读到了挂件", bool((layout_body.get("data") or {}).get("layout")), str((layout_body.get("data") or {}).get("widget_count")))
check("layout scope.kind=shared_file", (layout_body.get("scope") or {}).get("kind") == "shared_file")

put_resp = responses["PUT /overview/layout"]
check("PUT 非法布局 400", put_resp.status_code == 400, str(put_resp.status_code))
put_body = put_resp.json()
check("PUT 错误结构 {ok:false,error{code,message}}",
      put_body.get("ok") is False
      and (put_body.get("error") or {}).get("code") == "invalid_layout"
      and bool((put_body.get("error") or {}).get("message")),
      json.dumps(put_body.get("error"), ensure_ascii=False))

system_resp = responses["GET /overview/system"]
check("GET system 200", system_resp.status_code == 200)
system_body = system_resp.json()
# 门禁判据（已收紧）：真实 home 上 ``hermes_cli.web_routers.status.get_system_stats`` 是可用的
# （它是 sync 函数，必须走 ``_await_thread``；塞进 ``_await_async`` 会得到
# ``TypeError: 'dict' object can't be awaited`` 并把整张卡打成 upstream_error）。
# 所以这里**不再把结构化降级当作合法契约放过**：真实 home 上 ok 必须是 True。
# 结构化降级（import_failed / timeout）只允许出现在 [5] 的模拟注入里 —— 那是契约测试，
# 不是真实环境验收。旧判据两条都认，导致 880 PASS 全绿而线上两张卡是坏的。
check("system ok=true（真实 home 必须取到宿主读数，不接受结构化降级）",
      system_body.get("ok") is True,
      "" if system_body.get("ok") is True
      else "ok=%r error=%s" % (system_body.get("ok"),
                               json.dumps(system_body.get("error"), ensure_ascii=False)[:200]))
check("system 带口径表", bool(system_body.get("calibers")),
      json.dumps(list((system_body.get("calibers") or {}).keys()), ensure_ascii=False))
check("system scope.kind=host（口径没有被降级搞丢）",
      (system_body.get("scope") or {}).get("kind") == "host",
      json.dumps(system_body.get("scope"), ensure_ascii=False)[:160])
system_data = system_body.get("data") or {}
check("system data 非空且是 dict", isinstance(system_data, dict) and bool(system_data),
      json.dumps(sorted(system_data.keys())[:12], ensure_ascii=False))

analytics_resp = responses["GET /overview/analytics"]
check("GET analytics 200", analytics_resp.status_code == 200)
analytics_body = analytics_resp.json()
analytics_elapsed = elapsed.get("GET /overview/analytics", -1.0)
# 同上：真实 home 上 ``get_usage_analytics`` 必须真的取到数。线上曾经的坏法是
# 插件超时预算（25s）大于桌面端硬上限（12s），前端只能拿到 "Timed out ... 12000ms"，
# 拿不到结构化错误 —— 所以这里必须 ok=true，且耗时留在桌面端上限以内。
check("analytics ok=true（真实 home 必须取到用量数据，不接受结构化降级）",
      analytics_body.get("ok") is True,
      "" if analytics_body.get("ok") is True
      else "ok=%r error=%s" % (analytics_body.get("ok"),
                               json.dumps(analytics_body.get("error"), ensure_ascii=False)[:200]))
check("analytics scope.kind=profile", (analytics_body.get("scope") or {}).get("kind") == "profile")
analytics_data = analytics_body.get("data") or {}
check("analytics data 有 totals/period_days/daily/by_model/by_task",
      isinstance(analytics_data.get("totals"), dict)
      and "period_days" in analytics_data
      and isinstance(analytics_data.get("daily"), list)
      and isinstance(analytics_data.get("by_model"), list)
      and isinstance(analytics_data.get("by_task"), list),
      json.dumps(sorted(analytics_data.keys()), ensure_ascii=False))
check("analytics 耗时 < 桌面端硬上限 12s（预算错配回归）",
      analytics_elapsed < 12.0, "%.2fs" % analytics_elapsed)
cron_resp = responses["GET /overview/cron"]
check("GET cron 200", cron_resp.status_code == 200)
cron_body = cron_resp.json()
check("cron ok=true", cron_body.get("ok") is True)
check("cron scope.kind=profile", (cron_body.get("scope") or {}).get("kind") == "profile")
counts = cron_body.get("counts") or {}
check("cron counts 自洽（enabled+paused<=total）",
      counts.get("enabled", 0) + counts.get("paused", 0) <= counts.get("total", 0) + 1,
      json.dumps(counts))

sessions_resp = responses["GET /overview/sessions"]
check("GET sessions 200", sessions_resp.status_code == 200)
sessions_body = sessions_resp.json()
check("sessions ok=true", sessions_body.get("ok") is True)
sessions_data = sessions_body.get("data") or {}
check("sessions 有 total", isinstance(sessions_data.get("total"), int), str(sessions_data.get("total")))
check("sessions 分页字段回显", sessions_data.get("limit") == 20 and sessions_data.get("offset") == 0,
      str((sessions_data.get("limit"), sessions_data.get("offset"))))

# ---- /overview/status：gateway / heartbeat 两个挂件的真数据源 ----
status_resp = responses["GET /overview/status"]
check("GET status 200", status_resp.status_code == 200)
status_body = status_resp.json()
check("status ok=true（真实 home 必须取到宿主 status，不接受结构化降级）",
      status_body.get("ok") is True,
      "ok=%r error=%s" % (status_body.get("ok"),
                          json.dumps(status_body.get("error"), ensure_ascii=False)[:200]))
check("status scope.kind=host（网关是宿主单实例，不按 agent 取数）",
      (status_body.get("scope") or {}).get("kind") == "host",
      json.dumps(status_body.get("scope"), ensure_ascii=False)[:160])
check("status 带口径表", bool(status_body.get("calibers")),
      json.dumps(list((status_body.get("calibers") or {}).keys()), ensure_ascii=False))
status_data = status_body.get("data") or {}
check("status 有 gateway_running 布尔值", isinstance(status_data.get("gateway_running"), bool),
      repr(status_data.get("gateway_running")))
check("status 有 version 字符串", isinstance(status_data.get("version"), str) and bool(status_data.get("version")),
      repr(status_data.get("version")))
check("status platforms 是列表", isinstance(status_data.get("platforms"), list),
      str(len(status_data.get("platforms") or [])) + " 个平台")
print("        gateway_running:", status_data.get("gateway_running"),
      "| pid:", status_data.get("gateway_pid"),
      "| state:", status_data.get("gateway_state"),
      "| updated:", status_data.get("gateway_updated_at"))

# ---- /overview/logs：errors 挂件的真数据源 ----
logs_resp = responses["GET /overview/logs"]
check("GET logs 200", logs_resp.status_code == 200)
logs_body = logs_resp.json()
check("logs ok=true（真实 home 必须取到日志尾窗口，不接受结构化降级）",
      logs_body.get("ok") is True,
      "ok=%r error=%s" % (logs_body.get("ok"),
                          json.dumps(logs_body.get("error"), ensure_ascii=False)[:200]))
logs_data = logs_body.get("data") or {}
log_counts = logs_data.get("counts") or {}
check("logs 有 counts 且含 ERROR 键", isinstance(log_counts.get("ERROR"), int),
      json.dumps(log_counts, ensure_ascii=False))
check("logs 有 unlabeled 键（traceback 续行单独计，不并进 ERROR）",
      isinstance(log_counts.get("unlabeled"), int), json.dumps(log_counts, ensure_ascii=False))
check("logs scanned == len(lines)（计数口径与返回行数一致）",
      logs_data.get("scanned") == len(logs_data.get("lines") or []),
      "%r vs %r" % (logs_data.get("scanned"), len(logs_data.get("lines") or [])))
check("logs 计数之和 == scanned（没有丢行）",
      sum(v for k, v in log_counts.items() if isinstance(v, int)) == (logs_data.get("scanned") or 0),
      json.dumps(log_counts, ensure_ascii=False) + " sum=" +
      str(sum(v for k, v in log_counts.items() if isinstance(v, int))))
check("logs 带 file/path（说得出数据来自哪个文件）",
      bool(logs_data.get("file")) and bool(logs_data.get("path")),
      str(logs_data.get("file")) + " · " + str(logs_data.get("path")))
print("        file:", logs_data.get("file"), "| scanned:", logs_data.get("scanned"),
      "| counts:", json.dumps(log_counts, ensure_ascii=False))

# 非法入参必须是 400 结构化错误（不是 500，也不是静默返回空）
bad_file = client.get(PREFIX + "/overview/logs?file=../config.yaml")
check("logs 非白名单文件名 → 400 invalid_argument",
      bad_file.status_code == 400
      and (bad_file.json().get("error") or {}).get("code") == "invalid_argument",
      str(bad_file.status_code))
bad_level = client.get(PREFIX + "/overview/logs?level=NOPE")
check("logs 非法级别 → 400 invalid_argument",
      bad_level.status_code == 400
      and (bad_level.json().get("error") or {}).get("code") == "invalid_argument",
      str(bad_level.status_code))

print("[3] 完整 JSON 落盘（system / cron）")
OUT_DIR.mkdir(parents=True, exist_ok=True)
(OUT_DIR / "system.json").write_text(json.dumps(system_body, ensure_ascii=False, indent=2), encoding="utf-8")
(OUT_DIR / "cron.json").write_text(json.dumps(cron_body, ensure_ascii=False, indent=2), encoding="utf-8")
(OUT_DIR / "status.json").write_text(json.dumps(status_body, ensure_ascii=False, indent=2), encoding="utf-8")
(OUT_DIR / "logs.json").write_text(json.dumps(logs_body, ensure_ascii=False, indent=2), encoding="utf-8")
print("        system.json :", (OUT_DIR / "system.json").stat().st_size, "B")
print("        cron.json   :", (OUT_DIR / "cron.json").stat().st_size, "B")
print("        status.json :", (OUT_DIR / "status.json").stat().st_size, "B")
print("        logs.json   :", (OUT_DIR / "logs.json").stat().st_size, "B")

print("[4] agent 作用域真实性")
cron_all = client.get(PREFIX + "/overview/cron?agent=all")
check("agent=all 200 且 scope.kind=all", cron_all.status_code == 200
      and (cron_all.json().get("scope") or {}).get("kind") == "all", str(cron_all.status_code))
all_counts = (cron_all.json().get("counts") or {})
print("        agent=default total:", counts.get("total"), "| agent=all total:", all_counts.get("total"))
check("agent=all 的任务数 >= 单 profile 的任务数",
      (all_counts.get("total") or 0) >= (counts.get("total") or 0),
      str(all_counts))

agents_resp = client.get(PREFIX + "/agents")
agent_ids = [a["id"] for a in (agents_resp.json().get("agents") or [])]
other = next((a for a in agent_ids if a not in ("default", "code-worker")), "")
print("        agents :", agent_ids)
if other:
    other_cron = client.get(PREFIX + "/overview/cron?agent=" + other).json()
    other_sessions = client.get(PREFIX + "/overview/sessions?agent=" + other).json()
    check("非 default agent 的 cron 真的按该 profile 取（scope.agent 回显）",
          (other_cron.get("scope") or {}).get("agent") == other, other)
    check("非 default agent 的 sessions 真的按该 profile 取（scope.agent 回显）",
          (other_sessions.get("scope") or {}).get("agent") == other, other)
    print("        " + other + " cron total:", (other_cron.get("counts") or {}).get("total"),
          "| sessions total:", (other_sessions.get("data") or {}).get("total"),
          "| default sessions total:", sessions_data.get("total"))

missing = client.get(PREFIX + "/overview/cron?agent=__no_such_profile__")
missing_body = missing.json()
check("不存在的 agent → 结构化 unknown_agent（不冒充 default）",
      missing_body.get("ok") is False
      and (missing_body.get("error") or {}).get("code") in ("unknown_agent", "invalid_agent"),
      json.dumps(missing_body.get("error"), ensure_ascii=False))
check("不存在的 agent 不是 5xx", missing.status_code < 500, str(missing.status_code))

blank = client.get(PREFIX + "/overview/sessions?agent=")
blank_body = blank.json()
check("空 agent → scope.kind=current_profile（明说，不冒充）",
      (blank_body.get("scope") or {}).get("kind") == "current_profile",
      json.dumps(blank_body.get("scope"), ensure_ascii=False))

all_analytics = client.get(PREFIX + "/overview/analytics?agent=all")
check("analytics agent=all → 400 unsupported_agent",
      all_analytics.status_code == 400
      and (all_analytics.json().get("error") or {}).get("code") == "unsupported_agent",
      str(all_analytics.status_code))

print("[5] 退化：取数函数 import 失败 / 超时（不 500、不裸抛）")
real_load = api_overview._load
real_await_async = api_overview._await_async
real_await_thread = api_overview._await_thread


def broken_load(module_name, attr):
    raise ImportError("simulated: cannot import name '" + attr + "' from '" + module_name + "'")


def broken_timeout(*_a, **_k):
    raise __import__("asyncio").TimeoutError()


try:
    api_overview._load = broken_load
    for path in ("/overview/system", "/overview/analytics", "/overview/cron", "/overview/sessions",
                 "/overview/status", "/overview/logs"):
        resp = client.get(PREFIX + path + "?agent=default")
        body = resp.json()
        print("  ---- " + path + " (import 失败) -> " + str(resp.status_code) + "  " + resp.text[:200])
        check(path + " import 失败 → 200 + ok:false/import_failed",
              resp.status_code == 200
              and body.get("ok") is False
              and (body.get("error") or {}).get("code") == "import_failed",
              json.dumps(body.get("error"), ensure_ascii=False))
finally:
    api_overview._load = real_load

try:
    api_overview._await_async = broken_timeout
    api_overview._await_thread = broken_timeout
    resp = client.get(PREFIX + "/overview/system?agent=default")
    body = resp.json()
    print("  ---- /overview/system (超时) -> " + str(resp.status_code) + "  " + resp.text[:200])
    check("超时 → 200 + ok:false/timeout",
          resp.status_code == 200 and (body.get("error") or {}).get("code") == "timeout",
          json.dumps(body.get("error"), ensure_ascii=False))
finally:
    api_overview._await_async = real_await_async
    api_overview._await_thread = real_await_thread

print("[6] 布局文件未被改写（mtime + md5）")
after = fingerprint(LAYOUT)
print("        before :", before)
print("        after  :", after)
check("layout.json 存在", after.get("exists") is True)
check("layout.json mtime 未变", before.get("mtime") == after.get("mtime"),
      str(before.get("mtime")) + " vs " + str(after.get("mtime")))
check("layout.json md5 未变", before.get("md5") == after.get("md5"),
      str(before.get("md5")) + " vs " + str(after.get("md5")))
check("没有留下 .tmp 残留", not (LAYOUT.parent / (LAYOUT.name + ".tmp")).exists())

print("[7] 分区自检路由")
selfcheck = client.get(PREFIX + "/overview?agent=default")
selfcheck_body = selfcheck.json()
print("  ---- GET /overview -> " + str(selfcheck.status_code) + "  " + selfcheck.text[:200])
deps = ((selfcheck_body.get("data") or {}).get("deps") or {})
check("自检 200", selfcheck.status_code == 200)
check("自检 6 个宿主接口全部 ok", all((d or {}).get("ok") for d in deps.values()) and len(deps) == 6,
      json.dumps({k: (v or {}).get("ok") for k, v in deps.items()}))

print()
if failures:
    print("OVERVIEW-PROBE-FAIL (" + str(len(failures)) + "): " + ", ".join(failures))
    raise SystemExit(1)
print("OVERVIEW-PROBE-OK")

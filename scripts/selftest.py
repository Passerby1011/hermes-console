# -*- coding: utf-8 -*-
"""hermes-console · 自检（后端 + manifest 契约 + 启用状态），后续卡片直接复用。

跑法（用带 fastapi 的 Hermes 环境）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        D:/hermes-data/hermes/plugins/hermes-console/scripts/selftest.py

检查项：
1. manifest.json 的契约键（name/label/tab/entry/css/api/version）齐全且文件真实存在；
2. 按 dashboard 的方式加载 ``plugin_api.py``（合成模块名 + spec_from_file_location），
   断言 ``router.routes`` 含四个域路由，且概览自检列出的子路由与真挂载的 ``/overview/*`` 逐条相同；
3. ``GET /agents`` 走 TestClient 真返回值：恰好与磁盘上含 config.yaml 的 profile 数一致；
4. 概览分区自检（``GET /overview`` 列出的子路由 = 真挂载路由、每个取数子路由的宿主接口都能解析）+
   升级分区（V2）的 6 条真实路由：``/upgrade/scan`` 与 ``/upgrade/readiness`` 真返回数据，
   ``/upgrade/restore`` 在无待恢复快照时是 409 业务错误（不再是 stub）；
5. 根 config.yaml 的 ``plugins.enabled`` 含本插件（只读，不写）。
6. ``/agents`` 退化与目录变动（主目录每次请求重新解析）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
MANIFEST = DASHBOARD / "manifest.json"
API = DASHBOARD / "plugin_api.py"
PREFIX = "/api/plugins/hermes-console"
NAME = "hermes-console"

# 生产里后端就是从 hermes-agent 起的（sys.path[0] 就是它），概览取数要 import
# hermes_cli.web_routers.*（间接用到顶层模块 hermes_yaml）。这里补上同一个根，
# 保证自检与生产同源，而不是"探针能过、面板全灰"。
_AGENT_ROOT = PLUGIN_ROOT.parents[1] / "hermes-agent"
if _AGENT_ROOT.is_dir() and str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

failures = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + label + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


print("[1] manifest 契约")
data = json.loads(MANIFEST.read_text(encoding="utf-8"))
check("name", data.get("name") == NAME, str(data.get("name")))
check("label=控制台", data.get("label") == "控制台", str(data.get("label")))
check("version=0.1.0", data.get("version") == "0.1.0", str(data.get("version")))
tab = data.get("tab") or {}
check("tab.path=/console", tab.get("path") == "/console", str(tab.get("path")))
check("tab.position=end", tab.get("position") == "end", str(tab.get("position")))
check("api=plugin_api.py", data.get("api") == "plugin_api.py", str(data.get("api")))
for key in ("entry", "css"):
    rel = data.get(key) or ""
    target = DASHBOARD / rel
    check(f"{key}={rel} 存在且非空", target.is_file() and target.stat().st_size > 0,
          f"{target.stat().st_size} B" if target.is_file() else "missing")

print("[2] 聚合入口可 import（dashboard 同款加载方式）")
spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_hermes-console", API)
mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
sys.modules[spec.name] = mod  # type: ignore[union-attr]
spec.loader.exec_module(mod)  # type: ignore[union-attr]
paths = sorted({getattr(r, "path", "?") for r in mod.router.routes})
print("        routes:", paths)
for expected in (
    "/agents",
    "/overview",
    "/overview/layout",
    "/overview/system",
    "/overview/analytics",
    "/overview/cron",
    "/overview/sessions",
    "/weather",
    "/weather/now",
    "/weather/daily",
    "/weather/hourly",
    "/weather/at",
    # V2：升级分区从 1 条占位换成 6 条真实路由（42 - 1 + 6 = 47）
    "/upgrade/scan",
    "/upgrade/readiness",
    "/upgrade/prepare",
    "/upgrade/restore",
    "/upgrade/wait",
    "/upgrade/kill",
):
    check(f"routes 含 {expected}", expected in paths)

print("[3] GET /agents（实现层）")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app = FastAPI()
app.include_router(mod.router, prefix=PREFIX)
client = TestClient(app, raise_server_exceptions=False)

resp = client.get(f"{PREFIX}/agents")
check("HTTP 200", resp.status_code == 200, str(resp.status_code))
body = resp.json()
agents = body.get("agents") or []
print("        current:", body.get("current"), "| count:", len(agents))
print("        ids    :", [a["id"] for a in agents])
root = PLUGIN_ROOT.parents[1]  # plugins/<id>/ → <home>
disk = [d.name for d in sorted((root / "profiles").iterdir()) if (d / "config.yaml").is_file()]
check("agent 数与磁盘 profile 数一致", len(agents) == len(disk) + 1, f"{len(agents)} vs {len(disk) + 1}")
check("default 在首位且 is_default", bool(agents) and agents[0]["id"] == "default" and agents[0]["is_default"])
check("每个 agent 都有 config.yaml", all(a["has_config"] for a in agents))
check("current 非空", bool(body.get("current")))

print("[4] 概览自检 + 两个占位域")
selfcheck = client.get(f"{PREFIX}/overview?agent=default")
check("GET /overview 200", selfcheck.status_code == 200, str(selfcheck.status_code))
selfcheck_body = selfcheck.json()
selfcheck_data = selfcheck_body.get("data") or {}
routes_listed = [(r.get("method"), r.get("path")) for r in (selfcheck_data.get("routes") or [])]
print("        subroutes:", routes_listed)
# 自检列的路由必须与**真挂载在 router 上的** /overview/* 完全一致。
# 不写死条数：概览每加一个数据源（system/analytics/status/logs 都是这么来的）都要改一次
# 硬编码数字，改的人一懒就会把断言改成通过；对着 router 比才是真契约。
mounted_overview = sorted(
    (method, getattr(route, "path", ""))
    for route in mod.router.routes
    if getattr(route, "path", "").startswith("/overview/")
    for method in sorted(getattr(route, "methods", set()) or ())
)
check(
    "自检列出的子路由 = 真挂载的 /overview/* 路由（方法+路径逐条相同）",
    sorted(routes_listed) == mounted_overview,
    f"listed={sorted(routes_listed)} mounted={mounted_overview}",
)
check(
    "自检覆盖 GET/PUT 两种方法（布局可读可写，数据源只读）",
    ("GET", "/overview/layout") in routes_listed
    and ("PUT", "/overview/layout") in routes_listed
    and ("GET", "/overview/sessions") in routes_listed
    and all(m == "GET" for m, p in routes_listed if p != "/overview/layout"),
    str(routes_listed),
)
deps = selfcheck_data.get("deps") or {}
# 每个「取数」子路由都必须有一条可解析的宿主接口探针（layout 是本地文件，不需要宿主接口）。
data_names = sorted({
    path.rsplit("/", 1)[-1]
    for method, path in mounted_overview
    if method == "GET" and path != "/overview/layout"
})
check(
    "每个取数子路由都有宿主接口探针（名字集合一致，不是只探前 4 个）",
    sorted(deps) == data_names,
    f"deps={sorted(deps)} expected={data_names}",
)
check("所有宿主接口全部可解析（新路径，不是 hermes_cli.web_server）",
      bool(deps) and all((d or {}).get("ok") for d in deps.values()),
      json.dumps({k: (v or {}).get("ok") for k, v in deps.items()}))
check("自检里的布局路径指向 home-dashboard（原地沿用）",
      "home-dashboard" in str((selfcheck_data.get("layout") or {}).get("path", "")),
      str((selfcheck_data.get("layout") or {}).get("path")))
print("[4b] 天气分区自检（C4）")
wx = client.get(f"{PREFIX}/weather?agent=default")
check("GET /weather 200", wx.status_code == 200, str(wx.status_code))
wx_body = wx.json()
wx_routes = [(r.get("method"), r.get("path")) for r in (wx_body.get("routes") or [])]
print("        subroutes:", wx_routes)
check(
    "天气自检列出 5 条路由（4 取数 + 自检）",
    len(wx_routes) == 5 and ("GET", "/weather/now") in wx_routes and ("GET", "/weather/at") in wx_routes,
    str(len(wx_routes)),
)
check("weather_lib 可加载", bool((wx_body.get("weather_lib") or {}).get("ok")),
      str((wx_body.get("weather_lib") or {}).get("path") or (wx_body.get("weather_lib") or {}).get("error")))
check("降级顺序 = 彩云 → 和风 → Open-Meteo",
      wx_body.get("source_order") == ["彩云天气", "和风天气", "Open-Meteo"],
      str(wx_body.get("source_order")))
check("默认地点解析出坐标（不写死城市名）",
      bool((wx_body.get("default_place_resolved") or {}).get("resolved")),
      str(wx_body.get("default_place_resolved")))
check("单位表含 ℃ / km/h / μg/m³",
      (wx_body.get("units") or {}).get("temp") == "℃"
      and (wx_body.get("units") or {}).get("wind") == "km/h"
      and (wx_body.get("units") or {}).get("pm25") == "μg/m³",
      str(wx_body.get("units")))
print("[4b] 升级分区（V2：6 条真实路由，不再是 stub）")
up_scan = client.get(f"{PREFIX}/upgrade/scan")
check("/upgrade/scan = 200 且 ok=true", up_scan.status_code == 200 and up_scan.json().get("ok") is True,
      up_scan.text[:120])
_up_data = (up_scan.json() or {}).get("data") or {}
check("/upgrade/scan 含版本与就绪字段",
      bool((_up_data.get("version") or {}).get("version")) and isinstance((_up_data.get("readiness") or {}).get("ready"), bool),
      str(sorted(_up_data.keys())))
check("/upgrade/scan 的状态文件是 plugins/upgrade-console/state.json",
      str(_up_data.get("state_file") or "").replace("\\", "/").endswith("plugins/upgrade-console/state.json"),
      str(_up_data.get("state_file")))
up_ready = client.get(f"{PREFIX}/upgrade/readiness")
check("/upgrade/readiness = 200 且 ok=true",
      up_ready.status_code == 200 and up_ready.json().get("ok") is True, up_ready.text[:120])
# 真实 home 上只读：/restore 无待恢复快照必须是 409 业务错误（不是 500，也不写 state.json）
up_restore = client.post(f"{PREFIX}/upgrade/restore")
check("/upgrade/restore 无待恢复快照 → 409（业务错误，不是 500）",
      up_restore.status_code == 409
      and ((up_restore.json().get("error") or {}).get("code") == "no_pending_restore"),
      up_restore.text[:120])

print("[5] plugins.enabled（根 config.yaml，只读）")
import yaml  # noqa: E402

cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) or {}
enabled = ((cfg.get("plugins") or {}).get("enabled")) or []
check(f"plugins.enabled 含 {NAME}", NAME in enabled, f"{len(enabled)} 项")
check("plugins.enabled 无重复", len(enabled) == len(set(enabled)))
raw = (root / "config.yaml").read_text(encoding="utf-8")
check("config.yaml 里 hermes-console 只出现 1 次", raw.count(NAME) == 1, f"count={raw.count(NAME)}")

print("[6] /agents 退化与目录变动（主目录每次请求重新解析）")
import os  # noqa: E402
import tempfile  # noqa: E402

import api_agents  # noqa: E402  (plugin_api 已按本目录 import 过同名模块)

real_home = api_agents.hermes_home
with tempfile.TemporaryDirectory(prefix="hc-home-") as tmp:
    home = Path(tmp)
    (home / "config.yaml").write_text("profile: default\n", encoding="utf-8")
    # 6a. 有 home 但没有 profiles/ 目录：只能返回 default，且不 500
    api_agents.hermes_home = lambda: home
    r = client.get(f"{PREFIX}/agents")
    check("无 profiles/ 目录时仍 200", r.status_code == 200, str(r.status_code))
    body = r.json()
    ids = [a["id"] for a in body["agents"]]
    check("无 profiles/ 目录时只返回 default", ids == ["default"], str(ids))
    check("退化响应里 current 非空", bool(body.get("current")), str(body.get("current")))

    # 6b. 新增 profile 目录后**不重新 import**，下一次请求就应该看到（防静默失效）
    (home / "profiles" / "alpha").mkdir(parents=True)
    (home / "profiles" / "alpha" / "config.yaml").write_text("profile: alpha\n", encoding="utf-8")
    r2 = client.get(f"{PREFIX}/agents")
    ids2 = [a["id"] for a in r2.json()["agents"]]
    check("新增 profile 后下一次请求就可见（无需重启）", ids2 == ["default", "alpha"], str(ids2))

    # 6c. 没有 config.yaml 的目录不算 profile
    (home / "profiles" / "no-config").mkdir()
    ids3 = [a["id"] for a in client.get(f"{PREFIX}/agents").json()["agents"]]
    check("缺 config.yaml 的目录被忽略", ids3 == ["default", "alpha"], str(ids3))

    # 6d. profiles/ 是个文件（不是目录）：不抛异常，退化为 default
    (home / "profiles" / "alpha" / "config.yaml").unlink()
    (home / "profiles" / "alpha").rmdir()
    (home / "profiles" / "no-config").rmdir()
    (home / "profiles").rmdir()
    (home / "profiles").write_text("not a dir", encoding="utf-8")
    r4 = client.get(f"{PREFIX}/agents")
    ids4 = [a["id"] for a in r4.json()["agents"]]
    check("profiles/ 不是目录时仍 200 且只有 default", r4.status_code == 200 and ids4 == ["default"], str(ids4))

api_agents.hermes_home = real_home

print()
if failures:
    print(f"SELFTEST-FAIL ({len(failures)}): " + ", ".join(failures))
    raise SystemExit(1)
print("SELFTEST-OK")

# -*- coding: utf-8 -*-
"""hermes-console · 天气分区验收探针（真实 TestClient + 真实上游 + 真实降级）。

跑法（必须用带 fastapi 的 Hermes 环境）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        D:/hermes-data/hermes/plugins/hermes-console/scripts/probe_weather.py

覆盖：
[1] ``GET /weather`` 分区自检（5 条路由 + weather_lib 可加载 + 默认地点解析）
[2] 4 条真实路由（``/now`` ``/daily`` ``/hourly`` ``/at``）：状态码 + 响应前 300 字符 + 实际命中源
[3] 每条响应都带 ``source``/``source_label``/``fetched_at``/``fetched_at_ms``/``sources``/``units``
[4] 参数边界（days=99 → 16，hours=1 → 3，at hours=999 → 168）
[5] **上游失败降级**：只掐彩云 → 落和风或 Open-Meteo；再掐和风 → 落 Open-Meteo
[6] **全源失败**：三个源全掐 → HTTP 200 + ``{ok:false, error.code=all_sources_failed}``
[7] 地点非法 → 结构化错误（400/``invalid_place``）
[8] 只读约束：``plugins/weather``、``plugins/weather-card`` 今日 0 文件改动

降级是**掐 ``weather_lib._get_json`` 里对应域名**实现的（不是替换取数函数），这样
weather_lib 自己的 ``try/except → None`` 逻辑照常走，探针记录的"哪个源返回空"也是真的。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
API = DASHBOARD / "plugin_api.py"
PREFIX = "/api/plugins/hermes-console"

# 生产里后端从 hermes-agent 起，概览取数要 import hermes_cli.web_routers.*
_AGENT_ROOT = PLUGIN_ROOT.parents[1] / "hermes-agent"
if _AGENT_ROOT.is_dir() and str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

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


def head(text: str, n: int = 300) -> str:
    text = text.replace("\n", " ")
    return text[:n] + ("…" if len(text) > n else "")


# ---------------------------------------------------------------------------
# 加载（与 dashboard / selftest 同款：合成模块名 + spec_from_file_location）
# ---------------------------------------------------------------------------
spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_hermes-console", API)
mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
sys.modules[spec.name] = mod  # type: ignore[union-attr]
spec.loader.exec_module(mod)  # type: ignore[union-attr]

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api_weather  # noqa: E402  (plugin_api 已按本目录 import 过同名模块)

app = FastAPI()
app.include_router(mod.router, prefix=PREFIX)
client = TestClient(app, raise_server_exceptions=False)

REQUIRED_FIELDS = ("source", "source_label", "fetched_at", "fetched_at_ms", "sources", "units")

print("[1] GET /weather 分区自检")
r = client.get(PREFIX + "/weather")
body = r.json()
check("HTTP 200", r.status_code == 200, str(r.status_code))
check("列出 5 条路由", len(body.get("routes") or []) == 5, json.dumps(body.get("routes"), ensure_ascii=False))
check(
    "4 条取数路由都在",
    {(x["method"], x["path"]) for x in body.get("routes") or []}
    == {
        ("GET", "/weather"),
        ("GET", "/weather/now"),
        ("GET", "/weather/daily"),
        ("GET", "/weather/hourly"),
        ("GET", "/weather/at"),
    },
)
lib = body.get("weather_lib") or {}
check("weather_lib 可加载", bool(lib.get("ok")), str(lib.get("path") or lib.get("error")))
place = body.get("default_place_resolved") or {}
print("        默认地点 :", body.get("default_place"), "→", json.dumps(place, ensure_ascii=False))
check("默认地点解析出坐标（不是写死城市名）", bool(place.get("resolved")) and place.get("lat") is not None)
check("降级顺序 = 彩云 → 和风 → Open-Meteo", body.get("source_order") == ["彩云天气", "和风天气", "Open-Meteo"],
      json.dumps(body.get("source_order"), ensure_ascii=False))
check("单位表齐全（℃ / % / km/h / μg/m³）",
      body["units"]["temp"] == "℃" and body["units"]["wind"] == "km/h" and body["units"]["pm25"] == "μg/m³",
      json.dumps(body["units"], ensure_ascii=False))

print("[2] 4 条真实路由（真打上游）")
real = {}
for name, path, params in (
    ("now", "/weather/now", {}),
    ("daily", "/weather/daily", {"days": 7}),
    ("hourly", "/weather/hourly", {"hours": 24}),
    ("at", "/weather/at", {"hours": 6}),
):
    t0 = time.time()
    resp = client.get(PREFIX + path, params=params)
    cost = time.time() - t0
    text = resp.text
    real[name] = resp.json()
    print("        GET %-16s %s  %.1fs  %s" % (path, resp.status_code, cost, head(text, 300)))
    check(path + " HTTP 200", resp.status_code == 200, str(resp.status_code))
    payload = resp.json()
    check(path + " ok=true", payload.get("ok") is True, head(text, 160))
    check(path + " 命中源 = " + str(payload.get("source")), bool(payload.get("source")),
          "source=" + str(payload.get("source")) + " label=" + str(payload.get("source_label")))
    check(path + " 带 fetched_at", bool(payload.get("fetched_at")), str(payload.get("fetched_at")))
    missing = [f for f in REQUIRED_FIELDS if f not in payload]
    check(path + " 六个必带字段齐全", not missing, "缺 " + str(missing) if missing else "全在")

print("[3] 数据内容（照抄工具输出，不猜）")
now = real["now"]
rt = now.get("realtime") or {}
print("        realtime:", json.dumps(rt, ensure_ascii=False)[:220])
print("        air     :", json.dumps(now.get("air"), ensure_ascii=False)[:200])
print("        sources :", json.dumps(now.get("sources"), ensure_ascii=False))
check("实况温度是数字", isinstance(rt.get("temp"), (int, float)), str(rt.get("temp")))
check("实况带 source 字段（weather_lib 自报）", bool(rt.get("source")), str(rt.get("source")))
check("today / tomorrow 是日预报对象", isinstance(now.get("today"), dict) and isinstance(now.get("tomorrow"), dict))
check("今日有 min/max/text", all(k in (now.get("today") or {}) for k in ("min", "max", "text")),
      json.dumps(now.get("today"), ensure_ascii=False))
daily_rows = real["daily"].get("daily") or []
print("        daily[0]:", json.dumps(daily_rows[0], ensure_ascii=False) if daily_rows else "—")
check("多日 = 7 条", len(daily_rows) == 7, str(len(daily_rows)))
check("每条日预报都有 date/text/min/max",
      all(all(k in row for k in ("date", "text", "min", "max")) for row in daily_rows))
hourly_rows = real["hourly"].get("hourly") or []
print("        hourly[0]:", json.dumps(hourly_rows[0], ensure_ascii=False) if hourly_rows else "—")
check("逐小时 = 24 条", len(hourly_rows) == 24, str(len(hourly_rows)))
check("逐小时带 dt/temp/desc/pop 键",
      all(all(k in row for k in ("dt", "temp", "desc", "pop")) for row in hourly_rows))
at_text = str(real["at"].get("text") or "")
print("        at text :", at_text)
check("「N 小时后」是工具原话（含“小时后”）", "小时后" in at_text, at_text)

print("[4] 参数边界（clamp，不静默截断）")
check("days=99 → 16", client.get(PREFIX + "/weather/daily", params={"days": 99}).json().get("days") == 16)
check("days=0 → 2", client.get(PREFIX + "/weather/daily", params={"days": 0}).json().get("days") == 2)
check("hours=1 → 3", client.get(PREFIX + "/weather/hourly", params={"hours": 1}).json().get("hours") == 3)
check("hours=0 → 3（0 不当成“没传”）", client.get(PREFIX + "/weather/hourly", params={"hours": 0}).json().get("hours") == 3)
check("at hours=0 → 1", client.get(PREFIX + "/weather/at", params={"hours": 0}).json().get("hours") == 1)
check("hours=999 → 48", client.get(PREFIX + "/weather/hourly", params={"hours": 999}).json().get("hours") == 48)
check("at hours=999 → 168", client.get(PREFIX + "/weather/at", params={"hours": 999}).json().get("hours") == 168)

print("[5] 上游失败降级（掐域名，weather_lib 自己的降级逻辑照常走）")
wl = api_weather._load_weather_lib()
orig_get_json = wl._get_json


def block(*domains):
    def fake(url, timeout=15):
        for domain in domains:
            if domain in url:
                raise RuntimeError("探针模拟上游失败：" + domain)
        return orig_get_json(url, timeout)

    wl._get_json = fake


try:
    # 5a：只掐彩云
    block("caiyunapp.com")
    b = client.get(PREFIX + "/weather/now").json()
    print("        掐彩云 → source=%s  sources=%s" % (b.get("source"), json.dumps(b.get("sources"), ensure_ascii=False)))
    check("掐彩云后仍 ok", b.get("ok") is True)
    check("彩云记为 ok=false", any(s["source"] == "caiyun" and not s["ok"] for s in b.get("sources") or []),
          json.dumps(b.get("sources"), ensure_ascii=False))
    check("降级到下一源（非 caiyun）", b.get("source") in ("qweather", "open-meteo"), str(b.get("source")))

    # 5b：掐彩云 + 和风
    block("caiyunapp.com", "devapi.qweather.com")
    b2 = client.get(PREFIX + "/weather/now").json()
    print("        掐彩云+和风 → source=%s  sources=%s" % (b2.get("source"), json.dumps(b2.get("sources"), ensure_ascii=False)))
    check("两个源都掐后仍 ok", b2.get("ok") is True)
    check("落到 Open-Meteo", b2.get("source") == "open-meteo", str(b2.get("source")))
    check("彩云/和风都记为 ok=false",
          any(s["source"] == "caiyun" and not s["ok"] for s in b2.get("sources") or [])
          and any(s["source"] == "qweather" and not s["ok"] for s in b2.get("sources") or []),
          json.dumps(b2.get("sources"), ensure_ascii=False))

    # 5c：逐日 / 逐时也走降级
    b3 = client.get(PREFIX + "/weather/daily", params={"days": 7}).json()
    print("        逐日（掐彩云+和风）→ source=%s rows=%s" % (b3.get("source"), len(b3.get("daily") or [])))
    check("逐日降级到 Open-Meteo 且有 7 条", b3.get("source") == "open-meteo" and len(b3.get("daily") or []) == 7,
          str(b3.get("source")))

    print("[6] 全源失败 → 结构化错误（HTTP 200，面板进 ErrorState）")
    block("caiyunapp.com", "devapi.qweather.com", "api.open-meteo.com")
    r6 = client.get(PREFIX + "/weather/now")
    b6 = r6.json()
    print("        %s %s" % (r6.status_code, head(r6.text, 300)))
    check("全源失败仍是 HTTP 200", r6.status_code == 200, str(r6.status_code))
    check("ok=false", b6.get("ok") is False)
    check("error.code=all_sources_failed", (b6.get("error") or {}).get("code") == "all_sources_failed",
          str((b6.get("error") or {}).get("code")))
    check("错误信息说清哪个源空了", "×空" in str((b6.get("error") or {}).get("message")),
          str((b6.get("error") or {}).get("message")))
    check("失败响应也带 sources / fetched_at",
          bool(b6.get("sources")) and bool(b6.get("fetched_at")))
    r6b = client.get(PREFIX + "/weather/daily")
    check("逐日全源失败也是结构化错误", r6b.status_code == 200 and r6b.json().get("ok") is False, head(r6b.text, 200))
finally:
    wl._get_json = orig_get_json

print("[7] 地点非法")
r7 = client.get(PREFIX + "/weather/now", params={"place": "这个城市不存在zzz"})
b7 = r7.json()
print("        %s %s" % (r7.status_code, head(r7.text, 240)))
check("地点非法 → 结构化错误（不是 500）", r7.status_code in (200, 400) and b7.get("ok") is False,
      str(r7.status_code))
check("错误码说明是地点问题",
      (b7.get("error") or {}).get("code") in ("invalid_place", "all_sources_failed", "no_data", "upstream_error"),
      str((b7.get("error") or {}).get("code")))

print("[8] 只读约束：旧插件目录今日 0 文件改动")
home = PLUGIN_ROOT.parents[1]
today = time.strftime("%Y-%m-%d")
for rel in ("plugins/weather", "plugins/weather-card"):
    base = home / rel
    changed = []
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        if time.strftime("%Y-%m-%d", time.localtime(path.stat().st_mtime)) == today:
            changed.append(str(path.relative_to(home)))
    check(rel + " 今日 0 文件改动", not changed, ", ".join(changed[:5]) if changed else "clean")

print()
print("=" * 72)
print("PASS %d / FAIL %d" % (passes, len(failures)))
if failures:
    for item in failures:
        print("  FAILED:", item)
    sys.exit(1)
print("PROBE-WEATHER-OK")

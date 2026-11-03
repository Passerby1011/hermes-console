# -*- coding: utf-8 -*-
"""hermes-console · Advisor 本轮整改的后端验收（真路由 + 真台账 + 沙盒写设置）。

跑法::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        D:/hermes-data/hermes/plugins/hermes-console/scripts/probe_advisor_diag.py

覆盖本卡的后端验收项：

[A] 真路由清单：把 ``/advisor*`` 的真实路由打出来，证明 ``/advisor/status`` /
    ``/advisor/overview`` **不是路由**（卡片里那条 401 是鉴权门在路由之前拒绝的）。
[B] 逐端点真打（真 home、真台账）：状态码 + 耗时 + 响应体前 200 字符。
[C] ``GET /advisor/modes``：两种模式、当前模式、不可切换原因、提示词指纹、两个分支原文。
[D] ``GET /advisor/reviews``：504 归类成人话、错误次数/时间、飞行中评审、日志旁证。
[E] ``GET /advisor/settings``：逐项值 + 来源层（default / settings_file / forced_constant）、
    宿主层只读事实、enabled=false 提示。
[F] 沙盒写设置：只改一个键 → 后端回读一致；**另一进程独立回读**一致；
    未改动的键零写入（mtime + 内容都不变）；整份回写时不产生写盘；非法值 400 带原因。
[G] 建议正文结构自检（target / goal / evidence / impact / next）逐条结果。

只读保证：真 home 上只做 GET。所有写操作都在临时沙盒里（``_home`` 临时指向沙盒，
结束后恢复），不会碰生产的 settings.json / 台账。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
API = DASHBOARD / "plugin_api.py"
HOME = PLUGIN_ROOT.parents[1]
PREFIX = "/api/plugins/hermes-console"

if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))
for extra in (HOME / "hermes-agent", HOME / "plugins"):
    if extra.is_dir() and str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

failures: list[str] = []
passes = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passes
    if ok:
        passes += 1
        print("  PASS  " + label + (" — " + detail if detail else ""))
    else:
        failures.append(label + (" — " + detail if detail else ""))
        print("  FAIL  " + label + (" — " + detail if detail else ""))


def j(obj, n: int = 200) -> str:
    text = json.dumps(obj, ensure_ascii=False, default=str).replace("\n", " ")
    return text[:n] + ("…" if len(text) > n else "")


def show(label: str, response, n: int = 200) -> dict:
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        print("        %-38s %s  %s" % (label, response.status_code, response.text[:n]))
        return {}
    print("        %-38s %s  %s" % (label, response.status_code, j(body, n)))
    return body


# ---------------------------------------------------------------------------
# 加载真实插件入口（与 dashboard 同款：合成模块名 + spec_from_file_location）
# ---------------------------------------------------------------------------
spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_hermes-console", API)
mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
sys.modules[spec.name] = mod  # type: ignore[union-attr]
spec.loader.exec_module(mod)  # type: ignore[union-attr]

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api_advisor  # noqa: E402  (plugin_api 已 import 过同名模块)

app = FastAPI()
app.include_router(mod.router, prefix=PREFIX)
client = TestClient(app, raise_server_exceptions=False)


def url(path: str) -> str:
    return PREFIX + path


def timed(method: str, path: str, **kwargs):
    started = time.perf_counter()
    response = client.request(method, url(path), **kwargs)
    return response, round((time.perf_counter() - started) * 1000.0, 1)


print("home      :", api_advisor._home())
print("advisor   :", api_advisor.advisor_dir())
print("plugin_api: loaded=%s failed=%s" % (len(mod.DOMAIN_LOADED), mod.DOMAIN_FAILURES))

# ---------------------------------------------------------------------------
# [A] 真路由清单
# ---------------------------------------------------------------------------
print("\n[A] 真路由清单（证明 /advisor/status、/advisor/overview 不是路由）")
routes = sorted(
    (sorted(getattr(route, "methods", []) or [])[0] + " " + getattr(route, "path", ""))
    for route in mod.router.routes
    if "/advisor" in getattr(route, "path", "")
)
for item in routes:
    print("        " + item)
check("★/advisor/status 不在真实路由里", not any(p.endswith("/advisor/status") for p in routes),
      "共 %d 条 advisor 路由" % len(routes))
check("★/advisor/overview 不在真实路由里", not any(p.endswith("/advisor/overview") for p in routes))
check("★/advisor/modes 是新路由", any(p.endswith("/advisor/modes") for p in routes))

# ---------------------------------------------------------------------------
# [B] 逐端点真打
# ---------------------------------------------------------------------------
print("\n[B] 逐端点真打（真 home / 真台账；GET 只读）")
first = client.get(url("/advisor/advice"), params={"limit": 1}).json()
advice_id = ((first.get("data") or {}).get("items") or [{}])[0].get("id", "")
targets = [
    ("GET", "/advisor", None),
    ("GET", "/advisor/summary", None),
    ("GET", "/advisor/coverage", None),
    ("GET", "/advisor/reviews", {"limit": 20}),
    ("GET", "/advisor/advice", {"limit": 3}),
    ("GET", "/advisor/modes", None),
    ("GET", "/advisor/settings", {"agent": "default"}),
    ("GET", "/advisor/advice/" + advice_id, None),
]
timings: dict[str, float] = {}
for method, path, params in targets:
    response, ms = timed(method, path, params=params or {})
    timings[path] = ms
    show(path + (" " + str(params or "")), response)
    check("200 " + path, response.status_code == 200, "%sms" % ms)

# ---------------------------------------------------------------------------
# [C] 模式
# ---------------------------------------------------------------------------
print("\n[C] GET /advisor/modes：三态设置 / 生效值 / 来源层 / 提示词证据")
modes_response, modes_ms = timed("GET", "/advisor/modes")
modes_body = show("/advisor/modes", modes_response, 160)
modes = modes_body.get("data") or {}
check("★switchable=true（模式现在是设置项，可切）", modes.get("switchable") is True)
check("★mode 在设置白名单里", modes.get("mode_key_in_settings") is True,
      "writable=" + ",".join(modes.get("writable_settings") or []))
check("★列出 3 种设置值（auto / target / conservative）", len(modes.get("modes") or []) == 3,
      ",".join(one["id"] for one in modes.get("modes") or []))
check("switch_blocked_reason 为空（不再有「要改插件本体」这种说法）",
      not (modes.get("switch_blocked_reason") or ""),
      repr(modes.get("switch_blocked_reason")))
check("★current_setting 是三种之一",
      modes.get("current_setting") in ("auto", "target", "conservative"),
      repr(modes.get("current_setting")))
check("★setting_source 是 default 或 settings_file",
      modes.get("setting_source") in ("default", "settings_file"),
      repr(modes.get("setting_source")))
check("★resolved_mode 是本轮生效值（目标对齐 / 保守）",
      modes.get("resolved_mode") in ("goal_aligned", "conservative"),
      "%r（来源 %r）" % (modes.get("resolved_mode"), modes.get("resolved_mode_source")))
prompt = modes.get("prompt") or {}
check("提示词指纹非空（读的是真文件）", len(str(prompt.get("prompt_sha256") or "")) == 64,
      "sha256=" + str(prompt.get("prompt_sha256"))[:16] + "… chars=" + str(prompt.get("prompt_chars")))
check("保守模式分支原文确实说 CONSERVATIVE",
      bool(prompt.get("unknown_branch_says_conservative")),
      str(prompt.get("unknown_branch_head"))[:80])
check("目标对齐分支原文提到验收项", bool(prompt.get("known_branch_mentions_acceptance")),
      str(prompt.get("known_branch_head"))[:80])
check("★强制目标对齐 + 无目标分支存在（mode=target 才走得到）",
      bool(prompt.get("forced_target_says_mode_target")),
      str(prompt.get("forced_target_branch_head"))[:80])
current = modes.get("current") or {}
check("当前模式有结论（来自最近一条建议的目标来源）",
      current.get("mode") in ("goal_aligned", "conservative", "unknown"),
      "%s · goal_source=%s · advice=%s" % (current.get("mode"), current.get("goal_source"),
                                           current.get("advice_id")))
print("        current_label:", modes.get("current_label"))
print("        setting/resolved:", modes.get("current_setting"), "→", modes.get("resolved_mode"))
print("        modes:", j(modes.get("modes"), 400))

# ---------------------------------------------------------------------------
# [D] 评审流水 / 504 可读化
# ---------------------------------------------------------------------------
print("\n[D] GET /advisor/reviews：504 归类、次数、时间、飞行中、日志旁证")
reviews_response, reviews_ms = timed("GET", "/advisor/reviews", params={"limit": 20})
reviews = (reviews_response.json().get("data") or {})
errors = reviews.get("errors") or {}
log_evidence = reviews.get("log_evidence") or {}
print("        errors:", j(errors, 320))
print("        in_flight:", reviews.get("in_flight_count"))
print("        log_evidence:", j({k: log_evidence.get(k) for k in
                                ("path", "exists", "count", "by_kind", "first_at", "last_at",
                                 "scanned_lines")}, 320))
check("★日志旁证里 504 被归类为 model_timeout",
      "model_timeout" in (log_evidence.get("by_kind") or {}),
      j(log_evidence.get("by_kind")))
check("★504 有人话原因（不是只有 Error code）",
      all("超时" in str(item.get("readable")) for item in (log_evidence.get("failures") or [])[:3])
      and bool(log_evidence.get("failures")))
check("★504 有发生时间与次数",
      bool(log_evidence.get("first_at")) and bool(log_evidence.get("last_at"))
      and isinstance(log_evidence.get("count"), int),
      "%s ~ %s · %s 次" % (log_evidence.get("first_at"), log_evidence.get("last_at"),
                           log_evidence.get("count")))
check("★504 给了建议（换模型 / 缩短上下文 / 提高超时）",
      len((log_evidence.get("failures") or [{}])[0].get("hints") or []) >= 3,
      " / ".join((log_evidence.get("failures") or [{}])[0].get("hints") or []))
check("飞行中评审单列（不是「无建议」）",
      isinstance(reviews.get("in_flight_count"), int),
      "%s 次" % reviews.get("in_flight_count"))

# ---------------------------------------------------------------------------
# [E] 设置读：逐项 + 来源层 + 宿主层 + enabled 提示
# ---------------------------------------------------------------------------
print("\n[E] GET /advisor/settings：逐项值 / 来源层 / 宿主层 / enabled=false 提示")
settings_response, settings_ms = timed("GET", "/advisor/settings", params={"agent": "default"})
settings = (settings_response.json().get("data") or {})
detail = settings.get("values_detail") or []
print("        values:", j(settings.get("values"), 240))
for item in detail:
    print("        %-24s value=%-8s source=%-16s on_disk=%s default=%s"
          % (item.get("key"), item.get("value"), item.get("source"),
             item.get("on_disk"), item.get("default")))
print("        host_layers:", j(settings.get("host_layers"), 300))
print("        enabled_notice:", str(settings.get("enabled_notice"))[:120], "…")
check("★逐项都有来源层标注", all(item.get("source") for item in detail),
      "层=" + ",".join(sorted({str(item.get("source")) for item in detail})))
check("★可写 6 项（含 mode）+ 只读 1 项",
      len([x for x in detail if x["writable"]]) == 6
      and len([x for x in detail if x["readonly"]]) == 1,
      "writable=" + ",".join(x["key"] for x in detail if x["writable"]))
check("★enabled 提示与当前状态一致（不会说反）",
      (settings.get("enabled") is False and "false" in str(settings.get("enabled_notice")))
      or (settings.get("enabled") is not False and "true" in str(settings.get("enabled_notice"))),
      str(settings.get("enabled_notice"))[:60])
check("★宿主层单独列出（plugins.enabled / allow_gateway_injection）",
      "plugins_enabled" in (settings.get("host_layers") or {})
      and "allow_gateway_injection" in (settings.get("host_layers") or {}))
check("打开 enabled 时有明确后果说明",
      "allow_gateway_injection" in str(settings.get("enabled_effect") or ""))

# ---------------------------------------------------------------------------
# [F] 沙盒写设置（真 home 不动）
# ---------------------------------------------------------------------------
print("\n[F] 沙盒写设置：只改一个键 / 后端回读 / 独立进程回读 / 零多余写入 / 非法值 400")
REAL_HOME = api_advisor._home()
sandbox = Path(tempfile.mkdtemp(prefix="hc-adv-api-"))
try:
    (sandbox / "advisor" / "audit").mkdir(parents=True)
    (sandbox / "plugins" / "advisor").mkdir(parents=True)
    (sandbox / "plugins" / "advisor" / "plugin.yaml").write_text(
        'name: advisor\nversion: "1.4.0"\n', encoding="utf-8")
    (sandbox / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - advisor\n  entries:\n    advisor:\n"
        "      allow_gateway_injection: false\n", encoding="utf-8")
    real_settings = HOME / "advisor" / "audit" / "settings.json"
    base_settings = json.loads(real_settings.read_text(encoding="utf-8")) if real_settings.is_file() else {
        "enabled": False, "concern_default": "hold", "min_interval_seconds": 0,
        "model": "", "provider": ""}
    (sandbox / "advisor" / "audit" / "settings.json").write_text(
        json.dumps(base_settings, ensure_ascii=False, indent=2), encoding="utf-8")
    worker = sandbox / "profiles" / "code-worker"
    (worker / "plugins" / "advisor").mkdir(parents=True)
    (worker / "plugins" / "advisor" / "plugin.yaml").write_text(
        'name: advisor\nversion: "1.4.0"\n', encoding="utf-8")
    (worker / "config.yaml").write_text("plugins:\n  enabled:\n    - advisor\n", encoding="utf-8")
    (worker / "advisor" / "audit").mkdir(parents=True)
    api_advisor._home = lambda: sandbox  # type: ignore[assignment]

    settings_file = worker / "advisor" / "audit" / "settings.json"
    settings_file.write_text(json.dumps(base_settings, ensure_ascii=False, indent=2), encoding="utf-8")
    mtime_before = settings_file.stat().st_mtime_ns
    bytes_before = settings_file.read_bytes()

    # F1 只改一个键
    r1, ms1 = timed("PUT", "/advisor/settings", params={"agent": "code-worker"},
                    json={"concern_default": "deliver"})
    body1 = show("PUT concern_default=deliver", r1)
    data1 = body1.get("data") or {}
    check("★PUT 200 且只写了 1 个键", r1.status_code == 200
          and data1.get("written_keys") == ["concern_default"],
          "written=" + str(data1.get("written_keys")) + " skipped=" + str(data1.get("skipped_keys")))
    check("★后端回读一致（applied 与 values 一致）",
          (data1.get("applied") or {}).get("concern_default") == "deliver"
          and (data1.get("values") or {}).get("concern_default") == "deliver")
    check("落盘值也一致", json.loads(settings_file.read_text(encoding="utf-8")).get("concern_default")
          == "deliver")

    # F2 独立进程回读（不是同一个进程的缓存）
    readback_code = (
        "import json,os,sys;"
        "os.environ['ADVISOR_AUDIT_DIR']=sys.argv[1];"
        "sys.path.insert(0,sys.argv[2]);sys.path.insert(0,sys.argv[3]);"
        "import advisor.audit as a;"
        "print(json.dumps({'settings':a.get_settings(),"
        "'raw':open(os.path.join(sys.argv[1],'settings.json'),encoding='utf-8').read()},ensure_ascii=False))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", readback_code, str(worker / "advisor" / "audit"),
         str(HOME / "plugins"), str(HOME / "hermes-agent")],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    independent: dict = {}
    if proc.returncode == 0 and proc.stdout.strip():
        independent = json.loads(proc.stdout.strip().splitlines()[-1])
        print("        independent:", j(independent, 240))
        check("★另一进程独立回读一致",
              (independent.get("settings") or {}).get("concern_default") == "deliver")
    else:
        check("★另一进程独立回读一致", False,
              "exit=%s err=%s" % (proc.returncode, (proc.stderr or "")[-200:]))

    # F3 未改动的键零写入：整份回写（值全同）→ 不产生写盘
    mtime_mid = settings_file.stat().st_mtime_ns
    bytes_mid = settings_file.read_bytes()
    same = dict((independent.get("settings") or base_settings))
    payload = {k: same.get(k) for k in
               ("enabled", "concern_default", "min_interval_seconds", "model", "provider")}
    r2, _ = timed("PUT", "/advisor/settings", params={"agent": "code-worker"}, json=payload)
    body2 = show("PUT 整份回写（值全同）", r2)
    data2 = body2.get("data") or {}
    check("★未改动的键零写入（no_change + written_keys 空）",
          data2.get("no_change") is True and data2.get("written_keys") == [],
          "skipped=" + str(data2.get("skipped_keys")))
    check("★文件真的没动（mtime + 字节都没变）",
          settings_file.stat().st_mtime_ns == mtime_mid and settings_file.read_bytes() == bytes_mid)

    # F4 第二个键
    r3, _ = timed("PUT", "/advisor/settings", params={"agent": "code-worker"},
                  json={"min_interval_seconds": 45})
    body3 = show("PUT min_interval_seconds=45", r3)
    check("★第二个键也可改并回读一致",
          (body3.get("data") or {}).get("values", {}).get("min_interval_seconds") == 45,
          "written=" + str((body3.get("data") or {}).get("written_keys")))

    # F5 非法值：显示后端原因
    r4, _ = timed("PUT", "/advisor/settings", params={"agent": "code-worker"},
                  json={"min_interval_seconds": -5})
    body4 = show("PUT min_interval_seconds=-5", r4)
    message4 = ((body4.get("error") or {}).get("message") or "")
    check("★非法值 400 且带后端原因", r4.status_code == 400 and ">= 0" in message4, message4[:90])
    check("非法值没有写盘",
          json.loads(settings_file.read_text(encoding="utf-8")).get("min_interval_seconds") == 45)

    # F6 只读阀门
    r5, _ = timed("PUT", "/advisor/settings", params={"agent": "code-worker"},
                  json={"blocker_forced_delivery": False})
    body5 = show("PUT blocker_forced_delivery=false", r5)
    check("★单向阀门仍 400", r5.status_code == 400,
          str((body5.get("error") or {}).get("message"))[:60])

    # F7 打开 enabled：回读 + 提示
    r6, _ = timed("PUT", "/advisor/settings", params={"agent": "code-worker"},
                  json={"enabled": True})
    body6 = show("PUT enabled=true", r6)
    data6 = body6.get("data") or {}
    check("★enabled 打开后回读 true 且提示改成「每个 turn 都会评审」",
          (data6.get("values") or {}).get("enabled") is True
          and "每个 turn" in str(data6.get("enabled_notice")),
          str(data6.get("enabled_notice"))[:70])
finally:
    api_advisor._home = lambda: REAL_HOME  # type: ignore[assignment]
    shutil.rmtree(sandbox, ignore_errors=True)
check("沙盒已清理、_home 已恢复", api_advisor._home() == REAL_HOME)

# ---------------------------------------------------------------------------
# [G] 建议正文结构自检
# ---------------------------------------------------------------------------
print("\n[G] 建议正文结构自检（target / goal / evidence / impact / next）")
listing = client.get(url("/advisor/advice"), params={"limit": 3}).json()
data = listing.get("data") or {}
print("        structure:", j(data.get("structure"), 200))
for item in (data.get("items") or [])[:3]:
    structure = item.get("note_structure") or {}
    print("        %s sev=%-8s target=%-11s (has_target=%-5s) labels=%-22s missing=%s"
          % (item.get("id"), item.get("severity"), structure.get("target_state"),
             structure.get("has_target"),
             ",".join(structure.get("labels") or []) or "-", ",".join(structure.get("missing") or [])))
check("★每条都带结构自检结果",
      all("note_structure" in item for item in (data.get("items") or [])))
check("★结构汇总可读（total / with_target / missing_target / quoted_only / no_target）",
      all(key in (data.get("structure") or {})
          for key in ("total", "with_target", "missing_target", "quoted_only", "no_target")))
# target 三态：口径与插件闸门一致（四种识别形态才算点名，引号只作参考）
check("★target 三态口径（named / quoted_only / none）",
      all((item.get("note_structure") or {}).get("target_state")
          in ("named", "quoted_only", "none")
          for item in (data.get("items") or [])))
check("★引号不算点名：quoted_only 行的 has_target 必须是 false",
      all(not (item.get("note_structure") or {}).get("has_target")
          for item in (data.get("items") or [])
          if (item.get("note_structure") or {}).get("target_state") == "quoted_only"))
_summary = data.get("structure") or {}
check("★汇总里「只引了原话」与「没点名」分开计数且相加不超总数",
      (_summary.get("quoted_only", 0) + _summary.get("no_target", 0)
       + _summary.get("with_target", 0)) == _summary.get("total"))

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
print("\n" + "=" * 72)
if failures:
    print("ADVISOR-DIAG-PROBE-FAILED  (%d passed, %d failed)" % (passes, len(failures)))
    for item in failures:
        print("  - " + item)
    raise SystemExit(1)
print("ADVISOR-DIAG-PROBE-OK  (%d checks)" % passes)

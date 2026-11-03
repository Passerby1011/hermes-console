# -*- coding: utf-8 -*-
"""评审模式（auto / target / conservative）后端验收：真 plugin_api + 沙盒 home。

跑法::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        D:/hermes-data/hermes/plugins/hermes-console/scripts/probe_advisor_modes.py

验的是**契约与落盘**，不是"接口返回了个字段"：

[1] ``GET /advisor/modes?agent=A``：``switchable === true``、``current_setting`` /
    ``setting_source`` / ``resolved_mode`` 三个新字段有真值、``modes`` 三态、mode 在
    白名单里、``switch_blocked_reason`` 为空
[2] 按 agent 独立保存：A 设 ``conservative``、B 设 ``target``，各自回读正确、互不影响
    （并且**逐字节**确认对方的 settings.json 没被写）
[3] 非法值被拒且**不产生半写**（同一次补丁里的 ``model`` 也没落盘）
[4] 没写 ``mode`` 键的老 settings.json → ``auto`` + 来源 ``default``（向后兼容）
[5] 现有 model / provider / enabled / concern_default / min_interval_seconds 零回归
[6] 设置文件里的 ``mode`` 是**落盘真值**（读文件核对，不信接口）

沙盒是临时目录，真 home 一个字节都不动。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
HOME = PLUGIN_ROOT.parents[1]
PREFIX = "/api/plugins/hermes-console"

for extra in (HOME / "hermes-agent", HOME / "plugins", DASHBOARD):
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


CONFIG_YAML = """plugins:
  enabled:
    - advisor
    - hermes-console
  entries:
    advisor:
      allow_gateway_injection: true
"""
PLUGIN_YAML = 'name: advisor\nversion: "1.4.0"\ndescription: probe\n'


def build_sandbox() -> Path:
    """三个 profile：A（有 mode 键）、B（无 mode 键）、C（完全没有 settings.json）。"""
    root = Path(tempfile.mkdtemp(prefix="hc-adv-modes-"))
    (root / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    (root / "plugins" / "advisor").mkdir(parents=True, exist_ok=True)
    (root / "plugins" / "advisor" / "plugin.yaml").write_text(PLUGIN_YAML, encoding="utf-8")
    (root / "advisor" / "audit").mkdir(parents=True, exist_ok=True)

    def profile(name: str, settings: dict | None, *, reviews: list[dict] | None = None) -> None:
        phome = root / "profiles" / name
        (phome / "plugins" / "advisor").mkdir(parents=True, exist_ok=True)
        (phome / "plugins" / "advisor" / "plugin.yaml").write_text(PLUGIN_YAML, encoding="utf-8")
        (phome / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
        audit = phome / "advisor" / "audit"
        audit.mkdir(parents=True, exist_ok=True)
        if settings is not None:
            (audit / "settings.json").write_text(
                json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        if reviews:
            (audit / "reviews.jsonl").write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in reviews) + "\n",
                encoding="utf-8")

    base = {"schema_version": 1, "enabled": True, "concern_default": "hold",
            "min_interval_seconds": 0, "model": "", "provider": ""}
    # A：有 mode 键 + 一条历史评审（目标对齐）
    profile("agent-a", dict(base, mode="target"), reviews=[
        {"schema_version": 1, "outcome": "no_advice", "review_id": "rev-a-1",
         "ts": "2026-11-01T10:00:00.000+08:00", "profile": "agent-a",
         "goal_source": "kanban", "mode_setting": "target", "mode_resolved": "goal_aligned",
         "mode_prompt_marker": "This is what the agent's turn is judged against."},
    ])
    # B：老文件，没有 mode 键
    profile("agent-b", dict(base))
    # C：连 settings.json 都没有
    profile("agent-c", None)
    return root


def main() -> int:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    sandbox = build_sandbox()
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_hermes_console", str(DASHBOARD / "plugin_api.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    import api_advisor

    api_advisor._home = lambda: sandbox  # type: ignore[assignment]
    app = FastAPI()
    app.include_router(module.router, prefix=PREFIX)
    client = TestClient(app)

    def get(path: str, **params):
        return client.get(PREFIX + path, params=params)

    def put(path: str, body: dict, **params):
        return client.put(PREFIX + path, json=body, params=params)

    def settings_file(profile: str) -> Path:
        return sandbox / "profiles" / profile / "advisor" / "audit" / "settings.json"

    def on_disk(profile: str) -> dict:
        path = settings_file(profile)
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    print("sandbox:", sandbox)
    print("\n[1] GET /advisor/modes?agent=agent-a")
    response = get("/advisor/modes", agent="agent-a")
    check("200", response.status_code == 200, str(response.status_code))
    data = (response.json().get("data") or {})
    check("★switchable === true", data.get("switchable") is True, repr(data.get("switchable")))
    check("★switch_blocked_reason 为空", not (data.get("switch_blocked_reason") or ""),
          repr(data.get("switch_blocked_reason")))
    check("★mode 在白名单里", data.get("mode_key_in_settings") is True,
          ",".join(data.get("writable_settings") or []))
    check("★current_setting 来自该 agent 的 settings.json",
          data.get("current_setting") == "target", repr(data.get("current_setting")))
    check("★setting_source === settings_file", data.get("setting_source") == "settings_file",
          repr(data.get("setting_source")))
    check("★resolved_mode 是强制值（target→goal_aligned）",
          data.get("resolved_mode") == "goal_aligned" and data.get("resolved_mode_source") == "forced",
          repr((data.get("resolved_mode"), data.get("resolved_mode_source"))))
    check("★modes 是三态", [row.get("id") for row in data.get("modes") or []]
          == ["auto", "target", "conservative"],
          ",".join(row.get("id", "") for row in data.get("modes") or []))
    check("三态都带 when / criterion / source",
          all(row.get("when") and row.get("criterion") and row.get("source")
              for row in data.get("modes") or []))
    check("提示词证据里有第三种分支（mode=target 且无目标）",
          bool((data.get("prompt") or {}).get("forced_target_says_mode_target")),
          str((data.get("prompt") or {}).get("forced_target_branch_head"))[:70])
    check("mode_markers 两个分支的标记句都读到了",
          len((data.get("prompt") or {}).get("mode_markers") or {}) == 2)

    print("\n[2] 按 agent 独立保存：A=conservative、B=target")
    before_b = settings_file("agent-b").read_bytes()
    response = put("/advisor/settings", {"mode": "conservative"}, agent="agent-a")
    check("A 写入 200", response.status_code == 200, str(response.status_code))
    payload = response.json().get("data") or {}
    check("★A 回读 conservative", (payload.get("settings") or {}).get("mode") == "conservative",
          repr((payload.get("settings") or {}).get("mode")))
    check("★A 的 values.mode 也是 conservative（前端表单就是照它回填的）",
          (payload.get("values") or {}).get("mode") == "conservative",
          repr(payload.get("values")))
    check("★A 落盘真值 conservative", on_disk("agent-a").get("mode") == "conservative",
          json.dumps(on_disk("agent-a"), ensure_ascii=False))
    check("★写 A 没有动 B 的文件", settings_file("agent-b").read_bytes() == before_b)

    response = put("/advisor/settings", {"mode": "target"}, agent="agent-b")
    check("B 写入 200", response.status_code == 200, str(response.status_code))
    check("★B 回读 target", ((response.json().get("data") or {}).get("settings") or {}).get("mode")
          == "target")
    check("★B 落盘真值 target", on_disk("agent-b").get("mode") == "target")

    a_state = (get("/advisor/modes", agent="agent-a").json().get("data") or {})
    b_state = (get("/advisor/modes", agent="agent-b").json().get("data") or {})
    check("★两个 agent 的模式互不影响",
          (a_state.get("current_setting"), b_state.get("current_setting"))
          == ("conservative", "target"),
          "A=%r B=%r" % (a_state.get("current_setting"), b_state.get("current_setting")))
    check("★各自的生效值也各自正确",
          (a_state.get("resolved_mode"), b_state.get("resolved_mode"))
          == ("conservative", "goal_aligned"),
          "A=%r B=%r" % (a_state.get("resolved_mode"), b_state.get("resolved_mode")))
    check("★A 的文件里只有 A 的值",
          on_disk("agent-a").get("mode") == "conservative"
          and on_disk("agent-b").get("mode") == "target")

    print("\n[3] 非法值：拒绝 + 不半写")
    before = settings_file("agent-a").read_bytes()
    response = put("/advisor/settings", {"mode": "xxx"}, agent="agent-a")
    check("★非法 mode 400", response.status_code == 400, str(response.status_code))
    check("★文件一个字节没动", settings_file("agent-a").read_bytes() == before)
    response = put("/advisor/settings", {"model": "half-write", "mode": "xxx"}, agent="agent-a")
    check("★同一补丁里的 model 也没落盘（不半写）",
          response.status_code == 400 and on_disk("agent-a").get("model") == "",
          "status=%s model=%r" % (response.status_code, on_disk("agent-a").get("model")))
    for bad in ("TARGET", "auto ", "", 1, None):
        response = put("/advisor/settings", {"mode": bad}, agent="agent-a")
        check("非法值 %r 被拒" % (bad,), response.status_code == 400, str(response.status_code))

    print("\n[4] 向后兼容：没有 mode 键 / 没有 settings.json")
    legacy = (get("/advisor/modes", agent="agent-c").json().get("data") or {})
    check("★没有 settings.json → current_setting=auto", legacy.get("current_setting") == "auto",
          repr(legacy.get("current_setting")))
    check("★setting_source=default", legacy.get("setting_source") == "default",
          repr(legacy.get("setting_source")))
    check("★auto + 无评审记录 → resolved_mode 有推导结论",
          legacy.get("resolved_mode") in ("goal_aligned", "conservative")
          and legacy.get("resolved_mode_source") == "no_review",
          repr((legacy.get("resolved_mode"), legacy.get("resolved_mode_source"))))
    b_after_legacy = (get("/advisor/modes", agent="agent-b").json().get("data") or {})
    check("★B（老文件、已写 mode）来源是 settings_file",
          b_after_legacy.get("setting_source") == "settings_file",
          repr(b_after_legacy.get("setting_source")))

    print("\n[5] 现有五项设置零回归")
    response = put("/advisor/settings", {"enabled": True, "concern_default": "deliver",
                                        "min_interval_seconds": 30, "model": "m-1",
                                        "provider": "p-1"}, agent="agent-c")
    check("五项写入 200", response.status_code == 200, str(response.status_code))
    disk = on_disk("agent-c")
    check("★enabled / concern_default / min_interval_seconds / model / provider 全部落盘",
          (disk.get("enabled"), disk.get("concern_default"), disk.get("min_interval_seconds"),
           disk.get("model"), disk.get("provider")) == (True, "deliver", 30, "m-1", "p-1"),
          json.dumps(disk, ensure_ascii=False))
    check("★没写 mode 时不会写成别的模式（缺键或 auto 都等于默认）",
          disk.get("mode") in (None, "auto"), json.dumps(disk, ensure_ascii=False))
    settings_body = (get("/advisor/settings", agent="agent-c").json().get("data") or {})
    detail = {row["key"]: row for row in settings_body.get("values_detail") or []}
    check("★values_detail 里 mode 存在且生效值是 auto（来源随文件：缺键=default）",
          detail.get("mode", {}).get("value") == "auto"
          and detail.get("mode", {}).get("source") in ("default", "settings_file"),
          json.dumps(detail.get("mode"), ensure_ascii=False))
    check("GET /advisor/settings 的 values 里也有 mode",
          (settings_body.get("values") or {}).get("mode") == "auto")

    print("\n[6] 落盘真值核对（读文件，不信接口）")
    for profile in ("agent-a", "agent-b", "agent-c"):
        path = settings_file(profile)
        print("  %s: %s" % (profile, json.dumps(on_disk(profile), ensure_ascii=False)))
        check(profile + " settings.json 存在", path.is_file(), str(path))

    print()
    if failures:
        for item in failures:
            print("FAIL", item)
        print("ADVISOR-MODES-PROBE-FAIL (%d failed / %d passed)" % (len(failures), passes))
        return 1
    print("ADVISOR-MODES-PROBE-OK (%d checks)" % passes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

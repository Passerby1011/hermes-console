# -*- coding: utf-8 -*-
"""覆盖面矩阵 + 按 profile 设置：端点契约与隔离性。

三条硬约束在这里被钉住：

* ``GET /advisor/coverage`` 的每一行必须与**磁盘**一致（插件目录 / config.yaml 的
  ``plugins.enabled`` 与 ``allow_gateway_injection`` / 台账事件数 / sessions 数与暂扣数），
  并且状态四态判得对（含 ``reviewing_only`` 那句「进程还是旧代码」的诊断）；
* ``GET/PUT /advisor/settings?agent=`` 只读写**那一个** profile 的
  ``advisor/audit/settings.json``，别的 profile 一个字节都不动；
* 未知字段 / 只读字段 / 未知 profile / 没装插件的 profile 一律 400。
"""

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import api_advisor  # noqa: E402

#: 这些用例真读 ``plugins/advisor/audit.py``（生产侧台账模块）。沙盒/受限环境里该目录
#: 可能不可读（Windows ACL），此时跳过并写明原因，而不是把环境问题报成代码回归。
_ADVISOR_AUDIT = Path(api_advisor.advisor_dir()) / "audit.py"


def _audit_readable(path: Path) -> bool:
    #: Windows ACL 会让 ``Path.is_file()`` / ``os.stat`` 直接抛 ``PermissionError``，
    #: 这里吞掉异常，把「环境不可读」当成 skip 条件而不是收集错误。
    try:
        return os.path.isfile(str(path)) and os.access(str(path), os.R_OK)
    except OSError:
        return False


if not _audit_readable(_ADVISOR_AUDIT):
    pytest.skip("advisor 台账插件不可读（环境限制）：" + str(_ADVISOR_AUDIT),
                allow_module_level=True)

PREFIX = "/api/plugins/hermes-console"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _event(note: str, profile: str, ts: str) -> str:
    return json.dumps({
        "schema_version": 1, "event": "raised", "id": "id-" + note, "ts": ts,
        "profile": profile, "session_id": "s-" + note, "turn_id": "t1",
        "severity": "concern", "note": note, "decision": None, "reason": "",
        "actor": "program", "source": "runtime",
    }, ensure_ascii=False)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """四类 profile 各一个：在用 / 有评审无台账 / 装了没启用 / 根本没装。

    先把**真实**的 ``advisor.audit`` 加载一次（此时 home 还是真 home）：生产里面板
    走的是「复用宿主已加载的那个模块」这条路（``_reuse_loaded_audit``），而
    ``_ensure_sys_path`` 会把 ``<home>/plugins`` 插到 ``sys.path[0]``——临时 home 下
    那个目录只有 ``plugin.yaml``，是**命名空间包**，会把真正的包遮住。这不是放宽断言，
    是把测试摆到生产的加载顺序上。
    """
    real = api_advisor._load_audit()
    assert hasattr(real, "fold")
    # default：装了 + 启用 + 有台账 + 允许投递
    _write(tmp_path / "plugins" / "advisor" / "plugin.yaml", 'name: advisor\nversion: "1.4.0"\n')
    _write(tmp_path / "config.yaml",
           "plugins:\n  enabled:\n    - advisor\n  entries:\n    advisor:\n"
           "      allow_gateway_injection: true\n")
    _write(tmp_path / "advisor" / "audit" / "advice.jsonl",
           _event("a", "default", "2026-10-30T10:00:00.000+08:00") + "\n")
    _write(tmp_path / "advisor" / "audit" / "settings.json",
           json.dumps({"schema_version": 1, "enabled": False, "concern_default": "hold",
                       "min_interval_seconds": 0, "model": "", "provider": ""}))

    # work：装了 + 启用 + 有 sessions + 无台账（进程还是旧代码）
    _write(tmp_path / "profiles" / "work" / "plugins" / "advisor" / "plugin.yaml",
           'name: advisor\nversion: "1.4.0"\n')
    _write(tmp_path / "profiles" / "work" / "config.yaml",
           "plugins:\n  enabled:\n    - advisor\n")
    for index in range(2):
        _write(tmp_path / "profiles" / "work" / "advisor" / "sessions" / ("s%d.json" % index),
               json.dumps({"session_id": "s%d" % index,
                           "held_notes": [{"note": "n1", "severity": "concern"},
                                          {"note": "n2", "severity": "concern"}]}))

    # cad-worker：装了但 config 没启用
    _write(tmp_path / "profiles" / "cad-worker" / "plugins" / "advisor" / "plugin.yaml",
           'name: advisor\nversion: "1.4.0"\n')
    _write(tmp_path / "profiles" / "cad-worker" / "config.yaml", "plugins:\n  enabled: []\n")

    # xiaomo：连插件目录都没有
    _write(tmp_path / "profiles" / "xiaomo" / "config.yaml", "plugins:\n  enabled: []\n")

    monkeypatch.setattr(api_advisor, "_home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def client(home):
    app = FastAPI()
    app.include_router(api_advisor.router, prefix=PREFIX)
    return TestClient(app)


def _get(client, path, **params):
    response = client.get(PREFIX + path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _rows(payload):
    return {row["profile"]: row for row in payload["data"]["profiles"]}


# ── 覆盖面：与磁盘一致 ──────────────────────────────────────────────────────

def test_coverage_row_matches_disk_for_every_profile(client, home):
    rows = _rows(_get(client, "/advisor/coverage"))
    assert set(rows) == {"default", "work", "cad-worker", "xiaomo"}

    default = rows["default"]
    assert (default["plugin_installed"], default["enabled_in_config"],
            default["injection_allowed"]) == (True, True, True)
    assert default["plugin_version"] == "1.4.0"
    assert default["ledger_events"] == 1 and default["ledger_advice"] == 1
    assert default["last_event_at"] == "2026-10-30T10:00:00.000+08:00"
    assert default["settings_enabled"] is False
    assert default["status"] == "active"

    work = rows["work"]
    assert (work["plugin_installed"], work["enabled_in_config"],
            work["injection_allowed"]) == (True, True, False)
    assert work["audit_dir_exists"] is False and work["ledger_events"] == 0
    assert work["session_files"] == 2 and work["held_notes"] == 4
    assert work["status"] == "reviewing_only"

    cad = rows["cad-worker"]
    assert (cad["plugin_installed"], cad["enabled_in_config"]) == (True, False)
    assert cad["status"] == "installed_disabled"

    xiaomo = rows["xiaomo"]
    assert xiaomo["plugin_installed"] is False
    assert xiaomo["plugin_version"] == ""
    assert xiaomo["status"] == "not_installed"


def test_coverage_counts_and_readonly_flag(client):
    payload = _get(client, "/advisor/coverage")
    counts = payload["data"]["status_counts"]
    assert counts["active"] == 1 and counts["reviewing_only"] == 1
    assert counts["installed_disabled"] == 1 and counts["not_installed"] == 1
    assert payload["data"]["read_only"] is True
    assert sum(counts.values()) == payload["data"]["total"]


def test_reviewing_only_diagnosis_says_restart_the_process(client):
    """本轮排查最花时间的一句：必须在面板上自解释。"""
    work = _rows(_get(client, "/advisor/coverage"))["work"]
    assert "插件已加载但台账为空" in work["diagnosis"]
    assert "gateway/serve" in work["diagnosis"]
    assert "重启" in work["diagnosis"]


def test_coverage_is_read_only(client, home):
    before = {p: p.read_bytes() for p in home.rglob("*")
              if p.is_file() and p.suffix in (".json", ".jsonl", ".yaml")}
    _get(client, "/advisor/coverage")
    after = {p: p.read_bytes() for p in home.rglob("*")
             if p.is_file() and p.suffix in (".json", ".jsonl", ".yaml")}
    assert before == after


# ── 设置按 profile ─────────────────────────────────────────────────────────

def _posix(path: str) -> str:
    r"""Windows 后端返回反斜杠，断言按 `/` 写就得先归一化（否则跨平台必红）。"""
    return path.replace("\\", "/")


def test_settings_read_is_scoped_to_the_profile(client, home):
    default = _get(client, "/advisor/settings", agent="default")["data"]
    work = _get(client, "/advisor/settings", agent="work")["data"]
    assert default["profile"] == "default"
    assert _posix(default["path"]).endswith("advisor/audit/settings.json")
    assert "profiles" not in _posix(default["path"])
    assert default["source"] == "file" and default["stored"]["enabled"] is False
    assert work["profile"] == "work"
    assert _posix(work["path"]).endswith("profiles/work/advisor/audit/settings.json")
    assert work["source"] == "defaults" and work["stored"] == {}
    assert default["values"] != work["values"]
    assert set(default["profiles"]) == {"default", "work", "cad-worker", "xiaomo"}


def test_settings_write_touches_only_that_profile(client, home):
    default_path = home / "advisor" / "audit" / "settings.json"
    before_default = default_path.read_bytes()
    work_path = home / "profiles" / "work" / "advisor" / "audit" / "settings.json"
    assert not work_path.is_file()

    response = client.put(PREFIX + "/advisor/settings", params={"agent": "work"},
                          json={"min_interval_seconds": 7})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["profile"] == "work"
    assert data["applied"] == {"min_interval_seconds": 7}
    # 生效值来自回读，不是提交值
    assert data["settings"]["min_interval_seconds"] == 7
    assert data["stored"]["min_interval_seconds"] == 7

    assert work_path.is_file()
    assert json.loads(work_path.read_text(encoding="utf-8"))["min_interval_seconds"] == 7
    assert default_path.read_bytes() == before_default
    assert not (home / "profiles" / "cad-worker" / "advisor" / "audit" / "settings.json").is_file()


def test_settings_write_rejects_unknown_field(client, home):
    response = client.put(PREFIX + "/advisor/settings", params={"agent": "work"},
                          json={"enable_everything": True})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_argument"
    assert not (home / "profiles" / "work" / "advisor" / "audit" / "settings.json").is_file()


def test_settings_write_rejects_closing_the_valve(client):
    response = client.put(PREFIX + "/advisor/settings", params={"agent": "default"},
                          json={"blocker_forced_delivery": False})
    assert response.status_code == 400
    assert "单向阀门不可关闭" in response.json()["error"]["message"]


def test_settings_write_rejects_unknown_profile_and_missing_plugin(client):
    unknown = client.put(PREFIX + "/advisor/settings", params={"agent": "nope"},
                         json={"enabled": True})
    assert unknown.status_code == 400
    missing = client.put(PREFIX + "/advisor/settings", params={"agent": "xiaomo"},
                         json={"enabled": True})
    assert missing.status_code == 400
    assert "没装 advisor 插件" in missing.json()["error"]["message"]


def test_settings_get_rejects_unknown_profile(client):
    response = client.get(PREFIX + "/advisor/settings", params={"agent": "nope"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_argument"


# ── ?profile= 与宿主二级 profile 路由冲突（t_43f17bdb）─────────────────────

def test_settings_rejects_legacy_profile_param_on_get(client):
    """``?profile=`` 不再被接受：宿主会把带它的插件请求整条重绑到该 profile 的 config
    scope（本插件的 API 宿主在那里不存在 → 宿主自己 404 Plugin not found）。设置端点
    必须明确 400 并指向 ``?agent=``，**不能**静默当默认 profile 读（那会读错 profile）。"""
    response = client.get(PREFIX + "/advisor/settings", params={"profile": "work"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_argument"
    assert "?agent=" in error["message"]
    assert error["param"] == "profile" and error["replacement"] == "agent"


def test_settings_rejects_legacy_profile_param_on_put(client, home):
    """PUT 同理：带 ``?profile=`` 的写请求必须在写盘前被挡住（零写入）。"""
    work_path = home / "profiles" / "work" / "advisor" / "audit" / "settings.json"
    response = client.put(PREFIX + "/advisor/settings", params={"profile": "work"},
                          json={"min_interval_seconds": 11})
    assert response.status_code == 400
    assert "?agent=" in response.json()["error"]["message"]
    assert not work_path.is_file()


def test_settings_accepts_bridge_attached_profile_when_agent_present(client):
    """桌面桥 ``profileScoped()`` 会给每个插件请求附加 ``?profile=<ambient>``；前端自己
    发的是 ``?agent=``。两者同时出现时以 agent 为准，不能把正常请求打成 400
    （t_3e01204a：``?agent=default&profile=default`` 恒 400 的根因）。"""
    response = client.get(PREFIX + "/advisor/settings",
                          params={"agent": "default", "profile": "default"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["profile"] == "default"
    # 响应显式说明这个 profile 是桥加的、已被忽略（不是静默吞掉）
    assert data["ignored_profile_param"] == "default"


def test_settings_put_accepts_bridge_attached_profile_when_agent_present(client, home):
    """PUT 同理：带 agent 时桥附加的 profile 不得触发 legacy 闸门（且不写盘）。"""
    before = (home / "advisor" / "audit" / "settings.json").read_text(encoding="utf-8")
    response = client.put(PREFIX + "/advisor/settings",
                          params={"agent": "default", "profile": "default"}, json={})
    assert response.status_code == 400
    assert "查询参数已改名" not in response.json()["error"]["message"]
    assert (home / "advisor" / "audit" / "settings.json").read_text(encoding="utf-8") == before


def test_settings_agent_param_does_not_use_the_host_routing_name(client, home):
    """``agent=`` 与其它 advisor 端点同一口径：宿主只认 ``profile=``，所以这个选择器
    不会被宿主当成二级 profile 路由指令（回归点：改名后仍按 agent 分别读写）。"""
    default = _get(client, "/advisor/settings", agent="default")["data"]
    work = _get(client, "/advisor/settings", agent="work")["data"]
    assert default["profile"] == "default" and work["profile"] == "work"
    response = client.put(PREFIX + "/advisor/settings", params={"agent": "work"},
                          json={"min_interval_seconds": 13})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["profile"] == "work"
    assert (home / "profiles" / "work" / "advisor" / "audit" / "settings.json").is_file()
    # default 的 settings.json 里这些键本来就存在（0 = 默认值）：断言它没被改成 13
    assert json.loads((home / "advisor" / "audit" / "settings.json")
                      .read_text(encoding="utf-8"))["min_interval_seconds"] == 0


# ── 目标来源 ───────────────────────────────────────────────────────────────

def test_advice_rows_carry_goal_source_from_events(client, home):
    """goal_* 只存在事件里（fold 不保留）：行里必须能读出来，缺失时是 unknown。"""
    _write(home / "advisor" / "audit" / "advice.jsonl", json.dumps({
        "schema_version": 1, "event": "raised", "id": "id-g", "ts": "2026-10-30T11:00:00.000+08:00",
        "profile": "default", "session_id": "s-g", "turn_id": "t1", "severity": "concern",
        "note": "带目标", "decision": None, "reason": "", "actor": "program",
        "source": "runtime", "goal_source": "kanban", "goal_ref": "t_6c6fffd3",
    }, ensure_ascii=False) + "\n" + _event("无目标", "default", "2026-10-30T12:00:00.000+08:00") + "\n")

    items = _get(client, "/advisor/advice")["data"]["items"]
    by_note = {item["note"]: item for item in items}
    assert by_note["带目标"]["goal_source"] == "kanban"
    assert by_note["带目标"]["goal_ref"] == "t_6c6fffd3"
    assert by_note["带目标"]["goal_known"] is True
    assert by_note["无目标"]["goal_source"] == "unknown"
    assert by_note["无目标"]["goal_ref"] == ""
    assert by_note["无目标"]["goal_known"] is False

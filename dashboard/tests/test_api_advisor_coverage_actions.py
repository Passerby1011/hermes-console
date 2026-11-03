# -*- coding: utf-8 -*-
"""可操作覆盖矩阵写端点：安装 / 同步 / 启停插件 / 启停评审 / 卸载（t_72e9a7fa）。

钉住的契约：

* **默认零写入**：``mode=preview`` 只回变更清单，不建目录、不改文件；
* **执行必须二次确认**：``mode=apply`` 无 ``confirm=true`` → 400；
* **必须显式点名**：``profiles`` 空列表 → 400（不允许隐式全选 / 一键全装）；
* **只改被点名的 profile**：未点名的 profile 逐个比对 before/after，必须零变化；
* **写后回读**：安装/同步逐文件哈希；启停插件回读 config；启停评审回读 settings.json；
  并把「改前 → 改后」coverage 行一起返回。
"""

import json
import sys
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import api_advisor  # noqa: E402

PREFIX = "/api/plugins/hermes-console"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _event(note: str, profile: str) -> str:
    return json.dumps({
        "schema_version": 1, "event": "raised", "id": "id-" + note, "ts": "2026-10-30T10:00:00.000+08:00",
        "profile": profile, "session_id": "s-" + note, "turn_id": "t1", "severity": "concern",
        "note": note, "decision": None, "reason": "", "actor": "program", "source": "runtime",
    }, ensure_ascii=False)


def _config(enabled: bool, injection: bool = False) -> str:
    body = "plugins:\n"
    body += "  enabled:\n    - advisor\n" if enabled else "  enabled: []\n"
    body += "  entries:\n    advisor:\n      allow_gateway_injection: %s\n" % ("true" if injection else "false")
    return body


@pytest.fixture
def home(tmp_path, monkeypatch):
    """四类状态各一个：active / reviewing_only / installed_disabled / not_installed。"""
    real = api_advisor._load_audit()
    assert hasattr(real, "fold")

    # default：装了 + 启用 + 允许投递 + 有台账事件 → active
    _write(tmp_path / "plugins" / "advisor" / "plugin.yaml", 'name: advisor\nversion: "1.4.0"\n')
    _write(tmp_path / "config.yaml", _config(enabled=True, injection=True))
    _write(tmp_path / "advisor" / "audit" / "advice.jsonl", _event("a", "default") + "\n")
    _write(tmp_path / "advisor" / "audit" / "settings.json",
           json.dumps({"schema_version": 1, "enabled": False}))

    # reviewing：装了 + 启用 + 有 sessions 无台账 → reviewing_only
    _write(tmp_path / "profiles" / "reviewing" / "plugins" / "advisor" / "plugin.yaml",
           'name: advisor\nversion: "1.4.0"\n')
    _write(tmp_path / "profiles" / "reviewing" / "config.yaml", _config(enabled=True))
    _write(tmp_path / "profiles" / "reviewing" / "advisor" / "sessions" / "s0.json",
           json.dumps({"session_id": "s0", "held_notes": []}))

    # disabled：装了但 config 没启用（可测 enable_plugin / disable_plugin）
    _write(tmp_path / "profiles" / "disabled" / "plugins" / "advisor" / "plugin.yaml",
           'name: advisor\nversion: "1.4.0"\n')
    _write(tmp_path / "profiles" / "disabled" / "config.yaml", _config(enabled=False))
    _write(tmp_path / "profiles" / "disabled" / "advisor" / "audit" / "settings.json",
           json.dumps({"schema_version": 1, "enabled": True}))

    # missing：连插件目录都没有（可测 install / uninstall）
    _write(tmp_path / "profiles" / "missing" / "config.yaml", _config(enabled=False))

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


def _rows(client):
    return {row["profile"]: row for row in _get(client, "/advisor/coverage")["data"]["profiles"]}


def _post(client, body):
    return client.post(PREFIX + "/advisor/coverage/action", json=body)


# ── 预览 / 参数校验（零写入）──────────────────────────────────────────────

def test_preview_is_zero_write_and_lists_plan(client, home):
    before = sorted(p.name for p in (home / "profiles" / "missing").rglob("*"))
    payload = _post(client, {"action": "install", "profiles": ["missing"]}).json()
    assert payload["ok"] is True
    data = payload["data"]
    assert data["mode"] == "preview" and data["zero_write"] is True
    assert data["selected"] == ["missing"]
    plan = data["plans"][0]
    assert plan["allowed"] is True and plan["changes"]
    # 目录没建出来（零写入）
    after = sorted(p.name for p in (home / "profiles" / "missing").rglob("*"))
    assert before == after
    assert not (home / "profiles" / "missing" / "plugins" / "advisor").is_dir()
    # 未点名的不在 selected
    assert "default" in data["untouched"] and "missing" not in data["untouched"]


def test_apply_requires_confirm(client):
    resp = _post(client, {"action": "install", "profiles": ["missing"], "mode": "apply"})
    assert resp.status_code == 400
    assert "confirm" in resp.json()["error"]["message"]


def test_profiles_must_be_explicit(client):
    resp = _post(client, {"action": "install", "profiles": []})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_argument"


def test_unknown_action_and_profile_rejected(client):
    assert _post(client, {"action": "nuke", "profiles": ["missing"]}).status_code == 400
    assert _post(client, {"action": "install", "profiles": ["nope"]}).status_code == 400


# ── 安装：哈希回读 + 改前改后 + 未点名零变化 ────────────────────────────────

def test_install_creates_copy_and_hashes_match(client, home):
    others_before = {name: (home / "profiles" / name / "config.yaml").read_bytes()
                     for name in ("reviewing", "disabled")}
    payload = _post(client, {"action": "install", "profiles": ["missing"],
                             "mode": "apply", "confirm": True}).json()
    assert payload["ok"] is True, payload
    applied = payload["data"]["applied"][0]
    assert applied["ok"] is True
    hashes = applied["hash_match"]
    assert hashes["all_match"] is True
    by_file = {row["file"]: row for row in hashes["files"]}
    for name in ("models.py", "runtime.py", "advisor_prompt.py"):
        assert by_file[name]["match"] is True and by_file[name]["source"] == by_file[name]["dest"]
    # 目录真的建出来且带运行期文件
    dest = home / "profiles" / "missing" / "plugins" / "advisor"
    assert (dest / "models.py").is_file() and (dest / "plugin.yaml").is_file()
    # 改前 not_installed → 改后 installed_disabled
    transition = payload["data"]["transitions"][0]
    assert transition["before"]["status"] == "not_installed"
    assert transition["after"]["status"] == "installed_disabled"
    assert transition["after"]["plugin_installed"] is True
    # 未点名的 profile 零变化
    assert payload["data"]["untouched_unchanged"] is True
    for name, content in others_before.items():
        assert (home / "profiles" / name / "config.yaml").read_bytes() == content
    # GET coverage 复核
    assert _rows(client)["missing"]["status"] == "installed_disabled"


def test_install_blocked_when_already_installed(client):
    payload = _post(client, {"action": "install", "profiles": ["disabled"]}).json()
    assert payload["data"]["selected"] == []
    assert payload["data"]["blocked"][0]["profile"] == "disabled"


# ── 启停插件：config 回读 ────────────────────────────────────────────────

def _config_dict(home, profile):
    path = home / "config.yaml" if profile == "default" else home / "profiles" / profile / "config.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_enable_then_disable_plugin_readback(client, home):
    # disabled 当前未启用 → 启用
    payload = _post(client, {"action": "enable_plugin", "profiles": ["disabled"],
                             "mode": "apply", "confirm": True}).json()
    assert payload["ok"] is True, payload
    cfg = _config_dict(home, "disabled")
    assert "advisor" in cfg["plugins"]["enabled"]
    assert cfg["plugins"]["entries"]["advisor"]["enabled"] is True
    assert payload["data"]["applied"][0]["readback"]["enabled_in_config"] is True
    assert _rows(client)["disabled"]["enabled_in_config"] is True

    # 再停用
    payload = _post(client, {"action": "disable_plugin", "profiles": ["disabled"],
                             "mode": "apply", "confirm": True}).json()
    assert payload["ok"] is True, payload
    cfg = _config_dict(home, "disabled")
    assert "advisor" not in (cfg["plugins"]["enabled"] or [])
    assert cfg["plugins"]["entries"]["advisor"]["enabled"] is False
    assert payload["data"]["applied"][0]["readback"]["enabled_in_config"] is False
    assert _rows(client)["disabled"]["enabled_in_config"] is False


def test_enable_plugin_blocked_when_not_installed(client):
    payload = _post(client, {"action": "enable_plugin", "profiles": ["missing"]}).json()
    assert payload["data"]["selected"] == []
    assert "未安装" in payload["data"]["blocked"][0]["reason"]


# ── 启停评审：settings.json 回读 + 依赖门 ─────────────────────────────────

def test_review_enable_then_disable_readback(client, home):
    # default：装了 + 启用 + review 关 → 启用评审
    payload = _post(client, {"action": "enable_review", "profiles": ["default"],
                             "mode": "apply", "confirm": True}).json()
    assert payload["ok"] is True, payload
    stored = json.loads((home / "advisor" / "audit" / "settings.json").read_text(encoding="utf-8"))
    assert stored["enabled"] is True
    assert payload["data"]["applied"][0]["readback"]["enabled"] is True
    assert _rows(client)["default"]["settings_enabled"] is True

    payload = _post(client, {"action": "disable_review", "profiles": ["default"],
                             "mode": "apply", "confirm": True}).json()
    assert payload["ok"] is True, payload
    stored = json.loads((home / "advisor" / "audit" / "settings.json").read_text(encoding="utf-8"))
    assert stored["enabled"] is False
    assert _rows(client)["default"]["settings_enabled"] is False


def test_enable_review_blocked_when_plugin_disabled(client):
    # disabled：装了但插件未启用 → 启用评审被拦（插件没加载，评审跑不了）
    payload = _post(client, {"action": "enable_review", "profiles": ["disabled"]}).json()
    assert payload["data"]["selected"] == []
    assert "插件未启用" in payload["data"]["blocked"][0]["reason"]
    # apply 没有可执行项 → 400
    resp = _post(client, {"action": "enable_review", "profiles": ["disabled"],
                          "mode": "apply", "confirm": True})
    assert resp.status_code == 400


# ── 卸载 ────────────────────────────────────────────────────────────────

def test_uninstall_removes_dir_after_confirm(client, home):
    # 卸载必须 confirm
    assert _post(client, {"action": "uninstall", "profiles": ["disabled"]}).status_code == 400
    payload = _post(client, {"action": "uninstall", "profiles": ["disabled"],
                             "mode": "apply", "confirm": True}).json()
    assert payload["ok"] is True, payload
    assert not (home / "profiles" / "disabled" / "plugins" / "advisor").is_dir()
    assert _rows(client)["disabled"]["plugin_installed"] is False
    assert _rows(client)["disabled"]["status"] == "not_installed"


# ── 只改被点名的 profile（多选也逐个隔离）─────────────────────────────────

def test_only_selected_profiles_change(client, home):
    untouched_before = {name: (home / "profiles" / name / "config.yaml").read_bytes()
                        for name in ("reviewing", "disabled")}
    payload = _post(client, {"action": "enable_plugin", "profiles": ["reviewing"],
                             "mode": "apply", "confirm": True}).json()
    # reviewing 本来就启用：启用动作被拦，但 apply 里没别的 → 400（无可执行）
    # 用 install 到 missing 更稳：只动 missing
    payload = _post(client, {"action": "install", "profiles": ["missing"],
                             "mode": "apply", "confirm": True}).json()
    assert payload["ok"] is True
    assert payload["data"]["untouched_unchanged"] is True
    for name, content in untouched_before.items():
        assert (home / "profiles" / name / "config.yaml").read_bytes() == content

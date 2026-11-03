# -*- coding: utf-8 -*-
"""「允许注入」逐 Agent 开关（t_4121ded4）。

钉住的契约：

* **独立落点**：只写 ``plugins.entries.advisor.allow_gateway_injection``，
  不碰 ``plugins.enabled`` / ``plugins.entries.advisor.enabled`` / ``audit/settings.json``；
* **两段式**：``preview`` 零写入并给出「改哪个文件的哪个字段、before→after」；
  ``apply`` 必须 ``confirm=true``；写后回读 config 真值（不是回显入参）；
* **定点文本编辑**：只动那一行，注释与其它键**字节不变**（不整份 yaml.safe_dump）；
* **依赖只提示不阻止**：评审关着时打开注入 → ``notices`` 有提示但仍允许；
* **只改被点名的 profile**：未点名 profile 的 config 逐文件哈希不变；
* **幂等守卫**：本来就是目标值时不允许（preview 直接 blocked，零写入）。
"""

import hashlib
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
FIELD = "plugins.entries.advisor.allow_gateway_injection"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# 故意带注释 + 与 advisor 无关的键：定点编辑不许把它们动掉
CW_CONFIG = """\
# code-worker 的配置（手写注释，不许被面板改写掉）
model: gpt-5
plugins:
  enabled:
    - advisor          # 插件启用名单
    - file-policy
  disabled: []
  entries:
    hermes-auto-titler:
      allow_tool_override: false
    advisor:
      allow_tool_override: false
      llm:
        allow_provider_override: true
kanban:
  auto: true
"""

OTHER_CONFIG = """\
# 别的 agent，本卡一个字都不许动
plugins:
  enabled: []
  entries:
    advisor:
      allow_gateway_injection: false
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    real = api_advisor._load_audit()
    assert hasattr(real, "fold")

    # 目标 profile：装了 + 启用，但**没有** allow_gateway_injection 键
    _write(tmp_path / "profiles" / "cw" / "plugins" / "advisor" / "plugin.yaml",
           'name: advisor\nversion: "1.4.0"\n')
    _write(tmp_path / "profiles" / "cw" / "config.yaml", CW_CONFIG)
    _write(tmp_path / "profiles" / "cw" / "advisor" / "audit" / "settings.json",
           json.dumps({"schema_version": 1, "enabled": False}))

    # 未被点名的邻居：字节必须不变
    _write(tmp_path / "profiles" / "other" / "plugins" / "advisor" / "plugin.yaml",
           'name: advisor\nversion: "1.4.0"\n')
    _write(tmp_path / "profiles" / "other" / "config.yaml", OTHER_CONFIG)

    # 没有 advisor entry 的 profile：走整份 YAML 回写兜底
    _write(tmp_path / "profiles" / "bare" / "plugins" / "advisor" / "plugin.yaml",
           'name: advisor\nversion: "1.4.0"\n')
    _write(tmp_path / "profiles" / "bare" / "config.yaml", "plugins:\n  enabled:\n    - advisor\n")

    monkeypatch.setattr(api_advisor, "_home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def client(home):
    app = FastAPI()
    app.include_router(api_advisor.router, prefix=PREFIX)
    return TestClient(app)


def _post(client, body):
    return client.post(PREFIX + "/advisor/coverage/action", json=body)


def _rows(client):
    resp = client.get(PREFIX + "/advisor/coverage")
    assert resp.status_code == 200, resp.text
    return {row["profile"]: row for row in resp.json()["data"]["profiles"]}


def _cfg(home, profile):
    path = home / "config.yaml" if profile == "default" else home / "profiles" / profile / "config.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _bytes(home, profile):
    path = home / "profiles" / profile / "config.yaml"
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── 动作在路由白名单里 ────────────────────────────────────────────────────

def test_injection_actions_are_known(client):
    payload = _post(client, {"action": "enable_injection", "profiles": ["cw"]}).json()
    assert payload["ok"] is True, payload
    assert payload["data"]["action"] == "enable_injection"


# ── 预览：零写入 + 字段级变更清单 ──────────────────────────────────────────

def test_preview_is_zero_write_and_names_the_field(client, home):
    before = (home / "profiles" / "cw" / "config.yaml").read_bytes()
    payload = _post(client, {"action": "enable_injection", "profiles": ["cw"]}).json()
    data = payload["data"]
    assert data["mode"] == "preview" and data["zero_write"] is True
    assert data["selected"] == ["cw"]
    plan = data["plans"][0]
    assert plan["allowed"] is True
    change = plan["changes"][0]
    assert change["field"] == FIELD
    assert change["before"] is False and change["after"] is True
    assert change["path"].endswith("config.yaml")
    # 零写入：字节不变
    assert (home / "profiles" / "cw" / "config.yaml").read_bytes() == before


def test_preview_does_not_touch_the_other_three_landings(client, home):
    """四件事互相独立：注入动作的变更清单里不许出现 enabled / plugins.enabled / settings.json。

    （提示文案里**可以**提到 audit/settings.json —— 那是依赖关系说明，不是写入目标；
    所以断言只看 ``changes``，不看整段 JSON。）
    """
    payload = _post(client, {"action": "enable_injection", "profiles": ["cw"]}).json()
    plan = payload["data"]["plans"][0]
    fields = [c["field"] for c in plan["changes"]]
    assert fields == [FIELD]
    targets = json.dumps(plan["changes"], ensure_ascii=False)
    assert "plugins.entries.advisor.enabled" not in targets
    assert "settings.json" not in targets
    assert "plugins.enabled" not in targets


def test_apply_requires_confirm(client, home):
    before = (home / "profiles" / "cw" / "config.yaml").read_bytes()
    resp = _post(client, {"action": "enable_injection", "profiles": ["cw"], "mode": "apply"})
    assert resp.status_code == 400
    assert "confirm" in resp.json()["error"]["message"]
    assert (home / "profiles" / "cw" / "config.yaml").read_bytes() == before


# ── 打开 / 关闭 + 回读真值 ────────────────────────────────────────────────

def test_enable_then_disable_readback(client, home):
    payload = _post(client, {"action": "enable_injection", "profiles": ["cw"],
                             "mode": "apply", "confirm": True}).json()
    assert payload["ok"] is True, payload
    applied = payload["data"]["applied"][0]
    assert applied["ok"] is True
    readback = applied["readback"]
    assert readback["injection_allowed"] is True
    assert readback["stored_value"] is True
    assert readback["edit_mode"] == "text"
    # 磁盘真值
    assert _cfg(home, "cw")["plugins"]["entries"]["advisor"]["allow_gateway_injection"] is True
    # GET /advisor/coverage 的该行随之变化
    assert _rows(client)["cw"]["injection_allowed"] is True
    assert payload["data"]["transitions"][0]["before"]["injection_allowed"] is False
    assert payload["data"]["transitions"][0]["after"]["injection_allowed"] is True

    # 再关闭
    payload = _post(client, {"action": "disable_injection", "profiles": ["cw"],
                             "mode": "apply", "confirm": True}).json()
    assert payload["ok"] is True, payload
    assert payload["data"]["applied"][0]["readback"]["injection_allowed"] is False
    assert _cfg(home, "cw")["plugins"]["entries"]["advisor"]["allow_gateway_injection"] is False
    assert _rows(client)["cw"]["injection_allowed"] is False


def test_surgical_edit_keeps_comments_and_other_lines(client, home):
    """定点编辑：只多一行/改一行，注释、缩进、无关键全部原样。"""
    path = home / "profiles" / "cw" / "config.yaml"
    before_lines = path.read_text(encoding="utf-8").splitlines()

    _post(client, {"action": "enable_injection", "profiles": ["cw"],
                   "mode": "apply", "confirm": True})
    after_lines = path.read_text(encoding="utf-8").splitlines()

    assert len(after_lines) == len(before_lines) + 1
    assert after_lines.count("      allow_gateway_injection: true") == 1
    # 注释一字不少
    assert "# code-worker 的配置（手写注释，不许被面板改写掉）" in after_lines
    assert "# 插件启用名单" in path.read_text(encoding="utf-8")
    # 除新插入那一行之外，其余行完全相同（顺序与内容）
    inserted = "      allow_gateway_injection: true"
    assert [ln for ln in after_lines if ln != inserted] == before_lines

    # 再关闭：行数回到原样，只把值换成 false
    _post(client, {"action": "disable_injection", "profiles": ["cw"],
                   "mode": "apply", "confirm": True})
    off_lines = path.read_text(encoding="utf-8").splitlines()
    assert len(off_lines) == len(before_lines) + 1
    assert "      allow_gateway_injection: false" in off_lines
    assert [ln for ln in off_lines if ln != "      allow_gateway_injection: false"] == before_lines


def test_replaces_existing_key_in_place(client, home):
    """已有该键时是「替换那一行」，不是再加一行。"""
    path = home / "profiles" / "other" / "config.yaml"
    before_lines = path.read_text(encoding="utf-8").splitlines()
    _post(client, {"action": "enable_injection", "profiles": ["other"],
                   "mode": "apply", "confirm": True})
    after_lines = path.read_text(encoding="utf-8").splitlines()
    assert len(after_lines) == len(before_lines)
    assert "      allow_gateway_injection: true" in after_lines
    assert "      allow_gateway_injection: false" not in after_lines
    assert _cfg(home, "other")["plugins"]["entries"]["advisor"]["allow_gateway_injection"] is True


def test_missing_entry_falls_back_to_full_yaml_and_reads_back(client, home):
    payload = _post(client, {"action": "enable_injection", "profiles": ["bare"],
                             "mode": "apply", "confirm": True}).json()
    assert payload["ok"] is True, payload
    readback = payload["data"]["applied"][0]["readback"]
    assert readback["edit_mode"] == "full-yaml"
    assert readback["injection_allowed"] is True
    assert _cfg(home, "bare")["plugins"]["entries"]["advisor"]["allow_gateway_injection"] is True
    # 兜底路径也不许碰「插件启用」
    assert "advisor" in _cfg(home, "bare")["plugins"]["enabled"]


# ── 只改被点名的 profile ──────────────────────────────────────────────────

def test_only_selected_profile_changes(client, home):
    before = {name: _bytes(home, name) for name in ("other", "bare")}
    payload = _post(client, {"action": "enable_injection", "profiles": ["cw"],
                             "mode": "apply", "confirm": True}).json()
    assert payload["data"]["untouched_unchanged"] is True
    assert payload["data"]["untouched_changes"] == []
    for name, digest in before.items():
        assert _bytes(home, name) == digest, name
    # 目标 profile 确实变了
    assert _cfg(home, "cw")["plugins"]["entries"]["advisor"]["allow_gateway_injection"] is True


# ── 依赖关系：提示但不阻止 ────────────────────────────────────────────────

def test_notice_when_review_disabled_but_still_allowed(client):
    """评审关着时打开注入没有意义 —— 提示，但不拦。"""
    payload = _post(client, {"action": "enable_injection", "profiles": ["cw"]}).json()
    plan = payload["data"]["plans"][0]
    assert plan["allowed"] is True
    notices = " ".join(plan.get("notices") or [])
    assert "评审" in notices and "没有意义" in notices
    assert "只记账不打扰" in notices


def test_disable_notice_says_ledger_only(client):
    _post(client, {"action": "enable_injection", "profiles": ["cw"], "mode": "apply", "confirm": True})
    payload = _post(client, {"action": "disable_injection", "profiles": ["cw"]}).json()
    notices = " ".join(payload["data"]["plans"][0].get("notices") or [])
    assert "只写进台账" in notices


# ── 幂等守卫 ──────────────────────────────────────────────────────────────

def test_already_on_is_blocked_with_zero_write(client, home):
    _post(client, {"action": "enable_injection", "profiles": ["cw"], "mode": "apply", "confirm": True})
    before = _bytes(home, "cw")
    payload = _post(client, {"action": "enable_injection", "profiles": ["cw"]}).json()
    assert payload["data"]["selected"] == []
    assert "本来就允许注入" in payload["data"]["blocked"][0]["reason"]
    assert _bytes(home, "cw") == before


def test_already_off_is_blocked(client):
    payload = _post(client, {"action": "disable_injection", "profiles": ["cw"]}).json()
    assert payload["data"]["selected"] == []
    assert "本来就不允许注入" in payload["data"]["blocked"][0]["reason"]


def test_not_installed_is_blocked(client, home):
    _write(home / "profiles" / "ghost" / "config.yaml", "plugins:\n  enabled: []\n")
    payload = _post(client, {"action": "enable_injection", "profiles": ["ghost"]}).json()
    assert payload["data"]["selected"] == []
    assert "未安装" in payload["data"]["blocked"][0]["reason"]


# ── 默认全不选 ────────────────────────────────────────────────────────────

def test_profiles_must_be_explicit(client):
    resp = _post(client, {"action": "enable_injection", "profiles": []})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_argument"

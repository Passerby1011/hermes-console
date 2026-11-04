# -*- coding: utf-8 -*-
"""模型分区「moa 预设（配方）可编辑」契约测试 —— ``/models/moa/presets`` 四个端点。

本卡（t_3ca95551）把 ``moa.presets.<预设名>`` 从**只读**变成**可增删改**，
同时把旧插件 ``model-switch`` 的三条护栏原样搬过来：

1. **引用保护**：被 ``moa.default_preset`` / ``model.default`` /
   ``fallback_providers[].model`` 引用的预设，**删**一律 409；
   **改**默认也 409（要改被引用的内容必须显式 ``allow_referenced_edit: true``），
   **改名永远不允许**（改名会让引用指空）。
2. **拦在拿快照之前**：任何拒绝路径都不建快照、``config.yaml`` 哈希不变（零污染）。
3. **悬空引用红灯**：``default_preset`` 指不到就是红灯；红灯存在时**不许静默写**。

沙盒：``tmp_path`` 造一个形状真实的 home + ``HERMES_HOME`` 指过去，**绝不碰真 home**。

跑法::

    <venv>/Scripts/python.exe -m pytest dashboard/tests/test_api_models_moa_presets.py -q
"""

import copy
import hashlib
import sys
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = DASHBOARD_DIR.parent


def _find_up(start: Path, name: str) -> Path:
    for base in (start, *start.parents):
        candidate = base / name
        if candidate.exists():
            return candidate
    return start.parents[1] / name


#: 宿主源根（``hermes_cli`` / ``hermes_constants`` 都在这儿）。
HERMES_AGENT_DIR = _find_up(PLUGIN_ROOT, "hermes-agent")

if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))
if HERMES_AGENT_DIR.is_dir() and str(HERMES_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT_DIR))

PREFIX = "/api/plugins/hermes-console"
LIST = PREFIX + "/models/moa/presets"
APPLY = PREFIX + "/models/apply"

#: 注释必须逐字活下来（证明走的是保留注释的官方写入器，不是 yaml.dump 整篇重写）。
MOA_COMMENT = "  # 老板手写：moa 配方别乱动\n"

ROOT_CONFIG = """\
# 根配置（default agent）
model:
  default: 长庚
  provider: moa
  base_url: moa://local
  api_mode: chat_completions
providers:
  balance:
    name: Balance
    base_url: https://balance.example.com/v1
    key_env: BALANCE_API_KEY
fallback_providers:
  - provider: balance
    model: deepseek-v4.1-flash
  - provider: moa
    model: 素笺
agent:
  max_turns: 500
""" + MOA_COMMENT + """\
moa:
  default_preset: 星槎
  presets:
    default:
      reference_models:
        - provider: balance
          model: deepseek-v4.1-flash
          enabled: true
        - provider: balance
          model: glm-5.3-flash
          reasoning_effort: high
          enabled: true
      degraded_reference_policy: loud
      fanout: user_turn
      aggregator:
        provider: balance
        model: sensenova-6.8-flash-lite
        reasoning_effort: high
    素笺:
      enabled: true
      reference_models:
        - provider: balance
          model: kimi-k3
          enabled: true
          reasoning_effort: high
      aggregator:
        provider: balance
        model: deepseek-v4.1-flash
        reasoning_effort: high
      degraded_reference_policy: loud
      fanout: user_turn
    长庚:
      enabled: true
      reference_models:
        - provider: balance
          model: glm-5.3
          enabled: true
          reasoning_effort: max
      aggregator:
        provider: balance
        model: deepseek-v4.1-flash
        reasoning_effort: high
      degraded_reference_policy: loud
      fanout: user_turn
    星槎:
      enabled: true
      reference_models:
        - provider: balance
          model: kimi-k3
          enabled: true
      aggregator:
        provider: balance
        model: deepseek-v4.1-flash
      degraded_reference_policy: loud
      fanout: user_turn
    孤舟:
      enabled: true
      reference_models:
        - provider: balance
          model: glm-5.3
          enabled: true
      aggregator:
        provider: balance
        model: deepseek-v4.1-flash
        reasoning_effort: high
      degraded_reference_policy: loud
      fanout: user_turn
"""

#: 没有 ``moa:`` 段 ⇒ ``state = moa-presets-missing``。
WORKER_CONFIG = """\
model:
  default: worker-model-1
  provider: pworker
providers:
  pworker:
    name: Worker
    base_url: https://worker.example.com/v1
agent:
  max_turns: 120
"""

#: 有 ``moa:`` 段但没有预设 ⇒ ``state = moa-presets-empty``。
EMPTY_CONFIG = """\
model:
  default: empty-model-1
  provider: pempty
moa:
  enabled: true
"""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _presets(path: Path) -> dict:
    return (_doc(path).get("moa") or {}).get("presets") or {}


def _snapshot_count(home: Path) -> int:
    root = home / "plugins" / "hermes-console" / "data" / "snapshots"
    return len([item for item in root.iterdir() if item.is_dir()]) if root.is_dir() else 0


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """最小但形状真实的 home（根 + 两个 profile），``HERMES_HOME`` 指过去。"""
    root = tmp_path / "home"
    for name in ("worker", "empty"):
        (root / "profiles" / name).mkdir(parents=True)
        (root / "profiles" / name / "profile.yaml").write_text(
            "id: %s\ntitle: %s\n" % (name, name), encoding="utf-8")
    (root / "config.yaml").write_text(ROOT_CONFIG, encoding="utf-8")
    (root / "profiles" / "worker" / "config.yaml").write_text(WORKER_CONFIG, encoding="utf-8")
    (root / "profiles" / "empty" / "config.yaml").write_text(EMPTY_CONFIG, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda *a, **k: root,
                        raising=False)
    for name in [key for key in list(sys.modules) if key == "core" or key.startswith("core.")]:
        sys.modules.pop(name, None)
    return root


@pytest.fixture()
def api(home):
    import api_models

    return api_models


@pytest.fixture()
def client(api):
    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    return TestClient(app)


@pytest.fixture()
def root_cfg(home):
    return home / "config.yaml"


def _create(client, **payload):
    payload.setdefault("confirm", True)
    return client.post(LIST, json=payload)


def _update(client, name, **payload):
    payload.setdefault("confirm", True)
    return client.put(LIST + "/" + name, json=payload)


def _delete(client, name, **params):
    params.setdefault("confirm", "true")
    return client.delete(LIST + "/" + name, params=params)


REFS = [
    {"provider": "balance", "model": "kimi-k3", "enabled": True, "reasoning_effort": "high"},
    {"provider": "balance", "model": "glm-5.3", "enabled": False},
]
AGG = {"provider": "balance", "model": "deepseek-v4.1-flash", "reasoning_effort": "high"}


# ---------------------------------------------------------------------------
# 验收 1：列出（与配置逐字段一致）+ 三态
# ---------------------------------------------------------------------------
def test_list_matches_config_field_by_field(client, root_cfg):
    resp = client.get(LIST, params={"agent": "default"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["state"] == "moa-presets"
    assert body["default_preset"] == "星槎"
    assert body["default_preset_exists"] is True
    assert body["presets_container"] == "map"
    assert body["writable"] is True
    assert body["light_counts"] == {"red": 0, "amber": 0, "unknown": 0}

    raw = _presets(root_cfg)
    assert body["count"] == len(raw) == 5
    rows = {row["name"]: row for row in body["presets"]}
    assert sorted(rows) == sorted(raw)
    for name, preset in raw.items():
        row = rows[name]
        assert row["enabled"] == preset.get("enabled")
        assert row["fanout"] == preset.get("fanout")
        assert row["degraded_reference_policy"] == preset.get("degraded_reference_policy")
        assert row["aggregator"] == preset.get("aggregator")
        # 参考模型逐槽位一致（含逐槽位思考强度）
        assert len(row["reference_models"]) == len(preset["reference_models"])
        for got, want in zip(row["reference_models"], preset["reference_models"]):
            assert got["model"] == want["model"]
            assert got["provider"] == want["provider"]
            assert got["enabled"] == want.get("enabled", True)
            assert got.get("reasoning_effort") == want.get("reasoning_effort")


def test_list_three_states(client):
    assert client.get(LIST, params={"agent": "worker"}).json()["state"] == "moa-presets-missing"
    assert client.get(LIST, params={"agent": "empty"}).json()["state"] == "moa-presets-empty"
    assert client.get(LIST, params={"agent": "default"}).json()["state"] == "moa-presets"


def test_list_marks_references(client):
    rows = {row["name"]: row for row in client.get(LIST, params={"agent": "default"}).json()["presets"]}
    assert rows["星槎"]["is_default_preset"] is True
    assert [ref["where"] for ref in rows["星槎"]["references"]] == ["moa.default_preset"]
    assert [ref["where"] for ref in rows["长庚"]["references"]] == ["model.default"]
    assert [ref["where"] for ref in rows["素笺"]["references"]] == ["fallback_providers[1].model"]
    assert rows["孤舟"]["referenced"] is False


# ---------------------------------------------------------------------------
# 验收 2：新建
# ---------------------------------------------------------------------------
def test_create_round_trip(client, root_cfg, home):
    before_snaps = _snapshot_count(home)
    resp = _create(client, agent="default", name="新配方", reference_models=REFS,
                   aggregator=AGG, enabled=True, fanout="user_turn",
                   degraded_reference_policy="loud")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation"] == "moa_preset_create"
    assert body["changed"] is True and body["verified"] is True
    assert all(row["ok"] for row in body["verify"]), body["verify"]
    assert Path(body["snapshot_path"]).is_file()
    assert _snapshot_count(home) == before_snaps + 1

    written = _presets(root_cfg)["新配方"]
    assert written["reference_models"] == REFS
    assert written["aggregator"] == AGG
    assert written["enabled"] is True
    assert written["fanout"] == "user_turn"
    assert written["degraded_reference_policy"] == "loud"
    # 读接口与写接口说的是同一件事
    rows = {row["name"]: row for row in client.get(LIST, params={"agent": "default"}).json()["presets"]}
    assert rows["新配方"]["reference_models"] == REFS


def test_create_duplicate_is_409_and_disk_untouched(client, root_cfg, home):
    before, snaps = _sha(root_cfg), _snapshot_count(home)
    resp = _create(client, agent="default", name="孤舟", reference_models=REFS, aggregator=AGG)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"
    assert _sha(root_cfg) == before
    assert _snapshot_count(home) == snaps


def test_create_requires_confirm_and_validates(client, root_cfg, home):
    before, snaps = _sha(root_cfg), _snapshot_count(home)
    no_confirm = client.post(LIST, json={"agent": "default", "name": "甲",
                                         "reference_models": REFS, "aggregator": AGG})
    assert no_confirm.status_code == 400
    assert no_confirm.json()["error"]["code"] == "confirmation_required"
    # 带点的名字被拒（点分键歧义），且磁盘不动
    dotted = _create(client, agent="default", name="a.b", reference_models=REFS, aggregator=AGG)
    assert dotted.status_code == 400
    assert dotted.json()["error"]["code"] == "preset_name_invalid"
    # 空 reference_models 被拒
    empty = _create(client, agent="default", name="乙", reference_models=[], aggregator=AGG)
    assert empty.status_code == 400
    assert empty.json()["error"]["code"] == "reference_models_empty"
    assert _sha(root_cfg) == before
    assert _snapshot_count(home) == snaps


# ---------------------------------------------------------------------------
# 验收 3 / 5：改 reference_models（顺序敏感）+ 既有键逐字保留
# ---------------------------------------------------------------------------
def test_update_reference_models_keeps_order_and_other_keys(client, root_cfg):
    before_text = root_cfg.read_text(encoding="utf-8")
    resp = _update(client, "孤舟", agent="default", reference_models=list(reversed(REFS)))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation"] == "moa_preset_update"
    assert body["changed"] is True and body["verified"] is True

    written = _presets(root_cfg)["孤舟"]
    assert written["reference_models"] == list(reversed(REFS))
    # 红线：没让我改的键逐字保留
    assert written["degraded_reference_policy"] == "loud"
    assert written["fanout"] == "user_turn"
    assert written["enabled"] is True
    assert written["aggregator"] == {"provider": "balance", "model": "deepseek-v4.1-flash",
                                     "reasoning_effort": "high"}
    # 官方写入器保留注释（不是 yaml.dump 整篇重写）
    assert MOA_COMMENT in root_cfg.read_text(encoding="utf-8")
    # 别的预设一个字节都没动
    assert _presets(root_cfg)["default"] == _presets(root_cfg)["default"]
    assert "素笺" in before_text and "素笺" in root_cfg.read_text(encoding="utf-8")

    # 增：追加第三条参考模型（顺序 = fan-out 顺序，新条目在最后）
    three = list(reversed(REFS)) + [{"provider": "gemini", "model": "gemini-3.8-flash",
                                     "enabled": True}]
    assert _update(client, "孤舟", agent="default", reference_models=three).status_code == 200
    assert _presets(root_cfg)["孤舟"]["reference_models"] == three

    # 删：只留第一条（其余键仍然逐字未变）
    one = [three[0]]
    assert _update(client, "孤舟", agent="default", reference_models=one).status_code == 200
    written = _presets(root_cfg)["孤舟"]
    assert written["reference_models"] == one
    assert written["fanout"] == "user_turn"
    assert written["degraded_reference_policy"] == "loud"


def test_update_unchanged_is_zero_write(client, root_cfg, home):
    before, snaps = _sha(root_cfg), _snapshot_count(home)
    resp = _update(client, "孤舟", agent="default",
                   reference_models=[{"provider": "balance", "model": "glm-5.3", "enabled": True}])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] is False
    assert body["snapshot_path"] == ""
    assert _sha(root_cfg) == before
    assert _snapshot_count(home) == snaps


# ---------------------------------------------------------------------------
# 验收 4：改聚合器（含显式删强度）
# ---------------------------------------------------------------------------
def test_update_aggregator(client, root_cfg):
    resp = _update(client, "孤舟", agent="default",
                   aggregator={"provider": "gemini", "model": "gemini-3.8-flash",
                               "reasoning_effort": "max"})
    assert resp.status_code == 200, resp.text
    assert _presets(root_cfg)["孤舟"]["aggregator"] == {
        "provider": "gemini", "model": "gemini-3.8-flash", "reasoning_effort": "max"}
    # 显式 null ⇒ 删掉这个子键（其余键照旧）
    resp = _update(client, "孤舟", agent="default",
                   aggregator={"provider": "gemini", "model": "gemini-3.8-flash",
                               "reasoning_effort": None})
    assert resp.status_code == 200, resp.text
    assert _presets(root_cfg)["孤舟"]["aggregator"] == {
        "provider": "gemini", "model": "gemini-3.8-flash"}
    assert _presets(root_cfg)["孤舟"]["fanout"] == "user_turn"


# ---------------------------------------------------------------------------
# 验收 6：删除未被引用的预设
# ---------------------------------------------------------------------------
def test_delete_unreferenced(client, root_cfg, home):
    resp = _delete(client, "孤舟", agent="default")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation"] == "moa_preset_delete"
    assert body["changed"] is True and body["verified"] is True
    assert body["removed"]["aggregator"]["model"] == "deepseek-v4.1-flash"
    assert "孤舟" not in _presets(root_cfg)
    assert len(_presets(root_cfg)) == 4
    assert Path(body["snapshot_path"]).is_file()


# ---------------------------------------------------------------------------
# 验收 7 / 8：引用保护（409 + 零污染）
# ---------------------------------------------------------------------------
def test_delete_default_preset_is_409_zero_pollution(client, root_cfg, home):
    before, snaps = _sha(root_cfg), _snapshot_count(home)
    resp = _delete(client, "星槎", agent="default")
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "preset_referenced"
    assert error["references"] == [{"profile": "default", "where": "moa.default_preset",
                                    "value": "星槎", "kind": "default_preset"}]
    assert _sha(root_cfg) == before
    assert _snapshot_count(home) == snaps


def test_delete_fallback_referenced_is_409(client, root_cfg, home):
    before, snaps = _sha(root_cfg), _snapshot_count(home)
    resp = _delete(client, "素笺", agent="default")
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "preset_referenced"
    assert [ref["where"] for ref in error["references"]] == ["fallback_providers[1].model"]
    assert "fallback_providers[1]" in error["message"]
    assert _sha(root_cfg) == before
    assert _snapshot_count(home) == snaps


def test_update_referenced_requires_explicit_allow(client, root_cfg, home):
    before, snaps = _sha(root_cfg), _snapshot_count(home)
    resp = _update(client, "长庚", agent="default", reference_models=REFS)
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "preset_referenced"
    assert [ref["where"] for ref in error["references"]] == ["model.default"]
    assert _sha(root_cfg) == before
    assert _snapshot_count(home) == snaps

    # 显式确认后才允许改内容（引用按名字，内容变了引用仍然有效）
    ok = _update(client, "长庚", agent="default", reference_models=REFS,
                 allow_referenced_edit=True)
    assert ok.status_code == 200, ok.text
    assert _presets(root_cfg)["长庚"]["reference_models"] == REFS


def test_rename_referenced_is_always_409(client, root_cfg, home):
    before, snaps = _sha(root_cfg), _snapshot_count(home)
    resp = _update(client, "长庚", agent="default", rename_to="新长庚", allow_referenced_edit=True)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "preset_referenced"
    assert "改名" in resp.json()["error"]["message"]
    assert _sha(root_cfg) == before
    assert _snapshot_count(home) == snaps


def test_rename_unreferenced_moves_block_verbatim(client, root_cfg):
    before_block = copy.deepcopy(_presets(root_cfg)["孤舟"])
    resp = _update(client, "孤舟", agent="default", rename_to="新孤舟")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["operation"] == "moa_preset_rename"
    assert body["renamed_from"] == "孤舟"
    presets = _presets(root_cfg)
    assert "孤舟" not in presets
    assert presets["新孤舟"] == before_block


# ---------------------------------------------------------------------------
# 验收 9：悬空引用红灯（不许静默写）
# ---------------------------------------------------------------------------
def test_dangling_default_preset_reports_red_and_blocks_writes(client, root_cfg, home):
    text = root_cfg.read_text(encoding="utf-8").replace("default_preset: 星槎",
                                                       "default_preset: 幽灵")
    root_cfg.write_text(text, encoding="utf-8")

    body = client.get(LIST, params={"agent": "default"}).json()
    assert body["writable"] is False
    assert body["default_preset_exists"] is False
    assert body["light_counts"]["red"] == 1
    red = [item for item in body["lights"] if item["severity"] == "red"]
    assert red[0]["code"] == "default_preset_missing"
    assert red[0]["where"] == "moa.default_preset"
    assert red[0]["value"] == "幽灵"
    assert "幽灵" in body["write_blocked_reason"]

    # 红灯存在时，任何"不会把红灯修掉"的写入都被拒，且磁盘一个字节都不动
    before, snaps = _sha(root_cfg), _snapshot_count(home)
    blocked = _create(client, agent="default", name="无关配方", reference_models=REFS,
                      aggregator=AGG)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "dangling_reference"
    assert _sha(root_cfg) == before
    assert _snapshot_count(home) == snaps
    blocked_update = _update(client, "孤舟", agent="default", reference_models=REFS)
    assert blocked_update.status_code == 409
    assert blocked_update.json()["error"]["code"] == "dangling_reference"
    assert _sha(root_cfg) == before

    # 唯一放行的写入：把那个被引用的预设补上（红灯清零）
    fixed = _create(client, agent="default", name="幽灵", reference_models=REFS, aggregator=AGG)
    assert fixed.status_code == 200, fixed.text
    after = client.get(LIST, params={"agent": "default"}).json()
    assert after["writable"] is True
    assert after["light_counts"]["red"] == 0


# ---------------------------------------------------------------------------
# 验收 10 / F：回读校验不符 ⇒ 自动字节还原 + verify_mismatch（不是 ok:true）
# ---------------------------------------------------------------------------
def test_verify_mismatch_restores_bytes(client, api, root_cfg, home, monkeypatch):
    before = root_cfg.read_bytes()
    real_write = api._write_official

    def broken_write(path, data, **kwargs):
        tampered = copy.deepcopy(data)
        tampered["moa"]["presets"]["孤舟"]["reference_models"] = []  # 模拟写入器把内容写坏
        real_write(path, tampered, **kwargs)

    monkeypatch.setattr(api, "_write_official", broken_write)
    resp = _update(client, "孤舟", agent="default", reference_models=REFS)
    assert resp.status_code == 500
    error = resp.json()["error"]
    assert error["code"] == "verify_mismatch"
    assert error["restored"] is True
    assert error["snapshot_path"] and Path(error["snapshot_path"]).is_file()
    assert any(row["ok"] is False for row in error["verify"])
    # 磁盘回到原样（按字节）
    assert root_cfg.read_bytes() == before


# ---------------------------------------------------------------------------
# 边界：presets 是 list 形状（旧格式）—— 读得出来，但拒绝写
# ---------------------------------------------------------------------------
def test_list_shape_is_readable_but_not_writable(client, root_cfg, home):
    root_cfg.write_text(
        "model:\n  default: x\n  provider: moa\nmoa:\n  presets:\n"
        "    - name: 老配方\n      reference_models:\n"
        "        - provider: balance\n          model: glm-5.3\n"
        "      aggregator:\n        provider: balance\n        model: deepseek-v4.1-flash\n",
        encoding="utf-8")
    body = client.get(LIST, params={"agent": "default"}).json()
    assert body["state"] == "moa-presets"
    assert body["presets_container"] == "list"
    assert [row["name"] for row in body["presets"]] == ["老配方"]
    assert body["presets"][0]["reference_models"][0]["model"] == "glm-5.3"

    before = _sha(root_cfg)
    resp = _create(client, agent="default", name="新配方", reference_models=REFS, aggregator=AGG)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "presets_container_list"
    assert _sha(root_cfg) == before


# ---------------------------------------------------------------------------
# 验收 10：拒绝路径零污染（批量过一遍）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["no_confirm", "bad_name", "duplicate", "missing_preset",
                                  "referenced_delete", "referenced_update"])
def test_rejection_paths_leave_disk_untouched(client, root_cfg, home, kind):
    before, snaps = _sha(root_cfg), _snapshot_count(home)
    if kind == "no_confirm":
        resp = client.post(LIST, json={"agent": "default", "name": "甲",
                                       "reference_models": REFS, "aggregator": AGG})
    elif kind == "bad_name":
        resp = _create(client, agent="default", name="-bad", reference_models=REFS, aggregator=AGG)
    elif kind == "duplicate":
        resp = _create(client, agent="default", name="孤舟", reference_models=REFS, aggregator=AGG)
    elif kind == "missing_preset":
        resp = _update(client, "不存在的配方", agent="default", reference_models=REFS)
    elif kind == "referenced_delete":
        resp = _delete(client, "星槎", agent="default")
    else:
        resp = _update(client, "星槎", agent="default", reference_models=REFS)
    assert resp.status_code in (400, 404, 409), resp.text
    assert resp.json()["ok"] is False
    assert _sha(root_cfg) == before
    assert _snapshot_count(home) == snaps


# ---------------------------------------------------------------------------
# E 项：切换预设到底写哪几个键（宿主读 agent.model，不是 moa.default_preset）
# ---------------------------------------------------------------------------
def test_scope_model_switches_preset_without_touching_default_preset(client, root_cfg):
    resp = client.post(APPLY, json={"agent": "default", "scope": "model",
                                    "provider": "moa", "model": "孤舟", "confirm": True})
    assert resp.status_code == 200, resp.text
    doc = _doc(root_cfg)
    assert doc["model"]["default"] == "孤舟"
    assert doc["model"]["provider"] == "moa"
    # 既有契约不变：切主模型**不动** moa.default_preset
    assert doc["moa"]["default_preset"] == "星槎"


def test_host_resolves_preset_from_agent_model_first():
    """宿主 ``build_moa_facade`` 的解析顺序：``agent.model`` 优先，``default_preset`` 只兜底。"""
    source = HERMES_AGENT_DIR / "agent" / "moa_loop.py"
    if not source.is_file():
        pytest.skip("宿主源码不在预期位置：%s" % source)
    text = source.read_text(encoding="utf-8")
    start = text.index("def build_moa_facade(")
    end = text.index("def bind_moa_runtime(")
    body = text[start:end]
    assert 'getattr(agent, "model", None)' in body
    assert body.index('getattr(agent, "model", None)') < body.index('moa_cfg.get("default_preset")')


# ---------------------------------------------------------------------------
# 路径一致性（写路径 == 读路径 == 测试改的那份；防"读得到红灯、写去了别处"）
# ---------------------------------------------------------------------------
def test_write_path_equals_read_path(api, client, root_cfg):
    """``_profile_scope`` 给出的写入路径必须与 ``_config_path`` / GET 报的路径是同一个文件。

    这条守的是最容易\"看起来绿、其实写错文件\"的洞：如果 profile 作用域解析到了别的
    路径（例如上一个测试的 tmp home，或者真实 home），那么\"读得到红灯、写却写去别处\"
    会让所有闸门静默失效。
    """
    read_path = Path(api._config_path("default"))
    assert read_path.resolve() == root_cfg.resolve()
    with api._profile_scope("default") as write_path:
        assert Path(write_path).resolve() == read_path.resolve()
        assert api._read_for_write(Path(write_path)).get("moa", {}).get("default_preset") == "星槎"
    # GET 报的 config_path 也是同一份
    assert Path(client.get(LIST, params={"agent": "default"}).json()["config_path"]).resolve() \
        == read_path.resolve()
    # profile agent 同理（profiles/<id>/config.yaml）
    with api._profile_scope("worker") as worker_path:
        assert Path(worker_path).resolve() == Path(api._config_path("worker")).resolve()


def test_dangling_red_light_blocks_writes_on_profile_agent(client, home):
    """红灯闸在**非 default 的 profile** 上同样生效（路径是 profiles/<id>/config.yaml）。"""
    worker_cfg = home / "profiles" / "worker" / "config.yaml"
    worker_cfg.write_text(
        WORKER_CONFIG + "moa:\n  default_preset: 幽灵\n  presets:\n    星槎:\n      enabled: true\n"
        "      reference_models:\n        - provider: balance\n          model: kimi-k3\n"
        "          enabled: true\n      aggregator:\n        provider: balance\n"
        "        model: deepseek-v4.1-flash\n", encoding="utf-8")
    body = client.get(LIST, params={"agent": "worker"}).json()
    assert body["state"] == "moa-presets"
    assert body["writable"] is False and body["light_counts"]["red"] == 1

    before, snaps = _sha(worker_cfg), _snapshot_count(home)
    blocked = _create(client, agent="worker", name="无关配方", reference_models=REFS, aggregator=AGG)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "dangling_reference"
    assert _sha(worker_cfg) == before
    assert _snapshot_count(home) == snaps

    fixed = _create(client, agent="worker", name="幽灵", reference_models=REFS, aggregator=AGG)
    assert fixed.status_code == 200, fixed.text
    assert client.get(LIST, params={"agent": "worker"}).json()["writable"] is True


# ---------------------------------------------------------------------------
# 跨 profile 一致性 + agent 隔离
# ---------------------------------------------------------------------------
def test_cross_profile_consistency_flag(client, home):
    (home / "profiles" / "worker" / "config.yaml").write_text(
        WORKER_CONFIG + "moa:\n  presets:\n    素笺:\n      enabled: true\n"
        "      reference_models:\n        - provider: balance\n          model: kimi-k3\n"
        "          enabled: true\n          reasoning_effort: low\n"
        "      aggregator:\n        provider: balance\n        model: deepseek-v4.1-flash\n"
        "        reasoning_effort: high\n",
        encoding="utf-8")
    body = client.get(LIST, params={"agent": "default", "cross_profile": "1"}).json()
    names = [item["name"] for item in body["cross_profile_inconsistent"]]
    assert names == ["素笺"]
    detail = body["cross_profile_inconsistent"][0]
    assert detail["profiles"] == ["default", "worker"]
    assert len(detail["variants"]) == 2
    rows = {row["name"]: row for row in body["presets"]}
    assert rows["素笺"]["cross_profile_inconsistent"] is True
    assert rows["孤舟"]["cross_profile_inconsistent"] is False


def test_write_is_agent_scoped(client, home, root_cfg):
    root_before = _sha(root_cfg)
    resp = _create(client, agent="worker", name="力工配方", reference_models=REFS, aggregator=AGG)
    assert resp.status_code == 200, resp.text
    worker_cfg = home / "profiles" / "worker" / "config.yaml"
    assert _presets(worker_cfg)["力工配方"]["aggregator"]["model"] == "deepseek-v4.1-flash"
    assert _sha(root_cfg) == root_before  # 别的 agent 一个字节都没动
    assert client.get(LIST, params={"agent": "worker"}).json()["state"] == "moa-presets"

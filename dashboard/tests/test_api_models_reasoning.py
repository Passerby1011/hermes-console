# -*- coding: utf-8 -*-
"""模型分区「思考强度可写」（``scope=reasoning``）契约测试。

本卡（t_03cb0cba）把 ``agent.reasoning_effort`` 从**只读**变成**可写**，同时把老板那条
红线原样保留：**换布局不许顺带覆盖推理强度**。所以这里的断言分两半：

* 正面：8 个合法值写得进去、回读一致、快照可回滚、非法值 400 且磁盘不动；
* 反面（**最重要的两条**）：写 ``reasoning`` 时别的段逐字段未变；写 ``model`` /
  ``fallback`` / ``fallbacks`` / ``auxiliary`` 时 ``agent.reasoning_effort`` **逐字节未变**
  （连那一行的注释都得在）。

沙盒：``tmp_path`` 造一个形状真实的 home + ``HERMES_HOME`` 指过去，**绝不碰真 home**。

跑法::

    <venv>/Scripts/python.exe -m pytest dashboard/tests/test_api_models_reasoning.py -q
"""

import copy
import hashlib
import json
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


#: 宿主源根（``hermes_cli`` / ``hermes_constants`` / ``hermes_yaml`` 都在这儿）。
HERMES_AGENT_DIR = _find_up(PLUGIN_ROOT, "hermes-agent")

if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))
if HERMES_AGENT_DIR.is_dir() and str(HERMES_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT_DIR))

PREFIX = "/api/plugins/hermes-console"
APPLY = PREFIX + "/models/apply"
OVERVIEW = PREFIX + "/models/overview"

#: 8 个合法取值：关闭 + 宿主 7 个等级（顺序固定，界面下拉就照这个顺序）。
ALL_VALUES = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
#: 这一行**必须逐字节活下来**（注释也算）：它就是老板那条红线的可观察形态。
REASONING_LINE = "  # 老板定的：换布局不许顺带覆盖这一行\n  reasoning_effort: high\n"

ROOT_CONFIG = """\
# 根配置（default agent）
model:
  default: alpha-model-1
  provider: palpha
  base_url: https://alpha.example.com/v1
  api_mode: chat_completions
  key_env: ALPHA_API_KEY
providers:
  palpha:
    name: Alpha
    base_url: https://alpha.example.com/v1
    key_env: ALPHA_API_KEY
fallback_providers:
  - provider: palpha
    model: alpha-backup
    reasoning_effort: max
auxiliary:
  approval:
    provider: palpha
    model: alpha-small
    reasoning_effort: none
  compression:
    provider: auto
    model: ''
agent:
  max_turns: 500
  service_tier: normal
  image_input_mode: native
  verbose: false
""" + REASONING_LINE + """\
  personalities: {}
"""

WORKER_CONFIG = """\
model:
  default: worker-model-1
  provider: pworker
  base_url: https://worker.example.com/v1
  api_mode: chat_completions
providers:
  pworker:
    name: Worker
    base_url: https://worker.example.com/v1
agent:
  max_turns: 120
  reasoning_effort: medium
"""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """最小但形状真实的 home（根 + 一个 profile），``HERMES_HOME`` 指过去。"""
    root = tmp_path / "home"
    (root / "profiles" / "worker").mkdir(parents=True)
    (root / "config.yaml").write_text(ROOT_CONFIG, encoding="utf-8")
    (root / "profiles" / "worker" / "config.yaml").write_text(WORKER_CONFIG, encoding="utf-8")
    for name in ("worker",):
        (root / "profiles" / name / "profile.yaml").write_text(
            "id: %s\ntitle: %s\n" % (name, name), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    # 官方解析器返回 Path（`hermes_cli/profiles.py` 会直接 `/ "profiles"`）
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


@pytest.fixture()
def worker_cfg(home):
    return home / "profiles" / "worker" / "config.yaml"


def _apply(client, **payload):
    return client.post(APPLY, json=payload)


def _write_reasoning(client, value, agent="default"):
    return _apply(client, agent=agent, scope="reasoning", reasoning_effort=value, confirm=True)


def _reasoning_of(path: Path) -> str:
    return str((_doc(path).get("agent") or {}).get("reasoning_effort") or "")


# ---------------------------------------------------------------------------
# 1 / 3. 写入 + 回读（含 8 个合法值逐个走一遍）
# ---------------------------------------------------------------------------
def test_set_high_persists_to_disk(client, root_cfg):
    # 先把磁盘上的值改掉，再写 high（否则"值没变"会走零写分支，那是另一条用例）
    assert _write_reasoning(client, "low").json()["changed"] is True
    resp = _write_reasoning(client, "high")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["scope"] == "reasoning"
    assert body["field"] == "agent.reasoning_effort"
    assert body["reasoning_effort"] == "high"
    assert body["changed"] is True
    # 磁盘回读
    assert _reasoning_of(root_cfg) == "high"
    # 回读校验明细逐条 ok
    assert [item for item in body["verify"] if item["key"] == "agent.reasoning_effort"
            and item["ok"] is True and item["actual"] == "high"]
    # 快照路径写进响应（沿用 /apply 既有契约）
    assert body["snapshot_path"] and Path(body["snapshot_path"]).is_file()
    assert body["snapshot_ts"]
    # applied / previous 两份状态都带这个字段
    assert body["applied"]["reasoning_effort"] == "high"
    assert body["previous"]["reasoning_effort"] == "low"
    # diff 就一行，就是它
    assert body["diff"] == [{"key": "agent.reasoning_effort", "from": "low", "to": "high"}]


@pytest.mark.parametrize("value", ALL_VALUES)
def test_every_legal_value_round_trips(client, root_cfg, value):
    resp = _write_reasoning(client, value)
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert _reasoning_of(root_cfg) == value
    # 再读一次 overview：读接口与写接口说的是同一件事
    row = client.get(OVERVIEW, params={"agent": "default"}).json()["rows"][0]
    assert row["reasoning_effort"] == value


def test_normalizes_case_and_spaces(client, root_cfg):
    resp = _write_reasoning(client, "  HIGH  ")
    assert resp.status_code == 200, resp.text
    assert _reasoning_of(root_cfg) == "high"


def test_fields_alias_is_equivalent(client, root_cfg):
    resp = _apply(client, agent="default", scope="reasoning",
                  fields={"reasoning_effort": "max"}, confirm=True)
    assert resp.status_code == 200, resp.text
    assert _reasoning_of(root_cfg) == "max"


def test_conflicting_values_rejected(client, root_cfg):
    before = _sha(root_cfg)
    resp = _apply(client, agent="default", scope="reasoning", reasoning_effort="high",
                  fields={"reasoning_effort": "low"}, confirm=True)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_argument"
    assert _sha(root_cfg) == before


# ---------------------------------------------------------------------------
# 2. none = 关闭语义（按 parse_reasoning_effort 的实际解释断言）
# ---------------------------------------------------------------------------
def test_none_means_disabled_per_host_parser(client, root_cfg):
    from hermes_constants import parse_reasoning_effort, resolve_reasoning_config

    resp = _write_reasoning(client, "none")
    assert resp.status_code == 200, resp.text
    # 落盘形态：就是字符串 "none"（宿主解析器负责解释成"关闭"）
    assert _reasoning_of(root_cfg) == "none"
    assert parse_reasoning_effort(_reasoning_of(root_cfg)) == {"enabled": False}
    # 宿主真正的入口读出来也是关闭
    assert resolve_reasoning_config(_doc(root_cfg), "alpha-model-1") == {"enabled": False}
    # 响应里说明了关闭语义
    assert resp.json()["applied"]["reasoning_effort"] == "none"
    assert any("关闭" in item for item in resp.json()["warnings"])


# ---------------------------------------------------------------------------
# 4. 非法值 ⇒ 400 value_rejected，磁盘原值不动、连快照都不做
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["banana", "", "false", "disabled", "HIGHEST", True, 3])
def test_illegal_value_rejected_without_touching_disk(client, api, root_cfg, bad):
    before = _sha(root_cfg)
    resp = _write_reasoning(client, bad)
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["code"] == "value_rejected", json.dumps(error, ensure_ascii=False)
    assert error["allowed_values"] == ALL_VALUES
    # 磁盘原值未变（"不得静默写成默认值"）
    assert _reasoning_of(root_cfg) == "high"
    assert _sha(root_cfg) == before
    # 校验发生在快照之前：一个快照都不许做
    assert not (home_snapshots := api._snapshots_root()).exists() or not any(home_snapshots.iterdir())


def test_missing_confirm_still_required(client, root_cfg):
    before = _sha(root_cfg)
    resp = _apply(client, agent="default", scope="reasoning", reasoning_effort="low")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "confirmation_required"
    assert _sha(root_cfg) == before


# ---------------------------------------------------------------------------
# 5. 写 reasoning ⇒ 别的段逐字段未变（隔离）
# ---------------------------------------------------------------------------
def test_reasoning_write_does_not_touch_other_sections(client, root_cfg):
    before = copy.deepcopy(_doc(root_cfg))
    resp = _write_reasoning(client, "ultra")
    assert resp.status_code == 200, resp.text
    after = _doc(root_cfg)
    for section in ("model", "providers", "fallback_providers", "auxiliary"):
        assert after[section] == before[section], section
    # agent 段里只有 reasoning_effort 变，别的键一个都没动
    assert after["agent"]["max_turns"] == before["agent"]["max_turns"]
    assert after["agent"]["service_tier"] == before["agent"]["service_tier"]
    assert after["agent"]["personalities"] == before["agent"]["personalities"]
    assert after["agent"]["reasoning_effort"] == "ultra"
    assert {k for k in after["agent"]} == {k for k in before["agent"]}
    # 注释还在（原子写入器保留注释）
    assert "老板定的：换布局不许顺带覆盖这一行" in root_cfg.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 6. 反向隔离（老板那条红线的回归测试）：别的 scope 不许碰 agent.reasoning_effort
# ---------------------------------------------------------------------------
def _assert_reasoning_untouched(root_cfg: Path, before_agent: dict, before_text: str):
    text = root_cfg.read_text(encoding="utf-8")
    assert _doc(root_cfg)["agent"] == before_agent, "agent: 段被别的 scope 改了"
    assert REASONING_LINE in text, "reasoning_effort 那一行（含注释）被改写了"
    assert text.count("reasoning_effort: high") >= 1
    assert before_text.count("reasoning_effort: high") == text.count("reasoning_effort: high")


def test_model_scope_never_touches_reasoning(client, root_cfg):
    before_agent = copy.deepcopy(_doc(root_cfg)["agent"])
    before_text = root_cfg.read_text(encoding="utf-8")
    # 就算请求体里"顺手"带了 reasoning_effort，scope=model 也不许写它（只给一条提示）
    resp = _apply(client, agent="default", scope="model", provider="palpha",
                  model="alpha-model-2", reasoning_effort="ultra", confirm=True)
    assert resp.status_code == 200, resp.text
    assert _doc(root_cfg)["model"]["default"] == "alpha-model-2"
    _assert_reasoning_untouched(root_cfg, before_agent, before_text)
    assert resp.json()["applied"]["reasoning_effort"] == "high"
    assert any("scope=reasoning" in item for item in resp.json()["warnings"]), resp.json()["warnings"]


def test_auxiliary_scope_never_touches_reasoning(client, root_cfg):
    """★ 最容易串的地方：辅助槽位也有个 ``reasoning_effort``，改它绝不能顺手动全局。"""
    before_agent = copy.deepcopy(_doc(root_cfg)["agent"])
    before_text = root_cfg.read_text(encoding="utf-8")
    resp = _apply(client, agent="default", scope="auxiliary", slot="approval",
                  fields={"reasoning_effort": "low"}, confirm=True)
    assert resp.status_code == 200, resp.text
    assert _doc(root_cfg)["auxiliary"]["approval"]["reasoning_effort"] == "low"
    _assert_reasoning_untouched(root_cfg, before_agent, before_text)


def test_fallbacks_and_fallback_scopes_never_touch_reasoning(client, root_cfg):
    before_agent = copy.deepcopy(_doc(root_cfg)["agent"])
    before_text = root_cfg.read_text(encoding="utf-8")
    # ops 模式：只追加一条，没被点到的条目（含它自己的 reasoning_effort）原样保留
    resp = _apply(client, agent="default", scope="fallbacks", confirm=True,
                  ops=[{"op": "append", "provider": "palpha", "model": "b2"}])
    assert resp.status_code == 200, resp.text
    _assert_reasoning_untouched(root_cfg, before_agent, before_text)
    # 备用链条目自己的 reasoning_effort（max）也没被归一化顺手抹掉
    assert _doc(root_cfg)["fallback_providers"][0].get("reasoning_effort") == "max"

    # 整链替换 + 旧的首条语义：照样不许碰 agent.reasoning_effort
    resp = _apply(client, agent="default", scope="fallbacks", confirm=True,
                  fallback_models=[{"provider": "palpha", "model": "b1"},
                                   {"provider": "palpha", "model": "b2"}])
    assert resp.status_code == 200, resp.text
    _assert_reasoning_untouched(root_cfg, before_agent, before_text)
    resp = _apply(client, agent="default", scope="fallback", provider="palpha",
                  model="alpha-backup", confirm=True)
    assert resp.status_code == 200, resp.text
    _assert_reasoning_untouched(root_cfg, before_agent, before_text)
    assert _doc(root_cfg)["fallback_providers"][0] == {"provider": "palpha",
                                                       "model": "alpha-backup"}


# ---------------------------------------------------------------------------
# 7. 请求体不带该键 ⇒ 零写请求（哈希不变、不建快照）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("payload_extra", [{}, {"reasoning_effort": None}])
def test_missing_value_is_zero_write(client, api, root_cfg, payload_extra):
    before = _sha(root_cfg)
    resp = _apply(client, agent="default", scope="reasoning", confirm=True, **payload_extra)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["changed"] is False
    assert body["snapshot_path"] == "" and body["snapshot_ts"] == ""
    assert body["diff"] == []
    assert _sha(root_cfg) == before
    assert any("没带 reasoning_effort" in item for item in body["warnings"]), body["warnings"]
    snapshots = api._snapshots_root()
    assert not snapshots.exists() or not any(snapshots.iterdir())


def test_same_value_is_zero_write(client, api, root_cfg):
    before = _sha(root_cfg)
    resp = _write_reasoning(client, "high")
    assert resp.status_code == 200, resp.text
    assert resp.json()["changed"] is False
    assert _sha(root_cfg) == before


# ---------------------------------------------------------------------------
# 8. 写入/回读不符 ⇒ 自动回滚 + verify_mismatch（不是 ok:true）
# ---------------------------------------------------------------------------
def test_verify_mismatch_restores_snapshot(client, api, root_cfg, monkeypatch):
    before_sha = _sha(root_cfg)
    before_text = root_cfg.read_text(encoding="utf-8")
    original = api._write_official

    def tampering_write(path, data, **kwargs):
        tampered = copy.deepcopy(data)
        tampered["agent"]["reasoning_effort"] = "low"   # 写进去的不是我们要的
        original(path, tampered, **kwargs)

    monkeypatch.setattr(api, "_write_official", tampering_write)
    resp = _write_reasoning(client, "ultra")
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "verify_mismatch"
    assert body["error"]["restored"] is True
    assert body["error"]["restore_path"]
    # 失败明细里能看到"期望 ultra / 实际 low"
    assert body["error"]["verify"][0]["expected"] == "ultra"
    assert body["error"]["verify"][0]["actual"] == "low"
    # 磁盘按字节回到写之前（不是半截状态）
    assert _sha(root_cfg) == before_sha
    assert root_cfg.read_text(encoding="utf-8") == before_text


# ---------------------------------------------------------------------------
# 边界：config.yaml 里**还没有** agent: 段 ⇒ 只新建这一个键，别的段照旧
# ---------------------------------------------------------------------------
def test_creates_agent_block_with_only_reasoning_key(client, home):
    cfg = home / "profiles" / "worker" / "config.yaml"
    doc = _doc(cfg)
    doc.pop("agent", None)
    cfg.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    resp = _write_reasoning(client, "medium", agent="worker")
    assert resp.status_code == 200, resp.text
    after = _doc(cfg)
    assert after["agent"] == {"reasoning_effort": "medium"}
    assert after["model"]["default"] == "worker-model-1"


# ---------------------------------------------------------------------------
# 快照 → 回滚（既有安全链复用）
# ---------------------------------------------------------------------------
def test_snapshot_rollback_restores_previous_value(client, root_cfg):
    resp = _write_reasoning(client, "ultra")
    assert resp.status_code == 200, resp.text
    snapshot = resp.json()["snapshot_path"]
    assert _reasoning_of(root_cfg) == "ultra"
    rb = client.post(PREFIX + "/models/rollback",
                     json={"agent": "default", "snapshot": snapshot, "confirm": True})
    assert rb.status_code == 200, rb.text
    assert _reasoning_of(root_cfg) == "high"


# ---------------------------------------------------------------------------
# profile（非 default）也要能写：走同一个实现
# ---------------------------------------------------------------------------
def test_profile_agent_reasoning_write(client, worker_cfg, root_cfg):
    root_before = _sha(root_cfg)
    resp = _write_reasoning(client, "xhigh", agent="worker")
    assert resp.status_code == 200, resp.text
    assert _reasoning_of(worker_cfg) == "xhigh"
    # 根配置（default）一个字节都没动
    assert _sha(root_cfg) == root_before
    assert _doc(worker_cfg)["model"]["default"] == "worker-model-1"


# ---------------------------------------------------------------------------
# D. 读契约：overview 必须说明"这个字段可写"
# ---------------------------------------------------------------------------
def test_overview_declares_writable_contract(client, root_cfg):
    body = client.get(OVERVIEW, params={"agent": "default"}).json()
    assert body["scopes"] == ["model", "fallback", "fallbacks", "auxiliary", "reasoning"]
    assert body["reasoning_effort_values"] == ALL_VALUES
    assert body["reasoning_effort_field"] == "agent.reasoning_effort"
    assert body["reasoning_effort_writable"] is True
    row = body["rows"][0]
    assert row["reasoning_effort"] == "high"
    assert row["reasoning_effort_writable"] is True
    assert row["reasoning"]["field"] == "agent.reasoning_effort"
    assert row["reasoning"]["scope"] == "reasoning"
    assert row["reasoning"]["request_key"] == "reasoning_effort"
    assert row["reasoning"]["allowed_values"] == ALL_VALUES
    assert row["reasoning"]["off_value"] == "none"
    assert row["reasoning"]["off_semantics"] == {"enabled": False}
    assert row["reasoning"]["writable"] is True
    assert "agent.reasoning_effort" in row["writable_fields"]


def test_overview_shows_per_model_overrides_read_only(client, root_cfg):
    doc = _doc(root_cfg)
    doc["agent"]["reasoning_overrides"] = {"alpha-model-1": "max", "x-model": False}
    root_cfg.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    row = client.get(OVERVIEW, params={"agent": "default"}).json()["rows"][0]
    assert row["reasoning"]["overrides"] == {"alpha-model-1": "max", "x-model": "none"}
    assert row["reasoning"]["overrides_field"] == "agent.reasoning_overrides"
    assert row["reasoning"]["overrides_writable"] is False
    # 写全局值不许碰 per-model 覆盖（本卡不实现 per-model 写）
    before = copy.deepcopy(_doc(root_cfg)["agent"]["reasoning_overrides"])
    resp = _write_reasoning(client, "low")
    assert resp.status_code == 200, resp.text
    assert _doc(root_cfg)["agent"]["reasoning_overrides"] == before
    assert any("per-model" in item for item in resp.json()["warnings"])


# ---------------------------------------------------------------------------
# 批量端点复用同一份实现
# ---------------------------------------------------------------------------
def test_batch_apply_reasoning(client, root_cfg, worker_cfg):
    preview = client.post(PREFIX + "/models/batch/preview",
                          json={"agents": ["default", "worker"], "scope": "reasoning",
                                "reasoning_effort": "max"})
    assert preview.status_code == 200, preview.text
    rows = {row["agent"]: row for row in preview.json()["rows"]}
    assert rows["default"]["changes"] == [
        {"key": "agent.reasoning_effort", "from": "high", "to": "max"}]
    assert rows["worker"]["changes"] == [
        {"key": "agent.reasoning_effort", "from": "medium", "to": "max"}]

    resp = client.post(PREFIX + "/models/batch/apply",
                       json={"agents": ["default", "worker"], "scope": "reasoning",
                             "reasoning_effort": "max", "confirm": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert _reasoning_of(root_cfg) == "max"
    assert _reasoning_of(worker_cfg) == "max"
    # 每个 agent 的快照路径都在
    assert all(item["snapshot_path"] for item in body["results"])

# -*- coding: utf-8 -*-
"""自动命名面板后端（api_autotitler）契约测试。

打的是**面板自己的读法**，所以每个用例都在临时 home 里造出真实形状的文件：
插件目录（``plugin.yaml`` 的 23 键 ``config_schema`` + 0.2 存量 ``config.yaml``）、
宿主 ``plugins.entries.hermes-auto-titler.settings``、``plugin-data/.../state.json``、
以及一段**和插件真实格式逐字一致**的 ``logs/agent.log``。

覆盖卡片验收的 A（设置）与 B（监控）两段：

* 23 个参数按 schema 动态出，枚举选项与 ``choices`` 一致；
* ``max_title_length=5`` 被拒且错误可读（不是静默钳制到 10）；
* 写入只落宿主 settings，插件 ``config.yaml`` 的字节**一个都没变**；
* 写后回读一致，并给出「新值来自哪一层」；
* 三类计数（renamed/failed/skipped）与 ``grep`` 同一份日志**逐类相等**；
* 失败原因按 invalid decision payload / model call error / other 分类；
* 重试队列条目数与 ``state.json`` 一致；
* 每会话明细能对应到 state.json 里的真实会话。
"""

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = DASHBOARD_DIR.parent
REAL_PLUGIN = PLUGIN_ROOT.parent / "hermes-auto-titler"
#: 宿主源根（``hermes_cli`` / ``hermes_yaml`` / ``hermes_constants`` 都在这儿）。
#: 不插它，``save_plugin_setting_for`` → ``hermes_cli.plugins_state`` 会
#: ``ModuleNotFoundError: No module named 'hermes_yaml'``（实测）。所以写路径的用例必须先补上。
HERMES_AGENT_DIR = PLUGIN_ROOT.parents[1] / "hermes-agent"

if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))
if HERMES_AGENT_DIR.is_dir() and str(HERMES_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT_DIR))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


#: 一段与插件真实输出逐字同形的日志。
#: 关键点：``invalid decision payload`` 的 WARNING 出现在同会话 audit 行**之前**
#: （插件的 ``policy._generate`` 先打 WARNING，``titler.evaluate`` 再打 audit），
#: 面板就是靠这个顺序把根因归到对应会话上的。
SYNTHETIC_LOG = """\
2026-10-30 20:00:00,100 INFO hermes_auto_titler.titler: auto-titler state: restored 2 session(s)
2026-10-30 20:00:01,000 INFO hermes_auto_titler.titler: hermes-auto-titler: coexistence mode (first_title_mode=builtin); host title generation left untouched
2026-10-30 20:00:02,000 WARNING hermes_auto_titler.titler: auto-titler 20261029_204: model returned invalid decision payload: '两张卡已经在看板上跑着，当前状态：'
2026-10-30 20:00:02,010 WARNING hermes_auto_titler.titler: auto-titler 20261029_204: recorded failure (attempt 1, class=temporary, next retry in 30s)
2026-10-30 20:00:02,020 INFO hermes_auto_titler.titler: auto-titler audit: sid=20261029 act=failed reason=model call failed dur=17080ms
2026-10-30 20:00:03,000 WARNING hermes_auto_titler.titler: auto-titler 20261029_205: model returned invalid decision payload: '现在没有新的指令进来'
2026-10-30 20:00:03,010 INFO hermes_auto_titler.titler: auto-titler audit: sid=20261029 act=failed reason=model call failed dur=9980ms
2026-10-30 20:00:04,000 INFO hermes_auto_titler.titler: auto-titler local-ee6: llm result action=error title=None input=current=yes
2026-10-30 20:00:04,010 WARNING hermes_auto_titler.titler: auto-titler local-ee6: recorded failure (attempt 3, class=temporary, next retry in 60s)
2026-10-30 20:00:04,020 INFO hermes_auto_titler.titler: auto-titler audit: sid=local-ee act=failed reason=model call failed dur=7084ms
2026-10-30 20:00:05,000 INFO hermes_auto_titler.titler: auto-titler audit: sid=20261030 act=skipped reason=no messages dur=4ms
2026-10-30 20:00:05,100 INFO hermes_auto_titler.titler: auto-titler audit: sid=20261030 act=skipped reason=no messages dur=5ms
2026-10-30 20:00:06,000 INFO hermes_auto_titler.titler: auto-titler audit: sid=20261028 act=renamed reason=给墨墨绑定QQ通知渠道 dur=2100ms
2026-10-30 20:00:07,000 INFO hermes_auto_titler.titler: auto-titler audit: sid=20261027 act=keep reason=标题仍然准确 dur=1800ms
2026-10-30 20:00:08,000 INFO hermes_auto_titler.titler: auto-titler audit: sid=20261026 act=throttled reason=min interval dur=3ms
"""

STATE_JSON = {
    "version": 1,
    "sessions": {
        "20261028_211250_e9d8b2": {
            "base_title": "给墨墨绑定QQ通知渠道",
            "base_title_known": True,
            "expected_title": "给墨墨绑定QQ通知渠道",
            "kind": "counter",
            "rename_count": 1,
        },
        "local-ee68e367c002b7fcfd808c77ed55f147": {
            "base_title": "自动对话命名插件",
            "base_title_known": True,
            "expected_title": "自动对话命名插件：补可见性",
            "finalize": {
                "attempts": 10,
                "capacity": False,
                "class_attempts": 10,
                "close_epoch": 1,
                "next_retry_at": 1793365912.5,
                "queued_at": 1793345368.1,
                "reason": "finalize",
                "retry_class": "temporary",
            },
            "finalize_intent": True,
            "kind": "finalize",
            "rename_count": 0,
        },
        "20261029_204511_abcd": {
            "base_title": "接手看板",
            "base_title_known": True,
            "expected_title": "接手看板与自动会话归组修复",
            "kind": "finalize+retry",
            "rename_count": 2,
            "retry": {
                "attempts": 2,
                "class_attempts": 2,
                "next_retry_at": 1793369720.0,
                "queued_at": 1793345000.0,
                "reason": "error",
                "retry_class": "temporary",
            },
        },
    },
}

ROOT_CONFIG = """\
plugins:
  enabled:
    - hermes-auto-titler
  entries:
    hermes-auto-titler:
      settings:
        every_n_turns: 5
        strategy: aggressive
"""

PLUGIN_CONFIG = """\
enabled: true
every_n_turns: 2
on_close: true
min_interval_minutes: 5
max_title_length: null
max_display_width: 40
strategy: conservative
"""


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """造一个最小但形状真实的 home，并把 HERMES_HOME 指过去。"""
    root = tmp_path / "home"
    (root / "profiles").mkdir(parents=True)
    _write(root / "config.yaml", ROOT_CONFIG)

    plugin_dir = root / "plugins" / "hermes-auto-titler"
    plugin_dir.mkdir(parents=True)
    # 用真插件的 plugin.yaml（23 键 config_schema 的权威声明），不复制源码
    shutil.copy2(REAL_PLUGIN / "plugin.yaml", plugin_dir / "plugin.yaml")
    _write(plugin_dir / "config.yaml", PLUGIN_CONFIG)

    _write(root / "plugin-data" / "hermes-auto-titler" / "state.json", json.dumps(STATE_JSON, ensure_ascii=False))
    _write(root / "logs" / "agent.log", SYNTHETIC_LOG)

    # 一个带自己 config.yaml 的 profile（可写路径）
    profile = root / "profiles" / "code-worker"
    profile.mkdir(parents=True)
    _write(profile / "config.yaml", "plugins:\n  enabled:\n    - hermes-auto-titler\n")
    _write(profile / "profile.yaml", "title: code-worker\n")
    # 一个没有 config.yaml 的 profile（写不进去）
    bare = root / "profiles" / "bare-worker"
    bare.mkdir(parents=True)
    _write(bare / "profile.yaml", "title: bare-worker\n")

    monkeypatch.setenv("HERMES_HOME", str(root))
    # 官方解析器返回 Path（`hermes_cli/profiles.py` 会直接 `/ "profiles"`）；
    # 恒返回字符串会在 profile 写入路径上炸 `TypeError: str / str`。
    monkeypatch.setattr(
        "hermes_constants.get_default_hermes_root", lambda *a, **k: root, raising=False
    )
    for name in [key for key in list(sys.modules) if key == "core" or key.startswith("core.")]:
        sys.modules.pop(name, None)
    yield root


@pytest.fixture()
def client(home):
    import importlib

    import api_autotitler

    importlib.reload(api_autotitler)
    app = FastAPI()
    app.include_router(api_autotitler.router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# A. 设置
# ---------------------------------------------------------------------------
def test_settings_lists_every_schema_key(client):
    resp = client.get("/autotitler/settings", params={"agent": "default"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["field_count"] == 23
    assert len(data["fields"]) == 23
    assert set(data["schema_keys"]) == {f["key"] for f in data["fields"]}
    # 分组覆盖全部键，一个都不许漏（上游加参数会落进 other 组）
    grouped = [key for group in data["groups"] for key in group["keys"]]
    assert sorted(grouped) == sorted(data["schema_keys"])
    assert data["version"] == "0.3.0"
    assert data["declared_hooks"] == ["pre_llm_call", "on_session_end", "on_session_finalize"]


def test_settings_choices_come_from_schema(client):
    data = client.get("/autotitler/settings", params={"agent": "default"}).json()["data"]
    by_key = {f["key"]: f for f in data["fields"]}
    assert [c["value"] for c in by_key["strategy"]["choices"]] == ["conservative", "aggressive"]
    assert [c["value"] for c in by_key["title_style"]["choices"]] == ["concise", "complete"]
    assert [c["value"] for c in by_key["first_title_mode"]["choices"]] == ["builtin", "plugin"]
    # 没有 choices 的项必须是空数组（不是 None，前端才能无脑 map）
    assert by_key["every_n_turns"]["choices"] == []


def test_settings_reports_value_and_source_layer(client):
    data = client.get("/autotitler/settings", params={"agent": "default"}).json()["data"]
    by_key = {f["key"]: f for f in data["fields"]}

    # 宿主 settings 写过的键 → 来自宿主，且盖掉 config.yaml 的 2
    assert by_key["every_n_turns"]["value"] == 5
    assert by_key["every_n_turns"]["source"] == "host_settings_global"
    assert by_key["every_n_turns"]["default"] == 2
    assert by_key["strategy"]["value"] == "aggressive"
    assert by_key["strategy"]["source"] == "host_settings_global"

    # 只在插件 config.yaml 里的键 → 来自 config.yaml
    assert by_key["max_display_width"]["value"] == 40
    assert by_key["max_display_width"]["source"] == "plugin_config_yaml"

    # 谁都没设的键 → 默认层
    assert by_key["rename_confirmations"]["value"] == 1
    assert by_key["rename_confirmations"]["source"] == "default"

    # config.yaml 里是 null（= 不覆盖）的键，必须回落到默认值而不是 null
    assert by_key["max_title_length"]["value"] == 0
    assert by_key["max_title_length"]["source"] == "default"


def test_max_title_length_below_ten_is_rejected(client, home):
    before = _sha(home / "config.yaml")
    resp = client.put(
        "/autotitler/settings",
        params={"agent": "default"},
        json={"key": "max_title_length", "value": 5},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "value_rejected"
    message = body["error"]["message"]
    assert "10" in message and "0" in message
    # 被拒时一个字节都没写
    assert _sha(home / "config.yaml") == before


def test_unknown_key_and_bad_enum_rejected(client):
    resp = client.put(
        "/autotitler/settings", params={"agent": "default"}, json={"key": "nope", "value": 1}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unknown_setting_key"
    assert "writable_keys" in resp.json()["error"]

    resp = client.put(
        "/autotitler/settings",
        params={"agent": "default"},
        json={"key": "strategy", "value": "yolo"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "value_rejected"


def test_write_lands_in_host_settings_not_plugin_config(client, home):
    plugin_cfg = home / "plugins" / "hermes-auto-titler" / "config.yaml"
    plugin_before = _sha(plugin_cfg)

    resp = client.put(
        "/autotitler/settings",
        params={"agent": "default"},
        json={"key": "max_display_width", "value": 64},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["verified"] is True
    assert data["value"] == 64
    assert data["source"] == "host_settings_global"
    assert data["effective_value"] == 64

    # 落点是宿主 settings
    import yaml

    root_cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert root_cfg["plugins"]["entries"]["hermes-auto-titler"]["settings"]["max_display_width"] == 64
    # 插件 config.yaml 一个字节都没变
    assert _sha(plugin_cfg) == plugin_before

    # 回读：GET 立刻能看到新值与新的来源层
    after = client.get("/autotitler/settings", params={"agent": "default"}).json()["data"]
    by_key = {f["key"]: f for f in after["fields"]}
    assert by_key["max_display_width"]["value"] == 64
    assert by_key["max_display_width"]["source"] == "host_settings_global"


def test_write_needs_restart_hint_for_enabled(client):
    resp = client.put(
        "/autotitler/settings",
        params={"agent": "default"},
        json={"key": "first_title_mode", "value": "plugin"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["needs_restart"] is True
    assert "重启" in data["restart_hint"]


def test_profile_write_uses_agent_layer(client, home):
    resp = client.put(
        "/autotitler/settings",
        params={"agent": "code-worker"},
        json={"key": "every_n_turns", "value": 7},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["source"] == "host_settings_agent"
    import yaml

    cfg = yaml.safe_load((home / "profiles" / "code-worker" / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["plugins"]["entries"]["hermes-auto-titler"]["settings"]["every_n_turns"] == 7
    # 全局层没被动
    root_cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert root_cfg["plugins"]["entries"]["hermes-auto-titler"]["settings"]["every_n_turns"] == 5


def test_write_refused_without_agent_config(client):
    resp = client.put(
        "/autotitler/settings",
        params={"agent": "bare-worker"},
        json={"key": "every_n_turns", "value": 3},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "config_missing"


def test_optimistic_lock_blocks_stale_write(client, home):
    resp = client.put(
        "/autotitler/settings",
        params={"agent": "default"},
        json={"key": "every_n_turns", "value": 4, "expected_config": "stale-token"},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "config_changed"
    assert resp.json()["error"]["unchanged"] is True


def test_reads_do_not_touch_any_file(client, home):
    """读两次：config.yaml / state.json / 插件 config.yaml 的摘要都不许变（零写）。"""
    watched = [
        home / "config.yaml",
        home / "plugin-data" / "hermes-auto-titler" / "state.json",
        home / "plugins" / "hermes-auto-titler" / "config.yaml",
    ]
    before = [_sha(path) for path in watched]
    client.get("/autotitler/settings", params={"agent": "default"})
    client.get("/autotitler/overview", params={"agent": "default"})
    client.get("/autotitler/settings", params={"agent": "default"})
    assert [_sha(path) for path in watched] == before


# ---------------------------------------------------------------------------
# B. 监控
# ---------------------------------------------------------------------------
def test_overview_counts_match_the_log(client, home):
    """三类计数必须与**同一份日志**的 grep 结果逐类相等（卡片验收 B 第 1 条）。"""
    import re

    log = (home / "logs" / "agent.log").read_text(encoding="utf-8")
    expected = {}
    for act in re.findall(r"auto-titler audit:\s+sid=\S+\s+act=(\w+)", log):
        expected[act] = expected.get(act, 0) + 1
    invalid_expected = len(re.findall(r"model returned invalid decision payload", log))

    data = client.get("/autotitler/overview", params={"agent": "default"}).json()["data"]
    counts = data["health"]["counts"]
    for act in ("renamed", "failed", "skipped", "keep", "throttled"):
        assert counts[act] == expected.get(act, 0), act
    assert data["health"]["invalid_payload_total"] == invalid_expected == 2
    # 失败率：改名 1 / 失败 3 → 75%
    assert data["health"]["decisions"] == expected["renamed"] + expected["failed"] == 4
    assert data["health"]["failure_rate"] == pytest.approx(0.75)
    # 口径要能被脚本复现：响应里必须写清读了哪个文件、多少字节
    assert data["health"]["files"][0]["path"].endswith("agent.log")
    assert data["health"]["files"][0]["truncated"] is False


def test_overview_failure_classes_split_invalid_payload(client):
    """"invalid decision payload" 不许被 "model call failed" 吞掉。"""
    data = client.get("/autotitler/overview", params={"agent": "default"}).json()["data"]
    classes = {item["class"]: item["count"] for item in data["health"]["failure_classes"]}
    # 两条 WARNING 归到 invalid_decision_payload（sids 20261029_204 / _205）
    assert classes["invalid_decision_payload"] == 2
    # 一条只有 llm result action=error（local-ee6）→ model_call_error
    assert classes["model_call_error"] == 1
    assert sum(classes.values()) == data["health"]["counts"]["failed"] == 3

    diagnosis = data["health"]["diagnosis"]
    assert diagnosis["dominant_class"] == "invalid_decision_payload"
    assert diagnosis["invalid_payload_total"] == 2
    assert "散文" in diagnosis["headline"]
    assert any("policy.py" in item for item in diagnosis["where_to_fix"])
    assert diagnosis["panel_ok"] is True


def test_overview_recent_failures_carry_session_and_duration(client):
    data = client.get("/autotitler/overview", params={"agent": "default", "recent": 2}).json()["data"]
    failures = data["health"]["recent_failures"]
    assert len(failures) == 2
    newest = failures[0]
    # 最新一条是 local-ee6（7084ms，attempt 3）
    assert newest["session_id"] == "local-ee"
    assert newest["dur_ms"] == 7084
    assert newest["attempts"] == 3
    assert newest["class"] == "model_call_error"
    assert newest["ts"]


def test_overview_retry_queue_matches_state_json(client):
    data = client.get("/autotitler/overview", params={"agent": "default"}).json()["data"]
    retry = data["retry"]
    # state.json 里带 finalize 的 1 个、带 retry 的 1 个
    assert retry["finalize_count"] == 1
    assert retry["retry_count"] == 1
    assert retry["count"] == 2
    assert retry["earliest"] == pytest.approx(1793365912.5)
    assert retry["latest"] == pytest.approx(1793369720.0)

    entry = [item for item in retry["entries"] if item["block"] == "finalize"][0]
    assert entry["session_id"] == "local-ee68e367c002b7fcfd808c77ed55f147"
    assert entry["attempts"] == 10
    assert entry["retry_class"] == "temporary"
    assert entry["queued_at"] == pytest.approx(1793345368.1)


def test_overview_sessions_match_state_json(client):
    data = client.get("/autotitler/overview", params={"agent": "default"}).json()["data"]
    runtime = data["runtime"]
    assert runtime["sessions_total"] == 3
    assert runtime["rename_counts"] == {"0": 1, "1": 1, "2": 1}
    # kind 是复合值时两种口径都要给：拆开数（kind_counts）与原始值（raw_kind_counts）
    assert runtime["kind_counts"] == {"counter": 1, "finalize": 2, "retry": 1}
    assert runtime["raw_kind_counts"]["finalize+retry"] == 1

    by_id = {row["session_id"]: row for row in runtime["sessions"]}
    row = by_id["20261029_204511_abcd"]
    assert row["base_title"] == "接手看板"
    assert row["expected_title"] == "接手看板与自动会话归组修复"
    assert row["rename_count"] == 2
    assert row["has_retry"] is True


def test_overview_plugin_block_reports_hooks_and_state(client):
    data = client.get("/autotitler/overview", params={"agent": "default"}).json()["data"]
    plugin = data["plugin"]
    assert plugin["id"] == "hermes-auto-titler"
    assert plugin["version"] == "0.3.0"
    assert plugin["declared_hooks"] == ["pre_llm_call", "on_session_end", "on_session_finalize"]
    # 合成日志里有 register 行 → 必须被认出（这是"hooks 真的注册了"的唯一运行证据）
    assert plugin["registered"]["first_title_mode"] == "builtin"
    # 宿主三态来自 core.plugin_switch（enabled 名单里有它）
    assert plugin["state"] == "enabled"
    assert data["manual"]["available"] is False
    assert "rename-now" in data["manual"]["session_command"]


def test_overview_without_logs_is_empty_not_fabricated(client, home):
    """没有日志 ≠ 零失败：必须 distinct 出来（否则界面会说"一切正常"）。"""
    (home / "logs" / "agent.log").unlink()
    data = client.get("/autotitler/overview", params={"agent": "default"}).json()["data"]
    assert data["health"]["counts"]["total"] == 0
    assert data["health"]["failure_rate"] is None
    assert any("一条 auto-titler audit 都没有" in note for note in data["notes"])


def test_overview_rotated_files_optional(client, home):
    """``files=1`` 把 agent.log.1 也算进来；缺省 0 只看当前文件。"""
    rotated = home / "logs" / "agent.log.1"
    rotated.write_text(
        "2026-10-29 10:00:00,000 INFO hermes_auto_titler.titler: "
        "auto-titler audit: sid=20261028 act=renamed reason=旧标题 dur=1000ms\n",
        encoding="utf-8",
    )
    only_current = client.get("/autotitler/overview", params={"agent": "default"}).json()["data"]
    assert only_current["health"]["counts"]["renamed"] == 1
    assert len(only_current["health"]["files"]) == 1

    with_rotated = client.get(
        "/autotitler/overview", params={"agent": "default", "files": 1}
    ).json()["data"]
    assert with_rotated["health"]["counts"]["renamed"] == 2
    assert len(with_rotated["health"]["files"]) == 2


def test_invalid_agent_is_400_with_legal_ids(client):
    resp = client.get("/autotitler/overview", params={"agent": "../../etc"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_argument"
    assert "default" in body["error"]["agents"]


# ---------------------------------------------------------------------------
# B2. 时间窗口：历史 ≠ 当前
# ---------------------------------------------------------------------------
def _stamp(offset_seconds: float) -> str:
    """``now + offset`` 的日志时间戳（与插件写的格式逐字一致）。"""
    import datetime as _dt

    moment = _dt.datetime.now() + _dt.timedelta(seconds=offset_seconds)
    return moment.strftime("%Y-%m-%d %H:%M:%S") + ",000"


def _audit(offset_seconds: float, sid: str, act: str, reason: str = "", dur: int = 5) -> str:
    return "%s INFO hermes_auto_titler.titler: auto-titler audit: sid=%s act=%s reason=%s dur=%dms\n" % (
        _stamp(offset_seconds),
        sid,
        act,
        reason,
        dur,
    )


def _windowed_log() -> str:
    """一段「修好之前失败、修好之后不失败」的日志（相对 now 生成，窗口边界不会抖）。

    * 30 小时前：1 次失败（只在「全部历史」里）
    * 3 小时前：1 次失败（只在「24 小时」里，1 小时窗口看不到）
    * 10 分钟前：1 次改名 + 2 次 keep（三个窗口都能看到）
    """
    lines = [
        "%s INFO hermes_auto_titler.titler: hermes-auto-titler: coexistence mode "
        "(first_title_mode=builtin); host title generation left untouched\n" % _stamp(-3600 * 40),
        "%s WARNING hermes_auto_titler.titler: auto-titler old-30h: "
        "model returned invalid decision payload: '好的，我这就开始'\n" % _stamp(-3600 * 30),
        _audit(-3600 * 30, "old-30h", "failed", "model call failed", 9000),
        "%s WARNING hermes_auto_titler.titler: auto-titler old-3h: "
        "model returned invalid decision payload: '收到'\n" % _stamp(-3600 * 3),
        _audit(-3600 * 3, "old-3h", "failed", "model call failed", 8000),
        _audit(-600, "now-10m", "renamed", "标题已更新", 2100),
        _audit(-600, "now-10m", "keep", "", 1800),
        _audit(-600, "now-10m", "keep", "", 1500),
    ]
    return "".join(lines)


def test_overview_window_switches_the_numbers(client, home):
    """切换区间后数字**真的变**（卡片验收 B 第 2 条）。"""
    _write(home / "logs" / "agent.log", _windowed_log())

    one_hour = client.get(
        "/autotitler/overview", params={"agent": "default", "window": "1h"}
    ).json()["data"]["health"]
    day = client.get(
        "/autotitler/overview", params={"agent": "default", "window": "24h"}
    ).json()["data"]["health"]
    everything = client.get(
        "/autotitler/overview", params={"agent": "default", "window": "all"}
    ).json()["data"]["health"]

    # 最近 1 小时：只有 10 分钟前的 3 条，零失败 → 当前是好的
    assert one_hour["counts"]["failed"] == 0
    assert one_hour["counts"]["renamed"] == 1
    assert one_hour["counts"]["keep"] == 2
    assert one_hour["failure_rate"] == 0.0
    assert one_hour["window"]["label"] == "最近 1 小时"
    # 最近 24 小时：把 3 小时前那次失败也算进来
    assert day["counts"]["failed"] == 1
    assert day["failure_rate"] == pytest.approx(0.5)
    # 全部历史：30 小时前那次也在
    assert everything["counts"]["failed"] == 2
    assert everything["failure_rate"] == pytest.approx(2 / 3)
    # 三个区间的数字互不相等 —— 这就是「历史 vs 当前」的证据
    assert (
        one_hour["counts"]["failed"] != day["counts"]["failed"] != everything["counts"]["failed"]
    )
    # 每个区间都要能说清「从什么时候到什么时候」
    for health in (one_hour, day, everything):
        assert health["window"]["to_ts"]
        assert health["window"]["from_ts"]
        assert health["generated_at"]
    assert one_hour["window"]["since_epoch"] > day["window"]["since_epoch"]
    assert day["window"]["since_epoch"] > 0
    assert everything["window"]["since_epoch"] is None


def test_overview_window_reports_all_three_at_once(client, home):
    """三个区间并列返回：界面切换不必再发请求，且口径不可能对不上。"""
    _write(home / "logs" / "agent.log", _windowed_log())
    data = client.get("/autotitler/overview", params={"agent": "default"}).json()["data"]
    windows = data["health"]["windows"]
    assert list(windows) == ["1h", "24h", "all"]
    assert data["health"]["window_order"] == ["1h", "24h", "all"]
    assert windows["1h"]["counts"]["failed"] == 0
    assert windows["24h"]["counts"]["failed"] == 1
    assert windows["all"]["counts"]["failed"] == 2
    # 缺省 window=all：老调用方（不带 window）拿到的仍是整份日志口径
    assert data["health"]["counts"]["failed"] == 2


def test_overview_window_empty_is_null_rate_not_zero(client, home):
    """窗口里没有决策时失败率是 ``None``（“—”），不是 0% —— 空数据不许伪装成零失败。"""
    _write(home / "logs" / "agent.log", _audit(-3600 * 48, "old", "renamed", "", 100))
    health = client.get(
        "/autotitler/overview", params={"agent": "default", "window": "1h"}
    ).json()["data"]["health"]
    assert health["counts"]["total"] == 0
    assert health["failure_rate"] is None
    assert health["window"]["last_event_ts"] == ""


def test_overview_rejects_unknown_window(client):
    resp = client.get("/autotitler/overview", params={"agent": "default", "window": "7d"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_argument"
    assert body["error"]["windows"] == ["1h", "24h", "all"]


def test_overview_baseline_splits_before_and_after(client, home):
    """基线（修复点）：此前 N 次失败、此后 0 次 —— 默认取最后一次失败的时间。"""
    _write(home / "logs" / "agent.log", _windowed_log())
    baseline = client.get("/autotitler/overview", params={"agent": "default"}).json()["data"][
        "health"
    ]["baseline"]

    assert baseline["available"] is True
    assert baseline["source"] == "last_failure"
    assert baseline["ts"]
    # 基线就是 3 小时前那次失败；它算「此前」，之后的 3 条改名/keep 算「此后」
    assert baseline["before"]["counts"]["failed"] == 2
    assert baseline["after"]["counts"]["failed"] == 0
    assert baseline["after"]["counts"]["renamed"] == 1
    assert "基线之前失败 2 次；基线之后失败 0 次" in baseline["headline"]
    assert "零失败" in baseline["headline"]
    # 时间线：有桶、失败只出现在基线及之前的桶里
    assert baseline["timeline"]
    assert any(bucket["failed"] > 0 for bucket in baseline["timeline"])
    assert baseline["timeline"][-1]["failed"] == 0
    for bucket in baseline["timeline"]:
        assert bucket["start_ts"] and bucket["end_ts"]


def test_overview_baseline_accepts_explicit_time(client, home):
    """``?baseline=`` 可以钉住真正的修复时刻（例如部署完成那一刻）。"""
    _write(home / "logs" / "agent.log", _windowed_log())
    import datetime as _dt

    # 把基线钉在「20 小时前」：30 小时前的失败在之前，3 小时前的失败在之后
    pinned = (_dt.datetime.now() - _dt.timedelta(hours=20)).strftime("%Y-%m-%d %H:%M:%S")
    baseline = client.get(
        "/autotitler/overview", params={"agent": "default", "baseline": pinned}
    ).json()["data"]["health"]["baseline"]

    assert baseline["source"] == "explicit"
    assert baseline["source_label"] == "你指定的基线时间"
    assert baseline["before"]["counts"]["failed"] == 1
    assert baseline["after"]["counts"]["failed"] == 1
    assert baseline["ts"] == pinned

    bad = client.get("/autotitler/overview", params={"agent": "default", "baseline": "上周二"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_argument"


def test_health_marks_undated_rows_instead_of_dropping_them(client, home):
    """解析不出时间戳的 audit 行必须显式计数，不能悄悄消失。"""
    _write(
        home / "logs" / "agent.log",
        "WARNING 多行日志被截断的半行: auto-titler audit: sid=abc act=keep reason= dur=1ms\n"
        + _audit(-600, "now-10m", "renamed", "", 100),
    )
    health = client.get("/autotitler/overview", params={"agent": "default"}).json()["data"]["health"]
    assert health["undated_events"] == 1
    # 没有时间戳的行不进任何窗口
    assert health["counts"]["keep"] == 0
    assert health["counts"]["renamed"] == 1


# ---------------------------------------------------------------------------
# C. 运行日志（只读 + 脱敏）
# ---------------------------------------------------------------------------
LOG_ROW_KEYS = {"ts", "epoch", "sid", "act", "reason", "dur_ms", "class"}


def test_logs_returns_audit_rows_newest_first(client):
    data = client.get("/autotitler/logs", params={"agent": "default"}).json()["data"]
    rows = data["rows"]
    assert data["redacted"] is True
    assert data["total"] == 8  # 合成日志里 8 条 audit 行
    assert data["matched"] == 8
    assert len(rows) == 8
    # 倒序：第一条是日志里最后那条（throttled），最后一条是最早的 failed
    assert rows[0]["act"] == "throttled"
    assert rows[-1]["act"] == "failed"
    stamps = [row["ts"] for row in rows]
    assert stamps == sorted(stamps, reverse=True)
    for row in rows:
        assert set(row) == LOG_ROW_KEYS
        assert row["sid"] and row["act"]
        assert isinstance(row["dur_ms"], int)
    assert data["acts"] and {item["act"] for item in data["acts"]} == {
        "failed",
        "skipped",
        "renamed",
        "keep",
        "throttled",
    }


def test_logs_filters_by_action_and_session(client):
    failed = client.get("/autotitler/logs", params={"agent": "default", "act": "failed"}).json()["data"]
    assert failed["matched"] == 3
    assert {row["act"] for row in failed["rows"]} == {"failed"}
    # 失败行带 class（按会话 id 把 invalid payload 归因上）
    assert {row["class"] for row in failed["rows"]} == {
        "invalid_decision_payload",
        "model_call_error",
    }

    multi = client.get(
        "/autotitler/logs", params={"agent": "default", "act": "skipped,throttled"}
    ).json()["data"]
    assert multi["matched"] == 3

    by_sid = client.get("/autotitler/logs", params={"agent": "default", "sid": "local-ee"}).json()[
        "data"
    ]
    assert by_sid["matched"] == 1
    assert by_sid["rows"][0]["sid"] == "local-ee"
    assert by_sid["rows"][0]["dur_ms"] == 7084

    limited = client.get("/autotitler/logs", params={"agent": "default", "limit": 2}).json()["data"]
    assert limited["returned"] == 2
    assert limited["matched"] == 8


def test_logs_respects_time_filters(client, home):
    _write(home / "logs" / "agent.log", _windowed_log())
    recent = client.get(
        "/autotitler/logs", params={"agent": "default", "since": "1970-01-01 00:00:00"}
    ).json()["data"]
    assert recent["matched"] == 5

    import datetime as _dt

    since = (_dt.datetime.now() - _dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    last_hour = client.get("/autotitler/logs", params={"agent": "default", "since": since}).json()[
        "data"
    ]
    assert last_hour["matched"] == 3
    assert {row["act"] for row in last_hour["rows"]} == {"renamed", "keep"}

    bad = client.get("/autotitler/logs", params={"agent": "default", "since": "not-a-time"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_argument"

    backwards = client.get(
        "/autotitler/logs",
        params={"agent": "default", "since": "2026-10-31 10:00", "until": "2026-10-30 10:00"},
    )
    assert backwards.status_code == 400
    assert "since" in backwards.json()["error"]["message"]


def test_logs_never_returns_raw_lines_or_session_text(client, home):
    """脱敏：会话正文、日志原文都不许出现在响应里。"""
    secret = "这是会话正文里的敏感内容：银行卡 6222 0000 1111 2222"
    _write(
        home / "logs" / "agent.log",
        "%s INFO hermes_auto_titler.titler: 会话正文 %s\n" % (_stamp(-60), secret)
        + _audit(-60, "sid-1", "renamed", "标题", 100)
        + _audit(-50, "sid-2", "keep", "x" * 400, 10),
    )
    data = client.get("/autotitler/logs", params={"agent": "default"}).json()["data"]
    blob = json.dumps(data, ensure_ascii=False)
    assert secret not in blob
    assert "INFO hermes_auto_titler.titler" not in blob
    # reason 超长被截断（不让一条 reason 把整页撑爆）
    longest = max(len(row["reason"]) for row in data["rows"])
    assert longest <= 161
    assert data["redaction_note"]


def test_logs_route_is_read_only(client):
    """日志页只读：路由表里不许有 PUT/POST/DELETE 落在 /autotitler/logs 上。"""
    import api_autotitler

    methods = set()
    for route in api_autotitler.router.routes:
        if getattr(route, "path", "") == "/autotitler/logs":
            methods |= set(getattr(route, "methods", set()) or set())
    assert methods == {"GET"}
    # 整个模块也不许出现清空/删除日志的入口
    source = (DASHBOARD_DIR / "api_autotitler.py").read_text(encoding="utf-8")
    for forbidden in ("unlink(", "truncate(", "rmtree"):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# D. 用户点名的四项参数
# ---------------------------------------------------------------------------
def test_settings_pins_the_four_user_named_items(client):
    data = client.get("/autotitler/settings", params={"agent": "default"}).json()["data"]
    pinned = data["pinned"]
    assert [item["id"] for item in pinned] == ["enabled", "rhythm", "model", "prompt"]
    covered = set()
    for item in pinned:
        assert item["label"] and item["hint"]
        covered |= set(item["keys"])
    assert covered == {
        "enabled",
        "every_n_turns",
        "min_interval_minutes",
        "title_model",
        "provider",
        "custom_instructions",
    }
    by_key = {field["key"]: field for field in data["fields"]}
    for key in covered:
        assert by_key[key]["pinned"] is True
        assert by_key[key]["writable"] is True
    # 其余 17 项不被置顶，但仍然全部可读可写
    assert sum(1 for field in data["fields"] if field["pinned"]) == 6
    assert data["field_count"] == 23
    assert len(data["writable_keys"]) == 23
    # 提示词是长文本：界面要给它 textarea 而不是单行 input
    assert by_key["custom_instructions"]["multiline"] is True
    assert by_key["title_model"]["multiline"] is False


def test_custom_instructions_is_really_editable_and_persisted(client, home):
    """「提示词设计」必须真能编辑并落库（卡片验收 D 第 5 条）。"""
    prompt = "只回 JSON。标题不超过 20 字。\n不要照抄会话里的助手回复。"
    resp = client.put(
        "/autotitler/settings",
        params={"agent": "default"},
        json={"key": "custom_instructions", "value": prompt},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["key"] == "custom_instructions"
    assert body["value"] == prompt
    assert body["verified"] is True
    assert body["source"] == "host_settings_global"
    assert body["needs_restart"] is False
    assert "即时生效" in body["restart_hint"]

    # 磁盘回读（不信返回值）：落点在宿主 settings，插件 config.yaml 一个字节都没动
    root = (home / "config.yaml").read_text(encoding="utf-8")
    assert "custom_instructions" in root
    assert "不要照抄会话里的助手回复" in root
    plugin_cfg = (home / "plugins" / "hermes-auto-titler" / "config.yaml").read_text(encoding="utf-8")
    assert "不要照抄会话里的助手回复" not in plugin_cfg

    # 再读一遍 GET：新值 + 它现在来自哪一层
    fields = {
        field["key"]: field
        for field in client.get("/autotitler/settings", params={"agent": "default"}).json()["data"][
            "fields"
        ]
    }
    assert fields["custom_instructions"]["value"] == prompt
    assert fields["custom_instructions"]["source"] == "host_settings_global"


def test_all_23_keys_are_writable(client):
    """23 项全部能改（不只是用户点名那 4 项）。"""
    data = client.get("/autotitler/settings", params={"agent": "default"}).json()["data"]
    assert sorted(data["writable_keys"]) == sorted(field["key"] for field in data["fields"])
    assert data["readonly_keys"] == []


def test_put_reports_backend_reason_for_illegal_value(client, home):
    """非法值必须给出后端原因（不许静默失败/假报成功）。"""
    before = _sha(home / "config.yaml")
    bad_type = client.put(
        "/autotitler/settings",
        params={"agent": "default"},
        json={"key": "every_n_turns", "value": "abc"},
    )
    assert bad_type.status_code == 400
    error = bad_type.json()["error"]
    assert error["code"] == "value_rejected"
    assert "every_n_turns" in error["message"]
    assert _sha(home / "config.yaml") == before  # 拒绝时零写入

    out_of_range = client.put(
        "/autotitler/settings",
        params={"agent": "default"},
        json={"key": "max_display_width", "value": 999},
    )
    assert out_of_range.status_code == 400
    assert "10..100" in out_of_range.json()["error"]["message"]
    assert _sha(home / "config.yaml") == before


def test_four_named_items_all_roundtrip(client, home):
    """用户点名的四组键逐项写一遍，每次都要「写后回读一致」+ GET 反映新值。"""
    cases = [
        ("enabled", False),
        ("every_n_turns", 7),
        ("min_interval_minutes", 2.5),
        ("title_model", "gpt-5-mini"),
        ("provider", "openai"),
        ("custom_instructions", "只回 JSON，标题 ≤ 20 字"),
    ]
    for key, value in cases:
        resp = client.put("/autotitler/settings", params={"agent": "default"}, json={"key": key, "value": value})
        assert resp.status_code == 200, (key, resp.text)
        body = resp.json()["data"]
        assert body["verified"] is True, key
        assert body["value"] == value, key
        assert body["effective_value"] == value, (key, body["effective_value"])
        assert body["source"] == "host_settings_global", key

    fields = {
        field["key"]: field
        for field in client.get("/autotitler/settings", params={"agent": "default"}).json()["data"]["fields"]
    }
    for key, value in cases:
        assert fields[key]["value"] == value, key
        assert fields[key]["source"] == "host_settings_global", key

    # 磁盘上真的写了（不是只改了内存）
    text = (home / "config.yaml").read_text(encoding="utf-8")
    for needle in ("enabled: false", "every_n_turns: 7", "min_interval_minutes: 2.5", "title_model: gpt-5-mini", "provider: openai"):
        assert needle in text, needle
    assert "只回 JSON，标题 ≤ 20 字" in text

    # enabled 是插件加载期读的开关：写入要提示「需要重启」
    enabled = client.put(
        "/autotitler/settings", params={"agent": "default"}, json={"key": "enabled", "value": True}
    ).json()["data"]
    assert enabled["needs_restart"] is True
    assert "重启" in enabled["restart_hint"]

# -*- coding: utf-8 -*-
"""「执行 Lane」后端契约测试（全部用 fixture 造心跳，**不依赖真实 lane**）。

覆盖验收里点名的 8 条：

1. 心跳新鲜 + pid 活 → ``online``
2. 心跳过期 + pid 活 → ``stalled``
3. 心跳新鲜 + pid 不存在 → ``offline``
4. ``create_time`` 与 ``supervisor_started_at`` 差很多 → ``offline``（pid 复用）
5. 文件缺失 → ``unknown`` 且 HTTP 仍是 ``{ok:true}``
6. JSON 损坏 → ``unknown`` + error 原话，且其它 lane 照常
7. ``lane_script`` 只给文件名、返回体里没有完整命令行
8. ``task`` 有值算 ``running_seconds``、``task:null`` 不报错

沙盒：``monkeypatch.setenv("HERMES_HOME", tmp_path)``，心跳写到
``<tmp>/cache/lane/status/<assignee>.json``，**绝不碰真 home**。

pid 的"活/死"用两种 fixture：

* 真 psutil + 本进程 pid（online / stalled）；
* 假的 ``psutil`` 模块（死 pid / create_time 被复用的 pid）—— 避免依赖机器上
  某个 pid 恰好存不存在，测试才是确定性的。

跑法::

    <venv>/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_api_lanes.py
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = REPO_ROOT / "dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import api_lanes  # noqa: E402
import psutil  # noqa: E402

PREFIX = "/api/plugins/hermes-console"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture
def home(tmp_path, monkeypatch):
    """沙盒 HERMES_HOME + 真实的 status 目录（目录存在、默认没有文件）。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    directory = tmp_path / "cache" / "lane" / "status"
    directory.mkdir(parents=True)
    return tmp_path


@pytest.fixture
def client(home):
    # 与生产同形：plugin_api 被挂到 /api/plugins/hermes-console 下。
    app = FastAPI()
    app.include_router(api_lanes.router, prefix=PREFIX)
    return TestClient(app)


@pytest.fixture
def fake_psutil(monkeypatch):
    """用假的 ``psutil`` 固定"pid 不存在"或"create_time 是多少"。"""

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return _Fake.create_time

    class NoSuchProcess(Exception):
        pass

    class ZombieProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    # 类体里 `X = X` 的右值走 LOAD_NAME（只看类命名空间→全局→内建），
    # 看不到外层函数变量，必抛 NameError。故先用不同名字绑定到函数作用域。
    _NoSuchProcess = NoSuchProcess
    _ZombieProcess = ZombieProcess
    _AccessDenied = AccessDenied

    class _Fake:
        create_time = 0.0
        exists = True

        Process = FakeProcess
        NoSuchProcess = _NoSuchProcess
        ZombieProcess = _ZombieProcess
        AccessDenied = _AccessDenied

        @classmethod
        def dead(cls):
            cls.exists = False
            return cls

        @classmethod
        def alive_at(cls, create_time):
            cls.exists = True
            cls.create_time = create_time
            return cls

    class FakeModule:
        NoSuchProcess = _NoSuchProcess
        ZombieProcess = _ZombieProcess
        AccessDenied = _AccessDenied

        @staticmethod
        def Process(pid):  # noqa: N802 - 与 psutil API 同名
            if not _Fake.exists:
                raise NoSuchProcess(pid)
            return FakeProcess(pid)

    monkeypatch.setattr(api_lanes, "psutil", FakeModule)
    return _Fake


def write_heartbeat(home, assignee, **overrides):
    """在沙盒 status 目录写一个 schema=1 的心跳。"""
    now = time.time()
    body = {
        "schema": 1,
        "assignee": assignee,
        "lane_script": ("codex_lane.py" if "codex" in assignee else "claude_lane.py"),
        "pid": 64056,
        "supervisor_started_at": now - 3600,
        "last_poll_at": now - 5,
        "poll_interval": 20,
        "state": "idle",
        "task": None,
        "last_result": None,
    }
    body.update(overrides)
    path = home / "cache" / "lane" / "status" / (assignee + ".json")
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def lanes_by_assignee(payload):
    return {lane["assignee"]: lane for lane in payload["data"]["lanes"]}


def get_lanes(client):
    response = client.get(PREFIX + "/lanes")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    return payload


# ---------------------------------------------------------------------------
# 1) 新鲜 + 活 → online
# ---------------------------------------------------------------------------
def test_fresh_heartbeat_with_live_pid_is_online(client, home):
    pid = os.getpid()
    started = psutil.Process(pid).create_time()
    write_heartbeat(
        home,
        "codex-worker",
        pid=pid,
        supervisor_started_at=started,
        last_poll_at=time.time() - 5,
        state="running",
    )

    payload = get_lanes(client)
    lane = lanes_by_assignee(payload)["codex-worker"]

    assert lane["liveness"] == "online"
    assert lane["pid_alive"] is True
    assert lane["pid"] == pid
    assert lane["state"] == "running"
    assert lane["error"] is None
    assert 0 <= lane["age_seconds"] <= 30


# ---------------------------------------------------------------------------
# 2) 过期 + 活 → stalled
# ---------------------------------------------------------------------------
def test_stale_heartbeat_with_live_pid_is_stalled(client, home):
    pid = os.getpid()
    started = psutil.Process(pid).create_time()
    write_heartbeat(
        home,
        "codex-worker",
        pid=pid,
        supervisor_started_at=started,
        last_poll_at=time.time() - 600,  # 窗口 = max(3*20, 90) = 90 秒
        poll_interval=20,
    )

    lane = lanes_by_assignee(get_lanes(client))["codex-worker"]

    assert lane["liveness"] == "stalled"
    assert lane["pid_alive"] is True
    assert lane["age_seconds"] >= 500


# ---------------------------------------------------------------------------
# 3) 新鲜 + pid 不存在 → offline（不许只看心跳新旧）
# ---------------------------------------------------------------------------
def test_fresh_heartbeat_with_dead_pid_is_offline(client, home, fake_psutil):
    fake_psutil.dead()
    write_heartbeat(home, "codex-worker", pid=69092, last_poll_at=time.time() - 1)

    lane = lanes_by_assignee(get_lanes(client))["codex-worker"]

    assert lane["liveness"] == "offline"
    assert lane["pid_alive"] is False
    assert lane["pid"] == 69092


# ---------------------------------------------------------------------------
# 4) create_time 与 supervisor_started_at 差很多 → offline（pid 复用）
# ---------------------------------------------------------------------------
def test_pid_reuse_detected_by_create_time(client, home, fake_psutil):
    started = time.time() - 3600
    fake_psutil.alive_at(started + 3000)  # 差 3000 秒 ≫ 5 秒阈值
    write_heartbeat(home, "codex-worker", pid=12332, supervisor_started_at=started)

    lane = lanes_by_assignee(get_lanes(client))["codex-worker"]

    assert lane["liveness"] == "offline"
    assert lane["pid_alive"] is False
    assert "复用" in (lane["error"] or "")


# ---------------------------------------------------------------------------
# 5) 文件缺失 → unknown，但 ok:true / 200（目录空不许 500）
# ---------------------------------------------------------------------------
def test_missing_heartbeat_is_unknown_but_response_ok(client, home):
    # 删掉整个目录，模拟 supervisor 还没起过
    status = home / "cache" / "lane" / "status"
    for child in status.iterdir():
        child.unlink()
    status.rmdir()

    payload = get_lanes(client)
    lanes = lanes_by_assignee(payload)

    assert set(lanes) == {"codex-worker", "claude-worker"}
    for lane in lanes.values():
        assert lane["liveness"] == "unknown"
        assert lane["pid_alive"] is None
        assert "不存在" in lane["error"]
    assert Path(payload["data"]["status_dir"]) == home / "cache" / "lane" / "status"
    assert isinstance(payload["data"]["now"], float)


# ---------------------------------------------------------------------------
# 6) JSON 损坏 → unknown + error 原话；其它 lane 照常
# ---------------------------------------------------------------------------
def test_broken_json_only_affects_its_own_lane(client, home):
    now = time.time()
    good_pid = os.getpid()
    started = psutil.Process(good_pid).create_time()
    write_heartbeat(
        home, "codex-worker", pid=good_pid, supervisor_started_at=started, last_poll_at=now - 3
    )
    bad = home / "cache" / "lane" / "status" / "claude-worker.json"
    bad_text = '{"schema":1,"assignee":"claude-worker",'
    bad.write_text(bad_text, encoding="utf-8")

    try:
        json.loads(bad_text)
        expected = ""
    except json.JSONDecodeError as exc:
        expected = str(exc)

    lanes = lanes_by_assignee(get_lanes(client))

    assert lanes["codex-worker"]["liveness"] == "online"
    assert lanes["claude-worker"]["liveness"] == "unknown"
    assert "JSON" in lanes["claude-worker"]["error"]
    assert expected and expected in lanes["claude-worker"]["error"]


def test_unsupported_schema_is_unknown(client, home):
    write_heartbeat(home, "codex-worker", schema=2)

    lane = lanes_by_assignee(get_lanes(client))["codex-worker"]

    assert lane["liveness"] == "unknown"
    assert "schema" in lane["error"]


# ---------------------------------------------------------------------------
# 7) lane_script 只给文件名；返回体不含完整命令行
# ---------------------------------------------------------------------------
def test_lane_script_is_basename_and_no_command_line_leaks(client, home, fake_psutil):
    fake_psutil.dead()
    write_heartbeat(
        home,
        "codex-worker",
        lane_script="D:/hermes-data/hermes/scripts/codex_lane.py --token sk-secret-123",
    )

    payload = get_lanes(client)
    lane = lanes_by_assignee(payload)["codex-worker"]

    assert lane["lane_script"] == "codex_lane.py"
    assert lane["adapter"] == "codex"
    raw = json.dumps(payload)
    assert "sk-secret-123" not in raw
    assert "D:/hermes-data" not in raw
    assert "--token" not in raw
    # 字段白名单：不许回传原文件里的多余键
    assert set(lane) == {
        "assignee",
        "lane_script",
        "adapter",
        "liveness",
        "age_seconds",
        "pid",
        "pid_alive",
        "poll_interval",
        "state",
        "task",
        "last_result",
        "last_poll_at",
        "error",
    }


def test_unknown_adapter_for_unrecognized_lane(client, home, fake_psutil):
    fake_psutil.dead()
    write_heartbeat(home, "mystery-worker", lane_script="mystery_lane.py")

    payload = get_lanes(client)
    lane = lanes_by_assignee(payload)["mystery-worker"]

    assert lane["adapter"] == "unknown"


# ---------------------------------------------------------------------------
# 8) task 有值算 running_seconds；task:null 不报错
# ---------------------------------------------------------------------------
def test_task_running_seconds_and_null_task(client, home, fake_psutil):
    fake_psutil.dead()
    now = time.time()
    write_heartbeat(
        home,
        "codex-worker",
        state="running",
        task={
            "id": 42,
            "run_id": "run-abc",
            "title": "改一行文档",
            "claimed_at": now - 125,
        },
    )
    write_heartbeat(home, "claude-worker", state="idle", task=None)

    lanes = lanes_by_assignee(get_lanes(client))

    codex_task = lanes["codex-worker"]["task"]
    assert codex_task["title"] == "改一行文档"
    assert codex_task["run_id"] == "run-abc"
    assert 120 <= codex_task["running_seconds"] <= 200

    assert lanes["claude-worker"]["task"] is None
    assert lanes["claude-worker"]["state"] == "idle"

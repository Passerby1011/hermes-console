# -*- coding: utf-8 -*-
"""hermes-console · 「执行 Lane」分区后端（**只读**）。

挂载契约：本文件**不单独挂载**。``plugin_api.py`` 把本目录加进 ``sys.path`` 后 import
本模块，再 ``include_router(api_lanes.router)``；最终 URL 是
``/api/plugins/hermes-console/lanes``。

## 数据来源（唯一）

``<HERMES_HOME>/cache/lane/status/<assignee>.json`` —— supervisor 写的心跳文件，
schema 已锁定::

    {"schema":1,"assignee":"codex-worker","lane_script":"codex_lane.py","pid":64056,
     "supervisor_started_at":1793774000.0,"last_poll_at":1793774123.4,"poll_interval":20,
     "state":"idle|claimed|running","task":null|{"id","run_id","title","claimed_at"},
     "last_result":null|{"task","action":"complete|block|skipped","at","reason"}}

目录可能还不存在（supervisor 尚未起过）——那不是错误：返回两条已知 lane，
``liveness=unknown`` + ``error`` 原话，HTTP 仍是 200。

## liveness 判定（核心，别改成"看 pid 文件"）

========== ==========================================================
online     心跳可读 + ``now-last_poll_at <= max(3*poll_interval, 90)`` + pid 存活
stalled    心跳过期，但 pid 仍存活
offline    pid 不存在（无论心跳新旧）；create_time 与 supervisor_started_at
           相差 > 5 秒同样按 offline（**pid 复用**，实测残留过一串已死 pid）
unknown    文件缺失 / JSON 坏 / schema != 1 / 关键字段不可解析
========== ==========================================================

## 字段白名单

只输出：``assignee / lane_script / adapter / liveness / age_seconds / pid / pid_alive /
poll_interval / state / task / last_result / last_poll_at / error``。
``lane_script`` 只给 ``Path(...).name``；不返回完整命令行、环境变量、密钥。
单个文件坏掉只影响该 lane（异常就地吞掉，写进它的 ``error``）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter  # type: ignore

try:  # psutil 是本卡的硬依赖：create_time 比对是识别 pid 复用的唯一依据
    import psutil  # type: ignore
except Exception:  # noqa: BLE001 - 缺 psutil 时退化为"只看存在性"，并在 error 里写明
    psutil = None  # type: ignore[assignment]

router = APIRouter()

#: 已知的两条外部执行 lane（目录里没文件时也要出现在面板上）。
EXPECTED_ASSIGNEES: Tuple[str, ...] = ("codex-worker", "claude-worker")
#: pid 复用判定阈值（秒）：supervisor_started_at 与进程 create_time 之差超过它即视为复用。
PID_REUSE_TOLERANCE = 5.0
#: 心跳新鲜度下限（秒）：``max(3*poll_interval, 90)`` 里的 90。
MIN_FRESH_WINDOW = 90.0
#: poll_interval 缺失/非法时的兜底（与 schema 默认值一致）。
DEFAULT_POLL_INTERVAL = 20.0
#: schema 版本（锁死为 1）。
SUPPORTED_SCHEMA = 1


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def hermes_home() -> Path:
    """Hermes 主目录（``HERMES_HOME`` 原样采信）。

    心跳文件是 supervisor 按它自己的 ``HERMES_HOME`` 写的，所以**不做 profiles
    归一**：照读环境变量才能和写方落在同一目录。环境变量缺失时才退回布局探测
    （``api_agents.hermes_home()`` —— 那边按 ``<home>/profiles/<p>`` 归一是对的，
    因为它是给"枚举 agent"用的）。
    """
    env_home = (os.environ.get("HERMES_HOME") or "").strip()
    if env_home:
        return Path(env_home)
    try:
        from api_agents import hermes_home as _detect  # type: ignore

        return Path(_detect())
    except Exception:  # noqa: BLE001 - 兄弟模块不可用不阻断本分区
        return Path.home() / ".hermes"


def status_dir() -> Path:
    """心跳目录：``<HERMES_HOME>/cache/lane/status``（**只读**，不创建）。"""
    return hermes_home() / "cache" / "lane" / "status"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _as_float(value: Any) -> Optional[float]:
    """数字（不含 bool）→ float；其它返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / Inf
        return None
    return float(value)


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _script_name(value: Any) -> Optional[str]:
    """``lane_script`` 只给文件名（白名单：完整路径/命令行不外泄）。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # 命令行形态（带空格/参数）只取第一段；Windows 反斜杠路径在非 Windows 主机上
    # 也必须只回文件名，所以先统一分隔符再做 Path(...).name。
    first = text.split()[0].replace("\\", "/")
    name = Path(first).name
    return name or None


def _adapter_of(assignee: str, script_name: Optional[str]) -> str:
    """来自哪个执行器：文件名优先，其次 assignee。认不出 → unknown。"""
    haystack = " ".join(part for part in (script_name or "", assignee or "") if part).lower()
    if "codex" in haystack:
        return "codex"
    if "claude" in haystack:
        return "claude"
    return "unknown"


def _probe_pid(pid: Any, started_at: Optional[float]) -> Tuple[Optional[bool], Optional[str]]:
    """pid 是否仍是**那个 supervisor**。

    返回 ``(alive, error)``：
      * ``False`` —— 进程不存在 / 是僵尸 / create_time 与 supervisor_started_at 差 > 5s
        （pid 复用，旧 supervisor 早没了，这个 pid 是别人）；
      * ``True``  —— 进程存在且（可校验时）确实是那个 supervisor；
      * ``None``  —— 无法判断（pid 字段非法）。
    """
    pid_value = _as_int(pid)
    if pid_value is None or pid_value <= 0:
        return None, "pid 不是合法整数：%r" % (pid,)
    if psutil is None:
        # 没有 psutil 就无法按 create_time 识别 pid 复用，也无法安全地探测进程：
        # 宁可报 unknown + 原话，也不假装存活（更不能用 os.kill(pid, 0) —— Windows 上
        # 它走 TerminateProcess，sig=0 会真的把进程杀掉，违背"只读"）。
        return None, "psutil 不可用，无法校验 pid 存活 / create_time"
    try:
        proc = psutil.Process(pid_value)
        create_time = float(proc.create_time())
    except psutil.NoSuchProcess:
        return False, None
    except psutil.ZombieProcess:
        return False, None
    except psutil.AccessDenied:
        # 进程确实在，但拿不到 create_time：不能证明它被复用，按存活处理。
        return True, None
    except Exception as exc:  # noqa: BLE001 - 单个 pid 的探测异常不坏整个 lane
        return None, "探测 pid 失败：%s: %s" % (type(exc).__name__, exc)

    if started_at is not None and abs(create_time - started_at) > PID_REUSE_TOLERANCE:
        return False, (
            "pid %d 已被复用：进程 create_time=%.1f 与 supervisor_started_at=%.1f "
            "相差 %.1f 秒（阈值 %.0f 秒），按 offline 处理"
            % (pid_value, create_time, started_at, abs(create_time - started_at), PID_REUSE_TOLERANCE)
        )
    return True, None


def _task_view(raw: Any, now: float) -> Optional[Dict[str, Any]]:
    """task 白名单 + 运行时长；``null`` 原样返回 None（不编字段）。"""
    if not isinstance(raw, dict):
        return None
    claimed_at = _as_float(raw.get("claimed_at"))
    running_seconds = None
    if claimed_at is not None:
        running_seconds = round(max(0.0, now - claimed_at), 1)
    title = raw.get("title")
    return {
        "id": raw.get("id"),
        "run_id": raw.get("run_id"),
        "title": title if isinstance(title, str) else (None if title is None else str(title)),
        "claimed_at": claimed_at,
        "running_seconds": running_seconds,
    }


def _last_result_view(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    return {
        "task": raw.get("task"),
        "action": raw.get("action"),
        "at": _as_float(raw.get("at")),
        "reason": raw.get("reason"),
    }


def _empty_lane(assignee: str, error: str) -> Dict[str, Any]:
    """未知 lane 的完整白名单形状（前端不必判字段存在性）。"""
    return {
        "assignee": assignee,
        "lane_script": None,
        "adapter": _adapter_of(assignee, None),
        "liveness": "unknown",
        "age_seconds": None,
        "pid": None,
        "pid_alive": None,
        "poll_interval": None,
        "state": None,
        "task": None,
        "last_result": None,
        "last_poll_at": None,
        "error": error,
    }


# ---------------------------------------------------------------------------
# 单条 heartbeat → lane
# ---------------------------------------------------------------------------
def read_lane(path: Path, now: float) -> Dict[str, Any]:
    """读一个心跳文件并算出 lane。**本函数不抛异常**（坏文件 → unknown + error）。"""
    assignee = path.stem
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return _empty_lane(assignee, "心跳文件不存在：%s" % path)
    except OSError as exc:
        return _empty_lane(assignee, "读取心跳文件失败：%s: %s" % (type(exc).__name__, exc))

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        # 「error 原话」：把 json 模块的原话一字不改地给前端。
        return _empty_lane(assignee, "心跳文件 JSON 解析失败：%s" % exc)
    if not isinstance(raw, dict):
        return _empty_lane(assignee, "心跳文件根节点不是对象：%s" % type(raw).__name__)

    schema = _as_int(raw.get("schema"))
    if schema != SUPPORTED_SCHEMA:
        return _empty_lane(
            assignee, "schema=%r 不受支持（期望 %d）" % (raw.get("schema"), SUPPORTED_SCHEMA)
        )

    if isinstance(raw.get("assignee"), str) and raw.get("assignee").strip():
        assignee = raw.get("assignee").strip()
    script_name = _script_name(raw.get("lane_script"))

    last_poll_at = _as_float(raw.get("last_poll_at"))
    if last_poll_at is None:
        return _empty_lane(assignee, "last_poll_at 不是数字：%r" % (raw.get("last_poll_at"),))

    poll_interval = _as_float(raw.get("poll_interval"))
    if poll_interval is None or poll_interval <= 0:
        poll_interval = DEFAULT_POLL_INTERVAL
    supervisor_started_at = _as_float(raw.get("supervisor_started_at"))

    pid_alive, pid_error = _probe_pid(raw.get("pid"), supervisor_started_at)

    age_seconds = round(max(0.0, now - last_poll_at), 1)
    fresh_window = max(3.0 * poll_interval, MIN_FRESH_WINDOW)

    error = pid_error
    if pid_alive is False:
        liveness = "offline"
    elif pid_alive is None:
        liveness = "unknown"
    elif now - last_poll_at <= fresh_window:
        liveness = "online"
    else:
        liveness = "stalled"

    lane: Dict[str, Any] = {
        "assignee": assignee,
        "lane_script": script_name,
        "adapter": _adapter_of(assignee, script_name),
        "liveness": liveness,
        "age_seconds": age_seconds,
        "pid": _as_int(raw.get("pid")),
        "pid_alive": pid_alive,
        "poll_interval": poll_interval,
        "state": raw.get("state") if isinstance(raw.get("state"), str) else None,
        "task": _task_view(raw.get("task"), now),
        "last_result": _last_result_view(raw.get("last_result")),
        "last_poll_at": last_poll_at,
        "error": error,
    }
    return lane


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.get("/lanes")
def list_lanes() -> Dict[str, Any]:
    """两条（及目录里出现的其它）执行 lane 的只读快照。

    ``GET /lanes`` →
    ``{"ok": true, "data": {"now": 1793774130.0, "status_dir": "...", "lanes": [...]}}``

    目录不存在 / 空 ⇒ 仍然 200：两条已知 lane 各一条 ``liveness=unknown`` + ``error``。
    单个文件写坏 ⇒ 只有那一条变 unknown，其它照常。
    """
    directory = status_dir()
    now = time.time()
    lanes: Dict[str, Dict[str, Any]] = {}

    try:
        files = sorted(p for p in directory.glob("*.json") if p.is_file()) if directory.is_dir() else []
    except OSError:
        # 目录存在但不可枚举：只影响本次“有哪些文件”，已知 lane 照样返回 unknown。
        files = []

    for path in files:
        try:
            lane = read_lane(path, now)
        except Exception as exc:  # noqa: BLE001 - 单文件故障隔离，不把整个端点打成 500
            lane = _empty_lane(path.stem, "读取 lane %s 失败：%s: %s" % (path.name, type(exc).__name__, exc))
        lanes[lane["assignee"]] = lane

    # 已知 lane 一个都不能少：目录没建 / 文件没产出时也要在面板上出现。
    for name in EXPECTED_ASSIGNEES:
        if name not in lanes:
            lanes[name] = _empty_lane(name, "心跳文件不存在：%s" % (directory / (name + ".json")))

    order = {name: index for index, name in enumerate(EXPECTED_ASSIGNEES)}
    ordered: List[Dict[str, Any]] = sorted(
        lanes.values(), key=lambda lane: (order.get(lane["assignee"], len(order)), lane["assignee"])
    )

    return {
        "ok": True,
        "data": {
            "now": round(now, 3),
            "status_dir": str(directory),
            "lanes": ordered,
        },
    }


__all__ = ["router", "read_lane", "status_dir", "hermes_home"]

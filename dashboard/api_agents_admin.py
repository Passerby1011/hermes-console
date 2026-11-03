# -*- coding: utf-8 -*-
"""hermes-console · Agent 总览与管理（C7）。

挂载契约：本文件不单独挂载。``plugin_api.py`` 把本目录加进 ``sys.path`` 后 import 本模块，
再 ``include_router(api_agents_admin.router)``；最终 URL 是
``/api/plugins/hermes-console/agents/...``。

路由
====  ==========================================  ==================================
方法  路径                                        说明
====  ==========================================  ==================================
GET   ``/agents/overview``                        一屏 9 个 agent 的状态行（并发 + 失败隔离）
GET   ``/agents/{id}/sessions?limit&offset``      单个 agent 的会话列表（分页）
GET   ``/agents/{id}/cron?limit&offset``          单个 agent 的定时任务列表（分页）
====  ==========================================  ==================================

五条硬约束（都是本卡的验收点，不是风格偏好）
--------------------------------------------
1. **失败隔离**：9 个 agent 各自独立 try + 独立超时（``AGENT_TIMEOUT_S``）。某个 profile 的
   ``state.db`` 被占用/损坏/不存在，只有那一行 ``available=false`` + ``unavailable_reason``，
   其余行照常返回；整页永不 500。
2. **拿不到就是 null**：所有数字字段默认 ``None``，绝不填 0 冒充真实值。字段级的失败
   （例如 cron store 坏了但 state.db 正常）写进 ``field_errors``，不影响其它字段。
3. **每个数字带口径**：``calibers`` 字典随响应返回，前端只负责排版，不自己发明说法。
4. **只读**：本文件没有任何写操作。启停网关 / 改配置 / 代发消息都不在这里（越权边界）。
5. **不写死 agent 列表**：枚举走 ``api_agents._profile_ids()``（动态扫描 ``profiles/``），
   新增 profile 下次请求即出现。

取数来源（都是宿主已迁到 ``hermes_cli.web_routers.*`` 的新路径，不是旧 ``web_server``）::

    sessions  hermes_cli.web_server_sessions._open_session_db_for_profile / SessionDB
    cron      hermes_cli.web_routers.cron.list_cron_jobs(profile=...)
    gateway   hermes_cli.web_server_gateway._collect_profile_gateway_topology（10s 缓存）
              + 各 profile 自己的 gateway_state.json
    config    该 profile 的 config.yaml（model.default / plugins.enabled）

gateway 为什么不全信 ``gateway_state.json``：那份文件在进程退出后仍然留着 ``running``
（实测 work 的记录停在 2026-08-19），所以“在跑”以拓扑（活的 pid）为准，
``gateway_state.json`` 只作为 ``state``/``updated_at`` 的历史快照上报。
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, Query  # type: ignore
from fastapi.responses import JSONResponse  # type: ignore

_DASHBOARD_DIR = Path(__file__).resolve().parent
if str(_DASHBOARD_DIR) not in sys.path:
    # 兄弟模块（api_agents）只能靠 sys.path 找到：本文件被 dashboard 用合成模块名 exec，
    # 没有任何包上下文。plugin_api.py 已经插过一次，这里兜住单独 import 的场景。
    sys.path.insert(0, str(_DASHBOARD_DIR))

from api_agents import (  # noqa: E402 - 依赖上面的 sys.path 插入
    DEFAULT_PROFILE,
    _agent_label,
    _profile_dir,
    _profile_ids,
    hermes_home,
)

router = APIRouter()

logger = logging.getLogger(__name__)

#: 单 agent 取数上限（秒）。实测：default 的 2 GB state.db 0.26s、其余 ≤0.02s、
#: cron ≤0.12s，3s 是“慢盘 + 冷缓存”的余量，不是常态耗时。
AGENT_TIMEOUT_S = 3.0

#: 单 agent 会话/任务列表的上限（底层 get_sessions 要开另一个 profile 的 state.db，
#: 与概览分区同一档：慢不等于坏）。
HEAVY_TIMEOUT_S = 25.0

#: 会话列表分页上限（与底层 ``get_sessions(limit<=100)`` 对齐）。
SESSIONS_MAX_LIMIT = 100

#: cron 列表默认每页条数（任务数本来就少：实测 default 12 / xiaomo 11）。
CRON_DEFAULT_LIMIT = 50

#: “活跃会话”窗口（秒）。与宿主 ``web_routers.sessions._ACTIVE_WINDOW_S`` 同值，
#: 两边说法不一致会让“活跃”这个词失去意义。
ACTIVE_WINDOW_S = 300

#: 判定活跃时最多回看多少条会话（按最近活动倒序取前 N 条）。
ACTIVE_SCAN_LIMIT = 50

#: 列表接口只回这些字段（底层一行 60+ 列，含 origin_json/system_prompt 这类大字段）。
SESSION_FIELDS: Tuple[str, ...] = (
    "id",
    "title",
    "display_name",
    "message_count",
    "started_at",
    "last_active",
    "ended_at",
    "profile",
    "is_active",
    "model",
    "tool_call_count",
)

CRON_FIELDS: Tuple[str, ...] = (
    "id",
    "name",
    "enabled",
    "paused_at",
    "paused_reason",
    "state",
    "next_run_at",
    "last_run_at",
    "last_status",
    "last_error",
    "schedule_display",
    "profile",
    "repeat",
)

#: 每个数字的口径（前端原样展示，不另写一套说法）。
CALIBERS: Dict[str, str] = {
    "sessions_total": (
        "该 profile 的 state.db 里会话总数（session_count(exclude_children=True)："
        "不含子会话与压缩链子节点，含已归档）"
    ),
    "sessions_active": (
        "按最近活动倒序取前 50 条，其中未结束（ended_at 为空）且最近 "
        + str(ACTIVE_WINDOW_S)
        + " 秒内有活动的会话数 —— 与宿主 /api/status 的 active_sessions 同口径"
    ),
    "last_active_at": "该 profile 最近一次会话活动的时间戳（秒级 epoch），取自会话行的 last_active",
    "cron_total": "该 profile 的 cron/jobs.json 全部任务数（含已暂停、含已完成的一次性任务）",
    "cron_enabled": "enabled=true 且 paused_at 为空的任务数",
    "cron_paused": "paused_at 非空的任务数（暂停的任务 enabled 仍可能为 true）",
    "model": "该 profile 自己的 config.yaml 里 model.default（没有就是 null，不继承其它 profile、不猜）",
    "plugins_enabled_count": "该 profile 的 config.yaml 里 plugins.enabled 列表的长度（没这个键就是 null）",
    "gateway.running": "拓扑里该 profile 有活的网关进程（自己那份，或 default 的多路复用器）为 true",
    "gateway.pid": "活网关进程的 pid；gateway_state.json 里的 pid 只作历史快照，不作存活依据",
    "gateway.port": (
        "该网关在本机绑定的平台端口（拓扑 ports）；多路复用器没有 HTTP 平台时不绑定端口，"
        "所以这里通常是 null 而不是 0"
    ),
    "data_at": "本行数据的取数时刻（秒级 epoch），不是数据本身的更新时间",
    "available": "该 profile 的 state.db 能否只读打开并计数；打不开只影响这一行",
}


# ---------------------------------------------------------------------------
# 取数工具
# ---------------------------------------------------------------------------
def _load(module_name: str, attr: str) -> Callable[..., Any]:
    """惰性 import 宿主函数。

    **不做进程内缓存**：命中缓存后 monkeypatch 就失效了，而失败隔离测试正是靠
    monkeypatch 证明“只有那一行坏”。``sys.modules`` 本身已经缓存了 import 成本。
    """
    module = importlib.import_module(module_name)
    func = getattr(module, attr)
    if not callable(func):
        raise TypeError(module_name + "." + attr + " 不是可调用对象")
    return func


def _describe(exc: BaseException, timeout_s: float = AGENT_TIMEOUT_S) -> str:
    """异常 → 一句人话（保留原始信息，前端要能看到“为什么”）。"""
    if isinstance(exc, asyncio.TimeoutError):
        return "取数超过 " + str(int(timeout_s)) + " 秒仍未返回，已放弃本次读取"
    if isinstance(exc, (ImportError, AttributeError)):
        return "宿主接口不可用：" + type(exc).__name__ + "：" + str(exc)
    if isinstance(exc, FileNotFoundError):
        return "数据文件不存在：" + str(exc)
    if isinstance(exc, PermissionError):
        return "数据文件不可读（权限/占用）：" + str(exc)
    return type(exc).__name__ + "：" + str(exc)


def _project(rows: List[Dict[str, Any]], fields: Tuple[str, ...]) -> List[Dict[str, Any]]:
    """只留 UI 要用的字段；缺失的键不出现在结果里（前端据此显示 ``—``）。"""
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append({field: row[field] for field in fields if field in row})
    return out


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    """读一个小 JSON 文件；任何失败都返回 None（“没有”不等于“坏了”，两者都在注释里说明）。"""
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - 快照读不到只是没有历史，不阻断整行
        return None


# ---------------------------------------------------------------------------
# 网关快照（一次采集，9 行共用）
# ---------------------------------------------------------------------------
def _gateway_context(root: Path) -> Dict[str, Any]:
    """采集一次网关拓扑 + 各 profile 的 ``gateway_state.json``。

    返回 ``{"by_profile": {...}, "mode": str|None, "error": str|None, "default_state": {...}}``。
    拓扑取不到时 ``error`` 非空，9 行的 ``gateway`` 一律 null + 原因（不猜、不填 false）。
    """
    ctx: Dict[str, Any] = {"by_profile": {}, "mode": None, "error": None, "source": "",
                           "default_state": _read_json(root / "gateway_state.json") or {}}
    try:
        collect = _load("hermes_cli.web_server_gateway", "_collect_profile_gateway_topology_cached")
        topology = collect() or {}
        ctx["source"] = "hermes_cli.web_server_gateway._collect_profile_gateway_topology_cached"
        ctx["mode"] = topology.get("gateway_mode")
        for entry in topology.get("gateways") or []:
            if isinstance(entry, dict) and entry.get("profile"):
                ctx["by_profile"][str(entry["profile"])] = entry
    except Exception as exc:  # noqa: BLE001 - 拓扑是“可选证据”，缺了就说缺了
        logger.warning("hermes-console: 网关拓扑不可用：%s", exc)
        ctx["error"] = _describe(exc)
    return ctx


def _gateway_field(pid: str, home: Path, ctx: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(gateway 字段, 取不到时的原因)。

    ``running`` 只认拓扑（活 pid）；``gateway_state.json`` 只提供 ``state``/``updated_at``
    这类历史快照 —— 它在一进程退出后仍然写着 ``running``。
    """
    state = _read_json(home / "gateway_state.json") or {}
    snapshot = {
        "state": state.get("gateway_state"),
        "updated_at": state.get("updated_at"),
        "recorded_pid": state.get("pid"),
        "active_agents": state.get("active_agents"),
    }
    if ctx.get("error"):
        return None, "网关拓扑取不到（" + str(ctx["error"]) + "），无法判断该 agent 的网关是否在跑"

    by_profile: Dict[str, Any] = ctx["by_profile"]
    default_entry = by_profile.get(DEFAULT_PROFILE) or {}
    served = [str(p) for p in (default_entry.get("served_profiles") or [])]
    own = by_profile.get(pid)

    if own is not None:
        ports = own.get("ports") or {}
        port = next(iter(ports.values()), None) if isinstance(ports, dict) else None
        multiplexer = pid == DEFAULT_PROFILE and len(served) > 1
        return (
            {
                "running": True,
                "pid": snapshot.get("recorded_pid"),
                "port": port if isinstance(port, int) else None,
                "mode": "multiplexer" if multiplexer else "own",
                "state": snapshot.get("state"),
                "updated_at": snapshot.get("updated_at"),
                "active_agents": snapshot.get("active_agents"),
                "served_profiles": served or None,
                "note": (
                    "本 profile 自己的网关进程在跑"
                    if not multiplexer
                    else "default 的多路复用网关在跑，同时服务 " + str(len(served)) + " 个 profile"
                ),
            },
            None,
        )

    if pid in served:
        default_state = ctx.get("default_state") or {}
        return (
            {
                "running": True,
                # 多路复用器就是 default 的网关进程：pid 取自 default 的 gateway_state.json，
                # 拓扑条目只带 profile/ports/served_profiles，没有 pid。
                "pid": default_state.get("pid") or snapshot.get("recorded_pid"),
                "port": None,
                "mode": "multiplexed",
                "served_by": DEFAULT_PROFILE,
                "state": snapshot.get("state") if snapshot.get("state") else None,
                "updated_at": snapshot.get("updated_at"),
                "active_agents": None,
                "note": (
                    "没有自己的网关进程，由 default 的多路复用网关承载；"
                    "该网关未绑定平台端口，故 port 为 null"
                ),
            },
            None,
        )

    return (
        {
            "running": False,
            "pid": None,
            "port": None,
            "mode": "stale" if snapshot else "none",
            "state": snapshot.get("state"),
            "updated_at": snapshot.get("updated_at"),
            "recorded_pid": snapshot.get("recorded_pid"),
            "active_agents": None,
            "note": (
                "拓扑里没有该 profile 的活网关"
                + (
                    "；gateway_state.json 最后记录 state=" + str(snapshot.get("state"))
                    + " pid=" + str(snapshot.get("recorded_pid"))
                    + "（进程已不在，属历史快照）"
                    if snapshot
                    else "；也没有 gateway_state.json"
                )
            ),
        },
        None,
    )


# ---------------------------------------------------------------------------
# 单 agent 取数（同步；由路由放进线程池 + 独立超时）
# ---------------------------------------------------------------------------
def _sessions_metrics(pid: str) -> Dict[str, Any]:
    """会话三件套。**打不开 state.db 就抛**（这是 available 的判据）。"""
    from hermes_cli.web_server_sessions import (  # 惰性：宿主升级挪走时降级成一行原因
        _open_session_db_for_profile,
        _session_db_path_for_profile,
    )

    db_path = Path(_session_db_path_for_profile(pid))
    if not db_path.is_file():
        # 显式拦在 open 之前：_open_session_db_for_profile 会 bootstrap 一个空库，
        # 那会把“这个 profile 没有库”变成“有 0 个会话”——正是本卡禁止的假数据。
        raise FileNotFoundError(str(db_path))
    db = _open_session_db_for_profile(pid, read_only=True)
    try:
        total = db.session_count(exclude_children=True)
        rows = db.list_sessions_rich(
            limit=ACTIVE_SCAN_LIMIT, compact_rows=True, order_by_last_active=True
        )
        now = time.time()
        active = sum(
            1
            for row in rows
            if row.get("ended_at") is None
            and (now - (row.get("last_active") or row.get("started_at") or 0)) < ACTIVE_WINDOW_S
        )
        last_active = rows[0].get("last_active") if rows else None
        return {
            "sessions_total": total,
            "sessions_active": active,
            "last_active_at": last_active,
            "db_path": str(db_path),
        }
    finally:
        db.close()


def _cron_metrics(pid: str) -> Dict[str, Any]:
    """cron 三件套。``list_cron_jobs`` 是 async 路由函数，这里在自己的事件循环里跑。"""
    func = _load("hermes_cli.web_routers.cron", "list_cron_jobs")
    jobs = asyncio.run(func(profile=pid))
    if not isinstance(jobs, list):
        raise TypeError("list_cron_jobs 返回的不是列表：" + type(jobs).__name__)
    enabled = 0
    paused = 0
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if job.get("enabled") and not job.get("paused_at"):
            enabled += 1
        if job.get("paused_at"):
            paused += 1
    return {"cron_total": len(jobs), "cron_enabled": enabled, "cron_paused": paused}


def _config_metrics(home: Path) -> Dict[str, Any]:
    """该 profile 的 model 与 plugins.enabled 计数。**不继承**其它 profile 的配置。"""
    import yaml  # 宿主环境自带

    raw = (home / "config.yaml").read_text(encoding="utf-8", errors="replace")
    cfg = yaml.safe_load(raw) or {}
    if not isinstance(cfg, dict):
        raise ValueError("config.yaml 顶层不是映射")
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    model = None
    for key in ("default", "name", "model"):
        value = model_cfg.get(key)
        if isinstance(value, str) and value.strip():
            model = value.strip()
            break
    plugins_cfg = cfg.get("plugins") if isinstance(cfg.get("plugins"), dict) else None
    plugins_count: Optional[int] = None
    if plugins_cfg is not None and isinstance(plugins_cfg.get("enabled"), list):
        plugins_count = len(plugins_cfg["enabled"])
    return {"model": model, "plugins_enabled_count": plugins_count,
            "provider": model_cfg.get("provider")}


def _collect_row(pid: str, root: Path, home: Path, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """一行 = 一个 agent。三段取数各自 try，互不连坐。

    ``root`` 是 Hermes 主目录（``_agent_label`` 要它才能自己拼 profile 目录），
    ``home`` 是该 profile 自己的目录（config.yaml / gateway_state.json 在这里）。
    """
    started = time.time()
    row: Dict[str, Any] = {
        "id": pid,
        "label": _agent_label(pid, root),
        "is_default": pid == DEFAULT_PROFILE,
        "available": True,
        "unavailable_reason": None,
        "gateway": None,
        "sessions_total": None,
        "sessions_active": None,
        "last_active_at": None,
        "cron_total": None,
        "cron_enabled": None,
        "cron_paused": None,
        "model": None,
        "plugins_enabled_count": None,
        "data_at": started,
        "field_errors": {},
    }
    errors: Dict[str, str] = row["field_errors"]

    # 1) 会话 —— available 的判据
    try:
        row.update(_sessions_metrics(pid))
    except Exception as exc:  # noqa: BLE001 - 单行隔离：这一行标不可用，其余行照常
        row["available"] = False
        row["unavailable_reason"] = _describe(exc)
        logger.warning("hermes-console: agent %s 的 state.db 不可用：%s", pid, exc)

    # 2) cron
    try:
        row.update(_cron_metrics(pid))
    except Exception as exc:  # noqa: BLE001
        errors["cron_total"] = _describe(exc)

    # 3) config（model / plugins）
    try:
        model_cfg = _config_metrics(home)
        row["model"] = model_cfg.get("model")
        row["provider"] = model_cfg.get("provider")
        row["plugins_enabled_count"] = model_cfg.get("plugins_enabled_count")
        if row["model"] is None:
            errors["model"] = "该 profile 的 config.yaml 没有 model.default"
        if row["plugins_enabled_count"] is None:
            errors["plugins_enabled_count"] = "该 profile 的 config.yaml 没有 plugins.enabled 列表"
    except Exception as exc:  # noqa: BLE001
        errors["model"] = _describe(exc)
        errors["plugins_enabled_count"] = errors["model"]

    # 4) 网关（共享快照，不单独耗时）
    gateway, gateway_reason = _gateway_field(pid, home, ctx)
    row["gateway"] = gateway
    if gateway_reason:
        errors["gateway"] = gateway_reason

    row["data_at"] = time.time()
    row["elapsed_ms"] = int((row["data_at"] - started) * 1000)
    return row


async def _collect_row_bounded(
    pid: str, root: Path, home: Path, ctx: Dict[str, Any]
) -> Dict[str, Any]:
    """单 agent 独立超时 + 独立异常兜底。**这里是失败隔离的落点。**"""
    started = time.time()
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_collect_row, pid, root, home, ctx), AGENT_TIMEOUT_S
        )
    except Exception as exc:  # noqa: BLE001 - 超时/线程内异常都只影响这一行
        logger.warning("hermes-console: agent %s 取数失败：%s", pid, exc)
        try:
            label = _agent_label(pid, root)
        except Exception:  # noqa: BLE001 - 连名字都拼不出来时用 id 兜住，不要因为名字再崩一次
            label = pid
        return {
            "id": pid,
            "label": label,
            "is_default": pid == DEFAULT_PROFILE,
            "available": False,
            "unavailable_reason": _describe(exc),
            "gateway": None,
            "sessions_total": None,
            "sessions_active": None,
            "last_active_at": None,
            "cron_total": None,
            "cron_enabled": None,
            "cron_paused": None,
            "model": None,
            "plugins_enabled_count": None,
            "data_at": time.time(),
            "elapsed_ms": int((time.time() - started) * 1000),
            "field_errors": {},
        }


# ---------------------------------------------------------------------------
# 路由：总览
# ---------------------------------------------------------------------------
def _fail(code: str, message: str, status_code: int = 200) -> JSONResponse:
    """结构化失败。数据源坏了是 200（页面照常渲染），只有请求本身非法才 4xx。"""
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "error": {"code": code, "message": message}},
    )


@router.get("/agents/overview")
async def get_agents_overview() -> Any:
    """一屏全部 agent。**并发取数 + 逐行隔离，整页不允许 500。**"""
    requested_at = time.time()
    try:
        home = hermes_home()
        ids = _profile_ids(home)
    except Exception as exc:  # noqa: BLE001 - 连 profile 都枚举不出来才算真坏
        logger.warning("hermes-console: /agents/overview 枚举 profile 失败：%s", exc)
        return _fail("discovery_failed", "枚举 profile 失败：" + _describe(exc))

    ctx = _gateway_context(home)
    homes = {pid: _profile_dir(pid, home) for pid in ids}
    rows = await asyncio.gather(
        *[_collect_row_bounded(pid, home, homes[pid], ctx) for pid in ids]
    )

    return {
        "ok": True,
        "generated_at": time.time(),
        "elapsed_ms": int((time.time() - requested_at) * 1000),
        "agent_count": len(rows),
        "agents": list(rows),
        "calibers": CALIBERS,
        "gateway": {
            "mode": ctx.get("mode"),
            "source": ctx.get("source"),
            "error": ctx.get("error"),
        },
        "home": str(home),
    }


# ---------------------------------------------------------------------------
# 路由：单 agent 下钻
# ---------------------------------------------------------------------------
def _profile_home(pid: str) -> Tuple[Optional[Path], Optional[JSONResponse]]:
    """(profile 目录, 错误响应)。未知 agent → 404，不让它变成别的 profile 的数据。"""
    home = hermes_home()
    try:
        known = _profile_ids(home)
    except Exception as exc:  # noqa: BLE001
        return None, _fail("discovery_failed", "枚举 profile 失败：" + _describe(exc))
    if pid not in known:
        return None, _fail(
            "unknown_agent",
            "没有这个 agent：" + pid + "（现有：" + "、".join(known) + "）",
            status_code=404,
        )
    return _profile_dir(pid, home), None


@router.get("/agents/{agent_id}/sessions")
async def get_agent_sessions(
    agent_id: str,
    limit: int = Query(20, description="每页条数，钳到 1..100"),
    offset: int = Query(0, description="偏移，>=0"),
) -> Any:
    """单个 agent 的会话列表（只读分页）。透视 ``profile=`` 给宿主 ``get_sessions``。"""
    _, failure = _profile_home(agent_id)
    if failure is not None:
        return failure
    page_limit = max(1, min(SESSIONS_MAX_LIMIT, int(limit)))
    page_offset = max(0, int(offset))
    source = "hermes_cli.web_routers.sessions.get_sessions"
    try:
        func = _load("hermes_cli.web_routers.sessions", "get_sessions")
        payload = await asyncio.wait_for(
            asyncio.to_thread(
                func,
                limit=page_limit,
                offset=page_offset,
                order="recent",
                profile=agent_id,
            ),
            HEAVY_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes-console: %s 的会话列表失败：%s", agent_id, exc)
        return _fail("upstream_error", _describe(exc, HEAVY_TIMEOUT_S))
    payload = payload or {}
    rows = payload.get("sessions") or []
    return {
        "ok": True,
        "agent": agent_id,
        "source": source,
        "data": {
            "sessions": _project(rows, SESSION_FIELDS),
            "total": payload.get("total"),
            "limit": page_limit,
            "offset": page_offset,
            # 空列表 vs 存储损坏要能区分（宿主用同一形状报告损坏）
            "storage": payload.get("storage") or {},
        },
        "calibers": {
            "total": CALIBERS["sessions_total"],
            "limit/offset": "服务端分页，按 order=recent（跨压缩链的最新活动时间）排序",
            "last_active": "该会话最后一次活动的时间戳（秒），不是创建时间",
            "is_active": "ended_at 为空且最近 " + str(ACTIVE_WINDOW_S) + " 秒内有活动",
        },
    }


@router.get("/agents/{agent_id}/cron")
async def get_agent_cron(
    agent_id: str,
    limit: int = Query(CRON_DEFAULT_LIMIT, description="每页条数，钳到 1..500"),
    offset: int = Query(0, description="偏移，>=0"),
) -> Any:
    """单个 agent 的定时任务列表（只读分页）。透视 ``profile=`` 给宿主 ``list_cron_jobs``。"""
    _, failure = _profile_home(agent_id)
    if failure is not None:
        return failure
    page_limit = max(1, min(500, int(limit)))
    page_offset = max(0, int(offset))
    source = "hermes_cli.web_routers.cron.list_cron_jobs"
    try:
        func = _load("hermes_cli.web_routers.cron", "list_cron_jobs")
        jobs = await asyncio.wait_for(func(profile=agent_id), HEAVY_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes-console: %s 的 cron 列表失败：%s", agent_id, exc)
        return _fail("upstream_error", _describe(exc, HEAVY_TIMEOUT_S))
    jobs = jobs if isinstance(jobs, list) else []
    enabled = sum(1 for j in jobs if isinstance(j, dict) and j.get("enabled") and not j.get("paused_at"))
    paused = sum(1 for j in jobs if isinstance(j, dict) and j.get("paused_at"))
    page = jobs[page_offset:page_offset + page_limit]
    return {
        "ok": True,
        "agent": agent_id,
        "source": source,
        "data": {
            "jobs": _project(page, CRON_FIELDS),
            "total": len(jobs),
            "limit": page_limit,
            "offset": page_offset,
            "counts": {"total": len(jobs), "enabled": enabled, "paused": paused},
        },
        "calibers": {
            "total": CALIBERS["cron_total"],
            "enabled": CALIBERS["cron_enabled"],
            "paused": CALIBERS["cron_paused"],
            "limit/offset": "服务端切片（先取该 profile 全量任务再切，任务量本身很小）",
        },
    }

# -*- coding: utf-8 -*-
"""hermes-console · 概览分区后端（搬自 ``plugins/home-dashboard/dashboard/plugin_api.py``）。

挂载契约：本文件不单独挂载。``plugin_api.py``（manifest 的 ``api`` 键指向的文件）把本目录
加进 ``sys.path`` 后 import 本模块，再 ``include_router(api_overview.router)``；最终 URL 是
``/api/plugins/hermes-console/overview/...``。

路由
====  ==============================  ==========================================
方法  路径                            说明
====  ==============================  ==========================================
GET   ``/overview/layout``            看板挂件布局（只读）
PUT   ``/overview/layout``            写回布局（保留能力，本期不做拖拽 UI）
GET   ``/overview/system``            宿主 / 进程指标
GET   ``/overview/analytics``         token / 调用量（按天窗口）
GET   ``/overview/cron``              定时任务列表
GET   ``/overview/sessions``          会话列表（分页）
GET   ``/overview/status``            网关 / 版本 / 活跃会话（宿主级读数）
GET   ``/overview/logs``              日志尾窗口 + 按级别计数（按 agent 的 profile 读）
GET   ``/overview``                   分区自检（哪些子路由真的接上了）
====  ==============================  ==========================================

三条设计决定（都是旧代码的坑，不是风格偏好）
--------------------------------------------
1. **取数走新路径，且惰性 import**。旧 ``plugin_api.py`` 从 ``hermes_cli.web_server`` 导入
   ``get_system_stats`` / ``get_usage_analytics`` / ``list_cron_jobs`` / ``get_sessions``；这四个
   2026-10-26 已迁到 ``hermes_cli.web_routers/{status,analytics,cron,sessions}.py``，旧路径
   实测 ``ImportError``。这里全部改新路径，并且**在请求里 import**：某个域不可用只让那一张卡
   进错误态，不会让插件整体挂不上，也不会变成 500 栈。
2. **不裸抛**。每个路由都有超时 + 异常兜底，失败返回 ``{ok:false, error:{code, message}}``。
   数据源不可用是 HTTP 200（其它卡片照常渲染），只有请求本身非法（布局文档不合法 / agent
   没有等价语义）才 400。
3. **多 agent 不撒谎**。``agent`` 查询参数（缺省 ``default``）透传给底层 ``profile=``；底层
   确实不按 agent 取数的（宿主系统指标、布局文件只有一份），在 ``scope.kind`` 里写明，
   绝不拿 default 的数据冒充目标 agent 的。**日志也一样**（2026-10-30 修）：每个 profile
   有自己的一份 ``logs/``，``/overview/logs`` 把 ``agent`` 透传成 ``profile=``，
   不再"收下参数就扔掉、偷偷读本进程那一份"。
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, Query  # type: ignore
from fastapi.responses import JSONResponse  # type: ignore

router = APIRouter()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量（集中定义：所有会漂移的字面量只出现在这里）
# ---------------------------------------------------------------------------

#: 缺省作用域。前端选择器一定会传具体 agent；没传时按 ``default`` profile 取数（不是"进程碰巧
#: 是谁"，那种静默会让同一份 URL 在不同进程里返回不同人的数据）。
DEFAULT_AGENT = "default"

#: 跨 profile 聚合的哨兵值（只有 ``/cron`` 认它，其它路由会得到结构化错误）。
AGENT_ALL = "all"

#: **布局文件原地沿用 ``plugins/home-dashboard/layout.json``**。
#:
#: 为什么指向旧插件目录而不是搬过来：那是老板已经排好并保存过的看板布局（10 个挂件的
#: 位置/尺寸/``props``），搬一份新的等于让他重排；而且旧插件在阶段①收尾前仍然启用，
#: 两边读同一个文件才不会出现"新面板看到空布局"。所以本插件**只读复用**这个路径，
#: 不搬迁、不重建；``PUT /overview/layout`` 也写回同一份（阶段①收尾下线旧插件时，
#: 只需把 ``LAYOUT_RELATIVE`` 改到 hermes-console 目录，不动其它代码）。
LAYOUT_PLUGIN_ID = "home-dashboard"
LAYOUT_FILENAME = "layout.json"
LAYOUT_RELATIVE: Tuple[str, ...] = ("plugins", LAYOUT_PLUGIN_ID, LAYOUT_FILENAME)

#: 布局文档的版本号与挂件上限（与旧实现一致，避免读得进写不出）。
LAYOUT_VERSION = 1
MAX_WIDGETS = 64

#: 单次取数上限（秒）。宿主 SQLite 在锁竞争时可能长时间不返回，这里必须给前端一个确定的
#: 结束点：超时 → ``{ok:false, error:{code:"timeout"}}``，卡片显示"没读到 + 原因"。
FETCH_TIMEOUT_S = 8.0

#: 重查询的上限。``/analytics`` 实测 2.4–4.2 秒（InsightsEngine + 全表聚合，default profile
#: 217 个会话、46 亿 cache_read token），机器一忙 8 秒会误杀；``/sessions`` 要开另一个
#: profile 的 state.db。给它们单独一档，别把"慢"当成"坏"。
#:
#: **必须 < 12 秒**：桌面端把插件 API 调用包成 ``hermes:api`` 远程方法，硬上限 12000ms。
#: 本层预算一旦超过它，超时永远由桌面端先触发，前端只能拿到裸的
#: "Timed out connecting to Hermes backend after 12000ms"，看不到我们自己的结构化错误。
#: 取 10 秒 = 留 2 秒给 IPC 与序列化，保证"慢"由本层判、由本层解释。
HEAVY_FETCH_TIMEOUT_S = 10.0

#: 会话列表分页上限（与底层 ``get_sessions`` 的 ge=0/le=100 对齐）。
SESSIONS_MAX_LIMIT = 100

#: 分析窗口上限（与底层路由的 ``ge=1, le=365`` 对齐）。
ANALYTICS_MAX_DAYS = 365


# ---------------------------------------------------------------------------
# 主目录 / 布局路径
# ---------------------------------------------------------------------------
def _hermes_home() -> Path:
    """Hermes 主目录。

    优先复用 ``api_agents.hermes_home()``：它按布局正向探测（不信 ``HERMES_HOME``，因为
    worker/gateway/kanban 派生进程里那个变量是"当前 profile 目录"语义）。这里不做模块级
    缓存，profile 增删后下一次请求就能看到。
    """
    try:
        from api_agents import hermes_home  # 同目录兄弟模块（plugin_api.py 已加 sys.path）

        return hermes_home()
    except Exception:  # noqa: BLE001 - 兄弟模块缺失时退化为环境探测，不阻断概览
        env_home = (os.environ.get("HERMES_HOME") or "").strip()
        if env_home:
            candidate = Path(env_home)
            # <home>/profiles/<p> → <home>：HERMES_HOME 在派生进程里是 profile 目录
            if candidate.parent.name == "profiles":
                return candidate.parent.parent
            return candidate
        return Path.home() / ".hermes"


def layout_path() -> Path:
    """看板布局文件（见 ``LAYOUT_RELATIVE`` 的注释：原地沿用 home-dashboard 那份）。"""
    return _hermes_home().joinpath(*LAYOUT_RELATIVE)


# ---------------------------------------------------------------------------
# 作用域（agent）
# ---------------------------------------------------------------------------
def _process_profile() -> str:
    """本进程自己服务的那份 profile（作用域未定时的兜底）。"""
    for env_name in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        value = (os.environ.get(env_name) or "").strip()
        if value:
            return value
    env_home = (os.environ.get("HERMES_HOME") or "").strip()
    if env_home:
        candidate = Path(env_home)
        if candidate.parent.name == "profiles":
            return candidate.name
    return DEFAULT_AGENT


def _scope(kind: str, agent: str, note: str) -> Dict[str, Any]:
    """作用域声明。``kind`` 是"这个数字到底按什么取出来的"的唯一答案：

    * ``profile``         —— 真的按 ``?agent=`` 指定的 profile 取数；
    * ``all``             —— 跨全部 profile 聚合（只有 ``/cron`` 支持）；
    * ``current_profile`` —— 作用域未定，取的是本后端进程自己那份 profile（不冒充任何 agent）；
    * ``host``            —— 宿主/进程级指标，与 agent 无关；
    * ``shared_file``     —— 单一数据文件，与 agent 无关。
    """
    return {"kind": kind, "agent": agent, "note": note}


def _resolve_agent(agent: Optional[str]) -> Tuple[Optional[str], Dict[str, Any]]:
    """``?agent=`` → (底层 ``profile=`` 入参, scope 声明)。"""
    raw = (agent or "").strip()
    if not raw:
        # 前端作用域未定（/agents 失败时就是这样）：只能取本进程那份 profile，
        # 并在响应里明说，不让前端以为这是某个 agent 的数据。
        current = _process_profile()
        return None, _scope(
            "current_profile",
            current,
            "作用域未定（前端没传 agent），取的是本后端进程自己服务的 profile：" + current,
        )
    if raw == AGENT_ALL:
        return AGENT_ALL, _scope("all", raw, "跨全部 profile 聚合（仅 /cron 支持）")
    return raw, _scope("profile", raw, "按 agent=" + raw + " 的 profile 取数")


# ---------------------------------------------------------------------------
# 取数工具：惰性 import + 超时 + 异常归一
# ---------------------------------------------------------------------------
_RESOLVED: Dict[str, Callable[..., Any]] = {}


def _load(module_name: str, attr: str) -> Callable[..., Any]:
    """从宿主取一个取数函数（惰性 + 进程内缓存）。

    import 失败会抛 ``ImportError``/``ModuleNotFoundError``，由调用方转成结构化错误 ——
    这正是"宿主升级把函数挪走"时的表现，必须降级而不是 500。
    """
    key = module_name + "::" + attr
    cached = _RESOLVED.get(key)
    if cached is not None:
        return cached
    module = importlib.import_module(module_name)
    func = getattr(module, attr)
    if not callable(func):
        raise TypeError(module_name + "." + attr + " 不是可调用对象")
    _RESOLVED[key] = func
    return func


async def _await_async(factory: Callable[[], Any], timeout_s: float = FETCH_TIMEOUT_S) -> Any:
    """跑一个 async 取数函数并加超时。

    **2026-10-29 线上那批 ``'dict' object can't be awaited`` 的根因就在这一层**：宿主
    ``get_system_stats`` 是**同步**函数，被塞进这里后 ``asyncio.wait_for(dict)`` 抛
    ``TypeError``，整张「系统状态」卡变成 ``upstream_error``（errors.log 里连着几十条
    WARNING，同一张卡每次刷新都再记一条）。当时的修法是把调用点改成 ``_await_thread``；
    这里再补一道**类型防线**：factory() 返回的不是 awaitable 时，直接把它当结果返回并
    记一条 warning，而不是让 TypeError 把卡片打成"取数失败"。

    这道防线解决的是"同一个坑换个宿主函数再踩一次"—— 宿主升级把某个 async 函数改成
    同步、或者反过来，都不会再让界面上一整张卡凭空坏掉。
    """
    pending = factory()
    if not inspect.isawaitable(pending):
        logger.warning(
            "hermes-console: _await_async 收到非 awaitable 结果（%s），按同步返回值处理；"
            "该调用点应改用 _await_thread",
            type(pending).__name__,
        )
        return pending
    return await asyncio.wait_for(pending, timeout_s)


async def _await_thread(
    func: Callable[..., Any], *args: Any, timeout_s: float = FETCH_TIMEOUT_S, **kwargs: Any
) -> Any:
    """跑一个同步取数函数（放线程池，别堵事件循环）并加超时。

    注意：超时只保证**响应**在 ``timeout_s`` 内结束，底层线程仍会跑完 —— 这是
    ``asyncio.to_thread`` 的固有行为，比"整个后端卡住"好得多。

    反向踩坑也兜住：宿主哪天把某个取数函数改成 ``async``，``to_thread`` 只会把**协程对象**
    拿回来，直接塞进 JSON 会变成 ``"<coroutine object ...>"``。这里检测到 awaitable 就
    再 await 一次，而不是把协程对象当数据返回。
    """
    result = await asyncio.wait_for(asyncio.to_thread(func, *args, **kwargs), timeout_s)
    if inspect.isawaitable(result):
        logger.warning(
            "hermes-console: _await_thread 拿到 awaitable（%s），该调用点应改用 _await_async；"
            "这里补一次 await",
            type(result).__name__,
        )
        result = await asyncio.wait_for(result, timeout_s)
    return result


def _describe_error(exc: BaseException, timeout_s: float = FETCH_TIMEOUT_S) -> Tuple[str, str]:
    """异常 → (code, message)。message 保留原始信息，前端要能看到"为什么"。"""
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout", "取数超过 " + str(int(timeout_s)) + " 秒仍未返回，已放弃本次读取"
    if isinstance(exc, ImportError):
        # 宿主升级把取数函数挪走时的典型表现（旧 home-dashboard 就是这样坏的）
        return "import_failed", "宿主接口不可用（import 失败）：" + str(exc)
    if isinstance(exc, FileNotFoundError):
        return "not_found", "数据文件不存在：" + str(exc)
    # FastAPI 的 HTTPException：把 status 与 detail 原样带出来
    status = getattr(exc, "status_code", None)
    detail = getattr(exc, "detail", None)
    if isinstance(status, int) and detail is not None:
        if status == 404:
            return "unknown_agent", str(detail)
        if status == 400:
            return "invalid_agent", str(detail)
        return "upstream_http", "底层返回 HTTP " + str(status) + "：" + str(detail)
    return "upstream_error", type(exc).__name__ + "：" + str(exc)


def _fail(
    source: str, scope: Dict[str, Any], code: str, message: str, status_code: int = 200
) -> JSONResponse:
    """结构化失败响应。默认 200：数据源坏了不是请求错了，其它卡片要照常渲染。"""
    body = {
        "ok": False,
        "scope": scope,
        "source": source,
        "error": {"code": code, "message": message},
    }
    return JSONResponse(status_code=status_code, content=body)


def _ok(source: str, scope: Dict[str, Any], data: Any, **extra: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {"ok": True, "scope": scope, "source": source, "data": data}
    body.update(extra)
    return body


def _project(rows: List[Dict[str, Any]], fields: Tuple[str, ...]) -> List[Dict[str, Any]]:
    """只留 UI 要用的字段。缺失的键**不出现在结果里**（前端据此显示 ``—``，不填 0）。"""
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item: Dict[str, Any] = {}
        for field in fields:
            if field in row:
                item[field] = row[field]
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 布局文档校验（与旧实现等价）
# ---------------------------------------------------------------------------
def _valid_layout(layout: Any) -> bool:
    if not isinstance(layout, dict) or layout.get("version") != LAYOUT_VERSION:
        return False
    widgets = layout.get("widgets")
    if not isinstance(widgets, list) or len(widgets) > MAX_WIDGETS:
        return False
    for widget in widgets:
        if not isinstance(widget, dict) or not isinstance(widget.get("id"), str):
            return False
        if not all(isinstance(widget.get(k), int) for k in ("gx", "gy", "gw", "gh")):
            return False
    return True


# ---------------------------------------------------------------------------
# 路由：布局
# ---------------------------------------------------------------------------
_LAYOUT_SOURCE = "layout:" + LAYOUT_PLUGIN_ID + "/" + LAYOUT_FILENAME


@router.get("/overview/layout")
async def get_overview_layout(
    agent: str = Query(DEFAULT_AGENT, description="作用域 agent（布局文件与 agent 无关，仅为契约一致）")
) -> Any:
    """读看板挂件布局。文件缺失/损坏都返回 ``layout: null`` + 原因，不抛。"""
    _, scope = _resolve_agent(agent)
    scope = _scope(
        "shared_file",
        scope["agent"],
        "布局只有一个文件（" + _LAYOUT_SOURCE + "），所有 agent 共用；agent 参数不影响结果",
    )
    path = layout_path()
    info: Dict[str, Any] = {"path": str(path), "exists": False, "layout": None}
    try:
        info["exists"] = path.is_file()
        if info["exists"]:
            info["mtime"] = path.stat().st_mtime
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if _valid_layout(data):
                info["layout"] = data
                info["widget_count"] = len(data.get("widgets") or [])
            else:
                info["invalid"] = True
                info["reason"] = "文件存在但不是合法的布局文档（version/widgets 校验未过）"
    except OSError as exc:
        info["reason"] = "读不了布局文件：" + str(exc)
    except (ValueError, UnicodeDecodeError) as exc:
        info["reason"] = "布局文件不是合法 JSON：" + str(exc)
    return _ok(_LAYOUT_SOURCE, scope, info)


@router.put("/overview/layout")
async def put_overview_layout(body: Dict[str, Any]) -> Any:
    """写回布局（保留旧能力；本期概览分区不做拖拽编辑 UI）。

    body 形如 ``{"layout": {...}}``；不合法 → 400 结构化错误，绝不写坏老板已排好的文件
    （先校验、再写临时文件、最后原子替换）。
    """
    scope = _scope("shared_file", "", "布局只有一个文件，所有 agent 共用")
    layout = (body or {}).get("layout")
    if not _valid_layout(layout):
        return _fail(
            _LAYOUT_SOURCE,
            scope,
            "invalid_layout",
            "布局文档不合法：需要 version=1 且 widgets 是列表，每项含字符串 id 与整数 gx/gy/gw/gh",
            status_code=400,
        )
    path = layout_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(layout, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return _fail(_LAYOUT_SOURCE, scope, "write_failed", "写布局文件失败：" + str(exc))
    return _ok(
        _LAYOUT_SOURCE,
        scope,
        {"saved": True, "path": str(path), "widget_count": len(layout.get("widgets") or [])},
    )


# ---------------------------------------------------------------------------
# 路由：系统
# ---------------------------------------------------------------------------
_SYSTEM_SOURCE = "hermes_cli.web_routers.status.get_system_stats"

#: 前端展示哪几个数字，以及**每个数字的口径**。口径写在后端：前端只负责排版，
#: 不自己发明"这是累计/当前/近似"这类说法。
SYSTEM_CALIBERS: Dict[str, str] = {
    "memory.percent": "psutil.virtual_memory().percent —— 宿主整机内存占用率，不是 Hermes 进程内存",
    "cpu_percent": "psutil.cpu_percent(interval=0.1) —— 采样 0.1 秒的瞬时值，不是平均值",
    "uptime_seconds": "int(now - psutil.boot_time()) —— 主机开机至今，不是 Hermes 运行时长",
    "disk.percent": "psutil.disk_usage(HERMES_HOME).percent —— 只算 Hermes 主目录所在盘",
    "process.rss": "psutil.Process().memory_info().rss —— 仅本 dashboard 后端进程的常驻内存",
    "load_avg": "psutil.getloadavg()/os.getloadavg() —— 宿主 1/5/15 分钟平均负载",
    "hermes_version": "hermes_cli.version.get_version_info().base_version —— 安装版本号",
    "cpu_count": "os.cpu_count() —— 逻辑核数",
}


@router.get("/overview/system")
async def get_overview_system(
    agent: str = Query(DEFAULT_AGENT, description="作用域 agent（系统指标是宿主级，与 agent 无关）")
) -> Any:
    """宿主 / 进程指标。**与 agent 无关**（scope.kind=host），不做任何 profile 过滤。"""
    _, scope = _resolve_agent(agent)
    scope = _scope(
        "host",
        scope["agent"],
        "系统指标是宿主与本后端进程的读数，不按 agent 取数；agent 参数只回显",
    )
    try:
        func = _load("hermes_cli.web_routers.status", "get_system_stats")
        # get_system_stats 是同步函数（psutil.cpu_percent 会阻塞 ~100ms，宿主自己也是丢线程池跑），
        # 不能塞进 _await_async —— 那样拿到的是 dict，asyncio.wait_for 会抛
        # TypeError: 'dict' object can't be awaited，整张卡变成 upstream_error。
        data = await _await_thread(func)
    except Exception as exc:  # noqa: BLE001 - 任何失败都必须变成结构化错误
        logger.warning("hermes-console: /overview/system 失败：%s：%s", type(exc).__name__, exc)
        code, message = _describe_error(exc)
        return _fail(_SYSTEM_SOURCE, scope, code, message)
    return _ok(_SYSTEM_SOURCE, scope, data, calibers=SYSTEM_CALIBERS)


# ---------------------------------------------------------------------------
# 路由：用量分析
# ---------------------------------------------------------------------------
_ANALYTICS_SOURCE = "hermes_cli.web_routers.analytics.get_usage_analytics"

ANALYTICS_CALIBERS: Dict[str, str] = {
    "totals.total_input": "sessions 表 started_at 落在近 N 天内的 SUM(input_tokens)（按会话开始时间归属，不是按调用时间）",
    "totals.total_output": "同上窗口的 SUM(output_tokens)",
    "totals.total_cache_read": "同上窗口的 SUM(cache_read_tokens) —— 缓存命中的读入量，单独计，不并入 input",
    "totals.total_api_calls": "同上窗口的 SUM(api_call_count) —— API 调用次数，不是消息条数",
    "totals.total_estimated_cost": "同上窗口的 SUM(estimated_cost_usd) —— 估算值，不是账单实付",
    "totals.total_sessions": "同上窗口内开始的会话数（含子会话）",
    "period_days": "窗口长度（天）；前端可改 7/30/90",
}


@router.get("/overview/analytics")
async def get_overview_analytics(
    agent: str = Query(DEFAULT_AGENT, description="作用域 agent：透传为底层 profile 参数"),
    days: int = Query(30, description="统计窗口（天），钳到 1..365"),
) -> Any:
    """token / 调用量。**真的按 agent 取数**（底层按 profile 打开对应 state.db）。"""
    profile, scope = _resolve_agent(agent)
    if profile == AGENT_ALL:
        return _fail(
            _ANALYTICS_SOURCE,
            scope,
            "unsupported_agent",
            "用量分析只支持单个 profile，agent=all 没有等价语义；请指定一个 agent",
            status_code=400,
        )
    window = max(1, min(ANALYTICS_MAX_DAYS, int(days)))
    try:
        func = _load("hermes_cli.web_routers.analytics", "get_usage_analytics")
        # 概览只渲染 totals（token 卡）和 daily/by_model/by_task；宿主的 per-tool /
        # per-skill 两张表在真实 home 上要搬 ~78MB tool_calls 进 Python 解析（实测
        # 3.7s 里的 2s）。宿主函数支持 include_breakdown 时不计算它们；插件与宿主
        # 版本可能不同步，所以先看签名再传，老宿主照旧拿全量。
        call_kwargs: Dict[str, Any] = {"days": window, "profile": profile}
        try:
            if "include_breakdown" in inspect.signature(func).parameters:
                call_kwargs["include_breakdown"] = False
        except (TypeError, ValueError):  # 内置/无签名可读的可调用对象：按老路径走
            pass
        data = await _await_async(
            lambda: func(**call_kwargs), HEAVY_FETCH_TIMEOUT_S
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes-console: /overview/analytics 失败：%s：%s", type(exc).__name__, exc)
        code, message = _describe_error(exc, HEAVY_FETCH_TIMEOUT_S)
        return _fail(_ANALYTICS_SOURCE, scope, code, message)
    return _ok(_ANALYTICS_SOURCE, scope, data, calibers=ANALYTICS_CALIBERS)


# ---------------------------------------------------------------------------
# 路由：定时任务
# ---------------------------------------------------------------------------
_CRON_SOURCE = "hermes_cli.web_routers.cron.list_cron_jobs"

#: 概览卡片要显示的字段（旧面板拉全量，一个 job 还带 prompt/model 快照，23 个 job 体积过大）。
#: 需要全量时加 ``?full=1``。
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
    "schedule",
    "schedule_display",
    "profile",
    "is_default_profile",
    "repeat",
    "scheduler_heartbeat_age_s",
)

CRON_CALIBERS: Dict[str, str] = {
    "counts.total": "该 profile 的 cron/jobs.json 全部任务数（含已暂停、含已完成的一次性任务）",
    "counts.enabled": "enabled=true 且未暂停（paused_at 为空）的任务数",
    "counts.paused": "paused_at 非空的任务数（暂停的任务 enabled 仍可能为 true）",
    "next_run_at": "cron 调度器写入的下次执行时间；已暂停/已完成的任务可能为空",
    "last_status": "最近一次执行的终态；从未执行过时为空",
    "agent=all": "agent=all 时跨全部 profile 聚合（底层按 id 去重，default 优先）",
}


def _job_flags(job: Dict[str, Any]) -> Tuple[bool, bool]:
    """(是否启用, 是否暂停)。暂停与启用是两个正交字段，不能只看 enabled。"""
    enabled = bool(job.get("enabled"))
    paused = bool(job.get("paused_at"))
    return enabled, paused


@router.get("/overview/cron")
async def get_overview_cron(
    agent: str = Query(DEFAULT_AGENT, description="作用域 agent；也接受 all 做跨 profile 聚合"),
    full: bool = Query(False, description="true 时返回底层全量字段（含 prompt/model 快照）"),
) -> Any:
    """定时任务列表。**真的按 agent 取数**（底层用该 profile 的 cron store）。"""
    profile, scope = _resolve_agent(agent)
    try:
        func = _load("hermes_cli.web_routers.cron", "list_cron_jobs")
        jobs = await _await_async(lambda: func(profile=profile if profile is not None else DEFAULT_AGENT))
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes-console: /overview/cron 失败：%s：%s", type(exc).__name__, exc)
        code, message = _describe_error(exc)
        return _fail(_CRON_SOURCE, scope, code, message)
    if not isinstance(jobs, list):
        jobs = []
    enabled = 0
    paused = 0
    for job in jobs:
        if isinstance(job, dict):
            is_enabled, is_paused = _job_flags(job)
            if is_enabled and not is_paused:
                enabled += 1
            if is_paused:
                paused += 1
    rows = jobs if full else _project(jobs, CRON_FIELDS)
    return _ok(
        _CRON_SOURCE,
        scope,
        rows,
        counts={"total": len(jobs), "enabled": enabled, "paused": paused},
        calibers=CRON_CALIBERS,
    )


# ---------------------------------------------------------------------------
# 路由：会话
# ---------------------------------------------------------------------------
_SESSIONS_SOURCE = "hermes_cli.web_routers.sessions.get_sessions"

#: 概览列表要显示的字段（底层一行有 60+ 列，含 origin_json 这类大字段）。
SESSION_FIELDS: Tuple[str, ...] = (
    "id",
    "title",
    "display_name",
    "message_count",
    "started_at",
    "last_active",
    "ended_at",
    "profile",
    "source",
    "archived",
    "is_active",
    "model",
    "tool_call_count",
)

SESSIONS_CALIBERS: Dict[str, str] = {
    "total": "该 profile 的 state.db 里会话总数（exclude_children：不含子会话与压缩链子节点）",
    "limit/offset": "服务端分页，按 order=recent（跨压缩链的最新活动时间）排序",
    "last_active": "该会话最后一次活动的时间戳（秒），不是创建时间",
    "started_at": "会话创建时间戳（秒）",
    "message_count": "该会话已存消息条数（含工具消息）",
    "archived": "是否已归档；默认列表排除归档会话",
}


@router.get("/overview/sessions")
async def get_overview_sessions(
    agent: str = Query(DEFAULT_AGENT, description="作用域 agent：透传为底层 profile 参数"),
    limit: int = Query(20, description="每页条数，钳到 1..100"),
    offset: int = Query(0, description="偏移，>=0"),
    full: bool = Query(False, description="true 时返回底层全量字段"),
) -> Any:
    """会话列表（只读、分页）。**真的按 agent 取数**（底层按 profile 打开对应 state.db）。"""
    profile, scope = _resolve_agent(agent)
    if profile == AGENT_ALL:
        return _fail(
            _SESSIONS_SOURCE,
            scope,
            "unsupported_agent",
            "会话列表只支持单个 profile，agent=all 没有等价语义；请指定一个 agent",
            status_code=400,
        )
    page_limit = max(1, min(SESSIONS_MAX_LIMIT, int(limit)))
    page_offset = max(0, int(offset))
    try:
        func = _load("hermes_cli.web_routers.sessions", "get_sessions")
        payload = await _await_thread(
            func,
            limit=page_limit,
            offset=page_offset,
            order="recent",
            profile=profile,
            timeout_s=HEAVY_FETCH_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes-console: /overview/sessions 失败：%s：%s", type(exc).__name__, exc)
        code, message = _describe_error(exc, HEAVY_FETCH_TIMEOUT_S)
        return _fail(_SESSIONS_SOURCE, scope, code, message)
    rows = (payload or {}).get("sessions") or []
    total = (payload or {}).get("total")
    storage = (payload or {}).get("storage") or {}
    data = {
        "sessions": rows if full else _project(rows, SESSION_FIELDS),
        "total": total,
        "limit": page_limit,
        "offset": page_offset,
        # 空列表 vs 存储损坏（底层 503 家族）要能区分：损坏时 storage 非空
        "storage": storage,
    }
    return _ok(_SESSIONS_SOURCE, scope, data, calibers=SESSIONS_CALIBERS)


# ---------------------------------------------------------------------------
# 路由：网关 / 版本（宿主级读数）
# ---------------------------------------------------------------------------
_STATUS_SOURCE = "hermes_cli.web_routers.status.get_status"

#: 挂件只要这几个字段。底层 ``/api/status`` 还带 config 路径、auth 门形状、
#: 每个平台的状态对象（体积大且含运维细节），概览不展示就不搬。
STATUS_FIELDS: Tuple[str, ...] = (
    "gateway_running",
    "gateway_pid",
    "gateway_state",
    "gateway_updated_at",
    "gateway_exit_reason",
    "gateway_health_url",
    "active_sessions",
    "version",
    "release_date",
    "hermes_home",
    "config_version",
    "auth_required",
)

STATUS_CALIBERS: Dict[str, str] = {
    "gateway_running": "宿主 status 的网关存活判定（pid 活着 + 健康探测），不是本插件后端自己的存活",
    "gateway_pid": "网关进程 pid；网关没起来时为空",
    "gateway_state": "网关状态机当前状态（running/stopped/…），由网关自己写入",
    "gateway_updated_at": "网关状态文件最后一次被写入的时间 —— 心跳时效看这个，不是开机时间",
    "active_sessions": "宿主统计的当前活跃会话数（与 /overview/sessions 的总数是两个口径：那个是库里总数）",
    "version": "hermes_cli.version 的安装版本号",
    "platforms": "各消息平台的状态行（state/updated_at/error_message），来自宿主 status",
    "agent": "网关是宿主单实例，与 agent 无关；agent 参数只回显",
}


def _platform_rows(raw: Any) -> List[Dict[str, Any]]:
    """``gateway_platforms`` → 精简行。缺字段就不写（前端显示 ``—``）。"""
    rows: List[Dict[str, Any]] = []
    if not isinstance(raw, dict):
        return rows
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        row: Dict[str, Any] = {"name": str(name)}
        state = entry.get("state")
        if isinstance(state, str) and state:
            row["state"] = state
        updated = entry.get("updated_at")
        if isinstance(updated, str) and updated:
            row["updated_at"] = updated
        err = entry.get("error_message") or entry.get("error_code")
        if isinstance(err, str) and err:
            row["error"] = err[:200]
        rows.append(row)
    # 有错的排前面：概览里先看到坏的那条
    rows.sort(key=lambda r: (0 if r.get("error") else 1, r.get("name") or ""))
    return rows


@router.get("/overview/status")
async def get_overview_status(
    agent: str = Query(DEFAULT_AGENT, description="作用域 agent（网关是宿主级，与 agent 无关）")
) -> Any:
    """网关 / 版本 / 活跃会话。**宿主级读数**（scope.kind=host），不按 agent 取数。

    这是「gateway」「heartbeat」两个挂件的真数据源：旧 Home 面板走宿主
    ``api.getStatus()``，插件后端拿不到那个 SDK 面，所以在这里把同一个宿主函数
    （``hermes_cli.web_routers.status.get_status``）在进程内 await 一次，字段与口径
    都由本层给，前端不猜。
    """
    _, scope = _resolve_agent(agent)
    scope = _scope(
        "host",
        scope["agent"],
        "网关是宿主单实例读数（进程 pid / 状态机 / 心跳时间），不按 agent 取数；agent 参数只回显",
    )
    try:
        func = _load("hermes_cli.web_routers.status", "get_status")
        # get_status 是 async（内部走 run_in_threadpool + 健康探测），设计上支持
        # 在进程内 await（request=None），别当同步函数丢线程池。
        payload = await _await_async(lambda: func(), HEAVY_FETCH_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes-console: /overview/status 失败：%s：%s", type(exc).__name__, exc)
        code, message = _describe_error(exc, HEAVY_FETCH_TIMEOUT_S)
        return _fail(_STATUS_SOURCE, scope, code, message)
    if not isinstance(payload, dict):
        return _fail(_STATUS_SOURCE, scope, "upstream_error", "宿主 status 返回的不是对象")
    data = _project([payload], STATUS_FIELDS)
    row = data[0] if data else {}
    row["platforms"] = _platform_rows(payload.get("gateway_platforms"))
    return _ok(_STATUS_SOURCE, scope, row, calibers=STATUS_CALIBERS)


# ---------------------------------------------------------------------------
# 路由：日志尾窗口（errors 挂件的真数据）
# ---------------------------------------------------------------------------
_LOGS_SOURCE = "hermes_cli.web_routers.status.get_logs"

#: 允许读的日志（与宿主 ``hermes_cli.logs.LOG_FILES`` 的白名单一致；update/handoff 是
#: 更新流程专用，概览不展示，也不给白名单外的路径 —— 这里不是文件读取接口）。
LOG_FILES_ALLOWED: Tuple[str, ...] = ("agent", "errors", "gateway", "gui", "desktop")

#: 宿主日志行形如 ``2026-10-29 20:31:07,123 ERROR logger.name: …``。**与宿主
#: ``hermes_cli.logs._LEVEL_RE`` 同形**：优先用它（同一份解析，不另立一套），
#: 宿主改版把它挪走时退回本文件的同形正则，而不是让挂件变成错误态。
try:  # pragma: no cover - 宿主在时走这一支
    from hermes_cli.logs import _LEVEL_RE as _HOST_LEVEL_RE  # type: ignore
except Exception:  # noqa: BLE001
    _HOST_LEVEL_RE = None

_LEVEL_RE = _HOST_LEVEL_RE or re.compile(r"\s(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s")

#: 计数口径：一条日志记录一行；traceback 续行没有级别戳，单独计入 ``unlabeled``，
#: 不并进 ERROR（那会把一条异常的堆栈算成十几个错误）。
LOG_LEVELS: Tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

LOGS_CALIBERS: Dict[str, str] = {
    "counts": "尾窗口 N 行内按级别计数（不是全量历史、不是按天的错误率）",
    "unlabeled": "没有级别戳的行（traceback 续行等），单独计，不并进 ERROR",
    "scanned": "实际读到的尾窗口行数（请求 N 行，文件更短时就是文件行数）",
    "matched": "按 level 过滤后返回的行数；level=ALL 时与 scanned 相同",
    "exists": "只表示日志文件在不在（真实文件存在性）。文件在但窗口 0 行 ≠ 没有文件",
    "lines": "宿主 hermes_cli.logs._read_tail 的原始行，已截断到每条 300 字符",
    "level": "服务端预过滤级别（min_level）；ALL/空 = 不过滤",
    "file": "宿主日志名（agent/errors/gateway/gui/desktop），对应 <home>/logs/<name>.log",
}


def _log_path_for(name: str, profile: Optional[str]) -> Optional[Path]:
    """日志文件路径（在目标 profile 的作用域里解析）。

    为什么不能直接 ``log_file_path(name)``：它按 ``get_hermes_home()`` 解析，而那个值是
    **本进程**的 profile。宿主 ``get_logs`` 支持 ``profile=``，读的是目标 profile 的
    ``logs/``；路径解析必须跟着同一个作用域走，否则"文件在哪"和"读到几行"会来自两份文件。

    ``_config_profile_scope`` 是宿主自己的私有辅助（``status.py`` 用的是同一个），取不到
    就退回进程作用域 —— 路径只是附加信息，不能因为它让整块日志读数失败。
    """
    from hermes_cli.logs import log_file_path  # type: ignore

    if not profile:
        return log_file_path(name)
    try:
        from hermes_cli.web_server_profiles import _config_profile_scope  # type: ignore

        with _config_profile_scope(profile):
            return log_file_path(name)
    except Exception:  # noqa: BLE001
        return log_file_path(name)


@router.get("/overview/logs")
async def get_overview_logs(
    agent: str = Query(DEFAULT_AGENT, description="作用域 agent：透传为宿主 get_logs 的 profile 参数"),
    file: str = Query("agent", description="日志名：agent / errors / gateway / gui / desktop"),
    lines: int = Query(50, description="尾窗口行数，钳到 1..500"),
    level: str = Query("ERROR", description="最低级别（DEBUG/INFO/WARNING/ERROR/CRITICAL）；ALL = 不过滤"),
) -> Any:
    """日志尾窗口 + 按级别计数。**按 agent 的 profile 取数**（每个 profile 有自己的一份 logs/）。

    为什么概览要这块：旧 Home 的「Logs」挂件只显示 ERROR 行数，看不出"最近有没有出事"。
    这里给三样东西 —— 计数、窗口行数、原始尾行 —— 前端据此说"窗口内有 N 条 ERROR，
    最近一条是 …"，而不是一个孤零零的数字。

    **2026-10-30 修**：以前这条路由把 ``agent`` 参数收下就扔了（scope 写 ``host``），
    实际读的是**本后端进程自己那个 profile** 的 ``logs/``。界面上「日志错误」挂件于是
    永远显示后端进程那一份，切 agent 也不变 —— 而且没在任何地方说明。
    宿主 ``get_logs`` 本来就支持 ``profile=``，这里透传；``scope.kind`` 相应改成
    ``profile``，让前端那行作用域说明说的是真话。
    """
    profile, scope = _resolve_agent(agent)
    if profile == AGENT_ALL:
        return _fail(
            _LOGS_SOURCE,
            scope,
            "unsupported_agent",
            "日志只支持单个 profile，agent=all 没有等价语义；请指定一个 agent",
            status_code=400,
        )
    if profile is not None:
        scope = _scope(
            "profile",
            scope["agent"],
            "按 agent=" + str(profile) + " 的 profile 读它自己的 logs/ 目录（每个 profile 一份日志）",
        )
    else:
        scope = _scope(
            "current_profile",
            scope["agent"],
            "作用域未定（前端没传 agent），读本后端进程自己那个 profile 的 logs/",
        )
    name = (file or "").strip().lower()
    if name not in LOG_FILES_ALLOWED:
        return _fail(
            _LOGS_SOURCE,
            scope,
            "invalid_argument",
            "不支持的日志名：" + str(file) + "；可选 " + " / ".join(LOG_FILES_ALLOWED),
            status_code=400,
        )
    want = max(1, min(500, int(lines)))
    want_level = (level or "").strip().upper()
    if want_level in ("", "ALL"):
        want_level = ""
    elif want_level not in LOG_LEVELS:
        return _fail(
            _LOGS_SOURCE,
            scope,
            "invalid_argument",
            "不支持的级别：" + str(level) + "；可选 " + " / ".join(LOG_LEVELS) + " / ALL",
            status_code=400,
        )
    try:
        func = _load("hermes_cli.web_routers.status", "get_logs")
        payload = await _await_async(
            lambda: func(file=name, lines=want, level=want_level or None, profile=profile),
            FETCH_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes-console: /overview/logs 失败：%s：%s", type(exc).__name__, exc)
        code, message = _describe_error(exc)
        return _fail(_LOGS_SOURCE, scope, code, message)

    raw_lines = payload.get("lines") if isinstance(payload, dict) else None
    rows = [str(line)[:300] for line in (raw_lines or []) if isinstance(line, str)]
    counts: Dict[str, int] = {level_name: 0 for level_name in LOG_LEVELS}
    counts["unlabeled"] = 0
    for line in rows:
        match = _LEVEL_RE.search(line)
        if match:
            counts[match.group(1)] = counts.get(match.group(1), 0) + 1
        else:
            counts["unlabeled"] += 1

    info: Dict[str, Any] = {
        "file": name,
        "level": want_level or "ALL",
        "lines": rows,
        "counts": counts,
        "scanned": len(rows),
        # `matched` = 按级别过滤后真正返回的行数；`window` = 请求的尾窗口行数。
        # 这两个数 + `exists` 才能让前端说清三种完全不同的情况：
        #   没文件 / 文件空 / 文件有内容但该级别 0 条。
        "matched": len(rows),
        "window": want,
        # `exists` 只表示**文件在不在**。旧实现写的是 `bool(rows)` —— 文件明明在、
        # 只是尾窗口 0 行（例如按 ERROR 过滤后一条 ERROR 都没有）时会被报成"没有文件可读"，
        # 前端据此显示"不是 0 条错误，是没有文件可读"，那是假话（这正是本卡要修的缺数据误报）。
        "exists": False,
    }
    # 路径与 mtime 只用于"文件在哪、多久没写了"，读不到就整条不写（前端显示 —）。
    # 必须在**同一个 profile 作用域**里解析，否则路径会指到本进程那份日志，
    # 和上面读到的行数对不上（"说得出数据来自哪个文件"这句话就变成假的）。
    try:
        from hermes_cli.logs import log_file_path  # type: ignore

        path = _log_path_for(name, profile)
        if path is not None:
            info["path"] = str(path)
            if path.is_file():
                info["exists"] = True
                info["mtime"] = path.stat().st_mtime
                info["size"] = path.stat().st_size
    except Exception:  # noqa: BLE001 - 路径只是附加信息，取不到不影响计数
        pass
    # 兜底：真的读到了行，就说明文件存在（哪怕 log_file_path 解析不出来）。
    if rows:
        info["exists"] = True
    return _ok(_LOGS_SOURCE, scope, info, calibers=LOGS_CALIBERS)


# ---------------------------------------------------------------------------
# 路由：分区自检
# ---------------------------------------------------------------------------
_SUBROUTES = (
    ("layout", "GET", "/overview/layout"),
    ("layout", "PUT", "/overview/layout"),
    ("system", "GET", "/overview/system"),
    ("analytics", "GET", "/overview/analytics"),
    ("cron", "GET", "/overview/cron"),
    ("sessions", "GET", "/overview/sessions"),
    ("status", "GET", "/overview/status"),
    ("logs", "GET", "/overview/logs"),
)

#: 每个子路由真正依赖的宿主接口（探针据此判断"接上了没有"）。
_SUBROUTE_DEPS: Dict[str, Tuple[str, str]] = {
    "system": ("hermes_cli.web_routers.status", "get_system_stats"),
    "analytics": ("hermes_cli.web_routers.analytics", "get_usage_analytics"),
    "cron": ("hermes_cli.web_routers.cron", "list_cron_jobs"),
    "sessions": ("hermes_cli.web_routers.sessions", "get_sessions"),
    "status": ("hermes_cli.web_routers.status", "get_status"),
    "logs": ("hermes_cli.web_routers.status", "get_logs"),
}


@router.get("/overview")
async def get_overview_selfcheck(
    agent: str = Query(DEFAULT_AGENT, description="作用域 agent")
) -> Any:
    """分区自检：列出 6 个子路由 + 每个宿主接口是否可解析 + 布局文件位置。

    只做 import 级探测（不真的取数），所以很便宜。概览底部那块「分区自检」用的就是
    这个响应 —— 界面上直接显示"哪个数据源接没接上、坏在哪个 import"，不靠一张
    `ok=true` 的探针报告替界面圆场。
    """
    _, scope = _resolve_agent(agent)
    deps: Dict[str, Any] = {}
    for name, (module_name, attr) in _SUBROUTE_DEPS.items():
        try:
            _load(module_name, attr)
            deps[name] = {"ok": True, "target": module_name + "." + attr}
        except Exception as exc:  # noqa: BLE001
            code, message = _describe_error(exc)
            deps[name] = {
                "ok": False,
                "target": module_name + "." + attr,
                "error": {"code": code, "message": message},
            }
    path = layout_path()
    try:
        exists = path.is_file()
    except OSError:
        exists = False
    return {
        "ok": True,
        "scope": scope,
        "source": "api_overview",
        "data": {
            "routes": [{"name": n, "method": m, "path": p} for n, m, p in _SUBROUTES],
            "deps": deps,
            "layout": {"path": str(path), "exists": exists},
            "ts": time.time(),
        },
    }

# -*- coding: utf-8 -*-
"""hermes-console 插件后端聚合入口（只做聚合，不实现任何域）。

挂载契约：dashboard 只认 ``manifest.json`` 的 ``api`` 键所指的**这一个**文件
（``hermes_cli/web_server_dashboard._mount_one_plugin_api``：``spec_from_file_location(
"hermes_dashboard_plugin_<name>", api_path)``），挂在 ``/api/plugins/hermes-console/`` 下。
同目录其它 ``.py`` 不会被自动加载，所以本文件负责：

1. 把本目录插进 ``sys.path``（子模块之间、以及将来的域模块互相 import 都靠它）；
2. import 四个域模块；
3. ``include_router`` 把它们的路由并进本文件的 ``router``。

路由清单（当前为占位，由后续卡片逐域替换）：

============== ================================== ==========================
模块            路由                               状态
============== ================================== ==========================
api_agents     ``GET /agents``                    已实现（动态枚举 9 agent）
api_agents_admin ``/agents/overview`` + 下钻两路由  已实现（C7：全 agent 总览，逐行失败隔离）
api_overview   ``/overview/*``（6 个 + 自检）      已实现（C3：搬 home-dashboard）
api_weather    ``/weather/*``（4 个 + 自检）       已实现（C4：搬 weather-card）
api_upgrade    ``/upgrade/*``（6 个）             已实现（V2：scan/readiness/prepare/restore/wait/kill）
api_advisor    ``/advisor/*``（7 个 + 自检）      已实现（C9b：Advisor 审核台，消费 advisor/audit.py；
                                                    coverage 覆盖矩阵 / settings 按 profile 读写）
api_vcc        ``/vcc/*``（6 个）                 已实现（V3a：上下文引擎，引擎按 agent 解析）
api_remote     ``/remote/*``（5 个）               已实现（V4：远端后端，agent 列表动态枚举 + 凭据脱敏）
core.registry  ``GET /modules``                  已实现（M1b：模块注册核心，与上面的域聚合并行）
============== ================================== ==========================

不要在本文件里写业务逻辑：40 个路由挤一个文件就无法审阅了。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional

from fastapi import APIRouter  # type: ignore
from fastapi.responses import JSONResponse  # type: ignore

_DASHBOARD_DIR = Path(__file__).resolve().parent
if str(_DASHBOARD_DIR) not in sys.path:
    # 必须在 import 子模块之前完成：本文件被 dashboard 用任意合成模块名 exec，
    # 没有任何包上下文，兄弟模块只能靠 sys.path 找到。
    sys.path.insert(0, str(_DASHBOARD_DIR))

#: 参与聚合的域模块（顺序 = 路由注册顺序，仅影响文档展示）。
DOMAIN_MODULES = ("api_overview", "api_weather", "api_upgrade", "api_agents", "api_agents_admin",
                  "api_autogroup", "api_autogroup_panel",
                  "api_autogroup_projects",
                  "api_advisor", "api_console_settings", "api_autotitler", "api_vcc", "api_remote",
                  "api_models", "api_lanes",
                  # 热重载：POST /reload（就地重挂载本插件，不重启 serve）；见 api_reload.py
                  "api_reload")


def _import_domain(modname: str) -> ModuleType:
    """import 同目录的域模块。

    同名模块可能已被别的插件（同样是 ``api_*.py`` 命名）以裸名放进 ``sys.modules``；
    那种情况下直接 ``import`` 会拿到别人家的模块，路由就挂错了。发现文件路径不匹配时
    先摘掉再按本目录重新导入。
    """
    existing = sys.modules.get(modname)
    if existing is not None:
        found_path = getattr(existing, "__file__", None)
        same_dir = False
        if found_path:
            try:
                same_dir = Path(found_path).resolve().parent == _DASHBOARD_DIR
            except OSError:
                same_dir = False
        if not same_dir:
            del sys.modules[modname]
    return importlib.import_module(modname)


router = APIRouter()

#: 加载成功的域模块（顺序即注册顺序）。
DOMAIN_LOADED: list[str] = []
#: 加载失败的域模块 -> 原因。
#:
#: **一个域坏掉不许拖垮整个插件。** 桌面端后端只在启动时 import 本文件一次，
#: 任一子模块半截 / 语法错 / 缺依赖都会让整个 ``router`` 构造失败，
#: 于是全部路由一起消失、面板每个分区同时报错（排查时极难定位到是哪个域）。
#: 所以逐个 try：坏的那个只丢自己的路由，并通过 ``GET /health`` 暴露原因。
DOMAIN_FAILURES: dict[str, str] = {}

for _name in DOMAIN_MODULES:
    try:
        _mod = _import_domain(_name)
        router.include_router(_mod.router)
        DOMAIN_LOADED.append(_name)
    except Exception as _exc:  # noqa: BLE001 - 故意兜住所有异常，包括 SyntaxError
        DOMAIN_FAILURES[_name] = f"{type(_exc).__name__}: {_exc}"
        # 半截模块会留在 sys.modules 里，下一次导入会拿到坏的那个，先摘掉。
        sys.modules.pop(_name, None)


@router.get("/health")
def domain_health() -> dict:
    """自检：哪些域模块挂上了、哪些没挂以及原因。

    排查顺序永远是先看这里：``failed`` 里点名的那个模块才是真凶，
    其余分区的异常都只是它的连坐。
    """
    return {
        "ok": not DOMAIN_FAILURES,
        "loaded": DOMAIN_LOADED,
        "failed": DOMAIN_FAILURES,
        "route_count": len(router.routes),
    }


# ---------------------------------------------------------------------------
# M1b：模块注册核心（新结构，与上面的 DOMAIN_MODULES 旧聚合**并行**验证）
#
# 旧结构一行不改：``DOMAIN_MODULES`` / ``DOMAIN_LOADED`` / ``DOMAIN_FAILURES`` /
# ``GET /health`` 全部保持原样。这里只**新增**一个只读端点 ``GET /modules``，
# 数据来自 ``dashboard/core/registry.py``（扫描 ``dashboard/modules/*/manifest.json``）。
#
# 两条硬要求：
# 1. 注册核心 import 失败 / 算不出来 ⇒ 503 结构化错误，**不许 500**，也不许拖垮旧聚合；
# 2. 非法 agent ⇒ 400 + 合法 id 列表（不是 500，不是空列表）。
# ---------------------------------------------------------------------------
def _import_core_registry() -> ModuleType:
    """import ``core.registry``（带"同名别家模块"防护，理由同 :func:`_import_domain`）。"""
    for name in ("core.registry", "core"):
        existing = sys.modules.get(name)
        if existing is None:
            continue
        found_path = getattr(existing, "__file__", None)
        same_dir = False
        if found_path:
            try:
                same_dir = _DASHBOARD_DIR in Path(found_path).resolve().parents
            except OSError:
                same_dir = False
        if not same_dir:
            del sys.modules[name]
    return importlib.import_module("core.registry")


try:
    _REGISTRY = _import_core_registry()
    REGISTRY_ERROR = ""
except Exception as _exc:  # noqa: BLE001 - 注册核心坏掉只影响 /modules，不影响其它路由
    _REGISTRY = None  # type: ignore[assignment]
    REGISTRY_ERROR = "%s: %s" % (type(_exc).__name__, _exc)


def _fail(status_code: int, code: str, message: str, **extra: Any) -> JSONResponse:
    """统一失败体（与 ``api_advisor`` / ``api_overview`` 同款）：``{ok:false, error:{code,message}}``。"""
    error: Dict[str, Any] = {"code": code, "message": message}
    error.update({key: value for key, value in extra.items() if key not in ("code", "message")})
    return JSONResponse(status_code=status_code, content={"ok": False, "error": error})


@router.get("/modules")
def list_modules(agent: Optional[str] = None) -> Any:
    """某 agent 该看到哪些模块（含未启用的 + 原因），以及模块加载健康状态。

    ``GET /modules?agent=default`` →
    ``{"ok": true, "data": {"agent": "default", "modules": [{id,label,nav_order,section,
    enabled,reason,...}], "health": {"loaded": [...], "failed": {...}}}}``

    * 缺省 ``agent`` ⇒ ``default``（不是"进程碰巧是谁"，同一份 URL 在任何进程都返回同一份数据）
    * 非法 ``agent``（例如 ``../../etc``）⇒ **400** + 合法 id 列表
    * **不存在**但文法合法的 ``agent``（例如 ``nosuchagent42``）⇒ **404** ``agent_not_found``，
      **不回退成 default 的模块清单**。此前只校验文法不校验存在性，假 agent 会静默拿到
      default 的完整模块清单（200），前端等于被骗成「这个 agent 存在且模块全开」。
    * 注册核心不可用 ⇒ 503 结构化错误（其它路由不受影响）
    """
    if _REGISTRY is None:
        return _fail(
            503,
            "upstream_error",
            "模块注册核心不可用（dashboard/core/registry.py 加载失败）：" + REGISTRY_ERROR,
        )
    try:
        agent_id = _REGISTRY.normalize_agent_id(agent)
    except ValueError as exc:
        return _fail(400, "invalid_argument", str(exc), agents=_REGISTRY.legal_agent_ids())
    legal: List[str] = list(_REGISTRY.legal_agent_ids())
    if not _REGISTRY.agent_exists(agent_id):
        # 同时给两种契约形态：HTTP 404（资源不存在）+ 结构化 agent_resolved/reason，
        # 调用方按任一形态判定都成立；**唯一不许发生的是返回默认清单**。
        return _fail(
            404,
            "agent_not_found",
            "没有这个 agent：%r —— 不会回退成 default 的模块清单。当前存在的 id：%s"
            % (agent_id, ", ".join(legal) or "<none>"),
            agents=legal,
            agent=agent_id,
            agent_resolved=False,
            reason="agent_not_found",
        )
    try:
        data = _REGISTRY.describe(agent_id)
    except Exception as exc:  # noqa: BLE001 - 扫描/合并异常不许变成 500 栈
        return _fail(503, "upstream_error", "计算模块启用集失败：%s: %s" % (type(exc).__name__, exc))
    return {"ok": True, "data": data}


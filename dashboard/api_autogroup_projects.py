# -*- coding: utf-8 -*-
"""hermes-console · 项目实体 / 一致性自检 / 对齐修复 HTTP 层（t_53b48743）。

业务逻辑全在 ``autogroup_projects.py``（实体读）、``autogroup_align.py``（自检与对齐）、
``autogroup_project_ops.py``（新建/重命名/改目录/解散/删除）。本文件只做三件事：

1. **参数校验 + 错误码映射**（``OpError.code`` → HTTP 状态码）；
2. **``confirm: true`` 硬门**：所有写接口缺 ``confirm`` 一律 400 ``confirmation_required``
   **并把预览体一起返回**（前端不用再跑一次预览）；
3. 统一响应形状 ``{ok, data, notes}`` / ``{ok:false, error:{code,message,...}}``。

路由（前缀 ``/api/plugins/hermes-console``）：

================================================  ==========================
``GET  /autogroup/entities``                      项目实体清单（权威视图）
``GET  /autogroup/entities/{slug}``               单个项目实体
``GET  /autogroup/consistency``                   一致性自检（只读）
``GET  /autogroup/consistency/pairs``             按 (项目, cwd目录) 聚合的自检
``GET  /autogroup/align/preview``                 对齐预览（只读，mode=fix_cwd|fix_slug）
``POST /autogroup/align``                         执行对齐（需 confirm）
``POST /autogroup/project/create``                新建项目（需 confirm）
``PATCH /autogroup/project/name``                 改显示名（需 confirm）
``POST /autogroup/project/slug``                  改 slug（需 confirm + confirm_slug）
``POST /autogroup/project/path``                  改目录（需 confirm）
``POST /autogroup/project/dissolve``              解散（需 confirm + to）
``POST /autogroup/project/delete``                删除实体（需 confirm）
``GET  /autogroup/project/attrs/backfill``         存量项目缺属性行预览（只读）
``POST /autogroup/project/attrs/backfill``         补属性行（需 confirm，created_by=imported）
================================================  ==========================

与旧路由的关系：``POST/PATCH/DELETE /autogroup/projects`` 是**老接口**，保持可用（面板在用），
本文件的新路由是「真项目化」的完整契约。两边写的是同一份 projects.db，台账也是同一张
``console_ops``，所以撤销路径只有一条。

**agent 维度（t_9ad02075）**：本文件所有路由都吃 ``agent``（query 或 body）。空/``default`` ⇒ 根库；
``<p>`` ⇒ ``profiles/<p>/projects.db``；该 agent 没有库就是没有库（**不建库、不回落根库**）；
``__all__`` 读接口回「请先选定一个 agent」（**不合并跨 agent 清单**）、写接口 400 ``agent_required``。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Query  # type: ignore
from fastapi.responses import JSONResponse  # type: ignore

_DASHBOARD_DIR = Path(__file__).resolve().parent
if str(_DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD_DIR))

import api_autogroup as base  # noqa: E402
import autogroup_align as align  # noqa: E402
import autogroup_project_ops as ops  # noqa: E402
import autogroup_projects as ent  # noqa: E402

router = APIRouter()


def _ok(data: Dict[str, Any], notes: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"ok": True, "data": data, "notes": notes or []}


def _err(exc: ops.OpError) -> JSONResponse:
    content: Dict[str, Any] = {"ok": False,
                               "error": {"code": exc.code, "message": str(exc)}}
    if exc.extra:
        content["error"].update(exc.extra)
        content.update({k: v for k, v in exc.extra.items() if k not in ("code", "message")})
    return JSONResponse(status_code=exc.status, content=content)


def _need_confirm(payload: Dict[str, Any]) -> Optional[JSONResponse]:
    """``confirm is True`` 是唯一通过条件；字符串 "true" 不算。"""
    if payload.get("confirm") is True:
        return None
    return base._fail("confirmation_required",
                      "写操作需要 confirm=true（本请求未写任何东西）", extra={})


def _flag(payload: Dict[str, Any], key: str) -> bool:
    return payload.get(key) is True


# ---------------------------------------------------------------------------
# agent 作用域（t_9ad02075）
#
# 这些路由以前一律 ``home = base._home()``（天生吃根 home）⇒ ``agent=work`` 的面板拿小白的
# 17 个项目去配 work 的会话。现在统一走 agent 解析：
#
#   · 读路由：``agent`` 空/default ⇒ 根库；``agent=<p>`` ⇒ ``profiles/<p>/projects.db``；
#     该 agent 没有库/没有项目 ⇒ 明确空态，**绝不回落根库**；
#     ``agent=__all__`` ⇒ 200 + ``needs_agent``（只报「请先选定一个 agent」，不出跨 agent 清单）。
#   · 写路由：``agent=__all__`` ⇒ 400 ``agent_required``；没有库 ⇒ 409 ``no_project_db``。
#   · ``agent`` 支持 query 与 body 两种传法（body 里的优先），前端改传时不必换签名。
# ---------------------------------------------------------------------------

def _agent_of(agent: Any, payload: Optional[Dict[str, Any]] = None) -> str:
    """取 agent 名：**body 优先**，其次 query。

    直接调路由函数时（测试/进程内调用）``agent`` 会是 FastAPI 的 ``Query`` 对象而不是字符串，
    这种「没解析过的形参」一律当**未指定**（⇒ default/根库），绝不把它 str 成路径。
    """
    raw = (payload or {}).get("agent")
    if raw is None or raw == "":
        raw = agent if isinstance(agent, str) else ""
    return str(raw or "").strip()


def _write_scope(agent: str) -> Any:
    """写路由作用域：返回 ``(agent, home)``；``__all__`` 直接给 400 ``agent_required``。"""
    try:
        return base._scope(agent)
    except base.ScopedWrite as exc:
        return base._fail(exc.code, exc.message,
                          extra={"agent": str(agent or ""), "needs_agent": True})


def _no_db(agent_name: str, home: Path, exc: BaseException) -> JSONResponse:
    """该 agent 没有 ``projects.db`` ⇒ 明确报错（**不建库、不回落根库**）。

    只在**真正写库**那一步触发（``ensure_attrs`` 抛 ``FileNotFoundError``）——预览与
    ``confirm`` 硬门照旧先走，所以「缺 confirm 回 400 + 预览体」的既有契约不受影响。
    """
    path = ent.projects_db(home)
    return base._fail("no_project_db",
                      "该 agent（%s）还没有项目库：%s 不存在（%s）。本接口不会替你造库，"
                      "也不会回落到根库（别的 agent 的项目不归它）"
                      % (agent_name, path, base._describe(exc)),
                      status_code=409,
                      extra={"agent": agent_name, "projects_db": str(path), "needs_db": True})


# ---------------------------------------------------------------------------
# 读：实体清单
# ---------------------------------------------------------------------------

@router.get("/autogroup/entities")
def get_entities(include_dissolved: bool = Query(default=True),
                 agent: str = Query(default="", description=(
                     "按哪个 agent 取项目（空/default=根库；<p>=profiles/<p>；"
                     "__all__=不合并，只回「请先选定一个 agent」）"))) -> Any:
    """项目实体清单。每个项目回答：有没有目录 / 目录里有没有东西 / 多少会话。

    **按 agent 取**（t_9ad02075）：``agent=work`` 只出 work 自己的项目库；该 agent 没有库就报
    空态，**不回落根库**。``agent=__all__`` 只回 ``needs_agent=true``，不把多个 agent 的项目
    拼成一份清单。

    ``path_matches_slug=false`` 或 ``dir.exists=false`` 会额外进 ``notes``：目录口径与 slug
    不吻合、或权威目录根本不在，正是「面板文案与实际目录对不上」那个坑。
    """
    home = base._home()
    try:
        payload = ent.list_entities_for(home, _agent_of(agent))
    except Exception as exc:  # noqa: BLE001
        return base._fail("entities_failed", "读项目实体失败：" + base._describe(exc),
                          status_code=500)
    items = payload.get("projects") or []
    if not include_dissolved:
        items = [i for i in items if i.get("status") != "dissolved"]
        payload["projects"] = items
        payload["total"] = len(items)
    notes: List[str] = []
    if payload.get("message"):
        notes.append(payload["message"])
    if payload.get("needs_agent"):
        return _ok(payload, notes)
    missing = [i["slug"] for i in items if not (i["dir"] or {}).get("exists")]
    odd = [i["slug"] for i in items if i.get("path_basename") and not i.get("path_matches_slug")]
    if missing:
        notes.append("这些项目的权威目录不存在：%s" % "、".join(missing))
    if odd:
        notes.append("这些项目的目录名与 slug 不一致（path 仍是权威）：%s" % "、".join(odd))
    if not items and not payload.get("message"):
        notes.append("projects.db 里没有项目（或文件不存在）")
    no_attrs = [i["slug"] for i in items if not i.get("attrs_present")]
    if no_attrs:
        notes.append("%d 个项目还没有扩展属性行（created_by/status 为 null）：任意写操作会自动补" % len(no_attrs))
    return _ok(payload, notes)


@router.get("/autogroup/entities/{slug}")
def get_entity(slug: str,
               agent: str = Query(default="", description="按哪个 agent 取（空/default=根库）")) -> Any:
    name = _agent_of(agent)
    try:
        payload = ent.list_entities_for(base._home(), name)
    except Exception as exc:  # noqa: BLE001
        return base._fail("entities_failed", "读项目实体失败：" + base._describe(exc),
                          status_code=500)
    if payload.get("needs_agent"):
        return base._fail("agent_required",
                          "按 slug 取单个项目必须先选定一个 agent（agent=__all__ 没有单一项目库）",
                          extra={"needs_agent": True})
    item = None
    for row in payload.get("projects") or []:
        if row.get("slug") == slug:
            item = row
            break
    if item is None:
        return base._fail("unknown_project", "没有这个项目：%s（agent=%s）" % (slug, payload.get("agent")),
                          status_code=404, extra={"agent": payload.get("agent"),
                                                  "empty_reason": payload.get("empty_reason")})
    return _ok({"project": item, "agent": payload.get("agent")})


# ---------------------------------------------------------------------------
# t_ac537221 收敛：以下路由**已删除**（无 UI 入口、能力已被主流程覆盖）
#
#   GET  /autogroup/consistency        —— 无 UI 入口；结论已固化成 apply 的看板闸门
#   GET  /autogroup/consistency/pairs  —— 同上（聚合视图，仅探针用过）
#   GET  /autogroup/align/preview      —— cwd 对齐的预览；能力已被
#   POST /autogroup/align                 ``POST /autogroup/apply {sync_cwd:true}`` 覆盖
#                                         （apply 自己就带预览 + 回读 + 可撤销）
#
# ``autogroup_align.py`` 模块本身**保留**：``GET /autogroup/entities`` 用它判目录状态。
# 历史 ``align.fix_cwd`` 台账仍可通过 ``POST /autogroup/undo {op_id}`` 撤销。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 项目操作：新建 / 改名 / 改 slug / 改目录 / 解散 / 删除
# ---------------------------------------------------------------------------

def _guard(payload: Dict[str, Any], *, extra_flag: Optional[str] = None) -> Optional[JSONResponse]:
    blocked = _need_confirm(payload)
    if blocked is not None:
        return blocked
    if extra_flag and not _flag(payload, extra_flag):
        return base._fail("confirmation_required",
                          "该操作需要额外确认 %s=true" % extra_flag)
    return None


@router.post("/autogroup/project/create")
def post_project_create(payload: Dict[str, Any] = Body(...),
                        agent: str = Query(default="", description="在哪个 agent 下建项目（必填单一 agent）")) -> Any:
    scope = _write_scope(_agent_of(agent, payload))
    if isinstance(scope, JSONResponse):
        return scope
    agent_name, home = scope
    slug = str(payload.get("slug") or "").strip()
    name = str(payload.get("name") or "").strip()
    path = payload.get("path") or None
    plan = ops.preview_create(home, slug=slug, name=name, path=path)
    blocked = _need_confirm(payload)
    if blocked is not None:
        return base._fail("confirmation_required",
                          "新建项目需要 confirm=true（未写任何东西）",
                          extra={"preview": plan})
    if plan["problems"]:
        return base._fail("invalid_project", "；".join(plan["problems"]), extra={"preview": plan})
    try:
        result = ops.create(home, slug=slug, name=name, path=path,
                            created_by=str(payload.get("created_by") or "manual"),
                            allow_existing_dir=_flag(payload, "allow_existing_dir"),
                            note=payload.get("note"), sort_order=payload.get("sort_order"),
                            color=payload.get("color"))
    except FileNotFoundError as exc:      # 该 agent 没有库：明确报，不建库、不回落根库
        return _no_db(agent_name, home, exc)
    except ops.OpError as exc:
        return _err(exc)
    return _ok({"project": result["entity"], "op_id": result.get("op_id"),
                "dir_created": result.get("dir_created")}, result.get("notes"))


@router.patch("/autogroup/project/name")
def patch_project_name(payload: Dict[str, Any] = Body(...),
                       agent: str = Query(default="", description="哪个 agent 的项目（必填单一 agent）")) -> Any:
    """改**显示名**：目录不动、归属不动。"""
    scope = _write_scope(_agent_of(agent, payload))
    if isinstance(scope, JSONResponse):
        return scope
    agent_name, home = scope
    blocked = _need_confirm(payload)
    if blocked is not None:
        return blocked
    try:
        result = ops.rename_name(home, str(payload.get("slug") or ""),
                                 str(payload.get("name") or ""))
    except FileNotFoundError as exc:      # 该 agent 没有库：明确报，不建库、不回落根库
        return _no_db(agent_name, home, exc)
    except ops.OpError as exc:
        return _err(exc)
    return _ok(result, result.get("notes"))


@router.post("/autogroup/project/slug")
def post_project_slug(payload: Dict[str, Any] = Body(...),
                      agent: str = Query(default="", description="哪个 agent 的项目（必填单一 agent）")) -> Any:
    """改 **slug**（高风险）：迁目录 + 迁移归属 + 规则表。需 ``confirm`` 与 ``confirm_slug``。"""
    scope = _write_scope(_agent_of(agent, payload))
    if isinstance(scope, JSONResponse):
        return scope
    agent_name, home = scope
    slug = str(payload.get("slug") or "").strip()
    new_slug = str(payload.get("new_slug") or "").strip()
    try:
        plan = ops.preview_rename_slug(home, slug, new_slug)
    except FileNotFoundError as exc:      # 该 agent 没有库：明确报，不建库、不回落根库
        return _no_db(agent_name, home, exc)
    except ops.OpError as exc:
        return _err(exc)
    blocked = _guard(payload, extra_flag="confirm_slug")
    if blocked is not None:
        if payload.get("confirm") is True and not _flag(payload, "confirm_slug"):
            return base._fail("confirmation_required",
                              "改 slug 要迁移目录，必须再传 confirm_slug=true",
                              extra={"preview": plan})
        return base._fail("confirmation_required",
                          "改 slug 需要 confirm=true + confirm_slug=true（未写任何东西）",
                          extra={"preview": plan})
    if plan["problems"]:
        return base._fail("rename_slug_rejected", "；".join(plan["problems"]),
                          status_code=409, extra={"preview": plan})
    try:
        result = ops.rename_slug(home, slug, new_slug, move_dir=payload.get("move_dir") is not False)
    except FileNotFoundError as exc:      # 该 agent 没有库：明确报，不建库、不回落根库
        return _no_db(agent_name, home, exc)
    except ops.OpError as exc:
        return _err(exc)
    return _ok(result, result.get("notes"))


@router.post("/autogroup/project/path")
def post_project_path(payload: Dict[str, Any] = Body(...),
                      agent: str = Query(default="", description="哪个 agent 的项目（必填单一 agent）")) -> Any:
    """改目录：``mode=migrate`` 搬内容 / ``mode=rebind`` 只改指向。"""
    scope = _write_scope(_agent_of(agent, payload))
    if isinstance(scope, JSONResponse):
        return scope
    agent_name, home = scope
    slug = str(payload.get("slug") or "").strip()
    new_path = str(payload.get("new_path") or "").strip()
    mode = str(payload.get("mode") or ops.PATH_REBIND).strip()
    try:
        plan = ops.preview_change_path(home, slug, new_path, mode=mode)
    except FileNotFoundError as exc:      # 该 agent 没有库：明确报，不建库、不回落根库
        return _no_db(agent_name, home, exc)
    except ops.OpError as exc:
        return _err(exc)
    blocked = _need_confirm(payload)
    if blocked is not None:
        return base._fail("confirmation_required",
                          "改目录需要 confirm=true（未写任何东西）", extra={"preview": plan})
    if plan["problems"]:
        return base._fail("change_path_rejected", "；".join(plan["problems"]),
                          status_code=409, extra={"preview": plan})
    try:
        result = ops.change_path(home, slug, new_path, mode=mode,
                                 remove_old_empty_dir=_flag(payload, "remove_old_empty_dir"))
    except FileNotFoundError as exc:      # 该 agent 没有库：明确报，不建库、不回落根库
        return _no_db(agent_name, home, exc)
    except ops.OpError as exc:
        return _err(exc)
    return _ok(result, result.get("notes"))


@router.post("/autogroup/project/dissolve")
def post_project_dissolve(payload: Dict[str, Any] = Body(...),
                          agent: str = Query(default="", description="哪个 agent 的项目（必填单一 agent）")) -> Any:
    """解散项目。**必须显式选** ``to=ungrouped`` 或 ``to=move&target_slug=...``。"""
    scope = _write_scope(_agent_of(agent, payload))
    if isinstance(scope, JSONResponse):
        return scope
    agent_name, home = scope
    slug = str(payload.get("slug") or "").strip()
    to = str(payload.get("to") or "").strip()
    target_slug = payload.get("target_slug")
    dir_action = str(payload.get("dir_action") or ops.DIR_KEEP)
    if not to:
        return base._fail("dissolve_target_required",
                          "解散必须显式选归属去向 to=ungrouped（退回未分组）或 to=move（迁到 target_slug）；"
                          "禁止静默丢归属")
    try:
        plan = ops.preview_dissolve(home, slug, to=to, target_slug=target_slug,
                                    dir_action=dir_action)
    except FileNotFoundError as exc:      # 该 agent 没有库：明确报，不建库、不回落根库
        return _no_db(agent_name, home, exc)
    except ops.OpError as exc:
        return _err(exc)
    blocked = _need_confirm(payload)
    if blocked is not None:
        return base._fail("confirmation_required",
                          "解散需要 confirm=true（未写任何东西）", extra={"preview": plan})
    if plan["problems"]:
        return base._fail("dissolve_rejected", "；".join(plan["problems"]),
                          status_code=409, extra={"preview": plan})
    try:
        result = ops.dissolve(home, slug, to=to, target_slug=target_slug, dir_action=dir_action)
    except FileNotFoundError as exc:      # 该 agent 没有库：明确报，不建库、不回落根库
        return _no_db(agent_name, home, exc)
    except ops.OpError as exc:
        return _err(exc)
    return _ok({"slug": result["slug"], "to": result["to"],
                "target_slug": result["target_slug"], "moved": result["moved"],
                "ungrouped": result["ungrouped"], "cwd_sync_failed": result["cwd_sync_failed"],
                "removed_dir": result["removed_dir"], "project": result["entity"],
                "op_id": result.get("op_id")}, result.get("notes"))


@router.post("/autogroup/project/delete")
def post_project_delete(payload: Dict[str, Any] = Body(...),
                        agent: str = Query(default="", description="哪个 agent 的项目（必填单一 agent）")) -> Any:
    """删项目实体（与解散分开）。有归属 ⇒ 拒绝。"""
    scope = _write_scope(_agent_of(agent, payload))
    if isinstance(scope, JSONResponse):
        return scope
    agent_name, home = scope
    slug = str(payload.get("slug") or "").strip()
    dir_action = str(payload.get("dir_action") or ops.DIR_KEEP)
    try:
        plan = ops.preview_delete(home, slug, dir_action=dir_action)
    except FileNotFoundError as exc:      # 该 agent 没有库：明确报，不建库、不回落根库
        return _no_db(agent_name, home, exc)
    except ops.OpError as exc:
        return _err(exc)
    blocked = _need_confirm(payload)
    if blocked is not None:
        return base._fail("confirmation_required",
                          "删除项目需要 confirm=true（未写任何东西）", extra={"preview": plan})
    if plan["problems"]:
        return base._fail("delete_rejected", "；".join(plan["problems"]),
                          status_code=409, extra={"preview": plan})
    try:
        result = ops.delete(home, slug, dir_action=dir_action)
    except FileNotFoundError as exc:      # 该 agent 没有库：明确报，不建库、不回落根库
        return _no_db(agent_name, home, exc)
    except ops.OpError as exc:
        return _err(exc)
    return _ok(result, result.get("notes"))


@router.post("/autogroup/project/attrs/backfill")
def post_attrs_backfill(payload: Dict[str, Any] = Body(...)) -> Any:
    """**已下线**（t_ac537221）：一次性迁移，实测 ``missing=[] count=0``（17 个项目全有属性行），
    新建项目走 ``POST /autogroup/project/create`` 时就写 ``project_attrs``。

    保留这个 410 墓碑（不直接删路由）是为了让老脚本/老书签拿到**可读的原因**，
    而不是 404 让人以为路由拼错了。它**不在**任何 UI 流程里（主流程只走 project/create）。
    """
    return base._fail("endpoint_removed",
                      "GET/POST /autogroup/project/attrs/backfill 已下线："
                      "属性行回填是一次性迁移，已完成（实测 0 条缺失）；"
                      "新建项目请用 POST /autogroup/project/create", status_code=410)


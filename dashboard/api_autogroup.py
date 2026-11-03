# -*- coding: utf-8 -*-
"""hermes-console · 项目分组后端（C8a）：把 hermes-auto-group 的判定能力包成 HTTP。

挂载契约：本文件不单独挂载。``plugin_api.py`` 把本目录加进 ``sys.path`` 后 import 本模块，再
``include_router(api_autogroup.router)``；最终 URL 是 ``/api/plugins/hermes-console/autogroup/...``。

**不实现任何分组判定**：判定/存储/批次撤销全部来自 ``plugins/hermes-auto-group/core.py``（按文件
路径加载成 ``hermes_auto_group_core``，与插件 ``__init__.py`` 同名同文件 ⇒ 复用同一份模块对象）。
本文件只做 HTTP 包装、参数校验、**写后回读**、补 UI 读不到的只读信息（会话标题/profile/项目名）。

路由：``GET status``｜``GET suggestions``｜``GET rules``｜``POST rules``｜``POST apply``｜
``POST undo``｜``POST reject``｜``POST scan``｜``GET job/{id}``（C8a）。

t_43c5fd05 增补（面板写操作：建项目 / 批量归入 / 快速切换 / 自动模式 / 可撤销）：
``GET projects``｜``POST projects``｜``PATCH projects``｜``DELETE projects``｜``POST assign``｜
``GET ops``｜``GET auto``｜``POST auto``｜``POST auto/tick``。所有面板写操作都在
``auto-group/assignments.db`` 的 ``console_ops`` 表里留一条**完整回滚台账**（逐会话写入前的
整行快照 + cwd 原值），所以 ``POST undo`` 能真的还原到写入前状态（不只是把 status 改回
suggested —— 那会把「原本已归到 A 项目」的会话误退回「未归入」）。

四条硬约束：① 写操作成功后再回读（``load_rules``/``load_suggestions``/``status_summary``），写
操作自己的返回值不当成功证据；② 扫描只登记 job + 起后台线程，立刻返回 ``job_id``；③ ``apply``
的 ``confirm`` 不是 true 一律 400 ``confirmation_required`` + 预览体；④ 读不到给 ``null`` 并在
``notes`` 写明原因，不填 0 冒充真实值。dashboard 进程里没有宿主 agent ``ctx``（``classify_by_llm``
需要它），扫描因此是纯规则分类，``llm`` 字段写明「跳过了、为什么」。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Query  # type: ignore
from fastapi.responses import JSONResponse  # type: ignore

_DASHBOARD_DIR = Path(__file__).resolve().parent
if str(_DASHBOARD_DIR) not in sys.path:
    # 兄弟模块 api_agents 只能靠 sys.path 找到：本文件被 dashboard 用合成模块名 exec，无包上下文。
    sys.path.insert(0, str(_DASHBOARD_DIR))
from api_agents import hermes_home  # noqa: E402

router = APIRouter()
CORE_MODULE_NAME = "hermes_auto_group_core"   # 与插件 __init__.py 同名同文件 ⇒ 同一份模块
SUGGESTIONS_MAX_LIMIT = 200
SCAN_DEFAULT_LIMIT = 40                       # 与插件 auto_group 工具默认值一致
JOBS_KEEP = 20                                # 内存里保留多少个 job（进程重启即失）
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
KEYWORD_MAX = 40
#: 看板任务来源。硬约束（t_53b48743）：这些会话不得进入普通项目，所有写入口一律排除。
KANBAN_SOURCE = "kanban"


def auto_group_dir() -> Path:
    """``plugins/hermes-auto-group``（与本插件同树，不看 HERMES_HOME 环境变量）。"""
    return _DASHBOARD_DIR.parents[1] / "hermes-auto-group"


def _load_core():
    """按文件路径加载插件 core.py，并与插件自己注册的模块去重。"""
    existing = sys.modules.get(CORE_MODULE_NAME)
    path = auto_group_dir() / "core.py"
    if not path.is_file():
        raise FileNotFoundError("找不到 hermes-auto-group/core.py：" + str(path))
    if existing is not None:
        found = getattr(existing, "__file__", None)
        try:
            if found and Path(found).resolve() == path.resolve() and hasattr(existing, "status_summary"):
                return existing
        except OSError:
            pass
        del sys.modules[CORE_MODULE_NAME]
    spec = importlib.util.spec_from_file_location(CORE_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError("无法为 " + str(path) + " 建立 import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[CORE_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _home() -> Path:
    """Hermes 主目录（所有 profile 的共同父目录）。

    不用 ``core.hermes_home()``：它读 ``HERMES_HOME``，而 worker/gateway 进程里该变量常是
    ``<home>/profiles/<p>``，照读会把 projects.db / state.db 指到 profile 自己的目录（C7 已踩过）。
    """
    return hermes_home()


# ---------------------------------------------------------------------------
# agent 作用域（t_4fa94e17）
# ---------------------------------------------------------------------------
#
# **一个解析点**：四层数据（会话库 / 项目库 / 归属库 / 规则）全都相对 home 算，
# 所以只要把 home 换成「该 agent 的 home」，四层一起按 agent 分开。
# 这是本卡的核心收敛 —— 以前只有会话库走了这条解析，另外三层一律吃根 home，
# 于是 work 的会话被归进小白的项目库（``mt-table``）。

def agent_home(agent: Optional[str], home: Optional[Path] = None) -> Path:
    """``agent`` → 该 agent 的数据 home。``default``/空 ⇒ 根 home；其它 ⇒ ``profiles/<p>``。

    ``__all__`` **没有**单一 home（抛 ``ValueError``）：写操作必须先选定 agent，
    读操作走 :func:`agent_homes` 逐个 agent 读、绝不拼一份跨 agent 的假清单。
    """
    core = _load_core()
    return core.agent_home(home if home is not None else _home(), agent)


def is_all_agent(agent: Optional[str]) -> bool:
    core = _load_core()
    return str(agent or "").strip() == core.AGENT_ALL


def agent_homes(agent: Optional[str], home: Optional[Path] = None) -> List[Tuple[str, Path]]:
    """``[(agent 名, 该 agent 的 home)]``。

    * 点名 agent ⇒ 只回它一个（``__all__`` 会 ValueError，调用方先判 :func:`is_all_agent`）；
    * 未点名 / ``__all__`` ⇒ 根（default）+ 每个 profile。**按 agent 分开读，用完再合并**，
      绝不把某 agent 的项目库当成全局可选项目展示。
    """
    root = Path(home) if home is not None else _home()
    name = str(agent or "").strip()
    if name and name != _load_core().AGENT_ALL:
        return [(name, agent_home(name, root))]
    out: List[Tuple[str, Path]] = [(_load_core().DEFAULT_AGENT, root)]
    profs = root / "profiles"
    if profs.is_dir():
        for entry in sorted(profs.iterdir()):
            if entry.is_dir() and (entry / "config.yaml").is_file():
                out.append((entry.name, entry))
    return out


def _scope(agent: Optional[str], home: Optional[Path] = None) -> Tuple[str, Path]:
    """写路径的作用域：``(agent, home)``。``__all__`` ⇒ 400 ``agent_required``。

    返回 ``(agent, home)``；出错时抛 ``ScopedWrite``（路由层统一转 400）。
    """
    name = str(agent or "").strip() or _load_core().DEFAULT_AGENT
    if is_all_agent(name):
        raise ScopedWrite(
            "agent_required",
            "分组/建项目/写归属是**按 agent** 的：agent=__all__ 只读，不能写入。"
            "请先选定一个 agent（例如 work / default）再操作。")
    return name, agent_home(name, home)


class ScopedWrite(Exception):
    """写路径缺少具体 agent（或 agents 不唯一）。路由层转 400 ``agent_required``。"""

    def __init__(self, code: str, message: str, extra: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra or {}


def _agent_owning_session(session_id: str, home: Optional[Path] = None) -> str:
    """反查一个 session_id 属于哪个 agent（**只读**，扫 profiles 库的 sessions 表）。

    写路径（改建议 / 切换归属 / 忽略）在调用方没点名 agent 时用它推断归属：
    宁可多扫一次库，也不能拿根 home 去写别人的库 —— 那正是本卡要修的 bug。

    读不到 / 多处重复时返回 ``__all__``（由调用方报 ``agent_required``，不猜）。
    """
    root = Path(home) if home is not None else _home()
    try:
        import autogroup_ai as engine
        owners = {str(k): str(v) for k, v in (engine.session_profiles(root) or {}).items()}
    except Exception:  # noqa: BLE001 - 反查失败就交给调用方按 agent_required 报
        return _load_core().AGENT_ALL
    hit = owners.get(str(session_id))
    if not hit:
        return _load_core().AGENT_ALL
    return hit


def _fail(code: str, message: str, status_code: int = 400,
          extra: Optional[Dict[str, Any]] = None) -> JSONResponse:
    content: Dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
    if extra:
        content["error"].update(extra)
        content.update({k: v for k, v in extra.items() if k not in ("code", "message")})
    return JSONResponse(status_code=status_code, content=content)


def _describe(exc: BaseException) -> str:
    return type(exc).__name__ + "：" + str(exc)


def _session_meta(home: Path, ids: List[str]) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
    """会话 id → {title, profile, cwd, message_count, last_activity_at}（只读 state.db）。

    t_bba9216d：**扫该 home 下全部 profile 库**，不只根库。以前这里固定读 ``<home>/state.db``，
    而 work/xiaomo/worker 的会话住在 ``profiles/<p>/state.db`` ⇒ 候选会话的标题一律缺失
    （界面「会话标题缺失」而列表里明明有标题）。``profile`` 字段按**归属**给（
    ``profile_name`` 点名了真实 profile 就用它，否则用所在库的 profile）。
    """
    wanted = [i for i in dict.fromkeys(ids) if i]
    if not wanted:
        return {}, None
    import autogroup_ai as engine

    targets = engine.profile_scan_targets(home)
    if not targets:
        return {}, "没有可读的 state.db：<home>/state.db 与 profiles/*/state.db 都不存在"
    known = engine._supported_profiles(home)
    out: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    remaining = set(wanted)
    for container, db_path in targets:
        if not remaining:
            break
        path = Path(db_path)
        if not path.is_file():
            continue
        chunk = sorted(remaining)
        try:
            conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
                picked = ["id", "title", "cwd", "source", "message_count", "last_activity_at"]
                for optional in ("display_name", "profile_name"):
                    if optional in cols:
                        picked.append(optional)
                for start in range(0, len(chunk), 400):  # 分批，避开 sqlite 变量上限
                    part = chunk[start:start + 400]
                    sql = ("SELECT " + ", ".join(picked) + " FROM sessions WHERE id IN ("
                           + ",".join("?" * len(part)) + ")")
                    for row in conn.execute(sql, part):
                        data = dict(row)
                        sid = str(data["id"])
                        if sid in out:
                            continue
                        out[sid] = {
                            "title": data.get("title") or data.get("display_name") or "",
                            "profile": engine._row_profile(data, container, known),
                            "source": data.get("source"),
                            "cwd": data.get("cwd"),
                            "message_count": data.get("message_count"),
                            "last_activity_at": data.get("last_activity_at"),
                            "db": str(path)}
                        remaining.discard(sid)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - 标题是装饰信息，读不到不能拖垮整页
            errors.append(str(path) + "：" + _describe(exc))
    if not out and errors:
        return {}, "读 state.db 失败：" + "；".join(errors[:3])
    return out, (None if not errors else "部分库读失败（其余照常）：" + "；".join(errors[:3]))


def _project_names(home: Path) -> Dict[str, Dict[str, Any]]:
    """slug → {name, path}。projects.db 读不到就是空表（UI 回落到 slug 本身）。"""
    try:
        core = _load_core()
        return {str(p.get("slug")): {"name": p.get("name"), "path": p.get("primary_path")}
                for p in core.list_projects(home) if p.get("slug")}
    except Exception:  # noqa: BLE001
        return {}


def _basis(row: Dict[str, Any]) -> Dict[str, Any]:
    """把 core 存下的 (source, reason, confidence) 翻成 UI 可分档的「判定依据」，不重新判定。"""
    source, reason, confidence = (row.get("source") or "none").lower(), row.get("reason") or "", row.get("confidence")
    if source == "rule":
        words = ([w.strip() for w in reason.split("：", 1)[1].replace("，", "、").split("、") if w.strip()]
                 if "：" in reason else [])
        return {"kind": "rule", "keywords": words, "confidence": confidence, "reason": reason,
                "label": ("规则命中：" + "、".join(words)) if words else (reason or "规则命中")}
    if source == "llm":
        return {"kind": "llm", "keywords": [], "confidence": confidence, "reason": reason,
                "label": "模型判断" + ("（置信度 " + str(confidence) + "）" if confidence is not None else "")}
    return {"kind": source, "keywords": [], "confidence": confidence, "reason": reason, "label": reason or "无依据"}


def _item(row: Dict[str, Any], meta: Dict[str, Any], projects: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    slug = row.get("project_slug")
    project = projects.get(slug) or {}
    return {"session_id": row.get("session_id"), "title": meta.get("title") or "",
            "profile": meta.get("profile") or "", "message_count": meta.get("message_count"),
            "last_activity_at": meta.get("last_activity_at"), "project_slug": slug,
            "project_name": project.get("name") or slug, "project_path": project.get("path"),
            "project_exists": bool(project), "confidence": row.get("confidence"), "source": row.get("source"),
            "basis": _basis(row), "status": row.get("status"), "batch_id": row.get("batch_id"),
            "updated_at": row.get("updated_at"), "applied_at": row.get("applied_at")}


def _batch_stats(home: Path) -> Dict[str, Any]:
    """批次总数 / 未撤销数（只读投影：core 没有 list_batches，但 batches 表就是真相源）。"""
    try:
        core = _load_core()
        path = core.store_path(home)
        if not path.exists():
            return {"total": 0, "undone": 0, "active": 0}
        conn = core.connect_ro(path)
        try:
            total = conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
            undone = conn.execute("SELECT COUNT(*) FROM batches WHERE undone = 1").fetchone()[0]
        finally:
            conn.close()
        return {"total": int(total), "undone": int(undone), "active": int(total - undone)}
    except Exception:  # noqa: BLE001 - 批次数是装饰信息，读不到给 null 由 notes 说明
        return {"total": None, "undone": None, "active": None}


def _status_block(agent: str, home: Path, home_label: str) -> Dict[str, Any]:
    """单个 agent 的 status 块（数字全部来自 core，读的是**该 agent 自己的**库）。"""
    core = _load_core()
    summary = core.status_summary(home)
    rules = core.load_rules(home, agent)
    projects = core.list_projects(home)
    last = summary.get("last_batch")
    return {
        "agent": agent, "home": home_label,
        "store": summary.get("store"), "store_exists": summary.get("exists"),
        "suggested": summary.get("suggested"), "applied": summary.get("applied"),
        "rejected": summary.get("rejected"), "by_project": summary.get("by_project"),
        "last_batch": dict(last) if last else None,
        "batches": _batch_stats(home),
        "rules_projects": len(rules), "rules_total": sum(len(v) for v in rules.values()),
        "projects_total": len(projects),
        "projects": [{"slug": str(p.get("slug")), "name": p.get("name")} for p in projects],
    }


@router.get("/autogroup/status")
def get_status(agent: str = Query("", description="按哪个 agent 统计（空/default=根库；__all__=逐 agent 分开报，不合并）")) -> Any:
    """待确认 / 已应用 / 已忽略 / 规则数 / 批次数。数字全部来自 core，**按 agent 分开读**。

    ``agent=__all__`` / 不传：**逐个 agent 各报一块**（``by_agent``），顶层数字是各块相加的
    合计。**不产生跨 agent 的项目清单** —— ``projects`` 只在点名单个 agent 时给（``__all__``
    时为空数组 + ``needs_agent=true``），因为「把某个 agent 的项目当成全局可选项目」正是本卡
    要消灭的那个 bug。
    """
    home = _home()
    try:
        core = _load_core()
        pairs = agent_homes(agent, home)
        blocks = []
        for name, ahome in pairs:
            label = str(ahome)
            try:
                blocks.append(_status_block(name, ahome, label))
            except Exception as exc:  # noqa: BLE001 - 单个 agent 坏掉不许拖垮整页
                blocks.append({"agent": name, "home": label,
                               "error": {"code": "agent_unavailable",
                                         "message": _describe(exc)},
                               "suggested": None, "applied": None, "rejected": None,
                               "projects_total": None, "projects": [],
                               "store_exists": False, "store": None, "by_project": {},
                               "batches": {"total": None, "undone": None, "active": None},
                               "last_batch": None, "rules_projects": None, "rules_total": None})
    except Exception as exc:  # noqa: BLE001 - 插件坏了要说清是哪一步
        return _fail("autogroup_unavailable", "读取 hermes-auto-group 失败：" + _describe(exc), status_code=200)
    single = len(blocks) == 1
    merged = blocks[0] if single else {}
    notes: List[str] = []
    summed: Dict[str, Any] = {}
    for key in ("suggested", "applied", "rejected"):
        vals = [b.get(key) for b in blocks]
        summed[key] = (sum(v for v in vals if isinstance(v, int))
                       if any(isinstance(v, int) for v in vals) else None)
    if not single:
        notes.append("agent=__all__：以上是 %d 个 agent **分开统计后相加**的合计；"
                     "项目清单不跨 agent 合并（请先选定一个 agent 再分组/建项目）" % len(blocks))
        broken = [b["agent"] for b in blocks if b.get("error")]
        if broken:
            notes.append("这些 agent 的库读不到，已单独标注、未计入合计：" + "、".join(broken))
    else:
        b0 = blocks[0]
        if not b0.get("store_exists"):
            notes.append("建议库还没建（首次扫描后才会出现）：" + str(b0.get("store")))
        if not b0.get("projects"):
            notes.append("该 agent 的 projects.db 里没有未归档项目，建议无法落到具体项目")
        if (b0.get("batches") or {}).get("total") is None:
            notes.append("batches 表读不到，批次数给 null（不是 0）")
    return {"ok": True, "data": {
        "agent": (blocks[0]["agent"] if single else core.AGENT_ALL),
        "agent_scoped": single,
        "needs_agent": not single,
        "store": merged.get("store"), "store_exists": merged.get("store_exists"),
        "suggested": summed["suggested"], "applied": summed["applied"],
        "rejected": summed["rejected"], "by_project": merged.get("by_project"),
        "last_batch": merged.get("last_batch"),
        "batches": merged.get("batches"), "batches_total": (merged.get("batches") or {}).get("total"),
        "rules_projects": merged.get("rules_projects"), "rules_total": merged.get("rules_total"),
        "projects_total": merged.get("projects_total"),
                "projects": (merged.get("projects") or []) if single else [],
        "by_agent": blocks}, "notes": notes, "home": str(home)}


@router.get("/autogroup/suggestions")
def get_suggestions(
    status: str = Query("suggested", description="suggested / applied；其它值原样传给 core"),
    limit: int = Query(50, description="每页条数，钳到 1..200"),
    offset: int = Query(0, description="偏移，>=0"),
    agent: str = Query("", description="按哪个 agent 读（空=default 根库；__all__=逐 agent 合并，每行带它自己的 agent）"),
) -> Any:
    """建议列表（分页）。总数与排序都来自 ``core.load_suggestions``，本路由只补标题与项目名。

    **按 agent 分读**（t_4fa94e17）：``load_suggestions`` 读的是该 agent 自己的
    ``<agent_home>/auto-group/assignments.db``，项目名也只用**该 agent 自己的**
    ``projects.db`` 解析 ⇒ work 的建议里不可能出现小白库里的项目名。
    ``agent=__all__`` 时逐 agent 合并（每行带 ``agent`` 字段，项目名各自解析），
    不是把某个 agent 的项目当成全局清单。
    """
    home = _home()
    page_limit, page_offset = max(1, min(SUGGESTIONS_MAX_LIMIT, int(limit))), max(0, int(offset))
    try:
        core = _load_core()
        pairs = agent_homes(agent, home)
        rows: List[Dict[str, Any]] = []
        owner: Dict[str, str] = {}
        per_agent_counts: Dict[str, int] = {}
        for name, ahome in pairs:
            try:
                got = list(core.load_suggestions(ahome, status))
            except Exception as exc:  # noqa: BLE001 - 单个 agent 读不到不许拖垮整页
                per_agent_counts[name] = -1
                continue
            per_agent_counts[name] = len(got)
            for row in got:
                sid = row.get("session_id")
                # (agent, session_id) 才是唯一键：__all__ 合并时同一 id 在两个 agent 下是两行
                rows.append(dict(row, agent=name))
                owner[(name, sid)] = name
    except Exception as exc:  # noqa: BLE001
        return _fail("autogroup_unavailable", "读取建议失败：" + _describe(exc), status_code=200)
    page = rows[page_offset:page_offset + page_limit]
    meta, meta_error = _session_meta(home, [r.get("session_id") for r in page if r.get("session_id")])
    items: List[Dict[str, Any]] = []
    for row in page:
        ahome = agent_home(row.get("agent"), home)
        names = _project_names(ahome)
        item = _item(row, meta.get(row.get("session_id")) or {}, names)
        item["agent"] = row.get("agent")
        items.append(item)
    notes: List[str] = []
    if not rows:
        notes.append("归属表里没有 status=" + status + " 的建议"
                     + ("——先点「扫描并生成建议」" if status == "suggested" else ""))
    if len(pairs) > 1:
        notes.append("agent=__all__：逐 agent 分开读后合并（每行的项目名用它**自己的** projects.db 解析）；"
                     "各 agent 条数：" + "、".join("%s=%s" % (k, ("读不到" if v < 0 else v))
                                                for k, v in per_agent_counts.items()))
    if meta_error:
        notes.append("会话标题缺失：" + meta_error)
        for item in items:
            item["meta_error"] = meta_error
    return {"ok": True, "data": {"status": status, "total": len(rows), "limit": page_limit,
                                 "offset": page_offset, "items": items,
                                 "agent": (pairs[0][0] if len(pairs) == 1 else core.AGENT_ALL),
                                 "agent_scoped": len(pairs) == 1,
                                 "by_agent_counts": per_agent_counts}, "notes": notes}


@router.get("/autogroup/rules")
def get_rules(agent: str = Query("", description="按哪个 agent 的规则表（空/default=根库；__all__=逐 agent 分开返回）")) -> Any:
    """规则表：slug → 关键词，并区分内置（面板改不了）与用户追加（rules.json）。

    **按 agent 收敛**（t_4fa94e17 C 段）：内置 ``DEFAULT_RULES`` 只属于 ``default``
    （那是小白 17 个项目的关键词表）。点名 work 等其它 agent 时返回的是**它自己的**
    ``<agent_home>/auto-group/rules.json``，且只保留 slug 在本 agent ``projects.db``
    里真实存在的条目 —— 小白库里的 ``mt-table``/``quick-send`` 不会出现在 work 的规则表里。
    """
    home = _home()
    try:
        core = _load_core()
        pairs = agent_homes(agent, home)
    except Exception as exc:  # noqa: BLE001
        return _fail("autogroup_unavailable", "读取规则失败：" + _describe(exc), status_code=200)
    blocks = []
    for name, ahome in pairs:
        try:
            merged = core.load_rules(ahome, name)
            builtin = ({k: list(v) for k, v in getattr(core, "DEFAULT_RULES", {}).items()}
                       if name == getattr(core, "DEFAULT_RULES_AGENT", "default") else {})
        except Exception as exc:  # noqa: BLE001
            blocks.append({"agent": name, "error": {"code": "agent_unavailable",
                                                    "message": _describe(exc)}, "rules": []})
            continue
        projects = _project_names(ahome)
        out = []
        for slug in sorted(merged):
            words = merged[slug]
            base = set(builtin.get(slug, []))
            out.append({"slug": slug, "name": (projects.get(slug) or {}).get("name") or slug,
                        "project_exists": slug in projects, "builtin": sorted(base),
                        "custom": [w for w in words if w not in base], "keywords": words})
        extra_file = ahome / "auto-group" / "rules.json"
        blocks.append({"agent": name, "home": str(ahome), "rules": out,
                       "total_projects": len(out), "extra_file": str(extra_file),
                       "extra_exists": extra_file.is_file()})
    single = len(blocks) == 1
    first = blocks[0]
    notes: List[str] = []
    if single and not first.get("rules"):
        notes.append("该 agent 一条规则都没有：%s（内置 DEFAULT_RULES 只属于 default，"
                     "别的 agent 的规则来自它自己的 rules.json）"
                     % ("读库失败" if first.get("error") else "既没有内置词也没有自定义词"))
    if not single:
        notes.append("agent=__all__：逐 agent 分开返回（每块的规则只对它自己的项目库有效），"
                     "不合并成一份跨 agent 关键词表")
    return {"ok": True, "data": {
        "agent": (first["agent"] if single else core.AGENT_ALL), "agent_scoped": single,
        "rules": first.get("rules") or [], "total_projects": first.get("total_projects"),
        "extra_file": first.get("extra_file"), "extra_exists": first.get("extra_exists"),
        "by_agent": blocks}, "notes": notes}


def _rules_readback(home: Path, slug: str, keyword: str, expect: bool,
                    agent: Optional[str] = None) -> Dict[str, Any]:
    """写后回读：规则表里到底有没有这个词（读的是**该 agent 自己的** home）。"""
    core = _load_core()
    words = core.load_rules(home, agent).get(slug) or []
    present = keyword in words
    builtin_agent = getattr(core, "DEFAULT_RULES_AGENT", "default")
    return {"slug": slug, "keyword": keyword, "present": present, "expect_present": expect,
            "agent": agent,
            "builtin": (keyword in (getattr(core, "DEFAULT_RULES", {}).get(slug) or [])
                        and (not agent or agent == builtin_agent)),
            "matches_expectation": present == expect, "keywords": words}


#: ``rules.json`` 读改写串行化锁。POST 与 DELETE 共用同一把锁——两者都是
#: 「读全量 → 改一个词 → 写回」，不加锁时并发会丢更新（A 读到的快照里没有 B 刚加的词，
#: A 写回就把 B 的改动抹掉）。跨进程（agent 侧 auto_group 工具）仍无法覆盖，属已知边界。
_RULES_LOCK = threading.Lock()


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """原子写 JSON：同目录临时文件 + ``os.replace``。

    直接 ``write_text`` 会有一瞬间的**半截文件**：并发读者正好在那时 ``json.loads`` 就抛
    解析错，而文件本身其实是好的。``os.replace`` 在同一卷上是原子的，读者要么看到旧的
    完整内容，要么看到新的完整内容。
    """
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex[:8])
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


@router.post("/autogroup/rules")
def post_rule(payload: Dict[str, Any] = Body(...)) -> Any:
    """追加一条关键词 → **回读**再返回（写到**该 agent 自己的** rules.json）。"""
    home = _home()
    slug = str(payload.get("slug") or payload.get("project_slug") or "").strip()
    keyword = str(payload.get("keyword") or "").strip()
    agent = str(payload.get("agent") or "").strip() or None
    if not slug:
        return _fail("invalid_slug", "缺少 slug（建议项目的英文短名）")
    if not SLUG_RE.match(slug):
        return _fail("invalid_slug", "slug 只能是 2~41 位小写字母/数字/短横线：" + slug)
    if not keyword:
        return _fail("invalid_keyword", "缺少 keyword（要追加的关键词）")
    if len(keyword) > KEYWORD_MAX:
        return _fail("invalid_keyword", "keyword 太长（上限 " + str(KEYWORD_MAX) + " 字）：" + keyword)
    try:
        name, ahome = _scope(agent, home)
    except ScopedWrite as exc:
        return _fail(exc.code, exc.message, extra=exc.extra)
    try:
        core = _load_core()
        # 规则只能挂在本 agent 真实存在的项目上：否则就是一条永远写不进去的死规则，
        # 也正是「work 命中 mt-table」那类跨库串组的来源。
        owned = {str(p.get("slug")) for p in core.list_projects(ahome) if p.get("slug")}
        if slug not in owned:
            return _fail("project_not_in_agent",
                         "agent %s 的项目库里没有「%s」——规则不能指向别的 agent 的项目。"
                         "该 agent 现有项目：%s"
                         % (name, slug, "、".join(sorted(owned)) or "（一个都没有）"),
                         status_code=404,
                         extra={"agent": name, "slug": slug, "projects": sorted(owned)})
        core.save_rule(ahome, slug, keyword, agent=name)
        readback = _rules_readback(ahome, slug, keyword, True, agent=name)
    except Exception as exc:  # noqa: BLE001
        return _fail("write_failed", "写入规则失败：" + _describe(exc), status_code=500)
    if not readback["present"]:
        return _fail("readback_failed", "写入返回成功，但回读规则表里没有这条关键词："
                     + keyword + "（agent=" + name + "）",
                     status_code=500, extra={"readback": readback})
    return {"ok": True, "data": readback,
            "notes": ["已写入 agent %s 自己的 rules.json：%s"
                      % (name, ahome / "auto-group" / "rules.json")]}


@router.delete("/autogroup/rules")
def delete_rule(slug: str = "", keyword: str = "", agent: str = "") -> Any:
    """删掉一条**用户追加**的关键词 → **回读**再返回（只动该 agent 自己的 rules.json）。

    只动 ``<agent_home>/auto-group/rules.json`` 里的自定义词；``core.DEFAULT_RULES``
    是代码内置的，删不了（删了下次 import 又回来，属于假成功）。想屏蔽内置词只能
    改 core.py，那是 hermes-auto-group 插件的事，不在本面板职责内。
    """
    home = _home()
    slug = (slug or "").strip()
    keyword = (keyword or "").strip()
    if not slug:
        return _fail("invalid_slug", "缺少 slug（要删哪个项目的关键词）")
    if not keyword:
        return _fail("invalid_keyword", "缺少 keyword（要删哪个关键词）")
    try:
        name, ahome = _scope(agent, home)
    except ScopedWrite as exc:
        return _fail(exc.code, exc.message, extra=exc.extra)
    try:
        core = _load_core()
        builtin_agent = getattr(core, "DEFAULT_RULES_AGENT", "default")
        builtin = (list(getattr(core, "DEFAULT_RULES", {}).get(slug) or [])
                   if name == builtin_agent else [])
    except Exception as exc:  # noqa: BLE001
        return _fail("autogroup_unavailable", "读取内置规则失败：" + _describe(exc), status_code=200)
    if keyword in builtin:
        return _fail("builtin_immutable",
                     "「" + keyword + "」是内置关键词（core.DEFAULT_RULES），面板删不了；"
                     "要停用它得改 hermes-auto-group 的 core.py",
                     status_code=409,
                     extra={"slug": slug, "keyword": keyword, "builtin": builtin})

    home = ahome
    extra_file = home / "auto-group" / "rules.json"
    if not extra_file.is_file():
        return _fail("rule_not_found", "自定义规则文件还不存在，没有可删的词："
                     + str(extra_file), status_code=404,
                     extra={"slug": slug, "keyword": keyword})
    try:
        data = json.loads(extra_file.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, TypeError) as exc:
        return _fail("rule_file_unreadable", "rules.json 读不出来：" + _describe(exc),
                     status_code=500)
    if not isinstance(data, dict):
        return _fail("rule_file_malformed",
                     "rules.json 结构不对（顶层应为 {slug: [关键词...]}，实际是 "
                     + type(data).__name__ + "），为免写坏它，本次不做任何修改",
                     status_code=500, extra={"actual_type": type(data).__name__})
    words = data.get(slug)
    if words is not None and not isinstance(words, list):
        return _fail("rule_file_malformed",
                     "rules.json 里 " + slug + " 的值不是关键词列表（实际是 "
                     + type(words).__name__ + "），本次不做任何修改",
                     status_code=500, extra={"slug": slug, "actual_type": type(words).__name__})
    if not isinstance(words, list) or keyword not in words:
        return _fail("rule_not_found",
                     "「" + keyword + "」不在 " + slug + " 的自定义词里",
                     status_code=404,
                     extra={"slug": slug, "keyword": keyword,
                            "custom": words if isinstance(words, list) else []})
    words.remove(keyword)
    if not words:
        data.pop(slug, None)          # 空列表不留脏数据
    try:
        if data:
            _atomic_write_json(extra_file, data)
        else:
            extra_file.unlink()       # 全空了就把文件删掉，回到「无自定义规则」状态
    except OSError as exc:
        return _fail("write_failed", "写回 rules.json 失败：" + _describe(exc), status_code=500)
    try:
        readback = _rules_readback(home, slug, keyword, False, agent=name)
    except Exception as exc:  # noqa: BLE001
        return _fail("readback_failed", "删除后回读失败：" + _describe(exc), status_code=500)
    if readback["present"]:
        return _fail("readback_failed", "删除返回成功，但回读规则表里这个词还在：" + keyword,
                     status_code=500, extra={"readback": readback})
    return {"ok": True, "data": readback,
            "notes": ["只删了用户追加词；内置词（builtin）不受影响"]}


@router.patch("/autogroup/suggestions")
def patch_suggestion(payload: Dict[str, Any] = Body(...)) -> Any:
    """**改一条待确认建议的目标项目**（只动建议行，不动会话、不写 applied、不改 cwd）。

    界面上「逐条修改」落在这里：用户把 AI/规则的判断改掉，改的是**待确认的建议**，
    真正的写库仍然只在 ``POST /autogroup/apply {confirm:true}``。
    写后回读该行，回读不一致就报错（不认 200 当成功）。
    """
    home = _home()
    session_id = str(payload.get("session_id") or "").strip()
    slug = str(payload.get("project_slug") or payload.get("slug") or "").strip()
    reason = str(payload.get("reason") or "").strip()[:200]
    agent_raw = str(payload.get("agent") or "").strip() or None
    if not session_id:
        return _fail("invalid_session_id", "需要 session_id")
    if not slug:
        return _fail("invalid_slug", "需要 project_slug（要改成哪个项目）")
    if not SLUG_RE.match(slug):
        return _fail("invalid_slug", "slug 只能是 2~41 位小写字母/数字/短横线：" + slug)
    if agent_raw:
        try:
            agent_name, home = _scope(agent_raw, home)
        except ScopedWrite as exc:
            return _fail(exc.code, exc.message, extra=exc.extra)
    else:
        agent_name = _agent_owning_session(session_id, home)
        try:
            home = agent_home(agent_name, home)
        except ValueError as exc:
            return _fail("agent_required", str(exc))
    try:
        core = _load_core()
        projects = {str(p.get("slug")): p for p in core.list_projects(home) if p.get("slug")}
    except Exception as exc:  # noqa: BLE001
        return _fail("autogroup_unavailable", "读取项目清单失败：" + _describe(exc), status_code=200)
    if slug not in projects:
        return _fail("unknown_project",
                     "agent %s 的项目库里没有这个项目：%s" % (agent_name, slug), status_code=404,
                     extra={"agent": agent_name, "slug": slug, "projects": sorted(projects)})
    try:
        conn = core.ensure_store(home)
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE assignments SET project_slug = ?, reason = ?, source = 'manual',"
                    " updated_at = ? WHERE session_id = ? AND status = 'suggested'",
                    (slug, reason or "面板手动改判", time.time(), session_id))
            changed = cur.rowcount
            row = conn.execute("SELECT * FROM assignments WHERE session_id = ?",
                               (session_id,)).fetchone()
            row = dict(row) if row else None
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return _fail("write_failed", "改建议失败：" + _describe(exc), status_code=500)
    if not row:
        return _fail("not_found", "归属表里没有这个会话（先扫描生成建议）：" + session_id, status_code=404)
    if row.get("status") != "suggested":
        return _fail("not_suggested",
                     "该会话当前状态是 %r，不是待确认建议；已归入的要改请用 /autogroup/switch"
                     % row.get("status"), status_code=409, extra={"status": row.get("status")})
    if str(row.get("project_slug")) != slug:
        return _fail("readback_failed",
                     "写入返回成功但回读仍是 %r（预期 %r）" % (row.get("project_slug"), slug),
                     status_code=500, extra={"readback": row})
    return {"ok": True, "data": {"session_id": session_id, "project_slug": slug,
                                 "project_name": (projects.get(slug) or {}).get("name") or slug,
                                 "reason": row.get("reason"), "source": row.get("source"),
                                 "changed": changed, "readback": row},
            "notes": ["只改了建议行；确认应用（POST /autogroup/apply {confirm:true}）才会真正写库"]}


# t_ac537221 收敛：``GET /autogroup/batches`` 已删除（前端零调用，能力被
# ``GET /autogroup/ops`` 的 console_ops 台账 + ``apply`` 回读的 batch_id 完全覆盖）。


# ---------------------------------------------------------------------------
# 扫描（t_ac537221 收敛后：唯一入口 = 扫描未分组会话，可选同一次跑模型）
# ---------------------------------------------------------------------------
_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()

#: **归属表写入互斥**（scan 的 ``save_suggestions`` / 自动 tick / 面板 apply 共用）。
#:
#: 实测（t_aac43d35，非读源码推断）：手动 ``POST /autogroup/scan`` 与
#: ``POST /autogroup/auto/tick`` 同时触发，3/3 轮都出现同一种破坏 ——
#: ``save_suggestions`` 的「先 SELECT status、再写 suggested」两步之间没有事务保护，
#: tick 在中间把该会话写成 applied + cwd，scan 随后的 upsert 又把它按回 suggested，
#: 于是 tick 回执说「写了 12 条 applied」而归属表只剩 11 条，且那条的 cwd 已经指向项目目录
#: （applied 事实消失、cwd 没跟着回退，undo 也找不回它）。
#:
#: 因此把「读现状 → 写归属」这一段放进同一把**进程内** RLock：谁先拿到谁写完，
#: scan 再读 status 时就能看到 applied 并跳过。单进程服务（dashboard 只有一个进程，
#: cron 也是打 HTTP 回来）足够；跨进程各写一份库不在此列。
WRITE_LOCK = threading.RLock()
#: 一次扫描最多看多少条候选（与 /autogroup/suggestions 的 200 上限同口径）。
SCAN_MAX_LIMIT = 200
#: job 结果里回给界面的候选行上限（界面首屏够用，超出走 /autogroup/suggestions 分页）。
SCAN_MAX_ENTRIES = 200
#: 扫描范围：只补未分组（默认） / 全量重建（已 applied 的仍然不会被覆盖）。
SCOPES = ("ungrouped", "all")
#: 没有调模型时 `llm` 字段的三种**真实**原因（不再有硬编码谎报）。
LLM_NOT_REQUESTED = {
    "used": False, "code": "ai_not_requested",
    "message": ("本次只跑关键词规则（没要求 AI）。要模型判定请点「用 AI 分析」，"
                "或带 ai=true 再扫一次")}
LLM_NO_MODEL = {
    "used": False, "code": "no_model_configured",
    "message": ("项目分组 AI 没有可用的 provider/model：既没有独立配置"
                "（auto-group/ai_model.json），该 agent 的 config.yaml 里也没有模型")}


def _job_set(job_id: str, **fields: Any) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job.update(fields)


def _scan_pool(home: Path, core: Any, agent: Optional[str]) -> Tuple[List[Dict[str, Any]], set, set]:
    """扫描池：该 agent 的会话（排除 rejected + **排除看板任务会话**），并返回 applied / suggested 的 id 集合。

    .. note:: 扫描路径**已不走这里**（``_run_scan`` → ``_candidate_snapshot``）；勾选透传
       （``session_ids`` 收窄）在 ``_candidate_snapshot(only_ids=...)`` 里实现。本函数只按
       agent 取池、无法收窄到指定 id，保留给按 agent 看池的只读调用方。

    ``include_kanban=False`` 是**必须的**（t_3c2817b1 实测 bug）：
    ``core.list_sessions`` 的 ``include_kanban`` 默认是 True，而 ``core.EXCLUDED_SOURCES``
    只有 ``('subagent','oneshot','cron')`` —— **kanban 不在里面**。不传这个参数，扫描就会把
    看板任务会话当成「未分组普通会话」去提建议（实测：28 条未分组候选里 26 条是看板会话），
    而 AI 侧走 ``desktop_visible_pool`` 明确排除看板 ⇒ 两套候选集对不上，界面三个数字互相矛盾。

    取**全量**再在内存里筛「未分组」，而不是先 LIMIT 再筛：最近 60 条全已分组时，
    「先 LIMIT 再筛」会让扫描看起来没发现任何东西。
    """
    store_home = agent_home(agent, home)
    rejected = core.rejected_ids(store_home)
    pool = list(core.list_sessions(home, limit=0, exclude_ids=rejected, agent=agent,
                                  include_kanban=False))
    applied = {str(r.get("session_id")) for r in core.load_suggestions(store_home, "applied")}
    suggested = {str(r.get("session_id")) for r in core.load_suggestions(store_home, "suggested")}
    return pool, applied, suggested


def _candidate_snapshot(home: Path, core: Any, agent: Optional[str],
                        scope: str, only_ids: Optional[List[str]] = None,
                        store_home: Optional[Path] = None) -> Dict[str, Any]:
    """**唯一**的候选快照：扫描与 AI 都用这一份（t_3c2817b1 要求「同一份候选快照」）。

    口径 = 桌面端可见（``autogroup_ai.desktop_visible_pool``：排除 acp/kanban/oneshot/subagent/tool、
    hidden=0、archived 但 pinned 仍可见、message_count>=1、压缩续接/subagent 子行不列）∩ 该 agent ∩
    非 rejected；``scope='ungrouped'`` 再排除已有 applied 归属的行（A 模式），``scope='all'`` 保留（B 模式重算）。

    **数据源（t_bba9216d）**：池来自**该 agent 自己的** ``state.db``
    （``profiles/<p>/state.db``，与 ``/overview/sessions?agent=X`` 同源）；``agent`` 为空时是全部库并集。
    以前这里调 ``desktop_visible_pool(home)`` 不传 agent ⇒ 永远只看根库 ⇒ work/xiaomo/worker 的真实
    未分组会话（实测 25 条）一条都进不了候选，界面却按各 profile 库算出「21 条」。

    返回 ``{"rows": [...], "ids": [...], "excluded": {...}, "applied_ids": [...]}``。

    ``only_ids``（可选，t_bbddd533）：给了就**只保留这些 id**（顺序按传入顺序，便于界面回显
    「勾了哪几条」），并在 ``skipped`` 里逐条给出没进候选的**原因**（看板 / 别的 agent / 已忽略 /
    已归入 / 其它状态）—— 勾选透传时绝不能静默丢行。``only_ids=None`` 时行为与改前完全一致。
    """
    import autogroup_ai as engine
    #: ``store_home``（t_4fa94e17）：归属库/规则库按 agent 分；``home`` 必须保持**根 home**
    #: —— 会话池要靠它枚举 ``<root>/profiles/*``（取池与 /overview/sessions?agent=X 同源）。
    shome = Path(store_home) if store_home is not None else Path(home)
    wanted_agent = str(agent or "").strip()
    pool = engine.desktop_visible_pool(home, agents={wanted_agent} if wanted_agent else None)
    rows = [r for r in (pool.get("sessions") or []) if isinstance(r, dict)]
    visibility = dict(pool.get("counts") or {})
    rejected = {str(s) for s in (core.rejected_ids(shome) or set())}
    assignments = {str(r.get("session_id")): r for r in core.load_suggestions(shome, "suggested")}
    for status in ("applied", "undone", "rejected"):
        try:
            for row in core.load_suggestions(shome, status):
                assignments[str(row.get("session_id"))] = row
        except Exception:  # noqa: BLE001 - 老库可能没有这些状态，读不到就当没有
            continue
    applied_ids = {sid for sid, row in assignments.items() if str(row.get("status")) == "applied"}

    out: List[Dict[str, Any]] = []
    seen: set = set()
    #: 与 ``autogroup_ai._candidate_sessions`` 同名的排除项 + 看板（那边在取池阶段就挡掉了）
    #: ``agent`` 的初值来自池的 ``excluded_agent``：池在取小时已经按 agent 收窄，
    #: 别的 agent 的行**根本不会出现在 rows 里**，只看下面的循环会永远数出 0（t_bba9216d
    #: 回归：E2E 断言「把别的 agent 的可见会话明确排除」要的是这个数）。
    excluded = {"rejected": 0, "applied": 0, "agent": int(visibility.get("excluded_agent") or 0),
                "other_status": 0, "duplicate": 0,
                "kanban": int(visibility.get("excluded_kanban") or 0),
                #: 下面这几桶是**取池阶段**就挡掉的（不在池里）。空态文案要把它们跟本池
                #: 分解分开写，否则会读出「本池 47 条，其中看板任务 271 条」这种新的自相矛盾
                #: （t_bba9216d）。
                "hidden": int(visibility.get("excluded_hidden") or 0),
                "source": int(visibility.get("excluded_source") or 0),
                "archived": int(visibility.get("excluded_archived") or 0),
                "no_message": int(visibility.get("excluded_no_message") or 0),
                "child": int(visibility.get("excluded_child") or 0),
                "deduped": int(visibility.get("deduped") or 0)}
    #: 逐条排除原因（只给 ``only_ids`` 用：勾选透传必须能说清「这条为什么没处理」）
    drop_reason: Dict[str, str] = {}
    for row in rows:
        sid = str(row.get("id") or "")
        if not sid or sid in seen:
            excluded["duplicate"] += 1
            drop_reason.setdefault(sid, "重复行")
            continue
        seen.add(sid)
        if str(row.get("source") or "").strip().lower() == KANBAN_SOURCE:
            excluded["kanban"] += 1
            drop_reason[sid] = "看板任务会话（source=kanban），不得进入普通项目"
            continue
        if wanted_agent and str(row.get("_profile") or row.get("profile_name") or "") != wanted_agent:
            excluded["agent"] += 1
            drop_reason[sid] = "属于其它 agent（profile=%s，本次只扫 %s）" % (
                str(row.get("_profile") or row.get("profile_name") or "—"), wanted_agent)
            continue
        if sid in rejected:
            excluded["rejected"] += 1
            drop_reason[sid] = "已手动忽略（rejected）"
            continue
        assignment = assignments.get(sid)
        if assignment is not None:
            status = str(assignment.get("status") or "")
            # 与 AI 侧一致：已 applied 的排除（A 模式）；**其它非 suggested 状态也算排除**
            # （undone / rejected 之类不该被重新提议，否则撤销过的会被立刻again 提上来）
            if status == "applied":
                if scope != "all":
                    excluded["applied"] += 1
                    drop_reason[sid] = "已归入项目（applied）；scope=all 才会重算"
                    continue
            elif status != "suggested":
                excluded["other_status"] += 1
                drop_reason[sid] = "已有其它状态（%s），不重复提议" % (status or "—")
                continue
        out.append(row)
    skipped: List[Dict[str, Any]] = []
    if only_ids is not None:
        # 勾选透传：只保留被点名的 id（按传入顺序），其余逐条给出原因 —— 不静默丢
        wanted = [str(sid) for sid in only_ids]
        by_id = {str(r.get("id")): r for r in out}
        skipped = [{"session_id": sid, "reason": drop_reason.get(sid, "不在本次扫描池内")}
                   for sid in wanted if sid not in by_id]
        out = [by_id[sid] for sid in wanted if sid in by_id]
    return {"rows": out, "ids": [str(r.get("id")) for r in out],
            "excluded": excluded, "applied_ids": sorted(applied_ids),
            "visibility": visibility, "skipped": skipped,
            "pool": len(pool.get("sessions") or []),
            #: 本池口径里被 applied 挡掉的条数（t_bba9216d）：池 − 候选 − rejected − 其它状态。
            #: 界面「池里共 N 条、其中已分组 M 条」必须用这一对**同源**数字；
            #: 用全库 ``len(applied_ids)``（91）去解释一个本池为 0 的池会写出「0 里怎么有 91」。
            "applied_in_pool": max(0, len(pool.get("sessions") or []) - len(out)
                                   - excluded["rejected"] - excluded["other_status"]),
            "profiles_scanned": list(pool.get("profiles") or []),
            "requested_ids": ([str(sid) for sid in only_ids] if only_ids is not None else None)}


def _ai_classify(home: Path, agent: Optional[str], on_progress,
                 candidate_ids: Optional[List[str]] = None,
                 store_home: Optional[Path] = None) -> Dict[str, Any]:
    """跑一次模型分类（引擎 = autogroup_ai，模型 = 本项目自己的 provider/model）。

    ``candidate_ids``：**扫描定下来的那一份候选快照**。给了就把它当作唯一处理范围
    （引擎的 ``skip_ids`` 反向使用：不在快照里的一律跳过），保证「AI 分析的会话」
    与「扫描报告的会话」是同一批，不会各算一套。

    返回 ``{"ok":..., "results": [...], "llm": {...}, "error": ...}``。**只产建议，不写库**
    （写库统一由 core.save_suggestions 在这里之后做）。
    """
    import autogroup_ai as engine
    import autogroup_model as model_cfg

    caller, resolved = model_cfg.make_caller(home, agent)
    if caller is None:
        return {"ok": False, "results": [], "resolved": resolved,
                "llm": dict(LLM_NO_MODEL, resolved=resolved),
                "error": {"code": "no_model_configured",
                          "message": resolved.get("reason") or LLM_NO_MODEL["message"]}}
    skip_ids = None
    engine_candidate_ids: Optional[set] = None
    if candidate_ids is not None:
        wanted = {str(s) for s in candidate_ids}
        visible = engine.candidate_scope(home, agents={agent} if agent else None)
        engine_candidate_ids = {str(s) for s in (visible.get("candidate_session_ids") or [])}
        skip_ids = sorted(engine_candidate_ids - wanted)
    payload = engine.build_preview(home, limit=0, caller=caller, on_progress=on_progress,
                                   agents={agent} if agent else None, skip_ids=skip_ids,
                                   projects_home=(Path(store_home) if store_home is not None else None))
    if not payload.get("ok"):
        err = payload.get("error") or {}
        # 引擎失败 ≠ 没配模型：这里必须分开报，否则界面会把「上游报错」显示成
        # `no_model_configured`（把用户的注意力引到配置上，掩盖真正的失败原因）。
        return {"ok": False, "results": [], "resolved": resolved,
                "llm": {"used": False, "code": "llm_not_called",
                        "provider": resolved.get("provider"), "model": resolved.get("model"),
                        "source": resolved.get("source"), "layer": resolved.get("layer"),
                        "agent": resolved.get("agent"), "calls": 0,
                        "engine_error": err.get("detail") or err.get("code"),
                        "message": "模型调用失败：" + str(err.get("detail") or err.get("code") or "引擎没有给出原因")},
                "error": {"code": err.get("code") or "engine_failed",
                          "message": err.get("detail") or "AI 引擎没有给出失败原因"}}
    results: List[Dict[str, Any]] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        suggested = item.get("suggested") or {}
        slug = suggested.get("project_slug")
        results.append({"session_id": item.get("session_id"), "project_slug": slug,
                        "confidence": suggested.get("confidence"),
                        "reason": suggested.get("reason") or item.get("unjudged_reason"),
                        "source": "llm" if slug else "none",
                        "title": item.get("title")})
    model_info = payload.get("model") or {}
    usage = payload.get("usage") or {}
    calls = int(model_info.get("calls") or 0)
    llm = {
        "used": bool(calls) and bool(model_info.get("provider")),
        "code": "llm_used" if calls else "llm_not_called",
        "provider": model_info.get("provider"),
        "model": model_info.get("model"),
        "source": resolved.get("source"),
        "agent": resolved.get("agent"),
        "calls": calls,
        "elapsed_ms": model_info.get("elapsed_ms"),
        "avg_ms": model_info.get("avg_ms"),
        "usage": usage,
        "degraded": model_info.get("degraded"),
        "audit_path": str(model_cfg.audit_path(home)),
        "message": ("模型真实调用 %d 次（provider=%s model=%s，来源 %s）"
                    % (calls, model_info.get("provider"), model_info.get("model"),
                       resolved.get("source")) if calls else
                    "配置里有 provider/model，但一条都没调成（见 degraded / error）"),
    }
    return {"ok": True, "results": results, "llm": llm, "resolved": resolved,
            "ai_scope": _ai_scope_block(candidate_ids, results, engine_candidate_ids),
            "engine": {"counts": payload.get("counts"), "scope": payload.get("scope"),
                       "undecided": payload.get("undecided") or [],
                       "new_project_candidates": payload.get("new_project_candidates") or [],
                       "notes": payload.get("notes") or []}}


def _ai_scope_block(requested: Optional[List[str]], results: List[Dict[str, Any]],
                    engine_candidate_ids: Optional[set]) -> Optional[Dict[str, Any]]:
    """勾选透传时 AI 侧的账：请求了哪些 / 真调了模型的哪些 / 哪几条没进模型候选集、为什么。

    ``requested is None``（没传 session_ids）⇒ 返回 ``None``，不改动旧返回结构。
    ``unexpected_ids`` 必须永远为空 —— 非空就说明「只分类这 3 条」被打破，是回归信号。
    """
    if requested is None:
        return None
    requested_ids = [str(s) for s in requested]
    classified = [str(r.get("session_id")) for r in results]
    done = set(classified)
    wanted = set(requested_ids)
    return {
        "requested_ids": requested_ids,
        "classified_ids": classified,
        "not_classified": [
            {"session_id": sid,
             "reason": ("不在 AI 引擎候选集内（已归入项目 / 已忽略 / 桌面不可见），未调用模型"
                        if engine_candidate_ids is not None and sid not in engine_candidate_ids
                        else "引擎没有返回这一条（未调用模型）")}
            for sid in requested_ids if sid not in done],
        "unexpected_ids": [sid for sid in classified if sid not in wanted],
        "engine_candidates": (len(engine_candidate_ids)
                              if engine_candidate_ids is not None else None),
    }


def _ai_candidate_ids(home: Path, agent: Optional[str]) -> Optional[List[str]]:
    """AI 引擎**实际**会处理的候选 session id 列表（与 ``/autogroup/ai/scope`` 同一函数）。

    为什么要这个：扫描自己用 ``core.list_sessions``（message_count>=2）建候选池，而 AI 引擎用
    ``_candidate_sessions``（``desktop_visible_pool``：message_count>=1、hidden/archived/pinned
    逐字段判）。两套口径会让界面上的「已处理 N / 共 total」和真正调模型的条数对不上 ——
    正是本卡要求消灭的「AI 另扫一批」。所以 ``ai=True`` 时**以引擎的候选集为准**，
    扫描池只用来补标题/首条消息。
    """
    import autogroup_ai as engine
    scope = engine.candidate_scope(home, agents={agent} if agent else None)
    if not scope.get("ok"):
        return None
    return [str(sid) for sid in (scope.get("candidate_session_ids") or []) if str(sid)]


def _empty_pool_sentence(scope: str, pool: int, applied_in_pool: int, excluded: Dict[str, int],
                         applied_all: int, requested_count: int, candidates: int = 0) -> str:
    """空态文案：**每个数都必须能相加对上**，不把不同口径混进同一句（t_bba9216d）。

    以前这里是直接拼：「本池共 47 条会话（其中已分组 47 条、手动忽略 0 条、其它状态 0 条、
    看板任务 271 条）」。看板任务根本不在池里（取池阶段就被挡掉了），写进括号里会让人读成
    「47 里包含 271」—— 换了位置的同一类自相矛盾，和「池里共 0 条，其中已分组 91 条」同源。

    现在分三层，各层内部恒等式成立：

    1. **本池分解**（能相加）：``池 = 候选 + 本池已分组 + 手动忽略 + 其它状态``；
       只有真凑不上时才写「另有 N 条状态不明」，绝不把候选算进「状态不明」；
    2. **本池之外**（取池阶段就挡掉的，不进池）：看板 / 隐藏 / 不可见来源 / 归档未置顶 / 空会话 / 子行；
    3. **全机器归属表**（跨所有 agent，与上面两层都不是同一口径）：``applied_all``。
    """
    parts: List[str] = []
    if requested_count:
        parts.append("请求的 %d 条会话全部被本次范围排除（逐条原因见 skipped）；" % requested_count)
    elif scope == "ungrouped":
        parts.append("没有未分组会话；")
    else:
        parts.append("该范围内没有可扫描的会话；")

    rejected = int(excluded.get("rejected") or 0)
    other = int(excluded.get("other_status") or 0)
    cands = int(candidates or 0)
    accounted = cands + int(applied_in_pool) + rejected + other
    if pool <= 0:
        parts.append("本池 0 条会话（该 agent 的 state.db 里没有桌面端可见的普通对话）。")
    elif accounted >= pool:
        parts.append(
            "本池 %d 条会话 = 候选 %d 条 + 已分组 %d 条 + 手动忽略 %d 条 + 其它状态 %d 条。"
            % (pool, cands, applied_in_pool, rejected, other))
    else:
        parts.append(
            "本池 %d 条会话 = 候选 %d 条 + 已分组 %d 条 + 手动忽略 %d 条 + 其它状态 %d 条"
            "（合计 %d 条，另有 %d 条状态不明）。"
            % (pool, cands, applied_in_pool, rejected, other, accounted, pool - accounted))

    #: 这些是**取池阶段**就被挡掉的，根本不属于本池：单独成句，绝不写进「本池 N 条」的括号里。
    outside = [("看板任务", int(excluded.get("kanban") or 0)),
               ("隐藏会话", int(excluded.get("hidden") or 0)),
               ("不可见来源", int(excluded.get("source") or 0)),
               ("归档未置顶", int(excluded.get("archived") or 0)),
               ("空会话", int(excluded.get("no_message") or 0)),
               ("子会话", int(excluded.get("child") or 0))]
    shown = ["%s %d 条" % (name, count) for name, count in outside if count]
    if shown:
        parts.append("本池之外另排除 " + "、".join(shown) + "（正常排除，不是漏处理）。")
    parts.append("全机器归属表里已分组 %d 条（跨所有 agent，不是本池口径，不能与本池数字相加）。"
                 % int(applied_all or 0))
    return "".join(parts)


def _run_scan(job_id: str, home: Path, limit: int, *, agent: Optional[str] = None,
              scope: str = "ungrouped", ai: bool = False,
              session_ids: Optional[List[str]] = None) -> None:
    """后台线程：取候选快照 →（可选）模型判定 → 存建议 → 回读。任何异常都写进 job，不往上抛。

    ``session_ids``（t_bbddd533）：给了就只处理这批（勾选透传）；``None`` = 该 agent 的未分组会话，
    与改前完全一致。job 结果固定回 ``requested_ids`` / ``processed_ids`` / ``skipped_ids``，让
    「勾了几条 / 真处理了几条 / 哪几条没处理、为什么」可被外部逐条核对。

    **一份快照，两个消费者**（t_3c2817b1 硬要求）：候选集只算一次（``_candidate_snapshot``），
    规则路径与 AI 路径都吃这一份，并且把候选 ID 列表随 job 结果返回给界面 ——
    「扫描报告 N 条」与「AI 处理 N 条」永远相等，可被外部按 ID 集合逐条核对。
    """
    _job_set(job_id, state="running", started_at=time.time())
    started = time.time()
    try:
        core = _load_core()
        #: 该 agent 的**数据 home**（项目库 / 归属库 / 规则库都在这下面）。
        #: 根 ``home`` 仍留给会话池与 ``_session_meta``（它们要枚举 profiles/*）。
        store_home = agent_home(agent, home)
        projects = core.list_projects(store_home)
        rules = core.load_rules(store_home, agent)
        snapshot = _candidate_snapshot(home, core, agent, scope, only_ids=session_ids,
                                       store_home=store_home)
        pool_rows = snapshot["rows"]
        applied_ids = set(snapshot["applied_ids"])
        snapshot_ids = list(snapshot["ids"])
        total_available = len(snapshot_ids)
        page_ids = snapshot_ids[:limit]
        truncated = total_available > len(page_ids)
        #: 勾选透传的账：请求了哪些 / 真处理了哪些 / 哪些没处理（含 limit 截断），一条不漏
        requested_ids = list(snapshot.get("requested_ids") or [])
        processed_ids = list(page_ids)
        skipped_detail = list(snapshot.get("skipped") or [])
        done_set = set(processed_ids)
        for sid in requested_ids:
            if sid not in done_set:
                skipped_detail.append({"session_id": sid,
                                       "reason": "超出本次 limit=%d，未处理" % limit})
        skipped_ids = [row["session_id"] for row in skipped_detail]
        known = {str(r.get("id")): r for r in pool_rows}
        candidates = [known[sid] for sid in page_ids if sid in known]
        total = len(candidates)
        _job_set(job_id, progress={"done": 0, "total": total},
                 discovered={"candidates": total, "available": total_available,
                             "pool": snapshot.get("pool", len(pool_rows)),
                             "already_grouped": snapshot.get("applied_in_pool", 0),
                             "already_grouped_all": len(applied_ids),
                             "excluded": snapshot["excluded"],
                             "profiles_scanned": snapshot.get("profiles_scanned") or [],
                             "truncated": truncated,
                             "next_offset": len(page_ids) if truncated else None,
                             "candidate_ids": page_ids,
                             "available_ids": snapshot_ids,
                             "requested_ids": requested_ids,
                             "processed_ids": processed_ids,
                             "skipped_ids": skipped_ids,
                             "skipped": skipped_detail})
        if not candidates:
            # 候选 0 条时**不能说「没配模型」**：那会把「没有可归类的会话」误导成配置问题。
            empty_llm = dict(LLM_NOT_REQUESTED) if not ai else {
                "used": False, "code": "no_candidates",
                "message": "候选 0 条：没有任何会话进入 AI 分类，因此一次模型调用都没发生"
                           "（不是没配模型，也不是调用失败）"}
            empty_pool = int(snapshot.get("pool", len(pool_rows)) or 0)
            empty_applied_pool = int(snapshot.get("applied_in_pool", 0) or 0)
            empty_applied_all = len(applied_ids)
            _job_set(job_id, state="done", finished_at=time.time(),
                     progress={"done": 0, "total": 0},
                     result={"scope": scope, "agent": agent or "__all__", "limit": limit,
                             "pool": empty_pool,
                             #: 本池口径里被 applied 挡掉的条数（与 pool 同源，能相加对得上）
                             "already_grouped": empty_applied_pool,
                             #: 归属表里全机器的 applied 总数（跨 profile），**不是**本池口径
                             "already_grouped_all": empty_applied_all,
                             "candidates": 0, "available": 0, "scanned": 0,
                             "matched": 0, "unmatched": 0,
                             "saved": 0,
                             "suggested_total": len(core.load_suggestions(store_home, "suggested")),
                             "elapsed_ms": int((time.time() - started) * 1000),
                             "items": [], "ai": bool(ai),
                             "candidate_ids": [], "available_ids": [],
                             "requested_ids": requested_ids, "processed_ids": [],
                             "skipped_ids": skipped_ids, "skipped": skipped_detail,
                             "ai_scope": (_ai_scope_block(session_ids, [], None)
                                          if session_ids is not None else None),
                             "truncated": False, "next_offset": None,
                             "excluded": snapshot["excluded"],
                             "profiles_scanned": snapshot.get("profiles_scanned") or [],
                             "llm": empty_llm,
                             "empty": True,
                             # 文案必须**同源**：池 / 本池已分组 是一对（能相加对上），
                             # 全库 applied 单独标注，绝不再写「池里共 0 条，其中已分组 91 条」
                             # 这种字面不成立的句子（t_bba9216d）。
                             "empty_reason": _empty_pool_sentence(
                                 scope, empty_pool, empty_applied_pool, snapshot["excluded"],
                                 empty_applied_all, len(requested_ids), candidates=0),
                             "projects_available": [p.get("slug") for p in projects],
                             "rule_keywords": sum(len(v) for v in rules.values())})
            return
        results: List[Dict[str, Any]] = []
        llm_block: Dict[str, Any] = dict(LLM_NOT_REQUESTED)
        ai_engine: Optional[Dict[str, Any]] = None
        ai_error: Optional[Dict[str, Any]] = None
        ai_scope: Optional[Dict[str, Any]] = None
        if ai:
            def _progress(done: int, total_: int) -> None:
                _job_set(job_id, progress={"done": done, "total": total_})

            # 把**这一份快照**交给 AI：不在快照里的会话一律跳过（消灭「AI 另扫一批」）
            outcome = _ai_classify(home, agent, _progress, candidate_ids=page_ids,
                                   store_home=store_home)
            llm_block = outcome["llm"]
            ai_engine = outcome.get("engine")
            ai_scope = outcome.get("ai_scope") if session_ids is not None else None
            if outcome.get("ok"):
                results = outcome["results"]
            else:
                ai_error = outcome.get("error")
                llm_block = dict(llm_block, fell_back_to_rules=True)
        if not results:
            # 规则路径（ai 没要求，或模型失败后回落 —— 回落原因写在 llm.fell_back_to_rules）
            for index, session in enumerate(candidates, 1):
                body = core.first_user_text(store_home, session["id"])
                # ctx=None ⇒ 规则优先，模型那步在 core 内部降级（不抛）
                verdict = core.classify_session(None, session, body, projects, rules, use_llm=True)
                results.append({"session_id": session["id"],
                                "project_slug": verdict.get("project_slug"),
                                "confidence": verdict.get("confidence"),
                                "reason": verdict.get("reason"),
                                "source": verdict.get("source"),
                                "title": session.get("title")})
                _job_set(job_id, progress={"done": index, "total": len(candidates)})
        # 写建议这一段必须与 tick / apply 的写归属互斥：否则「SELECT status → 写 suggested」
        # 会把并发写进来的 applied 按回 suggested（见 WRITE_LOCK 注释里的实测）。
        with WRITE_LOCK:
            saved = core.save_suggestions(store_home, results)
            after = list(core.load_suggestions(store_home, "suggested"))   # 回读：以建议表为准，不信 saved
        summary = core.status_summary(store_home)
        project_names = {str(p.get("slug")): (p.get("name") or p.get("slug")) for p in projects}
        meta, meta_error = _session_meta(home, [r.get("session_id") for r in results])
        matched = sum(1 for r in results if r.get("project_slug"))
        # ``items`` 只装**本次扫描产出**（t_9cd859ba）：上面回读的 ``after`` 是**全库** suggested，
        # 库里历史遗留的、属于别的 agent 的 suggested 行会混进来 ⇒ 「扫描 agent=xiaomo，结果里
        # 不含 default 会话」这类断言假红（功能本身是对的）。所以按本次 ``results`` 的 session_id
        # 过滤 —— ``results`` 本身已按 agent 隔离 —— 全库口径另由 ``suggested_total`` /
        # ``status_summary`` 给出，两者不混用同一个字段。
        by_id = {str(row.get("session_id")): row for row in after}
        items = []
        for sid in [str(r.get("session_id")) for r in results][:SCAN_MAX_ENTRIES]:
            row = by_id.get(sid)
            if row is None:
                # 本次没落在建议表里（并发被 reject/apply 掉）：不冒充「本次产出」
                continue
            info = meta.get(sid) or {}
            items.append({
                "session_id": row.get("session_id"),
                "title": info.get("title") or "",
                "profile": info.get("profile") or "",
                "project_slug": row.get("project_slug"),
                "project_name": project_names.get(str(row.get("project_slug")))
                or row.get("project_slug"),
                "confidence": row.get("confidence"),
                "reason": row.get("reason"),
                "source": row.get("source"),
                "basis": _basis(row),
                "status": row.get("status"),
                "updated_at": row.get("updated_at"),
            })
        notes: List[str] = []
        if ai_error:
            notes.append("AI 判定失败，本次已回落到关键词规则：" + str(ai_error.get("message")))
        if ai_engine and ai_engine.get("undecided"):
            notes.append("AI 无法判定 %d 条（置信度 < 0.5），已列为「未确定」，不会写入"
                         % len(ai_engine["undecided"]))
        if ai_engine and ai_engine.get("new_project_candidates"):
            notes.append("AI 提出 %d 个「新项目」候选；本面板不自动建项目，"
                         "需要的话请在「项目管理」里手动建" % len(ai_engine["new_project_candidates"]))
        if meta_error:
            notes.append("会话标题缺失：" + meta_error)
        if saved == 0 and matched:
            notes.append("命中了 %d 条但没有新写入：这些会话可能已经是待确认建议（内容未变）"
                         "或已归入项目" % matched)
        _job_set(job_id, state="done", finished_at=time.time(),
                 progress={"done": len(candidates), "total": len(candidates)},
                 result={"scope": scope, "agent": agent or "__all__", "limit": limit,
                         "pool": snapshot.get("pool", len(pool_rows)),
                         #: 本池口径里被 applied 挡掉的条数（与 pool 同源）
                         "already_grouped": snapshot.get("applied_in_pool", 0),
                         #: 归属表全机器 applied 总数（跨所有 agent，不是本池口径）
                         "already_grouped_all": len(applied_ids),
                         "candidates": len(candidates), "available": total_available,
                         "truncated": truncated,
                         "next_offset": len(page_ids) if truncated else None,
                         "candidate_ids": page_ids, "available_ids": snapshot_ids,
                         "requested_ids": requested_ids, "processed_ids": processed_ids,
                         "skipped_ids": skipped_ids, "skipped": skipped_detail,
                         "excluded": snapshot["excluded"],
                         "profiles_scanned": snapshot.get("profiles_scanned") or [],
                         "scanned": len(candidates),
                         "matched": matched, "unmatched": len(results) - matched,
                         "saved": saved, "suggested_total": len(after),
                         "applied": summary.get("applied"), "rejected": summary.get("rejected"),
                         "elapsed_ms": int((time.time() - started) * 1000),
                         "items": items, "ai": bool(ai), "llm": llm_block,
                         "ai_engine": ai_engine, "ai_error": ai_error,
                         "ai_scope": ai_scope,
                         "empty": False, "empty_reason": None, "notes": notes,
                         "projects_available": [p.get("slug") for p in projects],
                         "rule_keywords": sum(len(v) for v in rules.values())})
    except Exception as exc:  # noqa: BLE001 - job 失败要变成结构化状态，不是 500
        _job_set(job_id, state="failed", finished_at=time.time(),
                 error={"code": "scan_failed", "message": _describe(exc),
                        "elapsed_ms": int((time.time() - started) * 1000)})


@router.post("/autogroup/scan")
def post_scan(payload: Optional[Dict[str, Any]] = Body(None)) -> Any:
    """登记 job 并起后台线程，**立刻**返回 job_id（不等扫描）。

    入参（全部可选）：``agent``（只扫这个 profile；空 = 全机器）、``limit``（1..200）、
    ``scope`` = ``ungrouped``（默认，只补未分组）/ ``all``（全量重建）、
    ``ai`` = true 时同一次跑模型（用**项目分组自己的** provider/model）、
    ``session_ids`` = 勾选透传（字符串数组）：给了就**只处理这批**；不传 = 该 agent 的未分组会话。

    ``session_ids`` 的三条硬规则（t_bbddd533）：
    * 空数组 / 非数组 → ``400 invalid_session_ids``（与 preview/apply 同口径）；
    * 不属于当前 ``agent`` 的 id → ``400 session_agent_mismatch``，把 ``foreign_ids`` 全列出来
      （**不许静默丢弃**）；
    * 不存在或桌面不可见的 id → ``400 unknown_session_ids``（``unknown_ids``）。
    校验同步做（``desktop_visible_pool`` 实测 ~12ms），不拿一个注定失败的 job 让界面去猜。
    """
    home = _home()
    body = payload or {}
    try:
        limit = max(1, min(SCAN_MAX_LIMIT, int(body.get("limit") or SCAN_DEFAULT_LIMIT)))
    except (TypeError, ValueError):
        return _fail("invalid_limit", "limit 必须是整数")
    scope = str(body.get("scope") or "ungrouped").strip().lower()
    if scope not in SCOPES:
        return _fail("invalid_scope", "scope 只能是 %s（收到 %r）" % (" / ".join(SCOPES), scope))
    agent = str(body.get("agent") or "").strip() or None
    # 扫描**写建议库**，而 `agent=__all__` 的契约是「只读不写」（t_4fa94e17）。
    # 显式传 __all__ 直接 400，绝不回落根库 —— 那会重演「拿小白的库装 work 的建议」。
    # 不传 agent 时按 default 处理（与改前 `list_projects(home)` 的口径一致，老调用方零改动）。
    if is_all_agent(agent):
        return _fail("agent_required",
                     "扫描会写归属库（按 agent 分库），agent=__all__ 是只读口径、不能扫描。"
                     "请先在上方选定一个 agent（例如 work / default）再扫描。",
                     extra={"agent": "__all__", "readonly": True})
    ai = body.get("ai") is True
    raw_ids = body.get("session_ids")
    session_ids: Optional[List[str]] = None
    if raw_ids is not None:
        if not isinstance(raw_ids, (list, tuple)):
            return _fail("invalid_session_ids", "session_ids 必须是字符串数组（收到 %s）"
                         % type(raw_ids).__name__)
        session_ids = [str(i).strip() for i in raw_ids if str(i).strip()]
        if not session_ids:
            return _fail("invalid_session_ids",
                         "session_ids 为空列表；不传表示该 agent 的未分组会话")
        ordered: List[str] = []
        for sid in session_ids:
            if sid not in ordered:
                ordered.append(sid)
        session_ids = ordered
        try:
            import autogroup_ai as engine
            # 存在性 / 归属判定要扫**全部** profile 库（t_bba9216d）：
            #   · 只扫根库 ⇒ work/xiaomo 的真实会话全被误判 unknown_session_ids；
            #   · 只扫目标 agent 库 ⇒ 别的 agent 的 id 会被报成「不存在」（unknown_session_ids），
            #     而契约要求是 `session_agent_mismatch`（400，把 foreign_ids 全列出来）。
            visible = engine.desktop_visible_pool(home)
            profiles = {str(r.get("id")): str(r.get("_profile") or r.get("profile_name") or "")
                        for r in (visible.get("sessions") or []) if isinstance(r, dict)}
        except Exception:  # noqa: BLE001 - 池读不到就不挡，交给后台 job 如实报错
            profiles = None
        if profiles is not None:
            unknown = [sid for sid in session_ids if sid not in profiles]
            if unknown:
                return _fail("unknown_session_ids",
                             "这些 session_id 不存在或桌面不可见（隐藏/归档/看板任务/无消息）："
                             + ", ".join(unknown),
                             extra={"unknown_ids": unknown, "requested_ids": session_ids})
            if agent:
                foreign = [sid for sid in session_ids if profiles[sid] != agent]
                if foreign:
                    return _fail("session_agent_mismatch",
                                 "%d 条会话不属于 agent %r（不会静默丢弃）：%s"
                                 % (len(foreign), agent,
                                    ", ".join("%s(profile=%s)" % (sid, profiles[sid] or "—")
                                              for sid in foreign)),
                                 extra={"foreign_ids": foreign, "agent": agent,
                                        "requested_ids": session_ids})
    job_id = "job-" + uuid.uuid4().hex[:10]
    job = {"id": job_id, "state": "running", "created_at": time.time(), "started_at": None,
           "finished_at": None, "limit": limit, "scope": scope, "agent": agent or "__all__",
           "ai": ai, "requested_ids": session_ids,
           "progress": {"done": 0, "total": 0}, "discovered": None,
           "result": None, "error": None}
    with _JOBS_LOCK:
        _JOBS[job_id] = job
        for key in sorted(_JOBS, key=lambda k: _JOBS[k].get("created_at") or 0)[
                :max(0, len(_JOBS) - JOBS_KEEP)]:
            _JOBS.pop(key, None)
    threading.Thread(target=_run_scan, args=(job_id, home, limit),
                     kwargs={"agent": agent, "scope": scope, "ai": ai,
                             "session_ids": session_ids},
                     name="autogroup-scan-" + job_id, daemon=True).start()
    return JSONResponse(status_code=202, content={
        "ok": True, "job_id": job_id,  # 顶层镜像：卡面契约就是「立刻返回 job_id」
        "data": {"job_id": job_id, "job": job, "poll": "/autogroup/job/" + job_id},
        "notes": ["扫描在后台进行；轮询 GET /autogroup/job/" + job_id
                  + " 直到 state=done/failed",
                  ("只处理勾选的 %d 条会话（processed_ids / skipped_ids 逐条回显）"
                   % len(session_ids) if session_ids else
                   "未指定 session_ids：扫该 agent 的全部未分组会话"),
                  ("本次会逐条调用模型（每条 ~5s），llm 字段如实回 provider/model/calls"
                   if ai else "本次只跑关键词规则；要模型判定请带 ai=true")]})


@router.get("/autogroup/job/{job_id}")
def get_job(job_id: str) -> Any:
    """job 状态：state ∈ running / done / failed，附 progress / discovered / error。"""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        job = dict(job) if job else None
    if job is None:
        return _fail("unknown_job", "没有这个 job：" + job_id + "（进程重启后 job 表会清空）", status_code=404)
    # 顶层 state/progress/error 与 data 里同一份值：卡面契约写的是 {state, progress, error}，
    # 前端按哪一层取都对，避免两边对不上。
    return {"ok": True, "state": job.get("state"), "progress": job.get("progress"),
            "discovered": job.get("discovered"), "error": job.get("error"),
            "data": job, "notes": []}


# apply：预览（confirm≠true → 400）→ 执行 → 回读
def _preview(home: Path, core: Any, session_ids: Optional[List[str]], sync_cwd: bool,
             store_home: Optional[Path] = None) -> Dict[str, Any]:
    """确认前必须看到的：逐条 会话 → 项目、是否同步 cwd、几条会被阈值跳过。不重新分类。

    ``store_home``（t_4fa94e17）：归属库与项目库按 agent 分；``home`` 仍是**根 home**
    （``_session_meta`` 要靠它枚举 profiles/*）。不传 = 与改前一致（default）。
    """
    shome = Path(store_home) if store_home is not None else Path(home)
    rows = list(core.load_suggestions(shome, "suggested"))
    if session_ids:
        keep = set(session_ids)
        rows = [r for r in rows if r.get("session_id") in keep]
    projects = {p["slug"]: p for p in core.list_projects(shome) if p.get("slug")}
    meta, meta_error = _session_meta(home, [r.get("session_id") for r in rows if r.get("session_id")])
    threshold = float(getattr(core, "MIN_CONFIDENCE", 0.65))
    items: List[Dict[str, Any]] = []
    for row in rows:
        slug = row.get("project_slug")
        project = projects.get(slug) or {}
        target = project.get("primary_path") or (
            (project.get("folders") or [{}])[0].get("path") if project.get("folders") else None)
        confidence = row.get("confidence")
        low = float(confidence or 0) < threshold
        info = meta.get(row.get("session_id")) or {}
        items.append({
            "session_id": row.get("session_id"), "title": info.get("title") or "",
            "profile": info.get("profile") or "", "project_slug": slug,
            "project_name": project.get("name") or slug, "target_path": target,
            "current_cwd": info.get("cwd"), "confidence": confidence, "basis": _basis(row),
            "source": info.get("source"), "kanban": (str(info.get("source") or "") == KANBAN_SOURCE),
            "will_apply": not low, "will_sync_cwd": bool(sync_cwd and target and not low),
            "skip_reason": ("置信度 " + str(confidence) + " 低于阈值 " + str(threshold) + "，不会应用"
                            if low else ("项目没有可用目录，同步 cwd 会跳过这一条"
                                         if (sync_cwd and not target) else None))})
    skipped = sum(1 for i in items if not i["will_apply"])
    missing = sorted({i["project_slug"] for i in items if i["will_apply"] and not i["target_path"]})
    kanban = [i["session_id"] for i in items if i.get("kanban")]
    notes: List[str] = []
    if kanban:
        notes.append("硬约束：%d 条是看板任务会话（source=kanban），不得进入普通项目，"
                     "确认后也不会写入" % len(kanban))
    if meta_error:
        notes.append("会话标题缺失：" + meta_error)
    if not sync_cwd:
        notes.append("本次不同步 cwd：只写插件归属表，桌面端项目树不会变化")
    if sync_cwd and missing:
        notes.append("这些项目在 projects.db 里没有目录，同步 cwd 会跳过：" + "、".join(missing))
    if skipped:
        notes.append(str(skipped) + " 条置信度低于 " + str(threshold) + "，确认后也不会写入")
    return {"count": len(items), "will_apply": len(items) - skipped, "skipped_low_confidence": skipped,
            "sync_cwd": bool(sync_cwd), "min_confidence": threshold, "session_ids": session_ids,
            "missing_project_path": missing, "items": items, "kanban": kanban, "notes": notes}


@router.post("/autogroup/preview")
def post_preview(payload: Optional[Dict[str, Any]] = Body(None)) -> Any:
    """**预览**：零写入，200 直接回预览体（t_ac537221 新增，界面的「预览变更」用它）。

    为什么单独一条：``POST /autogroup/apply {confirm:false}`` 按契约回 **400 + 预览**，
    而前端门面 ``rest.js`` 把非 2xx 归一成一句错误消息 —— 预览的逐条明细在界面上就拿不到了。
    这条路由把同一份 ``_preview()`` 以 **200** 给出（仍然零写入），确认写库依旧只走
    ``POST /autogroup/apply {confirm:true}``。

    入参：``session_ids``（不传 = 全部待确认建议）、``sync_cwd``（是否把 cwd 同步到项目目录）。
    """
    home = _home()
    body = payload or {}
    raw_ids = body.get("session_ids")
    if raw_ids in (None, "", []):
        session_ids: Optional[List[str]] = None
    elif isinstance(raw_ids, list):
        session_ids = [str(i).strip() for i in raw_ids if str(i).strip()]
        if not session_ids:
            return _fail("invalid_session_ids", "session_ids 为空列表；不传表示全部待确认建议")
    else:
        return _fail("invalid_session_ids", "session_ids 必须是字符串数组")
    sync_cwd = body.get("sync_cwd") is True
    agent_raw = str(body.get("agent") or "").strip() or None
    try:
        if agent_raw:
            agent_name, shome = _scope(agent_raw, home)
        else:
            agent_name, shome = _scope(None, home)
    except ScopedWrite as exc:
        return _fail(exc.code, exc.message, extra=exc.extra)
    try:
        core = _load_core()
        preview = _preview(home, core, session_ids, sync_cwd, store_home=shome)
    except Exception as exc:  # noqa: BLE001
        return _fail("preview_failed", "生成预览失败：" + _describe(exc), status_code=500)
    notes = list(preview.get("notes") or [])
    notes.append("预览零写入：确认应用（POST /autogroup/apply {confirm:true}）才会真正写库")
    preview = dict(preview, agent=agent_name)
    return {"ok": True, "data": preview, "notes": notes}


@router.get("/autogroup/ai/model")
def get_ai_model(agent: str = Query("", description="以哪个 agent 的配置/凭据判定（空=配置里记的 agent）")) -> Any:
    """项目分组 AI 的 provider/model 配置（**读**）。

    返回：``config``（盘上原文 + 是否存在）、``resolved``（**实际生效**值 + 来源：
    ``module`` 本项目独立 / ``global`` 跟随全局 / ``none`` 都没有）、``providers``
    （该 agent 可选 provider，**密钥只有变量名 + 是否存在**）、``models``（当前 provider 的模型清单）、
    ``audit_path``（每次真实调用的审计行落在这里）。
    """
    home = _home()
    import autogroup_model as model_cfg
    target = str(agent or "").strip() or None
    data = model_cfg.describe(home, target)
    data["recent_calls"] = model_cfg.read_audit(home, limit=10)
    return {"ok": True, "data": data, "notes": data.get("notes") or []}


@router.put("/autogroup/ai/model")
def put_ai_model(payload: Dict[str, Any] = Body(...)) -> Any:
    """保存**某个 Agent 的**项目分组 AI provider/model（**写后回读落盘真值**）。

    入参：
    * ``agent``：写到哪个 Agent 名下。**给了** ⇒ 只动这个 agent 的声明，别的 agent 一个字节不碰；
      空/不传 ⇒ 写 ``global`` 段（所有未声明 agent 的回落层）。
    * ``mode=module`` ⇒ 必须给 ``provider`` + ``model``，且后端校验「这个 model 属于这个 provider」
      （不属于 ⇒ 400，报错可读）；
    * ``mode=global`` ⇒ 该 Agent 跟随它自己 ``config.yaml`` 的当前模型（不需要 provider/model）。

    绝不回显密钥：``providers`` 里只有 ``key_env`` / ``key_present``。
    """
    home = _home()
    import autogroup_model as model_cfg
    mode = str(payload.get("mode") or "").strip().lower() or model_cfg.MODE_GLOBAL
    if mode not in model_cfg.MODES:
        return _fail("invalid_mode", "mode 只能是 %s（收到 %r）"
                     % (" / ".join(model_cfg.MODES), mode), status_code=400)
    agent = str(payload.get("agent") or "").strip() or None
    provider = str(payload.get("provider") or "").strip() or None
    model = str(payload.get("model") or "").strip() or None
    if mode == model_cfg.MODE_MODULE:
        if not agent:
            return _fail("invalid_agent",
                         "独立配置必须点名 agent：provider/model 是按 agent 的凭据与 config.yaml 校验的，"
                         "没有任何 agent 的独立配置无从校验")
        if not provider or not model:
            return _fail("invalid_combination", "独立配置必须同时给 provider 和 model")
        check = model_cfg.validate(home, agent=agent, provider=provider, model=model)
        if not check.get("ok"):
            return _fail(check.get("code") or "invalid_combination",
                         str(check.get("message") or "provider/model 组合校验失败"),
                         status_code=400,
                         extra={"models": check.get("models") or [],
                                "provider": check.get("provider") or provider,
                                "agent": agent})
        provider, model = check["provider"], check["model"]
    saved = model_cfg.save(home, mode=mode, agent=agent,
                           provider=provider if mode == model_cfg.MODE_MODULE else None,
                           model=model if mode == model_cfg.MODE_MODULE else None,
                           actor=str(payload.get("actor") or "panel"))
    if not saved.get("ok"):
        return _fail(saved.get("code") or "save_failed", saved.get("message") or "保存失败",
                     status_code=500, extra={k: saved[k] for k in ("readback", "expected")
                                             if k in saved})
    # 回读的 agent：写的是谁就读谁；写 global 时读 global 段的生效值
    data = model_cfg.describe(home, agent)
    data["saved"] = {"agent": saved.get("agent"), "layer": saved.get("layer"),
                     "mode": mode, "provider": provider, "model": model,
                     "target": "global" if agent is None else "agent:" + agent}
    data["recent_calls"] = model_cfg.read_audit(home, limit=10)
    target_label = "global 段（未声明 Agent 的回落层）" if agent is None else "Agent " + agent
    return {"ok": True, "data": data,
            "notes": ["已回读盘上真值（auto-group/ai_model.json · %s）" % target_label,
                      "按 Agent 独立：这次只改了「%s」，其它 Agent 的声明未变动" % target_label,
                      "下次「用 AI 分析」就会用这一份配置；审计行写在 auto-group/ai_calls.jsonl"]}


@router.get("/autogroup/ai/model/options")
def get_ai_model_options(agent: str = Query("default", description="以哪个 agent 的配置/凭据列 provider"),
                         provider: str = Query("", description="给了就只回该 provider 的模型清单"),
                         live: str = Query("0", description="1 = 向上游 <base_url>/models 拉一次")) -> Any:
    """两级配置的选项来源（**只读复用** ``api_models``，不新写模型清单后端）。

    * 不传 ``provider`` ⇒ 回该 agent 的 provider 目录（凭据脱敏）；
    * 传 ``provider`` ⇒ 回该 provider 的模型清单（``source`` 说明来自 config 还是 remote）。
    """
    home = _home()
    import autogroup_model as model_cfg
    target = str(agent or "default").strip() or "default"
    if provider.strip():
        listing = model_cfg.models_for(home, target, provider.strip(),
                                      live=str(live).strip().lower() in ("1", "true", "yes", "on"))
        if not listing.get("ok"):
            return _fail(listing.get("code") or "invalid_provider",
                         listing.get("message") or "模型清单读取失败", status_code=400,
                         extra={"providers": listing.get("providers")})
        return {"ok": True, "data": listing, "notes": [
            "模型清单来源 %r（config=该 agent 的 providers.<p>.models；remote=上游 /models）"
            % listing.get("source"),
            "清单为空说明该 provider 在 config.yaml 里没写 models 段，模型名无法离线校验"]}
    opts = model_cfg.options(home, target)
    if not opts.get("ok"):
        return _fail(opts.get("code") or "invalid_agent", opts.get("message") or "provider 目录读取失败",
                     status_code=400)
    return {"ok": True, "data": opts, "notes": [
        "密钥只以 key_env（变量名）+ key_present（布尔）出现，取值一律不回显"]}


@router.get("/autogroup/ai/scope")
def get_ai_scope(agent: str = Query("", description="只看这个 profile 的候选（空=全机器）")) -> Any:
    """AI 候选口径（**不调模型**）：候选总数 / 各类排除数。

    与 ``POST /autogroup/scan {ai:true}`` 共用引擎同一个 ``limit=0`` 口径，
    所以界面上的「共 N 条」与真正处理时的候选集合必然一致。

    ``agent``（t_bffc584b）：传了就**整份口径按该 agent 收窄** —— ``candidate_session_ids``、
    ``total_available`` 与 ``excluded_kanban/hidden/source/archived/no_message`` 都只算这个 profile；
    ``excluded_applied/rejected`` 也按归属计数。其它 agent 的桌面可见会话数进 ``excluded_agent``。
    不传 = 全机器（与改前逐字段一致，只多 ``excluded_agent=0`` / ``agent_scoped=false`` 两个字段）。
    """
    home = _home()
    import autogroup_ai as engine
    target = str(agent or "").strip() or None
    scope = engine.candidate_scope(home, agents={target} if target else None)
    if not scope.get("ok"):
        return _fail("scope_failed", (scope.get("error") or {}).get("detail") or "统计失败",
                     status_code=503)
    scope = dict(scope)
    scope.pop("candidate_session_ids", None)
    scope["agent"] = target or "__all__"
    notes = [
        "口径 %s：只处理桌面端真实可见的普通对话（source 不属于 acp/kanban/oneshot/subagent/tool、"
        "hidden=0、archived 但 pinned 仍可见、message_count>=1）" % scope.get("scope_rule"),
        "total_available 只算「无归属 / 待确认」的会话；已 applied %d 条、rejected %d 条属正常排除"
        % (scope.get("excluded_applied", 0), scope.get("excluded_rejected", 0)),
    ]
    if target:
        notes.append(
            "按 agent 隔离：以上每个数都只统计 agent=%s 的会话；其它 agent 的桌面可见会话 %d 条"
            "（excluded_agent）不计入任何排除桶，两个 agent 的候选集不可能有交集"
            % (target, scope.get("excluded_agent", 0)))
    else:
        notes.append("未指定 agent：这是全机器口径（各 agent 会话合在一起算）")
    return {"ok": True, "data": scope, "notes": notes}


@router.get("/autogroup/plan/ops")
def get_plan_ops(limit: int = Query(50, description="返回条数，钳到 1..200")) -> Any:
    """计划应用记录（历史台账，只读）。

    t_ac537221：计划契约（``POST /autogroup/plan`` → ``apply {plan_id}``）已下线，
    这条读接口**保留**是因为 ``mode_ops.db`` 里已有历史 op，撤销要能查到它们
    （``POST /autogroup/undo {op_id}`` 仍可整批撤销历史 op）。
    """
    home = _home()
    import autogroup_modes as modes
    page = max(1, min(200, int(limit)))
    data = modes.list_ops(home, limit=page)
    return {"ok": True, "data": data,
            "notes": [] if data.get("ops") else ["没有历史计划应用记录（计划契约已下线）"]}


@router.post("/autogroup/apply")
def post_apply(payload: Dict[str, Any] = Body(...)) -> Any:
    """confirm≠true → 400 + 预览；confirm=true → 执行 → **回读**。

    这是**唯一**的写入路径（t_ac537221 收敛：旧的 ``plan_id`` 计划契约已删除，
    预览→确认→应用→撤销只有这一条）。

    入参：``confirm``（必须是字面 true）、``session_ids``（不传 = 全部待确认建议）、
    ``sync_cwd``（true 时把会话 cwd 同步到项目目录）。
    """
    home = _home()
    if payload.get("plan_id") or payload.get("mode"):
        return _fail("plan_flow_removed",
                     "计划契约（plan_id/mode）已下线：现在只有一条写入路径 —— "
                     "POST /autogroup/apply {confirm:true, session_ids?}；"
                     "全量重建请用 POST /autogroup/scan {scope:'all'} 重新扫描", status_code=400)
    confirm, sync_cwd = payload.get("confirm") is True, payload.get("sync_cwd") is True
    agent_raw = str(payload.get("agent") or "").strip() or None
    try:
        agent_name, shome = _scope(agent_raw, home)
    except ScopedWrite as exc:
        return _fail(exc.code, exc.message, extra=exc.extra)
    raw_ids = payload.get("session_ids")
    if raw_ids in (None, "", []):
        session_ids: Optional[List[str]] = None
    elif isinstance(raw_ids, list):
        session_ids = [str(i).strip() for i in raw_ids if str(i).strip()]
        if not session_ids:
            return _fail("invalid_session_ids", "session_ids 为空列表；不传表示全部待确认建议")
    else:
        return _fail("invalid_session_ids", "session_ids 必须是字符串数组")
    try:
        core = _load_core()
        preview = _preview(home, core, session_ids, sync_cwd, store_home=shome)
    except Exception as exc:  # noqa: BLE001
        return _fail("preview_failed", "生成预览失败：" + _describe(exc), status_code=500)

    if not confirm:
        return _fail(
            "confirmation_required",
            "未执行。确认前先看预览：" + str(preview["will_apply"]) + " 条会写入"
            + ("、并同步 " + str(sum(1 for i in preview["items"] if i["will_sync_cwd"])) + " 条会话的 cwd"
               if sync_cwd else "（不同步 cwd）") + "。确认后带 confirm=true 再发一次。",
            status_code=400, extra={"preview": preview})
    # 看板硬约束（t_53b48743）：命中一条就整批拒绝，绝不「部分写入」
    if preview.get("kanban"):
        hit = list(preview["kanban"])
        return _fail("kanban_excluded",
                     "看板任务会话不得归入普通项目（source=kanban）：%d 条" % len(hit),
                     status_code=400,
                     extra={"blocked_session_ids": hit, "preview": preview})
    if preview["count"] == 0:
        return _fail("nothing_to_apply", "没有匹配的待确认建议（session_ids 过滤后为空）",
                     status_code=400, extra={"preview": preview})
    if preview["will_apply"] == 0:
        return _fail("all_below_threshold", "筛选出的 " + str(preview["count"]) + " 条置信度都低于阈值 "
                     + str(preview["min_confidence"]), status_code=400, extra={"preview": preview})
    try:
        # 与 scan 的写建议互斥（WRITE_LOCK 注释里有实测证据）
        with WRITE_LOCK:
            result = core.apply_assignments(shome, min_confidence=preview["min_confidence"],
                                            sync_cwd=sync_cwd, only=session_ids)
    except Exception as exc:  # noqa: BLE001
        return _fail("apply_failed", "写入失败：" + _describe(exc), status_code=500)

    # ---- 回读（写操作自己的返回值不当成功证据）----
    try:
        summary = core.status_summary(shome)
        applied_rows, still = (list(core.load_suggestions(shome, "applied")),
                               list(core.load_suggestions(shome, "suggested")))
    except Exception as exc:  # noqa: BLE001
        return _fail("readback_failed", "写入已执行，但回读失败：" + _describe(exc),
                     status_code=500, extra={"write_result": result})
    batch_id = result.get("batch_id")
    in_batch = [r["session_id"] for r in applied_rows if r.get("batch_id") == batch_id]
    notes: List[str] = []
    if batch_id and not in_batch:
        notes.append("回读的 applied 里没有该批次的会话 —— 以归属表为准，请刷新确认")
    if result.get("missing_project_path"):
        notes.append("这些项目没有目录，同步 cwd 已跳过：" + "、".join(result["missing_project_path"]))
    if result.get("kanban_excluded"):
        notes.append("看板任务会话按硬约束未写入：%d 条" % len(result["kanban_excluded"]))
    return {"ok": True, "data": {
        "agent": agent_name, "batch_id": batch_id, "write_result": result,
        "readback": {"batch_sessions": in_batch, "batch_count": len(in_batch), "suggested": len(still),
                     "applied": summary.get("applied"), "rejected": summary.get("rejected"),
                     "by_project": summary.get("by_project")}, "preview": preview}, "notes": notes}


@router.post("/autogroup/undo")
def post_undo(payload: Dict[str, Any] = Body(...)) -> Any:
    """撤销一次写入 → 回读。

    两条路，按「这次写入是谁做的」分流：

    * ``op_id``（或该批次有 ``console_ops`` 台账）= **面板写操作** → 走
      :func:`_undo_panel_op`，逐会话把整行还原成写入前快照、cwd 还原成原值；
    * 否则 ``batch_id`` = 老的 ``apply`` 批次 → 保持 C8a 行为（``core.undo_batch``：
      suggested↔applied 互换 + cwd 还原）。这条路对 apply 本身就是**真还原**：
      apply 只把 suggested 改成 applied，撤销就是改回 suggested。
    """
    home = _home()
    op_id = str(payload.get("op_id") or "").strip()
    batch_id = str(payload.get("batch_id") or "").strip()
    agent_raw = str(payload.get("agent") or "").strip() or None
    try:
        agent_name, shome = _scope(agent_raw, home)
    except ScopedWrite as exc:
        return _fail(exc.code, exc.message, extra=exc.extra)
    if op_id:
        # t_5e74252e：先看「计划应用台账」（auto-group/mode_ops.db）。命中就整批撤销三库 +
        # 项目实体；没命中才落到面板写操作台账（老路径，行为不变）。
        try:
            import autogroup_modes as modes
            handled = modes.maybe_undo(home, op_id)
        except Exception:  # noqa: BLE001 - 计划台账坏了不许拖垮老撤销（退回面板台账路径）
            handled = None
        if handled is not None:
            return handled
    # 台账实现放在 dashboard/api_autogroup_panel.py（t_43c5fd05 阶段2）。这里**显式 import**：
    # 这两个名字以前在本文件里被调用却从未定义 ⇒ 只要带 op_id 就必 500 NameError。import 失败
    # 也返回结构化 503，而不是让 NameError/ImportError 变成栈。
    try:
        from api_autogroup_panel import _op_id_for_batch, _undo_panel_op
    except Exception as exc:  # noqa: BLE001
        return _fail("panel_unavailable", "面板写操作模块加载失败：" + _describe(exc), status_code=503)
    if op_id:
        return _undo_panel_op(shome, op_id)
    if not batch_id:
        return _fail("invalid_batch_id", "需要 batch_id 或 op_id")
    panel_op = _op_id_for_batch(shome, batch_id)
    if panel_op:
        return _undo_panel_op(shome, panel_op)
    try:
        core = _load_core()
        before_applied = [r["session_id"] for r in core.load_suggestions(shome, "applied")
                          if r.get("batch_id") == batch_id]
        if not before_applied:
            return _fail("unknown_batch", "该批次没有仍为 applied 的会话（批次号错或已撤销）：" + batch_id,
                         status_code=404)
        result = core.undo_batch(shome, batch_id)
        summary = core.status_summary(shome)
        still, applied_rows = (list(core.load_suggestions(shome, "suggested")),
                               list(core.load_suggestions(shome, "applied")))
    except Exception as exc:  # noqa: BLE001
        return _fail("undo_failed", "撤销失败：" + _describe(exc), status_code=500)
    suggested_ids, applied_ids = {r.get("session_id") for r in still}, {r.get("session_id") for r in applied_rows}
    back = [sid for sid in before_applied if sid in suggested_ids]
    still_applied = [sid for sid in before_applied if sid in applied_ids]
    notes = (["回读显示该批次仍有 " + str(len(still_applied)) + " 条 applied —— 撤销未完全生效"]
             if still_applied else [])
    return {"ok": True, "data": {
        "agent": agent_name, "batch_id": batch_id, "undo_result": result,
        "readback": {"undone_sessions": len(back), "expected": len(before_applied), "back_to_suggested": back,
                     "still_applied": still_applied, "cwd_restored": result.get("cwd_restored"),
                     "suggested": len(still), "applied": summary.get("applied")}}, "notes": notes}


@router.post("/autogroup/reject")
def post_reject(payload: Dict[str, Any] = Body(...)) -> Any:
    """忽略一条建议（进 rejected 表，下次扫描不再处理）→ 回读。"""
    home = _home()
    session_id = str(payload.get("session_id") or "").strip()
    reason = str(payload.get("reason") or "面板手动忽略").strip()[:200]
    if not session_id:
        return _fail("invalid_session_id", "需要 session_id")
    agent_raw = str(payload.get("agent") or "").strip() or None
    if agent_raw:
        try:
            agent_name, shome = _scope(agent_raw, home)
        except ScopedWrite as exc:
            return _fail(exc.code, exc.message, extra=exc.extra)
    else:
        # 没点名就按会话归属反查（宁可多扫一次库，也不拿根 home 写别人的库）
        agent_name = _agent_owning_session(session_id, home)
        if is_all_agent(agent_name):
            return _fail("agent_required",
                         "无法判定会话 %r 属于哪个 agent（不是桌面可见会话或跨库重复）："
                         "请显式传 agent 再忽略" % session_id,
                         extra={"session_id": session_id})
        shome = agent_home(agent_name, home)
    try:
        core = _load_core()
        core.reject_session(shome, session_id, reason)
        still = list(core.load_suggestions(shome, "suggested"))
        rejected, summary = core.rejected_ids(shome), core.status_summary(shome)
    except Exception as exc:  # noqa: BLE001
        return _fail("reject_failed", "忽略失败：" + _describe(exc), status_code=500)
    gone, in_rejected = all(r.get("session_id") != session_id for r in still), session_id in rejected
    if not (gone and in_rejected):
        return _fail("readback_failed", "忽略返回成功，但回读不一致："
                     + ("仍在待确认建议里；" if not gone else "")
                     + ("rejected 表里没有该会话" if not in_rejected else ""),
                     status_code=500, extra={"session_id": session_id})
    return {"ok": True, "data": {"agent": agent_name, "session_id": session_id, "reason": reason,
                                 "readback": {"suggested": len(still), "rejected_total": len(rejected),
                                              "status_rejected": summary.get("rejected")}}, "notes": []}

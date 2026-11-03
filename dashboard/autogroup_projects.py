# -*- coding: utf-8 -*-
"""hermes-console · 项目分组**真项目化**（t_53b48743）：项目实体 + 归属/cwd 一致性。

设计与既有模块的分工（**不重复实现**）：

* ``plugins/hermes-auto-group/core.py`` —— 判定/规则/归属表，本文件不碰判定。
* ``api_autogroup.py`` —— 只读查询 + 老 apply/undo，本文件复用 ``_home``/``_fail``/``_load_core``。
* ``api_autogroup_panel.py`` —— 面板写操作 + **回滚台账** ``console_ops``。本文件的对齐修复
  也写同一张台账（``kind='header'/'session'``），撤销走同一个 ``_undo_panel_op``，不另造第二套。

权威口径（用户 2026-10-30 定）：

1. **项目实体 = ``projects.db`` 的 ``projects`` 行**。显示名就是 ``projects.name``，权威目录就是
   ``projects.primary_path``。本文件**只增不改**：新增 ``project_attrs`` 侧表承载
   ``created_by`` / ``status`` / ``sort_order`` / ``color`` / ``note``。**不复制 name / path**，
   避免又造出第二份真相。
2. **归属对齐方向 = 「改 cwd 去对齐归属」**（``mode='fix_cwd'``，默认）。反向 ``mode='fix_slug'``
   保留但必须显式选，且必须先过看板硬约束。
3. **一致性判据 = ``sessions.cwd`` 与 ``projects.primary_path`` 逐字相等**（比"目录名相等"更严：
   验收要求目录名一致，这条同时满足）。
4. **目标目录不存在 ⇒ 停手报错**，绝不静默建空目录（``dir_missing``）。

跨库原子性：``state.db`` 是 WAL，SQLite **不支持**跨 WAL 附加库的原子提交，所以本文件不谎称
"同一个事务"。真实保证是：**先把写入前整行 + cwd 原值落台账，再写 cwd，cwd 写失败即中止、
归属不落；归属写失败则立刻把 cwd 补偿回原值**。撤销路径能逐条回到原值（含 cwd 原值）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import api_autogroup as base
import api_autogroup_panel as panel

#: ``project_attrs``：项目实体的**扩展属性**。主键 slug 与 ``projects.slug`` 一一对应。
#: 显示名/目录**不在这里**：权威仍是 ``projects.name`` / ``projects.primary_path``。
ATTRS_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_attrs (
  slug        TEXT PRIMARY KEY,
  created_by  TEXT NOT NULL DEFAULT 'manual',
  status      TEXT NOT NULL DEFAULT 'active',
  sort_order  INTEGER NOT NULL DEFAULT 0,
  color       TEXT,
  note        TEXT,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);
"""

#: 合法的 created_by / status 取值（写接口按这两组校验，不接受任意字符串）。
CREATED_BY = ("manual", "ai", "imported")
STATUSES = ("active", "dissolved", "archived")

#: 对齐方向。
MODE_FIX_CWD = "fix_cwd"      # 归属为准 → 改会话 cwd（用户定的口径，默认）
MODE_FIX_SLUG = "fix_slug"    # cwd 为准 → 改归属（反向，必须显式选）
MODES = (MODE_FIX_CWD, MODE_FIX_SLUG)

#: 看板任务来源。硬约束：这些会话不得进入普通项目。
KANBAN_SOURCE = "kanban"

ALIGN_MAX = 500


# ---------------------------------------------------------------------------
# 路径 / 连接
# ---------------------------------------------------------------------------
#
# **agent 维度（t_9ad02075）**：四层数据（会话库 / 项目库 / 归属库 / 规则）全都相对 home 算，
# 所以把 home 解析成「该 agent 的 home」四层就一起分开（解析点只有 ``base.agent_home``）。
# 本文件以前只有「根 home」一种口径 ⇒ ``agent=work`` 的面板拿小白的 17 个项目去匹配 work 的
# 会话，8/8 串组。现在两个库路径都接受 agent 维度：
#
#     agent 为空 / 'default' → ``<root>/projects.db``、``<root>/auto-group/assignments.db``
#     agent = <p>            → ``<root>/profiles/<p>/projects.db``、``…/auto-group/assignments.db``
#
# **没有库就是没有库**：不建目录、不建库、**绝不回落根库**（回落正是本卡要消灭的 bug）。
# agent='__all__' 没有单一库 ⇒ ``ValueError``，由 :func:`list_entities_for` 转成
# 「请先选定一个 agent」的空态，绝不把多个 agent 的项目拼成一份清单。

def projects_db(home: Path, agent: Optional[str] = None) -> Path:
    """项目库路径。

    ``agent`` 为 ``None``/空 ⇒ 原义 ``<home>/projects.db``（``home`` 已经是该 agent 的 home，
    既有调用点全部这样用，行为不变）；给了 ``agent`` ⇒ 由本函数解析到该 agent 的库。
    """
    return _db_path(home, agent, Path("projects.db"))


def store_db(home: Path, agent: Optional[str] = None) -> Path:
    """归属库（与 ``core.store_path`` 同址；本文件不 import core 以免影响只读路径）。"""
    return _db_path(home, agent, Path("auto-group") / "assignments.db")


def _db_path(home: Path, agent: Optional[str], relative: Path) -> Path:
    """``agent`` → 库路径。``__all__`` 抛 ``ValueError``（没有单一库），不猜。"""
    name = str(agent or "").strip()
    if not name:
        return Path(home) / relative
    return Path(base.agent_home(name, home)) / relative


def scope_of(root_home: Path, agent: Optional[str]) -> Dict[str, Any]:
    """``agent`` → 作用域描述（**只读**，不建任何目录/库）。

    返回 ``{agent, home, projects_db, store_db, has_project_db, has_store, projects_total}``。
    ``agent='__all__'`` ⇒ ``ValueError``：跨 agent 没有单一项目库。
    """
    root = Path(root_home)
    name = str(agent or "").strip() or base._load_core().DEFAULT_AGENT
    ahome = base.agent_home(name, root)   # __all__ 在这里 ValueError
    pdb = projects_db(root, name)
    sdb = store_db(root, name)
    if pdb != ahome / "projects.db" or sdb != ahome / "auto-group" / "assignments.db":
        raise RuntimeError("agent 库路径解析不一致（双重前缀？）：%s vs %s" % (pdb, ahome))
    total = None
    if pdb.is_file():
        try:
            total = len(list(base._load_core().list_projects(ahome)))
        except Exception:  # noqa: BLE001 - 项目数只用来写空态文案，读不到就报 null
            total = None
    return {"agent": name, "home": str(ahome), "projects_db": str(pdb), "store_db": str(sdb),
            "has_project_db": pdb.is_file(), "has_store": sdb.is_file(),
            "projects_total": total}


def empty_state(scope: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """空态 ``(empty_reason, 文案)``；有项目（或读不到）返回 ``None``。"""
    if not scope.get("has_project_db"):
        return ("no_project_db",
                "该 agent（%s）还没有项目库：%s 不存在。不会回落到根库（小白的项目不归它）"
                % (scope["agent"], scope["projects_db"]))
    if scope.get("projects_total") == 0:
        return ("no_projects",
                "该 agent（%s）还没有项目：%s 里 0 条项目行。不会回落到根库"
                % (scope["agent"], scope["projects_db"]))
    return None


def list_entities_for(root_home: Path, agent: Optional[str] = None) -> Dict[str, Any]:
    """按 agent 取项目实体清单（``/autogroup/entities`` 的唯一数据源）。

    返回 ``{agent, needs_agent, home, projects_db, store_db, projects, total, empty,
    empty_reason, message}``：

    * ``agent='__all__'`` ⇒ ``needs_agent=True`` + ``projects=[]`` + 「请先选定一个 agent」，
      **绝不**把多个 agent 的项目拼成一份清单（``cross_agent_merged`` 恒为 False）；
    * 该 agent 没有库/没有项目 ⇒ ``projects=[]`` + 明确空态文案，**不回落根库**。
    """
    core = base._load_core()
    name = str(agent or "").strip() or core.DEFAULT_AGENT
    if name == core.AGENT_ALL:
        return {"agent": core.AGENT_ALL, "needs_agent": True, "home": None,
                "projects_db": None, "store_db": None, "root": None, "projects": [], "total": 0,
                "empty": True, "empty_reason": "needs_agent", "cross_agent_merged": False,
                "message": "agent=__all__ 没有单一项目库：请先选定一个 agent（例如 default / work）"
                           "再看项目清单；本接口不会把多个 agent 的项目拼成一份列表"}
    scope = scope_of(root_home, name)
    ahome = Path(scope["home"])
    items = list_entities(ahome)
    for item in items:
        item["agent"] = name
    state = empty_state(scope)
    out = dict(scope)
    out.update({"projects": items, "total": len(items), "needs_agent": False,
                "cross_agent_merged": False, "empty": state is not None,
                "root": str(project_root(ahome)),   # 前端用它提示「新项目会建到哪」
                "empty_reason": state[0] if state else None,
                "message": state[1] if state else None})
    return out


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_rw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(conn: sqlite3.Connection, schema: str = "main") -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM " + schema + ".sqlite_master WHERE type='table'")}


def ensure_attrs(home: Path) -> sqlite3.Connection:
    """打开 ``projects.db`` 并补建 ``project_attrs``（只在**写**路径调用）。

    读路径一律不建表：``projects.db`` 是桌面端共享库，只读请求不许产生 schema 变更。
    """
    path = projects_db(home)
    if not path.is_file():
        raise FileNotFoundError("projects.db 不存在：" + str(path))
    conn = _connect_rw(path)
    try:
        conn.executescript(ATTRS_SCHEMA)
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


# ---------------------------------------------------------------------------
# 读：项目实体
# ---------------------------------------------------------------------------

def _dir_state(path: Optional[str]) -> Dict[str, Any]:
    """目录在不在 / 空不空 / 里面有什么（只读首层，不递归）。"""
    if not path:
        return {"exists": False, "empty": None, "entries": 0, "path": None}
    folder = Path(str(path))
    try:
        if not folder.is_dir():
            return {"exists": False, "empty": None, "entries": 0, "path": str(folder)}
        names = sorted(p.name for p in folder.iterdir())
        return {"exists": True, "empty": not names, "entries": len(names),
                "sample": names[:5], "path": str(folder)}
    except OSError as exc:
        return {"exists": None, "empty": None, "entries": None, "path": str(folder),
                "error": base._describe(exc)}


def _attr_rows(home: Path) -> Dict[str, Dict[str, Any]]:
    """slug → 扩展属性。表还没建、库不在 ⇒ 空字典（读路径绝不建表）。"""
    path = projects_db(home)
    if not path.is_file():
        return {}
    try:
        conn = _connect_ro(path)
    except sqlite3.Error:
        return {}
    try:
        if "project_attrs" not in _table_names(conn):
            return {}
        return {str(r["slug"]): dict(r) for r in conn.execute("SELECT * FROM project_attrs")}
    finally:
        conn.close()


def _assignment_stats(home: Path) -> Dict[str, Dict[str, int]]:
    """slug → {total, applied, suggested}。看板条数由 :func:`kanban_counts` 单独算（要读 state.db）。"""
    out: Dict[str, Dict[str, int]] = {}
    path = store_db(home)
    if not path.is_file():
        return out
    conn = _connect_ro(path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT project_slug, status, COUNT(*) AS n FROM assignments GROUP BY project_slug, status")]
    finally:
        conn.close()
    for row in rows:
        slug = str(row["project_slug"])
        bucket = out.setdefault(slug, {"total": 0, "applied": 0, "suggested": 0})
        bucket["total"] += int(row["n"])
        if row["status"] in ("applied", "suggested"):
            bucket[str(row["status"])] += int(row["n"])
    return out


def _project_created_at(home: Path) -> Dict[str, Any]:
    """slug → ``created_at``（projects.db 原始值）。读不到就是空表，不猜、不填 0。"""
    path = projects_db(home)
    if not path.is_file():
        return {}
    try:
        conn = _connect_ro(path)
    except sqlite3.Error:
        return {}
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
        if "created_at" not in cols:
            return {}
        return {str(r["slug"]): r["created_at"]
                for r in conn.execute("SELECT slug, created_at FROM projects")}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def list_entities(home: Path) -> List[Dict[str, Any]]:
    """项目实体清单：**一个项目一条**，同时回答「有没有目录 / 目录里有东西 / 多少会话」。

    ``created_by`` / ``status`` 缺失时返回 ``None`` 并在 ``attrs_present=False`` 里说明，
    不拿默认值冒充（``project_attrs`` 可能是本文件第一次写才建的）。
    """
    core = base._load_core()
    projects = list(core.list_projects(home))
    attrs = _attr_rows(home)
    created = _project_created_at(home)
    stats = _assignment_stats(home)
    kanban_by_slug = kanban_counts(home)
    out: List[Dict[str, Any]] = []
    for project in projects:
        slug = str(project.get("slug") or "")
        attr = attrs.get(slug) or {}
        stat = stats.get(slug, {"total": 0, "applied": 0, "suggested": 0})
        state = _dir_state(project.get("primary_path"))
        base_name = Path(str(project.get("primary_path"))).name if project.get("primary_path") else None
        out.append({
            "slug": slug,
            "name": project.get("name"),
            "description": project.get("description"),
            "color": attr.get("color") or project.get("color"),
            "path": project.get("primary_path"),
            "path_basename": base_name,
            "path_matches_slug": (base_name == slug),
            "folders": [f.get("path") for f in (project.get("folders") or [])],
            "created_at": project.get("created_at") if project.get("created_at") is not None
                          else created.get(slug),
            "created_by": attr.get("created_by"),
            "status": attr.get("status") or ("archived" if project.get("archived") else "active"),
            "sort_order": attr.get("sort_order"),
            "note": attr.get("note"),
            "attrs_present": bool(attr),
            "dir": state,
            "assignments": stat,
            "kanban_assignments": kanban_by_slug.get(slug, 0),
        })
    out.sort(key=lambda item: (item.get("sort_order") is None, item.get("sort_order") or 0,
                               str(item.get("slug"))))
    return out


def get_entity(home: Path, slug: str) -> Optional[Dict[str, Any]]:
    for item in list_entities(home):
        if item["slug"] == slug:
            return item
    return None


# ---------------------------------------------------------------------------
# 读：会话来源 / 看板硬约束
# ---------------------------------------------------------------------------

def session_meta(home: Path, ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """session_id → {cwd, source, title, exists}（只读 state.db，分批避开变量上限）。"""
    wanted = [i for i in dict.fromkeys(ids) if i]
    out: Dict[str, Dict[str, Any]] = {}
    if not wanted:
        return out
    path = Path(home) / "state.db"
    if not path.is_file():
        return out
    conn = _connect_ro(path)
    try:
        for start in range(0, len(wanted), 400):
            chunk = wanted[start:start + 400]
            sql = ("SELECT id, cwd, source, title FROM sessions WHERE id IN ("
                   + ",".join("?" * len(chunk)) + ")")
            for row in conn.execute(sql, chunk):
                out[str(row["id"])] = {"cwd": row["cwd"], "source": row["source"],
                                       "title": row["title"] or "", "exists": True}
    finally:
        conn.close()
    return out


def kanban_session_ids(home: Path, ids: List[str]) -> List[str]:
    """这批会话里哪些是看板任务（``sessions.source == 'kanban'``）。"""
    meta = session_meta(home, ids)
    return [sid for sid in dict.fromkeys(ids) if (meta.get(sid) or {}).get("source") == KANBAN_SOURCE]


def kanban_counts(home: Path) -> Dict[str, int]:
    """slug → 该项目下**属于看板任务**的已应用归属条数（脏数据盘点，本卡只报不清）。"""
    path = store_db(home)
    if not path.is_file():
        return {}
    conn = _connect_ro(path)
    try:
        pairs = [(str(r["project_slug"]), str(r["session_id"])) for r in conn.execute(
            "SELECT project_slug, session_id FROM assignments")]
    finally:
        conn.close()
    if not pairs:
        return {}
    meta = session_meta(home, [sid for _slug, sid in pairs])
    out: Dict[str, int] = {}
    for slug, sid in pairs:
        if (meta.get(sid) or {}).get("source") == KANBAN_SOURCE:
            out[slug] = out.get(slug, 0) + 1
    return out


def kanban_assignment_ids(home: Path) -> set:
    path = store_db(home)
    if not path.is_file():
        return set()
    conn = _connect_ro(path)
    try:
        ids = [str(r["session_id"]) for r in conn.execute("SELECT session_id FROM assignments")]
    finally:
        conn.close()
    meta = session_meta(home, ids)
    return {sid for sid in ids if (meta.get(sid) or {}).get("source") == KANBAN_SOURCE}


# ---------------------------------------------------------------------------
# 写：属性行 / 目录 / projects 表
# ---------------------------------------------------------------------------

def project_root(home: Path) -> Path:
    """项目目录根。**从既有项目的 primary_path 反推**，不用 ``core.project_root``。

    ``core.project_root`` 返回 ``<home>/projects``（即 ``D:\\hermes-data\\hermes\\projects``），
    但真实 17 个项目的 ``primary_path`` 全在 ``D:\\hermes-data\\projects``（**home 的兄弟目录**）。
    照 core 的算法建新项目，会把新项目建到另一个根里，从此两套目录并存 —— 正是卡里
    「目录存在（空）文案与实际位置对不上」的根因。这里以既有项目的实际根为准，
    一个项目都没有时才退回 ``<home>/projects``。
    """
    parents: Dict[str, int] = {}
    path = projects_db(home)
    if path.is_file():
        try:
            conn = _connect_ro(path)
        except sqlite3.Error:
            conn = None
        if conn is not None:
            try:
                for row in conn.execute(
                        "SELECT primary_path FROM projects WHERE primary_path IS NOT NULL"
                        " AND primary_path != ''"):
                    parent = Path(str(row["primary_path"])).parent
                    parents[str(parent)] = parents.get(str(parent), 0) + 1
            finally:
                conn.close()
    if parents:
        # 多数决：绝大多数项目同根时以它为准（零散 outlier 不改判）
        return Path(max(parents.items(), key=lambda kv: (kv[1], len(kv[0])))[0])
    return Path(home) / "projects"


def upsert_attrs(conn: sqlite3.Connection, slug: str, *, created_by: Optional[str] = None,
                 status: Optional[str] = None, sort_order: Optional[int] = None,
                 color: Optional[str] = None, note: Optional[str] = None,
                 now: Optional[float] = None) -> None:
    """插入或更新 ``project_attrs``。只写显式给出的字段（None = 不动）。"""
    stamp = now or time.time()
    row = conn.execute("SELECT slug FROM project_attrs WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO project_attrs(slug, created_by, status, sort_order, color, note,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (slug, created_by or "manual", status or "active", sort_order or 0, color, note,
             stamp, stamp))
        return
    sets, params = [], []
    for column, value in (("created_by", created_by), ("status", status),
                          ("sort_order", sort_order), ("color", color), ("note", note)):
        if value is not None:
            sets.append(column + " = ?")
            params.append(value)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.extend([stamp, slug])
    conn.execute("UPDATE project_attrs SET " + ", ".join(sets) + " WHERE slug = ?", params)


def dir_probe(path: Path) -> Dict[str, Any]:
    """目录现状：不存在 / 存在且空 / 存在且非空（含首层样本名）。"""
    try:
        if not path.exists():
            return {"exists": False, "is_dir": False, "empty": None, "entries": 0, "path": str(path)}
        if not path.is_dir():
            return {"exists": True, "is_dir": False, "empty": None, "entries": None,
                    "path": str(path), "error": "路径已存在但不是目录"}
        with os.scandir(path) as it:
            names = sorted(entry.name for entry in it)
        return {"exists": True, "is_dir": True, "empty": not names, "entries": len(names),
                "sample": names[:8], "path": str(path)}
    except OSError as exc:
        return {"exists": None, "is_dir": None, "empty": None, "entries": None,
                "path": str(path), "error": base._describe(exc)}


def dir_is_empty(path: Path) -> bool:
    try:
        with os.scandir(path) as it:
            return next(it, None) is None
    except OSError:
        return False

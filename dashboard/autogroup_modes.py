# -*- coding: utf-8 -*-
"""两种分类模式（A 只归类未分组 / B 全量重建体系）+ 预览→确认→应用 + 整批撤销。

**为什么要单独一个模块**：老的 ``POST /autogroup/apply`` 语义是「把已经落库的 suggested 行
改成 applied」，它天然是增量的；用户要的是**显式的两种模式**，其中 B 会**抛弃旧体系**（删/改旧
项目与旧归属），属于破坏性操作，必须能整批回退。两套语义混在一个函数里会互相污染，所以判定
与执行都放在这里，HTTP 入口仍挂在 ``api_autogroup`` 的路由上（见该文件底部的委托）。

模式契约
--------
``mode="ungrouped_only"``（A）
    * 候选 = 可见会话 ∩ **没有 applied 归属行**
    * 已分组会话**一条都不动**（不写归属、不写 cwd）
    * 目标优先落在**现有项目**；确有必要才提议新项目，且新项目单独列在 ``new_projects``，
      只有 plan 请求里带 ``accept_new_projects=true`` 才会真的建
``mode="rebuild_all"``（B）
    * 候选 = 全部可见会话（已分组 + 未分组）
    * 输出 = **一套全新的项目结构 + 全量归属映射**（不是对旧分组做微调）
    * 旧项目在新体系里没有会话 ⇒ 记为 ``dropped``，应用时删实体（目录保留），
      删之前把项目实体与全部旧归属行完整存进 ``mode_ops.db``，撤销能逐字段还原

三阶段
------
``plan()``    纯只读：不写 assignments / projects / 不建快照 / 不建目录。计划只存在进程内存
              （``_PLANS``），所以「零写入」是字面意义上的零写入；进程重启后 plan_id 失效
              （⇒ 404 ``unknown_plan``，前端重新预览即可）。
``apply_plan()`` 必须带 ``plan_id`` + ``confirm=true``；先按指纹核对「预览之后数据没变」，
              再**按字节**备份三库到 ``<home>/auto-group/snapshots/<op_id>/``，然后写入，
              最后**逐条回读**；任何一条不符 ⇒ 整体回滚并返回 ``verify_mismatch``。
``undo_op()``  一个 plan 应用 = 一个 ``op_id``：逐字段还原归属行、cwd、被删的项目实体，
              然后把三库内容哈希与应用前比对；一致时再把快照字节拷回去（三库逐字节回到应用前）。

台账（第 4 个文件，**刻意不放在三库之一**）
------------------------------------------
``<home>/auto-group/mode_ops.db``：``ops`` / ``op_sessions`` / ``op_projects``。撤销记录放这里，
三库才能被快照字节「原样拷回」而不丢审计（如果审计写进 assignments.db，就不可能逐字节还原）。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MODE_UNGROUPED_ONLY = "ungrouped_only"
MODE_REBUILD_ALL = "rebuild_all"
MODES = (MODE_UNGROUPED_ONLY, MODE_REBUILD_ALL)
MODE_LABELS = {MODE_UNGROUPED_ONLY: "只归类未分组（A）", MODE_REBUILD_ALL: "全量重建体系（B）"}

PLAN_TTL_SECONDS = 900          # 预览有效期：过期必须重新预览（409 plan_expired）
PLAN_MAX_KEEP = 40              # 内存里最多留多少个计划
PLAN_DEFAULT_LIMIT = 200
PLAN_MAX_LIMIT = 500
SNAPSHOT_DIRNAME = "snapshots"
LEDGER_NAME = "mode_ops.db"
UNCATEGORIZED_SLUG = "uncategorized"
UNCATEGORIZED_NAME = "未归类"
PLAN_SOURCE = "plan"            # 归属行 source 字段（区别于 rule/llm/panel）

#: 三库里被本模块读写的（相对 home）。顺序固定，哈希/快照都按这个顺序。
TRACKED_DBS = ("state.db", "projects.db", "auto-group/assignments.db")
#: 内容哈希覆盖哪些表。``state.db`` 只取 ``sessions``：那是 2GB 级活库，把 messages / FTS
#: 全表拉出来算哈希要几十秒，而本模块只会改 ``sessions.cwd``。小库（projects/assignments）
#: 全表参与，撤销时据此判断「有没有别人的改动」。
CONTENT_TABLES: Dict[str, Optional[Tuple[str, ...]]] = {
    "state.db": ("sessions",),
    "projects.db": None,
    "auto-group/assignments.db": None,
}
#: 超过这个大小就不算文件字节哈希（2GB 的 state.db 每次 sha256 要好几秒），改用 size+mtime。
FILE_HASH_MAX_BYTES = 64 * 1024 * 1024
#: 撤销时允许**整库字节回填**的库。``state.db`` 是活的会话库（别的 agent 随时在写 messages），
#: 整库拷回会连别人的新写入一起抹掉 ⇒ 只做行级还原（sessions.cwd），绝不做字节回填。
BYTE_RESTORE_DBS = ("projects.db", "auto-group/assignments.db")
ASSIGN_COLUMNS = ("session_id", "project_slug", "confidence", "reason", "source", "status",
                  "batch_id", "applied_at", "updated_at")

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS ops (
  op_id TEXT PRIMARY KEY,
  plan_id TEXT,
  mode TEXT,
  applied_at REAL,
  undone INTEGER NOT NULL DEFAULT 0,
  undone_at REAL,
  snapshot_dir TEXT,
  pre_hashes TEXT,
  post_hashes TEXT,
  summary TEXT,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS op_sessions (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  op_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  row_before TEXT,
  cwd_before TEXT,
  cwd_known INTEGER NOT NULL DEFAULT 0,
  cwd_written INTEGER NOT NULL DEFAULT 0,
  slug_before TEXT,
  slug_after TEXT,
  outcome TEXT
);
CREATE TABLE IF NOT EXISTS op_projects (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  op_id TEXT NOT NULL,
  slug TEXT NOT NULL,
  action TEXT NOT NULL,
  project_json TEXT,
  folders_json TEXT,
  attrs_json TEXT,
  dir_created INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mode_ops_undone ON ops(undone);
CREATE INDEX IF NOT EXISTS idx_mode_op_sessions ON op_sessions(op_id);
CREATE INDEX IF NOT EXISTS idx_mode_op_projects ON op_projects(op_id);
"""

_PLANS: Dict[str, Dict[str, Any]] = {}
_PLANS_LOCK = threading.Lock()
_APPLY_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _base():
    """懒 import ``api_autogroup``：本模块被它 import，模块级 import 会成环。"""
    import api_autogroup as base  # noqa: PLC0415
    return base


def _core():
    return _base()._load_core()


def _fail(code: str, message: str, status_code: int = 400, extra: Optional[Dict[str, Any]] = None):
    return _base()._fail(code, message, status_code=status_code, extra=extra)


def _describe(exc: BaseException) -> str:
    return type(exc).__name__ + "：" + str(exc)


def _new_id(prefix: str) -> str:
    return prefix + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _db_path(home: Path, rel: str) -> Path:
    return Path(home) / rel


def file_hash(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _table_names(conn: sqlite3.Connection) -> List[str]:
    return [str(r[0]) for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def content_hash(path: Path, tables: Optional[Tuple[str, ...]] = None) -> Optional[str]:
    """库的**内容**哈希：表结构 + 行（按规范 JSON 排序），与 sqlite 页布局无关。

    ``tables`` 指定只哈希哪几张表（``None`` = 全部）。``state.db`` 传 ``("sessions",)``：
    2GB 级活库里 messages/FTS 全表哈希要几十秒，而本模块只会改 ``sessions.cwd``。
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=15)
    except sqlite3.Error:
        return None
    try:
        conn.row_factory = sqlite3.Row
        names = _table_names(conn)
        wanted = [n for n in names if tables is None or n in tables]
        digest = hashlib.sha256()
        for name in wanted:
            row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name = ?",
                               (name,)).fetchone()
            digest.update(("T:" + name + ":" + str(row[0] if row else "") + "\n").encode("utf-8"))
            rows = []
            for data in conn.execute('SELECT * FROM "' + name + '"'):
                rows.append(json.dumps(dict(data), ensure_ascii=False, sort_keys=True, default=str))
            rows.sort()
            for line in rows:
                digest.update((line + "\n").encode("utf-8"))
        return digest.hexdigest()
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def db_hashes(home: Path) -> Dict[str, Dict[str, Any]]:
    """三库的 ``{file: 字节哈希(过大则为 None), content: 内容哈希, size, mtime_ns, exists}``。"""
    out: Dict[str, Dict[str, Any]] = {}
    for rel in TRACKED_DBS:
        path = _db_path(home, rel)
        info: Dict[str, Any] = {"exists": path.is_file(), "file": None, "content": None,
                                "size": None, "mtime_ns": None, "file_skipped": None}
        if info["exists"]:
            try:
                stat = path.stat()
                info["size"], info["mtime_ns"] = stat.st_size, stat.st_mtime_ns
            except OSError:
                pass
            if info["size"] is not None and info["size"] <= FILE_HASH_MAX_BYTES:
                info["file"] = file_hash(path)
            else:
                info["file_skipped"] = "too_large"
            info["content"] = content_hash(path, CONTENT_TABLES.get(rel))
        out[rel] = info
    return out


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_rw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# 候选集：可见会话 / 已分组 / 已忽略
# ---------------------------------------------------------------------------

def visible_sessions(home: Path) -> Dict[str, Any]:
    """全部可见会话（不含看板）。

    口径与 ``core.list_sessions`` 一致（``message_count >= 2``、未归档、来源不在
    ``EXCLUDED_SOURCES``），但**显式**把 ``source='kanban'`` 单独拎出来（硬约束：
    看板任务会话不进 A 也不进 B 的候选集），并保留 cwd/source 供 UI 展示。
    """
    home = Path(home)
    path = home / "state.db"
    out: Dict[str, Any] = {"sessions": [], "kanban": [], "excluded_sources": [],
                           "error": None, "total_rows": 0}
    if not path.is_file():
        out["error"] = "state.db 不存在：" + str(path)
        return out
    core = _core()
    excluded = set(getattr(core, "EXCLUDED_SOURCES", ()) or ())
    try:
        conn = _connect_ro(path)
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, title, display_name, source, cwd, message_count, profile_name,"
                " last_activity_at FROM sessions WHERE archived = 0 AND message_count >= 2"
                " ORDER BY COALESCE(last_activity_at, started_at) DESC")]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        out["error"] = "读 state.db 失败：" + _describe(exc)
        return out
    out["total_rows"] = len(rows)
    for row in rows:
        source = str(row.get("source") or "")
        if source == "kanban":
            out["kanban"].append(row["id"])
            continue
        if source in excluded:
            out["excluded_sources"].append({"session_id": row["id"], "source": source})
            continue
        row["title"] = row.get("title") or row.get("display_name") or ""
        out["sessions"].append(row)
    return out


def applied_map(home: Path) -> Dict[str, Dict[str, Any]]:
    """session_id → 当前 applied 归属行（没有归属库就是空表）。"""
    core = _core()
    path = core.store_path(home)
    if not path.is_file():
        return {}
    try:
        conn = _connect_ro(path)
        try:
            return {str(r["session_id"]): dict(r) for r in conn.execute(
                "SELECT * FROM assignments WHERE status = 'applied'")}
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


def _assign_counts_by_slug(home: Path) -> Dict[str, int]:
    core = _core()
    path = core.store_path(home)
    if not path.is_file():
        return {}
    try:
        conn = _connect_ro(path)
        try:
            return {str(r[0]): int(r[1]) for r in conn.execute(
                "SELECT project_slug, COUNT(*) FROM assignments GROUP BY project_slug")}
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


# ---------------------------------------------------------------------------
# 分类（确定性、无模型调用）
# ---------------------------------------------------------------------------

def _cwd_basename(cwd: Optional[str]) -> Optional[str]:
    if not cwd:
        return None
    text = str(cwd).replace("/", "\\").rstrip("\\")
    return text.split("\\")[-1] if text else None


#: 这些目录名不是「项目」：按 cwd 聚类时直接跳过，免得给用户家目录 / 临时目录造出项目。
CWD_NOISE = {"users", "administrator", "public", "default", "tmp", "temp", "appdata", "windows",
             "programdata", "downloads", "desktop", "documents", "onedrive", "home", "projects",
             "workspace", "work", "src", "repos", "code", "hermes", "hermes-data"}


def _is_noise_dir(base_name: Optional[str]) -> bool:
    return bool(base_name) and str(base_name).strip().lower() in CWD_NOISE


def classify(text: str, session: Dict[str, Any], projects: Dict[str, Dict[str, Any]],
             rules: Dict[str, List[str]]) -> Dict[str, Any]:
    """给一条会话判一个「聚类目标」。**不做 LLM**（dashboard 进程没有 agent ctx）。

    顺序：关键词规则 → 标题/首条消息里出现项目 slug 或显示名 → 工作目录名。
    返回 ``{slug, name, confidence, reason, source, known}``；判不出来 ``slug=None``。
    """
    core = _core()
    text = text or ""
    verdict = None
    try:
        verdict = core.classify_by_rules(text, rules)
    except Exception:  # noqa: BLE001 - 规则坏了不该拖垮预览
        verdict = None
    if verdict and verdict.get("project_slug"):
        slug = str(verdict["project_slug"])
        return {"slug": slug, "name": (projects.get(slug) or {}).get("name") or slug,
                "confidence": verdict.get("confidence"), "reason": verdict.get("reason") or "规则命中",
                "source": "rule", "known": slug in projects}
    low = text.lower()
    if low.strip():
        for slug in sorted(projects):
            project = projects[slug]
            name = str(project.get("name") or "")
            if (slug and slug.lower() in low) or (name and name.lower() in low):
                return {"slug": slug, "name": name or slug, "confidence": 0.7,
                        "reason": "标题/首条消息提到项目 " + (name or slug),
                        "source": "match", "known": True}
    base_name = _cwd_basename(session.get("cwd"))
    if base_name and not _is_noise_dir(base_name):
        try:
            slug = core.slugify(base_name)
        except Exception:  # noqa: BLE001
            slug = None
        if slug:
            return {"slug": slug, "name": (projects.get(slug) or {}).get("name") or base_name,
                    "confidence": 0.6, "reason": "按工作目录名聚类（" + base_name + "）",
                    "source": "cwd", "known": slug in projects}
    return {"slug": None, "name": None, "confidence": None, "reason": "没有可用线索（标题/首条消息/目录都判不出来）",
            "source": "none", "known": False}


def _project_path(project: Optional[Dict[str, Any]]) -> Optional[str]:
    if not project:
        return None
    path = project.get("primary_path")
    if path:
        return str(path)
    folders = project.get("folders") or []
    return str(folders[0]["path"]) if folders else None


# ---------------------------------------------------------------------------
# 指纹：预览与写入之间数据漂移检测
# ---------------------------------------------------------------------------

def fingerprint(home: Path) -> str:
    """三库中与「候选集/归属/项目」相关部分的规范摘要。只读。"""
    home = Path(home)
    core = _core()
    digest = hashlib.sha256()
    state = home / "state.db"
    if state.is_file():
        try:
            conn = _connect_ro(state)
            try:
                rows = [dict(r) for r in conn.execute(
                    "SELECT id, source, cwd, message_count, archived,"
                    " COALESCE(last_activity_at, started_at) AS act FROM sessions ORDER BY id")]
            finally:
                conn.close()
            for row in rows:
                digest.update(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                         default=str).encode("utf-8"))
        except sqlite3.Error as exc:
            digest.update(("state-error:" + _describe(exc)).encode("utf-8"))
    else:
        digest.update(b"state-missing")
    store = core.store_path(home)
    if store.is_file():
        try:
            conn = _connect_ro(store)
            try:
                names = set(_table_names(conn))
                if "assignments" in names:
                    for row in conn.execute(
                            "SELECT session_id, project_slug, status, batch_id, updated_at"
                            " FROM assignments ORDER BY session_id"):
                        digest.update(json.dumps(dict(row), ensure_ascii=False, sort_keys=True,
                                                 default=str).encode("utf-8"))
                if "rejected" in names:
                    for row in conn.execute("SELECT session_id FROM rejected ORDER BY session_id"):
                        digest.update(("rej:" + str(row[0])).encode("utf-8"))
            finally:
                conn.close()
        except sqlite3.Error as exc:
            digest.update(("store-error:" + _describe(exc)).encode("utf-8"))
    else:
        digest.update(b"store-missing")
    projects_db = home / "projects.db"
    if projects_db.is_file():
        try:
            conn = _connect_ro(projects_db)
            try:
                names = set(_table_names(conn))
                if "projects" in names:
                    for row in conn.execute(
                            "SELECT id, slug, name, primary_path, archived FROM projects ORDER BY slug"):
                        digest.update(json.dumps(dict(row), ensure_ascii=False, sort_keys=True,
                                                 default=str).encode("utf-8"))
            finally:
                conn.close()
        except sqlite3.Error as exc:
            digest.update(("projects-error:" + _describe(exc)).encode("utf-8"))
    else:
        digest.update(b"projects-missing")
    return "sha256:" + digest.hexdigest()


# ---------------------------------------------------------------------------
# 生成计划（纯只读）
# ---------------------------------------------------------------------------

def _empty_plan_error(code: str, message: str, status_code: int = 400, extra=None):
    return _fail(code, message, status_code=status_code, extra=extra)


def plan(home: Path, payload: Optional[Dict[str, Any]]) -> Any:
    """``POST /autogroup/plan``：生成计划。**零写入**（不建快照、不建目录、不写三库）。"""
    payload = dict(payload or {})
    home = Path(home)
    mode = str(payload.get("mode") or "").strip()
    if mode not in MODES:
        return _empty_plan_error(
            "invalid_mode",
            "mode 只能是 " + " / ".join(MODES) + "（A=只归类未分组，B=全量重建体系），收到：" + repr(mode),
            extra={"modes": list(MODES), "labels": MODE_LABELS})
    try:
        limit = int(payload.get("limit") or PLAN_DEFAULT_LIMIT)
    except (TypeError, ValueError):
        return _empty_plan_error("invalid_limit", "limit 必须是整数")
    limit = max(1, min(PLAN_MAX_LIMIT, limit))
    propose_new = payload.get("propose_new_projects") is not False
    accept_new = payload.get("accept_new_projects") is True
    #: 与既有写路径同口径：默认同步 cwd（归属与 cwd 必须一致），显式 false 才只改归属表。
    sync_cwd = payload.get("sync_cwd") is not False

    try:
        core = _core()
        projects = {str(p["slug"]): p for p in core.list_projects(home) if p.get("slug")}
        rules = core.load_rules(home)
        applied = applied_map(home)
        rejected = set(core.rejected_ids(home) or set())
        visible = visible_sessions(home)
    except Exception as exc:  # noqa: BLE001
        return _empty_plan_error("plan_failed", "生成计划失败：" + _describe(exc), status_code=500)
    if visible.get("error"):
        return _empty_plan_error("state_unavailable", str(visible["error"]), status_code=503)

    sessions = [s for s in visible["sessions"] if s["id"] not in rejected]
    rejected_skipped = [s["id"] for s in visible["sessions"] if s["id"] in rejected]
    truncated = len(sessions) > limit
    pool = sessions[:limit]
    if mode == MODE_UNGROUPED_ONLY:
        candidates = [s for s in pool if s["id"] not in applied]
        skipped_grouped = [s["id"] for s in pool if s["id"] in applied]
    else:
        candidates = list(pool)
        skipped_grouped = []

    items: List[Dict[str, Any]] = []
    proposals: Dict[str, Dict[str, Any]] = {}
    clusters: Dict[str, List[str]] = {}
    pending_new: Dict[str, Dict[str, Any]] = {}
    unmatched: List[Dict[str, Any]] = []
    for session in candidates:
        sid = session["id"]
        text = str(session.get("title") or "")
        try:
            body = core.first_user_text(home, sid) or ""
        except Exception:  # noqa: BLE001 - 首条消息读不到就只按标题判
            body = ""
        verdict = classify((text + " " + body).strip(), session, projects, rules)
        row = applied.get(sid) or {}
        from_slug = row.get("project_slug")
        item: Dict[str, Any] = {
            "session_id": sid, "title": session.get("title") or "",
            "source": session.get("source"), "cwd_before": session.get("cwd"),
            "from_slug": from_slug, "to_slug": None, "change": "unmatched",
            "will_write": False, "confidence": verdict.get("confidence"),
            "basis": {"source": verdict.get("source"), "reason": verdict.get("reason")},
            "new_project": False, "cwd_target": None, "sync_cwd": False,
        }
        slug = verdict.get("slug")
        if not slug:
            unmatched.append({"session_id": sid, "title": item["title"],
                              "why": verdict.get("reason") or "判不出目标项目"})
            items.append(item)
            continue
        known = slug in projects
        if mode == MODE_UNGROUPED_ONLY and not known:
            # A：目标必须落在现有项目；新建只能「提议」，等确认
            if propose_new:
                pending_new.setdefault(slug, {"slug": slug, "name": verdict.get("name") or slug,
                                              "reason": verdict.get("reason"), "session_ids": []}
                                       )["session_ids"].append(sid)
                item["change"] = "pending_new_project"
                item["basis"]["reason"] = (verdict.get("reason") or "") + "；现有项目里没有 " + slug + \
                                          "，列为新建提议（等确认）"
            items.append(item)
            continue
        target_path = _project_path(projects.get(slug))
        if not known:
            target_path = str(_project_root(home) / slug)
        item["to_slug"] = slug
        item["new_project"] = not known
        item["cwd_target"] = target_path
        item["sync_cwd"] = bool(sync_cwd and target_path)
        if from_slug and from_slug == slug:
            item["change"] = "unchanged"
        elif from_slug:
            item["change"] = "moved"
            item["will_write"] = True
        else:
            item["change"] = "assigned"
            item["will_write"] = True
        clusters.setdefault(slug, []).append(sid)
        items.append(item)

    # 新建项目清单：B 一律要建（重建体系）；A 只有在 accept_new_projects 时才建
    new_projects: List[Dict[str, Any]] = []
    for slug in sorted(clusters):
        if slug in projects:
            continue
        project = projects.get(slug) or {}
        new_projects.append({
            "slug": slug, "name": project.get("name") or slug,
            "path": str(_project_root(home) / slug),
            "session_ids": clusters[slug], "session_count": len(clusters[slug]),
            "source": "rebuild" if mode == MODE_REBUILD_ALL else "proposal",
            "will_create": bool(mode == MODE_REBUILD_ALL or accept_new),
        })
    if mode == MODE_UNGROUPED_ONLY:
        for slug in sorted(pending_new):
            entry = pending_new[slug]
            new_projects.append({
                "slug": slug, "name": entry["name"],
                "path": str(_project_root(home) / slug),
                "session_ids": entry["session_ids"], "session_count": len(entry["session_ids"]),
                "source": "proposal", "will_create": bool(accept_new),
                "reason": entry.get("reason"),
            })
            for item in items:
                if item["session_id"] in entry["session_ids"] and accept_new:
                    item["to_slug"] = slug
                    item["new_project"] = True
                    item["cwd_target"] = str(_project_root(home) / slug)
                    item["sync_cwd"] = bool(sync_cwd)
                    item["change"] = "assigned"
                    item["will_write"] = True
    if not propose_new:
        new_projects = [p for p in new_projects if p["source"] == "rebuild"]

    # B：旧项目里在新体系中没有会话的 ⇒ 抛弃（truncated 时不许删，避免误伤未考量的会话）
    dropped: List[Dict[str, Any]] = []
    kept = sorted(set(clusters) & set(projects))
    if mode == MODE_REBUILD_ALL:
        counts = _assign_counts_by_slug(home)
        for slug in sorted(set(projects) - set(clusters)):
            dropped.append({
                "slug": slug, "name": (projects.get(slug) or {}).get("name") or slug,
                "assignments_now": counts.get(slug, 0),
                "will_delete": bool(not truncated),
                "blocked_reason": ("候选集被 limit 截断，不敢删旧项目（可能有未考量的会话）"
                                   if truncated else None),
            })

    changes = {
        "assign": sum(1 for i in items if i["will_write"] and not i["new_project"]),
        "move": sum(1 for i in items if i["change"] == "moved"),
        "unchanged": sum(1 for i in items if i["change"] == "unchanged"),
        "new_project_assignments": sum(1 for i in items if i["will_write"] and i["new_project"]),
        "pending_new_project": sum(1 for i in items if i["change"] == "pending_new_project"),
        "unmatched": len(unmatched),
        "new_projects": len([p for p in new_projects if p["will_create"]]),
        "dropped_projects": len([d for d in dropped if d["will_delete"]]),
        "will_write": sum(1 for i in items if i["will_write"]),
    }

    plan_id = _new_id("plan-")
    now = time.time()
    expires_at = now + PLAN_TTL_SECONDS
    stored = {
        "plan_id": plan_id, "mode": mode, "home": str(home), "created_at": now,
        "expires_at": expires_at, "fingerprint": fingerprint(home),
        "accept_new_projects": accept_new, "truncated": truncated, "limit": limit,
        "sync_cwd": sync_cwd,
        "items": items, "new_projects": new_projects, "dropped": dropped,
        "candidates": [s["id"] for s in candidates], "changes": changes,
    }
    with _PLANS_LOCK:
        _PLANS[plan_id] = stored
        for key in sorted(_PLANS, key=lambda k: _PLANS[k].get("created_at") or 0)[
                :max(0, len(_PLANS) - PLAN_MAX_KEEP)]:
            _PLANS.pop(key, None)

    notes: List[str] = [MODE_LABELS[mode] + "：候选 %d 条，计划写入 %d 条"
                        % (len(candidates), changes["will_write"])]
    if mode == MODE_UNGROUPED_ONLY:
        notes.append("模式 A 硬约束：已分组会话一条都不动（本次跳过的已分组会话 %d 条）"
                     % len(skipped_grouped))
        if any(p["source"] == "proposal" for p in new_projects):
            notes.append("新项目只是提议，需在 plan 请求里带 accept_new_projects=true 才会建；"
                         "也可以先在项目页手动建好再重新预览")
    else:
        notes.append("模式 B 会**抛弃**新体系里没有会话的旧项目（删实体、保留目录），"
                     "应用前按字节备份三库，撤销可整批还原")
    if truncated:
        notes.append("候选超过 limit=%d，本次只考量前 %d 条；B 模式下已因此禁用删旧项目"
                     % (limit, limit))
    if rejected_skipped:
        notes.append("%d 条会话被手动忽略过（rejected），不进候选" % len(rejected_skipped))
    if visible.get("kanban"):
        notes.append("看板任务会话按硬约束排除：%d 条" % len(visible["kanban"]))
    if unmatched:
        notes.append("%d 条判不出目标项目，不会写入" % len(unmatched))

    diff = {
        "sessions": [{"session_id": i["session_id"], "title": i["title"],
                      "from": i["from_slug"], "to": i["to_slug"], "change": i["change"]}
                     for i in items],
        "moved": [{"session_id": i["session_id"], "title": i["title"],
                   "from": i["from_slug"], "to": i["to_slug"]}
                  for i in items if i["change"] == "moved"],
        "assigned": [{"session_id": i["session_id"], "to": i["to_slug"]}
                     for i in items if i["change"] == "assigned"],
        "projects": {
            "kept": [{"slug": s, "name": (projects.get(s) or {}).get("name") or s,
                      "session_count": len(clusters.get(s) or [])} for s in kept],
            "created": [{"slug": p["slug"], "name": p["name"], "session_count": p["session_count"],
                         "will_create": p["will_create"]} for p in new_projects],
            "dropped": [{"slug": d["slug"], "name": d["name"], "assignments_now": d["assignments_now"],
                         "will_delete": d["will_delete"]} for d in dropped],
        },
        "counts": {"moved": changes["move"], "assigned": changes["assign"],
                   "unchanged": changes["unchanged"], "created": len(new_projects),
                   "dropped": len(dropped), "unmatched": len(unmatched)},
    }

    return {"ok": True, "data": {
        "plan_id": plan_id, "mode": mode, "mode_label": MODE_LABELS[mode],
        "created_at": now, "expires_at": expires_at, "ttl_seconds": PLAN_TTL_SECONDS,
        "fingerprint": stored["fingerprint"], "zero_write": True, "wrote": [],
        "accept_new_projects": accept_new, "truncated": truncated, "limit": limit,
        "sync_cwd": sync_cwd,
        "candidates": {"total": len(candidates), "session_ids": [s["id"] for s in candidates],
                       "skipped_grouped": skipped_grouped, "rejected": rejected_skipped,
                       "excluded_kanban": list(visible.get("kanban") or []),
                       "excluded_sources": visible.get("excluded_sources") or []},
        "changes": changes, "items": items, "new_projects": new_projects,
        "dropped_projects": dropped, "unmatched": unmatched, "diff": diff,
    }, "notes": notes}


def _project_root(home: Path) -> Path:
    """项目目录根：与面板/项目页同口径（从既有项目反推，失败退回 core 口径）。"""
    try:
        import autogroup_projects as ent  # noqa: PLC0415
        return ent.project_root(Path(home))
    except Exception:  # noqa: BLE001
        try:
            return _core().project_root(Path(home))
        except Exception:  # noqa: BLE001
            return Path(home) / "projects"


# ---------------------------------------------------------------------------
# 台账（第 4 个文件：三库要被快照字节原样拷回，审计不能写进三库）
# ---------------------------------------------------------------------------

def ledger_path(home: Path) -> Path:
    return Path(home) / "auto-group" / LEDGER_NAME


def _ledger(home: Path) -> sqlite3.Connection:
    path = ledger_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect_rw(path)
    conn.executescript(LEDGER_SCHEMA)
    conn.commit()
    return conn


def _ledger_ro(home: Path) -> Optional[sqlite3.Connection]:
    path = ledger_path(home)
    if not path.is_file():
        return None
    try:
        conn = _connect_ro(path)
    except sqlite3.Error:
        return None
    if "ops" not in set(_table_names(conn)):
        conn.close()
        return None
    return conn


def _op_row(home: Path, op_id: str) -> Optional[Dict[str, Any]]:
    conn = _ledger_ro(home)
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT * FROM ops WHERE op_id = ?", (op_id,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _op_sessions(home: Path, op_id: str) -> List[Dict[str, Any]]:
    conn = _ledger_ro(home)
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM op_sessions WHERE op_id = ? ORDER BY seq", (op_id,))]
    finally:
        conn.close()


def _op_projects(home: Path, op_id: str) -> List[Dict[str, Any]]:
    conn = _ledger_ro(home)
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM op_projects WHERE op_id = ? ORDER BY seq", (op_id,))]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 归属行 / cwd 的读写（与面板台账同语义）
# ---------------------------------------------------------------------------

def _assign_row(home: Path, session_id: str) -> Optional[Dict[str, Any]]:
    core = _core()
    path = core.store_path(home)
    if not path.is_file():
        return None
    conn = _connect_ro(path)
    try:
        row = conn.execute("SELECT " + ", ".join(ASSIGN_COLUMNS) +
                           " FROM assignments WHERE session_id = ?", (session_id,)).fetchone()
        return dict(row) if row is not None else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _read_cwd(home: Path, session_id: str) -> Tuple[bool, Optional[str]]:
    path = Path(home) / "state.db"
    if not path.is_file():
        return False, None
    try:
        conn = _connect_ro(path)
        try:
            row = conn.execute("SELECT cwd FROM sessions WHERE id = ?", (session_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False, None
    return (True, row["cwd"]) if row is not None else (False, None)


def _write_cwd(home: Path, session_id: str, value: Optional[str]) -> bool:
    path = Path(home) / "state.db"
    if not path.is_file():
        return False
    conn = sqlite3.connect(str(path), timeout=15)
    try:
        with conn:
            cur = conn.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (value, session_id))
        return cur.rowcount > 0
    finally:
        conn.close()


def _restore_assign_row(conn: sqlite3.Connection, session_id: str,
                        row_before: Optional[Dict[str, Any]]) -> str:
    if row_before is None:
        conn.execute("DELETE FROM assignments WHERE session_id = ?", (session_id,))
        return "deleted"
    cols = [c for c in ASSIGN_COLUMNS if c in row_before]
    conn.execute("INSERT OR REPLACE INTO assignments(" + ", ".join(cols) + ") VALUES(" +
                 ", ".join("?" * len(cols)) + ")", [row_before[c] for c in cols])
    return "restored"


def _write_assign_row(home: Path, *, session_id: str, slug: str, op_id: str, now: float,
                      confidence: Optional[float], reason: str) -> None:
    core = _core()
    conn = core.ensure_store(home)
    try:
        with conn:
            conn.execute(
                "INSERT INTO assignments(session_id, project_slug, confidence, reason, source,"
                " status, batch_id, applied_at, updated_at) VALUES(?,?,?,?,?,'applied',?,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET project_slug=excluded.project_slug,"
                " confidence=excluded.confidence, reason=excluded.reason, source=excluded.source,"
                " status='applied', batch_id=excluded.batch_id, applied_at=excluded.applied_at,"
                " updated_at=excluded.updated_at",
                (session_id, slug, float(confidence or 0.0), reason, PLAN_SOURCE, op_id, now, now))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 项目实体：建 / 删 / 逐字段还原
# ---------------------------------------------------------------------------

def _project_entity_rows(home: Path, slug: str) -> Dict[str, Any]:
    """项目实体三张表的原始行（撤销要逐字段还原 name/path/归属行/cwd）。"""
    out: Dict[str, Any] = {"project": None, "folders": [], "attrs": None}
    path = Path(home) / "projects.db"
    if not path.is_file():
        return out
    conn = _connect_ro(path)
    try:
        names = set(_table_names(conn))
        if "projects" not in names:
            return out
        row = conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            return out
        out["project"] = dict(row)
        if "project_folders" in names:
            out["folders"] = [dict(r) for r in conn.execute(
                "SELECT * FROM project_folders WHERE project_id = ?", (row["id"],))]
        if "project_attrs" in names:
            attrs = conn.execute("SELECT * FROM project_attrs WHERE slug = ?", (slug,)).fetchone()
            out["attrs"] = dict(attrs) if attrs is not None else None
    finally:
        conn.close()
    return out


def _restore_project_entity(home: Path, entity: Dict[str, Any]) -> Dict[str, Any]:
    """把 ``_project_entity_rows`` 的快照逐字段写回（INSERT OR REPLACE，不猜默认值）。"""
    project = entity.get("project")
    if not project:
        return {"slug": None, "restored": False, "why": "快照里没有项目实体"}
    try:
        import autogroup_projects as ent  # noqa: PLC0415
        conn = ent.ensure_attrs(Path(home))
    except Exception:  # noqa: BLE001 - 退回直接连 projects.db
        conn = _connect_rw(Path(home) / "projects.db")
    try:
        with conn:
            cols = list(project.keys())
            conn.execute("INSERT OR REPLACE INTO projects(" + ", ".join(cols) + ") VALUES(" +
                         ", ".join("?" * len(cols)) + ")", [project[c] for c in cols])
            conn.execute("DELETE FROM project_folders WHERE project_id = ?", (project["id"],))
            for folder in entity.get("folders") or []:
                fcols = list(folder.keys())
                conn.execute("INSERT OR REPLACE INTO project_folders(" + ", ".join(fcols) +
                             ") VALUES(" + ", ".join("?" * len(fcols)) + ")",
                             [folder[c] for c in fcols])
            attrs = entity.get("attrs")
            if attrs:
                acols = list(attrs.keys())
                conn.execute("INSERT OR REPLACE INTO project_attrs(" + ", ".join(acols) +
                             ") VALUES(" + ", ".join("?" * len(acols)) + ")",
                             [attrs[c] for c in acols])
    finally:
        conn.close()
    return {"slug": project.get("slug"), "restored": True,
            "name": project.get("name"), "path": project.get("primary_path"),
            "folders": len(entity.get("folders") or []), "attrs": bool(entity.get("attrs"))}


def _delete_project_rows(home: Path, slug: str) -> bool:
    """只删 projects.db 里的三行（不碰目录）：撤销「这次新建的项目」用。"""
    path = Path(home) / "projects.db"
    if not path.is_file():
        return False
    conn = _connect_rw(path)
    try:
        row = conn.execute("SELECT id FROM projects WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            return False
        with conn:
            conn.execute("DELETE FROM project_folders WHERE project_id = ?", (row["id"],))
            try:
                conn.execute("DELETE FROM project_attrs WHERE slug = ?", (slug,))
            except sqlite3.Error:
                pass
            conn.execute("DELETE FROM projects WHERE id = ?", (row["id"],))
        return True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

def _create_project_entity(home: Path, slug: str, name: str,
                           created_by: str = "ai") -> Dict[str, Any]:
    """建项目实体（projects + project_folders + project_attrs + 目录）。

    **刻意不走** ``autogroup_project_ops.create``：那个函数会往 ``assignments.db`` 的
    ``console_ops`` 台账写 header，而本模块要求「撤销时三库能被快照字节原样拷回」——
    自己写进三库的审计行会让内容哈希永远对不上。校验仍复用它的零写入预览。
    """
    import autogroup_project_ops as pops  # noqa: PLC0415
    import autogroup_projects as ent  # noqa: PLC0415
    plan = pops.preview_create(home, slug=slug, name=name)
    if plan.get("problems"):
        raise RuntimeError("；".join(plan["problems"]))
    if plan.get("needs_confirm"):
        raise RuntimeError("目标目录已存在且非空，拒绝静默复用：" + str(plan.get("path")))
    target = Path(str(plan["path"]))
    dir_created = False
    if not target.exists():
        target.mkdir(parents=True, exist_ok=False)
        dir_created = True
    project_id, now = "p_" + uuid.uuid4().hex[:8], time.time()
    conn = ent.ensure_attrs(Path(home))
    try:
        with conn:
            conn.execute(
                "INSERT INTO projects(id, slug, name, description, icon, color, board_slug,"
                " primary_path, created_at, archived) VALUES(?,?,?,?,?,?,?,?,?,0)",
                (project_id, slug, name, "计划新建（" + MODE_LABELS[MODE_REBUILD_ALL] + "）",
                 None, None, None, str(target), int(now)))
            conn.execute("INSERT INTO project_folders(project_id, path, label, is_primary, added_at)"
                         " VALUES(?,?,?,1,?)", (project_id, str(target), "main", int(now)))
            ent.upsert_attrs(conn, slug, created_by=created_by, status="active", now=now)
    finally:
        conn.close()
    return {"slug": slug, "project_id": project_id, "path": str(target),
            "dir_created": dir_created}


def _delete_project_entity(home: Path, slug: str) -> bool:
    """删项目实体（三张表的行，**目录保留**）。撤销「这次新建的项目」与 B 模式「抛弃旧项目」共用。"""
    return _delete_project_rows(home, slug)


def _checkpoint(path: Path, mode: str = "TRUNCATE") -> Dict[str, Any]:
    """把 ``-wal`` 里的帧落进主文件并截断 WAL（非 WAL 库是 no-op）。

    **为什么必须先做**：``shutil.copy2`` 只拷主文件。WAL 库里没 checkpoint 的写还在
    ``<db>-wal`` 里，主文件是**旧**的 ⇒ 快照陈旧（例如刚删掉的项目在快照里还在），
    撤销把陈旧快照拷回去就把已删的行「复活」了。

    ``busy`` 非 0 表示有别的连接挡住了 checkpoint（此时主文件仍不完整，调用方要降级）。
    """
    out: Dict[str, Any] = {"ok": False, "busy": None, "log": None, "checkpointed": None,
                           "error": None, "mode": mode}
    try:
        conn = sqlite3.connect(str(path), timeout=15)
    except sqlite3.Error as exc:
        out["error"] = _describe(exc)
        return out
    try:
        row = conn.execute("PRAGMA wal_checkpoint(" + mode + ")").fetchone()
        if row:
            out["busy"], out["log"], out["checkpointed"] = int(row[0]), int(row[1]), int(row[2])
            out["ok"] = int(row[0]) == 0
    except sqlite3.Error as exc:
        out["error"] = _describe(exc)
    finally:
        conn.close()
    return out


def _backup_db(src: Path, dst: Path) -> bool:
    """WAL 一致的单文件拷贝（``Connection.backup()``，按页拷，不依赖 ``-wal``）。

    checkpoint 被别的连接挡住时的降级路径：慢一点，但**绝不会**拿到陈旧内容。
    """
    try:
        sconn = sqlite3.connect(str(src), timeout=15)
    except sqlite3.Error:
        return False
    try:
        try:
            dconn = sqlite3.connect(str(dst), timeout=15)
        except sqlite3.Error:
            return False
        try:
            sconn.backup(dconn)
            # .bak 要能独立打开（只读也行）：别让它继承 WAL 头
            dconn.execute("PRAGMA journal_mode=delete")
            dconn.commit()
        except sqlite3.Error:
            return False
        finally:
            dconn.close()
    finally:
        sconn.close()
    return True


def _restore_db_backup(src: Path, live: Path) -> bool:
    """用备份接口把快照内容写回活库（WAL 安全，但页布局可能与主文件字节不同）。"""
    try:
        sconn = sqlite3.connect(str(src), timeout=15)
    except sqlite3.Error:
        return False
    try:
        try:
            dconn = sqlite3.connect(str(live), timeout=30)
        except sqlite3.Error:
            return False
        try:
            sconn.backup(dconn)
            dconn.commit()
        except sqlite3.Error:
            return False
        finally:
            dconn.close()
    finally:
        sconn.close()
    return True


def _drop_empty_wal(path: Path) -> bool:
    """删掉 0 字节的 ``-wal`` 残留（截断后只剩空壳；留着会让回填的主文件被「重放」成别的状态）。"""
    side = Path(str(path) + "-wal")
    try:
        if side.is_file() and side.stat().st_size == 0:
            side.unlink()
            return True
    except OSError:
        pass
    return False


def _snapshot(home: Path, op_id: str) -> Dict[str, Any]:
    """按字节复制**可回填**的库（projects.db / assignments.db）。

    ``state.db`` 有 2GB 级（含 messages + FTS），既不该每次 apply 拷一遍，也不该在撤销时整库
    覆盖（会抹掉别的 agent 在期间写的新消息）。所以它只记录内容哈希，撤销时做**行级** cwd 还原。

    WAL（``projects.db`` 真库就是 WAL）：拷主文件前先 ``wal_checkpoint(TRUNCATE)``，让快照与
    live **同一时刻**；checkpoint 被挡住时降级成 ``Connection.backup()``，宁可变慢也不交陈旧快照。
    """
    folder = Path(home) / "auto-group" / SNAPSHOT_DIRNAME / op_id
    folder.mkdir(parents=True, exist_ok=True)
    files: List[Dict[str, Any]] = []
    for rel in TRACKED_DBS:
        src = _db_path(home, rel)
        if rel not in BYTE_RESTORE_DBS:
            files.append({"db": rel, "file": None, "sha256": None, "bytes": 0, "missing": True,
                          "reason": "live_store_row_level_only"})
            continue
        if not src.is_file():
            files.append({"db": rel, "file": None, "sha256": None, "bytes": 0, "missing": True,
                          "reason": "db_missing"})
            continue
        dst = folder / (rel.replace("/", "__") + ".bak")
        cp = _checkpoint(src)
        method = "copy"
        if cp.get("ok"):
            shutil.copy2(src, dst)
            # 拷贝期间有人写 ⇒ 重拷一次（最多 3 次），仍不一致就如实标出来
            for _attempt in range(2):
                if file_hash(dst) == file_hash(src):
                    break
                shutil.copy2(src, dst)
        else:
            if not _backup_db(src, dst):
                raise OSError("WAL checkpoint 被挡住（%s），备份接口也失败：%s"
                              % (rel, cp.get("error") or cp))
            method = "backup"
        files.append({"db": rel, "file": str(dst), "sha256": file_hash(dst),
                      "bytes": dst.stat().st_size, "missing": False,
                      "method": method, "checkpoint": cp,
                      "byte_copy": method == "copy"})
    meta = {"dir": str(folder), "files": files,
            "created_at": time.time(), "op_id": op_id}
    # 快照自带清单：撤销是**另一个进程/另一次请求**，得知道每个 .bak 是 copy 还是 backup 来的
    try:
        (folder / "snapshot.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return meta


def _snapshot_consistent(home: Path, op_id: str, attempts: int = 3) -> Dict[str, Any]:
    """快照 + **同一时刻**的 ``pre_hashes``。

    原来 ``_snapshot()`` 与 ``db_hashes()`` 是两次独立读：中间只要有一次 checkpoint / 写入，
    撤销时用来比对的「应用前字节」就不是快照里那份，``byte_identical`` 会假红。这里取完快照
    立刻算哈希，并核对「快照 sha == live sha」，不一致（有并发写）就重取。
    """
    snap: Dict[str, Any] = {}
    pre: Dict[str, Dict[str, Any]] = {}
    drift: List[str] = []
    for attempt in range(max(1, int(attempts))):
        snap = _snapshot(home, op_id)
        pre = db_hashes(home)
        drift = []
        for entry in snap.get("files") or []:
            if entry.get("missing"):
                continue
            rel = str(entry.get("db"))
            if entry.get("method") == "backup":
                # 降级快照是「按页备份」出来的单文件，字节本来就不等于 live 主文件：
                # 一致性只能按**内容**核对（否则合法降级会被当成漂移）。
                same = content_hash(Path(str(entry.get("file")))) == (pre.get(rel) or {}).get("content")
            else:
                same = entry.get("sha256") == (pre.get(rel) or {}).get("file")
            if not same:
                drift.append(rel)
        snap["attempts"] = attempt + 1
        snap["pre_hashes"] = json.dumps(pre, ensure_ascii=False)
        snap["drift"] = drift
        if not drift:
            break
    return snap


def _restore_bytes(home: Path, snapshot: Dict[str, Any]) -> Dict[str, str]:
    """把快照字节拷回**允许回填**的库（只拷快照里有的）。返回 {库: 结果}。

    回填前先 ``wal_checkpoint(TRUNCATE)``：否则活库里 apply/undo 刚写的帧还在 ``-wal``，
    拷回的主文件会被这些帧「重放」覆盖，撤销等于没做。
    """
    out: Dict[str, str] = {}
    for entry in snapshot.get("files") or []:
        rel = str(entry.get("db") or "")
        if not rel:
            continue
        if rel not in BYTE_RESTORE_DBS:
            out[rel] = "skipped_live_store"
            continue
        src = entry.get("file")
        if not src or not Path(str(src)).is_file():
            out[rel] = "missing_snapshot"
            continue
        live = _db_path(home, rel)
        try:
            if str(entry.get("method") or "copy") == "backup":
                out[rel] = "restored" if _restore_db_backup(Path(str(src)), live) else "failed:backup"
                continue
            _checkpoint(live)
            _drop_empty_wal(live)
            shutil.copy2(str(src), str(live))
            got = file_hash(live)
            want = entry.get("sha256")
            out[rel] = "restored" if (not want or got == want) else "restored_differs"
        except OSError as exc:
            out[rel] = "failed:" + _describe(exc)
    return out


# ---------------------------------------------------------------------------
# 应用计划
# ---------------------------------------------------------------------------

def _plan_summary(stored: Dict[str, Any]) -> Dict[str, Any]:
    return {"plan_id": stored.get("plan_id"), "mode": stored.get("mode"),
            "mode_label": MODE_LABELS.get(str(stored.get("mode")), ""),
            "created_at": stored.get("created_at"), "expires_at": stored.get("expires_at"),
            "changes": stored.get("changes"), "candidates": len(stored.get("candidates") or []),
            "new_projects": stored.get("new_projects"), "dropped_projects": stored.get("dropped")}


def apply_plan(home: Path, payload: Optional[Dict[str, Any]]) -> Any:
    """``POST /autogroup/apply``（带 plan_id）：快照 → 建新项目 → 写归属 → 删旧项目 → 逐条回读。"""
    payload = dict(payload or {})
    home = Path(home)
    plan_id = str(payload.get("plan_id") or "").strip()
    if not plan_id:
        return _fail("plan_id_required",
                     "必须带 plan_id：先 POST /autogroup/plan 生成计划，确认后再带 plan_id + confirm=true 应用"
                     "（不接受「重新算一遍再写」，否则预览和写入之间会漂移）",
                     extra={"requires": "plan"})
    with _PLANS_LOCK:
        stored = _PLANS.get(plan_id)
    if stored is None:
        return _fail("unknown_plan", "没有这个计划（进程重启后计划表会清空，需要重新预览）：" + plan_id,
                     status_code=404, extra={"plan_id": plan_id, "requires": "plan"})
    if str(stored.get("home")) != str(home):
        return _fail("plan_home_mismatch", "这个计划不是针对当前 home 生成的，拒绝应用",
                     status_code=409, extra={"plan_id": plan_id, "plan_home": stored.get("home")})
    now = time.time()
    if float(stored.get("expires_at") or 0) < now:
        with _PLANS_LOCK:
            _PLANS.pop(plan_id, None)
        return _fail("plan_expired", "计划已过期（有效期 %d 秒），请重新预览" % PLAN_TTL_SECONDS,
                     status_code=409, extra={"plan_id": plan_id, "expired_at": stored.get("expires_at"),
                                             "requires": "plan"})
    if payload.get("confirm") is not True:
        return _fail("confirmation_required",
                     "应用计划需要 confirm=true。下面是计划摘要（未写任何东西）",
                     extra={"plan": _plan_summary(stored)})
    accept_new = payload.get("accept_new_projects")
    if accept_new is not None and bool(accept_new) != bool(stored.get("accept_new_projects")):
        return _fail("plan_option_mismatch",
                     "accept_new_projects 与计划生成时不一致，请重新预览（计划即契约，不在应用时改选项）",
                     status_code=409,
                     extra={"plan_id": plan_id, "plan_accept_new_projects": stored.get("accept_new_projects"),
                            "request_accept_new_projects": bool(accept_new), "requires": "plan"})
    try:
        drift = fingerprint(home)
    except Exception as exc:  # noqa: BLE001
        return _fail("fingerprint_failed", "无法核对预览之后的数据是否变化：" + _describe(exc),
                     status_code=500, extra={"plan_id": plan_id})
    if drift != stored.get("fingerprint"):
        return _fail("plan_stale",
                     "预览之后数据变了（新会话 / 归属变动 / 项目变动），计划已失效，请重新预览",
                     status_code=409, extra={"plan_id": plan_id, "fingerprint_at_plan": stored.get("fingerprint"),
                                             "fingerprint_now": drift, "requires": "plan"})

    with _APPLY_LOCK:
        return _apply_locked(home, stored, now)


def _apply_locked(home: Path, stored: Dict[str, Any], now: float) -> Any:
    mode = str(stored.get("mode"))
    plan_id = str(stored.get("plan_id"))
    items = [i for i in stored.get("items") or [] if i.get("will_write")]
    new_projects = [p for p in stored.get("new_projects") or [] if p.get("will_create")]
    dropped = [d for d in stored.get("dropped") or [] if d.get("will_delete")]
    op_id = _new_id("op-")
    notes: List[str] = []

    try:
        snapshot = _snapshot_consistent(home, op_id)
    except OSError as exc:
        return _fail("snapshot_failed", "应用前备份三库失败（未做任何写入）：" + _describe(exc),
                     status_code=500, extra={"plan_id": plan_id, "op_id": op_id})
    if snapshot.get("drift"):
        # 快照与 live 的字节对不上 = 取快照期间库被并发写了。拿这份快照当回滚基准会丢掉别人的写，
        # 也会让「应用前哈希」与快照不是同一时刻 ⇒ 直接不写，顺手清掉这次没用的快照目录。
        _dir = str(snapshot.get("dir") or "")
        if _dir:
            shutil.rmtree(_dir, ignore_errors=True)
        return _fail("snapshot_unstable",
                     "应用前取不到一致的库快照（取快照期间这些库被并发写入：%s），未做任何写入，请稍后重试"
                     % "、".join(snapshot.get("drift") or []),
                     status_code=409,
                     extra={"plan_id": plan_id, "op_id": op_id, "drift": snapshot.get("drift"),
                            "attempts": snapshot.get("attempts")})
    pre_hashes = json.loads(snapshot.get("pre_hashes") or "{}")

    created: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []
    written: List[Dict[str, Any]] = []
    failed_items: List[Dict[str, Any]] = []
    try:
        # 1) 新建项目（B 一律建；A 仅在 accept_new_projects=true 时建）
        for spec in new_projects:
            slug = str(spec.get("slug") or "")
            entity_before = _project_entity_rows(home, slug)
            try:
                created_res = _create_project_entity(home, slug, str(spec.get("name") or slug))
            except Exception as exc:  # noqa: BLE001 - 建项目失败 ⇒ 整批中止并回滚
                raise RuntimeError("新建项目 " + slug + " 失败：" + _describe(exc)) from exc
            created.append({"slug": slug, "name": spec.get("name"),
                            "entity_before": entity_before,
                            "dir_created": bool(created_res.get("dir_created")),
                            "dir": str(created_res.get("path"))})
        # 2) 逐条写归属（+ cwd）
        for item in items:
            sid, slug = str(item["session_id"]), str(item["to_slug"])
            row_before = _assign_row(home, sid)
            cwd_known, cwd_before = _read_cwd(home, sid)
            target = item.get("cwd_target")
            if not cwd_known:
                failed_items.append({"session_id": sid, "why": "会话在 state.db 里不存在，拒绝写归属"})
                continue
            sync = bool(item.get("sync_cwd") and target)
            if sync and not Path(str(target)).is_dir():
                failed_items.append({"session_id": sid, "why": "目标目录不存在，本条回滚（不静默建目录）："
                                     + str(target)})
                continue
            try:
                _write_assign_row(home, session_id=sid, slug=slug, op_id=op_id, now=now,
                                  confidence=item.get("confidence"),
                                  reason="计划 " + plan_id + "：" + str((item.get("basis") or {}).get("reason") or ""))
            except Exception as exc:  # noqa: BLE001 - 单条写失败 ⇒ 整批回滚（结构一致性优先）
                raise RuntimeError("写归属失败（" + sid + "）：" + _describe(exc)) from exc
            cwd_written = False
            if sync:
                if not _write_cwd(home, sid, str(target)):
                    # 单条回滚：归属退回去，cwd 不动
                    core = _core()
                    conn = core.ensure_store(home)
                    try:
                        with conn:
                            _restore_assign_row(conn, sid, row_before)
                    finally:
                        conn.close()
                    failed_items.append({"session_id": sid, "why": "写 cwd 失败，本条归属已回滚"})
                    continue
                cwd_written = True
            written.append({"session_id": sid, "slug": slug, "row_before": row_before,
                            "cwd_known": cwd_known, "cwd_before": cwd_before,
                            "cwd_written": cwd_written, "slug_before": (row_before or {}).get("project_slug"),
                            "cwd_target": str(target) if sync else None})
        # 3) B：删掉新体系里没有会话的旧项目（有残留归属的不删，如实说）
        for spec in dropped:
            slug = str(spec.get("slug") or "")
            counts = _assign_counts_by_slug(home)
            left = counts.get(slug, 0)
            if left:
                removed.append({"slug": slug, "deleted": False,
                                "why": "新体系里没有它的会话，但仍有 %d 条归属（候选集之外），保留未删" % left})
                continue
            entity = _project_entity_rows(home, slug)
            try:
                if not _delete_project_entity(home, slug):
                    raise RuntimeError("删除后回读不到该项目行")
            except Exception as exc:  # noqa: BLE001 - 删不掉不该毁掉整批写入，但要说清
                removed.append({"slug": slug, "deleted": False, "why": "删除失败：" + _describe(exc)})
                continue
            removed.append({"slug": slug, "deleted": True, "entity": entity})
    except Exception as exc:  # noqa: BLE001 - 整批回滚
        rollback = _rollback(home, snapshot, written, created, removed, op_id)
        return _fail("apply_failed",
                     "应用计划失败，已整批回滚：" + _describe(exc),
                     status_code=500,
                     extra={"plan_id": plan_id, "op_id": op_id, "rolled_back": True,
                            "rollback": rollback})

    # 4) 逐条回读
    readback = _readback(home, written, op_id)
    if readback["mismatched"] or readback["cwd_mismatched"]:
        rollback = _rollback(home, snapshot, written, created, removed, op_id)
        return _fail("verify_mismatch",
                     "写入后回读与计划不一致，已整批回滚（不是 ok:true）",
                     status_code=409,
                     extra={"plan_id": plan_id, "op_id": op_id, "rolled_back": True,
                            "mismatched": readback["mismatched"],
                            "cwd_mismatched": readback["cwd_mismatched"],
                            "rollback": rollback})

    post_hashes = db_hashes(home)
    summary = {
        "plan_id": plan_id, "mode": mode, "op_id": op_id,
        "written": len(written), "failed_items": failed_items,
        "new_projects": [{"slug": c["slug"], "name": c["name"]} for c in created],
        "dropped_projects": [{"slug": r["slug"], "deleted": r["deleted"],
                              "why": r.get("why")} for r in removed],
        "changes": stored.get("changes"),
    }
    conn = _ledger(home)
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO ops(op_id, plan_id, mode, applied_at, undone, snapshot_dir,"
                " pre_hashes, post_hashes, summary, notes) VALUES(?,?,?,?,0,?,?,?,?,?)",
                (op_id, plan_id, mode, now, snapshot["dir"],
                 json.dumps(pre_hashes, ensure_ascii=False), json.dumps(post_hashes, ensure_ascii=False),
                 json.dumps(summary, ensure_ascii=False), json.dumps(notes, ensure_ascii=False)))
            for rec in written:
                conn.execute(
                    "INSERT INTO op_sessions(op_id, session_id, row_before, cwd_before, cwd_known,"
                    " cwd_written, slug_before, slug_after, outcome) VALUES(?,?,?,?,?,?,?,?,?)",
                    (op_id, rec["session_id"],
                     json.dumps(rec["row_before"], ensure_ascii=False) if rec["row_before"] is not None else None,
                     rec["cwd_before"], 1 if rec["cwd_known"] else 0, 1 if rec["cwd_written"] else 0,
                     rec["slug_before"], rec["slug"], "written"))
            for c in created:
                conn.execute(
                    "INSERT INTO op_projects(op_id, slug, action, project_json, folders_json,"
                    " attrs_json, dir_created) VALUES(?,?,'created',?,?,?,?)",
                    (op_id, c["slug"], json.dumps(c["entity_before"].get("project"), ensure_ascii=False)
                     if c["entity_before"].get("project") else None,
                     json.dumps(c["entity_before"].get("folders") or [], ensure_ascii=False),
                     json.dumps(c["entity_before"].get("attrs"), ensure_ascii=False)
                     if c["entity_before"].get("attrs") else None,
                     1 if c.get("dir_created") else 0))
            for r in removed:
                if not r.get("deleted"):
                    continue
                entity = r.get("entity") or {}
                conn.execute(
                    "INSERT INTO op_projects(op_id, slug, action, project_json, folders_json,"
                    " attrs_json, dir_created) VALUES(?,?,'dropped',?,?,?,0)",
                    (op_id, r["slug"],
                     json.dumps(entity.get("project"), ensure_ascii=False) if entity.get("project") else None,
                     json.dumps(entity.get("folders") or [], ensure_ascii=False),
                     json.dumps(entity.get("attrs"), ensure_ascii=False) if entity.get("attrs") else None))
    finally:
        conn.close()
    with _PLANS_LOCK:
        _PLANS.pop(plan_id, None)

    if failed_items:
        notes.append("%d 条未写入（逐条回滚）：%s" % (len(failed_items), "；".join(
            i["why"] for i in failed_items[:3])))
    if mode == MODE_REBUILD_ALL:
        notes.append("模式 B：旧项目里新体系没有会话的已删除（目录保留），撤销可整批还原")
    notes.append("撤销：POST /autogroup/undo {op_id: %s}（整批回到应用前）" % op_id)
    return {"ok": True, "data": {
        "op_id": op_id, "plan_id": plan_id, "mode": mode, "mode_label": MODE_LABELS.get(mode, ""),
        "written": len(written), "sessions": [w["session_id"] for w in written],
        "failed_items": failed_items,
        "new_projects": [{"slug": c["slug"], "name": c["name"], "path": c["dir"]} for c in created],
        "dropped_projects": [{"slug": r["slug"], "deleted": r["deleted"], "why": r.get("why")}
                             for r in removed],
        "snapshot": {k: v for k, v in snapshot.items() if k != "pre_hashes"}, "readback": readback,
        "hashes": {"before": pre_hashes, "after": post_hashes},
        "diff": {"counts": (stored.get("changes") or {})},
    }, "notes": notes}


def _readback(home: Path, written: List[Dict[str, Any]], op_id: str) -> Dict[str, Any]:
    """逐条回读：归属行必须与计划一致；同步过 cwd 的还要 cwd 一致。"""
    out: Dict[str, Any] = {"checked": 0, "mismatched": [], "cwd_mismatched": []}
    for rec in written:
        sid = rec["session_id"]
        got = _assign_row(home, sid)
        out["checked"] += 1
        if got is None:
            out["mismatched"].append({"session_id": sid, "why": "回读没有这一行"})
            continue
        diff: Dict[str, Any] = {}
        if got.get("project_slug") != rec["slug"]:
            diff["project_slug"] = {"want": rec["slug"], "got": got.get("project_slug")}
        if got.get("status") != "applied":
            diff["status"] = {"want": "applied", "got": got.get("status")}
        if got.get("source") != PLAN_SOURCE:
            diff["source"] = {"want": PLAN_SOURCE, "got": got.get("source")}
        if got.get("batch_id") != op_id:
            diff["batch_id"] = {"want": op_id, "got": got.get("batch_id")}
        if diff:
            out["mismatched"].append({"session_id": sid, "diff": diff})
        if rec.get("cwd_written"):
            exists, cwd_now = _read_cwd(home, sid)
            if not exists or cwd_now != rec.get("cwd_target"):
                out["cwd_mismatched"].append({"session_id": sid, "want": rec.get("cwd_target"),
                                              "got": cwd_now})
    return out


def _rollback(home: Path, snapshot: Dict[str, Any], written: List[Dict[str, Any]],
              created: List[Dict[str, Any]], removed: List[Dict[str, Any]],
              op_id: str) -> Dict[str, Any]:
    """整批回滚：快照字节拷回 assignments/projects + cwd 逐条还原 + 删掉本次新建的项目目录。"""
    out: Dict[str, Any] = {"op_id": op_id, "bytes": {}, "cwd_restored": [], "cwd_failed": [],
                           "projects_deleted": [], "projects_restored": [], "content_match": {}}
    # cwd 在 state.db：先按台账还原（快照字节不动 state.db —— 那是活的会话库）
    for rec in written:
        if not rec.get("cwd_written"):
            continue
        ok = _write_cwd(home, rec["session_id"], rec.get("cwd_before"))
        (out["cwd_restored"] if ok else out["cwd_failed"]).append(rec["session_id"])
    out["bytes"] = _restore_bytes(home, snapshot)
    # 本次新建的项目：删实体行 + 删（空）目录
    for c in created:
        try:
            if _delete_project_rows(home, c["slug"]):
                out["projects_deleted"].append(c["slug"])
            folder = Path(str(c.get("dir") or ""))
            if c.get("dir_created") and folder.is_dir():
                try:
                    folder.rmdir()
                except OSError:
                    pass
        except Exception as exc:  # noqa: BLE001
            out.setdefault("errors", []).append("删新建项目 " + c["slug"] + " 失败：" + _describe(exc))
    # 已删的旧项目：逐字段写回
    for r in removed:
        if not r.get("deleted"):
            continue
        try:
            res = _restore_project_entity(home, r.get("entity") or {})
            if res.get("restored"):
                out["projects_restored"].append(res["slug"])
        except Exception as exc:  # noqa: BLE001
            out.setdefault("errors", []).append("还原项目 " + str(r.get("slug")) + " 失败：" + _describe(exc))
    for rel in TRACKED_DBS:
        out["content_match"][rel] = None
    try:
        pre = json.loads(snapshot.get("pre_hashes") or "{}")
    except ValueError:
        pre = {}
    out["content_match"] = {rel: (content_hash(_db_path(home, rel)) == (pre.get(rel) or {}).get("content")
                                 if pre else None) for rel in TRACKED_DBS}
    return out


# ---------------------------------------------------------------------------
# 撤销
# ---------------------------------------------------------------------------

def maybe_undo(home: Path, op_id: str) -> Optional[Any]:
    """``api_autogroup.post_undo`` 的分流口：台账里有这个 op 才接管，否则返回 None 走老路径。"""
    op_id = str(op_id or "").strip()
    if not op_id:
        return None
    try:
        row = _op_row(home, op_id)
    except Exception:  # noqa: BLE001 - 台账坏了不该让老撤销路径失效
        return None
    if row is None:
        return None
    return undo_op(home, op_id)


def undo_op(home: Path, op_id: str) -> Any:
    """整批撤销：归属行/cwd 逐字段还原 → 项目实体还原/删除 → 三库内容哈希比对 → 字节回填。"""
    home = Path(home)
    op = _op_row(home, op_id)
    if op is None:
        return _fail("unknown_op", "台账里没有这个计划应用记录：" + op_id, status_code=404,
                     extra={"op_id": op_id})
    if op.get("undone"):
        return _fail("already_undone", "这个操作已经撤销过：" + op_id, status_code=409,
                     extra={"op_id": op_id, "undone_at": op.get("undone_at")})
    records = _op_sessions(home, op_id)
    projects = _op_projects(home, op_id)
    snapshot_dir = Path(str(op.get("snapshot_dir") or ""))
    meta_files: Dict[str, Dict[str, Any]] = {}
    meta_path = snapshot_dir / "snapshot.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            meta = {}
        for entry in meta.get("files") or []:
            if isinstance(entry, dict) and entry.get("db"):
                meta_files[str(entry["db"])] = entry
    snapshot = {"dir": str(snapshot_dir), "files": []}
    for rel in TRACKED_DBS:
        candidate = snapshot_dir / (rel.replace("/", "__") + ".bak")
        snapshot["files"].append({"db": rel, "file": str(candidate) if candidate.is_file() else None,
                                  "sha256": file_hash(candidate) if candidate.is_file() else None})
    for entry in snapshot["files"]:
        extra = meta_files.get(str(entry.get("db"))) or {}
        if extra.get("method"):
            entry["method"] = extra["method"]
        if extra.get("missing"):
            entry["missing"] = True
    try:
        pre_hashes = json.loads(op.get("pre_hashes") or "{}")
    except ValueError:
        pre_hashes = {}
    snapshot["pre_hashes"] = json.dumps(pre_hashes)

    core = _core()
    restored: List[Dict[str, Any]] = []
    try:
        conn = core.ensure_store(home)
    except Exception as exc:  # noqa: BLE001
        return _fail("store_unavailable", "打不开归属库：" + _describe(exc), status_code=500)
    try:
        with conn:
            for rec in records:
                row_before = json.loads(rec["row_before"]) if rec.get("row_before") else None
                action = _restore_assign_row(conn, rec["session_id"], row_before)
                cwd_action = "untouched"
                if rec.get("cwd_written") and rec.get("cwd_known"):
                    cwd_action = "restored" if _write_cwd(home, rec["session_id"],
                                                          rec.get("cwd_before")) else "missing_session"
                restored.append({"session_id": rec["session_id"], "assign": action, "cwd": cwd_action,
                                 "from": rec.get("slug_before"), "to": rec.get("slug_after")})
    except Exception as exc:  # noqa: BLE001
        return _fail("undo_failed", "撤销归属失败：" + _describe(exc), status_code=500)
    finally:
        conn.close()

    projects_undo: List[Dict[str, Any]] = []
    for rec in projects:
        slug = str(rec.get("slug") or "")
        action = str(rec.get("action") or "")
        try:
            if action == "created":
                deleted = _delete_project_rows(home, slug)
                folder = _project_root(home) / slug
                dir_removed = False
                if rec.get("dir_created") and folder.is_dir():
                    try:
                        folder.rmdir()      # 只删空目录，绝不递归
                        dir_removed = True
                    except OSError:
                        dir_removed = False
                projects_undo.append({"slug": slug, "action": "created", "rows_deleted": deleted,
                                      "dir_removed": dir_removed})
            elif action == "dropped":
                entity = {
                    "project": json.loads(rec["project_json"]) if rec.get("project_json") else None,
                    "folders": json.loads(rec["folders_json"]) if rec.get("folders_json") else [],
                    "attrs": json.loads(rec["attrs_json"]) if rec.get("attrs_json") else None,
                }
                res = _restore_project_entity(home, entity)
                projects_undo.append({"slug": slug, "action": "dropped", **res})
        except Exception as exc:  # noqa: BLE001 - 逐项目记录失败，不中断其余还原
            projects_undo.append({"slug": slug, "action": action, "restored": False,
                                  "why": _describe(exc)})

    # 三库内容哈希：与应用前一致 ⇒ 没有别人的改动 ⇒ 再把快照字节原样拷回（小库逐字节回到应用前）
    after_logical = db_hashes(home)
    logical_match = {rel: (after_logical[rel]["content"] == (pre_hashes.get(rel) or {}).get("content"))
                     for rel in TRACKED_DBS}
    foreign = [rel for rel, ok in logical_match.items() if not ok]
    byte_restore: Dict[str, str] = {}
    for rel in BYTE_RESTORE_DBS:
        if not logical_match.get(rel, False):
            byte_restore[rel] = "skipped_foreign_changes"
    if all(logical_match.get(rel, False) for rel in BYTE_RESTORE_DBS):
        byte_restore.update(_restore_bytes(home, snapshot))
    snap_method = {str(e.get("db")): str(e.get("method") or "copy")
                   for e in snapshot.get("files") or []}
    final_hashes = db_hashes(home)
    byte_match: Dict[str, Optional[bool]] = {}
    backup_snapshots: List[str] = []
    for rel in TRACKED_DBS:
        if rel not in BYTE_RESTORE_DBS:
            byte_match[rel] = None      # 活库不回填字节：只保证内容哈希
            continue
        if snap_method.get(rel) == "backup":
            # 降级快照（checkpoint 被挡住时用 Connection.backup 取的）：页布局本来就可能与主文件
            # 不同，逐字节断言没有意义 —— 只看内容哈希，别把「合法降级」报成假红。
            byte_match[rel] = None
            backup_snapshots.append(rel)
            continue
        byte_match[rel] = final_hashes[rel]["file"] == (pre_hashes.get(rel) or {}).get("file")
    content_match = {rel: (final_hashes[rel]["content"] == (pre_hashes.get(rel) or {}).get("content"))
                     for rel in TRACKED_DBS}

    now = time.time()
    ledger = _ledger(home)
    try:
        with ledger:
            ledger.execute("UPDATE ops SET undone = 1, undone_at = ? WHERE op_id = ?", (now, op_id))
    finally:
        ledger.close()

    notes: List[str] = ["已逐字段还原 %d 条归属行；cwd 只在这次真写过时才回滚" % len(restored)]
    if foreign:
        notes.append("撤销时发现候选集之外还有别人的改动（%s），对应库因此**没有**把快照字节拷回，"
                     "只做了逐字段还原（避免覆盖别人的写入）" % "、".join(foreign))
    if any(v == "restored" for v in byte_restore.values()):
        notes.append("projects.db / assignments.db 已按快照字节拷回：逐字节回到应用前")
    if backup_snapshots:
        notes.append("这些库的快照是 WAL 一致备份（应用前 checkpoint 被别的连接挡住，降级成 "
                     "Connection.backup）：撤销按内容还原，%s 不做逐字节断言"
                     % "、".join(backup_snapshots))
    notes.append("state.db 是活会话库：只做行级 cwd 还原，不做整库字节回填（否则会抹掉期间别人写入的"
                 "新消息）；它的内容哈希已回到应用前")
    bad = [rel for rel, ok in content_match.items() if not ok]
    if bad:
        notes.append("注意：撤销后内容哈希与应用前不一致：" + "、".join(bad))
    byte_identical = (all(content_match.values())
                      and all(byte_match.get(rel) is not False for rel in BYTE_RESTORE_DBS))
    return {"ok": True, "data": {
        "op_id": op_id, "plan_id": op.get("plan_id"), "mode": op.get("mode"),
        "undone_at": now, "sessions": restored, "projects": projects_undo,
        "snapshot_dir": str(snapshot_dir), "byte_restore": byte_restore,
        "foreign_changes": foreign,
        "hashes": {"before_apply": pre_hashes, "after_undo": final_hashes,
                   "content_match": content_match, "byte_match": byte_match},
        "byte_identical": bool(byte_identical),
    }, "notes": notes}


def list_ops(home: Path, limit: int = 50) -> Dict[str, Any]:
    """台账里的计划应用记录（倒序），给 UI 的「可撤销」列表用。"""
    conn = _ledger_ro(Path(home))
    if conn is None:
        return {"ops": [], "total": 0}
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT op_id, plan_id, mode, applied_at, undone, undone_at, snapshot_dir, summary"
            " FROM ops ORDER BY applied_at DESC LIMIT ?", (int(limit),))]
        total = conn.execute("SELECT COUNT(*) FROM ops").fetchone()[0]
    finally:
        conn.close()
    ops = []
    for row in rows:
        try:
            summary = json.loads(row.get("summary") or "{}")
        except ValueError:
            summary = {}
        ops.append({"op_id": row["op_id"], "plan_id": row["plan_id"], "mode": row["mode"],
                    "applied_at": row["applied_at"], "undone": bool(row["undone"]),
                    "undone_at": row["undone_at"], "snapshot_dir": row["snapshot_dir"],
                    "written": summary.get("written"), "changes": summary.get("changes"),
                    "can_undo": not row["undone"]})
    return {"ops": ops, "total": int(total)}

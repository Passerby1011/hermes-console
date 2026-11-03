# -*- coding: utf-8 -*-
"""项目操作契约（t_53b48743 第 3 段）：新建 / 重命名 / 改目录 / 解散 / 删除。

五个操作各自**口径明确**，绝不共用「重命名」这种含糊语义：

==================  ==========================================================
操作                动了什么
==================  ==========================================================
``create``          projects 行 + project_folders + project_attrs + 目录
``rename_name``     **只改** ``projects.name``，目录/归属/slug 全不动
``rename_slug``     ``projects.slug`` + 目录改名 + 归属表 + 规则表 + 台账（高风险）
``change_path``     ``projects.primary_path`` + project_folders（迁移或重绑）
``dissolve``        ``project_attrs.status='dissolved'`` + 归属会话按选定口径处理
``delete``          删 projects 行 + 文件夹行 + 属性行（目录删/留分别选）
==================  ==========================================================

安全线（每个写操作都走）：

1. **预览** —— 只算影响面，零写入（沙盒 config/state 逐字节相同）。
2. **二次确认** —— 缺 ``confirm=true`` 一律 ``confirmation_required``（HTTP 400）+ 预览体。
3. **写入** —— 优先单库事务；跨库（projects.db × assignments.db）先落台账再写。
4. **逐条回读** —— 用项目表/归属表重新读一遍，不拿写入返回值当证据。
5. **批次回滚** —— ``op_id`` 记在 ``console_ops``，撤销走 ``panel._undo_panel_op``。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import api_autogroup as base
import api_autogroup_panel as panel
import autogroup_projects as ent

#: 解散时归属会话的去向。**必须显式选一个**，没有默认值。
DISSOLVE_TO_UNGROUPED = "ungrouped"      # 删掉归属行（退回未分组）
DISSOLVE_TO_PROJECT = "move"             # 迁到另一个项目（要 target_slug）
DISSOLVE_TARGETS = (DISSOLVE_TO_UNGROUPED, DISSOLVE_TO_PROJECT)

#: 目录处理方式。
DIR_KEEP = "keep"
DIR_DELETE_EMPTY = "delete_empty"        # 只删空目录（非空 ⇒ 拒绝，绝不递归删）
DIR_ACTIONS = (DIR_KEEP, DIR_DELETE_EMPTY)


class OpError(Exception):
    """项目操作被拒绝/失败。``code`` 映射 HTTP 状态码，``extra`` 给 UI。"""

    def __init__(self, code: str, message: str, status: int = 400,
                 extra: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra or {}


# ---------------------------------------------------------------------------
# 公共小工具
# ---------------------------------------------------------------------------

def _projects(home: Path) -> Dict[str, Dict[str, Any]]:
    core = base._load_core()
    return {str(p.get("slug")): p for p in core.list_projects(home)}


def _require_project(home: Path, slug: str) -> Dict[str, Any]:
    projects = _projects(home)
    project = projects.get(slug)
    if project is None:
        raise OpError("unknown_project", "没有这个项目：" + slug, status=404,
                      extra={"projects": sorted(projects)})
    return project


def _assign_counts(home: Path) -> Dict[str, int]:
    """slug → 归属条数（不清看板/非看板：删库前要的是真实总数）。"""
    path = ent.store_db(home)
    if not path.is_file():
        return {}
    conn = ent._connect_ro(path)
    try:
        return {str(r["project_slug"]): int(r["n"]) for r in conn.execute(
            "SELECT project_slug, COUNT(*) AS n FROM assignments GROUP BY project_slug")}
    finally:
        conn.close()


def _folder_paths(conn: sqlite3.Connection, project_id: str) -> List[Dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT path, label, is_primary FROM project_folders WHERE project_id = ?"
        " ORDER BY is_primary DESC, path", (project_id,))]


def _ledger_header(home: Path, *, op_id: str, op: str, payload: Dict[str, Any],
                   batch_id: Optional[str] = None) -> bool:
    """在 ``console_ops`` 记一条 header。失败返回 False（调用方要在 notes 说明）。"""
    try:
        _core, conn = panel._store(home)
    except Exception:  # noqa: BLE001
        return False
    try:
        with conn:
            panel._record_header(conn, op_id=op_id, op=op, payload=payload, batch_id=batch_id)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1) 新建项目
# ---------------------------------------------------------------------------

def preview_create(home: Path, *, slug: str, name: str, path: Optional[str] = None) -> Dict[str, Any]:
    """新建预览：slug 合法性/重名/目录现状/是否必须显式确认。**零写入**。"""
    slug = str(slug or "").strip()
    name = str(name or "").strip()
    notes: List[str] = []
    problems: List[str] = []
    if not base.SLUG_RE.match(slug):
        problems.append("slug 不合法（2-41 位小写字母/数字/连字符）：" + repr(slug))
    if not name:
        problems.append("缺少显示名 name")
    if len(name) > 60:
        problems.append("显示名最多 60 个字符")
    projects = _projects(home)
    if slug in projects:
        problems.append("slug 已被占用：" + slug)
    target = Path(path) if path else ent.project_root(home) / slug
    root = ent.project_root(home)
    try:
        resolved, resolved_root = target.resolve(), root.resolve()
        if resolved != resolved_root and resolved_root not in resolved.parents:
            problems.append("目录越界：%s 不在 %s 之下" % (resolved, resolved_root))
    except OSError as exc:
        problems.append("目录路径无法解析：" + base._describe(exc))
    state = ent.dir_probe(target)
    needs_confirm = bool(state.get("exists") and state.get("is_dir") and not state.get("empty"))
    if state.get("exists") and not state.get("is_dir"):
        problems.append("目标路径已存在但不是目录：" + str(target))
    if needs_confirm:
        notes.append("目标目录已存在且有 %s 项内容：复用必须显式传 allow_existing_dir=true"
                     % state.get("entries"))
    notes.insert(0, "将新建项目 %s（%s），目录 %s" % (slug, name or "<无>", target))
    return {"ok": not problems, "problems": problems, "notes": notes,
            "slug": slug, "name": name, "path": str(target),
            "dir": state, "needs_confirm": needs_confirm}


def create(home: Path, *, slug: str, name: str, path: Optional[str] = None,
           created_by: str = "manual", allow_existing_dir: bool = False,
           note: Optional[str] = None, sort_order: Optional[int] = None,
           color: Optional[str] = None) -> Dict[str, Any]:
    """建项目：projects 行 + 文件夹行 + 属性行 + 目录。目录非空必须 ``allow_existing_dir``。"""
    if created_by not in ent.CREATED_BY:
        raise OpError("invalid_created_by", "created_by 只能是 " + "/".join(ent.CREATED_BY))
    plan = preview_create(home, slug=slug, name=name, path=path)
    if plan["problems"]:
        raise OpError("invalid_project", "；".join(plan["problems"]), extra={"preview": plan})
    if plan["needs_confirm"] and allow_existing_dir is not True:
        raise OpError("existing_dir_confirmation_required",
                      "目标目录已存在且非空，复用必须显式 allow_existing_dir=true（不静默复用）",
                      status=409, extra={"preview": plan})
    target = Path(plan["path"])
    dir_created = False
    if not target.exists():
        try:
            target.mkdir(parents=True, exist_ok=False)
            dir_created = True
        except OSError as exc:
            raise OpError("dir_create_failed", "建目录失败：" + base._describe(exc),
                          status=500, extra={"path": str(target)})
    project_id, now = "p_" + uuid.uuid4().hex[:8], time.time()
    conn = ent.ensure_attrs(home)          # 打开 projects.db 并补建 project_attrs
    try:
        with conn:
            if conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
                raise OpError("slug_exists", "slug 已被占用（并发写入？）：" + slug, status=409)
            conn.execute(
                "INSERT INTO projects(id, slug, name, description, icon, color, board_slug,"
                " primary_path, created_at, archived) VALUES(?,?,?,?,?,?,?,?,?,0)",
                (project_id, slug, name, note or ("面板新建：%s" % name), None, color, None,
                 str(target), int(now)))
            conn.execute("INSERT INTO project_folders(project_id, path, label, is_primary, added_at)"
                         " VALUES(?,?,?,1,?)", (project_id, str(target), "main", int(now)))
            ent.upsert_attrs(conn, slug, created_by=created_by, status="active",
                             sort_order=sort_order, color=color, note=note, now=now)
    except OpError:
        if dir_created:
            try:
                target.rmdir()
            except OSError:
                pass
        raise
    except sqlite3.Error as exc:
        if dir_created:
            try:
                target.rmdir()
            except OSError:
                pass
        raise OpError("projects_db_write_failed", "写 projects.db 失败：" + base._describe(exc),
                      status=500)
    finally:
        conn.close()
    entity = ent.get_entity(home, slug)
    if entity is None or not entity["dir"].get("exists"):
        raise OpError("readback_failed", "建项目返回成功，但回读项目表/目录不符：" + slug,
                      status=500, extra={"slug": slug, "entity": entity})
    op_id = panel._new_id("op-")
    recorded = _ledger_header(home, op_id=op_id, op="projects.create",
                              payload={"slug": slug, "name": name, "path": str(target),
                                       "created_by": created_by, "dir_created": dir_created,
                                       "reused_existing_dir": bool(plan["needs_confirm"])})
    notes = ["已建项目 %s，目录 %s（%s）" % (slug, target,
                                          "新建" if dir_created else "复用已存在的空目录")]
    if not recorded:
        notes.append("项目已建，但写台账失败（撤销将无记录）")
    return {"ok": True, "slug": slug, "project_id": project_id, "entity": entity,
            "dir_created": dir_created, "op_id": op_id if recorded else None, "notes": notes}


# ---------------------------------------------------------------------------
# 2) 重命名：显示名（低风险） / slug（高风险，迁目录）
# ---------------------------------------------------------------------------

def rename_name(home: Path, slug: str, new_name: str) -> Dict[str, Any]:
    """**只改显示名**。目录、slug、归属、规则表全不动（这是「改名」的默认语义）。"""
    slug = str(slug or "").strip()
    new_name = str(new_name or "").strip()
    if not new_name:
        raise OpError("invalid_name", "name 不能为空")
    if len(new_name) > 60:
        raise OpError("invalid_name", "显示名最多 60 个字符")
    project = _require_project(home, slug)
    old_name = project.get("name")
    if old_name == new_name:
        return {"ok": True, "noop": True, "slug": slug, "name": new_name,
                "notes": ["显示名未变（%s），什么都没写" % new_name]}
    path_before, path_after = project.get("primary_path"), None
    conn = ent.ensure_attrs(home)
    try:
        with conn:
            conn.execute("UPDATE projects SET name = ? WHERE slug = ?", (new_name, slug))
            ent.upsert_attrs(conn, slug)
    except sqlite3.Error as exc:
        raise OpError("projects_db_write_failed", "改显示名失败：" + base._describe(exc), status=500)
    finally:
        conn.close()
    entity = ent.get_entity(home, slug)
    if entity is None or entity.get("name") != new_name:
        raise OpError("readback_failed", "改名返回成功，但回读显示名不符", status=500,
                      extra={"slug": slug, "entity": entity})
    path_after = entity.get("path")
    if str(path_before) != str(path_after):
        raise OpError("readback_failed", "只改显示名却动了目录路径（不应该发生）", status=500,
                      extra={"before": path_before, "after": path_after})
    op_id = panel._new_id("op-")
    recorded = _ledger_header(home, op_id=op_id, op="projects.rename_name",
                              payload={"slug": slug, "name_before": old_name, "name_after": new_name})
    return {"ok": True, "slug": slug, "name": new_name, "name_before": old_name,
            "path": path_after, "path_unchanged": True, "op_id": op_id if recorded else None,
            "notes": ["显示名 %r → %r；目录仍是 %s（没动）" % (old_name, new_name, path_after),
                      "归属表未改：%d 条仍指向 %s" % (entity["assignments"]["total"], slug)]}


def preview_rename_slug(home: Path, slug: str, new_slug: str) -> Dict[str, Any]:
    """改 slug 的预览：目录会怎么动、多少归属会跟着改、有没有阻塞。**零写入**。"""
    slug, new_slug = str(slug or "").strip(), str(new_slug or "").strip()
    problems: List[str] = []
    notes: List[str] = []
    project = _require_project(home, slug)
    if not base.SLUG_RE.match(new_slug):
        problems.append("新 slug 不合法：" + repr(new_slug))
    if new_slug in _projects(home):
        problems.append("新 slug 已被占用：" + new_slug)
    old_path = project.get("primary_path")
    new_path = None
    if old_path:
        new_path = str(Path(str(old_path)).parent / new_slug)
    state_old = ent.dir_probe(Path(str(old_path))) if old_path else {"exists": False}
    state_new = ent.dir_probe(Path(str(new_path))) if new_path else {"exists": False}
    if state_new.get("exists") and not state_new.get("empty"):
        problems.append("目标目录已存在且非空，拒绝覆盖：" + str(new_path))
    counts = _assign_counts(home)
    will_migrate = counts.get(slug, 0)
    notes.append("高风险操作：改名 %s → %s，目录 %s → %s" % (slug, new_slug, old_path, new_path))
    notes.append("归属表会有 %d 条 project_slug 跟着改（%s → %s）" % (will_migrate, slug, new_slug))
    notes.append("会话 cwd 若指向旧目录，改完就不再等于项目 path —— 需要再跑一次一致性对齐")
    if will_migrate:
        notes.append("有归属的项目改 slug 后必须复核 cwd 一致性；撤销会把 slug/目录/归属一起还原")
    return {"ok": not problems, "problems": problems, "notes": notes, "slug": slug,
            "new_slug": new_slug, "path_before": old_path, "path_after": new_path,
            "dir_before": state_old, "dir_after": state_new, "assignments_to_migrate": will_migrate}


def rename_slug(home: Path, slug: str, new_slug: str, *, move_dir: bool = True) -> Dict[str, Any]:
    """**改 slug**：projects.slug + 目录改名 + 归属表 + 规则表 + 台账（高风险，需二次确认）。"""
    plan = preview_rename_slug(home, slug, new_slug)
    if plan["problems"]:
        raise OpError("rename_slug_rejected", "；".join(plan["problems"]), status=409,
                      extra={"preview": plan})
    project = _require_project(home, slug)
    old_path, new_path = plan["path_before"], plan["path_after"]
    moved_dir = False
    if move_dir and old_path and new_path and plan["dir_before"].get("exists"):
        try:
            shutil.move(str(old_path), str(new_path))
            moved_dir = Path(str(new_path)).is_dir()
        except OSError as exc:
            raise OpError("dir_migrate_failed", "目录迁移失败（未改任何库）：" + base._describe(exc),
                          status=500, extra={"from": old_path, "to": new_path})
    migrated = {"assignments": 0, "rules": False, "ledger": False, "folders": 0}
    try:
        conn = ent.ensure_attrs(home)
        try:
            with conn:
                conn.execute("UPDATE projects SET slug = ?, primary_path = ? WHERE slug = ?",
                             (new_slug, new_path if moved_dir else old_path, slug))
                cur = conn.execute("UPDATE project_folders SET path = ? WHERE project_id = ? AND path = ?",
                                   (new_path if moved_dir else old_path, project.get("id"), old_path))
                migrated["folders"] = int(cur.rowcount or 0)
                conn.execute("UPDATE project_attrs SET slug = ?, updated_at = ? WHERE slug = ?",
                             (new_slug, time.time(), slug))
        finally:
            conn.close()
        store = ent.store_db(home)
        if store.is_file():
            sconn = ent._connect_rw(store)
            try:
                with sconn:
                    cur = sconn.execute("UPDATE assignments SET project_slug = ?, updated_at = ?"
                                        " WHERE project_slug = ?", (new_slug, time.time(), slug))
                    migrated["assignments"] = int(cur.rowcount or 0)
            finally:
                sconn.close()
        rules_file = Path(home) / "auto-group" / "rules.json"
        if rules_file.exists():
            try:
                data = json.loads(rules_file.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError):
                data = {}
            if slug in data:
                words = data.pop(slug)
                target = data.setdefault(new_slug, [])
                for word in words or []:
                    if word not in target:
                        target.append(word)
                base._atomic_write_json(rules_file, data)
                migrated["rules"] = True
    except Exception as exc:  # noqa: BLE001
        # 库写失败：把目录先迁回去，别留半截
        if moved_dir:
            try:
                shutil.move(str(new_path), str(old_path))
            except OSError:
                pass
        raise OpError("rename_slug_failed", "改 slug 失败（目录已尝试迁回）：" + base._describe(exc),
                      status=500, extra={"slug": slug, "new_slug": new_slug})
    entity = ent.get_entity(home, new_slug)
    if entity is None:
        raise OpError("readback_failed", "改 slug 返回成功，回读项目表里没有新 slug", status=500)
    if slug in _projects(home):
        raise OpError("readback_failed", "改 slug 返回成功，旧 slug 还在", status=500)
    if moved_dir and not Path(str(new_path)).is_dir():
        raise OpError("readback_failed", "改 slug 返回成功，但新目录不存在", status=500)
    left = _assign_counts(home).get(slug, 0)
    if left:
        raise OpError("readback_failed", "改 slug 后仍有 %d 条归属指向旧 slug" % left, status=500)
    op_id = panel._new_id("op-")
    recorded = _ledger_header(home, op_id=op_id, op="projects.rename_slug",
                              payload={"slug_before": slug, "slug_after": new_slug,
                                       "path_before": old_path, "path_after": new_path,
                                       "moved_dir": moved_dir, "migrated": migrated})
    notes = ["slug %s → %s；目录%s" % (slug, new_slug, "已迁移到 " + str(new_path) if moved_dir
                                      else "未迁移（move_dir=false 或原目录不存在）"),
             "归属迁移 %d 条、文件夹行 %d 条、规则表 %s"
             % (migrated["assignments"], migrated["folders"],
                "已迁移" if migrated["rules"] else "无改动"),
             "会话 cwd 未动：如指向旧目录，请再跑一致性对齐（fix_cwd）"]
    if not recorded:
        notes.append("已完成，但写台账失败（撤销将无记录）")
    return {"ok": True, "slug": slug, "new_slug": new_slug, "entity": entity,
            "moved_dir": moved_dir, "migrated": migrated,
            "op_id": op_id if recorded else None, "notes": notes}


# ---------------------------------------------------------------------------
# 3) 改目录（迁移 / 重新绑定）
# ---------------------------------------------------------------------------

PATH_MIGRATE = "migrate"      # 把旧目录里的内容搬到新目录
PATH_REBIND = "rebind"        # 不动磁盘，只把项目改指向新目录
PATH_MODES = (PATH_MIGRATE, PATH_REBIND)


def preview_change_path(home: Path, slug: str, new_path: str, *, mode: str = PATH_REBIND) -> Dict[str, Any]:
    """改目录预览。必须交代：旧目录有什么、新目录有什么、内容怎么处理。**零写入**。"""
    slug = str(slug or "").strip()
    mode = str(mode or PATH_REBIND).strip()
    problems: List[str] = []
    notes: List[str] = []
    project = _require_project(home, slug)
    if mode not in PATH_MODES:
        problems.append("mode 只能是 " + "/".join(PATH_MODES))
    old_path = project.get("primary_path")
    target = Path(str(new_path or "").strip())
    if not str(new_path or "").strip():
        problems.append("需要 new_path")
    root = ent.project_root(home)
    try:
        resolved, resolved_root = target.resolve(), root.resolve()
        if resolved != resolved_root and resolved_root not in resolved.parents:
            problems.append("目录越界：%s 不在 %s 之下" % (resolved, resolved_root))
    except OSError as exc:
        problems.append("目录路径无法解析：" + base._describe(exc))
    if old_path and str(target) == str(old_path):
        problems.append("新目录与当前目录相同，无需修改")
    state_old = ent.dir_probe(Path(str(old_path))) if old_path else {"exists": False}
    state_new = ent.dir_probe(target)
    if state_new.get("exists") and not state_new.get("is_dir"):
        problems.append("目标路径已存在但不是目录")
    if mode == PATH_MIGRATE:
        if not state_old.get("exists"):
            problems.append("旧目录不存在，没有可迁移的内容（改用 rebind）")
        if state_new.get("exists") and not state_new.get("empty"):
            problems.append("目标目录已存在且非空，迁移会冲突，拒绝（先清空或改用 rebind）")
        notes.append("将把 %s 的内容搬到 %s" % (old_path, target))
    else:
        notes.append("只改项目指向到 %s，磁盘上什么都不搬" % target)
        if state_new.get("exists") and not state_new.get("empty"):
            notes.append("目标目录已有 %s 项内容：项目会直接指过去，旧目录 %s 保留原地"
                         % (state_new.get("entries"), old_path))
        if not state_new.get("exists"):
            notes.append("目标目录不存在：改完会先建空目录（rebind 允许建新目录）")
    counts = _assign_counts(home)
    affected = counts.get(slug, 0)
    notes.append("归属 %d 条；改完会话 cwd 需要按新目录重对齐（fix_cwd）" % affected)
    return {"ok": not problems, "problems": problems, "notes": notes, "slug": slug, "mode": mode,
            "path_before": old_path, "path_after": str(target),
            "dir_before": state_old, "dir_after": state_new, "assignments": affected}


def change_path(home: Path, slug: str, new_path: str, *, mode: str = PATH_REBIND,
                remove_old_empty_dir: bool = False) -> Dict[str, Any]:
    """改目录：``migrate`` 搬内容 / ``rebind`` 只改指向。两种都回读项目表与磁盘。"""
    plan = preview_change_path(home, slug, new_path, mode=mode)
    if plan["problems"]:
        raise OpError("change_path_rejected", "；".join(plan["problems"]), status=409,
                      extra={"preview": plan})
    project = _require_project(home, slug)
    old_path, target = plan["path_before"], Path(plan["path_after"])
    mode = plan["mode"]
    if mode == PATH_MIGRATE:
        try:
            shutil.move(str(old_path), str(target))
        except OSError as exc:
            raise OpError("dir_migrate_failed", "目录迁移失败（未改任何库）：" + base._describe(exc),
                          status=500, extra={"from": old_path, "to": str(target)})
    else:
        if not target.exists():
            try:
                target.mkdir(parents=True, exist_ok=False)
            except OSError as exc:
                raise OpError("dir_create_failed", "建目录失败：" + base._describe(exc), status=500)
    removed_old = False
    try:
        conn = ent.ensure_attrs(home)
        try:
            with conn:
                conn.execute("UPDATE projects SET primary_path = ? WHERE slug = ?",
                             (str(target), slug))
                cur = conn.execute(
                    "UPDATE project_folders SET path = ? WHERE project_id = ? AND path = ?",
                    (str(target), project.get("id"), old_path))
                if not cur.rowcount:
                    conn.execute(
                        "INSERT INTO project_folders(project_id, path, label, is_primary, added_at)"
                        " VALUES(?,?,?,1,?)", (project.get("id"), str(target), "main", int(time.time())))
                ent.upsert_attrs(conn, slug)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        if mode == PATH_MIGRATE:
            try:
                shutil.move(str(target), str(old_path))
            except OSError:
                pass
        raise OpError("change_path_failed", "改目录失败（目录已尝试迁回）：" + base._describe(exc),
                      status=500)
    if remove_old_empty_dir and old_path and str(old_path) != str(target):
        try:
            if Path(str(old_path)).is_dir() and ent.dir_is_empty(Path(str(old_path))):
                Path(str(old_path)).rmdir()
                removed_old = True
        except OSError:
            removed_old = False
    entity = ent.get_entity(home, slug)
    if entity is None or str(entity.get("path")) != str(target):
        raise OpError("readback_failed", "改目录返回成功，但回读 primary_path 不符", status=500,
                      extra={"want": str(target), "got": (entity or {}).get("path")})
    if not Path(str(target)).is_dir():
        raise OpError("readback_failed", "改目录返回成功，但新目录不存在", status=500)
    op_id = panel._new_id("op-")
    recorded = _ledger_header(home, op_id=op_id, op="projects.change_path",
                              payload={"slug": slug, "mode": mode, "path_before": old_path,
                                       "path_after": str(target), "removed_old_dir": removed_old})
    notes = ["目录 %s → %s（%s）" % (old_path, target, "内容已迁移" if mode == PATH_MIGRATE
                                    else "只改指向，磁盘未搬")]
    if removed_old:
        notes.append("旧目录是空的，已删掉")
    notes.append("会话 cwd 未动：请再跑一致性对齐（fix_cwd）把 %d 条会话对齐到新目录"
                 % (entity["assignments"]["total"]))
    if not recorded:
        notes.append("已完成，但写台账失败（撤销将无记录）")
    return {"ok": True, "slug": slug, "mode": mode, "path_before": old_path,
            "path_after": str(target), "entity": entity, "removed_old_dir": removed_old,
            "op_id": op_id if recorded else None, "notes": notes}


# ---------------------------------------------------------------------------
# 4) 解散项目（归属会话去哪必须显式选）
# ---------------------------------------------------------------------------

def preview_dissolve(home: Path, slug: str, *, to: str, target_slug: Optional[str] = None,
                     dir_action: str = DIR_KEEP) -> Dict[str, Any]:
    """解散预览：多少会话会被退回未分组 / 迁到哪、目录怎么处理。**零写入**。"""
    slug = str(slug or "").strip()
    to = str(to or "").strip()
    dir_action = str(dir_action or DIR_KEEP).strip()
    problems: List[str] = []
    notes: List[str] = []
    project = _require_project(home, slug)
    if to not in DISSOLVE_TARGETS:
        problems.append("必须显式选归属去向 to：%s（不允许默认丢归属）" % "/".join(DISSOLVE_TARGETS))
    if dir_action not in DIR_ACTIONS:
        problems.append("dir_action 只能是 " + "/".join(DIR_ACTIONS))
    path = ent.store_db(home)
    sessions: List[str] = []
    if path.is_file():
        conn = ent._connect_ro(path)
        try:
            sessions = [str(r["session_id"]) for r in conn.execute(
                "SELECT session_id FROM assignments WHERE project_slug = ?", (slug,))]
        finally:
            conn.close()
    if to == DISSOLVE_TO_PROJECT:
        target = str(target_slug or "").strip()
        if not target:
            problems.append("to=move 必须给 target_slug")
        elif target == slug:
            problems.append("target_slug 不能是自己")
        elif target not in _projects(home):
            problems.append("目标项目不存在：" + target)
        else:
            tpath = _projects(home)[target].get("primary_path")
            if not tpath or not Path(str(tpath)).is_dir():
                problems.append("目标项目没有可用目录，迁移后 cwd 无法对齐：" + str(tpath))
            notes.append("把 %d 条会话从 %s 迁到 %s（cwd 会对齐到 %s）"
                         % (len(sessions), slug, target, tpath))
    else:
        notes.append("把 %d 条会话退回未分组（删掉归属行，会话本身不动）" % len(sessions))
    if dir_action == DIR_DELETE_EMPTY:
        notes.append("项目目录若为空会删掉；非空则拒绝并保留")
    else:
        notes.append("项目目录保留（%s）" % project.get("primary_path"))
    notes.append("解散后项目 status 置 dissolved（不再出现在活跃项目里），projects 行保留可审计")
    return {"ok": not problems, "problems": problems, "notes": notes, "slug": slug, "to": to,
            "target_slug": target_slug, "dir_action": dir_action,
            "sessions": sessions, "session_count": len(sessions),
            "path": project.get("primary_path")}


def dissolve(home: Path, slug: str, *, to: str, target_slug: Optional[str] = None,
             dir_action: str = DIR_KEEP) -> Dict[str, Any]:
    """解散：归属按 ``to`` 处理（退回未分组 / 迁到 target），项目置 dissolved。"""
    plan = preview_dissolve(home, slug, to=to, target_slug=target_slug, dir_action=dir_action)
    if plan["problems"]:
        raise OpError("dissolve_rejected", "；".join(plan["problems"]), status=409,
                      extra={"preview": plan})
    to, dir_action = plan["to"], plan["dir_action"]
    sessions = plan["sessions"]
    project = _require_project(home, slug)
    store = ent.store_db(home)
    target_path = None
    if to == DISSOLVE_TO_PROJECT:
        target_path = _projects(home)[str(target_slug)].get("primary_path")
    moved, ungrouped, cwd_fail = [], [], []
    try:
        if sessions and store.is_file():
            conn = ent._connect_rw(store)
            try:
                with conn:
                    for sid in sessions:
                        if to == DISSOLVE_TO_UNGROUPED:
                            conn.execute("DELETE FROM assignments WHERE session_id = ?", (sid,))
                            ungrouped.append(sid)
                        else:
                            conn.execute("UPDATE assignments SET project_slug = ?, updated_at = ?"
                                         " WHERE session_id = ?", (target_slug, time.time(), sid))
                            moved.append(sid)
            finally:
                conn.close()
        # 迁移到别的项目 ⇒ cwd 必须跟着走（否则又制造一批不一致）
        if to == DISSOLVE_TO_PROJECT and target_path:
            for sid in moved:
                if not _write_cwd(home, sid, str(target_path)):
                    cwd_fail.append(sid)
    except Exception as exc:  # noqa: BLE001
        raise OpError("dissolve_failed", "解散失败（归属事务已回滚）：" + base._describe(exc),
                      status=500)
    conn = ent.ensure_attrs(home)
    try:
        with conn:
            ent.upsert_attrs(conn, slug, status="dissolved")
    except sqlite3.Error as exc:
        raise OpError("dissolve_failed", "归属已处理，但置 dissolved 失败：" + base._describe(exc),
                      status=500, extra={"moved": len(moved), "ungrouped": len(ungrouped)})
    finally:
        conn.close()
    left = _assign_counts(home).get(slug, 0)
    if left:
        raise OpError("orphan_assignments", "解散后仍有 %d 条归属指向 %s（无孤儿归属是硬要求）"
                      % (left, slug), status=500)
    removed_dir = False
    if dir_action == DIR_DELETE_EMPTY and plan["path"]:
        folder = Path(str(plan["path"]))
        if folder.is_dir() and ent.dir_is_empty(folder):
            try:
                folder.rmdir()
                removed_dir = True
            except OSError:
                removed_dir = False
    entity = ent.get_entity(home, slug)
    op_id = panel._new_id("op-")
    recorded = _ledger_header(home, op_id=op_id, op="projects.dissolve",
                              payload={"slug": slug, "to": to, "target_slug": target_slug,
                                       "dir_action": dir_action, "removed_dir": removed_dir,
                                       "moved": moved, "ungrouped": ungrouped})
    notes = ["项目 %s 已解散：%d 条会话迁到 %s，%d 条退回未分组"
             % (slug, len(moved), target_slug or "-", len(ungrouped))]
    if cwd_fail:
        notes.append("⚠ %d 条会话 cwd 同步失败（归属已迁，cwd 未跟）：%s"
                     % (len(cwd_fail), "、".join(cwd_fail[:5])))
    notes.append("目录%s" % ("已删（原来是空目录）" if removed_dir else "保留"))
    if not recorded:
        notes.append("已完成，但写台账失败（撤销将无记录）")
    return {"ok": True, "slug": slug, "to": to, "target_slug": target_slug,
            "moved": moved, "ungrouped": ungrouped, "cwd_sync_failed": cwd_fail,
            "removed_dir": removed_dir, "entity": entity,
            "op_id": op_id if recorded else None, "notes": notes}


# ---------------------------------------------------------------------------
# 5) 删除项目（与解散分开：删实体 + 目录删/留分别选）
# ---------------------------------------------------------------------------

def preview_delete(home: Path, slug: str, *, dir_action: str = DIR_KEEP) -> Dict[str, Any]:
    """删除预览。**有归属就不许删**（先解散或迁移），避免静默丢归属。"""
    slug = str(slug or "").strip()
    dir_action = str(dir_action or DIR_KEEP).strip()
    problems: List[str] = []
    notes: List[str] = []
    project = _require_project(home, slug)
    if dir_action not in DIR_ACTIONS:
        problems.append("dir_action 只能是 " + "/".join(DIR_ACTIONS))
    left = _assign_counts(home).get(slug, 0)
    if left:
        problems.append("项目下还有 %d 条归属，拒绝删除：先 dissolve（选会话去向）再删" % left)
    folder = project.get("primary_path")
    state = ent.dir_probe(Path(str(folder))) if folder else {"exists": False}
    if dir_action == DIR_DELETE_EMPTY and state.get("exists") and not state.get("empty"):
        problems.append("目录非空（%s 项），拒绝删（本接口只删空目录，绝不递归删）" % state.get("entries"))
    notes.append("将删除项目实体 %s（projects 行 + 文件夹行 + 属性行）" % slug)
    notes.append("目录 %s：%s" % (folder, "空则删除" if dir_action == DIR_DELETE_EMPTY else "保留"))
    return {"ok": not problems, "problems": problems, "notes": notes, "slug": slug,
            "dir_action": dir_action, "path": folder, "dir": state, "assignments": left}


def delete(home: Path, slug: str, *, dir_action: str = DIR_KEEP) -> Dict[str, Any]:
    """删实体：projects + project_folders + project_attrs。目录按 dir_action 处理。"""
    plan = preview_delete(home, slug, dir_action=dir_action)
    if plan["problems"]:
        raise OpError("delete_rejected", "；".join(plan["problems"]), status=409,
                      extra={"preview": plan})
    project = _require_project(home, slug)
    removed_dir, dir_error = False, None
    if plan["dir_action"] == DIR_DELETE_EMPTY and plan["path"]:
        folder = Path(str(plan["path"]))
        if folder.is_dir() and ent.dir_is_empty(folder):
            try:
                folder.rmdir()
                removed_dir = True
            except OSError as exc:
                dir_error = base._describe(exc)
    conn = ent.ensure_attrs(home)
    try:
        with conn:
            conn.execute("DELETE FROM project_folders WHERE project_id = ?", (project.get("id"),))
            conn.execute("DELETE FROM project_attrs WHERE slug = ?", (slug,))
            conn.execute("DELETE FROM projects WHERE id = ?", (project.get("id"),))
    except sqlite3.Error as exc:
        raise OpError("delete_failed", "删项目失败：" + base._describe(exc), status=500)
    finally:
        conn.close()
    if ent.get_entity(home, slug) is not None:
        raise OpError("readback_failed", "删除返回成功，但回读项目表里还有它", status=500)
    ledger = Path(home) / "auto-group" / "ai_projects.json"
    if ledger.exists():
        try:
            entries = json.loads(ledger.read_text(encoding="utf-8")) or []
        except (OSError, ValueError):
            entries = []
        rest = [e for e in entries if not (isinstance(e, dict) and e.get("slug") == slug)]
        if len(rest) != len(entries):
            base._atomic_write_json(ledger, rest)
    op_id = panel._new_id("op-")
    recorded = _ledger_header(home, op_id=op_id, op="projects.delete",
                              payload={"slug": slug, "path": plan["path"],
                                       "removed_dir": removed_dir, "dir_action": plan["dir_action"]})
    notes = ["已删除项目实体 %s；目录%s" % (slug, "已删（原来是空目录）" if removed_dir else "保留")]
    if dir_error:
        notes.append("删空目录失败（已保留）：" + dir_error)
    if not recorded:
        notes.append("已完成，但写台账失败（撤销将无记录）")
    return {"ok": True, "slug": slug, "removed_dir": removed_dir, "path": plan["path"],
            "op_id": op_id if recorded else None, "notes": notes}


# ---------------------------------------------------------------------------
# 6) 存量项目补属性行（created_by/status 之前不存在 —— 不推断，显式标 imported）
# ---------------------------------------------------------------------------

def preview_backfill_attrs(home: Path) -> Dict[str, Any]:
    """哪些项目还没有 ``project_attrs`` 行、补上会写什么。**零写入**。"""
    items = ent.list_entities(home)
    missing = [i for i in items if not i.get("attrs_present")]
    return {"ok": True, "missing": [i["slug"] for i in missing], "count": len(missing),
            "total": len(items),
            "would_write": {"created_by": "imported", "status": "active",
                            "note": "存量项目：建组来源未记录，按 imported 标注，不猜 manual/ai"},
            "notes": ([
                "%d / %d 个项目还没有属性行（created_by / status / color / sort_order 全空）"
                % (len(missing), len(items)),
                "补属性行只动 projects.db 的 project_attrs 侧表：不改 name / primary_path，"
                "不碰归属表与会话 cwd",
                "created_by 一律写 imported（存量未知来源），需要区分 manual/ai 请逐个项目改",
            ] if missing else ["所有项目都已有属性行，无需回填"])}


def backfill_attrs(home: Path, *, created_by: str = "imported",
                   slugs: Optional[List[str]] = None) -> Dict[str, Any]:
    """给缺属性行的项目补 ``project_attrs``。只补缺的，已有的一行都不动。"""
    if created_by not in ent.CREATED_BY:
        raise OpError("invalid_created_by", "created_by 只能是 " + "/".join(ent.CREATED_BY))
    plan = preview_backfill_attrs(home)
    targets = [s for s in plan["missing"] if not slugs or s in set(slugs)]
    if not targets:
        return {"ok": True, "noop": True, "written": [], "op_id": None,
                "notes": ["没有需要回填的项目（或 slugs 过滤后为空），什么都没写"]}
    conn = ent.ensure_attrs(home)
    try:
        with conn:
            for slug in targets:
                ent.upsert_attrs(conn, slug, created_by=created_by, status="active")
    except sqlite3.Error as exc:
        raise OpError("attrs_write_failed", "写 project_attrs 失败：" + base._describe(exc), status=500)
    finally:
        conn.close()
    items = {i["slug"]: i for i in ent.list_entities(home)}
    bad = [s for s in targets
           if not items.get(s) or not items[s].get("attrs_present")
           or items[s].get("created_by") != created_by]
    if bad:
        raise OpError("readback_failed", "回填返回成功，但回读属性行不符：" + "、".join(bad), status=500,
                      extra={"slug": bad})
    op_id = panel._new_id("op-")
    recorded = _ledger_header(home, op_id=op_id, op="projects.backfill_attrs",
                              payload={"slugs": targets, "created_by": created_by})
    notes = ["已给 %d 个项目补属性行（created_by=%s，status=active）" % (len(targets), created_by)]
    if not recorded:
        notes.append("属性已写，但写台账失败（撤销将无记录）")
    return {"ok": True, "written": targets, "created_by": created_by,
            "op_id": op_id if recorded else None, "notes": notes}


def _write_cwd(home: Path, session_id: str, value: Optional[str]) -> bool:
    """解散迁移时同步 cwd（与 align 模块同一实现口径：行不存在 ⇒ False）。"""
    path = Path(home) / "state.db"
    if not path.is_file():
        return False
    conn = ent._connect_rw(path)
    try:
        with conn:
            cur = conn.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (value, session_id))
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6) 反做（撤销入口，由 panel._undo_header_op 调用）
# ---------------------------------------------------------------------------

def reverse(home: Path, op: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把一条**项目级**台账 header 反做回写前状态。不认识的 op 返回 None。

    ``projects.create`` / ``projects.delete`` 属于「实体生死」，**不自动反做**：目录可能已被
    用户塞了东西，自动删/建目录风险太高。这两类只如实说明，交给人工。
    """
    op = str(op or "")
    if op == "projects.rename_name":
        slug = str(payload.get("slug") or "")
        before = payload.get("name_before")
        if not slug or before is None:
            return {"op": op, "applied": None, "reason": "台账缺 slug/name_before，无法反做"}
        conn = ent.ensure_attrs(home)
        try:
            with conn:
                conn.execute("UPDATE projects SET name = ? WHERE slug = ?", (before, slug))
        finally:
            conn.close()
        entity = ent.get_entity(home, slug)
        if entity is None or entity.get("name") != before:
            return {"op": op, "applied": None, "reason": "反做后回读显示名不符"}
        return {"op": op, "applied": {"slug": slug, "name": before}, "kind": "reverse-name"}
    if op == "projects.rename_slug":
        slug_before = str(payload.get("slug_before") or "")
        slug_after = str(payload.get("slug_after") or "")
        path_before = payload.get("path_before")
        path_after = payload.get("path_after")
        if not slug_before or not slug_after:
            return {"op": op, "applied": None, "reason": "台账缺 slug_before/after"}
        moved_back = False
        if payload.get("moved_dir") and path_after and path_before and Path(str(path_after)).is_dir() \
                and not Path(str(path_before)).exists():
            try:
                shutil.move(str(path_after), str(path_before))
                moved_back = True
            except OSError as exc:
                return {"op": op, "applied": None, "reason": "目录迁回失败：" + base._describe(exc)}
        conn = ent.ensure_attrs(home)
        try:
            with conn:
                conn.execute("UPDATE projects SET slug = ?, primary_path = ? WHERE slug = ?",
                             (slug_before, path_before, slug_after))
                conn.execute("UPDATE project_folders SET path = ? WHERE path = ?",
                             (path_before, path_after))
                conn.execute("UPDATE project_attrs SET slug = ? WHERE slug = ?",
                             (slug_before, slug_after))
        finally:
            conn.close()
        store = ent.store_db(home)
        restored = 0
        if store.is_file():
            sconn = ent._connect_rw(store)
            try:
                with sconn:
                    cur = sconn.execute("UPDATE assignments SET project_slug = ?, updated_at = ?"
                                        " WHERE project_slug = ?", (slug_before, time.time(), slug_after))
                    restored = int(cur.rowcount or 0)
            finally:
                sconn.close()
        if ent.get_entity(home, slug_before) is None:
            return {"op": op, "applied": None, "reason": "反做后回读没有旧 slug"}
        return {"op": op, "applied": {"slug": slug_before, "path": path_before,
                                     "assignments_restored": restored,
                                     "dir_moved_back": moved_back}, "kind": "reverse-slug"}
    if op == "projects.change_path":
        slug = str(payload.get("slug") or "")
        before, after = payload.get("path_before"), payload.get("path_after")
        if not slug or before is None:
            return {"op": op, "applied": None, "reason": "台账缺 slug/path_before"}
        moved_back = False
        if payload.get("mode") == PATH_MIGRATE and after and Path(str(after)).is_dir() \
                and not Path(str(before)).exists():
            try:
                shutil.move(str(after), str(before))
                moved_back = True
            except OSError as exc:
                return {"op": op, "applied": None, "reason": "目录迁回失败：" + base._describe(exc)}
        conn = ent.ensure_attrs(home)
        try:
            with conn:
                conn.execute("UPDATE projects SET primary_path = ? WHERE slug = ?", (before, slug))
                conn.execute("UPDATE project_folders SET path = ? WHERE path = ?", (before, after))
        finally:
            conn.close()
        entity = ent.get_entity(home, slug)
        if entity is None or str(entity.get("path")) != str(before):
            return {"op": op, "applied": None, "reason": "反做后回读 primary_path 不符"}
        return {"op": op, "applied": {"slug": slug, "path": before, "dir_moved_back": moved_back},
                "kind": "reverse-path"}
    if op == "projects.backfill_attrs":
        slugs = list(payload.get("slugs") or [])
        if not slugs:
            return {"op": op, "applied": None, "reason": "台账缺 slugs，无法反做"}
        conn = ent.ensure_attrs(home)
        removed = 0
        try:
            with conn:
                for slug in slugs:
                    cur = conn.execute("DELETE FROM project_attrs WHERE slug = ?", (slug,))
                    removed += int(cur.rowcount or 0)
        finally:
            conn.close()
        left = [s for s in slugs if (ent.get_entity(home, s) or {}).get("attrs_present")]
        if left:
            return {"op": op, "applied": None, "reason": "反做后仍有属性行：" + "、".join(left)}
        return {"op": op, "applied": {"slugs": slugs, "attrs_removed": removed},
                "kind": "reverse-backfill-attrs"}
    if op == "projects.dissolve":
        slug = str(payload.get("slug") or "")
        moved = list(payload.get("moved") or [])
        ungrouped = list(payload.get("ungrouped") or [])
        if not slug:
            return {"op": op, "applied": None, "reason": "台账缺 slug"}
        store = ent.store_db(home)
        restored = 0
        if store.is_file():
            sconn = ent._connect_rw(store)
            try:
                with sconn:
                    for sid in moved:
                        cur = sconn.execute("UPDATE assignments SET project_slug = ?, updated_at = ?"
                                            " WHERE session_id = ?", (slug, time.time(), sid))
                        restored += int(cur.rowcount or 0)
            finally:
                sconn.close()
        # ungrouped 是**删掉的行**：台账里没有整行快照，只能报出来让人工确认
        conn = ent.ensure_attrs(home)
        try:
            with conn:
                ent.upsert_attrs(conn, slug, status="active")
        finally:
            conn.close()
        note = ("已把 %d 条迁出的归属改回 %s、项目恢复 active" % (restored, slug))
        if ungrouped:
            note += ("；但 %d 条**退回未分组**的归属行是删除操作，台账没有整行快照，"
                     "无法自动重建：%s" % (len(ungrouped), "、".join(ungrouped[:8])))
        return {"op": op, "applied": {"slug": slug, "restored": restored,
                                      "unrecoverable_ungrouped": ungrouped},
                "kind": "reverse-dissolve", "reason": note}
    return None

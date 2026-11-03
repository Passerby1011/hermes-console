# -*- coding: utf-8 -*-
"""沙盒：给「真项目化」写卡造一个假 home（projects.db / assignments.db / state.db 全在临时目录）。

**这是测试用假数据**，不是生产库快照。与 ``ai_sandbox.Sandbox`` 的区别：那个偏 AI 分类链路，
这个偏**项目实体 + 归属/cwd 一致性**，所以额外提供：

* ``add_session(source=...)`` —— 造 ``source='kanban'`` 的看板会话（硬约束断言要用）
* ``set_cwd()`` / ``cwd_of()`` —— 直接摆布会话 cwd
* ``add_assignment()`` —— 摆布归属行（含 status）
* ``add_project(create_dir=..., path=...)`` —— 摆布项目实体
* ``snapshot()`` —— 三张库 + 目录树的**逐字节**快照，用来断言「预览零写入」
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DASH = Path(__file__).resolve().parents[1]
if str(DASH) not in sys.path:
    sys.path.insert(0, str(DASH))

import api_autogroup  # noqa: E402

SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, source TEXT, user_id TEXT, session_key TEXT, chat_id TEXT, chat_type TEXT,
  thread_id TEXT, display_name TEXT, title TEXT, profile_name TEXT, cwd TEXT, message_count INTEGER,
  started_at REAL, ended_at REAL, last_activity_at REAL, archived INTEGER NOT NULL DEFAULT 0,
  hidden INTEGER NOT NULL DEFAULT 0
);
"""

PROJECTS_DDL = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL, description TEXT, icon TEXT,
  color TEXT, board_slug TEXT, primary_path TEXT, created_at INTEGER NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS project_folders (
  project_id TEXT NOT NULL, path TEXT NOT NULL, label TEXT, is_primary INTEGER NOT NULL DEFAULT 0,
  added_at INTEGER NOT NULL, PRIMARY KEY (project_id, path)
);
"""


def body(resp: Any) -> Dict[str, Any]:
    if isinstance(resp, dict):
        return resp
    raw = getattr(resp, "body", b"{}")
    return json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw))


def status_of(resp: Any) -> int:
    return int(getattr(resp, "status_code", 200))


class ProjectSandbox:
    """临时 home + 三张库 + 已装好的 ``_home`` 补丁。"""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="proj-sandbox-"))
        self.home = self.root / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.projects_root = self.home / "projects"
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self._orig_home = api_autogroup._home
        self._make_state_db()
        self._make_projects_db()
        self._make_store()

    # ---------------------------------------------------------------- 建库
    def _make_state_db(self) -> None:
        conn = sqlite3.connect(str(self.home / "state.db"))
        try:
            conn.executescript(SESSIONS_DDL)
            conn.commit()
        finally:
            conn.close()

    def _make_projects_db(self) -> None:
        conn = sqlite3.connect(str(self.home / "projects.db"))
        try:
            conn.executescript(PROJECTS_DDL)
            conn.commit()
        finally:
            conn.close()

    def _make_store(self) -> None:
        core = api_autogroup._load_core()
        conn = core.ensure_store(self.home)
        conn.close()

    # ---------------------------------------------------------------- 造数据
    def add_session(self, session_id: str, title: str, cwd: Optional[str],
                    source: str = "desktop", message_count: int = 4) -> None:
        conn = sqlite3.connect(str(self.home / "state.db"))
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO sessions(id, source, display_name, title, profile_name,"
                    " cwd, message_count, started_at, last_activity_at, archived)"
                    " VALUES(?,?,?,?,?,?,?,?,?,0)",
                    (session_id, source, title, title, "default", cwd, message_count,
                     time.time() - 100, time.time() - 10))
        finally:
            conn.close()

    def set_cwd(self, session_id: str, cwd: Optional[str]) -> None:
        conn = sqlite3.connect(str(self.home / "state.db"))
        try:
            with conn:
                conn.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (cwd, session_id))
        finally:
            conn.close()

    def cwd_of(self, session_id: str) -> Optional[str]:
        conn = sqlite3.connect("file:" + str(self.home / "state.db") + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT cwd FROM sessions WHERE id = ?", (session_id,)).fetchone()
            return row["cwd"] if row is not None else None
        finally:
            conn.close()

    def add_project(self, slug: str, name: str, *, create_dir: bool = True,
                    path: Optional[str] = None, created_by: Optional[str] = None,
                    status: Optional[str] = None) -> Path:
        folder = Path(path) if path else self.projects_root / slug
        if create_dir:
            folder.mkdir(parents=True, exist_ok=True)
        project_id = "p_" + slug[:10]
        conn = sqlite3.connect(str(self.home / "projects.db"))
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO projects(id, slug, name, description, primary_path,"
                    " created_at, archived) VALUES(?,?,?,?,?,?,0)",
                    (project_id, slug, name, "fixture 项目", str(folder), int(time.time())))
                conn.execute(
                    "INSERT OR REPLACE INTO project_folders(project_id, path, label, is_primary,"
                    " added_at) VALUES(?,?,?,1,?)", (project_id, str(folder), "main", int(time.time())))
                if created_by or status:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS project_attrs (slug TEXT PRIMARY KEY,"
                        " created_by TEXT NOT NULL DEFAULT 'manual',"
                        " status TEXT NOT NULL DEFAULT 'active',"
                        " sort_order INTEGER NOT NULL DEFAULT 0, color TEXT, note TEXT,"
                        " created_at REAL NOT NULL, updated_at REAL NOT NULL)")
                    conn.execute(
                        "INSERT OR REPLACE INTO project_attrs(slug, created_by, status, sort_order,"
                        " created_at, updated_at) VALUES(?,?,?,0,?,?)",
                        (slug, created_by or "manual", status or "active",
                         time.time(), time.time()))
        finally:
            conn.close()
        return folder

    def add_assignment(self, session_id: str, slug: str, *, status: str = "applied",
                       confidence: float = 1.0, reason: str = "fixture",
                       source: str = "panel", batch_id: Optional[str] = None) -> None:
        core = api_autogroup._load_core()
        conn = core.ensure_store(self.home)
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO assignments(session_id, project_slug, confidence, reason,"
                    " source, status, batch_id, applied_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (session_id, slug, confidence, reason, source, status, batch_id,
                     time.time() if status == "applied" else None, time.time()))
        finally:
            conn.close()

    # ---------------------------------------------------------------- 读数据
    def assignment(self, session_id: str) -> Optional[Dict[str, Any]]:
        conn = sqlite3.connect("file:" + str(self.home / "auto-group" / "assignments.db")
                               + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM assignments WHERE session_id = ?",
                               (session_id,)).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def assignments(self) -> List[Dict[str, Any]]:
        conn = sqlite3.connect("file:" + str(self.home / "auto-group" / "assignments.db")
                               + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM assignments")]
        finally:
            conn.close()

    def project_row(self, slug: str) -> Optional[Dict[str, Any]]:
        conn = sqlite3.connect("file:" + str(self.home / "projects.db") + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def attrs_row(self, slug: str) -> Optional[Dict[str, Any]]:
        conn = sqlite3.connect("file:" + str(self.home / "projects.db") + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "project_attrs" not in names:
                return None
            row = conn.execute("SELECT * FROM project_attrs WHERE slug = ?", (slug,)).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def set_attrs(self, slug: str, *, created_by: str = "manual", status: str = "active",
                  sort_order: int = 0, color: Optional[str] = None,
                  note: Optional[str] = None) -> None:
        """直接摆布 ``project_attrs`` 行（模拟「AI 建的项目」这类已有属性的情形）。"""
        conn = sqlite3.connect(str(self.home / "projects.db"))
        try:
            with conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS project_attrs ("
                    " slug TEXT PRIMARY KEY, created_by TEXT NOT NULL DEFAULT 'manual',"
                    " status TEXT NOT NULL DEFAULT 'active', sort_order INTEGER NOT NULL DEFAULT 0,"
                    " color TEXT, note TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
                conn.execute(
                    "INSERT OR REPLACE INTO project_attrs(slug, created_by, status, sort_order,"
                    " color, note, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (slug, created_by, status, sort_order, color, note, time.time(), time.time()))
        finally:
            conn.close()

    def console_ops(self, op_id: str) -> List[Dict[str, Any]]:
        conn = sqlite3.connect("file:" + str(self.home / "auto-group" / "assignments.db")
                               + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM console_ops WHERE op_id = ? ORDER BY seq", (op_id,))]
        finally:
            conn.close()

    # ---------------------------------------------------------------- 快照
    def snapshot(self) -> Dict[str, str]:
        """三张库 + 项目目录树的逐字节指纹（文件名 → sha256）。"""
        out: Dict[str, str] = {}
        for name in ("state.db", "projects.db", "auto-group/assignments.db"):
            path = self.home / name
            if path.is_file():
                out[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        for folder in sorted(self.projects_root.rglob("*")):
            rel = "tree/" + folder.relative_to(self.projects_root).as_posix()
            out[rel] = "dir" if folder.is_dir() else hashlib.sha256(folder.read_bytes()).hexdigest()
        return out

    # ---------------------------------------------------------------- 生命周期
    def __enter__(self) -> "ProjectSandbox":
        api_autogroup._home = lambda: self.home  # type: ignore[assignment]
        return self

    def __exit__(self, *exc_info: Any) -> None:
        api_autogroup._home = self._orig_home  # type: ignore[assignment]
        shutil.rmtree(self.root, ignore_errors=True)

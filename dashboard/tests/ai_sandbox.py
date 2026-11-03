# -*- coding: utf-8 -*-
"""**沙盒 fixture**：给 AI 分类写卡造一个假 home（state.db / projects.db / assignments.db 全在
临时目录里），绝不碰真实 home。

明确标注：这是**测试用假数据**，不是生产库快照。三张库都是临时目录里现建的：
``state.db``（sessions 表）、``projects.db``（projects + project_folders）、
``auto-group/assignments.db``（由插件 core.ensure_store 建表后灌样例行）。

用法::

    with Sandbox() as sb:
        sb.add_session("s1", "标题", cwd=r"D:\\work\\a")
        sb.add_project("memory", "记忆")
        sb.add_assignment("protected", "quick-send", status="applied")
        resp = api_autogroup_ai_apply.post_ai_preview({"preview": {...}})
"""
from __future__ import annotations

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
import api_autogroup_ai_apply  # noqa: E402
import api_autogroup_ai_undo  # noqa: E402
import api_autogroup_panel  # noqa: E402
import autogroup_ai_contract  # noqa: E402
import autogroup_ai_projects  # noqa: E402
import autogroup_ai_store  # noqa: E402

SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, source TEXT, user_id TEXT, session_key TEXT, chat_id TEXT, chat_type TEXT,
  thread_id TEXT, display_name TEXT, title TEXT, profile_name TEXT, cwd TEXT, message_count INTEGER,
  started_at REAL, ended_at REAL, last_activity_at REAL, archived INTEGER NOT NULL DEFAULT 0,
  hidden INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT, created_at REAL
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
    """路由可能回 dict，也可能回 JSONResponse —— 统一取 body。"""
    if isinstance(resp, dict):
        return resp
    raw = getattr(resp, "body", b"{}")
    return json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw))


def status_of(resp: Any) -> int:
    return int(getattr(resp, "status_code", 200))


class Sandbox:
    """临时 home + 三张库 + 已装好的 ``_home`` 补丁。"""

    def __init__(self, *, applied_fixture: int = 3):
        self.root = Path(tempfile.mkdtemp(prefix="ai-sandbox-"))
        self.home = self.root / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self._orig_home = api_autogroup._home
        self._make_state_db()
        self._make_projects_db()
        self._make_assignments(applied_fixture)

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

    def _make_assignments(self, applied_fixture: int) -> None:
        core = api_autogroup._load_core()
        conn = core.ensure_store(self.home)
        try:
            with conn:
                for index in range(applied_fixture):
                    conn.execute(
                        "INSERT OR REPLACE INTO assignments(session_id, project_slug, confidence, reason,"
                        " source, status, batch_id, applied_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        ("protected-%d" % index, "quick-send", 1.0, "原 56 条之一（fixture）",
                         "rules", "applied", "bg-fixture", time.time(), time.time()))
        finally:
            conn.close()

    # ---------------------------------------------------------------- 造数据
    def add_session(self, session_id: str, title: str, cwd: Optional[str],
                    message_count: int = 4, source: str = "desktop") -> None:
        conn = sqlite3.connect(str(self.home / "state.db"))
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO sessions(id, source, display_name, title, profile_name, cwd,"
                    " message_count, started_at, last_activity_at, archived) VALUES(?,?,?,?,?,?,?,?,?,0)",
                    (session_id, source, title, title, "default", cwd, message_count,
                     time.time() - 100, time.time() - 10))
        finally:
            conn.close()

    def add_project(self, slug: str, name: str, *, create_dir: bool = True,
                    path: Optional[str] = None) -> Path:
        """加一个项目（可选真建目录）。"""
        return self._add_project(slug, name, create_dir=create_dir, path=path)

    def add_message(self, session_id: str, content: str, role: str = "user") -> None:
        """给会话加一条消息（引擎摘要只读 role='user' 的前几条）。"""
        conn = sqlite3.connect(str(self.home / "state.db"))
        try:
            with conn:
                conn.execute(
                    "INSERT INTO messages(session_id, role, content, created_at) VALUES(?,?,?,?)",
                    (session_id, role, content, time.time()))
        finally:
            conn.close()

    def add_dialog(self, session_id: str, *user_messages: str) -> None:
        """按顺序灌几条用户消息（人工合成）。"""
        for text in user_messages:
            self.add_message(session_id, text, "user")

    def _add_project(self, slug: str, name: str, *, create_dir: bool = True,
                    path: Optional[str] = None) -> Path:
        folder = Path(path) if path else self.home / "projects" / slug
        if create_dir:
            folder.mkdir(parents=True, exist_ok=True)
        project_id = "p_" + slug[:8]
        conn = sqlite3.connect(str(self.home / "projects.db"))
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO projects(id, slug, name, description, primary_path,"
                    " created_at, archived) VALUES(?,?,?,?,?,?,0)",
                    (project_id, slug, name, "fixture 项目", str(folder), int(time.time())))
                conn.execute(
                    "INSERT OR REPLACE INTO project_folders(project_id, path, label, is_primary, added_at)"
                    " VALUES(?,?,?,1,?)", (project_id, str(folder), "main", int(time.time())))
        finally:
            conn.close()
        return folder

    def add_assignment(self, session_id: str, slug: str, *, status: str = "suggested",
                       confidence: float = 0.4, reason: str = "rules 命中", source: str = "rules",
                       batch_id: Optional[str] = None) -> None:
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
        path = self.home / "auto-group" / "assignments.db"
        conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM assignments WHERE session_id = ?", (session_id,)).fetchone()
            return dict(row) if row is not None else None
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

    def console_ops(self, op_id: str) -> List[Dict[str, Any]]:
        path = self.home / "auto-group" / "assignments.db"
        conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM console_ops WHERE op_id = ? ORDER BY seq", (op_id,))]
        finally:
            conn.close()

    def batch_row(self, batch_id: str) -> Optional[Dict[str, Any]]:
        path = self.home / "auto-group" / "assignments.db"
        conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM ai_preview_batches WHERE batch_id = ?",
                               (batch_id,)).fetchone()
            return dict(row) if row is not None else None
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

    # ---------------------------------------------------------------- 生命周期
    def __enter__(self) -> "Sandbox":
        api_autogroup._home = lambda: self.home  # type: ignore[assignment]
        return self

    def __exit__(self, *exc_info: Any) -> None:
        api_autogroup._home = self._orig_home  # type: ignore[assignment]
        shutil.rmtree(self.root, ignore_errors=True)

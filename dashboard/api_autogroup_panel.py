# -*- coding: utf-8 -*-
"""hermes-console · 项目分组**面板写操作**（t_43c5fd05 阶段2）：projects / assign / ops / auto。

本文件是 ``api_autogroup.py``（只读查询 + 老的 apply/undo）的**写操作扩展**。单独成文件是因为
主文件已 39KB，再往里塞写操作就没法审阅了。挂载方式：``plugin_api.DOMAIN_MODULES`` 里排在
``api_autogroup`` 之后，两个 ``router`` 并进同一个插件前缀，最终 URL 不变：
``/api/plugins/hermes-console/autogroup/...``。

**不实现任何分组判定**：判定/存储/项目表全部复用 ``plugins/hermes-auto-group/core.py``
（``list_projects`` / ``load_rules`` / ``ensure_store`` / ``connect_ro`` / ``create_project`` /
``slugify``）。本文件只做四件事：参数校验 → **写前快照** → 写 → **回读核对**。

回滚台账 ``console_ops``（建在 ``auto-group/assignments.db`` 里，本文件首次用到时补建）：
每个写操作一行 ``kind='header'``，其下**逐会话**一行 ``kind='session'``，记
``row_before``（写入前 ``assignments`` 的**整行** JSON，NULL = 写入前根本没有这一行）、
``cwd_before``（``state.db`` 里 ``sessions.cwd`` 的原值）、``cwd_before_known``（那行会话是否
存在，不存在就不许假装知道原值）、``cwd_written``（这次是否真写了 cwd）、``slug_before/after``。
``POST /autogroup/undo`` 带 ``op_id`` 时走 :func:`_undo_panel_op`：**逐字段还原整行**（原来没有
就删掉）+ cwd 还原成原值 —— 不是把 status 改回 suggested，那会把「本来就归在 A 项目」的会话
误退回未归入。``_op_id_for_batch`` 让老的 ``batch_id`` 调用也能落到台账上。

路由：``GET/POST/PATCH/DELETE projects``｜``POST assign``｜``POST switch``｜``GET ops``｜
``GET/POST auto``｜``GET auto/daemon``｜``POST auto/tick``。老 ``POST /autogroup/undo`` 的 ``op_id``
分支由本模块扩展（``api_autogroup.post_undo`` 里显式 import 这两个名字；此前它们全文件未定义
⇒ 带 op_id 必 NameError）。

自动归组的**引擎与周期触发**在 ``autogroup_auto.py`` / ``autogroup_auto_daemon.py``（t_0c8ab8c1）：
本文件的 ``POST auto/tick`` 只是它的 HTTP 入口，``POST auto`` 打开开关时会
:func:`autogroup_auto.ensure_daemon` 把守护进程拉起来 —— 关闭状态下不会拉起任何进程。

"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Query  # type: ignore
from fastapi.responses import JSONResponse  # type: ignore

_DASHBOARD_DIR = Path(__file__).resolve().parent
if str(_DASHBOARD_DIR) not in sys.path:
    # 单独 import 本模块（探针/测试）时也要能找到兄弟模块 api_autogroup。
    sys.path.insert(0, str(_DASHBOARD_DIR))

import api_autogroup as base  # noqa: E402 - 复用 _home/_fail/_load_core/_session_meta，不复制
import autogroup_auto as auto_engine  # noqa: E402 - 自动归组引擎（t_0c8ab8c1 B 块）

router = APIRouter()

AUTO_STATE_NAME = "auto_mode.json"      # home/auto-group/auto_mode.json
AUTO_DEFAULT_INTERVAL = 30              # 分钟
OPS_MAX_LIMIT = 200
ASSIGN_MAX = 200                        # 一次最多归入多少条
AUTO_TICK_MAX = 40
#: 看板任务会话来源（硬约束：不得进入普通项目）。
KANBAN_SOURCE = "kanban"
#: assignments 表的全部列（逐字段还原要用；顺序无关，但必须是全集）。
ASSIGN_COLUMNS = ("session_id", "project_slug", "confidence", "reason", "source", "status",
                  "batch_id", "applied_at", "updated_at")

#: 面板回滚台账。**一张表两种行**：header（操作级）+ session（逐会话写入前整行 + cwd 原值）。
CONSOLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS console_ops (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  op_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  op TEXT NOT NULL,
  session_id TEXT,
  batch_id TEXT,
  row_before TEXT,
  cwd_before TEXT,
  cwd_before_known INTEGER NOT NULL DEFAULT 0,
  cwd_written INTEGER NOT NULL DEFAULT 0,
  slug_before TEXT,
  slug_after TEXT,
  payload TEXT,
  note TEXT,
  created_at REAL NOT NULL,
  undone INTEGER NOT NULL DEFAULT 0,
  undone_at REAL
);
CREATE INDEX IF NOT EXISTS idx_console_ops_op ON console_ops(op_id);
CREATE INDEX IF NOT EXISTS idx_console_ops_batch ON console_ops(batch_id);
"""


def _store(home: Path) -> Tuple[Any, sqlite3.Connection]:
    """打开 ``assignments.db``（不存在按 core.SCHEMA 建）+ 补建 ``console_ops`` 台账表。"""
    core = base._load_core()
    conn = core.ensure_store(home)
    conn.executescript(CONSOLE_SCHEMA)
    conn.commit()
    return core, conn


# ---------------------------------------------------------------------------
# agent 作用域（t_6f525a71）
# ---------------------------------------------------------------------------
# 归属库/项目库/规则库/auto 状态**全部相对 home 算**（core.store_path(home)、
# core.list_projects(home)、_auto_state_path(home)），所以隔离只需要一个动作：
# 把路由拿到的 home 换成「该 agent 的 home」。以前这里 10 条路由一律 ``base._home()``
# ⇒ 面板选 work 也写进小白的根归属库（本卡要消灭的结构性根因）。

def _agent_of(agent: Any, payload: Optional[Dict[str, Any]] = None) -> str:
    """从 body ``agent`` 或 query ``agent`` 取 agent 名；都没有 ⇒ 空（= default/根库）。

    FastAPI 直接调路由函数时 ``agent`` 可能是 ``Query`` 对象，非 str 一律当未指定。
    """
    raw = (payload or {}).get("agent")
    if raw is None or raw == "":
        raw = agent if isinstance(agent, str) else ""
    return str(raw or "").strip()


def _read_home(agent: str) -> Tuple[Path, Optional[Any]]:
    """读路由 home：``(home, error)``。``agent=__all__`` 没有单一库 ⇒ 400，不拼假清单。"""
    if base.is_all_agent(agent):
        return Path(""), base._fail(
            "agent_required",
            "读归属/项目/自动模式必须先选定一个 agent：agent=__all__ 没有单一库，"
            "本接口不会把多个 agent 的记录拼成一份。",
            extra={"needs_agent": True})
    return base.agent_home(agent, base._home()), None


def _write_home(agent: str) -> Any:
    """写路由 home：``(agent, home)``；``__all__`` ⇒ 400 ``agent_required``。"""
    try:
        return base._scope(agent, base._home())
    except base.ScopedWrite as exc:
        return base._fail(exc.code, exc.message,
                          extra={"agent": str(agent or ""), "needs_agent": True})


def _new_id(prefix: str) -> str:
    return prefix + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _project_root(home: Path) -> Path:
    """项目目录根。**从既有项目的 primary_path 反推**（懒 import，避免循环依赖）。

    ``core.project_root`` 是 ``<home>/projects``，但真实 17 个项目全在 ``<home 的兄弟目录>/
    projects``（``D:\\hermes-data\\projects``）。照 core 的算法建项目会造出第二套目录根，
    正是「面板说目录存在、实际路径对不上」的根因（t_53b48743 ②）。
    """
    try:
        import autogroup_projects as ent
        return ent.project_root(home)
    except Exception:  # noqa: BLE001 - 反推失败就退回 core 的老口径，不阻断建项目
        return base._load_core().project_root(home)


def _assign_row(conn: sqlite3.Connection, session_id: str) -> Optional[Dict[str, Any]]:
    """写入前整行（NULL = 这一行原本不存在，撤销时应当删掉）。"""
    row = conn.execute("SELECT " + ", ".join(ASSIGN_COLUMNS) + " FROM assignments WHERE session_id = ?",
                       (session_id,)).fetchone()
    return dict(row) if row is not None else None


def _read_cwd(home: Path, session_id: str) -> Tuple[bool, Optional[str]]:
    """``(会话行是否存在, cwd 原值)``。读不到就是「不知道」，不猜、不填 0。"""
    path = Path(home) / "state.db"
    if not path.is_file():
        return False, None
    try:
        conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT cwd FROM sessions WHERE id = ?", (session_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False, None
    return (True, row["cwd"]) if row is not None else (False, None)


def _write_cwd(home: Path, session_id: str, value: Optional[str]) -> bool:
    """只改这一行的 cwd。返回是否真的更新到了行。"""
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


def _record_header(conn: sqlite3.Connection, *, op_id: str, op: str, payload: Optional[Dict[str, Any]] = None,
                   note: Optional[str] = None, batch_id: Optional[str] = None,
                   created_at: Optional[float] = None) -> None:
    conn.execute(
        "INSERT INTO console_ops(op_id, kind, op, batch_id, payload, note, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (op_id, "header", op, batch_id,
         json.dumps(payload, ensure_ascii=False) if payload is not None else None,
         note, created_at or time.time()))


def _record_session(conn: sqlite3.Connection, *, op_id: str, op: str, session_id: str,
                    row_before: Optional[Dict[str, Any]], cwd_known: bool, cwd_before: Optional[str],
                    cwd_written: bool, slug_before: Optional[str], slug_after: Optional[str],
                    batch_id: Optional[str], created_at: float) -> None:
    conn.execute(
        "INSERT INTO console_ops(op_id, kind, op, session_id, batch_id, row_before, cwd_before,"
        " cwd_before_known, cwd_written, slug_before, slug_after, created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (op_id, "session", op, session_id, batch_id,
         json.dumps(row_before, ensure_ascii=False) if row_before is not None else None,
         cwd_before, 1 if cwd_known else 0, 1 if cwd_written else 0,
         slug_before, slug_after, created_at))


def _restore_assign_row(conn: sqlite3.Connection, session_id: str,
                        row_before: Optional[Dict[str, Any]]) -> str:
    """逐字段还原：原来没有这一行就删掉，否则整行写回（INSERT OR REPLACE 覆盖全部列）。"""
    if row_before is None:
        conn.execute("DELETE FROM assignments WHERE session_id = ?", (session_id,))
        return "deleted"
    cols = [c for c in ASSIGN_COLUMNS if c in row_before]
    conn.execute(
        "INSERT OR REPLACE INTO assignments(" + ", ".join(cols) + ")"
        " VALUES(" + ", ".join("?" * len(cols)) + ")",
        [row_before[c] for c in cols])
    return "restored"


def _verify_rows(home: Path, expectations: List[Tuple[str, Optional[Dict[str, Any]]]]) -> Dict[str, Any]:
    """回读核对：逐会话把当前整行与「写入前整行」逐字段比。不信写入返回值。"""
    core = base._load_core()
    path = core.store_path(home)
    out: Dict[str, Any] = {"checked": 0, "mismatched": []}
    conn = core.connect_ro(path)
    try:
        for session_id, row_before in expectations:
            got = _assign_row(conn, session_id)
            out["checked"] += 1
            if row_before is None:
                if got is not None:
                    out["mismatched"].append({"session_id": session_id,
                                              "why": "写入前没有这一行，撤销后却还在", "now": got})
                continue
            if got is None:
                out["mismatched"].append({"session_id": session_id, "why": "写入前有这一行，撤销后不见了"})
                continue
            diff = {k: {"before": row_before.get(k), "now": got.get(k)}
                    for k in ASSIGN_COLUMNS if row_before.get(k) != got.get(k)}
            if diff:
                out["mismatched"].append({"session_id": session_id, "why": "字段不一致", "diff": diff})
    finally:
        conn.close()
    return out


def _op_id_for_batch(home: Path, batch_id: str) -> str:
    """老调用只带 ``batch_id``：查台账反查 op_id。台账没建/没这批次 ⇒ 空串（走老路径）。"""
    batch_id = str(batch_id or "").strip()
    if not batch_id:
        return ""
    try:
        core = base._load_core()
        path = core.store_path(home)
        if not path.exists():
            return ""
        conn = core.connect_ro(path)
        try:
            names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "console_ops" not in names:
                return ""
            row = conn.execute("SELECT op_id FROM console_ops WHERE batch_id = ? AND kind = 'session'"
                               " ORDER BY seq LIMIT 1", (batch_id,)).fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - 反查失败就当没有面板台账，退回老 batch 路径
        return ""
    return str(row["op_id"]) if row is not None else ""


def _undo_header_op(home: Path, header: Dict[str, Any]) -> Dict[str, Any]:
    """无逐会话行的操作（改名 / 自动模式 / 项目级操作）用 payload 里的 before 反做。"""
    op = str(header.get("op") or "")
    payload = json.loads(header["payload"]) if header.get("payload") else {}
    if op == "projects.rename":
        res = _rename_project(home, str(payload.get("slug_after") or ""),
                              name=payload.get("name_before"), new_slug=payload.get("slug_before"))
        return {"op": op, "applied": res, "kind": "reverse-rename"}
    if op == "auto.set":
        before = dict(payload.get("before") or {})
        _write_auto_state(home, before)
        return {"op": op, "applied": {"enabled": bool(before.get("enabled"))}, "kind": "restore-state"}
    # 项目级操作（t_53b48743）：改名显示名 / 改 slug / 改目录 / 解散，反做逻辑在
    # autogroup_project_ops.reverse（懒 import，避免模块级循环依赖）。
    try:
        import autogroup_project_ops as project_ops
        reversed_op = project_ops.reverse(home, op, payload)
    except Exception as exc:  # noqa: BLE001 - 反做失败要如实说，不能假装没这回事
        return {"op": op, "applied": None, "reason": "项目级反做失败：" + base._describe(exc)}
    if reversed_op is not None:
        return reversed_op
    return {"op": op, "applied": None,
            "reason": "该操作不自动反做（建项目/删项目只登记台账；项目与目录请用 projects 路由手动处理）"}


def _undo_panel_op(home: Path, op_id: str) -> Any:
    """撤销一次面板写操作 → 逐字段还原 + cwd 还原 → 回读核对。

    **这就是 ``POST /autogroup/undo`` 带 ``op_id`` 时走的那条路**（此前该名字在全仓不存在，
    带 op_id 必 500 NameError）。绝不把 status 一律改回 suggested：那是老 apply 的语义，
    对「本来就归在 A 项目、这次被改成 B」的会话会错退回「未归入」。
    """
    op_id = str(op_id or "").strip()
    if not op_id:
        return base._fail("invalid_op_id", "需要 op_id")
    try:
        core, conn = _store(home)
    except Exception as exc:  # noqa: BLE001
        return base._fail("store_unavailable", "打不开归属库：" + base._describe(exc), status_code=500)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM console_ops WHERE op_id = ? ORDER BY seq", (op_id,))]
        if not rows:
            return base._fail("unknown_op", "台账里没有这个面板操作：" + op_id, status_code=404,
                              extra={"op_id": op_id})
        header = next((r for r in rows if r["kind"] == "header"), None)
        records = [r for r in rows if r["kind"] == "session"]
        if header is not None and header.get("undone"):
            return base._fail("already_undone", "这个操作已经撤销过：" + op_id, status_code=409,
                              extra={"op_id": op_id, "undone_at": header.get("undone_at")})
        now, restored, expectations = time.time(), [], []
        with conn:
            for rec in records:
                row_before = json.loads(rec["row_before"]) if rec.get("row_before") else None
                expectations.append((rec["session_id"], row_before))
                assign_action = _restore_assign_row(conn, rec["session_id"], row_before)
                cwd_action = "untouched"
                if rec.get("cwd_written") and rec.get("cwd_before_known"):
                    cwd_action = "restored" if _write_cwd(home, rec["session_id"], rec.get("cwd_before")) \
                        else "missing_session"
                restored.append({"session_id": rec["session_id"], "assign": assign_action, "cwd": cwd_action,
                                 "slug_before": rec.get("slug_before"), "slug_after": rec.get("slug_after"),
                                 "cwd_before": rec.get("cwd_before") if rec.get("cwd_before_known") else None})
            header_action = _undo_header_op(home, header) if (header is not None and not records) else None
            conn.execute("UPDATE console_ops SET undone = 1, undone_at = ? WHERE op_id = ?", (now, op_id))
    finally:
        conn.close()
    try:
        readback = _verify_rows(home, expectations)
    except Exception as exc:  # noqa: BLE001
        return base._fail("readback_failed", "撤销后回读失败：" + base._describe(exc), status_code=500,
                          extra={"op_id": op_id})
    cwd_bad = []
    for item in restored:
        if item["cwd"] != "restored":
            continue
        exists, cwd_now = _read_cwd(home, item["session_id"])
        if not exists or cwd_now != item["cwd_before"]:
            cwd_bad.append({"session_id": item["session_id"], "expected": item["cwd_before"], "now": cwd_now})
    readback["cwd_mismatched"] = cwd_bad
    if readback["mismatched"] or cwd_bad:
        return base._fail("readback_failed", "撤销返回成功，但回读与写入前不一致", status_code=500,
                          extra={"op_id": op_id, "readback": readback})
    notes = []
    if header_action and header_action.get("applied") is None:
        notes.append(header_action.get("reason") or "")
    notes.append("已逐字段还原 %d 条会话；cwd 只在这次真写过时才回滚" % len(restored))
    return {"ok": True, "data": {"op_id": op_id, "op": (header or {}).get("op"), "undone_at": now,
                                 "sessions": restored, "header_action": header_action,
                                 "readback": readback}, "notes": [n for n in notes if n]}


@router.get("/autogroup/ops")
def get_ops(limit: int = Query(default=50, ge=1, le=OPS_MAX_LIMIT), op_id: str = "",
            undone: Optional[int] = Query(default=None),
            agent: str = Query(default="", description=(
                "按哪个 agent 的归属库取（空/default=根库；<p>=profiles/<p>；__all__ 需先选定）"))) -> Any:
    """面板写操作台账（倒序）。每条 op 带逐会话 from→to 与「写入前有没有这一行」。"""
    home, err = _read_home(_agent_of(agent))
    if err is not None:
        return err
    try:
        core = base._load_core()
        path = core.store_path(home)
        empty = {"ok": True, "data": {"ops": [], "total": 0, "store": str(path)}, "notes": []}
        if not path.exists():
            empty["notes"] = ["归属库还没建（auto-group/assignments.db）：先扫描或归入一次"]
            return empty
        conn = core.connect_ro(path)
        try:
            if "console_ops" not in {r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}:
                empty["notes"] = ["console_ops 台账还没建：本模块首次写入时才建表"]
                return empty
            sql = ("SELECT op_id, op, batch_id, created_at, undone, undone_at, note FROM console_ops"
                   " WHERE kind = 'header'")
            params: List[Any] = []
            if op_id:
                sql += " AND op_id = ?"
                params.append(op_id)
            if undone is not None:
                sql += " AND undone = ?"
                params.append(int(undone))
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(int(limit))
            headers = [dict(r) for r in conn.execute(sql, params)]
            ops: List[Dict[str, Any]] = []
            for head in headers:
                recs = [dict(r) for r in conn.execute(
                    "SELECT session_id, slug_before, slug_after, cwd_before, cwd_before_known, cwd_written,"
                    " row_before FROM console_ops WHERE kind = 'session' AND op_id = ? ORDER BY seq",
                    (head["op_id"],))]
                meta, meta_note = base._session_meta(home, [r["session_id"] for r in recs])
                ops.append({
                    "op_id": head["op_id"], "op": head["op"], "batch_id": head["batch_id"],
                    "created_at": head["created_at"], "undone": bool(head["undone"]),
                    "undone_at": head["undone_at"], "note": head["note"],
                    "session_count": len(recs), "meta_error": meta_note,
                    "sessions": [{
                        "session_id": r["session_id"], "title": (meta.get(r["session_id"]) or {}).get("title", ""),
                        "from": r["slug_before"], "to": r["slug_after"],
                        "row_existed_before": bool(r["row_before"]),
                        "cwd_before": r["cwd_before"] if r["cwd_before_known"] else None,
                        "cwd_written": bool(r["cwd_written"])} for r in recs],
                })
            total = conn.execute("SELECT COUNT(*) FROM console_ops WHERE kind = 'header'").fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return base._fail("ops_failed", "读台账失败：" + base._describe(exc), status_code=500)
    notes = []
    if not ops:
        notes.append("台账是空的：还没有任何面板写操作")
    return {"ok": True, "data": {"ops": ops, "total": int(total), "returned": len(ops),
                                 "store": str(path)}, "notes": notes}


def _assign_counts(home: Path) -> Tuple[Dict[str, Dict[str, int]], Optional[str]]:
    """slug → {total, applied, suggested}（只读投影，用来判断项目能不能删）。"""
    try:
        core = base._load_core()
        path = core.store_path(home)
        if not path.exists():
            return {}, None
        conn = core.connect_ro(path)
        try:
            rows = conn.execute("SELECT project_slug, status, COUNT(*) AS n FROM assignments"
                                " GROUP BY project_slug, status").fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return {}, "读归属表失败：" + base._describe(exc)
    out: Dict[str, Dict[str, int]] = {}
    for row in rows:
        bucket = out.setdefault(str(row["project_slug"]), {"total": 0, "applied": 0, "suggested": 0})
        bucket["total"] += int(row["n"])
        if row["status"] in ("applied", "suggested"):
            bucket[str(row["status"])] += int(row["n"])
    return out, None


def _dir_state(path: Optional[str]) -> Dict[str, Any]:
    """目录在不在、空不空。**绝不递归删**，所以这两个值就是删除前的判据。"""
    if not path:
        return {"exists": False, "empty": None, "path": None}
    folder = Path(path)
    try:
        if not folder.is_dir():
            return {"exists": False, "empty": None, "path": str(folder)}
        return {"exists": True, "empty": not any(folder.iterdir()), "path": str(folder)}
    except OSError as exc:
        return {"exists": None, "empty": None, "path": str(folder), "error": base._describe(exc)}


@router.get("/autogroup/projects")
def get_projects(agent: str = Query(default="", description=(
        "按哪个 agent 的项目/归属库取（空/default=根库；<p>=profiles/<p>）"))) -> Any:
    """项目清单（含目录状态与归属计数），以及哪些是面板/插件建的。只读。"""
    home, err = _read_home(_agent_of(agent))
    if err is not None:
        return err
    try:
        core = base._load_core()
        projects = list(core.list_projects(home))
        ai_slugs = {str(d.get("slug")) for d in core.list_ai_projects(home)}
    except Exception as exc:  # noqa: BLE001
        return base._fail("projects_failed", "读项目表失败：" + base._describe(exc), status_code=500)
    counts, counts_error = _assign_counts(home)
    items = []
    for project in projects:
        slug = str(project.get("slug") or "")
        stat = counts.get(slug, {"total": 0, "applied": 0, "suggested": 0})
        state = _dir_state(project.get("primary_path"))
        items.append({
            "slug": slug, "name": project.get("name"), "path": project.get("primary_path"),
            "folders": [f.get("path") for f in (project.get("folders") or [])],
            "created_by_panel": slug in ai_slugs, "dir": state,
            "assignments": stat,
            "can_delete": stat["total"] == 0 and bool(state.get("empty")),
        })
    notes = []
    if counts_error:
        notes.append(counts_error)
    if not projects:
        notes.append("projects.db 里没有项目（或文件不存在）")
    return {"ok": True, "data": {"projects": items, "total": len(items),
                                 "ai_projects": sorted(ai_slugs)}, "notes": notes}


@router.post("/autogroup/projects")
def post_project(payload: Dict[str, Any] = Body(...),
                 agent: str = Query(default="", description="给哪个 agent 建项目（空/default=根库）")) -> Any:
    """建项目（含绑目录 + 可选关键词/会话）→ 回读项目表。

    目录只建不删；``slug`` 缺省由 ``core.slugify`` 从名字推。建完立刻回读 ``list_projects``，
    写入返回值不当成功证据。
    """
    scope = _write_home(_agent_of(agent, payload))
    if isinstance(scope, JSONResponse):
        return scope
    agent_name, home = scope
    if agent_name != base._load_core().DEFAULT_AGENT:
        pdb = home / "projects.db"
        if not pdb.is_file():
            # 不给「还没有项目库」的 agent 造库、也不回落根库：create_project 会先 mkdir 目录
            # 再报 projects.db 不存在，那会在别人的 home 里留下空目录。
            return base._fail(
                "no_project_db",
                "该 agent（%s）还没有项目库：%s 不存在。本接口不替你造库，也不回落到根库"
                "（别的 agent 的项目不归它）" % (agent_name, pdb),
                status_code=409, extra={"agent": agent_name, "projects_db": str(pdb), "needs_db": True})
    name = str(payload.get("name") or "").strip()
    if not name:
        return base._fail("invalid_name", "需要 name")
    if len(name) > 60:
        return base._fail("invalid_name", "name 最多 60 个字符")
    raw_slug = str(payload.get("slug") or "").strip()
    keywords = [str(k).strip()[:base.KEYWORD_MAX] for k in (payload.get("keywords") or []) if str(k).strip()]
    session_ids = [str(s).strip() for s in (payload.get("session_ids") or []) if str(s).strip()]
    if len(session_ids) > ASSIGN_MAX:
        return base._fail("too_many_sessions", "一次最多 %d 条会话" % ASSIGN_MAX)
    try:
        core = base._load_core()
        slug = raw_slug or core.slugify(name)
        if not base.SLUG_RE.match(slug):
            return base._fail("invalid_slug", "slug 只能是 2-41 位小写字母/数字/连字符：" + str(slug),
                              extra={"suggestion": core.slugify(name)})
        existing = {str(p.get("slug")) for p in core.list_projects(home)}
        if slug in existing:
            return base._fail("slug_exists", "项目已存在：" + slug, status_code=409,
                              extra={"slug": slug, "projects": sorted(existing)})
        result = core.create_project(home, name=name, slug=slug, session_ids=session_ids or None,
                                     keywords=keywords or None,
                                     root=_project_root(home))
        if not result.get("ok"):
            return base._fail("create_failed", str(result.get("error") or "建项目失败"), status_code=409,
                              extra={"slug": slug})
        after = {str(p.get("slug")): p for p in core.list_projects(home)}
        project = after.get(slug)
    except Exception as exc:  # noqa: BLE001
        return base._fail("create_failed", "建项目失败：" + base._describe(exc), status_code=500)
    if project is None:
        return base._fail("readback_failed", "建项目返回成功，但回读项目表里没有它：" + slug,
                          status_code=500, extra={"slug": slug})
    state = _dir_state(project.get("primary_path"))
    if not state.get("exists"):
        return base._fail("readback_failed", "项目已登记但目录不存在：" + str(project.get("primary_path")),
                          status_code=500, extra={"slug": slug, "dir": state})
    op_id = _new_id("op-")
    try:
        _core, conn = _store(home)
        try:
            with conn:
                _record_header(conn, op_id=op_id, op="projects.create",
                               payload={"slug": slug, "name": name, "path": str(project.get("primary_path")),
                                        "session_ids": session_ids, "keywords": keywords})
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - 台账失败不回滚已建好的项目，但要如实报
        return {"ok": True, "data": {"project": project, "op_id": None, "dir": state},
                "notes": ["项目已建，但写台账失败（撤销将无记录）：" + base._describe(exc)]}
    notes = ["项目已登记并绑定目录：" + str(project.get("primary_path"))]
    if session_ids:
        notes.append("已把 %d 条会话排进待确认建议（status=suggested），还需 POST /autogroup/assign 才真归入"
                     % len(session_ids))
    return {"ok": True, "data": {"project": project, "op_id": op_id, "dir": state,
                                 "queued_sessions": len(session_ids)}, "notes": notes}


def _rename_project(home: Path, slug: str, *, name: Optional[str] = None,
                    new_slug: Optional[str] = None) -> Dict[str, Any]:
    """改名/改 slug。改 slug 时**连带**迁移归属表、规则表键、插件建组台账（否则历史归属指向空）。

    目录**不跟着改名**（那会动到磁盘上的真实路径），只在 notes 里说清 path 保持原样。
    """
    core = base._load_core()
    slug = str(slug or "").strip()
    new_slug = str(new_slug or "").strip()
    name = str(name).strip() if name is not None else None
    if not slug:
        return {"ok": False, "code": "invalid_slug", "error": "需要 slug"}
    projects = {str(p.get("slug")): p for p in core.list_projects(home)}
    if slug not in projects:
        return {"ok": False, "code": "unknown_project", "error": "没有这个项目：" + slug}
    if not name and (not new_slug or new_slug == slug):
        return {"ok": False, "code": "nothing_to_do", "error": "name 和 new_slug 至少给一个有变化的"}
    if new_slug and new_slug != slug:
        if not base.SLUG_RE.match(new_slug):
            return {"ok": False, "code": "invalid_slug", "error": "非法 slug：" + new_slug}
        if new_slug in projects:
            return {"ok": False, "code": "slug_exists", "error": "slug 已存在：" + new_slug}
    pdb = Path(home) / "projects.db"
    if not pdb.is_file():
        return {"ok": False, "code": "no_projects_db", "error": "projects.db 不存在"}
    migrated = {"assignments": 0, "rules": False, "ledger": False}
    conn = core.connect_rw(pdb)
    try:
        with conn:
            if name:
                conn.execute("UPDATE projects SET name = ? WHERE slug = ?", (name, slug))
            if new_slug and new_slug != slug:
                conn.execute("UPDATE projects SET slug = ? WHERE slug = ?", (new_slug, slug))
    finally:
        conn.close()
    final_slug = new_slug or slug
    if new_slug and new_slug != slug:
        store = core.ensure_store(home)
        try:
            with store:
                cur = store.execute("UPDATE assignments SET project_slug = ?, updated_at = ?"
                                    " WHERE project_slug = ?", (new_slug, time.time(), slug))
                migrated["assignments"] = int(cur.rowcount or 0)
        finally:
            store.close()
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
        ledger = Path(home) / "auto-group" / "ai_projects.json"
        if ledger.exists():
            try:
                entries = json.loads(ledger.read_text(encoding="utf-8")) or []
            except (OSError, ValueError):
                entries = []
            touched = False
            for entry in entries:
                if isinstance(entry, dict) and entry.get("slug") == slug:
                    entry["slug"] = new_slug
                    if name:
                        entry["name"] = name
                    touched = True
            if touched:
                base._atomic_write_json(ledger, entries)
                migrated["ledger"] = True
    after = {str(p.get("slug")): p for p in core.list_projects(home)}
    project_after = after.get(final_slug)
    if project_after is None:
        return {"ok": False, "code": "readback_failed",
                "error": "改名返回成功，但回读项目表里没有 " + final_slug}
    return {"ok": True, "slug": final_slug, "slug_before": slug, "name": project_after.get("name"),
            "path": project_after.get("primary_path"), "migrated": migrated}


@router.patch("/autogroup/projects")
def patch_project(payload: Dict[str, Any] = Body(...),
                  agent: str = Query(default="", description="改哪个 agent 的项目（空/default=根库）")) -> Any:
    """改名（``name``）/改 slug（``new_slug``）→ 回读。改 slug 会连带迁移归属与规则。"""
    scope = _write_home(_agent_of(agent, payload))
    if isinstance(scope, JSONResponse):
        return scope
    _agent_name, home = scope
    slug = str(payload.get("slug") or "").strip()
    if not slug:
        return base._fail("invalid_slug", "需要 slug")
    try:
        core = base._load_core()
        before = {str(p.get("slug")): p for p in core.list_projects(home)}.get(slug)
    except Exception as exc:  # noqa: BLE001
        return base._fail("projects_failed", "读项目表失败：" + base._describe(exc), status_code=500)
    if before is None:
        return base._fail("unknown_project", "没有这个项目：" + slug, status_code=404)
    try:
        result = _rename_project(home, slug, name=payload.get("name"),
                                 new_slug=payload.get("new_slug"))
    except Exception as exc:  # noqa: BLE001
        return base._fail("rename_failed", "改名失败：" + base._describe(exc), status_code=500)
    if not result.get("ok"):
        code = str(result.get("code") or "rename_failed")
        status = 404 if code == "unknown_project" else (409 if code in ("slug_exists",) else 400)
        return base._fail(code, str(result.get("error") or "改名失败"), status_code=status, extra={"slug": slug})
    op_id = _new_id("op-")
    notes = ["项目已改名：%s → %s" % (slug, result["slug"])]
    if result["slug"] != slug:
        notes.append("目录没跟着改名（path 仍是 %s）；归属迁移 %d 条、规则表 %s、建组台账 %s"
                     % (result.get("path"), result["migrated"]["assignments"],
                        "已迁移" if result["migrated"]["rules"] else "无改动",
                        "已迁移" if result["migrated"]["ledger"] else "无改动"))
    try:
        _core, conn = _store(home)
        try:
            with conn:
                _record_header(conn, op_id=op_id, op="projects.rename",
                               payload={"slug_before": slug, "slug_after": result["slug"],
                                        "name_before": before.get("name"), "name_after": result.get("name")})
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        notes.append("改名已完成，但写台账失败（撤销将无记录）：" + base._describe(exc))
        op_id = None
    return {"ok": True, "data": {"project": result, "op_id": op_id}, "notes": notes}


@router.delete("/autogroup/projects")
def delete_project(slug: str = "", payload: Optional[Dict[str, Any]] = Body(None),
                   agent: str = Query(default="", description="删哪个 agent 的项目（空/default=根库）")) -> Any:
    """删项目 = **只摘注册 + 只删空目录**。绝不递归删：目录非空直接 409 让人自己看一眼。"""
    scope = _write_home(_agent_of(agent, payload))
    if isinstance(scope, JSONResponse):
        return scope
    _agent_name, home = scope
    slug = str(slug or (payload or {}).get("slug") or "").strip()
    if not slug:
        return base._fail("invalid_slug", "需要 slug（query 或 body）")
    try:
        core = base._load_core()
        projects = {str(p.get("slug")): p for p in core.list_projects(home)}
    except Exception as exc:  # noqa: BLE001
        return base._fail("projects_failed", "读项目表失败：" + base._describe(exc), status_code=500)
    project = projects.get(slug)
    if project is None:
        return base._fail("unknown_project", "没有这个项目：" + slug, status_code=404,
                          extra={"projects": sorted(projects)})
    counts, counts_error = _assign_counts(home)
    stat = counts.get(slug, {"total": 0})
    if stat["total"] > 0:
        return base._fail("project_not_empty",
                          "项目下还有 %d 条会话归属，拒绝删除（先把它们归到别的项目，或先撤销）" % stat["total"],
                          status_code=409, extra={"slug": slug, "assignments": stat,
                                                  "counts_error": counts_error})
    folder = project.get("primary_path")
    state = _dir_state(folder)
    entries: List[str] = []
    if state.get("exists") and not state.get("empty"):
        try:
            entries = sorted(p.name for p in Path(str(folder)).iterdir())[:10]
        except OSError:
            entries = []
        return base._fail("dir_not_empty",
                          "目录非空，拒绝删除（本接口只删空目录，绝不递归删）：" + str(folder),
                          status_code=409, extra={"slug": slug, "dir": state, "entries": entries})
    removed_dir, removed_error = False, None
    if state.get("exists") and state.get("empty"):
        try:
            Path(str(folder)).rmdir()      # rmdir 只能删空目录，非空会抛 OSError
            removed_dir = True
        except OSError as exc:
            removed_error = base._describe(exc)
    try:
        pdb = Path(home) / "projects.db"
        conn = core.connect_rw(pdb)
        try:
            with conn:
                conn.execute("DELETE FROM project_folders WHERE project_id = ?", (project.get("id"),))
                conn.execute("DELETE FROM projects WHERE id = ?", (project.get("id"),))
        finally:
            conn.close()
        ledger = Path(home) / "auto-group" / "ai_projects.json"
        if ledger.exists():
            try:
                entries_all = json.loads(ledger.read_text(encoding="utf-8")) or []
            except (OSError, ValueError):
                entries_all = []
            rest = [e for e in entries_all if not (isinstance(e, dict) and e.get("slug") == slug)]
            if len(rest) != len(entries_all):
                base._atomic_write_json(ledger, rest)
        after = {str(p.get("slug")) for p in core.list_projects(home)}
    except Exception as exc:  # noqa: BLE001
        return base._fail("delete_failed", "删项目失败：" + base._describe(exc), status_code=500)
    if slug in after:
        return base._fail("readback_failed", "删除返回成功，但回读项目表里还有它：" + slug,
                          status_code=500, extra={"slug": slug})
    op_id = _new_id("op-")
    notes = ["已摘除注册；目录 %s" % ("已删（原来是空目录）" if removed_dir else "保留（不存在或删不掉）")]
    if removed_error:
        notes.append("删空目录失败（已保留）：" + removed_error)
    try:
        _core, conn = _store(home)
        try:
            with conn:
                _record_header(conn, op_id=op_id, op="projects.delete",
                               payload={"slug": slug, "path": folder, "removed_dir": removed_dir})
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        notes.append("删除已完成，但写台账失败：" + base._describe(exc))
        op_id = None
    return {"ok": True, "data": {"slug": slug, "removed_dir": removed_dir, "path": folder,
                                 "op_id": op_id, "remaining": sorted(after)}, "notes": notes}


def _normalize_sessions(payload: Dict[str, Any]) -> List[str]:
    """``session_ids`` 数组或单个 ``session_id``；去空、去重、保序。"""
    raw = payload.get("session_ids")
    if raw is None:
        single = str(payload.get("session_id") or "").strip()
        raw = [single] if single else []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise ValueError("session_ids 必须是数组（或单个 session_id）")
    return list(dict.fromkeys(str(s).strip() for s in raw if str(s).strip()))


def _assign_preview(home: Path, core: Any, session_ids: List[str], slug: str,
                    project: Dict[str, Any], projects: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """逐条 from→to。**不重新判定**：from 读的是归属表现状，to 是这次要写的目标。"""
    current: Dict[str, Dict[str, Any]] = {}
    path = core.store_path(home)
    if path.exists():
        conn = core.connect_ro(path)
        try:
            for start in range(0, len(session_ids), 400):
                chunk = session_ids[start:start + 400]
                sql = ("SELECT " + ", ".join(ASSIGN_COLUMNS) + " FROM assignments WHERE session_id IN ("
                       + ",".join("?" * len(chunk)) + ")")
                for row in conn.execute(sql, chunk):
                    current[str(row["session_id"])] = dict(row)
        finally:
            conn.close()
    meta, meta_error = base._session_meta(home, session_ids)
    items = []
    for session_id in session_ids:
        row = current.get(session_id)
        info = meta.get(session_id) or {}
        from_slug = row.get("project_slug") if row else None
        from_project = projects.get(from_slug) or {}
        if row is None:
            action = "insert"
        elif from_slug == slug and row.get("status") == "applied":
            action = "noop"
        else:
            action = "update"
        items.append({
            "session_id": session_id, "title": info.get("title") or "",
            "from": {"slug": from_slug, "name": from_project.get("name") or from_slug,
                     "status": row.get("status") if row else None,
                     "batch_id": row.get("batch_id") if row else None,
                     "row_existed": row is not None},
            "to": {"slug": slug, "name": project.get("name") or slug},
            "action": action, "cwd_before": info.get("cwd"), "session_exists": bool(info),
            # 硬约束：看板任务会话不得进入普通项目（预览就标出来，执行前再拒一次）
            "session_source": info.get("source"), "kanban": info.get("source") == KANBAN_SOURCE,
        })
    return {"target": {"slug": slug, "name": project.get("name"),
                       "path": project.get("primary_path")},
            "items": items, "meta_error": meta_error,
            "inserts": sum(1 for i in items if i["action"] == "insert"),
            "updates": sum(1 for i in items if i["action"] == "update"),
            "noops": sum(1 for i in items if i["action"] == "noop"),
            "kanban": [i["session_id"] for i in items if i["kanban"]]}


@router.post("/autogroup/assign")
def post_assign(payload: Dict[str, Any] = Body(...),
                agent: str = Query(default="", description="写哪个 agent 的归属库（空/default=根库）")) -> Any:
    """批量归入某项目。**缺 ``confirm=true`` 一律 400** + 逐条 from→to 预览。

    写入前逐会话快照整行 + cwd 原值进 ``console_ops``，所以 ``POST /autogroup/undo`` 带
    ``op_id``/``batch_id`` 都能逐字段还原。
    """
    scope = _write_home(_agent_of(agent, payload))
    if isinstance(scope, JSONResponse):
        return scope
    _agent_name, home = scope
    slug = str(payload.get("slug") or payload.get("project_slug") or "").strip()
    if not slug:
        return base._fail("invalid_slug", "需要 slug（目标项目）")
    try:
        session_ids = _normalize_sessions(payload)
    except ValueError as exc:
        return base._fail("invalid_session_ids", str(exc))
    if not session_ids:
        return base._fail("invalid_session_ids", "需要 session_ids（或单个 session_id）")
    if len(session_ids) > ASSIGN_MAX:
        return base._fail("too_many_sessions", "一次最多归入 %d 条" % ASSIGN_MAX)
    try:
        core = base._load_core()
        projects = {str(p.get("slug")): p for p in core.list_projects(home)}
    except Exception as exc:  # noqa: BLE001
        return base._fail("projects_failed", "读项目表失败：" + base._describe(exc), status_code=500)
    project = projects.get(slug)
    if project is None:
        return base._fail("unknown_project", "没有这个项目：" + slug, extra={"projects": sorted(projects)})
    try:
        preview = _assign_preview(home, core, session_ids, slug, project, projects)
    except Exception as exc:  # noqa: BLE001
        return base._fail("preview_failed", "生成预览失败：" + base._describe(exc), status_code=500)
    blocked = _reject_kanban(preview, home)
    if blocked is not None:
        return blocked
    if payload.get("confirm") is not True:
        return base._fail("confirmation_required",
                          "批量归入需要 confirm=true。下面是逐条 from→to 预览（未写任何东西）",
                          extra={"preview": preview})
    return _execute_assign(home, core, op="assign", slug=slug, project=project, projects=projects,
                           session_ids=session_ids, sync_cwd=_want_sync_cwd(payload, project),
                           reason=str(payload.get("reason") or "面板手动归入").strip()[:200],
                           preview=preview)


def _want_sync_cwd(payload: Dict[str, Any], project: Dict[str, Any]) -> bool:
    """写归属是否同步 cwd。**默认同步**（t_53b48743：归属与 cwd 必须一致）。

    只有显式 ``sync_cwd=false`` 才跳过；项目没有目录时也跳过（并在 notes 里如实说）。
    """
    explicit = payload.get("sync_cwd")
    if explicit is False:
        return False
    return bool(project.get("primary_path"))


def _reject_kanban(preview: Dict[str, Any], home: Path) -> Optional[Any]:
    """看板任务硬约束：命中了就 400 拒绝整批，不做「部分写入」。

    预览里已经标了 ``kanban``（按 ``sessions.source``）；这里再直接查一次 state.db，
    不依赖调用方是否传了正确的 session_ids。
    """
    hit = list(preview.get("kanban") or [])
    if not hit:
        return None
    meta, _err = base._session_meta(home, hit)
    detail = [{"session_id": sid, "title": (meta.get(sid) or {}).get("title", "")} for sid in hit]
    return base._fail("kanban_excluded",
                      "看板任务会话不得归入普通项目（source=kanban）：%d 条" % len(hit),
                      extra={"blocked_session_ids": hit, "blocked": detail,
                             "preview": preview})


def _execute_assign(home: Path, core: Any, *, op: str, slug: str, project: Dict[str, Any],
                    projects: Dict[str, Dict[str, Any]], session_ids: List[str], sync_cwd: bool,
                    reason: str, preview: Dict[str, Any]) -> Any:
    """真正写归属表：先落台账（写前整行 + cwd 原值）→ 写 → 回读核对。"""
    target_path = project.get("primary_path") or (
        project["folders"][0]["path"] if project.get("folders") else None)
    if sync_cwd and not target_path:
        return base._fail("missing_project_path", "项目没有目录，无法同步 cwd：" + slug)
    if sync_cwd and not Path(str(target_path)).is_dir():
        # **停手不建空目录**：目标目录不存在时报错，让用户先处理项目目录
        return base._fail("dir_missing",
                          "项目权威目录不存在，拒绝写入（不静默建空目录）：" + str(target_path),
                          extra={"slug": slug, "path": str(target_path)})
    op_id, batch_id, now = _new_id("op-"), _new_id("pn-"), time.time()
    written: List[str] = []
    skipped: List[Dict[str, Any]] = []
    cwd_undo: List[Dict[str, Any]] = []       # cwd 写前原值（事务回滚时用来补偿 state.db）
    try:
        _core, conn = _store(home)
    except Exception as exc:  # noqa: BLE001
        return base._fail("store_unavailable", "打不开归属库：" + base._describe(exc), status_code=500)
    try:
        with conn:
            _record_header(conn, op_id=op_id, op=op, batch_id=batch_id, created_at=now,
                           payload={"slug": slug, "session_ids": session_ids, "sync_cwd": sync_cwd,
                                    "reason": reason, "project_path": target_path})
            conn.execute("INSERT OR REPLACE INTO batches(batch_id, created_at, note, undone)"
                         " VALUES(?,?,?,0)", (batch_id, now, "面板 %s → %s" % (op, slug)))
            for session_id in session_ids:
                row_before = _assign_row(conn, session_id)
                cwd_known, cwd_before = _read_cwd(home, session_id)
                slug_before = row_before.get("project_slug") if row_before else None
                if (row_before is not None and slug_before == slug
                        and row_before.get("status") == "applied" and not sync_cwd):
                    skipped.append({"session_id": session_id,
                                    "why": "已经是该项目的 applied，未改写（也不进台账）"})
                    continue
                if row_before is None:
                    conn.execute(
                        "INSERT INTO assignments(session_id, project_slug, confidence, reason, source,"
                        " status, batch_id, applied_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (session_id, slug, 1.0, reason, "panel", "applied", batch_id, now, now))
                else:
                    conn.execute(
                        "UPDATE assignments SET project_slug = ?, confidence = ?, reason = ?, source = ?,"
                        " status = ?, batch_id = ?, applied_at = ?, updated_at = ? WHERE session_id = ?",
                        (slug, 1.0, reason, "panel", "applied", batch_id, now, now, session_id))
                if sync_cwd and target_path:
                    # 写归属 = 写 cwd（t_53b48743）：cwd 没写成就把这一行归属也退掉，不让它们分叉
                    if not _write_cwd(home, session_id, target_path):
                        raise RuntimeError("写 cwd 失败，归属不落：" + session_id)
                    cwd_undo.append({"session_id": session_id, "cwd_before": cwd_before})
                    cwd_written = True
                else:
                    cwd_written = False
                _record_session(conn, op_id=op_id, op=op, session_id=session_id, row_before=row_before,
                                cwd_known=cwd_known, cwd_before=cwd_before, cwd_written=cwd_written,
                                slug_before=slug_before, slug_after=slug, batch_id=batch_id, created_at=now)
                written.append(session_id)
    except Exception as exc:  # noqa: BLE001 - 事务已回滚，如实报
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        # 归属事务已回滚，但 cwd 在另一个库、已经写过了：逐条补偿回原值
        cwd_restored, cwd_restore_failed = [], []
        for rec in cwd_undo:
            ok = _write_cwd(home, rec["session_id"], rec["cwd_before"])
            (cwd_restored if ok else cwd_restore_failed).append(rec["session_id"])
        return base._fail("assign_failed",
                          "写归属表失败（归属已回滚；cwd 已补偿 %d 条，失败 %d 条）：%s"
                          % (len(cwd_restored), len(cwd_restore_failed), base._describe(exc)),
                          status_code=500,
                          extra={"op_id": op_id, "batch_id": batch_id,
                                 "cwd_restored": cwd_restored,
                                 "cwd_restore_failed": cwd_restore_failed})
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    # 回读核对：逐条查回写后的行，任何一项不符就 500，不谎报成功
    try:
        rconn = core.connect_ro(core.store_path(home))
        try:
            mismatched = []
            for session_id in written:
                got = _assign_row(rconn, session_id)
                if got is None:
                    mismatched.append({"session_id": session_id, "why": "写完回读没有这一行"})
                    continue
                diff = {}
                if got.get("project_slug") != slug:
                    diff["project_slug"] = {"want": slug, "got": got.get("project_slug")}
                if got.get("status") != "applied":
                    diff["status"] = {"want": "applied", "got": got.get("status")}
                if got.get("batch_id") != batch_id:
                    diff["batch_id"] = {"want": batch_id, "got": got.get("batch_id")}
                if got.get("source") != "panel":
                    diff["source"] = {"want": "panel", "got": got.get("source")}
                if diff:
                    mismatched.append({"session_id": session_id, "diff": diff})
        finally:
            rconn.close()
    except Exception as exc:  # noqa: BLE001
        return base._fail("readback_failed", "写完回读失败：" + base._describe(exc), status_code=500,
                          extra={"op_id": op_id, "batch_id": batch_id})
    cwd_mismatched = []
    if sync_cwd and target_path:
        for session_id in written:
            exists, cwd_now = _read_cwd(home, session_id)
            if exists and cwd_now != target_path:
                cwd_mismatched.append({"session_id": session_id, "want": target_path, "got": cwd_now})
    if mismatched or cwd_mismatched:
        return base._fail("readback_failed", "写入返回成功，但回读与预期不一致", status_code=500,
                          extra={"op_id": op_id, "batch_id": batch_id, "mismatched": mismatched,
                                 "cwd_mismatched": cwd_mismatched})
    notes = []
    if skipped:
        notes.append("%d 条原本就在该项目且已 applied，未改写" % len(skipped))
    notes.append("cwd 已同步到 %s（撤销会还原原值）" % target_path if sync_cwd
                 else "未同步 cwd（sync_cwd=false）：只改归属表，桌面端项目树不动")
    notes.append("撤销：POST /autogroup/undo {op_id} 或 {batch_id}")
    return {"ok": True, "data": {
        "op_id": op_id, "batch_id": batch_id, "op": op,
        "project": {"slug": slug, "name": (projects.get(slug) or project).get("name"),
                    "path": target_path},
        "written": len(written), "sessions": written, "skipped": skipped,
        "preview": preview,
        "readback": {"checked": len(written), "mismatched": [], "cwd_mismatched": [],
                     "cwd_synced": bool(sync_cwd)}}, "notes": notes}


@router.post("/autogroup/switch")
def post_switch(payload: Dict[str, Any] = Body(...),
                agent: str = Query(default="", description="写哪个 agent 的归属库（空/default=根库）")) -> Any:
    """单条会话快速切换项目（走同一条台账路径，同样可撤销）。"""
    scope = _write_home(_agent_of(agent, payload))
    if isinstance(scope, JSONResponse):
        return scope
    _agent_name, home = scope
    session_id = str(payload.get("session_id") or "").strip()
    slug = str(payload.get("slug") or payload.get("project_slug") or "").strip()
    if not session_id:
        return base._fail("invalid_session_id", "需要 session_id")
    if not slug:
        return base._fail("invalid_slug", "需要 slug（目标项目）")
    try:
        core = base._load_core()
        projects = {str(p.get("slug")): p for p in core.list_projects(home)}
    except Exception as exc:  # noqa: BLE001
        return base._fail("projects_failed", "读项目表失败：" + base._describe(exc), status_code=500)
    project = projects.get(slug)
    if project is None:
        return base._fail("unknown_project", "没有这个项目：" + slug, extra={"projects": sorted(projects)})
    try:
        preview = _assign_preview(home, core, [session_id], slug, project, projects)
    except Exception as exc:  # noqa: BLE001
        return base._fail("preview_failed", "生成预览失败：" + base._describe(exc), status_code=500)
    blocked = _reject_kanban(preview, home)
    if blocked is not None:
        return blocked
    result = _execute_assign(home, core, op="switch", slug=slug, project=project, projects=projects,
                             session_ids=[session_id], sync_cwd=_want_sync_cwd(payload, project),
                             reason=str(payload.get("reason") or "面板快速切换").strip()[:200],
                             preview=preview)
    return result


def _auto_state_path(home: Path) -> Path:
    return Path(home) / "auto-group" / AUTO_STATE_NAME


def _read_auto_state(home: Path) -> Dict[str, Any]:
    """自动模式状态。**默认关闭**；读坏了一律当关闭，绝不猜成开启。"""
    default: Dict[str, Any] = {"enabled": False, "interval_minutes": AUTO_DEFAULT_INTERVAL,
                               "updated_at": None, "updated_by": None, "last_tick": None,
                               "source": "default"}
    path = _auto_state_path(home)
    if not path.is_file():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as exc:
        out = dict(default)
        out.update({"source": "unreadable", "error": base._describe(exc)})
        return out
    if not isinstance(data, dict):
        out = dict(default)
        out["source"] = "malformed"
        return out
    merged = dict(default)
    for key in ("interval_minutes", "updated_at", "updated_by", "last_tick"):
        if key in data:
            merged[key] = data[key]
    merged["enabled"] = data.get("enabled") is True    # 只有明确的 true 才算开
    merged["source"] = "file"
    return merged


def _write_auto_state(home: Path, data: Dict[str, Any]) -> Dict[str, Any]:
    path = _auto_state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        interval = int(data.get("interval_minutes") or AUTO_DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        interval = AUTO_DEFAULT_INTERVAL
    payload = {"enabled": data.get("enabled") is True,
               "interval_minutes": max(1, min(1440, interval)),
               "updated_at": data.get("updated_at") or time.time(),
               "updated_by": data.get("updated_by") or "panel",
               "last_tick": data.get("last_tick")}
    base._atomic_write_json(path, payload)
    return payload


@router.get("/autogroup/auto")
def get_auto(agent: str = Query(default="", description=(
        "读哪个 agent 的自动模式（空/default=根库；<p>=profiles/<p>）"))) -> Any:
    """自动模式开关状态。**只读**：关着的时候连归属库都不打开。"""
    home, err = _read_home(_agent_of(agent))
    if err is not None:
        return err
    state = _read_auto_state(home)
    enabled = bool(state.get("enabled"))
    notes = []
    if state.get("source") in ("unreadable", "malformed"):
        notes.append("状态文件读不了，按「关闭」处理（不猜成开启）：" + str(state.get("error") or ""))
    notes.append("关闭时 POST /autogroup/auto/tick 直接跳过：零副作用" if not enabled
                 else "开启后由外部调度（cron / 手动「立即跑一轮」）调 tick："
                      "达阈值直接写 applied，不确定的留建议（建议间隔 %s 分钟）"
                      % state.get("interval_minutes"))
    return {"ok": True, "data": {"enabled": enabled,
                                 "interval_minutes": state.get("interval_minutes"),
                                 "updated_at": state.get("updated_at"),
                                 "updated_by": state.get("updated_by"),
                                 "last_tick": state.get("last_tick"),
                                 "state_file": str(_auto_state_path(home)),
                                 "source": state.get("source")}, "notes": notes}


def _daemon_view(home: Path) -> Dict[str, Any]:
    """**已下线**（t_ac537221）：守护进程这套第二触发者被删除，见 autogroup_auto.py 末尾注释。"""
    return {"running": False, "removed": True,
            "reason": "独立守护进程已下线：周期触发改为外部调度调 POST /autogroup/auto/tick"}


@router.get("/autogroup/auto/daemon")
def get_auto_daemon() -> Any:
    """**已下线**（t_ac537221）：独立守护进程删除，周期触发改为外部调度调 ``auto/tick``。

    保留这个 410 而不是直接删路由：老书签/老脚本能拿到**可读原因**，不会误以为路由拼错。
    """
    return base._fail("endpoint_removed",
                      "GET /autogroup/auto/daemon 已下线：独立守护进程（autogroup_auto_daemon.py）"
                      "与 POST /autogroup/auto/tick 调的是同一个 run_tick，属重复实现，本卡删除。"
                      "周期触发请用外部调度调 POST /autogroup/auto/tick", status_code=410)



@router.post("/autogroup/auto")
def post_auto(payload: Dict[str, Any] = Body(...),
              agent: str = Query(default="", description="给哪个 agent 开关自动模式（空/default=根库）")) -> Any:
    """打开/关闭自动模式 → 回读状态文件。默认 ``enabled=false``。"""
    scope = _write_home(_agent_of(agent, payload))
    if isinstance(scope, JSONResponse):
        return scope
    _agent_name, home = scope
    if "enabled" not in payload:
        return base._fail("invalid_enabled", "需要 enabled（true / false）")
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        return base._fail("invalid_enabled", "enabled 必须是布尔值（true / false），收到：" + repr(enabled))
    try:
        interval = int(payload.get("interval_minutes") or AUTO_DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        return base._fail("invalid_interval", "interval_minutes 必须是整数（分钟）")
    if not 1 <= interval <= 1440:
        return base._fail("invalid_interval", "interval_minutes 必须在 1-1440 之间")
    before = _read_auto_state(home)
    try:
        _write_auto_state(home, {"enabled": enabled, "interval_minutes": interval,
                                 "updated_at": time.time(), "updated_by": "panel",
                                 "last_tick": before.get("last_tick")})
        after = _read_auto_state(home)
    except Exception as exc:  # noqa: BLE001
        return base._fail("auto_write_failed", "写自动模式状态失败：" + base._describe(exc), status_code=500)
    if bool(after.get("enabled")) != enabled:
        return base._fail("readback_failed", "写入返回成功，但回读 enabled 与预期不一致",
                          status_code=500, extra={"want": enabled, "got": after.get("enabled")})
    # t_ac537221：不再拉起独立守护进程（与 auto/tick 是同一件事的第二套实现，已删）。
    # 开启只写状态文件；周期触发由外部调度调 POST /autogroup/auto/tick。
    daemon = {"running": False, "removed": True,
              "reason": "守护进程已下线：请用外部调度调 POST /autogroup/auto/tick"}

    op_id = _new_id("op-")
    try:
        _core, conn = _store(home)
        try:
            with conn:
                _record_header(conn, op_id=op_id, op="auto.set",
                               payload={"before": {"enabled": bool(before.get("enabled")),
                                                   "interval_minutes": before.get("interval_minutes")},
                                        "after": {"enabled": enabled, "interval_minutes": interval}})
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        op_id = None
        return {"ok": True, "data": {"enabled": enabled, "interval_minutes": interval, "op_id": None,
                                     "daemon": daemon},
                "notes": ["开关已写，但写台账失败：" + base._describe(exc)]}
    notes = ["自动模式已关闭：tick 之后直接跳过，什么也不写" if not enabled
             else "自动模式已开启：外部调度调 POST /autogroup/auto/tick 时达阈值的会话直接写 applied"
                  "（建议间隔 %d 分钟）" % int(after.get("interval_minutes") or AUTO_DEFAULT_INTERVAL)]
    if enabled:
        notes.append("本面板不会自己起后台进程：请把 POST /autogroup/auto/tick 交给 cron/计划任务，"
                     "或直接点「立即跑一轮」")
    return {"ok": True, "data": {"enabled": enabled, "interval_minutes": after.get("interval_minutes"),
                                 "updated_at": after.get("updated_at"), "op_id": op_id,
                                 "daemon": daemon,
                                 "state_file": str(_auto_state_path(home))}, "notes": notes}



@router.post("/autogroup/auto/tick")
def post_auto_tick(payload: Optional[Dict[str, Any]] = Body(None),
                   agent: str = Query(default="", description="跑哪个 agent 的自动归组（空/default=根库）")) -> Any:
    """自动模式一次 tick：**关着就立刻返回、零副作用**；开着按规则归组（达阈值直接 applied）。"""
    scope = _write_home(_agent_of(agent, payload))
    if isinstance(scope, JSONResponse):
        return scope
    _agent_name, home = scope
    try:
        limit = max(1, min(AUTO_TICK_MAX, int((payload or {}).get("limit") or 20)))
    except (TypeError, ValueError):
        return base._fail("invalid_limit", "limit 必须是整数")
    threshold = (payload or {}).get("threshold")
    return auto_engine.run_tick(home, limit=limit, threshold=threshold, trigger="panel-tick",
                                agent=_agent_name)








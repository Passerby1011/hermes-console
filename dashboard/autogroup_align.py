# -*- coding: utf-8 -*-
"""一致性自检 + 对齐修复（t_53b48743 第 2 段）。

判据（两条都要过，一条不过就算不一致）：

* ``path``：``sessions.cwd`` 与 ``projects.primary_path`` **逐字相等**（大小写/分隔符归一后）
* ``slug``：``basename(sessions.cwd) == projects.slug``

对齐方向：

* ``fix_cwd``（默认，用户定的口径）—— **归属为准**：把会话 cwd 改成项目 path。
  影响：桌面端项目树跟着动；会话之后在这个目录开新会话。
* ``fix_slug``（反向，必须显式选）—— **cwd 为准**：把归属的 ``project_slug`` 改成 cwd 目录名
  对应的项目。影响：桌面端项目树/目录完全不动，变的是归属表；cwd 目录名在
  ``projects.db`` 里找不到项目时该条**拒绝**（不静默新建项目）。

两种模式都**排除看板任务**（``source='kanban'``）：那是硬约束，不是可选项。
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import api_autogroup as base
import api_autogroup_panel as panel
import autogroup_projects as ent

#: 判定「一致」用的一致性判据版本（写进台账，撤销时能看出当时的口径）。
CONSISTENCY_VERSION = "path+slug/v1"


def norm_path(value: Optional[str]) -> Optional[str]:
    """路径归一：正反斜杠统一、去掉尾部分隔符、大小写拉平（Windows 语义）。

    只做比较用，**不拿归一后的值回写**：写回去的永远是项目表里的原始 ``primary_path``。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("/", "\\").rstrip("\\")
    return text.lower()


def basename_of(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).replace("/", "\\").rstrip("\\")
    return text.split("\\")[-1] if text else None


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def audit(home: Path, *, include_suggested: bool = False) -> Dict[str, Any]:
    """列出「归属 ≠ cwd 目录」的全部条目。**只读**。

    ``include_suggested=False``（默认）只看 ``status='applied'`` 的归属；这是验收口径
    （67 条 applied / 43 条不一致那个数）。
    """
    core = base._load_core()
    projects = {str(p.get("slug")): p for p in core.list_projects(home)}
    path = ent.store_db(home)
    if not path.is_file():
        return {"ok": True, "items": [], "counts": {"total": 0, "consistent": 0, "inconsistent": 0},
                "notes": ["归属库还没建：auto-group/assignments.db 不存在"], "projects": projects}
    conn = ent._connect_ro(path)
    try:
        sql = ("SELECT session_id, project_slug, status, source, batch_id, applied_at FROM assignments")
        if not include_suggested:
            sql += " WHERE status = 'applied'"
        rows = [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()

    meta = ent.session_meta(home, [r["session_id"] for r in rows])
    items: List[Dict[str, Any]] = []
    counts = {"total": len(rows), "consistent": 0, "inconsistent": 0, "no_session": 0,
              "no_project": 0, "no_path": 0, "kanban_excluded": 0, "cwd_null": 0}
    for row in rows:
        sid, slug = str(row["session_id"]), str(row["project_slug"])
        info = meta.get(sid)
        if info is None:
            counts["no_session"] += 1
            items.append({"session_id": sid, "slug": slug, "kind": "no_session",
                          "why": "state.db 里没有这条会话（cwd 无从对齐）",
                          "status": row["status"], "source": row["source"]})
            continue
        project = projects.get(slug)
        if project is None:
            counts["no_project"] += 1
            items.append({"session_id": sid, "slug": slug, "kind": "no_project",
                          "why": "归属指向的项目在 projects.db 里不存在", "cwd": info["cwd"],
                          "session_source": info["source"], "title": info["title"],
                          "status": row["status"], "source": row["source"]})
            continue
        target = project.get("primary_path")
        if not target:
            counts["no_path"] += 1
            items.append({"session_id": sid, "slug": slug, "kind": "no_path",
                          "why": "项目没有 primary_path（无权威目录可对齐）", "cwd": info["cwd"],
                          "session_source": info["source"], "title": info["title"],
                          "status": row["status"], "source": row["source"]})
            continue
        cwd = info["cwd"]
        same_path = norm_path(cwd) == norm_path(target)
        same_slug = basename_of(cwd) == slug
        if same_path and same_slug:
            counts["consistent"] += 1
            continue
        counts["inconsistent"] += 1
        if cwd is None:
            counts["cwd_null"] += 1
        is_kanban = info["source"] == ent.KANBAN_SOURCE
        if is_kanban:
            counts["kanban_excluded"] += 1
        items.append({
            "session_id": sid, "slug": slug, "kind": "mismatch",
            "title": info["title"], "status": row["status"], "source": row["source"],
            "cwd": cwd, "cwd_dir": basename_of(cwd), "cwd_null": cwd is None,
            "target_path": target, "path_equal": same_path, "slug_equal": same_slug,
            "session_source": info["source"], "kanban": is_kanban,
            "blocked": "kanban_excluded" if is_kanban else None,
            "target_dir_exists": Path(str(target)).is_dir(),
        })
    items.sort(key=lambda item: (item.get("kind") != "mismatch", str(item.get("slug")),
                                 str(item.get("session_id"))))
    notes = []
    if counts["inconsistent"]:
        notes.append("不一致 %d 条（判据 %s）；其中 %d 条是看板任务，对齐时一律排除"
                     % (counts["inconsistent"], CONSISTENCY_VERSION, counts["kanban_excluded"]))
    else:
        notes.append("全部一致（判据 %s，共 %d 条）" % (CONSISTENCY_VERSION, counts["total"]))
    if counts["cwd_null"]:
        notes.append("%d 条会话 cwd 为空（NULL）：对齐会写入目标目录，撤销会写回 NULL" % counts["cwd_null"])
    return {"ok": True, "items": items, "counts": counts, "notes": notes,
            "judge": CONSISTENCY_VERSION,
            "projects": {k: {"name": v.get("name"), "path": v.get("primary_path")}
                         for k, v in projects.items()}}


def mismatch_pairs(home: Path) -> List[Dict[str, Any]]:
    """按 ``(归属项目, cwd 目录)`` 聚合的不一致对，只返回 ``mismatch`` 类。"""
    report = audit(home)
    buckets: Dict[Tuple[str, Optional[str]], Dict[str, Any]] = {}
    for item in report["items"]:
        if item.get("kind") != "mismatch":
            continue
        key = (item["slug"], item.get("cwd_dir"))
        bucket = buckets.setdefault(key, {"slug": item["slug"], "cwd_dir": item.get("cwd_dir"),
                                          "count": 0, "kanban": 0, "session_ids": []})
        bucket["count"] += 1
        if item.get("kanban"):
            bucket["kanban"] += 1
        bucket["session_ids"].append(item["session_id"])
    out = list(buckets.values())
    out.sort(key=lambda b: (-b["count"], b["slug"] or "", b["cwd_dir"] or ""))
    return out


# ---------------------------------------------------------------------------
# 对齐修复：预览
# ---------------------------------------------------------------------------

def preview(home: Path, *, mode: str = ent.MODE_FIX_CWD, slugs: Optional[List[str]] = None,
            session_ids: Optional[List[str]] = None,
            include_kanban: bool = False) -> Dict[str, Any]:
    """生成对齐计划。**纯只读**：不建表、不写库、不碰目录。

    ``include_kanban=False``（默认）时看板任务会话一律 ``blocked``（硬约束）。
    传 ``True`` 只用于「用户明确要求连存量看板脏数据一起对齐 cwd」——那**不是**清理归属，
    只是让它们的 cwd 也指向归属项目的目录；归属行照旧留着。
    """
    mode = str(mode or ent.MODE_FIX_CWD).strip()
    if mode not in ent.MODES:
        return {"ok": False, "code": "invalid_mode", "error": "mode 只能是 " + "/".join(ent.MODES)}
    report = audit(home)
    projects = report["projects"]
    want_slugs = {s for s in (slugs or []) if s}
    want_ids = {s for s in (session_ids or []) if s}
    items: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    for item in report["items"]:
        if item.get("kind") != "mismatch":
            blocked.append({"session_id": item.get("session_id"), "slug": item.get("slug"),
                            "why": item.get("kind"), "detail": item.get("why")})
            continue
        if want_slugs and item["slug"] not in want_slugs:
            continue
        if want_ids and item["session_id"] not in want_ids:
            continue
        if item.get("kanban") and not include_kanban:
            blocked.append({"session_id": item["session_id"], "slug": item["slug"],
                            "why": "kanban_excluded",
                            "detail": "看板任务会话不得进入普通项目（source=kanban）"})
            continue
        cwd_dir = item.get("cwd_dir")
        if mode == ent.MODE_FIX_CWD:
            to_slug, to_cwd = item["slug"], item["target_path"]
            if not item.get("target_dir_exists"):
                blocked.append({"session_id": item["session_id"], "slug": item["slug"],
                                "why": "dir_missing",
                                "detail": "项目权威目录不存在，停手不建：" + str(to_cwd)})
                continue
            action = ("write_cwd" if norm_path(item.get("cwd")) != norm_path(to_cwd) else "noop")
        else:
            to_cwd = item.get("cwd")
            to_slug = cwd_dir if cwd_dir in projects else None
            if to_slug is None:
                blocked.append({"session_id": item["session_id"], "slug": item["slug"],
                                "why": "no_project_for_cwd",
                                "detail": "cwd 目录名 %r 在 projects.db 里没有对应项目（不静默新建）"
                                          % (cwd_dir,)})
                continue
            action = ("write_slug" if to_slug != item["slug"] else "noop")
        items.append({
            "session_id": item["session_id"], "title": item.get("title"),
            "from_slug": item["slug"], "to_slug": to_slug,
            "from_cwd": item.get("cwd"), "to_cwd": to_cwd,
            "cwd_null": item.get("cwd_null"), "session_source": item.get("session_source"),
            "action": action,
        })
    consistent = report["counts"].get("consistent") or 0
    affected = [i for i in items if i["action"] != "noop"]
    notes = [
        ("口径 fix_cwd：归属为准，把会话 cwd 改成项目权威目录（桌面端项目树会跟着动）"
         if mode == ent.MODE_FIX_CWD else
         "口径 fix_slug：cwd 为准，把归属改成 cwd 目录名对应的项目（目录与项目树不动）"),
        "判定依据：" + report["judge"] + "；一致 " + str(consistent) + " 条",
        "计划改动 %d 条，阻塞 %d 条（看板/目录缺失/无对应项目一律不动）"
        % (len(affected), len(blocked)),
    ]
    return {"ok": True, "mode": mode, "items": items, "affected": len(affected),
            "blocked": blocked, "notes": notes,
            "counts": {"planned": len(affected), "blocked": len(blocked), "consistent": consistent,
                       "inconsistent": report["counts"].get("inconsistent", 0)}}


# ---------------------------------------------------------------------------
# 对齐修复：执行（落台账 → 写 → 逐条回读）
# ---------------------------------------------------------------------------

def _write_cwd(home: Path, session_id: str, value: Optional[str]) -> bool:
    """只改这一行的 cwd。返回是否真的更新到了行（行不存在 ⇒ False，绝不假装成功）。"""
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


def _write_slug(home: Path, session_id: str, slug: str) -> bool:
    path = ent.store_db(home)
    if not path.is_file():
        return False
    conn = ent._connect_rw(path)
    try:
        with conn:
            cur = conn.execute("UPDATE assignments SET project_slug = ?, updated_at = ?"
                               " WHERE session_id = ?", (slug, time.time(), session_id))
        return cur.rowcount > 0
    finally:
        conn.close()


def execute(home: Path, *, mode: str = ent.MODE_FIX_CWD, session_ids: Optional[List[str]] = None,
            slugs: Optional[List[str]] = None, reason: str = "一致性对齐修复",
            include_kanban: bool = False) -> Dict[str, Any]:
    """执行对齐：**先落台账（写前整行 + cwd 原值）→ 写 → 逐条回读**。

    跨库原子性说明见模块 docstring：不谎称同一事务，靠「台账先行 + 写失败即中止 + 归属写失败
    立刻补偿 cwd」保证可撤销。撤销走 ``api_autogroup_panel._undo_panel_op(op_id)``。
    """
    plan = preview(home, mode=mode, slugs=slugs, session_ids=session_ids,
                   include_kanban=include_kanban)
    if not plan.get("ok"):
        return plan
    want = [item for item in plan["items"] if item["action"] != "noop"]
    if not want:
        return {"ok": True, "data": {"op_id": None, "written": 0, "skipped": 0,
                                     "sessions": [], "mode": plan["mode"]},
                "notes": ["没有需要改的条目（计划改动 0 条）"] + plan["notes"]}
    if len(want) > ent.ALIGN_MAX:
        return {"ok": False, "code": "too_many", "error": "一次最多对齐 %d 条" % ent.ALIGN_MAX}
    ids = [item["session_id"] for item in want]
    meta = ent.session_meta(home, ids)
    # 二次校验看板硬约束：预览到写入之间会话来源可能变，写前再确认一次
    if not include_kanban:
        now_kanban = [sid for sid in ids if (meta.get(sid) or {}).get("source") == ent.KANBAN_SOURCE]
        if now_kanban:
            return {"ok": False, "code": "kanban_excluded",
                    "error": "看板任务会话不得写入：%d 条" % len(now_kanban),
                    "session_ids": now_kanban}
    missing = [sid for sid in ids if sid not in meta]
    if missing:
        return {"ok": False, "code": "session_missing",
                "error": "state.db 里找不到 %d 条会话，中止（不写半截）" % len(missing),
                "session_ids": missing}

    op_id, batch_id, now = panel._new_id("op-"), panel._new_id("al-"), time.time()
    op_name = "align." + plan["mode"]      # align.fix_cwd / align.fix_slug（台账里可辨认）
    core, conn = panel._store(home)          # 打开归属库并补建 console_ops 台账
    written: List[Dict[str, Any]] = []
    try:
        with conn:
            panel._record_header(conn, op_id=op_id, op=op_name,
                                 batch_id=batch_id, created_at=now,
                                 payload={"mode": plan["mode"], "judge": CONSISTENCY_VERSION,
                                          "session_ids": ids, "reason": reason})
            conn.execute("INSERT OR REPLACE INTO batches(batch_id, created_at, note, undone)"
                         " VALUES(?,?,?,0)", (batch_id, now, "对齐修复 " + plan["mode"]))
            for item in want:
                sid = item["session_id"]
                row_before = panel._assign_row(conn, sid)
                exists, cwd_before = panel._read_cwd(home, sid)
                slug_before = row_before.get("project_slug") if row_before else None
                slug_after = item["to_slug"]
                if plan["mode"] == ent.MODE_FIX_CWD:
                    ok = _write_cwd(home, sid, item["to_cwd"])
                    if not ok:
                        raise RuntimeError("写 cwd 失败（行不存在？）：" + sid)
                    cwd_written = True
                    if slug_before != slug_after:
                        cur = conn.execute("UPDATE assignments SET project_slug = ?, updated_at = ?"
                                           " WHERE session_id = ?", (slug_after, now, sid))
                        if not cur.rowcount:
                            _write_cwd(home, sid, cwd_before)   # 归属没落 → cwd 补偿回原值
                            raise RuntimeError("写归属失败，已把 cwd 还原：" + sid)
                else:
                    cwd_written = False
                    cur = conn.execute("UPDATE assignments SET project_slug = ?, updated_at = ?"
                                       " WHERE session_id = ?", (slug_after, now, sid))
                    if not cur.rowcount:
                        raise RuntimeError("写归属失败：" + sid)
                panel._record_session(conn, op_id=op_id, op=plan["mode"], session_id=sid,
                                      row_before=row_before, cwd_known=exists,
                                      cwd_before=cwd_before, cwd_written=cwd_written,
                                      slug_before=slug_before, slug_after=slug_after,
                                      batch_id=batch_id, created_at=now)
                written.append({"session_id": sid, "from_slug": slug_before, "to_slug": slug_after,
                                "cwd_before": cwd_before, "cwd_after": item["to_cwd"],
                                "cwd_written": cwd_written})
    except Exception as exc:  # noqa: BLE001 - 事务已回滚；cwd 侧按上面的补偿处理
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "code": "align_failed",
                "error": "对齐写入失败（归属事务已回滚）：" + base._describe(exc),
                "op_id": op_id, "batch_id": batch_id, "written_before_fail": len(written)}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return _readback(home, op_id=op_id, batch_id=batch_id, mode=plan["mode"],
                     written=written, now=now)


def _readback(home: Path, *, op_id: str, batch_id: str, mode: str,
              written: List[Dict[str, Any]], now: float) -> Dict[str, Any]:
    """逐条回读：slug 与 cwd 都必须等于计划值。任何一条不符 ⇒ 500 级失败，不谎报成功。"""
    meta = ent.session_meta(home, [w["session_id"] for w in written])
    path = ent.store_db(home)
    conn = ent._connect_ro(path)
    try:
        sql = ("SELECT session_id, project_slug FROM assignments WHERE session_id IN ("
               + ",".join("?" * len(written)) + ")")
        slugs = {str(r["session_id"]): str(r["project_slug"]) for r in conn.execute(sql, [w["session_id"] for w in written])}
    finally:
        conn.close()
    bad: List[Dict[str, Any]] = []
    for w in written:
        sid = w["session_id"]
        info = meta.get(sid)
        if info is None:
            bad.append({"session_id": sid, "why": "回读时会话不见了"})
            continue
        diff: Dict[str, Any] = {}
        if slugs.get(sid) != w["to_slug"]:
            diff["project_slug"] = {"want": w["to_slug"], "got": slugs.get(sid)}
        if mode == ent.MODE_FIX_CWD and norm_path(info["cwd"]) != norm_path(w["cwd_after"]):
            diff["cwd"] = {"want": w["cwd_after"], "got": info["cwd"]}
        if diff:
            bad.append({"session_id": sid, "diff": diff})
    after = audit(home)
    notes = ["口径 %s：写 %d 条；回读 %s" % (mode, len(written), "全部一致" if not bad else "发现 %d 条不符" % len(bad)),
             "撤销：POST /autogroup/undo {op_id: %s}（或 batch_id: %s），cwd 逐条回原值" % (op_id, batch_id)]
    if mode == ent.MODE_FIX_CWD:
        notes.append("桌面端项目树现在按归属项目分组；不一致剩余 %d 条（含看板排除项）"
                     % after["counts"]["inconsistent"])
    if bad:
        return {"ok": False, "code": "readback_failed",
                "error": "写入返回成功但回读不一致：%d 条" % len(bad),
                "data": {"op_id": op_id, "batch_id": batch_id, "mismatched": bad,
                         "written": len(written)}, "notes": notes}
    return {"ok": True, "data": {"op_id": op_id, "batch_id": batch_id, "op": mode,
                                 "written": len(written), "sessions": written,
                                 "readback": {"checked": len(written), "mismatched": []},
                                 "audit_after": after["counts"]}, "notes": notes}

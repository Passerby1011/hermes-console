# -*- coding: utf-8 -*-
"""hermes-console · 项目分组**自动归组引擎**（t_0c8ab8c1 的 B 块）。

一句话：**自动模式开着的时候，命中 ``rules.json`` 且置信度达阈值的会话直接写 ``applied``；
不确定的留 ``suggested``；关着的时候连归属库都不打开（零副作用）。**

本文件只做引擎，不做 HTTP 路由也不做周期调度：

* HTTP 面由 ``api_autogroup_panel.py`` 的 ``POST /autogroup/auto/tick`` 调 :func:`run_tick`；
* 周期触发由同目录的 ``autogroup_auto_daemon.py``（独立守护进程）调 :func:`run_tick`；
* 判定/存储/项目表全部复用 ``plugins/hermes-auto-group/core.py``（``classify_session`` /
  ``list_sessions`` / ``first_user_text`` / ``load_rules`` / ``save_suggestions``），本文件
  不重新实现任何分类逻辑。

为什么周期触发用**独立守护进程**而不是 dashboard 进程里的守护线程：

1. 本卡硬约束「不重启 9119 serve」——写进 serve 进程的线程要等下次重启才存在，等于没交付；
2. 线程跟着 web 进程的生死走，测不了也观察不到；独立进程有 pid 文件 + 心跳文件，能单独启停、
   单独验收（沙盒 home 跑一遍就是真跑一遍）；
3. 关闭语义更硬：守护进程每轮只读 ``auto-group/auto_mode.json``，``enabled=false`` 时
   **一行都不写**（连心跳文件都不写），不会出现「线程还在后台偷偷跑」这种事。

台账与撤销：自动归入写的是 ``console_ops`` 台账（``op='auto.apply'``），和面板手动归入
**同一张表、同一套快照字段**（逐会话 ``row_before`` 整行 + cwd 原值），所以
``POST /autogroup/undo {op_id}`` 对自动写入同样逐字段还原。这里刻意**复用** panel 模块的
写入函数（:func:`_panel`），不复制一份 schema —— 撤销代码只有一份，不会两边跑偏。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_DASHBOARD_DIR = Path(__file__).resolve().parent
if str(_DASHBOARD_DIR) not in sys.path:
    # 单独 import 本模块（探针/守护进程）时也要能找到兄弟模块。
    sys.path.insert(0, str(_DASHBOARD_DIR))

import api_autogroup as base  # noqa: E402 - 复用 _load_core / _home / _fail

#: 一轮最多看多少会话（与面板 tick 的上限同口径）。
AUTO_TICK_MAX = 40

#: 置信度阈值：达阈值直接写 applied，低于阈值只留 suggested。
#: 取值来源是 ``core.MIN_CONFIDENCE``（0.65），运行时读不到才用这个兜底。
CONFIDENT_MIN = 0.65

#: 守护进程心跳文件名（放在 ``home/auto-group/`` 下，与 auto_mode.json 同目录）。
DAEMON_STATE_NAME = "auto_daemon.json"

#: 守护进程自己认为「多久没心跳算死了」（秒）；面板据此决定要不要重新拉起来。
DAEMON_STALE_SECONDS = 180


def _panel():
    """延迟 import 面板模块（它 import 本文件，顶层互相 import 会成环）。

    只为拿到 :func:`_store` / :func:`_record_header` / :func:`_record_session` /
    :func:`_read_auto_state` / :func:`_write_auto_state` —— 台账 schema 与撤销代码都只有一份。
    """
    import api_autogroup_panel as panel

    return panel


def _now(now: Optional[float]) -> float:
    return float(now) if now is not None else time.time()


def _new_id(prefix: str) -> str:
    return prefix + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _threshold(core: Any, override: Optional[float]) -> float:
    if override is not None:
        try:
            return max(0.0, min(1.0, float(override)))
        except (TypeError, ValueError):
            pass
    return float(getattr(core, "MIN_CONFIDENCE", CONFIDENT_MIN) or CONFIDENT_MIN)


def _current_rows(home: Path, core: Any) -> Dict[str, Dict[str, Any]]:
    """归属表现状（只读）：{session_id: 整行}。库不存在就是空表，不建库。"""
    path = core.store_path(home)
    if not path.exists():
        return {}
    conn = core.connect_ro(path)
    try:
        return {str(r["session_id"]): dict(r) for r in conn.execute("SELECT * FROM assignments")}
    finally:
        conn.close()


def plan_tick(home: Path, *, limit: int = AUTO_TICK_MAX,
              threshold: Optional[float] = None, agent: Optional[str] = None) -> Dict[str, Any]:
    """**只读**阶段：分类 + 幂等过滤，得出「这一轮该写什么」。

    幂等口径（同一条会话不重复处理）：

    * 已经是 ``applied`` 的会话**一律不碰**（人工或上一轮的自动写入都不覆盖）；
    * ``suggested`` 行且判定结果与库里完全一致 ⇒ 不写（重复跑第二次 0 写入）；
    * ``suggested`` 行这次达到阈值 ⇒ 升级成 ``applied``（这不是重复写，是把「建议」变「事实」）；
    * 判定不到项目 ⇒ 记 unmatched，不写。
    """
    core = base._load_core()
    projects = core.list_projects(home)
    #: 关键词规则必须带 agent（t_e0d081aa）：不带 ⇒ 回落 ``DEFAULT_RULES``（小白 17 个项目库），
    #: 于是 work/xiaomo/worker 的会话被关键词判进 mt-table 这类别人的项目（跨 agent 串组）。
    rules = core.load_rules(home, agent)
    th = _threshold(core, threshold)
    # 看板硬约束（t_53b48743）：自动模式同样不得把看板任务会话写进普通项目。
    # 先用 core 的口径过滤（include_kanban=False），循环里再按 source 兜一道。
    sessions = core.list_sessions(home, limit=max(1, min(AUTO_TICK_MAX, int(limit))),
                                  exclude_ids=core.rejected_ids(home), include_kanban=False)
    sessions = [s for s in sessions
                if str(s.get("source") or "").strip().lower() != base.KANBAN_SOURCE]
    current = _current_rows(home, core)
    to_apply: List[Dict[str, Any]] = []
    to_suggest: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = []
    for session in sessions:
        session_id = str(session["id"])
        body = core.first_user_text(home, session_id)
        verdict = core.classify_session(None, session, body, projects, rules, use_llm=False)
        slug = verdict.get("project_slug")
        confidence = float(verdict.get("confidence") or 0.0)
        row = current.get(session_id)
        record = {"session_id": session_id, "title": session.get("title") or "",
                  "project_slug": slug, "confidence": round(confidence, 2),
                  "reason": verdict.get("reason") or "", "source": verdict.get("source") or "none"}
        if not slug:
            unmatched.append(record)
            continue
        if row is not None and row.get("status") == "applied":
            skipped.append(dict(record, why="已经是 applied（" + str(row.get("project_slug"))
                                + "），自动模式不覆盖既有归属"))
            continue
        if confidence >= th:
            to_apply.append(dict(record, row_before=row,
                                 why="置信度 " + str(round(confidence, 2)) + " ≥ 阈值 " + str(th)
                                     + " ⇒ 直接写 applied"))
        else:
            if row is not None and row.get("project_slug") == slug \
                    and abs(float(row.get("confidence") or 0) - confidence) < 1e-9:
                skipped.append(dict(record, why="建议行与本次判定完全一致，不重复写"))
                continue
            to_suggest.append(dict(record,
                                   why="置信度 " + str(round(confidence, 2)) + " < 阈值 " + str(th)
                                       + " ⇒ 只留建议"))
    return {"core": core, "threshold": th, "scanned": len(sessions), "to_apply": to_apply,
            "to_suggest": to_suggest, "skipped": skipped, "unmatched": unmatched,
            "projects": {str(p.get("slug")): p for p in projects}}


def _target_path(plan: Dict[str, Any], slug: str) -> Optional[str]:
    """项目权威目录（``primary_path``，退回第一个 folder）。"""
    project = (plan.get("projects") or {}).get(str(slug)) or {}
    return project.get("primary_path") or (
        project["folders"][0]["path"] if project.get("folders") else None)


def _write_applied(home: Path, plan: Dict[str, Any], *, trigger: str, now: float) -> Dict[str, Any]:
    """把「该直接归入」的那批写进归属表：先落台账（写前整行 + cwd 原值）→ 写归属 + 写 cwd → 回读。

    **写归属 = 写 cwd**（t_53b48743）：cwd 写不成就把这条归属退掉，不让两套数据分叉；
    项目目录缺失的会话**直接跳过**（不静默建空目录）。
    """
    panel = _panel()
    core = plan["core"]
    items = plan["to_apply"]
    op_id, batch_id = _new_id("op-"), _new_id("an-")
    written: List[str] = []
    skipped: List[Dict[str, Any]] = []
    cwd_undo: List[Dict[str, Any]] = []
    _, conn = panel._store(home)
    try:
        with conn:
            panel._record_header(
                conn, op_id=op_id, op="auto.apply", batch_id=batch_id, created_at=now,
                payload={"trigger": trigger, "threshold": plan["threshold"],
                         "session_ids": [i["session_id"] for i in items],
                         "sync_cwd": True,
                         "reason": "自动模式：规则命中且置信度达阈值，直接写 applied"})
            conn.execute("INSERT OR REPLACE INTO batches(batch_id, created_at, note, undone)"
                         " VALUES(?,?,?,0)", (batch_id, now, "自动归入 %d 条" % len(items)))
            for item in items:
                session_id, slug = item["session_id"], item["project_slug"]
                target = _target_path(plan, slug)
                if not target:
                    skipped.append({"session_id": session_id, "why": "项目没有权威目录，跳过（归属不落）"})
                    continue
                if not Path(str(target)).is_dir():
                    skipped.append({"session_id": session_id,
                                    "why": "项目目录不存在，跳过（不静默建空目录）：" + str(target)})
                    continue
                row_before = panel._assign_row(conn, session_id)
                cwd_known, cwd_before = panel._read_cwd(home, session_id)
                slug_before = row_before.get("project_slug") if row_before else None
                if row_before is None:
                    conn.execute(
                        "INSERT INTO assignments(session_id, project_slug, confidence, reason, source,"
                        " status, batch_id, applied_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (session_id, slug, item["confidence"], item["reason"], "auto", "applied",
                         batch_id, now, now))
                else:
                    conn.execute(
                        "UPDATE assignments SET project_slug = ?, confidence = ?, reason = ?, source = ?,"
                        " status = ?, batch_id = ?, applied_at = ?, updated_at = ? WHERE session_id = ?",
                        (slug, item["confidence"], item["reason"], "auto", "applied", batch_id, now,
                         now, session_id))
                # 写归属 = 写 cwd：写不成 ⇒ 抛错让整个事务回滚，归属不落
                if not panel._write_cwd(home, session_id, str(target)):
                    raise RuntimeError("写 cwd 失败，归属不落：" + session_id)
                cwd_undo.append({"session_id": session_id, "cwd_before": cwd_before})
                panel._record_session(conn, op_id=op_id, op="auto.apply", session_id=session_id,
                                      row_before=row_before, cwd_known=cwd_known, cwd_before=cwd_before,
                                      cwd_written=True, slug_before=slug_before, slug_after=slug,
                                      batch_id=batch_id, created_at=now)
                written.append(session_id)
    except Exception:  # noqa: BLE001 - 归属事务已回滚，把 cwd 补偿回原值再抛
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        for rec in cwd_undo:
            panel._write_cwd(home, rec["session_id"], rec["cwd_before"])
        raise
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    # 回读：不信写入返回值，逐条查回写后的整行 + cwd
    mismatched: List[Dict[str, Any]] = []
    path = core.store_path(home)
    rconn = core.connect_ro(path)
    try:
        for item in items:
            if item["session_id"] not in written:
                continue
            got = panel._assign_row(rconn, item["session_id"])
            if got is None:
                mismatched.append({"session_id": item["session_id"], "why": "写完回读没有这一行"})
                continue
            diff = {}
            for key, want in (("project_slug", item["project_slug"]), ("status", "applied"),
                              ("batch_id", batch_id), ("source", "auto")):
                if got.get(key) != want:
                    diff[key] = {"want": want, "got": got.get(key)}
            exists, cwd_now = panel._read_cwd(home, item["session_id"])
            want_cwd = _target_path(plan, item["project_slug"])
            if not exists or cwd_now != want_cwd:
                diff["cwd"] = {"want": want_cwd, "got": cwd_now}
            if diff:
                mismatched.append({"session_id": item["session_id"], "diff": diff})
    finally:
        rconn.close()
    return {"op_id": op_id, "batch_id": batch_id, "written": written, "skipped": skipped,
            "readback": {"checked": len(written), "mismatched": mismatched}}


def run_tick(home: Path, *, limit: int = AUTO_TICK_MAX, threshold: Optional[float] = None,
             trigger: str = "manual", now: Optional[float] = None,
             agent: Optional[str] = None) -> Dict[str, Any]:
    """自动模式跑一轮。**关着就立刻返回、连归属库都不打开**。"""
    panel = _panel()
    ts = _now(now)
    state = panel._read_auto_state(home)
    if not state.get("enabled"):
        return {"ok": True, "data": {"skipped": True, "enabled": False, "trigger": trigger,
                                     "scanned": 0, "applied": 0, "suggested": 0, "wrote": 0,
                                     "rows": [], "last_tick": state.get("last_tick")},
                "notes": ["自动模式关闭（enabled=false）：本次 tick 直接跳过，没读也没写归属表"]}
    try:
        plan = plan_tick(home, limit=limit, threshold=threshold, agent=agent)
    except Exception as exc:  # noqa: BLE001
        return base._fail("tick_failed", "自动归组分类失败：" + base._describe(exc), status_code=500)
    applied = {"op_id": None, "batch_id": None, "written": [], "skipped": [],
               "readback": {"checked": 0, "mismatched": []}}
    # 写归属这一段必须与 scan 的写建议互斥：实测（t_aac43d35）两者并发时，
    # scan 的 upsert 会把这里刚写好的 applied 按回 suggested（cwd 却不回退）。
    with base.WRITE_LOCK:
        if plan["to_apply"]:
            try:
                applied = _write_applied(home, plan, trigger=trigger, now=ts)
            except Exception as exc:  # noqa: BLE001
                return base._fail("auto_write_failed", "自动归入写库失败（已回滚）：" + base._describe(exc),
                                  status_code=500)
        suggested_saved = 0
        if plan["to_suggest"]:
            try:
                suggested_saved = plan["core"].save_suggestions(
                    home, [{"session_id": i["session_id"], "project_slug": i["project_slug"],
                            "confidence": i["confidence"], "reason": i["reason"], "source": i["source"]}
                           for i in plan["to_suggest"]])
            except Exception as exc:  # noqa: BLE001 - 建议写失败不该抹掉已经落地的 applied
                suggested_saved = 0
                plan["suggested_error"] = base._describe(exc)
        try:
            panel._write_auto_state(home, dict(state, last_tick=ts))
        except Exception:  # noqa: BLE001 - last_tick 只是观测量
            pass
    mismatched = applied["readback"]["mismatched"]
    result: Dict[str, Any] = {
        "ok": not mismatched,
        "data": {"skipped": False, "enabled": True, "trigger": trigger, "threshold": plan["threshold"],
                 "scanned": plan["scanned"], "matched": plan["scanned"] - len(plan["unmatched"]),
                 "unmatched": len(plan["unmatched"]),
                 "applied": len(applied["written"]), "wrote": len(applied["written"]),
                 "suggested": suggested_saved, "skipped_existing": len(plan["skipped"]),
                 "op_id": applied["op_id"], "batch_id": applied["batch_id"],
                 "readback": applied["readback"], "last_tick": ts,
                 "rows": [{"session_id": i["session_id"], "title": i["title"],
                           "project_slug": i["project_slug"], "confidence": i["confidence"],
                           "why": i["why"], "ok": i["session_id"] not in
                           {m["session_id"] for m in mismatched}} for i in plan["to_apply"]],
                 "suggested_rows": [{"session_id": i["session_id"], "title": i["title"],
                                     "project_slug": i["project_slug"],
                                     "confidence": i["confidence"], "why": i["why"]}
                                    for i in plan["to_suggest"]],
                 "skipped_rows": plan["skipped"], "unmatched_rows": plan["unmatched"],
                 "skipped_no_dir": applied.get("skipped") or []},
        "notes": ["阈值 %s：达阈值直接写 applied，低于阈值只留建议" % plan["threshold"],
                  "本轮自动归入 %d 条、留建议 %d 条、跳过 %d 条（已 applied / 无变化）"
                  % (len(applied["written"]), suggested_saved, len(plan["skipped"]))]}
    if applied.get("skipped"):
        result["notes"].append("%d 条因项目目录缺失/无权威目录被跳过（归属未写）"
                               % len(applied["skipped"]))
    if applied["op_id"]:
        result["notes"].append("撤销：POST /autogroup/undo {op_id: %s}" % applied["op_id"])
    if mismatched:
        result["error"] = {"code": "readback_failed",
                           "message": "写入返回成功，但回读与预期不一致", "mismatched": mismatched}
    return result


# --------------------------------------------------------------------------
# 周期触发（t_ac537221 收敛）
# --------------------------------------------------------------------------
# 原来这里有一整套「独立守护进程」：``daemon_state_path`` / ``read_daemon_state`` /
# ``daemon_alive`` / ``ensure_daemon`` + 同目录的 ``autogroup_auto_daemon.py``。
# 它和 ``POST /autogroup/auto/tick`` 调的是**同一个** ``run_tick``，只是触发者不同 ——
# 属于同一件事的第二套实现，本卡删除（同一个功能只留一个入口）。
# 现在的周期触发 = 外部调度（cron / 手动点「立即跑一轮」）调 ``run_tick``。





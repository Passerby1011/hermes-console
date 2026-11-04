# -*- coding: utf-8 -*-
"""AI 分类全链路集成验收测试（合成数据，临时 home，不碰真实环境）。

这组测试刻意只做「验收」：全部会话都是人工合成的 ``synth-01``…``synth-10``，
标题里写明「合成测试数据」；引擎输出用**手工构造的 ai-preview/1 dict** 注入，
不调用真实模型、不联网；``snapshot`` / ``state`` 一律由服务端从沙盒里的
``state.db`` / ``assignments.db`` 自读，测试不伪造。

覆盖的硬约束（与 TASK.md 的 15 条一一对应）：
1. 10 条混批的 apply / suggest / unjudged 三档计数与 status_if_confirmed；
2. 同主题 3 条会话聚成一个 new_project 候选，建项目前不可写；
3. 已存在归属（protected fixture）在确认另一条后 9 列一字未变；
4. 已 applied 的会话被服务端硬性排除，原归属绝不改动；
5. 0.5 / 0.7999 / 0.8 / 0.4999 四个边界的分档与 cwd 语义；
6. 低置信度条目塞进 selected 时整批 selection_rejected，零写入；
7. 不带 confirm=true 的确认请求一行都不写；
8. post_ai_project 建真实目录 + projects.db 注册，之后条目可写并回读；
9. 先取消建项目不消耗条件，第二次 confirm 仍然成功；
10. 同一 batch_id 第二次确认 409 batch_consumed，归属行不被二次改写；
11. 预览后归属行被手改 ⇒ 第一条跳过、整批 paused、not_executed 如实报告；
12. 预览后项目目录被删 ⇒ 整批 409 batch_paused；
13. 预览后会话 cwd 被改 ⇒ 跳过该条并暂停整批，且不覆盖别人的新值；
14. 4 条整批撤销逐字段还原（含原本不存在 / 原本 suggested / 新建项目清理）；
15. 撤销时发现后续人工改动 ⇒ 409 undo_paused，手工值原样保留。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import unittest

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

_REMOVED_REASON = (
    "t_ac537221 已把 AI 预览/落库流水线并入 /autogroup/scan + /autogroup/apply；"
    "api_autogroup_ai_apply / api_autogroup_ai_undo / api_autogroup_ai_run / "
    "autogroup_ai_contract 等模块已删除。本文件保留作历史契约参考，不再对当前代码执行。"
)
_REMOVED_MODULES = {
    "api_autogroup_ai_apply", "api_autogroup_ai_undo", "api_autogroup_ai_run",
    "autogroup_ai_contract", "autogroup_ai_projects", "autogroup_ai_store",
}
try:
    from ai_sandbox import Sandbox, body, status_of  # noqa: E402

    import api_autogroup_ai_apply as apply_mod  # noqa: E402
    import api_autogroup_ai_undo as undo_mod  # noqa: E402
    import autogroup_ai_contract as contract  # noqa: E402
    import autogroup_ai as ai_engine  # noqa: E402
except ModuleNotFoundError as _exc:
    if _exc.name not in _REMOVED_MODULES:
        raise
    pytestmark = pytest.mark.skip(_REMOVED_REASON)

SYNTH_IDS = tuple("synth-%02d" % index for index in range(1, 11))


# ---------------------------------------------------------------- 合成数据工具

def _seed_synth(sb: Sandbox, count: int = 10, titles: dict | None = None) -> None:
    """在沙盒里造 ``synth-01``… 会话；标题统一带「合成测试数据」。"""
    titles = titles or {}
    for index in range(1, count + 1):
        session_id = "synth-%02d" % index
        title = titles.get(session_id) or "合成测试数据 %s：第 %d 条会话" % (session_id, index)
        sb.add_session(session_id, title, "D:/synth/%02d" % index, message_count=4)


def _item(session_id: str, project_slug: str | None, confidence: float, *, title: str | None = None,
          new_project: dict | None = None, support: list | None = None,
          unjudged_reason: str | None = None) -> dict:
    """构造一条 ai-preview/1 引擎输出项（手工合成，不是真实模型结果）。"""
    project_name = (new_project or {}).get("name") if new_project else project_slug
    return {
        "session_id": session_id,
        "title": title or ("合成测试数据 %s" % session_id),
        "snapshot": {"cwd": None, "message_count": 4, "last_activity_at": 0.0},
        "suggested": {
            "project_slug": project_slug,
            "project_name": project_name,
            "confidence": confidence,
            "reason": "合成测试数据：人工构造的理由",
            "new_project": new_project,
            "support_session_ids": list(support or []),
        },
        "unjudged_reason": unjudged_reason,
    }


def _preview(*items: dict) -> dict:
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "generated_at": 1793340000.0,
        "engine": {"name": "autogroup_ai", "model": "synthetic-fixture-v1", "elapsed_ms": 1.5,
                   "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                   "usage_estimated": False, "usage_note": "合成测试数据：不调用真实模型"},
        # 与真实引擎同口径（批次落库后确认入口会校验它）
        "scope": {"scope_rule": ai_engine.SCOPE_RULE, "total_available": len(items),
                  "excluded_kanban": 0},
        "items": list(items),
        "notes": ["合成测试数据：10 条人工构造的会话，不是生产快照"],
    }


def _post_preview(*items: dict) -> dict:
    resp = apply_mod.post_ai_preview({"preview": _preview(*items)})
    data = body(resp)
    if status_of(resp) != 200 or not data.get("ok"):
        raise AssertionError("预览失败：status=%s body=%r" % (status_of(resp), data))
    return data["data"]


def _by_session(items: list, session_id: str) -> dict:
    return next(item for item in items if item["session_id"] == session_id)


# ---------------------------------------------------------------- 数据库快照工具

def _all_assignments(sb: Sandbox) -> list:
    """整个 assignments 表的 ``SELECT *`` 快照（按 session_id 排序，便于前后比较）。"""
    path = sb.home / "auto-group" / "assignments.db"
    conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM assignments ORDER BY session_id")]
    finally:
        conn.close()


def _all_cwds(sb: Sandbox) -> dict:
    """state.db 里全部会话的 cwd 快照。"""
    path = sb.home / "state.db"
    conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return {str(row["id"]): row["cwd"] for row in
                conn.execute("SELECT id, cwd FROM sessions ORDER BY id")}
    finally:
        conn.close()


def _set_cwd(sb: Sandbox, session_id: str, value: str | None) -> None:
    """模拟别人并发改了会话 cwd（只改沙盒里的 state.db）。"""
    conn = sqlite3.connect(str(sb.home / "state.db"))
    try:
        with conn:
            conn.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (value, session_id))
    finally:
        conn.close()


def _project_folder_count(sb: Sandbox, project_id: str) -> int:
    conn = sqlite3.connect(str(sb.home / "projects.db"))
    try:
        row = conn.execute("SELECT COUNT(*) FROM project_folders WHERE project_id = ?",
                           (project_id,)).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _assert_row_equal(testcase: unittest.TestCase, expected: dict, actual: dict | None,
                      label: str) -> None:
    """按 assignments 的 9 列逐字段比较（TASK 要求的「9 列一字未变」）。"""
    testcase.assertIsNotNone(actual, label + "：归属行不见了")
    for column in contract.ASSIGN_COLUMNS:
        testcase.assertEqual(actual.get(column), expected.get(column),
                             "%s 列 %s 不一致" % (label, column))

class TestAutogroupAIIntegration(unittest.TestCase):
    """AI 分类全链路集成验收（每条测试一个独立 Sandbox）。"""

    def test_ten_synthetic_sessions_three_tiers(self):
        """10 条合成会话覆盖三档：计数守恒、每条 status_if_confirmed 与档位一致。"""
        with Sandbox() as sb:
            sb.add_project("memory", "记忆")
            sb.add_project("quick-send", "快传")
            _seed_synth(sb)
            data = _post_preview(
                _item("synth-01", "memory", 0.92),
                _item("synth-02", "quick-send", 0.88),
                _item("synth-03", "memory", 0.81),
                _item("synth-04", "quick-send", 0.65),
                _item("synth-05", "memory", 0.50),
                _item("synth-06", "memory", 0.30),
                _item("synth-07", "quick-send", 0.10),
                _item("synth-08", "memory", 0.79),
                _item("synth-09", "quick-send", 0.85),
                _item("synth-10", "memory", 0.55),
            )
            preview = data["preview"]
            items = preview["items"]
            summary = preview["summary"]
            self.assertEqual(len(items), 10)
            self.assertEqual(summary["total"], 10)
            self.assertEqual(summary["apply"], 4)
            self.assertEqual(summary["suggest"], 4)
            self.assertEqual(summary["unjudged"], 2)
            # 三档计数之和必须等于可处理条数（不是原始输入条数）
            self.assertEqual(summary["apply"] + summary["suggest"] + summary["unjudged"],
                             summary["total"])
            self.assertEqual(summary["writable"], summary["apply"] + summary["suggest"])
            expected = {
                "synth-01": ("apply", "applied"), "synth-02": ("apply", "applied"),
                "synth-03": ("apply", "applied"), "synth-04": ("suggest", "suggested"),
                "synth-05": ("suggest", "suggested"), "synth-06": ("unjudged", None),
                "synth-07": ("unjudged", None), "synth-08": ("suggest", "suggested"),
                "synth-09": ("apply", "applied"), "synth-10": ("suggest", "suggested"),
            }
            for item in items:
                tier, status = expected[item["session_id"]]
                self.assertEqual(item["tier"], tier, item["session_id"])
                self.assertEqual(item["write_policy"]["status_if_confirmed"], status,
                                 item["session_id"])
                self.assertEqual(item["write_policy"]["can_write"], status is not None,
                                 item["session_id"])
                # snapshot/state 由服务端自读：会话必须真实存在且 cwd 与 state.db 一致
                self.assertTrue(item["session_snapshot"]["exists"], item["session_id"])
                self.assertEqual(item["session_snapshot"]["cwd"], sb.cwd_of(item["session_id"]),
                                 item["session_id"])
            # 预览阶段一行归属都不许写
            for session_id in SYNTH_IDS:
                self.assertIsNone(sb.assignment(session_id), session_id)

    def test_three_same_theme_sessions_become_new_project_candidate(self):
        """3 条同主题且不匹配已有项目的会话 → 同一个 new_project 候选，建项目前 can_write=False。"""
        with Sandbox() as sb:
            sb.add_project("memory", "记忆")
            _seed_synth(sb, titles={
                "synth-06": "合成测试数据 同主题：语音备忘批量整理（一）",
                "synth-07": "合成测试数据 同主题：语音备忘批量整理（二）",
                "synth-08": "合成测试数据 同主题：语音备忘批量整理（三）",
            })
            support = ["synth-06", "synth-07", "synth-08"]
            new_project = {"slug": "synth-voice-notes", "name": "合成语音备忘"}
            data = _post_preview(
                _item("synth-06", None, 0.90, new_project=new_project, support=support),
                _item("synth-07", None, 0.72, new_project=new_project, support=support),
                _item("synth-08", None, 0.55, new_project=new_project, support=support),
            )
            items = data["preview"]["items"]
            # 3 条都指向同一个 slug（聚成一个候选，而不是三个项目）
            slugs = {item["suggested"]["new_project"]["slug"] for item in items}
            self.assertEqual(slugs, {"synth-voice-notes"})
            candidate_support: set = set()
            for item in items:
                self.assertIsNone(item["suggested"]["project_slug"])
                support_ids = item["suggested"]["support_session_ids"]
                self.assertGreaterEqual(len(set(support_ids)), 3, item["session_id"])
                self.assertEqual(set(support_ids), set(support), item["session_id"])
                candidate_support.update(support_ids)
                # 项目还没建：即使 0.9 也只能先建项目，不能直接写
                self.assertFalse(item["write_policy"]["can_write"], item["session_id"])
                self.assertIn("先经确认创建项目", item["write_policy"]["why"])
                expected_status = "applied" if item["tier"] == "apply" else "suggested"
                self.assertEqual(item["write_policy"]["status_if_confirmed"], expected_status)
            self.assertEqual(len(candidate_support), 3)
            # 建项目前逐条 selectable=False
            got = body(apply_mod.get_ai_preview(data["batch_id"]))["data"]
            for item in got["items"]:
                self.assertFalse(item["selectable"], item["session_id"])
                self.assertIn("先经确认创建项目", item["selectable_why"])
            # 强行选中也整批不可写
            resp = apply_mod.post_ai_confirm({"batch_id": data["batch_id"], "confirm": True,
                                              "selected": support})
            self.assertEqual(status_of(resp), 400)
            self.assertEqual(body(resp)["error"]["code"], "nothing_selected")
            for session_id in support:
                self.assertIsNone(sb.assignment(session_id))

    def test_protected_existing_assignment_never_changes(self):
        """保护 fixture：确认写入另一条会话后，原有 suggested 归属行 9 列逐字段不变。"""
        with Sandbox() as sb:
            sb.add_project("memory", "记忆")
            sb.add_project("quick-send", "快传")
            _seed_synth(sb, count=3)
            sb.add_assignment("synth-01", "memory", status="suggested", confidence=0.4,
                              reason="rules 命中（受保护）", source="rules", batch_id="legacy-batch")
            protected_before = sb.assignment("synth-01")
            self.assertIsNotNone(protected_before)
            data = _post_preview(
                _item("synth-01", "quick-send", 0.95),   # 引擎也建议了它，但本次不选
                _item("synth-02", "quick-send", 0.90),
            )
            resp = apply_mod.post_ai_confirm({"batch_id": data["batch_id"], "confirm": True,
                                              "selected": ["synth-02"]})
            out = body(resp)
            self.assertEqual(status_of(resp), 200, out)
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["data"]["written"], 1)
            self.assertEqual(out["data"]["status"], "done")
            protected_after = sb.assignment("synth-01")
            _assert_row_equal(self, protected_before, protected_after, "受保护归属行 synth-01")
            self.assertEqual(protected_after["status"], "suggested")
            self.assertEqual(protected_after["project_slug"], "memory")
            self.assertEqual(protected_after["batch_id"], "legacy-batch")
            self.assertEqual(sb.assignment("synth-02")["status"], "applied")

    def test_applied_sessions_are_hard_excluded(self):
        """已 applied 的会话即便出现在引擎输出里，也被服务端剔除进 excluded，原归属不动。"""
        with Sandbox() as sb:
            sb.add_project("memory", "记忆")
            sb.add_project("quick-send", "快传")
            _seed_synth(sb, count=4)
            sb.add_assignment("synth-01", "quick-send", status="applied", confidence=1.0,
                              reason="真实 56 条之一（模拟）", source="rules", batch_id="bg-real-56")
            applied_before = sb.assignment("synth-01")
            data = _post_preview(
                _item("synth-01", "memory", 0.99),   # 引擎即使把它排进来，也必须被硬性排除
                _item("synth-02", "memory", 0.90),
            )
            preview = data["preview"]
            self.assertEqual([item["session_id"] for item in preview["items"]], ["synth-02"])
            self.assertEqual(preview["summary"]["total"], 1)
            self.assertEqual(preview["summary"]["excluded"], 1)
            excluded = preview["excluded"]
            self.assertEqual(len(excluded), 1)
            self.assertEqual(excluded[0]["session_id"], "synth-01")
            self.assertIn("applied", excluded[0]["why"])
            # 尝试把被排除的会话塞进 selected：整批拒绝，另一条也不能写
            resp = apply_mod.post_ai_confirm({"batch_id": data["batch_id"], "confirm": True,
                                              "selected": ["synth-01", "synth-02"]})
            self.assertEqual(status_of(resp), 400)
            self.assertEqual(body(resp)["error"]["code"], "selection_rejected")
            self.assertIsNone(sb.assignment("synth-02"))
            # 只选合法条目才允许写入
            ok = body(apply_mod.post_ai_confirm({"batch_id": data["batch_id"], "confirm": True,
                                                 "selected": ["synth-02"]}))
            self.assertTrue(ok["ok"], ok)
            self.assertEqual(ok["data"]["written"], 1)
            _assert_row_equal(self, applied_before, sb.assignment("synth-01"), "applied 会话 synth-01")

    def test_boundary_confidence_050_079_080_081(self):
        """置信度边界：0.5/0.7999 只写 suggested 且 cwd 不变；0.8 写 applied 并同步 cwd；0.4999 不可选。"""
        with Sandbox() as sb:
            memory_dir = sb.add_project("memory", "记忆")
            _seed_synth(sb, count=4)
            data = _post_preview(
                _item("synth-01", "memory", 0.5),
                _item("synth-02", "memory", 0.7999),
                _item("synth-03", "memory", 0.8),
                _item("synth-04", "memory", 0.4999),
            )
            items = {item["session_id"]: item for item in data["preview"]["items"]}
            self.assertEqual(items["synth-01"]["tier"], "suggest")
            self.assertEqual(items["synth-01"]["write_policy"]["status_if_confirmed"], "suggested")
            self.assertTrue(items["synth-01"]["write_policy"]["can_write"])
            self.assertEqual(items["synth-02"]["tier"], "suggest")
            self.assertEqual(items["synth-02"]["write_policy"]["status_if_confirmed"], "suggested")
            self.assertTrue(items["synth-02"]["write_policy"]["can_write"])
            self.assertEqual(items["synth-03"]["tier"], "apply")
            self.assertEqual(items["synth-03"]["write_policy"]["status_if_confirmed"], "applied")
            self.assertTrue(items["synth-03"]["write_policy"]["can_write"])
            self.assertEqual(items["synth-04"]["tier"], "unjudged")
            self.assertFalse(items["synth-04"]["write_policy"]["can_write"])
            self.assertIsNone(items["synth-04"]["write_policy"]["status_if_confirmed"])
            got = {item["session_id"]: item for item in
                   body(apply_mod.get_ai_preview(data["batch_id"]))["data"]["items"]}
            self.assertFalse(got["synth-04"]["selectable"])
            # 0.4999 不可选：强行选它整批不写
            low = apply_mod.post_ai_confirm({"batch_id": data["batch_id"], "confirm": True,
                                             "selected": ["synth-04"]})
            self.assertEqual(status_of(low), 400)
            self.assertEqual(body(low)["error"]["code"], "nothing_selected")
            before_cwds = {session_id: sb.cwd_of(session_id)
                           for session_id in ("synth-01", "synth-02", "synth-03")}
            resp = apply_mod.post_ai_confirm({"batch_id": data["batch_id"], "confirm": True,
                                              "selected": ["synth-01", "synth-02", "synth-03"]})
            out = body(resp)
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["data"]["written"], 3)
            self.assertEqual(sb.assignment("synth-01")["status"], "suggested")
            self.assertEqual(sb.assignment("synth-02")["status"], "suggested")
            self.assertEqual(sb.assignment("synth-03")["status"], "applied")
            self.assertEqual(sb.cwd_of("synth-01"), before_cwds["synth-01"])
            self.assertEqual(sb.cwd_of("synth-02"), before_cwds["synth-02"])
            self.assertEqual(Path(sb.cwd_of("synth-03")).resolve(), Path(memory_dir).resolve())
            self.assertIsNone(sb.assignment("synth-04"))

    def test_low_confidence_never_written(self):
        """c=0.3 的条目即使塞进 selected，也整批 400 selection_rejected，归属表与 cwd 零变化。"""
        with Sandbox() as sb:
            sb.add_project("memory", "记忆")
            _seed_synth(sb, count=2)
            before_rows = _all_assignments(sb)
            before_cwds = _all_cwds(sb)
            data = _post_preview(
                _item("synth-01", "memory", 0.3),
                _item("synth-02", "memory", 0.9),   # 同批一条可写的，确保是「整批拒绝」而不是空选
            )
            resp = apply_mod.post_ai_confirm({"batch_id": data["batch_id"], "confirm": True,
                                              "selected": ["synth-01", "synth-02"]})
            out = body(resp)
            self.assertEqual(status_of(resp), 400, out)
            self.assertEqual(out["error"]["code"], "selection_rejected")
            # 整批一条都没写：归属表整表快照和 cwd 全量快照都必须一模一样
            self.assertEqual(_all_assignments(sb), before_rows)
            self.assertEqual(_all_cwds(sb), before_cwds)
            self.assertIsNone(sb.assignment("synth-01"))
            self.assertIsNone(sb.assignment("synth-02"))

    def test_no_confirm_means_no_write(self):
        """不带 confirm=true 的确认请求返回 400 confirmation_required，归属与 cwd 全量快照不变。"""
        with Sandbox() as sb:
            sb.add_project("memory", "记忆")
            _seed_synth(sb, count=3)
            before_rows = _all_assignments(sb)
            before_cwds = _all_cwds(sb)
            data = _post_preview(
                _item("synth-01", "memory", 0.9),
                _item("synth-02", "memory", 0.6),
                _item("synth-03", "memory", 0.2),
            )
            resp = apply_mod.post_ai_confirm({"batch_id": data["batch_id"],
                                              "selected": ["synth-01", "synth-02"]})
            out = body(resp)
            self.assertEqual(status_of(resp), 400, out)
            self.assertEqual(out["error"]["code"], "confirmation_required")
            self.assertIn("preview", out["error"])
            self.assertEqual(len(out["error"]["preview"]["items"]), 3)
            self.assertEqual(_all_assignments(sb), before_rows)
            self.assertEqual(_all_cwds(sb), before_cwds)
            self.assertEqual(sb.batch_row(data["batch_id"])["status"], "pending")

    def test_new_project_dir_created_and_writable(self):
        """post_ai_project(confirm=True) 后：真实目录 + projects.db 注册 + selectable + 0.8 写入回读。"""
        with Sandbox() as sb:
            _seed_synth(sb, count=8, titles={
                "synth-06": "合成测试数据 同主题：语音备忘批量整理（一）",
                "synth-07": "合成测试数据 同主题：语音备忘批量整理（二）",
                "synth-08": "合成测试数据 同主题：语音备忘批量整理（三）",
            })
            slug = "synth-voice-notes"
            support = ["synth-06", "synth-07", "synth-08"]
            new_project = {"slug": slug, "name": "合成语音备忘"}
            data = _post_preview(
                _item("synth-06", None, 0.8, new_project=new_project, support=support),
                _item("synth-07", None, 0.7, new_project=new_project, support=support),
                _item("synth-08", None, 0.6, new_project=new_project, support=support),
            )
            before = _by_session(data["preview"]["items"], "synth-06")
            self.assertFalse(before["write_policy"]["can_write"])
            created = body(apply_mod.post_ai_project({"batch_id": data["batch_id"], "slug": slug,
                                                      "confirm": True}))
            self.assertTrue(created["ok"], created)
            target = sb.home / "projects" / slug
            self.assertTrue(target.is_dir(), "新建项目必须是真实目录")
            row = sb.project_row(slug)
            self.assertIsNotNone(row, "projects.db 必须有注册行")
            self.assertEqual(Path(row["primary_path"]).resolve(), target.resolve())
            self.assertEqual(_project_folder_count(sb, row["id"]), 1)
            # 建好之后该条目从此 selectable=True，0.8 档写 applied
            got = {item["session_id"]: item for item in
                   body(apply_mod.get_ai_preview(data["batch_id"]))["data"]["items"]}
            self.assertTrue(got["synth-06"]["selectable"])
            self.assertEqual(got["synth-06"]["write_policy"]["status_if_confirmed"], "applied")
            resp = apply_mod.post_ai_confirm({"batch_id": data["batch_id"], "confirm": True,
                                              "selected": ["synth-06"]})
            out = body(resp)
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["data"]["written"], 1)
            assignment = sb.assignment("synth-06")
            self.assertEqual(assignment["project_slug"], slug)
            self.assertEqual(assignment["status"], "applied")
            self.assertEqual(Path(sb.cwd_of("synth-06")).resolve(), target.resolve())
            readback = body(apply_mod.get_ai_readback(data["batch_id"]))["data"]
            self.assertEqual(readback["bad"], [])
            item = _by_session(readback["items"], "synth-06")
            self.assertEqual(item["outcome"], "written")
            self.assertEqual(Path(item["now"]["cwd"]).resolve(), target.resolve())

    def test_cancel_then_new_project_condition_still_holds(self):
        """先不带 confirm 调 post_ai_project（被拒），再带 confirm 仍然成功，支持会话数仍 >=3。"""
        with Sandbox() as sb:
            _seed_synth(sb, count=8, titles={
                "synth-06": "合成测试数据 同主题：语音备忘批量整理（一）",
                "synth-07": "合成测试数据 同主题：语音备忘批量整理（二）",
                "synth-08": "合成测试数据 同主题：语音备忘批量整理（三）",
            })
            slug = "synth-voice-notes"
            support = ["synth-06", "synth-07", "synth-08"]
            new_project = {"slug": slug, "name": "合成语音备忘"}
            data = _post_preview(
                _item("synth-06", None, 0.9, new_project=new_project, support=support),
                _item("synth-07", None, 0.8, new_project=new_project, support=support),
                _item("synth-08", None, 0.7, new_project=new_project, support=support),
            )
            first_resp = apply_mod.post_ai_project({"batch_id": data["batch_id"], "slug": slug})
            first = body(first_resp)
            self.assertEqual(status_of(first_resp), 400, first)
            self.assertEqual(first["error"]["code"], "confirmation_required")
            self.assertGreaterEqual(first["error"]["check"]["support_count"], 3)
            self.assertIsNone(sb.project_row(slug))
            self.assertFalse((sb.home / "projects" / slug).exists())
            second = body(apply_mod.post_ai_project({"batch_id": data["batch_id"], "slug": slug,
                                                     "confirm": True}))
            self.assertTrue(second["ok"], second)
            self.assertGreaterEqual(second["data"]["support_count"], 3)
            self.assertGreaterEqual(second["data"]["selected_count"], 1)
            self.assertIsNotNone(sb.project_row(slug))
            self.assertTrue((sb.home / "projects" / slug).is_dir())

    def test_duplicate_confirm_is_idempotent(self):
        """同一 batch_id 第二次确认 409 batch_consumed，归属行 updated_at/batch_id 不被二次改写。"""
        with Sandbox() as sb:
            sb.add_project("memory", "记忆")
            _seed_synth(sb, count=2)
            data = _post_preview(_item("synth-01", "memory", 0.9),
                                 _item("synth-02", "memory", 0.6))
            first = body(apply_mod.post_ai_confirm({"batch_id": data["batch_id"], "confirm": True,
                                                    "selected": ["synth-01", "synth-02"]}))
            self.assertTrue(first["ok"], first)
            self.assertEqual(first["data"]["written"], 2)
            rows_after_first = {session_id: sb.assignment(session_id)
                                for session_id in ("synth-01", "synth-02")}
            second_resp = apply_mod.post_ai_confirm({"batch_id": data["batch_id"], "confirm": True,
                                                     "selected": ["synth-01", "synth-02"]})
            second = body(second_resp)
            self.assertEqual(status_of(second_resp), 409, second)
            self.assertEqual(second["error"]["code"], "batch_consumed")
            self.assertEqual(sb.batch_row(data["batch_id"])["status"], "done")
            for session_id in ("synth-01", "synth-02"):
                _assert_row_equal(self, rows_after_first[session_id],
                                  sb.assignment(session_id), "第二次确认后的 " + session_id)

    def test_ownership_changed_after_preview_pauses_batch(self):
        """预览后归属行被手改：第一条跳过、整批 paused、not_executed，第一条改动不被覆盖。"""
        with Sandbox() as sb:
            sb.add_project("memory", "记忆")
            sb.add_project("quick-send", "快传")
            _seed_synth(sb, count=2)
            data = _post_preview(_item("synth-01", "memory", 0.9),
                                 _item("synth-02", "memory", 0.9))
            # 模拟并发/别人手改：预览时是 absent，确认前变成 quick-send suggested
            sb.add_assignment("synth-01", "quick-send", status="suggested", confidence=0.77,
                              reason="别人改过（并发）", source="panel")
            manual = sb.assignment("synth-01")
            resp = apply_mod.post_ai_confirm({"batch_id": data["batch_id"], "confirm": True,
                                              "selected": ["synth-01", "synth-02"]})
            out = body(resp)
            self.assertEqual(status_of(resp), 409, out)
            self.assertEqual(out["error"]["code"], "batch_paused")
            result = out["error"]["result"]
            self.assertEqual(result["status"], "paused")
            self.assertEqual(result["written"], 0)
            self.assertEqual(result["results"][0]["session_id"], "synth-01")
            self.assertEqual(result["results"][0]["outcome"], "skipped")
            self.assertIn("预览快照与现状不一致", result["results"][0]["why"])
            self.assertEqual([row["session_id"] for row in result["not_executed"]], ["synth-02"])
            _assert_row_equal(self, manual, sb.assignment("synth-01"), "人工改动后的归属")
            self.assertIsNone(sb.assignment("synth-02"))
            self.assertEqual(sb.batch_row(data["batch_id"])["status"], "paused")

    def test_project_dir_removed_pauses_batch(self):
        """预览后目标项目目录被删：确认时整批 409 batch_paused，一条都不写。"""
        with Sandbox() as sb:
            memory_dir = sb.add_project("memory", "记忆")
            _seed_synth(sb, count=2)
            data = _post_preview(_item("synth-01", "memory", 0.9),
                                 _item("synth-02", "memory", 0.9))
            shutil.rmtree(memory_dir)
            self.assertFalse(memory_dir.exists())
            resp = apply_mod.post_ai_confirm({"batch_id": data["batch_id"], "confirm": True,
                                              "selected": ["synth-01", "synth-02"]})
            out = body(resp)
            self.assertEqual(status_of(resp), 409, out)
            self.assertEqual(out["error"]["code"], "batch_paused")
            result = out["error"]["result"]
            self.assertEqual(result["status"], "paused")
            self.assertEqual(result["written"], 0)
            self.assertEqual(result["results"][0]["session_id"], "synth-01")
            self.assertEqual(result["results"][0]["outcome"], "skipped")
            self.assertIn("项目目录不是真实目录", result["results"][0]["why"])
            self.assertIsNone(sb.assignment("synth-01"))
            self.assertIsNone(sb.assignment("synth-02"))
            self.assertEqual(sb.cwd_of("synth-01"), "D:/synth/01")
            self.assertEqual(sb.batch_row(data["batch_id"])["status"], "paused")

    def test_cwd_changed_concurrently_pauses_batch(self):
        """预览后某条会话 cwd 被改：该条跳过并暂停整批，且 cwd 保持别人的新值。"""
        with Sandbox() as sb:
            sb.add_project("memory", "记忆")
            _seed_synth(sb, count=2)
            data = _post_preview(_item("synth-01", "memory", 0.9),
                                 _item("synth-02", "memory", 0.9))
            _set_cwd(sb, "synth-01", "D:/somewhere/别人的新值")
            resp = apply_mod.post_ai_confirm({"batch_id": data["batch_id"], "confirm": True,
                                              "selected": ["synth-01", "synth-02"]})
            out = body(resp)
            self.assertEqual(status_of(resp), 409, out)
            self.assertEqual(out["error"]["code"], "batch_paused")
            result = out["error"]["result"]
            self.assertEqual(result["status"], "paused")
            self.assertEqual(result["written"], 0)
            self.assertEqual(result["results"][0]["session_id"], "synth-01")
            self.assertEqual(result["results"][0]["outcome"], "skipped")
            self.assertIn("会话 cwd 已变化", result["results"][0]["why"])
            # 关键：不能把别人的新 cwd 覆盖回预览时的旧值
            self.assertEqual(sb.cwd_of("synth-01"), "D:/somewhere/别人的新值")
            self.assertEqual(sb.cwd_of("synth-02"), "D:/synth/02")
            self.assertIsNone(sb.assignment("synth-01"))
            self.assertIsNone(sb.assignment("synth-02"))

    def test_full_batch_undo_restores_every_field(self):
        """4 条整批撤销：2 条原本不存在、1 条 suggested 到别项目、1 条 suggested 同项目；新建项目被清理。"""
        with Sandbox() as sb:
            sb.add_project("memory", "记忆")
            sb.add_project("quick-send", "快传")
            _seed_synth(sb, count=4)
            _set_cwd(sb, "synth-01", None)   # 原本 cwd=NULL，撤销必须回到 NULL
            sb.add_assignment("synth-03", "quick-send", status="suggested", confidence=0.42,
                              reason="原有同项目建议", source="rules", batch_id="legacy-3")
            sb.add_assignment("synth-04", "memory", status="suggested", confidence=0.35,
                              reason="原有别项目建议", source="rules", batch_id="legacy-4")
            ids = ["synth-01", "synth-02", "synth-03", "synth-04"]
            # 写入前先存 SELECT * 整行 dict（撤销后要逐列还原到这个值）
            original_rows = {session_id: sb.assignment(session_id) for session_id in ids}
            original_cwds = {session_id: sb.cwd_of(session_id) for session_id in ids}
            self.assertIsNone(original_rows["synth-01"])
            self.assertIsNone(original_rows["synth-02"])
            self.assertIsNotNone(original_rows["synth-03"])
            self.assertIsNotNone(original_rows["synth-04"])
            self.assertIsNone(original_cwds["synth-01"])
            new_slug = "synth-batch-project"
            data = _post_preview(
                _item("synth-01", "memory", 0.9),
                _item("synth-02", "quick-send", 0.9),
                _item("synth-03", "quick-send", 0.9),
                _item("synth-04", None, 0.9,
                      new_project={"slug": new_slug, "name": "合成批次项目"}, support=ids),
            )
            created = body(apply_mod.post_ai_project({"batch_id": data["batch_id"],
                                                      "slug": new_slug, "confirm": True}))
            self.assertTrue(created["ok"], created)
            target = sb.home / "projects" / new_slug
            self.assertTrue(target.is_dir())
            confirm = body(apply_mod.post_ai_confirm({"batch_id": data["batch_id"], "confirm": True,
                                                      "selected": ids}))
            self.assertTrue(confirm["ok"], confirm)
            self.assertEqual(confirm["data"]["written"], 4)
            self.assertEqual(confirm["data"]["status"], "done")
            self.assertEqual(sb.assignment("synth-01")["project_slug"], "memory")
            self.assertEqual(sb.assignment("synth-02")["project_slug"], "quick-send")
            self.assertEqual(sb.assignment("synth-03")["project_slug"], "quick-send")
            self.assertEqual(sb.assignment("synth-04")["project_slug"], new_slug)
            self.assertEqual(Path(sb.cwd_of("synth-01")).resolve(),
                             (sb.home / "projects" / "memory").resolve())
            self.assertEqual(Path(sb.cwd_of("synth-04")).resolve(), target.resolve())
            out = body(undo_mod.post_ai_undo({"batch_id": data["batch_id"], "confirm": True}))
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["data"]["status"], "undone")
            self.assertEqual(len(out["data"]["restored"]), 4)
            self.assertEqual(out["data"]["readback"]["mismatched"], [])
            self.assertEqual(out["data"]["readback"]["cwd_mismatched"], [])
            for session_id in ids:
                current = sb.assignment(session_id)
                if original_rows[session_id] is None:
                    self.assertIsNone(current, "%s 原本不存在，撤销后必须不存在" % session_id)
                else:
                    _assert_row_equal(self, original_rows[session_id], current,
                                      "撤销还原 " + session_id)
                self.assertEqual(sb.cwd_of(session_id), original_cwds[session_id],
                                 "%s 撤销后 cwd 应回到写入前的值" % session_id)
            cleanup = {row["slug"]: row for row in out["data"]["project_cleanup"]}
            self.assertIn(new_slug, cleanup)
            self.assertTrue(cleanup[new_slug]["cleaned"], cleanup[new_slug])
            self.assertIsNone(sb.project_row(new_slug))
            self.assertFalse(target.exists())

    def test_undo_conflict_does_not_overwrite_later_change(self):
        """写入后手工把某条改成另一个项目：撤销 409 undo_paused，手工值原样保留。"""
        with Sandbox() as sb:
            sb.add_project("memory", "记忆")
            sb.add_project("quick-send", "快传")
            _seed_synth(sb, count=2)
            data = _post_preview(_item("synth-01", "memory", 0.9),
                                 _item("synth-02", "memory", 0.9))
            confirm = body(apply_mod.post_ai_confirm({"batch_id": data["batch_id"], "confirm": True,
                                                      "selected": ["synth-01", "synth-02"]}))
            self.assertTrue(confirm["ok"], confirm)
            # 写入后别人又手工改成了另一个项目
            sb.add_assignment("synth-01", "quick-send", status="applied", confidence=0.95,
                              reason="人工改成另一个项目", source="panel")
            manual = sb.assignment("synth-01")
            resp = undo_mod.post_ai_undo({"batch_id": data["batch_id"], "confirm": True})
            out = body(resp)
            self.assertEqual(status_of(resp), 409, out)
            self.assertEqual(out["error"]["code"], "undo_paused")
            self.assertEqual(out["error"]["result"]["paused"]["session_id"], "synth-01")
            _assert_row_equal(self, manual, sb.assignment("synth-01"), "撤销冲突后的人工值")
            self.assertEqual(sb.assignment("synth-01")["project_slug"], "quick-send")
            self.assertEqual(sb.assignment("synth-01")["reason"], "人工改成另一个项目")
            # 整批暂停：后面的条目也不能被悄悄还原
            self.assertEqual(sb.assignment("synth-02")["status"], "applied")


if __name__ == "__main__":
    unittest.main(verbosity=2)
# -*- coding: utf-8 -*-
"""AI 分类跑批接口的真实测试（t_364da8f0）：scope / classify job / 分批续跑 / 预览可写性。

跑法（``dashboard`` 目录下）::

    <venv>/python.exe -m unittest tests.test_autogroup_ai_run

全部在**沙盒临时 home** 里跑（``tests/ai_sandbox.py``）：真 home 一个字节都不动。
模型调用用确定性 stub（``autogroup_ai.default_caller`` 被替换），不花额度、不连网。
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

DASH = Path(__file__).resolve().parents[1]
if str(DASH) not in sys.path:
    sys.path.insert(0, str(DASH))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import ai_sandbox  # noqa: E402
import api_autogroup_ai_apply as apply_mod  # noqa: E402
import api_autogroup_ai_run as run  # noqa: E402
import api_autogroup_ai_undo as undo_mod  # noqa: E402
import autogroup_ai as engine  # noqa: E402

body = ai_sandbox.body
status_of = ai_sandbox.status_of


def _verdict(slug, confidence, reason, new_project=None, support=None):
    return json.dumps({"project_slug": slug, "confidence": confidence, "reason": reason,
                       "new_project": new_project, "support_session_ids": support or []},
                      ensure_ascii=False)


class StubCaller:
    """确定性模型：按会话标题里的标记返回预设 JSON（同一批里每次调用一次）。"""

    def __init__(self, by_token):
        self.by_token = dict(by_token)
        self.calls = []

    def __call__(self, messages, **kwargs):
        text = json.dumps(messages, ensure_ascii=False)
        self.calls.append(text)
        for token, value in self.by_token.items():
            if token in text:
                if isinstance(value, Exception):
                    raise value
                return {"text": value, "model": "stub-model",
                        "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18}}
        return {"text": _verdict("memory", 0.5, "兜底"), "model": "stub-model"}


class RunCase(unittest.TestCase):
    """公共夹具：4 条候选会话（K1..K4）+ 1 条 applied（protected-0）+ 2 个项目。"""

    TOKENS = ("K1", "K2", "K3", "K4")

    def setUp(self):
        self.sb = ai_sandbox.Sandbox(applied_fixture=1)
        self.sb.__enter__()
        self.addCleanup(self._teardown)
        self.sb.add_project("memory", "记忆系统")
        self.sb.add_project("infra", "基础设施")
        self.sb.add_session("k1", "K1 记忆系统重构", cwd=r"D:\work\k1")
        self.sb.add_session("k2", "K2 记忆系统向量检索", cwd=r"D:\work\k2")
        self.sb.add_session("k3", "K3 k8s 集群升级", cwd=r"D:\work\k3")
        self.sb.add_session("k4", "K4 说不清的会话", cwd=r"D:\work\k4")
        # protected-0 是 applied（硬性排除）
        self.sb.add_session("r1", "R1 已拒绝的会话", cwd=r"D:\work\r1")
        for session_id, text in (("k1", "记忆系统的表结构要重构，cwd 也不对"),
                                 ("k2", "记忆系统的向量检索召回率怎么调"),
                                 ("k3", "k8s 集群从 1.28 升到 1.30"),
                                 ("k4", "嗯"),
                                 ("r1", "这条会话已被人工拒绝")):
            self.add_messages(session_id, text)
        self._original_caller = engine.default_caller
        self.set_caller({})

    def add_messages(self, session_id, text):
        """沙盒的 sessions 表没有 messages：引擎要读「前 3 条用户消息」当摘要，这里补上。"""
        import sqlite3

        conn = sqlite3.connect(str(self.sb.home / "state.db"))
        try:
            with conn:
                conn.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                             " session_id TEXT, role TEXT, content TEXT, created_at REAL)")
                conn.execute("INSERT INTO messages(session_id, role, content) VALUES(?, 'user', ?)",
                             (session_id, text))
        finally:
            conn.close()

    def _teardown(self):
        engine.default_caller = self._original_caller
        self.sb.__exit__(None, None, None)

    def set_caller(self, by_token):
        stub = StubCaller(by_token)
        engine.default_caller = lambda: stub  # type: ignore[assignment]
        self.stub = stub
        return stub

    def default_caller_map(self):
        return {
            "K1": _verdict("memory", 0.93, "标题与正文都是记忆系统重构"),
            "K2": _verdict("memory", 0.62, "向量检索，中等置信度"),
            "K3": _verdict(None, 0.9, "看起来是新的基础设施话题",
                           {"slug": "k8s-ops", "name": "K8s 运维"}, ["k3"]),
            "K4": _verdict("infra", 0.21, "信息太少，说不清"),
        }

    def run_job(self, payload, timeout=20.0):
        """POST /autogroup/ai/classify → 轮询 job 直到 done/failed，返回 job dict。"""
        resp = run.post_ai_classify(payload)
        code = status_of(resp)
        start = body(resp)
        self.assertEqual(code, 202, start)
        job_id = start.get("job_id")
        self.assertTrue(job_id, start)
        deadline = time.time() + timeout
        while time.time() < deadline:
            poll = body(run.get_ai_classify_job(job_id))
            state = poll.get("state")
            if state in ("done", "failed"):
                return poll.get("data") or {}
            time.sleep(0.05)
        self.fail("job 没在 %.1fs 内结束：%s" % (timeout, job_id))

    def rows_of(self, job):
        return {row["session_id"]: row for row in (job.get("result") or {}).get("rows") or []}

    def snapshot(self):
        """三个库 + 目录清单的指纹：用来证明跑批/预览零写入。"""
        out = {}
        for name in ("state.db", "projects.db", "auto-group/assignments.db"):
            path = self.sb.home / name
            out[name] = path.read_bytes() if path.is_file() else b""
        out["projects_dir"] = sorted(p.name for p in (self.sb.home / "projects").glob("*"))
        return out


class ScopeTest(RunCase):
    def test_scope_counts_and_zero_writes(self):
        before = self.snapshot()
        payload = body(run.get_ai_scope())
        self.assertTrue(payload["ok"], payload)
        data = payload["data"]
        self.assertEqual(data["total_available"], 5, data)   # k1..k4 + r1（rejected 未登记则也算）
        self.assertEqual(data["excluded_applied"], 1)
        self.assertEqual(data["batch_max"], 200)
        self.assertTrue(data["batch_default"] >= 1)
        self.assertNotIn("candidate_session_ids", data)
        self.assertEqual(before, self.snapshot(), "scope 不许写任何库")
        self.assertEqual(self.stub.calls, [], "scope 不许调模型")

    def test_scope_notes_name_normal_exclusions(self):
        payload = body(run.get_ai_scope())
        notes = " ".join(payload.get("notes") or [])
        self.assertIn("正常排除", notes)
        self.assertIn("limit", notes)


class ClassifyContractTest(RunCase):
    """limit 必须显式传；分批/续跑不重复处理同一会话。"""

    def test_limit_is_required(self):
        resp = run.post_ai_classify({})
        self.assertEqual(status_of(resp), 400)
        payload = body(resp)
        self.assertEqual(payload["error"]["code"], "limit_required")
        self.assertIn("40", payload["error"]["message"])
        self.assertEqual(self.stub.calls, [])

    def test_limit_out_of_range(self):
        for bad in (201, -1, "x"):
            resp = run.post_ai_classify({"limit": bad})
            self.assertEqual(status_of(resp), 400, bad)
            self.assertEqual(body(resp)["error"]["code"], "invalid_limit", bad)

    def test_full_run_reports_total_available_from_engine(self):
        self.set_caller(self.default_caller_map())
        job = self.run_job({"limit": 0})
        self.assertEqual(job["state"], "done", job)
        progress = job["result"]["progress"]
        self.assertEqual(progress["total_available"], 5, progress)
        self.assertEqual(progress["processed"], 5, progress)
        self.assertFalse(progress["truncated"], progress)
        self.assertEqual(progress["remaining"], 0)
        self.assertEqual(job["progress"]["done"], 5)
        self.assertTrue(job["result"]["batch_id"].startswith("aip-"))

    def test_truncated_run_reports_omitted(self):
        self.set_caller(self.default_caller_map())
        job = self.run_job({"limit": 2})
        progress = job["result"]["progress"]
        self.assertEqual(progress["total_available"], 5)
        self.assertEqual(progress["processed"], 2)
        self.assertTrue(progress["truncated"])
        self.assertEqual(progress["omitted"], 3)
        self.assertEqual(progress["remaining"], 3)
        self.assertEqual(len(job["result"]["rows"]), 2)

    def test_skip_ids_prevent_reprocessing(self):
        self.set_caller(self.default_caller_map())
        first = self.run_job({"limit": 2})
        first_ids = [row["session_id"] for row in first["result"]["rows"]]
        self.assertEqual(len(first_ids), 2)
        second = self.run_job({"limit": 2, "skip_ids": first_ids})
        second_ids = [row["session_id"] for row in second["result"]["rows"]]
        self.assertEqual(set(first_ids) & set(second_ids), set(), (first_ids, second_ids))
        self.assertEqual(second["result"]["progress"]["processed_before"], 2)
        third = self.run_job({"limit": 2, "skip_ids": first_ids + second_ids})
        self.assertEqual(third["result"]["progress"]["processed_now"], 1, third["result"]["progress"])
        self.assertEqual(third["result"]["progress"]["processed"], 5, third["result"]["progress"])
        self.assertEqual(third["result"]["progress"]["remaining"], 0, third["result"]["progress"])

    def test_offset_paging_is_disjoint(self):
        self.set_caller(self.default_caller_map())
        first = self.run_job({"limit": 2})
        first_ids = [row["session_id"] for row in first["result"]["rows"]]
        second = self.run_job({"limit": 2, "offset": 2})
        second_ids = [row["session_id"] for row in second["result"]["rows"]]
        self.assertEqual(set(first_ids) & set(second_ids), set(), (first_ids, second_ids))

    def test_classify_writes_nothing_but_preview_tables(self):
        self.set_caller(self.default_caller_map())
        before = self.snapshot()
        self.run_job({"limit": 0})
        after = self.snapshot()
        self.assertEqual(before["state.db"], after["state.db"], "跑批不许改 cwd/会话")
        self.assertEqual(before["projects.db"], after["projects.db"], "跑批不许建项目")
        self.assertEqual(before["projects_dir"], after["projects_dir"], "跑批不许建目录")
        self.assertIsNone(self.sb.assignment("k1"), "跑批不许写归属表")


class RowViewTest(RunCase):
    """逐条展示所需字段：标题 / 当前归属 / 建议 / 置信度 / 理由 / 新建 / 证据 / 分档与确认后状态。"""

    def setUp(self):
        super().setUp()
        self.set_caller(self.default_caller_map())
        self.job = self.run_job({"limit": 0})
        self.rows = self.rows_of(self.job)

    def test_row_fields_present(self):
        row = self.rows["k1"]
        self.assertEqual(row["title"], "K1 记忆系统重构")
        self.assertEqual(row["current"], {"slug": None, "status": None, "exists": False})
        self.assertEqual(row["suggested"]["slug"], "memory")
        self.assertEqual(row["suggested"]["name"], "记忆系统")
        self.assertAlmostEqual(row["suggested"]["confidence"], 0.93)
        self.assertIn("记忆系统", row["suggested"]["reason"])
        self.assertEqual(row["tier"], "apply")
        self.assertEqual(row["status_if_confirmed"], "applied")
        self.assertTrue(row["sync_cwd_if_confirmed"])
        self.assertTrue(row["selectable"], row)
        self.assertIsNone(row["new_project"])
        self.assertTrue(row["evidence"]["messages"], "证据不能空")

    def test_suggest_tier_does_not_sync_cwd(self):
        row = self.rows["k2"]
        self.assertEqual(row["tier"], "suggest")
        self.assertEqual(row["status_if_confirmed"], "suggested")
        self.assertFalse(row["sync_cwd_if_confirmed"])
        self.assertTrue(row["selectable"])

    def test_unjudged_row_is_not_selectable_and_has_reason(self):
        row = self.rows["k4"]
        self.assertEqual(row["tier"], "unjudged")
        self.assertFalse(row["selectable"])
        self.assertTrue(row["selectable_why"])
        self.assertTrue(row["unjudged_reason"])
        self.assertEqual(row["undecided_code"], "low_confidence")

    def test_insufficient_new_project_is_downgraded_with_reason(self):
        row = self.rows["k3"]
        self.assertEqual(row["new_project"]["slug"], "k8s-ops")
        self.assertFalse(row["new_project"]["candidate"])
        self.assertFalse(row["selectable"])
        self.assertIn("降级", row["selectable_why"] + (row["unjudged_reason"] or ""))

    def test_failed_call_is_listed_not_dropped(self):
        self.set_caller({"K1": RuntimeError("provider 500")})
        job = self.run_job({"limit": 0})
        rows = self.rows_of(job)
        only = rows["k1"]
        self.assertEqual(only["tier"], "unjudged")
        self.assertEqual(only["undecided_code"], "call_failed")
        self.assertIn("provider 500", only["unjudged_reason"])
        self.assertFalse(only["selectable"], "调用失败的行不许出现在可写集合里")
        self.assertEqual(len(rows), 5, "失败的行也要出现在清单里，不许静默丢弃")


class NewProjectTest(RunCase):
    """>=3 条同主题支撑才是候选；建项目是独立的一次确认。"""

    def setUp(self):
        super().setUp()
        self.sb.add_session("k5", "K5 k8s 证书轮换", cwd=r"D:\work\k5")
        self.sb.add_session("k6", "K6 k8s 节点扩容", cwd=r"D:\work\k6")
        self.add_messages("k5", "k8s 证书轮换流程要写下来")
        self.add_messages("k6", "k8s 节点扩容后 pod 调度不均")
        self.set_caller({
            "K3": _verdict(None, 0.9, "新的 k8s 运维话题", {"slug": "k8s-ops", "name": "K8s 运维"}),
            "K5": _verdict(None, 0.88, "k8s 证书轮换", {"slug": "k8s-ops", "name": "K8s 运维"}),
            "K6": _verdict(None, 0.91, "k8s 节点扩容", {"slug": "k8s-ops", "name": "K8s 运维"}),
        })
        self.job = self.run_job({"limit": 0})
        self.rows = self.rows_of(self.job)

    def test_candidate_needs_project_created_first(self):
        row = self.rows["k3"]
        self.assertTrue(row["new_project"]["candidate"], row)
        self.assertFalse(row["target_exists"])
        self.assertFalse(row["selectable"])
        self.assertIn("项目尚不存在", row["selectable_why"])

    def test_create_project_then_selectable(self):
        batch_id = self.job["result"]["batch_id"]
        preview = body(apply_mod.get_ai_preview(batch_id))
        check = body(apply_mod.post_ai_project({"batch_id": batch_id, "slug": "k8s-ops"}))
        self.assertFalse(check["ok"], "没 confirm=true 不许建项目")
        self.assertEqual(check["error"]["code"], "confirmation_required")
        self.assertIsNone(self.sb.project_row("k8s-ops"))
        created = body(apply_mod.post_ai_project({"batch_id": batch_id, "slug": "k8s-ops",
                                                 "confirm": True}))
        self.assertTrue(created["ok"], created)
        self.assertIsNotNone(self.sb.project_row("k8s-ops"))
        after = body(apply_mod.get_ai_preview(batch_id))["data"]
        rows = {row["session_id"]: row for row in after["items"]}
        self.assertTrue(rows["k3"]["selectable"], rows["k3"])
        self.assertTrue(preview["ok"])


class ConfirmFlowTest(RunCase):
    """确认前零写入 / 逐条取消 / applied 与 suggested 区分 / 回读 / 撤销恢复。"""

    def setUp(self):
        super().setUp()
        self.set_caller(self.default_caller_map())
        self.job = self.run_job({"limit": 0})
        self.batch_id = self.job["result"]["batch_id"]
        self.memory_dir = str(self.sb.home / "projects" / "memory")

    def test_confirm_without_flag_writes_nothing(self):
        resp = apply_mod.post_ai_confirm({"batch_id": self.batch_id, "selected": ["k1"]})
        self.assertEqual(status_of(resp), 400)
        self.assertEqual(body(resp)["error"]["code"], "confirmation_required")
        self.assertIsNone(self.sb.assignment("k1"))
        self.assertEqual(self.sb.cwd_of("k1"), r"D:\work\k1")

    def test_unjudged_selection_is_rejected(self):
        resp = apply_mod.post_ai_confirm({"batch_id": self.batch_id, "confirm": True,
                                          "selected": ["k4"]})
        self.assertIn(body(resp)["error"]["code"], ("selection_rejected", "nothing_selected"))
        self.assertIsNone(self.sb.assignment("k4"))

    def test_cancelled_row_is_not_written(self):
        # 用户在界面上把 k2 取消了：selected 里没有它
        result = body(apply_mod.post_ai_confirm({"batch_id": self.batch_id, "confirm": True,
                                                 "selected": ["k1"]}))["data"]
        self.assertEqual(result["written"], 1)
        self.assertIsNone(self.sb.assignment("k2"), "取消掉的那条一条都不许写")
        self.assertEqual(self.sb.cwd_of("k2"), r"D:\work\k2")

    def test_apply_and_suggest_are_distinguished(self):
        result = body(apply_mod.post_ai_confirm({"batch_id": self.batch_id, "confirm": True,
                                                 "selected": ["k1", "k2"]}))["data"]
        self.assertEqual(result["written"], 2, result)
        applied = self.sb.assignment("k1")
        suggested = self.sb.assignment("k2")
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["project_slug"], "memory")
        self.assertEqual(self.sb.cwd_of("k1"), self.memory_dir, ">=0.8 要同步 cwd")
        self.assertEqual(suggested["status"], "suggested")
        self.assertEqual(self.sb.cwd_of("k2"), r"D:\work\k2", "0.5–<0.8 不许动 cwd")
        readback = body(apply_mod.get_ai_readback(self.batch_id))["data"]
        self.assertEqual(readback["bad"], [], readback)
        outcomes = {row["session_id"]: row["outcome"] for row in readback["items"]}
        self.assertEqual(outcomes["k1"], "written")
        self.assertEqual(outcomes["k2"], "written")

    def test_second_confirm_is_refused(self):
        body(apply_mod.post_ai_confirm({"batch_id": self.batch_id, "confirm": True,
                                        "selected": ["k1"]}))
        again = apply_mod.post_ai_confirm({"batch_id": self.batch_id, "confirm": True,
                                           "selected": ["k2"]})
        self.assertEqual(status_of(again), 409)
        self.assertEqual(body(again)["error"]["code"], "batch_consumed")
        self.assertIsNone(self.sb.assignment("k2"), "防重复提交：第二次一条都不许写")

    def test_undo_restores_row_and_cwd(self):
        body(apply_mod.post_ai_confirm({"batch_id": self.batch_id, "confirm": True,
                                        "selected": ["k1", "k2"]}))
        self.assertIsNotNone(self.sb.assignment("k1"))
        undone = body(undo_mod.post_ai_undo({"batch_id": self.batch_id, "confirm": True}))
        self.assertTrue(undone["ok"], undone)
        data = undone["data"]
        self.assertEqual(data["status"], "undone")
        self.assertEqual(len(data["restored"]), 2, data)
        self.assertEqual(data["readback"]["mismatched"], [], data["readback"])
        self.assertEqual(data["readback"]["cwd_mismatched"], [], data["readback"])
        self.assertIsNone(self.sb.assignment("k1"), "原本不存在的归属行要恢复成不存在")
        self.assertIsNone(self.sb.assignment("k2"))
        self.assertEqual(self.sb.cwd_of("k1"), r"D:\work\k1", "cwd 要回滚")
        again = undo_mod.post_ai_undo({"batch_id": self.batch_id, "confirm": True})
        self.assertEqual(status_of(again), 409, "撤销不能重复执行")




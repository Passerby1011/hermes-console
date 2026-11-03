# -*- coding: utf-8 -*-
"""t_53b48743 D 段补强：**所有**写入口的看板硬约束。

覆盖三个此前会漏的写入口：

* 老接口 ``POST /autogroup/apply``（core.apply_assignments 路径）
* 自动模式 ``autogroup_auto.plan_tick`` / ``run_tick``
* AI 分类写入 ``api_autogroup_ai_apply._item_blocker``

断言口径：看板任务会话（``sessions.source='kanban'``）不得进入普通项目；
拒绝时**零写入**（归属表、cwd、台账都不许动）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DASH = Path(__file__).resolve().parents[1]
for extra in (str(DASH), str(Path(__file__).resolve().parent)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import unittest  # noqa: E402

from project_sandbox import ProjectSandbox, body, status_of  # noqa: E402

import api_autogroup as base  # noqa: E402
import api_autogroup_ai_apply as aiapply  # noqa: E402
import autogroup_auto as auto  # noqa: E402
import autogroup_align as align  # noqa: E402


class LegacyApplyKanbanTest(unittest.TestCase):
    """老 ``/autogroup/apply``：core 那条路以前不看看板来源，现在必须 400 拒绝。"""

    def test_apply_rejects_kanban_batch(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            sb.add_session("kb1", "看板任务", cwd=str(sb.root), source="kanban")
            sb.add_assignment("kb1", "alpha", status="suggested")
            before = sb.snapshot()
            resp = base.post_apply({"confirm": True, "session_ids": ["kb1"], "sync_cwd": True})
            self.assertEqual(status_of(resp), 400)
            payload = body(resp)
            self.assertEqual(payload["error"]["code"], "kanban_excluded")
            self.assertEqual(payload["error"]["blocked_session_ids"], ["kb1"])
            self.assertEqual(before, sb.snapshot(), "拒绝时零写入")
            self.assertEqual(sb.assignment("kb1")["status"], "suggested", "状态不许被改成 applied")
            self.assertEqual(sb.cwd_of("kb1"), str(sb.root), "cwd 不许被同步")

    def test_apply_preview_marks_kanban(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            sb.add_session("kb1", "看板任务", cwd=str(sb.root), source="kanban")
            sb.add_assignment("kb1", "alpha", status="suggested")
            resp = base.post_apply({"confirm": False, "session_ids": ["kb1"]})
            payload = body(resp)
            self.assertEqual(status_of(resp), 400)
            self.assertEqual(payload["error"]["code"], "confirmation_required")
            preview = payload["error"]["preview"]
            self.assertEqual(preview["kanban"], ["kb1"])
            self.assertTrue(preview["items"][0]["kanban"])
            self.assertEqual(preview["items"][0]["source"], "kanban")

    def test_apply_still_works_for_ordinary_session(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            sb.add_session("ok1", "普通会话", cwd=str(sb.root), source="desktop")
            sb.add_assignment("ok1", "alpha", status="suggested")
            resp = body(base.post_apply({"confirm": True, "session_ids": ["ok1"], "sync_cwd": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(sb.assignment("ok1")["status"], "applied")
            self.assertEqual(sb.cwd_of("ok1"), str(a), "写归属必须同时写 cwd")
            self.assertEqual(align.audit(sb.home)["counts"]["inconsistent"], 0)


class AutoModeKanbanTest(unittest.TestCase):
    """自动模式：看板会话不得进候选；写 applied 必须同时写 cwd。"""

    def test_plan_tick_excludes_kanban(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            sb.add_session("kb1", "看板任务 alpha", cwd=str(sb.root), source="kanban")
            plan = auto.plan_tick(sb.home, limit=20)
            ids = {i["session_id"] for i in plan["to_apply"]} | {
                i["session_id"] for i in plan["to_suggest"]}
            self.assertNotIn("kb1", ids, "看板会话不许进自动归组候选")

    def test_write_applied_syncs_cwd_and_skips_missing_dir(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            sb.add_project("beta", "Beta", create_dir=False)
            sb.add_session("s1", "普通一", cwd=str(sb.root))
            sb.add_session("s2", "普通二", cwd=str(sb.root))
            plan = {"core": base._load_core(), "threshold": 0.65,
                    "projects": {"alpha": {"slug": "alpha", "primary_path": str(a)},
                                 "beta": {"slug": "beta", "primary_path": str(sb.projects_root / "beta")}},
                    "to_apply": [
                        {"session_id": "s1", "title": "普通一", "project_slug": "alpha",
                         "confidence": 0.9, "reason": "t", "source": "rule"},
                        {"session_id": "s2", "title": "普通二", "project_slug": "beta",
                         "confidence": 0.9, "reason": "t", "source": "rule"},
                    ]}
            out = auto._write_applied(sb.home, plan, trigger="test", now=1.0)
            self.assertEqual(out["written"], ["s1"])
            self.assertEqual(sb.cwd_of("s1"), str(a), "写归属必须同时写 cwd")
            self.assertIsNone(sb.assignment("s2"), "目录缺失 ⇒ 归属不落")
            self.assertFalse((sb.projects_root / "beta").exists(), "不许静默建空目录")
            self.assertEqual([s["session_id"] for s in out["skipped"]], ["s2"])
            self.assertEqual(out["readback"]["mismatched"], [])

    def test_write_applied_rolls_back_when_cwd_write_fails(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            sb.add_session("s1", "普通一", cwd=str(sb.root))
            plan = {"core": base._load_core(), "threshold": 0.65,
                    "projects": {"alpha": {"slug": "alpha", "primary_path": str(a)},
                                 "gone": {"slug": "gone", "primary_path": str(sb.root)}},
                    "to_apply": [
                        {"session_id": "s1", "title": "普通一", "project_slug": "alpha",
                         "confidence": 0.9, "reason": "t", "source": "rule"},
                        {"session_id": "no-such-session", "title": "幽灵", "project_slug": "gone",
                         "confidence": 0.9, "reason": "t", "source": "rule"},
                    ]}
            with self.assertRaises(RuntimeError):
                auto._write_applied(sb.home, plan, trigger="test", now=1.0)
            self.assertIsNone(sb.assignment("s1"), "整批回滚：第一条归属也不许留下")
            self.assertEqual(sb.cwd_of("s1"), str(sb.root), "cwd 已补偿回原值")


class AiApplyKanbanTest(unittest.TestCase):
    """AI 分类写入：``_item_blocker`` 必须挡住看板会话。"""

    def test_item_blocker_rejects_kanban(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            sb.add_session("kb1", "看板任务", cwd=str(sb.root), source="kanban")
            core = base._load_core()
            projects = {p["slug"]: p for p in core.list_projects(sb.home)}
            item = {"session_id": "kb1", "tier": "apply",
                    "suggested": {"project_slug": "alpha", "confidence": 0.9, "reason": "t"},
                    "state": {}, "title": "看板任务", "cwd": str(sb.root),
                    "session_snapshot": {"exists": True, "cwd": str(sb.root),
                                         "message_count": 4}}
            check = aiapply._item_blocker(sb.home, core, item, projects)
            self.assertFalse(check["ok"])
            self.assertIn("kanban", check["why"])
            self.assertIsNone(sb.assignment("kb1"))

    def test_item_blocker_allows_ordinary_session(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            sb.add_session("ok1", "普通会话", cwd=str(sb.root), source="desktop")
            core = base._load_core()
            projects = {p["slug"]: p for p in core.list_projects(sb.home)}
            item = {"session_id": "ok1", "tier": "apply",
                    "suggested": {"project_slug": "alpha", "confidence": 0.9, "reason": "t"},
                    "state": {}, "title": "普通会话", "cwd": str(sb.root),
                    "session_snapshot": {"exists": True, "cwd": str(sb.root),
                                         "message_count": 4}}
            check = aiapply._item_blocker(sb.home, core, item, projects)
            self.assertTrue(check["ok"], check)
            self.assertEqual(check["target_slug"], "alpha")


if __name__ == "__main__":
    unittest.main(verbosity=2)

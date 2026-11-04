# -*- coding: utf-8 -*-
"""t_53b48743 A 段：一致性自检 + 对齐修复（口径 fix_cwd，可整批撤销）。

验收对应：
* A1 自检能准确列出不一致条目（数量可核）
* A2 对齐后 project_slug 与 cwd 目录名 100% 一致（0 条不一致）
* A3 修复可整批撤销，撤销后逐条回到原值（含 cwd 原值）
* C  预览零写入（沙盒三库 + 目录树逐字节相同）
"""
from __future__ import annotations

import sys
from pathlib import Path

DASH = Path(__file__).resolve().parents[1]
for extra in (str(DASH), str(Path(__file__).resolve().parent)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import unittest  # noqa: E402

from project_sandbox import ProjectSandbox, body, status_of  # noqa: E402

import api_autogroup as base  # noqa: E402
import api_autogroup_projects as api  # noqa: E402
import autogroup_align as align  # noqa: E402
import autogroup_projects as ent  # noqa: E402


class ConsistencyAuditTest(unittest.TestCase):
    """A1：自检把「归属 ≠ cwd 目录」的条目一条不漏地列出来。"""

    def test_lists_only_mismatched_applied_assignments(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            b = sb.add_project("beta", "Beta")
            sb.add_session("ok1", "一致的", cwd=str(a))
            sb.add_assignment("ok1", "alpha")
            sb.add_session("bad1", "不一致的", cwd=str(b))
            sb.add_assignment("bad1", "alpha")
            sb.add_session("bad2", "不一致二", cwd=str(b))
            sb.add_assignment("bad2", "alpha")
            sb.add_session("sug1", "只是建议", cwd=str(b))
            sb.add_assignment("sug1", "alpha", status="suggested")

            report = align.audit(sb.home)
            counts = report["counts"]
            self.assertEqual(counts["total"], 3, "只看 applied（suggested 不算）")
            self.assertEqual(counts["consistent"], 1)
            self.assertEqual(counts["inconsistent"], 2)
            bad = [i for i in report["items"] if i.get("kind") == "mismatch"]
            self.assertEqual(sorted(i["session_id"] for i in bad), ["bad1", "bad2"])
            for item in bad:
                self.assertEqual(item["slug"], "alpha")
                self.assertEqual(item["cwd_dir"], "beta")
                self.assertEqual(item["target_path"], str(a))
                self.assertTrue(item["target_dir_exists"])

    @unittest.skip(
        "t_ac537221：GET /autogroup/consistency 路由已下线；autogroup_align.audit 的只读性"
        "由本类其余用例（直接调库函数）覆盖。")
    def test_audit_is_read_only(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            b = sb.add_project("beta", "Beta")
            sb.add_session("s1", "x", cwd=str(b))
            sb.add_assignment("s1", "alpha")
            before = sb.snapshot()
            align.audit(sb.home)
            align.mismatch_pairs(sb.home)
            body(api.get_consistency())
            body(api.get_consistency_pairs())
            body(api.get_align_preview(mode="fix_cwd"))
            body(api.get_align_preview(mode="fix_slug"))
            body(api.get_entities())
            self.assertEqual(before, sb.snapshot(), "自检/预览必须零写入（含 schema）")

    def test_pairs_group_by_project_and_cwd_dir(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            b = sb.add_project("beta", "Beta")
            c = sb.add_project("gamma", "Gamma")
            for i in range(3):
                sb.add_session("s%d" % i, "t", cwd=str(b))
                sb.add_assignment("s%d" % i, "alpha")
            sb.add_session("s9", "t", cwd=str(c))
            sb.add_assignment("s9", "alpha")
            pairs = align.mismatch_pairs(sb.home)
            self.assertEqual(pairs[0]["slug"], "alpha")
            self.assertEqual(pairs[0]["cwd_dir"], "beta")
            self.assertEqual(pairs[0]["count"], 3)
            self.assertEqual(sum(p["count"] for p in pairs), 4)

    def test_null_cwd_is_counted_and_reported(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            sb.add_session("s1", "无 cwd", cwd=None)
            sb.add_assignment("s1", "alpha")
            report = align.audit(sb.home)
            self.assertEqual(report["counts"]["inconsistent"], 1)
            self.assertEqual(report["counts"]["cwd_null"], 1)
            self.assertTrue(report["items"][0]["cwd_null"])

    def test_kanban_sessions_are_flagged_not_aligned(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            b = sb.add_project("beta", "Beta")
            sb.add_session("kb1", "看板任务", cwd=str(b), source="kanban")
            sb.add_assignment("kb1", "alpha")
            sb.add_session("ok1", "普通", cwd=str(b), source="desktop")
            sb.add_assignment("ok1", "alpha")
            report = align.audit(sb.home)
            self.assertEqual(report["counts"]["kanban_excluded"], 1)
            plan = align.preview(sb.home, mode="fix_cwd")
            self.assertEqual([i["session_id"] for i in plan["items"]], ["ok1"])
            self.assertIn("kanban_excluded", [b_["why"] for b_ in plan["blocked"]])


class AlignPreviewTest(unittest.TestCase):
    """预览必须逐条给出 from→to，且不写任何东西。"""

    def test_preview_shows_from_to_for_fix_cwd(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            b = sb.add_project("beta", "Beta")
            sb.add_session("s1", "x", cwd=str(b))
            sb.add_assignment("s1", "alpha")
            plan = align.preview(sb.home, mode="fix_cwd")
            self.assertTrue(plan["ok"])
            self.assertEqual(plan["affected"], 1)
            item = plan["items"][0]
            self.assertEqual(item["from_slug"], "alpha")
            self.assertEqual(item["to_slug"], "alpha")
            self.assertEqual(item["from_cwd"], str(b))
            self.assertEqual(item["to_cwd"], str(a))
            self.assertEqual(item["action"], "write_cwd")

    def test_preview_fix_slug_refuses_unknown_cwd_project(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            stray = sb.root / "not-a-project"
            stray.mkdir(parents=True, exist_ok=True)
            sb.add_session("s1", "x", cwd=str(stray))
            sb.add_assignment("s1", "alpha")
            plan = align.preview(sb.home, mode="fix_slug")
            self.assertEqual(plan["affected"], 0)
            self.assertEqual([b_["why"] for b_ in plan["blocked"]], ["no_project_for_cwd"])

    def test_preview_blocks_when_target_dir_missing(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha", create_dir=False)
            b = sb.add_project("beta", "Beta")
            sb.add_session("s1", "x", cwd=str(b))
            sb.add_assignment("s1", "alpha")
            plan = align.preview(sb.home, mode="fix_cwd")
            self.assertEqual(plan["affected"], 0)
            self.assertEqual([b_["why"] for b_ in plan["blocked"]], ["dir_missing"])

    def test_preview_never_creates_missing_dir(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha", create_dir=False)
            b = sb.add_project("beta", "Beta")
            sb.add_session("s1", "x", cwd=str(b))
            sb.add_assignment("s1", "alpha")
            before = sb.snapshot()
            align.preview(sb.home, mode="fix_cwd")
            self.assertEqual(before, sb.snapshot(), "目录缺失时预览也不许建目录")

    def test_preview_rejects_unknown_mode(self):
        with ProjectSandbox() as sb:
            plan = align.preview(sb.home, mode="whatever")
            self.assertFalse(plan["ok"])
            self.assertEqual(plan["code"], "invalid_mode")

    def test_preview_scope_by_slug_and_session(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            sb.add_project("beta", "Beta")
            other = sb.add_project("gamma", "Gamma")
            sb.add_session("s1", "x", cwd=str(other))
            sb.add_assignment("s1", "alpha")
            sb.add_session("s2", "y", cwd=str(other))
            sb.add_assignment("s2", "beta")
            self.assertEqual(align.preview(sb.home, mode="fix_cwd", slugs=["alpha"])["affected"], 1)
            self.assertEqual(align.preview(sb.home, mode="fix_cwd",
                                           session_ids=["s2"])["affected"], 1)
            self.assertEqual(align.preview(sb.home, mode="fix_cwd",
                                           slugs=["alpha"], session_ids=["s2"])["affected"], 0)


@unittest.skip(
    "t_ac537221：POST /autogroup/align 路由已下线（一致性对齐并入 /autogroup/apply 的修复路径）；"
    "本类保留作历史契约参考。")
class AlignExecuteTest(unittest.TestCase):
    """A2/A3：写完之后 0 条不一致；撤销后 cwd 逐条回原值。"""

    def test_fix_cwd_reaches_zero_inconsistency(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            b = sb.add_project("beta", "Beta")
            c = sb.add_project("gamma", "Gamma")
            cases = [("s1", "alpha", str(b)), ("s2", "alpha", str(c)), ("s3", "beta", str(a)),
                     ("s4", "beta", str(c))]
            for sid, slug, cwd in cases:
                sb.add_session(sid, "t-" + sid, cwd=cwd)
                sb.add_assignment(sid, slug)
            self.assertEqual(align.audit(sb.home)["counts"]["inconsistent"], 4)

            resp = body(api.post_align({"mode": "fix_cwd", "confirm": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(resp["data"]["written"], 4)
            self.assertEqual(resp["data"]["readback"]["mismatched"], [])

            after = align.audit(sb.home)
            self.assertEqual(after["counts"]["inconsistent"], 0)
            self.assertEqual(sb.cwd_of("s1"), str(a))
            self.assertEqual(sb.cwd_of("s3"), str(b))
            for item in after["items"]:
                self.assertNotEqual(item.get("kind"), "mismatch")

    def test_align_requires_confirm_and_preview_is_in_error_body(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            b = sb.add_project("beta", "Beta")
            sb.add_session("s1", "x", cwd=str(b))
            sb.add_assignment("s1", "alpha")
            before = sb.snapshot()
            resp = api.post_align({"mode": "fix_cwd"})
            self.assertEqual(status_of(resp), 400)
            payload = body(resp)
            self.assertEqual(payload["error"]["code"], "confirmation_required")
            self.assertIn("preview", payload["error"])
            self.assertEqual(before, sb.snapshot(), "缺 confirm 必须零写入")

    def test_confirm_string_true_is_not_accepted(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            b = sb.add_project("beta", "Beta")
            sb.add_session("s1", "x", cwd=str(b))
            sb.add_assignment("s1", "alpha")
            resp = api.post_align({"mode": "fix_cwd", "confirm": "true"})
            self.assertEqual(status_of(resp), 400)

    def test_undo_restores_every_cwd_to_original_value(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            b = sb.add_project("beta", "Beta")
            c = sb.add_project("gamma", "Gamma")
            originals = {"s1": str(b), "s2": str(c), "s3": str(a)}
            for sid, cwd in originals.items():
                sb.add_session(sid, "t-" + sid, cwd=cwd)
                sb.add_assignment(sid, "alpha")
            done = body(api.post_align({"mode": "fix_cwd", "confirm": True}))
            op_id = done["data"]["op_id"]
            for sid in originals:
                self.assertEqual(sb.cwd_of(sid), str(a), "先确认真的改了")

            undo = body(base.post_undo({"op_id": op_id}))
            self.assertTrue(undo["ok"], undo)
            for sid, cwd in originals.items():
                self.assertEqual(sb.cwd_of(sid), cwd, "撤销必须逐条回到原值：" + sid)
            # 归属行不该被撤销动到
            for sid in originals:
                self.assertEqual(sb.assignment(sid)["project_slug"], "alpha")

    def test_undo_restores_null_cwd(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            sb.add_session("s1", "原本没有 cwd", cwd=None)
            sb.add_assignment("s1", "alpha")
            done = body(api.post_align({"mode": "fix_cwd", "confirm": True}))
            self.assertEqual(sb.cwd_of("s1"), str(a))
            body(base.post_undo({"op_id": done["data"]["op_id"]}))
            self.assertIsNone(sb.cwd_of("s1"), "原来 NULL 就要还原成 NULL")

    def test_undo_twice_is_refused(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            b = sb.add_project("beta", "Beta")
            sb.add_session("s1", "x", cwd=str(b))
            sb.add_assignment("s1", "alpha")
            done = body(api.post_align({"mode": "fix_cwd", "confirm": True}))
            op_id = done["data"]["op_id"]
            self.assertTrue(body(base.post_undo({"op_id": op_id}))["ok"])
            second = base.post_undo({"op_id": op_id})
            self.assertEqual(status_of(second), 409)
            self.assertEqual(body(second)["error"]["code"], "already_undone")

    def test_unselected_sessions_untouched(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            b = sb.add_project("beta", "Beta")
            sb.add_session("s1", "选中", cwd=str(b))
            sb.add_assignment("s1", "alpha")
            sb.add_session("s2", "未选中", cwd=str(b))
            sb.add_assignment("s2", "beta")
            before_s2 = sb.assignment("s2")
            resp = body(api.post_align({"mode": "fix_cwd", "confirm": True,
                                        "session_ids": ["s1"]}))
            self.assertEqual(resp["data"]["written"], 1)
            self.assertEqual(sb.cwd_of("s2"), str(b), "未选中的会话 cwd 不许变")
            self.assertEqual(sb.assignment("s2"), before_s2, "未选中的归属行逐字段不许变")

    def test_fix_slug_mode_moves_assignment_not_cwd(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            b = sb.add_project("beta", "Beta")
            sb.add_session("s1", "x", cwd=str(b))
            sb.add_assignment("s1", "alpha")
            resp = body(api.post_align({"mode": "fix_slug", "confirm": True,
                                        "confirm_slug": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(sb.assignment("s1")["project_slug"], "beta")
            self.assertEqual(sb.cwd_of("s1"), str(b), "fix_slug 不许动 cwd")
            self.assertEqual(align.audit(sb.home)["counts"]["inconsistent"], 0)
            undo = body(base.post_undo({"op_id": resp["data"]["op_id"]}))
            self.assertTrue(undo["ok"], undo)
            self.assertEqual(sb.assignment("s1")["project_slug"], "alpha")

    def test_fix_slug_needs_extra_confirmation(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            b = sb.add_project("beta", "Beta")
            sb.add_session("s1", "x", cwd=str(b))
            sb.add_assignment("s1", "alpha")
            resp = api.post_align({"mode": "fix_slug", "confirm": True})
            self.assertEqual(status_of(resp), 400)
            self.assertEqual(body(resp)["error"]["code"], "confirmation_required")

    def test_kanban_session_never_written_even_if_asked(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            b = sb.add_project("beta", "Beta")
            sb.add_session("kb1", "看板任务", cwd=str(b), source="kanban")
            sb.add_assignment("kb1", "alpha")
            resp = body(api.post_align({"mode": "fix_cwd", "confirm": True,
                                        "session_ids": ["kb1"]}))
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(resp["data"]["written"], 0, "看板会话一条都不许写")
            self.assertEqual(sb.cwd_of("kb1"), str(b))

    def test_include_kanban_requires_explicit_opt_in(self):
        """默认排除；显式 include_kanban=true 才对齐存量看板脏数据的 cwd（归属行不动）。"""
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            b = sb.add_project("beta", "Beta")
            sb.add_session("kb1", "看板任务", cwd=str(b), source="kanban")
            sb.add_assignment("kb1", "alpha")
            self.assertEqual(align.preview(sb.home, mode="fix_cwd")["affected"], 0)
            self.assertEqual(align.preview(sb.home, mode="fix_cwd",
                                           include_kanban=True)["affected"], 1)
            resp = body(api.post_align({"mode": "fix_cwd", "confirm": True,
                                        "include_kanban": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(sb.cwd_of("kb1"), str(a))
            self.assertEqual(sb.assignment("kb1")["project_slug"], "alpha",
                             "对齐 cwd 不等于清理归属")

    def test_execute_is_idempotent_when_nothing_to_do(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            sb.add_session("s1", "已一致", cwd=str(a))
            sb.add_assignment("s1", "alpha")
            resp = body(api.post_align({"mode": "fix_cwd", "confirm": True}))
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["data"]["written"], 0)
            self.assertIsNone(resp["data"]["op_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

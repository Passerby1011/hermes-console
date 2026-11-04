# -*- coding: utf-8 -*-
"""AI 分类写卡（t_6e3b1a32）真实测试：沙盒 fixture 全链路，不碰真实 home。

跑法：``python plugins/hermes-console/dashboard/tests/test_autogroup_ai_apply.py``
（stdlib unittest；本机 venv 没装 pytest）。

覆盖的硬约束：
* 没有 confirm=true 一律 400，且一行都不写；
* <0.5 不可选、选了整批不写；0.5–<0.8 只写 suggested 不改 cwd；>=0.8 写 applied 并同步 cwd；
* 原 applied（原 56 条）硬性排除；
* 预览快照与现状不一致 ⇒ 跳过该条 + 暂停整批 + 如实报告已完成/未执行；
* cwd 用 ``WHERE id=? AND cwd IS ?`` + changes()==1 + 回读；
* 撤销逐字段还原（原本不存在 ⇒ 删除），遇到后续修改暂停不覆盖；
* 新建项目需 >=3 条支持 + 有效选中，路径不越界、不覆盖非空目录，撤销只在无引用无内容时清理。
"""
from __future__ import annotations

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
    import autogroup_ai_projects as ai_projects  # noqa: E402
except ModuleNotFoundError as _exc:
    if _exc.name not in _REMOVED_MODULES:
        raise
    pytestmark = pytest.mark.skip(_REMOVED_REASON)


def item(session_id: str, slug: str, confidence: float, *, title: str = "标题",
         reason: str = "AI 认为相关", new_project=None, support=None,
         unjudged_reason=None) -> dict:
    return {"session_id": session_id, "title": title,
            "snapshot": {"cwd": None, "message_count": 4, "last_activity_at": 0.0},
            "suggested": {"project_slug": slug, "project_name": slug, "confidence": confidence,
                          "reason": reason, "new_project": new_project,
                          "support_session_ids": support or []},
            "unjudged_reason": unjudged_reason}


def preview(*items: dict, model: str = "test-model", scope_rule: str = "") -> dict:
    """造一份「引擎产出的预览」。默认带上当前口径（scope_rule），与真实引擎一致；
    传 ``scope_rule=None`` 可造一份**旧范围**批次，用来验证确认入口会拦它。"""
    rule = ai_engine.SCOPE_RULE if scope_rule == "" else scope_rule
    scope = {"total_available": len(items), "excluded_kanban": 0}
    if rule is not None:
        scope["scope_rule"] = rule
    return {"contract_version": contract.CONTRACT_VERSION, "generated_at": 1.0,
            "engine": {"name": "autogroup_ai", "model": model, "elapsed_ms": 12.5,
                       "usage": {"total_tokens": 100}, "usage_estimated": False},
            "scope": scope,
            "items": list(items), "notes": ["fixture"]}


class TestContract(unittest.TestCase):
    """纯函数层：分档边界与拒收口径。"""

    def test_tier_boundaries(self):
        self.assertEqual(contract.tier_for(0.80), "apply")
        self.assertEqual(contract.tier_for(0.79), "suggest")
        self.assertEqual(contract.tier_for(0.50), "suggest")
        self.assertEqual(contract.tier_for(0.49), "unjudged")

    def test_confidence_out_of_range_rejected(self):
        with self.assertRaises(contract.ContractError) as ctx:
            contract.normalize_item({"session_id": "s", "suggested": {"project_slug": "mem",
                                                                     "confidence": 1.4}},
                                    seq=0, existing_slugs={"mem": {}})
        self.assertEqual(ctx.exception.code, "confidence_out_of_range")

    def test_bool_confidence_rejected(self):
        with self.assertRaises(contract.ContractError):
            contract.normalize_item({"session_id": "s", "suggested": {"project_slug": "mem",
                                                                     "confidence": True}},
                                    seq=0, existing_slugs={"mem": {}})

    def test_unknown_project_rejected(self):
        with self.assertRaises(contract.ContractError) as ctx:
            contract.normalize_item({"session_id": "s", "suggested": {"project_slug": "nope",
                                                                     "confidence": 0.9}},
                                    seq=0, existing_slugs={"mem": {}})
        self.assertEqual(ctx.exception.code, "unknown_project")

    def test_new_project_conflicting_existing_rejected(self):
        with self.assertRaises(contract.ContractError) as ctx:
            contract.normalize_item({"session_id": "s", "suggested": {
                "project_slug": None, "confidence": 0.9,
                "new_project": {"slug": "mem", "name": "撞名"}}}, seq=0, existing_slugs={"mem": {}})
        self.assertEqual(ctx.exception.code, "new_project_conflicts_existing")

    def test_new_project_needs_three_support(self):
        two = contract.normalize_item({"session_id": "s", "suggested": {
            "project_slug": None, "confidence": 0.9, "new_project": {"slug": "fresh", "name": "新"},
            "support_session_ids": ["a", "b"]}}, seq=0, existing_slugs={"mem": {}})
        self.assertFalse(two["write_policy"]["can_write"])
        self.assertEqual(two["write_policy"]["status_if_confirmed"], "suggested")
        three = contract.normalize_item({"session_id": "s", "suggested": {
            "project_slug": None, "confidence": 0.9, "new_project": {"slug": "fresh", "name": "新"},
            "support_session_ids": ["a", "b", "c"]}}, seq=0, existing_slugs={"mem": {}})
        # 支持够 3 条，但项目还不存在 ⇒ 仍要等确认建项目，不允许直接写
        self.assertFalse(three["write_policy"]["can_write"])
        self.assertIn("先经确认创建项目", three["write_policy"]["why"])

    def test_flat_engine_shape_is_accepted(self):
        """引擎若按原卡 prompt 输出顶层平铺字段（project_slug/confidence/...），也要能收。"""
        flat = {"session_id": "s", "title": "标题", "cwd": "D:\\x",
                "project_slug": "mem", "confidence": 0.9, "reason": "平铺形状", "new_project": None}
        normalized = contract.normalize_item(flat, seq=0, existing_slugs={"mem": {}})
        self.assertEqual(normalized["suggested"]["project_slug"], "mem")
        self.assertEqual(normalized["tier"], "apply")
        self.assertTrue(normalized["write_policy"]["can_write"])

    def test_unjudged_never_writable(self):
        low = contract.normalize_item({"session_id": "s", "suggested": {
            "project_slug": "mem", "confidence": 0.2}}, seq=0, existing_slugs={"mem": {}})
        self.assertEqual(low["tier"], "unjudged")
        self.assertFalse(low["write_policy"]["can_write"])
        self.assertTrue(low["unjudged_reason"])


class TestPreview(unittest.TestCase):
    """预览落库：硬性排除 applied、服务端自己记快照、不写归属。"""

    def setUp(self):
        self.sb = Sandbox()
        self.sb.add_project("memory", "记忆")
        self.sb.add_project("quick-send", "快传")
        self.sb.add_session("s_apply", "写代码", r"D:\work\old")
        self.sb.add_session("s_suggest", "看日志", r"D:\work\old2")
        self.sb.add_session("s_low", "闲聊", r"D:\work\old3")
        self.sb.add_session("protected-0", "老会话", r"D:\work\old4")
        self.sb.__enter__()

    def tearDown(self):
        self.sb.__exit__()

    def test_preview_excludes_applied_and_keeps_unjudged_visible(self):
        resp = apply_mod.post_ai_preview({"preview": preview(
            item("s_apply", "memory", 0.9),
            item("s_suggest", "memory", 0.6),
            item("s_low", "memory", 0.2),
            item("protected-0", "memory", 0.99))})
        data = body(resp)
        self.assertTrue(data["ok"])
        self.assertEqual(status_of(resp), 200)
        batch_id = data["data"]["batch_id"]
        self.assertTrue(batch_id.startswith("aip-"))
        items = data["data"]["preview"]["items"]
        self.assertEqual([i["session_id"] for i in items], ["s_apply", "s_suggest", "s_low"])
        self.assertEqual(len(data["data"]["preview"]["excluded"]), 1)
        self.assertEqual(data["data"]["preview"]["excluded"][0]["session_id"], "protected-0")
        low = [i for i in items if i["session_id"] == "s_low"][0]
        self.assertEqual(low["tier"], "unjudged")
        self.assertFalse(low["write_policy"]["can_write"])
        # 预览一行归属都不许写
        self.assertIsNone(self.sb.assignment("s_apply"))
        self.assertIsNone(self.sb.assignment("s_suggest"))
        # 服务端自己记了权威快照（会话确实存在 + cwd 是库里的真实值）
        apply_item = [i for i in items if i["session_id"] == "s_apply"][0]
        self.assertTrue(apply_item["state"]["exists"] is False)  # 本来没有归属行
        self.assertEqual(apply_item["session_snapshot"]["cwd"], r"D:\work\old")
        self.assertEqual(self.sb.batch_row(batch_id)["status"], "pending")

    def test_preview_rejects_unknown_project(self):
        resp = apply_mod.post_ai_preview({"preview": preview(item("s_apply", "ghost", 0.9))})
        self.assertEqual(status_of(resp), 400)
        self.assertEqual(body(resp)["error"]["code"], "unknown_project")

    def test_preview_get_shows_live_selectability(self):
        resp = apply_mod.post_ai_preview({"preview": preview(item("s_apply", "memory", 0.9))})
        batch_id = body(resp)["data"]["batch_id"]
        got = body(apply_mod.get_ai_preview(batch_id))
        self.assertTrue(got["ok"])
        self.assertEqual(got["data"]["items"][0]["target_slug"], "memory")
        self.assertTrue(got["data"]["items"][0]["selectable"])

    def test_unknown_batch_404(self):
        self.assertEqual(status_of(apply_mod.get_ai_preview("aip-nope")), 404)


class TestConfirm(unittest.TestCase):
    """确认执行：批次绑定、逐条选中、分档写入、逐条回读、暂停语义。"""

    def setUp(self):
        self.sb = Sandbox()
        self.sb.add_project("memory", "记忆")
        self.sb.add_project("quick-send", "快传")
        self.sb.add_session("s_apply", "写代码", r"D:\work\old")
        self.sb.add_session("s_suggest", "看日志", r"D:\work\old2")
        self.sb.add_session("s_low", "闲聊", r"D:\work\old3")
        self.sb.add_session("s_second", "第二件事", r"D:\work\old4")
        self.sb.__enter__()
        resp = apply_mod.post_ai_preview({"preview": preview(
            item("s_apply", "memory", 0.9), item("s_suggest", "memory", 0.6),
            item("s_low", "memory", 0.2), item("s_second", "quick-send", 0.85))})
        self.batch_id = body(resp)["data"]["batch_id"]
        self.memory_path = str(self.sb.home / "projects" / "memory")

    def tearDown(self):
        self.sb.__exit__()

    def test_confirm_without_confirm_writes_nothing(self):
        resp = apply_mod.post_ai_confirm({"batch_id": self.batch_id, "selected": ["s_apply"]})
        data = body(resp)
        self.assertEqual(status_of(resp), 400)
        self.assertEqual(data["error"]["code"], "confirmation_required")
        self.assertIn("preview", data)
        self.assertIsNone(self.sb.assignment("s_apply"))
        self.assertEqual(self.sb.batch_row(self.batch_id)["status"], "pending")

    def test_unknown_batch_and_missing_batch_id(self):
        self.assertEqual(status_of(apply_mod.post_ai_confirm(
            {"batch_id": "aip-nope", "confirm": True, "selected": ["s_apply"]})), 404)
        self.assertEqual(status_of(apply_mod.post_ai_confirm({"confirm": True})), 400)

    def test_old_scope_batch_is_refused_without_writing(self):
        """旧范围批次（无 scope_rule，可能含看板任务会话）不得绕过新口径写入。"""
        legacy = preview(item("s_apply", "memory", 0.9), scope_rule=None)
        self.assertNotIn("scope_rule", legacy["scope"])
        resp = apply_mod.post_ai_preview({"preview": legacy})
        batch_id = body(resp)["data"]["batch_id"]
        out = apply_mod.post_ai_confirm({"batch_id": batch_id, "confirm": True,
                                         "selected": ["s_apply"]})
        data = body(out)
        self.assertEqual(status_of(out), 409, data)
        self.assertEqual(data["error"]["code"], "scope_stale")
        self.assertTrue(data["error"]["needs_regeneration"])
        self.assertIsNone(self.sb.assignment("s_apply"), "旧批次一条都不许写")
        self.assertEqual(self.sb.batch_row(batch_id)["status"], "pending")
        listed = {row["batch_id"]: row for row in
                  body(apply_mod.get_ai_batches(limit=20, status=""))["data"]["batches"]}
        self.assertTrue(listed[batch_id]["needs_regeneration"])
        view = body(apply_mod.get_ai_preview(batch_id))["data"]
        self.assertTrue(view["needs_regeneration"])
        self.assertIn("旧范围", view["scope_stale_reason"])

    def test_apply_tier_writes_applied_and_syncs_cwd(self):
        resp = apply_mod.post_ai_confirm({"batch_id": self.batch_id, "confirm": True,
                                          "selected": ["s_apply"]})
        data = body(resp)
        self.assertEqual(status_of(resp), 200, data)
        self.assertEqual(data["data"]["written"], 1)
        self.assertEqual(data["data"]["status"], "done")
        row = self.sb.assignment("s_apply")
        self.assertEqual(row["status"], "applied")
        self.assertEqual(row["project_slug"], "memory")
        self.assertEqual(row["source"], "ai")
        self.assertEqual(row["batch_id"], self.batch_id)
        self.assertEqual(self.sb.cwd_of("s_apply"), self.memory_path)
        ops = self.sb.console_ops(data["data"]["op_id"])
        header = [o for o in ops if o["kind"] == "header"][0]
        self.assertEqual(header["op"], "ai.apply")
        self.assertEqual(header["batch_id"], self.batch_id)
        sess = [o for o in ops if o["kind"] == "session"][0]
        self.assertIsNone(sess["row_before"])          # 写前整行 = 不存在
        self.assertEqual(sess["cwd_before"], r"D:\work\old")
        self.assertEqual(sess["cwd_before_known"], 1)
        self.assertEqual(sess["cwd_written"], 1)
        self.assertEqual(self.sb.batch_row(self.batch_id)["status"], "done")
        readback = body(apply_mod.get_ai_readback(self.batch_id))
        self.assertEqual(readback["data"]["bad"], [])
        self.assertEqual(readback["data"]["items"][0]["outcome"], "written")

    def test_suggest_tier_writes_suggested_without_touching_cwd(self):
        resp = apply_mod.post_ai_confirm({"batch_id": self.batch_id, "confirm": True,
                                          "selected": ["s_suggest"]})
        data = body(resp)
        self.assertEqual(data["data"]["written"], 1)
        row = self.sb.assignment("s_suggest")
        self.assertEqual(row["status"], "suggested")
        self.assertIsNone(row["applied_at"])
        self.assertEqual(self.sb.cwd_of("s_suggest"), r"D:\work\old2")
        sess = [o for o in self.sb.console_ops(data["data"]["op_id"]) if o["kind"] == "session"][0]
        self.assertEqual(sess["cwd_written"], 0)

    def test_unjudged_selection_rejected_entirely(self):
        resp = apply_mod.post_ai_confirm({"batch_id": self.batch_id, "confirm": True,
                                          "selected": ["s_low"]})
        data = body(resp)
        self.assertEqual(status_of(resp), 400)
        self.assertEqual(data["error"]["code"], "nothing_selected")
        self.assertIsNone(self.sb.assignment("s_low"))

    def test_foreign_session_id_rejected(self):
        resp = apply_mod.post_ai_confirm({"batch_id": self.batch_id, "confirm": True,
                                          "selected": ["s_apply", "ghost"]})
        data = body(resp)
        self.assertEqual(status_of(resp), 400)
        self.assertEqual(data["error"]["code"], "selection_rejected")
        self.assertIsNone(self.sb.assignment("s_apply"))   # 一条都没写

    def test_stale_item_pauses_batch_after_partial_write(self):
        # 预览之后有人手改了 s_second 的归属 ⇒ 写前校验必须发现并暂停
        self.sb.add_assignment("s_second", "memory", status="suggested", confidence=0.3,
                               reason="人工改过", source="panel")
        resp = apply_mod.post_ai_confirm({"batch_id": self.batch_id, "confirm": True,
                                          "selected": ["s_apply", "s_second"]})
        data = body(resp)
        self.assertEqual(status_of(resp), 200, data)
        self.assertEqual(data["data"]["status"], "paused")
        self.assertEqual(data["data"]["written"], 1)
        results = data["data"]["results"]
        self.assertEqual([r["outcome"] for r in results], ["written", "skipped"])
        self.assertIn("预览快照与现状不一致", results[1]["why"])
        kept = self.sb.assignment("s_second")
        self.assertEqual(kept["project_slug"], "memory")   # 没被覆盖
        self.assertEqual(kept["reason"], "人工改过")
        self.assertEqual(self.sb.batch_row(self.batch_id)["status"], "paused")

    def test_first_item_blocked_returns_409(self):
        self.sb.add_assignment("s_apply", "quick-send", status="applied")
        resp = apply_mod.post_ai_confirm({"batch_id": self.batch_id, "confirm": True,
                                          "selected": ["s_apply"]})
        data = body(resp)
        self.assertEqual(status_of(resp), 409)
        self.assertEqual(data["error"]["code"], "batch_paused")
        self.assertEqual(data["result"]["written"], 0)
        self.assertEqual(self.sb.assignment("s_apply")["project_slug"], "quick-send")

    def test_missing_project_dir_pauses(self):
        import shutil as _shutil
        _shutil.rmtree(self.memory_path)
        resp = apply_mod.post_ai_confirm({"batch_id": self.batch_id, "confirm": True,
                                          "selected": ["s_apply"]})
        data = body(resp)
        self.assertEqual(status_of(resp), 409)
        self.assertIn("项目目录不是真实目录", data["result"]["results"][0]["why"])
        self.assertIsNone(self.sb.assignment("s_apply"))

    def test_batch_cannot_be_confirmed_twice(self):
        apply_mod.post_ai_confirm({"batch_id": self.batch_id, "confirm": True, "selected": ["s_apply"]})
        resp = apply_mod.post_ai_confirm({"batch_id": self.batch_id, "confirm": True,
                                          "selected": ["s_suggest"]})
        self.assertEqual(status_of(resp), 409)
        self.assertEqual(body(resp)["error"]["code"], "batch_consumed")


class TestNewProject(unittest.TestCase):
    """新建项目候选：>=3 条支持 + 有效选中 + 目录安全 + 不覆盖。"""

    def setUp(self):
        self.sb = Sandbox()
        self.sb.add_project("memory", "记忆")
        for index in range(1, 5):
            self.sb.add_session("s%d" % index, "主题会话 %d" % index, r"D:\work\w%d" % index)
        self.sb.__enter__()

    def tearDown(self):
        self.sb.__exit__()

    def _preview(self, support):
        return apply_mod.post_ai_preview({"preview": preview(item(
            "s1", None, 0.9, new_project={"slug": "fresh-topic", "name": "新主题"},
            support=support))})

    def test_support_below_three_is_not_selectable(self):
        batch_id = body(self._preview(["s1", "s2"]))["data"]["batch_id"]
        got = body(apply_mod.get_ai_preview(batch_id))["data"]["items"][0]
        self.assertFalse(got["selectable"])
        self.assertIn("同主题支持", got["selectable_why"])
        resp = apply_mod.post_ai_confirm({"batch_id": batch_id, "confirm": True, "selected": ["s1"]})
        self.assertEqual(status_of(resp), 400)
        self.assertEqual(body(resp)["error"]["code"], "nothing_selected")

    def test_three_support_still_needs_project_creation_first(self):
        batch_id = body(self._preview(["s1", "s2", "s3"]))["data"]["batch_id"]
        got = body(apply_mod.get_ai_preview(batch_id))["data"]["items"][0]
        self.assertFalse(got["selectable"])
        self.assertIn("先经确认创建项目", got["selectable_why"])

    def test_project_create_requires_confirm(self):
        batch_id = body(self._preview(["s1", "s2", "s3"]))["data"]["batch_id"]
        resp = apply_mod.post_ai_project({"batch_id": batch_id, "slug": "fresh-topic"})
        self.assertEqual(status_of(resp), 400)
        self.assertEqual(body(resp)["error"]["code"], "confirmation_required")
        self.assertIsNone(self.sb.project_row("fresh-topic"))
        self.assertFalse((self.sb.home / "projects" / "fresh-topic").exists())

    def test_project_create_then_confirm_write(self):
        batch_id = body(self._preview(["s1", "s2", "s3"]))["data"]["batch_id"]
        created = body(apply_mod.post_ai_project({"batch_id": batch_id, "slug": "fresh-topic",
                                                 "confirm": True}))
        self.assertTrue(created["ok"], created)
        row = self.sb.project_row("fresh-topic")
        self.assertIsNotNone(row)
        target = self.sb.home / "projects" / "fresh-topic"
        self.assertEqual(Path(row["primary_path"]).resolve(), target.resolve())
        self.assertTrue(target.is_dir())
        ops = self.sb.console_ops(created["data"]["op_id"])
        self.assertEqual([o for o in ops if o["kind"] == "header"][0]["op"], "ai.project.create")
        # 项目建好之后这条才可选，并且写得进 applied + cwd 同步到新目录
        got = body(apply_mod.get_ai_preview(batch_id))["data"]["items"][0]
        self.assertTrue(got["selectable"])
        resp = apply_mod.post_ai_confirm({"batch_id": batch_id, "confirm": True, "selected": ["s1"]})
        data = body(resp)
        self.assertTrue(data["ok"], data)
        self.assertEqual(self.sb.assignment("s1")["project_slug"], "fresh-topic")
        self.assertEqual(self.sb.assignment("s1")["status"], "applied")
        self.assertEqual(Path(self.sb.cwd_of("s1")).resolve(), target.resolve())

    def test_refuses_non_empty_dir(self):
        batch_id = body(self._preview(["s1", "s2", "s3"]))["data"]["batch_id"]
        target = self.sb.home / "projects" / "fresh-topic"
        target.mkdir(parents=True, exist_ok=True)
        (target / "keep.txt").write_text("别人的文件", encoding="utf-8")
        resp = apply_mod.post_ai_project({"batch_id": batch_id, "slug": "fresh-topic", "confirm": True})
        self.assertEqual(status_of(resp), 409)
        self.assertEqual(body(resp)["error"]["code"], "dir_not_empty")
        self.assertTrue((target / "keep.txt").exists())
        self.assertIsNone(self.sb.project_row("fresh-topic"))

    def test_path_escape_and_bad_slug_rejected(self):
        for slug in ("../evil", "a/b", "Fresh", "x"):
            with self.assertRaises(ai_projects.ProjectCreateError):
                ai_projects.create_project(self.sb.home, slug=slug, name="坏",
                                           support_session_ids=["s1", "s2", "s3"],
                                           selected_session_ids=["s1"], existing_slugs={})


class TestUndo(unittest.TestCase):
    """撤销：逐字段还原（原本不存在⇒删除）、冲突暂停不覆盖、新建项目安全清理。"""

    def setUp(self):
        self.sb = Sandbox()
        self.sb.add_project("memory", "记忆")
        self.sb.add_project("quick-send", "快传")
        self.sb.add_session("s_new", "新会话", r"D:\work\a")
        self.sb.add_session("s_old", "老会话", r"D:\work\b")
        self.sb.__enter__()
        self.sb.add_assignment("s_old", "memory", status="suggested", confidence=0.4,
                               reason="rules 命中", source="rules")
        resp = apply_mod.post_ai_preview({"preview": preview(
            item("s_new", "quick-send", 0.9), item("s_old", "quick-send", 0.9))})
        self.batch_id = body(resp)["data"]["batch_id"]

    def tearDown(self):
        self.sb.__exit__()

    def _confirm(self, selected=("s_new", "s_old")):
        return body(apply_mod.post_ai_confirm({"batch_id": self.batch_id, "confirm": True,
                                               "selected": list(selected)}))

    def test_undo_requires_confirm_then_restores_every_field(self):
        data = self._confirm()
        self.assertTrue(data["ok"], data)
        op_id = data["data"]["op_id"]
        resp = undo_mod.post_ai_undo({"batch_id": self.batch_id})
        self.assertEqual(status_of(resp), 400)
        self.assertEqual(body(resp)["error"]["code"], "confirmation_required")
        self.assertEqual(self.sb.assignment("s_new")["project_slug"], "quick-send")
        out = body(undo_mod.post_ai_undo({"batch_id": self.batch_id, "confirm": True}))
        self.assertTrue(out["ok"], out)
        self.assertIsNone(self.sb.assignment("s_new"))          # 原本不存在 ⇒ 恢复为不存在
        row = self.sb.assignment("s_old")                       # 原本存在 ⇒ 逐字段还原
        self.assertEqual(row["project_slug"], "memory")
        self.assertEqual(row["status"], "suggested")
        self.assertEqual(row["confidence"], 0.4)
        self.assertEqual(row["reason"], "rules 命中")
        self.assertEqual(row["source"], "rules")
        self.assertIsNone(row["batch_id"])
        self.assertIsNone(row["applied_at"])
        self.assertEqual(self.sb.cwd_of("s_new"), r"D:\work\a")
        self.assertEqual(self.sb.cwd_of("s_old"), r"D:\work\b")
        header = [o for o in self.sb.console_ops(op_id) if o["kind"] == "header"][0]
        self.assertEqual(header["undone"], 1)
        self.assertEqual(self.sb.batch_row(self.batch_id)["status"], "undone")
        self.assertEqual(status_of(undo_mod.post_ai_undo({"batch_id": self.batch_id,
                                                          "confirm": True})), 409)

    def test_undo_pauses_on_later_modification(self):
        data = self._confirm(selected=("s_new",))
        op_id = data["data"]["op_id"]
        self.sb.add_assignment("s_new", "memory", status="applied", confidence=0.95,
                               reason="人工改", source="panel")
        resp = undo_mod.post_ai_undo({"op_id": op_id, "confirm": True})
        out = body(resp)
        self.assertEqual(status_of(resp), 409)
        self.assertEqual(out["error"]["code"], "undo_paused")
        kept = self.sb.assignment("s_new")
        self.assertEqual(kept["project_slug"], "memory")   # 不覆盖后续修改
        self.assertEqual(kept["reason"], "人工改")
        header = [o for o in self.sb.console_ops(op_id) if o["kind"] == "header"][0]
        self.assertEqual(header["undone"], 0)

    def _new_project_batch(self):
        for index in range(1, 4):
            self.sb.add_session("n%d" % index, "主题 %d" % index, r"D:\work\n%d" % index)
        resp = apply_mod.post_ai_preview({"preview": preview(item(
            "n1", None, 0.9, new_project={"slug": "brand-new", "name": "全新"},
            support=["n1", "n2", "n3"]))})
        batch_id = body(resp)["data"]["batch_id"]
        body(apply_mod.post_ai_project({"batch_id": batch_id, "slug": "brand-new", "confirm": True}))
        conf = body(apply_mod.post_ai_confirm({"batch_id": batch_id, "confirm": True,
                                               "selected": ["n1"]}))
        self.assertTrue(conf["ok"], conf)
        return batch_id, self.sb.home / "projects" / "brand-new"

    def test_undo_restores_null_cwd_to_null(self):
        # 生产里真有 cwd=NULL 的会话：写进去之后撤销必须还原成 NULL（不是空串、不是旧值）
        self.sb.add_session("s_null", "无 cwd 会话", None)
        resp = apply_mod.post_ai_preview({"preview": preview(item("s_null", "memory", 0.9))})
        batch_id = body(resp)["data"]["batch_id"]
        conf = body(apply_mod.post_ai_confirm({"batch_id": batch_id, "confirm": True,
                                              "selected": ["s_null"]}))
        self.assertTrue(conf["ok"], conf)
        self.assertEqual(self.sb.cwd_of("s_null"), str(self.sb.home / "projects" / "memory"))
        out = body(undo_mod.post_ai_undo({"batch_id": batch_id, "confirm": True}))
        self.assertTrue(out["ok"], out)
        self.assertIsNone(self.sb.cwd_of("s_null"))
        self.assertIsNone(self.sb.assignment("s_null"))

    def test_undo_cleans_created_project_when_empty(self):
        batch_id, target = self._new_project_batch()
        self.assertTrue(target.is_dir())
        out = body(undo_mod.post_ai_undo({"batch_id": batch_id, "confirm": True}))
        self.assertTrue(out["ok"], out)
        self.assertIsNone(self.sb.project_row("brand-new"))
        self.assertFalse(target.exists())
        self.assertTrue(out["data"]["project_cleanup"][0]["cleaned"])

    def test_undo_keeps_created_project_when_dir_has_content(self):
        batch_id, target = self._new_project_batch()
        (target / "later.txt").write_text("后续内容", encoding="utf-8")
        out = body(undo_mod.post_ai_undo({"batch_id": batch_id, "confirm": True}))
        self.assertTrue(out["ok"], out)
        self.assertIsNotNone(self.sb.project_row("brand-new"))
        self.assertTrue((target / "later.txt").exists())
        self.assertFalse(out["data"]["project_cleanup"][0]["cleaned"])
        self.assertIn("目录未删除", out["data"]["project_cleanup"][0]["why"])


class TestEndToEnd(unittest.TestCase):
    """10 条混合会话全链路：预览 → 建新项目 → 确认 → 逐条回读 → 整批撤销。"""

    def setUp(self):
        self.sb = Sandbox()
        self.sb.add_project("memory", "记忆")
        self.sb.add_project("quick-send", "快传")
        self.home = self.sb.home
        for index in range(1, 11):
            self.sb.add_session("m%d" % index, "混合会话 %d" % index, r"D:\work\m%d" % index)
        self.sb.__enter__()

    def tearDown(self):
        self.sb.__exit__()

    def test_mixed_batch_lifecycle(self):
        sb = self.sb
        sb.add_assignment("m10", "quick-send", status="applied")  # 原 applied，必须排除
        resp = apply_mod.post_ai_preview({"preview": preview(
            item("m1", "memory", 0.90), item("m2", "quick-send", 0.85),
            item("m3", "memory", 0.70), item("m4", "quick-send", 0.55),
            item("m5", "memory", 0.20), item("m6", "memory", 0.10),
            item("m7", None, 0.90, new_project={"slug": "brand-new", "name": "全新"},
                 support=["m7", "m8", "m9"]),
            item("m8", None, 0.82, new_project={"slug": "brand-new", "name": "全新"},
                 support=["m7", "m8", "m9"]),
            item("m10", "memory", 0.99))})
        data = body(resp)
        self.assertTrue(data["ok"], data)
        batch_id = data["data"]["batch_id"]
        summary = data["data"]["summary"]
        self.assertEqual(summary["total"], 8)          # m10 被硬性排除
        self.assertEqual(summary["excluded"], 1)
        self.assertEqual((summary["apply"], summary["suggest"], summary["unjudged"]), (4, 2, 2))

        created = body(apply_mod.post_ai_project({"batch_id": batch_id, "slug": "brand-new",
                                                 "confirm": True}))
        self.assertTrue(created["ok"], created)
        selected = ["m1", "m2", "m3", "m4", "m7", "m8"]
        conf = body(apply_mod.post_ai_confirm({"batch_id": batch_id, "confirm": True,
                                              "selected": selected}))
        self.assertTrue(conf["ok"], conf)
        self.assertEqual(conf["data"]["written"], 6)
        self.assertEqual(conf["data"]["status"], "done")

        rows = {sid: sb.assignment(sid) for sid in selected}
        self.assertEqual(rows["m1"]["status"], "applied")
        self.assertEqual(rows["m2"]["status"], "applied")
        self.assertEqual(rows["m3"]["status"], "suggested")
        self.assertEqual(rows["m4"]["status"], "suggested")
        self.assertEqual(rows["m7"]["status"], "applied")
        self.assertEqual(rows["m8"]["project_slug"], "brand-new")
        self.assertEqual(sb.cwd_of("m1"), str(self.home / "projects" / "memory"))
        self.assertEqual(sb.cwd_of("m3"), r"D:\work\m3")      # suggest 不动 cwd
        self.assertEqual(Path(sb.cwd_of("m7")).name, "brand-new")
        self.assertIsNone(sb.assignment("m5"))                 # 低置信度一条没写
        self.assertIsNone(sb.assignment("m6"))
        self.assertEqual(sb.assignment("m10")["batch_id"], None)  # 原 applied 原样

        readback = body(apply_mod.get_ai_readback(batch_id))["data"]
        self.assertEqual(readback["bad"], [])
        self.assertEqual(readback["checked"], 8)

        out = body(undo_mod.post_ai_undo({"batch_id": batch_id, "confirm": True}))
        self.assertTrue(out["ok"], out)
        for sid in selected:
            self.assertIsNone(sb.assignment(sid))
        self.assertEqual(sb.cwd_of("m1"), r"D:\work\m1")
        self.assertEqual(sb.cwd_of("m7"), r"D:\work\m7")
        self.assertIsNone(sb.project_row("brand-new"))
        self.assertEqual(sb.assignment("m10")["status"], "applied")


if __name__ == "__main__":
    unittest.main(verbosity=2)

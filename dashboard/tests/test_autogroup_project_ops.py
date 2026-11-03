# -*- coding: utf-8 -*-
"""t_53b48743 B/D 段：项目实体 + 五个操作契约 + 看板硬约束。

验收对应：
* B 新建 / 改名显示名 / 改 slug / 改目录 / 解散 / 删除
* C 无 confirm 返回 400；预览零写入
* D 看板任务会话无法被归入普通项目（接口层拒绝 + 断言）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

DASH = Path(__file__).resolve().parents[1]
for extra in (str(DASH), str(Path(__file__).resolve().parent)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import unittest  # noqa: E402

from project_sandbox import ProjectSandbox, body, status_of  # noqa: E402

import api_autogroup as base  # noqa: E402
import api_autogroup_panel as panel  # noqa: E402
import api_autogroup_projects as api  # noqa: E402
import autogroup_align as align  # noqa: E402
import autogroup_project_ops as ops  # noqa: E402
import autogroup_projects as ent  # noqa: E402


class EntityListTest(unittest.TestCase):
    """B：项目清单要能回答「有没有目录 / 目录里有东西 / 多少会话」。"""

    def test_entity_answers_dir_and_session_questions(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            (a / "readme.md").write_text("x", encoding="utf-8")
            b = sb.add_project("beta", "Beta", create_dir=False)
            sb.add_session("s1", "x", cwd=str(a))
            sb.add_assignment("s1", "alpha")
            items = {i["slug"]: i for i in ent.list_entities(sb.home)}
            self.assertTrue(items["alpha"]["dir"]["exists"])
            self.assertFalse(items["alpha"]["dir"]["empty"])
            self.assertEqual(items["alpha"]["dir"]["entries"], 1)
            self.assertEqual(items["alpha"]["assignments"]["applied"], 1)
            self.assertFalse(items["beta"]["dir"]["exists"], "目录不在要如实说，不填 0")
            self.assertEqual(items["beta"]["dir"]["entries"], 0)

    def test_entity_path_matches_slug_flag(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            weird = sb.projects_root / "not-alpha"
            sb.add_project("beta", "Beta", path=str(weird))
            items = {i["slug"]: i for i in ent.list_entities(sb.home)}
            self.assertTrue(items["alpha"]["path_matches_slug"])
            self.assertFalse(items["beta"]["path_matches_slug"])

    def test_entities_route_is_read_only(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            before = sb.snapshot()
            body(api.get_entities())
            body(api.get_entity("alpha"))
            self.assertEqual(before, sb.snapshot(), "读实体不许建 project_attrs 表")

    def test_unknown_entity_404(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            self.assertEqual(status_of(api.get_entity("nope")), 404)


class CreateProjectTest(unittest.TestCase):
    def test_create_writes_row_dir_and_attrs(self):
        with ProjectSandbox() as sb:
            resp = body(api.post_project_create({"slug": "delta", "name": "Delta",
                                                 "created_by": "manual", "confirm": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertIsNotNone(sb.project_row("delta"))
            self.assertTrue((sb.projects_root / "delta").is_dir())
            attrs = sb.attrs_row("delta")
            self.assertEqual(attrs["created_by"], "manual")
            self.assertEqual(attrs["status"], "active")
            self.assertIsNotNone(resp["data"]["op_id"], "要落台账")

    def test_create_needs_confirm(self):
        with ProjectSandbox() as sb:
            before = sb.snapshot()
            resp = api.post_project_create({"slug": "delta", "name": "Delta"})
            self.assertEqual(status_of(resp), 400)
            payload = body(resp)
            self.assertEqual(payload["error"]["code"], "confirmation_required")
            self.assertIn("preview", payload["error"])
            self.assertEqual(before, sb.snapshot())

    def test_create_refuses_nonempty_existing_dir_without_flag(self):
        with ProjectSandbox() as sb:
            target = sb.projects_root / "delta"
            target.mkdir(parents=True)
            (target / "keep.txt").write_text("precious", encoding="utf-8")
            resp = api.post_project_create({"slug": "delta", "name": "Delta", "confirm": True})
            self.assertEqual(status_of(resp), 409)
            self.assertEqual(body(resp)["error"]["code"], "existing_dir_confirmation_required")
            self.assertIsNone(sb.project_row("delta"), "拒绝时不许登记项目")
            self.assertTrue((target / "keep.txt").is_file(), "不许动已有文件")

    def test_create_allows_nonempty_dir_with_explicit_flag(self):
        with ProjectSandbox() as sb:
            target = sb.projects_root / "delta"
            target.mkdir(parents=True)
            (target / "keep.txt").write_text("precious", encoding="utf-8")
            resp = body(api.post_project_create({"slug": "delta", "name": "Delta",
                                                 "confirm": True, "allow_existing_dir": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertIsNotNone(sb.project_row("delta"))
            self.assertTrue((target / "keep.txt").is_file(), "复用也不能动已有文件")

    def test_create_reuses_empty_existing_dir(self):
        with ProjectSandbox() as sb:
            (sb.projects_root / "delta").mkdir(parents=True)
            resp = body(api.post_project_create({"slug": "delta", "name": "Delta",
                                                 "confirm": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertFalse(resp["data"]["dir_created"])

    def test_create_rejects_duplicate_slug_and_bad_slug(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            dup = api.post_project_create({"slug": "alpha", "name": "X", "confirm": True})
            self.assertEqual(status_of(dup), 400)
            bad = api.post_project_create({"slug": "Bad Slug!", "name": "X", "confirm": True})
            self.assertEqual(status_of(bad), 400)

    def test_create_is_referencable_by_assignment(self):
        with ProjectSandbox() as sb:
            body(api.post_project_create({"slug": "delta", "name": "Delta", "confirm": True}))
            sb.add_session("s1", "x", cwd=str(sb.root))
            resp = body(panel.post_assign({"slug": "delta", "session_ids": ["s1"],
                                           "confirm": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(sb.assignment("s1")["project_slug"], "delta")
            self.assertEqual(sb.cwd_of("s1"), str(sb.projects_root / "delta"),
                             "写归属默认必须同步 cwd")

    def test_legacy_panel_create_uses_real_project_root(self):
        """老 POST /autogroup/projects 也必须落在既有项目的根里，不另造一套目录根。"""
        with ProjectSandbox() as sb:
            # 先有一个「既有项目」，其目录在 home/projects 之外（模拟生产的兄弟目录根）
            real_root = sb.root / "real-projects"
            real_root.mkdir(parents=True, exist_ok=True)
            sb.add_project("existing", "既有", path=str(real_root / "existing"))
            resp = body(panel.post_project({"name": "新建的", "slug": "fresh"}))
            self.assertTrue(resp["ok"], resp)
            row = sb.project_row("fresh")
            self.assertIsNotNone(row)
            self.assertEqual(Path(row["primary_path"]).parent, real_root,
                             "新项目必须与既有项目同根，不能建到 home/projects")
            self.assertTrue(Path(row["primary_path"]).is_dir())

    def test_invalid_created_by_rejected(self):
        with ProjectSandbox() as sb:
            resp = api.post_project_create({"slug": "delta", "name": "D", "confirm": True,
                                            "created_by": "hacker"})
            self.assertEqual(status_of(resp), 400)

    def test_ai_new_project_target_uses_real_project_root(self):
        """AI 新建项目也必须落在既有项目的根里（不另造 home/projects 第二套根）。"""
        import autogroup_ai_projects as ai_projects
        with ProjectSandbox() as sb:
            real_root = sb.root / "real-projects"
            real_root.mkdir(parents=True, exist_ok=True)
            sb.add_project("existing", "既有", path=str(real_root / "existing"))
            target = ai_projects.resolve_target(sb.home, "fresh-topic")
            self.assertEqual(target.parent.resolve(), real_root.resolve(),
                             "AI 新项目目录必须与既有项目同根")
            created = ai_projects.create_project(
                sb.home, slug="fresh-topic", name="新主题",
                support_session_ids=["s1", "s2", "s3"], selected_session_ids=["s1"],
                existing_slugs={"existing": {}})
            self.assertEqual(Path(created["path"]).parent.resolve(), real_root.resolve())
            self.assertTrue(Path(created["path"]).is_dir())
            self.assertFalse((sb.home / "projects" / "fresh-topic").exists(),
                             "不许在 home/projects 下造第二套目录根")


class RenameTest(unittest.TestCase):
    def test_rename_display_name_keeps_path_and_assignments(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            sb.add_session("s1", "x", cwd=str(a))
            sb.add_assignment("s1", "alpha")
            before_assign = sb.assignment("s1")
            resp = body(api.patch_project_name({"slug": "alpha", "name": "阿尔法", "confirm": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(sb.project_row("alpha")["name"], "阿尔法")
            self.assertEqual(sb.project_row("alpha")["primary_path"], str(a), "目录路径不许变")
            self.assertEqual(sb.assignment("s1"), before_assign, "归属不许变")
            self.assertEqual(sb.cwd_of("s1"), str(a), "cwd 不许变")

    def test_rename_name_needs_confirm(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            before = sb.snapshot()
            resp = api.patch_project_name({"slug": "alpha", "name": "X"})
            self.assertEqual(status_of(resp), 400)
            self.assertEqual(before, sb.snapshot())

    def test_rename_name_is_undoable(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            resp = body(api.patch_project_name({"slug": "alpha", "name": "阿尔法", "confirm": True}))
            undo = body(base.post_undo({"op_id": resp["data"]["op_id"]}))
            self.assertTrue(undo["ok"], undo)
            self.assertEqual(sb.project_row("alpha")["name"], "Alpha")

    def test_rename_slug_migrates_dir_and_assignments(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            (a / "data.txt").write_text("hello", encoding="utf-8")
            sb.add_session("s1", "x", cwd=str(a))
            sb.add_assignment("s1", "alpha")
            resp = body(api.post_project_slug({"slug": "alpha", "new_slug": "alpha2",
                                               "confirm": True, "confirm_slug": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertIsNone(sb.project_row("alpha"), "旧 slug 必须消失")
            row = sb.project_row("alpha2")
            self.assertIsNotNone(row)
            self.assertTrue(Path(row["primary_path"]).is_dir(), "目录要迁到新名字")
            self.assertTrue((Path(row["primary_path"]) / "data.txt").is_file(), "内容要跟着走")
            self.assertFalse(a.exists(), "旧目录不该还在")
            self.assertEqual(sb.assignment("s1")["project_slug"], "alpha2", "归属要跟着改")

    def test_rename_slug_migrates_rules_and_ledger_without_crashing(self):
        """t_e5adf456 缺陷 1 的另一处：``rename_slug`` 迁 ``rules.json`` 时也用了 ``json.loads``。

        文件顶部原来没有 ``import json`` ⇒ 只要 ``<home>/auto-group/rules.json`` 存在，改名必崩
        （而项目/目录/归属已经改完），用户看到「报错但改了一半」。
        """
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            group = sb.home / "auto-group"
            group.mkdir(parents=True, exist_ok=True)
            rules = group / "rules.json"
            rules.write_text(json.dumps({"alpha": ["关键词一", "关键词二"], "other": ["x"]},
                                         ensure_ascii=False), encoding="utf-8")
            resp = api.post_project_slug({"slug": "alpha", "new_slug": "alpha2",
                                          "confirm": True, "confirm_slug": True})
            self.assertEqual(status_of(resp), 200, body(resp))
            self.assertTrue(body(resp)["ok"], body(resp))
            self.assertTrue(body(resp)["data"]["migrated"]["rules"], "规则迁移要如实记账")
            self.assertIsNone(sb.project_row("alpha"))
            self.assertIsNotNone(sb.project_row("alpha2"))
            data = json.loads(rules.read_text(encoding="utf-8"))
            self.assertEqual(sorted(data.get("alpha2") or []), ["关键词一", "关键词二"],
                             "规则要跟着迁到新 slug")
            self.assertNotIn("alpha", data)
            self.assertEqual(data.get("other"), ["x"], "别的项目的规则一条都不许动")

    def test_rename_slug_needs_double_confirmation(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            only_confirm = api.post_project_slug({"slug": "alpha", "new_slug": "alpha2",
                                                  "confirm": True})
            self.assertEqual(status_of(only_confirm), 400)
            self.assertEqual(body(only_confirm)["error"]["code"], "confirmation_required")
            self.assertIsNotNone(sb.project_row("alpha"), "拒绝时不许动")

    def test_rename_slug_refuses_existing_nonempty_target(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            other = sb.projects_root / "alpha2"
            other.mkdir(parents=True)
            (other / "x.txt").write_text("x", encoding="utf-8")
            resp = api.post_project_slug({"slug": "alpha", "new_slug": "alpha2",
                                          "confirm": True, "confirm_slug": True})
            self.assertEqual(status_of(resp), 409)
            self.assertIsNotNone(sb.project_row("alpha"))

    def test_rename_slug_is_undoable(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            sb.add_session("s1", "x", cwd=str(a))
            sb.add_assignment("s1", "alpha")
            resp = body(api.post_project_slug({"slug": "alpha", "new_slug": "alpha2",
                                               "confirm": True, "confirm_slug": True}))
            undo = body(base.post_undo({"op_id": resp["data"]["op_id"]}))
            self.assertTrue(undo["ok"], undo)
            self.assertIsNotNone(sb.project_row("alpha"))
            self.assertIsNone(sb.project_row("alpha2"))
            self.assertEqual(sb.assignment("s1")["project_slug"], "alpha")
            self.assertTrue(a.is_dir(), "目录要迁回原名")


class ChangePathTest(unittest.TestCase):
    def test_rebind_moves_no_files(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            (a / "keep.txt").write_text("x", encoding="utf-8")
            new = sb.projects_root / "alpha-new"
            resp = body(api.post_project_path({"slug": "alpha", "new_path": str(new),
                                               "mode": "rebind", "confirm": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(sb.project_row("alpha")["primary_path"], str(new))
            self.assertTrue(new.is_dir(), "rebind 允许建新空目录")
            self.assertTrue((a / "keep.txt").is_file(), "旧目录内容原地保留")

    def test_migrate_moves_contents(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            (a / "keep.txt").write_text("x", encoding="utf-8")
            new = sb.projects_root / "alpha-new"
            resp = body(api.post_project_path({"slug": "alpha", "new_path": str(new),
                                               "mode": "migrate", "confirm": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertTrue((new / "keep.txt").is_file(), "内容要搬过去")
            self.assertFalse(a.exists(), "旧目录不该还在")

    def test_migrate_refuses_nonempty_target(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            (a / "keep.txt").write_text("x", encoding="utf-8")
            new = sb.projects_root / "alpha-new"
            new.mkdir(parents=True)
            (new / "other.txt").write_text("y", encoding="utf-8")
            resp = api.post_project_path({"slug": "alpha", "new_path": str(new),
                                          "mode": "migrate", "confirm": True})
            self.assertEqual(status_of(resp), 409)
            self.assertTrue((a / "keep.txt").is_file(), "拒绝时不许动旧目录")

    def test_change_path_needs_confirm(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            before = sb.snapshot()
            resp = api.post_project_path({"slug": "alpha",
                                          "new_path": str(sb.projects_root / "n"),
                                          "mode": "rebind"})
            self.assertEqual(status_of(resp), 400)
            self.assertEqual(before, sb.snapshot())

    def test_change_path_rejects_escape(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            outside = sb.root / "outside"
            resp = api.post_project_path({"slug": "alpha", "new_path": str(outside),
                                          "mode": "rebind", "confirm": True})
            self.assertEqual(status_of(resp), 409)

    def test_change_path_is_undoable(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            (a / "keep.txt").write_text("x", encoding="utf-8")
            new = sb.projects_root / "alpha-new"
            resp = body(api.post_project_path({"slug": "alpha", "new_path": str(new),
                                               "mode": "migrate", "confirm": True}))
            undo = body(base.post_undo({"op_id": resp["data"]["op_id"]}))
            self.assertTrue(undo["ok"], undo)
            self.assertEqual(sb.project_row("alpha")["primary_path"], str(a))
            self.assertTrue((a / "keep.txt").is_file(), "内容要搬回来")


class DissolveTest(unittest.TestCase):
    def test_dissolve_requires_explicit_target(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            resp = api.post_project_dissolve({"slug": "alpha", "confirm": True})
            self.assertEqual(status_of(resp), 400)
            self.assertEqual(body(resp)["error"]["code"], "dissolve_target_required")

    def test_dissolve_to_ungrouped_leaves_no_orphans(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            sb.add_session("s1", "x", cwd=str(a))
            sb.add_session("s2", "y", cwd=str(a))
            sb.add_assignment("s1", "alpha")
            sb.add_assignment("s2", "alpha")
            resp = body(api.post_project_dissolve({"slug": "alpha", "to": "ungrouped",
                                                   "confirm": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(sorted(resp["data"]["ungrouped"]), ["s1", "s2"])
            self.assertEqual([r for r in sb.assignments() if r["project_slug"] == "alpha"], [],
                             "无孤儿归属是硬要求")
            self.assertEqual(sb.attrs_row("alpha")["status"], "dissolved")
            self.assertEqual(sb.cwd_of("s1"), str(a), "退回未分组不动会话 cwd")

    def test_dissolve_to_project_moves_assignments_and_cwd(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            b = sb.add_project("beta", "Beta")
            sb.add_session("s1", "x", cwd=str(a))
            sb.add_assignment("s1", "alpha")
            resp = body(api.post_project_dissolve({"slug": "alpha", "to": "move",
                                                   "target_slug": "beta", "confirm": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(sb.assignment("s1")["project_slug"], "beta")
            self.assertEqual(sb.cwd_of("s1"), str(b), "迁移项目时 cwd 必须跟着走")
            self.assertEqual([r for r in sb.assignments() if r["project_slug"] == "alpha"], [])
            self.assertEqual(align.audit(sb.home)["counts"]["inconsistent"], 0)

    def test_dissolve_move_needs_valid_target(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            resp = api.post_project_dissolve({"slug": "alpha", "to": "move",
                                              "target_slug": "ghost", "confirm": True})
            self.assertEqual(status_of(resp), 409)
            missing = api.post_project_dissolve({"slug": "alpha", "to": "move", "confirm": True})
            self.assertEqual(status_of(missing), 409)

    def test_dissolve_needs_confirm(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            before = sb.snapshot()
            resp = api.post_project_dissolve({"slug": "alpha", "to": "ungrouped"})
            self.assertEqual(status_of(resp), 400)
            self.assertEqual(before, sb.snapshot())

    def test_dissolve_ungrouped_is_reported_as_unrecoverable_on_undo(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            sb.add_session("s1", "x", cwd=str(a))
            sb.add_assignment("s1", "alpha")
            resp = body(api.post_project_dissolve({"slug": "alpha", "to": "ungrouped",
                                                   "confirm": True}))
            undo = body(base.post_undo({"op_id": resp["data"]["op_id"]}))
            self.assertTrue(undo["ok"], undo)
            self.assertEqual(sb.attrs_row("alpha")["status"], "active", "项目状态要还原")
            applied = undo["data"]["header_action"]["applied"]
            self.assertEqual(applied["unrecoverable_ungrouped"], ["s1"],
                             "删掉的归属行必须如实报「无法自动重建」，不假装还原")

    def test_dissolve_can_delete_empty_dir(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            sb.add_session("s1", "x", cwd=str(a))
            sb.add_assignment("s1", "alpha")
            resp = body(api.post_project_dissolve({"slug": "alpha", "to": "ungrouped",
                                                   "dir_action": "delete_empty", "confirm": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertTrue(resp["data"]["removed_dir"])
            self.assertFalse(a.exists())


class DeleteTest(unittest.TestCase):
    def test_delete_refuses_when_assignments_exist(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            sb.add_session("s1", "x", cwd=str(a))
            sb.add_assignment("s1", "alpha")
            resp = api.post_project_delete({"slug": "alpha", "confirm": True})
            self.assertEqual(status_of(resp), 409)
            self.assertIsNotNone(sb.project_row("alpha"), "有归属就不许删，避免静默丢归属")

    def test_delete_keeps_dir_by_default(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            (a / "keep.txt").write_text("x", encoding="utf-8")
            resp = body(api.post_project_delete({"slug": "alpha", "confirm": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertIsNone(sb.project_row("alpha"))
            self.assertTrue((a / "keep.txt").is_file(), "默认保留目录")

    def test_delete_empty_dir_when_chosen(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            resp = body(api.post_project_delete({"slug": "alpha", "dir_action": "delete_empty",
                                                 "confirm": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertFalse(a.exists())

    def test_delete_refuses_nonempty_dir_when_delete_chosen(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            (a / "keep.txt").write_text("x", encoding="utf-8")
            resp = api.post_project_delete({"slug": "alpha", "dir_action": "delete_empty",
                                            "confirm": True})
            self.assertEqual(status_of(resp), 409)
            self.assertIsNotNone(sb.project_row("alpha"), "拒绝时不许删实体")

    def test_delete_needs_confirm(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            before = sb.snapshot()
            resp = api.post_project_delete({"slug": "alpha"})
            self.assertEqual(status_of(resp), 400)
            self.assertEqual(before, sb.snapshot())

    def test_delete_after_dissolve(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            sb.add_session("s1", "x", cwd=str(a))
            sb.add_assignment("s1", "alpha")
            body(api.post_project_dissolve({"slug": "alpha", "to": "ungrouped", "confirm": True}))
            resp = body(api.post_project_delete({"slug": "alpha", "dir_action": "delete_empty",
                                                 "confirm": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertIsNone(sb.project_row("alpha"))

    def test_delete_with_ai_ledger_returns_200_and_clears_ledger(self):
        """t_e5adf456 缺陷 1：``ai_projects.json`` 存在时删项目不许 500。

        ``delete()`` 删完三行后会读台账把该 slug 的条目摘掉；原来文件顶部漏了 ``import json``，
        这条路径必抛 ``NameError``（HTTP 500），而项目其实已经删掉了 —— 用户看到「报错但删了」。
        """
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            sb.add_project("beta", "Beta")
            ledger = sb.home / "auto-group" / "ai_projects.json"
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text(json.dumps([{"slug": "alpha", "name": "Alpha", "created_by": "ai"},
                                          {"slug": "beta", "name": "Beta", "created_by": "ai"}],
                                         ensure_ascii=False), encoding="utf-8")
            resp = api.post_project_delete({"slug": "alpha", "confirm": True})
            self.assertEqual(status_of(resp), 200, body(resp))
            self.assertTrue(body(resp)["ok"], body(resp))
            self.assertIsNone(sb.project_row("alpha"))
            remaining = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual([e.get("slug") for e in remaining], ["beta"],
                             "台账里只摘掉被删项目，别把别人的条目也清了")
            self.assertEqual(remaining[0].get("created_by"), "ai", "条目内容原样保留")

    def test_delete_with_unreadable_ledger_still_returns_200(self):
        """台账坏成非 JSON 时也不许崩：删项目已经落库，台账只能放弃清理。"""
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            ledger = sb.home / "auto-group" / "ai_projects.json"
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text("{not json", encoding="utf-8")
            resp = api.post_project_delete({"slug": "alpha", "confirm": True})
            self.assertEqual(status_of(resp), 200, body(resp))
            self.assertIsNone(sb.project_row("alpha"))


class KanbanHardConstraintTest(unittest.TestCase):
    """D：看板任务会话无法被归入普通项目（接口层拒绝 + 断言）。"""

    def test_assign_rejects_kanban_session(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            sb.add_session("kb1", "看板任务", cwd=str(a), source="kanban")
            before = sb.snapshot()
            resp = panel.post_assign({"slug": "alpha", "session_ids": ["kb1"], "confirm": True})
            self.assertEqual(status_of(resp), 400)
            payload = body(resp)
            self.assertEqual(payload["error"]["code"], "kanban_excluded")
            self.assertEqual(payload["error"]["blocked_session_ids"], ["kb1"])
            self.assertEqual(before, sb.snapshot(), "拒绝时零写入")
            self.assertIsNone(sb.assignment("kb1"))

    def test_assign_rejects_whole_batch_if_any_kanban(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            sb.add_session("ok1", "普通会话", cwd=str(a), source="desktop")
            sb.add_session("kb1", "看板任务", cwd=str(a), source="kanban")
            resp = panel.post_assign({"slug": "alpha", "session_ids": ["ok1", "kb1"],
                                      "confirm": True})
            self.assertEqual(status_of(resp), 400)
            self.assertIsNone(sb.assignment("ok1"), "整批拒绝，不做部分写入")
            self.assertIsNone(sb.assignment("kb1"))

    def test_switch_rejects_kanban_session(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            sb.add_session("kb1", "看板任务", cwd=str(a), source="kanban")
            resp = panel.post_switch({"session_id": "kb1", "slug": "alpha"})
            self.assertEqual(status_of(resp), 400)
            self.assertEqual(body(resp)["error"]["code"], "kanban_excluded")
            self.assertIsNone(sb.assignment("kb1"))

    def test_preview_marks_kanban_before_writing(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            sb.add_session("kb1", "看板任务", cwd=str(a), source="kanban")
            resp = panel.post_assign({"slug": "alpha", "session_ids": ["kb1"]})
            self.assertEqual(status_of(resp), 400)
            self.assertEqual(body(resp)["error"]["code"], "kanban_excluded")

    def test_ordinary_session_still_assignable(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            sb.add_session("ok1", "普通会话", cwd=str(sb.root), source="desktop")
            resp = body(panel.post_assign({"slug": "alpha", "session_ids": ["ok1"],
                                           "confirm": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(sb.assignment("ok1")["project_slug"], "alpha")
            self.assertEqual(sb.cwd_of("ok1"), str(a), "写归属必须同时写 cwd")


class AssignSyncsCwdTest(unittest.TestCase):
    """第 2 段核心：写归属 = 写 cwd。"""

    def test_assign_syncs_cwd_by_default(self):
        with ProjectSandbox() as sb:
            a = sb.add_project("alpha", "Alpha")
            sb.add_session("s1", "x", cwd=str(sb.root))
            body(panel.post_assign({"slug": "alpha", "session_ids": ["s1"], "confirm": True}))
            self.assertEqual(sb.cwd_of("s1"), str(a))
            self.assertEqual(align.audit(sb.home)["counts"]["inconsistent"], 0)

    def test_assign_refuses_when_project_dir_missing(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha", create_dir=False)
            sb.add_session("s1", "x", cwd=str(sb.root))
            resp = panel.post_assign({"slug": "alpha", "session_ids": ["s1"], "confirm": True})
            self.assertEqual(status_of(resp), 400)
            self.assertEqual(body(resp)["error"]["code"], "dir_missing")
            self.assertIsNone(sb.assignment("s1"), "目录缺失时归属不落")
            self.assertFalse((sb.projects_root / "alpha").exists(), "不许静默建空目录")

    def test_assign_undo_restores_cwd(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            sb.add_session("s1", "x", cwd=str(sb.root))
            resp = body(panel.post_assign({"slug": "alpha", "session_ids": ["s1"],
                                           "confirm": True}))
            undo = body(base.post_undo({"op_id": resp["data"]["op_id"]}))
            self.assertTrue(undo["ok"], undo)
            self.assertEqual(sb.cwd_of("s1"), str(sb.root))


if __name__ == "__main__":
    unittest.main(verbosity=2)

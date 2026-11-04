# -*- coding: utf-8 -*-
"""t_53b48743 B 段补：存量项目属性行回填（created_by=imported，可撤销）。"""
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
import autogroup_projects as ent  # noqa: E402


@unittest.skip(
    "t_ac537221：POST /autogroup/project/attrs/backfill 路由已下线（属性回填并入项目创建/导入路径）；"
    "本文件保留作历史契约参考，不再对当前代码执行。")
class AttrsBackfillTest(unittest.TestCase):
    """存量 17 个项目没有 project_attrs 行 ⇒ 必须显式确认回填，且可撤销。"""

    def test_preview_lists_missing_and_writes_nothing(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            sb.add_project("beta", "Beta")
            before = sb.snapshot()
            resp = body(api.get_attrs_backfill())
            self.assertTrue(resp["ok"], resp)
            preview = resp["data"]["preview"]
            self.assertEqual(sorted(preview["missing"]), ["alpha", "beta"])
            self.assertEqual(preview["count"], 2)
            self.assertEqual(preview["would_write"]["created_by"], "imported")
            self.assertEqual(before, sb.snapshot(), "预览零写入（连 project_attrs 表都不建）")

    def test_backfill_needs_confirm(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            before = sb.snapshot()
            resp = api.post_attrs_backfill({})
            self.assertEqual(status_of(resp), 400)
            self.assertEqual(body(resp)["error"]["code"], "confirmation_required")
            self.assertEqual(before, sb.snapshot())

    def test_backfill_writes_imported_and_leaves_existing_rows_alone(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            sb.add_project("beta", "Beta")
            # beta 已经有属性行（AI 建的），回填不许覆盖它
            sb.set_attrs("beta", created_by="ai", status="active")
            resp = body(api.post_attrs_backfill({"confirm": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(resp["data"]["written"], ["alpha"])
            self.assertEqual(sb.attrs_row("alpha")["created_by"], "imported")
            self.assertEqual(sb.attrs_row("beta")["created_by"], "ai", "已有属性行不许被覆盖")
            items = {i["slug"]: i for i in ent.list_entities(sb.home)}
            self.assertTrue(items["alpha"]["attrs_present"])
            self.assertEqual(items["alpha"]["created_by"], "imported")
            self.assertIsNotNone(resp["data"]["op_id"], "要落台账")

    def test_backfill_is_undoable(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            sb.add_project("beta", "Beta")
            sb.set_attrs("beta", created_by="ai", status="active")
            done = body(api.post_attrs_backfill({"confirm": True}))
            undo = body(base.post_undo({"op_id": done["data"]["op_id"]}))
            self.assertTrue(undo["ok"], undo)
            self.assertIsNone(sb.attrs_row("alpha"), "撤销要把补出来的属性行删掉")
            self.assertEqual(sb.attrs_row("beta")["created_by"], "ai", "本来就有的一行不许被撤销动到")

    def test_backfill_noop_when_nothing_missing(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            sb.set_attrs("alpha", created_by="manual", status="active")
            resp = body(api.post_attrs_backfill({"confirm": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(resp["data"]["written"], [])
            self.assertTrue(resp["data"]["noop"])
            self.assertEqual(sb.attrs_row("alpha")["created_by"], "manual")

    def test_backfill_rejects_bad_created_by(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            resp = api.post_attrs_backfill({"confirm": True, "created_by": "hacker"})
            self.assertEqual(status_of(resp), 400)
            self.assertEqual(body(resp)["error"]["code"], "invalid_created_by")

    def test_slugs_filter_limits_scope(self):
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            sb.add_project("beta", "Beta")
            resp = body(api.post_attrs_backfill({"confirm": True, "slugs": ["beta"]}))
            self.assertEqual(resp["data"]["written"], ["beta"])
            self.assertIsNone(sb.attrs_row("alpha"), "未选中的项目零变更")


if __name__ == "__main__":
    unittest.main(verbosity=2)

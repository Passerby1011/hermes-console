# -*- coding: utf-8 -*-
"""t_5e74252e：两种分类模式（A 只归类未分组 / B 全量重建体系）+ 预览→确认→应用 + 整批撤销。

验收口径（对应卡面 10 条）：

1. 模式 A 只碰未分组：应用后已分组会话的归属行**逐字段**未变
2. 模式 A 候选集 = 可见且无 applied 行；看板会话 0 条
3. 模式 B 全量：候选包含已分组会话；输出含新旧映射 diff
4. ``/plan`` 零写入：三库哈希不变、不建快照、不建目录
5. plan_id 过期 / 数据漂移 ⇒ 409，不写入
6. 缺 confirm ⇒ 400 ``confirmation_required``
7. 应用后逐条回读，不符 ⇒ ``verify_mismatch`` + 自动回滚
8. 整批撤销后三库哈希 == 应用前（内容哈希 + 快照字节）
9. B 模式撤销能还原被删的旧项目（名 / path / 归属行 / 文件夹行）
10. 看板会话零误动（两种模式下都不出现）
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

DASH = Path(__file__).resolve().parents[1]
for extra in (str(DASH), str(Path(__file__).resolve().parent)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from project_sandbox import ProjectSandbox, body, status_of  # noqa: E402

import api_autogroup as base  # noqa: E402
import autogroup_modes as modes  # noqa: E402


def _plan(home, **payload):
    resp = base.post_plan(payload)
    assert status_of(resp) == 200, body(resp)
    return body(resp)


def _plan_data(home, **payload):
    return _plan(home, **payload)["data"]


def _apply(home, payload):
    resp = base.post_apply(payload)
    return status_of(resp), body(resp)


def _undo(home, op_id):
    resp = base.post_undo({"op_id": op_id})
    return status_of(resp), body(resp)


def _ids(items):
    return [i["session_id"] for i in items]


def _slugs_in(db_path):
    """只读列出一个库里的项目 slug（WAL 库也能读：走 -wal 拿最新视图）。"""
    conn = sqlite3.connect("file:" + str(db_path) + "?mode=ro", uri=True)
    try:
        return sorted(str(r[0]) for r in conn.execute("SELECT slug FROM projects"))
    finally:
        conn.close()


def _seed_mode_a(sb):
    """A 模式夹具：两个现有项目 + 未分组 / 已分组 / 判不出 / 看板 四类会话。"""
    alpha = sb.add_project("alpha", "Alpha")
    beta = sb.add_project("beta", "Beta")
    sb.add_session("s_a1", "alpha 方案讨论", cwd=str(sb.root))
    sb.add_session("s_b1", "beta 构建问题", cwd=str(sb.root))
    sb.add_session("s_none", "闲聊一下", cwd=None)
    sb.add_session("s_grouped", "beta 已经归好的会话", cwd=str(alpha))
    sb.add_assignment("s_grouped", "alpha", status="applied", source="panel", confidence=0.9)
    sb.add_session("kb1", "alpha 看板任务", cwd=str(sb.root), source="kanban")
    sb.add_assignment("kb1", "alpha", status="applied")
    return alpha, beta


def _seed_mode_b(sb):
    """B 模式夹具：alpha/beta 保留，zeta 在新体系里没有会话（应被抛弃）。"""
    alpha = sb.add_project("alpha", "Alpha")
    beta = sb.add_project("beta", "Beta")
    zeta = sb.add_project("zeta", "Zeta")
    sb.set_attrs("zeta", created_by="manual")      # 真实 projects.db 有 project_attrs 表
    sb.add_session("s1", "alpha 方案讨论", cwd=str(alpha))
    sb.add_assignment("s1", "alpha", source="panel")
    sb.add_session("s2", "beta 构建问题", cwd=str(alpha))
    sb.add_assignment("s2", "alpha", source="panel")
    sb.add_session("s3", "gamma 模块设计", cwd=str(sb.home / "work" / "gamma"))
    sb.add_assignment("s3", "zeta", source="panel")
    sb.add_session("kb1", "alpha 看板任务", cwd=str(alpha), source="kanban")
    sb.add_assignment("kb1", "alpha", status="applied")
    return alpha, beta, zeta


_PLAN_API_REMOVED = (
    "t_ac537221：/autogroup/plan、/autogroup/apply_plan、/autogroup/ops 已下线；"
    "A/B 两条计划流水线并入 /autogroup/scan + /autogroup/apply。本用例保留作历史契约参考。"
)

@unittest.skip(_PLAN_API_REMOVED)
class ModeAContractTest(unittest.TestCase):
    def test_candidates_exclude_grouped_and_kanban(self):
        with ProjectSandbox() as sb:
            _seed_mode_a(sb)
            data = _plan_data(sb.home, mode=modes.MODE_UNGROUPED_ONLY)
            self.assertEqual(data["mode"], "ungrouped_only")
            self.assertEqual(sorted(data["candidates"]["session_ids"]),
                             ["s_a1", "s_b1", "s_none"])
            self.assertEqual(data["candidates"]["excluded_kanban"], ["kb1"])
            self.assertEqual(sorted(data["candidates"]["skipped_grouped"]), ["s_grouped"])
            items = {i["session_id"]: i for i in data["items"]}
            self.assertNotIn("s_grouped", items, "已分组会话不进 A 的候选")
            self.assertNotIn("kb1", items, "看板会话不进 A 的候选")
            self.assertEqual(items["s_a1"]["to_slug"], "alpha")
            self.assertEqual(items["s_b1"]["to_slug"], "beta")
            self.assertEqual(items["s_none"]["change"], "unmatched")

    def test_plan_is_zero_write(self):
        with ProjectSandbox() as sb:
            _seed_mode_a(sb)
            before = sb.snapshot()
            before_hashes = modes.db_hashes(sb.home)
            for mode in modes.MODES:
                data = _plan_data(sb.home, mode=mode)
                self.assertTrue(data["zero_write"])
                self.assertEqual(data["wrote"], [])
            self.assertEqual(before, sb.snapshot(), "预览后三库 + 目录树逐字节不变")
            self.assertEqual(before_hashes, modes.db_hashes(sb.home))
            self.assertFalse((sb.home / "auto-group" / "snapshots").exists(), "预览不许建快照")
            self.assertFalse(modes.ledger_path(sb.home).exists(), "预览不许建台账")
            self.assertFalse((sb.projects_root / "gamma").exists())

    def test_apply_only_touches_ungrouped(self):
        with ProjectSandbox() as sb:
            alpha, beta = _seed_mode_a(sb)
            grouped_before = sb.assignment("s_grouped")
            kanban_before = sb.assignment("kb1")
            data = _plan_data(sb.home, mode=modes.MODE_UNGROUPED_ONLY)
            status, resp = _apply(sb.home, {"plan_id": data["plan_id"], "confirm": True})
            self.assertEqual(status, 200, resp)
            self.assertTrue(resp["ok"])
            out = resp["data"]
            self.assertEqual(sorted(out["sessions"]), ["s_a1", "s_b1"])
            self.assertEqual(out["readback"]["mismatched"], [])
            self.assertEqual(out["readback"]["cwd_mismatched"], [])
            # 未分组的两条真的落库了
            self.assertEqual(sb.assignment("s_a1")["project_slug"], "alpha")
            self.assertEqual(sb.assignment("s_a1")["status"], "applied")
            self.assertEqual(sb.cwd_of("s_a1"), str(alpha))
            self.assertEqual(sb.assignment("s_b1")["project_slug"], "beta")
            self.assertEqual(sb.cwd_of("s_b1"), str(beta))
            # 已分组 / 看板：逐字段未变
            self.assertEqual(sb.assignment("s_grouped"), grouped_before)
            self.assertEqual(sb.cwd_of("s_grouped"), str(alpha))
            self.assertEqual(sb.assignment("kb1"), kanban_before)
            self.assertEqual(sb.cwd_of("kb1"), str(sb.root))
            # 快照按字节备份了三库
            snap = Path(out["snapshot"]["dir"])
            self.assertTrue(snap.is_dir())
            self.assertEqual(sorted(f["db"] for f in out["snapshot"]["files"]),
                             sorted(modes.TRACKED_DBS))
            for entry in out["snapshot"]["files"]:
                if entry["db"] in modes.BYTE_RESTORE_DBS:
                    self.assertTrue(Path(entry["file"]).is_file(), entry["db"])
                else:
                    self.assertIsNone(entry["file"], "活库不做字节快照")
                    self.assertEqual(entry["reason"], "live_store_row_level_only")

    def test_new_projects_need_separate_confirmation(self):
        with ProjectSandbox() as sb:
            _seed_mode_a(sb)
            sb.add_session("s_new", "闲聊二", cwd=str(sb.home / "work" / "gamma"))
            data = _plan_data(sb.home, mode=modes.MODE_UNGROUPED_ONLY)
            proposals = [p for p in data["new_projects"] if p["slug"] == "gamma"]
            self.assertEqual(len(proposals), 1)
            self.assertFalse(proposals[0]["will_create"], "A 模式新项目默认只提议，不建")
            item = next(i for i in data["items"] if i["session_id"] == "s_new")
            self.assertEqual(item["change"], "pending_new_project")
            self.assertFalse(item["will_write"])
            status, resp = _apply(sb.home, {"plan_id": data["plan_id"], "confirm": True})
            self.assertEqual(status, 200, resp)
            self.assertIsNone(sb.project_row("gamma"), "没确认就不许建项目")
            self.assertIsNone(sb.assignment("s_new"))

            # 显式确认后才建
            data2 = _plan_data(sb.home, mode=modes.MODE_UNGROUPED_ONLY,
                               accept_new_projects=True)
            proposal = next(p for p in data2["new_projects"] if p["slug"] == "gamma")
            self.assertTrue(proposal["will_create"])
            status2, resp2 = _apply(sb.home, {"plan_id": data2["plan_id"], "confirm": True})
            self.assertEqual(status2, 200, resp2)
            project = sb.project_row("gamma")
            self.assertIsNotNone(project, "确认后应建出新项目")
            self.assertEqual(sb.assignment("s_new")["project_slug"], "gamma")
            self.assertEqual(sb.cwd_of("s_new"), project["primary_path"])

    def test_sync_cwd_false_only_writes_assignments(self):
        with ProjectSandbox() as sb:
            alpha, _beta = _seed_mode_a(sb)
            data = _plan_data(sb.home, mode=modes.MODE_UNGROUPED_ONLY, sync_cwd=False)
            self.assertFalse(data["sync_cwd"])
            item = next(i for i in data["items"] if i["session_id"] == "s_a1")
            self.assertFalse(item["sync_cwd"])
            status, resp = _apply(sb.home, {"plan_id": data["plan_id"], "confirm": True})
            self.assertEqual(status, 200, resp)
            self.assertEqual(sb.assignment("s_a1")["project_slug"], "alpha")
            self.assertEqual(sb.cwd_of("s_a1"), str(sb.root), "sync_cwd=false 时 cwd 不动")

    def test_apply_requires_confirm(self):
        with ProjectSandbox() as sb:
            _seed_mode_a(sb)
            data = _plan_data(sb.home, mode=modes.MODE_UNGROUPED_ONLY)
            before = sb.snapshot()
            status, resp = _apply(sb.home, {"plan_id": data["plan_id"]})
            self.assertEqual(status, 400)
            self.assertEqual(resp["error"]["code"], "confirmation_required")
            self.assertEqual(resp["plan"]["plan_id"], data["plan_id"])
            self.assertEqual(before, sb.snapshot(), "缺 confirm 时零写入")

    def test_apply_without_plan_id_rejected(self):
        with ProjectSandbox() as sb:
            _seed_mode_a(sb)
            before = sb.snapshot()
            status, resp = _apply(sb.home, {"mode": modes.MODE_UNGROUPED_ONLY, "confirm": True})
            self.assertEqual(status, 400)
            self.assertEqual(resp["error"]["code"], "plan_id_required")
            self.assertEqual(before, sb.snapshot(), "没有 plan_id 就绝不重新算一遍再写")

    def test_plan_stale_when_new_session_appears(self):
        with ProjectSandbox() as sb:
            _seed_mode_a(sb)
            data = _plan_data(sb.home, mode=modes.MODE_UNGROUPED_ONLY)
            sb.add_session("s_late", "alpha 后来出现的会话", cwd=str(sb.root))
            before = sb.snapshot()
            status, resp = _apply(sb.home, {"plan_id": data["plan_id"], "confirm": True})
            self.assertEqual(status, 409)
            self.assertEqual(resp["error"]["code"], "plan_stale")
            self.assertEqual(resp["requires"], "plan")
            self.assertEqual(before, sb.snapshot(), "过期计划零写入")
            self.assertIsNone(sb.assignment("s_a1"))

    def test_plan_expired(self):
        with ProjectSandbox() as sb:
            _seed_mode_a(sb)
            data = _plan_data(sb.home, mode=modes.MODE_UNGROUPED_ONLY)
            with modes._PLANS_LOCK:
                modes._PLANS[data["plan_id"]]["expires_at"] = time.time() - 5
            status, resp = _apply(sb.home, {"plan_id": data["plan_id"], "confirm": True})
            self.assertEqual(status, 409)
            self.assertEqual(resp["error"]["code"], "plan_expired")
            self.assertIsNone(sb.assignment("s_a1"))

    def test_unknown_plan(self):
        with ProjectSandbox() as sb:
            _seed_mode_a(sb)
            status, resp = _apply(sb.home, {"plan_id": "plan-does-not-exist", "confirm": True})
            self.assertEqual(status, 404)
            self.assertEqual(resp["error"]["code"], "unknown_plan")

    def test_verify_mismatch_rolls_back(self):
        with ProjectSandbox() as sb:
            _seed_mode_a(sb)
            before = modes.db_hashes(sb.home)
            data = _plan_data(sb.home, mode=modes.MODE_UNGROUPED_ONLY)
            fake = {"checked": 1, "mismatched": [{"session_id": "s_a1", "why": "注入的不一致"}],
                    "cwd_mismatched": []}
            with mock.patch.object(modes, "_readback", return_value=fake):
                status, resp = _apply(sb.home, {"plan_id": data["plan_id"], "confirm": True})
            self.assertEqual(status, 409)
            self.assertEqual(resp["error"]["code"], "verify_mismatch")
            self.assertTrue(resp["rolled_back"])
            self.assertIsNone(sb.assignment("s_a1"), "回读不符必须整批回滚")
            self.assertEqual(sb.cwd_of("s_a1"), str(sb.root))
            after = modes.db_hashes(sb.home)
            for rel in modes.TRACKED_DBS:
                self.assertEqual(after[rel]["content"], before[rel]["content"], rel)

    def test_undo_restores_three_dbs(self):
        with ProjectSandbox() as sb:
            alpha, beta = _seed_mode_a(sb)
            before_hashes = modes.db_hashes(sb.home)
            grouped_before = sb.assignment("s_grouped")
            data = _plan_data(sb.home, mode=modes.MODE_UNGROUPED_ONLY)
            status, resp = _apply(sb.home, {"plan_id": data["plan_id"], "confirm": True})
            self.assertEqual(status, 200, resp)
            op_id = resp["data"]["op_id"]
            status2, undo = _undo(sb.home, op_id)
            self.assertEqual(status2, 200, undo)
            self.assertTrue(undo["ok"])
            self.assertTrue(undo["data"]["byte_identical"],
                            json.dumps(undo["data"]["hashes"], ensure_ascii=False))
            after = modes.db_hashes(sb.home)
            for rel in modes.TRACKED_DBS:
                self.assertEqual(after[rel]["content"], before_hashes[rel]["content"], rel)
            for rel in modes.BYTE_RESTORE_DBS:
                self.assertEqual(after[rel]["file"], before_hashes[rel]["file"], rel)
            self.assertIsNone(sb.assignment("s_a1"), "撤销后归属行回到「原本没有」")
            self.assertEqual(sb.cwd_of("s_a1"), str(sb.root))
            self.assertEqual(sb.assignment("s_grouped"), grouped_before)
            # 再撤一次 ⇒ 409 already_undone
            status3, again = _undo(sb.home, op_id)
            self.assertEqual(status3, 409)
            self.assertEqual(again["error"]["code"], "already_undone")


@unittest.skip(_PLAN_API_REMOVED)
class ModeBContractTest(unittest.TestCase):
    def test_candidates_include_grouped_and_diff_present(self):
        with ProjectSandbox() as sb:
            _seed_mode_b(sb)
            data = _plan_data(sb.home, mode=modes.MODE_REBUILD_ALL)
            self.assertEqual(data["mode"], "rebuild_all")
            self.assertEqual(sorted(data["candidates"]["session_ids"]), ["s1", "s2", "s3"],
                             "B 模式候选含已分组会话，看板会话仍在候选外")
            self.assertEqual(data["candidates"]["excluded_kanban"], ["kb1"])
            diff = data["diff"]
            self.assertIn("projects", diff)
            self.assertEqual(sorted(p["slug"] for p in diff["projects"]["kept"]), ["alpha", "beta"])
            self.assertEqual([p["slug"] for p in diff["projects"]["created"]], ["gamma"])
            self.assertEqual([p["slug"] for p in diff["projects"]["dropped"]], ["zeta"])
            moved = {m["session_id"]: (m["from"], m["to"]) for m in diff["moved"]}
            self.assertEqual(moved["s2"], ("alpha", "beta"))
            self.assertEqual(moved["s3"], ("zeta", "gamma"))
            self.assertEqual(diff["counts"]["dropped"], 1)

    def test_apply_rebuild_then_undo_restores_dropped_project(self):
        with ProjectSandbox() as sb:
            alpha, beta, zeta = _seed_mode_b(sb)
            before_hashes = modes.db_hashes(sb.home)
            zeta_before = sb.project_row("zeta")
            s1_before, s2_before, s3_before = (sb.assignment("s1"), sb.assignment("s2"),
                                               sb.assignment("s3"))
            kb_before = sb.assignment("kb1")
            data = _plan_data(sb.home, mode=modes.MODE_REBUILD_ALL)
            status, resp = _apply(sb.home, {"plan_id": data["plan_id"], "confirm": True})
            self.assertEqual(status, 200, resp)
            out = resp["data"]
            self.assertEqual(out["readback"]["mismatched"], [])
            self.assertEqual(out["readback"]["cwd_mismatched"], [])
            # 新体系真的落地
            gamma = sb.project_row("gamma")
            self.assertIsNotNone(gamma, "B 模式应建出新项目")
            self.assertTrue((sb.projects_root / "gamma").is_dir())
            self.assertIsNone(sb.project_row("zeta"), "新体系里没有会话的旧项目被抛弃")
            self.assertEqual(sb.assignment("s2")["project_slug"], "beta")
            self.assertEqual(sb.cwd_of("s2"), str(beta))
            self.assertEqual(sb.assignment("s3")["project_slug"], "gamma")
            self.assertEqual(sb.cwd_of("s3"), gamma["primary_path"])
            self.assertEqual(sb.assignment("s1"), s1_before, "新体系里不变的会话一行都不动")
            self.assertEqual(sb.assignment("kb1"), kb_before, "看板会话零误动")
            self.assertEqual(sb.cwd_of("kb1"), str(alpha))

            status2, undo = _undo(sb.home, out["op_id"])
            self.assertEqual(status2, 200, undo)
            self.assertTrue(undo["data"]["byte_identical"],
                            json.dumps(undo["data"]["hashes"], ensure_ascii=False))
            after = modes.db_hashes(sb.home)
            for rel in modes.TRACKED_DBS:
                self.assertEqual(after[rel]["content"], before_hashes[rel]["content"], rel)
            for rel in modes.BYTE_RESTORE_DBS:
                self.assertEqual(after[rel]["file"], before_hashes[rel]["file"], rel)
            # 被抛弃的旧项目逐字段回来（名 / path / 文件夹行）
            restored = sb.project_row("zeta")
            self.assertIsNotNone(restored)
            for key in ("id", "slug", "name", "description", "primary_path", "archived"):
                self.assertEqual(restored[key], zeta_before[key], key)
            self.assertIsNotNone(sb.attrs_row("zeta"), "属性行也要回来")
            # 新建的项目被撤销
            self.assertIsNone(sb.project_row("gamma"))
            self.assertFalse((sb.projects_root / "gamma").exists(), "本次新建的空目录也要删掉")
            # 归属行与 cwd 逐字段还原
            self.assertEqual(sb.assignment("s1"), s1_before)
            self.assertEqual(sb.assignment("s2"), s2_before)
            self.assertEqual(sb.assignment("s3"), s3_before)
            self.assertEqual(sb.cwd_of("s2"), str(alpha))
            self.assertEqual(sb.cwd_of("s3"), str(sb.home / "work" / "gamma"))


@unittest.skip(_PLAN_API_REMOVED)
class KanbanHardConstraintTest(unittest.TestCase):
    def test_kanban_never_in_plan_items(self):
        with ProjectSandbox() as sb:
            _seed_mode_b(sb)
            for mode in modes.MODES:
                data = _plan_data(sb.home, mode=mode)
                self.assertNotIn("kb1", _ids(data["items"]), mode)
                self.assertNotIn("kb1", data["candidates"]["session_ids"], mode)
                self.assertIn("kb1", data["candidates"]["excluded_kanban"], mode)
                self.assertTrue(all("kb1" not in (p.get("session_ids") or [])
                                    for p in data["new_projects"]), mode)


class LedgerAndLegacyTest(unittest.TestCase):
    @unittest.skip(_PLAN_API_REMOVED)
    def test_ops_listing(self):
        with ProjectSandbox() as sb:
            _seed_mode_a(sb)
            data = _plan_data(sb.home, mode=modes.MODE_UNGROUPED_ONLY)
            status, resp = _apply(sb.home, {"plan_id": data["plan_id"], "confirm": True})
            self.assertEqual(status, 200, resp)
            listing = base.get_plan_ops(50)
            self.assertTrue(listing["ok"])
            self.assertEqual(listing["data"]["total"], 1)
            entry = listing["data"]["ops"][0]
            self.assertEqual(entry["op_id"], resp["data"]["op_id"])
            self.assertEqual(entry["mode"], "ungrouped_only")
            self.assertTrue(entry["can_undo"])

    def test_legacy_apply_path_untouched(self):
        """不带 plan_id / mode ⇒ 走老语义（把 suggested 改成 applied），行为不变。"""
        with ProjectSandbox() as sb:
            alpha = sb.add_project("alpha", "Alpha")
            sb.add_session("ok1", "普通会话", cwd=str(sb.root))
            sb.add_assignment("ok1", "alpha", status="suggested")
            resp = body(base.post_apply({"confirm": True, "session_ids": ["ok1"], "sync_cwd": True}))
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(sb.assignment("ok1")["status"], "applied")
            self.assertEqual(sb.cwd_of("ok1"), str(alpha))

    def test_unknown_op_falls_back_to_panel_ledger(self):
        """op_id 不在计划台账里 ⇒ 仍走面板台账（返回 unknown_op，不是崩）。"""
        with ProjectSandbox() as sb:
            _seed_mode_a(sb)
            status, resp = _undo(sb.home, "op-not-here")
            self.assertEqual(status, 404)
            self.assertIn(resp["error"]["code"], ("unknown_op",))


class WalSnapshotTest(unittest.TestCase):
    """t_e5adf456 缺陷 2：``projects.db`` 是 WAL，快照只拷主文件 ⇒ 撤销复活已删项目。

    复现条件（真库就是 WAL）：某次写（例如删项目）还在 ``-wal`` 里没 checkpoint，
    这时 ``shutil.copy2`` 只拷主文件 ⇒ 快照里那一行还在 ⇒ 撤销把陈旧快照拷回去，
    用户看到「删掉的项目又回来了」，且 ``byte_identical=false``。
    """

    @staticmethod
    def _delete_in_wal(sb, slug):
        """在 WAL 下删项目且**不 checkpoint**：写连接保持打开，那一行只存在于 ``-wal``。"""
        pdb = sb.home / "projects.db"
        conn = sqlite3.connect(str(pdb), timeout=15)
        conn.execute("PRAGMA journal_mode=wal").fetchone()
        conn.execute("PRAGMA wal_autocheckpoint=0")
        with conn:
            conn.execute("DELETE FROM project_folders WHERE project_id ="
                         " (SELECT id FROM projects WHERE slug = ?)", (slug,))
            conn.execute("DELETE FROM projects WHERE slug = ?", (slug,))
        return conn

    @unittest.skip(_PLAN_API_REMOVED)
    def test_undo_does_not_revive_project_deleted_in_wal(self):
        with ProjectSandbox() as sb:
            alpha = sb.add_project("alpha", "Alpha")
            sb.add_project("probe-new-project", "Probe")
            sb.add_session("s1", "alpha 方案讨论", cwd=str(alpha))
            sb.add_assignment("s1", "alpha", source="panel")
            sb.add_session("s2", "alpha 未分组会话", cwd=str(alpha))
            writer = self._delete_in_wal(sb, "probe-new-project")
            try:
                self.assertEqual(_slugs_in(sb.home / "projects.db"), ["alpha"],
                                 "删除已生效（读走 -wal）")
                wal = Path(str(sb.home / "projects.db") + "-wal")
                self.assertTrue(wal.is_file() and wal.stat().st_size > 0,
                                "删除必须还在 -wal 里没落盘，否则复现不了")
                data = _plan_data(sb.home, mode=modes.MODE_REBUILD_ALL)
                status, resp = _apply(sb.home, {"plan_id": data["plan_id"], "confirm": True})
                self.assertEqual(status, 200, resp)
                out = resp["data"]
                self.assertGreaterEqual(out["written"], 1, "B 模式应真写了归属行")
                entry = next(f for f in out["snapshot"]["files"] if f["db"] == "projects.db")
                self.assertTrue(entry["checkpoint"]["ok"], entry["checkpoint"])
                self.assertNotIn("probe-new-project", _slugs_in(entry["file"]),
                                 "快照必须与 live 同一时刻：不许留着已删的那一行")
                self.assertEqual(entry["sha256"], out["hashes"]["before"]["projects.db"]["file"],
                                 "pre_hashes 与快照是同一份字节（同一时刻取）")
                op_id = out["op_id"]
            finally:
                writer.close()      # 最后一个连接关闭 ⇒ sqlite 自己 checkpoint 并删掉 -wal
            status2, undo = _undo(sb.home, op_id)
            self.assertEqual(status2, 200, undo)
            self.assertTrue(undo["data"]["byte_identical"],
                            json.dumps(undo["data"]["hashes"], ensure_ascii=False))
            self.assertEqual(_slugs_in(sb.home / "projects.db"), ["alpha"],
                             "撤销不许把已删项目写回来")
            self.assertIsNotNone(sb.project_row("alpha"))
            self.assertIsNone(sb.assignment("s2"), "B 模式写的归属行也要撤销")

    def test_checkpoint_helper_is_noop_for_rollback_journal_db(self):
        """非 WAL 库（沙盒默认 / 真 assignments.db）上 checkpoint 是 no-op，不改字节。"""
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            pdb = sb.home / "projects.db"
            before = pdb.read_bytes()
            info = modes._checkpoint(pdb)
            self.assertTrue(info["ok"], info)
            self.assertEqual(info["busy"], 0, info)
            self.assertEqual(pdb.read_bytes(), before, "no-op 不许改动主文件")

    def test_backup_fallback_snapshot_is_wal_consistent(self):
        """checkpoint 被挡住时的降级路径（``Connection.backup()``）也不许交陈旧内容。"""
        with ProjectSandbox() as sb:
            sb.add_project("alpha", "Alpha")
            sb.add_project("probe-new-project", "Probe")
            writer = self._delete_in_wal(sb, "probe-new-project")
            try:
                dst = sb.root / "fallback.bak"
                self.assertTrue(modes._backup_db(sb.home / "projects.db", dst))
                self.assertNotIn("probe-new-project", _slugs_in(dst))
                self.assertIn("alpha", _slugs_in(dst))
            finally:
                writer.close()

    @unittest.skip(_PLAN_API_REMOVED)
    def test_backup_fallback_apply_and_undo_end_to_end(self):
        """checkpoint 被挡住时整条链仍然正确：快照降级成 backup，撤销按内容还原。"""
        with ProjectSandbox() as sb:
            alpha = sb.add_project("alpha", "Alpha")
            sb.add_project("probe-new-project", "Probe")
            sb.add_session("s1", "alpha 方案讨论", cwd=str(alpha))
            sb.add_assignment("s1", "alpha", source="panel")
            sb.add_session("s2", "alpha 未分组会话", cwd=str(alpha))
            writer = self._delete_in_wal(sb, "probe-new-project")
            real_checkpoint = modes._checkpoint

            def blocked(path, mode="TRUNCATE"):
                if Path(path).name == "projects.db":
                    return {"ok": False, "busy": 1, "log": -1, "checkpointed": -1,
                            "error": None, "mode": mode}
                return real_checkpoint(path, mode)

            modes._checkpoint = blocked
            try:
                data = _plan_data(sb.home, mode=modes.MODE_REBUILD_ALL)
                status, resp = _apply(sb.home, {"plan_id": data["plan_id"], "confirm": True})
                self.assertEqual(status, 200, resp)
                out = resp["data"]
                entry = next(f for f in out["snapshot"]["files"] if f["db"] == "projects.db")
                self.assertEqual(entry["method"], "backup", "checkpoint 挡不住时走 backup")
                self.assertNotIn("probe-new-project", _slugs_in(entry["file"]))
                op_id = out["op_id"]
            finally:
                modes._checkpoint = real_checkpoint
                writer.close()
            status2, undo = _undo(sb.home, op_id)
            self.assertEqual(status2, 200, undo)
            self.assertTrue(undo["data"]["byte_identical"],
                            json.dumps(undo["data"]["hashes"], ensure_ascii=False))
            self.assertIsNone(undo["data"]["hashes"]["byte_match"]["projects.db"],
                              "降级快照不做逐字节断言（页布局本来就不同），如实报 null")
            self.assertEqual(_slugs_in(sb.home / "projects.db"), ["alpha"],
                             "降级路径也不许复活已删项目")
            self.assertIsNone(sb.assignment("s2"))

    @unittest.skip(_PLAN_API_REMOVED)
    def test_unstable_snapshot_refuses_to_apply(self):
        """快照与 live 的字节对不上（取快照期间被并发写）⇒ 409 零写入，不拿陈旧快照当回滚基准。"""
        with ProjectSandbox() as sb:
            _seed_mode_a(sb)
            before = sb.snapshot()
            data = _plan_data(sb.home, mode=modes.MODE_UNGROUPED_ONLY)
            real = modes.db_hashes

            def drifting(home):
                out = real(home)
                out["projects.db"]["file"] = "deadbeef" * 8     # 模拟「快照之后 live 又变了」
                return out

            modes.db_hashes = drifting
            try:
                status, resp = _apply(sb.home, {"plan_id": data["plan_id"], "confirm": True})
            finally:
                modes.db_hashes = real
            self.assertEqual(status, 409, resp)
            self.assertEqual(resp["error"]["code"], "snapshot_unstable")
            self.assertEqual(before, sb.snapshot(), "拒绝时必须零写入")
            self.assertIsNone(sb.assignment("s_a1"))
            left = list((sb.home / "auto-group" / "snapshots").glob("*"))
            self.assertEqual(left, [], "没用的快照目录要清掉，别留孤儿")


if __name__ == "__main__":
    unittest.main(verbosity=2)

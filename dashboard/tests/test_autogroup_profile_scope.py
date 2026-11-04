# -*- coding: utf-8 -*-
"""t_bba9216d · 候选引擎必须看得见 ``profiles/<p>/state.db``（与 /overview/sessions 同源）。

背景（老板实测）：界面按钮按各 profile 库算出「未分组 21 条」，点下去引擎只读根 ``state.db``
→ 候选 0 条、结果空态。本文件锁死四件事：

1. **数据源同源**：``candidate_scope(agent=work)`` 的候选 id 来自 ``profiles/work/state.db``，
   与根库无重叠时也不会漏；
2. **按钮 N == 符合条件 N == 真正处理 N**：``candidate_scope`` / ``_candidate_snapshot`` /
   ``build_preview`` 三条路给出同一个 id 集合（同一时刻、同一 agent）；
3. **空态文案同源**：``pool`` 与 ``already_grouped`` 能相加对上；全机器 applied 单独标注，
   不再出现「池里共 0 条，其中已分组 91 条」；
4. **agent 隔离与去重**：跨库同 id 只算一次，别的 agent 的会话不进本 agent 的候选。

全部数据是**人工合成**、落在 ``tempfile.mkdtemp()`` 沙盒 home 里；不读真实会话、不碰生产库。
跑法（dashboard 目录下）::

    <venv>/python.exe -m pytest tests/test_autogroup_profile_scope.py -q
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

DASH = Path(__file__).resolve().parents[1]
if str(DASH) not in sys.path:
    sys.path.insert(0, str(DASH))

import api_autogroup as base  # noqa: E402
import autogroup_ai as ai  # noqa: E402

SESSIONS_DDL = """
CREATE TABLE sessions (
  id TEXT PRIMARY KEY, title TEXT, display_name TEXT, profile_name TEXT, cwd TEXT,
  source TEXT, message_count INTEGER, started_at REAL, last_activity_at REAL,
  archived INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0, pinned INTEGER DEFAULT 0,
  parent_session_id TEXT, session_key TEXT, ended_at REAL, end_reason TEXT, model_config TEXT
);
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT
);
"""

PROJECTS_DDL = """
CREATE TABLE projects (
  id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL, description TEXT,
  icon TEXT, color TEXT, board_slug TEXT, primary_path TEXT, created_at INTEGER NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE project_folders (
  project_id TEXT NOT NULL, path TEXT NOT NULL, label TEXT, is_primary INTEGER NOT NULL DEFAULT 0,
  added_at INTEGER NOT NULL, PRIMARY KEY (project_id, path)
);
"""

ASSIGN_DDL = """
CREATE TABLE assignments (
  session_id TEXT PRIMARY KEY, project_slug TEXT NOT NULL, confidence REAL NOT NULL, reason TEXT,
  source TEXT, status TEXT NOT NULL, batch_id TEXT, applied_at REAL, updated_at REAL
);
CREATE TABLE batches (batch_id TEXT PRIMARY KEY, created_at REAL, note TEXT, undone INTEGER DEFAULT 0);
CREATE TABLE rejected (session_id TEXT PRIMARY KEY, reason TEXT, created_at REAL);
"""


def _mk_state(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(SESSIONS_DDL)
    conn.commit()
    return conn


def _add(conn, sid, title, *, profile_name="", source="cli", messages=4,
         archived=0, hidden=0, pinned=0, cwd=None, parent=None, session_key=None,
         end_reason=None, ended_at=None, model_config=None, body=None, at=1000.0):
    conn.execute(
        "INSERT OR REPLACE INTO sessions(id, title, display_name, profile_name, cwd, source,"
        " message_count, started_at, last_activity_at, archived, hidden, pinned,"
        " parent_session_id, session_key, ended_at, end_reason, model_config)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, title, title, profile_name, cwd, source, messages, at, at + 10,
         archived, hidden, pinned, parent, session_key, ended_at, end_reason, model_config))
    if messages >= 1:
        conn.execute("INSERT INTO messages(session_id, role, content) VALUES(?,?,?)",
                     (sid, "user", body or (title + " 的正文：这是一条可分类的会话")))


class ScriptedCaller:
    def __init__(self):
        self.calls = []

    def __call__(self, messages, **_kw):
        text = json.dumps(messages, ensure_ascii=False)
        self.calls.append(text)
        return {"text": json.dumps({"project_slug": "memory", "confidence": 0.93,
                                    "reason": "关键词命中记忆系统", "new_project": None},
                                   ensure_ascii=False),
                "provider": "fake", "model": "fake-model", "elapsed_ms": 3,
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


class ProfileScopeCase(unittest.TestCase):
    """沙盒：根库（default）+ profiles/work + profiles/xiaomo + 一个 0 候选的 root-only 场景。"""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="ag-profile-scope-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self._projects()
        self._store()
        self._state()
        #: ``post_scan`` / ``get_job`` 走 ``base._home()``：不 patch 就会打到真 home（会被 approvals 挡，
        #: 更重要的是会把生产库当成沙盒。本文件的每个断言都只对沙盒成立）。
        self._orig_home = base._home
        base._home = lambda: self.home
        self.addCleanup(setattr, base, "_home", self._orig_home)

    # ------------------------------------------------------------------ 建库
    def _write_projects(self, home):
        conn = sqlite3.connect(str(Path(home) / "projects.db"))
        conn.executescript(PROJECTS_DDL)
        for pid, slug, name in (("p1", "memory", "记忆系统"), ("p2", "infra", "基础设施")):
            folder = self.home / "proj" / slug
            folder.mkdir(parents=True, exist_ok=True)
            conn.execute("INSERT INTO projects VALUES(?,?,?,NULL,NULL,NULL,NULL,?,0,0)",
                         (pid, slug, name, str(folder)))
            conn.execute("INSERT INTO project_folders VALUES(?,?,'main',1,0)", (pid, str(folder)))
        conn.commit()
        conn.close()

    def _projects(self):
        #: 项目库同样按 agent 分（t_4fa94e17）：default 在根，work 在 ``profiles/work``。
        self._write_projects(self.home)
        (self.home / "profiles" / "work").mkdir(parents=True, exist_ok=True)
        self._write_projects(self.home / "profiles" / "work")

    def _assign_store(self, home, rows):
        folder = Path(home) / "auto-group"
        folder.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(folder / "assignments.db"))
        conn.executescript(ASSIGN_DDL)
        for sid, slug in rows:
            conn.execute("INSERT INTO assignments VALUES(?,?,0.9,'人工归入','manual','applied',"
                         "'b0',?,?)", (sid, slug, 1500.0, 1500.0))
        conn.commit()
        conn.close()

    def _store(self):
        #: 归属库按 agent 分（t_4fa94e17）：default 在根库，work 在 ``profiles/work``。
        #: 全机器 applied = root-a / root-b / work-a，共 3 条（用来验「本池 vs 全库」）。
        self._assign_store(self.home, (("root-a", "memory"), ("root-b", "infra")))
        self._assign_store(self.home / "profiles" / "work", (("work-a", "memory"),))

    def _state(self):
        #: 根库（= profile ``default``）：3 条可见，其中 2 条已 applied
        root = _mk_state(self.home / "state.db")
        _add(root, "root-a", "根库：记忆系统的表结构", cwd=str(self.home / "proj" / "memory"))
        _add(root, "root-b", "根库：k8s 集群升级", cwd=str(self.home / "proj" / "infra"))
        _add(root, "root-c", "根库：这条还没归组")
        #: 根库里遗留的 xiaomo 行（宿主 profile_name 列点名了真实 profile）
        _add(root, "root-legacy-xm", "根库遗留：profile_name=xiaomo", profile_name="xiaomo")
        #: 根库里不合格的行：看板 / 隐藏 / 归档未置顶 / 0 消息 / 压缩续接子行
        _add(root, "root-kanban", "看板任务会话", source="kanban")
        _add(root, "root-hidden", "被隐藏的会话", hidden=1)
        _add(root, "root-archived", "归档未置顶", archived=1)
        _add(root, "root-empty", "空会话", messages=0)
        _add(root, "root-compress-child", "压缩续接行", parent="root-c", session_key="k1",
             end_reason="compression", model_config=json.dumps({}))
        root.commit()
        root.close()

        #: profiles/work：3 条可见（work-a 已 applied → 2 条候选）+ 1 条 subagent/delegate 子行
        work = _mk_state(self.home / "profiles" / "work" / "state.db")
        _add(work, "work-a", "work：记忆系统重构", cwd=str(self.home / "proj" / "memory"))
        _add(work, "work-b", "work：200L 塑料桶灌装线技术协议")
        _add(work, "work-c", "work：报价清单预算表")
        _add(work, "work-delegate", "work：subagent 运行", parent="work-b",
             model_config=json.dumps({"_delegate_from": "work-b"}))
        work.commit()
        work.close()
        (self.home / "profiles" / "work" / "config.yaml").write_text("model: x\n", encoding="utf-8")

        #: profiles/xiaomo：2 条可见；其中一条与根库同 id（验跨库去重）
        xm = _mk_state(self.home / "profiles" / "xiaomo" / "state.db")
        _add(xm, "xm-a", "xiaomo：录音转录与工时登记")
        _add(xm, "xm-b", "xiaomo：每日学习料推送")
        _add(xm, "root-legacy-xm", "xiaomo：同 id 也在根库（去重后只算一次）", messages=6)
        xm.commit()
        xm.close()
        (self.home / "profiles" / "xiaomo" / "config.yaml").write_text("model: x\n", encoding="utf-8")

    def scope(self, agent=None):
        return ai.candidate_scope(self.home, agents={agent} if agent else None)

    # ================================================================== 1
    def test_work_candidates_come_from_profile_db(self):
        """主修：work 的候选必须来自 profiles/work/state.db（根库里没有任何 work 行）。"""
        sc = self.scope("work")
        ids = set(sc["candidate_session_ids"])
        self.assertEqual(ids, {"work-b", "work-c"}, sc["candidate_session_ids"])
        self.assertEqual(sc["total_available"], 2)
        root_ids = {r[0] for r in sqlite3.connect(str(self.home / "state.db")).execute(
            "SELECT id FROM sessions")}
        conn = sqlite3.connect(str(self.home / "profiles" / "work" / "state.db"))
        try:
            work_ids = {r[0] for r in conn.execute("SELECT id FROM sessions")}
        finally:
            conn.close()
        self.assertTrue(ids.issubset(work_ids), "候选必须来自 profile 库")
        self.assertFalse(ids & root_ids, "候选不许来自根库")

    def test_scope_ids_are_attributed_to_the_right_profile(self):
        sc = self.scope("work")
        self.assertEqual(set(sc["candidate_profiles"].values()), {"work"})
        self.assertEqual([p["profile"] for p in sc["profiles_scanned"]], ["work"])

    # ================================================================== 2
    def test_button_count_equals_snapshot_and_preview(self):
        """验收 2/3：按钮 N == 符合条件 N == 真正处理 N（三条路同一个 id 集合）。"""
        sc = self.scope("work")
        core = base._load_core()
        snap = base._candidate_snapshot(self.home, core, "work", "ungrouped",
                                        store_home=base.agent_home("work", self.home))
        self.assertEqual(set(snap["ids"]), set(sc["candidate_session_ids"]))
        self.assertEqual(snap["pool"], sc["pool"])
        self.assertEqual(snap["applied_in_pool"], sc["applied_in_pool"])

        caller = ScriptedCaller()
        payload = ai.build_preview(self.home, limit=0, caller=caller, agents={"work"})
        self.assertTrue(payload["ok"], payload)
        preview_ids = {str(item["session_id"]) for item in payload["items"]}
        self.assertEqual(preview_ids, set(sc["candidate_session_ids"]), payload["items"])
        self.assertEqual(payload["scope"]["total_available"], sc["total_available"])
        # 每条候选都真的调了模型，且正文来自 profile 库（不是空摘要）
        self.assertEqual(len(caller.calls), len(preview_ids))
        for item in payload["items"]:
            self.assertTrue(item["evidence"]["messages"], item["session_id"])
        self.assertTrue(all(item["action"] != "undecided" for item in payload["items"]),
                        [(i["session_id"], i["undecided_code"]) for i in payload["items"]])

    def test_scan_job_reports_same_numbers(self):
        watcher = base.post_scan({"agent": "work", "limit": 0, "scope": "ungrouped"})
        body = getattr(watcher, "body", None)
        data = json.loads(body.decode("utf-8")) if body else watcher
        job_id = (data.get("data") or {}).get("job_id") or data.get("job_id")
        self.assertTrue(job_id, data)
        import time
        deadline = time.time() + 60
        result = {}
        while time.time() < deadline:
            poll = base.get_job(job_id)
            pbody = getattr(poll, "body", None)
            pdata = json.loads(pbody.decode("utf-8")) if pbody else poll
            job = pdata.get("data") or pdata
            if job.get("state") in ("done", "failed"):
                result = job.get("result") or {}
                self.assertEqual(job.get("state"), "done", job)
                break
            time.sleep(0.1)
        self.assertTrue(result, "scan job 没在 60s 内结束")
        self.assertEqual(result["available"], 2, result)
        self.assertEqual(set(result["available_ids"]), {"work-b", "work-c"}, result)
        self.assertEqual(result["scanned"], 2, result)
        # 空态字段同源（可相加）
        self.assertEqual(result["pool"], 3, result)
        self.assertEqual(result["already_grouped"], 1, result)      # work-a 已 applied
        self.assertEqual(result["already_grouped_all"], 3, result)  # 全机器 applied

    # ================================================================== 3
    def test_empty_state_numbers_are_consistent(self):
        """空态文案：池 / 本池已分组能相加对上；全库 applied 单独一个字段。"""
        root = self.scope("default")
        self.assertEqual(root["total_available"], 1, root["candidate_session_ids"])  # root-c
        self.assertEqual(root["pool"], 3, root)                # root-a/root-b/root-c
        self.assertEqual(root["applied_in_pool"], 2, root)     # root-a/root-b
        #: 按 agent 计数：只算 default 归属的 applied（root-a/root-b）；work-a 不算
        self.assertEqual(root["excluded_applied"], 2, root)
        self.assertEqual(self.scope()["excluded_applied"], 3, "全机器口径要把 work-a 算进来")
        self.assertLessEqual(root["applied_in_pool"], root["pool"])

        # 造一个「本池 0 候选、全库 applied > 0」的场景：default 池全部已分组
        conn = sqlite3.connect(str(self.home / "auto-group" / "assignments.db"))
        try:
            with conn:
                conn.execute("INSERT INTO assignments VALUES('root-c','memory',0.9,'x','manual',"
                             "'applied','b1',1600.0,1600.0)")
        finally:
            conn.close()
        root = self.scope("default")
        self.assertEqual(root["total_available"], 0)
        self.assertEqual(root["pool"], 3)
        self.assertEqual(root["applied_in_pool"], 3)          # <= pool：能相加对上
        self.assertEqual(root["excluded_applied"], 3)          # 按 agent：root-a/b/c
        self.assertEqual(self.scope()["excluded_applied"], 4, "全机器：再加 work-a")

        # 跑一次扫描，空态文案不许出现「池里共 0 条 … 已分组 4 条」那种矛盾
        import time
        watcher = base.post_scan({"agent": "default", "limit": 0, "scope": "ungrouped"})
        data = json.loads(watcher.body.decode("utf-8")) if getattr(watcher, "body", None) else watcher
        job_id = (data.get("data") or {}).get("job_id")
        deadline = time.time() + 60
        res = {}
        while time.time() < deadline:
            poll = base.get_job(job_id)
            pdata = json.loads(poll.body.decode("utf-8")) if getattr(poll, "body", None) else poll
            job = pdata.get("data") or pdata
            if job.get("state") in ("done", "failed"):
                res = job.get("result") or {}
                break
            time.sleep(0.1)
        self.assertTrue(res.get("empty"), res)
        self.assertEqual(res["pool"], 3)
        self.assertEqual(res["already_grouped"], 3)
        self.assertEqual(res["already_grouped_all"], 4)
        reason = res["empty_reason"]
        #: 本池分解必须写成**能相加的显式等式**（t_bba9216d 收口后的措辞）：
        #: 池 = 候选 + 已分组 + 手动忽略 + 其它状态。以前是「本池共 N 条，其中已分组 M 条」，
        #: 读者无法验证是否还有其他桶，也无法与「全机器 4 条」区分。
        self.assertIn("本池 3 条会话 = 候选 0 条 + 已分组 3 条 + 手动忽略 0 条 + 其它状态 0 条", reason)
        self.assertIn("全机器归属表里已分组 4 条", reason)
        self.assertIn("不是本池口径", reason)
        self.assertNotIn("池里共 0 条", reason)
        #: 取池阶段就被挡掉的桶（看板等）绝不能出现在「本池 N 条」的括号里，
        #: 否则会读出「本池 3 条，其中看板任务 1 条」这种新的自相矛盾。
        pool_clause = reason.split("本池之外另排除")[0]
        self.assertNotIn("看板", pool_clause, reason)

    # ================================================================== 4
    def test_agent_isolation_and_cross_db_dedupe(self):
        work = set(self.scope("work")["candidate_session_ids"])
        xm = set(self.scope("xiaomo")["candidate_session_ids"])
        default = set(self.scope("default")["candidate_session_ids"])
        self.assertFalse(work & xm, (work, xm))
        self.assertFalse(work & default, (work, default))
        self.assertFalse(xm & default, (xm, default))
        #: 根库遗留的 profile_name=xiaomo 行归 xiaomo，不因为落在根库被算进 default
        self.assertIn("root-legacy-xm", xm)
        self.assertNotIn("root-legacy-xm", default)

        #: __all__ = 并集，同 id 只算一次
        everything = self.scope()
        ids = everything["candidate_session_ids"]
        self.assertEqual(len(ids), len(set(ids)), "跨库同 id 必须去重")
        self.assertTrue(work | xm | default == set(ids), sorted(set(ids)))

    def test_profile_library_excluded_buckets_are_not_global(self):
        """别的 agent 的排除数不许混进本 agent 的桶。"""
        work = self.scope("work")
        self.assertEqual(work["excluded_agent"], 0, work)
        #: work 库里的 delegate 子行被排除，但计入「子会话」而不是任何可见性桶
        self.assertEqual(work["excluded_child"], 1, work)
        self.assertEqual(work["excluded_kanban"], 0, work)


class VisibilityParityCase(unittest.TestCase):
    """验收 C：archived / hidden / pinned / 子行的判定，前后端必须是同一套结论。"""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="ag-vis-parity-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        conn = sqlite3.connect(str(self.home / "projects.db"))
        conn.executescript(PROJECTS_DDL)
        conn.commit()
        conn.close()
        folder = self.home / "auto-group"
        folder.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(folder / "assignments.db"))
        conn.executescript(ASSIGN_DDL)
        conn.commit()
        conn.close()
        st = _mk_state(self.home / "state.db")
        _add(st, "v-plain", "普通会话")
        _add(st, "v-hidden", "隐藏", hidden=1)
        _add(st, "v-archived", "归档未置顶", archived=1)
        _add(st, "v-pinned", "归档但置顶", archived=1, pinned=1)
        _add(st, "v-empty", "0 消息", messages=0)
        _add(st, "v-kanban", "看板", source="kanban")
        _add(st, "v-acp", "acp", source="acp")
        _add(st, "v-zero-parent", "压缩续接", parent="v-plain", session_key="k",
             end_reason="compression")
        _add(st, "v-branch", "分支会话", parent="v-plain",
             model_config=json.dumps({"_branched_from": "v-plain"}), ended_at=900.0)
        st.commit()
        st.close()

    def test_hidden_archived_child_rules(self):
        sc = ai.candidate_scope(self.home)
        ids = set(sc["candidate_session_ids"])
        self.assertEqual(ids, {"v-plain", "v-pinned", "v-branch"}, sorted(ids))
        self.assertEqual(sc["excluded_hidden"], 1)
        self.assertEqual(sc["excluded_archived"], 1)
        self.assertEqual(sc["excluded_kanban"], 1)
        self.assertEqual(sc["excluded_source"], 1)
        self.assertEqual(sc["excluded_no_message"], 1)
        self.assertEqual(sc["excluded_child"], 1)


if __name__ == "__main__":
    unittest.main()

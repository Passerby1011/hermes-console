# -*- coding: utf-8 -*-
"""口径用例（t_a562c864 变更后）：AI 分类只处理**桌面端可见的普通对话**。

看板任务会话（``sessions.source='kanban'``）不参与 —— 判定依据是宿主的 ``sessions.source``
字段，不看标题/assignee/ID 前缀。

覆盖老板 2026-10-30 16:39 变更里的四条：
1. 看板会话不进候选，且「已排除看板任务 M 条」计数正确（正常排除，不是漏处理）；
2. 看板会话**不产生任何模型调用**（不是调用后丢弃）；``_collect_samples`` 的典型样例也不含看板；
3. 仅在正文里**讨论**看板的普通聊天不得被误排除；
4. 全量与分批（``skip_ids`` 游标）候选集合一致、分母同源。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autogroup_ai as ai  # noqa: E402
from test_autogroup_ai import SandboxCase, make_caller  # noqa: E402


class KanbanScopeTest(SandboxCase):
    """看板任务会话的排除口径。"""

    KANBAN = {
        "kb1": "[看板] 卡12 回归验证",
        "kb2": "[看板] 卡14 二十四小时观察",
        "kb3": "[看板] 定时任务排查",
    }
    DISCUSS_ID = "d1"
    DISCUSS_TITLE = "看板任务太多，想按项目分组查看"
    HIDDEN_ID = "h1"
    PINNED_ID = "p1"
    ARCHIVED_ID = "ar1"

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(str(self.home / "state.db"))
        # 桌面可见条件用到 hidden/pinned：测试沙盒补这两列（真实 home 本来就有）
        conn.execute("ALTER TABLE sessions ADD COLUMN hidden INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER DEFAULT 0")
        for index, (sid, title) in enumerate(self.KANBAN.items()):
            conn.execute("INSERT INTO sessions(id,title,cwd,source,archived,message_count,"
                         "started_at,last_activity_at) VALUES(?,?,?,?,0,4,?,?)",
                         (sid, title, "D:/work/kanban-" + sid, "kanban",
                          1500.0 + index, 2500.0 + index))
            conn.execute("INSERT INTO messages(session_id, role, content) VALUES(?,?,?)",
                         (sid, "user", title + " 的正文：看板卡片的执行记录与回归结论"))
        # 普通聊天（source='cli'），但**正文里在讨论看板** —— 不能被误排除
        conn.execute("INSERT INTO sessions(id,title,cwd,source,archived,message_count,"
                     "started_at,last_activity_at) VALUES(?,?,?,?,0,4,?,?)",
                     (self.DISCUSS_ID, self.DISCUSS_TITLE, "D:/work/d1", "cli", 1600.0, 2600.0))
        conn.execute("INSERT INTO messages(session_id, role, content) VALUES(?,?,?)",
                     (self.DISCUSS_ID, "user",
                      "看板任务太多，我想知道怎么按项目分组查看这些看板卡，能不能加个筛选"))
        # hidden=1：桌面端 include_hidden=False 看不到
        conn.execute("INSERT INTO sessions(id,title,cwd,source,archived,message_count,"
                     "started_at,last_activity_at,hidden,pinned) VALUES(?,?,?,?,0,4,?,?,1,0)",
                     (self.HIDDEN_ID, "被隐藏的会话", "D:/work/hid", "cli", 1700.0, 2700.0))
        conn.execute("INSERT INTO messages(session_id, role, content) VALUES(?,?,?)",
                     (self.HIDDEN_ID, "user", "这条会话被用户隐藏了"))
        # archived=1 但 pinned=1：include_pinned=True ⇒ 仍可见
        conn.execute("INSERT INTO sessions(id,title,cwd,source,archived,message_count,"
                     "started_at,last_activity_at,hidden,pinned) VALUES(?,?,?,?,1,4,?,?,0,1)",
                     (self.PINNED_ID, "归档但置顶的会话", "D:/work/pin", "cli", 1800.0, 2800.0))
        conn.execute("INSERT INTO messages(session_id, role, content) VALUES(?,?,?)",
                     (self.PINNED_ID, "user", "归档了但我置顶了，还要看"))
        # archived=1 且没置顶：不可见
        conn.execute("INSERT INTO sessions(id,title,cwd,source,archived,message_count,"
                     "started_at,last_activity_at,hidden,pinned) VALUES(?,?,?,?,1,4,?,?,0,0)",
                     (self.ARCHIVED_ID, "归档未置顶的会话", "D:/work/arc", "cli", 1900.0, 2900.0))
        conn.execute("INSERT INTO messages(session_id, role, content) VALUES(?,?,?)",
                     (self.ARCHIVED_ID, "user", "归档了也不置顶"))
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------ 1
    def test_kanban_sessions_are_not_candidates(self):
        payload = self.run_preview(make_caller(), limit=0)
        scope = payload["scope"]
        self.assertEqual(scope["scope_rule"], ai.SCOPE_RULE)
        self.assertEqual(scope["excluded_kanban"], len(self.KANBAN), scope)
        for sid in self.KANBAN:
            self.assertNotIn(sid, scope["candidate_session_ids"])
            self.assertNotIn(sid, self.items_of(payload))
        # 看板排除写在 notes 里，且明说是「正常排除」
        self.assertTrue(any("看板任务" in note and "正常排除" in note
                            for note in payload["notes"]), payload["notes"])

    # ------------------------------------------------------------------ 2
    def test_kanban_sessions_never_reach_the_model(self):
        caller = make_caller()
        payload = self.run_preview(caller, limit=0)
        texts = ["\n".join(m.get("content", "") for m in call["messages"])
                 for call in caller.calls]
        self.assertTrue(texts, "应当有模型调用（桌面会话）")
        for title in self.KANBAN.values():
            self.assertFalse(any(title in text for text in texts),
                             "看板会话不许产生模型调用：%s" % title)
        # 调用次数 == 候选条数（看板不占名额，也不是「调用后丢弃」）
        self.assertEqual(len(caller.calls), len(payload["scope"]["candidate_session_ids"]))

    def test_samples_for_existing_projects_exclude_kanban(self):
        memory_path = str(self.home / "proj" / "memory")
        projects = [{"slug": "memory", "name": "记忆系统",
                     "primary_path": memory_path, "folders": []}]
        sessions = [
            {"id": "d9", "title": "普通会话：记忆库召回调优", "cwd": memory_path, "source": "cli"},
            {"id": "kb1", "title": self.KANBAN["kb1"], "cwd": memory_path, "source": "kanban"},
        ]
        samples = ai._collect_samples(projects, sessions, {})
        self.assertIn("普通会话：记忆库召回调优", samples["memory"])
        for title in self.KANBAN.values():
            self.assertNotIn(title, samples["memory"])

    # ------------------------------------------------------------------ 3
    def test_ordinary_chat_discussing_kanban_is_kept(self):
        payload = self.run_preview(make_caller(), limit=0)
        self.assertIn(self.DISCUSS_ID, payload["scope"]["candidate_session_ids"],
                      "只在正文里讨论看板的普通聊天不得被误排除")
        self.assertIn(self.DISCUSS_ID, self.items_of(payload))

    # ------------------------------------------------------------------ 4
    def test_hidden_and_archived_counts_are_separate(self):
        """三类排除分开计数：看板 / 隐藏 / 归档未置顶，且 pinned 归档仍可见。"""
        payload = self.run_preview(make_caller(), limit=0)
        scope = payload["scope"]
        self.assertEqual(scope["excluded_kanban"], len(self.KANBAN), scope)
        self.assertEqual(scope["excluded_hidden"], 1, scope)
        self.assertEqual(scope["excluded_archived"], 1, scope)
        ids = scope["candidate_session_ids"]
        self.assertNotIn(self.HIDDEN_ID, ids, "hidden=1 桌面端看不见，不该进候选")
        self.assertNotIn(self.ARCHIVED_ID, ids, "归档未置顶不该进候选")
        self.assertIn(self.PINNED_ID, ids, "归档但置顶（include_pinned=True）仍应可见")
        # 池与候选的关系：候选 = 池 − applied − rejected（同一个池算出来的，不是两套口径）
        self.assertEqual(scope["desktop_visible"],
                         len(ids) + scope["excluded_applied"] + scope["excluded_rejected"], scope)

    def test_full_and_batched_candidates_match(self):
        full = self.run_preview(make_caller(), limit=0)
        first = self.run_preview(make_caller(), limit=2)
        done = set(first["scope"]["candidate_session_ids"])
        self.assertEqual(len(done), 2)
        second = self.run_preview(make_caller(), limit=0, skip_ids=done)
        rest = set(second["scope"]["candidate_session_ids"])
        # 分母同源：三次数出来的 total_available 是同一个数
        self.assertEqual(full["scope"]["total_available"], first["scope"]["total_available"])
        self.assertEqual(full["scope"]["total_available"], second["scope"]["total_available"])
        # 集合一致且零重叠（skip_ids 游标不是「漏处理」）
        self.assertEqual(done | rest, set(full["scope"]["candidate_session_ids"]))
        self.assertFalse(done & rest)


if __name__ == "__main__":
    unittest.main()

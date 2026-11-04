"""hermes-console · AI 分类引擎测试（unittest.TestCase，Python 3.14 不收集裸 test_ 函数）。

全部数据都是**人工合成**、落在 ``tempfile.mkdtemp()`` 沙盒 home 里：不读真实会话内容、
不碰生产 ``assignments.db``、不调用任何 HTTP 路由（尤其不碰 ``/autogroup/auto``）。
模型用脚本化 fake caller 注入，引擎本身的零写入性质由 sha256 + 目录清单断言。
"""

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import autogroup_ai as ai  # noqa: E402

PROJECTS = [
    {"slug": "quick-send", "name": "快传与微信投递链路"},
    {"slug": "memory", "name": "记忆系统"},
    {"slug": "infra", "name": "基础设施"},
]

#: 沙盒里的会话：id -> 标题
SESSIONS = {
    "s1": "微信投递链路又断了",
    "s2": "quick-send 重试逻辑",
    "s3": "微信语音投递排查",
    "s4": "记忆库召回率",
    "s5": "随便聊聊今天天气",
    "s6": "量子计算编译器研究",
    "s7": "语音备忘转写工具",
    "s8": "语音备忘批量导入",
    "s9": "语音备忘目录整理",
    "s10": "语音备忘导出",
    "a1": "已归入的老会话",
    "g1": "待确认的会话",
    "r1": "被拒绝的会话",
}


def _verdict(slug=None, confidence=0.9, reason="r", new_project=None):
    return json.dumps({"project_slug": slug, "confidence": confidence,
                       "reason": reason, "new_project": new_project}, ensure_ascii=False)


class ScriptedCaller:
    """脚本化 caller：按标题路由到预置 verdict，并记录每次调用（便于断言 prompt/次数）。"""

    def __init__(self, mapping=None, default=None):
        self.mapping = mapping or {}
        self.default = default
        self.calls = []

    def __call__(self, messages, *, task, purpose, temperature, max_tokens, timeout):
        text = "\n".join(m.get("content", "") for m in messages)
        self.calls.append({"messages": messages, "task": task, "purpose": purpose})
        out = self.default
        for sid, title in SESSIONS.items():
            if "标题：" + title in text:
                if sid in self.mapping:
                    out = self.mapping[sid]
                break
        if isinstance(out, BaseException):
            raise out
        return {"text": out or _verdict(None, 0.1, "unknown"), "provider": "fake",
                "model": "fake-model", "elapsed_ms": 5,
                "usage": {"prompt_tokens": 90, "completion_tokens": 12, "total_tokens": 102}}


def make_caller(mapping=None, default=None):
    return ScriptedCaller(mapping, default)


class SandboxCase(unittest.TestCase):
    """公共沙盒：projects.db + state.db + auto-group/assignments.db。"""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="agai-test-"))
        self._make_projects()
        self._make_state()
        self._make_assignments()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.home, ignore_errors=True)

    def _make_projects(self):
        conn = sqlite3.connect(str(self.home / "projects.db"))
        conn.executescript("""
            CREATE TABLE projects(id TEXT, slug TEXT, name TEXT, description TEXT, icon TEXT,
                color TEXT, board_slug TEXT, primary_path TEXT, created_at REAL,
                archived INTEGER DEFAULT 0);
            CREATE TABLE project_folders(project_id TEXT, path TEXT, label TEXT,
                is_primary INTEGER, added_at REAL);
        """)
        for pid, slug, name in (("p1", "quick-send", "快传与微信投递链路"),
                                ("p2", "memory", "记忆系统"),
                                ("p3", "infra", "基础设施")):
            folder = self.home / "proj" / slug
            conn.execute("INSERT INTO projects VALUES(?,?,?,NULL,NULL,NULL,NULL,?,0,0)",
                         (pid, slug, name, str(folder)))
            conn.execute("INSERT INTO project_folders VALUES(?,?,'main',1,0)", (pid, str(folder)))
        conn.commit()
        conn.close()

    def _make_state(self):
        conn = sqlite3.connect(str(self.home / "state.db"))
        conn.executescript("""
            CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, cwd TEXT, source TEXT,
                archived INTEGER DEFAULT 0, message_count INTEGER, started_at REAL,
                last_activity_at REAL);
            CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
                role TEXT, content TEXT);
        """)
        for index, (sid, title) in enumerate(SESSIONS.items()):
            conn.execute("INSERT INTO sessions VALUES(?,?,?,?,0,4,?,?)",
                         (sid, title, "D:/work/" + sid, "cli", 1000.0 + index, 2000.0 + index))
            # 正文刻意做成「标题 + 2000 个 x + 尾标记」：裁剪生效时 BODYEND 不该出现在证据里。
            conn.execute("INSERT INTO messages(session_id, role, content) VALUES(?,?,?)",
                         (sid, "user", title + " 的正文 " + ("x" * 2000) + " BODYEND"))
            conn.execute("INSERT INTO messages(session_id, role, content) VALUES(?,?,?)",
                         (sid, "assistant", "助手回复"))
        conn.commit()
        conn.close()

    def _make_assignments(self):
        folder = self.home / "auto-group"
        folder.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(folder / "assignments.db"))
        conn.executescript("""
            CREATE TABLE assignments(session_id TEXT PRIMARY KEY, project_slug TEXT NOT NULL,
                confidence REAL NOT NULL, reason TEXT, source TEXT, status TEXT NOT NULL,
                batch_id TEXT, applied_at REAL, updated_at REAL);
            CREATE TABLE rejected(session_id TEXT PRIMARY KEY, reason TEXT, created_at REAL);
        """)
        conn.execute("INSERT INTO assignments VALUES('a1','quick-send',0.9,'人工归入','manual',"
                     "'applied','b0',900.0,900.0)")
        conn.execute("INSERT INTO assignments VALUES('g1','memory',0.6,'规则命中','rule',"
                     "'suggested',NULL,NULL,950.0)")
        conn.execute("INSERT INTO rejected VALUES('r1','人工拒绝',900.0)")
        conn.commit()
        conn.close()

    def snapshot(self):
        hashes = {}
        for name in ("projects.db", "state.db", "auto-group/assignments.db"):
            path = self.home / name
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        tree = sorted(os.path.join(root, name) for root, _dirs, files in os.walk(self.home)
                      for name in files)
        return hashes, tree

    def run_preview(self, caller, **kwargs):
        kwargs.setdefault("limit", 40)
        kwargs.setdefault("now", 1793340000.0)
        return ai.build_preview(self.home, caller=caller, **kwargs)

    def items_of(self, payload):
        return {item["session_id"]: item for item in payload["items"]}

# ==========================================================================
# 用例部分：严格 JSON 校验 / 阈值边界 / 同主题聚合 / 既有记录保护 / 零写入 / 失败路径
# ==========================================================================

class ParseVerdictTest(unittest.TestCase):
    """严格 JSON 校验：宁可 undecided，也不宽容修正。"""

    def test_rejects_json_garbage(self):
        for text in ("", "我认为属于 memory", '{"project_slug":"memory",', '[{"project_slug":"memory"}]'):
            got = ai.parse_verdict(text, PROJECTS)
            self.assertFalse(got["ok"], text)
            self.assertEqual(got["code"], "invalid_json", text)
            self.assertIsNone(got["verdict"])

    def test_rejects_wrong_field_types(self):
        cases = [
            '{"project_slug":"memory","confidence":"0.9","reason":"x","new_project":null}',
            '{"project_slug":"memory","confidence":true,"reason":"x","new_project":null}',
            '{"project_slug":123,"confidence":0.9,"reason":"x","new_project":null}',
            '{"project_slug":null,"confidence":0.9,"reason":"x","new_project":"y"}',
            '{"project_slug":"memory","confidence":0.9,"reason":7,"new_project":null}',
        ]
        for text in cases:
            got = ai.parse_verdict(text, PROJECTS)
            self.assertFalse(got["ok"], text)
            self.assertEqual(got["code"], "invalid_schema", text)

    def test_rejects_out_of_range_without_clamping(self):
        for text in ('{"project_slug":"memory","confidence":1.5,"reason":"x","new_project":null}',
                     '{"project_slug":"memory","confidence":-0.1,"reason":"x","new_project":null}'):
            got = ai.parse_verdict(text, PROJECTS)
            self.assertEqual(got["code"], "confidence_out_of_range", text)
            self.assertIsNone(got["verdict"])

    def test_rejects_unknown_project(self):
        got = ai.parse_verdict(_verdict("不存在的项目", 0.95), PROJECTS)
        self.assertEqual(got["code"], "unknown_project")
        self.assertIsNone(got["verdict"])

    def test_rejects_inconsistent_results(self):
        cases = [
            _verdict("memory", 0.9, new_project={"slug": "new-one", "name": "新"}),
            _verdict(None, 0.9),
            _verdict(None, 0.3, new_project={"slug": "new-one", "name": "新"}),
        ]
        for text in cases:
            got = ai.parse_verdict(text, PROJECTS)
            self.assertFalse(got["ok"], text)
            self.assertEqual(got["code"], "inconsistent", text)

    def test_rejects_invalid_new_slug(self):
        for slug in ("Bad Slug", "UPPER", "memory", "x"):
            got = ai.parse_verdict(_verdict(None, 0.9, new_project={"slug": slug, "name": "新"}),
                                   PROJECTS)
            self.assertEqual(got["code"], "invalid_new_project", slug)

    def test_rejects_model_native_shapes_seen_in_production(self):
        """实测 deepseek-v4.1-flash 会自己发明字段名；这些形状必须判无效而不是被「猜」成归属。"""
        cases = [
            # 真实模型实测输出：用 slug/name 而不是 project_slug/new_project
            '{"slug": "hermes-plugins", "name": "Hermes 升级与插件维护", "confidence": 0.99, '
            '"reason": "标题与典型会话一致"}',
            # 少 reason
            '{"project_slug": "hermes-plugins", "project_name": "Hermes 升级与插件维护", '
            '"confidence": 0.99}',
            # 包了一层 session
            '{"session": {"title": "按项目分组", "project_slug": "hermes-plugins"}, "confidence": 0.9}',
            # 夹了 needs_clarification
            '{"slug": "legacy-misc", "confidence": 0.2, "needs_clarification": true}',
        ]
        for text in cases:
            got = ai.parse_verdict(text, PROJECTS)
            self.assertFalse(got["ok"], text)
            self.assertIsNone(got["verdict"], text)

    def test_tolerates_fence_and_unknown_fields(self):
        fenced = ai.parse_verdict('```json\n' + _verdict("memory", 0.9) + '\n```', PROJECTS)
        self.assertTrue(fenced["ok"])
        self.assertEqual(fenced["verdict"]["project_slug"], "memory")
        extra = ai.parse_verdict(json.dumps({"project_slug": "memory", "confidence": 0.9,
                                             "reason": "x", "new_project": None, "note": "多余"}),
                                 PROJECTS)
        self.assertTrue(extra["ok"])
        self.assertTrue(any("ignored_unknown_fields" in w for w in extra["warnings"]))


class TierTest(unittest.TestCase):
    """阈值边界必须精确到 0.80 / 0.50（闭区间）。"""

    def test_boundaries(self):
        self.assertEqual(ai.tier_for(0.80), "apply")
        self.assertEqual(ai.tier_for(0.7999), "suggest")
        self.assertEqual(ai.tier_for(0.50), "suggest")
        self.assertEqual(ai.tier_for(0.4999), "undecided")
        self.assertEqual(ai.tier_for(0.0), "undecided")
        self.assertEqual(ai.tier_for(1.0), "apply")


class EndToEndTest(SandboxCase):
    """全链路：分层、未知项目、同主题聚合、既有记录保护、零写入。"""

    def _mixed_caller(self):
        mapping = {
            "s1": _verdict("quick-send", 0.90),
            "s2": _verdict("quick-send", 0.80),
            "s3": _verdict("quick-send", 0.7999),
            "s4": _verdict("memory", 0.60),
            "s5": _verdict("memory", 0.4999),
            "s6": _verdict("不存在的项目", 0.95),
            "s7": "这不是 JSON，模型跑飞了",
            "s8": _verdict(None, 0.85, new_project={"slug": "voice-notes", "name": "语音备忘"}),
            "s9": _verdict(None, 0.60, new_project={"slug": "voice-notes", "name": "语音备忘"}),
            "s10": '```json\n' + _verdict(None, 0.55,
                                          new_project={"slug": "voice-notes", "name": "语音备忘"}) + '\n```',
            "g1": _verdict("memory", 0.50),
        }
        return make_caller(mapping)

    def test_threshold_actions(self):
        items = self.items_of(self.run_preview(self._mixed_caller()))
        self.assertEqual(items["s1"]["action"], "apply_after_confirm")
        self.assertEqual(items["s1"]["proposed_status"], "applied")
        self.assertEqual(items["s2"]["action"], "apply_after_confirm")
        self.assertEqual(items["s3"]["action"], "suggest_after_confirm")
        self.assertEqual(items["s3"]["proposed_status"], "suggested")
        self.assertEqual(items["s4"]["action"], "suggest_after_confirm")
        self.assertEqual(items["g1"]["action"], "suggest_after_confirm")
        self.assertEqual(items["s5"]["action"], "undecided")
        self.assertEqual(items["s5"]["undecided_code"], "low_confidence")

    def test_unknown_project_is_undecided_and_not_guessed(self):
        items = self.items_of(self.run_preview(self._mixed_caller()))
        self.assertEqual(items["s6"]["action"], "undecided")
        self.assertEqual(items["s6"]["undecided_code"], "unknown_project")
        self.assertIsNone(items["s6"]["project_slug"])
        self.assertEqual(items["s7"]["undecided_code"], "invalid_json")

    def test_new_project_candidate_needs_three_sessions(self):
        payload = self.run_preview(self._mixed_caller())
        items = self.items_of(payload)
        self.assertEqual(len(payload["new_project_candidates"]), 1)
        candidate = payload["new_project_candidates"][0]
        self.assertEqual(candidate["slug"], "voice-notes")
        self.assertEqual(candidate["evidence_count"], 3)
        self.assertEqual(sorted(candidate["supporting_session_ids"]), ["s10", "s8", "s9"])
        for sid in ("s8", "s9", "s10"):
            self.assertTrue(items[sid]["is_new_project"])
            self.assertTrue(items[sid]["writable"])
            self.assertEqual(items[sid]["project_slug"], "voice-notes")
            self.assertTrue(items[sid]["new_project"]["candidate"])
        self.assertEqual(items["s8"]["proposed_status"], "applied")
        self.assertEqual(items["s9"]["proposed_status"], "suggested")

    def test_new_project_downgraded_when_only_two_sessions(self):
        mapping = {
            "s8": _verdict(None, 0.90, new_project={"slug": "voice-notes", "name": "语音备忘"}),
            "s9": _verdict(None, 0.90, new_project={"slug": "voice-notes", "name": "语音备忘"}),
        }
        payload = self.run_preview(make_caller(mapping))
        items = self.items_of(payload)
        self.assertEqual(payload["new_project_candidates"], [])
        for sid in ("s8", "s9"):
            self.assertEqual(items[sid]["action"], "suggest_after_confirm")
            self.assertFalse(items[sid]["writable"])
            self.assertFalse(items[sid]["new_project"]["candidate"])
            self.assertIn("3", items[sid]["new_project"]["downgrade_reason"])
            self.assertNotIn(sid, {row["session_id"] for row in payload["undecided"]})

    def test_existing_records_are_protected(self):
        payload = self.run_preview(self._mixed_caller())
        items = self.items_of(payload)
        self.assertNotIn("a1", items, "applied 会话必须被排除")
        self.assertNotIn("r1", items, "rejected 会话必须被排除")
        self.assertIn("g1", items, "suggested 会话要进预览")
        self.assertGreaterEqual(payload["scope"]["excluded_applied"], 1)
        self.assertEqual(payload["scope"]["excluded_rejected"], 1)
        self.assertEqual(payload["scope"]["considered"], len(payload["items"]))
        self.assertEqual(items["g1"]["before"]["row_exists"], True)
        self.assertEqual(items["g1"]["before"]["status"], "suggested")
        self.assertEqual(items["g1"]["before"]["project_slug"], "memory")
        self.assertFalse(items["s1"]["before"]["row_exists"])

    def test_no_writes_at_all(self):
        before_hash, before_tree = self.snapshot()
        payload = self.run_preview(self._mixed_caller())
        after_hash, after_tree = self.snapshot()
        self.assertEqual(before_hash, after_hash, "引擎不许改动任何库文件")
        self.assertEqual(before_tree, after_tree, "引擎不许新建文件/目录")
        self.assertEqual(set(payload["writes"].values()), {0})

    def test_counts_are_disjoint_and_add_up(self):
        payload = self.run_preview(self._mixed_caller())
        counts = payload["counts"]
        self.assertEqual(counts["total"], len(payload["items"]))
        self.assertEqual(counts["apply"] + counts["suggest"] + counts["new_project"]
                         + counts["undecided"], counts["total"])
        self.assertEqual(counts["new_project"], 3)
        self.assertEqual(counts["apply"], 2)
        self.assertEqual(counts["undecided"], 3)

    def test_evidence_is_truncated(self):
        items = self.items_of(self.run_preview(self._mixed_caller()))
        evidence = items["s1"]["evidence"]
        self.assertTrue(evidence["messages"])
        for message in evidence["messages"]:
            self.assertLessEqual(len(message), 200)
        self.assertLessEqual(evidence["chars"], 600)
        self.assertNotIn("BODYEND", "".join(evidence["messages"]), "不许把整段对话塞进证据")

    def test_usage_and_model_metadata(self):
        payload = self.run_preview(self._mixed_caller())
        self.assertTrue(payload["usage"]["real"])
        self.assertEqual(payload["model"]["calls"], len(payload["items"]))
        self.assertEqual(payload["model"]["model"], "fake-model")
        self.assertIsNone(
            payload["model"]["task"],
            "t_ac537221：项目分组 AI 不借 auxiliary.title_generation 槽位，task 必须是 None")

    def test_payload_is_serializable(self):
        payload = self.run_preview(self._mixed_caller())
        self.assertEqual(payload["schema"], "autogroup_ai.preview.v1")
        self.assertTrue(payload["batch_id"].startswith("ai-"))
        json.dumps(payload, ensure_ascii=False)


class FailurePathTest(SandboxCase):
    """通道缺失 / 单条调用失败 / usage 缺失。"""

    def test_llm_unavailable(self):
        original = ai.default_caller
        ai.default_caller = lambda: None
        try:
            payload = ai.build_preview(self.home, limit=10, caller=None, now=1793340000.0)
        finally:
            ai.default_caller = original
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "llm_unavailable")
        self.assertEqual(payload["items"], [])
        self.assertEqual(set(payload["counts"].values()), {0})

    def test_caller_exception_marks_call_failed(self):
        payload = self.run_preview(make_caller({"s1": RuntimeError("provider 500")}))
        item = self.items_of(payload)["s1"]
        self.assertEqual(item["action"], "undecided")
        self.assertEqual(item["undecided_code"], "call_failed")
        self.assertEqual(set(payload["writes"].values()), {0})

    def test_missing_usage_is_estimated_with_note(self):
        def caller(messages, **kwargs):
            return {"text": _verdict("memory", 0.9), "provider": "p", "model": "m",
                    "usage": None, "elapsed_ms": 2}

        payload = self.run_preview(caller)
        self.assertFalse(payload["usage"]["real"])
        self.assertIn("估算", payload["usage"]["note"])
        self.assertGreater(payload["usage"]["prompt_tokens"], 0)

    def test_prompt_contents(self):
        caller = make_caller({})
        self.run_preview(caller, limit=1)
        blob = "\n".join(message["content"] for message in caller.calls[0]["messages"])
        for project in PROJECTS:
            self.assertIn(project["slug"], blob)
            self.assertIn(project["name"], blob)
        self.assertIn("优先", blob)
        self.assertIn("已有项目", blob)
        self.assertIsNone(
            caller.calls[0]["task"],
            "t_ac537221：调用方必须传 task=None，宿主才不会读 auxiliary.<task> 的配置")

    def test_session_brief_truncates(self):
        brief = ai.session_brief(self.home, "s1")
        self.assertEqual(brief["title"], "微信投递链路又断了")
        self.assertEqual(brief["cwd"], "D:/work/s1")
        self.assertEqual(len(brief["messages"]), 1)
        self.assertLessEqual(len(brief["messages"][0]), 200)
        self.assertLessEqual(brief["chars"], 600)
        self.assertNotIn("BODYEND", brief["messages"][0])
        self.assertEqual(ai.session_brief(self.home, "不存在的会话")["messages"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

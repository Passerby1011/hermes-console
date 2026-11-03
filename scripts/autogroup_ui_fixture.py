# -*- coding: utf-8 -*-
"""项目分组面板验收夹具：把**真实的** ``dashboard/plugin_api.py`` 起在指定端口，HERMES_HOME 指沙盒。

沙盒里有什么（全部在临时目录，真 home 一个字节都不动）：

* ``projects.db`` —— 真 projects.db 的**副本**（17 个真项目，目录状态是真读的，只读展示）；
* ``state.db`` —— **合成**（真库 2.1GB，绝不复制）：只建本分区会读的 ``sessions`` 列，
  6 条测试会话（标题/profile/cwd/message_count 都齐）；
* ``auto-group/assignments.db`` —— **合成**：3 条 ``status=suggested`` 归属行（slug 取自真项目表
  前三个），所以面板一打开就有可勾选、可归入、可撤销的行。

跑法（由 ``scripts/probe-autogroup-panel.mjs`` 自动拉起，也可手工）::

    <venv>/python.exe plugins/hermes-console/scripts/autogroup_ui_fixture.py --port 9195 --sandbox D:/tmp/sb
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
REAL_HOME = PLUGIN_ROOT.parents[1]
HERMES_AGENT = REAL_HOME / "hermes-agent"
PREFIX = "/api/plugins/hermes-console"

#: (session_id, title, profile_name, message_count, cwd, source, hidden, archived)
#: source 用来验桌面端口径（kanban / acp / subagent / tool 必须被界面排除）。
SESSIONS = (
    ("sess-agg-01", "重构 kanban 调度器：先看 t_43c5fd05 的卡面", "kanban-worker", 12,
     "D:/hermes-data/projects/kanban-cron", "cli", 0, 0),
    ("sess-agg-02", "quicker-actions 插件的快捷键冲突排查", "xiaomo", 8,
     "D:/hermes-data/projects/quicker-actions", "cli", 0, 0),
    ("sess-agg-03", "multi-agent 编排：父卡拆子卡的写法", "work", 21,
     "D:/hermes-data/projects/multi-agent", "cli", 0, 0),
    ("sess-agg-04", "quick-send 面板的拖拽排序想改成点击", "xiaomo", 5,
     "D:/hermes-data/projects/quick-send", "cli", 0, 0),
    ("sess-agg-05", "xiaomo-liveness 心跳超时的日志在哪", "media-worker", 3,
     "D:/hermes-data/projects/xiaomo-liveness", "cli", 0, 0),
    ("sess-agg-06", "weather 分区逐时图表的取数口径", "xiaomo", 9, None, "cli", 0, 0),
    # 下面 4 条是**口径排除项**：界面必须一条都不显示（但库里真实存在）。
    ("sess-agg-07", "看板卡 t_deadbeef：这条不该出现在会话列表", "default", 7,
     "D:/hermes-data/projects/kanban-cron", "kanban", 0, 0),
    ("sess-agg-08", "空会话：message_count=0 应被排除", "default", 0, None, "cli", 0, 0),
    ("sess-agg-09", "隐藏会话：hidden=1 应被排除", "default", 4, None, "cli", 1, 0),
    ("sess-agg-10", "acp 会话：source=acp 应被排除", "default", 6, None, "acp", 0, 0),
)

#: work / xiaomo / code-worker 三个 profile 的会话（验「切 agent 后分区计数跟着变」）。
#: (session_id, title, message_count, source, hidden)
PROFILE_SESSIONS = {
    "work": (
        ("sess-wk-01", "work：把 kanban 看板的派发逻辑收敛", 14, "cli", 0),
        ("sess-wk-02", "work：这条还没归到任何项目", 6, "cli", 0),
        ("sess-wk-03", "work：subagent 运行（应被排除）", 2, "subagent", 0),
    ),
    "xiaomo": (
        ("sess-xm-01", "xiaomo：微信投递的重试策略", 11, "cli", 0),
        ("sess-xm-02", "xiaomo：这条也还没归", 3, "cli", 0),
    ),
    # 跑本验收的进程自己就是 code-worker profile：前端 shell 启动时按 ``/agents`` 的
    # ``current``（= 进程环境 HERMES_PROFILE）拉 ``GET /modules?agent=<当前作用域>``。
    # 沙盒里没有它 ⇒ 后端按设计返回 404 ``agent_not_found``（前端回落 FALLBACK_SECTIONS 是
    # 正确降级），但验收报告里会留一条与产品无关的 404 噪声（t_3871d238）。
    # 补两条会话让 ``/modules?agent=code-worker`` 与 ``/overview/sessions?agent=code-worker``
    # 都返回 200 —— 与真 home 里「跑探针的那个 profile 确实存在」一致。
    "code-worker": (
        ("sess-cw-01", "code-worker：验 /modules?agent=code-worker 不再 404", 4, "cli", 0),
        ("sess-cw-02", "code-worker：这条用于 profile 会话聚合", 2, "cli", 0),
    ),
}

def copy_state_schema(real_state: Path, dst: Path) -> int:
    """把真 state.db 的 **DDL** 拷到 dst（表/索引/触发器/视图），不拷任何一行数据。

    为什么必须这么做：``GET /overview/sessions`` → 宿主 ``get_sessions`` →
    ``list_sessions_rich`` 会按 65 列的完整 schema 建 SQL（hidden / pinned /
    parent_session_id / model_config / system_prompts 联表 …）。手写 10 列的表
    连 reconcile 都过不去（"Cannot add a NOT NULL column with default value NULL"），
    会话列表永远是 500。
    """
    src = sqlite3.connect("file:" + str(real_state) + "?mode=ro", uri=True)
    try:
        rows = src.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"
            " AND type IN ('table','index','trigger','view')").fetchall()
    finally:
        src.close()
    out = sqlite3.connect(str(dst))
    created = 0
    try:
        for _kind, _name, sql in rows:
            try:
                out.execute(sql)
                created += 1
            except sqlite3.Error:
                # FTS 影子表 / sqlite_sequence 这类会被上一条语句顺带建出来，忽略即可。
                pass
        out.commit()
    finally:
        out.close()
    return created


def seed_state_db(db_path: Path, real_state: Path, rows, messages=None) -> None:
    """建一个「真 schema + 合成数据」的 state.db。"""
    copy_state_schema(real_state, db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        now = time.time()
        for index, row in enumerate(rows):
            if len(row) == 8:
                sid, title, profile, count, cwd, source, hidden, archived = row
            else:  # profile 目录里的行：不带 profile 列（宿主自己会写 profile=）
                sid, title, count, source, hidden = row
                profile, cwd, archived = "", None, 0
            conn.execute(
                "INSERT OR REPLACE INTO sessions(id, title, display_name, profile_name, source, cwd,"
                " message_count, archived, hidden, started_at, last_activity_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (sid, title, title, profile, source, cwd, count, archived, hidden,
                 now - 3600 * (index + 1), now - 600 * (index + 1)),
            )
            text = (messages or {}).get(sid) or title
            try:
                conn.execute(
                    "INSERT INTO messages(session_id, role, content, created_at) VALUES(?, 'user', ?, ?)",
                    (sid, text, now - 3600 * (index + 1)))
            except sqlite3.Error:
                # 真 schema 的 messages 列更多，插不进去就算了：会话列表不读 messages。
                pass
        conn.commit()
    finally:
        conn.close()

#: 每条会话一句用户原文：tick 的规则分类要读它（first_user_text → messages）。
MESSAGES = {
    "sess-agg-01": "kanban 看板卡片调度器重构，cron 触发时间不对",
    "sess-agg-02": "quicker 动作的快捷键冲突了，2fa 也不对",
    "sess-agg-03": "multi-agent 编排：父卡拆子卡怎么写",
    "sess-agg-04": "quick-send 面板想改成点击发送",
    "sess-agg-05": "xiaomo 心跳 liveness 超时日志在哪",
    "sess-agg-06": "weather 天气图表的逐时取数口径",
}

#: 与 plugins/hermes-auto-group/core.py 的 SCHEMA 一致（本文件不 import 它，避免名字撞车）。
ASSIGN_SCHEMA = """
CREATE TABLE IF NOT EXISTS assignments (
  session_id TEXT PRIMARY KEY, project_slug TEXT NOT NULL, confidence REAL NOT NULL,
  reason TEXT, source TEXT, status TEXT NOT NULL, batch_id TEXT, applied_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS batches (
  batch_id TEXT PRIMARY KEY, created_at REAL, note TEXT, undone INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cwd_backup (
  session_id TEXT, batch_id TEXT, old_cwd TEXT, new_cwd TEXT, PRIMARY KEY (session_id, batch_id)
);
CREATE TABLE IF NOT EXISTS rejected (session_id TEXT PRIMARY KEY, reason TEXT, created_at REAL);
"""


def _top_level_block(text: str, key: str) -> str:
    """从一个 YAML 文本里抠出某个**顶层**键的整块（含子行），供沙盒 profile 复用。

    只做缩进判断，不解析 YAML：本夹具只需要「原样搬过去」，不需要语义。
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith(key + ":"):
            start = index
            break
    if start is None:
        return ""
    out = [lines[start]]
    for line in lines[start + 1:]:
        if not line.strip():
            out.append(line)
            continue
        if line[0] not in (" ", "\t"):
            break
        out.append(line)
    return "\n".join(out).rstrip() + "\n"


def make_sandbox(target: Path, extra_suggested: int = 0) -> Path:
    """造一个「像真 home」的沙盒：真 projects.db 副本 + 合成 state.db / assignments.db + 插件树镜像。

    ``extra_suggested``：额外再塞 N 条 ``status=suggested`` 归属行（session_id 形如
    ``sess-bulk-0001``，**只进 assignments.db、不进 state.db**）。用途是造「建议数 > 后端单次
    上限 200」的场景（t_4d192e1b：500 条时状态条 / 徽标 / 明细行数必须口径一致，且能加载取完）。
    这些行在 state.db 里没有对应会话，所以 title/profile 为空 —— 本场景验的是条数与分页，
    不是标题，别拿它当「会话都能读到」的证据。

    **为什么还要镜像插件树**：``api_agents.hermes_home()`` 故意不采信 ``HERMES_HOME``（worker 里它
    是 profile 目录），而是按 ``<file>/../../..`` 正向探测。所以只设环境变量会把写操作打到**真 home**。
    把 ``dashboard/`` 与 ``hermes-auto-group/core.py`` 复制到 ``<sb>/plugins/...`` 之后，上溯 3 级正好
    落在沙盒上 —— 真 home 一个字节都不会被动到（本夹具启动后会断言这一点）。
    """
    target = Path(target)
    if target.exists():
        shutil.rmtree(target)
    (target / "profiles").mkdir(parents=True)
    real_cfg = REAL_HOME / "config.yaml"
    if real_cfg.is_file():
        shutil.copy2(real_cfg, target / "config.yaml")
    else:
        (target / "config.yaml").write_text("{}\n", encoding="utf-8")
    source_projects = REAL_HOME / "projects.db"
    if not source_projects.is_file():
        raise SystemExit("真 projects.db 不存在：" + str(source_projects))
    shutil.copy2(source_projects, target / "projects.db")

    # 真 projects.db 的 primary_path 指向**真目录**：改写成沙盒里的副本目录。
    # 不改的话「删项目」的 rmdir 会打到真磁盘（t_15df3606 期间就发生过一次，已恢复）。
    conn = sqlite3.connect(str(target / "projects.db"))
    try:
        with conn:
            for (slug,) in conn.execute("SELECT slug FROM projects WHERE archived = 0").fetchall():
                folder = target / "projects" / slug
                folder.mkdir(parents=True, exist_ok=True)
                conn.execute("UPDATE projects SET primary_path = ? WHERE slug = ?", (str(folder), slug))
                conn.execute(
                    "UPDATE project_folders SET path = ? WHERE project_id IN (SELECT id FROM projects WHERE slug = ?)",
                    (str(folder), slug),
                )
    finally:
        conn.close()

    real_state = REAL_HOME / "state.db"
    if not real_state.is_file():
        raise SystemExit("真 state.db 不存在：" + str(real_state))
    seed_state_db(target / "state.db", real_state, SESSIONS, MESSAGES)
    # 二级 profile 的 config.yaml：照抄真 home 的 providers:/moa: 段，只把 model 改成不同的默认值，
    # 这样「两个 Agent 用不同 provider/model」才有真实凭据与真实模型清单可校验
    # （t_3c2817b1 验收项：两个 Agent 各设不同 provider/model，各自回读且互不影响）。
    root_cfg = (target / "config.yaml").read_text(encoding="utf-8")
    providers_block = _top_level_block(root_cfg, "providers")
    moa_block = _top_level_block(root_cfg, "moa")
    for index, (profile, rows) in enumerate(PROFILE_SESSIONS.items()):
        folder = target / "profiles" / profile
        folder.mkdir(parents=True, exist_ok=True)
        parts = []
        if providers_block:
            # 第二个 profile 不声明 gemini 的 models 段会造成清单相同：这里保留原样即可，
            # 「不同 provider」由测试自己挑不同 provider 达成。
            parts.append(providers_block)
        if moa_block and index == 0:
            parts.append(moa_block)
        (folder / "config.yaml").write_text("\n".join(parts) + "\n", encoding="utf-8")
        seed_state_db(folder / "state.db", real_state, rows)

    ro = sqlite3.connect("file:" + str(target / "projects.db") + "?mode=ro", uri=True)
    try:
        slugs = [row[0] for row in ro.execute("SELECT slug FROM projects WHERE archived = 0 ORDER BY created_at LIMIT 3")]
    finally:
        ro.close()
    if not slugs:
        raise SystemExit("真 projects.db 里没有可用项目，无法造建议行")

    store = target / "auto-group" / "assignments.db"
    store.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(store))
    try:
        conn.executescript(ASSIGN_SCHEMA)
        now = time.time()
        # 3 条 suggested（待确认建议）
        for index, sid in enumerate(("sess-agg-01", "sess-agg-02", "sess-agg-03")):
            conn.execute(
                "INSERT OR REPLACE INTO assignments(session_id, project_slug, confidence, reason, source,"
                " status, updated_at) VALUES(?,?,?,?,?, 'suggested', ?)",
                (sid, slugs[index % len(slugs)], 0.8 - index * 0.1,
                 "规则命中：关键词「" + sid[-2:] + "」", "rule", now - 120),
            )
        # 4 条 applied（已分组）—— 跨三个 profile，验「已分组」分区与 agent 筛选同时成立
        for index, (sid, slug) in enumerate((
                ("sess-agg-04", slugs[0]), ("sess-agg-05", slugs[1]),
                ("sess-wk-01", slugs[2]), ("sess-xm-01", slugs[0]))):
            conn.execute(
                "INSERT OR REPLACE INTO assignments(session_id, project_slug, confidence, reason, source,"
                " status, batch_id, applied_at, updated_at) VALUES(?,?,?,?,?, 'applied', ?, ?, ?)",
                (sid, slug, 0.9, "面板归入（夹具预置）", "panel", "batch-fixture-%02d" % index,
                 now - 300, now - 300),
            )
        # 「>200 条」场景：再塞 extra_suggested 条 suggested 行（只进归属表，不造会话）。
        for index in range(max(0, int(extra_suggested or 0))):
            conn.execute(
                "INSERT OR REPLACE INTO assignments(session_id, project_slug, confidence, reason, source,"
                " status, updated_at) VALUES(?,?,?,?,?, 'suggested', ?)",
                ("sess-bulk-%04d" % (index + 1), slugs[index % len(slugs)],
                 round(0.5 + (index % 40) / 100.0, 2),
                 "批量夹具：第 %d 条（验 >200 条时的口径与分页）" % (index + 1),
                 "rule", now - 60 - index),
            )
        conn.commit()
    finally:
        conn.close()

    # 插件树镜像：<sb>/plugins/hermes-console/dashboard + <sb>/plugins/hermes-auto-group/core.py
    mirror = target / "plugins"
    shutil.copytree(
        DASHBOARD,
        mirror / "hermes-console" / "dashboard",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "dist"),
    )
    (mirror / "hermes-auto-group").mkdir(parents=True, exist_ok=True)
    shutil.copy2(REAL_HOME / "plugins" / "hermes-auto-group" / "core.py",
                 mirror / "hermes-auto-group" / "core.py")
    return target


def build_app(dashboard_dir: Path):
    from fastapi import FastAPI

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_hermes_console", str(dashboard_dir / "plugin_api.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix=PREFIX)
    return app, module


def install_ai_stub() -> str:
    """把引擎的模型调用器换成**确定性 stub**（只有 ``HC_AI_STUB=1`` 时才装）。

    为什么必须换：dashboard 进程里没有宿主的 agent ctx，真跑会走 5s/条的模型通道；
    界面验收要的是「流程与口径」，不是模型质量。stub 只替换 ``default_caller``，
    引擎的解析/校验/聚合/落库全部还是真代码。
    """
    import autogroup_ai  # noqa: E402 - 沙盒 dashboard 目录已在 sys.path
    import api_autogroup  # noqa: E402

    home = Path(api_autogroup._home())
    core = api_autogroup._load_core()
    slugs = [str(p.get("slug")) for p in core.list_projects(home)][:3]
    if len(slugs) < 3:
        raise SystemExit("[fixture] 项目不足 3 个，AI stub 无法给出确定建议")

    def reply(payload):
        return {"text": json.dumps(payload, ensure_ascii=False), "model": "stub-model",
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}

    def caller(messages, **kwargs):
        user = ""
        for message in messages if isinstance(messages, (list, tuple)) else []:
            if isinstance(message, dict) and message.get("role") == "user":
                user = str(message.get("content") or "")
        # 只看「当前会话」那一段：项目清单里的典型会话标题会把别的会话关键词带进来
        brief = user.split("当前会话：", 1)[-1]
        text = brief
        if "kanban" in text:
            return reply({"project_slug": slugs[0], "confidence": 0.93,
                          "reason": "标题就是 kanban 调度器重构", "new_project": None})
        if "quicker" in text:
            return reply({"project_slug": slugs[1], "confidence": 0.86,
                          "reason": "quicker 动作插件快捷键冲突", "new_project": None})
        if "multi-agent" in text:
            return reply({"project_slug": slugs[2], "confidence": 0.62,
                          "reason": "多 agent 编排，中等置信度", "new_project": None})
        if "quick-send" in text:
            return reply({"project_slug": slugs[0], "confidence": 0.55,
                          "reason": "面板交互与该项目同源", "new_project": None})
        if "liveness" in text:
            return reply({"project_slug": None, "confidence": 0.22,
                          "reason": "信息太少，说不清", "new_project": None})
        if "weather" in text:
            return reply({"project_slug": None, "confidence": 0.9,
                          "reason": "天气图表取数口径",
                          "new_project": {"slug": "weather-ops", "name": "天气运维"}})
        return reply({"project_slug": None, "confidence": 0.1, "reason": "无法判断", "new_project": None})

    autogroup_ai.default_caller = lambda: caller  # type: ignore[assignment]
    return "stub(%s)" % ",".join(slugs)


class _StubResponse:
    """假 LLM 响应：只要 `.usage` 与能被 extractor 取出文本。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.usage = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}


def install_transport_stub() -> str:
    """把**传输层**（`agent.auxiliary_client.call_llm`）换成确定性 stub。

    与旧 `install_ai_stub` 的区别：不替换 caller，所以 `autogroup_model.make_caller` 的
    provider/model 解析与 `ai_calls.jsonl` 审计全部是真代码 —— 「模型配置是否生效」这类
    验收必须看这层，而不是看谁构造了 caller。
    """
    import autogroup_ai  # noqa: E402
    import api_autogroup  # noqa: E402
    from agent import auxiliary_client as ac  # noqa: E402

    home = Path(api_autogroup._home())
    core = api_autogroup._load_core()
    slugs = [str(p.get("slug")) for p in core.list_projects(home)][:3]
    if len(slugs) < 3:
        raise SystemExit("[fixture] 项目不足 3 个，AI stub 无法给出确定建议")

    def decide(user_text: str) -> str:
        brief = user_text.split("当前会话：", 1)[-1]
        # 用标题里最稳的一个词判定（沙盒会话标题与首条消息都含这些词）
        for needle, slug, conf, why in (
            ("kanban", slugs[0], 0.93, "标题就是 kanban 调度器重构"),
            ("quicker", slugs[1], 0.86, "quicker 动作插件快捷键冲突"),
            ("multi-agent", slugs[2], 0.74, "多 agent 编排"),
            ("quick-send", slugs[0], 0.80, "面板交互与该项目同源"),
            ("liveness", None, 0.22, "信息太少，说不清"),
            ("weather", None, 0.90, "天气图表取数口径"),
        ):
            if needle in brief:
                return json.dumps({"project_slug": slug, "confidence": conf, "reason": why,
                                   "new_project": None}, ensure_ascii=False)
        return json.dumps({"project_slug": None, "confidence": 0.1,
                           "reason": "无法判断", "new_project": None}, ensure_ascii=False)

    def fake_call_llm(*, task=None, provider=None, model=None, messages=None, **kwargs):
        user = ""
        for message in messages if isinstance(messages, (list, tuple)) else []:
            if isinstance(message, dict) and message.get("role") == "user":
                user = str(message.get("content") or "")
        return _StubResponse(decide(user))

    def fake_extract(resp, *args, **kwargs):
        return getattr(resp, "text", "")

    ac.call_llm = fake_call_llm  # type: ignore[assignment]
    ac.extract_content_or_reasoning = fake_extract  # type: ignore[assignment]
    # 旧接缝保留（别的老探针还在用），但不影响新路径。
    autogroup_ai.default_caller = lambda: (lambda messages, **kw: {
        "text": decide("\n".join(str(m.get("content") or "") for m in (messages or [])
                                  if isinstance(m, dict) and m.get("role") == "user")),
        "model": "stub-model", "usage": {"total_tokens": 30}})
    return "transport-stub(%s)" % ",".join(slugs)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9195)
    parser.add_argument("--sandbox", default="")
    # 「建议数 > 后端单次上限 200」的场景用：默认 0 = 老样子（3 条 suggested）
    parser.add_argument("--suggested-extra", type=int, default=0,
                        help="额外塞多少条 status=suggested 归属行（只进 assignments.db）")
    args = parser.parse_args(argv)
    if not args.sandbox:
        raise SystemExit("本夹具只支持 --sandbox（真 home 不许当靶子）")
    home = make_sandbox(Path(args.sandbox), extra_suggested=args.suggested_extra)
    os.environ["HERMES_HOME"] = str(home)
    sandbox_dashboard = home / "plugins" / "hermes-console" / "dashboard"
    for path in (str(HERMES_AGENT), str(sandbox_dashboard)):
        if path not in sys.path:
            sys.path.insert(0, path)

    import uvicorn

    app, module = build_app(sandbox_dashboard)
    failed = getattr(module, "DOMAIN_FAILURES", {}) or {}

    # 硬断言：写操作的目标必须是沙盒。探测口径和真 home 一样（hermes_home()），不是看环境变量。
    sys.path.insert(0, str(sandbox_dashboard))
    import api_autogroup  # noqa: E402

    resolved = Path(api_autogroup._home()).resolve()
    if resolved != home.resolve():
        print("[fixture] REFUSE: hermes_home()=%s 不是沙盒 %s" % (resolved, home), flush=True)
        return 3
    ai_stub = ""
    if os.environ.get("HC_AI_STUB") == "1":
        ai_stub = install_transport_stub()
        print("[fixture] ai-stub=%s" % ai_stub, flush=True)
    print("[fixture] home=%s routes=%d failed=%s" % (home, len(module.router.routes), failed or "{}"), flush=True)
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="error", access_log=False)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            print("[fixture] ready on 127.0.0.1:%d%s" % (args.port, PREFIX), flush=True)
            break
        time.sleep(0.05)
    else:
        print("[fixture] uvicorn 没起来", flush=True)
        return 2
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

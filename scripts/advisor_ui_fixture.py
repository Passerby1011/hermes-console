# -*- coding: utf-8 -*-
"""Advisor 分区 UI 验收夹具：**沙盒 home + 真 plugin_api + 真台账形状的数据**。

为什么不用真 home：本轮的验收要真的点「保存」（改设置、回读），真 home 上的
``settings.json`` 是生产状态（``enabled=false`` 是上一张卡刻意保持的），不能为了截图去动它。

做法：
  1. 建一个临时 home，按真实布局铺好 ``advisor/audit/{advice.jsonl,settings.json}``、
     ``profiles/<name>/{config.yaml,plugins/advisor,advisor/audit/reviews.jsonl}``、``logs/agent.log``；
  2. 台账**直接拷真实的那份**（真建议），所以列表里的正文、severity、目标来源都是真数据；
  3. ``reviews.jsonl`` 与 ``agent.log`` 按真实形状构造：12 条只有 ``started``（真 home 的
     code-worker 现状）+ 1 条 ``error: Error code: 504`` + 1 条 ``no_advice``；
  4. **在台账末尾追加两条结构样本行**（``target_state=none`` / ``quoted_only``，ts 取台账
     最大值 +1s/+2s），让「未点名 / 仅引用」两类一定落在列表首屏 —— 见
     ``_append_structure_rows``（真台账的最新 20 行会漂，断言不能靠运气）；
  5. 把 ``api_advisor._home`` 临时指向沙盒（只影响本进程），再起 uvicorn。

跑法（由 ``scripts/probe-advisor-ui.mjs`` 自动拉起，也可手工）::

    <venv>/python.exe plugins/hermes-console/scripts/advisor_ui_fixture.py --port 9195

换台账来源（只换输入，不改断言口径）::

    HC_ADV_LEDGER=<path>            用指定 JSONL 代替真实台账
    HC_ADV_LEDGER_MODE=all-target   不拷真台账，直接造 20 行「全部带目标」的台账
    HC_ADV_STRUCTURE_ROWS=0         关掉第 4 步的两条结构样本行（验证 SKIP 分支用）
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
HOME = PLUGIN_ROOT.parents[1]
HERMES_AGENT = HOME / "hermes-agent"
for _p in (str(HERMES_AGENT), str(DASHBOARD)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PREFIX = "/api/plugins/hermes-console"

#: 真实台账（只读拷贝，绝不写回）
REAL_LEDGER = HOME / "advisor" / "audit" / "advice.jsonl"
#: 真实评审流水（code-worker 的 13 行全是 started）
REAL_REVIEWS = HOME / "profiles" / "code-worker" / "advisor" / "audit" / "reviews.jsonl"
REAL_LOG = HOME / "logs" / "agent.log"

# ---- 台账来源开关（验收要能换输入，断言口径不变）--------------------------
#: ``HC_ADV_LEDGER``：用指定 JSONL 代替真实台账（路径不存在则退回真台账）。
LEDGER_OVERRIDE = os.environ.get("HC_ADV_LEDGER", "").strip()
#: ``HC_ADV_LEDGER_MODE=all-target``：不拷真台账，直接造 20 行「全部带目标」的台账。
#: 这是验收「断言不再靠数据运气」的对照输入：20 行里一条 target_state=none 都没有。
LEDGER_MODE = os.environ.get("HC_ADV_LEDGER_MODE", "").strip().lower()
#: ``HC_ADV_STRUCTURE_ROWS=0``：关掉结构样本行（用来验证「数据不支持 → SKIP 而不是 FAIL」）。
STRUCTURE_ROWS = os.environ.get("HC_ADV_STRUCTURE_ROWS", "1").strip().lower() not in (
    "0", "false", "no", "off")

#: 结构样本行（**故意造**的两条，正文形态由下面的常量写死）：
#:   * ``STRUCTURE_NOTE_NONE``：没有任何目标形态、也没有引号 → ``target_state=none``
#:     （面板必须打「未点名」徽标）；
#:   * ``STRUCTURE_NOTE_QUOTED``：只有引号原文 → ``target_state=quoted_only``
#:     （面板必须打「仅引用」徽标，且详情不能说「点名了」）。
#: 两段正文都不含卡 id / 会话 id / ``comment N`` / ``文件:行``，也不含四个标签行首。
STRUCTURE_NOTE_NONE = (
    "这条建议只描述了一个现象，没有写清它在评哪一件事；补上对象后再投递。"
)
STRUCTURE_NOTE_QUOTED = (
    "复核时看到「三个探针全部通过」的说法，但没有写清被复核的是哪一个对象。"
)

CONFIG_YAML = """plugins:
  enabled:
    - advisor
    - hermes-console
  entries:
    advisor:
      allow_gateway_injection: true
"""

PLUGIN_YAML = 'name: advisor\nversion: "1.4.0"\ndescription: fixture\n'


def _latest_ts(lines: list[str]) -> datetime:
    """台账里最大的 ``ts``（解析不了的行跳过）；一行都没有就用「现在」。"""
    latest: datetime | None = None
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        raw = str(row.get("ts") or "")
        try:
            value = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if latest is None or value > latest:
            latest = value
    if latest is None:
        latest = datetime.now(timezone(timedelta(hours=8)))
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone(timedelta(hours=8)))
    return latest


def _ledger_event(advice_id: str, note: str, ts: datetime, *, turn: str,
                  severity: str = "concern") -> dict:
    """一条 ``raised`` 事件（字段与真台账同形；``fixture`` 是给人和探针看的标记）。"""
    return {
        "actor": "program",
        "decision": None,
        "event": "raised",
        "id": advice_id,
        "note": note,
        "profile": "default",
        "reason": "",
        "schema_version": 1,
        "session_id": "fixture0",
        "severity": severity,
        "source": "fixture",
        "ts": ts.isoformat(timespec="milliseconds"),
        "turn_id": turn,
        "goal_source": "unknown",
        "fixture": "advisor-ui-probe",
    }


def _all_target_ledger() -> str:
    """20 行**全部带目标**的台账（``target_state=named`` 一个不少）。

    用途：验收「把台账换成 20 行全带目标的夹具时探针仍然全绿」——证明
    「未点名 / 仅引用」两条断言不再靠真台账的最新 20 行运气，而是靠夹具里
    显式造的那两条结构样本行（``_append_structure_rows``）。
    """
    base = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    rows = []
    for index in range(20):
        card = "t_%08x" % (0x1A2B0000 + index)
        note = ("目标 %s 的验收证据只有一句话，缺 impact 与下一步；"
                "建议补上受影响的交付物和复验命令。" % card)
        rows.append(_ledger_event("feedface%08x" % index, note,
                                  base - timedelta(minutes=index),
                                  turn="all-target-%02d" % index))
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"


def _append_structure_rows(path: Path) -> list[str]:
    """在台账**末尾**追加两条结构样本行，返回它们的 id。

    为什么必须在夹具里造：真台账的「最新 20 行」是会漂的 —— 默认台账在 t_5dbc60b8
    期间从 364 行涨到 372 行，最新 20 行里 ``noTarget`` 从 1 变成 0，探针于是在
    2026-11-02 09:47 变成常红（130 passed / 2 failed，``noTarget=0, quoteOnly=11``）。
    「首屏必须有未点名标记」这条断言如果只看真数据，就是看运气。

    这两行的 ts 取「台账最大 ts + 2s / +1s」，所以无论拷的是哪份台账（包括
    ``HC_ADV_LEDGER_MODE=all-target`` 的 20 行全带目标），它们都稳定排在第一页，
    断言针对的是**确定数据**。
    """
    raw = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = [line for line in raw.splitlines() if line.strip()]
    base = _latest_ts(lines)
    rows = [
        # 新的在前：未点名那条排第一行（列表按 last_seen_at 倒序）
        _ledger_event("facefeed00000001", STRUCTURE_NOTE_NONE,
                      base + timedelta(seconds=2), turn="fixture-none"),
        _ledger_event("facefeed00000002", STRUCTURE_NOTE_QUOTED,
                      base + timedelta(seconds=1), turn="fixture-quoted"),
    ]
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return [row["id"] for row in rows]


def build_sandbox() -> Path:
    """铺一个与真实布局同形的 home（数据是拷来的真台账）。"""
    root = Path(tempfile.mkdtemp(prefix="hc-adv-ui-"))
    (root / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    (root / "plugins" / "advisor").mkdir(parents=True, exist_ok=True)
    (root / "plugins" / "advisor" / "plugin.yaml").write_text(PLUGIN_YAML, encoding="utf-8")

    audit = root / "advisor" / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    ledger = audit / "advice.jsonl"
    if LEDGER_MODE == "all-target":
        ledger.write_text(_all_target_ledger(), encoding="utf-8")
        ledger_source = "synthetic:all-target(20 rows, all named)"
    elif LEDGER_OVERRIDE:
        # 指了文件就必须用那份：静默退回真台账会让人以为测的是自定义输入（假验收）。
        if not Path(LEDGER_OVERRIDE).is_file():
            raise SystemExit("[fixture] HC_ADV_LEDGER 指向的文件不存在：%s" % LEDGER_OVERRIDE)
        shutil.copyfile(Path(LEDGER_OVERRIDE), ledger)
        ledger_source = LEDGER_OVERRIDE
    elif REAL_LEDGER.is_file():
        shutil.copyfile(REAL_LEDGER, ledger)
        ledger_source = str(REAL_LEDGER)
    else:  # 兜底：没有真台账也要有两条，否则列表是空的、点不到详情
        (audit / "advice.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in (
                {"actor": "program", "decision": None, "event": "raised", "id": "aaaabbbbccccdddd",
                 "note": "「所以解依赖才让它进入 running」仍是因果性推断，现有记录最多证明解依赖后该卡处于 "
                         "running；建议改为「解依赖后观察到卡处于 running」。",
                 "profile": "default", "reason": "", "schema_version": 1,
                 "session_id": "20261029_204210_bacebf", "severity": "concern", "source": "runtime",
                 "ts": "2026-10-30T19:52:27.329+08:00", "turn_id": "t1", "goal_source": "unknown"},
                {"actor": "program", "decision": None, "event": "raised", "id": "eeeeffff00001111",
                 "note": "comment 851 的结论缺少证据：只证明了 unlink 成功。",
                 "profile": "default", "reason": "", "schema_version": 1,
                 "session_id": "20261029_204210_bacebf", "severity": "blocker", "source": "runtime",
                 "ts": "2026-10-30T20:11:02.100+08:00", "turn_id": "t2",
                 "goal_source": "kanban", "goal_ref": "t_56b4ee48"},
            )) + "\n", encoding="utf-8")
        ledger_source = "fallback(2 rows)"
    # 结构样本行：保证「未点名 / 仅引用」两类一定在首屏（不靠真台账运气）
    injected = _append_structure_rows(ledger) if STRUCTURE_ROWS else []
    print("[fixture] ledger=%s injected=%s structure_rows=%s"
          % (ledger_source, injected or "-", "on" if STRUCTURE_ROWS else "off"), flush=True)
    # 与生产一致：enabled=false（上一张卡刻意保持），其余是默认值
    (audit / "settings.json").write_text(json.dumps({
        "schema_version": 1, "enabled": False, "concern_default": "hold",
        "min_interval_seconds": 0, "model": "", "provider": "",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    # default 这个 profile 的评审流水：一条真形状的「有目标」评审记录，让
    # /advisor/modes 的 auto 推导（resolved_mode）有据可依，而不是"没有记录"的兜底。
    # 故意**不写 mode 键**到 settings.json：面板必须把「缺键 = auto = 默认层」显示出来。
    (audit / "reviews.jsonl").write_text(json.dumps({
        "schema_version": 1, "outcome": "no_advice", "review_id": "revdefault01",
        "ts": "2026-11-01T09:30:00.000+08:00", "profile": "default",
        "session_id": "s-default", "turn_id": "t-default", "task_id": "",
        "goal_source": "kanban", "goal_ref": "t_56b4ee48", "goal_matched_by": "task_id",
        "mode_setting": "auto", "mode_resolved": "goal_aligned",
        "mode_prompt_marker": "This is what the agent's turn is judged against.",
        "mode_prompt_sha256": "0" * 64, "mode_prompt_chars": 0,
        "mode_marker_in_prompt": True,
        "model": "deepseek-v4.1-flash", "provider": "balance",
        "tokens": 0, "duration_ms": 0, "advice_count": 0,
    }, ensure_ascii=False) + "\n", encoding="utf-8")

    worker = root / "profiles" / "code-worker"
    (worker / "plugins" / "advisor").mkdir(parents=True, exist_ok=True)
    (worker / "plugins" / "advisor" / "plugin.yaml").write_text(PLUGIN_YAML, encoding="utf-8")
    (worker / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    worker_audit = worker / "advisor" / "audit"
    worker_audit.mkdir(parents=True, exist_ok=True)
    (worker_audit / "settings.json").write_text(json.dumps({
        "schema_version": 1, "enabled": False, "concern_default": "hold",
        "min_interval_seconds": 0, "model": "", "provider": "",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    records = []
    if REAL_REVIEWS.is_file():
        for line in REAL_REVIEWS.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    if not records:
        records = [{"outcome": "started", "review_id": "seed%04d" % i,
                    "ts": "2026-10-31T0%d:00:00.000+08:00" % (i % 9), "profile": "code-worker",
                    "model": "deepseek-v4.1-flash", "provider": "balance", "duration_ms": 0,
                    "advice_count": 0, "turn_id": "seed"} for i in range(6)]
    # 真台账里补三类终态，UI 才有东西可显示：504 错误 / 无建议 / 有建议
    records.extend([
        {"schema_version": 1, "outcome": "error", "review_id": "err504aaaa", "profile": "code-worker",
         "ts": "2026-10-30T18:54:47.055+08:00", "model": "长庚", "provider": "moa",
         "duration_ms": 180000, "advice_count": 0, "tokens": 0,
         "error": "Error code: 504", "skip_reason": "",
         "turn_id": "20261029_204210_bacebf:20261029_204210_bacebf:b1ebcf81"},
        {"schema_version": 1, "outcome": "no_advice", "review_id": "ok0000aaaa", "profile": "code-worker",
         "ts": "2026-10-31T01:02:03.000+08:00", "model": "deepseek-v4.1-flash", "provider": "balance",
         "duration_ms": 2400, "advice_count": 0, "tokens": 812, "error": "", "skip_reason": "",
         "turn_id": "x:y:z"},
        {"schema_version": 1, "outcome": "advised", "review_id": "adv0000aaaa", "profile": "code-worker",
         "ts": "2026-10-31T01:05:00.000+08:00", "model": "deepseek-v4.1-flash", "provider": "balance",
         "duration_ms": 3100, "advice_count": 1, "tokens": 940, "error": "", "skip_reason": "",
         "turn_id": "x:y:w"},
    ])
    (worker_audit / "reviews.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in records) + "\n", encoding="utf-8")

    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    if REAL_LOG.is_file():
        hits = [line for line in REAL_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
                if "Advisor review failed for turn" in line]
    else:
        hits = []
    if not hits:
        hits = [
            "2026-10-30 18:54:47,055 WARNING hermes_plugins.advisor.runtime: "
            "Advisor review failed for turn 20261029_204210_bacebf:20261029_204210_bacebf:b1ebcf81: "
            "Error code: 504",
        ]
    # 日志里放两条（一条来自真日志，一条同形状）：证明「次数统计」不是写死的 1
    extra = hits[0].replace("18:54:47,055", "19:31:02,120").replace("b1ebcf81", "c2fda092")
    (logs / "agent.log").write_text("\n".join(hits + [extra]) + "\n", encoding="utf-8")

    # ---- 可操作覆盖矩阵的四态夹具（t_72e9a7fa）：reviewing / disabled / missing ----
    # default 已是 active（装了 + 启用 + 允许投递 + 有台账），code-worker 是 reviewing_only。
    def _mk_profile(name: str, *, installed: bool, enabled: bool, sessions: int = 0,
                    injection: bool = False, review_enabled=None) -> None:
        phome = root / "profiles" / name
        (phome / "advisor" / "audit").mkdir(parents=True, exist_ok=True)
        if installed:
            (phome / "plugins" / "advisor").mkdir(parents=True, exist_ok=True)
            (phome / "plugins" / "advisor" / "plugin.yaml").write_text(PLUGIN_YAML, encoding="utf-8")
        cfg = "plugins:\n"
        cfg += "  enabled:\n    - advisor\n" if enabled else "  enabled: []\n"
        cfg += "  entries:\n    advisor:\n      allow_gateway_injection: %s\n" % (
            "true" if injection else "false")
        (phome / "config.yaml").write_text(cfg, encoding="utf-8")
        if review_enabled is not None:
            (phome / "advisor" / "audit" / "settings.json").write_text(json.dumps({
                "schema_version": 1, "enabled": bool(review_enabled), "concern_default": "hold",
                "min_interval_seconds": 0, "model": "", "provider": "",
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        if sessions:
            (phome / "advisor" / "sessions").mkdir(parents=True, exist_ok=True)
            for index in range(sessions):
                (phome / "advisor" / "sessions" / ("s%d.json" % index)).write_text(
                    json.dumps({"session_id": "s%d" % index, "held_notes": []}), encoding="utf-8")

    # reviewing：装了 + 启用 + 有 sessions 无台账 → reviewing_only
    _mk_profile("reviewing", installed=True, enabled=True, sessions=1)
    # disabled：装了但 config 没启用 → installed_disabled（可测 enable/disable 插件、启用评审被门挡）
    _mk_profile("disabled", installed=True, enabled=False)
    # missing：连插件目录都没有 → not_installed（可测 install / 同步 / 卸载）
    _mk_profile("missing", installed=False, enabled=False)
    # full：四件事全开（安装 + 插件启用 + 评审开 + 允许注入）→ 卡片收起态应没有主操作
    _mk_profile("full", installed=True, enabled=True, sessions=1, injection=True, review_enabled=True)
    # work：**设置值与 default 明确不同**的 agent（默认 set 里没有 mode 键 → auto；
    # 这里显式写 mode=conservative、concern_default=deliver、enabled=true），
    # 用来证明「切 agent 后表单显示的是它自己的值」，而不是上一个 agent 的残留。
    _mk_profile("work", installed=True, enabled=True, sessions=2, injection=True, review_enabled=True)
    work_settings = root / "profiles" / "work" / "advisor" / "audit" / "settings.json"
    work_settings.write_text(json.dumps({
        "schema_version": 1, "enabled": True, "concern_default": "deliver",
        "min_interval_seconds": 45, "model": "", "provider": "", "mode": "conservative",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return root


def build_app(sandbox: Path):
    from fastapi import FastAPI

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_hermes_console", str(DASHBOARD / "plugin_api.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    import api_advisor

    # 先把**真的**台账模块加载进 sys.modules，再改 home：
    # 沙盒里的 ``plugins/advisor`` 只有 plugin.yaml（为了让「装没装」判定为真），
    # 如果让 ``import advisor.audit`` 先撞上它，会得到没有 fold / get_settings 的假模块。
    # 生产里插件本来就已经在进程里加载过，这里复现的正是那个状态。
    if str(HOME / "plugins") not in sys.path:
        sys.path.insert(0, str(HOME / "plugins"))
    import advisor.audit  # noqa: F401  （真模块进 sys.modules）

    api_advisor._home = lambda: sandbox  # type: ignore[assignment]
    try:
        loaded = api_advisor._load_audit()
        print("[fixture] audit=%s file=%s fold=%s get_settings=%s" % (
            loaded, getattr(loaded, "__file__", "?"), hasattr(loaded, "fold"),
            hasattr(loaded, "get_settings")), flush=True)
    except Exception as exc:  # noqa: BLE001
        print("[fixture] audit load failed: %s: %s" % (type(exc).__name__, exc), flush=True)
    app = FastAPI()
    app.include_router(module.router, prefix=PREFIX)
    return app, module


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9195)
    args = parser.parse_args(argv)

    import uvicorn

    sandbox = build_sandbox()
    app, module = build_app(sandbox)
    print("[fixture] sandbox=%s" % sandbox, flush=True)
    print("[fixture] routes=%d failed=%s" % (len(module.router.routes),
                                             getattr(module, "DOMAIN_FAILURES", {}) or "{}"), flush=True)
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="error", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
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
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

# -*- coding: utf-8 -*-
"""自动命名面板 · **真 home 只读**对账探针。

与 ``probe-autotitler.mjs`` 的分工：

* 那个探针在**沙盒**里跑浏览器，证明"界面能正确渲染与写回"（数据是确定的合成数据）；
* 本探针打**真 home**，只调两个 GET，把面板的计数与**同一份日志的 grep 结果**逐类比对 ——
  卡片验收 B 第 1 条要的就是这个"可脚本比对"。

它同时是"面板没有偷偷写东西"的证据：跑之前与跑之后把
``config.yaml`` / ``state.json`` / 插件 ``config.yaml`` 的 sha256 各记一次，必须完全相同。

跑法::

    <venv>/python.exe plugins/hermes-console/scripts/probe_autotitler.py
    <venv>/python.exe plugins/hermes-console/scripts/probe_autotitler.py --agent code-worker
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
REAL_HOME = PLUGIN_ROOT.parents[1]
HERMES_AGENT = REAL_HOME / "hermes-agent"

for path in (str(DASHBOARD), str(HERMES_AGENT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PASS = 0
FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS
    if ok:
        PASS += 1
        print("  PASS %s%s" % (name, ("  — " + extra) if extra else ""))
    else:
        FAILED.append(name + (("  — " + extra) if extra else ""))
        print("  FAIL %s%s" % (name, ("  — " + extra) if extra else ""))


def sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "<missing>"


def grep_counts(paths: list[Path]) -> tuple[dict, int]:
    """与面板**同口径**的 grep：按 audit 行的 act= 计数 + invalid payload 条数。"""
    counts: dict[str, int] = {}
    invalid = 0
    audit = re.compile(r"auto-titler audit:\s+sid=\S+\s+act=(\w+)")
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for act in audit.findall(text):
            counts[act] = counts.get(act, 0) + 1
        invalid += len(re.findall(r"model returned invalid decision payload", text))
    return counts, invalid


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", default="default")
    args = parser.parse_args(argv)
    agent = args.agent

    import api_autotitler

    app = FastAPI()
    app.include_router(api_autotitler.router)
    client = TestClient(app)

    home = REAL_HOME if agent == "default" else REAL_HOME / "profiles" / agent
    watched = [
        home / "config.yaml",
        home / "plugin-data" / "hermes-auto-titler" / "state.json",
        REAL_HOME / "plugins" / "hermes-auto-titler" / "config.yaml",
    ]
    before = [sha(path) for path in watched]

    print("== GET /autotitler/settings?agent=%s ==" % agent)
    import time as _time

    _t0 = _time.perf_counter()
    resp = client.get("/autotitler/settings", params={"agent": agent})
    _dt = (_time.perf_counter() - _t0) * 1000
    check("settings 返回 200", resp.status_code == 200, "status=%d  %.0f ms" % (resp.status_code, _dt))
    data = resp.json().get("data") or {}
    check("参数个数 = 23（插件 config_schema）", data.get("field_count") == 23, str(data.get("field_count")))
    check("版本读到 0.3.0", data.get("version") == "0.3.0", str(data.get("version")))
    check(
        "三个 hook 都声明了",
        data.get("declared_hooks") == ["pre_llm_call", "on_session_end", "on_session_finalize"],
        str(data.get("declared_hooks")),
    )
    keys = [field["key"] for field in data.get("fields") or []]
    check("每个参数都带类型与来源层", all(f.get("type") and f.get("source") for f in data.get("fields") or []))
    from collections import Counter

    sources = Counter(field["source"] for field in data.get("fields") or [])
    print("     来源层分布：%s" % dict(sources))

    print("== GET /autotitler/overview?agent=%s ==" % agent)
    _t0 = _time.perf_counter()
    resp = client.get("/autotitler/overview", params={"agent": agent, "recent": 5})
    _dt = (_time.perf_counter() - _t0) * 1000
    check("overview 返回 200", resp.status_code == 200, "status=%d  %.0f ms" % (resp.status_code, _dt))
    overview = resp.json().get("data") or {}
    health = overview.get("health") or {}
    counts = health.get("counts") or {}

    # 用**面板自己报的文件清单**去 grep，这样"读了哪几个文件"没有含糊空间
    files = [Path(item["path"]) for item in health.get("files") or [] if item.get("bytes")]
    expected, invalid_expected = grep_counts(files)
    for act in ("renamed", "failed", "skipped", "keep", "pending", "throttled"):
        check(
            "计数 %-9s 与 grep 一致" % act,
            counts.get(act, 0) == expected.get(act, 0),
            "panel=%s grep=%s" % (counts.get(act, 0), expected.get(act, 0)),
        )
    check(
        "invalid payload 条数与 grep 一致",
        health.get("invalid_payload_total") == invalid_expected,
        "panel=%s grep=%s" % (health.get("invalid_payload_total"), invalid_expected),
    )
    decisions = counts.get("renamed", 0) + counts.get("failed", 0)
    rate = health.get("failure_rate")
    check(
        "失败率 = 失败/(改名+失败)",
        (rate is None and decisions == 0) or (rate is not None and abs(rate - counts.get("failed", 0) / decisions) < 1e-9),
        "rate=%s decisions=%s" % (rate, decisions),
    )

    classes = {item["class"]: item["count"] for item in health.get("failure_classes") or []}
    check(
        "失败原因分类之和 = 失败数",
        sum(classes.values()) == counts.get("failed", 0),
        str(classes),
    )
    if health.get("diagnosis"):
        print("     诊断：%s" % health["diagnosis"]["headline"])
        check("诊断指向插件侧（不是面板）", health["diagnosis"].get("panel_ok") is True)

    # 重试队列：与 state.json 直接对账
    import json

    state_path = home / "plugin-data" / "hermes-auto-titler" / "state.json"
    expected_queue = 0
    if state_path.is_file():
        sessions = (json.loads(state_path.read_text(encoding="utf-8")) or {}).get("sessions") or {}
        for record in sessions.values():
            if isinstance(record, dict):
                expected_queue += sum(1 for block in ("finalize", "retry") if isinstance(record.get(block), dict))
    retry = overview.get("retry") or {}
    check(
        "重试队列条目数 = state.json 的 finalize+retry 块数",
        retry.get("count") == expected_queue,
        "panel=%s state=%s" % (retry.get("count"), expected_queue),
    )

    runtime = overview.get("runtime") or {}
    if state_path.is_file():
        sessions = (json.loads(state_path.read_text(encoding="utf-8")) or {}).get("sessions") or {}
        check(
            "跟踪中的会话数 = state.json sessions 条数",
            runtime.get("sessions_total") == len(sessions),
            "panel=%s state=%s" % (runtime.get("sessions_total"), len(sessions)),
        )
        ids = {row["session_id"] for row in runtime.get("sessions") or []}
        check("每会话明细能对上真实会话 id", ids == set(sessions), "%d 条" % len(ids))

    # ---- 时间窗口：历史 vs 当前（本卡核心） ----
    print("== 时间区间（1h / 24h / all） ==")
    windows = health.get("windows") or {}
    check("三个区间都返回", list(windows) == ["1h", "24h", "all"], str(list(windows)))
    for wid in ("1h", "24h", "all"):
        view = windows.get(wid) or {}
        check(
            "区间 %-3s 带时间范围与最后更新时间" % wid,
            bool(view.get("from_ts") or wid == "all") and bool(view.get("to_ts")) and bool(health.get("generated_at")),
            "%s → %s" % (view.get("from_ts") or "(不限)", view.get("to_ts")),
        )
    one = (windows.get("1h") or {}).get("counts") or {}
    day = (windows.get("24h") or {}).get("counts") or {}
    every = (windows.get("all") or {}).get("counts") or {}
    print(
        "     1h: failed=%s renamed=%s total=%s | 24h: failed=%s renamed=%s total=%s | all: failed=%s renamed=%s total=%s"
        % (
            one.get("failed"), one.get("renamed"), one.get("total"),
            day.get("failed"), day.get("renamed"), day.get("total"),
            every.get("failed"), every.get("renamed"), every.get("total"),
        )
    )
    check("24h / all 的失败数不小于 1h（窗口是包含关系）",
          (one.get("failed") or 0) <= (day.get("failed") or 0) <= (every.get("failed") or 0))
    check("窗口切换真的改变数字（1h ≠ all 或 24h ≠ all）",
          one.get("total") != every.get("total") or day.get("total") != every.get("total"),
          "1h=%s 24h=%s all=%s" % (one.get("total"), day.get("total"), every.get("total")))

    baseline = health.get("baseline") or {}
    if baseline.get("available"):
        before_failed = (baseline.get("before") or {}).get("counts", {}).get("failed")
        after_failed = (baseline.get("after") or {}).get("counts", {}).get("failed")
        print("     基线 %s（%s）：%s" % (baseline.get("ts"), baseline.get("source_label"), baseline.get("headline")))
        check(
            "基线前后失败数之和 = 全部失败数",
            (before_failed or 0) + (after_failed or 0) == (every.get("failed") or 0),
            "%s + %s vs %s" % (before_failed, after_failed, every.get("failed")),
        )
        check("基线时间线非空", bool(baseline.get("timeline")), "%d 个桶" % len(baseline.get("timeline") or []))

    # ---- 运行日志（只读 + 脱敏） ----
    print("== 运行日志（GET /autotitler/logs） ==")
    _t0 = _time.perf_counter()
    logs_resp = client.get("/autotitler/logs", params={"agent": agent, "limit": 200})
    _dt = (_time.perf_counter() - _t0) * 1000
    check("logs 返回 200", logs_resp.status_code == 200, "status=%d  %.0f ms" % (logs_resp.status_code, _dt))
    logs = logs_resp.json().get("data") or {}
    rows = logs.get("rows") or []
    check("audit 行数 ≥ 5", len(rows) >= 5, "rows=%d" % len(rows))
    check("按时间倒序", [r["ts"] for r in rows] == sorted((r["ts"] for r in rows), reverse=True))
    check("每行只带 sid/act/reason/dur_ms/ts/class",
          all(set(r) <= {"ts", "epoch", "sid", "act", "reason", "dur_ms", "class"} for r in rows))
    check("脱敏标记", logs.get("redacted") is True)
    blob = json.dumps(logs, ensure_ascii=False)
    check("响应里没有日志原文（不含 'INFO hermes_auto_titler'）", "INFO hermes_auto_titler" not in blob)
    acts = {item["act"]: item["count"] for item in logs.get("acts") or []}
    for act in ("failed", "renamed", "keep", "skipped"):
        check("logs 的 %s 计数与 grep 一致" % act, acts.get(act, 0) == expected.get(act, 0),
              "logs=%s grep=%s" % (acts.get(act, 0), expected.get(act, 0)))
    failed_only = client.get("/autotitler/logs", params={"agent": agent, "act": "failed", "limit": 50}).json()["data"]
    check("act=failed 过滤只剩失败行", {r["act"] for r in failed_only.get("rows") or []} == {"failed"} if failed_only.get("rows") else True)
    print("     最近 5 条（脱敏）：")
    for row in rows[:5]:
        print("       %s  sid=%-12s act=%-9s dur=%-6s reason=%s"
              % (row["ts"], row["sid"], row["act"], row["dur_ms"], row["reason"] or "-"))

    # 日志端点只读：不许有 PUT/POST/DELETE
    import api_autotitler as _mod

    methods = set()
    for route in _mod.router.routes:
        if getattr(route, "path", "") == "/autotitler/logs":
            methods |= set(getattr(route, "methods", set()) or set())
    check("logs 只有 GET（没有删除/清空入口）", methods == {"GET"}, str(sorted(methods)))

    after = [sha(path) for path in watched]
    check("整轮读操作没有写任何文件", before == after)

    print("\n%d passed, %d failed" % (PASS, len(FAILED)))
    for item in FAILED:
        print("  - " + item)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

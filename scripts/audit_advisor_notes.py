# -*- coding: utf-8 -*-
"""D 项验收：抓 3 条**真实**反馈，逐条检查 target / concern / evidence 结构。

用的是面板同一条代码路径（``api_advisor._note_structure``），所以结论与界面上
「结构自检」一致 —— 不是另写一套标准，也不放宽判定。

target 口径**与插件闸门一致**（``plugins/advisor/models.py::_TARGET_PATTERNS``：卡 id /
会话 id / ``comment N`` / ``文件:行``）。引号原文只作参考、不算点名，所以统计里分别报
「四形态」与「含引号」两个数 —— 后者是面板旧口径，闸门不认。

跑法::

    <venv>/python.exe plugins/hermes-console/scripts/audit_advisor_notes.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
HOME = PLUGIN_ROOT.parents[1]
for extra in (DASHBOARD, HOME / "plugins", HOME / "hermes-agent"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

spec = importlib.util.spec_from_file_location(
    "hermes_dashboard_plugin_hermes_console", str(DASHBOARD / "plugin_api.py")
)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

import api_advisor  # noqa: E402

audit = api_advisor._load_audit()
items = []
for profile, directory in api_advisor.profile_audit_dirs():
    events, _ = api_advisor._read_jsonl(directory / api_advisor.LEDGER_FILE)
    supported = set(getattr(audit, "EVENTS", ()))
    events = [event for event in events if event.get("event") in supported]
    for item in audit.fold(events).values():
        source, ref = api_advisor._goal_of(item)
        items.append({
            "id": item.get("id", ""),
            "note": item.get("note", ""),
            "severity": item.get("severity", ""),
            "profile": profile,
            "session": item.get("session_id", ""),
            "ts": item.get("last_seen_at", ""),
            "goal_source": source,
            "goal_ref": ref,
        })

print("台账条数:", len(items))
structures = [api_advisor._note_structure(item["note"]) for item in items]
named = sum(1 for s in structures if s["target_state"] == api_advisor.TARGET_NAMED)
quoted_only = sum(1 for s in structures if s["target_state"] == api_advisor.TARGET_QUOTED_ONLY)
none = sum(1 for s in structures if s["target_state"] == api_advisor.TARGET_NONE)
# 两个口径都报，别让人把「面板口径」当成「闸门口径」：
#   四形态 = 插件闸门（plugins/advisor/models.py::_TARGET_PATTERNS）认的点名，决定能不能投递；
#   含引号 = 面板旧口径（多一条 quoted），只作参考，闸门不认。
print("点名对象 · 四形态（插件闸门口径：卡 id / 会话 id / comment N / 文件:行）:", named,
      "/", len(items))
print("点名对象 · 含引号（面板旧口径，只作参考，闸门不认）:", named + quoted_only, "/", len(items))
print("  其中 只引了原话（闸门会拒）:", quoted_only)
print("  其中 完全没点名:", none)
print("带 goal/evidence/impact/next 标签:", sum(1 for s in structures if s["labels"]))
print("五项齐全:", sum(1 for s in structures if s["complete"]))
print()

# 挑四条有代表性的：最长的、最短的、一条只引了原话的、一条完全没点名的
by_len = sorted(range(len(items)), key=lambda i: len(items[i]["note"]))
quoted_idx = [i for i, s in enumerate(structures) if s["target_state"] == api_advisor.TARGET_QUOTED_ONLY]
none_idx = [i for i, s in enumerate(structures) if s["target_state"] == api_advisor.TARGET_NONE]
picks = []
for idx in (by_len[-1], by_len[0],
            quoted_idx[0] if quoted_idx else by_len[len(by_len) // 2],
            none_idx[0] if none_idx else by_len[len(by_len) // 2]):
    if idx not in picks:
        picks.append(idx)

TARGET_STATE_TEXT = {
    api_advisor.TARGET_NAMED: "点名了（闸门放行）",
    api_advisor.TARGET_QUOTED_ONLY: "只引了原话（闸门会拒）",
    api_advisor.TARGET_NONE: "没点名",
}

for order, idx in enumerate(picks, 1):
    item = items[idx]
    structure = structures[idx]
    print("=" * 76)
    print("样本 %d · id=%s · severity=%s · profile=%s · %s"
          % (order, item["id"], item["severity"], item["profile"], item["ts"]))
    print("目标来源: %s %s" % (item["goal_source"], item["goal_ref"]))
    print("正文（%d 字）:" % len(item["note"]))
    print(item["note"])
    print("-- 结构自检 --")
    print("  target  :", TARGET_STATE_TEXT.get(structure["target_state"], structure["target_state"]),
          "· 四形态命中:", ", ".join("%s「%s」" % (t["kind"], t["text"]) for t in structure["targets"]) or "无",
          "· 引号参考:", ", ".join("「%s」" % q["text"] for q in structure.get("quotes") or []) or "无")
    print("  goal    :", "有" if "goal" in structure["labels"] else "缺")
    print("  evidence:", "有" if "evidence" in structure["labels"] else "缺")
    print("  impact  :", "有" if "impact" in structure["labels"] else "缺")
    print("  next    :", "有" if "next" in structure["labels"] else "缺")
    print("  缺失项  :", ", ".join(structure["missing"]) or "无")
    print()

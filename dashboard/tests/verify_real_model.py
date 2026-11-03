# -*- coding: utf-8 -*-
"""**真实配置通道**验收（不是 mock）：沙盒 10 条合成会话 → 引擎 → 预览 → 建项目 → 确认 → 回读 → 撤销。

为什么单独一个脚本而不是 unittest：它会真的调用宿主 aux 通道（``title_generation`` slot，
provider/model 由真实配置决定），耗时长且依赖外部模型；模型不可用时**必须如实报告阻塞**，
不能算作「确定性测试通过」。确定性链路见 ``tests/test_autogroup_ai_integration.py``（mock 模型）。

运行（在 ``plugins/hermes-console/dashboard`` 下）::

    python -m tests.verify_real_model --out D:\\...\\evidence\\real_model.json

证据只写 ``--out`` 指定的文件 + 沙盒临时目录，**绝不碰真实 home**。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DASH = Path(__file__).resolve().parents[1]
if str(DASH) not in sys.path:
    sys.path.insert(0, str(DASH))

import autogroup_ai as ai  # noqa: E402
import api_autogroup_ai_apply as apply_api  # noqa: E402
import api_autogroup_ai_undo as undo_api  # noqa: E402
from tests import synthetic_cases as cases  # noqa: E402
from tests.ai_sandbox import Sandbox, body, status_of  # noqa: E402


def desensitize(value: Any, sandbox_root: str) -> Any:
    """把沙盒绝对路径抹成 ``<SANDBOX>``；字符串过长截断。合成数据本身无隐私。"""
    if isinstance(value, str):
        text = value.replace(str(sandbox_root), "<SANDBOX>")
        text = text.replace(str(sandbox_root).replace("\\", "/"), "<SANDBOX>")
        return text if len(text) <= 400 else text[:399] + "…"
    if isinstance(value, list):
        return [desensitize(v, sandbox_root) for v in value]
    if isinstance(value, dict):
        return {k: desensitize(v, sandbox_root) for k, v in value.items()}
    return value


def build_sandbox() -> Sandbox:
    """10 条**人工合成**会话 + 3 个合成项目，全部落在临时 home 里。"""
    sb = Sandbox(applied_fixture=2)  # 2 条「受保护的既有 applied」用于验证不被改动
    for slug, name in cases.PROJECTS:
        sb.add_project(slug, name)
    for row in cases.SESSIONS:
        sb.add_session(row["id"], row["title"], cases.cwd_for(row, sb.root),
                       message_count=max(4, len(row["messages"]) + 1))
        sb.add_dialog(row["id"], *row["messages"])
    return sb


def compare(item: Dict[str, Any], expected: Dict[str, Any]) -> Dict[str, Any]:
    """引擎输出 vs 人工预期标签（逐项，不掩盖分歧）。"""
    suggested = item.get("suggested") or {}
    new_project = item.get("new_project") or suggested.get("new_project") or {}
    got_slug = item.get("project_slug") or suggested.get("project_slug")
    got_new = (new_project or {}).get("slug") if isinstance(new_project, dict) else None
    confidence = item.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        confidence = None
    got_tier = item.get("tier") or (ai.tier_for(float(confidence)) if confidence is not None else None)
    kind = "existing" if got_slug else ("new_project" if got_new else "unjudged")
    return {
        "expected_kind": expected["kind"], "got_kind": kind, "kind_match": kind == expected["kind"],
        "expected_slug": expected.get("project_slug"), "got_slug": got_slug,
        "slug_match": (got_slug == expected.get("project_slug")),
        "expected_new_slug": expected.get("new_slug"), "got_new_slug": got_new,
        "new_slug_match": (got_new == expected.get("new_slug")),
        "expected_tier": expected["tier"], "got_tier": got_tier, "tier_match": got_tier == expected["tier"],
        "confidence": confidence, "action": item.get("action"),
        "writable": item.get("writable"),
    }



def run_engine(sb: Sandbox) -> Dict[str, Any]:
    """跑引擎（真实调用器）。返回 preview + 逐条对比。"""
    caller = ai.default_caller()
    if not callable(caller):
        return {"blocked": "default_caller 不可用：宿主 aux 通道未注册，无法做真实模型验收"}
    started = time.time()
    preview = ai.build_preview(sb.home, limit=len(cases.SESSIONS), caller=caller)
    elapsed = round(time.time() - started, 2)
    if not isinstance(preview, dict) or preview.get("error"):
        return {"blocked": "引擎返回错误：%s" % json.dumps(desensitize(preview, str(sb.root)),
                                                          ensure_ascii=False)[:400]}
    rows: List[Dict[str, Any]] = []
    for item in preview.get("items") or []:
        sid = str(item.get("session_id"))
        expected = (cases.BY_ID.get(sid) or {}).get("expected") or {}
        row = {"session_id": sid, "title": item.get("title"),
               "engine_tier": item.get("tier"), "engine_action": item.get("action"),
               "engine_writable": item.get("writable"),
               "reason": (item.get("reason") or "")[:120],
               "model_raw_excerpt": (item.get("model") or {}).get("raw_excerpt"),
               "model": {k: (item.get("model") or {}).get(k)
                         for k in ("provider", "model", "elapsed_ms", "usage_real")},
               "support_session_ids": item.get("support_session_ids") or []}
        row["compare"] = compare(item, expected) if expected else {"missing_expected": True}
        rows.append(row)
    agree = sum(1 for r in rows if (r["compare"].get("kind_match") and r["compare"].get("tier_match")))
    return {"blocked": None, "elapsed_s": elapsed, "preview": preview, "rows": rows,
            "agreement": {"matched": agree, "total": len(rows)}}


def snapshot_state(sb: Sandbox) -> Dict[str, Any]:
    """沙盒内「归属行 + cwd」全量快照，用于撤销前后逐条对比。"""
    out: Dict[str, Any] = {}
    for row in cases.SESSIONS:
        sid = row["id"]
        out[sid] = {"assignment": sb.assignment(sid), "cwd": sb.cwd_of(sid)}
    return out


def run_chain(sb: Sandbox, preview: Dict[str, Any]) -> Dict[str, Any]:
    """引擎输出 → 预览落库 → 建项目 → 确认写入 → 回读 → 撤销 → 逐条对比。"""
    chain: Dict[str, Any] = {}
    before = snapshot_state(sb)

    resp = apply_api.post_ai_preview({"preview": preview, "note": "真实模型验收（合成数据）"})
    data = body(resp)
    chain["preview"] = {"status": status_of(resp), "ok": data.get("ok"),
                        "batch_id": (data.get("data") or {}).get("batch_id"),
                        "error": (data.get("error") or {}).get("code")}
    batch_id = chain["preview"]["batch_id"]
    if not batch_id:
        return {"chain": chain, "blocked": "预览落库失败，后续链路无法继续"}

    annotated = body(apply_api.get_ai_preview(batch_id)).get("data") or {}
    items = annotated.get("items") or []
    chain["batch_items"] = [{"session_id": i["session_id"], "tier": i.get("tier"),
                             "selectable": i.get("selectable"),
                             "selectable_why": i.get("selectable_why"),
                             "target_slug": i.get("target_slug")} for i in items]

    new_slugs = sorted({((i.get("suggested") or {}).get("new_project") or {}).get("slug")
                        for i in items} - {None})
    chain["new_project_candidates"] = new_slugs
    for slug in new_slugs:
        created = body(apply_api.post_ai_project({"batch_id": batch_id, "slug": slug,
                                                 "confirm": True}))
        chain["project_create:" + slug] = {
            "status": status_of(apply_api.post_ai_project({"batch_id": batch_id, "slug": slug,
                                                          "confirm": False})),
            "ok": created.get("ok"), "error": (created.get("error") or {}).get("code"),
            "path": ((created.get("data") or {}).get("project") or {}).get("path"),
            "support_count": (created.get("data") or {}).get("support_count")}

    annotated = body(apply_api.get_ai_preview(batch_id)).get("data") or {}
    selected = [i["session_id"] for i in (annotated.get("items") or []) if i.get("selectable")]
    chain["selected"] = selected
    confirmed = body(apply_api.post_ai_confirm({"batch_id": batch_id, "confirm": True,
                                               "selected": selected}))
    chain["confirm"] = {"ok": confirmed.get("ok"),
                        "error": (confirmed.get("error") or {}).get("code"),
                        "written": (confirmed.get("data") or {}).get("written"),
                        "status": (confirmed.get("data") or {}).get("status"),
                        "paused": (confirmed.get("data") or {}).get("paused")}
    chain["readback"] = body(apply_api.get_ai_readback(batch_id)).get("data") or {}
    after_write = snapshot_state(sb)

    undo = body(undo_api.post_ai_undo({"batch_id": batch_id, "confirm": True}))
    chain["undo"] = {"ok": undo.get("ok"), "error": (undo.get("error") or {}).get("code"),
                     "status": (undo.get("data") or {}).get("status"),
                     "restored": len((undo.get("data") or {}).get("restored") or []),
                     "project_cleanup": (undo.get("data") or {}).get("project_cleanup")}
    after_undo = snapshot_state(sb)

    diffs: List[Dict[str, Any]] = []
    for sid in before:
        was, now = before[sid], after_undo[sid]
        same_assign = was["assignment"] == now["assignment"]
        same_cwd = was["cwd"] == now["cwd"]
        if not (same_assign and same_cwd):
            diffs.append({"session_id": sid, "assign_restored": same_assign, "cwd_restored": same_cwd,
                          "before": was, "after": now})
    chain["undo_field_compare"] = {"sessions": len(before), "mismatched": diffs,
                                   "identical": len(before) - len(diffs)}
    chain["wrote_then_undone"] = {
        sid: {"after_write": after_write[sid], "after_undo": after_undo[sid]}
        for sid in selected}
    return {"chain": chain, "blocked": None}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="真实配置通道验收（沙盒 + 合成数据）")
    ap.add_argument("--out", default="", help="证据 JSON 落盘路径（默认写到当前目录）")
    args = ap.parse_args(argv)
    started = time.time()
    sb = build_sandbox()
    evidence: Dict[str, Any] = {
        "kind": "real_model_acceptance", "synthetic_data": cases.SYNTH_NOTE,
        "started_at": started, "mock_used": False,
        "sessions": [{"session_id": r["id"], "title": r["title"],
                      "expected": r["expected"]} for r in cases.SESSIONS],
    }
    with sb:
        engine = run_engine(sb)
        evidence["engine"] = {"blocked": engine.get("blocked"), "elapsed_s": engine.get("elapsed_s"),
                              "agreement": engine.get("agreement")}
        if engine.get("blocked"):
            evidence["blocked"] = engine["blocked"]
            evidence["verdict"] = "BLOCKED_EXTERNAL_MODEL"
        else:
            preview = engine["preview"]
            evidence["model_meta"] = {"provider": (preview.get("model") or {}).get("provider"),
                                      "model": (preview.get("model") or {}).get("model"),
                                      "usage": preview.get("usage"),
                                      "engine_info": preview.get("engine"),
                                      "counts": preview.get("counts"),
                                      "scope": {k: (preview.get("scope") or {}).get(k)
                                                for k in ("considered", "total_available",
                                                          "excluded_applied", "excluded_rejected",
                                                          "projects")},
                                      "writes": preview.get("writes"),
                                      "notes": preview.get("notes")}

            evidence["rows"] = engine["rows"]
            chain = run_chain(sb, preview)
            evidence["chain"] = chain.get("chain")
            if chain.get("blocked"):
                evidence["blocked"] = chain["blocked"]
                evidence["verdict"] = "CHAIN_BLOCKED"
            else:
                evidence["verdict"] = "REAL_MODEL_CHAIN_OK"
    evidence["elapsed_s"] = round(time.time() - started, 2)
    out = Path(args.out) if args.out else Path("real_model_evidence.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(desensitize(evidence, str(sb.root)), ensure_ascii=False, indent=2),
                   encoding="utf-8")
    summary = {"verdict": evidence.get("verdict"), "out": str(out),
               "agreement": evidence.get("engine", {}).get("agreement"),
               "blocked": evidence.get("blocked"),
               "written": ((evidence.get("chain") or {}).get("confirm") or {}).get("written"),
               "undo_mismatch": len(((evidence.get("chain") or {}).get("undo_field_compare")
                                     or {}).get("mismatched") or [])}
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if evidence.get("verdict") == "REAL_MODEL_CHAIN_OK" else 3


if __name__ == "__main__":
    raise SystemExit(main())


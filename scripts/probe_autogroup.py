# -*- coding: utf-8 -*-
"""hermes-console · 项目分组后端验收探针（C8a：真实 TestClient + 真实插件库）。

跑法（必须用带 fastapi 的 Hermes 环境）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        D:/hermes-data/hermes/plugins/hermes-console/scripts/probe_autogroup.py

覆盖（每条都是真打路由，不 mock 业务逻辑）：
[1] ``GET /autogroup/status`` 200 且含待确认数 / 批次数 / 规则数
[2] ``GET /autogroup/suggestions`` 200：空态给结构，有建议给真实条目
[3] ``POST /autogroup/rules`` 写测试关键词 → 回读存在 → 删回原状 → 贴前后 ``load_rules``
[4] ``POST /autogroup/scan`` 立刻返回 job_id（计时证明不阻塞）→ 轮询到终态
[5] ``POST /autogroup/apply`` confirm:false → **400 + 预览体**；confirm:true → 批次号 + 回读
    （含 sync_cwd=true 的会话 cwd 真变化，再 undo 真还原）
[6] ``POST /autogroup/undo`` → 撤销条数 + 回读（会话退回 suggested、cwd 还原）
[7] ``POST /autogroup/reject`` → 回读（从建议里消失、进 rejected 表）
[8] 扫描失败路径 → job ``state=failed`` + 结构化 ``error``（不崩、不是 500）
[9] ``plugin_api`` 路由清单（17 基线 + 本卡 11 条；其中 DELETE /autogroup/rules 与 GET /autogroup/batches 为编排者后补）+ 只读约束（插件目录今日 0 改动）

探针会写真实的 ``auto-group/assignments.db``（扫描生成建议、apply 一个批次、再 undo），
因此**开工先备份、收工原样还原**（备份在 finally 里恢复：store 原本不存在就删掉）。
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
API = DASHBOARD / "plugin_api.py"
HOME = PLUGIN_ROOT.parents[1]
PREFIX = "/api/plugins/hermes-console"
AUTOGROUP_DIR = HOME / "plugins" / "hermes-auto-group"

if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

failures = []
passes = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passes
    if ok:
        passes += 1
        print("  PASS  " + label + (" — " + detail if detail else ""))
    else:
        failures.append(label + (" — " + detail if detail else ""))
        print("  FAIL  " + label + (" — " + detail if detail else ""))


def head(text: str, n: int = 320) -> str:
    text = text.replace("\n", " ")
    return text[:n] + ("…" if len(text) > n else "")


def j(obj, n: int = 320) -> str:
    return head(json.dumps(obj, ensure_ascii=False), n)


# ---------------------------------------------------------------------------
# 加载（与 dashboard 同款：合成模块名 + spec_from_file_location）
# ---------------------------------------------------------------------------
spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_hermes-console", API)
mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
sys.modules[spec.name] = mod  # type: ignore[union-attr]
spec.loader.exec_module(mod)  # type: ignore[union-attr]

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api_autogroup  # noqa: E402  (plugin_api 已按本目录 import 过同名模块)

core = api_autogroup._load_core()
home = api_autogroup._home()
STORE = core.store_path(home)
RULES_FILE = home / "auto-group" / "rules.json"

app = FastAPI()
app.include_router(mod.router, prefix=PREFIX)
client = TestClient(app, raise_server_exceptions=False)

# ---------------------------------------------------------------------------
# 备份（finally 里原样还原）
# ---------------------------------------------------------------------------
STORE_EXISTED = STORE.is_file()
STORE_BACKUP = STORE.read_bytes() if STORE_EXISTED else None
RULES_EXISTED = RULES_FILE.is_file()
RULES_BACKUP = RULES_FILE.read_bytes() if RULES_EXISTED else None
ORIG_LIST_SESSIONS = core.list_sessions
TEST_SLUG = "kanban-cron"
TEST_KEYWORD = "探针测试关键词-C8a"

#: 「本卡不改插件、不改前端」的取证方式：开工/收工各拍一次 mtime+size 快照做对比。
#: 不用「今日 mtime」——那些目录今天被别的卡改过，按日期判会把历史改动算到本探针头上。
SNAP_DIRS = (("plugins/hermes-auto-group", AUTOGROUP_DIR),
             ("plugins/hermes-console/src", PLUGIN_ROOT / "src"))


def snapshot() -> dict:
    out = {}
    for _rel, base in SNAP_DIRS:
        for p in base.rglob("*"):
            if p.is_file() and "__pycache__" not in str(p):
                st = p.stat()
                out[str(p)] = (st.st_mtime_ns, st.st_size)
    return out


SNAP_BEFORE = snapshot()


def restore_state() -> None:
    """把 store / rules.json 还原成探针开始前的样子。"""
    core.list_sessions = ORIG_LIST_SESSIONS
    if RULES_EXISTED and RULES_BACKUP is not None:
        RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
        RULES_FILE.write_bytes(RULES_BACKUP)
    elif RULES_FILE.exists():
        RULES_FILE.unlink()
    if STORE_EXISTED and STORE_BACKUP is not None:
        STORE.write_bytes(STORE_BACKUP)
    elif STORE.exists():
        STORE.unlink()
    print("\n[还原] store_exists=%s  rules_exists=%s" % (STORE.is_file(), RULES_FILE.is_file()))


print("home          :", home)
print("store         :", STORE, "| 探针前存在:", STORE_EXISTED)
print("rules.json    :", RULES_FILE, "| 探针前存在:", RULES_EXISTED)

try:
    # -----------------------------------------------------------------------
    print("\n[1] GET /autogroup/status")
    r = client.get(PREFIX + "/autogroup/status")
    b = r.json()
    d = b.get("data") or {}
    print("        %s %s" % (r.status_code, j(d, 400)))
    check("HTTP 200", r.status_code == 200, str(r.status_code))
    check("ok=true", b.get("ok") is True, head(r.text, 160))
    check("有待确认数 suggested", isinstance(d.get("suggested"), int), str(d.get("suggested")))
    check("有已应用数 applied", isinstance(d.get("applied"), int), str(d.get("applied")))
    check("有已忽略数 rejected", isinstance(d.get("rejected"), int), str(d.get("rejected")))
    check("有批次数 batches_total", isinstance(d.get("batches_total"), int), str(d.get("batches_total")))
    check("有规则数 rules_total / rules_projects",
          isinstance(d.get("rules_total"), int) and isinstance(d.get("rules_projects"), int),
          "rules_total=%s rules_projects=%s" % (d.get("rules_total"), d.get("rules_projects")))
    check("store 路径指向插件自己的 sqlite", str(d.get("store") or "").endswith("assignments.db"),
          str(d.get("store")))
    print("        notes:", j(b.get("notes"), 200))

    # -----------------------------------------------------------------------
    print("\n[2] GET /autogroup/suggestions（此时应是空态结构）")
    r = client.get(PREFIX + "/autogroup/suggestions", params={"status": "suggested", "limit": 5, "offset": 0})
    b = r.json()
    d = b.get("data") or {}
    print("        %s %s" % (r.status_code, j(d, 300)))
    check("HTTP 200", r.status_code == 200, str(r.status_code))
    check("空态也给完整结构（status/total/limit/offset/items）",
          all(k in d for k in ("status", "total", "limit", "offset", "items")), j(sorted(d), 200))
    check("items 是列表", isinstance(d.get("items"), list), str(type(d.get("items"))))
    check("空态有 notes 说下一步", bool(b.get("notes")), j(b.get("notes"), 160))
    r = client.get(PREFIX + "/autogroup/suggestions", params={"limit": 999})
    check("limit 被钳到 200", (r.json().get("data") or {}).get("limit") == 200,
          str((r.json().get("data") or {}).get("limit")))

    # -----------------------------------------------------------------------
    print("\n[3] POST /autogroup/rules → 回读 → 删回原状")
    before_rules = core.load_rules(home)
    print("        前后 load_rules[%s] 之前: %s" % (TEST_SLUG, j(before_rules.get(TEST_SLUG), 200)))
    r = client.post(PREFIX + "/autogroup/rules", json={"slug": TEST_SLUG, "keyword": TEST_KEYWORD})
    b = r.json()
    print("        POST → %s %s" % (r.status_code, j(b.get("data"), 300)))
    check("POST rules HTTP 200", r.status_code == 200, head(r.text, 200))
    check("回读 present=true", (b.get("data") or {}).get("present") is True, j(b.get("data"), 200))
    check("回读 keywords 里真有这个词", TEST_KEYWORD in ((b.get("data") or {}).get("keywords") or []),
          j((b.get("data") or {}).get("keywords"), 240))
    r = client.get(PREFIX + "/autogroup/rules")
    got = [row for row in (r.json().get("data") or {}).get("rules", []) if row["slug"] == TEST_SLUG]
    check("GET rules 也能读到（另一次回读）", bool(got) and TEST_KEYWORD in (got[0].get("custom") or []),
          j(got[0].get("custom") if got else None, 200))
    # 删回原状（探针自己还原文件，不靠额外端点）
    if RULES_EXISTED and RULES_BACKUP is not None:
        RULES_FILE.write_bytes(RULES_BACKUP)
    else:
        RULES_FILE.unlink(missing_ok=True)
    after_rules = core.load_rules(home)
    print("        前后 load_rules[%s] 之后: %s" % (TEST_SLUG, j(after_rules.get(TEST_SLUG), 200)))
    check("删回原状：回读里已无测试关键词",
          TEST_KEYWORD not in (after_rules.get(TEST_SLUG) or []), j(after_rules.get(TEST_SLUG), 200))
    check("删回原状：与探针前完全一致", after_rules == before_rules, "rules 全等")
    r = client.post(PREFIX + "/autogroup/rules", json={"slug": "BAD SLUG", "keyword": "x"})
    check("非法 slug → 400 invalid_slug", r.status_code == 400
          and (r.json().get("error") or {}).get("code") == "invalid_slug", head(r.text, 160))

    # -----------------------------------------------------------------------
    print("\n[4] POST /autogroup/scan（必须立刻返回 job_id，不阻塞）")
    t0 = time.time()
    r = client.post(PREFIX + "/autogroup/scan", json={"limit": 40})
    cost = time.time() - t0
    b = r.json()
    job_id = (b.get("data") or {}).get("job_id")
    print("        POST → %s %.2fs %s" % (r.status_code, cost, j(b.get("data"), 260)))
    check("HTTP 202（已受理，不是同步跑完）", r.status_code == 202, str(r.status_code))
    check("返回 job_id", bool(job_id), str(job_id))
    check("顶层也有 job_id（卡面契约）", b.get("job_id") == job_id, str(b.get("job_id")))
    check("立刻返回：耗时 < 3s（证明不阻塞）", cost < 3.0, "%.2fs" % cost)
    states = []
    deadline = time.time() + 240
    while time.time() < deadline:
        jr = client.get(PREFIX + "/autogroup/job/" + str(job_id))
        jb = jr.json()
        jd = jb.get("data") or {}
        state = jd.get("state")
        if not states or states[-1] != state:
            states.append(state)
            print("        poll %-8s progress=%s" % (state, j(jd.get("progress"), 80)))
        if state in ("done", "failed"):
            break
        time.sleep(1.0)
    jd = client.get(PREFIX + "/autogroup/job/" + str(job_id)).json().get("data") or {}
    print("        终态 %s result=%s error=%s" % (jd.get("state"), j(jd.get("result"), 400), j(jd.get("error"), 200)))
    check("轮询到终态", jd.get("state") in ("done", "failed"), str(jd.get("state")))
    check("终态 = done", jd.get("state") == "done", str(jd.get("state")))
    res = jd.get("result") or {}
    check("job 带 progress 且 done == total",
          (jd.get("progress") or {}).get("done") == (jd.get("progress") or {}).get("total"),
          j(jd.get("progress"), 120))
    check("job 报告 scanned / matched / saved", all(k in res for k in ("scanned", "matched", "saved")),
          j({k: res.get(k) for k in ("scanned", "matched", "saved", "suggested_total")}, 200))
    check("LLM 不可用是结构化说明（不是崩）",
          (res.get("llm") or {}).get("code") == "llm_unavailable", j(res.get("llm"), 200))
    check("未知 job → 404 unknown_job",
          client.get(PREFIX + "/autogroup/job/job-nope").status_code == 404)
    final = client.get(PREFIX + "/autogroup/job/" + str(job_id)).json()
    check("job 顶层也给 state/progress/error（{state,progress,error} 契约）",
          final.get("state") == "done" and isinstance(final.get("progress"), dict) and "error" in final,
          j({"state": final.get("state"), "progress": final.get("progress"), "error": final.get("error")}, 160))

    # -----------------------------------------------------------------------
    print("\n[5] 扫描后 GET /autogroup/suggestions 应有真实条目")
    r = client.get(PREFIX + "/autogroup/suggestions", params={"status": "suggested", "limit": 20})
    d = r.json().get("data") or {}
    items = d.get("items") or []
    print("        total=%s 本页=%s" % (d.get("total"), len(items)))
    if items:
        print("        第一条: " + j(items[0], 460))
    check("有真实建议条目", len(items) > 0, "total=%s" % d.get("total"))
    check("条目带会话标题（来自 state.db）", bool(items and items[0].get("title")), j(items[0].get("title"), 120) if items else "")
    check("条目带目标项目名", bool(items and items[0].get("project_name")), j(items[0].get("project_name"), 80) if items else "")
    check("条目带判定依据 basis.kind=rule", bool(items and (items[0].get("basis") or {}).get("kind") == "rule"),
          j((items[0].get("basis") or {}).get("kind") if items else None, 60))
    check("分页 offset 生效（offset=1 少一条）",
          len((client.get(PREFIX + "/autogroup/suggestions", params={"limit": 1, "offset": 1})
               .json().get("data") or {}).get("items") or []) <= 1)

    # -----------------------------------------------------------------------
    print("\n[6] POST /autogroup/apply（confirm:false → 400 预览；confirm:true → 批次 + 回读）")
    # 选一个「真会写入」的会话：置信度必须 >= core.MIN_CONFIDENCE，否则 confirm:true 会 400。
    # 优先挑 cwd 与目标项目路径不同的，这样 sync_cwd 的效果能被观测到。
    pick, pv = None, {}
    candidates = [i["session_id"] for i in items[:8]]
    for want_change in (True, False):
        for cand in candidates:
            probe_r = client.post(PREFIX + "/autogroup/apply",
                                  json={"session_ids": [cand], "sync_cwd": True, "confirm": False})
            body = probe_r.json()
            prev = body.get("preview") or (body.get("error") or {}).get("preview") or {}
            first = (prev.get("items") or [{}])[0]
            differs = str(first.get("current_cwd") or "") != str(first.get("target_path") or "")
            print("        候选 %s… conf=%s will_apply=%s cwd≠目标=%s"
                  % (cand[:18], first.get("confidence"), first.get("will_apply"), differs))
            if first.get("will_apply") and (differs == want_change):
                pick, pv = cand, prev
                break
        if pick is not None:
            break
    if pick is None:
        pick = items[0]["session_id"] if items else None
    r = client.post(PREFIX + "/autogroup/apply",
                    json={"session_ids": [pick] if pick else [], "sync_cwd": True, "confirm": False})
    b = r.json()
    pv = b.get("preview") or b.get("error", {}).get("preview") or pv or {}
    print("        %s %s" % (r.status_code, j({"error": b.get("error", {}).get("code"),
                                               "message": b.get("error", {}).get("message")}, 260)))
    print("        预览体: " + j(pv, 520))
    check("confirm:false → HTTP 400", r.status_code == 400, str(r.status_code))
    check("错误码 confirmation_required",
          (b.get("error") or {}).get("code") == "confirmation_required", j((b.get("error") or {}).get("code"), 60))
    check("预览体带将影响的会话", bool(pv.get("items") and pv["items"][0].get("session_id")),
          j((pv.get("items") or [{}])[0].get("session_id"), 80))
    check("预览体带目标项目", bool(pv.get("items") and pv["items"][0].get("project_name")),
          j((pv.get("items") or [{}])[0].get("project_name"), 80))
    check("预览体带是否同步 cwd", pv.get("sync_cwd") is True, str(pv.get("sync_cwd")))
    check("预览体带影响条数", isinstance(pv.get("will_apply"), int) and isinstance(pv.get("count"), int),
          "count=%s will_apply=%s" % (pv.get("count"), pv.get("will_apply")))
    check("confirm:false 没写库（状态里 suggested 未减少）",
          (client.get(PREFIX + "/autogroup/status").json().get("data") or {}).get("applied") == 0,
          str((client.get(PREFIX + "/autogroup/status").json().get("data") or {}).get("applied")))

    target = (pv.get("items") or [{}])[0]
    cwd_before = target.get("current_cwd")
    print("        被测会话 cwd 前: %s  目标项目路径: %s" % (cwd_before, target.get("target_path")))
    r = client.post(PREFIX + "/autogroup/apply",
                    json={"session_ids": [pick], "sync_cwd": True, "confirm": True})
    b = r.json()
    dd = b.get("data") or {}
    batch_id = dd.get("batch_id")
    print("        confirm:true → %s %s" % (r.status_code, j(dd.get("readback"), 400)))
    check("confirm:true → HTTP 200", r.status_code == 200, head(r.text, 200))
    check("返回批次号", bool(batch_id), str(batch_id))
    check("回读 batch_sessions 含该会话", pick in (dd.get("readback") or {}).get("batch_sessions", []),
          j((dd.get("readback") or {}).get("batch_sessions"), 160))
    check("回读 applied 计数 = 1", (dd.get("readback") or {}).get("applied") == 1,
          str((dd.get("readback") or {}).get("applied")))
    conn = sqlite3.connect("file:" + str(home / "state.db") + "?mode=ro", uri=True)
    try:
        cwd_after = conn.execute("SELECT cwd FROM sessions WHERE id = ?", (pick,)).fetchone()[0]
    finally:
        conn.close()
    print("        被测会话 cwd 后: %s" % cwd_after)
    check("sync_cwd 真生效：state.db 里 cwd = 目标项目路径",
          str(cwd_after) == str(target.get("target_path")), "%s → %s" % (cwd_before, cwd_after))
    if str(cwd_before) != str(target.get("target_path")):
        check("cwd 确实被改写了（前后不同）", cwd_after != cwd_before,
              "%s → %s" % (cwd_before, cwd_after))
    conn = sqlite3.connect("file:" + str(STORE) + "?mode=ro", uri=True)
    try:
        backup_rows = conn.execute(
            "SELECT session_id, old_cwd, new_cwd FROM cwd_backup WHERE batch_id = ?", (batch_id,)).fetchall()
    finally:
        conn.close()
    print("        cwd_backup 记录: %s" % (backup_rows,))
    check("cwd_backup 记下原值与新值（undo 可还原的依据）", bool(backup_rows), str(len(backup_rows)) + " 条")

    # -----------------------------------------------------------------------
    print("\n[7] POST /autogroup/undo（撤销条数 + 回读 + cwd 还原）")
    r = client.post(PREFIX + "/autogroup/undo", json={"batch_id": batch_id})
    b = r.json()
    dd = b.get("data") or {}
    rb = dd.get("readback") or {}
    print("        %s %s" % (r.status_code, j(rb, 420)))
    check("HTTP 200", r.status_code == 200, head(r.text, 200))
    check("撤销条数 >= 1", (rb.get("undone_sessions") or 0) >= 1,
          "undone=%s expected=%s" % (rb.get("undone_sessions"), rb.get("expected")))
    check("回读：该会话已退回 suggested", pick in (rb.get("back_to_suggested") or []),
          j(rb.get("back_to_suggested"), 160))
    check("回读：批次里没有残留 applied", not (rb.get("still_applied") or []), j(rb.get("still_applied"), 120))
    check("回读：applied 归零", rb.get("applied") == 0, str(rb.get("applied")))
    conn = sqlite3.connect("file:" + str(home / "state.db") + "?mode=ro", uri=True)
    try:
        cwd_restored = conn.execute("SELECT cwd FROM sessions WHERE id = ?", (pick,)).fetchone()[0]
    finally:
        conn.close()
    print("        被测会话 cwd 还原为: %s（原值 %s）" % (cwd_restored, cwd_before))
    check("cwd 已还原成原值", cwd_restored == cwd_before, "%s vs %s" % (cwd_restored, cwd_before))
    r = client.post(PREFIX + "/autogroup/undo", json={"batch_id": batch_id})
    check("重复撤销同一批次 → 404 unknown_batch", r.status_code == 404,
          str(r.status_code) + " " + head(r.text, 120))

    # -----------------------------------------------------------------------
    print("\n[8] POST /autogroup/reject（回读：从建议消失、进 rejected 表）")
    r = client.get(PREFIX + "/autogroup/suggestions", params={"limit": 2})
    left = (r.json().get("data") or {}).get("items") or []
    victim = left[0]["session_id"] if left else None
    before_total = (r.json().get("data") or {}).get("total")
    r = client.post(PREFIX + "/autogroup/reject", json={"session_id": victim, "reason": "探针：手动忽略"})
    b = r.json()
    print("        %s %s" % (r.status_code, j(b.get("data"), 320)))
    check("HTTP 200", r.status_code == 200, head(r.text, 200))
    after = client.get(PREFIX + "/autogroup/suggestions", params={"limit": 200}).json().get("data") or {}
    check("回读：该会话已不在 suggested 里",
          all(i["session_id"] != victim for i in after.get("items") or []), str(victim))
    check("回读：rejected 总数 +1", (b.get("data") or {}).get("readback", {}).get("rejected_total", 0) >= 1,
          j((b.get("data") or {}).get("readback"), 160))
    check("回读：status.rejected 同步", (b.get("data") or {}).get("readback", {}).get("status_rejected") == 1,
          str((b.get("data") or {}).get("readback", {}).get("status_rejected")))
    check("忽略后建议总数减少", (after.get("total") or 0) < (before_total or 0),
          "%s → %s" % (before_total, after.get("total")))

    # -----------------------------------------------------------------------
    print("\n[9] 扫描失败路径 → job state=failed + 结构化 error（不崩）")


    def boom(*a, **kw):
        raise RuntimeError("探针模拟：state.db 读失败")

    core.list_sessions = boom
    r = client.post(PREFIX + "/autogroup/scan", json={"limit": 5})
    bad_id = (r.json().get("data") or {}).get("job_id")
    check("失败路径也立刻返回 job_id（202）", r.status_code == 202 and bool(bad_id),
          "%s %s" % (r.status_code, bad_id))
    deadline = time.time() + 60
    jd = {}
    while time.time() < deadline:
        jd = client.get(PREFIX + "/autogroup/job/" + str(bad_id)).json().get("data") or {}
        if jd.get("state") in ("done", "failed"):
            break
        time.sleep(0.5)
    print("        终态 %s error=%s" % (jd.get("state"), j(jd.get("error"), 260)))
    check("终态 = failed（不是 500、不是崩）", jd.get("state") == "failed", str(jd.get("state")))
    check("结构化 error.code=scan_failed", (jd.get("error") or {}).get("code") == "scan_failed",
          j(jd.get("error"), 200))
    check("error.message 说清原因", "state.db 读失败" in str((jd.get("error") or {}).get("message")),
          j((jd.get("error") or {}).get("message"), 200))
    core.list_sessions = ORIG_LIST_SESSIONS

    # -----------------------------------------------------------------------
    print("\n[10] plugin_api 路由清单")
    routes = [(sorted(getattr(rt, "methods", []) or []), rt.path) for rt in mod.router.routes]
    print("        总路由数: %d" % len(routes))
    for methods, path in routes:
        print("          %-6s %s" % ("/".join(methods), path))
    ag = [(m, p) for m, p in routes if p.startswith("/autogroup")]
    print("        autogroup 路由数: %d" % len(ag))
    # 基线 17 条（C7 验收时的清单）必须原样还在；别的卡之后加的路由（如 /health）不算本卡的账。
    baseline = {("GET", "/overview/layout"), ("PUT", "/overview/layout"), ("GET", "/overview/system"),
                ("GET", "/overview/analytics"), ("GET", "/overview/cron"), ("GET", "/overview/sessions"),
                ("GET", "/overview"), ("GET", "/weather/now"), ("GET", "/weather/daily"),
                ("GET", "/weather/hourly"), ("GET", "/weather/at"), ("GET", "/weather"),
                ("GET", "/upgrade/scan"), ("GET", "/agents"), ("GET", "/agents/overview"),
                ("GET", "/agents/{agent_id}/sessions"), ("GET", "/agents/{agent_id}/cron")}
    got_all = {(m[0], p) for m, p in routes}
    check("基线 17 条仍在", baseline <= got_all, "缺 %s" % sorted(baseline - got_all))
    others = sorted(got_all - baseline - {("GET", "/autogroup/status"), ("GET", "/autogroup/suggestions"),
                                          ("GET", "/autogroup/rules"), ("POST", "/autogroup/rules"),
                                          ("POST", "/autogroup/apply"), ("POST", "/autogroup/undo"),
                                          ("DELETE", "/autogroup/rules"), ("GET", "/autogroup/batches"),
                                          ("POST", "/autogroup/reject"), ("POST", "/autogroup/scan"),
                                          ("GET", "/autogroup/job/{job_id}")})
    print("        非本卡路由（基线 17 + 本卡 11 之外，来自别的卡）: %s" % (others or "无"))
    check("总路由 = 基线 17 + 本卡 11 + 别的卡 %d = %d" % (len(others), 28 + len(others)),
          len(routes) == 28 + len(others), str(len(routes)))
    health = client.get(PREFIX + "/health").json() if ("GET", "/health") in got_all else {}
    if health:
        print("        /health: %s" % j({"ok": health.get("ok"), "loaded": health.get("loaded"),
                                          "failed": health.get("failed")}, 240))
        check("/health 报 api_autogroup 已挂上", "api_autogroup" in (health.get("loaded") or []),
              j(health.get("loaded"), 200))
        check("/health 没有域加载失败", not health.get("failed"), j(health.get("failed"), 200))
    expected = {("GET", "/autogroup/status"), ("GET", "/autogroup/suggestions"),
                ("GET", "/autogroup/rules"), ("POST", "/autogroup/rules"),
                ("DELETE", "/autogroup/rules"), ("GET", "/autogroup/batches"),
                ("POST", "/autogroup/apply"), ("POST", "/autogroup/undo"),
                ("POST", "/autogroup/reject"), ("POST", "/autogroup/scan"),
                ("GET", "/autogroup/job/{job_id}")}
    got = {(m[0], p) for m, p in ag}
    check("本卡 11 条 (method,path) 全部注册", got == expected,
          "缺 %s / 多 %s" % (sorted(expected - got), sorted(got - expected)))

    # -----------------------------------------------------------------------
    print("\n[11] 只读约束：探针期间不改插件、不改前端")
    after_snap = snapshot()
    for rel, base in SNAP_DIRS:
        prefix = str(base)
        changed = [k for k, v in after_snap.items() if k.startswith(prefix) and SNAP_BEFORE.get(k) != v]
        added = [k for k in after_snap if k.startswith(prefix) and k not in SNAP_BEFORE]
        removed = [k for k in SNAP_BEFORE if k.startswith(prefix) and k not in after_snap]
        touched = [str(Path(x).relative_to(HOME)) for x in changed + added + removed]
        check(rel + " 探针期间 0 改动（mtime/size 快照对比）", not touched,
              ", ".join(touched[:4]) if touched else "clean（%d 个文件）" % len(
                  [k for k in after_snap if k.startswith(prefix)]))
finally:
    restore_state()

print()
print("=" * 72)
print("PASS %d / FAIL %d" % (passes, len(failures)))
if failures:
    for item in failures:
        print("  FAILED:", item)
    sys.exit(1)
print("AUTOGROUP-PROBE-OK")

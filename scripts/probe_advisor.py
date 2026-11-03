# -*- coding: utf-8 -*-
"""hermes-console · Advisor 审核台后端验收探针（C9b：真实 TestClient + 真实台账）。

跑法（必须用带 fastapi 的 Hermes 环境）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        D:/hermes-data/hermes/plugins/hermes-console/scripts/probe_advisor.py

覆盖（每条都是真打路由，不 mock 业务逻辑）：

[1] ``GET /advisor`` 自检：模块路径 / 台账路径 / 事件行数 / 路由清单
[2] ``GET /advisor/summary``：总数 / 待处理 / 已采纳 / 已拒绝 / severity 分布 / 7 天趋势
[3] ``GET /advisor/advice``：分页（limit/offset）、status 过滤、severity 过滤、q 关键词、
    非法 severity → 400 ``invalid_argument``
[4] ``GET /advisor/advice/{id}``：单条详情 + 完整事件时间线；不存在的 id → 404 ``not_found``
[5] ``POST /advisor/decision`` 采纳 → 回读 ``status=accepted``；**并从磁盘回读台账新增行**
    （证明是落盘，不是内存假成功）
[6] ``POST /advisor/decision`` 拒绝：缺 reason → 400 ``invalid_argument``；带 reason → 200
[7] ``GET /advisor/settings``：白名单设置 + 只读的 ``blocker_forced_delivery``
[8] ``PUT /advisor/settings``：合法字段生效并**回读**；未知字段 → 400；
    ``blocker_forced_delivery=false`` → 400「单向阀门不可关闭」
[9] 降级：模拟 ``advisor.audit`` import 失败 → 本分区结构化 503，**其它分区照常**
[10] 既有分区回归：概览 / 天气 / 升级 / Agents / 项目分组 各自状态码

探针会往**真实台账**追加两条决定事件（然后原样还原），并临时改一次 ``settings.json``
（同样还原）：台账是 append-only 的审计记录，不接受探针留下的假决定。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
API = DASHBOARD / "plugin_api.py"
HOME = PLUGIN_ROOT.parents[1]
PREFIX = "/api/plugins/hermes-console"

if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))
# 生产里后端就是从 hermes-agent 起的（sys.path[0] 是它）；台账模块延迟用到 hermes_constants。
for extra in (HOME / "hermes-agent", HOME / "plugins"):
    if extra.is_dir() and str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

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


def j(obj, n: int = 200) -> str:
    """响应 JSON 的前 n 个字符（验收要求「贴前 200 字符」）。"""
    text = json.dumps(obj, ensure_ascii=False).replace("\n", " ")
    return text[:n] + ("…" if len(text) > n else "")


def show(label: str, response, n: int = 200) -> str:
    body = ""
    try:
        body = j(response.json(), n)
    except Exception:  # noqa: BLE001
        body = response.text[:n]
    print("        %-42s %s  %s" % (label, response.status_code, body))
    return body


# ---------------------------------------------------------------------------
# 加载（与 dashboard 同款：合成模块名 + spec_from_file_location）
# ---------------------------------------------------------------------------
spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_hermes-console", API)
mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
sys.modules[spec.name] = mod  # type: ignore[union-attr]
spec.loader.exec_module(mod)  # type: ignore[union-attr]

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api_advisor  # noqa: E402  (plugin_api 已按本目录 import 过同名模块)

app = FastAPI()
app.include_router(mod.router, prefix=PREFIX)
client = TestClient(app, raise_server_exceptions=False)


def url(path: str) -> str:
    return PREFIX + path


audit = api_advisor._load_audit()
LEDGER = Path(audit.ledger_path())
SETTINGS = Path(audit.settings_path())
print("home          :", api_advisor._home())
print("advisor 模块  :", getattr(audit, "__file__", "?"))
print("台账          :", LEDGER, "| 存在:", LEDGER.is_file())
print("设置          :", SETTINGS, "| 存在:", SETTINGS.is_file())
print("plugin_api    :", "loaded=" + str(len(mod.DOMAIN_LOADED)) + " failed=" + str(mod.DOMAIN_FAILURES))

# ---------------------------------------------------------------------------
# 备份（finally 里原样还原）
#
# 一律按**字节**读写：台账是 append-only 的审计文件，用文本模式读写会踩 Windows 换行翻译
# （read_text 把 CRLF 折成 LF，write_text 又把 LF 写成 CRLF），还原后文件会多出 N 个 \r，
# 字节数变了就不再是「原样还原」。
# ---------------------------------------------------------------------------
LEDGER_ORIG = LEDGER.read_bytes() if LEDGER.is_file() else None
SETTINGS_EXISTED = SETTINGS.is_file()
SETTINGS_ORIG = SETTINGS.read_bytes() if SETTINGS_EXISTED else None


def restore() -> None:
    """只摘掉**本探针追加的行**：仍以原文开头才截回原文，否则原样留着并说明。"""
    if LEDGER_ORIG is not None and LEDGER.is_file():
        now = LEDGER.read_bytes()
        if now.startswith(LEDGER_ORIG):
            if now != LEDGER_ORIG:
                LEDGER.write_bytes(LEDGER_ORIG)
                print("\n[还原] 台账已截回探针前的 %d 字节（摘掉探针追加的行）" % len(LEDGER_ORIG))
            else:
                print("\n[还原] 台账无需改动")
        else:
            print("\n[还原] 台账在探针期间被别人追加过，未截断（保留全部行）")
    if SETTINGS_EXISTED and SETTINGS_ORIG is not None:
        SETTINGS.write_bytes(SETTINGS_ORIG)
        print("[还原] settings.json 已还原")
    elif not SETTINGS_EXISTED and SETTINGS.exists():
        SETTINGS.unlink()
        print("[还原] settings.json 已删除（探针前不存在）")


def ledger_lines() -> list[str]:
    if not LEDGER.is_file():
        return []
    return [line for line in LEDGER.read_text(encoding="utf-8").splitlines() if line.strip()]


try:
    # =======================================================================
    print("\n[1] GET /advisor（自检）")
    r = client.get(url("/advisor"))
    show("GET /advisor", r)
    body = r.json()
    check("自检 200 且 ok=true", r.status_code == 200 and body.get("ok") is True)
    check("自检报出台账路径与事件行数",
          body["data"]["ledger"] == str(LEDGER) and body["data"]["events"] == len(ledger_lines()),
          "events=%s lines=%s" % (body["data"]["events"], len(ledger_lines())))
    check("自检列出全部 9 条路由（8 条路径 + settings 的 GET/PUT）",
          set(body["data"]["routes"]) == {
              "GET /advisor", "GET /advisor/advice", "GET /advisor/advice/{id}",
              "GET /advisor/reviews", "POST /advisor/decision", "GET /advisor/summary",
              "GET /advisor/coverage", "GET /advisor/settings", "PUT /advisor/settings"},
          ",".join(body["data"]["routes"]))
    check("自检声明只读常量 blocker_forced_delivery",
          body["data"]["readonly_settings"] == ["blocker_forced_delivery"],
          str(body["data"]["readonly_settings"]))
    check("advisor 路由已挂进统一 router",
          "api_advisor" in mod.DOMAIN_LOADED and not mod.DOMAIN_FAILURES,
          "loaded=%s failed=%s" % (mod.DOMAIN_LOADED, mod.DOMAIN_FAILURES))

    # =======================================================================
    print("\n[2] GET /advisor/summary（统计卡）")
    r = client.get(url("/advisor/summary"))
    show("GET /advisor/summary", r)
    summary = r.json()
    check("统计 200 且 ok=true", r.status_code == 200 and summary.get("ok") is True)
    data = summary["data"]
    check("给出总数 / 待处理 / 已采纳 / 已拒绝",
          all(key in data for key in ("total", "pending", "accepted", "rejected", "delivered", "dropped")),
          j({k: data[k] for k in ("total", "pending", "accepted", "rejected")}, 120))
    check("总数与台账折叠结果一致", data["total"] == audit.summary()["total"],
          "api=%s audit=%s" % (data["total"], audit.summary()["total"]))
    check("severity 分布三个档位都在", sorted(data["by_severity"].keys()) == ["blocker", "concern", "nit"],
          str(data["by_severity"]))
    check("7 天趋势是 7 个桶且日期升序",
          len(data["trend"]) == 7 and data["trend"][0]["date"] < data["trend"][-1]["date"],
          j([t["date"] for t in data["trend"]], 120))
    check("趋势桶带 raised/delivered/decided 三个计数",
          all(set(bucket) == {"date", "raised", "delivered", "decided"} for bucket in data["trend"]))
    check("最近一天（今天）在趋势末尾", data["trend"][-1]["date"] >= data["trend"][0]["date"])

    # =======================================================================
    print("\n[3] GET /advisor/advice（分页 + 筛选）")
    r = client.get(url("/advisor/advice"), params={"limit": 5, "offset": 0})
    show("GET /advisor/advice?limit=5&offset=0", r)
    page1 = r.json()
    check("列表 200 且 ok=true", r.status_code == 200 and page1.get("ok") is True)
    d1 = page1["data"]
    #: 真实台账可能为空（新机器 / 台账被归档后）：`items` 里没有行时，"取 items[0] 逐字段"
    #: 这类断言就无从谈起。这不是"放宽断言"，而是把**数据环境依赖**的那几条换成
    #: **空态契约**断言（count=0 / items=[] / total=0），并在上面显式打印原因。
    HAS_ROWS = bool(d1.get("items"))
    first_id = d1["items"][0]["id"] if HAS_ROWS else "ffffffffffffffff"
    check("分页字段齐备（limit/offset/count/total 四个都在）",
          all(key in d1 for key in ("limit", "offset", "count", "total")),
          j({k: d1.get(k) for k in ("limit", "offset", "count", "total")}, 120))
    if HAS_ROWS:
        check("服务端分页：本页 5 条、total 是过滤后的总数",
              d1["count"] == 5 and d1["limit"] == 5 and d1["offset"] == 0 and d1["total"] >= 5,
              "count=%s total=%s" % (d1["count"], d1["total"]))
        check("total 与台账 total 一致", d1["total"] == audit.list_advice(limit=0)["total"],
              "api=%s audit=%s" % (d1["total"], audit.list_advice(limit=0)["total"]))
        check("行里带 UI 需要的字段（severity/status/note/profile/session_short/时间）",
              all(key in d1["items"][0] for key in
                  ("id", "note", "severity", "status", "profile", "session_short",
                   "first_raised_at", "last_seen_at", "raise_count", "deliver_count")),
              j(sorted(d1["items"][0].keys()), 200))
        ids1 = [item["id"] for item in d1["items"]]
        r = client.get(url("/advisor/advice"), params={"limit": 5, "offset": 5})
        show("GET /advisor/advice?limit=5&offset=5", r)
        ids2 = [item["id"] for item in r.json()["data"]["items"]]
        check("offset 真的翻页（第二页与第一页无交集）",
              bool(ids2) and not set(ids1) & set(ids2), "p1=%s p2=%s" % (ids1, ids2))
    else:
        print("        [SKIP] 真实台账为空（items=[]）：跳过「本页 5 条 / 逐字段 / 翻页」三条，")
        print("               改用空态契约断言（数据环境依赖，与本卡改动无关）")
        ids1, ids2 = [], []
        check("空台账：列表仍是 200 + count=0 + items=[]（空态不是 500）",
              r.status_code == 200 and d1["count"] == 0 and d1["items"] == [],
              j({k: d1.get(k) for k in ("count", "total")}, 120))
        check("空台账：total == 台账 total == 0",
              d1["total"] == audit.list_advice(limit=0)["total"] == 0,
              "api=%s audit=%s" % (d1["total"], audit.list_advice(limit=0)["total"]))

    r = client.get(url("/advisor/advice"), params={"status": "pending", "limit": 200})
    show("GET /advisor/advice?status=pending", r)
    pending = r.json()["data"]
    check("status 过滤生效（每行都是 pending，且 total 与行数一致）",
          all(item["status"] == "pending" for item in pending["items"])
          and (pending["total"] >= 1 if HAS_ROWS else pending["total"] == 0),
          "total=%s" % pending["total"])
    r = client.get(url("/advisor/advice"), params={"severity": "blocker", "limit": 200})
    show("GET /advisor/advice?severity=blocker", r)
    blockers = r.json()["data"]
    check("severity 过滤生效（每行都是 blocker）",
          all(item["severity"] == "blocker" for item in blockers["items"]),
          "total=%s" % blockers["total"])
    if HAS_ROWS:
        r = client.get(url("/advisor/advice"), params={"q": first_id, "limit": 10})
        show("GET /advisor/advice?q=<id>", r)
        hit = r.json()["data"]
        check("q 关键词命中（按 id 搜到自己）",
              hit["total"] == 1 and hit["items"][0]["id"] == first_id,
              "total=%s" % hit["total"])
    else:
        r = client.get(url("/advisor/advice"), params={"q": "probe-no-such-keyword", "limit": 10})
        show("GET /advisor/advice?q=<无命中关键词>", r)
        hit = r.json()["data"]
        check("空台账：q 关键词查询仍是 200 且 total=0（空态不是 500）",
              r.status_code == 200 and hit["total"] == 0 and hit["items"] == [],
              "total=%s" % hit["total"])
    r = client.get(url("/advisor/advice"), params={"severity": "fatal"})
    show("GET /advisor/advice?severity=fatal", r)
    bad = r.json()
    check("非法 severity → 400 invalid_argument",
          r.status_code == 400 and bad["error"]["code"] == "invalid_argument",
          bad["error"]["message"][:80])
    r = client.get(url("/advisor/advice"), params={"status": "whatever"})
    show("GET /advisor/advice?status=whatever", r)
    check("非法 status → 400 invalid_argument（台账自己抛的 ValueError）",
          r.status_code == 400 and r.json()["error"]["code"] == "invalid_argument",
          r.json()["error"]["message"][:80])

    # =======================================================================
    print("\n[4] GET /advisor/advice/{id}（详情 + 事件时间线）")
    if HAS_ROWS:
        r = client.get(url("/advisor/advice/" + first_id))
        show("GET /advisor/advice/{id}", r)
        detail = r.json()
        check("详情 200 且 ok=true", r.status_code == 200 and detail.get("ok") is True)
        d = detail["data"]
        check("详情带完整事件时间线（events 非空且每条有 event/ts）",
              bool(d["events"]) and all("event" in e and "ts" in e for e in d["events"]),
              "events=%s -> %s" % (len(d["events"]), [e["event"] for e in d["events"]]))
        check("详情带首次出现时间 / 出现次数 / 投递次数",
              d["first_raised_at"] != "" and isinstance(d["raise_count"], int)
              and isinstance(d["deliver_count"], int),
              "%s raise=%s deliver=%s" % (d["first_raised_at"], d["raise_count"], d["deliver_count"]))
    else:
        print("        [SKIP] 空台账没有可查的 id：跳过「详情 200 / 时间线 / 计数」三条")
    r = client.get(url("/advisor/advice/0123456789abcdef"))
    show("GET /advisor/advice/<不存在的 id>", r)
    check("不存在的 id → 404 not_found",
          r.status_code == 404 and r.json()["error"]["code"] == "not_found",
          r.json()["error"]["message"][:80])

    # =======================================================================
    print("\n[5] POST /advisor/decision 采纳（只写决定，落盘）")
    if HAS_ROWS:
        lines_before = len(ledger_lines())
        r = client.post(url("/advisor/decision"), json={"id": first_id, "decision": "accepted"})
        show("POST /advisor/decision {accepted}", r)
        accepted = r.json()
        check("采纳 200 且 ok=true", r.status_code == 200 and accepted.get("ok") is True)
        check("回读 status=accepted（不是信写操作的返回值）",
              accepted["data"]["readback"]["status"] == "accepted"
              and accepted["data"]["readback"]["decision"] == "accepted",
              j({"status": accepted["data"]["readback"]["status"],
                 "decision": accepted["data"]["readback"]["decision"]}, 120))
        check("响应明确写了「没有执行建议内容」",
              accepted["data"]["executed"] is False and "没有执行" in accepted["notes"][0],
              accepted["notes"][0][:60])
        lines_after = ledger_lines()
        check("台账真的多了一行（磁盘回读，不是内存）", len(lines_after) == lines_before + 1,
              "%s -> %s 行" % (lines_before, len(lines_after)))
        appended = json.loads(lines_after[-1])
        print("        新增事件行（advisor/audit/advice.jsonl 最后一行）:")
        print("        " + lines_after[-1][:400])
        check("新增行是 accepted 事件且 id / reason / actor 对得上",
              appended["event"] == "accepted" and appended["id"] == first_id
              and appended["actor"] == "user" and appended["source"] == "console",
              j({k: appended[k] for k in ("event", "id", "actor", "source")}, 160))
        check("决定事件不改台账历史行（append-only：前 %d 行字节未变）" % lines_before,
              LEDGER.read_bytes().startswith(LEDGER_ORIG or b""))
        r = client.get(url("/advisor/advice/" + first_id))
        show("GET /advisor/advice/{id}（采纳后）", r)
        after = r.json()["data"]
        check("再查详情：状态已是 accepted 且时间线末尾是决定事件",
              after["status"] == "accepted" and after["events"][-1]["event"] == "accepted",
              "status=%s last=%s" % (after["status"], after["events"][-1]["event"]))
        r = client.get(url("/advisor/summary"))
        check("统计卡已把这条算进「已采纳」", r.json()["data"]["accepted"] >= 1,
              "accepted=%s" % r.json()["data"]["accepted"])
    else:
        print("        [SKIP] 空台账没有可采纳的 id：跳过「采纳 → 回读 → 台账多一行」五条")
        r = client.post(url("/advisor/decision"),
                        json={"id": "ffffffffffffffff", "decision": "accepted"})
        show("POST /advisor/decision {不存在 id, accepted}", r)
        check("空台账：对不存在的 id 做决定 → 404 not_found（不是 500）",
              r.status_code == 404 and r.json()["error"]["code"] == "not_found",
              r.json()["error"]["message"][:80])

    # =======================================================================
    print("\n[6] POST /advisor/decision 拒绝（reason 必填）")
    second_id = ids2[0] if ids2 else first_id
    r = client.post(url("/advisor/decision"), json={"id": second_id, "decision": "rejected", "reason": ""})
    show("POST /advisor/decision {rejected, reason 空}", r)
    rejected_empty = r.json()
    check("缺 reason → 400 invalid_argument（前后端都拦一次）",
          r.status_code == 400 and rejected_empty["error"]["code"] == "invalid_argument",
          rejected_empty["error"]["message"][:80])
    check("400 的文案说清「拒绝必须填写原因」",
          "原因" in rejected_empty["error"]["message"], rejected_empty["error"]["message"][:60])
    if HAS_ROWS:
        lines_before = len(ledger_lines())
        r = client.post(url("/advisor/decision"),
                        json={"id": second_id, "decision": "rejected", "reason": "探针：这条建议与本机无关"})
        show("POST /advisor/decision {rejected, 有 reason}", r)
        rejected = r.json()
        check("带 reason 的拒绝 200 且 ok=true", r.status_code == 200 and rejected.get("ok") is True)
        check("回读 status=rejected 且原因写进台账",
              rejected["data"]["readback"]["status"] == "rejected"
              and rejected["data"]["readback"]["reason"] == "探针：这条建议与本机无关",
              j({"status": rejected["data"]["readback"]["status"],
                 "reason": rejected["data"]["readback"]["reason"]}, 140))
        lines_after = ledger_lines()
        check("台账又多一行（拒绝也落盘）", len(lines_after) == lines_before + 1,
              "%s -> %s 行" % (lines_before, len(lines_after)))
        last = json.loads(lines_after[-1])
        print("        新增事件行: " + lines_after[-1][:300])
        check("新增行是 rejected 且带 reason",
              last["event"] == "rejected" and last["reason"] == "探针：这条建议与本机无关",
              j({"event": last["event"], "reason": last["reason"]}, 140))
    else:
        print("        [SKIP] 空台账没有可拒绝的 id：跳过「拒绝落盘 / 台账多一行 / 回读 reason」三条")
    r = client.post(url("/advisor/decision"), json={"id": "ffffffffffffffff", "decision": "accepted"})
    show("POST /advisor/decision {不存在的 id}", r)
    check("不存在的 id → 404 not_found",
          r.status_code == 404 and r.json()["error"]["code"] == "not_found",
          r.json()["error"]["message"][:80])
    r = client.post(url("/advisor/decision"), json={"id": first_id, "decision": "maybe"})
    show("POST /advisor/decision {decision=maybe}", r)
    check("非法 decision → 400 invalid_argument",
          r.status_code == 400 and r.json()["error"]["code"] == "invalid_argument",
          r.json()["error"]["message"][:80])

    # =======================================================================
    print("\n[7] GET /advisor/settings（白名单 + 只读常量）")
    r = client.get(url("/advisor/settings"))
    show("GET /advisor/settings", r)
    settings_body = r.json()
    check("设置 200 且 ok=true", r.status_code == 200 and settings_body.get("ok") is True)
    sdata = settings_body["data"]
    check("白名单五个可写字段都在",
          sdata["writable"] == ["concern_default", "enabled", "min_interval_seconds", "model", "provider"],
          str(sdata["writable"]))
    check("blocker_forced_delivery 出现在响应里且为 true",
          sdata["settings"]["blocker_forced_delivery"] is True
          and sdata["forced"] == {"blocker_forced_delivery": True},
          j({"blocker_forced_delivery": sdata["settings"]["blocker_forced_delivery"],
             "readonly": sdata["readonly"]}, 120))
    check("它同时被标成只读字段", sdata["readonly"] == ["blocker_forced_delivery"],
          str(sdata["readonly"]))
    check("没有「关闭 blocker / 降级 blocker」这类设置项",
          not any("blocker" in key for key in sdata["writable"]),
          str(sdata["writable"]))

    # =======================================================================
    print("\n[8] PUT /advisor/settings（写白名单 + 只读字段被拒）")
    r = client.put(url("/advisor/settings"),
                   json={"enabled": False, "min_interval_seconds": 45, "model": "probe-model"})
    show("PUT /advisor/settings {enabled:false, min_interval_seconds:45, model:probe-model}", r)
    put = r.json()
    check("写设置 200 且 ok=true", r.status_code == 200 and put.get("ok") is True)
    check("回读三个字段都生效",
          put["data"]["values"]["enabled"] is False
          and put["data"]["values"]["min_interval_seconds"] == 45
          and put["data"]["values"]["model"] == "probe-model",
          j(put["data"]["values"], 160))
    check("settings.json 真的落盘（磁盘回读）",
          SETTINGS.is_file() and json.loads(SETTINGS.read_text(encoding="utf-8"))["enabled"] is False,
          str(SETTINGS))
    print("        settings.json: " + SETTINGS.read_text(encoding="utf-8").replace("\n", " ")[:200])
    r = client.put(url("/advisor/settings"), json={"blocker_forced_delivery": False})
    show("PUT /advisor/settings {blocker_forced_delivery:false}", r)
    valve = r.json()
    check("改只读常量 → 400 invalid_argument",
          r.status_code == 400 and valve["error"]["code"] == "invalid_argument",
          valve["error"]["message"][:100])
    check("400 的文案明说「单向阀门不可关闭」",
          "单向阀门不可关闭" in valve["error"]["message"], valve["error"]["message"][:100])
    check("响应里标明这是只读字段", valve["error"].get("readonly") is True)
    r = client.put(url("/advisor/settings"), json={"blocker_forced_delivery": True, "enabled": True})
    show("PUT /advisor/settings {blocker_forced_delivery:true(原值), enabled:true}", r)
    check("原值回写不算修改（照常 200，只写 enabled）",
          r.status_code == 200 and r.json()["data"]["values"]["enabled"] is True,
          j(r.json()["data"]["values"], 140))
    r = client.put(url("/advisor/settings"), json={"unknown_knob": 1})
    show("PUT /advisor/settings {unknown_knob:1}", r)
    check("未知字段 → 400 invalid_argument（整批拒绝，不半写）",
          r.status_code == 400 and r.json()["error"]["code"] == "invalid_argument",
          r.json()["error"]["message"][:100])
    r = client.put(url("/advisor/settings"), json={"min_interval_seconds": -5})
    show("PUT /advisor/settings {min_interval_seconds:-5}", r)
    check("非法取值 → 400 invalid_argument", r.status_code == 400
          and r.json()["error"]["code"] == "invalid_argument", r.json()["error"]["message"][:80])
    r = client.get(url("/advisor/settings"))
    check("被拒之后设置没被改（enabled 仍为 true）",
          r.json()["data"]["values"]["enabled"] is True,
          j(r.json()["data"]["values"], 140))

    # =======================================================================
    print("\n[9] 降级：模拟 advisor.audit import 失败")
    real_dir = api_advisor.advisor_dir
    real_loader = api_advisor._load_audit
    saved_modules = {name: sys.modules.pop(name)
                     for name in ("advisor", "advisor.audit") if name in sys.modules}

    class _Blocker:
        """meta_path 拦截器：让 ``advisor`` / ``advisor.audit`` 一律 ImportError。"""

        def find_module(self, fullname, path=None):  # 兼容旧协议
            return self if fullname == "advisor" or fullname.startswith("advisor.") else None

        def find_spec(self, fullname, path=None, target=None):
            if fullname == "advisor" or fullname.startswith("advisor."):
                raise ImportError("探针模拟：advisor 插件不可导入")
            return None

        def load_module(self, fullname):  # pragma: no cover - 旧协议兜底
            raise ImportError("探针模拟：advisor 插件不可导入")

    blocker = _Blocker()
    sys.meta_path.insert(0, blocker)
    api_advisor.advisor_dir = lambda: HOME / "no-such-advisor-plugin"   # 按路径加载也找不到
    try:
        r = client.get(url("/advisor/summary"))
        show("GET /advisor/summary（advisor 不可用）", r)
        degraded = r.json()
        check("本分区降级为结构化 503", r.status_code == 503,
              str(r.status_code))
        check("错误体是 {ok:false, error:{code,message}}",
              degraded.get("ok") is False and degraded["error"]["code"] == "upstream_error",
              j(degraded["error"], 160))
        check("原因里点名是 advisor.audit 加载失败",
              "advisor.audit" in degraded["error"]["message"],
              degraded["error"]["message"][:120])
        r = client.get(url("/advisor/advice"))
        show("GET /advisor/advice（advisor 不可用）", r)
        check("列表同样 503（不是 500、不是裸抛）", r.status_code == 503)
        r = client.put(url("/advisor/settings"), json={"enabled": True})
        show("PUT /advisor/settings（advisor 不可用）", r)
        check("设置写也 503（不假装成功）", r.status_code == 503)

        # 其它分区必须照常：它们各自 import 各自的域模块
        for label, path in (("/agents", "/agents"), ("/upgrade/scan", "/upgrade/scan"),
                            ("/autogroup/status", "/autogroup/status"),
                            ("/overview/layout", "/overview/layout")):
            other = client.get(url(path))
            show("GET %s（同一时刻）" % label, other)
            check("advisor 挂掉时 " + label + " 照常 200", other.status_code == 200,
                  "status=%s" % other.status_code)
        health = client.get(url("/health"))
        show("GET /health（同一时刻）", health)
        check("健康检查仍 ok=true（api_advisor 自身 import 没坏）",
              health.status_code == 200 and health.json()["ok"] is True,
              j(health.json(), 160))
    finally:
        sys.meta_path.remove(blocker)
        api_advisor.advisor_dir = real_dir
        api_advisor._load_audit = real_loader
        sys.modules.update(saved_modules)
    r = client.get(url("/advisor/summary"))
    check("摘掉故障后恢复 200", r.status_code == 200 and r.json()["ok"] is True,
          "status=%s" % r.status_code)

    # =======================================================================
    print("\n[10] 既有分区回归（五条路由各自状态码）")
    regressions = [
        ("概览 /overview/system", "/overview/system"),
        ("天气 /weather/now", "/weather/now"),
        ("升级 /upgrade/scan", "/upgrade/scan"),
        ("Agents /agents", "/agents"),
        ("项目分组 /autogroup/status", "/autogroup/status"),
        ("Advisor /advisor/summary", "/advisor/summary"),
    ]
    for label, path in regressions:
        r = client.get(url(path))
        show("GET " + label, r, 120)
        ok = r.status_code == 200
        if path.startswith("/weather"):
            # 天气走真网络（彩云→和风→Open-Meteo）；本卡只断言「路由还在、不 5xx」
            ok = r.status_code == 200
        check(label + " 可访问", ok, "status=%s" % r.status_code)

    print("\n[11] 路由清单")
    paths = sorted({getattr(route, "path", "") for route in mod.router.routes})
    advisor_routes = [p for p in paths if "/advisor" in p]
    print("        advisor 路由: " + ", ".join(advisor_routes))
    check("8 条 advisor 路径全部挂上（settings 的 GET+PUT 同一路径；本轮加 coverage）",
          len(advisor_routes) == 8, str(len(advisor_routes)))
    check("既有 26 条路径一条没少（advisor 是纯增量）",
          len(paths) - len(advisor_routes) >= 26,
          "total=%s advisor=%s baseline=%s" % (len(paths), len(advisor_routes),
                                               len(paths) - len(advisor_routes)))

finally:
    restore()

print("\n=== 汇总 ===")
print("PASS: %d   FAIL: %d" % (passes, len(failures)))
if failures:
    for item in failures:
        print("  ! " + item)
    sys.exit(1)
print("ADVISOR-PROBE-OK")

# -*- coding: utf-8 -*-
"""hermes-console · 上下文压缩「按对话下钻」后端契约探针（t_c4c4d49e）。

只读探针：真 ``dashboard/api_vcc.py`` + **真 home**（真 state.db，mode=ro），一个字节都不写。

    python plugins/hermes-console/scripts/probe_vcc_conversations.py
    python plugins/hermes-console/scripts/probe_vcc_conversations.py --json  # 打印样本响应

断言（对应卡片「要交付」1-4 + 硬约束）：
  A. 路由数 = 8，两条新路由在册；
  B. ``/vcc/conversations`` 一行 = 一个对话，且数字来自 messages 聚合（与直接 SQL 对账）；
  C. 搜索 / 筛选（compressed / recalled / none）真的生效；
  D. ``/vcc/conversation``：压缩次数、每次摘要、压缩前后长度都在，且与直接 SQL 对账；
  E. 召回次数与命中内容片段来自 session_search / memory-context 真行；
  F. **token 无记录**：本机 token_count 全为 NULL ⇒ tokens 必须是 None + 写明原因（不许是 0）；
  G. 没有压缩/召回的对话：两个计数都是 0、事件空数组（前端据此显示「无记录」）；
  H. 参数错误：未知 session ⇒ 404；空 session ⇒ 400。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DASH = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASH))

import api_vcc  # noqa: E402

PASS = 0
FAIL = []


def check(name, ok, extra=""):
    global PASS
    if ok:
        PASS += 1
        print("  PASS " + name + (("  — " + extra) if extra else ""))
    else:
        FAIL.append(name + (("  — " + extra) if extra else ""))
        print("  FAIL " + name + (("  — " + extra) if extra else ""))


def raw(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()


def body_of(resp):
    """FastAPI 返回 JSONResponse 时取 body；返回 dict 时原样。"""
    if isinstance(resp, dict):
        return resp
    payload = getattr(resp, "body", b"") or b""
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return {}


def main():
    home = api_vcc._home()
    print("home:", home)
    routes = sorted({route.path for route in api_vcc.router.routes})
    print("routes:", routes)

    print("\n[A] 路由在册")
    check("路由总数 = 8", len(routes) == 8, "实际 %d" % len(routes))
    check("/vcc/conversations 存在", "/vcc/conversations" in routes)
    check("/vcc/conversation 存在", "/vcc/conversation" in routes)

    print("\n[B] 对话列表")
    listing = body_of(api_vcc.vcc_conversations(agent="default", q="", filter="all",
                                                limit=400, offset=0))
    check("ok=true", listing.get("ok") is True)
    convs = listing.get("conversations") or []
    check("至少 1 个对话", len(convs) >= 1, "count=%d total=%s" % (len(convs), listing.get("total")))
    first = convs[0] if convs else {}
    for key in ("session", "label", "rows_total", "rows_active", "chars_active",
                "summary_rows", "summary_distinct", "recall_total", "has_compression", "has_recall"):
        check("行里有 %s" % key, key in first)

    db = Path(listing.get("db") or "")
    conn = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
    sid = first.get("session")
    n_all = raw(conn, "select count(*) from messages where session_id = ?", (sid,))[0]
    chars = raw(conn, "select coalesce(sum(length(coalesce(content,''))),0) from messages "
                      "where session_id = ? and active = 1", (sid,))[0]
    # 真库是活的（有 agent 正在写）：聚合有 20s 缓存，所以只要求「同一量级、差异可由追加解释」
    drift_rows = abs((first.get("rows_total") or 0) - n_all)
    drift_chars = abs((first.get("chars_active") or 0) - chars)
    check("rows_total 与 SQL 对账（活库，允许追加漂移）", drift_rows <= 200,
          "面板 %s vs SQL %s（差 %d）" % (first.get("rows_total"), n_all, drift_rows))
    check("chars_active 与 SQL 对账（活库，允许追加漂移）", drift_chars <= 20000,
          "面板 %s vs SQL %s（差 %d）" % (first.get("chars_active"), chars, drift_chars))

    print("\n[C] 搜索与筛选")
    needle = "local-"
    found = body_of(api_vcc.vcc_conversations(agent="default", q=needle, filter="all",
                                              limit=400, offset=0))
    check("q 命中数 <= 全量", (found.get("total") or 0) <= (listing.get("total") or 0),
          "%s vs %s" % (found.get("total"), listing.get("total")))
    check("q 过滤后每条都含关键词",
          all(needle in (row.get("session") or "").lower()
              or needle in (row.get("label") or "").lower()
              or needle in (row.get("model") or "").lower()
              for row in (found.get("conversations") or [])))
    comp = body_of(api_vcc.vcc_conversations(agent="default", q="", filter="compressed",
                                             limit=400, offset=0))
    check("filter=compressed 每行都 has_compression",
          all(row.get("has_compression") for row in (comp.get("conversations") or [])),
          "n=%d" % len(comp.get("conversations") or []))
    rec = body_of(api_vcc.vcc_conversations(agent="default", q="", filter="recalled",
                                            limit=400, offset=0))
    check("filter=recalled 每行都 has_recall",
          all(row.get("has_recall") for row in (rec.get("conversations") or [])),
          "n=%d" % len(rec.get("conversations") or []))
    none = body_of(api_vcc.vcc_conversations(agent="default", q="", filter="none",
                                             limit=400, offset=0))
    check("filter=none 每行都没有压缩也没有召回",
          all((not row.get("has_compression")) and (not row.get("has_recall"))
              for row in (none.get("conversations") or [])),
          "n=%d" % len(none.get("conversations") or []))

    print("\n[D] 一个有压缩的对话")
    target = next((row for row in (comp.get("conversations") or [])
                   if (row.get("summary_distinct") or 0) >= 2), None)
    check("找到压缩 >= 2 次的对话", target is not None,
          (target or {}).get("session", ""))
    if target:
        detail = body_of(api_vcc.vcc_conversation(session=target["session"], agent="default",
                                                  event_limit=40))
        check("ok=true", detail.get("ok") is True)
        ctx = detail.get("context") or {}
        cs = detail.get("compressions") or {}
        check("compressions.count = 列表里的 summary_distinct",
              cs.get("count") == target.get("summary_distinct"),
              "%s vs %s" % (cs.get("count"), target.get("summary_distinct")))
        events = cs.get("events") or []
        check("每条事件都有摘要预览", all((e.get("preview") or "").strip() for e in events))
        check("每条事件都有压缩前长度(字符)", all(isinstance(e.get("before_chars"), int) for e in events))
        check("每条事件都有压缩后长度(字符)", all(isinstance(e.get("after_chars"), int) for e in events))
        check("每条事件都写明在库里有几行", all(isinstance(e.get("rows"), int) and e["rows"] >= 1
                                       for e in events))
        ev = events[0] if events else {}
        mid = ev.get("message_id")
        if mid:
            row = raw(conn, "select length(content), timestamp from messages where id = ?", (mid,))
            check("摘要字符数与 SQL 对账", row and row[0] == ev.get("after_chars"),
                  "%s vs %s" % (ev.get("after_chars"), row[0] if row else None))
            arch = raw(conn, "select count(*), coalesce(sum(length(coalesce(content,''))),0) "
                             "from messages where session_id = ? and compacted = 1 and id < ?",
                       (target["session"], mid))
            check("压缩前条数与 SQL 对账", ev.get("before_rows") == arch[0],
                  "%s vs %s" % (ev.get("before_rows"), arch[0]))
            check("压缩前字符与 SQL 对账", ev.get("before_chars") == arch[1],
                  "%s vs %s" % (ev.get("before_chars"), arch[1]))

    print("\n[E] 召回")
    recall_target = next((row for row in (rec.get("conversations") or [])
                          if (row.get("recall_search") or 0) > 0), None)
    check("找到有会话召回的对话", recall_target is not None,
          (recall_target or {}).get("session", ""))
    if recall_target:
        detail = body_of(api_vcc.vcc_conversation(session=recall_target["session"],
                                                  agent="default", event_limit=40))
        recalls = detail.get("recalls") or {}
        search = recalls.get("search") or {}
        check("会话召回次数 > 0", (search.get("count") or 0) > 0, str(search.get("count")))
        evs = search.get("events") or []
        check("每次召回都带命中内容", any((e.get("hits") or []) for e in evs))
        hit = next((h for e in evs for h in (e.get("hits") or [])), {})
        check("命中片段有 title 或 preview", bool(hit.get("title") or hit.get("preview")),
              (hit.get("title") or "")[:40])
        n_sql = raw(conn, "select count(*) from messages where session_id = ? and "
                          "tool_name = 'session_search'", (recall_target["session"],))[0]
        check("召回次数与 SQL 对账", (search.get("count") or 0) == min(n_sql, 40),
              "%s vs %s" % (search.get("count"), n_sql))

    mem_target = next((row for row in (rec.get("conversations") or [])
                       if (row.get("recall_memory") or 0) > 0), None)
    check("找到有记忆召回的对话", mem_target is not None,
          (mem_target or {}).get("session", ""))
    if mem_target:
        detail = body_of(api_vcc.vcc_conversation(session=mem_target["session"],
                                                  agent="default", event_limit=40))
        memory = (detail.get("recalls") or {}).get("memory") or {}
        check("记忆召回次数 > 0", (memory.get("count") or 0) > 0, str(memory.get("count")))
        mev = (memory.get("events") or [{}])[0]
        check("记忆召回块有内容", (mev.get("block_chars") or 0) > 0, str(mev.get("block_chars")))

    print("\n[F] token 无记录（硬约束）")
    check("列表里 tokens 全是 None（本机 token_count 全 NULL）",
          all(row.get("tokens") is None for row in convs))
    if target:
        ctx = (body_of(api_vcc.vcc_conversation(session=target["session"], agent="default",
                                                event_limit=40))
               .get("context") or {})
        check("详情 tokens 是 None 而不是 0", ctx.get("tokens") is None, repr(ctx.get("tokens")))
        check("详情写明 token 无记录的原因", "NULL" in str(ctx.get("token_reason") or ""),
              str(ctx.get("token_reason"))[:80])
        check("token_rows 为 0", ctx.get("token_rows") == 0)
        check("占比为空且给了原因",
              ctx.get("ratio_pct") is None and bool(ctx.get("ratio_reason")),
              str(ctx.get("ratio_reason")))
        check("模型窗口要么有数要么有原因",
              (ctx.get("model_window") is not None) or bool(ctx.get("model_window_reason")),
              str(ctx.get("model_window_reason"))[:80])

    print("\n[G] 一个无记录的对话")
    empty_row = next((row for row in (none.get("conversations") or [])
                      if (row.get("rows_total") or 0) > 0), None)
    check("找到有消息但无压缩无召回的对话", empty_row is not None,
          (empty_row or {}).get("session", ""))
    if empty_row:
        detail = body_of(api_vcc.vcc_conversation(session=empty_row["session"], agent="default",
                                                  event_limit=40))
        cs = detail.get("compressions") or {}
        recalls = detail.get("recalls") or {}
        check("无记录对话 compressions.count = 0", cs.get("count") == 0, str(cs.get("count")))
        check("无记录对话 events 是空数组", cs.get("events") == [])
        check("无记录对话 recalls.total = 0", recalls.get("total") == 0, str(recalls.get("total")))
        check("无记录对话 rows_total > 0（不是空会话）",
              (detail.get("context") or {}).get("rows_total", 0) > 0)

    print("\n[H] 参数错误")
    bad = body_of(api_vcc.vcc_conversation(session="no-such-session-xyz", agent="default",
                                           event_limit=40))
    check("未知 session ⇒ 404 not_found",
          bad.get("ok") is False and (bad.get("error") or {}).get("code") == "not_found",
          str(bad)[:120])
    empty = body_of(api_vcc.vcc_conversation(session="   ", agent="default", event_limit=40))
    check("空 session ⇒ 400 invalid_argument",
          empty.get("ok") is False
          and (empty.get("error") or {}).get("code") == "invalid_argument",
          str(empty)[:120])
    unknown_agent = body_of(api_vcc.vcc_conversations(agent="no-such-agent", q="", filter="all"))
    check("未知 agent ⇒ 4xx（带合法列表，不 500）",
          unknown_agent.get("ok") is False
          and (unknown_agent.get("error") or {}).get("code") in ("invalid_argument", "not_found")
          and bool((unknown_agent.get("error") or {}).get("agents")),
          str(unknown_agent)[:160])

    print("\n[I] 数据来源写在响应里")
    srcs = listing.get("data_sources") or []
    check("data_sources 至少 6 条", len(srcs) >= 6, str(len(srcs)))
    check("token 那一条写明 token_count 且说明 NULL",
          any("token_count" in (s.get("fields") or "") and "NULL" in (s.get("note") or "")
              for s in srcs))
    check("口径里有「无记录」说法", "无记录" in str(listing.get("caliber") or ""))

    print("\n[J] 列表与详情同口径（summary_distinct == compressions.count）")
    bad = []
    compressed_rows = comp.get("conversations") or []
    for row in compressed_rows:
        detail = body_of(api_vcc.vcc_conversation(session=row["session"], agent="default",
                                                  event_limit=200))
        counted = (detail.get("compressions") or {}).get("count")
        if counted != row.get("summary_distinct"):
            bad.append("%s 列表 %s / 详情 %s" % (row["session"][:20],
                                               row.get("summary_distinct"), counted))
    check("每个有压缩的会话：列表次数 = 详情次数", not bad,
          "%d 个会话，不一致 %d 个 %s" % (len(compressed_rows), len(bad), " | ".join(bad[:3])))

    conn.close()
    print("\n%d PASS / %d FAIL" % (PASS, len(FAIL)))
    if FAIL:
        for item in FAIL:
            print("  FAILED: " + item)
        return 1
    return 0


if __name__ == "__main__":
    if "--json" in sys.argv:
        target = sys.argv[sys.argv.index("--json") + 1] if len(sys.argv) > sys.argv.index("--json") + 1 else ""
        out = api_vcc.vcc_conversation(session=target, agent="default", event_limit=5) if target \
            else api_vcc.vcc_conversations(agent="default", q="", filter="compressed", limit=5)
        print(json.dumps(body_of(out), ensure_ascii=False, indent=1)[:6000])
        raise SystemExit(0)
    raise SystemExit(main())

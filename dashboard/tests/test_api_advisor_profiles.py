# -*- coding: utf-8 -*-
"""面板侧：agent 过滤、跨 profile 聚合、三种台账状态。

这些用例打的是**面板自己的读法**（api_advisor 直接按文件读各 profile 的台账），
所以在临时 home 里造出「主 home 有建议、worker 只有评审流水、另一个 worker 连台账
都没有」三种局面，再验证：

* ``?agent=code-worker`` 与 ``?agent=default`` 返回**不同**数据；
* 每行都带 ``profile_source``，并单列每个 agent 的台账状态；
* 决定写回**建议所在的**台账，不串到 default。
"""

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import api_advisor  # noqa: E402


def _write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _advice_event(note: str, profile: str, session_id: str, ts: str, event: str = "raised") -> dict:
    return {
        "schema_version": 1,
        "event": event,
        "id": api_advisor_id(note, session_id),
        "ts": ts,
        "profile": profile,
        "session_id": session_id,
        "turn_id": "t1",
        "severity": "concern",
        "note": note,
        "decision": None,
        "reason": "",
        "actor": "program",
        "source": "runtime",
    }


def api_advisor_id(note: str, session_id: str) -> str:
    import hashlib

    key = " ".join((note or "").split()).casefold()
    return hashlib.sha256(f"{key}\x00{session_id}".encode("utf-8")).hexdigest()[:16]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """临时 home：default 有建议，code-worker 只有评审流水，work 什么都没有。"""
    default_audit = tmp_path / "advisor" / "audit"
    _write_jsonl(default_audit / "advice.jsonl", [
        _advice_event("default 的建议", "default", "s-default", "2026-10-30T10:00:00.000+08:00"),
    ])
    _write_jsonl(default_audit / "reviews.jsonl", [
        {"schema_version": 1, "ts": "2026-10-30T10:00:00.000+08:00", "review_id": "rv-default",
         "outcome": "advised", "profile": "default", "session_id": "s-default",
         "turn_id": "t1", "task_id": "", "skip_reason": "", "advice_count": 1,
         "severity_counts": {"concern": 1}, "model": "m", "provider": "p", "tokens": 10,
         "duration_ms": 5, "audit_events": 1, "audit_error": "",
         "identity_source": "env", "identity_unresolved": False},
    ])

    worker_audit = tmp_path / "profiles" / "code-worker" / "advisor" / "audit"
    # 台账文件存在但没有任何事件：评审跑过、没发现问题的那种局面
    _write_jsonl(worker_audit / "advice.jsonl", [])
    _write_jsonl(worker_audit / "reviews.jsonl", [
        {"schema_version": 1, "ts": "2026-10-30T16:20:53.000+08:00", "review_id": "rv-worker",
         "outcome": "no_advice", "profile": "code-worker", "session_id": "local-3bee",
         "turn_id": "t7", "task_id": "t_a562c864", "skip_reason": "", "advice_count": 0,
         "severity_counts": {}, "model": "deepseek-v4.1-flash", "provider": "balance",
         "tokens": 35980, "duration_ms": 1200, "audit_events": 0, "audit_error": "",
         "identity_source": "env", "identity_unresolved": False},
    ])
    (tmp_path / "profiles" / "work").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(api_advisor, "_home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def client(home):
    app = FastAPI()
    app.include_router(api_advisor.router, prefix="/api/plugins/hermes-console")
    return TestClient(app)


def _get(client, path: str, **params):
    response = client.get("/api/plugins/hermes-console" + path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ── agent 过滤真的过滤 ──────────────────────────────────────────────────────


def test_agent_filter_returns_different_data(client):
    default = _get(client, "/advisor/advice", agent="default")
    worker = _get(client, "/advisor/advice", agent="code-worker")

    assert default["data"]["total"] == 1
    assert [row["note"] for row in default["data"]["items"]] == ["default 的建议"]
    assert default["data"]["items"][0]["profile_source"] == "default"

    # worker 没有建议：与 default 的结果不同（不是同一份数据）
    assert worker["data"]["total"] == 0
    assert worker["data"]["items"] == []
    assert worker["data"]["ledger"].endswith("code-worker\\advisor\\audit\\advice.jsonl") \
        or worker["data"]["ledger"].endswith("code-worker/advisor/audit/advice.jsonl")


def test_aggregate_view_lists_every_agent_with_its_state(client):
    payload = _get(client, "/advisor/advice")
    agents = {row["profile"]: row for row in payload["data"]["agents"]}

    assert set(agents) >= {"default", "code-worker", "work"}
    assert agents["default"]["state"] == "has_advice"
    assert agents["code-worker"]["state"] == "no_advice"
    assert agents["work"]["state"] == "no_ledger"
    assert payload["data"]["total"] == 1, "聚合时只有 default 那条建议"
    assert payload["data"]["items"][0]["profile_source"] == "default"


def test_notes_explain_why_an_agent_is_empty(client):
    payload = _get(client, "/advisor/advice", agent="code-worker")
    notes = " ".join(payload["notes"])
    assert "有台账文件但没有任何建议" in notes
    assert "评审流水" in notes


def test_unknown_agent_is_rejected_with_the_known_list(client):
    response = client.get("/api/plugins/hermes-console/advisor/advice", params={"agent": "nope"})
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_argument"
    assert "code-worker" in body["known_agents"]


# ── 评审流水就是「跑没跑」的证据 ────────────────────────────────────────────


def test_reviews_endpoint_shows_no_advice_review_with_cost(client):
    payload = _get(client, "/advisor/reviews", agent="code-worker")
    assert payload["data"]["total"] == 1
    record = payload["data"]["items"][0]
    assert record["outcome"] == "no_advice"
    assert record["model"] == "deepseek-v4.1-flash"
    assert record["tokens"] == 35980
    assert record["profile_source"] == "code-worker"
    assert record["identity_unresolved"] is False


def test_summary_aggregates_and_reports_review_outcomes(client):
    payload = _get(client, "/advisor/summary")
    assert payload["data"]["total"] == 1
    assert payload["data"]["by_severity"]["concern"] == 1
    assert payload["data"]["reviews"]["advised"] == 1
    assert payload["data"]["reviews"]["no_advice"] == 1
    assert {row["profile"] for row in payload["data"]["agents"]} >= {"default", "code-worker"}


# ── 建议在哪个 profile，就在哪个 profile 读写 ──────────────────────────────


def test_detail_and_decision_work_on_a_worker_ledger(client, home):
    worker_audit = home / "profiles" / "code-worker" / "advisor" / "audit"
    note = "worker 自己的建议"
    advice_id = api_advisor_id(note, "local-3bee")
    _write_jsonl(worker_audit / "advice.jsonl", [
        _advice_event(note, "code-worker", "local-3bee", "2026-10-30T16:30:00.000+08:00"),
    ])

    detail = _get(client, "/advisor/advice/" + advice_id)
    assert detail["data"]["note"] == note
    assert detail["data"]["profile_source"] == "code-worker"
    assert "code-worker" in " ".join(detail["notes"])

    response = client.post("/api/plugins/hermes-console/advisor/decision",
                           json={"id": advice_id, "decision": "accepted", "reason": "ok"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"]["agent"] == "code-worker"
    assert body["data"]["readback"]["status"] == "accepted"

    # 决定写进了 worker 的台账，没有串到 default
    default_lines = (home / "advisor" / "audit" / "advice.jsonl").read_text(encoding="utf-8")
    assert "accepted" not in default_lines
    worker_lines = (worker_audit / "advice.jsonl").read_text(encoding="utf-8")
    assert "accepted" in worker_lines


def test_rejection_still_requires_a_reason(client, home):
    worker_audit = home / "profiles" / "code-worker" / "advisor" / "audit"
    note = "worker 的另一条建议"
    advice_id = api_advisor_id(note, "local-3bee")
    _write_jsonl(worker_audit / "advice.jsonl", [
        _advice_event(note, "code-worker", "local-3bee", "2026-10-30T16:31:00.000+08:00"),
    ])

    response = client.post("/api/plugins/hermes-console/advisor/decision",
                           json={"id": advice_id, "decision": "rejected", "reason": ""})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_argument"


def test_self_check_lists_agents(client):
    payload = _get(client, "/advisor")
    assert "review_outcomes_supported" in payload["data"]
    assert {row["profile"] for row in payload["data"]["agents"]} >= {"default", "code-worker"}
    assert "GET /advisor/reviews" in payload["data"]["routes"]

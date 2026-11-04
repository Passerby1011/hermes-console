# -*- coding: utf-8 -*-
"""详情端点也必须带 goal_source（详情走 _find_advice → 带 events 的 item，路径不同）。"""

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

DASHBOARD_DIR = Path(__file__).resolve().parents[1]
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import api_advisor  # noqa: E402

#: 真读 ``plugins/advisor/audit.py``：受限环境不可读时跳过，不把 ACL 问题报成代码回归。
_ADVISOR_AUDIT = Path(api_advisor.advisor_dir()) / "audit.py"


def _audit_readable(path: Path) -> bool:
    #: Windows ACL 会让 ``Path.is_file()`` / ``os.stat`` 直接抛 ``PermissionError``，
    #: 这里吞掉异常，把「环境不可读」当成 skip 条件而不是收集错误。
    try:
        return os.path.isfile(str(path)) and os.access(str(path), os.R_OK)
    except OSError:
        return False


if not _audit_readable(_ADVISOR_AUDIT):
    pytest.skip("advisor 台账插件不可读（环境限制）：" + str(_ADVISOR_AUDIT),
                allow_module_level=True)

PREFIX = "/api/plugins/hermes-console"


def _client(monkeypatch, home):
    app = FastAPI()
    app.include_router(api_advisor.router, prefix=PREFIX)
    monkeypatch.setattr(api_advisor, "_home", lambda: home)
    return TestClient(app)


def test_advice_detail_carries_goal_source(monkeypatch, tmp_path):
    # 先把**真实**的 advisor.audit 加载一次（此时 home 还是真 home）：临时 home 下的
    # plugins/ 只有 plugin.yaml，是命名空间包，会遮住真正的包。这与生产加载顺序一致。
    assert hasattr(api_advisor._load_audit(), "fold")
    (tmp_path / "plugins" / "advisor").mkdir(parents=True)
    (tmp_path / "plugins" / "advisor" / "plugin.yaml").write_text(
        'name: advisor\nversion: "1.4.0"\n', encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - advisor\n  entries:\n    advisor:\n"
        "      allow_gateway_injection: true\n", encoding="utf-8")
    audit_dir = tmp_path / "advisor" / "audit"
    audit_dir.mkdir(parents=True)
    event = {
        "schema_version": 1, "event": "raised", "id": "id-d", "ts": "2026-10-30T11:00:00.000+08:00",
        "profile": "default", "session_id": "s-d", "turn_id": "t1", "severity": "concern",
        "note": "详情带目标", "decision": None, "reason": "", "actor": "program",
        "source": "runtime", "goal_source": "kanban", "goal_ref": "t_6c6fffd3",
    }
    (audit_dir / "advice.jsonl").write_text(
        json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
    client = _client(monkeypatch, tmp_path)
    response = client.get(PREFIX + "/advisor/advice/id-d")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["goal_source"] == "kanban"
    assert data["goal_ref"] == "t_6c6fffd3"
    assert data["goal_known"] is True

# -*- coding: utf-8 -*-
"""模型分区「备用链 + 辅助槽位」契约在**真实 home** 上的**只读**核验（零写入）。

**这个脚本一个字节都不写**：只 GET、只调纯函数；开头 / 结尾各算一次生产指纹并断言相同。

它回答三个"沙盒证明不了"的问题：

1. 真实 9 个 agent 的 ``auxiliary`` 段，``GET /models/overview`` 的 ``auxiliary_slots``
   是不是**按各自配置动态生成**的（数量 / 槽位名与 yaml 逐字一致）；
2. 真实配置里每个槽位**当前的 provider** 能不能通过 ``scope=auxiliary`` 的 provider
   校验（``_aux_provider_value``，纯函数，不写盘）—— 这决定"现有值改得动吗"；
3. 真实 ``fallback_providers`` 链能不能被 ``_checks_for`` 的整链 / 任意下标口径完整比对。

跑法::

    <venv>/python.exe plugins/hermes-console/scripts/probe_models_slots_real.py
    echo "exit=$?"
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
REAL_HOME = PLUGIN_ROOT.parents[1]
HERMES_AGENT = REAL_HOME / "hermes-agent"
PREFIX = "/api/plugins/hermes-console"
MODELS = PREFIX + "/models"

sys.path.insert(0, str(HERMES_AGENT))
sys.path.insert(0, str(DASHBOARD))

passed = 0
failures: list = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    global passed
    print(("  PASS  " if ok else "  FAIL  ") + label + (" — " + detail if detail else ""))
    if ok:
        passed += 1
    else:
        failures.append(label + (" — " + detail if detail else ""))
    return ok


def sha256_of(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "<missing>"


def fingerprint() -> dict:
    paths = [REAL_HOME / "config.yaml"]
    profiles = REAL_HOME / "profiles"
    if profiles.is_dir():
        paths.extend(sorted(profiles.glob("*/config.yaml")))
    return {str(item): sha256_of(item) for item in paths}


def secret_values() -> dict:
    """真实 .env 里的密钥类取值（只用于"不许出现在响应里"的比对，从不打印）。"""
    found: dict = {}
    paths = [REAL_HOME / ".env"]
    profiles = REAL_HOME / "profiles"
    if profiles.is_dir():
        paths.extend(sorted(profiles.glob("*/.env")))
    markers = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, _, value = stripped.partition("=")
            name = name.strip().upper()
            value = value.strip().strip("\"'")
            if len(value) < 8 or "://" in value:
                continue
            if any(marker in name for marker in markers):
                found["%s@%s" % (name, path.parent.name)] = value
    return found


def main() -> int:
    before = fingerprint()
    print("[前置] 生产指纹（%d 个 config.yaml）" % len(before))
    for path, digest in before.items():
        print("        %s  %s" % (digest[:16], path.replace("\\", "/")))

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_hermes_console", str(DASHBOARD / "plugin_api.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(module.router, prefix=PREFIX)
    client = TestClient(app, raise_server_exceptions=False)
    import api_models

    print("[env] hermes_home :", api_models._home())
    check("域模块没有加载失败", module.DOMAIN_FAILURES == {}, json.dumps(module.DOMAIN_FAILURES))

    resp = client.get(MODELS + "/overview")
    body = resp.json()
    rows = body.get("agents") or []
    check("GET /models/overview 200", resp.status_code == 200, str(resp.status_code))
    check("scopes 四段齐全（真实 home）",
          body.get("scopes") == ["model", "fallback", "fallbacks", "auxiliary"],
          str(body.get("scopes")))
    print("        真实 agent 数 : %d" % len(rows))

    total_slots = 0
    total_fallbacks = 0
    for row in rows:
        agent = str(row.get("agent") or "")
        cfg = api_models._read_config(agent)
        real_slots = sorted((cfg.get("auxiliary") or {}).keys()) if isinstance(
            cfg.get("auxiliary"), dict) else []
        api_slots = [str(item.get("slot")) for item in row.get("auxiliary_slots") or []]
        ok = api_slots == real_slots
        total_slots += len(api_slots)
        print("        %-16s auxiliary_slots=%d fallbacks=%d %s"
              % (agent, len(api_slots), len(row.get("fallbacks") or []),
                 "OK" if ok else "MISMATCH"))
        if not check("%s 的 auxiliary_slots 与 config.yaml 逐字一致（动态生成）" % agent,
                     ok, json.dumps({"api": api_slots, "cfg": real_slots}, ensure_ascii=False)):
            continue
        # 每行的 writable_fields 必须动态（provider/model 恒在；key_env 永不出现）
        bad = [item.get("slot") for item in row.get("auxiliary_slots") or []
               if "provider" not in (item.get("writable_fields") or [])
               or "model" not in (item.get("writable_fields") or [])
               or any(name in (item.get("writable_fields") or []) for name in ("key_env",))]
        check("%s 每个槽位的 writable_fields 含 provider/model 且绝不含 key_env" % agent,
              not bad, json.dumps(bad, ensure_ascii=False))
        # 真实值能不能通过 provider 校验（纯函数，不写盘）
        rejected = []
        for item in row.get("auxiliary_slots") or []:
            slot = str(item.get("slot"))
            current = str(item.get("provider") or "")
            if not current:
                continue
            try:
                api_models._aux_provider_value(agent, slot, current, current)
            except api_models.ApiError as exc:
                rejected.append("%s=%s(%s)" % (slot, current, exc.code))
        check("%s 现有槽位 provider 全部能通过 scope=auxiliary 的校验（改得动）" % agent,
              not rejected, json.dumps(rejected, ensure_ascii=False))
        # 备用链整链口径
        chain = row.get("fallbacks") or []
        total_fallbacks += len(chain)
        if chain:
            expected = {"fallbacks.count": str(len(chain))}
            for index, entry in enumerate(chain):
                expected["fallbacks[%d].provider" % index] = entry["provider"]
                expected["fallbacks[%d].model" % index] = entry["model"]
            expected["fallbacks.chain"] = json.dumps(chain, ensure_ascii=False, sort_keys=True)
            checks = api_models._checks_for(api_models._current_state(agent), expected)
            check("%s 的备用链 %d 条能被 count/任意下标/整链口径全部比对通过"
                  % (agent, len(chain)), all(item["ok"] for item in checks),
                  json.dumps([c for c in checks if not c["ok"]], ensure_ascii=False)[:200])

    print("\n        合计辅助槽位 %d 个 / 备用链条目 %d 条（真实 home）" % (total_slots, total_fallbacks))
    check("真实 home 上至少 8 个辅助槽位（default profile 实测）", total_slots >= 8, str(total_slots))

    # ---- 凭据：真实 .env 的密钥取值一个都不许出现在 overview 响应里 ----
    secrets = secret_values()
    text = resp.text
    leaked = [name for name, value in secrets.items() if value in text]
    check("★overview 响应里不含真实 .env 的任何密钥取值（%d 条比对）" % len(secrets),
          not leaked, "泄漏 %d 条（只报键名）: %s" % (len(leaked), [n.split("@")[0] for n in leaked[:5]]))
    check("overview 响应里不含 'sk-' 形态的串", "sk-" not in text,
          "命中 %d 次" % text.count("sk-"))

    # ---- 收尾：生产零污染 ----
    after = fingerprint()
    changed = [path for path in before if before[path] != after.get(path)]
    print("\n[收尾] 生产零污染：%d 个 config.yaml，变化 %d 个" % (len(before), len(changed)))
    check("★9 个 config.yaml 一个字节都没变（只读脚本）", not changed, str(changed))

    print("\nPASS=%d FAIL=%d TOTAL=%d" % (passed, len(failures), passed + len(failures)))
    if failures:
        print("MODELS-SLOTS-REAL-FAILED")
        for item in failures:
            print("  - " + item)
        return 1
    print("MODELS-SLOTS-REAL-OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

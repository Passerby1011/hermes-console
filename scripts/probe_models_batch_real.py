# -*- coding: utf-8 -*-
"""模型分区「批量设模型」**真 home** 验收：真改一个 agent，再改回来（逐字节）。

与 ``probe-models-batch.mjs``（浏览器 + 沙盒）互补：那个证明 UI 五步都对，
这个证明**真实 profile 的 config.yaml** 能被改、能回读、能一键还原到逐字节相同。

选 ``media-worker`` 作为靶子：它当前没有在跑的进程（``processes.json`` 里没有），
且改的是同一个 provider 下的另一个模型名 —— 万一中途失败，影响面也最小。

跑法::

    <venv>/python.exe plugins/hermes-console/scripts/probe_models_batch_real.py
    <venv>/python.exe plugins/hermes-console/scripts/probe_models_batch_real.py --agent media-worker
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
REAL_HOME = PLUGIN_ROOT.parents[1]
HERMES_AGENT = REAL_HOME / "hermes-agent"
PREFIX = "/api/plugins/hermes-console"

for _p in (str(HERMES_AGENT), str(DASHBOARD)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

failures: list = []
checks_passed = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global checks_passed
    print(("  PASS  " if ok else "  FAIL  ") + label + (" — " + detail if detail else ""))
    if ok:
        checks_passed += 1
    else:
        failures.append(label + (" — " + detail if detail else ""))
    return ok


def sha256_of(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def build_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_hermes_console", str(DASHBOARD / "plugin_api.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix=PREFIX)
    return TestClient(app), module


def all_configs() -> dict:
    paths = [REAL_HOME / "config.yaml"]
    profiles = REAL_HOME / "profiles"
    if profiles.is_dir():
        paths.extend(sorted(item / "config.yaml" for item in profiles.iterdir()
                            if (item / "config.yaml").is_file()))
    return {str(path): sha256_of(path) for path in paths}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", default="media-worker")
    args = parser.parse_args(argv)
    agent = args.agent

    print("[env] real home:", REAL_HOME)
    print("[env] agent    :", agent)
    client, module = build_client()
    print("[env] routes   :", len(module.router.routes), "| failed:", module.DOMAIN_FAILURES or "{}")

    overview = client.get(PREFIX + "/models/overview", params={"agent": agent}).json()
    row = (overview.get("agents") or [{}])[0]
    print("[before]", json.dumps({k: row.get(k) for k in ("agent", "provider", "model", "base_url")},
                                ensure_ascii=False))
    check("目标 agent 有 config.yaml", bool(row.get("config_exists")), str(row.get("config_path")))

    providers = client.get(PREFIX + "/models/providers", params={"agent": agent}).json()
    provider_names = [str(item.get("provider")) for item in providers.get("providers") or []]
    check("provider 目录里能看到 moa（本卡第一诉求）", "moa" in provider_names, ",".join(provider_names[:12]))

    before_all = all_configs()
    data_existed = (PLUGIN_ROOT / "data").is_dir()
    before_self = before_all[str(REAL_HOME / "profiles" / agent / "config.yaml")] \
        if (REAL_HOME / "profiles" / agent / "config.yaml").is_file() else ""

    # 目标：同 provider 换一个模型名（真改，但路由不变，影响面最小）
    provider = str(row.get("provider") or "")
    current_model = str(row.get("model") or "")
    models = client.get(PREFIX + "/models/models", params={"agent": agent, "provider": provider}).json()
    candidates = [name for name in (models.get("models") or []) if name != current_model]
    target_model = candidates[0] if candidates else "probe-model-xyz"
    print("[plan] %s: %s / %s  →  %s / %s" % (agent, provider, current_model, provider, target_model))

    # ---- 1. 预览（纯读） ---------------------------------------------------
    preview = client.post(PREFIX + "/models/batch/preview",
                          json={"agents": [agent], "scope": "model",
                                "provider": provider, "model": target_model})
    body = preview.json()
    print("[preview]", json.dumps(body.get("agents"), ensure_ascii=False)[:400])
    check("★预览 200 且列出将写的字段",
          preview.status_code == 200 and bool((body.get("agents") or [{}])[0].get("changes")),
          str(preview.status_code))
    check("★预览没有改一个字节（sha256 全等）", all_configs() == before_all)

    # ---- 2. 真写 -----------------------------------------------------------
    applied = client.post(PREFIX + "/models/batch/apply",
                          json={"agents": [agent], "scope": "model", "provider": provider,
                                "model": target_model, "confirm": True, "note": "真机验收：批量改一个再改回"})
    ab = applied.json()
    print("[apply]", json.dumps({k: ab.get(k) for k in ("ok", "batch_id", "ok_count", "failed_count")},
                                ensure_ascii=False))
    result = (ab.get("results") or [{}])[0]
    print("        verify:", json.dumps(result.get("verify"), ensure_ascii=False))
    check("★真写成功且回读校验通过", applied.status_code == 200 and ab.get("ok") is True
          and result.get("verified") is True, str(applied.status_code))
    after_self_text = (REAL_HOME / "profiles" / agent / "config.yaml").read_text(encoding="utf-8")
    check("★磁盘上真的变成目标模型（回读，不信接口自述）",
          ("default: " + target_model) in after_self_text, target_model)
    check("★写前留了快照（快照文件存在）", Path(str(result.get("snapshot_path") or "")).is_file(),
          str(result.get("snapshot_path")))
    others = {path: sha for path, sha in all_configs().items()
              if path != str(REAL_HOME / "profiles" / agent / "config.yaml")}
    check("★别的 agent 一个字节都没动", all(others[p] == before_all[p] for p in others),
          "changed=" + str([p for p in others if others[p] != before_all.get(p)]))

    # ---- 3. 一键回滚 -------------------------------------------------------
    rolled = client.post(PREFIX + "/models/batch/rollback",
                         json={"batch_id": ab.get("batch_id"), "confirm": True})
    rb = rolled.json()
    print("[rollback]", json.dumps({k: rb.get(k) for k in ("ok", "ok_count", "failed_count")},
                                   ensure_ascii=False))
    check("★一键回滚成功", rolled.status_code == 200 and rb.get("ok") is True, str(rolled.status_code))
    check("★回滚后 config.yaml 与改前**逐字节相同**",
          sha256_of(REAL_HOME / "profiles" / agent / "config.yaml") == before_self,
          before_self[:16])
    check("★全部 9 份 config.yaml 都回到原样", all_configs() == before_all)

    # ---- 4. 账本可查 -------------------------------------------------------
    ledger = client.get(PREFIX + "/models/batches").json()
    hit = [item for item in ledger.get("batches") or [] if item.get("batch_id") == ab.get("batch_id")]
    check("★账本里能查到这次批量（并记下已回滚）",
          bool(hit) and bool((hit[0].get("rollback") or {}).get("at")),
          json.dumps(hit[0], ensure_ascii=False)[:200] if hit else "not found")

    print("\nPASS=%d FAIL=%d" % (checks_passed, len(failures)))
    if failures:
        print("MODELS-BATCH-REAL-FAILED")
        for item in failures:
            print("  - " + item)
        return 1
    print("MODELS-BATCH-REAL-OK")

    # 收尾：本次真写会在**真实插件目录**里留下 data/（快照 + 账本）。
    # 配置已经逐字节还原，这些只剩"证据"价值 ⇒ 挪到探针输出目录，别污染插件根目录
    # （probe_models.py 有一条断言就是"插件根目录不该凭空多出 data/"）。
    data_dir = PLUGIN_ROOT / "data"
    if not data_existed and data_dir.is_dir():
        target = PLUGIN_ROOT / "scripts" / "_probe_out" / "models-batch" / "real-write-evidence"
        target.mkdir(parents=True, exist_ok=True)
        dest = target / ("data-" + str(ab.get("batch_id") or "run"))
        try:
            data_dir.rename(dest)
            print("[cleanup] 快照/账本已归档到 %s" % dest)
        except OSError as exc:
            print("[cleanup] 归档失败（data/ 留在原处）：%s" % exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

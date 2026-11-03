# -*- coding: utf-8 -*-
"""模型分区「批量设模型」后端契约探针（沙盒 home，零真机写入）。

覆盖浏览器探针覆盖不到的地方：

* 预览是纯读（sha256 前后全等），缺参数/非法 provider 是 400 不是 500；
* 批量写入**缺 confirm** ⇒ 400 ``confirmation_required``；
* 某个 agent 没有 ``config.yaml`` ⇒ **只有那一条**失败（``snapshot_failed``），
  别的 agent 照常写入，账本里两条状态分明；
* 一键回滚让**每个 agent 回到它自己的原值**（两个 agent 原值不同，不能串）；
* 回滚到不存在的 batch_id ⇒ 400 且带现有 id 列表；
* ★ ``_write_official`` 的失败关闭：**除白名单外**任何键被省略都必须拒绝
  （防止"整篇覆盖"退化）。这条用直接调内部函数的方式证；
* ★ 切到 moa 时 ``model.key_env`` 被删掉、``base_url`` 写成 ``moa://local``。

跑法::

    <venv>/python.exe plugins/hermes-console/scripts/probe_models_batch_api.py
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
REAL_HOME = PLUGIN_ROOT.parents[1]
HERMES_AGENT = REAL_HOME / "hermes-agent"
PREFIX = "/api/plugins/hermes-console"

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
        return ""


ALPHA = """model:
  default: alpha-model-1
  provider: palpha
  base_url: https://alpha.example.com/v1
  api_mode: chat_completions
  key_env: ALPHA_API_KEY
providers:
  palpha:
    name: Alpha
    base_url: https://alpha.example.com/v1
    key_env: ALPHA_API_KEY
  pmoa:
    name: MoA
moa:
  default_preset: 星槎
  presets:
    星槎:
      enabled: true
    双璧:
      enabled: true
fallback_providers:
  - provider: palpha
    model: alpha-backup
agent:
  max_turns: 30
"""

BETA = """model:
  default: beta-model-9
  provider: pbeta
  base_url: https://beta.example.com/v1
  api_mode: chat_completions
providers:
  pbeta:
    name: Beta
    base_url: https://beta.example.com/v1
"""


def build_home(root: Path) -> None:
    (root / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    (root / "profiles" / "beta").mkdir(parents=True, exist_ok=True)
    (root / "profiles" / "no-cfg").mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("model:\n  default: root-model\n  provider: palpha\n",
                                      encoding="utf-8")
    (root / "profiles" / "alpha" / "config.yaml").write_text(ALPHA, encoding="utf-8")
    (root / "profiles" / "beta" / "config.yaml").write_text(BETA, encoding="utf-8")
    for name in ("alpha", "beta", "no-cfg"):
        (root / "profiles" / name / "profile.yaml").write_text(
            "id: %s\ntitle: %s\n" % (name, name), encoding="utf-8")
    (root / ".env").write_text("ALPHA_API_KEY=sk-sentinel-alpha-value\n", encoding="utf-8")


def load_module():
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_hermes_console", str(DASHBOARD / "plugin_api.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    sandbox = Path(tempfile.mkdtemp(prefix="models-batch-api-"))
    try:
        build_home(sandbox)
        os.environ["HERMES_HOME"] = str(sandbox)

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        module = load_module()
        app = FastAPI()
        app.include_router(module.router, prefix=PREFIX)
        client = TestClient(app)
        import api_models

        print("[env] sandbox:", sandbox)
        print("[env] routes :", len(module.router.routes), "| failed:", module.DOMAIN_FAILURES or "{}")
        check("没有域模块加载失败", module.DOMAIN_FAILURES == {}, json.dumps(module.DOMAIN_FAILURES))

        alpha_cfg = sandbox / "profiles" / "alpha" / "config.yaml"
        beta_cfg = sandbox / "profiles" / "beta" / "config.yaml"
        before = {str(p): sha256_of(p) for p in (alpha_cfg, beta_cfg,
                                                 sandbox / "config.yaml")}

        # ---- [1] 预览是纯读 -------------------------------------------------
        print("\n[1] /batch/preview 纯读 + 参数校验")
        prev = client.post(PREFIX + "/models/batch/preview",
                           json={"agents": ["alpha", "beta"], "scope": "model",
                                 "provider": "moa", "model": "星槎"})
        body = prev.json()
        rows = {row["agent"]: row for row in body.get("agents") or []}
        check("预览 200", prev.status_code == 200, str(prev.status_code))
        check("★预览点名每个 agent 将写的字段",
              any(c["key"] == "model.provider" and c["to"] == "moa" for c in rows["alpha"]["changes"]),
              json.dumps(rows["alpha"]["changes"], ensure_ascii=False)[:200])
        check("★moa 的 base_url / key_env 变化也在预览里（alpha 原来有 key_env）",
              any(c["key"] == "model.key_env" and c["to"] == "" for c in rows["alpha"]["changes"]),
              json.dumps([c["key"] for c in rows["alpha"]["changes"]]))
        check("beta 没有 key_env 就不虚报 key_env 变更",
              all(c["key"] != "model.key_env" for c in rows["beta"]["changes"]),
              json.dumps([c["key"] for c in rows["beta"]["changes"]]))
        check("★预览没有改一个字节",
              {str(p): sha256_of(p) for p in (alpha_cfg, beta_cfg, sandbox / "config.yaml")} == before)

        bad = client.post(PREFIX + "/models/batch/preview", json={"agents": [], "provider": "moa",
                                                                  "model": "x"})
        check("空 agents ⇒ 400 invalid_argument", bad.status_code == 400
              and bad.json()["error"]["code"] == "invalid_argument", str(bad.status_code))
        bad2 = client.post(PREFIX + "/models/batch/preview",
                           json={"agents": ["alpha"], "provider": "no-such-provider", "model": "x"})
        check("不认识 provider ⇒ 400（不是 500）", bad2.status_code == 400,
              str(bad2.status_code))
        bad3 = client.post(PREFIX + "/models/batch/preview", json={"agents": ["ghost"], "provider": "moa",
                                                                   "model": "x"})
        check("不存在的 agent ⇒ 400", bad3.status_code == 400, str(bad3.status_code))
        check("★非法输入也没有改一个字节",
              {str(p): sha256_of(p) for p in (alpha_cfg, beta_cfg, sandbox / "config.yaml")} == before)

        # ---- [2] 缺 confirm --------------------------------------------------
        print("\n[2] 服务端第二道确认")
        noconfirm = client.post(PREFIX + "/models/batch/apply",
                                json={"agents": ["alpha"], "provider": "moa", "model": "星槎"})
        check("★批量写入缺 confirm ⇒ 400 confirmation_required",
              noconfirm.status_code == 400
              and noconfirm.json()["error"]["code"] == "confirmation_required",
              str(noconfirm.status_code))
        check("缺 confirm 也没写（含没做快照）",
              {str(p): sha256_of(p) for p in (alpha_cfg, beta_cfg)} ==
              {k: v for k, v in before.items() if k != str(sandbox / "config.yaml")})

        # ---- [3] 一条失败不影响别的 -----------------------------------------
        print("\n[3] 某个 agent 没有 config.yaml ⇒ 只这一条失败")
        applied = client.post(PREFIX + "/models/batch/apply",
                              json={"agents": ["alpha", "no-cfg", "beta"], "scope": "model",
                                    "provider": "moa", "model": "星槎", "confirm": True})
        ab = applied.json()
        by_agent = {item["agent"]: item for item in ab.get("results") or []}
        check("批量写入 200 且 ok=false（有失败条目）", applied.status_code == 200 and ab.get("ok") is False,
              json.dumps({"ok": ab.get("ok"), "ok_count": ab.get("ok_count"),
                          "failed": ab.get("failed_count")}))
        check("★alpha / beta 写成功", by_agent["alpha"]["ok"] and by_agent["beta"]["ok"],
              json.dumps({k: by_agent[k]["ok"] for k in by_agent}))
        check("★no-cfg 单独失败且错误码说清是快照问题",
              by_agent["no-cfg"]["ok"] is False and by_agent["no-cfg"]["code"] == "snapshot_failed",
              by_agent["no-cfg"]["code"] + " " + by_agent["no-cfg"]["error"][:80])
        check("★alpha 每条都有回读校验明细（4 项全过）",
              all(item["ok"] for item in by_agent["alpha"]["verify"]) and len(by_agent["alpha"]["verify"]) >= 3,
              json.dumps(by_agent["alpha"]["verify"], ensure_ascii=False))
        alpha_text = alpha_cfg.read_text(encoding="utf-8")
        check("★alpha 磁盘上 provider= moa 且 default=星槎",
              "provider: moa" in alpha_text and "default: 星槎" in alpha_text)
        check("★alpha 的 model.key_env 被删掉（虚拟 provider 不要 key）",
              "ALPHA_API_KEY" not in alpha_text.split("providers:")[0])
        check("★alpha 的 base_url 写成 moa://local", "base_url: moa://local" in alpha_text)
        check("★alpha 别的段没被动（fallback_providers / agent.max_turns 还在）",
              "alpha-backup" in alpha_text and "max_turns: 30" in alpha_text)
        check("★beta 也写成功", "provider: moa" in beta_cfg.read_text(encoding="utf-8"))
        check("root config.yaml 没被碰", sha256_of(sandbox / "config.yaml") == before[str(sandbox / "config.yaml")])
        batch_id = ab.get("batch_id")
        ledger = sandbox / "plugins" / "hermes-console" / "data" / "batches" / (str(batch_id) + ".json")
        check("★整批一份账本落盘", ledger.is_file(), str(ledger))

        # ---- [4] 一键回滚各自回原值 -----------------------------------------
        print("\n[4] 一键回滚：每个 agent 各自回到自己的原值")
        rb = client.post(PREFIX + "/models/batch/rollback", json={"batch_id": batch_id, "confirm": True})
        rbb = rb.json()
        check("回滚 200 且全成功", rb.status_code == 200 and rbb.get("ok") is True,
              json.dumps({"ok": rbb.get("ok"), "ok_count": rbb.get("ok_count")}))
        check("★alpha 回到 alpha-model-1（不是 beta 的值）",
              "alpha-model-1" in alpha_cfg.read_text(encoding="utf-8")
              and "beta-model-9" not in alpha_cfg.read_text(encoding="utf-8"))
        check("★beta 回到 beta-model-9（不是 alpha 的值）",
              "beta-model-9" in beta_cfg.read_text(encoding="utf-8")
              and "alpha-model-1" not in beta_cfg.read_text(encoding="utf-8"))
        check("★两份 config.yaml 都逐字节还原（含注释与别的段）",
              {str(alpha_cfg): sha256_of(alpha_cfg), str(beta_cfg): sha256_of(beta_cfg)} ==
              {str(alpha_cfg): before[str(alpha_cfg)], str(beta_cfg): before[str(beta_cfg)]})
        listed = client.get(PREFIX + "/models/batches").json()
        hit = [item for item in listed.get("batches") or [] if item.get("batch_id") == batch_id]
        check("★账本记下已回滚（可复核）", bool(hit) and bool((hit[0].get("rollback") or {}).get("at")),
              json.dumps(hit[0].get("rollback"), ensure_ascii=False)[:160] if hit else "missing")

        ghost = client.post(PREFIX + "/models/batch/rollback",
                            json={"batch_id": "19990101-000000-abcdef", "confirm": True})
        check("回滚不存在的 batch_id ⇒ 400 且带现有 id",
              ghost.status_code == 400 and "batches" in ghost.json()["error"],
              str(ghost.status_code))
        ghost2 = client.post(PREFIX + "/models/batch/rollback",
                             json={"batch_id": batch_id})
        check("回滚缺 confirm ⇒ 400 confirmation_required",
              ghost2.status_code == 400
              and ghost2.json()["error"]["code"] == "confirmation_required")

        # ---- [5] _write_official 的失败关闭 ---------------------------------
        print("\n[5] ★整篇覆盖的失败关闭（除白名单外任何缺键都必须拒绝）")
        target = sandbox / "profiles" / "alpha" / "config.yaml"
        full = api_models._read_config_doc(target)
        stripped = json.loads(json.dumps(full))
        stripped.pop("agent", None)     # 凭空少一段 —— 这次不是"我要删的"
        raised = ""
        try:
            api_models._write_official(target, stripped)
        except Exception as exc:  # noqa: BLE001 - 就是要它抛
            raised = type(exc).__name__ + ": " + str(exc)[:160]
        check("★省略了白名单外的键 ⇒ 写入被拒（不静默丢配置）", bool(raised), raised or "did not raise")
        check("★被拒后文件没变（失败关闭，不半截写）", sha256_of(target) == before[str(alpha_cfg)])

        allowed_ok = json.loads(json.dumps(full))
        allowed_ok["model"] = {k: v for k, v in allowed_ok["model"].items() if k != "key_env"}
        try:
            api_models._write_official(target, allowed_ok, allow_deletions=["model.key_env"])
            ok_write = True
            err = ""
        except Exception as exc:  # noqa: BLE001
            ok_write = False
            err = type(exc).__name__ + ": " + str(exc)[:160]
        check("★只删白名单里的 model.key_env ⇒ 允许（并且别的键都还在）",
              ok_write and "max_turns: 30" in target.read_text(encoding="utf-8"), err)

        # ---- [6] 单 agent 端点仍拒绝 targets --------------------------------
        print("\n[6] 单 agent 端点边界未变")
        single = client.post(PREFIX + "/models/apply",
                             json={"agent": "alpha", "targets": ["alpha", "beta"],
                                   "provider": "moa", "model": "星槎", "confirm": True})
        check("★/apply 收到 targets ⇒ 400 multi_agent_forbidden",
              single.status_code == 400
              and single.json()["error"]["code"] == "multi_agent_forbidden", str(single.status_code))
        moa_models = client.get(PREFIX + "/models/models",
                                params={"agent": "alpha", "provider": "moa"}).json()
        check("★moa 的模型列表来自该 agent 的 moa.presets（不是 registry）",
              moa_models.get("source") in ("moa-presets", "moa-presets-missing"),
              str(moa_models.get("source")) + " " + json.dumps(moa_models.get("models"), ensure_ascii=False))

        # ---- [7] 批量改「备用模型」段 --------------------------------------
        print("\n[7] 批量作用段=fallback（改 fallback_providers 首条）")
        before_fb = {str(p): sha256_of(p) for p in (alpha_cfg, beta_cfg)}
        fb_prev = client.post(PREFIX + "/models/batch/preview",
                              json={"agents": ["alpha"], "scope": "fallback",
                                    "provider": "moa", "model": "双璧"}).json()
        fb_changes = [c["key"] for c in (fb_prev.get("agents") or [{}])[0].get("changes") or []]
        check("★fallback 预览点名 fallback_providers[0].provider / .model",
              "fallback_providers[0].provider" in fb_changes
              and "fallback_providers[0].model" in fb_changes, json.dumps(fb_changes))
        fb_apply = client.post(PREFIX + "/models/batch/apply",
                               json={"agents": ["alpha"], "scope": "fallback",
                                     "provider": "moa", "model": "双璧", "confirm": True}).json()
        check("fallback 批量写入成功", fb_apply.get("ok") is True,
              json.dumps({"ok": fb_apply.get("ok"), "failed": fb_apply.get("failed_count")}))
        fb_text = alpha_cfg.read_text(encoding="utf-8")
        check("★fallback 首条变成 moa / 双璧", "model: 双璧" in fb_text and "provider: moa" in fb_text,
              fb_text.split("fallback_providers:")[-1][:120].replace("\n", " | "))
        check("★主模型没被 fallback 段顺手改掉（还是 alpha-model-1）",
              "default: alpha-model-1" in fb_text)
        fb_rb = client.post(PREFIX + "/models/batch/rollback",
                            json={"batch_id": fb_apply.get("batch_id"), "confirm": True}).json()
        check("fallback 批量回滚成功且逐字节还原",
              fb_rb.get("ok") is True
              and {str(p): sha256_of(p) for p in (alpha_cfg, beta_cfg)} == before_fb,
              json.dumps({"ok": fb_rb.get("ok")}))

        print("\nPASS=%d FAIL=%d TOTAL=%d" % (passed, len(failures), passed + len(failures)))
        if failures:
            print("MODELS-BATCH-API-FAILED")
            for item in failures:
                print("  - " + item)
            return 1
        print("MODELS-BATCH-API-OK")
        return 0
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

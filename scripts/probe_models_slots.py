# -*- coding: utf-8 -*-
"""模型分区「备用模型有序列表 + 辅助槽位可读写」后端契约探针（沙盒 home，零真机写入）。

覆盖卡片 t_5de506f4 的验收标准 A/B/C/D：

* **A 兼容性**：旧配置（1 条 fallback）读取不报错；旧 ``scope=fallback`` 仍是"写首条"；
  ``scopes`` 四段齐全；辅助槽位表按配置动态生成。
* **B 多备用模型**：整链写入 ≥3 条并回读一致 / 删中间一条其余顺序不变 / 调整顺序 /
  空数组清空 / ``fallback_models`` 与 ``ops`` 互斥 / 下标越界 400 / ``ops`` 保留未点到
  条目的额外键。
* **C 辅助槽位**：8 个槽位全部可读可写；写 ``approval`` 不动 ``compression``；
  写辅助槽位不动 ``model`` 与 ``fallback_providers``；``key_env`` 只读（400 + 无明文）；
  槽位不存在 400；不可写字段 400。
* **D 安全链路**：无 ``confirm`` ⇒ 400；预览零写入（逐字节）；预览值 = 实际写入值；
  回滚后逐字节恢复；未选中的 agent 逐字段未变；写相同值 ⇒ ``changed=false`` 且零写入。

跑法::

    <venv>/python.exe plugins/hermes-console/scripts/probe_models_slots.py
    echo "exit=$?"
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
MODELS = PREFIX + "/models"

sys.path.insert(0, str(HERMES_AGENT))
sys.path.insert(0, str(DASHBOARD))

#: 沙盒里塞进 provider 块的**明文哨兵** api_key —— 出现在响应里就是泄漏。
SANDBOX_API_KEY = "sk-sandbox-sentinel-9f3c1a2b4d5e6f70"
#: 沙盒 .env 里的哨兵密钥（`key_env` 指向它）。
SANDBOX_ENV_KEY = "SANDBOX_ENV_SENTINEL_TOKEN_5de506f4"
SANDBOX_ENV_NAME = "AUX_API_KEY"

#: 真实 profile 上实测的 8 个辅助槽位（沙盒照抄一份，用来证明"动态生成 8 个"）。
AUX_SLOTS = ("approval", "compression", "curator", "kanban_decomposer",
             "title_generation", "triage_specifier", "vision", "web_extract")

ROOT_CFG = """model:
  default: root-model
  provider: proot
providers:
  proot:
    base_url: https://root.example/v1
    models:
      root-model: {}
  paux:
    base_url: https://paux.example/v1
    key_env: AUX_API_KEY
    api_key: %s
    models:
      aux-model-1: {}
      aux-model-2: {}
fallback_providers:
  - provider: proot
    model: root-backup-1
  - provider: proot
    model: root-backup-2
    reasoning_effort: high
  - provider: paux
    model: root-backup-3
auxiliary:
  vision:
    provider: paux
    model: aux-model-1
    base_url: https://paux.example/v1
    key_env: AUX_API_KEY
  approval:
    provider: proot
    model: root-model
    reasoning_effort: none
  title_generation:
    provider: proot
    model: root-model
    timeout: 120
    reasoning_effort: none
  triage_specifier:
    provider: auto
    model: ''
  curator:
    provider: paux
    model: aux-model-1
  web_extract:
    provider: auto
    model: ''
  compression:
    timeout: 300
    provider: proot
    model: root-model
  kanban_decomposer:
    provider: paux
    model: aux-model-2
agent:
  max_turns: 30
""" % SANDBOX_API_KEY

#: 旧配置：**只有一条** fallback（兼容性断言用）。
ALPHA_CFG = """model:
  default: alpha-model-1
  provider: palpha
providers:
  palpha:
    base_url: https://alpha.example/v1
    key_env: ALPHA_API_KEY
    models:
      alpha-model-1: {}
      alpha-model-2: {}
fallback_providers:
  - provider: palpha
    model: alpha-backup
auxiliary:
  approval:
    provider: palpha
    model: alpha-model-1
  compression:
    provider: palpha
    model: alpha-model-1
agent:
  max_turns: 5
"""

#: 第二个 profile：**没有 auxiliary 段**，用来证明"别的 agent 一个字节没动"。
BETA_CFG = """model:
  default: beta-model-9
  provider: pbeta
providers:
  pbeta:
    base_url: https://beta.example/v1
    models:
      beta-model-9: {}
fallback_providers:
  - provider: pbeta
    model: beta-backup
"""

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


def brief(value, n: int = 220) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    return text if len(text) <= n else text[:n] + "…"


def err_code(resp) -> str:
    try:
        return str((resp.json().get("error") or {}).get("code"))
    except Exception:  # noqa: BLE001
        return "<unparsed>"


def build_home(root: Path) -> None:
    (root / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    (root / "profiles" / "beta").mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(ROOT_CFG, encoding="utf-8")
    (root / "profiles" / "alpha" / "config.yaml").write_text(ALPHA_CFG, encoding="utf-8")
    (root / "profiles" / "beta" / "config.yaml").write_text(BETA_CFG, encoding="utf-8")
    for name in ("alpha", "beta"):
        (root / "profiles" / name / "profile.yaml").write_text(
            "id: %s\ntitle: 沙盒%s\n" % (name, name), encoding="utf-8")
    (root / ".env").write_text("%s=%s\nALPHA_API_KEY=%s\n"
                               % (SANDBOX_ENV_NAME, SANDBOX_ENV_KEY, SANDBOX_ENV_KEY),
                               encoding="utf-8")


def load_module():
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_hermes_console", str(DASHBOARD / "plugin_api.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    sandbox = Path(tempfile.mkdtemp(prefix="models-slots-"))
    try:
        build_home(sandbox)
        os.environ["HERMES_HOME"] = str(sandbox)

        import yaml
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        module = load_module()
        app = FastAPI()
        app.include_router(module.router, prefix=PREFIX)
        client = TestClient(app, raise_server_exceptions=False)
        import api_models

        print("[env] sandbox :", sandbox)
        print("[env] routes  :", len(module.router.routes))
        check("没有域模块加载失败", module.DOMAIN_FAILURES == {},
              json.dumps(module.DOMAIN_FAILURES))

        root_cfg = sandbox / "config.yaml"
        alpha_cfg = sandbox / "profiles" / "alpha" / "config.yaml"
        beta_cfg = sandbox / "profiles" / "beta" / "config.yaml"

        def doc_of(path: Path) -> dict:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        def chain_of(path: Path) -> list:
            raw = doc_of(path).get("fallback_providers") or []
            return [{"provider": str(i.get("provider") or ""), "model": str(i.get("model") or "")}
                    for i in items_of(raw)]

        def items_of(raw):
            return [i for i in raw if isinstance(i, dict)]

        def overview(agent: str) -> dict:
            body = client.get(MODELS + "/overview", params={"agent": agent}).json()
            return (body.get("agents") or [{}])[0]

        def aux_of(path: Path, slot: str) -> dict:
            return ((doc_of(path).get("auxiliary") or {}).get(slot) or {})

        def apply(**body):
            return client.post(MODELS + "/apply", json=body)

        def slot_body(**body) -> dict:
            payload = {"agent": "default", "scope": "auxiliary", "confirm": True}
            payload.update(body)
            return payload

        # ==================================================================
        print("\n[A] 兼容性：旧配置读得动、旧 scope 语义不变、四段齐全")
        ov_alpha = overview("alpha")
        check("★旧配置（1 条 fallback）overview 不报错且返回 1 条",
              [f["model"] for f in ov_alpha.get("fallbacks") or []] == ["alpha-backup"],
              brief(ov_alpha.get("fallbacks")))
        check("★scopes 五段齐全（model / fallback / fallbacks / auxiliary / reasoning）",
              client.get(MODELS + "/overview").json().get("scopes")
              == ["model", "fallback", "fallbacks", "auxiliary", "reasoning"],
              str(client.get(MODELS + "/overview").json().get("scopes")))

        # ---- ops 只动该动的那一条：额外键必须原样保留（**最原始的链**第 2 条带 reasoning_effort）----
        pristine = chain_of(root_cfg)
        keep_extra = apply(agent="default", scope="fallbacks", confirm=True,
                           ops=[{"op": "append", "provider": "paux", "model": "aux-model-1"}])
        raw_chain = doc_of(root_cfg).get("fallback_providers") or []
        check("★ops 追加一条 200 且落到链尾（顺序不动）",
              keep_extra.status_code == 200
              and chain_of(root_cfg) == pristine + [{"provider": "paux", "model": "aux-model-1"}],
              brief(chain_of(root_cfg)))
        check("★ops 只动该动的那一条（未点到条目的 reasoning_effort 还在）",
              any(str(item.get("reasoning_effort") or "") == "high" for item in raw_chain),
              brief(raw_chain))

        # 旧 scope=fallback：仍是"写首条"（旧实现是 **prepend**：新值进首条，原有条目依次后移）
        old_chain_before = chain_of(root_cfg)
        old_fb = apply(agent="default", scope="fallback", provider="paux", model="aux-model-2",
                       confirm=True)
        old_body = old_fb.json()
        chain_after_old = chain_of(root_cfg)
        check("旧 scope=fallback 写入 200 且 verified", old_fb.status_code == 200
              and old_body.get("verified") is True, err_code(old_fb))
        check("★旧 scope=fallback 仍只改首条（新值进首条，原有条目顺序不变地后移）",
              chain_after_old == [{"provider": "paux", "model": "aux-model-2"}] + old_chain_before,
              brief(chain_after_old))
        check("★旧 scope=fallback 的 verify 只比 fallback.provider/.model 两键",
              [v["key"] for v in old_body.get("verify") or []]
              == ["fallback.provider", "fallback.model"],
              brief([v["key"] for v in old_body.get("verify") or []]))

        # 复位成一条（和 B 段要写的 4 条全不相同 ⇒ 预览必须逐条点名）
        apply(agent="default", scope="fallbacks", confirm=True,
              fallback_models=[{"provider": "paux", "model": "aux-model-2"}])

        # 动态槽位表
        slots = {row["slot"]: row for row in ov_alpha.get("auxiliary_slots") or []}
        ov_root = overview("default")
        root_slots = {row["slot"]: row for row in ov_root.get("auxiliary_slots") or []}
        check("★辅助槽位表按配置动态生成（default 8 个 / alpha 2 个，不是写死）",
              sorted(root_slots) == sorted(AUX_SLOTS) and sorted(slots) == ["approval", "compression"],
              "%d vs %d" % (len(root_slots), len(slots)))
        check("★key_env 不出现在任何槽位的 writable_fields 里",
              all("key_env" not in row["writable_fields"] for row in root_slots.values()),
              brief({k: v["writable_fields"] for k, v in root_slots.items()}))
        check("★各槽位 writable_fields 按实际 schema 走（vision 有 base_url、compression 有 timeout）",
              "base_url" in root_slots["vision"]["writable_fields"]
              and "timeout" in root_slots["compression"]["writable_fields"]
              and "timeout" not in root_slots["approval"]["writable_fields"],
              brief({k: v["writable_fields"] for k, v in root_slots.items()}))
        check("槽位表里 key_env 只给变量名（值不是哨兵密钥）",
              root_slots["vision"].get("key_env") == SANDBOX_ENV_NAME,
              str(root_slots["vision"].get("key_env")))

        # ==================================================================
        print("\n[B] 多备用模型：整链写入 / 删中间 / 调顺序 / 清空")
        four = [{"provider": "proot", "model": "root-backup-1"},
                {"provider": "paux", "model": "aux-model-1"},
                {"provider": "proot", "model": "root-backup-2"},
                {"provider": "paux", "model": "aux-model-2"}]
        # ---- 预览（纯读） ----
        before_bytes = sha256_of(root_cfg)
        prev = client.post(MODELS + "/batch/preview",
                           json={"agents": ["default"], "scope": "fallbacks",
                                 "fallback_models": four}).json()
        prev_row = (prev.get("agents") or [{}])[0]
        check("★fallbacks 预览 200 且点名 count 与逐条字段",
              prev.get("ok") is True
              and any(c["key"] == "fallback_providers.count" for c in prev_row.get("changes") or [])
              and any(c["key"] == "fallback_providers[3].model" for c in prev_row.get("changes") or []),
              brief(prev_row.get("changes")))
        check("★预览零写入（config.yaml 逐字节相同）", sha256_of(root_cfg) == before_bytes,
              sha256_of(root_cfg)[:16])

        # ---- 写入 4 条 ----
        w4 = apply(agent="default", scope="fallbacks", confirm=True, fallback_models=four)
        w4b = w4.json()
        check("★写入 4 条备用模型 200 / changed=true / verified",
              w4.status_code == 200 and w4b.get("changed") is True
              and w4b.get("verified") is True, err_code(w4))
        check("★磁盘回读 4 条且顺序与写入一致", chain_of(root_cfg) == four, brief(chain_of(root_cfg)))
        check("★回读校验逐条比对（count + 4 条 × 2 字段 = 9 项全 ok）",
              len(w4b.get("verify") or []) == 9
              and all(v["ok"] for v in w4b.get("verify") or []),
              brief([v["key"] for v in w4b.get("verify") or []]))
        preview_map = {c["key"]: c["to"] for c in prev_row.get("changes") or []}
        disk_now = chain_of(root_cfg)
        pairs_ok = True
        pairs_detail = {}
        for index in range(len(four)):
            for field in ("provider", "model"):
                key = "fallback_providers[%d].%s" % (index, field)
                got = disk_now[index][field] if index < len(disk_now) else None
                pairs_detail[key] = {"preview": preview_map.get(key), "disk": got}
                if preview_map.get(key) != got:
                    pairs_ok = False
        check("★预览值 = 实际写入值（逐条一致）", pairs_ok, brief(pairs_detail))

        # ---- 删中间一条 ----
        rm = apply(agent="default", scope="fallbacks", confirm=True,
                   ops=[{"op": "remove", "index": 1}])
        check("★删除中间第 2 条 200", rm.status_code == 200 and rm.json().get("verified") is True,
              err_code(rm))
        check("★删中间一条后其余顺序保持",
              chain_of(root_cfg) == [four[0], four[2], four[3]], brief(chain_of(root_cfg)))

        # ---- 调整顺序 ----
        mv = apply(agent="default", scope="fallbacks", confirm=True,
                   ops=[{"op": "move", "from": 2, "to": 0}])
        check("★调整顺序 200", mv.status_code == 200 and mv.json().get("verified") is True,
              err_code(mv))
        check("★回读顺序与写入一致（第 3 条挪到首位）",
              chain_of(root_cfg) == [four[3], four[0], four[2]], brief(chain_of(root_cfg)))

        # ---- 清空 ----
        clear = apply(agent="default", scope="fallbacks", confirm=True, fallback_models=[])
        check("★空数组可写（清空备用链）", clear.status_code == 200
              and clear.json().get("verified") is True, err_code(clear))
        check("★清空后磁盘上是空列表且 count=0",
              doc_of(root_cfg).get("fallback_providers") == []
              and overview("default").get("fallback_count") == 0,
              brief(doc_of(root_cfg).get("fallback_providers")))

        # ---- 参数边界 ----
        both = apply(agent="default", scope="fallbacks", confirm=True,
                     fallback_models=four, ops=[{"op": "clear"}])
        check("★fallback_models 与 ops 同时给 ⇒ 400 invalid_argument",
              both.status_code == 400 and err_code(both) == "invalid_argument", err_code(both))
        oob = apply(agent="default", scope="fallbacks", confirm=True,
                    ops=[{"op": "move", "from": 3, "to": 0}])
        check("★ops 下标越界 ⇒ 400（不是 500）",
              oob.status_code == 400 and err_code(oob) == "invalid_argument", err_code(oob))
        empty_req = apply(agent="default", scope="fallbacks", confirm=True)
        check("既不给 fallback_models 也不给 ops ⇒ 400",
              empty_req.status_code == 400, err_code(empty_req))
        # 复位成 3 条
        apply(agent="default", scope="fallbacks", confirm=True,
              fallback_models=[{"provider": "proot", "model": "root-backup-1"},
                               {"provider": "proot", "model": "root-backup-2"},
                               {"provider": "paux", "model": "root-backup-3"}])

        # ==================================================================
        print("\n[C] 辅助槽位：8 个全可写 / 逐槽位隔离 / 逐段隔离 / key_env 只读")
        model_before = doc_of(root_cfg).get("model")
        chain_before = doc_of(root_cfg).get("fallback_providers")
        compression_before = aux_of(root_cfg, "compression")
        approval_before = aux_of(root_cfg, "approval")

        # ---- 只写 approval：逐槽位隔离 / 逐段隔离 ----
        only_approval = apply(**slot_body(slot="approval", provider="paux", model="aux-model-2"))
        check("★只写 approval 200 且 verified", only_approval.status_code == 200
              and only_approval.json().get("verified") is True, err_code(only_approval))
        check("★写 approval 不影响 compression（逐槽位隔离）",
              aux_of(root_cfg, "compression") == compression_before,
              brief(aux_of(root_cfg, "compression")))
        check("★写 approval 不影响 vision / curator / title_generation 等别的槽位",
              aux_of(root_cfg, "vision").get("model") == "aux-model-1"
              and aux_of(root_cfg, "curator").get("model") == "aux-model-1"
              and aux_of(root_cfg, "title_generation").get("timeout") == 120,
              brief({slot: aux_of(root_cfg, slot) for slot in
                     ("vision", "curator", "title_generation")}))
        check("★写辅助槽位不影响 model 段", doc_of(root_cfg).get("model") == model_before,
              brief(doc_of(root_cfg).get("model")))
        check("★写辅助槽位不影响 fallback_providers 段",
              doc_of(root_cfg).get("fallback_providers") == chain_before,
              brief(doc_of(root_cfg).get("fallback_providers")))
        check("★approval 的 reasoning_effort 没被顺手改掉",
              aux_of(root_cfg, "approval").get("reasoning_effort")
              == approval_before.get("reasoning_effort"),
              brief(aux_of(root_cfg, "approval")))

        # ---- 8 个槽位逐个写 ----
        written = {}
        for slot in AUX_SLOTS:
            body = slot_body(slot=slot, provider="paux", model="aux-model-2")
            resp = apply(**body)
            written[slot] = resp.status_code
        check("★8 个槽位全部可写（每个都 200）", all(code == 200 for code in written.values()),
              brief(written))
        disk_slots = doc_of(root_cfg).get("auxiliary") or {}
        check("★8 个槽位回读 provider/model 全部一致",
              all(str((disk_slots.get(slot) or {}).get("provider")) == "paux"
                  and str((disk_slots.get(slot) or {}).get("model")) == "aux-model-2"
                  for slot in AUX_SLOTS),
              brief({slot: (disk_slots.get(slot) or {}) for slot in AUX_SLOTS}))
        check("★8 个槽位写完后 key_env 只读指针没被写掉（vision.key_env 还在）",
              aux_of(root_cfg, "vision").get("key_env") == SANDBOX_ENV_NAME,
              brief(aux_of(root_cfg, "vision")))

        # 可选字段
        opt = apply(**slot_body(slot="title_generation", fields={"reasoning_effort": "high",
                                                                "timeout": "600"}))
        check("★可选字段可写（reasoning_effort + timeout）", opt.status_code == 200
              and opt.json().get("verified") is True, err_code(opt))
        title_item = aux_of(root_cfg, "title_generation")
        check("★timeout 写成整数 600（不是字符串）且 reasoning_effort 落地",
              title_item.get("timeout") == 600 and title_item.get("reasoning_effort") == "high",
              brief(title_item))
        check("★可写字段的 verify 逐键比对 auxiliary.<slot>.<field>",
              sorted(v["key"] for v in opt.json().get("verify") or [])
              == ["auxiliary.title_generation.reasoning_effort",
                  "auxiliary.title_generation.timeout"],
              brief([v["key"] for v in opt.json().get("verify") or []]))
        check("★写 title_generation 不动 approval",
              aux_of(root_cfg, "approval").get("model") == "aux-model-2",
              brief(aux_of(root_cfg, "approval")))

        # key_env 只读
        guard = apply(**slot_body(slot="vision", fields={"key_env": "SOME_OTHER_ENV"}))
        check("★写 key_env ⇒ 400 read_only_field",
              guard.status_code == 400 and err_code(guard) == "read_only_field", err_code(guard))
        check("★被拒后 vision.key_env 一个字节没变",
              aux_of(root_cfg, "vision").get("key_env") == SANDBOX_ENV_NAME,
              brief(aux_of(root_cfg, "vision")))
        check("★响应里不含沙盒明文哨兵密钥",
              SANDBOX_API_KEY not in guard.text and SANDBOX_ENV_KEY not in guard.text,
              "命中" if (SANDBOX_API_KEY in guard.text) else "干净")

        # 不可写字段 / 不存在的槽位
        bad_field = apply(**slot_body(slot="curator", fields={"timeout": 60}))
        check("★写该槽位没有的可选字段 ⇒ 400（可写字段列表随响应给）",
              bad_field.status_code == 400 and "writable_fields" in json.dumps(bad_field.json()),
              err_code(bad_field))
        ghost_slot = apply(**slot_body(slot="no_such_slot", provider="paux", model="x"))
        check("★不存在的槽位 ⇒ 400 not_found（带现有槽位列表）",
              ghost_slot.status_code == 400 and err_code(ghost_slot) == "not_found",
              err_code(ghost_slot))
        no_slot = apply(agent="default", scope="auxiliary", confirm=True, provider="paux")
        check("scope=auxiliary 缺 slot ⇒ 400", no_slot.status_code == 400, err_code(no_slot))
        bad_prov = apply(**slot_body(slot="approval", provider="no-such-provider"))
        check("★辅助槽位写不认识的 provider ⇒ 400", bad_prov.status_code == 400, err_code(bad_prov))
        auto_ok = apply(**slot_body(slot="triage_specifier", provider="auto", model=""))
        check("★auto 槽位可写（provider=auto / model 空）", auto_ok.status_code == 200
              and auto_ok.json().get("verified") is True, err_code(auto_ok))
        check("★auto 槽位回读 provider=auto、model 空",
              aux_of(root_cfg, "triage_specifier").get("provider") == "auto"
              and str(aux_of(root_cfg, "triage_specifier").get("model")) == "",
              brief(aux_of(root_cfg, "triage_specifier")))

        # 批量：两个 agent 各写自己的备用链（scope=fallbacks）
        fb_batch_prev = client.post(MODELS + "/batch/preview",
                                    json={"agents": ["alpha", "beta"], "scope": "fallbacks",
                                          "fallback_models": four}).json()
        check("★批量预览（scope=fallbacks）逐 agent 点名 fallback_providers.*",
              all(any(c["key"] == "fallback_providers.count"
                      for c in row.get("changes") or [])
                  for row in fb_batch_prev.get("agents") or []),
              brief([(row.get("agent"), row.get("error")) for row in fb_batch_prev.get("agents") or []]))
        alpha_before_fb = sha256_of(alpha_cfg)
        fb_batch = client.post(MODELS + "/batch/apply",
                               json={"agents": ["alpha", "beta"], "scope": "fallbacks",
                                     "fallback_models": four, "confirm": True}).json()
        check("★批量写备用链两 agent 都成功", fb_batch.get("ok") is True,
              brief({"ok": fb_batch.get("ok"), "failed": fb_batch.get("failed_count")}))
        check("★alpha / beta 的备用链都变成同一份 4 条",
              chain_of(alpha_cfg) == four and chain_of(beta_cfg) == four,
              brief({"alpha": chain_of(alpha_cfg), "beta": chain_of(beta_cfg)}))
        fb_rb = client.post(MODELS + "/batch/rollback",
                            json={"batch_id": fb_batch.get("batch_id"), "confirm": True}).json()
        check("★批量回滚备用链后 alpha 逐字节恢复",
              fb_rb.get("ok") is True and sha256_of(alpha_cfg) == alpha_before_fb,
              brief({"ok": fb_rb.get("ok")}))

        # 批量：两个 agent 各写自己的 approval
        # （provider 用 ``auto``：alpha 自己的 config.yaml 没声明 paux，写 paux 会被按
        #   agent 拒掉 —— 这是**有意的**严格度，见报告"未验证项/契约说明"）
        batch_prev = client.post(MODELS + "/batch/preview",
                                 json={"agents": ["default", "alpha"], "scope": "auxiliary",
                                       "slot": "approval", "provider": "auto",
                                       "model": "batch-aux-model"}).json()
        check("★批量预览（scope=auxiliary）点名 auxiliary.approval.*",
              all(any(c["key"] == "auxiliary.approval.provider"
                      for c in row.get("changes") or [])
                  for row in batch_prev.get("agents") or []),
              brief([(row.get("agent"), row.get("error")) for row in batch_prev.get("agents") or []]))
        beta_before = sha256_of(beta_cfg)
        batch_apply = client.post(MODELS + "/batch/apply",
                                  json={"agents": ["default", "alpha"], "scope": "auxiliary",
                                        "slot": "approval", "provider": "auto",
                                        "model": "batch-aux-model", "confirm": True}).json()
        check("★批量写辅助槽位两 agent 都成功", batch_apply.get("ok") is True,
              brief({"ok": batch_apply.get("ok"), "failed": batch_apply.get("failed_count")}))
        check("★alpha 的 approval 变成 auto/batch-aux-model（各 agent 各写自己的）",
              aux_of(alpha_cfg, "approval").get("provider") == "auto"
              and aux_of(alpha_cfg, "approval").get("model") == "batch-aux-model",
              brief(aux_of(alpha_cfg, "approval")))
        check("★未选中的 beta 一个字节没动", sha256_of(beta_cfg) == beta_before,
              sha256_of(beta_cfg)[:16])
        rb_batch = client.post(MODELS + "/batch/rollback",
                               json={"batch_id": batch_apply.get("batch_id"), "confirm": True}).json()
        check("批量回滚成功", rb_batch.get("ok") is True, brief(rb_batch.get("ok_count")))

        # ==================================================================
        print("\n[D] 安全链路：confirm / 零写入 / 预览=写入 / 逐字节回滚 / 未选中不变")
        alpha_bytes = sha256_of(alpha_cfg)
        no_confirm_fb = client.post(MODELS + "/apply",
                                    json={"agent": "alpha", "scope": "fallbacks",
                                          "fallback_models": four})
        check("★scope=fallbacks 无 confirm ⇒ 400 confirmation_required",
              no_confirm_fb.status_code == 400
              and err_code(no_confirm_fb) == "confirmation_required", err_code(no_confirm_fb))
        no_confirm_aux = client.post(MODELS + "/apply",
                                     json={"agent": "alpha", "scope": "auxiliary",
                                           "slot": "approval", "provider": "paux",
                                           "model": "aux-model-2"})
        check("★scope=auxiliary 无 confirm ⇒ 400 confirmation_required",
              no_confirm_aux.status_code == 400
              and err_code(no_confirm_aux) == "confirmation_required", err_code(no_confirm_aux))
        check("★缺 confirm 也没写一个字节", sha256_of(alpha_cfg) == alpha_bytes,
              sha256_of(alpha_cfg)[:16])

        # 预览值 = 实际写入值（alpha：整链）
        prev_alpha = client.post(MODELS + "/batch/preview",
                                 json={"agents": ["alpha"], "scope": "fallbacks",
                                       "fallback_models": four}).json()
        row_alpha = (prev_alpha.get("agents") or [{}])[0]
        preview_map = {c["key"]: c["to"] for c in row_alpha.get("changes") or []}
        applied_alpha = client.post(MODELS + "/batch/apply",
                                    json={"agents": ["alpha"], "scope": "fallbacks",
                                          "fallback_models": four, "confirm": True}).json()
        disk_alpha = chain_of(alpha_cfg)
        pairs_ok = True
        pairs_detail = {}
        for index, item in enumerate(four):
            for field in ("provider", "model"):
                key = "fallback_providers[%d].%s" % (index, field)
                want = preview_map.get(key)
                got = disk_alpha[index][field] if index < len(disk_alpha) else None
                pairs_detail[key] = {"preview": want, "disk": got}
                if want != got:
                    pairs_ok = False
        check("★预览值 = 实际写入值（逐项一致）", pairs_ok, brief(pairs_detail))
        check("★批量写入后逐条 verify 全 ok",
              all(item.get("ok") for item in (applied_alpha.get("results") or [{}])[0]
                  .get("verify") or []),
              brief((applied_alpha.get("results") or [{}])[0].get("verify")))

        # 回滚逐字节
        rb = client.post(MODELS + "/rollback",
                         json={"agent": "alpha",
                               "snapshot": (applied_alpha.get("results") or [{}])[0]
                               .get("snapshot_path"), "confirm": True})
        check("★回滚后 config.yaml 逐字节恢复", rb.status_code == 200
              and sha256_of(alpha_cfg) == alpha_bytes, err_code(rb))

        # 写相同值 ⇒ changed=false 且零写入
        before_same = sha256_of(root_cfg)
        same = apply(agent="default", scope="auxiliary", slot="approval",
                     provider="paux", model="aux-model-2", confirm=True)
        check("★写相同值 ⇒ changed=false 且不落盘（逐字节相同）",
              same.status_code == 200 and same.json().get("changed") is False
              and sha256_of(root_cfg) == before_same,
              brief({"changed": same.json().get("changed")}))
        same_fb = apply(agent="default", scope="fallbacks", confirm=True,
                        fallback_models=[{"provider": "proot", "model": "root-backup-1"},
                                         {"provider": "proot", "model": "root-backup-2"},
                                         {"provider": "paux", "model": "root-backup-3"}])
        check("★整链写相同值也走 changed=false（不建快照）",
              same_fb.status_code == 200 and same_fb.json().get("changed") is False
              and same_fb.json().get("snapshot_path") == "",
              brief({"changed": same_fb.json().get("changed"),
                     "snapshot": same_fb.json().get("snapshot_path")}))

        # 未选中的 agent 逐字段未变（整轮跑完后的最终断言）
        check("★未选中的 beta 逐字段未变",
              doc_of(beta_cfg).get("model") == {"default": "beta-model-9", "provider": "pbeta"}
              and doc_of(beta_cfg).get("fallback_providers")
              == [{"provider": "pbeta", "model": "beta-backup"}]
              and "auxiliary" not in doc_of(beta_cfg),
              brief(doc_of(beta_cfg)))

        # ==================================================================
        print("\n[E] _checks_for 支持任意下标 / 整链 / 辅助槽位键")
        state = {
            "provider": "", "model": "", "base_url": "", "api_mode": "", "key_env": "",
            "fallbacks": [{"provider": "a", "model": "m0"},
                          {"provider": "b", "model": "m1"},
                          {"provider": "c", "model": "m2"}],
            "auxiliary": {"approval": {"provider": "auto", "model": ""}},
        }
        chain_json = json.dumps(state["fallbacks"], ensure_ascii=False, sort_keys=True)
        checks = api_models._checks_for(state, {
            "fallbacks[2].model": "m2",
            "fallbacks[1].provider": "b",
            "fallbacks.count": "3",
            "fallbacks.chain": chain_json,
            "auxiliary.approval.provider": "auto",
            "auxiliary.approval.model": "",
        })
        check("★任意下标 / 整链 / 辅助槽位键 6 项全部比对通过",
              len(checks) == 6 and all(item["ok"] for item in checks), brief(checks))
        bad = api_models._checks_for(state, {"fallbacks[9].model": "x",
                                            "auxiliary.approval.provider": "wrong"})
        check("★越界下标与错值都不通过（不静默 ok）",
              [item["ok"] for item in bad] == [False, False], brief(bad))
        unknown = api_models._checks_for(state, {"nope.key": "x"})
        check("不认得的键 ⇒ 不通过", unknown[0]["ok"] is False, brief(unknown))

        print("\nPASS=%d FAIL=%d TOTAL=%d" % (passed, len(failures), passed + len(failures)))
        if failures:
            print("MODELS-SLOTS-FAILED")
            for item in failures:
                print("  - " + item)
            return 1
        print("MODELS-SLOTS-OK")
        return 0
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

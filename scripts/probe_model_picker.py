# -*- coding: utf-8 -*-
"""hermes-console · 模型配置统一化验收探针（t_4d654faf）—— **后端半边**。

跑法（**不要经管道拿退出码**）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        plugins/hermes-console/scripts/probe_model_picker.py
    echo "exit=$?"

覆盖验收标准里的后端部分::

    [1] /models/usage 的模块盘点：main + 宿主全部 aux 槽位，来源层三态，
        web_extract 标 active=false 并写明原因（宿主已忽略）
    [2] ★3 个 agent × 3 个 provider：换 provider 模型清单**确实不同**（集合差集非空）
    [3] ★非法 provider/model 组合被拒：/models/validate 与 scope=auxiliary 都 400，
        消息里点名 provider 与候选模型；**配置一个字节都没变**（sha256）
    [4] ★换 provider 却留着旧模型 ⇒ 400（两级联动的服务端兜底）
    [5] ★Agent 独立配置 vs 跟随全局：两种都写、都回读；来源层随之为 agent / global
    [6] ★按 agent 隔离：写 alpha 的 approval，beta/gamma 的 config.yaml sha256 不变
    [7] 思考强度取值来自宿主真值（与 hermes_constants.VALID_REASONING_EFFORTS 逐条相同）
    [8] 真实 home 只读：3 个真实 agent 的 /usage 200、无密钥取值泄漏
    [9] allow_unlisted_model=true 是**显式**逃生门：放行但响应 notes 记「未校验」

硬约束：所有写操作只在沙盒 home 里（子进程），真实 home 只发 GET。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
REAL_HOME = Path(__file__).resolve().parents[3]
HERMES_AGENT = REAL_HOME / "hermes-agent"
PREFIX = "/api/plugins/hermes-console"
MODELS = PREFIX + "/models"

AGENTS = ("alpha", "beta", "gamma")
PROVIDERS = ("palpha", "pbeta", "pgamma")
SANDBOX_KEY = "sbx-secret-value-should-never-appear-0123456789"

failures: list[str] = []
passed = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global passed
    print(("  PASS  " if ok else "  FAIL  ") + label + (" — " + detail if detail else ""))
    if ok:
        passed += 1
    else:
        failures.append(label)
    return ok


def sha256_of(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:  # noqa: BLE001
        return "<error:%s>" % exc


def brief(value, n: int = 200) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    return text if len(text) <= n else text[:n] + "…"


def redacted(resp) -> str:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = None
    code = "<none>"
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        code = str(body["error"].get("code"))
    return "status=%s error.code=%s" % (resp.status_code, code)


def load_plugin_api():
    for extra in (str(DASHBOARD), str(HERMES_AGENT)):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_hermes_console", DASHBOARD / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_client(module):
    from fastapi import FastAPI  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402

    app = FastAPI()
    app.include_router(module.router, prefix=PREFIX)
    return TestClient(app, raise_server_exceptions=False)


# ===========================================================================
# 沙盒 home
# ===========================================================================
def _agent_config(name: str) -> str:
    """每个 agent：三个 provider、模型清单各不相同、auxiliary 各不相同。"""
    lines = [
        "model:",
        "  default: %s-main" % name,
        "  provider: p%s" % name,
        "providers:",
    ]
    for provider in PROVIDERS:
        lines.append("  %s:" % provider)
        lines.append("    base_url: https://%s.example/v1" % provider)
        lines.append("    models:")
        # ★ 每个 provider 的模型清单**故意不同**：
        #   p<name> 有 main/alt；其它 provider 只有 main/alt + 自己独占的那个，
        #   而 pgamma 只有它独占的那个 —— 这样"换 provider 但旧模型不在新清单里"
        #   才有可测的用例（[4]）。
        if provider == "p%s" % name:
            names = ("%s-main" % name, "%s-alt" % name, "%s-%s-only" % (name, provider))
        elif provider == "pgamma":
            names = ("%s-pgamma-only" % name,)
        else:
            names = ("%s-main" % name, "%s-alt" % name, "%s-%s-only" % (name, provider))
        for model in names:
            lines.append("      %s: {}" % model)
    lines += [
        "auxiliary:",
        "  approval:",
        "    provider: p%s" % name,
        "    model: %s-main" % name,
        "  title_generation:",
        "    provider: auto",
        "    model: ''",
        "  compression:",
        "    provider: p%s" % name,
        "    model: %s-alt" % name,
    ]
    return "\n".join(lines) + "\n"


def write_sandbox(sandbox: Path) -> None:
    shutil.rmtree(sandbox, ignore_errors=True)
    (sandbox / "profiles").mkdir(parents=True, exist_ok=True)
    (sandbox / "config.yaml").write_text(_agent_config("default"), encoding="utf-8")
    (sandbox / ".env").write_text("SANDBOX_PICKER_KEY=%s\n" % SANDBOX_KEY, encoding="utf-8")
    for name in AGENTS:
        profile = sandbox / "profiles" / name
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "profile.yaml").write_text("title: 沙盒%s\n" % name, encoding="utf-8")
        (profile / "config.yaml").write_text(_agent_config(name), encoding="utf-8")


def sandbox_phase(sandbox: Path) -> int:
    write_sandbox(sandbox)
    os.environ["HERMES_HOME"] = str(sandbox)

    module = load_plugin_api()
    import api_models  # noqa: E402

    api_models._home = lambda: sandbox
    client = make_client(module)

    def cfg_path(agent: str) -> Path:
        return sandbox / "profiles" / agent / "config.yaml"

    def disk_aux(agent: str) -> dict:
        """独立回读（不经被测代码）：yaml → auxiliary 段。"""
        import yaml

        data = yaml.safe_load(cfg_path(agent).read_text(encoding="utf-8")) or {}
        block = data.get("auxiliary") or {}
        return {key: dict(value) for key, value in block.items() if isinstance(value, dict)}

    # ---- [1] 模块盘点 ------------------------------------------------------
    print("\n[1] /models/usage 模块盘点：槽位来自宿主常量，来源层三态")
    usage = client.get(MODELS + "/usage", params={"agent": "alpha"})
    body = usage.json() if usage.status_code == 200 else {}
    rows = body.get("modules") or []
    ids = [row.get("id") for row in rows]
    print("        count=%s ids=%s" % (body.get("count"), ids))
    check("/models/usage 200", usage.status_code == 200, redacted(usage))
    check("含 main 行 + 宿主全部 aux 槽位（≥12 行）", len(rows) >= 13, "rows=%d" % len(rows))
    check("槽位顺序来自宿主 _AUX_TASK_SLOTS（前 3 个是 vision/compression/skills_hub）",
          [row.get("slot") for row in rows[1:4]] == ["vision", "compression", "skills_hub"],
          str([row.get("slot") for row in rows[1:4]]))
    by_slot = {row.get("slot"): row for row in rows if row.get("kind") == "auxiliary"}
    check("approval 行 source=agent（config.yaml 显式声明且 provider 不是 auto）",
          by_slot.get("approval", {}).get("source") == "agent",
          brief(by_slot.get("approval", {}).get("source")))
    check("title_generation 行 source=global（provider=auto ⇒ 跟随主模型）",
          by_slot.get("title_generation", {}).get("source") == "global",
          brief(by_slot.get("title_generation", {}).get("source")))
    check("title_generation 的 resolved_provider = 主模型 provider（跟随全局解析出来了）",
          by_slot.get("title_generation", {}).get("resolved_provider") == "palpha",
          brief(by_slot.get("title_generation", {}).get("resolved_provider")))
    vision_row = by_slot.get("vision", {})
    check("vision 未在 alpha 的 config 里声明 ⇒ source=module_default",
          vision_row.get("source") == "module_default", brief(vision_row.get("source")))
    check("每个 aux 行都给 picker 契约（providers/models/apply scope+slot）",
          all((row.get("picker") or {}).get("apply_scope") == "auxiliary"
              and (row.get("picker") or {}).get("apply_slot") == row.get("slot")
              for row in rows if row.get("kind") == "auxiliary"))
    we = by_slot.get("web_extract", {})
    if we:
        check("web_extract 标 active=false 且写明「宿主已忽略」",
              we.get("active") is False and "忽略" in str(we.get("inactive_reason")),
              brief(we.get("inactive_reason")))
    else:
        check("web_extract 未出现在盘点里（该 agent 没声明、宿主也没这个槽位）", True, "skipped")
    check("usage 响应里没有密钥取值", SANDBOX_KEY not in usage.text)

    # ---- [2] 3 agent × 3 provider：模型清单确实不同 -------------------------
    print("\n[2] ★3 个 agent × 3 个 provider：换 provider 模型清单确实变化")
    listing = {}
    for agent in AGENTS:
        for provider in PROVIDERS:
            resp = client.get(MODELS + "/models", params={"agent": agent, "provider": provider})
            data = resp.json() if resp.status_code == 200 else {}
            listing[(agent, provider)] = set(data.get("models") or [])
            print("        %-6s / %-7s -> %s %s" % (agent, provider, resp.status_code,
                                                   sorted(data.get("models") or [])))
            check("%s/%s 的模型清单 200 且非空" % (agent, provider),
                  resp.status_code == 200 and bool(data.get("models")), redacted(resp))
    pairs_ok = 0
    total_pairs = 0
    for agent in AGENTS:
        for left in PROVIDERS:
            for right in PROVIDERS:
                if left >= right:
                    continue
                total_pairs += 1
                diff = listing[(agent, left)] ^ listing[(agent, right)]
                if diff:
                    pairs_ok += 1
    check("★同一 agent 内两两 provider 的模型集合都不相同（差集非空）",
          pairs_ok == total_pairs, "%d/%d 对" % (pairs_ok, total_pairs))
    check("★同一 provider 在不同 agent 下模型清单不同（清单是按 agent 取的）",
          len({tuple(sorted(listing[(agent, 'palpha')])) for agent in AGENTS}) == len(AGENTS),
          str([sorted(listing[(agent, 'palpha')]) for agent in AGENTS]))

    # ---- [3] 非法组合被拒 --------------------------------------------------
    print("\n[3] ★非法 provider/model 组合：/models/validate 与 scope=auxiliary 都 400")
    bad = client.get(MODELS + "/validate",
                     params={"agent": "alpha", "provider": "palpha", "model": "beta-main"})
    bad_body = bad.json() if bad.status_code != 200 else {}
    print("        validate(palpha, beta-main) ->", bad.status_code,
          brief((bad_body.get("error") or {}).get("message"), 120))
    check("★validate 非法组合 400", bad.status_code == 400, redacted(bad))
    message = str((bad_body.get("error") or {}).get("message") or "")
    check("报错可读：点名 provider 与候选模型",
          "palpha" in message and "alpha-main" in message, message[:160])
    check("报错给出逃生门（allow_unlisted_model）", "allow_unlisted_model" in message)
    good = client.get(MODELS + "/validate",
                      params={"agent": "alpha", "provider": "palpha", "model": "alpha-alt"})
    check("合法组合 validate 200", good.status_code == 200, redacted(good))
    unknown = client.get(MODELS + "/validate",
                         params={"agent": "alpha", "provider": "nope", "model": "x"})
    check("不认识的 provider ⇒ 400（不是 500）", unknown.status_code == 400, redacted(unknown))

    before = sha256_of(cfg_path("alpha"))
    write_bad = client.post(MODELS + "/apply", json={
        "agent": "alpha", "scope": "auxiliary", "slot": "approval",
        "provider": "palpha", "model": "beta-main", "confirm": True})
    print("        apply(aux approval, palpha/beta-main) ->", write_bad.status_code)
    check("★scope=auxiliary 非法 model ⇒ 400", write_bad.status_code == 400, redacted(write_bad))
    check("★被拒后 config.yaml 一个字节都没变", sha256_of(cfg_path("alpha")) == before)

    # ---- [4] 换 provider 却留着旧模型 --------------------------------------
    print("\n[4] ★换 provider 但旧模型不属于新 provider ⇒ 400（两级联动兜底）")
    stale = client.post(MODELS + "/apply", json={
        "agent": "alpha", "scope": "auxiliary", "slot": "approval",
        "provider": "pgamma", "confirm": True})
    print("        apply(aux approval, provider=pgamma 且不带 model) ->", stale.status_code)
    check("★只换 provider 且旧模型不属于新 provider ⇒ 400", stale.status_code == 400,
          redacted(stale))
    check("被拒后 config.yaml 仍然没变", sha256_of(cfg_path("alpha")) == before)

    # ---- [5] 两种来源层：独立配置 / 跟随全局 --------------------------------
    print("\n[5] ★Agent 独立配置 vs 跟随全局：都能写、都能回读")
    ok_write = client.post(MODELS + "/apply", json={
        "agent": "alpha", "scope": "auxiliary", "slot": "approval",
        "provider": "pgamma", "model": "alpha-pgamma-only", "confirm": True})
    print("        apply(aux approval, pgamma/alpha-pgamma-only) ->", ok_write.status_code)
    check("★合法组合写入 200", ok_write.status_code == 200, redacted(ok_write))
    disk = disk_aux("alpha").get("approval") or {}
    check("★回读盘上真值 = pgamma / alpha-pgamma-only",
          (disk.get("provider"), disk.get("model")) == ("pgamma", "alpha-pgamma-only"), brief(disk))
    row = [item for item in client.get(MODELS + "/usage", params={"agent": "alpha"}).json()
           .get("modules") or [] if item.get("slot") == "approval"][0]
    check("写完后 usage 的 approval.source 变成 agent（该 Agent 独立配置）",
          row.get("source") == "agent" and row.get("provider") == "pgamma", brief(row.get("source")))

    follow = client.post(MODELS + "/apply", json={
        "agent": "alpha", "scope": "auxiliary", "slot": "approval",
        "provider": "auto", "model": "", "confirm": True})
    print("        apply(aux approval, provider=auto, model='') ->", follow.status_code)
    check("★「跟随全局」写入 200", follow.status_code == 200, redacted(follow))
    disk = disk_aux("alpha").get("approval") or {}
    check("★回读 = auto / ''（跟随全局）",
          (disk.get("provider"), disk.get("model")) == ("auto", ""), brief(disk))
    row = [item for item in client.get(MODELS + "/usage", params={"agent": "alpha"}).json()
           .get("modules") or [] if item.get("slot") == "approval"][0]
    check("★usage 的 approval.source 变成 global，resolved 落到主模型",
          row.get("source") == "global" and row.get("resolved_provider") == "palpha"
          and row.get("resolved_model") == "alpha-main",
          brief({k: row.get(k) for k in ("source", "resolved_provider", "resolved_model")}))

    # ---- [6] 按 agent 隔离 -------------------------------------------------
    print("\n[6] ★按 agent 隔离：写 alpha，beta / gamma 的配置一个字节不变")
    before_beta = sha256_of(cfg_path("beta"))
    before_gamma = sha256_of(cfg_path("gamma"))
    client.post(MODELS + "/apply", json={
        "agent": "alpha", "scope": "auxiliary", "slot": "compression",
        "provider": "palpha", "model": "alpha-main", "confirm": True})
    check("★beta 的 config.yaml 未变", sha256_of(cfg_path("beta")) == before_beta)
    check("★gamma 的 config.yaml 未变", sha256_of(cfg_path("gamma")) == before_gamma)
    check("alpha 的 compression 真的改了",
          (disk_aux("alpha").get("compression") or {}).get("model") == "alpha-main",
          brief(disk_aux("alpha").get("compression")))

    # ---- [7] 思考强度真值 --------------------------------------------------
    print("\n[7] 思考强度选项来自宿主 VALID_REASONING_EFFORTS")
    overview = client.get(MODELS + "/overview", params={"agent": "alpha"}).json()
    values = overview.get("reasoning_effort_values") or []
    try:
        sys.path.insert(0, str(HERMES_AGENT))
        from hermes_constants import VALID_REASONING_EFFORTS  # noqa: E402

        host = [str(item).strip().lower() for item in VALID_REASONING_EFFORTS]
        check("界面取值 = ['none'] + 宿主常量（逐条相同）",
              values == ["none"] + host, "%s vs %s" % (values, ["none"] + host))
    except Exception as exc:  # noqa: BLE001
        check("宿主常量可读（读不到就无法证明没硬编码）", False, "%s: %s" % (type(exc).__name__, exc))

    # ---- [9] 显式逃生门 ----------------------------------------------------
    print("\n[9] allow_unlisted_model=true 是显式逃生门：放行但记「未校验」")
    before_esc = sha256_of(cfg_path("beta"))
    esc = client.post(MODELS + "/apply", json={
        "agent": "beta", "scope": "auxiliary", "slot": "approval",
        "provider": "pbeta", "model": "not-in-catalog-xyz", "confirm": True,
        "allow_unlisted_model": True})
    esc_body = esc.json() if esc.status_code == 200 else {}
    notes = " / ".join(esc_body.get("warnings") or [])
    print("        apply(beta approval, pbeta/not-in-catalog-xyz, allow_unlisted) ->",
          esc.status_code)
    check("显式 allow_unlisted_model=true ⇒ 200", esc.status_code == 200, redacted(esc))
    check("响应标 unverified_model=true（不冒充清单里选出来的）",
          esc_body.get("unverified_model") is True, brief(esc_body.get("unverified_model")))
    check("warnings 里写明「不在 provider … 的清单里」", "不在 provider" in notes, notes[:200])
    check("落盘就是那个名字",
          (disk_aux("beta").get("approval") or {}).get("model") == "not-in-catalog-xyz",
          brief(disk_aux("beta").get("approval")))
    check("beta 配置确实变了（逃生门真的写进去了）", sha256_of(cfg_path("beta")) != before_esc)

    print("\n=== 沙盒阶段汇总 ===")
    print("PASS: %d   FAIL: %d" % (passed, len(failures)))
    return 0 if not failures else 1


# ===========================================================================
# 真实 home（只读）
# ===========================================================================
def real_home_phase() -> int:
    print("[8] 真实 home 只读：3 个真实 agent 的 /usage")
    module = load_plugin_api()
    client = make_client(module)
    overview = client.get(MODELS + "/overview")
    check("真实 home /models/overview 200", overview.status_code == 200, redacted(overview))
    agents = [row.get("agent") for row in (overview.json().get("agents") or [])]
    print("        真实 agent:", agents)
    picked = [item for item in ("default", "code-worker", "work", "xiaomo", "cad-worker")
              if item in agents][:3]
    check("真实 home 至少能挑出 3 个 agent", len(picked) >= 3, str(picked))
    texts = []
    for agent in picked:
        resp = client.get(MODELS + "/usage", params={"agent": agent})
        body = resp.json() if resp.status_code == 200 else {}
        rows = body.get("modules") or []
        texts.append(resp.text)
        print("        %-12s -> %s rows=%d main=%s/%s" % (
            agent, resp.status_code, len(rows),
            (body.get("main") or {}).get("provider"), (body.get("main") or {}).get("model")))
        check("真实 agent %s 的 /usage 200 且行数 ≥ 13" % agent,
              resp.status_code == 200 and len(rows) >= 13, redacted(resp))
        sources = {row.get("source") for row in rows}
        check("%s 的来源层取值都在三态里" % agent,
              sources <= {"agent", "global", "module_default"}, str(sorted(sources)))
    blob = "\n".join(texts)
    check("真实 home 的 /usage 不含任何 sk- 形状的密钥取值", "sk-" not in blob,
          "sk- 出现=%s" % ("sk-" in blob))
    print("\n=== 真实 home 阶段汇总 ===")
    print("PASS: %d   FAIL: %d" % (passed, len(failures)))
    return 0 if not failures else 1


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--sandbox":
        return sandbox_phase(Path(sys.argv[2]))
    sandbox = REAL_HOME / "temp" / "probe-model-picker"
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--sandbox", str(sandbox)],
                          cwd=str(REAL_HOME), text=True)
    if proc.returncode != 0:
        print("\nMODEL-PICKER-PROBE-FAILED（沙盒阶段）")
        return proc.returncode
    code = real_home_phase()
    print("\n" + ("MODEL-PICKER-PROBE-OK" if code == 0 and not failures
                  else "MODEL-PICKER-PROBE-FAILED"))
    return 0 if code == 0 and not failures else 1


if __name__ == "__main__":
    sys.exit(main())

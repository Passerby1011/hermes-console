# -*- coding: utf-8 -*-
"""hermes-console · V5「模型」分区（models）验收探针（唯一验收依据）。

跑法（**不要经管道拿退出码**）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \\
        plugins/hermes-console/scripts/probe_models.py
    echo "exit=$?"

覆盖卡片 9 条::

    [1] 13 条路由全部注册，前缀 ``/models/`` 逐字正确
    [2] GET /overview、/providers、/models 在**真实 home** 上返回 200（只读）
    [3] ★凭据脱敏：/providers 响应里不含真 key（无 ``sk-``、无长度 >24 的可疑 token、
        **不含真实 .env 的任何密钥取值**；``credentials`` 里的取值只能是 ``***``）
    [4] ★按 agent 隔离：假 home 造两个 profile 配不同模型，``?agent=A`` / ``?agent=B``
        各自返回自己的模型（不是"谁的进程就返回谁"）
    [5] ★apply 往返：假 home 上 apply → 回读配置确认生效 → ``snapshot_path`` 存在且
        内容为**旧值**
    [6] ★rollback：用上一步的快照回滚 → 回读确认回到旧值
    [7] 非法输入：不存在的 agent / provider / 快照 ⇒ 全部 400（不是 500）
    [8] ★presets 不污染配置：增 / 改 / 删预设后，``profiles/*/config.yaml`` 与
        ``config.yaml`` 的 sha256 完全没变
    [9] 生产零污染：开头 / 结尾各算真实配置 sha256，断言全一致

另外三条硬约束（写在断言里，不靠自觉）::

    * **绝不对真实 profile 执行 apply/rollback**：所有写操作都在子进程的沙盒 home 里，
      主进程只发 GET + 必然被拒的 POST；
    * 写操作**必须**带 ``confirm=true``（服务端第二道二次确认），没带的必须 400；
    * **不做批量跨 profile**：``targets`` / agent 数组必须 400 ``multi_agent_forbidden``。

``HERMES_HOME`` 必须在 ``hermes_cli`` / ``core.scope`` 被 import **之前**设好，所以沙盒阶段
不图省事地和主阶段同进程（与 probe_remote 同一套路）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]      # <home>/plugins/hermes-console
DASHBOARD = PLUGIN_ROOT / "dashboard"
REAL_HOME = Path(__file__).resolve().parents[3]        # <home>
HERMES_AGENT = REAL_HOME / "hermes-agent"
PREFIX = "/api/plugins/hermes-console"
MODELS = PREFIX + "/models"

#: 本卡必须挂上的 13 条路由（方法 + 路径，路径相对 PREFIX，逐字比对）。
EXPECTED_ROUTES = (
    ("GET", "/models/overview"),
    ("GET", "/models/health"),
    ("GET", "/models/providers"),
    ("GET", "/models/providers/{agent}"),
    ("GET", "/models/models"),
    ("GET", "/models/snapshots"),
    ("GET", "/models/model-library"),
    ("POST", "/models/apply"),
    ("POST", "/models/rollback"),
    ("GET", "/models/presets"),
    ("POST", "/models/presets"),
    ("PUT", "/models/presets/{name}"),
    ("DELETE", "/models/presets/{name}"),
    # 批量（t_3d4db435）：预览 / 写入 / 一键回滚 / 账本列表
    ("POST", "/models/batch/preview"),
    ("POST", "/models/batch/apply"),
    ("POST", "/models/batch/rollback"),
    ("GET", "/models/batches"),
    # t_4d654faf：模块盘点（每个用模型的地方 + 来源层）与成对校验（provider + model）
    ("GET", "/models/usage"),
    ("GET", "/models/validate"),
)
#: 绝不能出现的写法（前缀错 / 单复数错 / 方法错配）。
FORBIDDEN_ROUTES = (
    ("GET", "/models"),
    ("GET", "/model/overview"),
    ("GET", "/models/provider"),
    ("POST", "/models/preset"),
    ("POST", "/models/apply_all"),
    ("GET", "/overview"),
)

#: 期望路由总数：V4 之后 58，本卡 +13 ⇒ 71。
EXPECTED_ROUTE_TOTAL = 71
#: 真实 home 上的 agent 数量下限（default + 8 个 profile）。
MIN_AGENTS = 9

#: 沙盒 fixture：两个 profile 配**不同** provider/模型（用来证明按 agent 隔离）。
SANDBOX_PROFILES = ("alpha", "beta")
SANDBOX_MODELS = {"alpha": ("palpha", "alpha-model-1"), "beta": ("pbeta", "beta-model-2")}
#: 沙盒里塞进 provider 块的**明文哨兵** api_key —— 出现在响应里就是泄漏。
SANDBOX_API_KEY = "sk-sandbox-SENTINEL-9f8e7d6c5b4a39281706"
#: 沙盒 .env 里的哨兵密钥（`key_env` 指向它）。
SANDBOX_ENV_KEY = "SANDBOX_ENV_SENTINEL_TOKEN_3c2b1a0f9e8d7c6b"
SANDBOX_ENV_NAME = "SANDBOX_PROVIDER_API_KEY"

#: 疑似 token：长度 >20 的连续"token 字符"串（**变量名例外**，见 :func:`looks_like_env_name`）。
SUSPICIOUS_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{21,}")
#: 全大写 + 下划线 = 环境变量**名**（不是取值，允许出现在响应里）。
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
#: 凭据类键名（只比对这些键的**取值**算不算泄漏）。
SECRET_VALUE_KEYS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")

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


#: 真实插件目录 ``data/`` 的基线（探针开始时拍一次，结束只判"没有新增"）。
DATA_BASELINE: list = []


def sha256_of(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "<missing>"
    except OSError as exc:  # noqa: BLE001 - 探针要的是"能不能比"
        return "<error:%s>" % exc


def brief(value, n: int = 240) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    return text if len(text) <= n else text[:n] + "…"


def redacted(resp) -> str:
    """失败时只打印状态码与 ``error.code``（4xx 体里可能有路径原话）。"""
    code = "<unparsed>"
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        code = str(err.get("code") or "<none>") if isinstance(err, dict) else "<no-error-object>"
    return "status=%s error.code=%s" % (resp.status_code, code)


def error_shape_ok(body) -> bool:
    """``{"ok": false, "error": {"code", "message"}}`` 的形状（只看键与类型）。"""
    if not isinstance(body, dict) or body.get("ok") is not False:
        return False
    err = body.get("error")
    return isinstance(err, dict) and bool(err.get("code")) and "message" in err


def string_values(node, out: list) -> list:
    """递归收集 JSON 里所有字符串**取值**（键不算）。"""
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for item in node.values():
            string_values(item, out)
    elif isinstance(node, (list, tuple)):
        for item in node:
            string_values(item, out)
    return out


def looks_like_env_name(text: str) -> bool:
    """``HERMES_CUSTOM_BALANCE_API_KEY`` 这类是变量**名**，不是密钥，不算泄漏。"""
    return bool(ENV_NAME_RE.match(text or ""))


def looks_like_url(text: str) -> bool:
    return "://" in (text or "")


def secret_values() -> dict:
    """真实 ``.env`` 里的**密钥类**取值（只用于"不许出现在响应里"的比对，从不打印）。"""
    found: dict = {}
    paths = [REAL_HOME / ".env"]
    profiles = REAL_HOME / "profiles"
    if profiles.is_dir():
        paths.extend(sorted(profiles.glob("*/.env")))
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
            if len(value) < 8 or looks_like_url(value):
                continue
            if any(marker in name for marker in SECRET_VALUE_KEYS):
                found["%s@%s/%s" % (name, path.parent.name, path.name)] = value
    return found


def load_plugin_api(extra_paths=()):
    """按 dashboard 的方式加载 plugin_api（合成模块名 + spec_from_file_location）。"""
    for extra in (str(DASHBOARD), str(HERMES_AGENT), *(str(p) for p in extra_paths)):
        if extra and extra not in sys.path:
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
# 子进程阶段：沙盒 home（HERMES_HOME 在任何 import 之前设好）
# ===========================================================================
def _write_sandbox_home(sandbox: Path) -> None:
    """造一个完整的假 home：根 config.yaml + 两个配了不同模型的 profile。"""
    (sandbox / "profiles").mkdir(parents=True, exist_ok=True)
    (sandbox / "config.yaml").write_text(
        "model:\n"
        "  default: root-model\n"
        "  provider: proot\n"
        "providers:\n"
        "  proot:\n"
        "    base_url: https://root.example/v1\n"
        "    models:\n"
        "      root-model: {}\n",
        encoding="utf-8")
    (sandbox / ".env").write_text(
        "%s=%s\n" % (SANDBOX_ENV_NAME, SANDBOX_ENV_KEY), encoding="utf-8")
    for name in SANDBOX_PROFILES:
        provider, model = SANDBOX_MODELS[name]
        profile = sandbox / "profiles" / name
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "profile.yaml").write_text("title: 沙盒%s\n" % name, encoding="utf-8")
        (profile / "config.yaml").write_text(
            "model:\n"
            "  default: %s\n"
            "  provider: %s\n"
            "  key_env: %s\n"
            "providers:\n"
            "  %s:\n"
            "    base_url: https://%s.example/v1\n"
            "    key_env: %s\n"
            "    api_key: %s\n"
            "    models:\n"
            "      %s: {}\n"
            "      %s-alt: {}\n"
            "  pother:\n"
            "    base_url: https://pother.example/v1\n"
            "    models:\n"
            "      other-model-1: {}\n"
            "fallback_providers:\n"
            "  - provider: %s\n"
            "    model: %s\n"
            % (model, provider, SANDBOX_ENV_NAME, provider, provider, SANDBOX_ENV_NAME,
               SANDBOX_API_KEY, model, model, provider, model),
            encoding="utf-8")


def sandbox_phase(sandbox: Path) -> int:
    shutil.rmtree(sandbox, ignore_errors=True)
    _write_sandbox_home(sandbox)
    os.environ["HERMES_HOME"] = str(sandbox)

    module = load_plugin_api()
    import api_models  # noqa: E402  (plugin_api 已按本目录 import 过同名模块)

    # 唯一漏斗：整个分区指向沙盒（内存里的路径解析也一起换，不靠环境变量走后门）
    api_models._home = lambda: sandbox
    client = make_client(module)

    def sb_check(label: str, ok: bool, detail: str = "") -> bool:
        return check("[sandbox] " + label, ok, detail)

    def cfg_of(name: str) -> Path:
        return sandbox / "profiles" / name / "config.yaml"

    def model_state(name: str) -> dict:
        """独立回读（不经被测代码）：raw yaml → model 段。"""
        import yaml  # 宿主自带

        # 允许读 _home() 之外的路径：直接用 yaml 读沙盒文件
        data = yaml.safe_load(cfg_of(name).read_text(encoding="utf-8")) or {}
        block = data.get("model") or {}
        if isinstance(block, str):
            block = {"default": block}
        return {
            "provider": str(block.get("provider") or ""),
            "model": str(block.get("default") or ""),
        }
    # ---- [S1] 按 agent 隔离 -------------------------------------------------
    print("\n[S-1] ★[4] 按 agent 隔离：两个 profile 配不同模型，各自返回自己的")
    print("        沙盒 home :", sandbox)
    rows = {}
    for name in SANDBOX_PROFILES:
        resp = client.get(MODELS + "/overview", params={"agent": name})
        body = resp.json() if resp.status_code == 200 else {}
        row = (body.get("agents") or [{}])[0]
        rows[name] = row
        print("        ?agent=%-6s -> %s provider=%s model=%s"
              % (name, resp.status_code, row.get("provider"), row.get("model")))
        sb_check("%s 的 overview 200" % name, resp.status_code == 200,
                 "" if resp.status_code == 200 else redacted(resp))
    for name, (provider, model) in SANDBOX_MODELS.items():
        sb_check("★?agent=%s 返回自己的 provider=%s" % (name, provider),
                 rows[name].get("provider") == provider, brief(rows[name].get("provider")))
        sb_check("★?agent=%s 返回自己的 model=%s" % (name, model),
                 rows[name].get("model") == model, brief(rows[name].get("model")))
    sb_check("★两个 agent 的模型**不同**（不是同一份数据换个壳）",
             rows["alpha"].get("model") != rows["beta"].get("model"),
             "%s vs %s" % (rows["alpha"].get("model"), rows["beta"].get("model")))
    all_rows = client.get(MODELS + "/overview").json().get("agents") or []
    sb_check("不带 agent ⇒ 列出 default + 2 个沙盒 profile",
             [row.get("agent") for row in all_rows] == ["default", *SANDBOX_PROFILES],
             str([row.get("agent") for row in all_rows]))

    # ---- [S2] 凭据脱敏（沙盒有已知明文哨兵） --------------------------------
    print("\n[S-2] ★[3] 沙盒 provider 里塞了明文哨兵 api_key ⇒ 一个字节都不许回显")
    prov = client.get(MODELS + "/providers", params={"agent": "alpha", "all": "1"})
    prov_text = prov.text
    body = prov.json()
    print("        /providers?agent=alpha :", prov.status_code, "| count =", body.get("count"))
    sb_check("/providers 200", prov.status_code == 200,
             "" if prov.status_code == 200 else redacted(prov))
    sb_check("★哨兵 api_key 没有出现在响应里", SANDBOX_API_KEY not in prov_text)
    sb_check("★哨兵 .env 取值也没有出现在响应里", SANDBOX_ENV_KEY not in prov_text)
    sb_check("响应里不含 'sk-'", "sk-" not in prov_text)
    cred_rows = [row for row in (body.get("providers") or [])
                 if (row.get("credentials") or {}).get("masked_keys")]
    print("        带凭据键的行 :", [(r.get("provider"), r["credentials"].get("masked_keys"))
                                     for r in cred_rows])
    sb_check("★沙盒 provider 的凭据键被识别并打码（masked_keys 非空）", bool(cred_rows),
             brief([r.get("provider") for r in cred_rows], 120))
    masked_ok = all(
        value == "***"
        for row in cred_rows
        for key, value in (row.get("credentials") or {}).items()
        if key != "masked_keys")
    sb_check("★凭据取值全是 '***'（没有一个真值溜出来）", masked_ok,
             brief([{k: v for k, v in (r.get("credentials") or {}).items() if k != "masked_keys"}
                    for r in cred_rows], 160))
    sb_check("key_env 仍可见（变量名不是秘密，界面要靠它指路人）",
             any(row.get("key_env") == SANDBOX_ENV_NAME for row in (body.get("providers") or [])),
             brief([row.get("key_env") for row in (body.get("providers") or [])], 160))

    # ---- [S3] apply 往返 ----------------------------------------------------
    print("\n[S-3] ★[5] apply 往返：写入 → 回读 → snapshot_path 存在且内容为旧值")
    target = "alpha"
    before = model_state(target)
    old_text = cfg_of(target).read_text(encoding="utf-8")
    print("        apply 前 :", before)
    resp = client.post(MODELS + "/apply", json={
        "agent": target, "scope": "model", "provider": "pother",
        "model": "other-model-1", "confirm": True, "note": "探针往返测试"})
    payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    print("        POST /apply :", resp.status_code,
          brief({k: payload.get(k) for k in ("ok", "agent", "snapshot_path", "verified")}, 200))
    sb_check("POST /apply 200", resp.status_code == 200,
             "" if resp.status_code == 200 else redacted(resp))
    sb_check("响应 ok=true 且 verified=true",
             payload.get("ok") is True and payload.get("verified") is True, brief(payload, 160))
    sb_check("★响应点名目标 agent（界面二次确认要显示的那个）",
             payload.get("agent") == target, str(payload.get("agent")))
    sb_check("★previous 是旧值（palpha / alpha-model-1）",
             (payload.get("previous") or {}).get("provider") == before["provider"]
             and (payload.get("previous") or {}).get("model") == before["model"],
             brief(payload.get("previous"), 160))
    sb_check("★applied 是目标值（pother / other-model-1）",
             (payload.get("applied") or {}).get("provider") == "pother"
             and (payload.get("applied") or {}).get("model") == "other-model-1",
             brief(payload.get("applied"), 160))
    after = model_state(target)
    print("        apply 后 :", after)
    sb_check("★磁盘上的 config.yaml 真的变成目标值（回读，不是凭接口自述）",
             after == {"provider": "pother", "model": "other-model-1"}, str(after))
    snap_path = Path(str(payload.get("snapshot_path") or ""))
    sb_check("★snapshot_path 存在且是文件", bool(str(snap_path)) and snap_path.is_file(),
             str(snap_path))
    snap_text = snap_path.read_text(encoding="utf-8") if snap_path.is_file() else ""
    snap_state = {}
    if snap_text:
        import yaml  # 宿主自带

        snap_doc = yaml.safe_load(snap_text) or {}
        snap_block = snap_doc.get("model") or {}
        if isinstance(snap_block, str):
            snap_block = {"default": snap_block}
        snap_state = {"provider": str(snap_block.get("provider") or ""),
                      "model": str(snap_block.get("default") or "")}
    print("        快照里的 model 段 :", snap_state)
    sb_check("★快照内容 = 写前的**旧值**（palpha / alpha-model-1）",
             snap_state == before, str(snap_state))
    sb_check("快照与写前原文逐字节相同", snap_text == old_text)

    # ---- [S4] rollback ------------------------------------------------------
    print("\n[S-4] ★[6] rollback：用上一步的快照回滚 → 回读确认回到旧值")
    rb = client.post(MODELS + "/rollback", json={
        "agent": target, "snapshot": str(snap_path), "confirm": True, "note": "探针回滚"})
    rb_body = rb.json() if rb.headers.get("content-type", "").startswith("application/json") else {}
    print("        POST /rollback :", rb.status_code,
          brief({k: rb_body.get(k) for k in ("ok", "agent", "verified")}, 200))
    sb_check("POST /rollback 200", rb.status_code == 200,
             "" if rb.status_code == 200 else redacted(rb))
    sb_check("回滚响应 verified=true", rb_body.get("verified") is True, brief(rb_body, 160))
    restored = model_state(target)
    print("        回滚后 :", restored)
    sb_check("★回读确认回到旧值（palpha / alpha-model-1）", restored == before, str(restored))
    sb_check("★回滚后 config.yaml 与写前原文逐字节相同",
             cfg_of(target).read_text(encoding="utf-8") == old_text)

    # ---- [S4b] fallback 段 --------------------------------------------------
    print("\n[S-4b] 备用模型段（scope=fallback）也要能写、能回滚")
    fb_before = model_state(target)
    fb = client.post(MODELS + "/apply", json={
        "agent": target, "scope": "fallback", "provider": "pother",
        "model": "other-model-2", "confirm": True})
    fb_body = fb.json() if fb.headers.get("content-type", "").startswith("application/json") else {}
    applied_fb = fb_body.get("applied") or {}
    head = applied_fb.get("fallback") or {}
    print("        apply(fallback) :", fb.status_code, "head =", head)
    sb_check("scope=fallback 的 apply 200", fb.status_code == 200,
             "" if fb.status_code == 200 else redacted(fb))
    sb_check("★applied.fallback 显式给出写进去的备用首条（主模型字段没被冒充）",
             head.get("provider") == "pother" and head.get("model") == "other-model-2",
             str(head))
    sb_check("主模型没被 fallback 段顺手改掉",
             model_state(target) == fb_before, str(model_state(target)))
    fb_rb = client.post(MODELS + "/rollback", json={
        "agent": target, "snapshot": fb_body.get("snapshot_path"), "confirm": True})
    sb_check("fallback 段也能用快照回滚（200 + verified）",
             fb_rb.status_code == 200 and fb_rb.json().get("verified") is True, redacted(fb_rb))
    sb_check("回滚后备用链与写前一致（逐字节）",
             cfg_of(target).read_text(encoding="utf-8") == old_text)

    # ---- [S5] 非法输入 ------------------------------------------------------
    print("\n[S-5] [7] 非法输入 ⇒ 400（不是 500）")
    cases = [
        ("不存在的 agent（apply）",
         lambda: client.post(MODELS + "/apply", json={
             "agent": "no-such-agent-xyz", "provider": "palpha", "model": "x", "confirm": True})),
        ("不存在的 provider（apply）",
         lambda: client.post(MODELS + "/apply", json={
             "agent": "alpha", "provider": "no-such-provider", "model": "x", "confirm": True})),
        ("不存在的快照（rollback）",
         lambda: client.post(MODELS + "/rollback", json={
             "agent": "alpha", "snapshot": "19990101-000000", "confirm": True})),
        ("非法 agent id（../../etc）",
         lambda: client.post(MODELS + "/apply", json={
             "agent": "../../etc", "provider": "palpha", "model": "x", "confirm": True})),
        ("没带 confirm（第二道确认）",
         lambda: client.post(MODELS + "/apply", json={
             "agent": "alpha", "provider": "palpha", "model": "x"})),
        ("批量 targets（跨 profile）",
         lambda: client.post(MODELS + "/apply", json={
             "agent": "alpha", "targets": ["alpha", "beta"],
             "provider": "palpha", "model": "x", "confirm": True})),
        ("非法 scope",
         lambda: client.post(MODELS + "/apply", json={
             "agent": "alpha", "scope": "everything", "provider": "palpha",
             "model": "x", "confirm": True})),
        ("空 model",
         lambda: client.post(MODELS + "/apply", json={
             "agent": "alpha", "provider": "palpha", "model": "  ", "confirm": True})),
        ("不存在的 provider（GET /models）",
         lambda: client.get(MODELS + "/models", params={"agent": "alpha",
                                                        "provider": "no-such-provider"})),
        ("不存在的预设（DELETE）",
         lambda: client.delete(MODELS + "/presets/no-such-preset")),
        ("不存在的预设（PUT）",
         lambda: client.put(MODELS + "/presets/no-such-preset", json={"model": "x"})),
    ]
    for label, call in cases:
        resp = call()
        body = resp.json()
        print("        %-30s -> %s %s" % (label, resp.status_code,
                                         (body.get("error") or {}).get("code")
                                         if isinstance(body, dict) else ""))
        sb_check("%-30s 400 且结构化错误" % label,
                 resp.status_code == 400 and error_shape_ok(body), redacted(resp))
    sb_check("★非法输入全都**没有**改动 config.yaml（拒绝发生在写入之前）",
             cfg_of("alpha").read_text(encoding="utf-8") == old_text)

    # ---- [S6] presets 不污染配置 -------------------------------------------
    print("\n[S-6] ★[8] presets 增删改**不污染** profile 配置")
    paths = [sandbox / "config.yaml"] + [cfg_of(name) for name in SANDBOX_PROFILES]
    before_sha = {str(p): sha256_of(p) for p in paths}
    created = client.post(MODELS + "/presets", json={
        "name": "probe-preset", "provider": "pother", "model": "other-model-1",
        "note": "探针建的"})
    sb_check("POST /presets 200", created.status_code == 200,
             "" if created.status_code == 200 else redacted(created))
    updated = client.put(MODELS + "/presets/probe-preset", json={"model": "other-model-2"})
    sb_check("PUT /presets/{name} 200", updated.status_code == 200,
             "" if updated.status_code == 200 else redacted(updated))
    listed = client.get(MODELS + "/presets", params={"agent": "alpha"})
    names = [row.get("name") for row in (listed.json().get("presets") or [])]
    sb_check("GET /presets 能看到新建的预设", "probe-preset" in names, str(names))
    sb_check("presets 文件落在 console 自己的数据目录（不是 profile 配置里）",
             str(listed.json().get("path", "")).replace("\\", "/").endswith(
                 "plugins/hermes-console/data/presets.json"),
             str(listed.json().get("path")))
    deleted = client.delete(MODELS + "/presets/probe-preset")
    sb_check("DELETE /presets/{name} 200", deleted.status_code == 200,
             "" if deleted.status_code == 200 else redacted(deleted))
    after_sha = {str(p): sha256_of(p) for p in paths}
    print("        presets 增删改前后 sha256 :")
    for path in paths:
        same = before_sha[str(path)] == after_sha[str(path)]
        print("          %-8s %s  %s" % ("same" if same else "CHANGED",
                                         after_sha[str(path)][:16], str(path)))
    sb_check("★所有 config.yaml 的 sha256 完全没变（预设只写 presets.json）",
             before_sha == after_sha, "变了: " + str([p for p in before_sha
                                                     if before_sha[p] != after_sha.get(p)]))

    print("\n  --- 沙盒阶段：FAIL %d ---" % len([f for f in failures if f.startswith("[sandbox]")]))
    return 1 if failures else 0


def main_phase() -> int:
    """主进程阶段：真实 home 只读 + 全量对账。"""
    global DATA_BASELINE
    _data_dir = PLUGIN_ROOT / "data"
    DATA_BASELINE = (sorted(item.name for item in _data_dir.iterdir())
                     if _data_dir.is_dir() else [])
    print("[0] 环境")
    print("        python   :", sys.version.split()[0])
    print("        plugin   :", PLUGIN_ROOT)
    print("        real home:", REAL_HOME)
    print("        HERMES_HOME(env):", os.environ.get("HERMES_HOME") or "<unset>")
    print("        hermes-agent:", HERMES_AGENT, "|", "exists=" + str(HERMES_AGENT.is_dir()))

    if not (DASHBOARD / "api_models.py").is_file():
        print("  FAIL  本卡交付缺失：找不到", DASHBOARD / "api_models.py")
        print("MODELS-PROBE-FAILED")
        return 1

    # -----------------------------------------------------------------------
    print("\n[1] 13 条路由全部注册（前缀逐字 /models/）")
    module = load_plugin_api()
    registered = set()
    for route in module.router.routes:
        for method in sorted(getattr(route, "methods", []) or []):
            registered.add((method, route.path))
    missing = [pair for pair in EXPECTED_ROUTES if pair not in registered]
    check("17 条路由一条不缺", not missing, "缺: " + str(missing))
    for method, path in EXPECTED_ROUTES:
        check("路由已注册 %-6s %s" % (method, path), (method, path) in registered)
    # t_3d4db435 起：批量只允许走 /models/batch/*（单 agent /apply 仍拒绝 targets）
    check("★批量端点只在 /models/batch/* 下（没有 apply_all 这类别名）", not any(
        "apply_all" in path or ("batch" in path.lower() and not path.startswith("/models/batch"))
        for _m, path in registered if path.startswith("/models")),
        str([p for _m, p in sorted(registered) if p.startswith("/models")]))
    models_routes = sorted("%s %s" % (m, p) for m, p in registered if p.startswith("/models"))
    check("/models/* 下恰好 23 条（不多不少：17 条旧契约 + 4 条批量 + moa 2 条 + usage/validate）",
          len(models_routes) == 23, str(models_routes))

    client = make_client(module)
    import api_models  # noqa: E402
    print("        加载失败的域模块 :", module.DOMAIN_FAILURES)
    check("没有域模块加载失败（failed={}）", module.DOMAIN_FAILURES == {},
          brief(module.DOMAIN_FAILURES, 200))
    check("api_models 出现在 DOMAIN_LOADED 里", "api_models" in module.DOMAIN_LOADED,
          str(module.DOMAIN_LOADED))

    # -----------------------------------------------------------------------
    print("\n[2] ★真实 home 只读端点 200（/overview、/providers、/models）")
    ov = client.get(MODELS + "/overview")
    ov_body = ov.json()
    rows = ov_body.get("agents") or []
    print("        /overview :", ov.status_code, "| count =", ov_body.get("count"))
    check("GET /models/overview 200", ov.status_code == 200,
          "" if ov.status_code == 200 else redacted(ov))
    check("★overview 覆盖 %d 个 agent（真实 home 现扫，不是写死 4 个）" % MIN_AGENTS,
          len(rows) >= MIN_AGENTS, "%d 个" % len(rows))
    check("每行都带 agent / provider / model / config_path",
          all("agent" in r and "provider" in r and "model" in r and "config_path" in r
              for r in rows), brief(rows[0] if rows else {}, 200))
    has_model = [r.get("agent") for r in rows if (r.get("model") or "")]
    check("至少有一个 agent 真的配了模型（不是全空表）", bool(has_model), str(has_model[:4]))

    pv = client.get(MODELS + "/providers", params={"agent": "default"})
    pv_body = pv.json()
    print("        /providers:", pv.status_code, "| count =", pv_body.get("count"))
    check("GET /models/providers 200", pv.status_code == 200,
          "" if pv.status_code == 200 else redacted(pv))
    check("provider 列表非空（真的读到了 registry / config）",
          int(pv_body.get("count") or 0) > 0, str(pv_body.get("count")))
    one = client.get(MODELS + "/providers/" + str((rows[0] or {}).get("agent") or "default"))
    check("GET /models/providers/{agent} 200", one.status_code == 200, redacted(one))

    prov0 = (pv_body.get("providers") or [{}])[0]
    prov_name = str(prov0.get("provider") or "")
    mdl = client.get(MODELS + "/models", params={"agent": "default", "provider": prov_name})
    print("        /models?provider=%s : %s | source = %s"
          % (prov_name, mdl.status_code, mdl.json().get("source")))
    check("GET /models/models 200", mdl.status_code == 200,
          "" if mdl.status_code == 200 else redacted(mdl))
    check("模型列表带 source 字段（说清楚数据哪来的）",
          mdl.json().get("source") in ("config", "config+remote", "remote", "none",
                                       "moa-presets", "moa-presets-empty",
                                       "moa-presets-missing", "virtual-no-catalog"),
          str(mdl.json().get("source")))
    check("★默认不打外网（live=0 时不出现 remote）",
          mdl.json().get("source") != "remote", str(mdl.json().get("source")))

    health = client.get(MODELS + "/health").json()
    print("        /models/health :", brief(health, 220))
    check("GET /models/health ok=true", health.get("ok") is True, brief(health, 200))
    check("health 里能看到官方写入器与 profile 作用域",
          (health.get("checks", {}).get("official_writer") or {}).get("ok") is True,
          brief(health.get("checks", {}).get("official_writer"), 200))
    check("★health 声明 apply 需要 confirm、且禁止批量",
          health.get("apply_requires_confirm") is True
          and health.get("multi_agent_forbidden") is True, brief(health, 200))

    lib = client.get(MODELS + "/model-library", params={"agent": "default"}).json()
    print("        /model-library : count =", lib.get("count"), "model_total =", lib.get("model_total"))
    check("GET /models/model-library 200 且有分组", int(lib.get("count") or 0) > 0,
          str(lib.get("count")))

    snaps = client.get(MODELS + "/snapshots").json()
    check("GET /models/snapshots 200（空目录也算合法空态）",
          snaps.get("ok") is True and isinstance(snaps.get("snapshots"), list),
          brief({k: snaps.get(k) for k in ("ok", "count")}, 120))

    # -----------------------------------------------------------------------
    print("\n[3] ★凭据脱敏：真实 home 的 /providers 不含任何真 key")
    texts = []
    for agent in ("default", (rows[0] or {}).get("agent") or "default"):
        for extra in ("0", "1"):
            resp = client.get(MODELS + "/providers",
                              params={"agent": str(agent), "all": extra})
            texts.append(resp.text)
    blob = "".join(texts)
    secrets = secret_values()
    print("        真实 .env 里的密钥类条目 : %d 条（取值不打印）" % len(secrets))
    check("★响应里不含 'sk-'", "sk-" not in blob,
          "命中位置 %d" % blob.count("sk-"))
    values = []
    for text in texts:
        try:
            string_values(json.loads(text), values)
        except ValueError:
            continue
    # 响应里的**标识符类字段**（provider 名 / 模型名 / 变量名 / 地址 / 标签）本来就该是长串，
    # 它们不是密钥。把它们收集成白名单后，剩下的"长 token"才需要怀疑。
    identifier_keys = {"provider", "slug", "label", "model", "models", "key_env", "key_envs",
                       "base_url", "configured_reason", "source", "kind", "id", "auth_type",
                       "agent", "profile", "path", "config_path", "snapshot_root",
                       "default_model", "masked_keys", "note"}
    identifiers = set()

    def collect(node) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if str(key) in identifier_keys:
                    if isinstance(item, str):
                        identifiers.add(item)
                    elif isinstance(item, (list, tuple)):
                        identifiers.update(item for item in item if isinstance(item, str))
                else:
                    collect(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                collect(item)

    for text in texts:
        try:
            collect(json.loads(text))
        except ValueError:
            continue
    suspicious = [v for v in values
                  if SUSPICIOUS_TOKEN_RE.search(v or "")
                  and v not in identifiers
                  and not looks_like_env_name(v)
                  and not looks_like_url(v)]
    print("        白名单标识符 %d 条 / 疑似 token : %s"
          % (len(identifiers), [item[:10] + "…" for item in suspicious[:6]]))
    check("★不含长度 >24 的可疑 token 串（标识符 / 变量名 / URL 除外）", not suspicious,
          brief(suspicious, 200))
    leaked = [name for name, value in secrets.items() if value in blob]
    check("★不含真实 .env 的任何密钥取值", not leaked,
          "泄漏 %d 条（只报键名）: %s" % (len(leaked), [n.split("@")[0] for n in leaked[:5]]))
    cred_values = [value for value in values if value == "***"]
    print("        掩码占位 '***' 出现次数 :", len(cred_values))
    check("响应里凭据字段要么缺失、要么是 '***'（没有第三种形态）",
          all(value in ("***", "") or not str(value)
              for row in (pv_body.get("providers") or [])
              for key, value in (row.get("credentials") or {}).items()
              if key != "masked_keys"), "见上一行")

    # -----------------------------------------------------------------------
    print("\n[4] ★真实 home 上**绝不**执行 apply/rollback（写操作全在沙盒）")
    for label, body in (
        ("apply 不带 confirm", {"agent": "default", "provider": prov_name, "model": "x"}),
        ("apply 带 confirm 但 agent 不存在",
         {"agent": "no-such-agent-xyz", "provider": prov_name, "model": "x", "confirm": True}),
        ("apply 批量 targets",
         {"agent": "default", "targets": ["default", "xiaomo"], "provider": prov_name,
          "model": "x", "confirm": True}),
    ):
        resp = client.post(MODELS + "/apply", json=body)
        print("        %-32s -> %s %s" % (label, resp.status_code,
                                          (resp.json().get("error") or {}).get("code")))
        check("%s ⇒ 4xx（被拒，不写配置）" % label,
              400 <= resp.status_code < 500 and error_shape_ok(resp.json()), redacted(resp))
    rb_bad = client.post(MODELS + "/rollback",
                         json={"agent": "default", "snapshot": "19990101-000000",
                               "confirm": True})
    check("rollback 到不存在的快照 ⇒ 400（不是 500）",
          rb_bad.status_code == 400 and error_shape_ok(rb_bad.json()), redacted(rb_bad))

    # -----------------------------------------------------------------------
    print("\n[5] 模块登记 + 旧插件仍在（并存期不许删）")
    manifest = PLUGIN_ROOT / "dashboard" / "modules" / "models" / "manifest.json"
    check("dashboard/modules/models/manifest.json 存在", manifest.is_file(), str(manifest))
    if manifest.is_file():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        print("        manifest :", brief(data, 200))
        # t_4d654faf：dashboard/manifest.json 的分区条目后来多了 requires/settings_schema/version，
        # 且「模型」分区的 default_enabled 已是 true（另一张卡改的）。这里只断本分区关心的六个键，
        # 不再要求"整个对象逐字相等"（那样每加一个元数据键都会假红）。
        check("manifest 分区条目的关键字段正确（id/label/nav_order/default_enabled/api_module/section）",
              data.get("id") == "models" and data.get("label") == "模型"
              and data.get("nav_order") == 25 and data.get("default_enabled") is True
              and data.get("api_module") == "api_models" and data.get("section") == "models",
              brief(data, 200))
    old_plugin = REAL_HOME / "plugins" / "model-switch"
    check("旧 plugins/model-switch 仍在（本卡不删）", old_plugin.is_dir(), str(old_plugin))
    check("旧 plugin_api.py 仍在（可回退）",
          (old_plugin / "dashboard" / "plugin_api.py").is_file())

    return 0


def main() -> int:
    global passed
    # 生产零污染基线
    def fingerprint() -> dict:
        paths = [REAL_HOME / "config.yaml"]
        profiles = REAL_HOME / "profiles"
        if profiles.is_dir():
            paths.extend(sorted(profiles.glob("*/config.yaml")))
        return {str(item): sha256_of(item) for item in paths}

    print("[9-前置] 生产指纹（开头）")
    before = fingerprint()
    for path, digest in before.items():
        print("        %s  %s" % (digest[:16], path.replace("\\", "/")))
    check("指纹覆盖到 9 个 config.yaml（根 + 8 个 profile）", len(before) == 9,
          "%d 个" % len(before))
    profiles_before = sorted(item.name for item in (REAL_HOME / "profiles").iterdir()
                             if item.is_dir())

    code = main_phase()

    # -----------------------------------------------------------------------
    print("\n[S] ★沙盒阶段（子进程 / 假 home）：隔离 / 脱敏 / apply 往返 / rollback / presets")
    sandbox = Path(tempfile.mkdtemp(prefix="models-probe-sandbox-"))
    try:
        env = dict(os.environ)
        env["HERMES_HOME"] = str(sandbox)     # 子进程里 import 之前就生效
        env.pop("PYTHONPATH", None)
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                               "--sandbox-phase", str(sandbox)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=600)
        out = proc.stdout or ""
        sys.stdout.write(out)
        if proc.stderr.strip():
            print("        [stderr]", proc.stderr.strip()[:800])
        for line in out.splitlines():
            if line.startswith("  PASS  "):
                passed += 1
            elif line.startswith("  FAIL  "):
                failures.append(line.strip()[8:])
        check("沙盒子进程退出码 0", proc.returncode == 0, "exit=%d" % proc.returncode)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    # -----------------------------------------------------------------------
    print("\n[9-收尾] ★生产零污染（结尾指纹 vs 开头指纹）")
    after = fingerprint()
    dirty = []
    for path, digest in before.items():
        now = after.get(path)
        if now != digest:
            dirty.append(path)
        print("        %-8s %s  %s" % ("same" if now == digest else "CHANGED",
                                       (now or "<missing>")[:16], path.replace("\\", "/")))
    check("9 个 config.yaml 一个字节都没变（真实 home 零污染）", not dirty, "变了: " + str(dirty))
    profiles_after = sorted(item.name for item in (REAL_HOME / "profiles").iterdir()
                            if item.is_dir())
    check("profiles/ 下没有新增目录", profiles_after == profiles_before,
          "新增: " + str(set(profiles_after) - set(profiles_before)))
    # 一个 GET /models/health 不许在真实插件目录里造出 data/（那是我一开始写 mkdir 的坑）。
    # t_4d654faf：data/ 现在**本来就存在**（presets.json / batches / remote-events.jsonl 是
    # 本分区自己的落点，10-31 起就在），所以改成"本次探针没有新增任何条目"——
    # 断言从"目录不存在"收敛为"GET 没有副作用"，判的是同一件事。
    data_dir = PLUGIN_ROOT / "data"
    data_after = sorted(item.name for item in data_dir.iterdir()) if data_dir.is_dir() else []
    check("★真实插件目录的 data/ 没有本次新增条目（GET 不许有副作用）",
          set(data_after) <= set(DATA_BASELINE),
          "新增: " + str(sorted(set(data_after) - set(DATA_BASELINE))))

    print()
    total = passed + len(failures)
    print("PASS=%d FAIL=%d TOTAL=%d" % (passed, len(failures), total))
    if failures or code != 0:
        print("MODELS-PROBE-FAILED（%d 项）" % len(failures))
        for label in failures:
            print("  -", label)
        return 1
    print("MODELS-PROBE-OK")
    return 0


if __name__ == "__main__":
    if "--sandbox-phase" in sys.argv:
        idx = sys.argv.index("--sandbox-phase")
        raise SystemExit(sandbox_phase(Path(sys.argv[idx + 1]).resolve()))
    raise SystemExit(main())


# -*- coding: utf-8 -*-
"""hermes-console · V3c「上下文引擎」分区（vcc）验收探针（唯一验收依据）。

跑法（**不要经管道拿退出码**）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        plugins/hermes-console/scripts/probe_vcc.py
    echo "exit=$?"        # bash；PowerShell 用 $LASTEXITCODE

覆盖卡片 7 条::

    [1] 6 条路由全部注册，路径**逐字**正确
        GET  /vcc/overview   GET  /vcc/log      GET  /vcc/compaction
        POST /vcc/engine     POST /vcc/search_archived   GET  /vcc/engine_status
    [2] GET /vcc/overview 真实返回 200（真实 home，只读）
    [2b] ★★引擎**版本**按 agent 解析：面板每行 engine_version_actual ==
        该 agent 自己 ``plugins/hermes-vcc-cn/plugin.yaml`` 的 version（独立读盘对照），
        只装了旧版的 agent 必须 engine_mismatch=true（不是全绿）
    [3] ★★GET /vcc/engine_status 的引擎名 == 该 profile 自己 config.yaml 的
        context.engine；且**两个配置不同的 profile 返回不同引擎名**（证明没写死）
    [4] POST /vcc/engine 缺 confirm=true ⇒ 400，且响应体是 {"ok":false,"error":{...}}
    [5] ★POST /vcc/engine 带 confirm=true ⇒ 在**假 home** 上真实写入并回读一致
    [5b] ★★切换前校验引擎本体：假 home 里没装引擎的 agent ⇒ 409 engine_not_installed，
        config.yaml 一字节未写；内置 compressor 不需要装插件 ⇒ 仍 200
    [6] GET /vcc/log 与 /vcc/compaction 返回结构合法（有数据 200；无数据空态，**不是 500**）
    [7] ★4xx **不打印响应体全文**（凭据泄露防护）：失败时只打印状态码与 error.code

外加：``/health`` 的 ``failed`` 为空、路由总数 = 71（V4 +5、V5 模型 +13 之后）。

**本探针的写入边界**：真实 home 上只做 GET（overview / log / compaction / engine_status）
与**必定被拒的** POST（缺 confirm）。所有真写入只在沙盒 home 里执行。

沙盒阶段跑在**子进程**里（``--sandbox-phase``）：``HERMES_HOME`` 必须在
``hermes_cli.config`` 被 import **之前**就设好，否则官方 ``save_config`` 会按真实
home 解析路径 —— 在同一个进程里"先跑真实 home 再设 env"有写穿真实配置的风险，
所以这里不图省事。
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

#: 本卡必须挂上的 8 条路由（方法 + 路径，路径相对 PREFIX，逐字比对）。
EXPECTED_ROUTES = (
    ("GET", "/vcc/overview"),
    ("GET", "/vcc/log"),
    ("GET", "/vcc/compaction"),
    ("POST", "/vcc/engine"),
    ("POST", "/vcc/search_archived"),
    ("GET", "/vcc/engine_status"),
    # t_c4c4d49e 按对话下钻
    ("GET", "/vcc/conversations"),
    ("GET", "/vcc/conversation"),
)

#: 期望路由总数**下限**：V3a 47→53（+6），V4 远端 +5 = 58，V5 模型 +13 = 71，
#: t_c4c4d49e 对话下钻 +2 = 73。并行卡片会继续加路由，所以这里只当下限用
#: （逐字比对的是上面 EXPECTED_ROUTES 那几条，总数只做回归兜底）。
EXPECTED_ROUTE_TOTAL = 71

#: 沙盒里给 ``default`` 与 ``alpha`` 写**不同**的引擎，用来证明引擎按 profile 解析。
SANDBOX_DEFAULT_ENGINE = "compressor"
SANDBOX_ALPHA_ENGINE = "hermes-vcc-cn"

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
    except FileNotFoundError:
        return "<missing>"
    except OSError as exc:  # noqa: BLE001 - 探针要的是"能不能比"，不是精确错误
        return "<error:%s>" % exc


def brief(value, n: int = 260) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    return text if len(text) <= n else text[:n] + "…"


def redacted(resp) -> str:
    """★[7] 4xx 的唯一打印口径：**只**给状态码与 ``error.code``。

    绝不回显请求体/响应体全文 —— 那些体里可能带着上游凭据、路径、原始异常。
    解析 body 只为了取 ``error.code`` 这一个字段，取到就丢。
    """
    code = "<unparsed>"
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - 非 JSON 就只报状态码
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        code = str(err.get("code") or "<none>") if isinstance(err, dict) else "<no-error-object>"
    return "status=%s error.code=%s" % (resp.status_code, code)


def error_shape_ok(body) -> bool:
    """``{"ok": false, "error": {...}}`` 的形状（只看键与类型，不看内容）。"""
    if not isinstance(body, dict) or body.get("ok") is not False:
        return False
    err = body.get("error")
    return isinstance(err, dict) and bool(err.get("code")) and "message" in err


#: 读 ``context.engine`` 的**独立**实现（故意不复用 api_vcc 的私有函数：
#: 探针要是拿被测代码自己的解析器去对账，等于自己跟自己比）。
_CTX_BLOCK_RE = re.compile(r"^context:\s*$(.*?)(?=^\S)", re.M | re.S)
_ENGINE_LINE_RE = re.compile(r"^\s+engine:\s*(\S+)", re.M)


def read_engine_independently(config_path: Path) -> str | None:
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
        if isinstance(data, dict):
            ctx = data.get("context")
            if isinstance(ctx, dict) and ctx.get("engine"):
                return str(ctx["engine"]).strip()
    except Exception:  # noqa: BLE001 - yaml 不可用就走正则
        pass
    block = _CTX_BLOCK_RE.search(text)
    if block:
        hit = _ENGINE_LINE_RE.search(block.group(1))
        if hit:
            return hit.group(1).strip()
    return None


def read_plugin_version_independently(meta_path: Path) -> str | None:
    """独立读 ``plugin.yaml`` 的 ``version``（不复用 api_vcc 的解析器，避免自己跟自己比）。"""
    try:
        text = meta_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
        if isinstance(data, dict) and data.get("version"):
            return str(data["version"]).strip()
    except Exception:  # noqa: BLE001 - yaml 不可用就走正则
        pass
    hit = re.search(r"^version:\s*(\S+)", text, re.M)
    return hit.group(1).strip().strip("'\"") if hit else None


# ===========================================================================
# 子进程阶段：沙盒 home（HERMES_HOME 在任何 import 之前设好）
# ===========================================================================
def sandbox_phase(sandbox: Path) -> int:
    shutil.rmtree(sandbox, ignore_errors=True)
    (sandbox / "profiles" / "alpha").mkdir(parents=True)
    (sandbox / "profiles" / "beta").mkdir(parents=True)
    (sandbox / "config.yaml").write_text(
        "context:\n  engine: %s\n" % SANDBOX_DEFAULT_ENGINE, encoding="utf-8")
    (sandbox / "profiles" / "alpha" / "config.yaml").write_text(
        "context:\n  engine: %s\n  vcc:\n    budget_ceiling_tokens: 3200\n" % SANDBOX_ALPHA_ENGINE,
        encoding="utf-8")
    (sandbox / "profiles" / "beta" / "config.yaml").write_text(
        "context:\n  engine: %s\n" % SANDBOX_DEFAULT_ENGINE, encoding="utf-8")

    os.environ["HERMES_HOME"] = str(sandbox)
    for extra in (str(DASHBOARD), str(HERMES_AGENT)):
        if extra not in sys.path:
            sys.path.insert(0, extra)

    from fastapi import FastAPI  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_hermes_console", DASHBOARD / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    import api_vcc  # noqa: E402  (plugin_api 已按本目录 import 过同名模块)

    api_vcc._home = lambda: sandbox          # 唯一漏斗：把整个分区指向沙盒
    api_vcc._CACHE.clear()

    app = FastAPI()
    app.include_router(mod.router, prefix=PREFIX)
    client = TestClient(app, raise_server_exceptions=False)

    global passed
    fails: list[str] = []

    def sb_check(label: str, ok: bool, detail: str = "") -> bool:
        global passed
        print(("  PASS  " if ok else "  FAIL  ") + label + (" — " + detail if detail else ""))
        if ok:
            passed += 1
        else:
            fails.append(label)
        return ok

    print("\n[S-1] ★★两个**配置不同**的 profile：引擎名必须各自解析（沙盒 home）")
    print("        沙盒 home :", sandbox)
    default = client.get(PREFIX + "/vcc/engine_status", params={"agent": "default"}).json()
    alpha = client.get(PREFIX + "/vcc/engine_status", params={"agent": "alpha"}).json()
    beta = client.get(PREFIX + "/vcc/engine_status", params={"agent": "beta"}).json()
    print("        default -> engine=%-14s source=%-7s config=%s"
          % (default.get("engine"), default.get("engine_source"), default.get("config_path")))
    print("        alpha   -> engine=%-14s source=%-7s context.vcc=%s"
          % (alpha.get("engine"), alpha.get("engine_source"), brief((alpha.get("context") or {}).get("vcc"))))
    print("        beta    -> engine=%-14s source=%-7s" % (beta.get("engine"), beta.get("engine_source")))
    sb_check("default 解析为 %s（沙盒根 config.yaml 写的）" % SANDBOX_DEFAULT_ENGINE,
             default.get("engine") == SANDBOX_DEFAULT_ENGINE, str(default.get("engine")))
    sb_check("alpha 解析为 %s（沙盒 profile 写的）" % SANDBOX_ALPHA_ENGINE,
             alpha.get("engine") == SANDBOX_ALPHA_ENGINE, str(alpha.get("engine")))
    sb_check("★两个 profile 返回**不同**引擎名 ⇒ 引擎按 profile 解析，没写死 default",
             default.get("engine") != alpha.get("engine"),
             "%s != %s" % (default.get("engine"), alpha.get("engine")))
    sb_check("engine_source=yaml（真读了 config.yaml，不是默认回退）",
             default.get("engine_source") == "yaml" and alpha.get("engine_source") == "yaml",
             "%s / %s" % (default.get("engine_source"), alpha.get("engine_source")))
    sb_check("alpha 的 config_path 指向沙盒里那份文件",
             Path(alpha.get("config_path") or "") == sandbox / "profiles" / "alpha" / "config.yaml",
             str(alpha.get("config_path")))

    print("\n[S-2] ★[5] POST /vcc/engine 带 confirm=true ⇒ 沙盒里真实写入并回读一致")
    alpha_cfg = sandbox / "profiles" / "alpha" / "config.yaml"
    before_text = alpha_cfg.read_text(encoding="utf-8")
    wrote = client.post(PREFIX + "/vcc/engine",
                        json={"agent": "alpha", "engine": SANDBOX_DEFAULT_ENGINE, "confirm": True})
    wbody = wrote.json() if wrote.status_code == 200 else {}
    print("        写 alpha: %s -> %s（changed=%s）"
          % (SANDBOX_ALPHA_ENGINE, wbody.get("engine"), wbody.get("changed")))
    sb_check("写请求 200", wrote.status_code == 200,
             "" if wrote.status_code == 200 else redacted(wrote))
    sb_check("回读 engine == %s" % SANDBOX_DEFAULT_ENGINE,
             wbody.get("engine") == SANDBOX_DEFAULT_ENGINE, str(wbody.get("engine")))
    sb_check("before 字段记录了写前值 %s" % SANDBOX_ALPHA_ENGINE,
             wbody.get("before") == SANDBOX_ALPHA_ENGINE, str(wbody.get("before")))
    sb_check("changed=true", wbody.get("changed") is True, str(wbody.get("changed")))
    after_text = alpha_cfg.read_text(encoding="utf-8")
    sb_check("★磁盘文件真的变了（engine: %s 落盘）" % SANDBOX_DEFAULT_ENGINE,
             ("engine: %s" % SANDBOX_DEFAULT_ENGINE) in after_text
             and ("engine: %s" % SANDBOX_ALPHA_ENGINE) not in after_text,
             "前 %d B → 后 %d B" % (len(before_text), len(after_text)))
    sb_check("同级的 context.vcc.budget_ceiling_tokens 没被写没（merge 保住其它键）",
             "budget_ceiling_tokens" in after_text and "3200" in after_text)
    sb_check("独立解析器读回也是 %s" % SANDBOX_DEFAULT_ENGINE,
             read_engine_independently(alpha_cfg) == SANDBOX_DEFAULT_ENGINE,
             str(read_engine_independently(alpha_cfg)))
    again = client.post(PREFIX + "/vcc/engine",
                        json={"agent": "alpha", "engine": SANDBOX_DEFAULT_ENGINE, "confirm": True}).json()
    sb_check("重复写同值 ⇒ changed=false（幂等，不再动文件）",
             again.get("changed") is False, str(again.get("message")))
    sb_check("beta 完全没被牵连（还是 %s）" % SANDBOX_DEFAULT_ENGINE,
             read_engine_independently(sandbox / "profiles" / "beta" / "config.yaml")
             == SANDBOX_DEFAULT_ENGINE)

    print("\n[S-3] [6] 空沙盒上的空态（有数据 200 / 无数据空态，**都不是 500**）")
    log_resp = client.get(PREFIX + "/vcc/log", params={"agent": "alpha"})
    log_body = log_resp.json()
    print("        /vcc/log ->", log_resp.status_code, brief({k: log_body.get(k) for k in ("ok", "agent", "count")}))
    sb_check("空 home 的 /vcc/log 仍是 200（空态不是 500）", log_resp.status_code == 200,
             "" if log_resp.status_code == 200 else redacted(log_resp))
    sb_check("空态结构合法：ok=true / count=0 / lines=[]",
             log_body.get("ok") is True and log_body.get("count") == 0 and log_body.get("lines") == [],
             brief({k: log_body.get(k) for k in ("count", "lines")}))
    comp_resp = client.get(PREFIX + "/vcc/compaction", params={"agent": "alpha", "message_id": 1})
    print("        /vcc/compaction(无 state.db) ->", redacted(comp_resp))
    sb_check("无 state.db 的 /vcc/compaction ⇒ 结构化 404（不是 500）",
             comp_resp.status_code == 404 and error_shape_ok(comp_resp.json()), redacted(comp_resp))
    bad_agent = client.get(PREFIX + "/vcc/log", params={"agent": "No Such Agent!"})
    sb_check("非法 agent ⇒ 结构化 400（不是 500）",
             bad_agent.status_code == 400 and error_shape_ok(bad_agent.json()), redacted(bad_agent))

    print("\n[S-3b] ★切换前校验引擎本体：该 agent 目录没装引擎 ⇒ 409 engine_not_installed（不许假成功）")
    beta_cfg = sandbox / "profiles" / "beta" / "config.yaml"
    beta_before = hashlib.sha256(beta_cfg.read_bytes()).hexdigest()
    not_installed = client.post(PREFIX + "/vcc/engine",
                                json={"agent": "beta", "engine": "hermes-vcc-cn", "confirm": True})
    nb = not_installed.json() if not_installed.status_code else {}
    print("        POST beta engine=hermes-vcc-cn ->", not_installed.status_code,
          (nb.get("error") or {}).get("code"), "| plugin_path =",
          (nb.get("error") or {}).get("plugin_path"))
    sb_check("★未装引擎的 agent 切换 ⇒ 409（不是 200 假成功）",
             not_installed.status_code == 409, redacted(not_installed))
    sb_check("★error.code == engine_not_installed",
             (nb.get("error") or {}).get("code") == "engine_not_installed",
             str((nb.get("error") or {}).get("code")))
    sb_check("失败体里给了该 agent 自己的 plugin.yaml 路径（可核对，不是空话）",
             str((nb.get("error") or {}).get("plugin_path") or "").endswith(
                 "plugins\\hermes-vcc-cn\\plugin.yaml")
             or str((nb.get("error") or {}).get("plugin_path") or "").endswith(
                 "plugins/hermes-vcc-cn/plugin.yaml"),
             str((nb.get("error") or {}).get("plugin_path")))
    sb_check("★409 时 config.yaml 一个字节都没写（sha256 前后一致）",
             hashlib.sha256(beta_cfg.read_bytes()).hexdigest() == beta_before,
             "before=%s… after=%s…" % (beta_before[:12],
                                       hashlib.sha256(beta_cfg.read_bytes()).hexdigest()[:12]))
    sb_check("回读仍是 compressor（没被偷改）",
             read_engine_independently(beta_cfg) == SANDBOX_DEFAULT_ENGINE,
             str(read_engine_independently(beta_cfg)))
    builtin_ok = client.post(PREFIX + "/vcc/engine",
                             json={"agent": "beta", "engine": "compressor", "confirm": True})
    sb_check("★内置 compressor 不需要装插件 ⇒ 仍 200（守卫没把内置引擎也拦掉）",
             builtin_ok.status_code == 200, redacted(builtin_ok))
    ov = client.get(PREFIX + "/vcc/overview", params={"recent": 1}).json()
    rows = {row.get("agent"): row for row in (ov.get("profiles") or [])}
    print("        沙盒 overview:", {k: (v.get("engine_installed"), v.get("engine_version_actual"),
                                       v.get("engine_mismatch")) for k, v in rows.items()})
    sb_check("overview 每行带 engine_installed / engine_version_actual / engine_mismatch",
             bool(rows) and all(
                 "engine_installed" in row and "engine_version_actual" in row
                 and "engine_mismatch" in row for row in rows.values()),
             str(list(rows)[:3]))
    sb_check("★沙盒里没装引擎 ⇒ engine_installed=false 且 version 为空（不回填全局版本）",
             bool(rows) and all(row.get("engine_installed") is False
                                and row.get("engine_version_actual") == ""
                                and row.get("engine_mismatch") is True for row in rows.values()),
             str({k: (v.get("engine_installed"), v.get("engine_version_actual"))
                  for k, v in rows.items()}))

    print("\n[S-4] 沙盒写路径没有跑到真实 home")
    sb_check("真实 home 里没有被创建 profiles/alpha",
             not (REAL_HOME / "profiles" / "alpha").exists(),
             str(REAL_HOME / "profiles" / "alpha"))

    print("\n  --- 沙盒阶段：PASS %d / FAIL %d ---" % (passed, len(fails)))
    return 1 if fails else 0


# ===========================================================================
# 主进程阶段：真实 home（只读）+ 全量对账
# ===========================================================================
def main() -> int:
    global passed
    print("[0] 环境")
    print("        python   :", sys.version.split()[0])
    print("        plugin   :", PLUGIN_ROOT)
    print("        real home:", REAL_HOME)
    print("        HERMES_HOME(env):", os.environ.get("HERMES_HOME") or "<unset>")

    if not (DASHBOARD / "plugin_api.py").is_file():
        print("  FAIL  插件根不对：找不到", DASHBOARD / "plugin_api.py")
        return 2

    for extra in (str(DASHBOARD), str(HERMES_AGENT)):
        if extra not in sys.path:
            sys.path.insert(0, extra)

    # -----------------------------------------------------------------------
    # [7-前置] 生产零污染基线：所有**可能被写**的 config.yaml 先算 sha256
    # -----------------------------------------------------------------------
    def fingerprint() -> dict[str, str]:
        paths = [REAL_HOME / "config.yaml"]
        profiles = REAL_HOME / "profiles"
        if profiles.is_dir():
            paths.extend(sorted(profiles.glob("*/config.yaml")))
        return {str(p): sha256_of(p) for p in paths}

    print("\n[7-前置] 生产指纹（开头）")
    before = fingerprint()
    for path, digest in before.items():
        print("        %s  %s" % (digest[:16], path.replace("\\", "/")))
    check("指纹覆盖到 9 个 profile config.yaml（根 + 8 个）", len(before) == 9, "%d 个" % len(before))
    dirs_before = sorted(p.name for p in (REAL_HOME / "profiles").iterdir() if p.is_dir())

    # -----------------------------------------------------------------------
    print("\n[1] 按 dashboard 的方式加载 plugin_api，检查 6 条路由（路径逐字）")
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_hermes_console", DASHBOARD / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    print("        failed  :", brief(mod.DOMAIN_FAILURES, 300))
    check("/health 口径：没有域模块加载失败（failed={}）", mod.DOMAIN_FAILURES == {},
          brief(mod.DOMAIN_FAILURES, 300))
    check("api_vcc 在已加载清单里", "api_vcc" in mod.DOMAIN_LOADED, str(mod.DOMAIN_LOADED))

    registered = set()
    for route in mod.router.routes:
        for method in sorted(getattr(route, "methods", []) or []):
            registered.add((method, route.path))

    for method, path in EXPECTED_ROUTES:
        check("路由已注册 %-5s %s（逐字）" % (method, path), (method, path) in registered,
              "" if (method, path) in registered else "缺这一条")
    # 反向：别家同名路径 / 大小写变体不该出现
    for method, path in (("GET", "/vcc/OverView"), ("GET", "/vcc/overviews"),
                         ("POST", "/vcc/engine_status"), ("GET", "/vcc/engine")):
        check("没有多余/写错的路由 %-5s %s" % (method, path), (method, path) not in registered)

    from fastapi import FastAPI  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402

    app = FastAPI()
    app.include_router(mod.router, prefix=PREFIX)
    client = TestClient(app, raise_server_exceptions=False)

    import api_vcc  # noqa: E402  (plugin_api 已按本目录 import 过同名模块)

    health = client.get(PREFIX + "/health").json()
    print("        /health :", brief({k: health.get(k) for k in ("ok", "failed", "route_count")}, 240))
    check("路由总数 >= %d（并行卡片会继续加，这里只做回归下限）" % EXPECTED_ROUTE_TOTAL,
          isinstance(health.get("route_count"), int)
          and health.get("route_count") >= EXPECTED_ROUTE_TOTAL,
          str(health.get("route_count")))
    check("/health 的 failed 为空", health.get("failed") == {}, brief(health.get("failed")))
    check("vcc 分区挂在 /health 的 loaded 里（api_vcc）", "api_vcc" in (health.get("loaded") or []),
          brief(health.get("loaded"), 400))

    # -----------------------------------------------------------------------
    print("\n[2] ★GET /vcc/overview（真实 home，只读）")
    resp = client.get(PREFIX + "/vcc/overview")
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    check("HTTP 200", resp.status_code == 200, "" if resp.status_code == 200 else redacted(resp))
    check("ok=true", body.get("ok") is True, brief(body.get("ok")))
    rows = body.get("profiles") or []
    print("        count   :", body.get("count"), "| engine_target:", body.get("engine_target"))
    print("        agents  :", ", ".join(r.get("agent", "?") for r in rows))
    check("payload 含 profiles / count / engine_target / engine_plugin / note",
          all(k in body for k in ("profiles", "count", "engine_target", "engine_plugin", "note")),
          str(sorted(body.keys())))
    check("profiles 是非空列表（真实 home 有 9 个 agent）", isinstance(rows, list) and len(rows) == 9,
          "%d 行" % len(rows))
    check("每行都有 agent / engine / engine_source / config_path / summaries",
          all(all(k in r for k in ("agent", "engine", "engine_source", "config_path", "summaries"))
              for r in rows), str(len(rows)) + " 行")
    check("count == len(profiles)", body.get("count") == len(rows), str(body.get("count")))

    # -----------------------------------------------------------------------
    print("\n[2b] ★★引擎版本按 agent 解析：面板每行 == 该 agent 自己 plugins 目录的 plugin.yaml")
    print("        %-16s %-14s %-14s %-8s %-9s %-9s %s"
          % ("agent", "面板(api)", "磁盘实际", "一致", "installed", "mismatch", "plugin.yaml"))
    drift = []
    disk_diff = []
    for row in rows:
        agent = row["agent"]
        home = REAL_HOME if agent == "default" else REAL_HOME / "profiles" / agent
        meta_path = home / "plugins" / "hermes-vcc-cn" / "plugin.yaml"
        disk_version = read_plugin_version_independently(meta_path)
        same = (disk_version == row.get("engine_version_actual"))
        print("        %-16s %-14s %-14s %-8s %-9s %-9s %s"
              % (agent, row.get("engine_version_actual") or "(none)", disk_version or "(none)",
                 "OK" if same else "DIFF", row.get("engine_installed"),
                 row.get("engine_mismatch"), meta_path))
        if not same:
            disk_diff.append(agent)
        if row.get("engine_mismatch"):
            drift.append("%s=%s" % (agent, row.get("engine_version_actual") or "未安装"))
    print("        全局版本: %s | 漂移: %s" % (body.get("engine_version_global"), ", ".join(drift) or "无"))
    check("★9 个 agent 的 engine_version_actual 全部 == 磁盘 plugin.yaml 实际版本",
          not disk_diff, "不一致: " + str(disk_diff))
    check("overview 带 engine_installed / engine_version_actual / engine_mismatch / engine_version_global",
          all(all(k in r for k in ("engine_installed", "engine_version_actual",
                                   "engine_mismatch", "engine_version_global")) for r in rows),
          str(sorted(rows[0].keys())) if rows else "no rows")
    check("★只装了旧版的 agent 被标成 mismatch（不是全绿）",
          bool(drift), "漂移: " + (", ".join(drift) or "无"))
    check("engine_mismatch 与「版本 != 全局」同口径",
          all((r.get("engine_version_actual") != body.get("engine_version_global"))
              == bool(r.get("engine_mismatch")) for r in rows if r.get("engine_installed")),
          "global=" + str(body.get("engine_version_global")))
    check("顶层的 engine_mismatch_count == 行内统计",
          body.get("engine_mismatch_count") == sum(1 for r in rows if r.get("engine_mismatch")),
          str(body.get("engine_mismatch_count")))

    # -----------------------------------------------------------------------
    print("\n[3] ★★GET /vcc/engine_status：引擎名 == 该 profile 自己的 context.engine")
    print("        %-16s %-16s %-16s %-8s %s" % ("agent", "engine_status", "config.yaml", "source", "一致"))
    mismatched = []
    for row in rows:
        agent = row["agent"]
        st = client.get(PREFIX + "/vcc/engine_status", params={"agent": agent})
        sb = st.json() if st.status_code == 200 else {}
        cfg_path = Path(sb.get("config_path") or row.get("config_path") or "")
        independent = read_engine_independently(cfg_path) if cfg_path.is_file() else None
        same = (st.status_code == 200 and independent is not None
                and sb.get("engine") == independent)
        print("        %-16s %-16s %-16s %-8s %s"
              % (agent, sb.get("engine"), independent, sb.get("engine_source"),
                 "OK" if same else "MISMATCH"))
        if not same:
            mismatched.append(agent)
    check("9 个 agent 的 engine_status.engine 全部 == 独立解析的 config.yaml context.engine",
          not mismatched, "不一致: " + str(mismatched))
    check("engine_status 带 config_path / engine_source / engine_target / is_target",
          all(k in (client.get(PREFIX + "/vcc/engine_status",
                               params={"agent": "default"}).json())
              for k in ("config_path", "engine_source", "engine_target", "is_target")))
    check("overview 每行的 engine 与 engine_status 同源一致",
          all(client.get(PREFIX + "/vcc/engine_status",
                         params={"agent": r["agent"]}).json().get("engine") == r["engine"] for r in rows))

    # -----------------------------------------------------------------------
    print("\n[4] ★POST /vcc/engine 缺 confirm=true ⇒ 400 + {\"ok\":false,\"error\":{...}}")
    current_engine = client.get(PREFIX + "/vcc/engine_status",
                                params={"agent": "default"}).json().get("engine")
    # 故意传**当前值**：万一确认检查失效，写入也不会改变真实配置。
    no_confirm = client.post(PREFIX + "/vcc/engine",
                             json={"agent": "default", "engine": current_engine})
    print("        缺 confirm     :", redacted(no_confirm))
    check("缺 confirm ⇒ 400", no_confirm.status_code == 400, redacted(no_confirm))
    check("响应体形状是 {\"ok\":false,\"error\":{code,message,...}}", error_shape_ok(no_confirm.json()),
          redacted(no_confirm))
    check("error.code == confirmation_required",
          (no_confirm.json().get("error") or {}).get("code") == "confirmation_required", redacted(no_confirm))
    check("缺 confirm 时**一个字节都没写**（真实 config.yaml 哈希不变）",
          sha256_of(REAL_HOME / "config.yaml") == before[str(REAL_HOME / "config.yaml")])
    no_confirm2 = client.post(PREFIX + "/vcc/engine",
                              json={"agent": "default", "engine": current_engine, "confirm": False})
    print("        confirm=false  :", redacted(no_confirm2))
    check("confirm=false 同样 400（只有字面 true 才放行）", no_confirm2.status_code == 400,
          redacted(no_confirm2))
    bad_engine = client.post(PREFIX + "/vcc/engine",
                             json={"agent": "default", "engine": "totally-not-an-engine", "confirm": True})
    print("        非法 engine    :", redacted(bad_engine))
    check("非法 engine ⇒ 400 invalid_argument（不是 500、不写盘）",
          bad_engine.status_code == 400
          and (bad_engine.json().get("error") or {}).get("code") == "invalid_argument",
          redacted(bad_engine))

    # -----------------------------------------------------------------------
    print("\n[6] GET /vcc/log 与 /vcc/compaction 结构合法（真实 home）")
    log_resp = client.get(PREFIX + "/vcc/log", params={"agent": "default", "limit": 20})
    log_body = log_resp.json()
    print("        /vcc/log ->", log_resp.status_code,
          brief({k: log_body.get(k) for k in ("ok", "agent", "limit", "count")}))
    check("HTTP 200", log_resp.status_code == 200, "" if log_resp.status_code == 200 else redacted(log_resp))
    check("结构合法：ok / agent / limit / count / lines 齐备且 lines 是 list",
          log_body.get("ok") is True and isinstance(log_body.get("lines"), list)
          and all(k in log_body for k in ("agent", "limit", "count")),
          brief({k: log_body.get(k) for k in ("ok", "agent", "limit", "count")}))
    check("count == len(lines) 且不超过 limit",
          log_body.get("count") == len(log_body.get("lines") or [])
          and len(log_body.get("lines") or []) <= 20,
          "%s / %s" % (log_body.get("count"), len(log_body.get("lines") or [])))
    if log_body.get("lines"):
        print("        首行样例:", str(log_body["lines"][0])[:150])
    check("有数据时 200 且带真实原始行（不是空态伪装）", bool(log_body.get("lines")),
          "%d 行" % len(log_body.get("lines") or []))
    bad_log = client.get(PREFIX + "/vcc/log", params={"agent": "nope-not-an-agent"})
    print("        未知 agent     :", redacted(bad_log))
    check("未知 agent ⇒ 结构化 404（不是 500）",
          bad_log.status_code == 404 and error_shape_ok(bad_log.json()), redacted(bad_log))

    # compaction：从 overview 的 summaries 里取一条真实 message_id
    target = None
    for row in rows:
        for summary in (row.get("summaries") or []):
            if summary.get("message_id") is not None:
                target = (row["agent"], int(summary["message_id"]))
                break
        if target:
            break
    if target:
        agent, message_id = target
        comp = client.get(PREFIX + "/vcc/compaction",
                          params={"agent": agent, "message_id": message_id, "sample": 20})
        comp_body = comp.json()
        print("        /vcc/compaction(%s#%s) -> %s" % (agent, message_id, comp.status_code))
        print("        顶层键   :", str(sorted(comp_body.keys()))[:300])
        check("有摘要时 /vcc/compaction HTTP 200", comp.status_code == 200,
              "" if comp.status_code == 200 else redacted(comp))
        check("结构合法：ok=true 且含 summary / window / kind / cited / ratio_pct 等栏目",
              comp_body.get("ok") is True
              and all(k in comp_body for k in ("agent", "summary", "window", "kind",
                                               "cited", "ratio_pct", "sampled_rows")),
              brief(sorted(comp_body.keys()), 300))
    else:
        print("        真实 home 里没有可用的摘要 message_id —— 改验空态路径")

    # 空态：不存在的 message_id ⇒ 结构化 4xx（不是 500）
    empty_comp = client.get(PREFIX + "/vcc/compaction", params={"agent": "default", "message_id": 1})
    print("        /vcc/compaction(不存在的 id) ->", redacted(empty_comp))
    check("不存在的 message_id ⇒ 结构化 4xx（空态不是 500）",
          empty_comp.status_code in (400, 404) and error_shape_ok(empty_comp.json()),
          redacted(empty_comp))

    search_empty = client.post(PREFIX + "/vcc/search_archived",
                               json={"agent": "default", "query": "   "})
    print("        /vcc/search_archived(空词) ->", redacted(search_empty))
    check("空关键词 ⇒ 结构化 400（不是 500）",
          search_empty.status_code == 400 and error_shape_ok(search_empty.json()),
          redacted(search_empty))

    # -----------------------------------------------------------------------
    print("\n[7] ★4xx 不打印响应体全文（凭据泄露防护）")
    # 造一个带哨兵串的非法请求：真实响应体里会回显这个串，打印口径必须把它吃掉。
    # ★哨兵必须是**非法 agent id**（带空格与感叹号）：合法 id 会走"本来就是 compressor
    #   （未改动）"的短路返回 200，既拿不到 4xx，也证明不了打印口径。
    sentinel = "SENTINEL SECRET 9f3a1c!DO-NOT-LEAK"
    leak_probe = client.post(PREFIX + "/vcc/engine",
                             json={"agent": sentinel, "engine": "compressor", "confirm": True})
    printed = redacted(leak_probe)
    print("        打印口径输出 :", printed)
    check("非法 agent ⇒ 400（4xx 路径真的走到了）", leak_probe.status_code == 400, printed)
    check("★哨兵串**没有**出现在打印输出里（不打印请求体/响应体全文）", sentinel not in printed, printed)
    check("打印输出里只有状态码与 error.code（无 message / 无 JSON 大括号）",
          "{" not in printed and "message" not in printed and "400" in printed, printed)
    check("响应体里确实**含有**哨兵串（证明这条断言不是空转）",
          sentinel in json.dumps(leak_probe.json(), ensure_ascii=False))
    # 断言失败路径同样不能泄漏：把 check 的 detail 换成 redacted 后仍不含哨兵
    check("失败分支的 detail 也走同一口径（redacted() 是唯一出口）",
          sentinel not in redacted(leak_probe))

    # -----------------------------------------------------------------------
    print("\n[5] ★沙盒 home：POST /vcc/engine 带 confirm=true 真实写入并回读（子进程）")
    sandbox = Path(tempfile.mkdtemp(prefix="vcc-probe-sandbox-"))
    try:
        env = dict(os.environ)
        env["HERMES_HOME"] = str(sandbox)   # 子进程里 import 之前就生效
        env.pop("PYTHONPATH", None)
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                               "--sandbox-phase", str(sandbox)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=300)
        out = proc.stdout or ""
        sys.stdout.write(out)
        if proc.stderr.strip():
            print("        [stderr]", proc.stderr.strip()[:400])
        for line in out.splitlines():
            if line.startswith("  PASS  "):
                passed += 1
            elif line.startswith("  FAIL  "):
                failures.append("[sandbox] " + line.strip()[8:])
        check("沙盒子进程退出码 0（写入+回读全绿）", proc.returncode == 0, "exit=%d" % proc.returncode)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    # -----------------------------------------------------------------------
    print("\n[7-收尾] ★生产零污染（结尾指纹 vs 开头指纹）")
    after = fingerprint()
    dirty = []
    for path, digest in before.items():
        now = after.get(path)
        mark = "same" if now == digest else "CHANGED"
        if now != digest:
            dirty.append(path)
        print("        %-8s %s  %s" % (mark, (now or "<missing>")[:16], path.replace("\\", "/")))
    check("9 个 config.yaml 一个字节都没变（真实 home 零污染）", not dirty, "变了: " + str(dirty))
    dirs_after = sorted(p.name for p in (REAL_HOME / "profiles").iterdir() if p.is_dir())
    check("profiles/ 下没有新增目录（本探针没在真实 home 造 profile）",
          dirs_after == dirs_before, "新增: " + str(set(dirs_after) - set(dirs_before)))

    # -----------------------------------------------------------------------
    print()
    total = passed + len(failures)
    print("PASS=%d FAIL=%d TOTAL=%d" % (passed, len(failures), total))
    if failures:
        print("VCC-PROBE-FAILED（%d 项）" % len(failures))
        for label in failures:
            print("  -", label)
        return 1
    print("VCC-PROBE-OK")
    return 0


if __name__ == "__main__":
    if "--sandbox-phase" in sys.argv:
        idx = sys.argv.index("--sandbox-phase")
        raise SystemExit(sandbox_phase(Path(sys.argv[idx + 1]).resolve()))
    raise SystemExit(main())

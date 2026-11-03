# -*- coding: utf-8 -*-
"""hermes-console · V4「远端后端」分区（remote）验收探针（唯一验收依据）。

跑法（**不要经管道拿退出码**）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \\
        plugins/hermes-console/scripts/probe_remote.py
    echo "exit=$?"        # bash；PowerShell 用 $LASTEXITCODE

覆盖卡片 8 条::

    [1] 6 条路由全部注册，前缀逐字正确
        GET  /remote/agents          GET  /remote/status
        POST /remote/agent/start     POST /remote/agent/stop
        GET  /remote/credentials     POST /remote/credentials/reveal
    [2] ★★agent 列表**动态**：GET /remote/agents 的 id 集合 == core.scope.list_agents()
        的结果，且数量 ≥ 9（default + 8 个 profile）
    [3] ★★列表里同时含 default 与至少一个非 default（如 xiaomo），且**不止**旧插件写死的那 4 个
        （{default, xiaomo, work, worker}）——这条专抓 agent 硬编码 bug
    [4] GET /status 200，且**每个** agent 都有状态字段（state / running / pid / port /
        started_at / uptime_seconds / credentials）
    [5] ★POST /agent/start 对**不存在的 agent** 返回 400（不是 500），
        且**不产生任何副作用**（启动器一次都没被调用、真实 home 目录内没有新增文件）
    [6] ★GET /credentials 响应里**不含任何真密钥**：不出现 ``sk-``、
        不含长度 >20 的疑似 token 串、**不含**真实 ``.env`` 里任何一个取值
    [7] ★生产零污染：开头/结尾各算真实 ``config.yaml`` + ``profiles/*/config.yaml`` 的
        sha256，断言全一致
    [8] ★不许真启停任何 agent：本探针把 ``api_remote._run_script`` 换成一个**一调就炸**的
        哨兵，主进程与沙盒阶段都装上；收尾断言调用次数 == 0

沙盒阶段（``--sandbox-phase``，跑在**子进程**里）额外证明三件事：

* 沙盒里造 3 个 profile ⇒ 列表就是 4 项；**再新建一个 profile ⇒ 下一次请求立刻多一项**，
  删掉又少一项（证明是每次现扫，不是启动时快照，更不是写死名单）；
* ``dry_run=true`` 的启停：200 + 回执里有 argv，且启动器**没被调用**；
* 沙盒 ``.env`` 里塞**已知哨兵明文密码**（``sk-sandbox-…``），``GET /credentials``
  必须一个字节都不回显 —— 这条比"在真实 home 上碰运气"硬得多。

``HERMES_HOME`` 必须在 ``hermes_cli`` / ``core.scope`` 被 import **之前**设好，所以沙盒阶段
不图省事地和主阶段同进程；主阶段全程只读（GET + 必定被拒的 POST + dry_run POST）。
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
REMOTE_PLUGIN = REAL_HOME / "plugins" / "hermes-remote"
PREFIX = "/api/plugins/hermes-console"

#: 本卡必须挂上的 6 条路由（方法 + 路径，路径相对 PREFIX，逐字比对）。
EXPECTED_ROUTES = (
    ("GET", "/remote/agents"),
    ("GET", "/remote/status"),
    ("POST", "/remote/agent/start"),
    ("POST", "/remote/agent/stop"),
    ("GET", "/remote/credentials"),
    ("POST", "/remote/credentials/reveal"),
)
#: 绝不能出现的写法（大小写 / 复数 / 方法错配）。
FORBIDDEN_ROUTES = (
    ("GET", "/remote/Agents"),
    ("GET", "/remote/agent"),
    ("POST", "/remote/agents"),
    ("GET", "/remote/agent/start"),
    ("POST", "/remote/credential"),
    ("GET", "/remote/start"),
)

#: 路由总数**下界**：V3 之后 53，V4 远端后端 +5 = 58，V5 模型分区 +13 = 71，
#: V4b 远端后端加 /remote/credentials/reveal +1 = 72。
#:
#: 为什么只守下界：同一个插件里还有别的卡在并行加路由（overview / settings 都在动），
#: 钉死一个精确数字会让"别人加路由"变成"这张卡红"。精确归属由
#: ``/remote/* 下恰好 6 条`` 这条来守 —— 那才是本卡的路由面。
ROUTE_TOTAL_MIN = 72
#: 真实 home 上的 agent 数量下限（default + 8 个 profile）。
MIN_AGENTS = 9
#: 旧插件 `plugin_api.py` 里写死的那 4 个名字（本卡要证明列表**不止**这些）。
OLD_HARDCODED_IDS = frozenset({"default", "xiaomo", "work", "worker"})

#: 沙盒 fixture。
SANDBOX_PROFILES = ("alpha", "beta", "gamma")
SANDBOX_EXTRA_PROFILE = "delta"
#: ★哨兵：这些串一旦出现在 /credentials 响应里就是泄漏。
SANDBOX_USERNAME = "sandboxuser"
SANDBOX_PASSWORD = "sk-sandbox-DEADBEEF0123456789abcdef"
SANDBOX_SECRET = "sandbox-secret-9f3a1c2b4d5e6f708192a3b4"

#: 疑似 token：长度 >20 的连续"token 字符"串。
SUSPICIOUS_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{21,}")
#: 凭据键名前缀：响应里出现的是**键名**（不是取值），不算泄漏。
KEY_NAME_PREFIX = "HERMES_DASHBOARD_BASIC_AUTH_"

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


def brief(value, n: int = 240) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    return text if len(text) <= n else text[:n] + "…"


def redacted(resp) -> str:
    """★失败时只打印状态码与 ``error.code``（4xx 体里可能有上游路径 / 原话）。"""
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


def secret_values() -> dict[str, str]:
    """真实 ``.env`` 里的凭据取值（只用于"不许出现在响应里"的比对，**从不打印**）。"""
    found: dict[str, str] = {}
    paths = [REAL_HOME / ".env"]
    profiles = REAL_HOME / "profiles"
    if profiles.is_dir():
        paths.extend(sorted(profiles.glob("*/.env")))
    keys = (KEY_NAME_PREFIX + "USERNAME", KEY_NAME_PREFIX + "PASSWORD",
            KEY_NAME_PREFIX + "PASSWORD_HASH", KEY_NAME_PREFIX + "SECRET")
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
            name = name.strip()
            value = value.strip().strip("\"'")
            if name in keys and len(value) >= 4:
                found[name + "@" + path.name + "@" + path.parent.name] = value
    return found


def read_dotenv(path: Path) -> dict[str, str]:
    """极简 ``.env`` 解析（KEY=VALUE，忽略注释/空行，去掉成对引号）。"""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[name.strip()] = value
    return out


def merged_env(agent: str) -> dict[str, str]:
    """顶层 ``.env`` 打底、该 agent 的 ``.env`` 覆盖（与 Hermes 的 profile 优先级一致）。"""
    merged = read_dotenv(REAL_HOME / ".env")
    if agent != "default":
        merged.update(read_dotenv(REAL_HOME / "profiles" / agent / ".env"))
    return merged


def tree_snapshot(root: Path) -> dict[str, str]:
    """目录树的 (相对路径 → sha256) 快照；用来断言"没有副作用"。

    **跳过日志文件**：``serve.log`` 是真实运行中的后端进程自己追加的，与本探针无关，
    把它算进来会让"零副作用"断言变成看运气。
    """
    snapshot: dict[str, str] = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if path.name == "serve.log" or path.suffix == ".log":
            continue
        snapshot[str(path.relative_to(root)).replace("\\", "/")] = sha256_of(path)
    return snapshot


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


# ===========================================================================
# 子进程阶段：沙盒 home（HERMES_HOME 在任何 import 之前设好）
# ===========================================================================
def sandbox_phase(sandbox: Path) -> int:
    shutil.rmtree(sandbox, ignore_errors=True)
    (sandbox / "profiles").mkdir(parents=True)
    (sandbox / "config.yaml").write_text("plugins: {}\n", encoding="utf-8")
    # 顶层 .env：凭据只配在这里 ⇒ 所有 agent 都是"共享凭据"
    (sandbox / ".env").write_text(
        "%sUSERNAME=%s\n%sPASSWORD=%s\n%sSECRET=%s\n"
        % (KEY_NAME_PREFIX, SANDBOX_USERNAME, KEY_NAME_PREFIX, SANDBOX_PASSWORD,
           KEY_NAME_PREFIX, SANDBOX_SECRET),
        encoding="utf-8")
    for name in SANDBOX_PROFILES:
        profile = sandbox / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text("plugins: {}\n", encoding="utf-8")
        (profile / "profile.yaml").write_text("title: 沙盒%s\n" % name, encoding="utf-8")

    os.environ["HERMES_HOME"] = str(sandbox)
    module = load_plugin_api()
    import api_remote  # noqa: E402  (plugin_api 已按本目录 import 过同名模块)

    api_remote._home = lambda: sandbox          # 唯一漏斗：整个分区指向沙盒
    calls: list = []

    def _forbidden_spawn(*args, **kwargs):
        calls.append(args)
        raise AssertionError("探针禁止真实调用启动器：%r" % (args,))

    api_remote._run_script = _forbidden_spawn    # ★[8] 一调就炸

    from fastapi import FastAPI  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402

    app = FastAPI()
    app.include_router(module.router, prefix=PREFIX)
    client = TestClient(app, raise_server_exceptions=False)

    def sb_check(label: str, ok: bool, detail: str = "") -> bool:
        return check("[sandbox] " + label, ok, detail)

    print("\n[S-1] ★★沙盒 home：agent 列表跟着磁盘现走（不是启动时快照）")
    print("        沙盒 home :", sandbox)
    first = client.get(PREFIX + "/remote/agents")
    fbody = first.json()
    ids = [row.get("agent") for row in fbody.get("agents") or []]
    expected = ["default", *SANDBOX_PROFILES]
    print("        agents    :", ids, "| count =", fbody.get("count"))
    sb_check("GET /remote/agents 200", first.status_code == 200,
             "" if first.status_code == 200 else redacted(first))
    sb_check("列表 = default + 3 个沙盒 profile（%s）" % ",".join(expected), ids == expected, str(ids))
    sb_check("count == len(agents)", fbody.get("count") == len(ids), str(fbody.get("count")))

    extra = sandbox / "profiles" / SANDBOX_EXTRA_PROFILE
    extra.mkdir(parents=True)
    (extra / "config.yaml").write_text("plugins: {}\n", encoding="utf-8")
    (extra / "profile.yaml").write_text("title: 沙盒新 agent\n", encoding="utf-8")
    grown = client.get(PREFIX + "/remote/agents").json()
    grown_ids = [row.get("agent") for row in grown.get("agents") or []]
    print("        新建 %s 后:" % SANDBOX_EXTRA_PROFILE, grown_ids)
    sb_check("★新建 profile 后**下一次请求**就多一项（每次现扫，不是写死、不是快照）",
             SANDBOX_EXTRA_PROFILE in grown_ids and len(grown_ids) == len(ids) + 1,
             str(grown_ids))
    shutil.rmtree(extra, ignore_errors=True)
    shrunk = client.get(PREFIX + "/remote/agents").json()
    shrunk_ids = [row.get("agent") for row in shrunk.get("agents") or []]
    print("        删掉后    :", shrunk_ids)
    sb_check("★删掉 profile 后下一次请求就少一项（动态的双向）",
             shrunk_ids == ids, str(shrunk_ids))

    print("\n[S-2] ★[8] 启停 dry_run：只回执，不 spawn")
    before_tree = tree_snapshot(sandbox)
    plan = client.post(PREFIX + "/remote/agent/start",
                       json={"agent": "alpha", "dry_run": True})
    pbody = plan.json()
    print("        dry_run   :", plan.status_code, brief({k: pbody.get(k) for k in
                                                          ("ok", "dry_run", "agent", "port")}))
    sb_check("dry_run 的 start 200", plan.status_code == 200,
             "" if plan.status_code == 200 else redacted(plan))
    sb_check("回执 dry_run=true / agent=alpha / 带 argv 与脚本路径",
             pbody.get("dry_run") is True and pbody.get("agent") == "alpha"
             and isinstance(pbody.get("argv"), list) and pbody.get("script"),
             brief(pbody, 200))
    sb_check("argv 里的动作是 start（真的委托给旧启动器，只是没执行）",
             (pbody.get("argv") or [])[1:2] == ["start"], brief(pbody.get("argv")))
    stop_plan = client.post(PREFIX + "/remote/agent/stop",
                           json={"agent": "beta", "dry_run": True})
    sbody = stop_plan.json()
    sb_check("dry_run 的 stop 200 且 argv 动作是 stop",
             stop_plan.status_code == 200 and (sbody.get("argv") or [])[1:2] == ["stop"],
             redacted(stop_plan) if stop_plan.status_code != 200 else brief(sbody.get("argv")))
    sb_check("★启动器**一次都没被调用**（_run_script 哨兵）", calls == [], str(calls))
    sb_check("dry_run 前后沙盒目录逐字节相同（真的一步都没动）",
             tree_snapshot(sandbox) == before_tree)

    print("\n[S-3] ★[5] 不存在的 agent ⇒ 400（不是 500），且零副作用")
    before_tree = tree_snapshot(sandbox)
    bad = client.post(PREFIX + "/remote/agent/start", json={"agent": "no-such-agent-xyz"})
    print("        start(不存在) :", redacted(bad))
    sb_check("HTTP 400（不是 500）", bad.status_code == 400, redacted(bad))
    sb_check("响应体是 {ok:false,error:{code,message}}", error_shape_ok(bad.json()), redacted(bad))
    sb_check("error.code == invalid_argument",
             (bad.json().get("error") or {}).get("code") == "invalid_argument", redacted(bad))
    sb_check("错误体里带当前合法 agent 列表",
             (bad.json().get("error") or {}).get("agents") == expected,
             brief((bad.json().get("error") or {}).get("agents")))
    sb_check("★启动器仍未被调用（非法输入连 spawn 都到不了）", calls == [], str(calls))
    sb_check("沙盒目录零变化（没有任何文件被写/删）", tree_snapshot(sandbox) == before_tree)
    bad_stop = client.post(PREFIX + "/remote/agent/stop", json={"agent": "no-such-agent-xyz"})
    sb_check("stop(不存在) 同样 400 且未调用启动器",
             bad_stop.status_code == 400 and calls == [], redacted(bad_stop))
    illegal = client.post(PREFIX + "/remote/agent/start", json={"agent": "../../etc"})
    sb_check("非法 agent id（../../etc）⇒ 400 且不 spawn",
             illegal.status_code == 400 and calls == [], redacted(illegal))

    print("\n[S-4] ★[6] 沙盒 .env 里塞了已知明文哨兵 ⇒ /credentials 一个字节都不许回显")
    cred_all = client.get(PREFIX + "/remote/credentials")
    cred_text = json.dumps(cred_all.json(), ensure_ascii=False)
    cred_one = client.get(PREFIX + "/remote/credentials", params={"agent": "alpha"})
    one_text = json.dumps(cred_one.json(), ensure_ascii=False)
    print("        /credentials(alpha) 状态 :", cred_one.status_code,
          "| configured =", (cred_one.json() or {}).get("configured"))
    sb_check("/credentials 与 /credentials?agent=alpha 都 200",
             cred_all.status_code == 200 and cred_one.status_code == 200,
             redacted(cred_one))
    sb_check("★哨兵密码 %r **没有**出现在响应里" % SANDBOX_PASSWORD[:12] + "…",
             SANDBOX_PASSWORD not in cred_text and SANDBOX_PASSWORD not in one_text)
    sb_check("★哨兵 SECRET 没有出现在响应里", SANDBOX_SECRET not in cred_text
             and SANDBOX_SECRET not in one_text)
    sb_check("★哨兵用户名也没有出现在响应里（用户名同属凭据）",
             SANDBOX_USERNAME not in cred_text and SANDBOX_USERNAME not in one_text)
    sb_check("响应里不含 'sk-'", "sk-" not in cred_text and "sk-" not in one_text)
    sb_check("响应里的取值全是掩码 ***（或空）",
             all(item.get("value") in ("***", "") for item in (cred_one.json().get("keys") or [])),
             brief([i.get("value") for i in (cred_one.json().get("keys") or [])], 120))
    sb_check("但**配置状态**是真的：configured=true 且 status=已配置",
             cred_one.json().get("configured") is True
             and cred_one.json().get("status") == "已配置",
             brief({k: cred_one.json().get(k) for k in ("configured", "status")}))

    print("\n[S-5] ★V4b：账号明文可复制 / 密码默认不出 / 显式 reveal 才给（且不落日志）")
    agents_text = json.dumps(client.get(PREFIX + "/remote/agents").json(), ensure_ascii=False)
    sb_check("★/remote/agents 带账号名（界面要显示并一键复制它）",
             SANDBOX_USERNAME in agents_text)
    sb_check("★/remote/agents 里**没有**密码（默认遮蔽，密码类字段一个都不出现）",
             SANDBOX_PASSWORD not in agents_text)
    sb_check("★/remote/agents 里没有 SECRET", SANDBOX_SECRET not in agents_text)
    no_confirm = client.post(PREFIX + "/remote/credentials/reveal",
                             json={"agent": "alpha", "field": "password"})
    sb_check("reveal 缺 confirm ⇒ 400（没人点按钮就拿不到明文）",
             no_confirm.status_code == 400, redacted(no_confirm))
    string_confirm = client.post(PREFIX + "/remote/credentials/reveal",
                                 json={"agent": "alpha", "field": "password", "confirm": "true"})
    sb_check("confirm 只认布尔 true（字符串 \"true\" 也拒）⇒ 400",
             string_confirm.status_code == 400, redacted(string_confirm))
    bad_field = client.post(PREFIX + "/remote/credentials/reveal",
                            json={"agent": "alpha", "field": "nope", "confirm": True})
    sb_check("reveal 非白名单 field ⇒ 400（附合法字段表）",
             bad_field.status_code == 400
             and isinstance((bad_field.json().get("error") or {}).get("allowed_fields"), list),
             redacted(bad_field))
    unknown_reveal = client.post(PREFIX + "/remote/credentials/reveal",
                                 json={"agent": "no-such-agent-xyz", "field": "password", "confirm": True})
    sb_check("reveal 未知 agent ⇒ 404（不是 500）",
             unknown_reveal.status_code == 404, redacted(unknown_reveal))
    before_reveal = tree_snapshot(sandbox)
    shown = client.post(PREFIX + "/remote/credentials/reveal",
                        json={"agent": "alpha", "field": "password", "confirm": True})
    sb_check("reveal(password) 200 且取到的就是 .env 里的那个值",
             shown.status_code == 200 and shown.json().get("value") == SANDBOX_PASSWORD,
             redacted(shown) if shown.status_code != 200 else "值已比对（不打印）")
    sb_check("reveal 响应带 Cache-Control: no-store（明文不进任何缓存）",
             str(shown.headers.get("cache-control") or "") == "no-store",
             str(shown.headers.get("cache-control")))
    shown_user = client.post(PREFIX + "/remote/credentials/reveal",
                             json={"agent": "alpha", "field": "username", "confirm": True})
    sb_check("reveal(username) 200 且值正确",
             shown_user.status_code == 200 and shown_user.json().get("value") == SANDBOX_USERNAME,
             redacted(shown_user))
    events_file = sandbox / "plugins" / "hermes-console" / "data" / "remote-events.jsonl"
    events_text = events_file.read_text(encoding="utf-8") if events_file.is_file() else ""
    sb_check("★reveal 在事件台账里留了审计行", '"action": "reveal"' in events_text,
             events_text.strip().splitlines()[-1][:120] if events_text.strip() else "<空>")
    sb_check("★台账里没有密码/SECRET 取值（只记字段名）",
             SANDBOX_PASSWORD not in events_text and SANDBOX_SECRET not in events_text)
    changed = sorted({path for path, _ in
                      (set(tree_snapshot(sandbox).items()) ^ set(before_reveal.items()))})
    sb_check("reveal 只新增台账这一个文件（不动 .env / state / ports.json）",
             changed == ["plugins/hermes-console/data/remote-events.jsonl"], str(changed))

    print("\n  --- 沙盒阶段：FAIL %d ---" % len([f for f in failures if f.startswith("[sandbox]")]))
    return 1 if failures else 0


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

    if not (DASHBOARD / "api_remote.py").is_file():
        print("  FAIL  本卡交付缺失：找不到", DASHBOARD / "api_remote.py")
        print("REMOTE-PROBE-FAILED")
        return 2

    # -----------------------------------------------------------------------
    # [7-前置] 生产零污染基线
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
    check("指纹覆盖到 9 个 config.yaml（根 + 8 个 profile）", len(before) == 9, "%d 个" % len(before))
    profiles_before = sorted(p.name for p in (REAL_HOME / "profiles").iterdir() if p.is_dir())
    remote_tree_before = tree_snapshot(REMOTE_PLUGIN)

    # -----------------------------------------------------------------------
    print("\n[1] 按 dashboard 的方式加载 plugin_api，检查 5 条路由（路径逐字）")
    module = load_plugin_api()
    print("        failed  :", brief(module.DOMAIN_FAILURES, 300))
    check("/health 口径：没有域模块加载失败（failed={}）", module.DOMAIN_FAILURES == {},
          brief(module.DOMAIN_FAILURES, 300))
    check("api_remote 在已加载清单里", "api_remote" in module.DOMAIN_LOADED,
          str(module.DOMAIN_LOADED))

    registered = set()
    for route in module.router.routes:
        for method in sorted(getattr(route, "methods", []) or []):
            registered.add((method, route.path))

    for method, path in EXPECTED_ROUTES:
        check("路由已注册 %-5s %s（逐字）" % (method, path), (method, path) in registered,
              "" if (method, path) in registered else "缺这一条")
    for method, path in FORBIDDEN_ROUTES:
        check("没有多余/写错的路由 %-5s %s" % (method, path), (method, path) not in registered)
    remote_routes = sorted("%s %s" % (m, p) for m, p in registered if p.startswith("/remote"))
    print("        /remote/* 全部路由 :", remote_routes)
    check("/remote/* 下恰好 6 条（不多不少）", len(remote_routes) == 6, str(remote_routes))

    from fastapi import FastAPI  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402

    app = FastAPI()
    app.include_router(module.router, prefix=PREFIX)
    client = TestClient(app, raise_server_exceptions=False)

    import api_remote  # noqa: E402  (plugin_api 已按本目录 import 过同名模块)

    # ★[8] 主进程也装上哨兵：本探针**绝不**允许真的启停任何 agent
    calls: list = []

    def _forbidden_spawn(*args, **kwargs):
        calls.append(args)
        raise AssertionError("探针禁止真实调用启动器：%r" % (args,))

    api_remote._run_script = _forbidden_spawn

    health = client.get(PREFIX + "/health").json()
    print("        /health :", brief({k: health.get(k) for k in ("ok", "failed", "route_count")}, 240))
    check("路由总数 ≥ %d（V4b 的下界；精确归属由 /remote/* 恰好 6 条守）" % ROUTE_TOTAL_MIN,
          isinstance(health.get("route_count"), int) and health.get("route_count") >= ROUTE_TOTAL_MIN,
          str(health.get("route_count")))
    check("route_count == 真实注册路由数（自检不是随手写的数）",
          health.get("route_count") == len(module.router.routes),
          "%s / %s" % (health.get("route_count"), len(module.router.routes)))
    check("/health 的 failed 为空", health.get("failed") == {}, brief(health.get("failed")))
    check("remote 分区挂在 /health 的 loaded 里（api_remote）",
          "api_remote" in (health.get("loaded") or []), brief(health.get("loaded"), 400))

    # -----------------------------------------------------------------------
    print("\n[2] ★★GET /remote/agents：agent 列表 == core.scope.list_agents()（动态枚举）")
    from core import scope  # noqa: E402

    independent = [str(item["id"]) for item in scope.list_agents(home=REAL_HOME)]
    resp = client.get(PREFIX + "/remote/agents")
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    rows = body.get("agents") or []
    got = [str(row.get("agent")) for row in rows]
    print("        真实扫盘（core.scope）:", independent)
    print("        GET /remote/agents   :", got)
    check("HTTP 200", resp.status_code == 200, "" if resp.status_code == 200 else redacted(resp))
    check("ok=true", body.get("ok") is True, brief(body.get("ok")))
    check("★id 集合 == core.scope.list_agents() 的 id 集合", set(got) == set(independent),
          "接口=%s 扫盘=%s" % (brief(got, 160), brief(independent, 160)))
    check("★顺序也一致（端口按这次扫到的顺序推，没有另一份写死顺序表）", got == independent,
          brief(got, 160))
    check("★数量 ≥ %d（default + 8 个 profile）" % MIN_AGENTS, len(got) >= MIN_AGENTS,
          "%d 个" % len(got))
    check("count == len(agents)", body.get("count") == len(rows), str(body.get("count")))
    check("payload 带 host / lan_addresses / script / script_exists",
          all(key in body for key in ("host", "lan_addresses", "script", "script_exists")),
          str(sorted(body.keys())))
    check("启动器文件真实存在（启停是真委托，不是空实现）", body.get("script_exists") is True,
          str(body.get("script")))

    # -----------------------------------------------------------------------
    print("\n[3] ★★列表里同时含 default 与非 default，且**不止**旧插件写死的 4 个")
    print("        旧插件写死的 _PORT_ORDER :", sorted(OLD_HARDCODED_IDS))
    extra_ids = [item for item in got if item not in OLD_HARDCODED_IDS]
    print("        接口里多出来的 agent     :", extra_ids)
    check("含 default", "default" in got, brief(got, 160))
    non_default = [item for item in got if item != "default"]
    check("含至少一个非 default（证明不是只回 default）", bool(non_default), brief(non_default, 160))
    check("含 xiaomo（旧插件里写过、新实现里也是动态来的）", "xiaomo" in got, brief(got, 160))
    check("★存在旧插件**写死名单之外**的 agent（硬编码 bug 已修）", bool(extra_ids),
          "额外的: " + str(extra_ids))

    # -----------------------------------------------------------------------
    print("\n[4] ★GET /remote/status：200，且每个 agent 都有状态字段")
    status_resp = client.get(PREFIX + "/remote/status")
    status_body = status_resp.json()
    status_rows = status_body.get("agents") or []
    print("        /remote/status -> %s | count=%s" % (status_resp.status_code, status_body.get("count")))
    check("HTTP 200", status_resp.status_code == 200,
          "" if status_resp.status_code == 200 else redacted(status_resp))
    check("每个 agent 一行（count == len(agents)）",
          status_body.get("count") == len(status_rows) == len(got),
          "%s / %s / %s" % (status_body.get("count"), len(status_rows), len(got)))
    required = ("state", "running", "pid", "port", "started_at", "uptime_seconds", "credentials",
                # V4b：信息密度那一列（连接状态 / 账号名 / 最近启动 / 最近停止 / 最近错误）
                "conn", "username", "username_configured", "username_source",
                "last_started_at", "last_stopped_at", "last_error")
    missing = [row.get("agent") for row in status_rows
               if not all(key in row for key in required)]
    check("★每行都带 %s" % "/".join(required), not missing, "缺字段的 agent: " + str(missing))
    conns = sorted({str(row.get("conn")) for row in status_rows})
    print("        出现过的 conn 取值 :", conns)
    check("conn 取值全部在 {connected, auth_required, unreachable, stopped, unknown} 里",
          set(conns) <= {"connected", "auth_required", "unreachable", "stopped", "unknown"}, str(conns))
    states = sorted({str(row.get("state")) for row in status_rows})
    print("        出现过的 state 取值 :", states)
    check("state 取值全部在 {running, stopped} 里（不是随手写的字符串）",
          set(states) <= {"running", "stopped"}, str(states))
    one = client.get(PREFIX + "/remote/status", params={"agent": "xiaomo"})
    one_body = one.json()
    print("        /remote/status?agent=xiaomo ->", one.status_code, "state =", one_body.get("state"))
    check("单 agent 查询 200 且 agent 字段正确",
          one.status_code == 200 and one_body.get("agent") == "xiaomo", redacted(one))
    unknown = client.get(PREFIX + "/remote/status", params={"agent": "nope-not-an-agent"})
    check("未知 agent 的只读查询 ⇒ 结构化 404（不是 500）",
          unknown.status_code == 404 and error_shape_ok(unknown.json()), redacted(unknown))

    # -----------------------------------------------------------------------
    print("\n[5] ★POST /remote/agent/start 对不存在的 agent ⇒ 400（不是 500），零副作用")
    before_tree = tree_snapshot(REMOTE_PLUGIN)
    bad = client.post(PREFIX + "/remote/agent/start", json={"agent": "nope-not-an-agent"})
    print("        start(不存在) :", redacted(bad))
    check("HTTP 400（不是 500）", bad.status_code == 400, str(bad.status_code))
    check("响应体是 {ok:false,error:{code,message}}", error_shape_ok(bad.json()), redacted(bad))
    check("error.code == invalid_argument",
          (bad.json().get("error") or {}).get("code") == "invalid_argument", redacted(bad))
    check("错误体带合法 agent 列表（前端可直接展示）",
          isinstance((bad.json().get("error") or {}).get("agents"), list),
          brief((bad.json().get("error") or {}).get("agents"), 200))
    bad_stop = client.post(PREFIX + "/remote/agent/stop", json={"agent": "nope-not-an-agent"})
    print("        stop(不存在)  :", redacted(bad_stop))
    check("stop 同样 400 + 结构化错误",
          bad_stop.status_code == 400 and error_shape_ok(bad_stop.json()), redacted(bad_stop))
    illegal = client.post(PREFIX + "/remote/agent/start", json={"agent": "../../etc/passwd"})
    check("非法 agent id ⇒ 400（不是 500）",
          illegal.status_code == 400 and error_shape_ok(illegal.json()), redacted(illegal))
    print("\n[5bis] dry_run 走真实 agent（只回执，不 spawn）")
    plan = client.post(PREFIX + "/remote/agent/start", json={"agent": "xiaomo", "dry_run": True})
    plan_body = plan.json()
    print("        dry_run   :", plan.status_code,
          brief({k: plan_body.get(k) for k in ("ok", "dry_run", "agent", "port")}, 200))
    check("dry_run 200 且 dry_run=true", plan.status_code == 200 and plan_body.get("dry_run") is True,
          redacted(plan))
    check("回执里点名目标 agent（二次确认要显示的那个名字）",
          plan_body.get("agent") == "xiaomo" and bool(plan_body.get("label")), brief(plan_body, 200))
    check("★[8] 启动器调用次数 == 0（真启停一次都没发生）", calls == [], str(calls))
    check("★[5] 真实 home 的 plugins/hermes-remote 零变化（没写 state、没写 ports.json）",
          tree_snapshot(REMOTE_PLUGIN) == before_tree,
          "变化的文件: " + str(sorted(set(tree_snapshot(REMOTE_PLUGIN).items())
                                      ^ set(before_tree.items()))[:4]))

    # -----------------------------------------------------------------------
    print("\n[6] ★GET /remote/credentials：不含任何真密钥")
    secrets = secret_values()
    print("        真实 .env 里的凭据条目数 : %d（取值不打印）" % len(secrets))
    cred_all = client.get(PREFIX + "/remote/credentials")
    all_text = json.dumps(cred_all.json(), ensure_ascii=False)
    cred_one = client.get(PREFIX + "/remote/credentials", params={"agent": "xiaomo"})
    one_text = json.dumps(cred_one.json(), ensure_ascii=False)
    print("        /credentials -> %s | 顶层键 %s" % (cred_all.status_code,
                                                    str(sorted(cred_all.json().keys()))[:200]))
    check("/credentials 与 /credentials?agent=xiaomo 都是 200",
          cred_all.status_code == 200 and cred_one.status_code == 200, redacted(cred_one))
    check("★响应里不含 'sk-'", "sk-" not in all_text and "sk-" not in one_text)
    values = string_values(cred_all.json(), []) + string_values(cred_one.json(), [])
    suspicious = [value for value in values
                  if SUSPICIOUS_TOKEN_RE.search(value or "")
                  # 键名本身（HERMES_DASHBOARD_BASIC_AUTH_*）不是秘密；它只是"哪个键"
                  and not str(value).startswith(KEY_NAME_PREFIX)]
    print("        疑似 token 串 :", [item[:8] + "…" for item in suspicious])
    check("★不含长度 >20 的疑似 token 串", not suspicious, brief(suspicious, 200))
    leaked = [name for name, value in secrets.items() if value in all_text or value in one_text]
    check("★响应里不含真实 .env 的任何一个取值（用户名也算）", not leaked,
          "泄漏了 %d 条（不打印取值）: %s" % (len(leaked), [n.split("@")[0] for n in leaked]))
    check("明细里每个值都是掩码 *** 或空",
          all(item.get("value") in ("***", "") for item in (cred_one.json().get("keys") or [])),
          brief([i.get("value") for i in (cred_one.json().get("keys") or [])], 120))
    check("只暴露'已配置/未配置'：status 字段存在且取值是这两者之一",
          cred_one.json().get("status") in ("已配置", "未配置"), str(cred_one.json().get("status")))
    check("summary 里有 configured / username_configured / password_configured / is_shared",
          all(key in (cred_all.json().get("agents") or [{}])[0]
              for key in ("configured", "username_configured", "password_configured", "is_shared")),
          brief((cred_all.json().get("agents") or [{}])[0], 200))
    unknown_cred = client.get(PREFIX + "/remote/credentials", params={"agent": "nope-not-an-agent"})
    check("未知 agent 的凭据查询 ⇒ 404（不是 500）",
          unknown_cred.status_code == 404, redacted(unknown_cred))

    # -----------------------------------------------------------------------
    print("\n[6bis] ★V4b：账号名可复制 / 密码只在显式 reveal 里出现 / 台账零污染")
    data_dir = REAL_HOME / "plugins" / "hermes-console" / "data"
    data_before = tree_snapshot(data_dir)
    rows_now = client.get(PREFIX + "/remote/agents").json().get("agents") or []
    agents_now_text = json.dumps(rows_now, ensure_ascii=False)
    user_values = {value for name, value in secrets.items()
                   if name.startswith(KEY_NAME_PREFIX + "USERNAME")}
    password_like = [value for name, value in secrets.items()
                     if not name.startswith(KEY_NAME_PREFIX + "USERNAME")]
    with_user = [row for row in rows_now if row.get("username_configured")]
    print("        配了账号的 agent 数 : %d / %d" % (len(with_user), len(rows_now)))
    check("★至少一个 agent 的账号名在 /remote/agents 里可见（一键复制的数据源）",
          bool(with_user)
          and all(isinstance(row.get("username"), str) and row.get("username") for row in with_user),
          brief([row.get("agent") for row in with_user], 160))
    check("★账号名与该 agent .env 里的 USERNAME 一致（是原值，不是掩码、不是编的）",
          bool(user_values) and all(row.get("username") in user_values for row in with_user),
          "%d 个账号名逐个比对（不打印取值）" % len(with_user))
    check("★/remote/agents 里没有任何密码/密码哈希/SECRET 取值（默认遮蔽）",
          not any(value and value in agents_now_text for value in password_like),
          "%d 条密码类取值全部缺席" % len(password_like))

    # reveal 的写审计走临时台账：真实 home 的 data 目录**一个字节都不该动**
    tmp_events_dir = Path(tempfile.mkdtemp(prefix="remote-events-"))
    tmp_events = tmp_events_dir / "remote-events.jsonl"
    original_events_path = api_remote._events_path
    api_remote._events_path = lambda: tmp_events
    try:
        no_confirm = client.post(PREFIX + "/remote/credentials/reveal",
                                 json={"agent": "xiaomo", "field": "password"})
        check("reveal 缺 confirm ⇒ 400（轮询/预取都拿不到明文）",
              no_confirm.status_code == 400 and error_shape_ok(no_confirm.json()),
              redacted(no_confirm))
        bad_field = client.post(PREFIX + "/remote/credentials/reveal",
                                json={"agent": "xiaomo", "field": "../../etc", "confirm": True})
        check("reveal 非白名单 field ⇒ 400", bad_field.status_code == 400, redacted(bad_field))
        unknown_agent = client.post(PREFIX + "/remote/credentials/reveal",
                                    json={"agent": "nope-not-an-agent", "field": "password",
                                          "confirm": True})
        check("reveal 未知 agent ⇒ 404（不是 500）",
              unknown_agent.status_code == 404, redacted(unknown_agent))
        expected_env = merged_env("xiaomo")
        want_user = (expected_env.get(KEY_NAME_PREFIX + "USERNAME") or "").strip()
        want_password = (expected_env.get(KEY_NAME_PREFIX + "PASSWORD") or "").strip()
        got_user = client.post(PREFIX + "/remote/credentials/reveal",
                               json={"agent": "xiaomo", "field": "username", "confirm": True})
        check("★reveal(username) 200 且返回的就是 .env 里的那个值",
              got_user.status_code == 200 and got_user.json().get("value") == want_user,
              redacted(got_user) if got_user.status_code != 200 else "值已比对（不打印）")
        got_password = client.post(PREFIX + "/remote/credentials/reveal",
                                   json={"agent": "xiaomo", "field": "password", "confirm": True})
        check("★reveal(password) 200 且返回的就是 .env 里的那个值",
              got_password.status_code == 200 and got_password.json().get("value") == want_password,
              redacted(got_password) if got_password.status_code != 200 else "值已比对（不打印）")
        check("reveal 响应带 Cache-Control: no-store",
              str(got_password.headers.get("cache-control") or "") == "no-store",
              str(got_password.headers.get("cache-control")))
        check("reveal 只回本字段（响应里没有别的凭据字段）",
              set(got_password.json().keys()) <= {
                  "ok", "agent", "label", "field", "key", "configured", "value", "source",
                  "length", "note"},
              str(sorted(got_password.json().keys())))
        events_text = tmp_events.read_text(encoding="utf-8") if tmp_events.is_file() else ""
        check("★reveal 在事件台账里留了审计行（只记字段名）",
              '"action": "reveal"' in events_text and '"field": "password"' in events_text,
              events_text.strip().splitlines()[-1][:120] if events_text.strip() else "<空>")
        check("★台账里没有明文密码 / 账号名",
              (not want_password or want_password not in events_text)
              and (not want_user or want_user not in events_text))
    finally:
        api_remote._events_path = original_events_path
        shutil.rmtree(tmp_events_dir, ignore_errors=True)
    check("★真实 home 的 plugins/hermes-console/data 零变化（reveal 不污染生产台账）",
          tree_snapshot(data_dir) == data_before,
          "变化的文件: " + str(sorted({p for p, _ in
                                  (set(tree_snapshot(data_dir).items()) ^ set(data_before.items()))}))[:200])

    # -----------------------------------------------------------------------
    print("\n[8] 旧插件保留作回退路径（并存期不许删）")
    check("plugins/hermes-remote 仍在", REMOTE_PLUGIN.is_dir(), str(REMOTE_PLUGIN))
    check("旧启动器 start_remote_backend.py 仍在（本卡只复用、没改）",
          (REMOTE_PLUGIN / "desktop" / "start_remote_backend.py").is_file())
    check("旧 dashboard/plugin_api.py 仍在",
          (REMOTE_PLUGIN / "dashboard" / "plugin_api.py").is_file())

    # -----------------------------------------------------------------------
    print("\n[S] ★沙盒阶段：假 home 上的动态枚举 + 零 spawn + 凭据掩码（子进程）")
    sandbox = Path(tempfile.mkdtemp(prefix="remote-probe-sandbox-"))
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
                failures.append(line.strip()[8:])
        check("沙盒子进程退出码 0（动态枚举 / 零 spawn / 凭据掩码全绿）",
              proc.returncode == 0, "exit=%d" % proc.returncode)
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
    profiles_after = sorted(p.name for p in (REAL_HOME / "profiles").iterdir() if p.is_dir())
    check("profiles/ 下没有新增目录（没在真实 home 造 profile）",
          profiles_after == profiles_before,
          "新增: " + str(set(profiles_after) - set(profiles_before)))
    check("★[8] 全程启动器调用次数 == 0（真启停绝不发生）", calls == [], str(calls))

    # -----------------------------------------------------------------------
    print()
    total = passed + len(failures)
    print("PASS=%d FAIL=%d TOTAL=%d" % (passed, len(failures), total))
    if failures:
        print("REMOTE-PROBE-FAILED（%d 项）" % len(failures))
        for label in failures:
            print("  -", label)
        return 1
    print("REMOTE-PROBE-OK")
    return 0


if __name__ == "__main__":
    if "--sandbox-phase" in sys.argv:
        idx = sys.argv.index("--sandbox-phase")
        raise SystemExit(sandbox_phase(Path(sys.argv[idx + 1]).resolve()))
    raise SystemExit(main())

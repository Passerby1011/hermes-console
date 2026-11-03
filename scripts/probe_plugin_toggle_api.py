# -*- coding: utf-8 -*-
"""hermes-console · M4b2 插件启停端点 验收探针（唯一验收依据）。

跑法（**不要经管道拿退出码**）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        plugins/hermes-console/scripts/probe_plugin_toggle_api.py
    echo "exit=$?"        # bash；PowerShell 用 $LASTEXITCODE

用 ``fastapi.testclient.TestClient`` **真打**新路由
``PUT /agents/{agent}/plugins/{plugin}``（以及 M4b0 的 settings / modules 路由做回归），
全部数据来自 ``dashboard/api_console_settings.py`` + ``dashboard/core/*`` 的真实返回值。

覆盖（对应卡片 8 条 + 模块开关那一条 + 生产零污染）::

    [1] 隔离              tempfile 假 HERMES_HOME（**在任何 import 之前**设好环境变量），
                          造 default / alpha / beta / gamma 四个 agent
    [2] ★enabled→disabled 只动 plugins.disabled；plugins.enabled 一个字节不变；其它 section 不变
    [3] ★disabled→enabled 从黑名单摘掉，state_after=enabled（它本来就在允许清单里）
    [4] ★unavailable→enabled 走官方准入（M2c 口径）：**真的**调用
                          ``hermes_cli.plugins_cmd._admit_and_save_plugin_sets``（探针给它加了
                          记录器，断言调用次数与候选集），回读文件确认它进了 plugins.enabled
    [5] ★乐观锁           过期 expected_config → 409 config_changed 且零字节变化；
                          只改 beta 的配置时 alpha 仍 200（锁不能锁到别人）
    [6] 非法输入          非法 agent（路径穿越）→ 400 + 合法 id 列表；非法 plugin → 400；
                          enabled 非布尔 → 400；无 config.yaml → 400 config_missing
    [7] 幂等              重复关同一个插件第二次 changed=False 且不写盘（sha256 不变）
    [8] ★三态随 agent 变  同一插件在 alpha 是 enabled、在 beta 是 unavailable
    [9] 模块开关白名单    GET .../plugins/hermes-console/settings 的 writable_keys 带
                          modules.<id>.enabled（registry 声明）；PUT 该键 → 落盘到
                          plugins.entries.hermes-console.settings.modules.<id>.enabled，
                          且 /agents/<id>/modules 立刻反映（真的写到了读的那条路径上）；
                          GET /agents/<id>/plugins 的 has_settings 对 hermes-console 为 true
    [10] 真实 home 只读   default / xiaomo / apm-worker 三个 agent 只读真打（不写），
                          打印 writable_keys / 三态 / 摘要的真实返回值
    [11] ★生产零污染      9 个真实 config.yaml 开头/结尾 sha256 全一致

**探针里唯一手写的 YAML 是 tempfile 里的假 fixture**；沙盒内的每一次写入都经
``PUT /agents/...`` → ``dashboard.core.plugin_switch.set_plugin_enabled`` /
``dashboard.core.settings.save_plugin_setting_for`` → 官方写入器。
真实 home 全程只读（第 11 条会抓）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]      # <home>/plugins/hermes-console
DASHBOARD = PLUGIN_ROOT / "dashboard"
REAL_HOME = Path(__file__).resolve().parents[3]        # <home>
HERMES_AGENT = REAL_HOME / "hermes-agent"
PREFIX = "/api/plugins/hermes-console"

failures: list[str] = []
passes = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global passes
    print(("  PASS  " if ok else "  FAIL  ") + label + (" — " + detail if detail else ""))
    if ok:
        passes += 1
    else:
        failures.append(label + (" — " + detail if detail else ""))
    return ok


def sha256_of(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "<missing>"
    except OSError as exc:  # noqa: BLE001 - 探针要的是"能不能比"，不是精确错误
        return "<error:%s>" % exc


def production_snapshot(home: Path) -> dict[str, str]:
    """真实 home 的 config.yaml + 每个 profile 的 config.yaml 的 sha256。"""
    files = [home / "config.yaml"]
    profiles_root = home / "profiles"
    if profiles_root.is_dir():
        files.extend(sorted(profiles_root.glob("*/config.yaml")))
    return {str(path): sha256_of(path) for path in files}


def print_snapshot(title: str, snapshot: dict[str, str]) -> None:
    print("        " + title + "（%d 个文件）" % len(snapshot))
    for path, digest in snapshot.items():
        print("          %s  %s" % (digest, path))


def load_yaml(path: Path):
    import yaml

    with open(path, encoding="utf-8-sig") as handle:
        return yaml.safe_load(handle) or {}


def plugins_of(path: Path) -> dict:
    node = load_yaml(path).get("plugins")
    return node if isinstance(node, dict) else {}


def entry_settings_of(path: Path, plugin_id: str) -> dict:
    node = ((load_yaml(path).get("plugins") or {}).get("entries") or {}).get(plugin_id)
    if not isinstance(node, dict):
        return {}
    got = node.get("settings")
    return got if isinstance(got, dict) else {}


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def brief(value, n: int = 300) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= n else text[:n] + "…"


def err(resp) -> dict:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return {}
    return body.get("error") or {}


def err_code(resp) -> str:
    return str(err(resp).get("code"))


# ---------------------------------------------------------------------------
print("[0] 环境")
print("        python   :", sys.version.split()[0])
print("        plugin   :", PLUGIN_ROOT)
print("        real home:", REAL_HOME)

if not (REAL_HOME / "config.yaml").is_file():
    check("真实 home 可定位（<home>/config.yaml 存在）", False, str(REAL_HOME))
    print("PLUGIN-TOGGLE-API-PROBE-FAILED")
    raise SystemExit(1)

# ---------------------------------------------------------------------------
print("[11-pre] 生产零污染 · 前置快照（在任何 import / 任何写入之前）")
prod_before = production_snapshot(REAL_HOME)
print_snapshot("真实配置 before", prod_before)
check(
    "前置快照非空且无读取错误",
    bool(prod_before) and all(not v.startswith("<error:") for v in prod_before.values()),
    "%d 个文件" % len(prod_before),
)

# ---------------------------------------------------------------------------
print("[1] 造隔离沙盒：tempfile 假 HERMES_HOME + default/alpha/beta/gamma")
# 短根目录，理由同 probe_plugin_switch_admission：官方准入拷贝 bundled 插件树时
# 路径会超过 Windows 260 字符上限（深层 vendor + 64 字符文件名），触发 WinError 3。
_SHORT_TMP = Path("D:/hcp")
_SHORT_TMP.mkdir(parents=True, exist_ok=True)
fake_home = Path(tempfile.mkdtemp(prefix="t-", dir=str(_SHORT_TMP)))
for name in ("alpha", "beta", "gamma"):
    (fake_home / "profiles" / name).mkdir(parents=True)
(fake_home / "plugins").mkdir(parents=True)
# 官方准入通道要在该 agent 自己的 plugins 目录里找已安装插件；空目录就够（候选集只含名单里的名字）。
(fake_home / "profiles" / "alpha" / "plugins").mkdir(parents=True)

import yaml  # noqa: E402

# 四个插件，覆盖三态：
PLUGIN_ON = "demo-plugin-on"        # 只在 enabled 里               → 三态 enabled
PLUGIN_OFF = "demo-plugin-off"      # 同时在 enabled 与 disabled   → 三态 disabled（黑名单优先）
PLUGIN_NEW = "demo-plugin-new"      # 两份名单都没有               → 三态 unavailable（走准入）
PLUGIN_DENYONLY = "demo-plugin-denyonly"  # **只在** disabled 里     → 开 = 摘黑名单但仍在 unavailable

root_cfg = {
    "plugins": {
        "enabled": [PLUGIN_ON, "hermes-console"],
        "disabled": ["root-off-1"],
        "entries": {
            # hermes-console 有 settings 块（用来验"模块开关之外的键照旧可写"），
            # 但**没有** modules.* 子块 ⇒ 模块开关只能靠 registry 声明进白名单。
            "hermes-console": {"settings": {"theme": "system"}},
        },
    },
    "agent": {"max_turns": 20},
}
alpha_cfg = {
    "agent": {"max_turns": 33},
    "terminal": {"timeout": 123},
    "plugins": {
        "enabled": [PLUGIN_ON, PLUGIN_OFF],
        "disabled": [PLUGIN_OFF, PLUGIN_DENYONLY],
    },
}
beta_cfg = {
    "plugins": {
        "enabled": ["beta-plugin-a"],
        "disabled": ["beta-off-1"],
        # PLUGIN_ON 只出现在 entries 里（两份名单都没有）⇒ beta 视图里它是 **unavailable**，
        # 而 alpha 视图里它是 enabled —— 同一插件、同一时刻、三态随 agent 变。
        "entries": {PLUGIN_ON: {"settings": {"note": "beta 侧只配置过、没进任何名单"}}},
    },
}

ROOT_CFG = fake_home / "config.yaml"
ALPHA_CFG = fake_home / "profiles" / "alpha" / "config.yaml"
BETA_CFG = fake_home / "profiles" / "beta" / "config.yaml"
GAMMA_DIR = fake_home / "profiles" / "gamma"

for path, data in ((ROOT_CFG, root_cfg), (ALPHA_CFG, alpha_cfg), (BETA_CFG, beta_cfg)):
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
# gamma 只有身份标记、没有 config.yaml：写 → 400 config_missing 的用例
(GAMMA_DIR / "profile.yaml").write_text(
    yaml.safe_dump({"title": "没有配置的 agent"}, allow_unicode=True), encoding="utf-8"
)

print("        fake home :", fake_home)
print("        alpha plugins :", dumps(plugins_of(ALPHA_CFG)))
print("        beta  plugins :", dumps(plugins_of(BETA_CFG)))

# 官方写入器 / 准入作用域锚定在**进程** home 上，所以必须在 import 之前把 HERMES_HOME 指到假 home。
os.environ["HERMES_HOME"] = str(fake_home)
if HERMES_AGENT.is_dir() and str(HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT))
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from dashboard.core import scope as core_scope  # noqa: E402
from dashboard.core import settings as core_settings  # noqa: E402
from dashboard.core import plugin_switch as core_switch  # noqa: E402

resolved_home = core_scope.hermes_home()
inside_temp = str(resolved_home).lower().startswith(str(fake_home).lower())
check("home 解析锚定在假沙盒内（否则立即中止，绝不写生产）", inside_temp,
      "hermes_home()=%s" % resolved_home)
if not inside_temp:
    print("PLUGIN-TOGGLE-API-PROBE-FAILED")
    raise SystemExit(1)
check(
    "四个 agent 的 config 路径各就各位",
    core_scope.agent_config_path("default") == ROOT_CFG
    and core_scope.agent_config_path("alpha") == ALPHA_CFG
    and core_scope.agent_config_path("beta") == BETA_CFG
    and core_scope.agent_config_path("gamma") == (GAMMA_DIR / "config.yaml"),
    str(core_scope.list_agent_ids()),
)
check("gamma 没有 config.yaml（400 config_missing 的用例前提）", not (GAMMA_DIR / "config.yaml").exists())

# 用真实的挂载方式加载聚合入口
api_spec = importlib.util.spec_from_file_location(
    "hermes_dashboard_plugin_hermes-console", DASHBOARD / "plugin_api.py"
)
api_mod = importlib.util.module_from_spec(api_spec)  # type: ignore[arg-type]
sys.modules[api_spec.name] = api_mod  # type: ignore[union-attr]
api_spec.loader.exec_module(api_mod)  # type: ignore[union-attr]

app = FastAPI()
app.include_router(api_mod.router, prefix=PREFIX)
client = TestClient(app, raise_server_exceptions=False)

console_settings = sys.modules.get("api_console_settings")
check("域模块 api_console_settings 已加载且未进 failed",
      "api_console_settings" in api_mod.DOMAIN_LOADED and not api_mod.DOMAIN_FAILURES,
      "loaded=%s failed=%s" % (api_mod.DOMAIN_LOADED, api_mod.DOMAIN_FAILURES))
check("api_console_settings 可按裸名取到（plugin_api 的 sys.path 生效）",
      console_settings is not None and Path(console_settings.__file__).name == "api_console_settings.py")

# ---------------------------------------------------------------------------
print("[2] 路由：新端点已挂上、总数 41 → 42、settings 路由没被吃掉")
health = client.get(PREFIX + "/health")
hbody = health.json()
print("        /health :", brief({k: hbody.get(k) for k in ("route_count", "failed")}, 240))
check("GET /health → 200", health.status_code == 200, str(health.status_code))
check("★路由总数 71（M4b0 的 41 + 本卡的启停端点 + V2 升级分区的 6 条，替换 1 条占位 + V3 上下文引擎 6 条 + V4 远端后端 5 条 + V5 模型 13 条）",
      hbody.get("route_count") == 71,
      str(hbody.get("route_count")))
check("/health 的 failed 仍为空", hbody.get("failed") == {}, brief(hbody.get("failed")))

routes = set()
for route in api_mod.router.routes:
    for method in sorted(getattr(route, "methods", []) or []):
        routes.add("%s %s" % (method, route.path))
TOGGLE_ROUTE = "PUT /agents/{agent_id:path}/plugins/{plugin_id:path}"
check("★启停路由 %r 在路由表里" % TOGGLE_ROUTE, TOGGLE_ROUTE in routes,
      brief(sorted(r for r in routes if "/agents/{agent_id:path}" in r)))
check("M4b0 的 4 条设置路由一条没少",
      {"GET /agents/{agent_id:path}/plugins",
       "GET /agents/{agent_id:path}/plugins/{plugin_id:path}/settings",
       "PUT /agents/{agent_id:path}/plugins/{plugin_id:path}/settings",
       "GET /agents/{agent_id:path}/modules"} <= routes)

# 注册顺序守卫：PUT .../plugins/<x>/settings 必须仍然命中**设置**端点，而不是被启停端点的
# ``{plugin_id:path}`` 吃成 plugin_id="<x>/settings"。
sha_before_ordering = sha256_of(ALPHA_CFG)
r = client.put(PREFIX + "/agents/alpha/plugins/nonexistent/settings",
               json={"key": "a", "value": 1})
print("        PUT .../plugins/nonexistent/settings ->", r.status_code, err_code(r))
check("★PUT .../plugins/<x>/settings 仍走设置端点（插件不存在 → 400，而不是启停端点的 400 config 语义）",
      r.status_code == 400 and err_code(r) in {"masked_value_rejected", "unknown_setting_key",
                                               "invalid_argument", "config_missing"},
      "%s %s" % (r.status_code, err_code(r)))
check("该探测请求零写入", sha256_of(ALPHA_CFG) == sha_before_ordering)

# ---------------------------------------------------------------------------
print("[3] ★enabled → disabled：只动 plugins.disabled，plugins.enabled 一个字节不变")
alpha_enabled_before = plugins_of(ALPHA_CFG).get("enabled")
alpha_disabled_before = plugins_of(ALPHA_CFG).get("disabled")
section_before = {k: v for k, v in (load_yaml(ALPHA_CFG) or {}).items() if k != "plugins"}
token = client.get(PREFIX + "/agents/alpha/plugins").json()["data"]["config_digest"]
print("        alpha enabled  before :", dumps(alpha_enabled_before))
print("        alpha disabled before :", dumps(alpha_disabled_before))
print("        token（GET plugins 的 config_digest）:", token)
check("GET /agents/alpha/plugins 的 config_digest == sha256(profiles/alpha/config.yaml)",
      token == sha256_of(ALPHA_CFG), "%s vs %s" % (token, sha256_of(ALPHA_CFG)))

r = client.put(PREFIX + "/agents/alpha/plugins/%s" % PLUGIN_ON,
               json={"enabled": False, "expected_config": token})
body = r.json()
data = body.get("data") or {}
print("        PUT {enabled:false} ->", r.status_code, brief(data, 700))
check("★启停端点 → 200 且 ok=true（不再是 404）",
      r.status_code == 200 and body.get("ok") is True, "%s %s" % (r.status_code, brief(body, 240)))
check("★state_before='enabled' / state_after='disabled'",
      data.get("state_before") == "enabled" and data.get("state_after") == "disabled",
      "%s → %s" % (data.get("state_before"), data.get("state_after")))
check("changed=true / requested_enabled=false / via='deny_list'",
      data.get("changed") is True and data.get("requested_enabled") is False
      and data.get("via") == "deny_list",
      brief({k: data.get(k) for k in ("changed", "requested_enabled", "via")}))
check("返回 verified=true 且带前后摘要",
      data.get("verified") is True
      and bool(data.get("config_digest_before")) and bool(data.get("config_digest_after"))
      and data.get("config_digest_before") != data.get("config_digest_after"),
      brief({k: data.get(k) for k in ("verified", "config_digest_before", "config_digest_after")}))
check("notes 如实说明只经 set_plugin_enabled",
      any("set_plugin_enabled" in note for note in data.get("notes") or []),
      brief(data.get("notes"), 260))

alpha_after = load_yaml(ALPHA_CFG)
check("★文件：%r 进了 plugins.disabled" % PLUGIN_ON,
      PLUGIN_ON in (plugins_of(ALPHA_CFG).get("disabled") or []),
      dumps(plugins_of(ALPHA_CFG).get("disabled")))
check("★★plugins.enabled 一个字节不变（逐项 + 原始值级比对）",
      plugins_of(ALPHA_CFG).get("enabled") == alpha_enabled_before,
      "%s → %s" % (dumps(alpha_enabled_before), dumps(plugins_of(ALPHA_CFG).get("enabled"))))
check("其它 section 原样保留（agent / terminal 没被吃掉）",
      {k: v for k, v in alpha_after.items() if k != "plugins"} == section_before,
      dumps({k: v for k, v in alpha_after.items() if k != "plugins"}))
check("disabled 里原有项一个没丢（%r 仍在）" % PLUGIN_OFF,
      PLUGIN_OFF in (plugins_of(ALPHA_CFG).get("disabled") or []),
      dumps(plugins_of(ALPHA_CFG).get("disabled")))
beta_digest_pin = sha256_of(BETA_CFG)
root_digest_pin = sha256_of(ROOT_CFG)
print("        beta/root 摘要已记录（后续每次写后都拿它比）: beta=%s root=%s"
      % (beta_digest_pin, root_digest_pin))

# ---------------------------------------------------------------------------
print("[4] ★disabled → enabled：从黑名单摘掉（不碰允许清单）")
token = client.get(PREFIX + "/agents/alpha/plugins").json()["data"]["config_digest"]
enabled_before_open = plugins_of(ALPHA_CFG).get("enabled")
r = client.put(PREFIX + "/agents/alpha/plugins/%s" % PLUGIN_OFF,
               json={"enabled": True, "expected_config": token})
data = (r.json() or {}).get("data") or {}
print("        PUT {enabled:true} ->", r.status_code, brief(data, 600))
check("→ 200 ok=true", r.status_code == 200 and (r.json() or {}).get("ok") is True,
      "%s %s" % (r.status_code, brief(r.json(), 200)))
check("state_before='disabled' / state_after='enabled'",
      data.get("state_before") == "disabled" and data.get("state_after") == "enabled",
      "%s → %s" % (data.get("state_before"), data.get("state_after")))
check("changed=true / via='deny_list'",
      data.get("changed") is True and data.get("via") == "deny_list",
      brief({k: data.get(k) for k in ("changed", "via")}))
check("★文件：%r 已不在 plugins.disabled 里" % PLUGIN_OFF,
      PLUGIN_OFF not in (plugins_of(ALPHA_CFG).get("disabled") or []),
      dumps(plugins_of(ALPHA_CFG).get("disabled")))
check("★plugins.enabled 仍然一个字节不变（开启也只动黑名单）",
      plugins_of(ALPHA_CFG).get("enabled") == enabled_before_open,
      "%s → %s" % (dumps(enabled_before_open), dumps(plugins_of(ALPHA_CFG).get("enabled"))))
check("beta / 根配置仍零变化",
      sha256_of(BETA_CFG) == beta_digest_pin and sha256_of(ROOT_CFG) == root_digest_pin)

# 黑名单优先 + M2 的诚实结果：PLUGIN_DENYONLY **只在** disabled 里（不在允许清单）。
# 关它 → 幂等（已在黑名单里，changed=False、不写盘）。
sha_pin = sha256_of(ALPHA_CFG)
r = client.put(PREFIX + "/agents/alpha/plugins/%s" % PLUGIN_DENYONLY, json={"enabled": False})
data = (r.json() or {}).get("data") or {}
print("        PUT denyonly{enabled:false} ->", r.status_code, brief(data, 400))
check("★只在黑名单里的插件 state_before='disabled'（黑名单优先，不被当成 enabled）",
      data.get("state_before") == "disabled", str(data.get("state_before")))
check("已在黑名单 ⇒ changed=false（不写盘）", data.get("changed") is False, dumps(data.get("changed")))
check("★幂等这次零写盘（sha256 不变）", sha256_of(ALPHA_CFG) == sha_pin,
      "%s vs %s" % (sha_pin, sha256_of(ALPHA_CFG)))

# 开启一个**只在黑名单里**的插件：按 M2 口径只摘黑名单 → 回读仍是 unavailable + warning。
# 本层**不许**把它补成 enabled（那要动 plugins.enabled 整表，本卡严禁），也不许把这种
# 诚实结果改写成 500。这一条正是"绝不退化成只摘黑名单"的反面守卫：真的没启用就如实说。
sha_pin = sha256_of(ALPHA_CFG)
enabled_before_denyonly = plugins_of(ALPHA_CFG).get("enabled")
r = client.put(PREFIX + "/agents/alpha/plugins/%s" % PLUGIN_DENYONLY, json={"enabled": True})
data = (r.json() or {}).get("data") or {}
print("        PUT denyonly{enabled:true} ->", r.status_code, brief(data, 620))
check("→ 200（这是执行成功，不是启用成功）", r.status_code == 200,
      "%s %s" % (r.status_code, brief(r.json(), 200)))
check("★via='deny_list'（只摘黑名单）且 state_after='unavailable'（如实：仍未启用）",
      data.get("via") == "deny_list" and data.get("state_after") == "unavailable",
      "%s / %s" % (data.get("via"), data.get("state_after")))
check("★带 warning（M2 原文，不吞）",
      bool(data.get("warning")) and "plugins.enabled" in str(data.get("warning")),
      brief(data.get("warning"), 260))
check("★plugins.enabled 仍然一个字节不变（本层没有替用户跨到启用）",
      plugins_of(ALPHA_CFG).get("enabled") == enabled_before_denyonly,
      "%s → %s" % (dumps(enabled_before_denyonly), dumps(plugins_of(ALPHA_CFG).get("enabled"))))
check("文件：%r 已不在 disabled 里（黑名单确实被摘掉了）" % PLUGIN_DENYONLY,
      PLUGIN_DENYONLY not in (plugins_of(ALPHA_CFG).get("disabled") or []),
      dumps(plugins_of(ALPHA_CFG).get("disabled")))
check("文件摘要确实变了（这一条真的写了盘）", sha256_of(ALPHA_CFG) != sha_pin,
      "%s → %s" % (sha_pin, sha256_of(ALPHA_CFG)))
# 复原成"只在黑名单里"，不影响后续用例
alpha_now = load_yaml(ALPHA_CFG)
disabled_now = [item for item in (plugins_of(ALPHA_CFG).get("disabled") or []) if item != PLUGIN_DENYONLY]
disabled_now.append(PLUGIN_DENYONLY)
alpha_now.setdefault("plugins", {})["disabled"] = disabled_now
ALPHA_CFG.write_text(yaml.safe_dump(alpha_now, allow_unicode=True, sort_keys=False), encoding="utf-8")
print("        已把 %r 复原到黑名单（后续用例的基线）" % PLUGIN_DENYONLY)

# ---------------------------------------------------------------------------
print("[5] ★幂等：重复关同一个插件第二次 changed=False 且不写盘")
sha_pin = sha256_of(ALPHA_CFG)
r = client.put(PREFIX + "/agents/alpha/plugins/%s" % PLUGIN_ON, json={"enabled": False})
data2 = (r.json() or {}).get("data") or {}
print("        第二次关 %r ->" % PLUGIN_ON, r.status_code, brief(data2, 400))
check("第二次关同一插件 → 200 / changed=false", r.status_code == 200 and data2.get("changed") is False,
      "%s changed=%r" % (r.status_code, data2.get("changed")))
check("state_before='disabled' / state_after='disabled'（如实反映没有变化）",
      data2.get("state_before") == "disabled" and data2.get("state_after") == "disabled",
      "%s → %s" % (data2.get("state_before"), data2.get("state_after")))
check("★★幂等调用零写盘（config.yaml sha256 一字不变）", sha256_of(ALPHA_CFG) == sha_pin,
      "%s vs %s" % (sha_pin, sha256_of(ALPHA_CFG)))
check("响应里 config_digest_before == config_digest_after（没写）",
      data2.get("config_digest_before") == data2.get("config_digest_after"),
      "%s vs %s" % (data2.get("config_digest_before"), data2.get("config_digest_after")))

# ---------------------------------------------------------------------------
print("[6] ★乐观锁：过期令牌 409 且零字节变化；只改 beta 时 alpha 仍 200")
alpha_token = sha256_of(ALPHA_CFG)
sha_pin = alpha_token
r = client.put(PREFIX + "/agents/alpha/plugins/%s" % PLUGIN_OFF,
               json={"enabled": False, "expected_config": "0" * 64})
j = r.json()
print("        PUT（过期令牌）->", r.status_code, brief(j, 420))
check("★过期 expected_config → 409 config_changed",
      r.status_code == 409 and err_code(r) == "config_changed",
      "%s %s" % (r.status_code, err_code(r)))
check("★409 时配置零字节变化", sha256_of(ALPHA_CFG) == sha_pin,
      "%s vs %s" % (sha_pin, sha256_of(ALPHA_CFG)))
check("409 错误体带 expected_config / actual_config / unchanged=True",
      bool(err(r).get("expected_config")) and bool(err(r).get("actual_config"))
      and err(r).get("unchanged") is True,
      brief({k: err(r).get(k) for k in ("expected_config", "actual_config", "unchanged")}))
check("409 的错误体形状是 {ok:false, error:{code,message}}",
      (j or {}).get("ok") is False and {"code", "message"} <= set(err(r) or {}), brief(j, 200))

# 方向二：只改 **beta**（另一个 agent）的配置，alpha 用自己刚才的令牌必须仍然成功
beta_now = load_yaml(BETA_CFG)
beta_now.setdefault("plugins", {}).setdefault("enabled", []).append("beta-external-edit")
BETA_CFG.write_text(yaml.safe_dump(beta_now, allow_unicode=True, sort_keys=False), encoding="utf-8")
beta_edited = sha256_of(BETA_CFG)
check("外部改动确实只换了 beta 的摘要（前提成立）", beta_edited != beta_digest_pin,
      "%s → %s" % (beta_digest_pin, beta_edited))
r = client.put(PREFIX + "/agents/alpha/plugins/%s" % PLUGIN_OFF,
               json={"enabled": False, "expected_config": alpha_token})
print("        PUT alpha（beta 刚被改过）->", r.status_code, brief(r.json(), 260))
check("★★只改 beta 的配置时 alpha 的 PUT 仍 200（锁没锁到别人）", r.status_code == 200,
      "%s %s" % (r.status_code, brief(r.json(), 220)))
check("这次写入落到 alpha 自己的文件（%r 进了它的 disabled）" % PLUGIN_OFF,
      PLUGIN_OFF in (plugins_of(ALPHA_CFG).get("disabled") or []),
      dumps(plugins_of(ALPHA_CFG).get("disabled")))
check("beta 的外部改动没有被我们回写覆盖",
      "beta-external-edit" in (plugins_of(BETA_CFG).get("enabled") or [])
      and sha256_of(BETA_CFG) == beta_edited,
      dumps(plugins_of(BETA_CFG).get("enabled")))

# 方向一：改 alpha **自己**后，旧令牌必须 409
stale = sha256_of(ALPHA_CFG)
alpha_now = load_yaml(ALPHA_CFG)
alpha_now.setdefault("agent", {})["max_turns"] = 44
ALPHA_CFG.write_text(yaml.safe_dump(alpha_now, allow_unicode=True, sort_keys=False), encoding="utf-8")
alpha_edited = sha256_of(ALPHA_CFG)
check("外部改动确实换了 alpha 的摘要", alpha_edited != stale, "%s → %s" % (stale, alpha_edited))
r = client.put(PREFIX + "/agents/alpha/plugins/%s" % PLUGIN_ON,
               json={"enabled": True, "expected_config": stale})
print("        PUT alpha（改自己后用旧令牌）->", r.status_code, err_code(r))
check("★改了自己文件后用旧令牌 → 409", r.status_code == 409 and err_code(r) == "config_changed",
      "%s %s" % (r.status_code, err_code(r)))
check("★409 时零字节变化", sha256_of(ALPHA_CFG) == alpha_edited,
      "%s vs %s" % (alpha_edited, sha256_of(ALPHA_CFG)))
r = client.put(PREFIX + "/agents/alpha/plugins/%s" % PLUGIN_ON, json={"enabled": True})
check("不带 expected_config 时不校验锁（新鲜读取即写，200）", r.status_code == 200,
      "%s %s" % (r.status_code, brief(r.json(), 200)))

# ---------------------------------------------------------------------------
print("[7] 非法输入一律 400（统一失败体）")
sha_pin = sha256_of(ALPHA_CFG)
r = client.put(PREFIX + "/agents/..%2F..%2Fetc/plugins/" + PLUGIN_ON, json={"enabled": False})
j = r.json()
print("        agent 路径穿越 ->", r.status_code, brief(j, 320))
check("★非法 agent → 400 invalid_argument", r.status_code == 400 and err_code(r) == "invalid_argument",
      "%s %s" % (r.status_code, err_code(r)))
check("★400 body 带合法 id 列表且含 default/alpha/beta",
      {"default", "alpha", "beta"} <= set(err(r).get("agents") or []),
      brief(err(r).get("agents")))
check("非法 agent 的错误体形状是 {ok:false, error:{code,message}}",
      (j or {}).get("ok") is False and {"code", "message"} <= set(err(r) or {}), brief(j, 200))

for label, path_part in (
    ("plugin '../evil'", "..%2Fevil"),
    ("plugin 大写 'Demo-Plugin-On'", "Demo-Plugin-On"),
    ("plugin 反斜杠 'a%5Cb'", "a%5Cb"),
):
    r = client.put(PREFIX + "/agents/alpha/plugins/" + path_part, json={"enabled": False})
    print("        %s ->" % label, r.status_code, err_code(r))
    check("%s → 400" % label, r.status_code == 400 and err_code(r) == "invalid_argument",
          "%s %s" % (r.status_code, err_code(r)))
r = client.put(PREFIX + "/agents/alpha/plugins/%s" % PLUGIN_ON, json={"enabled": "false"})
check("enabled='false'（字符串）→ 400（不把字符串当真值）",
      r.status_code == 400 and err_code(r) == "invalid_argument", "%s %s" % (r.status_code, err_code(r)))
r = client.put(PREFIX + "/agents/alpha/plugins/%s" % PLUGIN_ON, json={"enabled": 0})
check("enabled=0 → 400（只认真 bool）", r.status_code == 400 and err_code(r) == "invalid_argument",
      "%s %s" % (r.status_code, err_code(r)))
r = client.put(PREFIX + "/agents/alpha/plugins/%s" % PLUGIN_ON,
               json={"expected_config": "x" * 64})
check("缺 enabled 字段 → 400", r.status_code == 400 and err_code(r) == "invalid_argument",
      "%s %s" % (r.status_code, err_code(r)))
r = client.put(PREFIX + "/agents/alpha/plugins/%s" % PLUGIN_ON,
               json={"enabled": True, "expected_config": 123})
check("expected_config 非字符串 → 400", r.status_code == 400 and err_code(r) == "invalid_argument",
      "%s %s" % (r.status_code, err_code(r)))
r = client.put(PREFIX + "/agents/alpha/plugins/%s" % PLUGIN_ON, content=b"")
check("空请求体 → 400（统一失败体，不是 422）",
      r.status_code == 400 and err_code(r) == "invalid_argument", "%s %s" % (r.status_code, err_code(r)))
r = client.put(PREFIX + "/agents/alpha/plugins/%s" % PLUGIN_ON, content=b"{not json")
check("非法 JSON → 400（不是 500/422）",
      r.status_code == 400 and err_code(r) == "invalid_argument", "%s %s" % (r.status_code, err_code(r)))
check("以上非法请求全程零写入（alpha sha256 未变）", sha256_of(ALPHA_CFG) == sha_pin,
      "%s vs %s" % (sha_pin, sha256_of(ALPHA_CFG)))

r = client.put(PREFIX + "/agents/gamma/plugins/%s" % PLUGIN_ON, json={"enabled": False})
j = r.json()
print("        gamma（无 config.yaml）->", r.status_code, brief(j, 320))
check("★该 agent 没有 config.yaml → 400 config_missing",
      r.status_code == 400 and err_code(r) == "config_missing",
      "%s %s" % (r.status_code, err_code(r)))
check("说明里点名缺哪个文件",
      str(GAMMA_DIR / "config.yaml") in (err(r).get("message") or ""),
      brief(err(r).get("message"), 240))
check("gamma 目录没有因此被创建出 config.yaml", not (GAMMA_DIR / "config.yaml").exists())

# ---------------------------------------------------------------------------
print("[8] ★unavailable → enabled：走官方准入权威（M2c 口径）")
import hermes_cli.plugins_cmd as official_cmd  # noqa: E402

admission_calls: list[dict] = []
real_admit = official_cmd._admit_and_save_plugin_sets


def _recording_admit(enabled, disabled, **kwargs):
    """记录候选集后**调用真函数**（不是替换掉它——那样等于没测准入通道）。"""
    admission_calls.append(
        {
            "enabled": sorted(str(item) for item in (enabled or ())),
            "disabled": sorted(str(item) for item in (disabled or ())),
            "kwargs": {k: (v if k != "expected_config" else "<digest>") for k, v in kwargs.items()},
        }
    )
    return real_admit(enabled, disabled, **kwargs)


official_cmd._admit_and_save_plugin_sets = _recording_admit
state_before_admission = core_switch.plugin_state("alpha", PLUGIN_NEW, tri_state=True)
print("        alpha/%s 三态（写前）:" % PLUGIN_NEW, dumps(state_before_admission))
check("前提：%r 在 alpha 里是 unavailable（两份名单都没有）" % PLUGIN_NEW,
      state_before_admission["state"] == "unavailable",
      dumps(state_before_admission))
enabled_before_admission = plugins_of(ALPHA_CFG).get("enabled")
token = client.get(PREFIX + "/agents/alpha/plugins").json()["data"]["config_digest"]
started = time.time()
r = client.put(PREFIX + "/agents/alpha/plugins/%s" % PLUGIN_NEW,
               json={"enabled": True, "expected_config": token})
elapsed = time.time() - started
data = (r.json() or {}).get("data") or {}
print("        PUT {enabled:true} ->", r.status_code, "（%.1fs）" % elapsed, brief(data, 800))
print("        准入调用记录 :", brief(admission_calls, 600))
check("→ 200 ok=true（不再 404）", r.status_code == 200 and (r.json() or {}).get("ok") is True,
      "%s %s" % (r.status_code, brief(r.json(), 240)))
check("★★确实调了官方 _admit_and_save_plugin_sets（探针记录器抓到的真实调用）",
      len(admission_calls) >= 1, "%d 次" % len(admission_calls))
check("★候选集正确：enabled = 原 enabled ∪ {插件}，disabled = 原 disabled - {插件}",
      bool(admission_calls)
      and PLUGIN_NEW in admission_calls[-1]["enabled"]
      and set(enabled_before_admission or []) <= set(admission_calls[-1]["enabled"])
      and PLUGIN_NEW not in admission_calls[-1]["disabled"],
      brief(admission_calls[-1] if admission_calls else None, 400))
check("准入调用的 action='enable' / plugin=插件名",
      bool(admission_calls) and admission_calls[-1]["kwargs"].get("action") == "enable"
      and admission_calls[-1]["kwargs"].get("plugin") == PLUGIN_NEW,
      brief(admission_calls[-1]["kwargs"] if admission_calls else None, 300))
check("★state_before='unavailable' / state_after='enabled'",
      data.get("state_before") == "unavailable" and data.get("state_after") == "enabled",
      "%s → %s" % (data.get("state_before"), data.get("state_after")))
check("via='admission'（不是 deny_list —— 没退化成只摘黑名单）",
      data.get("via") == "admission", str(data.get("via")))
check("changed=true / verified=true",
      data.get("changed") is True and data.get("verified") is True,
      brief({k: data.get(k) for k in ("changed", "verified")}))
check("steps 里记录了准入那一步（via=admission）",
      any(str(step.get("via")) == "admission" for step in data.get("steps") or []),
      brief(data.get("steps"), 400))
check("★回读文件：%r 真的进了 plugins.enabled" % PLUGIN_NEW,
      PLUGIN_NEW in (plugins_of(ALPHA_CFG).get("enabled") or []),
      dumps(plugins_of(ALPHA_CFG).get("enabled")))
check("原有 enabled 项一个没丢",
      set(enabled_before_admission or []) <= set(plugins_of(ALPHA_CFG).get("enabled") or []),
      dumps(plugins_of(ALPHA_CFG).get("enabled")))
check("准入之后三态回读是 enabled",
      core_switch.plugin_state("alpha", PLUGIN_NEW, tri_state=True)["state"] == "enabled",
      dumps(core_switch.plugin_state("alpha", PLUGIN_NEW, tri_state=True)))
check("准入没有波及 beta / 根配置",
      sha256_of(ROOT_CFG) == root_digest_pin and sha256_of(BETA_CFG) == beta_edited,
      "root=%s beta=%s" % (sha256_of(ROOT_CFG) == root_digest_pin,
                           sha256_of(BETA_CFG) == beta_edited))

official_cmd._admit_and_save_plugin_sets = real_admit

# ---------------------------------------------------------------------------
print("[9] ★三态随 agent 变（同一插件在 alpha 是 enabled、在 beta 是 unavailable）")
r_alpha = client.get(PREFIX + "/agents/alpha/plugins")
r_beta = client.get(PREFIX + "/agents/beta/plugins")
alpha_states = {item["plugin_id"]: item["state"] for item in r_alpha.json()["data"]["plugins"]}
beta_states = {item["plugin_id"]: item["state"] for item in r_beta.json()["data"]["plugins"]}
print("        alpha/%s :" % PLUGIN_ON, alpha_states.get(PLUGIN_ON))
print("        beta /%s :" % PLUGIN_ON, beta_states.get(PLUGIN_ON))
check("★同一插件在 alpha 是 enabled", alpha_states.get(PLUGIN_ON) == "enabled",
      str(alpha_states.get(PLUGIN_ON)))
check("★同一插件在 beta 是 unavailable", beta_states.get(PLUGIN_ON) == "unavailable",
      str(beta_states.get(PLUGIN_ON)))
check("两个 agent 的清单不相同（按 agent 取，不是全局缓存）",
      set(alpha_states) != set(beta_states) or alpha_states != beta_states,
      "alpha=%s beta=%s" % (brief(sorted(alpha_states), 200), brief(sorted(beta_states), 200)))
check("三态取值只在这三种之内",
      set(alpha_states.values()) | set(beta_states.values()) <= {"enabled", "disabled", "unavailable"},
      brief(sorted(set(alpha_states.values()) | set(beta_states.values()))))

# ---------------------------------------------------------------------------
print("[10] 模块开关：writable_keys 带 registry 声明的 modules.<id>.enabled，且写得进去")
r = client.get(PREFIX + "/agents/default/plugins")
listing = (r.json() or {}).get("data") or {}
by_id = {item["plugin_id"]: item for item in listing.get("plugins") or []}
console_item = by_id.get("hermes-console") or {}
print("        default/hermes-console 条目 :", brief(console_item, 420))
check("★GET /agents/default/plugins 里 hermes-console 的 has_settings = true（M4b1 的空白名单已修）",
      console_item.get("has_settings") is True, str(console_item.get("has_settings")))

r = client.get(PREFIX + "/agents/default/plugins/hermes-console/settings")
view = (r.json() or {}).get("data") or {}
writable = view.get("writable_keys") or []
print("        GET default/hermes-console/settings ->", r.status_code)
print("        ★writable_keys 真实返回值 (%d 项) :" % len(writable))
for key in writable:
    print("            -", key)
module_keys = [key for key in writable if key.startswith("modules.") and key.endswith(".enabled")]
print("        模块开关键 :", brief(module_keys, 400))
check("★writable_keys 非空（不再是空数组）", bool(writable), brief(writable))
check("★writable_keys 含 registry 声明的全部模块开关（modules.<id>.enabled）",
      len(module_keys) >= 7 and {"modules.overview.enabled", "modules.settings.enabled",
                                 "modules.weather.enabled"} <= set(module_keys),
      "%d 个：%s" % (len(module_keys), brief(module_keys, 320)))
check("★writable_keys 仍含 M4b0 原来的 theme（既有可写键没被挤掉）",
      "theme" in writable, brief(writable))
check("模块开关的落点写在 notes 里（说明它不是 config_schema 来的）",
      any("registry" in note for note in view.get("notes") or []),
      brief(view.get("notes"), 320))
check("GET settings 带 module_switches（有效值 + 来源 + 原因）",
      isinstance(view.get("module_switches"), list) and len(view.get("module_switches") or []) >= 7,
      brief((view.get("module_switches") or [])[:2], 400))
sw = {item["key"]: item for item in view.get("module_switches") or []}
print("        模块开关样本 :", brief(sw.get("modules.overview.enabled"), 320))
check("module_switches 里 overview 的 enabled=true / source=module_default",
      (sw.get("modules.overview.enabled") or {}).get("enabled") is True
      and (sw.get("modules.overview.enabled") or {}).get("source") == "module_default",
      brief(sw.get("modules.overview.enabled"), 300))

# /modules 的"写前"值 → PUT 该键 → 落盘 → /agents/<id>/modules 立刻反映
modules_before = {item["id"]: item for item in
                  client.get(PREFIX + "/agents/default/modules").json()["data"]["modules"]}
print("        /modules overview（写前）:", brief(modules_before.get("overview"), 260))
check("写前 overview 是 enabled=true（module_default）",
      (modules_before.get("overview") or {}).get("enabled") is True,
      brief(modules_before.get("overview"), 260))

token = view.get("expected_config")
r = client.put(PREFIX + "/agents/default/plugins/hermes-console/settings",
               json={"key": "modules.overview.enabled", "value": False, "expected_config": token})
print("        PUT modules.overview.enabled=false ->", r.status_code, brief(r.json(), 420))
check("★PUT .../plugins/hermes-console/settings 认 modules.<id>.enabled（不再 unknown_setting_key）",
      r.status_code == 200 and (r.json() or {}).get("ok") is True,
      "%s %s" % (r.status_code, brief(r.json(), 300)))
written = entry_settings_of(ROOT_CFG, "hermes-console")
print("        落盘后的 settings :", brief(written, 400))
check("★落盘路径正是 plugins.entries.hermes-console.settings.modules.overview.enabled",
      ((written.get("modules") or {}).get("overview") or {}).get("enabled") is False,
      brief(written, 400))
check("同插件的既有键 theme 没被吃掉", written.get("theme") == "system", dumps(written.get("theme")))
modules_after = {item["id"]: item for item in
                 client.get(PREFIX + "/agents/default/modules").json()["data"]["modules"]}
print("        /modules overview（写后）:", brief(modules_after.get("overview"), 300))
check("★GET /agents/default/modules 立刻反映（enabled=false，来源 agent/global 之一）",
      (modules_after.get("overview") or {}).get("enabled") is False,
      brief(modules_after.get("overview"), 300))
profiles_root = (load_yaml(ROOT_CFG).get("profiles") or "sentinel")
check("写模块开关没有碰别的 section（plugins 之外的键没被重写）",
      "plugins" in load_yaml(ROOT_CFG) and isinstance(load_yaml(ROOT_CFG).get("agent"), dict),
      brief(sorted(load_yaml(ROOT_CFG)), 200))

# 未知的模块 id 仍然必须被拒（白名单是**声明**驱动的，不是"随便 modules.x.y"）
sha_pin = sha256_of(ROOT_CFG)
r = client.put(PREFIX + "/agents/default/plugins/hermes-console/settings",
               json={"key": "modules.does_not_exist.enabled", "value": True})
print("        PUT modules.does_not_exist.enabled ->", r.status_code, err_code(r))
check("★未声明的模块 id → 400 unknown_setting_key（白名单不是放行任意 modules.*）",
      r.status_code == 400 and err_code(r) == "unknown_setting_key",
      "%s %s" % (r.status_code, err_code(r)))
check("被拒后零字节变化", sha256_of(ROOT_CFG) == sha_pin)

# 启停端点对 hermes-console 自己也能用（界面上同一屏里插件开关与模块开关并存）
r = client.put(PREFIX + "/agents/default/plugins/hermes-console", json={"enabled": False})
print("        PUT default/hermes-console {enabled:false} ->", r.status_code, brief(r.json(), 300))
check("★hermes-console 自己的插件开关也走同一条启停端点（200）", r.status_code == 200,
      "%s %s" % (r.status_code, brief(r.json(), 260)))
r = client.put(PREFIX + "/agents/default/plugins/hermes-console", json={"enabled": True})
check("再打开它 → 200 且 state_after=enabled",
      r.status_code == 200 and ((r.json() or {}).get("data") or {}).get("state_after") == "enabled",
      "%s %s" % (r.status_code, brief(r.json(), 260)))

# ---------------------------------------------------------------------------
print("[10b] 真实 home 只读真打：default / xiaomo / apm-worker（不写）")
os.environ["HERMES_HOME"] = str(REAL_HOME)
real_home_now = core_scope.hermes_home()
check("HERMES_HOME 复位后 home 解析回到真实 home",
      Path(real_home_now).resolve() == REAL_HOME.resolve(), str(real_home_now))

real_writable: dict[str, list] = {}
real_states: dict[str, dict] = {}
for agent in ("default", "xiaomo", "apm-worker"):
    resp = client.get(PREFIX + "/agents/%s/plugins" % agent)
    data = (resp.json() or {}).get("data") or {}
    items = data.get("plugins") or []
    states = {item["plugin_id"]: item["state"] for item in items}
    real_states[agent] = states
    print("        GET /agents/%s/plugins ->" % agent, resp.status_code,
          "%d 项" % len(items), brief(data.get("counts")))
    check("真实 %s：GET plugins → 200 且三态全在范围内" % agent,
          resp.status_code == 200 and bool(states)
          and set(states.values()) <= {"enabled", "disabled", "unavailable"},
          brief(sorted(set(states.values()))))

    resp = client.get(PREFIX + "/agents/%s/plugins/hermes-console/settings" % agent)
    view = (resp.json() or {}).get("data") or {}
    real_writable[agent] = view.get("writable_keys") or []
    print("        GET /agents/%s/plugins/hermes-console/settings ->" % agent, resp.status_code,
          "writable_keys=%d 项" % len(real_writable[agent]))
    check("真实 %s：读 hermes-console settings → 200" % agent, resp.status_code == 200,
          "%s %s" % (resp.status_code, brief(view, 200)))
    check("★真实 %s：writable_keys 含模块开关（界面有可写入口了）" % agent,
          {"modules.overview.enabled", "modules.settings.enabled"} <= set(real_writable[agent]),
          brief(real_writable[agent], 480))

# ★M4b1 报的原始症状：真实 home 的 config.yaml 里**没有** plugins.entries.hermes-console，
#   所以 has_settings 只能由 registry 声明的模块开关撑起来。这一条专门盯那条分支：
#   把"模块开关也算有设置"改掉，这里立刻变 false。
real_entry = (((load_yaml(REAL_HOME / "config.yaml").get("plugins") or {}).get("entries")) or {}).get(
    "hermes-console"
)
print("        真实 default 的 plugins.entries.hermes-console :", dumps(real_entry))
check("前提：真实 default 的 config.yaml 里没有 hermes-console 的 settings 块",
      not isinstance((real_entry or {}).get("settings"), dict) or not (real_entry or {}).get("settings"),
      dumps(real_entry))
real_default_listing = (client.get(PREFIX + "/agents/default/plugins").json() or {}).get("data") or {}
real_console = {item["plugin_id"]: item for item in real_default_listing.get("plugins") or []}.get(
    "hermes-console"
) or {}
print("        真实 default/hermes-console 条目 :", brief(real_console, 320))
check("★★真实 default：hermes-console 的 has_settings = true（M4b1 的原始症状已修：没有 settings 块时靠模块开关）",
      real_console.get("has_settings") is True, str(real_console.get("has_settings")))

print("        ★真实 writable_keys（default，真实返回值 %d 项）:" % len(real_writable["default"]))
for key in real_writable["default"]:
    print("            -", key)

check("★真实三态随 agent 变（三个 agent 的插件清单/三态不完全相同）",
      len({json.dumps(v, sort_keys=True) for v in real_states.values()}) >= 2,
      " | ".join("%s=%d 项/%s" % (k, len(v), brief(sorted(set(v.values())), 120))
                 for k, v in real_states.items()))
# 真实 home 上真打一次非法 agent 的启停（不碰任何文件）
r = client.put(PREFIX + "/agents/..%2F..%2Fetc/plugins/demo", json={"enabled": False})
check("真实 home 下非法 agent 的启停请求也只是 400（不会碰任何文件）",
      r.status_code == 400, "%s %s" % (r.status_code, err_code(r)))

# ---------------------------------------------------------------------------
print("[11] ★生产零污染 · 后置快照")
prod_after = production_snapshot(REAL_HOME)
print_snapshot("真实配置 after", prod_after)
changed = {path: (prod_before.get(path), prod_after.get(path))
           for path in set(prod_before) | set(prod_after)
           if prod_before.get(path) != prod_after.get(path)}
check("★真实 config.yaml + profiles/*/config.yaml 前后 sha256 完全一致（零污染，%d 个文件）"
      % len(prod_after), not changed, dumps(changed) if changed else "全部一致")
check("前后文件集合一致（没有新增/删除配置文件）", set(prod_before) == set(prod_after))

# ---------------------------------------------------------------------------
shutil.rmtree(fake_home, ignore_errors=True)
print("        沙盒已清理 :", fake_home)

print()
print("统计：%d PASS / %d FAIL" % (passes, len(failures)))
if failures:
    print("失败 %d 条：" % len(failures))
    for item in failures:
        print("  - " + item)
    print("PLUGIN-TOGGLE-API-PROBE-FAILED")
    raise SystemExit(1)

print("全部检查通过（启停三态 + 只动黑名单 + 官方准入 + 按 agent 的乐观锁 + 统一错误体 + "
      "幂等零写盘 + 模块开关白名单 + 真实 home 只读 + %d 个真实配置文件零污染）" % len(prod_after))
print("PLUGIN-TOGGLE-API-PROBE-OK")
raise SystemExit(0)

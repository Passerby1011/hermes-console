# -*- coding: utf-8 -*-
"""hermes-console · M4b0 后端设置端点 验收探针（唯一验收依据）。

跑法（**不要经管道拿退出码**）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        plugins/hermes-console/scripts/probe_console_settings_api.py
    echo "exit=$?"        # bash；PowerShell 用 $LASTEXITCODE

用 ``fastapi.testclient.TestClient`` **真打**四条新路由（``GET /agents/{id}/plugins``、
``GET/PUT /agents/{id}/plugins/{pid}/settings``、``GET /agents/{id}/modules``），
全部数据来自 ``dashboard/api_console_settings.py`` + ``dashboard/core/*`` 的真实返回值。

覆盖（对应卡片的 9 条 + 编排者后补的\"按 agent 取摘要\"一条）::

    [1] 隔离              tempfile 假 HERMES_HOME（**在任何 import 之前**设好环境变量），
                          里面造 default / xiaomo / alpha / beta / gamma 五个 agent
    [2] ★三态显式         GET /agents/default/plugins：每项 state ∈ {enabled,disabled,unavailable}；
                          配置里 entries-only 的插件必须是 unavailable（不许被写成 enabled）；
                          响应里的 state 与 plugin_switch.plugin_state 逐项一致
    [3] 按 agent 取        xiaomo 与 default 的清单/三态不同（不是全局缓存）；切换 agent 后计数变化
    [4] 非法输入           ``..%2F..%2Fetc``（会真的进到 handler）→ 400 且带合法 id 列表；
                          plugin 侧 ../evil / 空串 / 大写 / 反斜杠 → 400
    [5] ★脱敏 + 来源      masked_keys 非空时对应值必然是 "***"；每个键都有 sources 标注；
                          三层来源（module_default / global / agent）各有一个真实用例
    [6] ★拒掩码           PUT "***" → 400 masked_value_rejected，且该 agent config.yaml 零字节变化
    [7] 未知键            PUT 未声明键 → 400 unknown_setting_key 并列出可写键
    [8] 只读保护          blocker_forced_delivery（程序常量）与 manifest readonly:true → 400 readonly_setting
    [9] ★合并语义         PUT 正常键 → 200，回读生效，同插件其它键**全部保留**（文件级 + API 级）
    [10] ★乐观锁          过期 expected_config → 409 config_changed + 零字节变化；
                          ★按 agent 取摘要：只改 beta 的配置，对 alpha 发 PUT **必须成功**
                          （锁到别人文件就会误报 409）；改 alpha 自己后同一令牌 → 409；
                          GET 返回的 expected_config == sha256(profiles/<agent>/config.yaml)
    [11] 缺 config.yaml    读 → 200 空结构（不是 404）；写 → 400 config_missing
    [12] 口径交叉校验      hermes-console 走 effective_settings，其它插件走同口径合并：
                          把 PLUGIN_ID 换成别名后两条路径逐键同值（证明没有第二套合并规则）
    [13] 真实 home 只读    HERMES_HOME 复位后对真实 default / xiaomo 只读真打，
                          打印三态实际返回值（不写任何真实配置）
    [14] ★生产零污染      真实 config.yaml + profiles/*/config.yaml 开头/结尾 sha256 全一致

**探针里唯一手写的 YAML 是 tempfile 里的假 fixture**；对\"配置\"的每一次写入都经
``PUT /agents/.../settings`` → ``dashboard.core.settings.save_plugin_setting_for`` →
官方 ``hermes_cli.plugins_state.save_plugin_setting``。真实 home 全程只读（第 14 条会抓）。
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

PLUGIN_ROOT = Path(__file__).resolve().parents[1]      # <home>/plugins/hermes-console
DASHBOARD = PLUGIN_ROOT / "dashboard"
REAL_HOME = Path(__file__).resolve().parents[3]        # <home>
HERMES_AGENT = REAL_HOME / "hermes-agent"
PREFIX = "/api/plugins/hermes-console"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  PASS  " if ok else "  FAIL  ") + label + (" — " + detail if detail else ""))
    if not ok:
        failures.append(label)
    return ok


def sha256_of(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "<missing>"
    except OSError as exc:  # noqa: BLE001 - 探针要的是\"能不能比\"，不是精确错误
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


def settings_of(path: Path, plugin_id: str) -> dict:
    node = ((load_yaml(path).get("plugins") or {}).get("entries") or {}).get(plugin_id)
    if not isinstance(node, dict):
        return {}
    got = node.get("settings")
    return got if isinstance(got, dict) else {}


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def brief(value, n: int = 260) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= n else text[:n] + "…"


def err_code(resp) -> str:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return "<non-json>"
    return str(((body or {}).get("error") or {}).get("code"))


# ---------------------------------------------------------------------------
print("[0] 环境")
print("        python   :", sys.version.split()[0])
print("        plugin   :", PLUGIN_ROOT)
print("        real home:", REAL_HOME)

if not (REAL_HOME / "config.yaml").is_file():
    check("真实 home 可定位（<home>/config.yaml 存在）", False, str(REAL_HOME))
    print("CONSOLE-SETTINGS-API-PROBE-FAILED")
    raise SystemExit(1)

# ---------------------------------------------------------------------------
print("[14-pre] 生产零污染 · 前置快照（在任何 import / 任何写入之前）")
prod_before = production_snapshot(REAL_HOME)
print_snapshot("真实配置 before", prod_before)
check(
    "前置快照非空且无读取错误",
    bool(prod_before) and all(not v.startswith("<error:") for v in prod_before.values()),
)

# ---------------------------------------------------------------------------
print("[1] 造隔离沙盒：tempfile 假 HERMES_HOME + default/xiaomo/alpha/beta/gamma")
fake_home = Path(tempfile.mkdtemp(prefix="hc-console-settings-api-probe-"))
for name in ("xiaomo", "alpha", "beta", "gamma"):
    (fake_home / "profiles" / name).mkdir(parents=True)

import yaml  # noqa: E402

# 根（default）：enabled / disabled / entries 三处都放东西，
# 并刻意留一个\"只在 entries 里\"的插件（hindsight）——它必须是 unavailable。
root_cfg = {
    "plugins": {
        "enabled": ["browserpaw", "hermes-console", "root-only-plugin", "readonly-demo"],
        "disabled": ["hermes-memory-ui"],
        "entries": {
            "browserpaw": {"settings": {"isolate_modal": False, "headless": False,
                                        "api_key": "sk-root-secret", "retry": {"max": 3}}},
            "hermes-console": {"settings": {"theme": "system",
                                            "modules": {"overview": {"enabled": True}}}},
            "readonly-demo": {"settings": {"level": 1}},
            # 只在 entries、两份名单里都没有 ⇒ 三态 unavailable
            "hindsight": {"settings": {"window": 20}},
        },
    }
}
xiaomo_cfg = {
    "plugins": {
        "enabled": ["apm-worktime", "weather", "browserpaw"],
        "disabled": ["image_gen/fal", "web/searxng"],
        "entries": {
            "hermes-console": {"settings": {"modules": {"weather": {"enabled": False}}}},
            "browserpaw": {"settings": {"isolate_modal": True, "api_key": "sk-xiaomo"}},
        },
    }
}
# alpha / beta：插件集相同、值不同（用来验\"按 agent 取摘要\"与\"合并语义\"）
alpha_cfg = {
    "agent": {"max_turns": 33},
    "plugins": {
        "enabled": ["alpha-plugin-a", "browserpaw", "hermes-console", "readonly-demo"],
        "entries": {
            "browserpaw": {"settings": {"isolate_modal": True, "timeout": 5,
                                        "api_token": "tok-alpha", "retry": {"max": 7}}},
            "hermes-console": {"settings": {"theme": "dark"}},
            "readonly-demo": {"settings": {"level": 2}},
        },
    },
}
beta_cfg = {
    "plugins": {
        "enabled": ["beta-plugin-a", "browserpaw", "hermes-console"],
        "entries": {
            "browserpaw": {"settings": {"isolate_modal": False, "timeout": 99}},
            "hermes-console": {"settings": {"theme": "light"}},
        },
    },
}

ROOT_CFG = fake_home / "config.yaml"
XIAOMO_CFG = fake_home / "profiles" / "xiaomo" / "config.yaml"
ALPHA_CFG = fake_home / "profiles" / "alpha" / "config.yaml"
BETA_CFG = fake_home / "profiles" / "beta" / "config.yaml"
GAMMA_DIR = fake_home / "profiles" / "gamma"

for path, data in (
    (ROOT_CFG, root_cfg),
    (XIAOMO_CFG, xiaomo_cfg),
    (ALPHA_CFG, alpha_cfg),
    (BETA_CFG, beta_cfg),
):
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
# gamma 只有身份标记、没有 config.yaml：读 200 空结构 / 写 400 的用例
(GAMMA_DIR / "profile.yaml").write_text(
    yaml.safe_dump({"title": "没有配置的 agent"}, allow_unicode=True), encoding="utf-8"
)

# 假插件：manifest 里带了只读键与 schema 默认值（验 [8] 只读保护 与 module_default 来源）
demo_dir = fake_home / "plugins" / "readonly-demo"
demo_dir.mkdir(parents=True)
(demo_dir / "plugin.yaml").write_text(
    yaml.safe_dump(
        {
            "name": "readonly-demo",
            "version": "1.0.0",
            "config_schema": {
                "locked_key": {"type": "boolean", "readonly": True,
                               "description": "由宿主程序锁定，不可在界面修改"},
                "mode": {"type": "str", "default": "safe", "choices": ["safe", "fast"]},
                "level": {"type": "number"},
            },
        },
        allow_unicode=True,
        sort_keys=False,
    ),
    encoding="utf-8",
)

print("        fake home :", fake_home)
print("        alpha.browserpaw :", dumps(settings_of(ALPHA_CFG, "browserpaw")))

# 官方写入器 / 作用域锚定在**进程** home 上，所以必须在 import 之前把 HERMES_HOME 指到假 home。
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
from dashboard.core import plugin_catalog as core_catalog  # noqa: E402

resolved_home = core_scope.hermes_home()
inside_temp = str(resolved_home).lower().startswith(str(fake_home).lower())
check("home 解析锚定在假沙盒内（否则立即中止，绝不写生产）", inside_temp,
      "hermes_home()=%s" % resolved_home)
if not inside_temp:
    print("CONSOLE-SETTINGS-API-PROBE-FAILED")
    raise SystemExit(1)
check(
    "五个 agent 的 config 路径各就各位",
    core_scope.agent_config_path("default") == ROOT_CFG
    and core_scope.agent_config_path("xiaomo") == XIAOMO_CFG
    and core_scope.agent_config_path("alpha") == ALPHA_CFG
    and core_scope.agent_config_path("beta") == BETA_CFG,
    str(core_scope.list_agent_ids()),
)
check("gamma 没有 config.yaml（读 200 / 写 400 的用例前提）",
      not (GAMMA_DIR / "config.yaml").exists())

# 用真实的挂载方式加载聚合入口（DOMAIN_MODULES 里必须已追加本模块）
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
print("[2] 路由总数 / health / 三态列表")
health = client.get(PREFIX + "/health")
hbody = health.json()
print("        /health :", brief(hbody, 400))
check("GET /health → 200", health.status_code == 200, str(health.status_code))
check("路由总数 71（M4b0 的 41 + M4b2 的启停端点 + V2 升级分区的 6 条，替换 1 条占位 + V3 上下文引擎 6 条 + V4 远端后端 5 条 + V5 模型 13 条）", hbody.get("route_count") == 71, str(hbody.get("route_count")))
check("/health 的 failed 仍为空", hbody.get("failed") == {}, brief(hbody.get("failed")))
check("M4b0 的 4 条路由都在（按路径前缀筛 /agents/{agent_id:path}，共 5 条含 M4b2 的启停端点）",
      len([r for r in api_mod.router.routes
           if str(getattr(r, "path", "")).startswith("/agents/{agent_id:path}")]) == 5,
      str([str(getattr(r, "path", "")) for r in api_mod.router.routes
           if str(getattr(r, "path", "")).startswith("/agents/{agent_id:path}")]))

resp = client.get(PREFIX + "/agents/default/plugins")
body = resp.json()
default_data = body.get("data") or {}
print("        GET /agents/default/plugins ->", resp.status_code, brief(default_data.get("counts")))
check("GET /agents/default/plugins → 200 且 ok=true",
      resp.status_code == 200 and body.get("ok") is True, str(resp.status_code))
items = default_data.get("plugins") or []
check("每项都含 state 且取值只在三态内",
      bool(items) and all(item.get("state") in ("enabled", "disabled", "unavailable") for item in items),
      brief(sorted({item.get("state") for item in items})))
check("每项都含 plugin_id/state/in_enabled/in_disabled/source/has_settings（六个键齐全）",
      all({"plugin_id", "state", "in_enabled", "in_disabled", "source", "has_settings"} <= set(item)
          for item in items),
      brief(items[:1], 300))

by_id = {item["plugin_id"]: item for item in items}
print("        browserpaw :", brief(by_id.get("browserpaw")))
print("        hindsight  :", brief(by_id.get("hindsight")))
print("        hermes-memory-ui :", brief(by_id.get("hermes-memory-ui")))
check("正在允许清单里的插件 state=enabled / source=enabled_list",
      by_id.get("browserpaw", {}).get("state") == "enabled"
      and by_id.get("browserpaw", {}).get("source") == "enabled_list"
      and by_id["browserpaw"]["in_enabled"] is True,
      brief(by_id.get("browserpaw")))
check("黑名单里的插件 state=disabled / source=disabled_list",
      by_id.get("hermes-memory-ui", {}).get("state") == "disabled"
      and by_id.get("hermes-memory-ui", {}).get("source") == "disabled_list"
      and by_id["hermes-memory-ui"]["in_disabled"] is True,
      brief(by_id.get("hermes-memory-ui")))
check("★只在 entries 里的插件 state=unavailable（绝不被写成 enabled）",
      by_id.get("hindsight", {}).get("state") == "unavailable"
      and by_id.get("hindsight", {}).get("in_enabled") is False
      and by_id.get("hindsight", {}).get("in_disabled") is False,
      brief(by_id.get("hindsight")))
check("★三态与 plugin_switch.plugin_state 逐项一致（本层不自己判三态）",
      all(
          by_id[name]["state"] == core_switch.plugin_state("default", name, tri_state=True)["state"]
          for name in by_id
          if core_settings.PLUGIN_ID_RE.match(name) or "/" in name
      ),
      brief({name: [by_id[name]["state"],
                    core_switch.plugin_state("default", name, tri_state=True)["state"]]
             for name in list(by_id)[:5]}))
check("counts 是三种状态各自的条数且合计等于清单长度",
      sum(default_data.get("counts", {}).values()) == len(items)
      and set(default_data.get("counts", {})) == {"enabled", "disabled", "unavailable"},
      brief(default_data.get("counts")))
check("skipped 为空（假 home 里没有超文法插件 id）", default_data.get("skipped") == [],
      brief(default_data.get("skipped")))
check("has_settings 对写了 settings 的插件为 true、对纯名单项为 false",
      by_id.get("browserpaw", {}).get("has_settings") is True
      and by_id.get("root-only-plugin", {}).get("has_settings") is False,
      brief({k: by_id[k]["has_settings"] for k in ("browserpaw", "root-only-plugin") if k in by_id}))

# ---------------------------------------------------------------------------
print("[3] 按 agent 取（不是全局缓存）")
resp_x = client.get(PREFIX + "/agents/xiaomo/plugins")
xdata = resp_x.json().get("data") or {}
print("        GET /agents/xiaomo/plugins ->", resp_x.status_code, brief(xdata.get("counts")))
x_ids = {item["plugin_id"] for item in xdata.get("plugins") or []}
d_ids = {item["plugin_id"] for item in items}
print("        xiaomo ids :", sorted(x_ids))
check("xiaomo → 200", resp_x.status_code == 200, str(resp_x.status_code))
check("★xiaomo 与 default 的插件集合不同（同一进程、同一路由，按 agent 取）",
      x_ids != d_ids, "只在 xiaomo=%s / 只在 default=%s" % (sorted(x_ids - d_ids), sorted(d_ids - x_ids)))
check("★同名插件的三态也随 agent 变（browserpaw 在 default 是 enabled，在 beta 也由名单决定）",
      client.get(PREFIX + "/agents/beta/plugins").json()["data"]["counts"]
      != default_data.get("counts"),
      "%s vs %s" % (client.get(PREFIX + "/agents/beta/plugins").json()["data"]["counts"],
                    default_data.get("counts")))
check("xiaomo 的黑名单项在 xiaomo 视图里是 disabled 而不是 absent/enabled",
      {item["plugin_id"]: item["state"] for item in xdata["plugins"]}.get("web/searxng") == "disabled",
      brief(xdata.get("counts")))

# ---------------------------------------------------------------------------
print("[4] 非法输入一律 400（不是 500，也不是 404）")
r = client.get(PREFIX + "/agents/..%2F..%2Fetc/plugins")
j = r.json()
print("        GET /agents/..%2F..%2Fetc/plugins ->", r.status_code, brief(j, 300))
check("★路径穿越 agent → 400 invalid_argument", r.status_code == 400 and err_code(r) == "invalid_argument",
      str(r.status_code))
check("★400 body 带合法 id 列表且含 default/xiaomo/alpha",
      {"default", "xiaomo", "alpha"} <= set(j.get("error", {}).get("agents") or []),
      brief(j.get("error", {}).get("agents")))
check("非法 agent 的错误体形状是 {ok:false, error:{code,message}}",
      j.get("ok") is False and {"code", "message"} <= set(j.get("error") or {}), brief(j, 200))

r = client.get(PREFIX + "/agents/default/plugins/..%2Fevil/settings")
print("        settings plugin '../evil' ->", r.status_code, err_code(r))
check("plugin_id '../evil' → 400", r.status_code == 400 and err_code(r) == "invalid_argument")
r = client.get(PREFIX + "/agents/default/plugins/BrowserPaw/settings")
check("plugin_id 大写 → 400（不做静默小写化）", r.status_code == 400, err_code(r))
r = client.get(PREFIX + "/agents/default/plugins//settings")
check("plugin_id 空串 → 400", r.status_code == 400, err_code(r))
r = client.get(PREFIX + "/agents/default/plugins/a%5Cb/settings")
check("plugin_id 反斜杠 → 400", r.status_code == 400, "%s %s" % (r.status_code, err_code(r)))
r = client.get(PREFIX + "/agents/..%2F..%2Fetc/plugins/browserpaw/settings")
check("读 settings 时 agent 穿越 → 400", r.status_code == 400, err_code(r))
r = client.get(PREFIX + "/agents/..%2F..%2Fetc/modules")
check("modules 端点 agent 穿越 → 400", r.status_code == 400, err_code(r))

# ---------------------------------------------------------------------------
print("[5] 脱敏 + 来源标注")
r = client.get(PREFIX + "/agents/alpha/plugins/browserpaw/settings")
d = r.json().get("data") or {}
print("        GET alpha/browserpaw/settings ->", r.status_code, brief(d.get("settings"), 300))
print("        masked_keys :", brief(d.get("masked_keys")))
print("        sources     :", brief(d.get("sources")))
check("GET settings → 200 ok=true", r.status_code == 200 and r.json().get("ok") is True, str(r.status_code))
check("★masked_keys 非空（api_token 命中敏感键）", bool(d.get("masked_keys")), brief(d.get("masked_keys")))
masked_set = d.get("masked_keys") or []


def dig(tree, dotted):
    node = tree
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return "<absent>"
        node = node[part]
    return node


check("★每个 masked_key 对应的值都是 '***'（不是真密钥）",
      all(dig(d.get("settings") or {}, key) == "***" for key in masked_set),
      brief({key: dig(d.get("settings") or {}, key) for key in masked_set}))
check("响应体里没有真密钥（api_token / api_key 均为掩码）",
      "tok-alpha" not in json.dumps(d, ensure_ascii=False) and "sk-root-secret" not in json.dumps(d, ensure_ascii=False),
      "响应里既不含 tok-alpha 也不含 sk-root-secret")
flat_settings = []


def walk(node, prefix=""):
    if isinstance(node, dict):
        for key, value in node.items():
            walk(value, "%s.%s" % (prefix, key) if prefix else str(key))
    else:
        flat_settings.append(prefix)


walk(d.get("settings") or {})
check("★settings 里每个点分键都在 sources 里出现（逐项标注，无遗漏）",
      set(flat_settings) <= set(d.get("sources") or {}),
      "缺=%s" % sorted(set(flat_settings) - set(d.get("sources") or {})))
check("sources 的取值只在 module_default/global/agent 三档内",
      set((d.get("sources") or {}).values()) <= {"module_default", "global", "agent"},
      brief(sorted(set((d.get("sources") or {}).values()))))
check("★agent 层覆盖：alpha 的 isolate_modal=true 且来源标 agent",
      dig(d.get("settings") or {}, "isolate_modal") is True
      and (d.get("sources") or {}).get("isolate_modal") == "agent",
      "%s / %s" % (dig(d.get("settings") or {}, "isolate_modal"),
                   (d.get("sources") or {}).get("isolate_modal")))
check("★global 层回落：headless 只在根配置里有，来源标 global",
      dig(d.get("settings") or {}, "headless") is False
      and (d.get("sources") or {}).get("headless") == "global",
      "%s / %s" % (dig(d.get("settings") or {}, "headless"),
                   (d.get("sources") or {}).get("headless")))
r2 = client.get(PREFIX + "/agents/alpha/plugins/readonly-demo/settings")
d2 = r2.json().get("data") or {}
print("        GET alpha/readonly-demo/settings ->", brief({k: d2.get(k) for k in ("settings", "sources", "readonly_keys", "writable_keys")}, 400))
check("★module_default 来源：manifest 的 config_schema.default 生效并标 module_default",
      dig(d2.get("settings") or {}, "mode") == "safe"
      and (d2.get("sources") or {}).get("mode") == "module_default",
      "%s / %s" % (dig(d2.get("settings") or {}, "mode"), (d2.get("sources") or {}).get("mode")))
check("readonly_keys 列出 manifest 标了 readonly 的键",
      d2.get("readonly_keys") == ["locked_key"], brief(d2.get("readonly_keys")))
check("readonly 键不在 writable_keys 里",
      "locked_key" not in (d2.get("writable_keys") or []), brief(d2.get("writable_keys")))
check("响应带 path / exists / expected_config",
      {"path", "exists", "expected_config"} <= set(d or {}), brief(sorted(d or {})))
check("path 指向该 agent 自己的 config.yaml",
      Path(d.get("path") or "") == ALPHA_CFG, str(d.get("path")))
check("★响应里没有第二份真配置读取口子：sources 的键与 settings 的叶子键一一对应（不多不少）",
      set(d.get("sources") or {}) == set(flat_settings),
      "多=%s" % sorted(set(d.get("sources") or {}) - set(flat_settings)))

# ---------------------------------------------------------------------------
print("[6] ★拒掩码：PUT '***' → 400 masked_value_rejected + 零字节变化")
sha_before = sha256_of(ALPHA_CFG)
r = client.put(PREFIX + "/agents/alpha/plugins/browserpaw/settings",
               json={"key": "api_token", "value": "***"})
j = r.json()
print("        PUT {api_token:'***'} ->", r.status_code, brief(j, 320))
check("★PUT '***' → 400 masked_value_rejected",
      r.status_code == 400 and err_code(r) == "masked_value_rejected",
      "%s %s" % (r.status_code, err_code(r)))
check("错误文案写明「脱敏占位符不能写回 / 等于拿掩码覆盖真密钥」",
      "脱敏占位符不能写回" in (j.get("error", {}).get("message") or "")
      and "掩码覆盖真密钥" in (j.get("error", {}).get("message") or ""),
      brief(j.get("error", {}).get("message"), 200))
check("★被拒后 alpha 的 config.yaml 零字节变化（sha256 相同）",
      sha256_of(ALPHA_CFG) == sha_before, "%s → %s" % (sha_before, sha256_of(ALPHA_CFG)))
r = client.put(PREFIX + "/agents/alpha/plugins/browserpaw/settings",
               json={"settings": {"retry": {"max": "***"}}})
check("★嵌套 mapping 里的 '***' 也拒（不是只查顶层）",
      r.status_code == 400 and err_code(r) == "masked_value_rejected", "%s %s" % (r.status_code, err_code(r)))
check("嵌套掩码被拒后仍零字节变化", sha256_of(ALPHA_CFG) == sha_before)
r = client.put(PREFIX + "/agents/alpha/plugins/browserpaw/settings",
               json={"key": "timeout", "value": "***abc"})
check("'***abc' 不是占位符（精确相等才算 '***'），所以不按掩码拒",
      err_code(r) != "masked_value_rejected", "%s %s" % (r.status_code, err_code(r)))
# 探针自己刚把它写成了 "***abc"：立刻恢复原值，后面的用例不该继承这次写入。
restore = client.put(PREFIX + "/agents/alpha/plugins/browserpaw/settings",
                     json={"key": "timeout", "value": 5})
check("探针把 fixture 恢复到 timeout=5（不把中间状态留给后面的用例）",
      restore.status_code == 200 and settings_of(ALPHA_CFG, "browserpaw").get("timeout") == 5,
      str(settings_of(ALPHA_CFG, "browserpaw").get("timeout")))

# ---------------------------------------------------------------------------
print("[7] 未知键 → 400 unknown_setting_key 并列出可写键")
sha_before = sha256_of(ALPHA_CFG)
r = client.put(PREFIX + "/agents/alpha/plugins/browserpaw/settings",
               json={"key": "definitely_not_a_key", "value": 1})
j = r.json()
print("        PUT 未知键 ->", r.status_code, brief(j, 320))
check("★未知键 → 400 unknown_setting_key",
      r.status_code == 400 and err_code(r) == "unknown_setting_key", "%s %s" % (r.status_code, err_code(r)))
writable = j.get("error", {}).get("writable_keys") or []
check("错误体列出可写键（非空且不含未知键）",
      bool(writable) and "definitely_not_a_key" not in writable, brief(writable))
check("被拒后零字节变化", sha256_of(ALPHA_CFG) == sha_before)

# ---------------------------------------------------------------------------
print("[8] 只读保护：程序常量 + manifest readonly")
sha_before = sha256_of(ALPHA_CFG)
r = client.put(PREFIX + "/agents/alpha/plugins/readonly-demo/settings",
               json={"key": "locked_key", "value": True})
j = r.json()
print("        PUT manifest readonly 键 ->", r.status_code, brief(j, 300))
check("★manifest readonly:true 的键 → 400 readonly_setting",
      r.status_code == 400 and err_code(r) == "readonly_setting", "%s %s" % (r.status_code, err_code(r)))
check("错误文案说明是 manifest 标的只读", "只读" in (j.get("error", {}).get("message") or ""),
      brief(j.get("error", {}).get("message"), 200))
r = client.put(PREFIX + "/agents/alpha/plugins/readonly-demo/settings",
               json={"key": "blocker_forced_delivery", "value": True})
j = r.json()
print("        PUT blocker_forced_delivery ->", r.status_code, brief(j, 300))
check("★程序常量 blocker_forced_delivery → 400 readonly_setting（不静默忽略）",
      r.status_code == 400 and err_code(r) == "readonly_setting", "%s %s" % (r.status_code, err_code(r)))
check("程序常量的说明写明「程序常量/单向阀门」",
      "程序常量" in (j.get("error", {}).get("message") or "")
      and "单向阀门" in (j.get("error", {}).get("message") or ""),
      brief(j.get("error", {}).get("message"), 200))
r = client.put(PREFIX + "/agents/alpha/plugins/readonly-demo/settings",
               json={"key": "blocker_forced_delivery", "value": False})
check("★把它改成 false 也一样 400（单向阀门不可关闭）",
      r.status_code == 400 and err_code(r) == "readonly_setting", "%s %s" % (r.status_code, err_code(r)))
check("两个只读请求都没写盘", sha256_of(ALPHA_CFG) == sha_before)

# ---------------------------------------------------------------------------
print("[9] ★合并语义：PUT 正常键 → 200 + 回读生效 + 其它键全部保留")
before_file = settings_of(ALPHA_CFG, "browserpaw")
print("        写前文件值 :", dumps(before_file))
sha_before = sha256_of(ALPHA_CFG)
d = client.get(PREFIX + "/agents/alpha/plugins/browserpaw/settings").json()["data"]
token = d.get("expected_config")
r = client.put(PREFIX + "/agents/alpha/plugins/browserpaw/settings",
               json={"key": "timeout", "value": 42, "expected_config": token})
j = r.json()
print("        PUT {timeout:42} ->", r.status_code, brief(j, 380))
check("PUT 正常键（带刚读到的 expected_config）→ 200 ok=true",
      r.status_code == 200 and j.get("ok") is True, "%s %s" % (r.status_code, brief(j, 200)))
pdata = j.get("data") or {}
check("返回 verified=true", pdata.get("verified") is True, brief({k: pdata.get(k) for k in ("verified", "previous")}))
check("previous 是写前文件里的旧值 5", pdata.get("previous") == 5, str(pdata.get("previous")))
check("★写后 config.yaml 摘要真的变了（不是自述成功）",
      pdata.get("config_digest_before") != pdata.get("config_digest_after")
      and sha256_of(ALPHA_CFG) != sha_before,
      "%s → %s" % (pdata.get("config_digest_before"), pdata.get("config_digest_after")))

after_file = settings_of(ALPHA_CFG, "browserpaw")
print("        写后文件值 :", dumps(after_file))
check("★文件里 timeout 变成 42", after_file.get("timeout") == 42, str(after_file.get("timeout")))
kept = {key: after_file.get(key) for key in before_file if key != "timeout"}
print("        保留的其它键 :", dumps(kept))
check("★同插件其它键全部保留（isolate_modal / api_token / retry.max 一项不少、值不变）",
      kept == {key: value for key, value in before_file.items() if key != "timeout"}
      and after_file.get("isolate_modal") is True
      and after_file.get("api_token") == "tok-alpha"
      and (after_file.get("retry") or {}).get("max") == 7,
      dumps(after_file))
check("同 agent 的 hermes-console settings 没被改（只动了目标插件）",
      settings_of(ALPHA_CFG, "hermes-console") == {"theme": "dark"},
      dumps(settings_of(ALPHA_CFG, "hermes-console")))
check("别的 agent 的 browserpaw 没被改（beta 的 timeout 仍是 99）",
      settings_of(BETA_CFG, "browserpaw").get("timeout") == 99,
      dumps(settings_of(BETA_CFG, "browserpaw")))
back = client.get(PREFIX + "/agents/alpha/plugins/browserpaw/settings").json()["data"]
check("GET 回读也是 42（写后读路径一致）",
      dig(back.get("settings") or {}, "timeout") == 42, dumps(back.get("settings")))
check("写完后 sources 里 timeout 标 agent（本次写的就是 agent 层）",
      (back.get("sources") or {}).get("timeout") == "agent",
      str((back.get("sources") or {}).get("timeout")))
check("写完 expected_config 更新成新摘要",
      back.get("expected_config") == sha256_of(ALPHA_CFG),
      "%s vs %s" % (back.get("expected_config"), sha256_of(ALPHA_CFG)))

# settings 映射写法等价（同一次只写一项）
r = client.put(PREFIX + "/agents/alpha/plugins/browserpaw/settings",
               json={"settings": {"retry.max": 9}})
check("settings 映射写法也能写（且 = 单项）",
      r.status_code == 200 and settings_of(ALPHA_CFG, "browserpaw").get("retry", {}).get("max") == 9,
      "%s %s" % (r.status_code, brief(r.json(), 200)))
r = client.put(PREFIX + "/agents/alpha/plugins/browserpaw/settings",
               json={"settings": {"timeout": 1, "isolate_modal": False}})
check("settings 里多项 → 400（本端点一次只写一项，不做部分写）",
      r.status_code == 400 and err_code(r) == "invalid_argument", "%s %s" % (r.status_code, err_code(r)))
r = client.put(PREFIX + "/agents/alpha/plugins/browserpaw/settings", content=b"")
check("空请求体 → 400（统一失败体，不是 422）",
      r.status_code == 400 and err_code(r) == "invalid_argument", "%s %s" % (r.status_code, err_code(r)))
r = client.put(PREFIX + "/agents/alpha/plugins/browserpaw/settings", content=b"{not json")
check("非法 JSON → 400（不是 500/422）",
      r.status_code == 400 and err_code(r) == "invalid_argument", "%s %s" % (r.status_code, err_code(r)))
r = client.put(PREFIX + "/agents/..%2F..%2Fetc/plugins/browserpaw/settings",
               json={"key": "timeout", "value": 1})
check("写时 agent 穿越 → 400", r.status_code == 400 and err_code(r) == "invalid_argument",
      "%s %s" % (r.status_code, err_code(r)))

# ---------------------------------------------------------------------------
print("[10] ★乐观锁：按 agent 取摘要（两个方向都要测）")
d = client.get(PREFIX + "/agents/alpha/plugins/browserpaw/settings").json()["data"]
alpha_token = d.get("expected_config")
check("★GET 返回的 expected_config == sha256(profiles/alpha/config.yaml)（不是根配置摘要）",
      alpha_token == sha256_of(ALPHA_CFG) and alpha_token != sha256_of(ROOT_CFG),
      "alpha=%s root=%s" % (alpha_token, sha256_of(ROOT_CFG)))
print("        alpha token :", alpha_token)

# 方向一：改**别的** agent（beta）后，对 alpha 发 PUT 必须成功（锁不能锁到别人文件）
beta_cfg_now = load_yaml(BETA_CFG)
beta_cfg_now.setdefault("plugins", {}).setdefault("entries", {}).setdefault("browserpaw", {})["settings"] = {
    "isolate_modal": True, "timeout": 77}
BETA_CFG.write_text(yaml.safe_dump(beta_cfg_now, allow_unicode=True, sort_keys=False), encoding="utf-8")
print("        已只改 beta 的配置（timeout 99 → 77），beta 新摘要 :", sha256_of(BETA_CFG))
sha_alpha_before = sha256_of(ALPHA_CFG)
r = client.put(PREFIX + "/agents/alpha/plugins/browserpaw/settings",
               json={"key": "timeout", "value": 43, "expected_config": alpha_token})
print("        PUT alpha（beta 已被改）->", r.status_code, brief(r.json(), 240))
check("★只改 beta 后，alpha 的 PUT 仍然 200（没有被别的 agent 的改动误伤）",
      r.status_code == 200, "%s %s" % (r.status_code, brief(r.json(), 200)))
check("该写入确实落到 alpha 自己的文件（beta 未被连带修改）",
      settings_of(ALPHA_CFG, "browserpaw").get("timeout") == 43
      and settings_of(BETA_CFG, "browserpaw").get("timeout") == 77,
      "alpha=%s beta=%s" % (settings_of(ALPHA_CFG, "browserpaw").get("timeout"),
                            settings_of(BETA_CFG, "browserpaw").get("timeout")))

# 方向二：改 alpha **自己**的配置后，用旧令牌发 PUT 必须 409（锁真的在看自己的文件）
stale_token = sha256_of(ALPHA_CFG)
alpha_now = load_yaml(ALPHA_CFG)
alpha_now.setdefault("plugins", {}).setdefault("entries", {}).setdefault("hermes-console", {}).setdefault(
    "settings", {})["theme"] = "dark-edited-elsewhere"
ALPHA_CFG.write_text(yaml.safe_dump(alpha_now, allow_unicode=True, sort_keys=False), encoding="utf-8")
sha_alpha_after_edit = sha256_of(ALPHA_CFG)
check("外部改动确实换了 alpha 的摘要（前提成立）", sha_alpha_after_edit != stale_token,
      "%s → %s" % (stale_token, sha_alpha_after_edit))
r = client.put(PREFIX + "/agents/alpha/plugins/browserpaw/settings",
               json={"key": "timeout", "value": 44, "expected_config": stale_token})
j = r.json()
print("        PUT alpha（过期令牌）->", r.status_code, brief(j, 340))
check("★改了自己文件后用旧令牌 → 409 config_changed",
      r.status_code == 409 and err_code(r) == "config_changed", "%s %s" % (r.status_code, err_code(r)))
check("★409 时配置文件零字节变化（一个字节都没写）",
      sha256_of(ALPHA_CFG) == sha_alpha_after_edit,
      "%s → %s" % (sha_alpha_after_edit, sha256_of(ALPHA_CFG)))
check("409 的错误体带 expected_config/actual_config 两个摘要，便于前端刷新",
      bool(j.get("error", {}).get("expected_config")) and bool(j.get("error", {}).get("actual_config")),
      brief({k: j.get("error", {}).get(k) for k in ("expected_config", "actual_config")}))
check("409 时 timeout 仍是外部改动前的值（没被这次请求改掉）",
      settings_of(ALPHA_CFG, "browserpaw").get("timeout") == 43,
      str(settings_of(ALPHA_CFG, "browserpaw").get("timeout")))
r = client.put(PREFIX + "/agents/alpha/plugins/browserpaw/settings",
               json={"key": "timeout", "value": 44})
check("不带 expected_config 时不校验锁（新鲜读取即写，200）", r.status_code == 200,
      "%s %s" % (r.status_code, brief(r.json(), 160)))
check("不带锁的那次写入确实生效（timeout=44）",
      settings_of(ALPHA_CFG, "browserpaw").get("timeout") == 44,
      str(settings_of(ALPHA_CFG, "browserpaw").get("timeout")))
check("★default 的 expected_config == sha256(<home>/config.yaml)（default 就是根文件）",
      client.get(PREFIX + "/agents/default/plugins/browserpaw/settings").json()["data"]["expected_config"]
      == sha256_of(ROOT_CFG),
      str(sha256_of(ROOT_CFG)))

# ---------------------------------------------------------------------------
print("[11] 没有 config.yaml 的 agent：读 200 空结构 / 写 400")
r = client.get(PREFIX + "/agents/gamma/plugins")
j = r.json()
print("        GET /agents/gamma/plugins ->", r.status_code, brief(j.get("data"), 300))
check("没有 config.yaml 的 agent：GET plugins → 200（不是 404）",
      r.status_code == 200 and j.get("ok") is True, str(r.status_code))
# M5a 口径变更（本卡有意改的）：清单 = catalog ∪ enabled ∪ disabled ∪ entries。
# gamma 没有 config.yaml ⇒ 自己的清单为空，但 catalog 里的自制插件**仍然列出**（否则界面上
# 连"给它启用"的入口都没有）。它们一个都不许显示成 enabled/disabled——没有名单就是 unavailable。
gamma_items = j["data"]["plugins"]
gamma_ids = {item["plugin_id"] for item in gamma_items}
print("        gamma ids :", sorted(gamma_ids))
check("★没有 config.yaml 的 agent 也会列出 catalog 里的自制插件（否则无法启用）",
      gamma_ids == set(core_catalog.catalog_ids()),
      "listed=%d catalog=%d 多=%s 缺=%s"
      % (len(gamma_ids), len(core_catalog.catalog_ids()),
         sorted(gamma_ids - set(core_catalog.catalog_ids())),
         sorted(set(core_catalog.catalog_ids()) - gamma_ids)))
check("★没有名单 ⇒ 每一项都必须是 unavailable（绝不因为\"在 catalog 里\"就显示成 enabled）",
      bool(gamma_items) and all(item["state"] == "unavailable" for item in gamma_items),
      brief(sorted({item["state"] for item in gamma_items})))
check("本 agent 自己的那份清单为空（counts 的 enabled/disabled 都是 0）",
      j["data"]["counts"]["enabled"] == 0 and j["data"]["counts"]["disabled"] == 0
      and j["data"]["counts"]["unavailable"] == len(gamma_items),
      brief(j["data"]["counts"]))
check("notes 说明「没有自己的 config.yaml」（不是静默空表）",
      any("config.yaml" in note for note in (j["data"]["notes"] or [])),
      brief(j["data"]["notes"], 300))
r = client.get(PREFIX + "/agents/gamma/plugins/browserpaw/settings")
j = r.json()
print("        GET /agents/gamma/plugins/browserpaw/settings ->", r.status_code, brief(j.get("data"), 320))
check("读 settings → 200 空结构（不是 404）",
      r.status_code == 200 and j.get("data", {}).get("exists") is False, str(r.status_code))
check("空结构里 settings 是 {}、sources 是 {}、masked_keys 是 []",
      j["data"]["settings"] == {} and j["data"]["sources"] == {} and j["data"]["masked_keys"] == [],
      brief({k: j["data"][k] for k in ("settings", "sources", "masked_keys")}))
r = client.put(PREFIX + "/agents/gamma/plugins/browserpaw/settings",
               json={"key": "timeout", "value": 1})
j = r.json()
print("        PUT /agents/gamma/... ->", r.status_code, brief(j, 300))
check("★写 → 400 config_missing 并说明原因",
      r.status_code == 400 and err_code(r) == "config_missing", "%s %s" % (r.status_code, err_code(r)))
check("说明里点名缺哪个文件", str(GAMMA_DIR / "config.yaml") in (j.get("error", {}).get("message") or ""),
      brief(j.get("error", {}).get("message"), 240))
check("gamma 目录没有因此被创建出 config.yaml", not (GAMMA_DIR / "config.yaml").exists())

# ---------------------------------------------------------------------------
print("[12] 口径交叉校验：hermes-console 走 effective_settings，其它插件走同口径合并")
d = client.get(PREFIX + "/agents/alpha/plugins/hermes-console/settings").json()["data"]
print("        GET alpha/hermes-console/settings ->", brief({k: d.get(k) for k in ("settings", "sources")}, 400))
expected = core_settings.effective_settings("alpha", None, None)
check("★本插件的 sources 与 effective_settings 的 source 逐键一致（权威路径，未另写一套）",
      d.get("sources") == {key: entry["source"] for key, entry in expected.items()},
      "响应=%s" % brief(d.get("sources"), 240))
orig_plugin_id = core_settings.PLUGIN_ID
try:
    # 把 PLUGIN_ID 换成一个别名，本模块就会走\"通用插件\"那条合并分支；
    # 同一份配置下两条路径必须逐键同值、同来源 —— 否则就是两套合并规则在打架。
    core_settings.PLUGIN_ID = "hermes-console-alias"
    generic = console_settings._layered_sources("alpha", "hermes-console", {})
finally:
    core_settings.PLUGIN_ID = orig_plugin_id
check("★通用合并分支与 effective_settings 逐键同值同来源（同一口径，不是第二套规则）",
      {key: entry["value"] for key, entry in generic.items()} == {key: entry["value"] for key, entry in expected.items()}
      and {key: entry["source"] for key, entry in generic.items()} == {key: entry["source"] for key, entry in expected.items()},
      "generic=%s / effective=%s" % (brief(sorted(generic.items())[:2], 200), brief(sorted(expected.items())[:2], 200)))

# ---------------------------------------------------------------------------
print("[12b] /agents/{id}/modules 是薄封装转发（不是重复实现）")
r = client.get(PREFIX + "/agents/default/modules")
j = r.json()
print("        GET /agents/default/modules ->", r.status_code, brief(j, 300))
check("GET modules → 200 且 agent 正确", r.status_code == 200 and j["data"]["agent"] == "default",
      str(r.status_code))
r2 = client.get(PREFIX + "/modules?agent=default")
check("★与 /modules?agent=default 响应完全一致（真的是转发）", r.json() == r2.json(),
      "转发体与聚合入口不同")
r3 = client.get(PREFIX + "/agents/xiaomo/modules")
check("别的 agent 也走同一条转发", r3.status_code == 200 and r3.json()["data"]["agent"] == "xiaomo",
      str(r3.status_code))
check("非法 agent 的 modules 请求 → 400（与其它端点口径一致）",
      client.get(PREFIX + "/agents/..%2F..%2Fetc/modules").status_code == 400)

# ---------------------------------------------------------------------------
print("[13] 真实 home 只读真打（不写任何真实配置）：default vs xiaomo")
os.environ["HERMES_HOME"] = str(REAL_HOME)
real_home_now = core_scope.hermes_home()
check("HERMES_HOME 复位后 home 解析回到真实 home",
      Path(real_home_now).resolve() == REAL_HOME.resolve(), str(real_home_now))
r = client.get(PREFIX + "/agents/default/plugins")
j = r.json()
real_states = {item["plugin_id"]: item["state"] for item in (j.get("data") or {}).get("plugins") or []}
# catalog 里有、但 default 自己配置里完全没有的插件：它们被判 unavailable 是正确行为
# （内联实现，不依赖本文件后面才定义的 config_plugin_ids）
_default_cfg = load_yaml(REAL_HOME / "config.yaml") if (REAL_HOME / "config.yaml").is_file() else {}
_dp = (_default_cfg.get("plugins") or {}) if isinstance(_default_cfg, dict) else {}
_default_ids = set((_dp.get("entries") or {}).keys())
for _k in ("enabled", "disabled"):
    _default_ids |= {v for v in (_dp.get(_k) or []) if isinstance(v, str)}
CATALOG_ONLY_IDS = set(core_catalog.catalog_ids(home=REAL_HOME)) - _default_ids
print("        catalog-only（default 无此插件）:", sorted(CATALOG_ONLY_IDS))
print("        GET /agents/default/plugins (real) ->", r.status_code, brief((j.get("data") or {}).get("counts")))
check("真实 default：GET plugins → 200 且 state 全在三态内",
      r.status_code == 200
      and bool(real_states)
      and set(real_states.values()) <= {"enabled", "disabled", "unavailable"},
      brief(sorted(set(real_states.values()))))
print("        真实 default 三态样本 :",
      brief({name: real_states[name] for name in list(real_states)[:6]}, 320))
r = client.get(PREFIX + "/agents/xiaomo/plugins")
j = r.json()
real_x_states = {item["plugin_id"]: item["state"] for item in (j.get("data") or {}).get("plugins") or []}
print("        GET /agents/xiaomo/plugins (real) ->", r.status_code, brief((j.get("data") or {}).get("counts")))
check("真实 xiaomo：GET plugins → 200", r.status_code == 200, str(r.status_code))
check("★真实数据上 xiaomo 与 default 也不同（不只是假 home）",
      set(real_x_states) != set(real_states),
      "只在 xiaomo=%d / 只在 default=%d" % (len(set(real_x_states) - set(real_states)),
                                            len(set(real_states) - set(real_x_states))))
print("        真实 xiaomo 三态样本 :",
      brief({name: real_x_states[name] for name in list(real_x_states)[:6]}, 320))
check("★真实 default 的 unavailable 只出现在 catalog 独有（本 profile 无此插件）的条目上，绝不伪装成 enabled",
      set(real_states.values()) <= {"enabled", "disabled", "unavailable"}
      and all(v != "enabled" for k, v in real_states.items() if k in CATALOG_ONLY_IDS),
      "default=%s xiaomo=%s | catalog-only 被判 enabled 的=%s"
      % (sorted(set(real_states.values())), sorted(set(real_x_states.values())),
         sorted(k for k in CATALOG_ONLY_IDS if real_states.get(k) == "enabled")))
# 2026-10-29：catalog 已扩到含「只存在于某 profile」的插件（apm-worktime / meme-library /
# microsoft-todo / xiaomo-vitality 等）。因此真实 default 出现 unavailable 是**正确行为**
# ——它在 default 的 enabled/disabled/entries 里确实都没有。旧断言「default 里没有 unavailable」
# 的前提已随 catalog 扩容失效，改为只验核心不变量：这些 catalog-only 条目不得被说成 enabled。


def config_plugin_ids(path: Path) -> set:
    plugins = (load_yaml(path).get("plugins") or {}) if path.is_file() else {}
    ids = set((plugins.get("entries") or {}).keys())
    for key in ("enabled", "disabled"):
        ids |= {v for v in (plugins.get(key) or []) if isinstance(v, str)}
    return ids


real_default_cfg = REAL_HOME / "config.yaml"
real_xiaomo_cfg = REAL_HOME / "profiles" / "xiaomo" / "config.yaml"
real_catalog_ids = set(core_catalog.catalog_ids())
check("★真实 default：清单 == config.yaml 里 entries ∪ enabled ∪ disabled ∪ catalog（不静默丢项）",
      set(real_states) == (config_plugin_ids(real_default_cfg) | real_catalog_ids),
      "缺=%s" % sorted((config_plugin_ids(real_default_cfg) | real_catalog_ids) - set(real_states)))
check("★真实 xiaomo：清单 == config.yaml 里 entries ∪ enabled ∪ disabled ∪ catalog（不静默丢项）",
      set(real_x_states) == (config_plugin_ids(real_xiaomo_cfg) | real_catalog_ids),
      "缺=%s" % sorted((config_plugin_ids(real_xiaomo_cfg) | real_catalog_ids) - set(real_x_states)))
print("        catalog ids (%d) : %s" % (len(real_catalog_ids), sorted(real_catalog_ids)))
check("★真实 default 的三态：清单里每一项的 state 都与 plugin_switch 逐项一致",
      all(item_state["state"] == core_switch.plugin_state("default", item_state["plugin_id"],
                                                          tri_state=True)["state"]
          for item_state in (client.get(PREFIX + "/agents/default/plugins").json()["data"]["plugins"])),
      "逐项比对")
r = client.get(PREFIX + "/agents/xiaomo/plugins/web%2Ftavily/settings")
j = r.json()
print("        真实 xiaomo web/tavily settings ->", r.status_code,
      brief({k: (j.get("data") or {}).get(k) for k in ("masked_keys", "expected_config")}, 300))
check("真实数据上读 settings 也 200（嵌套插件 id web/tavily 可达）",
      r.status_code == 200, "%s %s" % (r.status_code, brief(j, 200)))
check("真实 default 的 expected_config == 真实根 config.yaml 的 sha256",
      client.get(PREFIX + "/agents/default/plugins/browserpaw/settings").json()["data"]["expected_config"]
      == sha256_of(REAL_HOME / "config.yaml"),
      str(sha256_of(REAL_HOME / "config.yaml")))
check("真实 xiaomo 的 expected_config == 真实 profiles/xiaomo/config.yaml 的 sha256",
      client.get(PREFIX + "/agents/xiaomo/plugins/browserpaw/settings").json()["data"]["expected_config"]
      == sha256_of(REAL_HOME / "profiles" / "xiaomo" / "config.yaml"),
      str(sha256_of(REAL_HOME / "profiles" / "xiaomo" / "config.yaml")))
r = client.put(PREFIX + "/agents/..%2F..%2Fetc/plugins/browserpaw/settings", json={"key": "a", "value": 1})
check("真实 home 下非法 agent 的写请求也只是 400（不会碰任何文件）",
      r.status_code == 400, str(r.status_code))

# ---------------------------------------------------------------------------
print("[14] ★生产零污染 · 后置快照")
prod_after = production_snapshot(REAL_HOME)
print_snapshot("真实配置 after", prod_after)
changed = {path: (prod_before.get(path), prod_after.get(path))
           for path in set(prod_before) | set(prod_after)
           if prod_before.get(path) != prod_after.get(path)}
check("★真实 config.yaml + profiles/*/config.yaml 前后 sha256 完全一致（零污染）",
      not changed, dumps(changed) if changed else "全部一致，%d 个文件" % len(prod_after))
check("前后文件集合一致（没有新增/删除配置文件）", set(prod_before) == set(prod_after))

# ---------------------------------------------------------------------------
shutil.rmtree(fake_home, ignore_errors=True)
print("        沙盒已清理 :", fake_home)

print()
if failures:
    print("失败 %d 条：" % len(failures))
    for item in failures:
        print("  - " + item)
    print("CONSOLE-SETTINGS-API-PROBE-FAILED")
    raise SystemExit(1)

print("全部检查通过（三态显式 + 按 agent 取 + 脱敏带来源 + 拒掩码 + 白名单 + 只读保护 + "
      "合并语义 + 按 agent 的乐观锁 + 缺配置语义 + 转发 + %d 个真实配置文件零污染）" % len(prod_after))
print("CONSOLE-SETTINGS-API-PROBE-OK")
raise SystemExit(0)

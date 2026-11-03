# -*- coding: utf-8 -*-
"""hermes-console · M5a 自制插件目录（catalog）验收探针（唯一验收依据）。

跑法（**不要经管道拿退出码**）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        plugins/hermes-console/scripts/probe_plugin_catalog.py
    echo "exit=$?"        # bash；PowerShell 用 $LASTEXITCODE

覆盖卡片 9 条::

    [1] catalog 加载       plugins_catalog.json 能读，含老板点名的 advisor / hermes-auto-titler；
                           catalog 里**没有** version 字段（版本必须来自 plugin.yaml，不许写死）
    [2] ★核心修复          merged_plugin_ids('apm-worker') 含 advisor 与 hermes-auto-titler
                           （改前这两个在该 agent 是\"未列出\"——看不见就没法启用）
    [3] 并集语义           merged == catalog ∪ enabled ∪ disabled ∪ entries：
                           假 home 造 entries/enabled/disabled 三处各自的 catalog 外插件，断言全在并集里
    [4] ★catalog 坏掉不崩  非法 JSON → load_catalog 不抛异常、返回空目录 + 原因，
                           merged_plugin_ids 仍返回该 agent 原有插件
    [5] 版本以 yaml 为准   advisor → 1.3.0、hermes-auto-titler → 0.3.0（读 plugins/<id>/plugin.yaml）；
                           不存在的插件 → None（不猜）
    [6] 第三方仍在列       browserpaw 不在 catalog（in_catalog=false），但仍出现在 default 的清单里
    [7] 命名空间          GET /agents/default/plugins 每项含 ref 且以 plugin: 开头；顶层有 namespaces
    [8] 三态仍权威         advisor 在 default 是 enabled、在 apm-worker 是 unavailable；
                           每项 state 与 plugin_switch.plugin_state(tri_state=True) 逐项一致
    [9] ★生产零污染       真实 config.yaml + profiles/*/config.yaml 开头/结尾 sha256 全一致

外加：路由总数仍 41、``/health`` 的 ``failed`` 仍为空（本卡不新增端点，只换数据源）。

**本探针不写任何真实配置**：假 home 是 tempfile，真 home 只读（第 9 条会抓）。
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

#: 老板点名的两个插件（本卡存在的理由）。
NAMED = ("advisor", "hermes-auto-titler")

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


def brief(value, n: int = 300) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    return text if len(text) <= n else text[:n] + "…"


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
print("[0] 环境")
print("        python   :", sys.version.split()[0])
print("        plugin   :", PLUGIN_ROOT)
print("        real home:", REAL_HOME)

if not (REAL_HOME / "config.yaml").is_file():
    check("真实 home 可定位（<home>/config.yaml 存在）", False, str(REAL_HOME))
    print("PLUGIN-CATALOG-PROBE-FAILED")
    raise SystemExit(1)

# ---------------------------------------------------------------------------
print("[9-pre] 生产零污染 · 前置快照（在任何 import / 任何写入之前）")
prod_before = production_snapshot(REAL_HOME)
print_snapshot("真实配置 before", prod_before)
check(
    "前置快照非空且无读取错误",
    bool(prod_before) and all(not v.startswith("<error:") for v in prod_before.values()),
)

if HERMES_AGENT.is_dir() and str(HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT))
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

# 函数级检查一律显式传 home=REAL_HOME（不依赖进程 HERMES_HOME）；HTTP 段才动环境变量。
os.environ["HERMES_HOME"] = str(REAL_HOME)

from dashboard.core import plugin_catalog as catalog_mod  # noqa: E402
from dashboard.core import plugin_switch as switch_mod  # noqa: E402
from dashboard.core import settings as settings_mod  # noqa: E402

# ---------------------------------------------------------------------------
print("[1] catalog 能加载，且含老板点名的两个插件")
catalog = catalog_mod.load_catalog(home=REAL_HOME)
print("        catalog path   :", catalog["path"])
print("        schema_version :", catalog["schema_version"], "| ok:", catalog["ok"],
      "| entries:", len(catalog["plugins"]))
check("catalog 加载成功（ok=True，无 error）",
      catalog["ok"] is True and not catalog["error"], str(catalog["error"]))
check("schema_version == 1", catalog["schema_version"] == 1, str(catalog["schema_version"]))
ids = catalog_mod.catalog_ids(home=REAL_HOME)
print("        catalog ids (%d) : %s" % (len(ids), ids))
check("★catalog 含老板点名的 advisor", "advisor" in ids)
check("★catalog 含老板点名的 hermes-auto-titler", "hermes-auto-titler" in ids)
check("catalog 含其余自制插件（按 author 判定：小白/王铁军/local/Hermes Agent/无 author）",
      {"hermes-auto-group", "hermes-remote", "quick-send", "cua-ops", "model-switch",
       "file-policy", "xiaomo-media-delivery-guard", "douyin", "pmail",
       "weather", "hermes-vcc-cn"} <= set(ids),
      "缺=%s" % sorted({"hermes-auto-group", "hermes-remote", "quick-send", "cua-ops",
                       "model-switch", "file-policy",
                       "xiaomo-media-delivery-guard", "douyin", "pmail", "weather",
                       "hermes-vcc-cn"} - set(ids)))
# default-guard 曾在此清单里。2026-10-29 发现它的插件目录与 config 条目都被移除
# （plugins/default-guard/ 不存在、config.yaml 的 plugins.enabled 里也没有），
# catalog 里继续保留它只会让设置界面显示一个不存在的插件，故一并移除。
# 若日后重新装回该插件，把它加回本清单与 plugins_catalog.json 即可。
check("★明确排除的第三方插件一个都不在 catalog",
      not ({"browserpaw", "hades-coding-workflows", "hermes-switchyard", "plan-mode",
            "hindsight"} & set(ids)),
      "混入=%s" % sorted({"browserpaw", "hades-coding-workflows", "hermes-switchyard",
                          "plan-mode", "hindsight"} & set(ids)))
check("catalog 的 dropped 为空（所有 id 都合文法）", catalog["dropped"] == [],
      brief(catalog["dropped"]))
# 版本不许写死在 catalog 里：写死就会与 plugin.yaml 漂移，第 5 条专门查 yaml。
check("★catalog 里没有任何一条写死 version（版本必须运行时读 plugin.yaml）",
      not [pid for pid, entry in catalog["plugins"].items() if "version" in entry],
      brief([pid for pid, entry in catalog["plugins"].items() if "version" in entry]))
entry_advisor = catalog_mod.catalog_entry("advisor", home=REAL_HOME) or {}
print("        catalog[advisor] :", brief(entry_advisor))
check("catalog[advisor] 带 display_name / kind / source / module / has_dashboard_module 字段",
      {"display_name", "kind", "source", "has_dashboard_module"} <= set(entry_advisor),
      brief(sorted(entry_advisor)))
check("catalog[advisor].module == hermes-console/advisor（限定 ref，不是裸 id）",
      entry_advisor.get("module") == "hermes-console/advisor", str(entry_advisor.get("module")))
check("catalog_entry 对不在 catalog 的 id 返回 None（不猜）",
      catalog_mod.catalog_entry("browserpaw", home=REAL_HOME) is None
      and catalog_mod.catalog_entry("", home=REAL_HOME) is None
      and catalog_mod.catalog_entry("../etc", home=REAL_HOME) is None)

# ---------------------------------------------------------------------------
print("[2] ★核心修复：merged_plugin_ids('apm-worker') 能看到两个点名插件")
apm_merged = catalog_mod.merged_plugin_ids("apm-worker", home=REAL_HOME)
apm_config_ids = settings_mod.list_plugin_ids("apm-worker", home=REAL_HOME)
print("        apm-worker config 里的插件（改前的全部可见范围）:", apm_config_ids)
print("        apm-worker merged (%d) :" % len(apm_merged), apm_merged)
check("★merged_plugin_ids('apm-worker') 含 advisor",
      "advisor" in apm_merged, "改前 config 里没有它：%s" % apm_config_ids)
check("★merged_plugin_ids('apm-worker') 含 hermes-auto-titler",
      "hermes-auto-titler" in apm_merged, "改前 config 里没有它：%s" % apm_config_ids)
check("改前口径能看见的插件集合是 merged 的真子集（不是换了个清单，而是扩大了）",
      set(apm_config_ids) <= set(apm_merged),
      "丢=%s" % sorted(set(apm_config_ids) - set(apm_merged)))
check("merged 里这些插件的三态都是 unavailable（没在名单里 ≠ 已启用）",
      all(switch_mod.plugin_state("apm-worker", pid, home=REAL_HOME,
                                  tri_state=True)["state"] == "unavailable" for pid in NAMED),
      brief({pid: switch_mod.plugin_state("apm-worker", pid, home=REAL_HOME, tri_state=True)
             for pid in NAMED}))
for agent in ("default", "xiaomo", "media-worker", "apm-worker"):
    merged = catalog_mod.merged_plugin_ids(agent, home=REAL_HOME)
    print("        %-13s 点名插件可见: advisor=%s titler=%s (共 %d 项)"
          % (agent, "advisor" in merged, "hermes-auto-titler" in merged, len(merged)))
    check("★%s 的清单里 advisor 与 hermes-auto-titler 都可见" % agent,
          set(NAMED) <= set(merged))

# ---------------------------------------------------------------------------
print("[3] 并集语义：merged == catalog ∪ enabled ∪ disabled ∪ entries（假 home）")
fake_home = Path(tempfile.mkdtemp(prefix="hc-plugin-catalog-probe-"))
(fake_home / "profiles" / "fakeagent").mkdir(parents=True)
fake_cfg = {
    "plugins": {
        "enabled": ["fake-enabled-only"],
        "disabled": ["fake-disabled-only"],
        "entries": {"fake-entry-only": {"settings": {"x": 1}}},
    }
}
import yaml  # noqa: E402  假 fixture 用；真实配置全程不写

(fake_home / "config.yaml").write_text(
    yaml.safe_dump(fake_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
(fake_home / "profiles" / "fakeagent" / "config.yaml").write_text(
    yaml.safe_dump(fake_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
print("        fake home :", fake_home, "（没有 <home>/plugins_catalog.json ⇒ 用打包份）")

fake_catalog = catalog_mod.load_catalog(home=fake_home)
check("假 home 没有部署级 catalog 时回落到随插件打包的那份（不是空目录）",
      fake_catalog["ok"] is True and len(fake_catalog["plugins"]) == len(catalog["plugins"]),
      "%s entries=%d" % (fake_catalog["path"], len(fake_catalog["plugins"])))
fake_merged = catalog_mod.merged_plugin_ids("fakeagent", home=fake_home)
fake_own = settings_mod.list_plugin_ids("fakeagent", home=fake_home)
print("        fakeagent 自己配置里的 :", fake_own)
print("        fakeagent merged (%d) :" % len(fake_merged), fake_merged)
check("假 agent 的 enabled/disabled/entries 三处项都在并集里（catalog 外也不许丢）",
      {"fake-enabled-only", "fake-disabled-only", "fake-entry-only"} <= set(fake_merged),
      "缺=%s" % sorted({"fake-enabled-only", "fake-disabled-only", "fake-entry-only"}
                      - set(fake_merged)))
check("★catalog 里的插件也在并集里（即 catalog ∪ 自己那份，不是二选一）",
      set(catalog_mod.catalog_ids(home=fake_home)) <= set(fake_merged))
check("★merged == catalog ∪ enabled ∪ disabled ∪ entries（逐项完全相等，多一个少一个都算错）",
      set(fake_merged) == set(catalog_mod.catalog_ids(home=fake_home)) | set(fake_own),
      "多=%s 缺=%s" % (sorted(set(fake_merged) - (set(catalog_mod.catalog_ids(home=fake_home)) | set(fake_own))),
                      sorted((set(catalog_mod.catalog_ids(home=fake_home)) | set(fake_own)) - set(fake_merged))))
check("结果去重且排序（同一个 id 不会出现两次）",
      fake_merged == sorted(set(fake_merged)), brief(fake_merged[:5]))

# 部署级覆盖：<home>/plugins_catalog.json 存在时必须用它（老板改这一份就能加插件）
override = fake_home / catalog_mod.CATALOG_FILENAME
override.write_text(json.dumps({
    "schema_version": 1,
    "plugins": {"fake-catalog-plugin": {"display_name": "Fake", "kind": "plugin",
                                        "source": "self-developed"}},
}, ensure_ascii=False), encoding="utf-8")
overridden = catalog_mod.load_catalog(home=fake_home)
check("部署级 <home>/plugins_catalog.json 优先于打包份（老板能自己改目录）",
      overridden["ok"] is True and catalog_mod.catalog_ids(home=fake_home) == ["fake-catalog-plugin"],
      "%s → %s" % (overridden["path"], sorted(overridden["plugins"])))
check("覆盖后 merged 仍含该 agent 自己配置里的三项（catalog 换了也不丢本地插件）",
      {"fake-enabled-only", "fake-disabled-only", "fake-entry-only"}
      <= set(catalog_mod.merged_plugin_ids("fakeagent", home=fake_home)))

# ---------------------------------------------------------------------------
print("[4] ★catalog 坏掉（非法 JSON）→ 不抛异常、空目录 + 原因、原有插件仍在")
override.write_text("{ 这不是 JSON", encoding="utf-8")
raised = None
try:
    broken = catalog_mod.load_catalog(home=fake_home)
except Exception as exc:  # noqa: BLE001 - 探针要证明它不抛
    raised = exc
    broken = {}
print("        load_catalog(坏 JSON) ->", "raised %s" % raised if raised else brief(broken))
check("★catalog 是非法 JSON 时 load_catalog 不抛异常", raised is None, repr(raised))
check("★返回空目录（plugins={}）+ ok=False + 非空 error 说明原因",
      broken.get("ok") is False and broken.get("plugins") == {} and bool(broken.get("error")),
      brief({"ok": broken.get("ok"), "plugins": broken.get("plugins"),
             "error": broken.get("error")}, 300))
check("坏 catalog 下 catalog_ids 是 []、catalog_entry 是 None（不猜、不半残）",
      catalog_mod.catalog_ids(home=fake_home) == []
      and catalog_mod.catalog_entry("advisor", home=fake_home) is None)
broken_merged = catalog_mod.merged_plugin_ids("fakeagent", home=fake_home)
print("        坏 catalog 下 merged :", broken_merged)
check("★坏 catalog 下 merged_plugin_ids 仍返回该 agent 原有插件（catalog 坏不能拖垮设置界面）",
      broken_merged == sorted(set(fake_own)), "%s vs %s" % (broken_merged, sorted(set(fake_own))))
override.write_text("[]", encoding="utf-8")
non_object = catalog_mod.load_catalog(home=fake_home)
check("catalog 根不是对象（[]）也走同一条\"空目录 + 原因\"路径",
      non_object["ok"] is False and non_object["plugins"] == {} and bool(non_object["error"]),
      brief(non_object.get("error"), 200))
override.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
no_plugins = catalog_mod.load_catalog(home=fake_home)
check("catalog 缺 'plugins' 对象同样不抛（空目录 + 原因）",
      no_plugins["ok"] is False and bool(no_plugins["error"]), brief(no_plugins.get("error"), 200))
override.unlink()
missing = catalog_mod.load_catalog(home=fake_home)
check("catalog 文件整个缺失时不抛（回落打包份，ok=True）",
      missing["ok"] is True and len(missing["plugins"]) == len(catalog["plugins"]),
      "%s entries=%d" % (missing["path"], len(missing["plugins"])))
# 彻底没有 catalog 的极端情况：把打包份也指向不存在的路径
original_bundled = catalog_mod.BUNDLED_CATALOG
try:
    catalog_mod.BUNDLED_CATALOG = fake_home / "no-such-catalog.json"
    empty_home = Path(tempfile.mkdtemp(prefix="hc-plugin-catalog-empty-"))
    (empty_home / "profiles" / "solo").mkdir(parents=True)
    solo_cfg = {"plugins": {"enabled": ["solo-plugin"]}}
    solo_path = empty_home / "profiles" / "solo" / "config.yaml"
    solo_path.write_text(yaml.safe_dump(solo_cfg, allow_unicode=True), encoding="utf-8")
    (empty_home / "config.yaml").write_text(
        yaml.safe_dump(solo_cfg, allow_unicode=True), encoding="utf-8")
    none_catalog = catalog_mod.load_catalog(home=empty_home)
    none_merged = catalog_mod.merged_plugin_ids("solo", home=empty_home)
finally:
    catalog_mod.BUNDLED_CATALOG = original_bundled
check("★打包份也不存在时 load_catalog 不抛、返回空目录 + '文件不存在' 原因",
      none_catalog["ok"] is False and none_catalog["plugins"] == {}
      and "不存在" in (none_catalog["error"] or ""), brief(none_catalog.get("error"), 200))
check("★该极端情况下 merged_plugin_ids 仍返回 agent 自己的插件",
      none_merged == ["solo-plugin"], str(none_merged))

# ---------------------------------------------------------------------------
print("[5] version 从 plugins/<id>/plugin.yaml 读（catalog 不写死）")
versions = {pid: catalog_mod.read_plugin_version(pid, home=REAL_HOME) for pid in NAMED}
print("        读到的版本 :", versions)
check("★advisor 的 version 读到 1.3.0", versions["advisor"] == "1.3.0", str(versions["advisor"]))
check("★hermes-auto-titler 的 version 读到 0.3.0",
      versions["hermes-auto-titler"] == "0.3.0", str(versions["hermes-auto-titler"]))
check("version 与 plugin.yaml 原文逐字一致（不是 catalog 里抄的，也不做规范化）",
      all(versions[pid] == str(yaml.safe_load(
          (REAL_HOME / "plugins" / pid / "plugin.yaml").read_text(encoding="utf-8-sig"))["version"])
          for pid in NAMED),
      brief(versions))
check("不存在的插件 → None（不猜、不返回空串）",
      catalog_mod.read_plugin_version("no-such-plugin-xyz", home=REAL_HOME) is None
      and catalog_mod.read_plugin_version("../etc", home=REAL_HOME) is None
      and catalog_mod.read_plugin_version("", home=REAL_HOME) is None)
check("catalog 里全部自制插件的 version 都能从 plugin.yaml 读到（没有 None）",
      all(catalog_mod.read_plugin_version(pid, home=REAL_HOME) for pid in ids),
      brief({pid: catalog_mod.read_plugin_version(pid, home=REAL_HOME) for pid in ids}))

# ---------------------------------------------------------------------------
print("[6] 第三方插件不因 catalog 而消失：browserpaw 不在 catalog，但仍在 default 的清单里")
default_merged = catalog_mod.merged_plugin_ids("default", home=REAL_HOME)
print("        default merged 里 browserpaw :", "browserpaw" in default_merged)
check("browserpaw 不在 catalog（catalog_ids 不含它）", "browserpaw" not in catalog_mod.catalog_ids(home=REAL_HOME))
check("★browserpaw 仍出现在 default 的 merged 清单里（第三方按 enabled∪disabled∪entries 照旧列出）",
      "browserpaw" in default_merged)
check("browserpaw 的 catalog_entry 是 None ⇒ 响应里 in_catalog=false（第 7 条在 HTTP 上复核）",
      catalog_mod.catalog_entry("browserpaw", home=REAL_HOME) is None)

# ---------------------------------------------------------------------------
print("[7][8] HTTP 真打：GET /agents/{default,apm-worker}/plugins + /health")
api_spec = importlib.util.spec_from_file_location(
    "hermes_dashboard_plugin_hermes-console", DASHBOARD / "plugin_api.py")
api_mod = importlib.util.module_from_spec(api_spec)  # type: ignore[arg-type]
sys.modules[api_spec.name] = api_mod  # type: ignore[union-attr]
api_spec.loader.exec_module(api_mod)  # type: ignore[union-attr]

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app = FastAPI()
app.include_router(api_mod.router, prefix=PREFIX)
client = TestClient(app, raise_server_exceptions=False)

health = client.get(PREFIX + "/health")
hbody = health.json()
print("        /health :", health.status_code, brief({k: hbody.get(k) for k in ("route_count", "failed")}, 240))
check("GET /health → 200", health.status_code == 200, str(health.status_code))
check("路由总数 71（M4b0 的 41 + M4b2 的启停端点 + V2 升级分区的 6 条，替换 1 条占位 + V3 上下文引擎 6 条 + V4 远端后端 5 条 + V5 模型 13 条；本探针不新增端点）",
      hbody.get("route_count") == 71,
      str(hbody.get("route_count")))
check("failed 仍为空", hbody.get("failed") == {}, brief(hbody.get("failed")))
check("域模块 api_console_settings 未进 failed",
      "api_console_settings" in api_mod.DOMAIN_LOADED and not api_mod.DOMAIN_FAILURES,
      "loaded=%s failed=%s" % (api_mod.DOMAIN_LOADED, api_mod.DOMAIN_FAILURES))

resp = client.get(PREFIX + "/agents/default/plugins")
body = resp.json()
data = body.get("data") or {}
items = data.get("plugins") or []
by_id = {item["plugin_id"]: item for item in items}
print("        GET /agents/default/plugins ->", resp.status_code,
      "| %d 项" % len(items), brief(data.get("counts")))
check("GET /agents/default/plugins → 200 且 ok=true",
      resp.status_code == 200 and body.get("ok") is True, str(resp.status_code))
check("★每项都含 ref 且以 plugin: 开头",
      bool(items) and all(str(item.get("ref", "")).startswith("plugin:") for item in items),
      brief([item.get("ref") for item in items[:4]]))
check("ref 与 plugin_id 一一对应（plugin:<id>）",
      all(item["ref"] == "plugin:%s" % item["plugin_id"] for item in items),
      brief([(item["plugin_id"], item["ref"]) for item in items[:3]]))
check("★响应顶层有 namespaces 说明（区分 plugin: / module: 两套命名空间）",
      isinstance(data.get("namespaces"), dict)
      and data["namespaces"].get("plugin") == "plugin:<plugin_id>"
      and data["namespaces"].get("module") == "module:<console_module_id>",
      brief(data.get("namespaces"), 300))
check("★M4b0 的六个既有键一个没删（plugin_id/state/in_enabled/in_disabled/source/has_settings）",
      all({"plugin_id", "state", "in_enabled", "in_disabled", "source", "has_settings"} <= set(item)
          for item in items))
check("★M5a 新增字段齐全（display_name/kind/origin/version/has_dashboard_module/"
      "module/in_catalog/note/version_source/module_id/module_ref）",
      all({"display_name", "kind", "origin", "version", "version_source", "has_dashboard_module",
           "module", "module_id", "module_ref", "in_catalog", "note"} <= set(item)
          for item in items),
      brief(sorted(set(items[0]) - {"plugin_id"}) if items else []))
print("        advisor 条目 :", brief(by_id.get("advisor"), 500))
check("★advisor 在 default：state=enabled / in_catalog=true / version=1.3.0 / "
      "module=hermes-console/advisor / has_dashboard_module=true",
      by_id.get("advisor", {}).get("state") == "enabled"
      and by_id["advisor"]["in_catalog"] is True
      and by_id["advisor"]["version"] == "1.3.0"
      and by_id["advisor"]["version_source"] == "plugin.yaml"
      and by_id["advisor"]["module"] == "hermes-console/advisor"
      and by_id["advisor"]["module_ref"] == "module:hermes-console/advisor"
      and by_id["advisor"]["has_dashboard_module"] is True,
      brief(by_id.get("advisor"), 500))
check("★advisor 的 origin 是 self-developed、kind 是 plugin",
      by_id.get("advisor", {}).get("origin") == "self-developed"
      and by_id["advisor"]["kind"] == "plugin")
titler = by_id.get("hermes-auto-titler", {})
print("        hermes-auto-titler 条目 :", brief(titler, 400))
check("★hermes-auto-titler 在 default：in_catalog=true / version=0.3.0 / 无控制台面板",
      titler.get("in_catalog") is True and titler.get("version") == "0.3.0"
      and titler.get("has_dashboard_module") is False and titler.get("module") is None
      and titler.get("module_ref") is None,
      brief(titler, 400))
check("★browserpaw：in_catalog=false 但仍列出（第三方不因 catalog 消失）",
      by_id.get("browserpaw", {}).get("in_catalog") is False and "browserpaw" in by_id,
      brief(by_id.get("browserpaw"), 300))
check("★每项 state 与 plugin_switch.plugin_state(tri_state=True) 逐项一致（本层不自己判三态）",
      all(item["state"] == switch_mod.plugin_state("default", item["plugin_id"], home=REAL_HOME,
                                                   tri_state=True)["state"]
          for item in items),
      brief({pid: [item["state"], switch_mod.plugin_state("default", pid, home=REAL_HOME,
                                                          tri_state=True)["state"]]
             for pid, item in list(by_id.items())[:4]}))
check("counts 三种状态合计 == 清单长度",
      sum((data.get("counts") or {}).values()) == len(items),
      "%s vs %d" % (brief(data.get("counts")), len(items)))
check("响应里的 catalog 元信息：ok=true / entries>=14 / used>=14",
      (data.get("catalog") or {}).get("ok") is True
      and (data["catalog"].get("entries") or 0) >= 14
      and (data["catalog"].get("used") or 0) >= 14,
      brief(data.get("catalog"), 300))

apm_resp = client.get(PREFIX + "/agents/apm-worker/plugins")
apm_data = apm_resp.json().get("data") or {}
apm_by_id = {item["plugin_id"]: item for item in apm_data.get("plugins") or []}
print("        GET /agents/apm-worker/plugins ->", apm_resp.status_code,
      "| %d 项" % len(apm_by_id), brief(apm_data.get("counts")))
check("★HTTP 上 apm-worker 也能看到 advisor 与 hermes-auto-titler（本卡的核心修复）",
      {"advisor", "hermes-auto-titler"} <= set(apm_by_id),
      "advisor=%s titler=%s" % ("advisor" in apm_by_id, "hermes-auto-titler" in apm_by_id))
check("★advisor 在 apm-worker 是 unavailable（没在名单里 = 未启用，不许显示成 enabled）",
      apm_by_id.get("advisor", {}).get("state") == "unavailable",
      brief(apm_by_id.get("advisor"), 300))
check("★apm-worker 上其它项的三态也与 plugin_switch 逐项一致",
      all(item["state"] == switch_mod.plugin_state("apm-worker", item["plugin_id"], home=REAL_HOME,
                                                   tri_state=True)["state"]
          for item in apm_data.get("plugins") or []))
check("apm-worker 的 in_catalog 计数与 catalog used 一致（不是每条都标 in_catalog）",
      (apm_data.get("catalog") or {}).get("used")
      == sum(1 for item in apm_data["plugins"] if item["in_catalog"]),
      brief(apm_data.get("catalog"), 240))

# ---------------------------------------------------------------------------
print("[9-post] ★生产零污染 · 后置快照")
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
shutil.rmtree(empty_home, ignore_errors=True)
print("        沙盒已清理 :", fake_home, "|", empty_home)

print()
if failures:
    print("失败 %d 条：" % len(failures))
    for item in failures:
        print("  - " + item)
    print("PLUGIN-CATALOG-PROBE-FAILED")
    raise SystemExit(1)

print("全部检查通过（catalog 数据驱动 + 并集清单 + 坏 catalog 不崩 + 版本以 yaml 为准 + "
      "ref 命名空间 + 三态仍权威 + %d 个真实配置文件零污染）" % len(prod_after))
print("PLUGIN-CATALOG-PROBE-OK")
raise SystemExit(0)
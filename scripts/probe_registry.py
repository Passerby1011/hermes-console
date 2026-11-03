# -*- coding: utf-8 -*-
"""hermes-console · M1b 模块注册核心 验收探针（唯一验收依据）。

跑法（**不要经管道拿退出码**）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \\
        plugins/hermes-console/scripts/probe_registry.py
    echo $?          # bash；PowerShell 用 echo $LASTEXITCODE

覆盖（M1b 的 8 条 + M3 新增的 3 条）：

[1] ``discover_modules()`` 发现 **6** 个模块（overview / weather / agents / autogroup /
    advisor / upgrade），manifest 取值与 M3 卡片表格逐项一致、按 ``nav_order`` 排成
    ``[10,20,30,40,50,60]``；6 个模块的 ``api.py`` 薄封装两种导入方式（包上下文 / 裸名）
    都能拿到 ``router``，且**路由数与对应 ``api_*.py`` 完全一致**（薄封装而非重写）
[2] **坏模块隔离**：临时造 ``_probe_broken``（缺 ``id``）→ 它进 ``failed`` 且原因点名缺 ``id``，
    6 个真模块仍在 ``loaded``
[3] **删目录即下线**：临时造 ``_probe_temp`` → 被发现 → 删目录 → 不再被发现
[4] **启用集计算**：tempfile 假 home 里给假 agent ``probeagent`` 配
    ``modules.weather.enabled=false`` → ``enabled_modules`` 里没有 weather 且 ``reason`` 说清；
    同时验证三层优先级（module_default → global → agent）
[5] **依赖未满足**：``requires:["nonexistent"]`` 的模块不启用且 ``reason`` 点名缺哪个；
    依赖已安装但被关掉（``requires:["weather"]`` + weather 关闭）也不启用且点名是哪个依赖
[6] ``GET /modules?agent=default`` / ``?agent=xiaomo`` 真实 200、``agent`` 字段正确、``modules`` 非空
[7] ``GET /modules?agent=../../etc`` → **400**（不是 500），错误体带合法 id 列表
[8] 旧聚合回归：``GET /health`` 200 且 ``failed`` 为空、既有 36 条路由一条不少（总数 37）、
    ``failed`` 里不含 ``api_overview`` / ``api_weather``；M4b0 起总数 41（+4 条设置端点），
    M4b2 起总数 42（+1 条插件启停端点）
[9] **默认启用集（无任何 settings 覆盖）**：默认启用 = overview / weather / agents；
    autogroup / advisor / upgrade 默认关闭且 ``reason`` 说清是"模块默认关闭"
[10] 收尾：模块顶层恰好 6 个目录 + ``README.md``，无 ``_probe_*`` 残留

**临时目录纪律**：探针造的模块目录一律登记在 ``TEMP_DIRS``，``finally`` 里无条件删除
（异常也删），收工前再兜底扫一遍 ``_probe_*`` 残留。真实 ``config.yaml`` 全程只读：
假 agent 的配置写在 ``tempfile`` 假 home 里，通过 ``home=`` 参数注入，**不设 HERMES_HOME**。
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Dict, List

PLUGIN_ROOT = Path(__file__).resolve().parents[1]        # <home>/plugins/hermes-console
DASHBOARD = PLUGIN_ROOT / "dashboard"
MODULES = DASHBOARD / "modules"
REAL_HOME = PLUGIN_ROOT.parents[1]                        # <home>
HERMES_AGENT = REAL_HOME / "hermes-agent"
PREFIX = "/api/plugins/hermes-console"

# 探针会 import 每个模块的 api.py；不许在 dashboard/modules/*/ 里留下 __pycache__
# （验收要求模块目录里只有 manifest.json + api.py，别让探针自己弄脏）。
sys.dont_write_bytecode = True

for _extra in (DASHBOARD, PLUGIN_ROOT, HERMES_AGENT):
    if _extra.is_dir() and str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from dashboard.core import registry  # noqa: E402  (需要 PLUGIN_ROOT 在 sys.path)

#: 本卡之前既有的路由（M1b 只新增 ``GET /modules``，一条都不许少）。
BASELINE_ROUTES = (
    "DELETE /autogroup/rules",
    "GET /advisor",
    "GET /advisor/advice",
    "GET /advisor/advice/{advice_id}",
    "GET /advisor/settings",
    "GET /advisor/summary",
    "GET /agents",
    "GET /agents/overview",
    "GET /agents/{agent_id}/cron",
    "GET /agents/{agent_id}/sessions",
    "GET /autogroup/batches",
    "GET /autogroup/job/{job_id}",
    "GET /autogroup/rules",
    "GET /autogroup/status",
    "GET /autogroup/suggestions",
    "GET /health",
    "GET /overview",
    "GET /overview/analytics",
    "GET /overview/cron",
    "GET /overview/layout",
    "GET /overview/sessions",
    "GET /overview/system",
    # V2：升级分区的占位路由 ``GET /upgrade`` 被 6 条真实路由替换（42 - 1 + 6 = 47）。
    "GET /upgrade/readiness",
    "GET /upgrade/scan",
    "POST /upgrade/prepare",
    "POST /upgrade/restore",
    "POST /upgrade/wait",
    "POST /upgrade/kill",
    "GET /weather",
    "GET /weather/at",
    "GET /weather/daily",
    "GET /weather/hourly",
    "GET /weather/now",
    "POST /advisor/decision",
    "POST /autogroup/apply",
    "POST /autogroup/reject",
    "POST /autogroup/rules",
    "POST /autogroup/scan",
    "POST /autogroup/undo",
    "PUT /advisor/settings",
    "PUT /overview/layout",
)

#: M4b0 新增的 4 条（``dashboard/api_console_settings.py``）。卡片要求路由总数 37 → 41，
#: 所以这里从"只多了 GET /modules"改成"多了 GET /modules + 这 4 条"。断言口径不变：
#: 一条都不许少，总数必须精确等于 `既有 + 新增`。
M4B0_ROUTES = (
    "GET /agents/{agent_id:path}/plugins",
    "GET /agents/{agent_id:path}/plugins/{plugin_id:path}/settings",
    "PUT /agents/{agent_id:path}/plugins/{plugin_id:path}/settings",
    "GET /agents/{agent_id:path}/modules",
)

#: M4b2 新增的 1 条（``dashboard/api_console_settings.py``）：插件启停端点。
#: 路由总数 41 → 42。断言口径同上：这一条必须在，少一条就失败。
M4B2_ROUTES = (
    "PUT /agents/{agent_id:path}/plugins/{plugin_id:path}",
)

#: V3（上下文引擎 6 条，``dashboard/api_vcc.py``）—— 卡片给定，逐条列出。
V3_ROUTES = (
    "GET /vcc/overview",
    "GET /vcc/log",
    "GET /vcc/compaction",
    "POST /vcc/engine",
    "POST /vcc/search_archived",
    "GET /vcc/engine_status",
)

#: V4（远端后端 5 条，``dashboard/api_remote.py``）—— 本卡新增，逐条列出。
V4_ROUTES = (
    "GET /remote/agents",
    "GET /remote/status",
    "POST /remote/agent/start",
    "POST /remote/agent/stop",
    "GET /remote/credentials",
)

#: V5（模型分区 17 条，``dashboard/api_models.py``）—— 逐条列出。
#: t_3d4db435 追加 4 条批量端点（预览 / 写入 / 一键回滚 / 账本），13 → 17。
V5_ROUTES = (
    "GET /models/overview",
    "GET /models/health",
    "GET /models/providers",
    "GET /models/providers/{agent}",
    "GET /models/models",
    "GET /models/snapshots",
    "GET /models/model-library",
    "POST /models/apply",
    "POST /models/rollback",
    "GET /models/presets",
    "POST /models/presets",
    "PUT /models/presets/{name}",
    "DELETE /models/presets/{name}",
    "POST /models/batch/preview",
    "POST /models/batch/apply",
    "POST /models/batch/rollback",
    "GET /models/batches",
)

#: M3 之后应有的模块集合，按 ``nav_order`` 排序（id 顺序即 nav_order 顺序）。
#: M4b1 追加 ``settings``（nav_order 70，纯前端模块：没有 ``api_module`` / ``api.py``）。
#: V5 追加 ``models``（nav_order 25，「模型」分区：13 条 ``/models/*`` 路由，高危写操作默认关）。
EXPECTED_MODULE_IDS = ["overview", "weather", "models", "agents", "autogroup", "advisor", "upgrade",
                       "settings", "vcc", "remote"]
#: 对应的 ``nav_order``。
EXPECTED_NAV_ORDERS = [10, 20, 25, 30, 40, 50, 60, 70, 80, 90]
#: 纯前端模块（没有后端 ``api_module``，目录里只有 manifest.json，没有 api.py）。
FRONTEND_ONLY_IDS = ["settings"]
#: 「有 ``api_module`` 但**故意没有** ``api.py``」的模块：它们的 router 由
#: ``plugin_api.DOMAIN_MODULES`` 聚合（``dashboard/api_*.py`` 直接 include）。
#: 这类模块**不是**薄封装，所以下面跳过"api.py 路由数 == 实现路由数"那两条断言 ——
#: 但它们的 api_module 必须真的能 import 出 router（见 AGGREGATED 分支里单独断言）。
AGGREGATED_IDS = ["vcc", "remote", "models"]
#: 无覆盖时应当默认启用的模块（纯只读默认开）。设置界面本身默认可见 ⇒ 含 settings。
DEFAULT_ENABLED_IDS = ["overview", "weather", "agents", "settings"]
#: 无覆盖时应当默认关闭的模块（带写操作默认关）。
DEFAULT_DISABLED_IDS = ["models", "autogroup", "advisor", "upgrade", "vcc", "remote"]
#: manifest 取值表：M3 卡片给定的定值，逐项断言，防止"自己发挥"。
EXPECTED_MANIFEST: Dict[str, Dict[str, Any]] = {
    "overview": {"label": "概览", "nav_order": 10, "default_enabled": True,
                 "api_module": "api_overview", "section": "overview"},
    "weather": {"label": "天气", "nav_order": 20, "default_enabled": True,
                "api_module": "api_weather", "section": "weather"},
    "models": {"label": "模型", "nav_order": 25, "default_enabled": False,
               "api_module": "api_models", "section": "models"},
    "agents": {"label": "Agent 总览", "nav_order": 30, "default_enabled": True,
               "api_module": "api_agents", "section": "agents"},
    "autogroup": {"label": "项目分组", "nav_order": 40, "default_enabled": False,
                  "api_module": "api_autogroup", "section": "autogroup"},
    "advisor": {"label": "Advisor", "nav_order": 50, "default_enabled": False,
                "api_module": "api_advisor", "section": "advisor"},
    "upgrade": {"label": "升级", "nav_order": 60, "default_enabled": False,
                "api_module": "api_upgrade", "section": "upgrade"},
    "settings": {"label": "设置", "nav_order": 70, "default_enabled": True,
                 "api_module": None, "section": "settings"},
    "vcc": {"label": "上下文引擎", "nav_order": 80, "default_enabled": False,
            "api_module": "api_vcc", "section": "vcc"},
    "remote": {"label": "远端后端", "nav_order": 90, "default_enabled": False,
               "api_module": "api_remote", "section": "remote"},
}

failures: List[str] = []
passes = 0
#: 探针造的临时模块目录（``finally`` 里无条件删除）。
TEMP_DIRS: List[Path] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    global passes
    print(("  PASS  " if ok else "  FAIL  ") + label + (" — " + detail if detail else ""))
    if ok:
        passes += 1
    else:
        failures.append(label + (" — " + detail if detail else ""))
    return ok


def brief(obj: Any, n: int = 240) -> str:
    text = json.dumps(obj, ensure_ascii=False, default=str).replace("\n", " ")
    return text[:n] + ("…" if len(text) > n else "")


def make_module(dir_name: str, manifest: Any) -> Path:
    """在 ``dashboard/modules/`` 下造一个临时模块目录（内容原样写，``manifest`` 可以是坏 JSON 字符串）。"""
    path = MODULES / dir_name
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    payload = manifest if isinstance(manifest, str) else json.dumps(manifest, ensure_ascii=False, indent=2)
    (path / "manifest.json").write_text(payload, encoding="utf-8")
    if path not in TEMP_DIRS:
        TEMP_DIRS.append(path)
    return path


def drop_module(dir_name: str) -> Path:
    path = MODULES / dir_name
    shutil.rmtree(path, ignore_errors=True)
    if path in TEMP_DIRS:
        TEMP_DIRS.remove(path)
    return path


def cleanup_temp_modules() -> None:
    for path in list(TEMP_DIRS):
        shutil.rmtree(path, ignore_errors=True)
        if path in TEMP_DIRS:
            TEMP_DIRS.remove(path)
    # 兜底：上一次异常中断留下的 _probe_* 也清掉（编排者会检查这里没有残留）
    for stray in sorted(MODULES.glob("_probe_*")):
        shutil.rmtree(stray, ignore_errors=True)


def main() -> None:
    print("[0] 环境")
    print("        python   :", sys.version.split()[0])
    print("        plugin   :", PLUGIN_ROOT)
    print("        dashboard:", DASHBOARD)
    print("        real home:", REAL_HOME)
    print("        modules  :", MODULES)

    import yaml  # noqa: E402 - 只用于 tempfile 假 home 的一次性 fixture

    print("        registry :", registry.__file__)
    check("模块目录存在（dashboard/modules）", MODULES.is_dir(), str(MODULES))

    # -----------------------------------------------------------------------
    print("[1] discover_modules()：发现 9 个模块，manifest 取值与薄封装路由数")
    specs = registry.discover_modules()
    ids = [spec.id for spec in specs]
    print("        discover_modules() ids =", ids)
    for spec in specs:
        print(
            "        %-9s label=%-9s version=%-6s nav_order=%-4s default_enabled=%-5s api_module=%-12s section=%-9s requires=%s"
            % (
                spec.id,
                spec.label,
                spec.version,
                spec.nav_order,
                spec.default_enabled,
                spec.api_module,
                spec.section,
                list(spec.requires),
            )
        )
    check(
        "发现 9 个模块且顺序 = overview/weather/agents/autogroup/advisor/upgrade/settings/vcc/remote",
        ids == EXPECTED_MODULE_IDS,
        str(ids),
    )
    check(
        "id 集合 = {overview, weather, agents, autogroup, advisor, upgrade, settings, vcc, remote}",
        set(ids) == set(EXPECTED_MODULE_IDS),
        str(sorted(set(ids))),
    )
    check(
        "按 nav_order 排序为 [10,20,30,40,50,60,70,80,90]",
        [spec.nav_order for spec in specs] == EXPECTED_NAV_ORDERS,
        str([spec.nav_order for spec in specs]),
    )
    field_ok = True
    for spec in specs:
        field_ok = field_ok and (
            isinstance(spec.id, str)
            and spec.id
            and isinstance(spec.label, str)
            and spec.label
            and isinstance(spec.version, str)
            and isinstance(spec.nav_order, int)
            and isinstance(spec.default_enabled, bool)
            and (spec.api_module is None or isinstance(spec.api_module, str))
            and (spec.section is None or isinstance(spec.section, str))
            and isinstance(spec.settings_schema, dict)
            and isinstance(spec.requires, (list, tuple))
            and isinstance(spec.manifest_path, Path)
            and spec.manifest_path.is_file()
        )
    check("每个 ModuleSpec 字段齐全且类型正确（含 settings_schema / requires / manifest_path）", field_ok)
    by_id = {spec.id: spec for spec in specs}

    # M3：manifest 取值逐项对照卡片表格（新增 4 个模块按表填，不许自己发挥）
    table_problems: List[str] = []
    for module_id in EXPECTED_MODULE_IDS:
        expected = EXPECTED_MANIFEST[module_id]
        spec = by_id.get(module_id)
        if spec is None:
            table_problems.append("%s 未发现" % module_id)
            continue
        actual = {
            "label": spec.label,
            "nav_order": spec.nav_order,
            "default_enabled": spec.default_enabled,
            "api_module": spec.api_module,
            "section": spec.section,
        }
        for key, want in expected.items():
            got = actual.get(key)
            if got != want:
                table_problems.append("%s.%s：期望 %r 实际 %r" % (module_id, key, want, got))
    check(
        "9 份 manifest 的 label/nav_order/default_enabled/api_module/section 与卡片表格逐项一致",
        not table_problems,
        "; ".join(table_problems) if table_problems else "全部一致",
    )
    check(
        "version 一律 1.0.0、requires 一律 []",
        all(spec.version == "1.0.0" for spec in specs)
        and all(list(spec.requires) == [] for spec in specs),
        brief([(spec.id, spec.version, list(spec.requires)) for spec in specs], 320),
    )
    check(
        "默认开关策略：纯只读（overview/weather/agents/settings）默认开，带写（autogroup/advisor/upgrade/vcc/remote）默认关",
        [spec.id for spec in specs if spec.default_enabled] == DEFAULT_ENABLED_IDS
        and [spec.id for spec in specs if not spec.default_enabled] == DEFAULT_DISABLED_IDS,
        brief([(spec.id, spec.default_enabled) for spec in specs], 320),
    )
    health0 = registry.health()
    print("        health() =", brief(health0))
    check(
        "health() 初始 loaded = 9 个模块且 failed 为空",
        health0["loaded"] == EXPECTED_MODULE_IDS and health0["failed"] == {},
        brief(health0),
    )

    # 有后端薄封装的模块：两条导入路径都要能拿到 router，
    # 并且路由数必须与被封装的 api_*.py **完全一致**（证明是薄封装，不是重写）。
    # 纯前端模块（FRONTEND_ONLY_IDS）没有 api_module / api.py，这里单独断言它确实没有，
    # 而不是让它在循环里假失败。
    for module_id in EXPECTED_MODULE_IDS:
        impl_name = EXPECTED_MANIFEST[module_id]["api_module"]
        api_path = MODULES / module_id / "api.py"
        if module_id in FRONTEND_ONLY_IDS:
            check(
                "纯前端模块 %s 没有 api_module / api.py（不伪装成有后端）" % module_id,
                impl_name is None and not api_path.exists(),
                "api_module=%r api.py存在=%s" % (impl_name, api_path.exists()),
            )
            continue
        if module_id in AGGREGATED_IDS:
            # 聚合型模块：manifest 声明了 api_module，但目录里**故意没有** api.py
            # （router 由 plugin_api.DOMAIN_MODULES 直接 include）。断言两件事：
            #   1) api.py 确实不存在（别既登记又偷偷薄封装，两份真相）；
            #   2) 声明的 api_module 真的能 import 出非空 router。
            try:
                agg_mod = importlib.import_module(impl_name)
                agg_routes = len(agg_mod.router.routes)
                agg_error = ""
            except Exception as exc:  # noqa: BLE001
                agg_routes = -1
                agg_error = "%s: %s" % (type(exc).__name__, exc)
            check(
                "聚合型模块 %s：没有 api.py 但 %s 能 import 出 router（%d 条）"
                % (module_id, impl_name, agg_routes),
                impl_name is not None and not api_path.exists() and agg_routes > 0,
                "api.py存在=%s routes=%d %s" % (api_path.exists(), agg_routes, agg_error),
            )
            continue
        try:
            impl_mod = importlib.import_module(impl_name)
            impl_routes = len(impl_mod.router.routes)
        except Exception as exc:  # noqa: BLE001
            impl_mod = None
            impl_routes = -1
            check("import %s（模块 %s 的被封装实现）可用" % (impl_name, module_id), False,
                  "%s: %s" % (type(exc).__name__, exc))
        try:
            spec_obj = importlib.util.spec_from_file_location("hc_probe_%s_api" % module_id, api_path)
            loaded = importlib.util.module_from_spec(spec_obj)  # type: ignore[arg-type]
            spec_obj.loader.exec_module(loaded)  # type: ignore[union-attr]
            route_count = len(loaded.router.routes)
            check(
                "modules/%s/api.py 裸名导入可用（router 路由数 %d = %s 的 %d）"
                % (module_id, route_count, impl_name, impl_routes),
                route_count > 0 and route_count == impl_routes,
                "module=%d impl=%d" % (route_count, impl_routes),
            )
        except Exception as exc:  # noqa: BLE001
            check("modules/%s/api.py 裸名导入可用" % module_id, False, "%s: %s" % (type(exc).__name__, exc))
        try:
            pkg_mod = importlib.import_module("dashboard.modules.%s.api" % module_id)
            pkg_routes = len(pkg_mod.router.routes)
            check(
                "dashboard.modules.%s.api 包上下文导入可用（MODULE_ID=%s，路由数 %d = %s 的 %d）"
                % (module_id, pkg_mod.MODULE_ID, pkg_routes, impl_name, impl_routes),
                pkg_mod.MODULE_ID == module_id and pkg_routes > 0 and pkg_routes == impl_routes,
                "MODULE_ID=%s module=%d impl=%d" % (pkg_mod.MODULE_ID, pkg_routes, impl_routes),
            )
        except Exception as exc:  # noqa: BLE001
            check("dashboard.modules.%s.api 包上下文导入可用" % module_id, False, "%s: %s" % (type(exc).__name__, exc))

    # -----------------------------------------------------------------------
    print("[2] 坏模块隔离：_probe_broken（缺 id）只让它自己进 failed")
    make_module("_probe_broken", {"label": "坏模块（故意缺 id）"})
    health1 = registry.health()
    print("        health() =", brief(health1, 320))
    check("_probe_broken 进了 failed", "_probe_broken" in health1["failed"], brief(health1["failed"]))
    check(
        "_probe_broken 的失败原因点名缺 id 字段",
        "id" in health1["failed"].get("_probe_broken", ""),
        health1["failed"].get("_probe_broken", "<missing>"),
    )
    check(
        "另外 9 个模块仍在 loaded（坏模块没连坐）",
        health1["loaded"] == EXPECTED_MODULE_IDS,
        str(health1["loaded"]),
    )
    check(
        "discover_modules() 仍返回 9 个可用模块（坏的不进来，也不抛异常）",
        [spec.id for spec in registry.discover_modules()] == EXPECTED_MODULE_IDS,
    )
    # 再叠两个坏法：JSON 坏掉 / id 与已有模块重复
    make_module("_probe_badjson", "{ 这不是 JSON ")
    make_module("_probe_dup", {"id": "overview", "label": "重复 id"})
    health2 = registry.health()
    print("        health() =", brief(health2, 420))
    check(
        "JSON 坏掉的目录也进 failed 且原因提到 JSON",
        "JSON" in health2["failed"].get("_probe_badjson", ""),
        health2["failed"].get("_probe_badjson", "<missing>"),
    )
    check(
        "id 重复的目录进 failed 且原因提到重复",
        "重复" in health2["failed"].get("_probe_dup", ""),
        health2["failed"].get("_probe_dup", "<missing>"),
    )
    check(
        "三个坏模块都在 failed 里，9 个真模块依然 loaded",
        set(health2["failed"]) == {"_probe_broken", "_probe_badjson", "_probe_dup"}
        and health2["loaded"] == EXPECTED_MODULE_IDS,
        brief(health2),
    )
    drop_module("_probe_broken")
    drop_module("_probe_badjson")
    drop_module("_probe_dup")
    check("删掉三个坏目录后 health() 恢复干净", registry.health()["failed"] == {}, brief(registry.health()))

    # -----------------------------------------------------------------------
    print("[3] 删目录即下线：_probe_temp 出现 → 被发现；删目录 → 不再被发现")
    make_module("_probe_temp", {"id": "probetemp", "label": "临时模块", "nav_order": 90})
    ids_with_temp = [spec.id for spec in registry.discover_modules()]
    check("造出 _probe_temp 后被发现（id=probetemp）", "probetemp" in ids_with_temp, str(ids_with_temp))
    drop_module("_probe_temp")
    ids_after = [spec.id for spec in registry.discover_modules()]
    check("删掉目录后不再被发现（目录即模块）", "probetemp" not in ids_after, str(ids_after))
    check("删掉后模块集合回到 9 个（overview/weather/agents/autogroup/advisor/upgrade/settings/vcc/remote）",
          ids_after == EXPECTED_MODULE_IDS, str(ids_after))

    # -----------------------------------------------------------------------
    print("[4] 启用集计算：假 home 里的 probeagent 关掉 weather")
    fake_home = Path(tempfile.mkdtemp(prefix="hc-registry-probe-"))
    try:
        (fake_home / "profiles" / "probeagent").mkdir(parents=True)
        root_cfg = {
            "plugins": {
                "entries": {
                    "hermes-console": {
                        "settings": {"modules": {"overview": {"enabled": False}}}
                    }
                }
            }
        }
        agent_cfg = {
            "plugins": {
                "entries": {
                    "hermes-console": {
                        "settings": {
                            "modules": {
                                "overview": {"enabled": True},
                                "weather": {"enabled": False},
                            }
                        }
                    }
                }
            }
        }
        (fake_home / "config.yaml").write_text(
            yaml.safe_dump(root_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        (fake_home / "profiles" / "probeagent" / "config.yaml").write_text(
            yaml.safe_dump(agent_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        print("        fake home :", fake_home)

        def decisions(agent_id: str) -> Dict[str, Any]:
            return {d.spec.id: d for d in registry.resolve_modules(agent_id, home=fake_home)}

        # default：只看到 global 层 —— overview 被全局关掉、weather 走模块默认
        default_decisions = decisions("default")
        print("        default     :", brief({k: (v.enabled, v.source, v.reason) for k, v in default_decisions.items()}, 400))
        check(
            "default：overview 被全局设置关掉（source=global，reason 说清）",
            default_decisions["overview"].enabled is False
            and default_decisions["overview"].source == "global"
            and "全局设置关闭" in default_decisions["overview"].reason,
            default_decisions["overview"].reason,
        )
        check(
            "default：weather 走模块默认启用（source=module_default）",
            default_decisions["weather"].enabled is True
            and default_decisions["weather"].source == "module_default",
            default_decisions["weather"].reason,
        )

        # probeagent：agent 层覆盖 global 层
        agent_decisions = decisions("probeagent")
        print("        probeagent  :", brief({k: (v.enabled, v.source, v.reason) for k, v in agent_decisions.items()}, 400))
        check(
            "probeagent：overview 由 agent 设置重新打开（agent 覆盖 global）",
            agent_decisions["overview"].enabled is True
            and agent_decisions["overview"].source == "agent",
            agent_decisions["overview"].reason,
        )
        check(
            "probeagent：weather 被该 agent 关掉（source=agent）",
            agent_decisions["weather"].enabled is False
            and agent_decisions["weather"].source == "agent",
            agent_decisions["weather"].reason,
        )
        check(
            "probeagent：weather 的 reason 说清是 agent 关的",
            "关闭" in agent_decisions["weather"].reason and "agent" in agent_decisions["weather"].reason,
            agent_decisions["weather"].reason,
        )
        enabled_ids = [spec.id for spec in registry.enabled_modules("probeagent", home=fake_home)]
        print("        enabled_modules('probeagent') =", enabled_ids)
        check("enabled_modules 里没有 weather", "weather" not in enabled_ids, str(enabled_ids))
        check("enabled_modules 里仍有 overview", "overview" in enabled_ids, str(enabled_ids))
        status = {item["id"]: item for item in registry.module_status("probeagent", home=fake_home)}
        check(
            "module_status 每条都带 enabled + reason（未启用的也在列表里，不静默消失）",
            set(status) == set(EXPECTED_MODULE_IDS)
            and all("enabled" in item and item["reason"] for item in status.values()),
            brief(status, 400),
        )
    finally:
        shutil.rmtree(fake_home, ignore_errors=True)

    # -----------------------------------------------------------------------
    print("[4b] 默认启用集（无任何 settings 覆盖）：纯只读默认开、带写默认关")
    clean_home = Path(tempfile.mkdtemp(prefix="hc-registry-probe-default-"))
    try:
        clean_decisions = {d.spec.id: d for d in registry.resolve_modules("default", home=clean_home)}
        print(
            "        无覆盖 default :",
            brief({k: (v.enabled, v.source, v.reason) for k, v in clean_decisions.items()}, 560),
        )
        clean_enabled = [spec.id for spec in registry.enabled_modules("default", home=clean_home)]
        print("        enabled_modules('default')（无覆盖） =", clean_enabled)
        check(
            "无覆盖时默认启用集 = overview / weather / agents",
            clean_enabled == DEFAULT_ENABLED_IDS,
            str(clean_enabled),
        )
        off_problems: List[str] = []
        for module_id in DEFAULT_DISABLED_IDS:
            decision = clean_decisions[module_id]
            if not decision.enabled and decision.source == "module_default" and "模块默认关闭" in decision.reason:
                continue
            off_problems.append(
                "%s：enabled=%s source=%s reason=%s"
                % (module_id, decision.enabled, decision.source, decision.reason)
            )
        check(
            "autogroup / advisor / upgrade 默认关闭，reason 说清是模块默认关闭（source=module_default）",
            not off_problems,
            "; ".join(off_problems) if off_problems else "三个都按模块默认关闭",
        )
        check(
            "默认关闭的三个模块确实不在 enabled_modules 里",
            not (set(DEFAULT_DISABLED_IDS) & set(clean_enabled)),
            str(clean_enabled),
        )
    finally:
        shutil.rmtree(clean_home, ignore_errors=True)

    # 真实 home：没配过 settings ⇒ 三个只读模块按模块默认启用（只读，不写任何文件）
    real_enabled = [spec.id for spec in registry.enabled_modules("default")]
    print("        enabled_modules('default') [真实 home] =", real_enabled)
    check(
        "真实 home 下 default 启用 overview + weather + agents",
        real_enabled == DEFAULT_ENABLED_IDS,
        str(real_enabled),
    )

    # -----------------------------------------------------------------------
    print("[5] 依赖未满足：requires 指向不存在的模块 / 指向被关掉的模块")
    make_module(
        "_probe_dep",
        {"id": "probedep", "label": "依赖探测（缺依赖）", "nav_order": 95, "requires": ["nonexistent"]},
    )
    dep_decisions = {d.spec.id: d for d in registry.resolve_modules("default")}
    print("        probedep    :", brief((dep_decisions["probedep"].enabled, dep_decisions["probedep"].reason), 300))
    check("requires 指向不存在模块 ⇒ 不启用", dep_decisions["probedep"].enabled is False)
    check(
        "reason 点名缺哪个依赖（nonexistent）",
        "nonexistent" in dep_decisions["probedep"].reason,
        dep_decisions["probedep"].reason,
    )
    check(
        "依赖不满足的模块不会出现在 enabled_modules 里",
        "probedep" not in [spec.id for spec in registry.enabled_modules("default")],
    )
    drop_module("_probe_dep")

    # 依赖已安装但被关掉：requires:["weather"] + weather 在 probeagent 上关闭
    make_module(
        "_probe_child",
        {"id": "probechild", "label": "依赖探测（依赖被关）", "nav_order": 96, "requires": ["weather"]},
    )
    fake_home2 = Path(tempfile.mkdtemp(prefix="hc-registry-probe-dep-"))
    try:
        (fake_home2 / "profiles" / "probeagent").mkdir(parents=True)
        cfg = {
            "plugins": {
                "entries": {
                    "hermes-console": {"settings": {"modules": {"weather": {"enabled": False}}}}
                }
            }
        }
        (fake_home2 / "profiles" / "probeagent" / "config.yaml").write_text(
            yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        child_decisions = {d.spec.id: d for d in registry.resolve_modules("probeagent", home=fake_home2)}
        print(
            "        probechild  :",
            brief((child_decisions["probechild"].enabled, child_decisions["probechild"].reason), 300),
        )
        check("依赖模块被关掉 ⇒ 本模块不启用", child_decisions["probechild"].enabled is False)
        check(
            "reason 点名是哪个依赖没启用（weather）",
            "weather" in child_decisions["probechild"].reason,
            child_decisions["probechild"].reason,
        )
        check(
            "同一次计算里 weather 自身也是关闭的",
            child_decisions["weather"].enabled is False,
            child_decisions["weather"].reason,
        )
    finally:
        shutil.rmtree(fake_home2, ignore_errors=True)
    drop_module("_probe_child")
    check(
        "依赖探针清理后 health() 干净、模块集合回到 9 个",
        registry.health()["failed"] == {}
        and [spec.id for spec in registry.discover_modules()] == EXPECTED_MODULE_IDS,
        brief(registry.health()),
    )

    # -----------------------------------------------------------------------
    print("[6][7][8] 真实 HTTP：GET /modules + 旧聚合回归")
    api_spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_hermes-console", DASHBOARD / "plugin_api.py")
    api_mod = importlib.util.module_from_spec(api_spec)  # type: ignore[arg-type]
    sys.modules[api_spec.name] = api_mod  # type: ignore[union-attr]
    api_spec.loader.exec_module(api_mod)  # type: ignore[union-attr]

    from fastapi import FastAPI  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402

    app = FastAPI()
    app.include_router(api_mod.router, prefix=PREFIX)
    client = TestClient(app, raise_server_exceptions=False)

    print("        plugin_api: loaded=%s failed=%s registry_error=%r"
          % (api_mod.DOMAIN_LOADED, api_mod.DOMAIN_FAILURES, api_mod.REGISTRY_ERROR))

    resp = client.get(PREFIX + "/modules?agent=default")
    body = resp.json()
    print("        GET /modules?agent=default      %s  %s" % (resp.status_code, brief(body, 300)))
    check("GET /modules?agent=default → 200", resp.status_code == 200, str(resp.status_code))
    check("响应 ok=true 且 data.agent='default'",
          body.get("ok") is True and body.get("data", {}).get("agent") == "default", brief(body, 200))
    modules = body.get("data", {}).get("modules") or []
    check("modules 非空", len(modules) > 0, str(len(modules)))
    check(
        "modules 含 9 个模块，启用的正好是 overview / weather / agents / settings",
        {item["id"] for item in modules} == set(EXPECTED_MODULE_IDS)
        and [item["id"] for item in modules if item.get("enabled")] == DEFAULT_ENABLED_IDS,
        brief([(item["id"], item.get("enabled")) for item in modules], 400),
    )
    check(
        "默认关闭的三个模块在 /modules 里也带 enabled=false + reason（不静默消失）",
        all(
            item.get("enabled") is False and item.get("reason")
            for item in modules
            if item["id"] in DEFAULT_DISABLED_IDS
        ),
        brief([(item["id"], item.get("enabled"), item.get("reason")) for item in modules], 460),
    )
    check("每条 module 都含 id/label/nav_order/section/enabled/reason",
          all({"id", "label", "nav_order", "section", "enabled", "reason"} <= set(item) for item in modules),
          brief(modules[:1], 300))
    check("data.health.failed 为空（模块健康）",
          body.get("data", {}).get("health", {}).get("failed") == {},
          brief(body.get("data", {}).get("health"), 200))

    resp_x = client.get(PREFIX + "/modules?agent=xiaomo")
    body_x = resp_x.json()
    print("        GET /modules?agent=xiaomo        %s  %s" % (resp_x.status_code, brief(body_x, 240)))
    check("GET /modules?agent=xiaomo → 200 且 agent 字段正确",
          resp_x.status_code == 200 and body_x.get("data", {}).get("agent") == "xiaomo",
          "%s %s" % (resp_x.status_code, brief(body_x, 160)))

    resp_no = client.get(PREFIX + "/modules")
    check("GET /modules（不带 agent）→ 200 且回落 default",
          resp_no.status_code == 200 and resp_no.json().get("data", {}).get("agent") == "default",
          str(resp_no.status_code))

    resp_bad = client.get(PREFIX + "/modules?agent=../../etc")
    bad_body = resp_bad.json()
    print("        GET /modules?agent=../../etc     %s  %s" % (resp_bad.status_code, brief(bad_body, 300)))
    check("非法 agent 返回 400（不是 500）", resp_bad.status_code == 400, str(resp_bad.status_code))
    check("400 错误体 code=invalid_argument", bad_body.get("error", {}).get("code") == "invalid_argument",
          brief(bad_body, 200))
    agents = bad_body.get("error", {}).get("agents") or []
    check("400 错误体带合法 id 列表（含 default / xiaomo）",
          isinstance(agents, list) and "default" in agents and "xiaomo" in agents, brief(agents, 240))
    resp_bad2 = client.get(PREFIX + "/modules?agent=a%20b")
    check("另一个非法 agent（含空格）也是 400", resp_bad2.status_code == 400, str(resp_bad2.status_code))

    resp_health = client.get(PREFIX + "/health")
    health_body = resp_health.json()
    print("        GET /health                      %s  %s" % (resp_health.status_code, brief(health_body, 300)))
    check("GET /health → 200 且 ok=true", resp_health.status_code == 200 and health_body.get("ok") is True,
          brief(health_body, 200))
    check("/health 的 failed 为空", health_body.get("failed") == {}, brief(health_body.get("failed")))
    check("/health 的 failed 不含 api_overview / api_weather",
          "api_overview" not in health_body.get("failed", {})
          and "api_weather" not in health_body.get("failed", {}),
          brief(health_body.get("failed")))

    actual_routes = set()
    for route in api_mod.router.routes:
        for method in sorted(getattr(route, "methods", []) or []):
            actual_routes.add("%s %s" % (method, route.path))
    missing = sorted(set(BASELINE_ROUTES) - actual_routes)
    missing_new = sorted(set(M4B0_ROUTES) - actual_routes)
    missing_m4b2 = sorted(set(M4B2_ROUTES) - actual_routes)
    missing_v3 = sorted(set(V3_ROUTES) - actual_routes)
    missing_v4 = sorted(set(V4_ROUTES) - actual_routes)
    missing_v5 = sorted(set(V5_ROUTES) - actual_routes)
    expected_total = (len(BASELINE_ROUTES) + 1 + len(M4B0_ROUTES) + len(M4B2_ROUTES)
                      + len(V3_ROUTES) + len(V4_ROUTES) + len(V5_ROUTES))
    print("        路由总数 = %d（本卡之前 %d 条 + GET /modules + M4b0 的 %d 条 + M4b2 的 %d 条"
          " + V3 的 %d 条 + V4 的 %d 条 + V5 的 %d 条）"
          % (health_body.get("route_count", -1), len(BASELINE_ROUTES), len(M4B0_ROUTES),
             len(M4B2_ROUTES), len(V3_ROUTES), len(V4_ROUTES), len(V5_ROUTES)))
    check("既有 %d 条路由一条不少" % len(BASELINE_ROUTES), not missing, "缺: " + str(missing))
    check("M4b0 的 %d 条路由都在" % len(M4B0_ROUTES), not missing_new, "缺: " + str(missing_new))
    check("M4b2 的 %d 条路由（插件启停）都在" % len(M4B2_ROUTES), not missing_m4b2,
          "缺: " + str(missing_m4b2))
    check("V3 的 %d 条路由（上下文引擎）都在" % len(V3_ROUTES), not missing_v3,
          "缺: " + str(missing_v3))
    check("V4 的 %d 条路由（远端后端）都在" % len(V4_ROUTES), not missing_v4,
          "缺: " + str(missing_v4))
    check("V5 的 %d 条路由（模型分区）都在" % len(V5_ROUTES), not missing_v5,
          "缺: " + str(missing_v5))
    check("路由总数 = %d（既有一条不少 + GET /modules + M4b0 四条 + M4b2 一条"
          " + V3 六条 + V4 五条 + V5 十七条）" % expected_total,
          health_body.get("route_count") == expected_total,
          str(health_body.get("route_count")))

    # 旧分区抽查：overview / weather / 升级仍然 200（新端点没抢路由、没搞坏旧聚合）
    for path in ("/overview", "/weather", "/upgrade/scan"):
        resp_old = client.get(PREFIX + path)
        check("既有分区 GET %s 仍为 200" % path, resp_old.status_code == 200, str(resp_old.status_code))

    # 模块列表里的 api_module 名必须能在旧聚合里找到（薄封装指向同一份实现）
    for item in modules:
        api_module = item.get("api_module")
        if api_module:
            check(
                "模块 %s 的 api_module=%s 已在旧聚合里加载" % (item["id"], api_module),
                api_module in api_mod.DOMAIN_LOADED,
                str(api_mod.DOMAIN_LOADED),
            )


try:
    main()
except Exception:  # noqa: BLE001 - 任何未预期异常也算探针失败，但先清理现场
    traceback.print_exc()
    failures.append("未预期异常：" + traceback.format_exc(limit=1).strip().replace("\n", " "))
finally:
    cleanup_temp_modules()

# ---------------------------------------------------------------------------
print("[9] 收尾：dashboard/modules 下恰好 9 个模块目录 + README.md，无 _probe_* 残留")
residue = sorted(path.name for path in MODULES.glob("_probe_*"))
top_level = sorted(path.name for path in MODULES.iterdir()) if MODULES.is_dir() else []
print("        modules 顶层 =", top_level)
check("无 _probe_* 残留", residue == [], str(residue))
check(
    "modules 顶层 = README.md + 恰好 9 个模块目录",
    top_level == ["README.md"] + sorted(EXPECTED_MODULE_IDS),
    str(top_level),
)
for module_id in EXPECTED_MODULE_IDS:
    entries = sorted(path.name for path in (MODULES / module_id).iterdir())
    # 只有 manifest.json 的两种形态：纯前端（settings）与聚合型（vcc / remote，
    # router 由 plugin_api.DOMAIN_MODULES 聚合，目录里故意不放 api.py）。
    manifest_only = module_id in FRONTEND_ONLY_IDS or module_id in AGGREGATED_IDS
    expected_entries = ["manifest.json"] if manifest_only else ["api.py", "manifest.json"]
    check(
        "modules/%s/ 只含 %s（无残留）" % (module_id, " + ".join(expected_entries)),
        entries == expected_entries,
        str(entries),
    )
check("收尾后 health().failed 为空且 loaded 为 9 个", registry.health()["failed"] == {}
      and registry.health()["loaded"] == EXPECTED_MODULE_IDS, brief(registry.health()))

print("")
print("PASS %d / FAIL %d" % (passes, len(failures)))
if failures:
    for item in failures:
        print("  -", item)
    print("REGISTRY-PROBE-FAILED")
    raise SystemExit(1)
print("REGISTRY-PROBE-OK")

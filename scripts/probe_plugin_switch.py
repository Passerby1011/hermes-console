# -*- coding: utf-8 -*-
"""hermes-console · M2 插件级开关 验收探针（唯一验收依据）。

跑法（**不要经管道拿退出码**）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        plugins/hermes-console/scripts/probe_plugin_switch.py
    echo "exit=$?"        # bash；PowerShell 用 $LASTEXITCODE

覆盖（对应卡片的 8 条 + 真实 home 抽查 + 2 条附加）::

    [1] 隔离              tempfile 假 HERMES_HOME + 假 profile alpha / beta（各写不同的
                          plugins.enabled + plugins.disabled），并断言解析出的 home 在沙盒内
    [2] 黑名单优先        同时在 enabled 与 disabled 里 ⇒ enabled=False, source=disabled_list
    [3] 关闭生效          alpha 的 disabled 含 X；beta 的 disabled 不含 X；根配置不动
    [4] ★enabled 整表不变 关闭前后 alpha 的 plugins.enabled 值级 == 且 json.dumps 完全一致
    [5] 开启只动黑名单     从 disabled 移除；不在 enabled 里 ⇒ 返回体带 warning 且 enabled 仍不变
    [6] 幂等              连续两次关闭同一个 plugin，disabled 里只有一条
    [7] 非法输入          set_plugin_enabled('../../etc', ...) / plugin_id='' 抛 ValueError
                          且沙盒文件快照完全没变（一个字节都没写）
    [8] ★生产零污染       真实 config.yaml + profiles/*/config.yaml 开头/结尾 sha256 全一致
    [9] 真实 home 抽查    对 xiaomo 做只读 plugin_state，与 profiles/xiaomo/config.yaml 实际内容逐项一致
   [10] 附加              list_plugin_states 的并集/顺序；gamma（无 config.yaml）写入后只创建
                          plugins.disabled，不带任何默认值

**探针里唯一手写的 YAML 是 tempfile 里的假 fixture**；对"真配置"的每一次写入都走
``dashboard.core.plugin_switch.set_plugin_enabled`` → 官方 ``hermes_cli.config.save_config``。
真实 home 全程只读（第 8 条会抓）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]      # <home>/plugins/hermes-console
REAL_HOME = Path(__file__).resolve().parents[3]        # <home>
HERMES_AGENT = REAL_HOME / "hermes-agent"

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


def tree_snapshot(root: Path) -> dict[str, str]:
    """整棵目录的文件内容快照（非法输入断言「一个字节都没写」用）。"""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = sha256_of(path)
    return out


def load_yaml(path: Path):
    import yaml

    with open(path, encoding="utf-8-sig") as handle:
        return yaml.safe_load(handle) or {}


def plugins_of(path: Path) -> dict:
    node = load_yaml(path).get("plugins")
    return node if isinstance(node, dict) else {}


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
print("[0] 环境")
print("        python :", sys.version.split()[0])
print("        plugin :", PLUGIN_ROOT)
print("        real   :", REAL_HOME)

if not (REAL_HOME / "config.yaml").is_file():
    check("真实 home 可定位（<home>/config.yaml 存在）", False, str(REAL_HOME))
    print("PLUGIN-SWITCH-PROBE-FAILED")
    raise SystemExit(1)

# ---------------------------------------------------------------------------
print("[1] 生产零污染 · 前置快照（在任何 import / 任何写入之前）")
prod_before = production_snapshot(REAL_HOME)
print_snapshot("真实配置 before", prod_before)
check(
    "前置快照非空且无读取错误",
    bool(prod_before) and all(not v.startswith("<error:") for v in prod_before.values()),
)

# ---------------------------------------------------------------------------
print("[1b] 造隔离沙盒：tempfile 假 HERMES_HOME + 假 profile alpha / beta / gamma")
fake_home = Path(tempfile.mkdtemp(prefix="hc-plugin-switch-probe-"))
(fake_home / "profiles" / "alpha").mkdir(parents=True)
(fake_home / "profiles" / "beta").mkdir(parents=True)
(fake_home / "profiles" / "gamma").mkdir(parents=True)   # 只有 profile.yaml，没有 config.yaml

# 一次性 fixture 铺底（只在 tempfile 里；真实配置一律走官方写入器）
import yaml  # noqa: E402

PLUGIN_BOTH = "shared-both"          # 同时在 alpha 的 enabled 与 disabled 里 → 黑名单优先
PLUGIN_ALPHA_ON = "alpha-plugin-a"   # 只在 alpha 的 enabled 里
PLUGIN_TARGET = "alpha-plugin-c"     # alpha 两边都没有 → 测试关闭/开启
PLUGIN_DUP = "alpha-plugin-d"        # 幂等测试
PLUGIN_ABSENT = "no-such-plugin"     # 谁都没有

root_cfg = {
    "plugins": {
        "enabled": ["root-plugin-a", "root-plugin-b"],
        "disabled": ["root-off-1"],
    }
}
alpha_cfg = {
    "agent": {"max_turns": 33},
    "terminal": {"timeout": 123},
    "plugins": {
        "enabled": ["alpha-plugin-a", "alpha-plugin-b", PLUGIN_BOTH],
        "disabled": ["alpha-off-1", PLUGIN_BOTH],
    },
}
beta_cfg = {
    "plugins": {
        "enabled": ["beta-plugin-a"],
        "disabled": ["beta-off-1"],
    }
}
gamma_profile = {"ui_meta": {"hermes-bots": {"title": "Gamma Bot"}}}

(fake_home / "config.yaml").write_text(yaml.safe_dump(root_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
(fake_home / "profiles" / "alpha" / "config.yaml").write_text(
    yaml.safe_dump(alpha_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
)
(fake_home / "profiles" / "beta" / "config.yaml").write_text(
    yaml.safe_dump(beta_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
)
(fake_home / "profiles" / "gamma" / "profile.yaml").write_text(
    yaml.safe_dump(gamma_profile, allow_unicode=True, sort_keys=False), encoding="utf-8"
)

print("        fake home :", fake_home)
print("        fixtures  : default(enabled=2/disabled=1) alpha(enabled=3/disabled=2) beta(enabled=1/disabled=1)")
print("                    alpha 的 %r 同时出现在 enabled 与 disabled（黑名单优先用例）" % PLUGIN_BOTH)

# 官方机制锚定在**进程** home 上，所以必须在 import 之前把 HERMES_HOME 指到假 home。
os.environ["HERMES_HOME"] = str(fake_home)
if HERMES_AGENT.is_dir() and str(HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT))
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from dashboard.core import scope as core_scope  # noqa: E402
from dashboard.core.plugin_switch import (  # noqa: E402
    PLUGIN_ALLOW_KEY,
    PLUGIN_DENY_KEY,
    PluginSwitchWriteError,
    WARN_NOT_IN_ALLOW_LIST,
    list_plugin_states,
    normalize_plugin_id,
    plugin_state,
    set_plugin_enabled,
)

# 沙盒护栏：解析出来的 home 必须在 tempfile 里，否则立刻停手（一个字节都不写）
resolved_home = core_scope.hermes_home()
inside_temp = str(resolved_home).lower().startswith(str(fake_home).lower())
check("home 解析锚定在假沙盒内（否则立即中止，绝不写生产）", inside_temp, "hermes_home()=%s" % resolved_home)
if not inside_temp:
    print("PLUGIN-SWITCH-PROBE-FAILED")
    raise SystemExit(1)

ALPHA_CFG = fake_home / "profiles" / "alpha" / "config.yaml"
BETA_CFG = fake_home / "profiles" / "beta" / "config.yaml"
ROOT_CFG = fake_home / "config.yaml"
GAMMA_CFG = fake_home / "profiles" / "gamma" / "config.yaml"

# ---------------------------------------------------------------------------
print("[1c] 隔离：三个 agent 各自读到自己的两份列表")
print("        agent_config_path('alpha') =", core_scope.agent_config_path("alpha"))
print("        agent_config_path('beta')  =", core_scope.agent_config_path("beta"))
print("        agent_config_path('default') =", core_scope.agent_config_path("default"))
check(
    "三个 agent 的 config 路径都落在各自位置",
    core_scope.agent_config_path("alpha") == ALPHA_CFG
    and core_scope.agent_config_path("beta") == BETA_CFG
    and core_scope.agent_config_path("default") == ROOT_CFG,
)
check(
    "alpha / beta / default 的 enabled+disabled 互不串台",
    plugins_of(ALPHA_CFG).get("enabled") == ["alpha-plugin-a", "alpha-plugin-b", PLUGIN_BOTH]
    and plugins_of(ALPHA_CFG).get("disabled") == ["alpha-off-1", PLUGIN_BOTH]
    and plugins_of(BETA_CFG).get("enabled") == ["beta-plugin-a"]
    and plugins_of(BETA_CFG).get("disabled") == ["beta-off-1"]
    and plugins_of(ROOT_CFG).get("disabled") == ["root-off-1"],
)

# ---------------------------------------------------------------------------
print("[2] 黑名单优先：同时在 enabled 与 disabled 里 ⇒ enabled=False, source=disabled_list")
state_both = plugin_state("alpha", PLUGIN_BOTH)
print("        plugin_state('alpha', %r) = %s" % (PLUGIN_BOTH, state_both))
check(
    "两边都在 ⇒ enabled=False / source=disabled_list / in_*=True",
    state_both
    == {
        "plugin": PLUGIN_BOTH,
        "enabled": False,
        "in_disabled": True,
        "in_enabled": True,
        "source": "disabled_list",
    },
    str(state_both),
)

state_on = plugin_state("alpha", PLUGIN_ALPHA_ON)
print("        plugin_state('alpha', %r) = %s" % (PLUGIN_ALPHA_ON, state_on))
check(
    "只在 enabled 里 ⇒ enabled=True / source=enabled_list",
    state_on["enabled"] is True and state_on["source"] == "enabled_list" and state_on["in_disabled"] is False,
    str(state_on),
)

state_absent = plugin_state("alpha", PLUGIN_ABSENT)
print("        plugin_state('alpha', %r) = %s" % (PLUGIN_ABSENT, state_absent))
check(
    "两边都没有 ⇒ enabled=False / source=absent",
    state_absent["enabled"] is False
    and state_absent["source"] == "absent"
    and state_absent["in_enabled"] is False
    and state_absent["in_disabled"] is False,
    str(state_absent),
)

check(
    "beta 的同名 plugin 状态独立（不受 alpha 列表影响）",
    plugin_state("beta", PLUGIN_BOTH)["source"] == "absent"
    and plugin_state("beta", "beta-off-1") == {
        "plugin": "beta-off-1",
        "enabled": False,
        "in_disabled": True,
        "in_enabled": False,
        "source": "disabled_list",
    },
)

# ---------------------------------------------------------------------------
print("[3] 关闭生效：set_plugin_enabled('alpha', %r, False)" % PLUGIN_TARGET)
alpha_enabled_before = plugins_of(ALPHA_CFG).get("enabled")
alpha_enabled_json_before = dumps(alpha_enabled_before)
print("        写前 alpha plugins.enabled =", dumps(alpha_enabled_before))

result_off = set_plugin_enabled("alpha", PLUGIN_TARGET, False)
print("        返回体 :", dumps(result_off))

alpha_disabled_after_off = plugins_of(ALPHA_CFG).get("disabled")
beta_disabled_after_off = plugins_of(BETA_CFG).get("disabled")
root_disabled_after_off = plugins_of(ROOT_CFG).get("disabled")
print("        写后 alpha plugins.disabled =", dumps(alpha_disabled_after_off))
print("        写后 beta  plugins.disabled =", dumps(beta_disabled_after_off))
print("        写后 root  plugins.disabled =", dumps(root_disabled_after_off))

check("返回结构化结果（ok/changed/enabled/source 齐全）",
      result_off.get("ok") is True
      and result_off.get("changed") is True
      and result_off.get("requested_enabled") is False
      and result_off.get("enabled") is False
      and result_off.get("source") == "disabled_list",
      dumps(result_off))
check("alpha 的 disabled 含 %r（直接读文件核对）" % PLUGIN_TARGET, PLUGIN_TARGET in (alpha_disabled_after_off or []))
check("beta 的 disabled 不含 %r（跨 agent 隔离）" % PLUGIN_TARGET, PLUGIN_TARGET not in (beta_disabled_after_off or []))
check("根配置的 disabled 没被碰（default agent 隔离）", root_disabled_after_off == ["root-off-1"], dumps(root_disabled_after_off))
check("plugin_state 回读 = 关闭后状态（enabled=False / disabled_list）",
      plugin_state("alpha", PLUGIN_TARGET)["enabled"] is False
      and plugin_state("alpha", PLUGIN_TARGET)["source"] == "disabled_list")
check("alpha 原有的 disabled 项没丢", (alpha_disabled_after_off or [])[:2] == ["alpha-off-1", PLUGIN_BOTH],
      dumps(alpha_disabled_after_off))

# ---------------------------------------------------------------------------
print("[4] ★最关键：关闭操作前后 plugins.enabled 整表不变（值级 == + json.dumps 双重比对）")
alpha_enabled_after = plugins_of(ALPHA_CFG).get("enabled")
alpha_enabled_json_after = dumps(alpha_enabled_after)
print("        写前 (repr) :", repr(alpha_enabled_before))
print("        写后 (repr) :", repr(alpha_enabled_after))
print("        写前 (json) :", alpha_enabled_json_before)
print("        写后 (json) :", alpha_enabled_json_after)
check("值级深比较：写前 == 写后", alpha_enabled_after == alpha_enabled_before)
check("json.dumps 子树比对：完全一致", alpha_enabled_json_after == alpha_enabled_json_before)
check("enabled 长度未变（没有偷偷塞/删任何一项）",
      len(alpha_enabled_after or []) == len(alpha_enabled_before or []) == 3,
      "写后 %d 项" % len(alpha_enabled_after or []))
check("enabled 里没有出现本次操作的 plugin（%r）" % PLUGIN_TARGET,
      PLUGIN_TARGET not in (alpha_enabled_after or []))
check("返回体也报告 enabled 未变（in_enabled=False）", result_off.get("in_enabled") is False, dumps(result_off))

# ---- 附加守卫：partial 写（payload 只有 plugins.disabled）会不会丢掉别的 section？----
alpha_other_sections_after = {
    key: value
    for key, value in (load_yaml(ALPHA_CFG) or {}).items()
    if key not in ("plugins", "_config_version")
}
print("        写后 alpha 其它 section =", dumps(alpha_other_sections_after))
check("partial 写没有丢掉 alpha 的其它 section（agent/terminal 原样保留）",
      alpha_other_sections_after == {"agent": {"max_turns": 33}, "terminal": {"timeout": 123}},
      dumps(alpha_other_sections_after))

# ---------------------------------------------------------------------------
print("[5] 开启只动黑名单：set_plugin_enabled('alpha', %r, True)" % PLUGIN_TARGET)
result_on = set_plugin_enabled("alpha", PLUGIN_TARGET, True)
print("        返回体 :", dumps(result_on))
alpha_disabled_after_on = plugins_of(ALPHA_CFG).get("disabled")
alpha_enabled_after_on = plugins_of(ALPHA_CFG).get("enabled")
print("        写后 alpha plugins.disabled =", dumps(alpha_disabled_after_on))
print("        写后 alpha plugins.enabled  =", dumps(alpha_enabled_after_on))

check("%r 已从 disabled 移除" % PLUGIN_TARGET, PLUGIN_TARGET not in (alpha_disabled_after_on or []),
      dumps(alpha_disabled_after_on))
check("返回体带 warning（诚实告知仍未启用）", result_on.get("warning") == WARN_NOT_IN_ALLOW_LIST,
      dumps(result_on))
check("返回体 enabled=False / source=absent（没在 enabled 里，实际未启用）",
      result_on.get("enabled") is False and result_on.get("source") == "absent")
check("★这次 enabled 仍然没变（没有偷偷塞进允许清单）",
      alpha_enabled_after_on == alpha_enabled_before and dumps(alpha_enabled_after_on) == alpha_enabled_json_before)
check("disabled 里剩下的原有项完好", alpha_disabled_after_on == ["alpha-off-1", PLUGIN_BOTH],
      dumps(alpha_disabled_after_on))

# ---------------------------------------------------------------------------
print("[6] 幂等：连续两次关闭 %r" % PLUGIN_DUP)
first_dup = set_plugin_enabled("alpha", PLUGIN_DUP, False)
second_dup = set_plugin_enabled("alpha", PLUGIN_DUP, False)
dup_list = plugins_of(ALPHA_CFG).get("disabled") or []
print("        第一次 :", dumps(first_dup))
print("        第二次 :", dumps(second_dup))
print("        写后 disabled =", dumps(dup_list))
check("第一次 changed=True", first_dup.get("changed") is True)
check("第二次 changed=False（已在则不再写）", second_dup.get("changed") is False)
check("disabled 里 %r 只出现一次" % PLUGIN_DUP, dup_list.count(PLUGIN_DUP) == 1, "出现 %d 次" % dup_list.count(PLUGIN_DUP))
check("幂等两次之后 enabled 依旧没变", plugins_of(ALPHA_CFG).get("enabled") == alpha_enabled_before)

# ---------------------------------------------------------------------------
print("[7] 非法输入：一律 ValueError，且在摸文件之前拦住（沙盒快照零变化）")
sandbox_before = tree_snapshot(fake_home)

bad_cases = [
    ("agent '../../etc'", lambda: set_plugin_enabled("../../etc", PLUGIN_TARGET, False)),
    ("plugin_id ''", lambda: set_plugin_enabled("alpha", "", False)),
    ("plugin_id '../escape'", lambda: set_plugin_enabled("alpha", "../escape", False)),
    ("plugin_id 'a b'", lambda: set_plugin_enabled("alpha", "a b", True)),
    ("plugin_id 'web//x'", lambda: set_plugin_enabled("alpha", "web//x", True)),
    ("enabled='false'（字符串）", lambda: set_plugin_enabled("alpha", PLUGIN_TARGET, "false")),
    ("agent 不存在 'nobody'", lambda: set_plugin_enabled("nobody", PLUGIN_TARGET, False)),
]
for label, call in bad_cases:
    try:
        call()
        got, detail = "没有抛异常", ""
    except ValueError as exc:
        got, detail = "ValueError", str(exc)[:90]
    except Exception as exc:  # noqa: BLE001
        got, detail = type(exc).__name__, str(exc)[:90]
    check("%s ⇒ ValueError" % label, got == "ValueError", "%s %s" % (got, detail))

for label, raw in (("'../../etc'", "../../etc"), ("''", ""), ("'x\\0y'", "x\x00y"), ("'a/../../b'", "a/../../b")):
    try:
        normalize_plugin_id(raw)
        got, detail = "没有抛异常", ""
    except ValueError as exc:
        got, detail = "ValueError", str(exc)[:90]
    except Exception as exc:  # noqa: BLE001
        got, detail = type(exc).__name__, str(exc)[:90]
    check("normalize_plugin_id(%s) ⇒ ValueError" % label, got == "ValueError", "%s %s" % (got, detail))

sandbox_after = tree_snapshot(fake_home)
changed_files = {key: (sandbox_before.get(key), sandbox_after.get(key))
                 for key in set(sandbox_before) | set(sandbox_after)
                 if sandbox_before.get(key) != sandbox_after.get(key)}
check("非法输入全程零文件写入（沙盒快照前后一致）", not changed_files,
      dumps(changed_files) if changed_files else "%d 个文件一致" % len(sandbox_after))
check("非法输入后 alpha 的 disabled 仍是最后状态",
      plugins_of(ALPHA_CFG).get("disabled") == dup_list, dumps(plugins_of(ALPHA_CFG).get("disabled")))

# ---------------------------------------------------------------------------
print("[10] 附加 A：list_plugin_states 的并集与顺序")
alpha_states = list_plugin_states("alpha")
alpha_names = [item["plugin"] for item in alpha_states]
# enabled 原序（alpha-plugin-a / alpha-plugin-b / shared-both）再补 disabled 独有项
# （alpha-off-1 / alpha-plugin-d），shared-both 重复出现被去重
expected_names = ["alpha-plugin-a", "alpha-plugin-b", PLUGIN_BOTH, "alpha-off-1", PLUGIN_DUP]
print("        list_plugin_states('alpha') =", alpha_names)
check("并集 = enabled 原序 + disabled 独有项原序（去重）", alpha_names == expected_names, str(alpha_names))
check("并集里没有重复项", len(alpha_names) == len(set(alpha_names)))
check("每项都是 plugin_state 的完整结构",
      all(set(item) == {"plugin", "enabled", "in_disabled", "in_enabled", "source"} for item in alpha_states))
check("priority 项 state 正确（shared-both → disabled_list）",
      [i for i in alpha_states if i["plugin"] == PLUGIN_BOTH][0]["source"] == "disabled_list")

print("[10] 附加 B：gamma（没有自己的 config.yaml）写入后只创建 plugins.disabled")
gamma_result = set_plugin_enabled("gamma", "gamma-plugin-x", False)
gamma_cfg = load_yaml(GAMMA_CFG) if GAMMA_CFG.is_file() else {}
print("        gamma 返回体 :", dumps(gamma_result))
print("        gamma 文件内容:", dumps(gamma_cfg))
check("gamma 写入成功且 changed=True", gamma_result.get("ok") is True and gamma_result.get("changed") is True)
check("新建的 config.yaml 只有 plugins.disabled（没被灌默认值）",
      list(gamma_cfg.keys()) == ["plugins"] or (set(gamma_cfg.keys()) <= {"plugins", "_config_version"}),
      dumps(list(gamma_cfg.keys())))
check("gamma 的 plugins 只有 disabled 一个键",
      list(gamma_cfg.get("plugins", {}).keys()) == ["disabled"], dumps(gamma_cfg.get("plugins")))
check("gamma 的 disabled = ['gamma-plugin-x']",
      gamma_cfg.get("plugins", {}).get("disabled") == ["gamma-plugin-x"], dumps(gamma_cfg.get("plugins")))

print("[10] 附加 C：default agent（作用域 None）写入只改根 config.yaml")
root_enabled_before = plugins_of(ROOT_CFG).get("enabled")
root_disabled_before = plugins_of(ROOT_CFG).get("disabled")
alpha_snapshot_before_default_write = tree_snapshot(ALPHA_CFG.parent)
# 硬护栏：default 分支写的是「进程 home 的 config.yaml」，进程 home 必须还是假沙盒
guard_ok = core_scope.hermes_home().resolve() == fake_home.resolve()
check("default 写入前 hermes_home() 仍是假沙盒（否则不写）", guard_ok, str(core_scope.hermes_home()))
if guard_ok:
    default_off = set_plugin_enabled("default", "root-off-2", False)
    print("        返回体 :", dumps(default_off))
    print("        写后 root enabled  =", dumps(plugins_of(ROOT_CFG).get("enabled")))
    print("        写后 root disabled =", dumps(plugins_of(ROOT_CFG).get("disabled")))
    check("default 关闭生效：根 disabled 追加了 root-off-2",
          plugins_of(ROOT_CFG).get("disabled") == (root_disabled_before or []) + ["root-off-2"],
          dumps(plugins_of(ROOT_CFG).get("disabled")))
    check("default 关闭时根 enabled 不变", plugins_of(ROOT_CFG).get("enabled") == root_enabled_before,
          dumps(plugins_of(ROOT_CFG).get("enabled")))
    check("default 写入没有波及 alpha 的配置文件",
          tree_snapshot(ALPHA_CFG.parent) == alpha_snapshot_before_default_write)
    default_on = set_plugin_enabled("default", "root-off-2", True)
    print("        开启后返回体 :", dumps(default_on))
    check("default 开启：根 disabled 回到写前（只有 root-off-1）",
          plugins_of(ROOT_CFG).get("disabled") == root_disabled_before, dumps(plugins_of(ROOT_CFG).get("disabled")))
    check("default 开启返回 warning（root-off-2 不在根 enabled 里）",
          default_on.get("warning") == WARN_NOT_IN_ALLOW_LIST and default_on.get("source") == "absent")
    check("default 开启后根 enabled 依然不变", plugins_of(ROOT_CFG).get("enabled") == root_enabled_before,
          dumps(plugins_of(ROOT_CFG).get("enabled")))

# ---------------------------------------------------------------------------
print("[9] 真实 home 抽查：对 xiaomo 做只读 plugin_state，与 profiles/xiaomo/config.yaml 逐项核对")
XIAOMO_CFG = REAL_HOME / "profiles" / "xiaomo" / "config.yaml"
if not XIAOMO_CFG.is_file():
    check("profiles/xiaomo/config.yaml 存在", False, str(XIAOMO_CFG))
else:
    xiaomo = load_yaml(XIAOMO_CFG).get("plugins") or {}
    x_enabled = [v for v in (xiaomo.get("enabled") or []) if isinstance(v, str)]
    x_disabled = [v for v in (xiaomo.get("disabled") or []) if isinstance(v, str)]
    print("        xiaomo enabled  (%2d 项) :" % len(x_enabled), x_enabled)
    print("        xiaomo disabled (%2d 项) :" % len(x_disabled), x_disabled)

    probe_ids = []
    for name in x_enabled[:2] + x_disabled[:2] + ["definitely-not-a-plugin"]:
        if name not in probe_ids:
            probe_ids.append(name)

    all_match = True
    for name in probe_ids:
        got = plugin_state("xiaomo", name, home=REAL_HOME)
        want_enabled = name in x_enabled and name not in x_disabled
        want_source = "disabled_list" if name in x_disabled else ("enabled_list" if name in x_enabled else "absent")
        ok = (
            got["enabled"] is want_enabled
            and got["source"] == want_source
            and got["in_enabled"] is (name in x_enabled)
            and got["in_disabled"] is (name in x_disabled)
        )
        all_match = all_match and ok
        print("          %-32s 文件=%-14s 实测=%s%s"
              % (name, want_source, dumps(got), "" if ok else "   <<< 不一致"))
    check("xiaomo 的 plugin_state 与 profiles/xiaomo/config.yaml 实际内容逐项一致", all_match,
          "核对 %d 个 plugin：%s" % (len(probe_ids), probe_ids))

    x_states = list_plugin_states("xiaomo", home=REAL_HOME)
    x_names = [item["plugin"] for item in x_states]
    check("xiaomo 的 list_plugin_states 并集 = 文件里 enabled+disabled 的并集",
          set(x_names) == set(x_enabled) | set(x_disabled) and len(x_names) == len(x_enabled) + len(x_disabled),
          "%d 项（enabled %d + disabled %d）" % (len(x_names), len(x_enabled), len(x_disabled)))
    check("xiaomo 的并集里没有任何一项是 enabled（全部被黑名单或缺失挡住）或状态与文件不符",
          all(item["enabled"] == (item["plugin"] in x_enabled and item["plugin"] not in x_disabled)
              for item in x_states))

# ---------------------------------------------------------------------------
print("[8] 生产零污染 · 后置快照")
prod_after = production_snapshot(REAL_HOME)
print_snapshot("真实配置 after", prod_after)
changed_prod = {path: (prod_before.get(path), prod_after.get(path))
                for path in set(prod_before) | set(prod_after)
                if prod_before.get(path) != prod_after.get(path)}
check("★真实配置前后 sha256 完全一致（零污染）", not changed_prod,
      dumps(changed_prod) if changed_prod else "全部一致，%d 个文件" % len(prod_after))
check("前后文件集合一致（没有新增/删除配置文件）", set(prod_before) == set(prod_after))
check("探针进程内 HERMES_HOME 始终指向假沙盒（所有写入都锚在 tempfile）",
      str(core_scope.hermes_home()).lower().startswith(str(fake_home).lower()),
      "hermes_home()=%s" % core_scope.hermes_home())

# ---------------------------------------------------------------------------
shutil.rmtree(fake_home, ignore_errors=True)
print("        沙盒已清理 :", fake_home)

print()
if failures:
    print("失败 %d 条：" % len(failures))
    for item in failures:
        print("  - " + item)
    print("PLUGIN-SWITCH-PROBE-FAILED")
    raise SystemExit(1)

print("全部检查通过（沙盒写入隔离 + 黑名单优先 + enabled 整表零改动 + %d 个真实配置文件零污染）"
      % len(prod_after))
print("PLUGIN-SWITCH-PROBE-OK")
raise SystemExit(0)

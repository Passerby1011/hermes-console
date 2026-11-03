# -*- coding: utf-8 -*-
"""hermes-console · M2c「开启走官方准入权威」验收探针（唯一验收依据）。

跑法（**不要经管道拿退出码**）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        plugins/hermes-console/scripts/probe_plugin_switch_admission.py
    echo "exit=$?"        # bash；PowerShell 用 $LASTEXITCODE

覆盖（对应卡片 6 条 + 3 条附加）::

    [1] 隔离              tempfile 假 HERMES_HOME，**import 之前**设好；造 2 个假 profile
                          （alpha / beta），并断言解析出的 home 在沙盒内
    [2] 三态语义          三种状态各造一个 plugin：state 分别 enabled / disabled / unavailable；
                          同时断言**不带** tri_state 时 plugin_state 的键集合与 M2 完全一致
                          （M2 那个已验证的探针断言了精确键集合，不许被本卡打红）
    [3] ★unavailable→enabled 走准入   alpha 里两边名单都没有的 plugin → 调用开启 →
                          断言返回 via="admission"、state_after="enabled"，且**回读文件**确认它
                          真的进了 plugins.enabled；根配置与 beta 一个字节没动
    [3b] default agent 同一通道（scope=None）也写对文件
    [4] ★拒绝不改配置      故意造一个官方会拒的插件（manifest_version 999，安装器不支持）→
                          断言 refused=True、code、且该 profile 的 config.yaml **字节摘要完全一致**
    [5] 并发保护          传一个过期的 expected_config → 断言被拒绝且配置未变
    [6] 关闭/开启黑名单回归  M2 的 deny_list 路径行为不许变（关闭→加黑名单；开启→摘黑名单，
                          不在允许清单里就照旧 warning）
    [7] strict 语义        通道不可用时 strict=True 抛错、strict=False 退化（只摘黑名单 + warning）
    [8] ★生产零污染        真实 config.yaml + profiles/*/config.yaml 开头/结尾 sha256 全一致，
                          前后哈希值都打出来

耗时：假 home 是冷的，第一次准入要付一次官方 PM 事务的建环境成本（实测 ~40s 起，联网），
之后同一 home 内的准入都是「配置提交」级（<1s）。探针会逐条打印耗时。

**探针里唯一手写的 YAML 是 tempfile 里的假 fixture**；沙盒内的每一次写入都走
``dashboard.core.plugin_switch.set_plugin_enabled`` → 官方 ``_admit_and_save_plugin_sets``。
真实 home 全程只读（第 8 条会抓）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
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


def load_yaml(path: Path):
    import yaml

    with open(path, encoding="utf-8-sig") as handle:
        return yaml.safe_load(handle) or {}


def plugins_of(path: Path) -> dict:
    node = load_yaml(path).get("plugins")
    return node if isinstance(node, dict) else {}


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def step(label: str) -> float:
    print(label)
    return time.time()


def elapsed(started: float) -> str:
    return "%.1fs" % (time.time() - started)


# ---------------------------------------------------------------------------
print("[0] 环境")
print("        python :", sys.version.split()[0])
print("        plugin :", PLUGIN_ROOT)
print("        real   :", REAL_HOME)

if not (REAL_HOME / "config.yaml").is_file():
    check("真实 home 可定位（<home>/config.yaml 存在）", False, str(REAL_HOME))
    print("PLUGIN-SWITCH-ADMISSION-PROBE-FAILED")
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
print("[1b] 造隔离沙盒：tempfile 假 HERMES_HOME + 假 profile alpha / beta")
# 用短根目录：官方准入会把 bundled 插件树拷进
#   <tmp>/installs/<id>/environments/<32hex>/workspace/plugins/config_backends/remote/
#   vendor/conformance/config-resolution/<64 字符文件名>
# 默认 temp 根（C:\Users\...\AppData\Local\Temp，39 字符）叠加后超过 Windows 260 字符上限，
# 会触发 AdmissionRefused + WinError 3（路径找不到），与业务逻辑无关。
_SHORT_TMP = Path("D:/hcp")
_SHORT_TMP.mkdir(parents=True, exist_ok=True)
fake_home = Path(tempfile.mkdtemp(prefix="a-", dir=str(_SHORT_TMP)))
(fake_home / "profiles" / "alpha").mkdir(parents=True)
(fake_home / "profiles" / "beta").mkdir(parents=True)
(fake_home / "profiles" / "alpha" / "plugins").mkdir(parents=True)
(fake_home / "plugins").mkdir(parents=True)

import yaml  # noqa: E402

# ---- fixtures（只在 tempfile 里；真实配置一律走官方写入器）----
PLUGIN_ENABLED = "alpha-plugin-a"       # 在 alpha 的 enabled 里        → 三态 enabled
PLUGIN_DISABLED = "alpha-plugin-off"    # 在 alpha 的 disabled 里       → 三态 disabled
PLUGIN_UNAVAILABLE = "alpha-plugin-new"  # 两边都没有                  → 三态 unavailable
PLUGIN_BROKEN = "broken-plugin"         # manifest_version 999，官方必须拒
PLUGIN_DEFAULT = "root-plugin-new"      # default agent 的 unavailable 用例

root_cfg = {"plugins": {"enabled": ["root-plugin-a"], "disabled": ["root-off-1"]}}
alpha_cfg = {
    "agent": {"max_turns": 33},
    "terminal": {"timeout": 123},
    "plugins": {"enabled": [PLUGIN_ENABLED], "disabled": [PLUGIN_DISABLED]},
}
beta_cfg = {"plugins": {"enabled": ["beta-plugin-a"], "disabled": ["beta-off-1"]}}

ROOT_CFG = fake_home / "config.yaml"
ALPHA_CFG = fake_home / "profiles" / "alpha" / "config.yaml"
BETA_CFG = fake_home / "profiles" / "beta" / "config.yaml"
ROOT_CFG.write_text(yaml.safe_dump(root_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
ALPHA_CFG.write_text(yaml.safe_dump(alpha_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
BETA_CFG.write_text(yaml.safe_dump(beta_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

# 官方准入会**真的校验**被选中插件的 manifest，所以造一个安装器不支持的版本号：
# pm.plugin_declarations.manifest_version_error 会以 ValueError 拒掉它 → AdmissionRefused。
broken_dir = fake_home / "profiles" / "alpha" / "plugins" / PLUGIN_BROKEN
broken_dir.mkdir(parents=True)
(broken_dir / "plugin.yaml").write_text(
    "name: %s\nmanifest_version: 999\n" % PLUGIN_BROKEN, encoding="utf-8")

print("        fake home :", fake_home)
print("        fixtures  : root(enabled=1/disabled=1) alpha(enabled=1/disabled=1) beta(enabled=1/disabled=1)")
print("                    alpha 自带 %r（官方准入会拒绝的插件）" % PLUGIN_BROKEN)

# 官方机制锚定在**进程** home 上，所以必须在 import 之前把 HERMES_HOME 指到假 home。
os.environ["HERMES_HOME"] = str(fake_home)
if HERMES_AGENT.is_dir() and str(HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT))
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from dashboard.core import scope as core_scope  # noqa: E402
from dashboard.core import plugin_switch as ps  # noqa: E402
from dashboard.core.plugin_switch import (  # noqa: E402
    CODE_ADMISSION_REFUSED,
    STATE_DISABLED,
    STATE_ENABLED,
    STATE_UNAVAILABLE,
    VIA_ADMISSION,
    VIA_DENY_LIST,
    WARN_NOT_IN_ALLOW_LIST,
    PluginSwitchAdmissionError,
    list_plugin_states,
    plugin_state,
    plugin_state_tristate,
    set_plugin_enabled,
)

resolved_home = core_scope.hermes_home()
inside_temp = str(resolved_home).lower().startswith(str(fake_home).lower())
check("home 解析锚定在假沙盒内（否则立即中止，绝不写生产）", inside_temp, "hermes_home()=%s" % resolved_home)
if not inside_temp:
    print("PLUGIN-SWITCH-ADMISSION-PROBE-FAILED")
    raise SystemExit(1)

# ---------------------------------------------------------------------------
started = step("[2] 三态语义：enabled / disabled / unavailable 各一个 plugin")
state_on = plugin_state_tristate("alpha", PLUGIN_ENABLED)
state_off = plugin_state_tristate("alpha", PLUGIN_DISABLED)
state_none = plugin_state_tristate("alpha", PLUGIN_UNAVAILABLE)
print("        %-20s → %s" % (PLUGIN_ENABLED, dumps(state_on)))
print("        %-20s → %s" % (PLUGIN_DISABLED, dumps(state_off)))
print("        %-20s → %s" % (PLUGIN_UNAVAILABLE, dumps(state_none)))
check("enabled 用例 state=%r" % STATE_ENABLED,
      state_on["state"] == STATE_ENABLED and state_on["enabled"] is True, dumps(state_on))
check("disabled 用例 state=%r" % STATE_DISABLED,
      state_off["state"] == STATE_DISABLED and state_off["enabled"] is False, dumps(state_off))
check("unavailable 用例 state=%r（**不许**当成已开启）" % STATE_UNAVAILABLE,
      state_none["state"] == STATE_UNAVAILABLE and state_none["enabled"] is False
      and state_none["source"] == "absent", dumps(state_none))
check("unavailable 的 enabled 一定是 False（界面据此不许显示成已开启）",
      state_none["enabled"] is False and state_none["in_enabled"] is False)

plain = plugin_state("alpha", PLUGIN_UNAVAILABLE)
check("不带 tri_state 时 plugin_state 键集合与 M2 完全一致（M2 探针不许被打红）",
      set(plain) == {"plugin", "enabled", "in_disabled", "in_enabled", "source"}, dumps(sorted(plain)))
check("list_plugin_states 默认也不加键（M2 口径）",
      all(set(item) == {"plugin", "enabled", "in_disabled", "in_enabled", "source"}
          for item in list_plugin_states("alpha")))
tri_list = list_plugin_states("alpha", tri_state=True)
check("list_plugin_states(tri_state=True) 每项都带 state",
      bool(tri_list) and all(item.get("state") in (STATE_ENABLED, STATE_DISABLED, STATE_UNAVAILABLE)
                             for item in tri_list),
      dumps([(i["plugin"], i["state"]) for i in tri_list]))
print("        三态耗时 :", elapsed(started))

# ---------------------------------------------------------------------------
started = step("[3] ★unavailable → enabled 走官方准入权威（假 home 冷启动，第一次会慢）")
alpha_enabled_before = list(plugins_of(ALPHA_CFG).get("enabled") or [])
root_digest_before = sha256_of(ROOT_CFG)
beta_digest_before = sha256_of(BETA_CFG)
print("        写前 alpha enabled  =", dumps(alpha_enabled_before))
print("        写前 alpha disabled =", dumps(plugins_of(ALPHA_CFG).get("disabled")))

result_on = set_plugin_enabled("alpha", PLUGIN_UNAVAILABLE, True)
print("        返回体 :", dumps(result_on))

alpha_enabled_after = list(plugins_of(ALPHA_CFG).get("enabled") or [])
alpha_disabled_after = list(plugins_of(ALPHA_CFG).get("disabled") or [])
print("        写后 alpha enabled  =", dumps(alpha_enabled_after))
print("        写后 alpha disabled =", dumps(alpha_disabled_after))
print("        写后 alpha state    =", dumps(plugin_state_tristate("alpha", PLUGIN_UNAVAILABLE)))

check("返回体 ok=True / changed=True / via='admission'",
      result_on.get("ok") is True and result_on.get("changed") is True
      and result_on.get("via") == VIA_ADMISSION, dumps(result_on))
check("返回体 state_before='unavailable' / state_after='enabled'",
      result_on.get("state_before") == STATE_UNAVAILABLE
      and result_on.get("state_after") == STATE_ENABLED, dumps(result_on))
check("★回读文件：%r 真的进了 plugins.enabled" % PLUGIN_UNAVAILABLE,
      PLUGIN_UNAVAILABLE in alpha_enabled_after, dumps(alpha_enabled_after))
check("原有 enabled 项一个没丢（%r 仍在）" % PLUGIN_ENABLED,
      PLUGIN_ENABLED in alpha_enabled_after, dumps(alpha_enabled_after))
check("disabled 未被污染（仍是 %r）" % PLUGIN_DISABLED,
      alpha_disabled_after == [PLUGIN_DISABLED], dumps(alpha_disabled_after))
check("plugin_state 回读 state='enabled' / source='enabled_list'",
      plugin_state_tristate("alpha", PLUGIN_UNAVAILABLE)["state"] == STATE_ENABLED
      and plugin_state("alpha", PLUGIN_UNAVAILABLE)["source"] == "enabled_list")
check("跨 agent 隔离：根 config.yaml 一个字节没动", sha256_of(ROOT_CFG) == root_digest_before)
check("跨 agent 隔离：beta 的 config.yaml 一个字节没动", sha256_of(BETA_CFG) == beta_digest_before)
check("alpha 的其它 section 原样保留（partial 写没吃掉 agent/terminal）",
      {k: v for k, v in (load_yaml(ALPHA_CFG) or {}).items() if k != "plugins"}
      == {"agent": {"max_turns": 33}, "terminal": {"timeout": 123}},
      dumps({k: v for k, v in (load_yaml(ALPHA_CFG) or {}).items() if k != "plugins"}))
print("        准入耗时 :", elapsed(started))

# ---------------------------------------------------------------------------
started = step("[3b] default agent（作用域 None）走同一条准入通道")
root_enabled_before = list(plugins_of(ROOT_CFG).get("enabled") or [])
alpha_digest_before = sha256_of(ALPHA_CFG)
result_default = set_plugin_enabled("default", PLUGIN_DEFAULT, True)
print("        返回体 :", dumps(result_default))
print("        写后 root enabled =", dumps(plugins_of(ROOT_CFG).get("enabled")))
check("default：via='admission' 且 state_after='enabled'",
      result_default.get("via") == VIA_ADMISSION
      and result_default.get("state_after") == STATE_ENABLED, dumps(result_default))
check("default：%r 进了根 config.yaml 的 enabled" % PLUGIN_DEFAULT,
      PLUGIN_DEFAULT in list(plugins_of(ROOT_CFG).get("enabled") or []),
      dumps(plugins_of(ROOT_CFG).get("enabled")))
check("default：根 enabled 原有项没丢", root_enabled_before[0] in (plugins_of(ROOT_CFG).get("enabled") or []))
check("default：没有波及 alpha 的 config.yaml", sha256_of(ALPHA_CFG) == alpha_digest_before)
print("        耗时 :", elapsed(started))

# ---------------------------------------------------------------------------
started = step("[4] ★拒绝不改配置：官方拒掉 manifest_version 999 的插件")
alpha_digest_before = sha256_of(ALPHA_CFG)
alpha_bytes_before = ALPHA_CFG.read_bytes()
print("        写前 alpha sha256 =", alpha_digest_before)
refused = set_plugin_enabled("alpha", PLUGIN_BROKEN, True)
print("        返回体 :", dumps(refused))
alpha_digest_after = sha256_of(ALPHA_CFG)
print("        写后 alpha sha256 =", alpha_digest_after)

check("返回体 ok=False / refused=True", refused.get("ok") is False and refused.get("refused") is True,
      dumps(refused))
check("返回体带 code（原样透出官方拒绝）", refused.get("code") == CODE_ADMISSION_REFUSED,
      dumps(refused.get("code")))
check("返回体带 message（官方原文，不吞）", bool(refused.get("message")),
      str(refused.get("message"))[:120])
check("返回体 unchanged=True", refused.get("unchanged") is True, dumps(refused))
check("★config.yaml 字节完全一致（sha256 前后相同）", alpha_digest_after == alpha_digest_before,
      "%s -> %s" % (alpha_digest_before, alpha_digest_after))
check("★config.yaml 内容逐字节相同（read_bytes 比对）", ALPHA_CFG.read_bytes() == alpha_bytes_before)
check("被拒的插件没有混进 enabled",
      PLUGIN_BROKEN not in list(plugins_of(ALPHA_CFG).get("enabled") or []),
      dumps(plugins_of(ALPHA_CFG).get("enabled")))
print("        拒绝耗时 :", elapsed(started))

# ---------------------------------------------------------------------------
started = step("[5] 并发保护：传一个过期的 expected_config")
alpha_digest_before = sha256_of(ALPHA_CFG)
stale = set_plugin_enabled("alpha", "alpha-plugin-y", True, expected_config="0" * 64)
print("        返回体 :", dumps(stale))
check("过期摘要被拒绝（ok=False / refused=True）",
      stale.get("ok") is False and stale.get("refused") is True, dumps(stale))
check("拒绝原因来自官方 CAS（message 非空）", bool(stale.get("message")), str(stale.get("message"))[:140])
check("配置未变（sha256 前后相同）", sha256_of(ALPHA_CFG) == alpha_digest_before)
check("被拒的插件没有混进 enabled",
      "alpha-plugin-y" not in list(plugins_of(ALPHA_CFG).get("enabled") or []))
print("        耗时 :", elapsed(started))

# ---------------------------------------------------------------------------
started = step("[6] M2 黑名单路径回归：关闭 / 开启 / 幂等 / 非法输入")
close_once = set_plugin_enabled("alpha", PLUGIN_ENABLED, False)
close_twice = set_plugin_enabled("alpha", PLUGIN_ENABLED, False)
print("        关闭返回体 :", dumps(close_once))
print("        写后 alpha disabled =", dumps(plugins_of(ALPHA_CFG).get("disabled")))
check("关闭：via='deny_list'，state_before='enabled' → state_after='disabled'",
      close_once.get("via") == VIA_DENY_LIST and close_once.get("state_before") == STATE_ENABLED
      and close_once.get("state_after") == STATE_DISABLED, dumps(close_once))
check("关闭：%r 进了 disabled" % PLUGIN_ENABLED,
      PLUGIN_ENABLED in (plugins_of(ALPHA_CFG).get("disabled") or []))
check("关闭：幂等（第二次 changed=False）", close_twice.get("changed") is False, dumps(close_twice))
check("关闭：**没有**走准入通道（via 仍是 deny_list）", close_twice.get("via") == VIA_DENY_LIST)

open_again = set_plugin_enabled("alpha", PLUGIN_ENABLED, True)
print("        开启返回体 :", dumps(open_again))
print("        写后 alpha enabled  =", dumps(plugins_of(ALPHA_CFG).get("enabled")))
print("        写后 alpha disabled =", dumps(plugins_of(ALPHA_CFG).get("disabled")))
check("开启（在 disabled 里）：只摘黑名单，via='deny_list'",
      open_again.get("via") == VIA_DENY_LIST and open_again.get("changed") is True, dumps(open_again))
check("开启：%r 已不在 disabled" % PLUGIN_ENABLED,
      PLUGIN_ENABLED not in (plugins_of(ALPHA_CFG).get("disabled") or []))
check("开启：state_after='enabled'（它本来就在允许清单里）",
      open_again.get("state_after") == STATE_ENABLED, dumps(open_again))
check("开启：没有 warning（enabled 里本来就有它）", "warning" not in open_again, dumps(open_again))

bad_cases = [
    ("agent '../../etc'", lambda: set_plugin_enabled("../../etc", PLUGIN_ENABLED, False)),
    ("plugin_id ''", lambda: set_plugin_enabled("alpha", "", False)),
    ("plugin_id '../escape'", lambda: set_plugin_enabled("alpha", "../escape", True)),
    ("enabled='false'（字符串）", lambda: set_plugin_enabled("alpha", PLUGIN_ENABLED, "false")),
    ("strict='yes'（字符串）", lambda: set_plugin_enabled("alpha", PLUGIN_UNAVAILABLE, True, strict="yes")),
    ("expected_config=123（非字符串）",
     lambda: set_plugin_enabled("alpha", PLUGIN_UNAVAILABLE, True, expected_config=123)),
    ("agent 不存在 'nobody'", lambda: set_plugin_enabled("nobody", PLUGIN_ENABLED, False)),
]
digest_before_bad = sha256_of(ALPHA_CFG)
for label, call in bad_cases:
    try:
        call()
        got = "没有抛异常"
    except ValueError as exc:
        got = "ValueError"
        detail = str(exc)[:80]
    except Exception as exc:  # noqa: BLE001
        got = type(exc).__name__
        detail = str(exc)[:80]
    check("%s ⇒ ValueError" % label, got == "ValueError", got if got != "ValueError" else detail)
check("非法输入全程零写入（alpha sha256 未变）", sha256_of(ALPHA_CFG) == digest_before_bad)
print("        耗时 :", elapsed(started))

# ---------------------------------------------------------------------------
started = step("[7] strict 语义：通道不可用时 strict=True 抛错、strict=False 退化")
original = ps._admit_plugin_enable


def _unavailable(*_args, **_kwargs):
    raise PluginSwitchAdmissionError(ps.CODE_ADMISSION_UNAVAILABLE, "探针注入：通道不可用")


ps._admit_plugin_enable = _unavailable
try:
    try:
        set_plugin_enabled("alpha", "alpha-plugin-strict", True)
        strict_outcome = "没有抛异常"
    except PluginSwitchAdmissionError as exc:
        strict_outcome = "PluginSwitchAdmissionError: %s" % str(exc)[:60]
    except Exception as exc:  # noqa: BLE001
        strict_outcome = "%s: %s" % (type(exc).__name__, str(exc)[:60])

    digest_before_degraded = sha256_of(ALPHA_CFG)
    degraded = set_plugin_enabled("alpha", "alpha-plugin-strict", True, strict=False)
    print("        strict=True   :", strict_outcome)
    print("        strict=False  :", dumps(degraded))
    check("strict=True：通道不可用 ⇒ 抛 PluginSwitchAdmissionError（不假装成功）",
          strict_outcome.startswith("PluginSwitchAdmissionError"), strict_outcome)
    check("strict=False：退化为黑名单路径（via='deny_list'）",
          degraded.get("via") == VIA_DENY_LIST, dumps(degraded))
    check("strict=False：如实带 warning（没有偷偷塞进 enabled）",
          degraded.get("warning") == WARN_NOT_IN_ALLOW_LIST
          and degraded.get("enabled") is False, dumps(degraded))
    check("strict=False：退化时也确实没写盘", sha256_of(ALPHA_CFG) == digest_before_degraded)
    check("strict=False：notes 里写明退化原因",
          any("退化为只摘黑名单" in note for note in degraded.get("notes") or []),
          dumps(degraded.get("notes")))
finally:
    ps._admit_plugin_enable = original
print("        耗时 :", elapsed(started))

# ---------------------------------------------------------------------------
started = step("[8] 生产零污染 · 后置快照")
prod_after = production_snapshot(REAL_HOME)
print_snapshot("真实配置 after", prod_after)
changed_prod = {path: (prod_before.get(path), prod_after.get(path))
                for path in set(prod_before) | set(prod_after)
                if prod_before.get(path) != prod_after.get(path)}
check("★真实配置前后 sha256 完全一致（零污染）", not changed_prod,
      dumps(changed_prod) if changed_prod else "全部一致，%d 个文件" % len(prod_after))
check("前后文件集合一致（没有新增/删除配置文件）", set(prod_before) == set(prod_after))
check("探针进程内 HERMES_HOME 始终指向假沙盒",
      str(core_scope.hermes_home()).lower().startswith(str(fake_home).lower()),
      "hermes_home()=%s" % core_scope.hermes_home())
print("        耗时 :", elapsed(started))

# ---------------------------------------------------------------------------
shutil.rmtree(fake_home, ignore_errors=True)
print("        沙盒已清理 :", fake_home)

print()
if failures:
    print("失败 %d 条：" % len(failures))
    for item in failures:
        print("  - " + item)
    print("PLUGIN-SWITCH-ADMISSION-PROBE-FAILED")
    raise SystemExit(1)

print("全部检查通过（三态显式 + unavailable→enabled 走官方准入 + 拒绝零写入 + 并发 CAS + "
      "%d 个真实配置文件零污染）" % len(prod_after))
print("PLUGIN-SWITCH-ADMISSION-PROBE-OK")
raise SystemExit(0)

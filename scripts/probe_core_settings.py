# -*- coding: utf-8 -*-
"""hermes-console · M1a 配置分层核心 验收探针（唯一验收依据）。

跑法（**不要经管道拿退出码**）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        plugins/hermes-console/scripts/probe_core_settings.py
    echo $LASTEXITCODE      # PowerShell；bash 用 echo $?

覆盖（对应卡片的 8 条 + 2 条附加）：

1. 隔离：``tempfile`` 假 HERMES_HOME + 2 个假 profile（alpha/beta），全程只碰假 home
2. 分层顺序：module_default → global → agent，三层冲突键 agent 赢且 source=="agent"
3. 来源标注：三个键分别命中三个来源，逐个断言 ``source``
4. 写入隔离：``save_setting("alpha", ...)`` 只改 alpha，beta 与根配置不动
5. 读回一致：写进去的值 ``read_plugin_settings("alpha")`` 原样读回（含嵌套路径）
6. 缺文件容错：不存在的 agent / 没有 config.yaml 的 agent 都返回 ``{}`` 不抛异常
7. 非法 agent：``normalize_agent_id("../../etc")`` 抛 ``ValueError`` 且消息含合法 id 列表
8. 生产零污染：真实 ``config.yaml`` 与 ``profiles/*/config.yaml`` 前后 sha256 完全一致
9. （附加）``reset_setting`` 的"置空 → 回落全局"语义
10. （附加）真实 home 下 ``list_agents()`` 返回 9 个 agent（default + 8 profile）

**关于探针里的 yaml 写入**：只有 ``tempfile`` 里的一次性 fixture 用 ``yaml.safe_dump`` 铺底；
对"真配置"的每一次写入都走 ``dashboard.core.settings.save_setting`` → 官方
``save_plugin_setting``。真实 home 全程只读（第 8 条会抓）。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]          # <home>/plugins/hermes-console
REAL_HOME = Path(__file__).resolve().parents[3]            # <home>
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
        print("          %s  %s" % (digest[:16], path))


# ---------------------------------------------------------------------------
print("[0] 环境")
print("        python :", sys.version.split()[0])
print("        plugin :", PLUGIN_ROOT)
print("        real   :", REAL_HOME)

if not (REAL_HOME / "config.yaml").is_file():
    check("真实 home 可定位（<home>/config.yaml 存在）", False, str(REAL_HOME))
    print("CORE-SETTINGS-PROBE-FAILED")
    raise SystemExit(1)

# ---------------------------------------------------------------------------
print("[1] 生产零污染 · 前置快照（在任何 import / 任何写入之前）")
prod_before = production_snapshot(REAL_HOME)
print_snapshot("真实配置 before", prod_before)
check("前置快照非空且无读取错误", bool(prod_before) and all("<error:" not in v for v in prod_before.values()))

# ---------------------------------------------------------------------------
print("[1b] 造隔离沙盒：tempfile 假 HERMES_HOME + 假 profile alpha / beta / gamma")
fake_home = Path(tempfile.mkdtemp(prefix="hc-core-probe-"))
(fake_home / "profiles" / "alpha").mkdir(parents=True)
(fake_home / "profiles" / "beta").mkdir(parents=True)
(fake_home / "profiles" / "gamma").mkdir(parents=True)   # 只有 profile.yaml，没有 config.yaml

# 一次性 fixture 铺底（只在 tempfile 里；真实配置一律走官方写入器）
import yaml  # noqa: E402

root_cfg = {
    "plugins": {
        "entries": {
            "hermes-console": {
                "settings": {
                    "modules": {
                        "demo": {
                            "mode": "global",
                            "enabled": False,
                            "poll_seconds": 60,
                            "label": "global-label",
                        }
                    },
                    "global_only": "root-value",
                }
            }
        }
    }
}
alpha_cfg = {
    "plugins": {
        "entries": {
            "hermes-console": {
                "settings": {
                    "modules": {
                        "demo": {
                            "mode": "agent",
                            "enabled": True,
                            "label": "alpha-label",
                            "timeout": 9,
                        }
                    },
                    "agent_only": "alpha-value",
                }
            }
        }
    }
}
beta_cfg = {
    "plugins": {
        "entries": {
            "hermes-console": {
                "settings": {
                    "modules": {
                        "demo": {
                            "mode": "beta",
                            "enabled": False,
                            "poll_seconds": 30,
                            "label": "beta-label",
                        }
                    },
                    "agent_only": "beta-value",
                }
            }
        }
    }
}
gamma_profile = {"ui_meta": {"hermes-bots": {"title": "Gamma Bot"}}}

(fake_home / "config.yaml").write_text(yaml.safe_dump(root_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
(fake_home / "profiles" / "alpha" / "config.yaml").write_text(yaml.safe_dump(alpha_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
(fake_home / "profiles" / "beta" / "config.yaml").write_text(yaml.safe_dump(beta_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
(fake_home / "profiles" / "gamma" / "profile.yaml").write_text(yaml.safe_dump(gamma_profile, allow_unicode=True, sort_keys=False), encoding="utf-8")

print("        fake home :", fake_home)
print("        fixtures  : default / alpha / beta 各有 config.yaml，gamma 只有 profile.yaml")

# 官方机制锚定在**进程** home 上，所以必须在 import 之前把 HERMES_HOME 指到假 home。
os.environ["HERMES_HOME"] = str(fake_home)
if HERMES_AGENT.is_dir() and str(HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT))
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from dashboard.core import scope as core_scope  # noqa: E402
from dashboard.core.settings import (  # noqa: E402
    PLUGIN_ID,
    SOURCE_AGENT,
    SOURCE_GLOBAL,
    SOURCE_MODULE_DEFAULT,
    effective_settings,
    read_plugin_settings,
    reset_setting,
    save_setting,
)

# 沙盒护栏：解析出来的 home 必须在 tempfile 里，否则立刻停手（一行都不许写）
resolved_home = core_scope.hermes_home()
inside_temp = str(resolved_home).lower().startswith(str(fake_home).lower())
check(
    "home 解析锚定在假沙盒内（否则立即中止，绝不写生产）",
    inside_temp,
    "hermes_home()=%s" % resolved_home,
)
if not inside_temp:
    print("CORE-SETTINGS-PROBE-FAILED")
    raise SystemExit(1)

# ---------------------------------------------------------------------------
print("[2] 隔离：假 home 里只有 default / alpha / beta / gamma")
agents = core_scope.list_agents()
ids = [agent["id"] for agent in agents]
print("        list_agents() =", ids)
check("agent 枚举 = default+alpha+beta+gamma", ids == ["default", "alpha", "beta", "gamma"], str(ids))
for agent in agents:
    print(
        "        %-8s home=%s config_path=%s exists=%s label=%s"
        % (agent["id"], agent["home"], agent["config_path"].name, agent["exists"], agent["label"])
    )
check(
    "每个 agent 条目含 id/home/config_path/exists 且类型正确",
    all(
        isinstance(a["id"], str)
        and isinstance(a["home"], Path)
        and isinstance(a["config_path"], Path)
        and isinstance(a["exists"], bool)
        for a in agents
    ),
)
check(
    "alpha/beta 的 config_path 落在各自 profile 目录",
    core_scope.agent_config_path("alpha") == fake_home / "profiles" / "alpha" / "config.yaml"
    and core_scope.agent_config_path("beta") == fake_home / "profiles" / "beta" / "config.yaml"
    and core_scope.agent_config_path("default") == fake_home / "config.yaml",
)
check("gamma 的 exists=False（没有自己的 config.yaml）", [a for a in agents if a["id"] == "gamma"][0]["exists"] is False)

alpha_settings = read_plugin_settings("alpha")
beta_settings = read_plugin_settings("beta")
check(
    "alpha 与 beta 读到各自的 settings（互不串台）",
    alpha_settings["modules"]["demo"]["label"] == "alpha-label"
    and beta_settings["modules"]["demo"]["label"] == "beta-label",
    "alpha=%s beta=%s" % (alpha_settings["modules"]["demo"]["label"], beta_settings["modules"]["demo"]["label"]),
)

# ---------------------------------------------------------------------------
print("[3] 分层顺序 + [4] 来源标注（module_default < global < agent）")
DEFAULTS = {
    "mode": "module",           # 三层都有 → agent 赢
    "enabled": False,           # 默认 + 全局有，agent 也有 → agent 赢
    "poll_seconds": 5,          # 默认 + 全局有，agent 没有 → 全局赢
    "retry_limit": 3,           # 只有默认有 → module_default
    "label": "module-label",    # 三层都有 → agent 赢
}
eff_alpha = effective_settings("alpha", "demo", DEFAULTS)
print("        effective_settings('alpha', 'demo')：")
for key in sorted(eff_alpha):
    print("          %-14s value=%-14r source=%s" % (key, eff_alpha[key]["value"], eff_alpha[key]["source"]))

expected_alpha = {
    "mode": ("agent", SOURCE_AGENT),
    "enabled": (True, SOURCE_AGENT),
    "label": ("alpha-label", SOURCE_AGENT),
    "timeout": (9, SOURCE_AGENT),
    "poll_seconds": (60, SOURCE_GLOBAL),
    "retry_limit": (3, SOURCE_MODULE_DEFAULT),
}
for key, (want_value, want_source) in expected_alpha.items():
    got = eff_alpha.get(key) or {}
    check(
        "alpha/demo.%s == %r（source=%s）" % (key, want_value, want_source),
        got.get("value") == want_value and got.get("source") == want_source,
        "实测 value=%r source=%r" % (got.get("value"), got.get("source")),
    )

three_way = {k: eff_alpha[k]["source"] for k in ("mode", "enabled", "poll_seconds", "retry_limit")}
check(
    "三个来源各有键命中（agent/global/module_default 都出现）",
    set(three_way.values()) == {SOURCE_AGENT, SOURCE_GLOBAL, SOURCE_MODULE_DEFAULT},
    str(three_way),
)
check(
    "每个返回值都带 value+source（界面不用猜）",
    all(set(entry) == {"value", "source"} for entry in eff_alpha.values()),
)

eff_beta = effective_settings("beta", "demo", DEFAULTS)
print("        effective_settings('beta', 'demo')：")
for key in sorted(eff_beta):
    print("          %-14s value=%-14r source=%s" % (key, eff_beta[key]["value"], eff_beta[key]["source"]))
check(
    "同一模块在不同 agent 上得到不同有效值（beta.enabled=False / beta.poll_seconds=30 均来自 agent）",
    eff_beta["enabled"] == {"value": False, "source": SOURCE_AGENT}
    and eff_beta["poll_seconds"] == {"value": 30, "source": SOURCE_AGENT}
    and eff_beta["retry_limit"] == {"value": 3, "source": SOURCE_MODULE_DEFAULT},
)

flat_all = effective_settings("alpha", None)
print("        effective_settings('alpha', None) 扁平视图键：", sorted(flat_all))
check(
    "module_id 为空 → 全部 settings 的扁平视图（含 modules.demo.* 前缀）",
    flat_all["modules.demo.mode"] == {"value": "agent", "source": SOURCE_AGENT}
    and flat_all["agent_only"] == {"value": "alpha-value", "source": SOURCE_AGENT},
)

# ---------------------------------------------------------------------------
print("[5] 写入隔离：save_setting('alpha', ...) 只动 alpha")
fake_root_before = sha256_of(fake_home / "config.yaml")
alpha_before = sha256_of(fake_home / "profiles" / "alpha" / "config.yaml")
beta_before = sha256_of(fake_home / "profiles" / "beta" / "config.yaml")

save_setting("alpha", ("modules", "demo", "enabled"), False)
save_setting("alpha", ("modules", "demo", "retry", "max"), 7)

alpha_after = sha256_of(fake_home / "profiles" / "alpha" / "config.yaml")
beta_after = sha256_of(fake_home / "profiles" / "beta" / "config.yaml")
fake_root_after = sha256_of(fake_home / "config.yaml")
print("        alpha sha256 %s → %s" % (alpha_before[:16], alpha_after[:16]))
print("        beta  sha256 %s → %s" % (beta_before[:16], beta_after[:16]))
print("        root  sha256 %s → %s" % (fake_root_before[:16], fake_root_after[:16]))
check("alpha 的 config.yaml 变了", alpha_before != alpha_after)
check("beta 的 config.yaml 没变", beta_before == beta_after)
check("根 config.yaml（全局层）没被 agent 写入碰到", fake_root_before == fake_root_after)

alpha_now = read_plugin_settings("alpha")
beta_now = read_plugin_settings("beta")
check("alpha 落盘后 enabled=False", alpha_now["modules"]["demo"]["enabled"] is False)
check("beta 内容与写入前完全一致", beta_now == beta_settings, str(beta_now["modules"]["demo"]))
check("alpha 原有其它键未被写坏（mode/label/timeout 保留）",
      alpha_now["modules"]["demo"]["mode"] == "agent"
      and alpha_now["modules"]["demo"]["label"] == "alpha-label"
      and alpha_now["modules"]["demo"]["timeout"] == 9,
      str(alpha_now["modules"]["demo"]))

print("        alpha config.yaml 落盘内容：")
for line in (fake_home / "profiles" / "alpha" / "config.yaml").read_text(encoding="utf-8").splitlines():
    print("          | " + line)

print("[5b] 全局层写入：save_setting('default', ...) 只动根 config.yaml")
alpha_before_global = sha256_of(fake_home / "profiles" / "alpha" / "config.yaml")
beta_before_global = sha256_of(fake_home / "profiles" / "beta" / "config.yaml")
save_setting("default", ("modules", "demo", "poll_seconds"), 45)
fake_root_after2 = sha256_of(fake_home / "config.yaml")
print("        root  sha256 %s → %s" % (fake_root_after[:16], fake_root_after2[:16]))
print("        read_plugin_settings('default').modules.demo.poll_seconds =",
      read_plugin_settings("default")["modules"]["demo"]["poll_seconds"])
check("根 config.yaml 变了（全局层写入生效）", fake_root_after != fake_root_after2)
check("全局写入没碰 alpha / beta 的 config.yaml",
      sha256_of(fake_home / "profiles" / "alpha" / "config.yaml") == alpha_before_global
      and sha256_of(fake_home / "profiles" / "beta" / "config.yaml") == beta_before_global)
check(
    "全局值变化透传到没有覆盖的 agent（alpha.poll_seconds 45 ← global）",
    effective_settings("alpha", "demo", DEFAULTS)["poll_seconds"] == {"value": 45, "source": SOURCE_GLOBAL},
    str(effective_settings("alpha", "demo", DEFAULTS)["poll_seconds"]),
)
check(
    "有自己覆盖的 agent 不受全局变化影响（beta.poll_seconds 仍是自己的 30）",
    effective_settings("beta", "demo", DEFAULTS)["poll_seconds"] == {"value": 30, "source": SOURCE_AGENT},
    str(effective_settings("beta", "demo", DEFAULTS)["poll_seconds"]),
)

# ---------------------------------------------------------------------------
print("[6] 读回一致：写进去的值能原样读回")
check("read_plugin_settings('alpha') 读回 enabled=False", alpha_now["modules"]["demo"]["enabled"] is False)
check("嵌套路径 modules.demo.retry.max 读回 7", alpha_now["modules"]["demo"]["retry"]["max"] == 7)
eff_after = effective_settings("alpha", "demo", DEFAULTS)
check(
    "写入后 effective_settings 同步更新（enabled=False / retry.max=7 且 source=agent）",
    eff_after["enabled"] == {"value": False, "source": SOURCE_AGENT}
    and eff_after["retry.max"] == {"value": 7, "source": SOURCE_AGENT},
    "enabled=%s retry.max=%s" % (eff_after["enabled"], eff_after.get("retry.max")),
)
check("写入值类型保持（bool 仍是 bool，int 仍是 int）",
      isinstance(alpha_now["modules"]["demo"]["enabled"], bool)
      and isinstance(alpha_now["modules"]["demo"]["retry"]["max"], int)
      and not isinstance(alpha_now["modules"]["demo"]["retry"]["max"], bool))

# ---------------------------------------------------------------------------
print("[7] 缺文件容错：不抛异常，返回 {}")
try:
    missing_cfg = core_scope.read_agent_config("nonexistent")
    missing_ok = missing_cfg == {}
    missing_detail = repr(missing_cfg)
except Exception as exc:  # noqa: BLE001 - 抛了就是 FAIL
    missing_ok, missing_detail = False, "抛异常 %s: %s" % (type(exc).__name__, exc)
check("read_agent_config('nonexistent') == {} 且不抛异常", missing_ok, missing_detail)

try:
    missing_settings = read_plugin_settings("nonexistent")
    missing_settings_ok = missing_settings == {}
    missing_settings_detail = repr(missing_settings)
except Exception as exc:  # noqa: BLE001
    missing_settings_ok, missing_settings_detail = False, "抛异常 %s: %s" % (type(exc).__name__, exc)
check("read_plugin_settings('nonexistent') == {} 且不抛异常", missing_settings_ok, missing_settings_detail)

try:
    gamma_cfg = core_scope.read_agent_config("gamma")
    gamma_settings = read_plugin_settings("gamma")
    gamma_ok = gamma_cfg == {} and gamma_settings == {}
    gamma_detail = "config=%r settings=%r" % (gamma_cfg, gamma_settings)
except Exception as exc:  # noqa: BLE001
    gamma_ok, gamma_detail = False, "抛异常 %s: %s" % (type(exc).__name__, exc)
check("gamma（目录在但没有 config.yaml）→ {} 且不抛异常", gamma_ok, gamma_detail)
check("gamma 的有效值只剩模块默认层",
      effective_settings("gamma", "demo", DEFAULTS)["retry_limit"] == {"value": 3, "source": SOURCE_MODULE_DEFAULT})

# ---------------------------------------------------------------------------
print("[8] 非法 agent：normalize_agent_id 抛 ValueError 且消息含合法 id")
for alias, want in ((None, "default"), ("", "default"), ("current", "default"), ("default", "default"), ("Alpha", "alpha")):
    try:
        got = core_scope.normalize_agent_id(alias)
    except Exception as exc:  # noqa: BLE001
        got = "抛异常 %s: %s" % (type(exc).__name__, exc)
    check("normalize_agent_id(%r) == %r" % (alias, want), got == want, "实测 %r" % (got,))

bad = "../../etc"
try:
    core_scope.normalize_agent_id(bad)
    bad_ok, bad_detail = False, "没有抛异常"
except ValueError as exc:
    message = str(exc)
    bad_ok = all(legal in message for legal in ("default", "alpha", "beta"))
    bad_detail = message
except Exception as exc:  # noqa: BLE001
    bad_ok, bad_detail = False, "抛的是 %s: %s" % (type(exc).__name__, exc)
check("normalize_agent_id('../../etc') 抛 ValueError 且消息列出合法 id", bad_ok, bad_detail)

try:
    save_setting("../../etc", ("modules", "demo", "enabled"), True)
    bad_save_ok, bad_save_detail = False, "没有抛异常"
except ValueError as exc:
    bad_save_ok, bad_save_detail = True, str(exc)[:120]
except Exception as exc:  # noqa: BLE001
    bad_save_ok, bad_save_detail = False, "抛的是 %s: %s" % (type(exc).__name__, exc)
check("save_setting 对非法 id 也抛 ValueError（不会摸到文件）", bad_save_ok, bad_save_detail)

# ---------------------------------------------------------------------------
print("[9] 附加：reset_setting 的「置空 → 回落全局」语义")
before_reset = effective_settings("alpha", "demo", DEFAULTS)["label"]
reset_setting("alpha", ("modules", "demo", "label"))
alpha_raw = read_plugin_settings("alpha")
after_reset = effective_settings("alpha", "demo", DEFAULTS)["label"]
print("        reset 前 label=%s；raw 里现在 label=%r；reset 后 label=%s"
      % (before_reset, alpha_raw["modules"]["demo"].get("label"), after_reset))
check("reset 前 label 来自 agent", before_reset == {"value": "alpha-label", "source": SOURCE_AGENT}, str(before_reset))
check("reset 后 raw 里是显式 null（官方写入器没有删除语义）", alpha_raw["modules"]["demo"].get("label") is None)
check("reset 后有效值回落到 global", after_reset == {"value": "global-label", "source": SOURCE_GLOBAL}, str(after_reset))
check("reset 只影响 alpha，beta 的 label 仍是自己的", read_plugin_settings("beta")["modules"]["demo"]["label"] == "beta-label")

# ---------------------------------------------------------------------------
print("[10] 附加：真实 home 下 list_agents() 返回 9 个 agent（default + 8 profile）")
real_agents = core_scope.list_agents(home=REAL_HOME)
real_ids = [agent["id"] for agent in real_agents]
print("        real ids :", real_ids)
for agent in real_agents:
    print("          %-16s exists=%s  %s" % (agent["id"], agent["exists"], agent["config_path"]))
check("真实 home 有 9 个 agent", len(real_agents) == 9, str(len(real_agents)))
check("含 default", real_ids[0] == "default", str(real_ids))
check("全部 8 个 profile 都有 config.yaml（exists=True）", all(a["exists"] for a in real_agents))
check("真实 home 解析正确（default 指向 <home>/config.yaml）",
      core_scope.agent_config_path("default", home=REAL_HOME) == REAL_HOME / "config.yaml")

# ---------------------------------------------------------------------------
print("[11] 生产零污染 · 后置快照")
prod_after = production_snapshot(REAL_HOME)
print_snapshot("真实配置 after", prod_after)
changed = {path: (prod_before.get(path), prod_after.get(path)) for path in set(prod_before) | set(prod_after)
           if prod_before.get(path) != prod_after.get(path)}
check("真实配置前后 sha256 完全一致（零污染）", not changed, str(changed) if changed else "全部一致，%d 个文件" % len(prod_after))
check("前后文件集合一致（没有新增/删除配置文件）", set(prod_before) == set(prod_after))

# ---------------------------------------------------------------------------
shutil.rmtree(fake_home, ignore_errors=True)
print("        沙盒已清理 :", fake_home)

print()
if failures:
    print("失败 %d 条：" % len(failures))
    for item in failures:
        print("  - " + item)
    print("CORE-SETTINGS-PROBE-FAILED")
    raise SystemExit(1)

print("全部检查通过（%d 个 agent 的真实 home 零污染，沙盒写入隔离已验证）" % len(real_agents))
print("CORE-SETTINGS-PROBE-OK")
raise SystemExit(0)

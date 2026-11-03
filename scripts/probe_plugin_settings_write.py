# -*- coding: utf-8 -*-
"""hermes-console · M2b 通用插件配置写入 验收探针（唯一验收依据）。

跑法（**不要经管道拿退出码**）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        plugins/hermes-console/scripts/probe_plugin_settings_write.py
    echo "exit=$?"        # bash；PowerShell 用 $LASTEXITCODE

覆盖（对应卡片的 8 条 + 真实 home 只读抽查）::

    [1] 隔离              tempfile 假 HERMES_HOME（**在 import 之前**设好环境变量）+ 假 profile
                          alpha / beta，各自带不同的 hermes-console / browserpaw settings
    [2] 写非本插件        save_plugin_setting_for('alpha', 'browserpaw', ...) ⇒ 落
                          plugins.entries.browserpaw.settings，且 read_plugin_settings 回读一致
    [3] ★不串味           alpha 写 browserpaw 后：alpha 的 hermes-console settings 深比较**不变**、
                          beta 的 browserpaw 也**不变**
    [4] 写本插件仍可用     save_plugin_setting_for('alpha', 'hermes-console', ...) 正常
    [5] 非法输入          plugin_id='' / '../evil'、agent_id='../../etc' ⇒ ValueError，
                          且沙盒整棵树 sha256 快照**一个字节都没变**（不产生文件写入）
    [6] 嵌套插件 id        'web/xai' 能写能回读，且真的落在 entries["web/xai"]
    [7] 脱敏              mask_sensitive({'api_key','token','normal'}) 前两个变 ***、
                          normal 保留、masked_keys 正确
    [8] ★生产零污染       真实 config.yaml + profiles/*/config.yaml 开头/结尾 sha256 全一致
    [9] 真实 home 只读抽查 list_plugin_ids('xiaomo') == profiles/xiaomo/config.yaml 里
                          entries ∪ enabled ∪ disabled（只读，不写真实配置）

**探针里唯一手写的 YAML 是 tempfile 里的假 fixture**；对"配置"的每一次写入都走
``dashboard.core.settings.save_plugin_setting_for`` → 官方 ``save_plugin_setting``。
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
    """整棵目录的文件内容快照（断言"一个字节都没写"用）。"""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = sha256_of(path)
    return out


def load_yaml(path: Path):
    import yaml

    with open(path, encoding="utf-8-sig") as handle:
        return yaml.safe_load(handle) or {}


def entries_of(path: Path) -> dict:
    node = (load_yaml(path).get("plugins") or {}).get("entries")
    return node if isinstance(node, dict) else {}


def settings_of(path: Path, plugin_id: str) -> dict:
    entry = entries_of(path).get(plugin_id)
    if not isinstance(entry, dict):
        return {}
    node = entry.get("settings")
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
    print("PLUGIN-SETTINGS-WRITE-PROBE-FAILED")
    raise SystemExit(1)

# ---------------------------------------------------------------------------
print("[8-pre] 生产零污染 · 前置快照（在任何 import / 任何写入之前）")
prod_before = production_snapshot(REAL_HOME)
print_snapshot("真实配置 before", prod_before)
check(
    "前置快照非空且无读取错误",
    bool(prod_before) and all(not v.startswith("<error:") for v in prod_before.values()),
)

# 真实 home 只读抽查所需的数据也在这里先读好（纯读，不写）
XIAOMO_CFG = REAL_HOME / "profiles" / "xiaomo" / "config.yaml"
xiaomo_plugins = (load_yaml(XIAOMO_CFG).get("plugins") or {}) if XIAOMO_CFG.is_file() else {}
xiaomo_entries = xiaomo_plugins.get("entries") if isinstance(xiaomo_plugins.get("entries"), dict) else {}
xiaomo_enabled = [v for v in (xiaomo_plugins.get("enabled") or []) if isinstance(v, str)]
xiaomo_disabled = [v for v in (xiaomo_plugins.get("disabled") or []) if isinstance(v, str)]
xiaomo_expect = set(xiaomo_entries) | set(xiaomo_enabled) | set(xiaomo_disabled)

# ---------------------------------------------------------------------------
print("[1] 造隔离沙盒：tempfile 假 HERMES_HOME + 假 profile alpha / beta")
fake_home = Path(tempfile.mkdtemp(prefix="hc-plugin-settings-write-probe-"))
(fake_home / "profiles" / "alpha").mkdir(parents=True)
(fake_home / "profiles" / "beta").mkdir(parents=True)

import yaml  # noqa: E402

CONSOLE_ALPHA = {"modules": {"demo": {"enabled": True, "label": "计"}}, "theme": "dark"}
CONSOLE_BETA = {"modules": {"demo": {"enabled": False}}, "theme": "light"}
BROWSERPAW_ALPHA = {"headless": False, "timeout": 5}
BROWSERPAW_BETA = {"headless": True, "timeout": 99}
XAI_ALPHA = {"endpoint": "https://old.example", "model": "grok-legacy"}

root_cfg = {
    "plugins": {
        "enabled": ["root-plugin-a"],
        "entries": {"hermes-console": {"settings": {"theme": "system"}}},
    }
}
alpha_cfg = {
    "agent": {"max_turns": 33},
    "plugins": {
        "enabled": ["alpha-plugin-a", "browserpaw", "hermes-console"],
        "disabled": ["alpha-off-1"],
        "entries": {
            "hermes-console": {"settings": CONSOLE_ALPHA},
            "browserpaw": {"settings": BROWSERPAW_ALPHA},
            "web/xai": {"settings": XAI_ALPHA},
        },
    },
}
beta_cfg = {
    "plugins": {
        "enabled": ["beta-plugin-a", "browserpaw"],
        "entries": {
            "hermes-console": {"settings": CONSOLE_BETA},
            "browserpaw": {"settings": BROWSERPAW_BETA},
        },
    },
}

ROOT_CFG = fake_home / "config.yaml"
ALPHA_CFG = fake_home / "profiles" / "alpha" / "config.yaml"
BETA_CFG = fake_home / "profiles" / "beta" / "config.yaml"
ROOT_CFG.write_text(yaml.safe_dump(root_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
ALPHA_CFG.write_text(yaml.safe_dump(alpha_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
BETA_CFG.write_text(yaml.safe_dump(beta_cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")

print("        fake home :", fake_home)
print("        alpha.hermes-console :", dumps(CONSOLE_ALPHA))
print("        alpha.browserpaw    :", dumps(BROWSERPAW_ALPHA))
print("        beta.browserpaw     :", dumps(BROWSERPAW_BETA))

# 官方写入器 / 作用域锚定在**进程** home 上，所以必须在 import 之前把 HERMES_HOME 指到假 home。
os.environ["HERMES_HOME"] = str(fake_home)
if HERMES_AGENT.is_dir() and str(HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT))
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from dashboard.core import scope as core_scope  # noqa: E402
from dashboard.core.settings import (  # noqa: E402
    PluginSettingsWriteError,
    list_plugin_ids,
    mask_sensitive,
    normalize_plugin_id,
    read_plugin_settings,
    read_setting,
    save_plugin_setting_for,
    save_setting,
)

# 沙盒护栏：解析出来的 home 必须在 tempfile 里，否则立刻停手（一个字节都不写）
resolved_home = core_scope.hermes_home()
inside_temp = str(resolved_home).lower().startswith(str(fake_home).lower())
check("home 解析锚定在假沙盒内（否则立即中止，绝不写生产）", inside_temp, "hermes_home()=%s" % resolved_home)
if not inside_temp:
    print("PLUGIN-SETTINGS-WRITE-PROBE-FAILED")
    raise SystemExit(1)

check(
    "alpha / beta / default 的 config 路径各就各位",
    core_scope.agent_config_path("alpha") == ALPHA_CFG
    and core_scope.agent_config_path("beta") == BETA_CFG
    and core_scope.agent_config_path("default") == ROOT_CFG,
)

# ---------------------------------------------------------------------------
print("[2] 写非本插件：alpha 的 browserpaw")
result = save_plugin_setting_for("alpha", "browserpaw", ("headless",), True)
print("        返回 :", dumps({k: result[k] for k in ("agent_id", "plugin_id", "segments", "previous", "value", "verified")}))
check(
    "返回体带 verified=True / 记录了 previous",
    result["verified"] is True and result["previous"] is False and result["agent_id"] == "alpha",
    dumps(result),
)
check(
    "落盘位置 = plugins.entries.browserpaw.settings.headless（文件里读得到）",
    settings_of(ALPHA_CFG, "browserpaw").get("headless") is True,
    dumps(settings_of(ALPHA_CFG, "browserpaw")),
)
back = read_plugin_settings("alpha", "browserpaw")
print("        read_plugin_settings('alpha','browserpaw') =", dumps(back))
check(
    "read_plugin_settings 回读一致（headless=True，timeout 保留 5）",
    back.get("headless") is True and back.get("timeout") == 5,
    dumps(back),
)
check(
    "写后回读校验真的走了官方读路径（read_setting 也一致）",
    read_setting("alpha", ("headless",), plugin_id="browserpaw") is True,
)

# ---------------------------------------------------------------------------
print("[3] ★不串味：alpha 写 browserpaw 后，alpha 的 hermes-console 与 beta 的 browserpaw 都没变")
alpha_console_now = read_plugin_settings("alpha", "hermes-console", home=fake_home)
beta_paw_now = read_plugin_settings("beta", "browserpaw", home=fake_home)
print("        alpha.hermes-console 深比较 :", "相同" if alpha_console_now == CONSOLE_ALPHA else dumps(alpha_console_now))
print("        alpha.hermes-console 文件值 :", dumps(settings_of(ALPHA_CFG, "hermes-console")))
print("        beta.browserpaw      文件值 :", dumps(settings_of(BETA_CFG, "browserpaw")))
check(
    "★alpha 的 hermes-console settings 完全没变（深比较 == 且 json 一致）",
    alpha_console_now == CONSOLE_ALPHA and dumps(alpha_console_now) == dumps(CONSOLE_ALPHA),
    dumps(alpha_console_now),
)
check(
    "★beta 的 browserpaw 完全没变（深比较 == 且 json 一致）",
    beta_paw_now == BROWSERPAW_BETA and dumps(beta_paw_now) == dumps(BROWSERPAW_BETA),
    dumps(beta_paw_now),
)
check(
    "根配置 (default) 的 entries 完全没被碰过",
    settings_of(ROOT_CFG, "hermes-console") == {"theme": "system"},
    dumps(settings_of(ROOT_CFG, "hermes-console")),
)
check(
    "list_plugin_ids('alpha') = entries ∪ enabled ∪ disabled",
    set(list_plugin_ids("alpha")) == {"hermes-console", "browserpaw", "web/xai", "alpha-plugin-a", "alpha-off-1"},
    str(list_plugin_ids("alpha")),
)
check(
    "list_plugin_ids('beta') 与 alpha 不串台",
    set(list_plugin_ids("beta")) == {"beta-plugin-a", "browserpaw", "hermes-console"},
    str(list_plugin_ids("beta")),
)

# ---------------------------------------------------------------------------
print("[4] 写本插件（hermes-console）仍然可用")
res_console = save_plugin_setting_for("alpha", "hermes-console", ("modules", "demo", "label"), "通用设置页")
print("        返回 :", dumps(res_console))
check(
    "save_plugin_setting_for(alpha,'hermes-console',...) 正常且回读一致",
    res_console["verified"] is True
    and res_console["previous"] == "计"
    and read_setting("alpha", ("modules", "demo", "label"), plugin_id="hermes-console") == "通用设置页",
)
check(
    "旧的 save_setting 行为未受影响（仍写 hermes-console）",
    save_setting("alpha", ("modules", "demo", "retry", "max"), 7) is None
    and read_setting("alpha", ("modules", "demo", "retry", "max")) == 7,
)

# ---------------------------------------------------------------------------
print("[5] 非法输入：ValueError + 一个字节都不写")
bad_cases = [
    ("plugin_id=''", lambda: save_plugin_setting_for("alpha", "", ("a",), 1)),
    ("plugin_id='../evil'", lambda: save_plugin_setting_for("alpha", "../evil", ("a",), 1)),
    ("plugin_id='web/../xai'", lambda: save_plugin_setting_for("alpha", "web/../xai", ("a",), 1)),
    ("plugin_id='Alpha'（大写）", lambda: save_plugin_setting_for("alpha", "Alpha", ("a",), 1)),
    ("agent_id='../../etc'", lambda: save_plugin_setting_for("../../etc", "browserpaw", ("a",), 1)),
]
tree_before = tree_snapshot(fake_home)
illegal_ok = True
for label, call in bad_cases:
    try:
        call()
    except ValueError as exc:
        print("          %-38s → ValueError: %s" % (label, exc))
        continue
    except Exception as exc:  # noqa: BLE001 - 只接受 ValueError
        print("          %-38s → 错误类型不对: %r" % (label, exc))
        illegal_ok = False
        continue
    print("          %-38s → 没抛异常 <<< 不一致" % label)
    illegal_ok = False
check("非法 plugin_id / agent_id 一律抛 ValueError", illegal_ok)
tree_after = tree_snapshot(fake_home)
diff = {k: (tree_before.get(k), tree_after.get(k)) for k in set(tree_before) | set(tree_after) if tree_before.get(k) != tree_after.get(k)}
check(
    "★非法输入没有产生任何文件写入（沙盒整棵树 sha256 完全一致）",
    not diff,
    dumps(diff) if diff else "全部一致，%d 个文件" % len(tree_after),
)
try:
    normalize_plugin_id("web/xai")
    nested_ok = True
except ValueError:
    nested_ok = False
check("normalize_plugin_id('web/xai') 合法（嵌套插件 id 放行）", nested_ok)

# ---------------------------------------------------------------------------
print("[6] 嵌套插件 id：web/xai")
res_xai = save_plugin_setting_for("alpha", "web/xai", ("endpoint",), "https://new.example")
print("        返回 :", dumps(res_xai))
check(
    "web/xai 写入返回 verified / previous 保留旧值",
    res_xai["verified"] is True and res_xai["previous"] == "https://old.example",
    dumps(res_xai),
)
check(
    "真的落在 plugins.entries['web/xai'].settings（文件里的键就是带斜杠的 id）",
    settings_of(ALPHA_CFG, "web/xai").get("endpoint") == "https://new.example",
    dumps(entries_of(ALPHA_CFG)),
)
check(
    "web/xai 回读一致（model 保留 grok-legacy）",
    read_plugin_settings("alpha", "web/xai") == {"endpoint": "https://new.example", "model": "grok-legacy"},
    dumps(read_plugin_settings("alpha", "web/xai")),
)
check(
    "'web/xai' 出现在 list_plugin_ids('alpha') 里",
    "web/xai" in list_plugin_ids("alpha"),
    str(list_plugin_ids("alpha")),
)

# ---------------------------------------------------------------------------
print("[7] 脱敏 mask_sensitive")
sample = {"api_key": "sk-xxx", "token": "t", "normal": "v"}
masked = mask_sensitive(sample)
print("        输入 :", dumps(sample))
print("        输出 :", dumps(masked))
check(
    "api_key / token → '***'，normal 保留，masked_keys 正确",
    masked.get("api_key") == "***"
    and masked.get("token") == "***"
    and masked.get("normal") == "v"
    and masked.get("masked_keys") == ["api_key", "token"],
    dumps(masked),
)
check("脱敏不修改入参（原 dict 未被污染）", sample == {"api_key": "sk-xxx", "token": "t", "normal": "v"}, dumps(sample))
masked_nested = mask_sensitive({"web": {"password": "p", "endpoint": "e"}, "noop": 1})
check(
    "嵌套 mapping 递归脱敏 + 点分路径",
    masked_nested.get("web", {}).get("password") == "***"
    and masked_nested.get("web", {}).get("endpoint") == "e"
    and masked_nested.get("masked_keys") == ["web.password"],
    dumps(masked_nested),
)
for name in ("credential_id", "secret", "access_key", "PASSWORD"):
    check("键名含 %r 也脱敏（大小写不敏感）" % name, mask_sensitive({name: "x"}).get(name) == "***")

# ---------------------------------------------------------------------------
print("[9] 真实 home 只读抽查：list_plugin_ids('xiaomo') 与 profiles/xiaomo/config.yaml 对照")
if not XIAOMO_CFG.is_file():
    check("profiles/xiaomo/config.yaml 存在", False, str(XIAOMO_CFG))
else:
    got_ids = list_plugin_ids("xiaomo", home=REAL_HOME)
    print("        文件 entries       (%2d):" % len(list(xiaomo_entries)), list(xiaomo_entries))
    print("        文件 enabled  (%2d):" % len(xiaomo_enabled), xiaomo_enabled)
    print("        文件 disabled (%2d):" % len(xiaomo_disabled), xiaomo_disabled)
    print("        list_plugin_ids('xiaomo') (%2d):" % len(got_ids), got_ids)
    check(
        "list_plugin_ids('xiaomo') == entries ∪ enabled ∪ disabled（逐项对得上）",
        set(got_ids) == xiaomo_expect and len(got_ids) == len(xiaomo_expect),
        "缺=%s 多=%s" % (sorted(xiaomo_expect - set(got_ids)), sorted(set(got_ids) - xiaomo_expect)),
    )
    check(
        "所有 id 顺序稳定且无重复",
        len(got_ids) == len(set(got_ids)),
        str(got_ids),
    )
    # 真实 home 上的写入路径一律不碰：只读调用必须不新建/不改任何文件
    check(
        "只读抽查没有改动真实 xiaomo 配置（sha256 与前置快照一致）",
        prod_before.get(str(XIAOMO_CFG)) == sha256_of(XIAOMO_CFG),
        "%s → %s" % (prod_before.get(str(XIAOMO_CFG)), sha256_of(XIAOMO_CFG)),
    )

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
check("写入未生效的异常类型可导入（PluginSettingsWriteError）",
      issubclass(PluginSettingsWriteError, RuntimeError))

# ---------------------------------------------------------------------------
shutil.rmtree(fake_home, ignore_errors=True)
print("        沙盒已清理 :", fake_home)

print()
if failures:
    print("失败 %d 条：" % len(failures))
    for item in failures:
        print("  - " + item)
    print("PLUGIN-SETTINGS-WRITE-PROBE-FAILED")
    raise SystemExit(1)

print("全部检查通过（任意插件写入隔离 + 不串味 + 非法输入零写入 + 嵌套 id + 脱敏 + %d 个真实配置文件零污染）"
      % len(prod_after))
print("PLUGIN-SETTINGS-WRITE-PROBE-OK")
raise SystemExit(0)
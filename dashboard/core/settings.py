# -*- coding: utf-8 -*-
"""hermes-console · 配置分层合并 + 安全写入。

## 分层（低 → 高）

1. ``module_default`` —— 模块自带默认值（调用方传入的 ``defaults`` dict，不进磁盘）
2. ``global``        —— 根 ``config.yaml`` 的 ``plugins.entries["hermes-console"].settings``
3. ``agent``         —— 该 agent 自己 ``config.yaml`` 的同路径

每个返回值都**带来源**（``{"value": ..., "source": ...}``），界面要显示"这个值是哪来的"，
不许让人猜。

## 写入只走官方写入器

* ``save_setting`` → ``hermes_cli.plugins_state.save_plugin_setting(plugin_id, segments, value)``
  —— 官方原子写入器（跨进程锁 + fail-closed 原始读 + managed/管理员托管键拒绝）。
* profile 作用域 → ``hermes_cli.web_server_profiles._config_profile_scope(profile)``
  —— 官方请求内作用域（含密钥隔离）。``agent_id == "default"`` 传 ``None``（不切作用域）。

**硬规则：不手写 YAML、不手拼 dict 再 dump。** 本文件里没有任何一处直接写文件。
"""

from __future__ import annotations

import copy
import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .scope import (
    DEFAULT_AGENT_ID,
    agent_home,
    normalize_agent_id,
    read_agent_config,
)

logger = logging.getLogger(__name__)

#: 本插件的 id（= ``plugins.entries`` 下的键 = manifest 的 name）。
PLUGIN_ID = "hermes-console"

#: 来源标注（界面直接显示，别改字面量）。
SOURCE_MODULE_DEFAULT = "module_default"
SOURCE_GLOBAL = "global"
SOURCE_AGENT = "agent"

#: ``settings.modules`` 子树：按模块分组的配置都在这里。
MODULES_KEY = "modules"

#: 合法 plugin_id 文法：与官方 ``plugin_events._PLUGIN_ID_RE``（``^[a-z0-9_-]{1,64}$``）同一批字符，
#: 另允许 ``/`` 分隔的嵌套插件名（``web/xai`` ⇒ ``plugins.entries["web/xai"]``）。
PLUGIN_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}(/[a-z0-9_-]{1,64})*$")

#: :func:`mask_sensitive` 认定的敏感键名片段（小写比较，命中任一即脱敏）。
SENSITIVE_KEY_MARKERS = ("token", "key", "secret", "password", "credential")


class PluginSettingsWriteError(RuntimeError):
    """写后回读校验失败：文件内容与预期不符（"写入未生效"）。"""


def normalize_plugin_id(raw: Optional[str]) -> str:
    """校验并归一化 ``plugin_id``：小写字母/数字/下划线/短横线，可用 ``/`` 表示嵌套插件。

    非法（空串、``..``、首尾或重复 ``/``、``\\``、大写、绝对路径）一律 ``ValueError``。
    **纯文法检查，不碰任何文件**——写入路径必须在读/写配置之前调用它，这样非法输入
    连一次配置读取都到不了，更别说落盘。
    """
    text = "" if raw is None else str(raw).strip()
    if not PLUGIN_ID_RE.match(text):
        raise ValueError(
            "非法 plugin_id %r：只能是 1-64 位小写字母/数字/下划线/短横线，"
            "可用 '/' 分隔嵌套插件（如 'web/xai'）" % (text,)
        )
    return text


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def _plugin_settings_entry(config: object, plugin_id: str) -> Optional[Mapping[str, Any]]:
    """``plugins.entries.<plugin_id>`` —— 优先用官方 helper，没有就等价实现。"""
    try:
        from hermes_cli.plugins_state import _plugin_settings_entry as official_entry

        return official_entry(config, plugin_id)
    except Exception:  # noqa: BLE001 - 无 hermes_cli 时的等价实现
        node: Any = config
        for key in ("plugins", "entries", plugin_id):
            if not isinstance(node, Mapping) or key not in node:
                return None
            node = node[key]
        return node if isinstance(node, Mapping) else None


def read_plugin_settings(
    agent_id: str,
    plugin_id: str = PLUGIN_ID,
    *,
    home: Optional[Path] = None,
) -> Dict[str, Any]:
    """某 agent 某插件的 **原始** settings dict（``{}`` 表示没配过）。

    * 只读该 agent 自己的 ``config.yaml``（**不合并**全局层——合并是
      :func:`effective_settings` 的事，这里要的是"文件里到底写了什么"）
    * 文件不存在 / 没有该插件条目 / ``settings`` 不是 mapping → ``{}``，不抛异常
    * 返回值是深拷贝，调用方随便改不会污染缓存
    """
    agent = normalize_agent_id(agent_id, home=home)
    config = read_agent_config(agent, home=home)
    entry = _plugin_settings_entry(config, plugin_id)
    if not isinstance(entry, Mapping):
        return {}
    settings = entry.get("settings")
    if not isinstance(settings, Mapping):
        return {}
    return copy.deepcopy(dict(settings))


def _nested(root: Any, segments: Sequence[str]) -> Any:
    node = root
    for segment in segments:
        if not isinstance(node, Mapping) or segment not in node:
            return None
        node = node[segment]
    return node


def _flatten(tree: Any, prefix: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """把嵌套 mapping 拍平成 ``{"a.b.c": value}``。

    * mapping 递归展开（空 mapping 不产出键）
    * 其他类型（标量、list、``None``）算叶子
    """
    flat: Dict[str, Any] = {}
    if not isinstance(tree, Mapping):
        return flat
    for key, value in tree.items():
        path = prefix + (str(key),)
        if isinstance(value, Mapping):
            if not value:
                continue
            flat.update(_flatten(value, path))
        else:
            flat[".".join(path)] = value
    return flat


def effective_settings(
    agent_id: str,
    module_id: Optional[str] = None,
    defaults: Optional[Mapping[str, Any]] = None,
    *,
    home: Optional[Path] = None,
) -> Dict[str, Dict[str, Any]]:
    """某 agent 看到的**有效值**，每个值都带来源。

    返回 ``{"<点分键>": {"value": <值>, "source": "module_default"|"global"|"agent"}}``。

    * ``module_id`` 非空 ⇒ 只在 ``settings.modules.<module_id>`` 子树里找，
      返回的键相对该子树（``"enabled"``、``"retry.max"``）；``defaults`` 也被当作该子树的默认值。
    * ``module_id`` 为空 ⇒ 该插件全部 settings 的扁平视图（键含 ``modules.<id>.xxx`` 前缀），
      ``defaults`` 视作插件级默认值。

    合并规则：

    * 三层依次覆盖，高优先级赢
    * 值为 ``None``（``reset_setting`` 写的"置空"）**视为没有这一层**，回落到上一层
    * ``agent_id == "default"`` 时全局层与该 agent 层是同一个文件，只算 ``global``
      （否则每个值都会被标成 ``agent``，界面会误报"这个 agent 自己改过"）
    """
    agent = normalize_agent_id(agent_id, home=home)
    base_layer = _flatten(defaults or {})
    global_settings = read_plugin_settings(DEFAULT_AGENT_ID, home=home)
    agent_settings = {} if agent == DEFAULT_AGENT_ID else read_plugin_settings(agent, home=home)

    if module_id:
        module_path = (MODULES_KEY, str(module_id))
        global_layer = _flatten(_nested(global_settings, module_path))
        agent_layer = _flatten(_nested(agent_settings, module_path))
    else:
        global_layer = _flatten(global_settings)
        agent_layer = _flatten(agent_settings)

    merged: Dict[str, Dict[str, Any]] = {}
    for source, layer in (
        (SOURCE_MODULE_DEFAULT, base_layer),
        (SOURCE_GLOBAL, global_layer),
        (SOURCE_AGENT, agent_layer),
    ):
        for key, value in layer.items():
            if value is None:  # 置空 == 不覆盖，回落到上一层
                continue
            merged[key] = {"value": value, "source": source}
    return merged


def effective_value(
    agent_id: str,
    key: str,
    module_id: Optional[str] = None,
    defaults: Optional[Mapping[str, Any]] = None,
    *,
    home: Optional[Path] = None,
    fallback: Any = None,
) -> Any:
    """只要值不要来源的便捷封装（内部用 :func:`effective_settings`，口径一致）。"""
    entry = effective_settings(agent_id, module_id, defaults, home=home).get(key)
    return fallback if entry is None else entry["value"]


def read_setting(
    agent_id: str,
    segments: Sequence[str],
    *,
    plugin_id: str = PLUGIN_ID,
    home: Optional[Path] = None,
    default: Any = None,
) -> Any:
    """读该 agent **自己文件里**的某一项（不回落全局；没写过返回 ``default``）。"""
    agent = normalize_agent_id(agent_id, home=home)
    node: Any = read_plugin_settings(agent, plugin_id, home=home)
    for segment in segments:
        if not isinstance(node, Mapping) or segment not in node:
            return default
        node = node[segment]
    return node


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------
def _normalize_segments(segments: Iterable[str]) -> Tuple[str, ...]:
    """校验并归一化相对路径（``("modules", "demo", "enabled")``）。

    走官方 ``hermes_cli.plugins_state._plugin_relative_segments``：它拒绝空段、``/``、``\\``、
    遍历、以及 ``model``/``plugins``/``security``/``settings`` 这些保留根。
    非法 → ``ValueError``（消息与官方一致）。
    """
    if isinstance(segments, str):
        raise TypeError("segments 必须是字符串序列（tuple/list），不是点分字符串；" "例如 ('modules', 'demo', 'enabled')")
    raw = tuple(str(segment) for segment in segments)
    if not raw:
        raise ValueError("segments 不能为空：至少要给一个相对键，例如 ('modules', 'demo', 'enabled')")
    try:
        from hermes_cli.plugins_state import _plugin_relative_segments as official_validate

        return official_validate(".".join(raw))
    except ImportError:  # pragma: no cover - 无 hermes_cli 的纯路径场景
        if any((not segment) or ("/" in segment) or ("\\" in segment) for segment in raw):
            raise ValueError("非法的配置键路径：%r" % (raw,)) from None
        return raw


def _require_writable_agent(agent: str, home: Optional[Path] = None) -> None:
    """写入前确认该 agent 的 home 目录存在。

    官方 ``_config_profile_scope`` 对不存在的 profile 会抛 ``HTTPException(404)``，
    对 M1b（后端路由）来说那是 500 味的错误；这里提前换成带合法 id 列表的 ``ValueError``。
    """
    if agent == DEFAULT_AGENT_ID:
        return
    directory = agent_home(agent, home=home)
    try:
        if directory.is_dir():
            return
    except OSError:
        pass
    from .scope import list_agent_ids

    raise ValueError(
        "agent %r 不存在（%s 不是目录）。当前合法 id：%s"
        % (agent, directory, ", ".join(list_agent_ids(home=home)) or "<none>")
    )


def save_setting(
    agent_id: str,
    segments: Sequence[str],
    value: Any,
    *,
    home: Optional[Path] = None,
) -> None:
    """把一项配置写进**该 agent 自己的** ``config.yaml``（官方原子写入器）。

    ``segments`` 是相对 ``plugins.entries.hermes-console.settings`` 的路径，
    例如 ``("modules", "demo", "enabled")``、``("modules", "demo", "retry", "max")``。

    写入位置：``agent_id == "default"`` ⇒ 根 ``config.yaml``（作用域 ``None``）；
    其余 ⇒ ``<home>/profiles/<agent>/config.yaml``（作用域 = 该 profile）。

    ``home`` 只用于**存在性预检**（把官方 404 换成 ``ValueError``）。真正的落盘位置由官方
    ``_config_profile_scope`` + ``get_config_path()`` 决定，锚定在**进程** home 上——
    这正是 dashboard 后端每个请求的行为（进程 home = 它服务的那个部署）。
    要让写入落到别的 home，请在进程启动前设好 ``HERMES_HOME``（探针就是这么做的）。
    """
    agent = normalize_agent_id(agent_id, home=home)
    normalized = _normalize_segments(segments)
    _require_writable_agent(agent, home=home)

    from hermes_cli.plugins_state import save_plugin_setting  # 官方写入器
    from hermes_cli.web_server_profiles import _config_profile_scope  # 官方 profile 作用域

    scope = None if agent == DEFAULT_AGENT_ID else agent
    with _config_profile_scope(scope):
        save_plugin_setting(PLUGIN_ID, normalized, value)


def reset_setting(
    agent_id: str,
    segments: Sequence[str],
    *,
    home: Optional[Path] = None,
) -> None:
    """把该 agent 的这一项覆盖**置空**（写 ``null``），从而回落到全局层。

    **这是"置空"，不是"删除"。** 官方写入器 ``save_plugin_setting`` 只有"写某个值"一种语义，
    没有删键 API；硬删就得自己读 YAML 改 dict 再 dump，那正是本卡明令禁止的。
    所以这里写 ``None``：

    * 磁盘上会留下 ``modules.demo.label: null``（raw 读能看到这个显式 null）
    * :func:`effective_settings` 把 ``None`` 当作"没有这一层"，因此有效值回落到
      ``global`` / ``module_default``，功能上等价于"删掉覆盖"
    * 代价：文件里会残留一行 ``null``。等官方出现删除语义（或 M1b 的 registry 统一
      重写模块配置块）时再收敛。
    """
    save_setting(agent_id, segments, None, home=home)


def save_plugin_setting_for(
    agent_id: str,
    plugin_id: str,
    segments: Sequence[str],
    value: Any,
    *,
    home: Optional[Path] = None,
) -> Dict[str, Any]:
    """把 ``value`` 写进该 agent 的**任意**插件的 settings（官方原子写入器 + 写后回读校验）。

    与 :func:`save_setting` 的唯一区别是 ``plugin_id`` 不再写死为 ``PLUGIN_ID``：

    * 落盘位置 ``plugins.entries.<plugin_id>.settings.<segments>``（嵌套插件 id 如
      ``web/xai`` 就是 ``plugins.entries["web/xai"]``）
    * profile 作用域口径与 :func:`save_setting` 完全相同（``default`` ⇒ ``None``，其余 ⇒ 该 profile）
    * 返回 ``{"agent_id", "plugin_id", "segments", "previous", "value", "verified"}``

    失败语义：

    * 非法 ``agent_id`` / ``plugin_id`` / ``segments`` → ``ValueError``，**在摸文件之前**抛出
    * 该 agent 目录不存在 → ``ValueError``（把官方 404 换成带合法 id 列表的错误）
    * 写完回读，目标路径缺失或值不等 → :class:`PluginSettingsWriteError`（"写入未生效"）——
      退出码/自述都不算数，磁盘上真的变了才算成功
    """
    agent = normalize_agent_id(agent_id, home=home)
    plugin = normalize_plugin_id(plugin_id)
    normalized = _normalize_segments(segments)
    _require_writable_agent(agent, home=home)

    previous = read_setting(agent, normalized, plugin_id=plugin, home=home)

    from hermes_cli.plugins_state import save_plugin_setting  # 官方写入器
    from hermes_cli.web_server_profiles import _config_profile_scope  # 官方 profile 作用域

    scope = None if agent == DEFAULT_AGENT_ID else agent
    with _config_profile_scope(scope):
        save_plugin_setting(plugin, normalized, value)

    # 写后回读：走官方读路径（read_plugin_settings），断言目标键**存在**且值相等。
    dotted = ".".join(normalized)
    node: Any = read_plugin_settings(agent, plugin, home=home)
    for segment in normalized:
        if not isinstance(node, Mapping) or segment not in node:
            raise PluginSettingsWriteError(
                "写入未生效：plugins.entries[%r].settings.%s 回读时缺失（agent=%s）"
                % (plugin, dotted, agent)
            )
        node = node[segment]
    if node != value:
        raise PluginSettingsWriteError(
            "写入未生效：plugins.entries[%r].settings.%s 回读为 %r（%s），期望 %r（%s）"
            % (plugin, dotted, node, type(node).__name__, value, type(value).__name__)
        )
    return {
        "agent_id": agent,
        "plugin_id": plugin,
        "segments": normalized,
        "previous": previous,
        "value": value,
        "verified": True,
    }


def list_plugin_ids(
    agent_id: str,
    *,
    home: Optional[Path] = None,
    config: Optional[Mapping[str, Any]] = None,
) -> List[str]:
    """该 agent 配置里出现过的**全部**插件 id：``plugins.entries`` ∪ ``enabled`` ∪ ``disabled``。

    只读；只保留符合 :data:`PLUGIN_ID_RE` 文法的项（配置里可能有垃圾/历史残留），
    去重后按"entries 键序 → enabled → disabled"稳定排序。用于 M4b 设置界面列插件清单。

    ``config`` 是已经读好的该 agent 配置：批量路径（``GET /agents/<id>/plugins``）传它，
    避免同一个请求里为同一份 ``config.yaml`` 再解析一次（整棵树 YAML 解析 ~46ms/次）。
    """
    agent = normalize_agent_id(agent_id, home=home)
    plugins = (read_agent_config(agent, home=home) if config is None else config).get("plugins")
    if not isinstance(plugins, Mapping):
        return []
    candidates: List[Any] = []
    entries = plugins.get("entries")
    if isinstance(entries, Mapping):
        candidates.extend(entries.keys())
    for key in ("enabled", "disabled"):
        values = plugins.get(key)
        if isinstance(values, (list, tuple)):
            candidates.extend(values)
    seen: Dict[str, None] = {}
    for candidate in candidates:
        text = "" if candidate is None else str(candidate).strip()
        if text and PLUGIN_ID_RE.match(text):
            seen.setdefault(text, None)
    return list(seen)


def mask_sensitive(settings: Mapping[str, Any]) -> Dict[str, Any]:
    """界面展示用脱敏：键名含 :data:`SENSITIVE_KEY_MARKERS` 的值换成 ``"***"``。

    递归展开嵌套 mapping，返回体里附 ``masked_keys``（命中键的点分路径，已排序）。

    **只用于显示。** 回写配置一律用原始 settings 走 :func:`save_plugin_setting_for`——
    把 ``"***"`` 写回去等于拿掩码覆盖真密钥。

    天花板：顶层若有名为 ``masked_keys`` 的键会被元数据覆盖；list 里的 dict 不递归。
    """
    masked_keys: List[str] = []

    def walk(node: Any, prefix: str = "") -> Any:
        if not isinstance(node, Mapping):
            return copy.deepcopy(node)
        out: Dict[str, Any] = {}
        for key, item in node.items():
            name = str(key)
            path = "%s.%s" % (prefix, name) if prefix else name
            if isinstance(item, Mapping):
                out[key] = walk(item, path)
            elif any(marker in name.lower() for marker in SENSITIVE_KEY_MARKERS):
                out[key] = "***"
                masked_keys.append(path)
            else:
                out[key] = copy.deepcopy(item)
        return out

    result = walk(settings)
    result["masked_keys"] = sorted(masked_keys)
    return result


__all__ = [
    "PLUGIN_ID",
    "PLUGIN_ID_RE",
    "SENSITIVE_KEY_MARKERS",
    "SOURCE_AGENT",
    "SOURCE_GLOBAL",
    "SOURCE_MODULE_DEFAULT",
    "PluginSettingsWriteError",
    "effective_settings",
    "effective_value",
    "list_plugin_ids",
    "mask_sensitive",
    "normalize_plugin_id",
    "read_plugin_settings",
    "read_setting",
    "reset_setting",
    "save_plugin_setting_for",
    "save_setting",
]

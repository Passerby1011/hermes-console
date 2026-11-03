# -*- coding: utf-8 -*-
"""hermes-console · 插件级开关（per-agent「这个插件对我生效吗」）。

## 只走 ``plugins.disabled`` 黑名单，绝不重写 ``plugins.enabled`` 整表

依据（都在官方源码里，不是推测）：

* ``hermes_cli/plugins_cmd.py:474`` 原注释：``plugins.disabled`` is an explicit deny-list
  that **wins over** the ``plugins.enabled`` allow-list。
  ``hermes_cli/plugins.py::has_enabled_portable_mcp`` 的判定也是同一顺序：
  ``names & disabled`` ⇒ 跳过；``not names & enabled`` ⇒ 跳过。
* ``plugins.enabled`` 是「允许清单」，**覆盖写错会一次停掉所有面板**
  （agents 的 enabled 列表里有 20~30 项，整表重写等于替用户决定装什么）。
* 所以「关闭某个插件」= 往该 agent 的 ``plugins.disabled`` 追加一条；
  「开启某个插件」= 从 ``plugins.disabled`` 删掉一条。**enabled 一个字节都不动。**

## 写入只走官方写入器，且只提交 ``plugins.disabled`` 这一块

照抄官方 ``hermes_cli.plugins_state.save_plugin_setting`` 的骨架（先例：
``hermes_cli/plugins_cmd.py::_write_config_value``）：在官方 profile 作用域 + 官方跨进程锁内
``save_config({"plugins": {"disabled": [...]}}, merge_existing=True)``。

**payload 里根本不出现 ``plugins.enabled``**：磁盘上其它 section 由官方 merge 从原文件带过来，
所以「不碰允许清单」是结构保证，不是靠自觉。**本文件没有任何一处直接写 YAML。**

作用域：``agent_id == "default"`` ⇒ ``_config_profile_scope(None)``；
其余 ⇒ ``_config_profile_scope(<agent>)``，即 ``<home>/profiles/<agent>/config.yaml``。

## 写后回读

每次写盘后重新 ``read_agent_config``：

1. 目标键必须真的变了（关闭 ⇒ disabled 含它；开启 ⇒ disabled 里没有它），不符抛
   :class:`PluginSwitchWriteError`；
2. ``plugins.enabled`` 的**原始值**必须与写前完全相同（值级 ``==``），不符同样抛异常并把
   前后两份值打进消息里 —— 这条是「严禁碰 enabled 整表」的运行时守卫，不是注释。

## 「开启」的另一半：走官方准入权威（M2c）

M2 的「开启」只从黑名单里摘一条。若插件**既不在** ``disabled`` **也不在** ``enabled``，摘完仍
未启用 —— 这种情况（下面叫三态里的 ``unavailable``）交给 Hermes 唯一准入权威
:func:`hermes_cli.plugins_cmd._admit_and_save_plugin_sets`：把候选集（``enabled ∪ {plugin}``、
``disabled - {plugin}``）交给它，由官方在 PM 事务里解析、校验、并在同一事务内提交配置。

**本模块依然一个字节都不自己拼 YAML**：准入通道内部用官方 ``pm`` 发布器做「字节级 CAS 重写」，
失败/拒绝时「previous config bytes AND previous environment stay exactly in place」。

准入通道的三条实测结论（``scripts/probe_plugin_switch_admission.py`` 里也有断言）：

1. **不会 relaunch**：调用链 ``_admit_and_save_plugin_sets → admit_plugin_set_change →
   pm.client.sync_venv → pm.worker``（子进程）里没有 ``stop_for_relaunch`` / ``os.exec`` /
   ``sys.exit`` / ``raise SystemExit``。唯一带 ``stop_for_relaunch`` 的 ``_save_enabled_set``
   是「冻结的旧更新器入口」，本路径不经过它。父进程调用后**继续正常返回**。
2. **不改本进程的 ``os.environ``**：子进程拿到的是 ``runtime_environment()`` 造出来的**副本**
   （``subprocess.Popen(..., env=environment)``）。实测调用前后 ``os.environ`` 逐键相同。
   唯一的环境级副作用是它按需同步**共享依赖环境**（插件声明了包时才会重建 venv，官方 CLI 同款行为）。
3. **不需要 TTY**：worker 的 stdin/stdout 都是 ``subprocess.PIPE``；``console=None`` 只是跳过
   Rich 打印，不会等待输入。实测在无终端子进程里跑通。

代价（调用方必须知道）：这是一次**可能阻塞**的官方安装事务 —— 依赖集不变时是「配置提交」
（实测 <1s）；需要重建依赖环境时是分钟级并会联网（冷 home 实测 ~42s 起）。所以后端路由
**不要**在事件循环里同步调用它。

## 不猜、不代劳

「开启」只会把插件从黑名单里放出来；它**本来就不在** ``plugins.enabled`` 里时，实际仍未启用。
M2 时代这种情况只返回 ``warning``（``strict=False`` 时保留这条诚实退路）；M2c 起默认
（``strict=True``）改走官方准入通道把它真正启用，拒绝时**原样透出拒绝**、绝不吞掉。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .scope import (
    DEFAULT_AGENT_ID,
    normalize_agent_id,
    read_agent_config,
)
from .settings import _require_writable_agent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量（字面量与官方 key 一致，别改）
# ---------------------------------------------------------------------------
#: ``plugins.disabled`` —— 黑名单，**优先于** 允许清单。
PLUGIN_DENY_KEY = "disabled"
#: ``plugins.enabled`` —— 允许清单，本模块只读不写。
PLUGIN_ALLOW_KEY = "enabled"
#: 两个列表的父节点。
PLUGINS_SECTION = "plugins"

#: ``plugin_state`` 的 source 取值（界面直接显示，别改字面量）。
SOURCE_DISABLED_LIST = "disabled_list"
SOURCE_ENABLED_LIST = "enabled_list"
SOURCE_ABSENT = "absent"

#: 「开启」但 enabled 里也没有它时返回的告警文案（卡片指定原文，别改写）。
WARN_NOT_IN_ALLOW_LIST = (
    "已从黑名单移除，但该 agent 的 plugins.enabled 里也没有它，实际仍未启用；"
    "需要另加进 enabled（本卡不代劳）"
)

#: 三态（M4b 按这个渲染；``unavailable`` **绝不许**在界面上显示成「已开启」）。
STATE_ENABLED = "enabled"
STATE_DISABLED = "disabled"
STATE_UNAVAILABLE = "unavailable"

#: ``set_plugin_enabled`` 结果里的 ``via``：走黑名单，还是走官方准入通道。
VIA_DENY_LIST = "deny_list"
VIA_ADMISSION = "admission"

#: 准入通道被拒绝时的 ``code``（拒绝原样透出，不许改写成 ok）。
CODE_ADMISSION_REFUSED = "admission_refused"
CODE_DEPENDENCY_CONFLICT = "dependency_conflict"

#: 准入通道压根用不了（import 失败 / profile 作用域没生效）时的 ``code``。
CODE_ADMISSION_UNAVAILABLE = "admission_unavailable"

#: 与官方 ``plugins_state._PLUGIN_SETTING_SEGMENT_RE`` 同一条文法：字母/数字开头，
#: 允许字母/数字/下划线/短横线，≤128。插件 id 可能是带命名空间的两段（``web/firecrawl``），
#: 所以逐段校验后允许单层 ``/`` 连接。
_PLUGIN_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

#: 整个 plugin id 的长度上限（段长 × 常见 2 段，再留余量）。
_MAX_PLUGIN_ID_LEN = 256

#: 允许的最大段数（``cron_providers/chronos`` 是 2 段；留 4 段余量但拒绝无限嵌套）。
_MAX_PLUGIN_SEGMENTS = 4


class PluginSwitchWriteError(RuntimeError):
    """写盘后回读与预期不符。

    刻意不继承 ``ValueError``：``ValueError`` 在本模块里专指「入参非法，还没摸文件」，
    而本异常表示「文件已经被写过但结果不对」，调用方（后端路由）要分别映射成
    400 和 500。
    """


class PluginSwitchRefused(RuntimeError):
    """官方准入权威**拒绝**了这个候选集（配置与环境一个字节都没动）。

    ``code`` 取 :data:`CODE_ADMISSION_REFUSED` / :data:`CODE_DEPENDENCY_CONFLICT`；
    ``set_plugin_enabled`` 会把它翻成 ``{"ok": False, "refused": True, ...}`` 原样透出。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PluginSwitchAdmissionError(RuntimeError):
    """官方准入通道**用不了**（import 失败 / profile 作用域没生效）。

    与 :class:`PluginSwitchRefused` 严格区分：refused = 官方审过了、拒绝；unavailable =
    根本没问成。后者在 ``strict=False`` 时退化成 M2 的「只摘黑名单 + warning」。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# 入参校验（**在摸文件之前就拦住**）
# ---------------------------------------------------------------------------
def normalize_plugin_id(raw: Any) -> str:
    """校验 plugin id 文法，返回原样字符串（不改大小写、不 trim——不做静默修复）。

    合法：``weather``、``web/firecrawl``、``image_gen/openrouter``、``cron_providers/chronos``
    （与真实配置里的写法一致）。

    非法 → ``ValueError``：空串、``.`` / ``..`` 段、含 ``\\``、``/`` 打头/结尾/连续、
    出现空格或其它符号、段超 128 字符、整体超 256 字符、段数 > 4。

    只校验**文法**，不校验插件是否安装——被禁用/已卸载的插件名照样可能留在列表里。
    """
    if raw is None or isinstance(raw, bool) or not isinstance(raw, str):
        raise ValueError("非法 plugin id %r：必须是字符串" % (raw,))
    text = raw
    if not text:
        raise ValueError("非法 plugin id ''：不能为空")
    if text != text.strip():
        raise ValueError("非法 plugin id %r：首尾不能有空白字符" % (text,))
    if len(text) > _MAX_PLUGIN_ID_LEN:
        raise ValueError("非法 plugin id：长度 %d 超过上限 %d" % (len(text), _MAX_PLUGIN_ID_LEN))
    if "\\" in text:
        raise ValueError("非法 plugin id %r：不允许反斜杠" % (text,))
    segments = text.split("/")
    if len(segments) > _MAX_PLUGIN_SEGMENTS:
        raise ValueError(
            "非法 plugin id %r：最多 %d 段，实际 %d 段" % (text, _MAX_PLUGIN_SEGMENTS, len(segments))
        )
    for segment in segments:
        if segment in (".", ".."):
            raise ValueError("非法 plugin id %r：不允许 '.' / '..' 路径段（防目录穿越）" % (text,))
        if not _PLUGIN_SEGMENT_RE.match(segment):
            raise ValueError(
                "非法 plugin id %r：每段只能是 1-128 位字母/数字/下划线/短横线，且以字母或数字开头"
                "（命名空间形式形如 'web/firecrawl'）" % (text,)
            )
    return text


def _require_bool(enabled: Any) -> bool:
    """``enabled`` 参数必须是真 bool（``"false"`` / ``0`` 一律拒绝，别让字符串当真值）。"""
    if not isinstance(enabled, bool):
        raise ValueError("enabled 必须是布尔值（True/False），收到 %r" % (enabled,))
    return enabled


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def _string_list(value: Any) -> List[str]:
    """从配置里取「字符串列表」，非 list / 含非字符串项时只认字符串项。"""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _raw_list(value: Any) -> List[Any]:
    """原始列表（保留非字符串项的原有位置，写回时不会因为过滤而丢垃圾数据）。"""
    return list(value) if isinstance(value, list) else []


def _plugin_section(config: Mapping[str, Any]) -> Mapping[str, Any]:
    section = config.get(PLUGINS_SECTION) if isinstance(config, Mapping) else None
    return section if isinstance(section, Mapping) else {}


def plugin_lists(config: Mapping[str, Any]) -> Tuple[List[str], List[str]]:
    """``(enabled, disabled)`` 两份字符串列表（缺项 ⇒ ``[]``，非列表值按缺失处理）。"""
    section = _plugin_section(config)
    return (
        _string_list(section.get(PLUGIN_ALLOW_KEY)),
        _string_list(section.get(PLUGIN_DENY_KEY)),
    )


def _state_from_config(plugin: str, config: Mapping[str, Any]) -> Dict[str, Any]:
    """核心判定：**黑名单优先**。

    1. 在 ``disabled`` 里 ⇒ ``enabled=False``（``source="disabled_list"``）——即使它同时在
       ``enabled`` 里也一样（这正是官方 loader 的顺序）。
    2. 否则在 ``enabled`` 里 ⇒ ``enabled=True``（``source="enabled_list"``）。
    3. 都没有 ⇒ ``enabled=False``（``source="absent"``）。
    """
    enabled, disabled = plugin_lists(config)
    in_disabled = plugin in disabled
    in_enabled = plugin in enabled
    if in_disabled:
        state, source = False, SOURCE_DISABLED_LIST
    elif in_enabled:
        state, source = True, SOURCE_ENABLED_LIST
    else:
        state, source = False, SOURCE_ABSENT
    return {
        "plugin": plugin,
        "enabled": state,
        "in_disabled": in_disabled,
        "in_enabled": in_enabled,
        "source": source,
    }


def _state_name(plugin: str, config: Mapping[str, Any]) -> str:
    """三态：``disabled`` / ``enabled`` / ``unavailable``（与 :func:`_state_from_config` 同序）。

    ``unavailable`` = 两份名单里都没有它。官方 loader 视作未启用，界面**必须**单独渲染成
    「未启用（不在允许清单里）」，不许显示成「已开启」。
    """
    enabled, disabled = plugin_lists(config)
    if plugin in disabled:
        return STATE_DISABLED
    if plugin in enabled:
        return STATE_ENABLED
    return STATE_UNAVAILABLE


def plugin_state(
    agent_id: str,
    plugin_id: str,
    *,
    home: Optional[Path] = None,
    tri_state: bool = False,
    config: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """该 agent **自己文件里**这个插件是否生效（只读，不合并全局层）。

    返回 ``{"plugin", "enabled", "in_disabled", "in_enabled", "source"}``，``source`` 取
    ``"disabled_list"`` / ``"enabled_list"`` / ``"absent"``。

    ``config`` 是**已经读好的**该 agent 配置（``scope.read_agent_config`` 的返回值）。批量列举
    （``GET /agents/<id>/plugins``）时必须传：不传就要为**每一个**插件重读一遍
    ``profiles/<id>/config.yaml`` 并整棵树 YAML 解析一次——实测 51 个插件 = 51 次解析
    ≈ 2.4s/请求（ruamel 解析大配置 ~46ms），这就是设置页"非常慢"的真正主因。
    ``None``（默认）保持原语义：自己读一次。

    ``tri_state=True`` 时**额外**带一个 ``"state"`` 字段（``enabled`` / ``disabled`` /
    ``unavailable``），给 M4b 的三态渲染用。默认关闭是刻意的：M2 已验证的
    ``scripts/probe_plugin_switch.py`` 断言了返回体的**精确键集合**，默认加键会把它打红，
    而那张卡明确要求「不改 M2 已验证的探针」。要三态就显式传 ``tri_state=True``
    （或直接用 :func:`plugin_state_tristate`）。

    * ``agent_id`` / ``plugin_id`` 非法 ⇒ ``ValueError``（在打开文件之前）
    * 该 agent 没有 ``config.yaml`` ⇒ 视作两份列表都空 ⇒ ``enabled=False, source="absent"``
      （不抛异常，``read_agent_config`` 的 fail-soft 口径）
    """
    agent = normalize_agent_id(agent_id, home=home)
    plugin = normalize_plugin_id(plugin_id)
    # 批量列举时由调用方传入已读好的配置：**一次请求只解析一遍 config.yaml**。
    if config is None:
        config = read_agent_config(agent, home=home)
    result = _state_from_config(plugin, config)
    if tri_state:
        result["state"] = _state_name(plugin, config)
    return result


def plugin_state_tristate(
    agent_id: str,
    plugin_id: str,
    *,
    home: Optional[Path] = None,
) -> Dict[str, Any]:
    """:func:`plugin_state` 的三态版（永远带 ``state`` 字段）。"""
    return plugin_state(agent_id, plugin_id, home=home, tri_state=True)


def list_plugin_states(
    agent_id: str,
    *,
    home: Optional[Path] = None,
    tri_state: bool = False,
) -> List[Dict[str, Any]]:
    """该 agent 的 ``enabled`` ∪ ``disabled`` 并集，逐个给 :func:`plugin_state`。

    顺序：先 ``enabled`` 的原始顺序，再补上只出现在 ``disabled`` 里的（同样按原始顺序），
    去重 —— 前端因此能稳定渲染，不会因为 dict 序变来变去跳动。

    ``tri_state=True`` 时每项多一个 ``state`` 字段（同上：默认关，M2 探针断言了精确键集合）。
    """
    agent = normalize_agent_id(agent_id, home=home)
    enabled, disabled = plugin_lists(read_agent_config(agent, home=home))

    ordered: List[str] = []
    seen = set()
    for name in list(enabled) + list(disabled):
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return [
        plugin_state(agent, name, home=home, tri_state=tri_state)
        for name in ordered
    ]



# ---------------------------------------------------------------------------
# 写（只碰 plugins.disabled）
# ---------------------------------------------------------------------------
def _write_disabled(agent: str, disabled: List[Any], *, home: Optional[Path]) -> None:
    """把 *disabled* 写进该 agent 的 ``plugins.disabled``，**payload 里根本没有 enabled**。

    ``home`` 在这里**刻意不参与落盘路径**（与 M1a 的 ``settings.save_setting`` 同一口径）：
    真正的落盘位置由官方 ``_config_profile_scope`` + ``get_config_path()`` 决定，锚定在**进程**
    home 上——dashboard 后端每个请求就是这样服务的。要让写入落到别的 home，请在进程启动前
    设好 ``HERMES_HOME``（探针就是这么做的）。``home`` 只用于 ``set_plugin_enabled`` 里的
    只读回读与存在性预检。

    照抄官方 ``hermes_cli/plugins_state.save_plugin_setting`` 的写入骨架（先例，不是自创）：

    1. 托管安装 / 管理员托管键 ⇒ ``PermissionError``（fail-closed，不静默跳过）
    2. ``_config_profile_scope(<agent>)`` —— 官方 profile 作用域（``default`` ⇒ ``None``）
    3. ``_locked_plugin_state(get_config_path())`` + ``_CONFIG_LOCK`` —— 官方跨进程/跨线程锁，
       锁在**作用域内**求值，所以锁的是该 agent 自己的 ``config.yaml``
    4. ``read_user_config_raw()`` —— fail-closed 原始读：YAML 坏了就抛，绝不拿 ``{}`` 去覆盖
    5. ``save_config(partial, preserve_keys={("plugins","disabled")}, merge_existing=True)``
       —— 官方原子写入器，**只提交 ``{"plugins": {"disabled": [...]}}`` 这一块**：
       磁盘上其它 section（含 ``plugins.enabled``）由官方 merge 从原文件带过来，
       所以本模块在结构上就改不到允许清单。

    与「先 load_config() 全量、改一个键、再 save_config()」相比，这里还避开了两件事：
    ``load_config`` 会把 ``${VAR}`` 展开、把 schema 默认值注进来；partial 写只提交我们自己
    构造的那一块，别的一律不经过进程内存。
    """
    from hermes_cli import config as config_mod  # 官方读写 + 官方跨进程锁
    from hermes_cli import managed_scope  # 管理员托管键判定
    from hermes_cli.plugins_state import _locked_plugin_state  # 官方插件状态锁
    from hermes_cli.web_server_profiles import _config_profile_scope  # 官方 profile 作用域

    dotted = "%s.%s" % (PLUGINS_SECTION, PLUGIN_DENY_KEY)
    if config_mod.is_managed():
        raise PermissionError("托管安装（managed install）不允许修改 %s" % dotted)
    if managed_scope.is_key_managed(dotted):
        raise PermissionError("%r 由管理员托管，无法修改" % dotted)

    partial = {PLUGINS_SECTION: {PLUGIN_DENY_KEY: list(disabled)}}
    scope = None if agent == DEFAULT_AGENT_ID else agent
    try:
        # 注意顺序：锁的表达式在进入作用域之后才求值 ⇒ 锁住的是该 agent 自己的 config.yaml
        with (
            _config_profile_scope(scope),
            _locked_plugin_state(config_mod.get_config_path()),
            config_mod._CONFIG_LOCK,
        ):
            config_mod.read_user_config_raw()  # fail-closed：YAML 坏掉就抛，不覆盖
            config_mod.save_config(
                partial,
                preserve_keys={(PLUGINS_SECTION, PLUGIN_DENY_KEY)},
                merge_existing=True,
            )
    except Exception as exc:  # noqa: BLE001 - 把官方 404 换成 ValueError，其余原样上抛
        if exc.__class__.__name__ == "HTTPException" and getattr(exc, "status_code", None) in (400, 404):
            raise ValueError("agent %r 的写入作用域不可用：%s" % (agent, exc)) from exc
        raise


def _config_file_digest(agent: str, *, home: Optional[Path] = None) -> str:
    """该 agent ``config.yaml`` 的字节级摘要（乐观并发的 ``expected_config``）。

    优先用官方 ``hermes_cli.runtime_state._digest``（= ``pm.filesystem.file_digest``，就是
    sha256 十六进制；文件不存在给 ``None``，这里翻成 ``"missing"`` —— 与
    ``pm.publication.PluginSelection`` 的口径一致）。官方 import 不到时用等价的本地产法，
    两者字节级同值，不会因为 import 失败就把并发保护丢掉。
    """
    from .scope import agent_config_path

    path = Path(agent_config_path(agent, home=home))
    try:
        from hermes_cli.runtime_state import _digest as official_digest

        return official_digest(path) or "missing"
    except Exception:  # noqa: BLE001 - 官方不可用时退到同值本地实现
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "missing"


def _admit_plugin_enable(
    agent: str,
    plugin: str,
    *,
    enabled: set,
    disabled: set,
    expected_config: Optional[str],
    home: Optional[Path],
) -> None:
    """把候选集交给官方唯一准入权威 :func:`_admit_and_save_plugin_sets`。

    **自己不拼任何 YAML**：候选集（``enabled`` / ``disabled`` 两个集合）原样交给官方，由
    ``pm`` 在 worker 事务里做「读-改-字节级 CAS-写」。

    作用域：``agent == "default"`` ⇒ ``_config_profile_scope(None)``；其余 ⇒
    ``_config_profile_scope(<agent>)``。官方 ``_plugins_dir()`` / ``get_hermes_home()`` 都是
    **上下文局部**的，所以 scope 一进，``active_plugins_dir`` 就锚在该 agent 的
    ``<home>/plugins``，``PluginSelection.home`` 也就落在该 agent 的 home —— 写的是
    ``profiles/<agent>/config.yaml``，不是根配置。

    进 scope 后**再断言一次** ``get_hermes_home()`` 真的是这个 agent 的 home：scope 没生效时
    官方会去写**根** ``config.yaml``（实测过），那是跨 agent 串味，必须当场拒绝。

    抛出：
      * :class:`PluginSwitchAdmissionError` —— 通道用不了（import 失败 / scope 没生效）
      * :class:`PluginSwitchRefused` —— 官方拒绝（``AdmissionRefused`` / ``DependencyConflict``）
    """
    try:
        from hermes_cli.plugins_admission import AdmissionRefused, DependencyConflict
        from hermes_cli.plugins_cmd import _admit_and_save_plugin_sets
        from hermes_cli.web_server_profiles import _config_profile_scope
    except Exception as exc:  # noqa: BLE001 - 通道缺失就是 unavailable，不是 refused
        raise PluginSwitchAdmissionError(
            CODE_ADMISSION_UNAVAILABLE, "官方准入通道不可用：%s" % (exc,)
        ) from exc

    from hermes_constants import get_hermes_home

    from .scope import agent_config_path

    expected_home = Path(agent_config_path(agent, home=home)).parent
    scope = None if agent == DEFAULT_AGENT_ID else agent
    try:
        with _config_profile_scope(scope):
            actual_home = Path(get_hermes_home())
            if actual_home.resolve() != expected_home.resolve():
                raise PluginSwitchAdmissionError(
                    CODE_ADMISSION_UNAVAILABLE,
                    "profile 作用域没有生效（期望 home=%s，实际 %s）：拒绝调用准入，"
                    "否则会改到别的 agent 的配置" % (expected_home, actual_home),
                )
            try:
                _admit_and_save_plugin_sets(
                    set(enabled),
                    set(disabled),
                    action="enable",
                    plugin=plugin,
                    expected_config=expected_config,
                )
            except (AdmissionRefused, DependencyConflict) as exc:
                code = (
                    CODE_DEPENDENCY_CONFLICT
                    if isinstance(exc, DependencyConflict)
                    else CODE_ADMISSION_REFUSED
                )
                raise PluginSwitchRefused(code, str(exc)) from exc
    except PluginSwitchAdmissionError:
        raise
    except PluginSwitchRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - 官方其它异常也按 unavailable 上报，绝不吞
        raise PluginSwitchAdmissionError(
            CODE_ADMISSION_UNAVAILABLE, "官方准入通道调用失败：%s: %s" % (type(exc).__name__, exc)
        ) from exc



def _result_from_config(
    agent: str,
    plugin: str,
    *,
    want: bool,
    changed: bool,
    config: Mapping[str, Any],
    state_before: str,
    via: str,
    notes: List[str],
) -> Dict[str, Any]:
    """按 M2 的键位构造结果体（**只加键、不改键**），再补三态 / via / notes。"""
    state = _state_from_config(plugin, config)
    result: Dict[str, Any] = {
        "plugin": plugin,
        "agent": agent,
        "ok": True,
        "changed": changed,
        "requested_enabled": want,
        "enabled": state["enabled"],
        "source": state["source"],
        "in_enabled": state["in_enabled"],
        "in_disabled": state["in_disabled"],
        "disabled": sorted(_string_list(_plugin_section(config).get(PLUGIN_DENY_KEY))),
        "state_before": state_before,
        "state_after": _state_name(plugin, config),
        "via": via,
        "notes": list(notes),
    }
    if want and not state["in_enabled"]:
        result["warning"] = WARN_NOT_IN_ALLOW_LIST
        notes.append(
            "开启只放开了黑名单；它不在 plugins.enabled 里（state=%r），实际仍未启用 ——"
            " 再调用一次开启会走官方准入通道把它真正启用" % STATE_UNAVAILABLE
        )
    return result


def _enable_via_admission(
    agent: str,
    plugin: str,
    *,
    before: Mapping[str, Any],
    home: Optional[Path],
    expected_config: Optional[str],
    notes: List[str],
) -> Dict[str, Any]:
    """``unavailable → enabled``：候选集交给官方准入权威，成功/拒绝都返回结构化结果。

    * 成功 ⇒ ``ok=True, changed=True, via="admission", state_after="enabled"``（回读断言）
    * 官方拒绝 ⇒ ``ok=False, refused=True, code, message, unchanged``（回读断言一个字节没变）
    * 通道用不了 ⇒ 抛 :class:`PluginSwitchAdmissionError`，由调用方按 ``strict`` 决定怎么办
    """
    enabled_names, disabled_names = plugin_lists(before)
    # 两个摘要分工不同，别混：
    #   * ``expected_config`` 是**传给官方 CAS 的令牌**（调用方可能故意传一个过期的来做并发测试）
    #   * ``actual_before`` 是**这次调用真正开始时的文件摘要**，只用来断言「拒绝路径零写入」
    actual_before = _config_file_digest(agent, home=home)
    cas_token = expected_config if expected_config is not None else actual_before
    notes.append(
        "插件既不在 plugins.enabled 也不在 plugins.disabled（unavailable）："
        "走官方准入权威 _admit_and_save_plugin_sets"
    )
    try:
        _admit_plugin_enable(
            agent,
            plugin,
            enabled=set(enabled_names) | {plugin},
            disabled=set(disabled_names) - {plugin},
            expected_config=cas_token,
            home=home,
        )
    except PluginSwitchRefused as exc:
        after = read_agent_config(agent, home=home)
        digest_after = _config_file_digest(agent, home=home)
        unchanged = digest_after == actual_before
        notes.append("官方准入拒绝（%s）：%s" % (exc.code, exc))
        if not unchanged:
            # 「拒绝不该改配置」是官方契约；真变了就是硬故障，必须炸出来而不是当正常拒绝返回
            raise PluginSwitchWriteError(
                "官方准入拒绝了本次修改，但 agent=%r 的 config.yaml 摘要变了（%s -> %s）——"
                "拒绝路径必须零写入，已中止" % (agent, actual_before, digest_after)
            ) from exc
        result = _result_from_config(
            agent,
            plugin,
            want=True,
            changed=False,
            config=after,
            state_before=STATE_UNAVAILABLE,
            via=VIA_ADMISSION,
            notes=notes,
        )
        result.update(
            {
                "ok": False,
                "refused": True,
                "code": exc.code,
                "message": str(exc),
                "unchanged": True,
            }
        )
        result.pop("warning", None)
        return result

    after = read_agent_config(agent, home=home)
    digest_after = _config_file_digest(agent, home=home)
    result = _result_from_config(
        agent,
        plugin,
        want=True,
        changed=True,
        config=after,
        state_before=STATE_UNAVAILABLE,
        via=VIA_ADMISSION,
        notes=notes,
    )
    if digest_after == actual_before:
        raise PluginSwitchWriteError(
            "准入通过但 agent=%r 的 config.yaml 一个字节没变（摘要仍是 %s）——写入没有落地"
            % (agent, actual_before)
        )
    if result["state_after"] != STATE_ENABLED:
        raise PluginSwitchWriteError(
            "准入通过但回读状态是 %r（期望 %r）：agent=%r plugin=%r"
            % (result["state_after"], STATE_ENABLED, agent, plugin)
        )
    return result


def set_plugin_enabled(
    agent_id: str,
    plugin_id: str,
    enabled: bool,
    *,
    strict: bool = True,
    home: Optional[Path] = None,
    expected_config: Optional[str] = None,
) -> Dict[str, Any]:
    """按 per-agent 三态开关一个插件，返回结构化结果（不是 ``None``）。

    三条路径（对应卡片的行为契约）：

    关闭（``enabled=False``）
        把 ``plugin_id`` 追加进该 agent 的 ``plugins.disabled``；已在则**不写盘**（幂等）。
        ``plugins.enabled`` 一个字节不动。``via="deny_list"``。

    开启，且它在 ``plugins.disabled`` 里
        从 ``plugins.disabled`` 移除（不在则**不写盘**）。``via="deny_list"``。若它也不在
        ``plugins.enabled`` 里，照 M2 口径带 ``warning``（原文见
        :data:`WARN_NOT_IN_ALLOW_LIST`）—— 这一步只放开黑名单，**不替用户改允许清单**。

    开启，且它**既不在** ``enabled`` **也不在** ``disabled``（三态 ``unavailable``）
        走官方唯一准入权威 ``_admit_and_save_plugin_sets``：``enabled = 当前 ∪ {plugin}``、
        ``disabled = 当前 - {plugin}``、``action="enable"``、``plugin=plugin``。
        ``via="admission"``。

    返回 ``{"plugin", "agent", "ok", "changed", "requested_enabled", "enabled", "source",
    "in_enabled", "in_disabled", "disabled", "state_before", "state_after", "via", "notes"}``，
    条件允许时附 ``warning``。被官方拒绝时返回 ``{"ok": False, "refused": True, "code",
    "message", "unchanged": True}`` —— **原样透出，不吞**。

    参数：
      * ``strict=True``（默认）：准入通道用不了（import 失败 / profile 作用域没生效）时**抛**
        :class:`PluginSwitchAdmissionError`，绝不假装成功。
      * ``strict=False``：通道用不了时退化成 M2 的「只摘黑名单 + warning」，退化原因写进
        ``notes``。
      * ``expected_config``：乐观并发。传你读到的 ``config.yaml`` 摘要（官方
        ``runtime_state._digest``）；不传则本函数在读配置后立刻取一次 —— 期间别人改了配置，
        官方 CAS 会**拒绝**而不是覆盖。

    * ``agent_id`` / ``plugin_id`` / ``enabled`` / ``strict`` / ``expected_config`` 非法 ⇒
      ``ValueError``，**在摸文件之前**
    * agent 的 profile 目录不存在 ⇒ ``ValueError``（提前把官方 404 换掉）
    * 写后回读不符 ⇒ :class:`PluginSwitchWriteError`
    """
    agent = normalize_agent_id(agent_id, home=home)          # ① 文法校验，不碰文件
    plugin = normalize_plugin_id(plugin_id)                  # ② 同上
    want = _require_bool(enabled)                            # ③ 同上
    if not isinstance(strict, bool):
        raise ValueError("strict 必须是布尔值（True/False），收到 %r" % (strict,))
    if expected_config is not None and not isinstance(expected_config, str):
        raise ValueError("expected_config 必须是字符串或 None，收到 %r" % (expected_config,))
    _require_writable_agent(agent, home=home)                # ④ 目录存在性，仍不写

    before = read_agent_config(agent, home=home)
    state_before = _state_name(plugin, before)
    notes: List[str] = []

    # ---- 第三态：开启一个「两边名单里都没有」的插件 ⇒ 官方准入 ----
    if want and state_before == STATE_UNAVAILABLE:
        try:
            return _enable_via_admission(
                agent,
                plugin,
                before=before,
                home=home,
                expected_config=expected_config,
                notes=notes,
            )
        except PluginSwitchAdmissionError as exc:
            if strict:
                raise
            notes.append("官方准入通道不可用（%s）；strict=False 退化为只摘黑名单" % (exc,))
            before = read_agent_config(agent, home=home)  # 退化前重读，别拿旧快照写

    before_section = _plugin_section(before)
    before_enabled_raw = before_section.get(PLUGIN_ALLOW_KEY)
    before_disabled_raw = _raw_list(before_section.get(PLUGIN_DENY_KEY))
    _before_enabled_names, before_disabled = plugin_lists(before)
    state_before = _state_name(plugin, before)   # 退化分支里 before 已重读，这里再算一次

    if want:  # ---- 开启：只从黑名单里摘 ----
        kept = [item for item in before_disabled_raw if not (isinstance(item, str) and item == plugin)]
        changed = kept != before_disabled_raw
        new_disabled = kept
    else:     # ---- 关闭：只往黑名单里加 ----
        if plugin in before_disabled:
            new_disabled = before_disabled_raw
            changed = False
        else:
            new_disabled = before_disabled_raw + [plugin]
            changed = True

    if changed:
        _write_disabled(agent, new_disabled, home=home)
        after = read_agent_config(agent, home=home)
        after_section = _plugin_section(after)
        _after_enabled, after_disabled = plugin_lists(after)

        # ① 目标键真的变了吗
        if want and plugin in after_disabled:
            raise PluginSwitchWriteError(
                "写入未生效：agent=%r plugin=%r 仍出现在 plugins.disabled 里（%r）"
                % (agent, plugin, sorted(after_disabled))
            )
        if not want and plugin not in after_disabled:
            raise PluginSwitchWriteError(
                "写入未生效：agent=%r plugin=%r 没有出现在 plugins.disabled 里（%r）"
                % (agent, plugin, sorted(after_disabled))
            )

        # ② ★ 运行时守卫：plugins.enabled 的原始值必须与写前完全一致
        after_enabled_raw = after_section.get(PLUGIN_ALLOW_KEY)
        if json.dumps(after_enabled_raw, ensure_ascii=False, sort_keys=True, default=str) != json.dumps(
            before_enabled_raw, ensure_ascii=False, sort_keys=True, default=str
        ):
            raise PluginSwitchWriteError(
                "检测到 plugins.enabled 被改动（agent=%r plugin=%r）：写前=%s 写后=%s"
                " —— 本模块禁止触碰允许清单，已中止"
                % (
                    agent,
                    plugin,
                    json.dumps(before_enabled_raw, ensure_ascii=False, default=str),
                    json.dumps(after_enabled_raw, ensure_ascii=False, default=str),
                )
            )
    else:
        after = before

    result = _result_from_config(
        agent,
        plugin,
        want=want,
        changed=changed,
        config=after,
        state_before=state_before,
        via=VIA_DENY_LIST,
        notes=notes,
    )
    return result


__all__ = [
    "CODE_ADMISSION_REFUSED",
    "CODE_ADMISSION_UNAVAILABLE",
    "CODE_DEPENDENCY_CONFLICT",
    "PLUGIN_ALLOW_KEY",
    "PLUGIN_DENY_KEY",
    "PLUGINS_SECTION",
    "PluginSwitchAdmissionError",
    "PluginSwitchRefused",
    "PluginSwitchWriteError",
    "SOURCE_ABSENT",
    "SOURCE_DISABLED_LIST",
    "SOURCE_ENABLED_LIST",
    "STATE_DISABLED",
    "STATE_ENABLED",
    "STATE_UNAVAILABLE",
    "VIA_ADMISSION",
    "VIA_DENY_LIST",
    "WARN_NOT_IN_ALLOW_LIST",
    "list_plugin_states",
    "normalize_plugin_id",
    "plugin_lists",
    "plugin_state",
    "plugin_state_tristate",
    "set_plugin_enabled",
]

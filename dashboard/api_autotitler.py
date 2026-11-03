# -*- coding: utf-8 -*-
"""hermes-console · 自动改名插件（hermes-auto-titler）面板后端。

两个只读面 + 一条官方写入通道：

* ``GET /autotitler/settings?agent=`` —— 按插件 ``config_schema`` **动态**列出全部参数
  （类型 / 默认值 / 说明 / choices），每个键给**当前生效值**与**值来自哪一层**。
* ``GET /autotitler/overview?agent=`` —— 运行态（state.json）+ 日志健康度（成功/失败/跳过、
  失败原因分类、最近失败明细）+ 重试队列 + 每会话明细。
* ``PUT /autotitler/settings`` —— 单键写入，只走宿主官方 settings 通道
  （``core.settings.save_plugin_setting_for`` → ``plugins.entries.<plugin>.settings.<key>``），
  **绝不**写插件的 ``config.yaml``，**绝不**写 ``plugin-data/.../state.json``。

三条硬约束（卡片原文）：

① **插件源码只读**：本文件不 import 插件包、不改插件目录任何字节；参数语义（类型/边界）
   从 ``plugin.yaml`` 的 ``config_schema`` 读，边界规则见 :data:`BOUNDS`（与插件
   ``config.coerce_value`` 的钳制区间一致，但这里是**拒绝**而不是静默钳制，好让人看见自己填错了）。

② **写入只认官方通道**：``save_plugin_setting_for`` 内部带跨进程锁 + 写后回读断言；本文件
   在它之上再做一次独立回读断言（退出码/返回值会撒谎，磁盘上的值不会）。

③ **不动别人的卡**：本文件不 import ``api_models`` / ``api_autogroup*`` / ``autogroup_ai*``，
   也不改它们；只复用 ``core.*`` 与 ``api_console_settings`` 的只读助手。

失败诊断口径：插件的 ``act=failed`` 一律附带 ``reason=model call failed``，真正的根因在
前一行 WARNING —— ``model returned invalid decision payload``（模型回散文而不是 JSON 决策载荷）。
本模块按**会话 id**把 WARNING 与 audit 行对齐，所以 "invalid decision payload" 能独立归类，
而不是被 "model call failed" 吞掉。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from fastapi import APIRouter, Request  # type: ignore
from fastapi.responses import JSONResponse  # type: ignore
from starlette.concurrency import run_in_threadpool  # type: ignore

_DASHBOARD_DIR = Path(__file__).resolve().parent
if str(_DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD_DIR))

router = APIRouter()
logger = logging.getLogger(__name__)

#: 被监控的插件 id（宿主 settings 落点 ``plugins.entries.<此 id>.settings``）。
PLUGIN_ID = "hermes-auto-titler"

CODE_INVALID_ARGUMENT = "invalid_argument"
CODE_UNKNOWN_SETTING_KEY = "unknown_setting_key"
CODE_READONLY_SETTING = "readonly_setting"
CODE_CONFIG_MISSING = "config_missing"
CODE_CONFIG_CHANGED = "config_changed"
CODE_WRITE_NOT_APPLIED = "write_not_applied"
CODE_VALUE_REJECTED = "value_rejected"
CODE_UPSTREAM_ERROR = "upstream_error"

#: 值来源层（从低到高）。顺序即优先级，合并时后者覆盖前者。
SOURCE_DEFAULT = "default"
SOURCE_PLUGIN_FILE = "plugin_config_yaml"
SOURCE_HOST_GLOBAL = "host_settings_global"
SOURCE_HOST_AGENT = "host_settings_agent"

SOURCE_ORDER: Tuple[str, ...] = (
    SOURCE_DEFAULT,
    SOURCE_PLUGIN_FILE,
    SOURCE_HOST_GLOBAL,
    SOURCE_HOST_AGENT,
)

SOURCE_LABEL = {
    SOURCE_DEFAULT: "插件默认值",
    SOURCE_PLUGIN_FILE: "插件 config.yaml（0.2 存量）",
    SOURCE_HOST_GLOBAL: "宿主 settings（全局 default）",
    SOURCE_HOST_AGENT: "宿主 settings（该 agent）",
}

#: 需要重启才生效的键（插件 ``config_schema`` 的 description 明写 reload 的那两个）。
RESTART_KEYS: Tuple[str, ...] = ("enabled", "first_title_mode")

#: 数值型边界（与插件 ``hermes_auto_titler/config.py::coerce_value`` 的钳制区间逐条对齐）。
#:   ``None`` 表示该端不限制。写超界一律 400 拒绝（不静默钳制），错误里带上区间。
BOUNDS: Dict[str, Tuple[Optional[float], Optional[float], str]] = {
    "every_n_turns": (1, None, "整数，最小 1"),
    "opening_turns": (1, None, "整数，最小 1"),
    "recent_turns": (1, None, "整数，最小 1"),
    "min_interval_minutes": (0.0, None, "分钟，最小 0"),
    "preview_chars": (0, None, "字符数，最小 0"),
    "user_message_threshold": (0, None, "条数，最小 0（0 = 不过滤中间轮次）"),
    "user_message_preview_chars": (0, None, "字符数，最小 0"),
    "summary_preview_chars": (0, None, "字符数，最小 0"),
    "retitle_summary_chars": (0, None, "字符数，最小 0（0 = 沿用 preview_chars）"),
    "rename_confirmations": (0, None, "次数，最小 0（0 = 一次判定直接写）"),
    "max_renames_per_session": (0, None, "次数，最小 0（0 = 不限制）"),
    # max_title_length 的两个合法区段是「0（关闭）」与「10..100」，单独判断。
    "max_display_width": (10, 100, "列宽，10..100"),
}

#: 语义分组（键 → 组 id）。**没有分组的键自动落进「其它」组** —— 上游加参数不需要改代码。
GROUPS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("switch", "开关与模式", ("enabled", "first_title_mode", "on_close")),
    (
        "naming",
        "命名策略",
        (
            "strategy",
            "title_style",
            "rename_confirmations",
            "max_renames_per_session",
            "max_display_width",
            "max_title_length",
            "custom_instructions",
        ),
    ),
    ("rhythm", "触发节奏", ("every_n_turns", "min_interval_minutes")),
    ("channel", "模型通道", ("title_model", "provider")),
    (
        "context",
        "上下文取样",
        (
            "opening_turns",
            "recent_turns",
            "preview_chars",
            "include_all_user_messages",
            "user_message_threshold",
            "user_message_preview_chars",
            "ignore_model_messages",
            "summary_preview_chars",
            "retitle_summary_chars",
        ),
    ),
)

#: 分组归属反查（组 id → 组名 / 键集合）。
GROUP_NAMES: Dict[str, str] = {gid: gname for gid, gname, _ in GROUPS}
GROUP_KEYS: Dict[str, Tuple[str, ...]] = {gid: keys for gid, _, keys in GROUPS}
OTHER_GROUP = "other"

#: 用户点名要能改的四件事 → 具体键。界面把它们**置顶**（且不再在下方的语义分组里重复出现），
#: 因为「提示词设计」这类东西藏在第 18 项里等于没有。
#: 这里只是**展示顺序**的提示，写入约束仍然来自 ``config_schema``（不硬编码类型/边界）。
PINNED: Tuple[Tuple[str, str, str, Tuple[str, ...]], ...] = (
    (
        "enabled",
        "① 是否开启自动命名",
        "关掉它 = 不再跟踪、不再调模型；插件 config_schema 明写此项需要重启 Hermes 才生效。",
        ("enabled",),
    ),
    (
        "rhythm",
        "② 多久监测一次",
        "两个门槛同时生效：每 N 轮才评估一次，且同一会话两次例行检查至少隔 M 分钟。",
        ("every_n_turns", "min_interval_minutes"),
    ),
    (
        "model",
        "③ 使用什么模型",
        "留空 = 走 Hermes 自己的标题生成通道；填了就覆盖模型与 provider。",
        ("title_model", "provider"),
    ),
    (
        "prompt",
        "④ 提示词设计",
        "追加到标题生成的系统提示里；改完即时生效（插件每轮重读配置）。",
        ("custom_instructions",),
    ),
)

PINNED_KEYS: Tuple[str, ...] = tuple(key for _id, _label, _hint, keys in PINNED for key in keys)

#: 日志里 audit 行的固定形状（插件 ``titler.evaluate`` 写的）：
#:   ``auto-titler audit: sid=<8位> act=<动作> reason=<...> dur=<整数>ms``
_AUDIT_RE = re.compile(
    r"auto-titler audit:\s+sid=(\S+)\s+act=(\w+)(?:\s+reason=(.*?))?(?:\s+dur=(\d+)ms)?\s*$"
)
#: ``auto-titler <sid>: model returned invalid decision payload: '<text>'``
_INVALID_PAYLOAD_RE = re.compile(
    r"auto-titler (\S+): model returned invalid decision payload: (.*)$"
)
#: ``auto-titler <sid>: recorded failure (attempt N, class=<cls>, next retry in Xs)``
_RECORDED_FAILURE_RE = re.compile(
    r"auto-titler (\S+): recorded failure \(attempt (\d+), class=(\w+)(?:, next retry in (\d+)s)?"
)
#: ``auto-titler <sid>: llm result action=error title=None input=...``
_LLM_RESULT_RE = re.compile(r"auto-titler (\S+): llm result action=(\w+)")
#: 插件 ``register()`` 时打的一条：证明这个进程真的把 hooks 注册上了
#: （``hermes-auto-titler: coexistence mode (first_title_mode=builtin); host title generation untouched``）
_COEXIST_RE = re.compile(r"hermes-auto-titler: coexistence mode \(first_title_mode=(\w+)\)")
#: 日志行前缀 ``2026-10-30 20:11:52,822 WARNING ...``
_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[.,](\d{3})")

#: 每个日志文件最多读多少字节（从尾部读；日志可能几 MB，全读会把一个 GET 拖成几秒）。
LOG_TAIL_BYTES = 4 * 1024 * 1024
#: 最多看几个轮转文件（agent.log + agent.log.1..N）。0 = 只看当前文件。
LOG_MAX_FILES = 4

# ---------------------------------------------------------------------------
# 时间窗口：「历史」与「当前」必须分开算
#
# 为什么要有这层：插件曾经连着失败 109 次（模型回散文而非 JSON），修好之后失败归零。
# 但面板若只给一个「累计失败率」，它会永远显示 98%，用户看到的就是「还在报错」——
# 这是**读法**骗人，不是系统还在坏。所以每个计数都必须挂在明确的时间区间上。
# ---------------------------------------------------------------------------
WINDOW_1H = "1h"
WINDOW_24H = "24h"
WINDOW_ALL = "all"

#: ``(id, 人话标签, 小时数 | None=不限)``。顺序即界面按钮顺序。
WINDOWS: Tuple[Tuple[str, str, Optional[float]], ...] = (
    (WINDOW_1H, "最近 1 小时", 1.0),
    (WINDOW_24H, "最近 24 小时", 24.0),
    (WINDOW_ALL, "全部历史", None),
)

WINDOW_LABELS: Dict[str, str] = {wid: label for wid, label, _ in WINDOWS}

#: 默认窗口。取 ``all`` 是为了**向后兼容**：老调用方（不带 ``window``）拿到的仍是整份
#: 日志的计数，与 ``grep`` 逐条对得上；界面自己显式传 ``window=24h``。
DEFAULT_WINDOW = WINDOW_ALL

#: 时间线最多画多少格（多了界面糊成一片，少了看不出「修好之后」）。
TIMELINE_MAX_BUCKETS = 24
#: 时间线最小格宽（秒）：1 小时。日志只跨几十分钟时不要画出 24 个空格子。
TIMELINE_MIN_BUCKET_SECONDS = 3600

#: 日志里 audit 行允许出现在响应里的最大文本长度（脱敏：正文一律不返回）。
MAX_REASON_CHARS = 160
MAX_SID_CHARS = 64

#: 一次最多返回多少条 audit 行（只读列表，不提供导出/删除）。
LOG_ROWS_DEFAULT = 200
LOG_ROWS_MAX = 1000


def _naive_local_epoch(moment: Any) -> float:
    """``datetime``（naive，本地时间）→ epoch 秒。

    Windows 上 ``datetime.timestamp()`` 对 1970 附近的负 epoch 会抛 ``OSError``
    （时区换算越界），所以这里带一个纯算术兜底：本地时间差 - 时区偏移。
    """
    try:
        return float(moment.timestamp())
    except (OSError, OverflowError, ValueError):
        import datetime as _dt

        delta = moment - _dt.datetime(1970, 1, 1)
        return delta.total_seconds() - float(time.timezone)


def _ts_to_epoch(text: str) -> Optional[float]:
    """``2026-10-30 20:00:02.020`` → epoch 秒（**本地时区**，与日志写入方一致）。

    解析不了就返回 ``None``：这类行会被算进「总数」但不算进任何时间窗口，
    界面要能看出「有多少行没时间戳」，而不是被静默丢掉。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            import datetime as _dt

            return _naive_local_epoch(_dt.datetime.strptime(raw, fmt))
        except ValueError:
            continue
    return None


def _epoch_to_ts(value: Optional[float]) -> str:
    if value is None:
        return ""
    import datetime as _dt

    try:
        return _dt.datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def _parse_ts_arg(raw: Any) -> Optional[float]:
    """接受 epoch 秒（``1793369720``）或 ``YYYY-MM-DD HH:MM[:SS]`` / ISO ``T`` 分隔。

    非法值抛 :class:`ApiError(400)` —— 一个含糊的 ``since`` 会让整块统计失真，
    宁可拒绝也不猜。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    text = str(raw).strip()
    try:
        return float(text)
    except ValueError:
        pass
    normalized = text.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            import datetime as _dt

            return _naive_local_epoch(_dt.datetime.strptime(normalized, fmt))
        except (ValueError, OSError, OverflowError):
            continue
    raise ApiError(
        400,
        CODE_INVALID_ARGUMENT,
        "时间参数看不懂：%r。用 epoch 秒（1793369720）或 ``YYYY-MM-DD HH:MM[:SS]``" % (raw,),
    )


class ApiError(Exception):
    """带 HTTP 语义的业务错误（→ 统一失败体 ``{ok:false, error:{code,message}}``）。"""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra: Dict[str, Any] = dict(extra)


def _fail(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    error: Dict[str, Any] = {"code": code, "message": message}
    error.update({key: value for key, value in extra.items() if key not in ("code", "message")})
    return JSONResponse(status_code=status, content={"ok": False, "error": error})


def _error_response(exc: ApiError) -> JSONResponse:
    return _fail(exc.status, exc.code, exc.message, **exc.extra)


# ---------------------------------------------------------------------------
# core 惰性 import（与 api_console_settings 同一套防「同名别家模块」的写法）
# ---------------------------------------------------------------------------
def _core(submodule: str) -> Any:
    """import ``core.<submodule>``；坏掉时抛 ``ApiError(503)`` 而不是拖垮整个路由。"""
    import importlib
    import threading

    lock = _CORE_LOCK
    with lock:
        existing = sys.modules.get("core")
        if existing is not None:
            found = getattr(existing, "__file__", None)
            same = bool(found) and _DASHBOARD_DIR in Path(found).resolve().parents
            if not same:
                for name in [k for k in sys.modules if k == "core" or k.startswith("core.")]:
                    sys.modules.pop(name, None)
        try:
            return importlib.import_module("core.%s" % submodule)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(
                503,
                CODE_UPSTREAM_ERROR,
                "控制台核心模块 core.%s 不可用：%s: %s" % (submodule, type(exc).__name__, exc),
            ) from exc


_CORE_LOCK = __import__("threading").Lock()


# ---------------------------------------------------------------------------
# 插件安装位置 / config_schema / 版本（全部只读）
# ---------------------------------------------------------------------------
def _plugin_dir(agent: str) -> Optional[Path]:
    """插件目录：先该 agent 自己的 ``plugins/``，再 home 根。找不到 ⇒ ``None``。"""
    scope = _core("scope")
    for base in (
        Path(scope.agent_home(agent)) / "plugins",
        Path(scope.hermes_home()) / "plugins",
    ):
        candidate = base / PLUGIN_ID
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


def _read_manifest(agent: str) -> Dict[str, Any]:
    """插件 ``plugin.yaml`` 的解析结果（读不到 ⇒ ``{}``）；只读，永不抛。"""
    directory = _plugin_dir(agent)
    if directory is None:
        return {}
    manifest = directory / "plugin.yaml"
    try:
        if not manifest.is_file():
            return {}
        import yaml  # 宿主自带

        data = yaml.safe_load(manifest.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001 - 读不到就是没有 manifest
        logger.warning("hermes-console.autotitler: 读 %s 失败：%s", manifest, exc)
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


def _config_schema(agent: str) -> Dict[str, Dict[str, Any]]:
    """``plugin.yaml`` 的 ``config_schema``（``{键: 声明}``），只保留 mapping 值。"""
    raw = _read_manifest(agent).get("config_schema")
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): dict(value) for key, value in raw.items() if isinstance(value, Mapping)}


def _schema_defaults(schema: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    return {key: spec["default"] for key, spec in schema.items() if "default" in spec}


def _plugin_version(agent: str) -> Optional[str]:
    version = _read_manifest(agent).get("version")
    if isinstance(version, bool):
        return None
    if isinstance(version, (int, float)):
        return str(version)
    if isinstance(version, str) and version.strip():
        return version.strip()
    return None


def _declared_hooks(agent: str) -> List[str]:
    hooks = _read_manifest(agent).get("provides_hooks")
    if not isinstance(hooks, (list, tuple)):
        return []
    return [str(item) for item in hooks if isinstance(item, str) and item.strip()]


def _plugin_config_yaml(agent: str) -> Dict[str, Any]:
    """插件目录里那份 **0.2 存量** ``config.yaml``（只读；不存在 ⇒ ``{}``）。

    它在优先级里排在宿主 settings **之下**：插件 ``config.load_config`` 逐键判
    "这个键是否已被更高层显式设过"。这里把三层的原始值都读出来，界面上就能说清
    "现在生效的 5 分钟是 config.yaml 写的，而你在面板上写过的值会盖掉它"。
    """
    directory = _plugin_dir(agent)
    if directory is None:
        return {}
    path = directory / "config.yaml"
    try:
        if not path.is_file():
            return {}
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes-console.autotitler: 读 %s 失败：%s", path, exc)
        return {}
    if not isinstance(data, Mapping):
        return {}
    # 插件把 ``model`` 当内部键、``title_model`` 当对外别名；config.yaml 里两种写法都可能出现。
    out: Dict[str, Any] = {}
    for key, value in data.items():
        if value is None:
            continue
        out[str(key)] = value
    return out


def _host_layers(agent: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """宿主 settings 的两层原始值：``(全局 default 层, 该 agent 层)``。

    ``agent == "default"`` 时两者是同一份文件，第二层返回 ``{}``（否则每个值都会被误标成
    "这个 agent 自己改过"——与 ``core.settings.effective_settings`` 同口径）。
    """
    settings_mod = _core("settings")
    scope = _core("scope")
    global_layer = dict(settings_mod.read_plugin_settings(scope.DEFAULT_AGENT_ID, PLUGIN_ID))
    if agent == scope.DEFAULT_AGENT_ID:
        return global_layer, {}
    return global_layer, dict(settings_mod.read_plugin_settings(agent, PLUGIN_ID))


def _typed(raw: Any, spec: Mapping[str, Any]) -> Any:
    """按 schema 声明的类型把 YAML 值归一化；归不了就原样返回（显示层不做拒绝）。"""
    declared = str(spec.get("type") or "").lower()
    if declared == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)) and raw in (0, 1):
            return bool(raw)
        if isinstance(raw, str):
            text = raw.strip().lower()
            if text in ("true", "1", "yes", "on"):
                return True
            if text in ("false", "0", "no", "off"):
                return False
        return raw
    if declared == "int":
        if isinstance(raw, bool):
            return raw
        try:
            return int(raw)
        except (TypeError, ValueError):
            return raw
    if declared == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return raw
    if declared == "str":
        return raw if isinstance(raw, str) else str(raw)
    return raw


def _effective(agent: str, schema: Mapping[str, Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """逐键算**当前生效值 + 来源层**。

    优先级（高 → 低）与插件 ``config.load_config`` 一致：
    宿主该 agent 层 > 宿主全局层 > 插件 ``config.yaml`` > ``config_schema`` 默认值。

    插件还有一条 ``ctx.get_config()`` 通道（运行期读同一份宿主 settings），它读的落点与
    ``_host_layers`` 完全相同，所以这里不另算一层，只在 note 里写明。
    """
    defaults = _schema_defaults(schema)
    plugin_layer = _plugin_config_yaml(agent)
    global_layer, agent_layer = _host_layers(agent)

    sources: Sequence[Tuple[str, Mapping[str, Any]]] = (
        (SOURCE_DEFAULT, defaults),
        (SOURCE_PLUGIN_FILE, plugin_layer),
        (SOURCE_HOST_GLOBAL, global_layer),
        (SOURCE_HOST_AGENT, agent_layer),
    )
    out: Dict[str, Dict[str, Any]] = {}
    for key, spec in schema.items():
        chosen: Dict[str, Any] = {
            "value": defaults.get(key),
            "source": SOURCE_DEFAULT if key in defaults else "",
            "raw": None,
        }
        for source, layer in sources:
            if key not in layer:
                continue
            value = layer[key]
            if value is None:  # 置空 == 不覆盖，回落到上一层（与插件同规则）
                continue
            chosen = {"value": _typed(value, spec), "source": source, "raw": value}
        out[key] = chosen
    return out


# ---------------------------------------------------------------------------
# A. 设置面板
# ---------------------------------------------------------------------------
def _group_of(key: str) -> str:
    for group_id, keys in GROUP_KEYS.items():
        if key in keys:
            return group_id
    return OTHER_GROUP


def _choice_labels(spec: Mapping[str, Any]) -> List[Dict[str, str]]:
    """``choices`` → ``[{value,label}]``。

    **choices 里有什么就列什么**（前端下拉直接照抄）；``choices_labels`` / ``choice_labels``
    这类可选映射若存在就用作人话标签，没有就用原值。
    """
    raw = spec.get("choices")
    if not isinstance(raw, (list, tuple)):
        return []
    labels = spec.get("choices_labels") or spec.get("choice_labels") or {}
    if not isinstance(labels, Mapping):
        labels = {}
    out: List[Dict[str, str]] = []
    for item in raw:
        if isinstance(item, bool) or item is None:
            continue
        value = str(item)
        label = labels.get(item, labels.get(value))
        out.append({"value": value, "label": str(label) if label else value})
    return out


def _settings_fields(agent: str, schema: Mapping[str, Mapping[str, Any]], effective: Mapping[str, Mapping[str, Any]], writable: Sequence[str]) -> List[Dict[str, Any]]:
    """把 ``config_schema`` 逐项翻成前端可渲染的字段（**不硬编码任何键名**）。"""
    fields: List[Dict[str, Any]] = []
    for key, spec in schema.items():
        current = effective.get(key) or {}
        declared = str(spec.get("type") or "").lower()
        bounds = BOUNDS.get(key)
        fields.append(
            {
                "key": key,
                "group": _group_of(key),
                "label": str(spec.get("label") or key),
                "type": declared or "str",
                "description": str(spec.get("description") or ""),
                "default": spec.get("default"),
                "choices": _choice_labels(spec),
                # 当前生效值 + 它来自哪一层（界面必须两样都显示）
                "value": current.get("value"),
                "source": current.get("source") or "",
                "source_label": SOURCE_LABEL.get(str(current.get("source") or ""), "未设置"),
                # 写入约束：前端先拦一次，后端再拦一次（两边都做）
                "min": bounds[0] if bounds else None,
                "max": bounds[1] if bounds else None,
                "range_hint": bounds[2] if bounds else "",
                # max_title_length 的两个合法区段是 0 与 10..100，标准 min/max 表达不了
                "special": "zero_or_range" if key == "max_title_length" else "",
                "writable": key in writable,
                "needs_restart": key in RESTART_KEYS,
                "pinned": key in PINNED_KEYS,
                "multiline": declared == "str" and ("instructions" in key or "prompt" in key),
            }
        )
    return fields


def _settings_view(agent: str) -> Dict[str, Any]:
    """``GET /autotitler/settings`` 的 ``data``。"""
    schema = _config_schema(agent)
    effective = _effective(agent, schema) if schema else {}
    settings_mod = _core("settings")
    switch_mod = _core("plugin_switch")
    scope = _core("scope")

    config_path = Path(scope.agent_config_path(agent))
    try:
        config_exists = config_path.is_file()
    except OSError:
        config_exists = False

    # 可写键 = schema 里没标只读的全部键（**不看文件里写过什么**：新建 profile 也要能配）
    writable = sorted(key for key in schema if key not in _readonly_keys(schema))
    if not config_exists:
        writable = []

    state = {}
    try:
        state = dict(switch_mod.plugin_state(agent, PLUGIN_ID, tri_state=True))
    except Exception as exc:  # noqa: BLE001 - 三态算不出来不影响读参数
        logger.warning("hermes-console.autotitler: plugin_state 失败：%s", exc)

    notes: List[str] = []
    notes.append(
        "优先级（高 → 低）：该 agent 的宿主 settings > 全局 settings > 插件 config.yaml > "
        "插件默认值。面板写入落在**宿主 settings**（plugins.entries.%s.settings），"
        "插件目录的 config.yaml 一个字节都不动。" % PLUGIN_ID
    )
    restart = [key for key in writable if key in RESTART_KEYS]
    if restart:
        notes.append(
            "这些键改了需要重启 Hermes 才生效：%s（插件的 config_schema 明写 " "restart）。"
            % "、".join(restart)
        )
    if not config_exists:
        notes.append(
            "该 agent 没有自己的 config.yaml（%s）：读得出值（来自更低层），但写不进去。"
            "要改就先给这个 profile 建配置，或切到 default agent 改全局层。" % config_path
        )
    if not schema:
        notes.append(
            "没读到插件 manifest 的 config_schema：插件可能没安装在这个 home 下，"
            "参数清单为空（不是错误）。"
        )

    return {
        "agent": agent,
        "plugin_id": PLUGIN_ID,
        "plugin_dir": str(_plugin_dir(agent) or ""),
        "version": _plugin_version(agent),
        "declared_hooks": _declared_hooks(agent),
        "state": state.get("state") or "",
        "in_enabled": bool(state.get("in_enabled")),
        "in_disabled": bool(state.get("in_disabled")),
        "schema_keys": sorted(schema),
        "field_count": len(schema),
        "fields": _settings_fields(agent, schema, effective, writable) if schema else [],
        "groups": [
            {"id": group_id, "label": label, "keys": [k for k in keys if k in schema]}
            for group_id, label, keys in GROUPS
        ]
        + (
            [
                {
                    "id": OTHER_GROUP,
                    "label": "其它（上游新增）",
                    "keys": sorted(k for k in schema if _group_of(k) == OTHER_GROUP),
                }
            ]
            if any(_group_of(k) == OTHER_GROUP for k in schema)
            else []
        ),
        "writable_keys": writable,
        "readonly_keys": _readonly_keys(schema),
        "restart_keys": list(RESTART_KEYS),
        # 用户点名的四件事（置顶展示；字段本身仍在 fields 里，语义分组里不重复渲染）
        "pinned": [
            {
                "id": pinned_id,
                "label": pinned_label,
                "hint": pinned_hint,
                "keys": [key for key in keys if key in schema],
            }
            for pinned_id, pinned_label, pinned_hint, keys in PINNED
            if any(key in schema for key in keys)
        ],
        "path": str(config_path),
        "exists": config_exists,
        "config_digest": _config_digest(agent),
        "source_labels": dict(SOURCE_LABEL),
        "notes": notes,
    }


def _readonly_keys(schema: Mapping[str, Mapping[str, Any]]) -> List[str]:
    out: List[str] = []
    for key, spec in schema.items():
        if any(spec.get(flag) is True for flag in ("readonly", "read_only", "readOnly")):
            out.append(str(key))
        elif spec.get("writable") is False:
            out.append(str(key))
    return sorted(out)


def _config_digest(agent: str) -> str:
    """该 agent ``config.yaml`` 的字节摘要（PUT 的乐观锁令牌；与官方 ``_digest`` 同值）。"""
    try:
        switch_mod = _core("plugin_switch")
        digest_fn = getattr(switch_mod, "_config_file_digest", None)
        if callable(digest_fn):
            return str(digest_fn(agent))
    except Exception:  # noqa: BLE001 - 兜底路径更简单
        pass
    path = Path(_core("scope").agent_config_path(agent))
    try:
        from hermes_cli.runtime_state import _digest as official_digest

        return str(official_digest(path) or "missing")
    except Exception:  # noqa: BLE001
        try:
            import hashlib

            return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return "missing"


# ---------------------------------------------------------------------------
# A2. 值校验（前端拦一次 + 后端拦一次，两边都做）
# ---------------------------------------------------------------------------
def _coerce_input(key: str, spec: Mapping[str, Any], raw: Any) -> Any:
    """把前端提交的值按 schema 类型归一化；不合法一律 :class:`ApiError(400)`。"""
    declared = str(spec.get("type") or "").lower()

    if declared == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)) and raw in (0, 1):
            return bool(raw)
        if isinstance(raw, str):
            text = raw.strip().lower()
            if text in ("true", "1", "yes", "on"):
                return True
            if text in ("false", "0", "no", "off"):
                return False
        raise ApiError(
            400, CODE_VALUE_REJECTED,
            "%s 是布尔开关，只接受 true/false（收到 %r）" % (key, raw), key=key,
        )

    if declared in ("int", "float"):
        if isinstance(raw, bool):
            raise ApiError(
                400, CODE_VALUE_REJECTED,
                "%s 是数字，不接受 true/false（收到 %r）" % (key, raw), key=key,
            )
        try:
            number = float(raw) if declared == "float" else int(str(raw).strip() if isinstance(raw, str) else raw)
        except (TypeError, ValueError):
            raise ApiError(
                400, CODE_VALUE_REJECTED,
                "%s 是%s，收到 %r 不能转成数字"
                % (key, "小数" if declared == "float" else "整数", raw),
                key=key,
            )
        if declared == "float":
            import math

            if not math.isfinite(number):
                raise ApiError(400, CODE_VALUE_REJECTED, "%s 必须是有限数字（收到 %r）" % (key, raw), key=key)
        return number

    if declared == "str":
        if raw is None:
            return ""
        if isinstance(raw, (dict, list)):
            raise ApiError(
                400, CODE_VALUE_REJECTED,
                "%s 是文本项，不接受对象/数组（收到 %s）" % (key, type(raw).__name__), key=key,
            )
        return str(raw)

    return raw


def _check_choices(key: str, spec: Mapping[str, Any], value: Any) -> None:
    choices = _choice_labels(spec)
    if not choices:
        return
    allowed = [item["value"] for item in choices]
    if str(value) not in allowed:
        raise ApiError(
            400, CODE_VALUE_REJECTED,
            "%s 只能是 %s 之一（收到 %r）" % (key, " / ".join(allowed), value),
            key=key, choices=allowed,
        )


def _check_bounds(key: str, value: Any) -> None:
    """数值边界。``max_title_length`` 的两个区段是 0 与 10..100，单独处理。"""
    if key == "max_title_length":
        number = int(value)
        if number == 0:
            return
        if number < 0:
            raise ApiError(
                400, CODE_VALUE_REJECTED,
                "max_title_length 不能是负数：0 表示不限制，要限制请填 10..100（收到 %d）" % number,
                key=key, min=10, max=100,
            )
        if number < 10 or number > 100:
            raise ApiError(
                400, CODE_VALUE_REJECTED,
                "max_title_length 的合法区段只有「0（不限制）」与「10..100」："
                "填 %d 会越界。插件自己的规则是正数最小 10、最大 100 —— "
                "要么填 0 关掉硬截断，要么填 10 及以上的值" % number,
                key=key, min=10, max=100,
            )
        return

    bounds = BOUNDS.get(key)
    if bounds is None:
        return
    low, high, hint = bounds
    number = float(value)
    if low is not None and number < low:
        raise ApiError(
            400, CODE_VALUE_REJECTED,
            "%s 超出范围：%s（收到 %r）" % (key, hint, value), key=key, min=low, max=high,
        )
    if high is not None and number > high:
        raise ApiError(
            400, CODE_VALUE_REJECTED,
            "%s 超出范围：%s（收到 %r）" % (key, hint, value), key=key, min=low, max=high,
        )


def _check_model_pair(agent: str, key: str, value: Any, schema: Mapping[str, Any]) -> None:
    """``provider → model`` 两级联动的服务端闸门（t_4d654faf）。

    只拦**写入模型名**这一侧（``title_model`` / ``model``）：它必须属于**当前生效的**
    provider。写 ``provider`` 本身不拦（单键写入口一次只能写一个键，此刻旧 model 还是旧
    provider 的合法值；界面在换 provider 时会先把 model 清空，并在保存前调
    ``GET /models/validate`` 复核成对值）。

    拿不到 ``api_models`` / 读不到该 provider 的模型清单 ⇒ 放行（fail-open）。
    """
    if key not in ("title_model", "model"):
        return
    model = str(value or "").strip()
    if not model:
        return
    try:
        import api_models  # 同目录兄弟模块（plugin_api 已把本目录插进 sys.path）
    except Exception:  # noqa: BLE001
        return
    try:
        effective = _effective(agent, schema) if schema else {}
    except Exception:  # noqa: BLE001
        return
    provider = str(((effective.get("provider") or {}).get("value")) or "").strip()
    problem = api_models.model_pair_error(agent, provider, model)
    if problem:
        raise ApiError(400, CODE_INVALID_ARGUMENT, problem,
                       key=key, provider=provider, model=model,
                       hint="换 provider 后旧模型名不属于新 provider：先选 provider，再从它"
                            "的模型清单里选；要用清单外的名字请走「高级：手填模型名」")


def _do_put(agent: str, payload: Any) -> Dict[str, Any]:
    """写单项：校验 → 乐观锁 → 官方写入器 → 独立回读断言。

    落点固定 ``plugins.entries.hermes-auto-titler.settings.<key>``；
    **不碰**插件目录的 ``config.yaml``（那是 ``/autotitler config`` 的落点，本卡明确不许）。
    """
    if not isinstance(payload, Mapping):
        raise ApiError(
            400, CODE_INVALID_ARGUMENT,
            "请求体必须是 JSON 对象 {key, value, expected_config?}（收到 %s）"
            % type(payload).__name__,
        )
    key = str(payload.get("key") or "").strip()
    if not key:
        raise ApiError(400, CODE_INVALID_ARGUMENT, "缺少 key：要写哪一项参数？")
    if "value" not in payload:
        raise ApiError(400, CODE_INVALID_ARGUMENT, "缺少 value：要写成什么？")
    expected = payload.get("expected_config")

    schema = _config_schema(agent)
    if not schema:
        raise ApiError(
            503, CODE_UPSTREAM_ERROR,
            "没读到插件 manifest 的 config_schema：无法判断 %r 是否合法" % key,
        )
    spec = schema.get(key)
    if spec is None:
        raise ApiError(
            400, CODE_UNKNOWN_SETTING_KEY,
            "未知参数 %r：本插件 manifest 没有声明这项（可写键见 writable_keys）。"
            "本端点只写 config_schema 里声明过的键，否则会在配置里造出没人读的孤儿键" % key,
            key=key, writable_keys=sorted(schema),
        )
    if key in _readonly_keys(schema):
        raise ApiError(400, CODE_READONLY_SETTING, "%s 被 manifest 标为只读，拒绝写入" % key, key=key)

    scope = _core("scope")
    config_path = Path(scope.agent_config_path(agent))
    try:
        config_exists = config_path.is_file()
    except OSError:
        config_exists = False
    if not config_exists:
        raise ApiError(
            400, CODE_CONFIG_MISSING,
            "该 agent 没有自己的 config.yaml（%s）：写不进去。请先建该 profile 的配置，"
            "或改到 default agent 改全局层" % config_path,
            agent=agent, path=str(config_path),
        )

    value = _coerce_input(key, spec, payload.get("value"))
    _check_choices(key, spec, value)
    _check_bounds(key, value)
    _check_model_pair(agent, key, value, schema)

    digest_before = _config_digest(agent)
    if expected is not None and str(expected) != digest_before:
        raise ApiError(
            409, CODE_CONFIG_CHANGED,
            "配置已变化（乐观锁生效）：提交的 expected_config=%r，当前 %s。本次未写入任何内容，"
            "请重新读取后重试" % (expected, digest_before),
            # 字段名说人话：这里断言的是「本次一个字节都没写」，不是「值没变」。
            # 旧名 unchanged 会被人读成「值没变」，与事实相反（值恰恰变了）。
            expected_config=expected, actual_config=digest_before, nothing_written=True,
        )

    settings_mod = _core("settings")
    try:
        result = settings_mod.save_plugin_setting_for(agent, PLUGIN_ID, (key,), value)
    except settings_mod.PluginSettingsWriteError as exc:
        raise ApiError(
            500, CODE_WRITE_NOT_APPLIED, "写入未生效：%s" % exc, key=key, path=str(config_path)
        ) from exc
    except PermissionError as exc:
        raise ApiError(400, CODE_READONLY_SETTING, "该设置被托管/管理员键保护，拒绝写入：%s" % exc, key=key) from exc
    except ValueError as exc:
        raise ApiError(400, CODE_INVALID_ARGUMENT, "写入被拒绝：%s" % exc, key=key) from exc

    # 独立回读断言：不信写函数的返回值，磁盘上的值才算。
    try:
        actual = settings_mod.read_plugin_settings(agent, PLUGIN_ID).get(key)
    except Exception as exc:  # noqa: BLE001
        raise ApiError(
            500, CODE_WRITE_NOT_APPLIED, "写入后回读失败，无法确认生效：%s: %s" % (type(exc).__name__, exc), key=key
        ) from exc
    if type(actual) is not type(value) or actual != value:
        raise ApiError(
            500, CODE_WRITE_NOT_APPLIED,
            "写入返回成功但回读不一致（期望 %r/%s，实际 %r/%s）：已中止，请人工核对 %s"
            % (value, type(value).__name__, actual, type(actual).__name__, config_path),
            key=key,
        )

    # 写后重算生效视图：界面据此显示「新值 + 它现在来自哪一层」。
    effective = _effective(agent, schema).get(key) or {}
    return {
        "agent": agent,
        "plugin_id": PLUGIN_ID,
        "key": key,
        "value": value,
        "previous": (result or {}).get("previous"),
        "verified": True,
        "source": effective.get("source") or "",
        "source_label": SOURCE_LABEL.get(str(effective.get("source") or ""), ""),
        "effective_value": effective.get("value"),
        "needs_restart": key in RESTART_KEYS,
        "restart_hint": (
            "已保存到宿主 settings。%s 是插件加载期读取的开关，**需要重启 Hermes 才生效**" % key
            if key in RESTART_KEYS
            else "已保存到宿主 settings，即时生效（插件每轮评估都重读配置）"
        ),
        "plugin_config_untouched": str(_plugin_dir(agent) or ""),
        "config_digest": _config_digest(agent),
    }


# ---------------------------------------------------------------------------
# HTTP 路由：设置
# ---------------------------------------------------------------------------
@router.get("/autotitler/settings")
def get_autotitler_settings(agent: Optional[str] = None) -> Any:
    """按插件 ``config_schema`` 动态列出全部参数（类型/默认值/说明/choices/来源层）。

    ``GET /autotitler/settings?agent=code-worker`` →
    ``{"ok": true, "data": {agent, plugin_id, version, declared_hooks, field_count,
    fields:[{key,group,label,type,description,default,value,source,source_label,min,max,
    choices,writable,needs_restart}], groups:[...], writable_keys, path, config_digest, notes}}``

    缺省 ``agent`` ⇒ ``default``；非法 ``agent`` ⇒ 400 + 合法 id 列表。
    """
    try:
        agent_id = _prepare_agent(agent)
        return {"ok": True, "data": _settings_view(agent_id)}
    except ApiError as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("hermes-console.autotitler: 读参数失败 agent=%r", agent)
        return _fail(500, CODE_UPSTREAM_ERROR, "读参数失败：%s: %s" % (type(exc).__name__, exc))


@router.put("/autotitler/settings")
async def put_autotitler_setting(request: Request, agent: Optional[str] = None) -> Any:
    """写单项：``{"key": ..., "value": ..., "expected_config": "可选"}``，``?agent=`` 定作用域。"""
    try:
        agent_id = _prepare_agent(agent)
    except ApiError as exc:
        return _error_response(exc)

    raw = await request.body()
    if not raw or not raw.strip():
        return _fail(400, CODE_INVALID_ARGUMENT, "请求体为空：需要一个 JSON 对象 {key, value}")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        return _fail(400, CODE_INVALID_ARGUMENT, "请求体不是合法 JSON：%s: %s" % (type(exc).__name__, exc))

    try:
        data = await run_in_threadpool(_do_put, agent_id, payload)
        return {"ok": True, "data": data}
    except ApiError as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("hermes-console.autotitler: 写参数失败 agent=%r", agent)
        return _fail(500, CODE_UPSTREAM_ERROR, "写参数失败：%s: %s" % (type(exc).__name__, exc))


def _prepare_agent(raw: Any) -> str:
    """归一化 agent id；非法 → 400 + 合法 id 列表（不是 500）。"""
    scope = _core("scope")
    try:
        return scope.normalize_agent_id(raw)
    except ValueError as exc:
        try:
            agents = scope.list_agent_ids()
        except Exception:  # noqa: BLE001
            agents = []
        raise ApiError(400, CODE_INVALID_ARGUMENT, str(exc), agents=agents) from exc


# ---------------------------------------------------------------------------
# B. 监控 —— 日志扫描（只读）
#
# 口径写在响应里（``health.files`` + ``health.window``），探针/脚本能拿同一份文件清单
# 去 grep 对账，不用猜"面板到底读了哪几个文件"。
# ---------------------------------------------------------------------------
_LOG_CACHE: Dict[Tuple[str, int, int, int], Dict[str, Any]] = {}
_LOG_CACHE_LOCK = __import__("threading").Lock()
_LOG_CACHE_MAX = 24


def _logs_dir(agent: str) -> Path:
    """该 agent 的日志目录：``default`` → ``<home>/logs``；其余 → ``<home>/profiles/<id>/logs``。"""
    return Path(_core("scope").agent_home(agent)) / "logs"


def _log_files(agent: str, max_files: int) -> List[Path]:
    """``agent.log`` + 轮转 ``agent.log.1..N``（存在才列；按**旧 → 新**排序）。

    顺序很重要：解析是顺序扫描的（"上一条 WARNING 归因给下一条 audit"），把新文件排在前面
    会让跨文件的因果配错。
    """
    directory = _logs_dir(agent)
    current = directory / "agent.log"
    files: List[Path] = []
    if max_files <= 0:
        return [current] if _is_file(current) else []
    for index in range(int(max_files), 0, -1):
        candidate = directory / ("agent.log.%d" % index)
        if _is_file(candidate):
            files.append(candidate)
    if _is_file(current):
        files.append(current)
    return files


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _read_tail(path: Path, limit: int) -> Tuple[List[str], Dict[str, Any]]:
    """读文件尾部 ``limit`` 字节并按行切；返回 ``(lines, meta)``。

    从尾部读是为了让一个 GET 稳定在百毫秒级：agent.log 可能几 MB，整读会把面板拖死。
    截断时**丢掉第一行**（它多半是半行），并把 ``truncated=True`` 写进 meta ——
    计数口径要能被脚本复现，所以截断这件事必须显式。
    """
    meta: Dict[str, Any] = {"path": str(path), "bytes": 0, "truncated": False}
    try:
        size = path.stat().st_size
    except OSError as exc:
        meta["error"] = str(exc)
        return [], meta
    meta["size"] = size
    start = max(0, size - int(limit))
    meta["truncated"] = start > 0
    try:
        with open(path, "rb") as handle:
            if start:
                handle.seek(start)
            blob = handle.read()
    except OSError as exc:
        meta["error"] = str(exc)
        return [], meta
    meta["bytes"] = len(blob)
    text = blob.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start and lines:
        lines = lines[1:]
    return lines, meta


def _parse_logs(files: Sequence[Path], limit: int) -> Dict[str, Any]:
    """把若干日志文件解析成计数 + 失败明细。

    关键点：插件的 audit 行**永远**写 ``reason=model call failed``，真正的根因在它前面那条
    WARNING（``model returned invalid decision payload``）。所以这里按 **sid 前缀**把
    WARNING 暂存起来，等该会话的 audit ``act=failed`` 出现时消费掉 —— 否则 93 次
    invalid payload 会被 94 次 "model call failed" 完全吞掉，面板就等于没说话。
    """
    counts: Dict[str, int] = {}
    classes: Dict[str, int] = {}
    skip_reasons: Dict[str, int] = {}
    failures: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []       # 每一条 audit 行（带时间戳，供时间窗口/日志页用）
    invalid_events: List[Dict[str, Any]] = []
    pending: Dict[str, str] = {}      # sid8 -> 失败分类
    attempts: Dict[str, int] = {}     # sid8 -> 最近一次 attempt
    meta_files: List[Dict[str, Any]] = []
    invalid_payload_total = 0
    lines_seen = 0
    last_ts = ""
    registered: Optional[Dict[str, Any]] = None

    for path in files:
        lines, meta = _read_tail(path, limit)
        meta_files.append(meta)
        for line in lines:
            if "auto-titler" not in line and "hermes-auto-titler" not in line:
                continue
            lines_seen += 1
            stamp = _LOG_TS_RE.match(line)
            if stamp:
                last_ts = "%s.%s" % (stamp.group(1), stamp.group(2))

            if registered is None:
                coexist = _COEXIST_RE.search(line)
                if coexist:
                    registered = {"first_title_mode": coexist.group(1), "ts": last_ts}
                    continue

            match = _AUDIT_RE.search(line)
            if match:
                sid, act = match.group(1), match.group(2)
                reason = (match.group(3) or "").strip()
                dur = int(match.group(4) or 0)
                counts[act] = counts.get(act, 0) + 1
                event: Dict[str, Any] = {
                    "ts": last_ts,
                    "epoch": _ts_to_epoch(last_ts),
                    "sid": sid,
                    "act": act,
                    "reason": reason,
                    "dur_ms": dur,
                    "file": str(path),
                }
                if act == "failed":
                    klass = pending.pop(sid, "") or _classify_reason(reason)
                    classes[klass] = classes.get(klass, 0) + 1
                    event["class"] = klass
                    failures.append(
                        {
                            "session_id": sid,
                            "class": klass,
                            "class_label": _CLASS_LABEL.get(klass, klass),
                            "reason": reason or "model call failed",
                            "dur_ms": dur,
                            "attempts": attempts.get(sid, 0),
                            "ts": last_ts,
                        }
                    )
                elif act in ("skipped", "throttled"):
                    skip_reasons[reason or "(未注明)"] = skip_reasons.get(reason or "(未注明)", 0) + 1
                events.append(event)
                continue

            match = _INVALID_PAYLOAD_RE.search(line)
            if match:
                sid = match.group(1)[:8]
                pending[sid] = "invalid_decision_payload"
                invalid_payload_total += 1
                invalid_events.append({"ts": last_ts, "epoch": _ts_to_epoch(last_ts), "sid": sid})
                continue

            match = _RECORDED_FAILURE_RE.search(line)
            if match:
                attempts[match.group(1)[:8]] = int(match.group(2))
                if not pending.get(match.group(1)[:8]):
                    pending[match.group(1)[:8]] = "model_call_error"
                continue

            match = _LLM_RESULT_RE.search(line)
            if match and match.group(2) == "error":
                sid = match.group(1)[:8]
                pending.setdefault(sid, "model_call_error")
                continue

    return {
        "counts": counts,
        "classes": classes,
        "skip_reasons": skip_reasons,
        "failures": failures,
        "events": events,
        "invalid_events": invalid_events,
        "attempts": dict(attempts),
        "files": meta_files,
        "invalid_payload_total": invalid_payload_total,
        "lines_seen": lines_seen,
        "last_ts": last_ts,
        "registered": registered,
    }


_CLASS_LABEL = {
    "invalid_decision_payload": "模型返回散文而非 JSON 决策载荷",
    "model_call_error": "模型调用报错（非决策载荷问题）",
    "other": "其它原因",
}

def _classify_reason(reason: str) -> str:
    text = (reason or "").lower()
    if "invalid decision payload" in text:
        return "invalid_decision_payload"
    if "model call failed" in text or "error" in text:
        return "model_call_error"
    return "other"


def _scan_logs(agent: str, max_files: int, limit: int) -> Dict[str, Any]:
    """带缓存的日志扫描：文件 ``(路径, size, mtime_ns)`` 不变就复用上次结果。"""
    files = _log_files(agent, max_files)
    signature: List[Tuple[str, int, int, int]] = []
    for path in files:
        try:
            stat = path.stat()
            signature.append((str(path), stat.st_size, stat.st_mtime_ns, int(limit)))
        except OSError:
            continue
    key = (agent, int(max_files), int(limit), hash(tuple(signature)))
    with _LOG_CACHE_LOCK:
        cached = _LOG_CACHE.get(key)
    if cached is not None:
        return cached
    result = _parse_logs(files, limit)
    with _LOG_CACHE_LOCK:
        if len(_LOG_CACHE) > _LOG_CACHE_MAX:
            _LOG_CACHE.clear()
        _LOG_CACHE[key] = result
    return result


# ---------------------------------------------------------------------------
# B2. 监控 —— state.json（只读快照）
# ---------------------------------------------------------------------------
def _state_path(agent: str) -> Path:
    """插件运行态：``<agent_home>/plugin-data/hermes-auto-titler/state.json``。"""
    return Path(_core("scope").agent_home(agent)) / "plugin-data" / PLUGIN_ID / "state.json"


def _read_state(agent: str) -> Dict[str, Any]:
    """读 ``state.json``（**只读，绝不写**）；坏掉/不存在都 fail-soft 并给原因。"""
    path = _state_path(agent)
    meta: Dict[str, Any] = {"path": str(path), "exists": False}
    try:
        stat = path.stat()
        meta.update({"exists": True, "size": stat.st_size, "mtime": stat.st_mtime})
    except OSError:
        return {"meta": meta, "sessions": {}, "error": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 读坏了要说清，不是 500
        meta["error"] = "%s: %s" % (type(exc).__name__, exc)
        return {"meta": meta, "sessions": {}, "error": str(meta["error"])}
    sessions = data.get("sessions") if isinstance(data, Mapping) else None
    if not isinstance(sessions, Mapping):
        meta["error"] = "state.json 的 sessions 不是对象（版本不兼容？）"
        return {"meta": meta, "sessions": {}, "error": str(meta["error"])}
    return {"meta": meta, "sessions": dict(sessions), "error": ""}


def _kind_parts(kind: Any) -> List[str]:
    """``kind`` 可能是复合值（``finalize+retry`` / ``pending+counter``），拆开分别计数。"""
    if not isinstance(kind, str) or not kind.strip():
        return ["counter"]
    return [part for part in kind.split("+") if part]


def _state_summary(state: Mapping[str, Any]) -> Dict[str, Any]:
    """state.json → 概览 + 重试队列 + 每会话明细。"""
    sessions = state.get("sessions") or {}
    kind_counts: Dict[str, int] = {}
    raw_kind_counts: Dict[str, int] = {}
    rename_counts: Dict[str, int] = {}
    queue: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []

    for sid, record in sessions.items():
        if not isinstance(record, Mapping):
            continue
        kind = record.get("kind")
        raw_kind = str(kind or "counter")
        raw_kind_counts[raw_kind] = raw_kind_counts.get(raw_kind, 0) + 1
        for part in _kind_parts(kind):
            kind_counts[part] = kind_counts.get(part, 0) + 1

        try:
            rename_count = int(record.get("rename_count") or 0)
        except (TypeError, ValueError):
            rename_count = 0
        bucket = str(rename_count)
        rename_counts[bucket] = rename_counts.get(bucket, 0) + 1

        for block_name in ("finalize", "retry"):
            block = record.get(block_name)
            if not isinstance(block, Mapping):
                continue
            queue.append(
                {
                    "session_id": str(sid),
                    "block": block_name,
                    "kind": raw_kind,
                    "attempts": _as_int(block.get("attempts")),
                    "class_attempts": _as_int(block.get("class_attempts")),
                    "retry_class": str(block.get("retry_class") or ""),
                    "next_retry_at": _as_float(block.get("next_retry_at")),
                    "queued_at": _as_float(block.get("queued_at")),
                    "reason": str(block.get("reason") or ""),
                    "capacity": bool(block.get("capacity")),
                    "close_epoch": _as_int(block.get("close_epoch")),
                    "base_title": record.get("base_title"),
                }
            )

        pending_review = record.get("pending_review")
        rows.append(
            {
                "session_id": str(sid),
                "base_title": record.get("base_title"),
                "base_title_known": bool(record.get("base_title_known", True)),
                "expected_title": record.get("expected_title"),
                "kind": raw_kind,
                "rename_count": rename_count,
                "pending_candidate": (
                    pending_review.get("candidate")
                    if isinstance(pending_review, Mapping)
                    else None
                ),
                "pending_confirmations": (
                    _as_int(pending_review.get("confirmations"))
                    if isinstance(pending_review, Mapping)
                    else 0
                ),
                "has_finalize": isinstance(record.get("finalize"), Mapping),
                "has_retry": isinstance(record.get("retry"), Mapping),
            }
        )

    rows.sort(key=lambda item: (item["rename_count"], item["session_id"]), reverse=True)
    queue.sort(key=lambda item: item["next_retry_at"] or 0.0)
    next_times = [item["next_retry_at"] for item in queue if item["next_retry_at"]]
    finalize_rows = [item for item in queue if item["block"] == "finalize"]
    retry_rows = [item for item in queue if item["block"] == "retry"]
    return {
        "sessions_total": len(rows),
        "kind_counts": kind_counts,
        "raw_kind_counts": raw_kind_counts,
        "rename_counts": rename_counts,
        "queue": queue,
        "queue_finalize": finalize_rows,
        "queue_retry": retry_rows,
        "queue_earliest": min(next_times) if next_times else None,
        "queue_latest": max(next_times) if next_times else None,
        "sessions": rows,
    }


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN 过滤


# ---------------------------------------------------------------------------
# B3a. 时间窗口：把「历史」和「当前」分开算
# ---------------------------------------------------------------------------
COUNT_KEYS: Tuple[str, ...] = ("renamed", "failed", "skipped", "keep", "pending", "throttled")


def _window_hours(window_id: str) -> Optional[float]:
    for wid, _label, hours in WINDOWS:
        if wid == window_id:
            return hours
    return None


def _select_events(events: Sequence[Mapping[str, Any]], since: Optional[float], until: Optional[float]) -> List[Mapping[str, Any]]:
    """按 epoch 区间筛选。没有时间戳的行（``epoch=None``）**不进任何窗口**，单独计数。"""
    out: List[Mapping[str, Any]] = []
    for event in events:
        epoch = event.get("epoch")
        if epoch is None:
            continue
        if since is not None and epoch < since:
            continue
        if until is not None and epoch > until:
            continue
        out.append(event)
    return out


def _count_acts(events: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for event in events:
        act = str(event.get("act") or "")
        counts[act] = counts.get(act, 0) + 1
    return counts


def _normalized_counts(raw: Mapping[str, int]) -> Dict[str, int]:
    """固定 7 个键（缺的补 0）+ ``other`` 兜住上游新增动作。"""
    out: Dict[str, int] = {key: int(raw.get(key, 0)) for key in COUNT_KEYS}
    out["other"] = int(sum(raw.values()) - sum(out.values()))
    out["total"] = int(sum(raw.values()))
    return out


def _window_view(
    events: Sequence[Mapping[str, Any]],
    invalid_events: Sequence[Mapping[str, Any]],
    attempts: Mapping[str, int],
    window_id: str,
    now_epoch: float,
) -> Dict[str, Any]:
    """一个时间区间的全部读数（计数 / 失败率 / 失败分类 / 跳过原因）。"""
    hours = _window_hours(window_id)
    label = WINDOW_LABELS.get(window_id, window_id)
    since = None if hours is None else now_epoch - hours * 3600.0
    selected = _select_events(events, since, now_epoch)
    counts = _normalized_counts(_count_acts(selected))

    renamed = counts["renamed"]
    failed = counts["failed"]
    decisions = renamed + failed

    failures: List[Dict[str, Any]] = []
    for event in selected:
        if event.get("act") != "failed":
            continue
        klass = str(event.get("class") or _classify_reason(str(event.get("reason") or "")))
        sid = str(event.get("sid") or "")
        failures.append(
            {
                "session_id": sid,
                "class": klass,
                "class_label": _CLASS_LABEL.get(klass, klass),
                "reason": str(event.get("reason") or "") or "model call failed",
                "dur_ms": int(event.get("dur_ms") or 0),
                "attempts": int(attempts.get(sid, 0)),
                "ts": str(event.get("ts") or ""),
                "epoch": event.get("epoch"),
            }
        )

    class_counts: Dict[str, int] = {}
    for item in failures:
        class_counts[item["class"]] = class_counts.get(item["class"], 0) + 1
    classes = [
        {
            "class": klass,
            "label": _CLASS_LABEL.get(klass, klass),
            "count": count,
            "share": (count / failed) if failed else None,
        }
        for klass, count in sorted(class_counts.items(), key=lambda item: -item[1])
    ]

    skip_counts: Dict[str, int] = {}
    for event in selected:
        if event.get("act") in ("skipped", "throttled"):
            reason = str(event.get("reason") or "") or "(未注明)"
            skip_counts[reason] = skip_counts.get(reason, 0) + 1
    skip_reasons = [
        {"reason": reason, "count": count}
        for reason, count in sorted(skip_counts.items(), key=lambda item: -item[1])
    ]

    invalid_in_window = _select_events(invalid_events, since, now_epoch)
    stamps = [str(event.get("ts") or "") for event in selected if event.get("ts")]

    return {
        "id": window_id,
        "label": label,
        "hours": hours,
        "since_epoch": since,
        "until_epoch": now_epoch,
        "from_ts": _epoch_to_ts(since) or (min(stamps) if stamps else ""),
        "to_ts": _epoch_to_ts(now_epoch),
        "counts": counts,
        "total": counts["total"],
        "decisions": decisions,
        "failure_rate": (failed / decisions) if decisions else None,
        "success_rate": (renamed / decisions) if decisions else None,
        "failure_classes": classes,
        "skip_reasons": skip_reasons,
        "invalid_payload_total": len(invalid_in_window),
        "first_event_ts": min(stamps) if stamps else "",
        "last_event_ts": max(stamps) if stamps else "",
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# B3b. 基线（修复点）：一条时间线说明「此前 N 次失败，此后 0 次」
# ---------------------------------------------------------------------------
def _side_summary(events: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    counts = _normalized_counts(_count_acts(events))
    decisions = counts["renamed"] + counts["failed"]
    stamps = [str(event.get("ts") or "") for event in events if event.get("ts")]
    return {
        "counts": counts,
        "total": counts["total"],
        "decisions": decisions,
        "failure_rate": (counts["failed"] / decisions) if decisions else None,
        "first_ts": min(stamps) if stamps else "",
        "last_ts": max(stamps) if stamps else "",
    }


def _baseline_view(events: Sequence[Mapping[str, Any]], explicit_epoch: Optional[float]) -> Dict[str, Any]:
    """基线（修复点）前后对比。

    默认基线 = **最后一次 ``act=failed`` 的时间**：它是「此后还有没有失败」这条判断的
    天然分界。也可以用 ``?baseline=`` 显式钉住真正的修复时刻（例如部署完成那一刻），
    这样「此前 N 次失败、此后 0 次」就是对着人记得的那个时间说的，而不是猜出来的。
    """
    dated = [event for event in events if event.get("epoch") is not None]
    if not dated:
        return {
            "available": False,
            "ts": "",
            "epoch": None,
            "source": "none",
            "source_label": "日志里没有带时间戳的 audit 行",
            "before": None,
            "after": None,
            "timeline": [],
        }

    failed_events = [event for event in dated if event.get("act") == "failed"]
    if explicit_epoch is not None:
        epoch = float(explicit_epoch)
        source = "explicit"
        source_label = "你指定的基线时间"
    elif failed_events:
        epoch = float(max(float(event["epoch"]) for event in failed_events))
        source = "last_failure"
        source_label = "最后一次失败（修复点候选）"
    else:
        epoch = float(min(float(event["epoch"]) for event in dated))
        source = "first_event"
        source_label = "日志里第一条 audit（没有失败记录）"

    before = [event for event in dated if float(event["epoch"]) <= epoch]
    after = [event for event in dated if float(event["epoch"]) > epoch]
    before_summary = _side_summary(before)
    after_summary = _side_summary(after)

    failed_after = after_summary["counts"]["failed"]
    headline = "基线之前失败 %d 次；基线之后失败 %d 次" % (
        before_summary["counts"]["failed"],
        failed_after,
    )
    if failed_after == 0 and after_summary["total"]:
        headline += "（此后 %d 次审计里零失败）" % after_summary["total"]

    return {
        "available": True,
        "ts": _epoch_to_ts(epoch),
        "epoch": epoch,
        "source": source,
        "source_label": source_label,
        "headline": headline,
        "before": before_summary,
        "after": after_summary,
        "last_failure_ts": _epoch_to_ts(
            max(float(event["epoch"]) for event in failed_events) if failed_events else None
        ),
        "timeline": _timeline_view(dated, baseline_epoch=epoch),
    }


def _timeline_view(events: Sequence[Mapping[str, Any]], baseline_epoch: Optional[float] = None) -> List[Dict[str, Any]]:
    """按时间分桶的事件计数（失败格用红色标出），用来一眼看出「什么时候不失败」。

    分桶宽度自适应：跨度 / 24，但不小于 1 小时 —— 日志只跨 40 分钟时不该画出 24 个空格子。
    """
    dated = [event for event in events if event.get("epoch") is not None]
    if not dated:
        return []
    epochs = [float(event["epoch"]) for event in dated]
    start, end = min(epochs), max(epochs)
    span = max(end - start, 1.0)
    size = max(TIMELINE_MIN_BUCKET_SECONDS, span / float(TIMELINE_MAX_BUCKETS))
    # 桶对齐到整点/整分钟，让人能对上日志时间
    size = float(int(size) if size >= 60 else 60)
    origin = start - (start % size)

    buckets: Dict[int, Dict[str, int]] = {}
    index = int((start - origin) // size)
    while origin + index * size <= end:
        buckets[index] = {}
        index += 1

    for event in dated:
        slot = int((float(event["epoch"]) - origin) // size)
        bucket = buckets.setdefault(slot, {})
        act = str(event.get("act") or "")
        bucket[act] = bucket.get(act, 0) + 1

    out: List[Dict[str, Any]] = []
    for slot in sorted(buckets):
        bucket_start = origin + slot * size
        counts = _normalized_counts(buckets[slot])
        out.append(
            {
                "start_epoch": bucket_start,
                "start_ts": _epoch_to_ts(bucket_start),
                "end_ts": _epoch_to_ts(bucket_start + size),
                "bucket_seconds": int(size),
                "counts": counts,
                "total": counts["total"],
                "failed": counts["failed"],
                "renamed": counts["renamed"],
                "before_baseline": (
                    None if baseline_epoch is None else bucket_start < baseline_epoch
                ),
            }
        )
    return out


def _diagnosis_for(view: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """某个区间的失败诊断（只在「这个区间里真有失败」时给）。"""
    failed = int((view.get("counts") or {}).get("failed", 0))
    invalid_total = int(view.get("invalid_payload_total") or 0)
    if not failed or not invalid_total:
        return None
    reasons = list(view.get("failure_classes") or [])
    dominant = reasons[0]["class"] if reasons else ""
    return {
        "dominant_class": dominant,
        "invalid_payload_total": invalid_total,
        "headline": "模型返回的是散文而不是 JSON 决策载荷（%d 次）" % invalid_total,
        "detail": (
            "插件每轮让模型回一个 JSON 决策（rename / keep / approve …），但模型回的是聊天散文，"
            "解析器拿不到决策就整轮作废：日志先记 "
            "`model returned invalid decision payload`，随后 audit 行只写 "
            "`reason=model call failed`。所以「失败 %d 次」的真正原因是这 %d 次载荷不合法，"
            "不是网络或配额。" % (failed, invalid_total)
        ),
        "where_to_fix": [
            "插件侧提示词：hermes_auto_titler/policy.py 的 system prompt（要求只回 JSON）",
            "插件侧解析：hermes_auto_titler/titler.py::_parse_decision（对散文的容错/重试）",
        ],
        "panel_ok": True,
        "note": (
            "这不是面板的读数问题：同一份日志里 %d 条 invalid payload 与 %d 条 act=failed "
            "能按会话 id 一一对上。要修的是插件的提示词/解析，不在本面板。" % (invalid_total, failed)
        ),
    }


# ---------------------------------------------------------------------------
# B3c. 健康度：把计数翻成「一眼看出它现在还好不好」
# ---------------------------------------------------------------------------
def _health_view(
    scan: Mapping[str, Any],
    recent: int,
    window_id: str = DEFAULT_WINDOW,
    baseline_epoch: Optional[float] = None,
    now_epoch: Optional[float] = None,
) -> Dict[str, Any]:
    """健康度：**先给当前区间，再给各区间对照**，最后给一条基线时间线。

    注意 ``counts`` / ``failure_rate`` 这些老字段仍然在顶层（向后兼容），但它们的口径
    现在是**选中区间**而不是整份日志 —— 响应里 ``window`` 写明是哪个区间。
    """
    now = float(now_epoch if now_epoch is not None else time.time())
    events = list(scan.get("events") or [])
    invalid_events = list(scan.get("invalid_events") or [])
    attempts = dict(scan.get("attempts") or {})

    selected_id = window_id if window_id in WINDOW_LABELS else DEFAULT_WINDOW

    def _public_window(view: Mapping[str, Any]) -> Dict[str, Any]:
        """给界面的单个区间视图：**自带最近失败明细与诊断**，切换区间不必再发请求。"""
        failures_in = list(view.get("failures") or [])
        recent_in = list(reversed(failures_in[-int(recent):])) if recent > 0 else []
        for item in recent_in:
            item.pop("epoch", None)
        public = {key: value for key, value in view.items() if key != "failures"}
        public["recent_failures"] = recent_in
        public["diagnosis"] = _diagnosis_for(view)
        return public

    windows = {
        wid: _public_window(_window_view(events, invalid_events, attempts, wid, now))
        for wid, _label, _hours in WINDOWS
    }
    selected = windows[selected_id]

    recent_failures = selected["recent_failures"]
    diagnosis = selected["diagnosis"]
    invalid_total = int(selected["invalid_payload_total"])

    baseline = _baseline_view(events, baseline_epoch)
    undated = sum(1 for event in events if event.get("epoch") is None)

    return {
        # —— 老字段（= 选中区间口径，向后兼容）——
        "counts": dict(selected["counts"]),
        "decisions": selected["decisions"],
        "failure_rate": selected["failure_rate"],
        "success_rate": selected["success_rate"],
        "failure_classes": list(selected["failure_classes"]),
        "skip_reasons": list(selected["skip_reasons"]),
        "invalid_payload_total": invalid_total,
        "recent_failures": recent_failures,
        "diagnosis": diagnosis,
        "files": list(scan.get("files") or []),
        "lines_seen": int(scan.get("lines_seen", 0)),
        "last_audit_at": scan.get("last_ts") or "",
        # —— 新区段：区间对照 + 基线 + 时间线 ——
        "window": selected,
        "windows": windows,
        "window_order": [wid for wid, _label, _hours in WINDOWS],
        "baseline": baseline,
        "timeline": baseline.get("timeline") or [],
        "generated_at": _epoch_to_ts(now),
        "now_epoch": now,
        "undated_events": undated,
    }


# ---------------------------------------------------------------------------
# B4. 概览（运行态 + 健康度 + 重试队列 + 每会话明细）
# ---------------------------------------------------------------------------
def _overview_view(
    agent: str,
    recent: int,
    max_files: int,
    limit: int,
    window_id: str = DEFAULT_WINDOW,
    baseline_epoch: Optional[float] = None,
) -> Dict[str, Any]:
    schema = _config_schema(agent)
    effective = _effective(agent, schema) if schema else {}
    switch_mod = _core("plugin_switch")
    try:
        state = dict(switch_mod.plugin_state(agent, PLUGIN_ID, tri_state=True))
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes-console.autotitler: plugin_state 失败：%s", exc)
        state = {}

    scan = _scan_logs(agent, max_files, limit)
    health = _health_view(scan, recent, window_id=window_id, baseline_epoch=baseline_epoch)
    runtime_state = _read_state(agent)
    summary = _state_summary(runtime_state)

    enabled = effective.get("enabled") or {}
    plugin = {
        "id": PLUGIN_ID,
        "dir": str(_plugin_dir(agent) or ""),
        "version": _plugin_version(agent),
        "declared_hooks": _declared_hooks(agent),
        # 三态来自 core.plugin_switch（宿主两份名单），不是本层猜的
        "state": state.get("state") or "",
        "in_enabled": bool(state.get("in_enabled")),
        "in_disabled": bool(state.get("in_disabled")),
        # 运行期证据：插件 register() 打的 coexistence 行（没有就说明本次日志窗口里没加载）
        "registered": scan.get("registered"),
        "enabled_effective": enabled.get("value"),
        "enabled_source": enabled.get("source") or "",
        "enabled_source_label": SOURCE_LABEL.get(str(enabled.get("source") or ""), ""),
        "state_path": str(_state_path(agent)),
        "logs_dir": str(_logs_dir(agent)),
        "field_count": len(schema),
    }

    notes: List[str] = []
    if not plugin["dir"]:
        notes.append("插件目录没找到：这个 home 下没装 %s，所有读数都会是空的（不是面板坏了）。" % PLUGIN_ID)
    if plugin["state"] == "disabled":
        notes.append("宿主 disabled 名单里有它：hooks 不会被注册，日志里不会有新 audit 行。")
    elif plugin["state"] == "unavailable":
        notes.append(
            "宿主 enabled / disabled 两份名单里都没有它：按官方 loader 口径等于未启用。"
            "要它工作，先在某处把它列进 plugins.enabled。"
        )
    if runtime_state.get("error"):
        notes.append("state.json 读不了：%s（面板只读它，不会修）" % runtime_state["error"])
    if not runtime_state.get("meta", {}).get("exists"):
        notes.append("state.json 还不存在：插件没有正在跟踪的会话（新装或刚清理过）。")
    if health["counts"]["total"] == 0:
        notes.append(
            "选中区间（%s）里一条 auto-titler audit 都没有：插件可能没在跑，或这个 agent 的日志目录"
            "不是它写的那个；也可能只是这段时间没触发（切到「全部历史」对照）。"
            % (health.get("window", {}).get("label") or window_id)
        )
    notes.append(
        "计数口径：读 %s 下的 %s，每个文件从尾部最多 %d MB；响应里的 health.files 就是实际读的"
        "文件与字节数，可脚本对账。**所有计数都挂在时间区间上**：health.window 是选中区间，"
        "health.windows 是三个区间并列，health.baseline 是「修复点之前/之后」的对比。"
        % (
            plugin["logs_dir"],
            "agent.log（含轮转）" if max_files > 0 else "当前 agent.log",
            limit // (1024 * 1024),
        )
    )
    if health.get("undated_events"):
        notes.append(
            "有 %d 条 audit 行解析不出时间戳（多行日志被截断或格式变了）：它们不计入任何时间区间，"
            "只在「全部历史」的总数里。这不会让数字变好看，只会让区间读数偏小 —— 所以这里明写出来。"
            % int(health["undated_events"])
        )

    return {
        "agent": agent,
        "plugin": plugin,
        "window": health.get("window"),
        "health": health,
        "runtime": {
            "state_path": str(_state_path(agent)),
            "state_exists": bool(runtime_state.get("meta", {}).get("exists")),
            "state_mtime": runtime_state.get("meta", {}).get("mtime"),
            "state_error": runtime_state.get("error") or "",
            "sessions_total": summary["sessions_total"],
            "kind_counts": summary["kind_counts"],
            "raw_kind_counts": summary["raw_kind_counts"],
            "rename_counts": summary["rename_counts"],
            "sessions": summary["sessions"],
        },
        "retry": {
            "count": len(summary["queue"]),
            "finalize_count": len(summary["queue_finalize"]),
            "retry_count": len(summary["queue_retry"]),
            "earliest": summary["queue_earliest"],
            "latest": summary["queue_latest"],
            "entries": summary["queue"],
        },
        # 手动操作：本卡不伪造。面板进程没有插件实例，硬调会在后端进程里再造一个
        # titler（第二个写 state.json 的进程）——那是数据损坏，不是功能。
        "manual": {
            "available": False,
            "reason": (
                "手动重命名/重试需要插件运行态（AutoTitler 实例），它只在 agent 进程里。"
                "控制台后端另起一个实例会与网关里的实例并发写 state.json，本卡明确不这么做。"
            ),
            "session_command": "/autotitler rename-now <会话 id>",
            "queue_command": "/autotitler retitle-all --dry-run",
            "note": "把上面两条命令在对应会话里执行即可；面板只负责把该对哪个会话动手说清楚。",
        },
        "notes": notes,
    }


@router.get("/autotitler/overview")
def get_autotitler_overview(
    agent: Optional[str] = None,
    recent: int = 10,
    files: int = 0,
    limit: int = LOG_TAIL_BYTES,
    window: str = DEFAULT_WINDOW,
    baseline: Optional[str] = None,
) -> Any:
    """运行态 + 健康度 + 重试队列 + 每会话明细（全部只读）。

    ``GET /autotitler/overview?agent=default&recent=10&window=24h`` →
    ``{"ok": true, "data": {agent, plugin:{...}, window:{...}, health:{counts,failure_rate,
    failure_classes,recent_failures,diagnosis,files, window, windows, baseline, timeline,
    generated_at}, runtime:{...}, retry:{...}, manual:{...}, notes}}``

    ``window`` ∈ ``1h`` / ``24h`` / ``all``（缺省 ``all``，向后兼容整份日志口径）：
    **这是本面板最重要的一层** —— 插件曾经累计失败 109 次，修好之后失败归零，若只给一个
    累计失败率，面板会永远显示 98%，用户看到的就是「还在报错」。``health.windows`` 把三个
    区间并列返回，界面可以瞬间切换而不必再发请求。

    ``baseline``（epoch 秒或 ``YYYY-MM-DD HH:MM[:SS]``）可显式钉住修复时刻；不传则取
    **最后一次失败的时间**，据此给出「此前 N 次失败 / 此后 0 次」。

    ``files`` **缺省 0 = 只看当前 ``agent.log``**（这样计数与 ``grep`` 同一份文件对得上）；
    要连历史一起算就传 1..10 个轮转文件。``recent`` 是"最近几条失败明细"（0 = 不要明细）；
    ``limit`` 是每个文件从尾部读的字节数。
    """
    try:
        agent_id = _prepare_agent(agent)
        recent_n = max(0, min(int(recent), 200))
        files_n = max(0, min(int(files), 10))
        limit_n = max(64 * 1024, min(int(limit), 64 * 1024 * 1024))
        window_id = str(window or DEFAULT_WINDOW).strip().lower()
        if window_id not in WINDOW_LABELS:
            raise ApiError(
                400,
                CODE_INVALID_ARGUMENT,
                "window 只能是 %s 之一（收到 %r）" % (" / ".join(WINDOW_LABELS), window),
                window=window, windows=list(WINDOW_LABELS),
            )
        baseline_epoch = _parse_ts_arg(baseline)
        return {
            "ok": True,
            "data": _overview_view(agent_id, recent_n, files_n, limit_n, window_id, baseline_epoch),
        }
    except ApiError as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("hermes-console.autotitler: 读运行态失败 agent=%r", agent)
        return _fail(500, CODE_UPSTREAM_ERROR, "读运行态失败：%s: %s" % (type(exc).__name__, exc))


# ---------------------------------------------------------------------------
# B5. 运行日志（只读，脱敏）
# ---------------------------------------------------------------------------
def _clean_text(value: Any, limit: int) -> str:
    """脱敏：只保留单行文本并截断。会话正文/提示词**不会**出现在 audit 行里，但这里再兜一层。"""
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _logs_view(
    agent: str,
    act: Optional[str],
    sid: Optional[str],
    since: Optional[float],
    until: Optional[float],
    limit: int,
    max_files: int,
    order: str,
) -> Dict[str, Any]:
    """``GET /autotitler/logs`` 的 ``data``：audit 行（倒序）+ 过滤条件 + action 计数。

    只回 ``sid / act / reason / dur_ms / ts``：日志原文、会话正文、提示词一律不返回。
    本端点**只读**，没有删除/清空入口，也没有对应路由。
    """
    scan = _scan_logs(agent, max_files, LOG_TAIL_BYTES)
    events = list(scan.get("events") or [])

    wanted_acts: List[str] = []
    if act:
        wanted_acts = [item.strip() for item in str(act).split(",") if item.strip()]
    sid_needle = (sid or "").strip().lower()

    acts_all: Dict[str, int] = {}
    for event in events:
        key = str(event.get("act") or "")
        acts_all[key] = acts_all.get(key, 0) + 1

    matched: List[Mapping[str, Any]] = []
    for event in events:
        if wanted_acts and str(event.get("act") or "") not in wanted_acts:
            continue
        if sid_needle and sid_needle not in str(event.get("sid") or "").lower():
            continue
        epoch = event.get("epoch")
        if since is not None and (epoch is None or epoch < since):
            continue
        if until is not None and (epoch is None or epoch > until):
            continue
        matched.append(event)

    descending = str(order or "desc").lower() != "asc"
    if descending:
        matched = list(reversed(matched))

    rows: List[Dict[str, Any]] = []
    for event in matched[:limit]:
        rows.append(
            {
                "ts": _clean_text(event.get("ts"), 32),
                "epoch": event.get("epoch"),
                "sid": _clean_text(event.get("sid"), MAX_SID_CHARS),
                "act": _clean_text(event.get("act"), 24),
                "reason": _clean_text(event.get("reason"), MAX_REASON_CHARS),
                "dur_ms": int(event.get("dur_ms") or 0),
                "class": _clean_text(event.get("class"), 48) or "",
            }
        )

    return {
        "agent": agent,
        "generated_at": _epoch_to_ts(time.time()),
        "filters": {
            "act": wanted_acts,
            "sid": sid or "",
            "since": _epoch_to_ts(since),
            "until": _epoch_to_ts(until),
            "order": "desc" if descending else "asc",
            "limit": limit,
        },
        "acts": [
            {"act": key, "count": count} for key, count in sorted(acts_all.items(), key=lambda item: -item[1])
        ],
        "total": len(events),
        "matched": len(matched),
        "returned": len(rows),
        "rows": rows,
        "files": list(scan.get("files") or []),
        "redacted": True,
        "redaction_note": (
            "只返回 sid / act / reason / dur_ms / ts：会话正文与提示词不在 audit 行里，"
            "这里也不会读它们；reason 截断到 %d 字。本页只读，没有删除或清空日志的入口。"
            % MAX_REASON_CHARS
        ),
    }


@router.get("/autotitler/logs")
def get_autotitler_logs(
    agent: Optional[str] = None,
    act: Optional[str] = None,
    sid: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = LOG_ROWS_DEFAULT,
    files: int = 0,
    order: str = "desc",
) -> Any:
    """titler 的 audit 行（只读、倒序、可按 action / 会话 / 时间过滤）。

    ``GET /autotitler/logs?agent=default&act=failed&sid=local&limit=50`` →
    ``{"ok": true, "data": {rows:[{ts,sid,act,reason,dur_ms,class}], acts:[{act,count}],
    total, matched, returned, filters, redacted, redaction_note}}``

    ``act`` 支持逗号分隔多个（``failed,throttled``）。**没有写/删接口**：这是审计视图，
    不是日志管理台。
    """
    try:
        agent_id = _prepare_agent(agent)
        limit_n = max(1, min(int(limit), LOG_ROWS_MAX))
        files_n = max(0, min(int(files), 10))
        since_epoch = _parse_ts_arg(since)
        until_epoch = _parse_ts_arg(until)
        if since_epoch is not None and until_epoch is not None and since_epoch > until_epoch:
            raise ApiError(
                400,
                CODE_INVALID_ARGUMENT,
                "since 比 until 还晚：%s > %s" % (_epoch_to_ts(since_epoch), _epoch_to_ts(until_epoch)),
            )
        return {
            "ok": True,
            "data": _logs_view(agent_id, act, sid, since_epoch, until_epoch, limit_n, files_n, order),
        }
    except ApiError as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("hermes-console.autotitler: 读运行日志失败 agent=%r", agent)
        return _fail(500, CODE_UPSTREAM_ERROR, "读运行日志失败：%s: %s" % (type(exc).__name__, exc))







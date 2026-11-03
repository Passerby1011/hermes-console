# -*- coding: utf-8 -*-
"""hermes-console · 模块注册核心（M1b）：manifest 发现 + 启用集计算 + 故障隔离。

## 一句话模型

**一个功能模块 = ``dashboard/modules/<模块 id>/`` 一个自包含目录**，目录里有
``manifest.json``（声明）和可选的 ``api.py``（后端路由，薄封装）。删掉目录 = 该模块下线，
不需要改任何其它文件；某个目录的 manifest 写坏 = 只有它进 ``failed``，其它模块照常工作。

## 三层 API（M4 设置界面 / 前端动态导航都从这里取数）

1. :func:`discover_modules` —— 扫描磁盘，返回**能用的** :class:`ModuleSpec` 列表
2. :func:`enabled_modules` / :func:`resolve_modules` —— 算"某个 agent 该看到哪些模块"，
   每条都带 ``reason``（为什么启用 / 为什么没启用，不许静默消失）
3. :func:`health` —— ``{"loaded": [...], "failed": {id: 原因}}``，故障隔离的对外窗口

## 启用优先级（低 → 高）

``manifest.default_enabled`` → 全局 settings（根 ``config.yaml``）
→ 该 agent settings（``<home>/profiles/<agent>/config.yaml``）

合并完全交给 :func:`dashboard.core.settings.effective_settings`（它每值带 ``source``），
本模块**不自己读 YAML**，也不自己发明合并顺序。

配置落点（M4 写设置时用同一路径）：``plugins.entries.hermes-console.settings.modules.<id>.enabled``。

## 依赖

``manifest.requires`` 里的模块没启用 ⇒ 本模块不启用，``reason`` 里点名缺哪个（含未安装 / 成环）。

## 故障隔离

单个目录出现下列任一情况时**只影响它自己**：缺 ``manifest.json``、JSON 解析失败、根不是对象、
缺必填字段（``id`` / ``label``）、字段类型不对、``id`` 与别的模块重复。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import settings as core_settings
from .scope import list_agent_ids, normalize_agent_id

logger = logging.getLogger(__name__)

#: 模块目录名（``dashboard/modules``）。
MODULES_DIR_NAME = "modules"
#: 每个模块目录里的声明文件名。
MANIFEST_NAME = "manifest.json"
#: 模块 id 文法：小写字母开头，字母/数字/下划线/短横线，≤64 位（与目录名一致，便于"删目录即下线"）。
MODULE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
#: 启用开关在 ``settings.modules.<id>`` 子树里的键。
ENABLED_KEY = "enabled"
#: 可选字段的缺省值（写进 README，别在两处各写一套）。
DEFAULT_NAV_ORDER = 1000
DEFAULT_VERSION = "0.0.0"
DEFAULT_ENABLED = True
#: 必填字段：缺了只让该模块进 ``failed``。
REQUIRED_FIELDS: Tuple[str, ...] = ("id", "label")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
class ManifestError(Exception):
    """单个模块 manifest 不合格（消息即 ``health().failed`` 里的原因）。"""


@dataclass(frozen=True)
class ModuleSpec:
    """一个模块的声明（``manifest.json`` 的内存形态，只读）。"""

    id: str
    label: str
    version: str = DEFAULT_VERSION
    nav_order: int = DEFAULT_NAV_ORDER
    default_enabled: bool = DEFAULT_ENABLED
    #: 后端模块名（``dashboard/`` 下的 ``api_*.py``），可空 —— 纯前端模块没有后端。
    api_module: Optional[str] = None
    #: 前端分区 id（``src/sections/*.js`` 注册的 key），可空。
    section: Optional[str] = None
    #: 该模块的设置项声明（M4 设置界面按它渲染），可空。
    settings_schema: Dict[str, Any] = field(default_factory=dict)
    #: 依赖的其它模块 id，可空。
    requires: Tuple[str, ...] = ()
    #: 目录名（``dashboard/modules/<directory>``）—— 用于报错定位，不等于 ``id``。
    directory: str = ""
    #: manifest 的绝对路径。
    manifest_path: Optional[Path] = None

    def to_dict(self, *, enabled: Optional[bool] = None, reason: str = "") -> Dict[str, Any]:
        """``GET /modules`` 里的一条（``enabled`` / ``reason`` 由启用集计算填）。"""
        item: Dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "nav_order": self.nav_order,
            "section": self.section,
            "version": self.version,
            "api_module": self.api_module,
            "requires": list(self.requires),
        }
        if enabled is not None:
            item["enabled"] = bool(enabled)
        item["reason"] = reason
        return item


@dataclass(frozen=True)
class ModuleDecision:
    """一个模块对某个 agent 的启用结论（**带原因**，供 ``/modules`` 与设置界面显示）。"""

    spec: ModuleSpec
    enabled: bool
    reason: str
    #: 该模块自身的开关来源：``module_default`` / ``global`` / ``agent`` / ``requires`` / ``unknown``。
    source: str = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return self.spec.to_dict(enabled=self.enabled, reason=self.reason)


@dataclass(frozen=True)
class ScanResult:
    """一次扫描的结果：能用的 + 坏掉的（坏掉的带原因）。"""

    specs: List[ModuleSpec]
    failed: Dict[str, str]
    modules_dir: Path


# ---------------------------------------------------------------------------
# 目录定位 + manifest 解析
# ---------------------------------------------------------------------------
def modules_dir(modules_dir: Optional[Path] = None) -> Path:
    """模块根目录：显式传入优先，否则 ``<dashboard>/modules``（本文件是 ``dashboard/core/registry.py``）。"""
    if modules_dir is not None:
        return Path(modules_dir)
    return Path(__file__).resolve().parents[1] / MODULES_DIR_NAME


def _require_str(data: Mapping[str, Any], key: str, default: Optional[str] = None) -> Optional[str]:
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if not isinstance(value, str):
        raise ManifestError("字段 %r 必须是字符串，实际是 %s" % (key, type(value).__name__))
    text = value.strip()
    return text or default


def _parse_manifest(path: Path, directory: str) -> ModuleSpec:
    """解析并校验一份 manifest。任何不合格都抛 :class:`ManifestError`（消息写清原因）。"""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ManifestError("读不到 manifest.json：%s" % exc) from None
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - 解析失败 == 该模块坏掉，原因照原样报
        raise ManifestError("manifest.json 不是合法 JSON：%s: %s" % (type(exc).__name__, exc)) from None
    if not isinstance(data, Mapping):
        raise ManifestError("manifest.json 的根必须是对象（实际是 %s）" % type(data).__name__)

    for key in REQUIRED_FIELDS:
        if key not in data or data[key] is None or (isinstance(data[key], str) and not data[key].strip()):
            raise ManifestError("缺必填字段 %r（%s 必填：%s）" % (key, MANIFEST_NAME, ", ".join(REQUIRED_FIELDS)))

    module_id = data["id"]
    if not isinstance(module_id, str) or not MODULE_ID_RE.match(module_id.strip()):
        raise ManifestError(
            "字段 'id' 非法（%r）：只能是小写字母开头的 1-64 位小写字母/数字/下划线/短横线" % (module_id,)
        )
    module_id = module_id.strip()

    label = data["label"]
    if not isinstance(label, str) or not label.strip():
        raise ManifestError("字段 'label' 必须是非空字符串（人可读名字）")
    label = label.strip()

    version = _require_str(data, "version", DEFAULT_VERSION) or DEFAULT_VERSION

    nav_order = data.get("nav_order", DEFAULT_NAV_ORDER)
    if isinstance(nav_order, bool) or not isinstance(nav_order, int):
        raise ManifestError("字段 'nav_order' 必须是整数（实际是 %r）" % (nav_order,))

    default_enabled = data.get("default_enabled", DEFAULT_ENABLED)
    if not isinstance(default_enabled, bool):
        raise ManifestError("字段 'default_enabled' 必须是布尔值（实际是 %r）" % (default_enabled,))

    api_module = _require_str(data, "api_module")
    section = _require_str(data, "section")

    settings_schema = data.get("settings_schema")
    if settings_schema is None:
        settings_schema = {}
    if not isinstance(settings_schema, Mapping):
        raise ManifestError("字段 'settings_schema' 必须是对象（实际是 %s）" % type(settings_schema).__name__)

    requires_raw = data.get("requires")
    if requires_raw is None:
        requires_raw = []
    if isinstance(requires_raw, str):
        raise ManifestError("字段 'requires' 必须是字符串数组（不是单个字符串）")
    if not isinstance(requires_raw, Sequence):
        raise ManifestError("字段 'requires' 必须是字符串数组（实际是 %s）" % type(requires_raw).__name__)
    requires: List[str] = []
    for item in requires_raw:
        if not isinstance(item, str) or not item.strip():
            raise ManifestError("字段 'requires' 里每项都必须是非空字符串（实际有 %r）" % (item,))
        requires.append(item.strip())

    return ModuleSpec(
        id=module_id,
        label=label,
        version=version,
        nav_order=int(nav_order),
        default_enabled=bool(default_enabled),
        api_module=api_module,
        section=section,
        settings_schema=dict(settings_schema),
        requires=tuple(requires),
        directory=directory,
        manifest_path=path,
    )


def scan(modules_dir_path: Optional[Path] = None) -> ScanResult:
    """扫描模块根目录：**逐个目录独立 try**，坏的进 ``failed``，好的照常返回。

    返回的 ``specs`` 已按 ``(nav_order, id)`` 排序。

    **id 重复怎么判**（两遍扫描）：同一个 ``id`` 出现在多个目录时，

    1. 目录名恰好等于 ``id`` 的那份赢（``modules/overview/manifest.json`` 里写
       ``"id": "overview"`` 是规范写法）；
    2. 没有目录名对得上时，按目录名字典序的第一份赢。

    其余重复份进 ``failed``（原因点名赢了的是哪个目录）。这样**一个乱写的临时目录
    不会把真模块顶掉**——`modules/_junk/manifest.json` 里写 `"id": "overview"` 只会让
    `_junk` 自己失败。
    """
    root = modules_dir(modules_dir_path)
    failed: Dict[str, str] = {}
    parsed: List[Tuple[str, ModuleSpec]] = []

    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        logger.warning("hermes-console.registry: 模块目录 %s 不可读（按空处理）：%s", root, exc)
        return ScanResult(specs=[], failed={}, modules_dir=root)

    for child in children:
        try:
            is_dir = child.is_dir()
        except OSError:
            continue
        if not is_dir:
            continue  # README.md 之类
        name = child.name
        if name.startswith("__"):
            continue  # __pycache__ / 骨架占位
        manifest_path = child / MANIFEST_NAME
        try:
            if not manifest_path.is_file():
                raise ManifestError("缺 %s（每个模块目录都必须有一份）" % MANIFEST_NAME)
            spec = _parse_manifest(manifest_path, name)
        except ManifestError as exc:
            failed[name] = str(exc)
            logger.warning("hermes-console.registry: 模块 %r 不可用：%s", name, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - 兜底：任何意外也只损失这一个模块
            failed[name] = "%s: %s" % (type(exc).__name__, exc)
            logger.warning("hermes-console.registry: 模块 %r 加载异常：%s", name, exc)
            continue
        parsed.append((name, spec))

    # 第二遍：解决 id 重复（目录名 == id 的那份优先，否则字典序第一份）
    groups: Dict[str, List[Tuple[str, ModuleSpec]]] = {}
    for name, spec in parsed:
        groups.setdefault(spec.id, []).append((name, spec))

    specs: List[ModuleSpec] = []
    for module_id, candidates in groups.items():
        winner = next((item for item in candidates if item[0] == module_id), candidates[0])
        specs.append(winner[1])
        for name, _spec in candidates:
            if name == winner[0]:
                continue
            failed[name] = "id %r 与目录 %r 重复（模块 id 必须全局唯一，重复的这份不会生效）" % (
                module_id,
                winner[0],
            )
            logger.warning(
                "hermes-console.registry: 模块 %r 不可用：id %r 已被目录 %r 占用",
                name,
                module_id,
                winner[0],
            )

    specs.sort(key=lambda s: (s.nav_order, s.id))
    return ScanResult(specs=specs, failed=failed, modules_dir=root)



def discover_modules(modules_dir_path: Optional[Path] = None) -> List[ModuleSpec]:
    """扫描 ``dashboard/modules/*/manifest.json``，返回**可用**模块（按 ``nav_order`` 排序）。

    坏掉的模块不会出现在返回值里（也**不会**让本函数抛异常）——它们去哪了、为什么，
    看 :func:`health`。
    """
    return scan(modules_dir_path).specs


def health(modules_dir_path: Optional[Path] = None) -> Dict[str, Any]:
    """``{"loaded": [...], "failed": {id: 原因}}`` —— 故障隔离的对外窗口。

    * ``loaded``：能用模块的 id 列表（顺序 = ``nav_order``）
    * ``failed``：坏掉模块的**目录名** → 原因（缺字段 / JSON 坏 / id 重复 / 缺 manifest …）
    * 额外给 ``modules_dir`` 与 ``count``，方便排查"到底扫的哪个目录"
    """
    result = scan(modules_dir_path)
    return {
        "loaded": [spec.id for spec in result.specs],
        "failed": dict(result.failed),
        "count": len(result.specs),
        "modules_dir": str(result.modules_dir),
    }


# ---------------------------------------------------------------------------
# 启用集计算
# ---------------------------------------------------------------------------
def _own_enablement(
    spec: ModuleSpec,
    agent_id: str,
    home: Optional[Path],
) -> Tuple[bool, str, str]:
    """只看**模块自身**开关（不含依赖）：``(enabled, reason, source)``。"""
    effective = core_settings.effective_settings(
        agent_id,
        spec.id,
        {ENABLED_KEY: spec.default_enabled},
        home=home,
    )
    entry = effective.get(ENABLED_KEY)
    if entry is None:  # 理论上不会发生（defaults 兜底），保守按默认值
        return spec.default_enabled, "按模块默认（module_default）", core_settings.SOURCE_MODULE_DEFAULT
    value = entry.get("value")
    source = str(entry.get("source") or "unknown")
    enabled = bool(value)
    if enabled:
        if source == core_settings.SOURCE_MODULE_DEFAULT:
            reason = "模块默认启用（module_default）"
        elif source == core_settings.SOURCE_GLOBAL:
            reason = "全局设置启用（global）"
        elif source == core_settings.SOURCE_AGENT:
            reason = "该 agent 设置启用（agent）"
        else:
            reason = "启用（%s）" % source
    else:
        if source == core_settings.SOURCE_MODULE_DEFAULT:
            reason = "模块默认关闭（module_default）"
        elif source == core_settings.SOURCE_GLOBAL:
            reason = "全局设置关闭（global）"
        elif source == core_settings.SOURCE_AGENT:
            reason = "该 agent 显式关闭（agent 设置 modules.%s.enabled=false）" % spec.id
        else:
            reason = "关闭（%s）" % source
    return enabled, reason, source


def resolve_modules(
    agent_id: str,
    *,
    home: Optional[Path] = None,
    modules_dir_path: Optional[Path] = None,
) -> List[ModuleDecision]:
    """算出某 agent 对**每个已发现模块**的结论（含未启用的，带原因），按 ``nav_order`` 排序。

    依赖语义（``manifest.requires``）：

    * 依赖模块**没被发现**（没装）⇒ 不启用，原因点名缺哪个
    * 依赖模块被发现但**没启用** ⇒ 不启用，原因点名是哪个依赖
    * 依赖成环 ⇒ 环上每个都不启用，原因写明成环路径（不递归到栈溢出）
    """
    agent = normalize_agent_id(agent_id, home=home)
    specs = scan(modules_dir_path).specs
    by_id: Dict[str, ModuleSpec] = {spec.id: spec for spec in specs}
    own: Dict[str, Tuple[bool, str, str]] = {
        spec.id: _own_enablement(spec, agent, home) for spec in specs
    }

    decisions: Dict[str, ModuleDecision] = {}
    visiting: List[str] = []

    def resolve(module_id: str) -> ModuleDecision:
        if module_id in decisions:
            return decisions[module_id]
        spec = by_id[module_id]
        enabled, reason, source = own[module_id]
        if not enabled:
            decision = ModuleDecision(spec=spec, enabled=False, reason=reason, source=source)
            decisions[module_id] = decision
            return decision

        visiting.append(module_id)
        blocked_reason = ""
        for dependency in spec.requires:
            if dependency in visiting:
                cycle = " -> ".join(visiting[visiting.index(dependency):] + [dependency])
                blocked_reason = "依赖成环：%s（环上的模块都不会启用）" % cycle
                break
            if dependency not in by_id:
                blocked_reason = "缺少依赖模块：%s（%r 未安装 / 未通过校验）" % (dependency, dependency)
                break
            dependency_decision = resolve(dependency)
            if not dependency_decision.enabled:
                blocked_reason = "依赖模块未启用：%s（%s）" % (dependency, dependency_decision.reason)
                break
        visiting.pop()

        if blocked_reason:
            decision = ModuleDecision(spec=spec, enabled=False, reason=blocked_reason, source="requires")
        else:
            decision = ModuleDecision(spec=spec, enabled=True, reason=reason, source=source)
        decisions[module_id] = decision
        return decision

    for spec in specs:
        resolve(spec.id)
    return [decisions[spec.id] for spec in specs]


def enabled_modules(
    agent_id: str,
    *,
    home: Optional[Path] = None,
    modules_dir_path: Optional[Path] = None,
) -> List[ModuleSpec]:
    """该 agent 启用的模块（按 ``nav_order`` 排序）。原因见 :func:`resolve_modules`。"""
    return [
        decision.spec
        for decision in resolve_modules(agent_id, home=home, modules_dir_path=modules_dir_path)
        if decision.enabled
    ]


def module_status(
    agent_id: str,
    *,
    home: Optional[Path] = None,
    modules_dir_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """``GET /modules`` 里 ``modules`` 数组（**含未启用的**，每条带 ``enabled`` / ``reason``）。"""
    return [
        decision.to_dict()
        for decision in resolve_modules(agent_id, home=home, modules_dir_path=modules_dir_path)
    ]


def describe(agent_id: Optional[str] = None) -> Dict[str, Any]:
    """一次调用拿全：某 agent 的模块视图 + 健康状态（``/modules`` 直接用它）。"""
    agent = normalize_agent_id(agent_id)
    return {
        "agent": agent,
        "modules": module_status(agent),
        "health": health(),
    }


def legal_agent_ids(home: Optional[Path] = None) -> List[str]:
    """当前合法 agent id 列表（非法 agent 报 400 时回给前端，让人知道能填什么）。"""
    return list_agent_ids(home=home)


def agent_exists(agent_id: Optional[str], home: Optional[Path] = None) -> bool:
    """这个 agent id 现在**真的存在**吗（= 枚举得到的 id 之一）。

    与 :func:`normalize_agent_id` 的分工是刻意的：那个只归一 / 校验**文法**，
    故意放行「合法的假 id」（新建 agent 后再写配置的流程需要它宽容）；
    这里回答存在性，给「不存在的 agent 不许静默回退默认清单」的端点用
    （``GET /modules``：假 agent 曾拿到 default 的完整模块清单，静默 200）。

    判定口径与 :func:`list_agents` / ``GET /agents`` 完全一致 —— 前端作用域选择器也是从那里来的，
    不会出现「选择器里没有、/modules 却认得」的分叉。
    """
    agent = normalize_agent_id(agent_id, home=home)
    return agent in set(list_agent_ids(home=home))


__all__ = [
    "DEFAULT_ENABLED",
    "DEFAULT_NAV_ORDER",
    "DEFAULT_VERSION",
    "ENABLED_KEY",
    "MANIFEST_NAME",
    "MODULE_ID_RE",
    "MODULES_DIR_NAME",
    "ManifestError",
    "ModuleDecision",
    "ModuleSpec",
    "ScanResult",
    "agent_exists",
    "describe",
    "discover_modules",
    "enabled_modules",
    "health",
    "legal_agent_ids",
    "module_status",
    "modules_dir",
    "normalize_agent_id",
    "resolve_modules",
    "scan",
]

# -*- coding: utf-8 -*-
"""hermes-console · agent(=profile) → 路径解析 + 只读配置加载。

设计约束（来自 M1a 卡片，踩过坑的地方都写在下面）：

1. **不读 ``HERMES_HOME`` 做路径拼接。** 该变量在多 profile 进程里语义是"当前 profile 目录"
   （code-worker 进程里它是 ``<home>/profiles/code-worker``），照读会把 9 个 agent 缩成 1 个。
   统一走 ``hermes_constants.get_default_hermes_root()``（官方解析器，自己会把
   ``<root>/profiles/<p>`` 归一到 ``<root>``），并允许调用方显式传 ``home`` 覆盖。
2. **枚举逻辑自己写一份**，不 import ``dashboard/api_agents.py``（避免循环依赖）。
   但**语义**与它一致：``default`` + ``profiles/`` 下带身份标记、未被 tombstone 的目录。
3. **只读**。本模块不写任何文件；写入一律走 ``dashboard.core.settings.save_setting``
   → 官方 ``save_plugin_setting``。
4. 所有路径都是 ``pathlib.Path``；``read_agent_config`` 对缺文件返回 ``{}``，**从不抛异常**
   （很多 profile 没有自己的 ``config.yaml``）。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: ``default`` 不是 ``profiles/`` 下的目录，它是 home 根本身。
DEFAULT_AGENT_ID = "default"

#: 与官方 ``hermes_constants.PROFILE_ID_RE`` 同一条文法（小写、数字开头、≤64）。
#: 自己写一份而不是 import：本模块要能在没有 hermes_cli 的纯路径场景下被导入。
AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: 别名：这些都表示"当前/默认 agent"。
_ALIASES = frozenset({"", "current", "default"})

#: 一个目录算 profile 的身份标记（对齐官方 ``named_profile_has_identity``）。
_IDENTITY_MARKERS = ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db")

#: ``profile.yaml`` 里人可读名字的落点（官方放 ``ui_meta.hermes-bots.title``）。
_TITLE_PATHS = (
    ("ui_meta", "hermes-bots", "title"),
    ("ui_meta", "hermes_bots", "title"),
    ("title",),
    ("name",),
)


# ---------------------------------------------------------------------------
# home 解析
# ---------------------------------------------------------------------------
def _looks_like_home(path: Path) -> bool:
    """``<home>/config.yaml`` 与 ``<home>/profiles/`` 同时存在 ⇒ 这是所有 profile 的共同父目录。"""
    try:
        return (path / "config.yaml").is_file() and (path / "profiles").is_dir()
    except OSError:
        return False


def hermes_home(home: Optional[Path] = None) -> Path:
    """Hermes 主目录（所有 profile 的共同父目录）。

    * 调用方显式传 ``home`` ⇒ 直接用（探针、M1b 的测试都靠它）。
    * 否则问官方解析器 ``hermes_constants.get_default_hermes_root()``：它按
      ``native_home`` + ``HERMES_HOME`` 的**布局**判定根目录，``<root>/profiles/<p>``
      会归一到 ``<root>``，所以不会把 9 个 agent 缩成 1 个。
    * 再不行按本文件位置上溯 3 级（``<home>/plugins/hermes-console/dashboard/core/scope.py``）
      与 ``~/.hermes`` 兜底。

    每次调用都重新解析，不做模块级缓存：新增/删除 profile 目录后下一次调用就能看到。
    """
    if home is not None:
        return Path(home)

    candidates: List[Path] = []
    try:
        from hermes_constants import get_default_hermes_root  # 官方解析器

        candidates.append(Path(get_default_hermes_root()))
    except Exception:  # noqa: BLE001 - 没有 hermes_cli 时退回布局探测
        logger.debug("hermes-console.core.scope: get_default_hermes_root() 不可用，退回布局探测")
    try:
        # dashboard/core/scope.py → parents[3] == <home>
        candidates.append(Path(__file__).resolve().parents[3])
    except (IndexError, OSError):
        pass
    try:
        candidates.append(Path.home() / ".hermes")
    except (OSError, RuntimeError):
        pass

    fallback: Optional[Path] = None
    for cand in candidates:
        if _looks_like_home(cand):
            return cand
        if fallback is None and cand.is_dir():
            fallback = cand
    if fallback is not None:
        logger.warning(
            "hermes-console.core.scope: 未能按布局探测到 Hermes 主目录，退化为 %s（候选：%s）",
            fallback,
            [str(c) for c in candidates],
        )
        return fallback
    return Path.home() / ".hermes"


def agent_home(agent_id: str, home: Optional[Path] = None) -> Path:
    """某 agent 的 home 目录：``default`` → 根目录；其余 → ``<home>/profiles/<id>``。"""
    agent = normalize_agent_id(agent_id, home=home)
    root = hermes_home(home)
    return root if agent == DEFAULT_AGENT_ID else root / "profiles" / agent


def agent_config_path(agent_id: str, home: Optional[Path] = None) -> Path:
    """某 agent 的 ``config.yaml`` 路径（不保证存在）。"""
    return agent_home(agent_id, home=home) / "config.yaml"


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
def _has_identity(directory: Path) -> bool:
    """目录算 profile 的判定：任一身份标记是文件（或悬空符号链接）。"""
    for marker in _IDENTITY_MARKERS:
        try:
            if (directory / marker).is_file() or (directory / marker).is_symlink():
                return True
        except OSError:
            continue
    return False


def _is_tombstoned(profiles_root: Path, name: str) -> bool:
    """``profiles/.deleted/<name>`` 存在 ⇒ 该 profile 已被删除，不再列出/不再服务。"""
    try:
        return (profiles_root / ".deleted" / name).exists()
    except OSError:
        return False


def _agent_label(agent_id: str, directory: Path) -> str:
    """人可读名字：``profile.yaml`` 的 title，读不到就回退到 id。"""
    try:
        text = (directory / "profile.yaml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return agent_id
    try:
        import yaml  # 宿主自带；缺失时只是"没有名字"

        data: Any = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 - 解析失败 == 没有名字
        return agent_id
    for keys in _TITLE_PATHS:
        node: Any = data
        for key in keys:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, str) and node.strip():
            return node.strip()
    return agent_id


def _agent_entry(agent_id: str, directory: Path) -> Dict[str, Any]:
    config_path = directory / "config.yaml"
    try:
        exists = config_path.is_file()
    except OSError:
        exists = False
    return {
        "id": agent_id,
        "label": _agent_label(agent_id, directory),
        "home": directory,
        "config_path": config_path,
        "exists": exists,
        # 兼容 api_agents.py 的字段名，前端两个地方都能用同一份数据
        "has_config": exists,
        "is_default": agent_id == DEFAULT_AGENT_ID,
    }


def list_agents(home: Optional[Path] = None) -> List[Dict[str, Any]]:
    """枚举全部 agent：``default`` + ``profiles/*``（动态扫描，不写死）。

    每项至少含：

    ``id`` (str) / ``home`` (Path) / ``config_path`` (Path) / ``exists`` (bool)，
    另外附带 ``label`` / ``has_config`` / ``is_default``。

    ``exists`` 指该 agent **自己的** ``config.yaml`` 是否存在；``default`` 恒为根配置。
    没有 ``config.yaml`` 的 profile 也会列出（只要它有别的身份标记，比如 ``profile.yaml``），
    此时 ``exists=False``，读配置得到 ``{}``。
    """
    root = hermes_home(home)
    agents: List[Dict[str, Any]] = [_agent_entry(DEFAULT_AGENT_ID, root)]

    profiles_root = root / "profiles"
    try:
        children = sorted(profiles_root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return agents

    for child in children:
        name = child.name
        if name.startswith((".", "__")) or not AGENT_ID_RE.match(name):
            continue
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        if _is_tombstoned(profiles_root, name):
            continue
        if not _has_identity(child):
            continue
        agents.append(_agent_entry(name, child))
    return agents


def list_agent_ids(home: Optional[Path] = None) -> List[str]:
    """只要 id 列表（``list_agents`` 的轻量视图）。"""
    return [agent["id"] for agent in list_agents(home=home)]


# ---------------------------------------------------------------------------
# id 归一化
# ---------------------------------------------------------------------------
def normalize_agent_id(raw: Optional[str], home: Optional[Path] = None) -> str:
    """把外部输入（URL 参数、请求体、``None``）归一成合法 agent id。

    * ``None`` / ``""`` / ``"current"`` / ``"default"`` → ``"default"``
    * 大小写不敏感（``"Xiaomo"`` → ``"xiaomo"``），落盘目录名一律小写
    * 非法（``"../../etc"``、``"a b"``、超长、大写规范化后仍不合法）→ ``ValueError``，
      **消息里列出当前合法 id**，让调用方能直接把列表回给前端

    注意：只校验**文法**，不校验存在性——不存在的合法 id 也能过（读配置返回 ``{}``），
    这样"新建 agent 后再写配置"的流程不会被卡住。
    """
    text = "" if raw is None else str(raw).strip()
    lowered = text.lower()
    if lowered in _ALIASES:
        return DEFAULT_AGENT_ID
    if AGENT_ID_RE.match(lowered):
        return lowered

    legal = list_agent_ids(home=home)
    raise ValueError(
        "非法 agent id %r：agent id 只能是 1-64 位小写字母/数字/下划线/短横线，且必须以字母或数字开头"
        "（也可以是 'default' / 'current'）。当前合法 id：%s" % (text, ", ".join(legal) or "<none>")
    )


# ---------------------------------------------------------------------------
# 只读加载
# ---------------------------------------------------------------------------
def read_agent_config(agent_id: str, home: Optional[Path] = None) -> Dict[str, Any]:
    """只读加载某 agent 的 ``config.yaml``，返回**原始** dict（不合并默认值、不展开 ``${VAR}``）。

    * 文件不存在 → ``{}``（很多 profile 没有自己的 config.yaml），**不抛异常**
    * 解析失败 / 读到非 mapping 根 → 记一条 warning 后 ``{}``（读路径 fail-soft；
      写入路径在官方 ``save_plugin_setting`` 里 fail-closed，不会因为这里宽容而丢数据）

    读取走官方 ``hermes_cli.config.read_config_doc(path)``（指定路径的原始读）；
    宿主没有 hermes_cli 时退回 ``yaml.safe_load``（同样是只读）。
    """
    agent = normalize_agent_id(agent_id, home=home)
    path = agent_config_path(agent, home=home)
    try:
        from hermes_cli.config import read_config_doc  # 官方指定路径读取器
    except Exception:  # noqa: BLE001 - 纯路径场景（无 hermes_cli）退回 yaml
        read_config_doc = None  # type: ignore[assignment]

    try:
        if read_config_doc is not None:
            data = read_config_doc(path)
        else:
            import yaml

            with open(path, encoding="utf-8-sig") as handle:
                data = yaml.safe_load(handle)
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 - 读路径一律 fail-soft
        logger.warning("hermes-console.core.scope: 读取 %s 失败（按空配置处理）：%s", path, exc)
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "hermes-console.core.scope: %s 的根不是 mapping（%s），按空配置处理",
            path,
            type(data).__name__,
        )
        return {}
    return dict(data)


__all__ = [
    "DEFAULT_AGENT_ID",
    "AGENT_ID_RE",
    "agent_config_path",
    "agent_home",
    "hermes_home",
    "list_agent_ids",
    "list_agents",
    "normalize_agent_id",
    "read_agent_config",
]

# -*- coding: utf-8 -*-
"""hermes-console — Agent（profile）枚举端点。

挂载契约：本文件不单独挂载。``plugin_api.py``（manifest 的 ``api`` 键指向的文件）
把本目录加进 ``sys.path`` 后 import 本模块，再 ``include_router(api_agents.router)``。
所有路径最终落在 ``/api/plugins/hermes-console/`` 之下。

返回契约（GET /agents）::

    {
      "agents": [{"id": str, "label": str, "is_default": bool, "has_config": bool}, ...],
      "current": str
    }

**禁止写死 agent 列表。** agent = ``<home>/profiles/`` 下含 ``config.yaml`` 的目录 + ``default``；
新增一个 profile 目录，这里自动多一个 agent，不需要改代码。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query  # type: ignore

router = APIRouter()

logger = logging.getLogger(__name__)

#: ``default`` 不是 ``profiles/`` 下的目录，它是 home 根本身。
DEFAULT_PROFILE = "default"

#: ``profile.yaml`` 里人可读名字的落点。Hermes 把它放在 ``ui_meta.hermes-bots.title``
#: （实测 xiaomo=墨黎、work=牛马、home 根=小白），不是顶层 ``title:``。
_TITLE_PATHS = (
    ("ui_meta", "hermes-bots", "title"),
    ("ui_meta", "hermes_bots", "title"),
    ("title",),
    ("name",),
)


# ---------------------------------------------------------------------------
# 主目录解析
# ---------------------------------------------------------------------------
def _is_hermes_home(path: Path) -> bool:
    """``<home>/profiles/`` 与 ``<home>/config.yaml`` 同时存在 ⇒ 这是所有 profile 的共同父目录。"""
    try:
        return (path / "profiles").is_dir() and (path / "config.yaml").is_file()
    except OSError:
        return False


def hermes_home() -> Path:
    """Hermes 主目录（所有 profile 的共同父目录）。

    **不直接采信 ``HERMES_HOME`` 环境变量。** 实测：worker / gateway / kanban 派生的进程里
    它被设成 ``<home>/profiles/<p>``（"当前 profile 目录"语义，例如 code-worker 进程里是
    ``D:\\hermes-data\\hermes\\profiles\\code-worker``）。照读会把 9 个 agent 缩成 1 个，
    这是本项目已经踩过的坑（model-switch 的同一段注释有同样的结论）。

    因此按布局正向探测，顺序：本文件上溯 3 级 → ``HERMES_HOME`` 及其上两级 → ``~/.hermes``。
    """
    candidates: List[Path] = []
    try:
        # <home>/plugins/hermes-console/dashboard/api_agents.py → parents[3] == <home>
        candidates.append(Path(__file__).resolve().parents[3])
    except (IndexError, OSError):
        pass
    try:
        # worktree / 自定义检出位置：按目录名找同时含 plugins/ 与 hermes-agent/ 的祖先
        from _layout import hermes_root

        detected = hermes_root(Path(__file__).resolve().parent)
        if detected is not None:
            candidates.append(detected)
    except Exception:  # noqa: BLE001 - 布局探测失败不能影响路由
        pass
    env_home = (os.environ.get("HERMES_HOME") or "").strip()
    if env_home:
        candidates.append(Path(env_home))
        candidates.append(Path(env_home).parent.parent)  # <home>/profiles/<p> → <home>
    candidates.append(Path.home() / ".hermes")

    fallback: Optional[Path] = None
    for cand in candidates:
        try:
            if _is_hermes_home(cand):
                return cand
        except OSError:
            continue
        if fallback is None and cand.is_dir():
            fallback = cand
    if fallback is not None:
        logger.warning(
            "hermes-console: 未能按布局探测到 Hermes 主目录，退化为 %s（候选：%s）",
            fallback,
            [str(c) for c in candidates],
        )
        return fallback
    return Path.home() / ".hermes"


HERMES_HOME_RESOLUTION_NOTE = (
    "主目录**每次请求重新解析**（不做模块级缓存）：新增/删除 profile 目录后，"
    "下一次 GET /agents 就能看到，不需要重启 dashboard 后端。"
    "3 次 is_dir/is_file stat 的成本远低于一个需要重启才能刷新的静默失效点。"
)


# ---------------------------------------------------------------------------
# profile 发现 / 标签
# ---------------------------------------------------------------------------
def _profile_ids(home: Optional[Path] = None) -> List[str]:
    """``default`` + ``profiles/`` 下含 ``config.yaml`` 的目录（按名排序）。"""
    root = (home or hermes_home()) / "profiles"
    found: List[str] = [DEFAULT_PROFILE]
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return found
    for child in children:
        name = child.name
        if name.startswith((".", "__")) or name in found:
            continue
        try:
            if child.is_dir() and (child / "config.yaml").is_file():
                found.append(name)
        except OSError:
            continue
    return found


def _profile_dir(profile: str, home: Optional[Path] = None) -> Path:
    base = home or hermes_home()
    return base if profile == DEFAULT_PROFILE else base / "profiles" / profile


def _yaml_title(path: Path) -> Optional[str]:
    """从 ``profile.yaml`` 取人可读名字；读不到就返回 ``None``（由调用方回退到 id）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        import yaml  # 宿主环境自带；缺失时走下面的文本兜底

        data = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 - 任何解析失败都只是"没有名字"
        data = None
    for keys in _TITLE_PATHS:
        node: Any = data
        for key in keys:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, str) and node.strip():
            return node.strip()
    return None


def _agent_label(profile: str, home: Optional[Path] = None) -> str:
    title = _yaml_title(_profile_dir(profile, home) / "profile.yaml")
    return title or profile


def _current_profile(profile_dir: Path, requested: str = "") -> str:
    """当前作用域 agent。

    优先级：显式 ``?profile=`` → 启动方钉的 ``HERMES_PROFILE_NAME``/``HERMES_PROFILE`` →
    进程 ``HERMES_HOME`` 落在 ``<home>/profiles/<p>`` 时取 ``<p>`` → ``default``。

    桌面端按 profile 起多个 backend，每个进程的 ``HERMES_HOME`` 就指向自己那份 profile；
    前端切作用域时也可以直接用 ``?profile=`` 指定，不必依赖进程环境。
    """
    if requested and requested.strip():
        return requested.strip()
    for env_name in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        value = (os.environ.get(env_name) or "").strip()
        if value:
            return value
    try:
        if profile_dir.parent.name == "profiles":
            return profile_dir.name
    except (AttributeError, IndexError):
        pass
    return DEFAULT_PROFILE


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.get("/agents")
def list_agents(profile: str = Query(default="", description="覆盖当前作用域；留空按进程环境推断")) -> Dict[str, Any]:
    """枚举全部 agent（动态扫描，不写死）。

    主目录每次请求重新解析（见 ``HERMES_HOME_RESOLUTION_NOTE``）：新增 profile 不需要重启。
    """
    home = hermes_home()
    agents: List[Dict[str, Any]] = []
    for pid in _profile_ids(home):
        directory = _profile_dir(pid, home)
        try:
            has_config = (directory / "config.yaml").is_file()
        except OSError:
            has_config = False
        agents.append(
            {
                "id": pid,
                "label": _agent_label(pid, home),
                "is_default": pid == DEFAULT_PROFILE,
                "has_config": has_config,
            }
        )
    return {
        "agents": agents,
        "current": _current_profile(Path(os.environ.get("HERMES_HOME") or str(home)), profile),
    }

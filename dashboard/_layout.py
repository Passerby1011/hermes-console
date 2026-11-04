# -*- coding: utf-8 -*-
"""布局探测：本插件在不同检出位置（生产 ``plugins/hermes-console`` / git worktree
``.../hermes-console/.worktrees/<id>``）下都能找到兄弟插件与宿主根。

为什么单独一个模块：dashboard 里的模块会被 ``plugin_api`` 以裸模块名 import（无包上下文），
路径只能靠 ``__file__`` 上溯；以前写死 ``parents[1]`` / ``parents[3]``，在 worktree 里会
分别指到 ``.worktrees`` 和 ``hermes-console``，于是「找不到 hermes-auto-group/core.py」
「找不到 advisor/audit.py」——不是功能坏了，是布局变了。这里统一按**目录名**探测，
不看固定层数，生产与 worktree 都成立。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def plugins_dir(start: Path) -> Path:
    """``<hermes>/plugins``：从 ``start`` 向上找最近的名为 ``plugins`` 的祖先目录。

    找不到时退回 ``start.parents[1]``（生产布局 ``dashboard`` 的上一级即 ``plugins``）。
    """
    resolved = Path(start).resolve()
    for ancestor in resolved.parents:
        if ancestor.name == "plugins":
            return ancestor
    try:
        return resolved.parents[1]
    except IndexError:
        return resolved


def hermes_agent_dir(start: Path) -> Optional[Path]:
    """宿主源码根 ``<hermes>/hermes-agent``（含 ``hermes_yaml.py`` / ``hermes_cli``）。"""
    resolved = Path(start).resolve()
    for ancestor in resolved.parents:
        candidate = ancestor / "hermes-agent"
        if candidate.is_dir():
            return candidate
    return None


def hermes_root(start: Path) -> Optional[Path]:
    """Hermes 主目录：同时含 ``plugins/`` 与 ``hermes-agent/`` 的最近祖先。"""
    resolved = Path(start).resolve()
    for ancestor in resolved.parents:
        if (ancestor / "plugins").is_dir() and (ancestor / "hermes-agent").is_dir():
            return ancestor
    return None

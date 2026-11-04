# -*- coding: utf-8 -*-
"""pytest 引导：把 Hermes 宿主与插件自身接进 ``sys.path``。

本插件的后端直接 import 宿主模块（``hermes_cli.*`` / ``hermes_yaml``），
所以测试必须在「宿主存在」的环境里跑。宿主位置按以下顺序解析：

1. ``HERMES_HOME`` 环境变量（最可靠：指向 Hermes 数据根，其下应有 ``hermes-agent/``）；
2. 从本文件向上逐级找：任一祖先目录下存在 ``hermes-agent/hermes_yaml.py``，
   那就是作为插件安装时的正常布局（``<root>/plugins/hermes-console/dashboard/``）。

找不到宿主时**不报错也不静默**：相关测试会因 ImportError 失败，
报错文本里会直接出现 ``No module named 'hermes_yaml'`` —— 它说的是「没装宿主」，
不是「插件坏了」。安装/使用本插件本身不需要跑测试，见 README「测试前提」。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_DASHBOARD = Path(__file__).resolve().parent


def _candidate_roots():
    env = (os.environ.get("HERMES_HOME") or "").strip()
    if env:
        yield Path(env).expanduser()
    for parent in [_DASHBOARD, *_DASHBOARD.parents]:
        yield parent


def _find_host() -> Path | None:
    for root in _candidate_roots():
        agent = root / "hermes-agent"
        if (agent / "hermes_yaml.py").is_file():
            return agent
        # HERMES_HOME 或祖先本身就是 hermes-agent
        if (root / "hermes_yaml.py").is_file():
            return root
    return None


_HOST = _find_host()

# 顺序有意义：dashboard 在前，宿主在后 —— 插件的裸模块名（api_* / core / _layout）
# 不能被宿主的同名模块抢走。
for entry in (_HOST, _DASHBOARD):
    if entry is not None and str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

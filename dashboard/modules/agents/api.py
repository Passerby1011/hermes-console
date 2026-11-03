# -*- coding: utf-8 -*-
"""agents 模块 · 后端薄封装（**不含逻辑**）。

路由实现仍在 ``dashboard/api_agents.py``（C2 搬进来的那份），本文件只做"目录即模块"的接线：
``manifest.json`` 的 ``api_module`` 指向同一个模块名，``router`` 就是它的 router，一行逻辑都不搬。

导入方式两条都要能走通（理由见 ``modules/overview/api.py``）：

1. 包上下文（``dashboard.modules.agents.api``）→ ``from ... import api_agents``
2. 裸名上下文（``plugin_api.py`` 把 ``dashboard`` 插进 ``sys.path``）→ ``import api_agents``
"""
from __future__ import annotations

try:  # ① 包上下文：dashboard.modules.agents.api
    from ... import api_agents as _impl
except Exception:  # noqa: BLE001 - ② 裸名上下文（plugin_api 的 sys.path 方式）
    import api_agents as _impl  # type: ignore[no-redef]

#: 模块 id，与同目录 ``manifest.json`` 的 ``id`` 一致。
MODULE_ID = "agents"

#: 直接复用现有实现的路由对象（不复制、不包装）。
router = _impl.router

__all__ = ["MODULE_ID", "router"]

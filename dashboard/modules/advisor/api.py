# -*- coding: utf-8 -*-
"""advisor 模块 · 后端薄封装（**不含逻辑**）。

路由实现仍在 ``dashboard/api_advisor.py``（C9b 那份）。本文件只做"目录即模块"的接线。
``default_enabled=false``：该分区有 decision / settings 写入，按"带写默认关"策略出厂关闭。

导入方式两条都要能走通（理由见 ``modules/overview/api.py``）：

1. 包上下文（``dashboard.modules.advisor.api``）→ ``from ... import api_advisor``
2. 裸名上下文（``plugin_api.py`` 把 ``dashboard`` 插进 ``sys.path``）→ ``import api_advisor``
"""
from __future__ import annotations

try:  # ① 包上下文：dashboard.modules.advisor.api
    from ... import api_advisor as _impl
except Exception:  # noqa: BLE001 - ② 裸名上下文（plugin_api 的 sys.path 方式）
    import api_advisor as _impl  # type: ignore[no-redef]

#: 模块 id，与同目录 ``manifest.json`` 的 ``id`` 一致。
MODULE_ID = "advisor"

#: 直接复用现有实现的路由对象（不复制、不包装）。
router = _impl.router

__all__ = ["MODULE_ID", "router"]

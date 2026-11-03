# -*- coding: utf-8 -*-
"""autogroup 模块 · 后端薄封装（**不含逻辑**）。

路由实现仍在 ``dashboard/api_autogroup.py``。本文件只做"目录即模块"的接线，逻辑一行不搬；
``default_enabled=false`` 是因为该分区会写建议记录（扫描/采纳），按"纯只读默认开、带写默认关"的
既定策略出厂关闭，需要它的 agent 自己打开。

导入方式两条都要能走通（理由见 ``modules/overview/api.py``）：

1. 包上下文（``dashboard.modules.autogroup.api``）→ ``from ... import api_autogroup``
2. 裸名上下文（``plugin_api.py`` 把 ``dashboard`` 插进 ``sys.path``）→ ``import api_autogroup``
"""
from __future__ import annotations

try:  # ① 包上下文：dashboard.modules.autogroup.api
    from ... import api_autogroup as _impl
except Exception:  # noqa: BLE001 - ② 裸名上下文（plugin_api 的 sys.path 方式）
    import api_autogroup as _impl  # type: ignore[no-redef]

#: 模块 id，与同目录 ``manifest.json`` 的 ``id`` 一致。
MODULE_ID = "autogroup"

#: 直接复用现有实现的路由对象（不复制、不包装）。
router = _impl.router

__all__ = ["MODULE_ID", "router"]

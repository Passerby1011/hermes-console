# -*- coding: utf-8 -*-
"""weather 模块 · 后端薄封装（**不含逻辑**）。

路由实现仍然在 ``dashboard/api_weather.py``（C4 搬进来的那份）。本文件只做"目录即模块"
的接线，理由与 ``modules/overview/api.py`` 相同（见那里的说明）。
"""
from __future__ import annotations

try:  # ① 包上下文：dashboard.modules.weather.api
    from ... import api_weather as _impl
except Exception:  # noqa: BLE001 - ② 裸名上下文（plugin_api 的 sys.path 方式）
    import api_weather as _impl  # type: ignore[no-redef]

#: 模块 id，与同目录 ``manifest.json`` 的 ``id`` 一致。
MODULE_ID = "weather"

#: 直接复用现有实现的路由对象（不复制、不包装）。
router = _impl.router

__all__ = ["MODULE_ID", "router"]

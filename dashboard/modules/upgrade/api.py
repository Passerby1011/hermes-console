# -*- coding: utf-8 -*-
"""upgrade 模块 · 后端薄封装（**不含逻辑**）。

路由实现仍在 ``dashboard/api_upgrade.py``（目前只有 ``GET /upgrade`` 一条占位；C5 作废后未重做）。
本文件只做"目录即模块"的接线，``default_enabled=false``：占位分区默认不出现在导航里，
等真正的升级域重做后再由设置打开。

导入方式两条都要能走通（理由见 ``modules/overview/api.py``）：

1. 包上下文（``dashboard.modules.upgrade.api``）→ ``from ... import api_upgrade``
2. 裸名上下文（``plugin_api.py`` 把 ``dashboard`` 插进 ``sys.path``）→ ``import api_upgrade``
"""
from __future__ import annotations

try:  # ① 包上下文：dashboard.modules.upgrade.api
    from ... import api_upgrade as _impl
except Exception:  # noqa: BLE001 - ② 裸名上下文（plugin_api 的 sys.path 方式）
    import api_upgrade as _impl  # type: ignore[no-redef]

#: 模块 id，与同目录 ``manifest.json`` 的 ``id`` 一致。
MODULE_ID = "upgrade"

#: 直接复用现有实现的路由对象（不复制、不包装）。
router = _impl.router

__all__ = ["MODULE_ID", "router"]

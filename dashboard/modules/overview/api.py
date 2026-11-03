# -*- coding: utf-8 -*-
"""overview 模块 · 后端薄封装（**不含逻辑**）。

路由实现仍然在 ``dashboard/api_overview.py``（C3 搬进来的那份），本文件只把它挂到
"目录即模块"的新结构上：``manifest.json`` 的 ``api_module`` 指向同一个模块名，
``router`` 就是它的 router。

为什么这么薄：M1b 只验证"删掉目录即下线"成立，**不搬逻辑**。真正的逻辑迁移是后续卡片的事；
在那之前两份引用指向同一份实现，不存在两套行为漂移的风险。

导入方式有两条，两条都要能走通：

1. 作为包导入（``dashboard.modules.overview.api``）→ ``from ... import api_overview``
   （``...`` = ``dashboard``）
2. 裸名导入（``plugin_api.py`` 把 ``dashboard`` 插进 ``sys.path`` 后 import 本文件）→
   直接 ``import api_overview``，此时它通常已经在 ``sys.modules`` 里
"""
from __future__ import annotations

try:  # ① 包上下文：dashboard.modules.overview.api
    from ... import api_overview as _impl
except Exception:  # noqa: BLE001 - ② 裸名上下文（plugin_api 的 sys.path 方式）
    import api_overview as _impl  # type: ignore[no-redef]

#: 模块 id，与同目录 ``manifest.json`` 的 ``id`` 一致。
MODULE_ID = "overview"

#: 直接复用现有实现的路由对象（不复制、不包装）。
router = _impl.router

__all__ = ["MODULE_ID", "router"]

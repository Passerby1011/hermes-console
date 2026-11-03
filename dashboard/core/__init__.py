# -*- coding: utf-8 -*-
"""hermes-console · 配置分层核心（M1a）。

三个模块：

* :mod:`dashboard.core.scope`    —— agent(=profile) → 路径解析 + 只读加载配置
* :mod:`dashboard.core.settings` —— 分层合并（模块默认 < 全局 < 该 agent）+ 官方写入器
* :mod:`dashboard.core.registry` —— 模块注册核心（M1b）：manifest 发现 / 启用集 / 故障隔离
* :mod:`dashboard.core.plugin_catalog` —— 自制插件目录（M5a）：catalog ∪ enabled ∪ disabled ∪ entries

**为什么单独一个包**：模块化平台的地基。M1b（模块注册）与 M4（设置界面）都从这里取
"某个 agent 看到什么值、这个值是哪来的"，所以这里的接口签名要稳，不要临时拼。

**官方机制复用**（不许自己发明，见 ``settings.py`` 顶部注释）：

* 写：``hermes_cli.plugins_state.save_plugin_setting``
* 作用域：``hermes_cli.web_server_profiles._config_profile_scope``
* 读：``hermes_cli.config.read_config_doc``（指定路径的原始读）/ ``hermes_cli.plugins_state._plugin_settings_entry``

本包只读 + 只走官方写入器，**绝不**手写 YAML、绝不动真实 ``config.yaml``。
"""

from __future__ import annotations

__all__ = ["plugin_catalog", "registry", "scope", "settings"]

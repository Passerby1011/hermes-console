# -*- coding: utf-8 -*-
"""本卡自用：列出插件全部 /autogroup* 路由（真 import plugin_api，与生产同款加载方式）。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

DASHBOARD = Path(r"D:/hermes-data/hermes/plugins/hermes-console/dashboard")
API = DASHBOARD / "plugin_api.py"
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_hermes-console", API)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

rows = []
for r in mod.router.routes:
    path = getattr(r, "path", "")
    methods = sorted(getattr(r, "methods", []) or [])
    if "/autogroup" in path:
        rows.append((path, ",".join(methods), getattr(r, "name", "")))
rows.sort()
for path, methods, name in rows:
    print("%-6s %-58s %s" % (methods, path, name))
print("TOTAL_AUTOGROUP_ROUTES", len(rows))
print("TOTAL_ROUTES", len(mod.router.routes))
print("LOADED", mod.LOADED if hasattr(mod, "LOADED") else "")

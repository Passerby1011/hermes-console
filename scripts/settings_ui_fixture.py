# -*- coding: utf-8 -*-
"""设置分区 UI 验收夹具：把**真实的** ``dashboard/plugin_api.py`` 起在指定端口上。

与 ``remote_ui_fixture.py`` 的差别：那个要把 home 指到沙盒（因为要启停真进程）；
设置分区是**纯读**（GET /agents、GET /agents/<id>/plugins、GET /agents/<id>/modules），
所以这里直接打**真实 HERMES_HOME**：截图里的 agent 列表、插件三态、版本号、耗时
全是这台机器上的真数据，不是桩数据。

只读保证：本夹具不提供任何写路由的额外逻辑，也不改 home；前端探针只点"选 agent"。

跑法（由 ``scripts/probe-settings-ui.mjs`` 自动拉起，也可手工）::

    <venv>/python.exe plugins/hermes-console/scripts/settings_ui_fixture.py --port 9189
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
import threading
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
HOME = PLUGIN_ROOT.parents[1]
HERMES_AGENT = HOME / "hermes-agent"
for _p in (str(HERMES_AGENT), str(DASHBOARD)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PREFIX = "/api/plugins/hermes-console"


def build_app():
    from fastapi import FastAPI

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_hermes_console", str(DASHBOARD / "plugin_api.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix=PREFIX)
    return app, module


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9189)
    args = parser.parse_args(argv)

    import uvicorn

    app, module = build_app()
    failed = getattr(module, "DOMAIN_FAILURES", {}) or {}
    print("[fixture] home=%s routes=%d failed=%s" % (HOME, len(module.router.routes), failed or "{}"), flush=True)
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="error", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            print("[fixture] ready on 127.0.0.1:%d%s" % (args.port, PREFIX), flush=True)
            break
        time.sleep(0.05)
    else:
        print("[fixture] uvicorn 没起来", flush=True)
        return 2
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

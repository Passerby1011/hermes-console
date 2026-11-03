# -*- coding: utf-8 -*-
"""hermes-console · 远端后端分区（V4b）**UI 验收夹具**：给真前端产物喂真后端路由。

跑法（由 ``scripts/probe-remote-ui.mjs`` 自动拉起，也可手工）::

    <venv>/python.exe plugins/hermes-console/scripts/remote_ui_fixture.py --port 9188

它做的事：

1. 在临时目录造一个**沙盒 home**（4 个 profile + 顶层 ``.env`` 里的账号/密码哨兵），
   并把 ``plugins/hermes-remote/ports.json`` 指到 9180~9184 —— 避开真实 serve 的 9119，
   否则"default 在跑"会变成"真实进程被拍到截图里"。
2. 按 dashboard 的方式加载**真实的** ``dashboard/plugin_api.py``（不复制任何路由代码），
   把 ``api_remote._home`` 指向沙盒。
3. 唯一的两处替换，都是为了"不起真进程"：
   * ``_run_script`` → 假启动器：start 就在该端口起一个只会回 ``/api/status`` 的小 HTTP 服务，
     stop 就关掉它。**端口真的在听**，所以 status/pid/uptime/回读全是真的；
   * 无（``_listeners`` / ``_probe`` 保持原样，走真 netstat + 真 HTTP）。
4. 预置几条事件台账（一次启动、一次停止、一次失败），让"最近启动/最近停止/最近错误"
   三列在截图里有真实内容（格式与生产完全一致）。
5. 暴露 ``GET /__log``：收到的 (method, path) 列表 —— UI 探针靠它证明
   "点「启动」到「确认」之间一个请求都没发"。

**它不是生产代码**：只在验收时跑，跑完进程就退。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
REAL_HOME = PLUGIN_ROOT.parents[1]
PREFIX = "/api/plugins/hermes-console"

#: 沙盒里的 4 个 profile（+ default 共 5 行：一屏放得下，且 ≥3 的验收有余量）。
PROFILES = (
    ("alpha", "小墨 · 内容"),
    ("beta", "工作号"),
    ("gamma", "研究助理"),
    ("delta", "媒体流水线"),
)
#: 沙盒端口：刻意远离 9119（真实 serve 正在监听），避免截图拍到真进程。
PORT_BASE = 9180
#: 哨兵凭据（截图里会出现账号名，密码默认掩码；这两个值只用于断言，不打印）。
SENTINEL_USER = "hermes-demo"
SENTINEL_PASSWORD = "demo-pass-9f3a1c2b4d5e6f708192a3b4"


class _StatusHandler(BaseHTTPRequestHandler):
    """假 serve：只回公开的 ``/api/status``（探活要的就是这个）。"""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        payload = json.dumps({"ok": True, "auth_required": True,
                              "auth_providers": ["basic"]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:  # 静音：别把探针输出搅乱
        return


class FakeLauncher:
    """``_run_script`` 的替身：按端口起/停假 serve（端口真的在听，判活逻辑不用改）。"""

    def __init__(self) -> None:
        self.servers: Dict[int, ThreadingHTTPServer] = {}
        self.calls: List[List[str]] = []

    def _start(self, port: int) -> None:
        if port in self.servers:
            return
        server = ThreadingHTTPServer(("127.0.0.1", port), _StatusHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers[port] = server

    def _stop(self, port: int) -> None:
        server = self.servers.pop(port, None)
        if server is not None:
            server.shutdown()
            server.server_close()

    def __call__(self, *args: str) -> Dict[str, Any]:
        self.calls.append(list(args))
        action = args[0] if args else "status"
        if action == "start":
            agent = args[1] if len(args) > 1 else "default"
            port = int(args[2]) if len(args) > 2 else PORT_BASE
            self._start(port)
            return {"ok": True, "profile": agent, "port": port, "pid": 4242,
                    "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        if action == "stop":
            agent = args[1] if len(args) > 1 else "default"
            for port in list(self.servers):
                self._stop(port)
            return {"ok": True, "profile": agent, "killed": [4242], "still_listening": []}
        return {"ok": True, "agents": []}


def build_sandbox(home: Path) -> None:
    """造一个干净沙盒（幂等：已存在就只补缺失文件）。"""
    (home / "profiles").mkdir(parents=True, exist_ok=True)
    if not (home / "config.yaml").is_file():
        (home / "config.yaml").write_text("plugins: {}\n", encoding="utf-8")
    # 顶层 .env：所有 agent 共享同一套账号（界面会显示"共享"来源）
    (home / ".env").write_text(
        "HERMES_DASHBOARD_BASIC_AUTH_USERNAME=%s\n"
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=%s\n"
        % (SENTINEL_USER, SENTINEL_PASSWORD),
        encoding="utf-8",
    )
    for name, title in PROFILES:
        profile = home / "profiles" / name
        profile.mkdir(parents=True, exist_ok=True)
        if not (profile / "config.yaml").is_file():
            (profile / "config.yaml").write_text("plugins: {}\n", encoding="utf-8")
        (profile / "profile.yaml").write_text("title: %s\n" % title, encoding="utf-8")
    # beta 自己配一套账号（截图里能看到"独立凭据"和不同的账号名）
    (home / "profiles" / "beta" / ".env").write_text(
        "HERMES_DASHBOARD_BASIC_AUTH_USERNAME=work-account\n"
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=%s\n" % SENTINEL_PASSWORD,
        encoding="utf-8",
    )

    remote_plugin = home / "plugins" / "hermes-remote"
    remote_plugin.mkdir(parents=True, exist_ok=True)
    ports = {"default": PORT_BASE}
    for index, (name, _) in enumerate(PROFILES, start=1):
        ports[name] = PORT_BASE + index
    (remote_plugin / "ports.json").write_text(json.dumps(ports, indent=2), encoding="utf-8")
    # alpha 的 state：让"已运行 3 小时 12 分"这类读数是真的算出来的
    started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3 * 3600 - 12 * 60))
    (remote_plugin / "serve-alpha.json").write_text(
        json.dumps({"pid": 4242, "host": "0.0.0.0", "port": ports["alpha"],
                    "profile": "alpha", "started_at": started}, indent=2),
        encoding="utf-8",
    )

    # 事件台账（格式与生产一致）：一次启动、一次停止、一次失败
    data_dir = home / "plugins" / "hermes-console" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    today = time.strftime("%Y-%m-%d")
    lines = [
        {"at": started, "agent": "alpha", "action": "start", "ok": True, "port": ports["alpha"]},
        {"at": today + " 09:14:02", "agent": "gamma", "action": "stop", "ok": True},
        {"at": today + " 09:02:41", "agent": "delta", "action": "start", "ok": False,
         "port": ports["delta"], "error": "进程已退出，多半是端口被占或缺登录凭据"},
    ]
    (data_dir / "remote-events.jsonl").write_text(
        "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8")


def main(argv: List[str]) -> int:
    port = 9188
    home: Optional[Path] = None
    for index, item in enumerate(argv):
        if item == "--port" and index + 1 < len(argv):
            port = int(argv[index + 1])
        if item == "--home" and index + 1 < len(argv):
            home = Path(argv[index + 1]).resolve()
    if home is None:
        print("必须给 --home <沙盒目录>", file=sys.stderr)
        return 2
    home.mkdir(parents=True, exist_ok=True)
    build_sandbox(home)
    # core.scope / core.registry 读 HERMES_HOME：必须在 import plugin_api 之前指到沙盒，
    # 否则 /modules 会去扫真实 home 的 profile 清单。
    os.environ["HERMES_HOME"] = str(home)
    os.environ.pop("HERMES_PROFILE", None)

    for extra in (str(DASHBOARD), str(REAL_HOME / "hermes-agent")):
        if extra not in sys.path:
            sys.path.insert(0, extra)

    import importlib.util
    spec = importlib.util.spec_from_file_location("hc_fixture_plugin_api", DASHBOARD / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    import api_remote  # noqa: E402  (plugin_api 已按本目录 import 过同名模块)

    api_remote._home = lambda: home
    launcher = FakeLauncher()
    api_remote._run_script = launcher
    launcher._start(PORT_BASE + 1)          # alpha 起来（端口真的在听）

    from fastapi import FastAPI, Request  # noqa: E402
    from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"], expose_headers=["*"])
    request_log: List[Dict[str, str]] = []

    @app.middleware("http")
    async def _record(request: Request, call_next):  # noqa: ANN001, ANN202
        request_log.append({"method": request.method, "path": request.url.path})
        return await call_next(request)

    app.include_router(module.router, prefix=PREFIX)

    @app.get("/__log")
    def _log() -> Dict[str, Any]:
        return {"ok": True, "requests": request_log, "calls": launcher.calls}

    @app.post("/__log/reset")
    def _reset() -> Dict[str, Any]:
        request_log.clear()
        launcher.calls.clear()
        return {"ok": True}

    import uvicorn  # noqa: E402

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

# -*- coding: utf-8 -*-
"""模型分区「批量设模型」验收夹具：把**真实的** ``dashboard/plugin_api.py`` 起在指定端口。

两种模式：

* ``--home <dir>``：直接打这个 home（写操作会真的落到这个目录里）；
* ``--sandbox <dir>``：**先把真实 home 的关键文件复制进沙盒**（root ``config.yaml`` +
  每个 ``profiles/<id>/{config.yaml,profile.yaml}``），再把 ``HERMES_HOME`` 指到沙盒。
  截图里的 agent 列表、当前 provider/模型、moa 预设名全是这台机器的真数据，
  但 apply/rollback 只改沙盒里的副本 —— 真 profile 一个字节都不动。

跑法（由 ``scripts/probe-models-batch.mjs`` 自动拉起，也可手工）::

    <venv>/python.exe plugins/hermes-console/scripts/models_batch_fixture.py --port 9193 --sandbox D:/tmp/sb
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
import threading
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
REAL_HOME = PLUGIN_ROOT.parents[1]
HERMES_AGENT = REAL_HOME / "hermes-agent"

PREFIX = "/api/plugins/hermes-console"

#: 沙盒里带哪些 agent：真实 ``profiles/`` 下全部（截图里"全部 agent"才名副其实；
#: 每个只复制 config.yaml / profile.yaml / .env 三件，读起来很快）。
def _sandbox_profiles() -> tuple:
    root = REAL_HOME / "profiles"
    try:
        names = sorted(item.name for item in root.iterdir() if item.is_dir())
    except OSError:
        return ("xiaomo", "work", "media-worker")
    keep = tuple(name for name in names if not name.startswith((".", "__")))
    return keep or ("xiaomo", "work", "media-worker")


SANDBOX_PROFILES = _sandbox_profiles()


def make_sandbox(target: Path) -> Path:
    """真实 home → 沙盒副本（只复制本分区会用到的文件）。"""
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    root_cfg = REAL_HOME / "config.yaml"
    if root_cfg.is_file():
        shutil.copy2(root_cfg, target / "config.yaml")
    env = REAL_HOME / ".env"
    if env.is_file():
        shutil.copy2(env, target / ".env")
    for name in SANDBOX_PROFILES:
        source = REAL_HOME / "profiles" / name
        if not source.is_dir():
            continue
        dest = target / "profiles" / name
        dest.mkdir(parents=True, exist_ok=True)
        for filename in ("config.yaml", "profile.yaml", ".env"):
            src = source / filename
            if src.is_file():
                shutil.copy2(src, dest / filename)
    return target


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
    parser.add_argument("--port", type=int, default=9193)
    parser.add_argument("--home", default="")
    parser.add_argument("--sandbox", default="")
    args = parser.parse_args(argv)

    if args.sandbox:
        home = make_sandbox(Path(args.sandbox))
        os.environ["HERMES_HOME"] = str(home)
    elif args.home:
        home = Path(args.home)
        os.environ["HERMES_HOME"] = str(home)
    else:
        home = REAL_HOME

    for path in (str(HERMES_AGENT), str(DASHBOARD)):
        if path not in sys.path:
            sys.path.insert(0, path)

    import uvicorn

    app, module = build_app()
    failed = getattr(module, "DOMAIN_FAILURES", {}) or {}
    print("[fixture] home=%s routes=%d failed=%s" % (home, len(module.router.routes), failed or "{}"),
          flush=True)
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

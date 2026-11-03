# -*- coding: utf-8 -*-
"""hermes-console · ProviderModelPicker UI 验收夹具（t_4d654faf）。

真插件后端（`dashboard/plugin_api.py` 原样加载）+ 沙盒 home（3 个 agent × 3 个 provider，
模型清单各不相同），用 uvicorn 起在 127.0.0.1:<port>，前缀 `/api/plugins/hermes-console`。

跑法（由 probe-model-picker-ui.mjs 拉起，不单独用）::

    python picker_ui_fixture.py --port 9391 --sandbox <tmp>/home
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN / "dashboard"
HERMES_AGENT = Path(__file__).resolve().parents[3] / "hermes-agent"
PREFIX = "/api/plugins/hermes-console"

AGENTS = ("alpha", "beta", "gamma")
PROVIDERS = ("palpha", "pbeta", "pgamma")


def agent_config(name: str) -> str:
    lines = ["model:", "  default: %s-main" % name, "  provider: p%s" % name, "providers:"]
    for provider in PROVIDERS:
        lines.append("  %s:" % provider)
        lines.append("    base_url: https://%s.example/v1" % provider)
        lines.append("    models:")
        if provider == "pgamma":
            names = ("%s-pgamma-only" % name,)
        elif provider == "p%s" % name:
            names = ("%s-main" % name, "%s-alt" % name, "%s-%s-only" % (name, provider))
        else:
            names = ("%s-main" % name, "%s-alt" % name, "%s-%s-only" % (name, provider))
        for model in names:
            lines.append("      %s: {}" % model)
    lines += [
        "auxiliary:",
        "  approval:",
        "    provider: p%s" % name,
        "    model: %s-main" % name,
        "    reasoning_effort: none",
        "  title_generation:",
        "    provider: auto",
        "    model: ''",
        "  compression:",
        "    provider: p%s" % name,
        "    model: %s-alt" % name,
    ]
    return "\n".join(lines) + "\n"


def build_home(sandbox: Path) -> None:
    shutil.rmtree(sandbox, ignore_errors=True)
    (sandbox / "profiles").mkdir(parents=True, exist_ok=True)
    (sandbox / "config.yaml").write_text(agent_config("default"), encoding="utf-8")
    (sandbox / ".env").write_text("SANDBOX_PICKER_KEY=sbx-never-print-me\n", encoding="utf-8")
    for name in AGENTS:
        profile = sandbox / "profiles" / name
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "profile.yaml").write_text("title: 沙盒%s\n" % name, encoding="utf-8")
        (profile / "config.yaml").write_text(agent_config(name), encoding="utf-8")


def load_plugin_api():
    for extra in (str(DASHBOARD), str(HERMES_AGENT)):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_hermes_console", DASHBOARD / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--sandbox", required=True)
    args = parser.parse_args()
    sandbox = Path(args.sandbox)

    build_home(sandbox)
    import os

    os.environ["HERMES_HOME"] = str(sandbox)

    module = load_plugin_api()
    import api_models  # noqa: E402

    api_models._home = lambda: sandbox

    from fastapi import FastAPI  # noqa: E402
    import uvicorn  # noqa: E402

    app = FastAPI()

    @app.get(PREFIX + "/pmp-health")
    def pmp_health() -> dict:
        # 独立探活路径（**先**注册，避免被 plugin_api 自己的 /health 抢走）：
        # 端口被别的夹具占用时，探针必须能分辨"起来的是不是我这个夹具"。
        return {"ok": True, "sandbox": str(sandbox), "agents": list(AGENTS),
                "home": str(api_models._home())}

    app.include_router(module.router, prefix=PREFIX)

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())

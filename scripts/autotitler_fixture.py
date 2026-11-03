# -*- coding: utf-8 -*-
"""自动命名面板验收夹具：把**真实的** ``dashboard/plugin_api.py`` 起在指定端口。

沙盒里造的是一个形状真实但**内容确定**的 home：插件目录（真 ``plugin.yaml`` 的 23 键
``config_schema`` + 一份 0.2 存量 ``config.yaml``）、宿主 ``plugins.entries`` 的两层
settings、``plugin-data/.../state.json``、以及一段与插件真实输出逐字同形的 ``agent.log``。

为什么不用真 home：真日志每几分钟就变一次，"失败 3 条"这类断言没法稳定。真 home 的口径由
``scripts/probe_autotitler.py``（只读、与 ``grep`` 对账）负责，本夹具只负责让浏览器端
可复现地看到每一种状态。

跑法（``scripts/probe-autotitler.mjs`` 会自动拉起）::

    <venv>/python.exe plugins/hermes-console/scripts/autotitler_fixture.py --port 9197 --sandbox D:/tmp/sb
"""
from __future__ import annotations

import argparse
import importlib.util
import json
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
REAL_PLUGIN = REAL_HOME / "plugins" / "hermes-auto-titler"

PREFIX = "/api/plugins/hermes-console"

ROOT_CONFIG = """\
plugins:
  enabled:
    - hermes-auto-titler
  entries:
    hermes-auto-titler:
      settings:
        every_n_turns: 5
        strategy: aggressive
"""

PLUGIN_CONFIG = """\
enabled: true
every_n_turns: 2
on_close: true
min_interval_minutes: 5
max_title_length: null
max_display_width: 40
strategy: conservative
"""

#: 与插件真实输出逐字同形：invalid payload 的 WARNING 在 audit 行**之前**。
#:
#: 时间戳**相对 now 生成**（不是写死的日期）：面板现在按时间区间统计，「最近 1 小时 /
#: 24 小时 / 全部历史」三个口径必须给出**不同**的数字，写死日期会让探针隔天就红。
#: 三段设计（讲的就是这个插件的真实故事：以前一直失败，修好之后不再失败）：
#:
#:   · ``-30h``（只在「全部历史」里）：2 次失败 + 1 次跳过 —— 历史账
#:   · ``-9h``（24 小时窗口的边界内）：3 次失败（2 invalid payload + 1 model call error）
#:     + 1 改名 + 2 跳过 + 1 keep + 1 throttled
#:   · ``-10min``（1 小时窗口里）：2 改名 + 3 keep，**零失败**
#:
#: 于是三个口径分别是：1h = 0% 失败、24h = 3/6 = 50%、all = 5/8 = 62.5%。
def _ts(offset_seconds: float) -> str:
    import datetime as _dt

    moment = _dt.datetime.now() + _dt.timedelta(seconds=offset_seconds)
    return moment.strftime("%Y-%m-%d %H:%M:%S") + ",000"


def synthetic_log() -> str:
    hour = 3600.0
    rows = [
        # —— 插件 register 证据（解析层不按窗口筛它）——
        "%s INFO hermes_auto_titler.titler: hermes-auto-titler: coexistence mode "
        "(first_title_mode=builtin); host title generation left untouched" % _ts(-40 * hour),
        # —— 历史带：30 小时前（只在「全部历史」里）——
        "%s WARNING hermes_auto_titler.titler: auto-titler old-a: "
        "model returned invalid decision payload: '好的，我这就开始'" % _ts(-30 * hour),
        "%s INFO hermes_auto_titler.titler: auto-titler audit: sid=old-a act=failed "
        "reason=model call failed dur=9000ms" % _ts(-30 * hour),
        "%s INFO hermes_auto_titler.titler: auto-titler audit: sid=old-b act=failed "
        "reason=model call failed dur=7000ms" % _ts(-30 * hour),
        "%s INFO hermes_auto_titler.titler: auto-titler audit: sid=old-c act=skipped "
        "reason=no messages dur=4ms" % _ts(-30 * hour),
        # —— 修复前那一批：9 小时前 ——
        "%s WARNING hermes_auto_titler.titler: auto-titler 20261029_204: "
        "model returned invalid decision payload: '两张卡已经在看板上跑着，当前状态：'" % _ts(-9 * hour),
        "%s WARNING hermes_auto_titler.titler: auto-titler 20261029_204: recorded failure "
        "(attempt 1, class=temporary, next retry in 30s)" % _ts(-9 * hour),
        "%s INFO hermes_auto_titler.titler: auto-titler audit: sid=20261029 act=failed "
        "reason=model call failed dur=17080ms" % _ts(-9 * hour),
        "%s WARNING hermes_auto_titler.titler: auto-titler 20261029_205: "
        "model returned invalid decision payload: '现在没有新的指令进来'" % _ts(-9 * hour),
        "%s INFO hermes_auto_titler.titler: auto-titler audit: sid=20261029 act=failed "
        "reason=model call failed dur=9980ms" % _ts(-9 * hour),
        "%s INFO hermes_auto_titler.titler: auto-titler local-ee6: llm result action=error "
        "title=None input=current=yes" % _ts(-9 * hour),
        "%s WARNING hermes_auto_titler.titler: auto-titler local-ee6: recorded failure "
        "(attempt 3, class=temporary, next retry in 60s)" % _ts(-9 * hour),
        "%s INFO hermes_auto_titler.titler: auto-titler audit: sid=local-ee act=failed "
        "reason=model call failed dur=7084ms" % _ts(-9 * hour),
        "%s INFO hermes_auto_titler.titler: auto-titler audit: sid=20261030 act=skipped "
        "reason=no messages dur=4ms" % _ts(-9 * hour),
        "%s INFO hermes_auto_titler.titler: auto-titler audit: sid=20261030 act=skipped "
        "reason=no messages dur=5ms" % _ts(-9 * hour),
        "%s INFO hermes_auto_titler.titler: auto-titler audit: sid=20261028 act=renamed "
        "reason=给墨墨绑定QQ通知渠道 dur=2100ms" % _ts(-9 * hour),
        "%s INFO hermes_auto_titler.titler: auto-titler audit: sid=20261027 act=keep "
        "reason=标题仍然准确 dur=1800ms" % _ts(-9 * hour),
        "%s INFO hermes_auto_titler.titler: auto-titler audit: sid=20261026 act=throttled "
        "reason=min interval dur=3ms" % _ts(-9 * hour),
        # —— 修好之后：10 分钟前，零失败 ——
        "%s INFO hermes_auto_titler.titler: auto-titler audit: sid=now-a act=renamed "
        "reason=自动命名面板验收 dur=1900ms" % _ts(-600),
        "%s INFO hermes_auto_titler.titler: auto-titler audit: sid=now-b act=renamed "
        "reason=看板任务收尾 dur=1700ms" % _ts(-600),
        "%s INFO hermes_auto_titler.titler: auto-titler audit: sid=now-c act=keep "
        "reason=标题仍然准确 dur=1500ms" % _ts(-600),
        "%s INFO hermes_auto_titler.titler: auto-titler audit: sid=now-d act=keep "
        "reason=标题仍然准确 dur=1400ms" % _ts(-600),
        "%s INFO hermes_auto_titler.titler: auto-titler audit: sid=now-e act=keep "
        "reason=标题仍然准确 dur=1300ms" % _ts(-600),
    ]
    return "\n".join(rows) + "\n"

STATE_JSON = {
    "version": 1,
    "sessions": {
        "20261028_211250_e9d8b2": {
            "base_title": "给墨墨绑定QQ通知渠道",
            "base_title_known": True,
            "expected_title": "给墨墨绑定QQ通知渠道",
            "kind": "counter",
            "rename_count": 1,
        },
        "local-ee68e367c002b7fcfd808c77ed55f147": {
            "base_title": "自动对话命名插件",
            "base_title_known": True,
            "expected_title": "自动对话命名插件：补可见性",
            "finalize": {
                "attempts": 10,
                "capacity": False,
                "class_attempts": 10,
                "close_epoch": 1,
                "next_retry_at": 1793365912.5,
                "queued_at": 1793345368.1,
                "reason": "finalize",
                "retry_class": "temporary",
            },
            "finalize_intent": True,
            "kind": "finalize",
            "rename_count": 0,
        },
        "20261029_204511_abcd": {
            "base_title": "接手看板",
            "base_title_known": True,
            "expected_title": "接手看板与自动会话归组修复",
            "kind": "finalize+retry",
            "rename_count": 2,
            "retry": {
                "attempts": 2,
                "class_attempts": 2,
                "next_retry_at": 1793369720.0,
                "queued_at": 1793345000.0,
                "reason": "error",
                "retry_class": "temporary",
            },
        },
    },
}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_sandbox(target: Path) -> Path:
    target = Path(target)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    (target / "profiles").mkdir(parents=True)

    _write(target / "config.yaml", ROOT_CONFIG)

    plugin_dir = target / "plugins" / "hermes-auto-titler"
    plugin_dir.mkdir(parents=True)
    manifest = REAL_PLUGIN / "plugin.yaml"
    if manifest.is_file():
        shutil.copy2(manifest, plugin_dir / "plugin.yaml")
    _write(plugin_dir / "config.yaml", PLUGIN_CONFIG)

    _write(target / "plugin-data" / "hermes-auto-titler" / "state.json",
           json.dumps(STATE_JSON, ensure_ascii=False))
    _write(target / "logs" / "agent.log", synthetic_log())

    # 真机上插件跑在**某个 profile 进程**里，日志与 state.json 都在该 profile 的 home 下。
    # 沙盒把同一份数据也放到 code-worker 下：面板按作用域取数，两个 agent 都得有东西看，
    # 否则（真机上最常见的坑）面板会显示"零失败"，看起来像坏了。
    profile = target / "profiles" / "code-worker"
    profile.mkdir(parents=True)
    _write(profile / "config.yaml", "plugins:\n  enabled:\n    - hermes-auto-titler\n")
    _write(profile / "profile.yaml", "title: code-worker\n")
    _write(profile / "plugin-data" / "hermes-auto-titler" / "state.json",
           json.dumps(STATE_JSON, ensure_ascii=False))
    _write(profile / "logs" / "agent.log", synthetic_log())

    bare = target / "profiles" / "bare-worker"
    bare.mkdir(parents=True)
    _write(bare / "profile.yaml", "title: bare-worker\n")
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
    parser.add_argument("--port", type=int, default=9197)
    parser.add_argument("--sandbox", default="")
    args = parser.parse_args(argv)

    if args.sandbox:
        home = make_sandbox(Path(args.sandbox))
    else:
        home = REAL_HOME
    os.environ["HERMES_HOME"] = str(home)

    for path in (str(HERMES_AGENT), str(DASHBOARD)):
        if path not in sys.path:
            sys.path.insert(0, path)

    import uvicorn

    app, module = build_app()
    failed = getattr(module, "DOMAIN_FAILURES", {}) or {}
    print("[fixture] home=%s routes=%d failed=%s"
          % (home, len(module.router.routes), failed or "{}"), flush=True)
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="error", access_log=False)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
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

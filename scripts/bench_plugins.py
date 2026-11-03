# -*- coding: utf-8 -*-
"""设置页 /agents/{id}/plugins 并发与耗时实测台（真 uvicorn + 真 HTTP 回环）。

用法::

    python bench_plugins.py            # 单发冷/热 + 6 并发 + 每 agent 单发
    python bench_plugins.py --profile  # 附加一次 cProfile 热点表

不起桌面端、不打 9119（那条路要 dashboard 原生票据）；这里把插件自己的
``dashboard/plugin_api.py`` 按生产同样的方式挂进一个 FastAPI app，用 uvicorn 跑在
127.0.0.1:8781 上，再用真 HTTP 打。sync handler 因此仍然落在 FastAPI 的 threadpool 里，
并发行为与生产一致（这正是 503 / 串行化的现场）。
"""
from __future__ import annotations

import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]          # <home>/plugins/hermes-console
HOME = PLUGIN_ROOT.parents[1]                              # <home>
HERMES_AGENT = HOME / "hermes-agent"
for p in (str(HERMES_AGENT), str(PLUGIN_ROOT / "dashboard")):
    if p not in sys.path:
        sys.path.insert(0, p)

PREFIX = "/api/plugins/hermes-console"
PORT = 8781
AGENTS = ["default", "apm-worker", "cad-worker", "code-worker", "media-worker", "research-worker"]


def build_app():
    import importlib.util

    from fastapi import FastAPI

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_hermes_console", str(PLUGIN_ROOT / "dashboard" / "plugin_api.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix=PREFIX)
    return app, module


def serve(app):
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("uvicorn 没起来")


def hit(agent, timeout=60.0):
    url = "http://127.0.0.1:%d%s/agents/%s/plugins" % (PORT, PREFIX, agent)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
            code = resp.status
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也当作一次可测量结果
        body = exc.read()
        code = exc.code
    except Exception as exc:  # noqa: BLE001 - 超时/连接错误也要记
        return {"agent": agent, "code": "ERR:%s" % type(exc).__name__, "ms": (time.perf_counter() - started) * 1000.0,
                "plugins": None, "error": str(exc)[:160]}
    elapsed = (time.perf_counter() - started) * 1000.0
    count = None
    error = None
    try:
        data = json.loads(body.decode("utf-8"))
        if isinstance(data, dict) and data.get("ok"):
            count = len((data.get("data") or {}).get("plugins") or [])
        else:
            error = json.dumps((data or {}).get("error"), ensure_ascii=False)[:200]
    except Exception:  # noqa: BLE001
        error = body[:120].decode("utf-8", "replace")
    return {"agent": agent, "code": code, "ms": elapsed, "plugins": count, "error": error}


def table(title, rows):
    print("\n== " + title)
    print("  %-16s %-8s %10s  %s" % ("agent", "status", "耗时(ms)", "plugins/错误"))
    for row in rows:
        print("  %-16s %-8s %10.1f  %s" % (
            row["agent"], row["code"], row["ms"],
            row.get("plugins") if row.get("plugins") is not None else (row.get("error") or "")))
    ok = [r for r in rows if r["code"] == 200]
    bad = [r for r in rows if r["code"] != 200]
    times = [r["ms"] for r in ok]
    if times:
        print("  --- 成功 %d / %d；min %.1f  median %.1f  max %.1f  total %.1f ms" % (
            len(ok), len(rows), min(times), statistics.median(times), max(times), sum(times)))
    if bad:
        print("  !!! 失败 %d 个：%s" % (len(bad), [r["code"] for r in bad]))
    return len(bad) == 0


def concurrent(agents):
    results = [None] * len(agents)
    barrier = threading.Barrier(len(agents))

    def work(index, agent):
        barrier.wait()          # 尽量让 6 个请求同时进站
        results[index] = hit(agent)

    threads = [threading.Thread(target=work, args=(i, a)) for i, a in enumerate(agents)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = (time.perf_counter() - started) * 1000.0
    return results, wall


def main():
    profile = "--profile" in sys.argv
    app, module = build_app()
    print("[env] plugin     :", PLUGIN_ROOT)
    print("[env] home       :", HOME)
    print("[env] python     :", sys.version.split()[0])
    print("[env] routes     :", len(module.router.routes), "failed=", getattr(module, "DOMAIN_FAILED", None))
    serve(app)
    print("[env] uvicorn    : 127.0.0.1:%d%s" % (PORT, PREFIX))

    cold = hit("default")
    table("单发 · 冷（进程内第一次打 default）", [cold])
    hot = [hit("default") for _ in range(5)]
    table("单发 · 热（同一 agent 连续 5 次）", hot)

    per_agent = [hit(a) for a in AGENTS]
    table("逐 agent 串行（每个 agent 各 1 次）", per_agent)

    rows, wall = concurrent(AGENTS)
    ok6 = table("6 个 agent 并发（barrier 同时发）", rows)
    print("  --- 并发墙钟 %.1f ms" % wall)

    rows2, wall2 = concurrent(["default"] * 6)
    ok6b = table("同一 agent 6 并发（争用同一份 config.yaml）", rows2)
    print("  --- 并发墙钟 %.1f ms" % wall2)

    if profile:
        import cProfile
        import pstats

        pr = cProfile.Profile()
        pr.enable()
        hit("default")
        pr.disable()
        stats = pstats.Stats(pr)
        stats.sort_stats("cumulative")
        print("\n== cProfile 单次 /agents/default/plugins（按 cumulative 前 25）")
        stats.print_stats(25)

    print("\nRESULT: cold=%.1fms hot_median=%.1fms six_concurrent_wall=%.1fms zero_503=%s" % (
        cold["ms"], statistics.median([r["ms"] for r in hot]), wall, ok6 and ok6b))
    return 0 if (ok6 and ok6b and cold["code"] == 200) else 1


if __name__ == "__main__":
    raise SystemExit(main())

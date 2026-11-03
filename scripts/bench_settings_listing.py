# -*- coding: utf-8 -*-
"""设置页 ``/agents/{id}/plugins`` 三段式耗时实测 + 数据一致性校验（只读）。

口径说明（避免"只贴接口 200"）：
  * 冷加载 = 新进程里第一次打该端点（含 import / schema 缓存为空）；
  * 热加载 = 同一进程同 agent 连续 N 次的中位数；
  * 6 并发 = 6 个 agent 在 barrier 上同时进站，真 uvicorn + 真 HTTP 回环，
    sync handler 落在 FastAPI 的 threadpool 里（与生产同一条路），记 503 个数。
  * 数据一致性 = 与 ``baseline.json``（改造前落盘的一份完整响应）逐项 diff，
    字段名 / 三态 / 版本 / has_settings 必须一字不差。

用法::

    python scripts/bench_settings_listing.py --save-baseline   # 改造前落基线
    python scripts/bench_settings_listing.py                   # 改造后对比 + 计时
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

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HOME = PLUGIN_ROOT.parents[1]
HERMES_AGENT = HOME / "hermes-agent"
for p in (str(HERMES_AGENT), str(PLUGIN_ROOT / "dashboard")):
    if p not in sys.path:
        sys.path.insert(0, p)

PREFIX = "/api/plugins/hermes-console"
PORT = 8783
AGENTS = ["default", "apm-worker", "cad-worker", "code-worker", "media-worker", "research-worker"]
BASELINE = Path(__file__).resolve().parent / "_probe_out" / "settings_listing_baseline.json"


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
    threading.Thread(target=server.run, daemon=True).start()
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
    except urllib.error.HTTPError as exc:
        body = exc.read()
        code = exc.code
    except Exception as exc:  # noqa: BLE001
        return {"agent": agent, "code": "ERR:%s" % type(exc).__name__,
                "ms": (time.perf_counter() - started) * 1000.0, "body": None, "error": str(exc)[:160]}
    elapsed = (time.perf_counter() - started) * 1000.0
    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        parsed = None
    return {"agent": agent, "code": code, "ms": elapsed, "body": parsed}


def concurrent(agents):
    results = [None] * len(agents)
    barrier = threading.Barrier(len(agents))

    def work(index, agent):
        barrier.wait()
        results[index] = hit(agent)

    threads = [threading.Thread(target=work, args=(i, a)) for i, a in enumerate(agents)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, (time.perf_counter() - started) * 1000.0


def row(result):
    body = result.get("body") or {}
    data = body.get("data") if isinstance(body, dict) else None
    count = len(data.get("plugins") or []) if isinstance(data, dict) else None
    return (result["agent"], result["code"], result["ms"], count)


def diff_payload(new, old, agent):
    """逐项比对（键集合 + 每个插件的每个字段值）。返回差异行列表。"""
    problems = []
    if not isinstance(new, dict) or not isinstance(old, dict):
        return ["%s: 结构不可比" % agent]
    nd, od = new.get("data") or {}, old.get("data") or {}
    if set(nd) != set(od):
        problems.append("%s: data 键不同 %s" % (agent, sorted(set(nd) ^ set(od))))
    if nd.get("counts") != od.get("counts"):
        problems.append("%s: counts %r != %r" % (agent, nd.get("counts"), od.get("counts")))
    np_, op_ = nd.get("plugins") or [], od.get("plugins") or []
    if [i.get("plugin_id") for i in np_] != [i.get("plugin_id") for i in op_]:
        problems.append("%s: 插件清单顺序/成员不同" % agent)
    omap = {i.get("plugin_id"): i for i in op_}
    for item in np_:
        pid = item.get("plugin_id")
        other = omap.get(pid)
        if other is None:
            continue
        if set(item) != set(other):
            problems.append("%s/%s: 字段集合不同 %s" % (agent, pid, sorted(set(item) ^ set(other))))
        for key in item:
            if key in other and item[key] != other[key]:
                problems.append("%s/%s: %s = %r != 基线 %r" % (agent, pid, key, item[key], other[key]))
    return problems


def equivalence_check(module, agents):
    """把「扫描索引」这条新路径与「逐插件探测」这条旧路径**在同一进程里**逐项对比。

    改的只有两件事：插件目录怎么找（旧：每插件 ``is_dir`` + ``resolve``；新：一次 readdir 查表）
    和版本从哪读（旧：``read_plugin_version`` 每插件解析 manifest；新：同一份索引 + 签名缓存）。
    所以只要这两件事对**每个 agent 的每个插件**给出完全相同的答案，响应体就不可能变。
    """
    import importlib

    module = importlib.import_module("api_console_settings")
    scope = module._core("scope")
    catalog_mod = module._core("plugin_catalog")
    problems = []
    checked = 0
    for agent in agents:
        home = scope.hermes_home()
        config = scope.read_agent_config(agent)
        scan = module._plugin_scan(agent, home=home)
        for plugin_id in catalog_mod.merged_plugin_ids(agent, config=config):
            plugin = str(plugin_id)
            old_dirs = [str(p) for p in module._plugin_dirs(agent, plugin, home=home)]
            new_dirs = [str(p) for p in module._scan_dirs_for(scan, plugin)]
            if old_dirs != new_dirs:
                problems.append("%s/%s: 目录 %r != %r" % (agent, plugin, new_dirs, old_dirs))
            old_version = catalog_mod.read_plugin_version(plugin, home=home)
            # 生产路径传 fallback（就是官方那个函数），探针照同样的调用形态比
            new_version = module._plugin_version(
                plugin, scan, fallback=getattr(catalog_mod, "read_plugin_version", None)
            )
            if old_version != new_version:
                problems.append("%s/%s: 版本 %r != %r" % (agent, plugin, new_version, old_version))
            old_schema = module._plugin_schema(agent, plugin, home=home)
            new_schema = module._plugin_schema(agent, plugin, home=home, scan=scan)
            if old_schema != new_schema:
                problems.append("%s/%s: config_schema 不同" % (agent, plugin))
            checked += 1
    return checked, problems


def main():
    app, module = build_app()
    print("[env] plugin :", PLUGIN_ROOT)
    print("[env] home   :", HOME)
    print("[env] python :", sys.version.split()[0])
    print("[env] routes :", len(module.router.routes), "failed=", getattr(module, "DOMAIN_FAILED", None))
    serve(app)
    print("[env] uvicorn: 127.0.0.1:%d%s" % (PORT, PREFIX))

    # ---- 冷加载：进程内第一次打 default（缓存全空）----
    cold = hit("default")
    print("\n== 冷加载（新进程首次 / 缓存空）")
    print("   default  %s  %.1f ms  plugins=%s" % (cold["code"], cold["ms"], (cold["body"] or {}).get("data", {}).get("counts")))

    # ---- 热加载：同 agent 连续 7 次，取中位数 ----
    hot = [hit("default") for _ in range(7)]
    hot_ms = [r["ms"] for r in hot if r["code"] == 200]
    print("\n== 热加载（同进程同 agent 连续 7 次）")
    print("   min %.1f  median %.1f  max %.1f  ms" % (min(hot_ms), statistics.median(hot_ms), max(hot_ms)))

    # ---- 逐 agent ----
    per_agent = [hit(a) for a in AGENTS]
    print("\n== 逐 agent 串行")
    for agent, code, ms, count in [row(r) for r in per_agent]:
        print("   %-16s %s  %7.1f ms  plugins=%s" % (agent, code, ms, count))

    # ---- 6 并发 ----
    rows, wall = concurrent(AGENTS)
    ok = [r for r in rows if r["code"] == 200]
    print("\n== 6 个 agent 并发（barrier 同时发；真 uvicorn + threadpool）")
    for agent, code, ms, count in [row(r) for r in rows]:
        print("   %-16s %s  %7.1f ms  plugins=%s" % (agent, code, ms, count))
    print("   成功 %d/6  墙钟 %.1f ms  median %.1f ms  max %.1f ms  503=%d"
          % (len(ok), wall, statistics.median([r["ms"] for r in ok]) if ok else 0,
             max([r["ms"] for r in ok]) if ok else 0,
             len([r for r in rows if r["code"] == 503])))

    # ---- 同 agent 6 并发（争用同一份 config.yaml）----
    rows2, wall2 = concurrent(["default"] * 6)
    ok2 = [r for r in rows2 if r["code"] == 200]
    print("\n== 同一 agent 6 并发（争用同一份 config.yaml）")
    print("   成功 %d/6  墙钟 %.1f ms  median %.1f ms  503=%d"
          % (len(ok2), wall2, statistics.median([r["ms"] for r in ok2]) if ok2 else 0,
             len([r for r in rows2 if r["code"] == 503])))

    # ---- 数据一致性：新（扫描索引）vs 旧（逐插件探测），同一进程内逐项对比 ----
    checked, problems = equivalence_check(module, AGENTS)
    print("\n== 新旧路径等价性（%d 个 agent × 每个插件）" % len(AGENTS))
    if problems:
        for line in problems[:40]:
            print("   !", line)
        print("   差异 %d 处" % len(problems))
    else:
        print("   %d 项全部一致：候选目录 / 版本 / config_schema 与逐插件探测逐项相同" % checked)

    print("\nRESULT: cold=%.1fms hot_median=%.1fms six_concurrent_wall=%.1fms zero_503=%s"
          % (cold["ms"], statistics.median(hot_ms), wall,
             all(r["code"] == 200 for r in rows) and all(r["code"] == 200 for r in rows2)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""``POST /reload`` —— 就地热重载本插件的后端与前端，**不重启 serve 进程**。

## 为什么需要它

本插件有两处「改了不生效」的坑，老板反复踩：

1. **后端**：``dashboard/*.py`` 只在 serve 进程 import 时执行一次，之后模块对象固化在
   ``sys.modules`` 里。改完代码不重启 serve，界面拿到的永远是旧逻辑 —— 表现就是
   「前端改了、面板动了，但数据不对/接口还报老错」。
2. **前端**：``dashboard/dist`` 是 ``src/`` 的构建产物。改 ``src/`` 不跑 ``build.mjs``，
   浏览器加载的还是旧包 —— 表现就是「代码明明改了，界面一点没变」。

## 它怎么做到不重启

三条都是宿主公开/半公开的口子，本模块只调用、不修改宿主：

1. 从 ``request.app.router.routes`` 摘掉 ``/api/plugins/hermes-console`` 前缀的全部路由
   （FastAPI 的 ``app.router.routes`` 是 list，原地切片的写法见 ``_unmount``）；
2. 把本插件自己的裸模块从 ``sys.modules`` 摘掉（``plugin_api`` / ``api_*`` / ``autogroup_*``
   / ``core*`` / ``_layout``）。**必须自己算**：宿主不记录「谁 import 了谁」；
3. 调宿主的 ``_try_hot_mount_plugin_api('hermes-console')`` 重新扫盘 + 重新
   ``exec_module``（宿主实现里是 ``spec.loader.exec_module``，真的重跑文件，不是查缓存）。

POC（``cache/lane/gates/_poc_hot_reload.py``）已实测该路径能把 ``V1`` 换成 ``V2``。

## 安全网

重挂载失败时**不能让面板整体消失**（那比"没重载"更糟）。所以：

- 先备份旧 ``router`` 对象（Python 侧仍持有引用，模块删了也没关系）；
- 重挂载异常 → 把**整张路由表**还原成调用前的快照（不依赖模块缓存），响应里如实报 ``rolled_back``；
- 响应永远带 ``fingerprint_before/after``，前端/人可据此判断「到底换没换」。

## 不做的事

- **不杀进程、不重启网关、不重启 serve**。这正是本模块存在的意义。
- 不写 ``config.yaml``、不碰任何 profile 配置。
- 不在请求线程里做长时间构建：``build.mjs`` 只在「dist 落后于 src」时才跑，且有超时。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import sysconfig
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request  # type: ignore

router = APIRouter()

PLUGIN_ID = "hermes-console"

#: 本插件在 ``sys.modules`` 里可能出现的**裸模块名**。由 ``_own_module_names()``
#: 按磁盘上的 ``.py`` 现算，不写死清单 —— 新增一个 ``api_xxx.py`` 不用回来改这里。
_DASHBOARD_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- 指纹


def _fingerprint() -> Dict[str, Any]:
    """后端 + 前端产物的身份指纹，用来回答「这次重载到底换没换代码」。

    - ``backend``：``dashboard/**/*.py``（不含 tests）的 sha256 汇总，取前 16 位；
    - ``frontend``：``dist/index.js`` 的 sha256，取前 16 位；
    - ``dist_stale``：``src/`` 里有文件比 ``dist/index.js`` 新 ⇒ 产物落后于源码。
    """
    py_files: List[Path] = []
    for p in _DASHBOARD_DIR.rglob("*.py"):
        parts = set(p.parts)
        if "__pycache__" in parts or "tests" in parts:
            continue
        py_files.append(p)
    py_files.sort()

    h = hashlib.sha256()
    for p in py_files:
        try:
            h.update(p.relative_to(_DASHBOARD_DIR).as_posix().encode())
            h.update(p.read_bytes())
        except OSError:
            continue

    root = _DASHBOARD_DIR.parent
    dist_js = root / "dashboard" / "dist" / "index.js"
    dist_hash = ""
    dist_mtime = 0.0
    if dist_js.is_file():
        try:
            dist_hash = hashlib.sha256(dist_js.read_bytes()).hexdigest()[:16]
            dist_mtime = dist_js.stat().st_mtime
        except OSError:
            pass

    new_dist_hash = ""
    new_dist_js = _DASHBOARD_DIR / "dist" / "index.js"
    if new_dist_js.is_file() and new_dist_js != dist_js:
        try:
            new_dist_hash = hashlib.sha256(new_dist_js.read_bytes()).hexdigest()[:16]
            dist_mtime = max(dist_mtime, new_dist_js.stat().st_mtime)
        except OSError:
            pass

    src_dir = root / "src"
    newest_src = 0.0
    if src_dir.is_dir():
        for p in src_dir.rglob("*"):
            if p.suffix in (".js", ".css") and p.is_file():
                try:
                    newest_src = max(newest_src, p.stat().st_mtime)
                except OSError:
                    continue

    return {
        "backend": h.hexdigest()[:16] if py_files else "",
        "backend_files": len(py_files),
        "frontend": dist_hash or new_dist_hash,
        "dist_stale": bool(newest_src and dist_mtime and newest_src > dist_mtime),
        "src_newest_mtime": round(newest_src, 3) if newest_src else 0,
        "dist_mtime": round(dist_mtime, 3) if dist_mtime else 0,
    }


# ------------------------------------------------------------------- 模块清理


def _own_module_names() -> List[str]:
    """本插件在 ``sys.modules`` 里可能占用的裸模块名 —— 按磁盘现算。

    基线：``dashboard/`` 下所有 ``.py`` 的 stem（``api_models`` / ``autogroup_ai`` /
    ``plugin_api`` / ``_layout`` …），再加 ``core`` 包及其子模块。

    **为什么按磁盘算而不是写死**：写死清单会在新增 ``api_xxx.py`` 后静默漏清，
    于是「改了新模块不生效」这类 bug 会以「重启按钮没用」的形式再现。
    """
    stems = set()
    for p in _DASHBOARD_DIR.glob("*.py"):
        stems.add(p.stem)
    core_dir = _DASHBOARD_DIR / "core"
    names = set(stems)
    names.add("core")
    if core_dir.is_dir():
        for p in core_dir.glob("*.py"):
            if p.stem != "__init__":
                names.add(f"core.{p.stem}")
    # 只保留真的在 sys.modules 里的（不制造幻影条目）
    return sorted(n for n in names if n in sys.modules)


def _purge_own_modules() -> List[str]:
    """把本插件的裸模块从 ``sys.modules`` 摘掉，返回真的摘掉了哪些。"""
    removed: List[str] = []
    for name in _own_module_names():
        try:
            sys.modules.pop(name, None)
            removed.append(name)
        except Exception:  # noqa: BLE001 - 摘不掉不该中断重载
            continue
    # 宿主挂载时用的合成模块名
    synth = f"hermes_dashboard_plugin_{PLUGIN_ID}"
    if synth in sys.modules:
        sys.modules.pop(synth, None)
        removed.append(synth)
    return removed


# --------------------------------------------------------------------- 路由摘除


def _api_prefixes() -> List[str]:
    """本插件可能的路由前缀。

    宿主的 ``_mount_one_plugin_api`` 用 ``prefix=f"/api/plugins/{name}"`` 挂载，
    但**有些 route 的 path 已经自带完整前缀**（本插件的旧写法），所以两种都要认。
    """
    return [f"/api/plugins/{PLUGIN_ID}"]


def _unmount(app: Any) -> int:
    """从 ``app.router.routes`` 摘掉本插件的全部路由，返回摘掉条数。

    原地切片赋值（``routes[:] = kept``）：请求线程正在遍历这个 list 时，
    它会看到一次整体替换，而不是中途被改坏。
    """
    prefixes = tuple(_api_prefixes())
    routes = app.router.routes
    before = len(routes)
    kept = [r for r in routes if not str(getattr(r, "path", "")).startswith(prefixes)]
    routes[:] = kept
    return before - len(kept)


# ----------------------------------------------------------------------- 构建


def _build_frontend(timeout: int = 180) -> Dict[str, Any]:
    """跑 ``scripts/build.mjs``（dist 落后时）。返回真实结果，不吞错。

    为什么在请求线程里同步跑：构建是确定性的（esbuild 无时间戳），本机约 2 秒；
    异步跑会让「重载完成」的响应先于产物落盘，前端一刷新又是旧的。
    """
    root = _DASHBOARD_DIR.parent
    script = root / "scripts" / "build.mjs"
    if not script.is_file():
        return {"ran": False, "reason": "build.mjs 不存在"}

    node = _find_node()
    if not node:
        return {"ran": False, "reason": "找不到 node（请把 node 放进 PATH）"}

    env = dict(os.environ)
    esbuild = _find_esbuild()
    if esbuild:
        env["HERMES_ESBUILD"] = esbuild

    started = time.time()
    try:
        proc = subprocess.run(
            [node, str(script)],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ran": True, "ok": False, "reason": f"构建超时（>{timeout}s）"}
    except OSError as exc:
        return {"ran": False, "reason": f"启动 node 失败：{exc}"}

    tail = "\n".join(((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-6:])
    return {
        "ran": True,
        "ok": proc.returncode == 0 and "BUILD-OK" in (proc.stdout or ""),
        "exit": proc.returncode,
        "elapsed_ms": int((time.time() - started) * 1000),
        "tail": tail,
    }


def _find_node() -> Optional[str]:
    import shutil

    for name in ("node", "node.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _find_esbuild() -> Optional[str]:
    """esbuild 的 CLI 入口：优先宿主 ``hermes-agent/node_modules``。"""
    root = _DASHBOARD_DIR.parent
    candidates = [
        os.environ.get("HERMES_ESBUILD") or "",
        str((root / ".." / ".." / "hermes-agent" / "node_modules" / "esbuild" / "bin" / "esbuild").resolve()),
        str((root / "node_modules" / "esbuild" / "bin" / "esbuild").resolve()),
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


# ----------------------------------------------------------------------- 端点


@router.post("/reload")
def reload_plugin(request: Request, build: int = 1) -> Dict[str, Any]:
    """就地热重载本插件（后端重 import + 可选前端重建）。**不重启进程**。

    查询参数 ``build=0`` 可跳过前端重建（只想换后端时更快）。

    响应字段：

    ==========================  ====================================================
    ``ok``                      整体是否成功（重载成功且产物未落后）
    ``backend.reloaded``        后端模块是否真的重新 import 了
    ``backend.routes_before``   重载前本插件路由条数
    ``backend.routes_after``    重载后本插件路由条数
    ``backend.purged``          从 ``sys.modules`` 摘掉的模块名（按磁盘现算）
    ``frontend.built``          是否跑了构建、退出码、耗时
    ``fingerprint_before/after`` 代码身份指纹（用来判断「到底换没换」）
    ``rolled_back``             重挂载失败时是否回滚到旧 router
    ``advice``                  给人看的一句结论 + 下一步
    ==========================  ====================================================
    """
    return _do_reload(request.app, bool(build))


def _count_own_routes(app: Any) -> int:
    prefixes = tuple(_api_prefixes())
    return sum(
        1 for r in app.router.routes if str(getattr(r, "path", "")).startswith(prefixes)
    )


def _do_reload(app: Any, do_build: bool) -> Dict[str, Any]:
    """真正的重载流程。``app`` 由 request-aware wrapper 传入。"""
    fp_before = _fingerprint()
    routes_before = _count_own_routes(app)

    # ---- 1) 前端：dist 落后才构建（构建失败不阻止后端重载，但要如实报）
    build_info: Dict[str, Any] = {"ran": False, "reason": "未请求构建"}
    if do_build:
        if fp_before.get("dist_stale"):
            build_info = _build_frontend()
        else:
            build_info = {"ran": False, "reason": "dist 不落后于 src，跳过构建"}

    # ---- 2) 备份**整张路由表**（回滚用）
    #
    # 为什么不是备份 router 对象：本进程里可能根本拿不到那个合成模块
    # （宿主用 `hermes_dashboard_plugin_<name>` 挂载，但测试/独立进程里常是直接
    # `include_router`）。拿不到 router 就无从回滚，于是重载失败会把路由清空 ——
    # 那是「面板整体消失」，比「没重载」糟得多。
    #
    # 快照整张 `app.router.routes` 是精确的：回滚时整体替换回去即可，不依赖任何
    # 模块缓存状态，也不会因为部分挂载成功而留下重复路由。
    routes_snapshot = list(app.router.routes)

    # ---- 3) 摘路由 + 清模块
    removed_routes = _unmount(app)
    purged = _purge_own_modules()

    # ---- 4) 重新挂载（宿主会 force_rescan 磁盘并 exec_module）
    reloaded = False
    mount_error = ""
    try:
        from hermes_cli.web_server_dashboard import _try_hot_mount_plugin_api  # type: ignore

        reloaded = bool(_try_hot_mount_plugin_api(PLUGIN_ID))
    except Exception as exc:  # noqa: BLE001 - 宿主异常必须原样报出
        mount_error = f"{type(exc).__name__}: {exc}"

    routes_after = _count_own_routes(app)

    # ---- 5) 安全网：重挂载失败就把路由表整体还原，不让面板整体消失
    rolled_back = False
    if not reloaded or routes_after == 0:
        try:
            # 整体替换回快照：不依赖任何模块缓存状态，也不会留下重复路由。
            app.router.routes[:] = list(routes_snapshot)
            rolled_back = True
        except Exception as exc:  # noqa: BLE001
            mount_error = (
                mount_error + " | rollback: " + f"{type(exc).__name__}: {exc}"
            ).strip(" |")
        routes_after = _count_own_routes(app)

    fp_after = _fingerprint()

    ok = bool(reloaded) and routes_after > 0 and not fp_after.get("dist_stale")

    if ok:
        advice = (
            f"已热重载：路由 {routes_before} → {routes_after}，"
            f"后端指纹 {fp_before.get('backend')} → {fp_after.get('backend')}。"
            "刷新浏览器即可看到新界面（前端产物已是新的）。"
        )
    elif rolled_back:
        advice = (
            "重载失败，已回滚到旧代码（面板仍可用，但跑的还是旧逻辑）。"
            f"原因：{mount_error or '未知'}。看 serve 日志确认后修正再点一次。"
        )
    elif fp_after.get("dist_stale"):
        advice = (
            "后端已重载，但前端产物仍落后于 src —— 构建没成功。"
            f"构建结果：{json.dumps(build_info, ensure_ascii=False)[:300]}"
        )
    else:
        advice = f"重载未生效：{mount_error or '宿主未重新挂载'}（路由 {routes_before} → {routes_after}）"

    return {
        "ok": ok,
        "backend": {
            "reloaded": reloaded,
            "routes_before": routes_before,
            "routes_after": routes_after,
            "routes_removed": removed_routes,
            "purged_count": len(purged),
            "purged": purged,
            "error": mount_error,
        },
        "frontend": build_info,
        "fingerprint_before": fp_before,
        "fingerprint_after": fp_after,
        "rolled_back": rolled_back,
        "advice": advice,
    }

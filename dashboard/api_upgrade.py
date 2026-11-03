# -*- coding: utf-8 -*-
"""hermes-console · 升级分区后端（V2：搬 ``plugins/upgrade-console/dashboard/plugin_api.py``）。

挂载契约：本文件不单独挂载。``plugin_api.py`` 把本目录加进 ``sys.path`` 后 import 本模块，
再 ``include_router``；所有路径最终落在 ``/api/plugins/hermes-console/upgrade/`` 之下：

    GET  /api/plugins/hermes-console/upgrade/scan       进程分类 + 版本 + 就绪快照（纯只读）
    GET  /api/plugins/hermes-console/upgrade/readiness  完整就绪检查（含 cron 与自动恢复兜底）
    POST /api/plugins/hermes-console/upgrade/prepare    清场：暂停全部激活 cron（带快照）
    POST /api/plugins/hermes-console/upgrade/restore    恢复：只恢复本次快照里暂停的任务
    POST /api/plugins/hermes-console/upgrade/wait       轮询等待给定 PID 退出
    POST /api/plugins/hermes-console/upgrade/kill       结束指定进程（只放行 worker/CLI）

职责边界（与旧插件逐条一致，不许放宽）：

- 只扫描/结束【看板 worker 与独立 CLI 会话】这类 upgrade 流程不知道、不管理的进程；
- 绝不杀 gateway 与桌面 serve（升级流程会自行处理，杀它们只会破坏渠道）；
- cron 暂停带快照，恢复只恢复本次暂停的任务，绝不复活用户本就暂停的任务。

两条从旧实现继承、**不许简化**的约束：

1. **状态文件原地沿用**：``<home>/plugins/upgrade-console/state.json``。不搬迁、不重建、
   不改路径 —— 历史快照必须能继续读。路径**每次请求按布局重新解析**（不缓存，见
   :func:`hermes_home`），且不采信 ``HERMES_HOME`` 环境变量（worker 进程里它是 profile
   目录语义，照读会把状态文件指到 ``profiles/<p>/plugins/...``）。
2. **``/prepare`` 写快照失败必须回滚暂停**。2026-10 全 profile cron 批量暂停事故的根因
   就是「先暂停、后写快照」：写失败时暂停已经生效，却没有任何恢复依据。这里保持旧顺序
   不变，但写失败立刻把本次暂停逐条 resume，并返回结构化错误。

错误契约：所有失败统一 ``{"ok": false, "error": {"code": ..., "message": ...}}``
（4xx/5xx 同形，额外上下文如 ``rolled_back`` 挂在 ``error`` 里）。
成功契约：``{"ok": true, "data": {...}}``。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter  # type: ignore
from fastapi.responses import JSONResponse  # type: ignore
from pydantic import BaseModel  # type: ignore

#: 域名前缀。与 api_overview(/overview/*)、api_weather(/weather/*) 同风格：
#: 一个域一个子命名空间，避免将来某个域的裸 ``/scan`` 撞上别人的路由。
router = APIRouter(prefix="/upgrade", tags=["upgrade"])

logger = logging.getLogger(__name__)

#: 状态文件相对 Hermes 主目录的路径。**唯一的路径事实来源**（探针断言的就是它）。
STATE_RELATIVE = Path("plugins") / "upgrade-console" / "state.json"

#: 状态文件缺失/损坏时的默认状态。文件不存在或内容非 JSON **不是错误**，
#: 只代表「本次没有待恢复的快照」，必须用这份默认值继续跑（fail-open）。
DEFAULT_STATE: Dict[str, Any] = {"jobs": [], "restored": True}

#: 会真正锁住升级路径的“Python 解释器”特征：venv 本体与 .hermes-runtime 运行时。
VENV_MARKERS = (
    "hermes-agent\\venv",
    "hermes-agent/.hermes-runtime",
    "hermes-agent\\.hermes-runtime",
    ".hermes-runtime\\python",
    ".hermes-runtime/python",
)

_GATEWAY_RE = re.compile(r"gateway\s+run|hermes_cli[\\/]main\.py\s+gateway\s+run")
_SERVE_RE = re.compile(r"\bserve\b|\bserve\s+--host|--profile\s+\S+\s+serve")
_WORKER_RE = re.compile(r"--cli|--accept-hooks|--skills|\bkanban\b|claim|worker")

#: 自动恢复兜底：暂停超过该秒数即自动恢复（不依赖网关心跳）。
#: 网关心跳新鲜窗口放宽到 600 秒；若网关长期无心跳（已死），超过 1 小时仍强制恢复，
#: 避免"升级完忘了点恢复 / 网关死了"导致任务永久暂停。
AUTO_RESTORE_AFTER_SECONDS = 300
AUTO_RESTORE_FORCE_AFTER_SECONDS = 3600

#: /wait 的超时钳位（与旧实现一致：最少 5 秒、最多 600 秒）。
MIN_WAIT_SECONDS = 5
MAX_WAIT_SECONDS = 600

#: /wait 的轮询间隔（秒）。
WAIT_POLL_SECONDS = 2


# ---------------------------------------------------------------------------
# 主目录 / 状态文件解析
# ---------------------------------------------------------------------------
def _looks_like_home(path: Path) -> bool:
    """``<home>/profiles/`` 与 ``<home>/config.yaml`` 同时存在 ⇒ 所有 profile 的共同父目录。"""
    try:
        return (path / "profiles").is_dir() and (path / "config.yaml").is_file()
    except OSError:
        return False


def hermes_home() -> Path:
    """Hermes 主目录（所有 profile 的共同父目录，也就是 ``plugins/`` 的父目录）。

    **不采信 ``HERMES_HOME`` 环境变量**：worker / gateway / kanban 派生进程里它是
    ``<home>/profiles/<p>``（"当前 profile 目录"语义）。照读会把状态文件指到
    ``profiles/<p>/plugins/upgrade-console/state.json`` —— 那是一个永远读不到历史快照的
    空路径，正好是「恢复时没有快照可恢复」这类静默失效的来源。

    解析顺序：``api_agents.hermes_home()``（同目录、已有布局正向探测）→ 本文件上溯 3 级
    → ``HERMES_HOME`` 及其上两级 → ``~/.hermes``。**每次请求重新解析，不加缓存**：
    主目录挪了、profile 增减了，下一次请求就该看到，而不是等重启。
    """
    try:
        import api_agents  # type: ignore  # 同目录兄弟模块（plugin_api.py 已把本目录放进 sys.path）

        return Path(api_agents.hermes_home())
    except Exception as exc:  # noqa: BLE001 - 兄弟模块不可用时自己探一遍，绝不因此炸掉路由
        logger.warning("hermes-console/api_upgrade: api_agents.hermes_home 不可用（%s），自行探测", exc)
    return _fallback_home()


def _fallback_home() -> Path:
    candidates: List[Path] = []
    try:
        # <home>/plugins/hermes-console/dashboard/api_upgrade.py → parents[3] == <home>
        candidates.append(Path(__file__).resolve().parents[3])
    except (IndexError, OSError):
        pass
    env_home = (os.environ.get("HERMES_HOME") or "").strip()
    if env_home:
        candidates.append(Path(env_home))
        candidates.append(Path(env_home).parent.parent)  # <home>/profiles/<p> → <home>
    candidates.append(Path.home() / ".hermes")
    fallback: Optional[Path] = None
    for cand in candidates:
        try:
            if _looks_like_home(cand):
                return cand
        except OSError:
            continue
        if fallback is None and cand.is_dir():
            fallback = cand
    return fallback or (Path.home() / ".hermes")


def state_file() -> Path:
    """状态文件绝对路径（**每次调用重新解析**，见 :func:`hermes_home`）。"""
    return hermes_home() / STATE_RELATIVE


# ---------------------------------------------------------------------------
# 统一响应体
# ---------------------------------------------------------------------------
def _fail(status_code: int, code: str, message: str, **extra: Any) -> JSONResponse:
    """统一失败体（与 ``plugin_api._fail`` / ``api_advisor`` 同款）。"""
    error: Dict[str, Any] = {"code": code, "message": message}
    error.update({key: value for key, value in extra.items() if key not in ("code", "message")})
    return JSONResponse(status_code=status_code, content={"ok": False, "error": error})


def _ok(data: Any, **extra: Any) -> Dict[str, Any]:
    """统一成功体：``{"ok": true, "data": {...}}``。"""
    body: Dict[str, Any] = {"ok": True, "data": data}
    body.update(extra)
    return body


# ---------------------------------------------------------------------------
# 进程扫描与分类
# ---------------------------------------------------------------------------
def _py_procs_psutil() -> List[Dict[str, Any]]:
    import psutil  # type: ignore

    out: List[Dict[str, Any]] = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            info = p.info
            cmdline = info.get("cmdline") or []
            name = info.get("name") or ""
            if not cmdline or not isinstance(cmdline, list):
                continue
            # 只保留 Python 解释器进程；shell/bash 包装器、系统进程一律跳过
            if "python" not in name.lower():
                continue
            joined = " ".join(str(x) for x in cmdline)
            out.append(
                {
                    "pid": info["pid"],
                    "name": name,
                    "cmdline": joined,
                    "started": info.get("create_time") or 0,
                    "python": True,
                }
            )
        except Exception:  # noqa: BLE001 - 单个进程读不到就跳过，不许中断整轮扫描
            continue
    return out


def _py_procs_wmi() -> List[Dict[str, Any]]:
    """psutil 不可用的降级路径：PowerShell CIM 拿进程与命令行。"""
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match 'python' } | "
        "Select-Object ProcessId,Name,CreationDate,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=45,
        )
        data = json.loads(r.stdout or "[]")
        if isinstance(data, dict):
            data = [data]
        rows = []
        from datetime import datetime

        for d in data:
            try:
                started = (
                    datetime.fromisoformat(str(d.get("CreationDate", "")))
                    .timestamp()
                    if d.get("CreationDate")
                    else 0
                )
            except Exception:  # noqa: BLE001
                started = 0
            rows.append(
                {
                    "pid": d.get("ProcessId"),
                    "name": d.get("Name", ""),
                    "cmdline": d.get("CommandLine", "") or "",
                    "started": started,
                    "python": True,
                }
            )
        return rows
    except Exception:  # noqa: BLE001 - 降级路径也失败 ⇒ 空列表（scan 仍要返回 200）
        return []


def _classify(cmdline: str) -> Dict[str, Any]:
    """按命令行判断进程身份与是否可清场（判定顺序不许调换）。"""
    c = (cmdline or "").lower()
    has_hermes = any(m.lower() in c for m in VENV_MARKERS)
    if not has_hermes:
        return {"kind": "unrelated", "can_kill": False, "note": "非 Hermes venv/runtime 解释器，不影响升级"}
    if _GATEWAY_RE.search(c):
        return {"kind": "gateway", "can_kill": False, "note": "消息网关，升级流程自行停/拉，禁止手动杀"}
    if _SERVE_RE.search(c):
        return {"kind": "desktop-serve", "can_kill": False, "note": "桌面后端 serve，桌面端自动管理，禁止手动杀"}
    if _WORKER_RE.search(c):
        return {"kind": "worker", "can_kill": True, "note": "看板 worker / CLI 会话，安全清场对象"}
    return {"kind": "hermes-other", "can_kill": False, "note": "Hermes 相关进程（命令形态未识别），谨慎处理"}


def _scan_raw() -> List[Dict[str, Any]]:
    """扫一遍并分类。同步函数：路由里用 ``asyncio.to_thread`` 调，别阻塞事件循环。"""
    try:
        procs = _py_procs_psutil()
    except Exception:  # noqa: BLE001 - psutil 缺失/权限异常 ⇒ WMI 降级
        procs = _py_procs_wmi()
    for pr in procs:
        pr.update(_classify(pr.get("cmdline") or ""))
    return procs


def _blockers(procs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "pid": p.get("pid"),
            "cmdline": str(p.get("cmdline") or "")[:160],
            "kind": p.get("kind"),
        }
        for p in procs
        if p.get("can_kill")
    ]


# ---------------------------------------------------------------------------
# 版本信息（/scan 的「版本字段」）
# ---------------------------------------------------------------------------
def _version_info() -> Dict[str, Any]:
    """当前安装版本 / commit。取不到就如实说取不到（不编一个版本号）。"""
    try:
        from hermes_cli.version_info import get_code_identity  # type: ignore

        ident = get_code_identity() or {}
        return {
            "version": ident.get("version") or "unknown",
            "sha": ident.get("short_sha"),
            "source": ident.get("source") or "unknown",
        }
    except Exception as exc:  # noqa: BLE001 - 版本读不到不影响清场
        return {
            "version": "unknown",
            "sha": None,
            "source": "unavailable",
            "error": "%s: %s" % (type(exc).__name__, exc),
        }


# ---------------------------------------------------------------------------
# cron 快照暂停 / 恢复
# ---------------------------------------------------------------------------
async def _get_cron_jobs() -> List[Dict[str, Any]]:
    try:
        from hermes_cli.web_routers.cron import list_cron_jobs  # type: ignore

        jobs = await list_cron_jobs("all")
        return jobs if isinstance(jobs, list) else []
    except Exception as exc:  # noqa: BLE001 - cron 读不到 ⇒ 空列表（就绪检查据此说 cron_active=0）
        logger.warning("hermes-console/api_upgrade: 读取 cron 列表失败：%s", exc)
        return []


async def _pause_job(job_id: str) -> bool:
    try:
        from hermes_cli.web_routers.cron import pause_cron_job  # type: ignore

        r = await pause_cron_job(job_id)
        if isinstance(r, dict):
            # pause 返回完整任务对象；成功判据 = enabled 已翻转为 False / state=paused
            return r.get("enabled") is False or r.get("state") == "paused"
        return False
    except Exception as exc:  # noqa: BLE001 - 单条暂停失败只影响这一条
        logger.warning("hermes-console/api_upgrade: 暂停 cron %s 失败：%s", job_id, exc)
        return False


async def _resume_job(job_id: str) -> bool:
    try:
        from hermes_cli.web_routers.cron import resume_cron_job  # type: ignore

        r = await resume_cron_job(job_id)
        if isinstance(r, dict):
            # resume 返回完整任务对象；成功判据 = enabled 已恢复为 True
            return r.get("enabled") is True
        return False
    except Exception as exc:  # noqa: BLE001 - 单条恢复失败记进 failed 列表
        logger.warning("hermes-console/api_upgrade: 恢复 cron %s 失败：%s", job_id, exc)
        return False


def _job_active(job: Dict[str, Any]) -> bool:
    """根据 cron 列表返回字段判断任务是否激活（enabled/status 均可）。"""
    if job.get("enabled") is False:
        return False
    if job.get("status") in ("paused", "disabled", "inactive"):
        return False
    if job.get("next_run") is None and not job.get("id"):
        return False
    return True


# ---------------------------------------------------------------------------
# 状态文件读写（fail-open 是硬要求，不许退回抛异常版本）
# ---------------------------------------------------------------------------
def _load_state() -> Dict[str, Any]:
    """读状态文件。**永不抛异常**：缺失/空/非法 JSON/是目录/无权限 → 返回默认状态。

    这是本域唯一的读入口（/scan、/readiness、/prepare、/restore 都走它）。
    历史上 /prepare 的清场快照一旦写坏，restore 侧没有兜底，会把 cron 停在
    ``restored:false`` 不动；因此这里任何异常都必须降级成默认值 + warning。
    """
    target = state_file()
    try:
        if not target.is_file():
            return dict(DEFAULT_STATE)
        raw = target.read_text(encoding="utf-8")
        if not raw.strip():
            logger.warning("hermes-console/api_upgrade: 状态文件为空（%s），用默认值", target)
            return dict(DEFAULT_STATE)
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - fail-open 是硬要求
        logger.warning(
            "hermes-console/api_upgrade: 状态文件读不了（%s）：%s: %s —— 用默认值",
            target, type(exc).__name__, exc,
        )
        return dict(DEFAULT_STATE)
    if not isinstance(data, dict):
        logger.warning(
            "hermes-console/api_upgrade: 状态文件是 %s 不是 JSON 对象（%s），用默认值",
            type(data).__name__, target,
        )
        return dict(DEFAULT_STATE)
    return data


def _save_state(st: Dict[str, Any]) -> bool:
    """写状态文件。失败只记 warning 并返回 False —— 绝不让清场/恢复流程因落盘失败炸掉。"""
    target = state_file()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)  # 原子替换：写一半崩掉也不会留下半个 JSON
        return True
    except Exception as exc:  # noqa: BLE001 - fail-open 是硬要求
        logger.warning(
            "hermes-console/api_upgrade: 状态文件写不了（%s）：%s: %s —— 本次不落盘",
            target, type(exc).__name__, exc,
        )
        return False


def _pending_snapshot(st: Dict[str, Any]) -> bool:
    """是否存在「已暂停但未恢复」的快照。"""
    return st.get("restored") is False and bool(st.get("job_ids") or st.get("jobs"))


async def _do_restore(st: Dict[str, Any]) -> Dict[str, Any]:
    """按快照精确恢复：was_active=True 的任务 resume；其余不动。落盘 restored=True。"""
    resumed: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []
    jobs = st.get("jobs") or []
    for j in jobs:
        jid = str(j.get("job_id") or "")
        if not jid:
            continue
        if j.get("was_active") is False:
            skipped.append(jid)  # 暂停前就非激活，保持现状
            continue
        ok = await _resume_job(jid)
        if ok:
            resumed.append(jid)
        else:
            failed.append(jid)
    st["restored"] = True
    st["restored_at"] = time.time()
    st.pop("paused_at", None)
    _save_state(st)
    return {"resumed": resumed, "skipped": skipped, "failed": failed, "resumed_count": len(resumed)}


def _gateway_fresh(max_age_s: float = 600.0) -> bool:
    """default 网关心跳是否新鲜（升级完成 = gateway 重启后有新心跳）。"""
    try:
        hb = hermes_home() / "state" / "gateway.heartbeat"
        if not hb.exists():
            return False
        age = time.time() - hb.stat().st_mtime
        return 0 <= age <= max_age_s
    except Exception:  # noqa: BLE001
        return False


async def _readiness_payload(
    procs: Optional[List[Dict[str, Any]]] = None,
    allow_auto_restore: bool = True,
) -> Dict[str, Any]:
    """就绪检查的**唯一实现**（/scan 与 /readiness 共用，口径不许分叉）。

    ``allow_auto_restore=False`` 时不触发自动恢复（/scan 是纯只读端点，GET 不该改状态）：
    此时 ``pending_restore`` 照实报 True，``auto_restore_allowed`` 报 False。
    """
    if procs is None:
        procs = await asyncio.to_thread(_scan_raw)
    blockers = _blockers(procs)
    cron = await _get_cron_jobs()
    active = [j for j in cron if _job_active(j)]
    st = _load_state()
    wait = st.get("paused_at")
    pending = _pending_snapshot(st)
    restored_flag = st.get("restored")
    auto_restored = 0
    if pending and allow_auto_restore:
        elapsed = time.time() - (wait or 0)
        # 升级完成信号（心跳新鲜）或超长兜底（网关死了也不能永久暂停）→ 自动恢复
        if elapsed > AUTO_RESTORE_AFTER_SECONDS and (
            _gateway_fresh() or elapsed > AUTO_RESTORE_FORCE_AFTER_SECONDS
        ):
            res = await _do_restore(st)
            auto_restored = res.get("resumed_count", 0)
            pending = False
            wait = None
            restored_flag = True
    return {
        "ready": len(blockers) == 0 and len(active) == 0,
        "blockers": blockers,
        "cron_active": len(active),
        "cron_total": len(cron),
        "cron_paused_at": wait,
        "pending_restore": pending,
        "restored": restored_flag,
        "auto_restored": auto_restored,
        "auto_restore_allowed": bool(allow_auto_restore),
        "snapshot_jobs": len(st.get("jobs") or []),
        "state_file": str(state_file()),
    }


# ---------------------------------------------------------------------------
# 路由：扫描 / 就绪
# ---------------------------------------------------------------------------
@router.get("/scan")
async def scan() -> Any:
    """扫描所有 python 进程并分类 + 版本 + 就绪快照。**纯只读**（不暂停、不恢复、不写盘）。"""
    procs = await asyncio.to_thread(_scan_raw)
    # allow_auto_restore=False：GET 不许产生写副作用，自动恢复只走 /readiness。
    readiness = await _readiness_payload(procs, allow_auto_restore=False)
    return _ok(
        {
            "procs": procs,
            "workers": [p for p in procs if p.get("can_kill")],
            "state_file": str(state_file()),
            "version": _version_info(),
            "readiness": readiness,
        }
    )


@router.get("/readiness")
async def readiness() -> Any:
    """就绪检查：升级前是否还有需人工处理的拦路项 + 是否有未恢复的快照。

    这是**唯一**会做自动恢复兜底的读端点（超时未恢复的暂停快照在此自动放行）。
    """
    return _ok(await _readiness_payload(None, allow_auto_restore=True))


# ---------------------------------------------------------------------------
# 路由：清场（暂停 cron + 快照）
# ---------------------------------------------------------------------------
@router.post("/prepare")
async def prepare() -> Any:
    """升级前清场：暂停全部激活 cron（快照保留每个任务暂停前的状态），返回可清场 worker。

    若已存在未恢复快照，拒绝重复暂停（防止覆盖待恢复状态导致恢复丢失）。
    快照结构：``jobs: [{job_id, name, profile, was_active}]`` —— restore 时按 was_active
    精确还原（被暂停的恢复 active；快照外的一律不碰，保持用户原本的 paused/active）。
    """
    existing = _load_state()
    if _pending_snapshot(existing):
        return _fail(
            409,
            "pending_restore",
            "存在未恢复快照，请先恢复或等待自动恢复后再清场",
            paused=[],
            paused_count=0,
            state_file=str(state_file()),
        )

    paused_jobs: List[Dict[str, Any]] = []
    jobs = await _get_cron_jobs()
    for job in jobs:
        jid = job.get("id") or job.get("job_id")
        if not jid:
            continue
        if _job_active(job):
            ok = await _pause_job(str(jid))
            if ok:
                paused_jobs.append(
                    {
                        "job_id": str(jid),
                        "name": job.get("name") or str(jid),
                        "profile": job.get("profile") or job.get("profile_name") or "",
                        "was_active": True,
                    }
                )

    saved = _save_state(
        {
            "paused_at": time.time(),
            "jobs": paused_jobs,
            "restored": False,
        }
    )

    if not saved:
        # 快照没落盘 → 绝不能把 cron 留在「已暂停但没有可恢复快照」的状态。
        # 历史事故（state.json restored:false 无人恢复、全 profile 批量暂停）就是这个顺序造成的：
        # 先暂停、后写快照，写失败时暂停已经生效且没有任何恢复依据。
        # 这里立刻回滚本次暂停，保证「要么干净地停住、要么一条不动」。
        rolled_back: List[str] = []
        still_paused: List[str] = []
        for j in paused_jobs:
            if await _resume_job(j["job_id"]):
                rolled_back.append(j["job_id"])
            else:
                still_paused.append(j["job_id"])
        logger.warning(
            "hermes-console/api_upgrade /prepare 快照落盘失败 —— 已回滚 %d 条暂停，%d 条仍在暂停",
            len(rolled_back), len(still_paused),
        )
        return _fail(
            503,
            "state_unwritable",
            "状态快照无法落盘，已回滚本次暂停，清场未执行（避免 cron 卡在暂停态且无快照可恢复）",
            paused=[],
            paused_count=0,
            rolled_back=rolled_back,
            still_paused=still_paused,
            state_file=str(state_file()),
        )

    workers = [p for p in await asyncio.to_thread(_scan_raw) if p.get("can_kill")]
    return _ok(
        {
            "paused": [j["job_id"] for j in paused_jobs],
            "paused_count": len(paused_jobs),
            "jobs": paused_jobs,
            "workers": workers,
            "state_file": str(state_file()),
        }
    )


# ---------------------------------------------------------------------------
# 路由：恢复
# ---------------------------------------------------------------------------
@router.post("/restore")
async def restore() -> Any:
    """升级后恢复：只恢复本次快照里暂停的任务；快照外一律不碰。

    没有待恢复快照 ⇒ **409 业务错误**（旧实现会照样写一遍 state.json 把 restored 置 True，
    那是在没有快照的情况下改历史状态，本域不再这么做）。
    """
    st = _load_state()
    if not _pending_snapshot(st):
        return _fail(
            409,
            "no_pending_restore",
            "没有待恢复的暂停快照（state.json 的 restored 不是 false，或快照里没有任务），无需恢复",
            state_file=str(state_file()),
            restored=st.get("restored"),
        )
    return _ok(await _do_restore(st))


# ---------------------------------------------------------------------------
# 路由：等待收尾 / 强制结束
# ---------------------------------------------------------------------------
def _alive(pid: int) -> bool:
    try:
        import psutil  # type: ignore

        return bool(psutil.pid_exists(pid))
    except Exception:  # noqa: BLE001 - psutil 不可用走 tasklist
        try:
            probe = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            return str(pid) in (probe.stdout or "")
        except Exception:  # noqa: BLE001
            return False


class WaitBody(BaseModel):
    pids: List[int] = []
    timeout: int = 120


class KillBody(BaseModel):
    pids: List[int] = []


@router.post("/wait")
async def wait_for_exit(body: WaitBody) -> Any:
    """轮询等待给定 PID 退出（自然收尾）；超时返回仍存活的 pid 供用户决定。"""
    pids = [int(p) for p in (body.pids or []) if int(p) > 0]
    if not pids:
        return _fail(400, "no_target", "没有指定要等待的进程（pids 为空或都不是正整数）")
    timeout = max(MIN_WAIT_SECONDS, min(int(body.timeout), MAX_WAIT_SECONDS))
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = [p for p in pids if _alive(p)]
        if not alive:
            return _ok({"exited": pids, "still_alive": [], "timed_out": False, "timeout": timeout})
        await asyncio.sleep(WAIT_POLL_SECONDS)
    still = [p for p in pids if _alive(p)]
    return _ok(
        {
            "exited": [p for p in pids if p not in still],
            "still_alive": still,
            "timed_out": True,
            "timeout": timeout,
        }
    )


@router.post("/kill")
async def kill_workers(body: KillBody) -> Any:
    """结束指定进程（仅放行 worker/CLI 类，gateway/serve 一律拒绝）。"""
    pids = [int(p) for p in (body.pids or []) if int(p) > 0]
    if not pids:
        return _fail(400, "no_target", "没有指定要结束的进程（pids 为空或都不是正整数）")

    procs = await asyncio.to_thread(_scan_raw)
    by_pid = {p.get("pid"): p for p in procs}
    killed: List[int] = []
    refused: List[Dict[str, Any]] = []
    for pid in pids:
        info = by_pid.get(pid)
        if not info or not info.get("can_kill"):
            refused.append(
                {
                    "pid": pid,
                    "kind": (info or {}).get("kind") or "unknown",
                    "reason": "非可清场进程（gateway/serve/unrelated/未知）",
                }
            )
            continue
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, text=True, timeout=15,
            )
            killed.append(pid)
        except Exception as e:  # noqa: BLE001 - 单个失败不影响其它 pid
            refused.append({"pid": pid, "reason": "kill 失败: %s" % e})
    if not killed and refused:
        # 一条都没杀成 ⇒ 这是业务拒绝，不是 500：把逐条原因原样交回去。
        return _fail(409, "nothing_killed", "没有进程被结束：全部不在可清场范围内", refused=refused)
    return _ok({"killed": killed, "refused": refused})

# -*- coding: utf-8 -*-
"""hermes-console · VCC 上下文引擎分区后端（V3a）：8 条路由。

## 按对话下钻（t_c4c4d49e，本卡新增 2 条只读路由）

老板要看的是「一个对话一个对话地看」，所以引擎视角之外补了两条**按对话**的路由：

* ``GET /vcc/conversations`` —— 该 agent 的对话列表（可搜索、可筛「有压缩/有召回」），
  每行是 ``sessions`` 表的一行 + 该会话在 ``messages`` 里的真实聚合；
* ``GET /vcc/conversation``  —— 一个对话的详情：上下文（条数/字符数/token）、
  压缩几次（每次的摘要 + 压缩前后长度）、召回几次（每次命中的内容片段）。

**数据口径（硬约束：不存在就写「无记录」，不补造不估算）**：

==================  ==========================================  ==================================================
面板项              库 / 表 / 字段                              实测
==================  ==========================================  ==================================================
对话列表            ``<profile>/state.db`` ``sessions``          ``id/title/model/message_count/last_activity_at``
                    ``messages``                                ``group by session_id`` 的真实聚合
上下文条数/字符数   ``messages``                                ``active=1`` 的 ``count(*)`` 与 ``length(content)``
token 数            ``messages.token_count``                    **本机全库为 NULL**（340k 行 0 行有值）⇒ 无记录
模型窗口            ``<home>/context_length_cache.yaml``        ``context_lengths[model@base_url]``；没有 ⇒ 无记录
压缩次数与摘要      ``messages``                                ``_compressed_summary=1``（宿主自己的标记）+ 摘要内容
压缩前后长度        ``messages``                                ``compacted=1`` 行的 ``length(content)`` 累计/区间
召回（会话）        ``messages``                                ``tool_name='session_search'`` 行的 JSON ``content``
召回（记忆）        ``messages``                                ``content`` 里的 ``<memory-context>`` 注入块
==================  ==========================================  ==================================================

``token_count`` 这一列全库为 NULL，宿主只在内存里算 ``last_prompt_tokens``（``gateway/session_usage.py``）
不落库，所以 token 数与「占模型窗口比例」在本机**没有数据源**：面板显示「无记录」并写明查过哪一列，
**不用字符数折算 token**（那是估算，本卡明令禁止）。


挂载契约：本文件**不单独挂载**。``plugin_api.py`` 把本目录加进 ``sys.path`` 后 import 本模块，
再 ``include_router(api_vcc.router)``；最终 URL 是 ``/api/plugins/hermes-console/vcc/...``。

## 引擎按 agent(=profile) 解析（本卡的硬要求，写死 ``default`` 视为缺陷）

``context.engine`` 是**每个 profile 单独配**的，不是全局开关。因此：

* 每条路由都接受 ``agent`` 参数（缺省 ``default``，与 ``GET /modules?agent=`` 同一口径），
  经 ``core.scope.normalize_agent_id`` 归一（非法 ⇒ **400** + 合法 id 列表），
  再用 ``core.scope.agent_home`` 定位**该 agent 自己的** ``config.yaml``；
* 引擎名只从那份 ``config.yaml`` 的 ``context.engine`` 读（yaml 解析失败退回正则），
  响应里同时回 ``config_path`` / ``engine_source``，让人能自己核对来源；
* 引擎插件自述（名字 / 版本）来自插件自己的 ``plugins/hermes-vcc-cn/plugin.yaml``
  （:func:`_engine_meta`），**不是**本文件的字面量；``hermes-vcc-cn`` 字符串只作为
  "目录名"与"读不到 plugin.yaml 时的兜底值"出现，且兜底会写进 ``engine_plugin.fallback``。

## 复用引擎，不重实现

压缩逻辑一行都不在这里。本文件只读引擎**自己的产物**：``logs/*.log`` 里的
``hermes-vcc-cn: deterministic summary in X ms (N chars from M turns)``、以及每个 profile 的
``state.db`` 里真正落库的摘要。唯一写操作是 ``POST /engine``（切换 ``context.engine``），
走官方 ``hermes_cli.config.save_config``（``_config_profile_scope`` + 跨进程锁 + fail-closed
原始读 + partial merge），写后**立刻回读**，绝不"显示成功却没写进去"。

## 错误语义

所有 4xx/5xx 统一 ``{"ok": false, "error": {"code": ..., "message": ..., ...}}``；成功体是
裸 dict（与 ``api_upgrade`` / ``api_overview`` 同风格），并带 ``"ok": true`` 便于前端判分支。
"""
from __future__ import annotations

import importlib
import json
import logging
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Query  # type: ignore
from fastapi.responses import JSONResponse  # type: ignore

_DASHBOARD_DIR = Path(__file__).resolve().parent
if str(_DASHBOARD_DIR) not in sys.path:
    # 本文件被 dashboard 用合成模块名 exec，没有包上下文；core 包只能靠 sys.path 找到。
    sys.path.insert(0, str(_DASHBOARD_DIR))

router = APIRouter()
logger = logging.getLogger(__name__)

#: ``default`` 不是 ``profiles/`` 下的目录，它是 home 根本身（与 core.scope 同口径）。
DEFAULT_AGENT = "default"
#: 引擎插件**目录名**（定位 plugin.yaml 用）。它同时是读不到 plugin.yaml 时的兜底引擎名。
ENGINE_PLUGIN_DIRNAME = "hermes-vcc-cn"
#: 兜底引擎名：只在 plugin.yaml 读不到时使用，且响应里会标 ``engine_plugin.fallback=true``。
FALLBACK_ENGINE_NAME = "hermes-vcc-cn"
#: 宿主内置压缩引擎（``context.engine`` 的另一个合法取值）。
BUILTIN_ENGINE = "compressor"

#: 每个 profile 自己的日志（相对 profile home）。缺文件由 :func:`_tail` 吞掉。
LOG_FILES = ("logs/agent.log", "logs/gateway.log", "logs/gateway-stdio.log",
             "logs/desktop.log", "logs/errors.log")
LOG_TAIL_BYTES = 1_200_000
DB_SCAN_ROWS = 30_000
#: overview 里每个 profile 的摘要扫描窗口（比 :data:`DB_SCAN_ROWS` 小：
#: 总览要同时扫 9 个库，窗口开满会让首屏等到超时；详情接口用满窗口）。
OVERVIEW_SCAN_ROWS = 6_000
SEARCH_PREVIEW = 120
SEARCH_LIMIT = 20
SUMMARY_PREFIX = "[CONTEXT COMPACTION"
MIN_SUMMARY_CHARS = 800
MIN_SECTION_HITS = 2
SUMMARY_DEDUP_WINDOW_S = 120
SECTION_NAMES = ("Session Goal", "Files And Changes", "Commits", "Tracked Commands",
                 "Outstanding Context", "Key Outputs", "User Preferences")

COMPACT_RE = re.compile(
    r"hermes-vcc-cn: deterministic summary in ([\d.]+)ms \((\d+) chars from (\d+) turns\)")
USING_RE = re.compile(r"Using context engine: (\S+)")
DEGRADED_RE = re.compile(r"host contract degraded to local fallbacks for (.+)")
FAILED_RE = re.compile(r"hermes-vcc-cn: deterministic summary failed \(([^)]*)\)")
LLM_DISPATCH_RE = re.compile(r"Compression summary call dispatched: model=(\S+)")
REF_RE = re.compile(r"\(#(\d+)\)")
ENGINE_LINE_RE = re.compile(r"^\s+engine:\s*(\S+)", re.M)
CONTEXT_BLOCK_RE = re.compile(r"^context:\s*$(.*?)(?=^\S)", re.M | re.S)
#: 宿主在**引擎插件加载不到**时打的那一行（``agent/agent_init.py`` 的 fallback 分支）。
#: 有它 ⇒ 这个 agent 实际跑的是内置 ``compressor``，**不是** config.yaml 里写的插件。
#: 破折号在日志里是 U+2014，也容忍连字符变体；正文按宿主原文锚定，不模糊匹配。
FALLBACK_RE = re.compile(
    r"Context engine '([^']+)' not found\s*[—–-]\s*falling back to built-in compressor")

#: overview 缓存（15s）：9 个 profile × 日志尾 + 摘要扫描，别让每次刷新都全量重算。
_CACHE: Dict[str, Tuple[float, Any]] = {}
_CACHE_TTL = 15.0

# ---- 错误码（前端按字面量分支，别改） ------------------------------------
CODE_INVALID_ARGUMENT = "invalid_argument"
CODE_NOT_FOUND = "not_found"
CODE_CONFIRMATION_REQUIRED = "confirmation_required"
CODE_WRITE_NOT_APPLIED = "write_not_applied"
CODE_PERMISSION_DENIED = "permission_denied"
CODE_UPSTREAM_ERROR = "upstream_error"
CODE_NOT_A_SUMMARY = "not_a_summary"
#: 该 agent 的 plugins 目录里没有引擎插件本体 ⇒ 切过去只会假成功（运行时会回退内置 compressor）。
CODE_ENGINE_NOT_INSTALLED = "engine_not_installed"


class ApiError(Exception):
    """带 HTTP 语义的业务错误 → 统一失败体。"""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra: Dict[str, Any] = dict(extra)


def _fail(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    """统一失败体（与 ``plugin_api`` / ``api_autogroup`` 同款）。"""
    error: Dict[str, Any] = {"code": code, "message": message}
    error.update({key: value for key, value in extra.items() if key not in ("code", "message")})
    return JSONResponse(status_code=status, content={"ok": False, "error": error})


def _error_response(exc: ApiError) -> JSONResponse:
    return _fail(exc.status, exc.code, exc.message, **exc.extra)


# ---------------------------------------------------------------------------
# core 模块：惰性 import（core 坏掉只让本分区 503，不在挂载时拖垮整个插件）
# ---------------------------------------------------------------------------
def _core(submodule: str) -> Any:
    """import ``core.<submodule>``，带"同名别家模块"防护（口径同 ``api_console_settings._core``）。"""
    existing = sys.modules.get("core")
    if existing is not None:
        found = getattr(existing, "__file__", None)
        same_dir = False
        if found:
            try:
                same_dir = _DASHBOARD_DIR in Path(found).resolve().parents
            except OSError:
                same_dir = False
        if not same_dir:
            sys.modules.pop("core", None)
    try:
        return importlib.import_module("core." + submodule)
    except Exception as exc:  # noqa: BLE001 - 统一翻成 503 结构化错误
        raise ApiError(503, CODE_UPSTREAM_ERROR,
                       "dashboard/core/%s.py 不可用：%s: %s"
                       % (submodule, type(exc).__name__, exc)) from exc


def _home() -> Path:
    """Hermes 主目录（所有 profile 的共同父目录）。

    **唯一漏斗**：探针 monkeypatch 这一个函数就能把整个分区指向沙盒 home。
    不读 ``HERMES_HOME`` 环境变量（worker/gateway 里它是 ``<home>/profiles/<p>`` 语义，
    照读会把 9 个 agent 缩成 1 个）——解析逻辑全在 ``core.scope.hermes_home``。
    """
    return Path(_core("scope").hermes_home(None))


def _agent_of(raw: Optional[str]) -> str:
    """外部输入 → 合法 agent id；非法 ⇒ 400 + 合法 id 列表（不是 500，不是空列表）。"""
    scope = _core("scope")
    try:
        return str(scope.normalize_agent_id(raw))
    except ValueError as exc:
        raise ApiError(400, CODE_INVALID_ARGUMENT, str(exc),
                       agents=scope.list_agent_ids()) from exc


def _agent_home(agent: str) -> Path:
    return Path(_core("scope").agent_home(agent, home=_home()))


def _agent_config_path(agent: str) -> Path:
    return _agent_home(agent) / "config.yaml"


# ---------------------------------------------------------------------------
# 引擎解析（按 profile）
# ---------------------------------------------------------------------------
def _read_engine(config_path: Path) -> Tuple[str, str]:
    """读某 profile ``config.yaml`` 的 ``context.engine`` → ``(engine, source)``。

    ``source`` ∈ ``yaml`` / ``regex`` / ``missing`` / ``default``：
    * ``yaml``    —— yaml 正常解析到 ``context.engine``；
    * ``regex``   —— yaml 不可用/解析失败，退回行级正则（原文里确实写了 engine）；
    * ``missing`` —— 文件不存在（该 profile 没有自己的 config.yaml）；
    * ``default`` —— 文件在但没有 ``context.engine`` 键（宿主缺省就是 ``compressor``）。
    """
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return BUILTIN_ENGINE, "missing"
    except OSError as exc:
        logger.warning("api_vcc: 读不到 %s（%s）", config_path, exc)
        return BUILTIN_ENGINE, "missing"
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        ctx = data.get("context") if isinstance(data.get("context"), dict) else {}
        engine = str(ctx.get("engine") or "").strip()
        return (engine, "yaml") if engine else (BUILTIN_ENGINE, "default")
    except Exception:  # noqa: BLE001 - yaml 缺/坏都退回正则，绝不编造
        match = CONTEXT_BLOCK_RE.search(text)
        if match:
            inner = ENGINE_LINE_RE.search(match.group(1))
            if inner:
                return inner.group(1).strip().strip("'\""), "regex"
        return BUILTIN_ENGINE, "default"


def _read_context_block(config_path: Path) -> Dict[str, Any]:
    """某 profile 自己的 ``context`` 块（只读；含 ``context.vcc.*`` 这类引擎参数）。"""
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(config_path.read_text(encoding="utf-8", errors="replace")) or {}
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 - 读不到就给空块，别让整个路由 500
        return {}
    ctx = data.get("context") if isinstance(data, dict) else None
    return dict(ctx) if isinstance(ctx, dict) else {}


def _engine_meta() -> Dict[str, Any]:
    """引擎插件自述：从 ``plugins/hermes-vcc-cn/plugin.yaml`` 读 name / version / description。

    **复用插件、不重实现**：引擎叫什么、什么版本由插件自己声明，本文件不写死返回值。
    优先读与本插件同树的 ``plugins/hermes-vcc-cn``（宿主加载的就是这一份），
    再退到 ``<home>/plugins/hermes-vcc-cn``；都读不到 ⇒ ``available=false`` + 兜底名。
    """
    candidates: List[Path] = []
    try:
        candidates.append(_DASHBOARD_DIR.parents[1] / ENGINE_PLUGIN_DIRNAME)
    except (IndexError, OSError):
        pass
    try:
        candidates.append(_home() / "plugins" / ENGINE_PLUGIN_DIRNAME)
    except Exception:  # noqa: BLE001 - home 解析失败不影响兜底
        pass
    for plugin_dir in candidates:
        meta_path = plugin_dir / "plugin.yaml"
        try:
            text = meta_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        name, version, description = "", "", ""
        try:
            import yaml  # type: ignore

            data = yaml.safe_load(text) or {}
            if isinstance(data, dict):
                name = str(data.get("name") or "").strip()
                version = str(data.get("version") or "").strip()
                description = " ".join(str(data.get("description") or "").split())
        except Exception:  # noqa: BLE001 - 退回行级正则
            pass
        if not name:
            found = re.search(r"^name:\s*(\S+)", text, re.M)
            name = found.group(1).strip().strip("'\"") if found else ""
        if not version:
            found = re.search(r"^version:\s*(\S+)", text, re.M)
            version = found.group(1).strip().strip("'\"") if found else ""
        return {"name": name or FALLBACK_ENGINE_NAME, "version": version,
                "description": description[:400], "path": str(meta_path),
                "available": True, "fallback": not bool(name)}
    return {"name": FALLBACK_ENGINE_NAME, "version": "", "description": "",
            "path": "", "available": False, "fallback": True}


def _engine_plugin_meta(home: Path) -> Dict[str, Any]:
    """**某个 agent 自己**的引擎插件自述：``<home>/plugins/hermes-vcc-cn/plugin.yaml``。

    为什么要单独一份：运行时按 ``$HERMES_HOME/plugins`` 解析引擎
    （``plugins/plugin_loader.user_plugins_dir`` → ``plugins/context_engine`` 的
    ``_iter_engine_dirs``）。``default`` 的 HERMES_HOME 就是 home 根，其它 agent 是
    ``<home>/profiles/<id>``。所以 :func:`_engine_meta`（读本面板所在插件树 = 全局目录）
    **不能**拿来回答「这个 agent 装的是哪一版」——那正是本卡要修的假绿根因。

    只读文件，不做任何版本推断：读不到就 ``installed=false`` + 空版本，绝不回填全局版本。
    """
    meta_path = Path(home) / "plugins" / ENGINE_PLUGIN_DIRNAME / "plugin.yaml"
    info: Dict[str, Any] = {
        "installed": False, "name": "", "version": "", "requires_hermes": "",
        "path": str(meta_path), "dir": str(meta_path.parent),
    }
    try:
        text = meta_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return info
    name = version = requires = ""
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        if isinstance(data, dict):
            name = str(data.get("name") or "").strip()
            version = str(data.get("version") or "").strip()
            requires = str(data.get("requires_hermes") or "").strip()
    except Exception:  # noqa: BLE001 - yaml 缺/坏退回行级正则
        pass
    if not name:
        found = re.search(r"^name:\s*(\S+)", text, re.M)
        name = found.group(1).strip().strip("'\"") if found else ""
    if not version:
        found = re.search(r"^version:\s*(\S+)", text, re.M)
        version = found.group(1).strip().strip("'\"") if found else ""
    if not requires:
        found = re.search(r"^requires_hermes:\s*(\S+)", text, re.M)
        requires = found.group(1).strip().strip("'\"") if found else ""
    info.update({
        "installed": True,
        "name": name or ENGINE_PLUGIN_DIRNAME,
        "version": version,
        "requires_hermes": requires,
    })
    return info


def _engine_target() -> str:
    """引擎插件的名字（来自插件自述；读不到才用兜底常量）。"""
    return str(_engine_meta().get("name") or FALLBACK_ENGINE_NAME)


def _allowed_engines() -> List[str]:
    """``context.engine`` 的合法取值：宿主内置 ``compressor`` + 引擎插件自述的名字。"""
    target = _engine_target()
    return [BUILTIN_ENGINE] + ([target] if target and target != BUILTIN_ENGINE else [])


def _profiles() -> List[Dict[str, Any]]:
    """全部 agent（``default`` + ``profiles/*``，动态扫描）：id / home / 配置 / 引擎。

    ``engine`` 来自该 agent 自己的 ``config.yaml``；``engine_plugin`` / ``engine_installed`` /
    ``engine_version_actual`` 来自**该 agent 自己的** ``plugins/hermes-vcc-cn/plugin.yaml``
    ——运行时就是按这个目录解析引擎的，所以面板必须同口径。
    """
    scope = _core("scope")
    out: List[Dict[str, Any]] = []
    for entry in scope.list_agents(home=_home()):
        agent = str(entry.get("id") or "")
        home = Path(entry.get("home") or _agent_home(agent))
        config_path = Path(entry.get("config_path") or (home / "config.yaml"))
        engine, source = _read_engine(config_path)
        plugin = _engine_plugin_meta(home)
        out.append({"agent": agent, "home": home,
                    "config_path": config_path, "config_present": config_path.is_file(),
                    "engine": engine, "engine_source": source,
                    "engine_plugin": plugin,
                    "engine_installed": bool(plugin["installed"]),
                    "engine_version_actual": plugin["version"],
                    "engine_requires_hermes": plugin["requires_hermes"],
                    "engine_plugin_path": plugin["path"]})
    return out


# ---------------------------------------------------------------------------
# 日志：引擎自己的行（只读，真实原始行）
# ---------------------------------------------------------------------------
def _tail(path: Path, limit_bytes: int = LOG_TAIL_BYTES) -> List[str]:
    """读文件尾部 ``limit_bytes`` 字节，丢掉半行。缺文件/无权限 ⇒ 空列表（不抛）。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit_bytes:
                handle.seek(size - limit_bytes)
                handle.readline()  # 丢掉半行
            data = handle.read()
        return data.decode("utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001 - 日志缺失/占用都只是"没有数据"
        return []


def _log_lines(profile_home: Path) -> List[str]:
    lines: List[str] = []
    for rel in LOG_FILES:
        lines.extend(_tail(profile_home / rel))
    return lines


def _parse_logs(profile_home: Path) -> Dict[str, Any]:
    """从该 profile 的日志尾里抽引擎事件（压缩耗时、引擎切换、降级、失败、LLM 兜底）。"""
    compactions: List[Dict[str, Any]] = []
    using: List[Dict[str, Any]] = []
    fallbacks: List[Dict[str, Any]] = []
    degraded: List[str] = []
    failures: List[str] = []
    llm_dispatches: List[str] = []
    vcc_tail: List[str] = []
    for line in _log_lines(profile_home):
        if ("hermes-vcc-cn" not in line and "Using context engine" not in line
                and "Compression summary call dispatched" not in line
                and "falling back to built-in compressor" not in line):
            continue
        ts = line[:19]
        if "hermes-vcc-cn" in line:
            vcc_tail.append(line[:400])
        match = COMPACT_RE.search(line)
        if match:
            compactions.append({"ts": ts, "ms": float(match.group(1)),
                                "chars": int(match.group(2)), "turns": int(match.group(3))})
        match = USING_RE.search(line)
        if match:
            using.append({"ts": ts, "engine": match.group(1)})
        match = FALLBACK_RE.search(line)
        if match:
            fallbacks.append({"ts": ts, "engine": BUILTIN_ENGINE, "requested": match.group(1)})
        if DEGRADED_RE.search(line):
            degraded.append(line[:300])
        if FAILED_RE.search(line):
            failures.append(line[:300])
        if LLM_DISPATCH_RE.search(line):
            llm_dispatches.append(ts)
    return {
        "compactions": compactions,
        "using": using[-3:],
        "fallbacks": fallbacks[-3:],
        "degraded": degraded[-5:],
        "failures": failures[-5:],
        "vcc_tail": vcc_tail[-120:],
        # 计数口径：**当前日志尾巴窗口内**（约 1.2MB/文件）的调用次数，
        # 不是"自切换以来"的累计——面板上必须按这个口径标注。
        "llm_dispatch_in_tail": len(llm_dispatches),
        "llm_dispatch_last": llm_dispatches[-1] if llm_dispatches else "",
    }


# ---------------------------------------------------------------------------
# 实际生效引擎（日志证据）——配置里写的 ≠ 真的跑起来的
# ---------------------------------------------------------------------------
def _effective_engine(logs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """日志里**最近一次**引擎生效记录；没有就 ``None``（绝不拿配置冒充实际）。

    * ``source="using"``    —— 宿主 ``Using context engine: <name>``（插件加载成功，真的在用）；
    * ``source="fallback"`` —— 宿主 ``Context engine '<x>' not found — falling back …``
      （配置要插件，实际跑的是内置 ``compressor``）。

    两路都按时间戳取最新一条：日志行首是 ``YYYY-MM-DD HH:MM:SS``，字符串序即时间序。
    """
    candidates: List[Dict[str, Any]] = []
    for item in logs.get("using") or []:
        candidates.append({"ts": item.get("ts") or "", "engine": item.get("engine") or "",
                           "source": "using", "requested": item.get("engine") or ""})
    for item in logs.get("fallbacks") or []:
        candidates.append({"ts": item.get("ts") or "", "engine": item.get("engine") or BUILTIN_ENGINE,
                           "source": "fallback", "requested": item.get("requested") or ""})
    if not candidates:
        return None
    return max(candidates, key=lambda row: str(row.get("ts") or ""))


# ---------------------------------------------------------------------------
# DB：真正落库的压缩摘要（一律 mode=ro，按 id 下界封顶）
# ---------------------------------------------------------------------------
def _open_ro(db_path: Path) -> Optional[sqlite3.Connection]:
    """只读打开 profile 的 ``state.db``；不存在/打不开 ⇒ ``None``（路由翻成 404/空）。"""
    if not db_path.is_file():
        return None
    try:
        return sqlite3.connect("file:%s?mode=ro" % db_path.as_posix(), uri=True, timeout=20)
    except Exception as exc:  # noqa: BLE001 - 被占用时只读也打不开
        logger.warning("api_vcc: 打不开 %s（%s）", db_path, exc)
        return None


def _classify_summary(body: str, role: str) -> Optional[str]:
    """``"ours"`` / ``"legacy"`` / ``None`` —— 这行到底是不是一条压缩摘要。

    只靠 ``LIKE '%[Session Goal]%'`` 会把**提到**这个字符串的任何消息也算成摘要，
    所以做严格判定：assistant/user 行、长度 > 800，且（以 ``[CONTEXT COMPACTION`` 开头
    或命中 >=2 个栏目头）。
    """
    if role not in ("assistant", "user"):
        return None
    text = (body or "").lstrip()
    if len(text) < MIN_SUMMARY_CHARS:
        return None
    sections = [name for name in SECTION_NAMES if "[%s]" % name in text]
    prefixed = text.startswith(SUMMARY_PREFIX)
    if sections and (prefixed or len(sections) >= MIN_SECTION_HITS):
        return "ours"
    if prefixed:
        return "legacy"
    return None


def _summaries(profile_home: Path, limit: int = 20,
               scan_rows: int = DB_SCAN_ROWS) -> List[Dict[str, Any]]:
    """该 profile 落库的压缩摘要（严格判定 + 哨兵去重，按"自己的优先、新的优先"排序）。"""
    conn = _open_ro(profile_home / "state.db")
    if conn is None:
        return []
    try:
        max_id = conn.execute("select max(id) from messages").fetchone()[0] or 0
        floor = max(0, int(max_id) - max(0, int(scan_rows)))
        # 先宽筛（SQL 便宜），再逐条严格判定；多取 4 倍以便过滤后仍有足够条数
        rows = conn.execute(
            "select session_id, id, role, length(content), timestamp, content from messages "
            "where id >= ? and role in ('assistant','user') "
            "and (content like '%[Session Goal]%' or content like '[CONTEXT COMPACTION%') "
            "order by id desc limit ?", (floor, max(limit * 4, 40))).fetchall()
        out: List[Dict[str, Any]] = []
        seen: List[Tuple[str, int, int]] = []      # [(sid, ts_bucket, chars)] —— 去重哨兵
        for session, message_id, role, chars, ts, content in rows:
            kind = _classify_summary(content or "", role)
            if kind is None:
                continue          # 不是摘要：不列、不给按钮，杜绝"点了没反应"
            bucket = int(ts) // SUMMARY_DEDUP_WINDOW_S
            if any(sid == session and buck == bucket and size == chars
                   for sid, buck, size in seen):
                continue          # 同一摘要在库里存了多份，只留一份
            seen.append((session, bucket, chars))
            body = content or ""
            refs = REF_RE.findall(body)
            sections = [name for name in SECTION_NAMES if "[%s]" % name in body]
            out.append({
                "session": session,
                "message_id": message_id,
                "role": role,
                "chars": chars,
                "ts": time.strftime("%m-%d %H:%M:%S", time.localtime(ts)),
                "sections": sections,
                "refs": len(refs),
                "unique_refs": len(set(refs)),
                "kind": kind,
                "ours": kind == "ours",
                # 小压缩本来只能点名几条：面板要解释，别让人以为引用丢了
                "small": chars < 8000,
            })
            if len(out) >= limit:
                break
        out.sort(key=lambda row: (not row["ours"], -row["message_id"]))
        return out
    except Exception as exc:  # noqa: BLE001 - 表结构变了也只是"没有摘要"
        logger.warning("api_vcc: 摘要扫描失败 %s（%s）", profile_home / "state.db", exc)
        return []
    finally:
        conn.close()


def _last_compaction(profile_home: Path, parsed: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """最近一次压缩的真实数字：优先日志（有耗时），退回落库摘要（有引用数），都拿不到 ⇒ ``None``。"""
    logs = parsed if parsed is not None else _parse_logs(profile_home)
    compactions = logs.get("compactions") or []
    rows = _summaries(profile_home, limit=1)
    refs = rows[0]["unique_refs"] if rows else 0
    summary_id = rows[0]["message_id"] if rows else None
    if compactions:
        last = compactions[-1]
        return {"ts": last["ts"], "ms": last["ms"], "chars": last["chars"],
                "turns": last["turns"], "refs": refs, "summary_id": summary_id,
                "degraded": bool(logs.get("degraded")), "source": "log"}
    if summary_id is not None:
        return {"ts": rows[0]["ts"], "ms": None, "chars": rows[0]["chars"],
                "turns": None, "refs": refs, "summary_id": summary_id,
                "degraded": bool(logs.get("degraded")), "source": "db"}
    return None


# ---------------------------------------------------------------------------
# GET /vcc/overview —— 每个 agent 的引擎状态 + 最近压缩 + 计数
# ---------------------------------------------------------------------------
def _overview_payload(recent: int) -> Dict[str, Any]:
    """全 agent 总览（带 15s 缓存）。

    * ``engine`` / ``engine_source`` 来自**那一行自己的** ``config.yaml``；
    * ``engine_installed`` / ``engine_version_actual`` / ``engine_mismatch`` 来自
      **那一行自己的** ``plugins/hermes-vcc-cn/plugin.yaml``（运行时同口径）；
    * ``engine_version_global`` 是全局目录那一份，只用来标漂移，**不冒充**各 agent 的版本。
    """
    key = "overview:%d" % recent
    cached = _CACHE.get(key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]
    target = _engine_target()
    meta = _engine_meta()
    global_version = str(meta.get("version") or "")
    rows: List[Dict[str, Any]] = []
    for entry in _profiles():
        home = entry["home"]
        logs = _parse_logs(home)
        summaries = _summaries(home, limit=recent, scan_rows=OVERVIEW_SCAN_ROWS)
        actual = str(entry.get("engine_version_actual") or "")
        installed = bool(entry.get("engine_installed"))
        rows.append({
            # ``agent`` 是本文件的主字段；``profile`` 是旧面板的字段名，保留一版便于 V3b 迁移
            "agent": entry["agent"],
            "profile": entry["agent"],
            "engine": entry["engine"],
            "engine_source": entry["engine_source"],
            "active": entry["engine"] == target,
            "engine_target": target,
            # ★ 本卡核心：``engine_version`` 改成**该 agent 自己**的版本（旧值是全局版本，
            #   对 5 个只装了 0.3.1 的 agent 会假绿成 0.3.4）
            "engine_version": actual,
            "engine_version_actual": actual,
            "engine_version_global": global_version,
            "engine_installed": installed,
            "engine_mismatch": (not installed) or (actual != global_version),
            "engine_plugin_path": entry.get("engine_plugin_path") or "",
            "engine_requires_hermes": entry.get("engine_requires_hermes") or "",
            "config_path": str(entry["config_path"]),
            "config_present": entry["config_present"],
            "compactions_logged": len(logs["compactions"]),
            "last_compaction": logs["compactions"][-1] if logs["compactions"] else None,
            "using_last": logs["using"][-1] if logs["using"] else None,
            "effective_engine": _effective_engine(logs),
            "llm_dispatch_in_tail": logs["llm_dispatch_in_tail"],
            "llm_dispatch_last": logs["llm_dispatch_last"],
            "degraded": logs["degraded"],
            "failures": logs["failures"],
            "summaries": summaries,
            "summary_count": len(summaries),
            "state_db": (home / "state.db").is_file(),
        })
    payload: Dict[str, Any] = {
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "engine_target": target,
        "engine_plugin": meta,
        "engine_version_global": global_version,
        "engine_installed_count": sum(1 for row in rows if row["engine_installed"]),
        "engine_mismatch_count": sum(1 for row in rows if row["engine_mismatch"]),
        "count": len(rows),
        "profiles": rows,
        "note": ("每行的 engine 读自该 agent 自己的 config.yaml（context.engine）；"
                 "engine_version_actual 读自该 agent 自己的 plugins/hermes-vcc-cn/plugin.yaml"
                 "（运行时按 $HERMES_HOME/plugins 解析，不是全局目录）"),
    }
    _CACHE[key] = (time.time(), payload)
    return payload


@router.get("/vcc/overview")
def vcc_overview(
    agent: Optional[str] = Query(None, description="只返回这一个 agent 的行；缺省=全部 agent"),
    recent: int = Query(8, ge=1, le=50, description="每个 agent 附带多少条最近摘要"),
) -> Any:
    """全 agent 的引擎状态总览；给 ``agent`` 就只回那一行（引擎按该 agent 解析）。"""
    try:
        wanted = _agent_of(agent) if (agent or "").strip() else ""
        payload = _overview_payload(recent)
        if wanted:
            rows = [row for row in payload["profiles"] if row["agent"] == wanted]
            if not rows:
                raise ApiError(404, CODE_NOT_FOUND, "未知 agent：%s" % wanted,
                               agents=[row["agent"] for row in payload["profiles"]])
            payload = dict(payload, agent=wanted, profiles=rows, count=len(rows))
        return payload
    except ApiError as exc:
        return _error_response(exc)


# ---------------------------------------------------------------------------
# GET /vcc/log —— 引擎相关日志尾（真实原始行，便于核对）
# ---------------------------------------------------------------------------
@router.get("/vcc/log")
def vcc_log(
    agent: str = Query(DEFAULT_AGENT, description="agent(=profile) id，引擎/日志都按它解析"),
    limit: int = Query(80, ge=1, le=300, description="最多回多少行"),
) -> Any:
    """某 agent 自己 ``logs/*`` 里与引擎相关的原始行（不加工、不摘要）。"""
    try:
        name = _agent_of(agent)
        home = _agent_home(name)
        if not home.is_dir():
            raise ApiError(404, CODE_NOT_FOUND, "未知 agent：%s" % name,
                           agents=[row["agent"] for row in _profiles()])
        lines = _parse_logs(home)["vcc_tail"]
        shown = lines[-limit:]
        return {"ok": True, "agent": name, "profile": name, "limit": limit,
                # ``count`` 必须描述**返回的 lines**（之前是尾巴总数，和 lines 对不上，
                # 面板按 count 显示就会多报；总数另给 ``total_in_tail``）
                "count": len(shown), "total_in_tail": len(lines), "lines": shown,
                "config_path": str(home / "config.yaml"),
                "engine": _read_engine(home / "config.yaml")[0]}
    except ApiError as exc:
        return _error_response(exc)


# ---------------------------------------------------------------------------
# GET /vcc/compaction —— 一次压缩的输入与输出（逐条看它怎么组织的）
# ---------------------------------------------------------------------------
_BRIEF_SPLIT = "\n\n---"


def _parse_summary(body: str) -> Dict[str, Any]:
    """把落库摘要拆成「栏目 + brief 行」，便于逐条看它怎么组织的。"""
    sections: List[Dict[str, Any]] = []
    brief_lines: List[str] = []
    head, _, rest = body.partition(_BRIEF_SPLIT)
    current: Optional[Dict[str, Any]] = None
    for line in head.splitlines():
        match = re.match(r"^\[([^\]]+)\]$", line.strip())
        if match and match.group(1) in SECTION_NAMES:
            current = {"name": match.group(1), "items": []}
            sections.append(current)
            continue
        if current is not None and line.strip():
            current["items"].append(line.strip())
    for line in rest.splitlines():
        if line.strip():
            brief_lines.append(line.rstrip())
    return {"sections": sections, "brief_lines": brief_lines}


@router.get("/vcc/compaction")
def vcc_compaction(
    message_id: int = Query(..., description="摘要所在的 messages.id（从 /vcc/overview 的 summaries 里取）"),
    agent: str = Query(DEFAULT_AGENT, description="agent(=profile) id，state.db 按它定位"),
    sample: int = Query(220, ge=0, le=2000, description="窗口里未被点名的行抽样条数（0=不抽样）"),
) -> Any:
    """一次压缩的**输入与输出**：被压掉哪些消息、摘要怎么组织的、引用覆盖多少。"""
    try:
        name = _agent_of(agent)
        home = _agent_home(name)
        conn = _open_ro(home / "state.db")
        if conn is None:
            raise ApiError(404, CODE_NOT_FOUND, "该 agent 没有可读的 state.db",
                           agent=name, path=str(home / "state.db"))
        try:
            return _compaction_payload(conn, name, int(message_id), int(sample))
        finally:
            conn.close()
    except ApiError as exc:
        return _error_response(exc)


def _compaction_payload(conn: sqlite3.Connection, agent: str, message_id: int,
                        sample: int) -> Dict[str, Any]:
    """``/vcc/compaction`` 的取数主体（已在只读连接里，异常由调用方翻成结构化错误）。"""
    row = conn.execute(
        "select session_id, role, content, length(content), timestamp from messages where id = ?",
        (message_id,)).fetchone()
    if not row:
        raise ApiError(404, CODE_NOT_FOUND, "找不到该摘要行 #%s" % message_id,
                       agent=agent, message_id=message_id)
    session, role, body, chars, ts = row
    body = body or ""
    kind = _classify_summary(body, role)
    if kind is None:
        raise ApiError(400, CODE_NOT_A_SUMMARY,
                       "这条消息不是压缩摘要（它只是提到了摘要字段）。请从 /vcc/overview 的 "
                       "summaries 里选带栏目的那一行。", agent=agent, message_id=message_id)
    refs = [int(item) for item in REF_RE.findall(body)]
    parsed = _parse_summary(body)

    stats = conn.execute(
        "select count(*), sum(case when active=1 then 1 else 0 end), "
        "sum(case when compacted=1 then 1 else 0 end), "
        "sum(case when compacted=1 then length(content) else 0 end), "
        "min(case when compacted=1 then id end), max(case when compacted=1 then id end) "
        "from messages where session_id = ?", (session,)).fetchone()
    total, actives, compacted, compacted_chars, first_id, last_id = stats

    cited_rows: List[Dict[str, Any]] = []
    if refs:
        placeholders = ",".join("?" * len(refs))
        brief_map: Dict[int, str] = {}
        for line in parsed["brief_lines"]:
            found = REF_RE.search(line)
            if found:
                brief_map[int(found.group(1))] = line.strip()
        for cited in conn.execute(
                "select id, role, length(content), timestamp, substr(content, 1, 200) from messages "
                "where session_id = ? and id in (%s) order by id" % placeholders,
                (session, *refs)):
            rid, rrole, rchars, rts, preview = cited
            cited_rows.append({
                "id": rid, "role": rrole, "chars": rchars,
                "ts": time.strftime("%m-%d %H:%M:%S", time.localtime(rts)),
                "preview": (preview or "").replace("\n", " "),
                "brief_line": brief_map.get(rid, ""),
            })
    cited_ids = {item["id"] for item in cited_rows}

    by_role: Dict[str, int] = {}
    for r in conn.execute("select role, count(*) from messages where session_id=? and compacted=1 "
                          "group by role", (session,)):
        by_role[r[0]] = r[1]
    cited_role: Dict[str, int] = {}
    for item in cited_rows:
        cited_role[item["role"]] = cited_role.get(item["role"], 0) + 1

    # 抽样展示窗口里没被点名的行（默认只看被点名的，避免把 1800+ 行刷到前端）
    others: List[Dict[str, Any]] = []
    if sample and compacted:
        step = max(1, int(compacted) // max(1, int(sample)))
        for index, r in enumerate(conn.execute(
                "select id, role, length(content), timestamp, substr(content, 1, 120) from messages "
                "where session_id = ? and compacted = 1 order by id", (session,))):
            if r[0] in cited_ids or index % step:
                continue
            others.append({
                "id": r[0], "role": r[1], "chars": r[2],
                "ts": time.strftime("%m-%d %H:%M:%S", time.localtime(r[3])),
                "preview": (r[4] or "").replace("\n", " "),
                "brief_line": "",
            })
            if len(others) >= int(sample):
                break

    return {
        "ok": True,
        "agent": agent,
        "profile": agent,
        "session": session,
        "kind": kind,
        # 旧格式摘要没有栏目可解析：原文照给，面板做只读降级视图
        "raw_excerpt": body[:8000] + ("…[截断]" if len(body) > 8000 else ""),
        "summary": {
            "message_id": int(message_id), "role": role, "chars": chars,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
            "sections": parsed["sections"], "brief_lines": parsed["brief_lines"],
            "refs": len(refs), "unique_refs": len(set(refs)),
        },
        "window": {
            "rows": compacted or 0, "chars": compacted_chars or 0,
            "first_id": first_id, "last_id": last_id, "by_role": by_role,
        },
        "kept_tail": {"rows": actives or 0},
        "session_total_rows": total or 0,
        "cited": {"count": len(cited_rows), "by_role": cited_role,
                  "coverage_pct": round(100.0 * len(cited_rows) / max(1, compacted or 1), 2)},
        "cited_rows": cited_rows,
        "sampled_rows": others,
        "ratio_pct": round(100.0 * chars / max(1, compacted_chars or 1), 2),
    }


# ---------------------------------------------------------------------------
# POST /vcc/engine —— 切换某 agent 的压缩引擎（唯一写操作，写后回读）
# ---------------------------------------------------------------------------
def _write_engine(agent: str, want: str) -> None:
    """把 ``context.engine`` 写进**该 agent 自己的** ``config.yaml``。

    走官方写入器（照抄 ``core.plugin_switch._write_disabled`` 的骨架，先例不是自创）：
    ``_config_profile_scope`` 官方 profile 作用域 → ``_CONFIG_LOCK`` 官方锁 →
    ``read_user_config_raw()`` fail-closed 原始读 → ``save_config(partial, merge_existing=True)``。

    只提交 ``{"context": {"engine": want}}`` 这一块：磁盘上 ``context`` 的其它键
    （``context.vcc.budget_ceiling_tokens`` 这类引擎参数）由官方 merge 带过来，本模块在
    结构上改不到它们。
    """
    from hermes_cli import config as config_mod  # 官方读写 + 官方跨进程锁
    from hermes_cli import managed_scope  # 管理员托管键判定
    from hermes_cli.web_server_profiles import _config_profile_scope  # 官方 profile 作用域

    dotted = "context.engine"
    if config_mod.is_managed():
        raise ApiError(403, CODE_PERMISSION_DENIED,
                       "托管安装（managed install）不允许修改 %s" % dotted)
    if managed_scope.is_key_managed(dotted):
        raise ApiError(403, CODE_PERMISSION_DENIED, "%r 由管理员托管，无法修改" % dotted)

    partial = {"context": {"engine": want}}
    scope = None if agent == DEFAULT_AGENT else agent
    try:
        with (_config_profile_scope(scope), config_mod._CONFIG_LOCK):
            config_mod.read_user_config_raw()   # fail-closed：YAML 坏掉就抛，不覆盖
            config_mod.save_config(partial, preserve_keys={("context", "engine")},
                                   merge_existing=True)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - 官方作用域不可用 → 结构化 4xx，不是 500 栈
        if exc.__class__.__name__ == "HTTPException" and getattr(exc, "status_code", None) in (400, 404):
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "agent %r 的写入作用域不可用：%s" % (agent, exc), agent=agent) from exc
        raise ApiError(500, CODE_UPSTREAM_ERROR,
                       "写 %s 失败：%s: %s" % (dotted, type(exc).__name__, exc), agent=agent) from exc


@router.post("/vcc/engine")
def vcc_set_engine(payload: Dict[str, Any] = Body(...)) -> Any:
    """切换某 agent 的压缩引擎，并**回读文件**确认真实结果。

    body：``{"agent": "code-worker", "engine": "hermes-vcc-cn", "confirm": true}``

    * ``confirm`` 不是 ``true`` ⇒ **400** ``confirmation_required``（一个字节都不写）；
    * ``engine`` 只接受 ``compressor`` 或引擎插件自述的名字 ⇒ 否则 400；
    * 切到插件引擎前先校验**该 agent 自己**的 ``plugins/<engine>/plugin.yaml`` 在不在
      ⇒ 不在就 **409** ``engine_not_installed``（只写 config.yaml 会让宿主静默回退内置
      compressor，那是假成功）；
    * 写后回读 ``config.yaml``：与期望不符 ⇒ 409 ``write_not_applied``（绝不假报成功）。
    """
    try:
        body = payload if isinstance(payload, dict) else {}
        agent = _agent_of(body.get("agent"))
        want = str(body.get("engine") or "").strip()
        allowed = _allowed_engines()
        if want not in allowed:
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "engine 只接受 %s（当前引擎插件自述：%s）"
                           % (" / ".join(allowed), _engine_meta().get("name")),
                           allowed=allowed, agent=agent)
        if body.get("confirm") is not True:
            raise ApiError(400, CODE_CONFIRMATION_REQUIRED,
                           "切换压缩引擎需要 confirm=true（会改 %s 的 context.engine，"
                           "正在跑的会话仍用旧引擎，新会话生效）" % agent,
                           agent=agent, engine=want, current=_read_engine(_agent_config_path(agent))[0])
        # ★ 本卡核心：切换前先校验**该 agent 自己**的引擎本体在不在。
        #   运行时按 $HERMES_HOME/plugins 解析引擎；这里只写了 config.yaml 而插件没装，
        #   宿主会静默回退内置 compressor —— 那种「成功」就是假成功，必须 409 顶回去。
        home = _agent_home(agent)
        plugin = _engine_plugin_meta(home)
        if want != BUILTIN_ENGINE and not plugin["installed"]:
            raise ApiError(
                409, CODE_ENGINE_NOT_INSTALLED,
                "没切：%s 自己的 plugins 目录里没有引擎插件 %s（%s 不存在）。"
                "只写 config.yaml 会让宿主静默回退内置 compressor，等于假成功。"
                "先把引擎装到该 agent 的目录（或建 junction）再切。"
                % (agent, want, plugin["path"]),
                agent=agent, engine=want, plugin_dir=plugin["dir"],
                plugin_path=plugin["path"],
                engine_version_global=str(_engine_meta().get("version") or ""),
                engine_installed=False)
        config_path = _agent_config_path(agent)
        before, before_source = _read_engine(config_path)
        if before == want:
            return {"ok": True, "agent": agent, "engine": before, "before": before,
                    "engine_source": before_source, "changed": False, "config_path": str(config_path),
                    "message": "本来就是 %s（未改动）" % want}
        _write_engine(agent, want)
        after, after_source = _read_engine(config_path)
        _CACHE.clear()          # 总览缓存必须失效，否则面板还显示旧引擎
        if after != want:
            raise ApiError(409, CODE_WRITE_NOT_APPLIED,
                           "写入未生效：config.yaml 回读仍为 %s" % after,
                           agent=agent, engine=after, expected=want, config_path=str(config_path))
        return {"ok": True, "agent": agent, "engine": after, "before": before,
                "engine_source": after_source, "changed": after != before,
                "config_path": str(config_path),
                "message": "已切换到 %s（回读确认）" % after,
                "note": "已开着的会话仍用旧引擎，新会话生效"}
    except ApiError as exc:
        return _error_response(exc)


# ---------------------------------------------------------------------------
# POST /vcc/search_archived —— 在已归档消息里按关键词搜（含工具输出）
# ---------------------------------------------------------------------------
@router.post("/vcc/search_archived")
def vcc_search_archived(payload: Dict[str, Any] = Body(...)) -> Any:
    """在**已归档**消息里按关键词搜索（含工具输出）。

    面板显示"引用覆盖 3%"会让人以为丢东西，其实其余消息都还在库里，只是没进目录。
    这里就是让那 97% 可点可搜。``role_filter`` 固定带 ``tool``：Hermes 的
    session_search 默认只搜 user/assistant，工具输出会搜不到（已知坑）。
    """
    try:
        body = payload if isinstance(payload, dict) else {}
        agent = _agent_of(body.get("agent"))
        query = str(body.get("query") or "").strip()
        if not query:
            raise ApiError(400, CODE_INVALID_ARGUMENT, "关键词为空", agent=agent)
        session = str(body.get("session") or "").strip()
        conn = _open_ro(_agent_home(agent) / "state.db")
        if conn is None:
            raise ApiError(404, CODE_NOT_FOUND, "该 agent 没有可读的 state.db",
                           agent=agent, path=str(_agent_home(agent) / "state.db"))
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        try:
            params: List[Any] = [SEARCH_PREVIEW, like]
            sql = ("select id, role, substr(content, 1, ?) from messages "
                   "where content like ? escape '\\'")
            if session:
                sql += " and session_id = ?"
                params.append(session)
            sql += " order by id desc limit ?"
            params.append(SEARCH_LIMIT)
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        hits = [{"id": row[0], "role": row[1],
                 "preview": (row[2] or "").replace("\n", " ").strip()} for row in rows]
        return {"ok": True, "agent": agent, "profile": agent, "query": query,
                "session": session, "count": len(hits), "hits": hits,
                "role_filter": "user,assistant,tool",
                "note": "搜的是整库（含被压缩归档的消息），不只当前摘要"}
    except ApiError as exc:
        return _error_response(exc)


# ---------------------------------------------------------------------------
# GET /vcc/engine_status —— 引擎自检（引擎名按 agent 解析 + 最近一次压缩的真实数字）
# ---------------------------------------------------------------------------
@router.get("/vcc/engine_status")
def vcc_engine_status(
    agent: str = Query(DEFAULT_AGENT, description="agent(=profile) id：引擎名与配置都按它解析"),
) -> Any:
    """某 agent 的引擎自检：**引擎名读自该 agent 自己的 config.yaml**，最近一次压缩的真实数字
    （拿不到就 ``null``，绝不编）。"""
    try:
        name = _agent_of(agent)
        config_path = _agent_config_path(name)
        engine, source = _read_engine(config_path)
        target = _engine_target()
        meta = _engine_meta()
        home = _agent_home(name)
        context = _read_context_block(config_path)
        last = _last_compaction(home) if home.is_dir() else None
        plugin = _engine_plugin_meta(home)
        global_version = str(meta.get("version") or "")
        actual = str(plugin.get("version") or "")
        installed = bool(plugin.get("installed"))
        logs = _parse_logs(home) if home.is_dir() else {}
        effective = _effective_engine(logs) if logs else None
        warnings: List[str] = []
        if engine != target:
            warnings.append("该 agent 当前引擎不是 %s（而是 %s），下面的记录可能来自内置 compressor"
                            % (target, engine))
        if engine == target and not installed:
            warnings.append("config.yaml 写的是 %s，但该 agent 自己的 plugins 目录里没有引擎插件"
                            "（%s 不存在）⇒ 运行时一定会回退内置 compressor" % (target, plugin.get("path")))
        if installed and actual and global_version and actual != global_version:
            warnings.append("该 agent 的引擎插件是 %s，全局目录那份是 %s（版本漂移；"
                            "面板按各 agent 自己的目录显示）" % (actual, global_version))
        return {
            "ok": True,
            "agent": name,
            "profile": name,
            # ★ 本卡核心：这个 engine 来自上面那份 config_path，不是写死的 default
            "engine": engine,
            "engine_source": source,
            "engine_target": target,
            "is_target": engine == target,
            "config_path": str(config_path),
            "config_present": config_path.is_file(),
            "engine_plugin": meta,
            # ★ 该 agent 自己目录里那一份（不是全局那份）
            "engine_installed": installed,
            "engine_version_actual": actual,
            "engine_version_global": global_version,
            "engine_mismatch": (not installed) or (actual != global_version),
            "engine_requires_hermes": plugin.get("requires_hermes") or "",
            "engine_plugin_path": plugin.get("path") or "",
            "engine_plugin_dir": plugin.get("dir") or "",
            "context": context,
            "last_compaction": last,
            # ★ 日志里最近一次「真的生效」的引擎（using 优先 / 回退警告），没有就是 null
            "effective_engine": effective,
            "warning": "；".join(warnings) if warnings else None,
        }
    except ApiError as exc:
        return _error_response(exc)


# ===========================================================================
# 按对话下钻（t_c4c4d49e）：GET /vcc/conversations · GET /vcc/conversation
# ---------------------------------------------------------------------------
# 全部只读；两个口子都只从 ``<profile>/state.db`` 读，字段级口径写进响应的
# ``data_sources``，前端原样展示 —— 面板上每个数字都能在这里对到「哪个库哪张表哪列」。
# ===========================================================================
RECALL_TOOL = "session_search"          # 宿主召回工具名（messages.tool_name）
MEMORY_FENCE = "<memory-context>"       # 记忆召回注入块的围栏（messages.content 里）
SUMMARY_PREFIX_SQL = "[CONTEXT COMPACTION"   # 宿主压缩摘要的正文前缀
VCC_SECTION_SQL = "%[Session Goal]%"        # vcc 引擎摘要的栏目头
#: 一条消息算「压缩摘要」的 SQL 条件（与 :func:`_summary_kind` / :func:`_classify_summary` 同口径）。
#: 只用 ``like '%[Session Goal]%'`` 会把**提到**这个字符串的任何消息（比如我们自己在讨论压缩）
#: 也算成摘要，所以这里同时要求「正文 >= 800 字符 + 至少两个栏目头」，与 Python 侧判定对齐。
SUMMARY_SQL_COND = (
    "(role in ('assistant', 'user') "
    "and (_compressed_summary = 1 or content like '%s%%' "
    "or (length(coalesce(content, '')) >= %d and content like '%s' "
    "and (content like '%%[Files And Changes]%%' or content like '%%[Outstanding Context]%%' "
    "or content like '%%[Key Outputs]%%' or content like '%%[Tracked Commands]%%' "
    "or content like '%%[Commits]%%' or content like '%%[User Preferences]%%'))))"
    % (SUMMARY_PREFIX_SQL, MIN_SUMMARY_CHARS, VCC_SECTION_SQL))
AGG_CACHE_TTL = 20.0                    # 686 个会话的全量聚合 ≈ 2s，缓存 20s
CONV_LIMIT_DEFAULT = 250
CONV_LIMIT_MAX = 800

#: ``/vcc/conversations`` 列表每行只发**真的有人读**的字段：前端对话列表
#: （``src/sections/vcc.js::ConvItem`` + 搜索/筛选）与契约探针
#: （``scripts/probe_vcc_conversations.py``）合起来就这些；其余（title/cwd/source/
#: chat_type/profile_name/token_rows/first_ts…）在 ``/vcc/conversation`` 详情里仍然
#: 一个不少。300 行时响应体从 ~283KB 降到 ~111KB，默认页（250 行）≈ 92KB。
#: 需要旧形态加 ``?full=1``。
CONV_ROW_FIELDS: Tuple[str, ...] = (
    "session", "label", "model",
    "rows_total", "rows_active", "chars_active", "rows_archived",
    "summary_rows", "summary_distinct", "recall_total",
    "has_compression", "has_recall",
    "tokens", "last_ts_text",
)
EVENT_LIMIT_DEFAULT = 40
PREVIEW_CHARS = 600                     # 每条摘要给前端的预览字符数
EXCERPT_CHARS = 2600                    # 「看全文」用的片段（明确标注会截断）
SNIPPET_CHARS = 300                     # 召回命中片段的字符数
MEMORY_BULLET_CHARS = 240
MEMORY_BULLET_LIMIT = 6

#: 一次 SQL 拿到每个会话的全部聚合（避免 N+1）。列顺序见 :func:`_aggregate_row`。
AGG_SQL = """
select session_id,
       count(*),
       sum(case when active = 1 then 1 else 0 end),
       sum(case when active = 1 then length(coalesce(content, '')) else 0 end),
       sum(case when compacted = 1 then 1 else 0 end),
       sum(case when compacted = 1 then length(coalesce(content, '')) else 0 end),
       sum(case when %s then 1 else 0 end),
       count(distinct case when %s then content end),
       sum(case when tool_name = '%s' then 1 else 0 end),
       sum(case when instr(coalesce(content, ''), '%s') > 0 then 1 else 0 end),
       sum(case when token_count is not null then 1 else 0 end),
       sum(case when token_count is not null then token_count else 0 end),
       min(timestamp), max(timestamp)
  from messages
 group by session_id
""" % (SUMMARY_SQL_COND, SUMMARY_SQL_COND, RECALL_TOOL, MEMORY_FENCE)


def _fmt_ts(ts: Any, with_date: bool = True) -> str:
    """epoch 秒 → ``YYYY-MM-DD HH:MM:SS`` / ``MM-DD HH:MM``；拿不到就空串（不填假时间）。"""
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%m-%d %H:%M",
                         time.localtime(value))


def _aggregate_row(row: Tuple[Any, ...]) -> Dict[str, Any]:
    """AGG_SQL 的一行 → dict（字段名与前端一一对应）。"""
    (session, total, active_rows, active_chars, arch_rows, arch_chars,
     summary_rows, summary_distinct, recall_search, recall_memory,
     token_rows, token_sum, first_ts, last_ts) = row
    return {
        "rows_total": int(total or 0),
        "rows_active": int(active_rows or 0),
        "chars_active": int(active_chars or 0),
        "rows_archived": int(arch_rows or 0),
        "chars_archived": int(arch_chars or 0),
        "summary_rows": int(summary_rows or 0),
        "summary_distinct": int(summary_distinct or 0),
        "recall_search": int(recall_search or 0),
        "recall_memory": int(recall_memory or 0),
        "recall_total": int(recall_search or 0) + int(recall_memory or 0),
        "token_rows": int(token_rows or 0),
        "token_sum": int(token_sum or 0),
        "first_ts": float(first_ts or 0),
        "last_ts": float(last_ts or 0),
    }


def _session_aggregates(profile_home: Path) -> Dict[str, Any]:
    """该 agent 全部会话的消息聚合（一条 SQL；按 state.db 的 mtime/size 缓存 20s）。"""
    db_path = profile_home / "state.db"
    try:
        stat = db_path.stat()
    except OSError:
        return {"ok": False, "reason": "state.db 不存在", "sessions": {}, "db": str(db_path)}
    key = "agg:%s|%d|%d" % (db_path.as_posix(), int(stat.st_mtime), stat.st_size)
    cached = _CACHE.get(key)
    if cached and time.time() - cached[0] < AGG_CACHE_TTL:
        return cached[1]
    conn = _open_ro(db_path)
    if conn is None:
        return {"ok": False, "reason": "state.db 打不开（被占用？）", "sessions": {}, "db": str(db_path)}
    started = time.time()
    try:
        rows = conn.execute(AGG_SQL).fetchall()
    except Exception as exc:  # noqa: BLE001 - 表结构变了也只是「没有数据」，不 500
        logger.warning("api_vcc: 会话聚合失败 %s（%s）", db_path, exc)
        return {"ok": False, "reason": "聚合查询失败：%s: %s" % (type(exc).__name__, exc),
                "sessions": {}, "db": str(db_path)}
    finally:
        conn.close()
    payload = {
        "ok": True,
        "db": str(db_path),
        "reason": None,
        "sessions": {str(row[0]): _aggregate_row(row) for row in rows},
        "scanned": len(rows),
        "seconds": round(time.time() - started, 2),
    }
    _CACHE[key] = (time.time(), payload)
    return payload


def _sessions_table(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """``sessions`` 表的元信息（标题 / 模型 / 宿主自己记的 message_count / 活动时间）。"""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        rows = conn.execute(
            "select id, title, display_name, model, message_count, started_at, ended_at, "
            "last_activity_at, archived, hidden, cwd, source, chat_type, profile_name "
            "from sessions").fetchall()
    except Exception as exc:  # noqa: BLE001 - 老库没有某列时退回最小列集
        logger.warning("api_vcc: sessions 表读取失败（%s），退回最小列集", exc)
        rows = []
        try:
            for row in conn.execute("select id, title, message_count, last_activity_at from sessions"):
                rows.append((row[0], row[1], None, None, row[2], None, None, row[3],
                             None, None, None, None, None, None))
        except Exception:  # noqa: BLE001
            return {}
    for row in rows:
        (sid, title, display_name, model, message_count, started_at, ended_at,
         last_activity_at, archived, hidden, cwd, source, chat_type, profile_name) = row
        out[str(sid)] = {
            "title": title or "",
            "display_name": display_name or "",
            "model": model or "",
            "message_count": int(message_count or 0),
            "started_at": float(started_at or 0),
            "ended_at": float(ended_at or 0),
            "last_activity_at": float(last_activity_at or 0),
            "archived": bool(archived),
            "hidden": bool(hidden),
            "cwd": cwd or "",
            "source": source or "",
            "chat_type": chat_type or "",
            "profile_name": profile_name or "",
        }
    return out


def _conversation_row(session: str, meta: Dict[str, Any], agg: Dict[str, Any]) -> Dict[str, Any]:
    """列表里的一行：``sessions`` 的元信息 + ``messages`` 的聚合，**一个数字都不补**。"""
    row = dict(meta or {})
    row.update(agg or {})
    row["session"] = session
    row["label"] = (row.get("title") or row.get("display_name") or "").strip() or session
    # 「压缩过」只认摘要行（_compressed_summary / 摘要正文）；归档行是另一件事：
    # 310 个会话有 compacted=1 的消息但摘要行已被宿主删掉，那种情况只能显示「无记录 + 有归档」。
    row["has_compression"] = bool(row.get("summary_rows"))
    row["has_archived"] = bool(row.get("rows_archived"))
    row["has_recall"] = bool(row.get("recall_total"))
    # token 是否真的记过（本机全库为 0 ⇒ 面板显示「无记录」，不给 0 假装有值）
    row["tokens"] = row.get("token_sum") if row.get("token_rows") else None
    row["first_ts_text"] = _fmt_ts(row.get("first_ts"))
    row["last_ts_text"] = _fmt_ts(row.get("last_ts"), with_date=False)
    row["last_activity_text"] = _fmt_ts(row.get("last_activity_at"), with_date=False)
    return row


def _data_sources(db_path: Path, home: Path) -> List[Dict[str, str]]:
    """面板上每个数字的来源（库 / 表 / 字段 / 口径），前端原样贴出来。"""
    db = str(db_path)
    return [
        {"item": "对话列表（标题/模型/消息数/最后活动）", "where": db, "table": "sessions",
         "fields": "id, title, display_name, model, message_count, last_activity_at, archived",
         "note": "宿主自己记的会话元信息，原样展示"},
        {"item": "上下文条数与字符数", "where": db, "table": "messages",
         "fields": "count(*) / length(content)，条件 active = 1",
         "note": "字符数是数出来的，不是估的；token 见下一行"},
        {"item": "token 数 / 占模型窗口比例", "where": db, "table": "messages",
         "fields": "token_count",
         "note": "本机全库该列 100% 为 NULL ⇒ 面板显示「无记录」；不拿字符数折算 token"},
        {"item": "模型窗口", "where": str(home / "context_length_cache.yaml"), "table": "context_lengths",
         "fields": "context_lengths[\"<model>@<base_url>\"]",
         "note": "宿主自己的运行时缓存；缓存里没有该模型 ⇒ 无记录（不去 models.dev 猜同名模型）"},
        {"item": "压缩次数与摘要", "where": db, "table": "messages",
         "fields": "_compressed_summary = 1 / content 前缀 [CONTEXT COMPACTION / [Session Goal] 栏目",
         "note": "_compressed_summary 是宿主自己的摘要标记；同一段摘要被重写多次时按内容去重后单独标注"},
        {"item": "压缩前后长度", "where": db, "table": "messages",
         "fields": "compacted = 1 的 length(content)；摘要行自身 length(content)",
         "note": "「压缩前」= 截至这次压缩时该会话已归档消息的字符数（累计口径）；「本次新增」= 与上一条摘要之间的区间；都是数出来的"},
        {"item": "召回（会话）", "where": db, "table": "messages",
         "fields": "tool_name = 'session_search' 的 content（JSON：query / results[].title / results[].preview）",
         "note": "就是宿主真调用召回工具的记录，命中内容原样给出"},
        {"item": "召回（记忆）", "where": db, "table": "messages",
         "fields": "content 里的 <memory-context> 注入块",
         "note": "记忆 provider 每轮注入的召回块，块内条目原样截取"},
    ]


def _summary_kind(flag: Any, content: str, role: str) -> Optional[str]:
    """这行到底是不是压缩摘要 → ``ours`` / ``legacy`` / ``flagged`` / ``None``。

    ``_compressed_summary = 1`` 是**宿主自己**打的标记（权威）；没有标记但内容命中
    ``[CONTEXT COMPACTION`` / 两个以上栏目头的也算（vcc 引擎早期摘要不带标记）。
    """
    kind = _classify_summary(content or "", role)
    if kind:
        return kind
    if flag:
        return "flagged"
    return None


def _compression_events(conn: sqlite3.Connection, session: str,
                        limit: int) -> Dict[str, Any]:
    """该会话的压缩事件：摘要行 → 按内容去重 → 每次的摘要 + 压缩前后长度。

    * 压缩次数 = 去重后的摘要条目数（同一段摘要在库里被重写多次时只算一次，
      但会写清 ``rows`` 是几行）；
    * 压缩前长度 = 截至该次摘要时，``compacted = 1`` 的行数与字符数（累计口径）；
    * 本次新增 = 与上一条摘要之间的区间（按 messages.id 切分）；
    * 压缩后长度 = 摘要行自身的字符数。
    """
    import bisect

    rows = conn.execute(
        "select id, role, length(coalesce(content, '')), timestamp, content, _compressed_summary "
        "from messages where session_id = ? and %s order by id" % SUMMARY_SQL_COND,
        (session,)).fetchall()
    groups: List[Dict[str, Any]] = []
    index: Dict[str, Dict[str, Any]] = {}
    accepted = 0
    for message_id, role, chars, ts, content, flag in rows:
        kind = _summary_kind(flag, content or "", role)
        if kind is None:
            continue
        accepted += 1
        key = content or ""
        group = index.get(key)
        if group is None:
            group = {"message_ids": [], "chars": int(chars or 0), "kind": kind,
                     "first_ts": float(ts or 0), "last_ts": float(ts or 0),
                     "role": role, "content": key}
            index[key] = group
            groups.append(group)
        group["message_ids"].append(int(message_id))
        group["last_ts"] = max(group["last_ts"], float(ts or 0))

    arch = conn.execute(
        "select id, length(coalesce(content, '')) from messages "
        "where session_id = ? and compacted = 1 order by id", (session,)).fetchall()
    arch_ids = [int(item[0]) for item in arch]
    prefix_rows = [0]
    prefix_chars = [0]
    for _, size in arch:
        prefix_rows.append(prefix_rows[-1] + 1)
        prefix_chars.append(prefix_chars[-1] + int(size or 0))

    events: List[Dict[str, Any]] = []
    prev_rows = prev_chars = 0
    for group in groups[:max(1, limit)]:
        first_id = group["message_ids"][0]
        cut = bisect.bisect_left(arch_ids, first_id)      # 严格 id < first_id 的归档行
        cum_rows = prefix_rows[cut]
        cum_chars = prefix_chars[cut]
        body = group["content"] or ""
        refs = REF_RE.findall(body)
        sections = [name for name in SECTION_NAMES if "[%s]" % name in body]
        events.append({
            "message_id": first_id,
            "message_ids": group["message_ids"],
            "rows": len(group["message_ids"]),
            "role": group["role"],
            "kind": group["kind"],
            "ts": _fmt_ts(group["first_ts"]),
            "last_ts": _fmt_ts(group["last_ts"]),
            "chars": group["chars"],
            "sections": sections,
            "refs": len(refs),
            "unique_refs": len(set(refs)),
            # 压缩前（累计口径，真实行数与字符数）
            "before_rows": cum_rows,
            "before_chars": cum_chars,
            # 本次新增归档（与上一条摘要之间的区间）
            "delta_rows": cum_rows - prev_rows,
            "delta_chars": cum_chars - prev_chars,
            # 压缩后（摘要自身）
            "after_chars": group["chars"],
            "preview": body[:PREVIEW_CHARS] + ("…[截断]" if len(body) > PREVIEW_CHARS else ""),
            "excerpt": body[:EXCERPT_CHARS] + ("…[截断]" if len(body) > EXCERPT_CHARS else ""),
            "tokens": None,
        })
        prev_rows, prev_chars = cum_rows, cum_chars

    return {
        "count": len(events),
        "rows": accepted,
        "matched_rows": len(rows),
        "events": events,
        "note": ("压缩次数 = 库里去重后的摘要条目数（同一段摘要被重写多次只算一次，"
                 "每条会写明在库里有几行）。压缩前长度是「截至这次压缩，该会话已归档"
                 "（compacted=1）消息的条数与字符数」，累计口径；本次新增是与上一条摘要"
                 "之间的区间。token 数没有落库，所以不显示 token。"),
    }


def _recall_events(conn: sqlite3.Connection, session: str, limit: int) -> Dict[str, Any]:
    """该会话的召回记录：会话召回（session_search）+ 记忆召回注入（memory-context）。"""
    search_rows = conn.execute(
        "select id, timestamp, length(coalesce(content, '')), content from messages "
        "where session_id = ? and tool_name = ? order by id desc limit ?",
        (session, RECALL_TOOL, max(1, limit))).fetchall()
    search_events: List[Dict[str, Any]] = []
    hit_total = 0
    for message_id, ts, chars, content in search_rows:
        body = content or ""
        query, mode, hits, parsed = "", "", [], False
        try:
            data = json.loads(body)
        except Exception:  # noqa: BLE001 - 老格式/截断：不解析，原文照给
            data = None
        if isinstance(data, dict):
            parsed = True
            query = str(data.get("query") or "")
            mode = str(data.get("mode") or "")
            for item in (data.get("results") or [])[:8]:
                if not isinstance(item, dict):
                    continue
                hits.append({
                    "session": str(item.get("session_id") or ""),
                    "title": str(item.get("title") or ""),
                    "when": _fmt_ts(item.get("started_at") or item.get("last_active")),
                    "source": str(item.get("source") or ""),
                    "messages": item.get("message_count"),
                    "preview": " ".join(str(item.get("preview") or "").split())[:SNIPPET_CHARS],
                })
        hit_total += len(hits)
        search_events.append({
            "message_id": int(message_id),
            "ts": _fmt_ts(ts),
            "chars": int(chars or 0),
            "query": query,
            "mode": mode,
            "parsed": parsed,
            "hits": hits,
            "hit_count": len(hits),
            "snippet": " ".join(body.split())[:SNIPPET_CHARS] if not hits else "",
        })

    memory_rows = conn.execute(
        "select id, timestamp, length(coalesce(content, '')), content from messages "
        "where session_id = ? and instr(coalesce(content, ''), ?) > 0 order by id desc limit ?",
        (session, MEMORY_FENCE, max(1, limit))).fetchall()
    memory_events: List[Dict[str, Any]] = []
    for message_id, ts, chars, content in memory_rows:
        body = content or ""
        start = body.find(MEMORY_FENCE)
        end = body.find("</memory-context>", start + 1)
        block = body[start + len(MEMORY_FENCE):end if end > 0 else len(body)]
        bullets = []
        for line in block.splitlines():
            text = line.strip()
            if text.startswith("- "):
                bullets.append(text[2:][:MEMORY_BULLET_CHARS])
            if len(bullets) >= MEMORY_BULLET_LIMIT:
                break
        own = " ".join(body[:start].split()) if start > 0 else ""
        memory_events.append({
            "message_id": int(message_id),
            "ts": _fmt_ts(ts),
            "chars": int(chars or 0),
            "block_chars": len(block),
            "bullets": bullets,
            "bullet_count": block.count("\n- ") + (1 if block.strip().startswith("- ") else 0),
            "own_text": own[:SNIPPET_CHARS],
        })

    return {
        "total": len(search_events) + len(memory_events),
        "search": {"count": len(search_events), "hit_total": hit_total,
                   "events": search_events},
        "memory": {"count": len(memory_events), "events": memory_events},
        "note": ("会话召回 = 宿主真的调了 session_search 工具（messages.tool_name），命中"
                 "内容取自该工具返回的 JSON；记忆召回注入 = 用户消息里真实存在的 "
                 "<memory-context> 注入块。两者都没有 ⇒ 显示「无记录」，不猜。"),
    }


def _model_window(home: Path, model: str, base_url: str) -> Tuple[Optional[int], str, Optional[str]]:
    """模型窗口 → ``(tokens, source, reason)``。只认宿主自己的 ``context_length_cache.yaml``。"""
    path = home / "context_length_cache.yaml"
    if not model:
        return None, "", "会话没有记录模型名（sessions.model 为空），无法对窗口"
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
    except FileNotFoundError:
        return None, str(path), "没有 %s（宿主还没写过这个缓存）" % path
    except Exception as exc:  # noqa: BLE001
        return None, str(path), "读不了 %s（%s: %s）" % (path, type(exc).__name__, exc)
    table = data.get("context_lengths") if isinstance(data, dict) else None
    if not isinstance(table, dict) or not table:
        return None, str(path), "%s 里没有 context_lengths 条目" % path
    if base_url:
        exact = table.get("%s@%s" % (model, base_url))
        if isinstance(exact, int) and exact > 0:
            return exact, str(path), None
    matches = [(key, value) for key, value in table.items()
               if isinstance(key, str) and key.split("@", 1)[0] == model
               and isinstance(value, int) and value > 0]
    if len(matches) == 1:
        return matches[0][1], str(path), None
    if len(matches) > 1:
        return None, str(path), ("缓存里同名模型有 %d 个 base_url，无法确定该用哪个"
                                 % len(matches))
    return None, str(path), "缓存里没有模型「%s」的窗口值" % model


def _conversation_payload(name: str, home: Path, session: str,
                          event_limit: int) -> Dict[str, Any]:
    """一个对话的详情（自己开只读连接；缺数据一律 ``null`` / 「无记录」，不补造）。"""
    db_path = home / "state.db"
    conn = _open_ro(db_path)
    if conn is None:
        raise ApiError(404, CODE_NOT_FOUND, "该 agent 没有可读的 state.db",
                       agent=name, path=str(db_path))
    try:
        meta = _sessions_table(conn).get(session)
        # 单会话聚合：与列表同一口径，避免两处数字不一致
        agg_rows = conn.execute(
            "select session_id, count(*), "
            "sum(case when active = 1 then 1 else 0 end), "
            "sum(case when active = 1 then length(coalesce(content, '')) else 0 end), "
            "sum(case when compacted = 1 then 1 else 0 end), "
            "sum(case when compacted = 1 then length(coalesce(content, '')) else 0 end), "
            "sum(case when " + SUMMARY_SQL_COND + " then 1 else 0 end), "
            "count(distinct case when " + SUMMARY_SQL_COND + " then content end), "
            "sum(case when tool_name = ? then 1 else 0 end), "
            "sum(case when instr(coalesce(content, ''), ?) > 0 then 1 else 0 end), "
            "sum(case when token_count is not null then 1 else 0 end), "
            "sum(case when token_count is not null then token_count else 0 end), "
            "min(timestamp), max(timestamp) "
            "from messages where session_id = ? group by session_id",
            (RECALL_TOOL, MEMORY_FENCE, session)).fetchone()
        if agg_rows is None:
            raise ApiError(404, CODE_NOT_FOUND,
                           "这个会话在 messages 里一行都没有（会话 id 打错了？）",
                           agent=name, session=session, db=str(db_path))
        agg = _aggregate_row(agg_rows)
        compressions = _compression_events(conn, session, event_limit)
        compressions["archived"] = {"rows": agg["rows_archived"], "chars": agg["chars_archived"]}
        recalls = _recall_events(conn, session, event_limit)
        billing = conn.execute(
            "select billing_base_url, model from session_model_usage where session_id = ? "
            "order by last_seen desc limit 1", (session,)).fetchone()
        base_url = str(billing[0] or "") if billing else ""
        model = str((meta or {}).get("model") or (billing[1] if billing else "") or "")
    finally:
        conn.close()

    window, window_source, window_reason = _model_window(home, model, base_url)
    tokens = agg["token_sum"] if agg["token_rows"] else None
    token_missing = agg["rows_total"] - agg["token_rows"]
    if tokens is None:
        token_reason = ("messages.token_count 这一列在本库全为 NULL（%d 行里有值的 0 行），"
                        "宿主只在内存里算 last_prompt_tokens、不落库 ⇒ 无记录"
                        % agg["rows_total"])
    else:
        token_reason = ("token 只在 %d/%d 行上有值，缺 %d 行 ⇒ 合计不完整，按无记录处理"
                        % (agg["token_rows"], agg["rows_total"], token_missing))
        tokens = None
    ratio = None
    ratio_reason = None
    if tokens is not None and window:
        ratio = round(100.0 * tokens / window, 2)
    elif window is None:
        ratio_reason = "模型窗口无记录 ⇒ 算不出占比"
    else:
        ratio_reason = "上下文 token 数无记录 ⇒ 算不出占比"

    archived_total = agg["chars_archived"] + agg["chars_active"]
    row = _conversation_row(session, meta or {}, agg)
    return {
        "ok": True,
        "agent": name,
        "profile": name,
        "session": session,
        "db": str(db_path),
        "session_row": bool(meta),
        "row": row,
        "context": {
            "rows_total": agg["rows_total"],
            "rows_active": agg["rows_active"],
            "chars_active": agg["chars_active"],
            "rows_archived": agg["rows_archived"],
            "chars_archived": agg["chars_archived"],
            "archived_share_pct": round(100.0 * agg["chars_archived"] / archived_total, 2)
            if archived_total else None,
            "tokens": tokens,
            "token_rows": agg["token_rows"],
            "token_missing_rows": token_missing,
            "token_reason": token_reason,
            "model": model,
            "base_url": base_url,
            "model_window": window,
            "model_window_source": window_source,
            "model_window_reason": window_reason,
            "ratio_pct": ratio,
            "ratio_reason": ratio_reason,
            "first_ts": _fmt_ts(agg["first_ts"]),
            "last_ts": _fmt_ts(agg["last_ts"]),
        },
        "compressions": compressions,
        "recalls": recalls,
        "data_sources": _data_sources(db_path, home),
        "caliber": ("本面板只显示库里真实存在的数据：字符数/条数是数出来的，token 数本机"
                    "未落库所以显示「无记录」，不用字符数折算 token、不估算占比。"),
    }


@router.get("/vcc/conversations")
def vcc_conversations(
    agent: str = Query(DEFAULT_AGENT, description="agent(=profile) id，读它自己的 state.db"),
    q: str = Query("", description="关键词：会话 id / 标题 / 模型，子串匹配（服务端过滤）"),
    filter: str = Query("all", description="all / compressed（有压缩）/ recalled（有召回）/ none（都没有）"),
    limit: int = Query(CONV_LIMIT_DEFAULT, ge=1, le=CONV_LIMIT_MAX, description="最多回多少个对话"),
    offset: int = Query(0, ge=0, description="跳过前多少个（按最后活动时间倒序）"),
    full: bool = Query(False, description="true 时每行带 sessions/聚合的全部字段（旧形态）"),
) -> Any:
    """该 agent 的**对话列表**（每个对话一行：条数 / 字符数 / 压缩几次 / 召回几次）。"""
    try:
        name = _agent_of(agent)
        home = _agent_home(name)
        if not home.is_dir():
            raise ApiError(404, CODE_NOT_FOUND, "未知 agent：%s" % name,
                           agents=[row["agent"] for row in _profiles()])
        agg = _session_aggregates(home)
        conn = _open_ro(home / "state.db")
        if conn is None:
            raise ApiError(404, CODE_NOT_FOUND, "该 agent 没有可读的 state.db",
                           agent=name, path=str(home / "state.db"))
        try:
            metas = _sessions_table(conn)
        finally:
            conn.close()
        rows: List[Dict[str, Any]] = []
        for session, values in agg["sessions"].items():
            rows.append(_conversation_row(session, metas.get(session) or {}, values))
        # sessions 表里有、messages 里一行为空的会话也要出现（否则「对话消失了」）
        for session, meta in metas.items():
            if session not in agg["sessions"]:
                rows.append(_conversation_row(session, meta, _aggregate_row(
                    (session, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0))))
        needle = (q or "").strip().lower()
        if needle:
            rows = [row for row in rows
                    if needle in str(row["session"]).lower()
                    or needle in str(row.get("label") or "").lower()
                    or needle in str(row.get("model") or "").lower()]
        wanted = (filter or "all").strip().lower()
        if wanted == "compressed":
            rows = [row for row in rows if row["has_compression"]]
        elif wanted == "recalled":
            rows = [row for row in rows if row["has_recall"]]
        elif wanted == "none":
            rows = [row for row in rows if not row["has_compression"] and not row["has_recall"]]
        rows.sort(key=lambda row: (-(row.get("last_ts") or 0),
                                   -(row.get("last_activity_at") or 0)))
        total = len(rows)
        page = rows[max(0, offset):max(0, offset) + max(1, limit)]
        if not full:
            page = [{key: row[key] for key in CONV_ROW_FIELDS if key in row} for row in page]
        return {
            "ok": True,
            "agent": name,
            "profile": name,
            "db": agg["db"],
            "q": q or "",
            "filter": wanted,
            "count": len(page),
            "total": total,
            "offset": offset,
            "limit": limit,
            "fields": None if full else list(CONV_ROW_FIELDS),
            "seconds": agg.get("seconds"),
            "reason": agg.get("reason"),
            "conversations": page,
            "caliber": ("一行 = 一个对话。条数/字符数是 messages 里数出来的；压缩次数 = 去重后的"
                        "摘要条目数；召回次数 = session_search 工具调用 + <memory-context> 注入块。"
                        "token 数本机未落库 ⇒ 显示无记录。"),
            "data_sources": _data_sources(home / "state.db", home),
        }
    except ApiError as exc:
        return _error_response(exc)


@router.get("/vcc/conversation")
def vcc_conversation(
    session: str = Query(..., description="会话 id（从 /vcc/conversations 里取）"),
    agent: str = Query(DEFAULT_AGENT, description="agent(=profile) id，state.db 按它定位"),
    event_limit: int = Query(EVENT_LIMIT_DEFAULT, ge=1, le=200,
                             description="压缩/召回各最多回多少条事件"),
) -> Any:
    """**一个对话**的上下文、压缩、召回明细（全部只读，缺数据就 ``null`` / 「无记录」）。"""
    try:
        name = _agent_of(agent)
        home = _agent_home(name)
        wanted = (session or "").strip()
        if not wanted:
            raise ApiError(400, CODE_INVALID_ARGUMENT, "session 为空：先选一个对话", agent=name)
        return _conversation_payload(name, home, wanted, int(event_limit))
    except ApiError as exc:
        return _error_response(exc)


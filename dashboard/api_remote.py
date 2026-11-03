# -*- coding: utf-8 -*-
"""hermes-console · 远端后端分区后端（V4 → V4b）：6 条路由，agent 列表**动态枚举**。

挂载契约：本文件**不单独挂载**。``plugin_api.py`` 把本目录加进 ``sys.path`` 后 import 本模块，
再 ``include_router(api_remote.router)``；最终 URL 是 ``/api/plugins/hermes-console/remote/...``。

## 六条路由（逐字，旧 ``plugins/hermes-remote/dashboard/plugin_api.py`` 的移植 + V4b 加的一条）

============================== ====== ==================================================
路由                            方法    说明
============================== ====== ==================================================
``/remote/agents``             GET    全部 agent 的对外后端状态（动态枚举，含账号名 + 凭据**摘要**）
``/remote/status``             GET    某 agent（或全部）的状态；每个 agent 都带状态字段
``/remote/agent/start``        POST   拉起某 agent 的对外后端（危险写操作）
``/remote/agent/stop``         POST   停掉某 agent 的对外后端（危险写操作）
``/remote/credentials``        GET    凭据配置状态（**脱敏**：只说已配置/未配置）
``/remote/credentials/reveal`` POST   **显式**解密一个字段（凭据或地址；默认永远不返回明文）
============================== ====== ==================================================

## 本卡修掉的那个真 bug（agent 列表自相矛盾）

旧插件的 ``plugin_api.py`` 里有两份互相打架的列表：``_PORT_ORDER``（``["default", "xiaomo",
"work", "worker"]``，模块常量，写死 4 个）与 ``list_agents()``（扫盘得 9 个）。
结果是"一键启停"只对写死的那 4 个里的少数几个真的可用，另外 5 个 agent 在列表里却点不动。

本文件里**一个 agent 名字都没有**（``default`` 除外，它是 home 根目录的固定语义）：
每次请求都走 ``core.scope.list_agents()`` 现扫 ``profiles/``，端口序号也按**这次扫到的顺序**
推。新增 profile ⇒ 下一次刷新就出现；删掉 profile ⇒ 下一次刷新就消失。

## 启停：复用旧脚本，不重写

真正 spawn / kill 进程的只有 ``plugins/hermes-remote/desktop/start_remote_backend.py``
（该文件的规则是用血换来的：凭据必须显式注入子进程环境、state 必须按 profile 分文件、
判活只能看端口因为真在监听的是启动器的子进程……）。本文件**只**以子进程方式调用它：

* ``start_remote_backend.py start <agent> <port>``
* ``start_remote_backend.py stop  <agent>``

**一行启动逻辑都不抄进来**，也不改那个脚本（卡片红线）。它缺失 ⇒ 503 结构化错误（不是 500）。

## 危险操作的护栏

``POST /remote/agent/start`` / ``/remote/agent/stop`` 是**能改生产进程**的操作，所以：

1. 先校验目标 agent **存在**（在 ``core.scope.list_agent_ids()`` 里）——
   不存在 ⇒ **400 ``invalid_argument``**（带合法 id 列表），**不是 500，也不 spawn 任何进程**；
2. 校验在**任何**子进程 / 文件操作之前完成（非法输入连一次 netstat 都不跑）；
3. 支持 ``dry_run=true``：只回"将会执行什么"（agent / 端口 / 脚本路径），一个字节都不动 ——
   前端二次确认条与探针都靠它，不必"先打一枪看看"；
4. 脚本输出必须是 JSON（脚本自己保证），解析不出 ⇒ 结构化 503（附 stderr 尾巴）。

## 凭据：默认只出"已配置/未配置"，明文必须**显式**要

旧 ``GET /credentials`` 会**回明文密码**给面板"点击显示"。V4 一度把用户名也一并打码，结果是
"看不到账号、没法一键复制"（比旧功能还难用）。V4b 又把账号名放进轮询响应，方便显示与复制 ——
但那样每次刷新都在响应体里带一份明文凭据。V4c 的最终口径是**一个明文出口**：

* ``GET /remote/agents`` / ``/remote/status`` 的每行只带**布尔与来源**（``username_configured`` /
  ``password_configured`` / ``token_configured`` / ``username_source`` / ``is_shared``），
  **任何凭据取值都不进轮询响应**；另外带 ``base_url`` / ``local_url`` / ``lan_urls`` /
  ``model`` / ``model_provider``（这些不是秘密，界面要显示、要一键复制）；
* V5 起每行再带一份 ``credential_fields``：账号 / 密码 / Token / 登录有效期 **+ 后端地址 /
  本机地址**的**元信息**（``configured`` / ``revealable`` / ``source`` / ``secret`` /
  ``hash_only``），界面靠它渲染「每字段一行、独立查看/复制」的凭据区，同样一个字符的取值都不含；
  ``secret`` 区分"机密字段（要二次确认）"与"地址字段（复制只是方便，不算机密）"；
* ``GET /remote/credentials`` 仍然全掩码（``***``），只回答"有没有配账号 / 有没有配密码 / 是不是
  继承自顶层 ``.env``"，供轮询用；
* 明文（账号名 / 密码 / 密码哈希 / Token / TTL）与地址（``base_url`` / ``local_url``）**只有**
  ``POST /remote/credentials/reveal`` 能给，且必须 ``confirm=true``（人点了"查看/复制"才会发这个
  请求）；响应带 ``Cache-Control: no-store``，**不进任何缓存**；
* 取值**不进日志**：审计只记 ``{agent, action: "reveal", field}``，见下方"事件台账"；
* 错误响应（4xx/5xx）在出站前统一过一遍红字（:func:`_redact_any`）：**结构上**不可能带出
  明文或密码哈希，无论调用方在 message / stderr 尾巴里塞了什么。

## 前端调用契约：凭据**按需**解密（唯一明文出口）

轮询响应（``/remote/agents`` / ``/remote/status`` / ``/remote/credentials``）里**一个字符的凭据
取值都没有**，只有 ``configured`` / ``revealable`` / ``source`` 这类元信息。明文只在人点
「查看 / 复制」那一刻取，且**一次只取一个字段**：

```http
POST /api/plugins/hermes-console/remote/credentials/reveal
Content-Type: application/json

{"agent": "xiaomo", "field": "password", "confirm": true}
```

请求参数（全部走**请求体**，不进 URL —— 不进访问日志、不进浏览器历史、不进 Referer）：

=========== ====== =========================================================
参数         必填   说明
=========== ====== =========================================================
``agent``   否     目标 agent id；缺省 = ``default``（home 根）。文法非法 ⇒ 400，
                   不存在 ⇒ 404（两者都在读凭据**之前**判定）
``field``   是     ``username`` / ``password`` / ``password_hash`` / ``secret``
                   （界面叫 Token）/ ``ttl_seconds`` / ``base_url`` / ``local_url``。
                   别名 ``token``→``secret``、``pass``→``password``、
                   ``user``→``username``、``url``/``address``→``base_url``、
                   ``local``→``local_url``（归一后**仍然**要过白名单，别名不是后门）
``confirm`` 是     **必须**是 JSON 布尔 ``true``；缺省 / 字符串 ``"true"`` / ``1``
                   一律 400（"没人点过按钮就没人拿到过明文"）
=========== ====== =========================================================

成功响应（200，头带 ``Cache-Control: no-store`` + ``Pragma: no-cache`` +
``X-Hermes-Exportable: false``）——**只带被请求的那一个字段**，别的凭据一个都不带；
**键集固定**（``scripts/probe_remote.py`` 用它守"只回本字段"，别往里加键）：

```json
{"ok": true, "agent": "xiaomo", "label": "小墨", "field": "password",
 "key": "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "configured": true,
 "value": "<明文>", "source": "profile", "length": 14,
 "note": "仅本响应含明文；不进缓存（Cache-Control: no-store）、不写日志、不写台账。"}
```

* ``value`` 是**唯一**的取值字段，其余键都是元信息；明文只放组件内存，别进 query 缓存 /
  localStorage / 任何导出（响应头 ``X-Hermes-Exportable: false`` 就是给导出器看的）；
* ``source`` —— ``profile``（本 agent 的 ``.env``）/ ``shared``（继承顶层 ``.env``）/
  ``none``；地址字段是 ``derived``；
* **哪个字段是机密**不在这条响应里说，而在轮询响应里：``GET /remote/agents`` 的
  ``reveal_public_fields``（= ``["base_url", "local_url"]``，其余都算机密，建议二次确认）
  与每行的 ``credential_fields[].secret``。

失败响应（统一 ``{"ok": false, "error": {code, message, ...}}``，**不含任何取值**）：

========================== ====== ==========================================
HTTP                        code   场景
========================== ====== ==========================================
400 ``invalid_argument``           缺 ``confirm`` / 字段不在白名单 / agent 文法
                                   非法（附 ``allowed_fields`` 或 ``agents``）
404 ``not_found``                  agent 文法合法但不存在（附 ``agents``）
409 ``not_configured``             该字段没配（附 ``field`` / ``agent``）
409 ``not_revealable``             配了但解不出明文（只配了 ``PASSWORD_HASH``
                                   的密码 —— 宁可说清楚，也不回空串让界面
                                   "复制成功"）
503 ``upstream_error``             ``dashboard/core`` 不可用
========================== ====== ==========================================

**鉴权复用**：本接口没有任何自己的鉴权实现，也不接受 URL 里的 token / GET 方式调用 ——
它和其它路由一样挂在 dashboard 上，由宿主既有的会话 token / OAuth 门
（``hermes_cli/web_server.py::auth_middleware``，非公开路径一律要有效会话）保护。
凭据来源仍是各 agent 的 ``.env``，**本分区不提供凭据写入**。

## 事件台账：最近启动 / 最近停止 / 最近错误从哪来

``<home>/plugins/hermes-console/data/remote-events.jsonl``（本插件自己的数据目录，append-only）：

* 只有**真实**启停（以及 reveal）才写一行，``dry_run`` 与非法输入一行都不写；
* ``_append_event`` 的额外字段是**白名单**（``port`` / ``error`` / ``already`` / ``field`` /
  ``source``），凭据取值即使被误传也写不进去；
* ``GET /remote/agents`` 的 ``last_started_at`` / ``last_stopped_at`` / ``last_error`` 就从这里读；
  ``last_started_at`` 还会回落到 state 文件里的 ``started_at``（覆盖"不是本接口拉起的"情况）。

## 错误语义

所有 4xx/5xx 统一 ``{"ok": false, "error": {"code": ..., "message": ..., ...}}``；
成功体是裸 dict + ``"ok": true``（与 ``api_vcc`` / ``api_upgrade`` 同风格）。
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

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
#: 旧脚本约定的对外绑定地址与起始端口（只用于**推算**，不落盘）。
HOST = "0.0.0.0"
BASE_PORT = 9119
#: 旧插件的目录名与它的启动器（唯一真正 spawn/kill 进程的地方，本文件只调用它）。
REMOTE_PLUGIN_DIRNAME = "hermes-remote"
START_SCRIPT_RELPATH = ("desktop", "start_remote_backend.py")
#: 每个 agent 一份 state 文件（原因见旧脚本注释：共用一个文件会 stop 杀错进程）。
STATE_FILE_TEMPLATE = "serve-%s.json"
PORTS_FILE_NAME = "ports.json"
#: 内置 dashboard_auth/basic 读的键前缀（它只认环境变量，不解析 .env）。
AUTH_PREFIX = "HERMES_DASHBOARD_BASIC_AUTH_"
#: 会被注入子进程的鉴权键（顺序即展示顺序）。
AUTH_KEYS = (
    AUTH_PREFIX + "USERNAME",
    AUTH_PREFIX + "PASSWORD",
    AUTH_PREFIX + "PASSWORD_HASH",
    AUTH_PREFIX + "SECRET",
    AUTH_PREFIX + "TTL_SECONDS",
)
#: 本机探活超时（秒）：只探**已经在监听**的端口，不让列表接口被卡住。
PROBE_TIMEOUT = 3.0
#: 探活结果缓存（秒）：面板自己也在 9119 上，自打 /api/status 一次 ~150–400ms。
#: 轮询型读数允许 5 秒的旧值；启停动作后的回读走 ``_probe(..., fresh=True)``。
PROBE_CACHE_TTL = 5.0
_PROBE_CACHE: Dict[int, Any] = {}
#: 调旧脚本的超时（秒）：旧插件用的 120s，保持一致。
SCRIPT_TIMEOUT = 120
#: netstat 超时（秒）。
NETSTAT_TIMEOUT = 15
#: LAN 地址缓存（powershell 调用较慢 —— 实测 636ms，且变动极少）。60s 太短：
#: 每过一分钟就有一个请求要白等 0.6s。5 分钟足够反映网卡变化。
LAN_CACHE_TTL = 300.0
#: 本插件 id（= 自己数据目录的目录名；与 ``core.settings.PLUGIN_ID`` 同值）。
CONSOLE_PLUGIN_ID = "hermes-console"
#: 事件台账（append-only，一行一个 JSON）：最近启动/停止时间、最近错误都从这里读。
EVENTS_FILENAME = "remote-events.jsonl"
#: 读台账时最多回看多少行（台账只增不减，别让一个老 home 拖慢列表接口）。
EVENTS_TAIL_LINES = 400
#: 台账**允许**出现的额外字段（白名单）：凭据取值即使被误传也写不进去。
EVENT_EXTRA_KEYS = frozenset({"port", "error", "already", "field", "source"})
#: ``error`` 字段的截断长度（启动器 stderr 可能很长）。
EVENT_ERROR_MAX = 300
#: ``POST /remote/credentials/reveal`` 允许解密的**凭据**字段（= :data:`AUTH_KEYS` 的短名）。
REVEAL_ENV_FIELDS = ("username", "password", "password_hash", "secret", "ttl_seconds")
#: 派生字段：不是 ``.env`` 里的键，由端口 + 网卡地址推算（见 :func:`_endpoints_of`）。
#: 前端「一键复制后端地址」与「复制密码」共用同一条按需出口，所以它们也在白名单里 ——
#: 只是标记 ``secret=false``（地址不是机密），同样**只在你点的那一刻**返回。
REVEAL_DERIVED_FIELDS = ("base_url", "local_url")
#: 白名单 = 凭据字段 + 派生字段。**只有**这里的字段能通过 reveal 拿到值。
REVEAL_FIELDS = REVEAL_ENV_FIELDS + REVEAL_DERIVED_FIELDS
#: ``secret=false`` 的字段（轮询响应里本来就有它们，复制只是图方便）。
REVEAL_PUBLIC_FIELDS = frozenset(REVEAL_DERIVED_FIELDS)
#: ``reveal`` 的字段别名：老板/界面嘴里的「Token」就是 ``HERMES_DASHBOARD_BASIC_AUTH_SECRET``。
#: 别名只做归一，不改白名单 —— 归一后仍然要过 :data:`REVEAL_FIELDS` 这一关。
REVEAL_ALIASES = {
    "token": "secret",
    "pass": "password",
    "user": "username",
    "url": "base_url",
    "address": "base_url",
    "local": "local_url",
}
#: 前端要调的按需解密端点（写进响应体，前端不必自己拼路径）。
REVEAL_ENDPOINT = "/remote/credentials/reveal"

# ---- 错误码（前端按字面量分支，别改） ------------------------------------
CODE_INVALID_ARGUMENT = "invalid_argument"
CODE_NOT_FOUND = "not_found"
CODE_UPSTREAM_ERROR = "upstream_error"
CODE_START_FAILED = "start_failed"
CODE_STOP_FAILED = "stop_failed"
#: reveal 专用：字段压根没配 / 配了但解不出明文（都不是 400 —— 请求本身没错）。
CODE_NOT_CONFIGURED = "not_configured"
CODE_NOT_REVEALABLE = "not_revealable"

_LAN_CACHE: Dict[str, Any] = {"at": 0.0, "value": []}


class ApiError(Exception):
    """带 HTTP 语义的业务错误 → 统一失败体。"""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra: Dict[str, Any] = dict(extra)


# ---------------------------------------------------------------------------
# 错误体红字：**任何**返回给前端的错误文本，出站前都先过这里
#
# 为什么不是"调用方记得别把值拼进 message"：这是**结构性**保证，与本文件
# `_append_event` 的字段白名单同一个思路。错误路径是最容易顺手把上游原话吐出去的地方
# （启动器 stderr、子进程 stdout、第三方异常消息），而那里完全可能带着凭据。
# 这里把当前 home 里**已知的**凭据取值（含密码哈希）从任何字符串里换成 ``***``。
# ---------------------------------------------------------------------------
def _secret_values(agent: Optional[str] = None) -> List[str]:
    """当前 home 里已知的凭据取值（只收长度 ≥ 4 的；太短的值替换会误伤正常文本）。

    **永不抛**：枚举 / 读盘失败只是"没有可抹的值"，不许让错误响应二次失败。
    """
    try:
        agents = [agent] if agent else _agent_ids()
    except Exception:  # noqa: BLE001
        return []
    values: List[str] = []
    for item in agents:
        try:
            env = _merged_env(str(item))
        except Exception:  # noqa: BLE001
            continue
        for key in AUTH_KEYS:
            value = (env.get(key) or "").strip()
            if len(value) >= 4 and value not in values:
                values.append(value)
    # 长的先替换：否则短值先把长值切碎，长值就再也匹配不上了
    values.sort(key=len, reverse=True)
    return values


def _redact_text(text: str, values: Optional[List[str]] = None) -> str:
    """把一个字符串里的已知凭据取值抹成 ``***``（**永不抛**）。"""
    if not text:
        return text
    for value in (_secret_values() if values is None else values):
        if value in text:
            text = text.replace(value, "***")
    return text


#: 错误体里**不参与**红字的键：它们的取值按构造就是 id / 枚举 / 布尔，
#: 不可能带凭据；抹它们只会把诊断信息弄坏（例如密码恰好叫 "work" 时把 agent id 抹掉）。
REDACT_SKIP_KEYS = frozenset({
    "code", "field", "agent", "agents", "allowed_fields", "source", "hash_only", "action", "ok",
})


def _redact_any(node: Any, values: Optional[List[str]] = None) -> Any:
    """递归抹 dict / list / str（错误体里的每个文本字段都要过）。"""
    if isinstance(node, str):
        return _redact_text(node, values)
    if isinstance(node, dict):
        return {
            key: (value if key in REDACT_SKIP_KEYS else _redact_any(value, values))
            for key, value in node.items()
        }
    if isinstance(node, (list, tuple)):
        return [_redact_any(value, values) for value in node]
    return node


def _fail(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    """统一失败体（与 ``api_vcc`` / ``api_upgrade`` 同款）。

    ★ 所有文本先过 :func:`_redact_any`：错误响应**结构上**不可能带出凭据取值
    （明文或密码哈希），无论调用方在 ``message`` / ``stderr`` 尾巴里塞了什么。
    红字本身失败（例如 core 坏了枚举不出 agent）时宁可把详情整段丢掉，也不冒险原样返回。
    """
    error: Dict[str, Any] = {"code": code, "message": message}
    error.update({key: value for key, value in extra.items() if key not in ("code", "message")})
    try:
        error = _redact_any(error, _secret_values())
    except Exception:  # noqa: BLE001 - 红字失败不许让错误响应变成 500
        error = {"code": code, "message": "请求未完成（错误详情已省略）"}
    return JSONResponse(status_code=status, content={"ok": False, "error": error})


# ---------------------------------------------------------------------------
# core 模块：惰性 import（core 坏掉只让本分区 503，不在挂载时拖垮整个插件）
# ---------------------------------------------------------------------------
def _core(submodule: str) -> Any:
    """import ``core.<submodule>``，带"同名别家模块"防护（口径同 ``api_vcc._core``）。"""
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
    不读 ``HERMES_HOME`` 环境变量（worker/gateway 里它是 ``<home>/profiles/<p>`` 语义）。
    """
    return Path(_core("scope").hermes_home(None))


# ---------------------------------------------------------------------------
# agent 枚举（★ 本卡的核心：动态，不写死）
# ---------------------------------------------------------------------------
def _agent_entries() -> List[Dict[str, Any]]:
    """``core.scope.list_agents()`` 的原始结果（每项含 id / label / home / config_path）。"""
    return list(_core("scope").list_agents(home=_home()))


def _agent_ids() -> List[str]:
    """当前真实存在的 agent id（动态扫盘，顺序即端口推算顺序）。"""
    return [str(entry["id"]) for entry in _agent_entries()]


def _labels() -> Dict[str, str]:
    """id → 人可读名字（``profile.yaml`` 的 title，读不到就是 id）。"""
    return {str(entry["id"]): str(entry.get("label") or entry["id"]) for entry in _agent_entries()}


def _require_agent(raw: Optional[str], *, write: bool) -> str:
    """把外部输入归一成**真实存在**的 agent id。

    * 文法非法（``../../etc`` 之类）⇒ ``scope.normalize_agent_id`` 抛 ``ValueError`` ⇒ 400；
    * 文法合法但**不存在**（``nope-not-an-agent``）⇒
      ``write=True``（启停这类危险操作）时为 **400 invalid_argument**，
      ``write=False``（只读查询）时为 **404 not_found**；
    * 两种失败体都带 ``agents``（当前合法 id 列表），前端可直接展示。

    **本函数在读取/写入任何资源之前完成校验** —— 启停路由先调它，非法输入连一次 netstat 都不跑。
    """
    scope = _core("scope")
    try:
        agent = str(scope.normalize_agent_id(raw))
    except ValueError as exc:
        raise ApiError(400, CODE_INVALID_ARGUMENT, str(exc),
                       agents=scope.list_agent_ids(home=_home())) from exc
    known = _agent_ids()
    if agent not in known:
        message = ("agent %r 不存在（当前不存在这个 profile 目录）" % agent)
        if write:
            raise ApiError(400, CODE_INVALID_ARGUMENT, message, agents=known, agent=agent)
        raise ApiError(404, CODE_NOT_FOUND, message, agents=known, agent=agent)
    return agent


# ---------------------------------------------------------------------------
# 端口 / state（只读；与旧插件共用同一份 ports.json，但本文件**不写**它）
# ---------------------------------------------------------------------------
def _remote_plugin_dir() -> Path:
    return _home() / "plugins" / REMOTE_PLUGIN_DIRNAME


def _ports_file() -> Path:
    return _remote_plugin_dir() / PORTS_FILE_NAME


def _read_ports() -> Dict[str, int]:
    """读旧插件登记的端口表；坏文件 / 缺文件一律 ``{}``（不抛）。"""
    try:
        data = json.loads(_ports_file().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 读端口表失败不该让整个接口挂
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, int] = {}
    for key, value in data.items():
        text = str(value).strip()
        if text.isdigit():
            out[str(key)] = int(text)
    return out


def _port_map() -> Dict[str, int]:
    """给**当前存在的每个** agent 推算一个端口（纯读，不落盘）。

    已登记的沿用登记值；未登记的按"本次扫到的顺序"从 :data:`BASE_PORT` 往上排，
    跳过已被占用的号码 —— 这里没有任何写死 agent 名字的顺序表。
    """
    registered = _read_ports()
    used = set(registered.values())
    out: Dict[str, int] = {}
    for index, agent in enumerate(_agent_ids()):
        if agent in registered:
            out[agent] = registered[agent]
            continue
        candidate = BASE_PORT + index
        while candidate in used:
            candidate += 1
        used.add(candidate)
        out[agent] = candidate
    return out


def _state_file(agent: str) -> Path:
    return _remote_plugin_dir() / (STATE_FILE_TEMPLATE % agent)


def _read_state(agent: str) -> Dict[str, Any]:
    """读该 agent 的 state（旧脚本启动时写的 ``serve-<agent>.json``）；坏/缺 ⇒ ``{}``。"""
    try:
        data = json.loads(_state_file(agent).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# 事件台账（append-only）：最近启动 / 最近停止 / 最近错误
#
# 为什么需要它：旧启动器只在 state 文件里记 ``started_at``，stop 时把 state 清成 ``{}``
# —— 于是"上一次什么时候停的""上一次为什么起不来"这两个问题在磁盘上根本没有答案。
# 台账补的正是这两个洞。它**只记事实，不记凭据**：额外字段走白名单，
# 凭据取值即使被误传也写不进去（见 :data:`EVENT_EXTRA_KEYS`）。
# ---------------------------------------------------------------------------
def _data_dir() -> Path:
    """本插件自己的数据目录 ``<home>/plugins/hermes-console/data``（与 ``api_models`` 同口径）。"""
    return _home() / "plugins" / CONSOLE_PLUGIN_ID / "data"


def _events_path() -> Path:
    """台账文件路径。**探针 monkeypatch 这一个函数**就能把写入/读取指到临时文件。"""
    return _data_dir() / EVENTS_FILENAME


def _append_event(agent: str, action: str, ok: bool, **fields: Any) -> None:
    """往台账追加一行。**永不抛**（台账写不进去不许让启停失败），也永不写凭据取值。

    ``fields`` 只接受 :data:`EVENT_EXTRA_KEYS` 里的键；不在白名单里的一律丢弃 ——
    这是"值不可能进日志"的结构性保证，而不是"调用方记得别传"。
    """
    record: Dict[str, Any] = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "agent": agent,
        "action": action,
        "ok": bool(ok),
    }
    for key, value in fields.items():
        if key not in EVENT_EXTRA_KEYS:
            continue
        if value is None or value == "":
            continue
        if key == "error":
            text = str(value)
            record[key] = text[:EVENT_ERROR_MAX]
            continue
        record[key] = value if isinstance(value, (bool, int, float)) else str(value)
    try:
        path = _events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - 台账是辅助信息，坏掉只少一行历史
        logger.debug("hermes-console.api_remote: 事件台账写入失败（已忽略）")


def _read_events(limit: int = EVENTS_TAIL_LINES) -> List[Dict[str, Any]]:
    """读台账尾部 ``limit`` 行（坏行跳过，缺文件 ⇒ 空列表，绝不抛）。"""
    try:
        lines = _events_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001 - 没有台账就是"还没有历史"
        return []
    out: List[Dict[str, Any]] = []
    for line in lines[-limit:]:
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            record = json.loads(text)
        except Exception:  # noqa: BLE001 - 半截行（进程被杀）跳过
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def _event_index() -> Dict[str, Dict[str, Any]]:
    """按 agent 汇总台账：``last_started_at`` / ``last_stopped_at`` / ``last_error``。

    * ``already=true`` 的那次不算"启动"（那是"本来就在跑"，写进时间线会骗人）；
    * ``last_error`` 取**最后一条失败**（含失败时间、动作、原因）。
    """
    index: Dict[str, Dict[str, Any]] = {}
    for record in _read_events():
        agent = str(record.get("agent") or "")
        if not agent:
            continue
        slot = index.setdefault(agent, {})
        action = str(record.get("action") or "")
        at = str(record.get("at") or "")
        ok = bool(record.get("ok"))
        already = bool(record.get("already"))
        if ok and not already and at:
            if action == "start":
                slot["last_started_at"] = at
            elif action == "stop":
                slot["last_stopped_at"] = at
        elif not ok:
            slot["last_error"] = {
                "at": at,
                "action": action,
                "message": str(record.get("error") or "未说明原因"),
            }
    return index


# ---------------------------------------------------------------------------
# 进程 / 端口探测（只读）
# ---------------------------------------------------------------------------
def _listeners() -> List[Any]:
    """``netstat -ano -p TCP`` → ``[(port, pid)]``（只看 LISTENING 行）。"""
    try:
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, errors="replace", timeout=NETSTAT_TIMEOUT,
        )
    except Exception:  # noqa: BLE001 - 没有 netstat 的平台按"什么都没在听"处理
        logger.debug("hermes-console.api_remote: netstat 不可用")
        return []
    rows: List[Any] = []
    for line in (completed.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3].upper() != "LISTENING":
            continue
        try:
            rows.append((int(parts[1].rsplit(":", 1)[-1]), int(parts[4])))
        except (ValueError, IndexError):
            continue
    return rows


def _pids_for_port(port: Optional[int], listeners: Optional[List[Any]] = None) -> List[int]:
    if not port:
        return []
    rows = _listeners() if listeners is None else listeners
    found: List[int] = []
    for listener_port, pid in rows:
        if listener_port == int(port) and pid not in found:
            found.append(pid)
    return found


def _probe(port: Optional[int], fresh: bool = False) -> Optional[Dict[str, Any]]:
    """本机 127.0.0.1 打公开的 ``/api/status``，确认"真能连上"（不是看端口就算）。

    这是 /remote/agents 剩下的大头：面板自己也跑在同一台机器上，自打 ``/api/status``
    实测 ~150–400ms（它内部还要做健康探测），9 个 agent 轮询一次就全白等。所以探活结果
    缓存 ``PROBE_CACHE_TTL`` 秒：轮询是"看状态"，5 秒的旧读数不算撒谎；**启停动作后的
    回读走 ``fresh=True``**，不受缓存影响。
    """
    if not port:
        return None
    key = int(port)
    now = time.time()
    if not fresh:
        hit = _PROBE_CACHE.get(key)
        if hit and now - hit[0] < PROBE_CACHE_TTL:
            return dict(hit[1])
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/status" % key, timeout=PROBE_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
            payload = json.loads(body) if body.strip() else {}
            result = {
                "ok": True,
                "status": resp.status,
                "auth_required": bool(payload.get("auth_required")),
                "auth_providers": payload.get("auth_providers") or [],
            }
    except urllib.error.HTTPError as exc:
        # 401/403 也是"端口真在服务"，只是要登录
        result = {"ok": True, "status": exc.code, "auth_required": True, "auth_providers": []}
    except Exception as exc:  # noqa: BLE001 - 探活失败只是状态字段，不许变成 500
        result = {"ok": False, "status": None, "error": str(exc),
                  "auth_required": False, "auth_providers": []}
    _PROBE_CACHE[key] = (time.time(), result)
    return dict(result)


def _lan_addresses() -> List[str]:
    """非回环、非虚拟网卡的 IPv4（"复制连接地址"用）；带 60s 缓存。"""
    now = time.time()
    cached = _LAN_CACHE.get("value") or []
    if cached and now - float(_LAN_CACHE.get("at") or 0) < LAN_CACHE_TTL:
        return list(cached)
    skip_prefixes = ("127.", "169.254.", "198.18.", "198.19.")
    skip_aliases = ("vethernet", "loopback", "flclash", "docker", "hyper-v")
    found: List[str] = []
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetIPAddress -AddressFamily IPv4 | "
             "Select-Object -Property IPAddress,InterfaceAlias | ConvertTo-Json -Compress"],
            capture_output=True, text=True, errors="replace", timeout=15,
        )
        rows = json.loads((completed.stdout or "").strip() or "[]")
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            ip = str(row.get("IPAddress") or "").strip()
            alias = str(row.get("InterfaceAlias") or "").strip().lower()
            if not ip or ip.startswith(skip_prefixes):
                continue
            if any(name in alias for name in skip_aliases):
                continue
            if ip not in found:
                found.append(ip)
    except Exception:  # noqa: BLE001 - 取不到网卡地址只是"没有建议地址"
        found = []
    _LAN_CACHE["at"] = now
    _LAN_CACHE["value"] = list(found)
    return found


# ---------------------------------------------------------------------------
# 凭据：只出"已配置/未配置"（★ 脱敏）
# ---------------------------------------------------------------------------
def _read_dotenv(path: Path) -> Dict[str, str]:
    """极简 ``.env`` 解析（KEY=VALUE，忽略注释/空行，去掉成对引号）。**只在内存里用。**"""
    out: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 没有 .env 就是"没配"
        return out
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def _merged_env(agent: str) -> Dict[str, str]:
    """顶层 ``.env`` 打底、该 agent 的 ``.env`` 覆盖 —— 与 Hermes 的 profile 优先级一致。"""
    home = _home()
    merged: Dict[str, str] = {}
    merged.update(_read_dotenv(home / ".env"))
    if agent != DEFAULT_AGENT:
        merged.update(_read_dotenv(home / "profiles" / agent / ".env"))
    return merged


def _credential_summary(agent: str) -> Dict[str, Any]:
    """凭据**摘要**（进轮询响应）：只有布尔与"已配置/未配置"，没有任何取值。"""
    env = _merged_env(agent)
    own = _read_dotenv(_home() / "profiles" / agent / ".env") if agent != DEFAULT_AGENT else {}
    has_user = bool((env.get(AUTH_PREFIX + "USERNAME") or "").strip())
    has_password = bool((env.get(AUTH_PREFIX + "PASSWORD") or "").strip())
    has_hash = bool((env.get(AUTH_PREFIX + "PASSWORD_HASH") or "").strip())
    has_token = bool((env.get(AUTH_PREFIX + "SECRET") or "").strip())
    has_ttl = bool((env.get(AUTH_PREFIX + "TTL_SECONDS") or "").strip())
    own_user = bool((own.get(AUTH_PREFIX + "USERNAME") or "").strip())
    configured = bool(has_user and (has_password or has_hash))
    return {
        "configured": configured,
        "status": "已配置" if configured else "未配置",
        "username_configured": has_user,
        # 密码可以只配哈希（basic auth 的 PASSWORD_HASH）；界面把两者合成「密码」一栏
        "password_configured": bool(has_password or has_hash),
        "password_plain_configured": has_password,
        "password_hash_configured": has_hash,
        "token_configured": has_token,
        "ttl_configured": has_ttl,
        # 账号只配在顶层 .env ⇒ 该 agent 用的是共享凭据
        "is_shared": not own_user,
    }


def _username_of(agent: str) -> Dict[str, Any]:
    """该 agent 账号名的**元信息**（是否配了、来自哪一层）。**值本身不在这里出。**

    V4c 起账号名与密码/Token 同口径：都是凭据，默认遮蔽；明文只有
    ``POST /remote/credentials/reveal``（``field=username``）这一条出口。
    轮询响应里一个字符的凭据都不带 —— 老板要的「看不到」是"点一下就能看到并复制"，
    不是"每次刷新都把账号名摆在响应体里"。
    """
    env = _merged_env(agent)
    own = _read_dotenv(_home() / "profiles" / agent / ".env") if agent != DEFAULT_AGENT else {}
    value = (env.get(AUTH_PREFIX + "USERNAME") or "").strip()
    if own.get(AUTH_PREFIX + "USERNAME", "").strip():
        source = "profile"
    elif value:
        source = "shared"
    else:
        source = "none"
    return {
        "username_configured": bool(value),
        "username_source": source,
    }


def _model_of(agent: str) -> Dict[str, Any]:
    """该 agent 实际会用的模型（``config.yaml`` 的 ``model.default`` / ``model.provider``）。

    合并口径与 Hermes 一致：**顶层打底、profile 覆盖** —— profile 没写 ``model``
    就回落到顶层 ``config.yaml``。读不到一律 ``None``（界面显示「—」），不猜、不填默认值。
    """
    scope = _core("scope")
    home = _home()
    layers: List[tuple] = []
    if agent != DEFAULT_AGENT:
        layers.append(("profile", scope.read_agent_config(agent, home=home)))
    layers.append(("global", scope.read_agent_config(DEFAULT_AGENT, home=home)))
    for source, config in layers:
        node = config.get("model") if isinstance(config, dict) else None
        if not isinstance(node, dict):
            continue
        name = node.get("default")
        name = name.strip() if isinstance(name, str) else ""
        if not name:
            continue
        provider = node.get("provider")
        provider = provider.strip() if isinstance(provider, str) else ""
        return {
            "model": name,
            "model_provider": provider or None,
            "model_source": source,
        }
    return {"model": None, "model_provider": None, "model_source": "none"}


def _endpoints_of(port: Optional[int], lan: Optional[List[str]] = None) -> Dict[str, Any]:
    """该 agent 后端对外可连的地址（**纯推算**，一个请求都不发）。

    * ``base_url``  —— 优先给局域网地址（别的机器/手机要连的就是它）；取不到网卡地址
      就回落到 ``http://127.0.0.1:<port>``，并如实标 ``base_url_source``；
    * ``local_url`` —— 本机访问地址（永远给）；
    * ``lan_urls``  —— 全部局域网候选（复制区可以列出来）。
    """
    if not port:
        return {"base_url": None, "local_url": None, "lan_urls": [], "base_url_source": "none"}
    addresses = list(lan if lan is not None else _lan_addresses())
    lan_urls = ["http://%s:%d" % (ip, int(port)) for ip in addresses]
    local_url = "http://127.0.0.1:%d" % int(port)
    return {
        "base_url": lan_urls[0] if lan_urls else local_url,
        "local_url": local_url,
        "lan_urls": lan_urls,
        "base_url_source": "lan" if lan_urls else "loopback",
    }


def _credential_fields(agent: str, port: Optional[int] = None) -> List[Dict[str, Any]]:
    """凭据**字段清单**（进轮询响应）：只有元信息，**一个字符的取值都没有**。

    界面靠这份清单渲染「地址 / 账号 / 密码 / Token」每字段一行（每行独立「查看 / 复制」），
    所以这里必须回答四个问题：有没有配（``configured``）、能不能解出明文
    （``revealable``；只配了 ``PASSWORD_HASH`` 的密码解不出明文）、值来自哪一层
    （``source`` = ``profile`` / ``shared`` / ``none`` / ``derived``）、要不要二次确认
    （``secret``：凭据为 ``true``，地址为 ``false``）。

    ★ 明文唯一出口仍然是 ``POST /remote/credentials/reveal``；本函数连长度都不给。
    """
    env = _merged_env(agent)
    own = _read_dotenv(_home() / "profiles" / agent / ".env") if agent != DEFAULT_AGENT else {}

    def value_of(key: str, scope: Optional[Dict[str, str]] = None) -> str:
        table = env if scope is None else scope
        return (table.get(key) or "").strip()

    def source_of(*keys: str) -> str:
        for key in keys:
            if value_of(key, own):
                return "profile"
        for key in keys:
            if value_of(key):
                return "shared"
        return "none"

    username_key = AUTH_PREFIX + "USERNAME"
    password_key = AUTH_PREFIX + "PASSWORD"
    hash_key = AUTH_PREFIX + "PASSWORD_HASH"
    secret_key = AUTH_PREFIX + "SECRET"
    ttl_key = AUTH_PREFIX + "TTL_SECONDS"

    has_username = bool(value_of(username_key))
    has_plain = bool(value_of(password_key))
    has_hash = bool(value_of(hash_key))
    has_secret = bool(value_of(secret_key))
    has_ttl = bool(value_of(ttl_key))
    has_port = bool(port)
    return [
        {
            "field": "username",
            "label": "账号",
            "key": username_key,
            "configured": has_username,
            "source": source_of(username_key),
            "revealable": has_username,
            "plain": has_username,
            "secret": True,
        },
        {
            "field": "password",
            "label": "密码",
            "key": password_key,
            # 只配哈希也算「配了密码」——但那种情况没有明文可给（reveal ⇒ 409 not_revealable）
            "configured": bool(has_plain or has_hash),
            "source": source_of(password_key, hash_key),
            "revealable": has_plain,
            "plain": has_plain,
            "secret": True,
            "hash_only": bool(has_hash and not has_plain),
        },
        {
            "field": "secret",
            "label": "Token",
            # 界面上叫「Token」，环境变量叫 SECRET；别名见 REVEAL_ALIASES
            "alias": "token",
            "key": secret_key,
            "configured": has_secret,
            "source": source_of(secret_key),
            "revealable": has_secret,
            "plain": has_secret,
            "secret": True,
        },
        {
            "field": "ttl_seconds",
            "label": "登录有效期",
            "key": ttl_key,
            "configured": has_ttl,
            "source": source_of(ttl_key),
            "revealable": has_ttl,
            "plain": has_ttl,
            "secret": True,
        },
        # 地址不是机密（轮询响应里本来就有），但同样列在这里：前端一套「查看 / 复制」逻辑
        # 就能覆盖地址，且复制走的仍是同一条按需出口（不新开明文路径）。
        {
            "field": "base_url",
            "label": "后端地址",
            "key": "base_url",
            "configured": has_port,
            "source": "derived" if has_port else "none",
            "revealable": has_port,
            "plain": has_port,
            "secret": False,
        },
        {
            "field": "local_url",
            "label": "本机地址",
            "key": "local_url",
            "configured": has_port,
            "source": "derived" if has_port else "none",
            "revealable": has_port,
            "plain": has_port,
            "secret": False,
        },
    ]


def _reveal_value(agent: str, field: str) -> Dict[str, Any]:
    """解一个字段的取值（**唯一的明文出口**）。调用方必须先做完 confirm/白名单校验。

    两类字段：

    * **凭据字段**（``.env`` 里的 ``HERMES_DASHBOARD_BASIC_AUTH_*``）—— 机密，界面宜二次确认；
    * **派生字段**（``base_url`` / ``local_url``，由端口 + 网卡地址推算）—— 不是机密，
      但与凭据共用同一条出口（前端不必为"复制地址"另写一套逻辑）。

    返回体**只放本字段的取值**；不附带任何别的凭据。值不写日志、不写台账。
    **键集固定**（ok/agent/label/field/key/configured/value/source/length/note）——
    ``scripts/probe_remote.py`` 用"键集是这 10 个的子集"守"只回本字段"，所以
    "这个字段是不是机密"这类元信息放轮询响应（``credential_fields[].secret`` /
    ``reveal_public_fields``），**不许**往这条明文响应里加键。
    配了但解不出明文（只配了 ``PASSWORD_HASH``）⇒ 409 ``not_revealable``；
    压根没配 ⇒ 409 ``not_configured`` —— 两种失败体都不含任何取值，
    也**绝不**用空串假装成功（那会让界面"复制成功"一个空字符串）。
    """
    if field in REVEAL_DERIVED_FIELDS:
        endpoints = _endpoints_of(_port_map().get(agent), _lan_addresses())
        value = str(endpoints.get(field) or "")
        if not value:
            raise ApiError(409, CODE_NOT_CONFIGURED,
                           "agent %r 还没有对外后端端口，取不到 %s（先启动它的对外后端）"
                           % (agent, field),
                           agent=agent, field=field)
        return {
            "ok": True,
            "agent": agent,
            "label": _labels().get(agent, agent),
            "field": field,
            "key": field,
            "configured": True,
            "value": value,
            "source": "derived",
            "length": len(value),
            "note": "仅本响应含该地址；不进缓存（Cache-Control: no-store）、不写日志、不写台账。",
        }

    key = AUTH_PREFIX + field.upper()
    env = _merged_env(agent)
    value = (env.get(key) or "").strip()
    own = _read_dotenv(_home() / "profiles" / agent / ".env") if agent != DEFAULT_AGENT else {}
    if (own.get(key) or "").strip():
        source = "profile"
    elif value:
        source = "shared"
    else:
        source = "none"
    if not value:
        if field == "password" and (env.get(AUTH_PREFIX + "PASSWORD_HASH") or "").strip():
            raise ApiError(409, CODE_NOT_REVEALABLE,
                           "该 agent 的密码只配置了 PASSWORD_HASH（哈希），没有可显示的明文；"
                           "要看明文请把明文写进该 agent 的 .env",
                           agent=agent, field=field, hash_only=True)
        raise ApiError(409, CODE_NOT_CONFIGURED,
                       "该 agent 没有配置 %s（来源：%s）" % (field, source),
                       agent=agent, field=field, source=source)
    return {
        "ok": True,
        "agent": agent,
        "label": _labels().get(agent, agent),
        "field": field,
        "key": key,
        "configured": True,
        "value": value,
        "source": source,
        "length": len(value),
        "note": "仅本响应含明文；不进缓存（Cache-Control: no-store）、不写日志、不写台账。",
    }


def _credential_detail(agent: str) -> Dict[str, Any]:
    """``GET /remote/credentials`` 的单 agent 明细：**每个值都是掩码**。

    实现：把真实取值交给 ``core.settings.mask_sensitive``（它按键名
    ``token/key/secret/password/credential`` 打码），再对**所有**非空值统一改写成 ``"***"`` ——
    用户名不命中那些标记，但它同样是凭据，所以一律一起打码。
    环境变量的原值只在本函数栈里出现，**从不进入响应体**。
    """
    env = _merged_env(agent)
    raw = {key: (env.get(key) or "").strip() for key in AUTH_KEYS}
    present = {key: value for key, value in raw.items() if value}

    settings_core = _core("settings")
    # mask_sensitive 按键名打码（password / secret / …）。它**不认** USERNAME，
    # 而用户名同样是凭据 —— 所以下面把所有非空值统一改写成 "***"，一个原值都不留。
    masked_raw: Dict[str, Any] = dict(settings_core.mask_sensitive(present)) if present else {}
    masked_raw.pop("masked_keys", None)
    masked: Dict[str, Any] = {}
    for key, value in masked_raw.items():
        masked[key] = "***" if isinstance(value, str) and value.strip() else value

    keys: List[Dict[str, Any]] = []
    for key in AUTH_KEYS:
        value = raw.get(key) or ""
        # mask_sensitive 已把敏感键换成 "***"；其余（如 USERNAME）在这里同样打码。
        shown = "***" if value else ""
        keys.append({
            "key": key,
            "label": key[len(AUTH_PREFIX):].lower(),
            "configured": bool(value),
            "value": shown,
        })
    summary = _credential_summary(agent)
    return {
        "ok": True,
        "agent": agent,
        "label": _labels().get(agent, agent),
        # 复用 core.settings.mask_sensitive 的产物（键名语言与设置界面一致）
        "masked": masked,
        "masked_keys": sorted(masked.keys()),
        "keys": keys,
        # 界面真正需要的就是这两项
        "status": summary["status"],
        "configured": summary["configured"],
        "username_configured": summary["username_configured"],
        "password_configured": summary["password_configured"],
        "is_shared": summary["is_shared"],
        "note": "凭据一律脱敏显示：本响应不含任何明文用户名/密码/密钥，"
                "需要修改请到该 agent 的 .env（本分区不提供凭据写入）",
    }


# ---------------------------------------------------------------------------
# 状态（每个 agent 都有 state 字段）
# ---------------------------------------------------------------------------
def _agent_status(agent: str, ports: Dict[str, int], listeners: Optional[List[Any]],
                  events: Optional[Dict[str, Dict[str, Any]]] = None,
                  lan: Optional[List[str]] = None,
                  fresh_probe: bool = False) -> Dict[str, Any]:
    """单个 agent 的对外后端状态。**只读**：不写 ports.json、不写 state、不 spawn。"""
    port = int(ports.get(agent) or BASE_PORT)
    pids = _pids_for_port(port, listeners)
    running = bool(pids)
    state = _read_state(agent)
    started_at = state.get("started_at") or ""
    uptime: Optional[int] = None
    if running and started_at:
        try:
            started = time.mktime(time.strptime(str(started_at), "%Y-%m-%d %H:%M:%S"))
            uptime = max(0, int(time.time() - started))
        except Exception:  # noqa: BLE001 - 时间格式不对就是"算不出"
            uptime = None
    probe = _probe(port, fresh=fresh_probe) if running else None
    # 连接状态：在跑 ≠ 能连上（鉴权门坏 / 进程半死都会"在听但连不通"），所以单独一列。
    if not running:
        conn = "stopped"
    elif probe is None:
        conn = "unknown"
    elif probe.get("ok") and probe.get("auth_required"):
        conn = "auth_required"
    elif probe.get("ok"):
        conn = "connected"
    else:
        conn = "unreachable"
    history = (events if events is not None else _event_index()).get(agent) or {}
    return {
        "agent": agent,
        # 旧插件把同一个 id 叫 profile，前端两处都用同一份数据
        "profile": agent,
        "label": _labels().get(agent, agent),
        # ★ 状态字段（探针断言每个 agent 都有这几个键）
        "state": "running" if running else "stopped",
        "running": running,
        # 连接状态（running 之外的第二维）：connected / auth_required / unreachable / stopped / unknown
        "conn": conn,
        "pid": pids[0] if pids else None,
        "port": port,
        "host": HOST,
        "started_at": started_at or None,
        "uptime_seconds": uptime,
        "probe": probe,
        # ★ 时间线：最近启动 / 最近停止 / 最近错误（来自事件台账，见 _event_index）
        "last_started_at": history.get("last_started_at") or (started_at or None),
        "last_stopped_at": history.get("last_stopped_at"),
        "last_error": history.get("last_error"),
        "credentials": _credential_summary(agent),
        # ★ 凭据字段清单（地址 / 账号 / 密码 / Token / 有效期）：只有布尔与来源，没有任何取值
        "credential_fields": _credential_fields(agent, port),
        # ★ 一屏要看到的三样：后端地址 / 模型 / 凭据字段（值一律不在响应体里）
        **_endpoints_of(port, lan),
        **_model_of(agent),
        # ★ 账号名的元信息（明文只有 POST /remote/credentials/reveal 能给）
        **_username_of(agent),
    }


def _status_all(selected: Optional[str] = None,
                fresh_probe: bool = False) -> Dict[str, Any]:
    """全部（或单个）agent 的状态。netstat 只跑一次，台账只读一次，9 个 agent 共用结果。"""
    ports = _port_map()
    listeners = _listeners()
    events = _event_index()
    # 局域网地址也只用取一次（内部有 60s 缓存），9 行共用同一份 base_url 前缀
    lan = _lan_addresses()
    agents = _agent_ids()
    if selected is not None:
        agents = [selected]
    # 行与行互不依赖（各自读自己的 config/.env + 打自己的 /api/status）：串行时 9 行
    # 的耗时是相加的（实测 ~610ms，其中 _model_of 384ms、探活 161ms），并发后 ≈ 最慢
    # 的那一行。_port_map/_listeners/_event_index/_lan_addresses 已在外层取一次共用。
    workers = max(1, min(8, len(agents)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(
            lambda name: _agent_status(name, ports, listeners, events, lan, fresh_probe),
            agents))
    payload: Dict[str, Any] = {
        "ok": True,
        "count": len(rows),
        "agents": rows,
        "host": HOST,
        "script": str(_script_path()),
        "script_exists": _script_path().is_file(),
    }
    if selected is not None:
        payload["agent"] = selected
        payload.update({key: value for key, value in rows[0].items() if key not in ("agent", "profile")})
    return payload


# ---------------------------------------------------------------------------
# 启停：唯一真正 spawn/kill 的地方是旧脚本（本文件只调它）
# ---------------------------------------------------------------------------
def _script_path() -> Path:
    return _remote_plugin_dir().joinpath(*START_SCRIPT_RELPATH)


def _python_exe() -> str:
    """优先用 ``<home>/tools/<最新>/python.exe``（旧插件的口径），否则用当前解释器。"""
    tools = _home() / "tools"
    try:
        if tools.is_dir():
            for child in sorted(tools.iterdir(), reverse=True):
                exe = child / "python.exe"
                if exe.is_file():
                    return str(exe)
    except OSError:
        pass
    return sys.executable


def _run_script(*args: str) -> Dict[str, Any]:
    """调旧启动器（``status|start|stop``），把它最后一行 JSON 当结果。

    **这是本分区唯一的写操作出口。** 脚本缺失 ⇒ 显式 ``upstream_error``，绝不假装成功。
    """
    script = _script_path()
    if not script.is_file():
        raise ApiError(503, CODE_UPSTREAM_ERROR,
                       "远端后端启动器缺失：%s（旧插件 hermes-remote 被删或未安装？）" % script,
                       script=str(script))
    env = dict(os.environ)
    env.setdefault("HERMES_HOME", str(_home()))
    try:
        completed = subprocess.run(
            [_python_exe(), str(script), *args],
            capture_output=True, text=True, errors="replace", timeout=SCRIPT_TIMEOUT,
            env=env, cwd=str(_remote_plugin_dir()),
        )
    except subprocess.TimeoutExpired:
        raise ApiError(504, CODE_UPSTREAM_ERROR,
                       "%s 超时（%ds）" % (" ".join(args), SCRIPT_TIMEOUT))
    except OSError as exc:
        raise ApiError(503, CODE_UPSTREAM_ERROR,
                       "调不动启动器 %s：%s: %s" % (script, type(exc).__name__, exc))
    for line in reversed((completed.stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except Exception:  # noqa: BLE001 - 继续往前找
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ApiError(503, CODE_UPSTREAM_ERROR,
                   "启动器没有输出 JSON 结果（args=%s）" % " ".join(args),
                   stderr=(completed.stderr or "")[-500:],
                   stdout=(completed.stdout or "")[-500:])


def _body_agent(body: Optional[Dict[str, Any]]) -> str:
    """从请求体取 agent：``agent`` 优先，兼容旧插件的 ``profile``。"""
    payload = body or {}
    raw = payload.get("agent")
    if raw in (None, ""):
        raw = payload.get("profile")
    return "" if raw is None else str(raw)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.get("/remote/agents")
def list_remote_agents() -> Any:
    """全部 agent 的对外后端状态（★ 动态枚举：新增/删除 profile 立刻反映）。

    ``{"ok": true, "count": 9, "agents": [{agent, state, running, port, credentials, ...}],
    "lan_addresses": [...], "host": "0.0.0.0", "script": "<启动器路径>", "script_exists": true}``
    """
    try:
        payload = _status_all()
        payload["lan_addresses"] = _lan_addresses()
        # 明文出口的自描述：界面据此判断「哪些字段点得动」，不必在前端写死字段表
        payload["reveal_fields"] = list(REVEAL_FIELDS)
        payload["reveal_derived_fields"] = list(REVEAL_DERIVED_FIELDS)
        payload["reveal_public_fields"] = sorted(REVEAL_PUBLIC_FIELDS)
        payload["reveal_aliases"] = dict(REVEAL_ALIASES)
        payload["reveal_endpoint"] = REVEAL_ENDPOINT
        payload["reveal_confirm_required"] = True
        payload["agents_note"] = (
            "agent 列表由 core.scope.list_agents() 每次现扫 profiles/ 得到，本模块不写死任何 agent 名")
        payload["requested_by"] = "hermes-console.api_remote"
        return payload
    except ApiError as exc:
        return _fail(exc.status, exc.code, exc.message, **exc.extra)


@router.get("/remote/status")
def get_remote_status(agent: Optional[str] = Query(default=None)) -> Any:
    """某 agent（缺省 = 全部）的对外后端状态。

    * 每个 agent 都带 ``state`` / ``running`` / ``pid`` / ``port`` / ``started_at`` /
      ``uptime_seconds`` / ``credentials``；
    * 未知 agent ⇒ **404 not_found**（带合法 id 列表），非法 id ⇒ 400；
    * 只读：不写 ports.json / state，不 spawn 任何进程。
    """
    try:
        selected = None if agent in (None, "") else _require_agent(agent, write=False)
        return _status_all(selected)
    except ApiError as exc:
        return _fail(exc.status, exc.code, exc.message, **exc.extra)


@router.post("/remote/agent/start")
def post_agent_start(body: Optional[Dict[str, Any]] = Body(default=None)) -> Any:
    """拉起某 agent 的对外后端（**危险写操作**）。

    * 目标 agent 不存在 ⇒ **400 invalid_argument**（带合法 id 列表），**不 spawn 任何进程**；
    * ``dry_run=true`` ⇒ 只回"将会执行什么"，不动任何东西（前端二次确认条 / 探针用）；
    * 已在听同一端口 ⇒ ``already=true``，不重复拉；
    * 真正执行时**只**调用旧启动器 ``start_remote_backend.py start <agent> <port>``。
    """
    payload = body or {}
    raw_port = payload.get("port")
    dry_run = bool(payload.get("dry_run") is True)
    try:
        target = _body_agent(payload)
        # ★ 校验在第一行完成：非法/不存在的 agent 走不到任何子进程或文件写入
        chosen = _require_agent(target, write=True)
        port = int(raw_port) if raw_port not in (None, "") else _port_map()[chosen]
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "action": "start",
                "agent": chosen,
                "label": _labels().get(chosen, chosen),
                "port": port,
                "script": str(_script_path()),
                "argv": [str(_script_path()), "start", chosen, str(port)],
                "message": "预演：未执行任何操作（真要启动请去掉 dry_run）",
            }
        try:
            result = _run_script("start", chosen, str(port))
        except ApiError as exc:
            # 启动器缺失 / 超时：也要在台账里留一条"最近错误"，否则界面只看到"没反应"
            _append_event(chosen, "start", False, port=port, error=exc.message)
            raise
        ok = bool(result.get("ok"))
        _append_event(chosen, "start", ok, port=port,
                      already=True if result.get("already") else None,
                      error=None if ok else (result.get("error") or "启动器报告失败"))
        response = {
            "ok": ok,
            "action": "start",
            "agent": chosen,
            "label": _labels().get(chosen, chosen),
            "result": result,
            "status": _status_all(chosen, fresh_probe=True),
        }
        if not ok:
            response["error"] = {
                "code": CODE_START_FAILED,
                "message": str(result.get("error") or "启动器报告失败"),
                "detail": result,
            }
        return response
    except ApiError as exc:
        return _fail(exc.status, exc.code, exc.message, **exc.extra)
    except (TypeError, ValueError) as exc:
        return _fail(400, CODE_INVALID_ARGUMENT, "请求体不合法：%s: %s" % (type(exc).__name__, exc),
                     agents=_agent_ids())


@router.post("/remote/agent/stop")
def post_agent_stop(body: Optional[Dict[str, Any]] = Body(default=None)) -> Any:
    """停掉某 agent 的对外后端（**危险写操作**）。

    护栏与 ``/agent/start`` 一致：不存在 ⇒ 400（不 kill 任何进程）、``dry_run=true`` ⇒ 只预演。
    真正执行时**只**调用旧启动器 ``start_remote_backend.py stop <agent>``。
    """
    payload = body or {}
    dry_run = bool(payload.get("dry_run") is True)
    try:
        target = _body_agent(payload)
        chosen = _require_agent(target, write=True)
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "action": "stop",
                "agent": chosen,
                "label": _labels().get(chosen, chosen),
                "script": str(_script_path()),
                "argv": [str(_script_path()), "stop", chosen],
                "message": "预演：未执行任何操作（真要停止请去掉 dry_run）",
            }
        try:
            result = _run_script("stop", chosen)
        except ApiError as exc:
            _append_event(chosen, "stop", False, error=exc.message)
            raise
        ok = bool(result.get("ok"))
        _append_event(chosen, "stop", ok,
                      already=True if result.get("already") else None,
                      error=None if ok else (result.get("error") or "启动器报告失败"))
        response = {
            "ok": ok,
            "action": "stop",
            "agent": chosen,
            "label": _labels().get(chosen, chosen),
            "result": result,
            "status": _status_all(chosen, fresh_probe=True),
        }
        if not ok:
            response["error"] = {
                "code": CODE_STOP_FAILED,
                "message": str(result.get("error") or "启动器报告失败"),
                "detail": result,
            }
        return response
    except ApiError as exc:
        return _fail(exc.status, exc.code, exc.message, **exc.extra)
    except (TypeError, ValueError) as exc:
        return _fail(400, CODE_INVALID_ARGUMENT, "请求体不合法：%s: %s" % (type(exc).__name__, exc),
                     agents=_agent_ids())


@router.get("/remote/credentials")
def get_remote_credentials(agent: Optional[str] = Query(default=None)) -> Any:
    """凭据**配置状态**（脱敏）。

    单 agent（``?agent=xiaomo``）⇒ 该 agent 的明细；
    缺省 ⇒ 全部 agent 的摘要（``agents: [...]``，与 ``/remote/agents`` 同口径）。

    **响应里没有任何明文用户名/密码/密钥**：值一律 ``"***"``（走
    ``core.settings.mask_sensitive`` 后再统一改写），界面只需要"已配置/未配置"。
    """
    try:
        if agent not in (None, ""):
            return _credential_detail(_require_agent(agent, write=False))
        rows = [_credential_summary(item) for item in _agent_ids()]
        agents = _agent_ids()
        return {
            "ok": True,
            "count": len(agents),
            "agents": [{"agent": name, **summary} for name, summary in zip(agents, rows)],
            "note": "仅返回配置状态：任何凭据取值都不出现在本响应里",
        }
    except ApiError as exc:
        return _fail(exc.status, exc.code, exc.message, **exc.extra)


@router.post("/remote/credentials/reveal")
def post_credentials_reveal(body: Optional[Dict[str, Any]] = Body(default=None)) -> Any:
    """**显式**解一个凭据字段的明文（面板上「查看 / 复制密码」才会走到这里）。

    为什么必须是 POST + 显式 ``confirm``：

    * 明文不能出现在任何"顺手就会发"的请求里（轮询 / 预取 / 打开面板）—— 那正是上一版
      "看不到密码"的反面错误，也是更早那版"一进页面就明文渲染"的错误；
    * 字段放在请求体（不放 query），明文就不会落进访问日志 / 浏览器历史；
    * 响应带 ``Cache-Control: no-store``，不进任何中间缓存。

    护栏：

    * ``confirm`` 必须**是** ``true``（缺省 / 字符串 ``"true"`` / ``1`` 一律 400）——
      这条保证"没人点过按钮就没人拿到过明文"；
    * ``field`` 必须在 :data:`REVEAL_FIELDS` 白名单里（400 附合法字段表）；白名单 =
      凭据字段（username / password / password_hash / secret / ttl_seconds）+ 派生字段
      （base_url / local_url）。别名 ``token``/``url`` 只做归一，不是后门；
    * agent 不存在 ⇒ 404（只读语义），文法非法 ⇒ 400，两者都在读凭据之前判定；
    * 字段没配 ⇒ 409 ``not_configured``；配了但解不出明文（只配 ``PASSWORD_HASH``）
      ⇒ 409 ``not_revealable`` —— 宁可说清楚，也不回空串让界面"复制成功"；
    * 响应**只带被请求的那一个字段**，且带 ``Cache-Control: no-store``；
    * 台账只记 ``{agent, action: "reveal", field}``，**取值一个字节都不写**；
    * 鉴权不另起炉灶：与其它路由同处 dashboard 的会话 token / OAuth 门之后（见模块头）。
    """
    payload = body or {}
    raw_field = str(payload.get("field") or "").strip().lower()
    # 别名归一（token → secret、url → base_url）；归一后仍要过白名单，别名不是后门
    field = REVEAL_ALIASES.get(raw_field, raw_field)
    try:
        chosen = _require_agent(_body_agent(payload), write=False)
        if payload.get("confirm") is not True:
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "解密必须显式确认：请求体要带 confirm=true（本接口不给\"顺手就返回明文\"的机会）",
                           field=field, agent=chosen, allowed_fields=list(REVEAL_FIELDS))
        if field not in REVEAL_FIELDS:
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "field 必须是 %s 之一（界面上的 token 等价于 secret）"
                           % "/".join(REVEAL_FIELDS),
                           agent=chosen, field=field, allowed_fields=list(REVEAL_FIELDS))
        revealed = _reveal_value(chosen, field)
        # 审计：只记字段名，不记取值（_append_event 的额外字段是白名单，值写不进去）
        _append_event(chosen, "reveal", True, field=field, source=revealed.get("source"))
        return JSONResponse(
            content=revealed,
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                # 明文响应不是"可导出的数据"：任何导出 / CSV 生成器都该跳过它
                "X-Hermes-Exportable": "false",
            },
        )
    except ApiError as exc:
        # 失败不写台账：台账的 last_error 是"启动/停止失败"那一列，reveal 被拒
        # （没配 / 只配哈希）不是后端故障，写进去会让该 agent 的行凭空多一条"最近错误"。
        # 拒绝对日志同样是静默的：只有字段名与错误码，没有取值。
        logger.debug("hermes-console.api_remote: reveal 被拒 code=%s field=%s", exc.code, field)
        return _fail(exc.status, exc.code, exc.message, **exc.extra)

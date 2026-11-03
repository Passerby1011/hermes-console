# -*- coding: utf-8 -*-
"""hermes-console · 「模型」分区后端（V5）：13 条路由，**改真实 profile 的模型/provider 是写操作**。

挂载契约：本文件**不单独挂载**。``plugin_api.py`` 把本目录加进 ``sys.path`` 后 import 本模块，
再 ``include_router(api_models.router)``；最终 URL 是 ``/api/plugins/hermes-console/models/...``。

## 路由清单

========== ============================ ====== ==========================================
#          路由                          方法    说明
========== ============================ ====== ==========================================
1          ``/models/overview``          GET    当前模型概览（缺省列全部 agent，给 ``agent`` 只列那一个）
2          ``/models/health``            GET    轻量自检（配置/CLI/快照目录/预设文件）
3          ``/models/providers``         GET    某 agent 视角的 provider 目录（**凭据脱敏**）
4          ``/models/providers/{agent}`` GET    同上，agent 走路径（前端下拉用）
5          ``/models/models``            GET    某 provider 的模型列表（moa = 预设名）
6          ``/models/snapshots``         GET    已有快照列表（按时间戳升序）
7          ``/models/apply``             POST   ★把 provider/model 写进**一个** agent（改真实配置）
8          ``/models/rollback``          POST   ★从已有快照回滚（同样改真实配置）
9          ``/models/presets``           GET    预设列表（console 自己的数据文件）
10         ``/models/presets``           POST   新建预设（**不碰 profile 配置**）
11         ``/models/presets/{name}``    PUT    改预设（**不碰 profile 配置**）
12         ``/models/presets/{name}``    DELETE 删预设（**不碰 profile 配置**）
13         ``/models/model-library``     GET    模型库：每个 provider 的模型全表
14         ``/models/batch/preview``     POST   ★批量**预览**（纯读：将写哪些行、哪个字段）
15         ``/models/batch/apply``       POST   ★批量写入（逐 agent 快照 + 逐 agent 回读校验）
16         ``/models/batch/rollback``    POST   ★按账本一键回滚整批（逐 agent 字节还原）
17         ``/models/batches``           GET    批量账本列表（最近若干次，供一键回滚选）
========== ============================ ====== ==========================================

## 可写段（``scope``）与「只能设一个备用模型 / 其它槽位设不了」的由来

写操作一次只作用**一个段**（``SCOPES``），旧契约只有两段，所以界面上"备用模型"只能填
一条、辅助槽位根本没法设 —— 那是**契约限制，不是数据不支持**（``config.yaml`` 里
``fallback_providers`` 本来就可以有多条、``auxiliary`` 本来就有 8 个槽位）。现在五段：

============= ==========================================================================
``scope``     语义
============= ==========================================================================
``model``     主模型（``model.provider`` + ``model.default``）
``fallback``  备用链**首条**（旧语义，一字不动地保留，避免破坏既有调用方）
``fallbacks`` 备用链**有序整链**：任意条数、追加 / 删除 / 排序 / 清空
``auxiliary`` 辅助模型**单个槽位**（槽位名按该 agent 的 ``auxiliary:`` 段动态取）
``reasoning`` 主模型的**思考强度**：``agent.reasoning_effort`` 一个键
============= ==========================================================================

### ``scope=reasoning``（思考强度）的读写契约

请求体：``{"agent": "default", "scope": "reasoning", "reasoning_effort": "high",
"confirm": true}``（``fields: {"reasoning_effort": ...}`` 等价）。

* 合法取值 = ``none``（关闭）+ 宿主 ``VALID_REASONING_EFFORTS``
  （``minimal`` / ``low`` / ``medium`` / ``high`` / ``xhigh`` / ``max`` / ``ultra``），
  共 8 个；``GET /models/overview`` 的 ``reasoning_effort_values`` 是同一份；
* 归一化大小写/空格；归一化后仍非法 ⇒ 400 ``value_rejected``（**绝不**静默写默认值）；
* 请求体缺这个键 / 显式 ``null`` ⇒ **保持不变，零写请求**（``changed: false``，不建快照）；
* 落盘就是那个字符串（``none`` 的"关闭"语义由宿主
  ``parse_reasoning_effort`` 解释成 ``{"enabled": False}``）；
* 响应额外给 ``field: "agent.reasoning_effort"`` 与 ``reasoning_effort``（写入后的值）。

### ★ 红线：思考强度**不许**是别的 scope 的副作用

老板原话：**换布局不许顺带覆盖推理强度。** 所以：

* 只有 ``scope=reasoning`` 且请求体**明确带了值**时才写这一个键；
* 写 ``model`` / ``fallback`` / ``fallbacks`` / ``auxiliary`` 时
  ``agent.reasoning_effort`` **一个字节都不动**（``_plan_write`` 那四支根本不碰 ``agent:`` 段）；
* ``scope=reasoning`` 落盘前跑 :func:`_changed_paths` 核对"到底改了哪几个路径"，
  只要有一个不是 ``agent.reasoning_effort`` 就 500 拒绝写入 —— 护栏在代码里，不在记性里。

### 按模型覆盖（``agent.reasoning_overrides``）：只读

宿主 ``hermes_constants.resolve_reasoning_config`` 的优先级是
**per-model 覆盖 > ``agent.reasoning_effort``**。``GET /models/overview`` 每行的
``reasoning.overrides`` 给出只读视图，界面必须显示"这些模型有覆盖，改全局对它们无效"。
**本分区不写** ``reasoning_overrides``（原因见 :func:`_reasoning_overrides` 的 docstring）。

``scope=fallbacks`` 两种写法（**互斥**，都给 ⇒ 400）：

* ``fallback_models: [{provider, model}, ...]`` —— **整链替换**，``[]`` 表示清空备用链；
* ``ops: [{op: ...}]`` —— 在当前链上**逐条**动，未被点到的条目（含 provider/model
  之外的键）原样保留。``op`` 允许 ``append`` / ``insert``（``index`` 可省 = 追加）/
  ``remove``（``index``，或 ``{provider, model}`` 定位）/ ``move``（``from`` / ``to``）/
  ``clear`` / ``replace``（``fallback_models``）。

``scope=auxiliary`` 的请求体：``{"slot": "approval", "provider": ..., "model": ...,
"fields": {"reasoning_effort": ...}}``。可写字段**按槽位实际 schema 动态算**：
``provider`` / ``model`` 恒可写，其余只放该槽位**已存在**的可选字段
（``reasoning_effort`` / ``timeout`` / ``base_url`` / ``api_mode``）。``key_env`` 这类
凭据指针**只读**：不写、不回明文（写它 ⇒ 400 ``read_only_field``）。槽位清单与每行
的 ``writable_fields`` 见 ``GET /models/overview`` 的 ``auxiliary_slots``。

"保持不变"的字段/段**不产生任何写请求**：结构化段算完发现一个字节都不用改时，
``/apply`` 直接返回 ``changed: false``（不建快照、不落盘）。

## moa 为什么曾经不在 provider 列表里（2026-10-29 查清，代码行号为准）

``moa`` 是**官方注册过的虚拟 provider**，但**不在** ``hermes_cli.auth.PROVIDER_REGISTRY`` 里：

* ``hermes-agent/hermes_cli/providers.py:29``：``HERMES_OVERLAYS["moa"] =
  HermesOverlay(auth_type="virtual", base_url_override="moa://local")``；
* ``hermes-agent/hermes_cli/auth.py:242``：``PROVIDER_REGISTRY`` 由 ``_REGISTRY_ROWS``
  生成，**没有任何一行是 moa**（实测 ``len=78``，``"moa" in PROVIDER_REGISTRY == False``）；
* ``hermes-agent/hermes_cli/runtime_provider.py:984``：``provider == "moa"`` 走
  ``_resolve_requested_shortcuts`` 短路，返回 ``moa://local`` + 占位 key。

本文件原来的 provider 枚举只有两个来源 —— ``config.yaml`` 的 ``providers:`` 段 +
``PROVIDER_REGISTRY``（``_builtin_registry`` L424-450，取数在 L431-433）。于是：

* **不是"被过滤掉"**：``_provider_rows``（L488-552）里没有任何一行针对 moa 的判断；
* **不是"没进 catalog"**：模型名本来就只有 ``providers.<p>.models`` 一个来源；
* **是"根本没被枚举"**：moa 既没写进该 agent 的 ``providers:``（``default`` 的
  ``config.yaml`` 只有 openrouter / gemini / balance 三个），也不在 ``PROVIDER_REGISTRY``，
  所以 ``_provider_names``（L453-459）里没有它，``_require_provider``（L462-482）还会
  以 ``invalid_argument`` 拒绝 ``provider=moa``。

修法：新增 ``_virtual_providers()`` 从 ``HERMES_OVERLAYS`` 取 ``auth_type == "virtual"``
的 provider（当前只有 moa），并入 provider 枚举与写入路径；moa 的"模型列表"=
该 agent ``config.yaml`` 里 ``moa.presets`` 的**预设名**（``model.default`` 对
``provider: moa`` 就是预设名，见 ``agent/moa_loop.py:1541-1546`` 与
``bind_moa_runtime``），不是 OpenAI 风格的模型 id。

## 五条安全红线（本卡最重要，写在这里是为了下一任不用翻卡片）

1. **写操作必须备份 → 写入 → 回读校验。**
   ``POST /apply`` / ``POST /rollback`` 会改真实 ``config.yaml``，所以：

   * 写前把该 agent 的 ``config.yaml`` **按字节**复制进快照目录，快照路径**写进响应**；
   * 写后**回读磁盘**（``read_config_doc``），逐键比对期望值；不符 ⇒ 自动用刚做的快照
     盖回去，返回 ``verify_mismatch``（**不是** ``ok:true``），并带上 ``restore_path``；
   * 只写目标那一段（``model:`` / ``fallback_providers:`` / ``auxiliary.<slot>`` /
     ``agent.reasoning_effort``），**绝不整篇重写**（走 ``atomic_config_write``：
     保留注释、拒绝让缺键的 dict 删掉既有配置）。

2. **单个写用 ``/apply``；批量写只走 ``/batch/apply``，且逐个 agent 独立快照。**
   ``POST /models/apply`` 仍只作用于一个 agent，请求体带 ``targets`` / ``agents`` ⇒
   400 ``multi_agent_forbidden``（旧插件的 ``targets: [p1, p2, ...]`` 一次跨 profile 写，
   写错时没人知道该恢复哪个）。批量需求走 ``/models/batch/apply``：
   对**每个** agent 各做一份按字节快照、各自回读校验，任何一条失败只影响那一条，
   并写一份 ``data/batches/<batch_id>.json`` 账本 —— ``/models/batch/rollback`` 拿
   ``batch_id`` 就能逐个还原回各自的原值。界面上「预览 → 确认 → 逐 agent 回读 → 一键回滚」。

3. **二次确认在服务端也有一道。** 请求体没有 ``confirm: true`` ⇒ 400
   ``confirmation_required``。界面的「当前 → 目标」对比条是第一道，这里是第二道；
   任何脚本直接打接口都不会"手滑"写配置。

4. **凭据永不出网。** ``GET /providers`` 只给 ``key_env``（变量**名**）+ ``key_present``
   （布尔）+ ``credentials`` 里经 ``core.settings.mask_sensitive`` 打码后的 ``***``。
   真实取值一个字节都不出现在响应里。

5. **不手写 YAML。** 读走 ``hermes_cli.config.read_config_doc``（原始读，不展开 ``${VAR}``），
   写走 ``hermes_cli.config.atomic_config_write``（官方原子写入器），作用域走
   ``hermes_cli.web_server_profiles._config_profile_scope(agent)``（官方 profile 作用域）。
   本文件里**没有任何一处**自己拼 YAML 文本 / 自己 ``yaml.dump``。

## 与旧插件的差异（有意的）

* 旧插件把 ``hermes config set`` 当写入器（每键一次子进程，≈2.2s/键，且失败语义靠解析 CLI 输出）；
  本分区一次性读原始 doc → 改 dict → 官方原子写入，**一次落盘**，回读校验是断言而不是 CLI 回显。
* 旧插件的 ``TARGET_PROFILES`` 是写死的四个 profile 名；本分区**一个 agent 名字都没有**
  （``default`` 除外，它是 home 根目录的固定语义），全部走 ``core.scope.list_agents()`` 现扫。
* ``presets`` 从"读写各 profile 的 ``model:`` 预设块"改成 console 自己的
  ``data/presets.json``：**增删改预设不动任何 profile 的 config.yaml**
  （探针第 8 条就是拿 sha256 盯着这条）。
* 旧插件的 ``moa`` 一等公民通道已并入本分区（``/models/moa/presets``）：
  ``GET/POST/PUT/DELETE`` 直接改 ``moa.presets.<预设名>``（``reference_models`` /
  ``aggregator`` / ``enabled`` / ``fanout`` / ``degraded_reference_policy``），
  纯逻辑在 ``api_moa_presets.py``（移植自旧插件 ``moa_presets.py``）。
  引用保护照旧插件的口径：被 ``moa.default_preset`` / ``model.default`` /
  ``fallback_providers[].model`` 引用的预设，**删**一律 409、**改**默认也 409
  （要改被引用的内容需显式 ``allow_referenced_edit: true``；改名永远不允许）。
  **moa 作为 provider 依然可选**：``/models/providers`` 会列出它（``kind="virtual"``），
  ``/models/models?provider=moa`` 返回该 agent ``moa.presets`` 里的预设名
  （``source="moa-presets"``），写 ``provider: moa`` + ``model.default: <预设名>`` 与宿主
  ``bind_moa_runtime`` 完全同口径。
"""
from __future__ import annotations

import contextlib
import copy
import functools
import importlib
import json
import logging
import os
import re
import sys
import threading
import concurrent.futures as futures
from concurrent.futures import ThreadPoolExecutor
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Body, Query  # type: ignore
from fastapi.responses import JSONResponse  # type: ignore

_DASHBOARD_DIR = Path(__file__).resolve().parent
if str(_DASHBOARD_DIR) not in sys.path:
    # 本文件被 dashboard 用合成模块名 exec，没有包上下文；core 包只能靠 sys.path 找到。
    sys.path.insert(0, str(_DASHBOARD_DIR))

import api_moa_presets as moa_presets  # noqa: E402 - 纯逻辑模块（不依赖 FastAPI / 本文件）

router = APIRouter(prefix="/models")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量（前端/探针按字面量分支，别改）
# ---------------------------------------------------------------------------
#: ``default`` 不是 ``profiles/`` 下的目录，它是 home 根本身（与 core.scope 同口径）。
DEFAULT_AGENT = "default"

CODE_INVALID_ARGUMENT = "invalid_argument"
CODE_NOT_FOUND = "not_found"
CODE_CONFLICT = "conflict"
CODE_UPSTREAM_ERROR = "upstream_error"
CODE_CONFIRMATION_REQUIRED = "confirmation_required"
CODE_MULTI_AGENT_FORBIDDEN = "multi_agent_forbidden"
CODE_VERIFY_MISMATCH = "verify_mismatch"
CODE_WRITE_BUSY = "write_busy"
CODE_SNAPSHOT_FAILED = "snapshot_failed"
CODE_UNSUPPORTED = "unsupported_provider_kind"
CODE_READ_ONLY_FIELD = "read_only_field"
#: 值不合法（**不是**语法错误，而是"这个字段不接受这个取值"）：例如思考强度给了
#: ``banana``。与 ``invalid_argument`` 分开是为了让界面能一眼把"字段名写错/缺字段"
#: 和"取值不在允许集合里"区分开，并且**绝不静默写成默认值**。
CODE_VALUE_REJECTED = "value_rejected"

#: 允许写的段。五段语义各不相同（**旧的 ``fallback`` 语义一字不动地保留**）：
#:
#: * ``model`` —— 主模型（``model.provider`` + ``model.default``）；
#: * ``fallback`` —— 备用模型链**首条**（旧契约：只认一条，``fallback_providers[0]``）；
#: * ``fallbacks`` —— 备用模型**有序整链**：任意条数、可追加 / 删除 / 排序 / 清空；
#: * ``auxiliary`` —— 辅助模型**单个槽位**（槽位名按该 agent 的 ``auxiliary:`` 段
#:   动态取，不硬编码）；
#: * ``reasoning`` —— 主模型的**思考强度**（``agent.reasoning_effort`` 一个键）。
#:
#: ★ ``reasoning`` **只能由它自己写**：别的四段任何一段都**不许**碰
#: ``agent.reasoning_effort``（老板原话：「换布局不许顺带覆盖推理强度」）。
SCOPES = ("model", "fallback", "fallbacks", "auxiliary", "reasoning")

#: 不要求顶层 ``provider`` / ``model`` 的段：目标写在 payload 的结构里
#: （``fallback_models`` / ``ops`` / ``slot`` + ``fields`` / ``reasoning_effort``）。
STRUCTURED_SCOPES = ("fallbacks", "auxiliary", "reasoning")

#: 预设只描述"一个 provider + 一个 model"，因此只认这两段
#: （结构化段进预设会丢掉"整链 / 槽位"信息，宁可不支持，也不给半截语义）。
PRESET_SCOPES = ("model", "fallback")

#: 备用模型链最多允许多少条（防呆；再多就该换方案而不是堆链）。
FALLBACK_MAX_ENTRIES = 32

#: 备用链 ``ops`` 允许的操作名。
FALLBACK_OPS = ("append", "insert", "remove", "move", "clear", "replace")

#: 辅助槽位里**永远只读**的字段（凭据指针）：不许写，响应里只给变量**名**，
#: 绝不给取值，也绝不写回。
AUX_READONLY_FIELDS = frozenset({"key_env", "api_key_env", "api_key_env_var", "base_url_env_var"})
#: 辅助槽位里"该槽位**已存在**才可写"的可选字段（``provider`` / ``model`` 恒可写）。
AUX_OPTIONAL_FIELDS = ("reasoning_effort", "timeout", "base_url", "api_mode")
#: 辅助槽位名的文法（与 provider 同款）。
AUX_SLOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: 辅助槽位 ``timeout`` 的合法区间（秒）。
AUX_TIMEOUT_RANGE = (0, 3600)

#: ``agent.reasoning_effort``（主模型思考强度）在 ``config.yaml`` 里的点分路径。
#: 响应里逐字给这个路径，界面不用猜"到底是 agent 段还是 model 段"。
REASONING_EFFORT_PATH = "agent.reasoning_effort"
#: 按模型覆盖思考强度的映射（``agent.reasoning_overrides.<model>``）。**只读展示**：
#: 本分区不写它（见 ``_reasoning_overrides`` 的 docstring 与 STATUS 报告）。
REASONING_OVERRIDES_PATH = "agent.reasoning_overrides"
#: 宿主 ``hermes_constants.VALID_REASONING_EFFORTS`` 的兜底副本（运行时以宿主为准，
#: 见 :func:`_reasoning_levels`；宿主不在时用这份，绝不让面板因为没有宿主而拒写）。
REASONING_EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
#: **关闭**语义的规范写法（宿主 ``parse_reasoning_effort`` 把 ``none`` 解析成
#: ``{"enabled": False}``）。界面上"关闭"就是它。
REASONING_EFFORT_OFF = "none"

#: 凭据**指针**键（值是环境变量名，不是秘密，必须原样展示）。
KEY_POINTER_NAMES = frozenset({"key_env", "api_key_env", "api_key_env_var", "base_url_env_var"})
#: 凭据**取值**键（值是真密钥，必须打码）。
SECRET_KEY_NAMES = frozenset({
    "api_key", "apikey", "api", "api_token", "access_token", "refresh_token", "token",
    "secret", "client_secret", "password", "passwd", "credential", "credentials",
})

#: 预设名的文法：字母数字开头，1-64 位字母数字/点/下划线/短横线。
PRESET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: 预设文件版本（将来改结构时靠它迁移）。
PRESETS_VERSION = 1
#: console 自己的数据目录名（相对 Hermes home）：``<home>/plugins/hermes-console/data``。
CONSOLE_PLUGIN_ID = "hermes-console"
#: 快照 / 预设的落点（都在上面那个 data 目录里）。
SNAPSHOTS_DIRNAME = "snapshots"
PRESETS_FILENAME = "presets.json"
#: 批量账本目录（每次 /batch/apply 一份，一键回滚靠它找回"每个 agent 各自的原值"）。
BATCHES_DIRNAME = "batches"
#: 一次批量最多作用多少个 agent（再多就该分两次做，界面也画不下）。
BATCH_MAX_AGENTS = 64
#: 批量 id 文法（自己生成，只接受这种形状 ⇒ 顺带挡住路径穿越）。
BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: 表单/请求体里 provider 名的文法（内置 registry 里全是这种小写短横线名）。
PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: 疑似真密钥的形态（``_sanitize_text`` 用；宁可多打码，不许漏）。
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bpk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\brk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{8,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{8,}"),
    re.compile(r"\bxai-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}"),
)

#: provider 行的来源标签（界面直接显示这三个词）。
PROVIDER_SOURCE_LABELS = {
    "config": "配置文件",
    "builtin": "内置",
    "virtual": "虚拟",
}

#: 写锁：apply / rollback 互斥（同一时刻只允许一个写操作在跑）。
_WRITE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 错误 / 统一失败体
# ---------------------------------------------------------------------------
class ApiError(Exception):
    """带 HTTP 语义的业务错误 → 统一失败体。"""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra: Dict[str, Any] = dict(extra)


def _fail(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    """统一失败体（与 ``api_vcc`` / ``api_upgrade`` / ``api_remote`` 同款）。"""
    error: Dict[str, Any] = {"code": code, "message": message}
    error.update({key: value for key, value in extra.items() if key not in ("code", "message")})
    return JSONResponse(status_code=status, content={"ok": False, "error": error})


def _api(func):
    """把业务异常/意外异常统一翻成 ``{ok:false,error:{code,message}}``。

    **没有任何一条路由允许 500 裸栈**：``ApiError`` 按自带状态码走；其它异常一律
    500 ``upstream_error`` + 脱敏后的一句话（栈只进日志）。
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except ApiError as exc:
            return _fail(exc.status, exc.code, _sanitize_text(exc.message), **exc.extra)
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不让栈冒到 FastAPI
            logger.exception("hermes-console.api_models: %s 失败", func.__name__)
            return _fail(
                500,
                CODE_UPSTREAM_ERROR,
                "内部错误：%s: %s" % (type(exc).__name__, _sanitize_text(str(exc))),
            )

    return wrapper


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------
def _sanitize_text(text: str) -> str:
    """把疑似密钥的片段替换成 ``***``（用于任何要进响应体的字符串）。"""
    out = "" if text is None else str(text)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("***", out)
    return out


def _is_secret_key(name: str) -> bool:
    """键名是否**指向一个真密钥取值**（``key_env`` 这类指针不算）。"""
    lowered = str(name).strip().lower()
    if lowered in KEY_POINTER_NAMES:
        return False
    return lowered in SECRET_KEY_NAMES


def _credential_view(block: Dict[str, Any]) -> Dict[str, Any]:
    """一段 provider 配置里"跟凭据有关"的部分，经 ``core.settings.mask_sensitive`` 打码。

    返回 ``mask_sensitive`` 的原样结果（含 ``masked_keys``），所以响应里能看到
    ``{"api_key": "***", "masked_keys": ["api_key"]}`` —— 探针断言的就是这个形状：
    **键名可见、取值不可能是真密钥**。

    只把 ``_is_secret_key`` 命中的键交给它：``key_env`` 是变量**名**，属于必须展示的信息，
    打码会让界面没法告诉人去哪个环境变量里配 key。
    """
    subset: Dict[str, Any] = {}
    for key, value in (block or {}).items():
        if _is_secret_key(str(key)):
            subset[str(key)] = value
    if not subset:
        return {"masked_keys": []}
    try:
        masked = _core("settings").mask_sensitive(subset)
    except Exception as exc:  # noqa: BLE001 - 脱敏失败必须失败关闭，不能把原文透出去
        logger.warning("hermes-console.api_models: mask_sensitive 不可用（按全掩码处理）：%s", exc)
        return {"masked_keys": sorted(subset.keys()), **{key: "***" for key in subset}}
    # 双保险：万一 mask_sensitive 的键名规则没覆盖到，这里再按"真密钥键名"扫一遍。
    for key in list(masked.keys()):
        if key == "masked_keys":
            continue
        if not isinstance(masked[key], dict) and _is_secret_key(key) and masked[key] != "***":
            masked[key] = "***"
    return masked


# ---------------------------------------------------------------------------
# core 模块：惰性 import（core 坏掉只让本分区 503，不在挂载时拖垮整个插件）
# ---------------------------------------------------------------------------
def _core(submodule: str) -> Any:
    """import ``core.<submodule>``，带"同名别家模块"防护（口径同 ``api_remote._core``）。"""
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
    不直接读 ``HERMES_HOME``（worker 进程里它是 ``<home>/profiles/<p>`` 语义），
    口径与 ``core.scope.hermes_home()`` 完全一致。
    """
    return Path(_core("scope").hermes_home(None))


# ---------------------------------------------------------------------------
# agent 枚举（动态扫盘，不写死任何名字）
# ---------------------------------------------------------------------------
def _agent_entries() -> List[Dict[str, Any]]:
    """``core.scope.list_agents()`` 的原始结果（每项含 id / label / home / config_path）。"""
    return list(_core("scope").list_agents(home=_home()))


def _agent_ids() -> List[str]:
    """当前真实存在的 agent id（每次请求现扫）。"""
    return [str(entry["id"]) for entry in _agent_entries()]


def _labels() -> Dict[str, str]:
    """id → 人可读名字（``profile.yaml`` 的 title，读不到就是 id）。"""
    return {str(entry["id"]): str(entry.get("label") or entry["id"]) for entry in _agent_entries()}


def _require_agent(raw: Optional[str], *, write: bool) -> str:
    """把外部输入归一成**真实存在**的 agent id。

    * 文法非法（``../../etc`` 之类）⇒ 400 ``invalid_argument``；
    * 文法合法但**不存在** ⇒ 写操作（``write=True``）400 ``invalid_argument``，
      只读查询 404 ``not_found``；两者的失败体都带 ``agents``（当前合法 id 列表）。

    **在任何读/写资源之前完成校验** —— apply/rollback 先调它，非法输入连一次快照都不做。
    """
    scope = _core("scope")
    try:
        agent = str(scope.normalize_agent_id(raw))
    except ValueError as exc:
        raise ApiError(400, CODE_INVALID_ARGUMENT, str(exc),
                       agents=scope.list_agent_ids(home=_home())) from exc
    known = _agent_ids()
    if agent not in known:
        message = "agent %r 不存在（当前不存在这个 profile 目录）" % agent
        if write:
            raise ApiError(400, CODE_INVALID_ARGUMENT, message, agents=known, agent=agent)
        raise ApiError(404, CODE_NOT_FOUND, message, agents=known, agent=agent)
    return agent


# ---------------------------------------------------------------------------
# 路径：config.yaml / 快照 / 预设（全部挂在 home 之下，探针换 home 即全隔离）
# ---------------------------------------------------------------------------
def _config_path(agent: str) -> Path:
    """某 agent 的 ``config.yaml`` 路径（不保证存在）。"""
    return Path(_core("scope").agent_config_path(agent, home=_home()))


def _data_dir() -> Path:
    """console 自己的数据目录：``<home>/plugins/hermes-console/data``。

    刻意**不**用 ``Path(__file__)`` 推：探针把 home 指到沙盒时，预设/快照也得跟着进沙盒，
    否则"零污染"断言会变成看运气（真实目录里凭空多出一个 presets.json）。
    真实部署下本插件就住在 ``<home>/plugins/hermes-console``，两种算法得到同一路径。
    """
    return _home() / "plugins" / CONSOLE_PLUGIN_ID / "data"


def _snapshots_root() -> Path:
    return _data_dir() / SNAPSHOTS_DIRNAME


def _presets_path() -> Path:
    return _data_dir() / PRESETS_FILENAME


# ---------------------------------------------------------------------------
# 配置读取（全部走官方原始读；**不展开** ${VAR}，因此读到的和写回的是同一份文本语义）
# ---------------------------------------------------------------------------
def _read_config_doc(path: Path) -> Dict[str, Any]:
    """官方原始读：``hermes_cli.config.read_config_doc``；缺文件 ⇒ ``{}``，坏文件 ⇒ 抛。"""
    from hermes_cli.config import read_config_doc  # 官方读取器

    try:
        data = read_config_doc(path)
    except FileNotFoundError:
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ApiError(500, CODE_UPSTREAM_ERROR,
                       "%s 的根不是 mapping（%s）" % (path, type(data).__name__))
    return dict(data)


def _read_config(agent: str) -> Dict[str, Any]:
    """某 agent 的配置（只读，fail-soft：读不到就是 ``{}``，由 ``core.scope`` 保证）。"""
    return dict(_core("scope").read_agent_config(agent, home=_home()))


def _model_block(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """``model:`` 段（字符串简写 ``model: foo`` 归一成 ``{"default": foo}``）。"""
    block = cfg.get("model")
    if isinstance(block, str):
        return {"default": block}
    return dict(block) if isinstance(block, dict) else {}


def _providers_block(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """``providers:`` 段（值不是 dict 的条目按 ``{}`` 处理，不抛）。"""
    block = cfg.get("providers")
    if not isinstance(block, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, value in block.items():
        name = str(key).strip()
        if name:
            out[name] = dict(value) if isinstance(value, dict) else {}
    return out


def _fallback_entries(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``fallback_providers:`` 链（只保留 dict 项；provider/model 归一成字符串）。"""
    raw = cfg.get("fallback_providers")
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append({
            "provider": str(item.get("provider") or "").strip(),
            "model": str(item.get("model") or "").strip(),
        })
    return out


def _auxiliary_summary(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``auxiliary:`` 段（辅助模型）的**只读摘要**：槽位名 + provider + model。

    只取 provider/model 两个字段（**绝不**带 ``key_env`` / ``base_url`` 这类路由与凭据细节）。
    段不存在 ⇒ 空数组。

    **签名与返回形状保持不变**（面板在用）。写契约不在这里：
    槽位与可写字段见 :func:`_auxiliary_slots`，写入口是 ``scope=auxiliary``。
    """
    block = cfg.get("auxiliary")
    if not isinstance(block, dict):
        return []
    out: List[Dict[str, Any]] = []
    for slot in sorted(block.keys()):
        item = block.get(slot)
        if not isinstance(item, dict):
            continue
        out.append({
            "slot": str(slot).strip(),
            "provider": _sanitize_text(str(item.get("provider") or "").strip()),
            "model": _sanitize_text(str(item.get("model") or "").strip()),
        })
    return out


def _fallback_raw(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``fallback_providers:`` 的**原样**条目（保留 provider/model 之外的键）。

    ``scope=fallbacks`` 的 ``ops`` 模式用它：只增 / 删 / 移某一条时，别的条目里可能
    还有 ``base_url`` / ``reasoning_effort`` 这类宿主认得的键，不能被归一化顺手抹掉。
    """
    raw = cfg.get("fallback_providers")
    if not isinstance(raw, (list, tuple)):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _fallback_entry_of(item: Dict[str, Any]) -> Dict[str, Any]:
    """一条备用模型 ⇒ ``{provider, model}``（去空白；别的键不带走）。"""
    return {
        "provider": str(item.get("provider") or "").strip(),
        "model": str(item.get("model") or "").strip(),
    }


def _auxiliary_block(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """``auxiliary:`` 段（**只读视图**，不复制条目）。段不是 mapping ⇒ ``{}``。"""
    block = cfg.get("auxiliary")
    return block if isinstance(block, dict) else {}


def _aux_slot_names(cfg: Dict[str, Any]) -> List[str]:
    """配置里**真实存在**的辅助槽位名（排序后）。

    **不硬编码槽位名**：上游以后加 ``image_gen`` 之类的槽位，这里自动多一条，
    契约与面板都不用改代码。
    """
    block = _auxiliary_block(cfg)
    return sorted(str(name).strip() for name, item in block.items()
                  if isinstance(item, dict) and str(name).strip())


def _aux_writable_fields(item: Dict[str, Any]) -> List[str]:
    """某槽位**当前可写**的字段。

    ``provider`` / ``model`` 恒可写；其余只放该槽位**已经存在**的可选字段
    （``reasoning_effort`` / ``timeout`` / ``base_url`` / ``api_mode``）。
    ``key_env`` 这类凭据指针**永不**出现在这里（只读）。
    """
    fields = ["provider", "model"]
    for name in AUX_OPTIONAL_FIELDS:
        if name in item and name not in fields:
            fields.append(name)
    return fields


def _auxiliary_state(cfg: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """``auxiliary:`` 段的**可写字段**取值（槽位 → {字段: 字符串}）。

    只收 ``provider`` / ``model`` / 可选字段。``key_env`` 这类凭据指针**不进状态**：
    回读校验不需要它，响应体里也就不该出现它。
    """
    block = _auxiliary_block(cfg)
    out: Dict[str, Dict[str, str]] = {}
    for slot in _aux_slot_names(cfg):
        item = block.get(slot) or {}
        row: Dict[str, str] = {}
        for field in _aux_writable_fields(item):
            value = item.get(field)
            row[field] = "" if value is None else str(value).strip()
        out[slot] = row
    return out


def _auxiliary_slots(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``auxiliary:`` 段的**读写契约视图**（面板照着它渲染每个槽位的编辑器）。

    每行字段：

    * ``slot`` / ``provider`` / ``model`` —— 槽位名与当前值；
    * ``fields`` —— 该槽位**已存在**的可选字段取值（``reasoning_effort`` / ``timeout`` …）；
    * ``writable_fields`` —— 本次**可写**的字段名（动态算，不硬编码）；
    * ``read_only_fields`` —— 只读字段**名**（凭据指针）；
    * ``key_env`` —— 变量**名**（不是取值；只读，界面上用来说明 key 从哪来）；
    * ``present_fields`` —— 配置里实际有的键名（排查用）。

    槽位来自配置本身（**动态生成**），不写死 8 个常量。
    """
    block = _auxiliary_block(cfg)
    out: List[Dict[str, Any]] = []
    for slot in _aux_slot_names(cfg):
        item = block.get(slot) or {}
        out.append({
            "slot": slot,
            "provider": _sanitize_text(str(item.get("provider") or "").strip()),
            "model": _sanitize_text(str(item.get("model") or "").strip()),
            "fields": {name: _sanitize_text(str(item.get(name)))
                       for name in AUX_OPTIONAL_FIELDS if name in item},
            "writable_fields": _aux_writable_fields(item),
            "read_only_fields": sorted(name for name in item if name in AUX_READONLY_FIELDS),
            "key_env": _sanitize_text(str(item.get("key_env") or "").strip()),
            "present_fields": sorted(str(name) for name in item),
        })
    return out


def _reasoning_levels() -> Tuple[str, ...]:
    """思考强度的合法**等级**（不含关闭）：运行时以宿主常量为准。

    宿主 ``hermes_constants.VALID_REASONING_EFFORTS`` 是唯一真值来源（上游加一个等级，
    这里自动多一个选项）；宿主不可用 / 形状不对时退回 :data:`REASONING_EFFORT_LEVELS`
    （**绝不因为读不到宿主就拒写**，那会让面板在降级时整段不可用）。两者不一致时记一条
    warning，让"面板选项比宿主多/少"这件事在日志里看得见。
    """
    try:
        from hermes_constants import VALID_REASONING_EFFORTS  # type: ignore

        levels = tuple(str(item).strip().lower() for item in VALID_REASONING_EFFORTS
                       if str(item).strip())
        if levels:
            if levels != REASONING_EFFORT_LEVELS:
                logger.warning("hermes-console.api_models: 宿主 VALID_REASONING_EFFORTS=%s "
                               "与内置副本 %s 不一致，按宿主口径走",
                               list(levels), list(REASONING_EFFORT_LEVELS))
            return levels
    except Exception as exc:  # noqa: BLE001 - 宿主缺失只降级，不阻断
        logger.debug("hermes-console.api_models: VALID_REASONING_EFFORTS 不可用：%s", exc)
    return REASONING_EFFORT_LEVELS


def _reasoning_values() -> Tuple[str, ...]:
    """界面可选值 = **关闭**（``none``）+ 全部等级（共 8 个，顺序固定：关闭在最前）。"""
    return (REASONING_EFFORT_OFF,) + _reasoning_levels()


def _reasoning_off_semantics() -> Dict[str, Any]:
    """``none`` 在宿主眼里是什么（``parse_reasoning_effort("none")`` ⇒ ``{"enabled": False}``）。

    只用于响应/报告里说明"关闭"的落盘语义；读不到宿主就退回同一条已知结论。
    """
    try:
        from hermes_constants import parse_reasoning_effort  # type: ignore

        parsed = parse_reasoning_effort(REASONING_EFFORT_OFF)
        if isinstance(parsed, dict):
            return parsed
    except Exception as exc:  # noqa: BLE001
        logger.debug("hermes-console.api_models: parse_reasoning_effort 不可用：%s", exc)
    return {"enabled": False}


def _reasoning_effort(cfg: Dict[str, Any]) -> str:
    """``agent.reasoning_effort``（推理强度）的**只读取值**（原样字符串，缺失 ⇒ ``""``）。

    写它**只能**走 ``scope=reasoning``（:func:`_plan_reasoning`）。别的段（``model`` /
    ``fallback(s)`` / ``auxiliary``）永远不碰这个键 —— 老板原话「换布局不许顺带覆盖
    推理强度」，那条约束**原样保留**，并由 :func:`_changed_paths` 在写入前逐路径核对
    （只允许 ``agent.reasoning_effort`` 一个路径变）。

    ``reasoning_effort: false``（YAML 布尔）读出来是 ``""``：宿主把它当"关闭"，
    这里不把 ``False`` 当字符串展示。
    """
    block = cfg.get("agent")
    if not isinstance(block, dict):
        return ""
    return _sanitize_text(str(block.get("reasoning_effort") or "").strip())


def _reasoning_overrides(cfg: Dict[str, Any]) -> Dict[str, str]:
    """``agent.reasoning_overrides``（**按模型**覆盖思考强度）的只读视图：``{模型名: 取值}``。

    宿主 ``resolve_reasoning_config`` 的优先级是
    **per-model 覆盖 > ``agent.reasoning_effort``**，所以界面上必须能看到"这个模型有覆盖，
    改全局对它无效"。本分区**只读**这个映射：写它需要"删键"语义 + 模型名拼写归一
    （宿主匹配容忍 ``x-4.5`` / ``x.4.5`` / ``provider/x`` 等变体），半截实现会让用户
    以为改好了其实没生效。段缺失 / 不是 mapping ⇒ ``{}``。
    """
    block = cfg.get("agent")
    if not isinstance(block, dict):
        return {}
    overrides = block.get("reasoning_overrides")
    if not isinstance(overrides, dict):
        return {}
    out: Dict[str, str] = {}
    for model, raw in overrides.items():
        name = str(model).strip()
        if not name:
            continue
        if isinstance(raw, dict):
            enabled = raw.get("enabled", True) is not False
            level = str(raw.get("effort") or "").strip()
            out[name] = level if enabled else REASONING_EFFORT_OFF
            continue
        text = str(raw if raw is not None else "").strip()
        # YAML ``false`` ⇒ 关闭语义（与宿主 parse_reasoning_effort 同口径）
        out[name] = REASONING_EFFORT_OFF if str(raw).strip().lower() in ("false", "none", "disabled") \
            else text.lower()
    return out


def _current_state(agent: str) -> Dict[str, Any]:
    """该 agent **磁盘上**当前的模型状态（唯一真值来源，绝不读进程缓存）。"""
    cfg = _read_config(agent)
    block = _model_block(cfg)
    return {
        "provider": str(block.get("provider") or "").strip(),
        "model": str(block.get("default") or "").strip(),
        "base_url": str(block.get("base_url") or "").strip(),
        "api_mode": str(block.get("api_mode") or "").strip(),
        "key_env": str(block.get("key_env") or "").strip(),
        "fallbacks": _fallback_entries(cfg),
        "auxiliary": _auxiliary_state(cfg),
        "reasoning_effort": _reasoning_effort(cfg),
        "reasoning_overrides": _reasoning_overrides(cfg),
    }


# ---------------------------------------------------------------------------
# provider 目录（内置 registry + 该 agent 声明的 providers: 块）
# ---------------------------------------------------------------------------
def _builtin_registry() -> Dict[str, Dict[str, Any]]:
    """官方内置 provider registry（``hermes_cli.auth.PROVIDER_REGISTRY``）的只读摘要。

    条目字段：``id`` / ``label`` / ``base_url`` / ``auth_type`` / ``key_envs``（变量**名**列表）。
    **不带任何密钥取值**：``api_key_env_vars`` 本身就是变量名。
    """
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY  # type: ignore

        rows = dict(PROVIDER_REGISTRY)
    except Exception as exc:  # noqa: BLE001 - 没有 hermes_cli 时退回空目录（界面会标 degraded）
        logger.warning("hermes-console.api_models: PROVIDER_REGISTRY 不可用：%s", exc)
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for pid, entry in rows.items():
        name = str(getattr(entry, "id", None) or pid).strip()
        if not name:
            continue
        envs = getattr(entry, "api_key_env_vars", ()) or ()
        out[name] = {
            "id": name,
            "label": str(getattr(entry, "name", None) or name),
            "base_url": str(getattr(entry, "inference_base_url", "") or ""),
            #: 该 provider 的 base_url **覆盖变量名**（官方 registry 的 ``base_url_env_var``）：
            #  有值时优先于 ``inference_base_url``，与运行时的解析同序
            #  （``hermes_cli/runtime_provider.py`` 的 ``_explicit_api_key_provider``）。
            #  **只放变量名，绝不放取值** —— 取值只在 ``_provider_endpoint`` 里出站用。
            "base_url_env_var": str(getattr(entry, "base_url_env_var", "") or ""),
            "auth_type": str(getattr(entry, "auth_type", "") or ""),
            "key_envs": [str(item) for item in envs if str(item).strip()],
        }
    return out


def _virtual_providers() -> Dict[str, Dict[str, Any]]:
    """官方 ``HERMES_OVERLAYS`` 里 ``auth_type == "virtual"`` 的 provider（当前只有 ``moa``）。

    **为什么单独一张表**：``moa`` 不 ``PROVIDER_REGISTRY``（``hermes_cli/auth.py:242`` 的
    ``_REGISTRY_ROWS`` 里没有它），只在 ``hermes_cli/providers.py:29`` 的
    ``HERMES_OVERLAYS`` 里声明为 ``auth_type="virtual"`` / ``base_url_override="moa://local"``。
    只读 ``PROVIDER_REGISTRY`` 的枚举自然就把它漏掉了 —— 这就是设置页看不到 moa 的原因。

    字段与内置 registry 同形（``id`` / ``label`` / ``base_url`` / ``auth_type`` / ``key_envs``），
    额外 ``virtual=True``。虚拟 provider 没有 key（``key_envs`` 恒空），``base_url`` 是
    ``moa://local`` 这种非 http 的本地标记 —— 因此**永远不会**被拿去发外网请求。

    官方模块缺失只退回空表（界面少一行，不 500）。
    """
    try:
        from hermes_cli.providers import HERMES_OVERLAYS  # 官方静态覆盖表（离线，无网络）
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes-console.api_models: HERMES_OVERLAYS 不可用：%s", exc)
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for pid, overlay in dict(HERMES_OVERLAYS).items():
        kind = str(getattr(overlay, "auth_type", "") or "").strip().lower()
        if kind != "virtual":
            continue
        name = str(pid or "").strip()
        if not name:
            continue
        label = name
        base_url = str(getattr(overlay, "base_url_override", "") or "")
        try:
            from hermes_cli.providers import get_provider  # 官方解析（只读缓存，不联网）

            pdef = get_provider(name, allow_network=False)
            if pdef is not None:
                label = str(getattr(pdef, "name", "") or label)
                base_url = str(getattr(pdef, "base_url", "") or base_url)
        except Exception:  # noqa: BLE001 - 名字退化成 id，不影响可用性
            pass
        out[name] = {
            "id": name,
            "label": label,
            "base_url": base_url,
            "auth_type": "virtual",
            "key_envs": [],
            "virtual": True,
        }
    return out


def _virtual_lookup() -> Dict[str, str]:
    """``小写名 → 官方写法``（校验 provider 时用，避免大小写不一致导致两套口径）。"""
    return {name.lower(): name for name in _virtual_providers()}


def _is_virtual_provider(name: Any) -> bool:
    """这个 provider 名是不是虚拟 provider（moa）。"""
    return str(name or "").strip().lower() in _virtual_lookup()


def _moa_presets(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """``moa:`` 段里的预设（**原始读**，不发明不存在的预设）。

    返回 ``{"presets": {名字: 块}, "default_preset": str, "enabled": [...], "disabled": [...]}``。
    ``model.default`` 在 ``provider: moa`` 下就是预设名（``agent/moa_loop.py:1541``：
    ``resolved_preset = agent.model``），所以这里列出来的就是"moa 的模型列表"。
    """
    block = cfg.get("moa")
    if not isinstance(block, dict):
        return {"presets": {}, "default_preset": "", "enabled": [], "disabled": [], "has_block": False}
    presets = block.get("presets")
    presets = presets if isinstance(presets, dict) else {}
    enabled: List[str] = []
    disabled: List[str] = []
    for key, value in presets.items():
        name = str(key).strip()
        if not name:
            continue
        if isinstance(value, dict) and value.get("enabled") is False:
            disabled.append(name)
        else:
            enabled.append(name)
    return {
        "presets": {str(key).strip(): (dict(value) if isinstance(value, dict) else {})
                    for key, value in presets.items() if str(key).strip()},
        "default_preset": str(block.get("default_preset") or "").strip(),
        "enabled": sorted(enabled),
        "disabled": sorted(disabled),
        "has_block": True,
    }


def _provider_names(agent: str) -> List[str]:
    """该 agent 视角下所有**可切换**的 provider 名（声明过的在前，虚拟的其次，内置的补最后）。"""
    cfg = _read_config(agent)
    declared = list(_providers_block(cfg).keys())
    seen = {name.lower() for name in declared}
    virtual: List[str] = []
    for name in sorted(_virtual_providers()):
        if name.lower() not in seen:
            virtual.append(name)
            seen.add(name.lower())
    extra = [name for name in sorted(_builtin_registry()) if name.lower() not in seen]
    return declared + virtual + extra


def _require_provider(agent: str, raw: Optional[str]) -> str:
    """校验 provider 名：必须在该 agent 可切换集合里（声明过 或 内置 registry 里）。

    不存在 ⇒ 400 ``unknown_provider`` + ``providers``（当前可选列表），**不是 500**。
    """
    text = "" if raw is None else str(raw).strip()
    if not text:
        raise ApiError(400, CODE_INVALID_ARGUMENT, "缺少 provider（必填）",
                       providers=_provider_names(agent))
    if not PROVIDER_ID_RE.match(text):
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "非法 provider 名 %r：只能是 1-64 位字母/数字/点/下划线/短横线" % text,
                       providers=_provider_names(agent))
    allowed = _provider_names(agent)
    lowered = {name.lower(): name for name in allowed}
    if text.lower() not in lowered:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "agent %s 不认识 provider %r（既没在 config.yaml 的 providers: 里声明，"
                       "也不在内置 registry 里）" % (agent, text),
                       providers=allowed, agent=agent)
    return lowered[text.lower()]


# ---------------------------------------------------------------------------
# provider 行 / 模型列表（全部只读，凭据只出变量名与布尔）
# ---------------------------------------------------------------------------
def _provider_rows(agent: str, *, include_all: bool) -> List[Dict[str, Any]]:
    """组装 ``GET /providers`` 的行。

    * ``include_all=False``（默认）⇒ 只列**该 agent 真的能用**的：
      声明在 ``config.yaml`` 的 ``providers:`` 里，或内置 registry 且它的 ``key_env`` 有值；
    * ``include_all=True`` ⇒ 再加全部内置 registry 条目（``configured=false``）。

    每行字段：``provider`` / ``label`` / ``base_url`` / ``configured`` / ``configured_reason`` /
    ``current`` / ``model`` / ``key_env``（变量名）/ ``key_envs``（候选变量名）/
    ``key_present``（布尔）/ ``credentials``（``mask_sensitive`` 打码后的凭据键）。
    """
    cfg = _read_config(agent)
    declared = _providers_block(cfg)
    builtin = _builtin_registry()
    model = _current_state(agent)
    env_names = _env_names(agent)
    #: base_url 的 env 取值表（``_env_map_for`` 已按「进程环境 → home/.env → agent/.env」合并）。
    #  **只用于解析 base_url**：取值不进响应，响应里只有解析出来的地址本身。
    env_values = _env_map_for(agent)
    rows: List[Dict[str, Any]] = []
    seen: Dict[str, None] = {}

    def _row_for(name: str, block: Dict[str, Any], source: str) -> Dict[str, Any]:
        entry = builtin.get(name) or builtin.get(name.lower()) or {}
        key_env = str(block.get("key_env") or block.get("api_key_env") or "").strip()
        candidates = [key_env] if key_env else [str(x).strip() for x in (entry.get("key_envs") or ()) if str(x).strip()]
        present = any(item in env_names for item in candidates if item)
        base_url, base_url_source, _, base_url_env_var = _resolve_base_url(block, entry, env_values)
        return {
            "provider": name,
            "label": str(block.get("name") or entry.get("label") or name),
            "base_url": _sanitize_text(base_url),
            "base_url_source": base_url_source,
            "base_url_env_var": base_url_env_var,
            "kind": "provider",
            "source": source,
            "source_label": PROVIDER_SOURCE_LABELS.get(source, source),
            "in_config": source == "config",
            "in_builtin": bool(entry),
            "configured": True,
            "configured_reason": source,
            "current": bool(model["provider"]) and name.lower() == model["provider"].lower(),
            "model": str(block.get("model") or "").strip(),
            "key_env": key_env or (candidates[0] if candidates else ""),
            "key_envs": [item for item in candidates if item],
            "key_present": bool(present),
            "credentials": _credential_view(block),
            "declared_in_config": name in declared,
        }

    for name, block in declared.items():
        rows.append(_row_for(name, block, "config"))
        seen[name.lower()] = None

    # 虚拟 provider（moa）：不需要 key、不走 http，恒"可用"，因此默认列表里也列
    for name, entry in sorted(_virtual_providers().items()):
        if name.lower() in seen:
            continue
        row = _row_for(name, {}, "virtual")
        row["kind"] = "virtual"
        row["source_label"] = PROVIDER_SOURCE_LABELS["virtual"]
        row["virtual"] = True
        row["label"] = str(entry.get("label") or name)
        row["base_url"] = _sanitize_text(str(entry.get("base_url") or ""))
        row["configured"] = True
        row["configured_reason"] = "virtual"
        row["key_env"] = ""
        row["key_envs"] = []
        row["key_present"] = False
        row["model"] = model["model"] if row["current"] else ""
        rows.append(row)
        seen[name.lower()] = None

    for name in sorted(builtin):
        if name.lower() in seen:
            continue
        entry = builtin[name]
        envs = [str(item) for item in (entry.get("key_envs") or ())]
        present = any(item in env_names for item in envs if item)
        # 内置但既没声明、也探不到 key ⇒ 只有 include_all 才列（默认列表只给真能用的）
        if not include_all and not present:
            continue
        row = _row_for(name, {}, "builtin")
        row["configured"] = bool(present)
        row["configured_reason"] = "builtin" if present else ""
        rows.append(row)
        seen[name.lower()] = None

    rows.sort(key=lambda item: (0 if item.get("current") else 1,
                                0 if item.get("declared_in_config") else 1,
                                str(item.get("provider") or "")))
    return rows


def _env_names(agent: str) -> set:
    """该 agent 的 ``.env`` 里出现过的**变量名**集合（只读名字，绝不读取值）。

    口径：agent 自己的 ``.env`` → home 根的 ``.env`` → 进程环境名。
    真实取值一个字节都不读进内存，因此不可能泄漏。
    """
    names: set = set()
    for path in (_config_path(agent).parent / ".env", _home() / ".env"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name = stripped.partition("=")[0].strip()
            if name:
                names.add(name)
    names.update(os.environ.keys())
    return names


def _virtual_model_list(agent: str, provider: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """虚拟 provider（moa）的"模型列表" = 该 agent ``moa.presets`` 里的**预设名**。

    ``source`` 逐字说明来源：

    * ``moa-presets``：该 agent ``config.yaml`` 里有 ``moa.presets``，列出全部预设名；
    * ``moa-presets-empty``：有 ``moa:`` 段但一个预设都没有（宿主会回退到 ``default``）；
    * ``moa-presets-missing``：这个 agent 压根没有 ``moa:`` 段（切过去后 MoA 用内置默认配方，
      界面必须说清楚，不能假装"这个 agent 有 7 个预设"）。

    绝不联网：``base_url`` 是 ``moa://local``，本地标记不是端点。
    """
    entry = _virtual_providers().get(provider, {})
    if provider.lower() != "moa":
        return {
            "agent": agent, "provider": provider, "count": 0, "models": [],
            "source": "virtual-no-catalog", "default_model": "",
            "base_url": str(entry.get("base_url") or ""), "live_error": "",
            "kind": "virtual", "disabled_models": [], "presets_present": False,
            "note": "%s 是虚拟 provider，没有模型目录；它的\"模型\"由宿主自己的配方决定。" % provider,
        }
    presets = _moa_presets(cfg)
    names = sorted(presets["presets"].keys())
    if names:
        source = "moa-presets"
        note = ("moa 的\"模型\"就是 MoA 预设名（写进 model.default）；"
                "预设本身请在 MoA 入口里增删改，本分区不写配方。")
    elif presets["has_block"]:
        source = "moa-presets-empty"
        note = "该 agent 有 moa: 段但没有任何预设；宿主会回退到内置默认配方。"
    else:
        source = "moa-presets-missing"
        note = ("该 agent 的 config.yaml 里没有 moa: 段 —— 切到 moa 后用的是宿主内置默认配方，"
                "而不是这台机器 root config 里的那 7 个预设。")
    return {
        "agent": agent,
        "provider": provider,
        "count": len(names),
        "models": names,
        "source": source,
        "default_model": presets["default_preset"] or ("default" if presets["has_block"] else ""),
        "base_url": str(entry.get("base_url") or "moa://local"),
        "live_error": "",
        "kind": "virtual",
        "disabled_models": presets["disabled"],
        "presets_present": bool(names),
        "note": note,
    }


# ---------------------------------------------------------------------------
# 实时模型清单：真实 HTTP 请求 + 短 TTL 缓存 + 手动刷新（**唯一实现点**）
#
# 为什么只有这一处：``api_advisor`` / ``api_autotitler`` / ``api_autogroup`` 都复用本模块
# （``model_pair_error`` / ``_model_list`` / ``_provider_rows``）。模型来源一旦在别处再写一套，
# 面板展示的上游清单与写入口校验的清单就会再次分叉。
# ---------------------------------------------------------------------------
#: 实时清单的短 TTL（秒）。窗口内重复请求命中缓存；``refresh=1`` 无条件绕过。
LIVE_TTL_SECONDS = 60.0
#: 拉取**失败**的结果只缓存这么久（秒）：同一页里多个区块连续取数时不必反复打外网，
#: 但用户点刷新 / 稍等一下就会重新试真上游。
LIVE_FAIL_TTL_SECONDS = 5.0
#: 出站超时（秒）：连不上就快速失败并如实报错，不让面板长时间转圈。
LIVE_TIMEOUT_SECONDS = 6.0
#: 缓存：``(home, agent, provider) -> {"at": ts, "payload": {...}}``。
#: **只缓存成功**：失败不写缓存，下次请求必须重新看到真实错误（绝不拿旧结果顶）。
_LIVE_CACHE: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
_LIVE_LOCK = threading.Lock()

#: 版本段结尾（决定 ``/models`` 是拼在 ``<base>/models`` 还是 ``<base>/v1/models``）。
_MODELS_URL_TAIL_RE = re.compile(
    r"/(?:v\d+(?:beta\d*)?|paas/v\d+|compatible-mode/v\d+|api/gateway|zen(?:/go)?/v\d+)$")


def _flag(value: Any) -> bool:
    """查询参数的真值判定（与写接口的 ``_bool_flag`` 同口径）。"""
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _models_endpoint_url(base_url: str, provider: str) -> str:
    """``<base_url>/models`` 的实际 URL（含 anthropic / zai 这类非标准路径）。"""
    base = str(base_url or "").strip().rstrip("/")
    if base.lower().endswith("/models"):
        return base
    host = base.split("//", 1)[-1].split("/", 1)[0].lower()
    if "anthropic" in host:
        return base + "/v1/models"
    tail = base.split("//", 1)[-1]
    if "/" not in tail or not _MODELS_URL_TAIL_RE.search(base):
        return base + "/v1/models"
    return base + "/models"


def _provider_headers(url: str, token: str) -> Dict[str, str]:
    """出站请求头。**token 只进请求头**：不进返回值、不进日志、不进响应。"""
    headers = {"Accept": "application/json", "User-Agent": "hermes-console-models/1.0"}
    if not token:
        return headers
    if "anthropic" in url.lower():
        headers["x-api-key"] = token
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = "Bearer " + token
    return headers


def _parse_model_payload(raw: str) -> Tuple[List[str], str]:
    """上游响应 → 模型名列表。三种常见形状：OpenAI ``data[].id`` / Gemini ``models[].name`` / 纯列表。"""
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return [], "不是 JSON（%s）" % exc
    items: Any = None
    if isinstance(payload, dict):
        for key in ("data", "models", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                items = value
                break
        if items is None:
            return [], "JSON 里没有 data / models 数组（键：%s）" % "、".join(sorted(payload)[:6])
    elif isinstance(payload, list):
        items = payload
    else:
        return [], "JSON 根既不是对象也不是数组（%s）" % type(payload).__name__
    names: List[str] = []
    for item in items:
        ident = ""
        if isinstance(item, dict):
            ident = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
        elif isinstance(item, str):
            ident = item.strip()
        if ident.startswith("models/"):
            ident = ident.split("/", 1)[1]
        if ident:
            names.append(ident)
    return sorted(set(names)), ""


def _env_map_for(agent: str) -> Dict[str, str]:
    """该 agent 视角下的**取值**表：进程环境 → home 根 ``.env`` → agent 自己的 ``.env``（后者覆盖）。

    只用于拼出站请求头；取值绝不进任何响应、日志或返回结构。
    """
    values: Dict[str, str] = {str(k): str(v) for k, v in os.environ.items()}
    for path in (_home() / ".env", _config_path(agent).parent / ".env"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, _, value = stripped.partition("=")
            name = name.strip()
            if name:
                values[name] = value.strip().strip('"').strip("'")
    return values


def _agent_secret(agent: str, key_env: str) -> str:
    """某个环境变量的**取值**（只出站用）。取不到 ⇒ 空串（调用方据此报"无凭据"）。"""
    name = str(key_env or "").strip()
    if not name:
        return ""
    try:
        from hermes_cli.config import get_env_value_prefer_dotenv  # type: ignore

        value = str(get_env_value_prefer_dotenv(name) or "")
        if value:
            return value
    except Exception:  # noqa: BLE001 - 没有官方取值器就退回自己的 .env 解析
        pass
    return str(_env_map_for(agent).get(name) or "")


def _resolve_base_url(block: Dict[str, Any], entry: Dict[str, Any],
                      env_values: Dict[str, str]) -> Tuple[str, str, str, str]:
    """``(base_url, source, origin, env_var)`` —— base_url 的三段解析（**唯一实现点**）。

    顺序与运行时同序（``runtime_provider._explicit_api_key_provider``：``base_url_env_var``
    先于 ``inference_base_url``），外层多一条"该 agent 显式声明优先"：

    * ``config`` ⇒ ``config.yaml`` 的 ``providers.<name>.base_url``；
    * ``env`` ⇒ registry 声明的 ``base_url_env_var`` 在该 agent 视角下的取值；
    * ``builtin`` ⇒ registry 的 ``inference_base_url``（兜底，永远有值或空串，绝不静默用空值掩盖）。

    ``env_values`` 由调用方给（``_env_map_for(agent)`` 的结果），避免逐行重复读 ``.env``。
    """
    env_var = str(entry.get("base_url_env_var") or "").strip()
    declared_url = str(block.get("base_url") or "").strip()
    env_url = str(env_values.get(env_var) or "").strip() if env_var else ""
    if declared_url:
        return declared_url, "config", "config.yaml 的 providers.base_url", env_var
    if env_url:
        return env_url, "env", env_var, env_var
    return str(entry.get("base_url") or "").strip(), "builtin", "内置 registry 的 inference_base_url", env_var


def _provider_endpoint(agent: str, provider: str) -> Dict[str, Any]:
    """该 agent 视角下 provider 的出站端点：base_url / key_env（变量名）/ 候选变量名 / auth_type。

    **base_url 解析顺序与运行时同序**（``hermes_cli/runtime_provider.py`` 的
    ``_explicit_api_key_provider``：先读 ``base_url_env_var``，空了才回退 ``inference_base_url``），
    只比运行时多一层"该 agent 自己的声明优先"：

    1. ``config.yaml`` 的 ``providers.<name>.base_url``（该 agent 显式写死的 ⇒ ``base_url_source=config``）
    2. 内置 registry 的 ``base_url_env_var`` 在该 agent 视角下的取值（``base_url_source=env``）
       —— 取值走 ``_env_map_for(agent)``：进程环境 → home 的 ``.env`` → agent 自己的 ``.env``；
    3. 内置 registry 的 ``inference_base_url``（兜底 ⇒ ``base_url_source=builtin``）。

    因此「env 里配了地址、config.yaml 没声明」的 provider（如 ``openai-api`` / ``anthropic`` /
    ``xai`` / ``deepseek`` / ``minimax``）探测的不再是官方默认域名，而是中转站地址。
    """
    cfg = _read_config(agent)
    declared = _providers_block(cfg)
    block = declared.get(provider) or {}
    builtin = _builtin_registry()
    entry = builtin.get(provider) or builtin.get(provider.lower()) or {}
    base_url, base_url_source, base_url_origin, env_var = _resolve_base_url(
        block, entry, _env_map_for(agent))
    key_env = str(block.get("key_env") or block.get("api_key_env") or "").strip()
    candidates = [key_env] if key_env else [str(x).strip() for x in (entry.get("key_envs") or ()) if str(x).strip()]
    auth_type = str(entry.get("auth_type") or ("api_key" if candidates else "")).strip()
    return {
        "provider": provider,
        "base_url": base_url,
        "base_url_source": base_url_source,
        "base_url_origin": base_url_origin,
        "base_url_env_var": env_var,
        "key_env": key_env,
        "key_envs": candidates,
        "auth_type": auth_type,
        "declared_in_config": provider in declared,
        "source": "config" if provider in declared else ("builtin" if entry else ""),
    }


def _live_models(agent: str, provider: str, *, force: bool = False) -> Dict[str, Any]:
    """向该 agent 视角下 provider 的 ``/models`` 发一次**真实请求**（短 TTL 缓存）。

    返回结构（**失败也是这个结构**，绝不抛、绝不静默给空列表）：

    ``provider`` / ``agent`` / ``url`` / ``ok`` / ``models`` / ``count`` /
    ``error``（含 provider 名与原因的原文）/ ``error_kind``（``no_base_url`` /
    ``no_credential`` / ``http_401`` / ``transport`` / ``bad_payload`` / ``empty_list``）/
    ``http_status`` / ``elapsed_ms`` / ``cached`` / ``fetched_at`` / ``ttl_seconds``。
    """
    endpoint = _provider_endpoint(agent, provider)
    base_url = endpoint["base_url"]
    out: Dict[str, Any] = {
        "provider": provider, "agent": agent, "url": "", "ok": False, "models": [], "count": 0,
        "error": "", "error_kind": "", "http_status": None, "elapsed_ms": None,
        "cached": False, "fetched_at": "", "ttl_seconds": LIVE_TTL_SECONDS,
        "key_env": endpoint["key_env"], "key_envs": endpoint["key_envs"],
        "base_url": base_url, "auth_type": endpoint["auth_type"],
        # 来源标记：界面/排障一眼看出这次请求的地址是 config 写死的、env 给的、还是内置兜底。
        "base_url_source": endpoint.get("base_url_source", ""),
        "base_url_origin": endpoint.get("base_url_origin", ""),
        "base_url_env_var": endpoint.get("base_url_env_var", ""),
    }
    key = (str(_home()), str(agent).lower(), str(provider).lower())
    if not force:
        with _LIVE_LOCK:
            entry = _LIVE_CACHE.get(key)
        if entry and (time.time() - float(entry.get("at") or 0)) < float(entry.get("ttl") or LIVE_TTL_SECONDS):
            payload = dict(entry.get("payload") or {})
            # 命中缓存 = **这次没出网**：elapsed_ms 必须是 null（否则界面会把上次的耗时
            # 当成本次请求耗时），上次的真实耗时另存 ``last_elapsed_ms``。
            payload["last_elapsed_ms"] = payload.get("elapsed_ms")
            payload["elapsed_ms"] = None
            payload["cached"] = True
            payload["cache_age_s"] = round(time.time() - float(entry.get("at") or 0), 3)
            return payload

    if not base_url or not base_url.lower().startswith(("http://", "https://")):
        out.update(error_kind="no_base_url", url=base_url or "",
                   error=("provider %s 没有可请求的 base_url（当前 %r）：它是本地标记（如 moa://local）"
                          "或还没在 config.yaml 里声明。" % (provider, base_url or "")))
        return out

    url = _models_endpoint_url(base_url, provider)
    out["url"] = url
    token = _agent_secret(agent, endpoint["key_env"]) if endpoint["key_env"] else ""
    if not token and not endpoint["key_env"]:
        for name in endpoint["key_envs"]:
            token = _agent_secret(agent, name)
            if token:
                out["key_env"] = name
                break
    if not token and endpoint["auth_type"] in ("api_key", "oauth_device_code") and endpoint["key_envs"]:
        out.update(error_kind="no_credential",
                   error=("provider %s 在该 agent 下没有可用凭据：%s 都没有取值（未发起请求）。"
                          "请在 %s 的 .env 里配置其中一个。" %
                          (provider, "、".join(endpoint["key_envs"]), agent)))
        logger.warning("hermes-console.api_models: 实时拉取跳过 provider=%s agent=%s（无凭据）",
                       provider, agent)
        return out

    import urllib.error
    import urllib.request

    started = time.time()
    raw = ""
    status: Any = None
    try:
        request = urllib.request.Request(url, headers=_provider_headers(url, token), method="GET")
        with urllib.request.urlopen(request, timeout=LIVE_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", 200) or 200)
            raw = response.read(4_000_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(400).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 读不到 body 就算了
            detail = ""
        hint = ""
        if int(exc.code) in (401, 403) and not token:
            hint = "（该 agent 没有可用凭据：%s 都没配）" % ("、".join(endpoint["key_envs"]) or "key_env 未声明")
        out.update(error_kind="http_%d" % int(exc.code), http_status=int(exc.code),
                   error="provider %s 的 %s 返回 HTTP %d%s：%s" %
                         (provider, url, exc.code, hint, _sanitize_text(detail)[:160]))
    except Exception as exc:  # noqa: BLE001 - 连接/超时/DNS 都归到这里，原文如实带出
        out.update(error_kind="transport",
                   error="provider %s 的 %s 请求失败：%s: %s" %
                         (provider, url, type(exc).__name__, _sanitize_text(str(exc))[:160]))
    else:
        names, parse_error = _parse_model_payload(raw)
        if parse_error:
            out.update(error_kind="bad_payload", http_status=status,
                       error="provider %s 的 %s 返回的不是模型清单：%s" % (provider, url, parse_error))
        elif not names:
            out.update(error_kind="empty_list", http_status=status,
                       error="provider %s 的 %s 返回了 0 个模型（HTTP %s）" % (provider, url, status))
        else:
            out.update(ok=True, models=names, count=len(names), http_status=status,
                       fetched_at=datetime.now().isoformat(timespec="seconds"))
    out["elapsed_ms"] = int((time.time() - started) * 1000)

    if out["ok"]:
        logger.info("hermes-console.api_models: 实时拉取 provider=%s agent=%s url=%s -> %d 个模型（%sms, cached=False）",
                    provider, agent, url, out["count"], out["elapsed_ms"])
    else:
        logger.warning("hermes-console.api_models: 实时拉取失败 provider=%s agent=%s url=%s kind=%s error=%s",
                       provider, agent, url or "-", out["error_kind"], out["error"])
    # 缓存：成功存 ``LIVE_TTL_SECONDS``，失败只存 ``LIVE_FAIL_TTL_SECONDS``（短得多）。
    # 「缓存失败」不等于「回退旧列表」：缓存里存的就是这次失败本身（``models=[]`` + 明确
    # ``error``），命中时照原样再报一次错，绝不拿上一次成功的结果顶。
    # 点「刷新」（``force=True``）无条件绕过缓存重试。
    _LIVE_CACHE[key] = {
        "at": time.time(),
        "ttl": LIVE_TTL_SECONDS if out["ok"] else LIVE_FAIL_TTL_SECONDS,
        "payload": dict(out),
    }
    return out


def _config_models(block: Dict[str, Any]) -> List[str]:
    """该 provider 在 config.yaml 里**显式列出**的模型名（dict 或 list 两种写法都认）。"""
    models = block.get("models") if isinstance(block, dict) else None
    if isinstance(models, dict) and models:
        return sorted({str(key).strip() for key in models if str(key).strip()})
    if isinstance(models, (list, tuple)) and models:
        return sorted({str(item).strip() for item in models if str(item).strip()})
    return []


def _model_entries(config_models: List[str], live_models: List[str]) -> List[Dict[str, str]]:
    """逐条标注来源：``config``（配置文件里显式列出）/ ``live``（供应商实时返回）/ ``config+live``。"""
    out: List[Dict[str, str]] = []
    live_set = set(live_models)
    live_lower = {name.lower() for name in live_models}
    for name in config_models:
        out.append({"name": name, "source": "config+live" if (name in live_set or name.lower() in live_lower) else "config"})
    for name in live_models:
        if name in config_models:
            continue
        out.append({"name": name, "source": "live"})
    return out


def _model_list(agent: str, provider: str, *, live: bool, refresh: bool = False) -> Dict[str, Any]:
    """某 provider 的模型清单（**实时拉取**：真向 ``<base_url>/models`` 发请求）。

    * 默认 ``live=False``：只读该 agent ``config.yaml`` 里显式列出的模型，**一个字节都不出网**
      （探针 / 离线环境依赖这条，别改默认值）；
    * ``live=True``：真实拉取（短 TTL 缓存）；``refresh=True`` 无条件绕过缓存（手动刷新）；
    * **失败不静默**：``live.error`` 带 provider 名与原因原文，``live.ok=False``；
      绝不把上一次拉取的结果当这次的结果（缓存只存成功，失败不读旧值）；
    * 逐条来源：``model_entries`` 的 ``source`` ∈ ``config`` / ``live`` / ``config+live``，
      界面上不许把两者混成一个数字。
    """
    cfg = _read_config(agent)
    virtual = _virtual_lookup()
    if provider.lower() in virtual:
        return _virtual_model_list(agent, virtual[provider.lower()], cfg)
    block = _providers_block(cfg).get(provider, {})
    config_models = _config_models(block)
    endpoint = _provider_endpoint(agent, provider)
    live_info: Dict[str, Any] = {
        "requested": bool(live), "ok": False, "error": "", "error_kind": "", "url": "",
        "count": 0, "models": [], "cached": False, "fetched_at": "", "http_status": None,
        "elapsed_ms": None, "ttl_seconds": LIVE_TTL_SECONDS,
        "cache_age_s": None, "last_elapsed_ms": None,
        "base_url": endpoint.get("base_url", ""),
        "base_url_source": endpoint.get("base_url_source", ""),
        "base_url_origin": endpoint.get("base_url_origin", ""),
        "base_url_env_var": endpoint.get("base_url_env_var", ""),
    }
    live_models: List[str] = []
    if live:
        got = _live_models(agent, provider, force=bool(refresh))
        live_info.update({key: got.get(key) for key in live_info if key in got})
        live_info["requested"] = True
        if got.get("ok"):
            live_models = [str(name) for name in got.get("models") or []]

    entries = _model_entries(config_models, live_models)
    names = [entry["name"] for entry in entries]
    live_ok = bool(live and live_info["ok"])
    if live_ok:
        source = "config+remote" if config_models else "remote"
    else:
        source = "config" if config_models else "none"
    return {
        "agent": agent,
        "provider": provider,
        "count": len(names),
        "models": names,
        "model_entries": entries,
        "config_models": config_models,
        "config_count": len(config_models),
        "live_models": live_models,
        "live_count": len(live_models),
        "source": source,
        "source_label": {
            "config": "配置文件里显式列出的模型名",
            "remote": "供应商实时返回的模型清单",
            "config+remote": "配置文件里的 + 供应商实时返回的",
            "none": "还没有任何来源（配置文件里没列，实时也没拉到）",
        }.get(source, source),
        "default_model": str(block.get("model") or "").strip(),
        "base_url": _sanitize_text(str(endpoint.get("base_url") or "")),
        "base_url_source": endpoint.get("base_url_source", ""),
        "base_url_origin": endpoint.get("base_url_origin", ""),
        "base_url_env_var": endpoint.get("base_url_env_var", ""),
        "live_error": _sanitize_text(str(live_info.get("error") or "")),
        "live": {
            "requested": bool(live_info["requested"]),
            "ok": bool(live_info["ok"]),
            "error": _sanitize_text(str(live_info.get("error") or "")),
            "error_kind": str(live_info.get("error_kind") or ""),
            "url": _sanitize_text(str(live_info.get("url") or "")),
            "base_url": _sanitize_text(str(live_info.get("base_url") or "")),
            "base_url_source": str(live_info.get("base_url_source") or ""),
            "base_url_origin": str(live_info.get("base_url_origin") or ""),
            "base_url_env_var": str(live_info.get("base_url_env_var") or ""),
            "http_status": live_info.get("http_status"),
            "cached": bool(live_info.get("cached")),
            "fetched_at": str(live_info.get("fetched_at") or ""),
            "elapsed_ms": live_info.get("elapsed_ms"),
            "last_elapsed_ms": live_info.get("last_elapsed_ms"),
            "cache_age_s": live_info.get("cache_age_s"),
            "ttl_seconds": LIVE_TTL_SECONDS,
        },
        "note": ("" if not live else
                 ("实时拉取失败：%s" % live_info.get("error") if not live_info["ok"]
                  else "实时拉取成功%s" % ("（命中 %ds 缓存，点刷新可强制重取）" % int(LIVE_TTL_SECONDS)
                                        if live_info.get("cached") else ""))),
    }


# ---------------------------------------------------------------------------
# 快照：写前按字节复制、写后能字节还原
# ---------------------------------------------------------------------------
def _new_ts() -> str:
    """快照时间戳（秒级；同秒重复则追加 ``-01``/``-02``，绝不覆盖已有快照）。"""
    base = datetime.now().strftime("%Y%m%d-%H%M%S")
    if not (_snapshots_root() / base).exists():
        return base
    for index in range(1, 100):
        candidate = "%s-%02d" % (base, index)
        if not (_snapshots_root() / candidate).exists():
            return candidate
    return "%s-%s" % (base, int(time.time() * 1000) % 100000)


def _take_snapshot(agent: str, *, operation: str, note: str = "") -> Dict[str, Any]:
    """把该 agent 的 ``config.yaml`` **按字节**复制进快照目录，并写一份 manifest。

    返回 ``{"ts","dir","path","sha256","size"}``。任何一步失败 ⇒ 抛
    ``ApiError(500, snapshot_failed)``：**快照失败就不许写配置**（写坏了没得恢复）。

    为什么按字节复制而不是"存一份解析后的 model 块"：回滚要能恢复**整篇**文件
    （注释、别的段的 key、顺序），只存 model 块等于让回滚丢别的段。
    """
    import hashlib
    import shutil

    source = _config_path(agent)
    ts = _new_ts()
    target_dir = _snapshots_root() / ts
    target = target_dir / ("%s.yaml" % agent)
    try:
        if not source.is_file():
            raise ApiError(500, CODE_SNAPSHOT_FAILED,
                           "快照失败：%s 不存在（该 agent 还没有自己的 config.yaml，"
                           "无法保证写后可恢复）" % source, agent=agent)
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        raw = target.read_bytes()
        if not raw:
            raise ApiError(500, CODE_SNAPSHOT_FAILED,
                           "快照失败：复制出来的快照是空文件（%s）" % target, agent=agent)
        digest = hashlib.sha256(raw).hexdigest()
        manifest = {
            "operation": operation,
            "agent": agent,
            "note": note,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source": str(source),
            "snapshot": str(target),
            "sha256": digest,
            "size": len(raw),
        }
        (target_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一翻成结构化 500，绝不半截写入
        raise ApiError(500, CODE_SNAPSHOT_FAILED,
                       "快照失败：%s: %s" % (type(exc).__name__, _sanitize_text(str(exc))),
                       agent=agent) from exc
    return {"ts": ts, "dir": str(target_dir), "path": str(target),
            "sha256": digest, "size": len(raw)}


def _list_snapshots() -> List[Dict[str, Any]]:
    """列出快照（按时间戳升序）。坏 manifest / 坏目录只跳过，不抛。"""
    root = _snapshots_root()
    out: List[Dict[str, Any]] = []
    try:
        entries = sorted([item for item in root.iterdir() if item.is_dir()], key=lambda p: p.name)
    except OSError:
        return out
    for directory in entries:
        files = sorted(directory.glob("*.yaml"))
        manifest: Dict[str, Any] = {}
        try:
            loaded = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            manifest = loaded if isinstance(loaded, dict) else {}
        except Exception:  # noqa: BLE001 - 清单坏了仍要能列出快照
            manifest = {}
        files_map: Dict[str, str] = {}
        sizes: Dict[str, int] = {}
        for item in files:
            try:
                files_map[item.stem] = str(item)
                sizes[item.stem] = item.stat().st_size
            except OSError:
                continue
        out.append({
            "ts": directory.name,
            "dir": str(directory),
            "profiles": [item.stem for item in files],
            "agents": [item.stem for item in files],
            "files": files_map,
            "sizes": sizes,
            "operation": manifest.get("operation"),
            "agent": manifest.get("agent"),
            "note": manifest.get("note"),
            "created_at": manifest.get("created_at"),
            "sha256": manifest.get("sha256"),
        })
    return out


def _resolve_snapshot(agent: str, raw: Optional[str]) -> Tuple[Path, str]:
    """把用户给的快照引用（时间戳 / 目录 / 文件路径）解析成**快照根目录下的**一个文件。

    防目录穿越：``resolve()`` 之后必须落在 :func:`_snapshots_root` 之下，否则 400。
    找不到 ⇒ 400 ``snapshot_not_found``（**不是 404**：卡片要求非法快照一律 400）。
    """
    text = "" if raw is None else str(raw).strip()
    if not text:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "缺少 snapshot（可用 POST /models/apply 响应里的 snapshot_path、"
                       "snapshot_ts，或 GET /models/snapshots 里的 ts）", agent=agent)
    root = _snapshots_root()
    try:
        root_resolved = root.resolve()
    except OSError as exc:
        raise ApiError(500, CODE_UPSTREAM_ERROR,
                       "快照根目录不可解析：%s" % _sanitize_text(str(exc))) from exc

    candidates: List[Path] = []
    raw_path = Path(text)
    if raw_path.is_absolute() or "/" in text or "\\" in text:
        candidates.append(raw_path)
    else:
        candidates.append(root / text / ("%s.yaml" % agent))
        candidates.append(root / text)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if root_resolved not in resolved.parents and resolved != root_resolved:
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "非法快照路径 %r：必须位于快照根目录 %s 之下"
                           % (text, root_resolved), agent=agent)
        if resolved.is_file() and resolved.suffix == ".yaml":
            return resolved, resolved.stem
        if resolved.is_dir():
            inside = resolved / ("%s.yaml" % agent)
            if inside.is_file():
                return inside.resolve(), resolved.name
    raise ApiError(400, CODE_NOT_FOUND,
                   "快照不存在：%s（快照根目录 %s；用 GET /models/snapshots 看现有快照）"
                   % (text, root_resolved), agent=agent,
                   snapshots=[item["ts"] for item in _list_snapshots()])


# ---------------------------------------------------------------------------
# 写：官方 profile 作用域 + 官方原子写入器 + 回读校验
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _profile_scope(agent: str):
    """官方 profile 作用域（``_config_profile_scope``），产出**作用域内的** ``config.yaml`` 路径。

    * ``default`` ⇒ 传 ``None``（不切作用域，写 home 根的 config.yaml）；
    * 其它 agent ⇒ 传该 profile 名，官方作用域会把 ``get_config_path()`` 指到
      ``<home>/profiles/<agent>/config.yaml``（本函数返回的就是它，避免"我算的路径"和
      "官方写的路径"是两份口径）；
    * 作用域进不去（宿主版本差异 / profile 目录异常）⇒ 记一条 warning，退回
      ``core.scope.agent_config_path(agent)``。**写入器仍然是官方的**，只是少了密钥作用域。

    手工 ``__enter__/__exit__`` 而不是 ``with``：需要"进入失败就换路径继续"，
    用 ``with`` 只能二者选一。
    """
    from hermes_cli.config import get_config_path  # 官方路径解析（受作用域影响）

    handle = None
    target: Optional[Path] = None
    try:
        from hermes_cli.web_server_profiles import _config_profile_scope  # 官方 profile 作用域

        handle = _config_profile_scope(None if agent == DEFAULT_AGENT else agent)
        handle.__enter__()
        target = Path(get_config_path())
    except Exception as exc:  # noqa: BLE001 - 作用域不可用只降级，不影响写入正确性
        logger.warning("hermes-console.api_models: profile 作用域不可用（agent=%s），"
                       "退回按 home 推路径：%s: %s", agent, type(exc).__name__, exc)
        if handle is not None:
            try:
                handle.__exit__(type(exc), exc, exc.__traceback__)
            except Exception:  # noqa: BLE001 - 退出失败只记日志
                logger.debug("hermes-console.api_models: 作用域退出失败", exc_info=True)
            handle = None
        target = None
    if target is None:
        target = _config_path(agent)
    try:
        yield target
    finally:
        if handle is not None:
            try:
                handle.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.warning("hermes-console.api_models: profile 作用域退出失败", exc_info=True)


def _read_for_write(path: Path) -> Dict[str, Any]:
    """写入前的**失败关闭**原始读（官方 ``require_readable_config_before_write``）。

    读不到 / 解析不了 / 根不是 mapping ⇒ 直接拒写（否则"读成空 → 写回空"会把整个
    配置文件清空）。缺文件是合法首启状态，返回 ``{}``。
    """
    from hermes_cli.config import require_readable_config_before_write

    data = require_readable_config_before_write(path)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ApiError(500, CODE_UPSTREAM_ERROR,
                       "%s 的根不是 mapping（%s），拒绝写入" % (path, type(data).__name__))
    return data


def _write_official(path: Path, data: Dict[str, Any], *, allow_deletions: Sequence[str] = ()) -> None:
    """官方原子写：保留注释、拒绝"缺键即删"的整篇覆盖。

    ``allow_deletions`` 只放**本函数调用方显式要删**的点分路径（例如切到虚拟 provider
    ``moa`` 时删掉遗留的 ``model.key_env``）。流程是：

    1. 先走 ``atomic_config_write``（默认路径，任何缺键都会被它拒绝）；
    2. 只有它因为 "omitted config paths" 拒绝、且 ``allow_deletions`` 非空时才继续：
       重新按官方口径算出**被省略的路径全集**，逐条核对是否都在 ``allow_deletions`` 里，
       有一条不在就**原样抛出**（绝不拿一次 replace 赌运气）；
    3. 全部核对通过才用 ``atomic_config_replace`` 写这份**完整** doc（它和磁盘上的
       现状只差"目标段里被显式删掉的那几个键"），注释与别的段照样保留。

    换句话说：**整篇覆盖只在"我确实要删这几个键，且省略的正好就是这几个键"时发生**，
    其余情况一律失败关闭。
    """
    from hermes_cli.config import atomic_config_write

    try:
        atomic_config_write(path, data)
        return
    except Exception as exc:  # noqa: BLE001 - 先按官方默认路径试，失败再判断能不能安全降级
        allowed = {str(item).strip() for item in (allow_deletions or ()) if str(item).strip()}
        if not allowed or "omitted config paths" not in str(exc):
            raise
        try:
            from hermes_cli.config import (  # 官方工具：省略路径算法与全量写入器
                _omitted_config_paths,
                atomic_config_replace,
                require_readable_config_before_write,
            )
        except Exception:  # noqa: BLE001 - 宿主没有这些私有工具时，保持原来的拒绝（失败关闭）
            raise exc

        existing = require_readable_config_before_write(path)
        omitted = [str(item) for item in _omitted_config_paths(existing, data)]
        unexpected = [item for item in omitted if item not in allowed]
        if unexpected:
            raise exc
        atomic_config_replace(path, data)


def _sha256_file(path: Path) -> str:
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _restore_bytes(snapshot_file: Path, target: Path) -> None:
    """把快照**按字节**盖回目标文件（回滚与"写后校验不符的自动恢复"共用这一条路径）。"""
    import shutil

    if not snapshot_file.is_file():
        raise ApiError(500, CODE_UPSTREAM_ERROR,
                       "回滚失败：快照文件不存在 %s" % snapshot_file)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot_file, target)
    except OSError as exc:
        raise ApiError(500, CODE_UPSTREAM_ERROR,
                       "回滚失败：%s: %s" % (type(exc).__name__, _sanitize_text(str(exc)))) from exc


# ===========================================================================
# 读路由
# ===========================================================================
def _overview_row(agent: str) -> Dict[str, Any]:
    """一个 agent 的模型概览行（只读，**逐行失败隔离**：某行读炸不影响别的行）。"""
    labels = _labels()
    try:
        cfg = _read_config(agent)
        state = _current_state(agent)
        declared = _providers_block(cfg)
        missing: List[str] = []
        if not state["provider"]:
            missing.append("model.provider")
        if not state["model"]:
            missing.append("model.default")
        env_names = _env_names(agent)
        key_env = state["key_env"]
        return {
            "agent": agent,
            "profile": agent,
            "label": labels.get(agent, agent),
            "config_path": str(_config_path(agent)),
            "config_exists": _config_path(agent).is_file(),
            "model": state["model"],
            "provider": state["provider"],
            "base_url": _sanitize_text(state["base_url"]),
            "api_mode": state["api_mode"],
            "key_env": key_env,
            "key_present": bool(key_env) and key_env in env_names,
            "current": {
                "provider": state["provider"],
                "model": state["model"],
                "base_url": _sanitize_text(state["base_url"]),
                "api_mode": state["api_mode"],
                "key_env": key_env,
            },
            "fallbacks": state["fallbacks"],
            "fallback_count": len(state["fallbacks"]),
            "auxiliary": _auxiliary_summary(cfg),
            # 辅助槽位的**读写契约**（动态生成：槽位来自配置本身；每行的
            # writable_fields 就是 scope=auxiliary 这次能写的字段名）。
            "auxiliary_slots": _auxiliary_slots(cfg),
            "reasoning_effort": _reasoning_effort(cfg),
            # ★ 思考强度现在是**可写**的（scope=reasoning），响应里必须说清楚：
            # 字段路径、当前值、合法取值、请求体该带哪个键。界面不用猜契约。
            "reasoning_effort_writable": True,
            "reasoning": {
                "field": REASONING_EFFORT_PATH,
                "scope": "reasoning",
                "request_key": "reasoning_effort",
                "value": _reasoning_effort(cfg),
                "writable": True,
                "allowed_values": list(_reasoning_values()),
                "off_value": REASONING_EFFORT_OFF,
                "off_semantics": _reasoning_off_semantics(),
                "apply_endpoint": "POST /models/apply",
                # per-model 覆盖 > 全局：这里给只读视图，界面必须显示"有覆盖的模型
                # 不受全局值影响"，否则用户会以为改了没生效。
                "overrides_field": REASONING_OVERRIDES_PATH,
                "overrides": _reasoning_overrides(cfg),
                "overrides_writable": False,
            },
            # 本行**可写字段**的点分路径清单（思考强度在里面；旧四段的字段照旧走
            # scope=model / fallback / fallbacks / auxiliary）。
            "writable_fields": [
                "model.provider", "model.default", "model.base_url", "model.api_mode",
                "fallback_providers", "auxiliary.<slot>.<field>", REASONING_EFFORT_PATH,
            ],
            "provider_count": len(declared),
            "providers": sorted(declared.keys()),
            "missing": missing,
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001 - 单行失败不拖垮整表（旧插件同款）
        return {
            "agent": agent,
            "profile": agent,
            "label": labels.get(agent, agent),
            "config_path": str(_config_path(agent)),
            "config_exists": _config_path(agent).is_file(),
            "model": "",
            "provider": "",
            "error": "%s: %s" % (type(exc).__name__, _sanitize_text(str(exc))),
            "missing": ["read_error"],
        }


@router.get("/overview")
@_api
def overview(agent: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """当前模型概览（**只读**，无任何写入）。

    * 不给 ``agent`` ⇒ 列全部 agent（每次请求现扫 ``profiles/``，新增 agent 立刻出现）；
    * 给 ``agent`` ⇒ 只列那一个（不存在 ⇒ 404，非法文法 ⇒ 400）。

    每行至少含 ``agent`` / ``label`` / ``config_path`` / ``model`` / ``provider`` /
    ``base_url`` / ``api_mode`` / ``key_env`` / ``key_present`` / ``fallbacks`` / ``missing``。
    """
    rows: List[Dict[str, Any]]
    if agent is None or not str(agent).strip():
        rows = [_overview_row(item) for item in _agent_ids()]
        selected = None
    else:
        selected = _require_agent(agent, write=False)
        rows = [_overview_row(selected)]
    return {
        "ok": True,
        "read_only": True,
        "agent": selected,
        "count": len(rows),
        "agents": rows,
        "rows": rows,
        "scopes": list(SCOPES),
        # 思考强度的合法取值（关闭 + 全部等级，共 8 个）：界面下拉直接照这份渲染，
        # 与写入口的校验是同一个函数（``_reasoning_values``），不会两边不一致。
        "reasoning_effort_values": list(_reasoning_values()),
        "reasoning_effort_field": REASONING_EFFORT_PATH,
        "reasoning_effort_writable": True,
        "hermes_home": str(_home()),
        "note": "只读概览。写操作只有 POST /models/apply 与 POST /models/rollback，"
                "且一次只作用于一个 agent。思考强度走 scope=reasoning"
                "（请求体带 reasoning_effort；缺这个键 ⇒ 零写请求）。",
    }


@router.get("/health")
@_api
def health() -> Dict[str, Any]:
    """轻量自检：数据源可读、官方写入器可见、快照/预设目录可写、provider registry 条数。"""
    checks: Dict[str, Any] = {}
    agents = _agent_ids()
    checks["agents"] = {"count": len(agents), "ids": agents}

    scope_block: Dict[str, Any] = {}
    try:
        entries = _agent_entries()
        scope_block["ok"] = bool(entries)
        scope_block["with_config"] = sum(1 for item in entries if item.get("exists"))
    except Exception as exc:  # noqa: BLE001
        scope_block = {"ok": False, "error": "%s: %s" % (type(exc).__name__, _sanitize_text(str(exc)))}
    checks["core_scope"] = scope_block

    writer_block: Dict[str, Any] = {}
    try:
        from hermes_cli.config import atomic_config_write, read_config_doc  # noqa: F401
        from hermes_cli.web_server_profiles import _config_profile_scope  # noqa: F401

        writer_block = {"ok": True, "writer": "hermes_cli.config.atomic_config_write",
                        "scope": "hermes_cli.web_server_profiles._config_profile_scope"}
    except Exception as exc:  # noqa: BLE001
        writer_block = {"ok": False, "error": "%s: %s" % (type(exc).__name__, _sanitize_text(str(exc)))}
    checks["official_writer"] = writer_block

    snap_root = _snapshots_root()
    # **只读探活**：不 mkdir（一个 GET 不该在真实目录里造出任何东西）。
    # 可写性看最近的存在祖先目录的权限位，够回答问题又零副作用。
    writable = False
    probe_path = snap_root
    try:
        while not probe_path.exists() and probe_path != probe_path.parent:
            probe_path = probe_path.parent
        writable = probe_path.is_dir() and os.access(probe_path, os.W_OK)
    except OSError:
        writable = False
    checks["snapshots"] = {"root": str(snap_root), "exists": snap_root.is_dir(),
                           "writable": writable, "count": len(_list_snapshots())}
    checks["presets"] = {"path": str(_presets_path()), "exists": _presets_path().is_file()}

    registry = _builtin_registry()
    checks["provider_registry"] = {"ok": bool(registry), "count": len(registry)}

    return {
        "ok": all(bool(block.get("ok", True)) for block in checks.values() if isinstance(block, dict)),
        "read_only": False,
        "hermes_home": str(_home()),
        "scopes": list(SCOPES),
        "write_path": "hermes_cli.config.atomic_config_write（官方原子写入器，保留注释）"
                      " + _config_profile_scope(agent) 作用域",
        "apply_requires_confirm": True,
        "multi_agent_forbidden": True,
        "checks": checks,
    }


@router.get("/providers")
@_api
def providers(agent: str = Query(default=DEFAULT_AGENT),
              all: str = Query(default="0")) -> Dict[str, Any]:
    """某 agent 视角的 provider 目录（**凭据脱敏**）。

    查询参数：
    * ``agent``：以哪个 agent 的配置/``.env`` 判定可用性（默认 ``default``）；
    * ``all``：``1/true/yes/on`` ⇒ 连内置 registry 里当前不可用的也列出来（``configured=false``）。

    响应里**不含任何密钥取值**：只有 ``key_env``（变量名）、``key_present``（布尔）与
    ``credentials``（``mask_sensitive`` 打码后的 ``***``）。
    """
    target = _require_agent(agent, write=False)
    include_all = str(all).strip().lower() in ("1", "true", "yes", "on", "y")
    rows = _provider_rows(target, include_all=include_all)
    state = _current_state(target)
    return {
        "ok": True,
        "agent": target,
        "profile": target,
        "label": _labels().get(target, target),
        "show_all": include_all,
        "count": len(rows),
        "current": {"provider": state["provider"], "model": state["model"]},
        "providers": rows,
        "hermes_home": str(_home()),
        "note": "默认只列**真的能用**的 provider（config.yaml 里声明过，或内置 registry 且 key_env 有值）。"
                "加 ?all=1 看全部。密钥只以变量名 + 布尔出现，取值一律 ***。",
    }


@router.get("/providers/{agent}")
@_api
def providers_for_agent(agent: str, all: str = Query(default="0")) -> Dict[str, Any]:
    """同上，agent 走路径（前端下拉切换视角时用）。"""
    return providers(agent=agent, all=all)


@router.get("/models")
@_api
def models(provider: str = Query(default=""),
           agent: str = Query(default=DEFAULT_AGENT),
           live: str = Query(default="0"),
           refresh: str = Query(default="0")) -> Dict[str, Any]:
    """某 provider 的模型清单（**实时拉取**，唯一实现点）。

    查询参数：

    * ``provider`` 必填（缺失 / 不认识 ⇒ 400 ``invalid_argument``，带可选列表）；
    * ``live=1``：真向该 provider 的 ``<base_url>/models`` 发请求（短 TTL 缓存）；
    * ``refresh=1``：手动刷新 —— 隐含 ``live=1`` 且**绕过缓存**；
    * 默认 ``live=0``：只读该 agent ``config.yaml`` 里显式列出的模型，**不出网**
      （离线环境 / 探针依赖这个默认值，不要改成默认联网）。

    响应里 ``source`` ∈ ``config`` / ``remote`` / ``config+remote`` / ``none``，
    ``model_entries`` 逐条标注 ``config`` / ``live`` / ``config+live``，
    ``config_count`` 与 ``live_count`` **分开给**（不许混成一个数字）。
    实时拉取失败时 ``live.ok=false`` 且 ``live.error`` 带 provider 名与原因原文 ——
    **不回退旧结果、不给没有解释的空列表**。
    """
    target = _require_agent(agent, write=False)
    name = _require_provider(target, provider)
    force = _flag(refresh)
    use_live = force or _flag(live)
    payload = _model_list(target, name, live=use_live, refresh=force)
    return {"ok": True, **payload}


def model_pair_error(agent: str, provider: Any, model: Any,
                     *, allow_unlisted: bool = False) -> Optional[str]:
    """``provider → model`` 组合的**可读拒绝原因**（``None`` = 没问题）。

    给别的域模块（``api_advisor`` / ``api_autotitler``）复用：它们的写入口不是
    ``scope=auxiliary``，但同样只该接受"这个 provider 下真的有的模型"。
    本函数**从不抛异常**：读不到清单 / provider 不认识 ⇒ 返回 ``None``（fail-open，
    宁可放过也不让面板因为读不到宿主而整段不可用）。
    """
    provider_text = str(provider or "").strip()
    model_text = str(model or "").strip()
    if not provider_text or not model_text or allow_unlisted:
        return None
    if provider_text.lower() == "auto":
        return None
    names = _model_catalog(agent, provider_text)
    if not names:
        return None
    if model_text in names or model_text.lower() in {name.lower() for name in names}:
        return None
    return ("模型 %r 不属于 provider %r：该 provider 在 agent %s 下的可用模型是 %s%s。"
            "要用手填的清单外模型，请走「高级：手填模型名」（请求体带 "
            "allow_unlisted_model=true）"
            % (model_text[:80], provider_text, agent, "、".join(names[:20]),
               "（还有 %d 个）" % (len(names) - 20) if len(names) > 20 else ""))


#: 宿主 ``web_server_config._AUX_TASK_SLOTS`` 的**降级副本**（只在宿主不可用时用）。
AUX_TASK_SLOTS_FALLBACK = ("vision", "compression", "skills_hub", "approval", "mcp",
                           "title_generation", "review", "voice_chat", "triage_specifier",
                           "kanban_decomposer", "profile_describer", "curator")

#: 槽位 → 人可读模块名。**只用于显示**；槽位清单本身来自宿主常量 / 该 agent 的配置。
AUX_SLOT_LABELS = {
    "vision": "视觉（看图 / 图像理解）",
    "compression": "上下文压缩（压缩摘要）",
    "skills_hub": "技能库（搜索 / 安装建议）",
    "approval": "命令审批（smart approval 分类器）",
    "mcp": "MCP（工具描述 / 选择）",
    "title_generation": "自动命名（会话标题）",
    "review": "/review 评审子代理",
    "voice_chat": "语音对话",
    "triage_specifier": "看板细化（triage specifier）",
    "kanban_decomposer": "看板分解（decomposer）",
    "profile_describer": "Profile 描述生成",
    "curator": "技能策展（curator）",
}

#: 配置里还在、但宿主**已不再调用模型**的槽位（残留块被忽略）。
IGNORED_AUX_SLOTS = {
    "web_extract": "宿主 hermes_cli/config_defaults.py 明写 web_extract / session_search "
                   "不再用 aux LLM，配置里的残留块会被忽略（没有任何模型调用可配）",
    "session_search": "同上：session_search 的 aux 块已被宿主忽略",
}


def _host_aux_slots() -> List[str]:
    """宿主认的 aux 槽位顺序（``web_server_config._AUX_TASK_SLOTS``）；读不到用降级副本。"""
    try:
        from hermes_cli.web_server_config import _AUX_TASK_SLOTS  # type: ignore

        names = [str(item).strip() for item in _AUX_TASK_SLOTS if str(item).strip()]
        if names:
            return names
    except Exception as exc:  # noqa: BLE001 - 宿主缺失只降级
        logger.debug("hermes-console.api_models: _AUX_TASK_SLOTS 不可用：%s", exc)
    return list(AUX_TASK_SLOTS_FALLBACK)


def _host_aux_defaults() -> Dict[str, Dict[str, Any]]:
    """宿主 ``DEFAULT_CONFIG["auxiliary"]``（模块内置默认值）；读不到 ⇒ ``{}``。"""
    try:
        from hermes_cli.config_defaults import DEFAULT_CONFIG  # type: ignore

        block = (DEFAULT_CONFIG or {}).get("auxiliary")
        if isinstance(block, dict):
            return {str(key): dict(value) for key, value in block.items()
                    if isinstance(value, dict)}
    except Exception as exc:  # noqa: BLE001
        logger.debug("hermes-console.api_models: DEFAULT_CONFIG 不可用：%s", exc)
    return {}


def _aux_usage_row(agent: str, slot: str, cfg: Dict[str, Any],
                   defaults: Dict[str, Dict[str, Any]], main_state: Dict[str, str]
                   ) -> Dict[str, Any]:
    """一个辅助槽位的**模块盘点行**：当前值 + 来源层 + 解析后真值 + 选择器契约。

    来源层（三选一，界面文案就用 ``source_label``）：

    * ``agent`` —— 该 agent 的 ``config.yaml`` 显式声明了这个槽位且 provider 不是 ``auto``；
    * ``global`` —— provider 是 ``auto``（**跟随主模型 / Hermes 全局配置**）；
    * ``module_default`` —— 该 agent 没声明这个槽位，取宿主 ``DEFAULT_CONFIG`` 的内置默认。
    """
    block = _auxiliary_block(cfg)
    declared = slot in block and isinstance(block.get(slot), dict)
    item = dict(block.get(slot) or {}) if declared else dict(defaults.get(slot) or {})
    provider = str(item.get("provider") or "").strip()
    model = str(item.get("model") or "").strip()
    # 来源层按**值从哪来**判，不按 provider 的取值猜：
    #   声明过 + provider 不是 auto ⇒ agent（该 Agent 独立配置）
    #   声明过 + provider == auto   ⇒ global（这个 agent 明确选了"跟随全局/主模型"）
    #   没声明                      ⇒ module_default（取宿主 DEFAULT_CONFIG 的内置默认）
    if not declared:
        source = "module_default"
    elif provider.lower() == "auto" or not provider:
        source = "global"
    else:
        source = "agent"
    follows_main = (not provider) or provider.lower() == "auto"
    if follows_main:
        resolved_provider = main_state.get("provider") or ""
        resolved_model = model or (main_state.get("model") or "")
    else:
        resolved_provider = provider
        resolved_model = model
    source_label = {
        "agent": "%s 独立配置（config.yaml 的 auxiliary.%s）" % (agent, slot),
        "global": "跟随 Hermes 全局 / 主模型（provider=auto）",
        "module_default": "模块默认（该 agent 未声明，取宿主内置默认%s）"
                          % ("：默认值就是 auto ⇒ 运行时跟随主模型" if follows_main else ""),
    }[source]
    catalog = _model_catalog(agent, provider) if provider and provider.lower() != "auto" else []
    return {
        "id": "aux." + slot,
        "slot": slot,
        "kind": "auxiliary",
        "label": AUX_SLOT_LABELS.get(slot, slot),
        "active": slot not in IGNORED_AUX_SLOTS,
        "inactive_reason": IGNORED_AUX_SLOTS.get(slot, ""),
        "declared_in_config": declared,
        "provider": provider,
        "model": model,
        "source": source,
        "source_label": source_label,
        "follows_main": follows_main,
        "resolved_provider": resolved_provider,
        "resolved_model": resolved_model,
        "agent_override": True,
        "writable_fields": _aux_writable_fields(item) if item else ["provider", "model"],
        "key_env": _sanitize_text(str(item.get("key_env") or "").strip()),
        "model_in_catalog": (None if not catalog or not model
                             else (model in catalog or model.lower() in {n.lower() for n in catalog})),
        "model_catalog_size": len(catalog),
        "picker": {
            "providers_endpoint": "/models/providers",
            "models_endpoint": "/models/models",
            "apply_endpoint": "POST /models/apply",
            "apply_scope": "auxiliary",
            "apply_slot": slot,
            "agent_param": "agent",
        },
    }


@router.get("/usage")
@_api
def usage(agent: str = Query(default=DEFAULT_AGENT)) -> Dict[str, Any]:
    """**模块盘点**（只读）：每个用模型的地方 → 槽位 / 当前值 / 来源层 / 是否支持 Agent 覆盖。

    为什么要有这个接口：老板要求"控制台里所有用到大模型的地方都能选 provider + model"，
    那就得先有一份**机器可读**的清单，界面照着它渲染、验收照着它逐行判。
    槽位清单来自宿主 ``web_server_config._AUX_TASK_SLOTS``（读不到才用降级副本），
    **不写死 8 个常量**；配置里还有、宿主已忽略的槽位（``web_extract``）标
    ``active=false`` 并写明原因。
    """
    target = _require_agent(agent, write=False)
    cfg = _read_config(target)
    state = _current_state(target)
    defaults = _host_aux_defaults()
    order = _host_aux_slots()
    declared = [name for name in _aux_slot_names(cfg)]
    slots: List[str] = []
    for name in list(order) + declared:
        if name not in slots:
            slots.append(name)
    rows = [_aux_usage_row(target, slot, cfg, defaults, state) for slot in slots]
    main_row = {
        "id": "main",
        "slot": "",
        "kind": "model",
        "label": "主模型（对话 / 工具调用）",
        "active": True,
        "inactive_reason": "",
        "declared_in_config": bool(state.get("provider")),
        "provider": state.get("provider") or "",
        "model": state.get("model") or "",
        "source": "agent",
        "source_label": "%s 的主模型（config.yaml 的 model:）" % target,
        "resolved_provider": state.get("provider") or "",
        "resolved_model": state.get("model") or "",
        "agent_override": True,
        "writable_fields": ["provider", "model", "base_url", "api_mode"],
        "key_env": state.get("key_env") or "",
        "model_in_catalog": None,
        "model_catalog_size": 0,
        "picker": {
            "providers_endpoint": "/models/providers",
            "models_endpoint": "/models/models",
            "apply_endpoint": "POST /models/apply",
            "apply_scope": "model",
            "apply_slot": "",
            "agent_param": "agent",
        },
    }
    return {
        "ok": True,
        "read_only": True,
        "agent": target,
        "count": len(rows) + 1,
        "modules": [main_row] + rows,
        "aux_slot_source": "hermes_cli.web_server_config._AUX_TASK_SLOTS",
        "aux_slot_order": slots,
        "ignored_slots": dict(IGNORED_AUX_SLOTS),
        "main": {"provider": main_row["provider"], "model": main_row["model"]},
        "note": "只读盘点。写入口仍是 POST /models/apply（scope=auxiliary 需 slot；"
                "scope=model 改主模型）。来源层三态：agent / global（provider=auto）/"
                "module_default（该 agent 未声明，取宿主内置默认）。",
    }


@router.get("/validate")
@_api
def validate(agent: str = Query(default=DEFAULT_AGENT),
             provider: str = Query(default=""),
             model: str = Query(default=""),
             allow_unlisted: str = Query(default="0")) -> Dict[str, Any]:
    """``provider + model`` 组合的**只读校验**（非法 ⇒ 400，报错可读）。

    界面在保存前调它（尤其 advisor / autotitler 这两条写入口不归本分区管），
    这样"非法组合"在**任何**模块里都不会走到写盘。与 ``scope=auxiliary`` 的写入校验
    共用 :func:`_model_value` 同一套规则，不会出现"面板说行、接口说不行"。
    """
    target = _require_agent(agent, write=False)
    name = _require_provider(target, provider)
    allow = str(allow_unlisted).strip().lower() in ("1", "true", "yes", "on", "y")
    names = _model_catalog(target, name)
    checked = _model_value(target, provider=name, raw=model, current="",
                           allow_unlisted=allow, where="provider %s" % name)
    return {
        "ok": True,
        "valid": True,
        "agent": target,
        "provider": name,
        "model": checked,
        "catalog_size": len(names),
        "catalog": names[:200],
        "unverified": bool(allow and names and checked not in names),
        "note": "空清单表示该 provider 在 config.yaml 里没有列出模型（无法校验，不拒写）。",
    }


@router.get("/snapshots")
@_api
def snapshots() -> Dict[str, Any]:
    """列出已有快照（按时间戳升序）。空目录是合法空态（``count=0``），不是错误。"""
    items = _list_snapshots()
    return {
        "ok": True,
        "snapshot_root": str(_snapshots_root()),
        "count": len(items),
        "snapshots": items,
        "note": "写操作（apply/rollback）会在写前把该 agent 的 config.yaml 按字节复制到这里；"
                "回滚只允许指向这里已有的快照。",
    }


@router.get("/model-library")
@_api
def model_library(agent: str = Query(default=DEFAULT_AGENT),
                  provider: str = Query(default=""),
                  live: str = Query(default="0"),
                  refresh: str = Query(default="0")) -> Dict[str, Any]:
    """模型库：每个 provider 的模型清单（**实时拉取**，逐条标注来源）。

    ``live=1`` 真拉上游（短 TTL 缓存，按 (agent, provider) 隔离）；``refresh=1`` 强制绕过缓存
    （手动刷新）。默认 ``live=0`` 只读配置、一个字节都不出网。

    失败的 provider **不会**被静默吞掉：该组的 ``live.ok=false`` + ``live.error`` 带
    provider 名与原因原文，``models`` 只保留配置文件里显式列出的那些（并标 ``source=config``），
    整个响应仍 200（一个上游挂掉不该让整页不可用），但 ``errors`` 逐条列出失败项。
    """
    target = _require_agent(agent, write=False)
    force = _flag(refresh)
    use_live = force or _flag(live)
    rows = _provider_rows(target, include_all=True)
    if provider and str(provider).strip():
        wanted = str(provider).strip()
        rows = [row for row in rows if str(row.get("provider", "")).lower() == wanted.lower()]
        if not rows:
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "agent %s 的 provider 目录里没有 %r" % (target, wanted),
                           providers=_provider_names(target), agent=target)

    # 只有"真的可用"的 provider 才值得联网：``configured=False`` 的内置项（没声明、也探不到
    # key）与虚拟 / 本地标记（moa://local）都不发请求，直接给一条**显式跳过原因**
    # （``skip_reason``）—— 52 个内置 provider 逐一打外网既慢，也会把用不上的凭据发出去。
    todo: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for row in rows:
        if not use_live:
            skipped.append({"provider": row.get("provider"), "reason": "live_not_requested",
                            "detail": "本次没请求实时拉取（live=0 / refresh=0）"})
        elif row.get("kind") == "virtual" or not str(row.get("base_url") or "").lower().startswith(("http://", "https://")):
            skipped.append({"provider": row.get("provider"), "reason": "local_no_http_endpoint",
                            "detail": "base_url 是本地标记（如 moa://local）或为空：本来就不该联网，"
                                      "它的\"模型\"由本地配方决定"})
        elif row.get("configured") is False and not row.get("declared_in_config"):
            skipped.append({"provider": row.get("provider"), "reason": "not_configured",
                            "detail": "内置 registry 里有，但该 agent 既没在 config.yaml 声明、也探不到凭据"})
        else:
            todo.append(row)
    results: Dict[str, Dict[str, Any]] = {}

    def _one(row: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        name = str(row.get("provider") or "")
        endpoint = _provider_endpoint(target, name)
        if not endpoint["base_url"].lower().startswith(("http://", "https://")):
            return name, {"ok": False, "models": [], "count": 0, "cached": False, "fetched_at": "",
                          "url": "", "http_status": None, "elapsed_ms": None,
                          "ttl_seconds": LIVE_TTL_SECONDS, "error_kind": "no_base_url",
                          "error": "provider %s 没有可请求的 base_url（%r）：本地标记或还没声明 base_url。"
                                   % (name, endpoint["base_url"] or "")}
        token_needed = bool(endpoint["key_envs"]) and endpoint["auth_type"] in ("api_key", "oauth_device_code")
        if token_needed and not any(_agent_secret(target, env) for env in ([endpoint["key_env"]] if endpoint["key_env"] else endpoint["key_envs"])):
            return name, {"ok": False, "models": [], "count": 0, "cached": False, "fetched_at": "",
                          "url": _models_endpoint_url(endpoint["base_url"], name), "http_status": None,
                          "elapsed_ms": None, "ttl_seconds": LIVE_TTL_SECONDS, "error_kind": "no_credential",
                          "error": "provider %s 在该 agent 下没有可用凭据：%s 都没有取值（未发起请求）。"
                                   % (name, "、".join(endpoint["key_envs"]))}
        return name, _live_models(target, name, force=force)

    if todo:
        workers = max(1, min(6, len(todo)))
        with futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for name, got in pool.map(_one, todo):
                results[name] = got

    groups: List[Dict[str, Any]] = []
    config_total = 0
    live_total = 0
    errors: List[Dict[str, Any]] = []
    skip_map = {str(item.get("provider") or "").lower(): item for item in skipped}
    for row in rows:
        name = str(row["provider"])
        payload = _model_list(target, name, live=False)
        live_info = results.get(name)
        if live_info is None:
            live_info = {"ok": False, "error": "", "error_kind": "", "url": "", "count": 0,
                         "models": [], "cached": False, "fetched_at": "", "http_status": None,
                         "elapsed_ms": None, "ttl_seconds": LIVE_TTL_SECONDS}
        live_models = [str(item) for item in (live_info.get("models") or [])] if live_info.get("ok") else []
        entries = payload.get("model_entries") or []
        if live_info.get("ok"):
            entries = _model_entries(payload.get("config_models") or [], live_models)
        names = [entry["name"] for entry in entries]
        source = payload.get("source") or "none"
        if live_info.get("ok") and live_models:
            source = "config+remote" if payload.get("config_models") else "remote"
        config_total += int(payload.get("config_count") or 0)
        live_total += len(live_models)
        skip = skip_map.get(name.lower()) or {}
        if live_info.get("error"):
            errors.append({"provider": name, "error": live_info["error"],
                           "error_kind": live_info.get("error_kind") or ""})
        groups.append({
            "provider": name,
            "label": row["label"],
            "base_url": row["base_url"],
            # 来源标记（config / env / builtin）：排障时一眼看出这个物理地址是谁给的。
            "base_url_source": row.get("base_url_source") or "",
            "base_url_env_var": row.get("base_url_env_var") or "",
            "configured": row.get("configured"),
            "current": row.get("current"),
            "key_env": row.get("key_env"),
            "key_present": row.get("key_present"),
            "source": source,
            "source_label": payload.get("source_label") or "",
            "provider_source": (row.get("source") or ""),
            "provider_source_label": (row.get("source_label") or ""),
            "models": names,
            "model_entries": entries,
            "model_count": len(names),
            "config_models": payload.get("config_models") or [],
            "config_count": int(payload.get("config_count") or 0),
            "live_models": live_models,
            "live_count": len(live_models),
            "default_model": payload.get("default_model"),
            "skipped_live": str(skip.get("reason") or ""),
            "skipped_reason": str(skip.get("detail") or ""),
            "live": {
                "requested": bool(use_live),
                "ok": bool(live_info.get("ok")),
                "error": _sanitize_text(str(live_info.get("error") or "")),
                "error_kind": str(live_info.get("error_kind") or ""),
                "url": _sanitize_text(str(live_info.get("url") or "")),
                "http_status": live_info.get("http_status"),
                "cached": bool(live_info.get("cached")),
                "fetched_at": str(live_info.get("fetched_at") or ""),
                "elapsed_ms": live_info.get("elapsed_ms"),
                "ttl_seconds": LIVE_TTL_SECONDS,
            },
        })
    return {
        "ok": True,
        "agent": target,
        "count": len(groups),
        "model_total": config_total + live_total,
        "config_total": config_total,
        "live_total": live_total,
        "errors": errors,
        "error_count": len(errors),
        "providers": groups,
        "live": use_live,
        "refresh": force,
        "ttl_seconds": LIVE_TTL_SECONDS,
        "hermes_home": str(_home()),
        "note": ("模型清单来自**供应商实时返回**（live=1，%gs 短 TTL 缓存）；"
                 "配置里显式列出的模型另算一份（config_count），两者分开计数。"
                 "失败项在 errors / 每组的 live.error 里点名 provider 与原因，绝不静默回退。"
                 % LIVE_TTL_SECONDS),
    }


# ===========================================================================
# 写路由（★ 改真实配置；全部：校验 → diff → 快照 → 写入 → 回读校验）
# ===========================================================================
def _state_of_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """一份 config dict → 模型状态（与 :func:`_current_state` 同口径，但不落盘读）。"""
    block = _model_block(doc)
    return {
        "provider": str(block.get("provider") or "").strip(),
        "model": str(block.get("default") or "").strip(),
        "base_url": str(block.get("base_url") or "").strip(),
        "api_mode": str(block.get("api_mode") or "").strip(),
        "key_env": str(block.get("key_env") or "").strip(),
        "fallbacks": _fallback_entries(doc),
        "auxiliary": _auxiliary_state(doc),
        "reasoning_effort": _reasoning_effort(doc),
        "reasoning_overrides": _reasoning_overrides(doc),
    }


def _checks_for(state: Dict[str, Any], expected: Dict[str, str]) -> List[Dict[str, Any]]:
    """``expected``（点分键）逐条与实际状态比对。

    认得的键（旧的三种一字不动）：

    * ``model.<field>`` —— 主模型段；
    * ``fallback.<field>`` —— 备用链**首条**（旧语义）；
    * ``fallbacks.count`` —— 备用链长度；
    * ``fallbacks[N].provider`` / ``fallbacks[N].model`` —— 备用链**任意下标**；
    * ``fallbacks.chain`` —— **整链**（JSON 逐字比，**顺序敏感**）；
    * ``auxiliary.<slot>.<field>`` —— 某个辅助槽位的某个字段（槽位名里可以带 ``.``，
      所以从右往左切）；
    * ``agent.reasoning_effort`` —— 主模型思考强度（``scope=reasoning`` 的回读断言）。

    不认得的键 ⇒ ``actual="<unknown-key>"``（一定不 ok，绝不静默通过）。
    """
    # 点分键 → state 里的字段名（model.default 在 state 里叫 model，别用 split 猜）
    field_map = {
        "model.provider": "provider",
        "model.default": "model",
        "model.base_url": "base_url",
        "model.api_mode": "api_mode",
        "model.key_env": "key_env",
        REASONING_EFFORT_PATH: "reasoning_effort",
    }
    out: List[Dict[str, Any]] = []
    for key, want in expected.items():
        if key in field_map:
            actual = state.get(field_map[key], "")
        elif key == "fallback.provider":
            actual = state["fallbacks"][0]["provider"] if state["fallbacks"] else ""
        elif key == "fallback.model":
            actual = state["fallbacks"][0]["model"] if state["fallbacks"] else ""
        elif key == "fallbacks.count":
            actual = str(len(state.get("fallbacks") or []))
        elif key == "fallbacks.chain":
            actual = json.dumps(state.get("fallbacks") or [], ensure_ascii=False, sort_keys=True)
        elif key.startswith("fallbacks[") and key.endswith(("].provider", "].model")):
            index_text, _, field = key[len("fallbacks["):].partition("].")
            entries = state.get("fallbacks") or []
            try:
                index = int(index_text)
            except ValueError:
                index = -1
            actual = str(entries[index].get(field) or "") if 0 <= index < len(entries) else "<missing>"
        elif key.startswith("auxiliary."):
            head, _, field = key.rpartition(".")
            slot = head[len("auxiliary."):]
            actual = str(((state.get("auxiliary") or {}).get(slot) or {}).get(field, "<missing>"))
        else:
            actual = "<unknown-key>"
        out.append({"key": key, "expected": want, "actual": actual,
                    "ok": str(actual or "") == str(want or "")})
    return out


def _bool_flag(value: Any) -> bool:
    """请求体里的布尔（接受 ``true`` / ``\"1\"`` / ``\"true\"``；其余一律 False）。"""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _require_confirm(payload: Dict[str, Any], *, action: str) -> None:
    """服务端第二道二次确认：没有 ``confirm: true`` 就拒。

    界面的「当前 → 目标」对比条是第一道；这里是第二道 —— 任何脚本直接打接口都不会
    因为少打一个字段就把真实 profile 的模型改掉。
    """
    if not _bool_flag(payload.get("confirm")):
        raise ApiError(400, CODE_CONFIRMATION_REQUIRED,
                       "%s 会改真实 profile 的 config.yaml，必须在请求体里带 confirm=true"
                       "（界面在二次确认条上点「确认」后才发这一步）" % action)


def _write_scope(payload: Dict[str, Any]) -> str:
    """请求体里的 ``scope``（缺省 ``model``），非法 ⇒ 400 且带上允许的四个段。"""
    scope = str(payload.get("scope") or "model").strip().lower()
    if scope not in SCOPES:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "scope 必须是 %s 之一（收到 %r）" % (list(SCOPES), scope))
    return scope


def _require_model_name(raw: Any) -> str:
    """顶层 ``model``（主模型 / 备用首条用）：必填、≤200、不含控制字符。"""
    model = "" if raw is None else str(raw).strip()
    if not model:
        raise ApiError(400, CODE_INVALID_ARGUMENT, "model 必填（目标模型名不能为空）")
    if len(model) > 200 or any(ch in model for ch in "\r\n\t"):
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "非法 model 名：不能含换行/制表符，长度 ≤200（收到 %r）" % model[:80])
    return model


def _write_target(agent: str, payload: Dict[str, Any]) -> Tuple[str, str, str]:
    """按 ``scope`` 校验并归一本次写入目标 ⇒ ``(scope, provider, model)``。

    ``model`` / ``fallback`` 两段沿用旧校验（provider 必须在该 agent 可切换集合里、
    model 必填）；``fallbacks`` / ``auxiliary`` / ``reasoning`` 是**结构化段**，目标写在
    payload 的结构里（``fallback_models`` / ``ops`` / ``slot`` + ``fields`` /
    ``reasoning_effort``），顶层 provider/model 不是必填，交给 :func:`_plan_write`
    按段校验（``reasoning`` 的取值合法性在这里就查一遍：**快照之前**就拒掉非法值）。
    """
    scope = _write_scope(payload)
    if scope == "fallbacks":
        _check_fallbacks_payload(payload)
        return scope, str(payload.get("provider") or "").strip(), str(payload.get("model") or "").strip()
    if scope == "auxiliary":
        _require_aux_slot_name(payload)
        return scope, str(payload.get("provider") or "").strip(), str(payload.get("model") or "").strip()
    if scope == "reasoning":
        # 取值非法 ⇒ 400 value_rejected（缺键不算错：那表示"保持不变"）。
        _reasoning_requested(payload)
        return scope, "", ""
    return scope, _require_provider(agent, payload.get("provider")), _require_model_name(payload.get("model"))


def _batch_target(agents: Sequence[str], payload: Dict[str, Any]) -> Tuple[str, str, str]:
    """批量版的 :func:`_write_target`：结构化段不做"并集 provider"校验（逐 agent 判）。"""
    scope = _write_scope(payload)
    if scope == "fallbacks":
        _check_fallbacks_payload(payload)
        return scope, str(payload.get("provider") or "").strip(), str(payload.get("model") or "").strip()
    if scope == "auxiliary":
        _require_aux_slot_name(payload)
        return scope, str(payload.get("provider") or "").strip(), str(payload.get("model") or "").strip()
    if scope == "reasoning":
        _reasoning_requested(payload)
        return scope, "", ""
    return scope, _batch_provider(agents, payload.get("provider")), _require_model_name(payload.get("model"))


def _require_fallback_entry(entry: Dict[str, Any], where: str) -> None:
    """一条备用模型的最小合法性（provider 文法 + model 非空且不含控制字符）。"""
    if not entry["provider"]:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "%s 缺 provider（备用模型每条都要有 provider）" % where)
    if not PROVIDER_ID_RE.match(entry["provider"]):
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "%s 的 provider %r 非法：只能是 1-64 位字母/数字/点/下划线/短横线"
                       % (where, entry["provider"]))
    if not entry["model"]:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "%s 缺 model（备用模型每条都要有 model）" % where)
    if len(entry["model"]) > 200 or any(ch in entry["model"] for ch in "\r\n\t"):
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "%s 的 model 名非法：不能含换行/制表符，长度 ≤200（收到 %r）"
                       % (where, entry["model"][:80]))


def _fallback_list_from(raw: Any) -> List[Dict[str, Any]]:
    """``fallback_models`` → 要写的整条链（**空数组合法** = 清空备用链）。"""
    if not isinstance(raw, (list, tuple)):
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "fallback_models 必须是数组（每项 {provider, model}；空数组合法，"
                       "表示清空备用链）")
    if len(raw) > FALLBACK_MAX_ENTRIES:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "备用模型最多 %d 条（收到 %d 条）" % (FALLBACK_MAX_ENTRIES, len(raw)))
    out: List[Dict[str, Any]] = []
    for index, item in enumerate(raw):
        where = "fallback_models[%d]" % index
        if not isinstance(item, dict):
            raise ApiError(400, CODE_INVALID_ARGUMENT, "%s 必须是 {provider, model} 对象" % where)
        entry = _fallback_entry_of(item)
        _require_fallback_entry(entry, where)
        out.append(entry)
    return out


def _index_of(raw: Any, length: int, where: str, *, allow_end: bool = False) -> int:
    """``ops`` 里的下标：必须是整数且落在 ``0..length-1``（``allow_end`` 时允许 ``length``）。"""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "%s 的下标必须是整数（收到 %r）" % (where, raw)) from None
    upper = length if allow_end else length - 1
    if value < 0 or value > upper:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "%s 的下标 %d 越界（当前 %d 条，允许 0..%d）"
                       % (where, value, length, upper))
    return value


def _fallback_apply_ops(entries: Sequence[Dict[str, Any]],
                        raw_ops: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """在当前链上**逐条**执行 ops（``append`` / ``insert`` / ``remove`` / ``move`` /
    ``clear`` / ``replace``），返回 ``(新链, notes)``。

    **没被 op 点到的条目原样保留**（含 provider/model 之外的键）：只动该动的那一条。
    """
    if not isinstance(raw_ops, (list, tuple)) or not raw_ops:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "ops 必须是非空数组（[{op: append|insert|remove|move|clear|replace, ...}]）")
    chain: List[Dict[str, Any]] = [dict(item) for item in entries]
    notes: List[str] = []
    for position, raw in enumerate(raw_ops):
        where = "ops[%d]" % position
        if not isinstance(raw, dict):
            raise ApiError(400, CODE_INVALID_ARGUMENT, "%s 必须是对象（{op: ...}）" % where)
        op = str(raw.get("op") or "").strip().lower()
        if op not in FALLBACK_OPS:
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "%s 的 op %r 不认识（允许：%s）"
                           % (where, op, "、".join(FALLBACK_OPS)))
        if op == "clear":
            chain = []
            notes.append("%s：清空备用链" % where)
        elif op == "replace":
            chain = _fallback_list_from(raw.get("fallback_models"))
            notes.append("%s：整链替换为 %d 条" % (where, len(chain)))
        elif op in ("append", "insert"):
            entry = _fallback_entry_of(raw)
            _require_fallback_entry(entry, where)
            if op == "append" or raw.get("index") is None:
                chain.append(entry)
                notes.append("%s：追加 %s/%s" % (where, entry["provider"], entry["model"]))
            else:
                at = _index_of(raw.get("index"), len(chain), where, allow_end=True)
                chain.insert(at, entry)
                notes.append("%s：插入到第 %d 位 %s/%s"
                             % (where, at, entry["provider"], entry["model"]))
        elif op == "remove":
            if raw.get("index") is not None:
                at = _index_of(raw.get("index"), len(chain), where)
                removed = _fallback_entry_of(chain.pop(at))
                notes.append("%s：删除第 %d 条 %s/%s"
                             % (where, at, removed["provider"], removed["model"]))
            else:
                target = _fallback_entry_of(raw)
                if not target["provider"] or not target["model"]:
                    raise ApiError(400, CODE_INVALID_ARGUMENT,
                                   "%s 的 remove 需要 index，或 {provider, model} 用来定位"
                                   % where)
                hit = -1
                for at, item in enumerate(chain):
                    same_provider = (str(item.get("provider") or "").strip().lower()
                                     == target["provider"].lower())
                    same_model = str(item.get("model") or "").strip() == target["model"]
                    if same_provider and same_model:
                        hit = at
                        break
                if hit < 0:
                    raise ApiError(400, CODE_NOT_FOUND,
                                   "%s：链里没有 %s/%s（当前 %d 条）"
                                   % (where, target["provider"], target["model"], len(chain)))
                chain.pop(hit)
                notes.append("%s：删除第 %d 条 %s/%s"
                             % (where, hit, target["provider"], target["model"]))
        else:  # move
            frm = _index_of(raw.get("from"), len(chain), where)
            to = _index_of(raw.get("to"), len(chain), where)
            if frm != to:
                chain.insert(to, chain.pop(frm))
            notes.append("%s：把第 %d 条移到第 %d 位" % (where, frm, to))
        if len(chain) > FALLBACK_MAX_ENTRIES:
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "%s 之后备用链有 %d 条，超过上限 %d 条"
                           % (where, len(chain), FALLBACK_MAX_ENTRIES))
    return chain, notes


def _check_fallbacks_payload(payload: Dict[str, Any]) -> None:
    """``scope=fallbacks`` 的**入口校验**（不需要 doc 就能判的部分）。

    两种写法互斥：``fallback_models``（整链替换）或 ``ops``（逐条增删排序）。
    两边都给 ⇒ 400（不许两种语义打架）。
    """
    has_list = payload.get("fallback_models") is not None
    has_ops = payload.get("ops") is not None
    if has_list and has_ops:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "fallback_models 与 ops 只能给一个"
                       "（前者整链替换，后者逐条增删排序）")
    if not has_list and not has_ops:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "scope=fallbacks 需要 fallback_models（整链，可为空数组）"
                       "或 ops（[{op: append|insert|remove|move|clear|replace, ...}]）")
    if has_list:
        _fallback_list_from(payload.get("fallback_models"))   # 只校验形状，结果丢弃
    elif not isinstance(payload.get("ops"), (list, tuple)) or not payload.get("ops"):
        raise ApiError(400, CODE_INVALID_ARGUMENT, "ops 必须是非空数组")


def _require_aux_slot_name(payload: Dict[str, Any]) -> str:
    """``scope=auxiliary`` 的入口校验：``slot`` 必填且文法合法（存在性按 agent 逐个判）。"""
    slot = str(payload.get("slot") or "").strip()
    if not slot:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "scope=auxiliary 必须带 slot（槽位名按 agent 的 config.yaml 动态取，"
                       "见 GET /models/overview 的 auxiliary_slots）")
    if not AUX_SLOT_RE.match(slot):
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "非法槽位名 %r：只能是 1-64 位字母/数字/点/下划线/短横线" % slot)
    return slot


def _aux_provider_value(agent: str, slot: str, raw: Any, current: str) -> str:
    """辅助槽位的 provider：必须在该 agent 认识的集合里，另外 ``auto`` 恒允许
    （辅助槽位可以用 ``auto`` 跟随主模型），**当前值**也恒允许（否则改不动）。"""
    text = "" if raw is None else str(raw).strip()
    if not text:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "槽位 %s 的 provider 不能写成空（要清空请改 model）" % slot)
    if not PROVIDER_ID_RE.match(text):
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "非法 provider 名 %r：只能是 1-64 位字母/数字/点/下划线/短横线" % text)
    allowed: Dict[str, str] = {name.lower(): name for name in _provider_names(agent)}
    allowed.setdefault("auto", "auto")
    if current:
        allowed.setdefault(current.lower(), current)
    if text.lower() not in allowed:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "agent %s 不认识 provider %r（辅助槽位可选：%s）"
                       % (agent, text, "、".join(sorted(allowed.values()))),
                       providers=sorted(allowed.values()), agent=agent, slot=slot)
    return allowed[text.lower()]


def _model_catalog(agent: str, provider: str) -> List[str]:
    """该 agent 视角下 ``provider`` 的模型清单（**只读配置**，不发外网）。

    ``live=False``：面板的每次渲染都不该替用户打上游。拉不到 / 不认识 ⇒ 空清单，
    调用方据此把"无法校验"和"校验不过"分开（空清单时**不拒写**，见 :func:`_model_value`）。
    """
    try:
        names = list(_model_list(agent, provider, live=False).get("models") or [])
    except ApiError:
        return []
    # 写入口的校验清单 = 配置里显式列出的 ∪ **已经拉到过**的实时结果（只读缓存，不发新请求）：
    # 面板刚展示过的真实模型不该在写入口被判非法；缓存没命中时行为与以前完全一致。
    with _LIVE_LOCK:
        entry = _LIVE_CACHE.get((str(_home()), str(agent).lower(), str(provider).lower()))
    if entry and entry.get("payload", {}).get("ok"):
        names = sorted(set(names) | set(entry["payload"].get("models") or []))
    return names


def _allow_unlisted_model(payload: Dict[str, Any]) -> bool:
    """请求体是否显式允许"模型不在该 provider 的清单里"。

    这是「高级：手填模型名」的**唯一**通道：默认关闭，写 true 时后端跳过清单校验并在
    响应/notes 里留一条 ``unverified_model``。为什么要有：有些 provider 的模型清单在
    config.yaml 里只列了几条（``source=config``），而用户确实要用清单外的模型；
    但**默认拒绝**才能让"非法 provider/model 组合"不静默落盘。
    """
    return _bool_flag(payload.get("allow_unlisted_model"))


def _model_value(agent: str, *, provider: str, raw: Any, current: str,
                 allow_unlisted: bool, where: str) -> str:
    """``provider → model`` 两级联动在**服务端**的最后一道闸：模型必须属于这个 provider。

    规则（顺序即优先级）：

    1. 空字符串合法 —— 表示"跟随该 provider 的默认模型"（``triage_specifier`` 这类槽位常态）；
    2. 与**当前值**相同 ⇒ 恒合法（否则用户改不动自己已经写着的值）；
    3. ``provider`` 是 ``auto`` / 空 ⇒ 恒合法（跟随主模型，无法按 provider 校验）；
    4. ``allow_unlisted=True``（界面的「高级：手填模型名」）⇒ 合法，但调用方要记一条
       ``unverified_model`` 说明；
    5. 该 provider 的模型清单**为空** ⇒ 合法（没有清单就没法判对错；**不因为读不到清单拒写**）；
    6. 清单里有（大小写不敏感，命中后**归一成清单里的拼法**）⇒ 合法；
    7. 否则 ⇒ 400 ``value_rejected``，消息里点名 agent / provider / 收到的值 / 该 provider
       的候选模型（最多 20 个），并告诉调用方「高级自定义要显式带 allow_unlisted_model」。
    """
    text = "" if raw is None else str(raw).strip()
    if not text:
        return ""
    if text == str(current or "").strip():
        return text
    provider_name = str(provider or "").strip()
    if not provider_name or provider_name.lower() == "auto":
        return text
    if allow_unlisted:
        return text
    names = _model_catalog(agent, provider_name)
    if not names:
        return text
    lowered = {name.lower(): name for name in names}
    if text.lower() in lowered:
        return lowered[text.lower()]
    raise ApiError(
        400, CODE_VALUE_REJECTED,
        "%s 的模型 %r 不属于 provider %r：该 provider 在该 agent 的可用模型是 %s%s。"
        "要用手填的清单外模型，请在请求体里带 allow_unlisted_model=true（界面在「高级："
        "手填模型名」里做这件事）"
        % (where, text[:80], provider_name, "、".join(names[:20]),
           "（还有 %d 个）" % (len(names) - 20) if len(names) > 20 else ""),
        field="model", agent=agent, provider=provider_name, value=text[:80],
        models=names[:200], allow_unlisted_model=True)


def _aux_text_value(name: str, raw: Any) -> str:
    """辅助槽位的字符串字段（``model`` / ``reasoning_effort`` / ``base_url`` / ``api_mode``）。

    ``model`` **允许空字符串**：``triage_specifier`` / ``web_extract`` 这类槽位的
    常态就是 ``provider=auto, model=''``，写空是合法操作。
    """
    text = "" if raw is None else str(raw).strip()
    if len(text) > 200 or any(ch in text for ch in "\r\n\t"):
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "%s 的取值非法：不能含换行/制表符，长度 ≤200（收到 %r）"
                       % (name, text[:80]))
    return text


def _aux_timeout_value(raw: Any, slot: str) -> int:
    """辅助槽位的 ``timeout``（整数秒，0..3600）。"""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "槽位 %s 的 timeout 必须是整数秒（收到 %r）" % (slot, raw)) from None
    low, high = AUX_TIMEOUT_RANGE
    if value < low or value > high:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "槽位 %s 的 timeout 必须在 %d..%d 秒之间（收到 %d）"
                       % (slot, low, high, value))
    return value


def _plan_auxiliary(agent: str, slot: str, item: Dict[str, Any],
                    writable: Sequence[str], payload: Dict[str, Any]
                    ) -> Tuple[Dict[str, Any], Dict[str, str], List[str]]:
    """算出 ``scope=auxiliary`` 这次要写进 ``auxiliary.<slot>`` 的字段。

    返回 ``(新槽位 dict, expected, notes)``。

    **只碰这一个槽位、只碰点名的字段**：没点名的字段原样保留（``key_env`` 这种只读
    指针也在其中，且永远不在 ``expected`` 里）。值没变 ⇒ **既不写也不进 expected**
    （"保持不变"的字段不产生任何写请求）。
    """
    new_item = dict(item)
    expected: Dict[str, str] = {}
    notes: List[str] = []

    fields = payload.get("fields")
    if fields is None:
        fields = {}
    if not isinstance(fields, dict):
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "fields 必须是对象（例如 {\"reasoning_effort\": \"high\"}）")

    requested: Dict[str, Any] = {}
    for key, value in fields.items():
        name = str(key).strip()
        if name in AUX_READONLY_FIELDS:
            raise ApiError(400, CODE_READ_ONLY_FIELD,
                           "辅助槽位的 %s 是**只读**的凭据指针：不写、不回明文。"
                           "要换 key 请改 .env 里那个变量的取值" % name,
                           field=name, slot=slot)
        if name not in writable:
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "槽位 %s 不能写 %r（该槽位可写字段：%s）"
                           % (slot, name, "、".join(writable)),
                           slot=slot, writable_fields=list(writable))
        requested[name] = value

    for name in ("provider", "model"):
        if payload.get(name) is None:
            continue
        if name in requested and str(requested[name]) != str(payload.get(name)):
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "%s 给了两个不同的值（顶层 %s 与 fields.%s 打架）：只留一个"
                           % (slot, name, name), slot=slot, field=name)
        requested[name] = payload.get(name)

    for name in writable:
        if name not in requested:
            continue
        raw = requested[name]
        if name == "provider":
            new_value: Any = _aux_provider_value(agent, slot, raw,
                                                 str(item.get("provider") or "").strip())
        elif name == "model":
            # provider → model 两级联动的服务端一道闸：**按新的 provider 校验模型**
            # （provider 在 writable 里排第一，所以这里拿到的一定是本次要写的 provider）。
            effective_provider = str(new_item.get("provider") or item.get("provider") or "").strip()
            new_value = _model_value(
                agent, provider=effective_provider, raw=raw,
                current=str(item.get("model") or ""),
                allow_unlisted=_allow_unlisted_model(payload),
                where="槽位 %s" % slot)
            catalog = _model_catalog(agent, effective_provider) if effective_provider else []
            if (new_value and catalog and new_value not in catalog
                    and effective_provider.lower() != "auto"):
                notes.append("%s.model=%s 不在 provider %s 的清单里（按 allow_unlisted_model 放行，"
                             "运行时若上游不认这个名字会失败）"
                             % (slot, new_value, effective_provider))
        elif name == "timeout":
            new_value = _aux_timeout_value(raw, slot)
        else:
            new_value = _aux_text_value(name, raw)
        old_value = item.get(name)
        if str(old_value) == str(new_value):
            continue          # 值没变 ⇒ 不写、不记 expected（"保持不变"）
        new_item[name] = new_value
        expected["auxiliary.%s.%s" % (slot, name)] = str(new_value)
        notes.append("%s.%s：%s → %s" % (slot, name,
                                         "" if old_value is None else old_value,
                                         new_value))
    # ---- 「provider 变了，旧 model 必须清空或重新校验」的服务端兜底 ----------
    # 界面在 provider 的 onChange 里就把 model 清空了；这里再挡一次，避免脚本/旧调用方
    # 只改 provider、把属于旧 provider 的模型留在新 provider 名下（那种错配只有运行时才炸）。
    if "provider" in requested and "model" not in requested:
        new_provider = str(new_item.get("provider") or "").strip()
        kept = str(new_item.get("model") or "").strip()
        if kept and new_provider.lower() != "auto":
            catalog = _model_catalog(agent, new_provider)
            if catalog and kept not in catalog and kept.lower() not in {n.lower() for n in catalog}:
                raise ApiError(
                    400, CODE_VALUE_REJECTED,
                    "槽位 %s 换到 provider %r 后，原来的模型 %r 不属于它（可选：%s）。"
                    "两级联动要求换 provider 时同时给 model（清空就写 model=\"\"，"
                    "再按新 provider 的清单重选）"
                    % (slot, new_provider, kept[:80], "、".join(catalog[:20])),
                    slot=slot, provider=new_provider, model=kept,
                    models=catalog[:200])
    return new_item, expected, notes


def _changed_paths(before: Any, after: Any, prefix: str = "") -> List[str]:
    """两份 config dict 之间**真的变了**的点分路径（递归，顺序稳定）。

    用途：``scope=reasoning`` 的红线护栏 —— 写之前先算一遍"我到底改了哪几个路径"，
    只要有一个不是 ``agent.reasoning_effort``，就**拒绝写入**（500，绝不落盘）。
    这条把"换布局不许顺带覆盖推理强度"从"我们记得别写"变成"写了就炸"。
    """
    out: List[str] = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in list(before.keys()) + [k for k in after.keys() if k not in before]:
            path = "%s.%s" % (prefix, key) if prefix else str(key)
            if key not in before or key not in after:
                out.append(path)
                continue
            out.extend(_changed_paths(before[key], after[key], path))
        return out
    if isinstance(before, (list, tuple)) and isinstance(after, (list, tuple)):
        if len(before) != len(after):
            out.append(prefix)
            return out
        for index, (old, new) in enumerate(zip(before, after)):
            out.extend(_changed_paths(old, new, "%s[%d]" % (prefix, index)))
        return out
    if before != after or type(before) is not type(after):
        out.append(prefix)
    return out


def _normalize_reasoning_value(raw: Any) -> str:
    """把一个外部给的思考强度**归一化**（大小写 / 前后空格），非法 ⇒ 400 ``value_rejected``。

    只接受 ``none`` + 宿主 ``VALID_REASONING_EFFORTS`` 这 8 个值。**绝不**把不认识的
    取值悄悄换成默认值（那会让"我设了 ultra"变成"其实跑的是 medium"）：
    宿主 ``parse_reasoning_effort`` 对不认识的取值返回 ``None`` ⇒ 调用方用默认值，
    界面上完全看不出来，所以校验必须在这里挡住。

    ``false`` / ``disabled`` / 空串 / 布尔 都不是合法取值（虽然宿主解析器认它们），
    错误信息里明确告诉调用方"关闭请用 none"。
    """
    allowed = _reasoning_values()
    if isinstance(raw, bool):
        raise ApiError(400, CODE_VALUE_REJECTED,
                       "reasoning_effort 不接受布尔值 %r：关闭请写 %r，等级请写 %s 之一"
                       % (raw, REASONING_EFFORT_OFF, "、".join(allowed[1:])),
                       field=REASONING_EFFORT_PATH, allowed_values=list(allowed))
    text = "" if raw is None else str(raw).strip().lower()
    if text not in allowed:
        raise ApiError(400, CODE_VALUE_REJECTED,
                       "reasoning_effort 只能是 %s 之一（收到 %r；大小写与前后空格会被归一化，"
                       "但归一化后仍非法就拒写，绝不静默写成默认值）"
                       % ("、".join(allowed), text[:40]),
                       field=REASONING_EFFORT_PATH, value=text[:40],
                       allowed_values=list(allowed),
                       off_semantics=_reasoning_off_semantics())
    return text


def _reasoning_requested(payload: Dict[str, Any]) -> Optional[str]:
    """从请求体取"这次要写的思考强度" ⇒ ``None`` 表示**保持不变**。

    两种写法等价（**互斥**，给两个不同的值 ⇒ 400）：

    * 顶层 ``reasoning_effort``（推荐，界面就用这个）；
    * ``fields: {"reasoning_effort": ...}``（与 ``scope=auxiliary`` 同款，脚本复用方便）。

    缺键 / 显式 ``null`` ⇒ ``None``：调用方据此**一个字节都不写**（连快照都不做）。
    空字符串**不算**缺键（那是"给了个非法值"，必须 400 而不是静默保持原样）。
    """
    missing = object()
    top: Any = payload.get("reasoning_effort", missing)
    nested: Any = missing
    fields = payload.get("fields")
    if fields is not None:
        if not isinstance(fields, dict):
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "fields 必须是对象（例如 {\"reasoning_effort\": \"high\"}）")
        unknown = [str(key) for key in fields if str(key).strip() != "reasoning_effort"]
        if unknown:
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "scope=reasoning 的 fields 只认 reasoning_effort（收到 %s）"
                           % "、".join(sorted(unknown)),
                           field=REASONING_EFFORT_PATH)
        if "reasoning_effort" in fields:
            nested = fields.get("reasoning_effort")
    if top is not missing and nested is not missing:
        if str(top).strip().lower() != str(nested).strip().lower():
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "reasoning_effort 给了两个不同的值（顶层 %r 与 fields.reasoning_effort %r "
                           "打架）：只留一个" % (top, nested), field=REASONING_EFFORT_PATH)
    value = nested if top is missing else top
    if value is missing or value is None:
        return None
    return _normalize_reasoning_value(value)


def _plan_reasoning(doc: Dict[str, Any], payload: Dict[str, Any],
                    previous: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any],
                                                        Dict[str, str], List[str]]:
    """``scope=reasoning``：**只**改 ``agent.reasoning_effort`` 一个键。

    返回 ``(doc, target_state, expected, notes)``。三条硬约束：

    1. **只碰这一个键**：``agent:`` 段里别的键（``max_turns`` / ``service_tier`` /
       ``personalities`` …）一个都不动；``agent:`` 段不存在时只新建 ``{reasoning_effort: …}``。
    2. **只允许这一个路径变**：写完在内存里跑 :func:`_changed_paths`，发现别的路径也变了
       ⇒ 500 拒绝（这是"换布局不许顺带覆盖推理强度"的护栏，不是靠记性）。
    3. **请求体没带值 ⇒ 一个字节都不写**（``expected`` 为空、``doc`` 原样返回），
       由 ``_apply_one`` 的"不变就不落盘"分支返回 ``changed: false``。
    """
    value = _reasoning_requested(payload)
    notes: List[str] = []
    expected: Dict[str, str] = {}
    if value is None:
        notes.append("请求体没带 reasoning_effort：视为保持不变，本次**没有**任何写请求")
        return doc, dict(previous), expected, notes

    block = doc.get("agent")
    if block is None:
        block = {}
    elif not isinstance(block, dict):
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "agent: 段不是 mapping（%s），拒绝改它（否则会连带覆盖整个 agent 段）"
                       % type(block).__name__, field=REASONING_EFFORT_PATH)
    current = _reasoning_effort(doc)
    before_doc = copy.deepcopy(doc)
    if current == value:
        notes.append("%s 已经是 %r：值没变，本次不写任何字节" % (REASONING_EFFORT_PATH, value))
        return doc, dict(previous), expected, notes

    block = dict(block)
    block["reasoning_effort"] = value
    doc["agent"] = block
    changed = _changed_paths(before_doc, doc)
    # 允许的变更路径：恒有 agent.reasoning_effort；``agent:`` 段本来不存在时，
    # 新建这个段本身也算（但段里只许有这一个键）。
    allowed = {REASONING_EFFORT_PATH}
    if "agent" not in before_doc and set(doc.get("agent") or {}) <= {"reasoning_effort"}:
        allowed.add("agent")
    unexpected = [path for path in changed if path not in allowed]
    if unexpected:
        raise ApiError(500, CODE_UPSTREAM_ERROR,
                       "内部护栏：scope=reasoning 只允许改 %s，实际会改 %s —— 拒绝写入"
                       % (REASONING_EFFORT_PATH, "、".join(unexpected)),
                       field=REASONING_EFFORT_PATH, changed_paths=changed)
    expected[REASONING_EFFORT_PATH] = value
    notes.append("%s：%s → %s" % (REASONING_EFFORT_PATH, current or "（未设置）", value))
    if value == REASONING_EFFORT_OFF:
        notes.append("%r 在宿主眼里是关闭（parse_reasoning_effort ⇒ %s）"
                     % (REASONING_EFFORT_OFF, json.dumps(_reasoning_off_semantics(),
                                                         ensure_ascii=False)))
    overrides = _reasoning_overrides(doc)
    if overrides:
        notes.append("该 agent 还有 per-model 覆盖（%s）：那些模型**优先用覆盖值**，"
                     "改全局对它们无效（本分区只读覆盖，不写）"
                     % "、".join(sorted(overrides)))
    target_state = dict(previous)
    target_state["reasoning_effort"] = value
    return doc, target_state, expected, notes


def _diff_rows(previous: Dict[str, Any], target_state: Dict[str, Any], scope: str) -> List[Dict[str, Any]]:
    """「当前 → 目标」两行对比（界面/响应共用；``scope`` 决定比哪两个字段）。"""
    if scope == "model":
        return [
            {"key": "provider", "from": previous["provider"], "to": target_state["provider"]},
            {"key": "model", "from": previous["model"], "to": target_state["model"]},
        ]
    if scope in STRUCTURED_SCOPES:
        # 整链 / 单槽位：逐条逐字段比（口径与预览表完全一致，不许两套说法）
        return _changes_for(previous, target_state, scope)
    old = previous["fallbacks"][0] if previous["fallbacks"] else {}
    new = target_state.get("fallback") or {}
    return [
        {"key": "fallback.provider", "from": old.get("provider", ""), "to": new.get("provider", "")},
        {"key": "fallback.model", "from": old.get("model", ""), "to": new.get("model", "")},
    ]


def _plan_write(doc: Dict[str, Any], *, scope: str, provider: str, model: str,
                payload: Dict[str, Any], agent: str = ""
                ) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, str], List[str], List[str]]:
    """在 ``doc`` 上**就地**算出这次要写的字段 ⇒ ``(doc, target_state, expected, notes, deleted)``。

    **只碰目标段**：``scope=model`` 只改 ``model:`` 里的键，``scope=fallback`` 只改
    ``fallback_providers`` 的首条，``scope=fallbacks`` 只改 ``fallback_providers`` 整链，
    ``scope=auxiliary`` 只改 ``auxiliary.<slot>`` 这一个槽位。
    ``expected`` 是写后回读校验逐键要比的点分键（认得的键见 :func:`_checks_for`）。

    虚拟 provider（moa）额外把 ``base_url`` 写成本地标记（``moa://local``）、
    ``api_mode`` 写成 ``chat_completions``（宿主 ``bind_moa_runtime`` 的口径），并删掉
    遗留的 ``key_env`` —— 虚拟 provider 不需要 key，留着等于把别人的 key 指向本地方案。

    本函数**不落盘**：预览（``/batch/preview``）拿它的输出来告诉用户"将写哪些行"。
    """
    previous = _state_of_doc(doc)
    notes: List[str] = []
    deleted: List[str] = []
    virtual = _virtual_lookup()

    # ★ 红线的另一半：别的段收到 reasoning_effort 时**不写、但要说话**。
    # 静默忽略会让界面/脚本以为"顺手也把思考强度改了"，这里明确告诉调用方去用
    # scope=reasoning（辅助槽位的 fields.reasoning_effort 是槽位自己的字段，不在此列）。
    if scope != "reasoning" and payload.get("reasoning_effort") is not None:
        notes.append("本次 scope=%s 不写思考强度：agent.reasoning_effort 一个字节都没动；"
                     "要改主模型的思考强度请用 scope=reasoning" % scope)

    if scope == "model":
        block = _model_block(doc)
        old_provider = str(block.get("provider") or "").strip()
        block["provider"] = provider
        block["default"] = model
        expected: Dict[str, str] = {"model.provider": provider, "model.default": model}
        if provider.lower() in virtual:
            entry = _virtual_providers().get(virtual[provider.lower()], {})
            base_url = str(entry.get("base_url") or "")
            if base_url:
                block["base_url"] = base_url
                expected["model.base_url"] = base_url
            block["api_mode"] = "chat_completions"
            expected["model.api_mode"] = "chat_completions"
            dropped = [key for key in ("key_env",) if block.pop(key, None) not in (None, "")]
            deleted.extend("model." + key for key in dropped)
            notes.append("%s 是虚拟 provider（%s）：写入 base_url=%s / api_mode=chat_completions%s"
                         % (provider, entry.get("label") or provider, base_url or "（空）",
                            "，并删除遗留的 " + "、".join(dropped) if dropped else ""))
            if provider.lower() == "moa":
                presets = _moa_presets(doc)
                names = sorted(presets["presets"].keys())
                if names and model not in presets["presets"]:
                    notes.append("预设 %r 不在该 agent 的 moa.presets（%s）里：宿主会静默回退到 "
                                 "default_preset %r"
                                 % (model, "、".join(names[:8]),
                                    presets["default_preset"] or "default"))
                elif not names:
                    notes.append("该 agent 没有 moa.presets：切到 moa 后用的是宿主内置默认配方")
        elif old_provider and old_provider.lower() != provider.lower():
            # 官方路由同步：换 provider 后旧 base_url/api_mode 属于旧路由，
            # 留着会把新 provider 的 key 发到旧端点（#113719 / #40862）。
            try:
                from hermes_cli.route_identity import drop_stale_model_route  # type: ignore

                popped, unverified = drop_stale_model_route(block, provider, doc)
                if popped:
                    notes.append("已清除旧 provider 的路由键：" + ", ".join(sorted(popped)))
                if unverified:
                    notes.append("model.base_url 归属无法确认（不是 %s 的端点），"
                                 "已保留；如不对请手动改" % provider)
            except Exception as exc:  # noqa: BLE001 - 官方 helper 缺失只是少一步清理
                notes.append("官方路由清理不可用（%s），base_url 保持原样" % type(exc).__name__)
        doc["model"] = block
        target_state = {"provider": provider, "model": model,
                        "base_url": str(block.get("base_url") or "").strip(),
                        "key_env": str(block.get("key_env") or "").strip(),
                        "api_mode": str(block.get("api_mode") or "").strip(),
                        "fallbacks": previous["fallbacks"],
                        # ★ 思考强度**不归本段管**：原样带回（一个字节都不改），
                        # 只是让 applied / previous 两份状态可比。
                        "reasoning_effort": previous.get("reasoning_effort") or ""}
        return doc, target_state, expected, notes, deleted

    # ---- scope == "fallbacks"（有序整链：增 / 删 / 排序 / 清空） --------------
    # 只写 fallback_providers 这一段；两种写法互斥（整链替换 / 逐条 ops）。
    if scope == "fallbacks":
        if payload.get("fallback_models") is not None and payload.get("ops") is not None:
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "fallback_models 与 ops 只能给一个"
                           "（前者整链替换，后者逐条增删排序）")
        if payload.get("fallback_models") is not None:
            entries = _fallback_list_from(payload.get("fallback_models"))
            notes.append("整链替换为 %d 条备用模型（按给定顺序）" % len(entries))
        elif payload.get("ops") is not None:
            entries, op_notes = _fallback_apply_ops(_fallback_raw(doc), payload.get("ops"))
            notes.extend(op_notes)
        else:
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "scope=fallbacks 需要 fallback_models（整链，可为空数组）"
                           "或 ops（[{op: append|insert|remove|move|clear|replace, ...}]）")
        doc["fallback_providers"] = entries
        target_state = dict(previous)
        # 读视图与 _state_of_doc 同口径（归一化成 {provider, model}），否则回读校验会假阳
        target_state["fallbacks"] = _fallback_entries(doc)
        target_state["fallback"] = (dict(target_state["fallbacks"][0])
                                    if target_state["fallbacks"] else None)
        expected: Dict[str, str] = {"fallbacks.count": str(len(target_state["fallbacks"]))}
        for index, item in enumerate(target_state["fallbacks"]):
            expected["fallbacks[%d].provider" % index] = item["provider"]
            expected["fallbacks[%d].model" % index] = item["model"]
        return doc, target_state, expected, notes, deleted

    # ---- scope == "auxiliary"（辅助模型单个槽位；槽位按配置动态取） ----------
    if scope == "auxiliary":
        block = doc.get("auxiliary")
        slots = _aux_slot_names(doc)
        if not isinstance(block, dict) or not slots:
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "agent %s 的 config.yaml 没有 auxiliary: 段（没有可写的辅助槽位）"
                           % (agent or "?"), slots=[])
        slot = str(payload.get("slot") or "").strip()
        if not slot:
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "scope=auxiliary 必须带 slot（该 agent 的槽位：%s）"
                           % "、".join(slots), slots=slots)
        if slot not in slots:
            raise ApiError(400, CODE_NOT_FOUND,
                           "agent %s 没有辅助槽位 %r（现有：%s）"
                           % (agent or "?", slot, "、".join(slots)),
                           slots=slots, agent=agent or "")
        item = dict(block.get(slot) or {})
        writable = _aux_writable_fields(item)
        new_item, expected, aux_notes = _plan_auxiliary(agent, slot, item, writable, payload)
        notes.extend(aux_notes)
        block[slot] = new_item
        doc["auxiliary"] = block
        target_state = dict(previous)
        target_state["auxiliary"] = _auxiliary_state(doc)
        return doc, target_state, expected, notes, deleted

    # ---- scope == "reasoning"（主模型思考强度；**只写 agent.reasoning_effort**）----
    # ★ 红线：这一支是唯一允许碰 agent.reasoning_effort 的地方。别的段（上面四支）
    # 一个字节都不动它 —— 老板原话「换布局不许顺带覆盖推理强度」。
    if scope == "reasoning":
        doc, target_state, expected, reasoning_notes = _plan_reasoning(doc, payload, previous)
        notes.extend(reasoning_notes)
        return doc, target_state, expected, notes, deleted

    # ---- scope == "fallback" ------------------------------------------------
    entries = _fallback_entries(doc)
    raw_list = payload.get("fallback_models")
    if raw_list is not None:
        if not isinstance(raw_list, list) or not raw_list:
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "fallback_models 必须是非空数组（每项 {provider, model}）")
        entries = []
        for item in raw_list:
            if not isinstance(item, dict):
                raise ApiError(400, CODE_INVALID_ARGUMENT,
                               "fallback_models 的每一项必须是 {provider, model} 对象")
            entries.append({
                "provider": str(item.get("provider") or "").strip(),
                "model": str(item.get("model") or "").strip(),
            })
    else:
        head = {"provider": provider, "model": model}
        rest = [item for item in entries
                if not (item["provider"].lower() == provider.lower()
                        and item["model"] == model)]
        entries = [head, *rest]
    doc["fallback_providers"] = entries
    target_state = dict(previous)
    target_state["fallbacks"] = entries
    # 备用段的"写入了什么"必须显式给出：applied.provider/model 是**主模型**，
    # 在 fallback 段里它们没变，界面拿它们做成功文案会骗人。
    target_state["fallback"] = dict(entries[0]) if entries else None
    expected = {"fallback.provider": provider, "fallback.model": model}
    return doc, target_state, expected, notes, deleted


def _scope_unchanged(previous: Dict[str, Any], target_state: Dict[str, Any], scope: str) -> bool:
    """结构化段（``fallbacks`` / ``auxiliary`` / ``reasoning``）算完是不是**一个字都没变**。

    用来实现"「保持不变」字段不产生任何写请求"：不变就整段不写（连快照都不做）。
    ``reasoning`` 段"请求体没带值"与"值本来就是这个"都落进这里 ⇒ 零写请求。
    """
    if scope == "fallbacks":
        return (previous.get("fallbacks") or []) == (target_state.get("fallbacks") or [])
    if scope == "auxiliary":
        return (previous.get("auxiliary") or {}) == (target_state.get("auxiliary") or {})
    if scope == "reasoning":
        return str(previous.get("reasoning_effort") or "") == str(target_state.get("reasoning_effort") or "")
    return False


def _apply_one(*, agent: str, scope: str, provider: str, model: str, note: str,
               payload: Dict[str, Any]) -> Dict[str, Any]:
    """对**一个** agent 执行「快照 → 官方原子写 → 回读校验（不符则字节还原）」。

    成功返回完整响应体；失败抛 ``ApiError``（``/apply`` 交给 ``_api`` 翻成失败体，
    ``/batch/apply`` 逐条捕获 —— 一条失败不影响别的 agent）。

    **调用方负责持有** ``_WRITE_LOCK``（批量时整批一把锁，避免和别的写操作交错）。
    """
    with _profile_scope(agent) as target_path:
        doc = _read_for_write(target_path)
        previous = _state_of_doc(doc)
        doc, target_state, expected, notes, deleted = _plan_write(
            doc, scope=scope, provider=provider, model=model, payload=payload, agent=agent)
        # 本段写的是哪个点分字段（``reasoning`` 段恒为 agent.reasoning_effort，
        # 别的段没有"单一字段"概念 ⇒ 空字符串）。界面靠它决定成功文案。
        field = REASONING_EFFORT_PATH if scope == "reasoning" else ""

        # ---- 「保持不变」的整段 ⇒ 一个字节都不写 ------------------------------
        # 结构化段（整链 / 单槽位 / 思考强度）算完发现一个字段都没变时，**不建快照、不落盘**：
        # 原子写入器即使值相同也会重排文本，那属于"没必要的写"。
        if scope in STRUCTURED_SCOPES and _scope_unchanged(previous, target_state, scope):
            return {
                "ok": True,
                "operation": "apply",
                "agent": agent,
                "label": _labels().get(agent, agent),
                "scope": scope,
                "slot": str(payload.get("slot") or ""),
                "field": field,
                "provider": provider,
                "model": model,
                "reasoning_effort": str(target_state.get("reasoning_effort") or ""),
                "note": note,
                "changed": False,
                "applied": target_state,
                "previous": previous,
                "diff": [],
                "snapshot_path": "",
                "snapshot_ts": "",
                "snapshot_sha256": "",
                "verify": [],
                "verified": True,
                "config_path": str(target_path),
                "write_path": "",
                "warnings": list(notes) + ["本次请求与磁盘上的值完全一致：没有写任何字节"],
                "note_text": "没有任何字段需要改：本次**没有**写 config.yaml（也没做快照）。",
            }

        # ---- 快照（写前，失败就不许写） ------------------------------------
        snapshot = _take_snapshot(agent, operation="apply", note=note or "apply %s" % scope)

        # ---- 官方原子写 ----------------------------------------------------
        # allow_deletions 只放"切到虚拟 provider 时确实要删的那几个键"，
        # 官方写入器若还发现别的缺键，照样拒绝（见 _write_official）。
        _write_official(target_path, doc, allow_deletions=deleted)
        after_doc = _read_config_doc(target_path)
        after = _state_of_doc(after_doc)
        checks = _checks_for(after, expected)
        verified = all(item["ok"] for item in checks)

        if not verified:
            # 写进去的东西跟期望不一致 ⇒ 立刻用刚做的快照字节还原，绝不留下半截状态
            restored = False
            try:
                _restore_bytes(Path(snapshot["path"]), target_path)
                restored = True
            except ApiError:
                restored = False
            raise ApiError(
                500, CODE_VERIFY_MISMATCH,
                "写入后回读校验不符（已尝试用快照还原，restored=%s）。"
                "恢复路径：%s → %s" % (restored, snapshot["path"], target_path),
                agent=agent, scope=scope, expected=expected, verify=checks,
                snapshot_path=snapshot["path"], snapshot_ts=snapshot["ts"],
                restore_path=snapshot["path"], restored=restored,
                config_path=str(target_path), previous=previous, applied=target_state,
            )

        return {
            "ok": True,
            "operation": "apply",
            "agent": agent,
            "label": _labels().get(agent, agent),
            "scope": scope,
            "slot": str(payload.get("slot") or ""),
            "field": field,
            "provider": provider,
            "model": model,
            "reasoning_effort": str(target_state.get("reasoning_effort") or ""),
            "note": note,
            "changed": True,
            "applied": target_state,
            "previous": previous,
            "diff": _diff_rows(previous, target_state, scope),
            "snapshot_path": snapshot["path"],
            "snapshot_ts": snapshot["ts"],
            "snapshot_sha256": snapshot["sha256"],
            "verify": checks,
            "verified": True,
            "config_path": str(target_path),
            "write_path": "hermes_cli.config.atomic_config_write",
            "warnings": notes,
            # 「高级：手填模型名」写进去的名字**不在该 provider 的清单里** ⇒ 显式标出来，
            # 界面据此显示"未校验"，不让人以为这是清单里选出来的。
            "unverified_model": bool(
                model) and scope == "auxiliary" and bool(provider)
            and provider.lower() != "auto" and _allow_unlisted_model(payload)
            and model not in _model_catalog(agent, provider),
            "note_text": "已写入并回读校验通过。要撤销，把 snapshot_path 传给 POST /models/rollback。",
        }


@router.post("/apply")
@_api
def apply_changes(payload: Dict[str, Any] = Body(default=None)) -> Any:  # noqa: A001
    """★把 provider/model 写进**一个** agent 的 ``config.yaml``。

    请求体（``scope=model`` / ``fallback``，旧形状不变）::

        {"agent": "xiaomo", "scope": "model", "provider": "balance",
         "model": "glm-5.3", "confirm": true, "note": "手动切换"}

    ``scope=fallbacks``（备用链整链，任意条数）::

        {"agent": "xiaomo", "scope": "fallbacks", "confirm": true,
         "fallback_models": [{"provider": "balance", "model": "a"},
                             {"provider": "gemini", "model": "b"}]}

        {"agent": "xiaomo", "scope": "fallbacks", "confirm": true,
         "ops": [{"op": "append", "provider": "balance", "model": "c"},
                 {"op": "remove", "index": 1},
                 {"op": "move", "from": 2, "to": 0}]}

    ``scope=auxiliary``（辅助模型单个槽位）::

        {"agent": "xiaomo", "scope": "auxiliary", "slot": "approval",
         "provider": "balance", "model": "deepseek-v4.1-flash",
         "fields": {"reasoning_effort": "none"}, "confirm": true}

    ``scope=reasoning``（主模型的**思考强度**，只写 ``agent.reasoning_effort``）::

        {"agent": "xiaomo", "scope": "reasoning", "reasoning_effort": "high",
         "confirm": true, "note": "代码活，想清楚点"}

    * 合法取值 = ``none``（关闭）+ ``minimal`` / ``low`` / ``medium`` / ``high`` /
      ``xhigh`` / ``max`` / ``ultra``，共 8 个（``GET /models/overview`` 的
      ``reasoning_effort_values`` 是同一份）；大小写/前后空格会归一化
      （``"  HIGH "`` ⇒ ``"high"``），归一化后仍不合法 ⇒ **400 ``value_rejected``**
      （绝不静默写成默认值）。``false`` / ``disabled`` / 空串 / 布尔都拒（关闭请用 ``none``）。
    * ``fields: {"reasoning_effort": ...}`` 是等价写法（与 ``scope=auxiliary`` 同款）；
      两种写法给两个不同的值 ⇒ 400 ``invalid_argument``。
    * **请求体没带这个键（或显式 ``null``）⇒ 视为「保持不变」，零写请求**：
      返回 ``changed: false``，不建快照、不落盘，``config.yaml`` 字节不变。
    * ★ **别的 scope 永远不会写它**：写 ``model`` / ``fallback(s)`` / ``auxiliary`` 时
      ``agent.reasoning_effort`` 一个字节都不动（老板原话「换布局不许顺带覆盖推理强度」）。
      这条由 :func:`_changed_paths` 在落盘前逐路径核对，不是靠记性。
    * 响应额外给 ``field: "agent.reasoning_effort"`` 与 ``reasoning_effort: "<写入后的值>"``
      （旧字段一个不少）。

    流程（顺序不许变）：**校验 → 算 diff → 快照 → 官方写入 → 回读校验 →（不符则字节还原）**。
    结构化段（``fallbacks`` / ``auxiliary`` / ``reasoning``）若算下来一个字节都不用改，
    返回 ``changed: false`` 且**不建快照、不落盘**。

    响应：``{"ok", "operation", "agent", "scope", "slot", "field", "changed", "applied",
    "previous", "reasoning_effort", "snapshot_path", "snapshot_ts", "verify", "config_path"}``。

    拒绝的情况（全部 4xx，带可执行的原因）：

    * 没带 ``confirm: true`` ⇒ 400 ``confirmation_required``；
    * 请求体带 ``targets`` / 多个 agent ⇒ 400 ``multi_agent_forbidden``（单 agent 端点不做批量；
      批量请走 ``POST /models/batch/apply``，那边逐 agent 快照、逐 agent 回读）；
    * agent / provider / scope / model 不合法 ⇒ 400 ``invalid_argument``；
    * 写辅助槽位的 ``key_env`` 等凭据指针 ⇒ 400 ``read_only_field``；
    * 已有写操作在跑 ⇒ 409 ``write_conflict``（写锁）；
    * 快照做不出来 ⇒ 500 ``snapshot_failed``（**不许**在没有快照的情况下写）。
    """
    payload = payload or {}
    if not isinstance(payload, dict):
        raise ApiError(400, CODE_INVALID_ARGUMENT, "请求体必须是 JSON 对象")

    # --- 批量跨 profile：明确拒绝（旧插件的 targets 数组） --------------------
    if payload.get("targets") is not None or payload.get("agents") is not None:
        raise ApiError(400, CODE_MULTI_AGENT_FORBIDDEN,
                       "本分区不做批量跨 profile 应用：一次只允许作用一个 agent。"
                       "请用 agent 字段（单值）逐个提交，界面会点名目标 agent。")
    if "agent" not in payload:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "缺少 agent（必填；一次只能作用于一个 agent）")
    raw_agent = payload.get("agent")
    if isinstance(raw_agent, (list, tuple)):
        raise ApiError(400, CODE_MULTI_AGENT_FORBIDDEN,
                       "agent 只能是单个值（收到数组 %r）：本分区不做批量跨 profile 应用"
                       % (list(raw_agent)[:4],))

    _require_confirm(payload, action="POST /models/apply")
    agent = _require_agent(raw_agent, write=True)

    scope, provider, model = _write_target(agent, payload)
    note = str(payload.get("note") or "").strip()[:200]

    if not _WRITE_LOCK.acquire(blocking=False):
        raise ApiError(409, CODE_WRITE_BUSY, "已有 apply/rollback 在跑，请稍后重试")
    try:
        # 真正的写入序列在 _apply_one 里（批量端点复用同一份实现，口径不许分叉）
        return _apply_one(agent=agent, scope=scope, provider=provider,
                          model=model, note=note, payload=payload)
    finally:
        _WRITE_LOCK.release()


@router.post("/rollback")
@_api
def rollback(payload: Dict[str, Any] = Body(default=None)) -> Any:
    """★从**已有快照**把该 agent 的 ``config.yaml`` 按字节还原。

    请求体::

        {"agent": "xiaomo", "snapshot": "<apply 响应里的 snapshot_path 或 snapshot_ts>",
         "confirm": true, "note": "撤销刚才的切换"}

    ``snapshot`` 三种写法都认：快照目录的 ``ts``、快照目录路径、``*.yaml`` 文件路径。
    **只允许回滚到已有快照**：不存在 ⇒ 400 ``snapshot_not_found``；路径越出快照根 ⇒ 400。

    回滚同样走「校验 → 回读校验」：还原后重新读盘比对快照里的期望值，不符 ⇒ 500
    ``verify_mismatch`` 并给出 ``restore_path``（快照文件本身仍在，可人工再盖一次）。
    """
    payload = payload or {}
    if not isinstance(payload, dict):
        raise ApiError(400, CODE_INVALID_ARGUMENT, "请求体必须是 JSON 对象")
    if payload.get("targets") is not None or payload.get("agents") is not None:
        raise ApiError(400, CODE_MULTI_AGENT_FORBIDDEN,
                       "单 agent 回滚端点不做批量：批量请走 POST /models/batch/rollback"
                       "（按账本 batch_id 一键还原整批）。")
    if "agent" not in payload:
        raise ApiError(400, CODE_INVALID_ARGUMENT, "缺少 agent（必填；一次只能作用于一个 agent）")
    raw_agent = payload.get("agent")
    if isinstance(raw_agent, (list, tuple)):
        raise ApiError(400, CODE_MULTI_AGENT_FORBIDDEN,
                       "agent 只能是单个值（收到数组）：本分区不做批量回滚")
    _require_confirm(payload, action="POST /models/rollback")
    agent = _require_agent(raw_agent, write=True)
    note = str(payload.get("note") or "").strip()[:200]

    if not _WRITE_LOCK.acquire(blocking=False):
        raise ApiError(409, CODE_WRITE_BUSY, "已有 apply/rollback 在跑，请稍后重试")
    try:
        # 还原序列在 _restore_one 里（批量回滚复用同一份实现）
        one = _restore_one(agent=agent, snapshot_ref=payload.get("snapshot"))
        # 回滚后要不要再留一份"回滚前"的快照？不自动留：用户要撤销这次回滚，
        # 应该重新 apply —— 自动快照会让快照目录在每次回滚后翻倍，反而没人敢清理。
        return {
            "ok": True,
            "operation": "rollback",
            "agent": agent,
            "label": one.get("label") or agent,
            "note": note,
            "snapshot_path": one["snapshot_path"],
            "snapshot_ts": one["snapshot_ts"],
            "restored": one["restored"],
            "previous": one["previous"],
            "diff": [
                {"key": "provider", "from": one["previous"]["provider"], "to": one["restored"]["provider"]},
                {"key": "model", "from": one["previous"]["model"], "to": one["restored"]["model"]},
            ],
            "verify": one["verify"],
            "verified": True,
            "config_path": one["config_path"],
            "note_text": "已按字节还原到快照内容，并回读校验通过。",
        }
    finally:
        _WRITE_LOCK.release()


# ===========================================================================
# 批量：多个 / 全部 agent 一次设模型（预览 → 逐个快照写入 → 逐条回读 → 一键回滚）
# ===========================================================================
def _batches_root() -> Path:
    """批量账本目录：``<home>/plugins/hermes-console/data/batches``。"""
    return _data_dir() / BATCHES_DIRNAME


def _new_batch_id() -> str:
    """批量 id（时间戳 + 随机尾巴，可读又可排序；文法见 :data:`BATCH_ID_RE`）。"""
    import uuid

    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _batch_path(batch_id: Any) -> Path:
    """``batch_id`` → 账本文件路径（文法非法直接 400，顺带挡住路径穿越）。"""
    text = "" if batch_id is None else str(batch_id).strip()
    if not BATCH_ID_RE.match(text):
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "非法 batch_id %r：只允许 1-64 位字母/数字/点/下划线/短横线" % text)
    return _batches_root() / (text + ".json")


def _save_batch(record: Dict[str, Any]) -> Path:
    """原子写账本（``.tmp`` + ``os.replace``）；目录不存在就建。"""
    path = _batch_path(record.get("batch_id"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise ApiError(500, CODE_UPSTREAM_ERROR,
                       "批量账本写入失败：%s: %s"
                       % (type(exc).__name__, _sanitize_text(str(exc)))) from exc
    return path


def _load_batch(batch_id: Any) -> Dict[str, Any]:
    """读一份账本。不存在 ⇒ 400 ``not_found``（带现有 id 列表，别让人猜）。"""
    path = _batch_path(batch_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ApiError(400, CODE_NOT_FOUND,
                       "批量账本不存在：%s（GET /models/batches 看现有的）" % path.name,
                       batches=[item.get("batch_id") for item in _list_batches()]) from exc
    except (OSError, ValueError) as exc:
        raise ApiError(500, CODE_UPSTREAM_ERROR,
                       "批量账本读不了：%s: %s"
                       % (type(exc).__name__, _sanitize_text(str(exc)))) from exc
    if not isinstance(data, dict):
        raise ApiError(500, CODE_UPSTREAM_ERROR, "批量账本 %s 的根不是对象" % path)
    return data


def _list_batches(limit: int = 20) -> List[Dict[str, Any]]:
    """最近若干份账本（按 id 倒序）。坏文件只跳过，不抛。"""
    root = _batches_root()
    out: List[Dict[str, Any]] = []
    try:
        files = sorted(root.glob("*.json"), key=lambda p: p.name, reverse=True)
    except OSError:
        return out
    for path in files[:max(1, int(limit))]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 账本坏了不该让列表 500
            continue
        if not isinstance(data, dict):
            continue
        items = data.get("results") or []
        out.append({
            "batch_id": data.get("batch_id") or path.stem,
            "created_at": data.get("created_at"),
            "scope": data.get("scope"),
            "provider": data.get("provider"),
            "model": data.get("model"),
            "count": data.get("count", len(items) if isinstance(items, list) else 0),
            "ok_count": data.get("ok_count"),
            "failed_count": data.get("failed_count"),
            "agents": [item.get("agent") for item in items if isinstance(item, dict)],
            "rollback": data.get("rollback") or None,
            "path": str(path),
        })
    return out


def _batch_agent_ids(payload: Dict[str, Any]) -> List[str]:
    """请求体里的 ``agents`` → 真实存在的 agent id 列表（去重、保序、逐个校验）。"""
    raw = payload.get("agents")
    if raw is None:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "缺少 agents（数组，例如 [\"xiaomo\", \"code-worker\"]；"
                       "单个 agent 请用 POST /models/apply）")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "agents 必须是非空数组（收到 %r）" % (raw,))
    if len(raw) > BATCH_MAX_AGENTS:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "一次最多 %d 个 agent（收到 %d 个）：再多请分两次做"
                       % (BATCH_MAX_AGENTS, len(raw)))
    ids: List[str] = []
    for item in raw:
        agent = _require_agent(item, write=True)
        if agent not in ids:
            ids.append(agent)
    return ids


def _batch_provider(agents: Sequence[str], raw: Any) -> str:
    """批量目标的 provider：必须落在**这些 agent 的并集**里（虚拟 provider 也算）。"""
    text = "" if raw is None else str(raw).strip()
    if not text:
        raise ApiError(400, CODE_INVALID_ARGUMENT, "缺少 provider（必填）")
    if not PROVIDER_ID_RE.match(text):
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "非法 provider 名 %r：只能是 1-64 位字母/数字/点/下划线/短横线" % text)
    allowed: List[str] = []
    seen: set = set()
    for agent in agents:
        for name in _provider_names(agent):
            if name.lower() not in seen:
                seen.add(name.lower())
                allowed.append(name)
    lookup = {name.lower(): name for name in allowed}
    if text.lower() not in lookup:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "这些 agent 里没有一个认识 provider %r（既没在 config.yaml 的 providers: 里声明，"
                       "也不在内置 registry / 虚拟 provider 里）" % text,
                       providers=allowed)
    return lookup[text.lower()]


def _changes_for(previous: Dict[str, Any], target_state: Dict[str, Any], scope: str) -> List[Dict[str, Any]]:
    """「这次要写哪些字段、从什么变成什么」（预览表格的每一行）。

    ``model.provider`` / ``model.default`` 恒列出（那是本次动作的目标）；
    ``base_url`` / ``api_mode`` / ``key_env`` 只在**真的会变**时列出 —— 预览不许虚报。
    """
    out: List[Dict[str, Any]] = []
    if scope == "model":
        out.append({"key": "model.provider",
                    "from": str(previous.get("provider") or ""),
                    "to": str(target_state.get("provider") or "")})
        out.append({"key": "model.default",
                    "from": str(previous.get("model") or ""),
                    "to": str(target_state.get("model") or "")})
        for key, field in (("model.base_url", "base_url"), ("model.api_mode", "api_mode"),
                           ("model.key_env", "key_env")):
            old = str(previous.get(field) or "")
            new = str(target_state.get(field) or "")
            if old != new:
                out.append({"key": key, "from": old, "to": new})
        return out
    if scope == "fallbacks":
        old_chain = previous.get("fallbacks") or []
        new_chain = target_state.get("fallbacks") or []
        out.append({"key": "fallback_providers.count",
                    "from": str(len(old_chain)), "to": str(len(new_chain))})
        for index in range(max(len(old_chain), len(new_chain))):
            old_item = old_chain[index] if index < len(old_chain) else {}
            new_item = new_chain[index] if index < len(new_chain) else {}
            for field in ("provider", "model"):
                before = str(old_item.get(field) or "")
                after = str(new_item.get(field) or "")
                if before != after:
                    out.append({"key": "fallback_providers[%d].%s" % (index, field),
                                "from": before, "to": after})
        return out
    if scope == "auxiliary":
        old_aux = previous.get("auxiliary") or {}
        new_aux = target_state.get("auxiliary") or {}
        for slot in sorted(set(old_aux) | set(new_aux)):
            old_row = old_aux.get(slot) or {}
            new_row = new_aux.get(slot) or {}
            for field in sorted(set(old_row) | set(new_row)):
                before = str(old_row.get(field) or "")
                after = str(new_row.get(field) or "")
                if before != after:
                    out.append({"key": "auxiliary.%s.%s" % (slot, field),
                                "from": before, "to": after})
        return out
    if scope == "reasoning":
        # 本段只有一行：思考强度。恒列出（与 model 段"恒列出 provider/model"同款），
        # 让界面能显示"当前 → 目标"，哪怕两边相等。
        out.append({"key": REASONING_EFFORT_PATH,
                    "from": str(previous.get("reasoning_effort") or ""),
                    "to": str(target_state.get("reasoning_effort") or "")})
        return out
    old_head = previous["fallbacks"][0] if previous.get("fallbacks") else {}
    new_head = target_state.get("fallback") or {}
    out.append({"key": "fallback_providers[0].provider",
                "from": str(old_head.get("provider") or ""),
                "to": str(new_head.get("provider") or "")})
    out.append({"key": "fallback_providers[0].model",
                "from": str(old_head.get("model") or ""),
                "to": str(new_head.get("model") or "")})
    return out


def _preview_one(agent: str, *, scope: str, provider: str, model: str,
                 payload: Dict[str, Any]) -> Dict[str, Any]:
    """一个 agent 的"将写入什么"（**纯读**，零副作用：只读 config + 内存里算）。"""
    row: Dict[str, Any] = {"agent": agent, "label": _labels().get(agent, agent)}
    try:
        with _profile_scope(agent) as target_path:
            exists = Path(target_path).is_file()
            doc = _read_config_doc(Path(target_path)) if exists else {}
            previous = _state_of_doc(doc)
            preview_doc, target_state, _expected, notes, deleted = _plan_write(
                copy.deepcopy(doc), scope=scope, provider=provider, model=model,
                payload=payload, agent=agent)
            # 批量允许"这些 agent 的并集"里的 provider；但对**某一个** agent 来说它可能不成立
            # （比如只有 xiaomo 声明了 me）。这种情况必须在预览里点名，不能等写完才发现跑不起来。
            # 结构化段不套这条：``auxiliary`` 的 provider 可能是 ``auto``（不是 registry 里的
            # provider），``fallbacks`` 的 provider 写在每条里、已由 _plan_write 逐条校验。
            if (scope not in STRUCTURED_SCOPES
                    and provider.lower() not in {name.lower() for name in _provider_names(agent)}):
                notes = list(notes) + [
                    "该 agent 自己的 config.yaml 既没声明 %s，也没有它的 key —— 写入后宿主可能"
                    "解析不到这个 provider，建议先在它的 providers: 里声明" % provider]
            row.update({
                "ok": True,
                "config_path": str(target_path),
                "config_exists": exists,
                "writable": exists,   # 没有 config.yaml ⇒ 做不出快照 ⇒ 写会被拒
                "current": previous,
                "target": target_state,
                "changes": _changes_for(previous, target_state, scope),
                "deletions": list(deleted),
                "warnings": notes if exists else notes + [
                    "该 agent 没有自己的 config.yaml：写前无法快照，/batch/apply 会拒绝这一条"
                    "（不会半截写入，也不会污染别的 agent）"],
                "error": "",
            })
            del preview_doc
    except ApiError as exc:
        row.update({"ok": False, "writable": False, "changes": [], "warnings": [],
                    "error": "%s: %s" % (exc.code, exc.message)})
    except Exception as exc:  # noqa: BLE001 - 单行失败不拖垮整张预览表
        row.update({"ok": False, "writable": False, "changes": [], "warnings": [],
                    "error": "%s: %s" % (type(exc).__name__, _sanitize_text(str(exc)))})
    return row


@router.post("/batch/preview")
@_api
def batch_preview(payload: Dict[str, Any] = Body(default=None)) -> Any:
    """★批量**预览**：只读，告诉界面"将写入哪些行、哪个字段、从什么变成什么"。

    请求体:::

        {"agents": ["xiaomo", "code-worker", "work"], "scope": "model",
         "provider": "moa", "model": "星槎"}

    **不发任何写操作、不做快照、不改一个字节** —— 界面把这张表摆给人看完再确认。
    不需要 ``confirm``（没有副作用），但 ``/batch/apply`` 必须要。
    """
    payload = payload or {}
    if not isinstance(payload, dict):
        raise ApiError(400, CODE_INVALID_ARGUMENT, "请求体必须是 JSON 对象")
    agents = _batch_agent_ids(payload)
    scope, provider, model = _batch_target(agents, payload)

    rows = [_preview_one(agent, scope=scope, provider=provider, model=model, payload=payload)
            for agent in agents]
    writable = [row for row in rows if row.get("writable")]
    return {
        "ok": True,
        "operation": "batch_preview",
        "read_only": True,
        "scope": scope,
        "provider": provider,
        "model": model,
        "count": len(rows),
        "writable_count": len(writable),
        "blocked_count": len(rows) - len(writable),
        "agents": rows,
        "rows": rows,
        "note": "这是预览：还没有写任何文件。确认后走 POST /models/batch/apply（带 confirm=true），"
                "它会逐 agent 快照 + 写入 + 回读校验。",
    }


@router.post("/batch/apply")
@_api
def batch_apply(payload: Dict[str, Any] = Body(default=None)) -> Any:
    """★批量写入：对**每个** agent 各做「快照 → 官方原子写 → 回读校验」。

    请求体:::

        {"agents": ["xiaomo", "code-worker"], "scope": "model",
         "provider": "moa", "model": "星槎", "confirm": true, "note": "统一换成 MoA"}

    语义（与单 agent ``/apply`` 完全同一份实现 :func:`_apply_one`）：

    * **一条一个快照**：写前把该 agent 的 ``config.yaml`` 按字节复制进快照目录；
    * **一条一次回读**：写后立刻读盘逐键比对，不符 ⇒ 用刚做的快照字节还原这条，
      并把它标成失败（**不碰**别的 agent）；
    * **一条失败不影响别的**：返回体里逐条给 ``ok`` / ``error`` / ``verify``；
    * **整批一份账本**：``data/batches/<batch_id>.json`` 记下每个 agent 的快照与前后值，
      ``POST /models/batch/rollback {"batch_id": ...}`` 就能一键全部还原。

    没有 ``confirm: true`` ⇒ 400 ``confirmation_required``（服务端第二道确认）。
    """
    payload = payload or {}
    if not isinstance(payload, dict):
        raise ApiError(400, CODE_INVALID_ARGUMENT, "请求体必须是 JSON 对象")
    _require_confirm(payload, action="POST /models/batch/apply")
    agents = _batch_agent_ids(payload)
    scope, provider, model = _batch_target(agents, payload)
    note = str(payload.get("note") or "").strip()[:200]

    if not _WRITE_LOCK.acquire(blocking=False):
        raise ApiError(409, CODE_WRITE_BUSY, "已有 apply/rollback 在跑，请稍后重试")
    try:
        results: List[Dict[str, Any]] = []
        for agent in agents:
            try:
                one = _apply_one(agent=agent, scope=scope, provider=provider,
                                 model=model, note=note or "batch %s" % scope, payload=payload)
                results.append({
                    "agent": agent, "ok": True, "error": "", "code": "",
                    "snapshot_ts": one["snapshot_ts"], "snapshot_path": one["snapshot_path"],
                    "config_path": one["config_path"],
                    "previous": one["previous"], "applied": one["applied"],
                    "verify": one["verify"], "verified": one["verified"],
                    "changed": one.get("changed", True),
                    "slot": one.get("slot", ""),
                    "field": one.get("field", ""),
                    "reasoning_effort": one.get("reasoning_effort", ""),
                    "warnings": one.get("warnings") or [],
                    "label": one.get("label") or agent,
                })
            except ApiError as exc:
                # 这条失败（快照做不出来 / 回读不符已自动还原 / 校验不过）⇒ 只标这条
                results.append({
                    "agent": agent, "ok": False, "error": exc.message, "code": exc.code,
                    "snapshot_ts": exc.extra.get("snapshot_ts", ""),
                    "snapshot_path": exc.extra.get("snapshot_path", ""),
                    "config_path": exc.extra.get("config_path", ""),
                    "previous": exc.extra.get("previous") or {},
                    "applied": exc.extra.get("applied") or {},
                    "verify": exc.extra.get("verify") or [],
                    "verified": False, "warnings": [], "label": _labels().get(agent, agent),
                })
        ok_count = sum(1 for item in results if item["ok"])
        batch_id = _new_batch_id()
        record = {
            "batch_id": batch_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "operation": "batch_apply",
            "scope": scope,
            "provider": provider,
            "model": model,
            "note": note,
            "count": len(results),
            "ok_count": ok_count,
            "failed_count": len(results) - ok_count,
            "results": results,
        }
        path = _save_batch(record)
        return {
            "ok": ok_count == len(results),
            "operation": "batch_apply",
            "batch_id": batch_id,
            "batch_path": str(path),
            "scope": scope,
            "slot": str(payload.get("slot") or ""),
            "provider": provider,
            "model": model,
            "count": len(results),
            "ok_count": ok_count,
            "failed_count": len(results) - ok_count,
            "results": results,
            "agents": results,
            "write_path": "hermes_cli.config.atomic_config_write（每个 agent 一次，逐条回读）",
            "note_text": "逐 agent 快照 + 写入 + 回读校验。要一键撤销整批："
                         "POST /models/batch/rollback {\"batch_id\": %r, \"confirm\": true}" % batch_id,
        }
    finally:
        _WRITE_LOCK.release()


def _restore_one(*, agent: str, snapshot_ref: Any) -> Dict[str, Any]:
    """把**一个** agent 按字节还原到某个快照，并回读校验（批量回滚与单条回滚共用）。"""
    snapshot_file, snapshot_name = _resolve_snapshot(agent, snapshot_ref)
    snapshot_state = _state_of_doc(_read_config_doc(snapshot_file))
    current = _current_state(agent)
    with _profile_scope(agent) as target_path:
        _restore_bytes(snapshot_file, target_path)
        after = _state_of_doc(_read_config_doc(target_path))
        expected = {
            "model.provider": snapshot_state["provider"],
            "model.default": snapshot_state["model"],
        }
        checks = _checks_for(after, expected)
        verified = all(item["ok"] for item in checks)
        if not verified:
            raise ApiError(
                500, CODE_VERIFY_MISMATCH,
                "回滚后回读校验不符（快照文件仍在，可人工再盖一次）。恢复路径：%s → %s"
                % (snapshot_file, target_path),
                agent=agent, snapshot_path=str(snapshot_file), snapshot_ts=snapshot_name,
                restore_path=str(snapshot_file), verify=checks,
                config_path=str(target_path), previous=current, restored=after,
            )
        return {
            "agent": agent, "ok": True, "error": "", "code": "",
            "snapshot_path": str(snapshot_file), "snapshot_ts": snapshot_name,
            "restored": after, "previous": current,
            "verify": checks, "verified": True, "config_path": str(target_path),
            "label": _labels().get(agent, agent),
        }


@router.post("/batch/rollback")
@_api
def batch_rollback(payload: Dict[str, Any] = Body(default=None)) -> Any:
    """★一键回滚整批：按账本把每个 agent 各自还原回**它自己**的原值。

    请求体:::

        {"batch_id": "20261029-231500-3f2a1b", "confirm": true}

    也接受显式条目（账本被清理时手工补救）::

        {"items": [{"agent": "xiaomo", "snapshot": "20261029-231500"},
                   {"agent": "work", "snapshot": "20261029-231500-01"}], "confirm": true}

    每条都走 :func:`_restore_one`：按字节盖回快照 → 回读比对 → 不符就是这条失败
    （``verify_mismatch``），别的条目继续。回滚结果写回账本（``rollback`` 字段），
    所以「回滚过没有」可查、可复核。
    """
    payload = payload or {}
    if not isinstance(payload, dict):
        raise ApiError(400, CODE_INVALID_ARGUMENT, "请求体必须是 JSON 对象")
    _require_confirm(payload, action="POST /models/batch/rollback")

    batch_id = str(payload.get("batch_id") or "").strip()
    items: List[Dict[str, Any]] = []
    record: Dict[str, Any] = {}
    if batch_id:
        record = _load_batch(batch_id)
        for item in record.get("results") or []:
            if not isinstance(item, dict) or not item.get("ok"):
                continue          # 当时就没写成功的条目没有"原值"可还原
            snapshot_ref = item.get("snapshot_path") or item.get("snapshot_ts")
            if not snapshot_ref:
                continue
            items.append({"agent": item.get("agent"), "snapshot": snapshot_ref})
        if not items:
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "批量 %r 里没有写入成功的条目，没有可回滚的东西" % batch_id,
                           batch_id=batch_id)
    else:
        raw_items = payload.get("items")
        if not isinstance(raw_items, (list, tuple)) or not raw_items:
            raise ApiError(400, CODE_INVALID_ARGUMENT,
                           "缺少 batch_id（推荐）或 items（[{agent, snapshot}, ...]）")
        for item in raw_items:
            if not isinstance(item, dict):
                raise ApiError(400, CODE_INVALID_ARGUMENT,
                               "items 的每一项必须是 {agent, snapshot} 对象")
            agent = _require_agent(item.get("agent"), write=True)
            items.append({"agent": agent, "snapshot": item.get("snapshot")})
        batch_id = "manual-" + _new_batch_id()

    if not _WRITE_LOCK.acquire(blocking=False):
        raise ApiError(409, CODE_WRITE_BUSY, "已有 apply/rollback 在跑，请稍后重试")
    try:
        results: List[Dict[str, Any]] = []
        for item in items:
            try:
                results.append(_restore_one(agent=item["agent"], snapshot_ref=item["snapshot"]))
            except ApiError as exc:
                results.append({
                    "agent": item.get("agent"), "ok": False,
                    "error": exc.message, "code": exc.code,
                    "snapshot_path": exc.extra.get("snapshot_path", str(item.get("snapshot") or "")),
                    "snapshot_ts": exc.extra.get("snapshot_ts", ""),
                    "restored": exc.extra.get("restored") or {},
                    "previous": exc.extra.get("previous") or {},
                    "verify": exc.extra.get("verify") or [],
                    "verified": False, "label": _labels().get(str(item.get("agent")), item.get("agent")),
                })
        ok_count = sum(1 for item in results if item["ok"])
        if record:
            record["rollback"] = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "ok_count": ok_count,
                "count": len(results),
                "results": [{"agent": item.get("agent"), "ok": item.get("ok"),
                             "snapshot_ts": item.get("snapshot_ts"),
                             "error": item.get("error", "")} for item in results],
            }
            try:
                _save_batch(record)
            except ApiError:
                logger.warning("hermes-console.api_models: 回滚结果写回账本失败（回滚本身已完成）",
                               exc_info=True)
        return {
            "ok": ok_count == len(results),
            "operation": "batch_rollback",
            "batch_id": batch_id,
            "count": len(results),
            "ok_count": ok_count,
            "failed_count": len(results) - ok_count,
            "results": results,
            "agents": results,
            "note_text": "逐 agent 按字节还原到各自的原值，并逐条回读校验。",
        }
    finally:
        _WRITE_LOCK.release()


@router.get("/batches")
@_api
def batches(limit: int = Query(default=20)) -> Dict[str, Any]:
    """批量账本列表（最近若干次）。空目录是合法空态（``count=0``）。"""
    items = _list_batches(limit=limit)
    return {
        "ok": True,
        "read_only": True,
        "root": str(_batches_root()),
        "count": len(items),
        "batches": items,
        "note": "每次 POST /models/batch/apply 都会留一份账本；"
                "把 batch_id 传给 POST /models/batch/rollback 就能一键还原整批。",
    }


# ===========================================================================
# 预设（★ 只写 console 自己的 data/presets.json，绝不动 profile 配置）
# ===========================================================================
def _empty_presets() -> Dict[str, Any]:
    return {"version": PRESETS_VERSION, "presets": {}}


def _load_presets() -> Dict[str, Any]:
    """读 ``data/presets.json``。缺文件 ⇒ 空结构；坏文件 ⇒ 抛 500（不静默清空用户数据）。

    **注意**：坏文件必须报错而不是返回空 —— 否则下一次 ``POST /presets`` 会把
    "解析不了" 当成 "本来就没有"，用一条新预设覆盖掉整份文件。
    """
    path = _presets_path()
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return _empty_presets()
    except OSError as exc:
        raise ApiError(500, CODE_UPSTREAM_ERROR,
                       "预设文件不可读：%s: %s" % (type(exc).__name__, _sanitize_text(str(exc)))) from exc
    text = raw.strip()
    if not text:
        return _empty_presets()
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ApiError(500, CODE_UPSTREAM_ERROR,
                       "预设文件 %s 不是合法 JSON（已拒绝写入，避免覆盖）：%s"
                       % (path, _sanitize_text(str(exc)))) from exc
    if not isinstance(data, dict):
        raise ApiError(500, CODE_UPSTREAM_ERROR,
                       "预设文件 %s 的根不是对象（已拒绝写入）" % path)
    presets = data.get("presets")
    if presets is None:
        data["presets"] = {}
    elif not isinstance(presets, dict):
        raise ApiError(500, CODE_UPSTREAM_ERROR,
                       "预设文件 %s 的 presets 字段不是对象（已拒绝写入）" % path)
    data.setdefault("version", PRESETS_VERSION)
    return data


def _save_presets(data: Dict[str, Any]) -> None:
    """原子写预设文件（先写 ``.tmp`` 再 ``os.replace``；目录不存在就建）。"""
    import os

    path = _presets_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise ApiError(500, CODE_UPSTREAM_ERROR,
                       "预设文件写入失败：%s: %s" % (type(exc).__name__, _sanitize_text(str(exc)))) from exc


def _normalize_preset_name(raw: Any) -> str:
    """预设名文法：字母数字开头，1-64 位字母数字/点/下划线/短横线（防路径穿越）。"""
    text = "" if raw is None else str(raw).strip()
    if not text:
        raise ApiError(400, CODE_INVALID_ARGUMENT, "预设名必填")
    if not PRESET_NAME_RE.match(text):
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "非法预设名 %r：只能是 1-64 位字母/数字/点/下划线/短横线，"
                       "且必须以字母或数字开头" % text)
    return text


def _preset_body(payload: Dict[str, Any], *, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把请求体归一成一条预设（``PUT`` 时缺省字段沿用 ``existing``）。

    预设**只描述目标**（provider / model / scope / note），不含任何凭据。
    """
    base = dict(existing or {})
    scope = str(payload.get("scope") or base.get("scope") or "model").strip().lower()
    if scope not in PRESET_SCOPES:
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "预设的 scope 必须是 %s 之一（收到 %r）：预设只描述"
                       "「一个 provider + 一个 model」，整链 / 辅助槽位不进预设"
                       % (list(PRESET_SCOPES), scope))
    provider = str(payload.get("provider") or base.get("provider") or "").strip()
    if not provider:
        raise ApiError(400, CODE_INVALID_ARGUMENT, "provider 必填")
    if not PROVIDER_ID_RE.match(provider):
        raise ApiError(400, CODE_INVALID_ARGUMENT,
                       "非法 provider 名 %r：只能是 1-64 位字母/数字/点/下划线/短横线" % provider)
    model = str(payload.get("model") or base.get("model") or "").strip()
    if not model:
        raise ApiError(400, CODE_INVALID_ARGUMENT, "model 必填")
    if len(model) > 200 or any(ch in model for ch in "\r\n\t"):
        raise ApiError(400, CODE_INVALID_ARGUMENT, "非法 model 名（含换行/制表符或过长）")
    label = str(payload.get("label") or base.get("label") or "").strip()[:80]
    note = str(payload.get("note") or base.get("note") or "").strip()[:200]
    return {
        "scope": scope,
        "provider": provider,
        "model": model,
        "label": label or model,
        "note": note,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _presets_payload(data: Dict[str, Any], *, agent: Optional[str]) -> Dict[str, Any]:
    """``presets`` 的读视图。``agent`` 只用于顺带告诉界面"当前值是什么"（判断是否已生效）。"""
    store = data.get("presets") or {}
    rows: List[Dict[str, Any]] = []
    current = _current_state(agent) if agent else None
    for name in sorted(store.keys()):
        entry = store[name] if isinstance(store[name], dict) else {}
        row = {"name": name, **entry}
        if current is not None and entry:
            row["is_current"] = (
                str(entry.get("provider") or "").lower() == current["provider"].lower()
                and str(entry.get("model") or "") == current["model"]
            )
        rows.append(row)
    return {
        "ok": True,
        "path": str(_presets_path()),
        "version": data.get("version", PRESETS_VERSION),
        "count": len(rows),
        "presets": rows,
        "agent": agent,
        "note": "预设存在 console 自己的数据目录里（data/presets.json），"
                "**不改任何 profile 的 config.yaml**；应用预设仍然走 POST /models/apply"
                "（需要 confirm=true，且一次只作用一个 agent）。",
    }


@router.get("/presets")
@_api
def presets_list(agent: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """预设列表（只读 console 自己的数据文件）。空文件是合法空态（``count=0``）。"""
    target = None
    if agent is not None and str(agent).strip():
        target = _require_agent(agent, write=False)
    return _presets_payload(_load_presets(), agent=target)


@router.post("/presets")
@_api
def presets_create(payload: Dict[str, Any] = Body(default=None)) -> Any:
    """新建预设（**只写 data/presets.json**）。

    同名已存在 ⇒ 409 ``preset_exists``（要改请用 PUT，避免"手滑覆盖"）。
    """
    payload = payload or {}
    if not isinstance(payload, dict):
        raise ApiError(400, CODE_INVALID_ARGUMENT, "请求体必须是 JSON 对象")
    name = _normalize_preset_name(payload.get("name"))
    data = _load_presets()
    store = data["presets"]
    if name in store:
        raise ApiError(409, CODE_CONFLICT,
                       "预设 %r 已存在（要修改请用 PUT /models/presets/%s）" % (name, name),
                       name=name)
    entry = _preset_body(payload)
    entry["created_at"] = datetime.now().isoformat(timespec="seconds")
    store[name] = entry
    _save_presets(data)
    return {"ok": True, "operation": "preset_create", "name": name, "preset": entry,
            "path": str(_presets_path()),
            "note": "只写了 presets.json；任何 profile 的 config.yaml 都没动。"}


@router.put("/presets/{name}")
@_api
def presets_update(name: str, payload: Dict[str, Any] = Body(default=None)) -> Any:
    """改预设（缺省字段沿用原值）。不存在 ⇒ 400 ``preset_not_found``。"""
    payload = payload or {}
    if not isinstance(payload, dict):
        raise ApiError(400, CODE_INVALID_ARGUMENT, "请求体必须是 JSON 对象")
    key = _normalize_preset_name(name)
    data = _load_presets()
    store = data["presets"]
    if key not in store:
        raise ApiError(400, CODE_NOT_FOUND,
                       "预设 %r 不存在（用 GET /models/presets 看现有预设）" % key,
                       presets=sorted(store.keys()))
    existing = store[key] if isinstance(store[key], dict) else {}
    entry = _preset_body(payload, existing=existing)
    entry["created_at"] = existing.get("created_at") or entry["updated_at"]
    store[key] = entry
    _save_presets(data)
    return {"ok": True, "operation": "preset_update", "name": key, "preset": entry,
            "path": str(_presets_path()),
            "note": "只写了 presets.json；任何 profile 的 config.yaml 都没动。"}


@router.delete("/presets/{name}")
@_api
def presets_delete(name: str) -> Any:
    """删预设。不存在 ⇒ 400 ``preset_not_found``（不是 500，也不是静默成功）。"""
    key = _normalize_preset_name(name)
    data = _load_presets()
    store = data["presets"]
    if key not in store:
        raise ApiError(400, CODE_NOT_FOUND,
                       "预设 %r 不存在（用 GET /models/presets 看现有预设）" % key,
                       presets=sorted(store.keys()))
    removed = store.pop(key)
    _save_presets(data)
    return {"ok": True, "operation": "preset_delete", "name": key, "removed": removed,
            "path": str(_presets_path()), "count": len(store),
            "note": "只写了 presets.json；任何 profile 的 config.yaml 都没动。"}


# ===========================================================================
# moa 预设（MoA 配方）：/models/moa/presets
# ===========================================================================
#: 本分区写 moa 预设时的点分前缀（失败体/回读断言里逐字用它，界面不用猜）。
MOA_PRESETS_PATH = "moa.presets"
#: 悬空引用红灯的失败码（409）。任何会让配置\"指不到配方\"的写入都走它。
CODE_DANGLING_REFERENCE = "dangling_reference"
#: 预设被引用时的失败码（409）。
CODE_PRESET_REFERENCED = "preset_referenced"

#: ``MoaPresetError.code`` → HTTP 状态码（其余一律 400）。
_MOA_STATUS = {
    "preset_not_found": 404,
    "preset_exists": 409,
    "preset_referenced": 409,
    "dangling_reference": 409,
    "presets_container_list": 409,
    "moa_block_invalid": 409,
}


def _moa_fail(exc: "moa_presets.MoaPresetError") -> ApiError:
    """纯逻辑层的 ``MoaPresetError`` → ``ApiError``（状态码按 code 查表，默认 400）。"""
    code = str(getattr(exc, "code", "") or CODE_INVALID_ARGUMENT)
    return ApiError(_MOA_STATUS.get(code, 400), code, exc.message, **exc.extra)


def _moa_apply(func: Any, *args: Any, **kwargs: Any) -> Any:
    """调纯逻辑层的 doc 变换（``set_preset`` / ``rename_preset`` / ``delete_preset``）。

    这些函数会拒绝\"写不进去的形状\"（例如 ``moa.presets`` 是 list 的旧格式），
    错误必须翻成 4xx/409 而不是 500 —— 它们不是\"服务坏了\"，是\"这个形状不许我写\"。
    """
    try:
        return func(*args, **kwargs)
    except moa_presets.MoaPresetError as exc:
        raise _moa_fail(exc) from exc


def _moa_sig(doc: Dict[str, Any], *, drop_presets: Sequence[str] = ()) -> str:
    """整篇 doc 的可比签名（可剔除若干预设块；改名时要同时剔掉旧名和新名）。

    用途：写入后除了逐键比对预设本身，还要断言**其余键一个都没动**
    （``atomic_config_write`` 保留注释，但\"没丢键\"这件事要断言，不能靠信任）。
    序列化不了就返回空串，调用方跳过这一行（不因此判失败）。
    """
    data = copy.deepcopy(doc)
    moa = data.get("moa")
    if isinstance(moa, dict):
        presets = moa.get("presets")
        if isinstance(presets, dict):
            for name in drop_presets:
                presets.pop(name, None)
            if not presets:
                moa.pop("presets", None)
        if not moa:
            # 该 agent 本来没有 moa 段（本次写入顺带建了它）⇒ 不算"别的键被动了"
            data.pop("moa", None)
    try:
        return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 - 序列化失败只是少了这一条断言
        return ""


def _moa_dangling_gate(agent: str, before_doc: Dict[str, Any], after_doc: Dict[str, Any],
                       *, action: str) -> List[Dict[str, Any]]:
    """**写入前的红灯闸**：写完之后配置里只要还有红灯，就 409 且一个字节都不写。

    * 本次写入**制造**了红灯（改名到不存在的引用）⇒ 直接拒；
    * 配置**本来就**有红灯（``moa.default_preset`` 指不到）⇒ 也拒，但说明\"先修好再改\"，
      并告诉人怎么修：补上那个预设（POST 同名）或把 default_preset 改到现有预设。
      唯一被放行的写入是\"把红灯修掉\"的那一次（写完之后红灯清零）。
    """
    lights_after = moa_presets.dangling_references(agent, after_doc)
    reds_after = moa_presets.red_lights(lights_after)
    if not reds_after:
        return lights_after
    reds_before = moa_presets.red_lights(moa_presets.dangling_references(agent, before_doc))
    if reds_before:
        message = (
            "%s 被拒绝：这个 agent 的配置**本来就**有 %d 处悬空引用（%s），"
            "先修好再改预设 —— 补上被引用的那个预设（POST 同名），"
            "或把 moa.default_preset 改到现有预设。"
            % (action, len(reds_before),
               "、".join(sorted({str(item.get("where")) for item in reds_before})))
        )
    else:
        message = ("%s 会写出悬空引用（%s），已拒绝且未写任何字节。"
                   % (action, "、".join(sorted({str(item.get("where")) for item in reds_after}))))
    raise ApiError(409, CODE_DANGLING_REFERENCE, message,
                   agent=agent, lights=reds_after, light_counts=moa_presets.light_counts(lights_after))


def _moa_commit(*, agent: str, target_path: Path, before_doc: Dict[str, Any],
                after_doc: Dict[str, Any], operation: str, preset_name: str,
                expected: Dict[str, Any], preserved: Dict[str, Any],
                deletions: Sequence[str] = (), must_be_absent: bool = False,
                drop_extra: Sequence[str] = (), note: str = "") -> Dict[str, Any]:
    """**快照 → 官方原子写 → 回读逐键比对 → 不符自动字节还原**（顺序不许变）。

    调用方负责在**调用本函数之前**做完所有校验与闸门（引用保护、悬空红灯）——
    这是旧插件 ``_preset_write_blockers`` 的关键顺序：拦在拿快照之前，拒绝路径零污染。
    """
    before_block = moa_presets.doc_presets(before_doc).get(preset_name)
    after_block = moa_presets.doc_presets(after_doc).get(preset_name)
    unchanged = before_doc == after_doc
    base: Dict[str, Any] = {
        "ok": True,
        "operation": operation,
        "agent": agent,
        "label": _labels().get(agent, agent),
        "preset": preset_name,
        "path": MOA_PRESETS_PATH,
        "changed": not unchanged,
        "config_path": str(target_path),
        "write_path": "hermes_cli.config.atomic_config_write",
    }
    if unchanged:
        base.update({
            "previous": before_block,
            "applied": after_block,
            "diff": [],
            "snapshot_path": "", "snapshot_ts": "", "snapshot_sha256": "",
            "verify": [], "verified": True, "deletions": list(deletions),
            "warnings": ["本次请求与磁盘上的内容完全一致：没有写任何字节（也没做快照）"],
            "note_text": "没有任何字段需要改：本次**没有**写 config.yaml。",
        })
        return base

    snapshot = _take_snapshot(agent, operation=operation,
                              note=note or "%s %s" % (operation, preset_name))
    try:
        _write_official(target_path, after_doc, allow_deletions=deletions)
    except Exception as exc:  # noqa: BLE001 - 官方写入器拒绝 ⇒ 什么都没写，翻成结构化 500
        raise ApiError(500, CODE_UPSTREAM_ERROR,
                       "写入失败（官方 atomic_config_write 拒绝）：%s: %s"
                       % (type(exc).__name__, _sanitize_text(str(exc))),
                       agent=agent, preset=preset_name, snapshot_path=snapshot["path"],
                       deletions=list(deletions)) from exc

    disk_doc = _read_config_doc(target_path)
    disk_block = moa_presets.doc_presets(disk_doc).get(preset_name)
    checks = moa_presets.verify_preset(expected=expected, actual=disk_block,
                                       preserved=preserved)
    if must_be_absent:
        checks.append({"key": "<preset 已删除>", "expected": "absent",
                       "actual": "absent" if disk_block is None else "present",
                       "ok": disk_block is None})
    before_sig = _moa_sig(before_doc, drop_presets=[preset_name, *drop_extra])
    after_sig = _moa_sig(disk_doc, drop_presets=[preset_name, *drop_extra])
    if before_sig and after_sig:
        checks.append({"key": "<config 其余键>", "expected": "unchanged",
                       "actual": "unchanged" if before_sig == after_sig else "changed",
                       "ok": before_sig == after_sig, "preserved": True})
    verified = all(row["ok"] for row in checks)

    if not verified:
        restored = False
        try:
            _restore_bytes(Path(snapshot["path"]), target_path)
            restored = True
        except ApiError:
            restored = False
        raise ApiError(
            500, CODE_VERIFY_MISMATCH,
            "写入后回读逐键校验不符（已尝试用快照还原，restored=%s）。恢复路径：%s → %s"
            % (restored, snapshot["path"], target_path),
            agent=agent, preset=preset_name, expected=expected, verify=checks,
            snapshot_path=snapshot["path"], snapshot_ts=snapshot["ts"],
            restored=restored, config_path=str(target_path),
        )

    parsed = moa_presets.parse_moa(disk_doc)
    base.update({
        "previous": before_block,
        "applied": parsed["presets"].get(preset_name),
        "diff": moa_presets.preset_diff(before_block, parsed["presets"].get(preset_name)),
        "snapshot_path": snapshot["path"],
        "snapshot_ts": snapshot["ts"],
        "snapshot_sha256": snapshot["sha256"],
        "verify": checks,
        "verified": True,
        "deletions": list(deletions),
        "state": moa_presets.preset_state(parsed),
        "default_preset": parsed["default_preset"],
        "references": moa_presets.find_references(preset_name, agent, disk_doc),
        "lights": moa_presets.dangling_references(agent, disk_doc),
        "warnings": [],
        "note_text": "已写入并回读逐键校验通过。要撤销，把 snapshot_path 传给 POST /models/rollback。",
    })
    base["light_counts"] = moa_presets.light_counts(base["lights"])
    return base


@router.get("/moa/presets")
@_api
def moa_presets_list(agent: Optional[str] = Query(default=None),
                     cross_profile: str = Query(default="0")) -> Dict[str, Any]:
    """列出该 agent 的 **moa 预设（配方）**：内容 + 谁在引用 + 悬空引用红灯。**只读。**

    ``state`` 三态（界面据此画\"编辑器 / 空态 / 没这个段\"）：

    * ``moa-presets`` —— 有预设；
    * ``moa-presets-empty`` —— 有 ``moa:`` 段但没有预设；
    * ``moa-presets-missing`` —— 该 agent 压根没有 ``moa:`` 段。

    ``writable`` = 配置里没有红灯（红灯时任何预设写入都会被 409 拒绝，见 POST/PUT/DELETE）。
    ``cross_profile=1`` 时额外给 ``cross_profile_inconsistent``（同名预设跨 profile 内容不一致）。
    """
    target = _require_agent(agent or DEFAULT_AGENT, write=False)
    cfg = _read_config(target)
    parsed = moa_presets.parse_moa(cfg)
    lights = moa_presets.dangling_references(target, cfg)

    consistency: Optional[Dict[str, Dict[str, Any]]] = None
    consistency_list: List[Dict[str, Any]] = []
    if _bool_flag(cross_profile):
        per_profile: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for other in _agent_ids():
            per_profile[other] = moa_presets.parse_moa(_read_config(other))["presets"]
        consistency_list = moa_presets.cross_profile_consistency(per_profile)
        consistency = {item["name"]: item for item in consistency_list}

    rows = [
        moa_presets.preset_row(name=name, parsed=parsed, profile=target, cfg=cfg,
                               cross_profile=consistency)
        for name in sorted(parsed["presets"].keys())
    ]
    reds = moa_presets.red_lights(lights)
    state = moa_presets.preset_state(parsed)
    default_preset = parsed["default_preset"]
    return {
        "ok": True,
        "agent": target,
        "label": _labels().get(target, target),
        "config_path": str(_config_path(target)),
        "state": state,
        "present": bool(parsed["present"]),
        "enabled": parsed["enabled"],
        "default_preset": default_preset,
        "default_preset_exists": bool(default_preset) and default_preset in parsed["presets"],
        "presets_container": parsed["presets_container"],
        "presets_error": parsed["error"],
        "count": len(rows),
        "presets": rows,
        "lights": lights,
        "light_counts": moa_presets.light_counts(lights),
        "writable": not reds,
        "write_blocked_reason": (
            "配置里有悬空引用（%s）：先把它们修好，否则任何预设写入都会被 409 拒绝"
            % "、".join(sorted({"%s=%s" % (item.get("where"), item.get("value"))
                                for item in reds}))
        ) if reds else "",
        "cross_profile_inconsistent": consistency_list,
        "write_path": "hermes_cli.config.atomic_config_write",
        "note": (
            "moa 预设就是 MoA 的\"模型\"：model.provider=moa 时 model.default 就是预设名"
            "（宿主 agent/moa_loop.py: build_moa_facade 先取 agent.model，取不到才回落到 "
            "moa.default_preset）。改/删被引用的预设会被 409 拒绝。"
        ),
    }


@router.post("/moa/presets")
@_api
def moa_presets_create(payload: Dict[str, Any] = Body(default=None)) -> Any:
    """新建一个 **moa 预设**（写该 agent 的 ``config.yaml`` 的 ``moa.presets.<name>``）。

    请求体::

        {"agent": "default", "name": "新配方", "confirm": true,
         "reference_models": [{"provider": "balance", "model": "kimi-k3",
                               "enabled": true, "reasoning_effort": "high"}],
         "aggregator": {"provider": "balance", "model": "deepseek-v4.1-flash",
                        "reasoning_effort": "high"},
         "enabled": true, "fanout": "user_turn", "degraded_reference_policy": "loud"}

    * 同名已存在 ⇒ 409 ``preset_exists``（要改请用 PUT，避免\"新建\"意外覆盖别人的配方）；
    * 没带 ``confirm: true`` ⇒ 400 ``confirmation_required``；
    * 配置里有悬空引用（红灯）⇒ 409 ``dangling_reference``（除非这次新建正好把它补上）；
    * 顺序：**校验 → 闸门 → 快照 → 官方原子写 → 回读逐键比对 →（不符则字节还原）**。
    """
    payload = payload if isinstance(payload, dict) else {}
    try:
        name = moa_presets.validate_new_name(payload.get("name"))
        refs = moa_presets.normalize_refs(payload.get("reference_models"))
        aggregator = moa_presets.normalize_aggregator(payload.get("aggregator"))
    except moa_presets.MoaPresetError as exc:
        raise _moa_fail(exc) from exc
    _require_confirm(payload, action="新建 moa 预设")
    target = _require_agent(str(payload.get("agent") or DEFAULT_AGENT).strip(), write=True)
    block = moa_presets.build_new_block(
        reference_models=refs,
        aggregator=aggregator,
        enabled=moa_presets._as_bool(payload.get("enabled")) if "enabled" in payload else None,
        fanout=(str(payload.get("fanout")).strip() or None) if payload.get("fanout") else None,
        degraded_reference_policy=(
            str(payload.get("degraded_reference_policy")).strip() or None
        ) if payload.get("degraded_reference_policy") else None,
    )
    with _profile_scope(target) as target_path:
        with _WRITE_LOCK:
            doc = _read_for_write(target_path)
            existing = moa_presets.doc_presets(doc)
            if name in existing:
                raise ApiError(409, CODE_CONFLICT,
                               "预设 %r 已存在（要改内容请用 PUT /models/moa/presets/%s）"
                               % (name, name), name=name, presets=sorted(existing.keys()))
            after_doc = _moa_apply(moa_presets.set_preset, doc, name, block)
            _moa_dangling_gate(target, doc, after_doc, action="新建预设 %r" % name)
            result = _moa_commit(
                agent=target, target_path=target_path, before_doc=doc, after_doc=after_doc,
                operation="moa_preset_create", preset_name=name,
                expected=block, preserved={}, deletions=(), note="新建 moa 预设 %s" % name)
    result["references"] = []
    return result


@router.put("/moa/presets/{name}")
@_api
def moa_presets_update(name: str, payload: Dict[str, Any] = Body(default=None)) -> Any:
    """改一个 **moa 预设**的内容（``reference_models`` / ``aggregator`` / ``enabled`` /
    ``fanout`` / ``degraded_reference_policy``），可选改名。

    请求体（**全部可选**，没给的键逐字保留）::

        {"agent": "default", "confirm": true,
         "reference_models": [...],          # 整条替换，**顺序即 fan-out 顺序**
         "aggregator": {"provider": ..., "model": ..., "reasoning_effort": ...},
         "enabled": false, "fanout": "user_turn",
         "degraded_reference_policy": "loud",
         "rename_to": "新名字",               # 可选：改名（块内容逐字搬过去）
         "allow_referenced_edit": true}      # 可选：显式确认\"我知道它在被引用\"

    引用保护（**在拿快照之前**判）：

    * 该预设正被 ``moa.default_preset`` / ``model.default`` / ``fallback_providers[].model``
      引用时 ⇒ **409 ``preset_referenced``**（响应里点名到 agent + 字段 + 值），
      除非请求体显式带 ``allow_referenced_edit: true``（**改名永远不允许**：改名会让引用指空）；
    * 写完之后配置里还有红灯 ⇒ 409 ``dangling_reference``；
    * 内容一个字节都不用改 ⇒ ``changed: false``、不建快照、不落盘。
    """
    payload = payload if isinstance(payload, dict) else {}
    _require_confirm(payload, action="修改 moa 预设 %r" % name)
    target = _require_agent(str(payload.get("agent") or DEFAULT_AGENT).strip(), write=True)
    try:
        key = moa_presets.validate_lookup_name(name)
        rename_to = payload.get("rename_to")
        new_name = moa_presets.validate_new_name(rename_to) if rename_to else key
        refs = (moa_presets.normalize_refs(payload.get("reference_models"))
                if payload.get("reference_models") is not None else None)
    except moa_presets.MoaPresetError as exc:
        raise _moa_fail(exc) from exc

    with _profile_scope(target) as target_path:
        with _WRITE_LOCK:
            doc = _read_for_write(target_path)
            existing = moa_presets.doc_presets(doc)
            if key not in existing:
                raise ApiError(404, CODE_NOT_FOUND,
                               "预设 %r 不存在（GET /models/moa/presets 看现有预设）" % key,
                               name=key, presets=sorted(existing.keys()), agent=target)
            existing_block = existing[key] if isinstance(existing[key], dict) else {}
            references = moa_presets.find_references(key, target, doc)
            renaming = new_name != key
            allow_referenced = _bool_flag(payload.get("allow_referenced_edit"))
            if references and (renaming or not allow_referenced):
                why = ("改名会让引用它的地方全部指空" if renaming
                       else "它正被引用，改内容会立刻影响使用它的 agent；"
                            "确认要改请带 allow_referenced_edit: true")
                raise ApiError(
                    409, CODE_PRESET_REFERENCED,
                    "预设 %r 正被 %d 处引用，拒绝修改：%s。引用明细见 references。"
                    % (key, len(references), why),
                    agent=target, name=key, references=references,
                    hint="先把引用改掉（moa.default_preset / model.default / fallback_providers），"
                         "或改内容时带 allow_referenced_edit: true",
                )
            try:
                block = moa_presets.build_updated_block(
                    existing_block,
                    reference_models=refs,
                    aggregator=payload.get("aggregator") if isinstance(payload.get("aggregator"), dict) else None,
                    enabled=moa_presets._as_bool(payload.get("enabled")) if "enabled" in payload else None,
                    fanout=(str(payload.get("fanout")).strip() or None) if payload.get("fanout") else None,
                    degraded_reference_policy=(
                        str(payload.get("degraded_reference_policy")).strip() or None
                    ) if payload.get("degraded_reference_policy") else None,
                )
            except moa_presets.MoaPresetError as exc:
                raise _moa_fail(exc) from exc

            if renaming:
                after_doc = _moa_apply(moa_presets.rename_preset, doc, key, new_name)
                after_doc = _moa_apply(moa_presets.set_preset, after_doc, new_name, block)
            else:
                after_doc = _moa_apply(moa_presets.set_preset, doc, key, block)

            old_agg = existing_block.get("aggregator") if isinstance(existing_block.get("aggregator"), dict) else {}
            new_agg = block.get("aggregator") if isinstance(block.get("aggregator"), dict) else {}
            dropped = sorted(set(old_agg.keys()) - set(new_agg.keys()))
            # 只放行\"这次真的要删\"的点分路径：改名 ⇒ 旧名字整块被省略；
            # 改聚合器时显式给 null ⇒ 该子键被省略（其余缺键官方写入器照样拒）。
            deletions: List[str] = []
            if renaming:
                deletions.append("moa.presets.%s" % key)
            for dropped_key in dropped:
                deletions.append("moa.presets.%s.aggregator.%s" % (new_name, dropped_key))
            _moa_dangling_gate(target, doc, after_doc, action="修改预设 %r" % key)

            expected = {
                "reference_models": block.get("reference_models"),
                "aggregator": block.get("aggregator"),
            }
            if "enabled" in block:
                expected["enabled"] = block.get("enabled")
            for optional_key in ("fanout", "degraded_reference_policy"):
                if optional_key in block:
                    expected[optional_key] = block.get(optional_key)
            preserved = {
                field: existing_block[field] for field in existing_block
                if field not in expected
            }
            result = _moa_commit(
                agent=target, target_path=target_path, before_doc=doc, after_doc=after_doc,
                operation="moa_preset_rename" if renaming else "moa_preset_update",
                preset_name=new_name, expected=expected, preserved=preserved,
                deletions=deletions, drop_extra=[key] if renaming else [],
                note=("改名 moa 预设 %s -> %s" % (key, new_name)) if renaming else ("修改 moa 预设 %s" % key))
    result["renamed_from"] = key if renaming else ""
    return result


@router.delete("/moa/presets/{name}")
@_api
def moa_presets_delete(name: str,
                       agent: Optional[str] = Query(default=None),
                       confirm: Optional[str] = Query(default=None),
                       payload: Dict[str, Any] = Body(default=None)) -> Any:
    """删一个 **moa 预设**。

    ``?agent=<id>&confirm=true``（``confirm`` 也可以放 JSON body 里）。

    引用保护（**在拿快照之前**判，没有任何\"强制删除\"开关）：

    * 被 ``moa.default_preset`` / ``model.default`` / ``fallback_providers[].model``
      引用 ⇒ **409 ``preset_referenced``**，响应 ``references`` 逐条点名
      （哪个 agent、哪个字段、哪条 fallback）；
    * 写完之后配置里还有红灯 ⇒ 409 ``dangling_reference``；
    * 拒绝路径**零污染**：不建快照、``config.yaml`` 哈希不变。
    """
    body = payload if isinstance(payload, dict) else {}
    raw_confirm = body.get("confirm") if "confirm" in body else confirm
    _require_confirm({"confirm": raw_confirm}, action="删除 moa 预设 %r" % name)
    target = _require_agent(agent or DEFAULT_AGENT, write=True)
    try:
        key = moa_presets.validate_lookup_name(name)
    except moa_presets.MoaPresetError as exc:
        raise _moa_fail(exc) from exc
    with _profile_scope(target) as target_path:
        with _WRITE_LOCK:
            doc = _read_for_write(target_path)
            existing = moa_presets.doc_presets(doc)
            if key not in existing:
                raise ApiError(404, CODE_NOT_FOUND,
                               "预设 %r 不存在（GET /models/moa/presets 看现有预设）" % key,
                               name=key, presets=sorted(existing.keys()), agent=target)
            references = moa_presets.find_references(key, target, doc)
            if references:
                raise ApiError(
                    409, CODE_PRESET_REFERENCED,
                    "预设 %r 正被 %d 处引用，拒绝删除（删了会让引用它的地方当场指不到配方）：%s。"
                    "先改掉引用它的 moa.default_preset / model.default / fallback_providers 再来删。"
                    % (key, len(references),
                       "、".join("%s(%s)" % (item["where"], item["profile"]) for item in references)),
                    agent=target, name=key, references=references,
                )
            after_doc, removed = _moa_apply(moa_presets.delete_preset, doc, key)
            _moa_dangling_gate(target, doc, after_doc, action="删除预设 %r" % key)
            result = _moa_commit(
                agent=target, target_path=target_path, before_doc=doc, after_doc=after_doc,
                operation="moa_preset_delete", preset_name=key,
                expected={}, preserved={}, must_be_absent=True,
                deletions=moa_presets.deletion_paths(name=key),
                note="删除 moa 预设 %s" % key)
    result["removed"] = removed
    result["references"] = []
    return result


__all__ = ["router"]








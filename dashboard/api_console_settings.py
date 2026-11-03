# -*- coding: utf-8 -*-
"""hermes-console · M4b0 后端设置端点：按 agent 列插件三态 + 读脱敏配置(带来源) + 写回。

挂载契约：本文件不单独挂载。``plugin_api.py`` 把本目录加进 ``sys.path`` 后 import 本模块，再
``include_router(api_console_settings.router)``；最终 URL 是
``/api/plugins/hermes-console/agents/<agent>/...``。

## 为什么需要这一层

三套已验收的 Python 能力（``dashboard/core/{scope,settings,plugin_switch,registry}.py``）
在**打包后的前端**里根本 import 不到。设置界面要读\"这个值是什么、来自哪一层\"、要写回、
要按 agent 列插件三态，只能走 HTTP。本文件**只做 HTTP 包装**：

* 参数校验（非法 agent / plugin / 键 → 400，且带可选项列表）
* 把 core 的分层结果翻成前端可渲染的形状（脱敏 + 每个键的来源层）
* 写入前置闸门（拒掩码 / 拒只读 / 拒未知键 / 乐观锁）
* 写后**二次断言**（不信写函数的自述，也不信核心层的回读结论）

不重复实现任何业务：三态来自 ``plugin_switch.plugin_state``，分层合并来自
``settings.effective_settings``（本插件）/ 同一口径的通用合并（任意插件，见下），写入只走
``settings.save_plugin_setting_for``，``/agents/{id}/modules`` 只**转发**旧聚合的
``/modules?agent=`` 处理函数。

## 四条硬约束（对应卡片）

① **三态显式**：每项 ``state`` 直接取 ``plugin_switch.plugin_state``（``enabled`` /
   ``disabled`` / ``unavailable``）。``unavailable`` 是\"两份名单里都没有\"，官方 loader 视作未
   启用，**绝不**在响应里被写成 ``enabled``。

② **脱敏 + 来源**：值一律走 ``settings.mask_sensitive``；每个键都在 ``sources`` 里标出它来自
   ``module_default`` / ``global`` / ``agent`` 哪一层。

③ **写入拒掩码**：任何字段值等于 ``"***"`` 一律 400 ``masked_value_rejected``——把掩码写回
   等于拿 ``***`` 覆盖真密钥。同时拒未知键（``unknown_setting_key``，并回可写键列表）、拒只读键
   （``readonly_setting``）、支持 ``expected_config`` 乐观锁（不一致 → 409 ``config_changed``，
   且此时**一个字节都没写**）。

④ **错误语义统一**：所有 4xx/5xx 都是 ``{"ok": false, "error": {"code", "message", ...}}``。
   非法 agent → 400 且 body 带合法 id 列表；非法 plugin → 400；该 agent 没有 ``config.yaml``
   → 读返回空结构 200、写返回 400。

## 为什么\"任意插件的分层合并\"没有直接调 ``effective_settings``

``settings.effective_settings`` 的 ``plugin_id`` 是写死的 :data:`settings.PLUGIN_ID`
（``hermes-console``）：它内部调用 ``read_plugin_settings(DEFAULT_AGENT_ID)`` 时没传 plugin_id。
本卡要的是**任意插件**的 settings，直接用它会把 ``hermes-console`` 的配置当成 ``browserpaw``
的返回，是彻底的错数据。所以：

* ``plugin_id == settings.PLUGIN_ID`` ⇒ 直接用 ``effective_settings``（权威路径）；
* 其它插件 ⇒ :func:`_layered_sources` 按**同一口径**（同一 ``_flatten``、同一 ``SOURCE_*``
  常量、同一\"``None`` = 没有这一层\"回落规则）合并 ``global`` / ``agent`` 两层 + schema 默认值。
  探针里有交叉校验：两条路径对 ``hermes-console`` 必须给出**逐键相同**的结果。

## 不写 YAML

本文件没有任何一处直接写文件：写入全部经 ``save_plugin_setting_for`` →
官方 ``hermes_cli.plugins_state.save_plugin_setting``（跨进程锁 + fail-closed 原始读 +
管理员托管键拒绝）。``plugin.yaml`` 的 ``config_schema`` 只读。
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fastapi import APIRouter, Request  # type: ignore
from fastapi.responses import JSONResponse  # type: ignore
from starlette.concurrency import run_in_threadpool  # type: ignore

_DASHBOARD_DIR = Path(__file__).resolve().parent
if str(_DASHBOARD_DIR) not in sys.path:
    # 本文件被 dashboard 用合成模块名 exec，没有包上下文；兄弟模块 / core 包只能靠 sys.path 找到。
    sys.path.insert(0, str(_DASHBOARD_DIR))

router = APIRouter()
logger = logging.getLogger(__name__)

#: 脱敏占位符（``settings.mask_sensitive`` 的产物）。**它永远不许写回去。**
MASK_PLACEHOLDER = "***"

# ---- 错误码（前端按这些字面量分支，别改） --------------------------------
CODE_INVALID_ARGUMENT = "invalid_argument"
CODE_MASKED_VALUE_REJECTED = "masked_value_rejected"
CODE_UNKNOWN_SETTING_KEY = "unknown_setting_key"
CODE_READONLY_SETTING = "readonly_setting"
CODE_CONFIG_MISSING = "config_missing"
CODE_CONFIG_CHANGED = "config_changed"
CODE_WRITE_NOT_APPLIED = "write_not_applied"
CODE_PERMISSION_DENIED = "permission_denied"
CODE_UPSTREAM_ERROR = "upstream_error"
#: ``unavailable → enabled`` 走官方准入通道时，通道**本身**不可用（`strict=True` 就报这个，
#: 绝不退化成\"只摘黑名单\"）。字面量与 ``core.plugin_switch.CODE_ADMISSION_UNAVAILABLE`` 一致。
CODE_ADMISSION_UNAVAILABLE = "admission_unavailable"
#: 官方准入权威**拒绝**（把 ``plugin_switch`` 的 code 原样透出，别改写）。
CODE_ADMISSION_REFUSED = "admission_refused"
CODE_DEPENDENCY_CONFLICT = "dependency_conflict"

#: 三态（与 ``plugin_switch.STATE_*`` 同字面量；这里再写一份只为在有 bug 时能立刻看出不一致）。
TRISTATE = ("enabled", "disabled", "unavailable")

#: 程序常量：**不是**设置项，任何写入一律 400 并说明原因（先例：``api_advisor`` 的同名白名单）。
#: 键名比较用\"整键或叶子段\"，所以 ``modules.x.blocker_forced_delivery`` 这种怪路径同样被拒。
PROGRAM_CONSTANTS: Dict[str, str] = {
    "blocker_forced_delivery": (
        "blocker_forced_delivery 是程序常量（单向阀门）：blocker 永远强制交付，"
        "没有、也不会有把 blocker 降级或关闭提示的入口，所以它不是可写设置项"
    ),
}

#: ``config_schema`` 里表示只读的字段名（官方 schema 是自由 mapping，这里把常见写法都认了）。
_READONLY_FLAGS = ("readonly", "read_only", "readOnly")
#: ``config_schema`` 声明\"值可以是容器\"的 type（其余类型只接受标量，防止把标量改成 mapping）。
_JSON_TYPES = ("json", "list", "array", "dict", "object")

#: ``(manifest 路径, mtime_ns, size) -> config_schema``：插件元数据缓存（见 :func:`_plugin_schema`）。
_SCHEMA_CACHE: Dict[Tuple[str, int, int], Dict[str, Dict[str, Any]]] = {}
_SCHEMA_CACHE_LOCK = threading.Lock()
_SCHEMA_CACHE_MAX = 512

#: ``(manifest 路径, mtime_ns, size) -> version``：插件版本缓存（见 :func:`_manifest_version`）。
#: 与 :data:`_SCHEMA_CACHE` 同键同失效口径：manifest 一改立刻重读，正常请求只 stat 不解析。
_VERSION_CACHE: Dict[Tuple[str, int, int], Optional[str]] = {}
_VERSION_CACHE_LOCK = threading.Lock()
_VERSION_CACHE_MAX = 512

#: 目录扫描的最大深度：插件 id 允许带 ``/``（真实例子 ``web/tavily``），实测最深 2 段，留 4 层余量。
_PLUGIN_SCAN_MAX_DEPTH = 4


class ApiError(Exception):
    """带 HTTP 语义的业务错误（:class:`ApiError` → 统一失败体）。"""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra: Dict[str, Any] = dict(extra)


def _fail(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    """统一失败体（与 ``plugin_api`` / ``api_advisor`` 同款）。"""
    error: Dict[str, Any] = {"code": code, "message": message}
    error.update({key: value for key, value in extra.items() if key not in ("code", "message")})
    return JSONResponse(status_code=status, content={"ok": False, "error": error})


def _error_response(exc: ApiError) -> JSONResponse:
    return _fail(exc.status, exc.code, exc.message, **exc.extra)


# ---------------------------------------------------------------------------
# core 模块按请求惰性 import
# ---------------------------------------------------------------------------
#: 串行化 core 包的「判定 → 摘除 → 导入」。本函数每个请求都会跑，而它会删 sys.modules 里的
#: core/core.* 再重新 import；桌面端同时刷多张卡（/agents/*/plugins、/modules、/overview/*）时，
#: 一个线程在删、另一个正在 import，importlib 内部就会抛 KeyError: 'core' / 'core.scope' /
#: 'core.settings'（实测 6 并发里 3 个 503）。锁是必需的，不是保守。
_CORE_IMPORT_LOCK = threading.Lock()


def _core(submodule: str) -> Any:
    """import ``core.<submodule>``（带\"同名别家模块\"防护，口径同 ``plugin_api._import_core_registry``）。

    惰性 + 每次请求重试是刻意的（照 ``api_advisor`` 的第 ③ 条）：``core`` 坏掉只让本分区返回
    503 结构化错误，而不是在挂载时把整个插件的路由一起拖没。
    """
    with _CORE_IMPORT_LOCK:
        existing = sys.modules.get("core")
        if existing is not None:
            found = getattr(existing, "__file__", None)
            same_dir = False
            if found:
                try:
                    # 归属判定必须看「_DASHBOARD_DIR 是否在其祖先链里」。
                    # 写成 .parent == _DASHBOARD_DIR 是错的：core/scope.py 的 parent 是
                    # dashboard/core，永远不等于 dashboard，于是每次请求都判成「异源」，
                    # 把 core 整棵树从 sys.modules 摘掉重导 —— 既慢，又是并发 KeyError 的源头。
                    # 口径与 plugin_api._import_core_registry 保持一致（那边用的是 in parents）。
                    same_dir = _DASHBOARD_DIR in Path(found).resolve().parents
                except OSError:
                    same_dir = False
            if not same_dir:
                # 别家插件的 ``core`` 包：整棵摘掉（含子模块），否则 import 会拿到别人的模块。
                for name in [key for key in list(sys.modules) if key == "core" or key.startswith("core.")]:
                    del sys.modules[name]
        try:
            return importlib.import_module("core." + submodule)
        except Exception as exc:  # noqa: BLE001 - 核心不可用 = 503，不是 500 栈
            raise ApiError(
                503,
                CODE_UPSTREAM_ERROR,
                "配置核心不可用（dashboard/core/%s.py 加载失败）：%s: %s"
                % (submodule, type(exc).__name__, exc),
            ) from exc


def _legal_agents() -> List[str]:
    """当前合法 agent id（非法 agent 的 400 里回给前端；这里自己坏掉也不许再抛）。"""
    try:
        return list(_core("scope").list_agent_ids())
    except Exception:  # noqa: BLE001
        return []


def _prepare_agent(raw: Any) -> str:
    """归一化 agent id；非法 → 400 + 合法 id 列表（**不是** 500）。"""
    scope = _core("scope")
    try:
        return scope.normalize_agent_id(raw)
    except ValueError as exc:
        raise ApiError(400, CODE_INVALID_ARGUMENT, str(exc), agents=_legal_agents()) from exc
    except Exception as exc:  # noqa: BLE001
        raise ApiError(
            503, CODE_UPSTREAM_ERROR, "解析 agent id 失败：%s: %s" % (type(exc).__name__, exc)
        ) from exc


def _prepare_plugin(raw: Any) -> str:
    """归一化 plugin id；非法（空串 / 大写 / 反斜杠 / ``..``）→ 400。

    用 ``settings.normalize_plugin_id``：**与写入器同一套文法**，否则\"接口放行、写入器拒绝\"会
    变成 500。
    """
    settings_mod = _core("settings")
    try:
        return settings_mod.normalize_plugin_id(raw)
    except ValueError as exc:
        raise ApiError(400, CODE_INVALID_ARGUMENT, str(exc)) from exc


# ---------------------------------------------------------------------------
# 只读辅助：config 摘要 / plugin.yaml 的 config_schema
# ---------------------------------------------------------------------------
def _config_digest(agent: str) -> str:
    """该 agent ``config.yaml`` 的字节级摘要（乐观锁令牌，与官方 ``runtime_state._digest`` 同值）。

    优先复用 M2c 已验收的 ``core.plugin_switch._config_file_digest``（它就是官方 ``_digest`` +
    同值本地兜底），拿不到再自己按同一口径算：三者字节级同值，不会因为 import 失败就把并发
    保护悄悄丢掉。
    """
    try:
        switch_mod = _core("plugin_switch")
        digest_fn = getattr(switch_mod, "_config_file_digest", None)
        if callable(digest_fn):
            return str(digest_fn(agent))
    except Exception:  # noqa: BLE001 - 兜底路径更简单，失败就直接算
        pass
    path = Path(_core("scope").agent_config_path(agent))
    try:
        from hermes_cli.runtime_state import _digest as official_digest

        return str(official_digest(path) or "missing")
    except Exception:  # noqa: BLE001
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return "missing"


def _plugin_dirs(
    agent: str,
    plugin: str,
    home: Optional[Path] = None,
    scan: Optional[Dict[str, Any]] = None,
) -> List[Path]:
    """该插件的候选安装目录（**只读**）：先该 agent 自己的 plugins 目录，再 home 根的。

    ``plugin`` 已过 :func:`_prepare_plugin`（无 ``..`` / 无反斜杠 / 无空段），拼接出不去；再补一次
    resolve 包含性检查，防的是符号链接之类的意外。

    ``home``：调用方已解析好的主目录（批量路径传它）。不传就各自解析 —— 而
    ``scope.hermes_home()`` 每次都要走官方根目录探测（实测 ~1.3ms），51 个插件 × 2 次 = 100+ 次
    白白重复解析。

    ``scan``：调用方用 :func:`_plugin_scan` 建好的一次性目录索引（批量路径传它）。传了就是**纯
    dict 查表**，不再逐插件做 ``is_dir()`` + ``resolve()`` —— 那两步每个插件两次，51 个插件实测
    228 次 ``Path.resolve()`` ≈ 19ms，占旧实现剩余耗时的一半。不传则退回逐插件探测（单插件
    调用点用这条，避免为了一个插件扫两棵目录树）。
    """
    if scan is not None:
        return _scan_dirs_for(scan, plugin)

    scope = _core("scope")
    candidates: List[Path] = []
    for base in (Path(scope.agent_home(agent, home=home)) / "plugins", Path(scope.hermes_home(home)) / "plugins"):
        try:
            target = base.joinpath(*plugin.split("/"))
            if not target.is_dir():
                continue
            resolved_base = base.resolve()
            resolved = target.resolve()
            if resolved != resolved_base and resolved_base not in resolved.parents:
                continue
            if resolved not in candidates:
                candidates.append(resolved)
        except OSError:
            continue
    return candidates


# ---------------------------------------------------------------------------
# 一次目录扫描（替代 N 次 is_dir + resolve + 逐插件 yaml 读）
# ---------------------------------------------------------------------------
def _is_reparse_point(entry: Any) -> bool:
    """该目录项是不是符号链接 / Windows 目录联接（junction）。

    ``os.path.isjunction`` 是 **Python 3.12+** 才有的：生产 venv 可能更旧，直接调用会
    ``AttributeError``（实测本机 hermes-agent venv 就是这样，整个设置页 500）。
    所以这里按能力探测：没有 ``isjunction`` 时用 ``st_file_attributes`` 里的
    ``FILE_ATTRIBUTE_REPARSE_POINT`` 判定，两者都拿不到才退回 ``is_symlink()``。
    """
    try:
        if entry.is_symlink():
            return True
    except OSError:
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if callable(isjunction):
        try:
            return bool(isjunction(entry.path))
        except OSError:
            return True
    try:
        # ``entry.stat(follow_symlinks=False)`` 在 Windows 上直接复用 scandir 那次
        # FindFirstFile 的结果（实测 0.27ms vs ``os.lstat`` 的 1.21ms），所以这里不另发系统调用。
        stat = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    attributes = getattr(stat, "st_file_attributes", 0) or 0
    # FILE_ATTRIBUTE_REPARSE_POINT = 0x400：junction / 符号链接 / 云占位文件都会带这个位
    return bool(attributes & 0x400)


def _scan_plugin_tree(base: Path) -> Dict[str, Path]:
    """把 ``<base>`` 下所有插件目录**一次**扫成 ``{相对 id: 目录}``（只 readdir，不读任何 yaml）。

    插件 id 是相对路径（``web/tavily`` 这种带 ``/`` 的也存在），所以按层下钻；深度由
    :data:`_PLUGIN_SCAN_MAX_DEPTH` 兜底，隐藏目录与 ``__pycache__`` 不进去（不是插件）。

    **符号链接只在遇到时才 resolve + 做包含性检查**：普通目录直接取 ``DirEntry.path``，
    不付 ``realpath()`` 的钱（旧实现每个插件 2 次 resolve，实测 19ms/请求）。
    """
    found: Dict[str, Path] = {}
    try:
        base_resolved = Path(base).resolve()
    except OSError:
        base_resolved = Path(base)
    try:
        stack: List[Tuple[Path, str, int]] = [(Path(base), "", 0)]
        while stack:
            directory, prefix, depth = stack.pop()
            if depth >= _PLUGIN_SCAN_MAX_DEPTH:
                continue
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                name = entry.name
                if name.startswith(".") or name == "__pycache__":
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                    # 符号链接 / Windows 目录联接（junction）**必须** resolve 后验一次包含性：
                    # 指向 base 之外的重解析点不算本目录的插件（口径与原 :func:`_plugin_dirs`
                    # 的 `resolved_base in resolved.parents` 一致，真实例子：plugins/hermes-vcc-cn
                    # 是指向 D:\hermes-data\TOOL\... 的 junction，旧实现同样不跟随）。
                    if _is_reparse_point(entry):
                        resolved = Path(entry.path).resolve()
                        if resolved != base_resolved and base_resolved not in resolved.parents:
                            continue
                        path = resolved
                    else:
                        path = Path(entry.path)
                except OSError:
                    continue
                relative = prefix + name
                found[relative] = path
                stack.append((path, relative + "/", depth + 1))
    except OSError:
        pass
    return found


def _plugin_scan(agent: str, home: Optional[Path] = None) -> Dict[str, Any]:
    """一次请求共用一份的插件目录索引（agent 自己的 ``plugins/`` + home 根 ``plugins/``）。

    这是本卡「一次目录扫描代替 N 次 yaml 读」的那一次扫描：旧实现对每个插件各做
    ``is_dir()`` + ``resolve()`` 两次（51 个插件 = 102 次探测 + 228 次 resolve），现在两个
    基础目录各 readdir 一遍，之后全是 dict 查表。

    返回的 dict 里 ``profiles`` 是**惰性**的（默认 ``None``）：只有「本地两处都找不到版本」时才
    去扫 ``<home>/profiles/*/plugins``（自制插件常只装在某个 profile 下）。多数 agent 一次都
    用不到它，所以不预先扫。
    """
    scope = _core("scope")
    resolved_home = Path(scope.hermes_home(home))
    agent_base = Path(scope.agent_home(agent, home=resolved_home)) / "plugins"
    root_base = resolved_home / "plugins"
    same = False
    try:
        same = agent_base.resolve() == root_base.resolve()
    except OSError:
        same = str(agent_base) == str(root_base)
    agent_map = _scan_plugin_tree(agent_base)
    return {
        "home": resolved_home,
        # ``default`` agent 的 home 就是根目录，两棵是同一棵树：共用一份索引（版本查找也要看它）。
        "agent": agent_map,
        "root": agent_map if same else _scan_plugin_tree(root_base),
    }


def _scan_dirs_for(scan: Mapping[str, Any], plugin: str) -> List[Path]:
    """从 :func:`_plugin_scan` 的索引里取候选目录（agent 自己的优先，其次 home 根）。"""
    out: List[Path] = []
    for table in (scan.get("agent") or {}, scan.get("root") or {}):
        path = table.get(plugin)
        if path is not None and path not in out:
            out.append(path)
    return out


def _manifest_version(directory: Path) -> Optional[str]:
    """插件目录 manifest 里的 ``version``（读不到 ⇒ ``None``）；按 manifest 签名缓存。

    与 :func:`_plugin_schema` 同一套失效口径（``(路径, mtime_ns, size)``）：``version`` 是元数据，
    只在安装 / 升级时变，而设置页一次列举要问 51 个插件的版本 —— 旧实现每个插件都要整树解析
    一遍 ``plugin.yaml``（实测 21ms/请求），现在只 stat。
    """
    for name in ("plugin.yaml", "plugin.json"):
        manifest = directory / name
        try:
            if not manifest.is_file():
                continue
        except OSError:
            continue
        signature = _manifest_signature(directory)
        with _VERSION_CACHE_LOCK:
            if signature in _VERSION_CACHE:
                return _VERSION_CACHE[signature]
        version = _parse_manifest_version(manifest)
        with _VERSION_CACHE_LOCK:
            if len(_VERSION_CACHE) > _VERSION_CACHE_MAX:
                _VERSION_CACHE.clear()
            _VERSION_CACHE[signature] = version
        if version:
            return version
    return None


def _parse_manifest_version(manifest: Path) -> Optional[str]:
    """真正读文件的那一步（结果由 :func:`_manifest_version` 按签名缓存）。"""
    try:
        import yaml  # 宿主自带

        data: Any = yaml.safe_load(manifest.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 - 没有 manifest / yaml 坏 ⇒ 没有版本，不是错误
        return None
    if not isinstance(data, Mapping):
        return None
    version = data.get("version")
    if isinstance(version, bool):  # bool 是 int 子类，别把 True 当版本号
        return None
    if isinstance(version, (int, float)):
        return str(version)
    if isinstance(version, str) and version.strip():
        return version.strip()
    return None


def _plugin_version(plugin: str, scan: Dict[str, Any], fallback: Any = None) -> Optional[str]:
    """该插件版本，口径与核心 ``plugin_catalog.read_plugin_version`` **逐字对齐**：

    1. 部署根 ``<home>/plugins/<id>/plugin.yaml``；
    2. 各 profile ``<home>/profiles/*/plugins/<id>/plugin.yaml``（按 profile 名字排序，取第一个有版本的）。

    第 1 步用本请求的目录索引（dict 命中 + manifest 签名缓存，不读 YAML）；**第 2 步直接交给
    ``fallback``（就是官方那个函数）**，不自己扫 8 个 profile 的目录树 —— 实测「扫 8 棵 plugins
    树」18.5ms，而「逐插件 stat 候选 manifest」只要 10ms，官方函数做的正是后者，且它自己也带
    签名缓存。为了省一次判断而把更贵的那条路写进来是亏的（等价性探针抓过：两者结果一致）。

    ``fallback``：核心模块的 ``read_plugin_version``；拿不到时本函数只认 root 段。
    """
    root_dirs = [scan["root"][plugin]] if plugin in (scan.get("root") or {}) else []
    for directory in root_dirs:
        version = _manifest_version(directory)
        if version:
            return version
    if callable(fallback):
        try:
            return fallback(plugin, home=scan["home"])
        except Exception:  # noqa: BLE001 - 版本读不到就是 None，不许拖垮整张清单
            return None
    return None



def _local_manifest_schema(directory: Path) -> Dict[str, Any]:
    """官方 helper 不可用时的等价实现：``plugin.yaml`` 的 ``config_schema``（只读）。"""
    manifest = directory / "plugin.yaml"
    if not manifest.is_file():
        return {}
    try:
        import yaml  # 宿主自带

        data: Any = yaml.safe_load(manifest.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 - 读不到就是没有 schema
        return {}
    raw = data.get("config_schema") if isinstance(data, Mapping) else None
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): dict(value) for key, value in raw.items() if isinstance(value, Mapping)}


def _plugin_schema(
    agent: str,
    plugin: str,
    home: Optional[Path] = None,
    scan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """该插件的 ``config_schema``（``{键: 声明}``）；未安装 / 没声明 ⇒ ``{}``。

    优先官方 ``hermes_cli.plugins_settings._manifest_config_schema``（它还认 portable 包的
    ``plugin.json``），拿不到才用本地只读实现。

    结果按 manifest 的 ``(路径, mtime_ns, size)`` 记忆：``config_schema`` 是插件**元数据**，
    只在插件安装 / 升级时变；而设置页一次列举要问 49 个插件的 schema（每个都要读一次
    plugin.yaml 做整树 YAML 解析）。键含 mtime + size，manifest 一改立刻失效。
    返回的是**拷贝**：调用方拿去算可写/只读键，不许把缓存里的对象改脏。

    ``scan``：批量路径传 :func:`_plugin_scan` 的索引（省掉逐插件 ``is_dir`` + ``resolve``）。
    """
    for directory in _plugin_dirs(agent, plugin, home=home, scan=scan):
        signature = _manifest_signature(directory)
        with _SCHEMA_CACHE_LOCK:
            cached = _SCHEMA_CACHE.get(signature)
        if cached is not None:
            if cached:
                return copy.deepcopy(cached)
            continue  # 这个候选目录没有 schema：继续看下一个候选
        try:
            from hermes_cli.plugins_settings import _manifest_config_schema

            raw = _manifest_config_schema(directory)
        except Exception:  # noqa: BLE001
            raw = _local_manifest_schema(directory)
        schema = {str(key): dict(value) for key, value in (raw or {}).items() if isinstance(value, Mapping)}
        with _SCHEMA_CACHE_LOCK:
            if len(_SCHEMA_CACHE) > _SCHEMA_CACHE_MAX:
                _SCHEMA_CACHE.clear()
            _SCHEMA_CACHE[signature] = copy.deepcopy(schema)
        if schema:
            return schema
    return {}


def _manifest_signature(directory: Path) -> Tuple[str, int, int]:
    """插件目录里 manifest 的文件签名（读不到就用目录自己的 mtime，仍能感知"后来装了 manifest"）。"""
    for name in ("plugin.yaml", "plugin.json"):
        candidate = directory / name
        try:
            stat = candidate.stat()
        except OSError:
            continue
        return (str(candidate), stat.st_mtime_ns, stat.st_size)
    try:
        stat = directory.stat()
        return (str(directory), stat.st_mtime_ns, -1)
    except OSError:
        return (str(directory), -1, -1)


# ---------------------------------------------------------------------------
# 模块开关白名单（registry 声明的 ``modules.<id>.enabled``）
# ---------------------------------------------------------------------------
def _module_switch_declarations() -> List[Dict[str, Any]]:
    """``core.registry`` 发现的控制台模块各自的开关（**声明**，不是值）。

    为什么要这一层：``dashboard/manifest.json``（本插件的插件清单）**没有 ``config_schema``**，
    所以 ``plugins.entries.hermes-console.settings`` 在文件里没被写过时，白名单是空数组，
    设置界面上连一个可写入口都没有（M4b1 实测：``has_settings=false`` + ``writable_keys=[]``）。
    而"某个 Agent 显示哪些模块"的真实落点早就定了：
    ``plugins.entries.hermes-console.settings.modules.<id>.enabled``
    （``core.registry._own_enablement`` 就是按这个键读的）。

    所以白名单从**注册表**推导：每个已发现模块 ⇒ ``modules.<id>.enabled``（registry 对**所有**
    模块都用这一个键，见 :data:`core.registry.ENABLED_KEY`），外加该模块 manifest 里
    ``settings_schema`` 声明的其它键 ⇒ ``modules.<id>.<key>``。

    返回 ``[{"key","module_id","label","default","default_enabled","declared"}]``；
    注册表不可用 ⇒ ``[]``（退化成 M4b0 的行为，而不是抛错——列清单不该因为模块注册表坏掉就 500）。
    """
    try:
        registry = _core("registry")
        specs = list(registry.discover_modules())
    except Exception:  # noqa: BLE001 - 注册表坏掉不该让设置端点连坐
        logger.warning("hermes-console.console_settings: 模块注册表不可用，模块开关不进白名单")
        return []

    out: List[Dict[str, Any]] = []
    for spec in specs:
        module_id = str(getattr(spec, "id", "") or "").strip()
        if not module_id:
            continue
        keys: Dict[str, Dict[str, Any]] = {}
        enabled_key = "%s.%s.%s" % (settings_mod_modules(), module_id, "enabled")
        keys[enabled_key] = {
            "key": enabled_key,
            "module_id": module_id,
            "label": str(getattr(spec, "label", module_id)),
            "default": bool(getattr(spec, "default_enabled", True)),
            "default_enabled": bool(getattr(spec, "default_enabled", True)),
            "declared": "registry.enabled",
        }
        schema = getattr(spec, "settings_schema", None)
        if isinstance(schema, Mapping):
            for raw_key in schema:
                name = str(raw_key).strip()
                if not name or name == "enabled":
                    continue
                dotted = "%s.%s.%s" % (settings_mod_modules(), module_id, name)
                keys[dotted] = {
                    "key": dotted,
                    "module_id": module_id,
                    "label": "%s · %s" % (str(getattr(spec, "label", module_id)), name),
                    "default": None,
                    "default_enabled": None,
                    "declared": "settings_schema",
                }
        out.extend(keys.values())
    return out


def settings_mod_modules() -> str:
    """``settings.modules`` 子树的第一段（= ``core.settings.MODULES_KEY``）。

    单独包一层只为在 core 不可用时给一个**字面量常量**兜底：这个键是 registry 文档定死的
    （``plugins.entries.<plugin>.settings.modules.<id>.enabled``），不该因为 import 失败就变。
    """
    try:
        return str(_core("settings").MODULES_KEY)
    except Exception:  # noqa: BLE001
        return "modules"


def _module_switch_layout() -> Dict[str, Any]:
    """registry 声明的模块开关：``{"keys": [点分键...], "by_key": {键: 声明}}``。

    键路径是 registry 文档定死的 ``settings.modules.<id>.enabled``（外加模块 manifest
    ``settings_schema`` 里的其它键），所以它属于"有权威声明的可写键"，不是本层发明的东西。
    """
    declarations = _module_switch_declarations()
    by_key = {str(item["key"]): item for item in declarations}
    return {"keys": sorted(by_key), "by_key": by_key}


def _schema_defaults(schema: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """``config_schema`` 里带 ``default`` 的键 ⇒ ``module_default`` 层。"""
    defaults: Dict[str, Any] = {}
    for key, spec in schema.items():
        if isinstance(spec, Mapping) and "default" in spec:
            defaults[str(key)] = spec["default"]
    return defaults


def _readonly_keys(schema: Mapping[str, Mapping[str, Any]]) -> List[str]:
    """``config_schema`` 里标了只读的键（``readonly`` / ``read_only`` / ``readOnly`` / ``writable: false``）。"""
    out: List[str] = []
    for key, spec in schema.items():
        if not isinstance(spec, Mapping):
            continue
        if any(spec.get(flag) is True for flag in _READONLY_FLAGS) or spec.get("writable") is False:
            out.append(str(key))
    return sorted(out)


# ---------------------------------------------------------------------------
# 分层合并（任意插件）
# ---------------------------------------------------------------------------
def _flatten(tree: Any, prefix: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """把嵌套 mapping 拍平成 ``{"a.b.c": value}``（与 ``core.settings._flatten`` 同口径）。"""
    flat: Dict[str, Any] = {}
    if not isinstance(tree, Mapping):
        return flat
    for key, value in tree.items():
        path = prefix + (str(key),)
        if isinstance(value, Mapping):
            if not value:
                continue
            flat.update(_flatten(value, path))
        else:
            flat[".".join(path)] = value
    return flat


def _layered_sources(agent: str, plugin: str, defaults: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """任意插件的三层合并：``module_default`` < ``global`` < ``agent``，每值带 ``source``。

    * ``plugin == settings.PLUGIN_ID`` ⇒ 直接用 ``effective_settings``（权威路径，不复制口径）；
    * 其它插件 ⇒ 同一套 ``_flatten`` / ``SOURCE_*`` / \"``None`` 视为没有这一层\" 规则；
    * ``agent == "default"`` 时全局层与该 agent 层是同一个文件，只算 ``global``（否则每个值都会
      被误标成\"这个 agent 自己改过\"）。
    """
    settings_mod = _core("settings")
    scope = _core("scope")
    if plugin == settings_mod.PLUGIN_ID:
        return dict(settings_mod.effective_settings(agent, None, defaults))

    base_layer = _flatten(defaults or {})
    global_layer = _flatten(settings_mod.read_plugin_settings(scope.DEFAULT_AGENT_ID, plugin))
    agent_layer: Dict[str, Any] = {}
    if agent != scope.DEFAULT_AGENT_ID:
        agent_layer = _flatten(settings_mod.read_plugin_settings(agent, plugin))

    merged: Dict[str, Dict[str, Any]] = {}
    for source, layer in (
        (settings_mod.SOURCE_MODULE_DEFAULT, base_layer),
        (settings_mod.SOURCE_GLOBAL, global_layer),
        (settings_mod.SOURCE_AGENT, agent_layer),
    ):
        for key, value in layer.items():
            if value is None:  # 置空 == 不覆盖，回落到上一层（与 effective_settings 同规则）
                continue
            merged[key] = {"value": value, "source": source}
    return merged


def _unflatten(flat: Mapping[str, Any]) -> Dict[str, Any]:
    """``{"retry.max": 3}`` ⇒ ``{"retry": {"max": 3}}``（``_flatten`` 的逆运算，供脱敏用）。"""
    out: Dict[str, Any] = {}
    for key, value in flat.items():
        parts = str(key).split(".")
        node = out
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value
    return out


def _raw_keys(agent: str, plugin: str) -> List[str]:
    """该插件在**文件里**出现过的全部点分键（global 层 ∪ 该 agent 层），只读。"""
    settings_mod = _core("settings")
    scope = _core("scope")
    keys: Dict[str, None] = {}
    layers = [settings_mod.read_plugin_settings(scope.DEFAULT_AGENT_ID, plugin)]
    if agent != scope.DEFAULT_AGENT_ID:
        layers.append(settings_mod.read_plugin_settings(agent, plugin))
    for layer in layers:
        for key in _flatten(layer):
            keys.setdefault(key, None)
    return list(keys)


def _settings_view(agent: str, plugin: str) -> Dict[str, Any]:
    """``GET .../settings`` 的 ``data``：脱敏值 + 每键来源 + 可写/只读 + 摘要。

    该 agent **没有自己的 ``config.yaml``** 时返回**空结构**（``settings`` / ``sources`` /
    ``masked_keys`` 全空）而不是全局层的值 —— 卡片的口径是\"读返回空结构 200\"，而把全局值
    铺在一份没有覆盖的 agent 上会让人以为\"这个 agent 配过\"。全局层有多少值只写进 ``notes``
    提示（要看全局就切到 ``default``），不静默丢信息。
    """
    settings_mod = _core("settings")
    scope = _core("scope")

    path = Path(scope.agent_config_path(agent))
    try:
        exists = path.is_file()
    except OSError:
        exists = False

    schema = _plugin_schema(agent, plugin)
    defaults = _schema_defaults(schema)
    layered = _layered_sources(agent, plugin, defaults) if exists else {}

    masked = settings_mod.mask_sensitive(_unflatten({key: item["value"] for key, item in layered.items()}))
    # ``mask_sensitive`` 把 ``masked_keys`` 塞在同一个 dict 里（顶层同名键会被它覆盖，已在 docstring
    # 记为天花板）；这里取出来放到平级字段，前端渲染更省事。
    masked_keys = list(masked.pop("masked_keys", []) or [])
    sources = {key: str(item.get("source") or "unknown") for key, item in layered.items()}

    readonly = _readonly_keys(schema)
    readonly_set = set(readonly) | set(PROGRAM_CONSTANTS)
    # 模块开关：本插件（hermes-console）的白名单来自 registry 声明，而不是 manifest 的
    # ``config_schema``（它没有）。其它插件没有模块开关这个概念，保持 M4b0 原口径。
    module_switches = _module_switch_declarations() if plugin == settings_mod.PLUGIN_ID else []
    extra_keys = [str(item["key"]) for item in module_switches]
    if exists:
        writable = sorted(
            {
                key
                for key in list(_raw_keys(agent, plugin)) + list(schema) + extra_keys
                if key not in readonly_set
            }
        )
    else:
        writable = []

    notes: List[str] = []
    if not exists:
        notes.append(
            "该 agent 没有自己的 config.yaml（%s）：读返回的是空结构（不是错误），写入会被拒绝"
            % path
        )
        if defaults:
            notes.append(
                "该插件的 manifest 声明了 %d 个带默认值的键（%s）：值是全局/默认层来的，要看请切换到 default agent"
                % (len(defaults), ", ".join(sorted(defaults)))
            )
    if masked_keys:
        notes.append("值里含敏感键，已脱敏成 %r；回写时必须提交新值，不能提交这个占位符" % MASK_PLACEHOLDER)
    if readonly:
        notes.append("manifest 标了只读的键：%s（写入一律 400）" % ", ".join(readonly))
    if module_switches:
        notes.append(
            "本插件没有 config_schema，模块开关的白名单来自 registry 声明（%d 个键）：%s。"
            "写入落点 plugins.entries.%s.settings.<键>，读回的三态见 module_switches"
            % (
                len(module_switches),
                ", ".join(extra_keys),
                settings_mod.PLUGIN_ID,
            )
        )

    digest = _config_digest(agent)
    view: Dict[str, Any] = {
        "agent": agent,
        "plugin_id": plugin,
        "settings": masked,
        "masked_keys": masked_keys,
        "sources": sources,
        "writable_keys": writable,
        "readonly_keys": readonly,
        "program_constants": dict(PROGRAM_CONSTANTS),
        "path": str(path),
        "exists": exists,
        # 乐观锁令牌 = **该 agent 自己** config.yaml 的摘要（不是根配置：改 profile 时的并发冲突
        # 发生在 profiles/<id>/config.yaml，用根文件摘要会锁到别人、且漏掉自己的变更）。
        # 与 plugin_switch._config_file_digest(agent) 同值，前端读一次就能带去 PUT。
        "expected_config": digest,
        "config_digest": digest,
        "notes": notes,
    }
    if module_switches:
        # 只**追加**字段（M4b0 的键一个不动）：前端要勾模块得有值 + 来源 + 开关 id，
        # 而 settings/sources 里不该凭空多出 7 个 modules.*.enabled（那是 registry 的默认层，
        # 写进 settings 会让\"这个 agent 配过\"的假象成立）。
        view["module_switches"] = _module_switch_view(agent, module_switches)
    return view


def _module_switch_view(agent: str, declarations: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """每个模块开关的**有效值 + 来源 + 是否可写**（值来自 ``registry.resolve_modules``）。

    三态判定仍然只认 ``core.registry``（它内部走 ``settings.effective_settings``）；本函数只把
    结果整理成前端能直接渲染的形状，不自己读 YAML、也不自己合并层。
    """
    by_module: Dict[str, Dict[str, Any]] = {}
    try:
        decisions = list(_core("registry").resolve_modules(agent))
    except Exception as exc:  # noqa: BLE001 - 注册表算不出来：开关列为空，但键照样可写
        logger.warning("hermes-console.console_settings: 计算模块开关状态失败：%s", exc)
        decisions = []
    for decision in decisions:
        spec = getattr(decision, "spec", None)
        module_id = str(getattr(spec, "id", "") or "").strip()
        if not module_id:
            continue
        by_module[module_id] = {
            "enabled": bool(getattr(decision, "enabled", False)),
            "reason": str(getattr(decision, "reason", "") or ""),
            "source": str(getattr(decision, "source", "") or "unknown"),
            "default_enabled": bool(getattr(spec, "default_enabled", True)),
        }

    out: List[Dict[str, Any]] = []
    for item in declarations:
        module_id = str(item["module_id"])
        state = by_module.get(module_id, {})
        entry = dict(item)
        entry["enabled"] = state.get("enabled", bool(item.get("default_enabled", True)))
        entry["reason"] = state.get("reason", "")
        entry["source"] = state.get("source", "module_default")
        out.append(entry)
    return out


def _catalog_aliases(raw: Any) -> List[str]:
    """catalog 里的 ``aliases``（搜索别名）→ 干净的 ``list[str]``。

    老板按中文找插件（「自动对话命名」），而插件 id / display_name 常常是英文；
    别名只是**显示/搜索**用的元数据，不影响任何三态判断。

    容错：不是 list/tuple ⇒ ``[]``；元素只留非空字符串（去首尾空白），去重保序。
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[str] = []
    seen: Dict[str, None] = {}
    for item in raw:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text and text not in seen:
            seen[text] = None
            out.append(text)
    return out


def _has_settings(
    agent: str,
    plugin: str,
    config: Mapping[str, Any],
    schemas: Dict[str, bool],
    home: Optional[Path] = None,
    scan: Optional[Dict[str, Any]] = None,
) -> bool:
    """该插件是否\"有设置可看\"：文件里写过 settings，或 manifest 声明了 ``config_schema``，
    或（仅本插件）有 registry 声明的模块开关。

    第三条是本卡的修：``dashboard/manifest.json`` 没有 ``config_schema``，真实 home 里
    ``plugins.entries.hermes-console.settings`` 也没被写过 ⇒ M4b0 口径会给出
    ``has_settings=false``，设置界面因此**没有任何可写入口**。而模块开关的落点
    （``settings.modules.<id>.enabled``）是有权威声明的（registry），所以它必须算\"有设置\"。

    ``schemas`` 是**按请求**的 memo（插件 ⇒ 有没有 schema），``scan`` 是同一请求的目录索引：
    两者一起把「51 个插件各摸一次 plugin.yaml」压成「一次扫描 + 一次 stat/插件」。
    """
    settings_mod = _core("settings")
    entry = settings_mod._plugin_settings_entry(config, plugin)
    raw = entry.get("settings") if isinstance(entry, Mapping) else None
    if isinstance(raw, Mapping) and raw:
        return True
    if plugin == getattr(settings_mod, "PLUGIN_ID", "hermes-console"):
        if _module_switch_layout()["keys"]:
            return True
    if plugin not in schemas:
        schemas[plugin] = bool(_plugin_schema(agent, plugin, home=home, scan=scan))
    return bool(schemas[plugin])


def _plugin_listing(agent: str) -> Dict[str, Any]:
    """``GET /agents/{id}/plugins`` 的 ``data``：每条都带**显式三态**。

    * 清单 = ``catalog ∪ plugins.entries ∪ plugins.enabled ∪ plugins.disabled``
      （``core.plugin_catalog.merged_plugin_ids``，M5a）。只列两份名单会漏掉\"有配置但两份名单里都没有\"
      的插件，而那正是 :data:`TRISTATE` 里最容易被误读成\"已开启\"的 ``unavailable`` 一态；只列本
      agent 的配置则会漏掉\"本 agent 名单里没有的自制插件\"——看不见就没法启用（M5a 修的断裂）。
      catalog 坏掉时 ``plugin_catalog`` 返回空目录，清单退化为 ``enabled ∪ disabled ∪ entries``。
    * ``state`` 直接来自 ``plugin_switch.plugin_state(tri_state=True)``；本文件不自己判三态。
    * 无法用官方文法表示的 id（例如超过 4 段的深嵌套）**不静默丢弃**：进 ``skipped`` 并带原因。
    * 每项除 M4b0 的六个既有键外，M5a 追加 ``ref`` / ``display_name`` / ``kind`` / ``origin`` /
      ``version`` / ``version_source`` / ``has_dashboard_module`` / ``module`` / ``module_id`` /
      ``module_ref`` / ``in_catalog`` / ``note``（**既有键一个没删**）。
    """
    switch_mod = _core("plugin_switch")
    catalog_mod = _core("plugin_catalog")
    scope = _core("scope")

    # **一次请求只解析一遍 config.yaml**：下面每个插件都用这份已读好的配置。
    # 之前每个插件各调一次 plugin_state() → 各重读一遍 profiles/<id>/config.yaml 并整树 YAML
    # 解析（实测 51 个插件 = 51 次解析 ≈ 2.4s），这是设置页"非常慢"的主因，不是 catalog。
    config = scope.read_agent_config(agent)
    plugin_ids = list(catalog_mod.merged_plugin_ids(agent, config=config))

    # 主目录只解析一次（scope.hermes_home() 每次都要走官方根目录探测 ~1.3ms；逐插件再解析 2 次
    # = 100+ 次白白重复），下面所有元数据读取都用这一份。
    home = scope.hermes_home()

    # **一次目录扫描**代替逐插件 ``is_dir()`` + ``resolve()``（旧实现 51 个插件 = 102 次探测 +
    # 228 次 realpath ≈ 19ms），schema / 版本 / 安装位置全部从这份索引里查。
    scan = _plugin_scan(agent, home=home)

    # catalog 同理：整份 JSON 只读一次，逐插件的条目从这份内存表里取（原来是每个插件读一次文件）。
    catalog = catalog_mod.load_catalog()
    catalog_entries: Mapping[str, Any] = catalog["plugins"]

    schemas: Dict[str, bool] = {}
    items: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    for plugin_id in plugin_ids:
        try:
            state = switch_mod.plugin_state(agent, plugin_id, tri_state=True, config=config)
        except ValueError as exc:
            skipped.append({"plugin_id": str(plugin_id), "reason": str(exc)})
            continue
        name = str(state.get("state") or "")
        if name not in TRISTATE:  # 三态以外的值 = 核心层 bug，宁可报错也不许前端瞎猜
            raise ApiError(
                500,
                CODE_UPSTREAM_ERROR,
                "plugin_state 返回了未知状态 %r（插件 %r）：期望 %s 之一"
                % (name, plugin_id, "/".join(TRISTATE)),
            )
        # catalog 是**元数据**来源（显示名 / 来源 / 版本 / 面板）；三态仍然只认 plugin_switch。
        entry = catalog_entries.get(str(plugin_id))
        metadata: Mapping[str, Any] = entry if isinstance(entry, Mapping) else {}
        # 版本走同一份目录索引 + manifest 签名缓存（原来是每插件整树解析一遍 plugin.yaml）；
        # 索引里彻底找不到（只存在于别的 profile 下）才回退核心模块的官方口径。
        version = _plugin_version(
            str(plugin_id), scan, fallback=getattr(catalog_mod, "read_plugin_version", None)
        )
        module = metadata.get("module") if entry else None
        items.append(
            {
                # ---- M4b0 既有键（前端已依赖，一个不许删）----
                "plugin_id": str(plugin_id),
                "state": name,
                "in_enabled": bool(state.get("in_enabled")),
                "in_disabled": bool(state.get("in_disabled")),
                "source": str(state.get("source") or ""),
                "has_settings": _has_settings(
                    agent, str(plugin_id), config, schemas, home=home, scan=scan
                ),
                # ---- M5a 新增 ----
                # 命名空间：插件与模块的裸 id 可能同名（advisor 插件 vs advisor 模块），前端做 key 用 ref。
                "ref": "plugin:%s" % plugin_id,
                "display_name": str(metadata.get("display_name") or plugin_id),
                "kind": str(metadata.get("kind") or "plugin"),
                "origin": metadata.get("source"),
                "version": version,
                # 版本一律以 plugins/<id>/plugin.yaml 为准（catalog 不写死版本号）。
                "version_source": "plugin.yaml" if version else None,
                "has_dashboard_module": bool(metadata.get("has_dashboard_module")) if entry else False,
                "module": module,
                "module_id": metadata.get("module_id") if entry else None,
                "module_ref": ("module:%s" % module) if module else None,
                "in_catalog": entry is not None,
                "note": metadata.get("note"),
                # 搜索别名（catalog 可选字段）：老板用中文找插件（例：「自动对话命名」），
                # 而插件 id / display_name 常常是英文。缺省空列表，前端缺字段就降级。
                "aliases": _catalog_aliases(metadata.get("aliases")),
            }
        )

    counts: Dict[str, int] = {name: 0 for name in TRISTATE}
    for item in items:
        counts[item["state"]] = counts.get(item["state"], 0) + 1

    try:
        cfg_exists = Path(scope.agent_config_path(agent)).is_file()
    except OSError:
        cfg_exists = False

    # catalog 元信息：坏掉时也要让界面知道"这次没有自制清单，只有本 agent 自己的配置项"。
    # （`catalog` 在本函数开头已读一次，这里不再重读文件。）
    in_catalog_count = sum(1 for item in items if item["in_catalog"])

    notes: List[str] = []
    if not cfg_exists:
        notes.append(
            "该 agent 没有自己的 config.yaml：本 agent 自己的插件清单为空（不是错误），"
            "也不存在可写目标"
        )
    if not catalog["ok"]:
        notes.append(
            "自制插件目录（catalog）不可用，本次清单只含该 agent 自己的配置项：%s"
            % (catalog.get("error") or "未知原因")
        )

    return {
        "agent": agent,
        "plugins": items,
        "counts": counts,
        "skipped": skipped,
        "path": str(scope.agent_config_path(agent)),
        "exists": cfg_exists,
        "config_digest": _config_digest(agent),
        # ---- M5a：命名空间说明（插件与模块的裸 id 可能同名，前端按 ref 做 key）----
        "namespaces": {
            "plugin": "plugin:<plugin_id>",
            "module": "module:<console_module_id>",
            "note": (
                "本响应每一项都是插件（ref 以 plugin: 开头）；控制台模块另见 "
                "GET /agents/{agent}/modules，那里的 id 是裸模块 id，可能与插件 id 同名"
                "（例：advisor 插件 vs advisor 模块），前端做 key 请用 ref / module_ref"
            ),
        },
        "catalog": {
            "ok": bool(catalog["ok"]),
            "path": catalog["path"],
            "schema_version": catalog["schema_version"],
            "entries": len(catalog["plugins"]),
            "used": in_catalog_count,
            "error": catalog["error"],
            "dropped": catalog["dropped"],
        },
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 写入闸门
# ---------------------------------------------------------------------------
def _has_mask_placeholder(value: Any) -> bool:
    """递归找 ``"***"``（字符串精确相等；``"***abc"`` 不算——那是别的值，由白名单/类型管）。"""
    if isinstance(value, str):
        return value == MASK_PLACEHOLDER
    if isinstance(value, Mapping):
        return any(_has_mask_placeholder(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_mask_placeholder(item) for item in value)
    return False


def _readonly_reason(key: str, schema: Mapping[str, Mapping[str, Any]]) -> str:
    """该键为什么不可写（空串 = 可写）。程序常量优先，其次 manifest 声明。"""
    leaf = key.split(".")[-1]
    for constant, reason in PROGRAM_CONSTANTS.items():
        if key == constant or leaf == constant:
            return reason
    spec = schema.get(key)
    if isinstance(spec, Mapping):
        if any(spec.get(flag) is True for flag in _READONLY_FLAGS) or spec.get("writable") is False:
            description = str(spec.get("description") or spec.get("label") or "").strip()
            return (
                "%s 在插件 manifest 的 config_schema 里标了只读（readonly: true）%s"
                % (key, "：" + description if description else "")
            )
    return ""


def _value_type_ok(value: Any, spec: Optional[Mapping[str, Any]], current: Any) -> bool:
    """值类型是否可写。

    * 标量（字符串/数字/布尔/null）一律放行；
    * 容器（dict/list）只在 manifest 明确声明成 json/list **或** 该键当前就是个同类型容器时才放行
      —— 后者是为了\"插件把 list/dict 写进配置但没写 config_schema\"这种真实情况（例如
      ``approved_model_registry: []``），否则用户连原有列表都改不了；
    * 反向不允许：把标量覆盖成 mapping（那是往配置里塞结构，会静默破坏读取方）。
    """
    if not isinstance(value, (dict, list)):
        return True
    kind = str((spec or {}).get("type") or "").lower()
    if kind in _JSON_TYPES:
        return True
    if isinstance(value, dict):
        return isinstance(current, Mapping)
    return isinstance(current, list)


def _parse_payload(payload: Any) -> Tuple[str, Any, Optional[str]]:
    """从请求体取出 ``(key, value, expected_config)``。

    支持两种写法（都只写**单项**）：

    * ``{"key": "retry.max", "value": 5, "expected_config": "<digest>"}``
    * ``{"settings": {"retry.max": 5}, "expected_config": "<digest>"}``（映射里必须恰好一项）
    """
    if not isinstance(payload, Mapping):
        raise ApiError(400, CODE_INVALID_ARGUMENT, "请求体必须是 JSON 对象（{key, value, expected_config}）")

    expected = payload.get("expected_config")
    if expected is not None and not isinstance(expected, str):
        raise ApiError(400, CODE_INVALID_ARGUMENT, "expected_config 必须是字符串（config.yaml 的摘要）")

    if "settings" in payload and "key" not in payload:
        mapping = payload.get("settings")
        if not isinstance(mapping, Mapping):
            raise ApiError(400, CODE_INVALID_ARGUMENT, "settings 必须是 {\"点分键\": 值} 对象")
        if len(mapping) != 1:
            raise ApiError(
                400,
                CODE_INVALID_ARGUMENT,
                "本端点一次只写一项：settings 里必须恰好一个键（收到 %d 个）" % len(mapping),
                settings=sorted(str(key) for key in mapping),
            )
        key, value = next(iter(mapping.items()))
        key = str(key)
    elif "key" in payload:
        key = payload.get("key")
        if not isinstance(key, str):
            raise ApiError(400, CODE_INVALID_ARGUMENT, "key 必须是点分字符串（例如 'retry.max'）")
        if "value" not in payload:
            raise ApiError(400, CODE_INVALID_ARGUMENT, "缺少 value 字段（要写进去的值）")
        value = payload.get("value")
    else:
        raise ApiError(
            400, CODE_INVALID_ARGUMENT, "请求体缺少 key/value（或 settings 映射），没有可写的东西"
        )
    return key, value, expected


def _do_put(agent: str, plugin: str, payload: Any) -> Dict[str, Any]:
    """闸门 → 写 → 二次断言。所有失败都是 :class:`ApiError`（4xx）或 500（写没生效）。"""
    settings_mod = _core("settings")
    scope = _core("scope")

    key, value, expected = _parse_payload(payload)

    # ① 拒掩码：必须在任何其它判断之前——把 *** 写回去等于拿掩码覆盖真密钥。
    if _has_mask_placeholder(value):
        raise ApiError(
            400,
            CODE_MASKED_VALUE_REJECTED,
            "脱敏占位符不能写回：值里的 %r 是界面展示用的掩码，等于拿掩码覆盖真密钥。"
            "要改这一项请提交新的真实值（不改就整项别提交）" % MASK_PLACEHOLDER,
            key=key,
            placeholder=MASK_PLACEHOLDER,
        )

    # ② 键路径文法：走官方同一套校验（拒空段 / '/' / '\\' / 保留根），在摸文件之前拦下。
    text = key.strip()
    if not text or text != key:
        raise ApiError(400, CODE_INVALID_ARGUMENT, "非法的设置键 %r：不能为空、首尾不能有空白" % (key,), key=key)
    segments = tuple(text.split("."))
    if any(not segment for segment in segments):
        raise ApiError(400, CODE_INVALID_ARGUMENT, "非法的设置键 %r：不允许空段（如 'a..b'）" % (key,), key=key)
    try:
        normalized = tuple(settings_mod._normalize_segments(segments))
    except (ValueError, TypeError) as exc:
        raise ApiError(400, CODE_INVALID_ARGUMENT, "非法的设置键 %r：%s" % (key, exc), key=key) from exc

    config_path = Path(scope.agent_config_path(agent))
    try:
        config_exists = config_path.is_file()
    except OSError:
        config_exists = False
    if not config_exists:
        raise ApiError(
            400,
            CODE_CONFIG_MISSING,
            "该 agent 没有自己的 config.yaml（%s）：写不进去。请先在磁盘上创建该 profile 的配置，"
            "或改写到 default agent" % config_path,
            agent=agent,
            path=str(config_path),
        )

    schema = _plugin_schema(agent, plugin)

    # ③ 只读保护：程序常量与 manifest readonly 键一律 400，绝不静默忽略。
    readonly_reason = _readonly_reason(text, schema)
    if readonly_reason:
        raise ApiError(400, CODE_READONLY_SETTING, readonly_reason, key=key, readonly=True)

    # ④ 白名单：只认"文件里已存在"或"manifest 声明过"的键，外加（本插件）registry 声明的模块开关。
    module_layout = _module_switch_layout() if plugin == settings_mod.PLUGIN_ID else {"keys": [], "by_key": {}}
    writable = sorted(
        {
            candidate
            for candidate in list(_raw_keys(agent, plugin)) + list(schema) + list(module_layout["keys"])
            if not _readonly_reason(candidate, schema)
        }
    )
    if text not in writable:
        raise ApiError(
            400,
            CODE_UNKNOWN_SETTING_KEY,
            "未知的设置键 %r：本端点只写该插件已存在的键或 manifest 声明过的键，否则会在配置里造出"
            "没人读的孤儿键。可写键：%s" % (key, ", ".join(writable) or "<none>"),
            key=key,
            writable_keys=writable,
        )

    # ⑤ 值类型：标量放行；容器只在 schema 明说 json/list（或该键当前就是同类型容器）时放行。
    spec = schema.get(text)
    current_value = settings_mod.read_setting(agent, normalized, plugin_id=plugin)
    if not _value_type_ok(value, spec if isinstance(spec, Mapping) else None, current_value):
        raise ApiError(
            400,
            CODE_INVALID_ARGUMENT,
            "键 %r 的值类型不允许：manifest 没有把它声明成 json/list，且它当前不是同类型容器，"
            "只能写标量（字符串/数字/布尔/null），收到 %s" % (key, type(value).__name__),
            key=key,
        )

    # ⑥ 乐观锁：与当前 config.yaml 摘要比对，不一致就 409，此时一个字节都没写。
    digest_before = _config_digest(agent)
    if expected is not None and expected != digest_before:
        raise ApiError(
            409,
            CODE_CONFIG_CHANGED,
            "配置已变化（乐观锁生效）：提交的 expected_config=%r，当前 %s。本次未写入任何内容，"
            "请重新读取后重试" % (expected, digest_before),
            expected_config=expected,
            actual_config=digest_before,
            path=str(config_path),
            unchanged=True,
        )

    # ⑦ 写：只走官方写入器（内部已做\"写后回读\"断言）。
    try:
        result = settings_mod.save_plugin_setting_for(agent, plugin, normalized, value)
    except settings_mod.PluginSettingsWriteError as exc:
        raise ApiError(
            500, CODE_WRITE_NOT_APPLIED, "写入未生效：%s" % exc, key=key, path=str(config_path)
        ) from exc
    except PermissionError as exc:
        raise ApiError(
            403,
            CODE_PERMISSION_DENIED,
            "该设置被托管/管理员键保护，拒绝写入：%s" % exc,
            key=key,
        ) from exc
    except ValueError as exc:
        raise ApiError(400, CODE_INVALID_ARGUMENT, "写入被拒绝：%s" % exc, key=key) from exc

    # ⑧ 接口层再断言一次：不信写函数的自述（退出码/返回值都会撒谎，磁盘上的值不会）。
    try:
        actual = settings_mod.read_setting(agent, normalized, plugin_id=plugin)
    except Exception as exc:  # noqa: BLE001
        raise ApiError(
            500,
            CODE_WRITE_NOT_APPLIED,
            "写入后回读失败，无法确认生效：%s: %s" % (type(exc).__name__, exc),
            key=key,
        ) from exc
    if type(actual) is not type(value) or actual != value:
        raise ApiError(
            500,
            CODE_WRITE_NOT_APPLIED,
            "写入返回成功但回读不一致（期望 %r/%s，实际 %r/%s）：已中止，请人工核对 %s"
            % (value, type(value).__name__, actual, type(actual).__name__, config_path),
            key=key,
            expected=value,
            actual=actual,
        )
    digest_after = _config_digest(agent)
    if digest_after == digest_before:
        raise ApiError(
            500,
            CODE_WRITE_NOT_APPLIED,
            "回读通过但 config.yaml 摘要没变（%s）：写入没有落地" % digest_after,
            key=key,
            path=str(config_path),
        )

    view = _settings_view(agent, plugin)
    return {
        "agent": agent,
        "plugin_id": plugin,
        "key": ".".join(normalized),
        "value": value,
        "previous": result.get("previous"),
        "verified": True,
        "path": str(config_path),
        "config_digest_before": digest_before,
        "config_digest_after": digest_after,
        "settings": view["settings"],
        "masked_keys": view["masked_keys"],
        "sources": view["sources"],
        "notes": ["写入经官方 save_plugin_setting_for，写后已二次回读断言（键存在 + 值相等 + 摘要变化）"],
    }


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.get("/agents/{agent_id:path}/plugins")
def list_agent_plugins(agent_id: str) -> Any:
    """该 agent 的插件三态列表。

    ``GET /agents/xiaomo/plugins`` →
    ``{"ok": true, "data": {"agent","plugins":[{plugin_id,state,in_enabled,in_disabled,source,
    has_settings}], "counts":{"enabled","disabled","unavailable"}, "path", "exists",
    "config_digest", "notes"}}``

    用 ``{agent_id:path}`` 而不是 ``{agent_id}``：路径里的 ``%2F`` 编码段（``..%2F..%2Fetc``）
    会原样进到 handler，于是能返回 **400 + 合法 id 列表**，而不是让路由层 404 掉、让人以为
    \"这个 agent 不存在\"。真正的目录穿越在 :func:`_prepare_agent` 里就被文法拦下。
    """
    try:
        agent = _prepare_agent(agent_id)
        return {"ok": True, "data": _plugin_listing(agent)}
    except ApiError as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("hermes-console.console_settings: 列举插件失败 agent=%r", agent_id)
        return _fail(500, CODE_UPSTREAM_ERROR, "列举插件失败：%s: %s" % (type(exc).__name__, exc))


@router.get("/agents/{agent_id:path}/plugins/{plugin_id:path}/settings")
def get_plugin_settings(agent_id: str, plugin_id: str) -> Any:
    """读该插件 settings：**已脱敏** + 每个键带来源层。

    ``{"ok": true, "data": {"agent","plugin_id","settings","masked_keys","sources",
    "writable_keys","readonly_keys","program_constants","path","exists","config_digest","notes"}}``

    * 值一律经 ``settings.mask_sensitive``；``masked_keys`` 里列出的键，其值必然是 ``"***"``
    * ``sources`` 逐键标 ``module_default`` / ``global`` / ``agent``（界面显示\"这个值来自哪一层\"）
    * ``config_digest`` 就是 ``PUT`` 要的 ``expected_config``，读一次就能带乐观锁去写
    * 该 agent 没有 ``config.yaml`` ⇒ **200 + 空结构**（不是 404：没有覆盖是正常状态，不是缺失）
    """
    try:
        agent = _prepare_agent(agent_id)
        plugin = _prepare_plugin(plugin_id)
        return {"ok": True, "data": _settings_view(agent, plugin)}
    except ApiError as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "hermes-console.console_settings: 读设置失败 agent=%r plugin=%r", agent_id, plugin_id
        )
        return _fail(500, CODE_UPSTREAM_ERROR, "读设置失败：%s: %s" % (type(exc).__name__, exc))


@router.put("/agents/{agent_id:path}/plugins/{plugin_id:path}/settings")
async def put_plugin_setting(agent_id: str, plugin_id: str, request: Request) -> Any:
    """写单项（白名单 + 拒掩码 + 拒只读 + 乐观锁 + 写后回读）。

    请求体：``{"key": "retry.max", "value": 5, "expected_config": "<摘要，可选>"}``，
    或等价的 ``{"settings": {"retry.max": 5}}``（映射里必须恰好一项）。

    请求体用手工解析而不是 ``Body(...)``：FastAPI 的校验失败是 422 且形状不同，
    而本卡要求 **所有** 4xx 都是 ``{ok:false, error:{code,message}}``。
    真正的写入放进线程池（官方写入器会拿跨进程锁，别卡事件循环）。
    """
    try:
        agent = _prepare_agent(agent_id)
        plugin = _prepare_plugin(plugin_id)
    except ApiError as exc:
        return _error_response(exc)

    import json

    raw = await request.body()
    if not raw or not raw.strip():
        return _fail(400, CODE_INVALID_ARGUMENT, "请求体为空：需要一个 JSON 对象 {key, value, expected_config?}")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:  # noqa: BLE001 - 任何解析失败都是 400
        return _fail(
            400, CODE_INVALID_ARGUMENT, "请求体不是合法 JSON：%s: %s" % (type(exc).__name__, exc)
        )

    try:
        data = await run_in_threadpool(_do_put, agent, plugin, payload)
        return {"ok": True, "data": data}
    except ApiError as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "hermes-console.console_settings: 写设置失败 agent=%r plugin=%r", agent, plugin
        )
        return _fail(500, CODE_UPSTREAM_ERROR, "写设置失败：%s: %s" % (type(exc).__name__, exc))


@router.put("/agents/{agent_id:path}/plugins/{plugin_id:path}")
async def put_plugin_enabled(agent_id: str, plugin_id: str, request: Request) -> Any:
    """开关一个插件（per-agent 三态）：``enabled=true`` 启用 / ``false`` 禁用。

    请求体：``{"enabled": <bool>, "expected_config": "<该 agent config.yaml 摘要，可选>"}``
    成功 200：``{"ok": true, "data": {... set_plugin_enabled 的返回 ..., "state_before",
    "state_after", "steps", "verified"}}``

    这是 M4b1 设置界面「开启 / 关闭」按钮打的那一条（前端早就实现好，只是后端没有它，
    实测 404）。**注册顺序很关键**：本路由必须在 ``.../plugins/{plugin_id:path}/settings``
    **之后**注册，否则 ``{plugin_id:path}`` 会把 ``browserpaw/settings`` 整个吃进 plugin_id，
    写设置端点就永远匹配不到。

    本函数只做 HTTP 闸门：三态判定、黑名单优先、``unavailable → enabled`` 的官方准入全在
    ``core.plugin_switch.set_plugin_enabled`` 里，本层**一个字节的名单都不拼**。
    写入放进线程池：准入通道在需要重建依赖环境时是分钟级且会联网，别卡事件循环。
    """
    try:
        agent = _prepare_agent(agent_id)
        plugin = _prepare_plugin(plugin_id)
    except ApiError as exc:
        return _error_response(exc)

    import json

    raw = await request.body()
    if not raw or not raw.strip():
        return _fail(
            400, CODE_INVALID_ARGUMENT, "请求体为空：需要一个 JSON 对象 {enabled, expected_config?}"
        )
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:  # noqa: BLE001 - 任何解析失败都是 400
        return _fail(
            400, CODE_INVALID_ARGUMENT, "请求体不是合法 JSON：%s: %s" % (type(exc).__name__, exc)
        )

    try:
        data = await run_in_threadpool(_do_toggle, agent, plugin, payload)
        return {"ok": True, "data": data}
    except ApiError as exc:
        return _error_response(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "hermes-console.console_settings: 启停插件失败 agent=%r plugin=%r", agent, plugin
        )
        return _fail(500, CODE_UPSTREAM_ERROR, "启停插件失败：%s: %s" % (type(exc).__name__, exc))


def _parse_toggle_payload(payload: Any) -> Tuple[bool, Optional[str]]:
    """从请求体取出 ``(enabled, expected_config)``。

    只接受 ``{"enabled": <真 bool>, "expected_config": "<摘要，可选>"}``：

    * ``enabled`` 必须是**真 bool**（``"false"`` / ``0`` / ``1`` 一律 400）——官方
      ``plugin_switch._require_bool`` 也是这个口径，在 API 层先拦能给出更清楚的错误体；
    * ``expected_config`` 必须是字符串（``None`` = 不做乐观锁校验）。
    """
    if not isinstance(payload, Mapping):
        raise ApiError(
            400, CODE_INVALID_ARGUMENT, "请求体必须是 JSON 对象（{enabled, expected_config?}）"
        )
    if "enabled" not in payload:
        raise ApiError(
            400,
            CODE_INVALID_ARGUMENT,
            "请求体缺少 enabled 字段（True/False）：本端点只做启停，不做部分更新",
        )
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise ApiError(
            400,
            CODE_INVALID_ARGUMENT,
            "enabled 必须是布尔值（true/false），收到 %r（字符串 'false' 不算假：那是另一回事）"
            % (enabled,),
            enabled=enabled,
        )
    expected = payload.get("expected_config")
    if expected is not None and not isinstance(expected, str):
        raise ApiError(
            400, CODE_INVALID_ARGUMENT, "expected_config 必须是字符串（该 agent config.yaml 的摘要）"
        )
    return enabled, expected


def _read_state(switch_mod: Any, agent: str, plugin: str) -> Dict[str, Any]:
    """回读三态（**只认** ``plugin_switch.plugin_state``，本层不自己判）。"""
    state = switch_mod.plugin_state(agent, plugin, tri_state=True)
    name = str(state.get("state") or "")
    if name not in TRISTATE:
        raise ApiError(
            500,
            CODE_UPSTREAM_ERROR,
            "plugin_state 返回了未知状态 %r（agent=%r plugin=%r）：期望 %s 之一"
            % (name, agent, plugin, "/".join(TRISTATE)),
        )
    return state


def _toggle_once(
    switch_mod: Any, agent: str, plugin: str, want: bool, expected: Optional[str]
) -> Dict[str, Any]:
    """调**一次** ``set_plugin_enabled``（唯一写入口，`strict=True`），把异常翻成 ApiError。"""
    try:
        return switch_mod.set_plugin_enabled(
            agent,
            plugin,
            want,
            strict=True,
            expected_config=expected,
        )
    except switch_mod.PluginSwitchAdmissionError as exc:
        # 准入通道用不了：**绝不**退化成"只摘黑名单"，如实报错。
        raise ApiError(
            503,
            getattr(exc, "code", None) or CODE_ADMISSION_UNAVAILABLE,
            "官方准入通道不可用，本次没有改动任何配置（不会退化成只摘黑名单）：%s" % exc,
            agent=agent,
            plugin_id=plugin,
        ) from exc
    except switch_mod.PluginSwitchRefused as exc:
        raise ApiError(
            400,
            getattr(exc, "code", None) or CODE_ADMISSION_REFUSED,
            "官方准入权威拒绝了这个插件：%s" % exc,
            agent=agent,
            plugin_id=plugin,
            refused=True,
        ) from exc
    except switch_mod.PluginSwitchWriteError as exc:
        raise ApiError(
            500,
            CODE_WRITE_NOT_APPLIED,
            "写入未生效（写后回读与预期不符）：%s" % exc,
            agent=agent,
            plugin_id=plugin,
        ) from exc
    except PermissionError as exc:
        # 托管安装 / 管理员托管键：官方 fail-closed 抛 PermissionError，这里原样透出成 403。
        raise ApiError(
            403,
            CODE_PERMISSION_DENIED,
            "该插件开关被托管/管理员键保护，拒绝写入：%s" % exc,
            agent=agent,
            plugin_id=plugin,
        ) from exc
    except ValueError as exc:
        raise ApiError(400, CODE_INVALID_ARGUMENT, "启停请求被拒绝：%s" % exc) from exc


def _do_toggle(agent: str, plugin: str, payload: Any) -> Dict[str, Any]:
    """启停闸门 → ``set_plugin_enabled`` → 回读三态断言。

    分工（与 M4b0 的 ``_do_put`` 同口径）：

    1. **入参**（``enabled`` 真 bool / ``expected_config`` 字符串）；
    2. 该 agent 没有 ``config.yaml`` ⇒ 400 ``config_missing``（与写设置端点一致，不自己去造文件）；
    3. **乐观锁**：``expected_config`` 与**该 agent 自己**的摘要比对，不一致 ⇒ 409 且零写入；
    4. 写：**只**调 ``core.plugin_switch.set_plugin_enabled(strict=True)``——三态判定、黑名单优先、
       ``unavailable → enabled`` 的官方准入全在核心里，本层一个字节的名单都不拼；
    5. **写后回读**三态并断言结果，不满足 ⇒ 500（不信返回值自述）。
    """
    switch_mod = _core("plugin_switch")
    scope = _core("scope")

    want, expected = _parse_toggle_payload(payload)

    config_path = Path(scope.agent_config_path(agent))
    try:
        config_exists = config_path.is_file()
    except OSError:
        config_exists = False
    if not config_exists:
        raise ApiError(
            400,
            CODE_CONFIG_MISSING,
            "该 agent 没有自己的 config.yaml（%s）：启停开关要写进这份配置，写不进去。"
            "请先在磁盘上创建该 profile 的配置，或改到 default agent" % config_path,
            agent=agent,
            path=str(config_path),
        )

    state_before = _read_state(switch_mod, agent, plugin)
    digest_before = _config_digest(agent)

    # ③ 乐观锁：先比摘要再动配置。不符 ⇒ 409 且此时一个字节都没写。
    if expected is not None and expected != digest_before:
        raise ApiError(
            409,
            CODE_CONFIG_CHANGED,
            "配置已变化（乐观锁生效）：提交的 expected_config=%r，当前 %s。本次未写入任何内容，"
            "请重新读取后重试" % (expected, digest_before),
            expected_config=expected,
            actual_config=digest_before,
            path=str(config_path),
            unchanged=True,
        )

    # ④ 写：唯一入口。``strict=True`` —— 准入通道不可用就报错，绝不退化成只摘黑名单。
    result = _toggle_once(switch_mod, agent, plugin, want, expected)
    steps: List[Dict[str, Any]] = [
        {
            "step": 1,
            "requested_enabled": want,
            "changed": bool(result.get("changed")),
            "via": result.get("via"),
            "state_before": result.get("state_before"),
            "state_after": result.get("state_after"),
        }
    ]

    if result.get("ok") is False and result.get("refused"):
        # 官方准入**拒绝**（核心按 M2c 口径返回结构化结果而不是抛异常）：原样透出 code/message，
        # 并当场断言"拒绝路径零写入"——官方的契约是配置与环境一个字节都不动，真变了就是硬故障。
        digest_after = _config_digest(agent)
        if digest_after != digest_before:
            raise ApiError(
                500,
                CODE_WRITE_NOT_APPLIED,
                "官方准入拒绝了本次修改，但配置摘要变了（%s -> %s）：拒绝路径必须零写入"
                % (digest_before, digest_after),
                agent=agent,
                plugin_id=plugin,
            )
        raise ApiError(
            400,
            str(result.get("code") or CODE_ADMISSION_REFUSED),
            str(result.get("message") or "官方准入权威拒绝了这个插件"),
            agent=agent,
            plugin_id=plugin,
            refused=True,
            state_before=result.get("state_before"),
            state_after=result.get("state_after"),
            unchanged=True,
        )

    # ④b 「开启」的第二段：**不做**。M2 的既定协议是——插件在黑名单里、但不在允许清单里时，
    #     开启只把它从黑名单摘掉，然后如实返回 `warning`（原文见
    #     ``plugin_switch.WARN_NOT_IN_ALLOW_LIST``），回读三态是 ``unavailable``。
    #     这里**不再自动补一次准入**：那会顺手改写 ``plugins.enabled`` 整表，违反本卡
    #     "``enabled`` 整表不许被顺手改写"的硬要求。真要启用一个既不在允许清单也不在黑名单的
    #     插件（``unavailable``），用户再点一次开启 —— 那一次才是 ``unavailable → enabled``，
    #     由核心走官方准入通道。本层不替用户跨过这一步。

    # ⑤ 写后回读：三态只认 plugin_state，**不**信返回值自述。
    #    "不符" 指的是**核心自述的 state_after 与磁盘回读不一致**（那才是故障）；至于
    #    "用户要求 enabled，但只从黑名单放开后仍是 unavailable"，那是 M2 既定的诚实结果
    #    （带 warning），不许被本层改写成 500、更不许被本层悄悄补成 enabled。
    state_after = _read_state(switch_mod, agent, plugin)
    claimed_after = str(result.get("state_after") or "")
    if claimed_after != state_after["state"]:
        raise ApiError(
            500,
            CODE_WRITE_NOT_APPLIED,
            "写入返回的 state_after=%r 与磁盘回读 %r 不一致：agent=%r plugin=%r，请人工核对 %s"
            % (claimed_after, state_after["state"], agent, plugin, config_path),
            agent=agent,
            plugin_id=plugin,
            state_before=state_before["state"],
            state_after=state_after["state"],
            claimed_state_after=claimed_after,
        )
    if bool(result.get("enabled")) != bool(state_after.get("enabled")):
        raise ApiError(
            500,
            CODE_WRITE_NOT_APPLIED,
            "写入返回的 enabled=%r 与回读 %r 不一致：agent=%r plugin=%r"
            % (result.get("enabled"), state_after.get("enabled"), agent, plugin),
            agent=agent,
            plugin_id=plugin,
        )
    digest_after = _config_digest(agent)
    if result.get("changed") and digest_after == digest_before:
        raise ApiError(
            500,
            CODE_WRITE_NOT_APPLIED,
            "回读通过但 config.yaml 摘要没变（%s）：写入没有落地" % digest_after,
            agent=agent,
            plugin_id=plugin,
            path=str(config_path),
        )
    if not result.get("changed") and digest_after != digest_before:
        raise ApiError(
            500,
            CODE_WRITE_NOT_APPLIED,
            "核心自称 changed=False，但 config.yaml 摘要变了（%s -> %s）：可疑写入，已中止"
            % (digest_before, digest_after),
            agent=agent,
            plugin_id=plugin,
            path=str(config_path),
        )

    data: Dict[str, Any] = dict(result)
    data.update(
        {
            "agent": agent,
            "plugin_id": plugin,
            "requested_enabled": want,
            "state_before": state_before["state"],
            "state_after": state_after["state"],
            "state_before_detail": {
                "enabled": state_before.get("enabled"),
                "in_enabled": state_before.get("in_enabled"),
                "in_disabled": state_before.get("in_disabled"),
                "source": state_before.get("source"),
            },
            "state_after_detail": {
                "enabled": state_after.get("enabled"),
                "in_enabled": state_after.get("in_enabled"),
                "in_disabled": state_after.get("in_disabled"),
                "source": state_after.get("source"),
            },
            "verified": True,
            "path": str(config_path),
            "config_digest_before": digest_before,
            "config_digest_after": digest_after,
            "steps": steps,
        }
    )
    if "notes" not in data:
        data["notes"] = []
    else:
        data["notes"] = list(data["notes"])
    data["notes"].append(
        "启停只经 core.plugin_switch.set_plugin_enabled(strict=True)：黑名单优先、plugins.enabled 不被改写，"
        "unavailable→enabled 走官方准入；写后已按 plugin_state 回读断言"
    )
    return data


def _host_plugin_api() -> Any:
    """找到加载本模块的那个 ``plugin_api.py`` 模块对象（用于**转发** ``/modules``）。

    dashboard 用 ``spec_from_file_location("hermes_dashboard_plugin_<name>", api_path)`` 加载它，
    模块名不可预知，但 ``__file__`` 是这条路径，所以按文件路径在 ``sys.modules`` 里认领。
    """
    api_path = _DASHBOARD_DIR / "plugin_api.py"
    try:
        wanted = api_path.resolve()
    except OSError:
        return None
    for module in list(sys.modules.values()):
        found = getattr(module, "__file__", None)
        if not found:
            continue
        try:
            if Path(found).resolve() != wanted:
                continue
        except OSError:
            continue
        return module
    return None


@router.get("/agents/{agent_id:path}/modules")
def list_agent_modules(agent_id: str) -> Any:
    """该 agent 启用模块 —— **薄封装转发** ``GET /modules?agent=<id>``。

    这个能力 ``plugin_api.list_modules`` 已经有了（含 ``enabled`` / ``reason`` / ``health``），
    所以这里**不重复实现**：只做 agent 校验（非法 → 400 + 合法 id 列表，与其它端点一致），
    然后把请求转给那个处理函数，响应原样返回。

    ``plugin_api`` 没被加载（例如本模块被单独 import 做单测）⇒ 503 结构化错误并说明原因，
    而不是悄悄返回一份自己拼的假数据。
    """
    try:
        agent = _prepare_agent(agent_id)
    except ApiError as exc:
        return _error_response(exc)
    host = _host_plugin_api()
    handler = getattr(host, "list_modules", None) if host is not None else None
    if not callable(handler):
        return _fail(
            503,
            CODE_UPSTREAM_ERROR,
            "无法转发到 /modules：聚合入口 plugin_api.py 未加载（本模块被单独 import 时会出现，"
            "dashboard 正常挂载时不会）",
        )
    return handler(agent=agent)


__all__ = [
    "CODE_ADMISSION_REFUSED",
    "CODE_ADMISSION_UNAVAILABLE",
    "CODE_CONFIG_CHANGED",
    "CODE_CONFIG_MISSING",
    "CODE_MASKED_VALUE_REJECTED",
    "CODE_READONLY_SETTING",
    "CODE_UNKNOWN_SETTING_KEY",
    "CODE_WRITE_NOT_APPLIED",
    "MASK_PLACEHOLDER",
    "PROGRAM_CONSTANTS",
    "TRISTATE",
    "router",
]

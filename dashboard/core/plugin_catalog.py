# -*- coding: utf-8 -*-
"""hermes-console · 自制插件目录（catalog，M5a）。

数据源是**同目录的 JSON**（``dashboard/plugins_catalog.json``）——不是 .py 里的清单，老板要能
自己加减插件而不改代码。

设置界面为什么要它：``GET /agents/{id}/plugins`` 过去的清单只有 ``enabled ∪ disabled ∪ entries``
（``settings.list_plugin_ids``），于是「本 agent 名单里没有的自制插件」完全看不见，也就没法给它
「启用」——功能断裂。M5a 把清单改成 ``catalog ∪ enabled ∪ disabled ∪ entries``：
:func:`merged_plugin_ids` 就是那一步。

**失败语义：catalog 坏掉不能拖垮设置界面。** JSON 缺失 / 非法 / 结构不对一律返回空目录 + 原因，
绝不抛异常；catalog 为空时 :func:`merged_plugin_ids` 仍然返回该 agent 自己配置里的插件。

``home`` 参数两处用途：

* :func:`read_plugin_version` —— 定位 ``<home>/plugins/<id>/plugin.yaml``（``home=None`` 时用
  :func:`core.scope.hermes_home`，即进程所在部署）
* :func:`load_catalog` —— 部署级覆盖：``<home>/plugins_catalog.json`` 存在就用它，否则用随插件
  打包的那份 ``dashboard/plugins_catalog.json``

本模块**全部函数只读**：不写文件、不改配置、不 import 官方写入器。
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .scope import hermes_home
from .settings import PLUGIN_ID_RE, list_plugin_ids

logger = logging.getLogger(__name__)

#: catalog 文件名（打包份与部署覆盖共用同一个名字）。
CATALOG_FILENAME = "plugins_catalog.json"

#: ``(路径, mtime_ns, size) -> version``：plugin.yaml 的版本号是元数据，按文件签名记忆即可。
#: 设置页一次列举会问 51 个插件（每个还要试根目录 + 8 个 profile 目录），不缓存就是几百次 stat
#: 加几十次 YAML 解析 —— 实测那是修完"每插件重读 config.yaml"之后剩下的第二大开销。
_VERSION_CACHE: Dict[Any, Optional[str]] = {}
_VERSION_CACHE_LOCK = threading.Lock()
_VERSION_CACHE_MAX = 2048

#: 随插件打包的 catalog：``<plugin root>/dashboard/plugins_catalog.json``。
BUNDLED_CATALOG = Path(__file__).resolve().parents[1] / CATALOG_FILENAME


def catalog_path(home: Optional[Path] = None) -> Path:
    """catalog 文件位置：``<home>/plugins_catalog.json`` 优先（部署覆盖），否则用打包份。

    ``home`` 为空时用 :func:`core.scope.hermes_home` 解析（即进程所在部署，``HERMES_HOME`` 生效）
    ——**不能**直接跳到打包份，否则部署级覆盖永远读不到。
    """
    try:
        root = hermes_home(home)
    except Exception:  # noqa: BLE001 - home 解析不了也只会退到打包份
        return BUNDLED_CATALOG
    override = root / CATALOG_FILENAME
    try:
        if override.is_file():
            return override
    except OSError:
        pass
    return BUNDLED_CATALOG


def load_catalog(home: Optional[Path] = None) -> Dict[str, Any]:
    """读 catalog。**任何失败都返回空目录 + 原因，绝不抛异常。**

    返回 ``{"ok", "schema_version", "plugins", "path", "error", "dropped"}``：

    * ``ok=False`` + ``error`` 非空 ⇒ catalog 不可用（缺失 / JSON 坏 / 根不是对象 / 没有 plugins
      对象），此时 ``plugins`` 是 ``{}``
    * ``dropped``：id 不合 :data:`settings.PLUGIN_ID_RE` 文法而被忽略的键（不静默丢弃）
    """
    path = catalog_path(home)
    result: Dict[str, Any] = {
        "ok": False,
        "schema_version": None,
        "plugins": {},
        "path": str(path),
        "error": None,
        "dropped": [],
    }

    try:
        raw_text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        result["error"] = "catalog 文件不存在：%s" % path
        return result
    except OSError as exc:
        result["error"] = "catalog 不可读（%s）：%s" % (path, exc)
        logger.warning("hermes-console.plugin_catalog: %s", result["error"])
        return result
    except Exception as exc:  # noqa: BLE001 - 解码之类的意外也不许冒泡
        result["error"] = "catalog 读取失败（%s）：%s: %s" % (path, type(exc).__name__, exc)
        logger.warning("hermes-console.plugin_catalog: %s", result["error"])
        return result

    try:
        data: Any = json.loads(raw_text)
    except Exception as exc:  # noqa: BLE001 - JSON 坏 = 空目录 + 原因
        result["error"] = "catalog JSON 解析失败（%s）：%s: %s" % (path, type(exc).__name__, exc)
        logger.warning("hermes-console.plugin_catalog: %s", result["error"])
        return result

    if not isinstance(data, Mapping):
        result["error"] = "catalog 根不是对象（%s）：%s" % (type(data).__name__, path)
        return result

    raw_plugins = data.get("plugins")
    if not isinstance(raw_plugins, Mapping):
        result["error"] = "catalog 缺 'plugins' 对象：%s" % path
        return result

    plugins: Dict[str, Dict[str, Any]] = {}
    dropped: List[str] = []
    for key, value in raw_plugins.items():
        text = str("" if key is None else key).strip()
        if not text or not PLUGIN_ID_RE.match(text) or not isinstance(value, Mapping):
            dropped.append(str(key))
            continue
        plugins[text] = dict(value)

    schema_version = data.get("schema_version")
    result.update(
        {
            "ok": True,
            "schema_version": schema_version if isinstance(schema_version, int) else None,
            "plugins": plugins,
            "dropped": sorted(dropped),
        }
    )
    return result


def catalog_ids(home: Optional[Path] = None) -> List[str]:
    """catalog 里全部插件 id（已排序）。catalog 坏掉时是 ``[]``。"""
    return sorted(load_catalog(home=home)["plugins"])


def catalog_entry(plugin_id: Any, home: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """catalog 里某插件的条目（深拷贝）；不在 catalog / id 非法 ⇒ ``None``（不猜）。"""
    text = str("" if plugin_id is None else plugin_id).strip()
    if not PLUGIN_ID_RE.match(text):
        return None
    entry = load_catalog(home=home)["plugins"].get(text)
    if not isinstance(entry, Mapping):
        return None
    return copy.deepcopy(dict(entry))


def _read_version_from(manifest: Path) -> Optional[str]:
    """从单个 ``plugin.yaml`` 读 ``version``；文件缺失 / YAML 坏 / 无版本 ⇒ ``None``。

    结果按 ``(路径, mtime_ns, size)`` 记忆：``version`` 是**元数据**，只在插件安装 / 升级时才变，
    而设置页一次列举要问 51 个插件的版本（每个还要试 ``<home>/plugins`` 与 8 个 profile 目录）。
    缓存键含 mtime + size，插件一换文件立刻失效，不会返回旧版本号。
    """
    try:
        stat = manifest.stat()
    except OSError:
        return None
    key = (str(manifest), stat.st_mtime_ns, stat.st_size)
    with _VERSION_CACHE_LOCK:
        if key in _VERSION_CACHE:
            return _VERSION_CACHE[key]
    version = _parse_version(manifest)
    with _VERSION_CACHE_LOCK:
        if len(_VERSION_CACHE) > _VERSION_CACHE_MAX:
            _VERSION_CACHE.clear()
        _VERSION_CACHE[key] = version
    return version


def _parse_version(manifest: Path) -> Optional[str]:
    """真正去读文件的那一步（结果由 :func:`_read_version_from` 缓存）。"""
    try:
        import yaml

        with open(manifest, encoding="utf-8-sig") as handle:
            data: Any = yaml.safe_load(handle)
    except Exception:  # noqa: BLE001 - 没有 manifest / yaml 坏 / 变量没声明 → 没有版本
        return None
    if not isinstance(data, Mapping):
        return None
    version = data.get("version")
    if isinstance(version, bool):  # bool 是 int 的子类，别把 True 当版本号
        return None
    if isinstance(version, (int, float)):
        return str(version)
    if isinstance(version, str) and version.strip():
        return version.strip()
    return None


def read_plugin_version(plugin_id: Any, home: Optional[Path] = None) -> Optional[str]:
    """读插件的 ``version``；读不到 ⇒ ``None``。

    查找顺序（**必须两处都查**）：

    1. 部署根 ``<home>/plugins/<id>/plugin.yaml``
    2. 各 profile 的 ``<home>/profiles/*/plugins/<id>/plugin.yaml``（按名字排序取第一个有版本的）

    为什么要有第 2 步：不少自制插件**只存在于某个 profile 下**（如 ``apm-worktime`` 只在
    ``profiles/apm-worker``、``xiaomo-vitality`` 只在 ``profiles/xiaomo``），根目录没有副本。
    只查根目录会让这些插件在界面上显示不出真实版本。

    **不在本模块写死任何版本号**：插件自己声明的 version 才是准。
    id 先过 :data:`settings.PLUGIN_ID_RE`（无 ``..`` / 无反斜杠 / 无空段），拼接出不了目录。
    """
    text = str("" if plugin_id is None else plugin_id).strip()
    if not PLUGIN_ID_RE.match(text):
        return None
    segments = text.split("/")
    root = hermes_home(home)

    version = _read_version_from(root.joinpath("plugins", *segments, "plugin.yaml"))
    if version is not None:
        return version

    profiles_root = root / "profiles"
    if profiles_root.is_dir():
        for profile_dir in sorted(p for p in profiles_root.iterdir() if p.is_dir()):
            version = _read_version_from(
                profile_dir.joinpath("plugins", *segments, "plugin.yaml"))
            if version is not None:
                return version
    return None


def merged_plugin_ids(
    agent_id: str,
    home: Optional[Path] = None,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> List[str]:
    """设置界面的插件清单 = **``catalog ∪ enabled ∪ disabled ∪ entries``**（去重排序）。

    catalog 为空（文件缺失 / JSON 坏）时退化成 ``enabled ∪ disabled ∪ entries``——catalog 坏不该
    让整张清单消失。返回的 id 都过文法校验（配置里可能有历史垃圾）。

    ``config``：已经读好的该 agent 配置（批量路径传它，省掉一次整树 YAML 解析）。
    """
    merged: Dict[str, None] = {}
    candidates: List[Any] = list(catalog_ids(home=home))
    candidates.extend(list_plugin_ids(agent_id, home=home, config=config))
    for candidate in candidates:
        text = str("" if candidate is None else candidate).strip()
        if text and PLUGIN_ID_RE.match(text):
            merged.setdefault(text, None)
    return sorted(merged)


__all__ = [
    "BUNDLED_CATALOG",
    "CATALOG_FILENAME",
    "catalog_entry",
    "catalog_ids",
    "catalog_path",
    "load_catalog",
    "merged_plugin_ids",
    "read_plugin_version",
]
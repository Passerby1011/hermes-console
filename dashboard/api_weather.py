# -*- coding: utf-8 -*-
"""hermes-console · 天气分区后端（搬自 ``plugins/weather-card/dashboard/plugin_api.py``）。

挂载契约：本文件不单独挂载。``plugin_api.py``（manifest 的 ``api`` 键指向的文件）把本目录
加进 ``sys.path`` 后 import 本模块，再 ``include_router(api_weather.router)``；最终 URL 是
``/api/plugins/hermes-console/weather/...``。

路由
====  ==============================  ==========================================
方法  路径                            说明
====  ==============================  ==========================================
GET   ``/weather/now``                实况 + 今日/明日 + 分钟级降水 + 预警
GET   ``/weather/daily``              多日预报（2–16 天，默认 7）
GET   ``/weather/hourly``             逐小时（3–48 小时，默认 24）
GET   ``/weather/at``                 「N 小时后」一句中文（工具原话，不加工）
GET   ``/weather``                    分区自检（路由清单 / 数据源 / 默认城市）
====  ==============================  ==========================================

数据源与降级顺序**原样沿用** ``scripts/weather_lib.py``（彩云 → 和风 → Open-Meteo），本文件
不改数据源、不改降级顺序、不改任何取数语义，只做三件事：

1. **显式标出来源与取数时间**。旧 ``plugin_api.py`` 只在实况里带一个 ``source``，逐日/逐时
   完全不说是哪家给的，前端没法回答"这数据哪来的"。这里在**每条响应**上给
   ``source``（机读 slug）/``source_label``（人读中文）/``fetched_at``（取数时刻）/
   ``sources``（三个源各自有没有命中、调了几次）。
2. **来源不靠猜**。``weather_lib`` 的 ``Location.daily()/hourly()`` 只返回列表、不返回来源，
   所以这里在进程内给它的三个单源函数套一层**只读探针**（记录返回值，不改语义），
   事后按降级顺序 + 返回体里该有的键（``daily``/``hourly``/``realtime``）判定到底是谁命中的。
   实况另有一条更硬的证据：``Location.realtime()`` 自己就返回 ``source`` 字段，优先用它。
3. **不裸抛**。单源失败 / 全部失败 / 地点解析不了 / 超时，全部转成
   ``{ok:false, error:{code, message}}``：数据源不可用是 HTTP 200（面板照常渲染错误态，
   不会把整个插件打成 500），只有地点参数非法才 400。

两条口径说明（写清楚，避免被当成"猜值"）：

* ``pm25`` 只在**彩云命中**时才有（彩云 ``realtime.air_quality.pm25``，单位 μg/m³）。
  和风 ``weather/now`` 与 Open-Meteo 当前这次请求都不含颗粒物，**拿不到就不给这个键**，
  前端显示 ``—``，不填 0。
* ``aqi_chn`` 是**空气质量指数**（CN 标准），不是 PM2.5 浓度。旧
  ``weather_lib.summary()`` 把它印成 "PM2.5"，那是错标；本模块把两者分开给。

只读约束：本文件**不写** ``plugins/weather/``（原生工具插件）、**不写**
``plugins/weather-card/``（旧面板，只做参考）。彩云 key 由 ``weather_lib`` 自己从
``.env``/环境读，本文件不碰任何密钥。
"""
from __future__ import annotations

import asyncio
import datetime
import importlib.util
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Query  # type: ignore
from fastapi.responses import JSONResponse  # type: ignore

router = APIRouter()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 默认地点。**不是硬编码**：``家`` 是 ``weather_lib.CITY_TABLE`` 里的键，解析结果由
#: 部署方配置决定（``HERMES_WEATHER_PLACE`` 可设为城市名或「纬度,经度」），本文件不写任何
#: 具体地点与坐标；改默认值只需设该环境变量，不用改代码。
#: 面板上还有输入框，任何城市/坐标都能临时查。
DEFAULT_PLACE = (os.environ.get("HERMES_WEATHER_PLACE") or "家").strip() or "家"

#: 单位表（前端直接用，避免三处各写一遍 "℃"/"C"/"度"）。
UNITS: Dict[str, str] = {
    "temp": "℃",
    "humidity": "%",
    "wind": "km/h",
    "pm25": "μg/m³",
    "pop": "%",
    "aqi": "",
}

#: weather_lib 里三个单源函数 → 机读 slug / 人读标签。顺序 = 降级顺序。
SOURCE_SLUG: Dict[str, str] = {
    "fetch_caiyun": "caiyun",
    "hf_get": "qweather",
    "fetch_openmeteo": "open-meteo",
}
SOURCE_LABEL: Dict[str, str] = {
    "caiyun": "彩云天气",
    "qweather": "和风天气",
    "open-meteo": "Open-Meteo",
}
#: ``Location.realtime()`` 自带的 ``source`` 是中文（"彩云"/"和风"/"Open-Meteo"），归一成 slug。
LIB_SOURCE_TO_SLUG: Dict[str, str] = {
    "彩云": "caiyun",
    "和风": "qweather",
    "Open-Meteo": "open-meteo",
}

#: 各路线的取数超时（秒）。旧 ``plugin_api.py`` 一个都没有：网络卡住时面板会一直转，
#: 没有任何确定的结束点。这里按「实况是主体，其余是附加」分档，附加项超时只让那一块变
#: ``—``，不拖死整张卡。
NOW_REALTIME_TIMEOUT_S = 12.0
NOW_EXTRA_TIMEOUT_S = 8.0
NOW_MINOR_TIMEOUT_S = 5.0
DAILY_TIMEOUT_S = 12.0
HOURLY_TIMEOUT_S = 12.0
AT_TIMEOUT_S = 15.0

#: 参数范围（与旧实现一致，避免"读得进写不出"）。
DAILY_MIN, DAILY_MAX, DAILY_DEFAULT = 2, 16, 7
HOURLY_MIN, HOURLY_MAX, HOURLY_DEFAULT = 3, 48, 24
AT_MIN, AT_MAX, AT_DEFAULT = 1, 168, 24
PLACE_MAX_LEN = 64


# ---------------------------------------------------------------------------
# weather_lib 定位与加载（惰性 + 缓存；失败只坏天气分区，不坏整个插件）
# ---------------------------------------------------------------------------
_WL: Any = None
_WL_PATH: str = ""
_WL_ERROR: str = ""


def _hermes_home() -> Path:
    """Hermes 主目录（复用 api_agents 的正向探测，失败再退环境变量）。"""
    try:
        from api_agents import hermes_home  # 同目录兄弟模块（plugin_api.py 已加 sys.path）

        return hermes_home()
    except Exception:  # noqa: BLE001 - 兄弟模块缺失时退化，不阻断天气分区
        env_home = (os.environ.get("HERMES_HOME") or "").strip()
        if env_home:
            candidate = Path(env_home)
            if candidate.parent.name == "profiles":
                return candidate.parent.parent
            return candidate
        return Path.home() / ".hermes"


def _weather_lib_candidates() -> List[Path]:
    """``weather_lib.py`` 的候选路径，按优先级。

    为什么有多个：旧 ``weather-card/dashboard/plugin_api.py`` 把 ``D:/hermes-data/hermes/scripts``
    **写死**进 ``sys.path``（换机器就崩）。这里先按主目录正向探测，再退到旧路径，
    且**只读**这些文件。
    """
    out: List[Path] = []
    env_lib = (os.environ.get("HERMES_WEATHER_LIB") or "").strip()
    if env_lib:
        p = Path(env_lib)
        out.append(p if p.suffix == ".py" else p / "weather_lib.py")
    home = _hermes_home()
    out.append(home / "scripts" / "weather_lib.py")
    out.append(home / "plugins" / "weather" / "weather_lib.py")
    # 兜底：显式 HERMES_HOME。**不再写死任何机器绝对路径** —— 原实现最后一条是
    # ``Path("D:/hermes-data/hermes/scripts/weather_lib.py")``，换设备后它指向不存在的盘符，
    # 只是靠「候选路径不存在就跳过」侥幸不崩，但会把可移植性写成假的。
    env_home = (os.environ.get("HERMES_HOME") or "").strip()
    if env_home:
        out.append(Path(env_home) / "scripts" / "weather_lib.py")
    return out


def _load_weather_lib() -> Any:
    """加载 ``weather_lib``（进程内只加载一次）。失败抛 ``ImportError``（调用方转结构化错误）。"""
    global _WL, _WL_PATH, _WL_ERROR
    if _WL is not None:
        return _WL
    # 1) 已经能按裸名 import（旧插件或工具插件先加载过同一份库）→ 直接复用，避免两份模块
    existing = sys.modules.get("weather_lib")
    if existing is not None and getattr(existing, "Location", None) is not None:
        _WL, _WL_PATH = existing, str(getattr(existing, "__file__", "") or "")
        _install_probe(_WL)
        return _WL
    # 2) 按候选路径显式加载
    tried: List[str] = []
    for candidate in _weather_lib_candidates():
        tried.append(str(candidate))
        try:
            if not candidate.is_file():
                continue
            spec = importlib.util.spec_from_file_location("weather_lib", candidate)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules["weather_lib"] = module
            spec.loader.exec_module(module)
            _WL, _WL_PATH = module, str(candidate)
            _install_probe(_WL)
            return _WL
        except Exception as exc:  # noqa: BLE001 - 坏一份就试下一份
            logger.warning("weather_lib 加载失败 %s：%s", candidate, exc)
            continue
    _WL_ERROR = "找不到可用的 weather_lib.py，已尝试：" + " | ".join(tried)
    raise ImportError(_WL_ERROR)


# ---------------------------------------------------------------------------
# 只读探针：记录三个单源函数的返回值，用于判定「这次到底是谁命中的」
# ---------------------------------------------------------------------------
_TRACE = threading.local()


def _record(name: str, value: Any) -> None:
    trace = getattr(_TRACE, "trace", None)
    if trace is not None:
        trace.append((name, value))


def _install_probe(module: Any) -> None:
    """给模块级三个单源函数套一层记录器（幂等，只记录返回值，不改变任何语义）。"""
    if getattr(module, "__hc_weather_probe__", False):
        return
    for name in SOURCE_SLUG:
        original = getattr(module, name, None)
        if not callable(original):
            continue

        def make_wrapper(fn_name: str, fn: Any) -> Any:
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                value = fn(*args, **kwargs)
                _record(fn_name, value)
                return value

            wrapper.__name__ = getattr(fn, "__name__", fn_name)
            wrapper.__doc__ = getattr(fn, "__doc__", None)
            return wrapper

        setattr(module, name, make_wrapper(name, original))
    setattr(module, "__hc_weather_probe__", True)


async def _run(trace: List[Tuple[str, Any]], fn: Any, timeout_s: float) -> Any:
    """在线程池里跑一个同步取数函数，带超时。

    探针状态是 ``threading.local``，所以必须在**工作线程里**把 trace 挂上；每个子调用各自
    一个线程、各自挂一次，互不串味（并发请求也不会互相污染）。
    """

    def runner() -> Any:
        previous = getattr(_TRACE, "trace", None)
        _TRACE.trace = trace
        try:
            return fn()
        finally:
            _TRACE.trace = previous

    return await asyncio.wait_for(asyncio.to_thread(runner), timeout_s)


async def _soft(
    trace: List[Tuple[str, Any]], fn: Any, timeout_s: float, label: str, notes: List[str]
) -> Any:
    """附加项取数：失败/超时返回 ``None`` 并记一条 note，**不**影响整张卡。"""
    try:
        return await _run(trace, fn, timeout_s)
    except asyncio.TimeoutError:
        notes.append(label + "：超过 " + str(int(timeout_s)) + " 秒未返回")
    except Exception as exc:  # noqa: BLE001 - 附加项坏了不该拖死实况
        notes.append(label + "：" + type(exc).__name__ + "：" + str(exc))
    return None


def _attempts(trace: Sequence[Tuple[str, Any]]) -> List[Dict[str, Any]]:
    """三个源各自命中情况（机读，前端可以据此说清"降级到了哪一源"）。"""
    out: List[Dict[str, Any]] = []
    for name in SOURCE_SLUG:  # 字典顺序 = 降级顺序
        calls = [value for recorded, value in trace if recorded == name]
        if not calls:
            continue
        out.append(
            {
                "source": SOURCE_SLUG[name],
                "label": SOURCE_LABEL[SOURCE_SLUG[name]],
                "calls": len(calls),
                "ok": any(value is not None for value in calls),
            }
        )
    return out


def _pick_source(
    trace: Sequence[Tuple[str, Any]], order: Sequence[str], need: Optional[str]
) -> Optional[str]:
    """按降级顺序找出**真正命中**的源。

    ``need`` 是"这个源的返回体里必须有的键"：``hf_get`` 对任何接口都返回 dict，
    但 ``weather/7d`` 成功不代表 ``daily`` 数据可用，所以必须看键，不能只看非 None。
    """
    for name in order:
        for recorded, value in trace:
            if recorded != name:
                continue
            if isinstance(value, dict) and (need is None or need in value):
                return SOURCE_SLUG[name]
    return None


def _source_fields(source: Optional[str]) -> Dict[str, Any]:
    return {
        "source": source,
        "source_label": SOURCE_LABEL.get(source or "", "") or "",
    }


def _now_iso() -> Tuple[str, int]:
    """取数时刻：本地时区 ISO（带偏移）+ 毫秒时间戳。"""
    now = datetime.datetime.now().astimezone()
    return now.isoformat(timespec="seconds"), int(now.timestamp() * 1000)


# ---------------------------------------------------------------------------
# 响应包装
# ---------------------------------------------------------------------------
def _scope(agent: str) -> Dict[str, Any]:
    """作用域声明。天气是全局数据（与 agent 无关），写清楚，不冒充。"""
    return {
        "kind": "global",
        "agent": (agent or "").strip() or None,
        "note": "天气是全局数据，不随 agent 变化；agent 参数只做回显",
    }


def _ok(
    source: Optional[str],
    scope: Dict[str, Any],
    place: str,
    fetched_at: str,
    fetched_at_ms: int,
    trace: Sequence[Tuple[str, Any]],
    **extra: Any
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "ok": True,
        "place": place,
        "scope": scope,
        "fetched_at": fetched_at,
        "fetched_at_ms": fetched_at_ms,
        "sources": _attempts(trace),
        "units": UNITS,
    }
    body.update(_source_fields(source))
    body.update(extra)
    return body


def _fail(
    code: str,
    message: str,
    place: str = "",
    scope: Optional[Dict[str, Any]] = None,
    trace: Sequence[Tuple[str, Any]] = (),
    source: Optional[str] = None,
    status_code: int = 200,
) -> JSONResponse:
    """结构化失败。默认 200：源坏了不是请求错了，面板要显示"没读到 + 为什么"。"""
    fetched_at, fetched_at_ms = _now_iso()
    body: Dict[str, Any] = {
        "ok": False,
        "place": place,
        "scope": scope or _scope(""),
        "fetched_at": fetched_at,
        "fetched_at_ms": fetched_at_ms,
        "sources": _attempts(trace),
        "units": UNITS,
        "error": {"code": code, "message": message},
    }
    body.update(_source_fields(source))
    return JSONResponse(status_code=status_code, content=body)


def _describe_attempts(trace: Sequence[Tuple[str, Any]]) -> str:
    """失败原因摘要：哪个源试过、是否返回空。给人看的一句话。"""
    rows = _attempts(trace)
    if not rows:
        return "没有可用的天气源（weather_lib 未发出任何请求）"
    parts = []
    for row in rows:
        parts.append(row["label"] + ("✓" if row["ok"] else "×空"))
    return "；".join(parts)


# ---------------------------------------------------------------------------
# 地点
# ---------------------------------------------------------------------------
def _clean_place(place: Optional[str]) -> str:
    text = (place or "").strip()
    if not text:
        return DEFAULT_PLACE
    return text[:PLACE_MAX_LEN]


def _resolve_place_fields(lib: Any, loc: Any) -> Dict[str, Any]:
    """地点解析结果（lat/lng/和风 LocationID），解析失败不抛，给 null。"""
    try:
        loc.resolve()
    except Exception as exc:  # noqa: BLE001 - 解析失败由调用方按"地点非法"处理
        return {"name": getattr(loc, "name", ""), "resolved": False, "error": str(exc)}
    return {
        "name": getattr(loc, "name", ""),
        "resolved": True,
        "lat": getattr(loc, "lat", None),
        "lng": getattr(loc, "lng", None),
        "qweather_id": getattr(loc, "hf_loc", None),
    }


def _enrich_daily(rows: List[Dict[str, Any]], trace: Sequence[Tuple[str, Any]]) -> List[Dict[str, Any]]:
    """给逐日行补 ``pop``（降水概率 %）——**只在原始返回里真的有这个数时才补**。

    ``weather_lib`` 的 ``Location.daily()`` 把降水概率丢了（它只投影 date/text/min/max）。
    面板要求"一行一天 + 降水概率"，所以这里从探针记下的原始 payload 里取：
    Open-Meteo 的 ``daily.precipitation_probability_max``（按日期对齐）。和风 ``weather/7d``
    给的是 ``precip``（毫米降水量，不是概率），**不冒充概率**，所以那条路不补 pop，
    前端显示 ``—``。
    """
    if not rows:
        return rows
    for name, value in trace:
        if name != "fetch_openmeteo" or not isinstance(value, dict):
            continue
        daily = value.get("daily")
        if not isinstance(daily, dict):
            continue
        times = daily.get("time") or []
        pops = daily.get("precipitation_probability_max") or []
        by_date = {str(t)[:10]: pops[i] for i, t in enumerate(times) if i < len(pops)}
        for row in rows:
            key = str(row.get("date") or "")[:10]
            if key in by_date and by_date[key] is not None and "pop" not in row:
                row["pop"] = by_date[key]
    return rows


def _caiyun_air(trace: Sequence[Tuple[str, Any]], lib: Any) -> Optional[Dict[str, Any]]:
    """从彩云原始返回里取 PM2.5（μg/m³）与 AQI。

    为什么要从原始返回里取：``Location.realtime()`` 只投影出 ``aqi_chn``（AQI 指数），
    把颗粒物浓度丢了；而面板要显示 PM2.5。探针已经记下了原始 payload，这里就地取，
    **不发额外请求、不改 weather_lib**。非彩云命中时返回 ``None``（前端显示 —）。
    """
    for name, value in trace:
        if name != "fetch_caiyun" or not isinstance(value, dict):
            continue
        air = ((value.get("realtime") or {}).get("air_quality")) or {}
        pm25 = air.get("pm25")
        aqi = (air.get("aqi") or {}).get("chn")
        if pm25 is None and aqi is None:
            continue
        level = ""
        try:
            level = lib.aqi_level(aqi) if aqi is not None else ""
        except Exception:  # noqa: BLE001
            level = ""
        return {
            "pm25": pm25,
            "pm25_unit": UNITS["pm25"],
            "aqi_chn": aqi,
            "aqi_level": level,
            "description": air.get("description"),
            "source": "caiyun",
            "source_label": SOURCE_LABEL["caiyun"],
        }
    return None


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.get("/weather/now")
async def weather_now(
    place: str = Query(default="", description="城市名或 '纬度,经度'；留空用配置的默认地点"),
    agent: str = Query(default="", description="作用域 agent（天气是全局数据，仅回显）"),
) -> Any:
    """实况 + 今日/明日 + 未来 2 小时降水 + 预警。"""
    trace: List[Tuple[str, Any]] = []
    notes: List[str] = []
    target = _clean_place(place)
    scope = _scope(agent)
    try:
        lib = _load_weather_lib()
    except ImportError as exc:
        return _fail("import_failed", "天气库不可用：" + str(exc), target, scope)
    try:
        loc = lib.Location(target)
    except Exception as exc:  # noqa: BLE001 - 构造极少失败，仍兜住
        return _fail("invalid_place", "地点参数不可用：" + str(exc), target, scope, status_code=400)

    fetched_at, fetched_at_ms = _now_iso()
    try:
        realtime = await _run(trace, loc.realtime, NOW_REALTIME_TIMEOUT_S)
    except asyncio.TimeoutError:
        return _fail(
            "timeout",
            "实况取数超过 " + str(int(NOW_REALTIME_TIMEOUT_S)) + " 秒未返回（"
            + _describe_attempts(trace) + "）",
            target,
            scope,
            trace,
        )
    except ValueError as exc:  # weather_lib 解析不了地点时抛的就是这个
        return _fail("invalid_place", str(exc), target, scope, trace, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return _fail(
            "upstream_error", type(exc).__name__ + "：" + str(exc), target, scope, trace
        )
    if not realtime:
        return _fail(
            "all_sources_failed",
            "三个天气源都没返回实况（" + _describe_attempts(trace) + "）",
            target,
            scope,
            trace,
        )

    # 实况自带 source（中文），这是最硬的来源证据，优先于探针推断。
    source = LIB_SOURCE_TO_SLUG.get(str(realtime.get("source") or ""), None)
    if source is None:
        source = _pick_source(trace, ("fetch_caiyun", "hf_get", "fetch_openmeteo"), "realtime")

    daily = await _soft(trace, lambda: loc.daily(7), NOW_EXTRA_TIMEOUT_S, "多日预报", notes)
    minutely = await _soft(trace, loc.minutely, NOW_MINOR_TIMEOUT_S, "分钟级降水", notes)
    warnings = await _soft(trace, loc.warnings, NOW_MINOR_TIMEOUT_S, "预警", notes)

    daily = _enrich_daily(list(daily or []), trace)
    return _ok(
        source,
        scope,
        target,
        fetched_at,
        fetched_at_ms,
        trace,
        place_resolved=_resolve_place_fields(lib, loc),
        realtime=realtime,
        today=daily[0] if daily else None,
        tomorrow=daily[1] if len(daily) > 1 else None,
        minutely=minutely,
        warnings=list(warnings or [])[:2],
        air=_caiyun_air(trace, lib),
        partial_errors=notes,
    )


@router.get("/weather/daily")
async def weather_daily(
    place: str = Query(default=""),
    days: int = Query(default=DAILY_DEFAULT, description="2–16 天"),
    agent: str = Query(default=""),
) -> Any:
    """多日预报。"""
    trace: List[Tuple[str, Any]] = []
    target = _clean_place(place)
    scope = _scope(agent)
    # 注意别写 `days or DAILY_DEFAULT`：days=0 是"用户真的传了 0"，会被 clamp 到下限 2，
    # 而不是悄悄当成"没传"。缺省值由 Query(default=...) 负责。
    want = max(DAILY_MIN, min(int(days), DAILY_MAX))
    try:
        lib = _load_weather_lib()
    except ImportError as exc:
        return _fail("import_failed", "天气库不可用：" + str(exc), target, scope)
    try:
        loc = lib.Location(target)
    except Exception as exc:  # noqa: BLE001
        return _fail("invalid_place", "地点参数不可用：" + str(exc), target, scope, status_code=400)

    fetched_at, fetched_at_ms = _now_iso()
    try:
        rows = await _run(trace, lambda: loc.daily(want), DAILY_TIMEOUT_S)
    except asyncio.TimeoutError:
        return _fail("timeout", "多日预报超过 " + str(int(DAILY_TIMEOUT_S)) + " 秒未返回", target, scope, trace)
    except ValueError as exc:
        return _fail("invalid_place", str(exc), target, scope, trace, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return _fail("upstream_error", type(exc).__name__ + "：" + str(exc), target, scope, trace)

    source = _pick_source(trace, ("hf_get", "fetch_openmeteo"), "daily")
    if rows is None:
        return _fail(
            "all_sources_failed",
            "多日预报没取到（" + _describe_attempts(trace) + "）",
            target,
            scope,
            trace,
            source,
        )
    return _ok(
        source,
        scope,
        target,
        fetched_at,
        fetched_at_ms,
        trace,
        place_resolved=_resolve_place_fields(lib, loc),
        days=want,
        daily=_enrich_daily(list(rows), trace),
    )


@router.get("/weather/hourly")
async def weather_hourly(
    place: str = Query(default=""),
    hours: int = Query(default=HOURLY_DEFAULT, description="3–48 小时"),
    agent: str = Query(default=""),
) -> Any:
    """逐小时（温度 + 天气现象 + 降水概率）。"""
    trace: List[Tuple[str, Any]] = []
    target = _clean_place(place)
    scope = _scope(agent)
    want = max(HOURLY_MIN, min(int(hours), HOURLY_MAX))
    try:
        lib = _load_weather_lib()
    except ImportError as exc:
        return _fail("import_failed", "天气库不可用：" + str(exc), target, scope)
    try:
        loc = lib.Location(target)
    except Exception as exc:  # noqa: BLE001
        return _fail("invalid_place", "地点参数不可用：" + str(exc), target, scope, status_code=400)

    fetched_at, fetched_at_ms = _now_iso()
    try:
        rows = await _run(trace, lambda: loc.hourly(want), HOURLY_TIMEOUT_S)
    except asyncio.TimeoutError:
        return _fail("timeout", "逐小时超过 " + str(int(HOURLY_TIMEOUT_S)) + " 秒未返回", target, scope, trace)
    except ValueError as exc:
        return _fail("invalid_place", str(exc), target, scope, trace, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return _fail("upstream_error", type(exc).__name__ + "：" + str(exc), target, scope, trace)

    source = _pick_source(trace, ("fetch_caiyun", "fetch_openmeteo"), "hourly")
    if rows is None:
        return _fail(
            "all_sources_failed",
            "逐小时没取到（" + _describe_attempts(trace) + "）",
            target,
            scope,
            trace,
            source,
        )
    return _ok(
        source,
        scope,
        target,
        fetched_at,
        fetched_at_ms,
        trace,
        place_resolved=_resolve_place_fields(lib, loc),
        hours=want,
        hourly=list(rows),
    )


@router.get("/weather/at")
async def weather_at(
    place: str = Query(default=""),
    hours: int = Query(default=AT_DEFAULT, description="1–168 小时之后"),
    agent: str = Query(default=""),
) -> Any:
    """「N 小时后」——一句中文，**照抄 weather_lib 的原话**，不做二次合并。"""
    trace: List[Tuple[str, Any]] = []
    target = _clean_place(place)
    scope = _scope(agent)
    want = max(AT_MIN, min(int(hours), AT_MAX))
    try:
        lib = _load_weather_lib()
    except ImportError as exc:
        return _fail("import_failed", "天气库不可用：" + str(exc), target, scope)
    try:
        loc = lib.Location(target)
    except Exception as exc:  # noqa: BLE001
        return _fail("invalid_place", "地点参数不可用：" + str(exc), target, scope, status_code=400)

    fetched_at, fetched_at_ms = _now_iso()
    try:
        text = await _run(trace, lambda: loc.at(want), AT_TIMEOUT_S)
    except asyncio.TimeoutError:
        return _fail("timeout", "「" + str(want) + " 小时后」超过 " + str(int(AT_TIMEOUT_S)) + " 秒未返回", target, scope, trace)
    except ValueError as exc:
        return _fail("invalid_place", str(exc), target, scope, trace, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return _fail("upstream_error", type(exc).__name__ + "：" + str(exc), target, scope, trace)

    source = _pick_source(trace, ("fetch_caiyun", "hf_get", "fetch_openmeteo"), None)
    text = "" if text is None else str(text)
    # weather_lib 查不到时自己也会返回一句 "…暂时查不到"：原样带出来，但标 ok=false，
    # 免得前端把"查不到"当成"有数据"。
    found = "暂时查不到" not in text and bool(text)
    if not found:
        return _fail(
            "no_data",
            text or ("「" + str(want) + " 小时后」没取到（" + _describe_attempts(trace) + "）"),
            target,
            scope,
            trace,
            source,
        )
    return _ok(
        source,
        scope,
        target,
        fetched_at,
        fetched_at_ms,
        trace,
        place_resolved=_resolve_place_fields(lib, loc),
        hours=want,
        text=text,
    )


@router.get("/weather")
async def weather_selfcheck(agent: str = Query(default="")) -> Any:
    """分区自检：路由清单 + 天气库是否可用 + 默认地点 + 降级顺序。

    不取实时数据（自检不该打外网），所以这里不会让面板变慢。
    """
    routes = [
        ("GET", "/weather"),
        ("GET", "/weather/now"),
        ("GET", "/weather/daily"),
        ("GET", "/weather/hourly"),
        ("GET", "/weather/at"),
    ]
    lib_info: Dict[str, Any] = {"ok": False, "path": "", "error": ""}
    default_resolved: Dict[str, Any] = {}
    try:
        lib = _load_weather_lib()
        lib_info = {"ok": True, "path": _WL_PATH, "error": ""}
        default_resolved = _resolve_place_fields(lib, lib.Location(DEFAULT_PLACE))
    except Exception as exc:  # noqa: BLE001 - 自检要把失败原因带出来，不抛
        lib_info["error"] = type(exc).__name__ + "：" + str(exc)
    return {
        "ok": True,
        "section": "weather",
        "scope": _scope(agent),
        "routes": [{"method": method, "path": path} for method, path in routes],
        "weather_lib": lib_info,
        "default_place": DEFAULT_PLACE,
        "default_place_resolved": default_resolved,
        "source_order": [SOURCE_LABEL[slug] for slug in ("caiyun", "qweather", "open-meteo")],
        "units": UNITS,
        "ranges": {
            "days": [DAILY_MIN, DAILY_MAX, DAILY_DEFAULT],
            "hours": [HOURLY_MIN, HOURLY_MAX, HOURLY_DEFAULT],
            "at_hours": [AT_MIN, AT_MAX, AT_DEFAULT],
        },
        "note": "数据源与降级顺序原样沿用 scripts/weather_lib.py；本模块不改数据源、不写旧插件目录",
    }

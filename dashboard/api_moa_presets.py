# -*- coding: utf-8 -*-
"""moa 预设（MoA 配方）域的**纯逻辑**：解析 / 归一 / 悬空引用红灯 / 写入计划 / 回读断言。

移植自 ``plugins/model-switch/dashboard/moa_presets.py``（755 行）里与预设有关的部分，
**不搬**它的 CLI 写入清单（旧插件走 ``hermes config set`` 每键一次子进程，≈2.2s/键）。
本分区的写入口是 ``api_models._write_official`` → ``hermes_cli.config.atomic_config_write``：
一次落盘、保留注释、**缺键即拒**（缺键 dict 删既有配置会被官方写入器直接拒绝）。

本模块**不 import FastAPI，也不 import api_models**（否则循环 import）：只吃/吐 dict，
错误用 :class:`MoaPresetError` 表达，由 api_models 翻成 ``ApiError``。

三条硬纪律（与 api_models 头部同一份清单）：

1. **不碰密钥**：预设只描述 provider + model，没有任何凭据字段；本模块也不读 ``.env``。
2. **不猜**：拿不准的引用显式标 ``unknown`` 并给原因，绝不静默当成可用（旧插件踩过的坑：
   ``K3-4.1flash`` 是 preset 而 ``auto`` / ``hy3`` 是模型名，字符形态完全重叠）。
3. **不改没让我改的键**：写入一律 ``deepcopy`` 原块后只覆盖明确给出的键，
   ``degraded_reference_policy`` / ``fanout`` / 未来新增的键逐字保留。
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: 虚拟 provider 名：``model.provider == moa`` 时 ``model.default`` 就是预设名。
MOA_PROVIDER = "moa"

#: 新建预设的名字文法：字母/数字/下划线/短横线/汉字，1-64 位。
#: **故意不许带点** —— 旧插件同样拒绝（点会让 ``moa.presets.<name>`` 这类点分键歧义，
#: 而且 ``a.b`` 在日志里分不清是预设名还是嵌套路径）。
NEW_PRESET_NAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\-\u4e00-\u9fff]{0,63}$")
#: **定位**已存在预设的名字文法（PUT / DELETE）：允许点（历史配置里真有 ``K3-4.1flash``），
#: 但仍挡控制字符与路径分隔符。合法性真正的判据是\"这个名字在 moa.presets 里存在\"。
LOOKUP_PRESET_NAME_RE = re.compile(r"^[^\x00-\x1f/\\]{1,64}$")

#: ``GET /models/moa/presets`` 的三种状态（沿用 api_models 既有 ``_moa_presets`` 口径）。
STATE_PRESETS = "moa-presets"
STATE_EMPTY = "moa-presets-empty"
STATE_MISSING = "moa-presets-missing"

#: 悬空引用灯的三档。``red`` = 确定错（必须处理）；``amber`` = 能跑但可疑；
#: ``unknown`` = 拿不准，只提示、绝不自动处置。
SEVERITY_RED = "red"
SEVERITY_AMBER = "amber"
SEVERITY_UNKNOWN = "unknown"

#: ``model.default`` / ``fallback_providers[].model`` 指向的到底是什么。
MODEL_SHAPE_PRESET = "preset"
MODEL_SHAPE_DIRECT = "direct_model"
MODEL_SHAPE_LEGACY_TOP_LEVEL = "legacy_top_level"
MODEL_SHAPE_UNKNOWN = "unknown_reference"

#: 写回时要逐字保留的键（**不属于**本次可改的三个字段）。
PRESERVED_KEYS: Tuple[str, ...] = ("fanout", "degraded_reference_policy")


class MoaPresetError(ValueError):
    """预设域的业务错误（api_models 负责翻成 ``ApiError``）。

    ``code`` 是**机器可读**的分类（``preset_name_invalid`` / ``preset_exists`` /
    ``preset_referenced`` / ``dangling_reference`` ...），``extra`` 原样进失败体的 ``error``。
    """

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra: Dict[str, Any] = dict(extra)


# ---------------------------------------------------------------------------
# 归一（与旧插件逐字同口径）
# ---------------------------------------------------------------------------
def _first_nonempty(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _as_ref_list(raw: Any) -> List[Dict[str, Any]]:
    """把 ``reference_models`` 规范成 ``[{provider, model, enabled, reasoning_effort?}]``。

    ``reasoning_effort`` 必须带上：MoA 的思考强度是**逐槽位**实装的，
    解析侧丢了它，``GET`` 读回就看不到\"这条参考模型用多强的思考\"（旧插件修过这个 bug）。
    """
    out: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                entry: Dict[str, Any] = {
                    "provider": _first_nonempty(item.get("provider")),
                    "model": _first_nonempty(item.get("model"), item.get("id"), item.get("name")),
                    "enabled": _as_bool(item.get("enabled"), True),
                }
                effort = _first_nonempty(item.get("reasoning_effort"))
                if effort:
                    entry["reasoning_effort"] = effort
                out.append(entry)
            elif isinstance(item, str) and item.strip():
                out.append({"provider": None, "model": item.strip(), "enabled": True})
    return out


def _as_aggregator(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, dict):
        agg: Dict[str, Any] = {
            "provider": _first_nonempty(raw.get("provider")),
            "model": _first_nonempty(raw.get("model"), raw.get("id"), raw.get("name")),
        }
        effort = _first_nonempty(raw.get("reasoning_effort"))
        if effort:
            agg["reasoning_effort"] = effort
        return agg
    if isinstance(raw, str) and raw.strip():
        return {"provider": None, "model": raw.strip()}
    return None


def parse_moa(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """解析某 agent 的 ``moa:`` 顶层段。

    返回::

        {"present": bool, "enabled": bool|None, "default_preset": str|None,
         "presets": {name: 归一后的块}, "presets_raw": {name: 原始块副本},
         "top_level": {...}, "presets_container": "map"|"list"|None, "error": str|None}

    ``presets`` 支持两种形状：**map**（``presets.<name>``）与 **list**
    （``presets: [{name: ..., ...}]``）—— 两种本机都出现过，读侧必须都认。
    ``presets_raw`` 是给写入/校验用的**原始**副本（含本模块不认识的键）。
    """
    moa = cfg.get("moa")
    if not isinstance(moa, dict):
        return {
            "present": False, "enabled": None, "default_preset": None,
            "presets": {}, "presets_raw": {}, "top_level": {},
            "presets_container": None, "error": None,
        }

    raw_presets = moa.get("presets")
    presets: Dict[str, Dict[str, Any]] = {}
    presets_raw: Dict[str, Dict[str, Any]] = {}
    container: Optional[str] = None
    error: Optional[str] = None

    def _absorb(name: str, body: Dict[str, Any]) -> None:
        presets[name] = {
            "name": name,
            "reference_models": _as_ref_list(body.get("reference_models")),
            "aggregator": _as_aggregator(body.get("aggregator")),
            "enabled": _as_bool(body.get("enabled"), True) if "enabled" in body else None,
            "fanout": _first_nonempty(body.get("fanout")),
            "degraded_reference_policy": _first_nonempty(body.get("degraded_reference_policy")),
            "raw_keys": sorted(str(key) for key in body.keys()),
        }
        presets_raw[name] = copy.deepcopy(body)

    if isinstance(raw_presets, dict):
        container = "map"
        for name, body in raw_presets.items():
            _absorb(str(name), body if isinstance(body, dict) else {})
    elif isinstance(raw_presets, list):
        container = "list"
        for index, body in enumerate(raw_presets):
            if not isinstance(body, dict):
                continue
            name = _first_nonempty(body.get("name"), body.get("id"), body.get("preset"))
            if not name:
                error = error or "presets[%d] 没有 name/id，已跳过" % index
                continue
            _absorb(name, body)
    elif raw_presets is not None:
        error = "moa.presets 既不是 map 也不是 list（是 %s）" % type(raw_presets).__name__

    return {
        "present": True,
        "enabled": _as_bool(moa.get("enabled"), True) if "enabled" in moa else None,
        "default_preset": _first_nonempty(moa.get("default_preset")),
        "presets": presets,
        "presets_raw": presets_raw,
        "top_level": {
            "reference_models": _as_ref_list(moa.get("reference_models")),
            "aggregator": _as_aggregator(moa.get("aggregator")),
            "fanout": _first_nonempty(moa.get("fanout")),
            "max_tokens": moa.get("max_tokens"),
            "degraded_reference_policy": _first_nonempty(moa.get("degraded_reference_policy")),
        },
        "presets_container": container,
        "error": error,
    }


def preset_state(parsed: Dict[str, Any]) -> str:
    """三种状态之一（界面靠它决定画\"编辑器 / 空态 / 没这个段\"）。"""
    if not parsed.get("present"):
        return STATE_MISSING
    if not parsed.get("presets"):
        return STATE_EMPTY
    return STATE_PRESETS


# ---------------------------------------------------------------------------
# 悬空引用红灯（本卡验收第 9 条）
# ---------------------------------------------------------------------------
def _moa_top_level_present(moa: Dict[str, Any]) -> bool:
    """``moa:`` 顶层兼容配方是否可用于兜底（旧版 MoA 把配方直接写在 moa 段上）。"""
    top = moa.get("top_level") or {}
    if top.get("reference_models"):
        return True
    agg = top.get("aggregator") or {}
    return bool(agg.get("model"))


def classify_model_reference(
    model: Any,
    preset_names: Sequence[str],
    *,
    moa_has_top_level: bool = False,
) -> str:
    """``model.default`` / ``fallback_providers[].model`` 指向的是预设还是模型名？

    返回四选一：``preset`` / ``direct_model`` / ``legacy_top_level`` / ``unknown_reference``。

    **不做形态猜测**（旧插件踩过：``K3-4.1flash`` 是 preset，``auto`` / ``hy3`` 是模型名，
    字符层面完全重叠），只做\"确定判定\"，拿不准的显式标 unknown。
    """
    if not isinstance(model, str) or not model.strip():
        return MODEL_SHAPE_DIRECT
    text = model.strip()
    if text in set(preset_names or ()):
        return MODEL_SHAPE_PRESET
    if moa_has_top_level:
        return MODEL_SHAPE_LEGACY_TOP_LEVEL
    return MODEL_SHAPE_UNKNOWN


def dangling_references(profile: str, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """扫出该 agent 的**悬空预设引用**（空列表 = 干净）。

    检查三处：``moa.default_preset`` / ``model.default``（仅 provider==moa）/
    每个 ``fallback_providers[i].model``（仅 provider==moa）。

    * ``default_preset`` 指不到 ⇒ **red**（显式锚点，指不到就是错）；
    * ``model.default`` / fallback 的 moa 引用指不到 ⇒ 有顶层兼容段则 **amber**
      （MoA 会退到顶层配方，还能跑），否则 **unknown**（拿不准，禁止静默处置）。
    """
    lights: List[Dict[str, Any]] = []
    moa = parse_moa(cfg)
    if not moa["present"]:
        return lights

    preset_names = sorted(moa["presets"].keys())
    has_top = _moa_top_level_present(moa)

    default_preset = moa.get("default_preset")
    if default_preset and default_preset not in moa["presets"]:
        lights.append({
            "profile": profile,
            "severity": SEVERITY_RED,
            "code": "default_preset_missing",
            "where": "moa.default_preset",
            "value": default_preset,
            "message": (
                "moa.default_preset = 「%s」，但 moa.presets 里没有这个键（现有：%s）。"
                "MoA 启动时找不到这个配方；禁止静默写入 —— 先补预设或改正 default_preset。"
                % (default_preset, "、".join(preset_names) or "无")
            ),
            "available_presets": preset_names,
        })

    model = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    if _first_nonempty(model.get("provider")) == MOA_PROVIDER:
        model_default = _first_nonempty(model.get("default"))
        if model_default:
            shape = classify_model_reference(model_default, preset_names,
                                             moa_has_top_level=has_top)
            if shape == MODEL_SHAPE_LEGACY_TOP_LEVEL:
                lights.append({
                    "profile": profile, "severity": SEVERITY_AMBER,
                    "code": "preset_missing_recovered_by_top_level",
                    "where": "model.default", "value": model_default,
                    "message": (
                        "model.default = 「%s」不在 moa.presets 里；但 moa 顶层兼容段存在，"
                        "MoA 会退到顶层配方，所以还能跑。建议补同名预设或改正 model.default。"
                        % model_default
                    ),
                    "available_presets": preset_names,
                })
            elif shape == MODEL_SHAPE_UNKNOWN:
                lights.append({
                    "profile": profile, "severity": SEVERITY_UNKNOWN,
                    "code": "moa_model_reference_unresolved",
                    "where": "model.default", "value": model_default,
                    "message": (
                        "model.provider = moa，model.default = 「%s」不在 moa.presets 里，"
                        "且 moa 顶层兼容段也不存在 —— 无法确定这是模型名还是被删掉的预设名。"
                        "禁止静默处置，请人工确认。" % model_default
                    ),
                    "available_presets": preset_names,
                })

    raw_fallback = cfg.get("fallback_providers")
    if isinstance(raw_fallback, list):
        for index, item in enumerate(raw_fallback):
            if not isinstance(item, dict):
                continue
            if _first_nonempty(item.get("provider")) != MOA_PROVIDER:
                continue
            fallback_model = _first_nonempty(item.get("model"))
            if not fallback_model or fallback_model in preset_names:
                continue
            shape = classify_model_reference(fallback_model, preset_names,
                                             moa_has_top_level=has_top)
            where = "fallback_providers[%d].model" % index
            if shape == MODEL_SHAPE_LEGACY_TOP_LEVEL:
                lights.append({
                    "profile": profile, "severity": SEVERITY_AMBER,
                    "code": "preset_missing_recovered_by_top_level",
                    "where": where, "value": fallback_model,
                    "message": (
                        "fallback_providers[%d] 指向 moa 预设「%s」，但它不在 moa.presets 里；"
                        "顶层兼容配方存在，仍能跑。建议补齐或改正。" % (index, fallback_model)
                    ),
                    "available_presets": preset_names,
                })
            elif shape == MODEL_SHAPE_UNKNOWN:
                lights.append({
                    "profile": profile, "severity": SEVERITY_UNKNOWN,
                    "code": "moa_model_reference_unresolved",
                    "where": where, "value": fallback_model,
                    "message": (
                        "fallback_providers[%d] 的 moa 引用「%s」不在 moa.presets 里，"
                        "且无顶层兼容段兜底 —— 无法确定是模型名还是被删的预设名。"
                        "禁止静默处置，请人工确认。" % (index, fallback_model)
                    ),
                    "available_presets": preset_names,
                })
    return lights


def find_references(preset_name: str, profile: str, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """该 agent 里**谁引用了这个预设**（用于改/删的 409 拒绝，必须点名到字段）。"""
    refs: List[Dict[str, Any]] = []
    moa = parse_moa(cfg)

    if moa.get("default_preset") == preset_name:
        refs.append({"profile": profile, "where": "moa.default_preset",
                     "value": preset_name, "kind": "default_preset"})

    model = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    if _first_nonempty(model.get("provider")) == MOA_PROVIDER:
        if _first_nonempty(model.get("default")) == preset_name:
            refs.append({"profile": profile, "where": "model.default",
                         "value": preset_name, "kind": "model_default"})

    raw_fallback = cfg.get("fallback_providers")
    if isinstance(raw_fallback, list):
        for index, item in enumerate(raw_fallback):
            if not isinstance(item, dict):
                continue
            if _first_nonempty(item.get("provider")) != MOA_PROVIDER:
                continue
            if _first_nonempty(item.get("model")) == preset_name:
                refs.append({"profile": profile,
                             "where": "fallback_providers[%d].model" % index,
                             "value": preset_name, "kind": "fallback"})
    return refs


def light_counts(lights: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    return {
        SEVERITY_RED: len([item for item in lights if item.get("severity") == SEVERITY_RED]),
        SEVERITY_AMBER: len([item for item in lights if item.get("severity") == SEVERITY_AMBER]),
        SEVERITY_UNKNOWN: len([item for item in lights if item.get("severity") == SEVERITY_UNKNOWN]),
    }


def red_lights(lights: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [item for item in lights if item.get("severity") == SEVERITY_RED]


# ---------------------------------------------------------------------------
# 指纹 / 跨 profile 一致性（旧插件为\"改小趴菜思考强度对力工不生效\"这个长期隐身 bug 加的）
# ---------------------------------------------------------------------------
def _effort_token(value: Any) -> str:
    text = str(value or "").strip()
    return text if text else "-"


def preset_fingerprint(preset: Dict[str, Any]) -> str:
    """配方可比指纹：只看**配方内容**（含思考强度），不看 enabled / 名字 / 展示字段。

    强度必须进指纹：否则 ``sensenova@max`` 与 ``sensenova@high`` 会被判为\"一致\"，
    跨 profile 不一致的高亮永远不亮。
    """
    refs = []
    for item in preset.get("reference_models") or []:
        refs.append("%s/%s@%s" % (
            item.get("provider") or "?", item.get("model") or "?",
            _effort_token(item.get("reasoning_effort")),
        ))
    aggregator = preset.get("aggregator") or {}
    aggregator_text = "%s/%s@%s" % (
        aggregator.get("provider") or "?", aggregator.get("model") or "?",
        _effort_token(aggregator.get("reasoning_effort")),
    )
    return "|".join(sorted(refs)) + " >> " + aggregator_text


def cross_profile_consistency(
    per_profile_presets: Dict[str, Dict[str, Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """同名预设跨 profile 内容不一致 → 高亮标注。"""
    by_name: Dict[str, Dict[str, List[str]]] = {}
    for profile, presets in (per_profile_presets or {}).items():
        for name, preset in (presets or {}).items():
            fingerprint = preset_fingerprint(preset)
            by_name.setdefault(name, {}).setdefault(fingerprint, []).append(profile)

    out: List[Dict[str, Any]] = []
    for name in sorted(by_name.keys()):
        variants = by_name[name]
        if len(variants) <= 1:
            continue
        out.append({
            "name": name,
            "profiles": sorted(profile for group in variants.values() for profile in group),
            "inconsistent": True,
            "variants": [
                {"fingerprint": fingerprint, "profiles": sorted(group), "count": len(group)}
                for fingerprint, group in sorted(variants.items())
            ],
        })
    return out


# ---------------------------------------------------------------------------
# 写入侧：校验 / 合并 / 生成新 doc / 回读断言
# ---------------------------------------------------------------------------
def validate_new_name(raw: Any) -> str:
    """新建预设的名字（必填、文法见 :data:`NEW_PRESET_NAME_RE`）。"""
    text = "" if raw is None else str(raw).strip()
    if not text:
        raise MoaPresetError("preset_name_invalid", "预设名必填（name）")
    if not NEW_PRESET_NAME_RE.match(text):
        raise MoaPresetError(
            "preset_name_invalid",
            "非法预设名 %r：1-64 位字母/数字/下划线/短横线/汉字，不能以符号开头，"
            "**不能带点**（点会让 moa.presets.<name> 的点分路径歧义）" % text,
            name=text,
        )
    return text


def validate_lookup_name(raw: Any) -> str:
    """定位已存在预设的名字（允许点，挡控制字符与路径分隔符）。"""
    text = "" if raw is None else str(raw).strip()
    if not text:
        raise MoaPresetError("preset_name_invalid", "预设名必填")
    if not LOOKUP_PRESET_NAME_RE.match(text):
        raise MoaPresetError(
            "preset_name_invalid",
            "非法预设名 %r：1-64 位，不能含控制字符或路径分隔符（/ \\）" % text,
            name=text,
        )
    return text


def _require_text(value: Any, *, what: str, allow_none: bool = False) -> Optional[str]:
    if value is None and allow_none:
        return None
    text = "" if value is None else str(value).strip()
    if not text:
        raise MoaPresetError("invalid_field", "%s 必填（不能是空串）" % what, field=what)
    if len(text) > 200 or any(char in text for char in "\r\n\t"):
        raise MoaPresetError("invalid_field", "%s 非法：不能含换行/制表符，长度 ≤200" % what,
                             field=what, value=text[:80])
    return text


def normalize_refs(raw: Any) -> List[Dict[str, Any]]:
    """校验并归一 ``reference_models``（**有序**，顺序就是 fan-out 顺序，必须原样保留）。"""
    if not isinstance(raw, list):
        raise MoaPresetError("invalid_field", "reference_models 必须是数组（有序）",
                             field="reference_models")
    if not raw:
        raise MoaPresetError("reference_models_empty",
                             "reference_models 不能为空：MoA 至少要有参考模型",
                             field="reference_models")
    out: List[Dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise MoaPresetError("invalid_field",
                                 "reference_models[%d] 必须是对象 {provider, model, enabled}" % index,
                                 field="reference_models", index=index)
        model = _require_text(item.get("model"), what="reference_models[%d].model" % index)
        provider = item.get("provider")
        if provider is not None:
            provider = _require_text(provider, what="reference_models[%d].provider" % index)
        slot: Dict[str, Any] = {"model": model}
        if provider is not None:
            # 没给 provider 就不写这个键（写 `provider: null` 是噪音，且宿主解析未必认）
            slot = {"provider": provider, "model": model}
        slot["enabled"] = _as_bool(item.get("enabled"), True)
        effort = item.get("reasoning_effort")
        if effort is not None:
            slot["reasoning_effort"] = _require_text(
                effort, what="reference_models[%d].reasoning_effort" % index)
        out.append(slot)
    return out


def normalize_aggregator(raw: Any, *, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """校验并归一 ``aggregator``（**合并**语义：没给的键沿用 ``existing``）。

    显式给 ``null`` 表示\"删掉这个键\"（只有 ``reasoning_effort`` 允许这样，
    ``provider`` / ``model`` 是必填）。
    """
    if not isinstance(raw, dict):
        raise MoaPresetError("invalid_field", "aggregator 必须是对象 {provider, model, reasoning_effort}",
                             field="aggregator")
    base = dict(existing or {})
    if "provider" in raw:
        provider = raw.get("provider")
        if provider is None:
            base.pop("provider", None)  # 显式 null ⇒ 删掉这个键
        else:
            base["provider"] = _require_text(provider, what="aggregator.provider")
    if "model" in raw or "model" not in base:
        base["model"] = _require_text(raw.get("model"), what="aggregator.model")
    if "reasoning_effort" in raw:
        effort = raw.get("reasoning_effort")
        if effort is None:
            base.pop("reasoning_effort", None)
        else:
            base["reasoning_effort"] = _require_text(effort, what="aggregator.reasoning_effort")
    if not base.get("model"):
        raise MoaPresetError("aggregator_model_empty", "aggregator.model 必填", field="aggregator.model")
    return base


def build_new_block(
    *,
    reference_models: List[Dict[str, Any]],
    aggregator: Dict[str, Any],
    enabled: Optional[bool] = None,
    fanout: Optional[str] = None,
    degraded_reference_policy: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """新建预设的完整块（只写明确给出的键，绝不给\"默认值\"填空）。"""
    block: Dict[str, Any] = {}
    if extra:
        block.update(copy.deepcopy(extra))
    if enabled is not None:
        block["enabled"] = bool(enabled)
    block["reference_models"] = copy.deepcopy(reference_models)
    block["aggregator"] = copy.deepcopy(aggregator)
    if fanout:
        block["fanout"] = str(fanout)
    if degraded_reference_policy:
        block["degraded_reference_policy"] = str(degraded_reference_policy)
    return block


def build_updated_block(
    existing: Dict[str, Any],
    *,
    reference_models: Optional[List[Dict[str, Any]]] = None,
    aggregator: Optional[Dict[str, Any]] = None,
    enabled: Optional[bool] = None,
    fanout: Optional[str] = None,
    degraded_reference_policy: Optional[str] = None,
) -> Dict[str, Any]:
    """改预设：``deepcopy`` 原块后**只覆盖明确给出的键**。

    ★ 红线：``degraded_reference_policy`` / ``fanout`` / 未来新增的键，
    只要本次没给，就逐字保留（旧插件把整块重建导致这些键被抹掉过）。
    """
    block = copy.deepcopy(existing or {})
    if reference_models is not None:
        block["reference_models"] = copy.deepcopy(reference_models)
    if aggregator is not None:
        merged = normalize_aggregator(aggregator, existing=block.get("aggregator")
                                      if isinstance(block.get("aggregator"), dict) else None)
        block["aggregator"] = merged
    if enabled is not None:
        block["enabled"] = bool(enabled)
    if fanout:
        block["fanout"] = str(fanout)
    if degraded_reference_policy:
        block["degraded_reference_policy"] = str(degraded_reference_policy)
    return block


def doc_presets(doc: Dict[str, Any]) -> Dict[str, Any]:
    """``doc['moa']['presets']``（不存在 / 形状不对 ⇒ 空 dict；**不改** doc）。"""
    moa = doc.get("moa")
    if not isinstance(moa, dict):
        return {}
    presets = moa.get("presets")
    return presets if isinstance(presets, dict) else {}


def set_preset(doc: Dict[str, Any], name: str, block: Dict[str, Any]) -> Dict[str, Any]:
    """返回**新的** doc：``moa.presets.<name>`` = block（其余段一律 deepcopy 原样带走）。"""
    new_doc = copy.deepcopy(doc)
    moa = new_doc.get("moa")
    if moa is None:
        moa = {}
        new_doc["moa"] = moa
    if not isinstance(moa, dict):
        raise MoaPresetError("moa_block_invalid",
                             "config.yaml 的 moa 段不是 mapping（是 %s），拒绝写入"
                             % type(moa).__name__)
    presets = moa.get("presets")
    if presets is None:
        presets = {}
        moa["presets"] = presets
    if not isinstance(presets, dict):
        raise MoaPresetError(
            "presets_container_list",
            "moa.presets 是 list 形状（旧格式）：本分区只写 map 形状，"
            "请先人工把 presets 改成 map（否则写回会把 list 整段换掉）",
        )
    presets[name] = copy.deepcopy(block)
    return new_doc


def delete_preset(doc: Dict[str, Any], name: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """返回 ``(新 doc, 被删掉的原始块)``。不存在 ⇒ ``preset_not_found``。"""
    presets = doc_presets(doc)
    if name not in presets:
        raise MoaPresetError("preset_not_found", "预设 %r 不存在" % name, name=name,
                             presets=sorted(presets.keys()))
    new_doc = copy.deepcopy(doc)
    removed = new_doc["moa"]["presets"].pop(name)
    return new_doc, removed


def rename_preset(doc: Dict[str, Any], old: str, new: str) -> Dict[str, Any]:
    """返回新 doc：块内容**逐字搬**到新名字下（旧名字删掉）。"""
    presets = doc_presets(doc)
    if old not in presets:
        raise MoaPresetError("preset_not_found", "预设 %r 不存在" % old, name=old,
                             presets=sorted(presets.keys()))
    if new in presets:
        raise MoaPresetError("preset_exists", "预设 %r 已存在（改名目标重名）" % new, name=new)
    new_doc = copy.deepcopy(doc)
    block = new_doc["moa"]["presets"].pop(old)
    new_doc["moa"]["presets"][new] = block
    return new_doc


def deletion_paths(*, name: str, deleted_aggregator_keys: Sequence[str] = ()) -> List[str]:
    """``_write_official(allow_deletions=...)`` 要放行的点分路径。

    删预设 ⇒ ``moa.presets.<name>``（整块被省略）；改聚合器时显式删强度 ⇒
    ``moa.presets.<name>.aggregator.reasoning_effort``。
    """
    paths = ["moa.presets.%s" % name]
    for key in deleted_aggregator_keys:
        paths.append("moa.presets.%s.aggregator.%s" % (name, key))
    return paths


def _dump(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None:
        return ""
    return str(value)


def verify_preset(
    *,
    expected: Dict[str, Any],
    actual: Optional[Dict[str, Any]],
    preserved: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """**回读磁盘逐键比对**：``expected`` 的每个键必须逐字相等，``preserved`` 的必须没变。

    ``expected`` = 本次明确要写的键；``preserved`` = 本次没碰、必须原样活着的键
    （``fanout`` / ``degraded_reference_policy`` / 别的既有键）。
    返回 ``[{key, expected, actual, ok}]``；任何一行不 ok ⇒ 调用方自动回滚。
    """
    rows: List[Dict[str, Any]] = []
    for key in sorted(expected.keys()):
        want = _dump(expected[key])
        got = "<missing>" if actual is None else _dump(actual.get(key))
        rows.append({"key": key, "expected": want, "actual": got, "ok": want == got})
    for key in sorted((preserved or {}).keys()):
        if key in expected:
            continue
        want = _dump((preserved or {})[key])
        got = "<missing>" if actual is None else _dump(actual.get(key))
        rows.append({"key": key, "expected": want, "actual": got, "ok": want == got,
                     "preserved": True})
    return rows


def preset_diff(before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """``[{key, from, to}]``（只列真的变了的键；``reference_models`` 顺序变了也算变）。"""
    before = before or {}
    after = after or {}
    rows: List[Dict[str, Any]] = []
    for key in sorted(set(before.keys()) | set(after.keys())):
        old = _dump(before.get(key, "<missing>"))
        new = _dump(after.get(key, "<missing>"))
        if old != new:
            rows.append({"key": key, "from": old, "to": new})
    return rows


def preset_row(
    *,
    name: str,
    parsed: Dict[str, Any],
    profile: str,
    cfg: Dict[str, Any],
    cross_profile: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """一条预设的读视图（内容 + 谁在引用 + 是不是默认预设 + 跨 profile 一致性）。"""
    body = parsed["presets"].get(name) or {}
    refs = find_references(name, profile, cfg)
    row: Dict[str, Any] = {
        "name": name,
        "enabled": body.get("enabled"),
        "reference_models": body.get("reference_models") or [],
        "aggregator": body.get("aggregator"),
        "fanout": body.get("fanout"),
        "degraded_reference_policy": body.get("degraded_reference_policy"),
        "raw_keys": body.get("raw_keys") or [],
        "fingerprint": preset_fingerprint(body),
        "is_default_preset": parsed.get("default_preset") == name,
        "referenced": bool(refs),
        "references": refs,
        "delete_blocked": bool(refs),
    }
    if cross_profile is not None:
        detail = cross_profile.get(name)
        row["cross_profile_inconsistent"] = bool(detail)
        if detail:
            row["cross_profile_detail"] = detail
    return row

# -*- coding: utf-8 -*-
"""hermes-console · 项目分组的**独立** provider/model 配置（t_ac537221 阶段三）。

## 为什么要有这个文件

在此之前项目分组 AI 借的是宿主的 ``auxiliary.title_generation`` 槽位
（``autogroup_ai.py`` 的 ``AUX_TASK``）：`call_llm(task="title_generation")` 且**不传** provider/model，
于是「设置项目分组 AI 的模型」实际改的是自动命名的配置。本模块给项目分组**自己的**一份配置：

* 存放：``HERMES_HOME/auto-group/ai_model.json``
  ``{"mode":"module"|"global", "agent":"<agent>", "provider":..., "model":..., "updated_at":..., "updated_by":...}``
* ``mode=module`` ⇒ 用配置里点名的 provider/model；
* ``mode=global`` ⇒ 用该 agent ``config.yaml`` 里**当前生效**的全局模型
  （真值来自 ``api_models._current_state``，本模块不自己解析 config.yaml）；
* 调用时 **显式** 传 ``provider=`` / ``model=``，``task=None`` —— 不走任何 aux 槽位的模型解析，
  宿主的 aux 槽位只用来给并发/超时兜底（不传 task 就不读 ``auxiliary.<task>`` 的任何配置）。

## 三条硬约束

1. **密钥只以变量名 + 布尔出现**：``key_env`` / ``key_present``，取值一律不回显（复用 ``api_models``）；
2. **保存后回读落盘真值**（``load`` 重新读文件），不返回「我刚写进去的东西」；
3. **每次真实模型调用都写一行审计**（``auto-group/ai_calls.jsonl``），
   界面的 ``llm`` 字段与这行审计必须一致 —— 没调就写 ``used:false`` + 降级原因，不许谎报。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_DASHBOARD_DIR = Path(__file__).resolve().parent
if str(_DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD_DIR))

CONFIG_NAME = "ai_model.json"
AUDIT_NAME = "ai_calls.jsonl"
MODE_MODULE = "module"
MODE_GLOBAL = "global"
MODES = (MODE_MODULE, MODE_GLOBAL)
#: 配置 schema 版本。1 = 老的「单组」格式（{"mode","agent","provider","model"}）；
#: 2 = 按 Agent 映射（{"agents": {"<agent>": {...}}, "global": {...}}）。
SCHEMA_LEGACY = 1
SCHEMA_AGENTS = 2
SCHEMA = SCHEMA_AGENTS
#: 来源层（UI 要显示「该 Agent 独立 / 跟随全局 / 模块默认」）。
LAYER_AGENT = "agent"            # 该 Agent 有独立声明（mode=module 且点了 provider/model）
LAYER_AGENT_GLOBAL = "agent_global"  # 该 Agent 独立声明为「跟随全局」
LAYER_GLOBAL = "global"          # 该 Agent 无声明，用 global 段
LAYER_MODULE_DEFAULT = "module_default"  # 什么声明都没有 ⇒ 回落该 Agent config.yaml 当前模型
LAYER_LABELS = {
    LAYER_AGENT: "该 Agent 独立",
    LAYER_AGENT_GLOBAL: "该 Agent 跟随全局",
    LAYER_GLOBAL: "跟随全局（global 段）",
    LAYER_MODULE_DEFAULT: "模块默认（无声明，回落该 Agent 当前模型）",
}
#: 一次分类调用的超时（秒）。批量跑时每条一次调用，60s 与引擎口径一致。
CALL_TIMEOUT = 60.0
CONFIG_MAX_BYTES = 64 * 1024

_CONFIG_LOCK = threading.Lock()


def _dir(home: Path) -> Path:
    return Path(home) / "auto-group"


def config_path(home: Path) -> Path:
    return _dir(home) / CONFIG_NAME


def audit_path(home: Path) -> Path:
    return _dir(home) / AUDIT_NAME


# --------------------------------------------------------------------------
# 读写（schema 2：按 Agent 映射；旧单组格式可读）
# --------------------------------------------------------------------------
def _group(mode: Optional[str], provider: Optional[str], model: Optional[str],
           declared: bool = True) -> Dict[str, Any]:
    """一组配置。``declared=False`` 表示「文件里没有这一段」。"""
    mode = str(mode or MODE_GLOBAL).strip().lower()
    if mode not in MODES:
        mode = MODE_GLOBAL
    return {"mode": mode,
            "provider": (str(provider).strip() or None) if provider else None,
            "model": (str(model).strip() or None) if model else None,
            "declared": bool(declared)}


def _empty_state(path: Path, exists: bool) -> Dict[str, Any]:
    return {"schema": SCHEMA_AGENTS, "agents": {}, "global": _group(None, None, None, declared=False),
            "legacy": False, "exists": exists, "path": str(path),
            "updated_at": None, "updated_by": None, "error": None}


def load(home: Path) -> Dict[str, Any]:
    """读盘上的配置。文件不存在/坏掉都给**明确空态**，不抛。

    返回 ::

        {"schema": 2,
         "agents": {"<agent>": {"mode","provider","model","declared"}},
         "global": {"mode","provider","model","declared"},
         "legacy": bool,          # True = 盘上还是老的单组格式（读取时已翻译）
         "exists","path","updated_at","updated_by","error"}
    """
    path = config_path(home)
    out = _empty_state(path, path.is_file())
    if not path.is_file():
        return out
    try:
        raw = path.read_text(encoding="utf-8")
        if len(raw) > CONFIG_MAX_BYTES:
            out["error"] = "配置文件超过 %d 字节，拒绝解析" % CONFIG_MAX_BYTES
            return out
        data = json.loads(raw) or {}
        if not isinstance(data, dict):
            out["error"] = "配置文件顶层不是对象（实际 %s）" % type(data).__name__
            return out
    except (OSError, ValueError, TypeError) as exc:
        out["error"] = "读 %s 失败：%s" % (path.name, type(exc).__name__ + "：" + str(exc))
        return out

    out["updated_at"] = data.get("updated_at")
    out["updated_by"] = data.get("updated_by")
    raw_agents = data.get("agents")
    if isinstance(raw_agents, dict):
        out["schema"] = SCHEMA_AGENTS
        for name, block in raw_agents.items():
            key = str(name or "").strip()
            if not key or not isinstance(block, dict):
                continue
            out["agents"][key] = _group(block.get("mode"), block.get("provider"), block.get("model"))
        glob = data.get("global")
        if isinstance(glob, dict):
            out["global"] = _group(glob.get("mode"), glob.get("provider"), glob.get("model"))
        return out

    # ---- 旧格式（schema 1）：{"mode","agent","provider","model"} ----
    # 翻译规则：老文件记的是「某一组」，所以把它当作 global 段的声明；如果老文件点了名
    # 某个 agent，同一个值同时也是那个 agent 的声明（与改造前的行为逐字节等价）。
    out["legacy"] = True
    glob = _group(data.get("mode"), data.get("provider"), data.get("model"))
    out["global"] = glob
    legacy_agent = str(data.get("agent")).strip() if data.get("agent") else ""
    if legacy_agent:
        out["agents"][legacy_agent] = dict(glob)
    return out


def _group_for(cfg: Dict[str, Any], agent: str) -> Tuple[Dict[str, Any], str]:
    """该 agent 生效的那一组 + 来源层。**不做** config.yaml 解析（那是 resolve 的事）。"""
    entry = (cfg.get("agents") or {}).get(agent)
    if entry is not None:
        return entry, (LAYER_AGENT if entry.get("mode") == MODE_MODULE else LAYER_AGENT_GLOBAL)
    glob = cfg.get("global") or {}
    if glob.get("declared"):
        return glob, LAYER_GLOBAL
    return _group(None, None, None, declared=False), LAYER_MODULE_DEFAULT


def load_agent(home: Path, agent: Optional[str] = None) -> Dict[str, Any]:
    """单看一个 agent 的声明（不解析 config.yaml）：``{agent, group, layer, declared}``。"""
    cfg = load(home)
    target = str(agent or "default")
    group, layer = _group_for(cfg, target)
    return {"agent": target, "group": group, "layer": layer, "layer_label": LAYER_LABELS.get(layer),
            "declared": bool(group.get("declared")), "legacy": bool(cfg.get("legacy")),
            "error": cfg.get("error"), "path": cfg.get("path"), "exists": cfg.get("exists")}


def _atomic_write(path: Path, data: Dict[str, Any]) -> None:
    """同目录临时文件 + ``os.replace``：并发读者要么看到旧的完整内容，要么看到新的。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex[:8])
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def save(home: Path, *, mode: str, agent: Optional[str], provider: Optional[str],
         model: Optional[str], actor: str = "panel") -> Dict[str, Any]:
    """写**该 agent 那一组**配置 → **回读**。返回 ``{"ok":..., "readback": {...}}``。

    按 Agent 独立：``agent`` 给了就只动 ``agents[agent]``，别的 agent 的声明一个字节都不碰；
    ``agent=None`` 时写 ``global`` 段（作为所有未声明 agent 的回落）。
    读-改-写全部在 ``_CONFIG_LOCK`` 内完成并走 ``_atomic_write``，并发保存不会丢别人的段。
    """
    mode = str(mode or MODE_GLOBAL).strip().lower()
    if mode not in MODES:
        return {"ok": False, "code": "invalid_mode",
                "message": "mode 只能是 %s（收到 %r）" % (" / ".join(MODES), mode)}
    target = (str(agent).strip() or None) if agent else None
    group = _group(mode, provider, model)
    with _CONFIG_LOCK:
        before = load(home)
        if before.get("error"):
            # 盘上是坏文件：**不覆盖**（覆盖等于把用户手写的内容丢掉），让调用方先看到错误。
            return {"ok": False, "code": "config_unreadable",
                    "message": "盘上配置读不出来，拒绝覆盖：" + str(before["error"]),
                    "readback": before}
        agents = {name: dict(block) for name, block in (before.get("agents") or {}).items()}
        glob = dict(before.get("global") or _group(None, None, None, declared=False))
        if target:
            agents[target] = group
        else:
            glob = group
        payload: Dict[str, Any] = {
            "schema": SCHEMA_AGENTS,
            "agents": {name: {"mode": block["mode"], "provider": block["provider"],
                              "model": block["model"]}
                       for name, block in sorted(agents.items())},
            "global": {"mode": glob["mode"], "provider": glob["provider"], "model": glob["model"]},
            "updated_at": time.time(), "updated_by": actor,
        }
        if target:
            payload["updated_agent"] = target
        try:
            _atomic_write(config_path(home), payload)
        except OSError as exc:
            return {"ok": False, "code": "write_failed",
                    "message": "写 %s 失败：%s" % (CONFIG_NAME, type(exc).__name__ + "：" + str(exc))}
        readback = load(home)
    if readback.get("error"):
        return {"ok": False, "code": "readback_failed",
                "message": "写入返回成功但回读失败：" + str(readback["error"]), "readback": readback}
    got, layer = _group_for(readback, target or "default") if target else (readback.get("global"), LAYER_GLOBAL)
    same = (got.get("mode") == mode and got.get("provider") == group["provider"]
            and got.get("model") == group["model"])
    if not same:
        return {"ok": False, "code": "readback_mismatch",
                "message": "回读与写入不一致（盘上真值如下）", "readback": readback,
                "expected": {"mode": mode, "agent": target, "provider": group["provider"],
                             "model": group["model"]}}
    return {"ok": True, "readback": readback, "agent": target or "default", "layer": layer}


# --------------------------------------------------------------------------
# 校验 / 解析（全部复用 api_models 的只读能力，不新写模型清单后端）
# --------------------------------------------------------------------------
def _models_api():
    import api_models  # 懒 import：避免与 plugin_api 的 import 顺序耦合
    return api_models


def options(home: Path, agent: str) -> Dict[str, Any]:
    """该 agent 视角的可选 provider 列表（**凭据脱敏**：只有 key_env + key_present）。"""
    api = _models_api()
    try:
        target = api._require_agent(agent or "default", write=False)
    except Exception as exc:  # noqa: BLE001 - ApiError 也是 Exception
        return {"ok": False, "code": getattr(exc, "code", "invalid_agent"),
                "message": getattr(exc, "message", None) or str(exc)}
    rows = api._provider_rows(target, include_all=False)
    state = api._current_state(target)
    return {"ok": True, "agent": target, "providers": rows,
            "current": {"provider": state.get("provider"), "model": state.get("model")}}


def models_for(home: Path, agent: str, provider: str, *, live: bool = False) -> Dict[str, Any]:
    """该 agent 下某 provider 的模型列表。"""
    api = _models_api()
    try:
        target = api._require_agent(agent or "default", write=False)
        name = api._require_provider(target, provider)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": getattr(exc, "code", "invalid_argument"),
                "message": getattr(exc, "message", None) or str(exc),
                "providers": getattr(exc, "extra", {}).get("providers") if hasattr(exc, "extra") else None}
    payload = api._model_list(target, name, live=live)
    return {"ok": True, **payload}


def validate(home: Path, *, agent: str, provider: str, model: str) -> Dict[str, Any]:
    """校验 provider/model 组合属于该 agent 的该 provider。

    模型清单来源是 ``api_models._model_list``（config 里列出的模型 / live 拉取）。
    清单为空（``source=none``，即该 provider 在 config 里没写 models 段）时**不硬拒**，
    而是标 ``unchecked`` 并把原因写清 —— 拒绝一个真实存在的模型比放过一个可疑的更难收拾。
    """
    listing = models_for(home, agent, provider)
    if not listing.get("ok"):
        return {"ok": False, "code": listing.get("code") or "invalid_provider",
                "message": listing.get("message") or "provider 校验失败"}
    names = [str(n) for n in (listing.get("models") or [])]
    source = str(listing.get("source") or "none")
    model = str(model or "").strip()
    if not model:
        return {"ok": False, "code": "invalid_model", "message": "缺少 model（必填）",
                "models": names, "source": source}
    if names and model not in names:
        return {"ok": False, "code": "unknown_model",
                "message": "provider %s 的模型清单里没有 %r（可选 %d 个：%s）"
                           % (listing.get("provider") or provider, model, len(names),
                              "、".join(names[:12]) + ("…" if len(names) > 12 else "")),
                "models": names, "source": source, "provider": listing.get("provider")}
    return {"ok": True, "provider": listing.get("provider") or provider, "model": model,
            "models": names, "source": source,
            "unchecked": not names,
            "unchecked_reason": (None if names else
                                 "该 provider 在 config.yaml 里没有 models 段，模型名无法离线校验")}


def resolve(home: Path, agent: Optional[str] = None) -> Dict[str, Any]:
    """解析**实际生效**的 provider/model（含来源层）。这是「传给 AI 任务的那一份」。

    来源层（``layer``）：
      * ``agent``          该 Agent 独立配置（``mode=module`` 且点了 provider/model）
      * ``agent_global``   该 Agent 独立声明为「跟随全局」，实际值取该 agent config.yaml
      * ``global``         该 Agent 没有声明，用 ``global`` 段
      * ``module_default`` 什么声明都没有，回落该 Agent config.yaml 当前模型
    无论哪一层，只要最终值来自 config.yaml，``source`` 就是 ``global``（UI 的老口径）；
    ``layer`` 才是「这份配置是从哪一层来的」。
    """
    cfg = load(home)
    target = str(agent or "default")
    group, layer = _group_for(cfg, target)
    out: Dict[str, Any] = {"agent": target, "mode": group.get("mode") or MODE_GLOBAL,
                           "layer": layer, "layer_label": LAYER_LABELS.get(layer),
                           "source": "module_default", "provider": None, "model": None,
                           "reason": "", "config_path": str(config_path(home)),
                           "config_exists": bool(cfg.get("exists")),
                           "config_schema": cfg.get("schema"), "config_legacy": bool(cfg.get("legacy"))}
    if cfg.get("error"):
        out["reason"] = "配置文件读不出来，回落到该 agent 的全局模型：" + str(cfg["error"])
    if group.get("declared") and group.get("mode") == MODE_MODULE \
            and group.get("provider") and group.get("model"):
        out.update({"source": "module", "provider": group["provider"], "model": group["model"]})
        out["reason"] = (out["reason"] or "") or \
            "项目分组独立配置（auto-group/%s · 层=%s）" % (CONFIG_NAME, out["layer_label"])
        return out
    if group.get("declared") and group.get("mode") == MODE_MODULE:
        # 声明成「独立」却没给 provider/model：如实说出来，别静默当跟随全局。
        out["reason"] = (out["reason"] + "；" if out["reason"] else "") + \
            "该 Agent 声明了独立配置但缺 provider/model，回落到 config.yaml"
    # 跟随全局：真值来自该 agent 的 config.yaml（api_models._current_state，绝不读进程缓存）
    api = _models_api()
    try:
        state = api._current_state(target)
    except Exception as exc:  # noqa: BLE001
        out["reason"] = (out["reason"] + "；" if out["reason"] else "") + \
            "读该 agent 全局模型失败：%s" % (type(exc).__name__ + "：" + str(exc))
        return out
    provider, model = str(state.get("provider") or ""), str(state.get("model") or "")
    out.update({"source": "global", "provider": provider or None, "model": model or None})
    if not (provider and model):
        out["source"] = "none"
        out["reason"] = (out["reason"] + "；" if out["reason"] else "") + \
            "该 agent 的 config.yaml 里没有 provider/model，且项目分组没有为此 Agent 配置独立模型"
    else:
        out["reason"] = out["reason"] or (
            "跟随全局（agent %s 的 config.yaml 当前模型 · 层=%s）" % (target, out["layer_label"]))
    return out


def describe(home: Path, agent: Optional[str] = None) -> Dict[str, Any]:
    """界面要的一整块：配置原文 + 实际生效值 + 该 agent 的 provider 目录（脱敏）。"""
    cfg = load(home)
    target = str(agent or "default")
    resolved = resolve(home, target)
    opts = options(home, target)
    listing = None
    if resolved.get("provider"):
        listing = models_for(home, target, resolved["provider"])
    declared = {}
    for name, block in (cfg.get("agents") or {}).items():
        declared[name] = dict(block, layer=LAYER_LABELS.get(
            LAYER_AGENT if block.get("mode") == MODE_MODULE else LAYER_AGENT_GLOBAL))
    return {"ok": True, "config": cfg, "agent": target, "resolved": resolved,
            "agent_declaration": load_agent(home, target),
            "declared_agents": declared,
            "global_declaration": dict(cfg.get("global") or {}, layer=LAYER_LABELS.get(LAYER_GLOBAL)),
            "providers": (opts.get("providers") or []) if opts.get("ok") else [],
            "providers_error": None if opts.get("ok") else opts.get("message"),
            "agent_current": (opts.get("current") or {}) if opts.get("ok") else {},
            "models": (listing or {}).get("models") or [],
            "models_source": (listing or {}).get("source"),
            "audit_path": str(audit_path(home)),
            "notes": [
                "密钥只显示变量名与是否存在（key_env / key_present），取值一律不回显",
                "按 Agent 独立：每个 agent 一组 provider/model，互不覆盖；"
                "没有任何声明的 agent 回落到 global 段，再回落到它自己 config.yaml 的当前模型",
                "「跟随全局」= 用该 agent config.yaml 里当前生效的模型；"
                "「项目分组独立」= 用 auto-group/%s 里点名的 provider/model" % CONFIG_NAME,
                "调用时显式传 provider/model（task=None），不读 auxiliary.title_generation",
            ]}


# --------------------------------------------------------------------------
# 调用 + 审计
# --------------------------------------------------------------------------
def make_caller(home: Path, agent: Optional[str] = None, *, temperature: float = 0.1,
                max_tokens: int = 800, timeout: float = CALL_TIMEOUT):
    """造一个 ``caller(messages, task=…, …) -> {text, provider, model, usage, elapsed_ms}``。

    引擎 ``autogroup_ai.build_preview`` 接受任意 caller，这里给的就是「本项目自己的模型」。
    provider/model 解析不出来时返回 ``None``，让上层如实报「没有可用模型」而不是静默降级。
    """
    resolved = resolve(home, agent)
    if not (resolved.get("provider") and resolved.get("model")):
        return None, resolved
    provider, model, target = resolved["provider"], resolved["model"], resolved["agent"]

    def _call(messages: list, *, task: Optional[str] = None, purpose: str = "auto-group",
              temperature: float = temperature, max_tokens: int = max_tokens,
              timeout: float = timeout) -> Dict[str, Any]:
        started = time.time()
        record: Dict[str, Any] = {"ts": started, "agent": target, "provider": provider,
                                  "model": model, "source": resolved.get("source"),
                                  "layer": resolved.get("layer"),
                                  "purpose": purpose, "ok": False, "error": None,
                                  "elapsed_ms": None, "usage": None, "task": task}
        try:
            from agent import auxiliary_client as ac  # 延迟 import：宿主的 aux 调用器
        except Exception as exc:  # noqa: BLE001
            record["error"] = "auxiliary_client 不可用：" + type(exc).__name__ + "：" + str(exc)
            record["elapsed_ms"] = int((time.time() - started) * 1000)
            append_audit(home, record)
            raise RuntimeError(record["error"]) from exc
        route_info: Dict[str, Any] = {}
        latency_info: Dict[str, Any] = {}
        try:
            resp = ac.call_llm(
                # task=None + 显式 provider/model ⇒ provider/model 只认本项目配置
                task=None, provider=provider, model=model, messages=messages,
                temperature=temperature, max_tokens=max_tokens, timeout=timeout,
                route_info=route_info, latency_info=latency_info,
            )
            extractor = getattr(ac, "extract_content_or_reasoning", None)
            text = str(extractor(resp) or "") if callable(extractor) else ""
            usage_obj = getattr(resp, "usage", None)
            usage = None
            if usage_obj is not None:
                usage = {k: _int_or_none(usage_obj, k)
                         for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
            elapsed = latency_info.get("summary_generation_ms") if isinstance(latency_info, dict) else None
            record.update({"ok": bool(text), "usage": usage,
                           "elapsed_ms": int(elapsed) if isinstance(elapsed, (int, float))
                           else int((time.time() - started) * 1000),
                           "route": {k: route_info.get(k) for k in ("provider", "model")}})
            append_audit(home, record)
            return {"text": text, "provider": route_info.get("provider") or provider,
                    "model": route_info.get("model") or model, "usage": usage,
                    "elapsed_ms": record["elapsed_ms"], "audit": record}
        except Exception as exc:  # noqa: BLE001 - 调用失败要留审计，不能静默
            record["error"] = type(exc).__name__ + "：" + str(exc)
            record["elapsed_ms"] = int((time.time() - started) * 1000)
            append_audit(home, record)
            raise

    return _call, resolved


def _int_or_none(obj: Any, name: str) -> Optional[int]:
    value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def append_audit(home: Path, record: Dict[str, Any]) -> None:
    """追加一行审计（jsonl）。写不进去不影响分类结果，但错误会留在 ``record`` 里。"""
    try:
        path = audit_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_audit(home: Path, limit: int = 20) -> List[Dict[str, Any]]:
    """最近 N 行审计（界面「AI 实际调用记录」用）。"""
    path = audit_path(home)
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for line in lines[-max(1, int(limit)):]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out

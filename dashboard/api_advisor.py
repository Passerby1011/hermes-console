# -*- coding: utf-8 -*-
"""hermes-console · Advisor 审核台后端（C9b）：把 C9a 的建议台账包成 HTTP。

挂载契约：本文件不单独挂载。``plugin_api.py`` 把本目录加进 ``sys.path`` 后 import 本模块，再
``include_router(api_advisor.router)``；最终 URL 是 ``/api/plugins/hermes-console/advisor/...``。

**不自己读日志拼数据。** 建议、状态、事件时间线、统计、策略设置全部来自
``plugins/advisor/audit.py``（C9a 交付的 append-only 台账）。本文件只做四件事：
① HTTP 包装与参数校验；② 写后回读（不信写操作自己的返回值）；③ 把台账里 UI 需要但没暴露的
派生量算出来（关键词过滤、7 天趋势、会话短 id）；④ 把模块不可用降级成结构化错误。

路由：
  ``GET  /advisor``                自检（模块路径 / 各 profile 台账 / 行数 / 路由清单）
  ``GET  /advisor/advice``         分页 + 筛选（agent / status / severity / q / limit / offset）
  ``GET  /advisor/advice/{id}``    单条详情 + 完整事件时间线（跨 profile 查找）
  ``GET  /advisor/reviews``        评审流水（有建议 / 无建议 / 出错 / 跳过原因 + 模型与 token）
  ``POST /advisor/decision``       ``{id, decision: accepted|rejected, reason}``（只记录，不执行）
  ``GET  /advisor/summary``        统计卡 + severity 分布 + 最近 7 天趋势（可 ``?agent=``）
  ``GET  /advisor/coverage``       覆盖面矩阵（每个 profile 一行：装没装 / 启用没启用 / 台账 / sessions /
                                   能否投递 / 状态 + 可操作诊断）——**全只读**
  ``GET  /advisor/settings``       白名单设置（含只读的 blocker_forced_delivery；``?agent=`` 指定 profile）
  ``PUT  /advisor/settings``       写白名单设置；只读字段一改就 400（``?agent=`` 指定写哪个 profile）
  ``GET  /advisor/modes``          评审模式（提示词模式）有哪几种 / 当前用哪种 / 为什么不能切

台账按 profile 分文件（主 home 记作 ``default``，``profiles/<name>`` 属于该 profile），
所以读侧**聚合各 profile 的台账**并标注来源（``profile_source``），写侧（决定）写回
建议所在的那个文件——绝不靠改进程环境变量去切 profile。

四条硬约束：
① **单向阀门不可关闭**：``blocker_forced_delivery`` 只出现在响应里；PUT 里想改它（哪怕改成
   ``false``）一律 400 ``invalid_argument``，并写明「单向阀门不可关闭」。这里没有、也不许有
   任何「关闭 blocker 提示」「把 blocker 降级」的入口。
② **采纳/拒绝只写决定**：``record_decision`` 只往台账追加一行 ``accepted``/``rejected`` 事件，
   不执行建议内容、不改任何被评审的代码或配置。
③ **请求内惰性 import**：``advisor.audit`` 在每次请求里才加载。它坏了只让本分区返回 503，
   控制台其它分区照常（照 C3 的既有做法，不把 import 放在模块顶层）。
④ 失败一律 ``{ok:false, error:{code,message}}``，code 只有三个值：
   ``not_found`` / ``invalid_argument`` / ``upstream_error``。
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.util
import json
import os
import re
import sys
import threading
import types
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from fastapi import APIRouter, Body, Query, Request  # type: ignore
from fastapi.responses import JSONResponse  # type: ignore

_DASHBOARD_DIR = Path(__file__).resolve().parent
if str(_DASHBOARD_DIR) not in sys.path:
    # 兄弟模块 api_agents 只能靠 sys.path 找到：本文件被 dashboard 用合成模块名 exec，无包上下文。
    sys.path.insert(0, str(_DASHBOARD_DIR))
from api_agents import DEFAULT_PROFILE, _profile_dir, _profile_ids, hermes_home  # noqa: E402

router = APIRouter()

#: 台账模块的导入名。C9a 的插件包名是 ``advisor``（``plugins/advisor/audit.py``）。
AUDIT_MODULE_NAME = "advisor.audit"
#: 宿主里插件可能已被加载成 ``hermes_plugins.advisor``（plugins_loader 的命名），复用同一份模块。
HOST_AUDIT_NAMES = ("hermes_plugins.advisor.audit",)

#: 列表每页上限（与 api_autogroup 的 200 保持一致的量级）。
ADVICE_MAX_LIMIT = 200
#: ``q`` 关键词长度上限：超长的当参数错误，不做静默截断。
QUERY_MAX = 120
#: 7 天趋势的窗口。
TREND_DAYS = 7
#: 合法 severity（台账 schema 只认这三个）。
SEVERITIES = ("nit", "concern", "blocker")
#: 合法状态（与 ``advisor.audit.STATUSES`` 同集合；面板在过滤前先校验，报错比静默空表好）。
STATUSES = ("pending", "delivered", "accepted", "rejected", "dropped")


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def advisor_dir() -> Path:
    """``plugins/advisor``（与本插件同树，不看 HERMES_HOME 环境变量）。"""
    return _DASHBOARD_DIR.parents[1] / "advisor"


def _home() -> Path:
    """Hermes 主目录（所有 profile 的共同父目录），复用 api_agents 的布局探测。"""
    return hermes_home()


def _main_audit_dir() -> Path:
    """主目录下的台账目录（``<home>/advisor/audit``），只用于「口径不一致」时给人指路。"""
    return _home() / "advisor" / "audit"


# ---------------------------------------------------------------------------
# 跨 profile 台账
# ---------------------------------------------------------------------------
# 台账是**按 profile 分文件**的：主 home 的 ``advisor/audit/advice.jsonl`` 属于
# ``default``，``profiles/<name>/advisor/audit/advice.jsonl`` 属于那个 profile。
# 面板原来只读主 home，于是 worker 的台账根本不可见——那不是「没有建议」，而是
# 「没读那个文件」。这里自己按文件读、按来源标注，并把三种状态分开：
#
#   ``no_ledger``   该 profile 还没有台账文件（从未评审过，或插件副本还没有审计层）
#   ``no_advice``   有台账文件但没有建议（评审跑过、没发现问题）
#   ``has_advice``  有建议
#
# ``reviews.jsonl`` 是同一目录下的评审流水（每次评审一行）：它让「跑过但没建议」
# 有据可查，而不是靠「台账空 ⇒ 没跑」这种猜测。
AGENT_STATES = ("no_ledger", "no_advice", "has_advice")
LEDGER_FILE = "advice.jsonl"
REVIEWS_FILE = "reviews.jsonl"
SETTINGS_FILE = "settings.json"
DEFAULT_AGENT = "default"


def profiles_root() -> Path:
    """``<home>/profiles``（各 profile 的 home 都在这里）。"""
    return _home() / "profiles"


def profile_audit_dirs() -> List[Tuple[str, Path]]:
    """``[(profile 名, 台账目录)]``：主 home 记作 ``default``，其后按名字排序。"""
    dirs: List[Tuple[str, Path]] = [(DEFAULT_AGENT, _main_audit_dir())]
    try:
        children = sorted((p for p in profiles_root().iterdir() if p.is_dir()),
                          key=lambda p: p.name)
    except OSError:
        children = []
    for child in children:
        dirs.append((child.name, child / "advisor" / "audit"))
    return dirs


def known_agents() -> List[str]:
    return [profile for profile, _ in profile_audit_dirs()]


def _read_jsonl(path: Path) -> Tuple[List[Dict[str, Any]], str]:
    """按行读一个 JSONL 文件。坏行跳过（与台账模块的容错一致），返回 ``(行, 错误)``。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], ""
    except OSError as exc:
        return [], _describe(exc)
    rows: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            rows.append(data)
    return rows, ""


def _agent_ledger(profile: str, directory: Path, audit: Any) -> Dict[str, Any]:
    """读一个 profile 的台账 + 评审流水，折叠成当前状态。

    ``profile`` 是**来源目录**（文件在哪），每条建议另外保留台账自己写的
    ``profile`` 字段：两者不一致正是「建议被记到了别的 profile」的可见证据。
    """
    ledger = directory / LEDGER_FILE
    reviews_file = directory / REVIEWS_FILE
    supported = set(getattr(audit, "EVENTS", ()))
    events, ledger_error = _read_jsonl(ledger)
    events = [event for event in events if event.get("event") in supported]
    folded = audit.fold(events)
    items: List[Dict[str, Any]] = []
    for item in folded.values():
        # 目标来源只在**事件**里（C 卡新增字段），fold 后要趁 events 还在就取出来：
        # 下面这行会剥掉 events，剥完再问就是永远的 unknown（面板会静默显示「未知目标」）。
        goal_source, goal_ref = _goal_of(item)
        row = {key: value for key, value in item.items() if key != "events"}
        row["profile_source"] = profile
        row["goal_source"] = goal_source
        row["goal_ref"] = goal_ref
        items.append(row)

    outcomes = set(getattr(audit, "REVIEW_OUTCOMES", ("advised", "no_advice", "error", "skipped")))
    reviews, reviews_error = _read_jsonl(reviews_file)
    reviews = [record for record in reviews if record.get("outcome") in outcomes]
    reviews.sort(key=lambda record: (record.get("ts") or "", record.get("review_id") or ""),
                 reverse=True)

    if not ledger.is_file():
        state = "no_ledger"
    elif items:
        state = "has_advice"
    else:
        state = "no_advice"

    terminal = set(getattr(audit, "TERMINAL_REVIEW_OUTCOMES",
                            ("advised", "no_advice", "error", "skipped")))
    review_counts: Dict[str, int] = {name: 0 for name in outcomes}
    finished_ids: set = set()
    for record in reviews:
        review_counts[record["outcome"]] = review_counts.get(record["outcome"], 0) + 1
        if record["outcome"] in terminal and record.get("review_id"):
            finished_ids.add(record["review_id"])
    # 评审开始过但没有终态记录 = 评审在飞行中丢了（进程先退出）
    in_flight = [record for record in reviews
                 if record["outcome"] == "started" and record.get("review_id") not in finished_ids]

    return {
        "profile": profile,
        "ledger": str(ledger),
        "ledger_exists": ledger.is_file(),
        "events": len(events),
        "advice_total": len(items),
        "state": state,
        "reviews_path": str(reviews_file),
        "reviews_exists": reviews_file.is_file(),
        "review_total": len(finished_ids),
        "review_lines": len(reviews),
        "review_in_flight": len(in_flight),
        "review_outcomes": review_counts,
        "reviews": reviews,
        "error": ledger_error or reviews_error,
        "items": items,
    }


def _agents(audit: Any, only: str = "") -> List[Dict[str, Any]]:
    """一个或多个 profile 的台账；``only`` 指定 agent 时只读那一个。"""
    wanted = (only or "").strip()
    return [
        _agent_ledger(profile, directory, audit)
        for profile, directory in profile_audit_dirs()
        if not wanted or profile == wanted
    ]


def _agent_brief(agent: Dict[str, Any]) -> Dict[str, Any]:
    """``agents`` 清单里的一项：够面板判断「有没有台账 / 有没有建议」。"""
    return {
        "profile": agent["profile"],
        "ledger": agent["ledger"],
        "ledger_exists": agent["ledger_exists"],
        "events": agent["events"],
        "advice_total": agent["advice_total"],
        "state": agent["state"],
        "reviews_path": agent["reviews_path"],
        "reviews_exists": agent["reviews_exists"],
        "review_total": agent["review_total"],
        "review_in_flight": agent["review_in_flight"],
        "review_outcomes": agent["review_outcomes"],
        "error": agent["error"],
    }


def _state_note(agent: Dict[str, Any]) -> str:
    """把三种状态说成人话，并给出「为什么空」的直接原因。"""
    profile = agent["profile"]
    if agent["state"] == "no_ledger":
        note = (profile + "：没有台账文件 " + agent["ledger"]
                + "（该 agent 从未完成过一次评审，或它的插件副本还没有审计层）")
        if agent["review_total"]:
            note += "；但评审流水有 " + str(agent["review_total"]) + " 条记录，见 " + agent["reviews_path"]
        return note
    if agent["state"] == "no_advice":
        note = (profile + "：有台账文件但没有任何建议（评审跑过、没发现问题；"
                + "评审流水 " + str(agent["review_total"]) + " 次）")
        if agent.get("review_in_flight"):
            note += ("；另有 " + str(agent["review_in_flight"])
                     + " 次评审只记录了开始、没有结果（进程先退出，属丢失，不算「无建议」）")
        return note
    return (profile + "：" + str(agent["advice_total"]) + " 条建议，台账 " + agent["ledger"])


# ---------------------------------------------------------------------------
# 覆盖面矩阵（每个 profile 一行，**全只读**）
# ---------------------------------------------------------------------------
# 面板原来只能看到「有没有台账文件」，于是两件完全不同的事长得一模一样：
#   ① config 里根本没启用 advisor；② 启用了、也在评审，但跑着的进程还是接入台账
#      之前的旧代码（插件不热加载），于是台账永远是空的。
# 两者都表现为「没有数据」。覆盖面把这两件事拆开：每个 profile 一行，装没装、
# config 里启用没启用、台账有没有事件、sessions 有几个、允许不允许投递，最后给
# 一个状态 + 一句**可操作**诊断（见 ``_coverage_diagnosis``）。
#
# 本分区只 stat/read：不写、不改任何 profile 的配置或台账。
COVERAGE_STATUSES = ("active", "reviewing_only", "installed_disabled", "not_installed")
#: 防御性状态：有台账事件但没允许投递。今天没有 profile 命中；单列是因为
#: 「在记账」和「投得出去」是两件事，并进 active 又会变成看不见的静默。
COVERAGE_STATUS_LEDGER_NO_DELIVERY = "ledger_no_delivery"
#: ``allow_gateway_injection`` 在 profile 自己的 config.yaml 里的位置。
INJECTION_KEY_PATH = ("plugins", "entries", "advisor", "allow_gateway_injection")


def _profile_home(profile: str) -> Path:
    """``default`` = 主 home；其它 = ``<home>/profiles/<name>``。"""
    return _profile_dir(profile, _home())


def _plugin_dir(profile: str) -> Path:
    """该 profile 自己的插件目录（``<profile home>/plugins/advisor``）。"""
    return _profile_home(profile) / "plugins" / "advisor"


def _plugin_manifest(profile: str) -> Dict[str, Any]:
    """``(装没装, 版本, 清单路径)``：目录在但清单读不到也算「装了」（半装比没装更要说清）。"""
    directory = _plugin_dir(profile)
    manifest = directory / "plugin.yaml"
    try:
        installed = directory.is_dir() or manifest.is_file()
    except OSError:
        installed = False
    version = ""
    if manifest.is_file():
        try:
            import yaml  # 宿主自带；缺失时只是「没有版本号」

            data = yaml.safe_load(manifest.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                version = str(data.get("version") or "")
        except Exception:  # noqa: BLE001 - 版本号读不到不该让整行消失
            version = ""
    return {"installed": installed, "version": version, "manifest": str(manifest),
            "plugin_dir": str(directory)}


def _profile_config(profile: str) -> Dict[str, Any]:
    """``(config 字典, 路径, 错误)``：读不到就返回空字典 + 原因（不猜配置）。"""
    path = _profile_home(profile) / "config.yaml"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {}, str(path), "没有 config.yaml"
    except OSError as exc:
        return {}, str(path), _describe(exc)
    try:
        import yaml  # 宿主自带

        data = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 - 解析失败只是「读不出配置」
        return {}, str(path), _describe(exc)
    return (data if isinstance(data, dict) else {}), str(path), ""


def _config_enabled(config: Dict[str, Any]) -> bool:
    """advisor 插件在该 profile 的 config.yaml 里是否启用（三个落点任一明确关闭即关闭）。

    * ``plugins.entries.advisor.enabled`` —— 本面板「启停插件」写这个布尔值；
    * ``plugins.enabled`` 名单 —— 宿主**真正**据此决定加载（含 advisor 才算启用）；
    * ``plugins.disabled`` —— 宿主 deny-list，出现 advisor 一律算关闭（优先级最高）。

    缺省（无 config / 无这些键）视为未启用，与「必须显式写进 enabled 名单才会被加载」对齐。
    """
    plugins = config.get("plugins") if isinstance(config, dict) else None
    if not isinstance(plugins, dict):
        return False
    disabled = plugins.get("disabled")
    if isinstance(disabled, list) and any(str(name).strip() == "advisor" for name in disabled):
        return False
    entries = plugins.get("entries")
    advisor_entry = entries.get("advisor") if isinstance(entries, dict) else None
    if isinstance(advisor_entry, dict) and advisor_entry.get("enabled") is False:
        return False
    enabled = plugins.get("enabled")
    if isinstance(enabled, list):
        return any(str(name).strip() == "advisor" for name in enabled)
    return False


def _config_injection(config: Dict[str, Any]) -> bool:
    """``plugins.entries.advisor.allow_gateway_injection`` 是否 **严格 true**。

    缺省 / 字符串 "true" / 1 一律算 False：这条决定的是「建议能不能投到会话里」，
    只有明确写 true 才算开（与宿主读配置的严格程度对齐，宁可少投也不误投）。
    """
    node: Any = config if isinstance(config, dict) else {}
    for key in INJECTION_KEY_PATH:
        node = node.get(key) if isinstance(node, dict) else None
    return node is True


def _sessions_summary(profile: str) -> Dict[str, Any]:
    """``advisor/sessions/*.json`` 的个数与其中暂扣（held）建议合计。"""
    directory = _profile_home(profile) / "advisor" / "sessions"
    files = 0
    held = 0
    newest = ""
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        paths = []
    for path in paths:
        files += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        notes = data.get("held_notes") if isinstance(data, dict) else None
        if isinstance(notes, list):
            held += len(notes)
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()
        except OSError:
            continue
        if mtime > newest:
            newest = mtime
    return {"dir": str(directory), "files": files, "held_notes": held, "newest_mtime": newest}


def _settings_file_state(profile: str) -> Dict[str, Any]:
    """该 profile 的 ``advisor/audit/settings.json``（只读快照，不加载台账模块）。"""
    path = _profile_home(profile) / "advisor" / "audit" / SETTINGS_FILE
    stored: Dict[str, Any] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        if isinstance(data, dict):
            stored = data
    except FileNotFoundError:
        stored = {}
    except (OSError, ValueError):
        stored = {}
    return {"path": str(path), "exists": path.is_file(), "stored": stored,
            # 与 advisor/audit.py 的 DEFAULT_SETTINGS 一致：没写文件时 enabled 默认 True
            "enabled": stored.get("enabled", True) is not False}


def _coverage_row(profile: str, audit: Any) -> Dict[str, Any]:
    """一个 profile 的覆盖行（只读文件与配置，不写任何东西）。"""
    manifest = _plugin_manifest(profile)
    config, config_path, config_error = _profile_config(profile)
    enabled = _config_enabled(config)
    injection = _config_injection(config)
    audit_directory = _profile_home(profile) / "advisor" / "audit"
    ledger = audit_directory / LEDGER_FILE
    events, ledger_error = _read_jsonl(ledger)
    supported = set(getattr(audit, "EVENTS", ())) if audit is not None else set()
    if supported:
        events = [event for event in events if event.get("event") in supported]
    else:
        events = [event for event in events if event.get("event")]
    reviews, reviews_error = _read_jsonl(audit_directory / REVIEWS_FILE)
    sessions = _sessions_summary(profile)
    settings = _settings_file_state(profile)
    try:
        audit_dir_exists = audit_directory.is_dir()
    except OSError:
        audit_dir_exists = False
    timestamps = [str(event.get("ts") or "") for event in events]
    review_times = [str(record.get("ts") or "") for record in reviews]
    row: Dict[str, Any] = {
        "profile": profile,
        "profile_home": str(_profile_home(profile)),
        "plugin_installed": bool(manifest["installed"]),
        "plugin_version": manifest["version"],
        "plugin_dir": manifest["plugin_dir"],
        "plugin_manifest": manifest["manifest"],
        "enabled_in_config": enabled,
        "config_path": config_path,
        "config_error": config_error,
        "injection_allowed": injection,
        "injection_key": ".".join(INJECTION_KEY_PATH),
        "audit_dir": str(audit_directory),
        "audit_dir_exists": audit_dir_exists,
        "ledger": str(ledger),
        "ledger_exists": ledger.is_file(),
        "ledger_events": len(events),
        "ledger_advice": len(audit.fold(events)) if audit is not None else len(
            {str(event.get("id") or "") for event in events}),
        "last_event_at": max(timestamps) if timestamps else "",
        "reviews_path": str(audit_directory / REVIEWS_FILE),
        "review_lines": len(reviews),
        "last_review_at": max(review_times) if review_times else "",
        "sessions_dir": sessions["dir"],
        "session_files": sessions["files"],
        "held_notes": sessions["held_notes"],
        "last_session_at": sessions["newest_mtime"],
        "settings_path": settings["path"],
        "settings_exists": settings["exists"],
        "settings_enabled": settings["enabled"],
        "error": ledger_error or reviews_error,
    }
    row["status"] = _coverage_status(row)
    row["diagnosis"] = _coverage_diagnosis(row)
    return row


def _coverage_status(row: Dict[str, Any]) -> str:
    """四态（外加一个防御性态）：装没装 → 启用没启用 → 台账有没有事件 → 能否投递。

    * ``not_installed``     没有插件目录
    * ``installed_disabled`` 装了但 config 没启用（``enabled_in_config=false``）；
      或者启用了却从未产生任何评审证据（0 sessions、0 评审流水、0 台账事件）
    * ``active``            启用 + 有台账事件 + 允许投递
    * ``reviewing_only``    启用 + 台账 0 条，但 sessions / 评审流水有记录
    * ``ledger_no_delivery`` 有台账事件却没允许投递（今天没有 profile 命中）
    """
    if not row["plugin_installed"]:
        return "not_installed"
    if not row["enabled_in_config"]:
        return "installed_disabled"
    if row["ledger_events"]:
        return "active" if row["injection_allowed"] else COVERAGE_STATUS_LEDGER_NO_DELIVERY
    if row["session_files"] or row["review_lines"]:
        return "reviewing_only"
    return "installed_disabled"


def _coverage_diagnosis(row: Dict[str, Any]) -> str:
    """一句**可操作**诊断：每个状态说清「为什么没数据」和「下一步做什么」。

    ``reviewing_only`` 那句是本轮排查最花时间的结论，必须自解释：插件确实在评审
    （sessions 里有记录），台账却是空的 —— 因为跑着的进程还是接入台账之前的旧代码，
    插件代码不热加载，重启该 profile 的 gateway/serve 之后才会开始写台账。
    """
    profile = row["profile"]
    status = row["status"]
    if status == "not_installed":
        return (profile + "：没装 advisor 插件（" + row["plugin_dir"]
                + " 不存在），所以它不可能产出评审。要用就在该 profile 里装插件并在 config 里启用。")
    if status == "installed_disabled":
        if not row["enabled_in_config"]:
            return (profile + "：插件已装（v" + (row["plugin_version"] or "?")
                    + "），但 config.yaml 的 plugins.enabled 里没有 advisor —— 它不会评审。"
                    "要启用就把 advisor 加进该 profile 的 plugins.enabled（本面板不代改配置）。")
        return (profile + "：插件已装且在 config 里启用，但从未产生评审（sessions 0 个、"
                "评审流水 0 条、台账 0 条事件）——要么这个 agent 还没跑过对话，"
                "要么它跑起来的进程没加载到插件（看该 profile 的 logs 里有没有 advisor.diag）。")
    if status == "reviewing_only":
        return (profile + "：插件已加载但台账为空：该 profile 的运行进程可能仍是旧代码，"
                "需重启该 profile 的 gateway/serve 才会写入台账。"
                "证据：sessions 有 " + str(row["session_files"]) + " 个文件（"
                + str(row["held_notes"]) + " 条暂扣建议）、评审流水 " + str(row["review_lines"])
                + " 行，台账 " + row["ledger"] + " 却是空的。")
    if status == COVERAGE_STATUS_LEDGER_NO_DELIVERY:
        return (profile + "：台账有 " + str(row["ledger_events"]) + " 条事件，但 "
                + row["injection_key"] + " 不是 true —— 建议产得出来却投不进会话。"
                "要投递就在该 profile 的 config.yaml 里显式写 true（本面板不代改配置）。")
    note = (profile + "：在记账（台账 " + str(row["ledger_events"]) + " 条事件 / "
            + str(row["ledger_advice"]) + " 条建议）且允许投递")
    if not row["injection_allowed"]:
        note += "（但 " + row["injection_key"] + " 不是 true）"
    if not row["settings_enabled"]:
        note += "；注意它的 settings.json 里 enabled=false，评审其实被关掉了"
    return note + "。"


def _coverage_notes(rows: List[Dict[str, Any]]) -> List[str]:
    """矩阵整体的一句话结论（比逐行更早被读到）。"""
    counts = {name: 0 for name in COVERAGE_STATUSES}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    notes = ["覆盖口径：active = 有台账事件且允许投递；reviewing_only = 有评审记录但台账为空；"
             "installed_disabled = 装了但没启用（或启用了却从未评审）；not_installed = 没装。"]
    notes.append("合计 " + str(len(rows)) + " 个 profile：active " + str(counts["active"])
                 + " / reviewing_only " + str(counts["reviewing_only"])
                 + " / installed_disabled " + str(counts["installed_disabled"])
                 + " / not_installed " + str(counts["not_installed"]))
    if counts["reviewing_only"]:
        notes.append("有 " + str(counts["reviewing_only"]) + " 个 profile 在评审但台账为空 —— "
                     "插件代码不热加载，重启对应 profile 的 gateway/serve 之后才会开始写台账。")
    if counts["active"] and any(not row["injection_allowed"] for row in rows
                                if row["status"] == "active"):
        notes.append("有的 active profile 没允许投递：见每行 injection_allowed。")
    return notes


# ---------------------------------------------------------------------------
# 惰性 import
# ---------------------------------------------------------------------------
class AdvisorUnavailable(RuntimeError):
    """``advisor.audit`` 加载失败 —— 带上卡在哪一步，便于人直接照原因排查。"""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(detail)
        self.stage = stage
        self.detail = detail


def _ensure_sys_path() -> None:
    """把 ``<home>/plugins`` 与 ``<home>/hermes-agent`` 补进 sys.path。

    ``advisor`` 是**用户插件包**（不在宿主源码树里），``audit.py`` 又用相对 import
    （``from .models import ...``）并延迟调用宿主（``hermes_constants`` / ``utils``）。
    生产里 dashboard 进程的 sys.path 通常已有 hermes-agent 根，但 plugins 目录不一定有，
    所以这里显式补齐；已存在的不重复插。
    """
    home = _home()
    for path in (home / "plugins", home / "hermes-agent"):
        try:
            if path.is_dir() and str(path) not in sys.path:
                sys.path.insert(0, str(path))
        except OSError:
            continue


def _reuse_loaded_audit():
    """已经加载过的台账模块（同进程内 advisor 插件自己用过）直接复用，避免出现两份状态。"""
    for name in (AUDIT_MODULE_NAME,) + HOST_AUDIT_NAMES:
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "list_advice"):
            return module
    host_pkg = sys.modules.get("hermes_plugins.advisor")
    candidate = getattr(host_pkg, "audit", None) if host_pkg is not None else None
    if candidate is not None and hasattr(candidate, "list_advice"):
        return candidate
    return None


def _load_by_path() -> Any:
    """按文件路径加载 ``advisor/audit.py``（**不执行** ``__init__.py``）。

    为什么不直接 import 包：``advisor/__init__.py`` 会拉起整个运行时（hooks、模型客户端），
    那是 agent 侧的东西，dashboard 进程只需要台账这一个模块。这里造一个只有 ``__path__``
    的空包当壳，``audit.py`` 里的 ``from .models import normalise_note`` 就能照常解析。
    """
    path = advisor_dir() / "audit.py"
    if not path.is_file():
        raise AdvisorUnavailable("locate", "找不到 advisor 台账模块：" + str(path))
    if "advisor" not in sys.modules:
        shell = types.ModuleType("advisor")
        shell.__path__ = [str(advisor_dir())]  # type: ignore[attr-defined]
        sys.modules["advisor"] = shell
    spec = importlib.util.spec_from_file_location(AUDIT_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise AdvisorUnavailable("spec", "无法为 " + str(path) + " 建立 import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[AUDIT_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _load_audit() -> Any:
    """取台账模块（请求内调用）。失败抛 ``AdvisorUnavailable``，绝不往上冒 ImportError。"""
    loaded = _reuse_loaded_audit()
    if loaded is not None:
        return loaded
    _ensure_sys_path()
    import_error = ""
    try:
        return importlib.import_module(AUDIT_MODULE_NAME)
    except Exception as exc:  # noqa: BLE001 - 任何 import 期异常都算「模块不可用」
        import_error = type(exc).__name__ + "：" + str(exc)
        sys.modules.pop(AUDIT_MODULE_NAME, None)  # 半截模块不能留着
    try:
        return _load_by_path()
    except AdvisorUnavailable as exc:
        raise AdvisorUnavailable(
            "import",
            "advisor.audit 加载失败（import: " + import_error + "；按路径加载: "
            + exc.stage + " " + exc.detail + "）",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise AdvisorUnavailable(
            "import",
            "advisor.audit 加载失败（import: " + import_error + "；按路径加载: "
            + type(exc).__name__ + "：" + str(exc) + "）",
        ) from exc


# ---------------------------------------------------------------------------
# 错误封装
# ---------------------------------------------------------------------------
def _fail(code: str, message: str, status_code: int = 400,
          extra: Optional[Dict[str, Any]] = None) -> JSONResponse:
    """统一失败体：``{ok:false, error:{code,message}}``。code 只有三个值。"""
    content: Dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
    if extra:
        content["error"].update({k: v for k, v in extra.items() if k not in ("code", "message")})
        content.update({k: v for k, v in extra.items() if k not in ("code", "message")})
    return JSONResponse(status_code=status_code, content=content)


def _unavailable(exc: AdvisorUnavailable) -> JSONResponse:
    """模块不可用 → 503。控制台其它分区不受影响（它们各自 import 各自的域模块）。"""
    return _fail(
        "upstream_error",
        "advisor 台账模块不可用，本分区暂时读不到建议（其它分区不受影响）：" + exc.detail,
        status_code=503,
        extra={"stage": exc.stage, "module": AUDIT_MODULE_NAME,
               "module_path": str(advisor_dir() / "audit.py")},
    )


def _describe(exc: BaseException) -> str:
    return type(exc).__name__ + "：" + str(exc)


# ---------------------------------------------------------------------------
# 派生量（UI 需要，台账没暴露）
# ---------------------------------------------------------------------------
def _session_short(session_id: str) -> str:
    """会话短 id：前 8 位（长 id 在窄窗里会把整行撑开）。"""
    text = (session_id or "").strip()
    return text[:8] if text else ""


def _goal_of(item: Dict[str, Any]) -> Tuple[str, str]:
    """从这条建议取**目标来源**（``goal_source`` / ``goal_ref``）。

    ``advisor.audit.fold()`` 只保留建议状态，不保留 ``goal_*``（那是 C 卡新加的
    事件字段），所以这里从原始事件里取最新一个带目标的：台账没有就是 ``unknown``，
    面板显示「未知目标」，不报错也不猜。

    列表路径（``_agent_ledger``）会先剥掉 ``events`` 再往下传，所以它在那里就先把
    ``goal_*`` 算好挂到行上；行上已经有值就直接用，别再拿空的事件列表重算一次
    （那样永远是 unknown —— 这正是「fold 之后读不出 goal」的坑）。
    """
    preset = str(item.get("goal_source") or "")
    if preset:
        return preset, str(item.get("goal_ref") or "")
    source = ""
    ref = ""
    for event in item.get("events") or []:
        if not isinstance(event, dict):
            continue
        value = str(event.get("goal_source") or "")
        if value:
            source = value
            ref = str(event.get("goal_ref") or "")
    return (source or "unknown"), ref


def _row(item: Dict[str, Any]) -> Dict[str, Any]:
    """台账条目 → 列表行（只补派生字段，不改原值）。"""
    goal_source, goal_ref = _goal_of(item)
    return {
        "id": item.get("id", ""),
        "note": item.get("note", ""),
        # 结构自检（target / goal / evidence / impact / next）：只报客观检测结果，不评价好坏
        "note_structure": _note_structure(item.get("note", "")),
        "severity": item.get("severity", ""),
        "status": item.get("status", ""),
        "profile": item.get("profile", ""),
        # 这一行是从哪个 profile 的台账文件读出来的（与建议自报的 profile 分开看）
        "profile_source": item.get("profile_source", ""),
        "goal_source": goal_source,
        "goal_ref": goal_ref,
        "goal_known": goal_source != "unknown",
        "session_id": item.get("session_id", ""),
        "session_short": _session_short(item.get("session_id", "")),
        "turn_id": item.get("turn_id", ""),
        "first_raised_at": item.get("first_raised_at", ""),
        "last_seen_at": item.get("last_seen_at", ""),
        "raise_count": item.get("raise_count", 0),
        "deliver_count": item.get("deliver_count", 0),
        "held_count": item.get("held_count", 0),
        "reconfirmed_count": item.get("reconfirmed_count", 0),
        "event_count": item.get("event_count", 0),
        "decision": item.get("decision"),
        "reason": item.get("reason", ""),
        "decided_at": item.get("decided_at"),
        "actor": item.get("actor", ""),
        "source": item.get("source", ""),
    }


def _matches(item: Dict[str, Any], needle: str) -> bool:
    """关键词命中：正文 / id / 会话 / profile / 状态 / 严重度 / 原因（大小写不敏感）。"""
    if not needle:
        return True
    haystack = " ".join(str(item.get(key) or "") for key in
                        ("note", "id", "session_id", "profile", "status", "severity", "reason"))
    return needle.lower() in haystack.lower()


def _parse_ts(value: str) -> Optional[datetime]:
    """台账 ts → datetime（带时区）。解析不了返回 None（不猜时间）。"""
    text = (value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment


def _trend(events: List[Dict[str, Any]], days: int = TREND_DAYS) -> List[Dict[str, Any]]:
    """最近 N 天每天新增/投递/决定条数（够画一行小字）。

    只数台账里真有的事件：某天没有事件就是 0，不按「平均」补齐。日期按**本地时区**切分
    （台账 ts 自带 offset，先 astimezone 再取 date）。
    """
    today = datetime.now().astimezone().date()
    buckets: Dict[str, Dict[str, Any]] = {}
    ordered: List[str] = []
    for index in range(days - 1, -1, -1):
        day = (today - timedelta(days=index)).isoformat()
        buckets[day] = {"date": day, "raised": 0, "delivered": 0, "decided": 0}
        ordered.append(day)
    for event in events:
        moment = _parse_ts(event.get("ts", ""))
        if moment is None:
            continue
        day = moment.astimezone().date().isoformat()
        bucket = buckets.get(day)
        if bucket is None:
            continue
        kind = event.get("event")
        if kind == "raised":
            bucket["raised"] += 1
        elif kind == "delivered":
            bucket["delivered"] += 1
        elif kind in ("accepted", "rejected"):
            bucket["decided"] += 1
    return [buckets[day] for day in ordered]


def _ledger_note(audit: Any) -> List[str]:
    """台账目录与主目录不一致时说清楚（多 profile 下别让人以为「建议丢了」）。"""
    try:
        resolved = Path(audit.audit_dir())
    except Exception:  # noqa: BLE001
        return []
    main = _main_audit_dir()
    try:
        if resolved.resolve() == main.resolve():
            return []
    except OSError:
        return []
    return ["台账目录是 " + str(resolved) + "（当前进程 profile），主目录台账在 " + str(main)]


# ---------------------------------------------------------------------------
# 建议正文的结构自检（「反馈更专注主题」的可验证部分）
# ---------------------------------------------------------------------------
# 插件本体（``advisor/advisor_prompt.py`` 的 ``<four_elements>``）要求每条建议带四个要素：
# ``goal`` / ``evidence`` / ``impact`` / ``next``，并要求「点名它在评什么」（``target``）。
# **这里只做检测与展示，不判定建议好坏，也不改判定标准**：检测到的就是正文里客观存在的
# 标记与引用（台账原文），检测不到就报「缺」，绝不因为「看起来像」而补一个勾。
#
# **两边的关系：闸门是约束，面板是展示。** 「算不算点了名」只有一个定义，在插件本体
# （``plugins/advisor/models.py::_TARGET_PATTERNS`` + ``runtime.py::_required_elements``）：
# 卡 id / 会话 id / ``comment N`` / ``文件:行`` 四种识别形态，命中才允许投递。面板必须
# 用同一套口径，否则会出现「面板说点了名、闸门却拒收」——用户看着一条永远不会到会话的
# 建议被标成合规，却不知道为什么没到。
#
# 实测（266 行真实台账）：旧口径（多一条 ``quoted``）判「点了名」203 条，其中 175 条只靠
# 一句引号；闸门口径只认 28 条。引号引的是结论（「504 只降了 14%」「部署就绪，桌面效果
# 待验」）而不是对象 —— 引一个标准不是点一个对象。所以 ``quoted`` 从 ``_TARGET_PATTERNS``
# 里去掉，降级为**只作参考**的 ``_QUOTE_PATTERN``：它不参与 ``has_target``，只让面板能
# 说清三态（``target_state``）——``named`` 点了名 / ``quoted_only`` 只引了原话 /
# ``none`` 没点名。
_NOTE_LABELS = (
    ("goal", ("goal", "目标")),
    ("evidence", ("evidence", "证据")),
    ("impact", ("impact", "影响")),
    ("next", ("next", "下一步")),
)
#: 「点名对象」的客观形态：与插件闸门 ``plugins/advisor/models.py::_TARGET_PATTERNS``
#: **逐条一致**（卡 id / 会话 id / ``comment N`` / ``文件:行``）。**别在这里加宽**：
#: 面板多认一种形态，就会把闸门会拒收的建议显示成「点了名」。
_TARGET_PATTERNS = (
    ("kanban_card", re.compile(r"\bt_[0-9a-f]{8}\b")),
    ("session", re.compile(r"\b\d{8}_\d{6}_[0-9a-f]{6,}\b")),
    ("comment", re.compile(r"\bcomment\s*#?\s*\d+\b", re.IGNORECASE)),
    ("file_line", re.compile(r"[\w./\\-]+\.(?:py|js|mjs|cjs|ts|css|json|ya?ml|md|sql|html):\d+")),
)
#: 只作参考的「引号原文」：**不参与** ``has_target``（闸门不认它）。保留它只为把
#: 「只引了原话」和「什么都没写」区分开 —— 前者是引了结论，后者是无话可说。
_QUOTE_PATTERN = re.compile(r"[「“\"][^」”\"]{4,}[」”\"]")

#: target 三态（面板文案与探针共用这套名字）。
TARGET_NAMED = "named"
TARGET_QUOTED_ONLY = "quoted_only"
TARGET_NONE = "none"


def _note_structure(note: str) -> Dict[str, Any]:
    """一条建议正文的结构自检结果（**只报客观存在的东西**）。

    ``labels`` 命中插件的四要素标签（``- goal:`` / ``目标：`` 这类行首标签），
    ``targets`` 只命中**闸门认的四种**识别形态（卡 id / 会话 id / ``comment N`` /
    ``文件:行``）；``quotes`` 是正文里的引号原文，**只作参考**，不算点名。
    ``target_state`` 是三态：``named`` 点了名 / ``quoted_only`` 只引了原话 / ``none``
    没点名。``missing`` 的口径与插件闸门一致（``quoted_only`` 也算缺 ``target``），
    面板照着它说「这条没点名对象」，而不是替它圆场。
    """
    text = note or ""
    lowered = text.lower()
    labels: List[str] = []
    for key, needles in _NOTE_LABELS:
        for needle in needles:
            # 行首标签才算（正文里顺口提到 "goal" 不算写了 goal 要素）
            if re.search(r"(?im)^[\s>*\-]*(?:[-*•]\s*)?" + re.escape(needle) + r"\s*[:：]", lowered):
                labels.append(key)
                break
    targets: List[Dict[str, str]] = []
    for kind, pattern in _TARGET_PATTERNS:
        match = pattern.search(text)
        if match:
            targets.append({"kind": kind, "text": match.group(0)[:80]})
    # 引号原文只作参考（闸门不认）：留着它是为了把「只引了原话」和「什么都没写」分开。
    quotes: List[Dict[str, str]] = [
        {"kind": "quoted", "text": one.group(0)[:80]}
        for one in list(_QUOTE_PATTERN.finditer(text))[:3]
    ]
    if targets:
        target_state = TARGET_NAMED
    elif quotes:
        target_state = TARGET_QUOTED_ONLY
    else:
        target_state = TARGET_NONE
    missing: List[str] = []
    if target_state != TARGET_NAMED:
        missing.append("target")
    for key, _ in _NOTE_LABELS:
        if key not in labels:
            missing.append(key)
    return {
        "labels": labels,
        "targets": targets,
        "quotes": quotes,
        "target_state": target_state,
        # 与插件闸门同口径：只有四种识别形态算「点了名」
        "has_target": target_state == TARGET_NAMED,
        "has_quote_only": target_state == TARGET_QUOTED_ONLY,
        "has_concern": bool(text.strip()),
        "complete": not missing,
        "missing": missing,
        "chars": len(text),
    }


# ---------------------------------------------------------------------------
# 评审模式（提示词模式）：代码里到底有哪几种 —— 读源码，不猜
# ---------------------------------------------------------------------------
# 用户说「有关提示词的模式选择并没有体现出来」。查源码的结论是：**没有 mode 设置项**。
# 评审模式是 ``advisor/goal.py::Goal.prompt_block()`` 的两个分支，由**每次评审时解析出的
# 目标**决定，不是持久化的开关：
#
#   * ``goal_aligned``   解析出目标（kanban 卡 / 用户显式目标 / 首个请求）→ 按目标验收项判定；
#   * ``conservative``   ``goal.source == unknown`` → 只报会破坏或浪费工作的技术风险。
#
# 另外 ``<attention>``（cwd 下的 WATCHDOG.md）是附加段落，不是模式。
# 本模块**只读地**加载这两个插件文件（不执行 ``__init__.py``，不拉起 runtime），
# 把提示词的指纹与两个分支的首行原文贴出来，让人自己核对。
MODE_GOAL_ALIGNED = "goal_aligned"
MODE_CONSERVATIVE = "conservative"
MODE_UNKNOWN = "unknown"
MODE_LABEL = {
    MODE_GOAL_ALIGNED: "目标对齐模式",
    MODE_CONSERVATIVE: "保守模式",
    MODE_UNKNOWN: "未知（还没有可判断的建议）",
}
#: The ``mode`` **setting** (three states). ``auto`` is the shipped default and
#: reproduces the pre-mode behaviour: the resolved goal decides.
SETTING_AUTO = "auto"
SETTING_TARGET = "target"
SETTING_CONSERVATIVE = "conservative"
SETTING_MODES = (SETTING_AUTO, SETTING_TARGET, SETTING_CONSERVATIVE)
SETTING_MODE_LABEL = {
    SETTING_AUTO: "自动（按目标决定）",
    SETTING_TARGET: "目标对齐（强制）",
    SETTING_CONSERVATIVE: "保守（强制）",
}
#: Which resolved mode each setting pins (``auto`` has none — the goal decides).
SETTING_TO_RESOLVED = {
    SETTING_TARGET: MODE_GOAL_ALIGNED,
    SETTING_CONSERVATIVE: MODE_CONSERVATIVE,
}
#: 插件里「决定模式」的那两个函数/常量（面板要能指到源码位置）。
MODE_SOURCES = {
    "goal_aligned": "plugins/advisor/goal.py::Goal.prompt_block（source=kanban|user|first_request 分支）",
    "conservative": "plugins/advisor/goal.py::Goal.prompt_block（source=unknown 分支）",
    "mode_setting": "plugins/advisor/audit.py::SETTINGS_VALIDATORS['mode']（auto / target / conservative）",
    "mode_resolution": "plugins/advisor/goal.py::resolve_mode（设置优先，其次按目标推导）",
    "prompt": "plugins/advisor/advisor_prompt.py::ADVISOR_SYSTEM_PROMPT",
    "enforcement": "plugins/advisor/runtime.py::_required_elements",
}
#: 三态设置值：什么时候生效、按什么判定（面板照这个显示规则说明）。
MODE_FIELDS = (
    (SETTING_AUTO, "默认。按该 turn 解析出的目标推导",
     "有目标→目标对齐（按验收项判定）；无目标→保守（只报技术风险）"),
    (SETTING_TARGET, "手动选了「目标对齐」",
     "无论有无目标都用目标对齐 prompt；无目标时以 transcript 里该 turn 正在回答的请求为准"),
    (SETTING_CONSERVATIVE, "手动选了「保守」",
     "无论有无目标都用保守 prompt；目标只作为背景，不作为验收标杆"),
)


def _load_plugin_module(module_name: str, filename: str) -> Any:
    """按文件路径加载 advisor 的一个**纯模块**（不执行包 ``__init__``）。

    与 ``_load_by_path`` 同一手法：造一个只有 ``__path__`` 的空包当壳，再按路径 exec。
    已经加载过（插件自己在本进程里用过）就复用，避免出现两份状态。
    """
    cached = sys.modules.get(module_name)
    path = advisor_dir() / filename
    if cached is not None and str(getattr(cached, "__file__", "")).startswith(str(advisor_dir())):
        return cached
    if not path.is_file():
        raise AdvisorUnavailable("locate", "找不到 advisor 模块：" + str(path))
    if "advisor" not in sys.modules:
        shell = types.ModuleType("advisor")
        shell.__path__ = [str(advisor_dir())]  # type: ignore[attr-defined]
        sys.modules["advisor"] = shell
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AdvisorUnavailable("spec", "无法为 " + str(path) + " 建立 import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _prompt_facts(has_mode_setting: bool = False) -> Dict[str, Any]:
    """提示词与三个模式分支的**实测**事实（文件、指纹、分支首行）。"""
    prompt_module = _load_plugin_module("advisor.advisor_prompt", "advisor_prompt.py")
    goal_module = _load_plugin_module("advisor.goal", "goal.py")
    prompt = str(getattr(prompt_module, "ADVISOR_SYSTEM_PROMPT", ""))
    goal_class = getattr(goal_module, "Goal", None)
    unknown_block = ""
    known_block = ""
    forced_target_block = ""
    markers_map = {}
    if callable(goal_class):
        unknown_block = str(goal_class().prompt_block())
        # 造一个已知目标，只为拿到「另一条分支」的原文（不查库、不读盘）
        known_block = str(goal_class(source="kanban", title="（示例）", text="（示例目标）",
                                     acceptance="（示例验收项）", matched_by="task_id",
                                     task_id="t_00000000").prompt_block())
        # 强制目标对齐 + 无目标：这是 mode=target 才走得到的第三种分支
        forced_target_block = str(goal_class().prompt_block("target"))
        markers_map = dict(getattr(goal_module, "PROMPT_MARKER", {}) or {})
    markers = [name for name in
               ("<scope>", "<goal_alignment>", "<four_elements>", "<workflow>", "<communication>",
                "<critical>", "<severity>", "<your_authority>")
               if name in prompt]
    return {
        "prompt_file": str(advisor_dir() / "advisor_prompt.py"),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "prompt_chars": len(prompt),
        "prompt_sections": markers,
        "goal_file": str(advisor_dir() / "goal.py"),
        "unknown_branch_head": unknown_block.strip().splitlines()[0] if unknown_block.strip() else "",
        "unknown_branch_says_conservative": "CONSERVATIVE" in unknown_block,
        "known_branch_head": known_block.strip().splitlines()[0] if known_block.strip() else "",
        "known_branch_mentions_acceptance": "Acceptance criteria" in known_block,
        "forced_target_branch_head": (
            forced_target_block.strip().splitlines()[0] if forced_target_block.strip() else ""
        ),
        "forced_target_says_mode_target": "mode=target" in forced_target_block,
        "mode_markers": markers_map,
        "has_mode_setting": bool(has_mode_setting),
    }


def _mode_of_goal_source(source: str) -> str:
    """``goal_source`` → 该次评审用的模式。台账里 ``unknown`` 就是保守模式。"""
    value = (source or "").strip()
    if not value:
        return MODE_UNKNOWN
    if value == "unknown":
        return MODE_CONSERVATIVE
    return MODE_GOAL_ALIGNED


def _latest_mode(audit: Any) -> Dict[str, Any]:
    """所有 profile 里**最近一条建议**用的模式（+ 它评的是哪条建议）。"""
    best: Dict[str, Any] = {"mode": MODE_UNKNOWN, "advice_id": "", "goal_source": "",
                            "profile": "", "at": "", "targets": []}
    for profile, directory in profile_audit_dirs():
        try:
            events, _ = _read_jsonl(directory / LEDGER_FILE)
            supported = set(getattr(audit, "EVENTS", ()))
            events = [event for event in events if event.get("event") in supported]
            folded = audit.fold(events)
        except Exception:  # noqa: BLE001 — 一个 profile 读坏了不拖垮结论
            continue
        for item in folded.values():
            source, ref = _goal_of(item)
            at = str(item.get("last_seen_at") or "")
            if at >= best["at"]:
                targets = [ref] if ref else []
                if item.get("session_id"):
                    targets.append(str(item["session_id"])[:8])
                best = {"mode": _mode_of_goal_source(source), "advice_id": str(item.get("id") or ""),
                        "goal_source": source, "profile": profile, "at": at, "targets": targets}
    return best


# ---------------------------------------------------------------------------
# 评审失败的分类（「接口报错」要能读懂）
# ---------------------------------------------------------------------------
# 真实样本（``logs/agent.log``）：``Advisor review failed for turn ...: Error code: 504``。
# 504 是**模型/网关超时**，不是插件坏了、也不是路由问题。面板原来只把它当一行字，看不出
# 「偶发还是持续、该动什么」。这里按错误码归类，给出人话原因与建议，并统计发生时间与次数。
ERROR_KINDS = ("model_timeout", "rate_limit", "auth", "bad_request", "server_error",
               "network", "other")
ERROR_LABEL = {
    "model_timeout": "模型超时",
    "rate_limit": "被限流",
    "auth": "鉴权失败",
    "bad_request": "请求被拒",
    "server_error": "上游 5xx",
    "network": "网络不通",
    "other": "其它错误",
}
ERROR_READABLE = {
    "model_timeout": "评审模型超时（504 / request timed out）：模型在超时时间内没有返回。"
                     "这不是插件故障，也不是接口路由问题 —— 模型没回话。",
    "rate_limit": "评审模型被限流（429）：短时间内请求过多，等一会儿或换模型。",
    "auth": "评审模型鉴权失败（401/403）：provider 的 key 或权限不对。",
    "bad_request": "评审请求被拒（400/422）：模型或参数不被接受（看设置里的 model/provider）。",
    "server_error": "上游服务错误（5xx）：provider 侧故障，稍后重试或换 provider。",
    "network": "网络不通：连接/超时/DNS 失败，评审请求没有到达模型。",
    "other": "未归类的评审错误，原文见 message。",
}
ERROR_HINTS = {
    "model_timeout": ["换一个响应更快的评审模型（设置里改 model / provider）",
                      "缩短上下文：会话 history 越大，评审 prompt 越大，越容易超时",
                      "提高网关 / 客户端的超时时间"],
    "rate_limit": ["降低评审频率（min_interval_seconds）", "换 provider 或模型"],
    "auth": ["检查该 provider 的 key 与权限（本面板不读凭据）"],
    "bad_request": ["确认 model / provider 是真实存在的名字"],
    "server_error": ["稍后重试；持续出现就换 provider"],
    "network": ["检查网络 / 代理；确认 provider 可达"],
    "other": ["看 message 原文定位"],
}


def _classify_error(text: str) -> str:
    """错误原文 → 归类（按**错误码优先**，其次按措辞）。"""
    raw = (text or "").lower()
    if not raw:
        return "other"
    if "504" in raw or "timed out" in raw or "timeout" in raw:
        return "model_timeout"
    if "429" in raw or "rate limit" in raw or "too many requests" in raw:
        return "rate_limit"
    if "401" in raw or "403" in raw or "unauthorized" in raw or "api key" in raw:
        return "auth"
    if "400" in raw or "422" in raw or "invalid" in raw:
        return "bad_request"
    if any(code in raw for code in ("500", "502", "503")):
        return "server_error"
    if any(word in raw for word in ("connect", "connection", "dns", "ssl", "unreachable")):
        return "network"
    return "other"


def _error_facts(text: str) -> Dict[str, Any]:
    kind = _classify_error(text)
    return {"kind": kind, "label": ERROR_LABEL[kind], "readable": ERROR_READABLE[kind],
            "hints": list(ERROR_HINTS[kind])}


#: 日志旁证：``logs/agent.log`` 里插件自己写的评审失败行。
LOG_FILE = ("logs", "agent.log")
LOG_FAILURE_MARK = "Advisor review failed for turn "
LOG_SCAN_LINES = 40000


def _log_review_failures() -> Dict[str, Any]:
    """从 ``<home>/logs/agent.log`` 读插件写的评审失败行（**旁证，不是台账**）。

    为什么要有这一条：``reviews.jsonl`` 是后来才加的，历史 504 只留在日志里。面板若只读
    台账，那段真实失败就永远看不见。这里只做「找 + 归类 + 计数」，并把文件路径摊开写清。
    """
    path = _home().joinpath(*LOG_FILE)
    payload: Dict[str, Any] = {
        "path": str(path), "exists": False, "scanned_lines": 0, "truncated": False,
        "failures": [], "count": 0, "by_kind": {}, "first_at": "", "last_at": "",
    }
    try:
        if not path.is_file():
            return payload
        payload["exists"] = True
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        payload["error"] = _describe(exc)
        return payload
    if len(lines) > LOG_SCAN_LINES:
        payload["truncated"] = True
        lines = lines[-LOG_SCAN_LINES:]
    payload["scanned_lines"] = len(lines)
    for line in lines:
        index = line.find(LOG_FAILURE_MARK)
        if index < 0:
            continue
        stamp = line[:19] if len(line) > 19 else ""
        rest = line[index + len(LOG_FAILURE_MARK):]
        turn, _, error = rest.partition(": ")
        facts = _error_facts(error.strip())
        payload["failures"].append({
            "ts": stamp, "turn_id": turn.strip(), "error": error.strip(),
            "kind": facts["kind"], "label": facts["label"],
            "readable": facts["readable"], "hints": facts["hints"],
        })
        payload["by_kind"][facts["kind"]] = payload["by_kind"].get(facts["kind"], 0) + 1
    payload["count"] = len(payload["failures"])
    stamps = [item["ts"] for item in payload["failures"] if item["ts"]]
    payload["first_at"] = min(stamps) if stamps else ""
    payload["last_at"] = max(stamps) if stamps else ""
    return payload


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
def _latest_review_mode(directory: Path) -> Dict[str, Any]:
    """该 profile 最近一条带模式信息的评审记录（新→旧），没有就全空。

    ``mode_resolved`` 是本轮真正生效的模式（``runtime.py`` 写进台账）；老记录没有这个
    键，就按 ``goal_source`` 推（``unknown`` 就是保守模式，其余是目标对齐）。
    """
    rows, _error = _read_jsonl(directory / REVIEWS_FILE)
    for record in reversed(rows):
        resolved = str(record.get("mode_resolved") or "").strip()
        goal_source = str(record.get("goal_source") or "").strip()
        if not resolved and goal_source:
            resolved = MODE_CONSERVATIVE if goal_source == "unknown" else MODE_GOAL_ALIGNED
        if resolved in (MODE_GOAL_ALIGNED, MODE_CONSERVATIVE):
            return {
                "resolved": resolved,
                "goal_source": goal_source,
                "mode_setting": str(record.get("mode_setting") or ""),
                "marker": str(record.get("mode_prompt_marker") or ""),
                "prompt_sha256": str(record.get("mode_prompt_sha256") or ""),
                "review_id": str(record.get("review_id") or ""),
                "ts": str(record.get("ts") or ""),
            }
    return {"resolved": "", "goal_source": "", "mode_setting": "", "marker": "",
            "prompt_sha256": "", "review_id": "", "ts": ""}


def _mode_state(audit: Any, profile: str, directory: Path) -> Dict[str, Any]:
    """一个 agent 的三件事：**设置值**、**本轮生效值**、**来源层**。

    设置值是落盘真值（``settings.json``，没有该键就是 ``DEFAULT_SETTINGS`` 的 ``auto``）；
    生效值由插件自己的 ``goal.resolve_mode`` 决定：强制值直接用，``auto`` 则看该 agent
    最近一次评审实际走了哪个分支。两者不同正是 ``auto`` 存在的意义，面板必须都显示。
    """
    data = _settings_payload(audit, profile, directory)
    setting = str((data.get("settings") or {}).get("mode") or SETTING_AUTO)
    detail = next((row for row in data.get("values_detail") or []
                   if row.get("key") == "mode"), {})
    setting_source = str(detail.get("source") or "default")
    last = _latest_review_mode(directory)
    if setting in SETTING_TO_RESOLVED:
        resolved = SETTING_TO_RESOLVED[setting]
        resolved_source = "forced"
        note = ("手动设定为 " + setting + "：无论该 turn 有没有目标，都用"
                + MODE_LABEL[resolved] + "的 prompt")
    elif last["resolved"]:
        resolved = last["resolved"]
        resolved_source = "last_review"
        note = ("auto：最近一次评审（" + (last["review_id"] or "?") + "）的目标来源是 "
                + (last["goal_source"] or "?") + "，所以那次走的是"
                + MODE_LABEL[resolved] + "；下一条建议若解析出目标/没解析出，会跟着变")
    else:
        resolved = MODE_CONSERVATIVE
        resolved_source = "no_review"
        note = ("auto：这个 agent 还没有带模式的评审记录；无目标时按保守模式跑，"
                "解析出目标时按目标对齐跑")
    return {
        "profile": profile,
        "current_setting": setting,
        "current_setting_label": SETTING_MODE_LABEL.get(setting, setting),
        "setting_source": setting_source,
        "setting_path": str(data.get("path") or ""),
        "setting_on_disk": detail.get("on_disk", None),
        "setting_on_disk_present": bool(detail.get("on_disk_present")),
        "resolved_mode": resolved,
        "resolved_label": MODE_LABEL.get(resolved, resolved),
        "resolved_mode_source": resolved_source,
        "resolved_note": note,
        "last_review": last,
        # 同一份设置（model / provider / enabled / concern_default 也在这里），
        # 免得为了另四项再读一遍磁盘。
        "settings": dict(data.get("settings") or {}),
    }


@router.get("/advisor/modes")
def get_modes(agent: str = Query("", description="只看某个 agent（profile）")) -> Any:
    """评审模式（提示词模式）：三态设置值、本轮生效值、来源层，以及**为什么**。

    模式现在是**设置项**（``mode``：``auto`` / ``target`` / ``conservative``），按 agent
    独立存在 ``<profile>/advisor/audit/settings.json``，由 ``PUT /advisor/settings?agent=``
    写入、由 ``goal.py::resolve_mode`` 在每次评审时读出来决定 prompt 分支：

    * ``auto``（默认）—— 沿用改造前的行为：有目标→目标对齐，无目标→保守；
    * ``target`` —— 无论有无目标，都用目标对齐 prompt；
    * ``conservative`` —— 无论有无目标，都用保守 prompt。

    这里返回的是**证据**，不是承诺：``resolved_mode`` 的 ``last_review`` 分支来自该 agent
    台账里最近一次评审记录的 ``mode_resolved``（``runtime.py`` 写入，且带
    ``mode_marker_in_prompt`` 与 ``mode_prompt_sha256``），提示词分支的首行原文也一并贴出。
    """
    agent_filter = (agent or "").strip()
    try:
        audit = _load_audit()
    except AdvisorUnavailable as exc:
        return _unavailable(exc)
    writable = sorted(getattr(audit, "SETTINGS_VALIDATORS", {}).keys())
    try:
        facts = _prompt_facts("mode" in writable)
    except AdvisorUnavailable as exc:
        facts = {"error": exc.detail}
    except Exception as exc:  # noqa: BLE001 — 读不到提示词不该让整块消失
        facts = {"error": _describe(exc)}
    latest = _latest_mode(audit)
    profiles = known_profiles()
    if agent_filter and agent_filter not in profiles:
        return _fail("invalid_argument",
                     "未知 agent：" + agent_filter + "（已知：" + " / ".join(profiles) + "）",
                     extra={"known_agents": profiles})
    states: List[Dict[str, Any]] = []
    for profile in profiles:
        if agent_filter and profile != agent_filter:
            continue
        try:
            states.append(_mode_state(audit, profile, _profile_audit_dir(profile)))
        except Exception as exc:  # noqa: BLE001 — 一个 profile 读坏了不拖垮整块
            states.append({"profile": profile, "error": _describe(exc),
                           "current_setting": SETTING_AUTO, "setting_source": "default",
                           "resolved_mode": MODE_UNKNOWN, "resolved_mode_source": "error"})
    # 没点名 agent 时用「最近一条建议所在的 agent」，再退到 default：面板总得有个主语
    subject = next((row for row in states if row["profile"] == agent_filter), None) \
        if agent_filter else None
    if subject is None:
        preferred = latest.get("profile") or ""
        subject = next((row for row in states if row["profile"] == preferred), None) \
            or next((row for row in states if row["profile"] == "default"), None) \
            or (states[0] if states else {})
    settings_by_profile = [{
        "profile": row["profile"],
        "model": (row.get("settings") or {}).get("model", ""),
        "provider": (row.get("settings") or {}).get("provider", ""),
        "concern_default": (row.get("settings") or {}).get("concern_default", ""),
        "enabled": (row.get("settings") or {}).get("enabled"),
        "mode": row.get("current_setting", SETTING_AUTO),
        "mode_source": row.get("setting_source", "default"),
        "resolved_mode": row.get("resolved_mode", MODE_UNKNOWN),
        "error": row.get("error", ""),
    } for row in states]
    modes = [{
        "id": setting,
        "value": setting,
        "label": SETTING_MODE_LABEL[setting],
        "when": when,
        "criterion": criterion,
        "source": MODE_SOURCES["mode_setting"],
        "resolved_mode": SETTING_TO_RESOLVED.get(setting, ""),
        "resolved_label": MODE_LABEL.get(SETTING_TO_RESOLVED.get(setting, ""), "按目标决定"),
        "is_current": setting == subject.get("current_setting"),
    } for setting, when, criterion in MODE_FIELDS]
    return {"ok": True, "data": {
        "switchable": True,
        "switch_blocked_reason": "",
        "mode_key_in_settings": "mode" in writable,
        "writable_settings": writable,
        "settings_endpoint": "/advisor/settings?agent=",
        "agent": subject.get("profile", agent_filter),
        # ---- 该 agent 的三件事（面板照这三个显示，缺一个就是猜）----
        "current_setting": subject.get("current_setting", SETTING_AUTO),
        "current_setting_label": SETTING_MODE_LABEL.get(
            subject.get("current_setting", SETTING_AUTO), subject.get("current_setting", "")),
        "setting_source": subject.get("setting_source", "default"),
        "setting_path": subject.get("setting_path", ""),
        "setting_on_disk": subject.get("setting_on_disk", None),
        "resolved_mode": subject.get("resolved_mode", MODE_UNKNOWN),
        "resolved_label": MODE_LABEL.get(subject.get("resolved_mode", MODE_UNKNOWN),
                                         subject.get("resolved_mode", MODE_UNKNOWN)),
        "resolved_mode_source": subject.get("resolved_mode_source", ""),
        "resolved_note": subject.get("resolved_note", ""),
        "last_review": subject.get("last_review", {}),
        # ---- 最近一条建议（跨 profile）用的是什么：老字段，保留 ----
        "current": latest,
        "current_label": MODE_LABEL.get(latest["mode"], latest["mode"]),
        "modes": modes,
        "setting_modes": list(SETTING_MODES),
        "resolved_modes": [
            {"id": mode_id, "label": MODE_LABEL[mode_id],
             "marker": str((facts.get("mode_markers") or {}).get(mode_id, "")),
             "source": MODE_SOURCES[mode_id]}
            for mode_id in (MODE_GOAL_ALIGNED, MODE_CONSERVATIVE)
        ],
        "extra_blocks": [
            {"id": "attention", "label": "额外注意力段（不是模式）",
             "when": "cwd 下有 WATCHDOG.md 且非空",
             "source": "plugins/advisor/runtime.py::_build_review_prompt"},
        ],
        "prompt": facts,
        "sources": MODE_SOURCES,
        "settings": settings_by_profile,
        "agents": states,
        "profiles": profiles,
    }, "notes": [
        "模式是**按 agent 的设置项**（mode），落盘在 "
        + str(subject.get("setting_path") or "该 agent 的 advisor/audit/settings.json")
        + "：改它用 PUT /advisor/settings?agent=<profile>，只动那一个 agent 的文件。",
        "设置值 " + str(subject.get("current_setting", "")) + "（来源 "
        + str(subject.get("setting_source", "")) + "）→ 本轮生效值 "
        + str(subject.get("resolved_mode", "")) + "（来源 "
        + str(subject.get("resolved_mode_source", "")) + "）。"
        + str(subject.get("resolved_note") or ""),
        "最近一条建议（" + (latest["advice_id"] or "无") + "）的目标来源是 "
        + (latest["goal_source"] or "无") + "，那次用的是「"
        + MODE_LABEL.get(latest["mode"], latest["mode"]) + "」。",
        "生效值不是承诺：每次评审都会把 mode_setting / mode_resolved / "
        "mode_prompt_sha256 / mode_marker_in_prompt 写进 reviews.jsonl，"
        "「到底发了哪段 prompt」可以从台账逐条核对。",
    ]}


@router.get("/advisor")
def get_self_check() -> Any:
    """自检：模块路径 / 各 profile 台账 / 事件行数 / 折叠后的建议数 / 路由清单。"""
    try:
        audit = _load_audit()
    except AdvisorUnavailable as exc:
        return _unavailable(exc)
    try:
        agents = _agents(audit)
    except Exception as exc:  # noqa: BLE001
        return _fail("upstream_error", "读取台账失败：" + _describe(exc), status_code=500)
    ledger = _main_audit_dir() / LEDGER_FILE
    return {"ok": True, "data": {
        "module": AUDIT_MODULE_NAME,
        "module_file": getattr(audit, "__file__", ""),
        "ledger": str(ledger), "ledger_exists": ledger.is_file(),
        "events": sum(found["events"] for found in agents),
        "advice_total": sum(found["advice_total"] for found in agents),
        "statuses": list(getattr(audit, "STATUSES", ())),
        "events_supported": list(getattr(audit, "EVENTS", ())),
        "review_outcomes_supported": list(getattr(audit, "REVIEW_OUTCOMES", ())),
        "readonly_settings": list(getattr(audit, "READONLY_SETTINGS", ())),
        "forced_settings": dict(getattr(audit, "FORCED_SETTINGS", {})),
        "writable_settings": sorted(getattr(audit, "SETTINGS_VALIDATORS", {}).keys()),
        "agents": [_agent_brief(found) for found in agents],
        "known_agents": known_agents(),
        "routes": ["GET /advisor", "GET /advisor/advice", "GET /advisor/advice/{id}",
                   "GET /advisor/reviews", "POST /advisor/decision", "GET /advisor/summary",
                   "GET /advisor/coverage", "GET /advisor/settings", "PUT /advisor/settings",
                   "GET /advisor/modes"],
    }, "notes": [_state_note(found) for found in agents]}


@router.get("/advisor/coverage")
def get_coverage() -> Any:
    """覆盖面矩阵：每个 profile 一行（装没装 / 启用没启用 / 台账 / sessions / 投递 / 状态）。

    **全部只读**：只 stat/read 文件与 config.yaml，不写、不改任何 profile 的配置。

    台账模块坏了也照样返回矩阵（此时按「行里有 event 字段」计数）：覆盖面存在的意义
    就是解释「为什么没数据」，它自己不能因为同一个原因变成 503。
    """
    audit: Any = None
    notes: List[str] = []
    try:
        audit = _load_audit()
    except AdvisorUnavailable as exc:
        notes.append("advisor 台账模块不可用（" + exc.detail + "）：本次台账事件数按原始行数统计，"
                     "折叠后的建议条数不可用")
    profiles = _profile_ids(_home())
    rows: List[Dict[str, Any]] = []
    for profile in profiles:
        try:
            rows.append(_coverage_row(profile, audit))
        except Exception as exc:  # noqa: BLE001 — 一个 profile 读坏了不该拖垮整张矩阵
            rows.append({"profile": profile, "status": "unknown",
                         "error": _describe(exc),
                         "diagnosis": profile + "：这一行读失败（" + _describe(exc)
                         + "），其它 profile 不受影响"})
    counts = {name: 0 for name in COVERAGE_STATUSES}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"ok": True, "data": {
        "profiles": rows,
        "status_counts": counts,
        "statuses": list(COVERAGE_STATUSES),
        "total": len(rows),
        "known_agents": known_agents(),
        "home": str(_home()),
        "injection_key": ".".join(INJECTION_KEY_PATH),
        "read_only": True,
    }, "notes": _coverage_notes(rows) + notes}


@router.get("/advisor/reviews")
def get_reviews(
    agent: str = Query("", description="只看某个 agent（profile），空=全部聚合"),
    limit: int = Query(50, description="最多返回多少条，钳到 1..200"),
) -> Any:
    """评审流水：每次评审一行（有建议 / 无建议 / 出错 / 跳过原因 + 模型与 token）。

    这是「评审到底跑没跑」的直接证据：台账空但有 ``no_advice`` 记录，说明评审
    跑过、没发现问题，不是故障。

    本轮补三件事（都是「接口报错要能读懂」缺的那一半）：

    * 每条 ``outcome=error`` 的记录带上归类（``error_kind`` / ``error_label``）与人话原因
      （``error_readable``）+ 建议（``error_hints``）——「Error code: 504」不再是一行天书；
    * 顶层 ``errors`` 汇总：次数、按类分布、首次/最近发生时间，用来判断偶发还是持续；
    * ``in_flight``：只写了 ``started`` 没有终态的评审（进程先退出），单列出来，
      不许被当成「无建议」；
    * ``log_evidence``：``reviews.jsonl`` 之前的历史失败只留在 ``logs/agent.log`` 里，
      这里作为**旁证**读出来并标明路径，不冒充台账。
    """
    page_limit = max(1, min(ADVICE_MAX_LIMIT, int(limit)))
    agent_filter = (agent or "").strip()
    try:
        audit = _load_audit()
    except AdvisorUnavailable as exc:
        return _unavailable(exc)
    try:
        agents = _agents(audit, agent_filter)
    except Exception as exc:  # noqa: BLE001
        return _fail("upstream_error", "读取评审流水失败：" + _describe(exc), status_code=500)
    if agent_filter and not agents:
        return _fail("invalid_argument",
                     "未知 agent：" + agent_filter + "（已知：" + " / ".join(known_agents()) + "）",
                     extra={"known_agents": known_agents()})
    records: List[Dict[str, Any]] = []
    for found in agents:
        for record in found["reviews"]:
            row = dict(record)
            row["profile_source"] = found["profile"]
            if row.get("outcome") == "error" or str(row.get("error") or "").strip():
                row.update(_error_facts(str(row.get("error") or row.get("skip_reason") or "")))
            records.append(row)
    records.sort(key=lambda record: (record.get("ts") or "", record.get("review_id") or ""),
                 reverse=True)
    total = len(records)
    page = records[:page_limit]

    # ---- 错误汇总（偶发还是持续）----
    error_rows = [row for row in records if row.get("kind")]
    by_kind: Dict[str, int] = {}
    for row in error_rows:
        by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
    stamps = [str(row.get("ts") or "") for row in error_rows if row.get("ts")]
    errors = {
        "count": len(error_rows),
        "by_kind": by_kind,
        "labels": {kind: ERROR_LABEL.get(kind, kind) for kind in by_kind},
        "first_at": min(stamps) if stamps else "",
        "last_at": max(stamps) if stamps else "",
        "items": [{"ts": row.get("ts", ""), "review_id": row.get("review_id", ""),
                   "profile": row.get("profile_source", ""), "kind": row["kind"],
                   "label": row["label"], "readable": row["readable"],
                   "hints": row["hints"], "error": row.get("error", "")}
                  for row in error_rows[:20]],
    }
    # ---- 飞行中（有开始、无终态）----
    in_flight: List[Dict[str, Any]] = []
    for found in agents:
        finished = {row.get("review_id") for row in found["reviews"]
                    if row.get("outcome") in ("advised", "no_advice", "error", "skipped")}
        for row in found["reviews"]:
            if row.get("outcome") == "started" and row.get("review_id") not in finished:
                in_flight.append({"ts": row.get("ts", ""), "review_id": row.get("review_id", ""),
                                  "profile": found["profile"], "model": row.get("model", ""),
                                  "provider": row.get("provider", ""),
                                  "turn_id": row.get("turn_id", "")})
    log_evidence = _log_review_failures()

    notes: List[str] = []
    for found in agents:
        notes.append(_state_note(found))
    if error_rows:
        notes.append("评审出错 " + str(len(error_rows)) + " 次，按类："
                     + "、".join(kind + " " + str(count) for kind, count in by_kind.items())
                     + "；首次 " + (errors["first_at"] or "?") + "，最近 " + (errors["last_at"] or "?"))
    if in_flight:
        notes.append("另有 " + str(len(in_flight)) + " 次评审只记了开始、没有终态（进程先退出，"
                     "不是「无建议」，也不是接口 404）：这些评审的结果永远不会出现，重跑才有。")
    if log_evidence["count"]:
        notes.append("日志旁证：" + log_evidence["path"] + " 里有 " + str(log_evidence["count"])
                     + " 条 advisor 评审失败（" + (log_evidence["first_at"] or "?") + " ~ "
                     + (log_evidence["last_at"] or "?") + "），归类见 log_evidence。")
    return {"ok": True, "data": {
        "agent": agent_filter or None,
        "total": total, "count": len(page), "limit": page_limit,
        "items": page,
        "errors": errors,
        "in_flight": in_flight,
        "in_flight_count": len(in_flight),
        "log_evidence": log_evidence,
        "error_kinds": list(ERROR_KINDS),
        "agents": [_agent_brief(found) for found in agents],
        "known_agents": known_agents(),
        "paths": [found["reviews_path"] for found in agents],
    }, "notes": notes}


@router.get("/advisor/advice")
def get_advice_list(
    status: str = Query("", description="pending|delivered|accepted|rejected|dropped，空=全部"),
    severity: str = Query("", description="nit|concern|blocker，空=全部"),
    q: str = Query("", description="关键词（正文 / id / 会话 / profile / 原因）"),
    agent: str = Query("", description="只看某个 agent（profile）的台账，空=全部 agent 聚合"),
    limit: int = Query(20, description="每页条数，钳到 1..200"),
    offset: int = Query(0, description="偏移，>=0"),
) -> Any:
    """建议列表：服务端分页，返回过滤后的 ``total``（不是整表条数）。

    台账按 profile 分文件，所以这里**聚合各 profile 的台账**并给每行标出来源
    （``profile_source``）；``agent`` 只挑一个。``agents`` 清单同时给出每个 agent
    的状态（无台账文件 / 有文件但无建议 / 有建议），面板不必靠猜。
    """
    page_limit, page_offset = max(1, min(ADVICE_MAX_LIMIT, int(limit))), max(0, int(offset))
    needle = (q or "").strip()
    if len(needle) > QUERY_MAX:
        return _fail("invalid_argument", "关键词太长（上限 " + str(QUERY_MAX) + " 字）：" + str(len(needle)))
    status_filter = (status or "").strip().lower()
    severity_filter = (severity or "").strip().lower()
    agent_filter = (agent or "").strip()
    if severity_filter and severity_filter not in SEVERITIES:
        return _fail("invalid_argument",
                     "severity 只能是 " + " / ".join(SEVERITIES) + "：收到 " + repr(severity_filter))
    if status_filter and status_filter not in STATUSES:
        return _fail("invalid_argument",
                     "status 只能是 " + " / ".join(STATUSES) + "：收到 " + repr(status_filter))
    try:
        audit = _load_audit()
    except AdvisorUnavailable as exc:
        return _unavailable(exc)
    try:
        agents = _agents(audit, agent_filter)
    except Exception as exc:  # noqa: BLE001
        return _fail("upstream_error", "读取台账失败：" + _describe(exc), status_code=500)
    if agent_filter and not agents:
        return _fail("invalid_argument",
                     "未知 agent：" + agent_filter + "（已知：" + " / ".join(known_agents()) + "）",
                     extra={"known_agents": known_agents()})

    items = [item for found in agents for item in found["items"]]
    if status_filter:
        items = [item for item in items if item["status"] == status_filter]
    if severity_filter:
        items = [item for item in items if item["severity"] == severity_filter]
    items.sort(key=lambda i: (i.get("last_seen_at") or "", i.get("id") or ""), reverse=True)
    matched = [item for item in items if _matches(item, needle)]
    total = len(matched)
    page = matched[page_offset:page_offset + page_limit]

    notes: List[str] = []
    for found in agents:
        notes.append(_state_note(found))
    # 结构自检汇总：不是「质量评分」，是「有多少条能看出它在评哪件事」。
    # 口径与插件闸门一致：只有四种识别形态算「点了名」，``quoted_only`` 单列（闸门会拒）。
    structures = [_note_structure(item.get("note", "")) for item in items]
    structure = {
        "total": len(structures),
        "with_target": sum(1 for one in structures if one["target_state"] == TARGET_NAMED),
        "missing_target": sum(1 for one in structures if one["target_state"] != TARGET_NAMED),
        "quoted_only": sum(1 for one in structures if one["target_state"] == TARGET_QUOTED_ONLY),
        "no_target": sum(1 for one in structures if one["target_state"] == TARGET_NONE),
        "with_labels": sum(1 for one in structures if one["labels"]),
        "complete": sum(1 for one in structures if one["complete"]),
    }
    if agents and not any(found["items"] for found in agents):
        notes.append("聚合范围内没有任何建议：这可能是「评审跑过、没发现问题」，也可能是"
                     "「该 agent 从未评审」——看上面每个 agent 的台账状态与评审流水条数即可区分")
    elif not matched:
        notes.append("当前筛选条件下没有建议（聚合范围内共有 " + str(len(items)) + " 条）")

    return {"ok": True, "data": {
        "status": status_filter or None, "severity": severity_filter or None, "q": needle,
        "agent": agent_filter or None,
        "total": total, "limit": page_limit, "offset": page_offset,
        "count": len(page), "items": [_row(item) for item in page],
        "structure": structure,
        "agents": [_agent_brief(found) for found in agents],
        "known_agents": known_agents(),
        "ledger": (agents[0]["ledger"] if agent_filter and agents else
                   str(_main_audit_dir() / LEDGER_FILE)),
        "ledgers": [found["ledger"] for found in agents],
    }, "notes": notes}


def _find_advice(audit: Any, advice_id_value: str,
                 agent_filter: str = "") -> Tuple[Optional[Dict[str, Any]], str, Optional[Path]]:
    """``(建议, 来源 profile, 台账目录)``：跨 profile 找一条建议。

    建议可能存在 worker 自己的台账里（面板原来只读主 home，于是这些行「查不到」）。
    这里按文件找，找到就带上来源 profile，写决定时也写回同一个文件。
    """
    for profile, directory in profile_audit_dirs():
        if agent_filter and profile != agent_filter:
            continue
        try:
            reader = getattr(audit, "get_advice_at", None)
            item = reader(directory, advice_id_value) if callable(reader) else (
                audit.get_advice(advice_id_value) if profile == DEFAULT_AGENT else None)
        except Exception:  # noqa: BLE001 — 一个 profile 读坏了不该拖垮其它 profile
            continue
        if item is not None:
            item["profile_source"] = profile
            return item, profile, directory
    return None, "", None


@router.get("/advisor/advice/{advice_id}")
def get_advice_detail(advice_id: str, agent: str = Query("", description="只在该 agent 的台账里找")) -> Any:
    """单条详情 + 完整事件时间线（raised / delivered / held / reconfirmed / decision）。"""
    target = (advice_id or "").strip()
    if not target:
        return _fail("invalid_argument", "缺少建议 id")
    try:
        audit = _load_audit()
    except AdvisorUnavailable as exc:
        return _unavailable(exc)
    try:
        item, profile, directory = _find_advice(audit, target, (agent or "").strip())
    except Exception as exc:  # noqa: BLE001
        return _fail("upstream_error", "读取建议详情失败：" + _describe(exc), status_code=500)
    if item is None:
        return _fail("not_found", "台账里没有这条建议：" + target, status_code=404,
                     extra={"id": target, "searched": [str(d) for _, d in profile_audit_dirs()]})
    row = _row(item)
    row["events"] = [
        {"event": event.get("event", ""), "ts": event.get("ts", ""),
         "actor": event.get("actor", ""), "source": event.get("source", ""),
         "severity": event.get("severity", ""), "decision": event.get("decision"),
         "reason": event.get("reason", ""), "profile": event.get("profile", "")}
        for event in (item.get("events") or [])
    ]
    return {"ok": True, "data": row,
            "notes": ["这条建议来自 " + profile + " 的台账：" + str(directory)]}


@router.post("/advisor/decision")
def post_decision(payload: Dict[str, Any] = Body(...)) -> Any:
    """记录采纳/拒绝（**只写决定，不执行建议内容**）→ 回读台账确认。

    ``rejected`` 必须带原因（空则 400）：拒绝理由是审核台存在的意义，不能省。
    """
    target = str(payload.get("id") or "").strip()
    decision = str(payload.get("decision") or "").strip().lower()
    reason = str(payload.get("reason") or "").strip()
    if not target:
        return _fail("invalid_argument", "缺少建议 id")
    if decision not in ("accepted", "rejected"):
        return _fail("invalid_argument", "decision 只能是 accepted 或 rejected：收到 " + repr(decision))
    if decision == "rejected" and not reason:
        return _fail("invalid_argument", "拒绝必须填写原因（reason 不能为空）",
                     extra={"id": target, "decision": decision})
    try:
        audit = _load_audit()
    except AdvisorUnavailable as exc:
        return _unavailable(exc)
    agent_filter = str(payload.get("agent") or "").strip()
    item, profile, directory = _find_advice(audit, target, agent_filter)
    if item is None:
        return _fail("not_found", "台账里没有这条建议：" + target, status_code=404,
                     extra={"id": target, "searched": [str(d) for _, d in profile_audit_dirs()]})
    try:
        writer = getattr(audit, "record_decision_at", None)
        if callable(writer):
            written = writer(directory, target, decision, reason=reason, actor="user")
        else:  # 老台账模块：只能写当前 profile 的台账
            written = audit.record_decision(target, decision, reason=reason, actor="user")
    except ValueError as exc:
        message = str(exc)
        if "no advice" in message:
            return _fail("not_found", "台账里没有这条建议：" + target, status_code=404,
                         extra={"id": target})
        return _fail("invalid_argument", message, extra={"id": target, "decision": decision})
    except Exception as exc:  # noqa: BLE001
        return _fail("upstream_error", "写决定失败：" + _describe(exc), status_code=500)

    # ---- 回读（写操作自己的返回值不当成功证据）----
    try:
        reader = getattr(audit, "get_advice_at", None)
        readback = reader(directory, target) if callable(reader) else audit.get_advice(target)
    except Exception as exc:  # noqa: BLE001
        return _fail("upstream_error", "决定已写入，但回读失败：" + _describe(exc),
                     status_code=500, extra={"id": target, "written": written})
    if readback is None:
        return _fail("upstream_error", "决定已写入，但回读不到这条建议：" + target,
                     status_code=500, extra={"id": target, "written": written})
    if readback.get("status") != decision or readback.get("decision") != decision:
        return _fail("upstream_error",
                     "写入返回成功，但回读的状态是 " + str(readback.get("status"))
                     + "（期望 " + decision + "）—— 以台账为准，请刷新确认",
                     status_code=500, extra={"id": target, "readback": _row(readback)})
    return {"ok": True, "data": {
        "id": target, "decision": decision, "reason": reason,
        "written_event": {k: written.get(k) for k in
                          ("event", "id", "ts", "decision", "reason", "actor", "source", "profile")},
        "readback": _row(readback),
        "agent": profile,
        "ledger": str(directory / LEDGER_FILE),
        "executed": False,
    }, "notes": ["只记录了决定，没有执行建议内容，也没有改动任何被评审的代码或配置",
                 "决定写回了建议所在的台账（" + profile + "）：" + str(directory / LEDGER_FILE)]}


@router.get("/advisor/summary")
def get_summary(
    agent: str = Query("", description="只看某个 agent（profile），空=全部聚合"),
) -> Any:
    """统计卡数据：总数 / 待处理 / 已采纳 / 已拒绝 / severity 分布 / 最近 7 天趋势。

    跨 profile 聚合（``agent`` 可只挑一个）：统计的是「所有 agent 的建议」，
    并单列每个 agent 的台账状态与评审流水，避免「default 有、worker 没有」被读成
    「worker 没评审」。
    """
    try:
        audit = _load_audit()
    except AdvisorUnavailable as exc:
        return _unavailable(exc)
    agent_filter = (agent or "").strip()
    try:
        agents = _agents(audit, agent_filter)
    except Exception as exc:  # noqa: BLE001
        return _fail("upstream_error", "读取统计失败：" + _describe(exc), status_code=500)
    if agent_filter and not agents:
        return _fail("invalid_argument",
                     "未知 agent：" + agent_filter + "（已知：" + " / ".join(known_agents()) + "）",
                     extra={"known_agents": known_agents()})

    counts = {name: 0 for name in STATUSES}
    by_severity = {name: 0 for name in SEVERITIES}
    events: List[Dict[str, Any]] = []
    review_totals = {name: 0 for name in ("advised", "no_advice", "error", "skipped")}
    review_in_flight = 0
    for found in agents:
        for item in found["items"]:
            status = item.get("status") or ""
            counts[status] = counts.get(status, 0) + 1
            severity = item.get("severity") or ""
            if severity in by_severity:
                by_severity[severity] += 1
        for name, value in (found["review_outcomes"] or {}).items():
            if name in review_totals:
                review_totals[name] = review_totals.get(name, 0) + int(value or 0)
        review_in_flight += int(found.get("review_in_flight") or 0)
        # 趋势按事件时间算，所以这里读事件而不是折叠后的行
        file_events, _ = _read_jsonl(Path(found["ledger"]))
        supported = set(getattr(audit, "EVENTS", ()))
        events.extend(event for event in file_events if event.get("event") in supported)

    notes = [_state_note(found) for found in agents]
    notes.append("评审流水合计：已评审有建议 " + str(review_totals["advised"])
                 + " / 已评审无建议 " + str(review_totals["no_advice"])
                 + " / 出错 " + str(review_totals["error"])
                 + " / 跳过 " + str(review_totals["skipped"]))
    return {"ok": True, "data": {
        "total": sum(counts.values()),
        "pending": counts["pending"],
        "delivered": counts["delivered"],
        "accepted": counts["accepted"],
        "rejected": counts["rejected"],
        "dropped": counts["dropped"],
        "by_severity": by_severity,
        "trend": _trend(events), "trend_days": TREND_DAYS,
        "events": len(events),
        "agent": agent_filter or None,
        "agents": [_agent_brief(found) for found in agents],
        "known_agents": known_agents(),
        "reviews": review_totals,
        "reviews_in_flight": review_in_flight,
        "ledger": (agents[0]["ledger"] if agent_filter and agents else
                   str(_main_audit_dir() / LEDGER_FILE)),
        "ledger_exists": any(found["ledger_exists"] for found in agents),
    }, "notes": notes}


# ---------------------------------------------------------------------------
# 设置：按 profile 读写（白名单校验仍然由 advisor.audit 自己做）
# ---------------------------------------------------------------------------
# 设置文件是**按 profile 分文件**的（``<profile home>/advisor/audit/settings.json``），
# 而 ``advisor.audit.get_settings()/update_settings()`` 只认「本进程的 home」。为了
# **复用它的白名单校验**（而不是在这里另写一套 validator），这里用 advisor 自己提供的
# ``ADVISOR_AUDIT_DIR`` 覆盖口（audit.py 的 docstring 明说它存在就是为了「一次性的、
# 操作别的 profile 台账的工具」）把读写指到目标 profile，调用完立刻还原。
#
# 风险与代价（写在代码里而不是留给后人猜）：
#   * 环境变量是进程级的，所以整段加 ``_SETTINGS_LOCK``，且只在调用的几毫秒内存在；
#   * 本进程里其它读台账的路径都走**显式目录**（``read_events_at`` / ``get_advice_at``
#     / ``record_decision_at``），不读这个环境变量，所以不会被这段窗口影响；
#   * 绝不为了「顺手」把环境变量永久改成某个 profile —— 那会让整个进程变成那个 profile。
_SETTINGS_LOCK = threading.RLock()


def known_profiles() -> List[str]:
    """``default`` + ``profiles/`` 下带 config.yaml 的目录（与 ``GET /agents`` 同一口径）。"""
    return _profile_ids(_home())


def _profile_audit_dir(profile: str) -> Path:
    return _profile_home(profile) / "advisor" / "audit"


@contextlib.contextmanager
def _audit_dir_scope(directory: Path) -> Iterator[None]:
    """临时把 ``ADVISOR_AUDIT_DIR`` 指向 *directory*（进出都还原，异常也还原）。"""
    previous = os.environ.get("ADVISOR_AUDIT_DIR")
    os.environ["ADVISOR_AUDIT_DIR"] = str(directory)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("ADVISOR_AUDIT_DIR", None)
        else:
            os.environ["ADVISOR_AUDIT_DIR"] = previous


def _resolve_profile(requested: str) -> Tuple[str, str]:
    """``(profile, 错误)``：空 = ``default``；不认识的 profile 返回错误文本。"""
    target = (requested or "").strip() or DEFAULT_AGENT
    known = known_profiles()
    if target not in known:
        return target, ("未知 profile：" + target + "（已知：" + " / ".join(known) + "）")
    return target, ""


#: ``/advisor/settings`` 曾经用 ``?profile=``。这个名字**不能用**：宿主的插件运行时门
#: （``hermes_cli/web_server_plugin_profiles.plugin_request_scope``）把任何插件请求上的
#: ``?profile=`` 当作「二级 profile 路由」指令，整条请求被切到那个 profile 的 config scope，
#: 而本插件的 API 宿主在那个 scope 下不存在 → 宿主直接 404 ``Plugin not found``（请求根本
#: 到不了本端点）。所以设置端点改用 ``?agent=``，并且**不做 ``?profile=`` 兼容**（兼容就等于没修）。
#:
#: **但有一个必须区分的例外（t_3e01204a）**：桌面端 ``pluginRest`` 会把
#: ``profileScoped()``（当前 agent 的 ambient profile）spread 进每个请求，electron main
#: 在「单 host 后端」形态下再用 ``pathWithProfileScope`` 追加 ``?profile=<ambient>``。
#: 于是前端明明只发了 ``?agent=default``，宿主实际收到的是
#: ``?agent=default&profile=default``；当 ambient 恰好等于 launch profile 时宿主不重绑、
#: 请求真的到达本端点，旧的「见 profile 就 400」把每一条正常请求都打成 400。
#: 因此：**``agent`` 存在时以 agent 为准、忽略桥附加的 profile**；
#: 「只带 profile 不带 agent」才是老误用，仍然 400。
_LEGACY_SCOPE_PARAM = "profile"


def _legacy_profile_param(request: Any) -> Optional[JSONResponse]:
    """``?profile=`` 出现**且没有 ``?agent=``** → 400 并说明改名；否则返回 None。

    判据只看「agent 在不在」，不看 profile 的值：
    * ``?agent=x``（可带桥附加的 ``?profile=...``）→ 放行，作用域由 agent 决定；
    * 只有 ``?profile=...`` → 400，提示改用 ``?agent=``（老误用/旧客户端仍能看懂）。
    """
    try:
        params = request.query_params
        present = _LEGACY_SCOPE_PARAM in params
        # 桥会给每个插件请求附加 ?profile=<ambient>；前端自己发的是 ?agent=。
        # 两者同时出现时，agent 才是本端点的作用域选择器。
        agent_present = bool(str(params.get("agent") or "").strip())
    except AttributeError:  # 没有 Request（旧调用方直接函数调用）：当作没带
        present = False
        agent_present = False
    if not present:
        return None
    if agent_present:
        # 宿主桥自动附加的 profile 不是调用方的意图：忽略它，别把正常请求打成 400。
        return None
    return _fail(
        "invalid_argument",
        "/advisor/settings 的查询参数已改名：请用 ?agent=<profile>。?profile= 是宿主 gateway 的"
        "「二级 profile 路由」选择器——对本插件的任何请求带 ?profile= 都会被宿主重绑到那个 profile 的"
        " config scope，本插件的 API 宿主在该 scope 下不存在，请求到不了本端点（表现为 404 Plugin not found）。"
        "这里不再接受、也不做兼容。",
        extra={"param": _LEGACY_SCOPE_PARAM, "replacement": "agent", "known_agents": known_profiles()},
    )


def _ignored_profile_param(request: Any) -> str:
    """返回被忽略的「桥附加 profile」值（有 agent 且带 profile 时），否则空串。

    只用于让响应**显式**说明「这个 ?profile= 是宿主桥加的，已按 agent 处理」，
    不改变作用域判定（判定在 :func:`_legacy_profile_param`）。
    """
    try:
        params = request.query_params
        if _LEGACY_SCOPE_PARAM in params and str(params.get("agent") or "").strip():
            return str(params.get(_LEGACY_SCOPE_PARAM) or "")
    except AttributeError:
        pass
    return ""


def _settings_payload(audit: Any, profile: str, directory: Path) -> Dict[str, Any]:
    """读一个 profile 的生效设置（``audit.get_settings()`` 在目标目录里跑）。

    ``values_detail`` 是「逐项读出当前值并标注来源层」的答案。**advisor 的设置只有两层**，
    这里不编造第三层：

    * ``settings_file`` —— 该 profile 的 ``advisor/audit/settings.json`` 里显式写了这个键；
    * ``default``       —— 文件里没有这个键，值来自 ``audit.DEFAULT_SETTINGS``（代码默认）。

    宿主层（``config.yaml`` 的 ``plugins.enabled`` / ``plugins.entries.advisor.*``）不是
    advisor 的设置项，但决定了「插件会不会被加载、建议投不投得出去」，所以单列在
    ``host_layers`` 里（只读，本面板不代改配置）。
    """
    with _SETTINGS_LOCK, _audit_dir_scope(directory):
        settings = audit.get_settings()
        path = Path(audit.settings_path())
    stored: Dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                stored = data
        except (OSError, ValueError):
            stored = {}
    writable = sorted(getattr(audit, "SETTINGS_VALIDATORS", {}).keys())
    defaults = dict(getattr(audit, "DEFAULT_SETTINGS", {}))
    readonly = list(getattr(audit, "READONLY_SETTINGS", ()))
    forced = dict(getattr(audit, "FORCED_SETTINGS", {}))
    detail: List[Dict[str, Any]] = []
    for key in writable:
        on_disk = stored.get(key, None)
        has_on_disk = key in stored
        detail.append({
            "key": key,
            "value": settings.get(key),
            "source": "settings_file" if has_on_disk else "default",
            "on_disk": on_disk,
            "on_disk_present": has_on_disk,
            "default": defaults.get(key),
            "writable": True,
            "readonly": False,
        })
    for key in readonly:
        detail.append({
            "key": key, "value": forced.get(key), "source": "forced_constant",
            "on_disk": None, "on_disk_present": False, "default": forced.get(key),
            "writable": False, "readonly": True,
        })
    config, config_path, config_error = _profile_config(profile)
    # ---- 「跟随全局」到底跟到哪：把该 profile 的 config.yaml 主模型读出来 ----
    # 空 provider/model = 继承，运行时 kwargs 里根本不带 model/provider，宿主按会话主模型
    # 调用（见 advisor/runtime.py）。既然界面写着「跟随默认」，就必须把**跟到的是什么**
    # 说出来，否则「跟随」两个字是空的：面板这里只读 config.yaml，不改它。
    model_block = config.get("model") if isinstance(config, dict) else None
    model_block = model_block if isinstance(model_block, dict) else {}
    inherited_provider = str(model_block.get("provider") or "").strip()
    inherited_model = str(model_block.get("default") or "").strip()
    configured_provider = settings.get("provider") or ""
    configured_model = settings.get("model") or ""
    if configured_provider or configured_model:
        pair_source = "settings_file"
    elif inherited_provider or inherited_model:
        pair_source = "config_yaml"
    else:
        pair_source = "none"
    inherited = {
        # 兜底目标：该 agent 的 config.yaml → model.provider / model.default
        "provider": inherited_provider,
        "model": inherited_model,
        "config_path": config_path,
        "config_error": config_error,
        "available": bool(inherited_provider or inherited_model),
        # 当前**实际会用**的那一组（配了就用配的，没配就用继承的）
        "effective_provider": configured_provider or inherited_provider,
        "effective_model": configured_model or inherited_model,
        "source": pair_source,
        "source_label": {
            "settings_file": "该 Agent 独立配置（settings.json）",
            "config_yaml": "跟随全局（该 Agent 的 config.yaml → model 段）",
            "none": "无可用默认（config.yaml 没有 model 段）",
        }[pair_source],
        "note": (
            "provider/model 都空 = 跟随全局：评审调用不带 model/provider，宿主按该 agent "
            "config.yaml 的主模型调用，所以这里显示的就是会生效的那一组。"
            "任何一项非空 = 该 Agent 独立配置，另一项仍留空时该项跟随该 provider 默认。"
        ),
    }
    enabled = settings.get("enabled") is not False
    return {
        "profile": profile,
        "settings": settings,
        "values": {key: settings.get(key) for key in writable},
        "values_detail": detail,
        "writable": writable,
        "readonly": readonly,
        "forced": forced,
        "path": str(path),
        "path_exists": path.is_file(),
        # 文件里实际存了什么（没有文件就是空）：让人能区分「生效值」与「落盘值」
        "stored": stored,
        "source": "file" if path.is_file() else "defaults",
        "defaults": defaults,
        "layers": ["default", "settings_file"],
        # 「跟随全局」跟到哪 + 当前实际会用的那一组（只读，来自该 agent 的 config.yaml）
        "model_pair": inherited,
        "host_layers": {
            "config_path": config_path,
            "config_error": config_error,
            "plugins_enabled": {"value": _config_enabled(config),
                                "key": "plugins.enabled（含 advisor？）", "writable": False},
            "allow_gateway_injection": {"value": _config_injection(config),
                                        "key": ".".join(INJECTION_KEY_PATH), "writable": False},
            "note": "宿主层只读：本面板不代改任何 profile 的 config.yaml。"
                    "plugins.enabled 决定插件会不会被加载；allow_gateway_injection 决定建议投不投得进会话。",
        },
        # enabled 是「评审总闸」：关着的时候界面必须一眼看出来，打开时必须有提示
        "enabled": enabled,
        "enabled_notice": (
            "当前 enabled=false：advisor 不会对任何 turn 做评审（这是上一张卡刻意保持的状态）。"
            "打开它会让每个 turn 结束后都调用一次评审模型，成本与延迟都会上来。"
            if not enabled else
            "当前 enabled=true：每个 turn 结束后都会调用一次评审模型。"
        ),
        "enabled_effect": (
            "打开后：每个 turn 结束都会调用评审模型；建议是否投进会话还取决于宿主层 "
            "allow_gateway_injection（当前 "
            + ("true" if _config_injection(config) else "不是 true") + "）。"
        ),
    }


def _model_pair_problem(profile: str, current: Dict[str, Any], changed: Dict[str, Any],
                        allow_unlisted: bool = False) -> Optional[str]:
    """``model`` / ``provider`` 两级联动的服务端闸门（t_4d654faf）。

    只有本次真的改了 ``model`` 或 ``provider`` 才校验，且按**改完之后**的组合判：
    单独改 model ⇒ 用当前的 provider；单独改 provider ⇒ 用当前的 model（于是"换 provider
    却留着旧模型"会被拒，这正是两级联动要的行为）；两个一起改 ⇒ 按新组合判。

    校验规则复用 ``api_models.model_pair_error``（与 ``scope=auxiliary`` 写入同一套），
    拿不到那个模块 / 读不到模型清单 ⇒ 放行（fail-open，绝不让面板因为宿主缺失整段不可用）。
    """
    if "model" not in changed and "provider" not in changed:
        return None
    provider = changed.get("provider", current.get("provider"))
    model = changed.get("model", current.get("model"))
    try:
        import api_models  # 同目录兄弟模块（plugin_api 已把本目录插进 sys.path）
    except Exception:  # noqa: BLE001 - 单独加载本模块时没有兄弟模块，fail-open
        return None
    return api_models.model_pair_error(profile, provider, model, allow_unlisted=allow_unlisted)


@router.get("/advisor/settings")
def get_policy_settings(
    request: Request,
    agent: str = Query("", description="看哪个 profile（agent）的设置（默认 default）"),
) -> Any:
    """某个 profile 的白名单设置 + 只读事实（``blocker_forced_delivery`` 只读出现）。

    按 profile 读：``default`` 读主 home，其余读 ``profiles/<name>/advisor/audit/settings.json``。
    选哪个 profile 用 ``?agent=``（**不是** ``?profile=``：那会被宿主当成二级 profile 路由选择器，
    见 :func:`_legacy_profile_param`）。
    """
    legacy = _legacy_profile_param(request)
    if legacy is not None:
        return legacy
    target, problem = _resolve_profile(agent)
    if problem:
        return _fail("invalid_argument", problem, extra={"known_agents": known_profiles()})
    try:
        audit = _load_audit()
    except AdvisorUnavailable as exc:
        return _unavailable(exc)
    try:
        data = _settings_payload(audit, target, _profile_audit_dir(target))
    except Exception as exc:  # noqa: BLE001
        return _fail("upstream_error", "读取设置失败：" + _describe(exc), status_code=500,
                     extra={"profile": target})
    data["profiles"] = known_profiles()
    ignored = _ignored_profile_param(request)
    notes = ["blocker_forced_delivery 是程序常量（单向阀门），不是设置项：这里只读展示，"
             "PUT 改它会 400",
             "设置按 profile 分文件：当前显示的是 " + target + " 的 "
             + data["path"] + "（" + data["source"] + "）"]
    if ignored:
        data["ignored_profile_param"] = ignored
        notes.insert(0, "宿主桥自动附加的 ?profile=" + ignored
                     + " 已忽略：作用域按 ?agent=" + target + " 解析（不是调用方传的）。")
    return {"ok": True, "data": data, "notes": notes}


@router.put("/advisor/settings")
def put_policy_settings(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    agent: str = Query("", description="写哪个 profile（agent）的设置（默认 default）"),
) -> Any:
    """写某个 profile 的白名单设置 → 回读（不信写操作自己的返回值）。

    只读字段（``blocker_forced_delivery``）想改就 400，**并明确说明「单向阀门不可关闭」**；
    原样回传 ``true`` 不算修改（前端会把整份设置回写），忽略即可。
    未知字段一律 400（白名单由 ``advisor.audit.update_settings`` 自己判）。
    选哪个 profile 用 ``?agent=``（**不是** ``?profile=``，理由同 GET）。
    """
    legacy = _legacy_profile_param(request)
    if legacy is not None:
        return legacy
    target, problem = _resolve_profile(agent)
    if problem:
        return _fail("invalid_argument", problem, extra={"known_agents": known_profiles()})
    if not isinstance(payload, dict):
        return _fail("invalid_argument", "设置补丁必须是对象", extra={"profile": target})
    try:
        audit = _load_audit()
    except AdvisorUnavailable as exc:
        return _unavailable(exc)
    # 没装插件的 profile 不写设置：写了也没人读，只会在磁盘上留下一个假开关
    if not _plugin_manifest(target)["installed"]:
        return _fail("invalid_argument",
                     target + " 没装 advisor 插件（" + str(_plugin_dir(target))
                     + " 不存在），写它的设置没有意义：先在那个 profile 里装插件",
                     extra={"profile": target})
    patch: Dict[str, Any] = dict(payload)
    notes: List[str] = []
    if "blocker_forced_delivery" in patch:
        value = patch.pop("blocker_forced_delivery")
        if value is not True:
            return _fail(
                "invalid_argument",
                "blocker_forced_delivery 是只读常量：单向阀门不可关闭（blocker 永远强制交付），"
                "没有、也不会有把 blocker 降级或关闭提示的入口",
                extra={"field": "blocker_forced_delivery", "value": value, "readonly": True,
                       "profile": target},
            )
        notes.append("blocker_forced_delivery 只读，已忽略本次回写的 true（值本来就等于它）")
    if not patch:
        return _fail("invalid_argument",
                     "补丁里没有可写字段（可写：" + " / ".join(
                         sorted(getattr(audit, "SETTINGS_VALIDATORS", {}).keys())) + "）",
                     extra={"profile": target})
    directory = _profile_audit_dir(target)
    # ---- 未改动的键不写（「零写请求」的兜底）----
    # 前端只发改动过的键；这里再做一次「与当前生效值相同就跳过」，这样即使调用方把整份
    # 设置回写，也不会产生无意义的写盘（settings.json 的 mtime 与内容都不动）。
    try:
        before = _settings_payload(audit, target, directory)
    except Exception as exc:  # noqa: BLE001
        return _fail("upstream_error", "读取当前设置失败（未写入任何东西）：" + _describe(exc),
                     status_code=500, extra={"profile": target})
    current = before["settings"] or {}
    changed = {key: value for key, value in patch.items() if current.get(key) != value}
    skipped = [key for key in patch if key not in changed]
    if not changed:
        return {"ok": True, "data": {
            **before,
            "profiles": known_profiles(),
            "applied": {}, "written_keys": [], "skipped_keys": skipped,
            "no_change": True,
        }, "notes": ["提交的键与当前生效值一致，没有写入任何东西（"
                     + "、".join(skipped) + "）；settings.json 未被改动。"]}
    # ---- provider + model 两级联动的服务端闸门（t_4d654faf）----
    # 「高级：手填模型名」用请求体里的 allow_unlisted_model=true 显式放行；它不是设置项，
    # 所以先摘掉再判白名单（否则会被 audit.update_settings 当未知键拒掉）。
    allow_unlisted = bool(changed.pop("allow_unlisted_model", False))
    pair_problem = _model_pair_problem(target, current, changed, allow_unlisted=allow_unlisted)
    if pair_problem:
        return _fail("invalid_argument", "设置未写入：" + pair_problem,
                     extra={"profile": target, "patch": changed,
                            "current": {"provider": current.get("provider"),
                                        "model": current.get("model")},
                            "allow_unlisted_model": allow_unlisted})
    if not changed:
        return {"ok": True, "data": {
            **before,
            "profiles": known_profiles(),
            "applied": {}, "written_keys": [], "skipped_keys": skipped,
            "no_change": True,
        }, "notes": ["只提交了 allow_unlisted_model（不是设置项）：没有写入任何东西。"]}
    try:
        with _SETTINGS_LOCK, _audit_dir_scope(directory):
            audit.update_settings(changed)
    except ValueError as exc:  # SettingsError 是 ValueError 的子类
        return _fail("invalid_argument", "设置未写入：" + str(exc),
                     extra={"patch": changed, "profile": target})
    except Exception as exc:  # noqa: BLE001
        return _fail("upstream_error", "写设置失败：" + _describe(exc), status_code=500,
                     extra={"profile": target})

    # ---- 回读（不信写操作的返回值；也把落盘原文一起返回，人才能核对）----
    try:
        data = _settings_payload(audit, target, directory)
    except Exception as exc:  # noqa: BLE001
        return _fail("upstream_error", "设置已写入，但回读失败：" + _describe(exc),
                     status_code=500, extra={"profile": target})
    settings = data["settings"]
    mismatched = [key for key, value in changed.items() if settings.get(key) != value]
    if mismatched:
        return _fail("upstream_error",
                     "写入返回成功，但回读不一致：" + "、".join(mismatched),
                     status_code=500,
                     extra={"patch": changed, "settings": settings, "profile": target})
    data["profiles"] = known_profiles()
    ignored = _ignored_profile_param(request)
    if ignored:
        data["ignored_profile_param"] = ignored
        notes.insert(0, "宿主桥自动附加的 ?profile=" + ignored
                     + " 已忽略：作用域按 ?agent=" + target + " 解析。")
    data["applied"] = {key: settings.get(key) for key in changed}
    data["written_keys"] = sorted(changed)
    data["skipped_keys"] = skipped
    data["no_change"] = False
    notes.append("已写入并回读确认：" + target + " 的 " + data["path"]
                 + "（只动了这个 profile 的文件）")
    notes.append("本次写入的键：" + "、".join(sorted(changed))
                 + ("；未改动因此没写的键：" + "、".join(skipped) if skipped else ""))
    return {"ok": True, "data": data, "notes": notes}


# ---------------------------------------------------------------------------
# 可操作的覆盖面（t_72e9a7fa）：安装 / 同步 / 启停插件 / 启停评审 / 启停注入 / 卸载
# ---------------------------------------------------------------------------
# 硬约束：
#   * 只改请求里点名的 profile；未点名的 profile 逐个比对 before/after，必须零变化。
#   * 默认零写入：``mode=preview`` 只回变更清单；执行必须 ``mode=apply`` 且 ``confirm=true``。
#   * 安装/同步按文件哈希回读（models.py / runtime.py / advisor_prompt.py / audit.py /
#     goal.py / plugin.yaml），与主 checkout（本插件自己的 advisor/ 树）逐文件相同才算成功。
#   * 启停插件：写 ``plugins.entries.advisor.enabled``（布尔）+ 同步 ``plugins.enabled``
#     名单 / ``plugins.disabled``，回读 config.yaml 真值。
#   * 启停评审：写 profile 的 ``advisor/audit/settings.json`` 的 ``enabled``，回读真值。
#   * 启停注入：**只**写 ``plugins.entries.advisor.allow_gateway_injection``（布尔），
#     不碰上面任何一个落点 —— 四件事互相独立，合并会让人以为「装了就全开」。
#   * 不碰真实台账 ``advice.jsonl``；不碰宿主 ``hermes_cli/``。
COVERAGE_WRITE_ACTIONS = ("install", "sync", "enable_plugin", "disable_plugin",
                          "enable_review", "disable_review",
                          "enable_injection", "disable_injection", "uninstall")
#: 安装/同步复制的运行期文件集：整套 plugins/advisor 去掉开发产物。
_PLUGIN_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "tests", "node_modules", ".mypy_cache"}
_PLUGIN_SKIP_SUFFIXES = (".pyc", ".pyo")
#: 安装/同步后必须逐文件哈希一致的证据文件（缺一不可）。
PLUGIN_HASH_EVIDENCE = ("models.py", "runtime.py", "advisor_prompt.py", "audit.py", "goal.py", "plugin.yaml")


def _source_plugin_dir() -> Path:
    """主 checkout 的 advisor 插件目录（本插件自己的树，不看 HERMES_HOME）。"""
    return advisor_dir()


def _runtime_plugin_files(source: Path) -> List[str]:
    """整套运行期文件（相对路径，posix 分隔）按字典序；排除 .git/__pycache__/tests 等。"""
    out: List[str] = []
    if not source.is_dir():
        return out
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(source)
        parts = rel.parts
        if any(part in _PLUGIN_SKIP_DIRS for part in parts):
            continue
        if path.suffix in _PLUGIN_SKIP_SUFFIXES:
            continue
        if "scripts" in parts[:-1] and path.suffix != ".py":
            continue
        out.append(rel.as_posix())
    return sorted(out)


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _copy_plugin(source: Path, dest: Path) -> Dict[str, Any]:
    """把主 checkout 的运行期文件集复制到 dest。返回 {files, count, created_dir, dest}。"""
    import shutil

    files = _runtime_plugin_files(source)
    created_dir = not dest.is_dir()
    dest.mkdir(parents=True, exist_ok=True)
    for rel in files:
        src = source / rel
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return {"files": files, "count": len(files), "created_dir": created_dir, "dest": str(dest)}


def _hash_compare(source: Path, dest: Path, names: Optional[Tuple[str, ...]] = None) -> Dict[str, Any]:
    """逐文件哈希比对（默认 PLUGIN_HASH_EVIDENCE）。返回 {files, all_match}。"""
    wanted = names or PLUGIN_HASH_EVIDENCE
    rows: List[Dict[str, Any]] = []
    for name in wanted:
        src = source / name
        dst = dest / name
        src_hash = _sha256_file(src) if src.is_file() else ""
        dst_hash = _sha256_file(dst) if dst.is_file() else ""
        rows.append({"file": name, "source": src_hash, "dest": dst_hash,
                     "match": bool(src_hash) and src_hash == dst_hash})
    all_match = bool(rows) and all(row["match"] for row in rows)
    return {"files": rows, "all_match": all_match}


def _load_yaml(path: Path) -> Tuple[Dict[str, Any], str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {}, ""
    except OSError as exc:
        return {}, _describe(exc)
    try:
        import yaml

        data = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001
        return {}, "YAML 解析失败：" + _describe(exc)
    return (data if isinstance(data, dict) else {}), ""


def _dump_yaml(path: Path, data: Dict[str, Any]) -> None:
    import yaml

    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _plugin_enable_plan(profile: str, enable: bool) -> Dict[str, Any]:
    """算出「启用/停用插件」会改的 config 字段（零写入）。"""
    config_path = _profile_home(profile) / "config.yaml"
    config, err = _load_yaml(config_path)
    if err:
        return {"allowed": False, "reason": err, "path": str(config_path), "changes": []}
    plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    enabled = list(plugins.get("enabled") or [])
    disabled = list(plugins.get("disabled") or [])
    entries = plugins.get("entries") if isinstance(plugins.get("entries"), dict) else {}
    advisor_entry = dict(entries.get("advisor") or {}) if isinstance(entries.get("advisor"), dict) else {}
    before_entry = advisor_entry.get("enabled")
    before_list = "advisor" in [str(n).strip() for n in enabled]
    changes: List[Dict[str, Any]] = [{"path": str(config_path),
                                      "field": "plugins.entries.advisor.enabled",
                                      "before": before_entry, "after": bool(enable)}]
    if enable:
        if not before_list:
            changes.append({"path": str(config_path), "field": "plugins.enabled",
                            "before": "（无 advisor）", "after": "加入 advisor"})
        if "advisor" in [str(n).strip() for n in disabled]:
            changes.append({"path": str(config_path), "field": "plugins.disabled",
                            "before": "含 advisor", "after": "移除 advisor"})
    elif before_list:
        changes.append({"path": str(config_path), "field": "plugins.enabled",
                        "before": "含 advisor", "after": "移除 advisor"})
    return {"allowed": True, "reason": "", "path": str(config_path), "changes": changes,
            "config_path": str(config_path)}


def _apply_plugin_enable(profile: str, enable: bool) -> Dict[str, Any]:
    config_path = _profile_home(profile) / "config.yaml"
    config, err = _load_yaml(config_path)
    if err:
        return {"ok": False, "error": err}
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
        config["plugins"] = plugins
    enabled = [str(n) for n in (plugins.get("enabled") or [])]
    disabled = [str(n) for n in (plugins.get("disabled") or [])]
    entries = plugins.get("entries")
    if not isinstance(entries, dict):
        entries = {}
        plugins["entries"] = entries
    advisor_entry = entries.get("advisor")
    if not isinstance(advisor_entry, dict):
        advisor_entry = {}
        entries["advisor"] = advisor_entry
    advisor_entry["enabled"] = bool(enable)
    leaf = [n for n in enabled if n != "advisor"]
    if enable:
        leaf.append("advisor")
    plugins["enabled"] = leaf
    plugins["disabled"] = [n for n in disabled if n != "advisor"]
    _dump_yaml(config_path, config)
    # ---- 回读落盘真值 ----
    reconfig, rerr = _load_yaml(config_path)
    re_entries = (reconfig.get("plugins") or {}).get("entries") if isinstance(reconfig.get("plugins"), dict) else {}
    re_advisor = re_entries.get("advisor") if isinstance(re_entries, dict) else None
    return {"ok": not rerr, "error": rerr, "enabled_in_config": _config_enabled(reconfig),
            "entries_enabled": re_advisor.get("enabled") if isinstance(re_advisor, dict) else None,
            "path": str(config_path)}


def _review_enable_plan(profile: str, enable: bool) -> Dict[str, Any]:
    path = _profile_home(profile) / "advisor" / "audit" / SETTINGS_FILE
    stored: Dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                stored = data
        except (OSError, ValueError):
            stored = {}
    before = stored.get("enabled", True)
    return {"allowed": True, "reason": "", "path": str(path),
            "changes": [{"path": str(path), "field": "enabled", "before": before,
                         "after": bool(enable)}]}


def _apply_review_enable(profile: str, enable: bool) -> Dict[str, Any]:
    path = _profile_home(profile) / "advisor" / "audit" / SETTINGS_FILE
    stored: Dict[str, Any] = {"schema_version": 1}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                stored = data
        except (OSError, ValueError):
            stored = {"schema_version": 1}
    stored["enabled"] = bool(enable)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
    back = json.loads(path.read_text(encoding="utf-8"))
    return {"ok": True, "enabled": back.get("enabled") is True, "path": str(path),
            "stored_enabled": back.get("enabled")}


def _injection_plan(profile: str, allow: bool) -> Dict[str, Any]:
    """算出「允许 / 不允许建议注入」会改的 config 字段（零写入）。

    **独立于「安装 / 插件启用 / 评审启用」的第四件事**：它只写
    ``plugins.entries.advisor.allow_gateway_injection``，不碰 ``plugins.enabled``、
    不碰 ``plugins.entries.advisor.enabled``、不碰 ``audit/settings.json``。
    把这几件事并成一个动作会让人以为「装了就全开」，所以这里刻意只改一个字段。
    """
    config_path = _profile_home(profile) / "config.yaml"
    config, err = _load_yaml(config_path)
    if err:
        return {"allowed": False, "reason": err, "path": str(config_path), "changes": []}
    before = _config_injection(config)
    field = ".".join(INJECTION_KEY_PATH)
    plan: Dict[str, Any] = {
        "allowed": True, "reason": "", "path": str(config_path), "config_path": str(config_path),
        "changes": [{"path": str(config_path), "field": field,
                     "before": before, "after": bool(allow)}],
    }
    # 依赖关系只提示、不阻止：评审没开时打开注入没有意义（但用户可能正在按顺序配）
    notices: List[str] = []
    if allow and not _config_enabled(config):
        notices.append("该 profile 的 advisor 插件在 config 里没有启用：现在打开注入不会有建议可投"
                       "（建议先把「插件启用」打开）")
    settings = _settings_file_state(profile)
    if allow and settings.get("enabled") is False:
        notices.append("该 profile 的评审（audit/settings.json → enabled）是关的："
                       "现在打开注入没有意义，评审不会产出建议")
    if allow:
        notices.append("打开后：评审出的建议会投进会话（会打断/提示）；关闭后：只记账不打扰。")
    else:
        notices.append("关闭后：建议只写进台账、不再投进会话。")
    if notices:
        plan["notices"] = notices
    return plan


def _injection_text_edit(text: str, allow: bool) -> Tuple[Optional[str], str]:
    """在 config.yaml 文本里**只改一行** ``allow_gateway_injection``，其余字节原样保留。

    为什么不直接 ``yaml.safe_dump`` 整份写回：那会重排缩进、丢注释、把用户的
    ``config.yaml`` 整体改一遍 —— 「只改一个字段」的说法就站不住了，而且用户
    手写的注释会消失。这里做定点文本编辑，失败就返回 ``(None, 原因)`` 让上层决定。

    返回 ``(新文本 | None, 说明)``。
    """
    lines = text.splitlines(keepends=True)

    def indent_of(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def is_blank_or_comment(line: str) -> bool:
        stripped = line.strip()
        return not stripped or stripped.startswith("#")

    def block_end(start: int, owner_indent: int) -> int:
        for j in range(start + 1, len(lines)):
            if is_blank_or_comment(lines[j]):
                continue
            if indent_of(lines[j]) <= owner_indent:
                return j
        return len(lines)

    def find_key(start: int, end: int, key: str, min_indent: int) -> int:
        pattern = re.compile(r"^(\s+)" + re.escape(key) + r"\s*:\s*(.*)$")
        for j in range(start, end):
            m = pattern.match(lines[j])
            if m and len(m.group(1)) >= min_indent:
                return j
        return -1

    plugins_idx = -1
    for i, line in enumerate(lines):
        if re.match(r"^plugins\s*:\s*$", line):
            plugins_idx = i
            break
    if plugins_idx < 0:
        return None, "没有顶层 plugins: 段（走整份 YAML 回写兜底）"
    plugins_end = block_end(plugins_idx, 0)

    entries_idx = find_key(plugins_idx + 1, plugins_end, "entries", 1)
    if entries_idx < 0:
        return None, "plugins 下没有 entries: 段（走整份 YAML 回写兜底）"
    entries_indent = indent_of(lines[entries_idx])
    entries_end = block_end(entries_idx, entries_indent)

    advisor_idx = find_key(entries_idx + 1, entries_end, "advisor", entries_indent + 1)
    if advisor_idx < 0:
        return None, "entries 下没有 advisor: 段（走整份 YAML 回写兜底）"
    if lines[advisor_idx].split(":", 1)[1].strip():
        return None, "advisor: 是行内写法（不是块），文本定点改不安全（走整份 YAML 回写兜底）"
    advisor_indent = indent_of(lines[advisor_idx])
    advisor_end = block_end(advisor_idx, advisor_indent)

    value = "true" if allow else "false"
    child_indent = " " * (advisor_indent + 2)
    new_line = child_indent + "allow_gateway_injection: " + value + "\n"

    key_idx = find_key(advisor_idx + 1, advisor_end, "allow_gateway_injection", advisor_indent + 1)
    if key_idx >= 0:
        original = lines[key_idx]
        eol = "\n" if original.endswith("\n") else ""
        lines[key_idx] = child_indent + "allow_gateway_injection: " + value + eol
        how = "替换已有行（第 %d 行）" % (key_idx + 1)
    else:
        # 插到 advisor 块最后一个非空非注释行之后，缩进与兄弟键对齐
        insert_at = advisor_idx + 1
        for j in range(advisor_idx + 1, advisor_end):
            if not is_blank_or_comment(lines[j]):
                insert_at = j + 1
        if not lines[advisor_idx].endswith("\n"):
            lines[advisor_idx] = lines[advisor_idx] + "\n"
        lines.insert(insert_at, new_line)
        how = "新增一行（插在第 %d 行之后）" % insert_at
    return "".join(lines), how


def _apply_injection(profile: str, allow: bool) -> Dict[str, Any]:
    """只改 ``plugins.entries.advisor.allow_gateway_injection``，然后回读真值。

    优先**定点文本编辑**（只动那一行，注释与其它键字节不变）；文本结构不适合
    定点改时降级为整份 YAML 回写，并在 ``edit_mode`` 里如实说明走了哪条路。
    """
    config_path = _profile_home(profile) / "config.yaml"
    config, err = _load_yaml(config_path)
    if err:
        return {"ok": False, "error": err}
    text_before = config_path.read_text(encoding="utf-8", errors="replace") if config_path.is_file() else ""
    new_text, how = _injection_text_edit(text_before, allow) if text_before else (None, "没有 config.yaml")

    edit_mode = "text"
    if new_text is not None:
        # 定点编辑必须过这一关：解析结果除目标键之外与改前完全一致
        try:
            import yaml as _yaml

            reparsed = _yaml.safe_load(new_text)
        except Exception as exc:  # noqa: BLE001
            new_text = None
            how = "定点编辑后 YAML 解析失败（%s）" % _describe(exc)
        else:
            expect = json.loads(json.dumps(config))  # 深拷贝
            node = expect
            for key in INJECTION_KEY_PATH[:-1]:
                if not isinstance(node.get(key), dict):
                    node[key] = {}
                node = node[key]
            node[INJECTION_KEY_PATH[-1]] = bool(allow)
            if reparsed != expect:
                new_text = None
                how = "定点编辑结果与预期不一致（拒绝写入）"
    if new_text is None:
        edit_mode = "full-yaml"
        plugins = config.get("plugins")
        if not isinstance(plugins, dict):
            plugins = {}
            config["plugins"] = plugins
        entries = plugins.get("entries")
        if not isinstance(entries, dict):
            entries = {}
            plugins["entries"] = entries
        advisor_entry = entries.get("advisor")
        if not isinstance(advisor_entry, dict):
            advisor_entry = {}
            entries["advisor"] = advisor_entry
        advisor_entry["allow_gateway_injection"] = bool(allow)
        new_text = None  # 交给 _dump_yaml
    if new_text is not None:
        config_path.write_text(new_text, encoding="utf-8")
    else:
        _dump_yaml(config_path, config)
    # ---- 回读落盘真值（不是回显入参）----
    reconfig, rerr = _load_yaml(config_path)
    re_plugins = reconfig.get("plugins") if isinstance(reconfig.get("plugins"), dict) else {}
    re_entries = re_plugins.get("entries") if isinstance(re_plugins.get("entries"), dict) else {}
    re_advisor = re_entries.get("advisor") if isinstance(re_entries.get("advisor"), dict) else {}
    text_after = config_path.read_text(encoding="utf-8", errors="replace")
    return {"ok": not rerr, "error": rerr,
            "injection_allowed": _config_injection(reconfig),
            "stored_value": re_advisor.get("allow_gateway_injection"),
            "entries_enabled": re_advisor.get("enabled"),
            "enabled_in_config": _config_enabled(reconfig),
            "edit_mode": edit_mode, "edit_how": how,
            "text_before_sha256": hashlib.sha256(text_before.encode("utf-8")).hexdigest(),
            "text_after_sha256": hashlib.sha256(text_after.encode("utf-8")).hexdigest(),
            "path": str(config_path)}


def _plan_one(action: str, profile: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """一个 profile + 一个动作：可用性、原因、变更清单（零写入，含依赖门）。"""
    installed = bool(row.get("plugin_installed"))
    plugin_on = bool(row.get("enabled_in_config"))
    review_on = bool(row.get("settings_enabled"))
    base = {"profile": profile, "action": action}
    if action == "install":
        if installed:
            return {**base, "allowed": False, "reason": "已安装（用「同步副本」刷新）", "changes": []}
        source = _source_plugin_dir()
        dest = _plugin_dir(profile)
        n = len(_runtime_plugin_files(source))
        return {**base, "allowed": True, "reason": "",
                "changes": [{"path": str(dest), "field": "（目录复制）",
                             "before": "不存在", "after": "复制 %d 个运行期文件" % n}],
                "source": str(source), "dest": str(dest), "file_count": n}
    if action == "sync":
        if not installed:
            return {**base, "allowed": False, "reason": "未安装（先安装）", "changes": []}
        source = _source_plugin_dir()
        dest = _plugin_dir(profile)
        return {**base, "allowed": True, "reason": "",
                "changes": [{"path": str(dest), "field": "（覆盖运行期文件）",
                             "before": "现有副本", "after": "与主 checkout 对齐"}],
                "source": str(source), "dest": str(dest)}
    if action in ("enable_plugin", "disable_plugin"):
        enable = action == "enable_plugin"
        if not installed:
            return {**base, "allowed": False, "reason": "未安装（先安装）", "changes": []}
        if enable and plugin_on:
            return {**base, "allowed": False, "reason": "插件已在 config 里启用", "changes": []}
        if (not enable) and (not plugin_on):
            return {**base, "allowed": False, "reason": "插件在 config 里本来就未启用", "changes": []}
        plan = _plugin_enable_plan(profile, enable)
        return {**base, **plan}
    if action in ("enable_review", "disable_review"):
        enable = action == "enable_review"
        if not installed:
            return {**base, "allowed": False, "reason": "未安装（先安装）", "changes": []}
        if not plugin_on:
            return {**base, "allowed": False,
                    "reason": "插件未启用（先启用插件，否则评审不会跑）", "changes": []}
        if enable and review_on:
            return {**base, "allowed": False, "reason": "评审本来就开着", "changes": []}
        if (not enable) and (not review_on):
            return {**base, "allowed": False, "reason": "评审本来就关着", "changes": []}
        plan = _review_enable_plan(profile, enable)
        if enable and not row.get("injection_allowed"):
            plan["notice"] = ("评审会跑，但 plugins.entries.advisor.allow_gateway_injection "
                              "不是 true，建议不会注入会话")
        return {**base, **plan}
    if action in ("enable_injection", "disable_injection"):
        allow = action == "enable_injection"
        if not installed:
            return {**base, "allowed": False, "reason": "未安装（先安装）", "changes": []}
        if allow and row.get("injection_allowed"):
            return {**base, "allowed": False, "reason": "本来就允许注入（allow_gateway_injection 已是 true）",
                    "changes": []}
        if (not allow) and not row.get("injection_allowed"):
            return {**base, "allowed": False, "reason": "本来就不允许注入（allow_gateway_injection 不是 true）",
                    "changes": []}
        # 与「安装 / 插件启用 / 评审启用」完全解耦：只改这一个字段，不做任何联动写入
        plan = _injection_plan(profile, allow)
        return {**base, **plan}
    if action == "uninstall":
        if not installed:
            return {**base, "allowed": False, "reason": "未安装", "changes": []}
        dest = _plugin_dir(profile)
        return {**base, "allowed": True, "reason": "危险：会删除该 profile 的插件目录",
                "changes": [{"path": str(dest), "field": "（删除目录）",
                             "before": "存在", "after": "删除"}],
                "dest": str(dest), "dangerous": True}
    return {**base, "allowed": False, "reason": "未知动作：" + action, "changes": []}


def _row_digest(row: Dict[str, Any]) -> Dict[str, Any]:
    return {"status": row.get("status"),
            "plugin_installed": row.get("plugin_installed"),
            "enabled_in_config": row.get("enabled_in_config"),
            "settings_enabled": row.get("settings_enabled"),
            "injection_allowed": row.get("injection_allowed")}


@router.post("/advisor/coverage/action")
def coverage_action(payload: Dict[str, Any] = Body(...)) -> Any:
    """对覆盖面矩阵执行可操作动作（安装 / 同步 / 启停插件 / 启停评审 / 启停注入 / 卸载）。

    四件事互相独立、逐列可操作，**不合并**：
      * 安装      —— ``profiles/<p>/plugins/advisor/`` 存不存在
      * 插件启用  —— ``config.yaml → plugins.entries.advisor.enabled``（+ ``plugins.enabled`` 名单）
      * 评审启用  —— ``audit/settings.json → enabled``
      * 允许注入  —— ``config.yaml → plugins.entries.advisor.allow_gateway_injection``
    合并会让人以为「装了就全开」，所以每个动作只写自己那一个落点。

    两段式，**默认零写入**：``mode=preview``（缺省）只回可用性 + 变更清单；
    ``mode=apply`` 且 ``confirm=true`` 才真正执行，然后回读（哈希 / config / settings）
    并重算 coverage，给出「改前 → 改后」。``profiles`` 必须显式点名，空列表拒绝。
    """
    action = str(payload.get("action") or "").strip()
    if action not in COVERAGE_WRITE_ACTIONS:
        return _fail("invalid_argument",
                     "未知动作：" + (action or "（空）") + "（可用：" + " / ".join(COVERAGE_WRITE_ACTIONS) + "）",
                     extra={"actions": list(COVERAGE_WRITE_ACTIONS)})
    raw_profiles = payload.get("profiles")
    if not isinstance(raw_profiles, list) or not raw_profiles:
        return _fail("invalid_argument", "profiles 必须是非空列表（逐个点名，不允许默认全选）")
    profiles: List[str] = []
    for name in raw_profiles:
        name = str(name or "").strip()
        if name and name not in profiles:
            profiles.append(name)
    if not profiles:
        return _fail("invalid_argument", "profiles 里没有有效的 profile 名")
    known = known_profiles()
    unknown = [name for name in profiles if name not in known]
    if unknown:
        return _fail("invalid_argument", "未知 profile：" + " / ".join(unknown),
                     extra={"known_profiles": known})
    mode = str(payload.get("mode") or "preview").strip().lower()
    if mode not in ("preview", "apply"):
        return _fail("invalid_argument", "mode 只能是 preview 或 apply：" + mode)
    confirm = payload.get("confirm") is True
    if action == "uninstall" and not confirm:
        return _fail("invalid_argument", "卸载是危险操作，必须 confirm=true",
                     extra={"dangerous": True, "action": action})

    audit: Any = None
    try:
        audit = _load_audit()
    except AdvisorUnavailable:
        audit = None
    before_rows: Dict[str, Dict[str, Any]] = {}
    for name in known:
        try:
            before_rows[name] = _coverage_row(name, audit)
        except Exception as exc:  # noqa: BLE001
            before_rows[name] = {"profile": name, "status": "unknown", "error": _describe(exc)}
    plans = [_plan_one(action, name, before_rows.get(name, {})) for name in profiles]
    selected = [p for p in plans if p.get("allowed")]
    blocked = [p for p in plans if not p.get("allowed")]
    untouched = [name for name in known if name not in profiles]

    if mode == "preview":
        return {"ok": True, "data": {
            "mode": "preview", "action": action, "zero_write": True,
            "plans": plans,
            "selected": [p["profile"] for p in selected],
            "blocked": [{"profile": p["profile"], "reason": p["reason"]} for p in blocked],
            "untouched": untouched,
        }, "notes": ["预览模式：没有写入任何文件。要执行请 mode=apply 且 confirm=true。"]}

    if not confirm:
        return _fail("invalid_argument", "执行写操作必须 confirm=true（二次确认）",
                     extra={"mode": mode})
    if not selected:
        return _fail("invalid_argument", "没有可执行的动作（全部被拦下）",
                     extra={"blocked": [{"profile": p["profile"], "reason": p["reason"]} for p in blocked]})

    applied: List[Dict[str, Any]] = []
    for plan in selected:
        name = plan["profile"]
        result: Dict[str, Any] = {"profile": name, "action": action, "changes": plan.get("changes", [])}
        try:
            if action in ("install", "sync"):
                copy_info = _copy_plugin(Path(plan["source"]), Path(plan["dest"]))
                hashes = _hash_compare(Path(plan["source"]), Path(plan["dest"]))
                result.update({"ok": hashes["all_match"], "copy": copy_info, "hash_match": hashes})
                if not hashes["all_match"]:
                    result["error"] = "安装/同步后哈希不一致（逐文件见 hash_match）"
            elif action in ("enable_plugin", "disable_plugin"):
                expect = action == "enable_plugin"
                res = _apply_plugin_enable(name, expect)
                result.update({"ok": bool(res.get("ok")) and res.get("enabled_in_config") == expect,
                               "readback": res})
                if not result["ok"]:
                    result["error"] = "config 回读与预期不一致"
            elif action in ("enable_review", "disable_review"):
                expect = action == "enable_review"
                res = _apply_review_enable(name, expect)
                result.update({"ok": bool(res.get("ok")) and res.get("enabled") == expect,
                               "readback": res})
                if not result["ok"]:
                    result["error"] = "settings.json 回读与预期不一致"
            elif action in ("enable_injection", "disable_injection"):
                expect = action == "enable_injection"
                res = _apply_injection(name, expect)
                result.update({"ok": bool(res.get("ok")) and res.get("injection_allowed") is expect,
                               "readback": res})
                if not result["ok"]:
                    result["error"] = "config 回读 allow_gateway_injection 与预期不一致"
            elif action == "uninstall":
                import shutil

                dest = Path(plan["dest"])
                if dest.is_dir():
                    shutil.rmtree(dest)
                result.update({"ok": not dest.is_dir(), "dest": str(dest)})
                if not result["ok"]:
                    result["error"] = "删除后目录仍存在"
        except Exception as exc:  # noqa: BLE001
            result.update({"ok": False, "error": _describe(exc)})
        applied.append(result)

    after_rows: Dict[str, Dict[str, Any]] = {}
    for name in known:
        try:
            after_rows[name] = _coverage_row(name, audit)
        except Exception as exc:  # noqa: BLE001
            after_rows[name] = {"profile": name, "status": "unknown", "error": _describe(exc)}

    transitions = [{"profile": result["profile"],
                    "before": _row_digest(before_rows.get(result["profile"], {})),
                    "after": _row_digest(after_rows.get(result["profile"], {}))}
                   for result in applied]
    untouched_changes = []
    for name in untouched:
        b, a = _row_digest(before_rows.get(name, {})), _row_digest(after_rows.get(name, {}))
        if b != a:
            untouched_changes.append({"profile": name, "before": b, "after": a})
    all_ok = all(result.get("ok") for result in applied)
    notes = ["已执行并回读：" + "、".join(result["profile"] for result in applied)]
    notes.append("未点名的 profile 有变化（不应发生）：" + str(untouched_changes)
                 if untouched_changes else "未点名的 profile 全部零变化。")
    return {"ok": all_ok, "data": {
        "mode": "apply", "action": action,
        "applied": applied,
        "transitions": transitions,
        "blocked": [{"profile": p["profile"], "reason": p["reason"]} for p in blocked],
        "untouched": untouched,
        "untouched_unchanged": not untouched_changes,
        "untouched_changes": untouched_changes,
    }, "notes": notes}

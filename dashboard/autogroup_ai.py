# -*- coding: utf-8 -*-
"""hermes-console · 轻量 AI 项目分类与“新项目候选”聚合引擎。

为什么单独做这一层：
* 现有自动归组主要依赖规则，规则覆盖不到的主题需要模型给出**建议**；
* 但模型建议不能直接落库，本模块只读 state/projects/assignments，返回可序列化预览，
  真正的写入留给人工确认后的存储层；
* 输入只取无归属或 suggested 的会话，applied 与 rejected 一律排除，避免重复覆盖既有归属；
* prompt 只放裁剪后的标题、cwd 和少量用户消息，绝不把整段对话塞给模型。
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_DASHBOARD_DIR = Path(__file__).resolve().parent
if str(_DASHBOARD_DIR) not in sys.path:
    # 单独 import 本模块（测试/探针）时也要能找到兄弟模块。
    sys.path.insert(0, str(_DASHBOARD_DIR))

import api_autogroup as base  # noqa: E402


class _LazyCore:
    """按需加载 ``hermes-auto-group/core.py``。

    ``autogroup_auto.py`` 是顶层 ``base._load_core()``，本文件刻意改成**延迟**：引擎会被存储层、
    测试、探针分别 import，顶层加载会把「core.py 路径不对」变成 import 期异常（连语法检查都过不去），
    而且 import 顺序不同可能拿到两份模块对象。延迟到第一次真正调用时加载，只有真要用库才失败。
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(base._load_core(), name)


core = _LazyCore()

PREVIEW_SCHEMA = "autogroup_ai.preview.v1"
#: 口径版本：AI 分类只处理「桌面端真实可见的普通对话」。批次里记下这个值，确认时用它拦旧批次。
SCOPE_RULE = "desktop-visible/v3"
KANBAN_SOURCE = "kanban"
DEFAULT_PROFILE = "default"
#: 宿主 ``_RESET_END_REASONS`` 的同一份名单（老库无 ``_reset_from`` 标记时用它认重置子行）。
RESET_END_REASONS = ("session_reset", "session_switch", "idle", "daily", "suspended",
                     "resume_pending_expired")
#: 桌面端 sidebar 真正不返回的来源（cron / 消息平台**各有分区、仍可见**，不能排除）
DESKTOP_EXCLUDED_SOURCES = ("acp", "kanban", "oneshot", "subagent", "tool")
DESKTOP_MIN_MESSAGES = 1
CONFIDENT_APPLY = 0.80        # >= 此值：确认后可 applied
CONFIDENT_SUGGEST = 0.50      # >= 此值且 < CONFIDENT_APPLY：确认后只 suggested
NEW_PROJECT_MIN_SESSIONS = 3  # 新项目候选所需的最少不同会话数
BRIEF_MAX_MESSAGES = 3        # 每条会话最多取几条用户消息
BRIEF_MAX_CHARS = 600         # 每条会话摘要总字符上限
TITLE_SAMPLES_PER_PROJECT = 3 # prompt 里每个项目给几条典型会话标题样例
#: t_ac537221：项目分组 AI **不再**借宿主 ``auxiliary.title_generation`` 槽位。
#: provider/model 由 ``autogroup_model``（``auto-group/ai_model.json``）显式给出，调用时
#: ``call_llm(task=None, provider=…, model=…)``；传 ``None`` 时宿主不会去读
#: ``auxiliary.<task>`` 的任何配置 —— 这就是「改项目分组的模型不再改到自动命名」的机制。
AUX_TASK = None
PURPOSE = "auto-group-ai"

_VERDICT_FIELDS = {"project_slug", "confidence", "reason", "new_project"}
_NEW_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
_USAGE_NOTE_REAL = "usage 来自 provider 响应（真实 token 数）"
_USAGE_NOTE_ESTIMATE = (
    "缺失真实 usage，按字符数估算：prompt_tokens=ceil(输入字符数/3)，"
    "completion_tokens=ceil(输出字符数/3)（中英混排经验口径，非计费值）"
)


def _now(now: Any = None) -> float:
    """把可选的 now（时间戳或 datetime）统一成 epoch 秒。"""
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        try:
            return float(now.timestamp())
        except (OverflowError, OSError, ValueError):
            return time.time()
    try:
        return float(now)
    except (TypeError, ValueError):
        return time.time()


def _new_batch_id(generated_at: float) -> str:
    """生成预览批次号；只用于人工确认时回传，不落任何库。"""
    try:
        stamp = datetime.fromtimestamp(generated_at).strftime("%Y%m%d-%H%M%S")
    except (OverflowError, OSError, ValueError):
        stamp = time.strftime("%Y%m%d-%H%M%S")
    return "ai-" + stamp + "-" + uuid.uuid4().hex[:6]


def tier_for(confidence: float) -> str:
    """按闭区间分层；只决定确认后的动作，不绕过人工确认。"""
    if confidence >= CONFIDENT_APPLY:
        return "apply"
    if confidence >= CONFIDENT_SUGGEST:
        return "suggest"
    return "undecided"


def _round_confidence(confidence: float) -> float:
    """按 SPEC 保留三位小数，但不要因四舍五入跨过 0.50 / 0.80 边界。"""
    rounded = round(float(confidence), 3)
    if tier_for(float(confidence)) != tier_for(rounded):
        return float(confidence)
    return rounded


def _raw_text(text: Any) -> str:
    if isinstance(text, str):
        return text
    if text is None:
        return ""
    try:
        return str(text)
    except Exception:
        return ""


def _project_slug_set(projects: List[Dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for project in projects or []:
        if isinstance(project, dict):
            slug = project.get("slug")
            if isinstance(slug, str) and slug:
                out.add(slug)
    return out


def parse_verdict(text: str, projects: List[Dict[str, Any]]) -> Dict[str, Any]:
    """严格解析模型输出；宁可让一行 undecided，也不宽容修正非法 JSON。"""
    raw = _raw_text(text)
    raw_excerpt = raw[:200]
    warnings: List[str] = []

    def fail(code: str, detail: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "verdict": None,
            "code": code,
            "detail": detail,
            "warnings": warnings,
            "raw_excerpt": raw_excerpt,
        }

    cleaned = raw.strip()
    if "```" in cleaned:
        cleaned = re.sub(r"```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()

    data: Any = None
    parsed_directly = False
    if cleaned:
        try:
            data = json.loads(cleaned)
            parsed_directly = True
        except (TypeError, ValueError):
            parsed_directly = False

    if not parsed_directly:
        match = re.search(r"\{.*\}", cleaned, re.S)
        if not match:
            return fail("invalid_json", "模型输出中没有可解析的 JSON 对象")
        try:
            data = json.loads(match.group(0))
        except (TypeError, ValueError):
            return fail("invalid_json", "模型输出的 JSON 无法解析")

    if not isinstance(data, dict):
        return fail("invalid_json", "模型输出不是 JSON 对象")

    unknown = [key for key in data.keys() if key not in _VERDICT_FIELDS]
    if unknown:
        warnings.append("ignored_unknown_fields: " + ",".join(sorted(unknown)))

    if "project_slug" not in data:
        return fail("invalid_schema", "缺少 project_slug 字段")
    project_slug = data.get("project_slug")
    if project_slug is not None and not isinstance(project_slug, str):
        return fail("invalid_schema", "project_slug 必须是字符串或 null")

    if "confidence" not in data:
        return fail("invalid_schema", "缺少 confidence 字段")
    confidence = data.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return fail("invalid_schema", "confidence 必须是数字且不能是布尔值")

    if "reason" not in data:
        return fail("invalid_schema", "缺少 reason 字段")
    reason = data.get("reason")
    if not isinstance(reason, str):
        return fail("invalid_schema", "reason 必须是字符串")

    new_project = data.get("new_project")
    if new_project is not None:
        if not isinstance(new_project, dict):
            return fail("invalid_schema", "new_project 必须是对象或 null")
        if not isinstance(new_project.get("slug"), str) or not isinstance(new_project.get("name"), str):
            return fail("invalid_schema", "new_project 必须包含字符串 slug 和 name")

    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        return fail("invalid_schema", "confidence 必须是数字")
    if not 0.0 <= confidence_value <= 1.0:
        return fail("confidence_out_of_range", "confidence 必须位于 0.0 到 1.0 的闭区间内")

    if project_slug == "":
        project_slug = None

    existing_slugs = _project_slug_set(projects)
    if project_slug is not None and project_slug not in existing_slugs:
        return fail("unknown_project", "模型给出的 project_slug 不在现有项目列表中")

    normalized_new: Optional[Dict[str, str]] = None
    if new_project is not None:
        new_slug = new_project["slug"]
        new_name = new_project["name"]
        if not _NEW_SLUG_RE.fullmatch(new_slug):
            return fail("invalid_new_project", "new_project.slug 格式不合法")
        if new_slug.lower() in {slug.lower() for slug in existing_slugs}:
            return fail("invalid_new_project", "new_project.slug 与现有项目撞名")
        normalized_new = {"slug": new_slug, "name": new_name}

    if project_slug is not None and normalized_new is not None:
        return fail("inconsistent", "project_slug 与 new_project 不能同时非空")
    if project_slug is None and normalized_new is None and confidence_value >= CONFIDENT_SUGGEST:
        return fail("inconsistent", "模型声称有把握但没有给出项目或新项目")
    if normalized_new is not None and confidence_value < CONFIDENT_SUGGEST:
        return fail("inconsistent", "低置信度不允许提出新项目")

    verdict = {
        "project_slug": project_slug,
        "confidence": _round_confidence(confidence_value),
        "reason": reason[:200],
        "new_project": normalized_new,
    }
    return {
        "ok": True,
        "verdict": verdict,
        "code": None,
        "detail": "模型输出校验通过",
        "warnings": warnings,
        "raw_excerpt": raw_excerpt,
    }


def _state_db_path(home: Any) -> Path:
    return Path(home) / "state.db"


def _empty_brief() -> Dict[str, Any]:
    return {"title": "", "cwd": None, "messages": [], "chars": 0}


def _clip_message(text: Any, limit: int = 200) -> str:
    """压缩空白并把单条消息裁到最多 limit 个字符（含省略号）。"""
    normalized = re.sub(r"\s+", " ", _raw_text(text)).strip()
    if len(normalized) <= limit:
        return normalized
    if limit <= 1:
        return "…"[:limit]
    return normalized[: limit - 1].rstrip() + "…"


def session_brief(
    home,
    session_id: str,
    *,
    max_messages=BRIEF_MAX_MESSAGES,
    max_chars=BRIEF_MAX_CHARS,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """读取一个会话的轻量摘要；state.db 不存在或读不到时安静返回空摘要。

    ``db_path``（t_bba9216d）：会话所在的那个 ``state.db``。``desktop_visible_pool`` 现在按
    agent 扫 ``profiles/<p>/state.db``，正文也必须从**同一个库**读 —— 固定读根库会让
    work/xiaomo 的会话正文全是空的（引擎随后判 ``brief_unavailable``）。
    不给就退回 ``<home>/state.db``（单库调用方的旧行为不变）。
    """
    result = _empty_brief()
    path = Path(db_path) if db_path else _state_db_path(home)
    if not path.exists():
        return result

    try:
        message_limit = int(max_messages)
    except (TypeError, ValueError):
        message_limit = BRIEF_MAX_MESSAGES
    try:
        char_limit = int(max_chars)
    except (TypeError, ValueError):
        char_limit = BRIEF_MAX_CHARS
    if message_limit <= 0 or char_limit <= 0:
        # 仍然尝试拿标题/cwd，只是不取正文。
        message_limit = 0

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = core.connect_ro(path)
        try:
            row = conn.execute(
                "SELECT title, cwd FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is not None:
                result["title"] = _raw_text(row["title"])
                result["cwd"] = row["cwd"]
        except sqlite3.Error:
            # sessions 表缺失/字段不齐不应影响正文读取。
            pass

        if message_limit <= 0:
            return result

        try:
            rows = conn.execute(
                "SELECT content FROM messages "
                "WHERE session_id = ? AND role = 'user' "
                "AND content IS NOT NULL AND content != '' ORDER BY id LIMIT ?",
                (session_id, message_limit),
            ).fetchall()
        except sqlite3.Error:
            return result

        kept: List[str] = []
        used = 0
        for row in rows:
            text = _clip_message(row["content"], 200)
            if not text:
                continue
            remaining = char_limit - used
            if remaining <= 0:
                break
            if len(text) > remaining:
                text = text[:remaining]
            if not text:
                break
            kept.append(text)
            used += len(text)
            if used >= char_limit:
                break
        result["messages"] = kept
        result["chars"] = used
        return result
    except (OSError, sqlite3.Error, ValueError):
        return result
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _sample_titles(samples: Any, slug: str) -> List[str]:
    """兼容 dict[slug, list] 与 list[dict] 两种样例输入。"""
    raw: Any = []
    if isinstance(samples, dict):
        raw = samples.get(slug, [])
    elif isinstance(samples, (list, tuple)):
        for item in samples:
            if not isinstance(item, dict):
                continue
            if item.get("slug") == slug or item.get("project_slug") == slug:
                if "titles" in item and isinstance(item["titles"], list):
                    raw.extend(item["titles"])
                elif isinstance(item.get("title"), str):
                    raw.append(item["title"])
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    out: List[str] = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("title")
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        if len(out) >= TITLE_SAMPLES_PER_PROJECT:
            break
    return out


def build_messages(projects, samples, brief) -> List[Dict[str, str]]:
    """构造轻量 prompt：项目清单 + 每项目少量标题样例 + 当前会话裁剪摘要。"""
    project_lines: List[str] = []
    valid_project_count = 0
    for project in projects or []:
        if not isinstance(project, dict):
            continue
        valid_project_count += 1
        slug = _raw_text(project.get("slug"))
        name = _raw_text(project.get("name"))
        primary_path = project.get("primary_path")
        path_text = _raw_text(primary_path) if primary_path is not None else ""
        titles = _sample_titles(samples, slug)
        line = f"- slug: {slug} | name: {name}"
        if path_text:
            line += f" | path: {path_text}"
        if titles:
            line += " | 典型会话标题: " + " / ".join(titles)
        project_lines.append(line)
    if not project_lines:
        project_lines.append("（当前没有可用项目）")

    brief = brief if isinstance(brief, dict) else {}
    title = _raw_text(brief.get("title"))
    cwd = brief.get("cwd")
    cwd_text = "null" if cwd is None else _raw_text(cwd)
    messages = brief.get("messages")
    if not isinstance(messages, (list, tuple)):
        messages = []

    system_content = (
        "你是 Hermes 的会话项目归类助手。你的任务是根据会话标题、cwd 和少量用户消息，"
        "判断会话最可能属于哪个现有项目，或者是否需要提出一个新项目。\n"
        "优先复用已有项目：只要现有项目的名称、路径或典型会话标题能合理覆盖会话主题，"
        "就必须选择现有项目，不要轻易新建。\n"
        "输出格式是硬性契约，字段名必须逐字一致：只允许 project_slug、confidence、reason、"
        "new_project 这四个键，多写任何别的键（如 slug、name、path、needs_clarification、"
        "project_name、session 等）都会被判为无效输出。\n"
        "只输出一个 JSON 对象，不要输出 Markdown、代码围栏或额外解释。\n"
        '字段含义：project_slug（现有项目 slug，或 null）、confidence（0 到 1 的数字）、'
        "reason（一句话中文理由，必填）、new_project（null，或只含 slug 与 name 两个键的对象）。\n"
        "project_slug 与 new_project 不能同时非空；没有把握时两者都为空且 confidence 小于 0.5。\n"
        "示例（归入现有项目）："
        '{"project_slug": "hermes-plugins", "confidence": 0.93, '
        '"reason": "标题与该项目典型会话一致，都在排查插件问题", "new_project": null}\n'
        "示例（无法判定）："
        '{"project_slug": null, "confidence": 0.2, '
        '"reason": "会话只有一句寒暄，看不出项目归属", "new_project": null}'
    )

    user_lines = [
        f"现有项目清单（共 {valid_project_count} 个）：",
        *project_lines,
        "",
        "当前会话：",
        "标题：" + title,
        "cwd：" + cwd_text,
        "用户消息（已按 session_brief 裁剪）：",
    ]
    if messages:
        for index, message in enumerate(messages, 1):
            user_lines.append(f"{index}. {_raw_text(message)}")
    else:
        user_lines.append("（无可用正文，请主要依据标题和 cwd 判断）")
    user_lines.extend([
        "",
        "只返回一个 JSON 对象，键必须恰好是 project_slug / confidence / reason / new_project：",
        '{"project_slug": "<现有项目 slug 或 null>", "confidence": <0 到 1 的数字>, '
        '"reason": "<一句话理由>", "new_project": null}',
        "不要输出 slug / name / path / project_name / needs_clarification 等其它键，"
        "不要代码围栏，不要解释文字。",
    ])
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "\n".join(user_lines)},
    ]


def default_caller() -> Optional[Callable[..., Dict[str, Any]]]:
    """延迟 import 宿主 auxiliary_client；任何 import 异常都让上层降级。"""
    try:
        from agent import auxiliary_client as ac
    except Exception:
        return None
    if not callable(getattr(ac, "call_llm", None)):
        return None

    def _call(
        messages: list[dict],
        *,
        task: str,
        purpose: str,
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> Dict[str, Any]:
        route_info: Dict[str, Any] = {}
        latency_info: Dict[str, Any] = {}
        resp = ac.call_llm(
            task=task,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            route_info=route_info,
            latency_info=latency_info,
        )
        text = ""
        extractor = getattr(ac, "extract_content_or_reasoning", None)
        if callable(extractor):
            text = _raw_text(extractor(resp))
        usage_obj = getattr(resp, "usage", None)
        usage: Optional[Dict[str, Any]] = None
        if usage_obj is not None:
            usage = {
                "prompt_tokens": _token_value(usage_obj, "prompt_tokens"),
                "completion_tokens": _token_value(usage_obj, "completion_tokens"),
                "total_tokens": _token_value(usage_obj, "total_tokens"),
            }
        elapsed = None
        if isinstance(latency_info, dict):
            elapsed = latency_info.get("summary_generation_ms")
        return {
            "text": text,
            "provider": route_info.get("provider") if isinstance(route_info, dict) else None,
            "model": route_info.get("model") if isinstance(route_info, dict) else None,
            "usage": usage,
            "elapsed_ms": elapsed,
        }

    return _call


def _token_value(usage_obj: Any, name: str) -> Optional[int]:
    if isinstance(usage_obj, dict):
        value = usage_obj.get(name)
    else:
        value = getattr(usage_obj, name, None)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _load_assignments(home: Path) -> tuple[Dict[str, Dict[str, Any]], set[str]]:
    """只读 assignments 表；不存在或表结构不完整时返回空映射。"""
    try:
        path = core.store_path(home)
    except Exception:
        return {}, set()
    if not Path(path).exists():
        return {}, set()
    conn: Optional[sqlite3.Connection] = None
    rows: List[Dict[str, Any]] = []
    try:
        conn = core.connect_ro(Path(path))
        rows = [dict(row) for row in conn.execute("SELECT * FROM assignments")]
    except (OSError, sqlite3.Error, ValueError):
        rows = []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    by_session: Dict[str, Dict[str, Any]] = {}
    applied: set[str] = set()
    for row in rows:
        session_id = row.get("session_id")
        if session_id is None:
            continue
        sid = str(session_id)
        if row.get("status") == "applied":
            applied.add(sid)
        previous = by_session.get(sid)
        if previous is None:
            by_session[sid] = row
        else:
            old_updated = previous.get("updated_at") or 0
            new_updated = row.get("updated_at") or 0
            try:
                if float(new_updated) >= float(old_updated):
                    by_session[sid] = row
            except (TypeError, ValueError):
                by_session[sid] = row
    return by_session, applied


def _read_rejected(home: Path) -> set[str]:
    try:
        return set(core.rejected_ids(home) or set())
    except Exception:
        return set()


def _session_columns_at(path: Path) -> set:
    """某个 ``state.db`` 的 ``sessions`` 列名（读不到就空集，调用方据此降级）。"""
    path = Path(path)
    if not path.is_file():
        return set()
    try:
        conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
    except sqlite3.Error:
        return set()
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def _session_columns(home: Path) -> set:
    return _session_columns_at(Path(home) / "state.db")


def _supported_profiles(home: Any) -> List[str]:
    """``["default", <profiles/* 名字>...]`` —— 用于判断 ``profile_name`` 列是否点名了真实 profile。"""
    names = [DEFAULT_PROFILE]
    try:
        children = sorted((Path(home) / "profiles").iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return names
    for child in children:
        name = child.name
        if name.startswith((".", "__")) or name in names:
            continue
        try:
            if child.is_dir() and (child / "config.yaml").is_file():
                names.append(name)
        except OSError:
            continue
    return names


def _row_profile(row: Dict[str, Any], container: str, known: List[str]) -> str:
    """一行属于哪个 agent。

    优先级：

    1. ``profile_name`` 列**点名了一个真实 profile** → 用它。这是宿主自己写的归属列，
       根库里遗留的 ``profile_name='xiaomo'`` 行因此仍归 xiaomo，不会因为落在根库就被算进 default；
    2. 否则 → 这行所在库的 profile（``profiles/<p>/state.db`` 的行一律归 ``p``）。

    第 2 条是这次修复的关键：profile 目录里的行经常不写 ``profile_name``（实测 work/xiaomo 都是空），
    以前按列判会把它们全部算成「别的 agent」。
    """
    named = str(row.get("profile_name") or "").strip()
    if named and named in known:
        return named
    return container


def session_profiles(home: Any) -> Dict[str, str]:
    """``session_id -> 归属 profile``（只读；扫全部 profile 库，含不可见的行）。

    用途：``excluded_applied`` / ``excluded_rejected`` 是**归属表**里的 id 集合，归属表本身不带
    profile，必须回 sessions 表查归属才能按 agent 计数（t_bffc584b）。扫全部库而不是只扫根库，
    否则 work/xiaomo 的 applied 一条都数不到（t_bba9216d）。
    """
    known = _supported_profiles(home)
    out: Dict[str, str] = {}
    for container, db_path in profile_scan_targets(home):
        cols = _session_columns_at(db_path)
        if not cols:
            continue
        has_name = "profile_name" in cols
        sql = "SELECT id, profile_name FROM sessions" if has_name else "SELECT id FROM sessions"
        try:
            conn = sqlite3.connect("file:" + str(db_path) + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                for row in conn.execute(sql):
                    sid = str(row["id"])
                    if sid in out:
                        continue
                    data = dict(row) if has_name else {"id": row["id"]}
                    out[sid] = _row_profile(data, container, known)
            finally:
                conn.close()
        except sqlite3.Error:
            continue
    return out


def profile_scan_targets(home: Any, agents: Optional[set] = None) -> List[tuple]:
    """``[(profile, state.db 路径)]`` —— **与 ``/overview/sessions?agent=X`` 完全同源**。

    宿主那条路由走 ``hermes_cli.web_server_sessions._open_session_db_for_profile(profile)``：

    * ``default`` → ``<home>/state.db``；
    * 其它 agent → ``<home>/profiles/<name>/state.db``。

    这里的发现规则照抄 ``api_agents._profile_ids``（``profiles/`` 下含 ``config.yaml`` 的目录），
    不 import api_agents（``autogroup_ai`` 已被 api_autogroup 反向依赖，避免循环）。

    ``agents`` 给了就只返回这些 agent 的库；**没建的 profile 直接缺席**（调用方据此报 0，
    而不是悄悄退回根库 —— 那正是本卡要修的 bug：根库不是 work/xiaomo 的数据源）。
    """
    home_path = Path(home)
    available: List[tuple] = []
    try:
        root_db = home_path / "state.db"
        available.append((DEFAULT_PROFILE, root_db))
    except OSError:  # pragma: no cover - Path 构造不会抛，留作防御
        pass
    profiles_root = home_path / "profiles"
    try:
        children = sorted(profiles_root.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        children = []
    for child in children:
        name = child.name
        if name.startswith((".", "__")) or name == DEFAULT_PROFILE:
            continue
        try:
            if child.is_dir() and (child / "config.yaml").is_file():
                available.append((name, child / "state.db"))
        except OSError:
            continue
    wanted = [str(a).strip() for a in (agents or set()) if str(a).strip()]
    if not wanted:
        return available
    by_name = {name: path for name, path in available}
    return [(name, by_name[name]) for name in dict.fromkeys(wanted) if name in by_name]


def _read_session_rows(db_path: Path, cols: set) -> List[Dict[str, Any]]:
    """只读拉一个库的 ``sessions`` 行（只取判定与展示需要的列）。"""
    wanted = ["id", "title", "cwd", "source", "message_count", "started_at", "last_activity_at"]
    for optional in ("archived", "hidden", "pinned", "profile_name",
                     "parent_session_id", "session_key", "ended_at", "end_reason", "model_config"):
        if optional in cols:
            wanted.append(optional)
    sql = "SELECT " + ", ".join(wanted) + " FROM sessions"
    try:
        conn = sqlite3.connect("file:" + str(db_path) + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in conn.execute(sql)]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _model_markers(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = row.get("model_config")
    if not isinstance(raw, str) or not raw.strip().startswith("{"):
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_listable_child(row: Dict[str, Any], by_id: Dict[str, Dict[str, Any]], cols: set) -> bool:
    """子行是否属于「桌面端会话列表会列出来的那种子行」。

    宿主 ``list_sessions_rich(include_children=False)`` 用的是 ``_LISTABLE_CHILD_SQL``（根行 +
    branch/reset 子行）+ ``_delegate_from IS NULL``：**压缩续接行与 subagent 运行不列**。
    这里按同一组标记复刻（只读字段判定，不猜 ID 前缀）：

    * 无 ``parent_session_id`` → 根行，列；
    * ``_delegate_from`` → subagent 运行，不列；
    * ``_branched_from`` / ``_reset_from`` → 分支 / 重置出的新会话，列；
    * 父行 ``end_reason`` 在重置原因集里且 ``session_key`` 与父一致 → 老库（无标记）的重置子行，列；
    * 父行 ``end_reason='branched'`` 且子行不早于父结束 → 老库的分支子行，列；
    * 其余（压缩续接 / 其它非用户可见子行）→ 不列。
    """
    if "parent_session_id" not in cols:
        return True
    parent_id = str(row.get("parent_session_id") or "").strip()
    if not parent_id:
        return True
    markers = _model_markers(row)
    if markers.get("_delegate_from"):
        return False
    if markers.get("_branched_from") or markers.get("_reset_from"):
        return True
    parent = by_id.get(parent_id) or {}
    session_key = str(row.get("session_key") or "")
    if (session_key and session_key == str(parent.get("session_key") or "")
            and str(parent.get("end_reason") or "") in RESET_END_REASONS):
        return True
    if str(parent.get("end_reason") or "") == "branched":
        try:
            if float(row.get("started_at") or 0) >= float(parent.get("ended_at") or 0):
                return True
        except (TypeError, ValueError):
            pass
    return False


def _row_rank(row: Dict[str, Any]) -> tuple:
    """跨库同 id 去重时的「保留谁」排序键：消息更多 → 更近活跃。"""
    try:
        messages = int(row.get("message_count") or 0)
    except (TypeError, ValueError):
        messages = 0
    try:
        active = float(row.get("last_activity_at") or row.get("started_at") or 0)
    except (TypeError, ValueError):
        active = 0.0
    return (messages, active)


def desktop_visible_pool(home: Path, agents: Optional[set] = None) -> Dict[str, Any]:
    """桌面端**真实可见**的会话池 —— 扫**该 agent 自己的** ``state.db``，不是根库。

    数据源与 ``/overview/sessions?agent=X``（``_open_session_db_for_profile``）**同源**
    （t_bba9216d：以前这里固定读 ``H/state.db``，而 work/xiaomo/worker 的会话住在
    ``profiles/<p>/state.db``，导致「按钮说 21 条、引擎说 0 条」）：

    * ``default``（以及 ``agents=None`` 时的根库）→ ``<home>/state.db``；
    * 其它 agent → ``<home>/profiles/<name>/state.db``；
    * ``agents=None`` → 全部库**并集**（按 session id 去重）。

    逐行可见性（与宿主 sidebar 同条件 ``include_hidden=False`` / ``include_pinned=True`` /
    ``min_messages=1``）：

    * ``source`` 不属于 ``acp`` / ``kanban`` / ``oneshot`` / ``subagent`` / ``tool``
      （``cron`` 与消息平台**各有分区、仍可见**，不能排除）；
    * ``hidden = 0``；
    * ``archived = 0``，但 ``pinned = 1`` 的已归档行仍算可见；
    * ``message_count >= 1``；
    * 是「会列出来的」子行（见 :func:`_is_listable_child`；压缩续接 / subagent 运行不列）。

    **归属**（``row["_profile"]``）：``profile_name`` 列点名了真实 profile 就用它（宿主自己写的归属，
    根库里遗留的 ``profile_name='xiaomo'`` 行仍归 xiaomo，不会因为落在根库被算进 default），
    否则用**这行所在库的 profile**（``profiles/<p>/state.db`` 的行一律归 ``p`` —— 实测这些库经常
    不写 ``profile_name``，只看列会把它们全算成「别的 agent」）。见 :func:`_row_profile`。

    ``agents`` 给了就只保留这些 profile 的会话，并把「本来会进池、但属于其它 agent」的行计进
    ``excluded_agent``（归属判定排在可见性判定之前，别的 agent 的行不许混进本 agent 的任何排除桶）。

    返回 ``{"sessions": [...], "counts": {...}, "profiles": [...]}``；每行额外带
    ``_profile``（归属）与 ``_db``（所在库路径，读正文用）。
    """
    counts = {"desktop_visible": 0, "excluded_kanban": 0, "excluded_hidden": 0,
              "excluded_source": 0, "excluded_archived": 0, "excluded_no_message": 0,
              "excluded_child": 0, "excluded_agent": 0, "deduped": 0}
    wanted_agents = {str(a).strip() for a in (agents or set()) if str(a).strip()}
    known_profiles = _supported_profiles(home)
    targets = profile_scan_targets(home, agents=wanted_agents or None)
    result = {"sessions": [], "counts": counts,
              "profiles": [{"profile": name, "db": str(path), "exists": Path(path).is_file(),
                            "scanned": False} for name, path in targets]}
    if not targets:
        return result

    kept: List[Dict[str, Any]] = []
    for profile, db_path in targets:
        info = next(item for item in result["profiles"] if item["profile"] == profile)
        if not Path(db_path).is_file():
            continue
        cols = _session_columns_at(db_path)
        rows = _read_session_rows(db_path, cols)
        info["scanned"] = True
        info["rows"] = len(rows)
        by_id = {str(row.get("id")): row for row in rows if row.get("id") is not None}
        for row in rows:
            row["_db"] = str(db_path)
            owner = _row_profile(row, profile, known_profiles)
            row["_profile"] = owner
            if not str(row.get("profile_name") or "").strip():
                # profile 目录里的行常常不写 profile_name：补成归属，供展示/日志用。
                row["profile_name"] = owner
            source = str(row.get("source") or "").strip().lower()
            try:
                messages = int(row.get("message_count") or 0)
            except (TypeError, ValueError):
                messages = 0
            hidden = int(row.get("hidden") or 0) if "hidden" in cols else 0
            pinned = int(row.get("pinned") or 0) if "pinned" in cols else 0
            archived = int(row.get("archived") or 0) if "archived" in cols else 0
            listable = _is_listable_child(row, by_id, cols)
            visible = (messages >= DESKTOP_MIN_MESSAGES
                       and source not in DESKTOP_EXCLUDED_SOURCES
                       and not hidden and not (archived and not pinned)
                       and listable)
            if wanted_agents and owner not in wanted_agents:
                # 别的 agent 的会话：只把「本来会进池」的计入 excluded_agent，
                # 绝不混进本 agent 的可见性排除数。
                if visible:
                    counts["excluded_agent"] += 1
                continue
            if messages < DESKTOP_MIN_MESSAGES:
                counts["excluded_no_message"] += 1
                continue
            if source in DESKTOP_EXCLUDED_SOURCES:
                counts["excluded_kanban" if source == KANBAN_SOURCE else "excluded_source"] += 1
                continue
            if hidden:
                counts["excluded_hidden"] += 1
                continue
            if archived and not pinned:
                counts["excluded_archived"] += 1
                continue
            if not listable:
                counts["excluded_child"] += 1
                continue
            kept.append(row)

    picked: Dict[str, Dict[str, Any]] = {}
    for row in kept:
        sid = str(row.get("id"))
        previous = picked.get(sid)
        if previous is None:
            picked[sid] = row
            continue
        counts["deduped"] += 1
        if _row_rank(row) > _row_rank(previous):
            picked[sid] = row
    sessions = list(picked.values())
    counts["desktop_visible"] = len(sessions)
    sessions.sort(key=lambda r: float(r.get("last_activity_at") or r.get("started_at") or 0),
                  reverse=True)
    result["sessions"] = sessions
    return result


def _candidate_sessions(
    home: Path,
    limit: int,
    assignments: Dict[str, Dict[str, Any]],
    applied_ids: set[str],
    rejected: set[str],
    offset: int = 0,
    skip_ids: Optional[set] = None,
    agents: Optional[set] = None,
) -> tuple[List[Dict[str, Any]], int, int, int, Dict[str, int]]:
    """读取候选会话并剔除 applied/rejected/其他状态；limit=0 表示不设上限。

    **口径（SCOPE_RULE）**：只处理**桌面端真实可见的普通对话** —— 候选池由
    :func:`desktop_visible_pool` 按 ``sessions`` 字段逐条判定（``source`` 排除
    acp/kanban/oneshot/subagent/tool；``hidden=0``；``archived`` 但 ``pinned`` 仍可见；
    ``message_count>=1``；压缩续接/subagent 子行不列）。看板任务会话在取池阶段就被挡掉，
    **不会产生任何模型调用**。

    数据源（t_bba9216d）：池来自**该 agent 自己的** ``state.db``（``profiles/<p>/state.db``），
    与 ``/overview/sessions?agent=X`` 同源；``agents=None`` 时取全部库的并集（按 id 去重）。

    ``offset`` 跳过前 N 条候选（分批续跑用）；``skip_ids`` 是调用方**已经处理过**的会话
    （UI 把上一批的 id 带回来，保证同一会话不会被处理两次 —— 不是「漏处理」）。

    返回 ``(本批候选, excluded_other, total_available, skipped, kanban_excluded)``：

    * ``total_available`` —— 本次口径下的候选**总数**（不受 limit/offset/skip 影响）。
      UI 的「已处理 N / 共 total_available」只用这一个数，不自己估算；
    * ``skipped`` —— 被 ``skip_ids`` 剔掉的数量（= 之前批次已处理）。
    """
    exclude_ids = set(applied_ids) | set(rejected)
    skip = {str(s) for s in (skip_ids or set()) if str(s).strip()}
    # agent 作用域（t_ac537221）：只在点名 profile 的会话里选，几个 agent 的数据不许串。
    # t_bffc584b：**在取池阶段就按 agent 收窄**（而不是取全局池再事后过滤），
    # 这样 visibility 里的 kanban/hidden/archived/no_message 各桶也自动变成该 agent 的口径。
    wanted_agents = {str(a) for a in (agents or set()) if str(a).strip()}
    # 候选池 = 桌面端真实可见池（source/hidden/archived+pinned/min_messages/子行 一起判）
    pool = desktop_visible_pool(home, agents=wanted_agents or None)
    rows = pool.get("sessions") or []
    visibility = dict(pool.get("counts") or {})

    candidates: List[Dict[str, Any]] = []
    seen: set[str] = set()
    excluded_other = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        raw_id = row.get("id")
        if raw_id is None:
            continue
        sid = str(raw_id)
        if sid in seen:
            continue
        seen.add(sid)
        # 兜底：即使 core 没按 include_kanban 过滤，这里也不让看板会话进候选
        if str(row.get("source") or "").strip().lower() == KANBAN_SOURCE:
            continue
        # 归属按**库**判（与宿主 get_sessions 给行打 profile 的方式一致）：
        # 池在它那一层已经按 agent 收窄过，这里只做兜底，不重复计数。
        if wanted_agents and str(row.get("_profile") or row.get("profile_name") or "") not in wanted_agents:
            continue
        if sid in applied_ids or sid in rejected:
            continue
        assignment = assignments.get(sid)
        if assignment is not None:
            status = assignment.get("status")
            if status == "applied":
                continue
            if status != "suggested":
                excluded_other += 1
                continue
        candidates.append(row)

    total_available = len(candidates)
    if skip:
        candidates = [row for row in candidates if str(row.get("id")) not in skip]
    skipped = total_available - len(candidates)
    start = max(0, int(offset or 0))
    sliced = candidates[start:]
    if limit:
        sliced = sliced[:limit]
    return sliced, excluded_other, total_available, skipped, visibility


def _path_matches_project(cwd: Any, project: Dict[str, Any]) -> bool:
    if not cwd:
        return False
    try:
        cwd_norm = os.path.normcase(os.path.abspath(os.fspath(cwd)))
    except (OSError, TypeError, ValueError):
        return False
    paths: List[Any] = []
    if project.get("primary_path"):
        paths.append(project.get("primary_path"))
    for folder in project.get("folders") or []:
        if isinstance(folder, dict) and folder.get("path"):
            paths.append(folder.get("path"))
    for raw_path in paths:
        try:
            project_norm = os.path.normcase(os.path.abspath(os.fspath(raw_path)))
        except (OSError, TypeError, ValueError):
            continue
        if cwd_norm == project_norm:
            return True
        prefix = project_norm.rstrip("\\/") + os.sep
        if cwd_norm.startswith(prefix):
            return True
    return False


def _collect_samples(
    projects: List[Dict[str, Any]],
    sessions: List[Dict[str, Any]],
    assignments: Dict[str, Dict[str, Any]],
) -> Dict[str, List[str]]:
    samples: Dict[str, List[str]] = {}
    for project in projects or []:
        if isinstance(project, dict):
            slug = _raw_text(project.get("slug"))
            if slug:
                samples[slug] = []
    for session in sessions:
        # 口径：看板任务会话不参与，也不允许成为「已有项目」的典型样例
        if str(session.get("source") or "").strip().lower() == KANBAN_SOURCE:
            continue
        title = _raw_text(session.get("title")).strip()
        if not title:
            continue
        sid = str(session.get("id", ""))
        assignment = assignments.get(sid)
        assigned_slug = None
        if assignment is not None and assignment.get("status") == "suggested":
            assigned_slug = _raw_text(assignment.get("project_slug"))
        matched_slug = assigned_slug if assigned_slug in samples else None
        if matched_slug is None:
            for project in projects or []:
                if not isinstance(project, dict):
                    continue
                slug = _raw_text(project.get("slug"))
                if slug in samples and _path_matches_project(session.get("cwd"), project):
                    matched_slug = slug
                    break
        if matched_slug is not None and title not in samples[matched_slug]:
            if len(samples[matched_slug]) < TITLE_SAMPLES_PER_PROJECT:
                samples[matched_slug].append(title)
    return samples


def _usage_values(usage_obj: Any) -> Dict[str, Optional[int]]:
    return {
        "prompt_tokens": _token_value(usage_obj, "prompt_tokens"),
        "completion_tokens": _token_value(usage_obj, "completion_tokens"),
        "total_tokens": _token_value(usage_obj, "total_tokens"),
    }


def _meta_from_response(
    resp: Any,
    messages: List[Dict[str, str]],
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """把 caller 返回统一成内部结构，并计算缺失 usage 的字符估算。"""
    prompt_chars = sum(len(_raw_text(m.get("content"))) for m in messages if isinstance(m, dict))
    prompt_estimate = int(math.ceil(prompt_chars / 3)) if prompt_chars else 0

    if error is None and not isinstance(resp, dict):
        error = "caller 返回值不是 dict"
    if error is None and not isinstance(resp.get("text"), str):
        error = "caller 返回值缺少 text 字符串"

    text = _raw_text(resp.get("text")) if isinstance(resp, dict) and error is None else ""
    provider = resp.get("provider") if isinstance(resp, dict) else None
    model_name = resp.get("model") if isinstance(resp, dict) else None
    elapsed = resp.get("elapsed_ms") if isinstance(resp, dict) else None
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
        elapsed_value: Optional[int] = None
    else:
        elapsed_value = int(elapsed)

    raw_usage = resp.get("usage") if isinstance(resp, dict) else None
    usage = _usage_values(raw_usage)
    usage_real = all(value is not None for value in usage.values())
    completion_estimate = int(math.ceil(len(text) / 3)) if text else 0

    prompt_tokens = usage["prompt_tokens"] if usage["prompt_tokens"] is not None else prompt_estimate
    completion_tokens = (
        usage["completion_tokens"] if usage["completion_tokens"] is not None else completion_estimate
    )
    total_tokens = usage["total_tokens"]
    if total_tokens is None:
        total_tokens = prompt_tokens + completion_tokens

    return {
        "text": text,
        "provider": provider if isinstance(provider, str) else None,
        "model": model_name if isinstance(model_name, str) else None,
        "elapsed_ms": elapsed_value,
        "usage_real": usage_real and error is None,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "call_error": error,
        "raw_excerpt": text[:200],
    }


def _engine_info() -> Dict[str, Any]:
    return {"name": "autogroup_ai", "version": 1, "dry_run": True}


def _empty_counts() -> Dict[str, int]:
    return {"total": 0, "apply": 0, "suggest": 0, "new_project": 0, "undecided": 0}


def _aggregate_usage(call_metas: List[Dict[str, Any]]) -> Dict[str, Any]:
    prompt_tokens = sum(int(meta.get("prompt_tokens") or 0) for meta in call_metas)
    completion_tokens = sum(int(meta.get("completion_tokens") or 0) for meta in call_metas)
    total_tokens = sum(int(meta.get("total_tokens") or 0) for meta in call_metas)
    real = bool(call_metas) and all(
        bool(meta.get("usage_real")) and not meta.get("call_error") for meta in call_metas
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "real": real,
        "note": _USAGE_NOTE_REAL if real else _USAGE_NOTE_ESTIMATE,
    }


def _aggregate_model(call_metas: List[Dict[str, Any]]) -> Dict[str, Any]:
    provider = None
    model_name = None
    for meta in call_metas:
        if provider is None and isinstance(meta.get("provider"), str):
            provider = meta.get("provider")
        if model_name is None and isinstance(meta.get("model"), str):
            model_name = meta.get("model")
    elapsed_values = [
        int(meta["elapsed_ms"])
        for meta in call_metas
        if isinstance(meta.get("elapsed_ms"), int) and not isinstance(meta.get("elapsed_ms"), bool)
    ]
    elapsed_total = sum(elapsed_values)
    calls = len(call_metas)
    failures = sum(1 for meta in call_metas if meta.get("call_error"))
    degraded = f"{failures} 次模型调用失败" if failures else None
    return {
        "provider": provider,
        "model": model_name,
        "task": AUX_TASK,
        "purpose": PURPOSE,
        "calls": calls,
        "elapsed_ms": elapsed_total,
        "avg_ms": round(elapsed_total / calls, 1) if calls else 0.0,
        "degraded": degraded,
    }


def _error_payload(
    home: Path,
    limit: int,
    generated_at: float,
    batch_id: str,
    detail: str,
    projects_count: int = 0,
    code: str = "llm_unavailable",
) -> Dict[str, Any]:
    model_info = _aggregate_model([])
    model_info["degraded"] = code
    return {
        "ok": False,
        "schema": PREVIEW_SCHEMA,
        "batch_id": batch_id,
        "generated_at": generated_at,
        "home": str(home),
        "engine": _engine_info(),
        "error": {"code": code, "detail": detail},
        "model": model_info,
        "usage": _aggregate_usage([]),
        "scope": {
            "limit": limit,
            "considered": 0,
            "excluded_applied": 0,
            "excluded_rejected": 0,
            "excluded_other": 0,
            "projects": projects_count,
            "candidate_session_ids": [],
        },
        "counts": _empty_counts(),
        "items": [],
        "new_project_candidates": [],
        "undecided": [],
        "writes": {"assignments": 0, "projects": 0, "dirs": 0, "cwd": 0},
        "notes": [],
    }


def _before_snapshot(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not row:
        return {
            "row_exists": False,
            "status": None,
            "project_slug": None,
            "confidence": None,
            "source": None,
            "updated_at": None,
            "applied_at": None,
        }
    return {
        "row_exists": True,
        "status": row.get("status"),
        "project_slug": row.get("project_slug"),
        "confidence": row.get("confidence"),
        "source": row.get("source"),
        "updated_at": row.get("updated_at"),
        "applied_at": row.get("applied_at"),
    }


def _make_item(
    session: Dict[str, Any],
    before: Dict[str, Any],
    brief: Dict[str, Any],
    *,
    action: str,
    proposed_status: Optional[str],
    project_slug: Optional[str],
    project_name: Optional[str],
    is_new_project: bool,
    writable: bool,
    confidence: Optional[float],
    reason: Any,
    new_project: Optional[Dict[str, Any]],
    undecided_reason: Optional[str],
    undecided_code: Optional[str],
    meta: Dict[str, Any],
    raw_excerpt: Optional[str] = None,
    support_session_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    messages = brief.get("messages")
    if not isinstance(messages, list):
        messages = []
    title = _raw_text(session.get("title")) or _raw_text(brief.get("title"))
    cwd = session.get("cwd") if "cwd" in session else brief.get("cwd")
    confidence_value: Optional[float]
    if confidence is None:
        # 无法判定的行也必须有数字 confidence：存储层契约要求数字，而 0.0 天然落在 unjudged 档
        # （可写性恒 False），不会造成「看起来有把握」的误导。其余动作保持 None（表示没拿到数）。
        confidence_value = 0.0 if action == "undecided" else None
    else:
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            confidence_value = 0.0 if action == "undecided" else None
    return {
        "session_id": str(session.get("id", "")),
        "title": title,
        "cwd": cwd,
        "before": before,
        "action": action,
        "proposed_status": proposed_status,
        "project_slug": project_slug,
        "project_name": project_name,
        "is_new_project": bool(is_new_project),
        "writable": bool(writable),
        "confidence": confidence_value,
        "reason": _raw_text(reason)[:200],
        "evidence": {
            "title": _raw_text(brief.get("title")) or title,
            "cwd": brief.get("cwd") if "cwd" in brief else cwd,
            "messages": list(messages),
            "chars": int(brief.get("chars") or 0),
        },
        "new_project": new_project,
        "support_session_ids": list(support_session_ids or []),
        "undecided_reason": undecided_reason,
        "undecided_code": undecided_code,
        # 与存储层契约（autogroup_ai_contract.ITEM_KEYS）对齐的别名键：
        # ``unjudged_reason`` 同 ``undecided_reason``；``snapshot`` 是会话侧快照（cwd/条数/最后活动），
        # 不是归属行快照（那是 ``before``）。多给这几个键不会污染 schema：存储层只取它认识的键。
        "unjudged_reason": undecided_reason,
        "snapshot": {
            "cwd": cwd,
            "message_count": session.get("message_count"),
            "last_activity_at": session.get("last_activity_at"),
        },
        "model": {
            "provider": meta.get("provider"),
            "model": meta.get("model"),
            "elapsed_ms": meta.get("elapsed_ms"),
            "usage_real": bool(meta.get("usage_real")),
            "raw_excerpt": raw_excerpt if raw_excerpt is not None else _raw_text(meta.get("raw_excerpt"))[:200],
        },
    }


def _finalize_new_projects(pending: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """两阶段聚合：先按 slug 归组，再决定是否达到新项目候选门槛。"""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for entry in pending:
        groups.setdefault(entry["slug"].lower(), []).append(entry)

    candidates: List[Dict[str, Any]] = []
    for slug, entries in groups.items():
        supporting_ids = sorted({str(entry["item"].get("session_id", "")) for entry in entries})
        evidence_count = len(supporting_ids)
        name = entries[0].get("name") or slug
        confidences = [
            float(entry["item"].get("confidence") or 0.0) for entry in entries
        ]
        avg_confidence = round(sum(confidences) / len(confidences), 3) if confidences else 0.0
        is_candidate = evidence_count >= NEW_PROJECT_MIN_SESSIONS
        downgrade = (
            f"新项目候选仅 {evidence_count} 条会话，不足 {NEW_PROJECT_MIN_SESSIONS} 条，"
            "降为 suggested（需人工复核）"
        )
        for entry in entries:
            item = entry["item"]
            item["is_new_project"] = True
            item["project_name"] = name
            item["writable"] = is_candidate
            item["support_session_ids"] = list(supporting_ids)
            if is_candidate:
                item["project_slug"] = slug
                item["action"] = (
                    "apply_after_confirm"
                    if tier_for(float(item.get("confidence") or 0.0)) == "apply"
                    else "suggest_after_confirm"
                )
                item["proposed_status"] = (
                    "applied" if item["action"] == "apply_after_confirm" else "suggested"
                )
                item["undecided_code"] = None
                item["undecided_reason"] = None
                item["new_project"] = {
                    "slug": slug,
                    "name": name,
                    "candidate": True,
                    "evidence_count": evidence_count,
                    "supporting_session_ids": supporting_ids,
                    "avg_confidence": avg_confidence,
                    "downgrade_reason": None,
                }
            else:
                item["project_slug"] = None
                item["action"] = "suggest_after_confirm"
                item["proposed_status"] = "suggested"
                item["undecided_code"] = "new_project_insufficient"
                item["undecided_reason"] = downgrade
                item["new_project"] = {
                    "slug": slug,
                    "name": name,
                    "candidate": False,
                    "evidence_count": evidence_count,
                    "supporting_session_ids": supporting_ids,
                    "avg_confidence": avg_confidence,
                    "downgrade_reason": downgrade,
                }
        if is_candidate:
            candidates.append({
                "slug": slug,
                "name": name,
                "evidence_count": evidence_count,
                "supporting_session_ids": supporting_ids,
                "avg_confidence": avg_confidence,
                "candidate": True,
            })

    candidates.sort(key=lambda item: (-item["evidence_count"], item["slug"]))
    return candidates


def build_preview(home=None, *, limit=40, caller=None, now=None, offset=0,
                  skip_ids=None, on_progress=None, agents=None,
                  projects_home=None) -> Dict[str, Any]:
    """读取候选会话、逐条调用模型并返回可序列化预览；绝不写业务库。

    分页/分批（界面**必须**显式传 ``limit``：默认 40 只是历史兼容，不是给面板用的）：

    * ``limit=0`` ⇒ 全量，处理 ``scope.total_available`` 条；
    * ``offset`` / ``skip_ids`` ⇒ 分批续跑。``skip_ids`` 优先：UI 把上一批的 session_id
      带回来去重，即使两次调用之间候选列表变了也不会重复处理同一会话；
    * ``on_progress(done, total)`` ⇒ 每处理完一条回调一次（后台 job 靠它报「已处理 N」）。
    """
    generated_at = _now(now)
    batch_id = _new_batch_id(generated_at)
    home_path = Path(home) if home is not None else Path(base._home())
    #: 项目库 / 归属库 / 规则库的 home（t_4fa94e17）：按 agent 分。
    #: ``home_path`` 保持**根 home** —— 会话池要靠它枚举 ``<root>/profiles/*``。
    #: 两者相同时（agent=default / 老调用方）行为与改前逐字段一致。
    pj_home = Path(projects_home) if projects_home is not None else home_path
    try:
        lim = int(limit) if limit is not None else 0
    except (TypeError, ValueError):
        lim = 40
    if lim < 0:
        lim = 0

    try:
        projects = [p for p in core.list_projects(pj_home) if isinstance(p, dict)]
    except Exception as exc:  # noqa: BLE001
        # 读不到项目清单时**不能当成「一个项目都没有」**：那会把每条会话都判成新项目候选。
        return _error_payload(
            home_path, lim, generated_at, batch_id,
            "读取项目清单失败：" + base._describe(exc), 0, "projects_unavailable",
        )
    project_names: Dict[str, str] = {}
    for project in projects:
        slug = _raw_text(project.get("slug"))
        if slug:
            project_names[slug] = _raw_text(project.get("name")) or slug

    if caller is None:
        try:
            caller = default_caller()
        except Exception:
            caller = None
    if not callable(caller):
        return _error_payload(
            home_path,
            lim,
            generated_at,
            batch_id,
            "没有可用的模型调用器（default_caller 不可用）",
            len(projects),
        )

    assignments, applied_ids = _load_assignments(pj_home)
    rejected = _read_rejected(pj_home)
    skip_set = {str(s) for s in (skip_ids or []) if str(s).strip()}
    try:
        off = max(0, int(offset or 0))
    except (TypeError, ValueError):
        off = 0
    sessions, excluded_other, total_available, skipped, visibility = _candidate_sessions(
        home_path, lim, assignments, applied_ids, rejected, offset=off, skip_ids=skip_set,
        agents=agents,
    )
    samples = _collect_samples(projects, sessions, assignments)

    items: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    call_metas: List[Dict[str, Any]] = []

    for position, session in enumerate(sessions, 1):
        if callable(on_progress):
            try:
                on_progress(position - 1, len(sessions))
            except Exception:  # noqa: BLE001 - 进度回调坏了不许影响分类
                pass
        sid = str(session.get("id", ""))
        before = _before_snapshot(assignments.get(sid))
        brief = session_brief(home_path, sid, db_path=session.get("_db"))
        if not brief.get("title"):
            brief["title"] = _raw_text(session.get("title"))
        if brief.get("cwd") is None and session.get("cwd") is not None:
            brief["cwd"] = session.get("cwd")
        messages = build_messages(projects, samples, brief)

        try:
            resp = caller(
                messages,
                task=AUX_TASK,
                purpose=PURPOSE,
                temperature=0.1,
                max_tokens=800,
                timeout=60.0,
            )
        except Exception as exc:
            meta = _meta_from_response(None, messages, error="模型调用失败: " + str(exc))
            call_metas.append(meta)
            items.append(_make_item(
                session, before, brief,
                action="undecided",
                proposed_status=None,
                project_slug=None,
                project_name=None,
                is_new_project=False,
                writable=False,
                confidence=None,
                reason=meta["call_error"],
                new_project=None,
                undecided_reason=meta["call_error"],
                undecided_code="call_failed",
                meta=meta,
            ))
            continue

        meta = _meta_from_response(resp, messages)
        call_metas.append(meta)
        if meta.get("call_error"):
            items.append(_make_item(
                session, before, brief,
                action="undecided",
                proposed_status=None,
                project_slug=None,
                project_name=None,
                is_new_project=False,
                writable=False,
                confidence=None,
                reason=meta["call_error"],
                new_project=None,
                undecided_reason=meta["call_error"],
                undecided_code="call_failed",
                meta=meta,
            ))
            continue

        parsed = parse_verdict(meta["text"], projects)
        if not parsed["ok"]:
            code = parsed.get("code") or "invalid_json"
            detail = parsed.get("detail") or "模型输出无法校验"
            items.append(_make_item(
                session, before, brief,
                action="undecided",
                proposed_status=None,
                project_slug=None,
                project_name=None,
                is_new_project=False,
                writable=False,
                confidence=None,
                reason=detail,
                new_project=None,
                undecided_reason=detail,
                undecided_code=code,
                meta=meta,
                raw_excerpt=parsed.get("raw_excerpt"),
            ))
            continue

        verdict = parsed.get("verdict") or {}
        try:
            confidence = float(verdict.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        tier = tier_for(confidence)

        if not brief.get("messages"):
            detail = "会话正文不可用，无法可靠分类"
            items.append(_make_item(
                session, before, brief,
                action="undecided",
                proposed_status=None,
                project_slug=None,
                project_name=None,
                is_new_project=False,
                writable=False,
                confidence=confidence,
                reason=verdict.get("reason") or detail,
                new_project=None,
                undecided_reason=detail,
                undecided_code="brief_unavailable",
                meta=meta,
                raw_excerpt=parsed.get("raw_excerpt"),
            ))
            continue

        if tier == "undecided":
            detail = (
                f"模型置信度 {confidence:.3f} 低于 {CONFIDENT_SUGGEST:.2f}，仅记录不归类"
            )
            items.append(_make_item(
                session, before, brief,
                action="undecided",
                proposed_status=None,
                project_slug=None,
                project_name=None,
                is_new_project=False,
                writable=False,
                confidence=confidence,
                reason=verdict.get("reason") or detail,
                new_project=None,
                undecided_reason=detail,
                undecided_code="low_confidence",
                meta=meta,
                raw_excerpt=parsed.get("raw_excerpt"),
            ))
            continue

        project_slug = verdict.get("project_slug")
        if isinstance(project_slug, str) and project_slug:
            action = "apply_after_confirm" if tier == "apply" else "suggest_after_confirm"
            proposed_status = "applied" if tier == "apply" else "suggested"
            items.append(_make_item(
                session, before, brief,
                action=action,
                proposed_status=proposed_status,
                project_slug=project_slug,
                project_name=project_names.get(project_slug) or project_slug,
                is_new_project=False,
                writable=True,
                confidence=confidence,
                reason=verdict.get("reason") or "",
                new_project=None,
                undecided_reason=None,
                undecided_code=None,
                meta=meta,
                raw_excerpt=parsed.get("raw_excerpt"),
            ))
            continue

        new_project = verdict.get("new_project")
        if isinstance(new_project, dict):
            np_slug = _raw_text(new_project.get("slug"))
            np_name = _raw_text(new_project.get("name"))
            action = "apply_after_confirm" if tier == "apply" else "suggest_after_confirm"
            proposed_status = "applied" if tier == "apply" else "suggested"
            item = _make_item(
                session, before, brief,
                action=action,
                proposed_status=proposed_status,
                project_slug=None,
                project_name=np_name,
                is_new_project=True,
                writable=False,
                confidence=confidence,
                reason=verdict.get("reason") or "",
                new_project={
                    "slug": np_slug,
                    "name": np_name,
                    "candidate": False,
                    "evidence_count": 0,
                    "supporting_session_ids": [],
                    "downgrade_reason": None,
                },
                undecided_reason=None,
                undecided_code=None,
                meta=meta,
                raw_excerpt=parsed.get("raw_excerpt"),
            )
            items.append(item)
            pending.append({"slug": np_slug, "name": np_name, "item": item})
            continue

        # parse_verdict 已阻止这种组合；保留兜底但不伪造项目。
        detail = "模型没有给出可用的项目目标"
        items.append(_make_item(
            session, before, brief,
            action="undecided",
            proposed_status=None,
            project_slug=None,
            project_name=None,
            is_new_project=False,
            writable=False,
            confidence=confidence,
            reason=verdict.get("reason") or detail,
            new_project=None,
            undecided_reason=detail,
            undecided_code="low_confidence",
            meta=meta,
            raw_excerpt=parsed.get("raw_excerpt"),
        ))

    if callable(on_progress):
        try:
            on_progress(len(sessions), len(sessions))
        except Exception:  # noqa: BLE001
            pass

    candidates = _finalize_new_projects(pending)

    undecided = [
        {
            "session_id": item["session_id"],
            "title": item["title"],
            "code": item["undecided_code"],
            "reason": item["undecided_reason"] or item["reason"],
        }
        for item in items
        if item["action"] == "undecided"
    ]
    undecided.sort(key=lambda row: row["session_id"])

    # 分桶互斥（undecided > new_project > apply > suggest），这样四个数相加 == total，
    # 存储层/UI 可以直接拿去显示「这轮各档多少条」而不用自己再算一遍。
    counts = {
        "total": len(items),
        "apply": 0,
        "suggest": 0,
        "new_project": 0,
        "undecided": 0,
    }
    for item in items:
        if item["action"] == "undecided":
            counts["undecided"] += 1
        elif item["is_new_project"]:
            counts["new_project"] += 1
        elif item["action"] == "apply_after_confirm":
            counts["apply"] += 1
        else:
            counts["suggest"] += 1

    notes = [
        "范围口径（%s）：只处理**桌面端真实可见的普通对话**；已排除看板任务 %d 条、隐藏会话 %d 条、"
        "其他不可见来源 %d 条、已归档未置顶 %d 条 —— 这些都是**正常排除，不是漏处理**"
        % (SCOPE_RULE, visibility.get("excluded_kanban", 0), visibility.get("excluded_hidden", 0),
           visibility.get("excluded_source", 0), visibility.get("excluded_archived", 0)),
        "分层口径：>=%.2f 确认后可 applied；%.2f~<%.2f 确认后只 suggested；<%.2f 只进无法判定清单"
        % (CONFIDENT_APPLY, CONFIDENT_SUGGEST, CONFIDENT_APPLY, CONFIDENT_SUGGEST),
        "输入只含无归属/待确认会话：已排除 applied %d 条、rejected %d 条、其他状态 %d 条"
        % (len(applied_ids), len(rejected), excluded_other),
        "数据源：%s 的 state.db（与 /overview/sessions?agent=X 同源；不是只看根库）"
        % ("、".join(str(p) for p, _d in profile_scan_targets(
            home_path, agents={str(a) for a in (agents or set()) if str(a).strip()} or None)) or "—"),
        "本预览不写任何库、不建目录、不改 cwd（writes 恒为 0）；确认与写入由存储层执行",
    ]
    if candidates:
        notes.append("新项目候选 %d 个（每个需 >=%d 条不同会话支撑）"
                     % (len(candidates), NEW_PROJECT_MIN_SESSIONS))
    if not _aggregate_usage(call_metas)["real"]:
        notes.append("usage 缺失真实值，已按字符数估算（口径见 usage.note）")

    return {
        "ok": True,
        "schema": PREVIEW_SCHEMA,
        "batch_id": batch_id,
        "generated_at": generated_at,
        "home": str(home_path),
        "engine": _engine_info(),
        "model": _aggregate_model(call_metas),
        "usage": _aggregate_usage(call_metas),
        "scope": {
            "limit": lim,
            "offset": off,
            "considered": len(sessions),
            "total_available": total_available,
            "scope_rule": SCOPE_RULE,
            "desktop_visible": visibility.get("desktop_visible", total_available),
            "excluded_kanban": visibility.get("excluded_kanban", 0),
            "excluded_hidden": visibility.get("excluded_hidden", 0),
            "excluded_source": visibility.get("excluded_source", 0),
            "excluded_archived": visibility.get("excluded_archived", 0),
            "excluded_no_message": visibility.get("excluded_no_message", 0),
            "excluded_child": visibility.get("excluded_child", 0),
            "visibility_rule": ("桌面端可见条件：source 不属于 acp/kanban/oneshot/subagent/tool、"
                                "hidden=0、archived 但 pinned 仍可见、message_count>=1、"
                                "压缩续接/subagent 子行不列（正常排除，不是漏处理）"),
            "truncated": bool(total_available > len(sessions)),
            "omitted": max(0, total_available - off - len(sessions)),
            "skipped_already_done": skipped,
            "excluded_applied": len(applied_ids),
            "excluded_rejected": len(rejected),
            "excluded_other": excluded_other,
            #: 本池口径内被 excluded 的 applied 条数：池 − 候选 − rejected − 其它状态。
            #: 界面写「池里共 N 条、其中已分组 M 条」必须用这一对同源数字（t_bba9216d）。
            "applied_in_pool": max(0, int(visibility.get("desktop_visible") or 0)
                                   - total_available - len(rejected) - excluded_other),
            "projects": len(projects),
            "candidate_session_ids": [str(session.get("id", "")) for session in sessions],
        },
        "counts": counts,
        "items": items,
        "new_project_candidates": candidates,
        "undecided": undecided,
        "writes": {"assignments": 0, "projects": 0, "dirs": 0, "cwd": 0},
        "notes": notes,
    }


def candidate_scope(home=None, *, agents=None, projects_home=None) -> Dict[str, Any]:
    """**不调模型**的口径统计：候选总数 / 各类排除数（面板打开时先拿它显示「共 N 条」）。

    与 :func:`build_preview` 共用 :func:`_candidate_sessions`（同一个 limit=0 口径），
    所以界面上的 ``total_available`` 与真正分类时的候选集合**必然一致** —— 不存在
    「统计说 67、实际只处理 40」这种两套口径的问题。

    ``agents``（t_bffc584b）：给了就**整份口径都按这些 agent 收窄** —— 候选集、
    ``excluded_kanban/hidden/source/archived/no_message``（取池阶段按 agent 过滤）、
    ``excluded_applied/rejected``（扫全部 profile 库查归属后计数）、``projects`` 不变（项目是全局的）。
    其它 agent 的桌面可见会话数单独放在 ``excluded_agent``，不混进任何一桶。

    **数据源（t_bba9216d）**：池来自该 agent 自己的 ``state.db``（``profiles/<p>/state.db``），
    与 ``/overview/sessions?agent=X`` 同源；``agents=None`` 取全部库并集（id 去重）。
    另给 ``pool``（池大小）/ ``applied_in_pool``（本池里被 applied 挡掉的条数）/
    ``candidate_profiles``（候选 → 归属）—— 界面必须用这一组同源数字解释空态，
    不能拿全库 applied 去说明一个本池为 0 的池。
    """
    home_path = Path(home) if home is not None else Path(base._home())
    pj_home = Path(projects_home) if projects_home is not None else home_path
    wanted_agents = {str(a).strip() for a in (agents or set()) if str(a).strip()}
    try:
        projects = [p for p in core.list_projects(pj_home) if isinstance(p, dict)]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": {"code": "projects_unavailable",
                                      "detail": "读取项目清单失败：" + base._describe(exc)}}
    assignments, applied_ids = _load_assignments(pj_home)
    rejected = _read_rejected(pj_home)
    candidates, excluded_other, total_available, _skipped, visibility = _candidate_sessions(
        home_path, 0, assignments, applied_ids, rejected, agents=wanted_agents or None
    )
    sessions_total = int(visibility.get("desktop_visible") or 0)
    # 归属表不带 profile：按 agent 计数必须回 sessions 表查归属（扫全部 profile 库，见
    # ``session_profiles``；只查根库会把 work/xiaomo 的 applied 一条都数不到 —— t_bba9216d）。
    owner_of = session_profiles(home_path)
    applied_count, rejected_count = len(applied_ids), len(rejected)
    if wanted_agents:
        applied_count = len({str(s) for s in applied_ids
                             if owner_of.get(str(s), "") in wanted_agents})
        rejected_count = len({str(s) for s in rejected
                              if owner_of.get(str(s), "") in wanted_agents})
    #: 本池口径内被 applied 排除的条数（= desktop_visible − 候选 − rejected − 其它状态）。
    #: 界面必须用这个数说「池里被排除多少」，不能拿**全库** applied（91）去解释一个本池为 0 的池
    #: （那会写出「池里共 0 条，其中已分组 91 条」这种字面不成立的句子 —— t_bba9216d）。
    applied_in_pool = max(0, sessions_total - total_available - rejected_count - excluded_other)
    return {
        "ok": True,
        "home": str(home_path),
        "engine": _engine_info(),
        "agent": (sorted(wanted_agents)[0] if len(wanted_agents) == 1
                  else ("__all__" if not wanted_agents else sorted(wanted_agents))),
        "agent_scoped": bool(wanted_agents),
        "total_available": total_available,
        "desktop_visible": total_available,
        "scope_rule": SCOPE_RULE,
        "excluded_kanban": visibility.get("excluded_kanban", 0),
        "excluded_hidden": visibility.get("excluded_hidden", 0),
        "excluded_source": visibility.get("excluded_source", 0),
        "excluded_archived": visibility.get("excluded_archived", 0),
        "excluded_no_message": visibility.get("excluded_no_message", 0),
        "excluded_child": visibility.get("excluded_child", 0),
        "excluded_agent": visibility.get("excluded_agent", 0),
        "visibility_rule": ("桌面端可见条件：source 不属于 acp/kanban/oneshot/subagent/tool、"
                            "hidden=0、archived 但 pinned 仍可见、message_count>=1、"
                            "压缩续接/subagent 子行不列（正常排除，不是漏处理）"),
        "candidate_session_ids": [str(row.get("id", "")) for row in candidates],
        "candidate_profiles": {str(row.get("id", "")): str(row.get("_profile") or "")
                               for row in candidates},
        "sessions_total": sessions_total,
        "pool": sessions_total,
        "applied_in_pool": applied_in_pool,
        "excluded_applied": applied_count,
        "excluded_rejected": rejected_count,
        "excluded_other": excluded_other,
        "projects": len(projects),
        "profiles_scanned": [
            {"profile": str(p), "db": str(d), "exists": Path(d).is_file()}
            for p, d in profile_scan_targets(home_path, agents=wanted_agents or None)],
        "writes": {"assignments": 0, "projects": 0, "dirs": 0, "cwd": 0},
    }

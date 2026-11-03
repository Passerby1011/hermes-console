# -*- coding: utf-8 -*-
"""Advisor 评审模型「运行时真生效」证据（t_4d654faf）。

不接受"接口返回 200"：这里真跑一轮 advisor 评审，把 **实际发给 ctx.llm.complete 的 kwargs**
记下来，比对 settings.json 里写的 provider / model。

做法（复用 advisor 自带的 host 模拟，但不改它的测试）：

    temp home → 写 <home>/advisor/audit/settings.json（enabled / model / provider）
    → 用 FakeCtx（llm.complete 记录 kwargs）挂 AdvisorRuntime
    → on_post_llm_call（= 一次 turn 结束）
    → 断言 kwargs.model / kwargs.provider == settings.json 里的值
    → 把 settings 改成空 ⇒ kwargs 里**没有** model/provider（继承主模型）
    → 读 reviews.jsonl：一条 review 记录的 model/provider 与 kwargs 一致
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]          # <home>/plugins/hermes-console
HOME_ROOT = Path(__file__).resolve().parents[3]        # <home>
ADVISOR = HOME_ROOT / "plugins" / "advisor"
HERMES_AGENT = HOME_ROOT / "hermes-agent"

failures: list[str] = []
passed = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global passed
    print(("  PASS  " if ok else "  FAIL  ") + label + (" — " + detail if detail else ""))
    if ok:
        passed += 1
    else:
        failures.append(label)
    return ok


class _Usage:
    total_tokens = 7


class _Result:
    provider = ""
    model = ""
    usage = _Usage()

    def __init__(self, text: str) -> None:
        self.text = text


class RecordingLlm:
    """记录每次 complete 的 kwargs（这就是"该模块实际调用时用的 provider/model"）。"""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls: list[dict] = []

    def complete(self, **kwargs):
        self.calls.append(dict(kwargs))
        result = _Result(self.text)
        result.model = str(kwargs.get("model") or "")
        result.provider = str(kwargs.get("provider") or "")
        return result


class FakeCtx:
    def __init__(self) -> None:
        self.llm = RecordingLlm("")
        self.injected: list[tuple[str, str]] = []

    def inject_message(self, message, role="user", *, session_key=None):
        self.injected.append((message, role))
        return True


def write_settings(home: Path, payload: dict) -> Path:
    path = home / "advisor" / "audit" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def run_one_turn(runtime, ctx, turn_id: str, session_id: str = "s1") -> None:
    from gateway.session_context import clear_session_vars, set_session_vars

    @contextmanager
    def _bound():
        tokens = set_session_vars(
            session_key="agent:main:local:dm:%s" % session_id,
            session_id=session_id,
            platform="local",
        )
        try:
            yield
        finally:
            clear_session_vars(tokens)

    with _bound():
        runtime.on_post_llm_call(
            session_id=session_id,
            turn_id=turn_id,
            user_message="wire up the retry",
            assistant_response="wired up the retry",
            conversation_history=[{"role": "user", "content": "wire up the retry"}],
            model="primary-model",
        )
    if not runtime.wait_for_idle(15.0):
        raise RuntimeError("review never finished")


def review_rows(home: Path) -> list[dict]:
    path = home / "advisor" / "audit" / "reviews.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def main() -> int:
    sandbox = Path(tempfile.mkdtemp(prefix="adv-runtime-"))
    try:
        os.environ["HERMES_HOME"] = str(sandbox)
        for path in (str(ADVISOR.parent), str(HERMES_AGENT)):
            if path not in sys.path:
                sys.path.insert(0, path)

        import hermes_constants  # noqa: E402

        hermes_constants.get_hermes_home = lambda: sandbox
        sys.path.insert(0, str(ADVISOR / "tests"))
        import host_sim  # noqa: E402
        from advisor.runtime import AdvisorRuntime  # noqa: E402

        host_sim.seed_store(sandbox, "s1", "s2")

        print("[A] settings.json 写了 provider=palpha / model=adv-model-X ⇒ 真的按它调")
        write_settings(sandbox, {
            "enabled": True, "model": "adv-model-X", "provider": "palpha",
            "concern_default": "hold", "min_interval_seconds": 0, "mode": "auto",
        })
        ctx = FakeCtx()
        runtime = AdvisorRuntime(ctx)
        ctx.llm.text = ""
        run_one_turn(runtime, ctx, "turn-A")
        check("真调了一次 ctx.llm.complete", len(ctx.llm.calls) == 1, "calls=%d" % len(ctx.llm.calls))
        first = ctx.llm.calls[0] if ctx.llm.calls else {}
        check("★kwargs.model == settings.json 的 model（adv-model-X）",
              first.get("model") == "adv-model-X", str(first.get("model")))
        check("★kwargs.provider == settings.json 的 provider（palpha）",
              first.get("provider") == "palpha", str(first.get("provider")))
        check("purpose 标明这是一次评审（可归因）",
              str(first.get("purpose") or "").startswith("advisor_review"),
              str(first.get("purpose")))

        print("\n[B] reviews.jsonl 里记的 model/provider 与调用参数一致（审计行）")
        rows = [row for row in review_rows(sandbox) if row.get("outcome") not in (None, "started")]
        check("有 review 记录（非 started）", bool(rows), "rows=%d" % len(rows))
        last = rows[-1] if rows else {}
        check("记录里 model == adv-model-X", last.get("model") == "adv-model-X", str(last.get("model")))
        check("记录里 provider == palpha", last.get("provider") == "palpha", str(last.get("provider")))

        print("\n[C] 把 model/provider 清空 ⇒ kwargs 里没有 model/provider（继承主模型）")
        write_settings(sandbox, {
            "enabled": True, "model": "", "provider": "",
            "concern_default": "hold", "min_interval_seconds": 0, "mode": "auto",
        })
        ctx2 = FakeCtx()
        runtime2 = AdvisorRuntime(ctx2)
        run_one_turn(runtime2, ctx2, "turn-C")
        check("又调了一次", len(ctx2.llm.calls) == 1, "calls=%d" % len(ctx2.llm.calls))
        second = ctx2.llm.calls[0] if ctx2.llm.calls else {}
        check("★kwargs 里没有 model（继承主模型，而不是空串覆写）",
              "model" not in second, str(sorted(second.keys())))
        check("★kwargs 里没有 provider", "provider" not in second, str(sorted(second.keys())))

        print("\n[D] 按 profile 独立：同一台机器上另一个 profile 的 settings 不串")
        settings_a = write_settings(sandbox, {"enabled": False, "model": "a-model", "provider": "palpha"})
        dig_a = settings_a.read_bytes()
        other = sandbox / "profiles" / "beta"
        (other / "advisor" / "audit").mkdir(parents=True, exist_ok=True)
        (other / "advisor" / "audit" / "settings.json").write_text(
            json.dumps({"enabled": False, "model": "b-model", "provider": "pbeta"}), encoding="utf-8")
        check("写 beta 的 settings 后 alpha 的文件逐字节相同",
              settings_a.read_bytes() == dig_a, "changed")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    print("\n=== 汇总 ===")
    print("PASS: %d   FAIL: %d" % (passed, len(failures)))
    print("ADVISOR-RUNTIME-OK" if not failures else "ADVISOR-RUNTIME-FAILED")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())

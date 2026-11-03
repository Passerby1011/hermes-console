# -*- coding: utf-8 -*-
"""hermes-console · Advisor / 自动命名 的 provider→model 闸门（t_4d654faf）。

跑法::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        plugins/hermes-console/scripts/probe_model_pair_gates.py

这两条写入口**不归 api_models**（advisor 写 settings.json、自动命名写宿主 settings），
但同样只该接受"这个 provider 下真的有的模型"。本探针真 import 两个域模块、真读沙盒
config.yaml，验证：

    [1] api_models.model_pair_error：合法组合 None / 非法组合给可读原因
    [2] api_advisor._model_pair_problem：只换 provider 留旧模型 ⇒ 有原因（会被 PUT 400）
    [3] api_advisor._model_pair_problem：两个键一起改成合法组合 ⇒ None
    [4] api_autotitler._check_model_pair：非法模型名 ⇒ ApiError 400，消息点名 provider 与候选
    [5] api_autotitler._check_model_pair：合法模型名 / 空值 / 非 model 键 ⇒ 放行
    [6] 两条闸门都不会因为"读不到清单"而拒（fail-open）
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN / "dashboard"
HERMES_AGENT = Path(__file__).resolve().parents[3] / "hermes-agent"

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


CONFIG = """\
model:
  default: alpha-main
  provider: palpha
providers:
  palpha:
    base_url: https://palpha.example/v1
    models:
      alpha-main: {}
      alpha-alt: {}
  pbeta:
    base_url: https://pbeta.example/v1
    models:
      beta-main: {}
"""


def main() -> int:
    sandbox = Path(tempfile.mkdtemp(prefix="pairgate-"))
    try:
        (sandbox / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
        (sandbox / "config.yaml").write_text(CONFIG, encoding="utf-8")
        (sandbox / "profiles" / "alpha" / "config.yaml").write_text(CONFIG, encoding="utf-8")
        (sandbox / "profiles" / "alpha" / "profile.yaml").write_text("title: alpha\n", encoding="utf-8")
        os.environ["HERMES_HOME"] = str(sandbox)

        for extra in (str(DASHBOARD), str(HERMES_AGENT)):
            if extra not in sys.path:
                sys.path.insert(0, extra)
        import api_models  # noqa: E402

        api_models._home = lambda: sandbox

        import api_advisor  # noqa: E402
        import api_autotitler  # noqa: E402
        from api_models import ApiError  # noqa: E402

        # api_autotitler 自带一个同名 ApiError 类（它自己定义的），捕获要认它那一个。
        AutotitlerError = getattr(api_autotitler, "ApiError", ApiError)

        print("[1] api_models.model_pair_error（与 /validate、scope=auxiliary 同一套规则）")
        check("合法 palpha/alpha-alt ⇒ None",
              api_models.model_pair_error("alpha", "palpha", "alpha-alt") is None)
        bad = api_models.model_pair_error("alpha", "palpha", "beta-main")
        check("非法 palpha/beta-main ⇒ 给原因", bool(bad), str(bad)[:120])
        check("原因里点名 provider 与候选", bad and "palpha" in bad and "alpha-main" in bad,
              str(bad)[:160])
        check("provider=auto ⇒ 不校验（跟随主模型）",
              api_models.model_pair_error("alpha", "auto", "whatever") is None)
        check("不认识的 provider（清单为空）⇒ 放行（fail-open）",
              api_models.model_pair_error("alpha", "pnothere", "x") is None)

        print("\n[2] api_advisor：只换 provider 留旧模型 ⇒ 会被 PUT 400")
        problem = api_advisor._model_pair_problem(
            "alpha", {"model": "alpha-main", "provider": "palpha"},
            {"provider": "pbeta"})
        check("换到 pbeta 但模型还是 alpha-main ⇒ 有原因", bool(problem), str(problem)[:140])
        check("原因写明 alpha-main 不属于 pbeta",
              problem and "alpha-main" in problem and "pbeta" in problem, str(problem)[:160])

        print("\n[3] api_advisor：两个键一起改成合法组合 ⇒ 放行")
        check("pbeta + beta-main ⇒ None",
              api_advisor._model_pair_problem(
                  "alpha", {"model": "alpha-main", "provider": "palpha"},
                  {"provider": "pbeta", "model": "beta-main"}) is None)
        check("只改 model 到本 provider 的另一个模型 ⇒ None",
              api_advisor._model_pair_problem(
                  "alpha", {"model": "alpha-main", "provider": "palpha"},
                  {"model": "alpha-alt"}) is None)
        check("不改 model/provider（只改 enabled）⇒ 不校验",
              api_advisor._model_pair_problem(
                  "alpha", {"model": "alpha-main", "provider": "palpha"},
                  {"enabled": False}) is None)

        print("\n[4] api_autotitler：写 title_model 时按当前 provider 校验")
        schema = {"title_model": {"type": "str"}, "provider": {"type": "str"}}
        # `_effective()` 会去读该 agent 的宿主 settings / 插件 config.yaml；沙盒里没有装插件，
        # 所以把"生效值"这一层换成我们要的场景（这正是生产读的那个 seam，不是绕过被测代码）。
        original_effective = api_autotitler._effective

        def _effective_stub(_agent, _schema):
            return {"provider": {"value": "palpha", "source": "host_settings"},
                    "title_model": {"value": "", "source": "default"}}

        api_autotitler._effective = _effective_stub
        try:
            try:
                api_autotitler._check_model_pair("alpha", "title_model", "beta-main", schema)
                check("非法 title_model（beta-main@palpha）⇒ 400", False, "没有抛")
            except AutotitlerError as exc:
                message = str(getattr(exc, "message", "") or exc)
                check("非法 title_model（beta-main@palpha）⇒ 400", True, "status=%s" % exc.status)
                check("报错可读：点名 provider 与候选模型",
                      "palpha" in message and "alpha-main" in message, message[:160])

            print("\n[5] api_autotitler：合法 / 空 / 非 model 键都放行")
            for label, key, value in (
                ("合法 title_model=alpha-alt", "title_model", "alpha-alt"),
                ("空 title_model（跟随默认）", "title_model", ""),
                ("改 provider（不由这条闸门管）", "provider", "pbeta"),
                ("改 enabled（与模型无关）", "enabled", True),
            ):
                try:
                    api_autotitler._check_model_pair("alpha", key, value, schema)
                    check(label + " ⇒ 放行", True)
                except ApiError as exc:  # noqa: BLE001
                    check(label + " ⇒ 放行", False, str(exc)[:120])
        finally:
            api_autotitler._effective = original_effective

        print("\n[6] 读不到清单时 fail-open（不让面板因为宿主缺失整段不可用）")
        empty = Path(tempfile.mkdtemp(prefix="pairgate-empty-"))
        try:
            (empty / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
            (empty / "config.yaml").write_text("model:\n  provider: pnone\n", encoding="utf-8")
            (empty / "profiles" / "alpha" / "config.yaml").write_text(
                "model:\n  provider: pnone\n", encoding="utf-8")
            api_models._home = lambda: empty
            check("没有 providers 段 ⇒ model_pair_error 返回 None",
                  api_models.model_pair_error("alpha", "pnone", "anything") is None)
        finally:
            shutil.rmtree(empty, ignore_errors=True)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    print("\n=== 汇总 ===")
    print("PASS: %d   FAIL: %d" % (passed, len(failures)))
    print("MODEL-PAIR-GATES-OK" if not failures else "MODEL-PAIR-GATES-FAILED")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())

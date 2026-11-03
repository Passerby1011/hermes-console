# -*- coding: utf-8 -*-
"""沙盒验收探针：A/B/E/F + 多 profile 隔离（**绝不碰真 home**）。

沙盒：tmp 目录造一个形状真实的 home（``config.yaml`` + ``.env`` + ``profiles/<p>/…``），
把 ``api_models._home`` 这一个漏斗指向沙盒；再用 ``os.environ.pop`` 清掉真实的
``*_BASE_URL``，这样"清掉 OPENAI_BASE_URL"是真的清掉，而不是绕开被测代码。

断言：
  A. 清掉 OPENAI_BASE_URL ⇒ openai-api 回落到 https://api.openai.com/v1（builtin，不空不崩）
  B. config 显式 base_url 优先于 env（env 同时给不同值，必须取 config）⇒ source=config
  C. _models_endpoint_url 三种边界（/v1 结尾、不以 /v1 结尾、尾部斜杠）都不得出现 /v1/v1/
  D. 真实中转站地址推出的 URL 精确等于 https://newapi.passerbywtj.club/v1/models
  E. env 与 config 都没有 ⇒ source=builtin，base_url 非空（不得静默空值）
  F. 多 profile 隔离：两个 profile 的 .env 给不同地址 ⇒ 各自读到自己的
  G. registry 结构带 base_url_env_var（且只放变量名）
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN = Path("D:/hermes-data/hermes/plugins/hermes-console")
DASH = PLUGIN / "dashboard"
AGENT_ROOT = Path("D:/hermes-data/hermes/hermes-agent")
sys.path.insert(0, str(AGENT_ROOT))
sys.path.insert(0, str(DASH))

import api_models  # noqa: E402

passed = 0
failures: list = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    global passed
    print(("  PASS  " if ok else "  FAIL  ") + label + ("  — " + detail if detail else ""))
    if ok:
        passed += 1
    else:
        failures.append(label + ("  — " + detail if detail else ""))
    return ok


ENV_KEYS = ["OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "XAI_BASE_URL", "DEEPSEEK_BASE_URL",
            "MINIMAX_BASE_URL", "OPENROUTER_BASE_URL", "GLM_BASE_URL", "GEMINI_BASE_URL"]
SAVED_ENV = {k: os.environ.pop(k) for k in ENV_KEYS if k in os.environ}
print("进程环境里已临时移除的 %d 个 *_BASE_URL（稍后原样恢复，取值不打印）" % len(SAVED_ENV))

sandbox = Path(tempfile.mkdtemp(prefix="hc-baseurl-", dir=str(Path(__file__).parent)))
try:
    (sandbox / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    (sandbox / "profiles" / "beta").mkdir(parents=True, exist_ok=True)
    (sandbox / "profiles" / "gamma").mkdir(parents=True, exist_ok=True)
    # 根配置：显式写死 base_url 的 provider（负控 B 的 fixture）
    (sandbox / "config.yaml").write_text(
        "model:\n  default: m0\n  provider: pinned\n"
        "providers:\n"
        "  pinned:\n"
        "    base_url: https://config-wins.example/v1\n"
        "    key_env: PINNED_API_KEY\n", encoding="utf-8")
    # 根 .env：给 pinned 一个**不同**的 env 值（必须被 config 压住）、给 openai 一个中转站值
    (sandbox / ".env").write_text(
        "PINNED_BASE_URL=https://env-loses.example/v1\n"
        "OPENAI_BASE_URL=https://relay.example/v1\n"
        "OPENAI_API_KEY=sk-not-a-real-key\n", encoding="utf-8")
    # alpha：自己的 config 声明了 pinned（与根配置不同的地址）+ 自己的 .env 覆盖 openai（多 profile 隔离）
    (sandbox / "profiles" / "alpha" / "config.yaml").write_text(
        "model:\n  default: m1\n"
        "providers:\n"
        "  pinned:\n"
        "    base_url: https://alpha-config-wins.example/v1\n", encoding="utf-8")
    (sandbox / "profiles" / "alpha" / ".env").write_text(
        "OPENAI_BASE_URL=https://alpha-relay.example/v1\n", encoding="utf-8")
    # beta：没有自己的 .env；它的 OPENAI_BASE_URL 只来自**根 .env**（用于证明根 .env 也是有效来源）
    (sandbox / "profiles" / "beta" / "config.yaml").write_text("model:\n  default: m2\n", encoding="utf-8")
    # gamma：env 地址**带尾部斜杠**、且不以 /v1 结尾（anthropic 形态）
    (sandbox / "profiles" / "gamma" / "config.yaml").write_text("model:\n  default: m3\n", encoding="utf-8")
    (sandbox / "profiles" / "gamma" / ".env").write_text(
        "ANTHROPIC_BASE_URL=https://gamma-relay.example\n", encoding="utf-8")

    api_models._home = lambda: sandbox
    api_models._LIVE_CACHE.clear()

    print("\n[G] registry 结构带 base_url_env_var")
    reg = api_models._builtin_registry()
    expect_env = {"openai-api": "OPENAI_BASE_URL", "anthropic": "ANTHROPIC_BASE_URL",
                  "xai": "XAI_BASE_URL", "deepseek": "DEEPSEEK_BASE_URL",
                  "minimax": "MINIMAX_BASE_URL", "gemini": "GEMINI_BASE_URL"}
    for name, var in expect_env.items():
        entry = reg.get(name) or {}
        check("registry[%s].base_url_env_var == %r" % (name, var),
              entry.get("base_url_env_var") == var, repr(entry.get("base_url_env_var")))
    check("registry 条目只放变量名、没有任何取值泄漏（值里不含 '://'）",
          all("://" not in str((reg.get(n) or {}).get("base_url_env_var") or "") for n in expect_env))

    print("\n[A] 无 env ⇒ builtin 兜底仍生效（beta 自己没 .env；再临时撤掉根 .env 的该变量）")
    root_env = sandbox / ".env"
    root_env_text = root_env.read_text(encoding="utf-8")
    root_env.write_text("PINNED_BASE_URL=https://env-loses.example/v1\n", encoding="utf-8")
    try:
        ep_beta = api_models._provider_endpoint("beta", "openai-api")
    finally:
        root_env.write_text(root_env_text, encoding="utf-8")
    url_beta = api_models._models_endpoint_url(ep_beta["base_url"], "openai-api")
    check("openai-api(beta, 无 env) base_url == https://api.openai.com/v1",
          ep_beta["base_url"] == "https://api.openai.com/v1", repr(ep_beta["base_url"]))
    check("openai-api(beta) base_url_source == builtin",
          ep_beta["base_url_source"] == "builtin", repr(ep_beta["base_url_source"]))
    check("openai-api(beta) 请求 URL == https://api.openai.com/v1/models",
          url_beta == "https://api.openai.com/v1/models", url_beta)
    check("openai-api(beta) 没崩、没空串", bool(ep_beta["base_url"]), "")

    print("\n[B] config 显式 base_url 压住 env（防错位）")
    for agent in ("default", "alpha"):
        ep = api_models._provider_endpoint(agent, "pinned")
        expect = ("https://config-wins.example/v1" if agent == "default"
                  else "https://alpha-config-wins.example/v1")
        check("pinned(%s) base_url 取 config 的（env 里是另一个值）" % agent,
              ep["base_url"] == expect, repr(ep["base_url"]))
        check("pinned(%s) base_url_source == config" % agent,
              ep["base_url_source"] == "config", repr(ep["base_url_source"]))
    envmap = api_models._env_map_for("default")
    check("同一视角下 env 里确实有 PINNED_BASE_URL（证明上面不是 env 空着蒙对的）",
          envmap.get("PINNED_BASE_URL") == "https://env-loses.example/v1",
          repr(envmap.get("PINNED_BASE_URL")))

    print("\n[C] 边界值：三种写法都不得产生 /v1/v1/")
    for base in ("https://relay.example/v1", "https://relay.example", "https://relay.example/v1/"):
        got = api_models._models_endpoint_url(base, "openai-api")
        check("_models_endpoint_url(%r) ⇒ https://relay.example/v1/models" % base,
              got == "https://relay.example/v1/models", got)
        check("  ↳ 不含 /v1/v1/", "/v1/v1/" not in got, got)
    for base in ("https://relay.example", "https://relay.example/"):
        got = api_models._models_endpoint_url(base, "anthropic")
        check("anthropic 走中转（host 不含 anthropic）_models_endpoint_url(%r) ⇒ .../v1/models" % base,
              got == "https://relay.example/v1/models", got)

    print("\n[D] 中转站地址推出的 URL 精确匹配")
    ep_root = api_models._provider_endpoint("default", "openai-api")
    url_root = api_models._models_endpoint_url(ep_root["base_url"], "openai-api")
    check("openai-api(default) base_url == https://relay.example/v1",
          ep_root["base_url"] == "https://relay.example/v1", repr(ep_root["base_url"]))
    check("openai-api(default) base_url_source == env", ep_root["base_url_source"] == "env", "")
    check("openai-api(default) URL == https://relay.example/v1/models",
          url_root == "https://relay.example/v1/models", url_root)
    check("openai-api(default) URL 里不含 api.openai.com", "api.openai.com" not in url_root, url_root)

    print("\n[E] env 与 config 都没有 ⇒ builtin，且不得静默空值")
    ep_min = api_models._provider_endpoint("beta", "minimax")
    check("minimax(beta, 无 env) base_url_source == builtin", ep_min["base_url_source"] == "builtin", "")
    check("minimax(beta) base_url 非空", bool(ep_min["base_url"]), repr(ep_min["base_url"]))
    check("minimax(beta) base_url == 内置 https://api.minimax.io/anthropic",
          ep_min["base_url"] == "https://api.minimax.io/anthropic", repr(ep_min["base_url"]))
    live_min = api_models._live_models("beta", "minimax", force=True)
    check("minimax(beta) 探测给出明确失败原因（不是静默空 ok=True）",
          live_min["ok"] is False and bool(live_min["error_kind"]),
          "kind=%s ok=%s" % (live_min["error_kind"], live_min["ok"]))
    check("minimax(beta) 探测返回里带 base_url_source",
          live_min.get("base_url_source") in ("config", "env", "builtin"),
          repr(live_min.get("base_url_source")))

    print("\n[F] 多 profile 隔离（各自的 .env 各读到自己的；根 .env 是共同底层）")
    ep_alpha = api_models._provider_endpoint("alpha", "openai-api")
    ep_beta2 = api_models._provider_endpoint("beta", "openai-api")
    check("alpha 自己的 .env 压住根 .env：https://alpha-relay.example/v1",
          ep_alpha["base_url"] == "https://alpha-relay.example/v1", repr(ep_alpha["base_url"]))
    check("alpha base_url_source == env", ep_alpha["base_url_source"] == "env", "")
    check("beta（自己没有 .env）取根 .env 的值，且**不是** alpha 那份",
          ep_beta2["base_url"] == "https://relay.example/v1" and ep_beta2["base_url"] != ep_alpha["base_url"],
          "%s / %s" % (ep_beta2["base_url"], ep_beta2["base_url_source"]))
    ep_gamma = api_models._provider_endpoint("gamma", "anthropic")
    check("gamma 的 ANTHROPIC_BASE_URL（无 /v1）⇒ base=https://gamma-relay.example",
          ep_gamma["base_url"] == "https://gamma-relay.example", repr(ep_gamma["base_url"]))
    check("gamma anthropic 请求 URL ⇒ https://gamma-relay.example/v1/models",
          api_models._models_endpoint_url(ep_gamma["base_url"], "anthropic") == "https://gamma-relay.example/v1/models",
          api_models._models_endpoint_url(ep_gamma["base_url"], "anthropic"))

    print("\n[H] _model_list 的响应体也带来源标记")
    body = api_models._model_list("default", "openai-api", live=False)
    check("_model_list(default, openai-api).base_url_source == env",
          body.get("base_url_source") == "env", repr(body.get("base_url_source")))
    check("_model_list(...).base_url == https://relay.example/v1",
          body.get("base_url") == "https://relay.example/v1", repr(body.get("base_url")))

finally:
    os.environ.update(SAVED_ENV)
    shutil.rmtree(sandbox, ignore_errors=True)

print("\n%d PASS / %d FAIL" % (passed, len(failures)))
for item in failures:
    print("  -", item)
sys.exit(0 if not failures else 1)

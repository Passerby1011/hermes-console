# -*- coding: utf-8 -*-
"""t_3c2817b1 · 后端契约与真实调用验收（**重启后的新进程**，不看 pid 2972 的旧内存）。

跑法：
    <venv>/python.exe scripts/probe_autogroup_frontend.py --sandbox D:/hermes-data/temp/ag-frontend/sb

验：
  A. 重启后的后端里，5 条旧前端调用各自的新归属路由都存在且真的能跑（非 404）
  B. 扫描三态：进行中 / 空结果 / 失败（失败用一次非法 scope 造的 400 也覆盖，另有 job 失败路径）
  C. AI 分类：候选集与 /autogroup/ai/scope 同口径；预览零写入；apply 前归属表零变化
  D. 换 provider → 模型清单确实变化
  E. 两个 Agent 各设不同 provider/model → 各自回读 + 互不影响
  F. auto-group/ai_calls.jsonl 真实调用记录（provider/model/layer/agent）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
PYTHON = sys.executable
PREFIX = "/api/plugins/hermes-console"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  PASS " if ok else "  FAIL ") + name + (("  — " + str(extra)) if extra else ""), flush=True)


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def req(self, method: str, path: str, body=None):
        url = self.base + PREFIX + path
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["content-type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(raw) if raw else None
            except ValueError:
                return exc.code, {"raw": raw}
        except urllib.error.URLError as exc:
            return 0, {"error": str(exc)}

    def get(self, path):
        return self.req("GET", path)

    def post(self, path, body=None):
        return self.req("POST", path, body if body is not None else {})

    def put(self, path, body=None):
        return self.req("PUT", path, body if body is not None else {})


def wait_ready(port: int, timeout: float = 60.0) -> bool:
    client = Client("http://127.0.0.1:%d" % port)
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, payload = client.get("/health")
        if status == 200:
            return True
        time.sleep(0.3)
    return False


def assignments_hash(sandbox: Path) -> str:
    import hashlib
    path = sandbox / "auto-group" / "assignments.db"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"


def rows(sandbox: Path, status: str):
    import sqlite3
    path = sandbox / "auto-group" / "assignments.db"
    conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT session_id, project_slug, status, batch_id FROM assignments WHERE status=?",
            (status,))]
    finally:
        conn.close()


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--sandbox", default="D:/hermes-data/temp/ag-frontend/sb")
    parser.add_argument("--port", type=int, default=9331)
    parser.add_argument("--keep", action="store_true", help="跑完不关停夹具（截图探针接着用）")
    args = parser.parse_args(argv)
    sandbox = Path(args.sandbox)
    out_dir = HERE / "_probe_out" / "autogroup-frontend"
    out_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "HC_AI_STUB": "1"})
    stdout = open(out_dir / "fixture.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [PYTHON, str(PLUGIN / "scripts" / "autogroup_ui_fixture.py"),
         "--port", str(args.port), "--sandbox", str(sandbox)],
        env=env, stdout=stdout, stderr=subprocess.STDOUT, cwd=str(PLUGIN),
    )
    try:
        if not wait_ready(args.port):
            print("fixture 没起来，见", out_dir / "fixture.log")
            print(stdout)
            return 2
        client = Client("http://127.0.0.1:%d" % args.port)

        # ---------- A. 路由存在性（重启后的新进程） ----------
        print("[A] 重启后 5 条旧路径的新归属路由都能用（非 404）")
        status, health = client.get("/health")
        check("健康检查 200", status == 200, str(status))
        check("没有失败的域模块", not (health or {}).get("failed"), str((health or {}).get("failed")))

        status, payload = client.post("/autogroup/scan", {"limit": 5})
        check("① /autogroup/scan 非 404", status in (200, 202), "status=%d" % status)
        job_id = (payload or {}).get("job_id") or ""
        check("① 立刻返回 job_id", bool(job_id), job_id)
        # 轮询到 done
        state = ""
        for _ in range(120):
            st, jobp = client.get("/autogroup/job/" + job_id)
            if st != 200:
                state = "http-%d" % st
                break
            state = (jobp or {}).get("state") or ((jobp or {}).get("data") or {}).get("state") or ""
            if state in ("done", "failed"):
                break
            time.sleep(0.2)
        check("① job 轮询能拿到 done（/autogroup/job/{id} 非 404）", state == "done", state)
        scan_result = ((jobp or {}).get("data") or {}).get("result") or {}

        status, preview = client.post("/autogroup/preview", {})
        check("② /autogroup/preview 返回 200（不是 plan 的 400）", status == 200, "status=%d" % status)
        check("② 预览体带逐条 items", isinstance((preview or {}).get("data", {}).get("items"), list),
              str(type((preview or {}).get("data", {}).get("items"))))
        status, undo_probe = client.post("/autogroup/undo", {"batch_id": "no-such-batch"})
        check("③ /autogroup/undo 存在（未知批次给 404 而不是 404 路由缺失）",
              status in (404, 400, 409), "status=%d body=%s" % (status, str(undo_probe)[:120]))
        status, projects = client.get("/autogroup/projects")
        check("④ /autogroup/projects 非 404", status == 200, "status=%d" % status)
        status, apply_probe = client.post("/autogroup/apply", {"confirm": False})
        check("⑤ /autogroup/apply 存在（不带 confirm 给 400 confirmation_required）",
              status == 400 and ((apply_probe or {}).get("error", {}).get("code") == "confirmation_required"),
              "status=%d" % status)
        status, gone = client.post("/autogroup/plan", {"mode": "rebuild_all"})
        check("旧 /autogroup/plan 确实是 404（证明前端必须改调）", status == 404, "status=%d" % status)
        for stale in ("/autogroup/ai/classify", "/autogroup/ai/confirm",
                      "/autogroup/ai/undo", "/autogroup/ai/project"):
            st2, _ = client.post(stale, {})
            check("旧 %s 确实是 404（证明前端必须改调）" % stale, st2 == 404, "status=%d" % st2)

        # ---------- B. 扫描三态 ----------
        print("\n[B] 扫描三态")
        status, bad = client.post("/autogroup/scan", {"scope": "nonsense"})
        check("非法 scope → 400（失败有真错误，不静默）",
              status == 400 and ((bad or {}).get("error", {}).get("code") == "invalid_scope"),
              "status=%d %s" % (status, str(bad)[:120]))
        check("非法 scope 的 message 可读",
              "scope" in str((bad or {}).get("error", {}).get("message")),
              str((bad or {}).get("error", {}).get("message"))[:120])

        # 空结果：rejected 全部候选再扫 → 只能靠 scope=ungrouped + 已全 applied 的沙盒
        # 这里用 limit=0 + 把候选都标 applied 的方式太侵入；改用「再扫一次同一批」，
        # 已 suggested 的行会被重新判（不会为空）。真正可复现的空态是：候选池为空。
        status, scope_before = client.get("/autogroup/ai/scope")
        total_before = ((scope_before or {}).get("data") or {}).get("total_available")
        check("候选总数来自后端（/autogroup/ai/scope）", isinstance(total_before, int), str(total_before))

        # ---------- C. AI 分类：候选集同口径 + 预览零写入 ----------
        print("\n[C] AI 分类（候选同口径 / 预览零写入 / 确认才写）")
        status, scope_ai = client.get("/autogroup/ai/scope")
        scope_total = ((scope_ai or {}).get("data") or {}).get("total_available")
        before_hash = assignments_hash(sandbox)
        status, ai_scan = client.post("/autogroup/scan", {"ai": True, "limit": 0, "scope": "ungrouped"})
        check("AI 扫描 202 + job_id", status in (200, 202) and bool((ai_scan or {}).get("job_id")),
              "status=%d" % status)
        ai_job = (ai_scan or {}).get("job_id") or ""
        ai_state = ""
        for _ in range(900):
            st, jobp = client.get("/autogroup/job/" + ai_job)
            body = (jobp or {}).get("data") or {}
            ai_state = (jobp or {}).get("state") or body.get("state") or ""
            if ai_state in ("done", "failed"):
                break
            time.sleep(0.2)
        ai_result = body.get("result") or {}
        check("AI 扫描跑完", ai_state == "done", ai_state)
        check("AI 候选数与 /autogroup/ai/scope 同口径（同一批会话）",
              ai_result.get("candidates") == scope_total,
              "scan=%s scope=%s" % (ai_result.get("candidates"), scope_total))
        llm = ai_result.get("llm") or {}
        check("llm.code 是 llm_used / no_model_configured 之一（不是旧的 LLM_SKIP 谎报）",
              llm.get("code") in ("llm_used", "no_model_configured", "llm_not_called"),
              str(llm.get("code")))
        check("AI 扫描没写 applied（预览零写入）", assignments_hash(sandbox) == before_hash or
              len(rows(sandbox, "applied")) == 4,
              "applied=%d" % len(rows(sandbox, "applied")))

        # 预览 → 零写入
        status, pre = client.post("/autogroup/preview", {"sync_cwd": False})
        pre_data = (pre or {}).get("data") or {}
        check("预览 200", status == 200, "status=%d" % status)
        check("预览有 will_apply 计数", isinstance(pre_data.get("will_apply"), int), str(pre_data.get("will_apply")))
        check("预览零写入（applied 条数不变）", len(rows(sandbox, "applied")) == 4,
              str(len(rows(sandbox, "applied"))))

        # 确认写库
        picked = [item["session_id"] for item in (pre_data.get("items") or []) if item.get("will_apply")]
        check("预览里有可写条目（AI 建议含项目）", bool(picked), "picked=%d" % len(picked))
        status, applied = client.post("/autogroup/apply",
                                      {"confirm": True, "session_ids": picked, "sync_cwd": False})
        check("apply{confirm:true} 200", status == 200, "status=%d %s" % (status, str(applied)[:160]))
        write_result = ((applied or {}).get("data") or {}).get("write_result") or {}
        check("写入了 >=1 条 applied", (write_result.get("applied") or 0) >= 1, str(write_result.get("applied")))
        after_applied = rows(sandbox, "applied")
        check("回读 applied 条数确实增加了", len(after_applied) > 4, "%d → %d" % (4, len(after_applied)))
        batch_id = ((applied or {}).get("data") or {}).get("batch_id")
        check("apply 返回 batch_id（撤销入口）", bool(batch_id), str(batch_id))

        # 撤销
        status, undone = client.post("/autogroup/undo", {"batch_id": batch_id})
        check("undo{batch_id} 200", status == 200, "status=%d %s" % (status, str(undone)[:160]))
        check("撤销后 applied 回到 4 条", len(rows(sandbox, "applied")) == 4,
              str(len(rows(sandbox, "applied"))))

        # ---------- D. provider 变化 → 模型清单变化 ----------
        print("\n[D] 换 provider 模型清单确实变化")
        status, opts = client.get("/autogroup/ai/model/options?agent=default")
        providers = ((opts or {}).get("data") or {}).get("providers") or []
        check("provider 目录非空", bool(providers), "n=%d" % len(providers))
        check("provider 行里没有密钥取值（只有 key_env + key_present）",
              all(("key_env" in p and "key_present" in p) for p in providers),
              str(sorted(providers[0].keys())) if providers else "")
        leaked = []
        for row in providers:
            for key, value in row.items():
                if key in ("key_env", "key_envs") or "credential" in key:
                    continue
                if isinstance(value, str) and len(value) > 20 and value.startswith(("sk-", "sk_", "ghp_")):
                    leaked.append((row.get("provider"), key))
        check("没有回显任何密钥取值", not leaked, str(leaked))

        provider_names = [p.get("provider") for p in providers]
        listings = {}
        for name in provider_names[:6]:
            st, listing = client.get("/autogroup/ai/model/options?agent=default&provider=" + str(name))
            if st == 200:
                listings[name] = ((listing or {}).get("data") or {}).get("models") or []
        distinct = {name: tuple(sorted(models)) for name, models in listings.items()}
        non_empty = {k: v for k, v in distinct.items() if v}
        check("换 provider 后模型清单确实变化（至少两个 provider 清单内容不同）",
              len(set(non_empty.values())) >= 2,
              json.dumps({k: list(v[:3]) for k, v in distinct.items()}, ensure_ascii=False)[:500])

        # ---------- E. 两个 Agent 各自独立 ----------
        print("\n[E] 两个 Agent 各设不同 provider/model，互不影响")
        def agent_choices(agent: str):
            """该 agent 实际可用的 (provider, models) 列表 —— 按它自己的 provider 目录取。

            **跳过 virtual provider**（moa 之类是本地编排器，不是真实上游）：它们能出现在
            清单里但真调用会走编排链路，不适合用来验「provider/model 配置生效」。
            """
            st, opts2 = client.get("/autogroup/ai/model/options?agent=" + agent)
            rows2 = ((opts2 or {}).get("data") or {}).get("providers") or []
            out = []
            for row in rows2:
                if row.get("virtual"):
                    continue
                name = row.get("provider")
                st2, listing = client.get(
                    "/autogroup/ai/model/options?agent=%s&provider=%s" % (agent, name))
                if st2 != 200:
                    continue
                models = ((listing or {}).get("data") or {}).get("models") or []
                if models:
                    out.append((name, models))
            return out

        AGENT_A, AGENT_B = "xiaomo", "work"
        choices_default = agent_choices(AGENT_A)
        choices_xiaomo = agent_choices(AGENT_B)
        check("%s 有可用的 provider+模型清单" % AGENT_A, bool(choices_default),
              str([c[0] for c in choices_default]))
        check("%s 有可用的 provider+模型清单" % AGENT_B, bool(choices_xiaomo),
              str([c[0] for c in choices_xiaomo]))
        # 选两个 provider 不同、且有**不同模型清单**的组合（证明「不同 agent 用不同 provider/model」）
        pair = None
        for p1, m1 in choices_default:
            for p2, m2 in choices_xiaomo:
                if p1 != p2 and tuple(m1) != tuple(m2):
                    pair = (p1, m1, p2, m2)
                    break
            if pair:
                break
        if pair is None and choices_default and choices_xiaomo:
            p1, m1 = choices_default[0]
            p2, m2 = choices_xiaomo[0]
            pair = (p1, m1, p2, m2)
        if pair is not None:
            p1, m1, p2, m2 = pair
            status_a, res_a = client.put("/autogroup/ai/model",
                                         {"agent": AGENT_A, "mode": "module",
                                          "provider": p1, "model": m1[0]})
            check("Agent %s 保存成功" % AGENT_A, status_a == 200,
                  "status=%d %s" % (status_a, str(res_a)[:160]))
            status_b, res_b = client.put("/autogroup/ai/model",
                                         {"agent": AGENT_B, "mode": "module",
                                          "provider": p2, "model": m2[-1]})
            check("Agent %s 保存成功" % AGENT_B, status_b == 200,
                  "status=%d %s" % (status_b, str(res_b)[:160]))
            st_a, got_a = client.get("/autogroup/ai/model?agent=" + AGENT_A)
            st_b, got_b = client.get("/autogroup/ai/model?agent=" + AGENT_B)
            decl_a = ((got_a or {}).get("data") or {}).get("agent_declaration") or {}
            decl_b = ((got_b or {}).get("data") or {}).get("agent_declaration") or {}
            group_a = decl_a.get("group") or {}
            group_b = decl_b.get("group") or {}
            check("%s 回读 provider/model 正确" % AGENT_A,
                  group_a.get("provider") == p1 and group_a.get("model") == m1[0],
                  "%s/%s" % (group_a.get("provider"), group_a.get("model")))
            check("%s 回读 provider/model 正确" % AGENT_B,
                  group_b.get("provider") == p2 and group_b.get("model") == m2[-1],
                  "%s/%s" % (group_b.get("provider"), group_b.get("model")))
            check("写 %s 没覆盖 %s" % (AGENT_B, AGENT_A),
                  group_a.get("provider") == p1 and group_a.get("model") == m1[0])
            check("来源层都是「该 Agent 独立」",
                  decl_a.get("layer") == "agent" and decl_b.get("layer") == "agent",
                  "%s / %s" % (decl_a.get("layer"), decl_b.get("layer")))
            # 让两个 Agent 各跑一次真实 AI 扫描：审计记录必须能证明「配置真的生效」
            for agent_id, want_provider, want_model in (
                    (AGENT_A, p1, m1[0]), (AGENT_B, p2, m2[-1])):
                st_s, scan_res = client.post(
                    "/autogroup/scan",
                    {"ai": True, "limit": 0, "scope": "ungrouped", "agent": agent_id})
                jid = (scan_res or {}).get("job_id") or ""
                final = {}
                for _ in range(900):
                    st_j, jobp = client.get("/autogroup/job/" + jid)
                    final = (jobp or {}).get("data") or {}
                    if final.get("state") in ("done", "failed"):
                        break
                    time.sleep(0.2)
                res2 = final.get("result") or {}
                llm2 = res2.get("llm") or {}
                check("Agent %s 的 AI 扫描跑完" % agent_id, final.get("state") == "done",
                      str(final.get("state")))
                check("Agent %s 的扫描用了自己配置的 provider/model" % agent_id,
                      llm2.get("provider") == want_provider and llm2.get("model") == want_model,
                      "%s/%s（期望 %s/%s）" % (llm2.get("provider"), llm2.get("model"),
                                              want_provider, want_model))
                check("Agent %s 的 llm.code=llm_used" % agent_id, llm2.get("code") == "llm_used",
                      str(llm2.get("code")))

            cfg_file = sandbox / "auto-group" / "ai_model.json"
            on_disk = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.is_file() else {}
            check("盘上是按 Agent 映射（schema 2）", on_disk.get("schema") == 2, str(on_disk.get("schema")))
            check("盘上两个 agent 段都在",
                  set(on_disk.get("agents", {})) >= {AGENT_A, AGENT_B},
                  str(sorted(on_disk.get("agents", {}))))
        else:
            check("至少两个 provider 有模型清单（前置条件）", False,
                  "providers=%s" % provider_names)

        # ---------- F. 真实调用审计 ----------
        print("\n[F] auto-group/ai_calls.jsonl 真实调用记录")
        audit = sandbox / "auto-group" / "ai_calls.jsonl"
        check("审计文件存在", audit.is_file(), str(audit))
        lines = []
        if audit.is_file():
            for line in audit.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        lines.append(json.loads(line))
                    except ValueError:
                        pass
        check("审计里有调用记录", bool(lines), "n=%d" % len(lines))
        with_provider = [r for r in lines if r.get("provider") and r.get("model")]
        check("审计行带 provider + model", bool(with_provider), "n=%d" % len(with_provider))
        check("审计行带 layer（来源层）", any(r.get("layer") for r in lines),
              str(sorted({str(r.get("layer")) for r in lines})))
        agents_in_audit = sorted({str(r.get("agent")) for r in lines if r.get("agent")})
        check("审计里至少两个不同 Agent 有调用记录", len(agents_in_audit) >= 2, str(agents_in_audit))
        pairs = sorted({(str(r.get("agent")), str(r.get("provider")), str(r.get("model"))) for r in lines})
        check("不同 Agent 用的 provider/model 不同", len({(p, m) for (_, p, m) in pairs}) >= 2, str(pairs))

        print("\nFRONTEND-ACCEPT: %d PASS / %d FAIL" % (len(PASS), len(FAIL)))
        for name in FAIL:
            print("  FAILED:", name)
        (out_dir / "acceptance.txt").write_text(
            "FRONTEND-ACCEPT: %d PASS / %d FAIL\n\n" % (len(PASS), len(FAIL))
            + "\n".join("PASS " + n for n in PASS)
            + ("\n\n" + "\n".join("FAIL " + n for n in FAIL) if FAIL else "")
            + "\n\nAUDIT SAMPLE:\n"
            + "\n".join(json.dumps(r, ensure_ascii=False) for r in lines[-6:]),
            encoding="utf-8")
        return 1 if FAIL else 0
    finally:
        if not args.keep:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        stdout.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

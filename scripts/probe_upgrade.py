# -*- coding: utf-8 -*-
"""hermes-console · V2 升级分区（6 路由）验收探针（唯一验收依据）。

跑法（**不要经管道拿退出码**）::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        plugins/hermes-console/scripts/probe_upgrade.py
    echo "exit=$?"        # bash；PowerShell 用 $LASTEXITCODE

覆盖卡片 7 条::

    [1] 6 条路由全部注册      GET /upgrade/scan · GET /upgrade/readiness · POST /upgrade/prepare
                              POST /upgrade/restore · POST /upgrade/wait · POST /upgrade/kill
                              （占位的 GET /upgrade 必须已经消失：42 - 1 + 6 = 47，V3 上下文引擎再 +6 = 53，V4 远端后端 +5 = 58，V5 模型 +13 = 71）
    [2] ★scan 真实可用       GET /upgrade/scan → 200，含版本字段（version.version）与就绪字段
                              （readiness.ready / blockers / cron_active），用真实 home **只读**
    [3] ★★状态文件路径       读到的必须是 <home>/plugins/upgrade-console/state.json：
                              不是 profiles/<p>/plugins/...、不是 cwd 相对路径；
                              而且**不受 HERMES_HOME 环境变量影响**（worker 里它是 profile 语义）
    [4] ★★prepare 失败回滚   假 home + 写不进去的目录 → 503 结构化错误（code=state_unwritable），
                              本次暂停逐条被 resume 回来（rolled_back 全中、still_paused 为空），
                              快照文件没有落盘
    [5] 业务错误不是 500     /restore 无待恢复快照 → 409；/kill 空 pids → 400、
                              不存在的 pid → 409（全部结构化，不是 500 栈）
    [6] 快照往返（假 home）  prepare 成功 → 重复 prepare 被 409 拒绝 → restore 只恢复快照内任务
                              → 再次 restore 报 409；state.json 的 restored 字段真的翻转
    [7] ★生产零污染          真实 config.yaml + profiles/*/config.yaml + upgrade-console/state.json
                              开头/结尾 sha256 全一致（并打印前后值）

外加：``/health`` 的 ``failed`` 为空、路由总数 = 58（V2 收尾 47 + V3 上下文引擎 6 + V4 远端后端 5）。

**本探针不跑真升级**：真实 home 上只做 GET（scan/readiness）；所有写动作（prepare/restore/kill）
只在假 home + 假 cron 上执行，绝不碰真实 cron 状态与真实 state.json。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]      # <home>/plugins/hermes-console
DASHBOARD = PLUGIN_ROOT / "dashboard"
REAL_HOME = Path(__file__).resolve().parents[3]        # <home>
HERMES_AGENT = REAL_HOME / "hermes-agent"
PREFIX = "/api/plugins/hermes-console"

#: 本卡必须挂上的 6 条路由（方法 + 路径，路径相对 PREFIX）。
EXPECTED_ROUTES = (
    ("GET", "/upgrade/scan"),
    ("GET", "/upgrade/readiness"),
    ("POST", "/upgrade/prepare"),
    ("POST", "/upgrade/restore"),
    ("POST", "/upgrade/wait"),
    ("POST", "/upgrade/kill"),
)

#: 期望路由总数：V1 收尾时 42，本卡「去掉 1 条占位 + 新增 6 条」= 47，V3 上下文引擎再 +6 = 53。
EXPECTED_ROUTE_TOTAL = 71

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  PASS  " if ok else "  FAIL  ") + label + (" — " + detail if detail else ""))
    if not ok:
        failures.append(label)
    return ok


def sha256_of(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "<missing>"
    except OSError as exc:  # noqa: BLE001 - 探针要的是"能不能比"，不是精确错误
        return "<error:%s>" % exc


def brief(value, n: int = 300) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    return text if len(text) <= n else text[:n] + "…"


# ---------------------------------------------------------------------------
print("[0] 环境")
print("        python   :", sys.version.split()[0])
print("        plugin   :", PLUGIN_ROOT)
print("        real home:", REAL_HOME)

if not (DASHBOARD / "plugin_api.py").is_file():
    print("  FAIL  插件根不对：找不到", DASHBOARD / "plugin_api.py")
    raise SystemExit(2)

# 与生产同源：dashboard 从 hermes-agent 起，概览/cron 取数要 import hermes_cli.*
for extra in (str(DASHBOARD), str(HERMES_AGENT)):
    if extra not in sys.path:
        sys.path.insert(0, extra)


# ---------------------------------------------------------------------------
# [7-前置] 生产零污染基线：所有**可能被写**的文件先算一遍 sha256
# ---------------------------------------------------------------------------
def production_fingerprint() -> dict[str, str]:
    """真实 home 里本卡可能碰到的配置文件与状态文件。"""
    paths = [REAL_HOME / "config.yaml"]
    profiles = REAL_HOME / "profiles"
    if profiles.is_dir():
        paths.extend(sorted(profiles.glob("*/config.yaml")))
    upgrade_dir = REAL_HOME / "plugins" / "upgrade-console"
    paths.extend([upgrade_dir / "state.json", upgrade_dir / "state.json.bak-20260928-184737"])
    return {str(p): sha256_of(p) for p in paths}


print("\n[7-前置] 生产指纹（开头）")
before = production_fingerprint()
for path, digest in before.items():
    print("        %s  %s" % (digest[:16], path.replace("\\", "/")))

# ---------------------------------------------------------------------------
print("\n[1] 按 dashboard 的方式加载 plugin_api，检查 6 条路由")
spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_hermes_console",
                                             DASHBOARD / "plugin_api.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

print("        failed  :", brief(mod.DOMAIN_FAILURES, 300))
check("/health 口径：没有域模块加载失败", mod.DOMAIN_FAILURES == {}, brief(mod.DOMAIN_FAILURES, 300))
check("api_upgrade 在已加载清单里", "api_upgrade" in mod.DOMAIN_LOADED, str(mod.DOMAIN_LOADED))

registered = set()
for route in mod.router.routes:
    for method in sorted(getattr(route, "methods", []) or []):
        registered.add((method, route.path))

for method, path in EXPECTED_ROUTES:
    check("路由已注册 %-5s %s" % (method, path), (method, path) in registered,
          "" if (method, path) in registered else "缺这一条")

check("占位路由 GET /upgrade 已消失（本卡用 6 条真实路由替换它）",
      ("GET", "/upgrade") not in registered)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app = FastAPI()
app.include_router(mod.router, prefix=PREFIX)
client = TestClient(app, raise_server_exceptions=False)

import api_upgrade  # noqa: E402  (plugin_api 已按本目录 import 过同名模块)

health = client.get(PREFIX + "/health").json()
print("        /health :", brief({k: health.get(k) for k in ("ok", "failed", "route_count")}, 240))
check("路由总数 = %d（47 + V3 上下文引擎 6 条）" % EXPECTED_ROUTE_TOTAL,
      health.get("route_count") == EXPECTED_ROUTE_TOTAL, str(health.get("route_count")))
check("/health 的 failed 为空", health.get("failed") == {}, brief(health.get("failed")))

# ---------------------------------------------------------------------------
print("\n[3] ★★状态文件路径断言（必须是 plugins/upgrade-console/state.json）")
expected_state = REAL_HOME / "plugins" / "upgrade-console" / "state.json"
print("        STATE_RELATIVE :", api_upgrade.STATE_RELATIVE)
print("        state_file()   :", api_upgrade.state_file())
print("        期望            :", expected_state)

check("STATE_RELATIVE == plugins/upgrade-console/state.json",
      api_upgrade.STATE_RELATIVE == Path("plugins") / "upgrade-console" / "state.json",
      str(api_upgrade.STATE_RELATIVE))
check("hermes_home() 解析到真实主目录", Path(api_upgrade.hermes_home()) == REAL_HOME,
      str(api_upgrade.hermes_home()))
check("state_file() 是绝对路径且指向 <home>/plugins/upgrade-console/state.json",
      Path(api_upgrade.state_file()) == expected_state, str(api_upgrade.state_file()))
check("state_file() 不是相对 cwd 的路径", Path(api_upgrade.state_file()).is_absolute(),
      str(api_upgrade.state_file()))
check("state_file() 不在 profiles/<p>/ 之下（worker 里的 HERMES_HOME 陷阱）",
      "profiles" not in Path(api_upgrade.state_file()).parts,
      str(api_upgrade.state_file()))

# 环境变量陷阱：worker/gateway 进程里 HERMES_HOME=<home>/profiles/<p>。
# 解析结果必须不受它影响，否则恢复时会去一个空路径找快照。
_saved_env = os.environ.get("HERMES_HOME")
try:
    os.environ["HERMES_HOME"] = str(REAL_HOME / "profiles" / "code-worker")
    after_env = Path(api_upgrade.state_file())
finally:
    if _saved_env is None:
        os.environ.pop("HERMES_HOME", None)
    else:
        os.environ["HERMES_HOME"] = _saved_env
check("HERMES_HOME 被设成 profiles/<p> 时，state_file() 仍然不变",
      after_env == expected_state, str(after_env))

check("历史状态文件真实存在（原地沿用，不搬迁不重建）", expected_state.is_file(), str(expected_state))
if expected_state.is_file():
    raw_state = json.loads(expected_state.read_text(encoding="utf-8"))
    print("        state keys:", sorted(raw_state.keys()), "| jobs:", len(raw_state.get("jobs") or []),
          "| restored:", raw_state.get("restored"))
    check("历史状态能被解析成 dict 且含 jobs / restored",
          isinstance(raw_state, dict) and "jobs" in raw_state and "restored" in raw_state,
          brief(raw_state, 160))
    check("历史快照里的 job 结构完整（job_id / was_active）",
          all(isinstance(j, dict) and j.get("job_id") for j in (raw_state.get("jobs") or [])),
          str(len(raw_state.get("jobs") or [])))

# ---------------------------------------------------------------------------
print("\n[2] ★GET /upgrade/scan（真实 home，只读）")
resp = client.get(PREFIX + "/upgrade/scan")
body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
print("        status:", resp.status_code, "| body:", brief(body, 400))
check("HTTP 200", resp.status_code == 200, str(resp.status_code))
check("ok=true", body.get("ok") is True, brief(body.get("ok")))
scan = body.get("data") or {}
check("data 含 procs / state_file / version / readiness",
      all(k in scan for k in ("procs", "state_file", "version", "readiness")),
      str(sorted(scan.keys())))
check("data.state_file 就是 plugins/upgrade-console/state.json",
      Path(scan.get("state_file") or "") == expected_state, str(scan.get("state_file")))

version = scan.get("version") or {}
print("        version :", brief(version, 200))
check("版本字段非空（version.version 是真版本号，不是空串）",
      isinstance(version.get("version"), str) and bool(version.get("version"))
      and version.get("version") != "unknown",
      str(version.get("version")))
check("版本字段带来源（source）", bool(version.get("source")), str(version.get("source")))

readiness = scan.get("readiness") or {}
print("        readiness:", brief(readiness, 400))
check("就绪字段 ready 是布尔值", isinstance(readiness.get("ready"), bool), str(readiness.get("ready")))
check("就绪字段 blockers 是列表", isinstance(readiness.get("blockers"), list),
      str(type(readiness.get("blockers")).__name__))
check("就绪字段 cron_active 是整数", isinstance(readiness.get("cron_active"), int),
      str(readiness.get("cron_active")))
check("scan 是只读端点：auto_restore_allowed=false（GET 不产生写副作用）",
      readiness.get("auto_restore_allowed") is False, str(readiness.get("auto_restore_allowed")))

procs = scan.get("procs") or []
print("        procs   :", len(procs), "| workers:", len(scan.get("workers") or []))
check("procs 是列表且每项带 kind / can_kill",
      isinstance(procs, list)
      and all(isinstance(p, dict) and p.get("kind") and "can_kill" in p for p in procs),
      str(len(procs)))
check("workers 与 procs 里 can_kill 的项一致",
      sorted(p.get("pid") for p in (scan.get("workers") or []))
      == sorted(p.get("pid") for p in procs if p.get("can_kill")))

print("\n[2b] GET /upgrade/readiness（真实 home，只读；当前无待恢复快照）")
resp_r = client.get(PREFIX + "/upgrade/readiness")
ready_body = resp_r.json()
print("        status:", resp_r.status_code, "| body:", brief(ready_body, 400))
check("HTTP 200 且 ok=true", resp_r.status_code == 200 and ready_body.get("ok") is True,
      str(resp_r.status_code))
ready = ready_body.get("data") or {}
check("readiness 字段齐全（ready/blockers/cron_active/pending_restore/restored）",
      all(k in ready for k in ("ready", "blockers", "cron_active", "pending_restore", "restored")),
      str(sorted(ready.keys())))
check("当前真实 state.json 的 restored=true ⇒ pending_restore=false（没有历史欠账）",
      ready.get("pending_restore") is False and ready.get("restored") is True,
      brief({k: ready.get(k) for k in ("pending_restore", "restored")}))
check("真实 cron 没被本探针改动：auto_restored = 0",
      ready.get("auto_restored") == 0, str(ready.get("auto_restored")))

# ---------------------------------------------------------------------------
print("\n[4] ★★/prepare 写快照失败必须回滚暂停（假 home + 写不进去的目录）")


class FakeCron:
    """假 cron：所有 pause/resume 只改内存里的 dict，绝不碰真实 cron/jobs.json。"""

    def __init__(self, jobs):
        self.jobs = {j["id"]: dict(j) for j in jobs}
        self.paused_calls: list[str] = []
        self.resumed_calls: list[str] = []

    async def list_jobs(self):
        return [dict(v) for v in self.jobs.values()]

    async def pause(self, job_id):
        self.paused_calls.append(job_id)
        job = self.jobs.get(job_id)
        if job is None:
            return False
        job["enabled"] = False
        job["state"] = "paused"
        return True

    async def resume(self, job_id):
        self.resumed_calls.append(job_id)
        job = self.jobs.get(job_id)
        if job is None:
            return False
        job["enabled"] = True
        job["state"] = "active"
        return True

    def enabled_map(self):
        return {jid: bool(job.get("enabled")) for jid, job in self.jobs.items()}


def fake_jobs():
    return [
        {"id": "job-a", "name": "看门狗", "profile": "default", "enabled": True},
        {"id": "job-b", "name": "日报", "profile": "xiaomo", "enabled": True},
        {"id": "job-c", "name": "本来就暂停", "profile": "xiaomo", "enabled": False, "state": "paused"},
    ]


_saved_impl = (
    api_upgrade.hermes_home,
    api_upgrade._get_cron_jobs,
    api_upgrade._pause_job,
    api_upgrade._resume_job,
)


def install_fake_home(home: Path, cron: FakeCron):
    api_upgrade.hermes_home = lambda: home
    api_upgrade._get_cron_jobs = cron.list_jobs
    api_upgrade._pause_job = cron.pause
    api_upgrade._resume_job = cron.resume


def restore_impl():
    (api_upgrade.hermes_home, api_upgrade._get_cron_jobs,
     api_upgrade._pause_job, api_upgrade._resume_job) = _saved_impl


tmp_root = Path(tempfile.mkdtemp(prefix="upgrade-probe-"))
try:
    bad_home = tmp_root / "bad-home"
    bad_state_dir = bad_home / "plugins" / "upgrade-console"
    bad_state_dir.mkdir(parents=True)
    # 让 _save_state 一定失败：它要写的临时文件位置被一个**目录**占着（Windows 上最稳的注入）。
    (bad_state_dir / "state.json.tmp").mkdir()

    bad_cron = FakeCron(fake_jobs())
    install_fake_home(bad_home, bad_cron)
    try:
        print("        假 home   :", bad_home)
        print("        注入失败点:", bad_state_dir / "state.json.tmp", "(目录)")

        rollback_resp = client.post(PREFIX + "/upgrade/prepare")
        rollback_body = rollback_resp.json()
        print("        status:", rollback_resp.status_code, "| body:", brief(rollback_body, 400))

        check("失败时是结构化错误而不是 500",
              rollback_resp.status_code == 503 and rollback_body.get("ok") is False,
              str(rollback_resp.status_code))
        err = rollback_body.get("error") or {}
        check("错误形状 = {ok:false,error:{code,message}}",
              isinstance(err.get("code"), str) and isinstance(err.get("message"), str)
              and bool(err.get("message")),
              brief(err, 240))
        check("error.code = state_unwritable", err.get("code") == "state_unwritable",
              str(err.get("code")))
        check("错误体里 state_file 指向假 home 的 state.json（不是真 home）",
              Path(err.get("state_file") or "") == bad_state_dir / "state.json",
              str(err.get("state_file")))

        check("本次暂停真的发生过（两条激活任务都被 pause）",
              sorted(bad_cron.paused_calls) == ["job-a", "job-b"], str(bad_cron.paused_calls))
        check("★回滚：rolled_back 覆盖本次全部暂停",
              sorted(err.get("rolled_back") or []) == ["job-a", "job-b"],
              str(err.get("rolled_back")))
        check("★回滚：still_paused 为空（没有卡在暂停态的残留）",
              (err.get("still_paused") or []) == [], str(err.get("still_paused")))
        check("★回滚后假 cron 的 enabled 全部恢复原样（job-c 本来就暂停，不许被复活）",
              bad_cron.enabled_map() == {"job-a": True, "job-b": True, "job-c": False},
              str(bad_cron.enabled_map()))
        check("快照没有落盘（失败后不该留下半个 state.json）",
              not (bad_state_dir / "state.json").exists())
        check("错误体 paused_count = 0（清场没执行）", err.get("paused_count") == 0,
              str(err.get("paused_count")))
    finally:
        restore_impl()

    # -----------------------------------------------------------------------
    print("\n[6] 快照往返：prepare → 重复 prepare 被拒 → restore → 再次 restore 被拒（假 home）")
    good_home = tmp_root / "good-home"
    good_state = good_home / "plugins" / "upgrade-console" / "state.json"
    good_cron = FakeCron(fake_jobs())
    install_fake_home(good_home, good_cron)
    try:
        prep = client.post(PREFIX + "/upgrade/prepare")
        prep_body = prep.json()
        print("        prepare:", prep.status_code, "|", brief(prep_body, 320))
        check("prepare 成功 200 且 ok=true",
              prep.status_code == 200 and prep_body.get("ok") is True, str(prep.status_code))
        prep_data = prep_body.get("data") or {}
        check("paused_count = 2（只暂停激活的那两条）", prep_data.get("paused_count") == 2,
              str(prep_data.get("paused_count")))
        check("paused 列表 = job-a / job-b",
              sorted(prep_data.get("paused") or []) == ["job-a", "job-b"],
              str(prep_data.get("paused")))
        check("快照里每条都带 was_active=true（恢复依据）",
              all(j.get("was_active") is True for j in (prep_data.get("jobs") or [])),
              brief(prep_data.get("jobs"), 240))
        check("快照真的写到了假 home 的 plugins/upgrade-console/state.json",
              good_state.is_file(), str(good_state))
        if good_state.is_file():
            snap = json.loads(good_state.read_text(encoding="utf-8"))
            print("        snapshot:", brief(snap, 300))
            check("快照 restored=false 且含 2 条 job（restore 的唯一依据）",
                  snap.get("restored") is False and len(snap.get("jobs") or []) == 2,
                  brief({k: snap.get(k) for k in ("restored",)}, 120))
            check("快照 paused_at 是数字时间戳", isinstance(snap.get("paused_at"), (int, float)),
                  str(snap.get("paused_at")))

        again = client.post(PREFIX + "/upgrade/prepare")
        again_body = again.json()
        print("        prepare#2:", again.status_code, "|", brief(again_body, 240))
        check("★重复 prepare 被拒（409 pending_restore，不覆盖待恢复快照）",
              again.status_code == 409 and (again_body.get("error") or {}).get("code") == "pending_restore",
              str(again.status_code))
        check("被拒时没有额外 pause 真实任务", sorted(good_cron.paused_calls) == ["job-a", "job-b"],
              str(good_cron.paused_calls))

        rest = client.post(PREFIX + "/upgrade/restore")
        rest_body = rest.json()
        print("        restore :", rest.status_code, "|", brief(rest_body, 320))
        check("restore 成功 200 且 ok=true",
              rest.status_code == 200 and rest_body.get("ok") is True, str(rest.status_code))
        rest_data = rest_body.get("data") or {}
        check("resumed_count = 2", rest_data.get("resumed_count") == 2,
              str(rest_data.get("resumed_count")))
        check("恢复后假 cron 的 enabled 全部为 true（job-c 未被复活，仍是 false）",
              good_cron.enabled_map() == {"job-a": True, "job-b": True, "job-c": False},
              str(good_cron.enabled_map()))
        if good_state.is_file():
            snap2 = json.loads(good_state.read_text(encoding="utf-8"))
            check("恢复后快照 restored 翻成 true 且 paused_at 被清掉",
                  snap2.get("restored") is True and "paused_at" not in snap2,
                  brief({k: snap2.get(k) for k in ("restored", "paused_at")}, 160))

        rest2 = client.post(PREFIX + "/upgrade/restore")
        rest2_body = rest2.json()
        print("        restore#2:", rest2.status_code, "|", brief(rest2_body, 240))
        check("★再次 restore 是 409 业务错误（不是 500，也不改写历史状态）",
              rest2.status_code == 409
              and (rest2_body.get("error") or {}).get("code") == "no_pending_restore",
              str(rest2.status_code))
    finally:
        restore_impl()

    # -----------------------------------------------------------------------
    print("\n[5] 业务错误不是 500（真实 home，只读 + 拒绝）")
    r_restore = client.post(PREFIX + "/upgrade/restore")
    r_restore_body = r_restore.json()
    print("        restore(真实 home, 无待恢复):", r_restore.status_code,
          "|", brief(r_restore_body, 240))
    check("/restore 无待恢复快照 → 409 而非 500",
          r_restore.status_code == 409
          and (r_restore_body.get("error") or {}).get("code") == "no_pending_restore",
          str(r_restore.status_code))
    check("/restore 的错误体是统一形状", r_restore_body.get("ok") is False
          and isinstance((r_restore_body.get("error") or {}).get("message"), str),
          brief(r_restore_body, 200))

    r_kill_empty = client.post(PREFIX + "/upgrade/kill", json={"pids": []})
    print("        kill(空 pids):", r_kill_empty.status_code, "|", brief(r_kill_empty.json(), 200))
    check("/kill 空 pids → 400 no_target（不是 500、不是 200）",
          r_kill_empty.status_code == 400
          and (r_kill_empty.json().get("error") or {}).get("code") == "no_target",
          str(r_kill_empty.status_code))

    r_kill_fake = client.post(PREFIX + "/upgrade/kill", json={"pids": [999999]})
    r_kill_fake_body = r_kill_fake.json()
    print("        kill(不存在的 pid):", r_kill_fake.status_code, "|", brief(r_kill_fake_body, 240))
    check("/kill 不存在的 pid → 409 nothing_killed（业务拒绝，不是 500）",
          r_kill_fake.status_code == 409
          and (r_kill_fake_body.get("error") or {}).get("code") == "nothing_killed",
          str(r_kill_fake.status_code))
    check("/kill 拒绝时逐条给出原因",
          bool((r_kill_fake_body.get("error") or {}).get("refused")),
          brief((r_kill_fake_body.get("error") or {}).get("refused"), 200))

    r_wait_empty = client.post(PREFIX + "/upgrade/wait", json={"pids": [], "timeout": 5})
    print("        wait(空 pids):", r_wait_empty.status_code, "|", brief(r_wait_empty.json(), 200))
    check("/wait 空 pids → 400 no_target（立刻返回，不空转 5 秒以上）",
          r_wait_empty.status_code == 400
          and (r_wait_empty.json().get("error") or {}).get("code") == "no_target",
          str(r_wait_empty.status_code))

    r_wait_gone = client.post(PREFIX + "/upgrade/wait", json={"pids": [999999], "timeout": 5})
    wait_body = r_wait_gone.json()
    print("        wait(不存在的 pid):", r_wait_gone.status_code, "|", brief(wait_body, 200))
    check("/wait 对已退出的 pid 立刻返回 timed_out=false",
          r_wait_gone.status_code == 200 and (wait_body.get("data") or {}).get("timed_out") is False,
          brief(wait_body, 160))
    check("/wait 的 timeout 被钳到 5（请求给的是 5）",
          (wait_body.get("data") or {}).get("timeout") == 5,
          str((wait_body.get("data") or {}).get("timeout")))
finally:
    restore_impl()
    shutil.rmtree(tmp_root, ignore_errors=True)

# ---------------------------------------------------------------------------
print("\n[7] ★生产零污染（结尾指纹 vs 开头指纹）")
after = production_fingerprint()
dirty = []
for path, digest in before.items():
    now = after.get(path)
    mark = "same" if now == digest else "CHANGED"
    if now != digest:
        dirty.append(path)
    print("        %-8s %s  %s" % (mark, (now or "<missing>")[:16], path.replace("\\", "/")))

check("config.yaml + profiles/*/config.yaml + state.json 一个字节都没变",
      not dirty, "变了: " + str(dirty))
check("指纹覆盖到真实 config.yaml", str(REAL_HOME / "config.yaml") in before,
      str(len(before)) + " 个文件")
check("指纹覆盖到 upgrade-console/state.json（不改状态文件内容）",
      str(REAL_HOME / "plugins" / "upgrade-console" / "state.json") in before)

# ---------------------------------------------------------------------------
print()
if failures:
    print("UPGRADE-PROBE-FAILED（%d 项）" % len(failures))
    for label in failures:
        print("  -", label)
    raise SystemExit(1)
print("UPGRADE-PROBE-OK")
raise SystemExit(0)

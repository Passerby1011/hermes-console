# -*- coding: utf-8 -*-
"""t_3c2817b1 · autogroup_model 按 Agent 存储的验收探针（隔离 home，不碰生产库）。

验：
  1. 给 A 设 X、给 B 设 Y → 各自回读正确、互不影响（逐字节比较另一段）
  2. 并发保存不丢数据（多线程同时写不同 agent，最终所有人的段都在）
  3. 旧单组格式（schema 1）能被读成「global 段 + 该 agent 声明」，行为与改造前等价
  4. 坏文件不被覆盖
  5. 来源层标签正确（agent / agent_global / global / module_default）
  6. _atomic_write 仍被使用（os.replace 路径存在，不出现半截文件）
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

DASH = Path(r"D:/hermes-data/hermes/plugins/hermes-console/dashboard")
sys.path.insert(0, str(DASH))

spec = importlib.util.spec_from_file_location("agm", DASH / "autogroup_model.py")
agm = importlib.util.module_from_spec(spec)
sys.modules["agm"] = agm
spec.loader.exec_module(agm)

PASS = []
FAIL = []


def check(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("  PASS " if ok else "  FAIL ") + name + (("  — " + str(extra)) if extra else ""))


def raw(home):
    return (Path(home) / "auto-group" / "ai_model.json").read_text(encoding="utf-8")


home = Path(tempfile.mkdtemp(prefix="agm-probe-"))
print("SANDBOX:", home, "\n")

# ---------------- 1. A / B 独立 ----------------
print("[1] 按 Agent 独立")
r_a = agm.save(home, mode="module", agent="agent-a", provider="openrouter", model="model-x")
check("A 保存成功", r_a.get("ok") is True, r_a.get("message", ""))
before_b = None
r_b = agm.save(home, mode="module", agent="agent-b", provider="deepseek", model="model-y")
check("B 保存成功", r_b.get("ok") is True, r_b.get("message", ""))
cfg = agm.load(home)
check("A 回读 provider", cfg["agents"]["agent-a"]["provider"] == "openrouter", cfg["agents"].get("agent-a"))
check("A 回读 model", cfg["agents"]["agent-a"]["model"] == "model-x", cfg["agents"].get("agent-a"))
check("B 回读 provider", cfg["agents"]["agent-b"]["provider"] == "deepseek", cfg["agents"].get("agent-b"))
check("B 回读 model", cfg["agents"]["agent-b"]["model"] == "model-y", cfg["agents"].get("agent-b"))
check("写 B 没覆盖 A", cfg["agents"]["agent-a"]["provider"] == "openrouter")
check("schema=2", cfg["schema"] == 2, cfg.get("schema"))
check("legacy=False", cfg["legacy"] is False)

ra = agm.resolve(home, "agent-a")
rb = agm.resolve(home, "agent-b")
check("resolve A → module", ra["source"] == "module" and ra["provider"] == "openrouter"
      and ra["model"] == "model-x", ra)
check("resolve B → module", rb["source"] == "module" and rb["provider"] == "deepseek"
      and rb["model"] == "model-y", rb)
check("resolve A layer=agent", ra["layer"] == "agent", ra.get("layer"))
check("resolve A agent 字段正确", ra["agent"] == "agent-a", ra.get("agent"))

# ---------------- 2. 并发保存 ----------------
print("\n[2] 并发保存不丢数据")
names = ["c-%02d" % i for i in range(12)]
errors = []


def worker(name):
    try:
        res = agm.save(home, mode="module", agent=name, provider="p-" + name, model="m-" + name)
        if not res.get("ok"):
            errors.append((name, res.get("code"), res.get("message")))
    except Exception as exc:  # noqa: BLE001
        errors.append((name, type(exc).__name__, str(exc)))


threads = [threading.Thread(target=worker, args=(n,)) for n in names]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("并发 12 个 agent 全部保存成功", not errors, errors[:3])
cfg2 = agm.load(home)
missing = [n for n in names if n not in (cfg2.get("agents") or {})]
check("并发后 12 个段都在", not missing, "missing=" + str(missing))
check("并发后 A/B 段仍在（没被冲掉）",
      cfg2["agents"].get("agent-a", {}).get("model") == "model-x"
      and cfg2["agents"].get("agent-b", {}).get("model") == "model-y")
bad = [n for n in names if (cfg2["agents"].get(n) or {}).get("model") != "m-" + n]
check("并发各段值正确", not bad, "bad=" + str(bad))
tmp_left = [p.name for p in (home / "auto-group").glob("*.tmp-*")]
check("没有遗留临时文件（_atomic_write 收尾正常）", not tmp_left, tmp_left)

# ---------------- 3. 旧格式兼容 ----------------
print("\n[3] 旧单组格式兼容读取")
legacy_home = Path(tempfile.mkdtemp(prefix="agm-legacy-"))
(legacy_home / "auto-group").mkdir(parents=True)
legacy_payload = {"mode": "module", "agent": "agent-a", "provider": "openrouter",
                  "model": "legacy-model", "updated_at": 1.0, "updated_by": "old"}
(legacy_home / "auto-group" / "ai_model.json").write_text(
    json.dumps(legacy_payload), encoding="utf-8")
cfg3 = agm.load(legacy_home)
check("旧格式被识别为 legacy", cfg3["legacy"] is True, cfg3.get("legacy"))
check("旧格式 → agent-a 段", (cfg3["agents"].get("agent-a") or {}).get("model") == "legacy-model",
      cfg3["agents"].get("agent-a"))
check("旧格式 → global 段", (cfg3["global"] or {}).get("model") == "legacy-model", cfg3.get("global"))
res_legacy = agm.resolve(legacy_home, "agent-a")
check("旧格式 resolve agent-a → module/legacy-model",
      res_legacy["source"] == "module" and res_legacy["model"] == "legacy-model", res_legacy)
legacy_bytes_before = (legacy_home / "auto-group" / "ai_model.json").read_bytes()
check("只读不写：旧文件字节未变", legacy_bytes_before ==
      (legacy_home / "auto-group" / "ai_model.json").read_bytes())
# 升级写入后旧 agent 的声明保留
r_up = agm.save(legacy_home, mode="module", agent="agent-b", provider="deepseek", model="m-b")
cfg4 = agm.load(legacy_home)
check("升级写 B 后 A 的旧声明仍在",
      (cfg4["agents"].get("agent-a") or {}).get("model") == "legacy-model", cfg4["agents"])
check("升级后 schema=2", cfg4["schema"] == 2 and cfg4["legacy"] is False)

# ---------------- 4. 坏文件不覆盖 ----------------
print("\n[4] 坏文件拒绝覆盖")
bad_home = Path(tempfile.mkdtemp(prefix="agm-bad-"))
(bad_home / "auto-group").mkdir(parents=True)
(bad_home / "auto-group" / "ai_model.json").write_text("{not json", encoding="utf-8")
r_bad = agm.save(bad_home, mode="global", agent=None, provider=None, model=None)
check("坏文件 save 返回失败", r_bad.get("ok") is False, r_bad.get("code"))
check("坏文件 code=config_unreadable", r_bad.get("code") == "config_unreadable", r_bad.get("code"))
check("坏文件内容未被覆盖",
      (bad_home / "auto-group" / "ai_model.json").read_text(encoding="utf-8") == "{not json")

# ---------------- 5. 来源层标签 ----------------
print("\n[5] 来源层")
layer_home = Path(tempfile.mkdtemp(prefix="agm-layer-"))
la = agm.load_agent(layer_home, "agent-z")
check("无任何声明 → module_default", la["layer"] == "module_default", la)
agm.save(layer_home, mode="global", agent=None, provider=None, model=None)
lg = agm.load_agent(layer_home, "agent-z")
check("有 global 段 → layer=global", lg["layer"] == "global", lg)
agm.save(layer_home, mode="global", agent="agent-z", provider=None, model=None)
lz = agm.load_agent(layer_home, "agent-z")
check("该 Agent 声明跟随全局 → agent_global", lz["layer"] == "agent_global", lz)
agm.save(layer_home, mode="module", agent="agent-z", provider="p", model="m")
lz2 = agm.load_agent(layer_home, "agent-z")
check("该 Agent 独立 → agent", lz2["layer"] == "agent", lz2)

# ---------------- 6. _atomic_write 在用 ----------------
src = (DASH / "autogroup_model.py").read_text(encoding="utf-8")
check("save() 调用 _atomic_write", "_atomic_write(config_path(home)" in src)
check("_atomic_write 用 os.replace", "os.replace(tmp, path)" in src)
check("save 的读改写都在 _CONFIG_LOCK 内",
      src.index("with _CONFIG_LOCK:") < src.index("_atomic_write(config_path(home)"))

print("\nMODEL-AGENT-PROBE: %d PASS / %d FAIL" % (len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  FAILED:", f)
shutil.rmtree(home, ignore_errors=True)
shutil.rmtree(legacy_home, ignore_errors=True)
shutil.rmtree(bad_home, ignore_errors=True)
shutil.rmtree(layer_home, ignore_errors=True)
sys.exit(1 if FAIL else 0)

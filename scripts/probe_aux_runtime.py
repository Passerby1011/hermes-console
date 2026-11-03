# -*- coding: utf-8 -*-
"""hermes-console · **运行时生效**证据（t_4d654faf）—— 不接受"接口返回 200"。

跑法::

    D:/hermes-data/hermes/hermes-agent/venv/Scripts/python.exe \
        plugins/hermes-console/scripts/probe_aux_runtime.py
    echo "exit=$?"

做法（真链路，不打桩模型调用本身）::

    本地 HTTP 桩服务（127.0.0.1，只记录 + 回一个合法 chat.completion）
        ↑ 子进程里真的调 agent.auxiliary_client.call_llm(task="approval" | "compression")
    子进程的 HERMES_HOME 指向临时 home（config.yaml 现写现用）

断言（每个场景一个独立子进程，避免宿主配置缓存串味）::

    [A] auxiliary.approval = {provider: stubalt, model: aux-model-B}
        ⇒ 桩收到的请求：路径 /alt/v1/... 且 body.model == "aux-model-B"
    [B] auxiliary.approval = {provider: auto, model: ""}（跟随全局）
        ⇒ 桩收到 /main/v1/... 且 body.model == 主模型 "main-model-A"
    [C] auxiliary.approval = {provider: stubalt, model: aux-model-C}（同 provider 换模型）
        ⇒ body.model == "aux-model-C"（换模型真的换了，不是只改了配置文件）
    [D] auxiliary.compression = {provider: stubalt, model: aux-model-D}，approval 保持 A
        ⇒ compression 的调用用 D，approval 的调用仍用 B（**按槽位独立**，不互相串）

四个场景覆盖了「Agent 独立配置 / 跟随全局」两种来源层的运行时真值，
并且给的是**该模块实际调用时用的 provider + model**（请求行），不是接口回执。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REAL_HOME = Path(__file__).resolve().parents[3]
HERMES_AGENT = REAL_HOME / "hermes-agent"

RECORDED: list[dict] = []
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


class _Stub(BaseHTTPRequestHandler):
    """最小 OpenAI 兼容端点：记录请求，回一个合法的 chat.completion。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D401 - 静音
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            body = {}
        RECORDED.append({
            "path": self.path,
            "model": str(body.get("model") or ""),
            "stream": bool(body.get("stream")),
            "auth_present": bool(self.headers.get("Authorization")),
        })
        payload = json.dumps({
            "id": "chatcmpl-stub",
            "object": "chat.completion",
            "created": 0,
            "model": str(body.get("model") or "stub"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        # 有的客户端会先探 /models
        RECORDED.append({"path": self.path, "model": "", "stream": False, "auth_present": False})
        payload = b'{"object":"list","data":[]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_home(home: Path, port: int, *, approval: dict, compression: dict | None = None) -> None:
    def aux_block(name: str, spec: dict) -> list[str]:
        lines = ["  %s:" % name, "    provider: %s" % spec["provider"],
                 "    model: %r" % spec["model"]]
        return lines

    lines = [
        "model:",
        "  default: main-model-A",
        "  provider: stubmain",
        "  base_url: http://127.0.0.1:%d/main/v1" % port,
        "  api_key: stub-key-main",
        "providers:",
        "  stubmain:",
        "    base_url: http://127.0.0.1:%d/main/v1" % port,
        "    api_key: stub-key-main",
        "    models:",
        "      main-model-A: {}",
        "  stubalt:",
        "    base_url: http://127.0.0.1:%d/alt/v1" % port,
        "    api_key: stub-key-alt",
        "    models:",
        "      aux-model-B: {}",
        "      aux-model-C: {}",
        "      aux-model-D: {}",
        "auxiliary:",
    ]
    lines += aux_block("approval", approval)
    if compression is not None:
        lines += aux_block("compression", compression)
    (home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (home / "profiles").mkdir(parents=True, exist_ok=True)


def child(port: int, task: str) -> int:
    """子进程：真调宿主 aux 链路（HERMES_HOME 由父进程通过环境变量给）。"""
    if str(HERMES_AGENT) not in sys.path:
        sys.path.insert(0, str(HERMES_AGENT))
    from agent import auxiliary_client  # noqa: E402

    response = auxiliary_client.call_llm(
        task=task,
        messages=[{"role": "user", "content": "runtime-evidence probe"}],
        temperature=0,
        max_tokens=8,
        timeout=20,
    )
    text = ""
    try:
        text = str(response.choices[0].message.content)
    except Exception:  # noqa: BLE001
        text = repr(response)[:120]
    print("CHILD-OK task=%s response=%s" % (task, text[:40]))
    return 0


def run_scenario(name: str, *, task: str, approval: dict, compression: dict | None = None,
                 expect_path: str, expect_model: str) -> bool:
    print("\n[%s] task=%s approval=%s" % (name, task, approval))
    port = free_port()
    home = Path(tempfile.mkdtemp(prefix="auxrt-"))
    write_home(home, port, approval=approval, compression=compression)
    RECORDED.clear()
    server = ThreadingHTTPServer(("127.0.0.1", port), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    env["HERMES_PROFILE"] = ""
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child",
                           str(port), task], env=env, cwd=str(REAL_HOME), text=True,
                          capture_output=True, timeout=180)
    server.shutdown()
    server.server_close()
    tail = (proc.stdout or "")[-300:] + (proc.stderr or "")[-600:]
    posts = [item for item in RECORDED if item["path"].startswith("/") and item["model"]]
    print("        child exit=%s recorded=%s" % (proc.returncode, posts[-2:]))
    if proc.returncode != 0 or not posts:
        print("        child output tail:", tail.replace("\n", " | ")[:400])
        check("%s：子进程真的发出了一次模型调用" % name, False, "exit=%s" % proc.returncode)
        shutil.rmtree(home, ignore_errors=True)
        return False
    last = posts[-1]
    ok_path = last["path"].startswith(expect_path)
    ok_model = last["model"] == expect_model
    check("%s：请求打到 %s（实际 %s）" % (name, expect_path, last["path"]), ok_path)
    check("%s：body.model == %r（实际 %r）" % (name, expect_model, last["model"]), ok_model)
    shutil.rmtree(home, ignore_errors=True)
    return ok_path and ok_model


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        return child(int(sys.argv[2]), sys.argv[3])

    print("运行时生效证据：真调 agent.auxiliary_client.call_llm → 本地桩端点记录请求行")
    run_scenario("A 该 Agent 独立配置", task="approval",
                 approval={"provider": "stubalt", "model": "aux-model-B"},
                 expect_path="/alt/v1", expect_model="aux-model-B")
    run_scenario("B 跟随全局（provider=auto）", task="approval",
                 approval={"provider": "auto", "model": ""},
                 expect_path="/main/v1", expect_model="main-model-A")
    run_scenario("C 同 provider 换模型", task="approval",
                 approval={"provider": "stubalt", "model": "aux-model-C"},
                 expect_path="/alt/v1", expect_model="aux-model-C")
    run_scenario("D 按槽位独立（compression）", task="compression",
                 approval={"provider": "stubalt", "model": "aux-model-B"},
                 compression={"provider": "stubalt", "model": "aux-model-D"},
                 expect_path="/alt/v1", expect_model="aux-model-D")
    run_scenario("E 按槽位独立（approval 不受 compression 影响）", task="approval",
                 approval={"provider": "stubalt", "model": "aux-model-B"},
                 compression={"provider": "stubalt", "model": "aux-model-D"},
                 expect_path="/alt/v1", expect_model="aux-model-B")

    print("\n=== 汇总 ===")
    print("PASS: %d   FAIL: %d" % (passed, len(failures)))
    print("AUX-RUNTIME-OK" if not failures else "AUX-RUNTIME-FAILED")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())

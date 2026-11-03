import json, os, sys, tempfile, shutil
from pathlib import Path
DASH = Path(r"D:/hermes-data/hermes/plugins/hermes-console/dashboard")
HOME = Path(r"D:/hermes-data/hermes")
for p in (str(DASH), str(HOME / "hermes-agent")):
    sys.path.insert(0, p)
sb = Path(tempfile.mkdtemp(prefix="rt-smoke-"))
(sb / "profiles").mkdir()
(sb / "config.yaml").write_text("plugins: {}\n", encoding="utf-8")
(sb / ".env").write_text("HERMES_DASHBOARD_BASIC_AUTH_USERNAME=bossuser\nHERMES_DASHBOARD_BASIC_AUTH_PASSWORD=supersecretpw\n", encoding="utf-8")
for name in ("alpha", "beta", "gamma"):
    d = sb / "profiles" / name
    d.mkdir()
    (d / "config.yaml").write_text("plugins: {}\n", encoding="utf-8")
    (d / "profile.yaml").write_text("title: sandbox-" + name + "\n", encoding="utf-8")
os.environ["HERMES_HOME"] = str(sb)
import importlib.util
spec = importlib.util.spec_from_file_location("hc_api", DASH / "plugin_api.py")
m = importlib.util.module_from_spec(spec)
sys.modules["hc_api"] = m
spec.loader.exec_module(m)
import api_remote
api_remote._home = lambda: sb
from fastapi import FastAPI
from fastapi.testclient import TestClient
app = FastAPI()
app.include_router(m.router, prefix="/api/plugins/hermes-console")
c = TestClient(app, raise_server_exceptions=False)
r = c.get("/api/plugins/hermes-console/remote/agents")
b = r.json()
print("status", r.status_code)
print("agents:", [(x["agent"], x["username"], x["conn"], x["last_started_at"], x["last_stopped_at"], x["last_error"]) for x in b["agents"]])
print("keys:", sorted(b["agents"][0].keys()))
print("password leaked in agents?", "supersecretpw" in json.dumps(b))
print("reveal no confirm:", c.post("/api/plugins/hermes-console/remote/credentials/reveal", json={"agent": "alpha", "field": "password"}).status_code)
print("reveal bad field:", c.post("/api/plugins/hermes-console/remote/credentials/reveal", json={"agent": "alpha", "field": "nope", "confirm": True}).status_code)
print("reveal unknown agent:", c.post("/api/plugins/hermes-console/remote/credentials/reveal", json={"agent": "zzz", "field": "password", "confirm": True}).status_code)
rv = c.post("/api/plugins/hermes-console/remote/credentials/reveal", json={"agent": "alpha", "field": "password", "confirm": True})
print("reveal ok:", rv.status_code, rv.json()["value"] == "supersecretpw", rv.headers.get("cache-control"))
ev = (sb / "plugins/hermes-console/data/remote-events.jsonl")
print("events:", ev.read_text(encoding="utf-8").strip())
print("events has secret?", "supersecretpw" in ev.read_text(encoding="utf-8"))
api_remote._append_event("alpha", "reveal", True, value="supersecretpw", field="password")
print("guard (value still absent)?", "supersecretpw" in ev.read_text(encoding="utf-8"))
print("creds detail masked:", json.dumps(c.get("/api/plugins/hermes-console/remote/credentials", params={"agent": "alpha"}).json())[:220])
print("health route_count:", c.get("/api/plugins/hermes-console/health").json()["route_count"])
shutil.rmtree(sb, ignore_errors=True)

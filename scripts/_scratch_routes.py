import sys, os, importlib.util
from pathlib import Path
DASH = Path(r"D:/hermes-data/hermes/plugins/hermes-console/dashboard")
HOME = Path(r"D:/hermes-data/hermes")
for p in (str(DASH), str(HOME / "hermes-agent")):
    sys.path.insert(0, p)
spec = importlib.util.spec_from_file_location("hc_api_probe", DASH / "plugin_api.py")
m = importlib.util.module_from_spec(spec)
sys.modules["hc_api_probe"] = m
spec.loader.exec_module(m)
rows = sorted("%s %s" % (sorted(r.methods)[0], r.path) for r in m.router.routes)
print(len(rows))
for row in rows:
    print(row)
print("failed:", m.DOMAIN_FAILURES)

# -*- coding: utf-8 -*-
"""把探针 JSON 压成摘要（只读）。"""
import json
from pathlib import Path

p = Path(__file__).resolve().parent / "_probe_out" / "profile-dbs" / "db-report.json"
data = json.load(open(p, encoding="utf-8"))
out = []
for r in data:
    r.pop("ids", None)
    r.pop("ids_sample", None)
    out.append(r.get("label") + " rows=" + str(r.get("rows")))
    for k in ("profile_name", "visible", "archived_rows", "pinned_rows", "hidden_rows",
              "overlap_root", "missing"):
        if k in r:
            out.append("    %s = %s" % (k, r[k]))
    if "sources" in r:
        out.append("    sources = %s" % dict(list(r["sources"].items())[:10]))
    out.append("")
text = "\n".join(out)
print(text)
Path(__file__).resolve().parent.joinpath("_probe_out", "profile-dbs", "summary.txt").write_text(
    text, encoding="utf-8")

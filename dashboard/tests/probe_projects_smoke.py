# -*- coding: utf-8 -*-
"""冒烟：新模块在沙盒里能不能跑通（先跑这个，挂了就别看后面的用例）。"""
from __future__ import annotations

import sys
from pathlib import Path

DASH = Path(__file__).resolve().parents[1]
if str(DASH) not in sys.path:
    sys.path.insert(0, str(DASH))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from project_sandbox import ProjectSandbox, body, status_of  # noqa: E402

import api_autogroup_projects as api  # noqa: E402
import autogroup_align as align  # noqa: E402
import autogroup_projects as ent  # noqa: E402


def main() -> int:
    with ProjectSandbox() as sb:
        a = sb.add_project("alpha", "Alpha")
        b = sb.add_project("beta", "Beta")
        sb.add_session("s1", "会话一", cwd=str(b))
        sb.add_session("s2", "会话二", cwd=str(b))
        sb.add_assignment("s1", "alpha")
        sb.add_assignment("s2", "alpha")

        ents = body(api.get_entities())
        print("entities ok:", ents["ok"], "total:", ents["data"]["total"])
        for item in ents["data"]["projects"]:
            print("   ", item["slug"], item["dir"], item["assignments"])

        rep = align.audit(sb.home)
        print("audit:", rep["counts"])

        prev = body(api.get_align_preview(mode="fix_cwd"))
        print("preview ok:", prev["ok"], "affected:", prev["data"]["affected"],
              "blocked:", len(prev["data"]["blocked"]))

        no_confirm = api.post_align({"mode": "fix_cwd"})
        print("no confirm ->", status_of(no_confirm), body(no_confirm)["error"]["code"])

        done = api.post_align({"mode": "fix_cwd", "confirm": True})
        print("align ->", status_of(done), body(done).get("ok"))
        print("   s1 cwd:", sb.cwd_of("s1"), "expect:", a)
        after = align.audit(sb.home)
        print("audit after:", after["counts"])

        op_id = body(done)["data"]["op_id"]
        import api_autogroup as base
        undo = body(base.post_undo({"op_id": op_id}))
        print("undo ok:", undo.get("ok"))
        print("   s1 cwd restored:", sb.cwd_of("s1"), "expect:", b)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

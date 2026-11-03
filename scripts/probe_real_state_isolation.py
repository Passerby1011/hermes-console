# -*- coding: utf-8 -*-
"""只读探针：证明沙盒验收没有写进真 home 的 state.db。

为什么不用哈希：真 ``state.db`` 是 2.2GB 的**活会话库**，Hermes 自己（本 worker 会话、
网关、其它 agent）随时在往里写消息 —— 文件 mtime 每几分钟就变一次，用 mtime/size 做断言
只会得到假红。真正该证明的是「夹具的会话没有出现在生产库里」：只要没出现，就说明
``/autogroup/apply`` 的写入落在沙盒，而不是真 home。

用法::

    python scripts/probe_real_state_isolation.py --db D:/hermes-data/hermes/state.db \\
        --prefix sess-agg- --prefix sess-wk- --prefix sess-xm-

输出一行 JSON：``{"db": ..., "total": N, "prefixes": [...], "found": [...], "ok": bool}``
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--prefix", action="append", default=[])
    args = parser.parse_args(argv)

    path = Path(args.db)
    if not path.is_file():
        print(json.dumps({"db": str(path), "ok": True, "total": 0, "found": [],
                          "note": "state.db 不存在（不可能是写入目标）"}))
        return 0
    conn = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
    try:
        total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        found: list[str] = []
        for prefix in args.prefix:
            rows = conn.execute(
                "SELECT id FROM sessions WHERE id LIKE ? LIMIT 5", (prefix + "%",)).fetchall()
            found.extend(str(r[0]) for r in rows)
    finally:
        conn.close()
    print(json.dumps({"db": str(path), "ok": not found, "total": int(total),
                      "prefixes": list(args.prefix), "found": found}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

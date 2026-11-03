# -*- coding: utf-8 -*-
"""截图非空/两栏都渲染的客观检查（vision 服务 504 时的替代证据）。

对 PNG 做像素统计：整图墨迹率、左 1/4 与右 3/4 区域各自的墨迹率。
两栏都渲染 ⇒ 两个区域都有明显墨迹；空白页 ⇒ 墨迹率接近 0。
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image
except Exception as exc:  # noqa: BLE001
    print("PIL 不可用：%s" % exc)
    raise SystemExit(2)


def ink(path: Path) -> dict:
    image = Image.open(path).convert("L")
    width, height = image.size
    pixels = image.load()
    step = 2  # 采样步长，够用且快
    hist = [0] * 256
    samples = []
    for y in range(0, height, step):
        for x in range(0, width, step):
            value = pixels[x, y]
            hist[value] += 1
            samples.append((x, value))
    background = max(range(256), key=lambda index: hist[index])

    def ratio(items) -> float:
        if not items:
            return 0.0
        hit = sum(1 for _, value in items if abs(value - background) > 8)
        return round(100.0 * hit / len(items), 3)

    return {
        "size": (width, height),
        "background": background,
        "distinct": sum(1 for count in hist if count),
        "all_pct": ratio(samples),
        "left_pct": ratio([item for item in samples if item[0] < width // 4]),
        "right_pct": ratio([item for item in samples if item[0] > width // 2]),
    }


def main(paths: list[str]) -> int:
    bad = 0
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            print("MISSING %s" % path)
            bad += 1
            continue
        stats = ink(path)
        ok = (stats["all_pct"] > 2 and stats["left_pct"] > 1 and stats["right_pct"] > 1
              and stats["distinct"] > 30)
        print(("  ok  " if ok else "  BAD ") + path.name + "  " + str(stats))
        if not ok:
            bad += 1
    print("\nBAD =", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

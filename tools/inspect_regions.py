"""诊断脚本：列出基线 DXF 中 H2 / D2 / 轴伸 三个红框区域内的全部实体。"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import ezdxf

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "output" / "TD50-40G_真实基线_clean.dxf"

REGIONS = {
    "H2": (50900, 51400, 1300, 1530),
    "D2": (51350, 51600, 700, 1010),
    "SHAFT": (51100, 51450, 400, 700),
}


def bbox(e):
    t = e.dxftype()
    try:
        if t == "LINE":
            s, en = e.dxf.start, e.dxf.end
            return (min(s.x, en.x), max(s.x, en.x), min(s.y, en.y), max(s.y, en.y))
        if t in ("CIRCLE", "ARC"):
            c = e.dxf.center
            r = e.dxf.radius
            return (c.x - r, c.x + r, c.y - r, c.y + r)
        if t == "TEXT":
            p = e.dxf.insert
            return (p.x, p.x + 200, p.y, p.y + 100)
        if t == "MTEXT":
            p = e.dxf.insert
            return (p.x, p.x + 300, p.y, p.y + 200)
        if t == "SPLINE":
            pts = list(e.control_points)
            return (min(p.x for p in pts), max(p.x for p in pts),
                    min(p.y for p in pts), max(p.y for p in pts))
    except Exception:
        pass
    return (0, 0, 0, 0)


def geom(e):
    t = e.dxftype()
    try:
        if t == "LINE":
            s, en = e.dxf.start, e.dxf.end
            return f"({s.x:.1f},{s.y:.1f})->({en.x:.1f},{en.y:.1f})"
        if t in ("CIRCLE", "ARC"):
            c = e.dxf.center
            extra = ""
            if t == "ARC":
                extra = f" a={e.dxf.start_angle:.1f}..{e.dxf.end_angle:.1f}"
            return f"c=({c.x:.1f},{c.y:.1f}) r={e.dxf.radius:.1f}{extra}"
        if t == "TEXT":
            return f"tx={e.dxf.text!r} @({e.dxf.insert.x:.1f},{e.dxf.insert.y:.1f})"
        if t == "MTEXT":
            return f"mt={e.dxf.text[:30]!r} @({e.dxf.insert.x:.1f},{e.dxf.insert.y:.1f})"
    except Exception:
        pass
    return ""


def main():
    doc = ezdxf.readfile(str(SRC))
    msp = doc.modelspace()
    ents = list(msp)
    print(f"TOTAL {len(ents)}")
    print("TYPES", Counter(e.dxftype() for e in ents))
    for name, (x0, x1, y0, y1) in REGIONS.items():
        print(f"\n===== {name} X[{x0},{x1}] Y[{y0},{y1}] =====")
        sel = []
        for i, e in enumerate(ents):
            bx0, bx1, by0, by1 = bbox(e)
            if bx1 >= x0 and bx0 <= x1 and by1 >= y0 and by0 <= y1:
                sel.append((i, e, (bx0, bx1, by0, by1)))
        print("COUNT", len(sel), Counter(e.dxftype() for _, e, _ in sel))
        print("LAYERS", Counter(e.dxf.layer for _, e, _ in sel))
        for i, e, bb in sel:
            print(f"{i:5d} h={e.dxf.handle:<6s} {e.dxftype():<8s} L={e.dxf.layer:<12s} "
                  f"bb=({bb[0]:.1f},{bb[1]:.1f},{bb[2]:.1f},{bb[3]:.1f}) {geom(e)}")


if __name__ == "__main__":
    main()

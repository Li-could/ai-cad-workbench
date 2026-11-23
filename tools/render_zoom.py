"""把 DXF 指定区域渲染成带 handle 标签的放大 SVG，便于人工核对实体。"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

import ezdxf

ROOT = Path(__file__).resolve().parent.parent


def un(t):
    return re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), t)


def render(src, out, x0, x1, y0, y1, title="", labels=True):
    d = ezdxf.readfile(src)
    m = d.modelspace()
    W, H = 1500, 1000
    sc = min((W - 60) / (x1 - x0), (H - 80) / (y1 - y0))
    ox = 30 - x0 * sc
    oy = H - 50 + y0 * sc
    X = lambda v: ox + v * sc
    Y = lambda v: oy - v * sc
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
         '<rect width="100%" height="100%" fill="#fff"/>',
         f'<text x="20" y="24" font-size="18" fill="#123">{title}</text>']
    for e in m:
        t = e.dxftype()
        try:
            if t == "LINE":
                s, en = e.dxf.start, e.dxf.end
                if max(s.x, en.x) < x0 or min(s.x, en.x) > x1 or max(s.y, en.y) < y0 or min(s.y, en.y) > y1:
                    continue
                p.append(f'<line x1="{X(s.x):.1f}" y1="{Y(s.y):.1f}" x2="{X(en.x):.1f}" y2="{Y(en.y):.1f}" stroke="#222" stroke-width="1"/>')
                if labels:
                    mx, my = (s.x + en.x) / 2, (s.y + en.y) / 2
                    p.append(f'<text x="{X(mx):.1f}" y="{Y(my):.1f}" font-size="8" fill="#c00">{e.dxf.handle}</text>')
            elif t in ("ARC", "CIRCLE"):
                c = e.dxf.center
                r = e.dxf.radius
                if c.x + r < x0 or c.x - r > x1 or c.y + r < y0 or c.y - r > y1:
                    continue
                if t == "CIRCLE":
                    p.append(f'<circle cx="{X(c.x):.1f}" cy="{Y(c.y):.1f}" r="{r*sc:.1f}" fill="none" stroke="#222" stroke-width="1"/>')
                else:
                    a1, a2 = e.dxf.start_angle, e.dxf.end_angle
                    if a2 < a1:
                        a2 += 360
                    pts = [(c.x + r * math.cos(math.radians(a1 + (a2 - a1) * i / 40)),
                            c.y + r * math.sin(math.radians(a1 + (a2 - a1) * i / 40))) for i in range(41)]
                    p.append('<polyline fill="none" stroke="#222" stroke-width="1" points="' +
                             " ".join(f"{X(a):.1f},{Y(b):.1f}" for a, b in pts) + '"/>')
                if labels:
                    p.append(f'<text x="{X(c.x):.1f}" y="{Y(c.y):.1f}" font-size="8" fill="#00c">{e.dxf.handle}</text>')
            elif t in ("TEXT", "MTEXT"):
                q = e.dxf.insert
                if not (x0 <= q.x <= x1 and y0 <= q.y <= y1):
                    continue
                tx = un(e.dxf.text if t == "TEXT" else e.dxf.text)
                p.append(f'<text x="{X(q.x):.1f}" y="{Y(q.y):.1f}" font-size="11" fill="#070">{tx[:24]}</text>')
        except Exception:
            pass
    p.append("</svg>")
    Path(out).write_text("\n".join(p), encoding="utf-8")
    import xml.dom.minidom as m
    m.parse(str(out))


if __name__ == "__main__":
    src = ROOT / "output" / "TD50-40G_真实基线_clean.dxf"
    o = ROOT / "output"
    render(src, o / "_zoom_shaft.svg", 50850, 51500, 420, 720, "轴伸/联轴器区（实体 handle）")
    render(src, o / "_zoom_d2.svg", 51290, 51600, 700, 1060, "接线盒区（实体 handle）")
    render(src, o / "_zoom_h2.svg", 50900, 51420, 1310, 1530, "风罩顶部区（实体 handle）")
    print("zoom svgs generated")

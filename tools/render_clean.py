"""干净渲染 DXF 指定区域为 SVG（无 handle 标签，工程图风格）。"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

import ezdxf

ROOT = Path(__file__).resolve().parent.parent


def un(t):
    return re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), t)


def render_clean(src, out, x0, x1, y0, y1, title="", W=2200, H=1500):
    d = ezdxf.readfile(src)
    m = d.modelspace()
    sc = min((W - 80) / (x1 - x0), (H - 110) / (y1 - y0))
    ox = 40 - x0 * sc
    oy = H - 70 + y0 * sc
    X = lambda v: ox + v * sc
    Y = lambda v: oy - v * sc
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
         '<rect width="100%" height="100%" fill="#fff"/>']
    if title:
        p.append(f'<text x="20" y="30" font-size="20" fill="#123" font-family="Microsoft YaHei">{title}</text>')
    for e in m:
        t = e.dxftype()
        try:
            if t == "LINE":
                s, en = e.dxf.start, e.dxf.end
                if max(s.x, en.x) < x0 or min(s.x, en.x) > x1 or max(s.y, en.y) < y0 or min(s.y, en.y) > y1:
                    continue
                p.append(f'<line x1="{X(s.x):.2f}" y1="{Y(s.y):.2f}" x2="{X(en.x):.2f}" y2="{Y(en.y):.2f}" stroke="#1a1a1a" stroke-width="1.1"/>')
            elif t == "ARC":
                c = e.dxf.center
                if c.x + e.dxf.radius < x0 or c.x - e.dxf.radius > x1 or c.y + e.dxf.radius < y0 or c.y - e.dxf.radius > y1:
                    continue
                r = e.dxf.radius * sc
                a0, a1 = -e.dxf.end_angle, -e.dxf.start_angle
                xA, yA = X(c.x) + r * math.cos(math.radians(e.dxf.start_angle)), Y(c.y) - r * math.sin(math.radians(e.dxf.start_angle))
                xB, yB = X(c.x) + r * math.cos(math.radians(e.dxf.end_angle)), Y(c.y) - r * math.sin(math.radians(e.dxf.end_angle))
                laf = 1 if (e.dxf.end_angle - e.dxf.start_angle) % 360 > 180 else 0
                p.append(f'<path d="M {xA:.2f} {yA:.2f} A {r:.2f} {r:.2f} 0 {laf} 1 {xB:.2f} {yB:.2f}" fill="none" stroke="#1a1a1a" stroke-width="1.1"/>')
            elif t == "CIRCLE":
                c = e.dxf.center
                if c.x + e.dxf.radius < x0 or c.x - e.dxf.radius > x1 or c.y + e.dxf.radius < y0 or c.y - e.dxf.radius > y1:
                    continue
                p.append(f'<circle cx="{X(c.x):.2f}" cy="{Y(c.y):.2f}" r="{e.dxf.radius * sc:.2f}" fill="none" stroke="#1a1a1a" stroke-width="1.1"/>')
            elif t == "SOLID":
                pts = [e.dxf.vtx0, e.dxf.vtx1, e.dxf.vtx2, e.dxf.vtx3]
                p.append(f'<polygon fill="#1a1a1a" points="{" ".join(f"{X(a.x):.2f},{Y(a.y):.2f}" for a in pts)}"/>')
            elif t == "TEXT":
                ins = e.dxf.insert
                if not (x0 <= ins.x <= x1 and y0 <= ins.y <= y1):
                    continue
                h = max(10, (e.dxf.height or 2.5) * sc * 1.15)
                p.append(f'<text x="{X(ins.x):.2f}" y="{Y(ins.y):.2f}" font-size="{h:.1f}" fill="#111" font-family="Microsoft YaHei">{un(str(e.dxf.text))}</text>')
            elif t == "MTEXT":
                ins = e.dxf.insert
                if not (x0 <= ins.x <= x1 and y0 <= ins.y <= y1):
                    continue
                h = max(10, (e.dxf.char_height or 2.5) * sc * 1.15)
                txt = re.sub(r"\\[A-Za-z][^;]*;", "", un(str(e.dxf.text))).replace("\\\\", "\\")
                p.append(f'<text x="{X(ins.x):.2f}" y="{Y(ins.y):.2f}" font-size="{h:.1f}" fill="#111" font-family="Microsoft YaHei">{txt}</text>')
        except Exception:
            continue
    p.append("</svg>")
    Path(out).write_text("\n".join(p), encoding="utf-8")


if __name__ == "__main__":
    src, out = sys.argv[1], sys.argv[2]
    x0, x1, y0, y1 = map(float, sys.argv[3:7])
    title = sys.argv[7] if len(sys.argv) > 7 else ""
    render_clean(src, out, x0, x1, y0, y1, title)
    print("ok", out)

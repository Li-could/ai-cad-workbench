"""图纸预览与修改前后差异图（方案：图纸预览区 + 视觉级质检）。"""

from __future__ import annotations

from typing import Any
import html


def _esc(s: str) -> str:
    return html.escape(str(s))


def make_preview(parsed: dict[str, Any]) -> str:
    """基于解析出的实体坐标绘制简化预览（LINE/CIRCLE/ARC/TEXT）。"""
    ents = parsed.get("entities", [])
    pts = []
    for e in ents:
        x, y = e.get("x"), e.get("y")
        x2, y2 = e.get("x2"), e.get("y2")
        if x is not None and y is not None:
            pts.append((float(x), float(y)))
        if x2 is not None and y2 is not None:
            pts.append((float(x2), float(y2)))
    if pts:
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        xmin, xmax = min(xs), max(xs); ymin, ymax = min(ys), max(ys)
    else:
        xmin, xmax, ymin, ymax = 0, 1000, 0, 1000
    w = max(xmax - xmin, 1); h = max(ymax - ymin, 1)
    sc = min(900 / w, 700 / h)
    ox, oy = 40 - xmin * sc, 740 + ymin * sc

    def X(x): return ox + float(x) * sc
    def Y(y): return oy - float(y) * sc

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="800" viewBox="0 0 1000 800">',
             '<rect width="1000" height="800" fill="#ffffff"/>']
    for e in ents:
        t = e.get("type"); layer = _esc(e.get("layer", ""))
        color = "#333"
        if "修改" in layer or "H2" in layer: color = "#d32f2f"
        elif "D2" in layer: color = "#1565c0"
        elif "轴" in layer: color = "#ef6c00"
        x, y = e.get("x"), e.get("y")
        if t == "LINE" and x is not None and e.get("x2") is not None:
            parts.append(f'<line x1="{X(x):.1f}" y1="{Y(y):.1f}" x2="{X(e["x2"]):.1f}" y2="{Y(e["y2"]):.1f}" stroke="{color}" stroke-width="1"/>')
        elif t == "CIRCLE" and x is not None and e.get("height_or_radius"):
            parts.append(f'<circle cx="{X(x):.1f}" cy="{Y(y):.1f}" r="{float(e["height_or_radius"])*sc:.1f}" fill="none" stroke="{color}" stroke-width="1"/>')
        elif t in ("TEXT", "MTEXT") and x is not None:
            txt = _esc(e.get("text", ""))[:40]
            parts.append(f'<text x="{X(x):.1f}" y="{Y(y):.1f}" font-size="11" fill="{color}">{txt}</text>')
    parts.append(f'<text x="20" y="785" font-size="12" fill="#666">实体数 {len(ents)} · 图层 {len(parsed.get("layers",[]))} · 解析后端 {_esc(parsed.get("parser",""))}</text>')
    parts.append('</svg>')
    return "\n".join(parts)


def make_diff(before: dict, after: dict, plan: dict) -> str:
    """修改前后对照：差异尺寸用红/蓝/橙标出。"""
    bi = before.get("size_index", {}); ai = after.get("size_index", {})
    rows = []
    allk = list(dict.fromkeys(list(bi.keys()) + list(ai.keys())))
    for k in allk:
        b = len(bi.get(k, [])); a = len(ai.get(k, []))
        if b != a:
            col = "#d32f2f"
            if "D2" in k: col = "#1565c0"
            elif "轴" in k: col = "#ef6c00"
            rows.append((k, b, a, col))
        else:
            rows.append((k, b, a, "#555"))
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="max(400,' + str(80 + len(rows) * 26) + ')" viewBox="0 0 900 ' + str(80 + len(rows) * 26) + '">',
             '<rect width="900" height="' + str(80 + len(rows) * 26) + '" fill="#fff"/>',
             '<text x="20" y="36" font-size="18" font-weight="bold" fill="#222">修改前后尺寸图谱对照</text>',
             '<text x="20" y="60" font-size="12" fill="#666">红色=H2 蓝色=D2 橙色=轴伸；数值变化以实际解析为准</text>']
    y = 90
    for k, b, a, col in rows[:60]:
        parts.append(f'<text x="20" y="{y}" font-size="13" fill="{col}">{_esc(k)}　改前命中 {b} → 改后命中 {a}</text>')
        y += 26
    parts.append('</svg>')
    return "\n".join(parts)

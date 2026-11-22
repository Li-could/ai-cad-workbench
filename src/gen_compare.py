"""生成几何修改前后对照 SVG（基于真实实体坐标）。"""
import ezdxf, re, math, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def snap(e):
    t = e.dxftype()
    try:
        if t == 'LINE':
            s, en = e.dxf.start, e.dxf.end
            return (t, round(s.x, 2), round(s.y, 2), round(en.x, 2), round(en.y, 2))
        if t in ('ARC', 'CIRCLE'):
            c = e.dxf.center
            return (t, round(c.x, 2), round(c.y, 2), round(e.dxf.radius, 4))
        if t in ('TEXT', 'MTEXT'):
            ip = e.dxf.insert
            return (t, round(ip.x, 2), round(ip.y, 2), str(e.dxf.text))
    except Exception:
        return None
    return None


def highlight_color(e, zones=None):
    """按变更区域给高亮色；zones 由图纸档案提供，缺省统一红色。"""
    t = e.dxftype()
    try:
        if t == 'LINE':
            mx = (e.dxf.start.x + e.dxf.end.x) / 2
            my = (e.dxf.start.y + e.dxf.end.y) / 2
        elif t in ('ARC', 'CIRCLE'):
            mx, my = e.dxf.center.x, e.dxf.center.y
        elif t in ('TEXT', 'MTEXT'):
            mx, my = e.dxf.insert.x, e.dxf.insert.y
        else:
            return '#d32f2f'
    except Exception:
        return '#d32f2f'
    if zones:
        for zone in zones:
            x0, x1, y0, y1 = zone["bbox"]
            if x0 <= mx <= x1 and y0 <= my <= y1:
                return zone.get("color", "#d32f2f")
    return '#d32f2f'


def mtext_plain(text: str) -> str:
    """MTEXT -> 纯文本（去控制码），\\P 换行。"""
    t = text.replace('\\P', '\n')
    t = re.sub(r'\\[^\\{}\n]{0,30};', '', t)   # \H0.7x; \C1; \fSimSun|b0; 等
    t = re.sub(r'\\([{}\\])', r'\1', t)        # 转义 \{ \} \\
    t = t.replace('~~', '~')
    return t


def xml_esc(s: str) -> str:
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def render(src, out, title, baseline=None, viewport=None, fs_ann=11, fs_orig=7,
           zones=None):
    d = ezdxf.readfile(src); msp = d.modelspace()
    base_snap = {}
    if baseline:
        bd = ezdxf.readfile(baseline)
        base_snap = {e.dxf.handle: snap(e) for e in bd.modelspace()}
    if viewport:
        x0, x1, y0, y1 = viewport
        W, H = 1500, 1000
        mgl = 15
    else:
        from ezdxf import bbox as _bbox

        ext = _bbox.extents(msp)
        pad = max((ext.extmax.x - ext.extmin.x), (ext.extmax.y - ext.extmin.y)) * 0.03
        x0, x1 = ext.extmin.x - pad, ext.extmax.x + pad
        y0, y1 = ext.extmin.y - pad, ext.extmax.y + pad
        W, H = 1400, 900
        mgl = 40
    sc = min((W - 2 * mgl) / (x1 - x0), (H - 2 * mgl) / (y1 - y0))
    ox = mgl - x0 * sc; oy = H - mgl + y0 * sc
    X = lambda v: ox + v * sc; Y = lambda v: oy - v * sc

    def un(t):
        return re.sub(r'\\U\+([0-9A-Fa-f]{4})', lambda m: chr(int(m.group(1), 16)), t)

    colmap = {'变更_H2_红色': '#d32f2f', '变更_D2_蓝色': '#1565c0', '变更_轴伸_橙色': '#ea6c0c'}
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
         f'font-family="Microsoft YaHei, SimHei, sans-serif">',
         '<rect width="100%" height="100%" fill="#fff"/>',
         f'<text x="20" y="26" font-size="18" font-weight="bold" fill="#1e3a8a">{xml_esc(title)}</text>']
    for e in msp:
        t = e.dxftype(); lyr = e.dxf.layer
        col = colmap.get(lyr)
        if col is None and base_snap:
            # 与基线做 handle 级 diff：几何发生变化/新增的实体高亮，其余保持原图灰黑
            col = highlight_color(e, zones) if base_snap.get(e.dxf.handle) != snap(e) else '#333'
        elif col is None:
            col = '#333'
        w = 1.8 if col != '#333' else 0.5
        if viewport and col != '#333':
            w = 3.0
        elif viewport:
            w = 0.9
        fs = fs_ann if col != '#333' else fs_orig
        try:
            if t == 'LINE':
                s, en = e.dxf.start, e.dxf.end
                p.append(f'<line x1="{X(s.x):.1f}" y1="{Y(s.y):.1f}" x2="{X(en.x):.1f}" y2="{Y(en.y):.1f}" stroke="{col}" stroke-width="{w}"/>')
            elif t == 'CIRCLE':
                c = e.dxf.center
                p.append(f'<circle cx="{X(c.x):.1f}" cy="{Y(c.y):.1f}" r="{e.dxf.radius*sc:.1f}" fill="none" stroke="{col}" stroke-width="{w}"/>')
            elif t == 'ARC':
                c = e.dxf.center; a1, a2 = e.dxf.start_angle, e.dxf.end_angle
                if a2 < a1: a2 += 360
                pts = [(c.x+e.dxf.radius*math.cos(math.radians(a1+(a2-a1)*i/24)),
                        c.y+e.dxf.radius*math.sin(math.radians(a1+(a2-a1)*i/24))) for i in range(25)]
                p.append(f'<polyline fill="none" stroke="{col}" stroke-width="{w}" points="{" ".join(f"{X(a):.1f},{Y(b):.1f}" for a,b in pts)}"/>')
            elif t == 'TEXT':
                tx = un(e.dxf.text)
                al, pt, _pt2 = e.get_placement()
                rot = float(getattr(e.dxf, 'rotation', 0) or 0)
                name = al.name
                if name in ('MIDDLE', 'MIDDLE_CENTER') or name.endswith('_CENTER'):
                    anchor = 'middle'
                elif name.endswith('_RIGHT'):
                    anchor = 'end'
                else:
                    anchor = 'start'
                dy = 0.0
                if name.startswith('TOP_'):
                    dy = fs * 0.8
                elif name.startswith('MIDDLE_') or name == 'MIDDLE':
                    dy = fs * 0.35
                x, y = X(pt.x), Y(pt.y) + dy
                # DXF 角度为逆时针；SVG y 轴向下，故取负号，绕对齐点旋转
                tr = f' transform="rotate({-rot:.0f} {x:.1f} {y:.1f})"' if rot else ''
                p.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{fs}" fill="{col}" text-anchor="{anchor}"'
                         f' font-weight="{"bold" if col!="#333" else "normal"}"{tr}>{xml_esc(tx[:30])}</text>')
            elif t == 'MTEXT':
                tx = mtext_plain(un(str(e.dxf.text)))
                ip = e.dxf.insert
                ch = float(getattr(e.dxf, 'char_height', fs) or fs)
                # MTEXT 的方向由 11 组码方向向量决定（竖排文字 rotation=0 但向量朝上），
                # 必须用 get_rotation() 而非 dxf.rotation
                rot = float(e.get_rotation() or 0)
                ap = int(getattr(e.dxf, 'attachment_point', 1) or 1)
                lines = [ln.strip() for ln in tx.split('\n')]
                lines = [ln for ln in lines if ln]
                if not lines:
                    continue
                n = len(lines)
                lh = ch * 1.4
                # 附件点：1-3顶行、4-6中行、7-9底行；列：左/中/右
                if ap <= 3:
                    top = ip.y
                elif ap <= 6:
                    top = ip.y + n * lh / 2
                else:
                    top = ip.y + n * lh
                col_i = (ap - 1) % 3
                anchor = ('start', 'middle', 'end')[col_i]
                a = math.radians(rot)
                ca, sa = math.cos(a), math.sin(a)
                piv_x, piv_y = X(ip.x), Y(ip.y)
                for i, ln in enumerate(lines):
                    px, py = ip.x, top - ch * 0.8 - i * lh
                    rx = ip.x + (px - ip.x) * ca - (py - ip.y) * sa
                    ry = ip.y + (px - ip.x) * sa + (py - ip.y) * ca
                    x, y = X(rx), Y(ry)
                    tr = f' transform="rotate({-rot:.0f} {piv_x:.1f} {piv_y:.1f})"' if rot else ''
                    p.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{fs}" fill="{col}" text-anchor="{anchor}"'
                             f' font-weight="{"bold" if col!="#333" else "normal"}"{tr}>{xml_esc(ln[:40])}</text>')
            elif t == 'SOLID':
                pts = [e.dxf.vtx0, e.dxf.vtx1, e.dxf.vtx2, e.dxf.vtx3]
                p.append(f'<polygon fill="{col}" points="{" ".join(f"{X(a.x):.1f},{Y(a.y):.1f}" for a in pts)}"/>')
        except Exception:
            pass
    p.append('</svg>')
    Path(out).write_text('\n'.join(p), encoding='utf-8')
    import xml.dom.minidom as m; m.parse(str(out))
    return len(list(msp))


if __name__ == "__main__":
    o = ROOT / "output"
    render(o / "TD50-40G_真实基线_clean.dxf", o / "对照_几何改前.svg", "修改前 - 原始 TD50-40G")
    # after 对照图用带彩色尺寸标注的副本渲染（交付 DWG 本身仍是无标注干净版）
    import geometry_modifier
    annotated = o / "TD50-40G_几何修改_标注版.dxf"
    geometry_modifier.apply(o / "TD50-40G_真实基线_clean.dxf", annotated, annotate=True)
    render(annotated, o / "对照_几何改后.svg",
           "修改后 - H2=443 / D2=294 / 轴伸E+20（彩色为参考图风格尺寸标注，交付DWG无彩色）",
           baseline=str(o / "TD50-40G_真实基线_clean.dxf"))
    print("compare generated")

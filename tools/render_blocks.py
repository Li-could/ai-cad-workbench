"""渲染 25QYL DXF 为 PNG 预览（展开 INSERT 块，含标注文字）。"""
from __future__ import annotations
import math, sys, subprocess
from pathlib import Path
import ezdxf

ROOT = Path(__file__).resolve().parent.parent


def esc(s): return s.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')


def render(src, out_svg, ann_color="#c62828", W=1500, H=1000):
    d = ezdxf.readfile(str(src))
    msp = d.modelspace()
    x0, x1, y0, y1 = 0, 300, 68, 195
    sc = min((W-40)/(x1-x0), (H-40)/(y1-y0))
    ox = 20 - x0*sc
    oy = H-20 + y0*sc
    X = lambda v: ox + v*sc
    Y = lambda v: oy - v*sc
    p = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">'%(W,H,W,H),
         '<rect width="100%" height="100%" fill="#fff"/>']

    def draw(e, is_ann=False):
        t=e.dxftype()
        try:
            if t=='LINE':
                s,en=e.dxf.start,e.dxf.end
                col = ann_color if is_ann else '#1a1a1a'
                p.append(f'<line x1="{X(s.x):.1f}" y1="{Y(s.y):.1f}" x2="{X(en.x):.1f}" y2="{Y(en.y):.1f}" stroke="{col}" stroke-width="1"/>')
            elif t=='ARC':
                c=e.dxf.center;r=e.dxf.radius
                xA=X(c.x)+r*sc*math.cos(math.radians(e.dxf.start_angle))
                yA=Y(c.y)-r*sc*math.sin(math.radians(e.dxf.start_angle))
                xB=X(c.x)+r*sc*math.cos(math.radians(e.dxf.end_angle))
                yB=Y(c.y)-r*sc*math.sin(math.radians(e.dxf.end_angle))
                laf=1 if (e.dxf.end_angle-e.dxf.start_angle)%360>180 else 0
                p.append(f'<path d="M {xA:.1f} {yA:.1f} A {r*sc:.1f} {r*sc:.1f} 0 {laf} 1 {xB:.1f} {yB:.1f}" fill="none" stroke="#1a1a1a" stroke-width="1"/>')
            elif t=='CIRCLE':
                c=e.dxf.center;r=e.dxf.radius
                p.append(f'<circle cx="{X(c.x):.1f}" cy="{Y(c.y):.1f}" r="{r*sc:.1f}" fill="none" stroke="#1a1a1a" stroke-width="1"/>')
            elif t in ('TEXT','MTEXT'):
                ins=e.dxf.insert
                if not (x0<=ins.x<=x1 and y0<=ins.y<=y1): return
                s=e.dxf.text if t=='TEXT' else e.text
                import re
                s=re.sub(r'\\U\+([0-9A-Fa-f]{4})',lambda m:chr(int(m.group(1),16)),str(s))
                s=re.sub(r'\\[A-Za-z][^;]*;','',s)
                rot=e.dxf.get('rotation',0) or 0
                h=max(11,(e.dxf.get('height',2.5) if t=='TEXT' else e.dxf.get('char_height',2.5))*sc*1.1)
                col=ann_color if is_ann else '#111'
                p.append(f'<text x="{X(ins.x):.1f}" y="{Y(ins.y):.1f}" font-size="{h:.1f}" fill="{col}" font-family="Arial" transform="rotate({-rot},{X(ins.x):.1f},{Y(ins.y):.1f})">{esc(s)}</text>')
        except Exception as ex:
            pass

    for e in msp:
        if e.dxftype()=='INSERT':
            try:
                for ve in e.virtual_entities():
                    draw(ve, is_ann=True)
            except Exception:
                pass
        elif e.dxftype()=='DIMENSION':
            # 绘制尺寸线
            try:
                for ve in e.virtual_entities():
                    draw(ve, is_ann=True)
            except Exception:
                pass
        else:
            draw(e)
    p.append('</svg>')
    Path(out_svg).write_text('\n'.join(p), encoding='utf-8')


def svg_to_png(svg, png):
    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    import shutil
    if not Path(chrome).exists():
        chrome = shutil.which('chrome') or shutil.which('chrome.exe') or chrome
    subprocess.run([chrome,'--headless','--disable-gpu','--hide-scrollbars',
                    f'--screenshot={Path(png).resolve()}','--window-size=1500,1000',
                    Path(svg).resolve().as_uri()], capture_output=True, timeout=60)


if __name__=='__main__':
    src=sys.argv[1]; base=sys.argv[2]
    render(src, ROOT/'output'/f'{base}.svg')
    svg_to_png(ROOT/'output'/f'{base}.svg', ROOT/'output'/f'{base}.png')
    print('ok', base)

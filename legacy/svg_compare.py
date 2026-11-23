"""生成改前/改后对照 SVG，供工作台网页直观预览三处修改。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"

FLANGE_Y = 300
BASE_Y = 120


def make_svg(old=(418, 281, 0), new=(443, 294, 20)) -> str:
    SX = lambda x: 430 + x * 0.62
    SY = lambda y: 660 - y * 0.62
    oh, od, osh = old
    nh, nd, nsh = new

    def part(h, d, sh, col, dash="", op=1.0):
        top_y = FLANGE_Y + h
        cover_bottom = top_y - 55
        cw, mw = 297 / 2, 210 / 2
        s = []
        st = f'stroke="{col}" stroke-width="2" fill="none" stroke-dasharray="{dash}" opacity="{op}"'
        # 风罩
        s.append(f'<rect x="{SX(-cw):.1f}" y="{SY(top_y):.1f}" width="{2*cw*0.62:.1f}" height="{55*0.62:.1f}" {st}/>')
        # 电机
        s.append(f'<rect x="{SX(-mw):.1f}" y="{SY(cover_bottom):.1f}" width="{2*mw*0.62:.1f}" height="{(cover_bottom-(FLANGE_Y+10))*0.62:.1f}" {st}/>')
        for x in range(-int(mw) + 12, int(mw), 14):
            s.append(f'<line x1="{SX(x):.1f}" y1="{SY(cover_bottom):.1f}" x2="{SX(x):.1f}" y2="{SY(FLANGE_Y+14):.1f}" stroke="{col}" stroke-width="1" opacity="0.5"/>')
        # 接线盒
        by = cover_bottom - 120
        s.append(f'<rect x="{SX(mw):.1f}" y="{SY(by+90):.1f}" width="{(d-mw)*0.62:.1f}" height="{90*0.62:.1f}" {st}/>')
        # 轴伸
        s.append(f'<rect x="{SX(-16):.1f}" y="{SY(FLANGE_Y+30):.1f}" width="{32*0.62:.1f}" height="{(30+sh)*0.62:.1f}" {st}/>')
        return "\n".join(s)

    # 泵体/基础（共用，灰色）
    cy = BASE_Y + 35 + 95
    base = f'''
    <rect x="{SX(-217.5):.1f}" y="{SY(0):.1f}" width="{435*0.62:.1f}" height="{140*0.62:.1f}" fill="#eef1f5" stroke="#9aa4b2" stroke-width="1"/>
    <rect x="{SX(-117.5):.1f}" y="{SY(BASE_Y+35):.1f}" width="{235*0.62:.1f}" height="{35*0.62:.1f}" fill="#dfe4ea" stroke="#6b7280" stroke-width="1.5"/>
    <circle cx="{SX(0):.1f}" cy="{SY(cy):.1f}" r="{95*0.62:.1f}" fill="#f4f6f9" stroke="#6b7280" stroke-width="1.5"/>
    <circle cx="{SX(0):.1f}" cy="{SY(cy):.1f}" r="{78*0.62:.1f}" fill="none" stroke="#9aa4b2" stroke-width="1"/>
    <rect x="{SX(-120):.1f}" y="{SY(FLANGE_Y+10):.1f}" width="{240*0.62:.1f}" height="{20*0.62:.1f}" fill="#dfe4ea" stroke="#6b7280" stroke-width="1.5"/>
    <line x1="{SX(0):.1f}" y1="{SY(BASE_Y-30):.1f}" x2="{SX(0):.1f}" y2="{SY(FLANGE_Y+nh+40):.1f}" stroke="#3b82f6" stroke-width="1" stroke-dasharray="6 5" opacity="0.6"/>
    <text x="{SX(0)+8:.1f}" y="{SY(BASE_Y-20):.1f}" font-size="12" fill="#3b82f6">电机轴线</text>
    '''

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 760" width="100%" style="font-family:'Microsoft YaHei',sans-serif">
  <rect x="0" y="0" width="900" height="760" fill="#ffffff"/>
  <text x="30" y="42" font-size="22" font-weight="700" fill="#1f2430">TD50-40G 改前 / 改后对照</text>
  <text x="30" y="68" font-size="13" fill="#5b6472">灰色虚线 = 修改前 · 彩色实线 = 修改后（CAD 成果以 DXF 为准）</text>
  {base}
  {part(oh, od, osh, "#9aa4b2", dash="7 6", op=0.9)}
  {part(nh, nd, nsh, "#2563eb")}
  <!-- 风罩顶 H2 变化（红） -->
  <line x1="{SX(-cw if False else -297/2):.1f}" y1="{SY(FLANGE_Y+oh):.1f}" x2="{SX(-297/2):.1f}" y2="{SY(FLANGE_Y+nh):.1f}" stroke="#dc2626" stroke-width="3"/>
  <line x1="{SX(-297/2)-25:.1f}" y1="{SY(FLANGE_Y+oh):.1f}" x2="{SX(-297/2)+25:.1f}" y2="{SY(FLANGE_Y+oh):.1f}" stroke="#dc2626" stroke-width="2"/>
  <line x1="{SX(-297/2)-25:.1f}" y1="{SY(FLANGE_Y+nh):.1f}" x2="{SX(-297/2)+25:.1f}" y2="{SY(FLANGE_Y+nh):.1f}" stroke="#dc2626" stroke-width="2"/>
  <text x="70" y="{SY((FLANGE_Y+oh+FLANGE_Y+nh)/2):.1f}" font-size="16" font-weight="700" fill="#dc2626">① H2: {oh} → {nh} mm</text>
  <text x="70" y="{SY((FLANGE_Y+oh+FLANGE_Y+nh)/2)+20:.1f}" font-size="12" fill="#dc2626">罩壳轴向变形，机身/安装面/绿罩不动</text>
  <!-- 接线盒 D2 变化（蓝） -->
  <line x1="{SX(od):.1f}" y1="{SY(cy):.1f}" x2="{SX(nd):.1f}" y2="{SY(cy):.1f}" stroke="#2563eb" stroke-width="3"/>
  <text x="{SX(nd)+12:.1f}" y="{SY(cy)-6:.1f}" font-size="15" font-weight="700" fill="#2563eb">② D2: {od} → {nd}</text>
  <text x="{SX(nd)+12:.1f}" y="{SY(cy)+14:.1f}" font-size="11" fill="#2563eb">外盖径向随动（297非D2）</text>
  <!-- 轴伸（橙） -->
  <line x1="{SX(16):.1f}" y1="{SY(FLANGE_Y):.1f}" x2="{SX(16):.1f}" y2="{SY(FLANGE_Y-nsh):.1f}" stroke="#ea580c" stroke-width="3"/>
  <text x="{SX(16)+14:.1f}" y="{SY(FLANGE_Y-nsh/2):.1f}" font-size="14" font-weight="700" fill="#ea580c">③ 轴伸 +{nsh}</text>
  <!-- 图例 -->
  <rect x="700" y="640" width="18" height="4" fill="#9aa4b2"/><text x="726" y="647" font-size="12" fill="#5b6472">修改前</text>
  <rect x="700" y="662" width="18" height="4" fill="#2563eb"/><text x="726" y="669" font-size="12" fill="#5b6472">修改后</text>
  <text x="30" y="735" font-size="11" fill="#94a3b8">示意对照：真实改变量须从原CAD实测；正式替换原DWG前请人工复核。</text>
</svg>'''
    return svg


def generate():
    OUTPUT.mkdir(exist_ok=True)
    p = OUTPUT / "compare.svg"
    p.write_text(make_svg(), encoding="utf-8")
    return p


if __name__ == "__main__":
    print("生成:", generate())

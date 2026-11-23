"""泵安装工程图 DXF 生成器（零第三方依赖）。

生成 TD50-40G 立式管道泵安装图，参数化体现三处变更：
  - H2 (安装法兰贴合面 -> 风罩最高点) 目标 443
  - D2 (电机轴线 -> 接线盒最外端) 目标 294
  - 轴伸 = 江潮标准轴伸 E + 20

输出标准 DXF R2000 (AC1015)，可在 AutoCAD / 中望 / 浩辰 / LibreCAD 直接打开。
中文标注使用 AutoCAD 通用的 \\U+XXXX 转义，确保各版本 CAD 正常显示。

同时生成一张 SVG 对照图，供工作台网页预览（修改部分按红/蓝/橙高亮）。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"

# ---------- ACI 颜色 ----------
RED, BLUE, ORANGE, GREY, BLACK, GREEN = 1, 5, 30, 8, 7, 3

LAYERS = [
    ("轮廓", BLACK), ("中心线", 4), ("尺寸", BLACK), ("文字", BLACK),
    ("修改_H2", RED), ("修改_D2", BLUE), ("修改_轴伸", ORANGE),
    ("原位置参考", GREY), ("混凝土", 9),
]


def esc(s: str) -> str:
    """把中文转成 AutoCAD 的 \\U+XXXX 转义；ASCII 原样保留。"""
    out = []
    for ch in s:
        cp = ord(ch)
        if 32 <= cp < 127:
            out.append(ch)
        else:
            out.append(f"\\U+{cp:04X}")
    return "".join(out)


class DXF:
    def __init__(self):
        self.ents: list[str] = []

    def _p(self, code, value):
        self.ents.append(f"{code}\n{value}\n")

    def line(self, x1, y1, x2, y2, layer="轮廓", color=None):
        self._p(0, "LINE"); self._p(8, layer)
        if color: self._p(62, color)
        self._p(10, f"{x1:.3f}"); self._p(20, f"{y1:.3f}"); self._p(30, 0)
        self._p(11, f"{x2:.3f}"); self._p(21, f"{y2:.3f}"); self._p(31, 0)

    def rect(self, x1, y1, x2, y2, layer="轮廓", color=None):
        self.line(x1, y1, x2, y1, layer, color)
        self.line(x2, y1, x2, y2, layer, color)
        self.line(x2, y2, x1, y2, layer, color)
        self.line(x1, y2, x1, y1, layer, color)

    def circle(self, cx, cy, r, layer="轮廓", color=None):
        self._p(0, "CIRCLE"); self._p(8, layer)
        if color: self._p(62, color)
        self._p(10, f"{cx:.3f}"); self._p(20, f"{cy:.3f}"); self._p(30, 0)
        self._p(40, f"{r:.3f}")

    def arc(self, cx, cy, r, a1, a2, layer="轮廓", color=None):
        self._p(0, "ARC"); self._p(8, layer)
        if color: self._p(62, color)
        self._p(10, f"{cx:.3f}"); self._p(20, f"{cy:.3f}"); self._p(30, 0)
        self._p(40, f"{r:.3f}"); self._p(50, f"{a1:.3f}"); self._p(51, f"{a2:.3f}")

    def text(self, s, x, y, h=14, layer="文字", color=None, rot=0):
        self._p(0, "TEXT"); self._p(8, layer)
        if color: self._p(62, color)
        self._p(10, f"{x:.3f}"); self._p(20, f"{y:.3f}"); self._p(30, 0)
        self._p(40, f"{h:.3f}"); self._p(1, esc(s)); self._p(50, f"{rot:.3f}")
        self._p(7, "STANDARD")

    def dim_v(self, x, y1, y2, label, layer="尺寸", color=None, ext=12, txt_h=12):
        """竖直尺寸线 + 箭头 + 文字（旋转90）。"""
        self.line(x - ext, y1, x + ext, y1, layer, color)
        self.line(x - ext, y2, x + ext, y2, layer, color)
        self.line(x, y1, x, y2, layer, color)
        a = min(9, (y2 - y1) / 4)
        self.line(x, y1, x - 3, y1 + a, layer, color); self.line(x, y1, x + 3, y1 + a, layer, color)
        self.line(x, y2, x - 3, y2 - a, layer, color); self.line(x, y2, x + 3, y2 - a, layer, color)
        self.text(label, x - 3, (y1 + y2) / 2, txt_h, layer, color, rot=90)

    def dim_h(self, y, x1, x2, label, layer="尺寸", color=None, ext=12, txt_h=12):
        self.line(x1, y - ext, x1, y + ext, layer, color)
        self.line(x2, y - ext, x2, y + ext, layer, color)
        self.line(x1, y, x2, y, layer, color)
        a = min(9, (x2 - x1) / 4)
        self.line(x1, y, x1 + a, y - 3, layer, color); self.line(x1, y, x1 + a, y + 3, layer, color)
        self.line(x2, y, x2 - a, y - 3, layer, color); self.line(x2, y, x2 - a, y + 3, layer, color)
        self.text(label, (x1 + x2) / 2 - len(label) * 3, y + 6, txt_h, layer, color)

    def leader(self, x1, y1, x2, y2, label, layer="文字", color=None, txt_h=12):
        self.line(x1, y1, x2, y2, layer, color)
        self.text(label, x2 + 3, y2, txt_h, layer, color)

    def save(self, path: Path):
        # R2000 规范要求表、图层带句柄(组码5)与子类标记(组码100)。
        # 缺失句柄时 ezdxf/部分 CAD 会报 "invalid table head handle"，故在此补齐。
        head = (
            "0\nSECTION\n2\nHEADER\n"
            "9\n$ACADVER\n1\nAC1015\n"
            "9\n$HANDSEED\n5\nFFFF\n"
            "9\n$INSUNITS\n70\n4\n"
            "9\n$LUNITS\n70\n2\n"
            "0\nENDSEC\n"
            "0\nSECTION\n2\nTABLES\n"
            "0\nTABLE\n2\nLAYER\n5\n2\n100\nAcDbSymbolTable\n70\n" + str(len(LAYERS)) + "\n"
        )
        for i, (name, col) in enumerate(LAYERS):
            h = f"{0x20 + i:X}"  # 图层句柄 20,21,22...
            head += (
                f"0\nLAYER\n5\n{h}\n100\nAcDbSymbolTableRecord\n"
                f"100\nAcDbLayerTableRecord\n2\n{esc(name)}\n70\n0\n62\n{col}\n6\nCONTINUOUS\n"
            )
        head += "0\nENDTAB\n0\nENDSEC\n"
        body = "0\nSECTION\n2\nENTITIES\n" + "".join(self.ents) + "0\nENDSEC\n0\nEOF\n"
        path.write_text(head + body, encoding="utf-8")


# ---------------- 图纸几何 ----------------
def draw_frame(d: DXF, W=1200, H=900, title="TD50-40G 立式管道泵安装图（修改后）"):
    d.rect(15, 15, W - 15, H - 15, "轮廓")
    d.rect(25, 25, W - 25, H - 25, "轮廓")
    # 标题栏
    d.rect(W - 420, 25, W - 25, 130, "轮廓")
    d.line(W - 420, 75, W - 25, 75, "轮廓")
    d.line(W - 260, 25, W - 260, 130, "轮廓")
    d.text(title, W - 412, 100, 16, "文字")
    d.text("修改: H2=443 / D2=294 / 轴伸 E+20", W - 412, 55, 13, "文字")
    d.text("比例 1:10", W - 252, 100, 13, "文字")
    d.text("单位 mm", W - 252, 60, 13, "文字")
    d.text("阶段1 AI改图成果（示意重建，替换原DWG前请人工复核）", 40, 40, 12, "文字")


def draw_concrete(d: DXF, y0=120, depth=140, half=217.5):
    """混凝土基础剖面（简化）。"""
    d.rect(-half, y0 - depth, half, y0, "混凝土", GREY)
    for x in range(-int(half) + 10, int(half), 26):
        d.line(x, y0 - depth, x + 20, y0, "混凝土", GREY)
    d.text("推荐混凝土基础 435x435x550", -half, y0 - depth - 26, 13, "文字")


def draw_pump_body(d: DXF, base_y, flange_y):
    """泵体（蜗壳）+ 底板 + 法兰。"""
    # 底板 235 宽, 35 厚
    d.rect(-117.5, base_y, 117.5, base_y + 35, "轮廓")
    # 蜗壳
    cy = base_y + 35 + 95
    d.circle(0, cy, 95, "轮廓")
    d.circle(0, cy, 78, "轮廓")
    d.arc(0, cy, 86, 200, 340, "轮廓")
    # 出口法兰（向右）
    d.rect(78, cy - 40, 210, cy + 40, "轮廓")
    d.rect(205, cy - 52, 215, cy + 52, "轮廓")
    # 安装法兰贴合面（H2 基准）
    d.rect(-120, flange_y - 10, 120, flange_y + 10, "轮廓")
    d.line(-135, flange_y, 135, flange_y, "中心线", 4)
    for x in [-100, 100]:
        d.circle(x, flange_y, 6, "轮廓")
    return cy


def draw_motor_and_cover(d: DXF, flange_y, h2, motor_w=210, cover_h=55):
    """电机 + 风罩。风罩顶 = flange_y + h2。"""
    top = flange_y + h2
    cover_bottom = top - cover_h
    motor_w2 = motor_w / 2
    cover_w2 = 297 / 2
    # 电机本体
    d.rect(-motor_w2, flange_y + 10, motor_w2, cover_bottom, "轮廓")
    for x in range(-int(motor_w2) + 12, int(motor_w2), 14):
        d.line(x, flange_y + 14, x, cover_bottom - 6, "轮廓")
    # 风罩
    d.rect(-cover_w2, cover_bottom, cover_w2, top, "轮廓")
    for x in range(-int(cover_w2) + 8, int(cover_w2), 12):
        d.line(x, cover_bottom, x, top, "轮廓")
    return top, cover_bottom, cover_w2, motor_w2


def draw_terminal_box(d: DXF, flange_y, cover_bottom, d2, motor_w2, layer="轮廓", color=None):
    """接线盒：左端接电机右壁，右端到 x=d2。"""
    box_y = cover_bottom - 120
    d.rect(motor_w2, box_y, d2, box_y + 90, layer, color)
    d.circle(d2 - 10, box_y + 45, 5, layer, color)


def draw_shaft(d: DXF, flange_y, cy, ext, layer="轮廓", color=None):
    """轴伸：从安装法兰面向下（泵侧）延伸 ext。"""
    d.rect(-16, flange_y - ext, 16, flange_y + 30, layer, color)
    # 键槽
    d.rect(-5, flange_y - ext + 4, 5, flange_y - ext + 30, "轮廓")


def draw_top_view(d: DXF, ox, oy):
    """俯视图：基础 + 底板 + 地脚孔。"""
    d.rect(ox - 217.5, oy - 217.5, ox + 217.5, oy + 217.5, "轮廓")
    d.rect(ox - 117.5, oy - 117.5, ox + 117.5, oy + 117.5, "轮廓")
    d.circle(ox, oy, 95, "轮廓")
    d.circle(ox, oy, 30, "轮廓")
    for sx in (-1, 1):
        for sy in (-1, 1):
            d.circle(ox + sx * 97.5, oy + sy * 97.5, 7, "轮廓")
    d.text("基础平面尺寸", ox - 90, oy + 250, 13, "文字")
    d.text("4-14 地脚孔 / 孔距195", ox - 120, oy - 250, 12, "文字")


def build(moded: str, h2: int, d2: int, shaft_ext: int, old_h2=None, old_d2=None) -> DXF:
    d = DXF()
    draw_frame(d, title=f"TD50-40G 立式管道泵安装图（{moded}）")
    base_y = 120
    flange_y = 300
    # 基础与泵体
    draw_concrete(d, y0=base_y, depth=140)
    cy = draw_pump_body(d, base_y, flange_y)
    # 修改前参考轮廓（灰色虚线感：用灰色实线简化）
    if old_h2 is not None:
        d.rect(-297 / 2, flange_y + old_h2 - 55, 297 / 2, flange_y + old_h2, "原位置参考", GREY)
        d.text(f"改前 H2={old_h2}", -297 / 2 - 120, flange_y + old_h2, 11, "原位置参考", GREY)
    if old_d2 is not None:
        d.line(old_d2, flange_y - 130, old_d2, flange_y - 20, "原位置参考", GREY)
        d.text(f"改前 D2={old_d2}", old_d2 + 4, flange_y - 130, 11, "原位置参考", GREY)

    # 电机 + 风罩（H2 修改部分用红色图层强调顶部）
    top, cover_bottom, cover_w2, motor_w2 = draw_motor_and_cover(d, flange_y, h2)
    d.line(-cover_w2, top, cover_w2, top, "修改_H2", RED)
    draw_terminal_box(d, flange_y, cover_bottom, d2, motor_w2,
                      layer="修改_D2" if moded == "修改后" else "轮廓",
                      color=BLUE if moded == "修改后" else None)
    draw_shaft(d, flange_y, cy, shaft_ext,
               layer="修改_轴伸" if moded == "修改后" else "轮廓",
               color=ORANGE if moded == "修改后" else None)

    # 轴线
    d.line(0, base_y - 60, 0, top + 40, "中心线", 4)
    d.text("电机轴线", 6, base_y - 50, 11, "中心线", 4)

    # 尺寸标注
    d.dim_v(-370, flange_y, top, f"H2 = {h2}", layer="修改_H2" if moded == "修改后" else "尺寸",
            color=RED if moded == "修改后" else None)
    d.dim_h(top + 45, -cover_w2, cover_w2, "风罩宽 297", layer="尺寸")
    d.dim_h(cover_bottom - 150, 0, d2, f"D2 = {d2}", layer="修改_D2" if moded == "修改后" else "尺寸",
            color=BLUE if moded == "修改后" else None)
    d.text(f"轴伸 = E + {shaft_ext}", 30, flange_y - shaft_ext - 20, 13,
           "修改_轴伸" if moded == "修改后" else "文字",
           ORANGE if moded == "修改后" else None)
    d.dim_v(300, base_y, base_y + 35, "35", layer="尺寸")
    d.dim_h(base_y - 30, -117.5, 117.5, "235 底板", layer="尺寸")
    d.dim_h(base_y - 60, -217.5, 217.5, "435 基础", layer="尺寸")

    # 引线注释（三条变更要点）
    d.leader(cover_w2 - 10, top - 20, cover_w2 + 40, top + 80, "① H2: 罩壳轴向变形, 机身/安装面不动",
             layer="修改_H2", color=RED)
    d.leader(d2 - 5, cover_bottom - 90, d2 + 30, cover_bottom - 60, "② D2: 外盖径向随动, 内部件不动 (297非D2)",
             layer="修改_D2", color=BLUE)
    d.leader(16, flange_y - shaft_ext, 60, flange_y - shaft_ext + 40, "③ 轴伸+20, 键槽/法兰不动, 不加到H2",
             layer="修改_轴伸", color=ORANGE)

    # 俯视图
    draw_top_view(d, ox=-640, oy=430)
    return d


def generate_all():
    OUTPUT.mkdir(exist_ok=True)
    # 修改前（示意原值）
    before = build("修改前", h2=418, d2=281, shaft_ext=0)
    p_before = OUTPUT / "TD50-40G_修改前.dxf"
    before.save(p_before)
    # 修改后（目标值），叠加改前参考
    after = build("修改后", h2=443, d2=294, shaft_ext=20, old_h2=418, old_d2=281)
    p_after = OUTPUT / "TD50-40G_修改后.dxf"
    after.save(p_after)
    # 变更日志
    log = {
        "drawing": "TD50-40G.dwg",
        "changes": [
            {"rule": "H2_AXIAL_HEIGHT", "from": 418, "to": 443, "delta": 25, "op": "deform",
             "note": "示意原值418，真实当前值须从原CAD实测；443是总高非风罩长度"},
            {"rule": "D2_RADIAL_TERMINAL_BOX", "from": 281, "to": 294, "delta": 13, "op": "move",
             "note": "图上297不是D2，不得替换；真实当前值须实测"},
            {"rule": "SHAFT_EXT_PLUS20", "base": "江潮标准轴伸E", "add": 20, "op": "extend",
             "note": "键槽/法兰不动；+20不加到H2；轴伸E需电机图确认"},
        ],
        "outputs": [str(p_before), str(p_after)],
        "caveat": "本 DXF 为依据标注图参数化重建的示意成果，替换原DWG前须人工复核；正式精确修改需先把原DWG另存为DXF后在原图上改。",
    }
    (OUTPUT / "change_log.json").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    return p_before, p_after, log


if __name__ == "__main__":
    b, a, l = generate_all()
    print("生成:", b)
    print("生成:", a)
    print("日志:", OUTPUT / "change_log.json")

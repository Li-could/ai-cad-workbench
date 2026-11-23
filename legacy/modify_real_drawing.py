"""在真实 TD50-40G 图纸上执行三条变更标注（方案 5/6/8 章）。

输入：clean 后的真实 DXF（由原 DWG 经 SDK 导出 + 清洗）
输出：修改后的 DXF —— 保留原图所有 2873 个实体几何不动，叠加三条变更标注与引线。

三条变更（依据参考图与知识卡）：
- ① 红 H2=443：安装法兰面→风罩顶总高
- ② 蓝 D2=294：电机轴线→接线盒最外端（原图 297 保留但注明不是 D2）
- ③ 橙 轴伸 E+20：轴端向泵侧伸出 20

按方案 1.1「保留原有设计」：原图几何、原尺寸数字全部保留，不伪造改后轮廓；
变更以彩色标注 + 引线 + 说明框呈现，便于 CAXA/SolidWorks 打开后核对与继续编辑。
"""
from __future__ import annotations

import sys
from pathlib import Path

import ezdxf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# 颜色（ACI）
RED, BLUE, ORANGE, GREEN = 1, 5, 30, 3

# 从真实图纸量得的关键坐标（原图 297 标注位于 handle C58, insert=(51153,1521)）
# 接线盒/风罩/电机区域大约在 X:50800-51600, Y:700-1600
ANNOT_X = 53300  # 标注区放在图纸右侧空白
ANNOT_Y_TOP = 1700


def ensure_layer(doc, name: str, color: int):
    if name not in doc.layers:
        doc.layers.add(name, color=color)
    return name


def add_text(msp, text, x, y, h, layer, color, rot=0):
    # 中文用 \U+ 转义，确保 CAXA/SolidWorks/各版本 CAD 正确显示
    esc = "".join(ch if 32 <= ord(ch) < 127 else f"\\U+{ord(ch):04X}" for ch in text)
    e = msp.add_text(esc, height=h, dxfattribs={"layer": layer, "color": color, "rotation": rot})
    e.set_placement((x, y))
    return e


def add_line(msp, x1, y1, x2, y2, layer, color):
    msp.add_line((x1, y1), (x2, y2), dxfattribs={"layer": layer, "color": color})


def add_box(msp, x1, y1, x2, y2, layer, color):
    add_line(msp, x1, y1, x2, y1, layer, color)
    add_line(msp, x2, y1, x2, y2, layer, color)
    add_line(msp, x2, y2, x1, y2, layer, color)
    add_line(msp, x1, y2, x1, y1, layer, color)


def add_arrow_v(msp, x, y1, y2, layer, color):
    """竖直尺寸线 + 箭头（指向 y1,y2）。"""
    add_line(msp, x, y1, x, y2, layer, color)
    a = min(60, (y2 - y1) / 4)
    add_line(msp, x, y1, x - 18, y1 + a, layer, color)
    add_line(msp, x, y1, x + 18, y1 + a, layer, color)
    add_line(msp, x, y2, x - 18, y2 - a, layer, color)
    add_line(msp, x, y2, x + 18, y2 - a, layer, color)


def add_arrow_h(msp, y, x1, x2, layer, color):
    """水平尺寸线 + 箭头。"""
    add_line(msp, x1, y, x2, y, layer, color)
    a = min(60, (x2 - x1) / 4)
    add_line(msp, x1, y, x1 + a, y - 18, layer, color)
    add_line(msp, x1, y, x1 + a, y + 18, layer, color)
    add_line(msp, x2, y, x2 - a, y - 18, layer, color)
    add_line(msp, x2, y, x2 - a, y + 18, layer, color)


def modify(src: str | Path, dst: str | Path) -> dict:
    src, dst = Path(src), Path(dst)
    doc = ezdxf.readfile(str(src))
    msp = doc.modelspace()
    before_count = len(list(msp))

    lh = ensure_layer(doc, "变更_H2_红色", RED)
    ld = ensure_layer(doc, "变更_D2_蓝色", BLUE)
    ls = ensure_layer(doc, "变更_轴伸_橙色", ORANGE)
    ln = ensure_layer(doc, "变更_说明", 7)

    # ===== ① H2=443（红色，竖直尺寸线）=====
    # 风罩顶附近 Y≈1520，安装法兰面附近 Y≈600（原图 770 标注范围）
    h2_x = 50820
    add_arrow_v(msp, h2_x, 600, 1560, lh, RED)
    add_text(msp, "H2 = 443 mm", h2_x - 260, 1080, 95, lh, RED, rot=90)

    # ===== ② D2=294（蓝色，水平尺寸线）=====
    # 电机轴线（中心）到接线盒最外端；原 297 标注在 X≈51153,Y=1521
    d2_y = 1660
    add_arrow_h(msp, d2_y, 50600, 51420, ld, BLUE)
    add_text(msp, "D2 = 294 mm", 50760, d2_y + 90, 95, ld, BLUE)
    # 保留原 297 并加注记（方案禁令：297 不是 D2）
    add_text(msp, "注:原图297不是D2,不得替换", 50600, 1420, 70, ld, BLUE)

    # ===== ③ 轴伸 E+20（橙色）=====
    # 轴端位于泵体上方中心，此处用引线指向轴端区域并标注
    add_line(msp, 51200, 620, 51500, 900, ls, ORANGE)
    add_text(msp, "轴伸 = E + 20 mm (向泵侧)", 51510, 900, 85, ls, ORANGE)

    # ===== 右侧说明框（三条规则概要）=====
    bx, by = ANNOT_X, ANNOT_Y_TOP
    add_box(msp, bx, by - 1350, bx + 2400, by, ln, 7)
    add_text(msp, "变更说明 (AI 按参考图生成)", bx + 60, by - 130, 90, ln, 7)
    add_text(msp, "[1] H2: 法兰面到风罩顶 = 443mm; 罩壳本体轴向变形,机身/安装面不动", bx + 60, by - 330, 68, lh, RED)
    add_text(msp, "[2] D2: 电机轴线到接线盒最外端 = 294mm; 外盖随动,内部件不动", bx + 60, by - 500, 68, ld, BLUE)
    add_text(msp, "    注意: 原图297不是D2, 不得用它替换294", bx + 60, by - 620, 68, ld, BLUE)
    add_text(msp, "[3] 轴伸: E + 20mm 向泵侧伸出; 键槽保留", bx + 60, by - 790, 68, ls, ORANGE)
    add_text(msp, "    注意: +20 不加到H2, 不拉长虚线框内支撑/法兰", bx + 60, by - 910, 68, ls, ORANGE)
    add_text(msp, "原图几何与尺寸数字全部保留; 修改部分以彩色标注呈现", bx + 60, by - 1120, 60, ln, 8)

    dst.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(dst))

    after_count = len(list(ezdxf.readfile(str(dst)).modelspace()))
    return {
        "ok": True, "src": str(src), "dst": str(dst),
        "entities_before": before_count, "entities_after": after_count,
        "added": after_count - before_count,
        "layers_added": ["变更_H2_红色", "变更_D2_蓝色", "变更_轴伸_橙色", "变更_说明"],
        "note": "原图几何/尺寸保留，三条变更以彩色标注+引线呈现，CAXA/SolidWorks 可直接打开编辑",
    }


if __name__ == "__main__":
    import json
    r = modify("output/TD50-40G_真实基线_clean.dxf", "output/TD50-40G_修改后_真实图纸.dxf")
    print(json.dumps(r, ensure_ascii=False, indent=2))

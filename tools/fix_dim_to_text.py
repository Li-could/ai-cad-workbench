"""把两个原生 DIMENSION 的 MTEXT 文字替换为 rotation=90 的 TEXT。

根因：eDrawings 对 DIMENSION(TH_GBDIM 样式) 的 MTEXT 强制沿尺寸线竖排，
但把字符间距压扁（62.2 的 6/2/小数点叠在一起看不清）。
图上其它清晰竖排标注(36.4/70.6/53.6)都是 TEXT + rotation=90，
字符间距正常。故把这两处也改成同样的实体类型与方式。

做法：在 geometry 块 *X18/*X19 内删除 MTEXT，新建 TEXT：
  - text = 原数值
  - rotation = 90（真竖排）
  - height = 2.5（与图上其它标注统一）
  - insert = 尺寸线左侧 1.5（贴蓝线，且 72.1 远离 x=287 绿图框线）
DIMENSION 的尺寸线/箭头/图形主体一律不动。
"""
from __future__ import annotations
from pathlib import Path
import ezdxf

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "output" / "25QYL-2SL_1比1.dxf"
DST = ROOT / "output" / "25QYL-2SL_1比1_标注修复.dxf"

# handle -> (文字, TEXT插入点, 尺寸线x)
FIX = {
    "C52": ("62.2", (103.76, 130.42), 105.26),
    "C5F": ("72.1", (276.62, 135.39), 278.12),
}
HEIGHT = 2.5
OFFSET = 1.5  # 文字在尺寸线左侧距离


def main():
    doc = ezdxf.readfile(str(SRC))
    info = []
    for d in doc.modelspace().query("DIMENSION"):
        h = d.dxf.handle
        if h not in FIX:
            continue
        text, (tx, ty), dimx = FIX[h]
        # 文字位置 = 尺寸线左侧 1.5
        ins = (dimx - OFFSET, ty, 0.0)
        geom = d.dxf.get("geometry", "")
        if not geom or geom not in doc.blocks:
            continue
        blk = doc.blocks[geom]
        # 删除块内所有 MTEXT/TEXT
        for e in list(blk):
            if e.dxftype() in ("MTEXT", "TEXT"):
                blk.delete_entity(e)
        # 新增 TEXT rotation=90
        t = blk.add_text(
            text,
            height=HEIGHT,
            dxfattribs={"insert": ins, "rotation": 90.0, "layer": "0"},
        )
        # 同步 DIMENSION 的 text_midpoint
        d.dxf.text_midpoint = ins
        info.append((h, text, geom, ins))

    doc.saveas(str(DST))
    print("替换为 TEXT rot=90 明细：")
    for h, text, geom, ins in info:
        print(f"  {h} {text!r} 块={geom} insert={tuple(round(v,2) for v in ins)} rot=90 h={HEIGHT}")
    print(f"\n输出: {DST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

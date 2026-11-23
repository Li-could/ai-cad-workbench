"""修复 25QYL-2SL 两个竖向 DIMENSION 文字位置。

参照图中清晰的竖排标注（36.4/70.6/53.6，均在 GS_BlockDim 块内）：
  - 文字为 TEXT，rotation=90（竖排），字高 2.5
  - 文字 insert 位于尺寸线左侧约 1.5 单位（贴着蓝线，骑在线上但留缝）

问题标注：
  - C52 (62.2) 尺寸线 x=105.26 —— 上一版推到 x=99（离蓝线 6.26，飘太远）
  - C5F (72.1) 尺寸线 x=278.12 —— 上一版推到 x=285（压在 x≈290 的绿色图框线上）

本版：把两处文字贴回各自尺寸线左侧 1.5 单位，字高统一 2.5，
      与图中其它竖排标注风格一致；尺寸线/箭头/图形主体一律不动。

eDrawings 对 DIMENSION 的 MTEXT 强制沿尺寸线竖排显示，位置读取
DIMENSION.text_midpoint 与 geometry 块内 MTEXT.insert，两处同步。
"""
from __future__ import annotations
from pathlib import Path
import ezdxf

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "output" / "25QYL-2SL_1比1.dxf"
DST = ROOT / "output" / "25QYL-2SL_1比1_标注修复.dxf"

# 目标文字位置 (x, y)：贴尺寸线左侧 1.5 单位，y 取标注中点
# 62.2: 尺寸线 x=105.26 -> 文字 x=103.76
# 72.1: 尺寸线 x=278.12 -> 文字 x=276.62
FIX = {
    "C52": (103.76, 130.42),
    "C5F": (276.62, 135.39),
}
TEXT_HEIGHT = 2.5


def get_h(e):
    for k in ("char_height", "height"):
        try:
            return e.dxf.get(k)
        except Exception:
            pass
    return None


def set_h(e, val):
    for k in ("char_height", "height"):
        try:
            e.dxf.set(k, val)
            return
        except Exception:
            pass


def main():
    doc = ezdxf.readfile(str(SRC))
    info = []
    for d in doc.modelspace().query("DIMENSION"):
        h = d.dxf.handle
        if h not in FIX:
            continue
        tx, ty = FIX[h]
        old_mid = tuple(d.dxf.text_midpoint)
        geom = d.dxf.get("geometry", "")
        old_txt = None
        if geom and geom in doc.blocks:
            for e in doc.blocks[geom]:
                if e.dxftype() in ("MTEXT", "TEXT"):
                    old_txt = (e.dxf.text, tuple(e.dxf.insert), get_h(e))
                    e.dxf.insert = (tx, ty, 0)
                    set_h(e, TEXT_HEIGHT)
        d.dxf.text_midpoint = (tx, ty, 0)
        info.append((h, d.dxf.text, old_mid, old_txt, (tx, ty)))

    doc.saveas(str(DST))
    print("修复明细：")
    for h, text, old_mid, old_txt, new in info:
        print(f"  {h} text={text!r}")
        print(f"     text_mid {tuple(round(v, 2) for v in old_mid)} -> {new}")
        print(f"     文字原 {old_txt} -> insert={new} h={TEXT_HEIGHT}")
    print(f"\n输出: {DST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

"""25QYL-2SL：按全图最贴合的 1:4 标准比例统一标注，单位为 mm。
依据：14条标注中6条(293/291/284/255/220/150)比例落在4.0~4.12，中位数4.04≈4.0，
      符合机械制图1:4标准比例系列；610(4.23)、453(4.33)及85/60/28偏离，为原图标注错误。
规则：图形实体与图框完全不动；每条标注 = 图形实测跨度 × 4.0，四舍五入到整数mm；
      规格类(G1/G3/4/4-Φ11)不动。从 clean.dxf 原始基线出发。
"""
from __future__ import annotations
import json
from pathlib import Path
import ezdxf

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "output" / "25QYL-2SL_clean.dxf"
DST = ROOT / "output" / "25QYL-2SL_1比4标准版.dxf"
REPORT = ROOT / "output" / "25QYL-2SL_1比4标准版报告.json"

K = 4.0  # 1:4 标准绘图比例
UNIT = "mm"
SPEC_TEXTS = {"G1", "G3/4", "4-%%C11", "4-Φ11"}


def block_span(block):
    hs, vs = [], []
    for e in block:
        if e.dxftype() != "LINE":
            continue
        s, t = e.dxf.start, e.dxf.end
        if abs(s.y - t.y) < 0.01:
            hs.append(abs(t.x - s.x))
        if abs(s.x - t.x) < 0.01:
            vs.append(abs(t.y - s.y))
    return (max(hs) if hs else 0.0, max(vs) if vs else 0.0)


def fmt(v: float) -> str:
    return str(int(round(v)))


def main():
    doc = ezdxf.readfile(str(SRC))
    changes = []
    for b in doc.blocks:
        if not b.name.startswith("GS_BlockDim"):
            continue
        texts = [e for e in b if e.dxftype() == "TEXT"]
        if not texts:
            continue
        old = texts[0].dxf.text
        if old in SPEC_TEXTS:
            continue
        hspan, vspan = block_span(b)
        rot = texts[0].dxf.get("rotation", 0) or 0
        measured = vspan if abs(rot - 90) < 1 else hspan
        if measured <= 0:
            measured = max(hspan, vspan)
        new = fmt(measured * K)
        for t in texts:
            t.dxf.text = new
        changes.append({
            "kind": "BLOCK", "block": b.name, "old": old, "new": new,
            "measured": round(measured, 4), "scaled": round(measured * K, 4),
        })

    for d in doc.modelspace().query("DIMENSION"):
        old = d.dxf.text
        if old in SPEC_TEXTS:
            continue
        meas = float(d.get_measurement())
        new = fmt(meas * K)
        d.dxf.text = new
        geom = d.dxf.get("geometry", "")
        if geom and geom in doc.blocks:
            for e in doc.blocks[geom]:
                if e.dxftype() == "MTEXT":
                    e.text = new
                elif e.dxftype() == "TEXT":
                    e.dxf.text = new
        changes.append({
            "kind": "DIM", "handle": d.dxf.handle, "old": old, "new": new,
            "measured": round(meas, 4), "scaled": round(meas * K, 4),
            "geometry_block": geom,
        })

    doc.saveas(str(DST))
    REPORT.write_text(json.dumps({
        "source": str(SRC.relative_to(ROOT)),
        "output": str(DST.relative_to(ROOT)),
        "unit": UNIT,
        "rule": "全图最贴合1:4标准比例；图形与图框完全不动；标注=实测跨度×4取整到mm；规格类不动",
        "base": {"ratio": "1:4", "K": K, "median_of_all": 4.0356},
        "changes": changes,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"单位 {UNIT}  比例 1:4  K={K}\n")
    print(f"{'类型':6s} {'块/句柄':22s} {'原值':>6s} {'实测':>9s} {'新值mm':>8s}")
    for c in changes:
        who = c.get("block") or c.get("handle", "")
        print(f"{c['kind']:6s} {who:22s} {c['old']:>6s} {c['measured']:>9.2f} {c['new']:>8s}")
    print(f"\n输出: {DST.relative_to(ROOT)}  ({len(changes)} 条)")


if __name__ == "__main__":
    main()

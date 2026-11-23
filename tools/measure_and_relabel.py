"""25QYL-2SL：图形不动，把每条长度标注改成它对应尺寸线的真实几何跨度（1单位=1单位）。
规格类（G1/G3/4/4-Φ11）不动。输出 DXF + 诊断报告 JSON。
"""
from __future__ import annotations
import json, math, sys
from pathlib import Path
import ezdxf

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "output" / "25QYL-2SL_clean.dxf"
DST = ROOT / "output" / "25QYL-2SL_实测1to1.dxf"
REPORT = ROOT / "output" / "25QYL-2SL_实测报告.json"

SPEC_TEXTS = {"G1", "G3/4", "4-%%C11", "4-Φ11"}


def block_span(block):
    """返回 (水平线最长跨度, 垂直线最长跨度)。"""
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
    return f"{v:.1f}"


def main():
    doc = ezdxf.readfile(str(SRC))
    changes = []
    # 1) 块标注
    for b in doc.blocks:
        if not b.name.startswith("GS_BlockDim"):
            continue
        texts = [e for e in b if e.dxftype() == "TEXT"]
        if not texts:
            continue
        old = texts[0].dxf.text
        if old in SPEC_TEXTS:
            continue  # 规格类不动
        hspan, vspan = block_span(b)
        rot = texts[0].dxf.get("rotation", 0) or 0
        # 竖直标注取垂直跨度，水平标注取水平跨度
        measured = vspan if abs(rot - 90) < 1 else hspan
        if measured <= 0:
            measured = max(hspan, vspan)
        new = fmt(measured)
        for t in texts:
            t.dxf.text = new
        changes.append({
            "kind": "BLOCK", "block": b.name, "old": old, "new": new,
            "measured": round(measured, 4), "hspan": round(hspan, 4),
            "vspan": round(vspan, 4), "rot": rot,
        })

    # 2) 原生 DIMENSION：实体 text 属性 + 它引用的 geometry 块里的 MTEXT 都要改
    for d in doc.modelspace().query("DIMENSION"):
        old = d.dxf.text
        if old in SPEC_TEXTS:
            continue
        meas = float(d.get_measurement())
        new = fmt(meas)
        d.dxf.text = new
        # 同步 geometry 块内的 MTEXT（渲染/Oda 显示的是这一份）
        geom = d.dxf.get("geometry", "")
        if geom and geom in doc.blocks:
            for e in doc.blocks[geom]:
                if e.dxftype() == "MTEXT":
                    e.text = new
                elif e.dxftype() == "TEXT":
                    e.dxf.text = new
        changes.append({
            "kind": "DIM", "handle": d.dxf.handle, "old": old, "new": new,
            "measured": round(meas, 4), "geometry_block": geom,
        })

    doc.saveas(str(DST))
    REPORT.write_text(json.dumps({
        "source": str(SRC.relative_to(ROOT)),
        "output": str(DST.relative_to(ROOT)),
        "rule": "图形实体与图框完全不动；长度标注=对应尺寸线真实几何跨度；规格类不动；1单位=1单位",
        "changes": changes,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{'类型':6s} {'块/句柄':22s} {'原值':>8s} {'新值':>8s} {'实测':>9s}")
    for c in changes:
        who = c.get("block") or c.get("handle", "")
        print(f"{c['kind']:6s} {who:22s} {c['old']:>8s} {c['new']:>8s} {c['measured']:>9.2f}")
    print(f"\n输出: {DST.relative_to(ROOT)}  ({len(changes)} 条标注)")


if __name__ == "__main__":
    main()

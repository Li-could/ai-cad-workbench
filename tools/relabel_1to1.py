"""25QYL-2SL：严格 1:1 —— 标注数字 = 图形实测长度（1 图形单位 = 1 mm）。
图形与图框完全不动；规格类(G1/G3/4/4-Φ11)不动。
从 clean.dxf 原始基线出发，避免二次换算。
"""
from __future__ import annotations
import json
from pathlib import Path
import ezdxf

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "output" / "25QYL-2SL_clean.dxf"
DST = ROOT / "output" / "25QYL-2SL_1比1.dxf"
REPORT = ROOT / "output" / "25QYL-2SL_1比1报告.json"

K = 1.0  # 严格 1:1
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
    return f"{v:.1f}"


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
            "measured": round(measured, 4),
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
            "measured": round(meas, 4), "geometry_block": geom,
        })

    doc.saveas(str(DST))
    REPORT.write_text(json.dumps({
        "source": str(SRC.relative_to(ROOT)),
        "output": str(DST.relative_to(ROOT)),
        "unit": "图形单位(=mm)",
        "rule": "严格1:1；图形与图框完全不动；标注=图形实测跨度×1；规格类不动",
        "base": {"ratio": "1:1", "K": K},
        "changes": changes,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"比例 1:1  K={K}\n")
    print(f"{'类型':6s} {'块/句柄':22s} {'原值':>6s} {'实测=新值':>10s}")
    for c in changes:
        who = c.get("block") or c.get("handle", "")
        print(f"{c['kind']:6s} {who:22s} {c['old']:>6s} {c['new']:>10s}")
    print(f"\n输出: {DST.relative_to(ROOT)}  ({len(changes)} 条)")


if __name__ == "__main__":
    main()

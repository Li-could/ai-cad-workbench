"""25QYL-2SL：以标注 610 为真实尺寸基准，所有长度标注按同一比例重算。
基准：原标注 610 对应图形实测跨度 144.2571 -> K = 610 / 144.2571 ≈ 4.228561
规则：图形实体与图框完全不动；每条标注 = 该尺寸线实测跨度 * K；规格类不动。
从 clean.dxf 原始基线出发，避免二次换算。
"""
from __future__ import annotations
import json
from pathlib import Path
import ezdxf

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "output" / "25QYL-2SL_clean.dxf"
DST = ROOT / "output" / "25QYL-2SL_610基准.dxf"
REPORT = ROOT / "output" / "25QYL-2SL_610基准报告.json"

# 基准：原标注 610 的精确实测跨度（来自 25QYL-2SL_实测报告.json）
BASE_OLD = 610.0
BASE_MEASURED = 144.2571
K = BASE_OLD / BASE_MEASURED

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
    # 1) 块标注
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

    # 2) 原生 DIMENSION：实体 text 属性 + geometry 块内 MTEXT 同步
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
        "rule": "以610为真实尺寸基准；K=610/144.2571=4.228561；图形与图框完全不动；每条标注=实测跨度*K；规格类不动",
        "base": {"old": BASE_OLD, "measured": BASE_MEASURED, "K": round(K, 6)},
        "changes": changes,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"基准 K = {K:.6f}  (610 / {BASE_MEASURED})\n")
    print(f"{'类型':6s} {'块/句柄':22s} {'原值':>8s} {'实测':>9s} {'新值':>9s}")
    for c in changes:
        who = c.get("block") or c.get("handle", "")
        print(f"{c['kind']:6s} {who:22s} {c['old']:>8s} {c['measured']:>9.2f} {c['new']:>9s}")
    print(f"\n输出: {DST.relative_to(ROOT)}  ({len(changes)} 条标注)")


if __name__ == "__main__":
    main()

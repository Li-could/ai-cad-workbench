"""Finalize the 1:1 real-millimeter drawing from output/_real_mm.dxf.

The source DXF already contains the non-uniformly scaled millimetre geometry.
This step transforms the two native dimension display blocks, rewrites every
length annotation from its measured geometry, audits the document, and writes a
verification report.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import ezdxf
from ezdxf.math import Matrix44, Vec2

SRC = ROOT / "output" / "_real_mm.dxf"
DST = ROOT / "output" / "_real_mm_final.dxf"
REPORT = ROOT / "output" / "_real_mm_final_report.json"

SX = 610.0 / 144.2571
SY = 284.0 / 70.5571
SPEC_TEXTS = {"G1", "G3/4", "4-%%C11", "4-Φ11"}


def fmt(value: float) -> str:
    return f"{value:.1f}"


def angle_diff(a: float, b: float) -> float:
    diff = abs(a - b) % 180.0
    return min(diff, 180.0 - diff)


def line_angle(line) -> float:
    delta = Vec2(line.dxf.end) - Vec2(line.dxf.start)
    return math.degrees(math.atan2(delta.y, delta.x)) % 180.0


def dimension_line_length(block) -> tuple[float | None, list[dict]]:
    lines = [entity for entity in block if entity.dxftype() == "LINE"]
    details = []
    for line in lines:
        delta = Vec2(line.dxf.end) - Vec2(line.dxf.start)
        details.append(
            {
                "start": [round(value, 4) for value in line.dxf.start],
                "end": [round(value, 4) for value in line.dxf.end],
                "length": round(delta.magnitude, 4),
                "angle": round(line_angle(line), 4),
            }
        )
    if len(lines) != 3:
        return None, details

    angles = [line_angle(line) for line in lines]
    for index, angle in enumerate(angles):
        other = [angles[i] for i in range(3) if i != index]
        if all(angle_diff(angle, value) > 1.0 for value in other):
            delta = Vec2(lines[index].dxf.end) - Vec2(lines[index].dxf.start)
            return delta.magnitude, details
    return None, details


def transform_text(entity) -> None:
    insert = entity.dxf.insert
    entity.dxf.insert = (
        insert.x * SX,
        insert.y * SY,
        insert.z,
    )
    height = entity.dxf.get("height", 2.5)
    entity.dxf.height = height * SY
    align_point = entity.dxf.get("align_point")
    if align_point is not None:
        entity.dxf.align_point = (
            align_point.x * SX,
            align_point.y * SY,
            align_point.z,
        )


def main() -> int:
    doc = ezdxf.readfile(str(SRC))
    msp = doc.modelspace()
    changes: list[dict] = []

    for block in doc.blocks:
        if not block.name.startswith("GS_BlockDim"):
            continue
        texts = [entity for entity in block if entity.dxftype() == "TEXT"]
        if not texts:
            continue
        old = texts[0].dxf.text
        if old in SPEC_TEXTS:
            continue
        measured, details = dimension_line_length(block)
        if measured is None:
            raise RuntimeError(f"无法确定标注线: {block.name}")
        new = fmt(measured)
        for text in texts:
            text.dxf.text = new
        changes.append(
            {
                "kind": "BLOCK",
                "name": block.name,
                "old": old,
                "new": new,
                "measured": round(measured, 6),
                "lines": details,
            }
        )

    transform = Matrix44.scale(SX, SY, 1.0)
    for dimension in msp.query("DIMENSION"):
        geometry_name = dimension.dxf.get("geometry", "")
        if not geometry_name or geometry_name not in doc.blocks:
            raise RuntimeError(f"标注缺少显示块: {dimension.dxf.handle}")
        geometry_block = doc.blocks[geometry_name]
        for entity in geometry_block:
            if entity.dxftype() == "TEXT":
                transform_text(entity)
            else:
                entity.transform(transform)

        measured = float(dimension.get_measurement())
        new = fmt(measured)
        old = dimension.dxf.text
        dimension.dxf.text = new
        block_text = next(
            entity for entity in geometry_block if entity.dxftype() == "TEXT"
        )
        block_text.dxf.text = new
        dimension.dxf.text_midpoint = block_text.dxf.insert
        changes.append(
            {
                "kind": "DIMENSION",
                "handle": dimension.dxf.handle,
                "geometry_block": geometry_name,
                "old": old,
                "new": new,
                "measured": round(measured, 6),
            }
        )

    auditor = doc.audit()
    first_fixes = len(auditor.fixes)
    first_errors = len(auditor.errors)
    doc.saveas(str(DST))

    doc = ezdxf.readfile(str(DST))
    auditor = doc.audit()
    second_fixes = len(auditor.fixes)
    second_errors = len(auditor.errors)
    if second_fixes or second_errors:
        doc.saveas(str(DST))
        doc = ezdxf.readfile(str(DST))
        auditor = doc.audit()
    final_fixes = len(auditor.fixes)
    final_errors = len(auditor.errors)

    REPORT.write_text(
        json.dumps(
            {
                "source": str(SRC.relative_to(ROOT)),
                "output": str(DST.relative_to(ROOT)),
                "scale_x": SX,
                "scale_y": SY,
                "units": "millimeter",
                "rule": "length annotation = measured geometry; 1 drawing unit = 1 mm",
                "audit": {
                    "first_fixes": first_fixes,
                    "first_errors": first_errors,
                    "second_fixes": second_fixes,
                    "second_errors": second_errors,
                    "final_fixes": final_fixes,
                    "final_errors": final_errors,
                },
                "changes": changes,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"scale_x={SX:.9f} scale_y={SY:.9f}")
    for change in changes:
        print(
            f"{change['kind']:9s} {change.get('name') or change.get('handle'):20s} "
            f"{change['old']:>8s} -> {change['new']:>8s} "
            f"(measured {change['measured']:.4f})"
        )
    print(
        f"audit first={first_fixes}/{first_errors} "
        f"second={second_fixes}/{second_errors} "
        f"final={final_fixes}/{final_errors}"
    )
    print(f"output={DST.relative_to(ROOT)}")
    return 0 if final_fixes == 0 and final_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

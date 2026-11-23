"""Read a delivered real-millimeter DWG back through ODA and verify key numbers."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import ezdxf
from ezdxf import bbox

from dwg_export import find_converter
from real_mm_engine import measure_all, part_view_heights

DEFAULT_DWG = ROOT / "output" / "25QYL-2SL_1比1_真实毫米610.dwg"
EXPECTED_VALUES = {
    "147.7", "610.0", "127.7", "307.0", "442.3", "134.9",
    "33.8", "78.4", "284.0", "215.7", "309.6", "265.9", "250.4",
}


def dwg_to_dxf(dwg: Path, target: Path) -> None:
    exe = find_converter()
    if exe is None:
        raise RuntimeError("ODA File Converter 未安装")
    with tempfile.TemporaryDirectory(prefix="real_mm_verify_") as tmp:
        tmp_path = Path(tmp)
        in_dir = tmp_path / "in"
        out_dir = tmp_path / "out"
        in_dir.mkdir()
        out_dir.mkdir()
        shutil.copy2(dwg, in_dir / dwg.name)
        subprocess.run(
            [str(exe), str(in_dir), str(out_dir), "ACAD2018", "DXF", "0", "1", "*.dwg"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        produced = list(out_dir.glob("*.dxf"))
        if not produced:
            raise RuntimeError("ODA 未能把 DWG 转回 DXF")
        shutil.copy2(produced[0], target)


def main(dwg: Path = DEFAULT_DWG) -> dict:
    with tempfile.TemporaryDirectory(prefix="real_mm_readback_") as tmp:
        dxf = Path(tmp) / "readback.dxf"
        dwg_to_dxf(dwg, dxf)
        doc = ezdxf.readfile(str(dxf))
        msp = doc.modelspace()
        extents = bbox.extents(msp)
        rows = measure_all(doc)
        values = {row["old"] for row in rows}
        views = part_view_heights(doc)
        left_view = views.get("left") or {}
        right_view = views.get("right") or {}
        left_height = float(left_view.get("height") or 0.0)
        right_height = float(right_view.get("height") or 0.0)
        return {
            "dwg": str(dwg),
            "insunits": doc.header.get("$INSUNITS"),
            "counts": dict(Counter(entity.dxftype() for entity in msp)),
            "extents": {
                "x": round(extents.extmax.x - extents.extmin.x, 3),
                "y": round(extents.extmax.y - extents.extmin.y, 3),
            },
            "view_geometry": {
                "left_height": round(left_height, 3),
                "right_height": round(right_height, 3),
                "left_bottom": left_view.get("y0"),
                "right_bottom": right_view.get("y0"),
                "equal_height": abs(left_height - right_height) <= 0.05,
                "same_baseline": abs(
                    float(left_view.get("y0") or 0.0)
                    - float(right_view.get("y0") or 0.0)
                ) <= 0.05,
            },
            "annotations": [
                {"where": row["where"], "text": row["old"], "measured": row["measured"]}
                for row in rows
            ],
            "missing_expected": sorted(EXPECTED_VALUES - values),
            "unexpected": sorted(values - EXPECTED_VALUES),
        }


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DWG
    print(json.dumps(main(path), ensure_ascii=False, indent=2))

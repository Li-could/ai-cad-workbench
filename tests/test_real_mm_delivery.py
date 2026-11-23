"""Regression test for the 25QYL real-millimeter delivery.

Run with: python tests/test_real_mm_delivery.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import ezdxf

import real_mm_engine as rm

SOURCE = ROOT / "output" / "25QYL-2SL_clean.dxf"
SOURCE_DWG = ROOT / "input" / "25QYL-2SL.dwg"
TARGET_HEIGHT = 284.0


def _prepare_source(work: Path) -> Path:
    """Use the cached clean DXF, or rebuild it from the original DWG."""
    if SOURCE.exists():
        return SOURCE
    if not SOURCE_DWG.exists():
        raise SystemExit(f"缺少测试源文件：{SOURCE} / {SOURCE_DWG}")

    from dwg_bridge import dwg_to_dxf
    from dxf_cleaner import clean_dxf

    raw = work / "source.dxf"
    conversion = dwg_to_dxf(SOURCE_DWG, raw)
    if not conversion.get("ok"):
        raise SystemExit(f"测试基线转换失败：{conversion}")
    clean = work / "clean.dxf"
    if conversion.get("needs_cleaning"):
        clean_dxf(raw, clean)
    else:
        shutil.copy2(raw, clean)
    return clean


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="real_mm_test_") as tmp:
        work = Path(tmp)
        source = _prepare_source(work)
        output = work / "result.dxf"
        report = rm.set_real_mm(source, output)

        watermarks = report.get("watermarks") or {}
        assert "WatermarkLayer" not in (watermarks.get("layers") or []), watermarks

        views = report["calibration"]["view_normalization"]["after"]
        left, right = views["left"], views["right"]
        assert abs(left["height"] - TARGET_HEIGHT) <= 0.01, left
        assert abs(right["height"] - TARGET_HEIGHT) <= 0.01, right
        assert abs(left["y0"] - right["y0"]) <= 0.01, (left, right)
        assert abs(left["y1"] - right["y1"]) <= 0.01, (left, right)
        assert abs(left["y1"] - left["y0"] - right["y1"] + right["y0"]) <= 0.01

        for change in report["changes"]:
            assert abs(float(change["new"]) - float(change["measured"])) <= 0.051, change
        labels = {change["new"] for change in report["changes"]}
        assert {"610.0", "284.0", "147.7", "250.4"} <= labels, labels

        assert report["audit"]["final"]["errors"] == 0
        assert report["audit"]["final"]["fixes"] == 0

        doc = ezdxf.readfile(str(output))
        assert doc.header.get("$INSUNITS") == 4
        assert doc.header.get("$MEASUREMENT") == 1
        texts = []
        for entity in doc.modelspace():
            if entity.dxftype() == "TEXT":
                texts.append(entity.dxf.text)
            elif entity.dxftype() == "MTEXT":
                texts.append(entity.text)
        for block in doc.blocks:
            for entity in block:
                if entity.dxftype() == "TEXT":
                    texts.append(entity.dxf.text)
                elif entity.dxftype() == "MTEXT":
                    texts.append(entity.text)
        joined = "|".join(texts)
        assert "G1" in joined
        assert "G3/4" in joined
        assert "4-%%C11" in joined
        assert "evaluation only" not in joined.lower()

    print("PASS: watermarks removed, both view outlines 284.0, annotations = measured")


if __name__ == "__main__":
    main()

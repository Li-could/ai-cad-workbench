from __future__ import annotations

from pathlib import Path

import ezdxf

import calibration


def test_scan_and_skeleton_are_draft_until_reviewed(golden_task: Path):
    doc = ezdxf.readfile(str(golden_task / "clean.dxf"))
    scan = calibration.scan_drawing(doc)
    assert scan["entity_count"] == 2873
    assert scan["text_candidates"]
    assert scan["dimensions"] or scan["longest_lines"]

    skeleton = calibration.build_skeleton(
        doc,
        profile_id="td50-40g-copy",
        model="TD50-40G",
        title="TD50 副本",
        source_file="TD50-40G.dwg",
        source_sha256="0" * 64,
        scan=scan,
    )
    assert skeleton["status"] == "draft"
    assert skeleton["review"]["status"] == "draft"
    assert skeleton["fingerprint"]["required_handles"] == []
    assert skeleton["evidence"]["text_candidates"]

    problems = calibration.readiness_problems(skeleton)
    assert any("review.status" in problem for problem in problems)
    assert any("required_handles" in problem for problem in problems)

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
for path in (str(ROOT), str(SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)

import runtime  # noqa: E402

runtime.bootstrap()


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def td50_dwg(project_root: Path) -> Path:
    path = project_root / "input" / "TD50-40G.dwg"
    if not path.is_file():
        pytest.skip("需要本地图纸 input/TD50-40G.dwg")
    return path


@pytest.fixture(scope="session")
def td50_profile(project_root: Path):
    import drawing_profile

    path = project_root / "knowledge" / "drawings" / "td50-40g.profile.json"
    if not path.is_file():
        pytest.skip("需要本地 TD50 定标档案")
    return drawing_profile.load_profile(path)


@pytest.fixture
def installed_test_profile(monkeypatch, tmp_path: Path):
    import drawing_profile

    monkeypatch.setattr(drawing_profile, "PROFILE_DIR", tmp_path / "profiles")
    path = drawing_profile.save_profile({
        "profile_id": "test-profile",
        "model": "TEST",
        "fingerprint": {"markers": ["TEST-CALIBRATED"], "entity_count": [2, 2]},
        "calibration": {
            "vertical": {
                "units_per_mm": 1.0,
                "datum_from": "hood_top_y",
                "datum_to": "flange_y",
                "reference_mm": 100.0,
            },
            "horizontal": {"units_per_mm": 1.0},
        },
        "datums": {"hood_top_y": 100.0, "flange_y": 0.0},
        "original": {"H2": 100.0},
        "rules": {"H2": {}, "D2": {}, "shaft": {}},
    })
    return drawing_profile.load_profile(path)


@pytest.fixture(scope="session")
def golden_task(project_root: Path) -> Path:
    path = project_root / "tests" / "fixtures" / "td50-golden"
    if not (path / "clean.dxf").exists() or not (path / "result.dxf").exists():
        pytest.skip("TD50 golden fixture 不存在")
    return path

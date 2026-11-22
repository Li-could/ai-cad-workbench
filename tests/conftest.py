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
def golden_task(project_root: Path) -> Path:
    path = project_root / "tests" / "fixtures" / "td50-golden"
    if not (path / "clean.dxf").exists() or not (path / "result.dxf").exists():
        pytest.skip("TD50 golden fixture 不存在")
    return path

"""Render a clean PNG preview of the finished real-millimeter drawing."""
from __future__ import annotations

import html
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import ezdxf
from ezdxf import bbox
from ezdxf.path import from_hatch

SRC = ROOT / "output" / "_real_mm_final.dxf"
PNG = ROOT / "output" / "25QYL-2SL_1比1_真实毫米610_预览.png"

WIDTH = 2400
MARGIN = 50


def clean_text(text: str) -> str:
    return (
        text.replace("%%C", "Φ")
        .replace("%%c", "Φ")
        .replace("%%D", "°")
        .replace("%%P", "±")
    )


def iter_entities(entity):
    if entity.dxftype() in {"INSERT", "DIMENSION"}:
        try:
            yield from entity.virtual_entities()
            return
        except Exception:
            pass
    yield entity


def main(src: Path = SRC, png: Path = PNG) -> None:
    svg_path = png.with_suffix(".svg")
    doc = ezdxf.readfile(str(src))
    msp = doc.modelspace()
    extents = bbox.extents(msp)
    x0, y0 = extents.extmin.x, extents.extmin.y
    x1, y1 = extents.extmax.x, extents.extmax.y
    scale = (WIDTH - 2 * MARGIN) / (x1 - x0)
    height = round((y1 - y0) * scale + 2 * MARGIN)

    def sx(value: float) -> float:
        return MARGIN + (value - x0) * scale

    def sy(value: float) -> float:
        return height - MARGIN - (value - y0) * scale

    fills: list[str] = []
    lines: list[str] = []
    texts: list[str] = []

    for top_level in msp:
        for entity in iter_entities(top_level):
            kind = entity.dxftype()
            if kind == "LINE":
                start, end = entity.dxf.start, entity.dxf.end
                lines.append(
                    f'<line x1="{sx(start.x):.2f}" y1="{sy(start.y):.2f}" '
                    f'x2="{sx(end.x):.2f}" y2="{sy(end.y):.2f}"/>'
                )
            elif kind == "ELLIPSE":
                points = [
                    f"{sx(point.x):.2f},{sy(point.y):.2f}"
                    for point in entity.flattening(0.05)
                ]
                lines.append(
                    f'<polyline points="{" ".join(points)}" fill="none"/>'
                )
            elif kind == "HATCH":
                try:
                    path = from_hatch(entity)
                    points = [
                        f"{sx(point.x):.2f},{sy(point.y):.2f}"
                        for point in path.vertices()
                    ]
                    if points:
                        fills.append(f'<polygon points="{" ".join(points)}"/>')
                except Exception:
                    pass
            elif kind == "TEXT":
                insert = entity.dxf.insert
                text = html.escape(clean_text(entity.dxf.text))
                font_size = max(7.0, entity.dxf.height * scale * 1.15)
                rotation = -(entity.dxf.get("rotation", 0.0) or 0.0)
                texts.append(
                    f'<text x="{sx(insert.x):.2f}" y="{sy(insert.y):.2f}" '
                    f'font-size="{font_size:.2f}" '
                    f'transform="rotate({rotation:.2f} {sx(insert.x):.2f} {sy(insert.y):.2f})">'
                    f"{text}</text>"
                )

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<g fill="#111111" stroke="none">',
        *fills,
        "</g>",
        '<g stroke="#151515" stroke-width="1.4" fill="none" '
        'stroke-linecap="round" stroke-linejoin="round">',
        *lines,
        "</g>",
        '<g fill="#151515" font-family="Arial, Microsoft YaHei, sans-serif" '
        'font-weight="500">',
        *texts,
        "</g>",
        "</svg>",
    ]
    svg_path.write_text("\n".join(svg), encoding="utf-8")

    chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    if not chrome.exists():
        raise FileNotFoundError(f"找不到 Chrome，无法渲染 PNG：{chrome}")
    with tempfile.TemporaryDirectory(prefix="real_mm_chrome_") as profile_dir:
        subprocess.run(
            [
                str(chrome),
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--hide-scrollbars",
                f"--user-data-dir={Path(profile_dir).resolve()}",
                f"--screenshot={png.resolve()}",
                f"--window-size={WIDTH},{height}",
                svg_path.resolve().as_uri(),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
    if not png.exists() or png.stat().st_size == 0:
        raise RuntimeError(f"PNG 渲染失败，未生成有效文件：{png}")
    print(png)


if __name__ == "__main__":
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else SRC
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else PNG
    main(source, target)

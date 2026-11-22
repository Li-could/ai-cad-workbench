"""DWG 桥接层。

优先用 ODA File Converter 读 DWG（无损、无水印、保留 handle），
没有 ODA 时回退 aspose-cad（只读，导出 DXF 后需清洗评估水印）。

原始 DWG 永远只读；所有转换在工作副本/临时目录里完成。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import runtime

runtime.bootstrap()


class DWG_UNSUPPORTED(RuntimeError):
    pass


def sdk_info() -> dict[str, Any]:
    from dwg_export import find_converter

    converter = find_converter()
    info: dict[str, Any] = {
        "oda": str(converter) if converter else None,
        "aspose": None,
    }
    try:
        import aspose.cad as cad

        info["aspose"] = getattr(cad, "__version__", "installed")
    except Exception as exc:  # noqa: BLE001
        info["aspose_error"] = f"{type(exc).__name__}: {exc}"
    info["available"] = bool(info["oda"] or info["aspose"])
    return info


def _load_image(path: str | Path):
    try:
        import aspose.cad as cad
    except ImportError as exc:
        raise DWG_UNSUPPORTED(f"aspose-cad 未安装：{exc}") from exc
    return cad.Image.load(str(path))


def dwg_to_dxf(src: str | Path, dst: str | Path) -> dict[str, Any]:
    """DWG -> DXF。ODA 优先，aspose 兜底。"""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    from dwg_export import dwg_to_dxf as oda_dwg_to_dxf, oda_available

    if oda_available():
        result = oda_dwg_to_dxf(src, dst)
        result["converter"] = "oda"
        result["needs_cleaning"] = False
        return result

    try:
        image = _load_image(src)
        image.save(str(dst))
        return {
            "ok": True,
            "converter": "aspose",
            "needs_cleaning": True,
            "src": str(src),
            "dst": str(dst),
            "size": dst.stat().st_size if dst.exists() else 0,
            "warning": "aspose 免费版会写评估水印，后续必须清洗",
        }
    except DWG_UNSUPPORTED:
        raise
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "converter": "aspose",
                "error": f"{type(exc).__name__}: {exc}"}


def dwg_to_png(src: str | Path, dst: str | Path, width: int = 1400) -> dict[str, Any]:
    """DWG -> PNG 预览（可选能力，缺 aspose 时返回不可用）。"""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        from aspose.cad.imageoptions import CadRasterizationOptions, PngOptions

        image = _load_image(src)
        raster = CadRasterizationOptions()
        raster.page_width = width
        raster.page_height = int(width * 0.75)
        raster.draw_type = 0
        options = PngOptions()
        options.vector_rasterization_options = raster
        image.save(str(dst), options)
        return {"ok": True, "dst": str(dst), "size": dst.stat().st_size}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def probe_dwg(path: str | Path) -> dict[str, Any]:
    """轻量探测：文件头、大小、可用转换器。不做完整转换。"""
    path = Path(path)
    info: dict[str, Any] = {
        "path": str(path),
        "size": path.stat().st_size if path.exists() else 0,
        "sdk": sdk_info(),
    }
    head = path.read_bytes()[:6] if path.exists() else b""
    info["version_code"] = head.decode("ascii", errors="ignore")
    info["is_dwg"] = head.startswith(b"AC")
    if not info["is_dwg"]:
        info["openable"] = False
        info["reason"] = "非 DWG 文件头"
    else:
        info["openable"] = bool(info["sdk"]["available"])
        info["converter"] = "oda" if info["sdk"]["oda"] else (
            "aspose" if info["sdk"]["aspose"] else None)
    return info


if __name__ == "__main__":
    import json
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "input/TD50-40G.dwg"
    print(json.dumps(probe_dwg(target), ensure_ascii=False, indent=2))

"""DWG → DXF 转换封装。

DWG 是二进制格式（本样本为 AC1032 / AutoCAD 2018），解析前需转 DXF。
本模块按优先级查找本机可用转换器：
  1. ODA File Converter
  2. LibreCAD (librecad / dwg2dxf)
  3. AutoCAD accoreconsole.exe
  4. FreeCAD / QCAD
若都不可用，给出清晰的人工转换指引。

纯标准库实现，无第三方依赖。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def find_converters() -> list[tuple[str, str]]:
    """返回本机可用转换器 [(名称, 可执行文件/路径)]。"""
    found: list[tuple[str, str]] = []
    candidates = [
        ("ODA File Converter", "ODAFileConverter"),
        ("LibreCAD", "librecad"),
        ("LibreCAD dwg2dxf", "dwg2dxf"),
        ("AutoCAD accoreconsole", "accoreconsole"),
        ("FreeCAD", "FreeCADCmd"),
        ("QCAD", "qcad"),
    ]
    for name, exe in candidates:
        p = shutil.which(exe)
        if p:
            found.append((name, p))
    return found


def convert_dwg_to_dxf(dwg_path: str | Path, dxf_path: str | Path | None = None,
                       version: str = "AC1027") -> Path:
    """把 DWG 转为 DXF，返回 DXF 路径。"""
    dwg = Path(dwg_path).resolve()
    if not dwg.exists():
        raise FileNotFoundError(f"DWG 不存在: {dwg}")
    if dxf_path is None:
        dxf = dwg.with_suffix(".dxf")
    else:
        dxf = Path(dxf_path).resolve()

    converters = find_converters()
    if not converters:
        raise RuntimeError(
            "未找到 DWG→DXF 转换器。请选择其一：\n"
            "  A) 安装 ODA File Converter（免费）：https://www.opendesign.com/guestfiles/oda_file_converter\n"
            "  B) 安装 LibreCAD：https://librecad.org/\n"
            "  C) 用 AutoCAD 打开 DWG 后另存为 DXF（R2013/R2018）\n"
            "  D) 把转换脚本路径加入 PATH 后重试\n"
            f"源文件: {dwg}"
        )

    name, exe = converters[0]
    # ODA 有独立的命令行参数格式，此处给通用参数示例；实际按所选工具调整。
    if "ODA" in name:
        cmd = [exe, str(dwg.parent), str(dxf.parent), version, "0", "0", dwg.name]
    elif "accoreconsole" in name:
        raise RuntimeError("AutoCAD accoreconsole 需 .scr 脚本驱动，请手动另存为 DXF 或改用 ODA/LibreCAD。")
    else:
        cmd = [exe, str(dwg), str(dxf)]

    subprocess.run(cmd, check=True, capture_output=True, text=True)
    if not dxf.exists():
        raise RuntimeError(f"转换命令执行了但未生成 DXF: {dxf}")
    return dxf


def guide() -> str:
    converters = find_converters()
    if converters:
        return "可用转换器:\n" + "\n".join(f"  - {n}: {p}" for n, p in converters)
    return ("本机未检测到 DWG→DXF 转换器。\n"
            "临时方案：用 CAD 软件打开 TD50-40G.dwg，另存为 DXF（推荐 R2013 格式），\n"
            "然后把 DXF 路径传给 drawing_pipeline.py 即可继续自动解析。")

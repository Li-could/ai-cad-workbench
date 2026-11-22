"""ODA File Converter 封装：DXF/DWG 双向转换 + 往返重开校验。

历史问题：
- 旧实现把输入固定复制成 ``output/_oda_in/TD50-40G_几何修改_最终.dxf``，
  并清空共享输出目录后取 ``produced[0]``。连续/并发执行会抓到上一轮残留文件。
- 没有验证"转换出来的 DWG 还是不是这张图"。

现在每次调用使用独立临时目录，输入输出文件名保持不变，转换后校验文件头、
实体数与 handle 集合；需要时再做 DWG→DXF 往返比对。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

VERSION_MAP = {
    "AC1032": "ACAD2018",
    "AC1027": "ACAD2013",
    "AC1024": "ACAD2010",
    "AC1021": "ACAD2007",
    "AC1018": "ACAD2004",
}


def find_converter() -> Path | None:
    exe = shutil.which("ODAFileConverter.exe") or shutil.which("ODAFileConverter")
    if exe:
        return Path(exe)
    bases = [Path(r"C:\Program Files\ODA"),
             Path(r"C:\Program Files (x86)\ODA"),
             Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ODA"]
    for base in bases:
        if not base.exists():
            continue
        for directory in sorted(base.glob("ODAFileConverter*"), reverse=True):
            candidate = directory / "ODAFileConverter.exe"
            if candidate.exists():
                return candidate
    return None


def oda_available() -> bool:
    return find_converter() is not None


def _run_convert(converter: Path, in_dir: Path, out_dir: Path,
                 version: str, out_type: str, pattern: str) -> dict:
    acad_version = VERSION_MAP.get(version, version if version.startswith("ACAD") else "ACAD2018")
    command = [str(converter), str(in_dir), str(out_dir), acad_version, out_type,
               "0", "1", pattern]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=240)
    return {
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-400:],
        "stderr": (proc.stderr or "")[-400:],
    }


def convert(src: str | Path, dst: str | Path, *, version: str = "AC1032",
            out_type: str = "DWG") -> dict:
    """把单个文件转成 DWG 或 DXF；使用独立临时目录，文件名保持一致。"""
    src, dst = Path(src).resolve(), Path(dst).resolve()
    converter = find_converter()
    if converter is None:
        return {"ok": False, "error": "ODA File Converter 未安装"}
    if not src.exists():
        return {"ok": False, "error": f"输入文件不存在：{src}"}

    expected_suffix = ".dwg" if out_type.upper() == "DWG" else ".dxf"
    with tempfile.TemporaryDirectory(prefix="oda_") as tmp:
        base = Path(tmp)
        in_dir = base / "in"
        out_dir = base / "out"
        in_dir.mkdir()
        out_dir.mkdir()
        staged = in_dir / src.name
        shutil.copy2(src, staged)
        run = _run_convert(converter, in_dir, out_dir, version, out_type.upper(),
                           f"*{src.suffix.lower()}")
        produced = out_dir / (src.stem + expected_suffix)
        if not produced.exists():
            matches = sorted(out_dir.glob(f"*{expected_suffix}"))
            produced = matches[0] if matches else None
        if produced is None or not produced.exists():
            return {"ok": False, "error": "ODA 未生成目标文件",
                    "converter": str(converter), **run}
        if produced.stat().st_size <= 0:
            return {"ok": False, "error": "ODA 产出为空文件", **run}
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(produced, dst)

    head = dst.read_bytes()[:6]
    info = {
        "ok": True,
        "converter": str(converter),
        "output": str(dst),
        "size": dst.stat().st_size,
        "header": head.decode("ascii", errors="ignore"),
        "version": version,
        "out_type": out_type.upper(),
        "run": run,
    }
    if out_type.upper() == "DWG" and not head.startswith(b"AC"):
        info["ok"] = False
        info["error"] = f"DWG 文件头异常：{head!r}"
    return info


def export(dxf: str | Path, dwg: str | Path, version: str = "AC1032") -> dict:
    """DXF -> DWG（保留旧调用名）。"""
    return convert(dxf, dwg, version=version, out_type="DWG")


def dwg_to_dxf(src: str | Path, dst: str | Path, version: str = "AC1032") -> dict:
    return convert(src, dst, version=version, out_type="DXF")


def roundtrip_check(dwg: str | Path, reference_dxf: str | Path) -> dict:
    """把成果 DWG 再转回 DXF，验证它能被重新打开且图元没有丢失。"""
    dwg, reference_dxf = Path(dwg), Path(reference_dxf)
    if not dwg.exists() or not reference_dxf.exists():
        return {"ok": False, "error": "缺少待校验文件"}
    with tempfile.TemporaryDirectory(prefix="oda_rt_") as tmp:
        back = Path(tmp) / (dwg.stem + "_往返.dxf")
        info = dwg_to_dxf(dwg, back)
        if not info.get("ok"):
            return {"ok": False, "step": "dwg->dxf", "detail": info}
        try:
            import ezdxf

            before = ezdxf.readfile(str(reference_dxf))
            after = ezdxf.readfile(str(back))
            before_handles = {e.dxf.handle for e in before.modelspace()}
            after_handles = {e.dxf.handle for e in after.modelspace()}
            audit = after.audit()
            missing = sorted(before_handles - after_handles)
            extra = sorted(after_handles - before_handles)
            ok = (not missing and not extra and len(audit.errors) == 0
                  and after.dxfversion == before.dxfversion)
            return {
                "ok": ok,
                "dxfversion": after.dxfversion,
                "entities": {"before": len(before_handles), "after": len(after_handles)},
                "missing_handles": missing[:20],
                "extra_handles": extra[:20],
                "audit_errors": len(audit.errors),
                "audit_fixes": len(audit.fixes),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "step": "reopen", "error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) >= 3:
        print(json.dumps(export(sys.argv[1], sys.argv[2]), ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"converter": str(find_converter())}, ensure_ascii=False, indent=2))

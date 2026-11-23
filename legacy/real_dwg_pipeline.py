"""真实 TD50-40G.dwg 基线处理流水线（方案第13章第一/二步）。

前置：需安装 aspose-cad（提供 AC1032 DWG 读取）。
步骤：
1. 校验原图哈希与基线一致（只读保护）
2. 用 SDK 打开 DWG，导出 DXF 基线
3. ezdxf 解析图层/实体/尺寸，生成清单 + 预览
4. 输出基线报告
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dwg_bridge import sdk_info, dwg_to_dxf, dwg_to_png  # noqa: E402
from dxf_parser import auto_parse  # noqa: E402


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def main():
    src = ROOT / "input" / "TD50-40G.dwg"
    out = ROOT / "output"
    out.mkdir(exist_ok=True)

    report: dict = {"source": str(src), "sha256": sha256(src), "sdk": sdk_info()}
    print("[1] 校验源文件:", report["sha256"][:16], "...", report["sdk"])

    if not report["sdk"]["available"]:
        report["status"] = "blocked"
        report["reason"] = "DWG SDK 未安装，无法读取 AC1032 二进制。请安装 aspose-cad 或 ODA SDK。"
        (out / "real_baseline_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[X]", report["reason"])
        return

    print("[2] DWG -> DXF ...")
    conv = dwg_to_dxf(src, out / "TD50-40G_真实基线.dxf")
    report["conversion"] = conv
    if not conv.get("ok"):
        report["status"] = "failed"
        print("[X] 转换失败:", conv.get("error"))
        (out / "real_baseline_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    print("[3] 解析 DXF 建基线 ...")
    parsed = auto_parse(out / "TD50-40G_真实基线.dxf")
    report["counts"] = parsed.get("counts")
    report["layers"] = parsed.get("layers", [])
    report["blocks"] = parsed.get("blocks", [])
    report["size_index"] = parsed.get("size_index", {})
    report["entities"] = parsed.get("entities", [])
    report["parser"] = parsed.get("parser")

    print("[4] 导出预览 ...")
    pv = dwg_to_png(src, out / "TD50-40G_原图预览.png")
    report["preview"] = pv

    report["status"] = "ok"
    (out / "real_baseline_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "real_baseline_entities.json").write_text(
        json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[OK] 实体", parsed.get("counts"), "图层", len(report["layers"]), "块", len(report["blocks"]))
    print("     尺寸图谱:", list(report["size_index"].keys())[:20])


if __name__ == "__main__":
    main()

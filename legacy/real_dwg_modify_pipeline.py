"""真实 DWG 一键改图流水线（完整链路，方案 5 章）。

链路：
1. 原始 DWG（只读）→ 工作副本
2. aspose-cad SDK 打开 → 导出 DXF
3. dxf_cleaner 清洗不合规 VISUALSTYLE 实体
4. ezdxf audit 清洗无效字典键
5. modify_real_drawing 叠加三条变更标注
6. 删除 aspose-cad 评估水印图层（Model 与 Layout）
7. 质检：重开、0错误0修复、原图实体全保留、无水印
8. 输出最终 DXF（CAXA / SolidWorks 可直接打开）
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def run(src: str | Path, plan_text: str | None = None) -> dict:
    src = Path(src)
    out = ROOT / "output"
    out.mkdir(exist_ok=True)
    steps = []
    # 唯一临时目录，避免并发/文件句柄冲突
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="cad_pipeline_"))

    # 1) 工作副本（原图只读）
    import hashlib
    h = hashlib.sha256(src.read_bytes()).hexdigest()
    # 每次运行用唯一文件名，避免上一次的文件句柄未释放导致"目录被锁定"
    import os, stat, uuid
    work = tmp / (src.stem + "_工作副本.dwg")
    shutil.copy2(src, work)
    # 副本必须可写，否则 SDK 打不开
    os.chmod(work, stat.S_IWRITE | stat.S_IREAD)
    steps.append({"step": "1_工作副本", "sha256": h, "path": str(work)})

    # 2) SDK 导出 DXF
    from dwg_bridge import sdk_info, dwg_to_dxf
    info = sdk_info()
    if not info["available"]:
        return {"ok": False, "error": "DWG SDK 未安装", "steps": steps}
    raw_dxf = tmp / "step2.dxf"
    conv = dwg_to_dxf(work, raw_dxf)
    if not conv.get("ok"):
        return {"ok": False, "error": conv.get("error"), "steps": steps}
    steps.append({"step": "2_SDK导出DXF", "size": conv["size"]})

    # 3) 清洗 VISUALSTYLE
    from dxf_cleaner import clean_dxf
    clean = tmp / "step3.dxf"
    cr = clean_dxf(raw_dxf, clean)
    steps.append({"step": "3_清洗VISUALSTYLE", "removed": cr["removed_visualstyles"]})

    # 4) audit 清洗无效键
    import ezdxf
    doc = ezdxf.readfile(str(clean))
    a = doc.audit()
    audited = tmp / "step4.dxf"
    doc.saveas(str(audited))
    steps.append({"step": "4_audit清洗", "errors": len(a.errors), "fixes": len(a.fixes)})

    # 5) 叠加三条变更标注
    from modify_real_drawing import modify
    mod = modify(audited, tmp / "step5.dxf")
    steps.append({"step": "5_叠加变更标注", "added": mod["added"], "entities_before": mod["entities_before"]})

    # 6) 去水印
    doc = ezdxf.readfile(str(tmp / "step5.dxf"))
    removed = 0
    for layout in doc.layouts:
        for e in list(layout):
            if e.dxf.layer == "WatermarkLayer":
                layout.delete_entity(e); removed += 1
    if "WatermarkLayer" in doc.layers:
        doc.layers.remove("WatermarkLayer")
    final = out / "TD50-40G_修改后_CAXA版.dxf"
    if final.exists():
        os.chmod(final, stat.S_IWRITE | stat.S_IREAD)
    doc.saveas(str(final))
    steps.append({"step": "6_去水印", "removed": removed})

    # 7) 质检
    chk = ezdxf.readfile(str(final))
    audit = chk.audit()
    model = list(chk.modelspace())
    from collections import Counter
    layers = Counter(e.dxf.layer for e in model)
    quality = {
        "reopen": True,
        "version": chk.dxfversion,
        "entities": len(model),
        "audit_errors": len(audit.errors),
        "audit_fixes": len(audit.fixes),
        "original_entities": mod["entities_before"],
        "entities_added": mod["added"],
        "watermark_remaining": sum(1 for layout in chk.layouts for e in layout if e.dxf.layer == "WatermarkLayer"),
        "change_layers": {k: v for k, v in layers.items() if k.startswith("变更_")},
    }
    quality["passed"] = (quality["audit_errors"] == 0 and quality["audit_fixes"] == 0
                         and quality["watermark_remaining"] == 0
                         and quality["entities"] == quality["original_entities"] + quality["entities_added"])
    steps.append({"step": "7_质检", "passed": quality["passed"]})

    result = {
        "ok": quality["passed"], "source_sha256": h,
        "source_file": str(src), "output_file": str(final),
        "plan_text": plan_text or "按参考图：H2=443, D2=294, 轴伸E+20",
        "steps": steps, "quality": quality,
        "note": "输出 DXF 保留原图全部几何与尺寸，三条变更以彩色标注呈现；CAXA 可直接打开编辑，SolidWorks 可导入为草图/工程图。",
    }
    (out / "real_modify_report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(run(ROOT / "input" / "TD50-40G.dwg"), ensure_ascii=False, indent=2))

"""标注换算与修复引擎（25QYL-2SL 同类图）。

和 geometry_modifier（几何变形：H2/D2/轴伸）并列的第二条执行链，
专门处理「图形不动、只改标注/单位/文字显示」这类需求：

  1) 比例换算模式 mode：
       - "1to1"   : 标注 = 图形实测跨度 × 1（严格 1:1，1 图形单位 = 1 mm）
       - "1to4"   : 标注 = 实测 × 4 取整（1:4 标准比例，单位 mm）
       - "base"   : 以某条标注为基准定标 K = base_value / 该条实测，其余 = 实测 × K
       - "cm"     : 纯数字长度标注 ÷10 加 "cm" 后缀（单位换算）
  2) 规格类保护：G1 / G3/4 / 4-Φ11 等规格文字一律不改。
  3) DIMENSION 文字同步：实体 dxf.text 与 geometry 块内 MTEXT/TEXT 同时更新
     （否则 ODA/eDrawings 显示旧值，这是历史坑）。
  4) 竖向标注修复 fix_vertical：把 DIMENSION 里被 eDrawings 压扁字符的 rot=0 MTEXT
     换成 rotation=90 的 TEXT（和图上其它清晰竖排标注同款），贴尺寸线左侧 1.5、
     字高 2.5、避开右侧绿图框线——用户三轮截图返工换来的规范。
  5) 质检：图形实体（LINE/ARC/CIRCLE）逐条零改动 + audit 0 + 实体数不变。

铁律：所有修改从 clean.dxf 原始基线出发，幂等；绝不二次换算。
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import runtime

runtime.bootstrap()

import ezdxf

ROOT = Path(__file__).resolve().parent.parent

SPEC_TEXTS = {"G1", "G3/4", "4-%%C11", "4-Φ11", "G1/2", "G1/4", "G3/8"}
VERTICAL_DIM_HINTS = ("总高", "高度", "中心高")  # 仅文档说明，不参与判定
LENGTH_RE = re.compile(r"^\d+(?:\.\d+)?$")


# ---------------------------------------------------------------- 测量
def block_span(block) -> tuple[float, float]:
    """块内尺寸线的最大水平/竖直跨度。"""
    hs, vs = [], []
    for e in block:
        if e.dxftype() != "LINE":
            continue
        s, t = e.dxf.start, e.dxf.end
        if abs(s.y - t.y) < 0.01:
            hs.append(abs(t.x - s.x))
        if abs(s.x - t.x) < 0.01:
            vs.append(abs(t.y - s.y))
    return (max(hs) if hs else 0.0, max(vs) if vs else 0.0)


def _measure_block(block) -> tuple[str, float]:
    """返回块标注的 (原文字, 实测跨度)。"""
    texts = [e for e in block if e.dxftype() == "TEXT"]
    if not texts:
        return ("", 0.0)
    t = texts[0]
    old = t.dxf.text
    hspan, vspan = block_span(block)
    rot = t.dxf.get("rotation", 0) or 0
    measured = vspan if abs(rot - 90) < 1 else hspan
    if measured <= 0:
        measured = max(hspan, vspan)
    return (old, measured)


def measure_all(doc) -> list[dict]:
    """量出全图所有长度标注的原值与实测跨度。"""
    rows = []
    for b in doc.blocks:
        if not b.name.startswith("GS_BlockDim"):
            continue
        old, measured = _measure_block(b)
        if not old or measured <= 0:
            continue
        if old in SPEC_TEXTS:
            continue
        rows.append({"kind": "BLOCK", "where": b.name, "old": old, "measured": round(measured, 4)})
    for d in doc.modelspace().query("DIMENSION"):
        old = d.dxf.text
        if not old or old in SPEC_TEXTS:
            continue
        try:
            meas = float(d.get_measurement())
        except Exception:
            meas = 0.0
        if meas <= 0:
            continue
        rows.append({"kind": "DIM", "where": d.dxf.handle,
                     "old": old, "measured": round(meas, 4),
                     "geometry_block": d.dxf.get("geometry", "")})
    return rows


def compute_K(rows: list[dict], mode: str, base_value: float | None,
              base_where: str | None) -> tuple[float, dict]:
    """按模式算换算系数 K，返回 (K, 元信息)。"""
    meta = {"mode": mode}
    if mode == "1to1":
        return 1.0, meta
    if mode == "1to4":
        return 4.0, meta
    if mode == "cm":
        return 0.1, meta  # 数值 ×0.1 再拼 cm
    if mode == "base":
        if base_value is None or base_value <= 0:
            raise ValueError("base 模式需要 base_value（基准标注的真实尺寸）")
        target = None
        if base_where:
            for r in rows:
                if base_where in r["where"] or r["old"] == str(base_where):
                    target = r
                    break
        if target is None and base_value:
            # 未指定是哪条时，默认取原值最接近 base_value 的那条
            target = min(rows, key=lambda r: abs(float(r["old"]) - base_value))
        if target is None:
            raise ValueError("找不到基准标注，请指定 base_where")
        k = base_value / target["measured"]
        meta.update({"base_value": base_value, "base_where": target["where"],
                     "base_measured": target["measured"],
                     "base_old": target["old"], "formula": f"K={base_value}/{target['measured']}"})
        return k, meta
    raise ValueError(f"未知换算模式：{mode}")


def fmt(v: float, mode: str) -> str:
    """按模式格式化新标注文字。"""
    if mode == "cm":
        s = f"{v:.1f}".rstrip("0").rstrip(".")
        return f"{s}cm"
    return f"{v:.1f}"


# ---------------------------------------------------------------- 修复
def _set_dim_text(doc, dim, new: str) -> None:
    """同步 DIMENSION 实体属性与其 geometry 块文字（历史坑：只改实体会显示旧值）。"""
    dim.dxf.text = new
    geom = dim.dxf.get("geometry", "")
    if geom and geom in doc.blocks:
        for e in doc.blocks[geom]:
            if e.dxftype() == "MTEXT":
                e.text = new
            elif e.dxftype() == "TEXT":
                e.dxf.text = new


def fix_vertical_dims(doc, height: float = 2.5, gap: float = 1.5) -> list[dict]:
    """把所有 DIMENSION 内 rot=0 的 MTEXT 换成 rotation=90 的 TEXT。

    eDrawings 对 DIMENSION(TH_GBDIM 样式) 的 MTEXT 强制沿尺寸线竖排，
    但把字符间距压扁（62.2 的 6/2/小数点叠一起）。图上其它清晰竖排标注
    都是 TEXT + rotation=90。换成同款即可让每个字符/小数点清楚分开。

    position: 文字贴竖直尺寸线左侧 gap，y 取原标注中点；字高统一 height。
    尺寸线/箭头/图形主体不动。
    """
    fixed = []
    for d in doc.modelspace().query("DIMENSION"):
        # 只处理竖直尺寸线（angle=90）
        angle = d.dxf.get("angle", 0)
        geom = d.dxf.get("geometry", "")
        if not geom or geom not in doc.blocks or abs(float(angle) - 90.0) > 1:
            continue
        blk = doc.blocks[geom]
        mtexts = [e for e in blk if e.dxftype() == "MTEXT"]
        if not mtexts:
            continue  # 已是 TEXT，无需修
        text = mtexts[0].text
        # 竖直尺寸线 x：从块内竖线取
        dim_xs = [round(e.dxf.start.x, 4) for e in blk
                  if e.dxftype() == "LINE" and abs(e.dxf.start.x - e.dxf.end.x) < 0.01]
        if not dim_xs:
            continue
        dim_x = dim_xs[0]
        mid = d.dxf.text_midpoint
        ins = (dim_x - gap, mid.y, 0.0)
        for e in list(blk):
            if e.dxftype() in ("MTEXT", "TEXT"):
                blk.delete_entity(e)
        blk.add_text(text, height=height,
                     dxfattribs={"insert": ins, "rotation": 90.0, "layer": "0"})
        d.dxf.text_midpoint = ins
        fixed.append({"handle": d.dxf.handle, "text": text, "geometry_block": geom,
                      "dim_x": dim_x, "insert": [round(v, 2) for v in ins]})
    return fixed


# ---------------------------------------------------------------- 主流程
def _geom_signature(msp) -> list[tuple]:
    """图形实体签名：LINE/ARC/CIRCLE 逐条坐标，用于验证零改动。"""
    out = []
    for e in msp:
        t = e.dxftype()
        if t == "LINE":
            out.append(("L", round(e.dxf.start.x, 4), round(e.dxf.start.y, 4),
                        round(e.dxf.end.x, 4), round(e.dxf.end.y, 4)))
        elif t in ("ARC", "CIRCLE"):
            out.append((t[0], round(e.dxf.center.x, 4), round(e.dxf.center.y, 4),
                        round(e.dxf.radius, 4)))
    return out


def relabel(src: str | Path, dst: str | Path,
            mode: str = "1to1", base_value: float | None = None,
            base_where: str | None = None, fix_vertical: bool = True,
            unit: str | None = None) -> dict:
    """标注换算主入口（从 clean.dxf 原始基线出发，幂等）。

    src  : 清洗后的 clean.dxf
    dst  : 输出的标注换算后 DXF
    mode : 1to1 / 1to4 / base / cm
    base_value / base_where : base 模式用
    fix_vertical : 是否把压扁的竖向 DIMENSION MTEXT 换成 rot=90 TEXT
    unit : 仅文档记录（mm/cm），由 mode 推导
    """
    src, dst = Path(src), Path(dst)
    doc = ezdxf.readfile(str(src))
    before_geom = _geom_signature(doc.modelspace())
    count_before = len(list(doc.modelspace()))

    rows = measure_all(doc)
    k, k_meta = compute_K(rows, mode, base_value, base_where)

    changes = []
    # 块标注
    for b in doc.blocks:
        if not b.name.startswith("GS_BlockDim"):
            continue
        old, measured = _measure_block(b)
        if not old or old in SPEC_TEXTS or measured <= 0:
            continue
        if mode == "cm" and not LENGTH_RE.match(old):
            continue  # 规格/非纯数字不动
        new = fmt(measured * k, mode)
        for t in b:
            if t.dxftype() == "TEXT":
                t.dxf.text = new
        changes.append({"kind": "BLOCK", "where": b.name, "old": old,
                        "new": new, "measured": round(measured, 4)})
    # 原生 DIMENSION
    for d in doc.modelspace().query("DIMENSION"):
        old = d.dxf.text
        if not old or old in SPEC_TEXTS:
            continue
        if mode == "cm" and not LENGTH_RE.match(old):
            continue
        try:
            meas = float(d.get_measurement())
        except Exception:
            meas = 0.0
        if meas <= 0:
            continue
        new = fmt(meas * k, mode)
        _set_dim_text(doc, d, new)
        changes.append({"kind": "DIM", "where": d.dxf.handle, "old": old,
                        "new": new, "measured": round(meas, 4)})

    fixed = fix_vertical_dims(doc) if fix_vertical else []

    doc.saveas(str(dst))

    # 质检
    chk = ezdxf.readfile(str(dst))
    after_geom = _geom_signature(chk.modelspace())
    audit = chk.audit()
    report = {
        "engine": "annotation_engine",
        "source": str(src), "output": str(dst),
        "mode": mode, "K": round(k, 6), "K_meta": k_meta,
        "unit": unit or ("cm" if mode == "cm" else "mm"),
        "fix_vertical": fix_vertical,
        "stat": {
            "entity_count_before": count_before,
            "entity_count_after": len(list(chk.modelspace())),
            "entity_count_same": count_before == len(list(chk.modelspace())),
            "geometry_identical": before_geom == after_geom,
            "geometry_entity_count": len(after_geom),
        },
        "qa": {
            "audit_errors": len(audit.errors),
            "audit_fixes": len(audit.fixes),
            "passed": (before_geom == after_geom and len(audit.errors) == 0
                       and count_before == len(list(chk.modelspace()))),
        },
        "changes": changes,
        "vertical_fixes": fixed,
    }
    return report


def export_dwg(dxf: str | Path, dwg: str | Path, version: str = "AC1032") -> dict:
    """ODA 高保真导出 DWG。"""
    from dwg_export import export as _export
    return _export(str(dxf), str(dwg), version=version)


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "1to1"
    src = sys.argv[2] if len(sys.argv) > 2 else "output/25QYL-2SL_clean.dxf"
    dst = sys.argv[3] if len(sys.argv) > 3 else f"output/_engine_test_{mode}.dxf"
    r = relabel(src, dst, mode=mode)
    print(json.dumps({k: v for k, v in r.items() if k != "changes"},
                     ensure_ascii=False, indent=2))

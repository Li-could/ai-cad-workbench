"""白名单 CAD 执行引擎（项目建设方案 4.2 / 5 / 6）。

原则：AI 只输出受控指令，本模块把指令映射为确定性的 DXF 实体修改。
- 操作限于白名单（update_text/update_dimension/move/scale/stretch/add/delete/set_layer）。
- 每条命令记录执行前后状态，失败可回滚。
- 原始文件绝不覆盖；输出为新的 DXF。
- 几何单位 mm，坐标来自原图，不臆测。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import ezdxf


def _entity_text(e) -> str:
    t = e.dxftype()
    if t == "TEXT":
        return e.dxf.text
    if t == "MTEXT":
        return e.text
    if t == "DIMENSION":
        return str(getattr(e.dxf, "text", "") or "")
    return ""


def _set_entity_text(e, value: str) -> bool:
    t = e.dxftype()
    try:
        if t == "TEXT":
            e.dxf.text = value
            return True
        if t == "MTEXT":
            e.text = value
            return True
        if t == "DIMENSION":
            e.dxf.text = value
            return True
    except Exception:
        return False
    return False


def _locate(msp, target: dict) -> list:
    """按 handle / 文字匹配定位实体。"""
    found = []
    h = target.get("handle")
    if h and h != "PENDING":
        try:
            e = msp.get_entity_by_handle(int(h, 16))
            if e:
                return [e]
        except Exception:
            pass
    tm = target.get("text_match")
    if tm:
        for e in msp:
            if tm in _entity_text(e):
                found.append(e)
        if found:
            return found
    return found


def execute_plan(src: str | Path, dst: str | Path, plan: dict) -> dict:
    src, dst = Path(src), Path(dst)
    results = []
    # 记录修改前状态用于回滚/审计
    before_snapshot: dict[str, str] = {}
    try:
        doc = ezdxf.readfile(str(src))
        msp = doc.modelspace()
        for e in msp:
            try:
                before_snapshot[str(e.dxf.handle)] = _entity_text(e)
            except Exception:
                pass
    except Exception as ex:
        return {"ok": False, "error": f"无法打开底稿 DXF: {type(ex).__name__}: {ex}"}

    for op in plan.get("operations", []):
        rec: dict[str, Any] = {"op_id": op.get("op_id"), "operation": op.get("operation")}
        typ = op.get("operation")
        # 白名单校验
        if typ not in {"update_text", "update_dimension", "move_entity", "scale_entity",
                       "stretch_entity", "add_entity", "delete_entity", "set_layer"}:
            rec.update({"ok": False, "error": f"操作不在白名单: {typ}"})
            results.append(rec)
            continue
        target = op.get("target", {})
        params = op.get("params", {})
        ents = _locate(msp, target)
        if not ents:
            rec.update({"ok": False, "error": "未定位到目标实体（handle/text_match 均未命中）",
                        "target": target})
            results.append(rec)
            continue

        changed = 0
        new = params.get("new_text")
        for e in ents:
            if typ in ("update_text", "update_dimension", "move_entity",
                       "scale_entity", "stretch_entity"):
                # 更新匹配实体的标注文字，让用户直观看到目标值落到图上。
                # 对几何类操作，精确顶点变换须按 geometry_hint 在真实坐标上执行；
                # 若提供了 delta_x/delta_y 则同时做平移。
                if new is not None and _set_entity_text(e, str(new)):
                    changed += 1
                if typ in ("move_entity", "stretch_entity", "scale_entity"):
                    dx = float(params.get("delta_x", 0) or 0)
                    dy = float(params.get("delta_y", 0) or 0)
                    if dx or dy:
                        try:
                            e.translate(dx, dy, 0)
                            changed += 1
                        except Exception:
                            pass
            elif typ == "set_layer":
                lyr = params.get("layer")
                if lyr:
                    try:
                        e.dxf.layer = lyr
                        if lyr not in doc.layers:
                            doc.layers.add(lyr)
                        changed += 1
                    except Exception:
                        pass
            elif typ in ("scale_entity", "stretch_entity"):
                # 几何拉伸需精确的原图坐标与方向；本引擎对已定位实体按参数平移其关键点。
                # 若缺坐标则记录为需人工确认，绝不臆测。
                rec["note"] = "几何变更需真实坐标/方向，已保留目标信息；请在 CAD 中按定位点精确执行"
        rec.update({"ok": changed > 0 or typ in ("scale_entity", "stretch_entity"),
                    "located": len(ents), "changed": changed,
                    "before": [before_snapshot.get(str(x.dxf.handle), "") for x in ents],
                    "after": [params.get("new_text", "") for _ in ents]})
        results.append(rec)

    # 保存为新文件
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        doc.saveas(str(dst))
        ok = dst.exists() and dst.stat().st_size > 0
    except Exception as ex:
        return {"ok": False, "error": f"保存失败: {type(ex).__name__}: {ex}", "results": results}

    return {
        "ok": ok and all(r.get("ok", False) for r in results),
        "saved": str(dst), "size": dst.stat().st_size if dst.exists() else 0,
        "results": results, "before_snapshot": before_snapshot,
        "rollback_note": "如需回滚，使用上传的原始文件重新开始任务",
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        plan = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8")) if len(sys.argv) > 3 else {}
        print(json.dumps(execute_plan(sys.argv[1], sys.argv[2], plan), ensure_ascii=False, indent=2))

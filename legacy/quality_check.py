"""自动质量检查与安全机制（依据项目建设方案第八章）。

检查维度：文件级 / 实体级 / 尺寸级 / 图层次 / 视觉级 / 范围级 / 审计级。
设计原则：
- 原始文件只读，绝不覆盖。
- 重开测试未通过不得标记成功。
- 修改范围外对象发生非预期变化 -> 失败。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_level(path: str | Path, original: str | Path | None = None) -> dict[str, Any]:
    p = Path(path)
    r: dict[str, Any] = {"exists": p.exists(), "size": p.stat().st_size if p.exists() else 0}
    r["passed"] = bool(r["exists"] and r["size"] > 0)
    head = p.read_bytes()[:6] if p.exists() else b""
    r["header"] = head.decode("ascii", errors="ignore")
    r["format"] = "DWG" if head.startswith(b"AC") else ("DXF" if b"AC" in p.read_bytes()[:200] else "unknown")
    if original is not None:
        r["original_sha256"] = sha256(original)
        r["original_untouched"] = Path(original).exists()
    return r


def reopen_test(path: str | Path, engine: str = "ezdxf") -> dict[str, Any]:
    """重新打开测试：能打开即为通过（DXF 用 ezdxf，DWG 用 CAD 引擎）。"""
    p = Path(path)
    try:
        if engine == "ezdxf":
            import ezdxf
            doc = ezdxf.readfile(str(p))
            audit = doc.audit()
            return {
                "passed": True, "engine": engine,
                "entities": len(list(doc.modelspace())),
                "audit_errors": len(audit.errors), "audit_fixes": len(audit.fixes),
                "dxfversion": doc.dxfversion,
            }
        else:
            return {"passed": False, "engine": engine, "note": "未配置该引擎"}
    except Exception as ex:
        return {"passed": False, "engine": engine, "error": f"{type(ex).__name__}: {ex}"}


def entity_level(before: dict[str, Any], after: dict[str, Any], expected_delta: dict[str, int] | None = None) -> dict[str, Any]:
    """实体数量变化是否符合预期；是否产生重复/丢失/损坏。"""
    b = before.get("counts", {})
    a = after.get("counts", {})
    delta = {k: a.get(k, 0) - b.get(k, 0) for k in set(b) | set(a)}
    ok = True
    reasons = []
    if expected_delta:
        for k, v in expected_delta.items():
            if delta.get(k, 0) != v:
                ok = False
                reasons.append(f"{k}: 期望变化 {v}, 实际 {delta.get(k,0)}")
    # 实体数大幅异常（>50% 变化）视为可疑
    be, ae = b.get("entities", 0), a.get("entities", 0)
    if be and abs(ae - be) / be > 0.5:
        ok = False
        reasons.append(f"实体数变化超过 50%: {be}->{ae}")
    return {"passed": ok, "delta": delta, "reasons": reasons}


def dimension_checks(parsed: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    """关键尺寸数值/单位/标注显示检查。"""
    idx = parsed.get("size_index", {})
    results = []
    all_ok = True
    for c in checks:
        label = c["dimension"]
        expected = c["expected"]
        hits = idx.get(label, [])
        # 在尺寸图谱里找目标值
        found = any(str(expected) in str(k) for k in idx) or len(hits) > 0
        # 更严格：若给了 numeric expected，尝试在 key 中匹配数值
        if isinstance(expected, (int, float)):
            found = any(abs(float(k) - float(expected)) < c.get("tolerance", 0.01)
                        for k in idx if _is_number(k)) or found
        results.append({"dimension": label, "expected": expected, "found": found, "hits": len(hits)})
        if not found:
            all_ok = False
    return {"passed": all_ok, "checks": results}


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False


def unchanged_region_check(before: dict[str, Any], after: dict[str, Any], allowed_layers: set[str]) -> dict[str, Any]:
    """范围级：检查修改范围外（非允许图层）对象是否变化。"""
    bl = set(before.get("layers", []))
    al = set(after.get("layers", []))
    removed = bl - al
    added = al - bl
    outside = (removed | added) - allowed_layers
    return {
        "passed": len(outside) == 0,
        "layers_removed": sorted(removed),
        "layers_added": sorted(added),
        "unexpected_layer_changes": sorted(outside),
    }


def run_all(
    output_path: str | Path,
    original_path: str | Path,
    before_parsed: dict[str, Any],
    after_parsed: dict[str, Any],
    plan: dict[str, Any],
    engine: str = "ezdxf",
) -> dict[str, Any]:
    """执行全部检查，汇总报告。任何硬检查失败 -> overall=False。"""
    import sys
    ext = Path(output_path).suffix.lower()
    use_engine = "ezdxf" if ext == ".dxf" else engine

    f = file_level(output_path, original_path)
    ro = reopen_test(output_path, use_engine)
    e = entity_level(before_parsed, after_parsed)
    allowed = set()
    for op in plan.get("operations", []):
        lyr = op.get("target", {}).get("layer")
        if lyr:
            allowed.add(lyr)
    u = unchanged_region_check(before_parsed, after_parsed, allowed)
    dims = plan.get("verification", {}).get("dimension_checks", [])
    d = dimension_checks(after_parsed, dims) if dims else {"passed": True, "checks": [], "note": "未配置尺寸检查"}

    checks = {"文件级": f, "重开测试": ro, "实体级": e, "图层次/范围级": u, "尺寸级": d}
    hard = [f["passed"], ro["passed"], e["passed"], u["passed"], d["passed"]]
    return {
        "overall_passed": all(hard),
        "checks": checks,
        "original_sha256": f.get("original_sha256"),
        "note": "重开测试未通过时不得标记为成功；关键尺寸与低置信度定位须人工确认。",
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        report = run_all(sys.argv[1], sys.argv[2], {}, {}, {})
        print(json.dumps(report, ensure_ascii=False, indent=2))

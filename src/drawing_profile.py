"""图纸档案：把"每张图的定标与锚点"从代码搬到数据。

一张档案回答四个问题：
1. 这张图是谁？（fingerprint：sha256 / 文件名 / 实体数 / 包围盒 / 特征文字 / 关键 handle）
2. 一个毫米等于多少图面单位？（calibration，纵横向分别定标）
3. 风罩顶、法兰面、电机轴线、接线盒外端这些基准在哪？（datums）
4. 三条规则各自的区域、排除对象、联动尺寸在哪？（rules）

安全边界：``resolve_for()`` 只返回通过结构校验的档案。校验不过就返回 None，
调用方必须拒绝执行几何修改，而不是拿另一张图的常量硬套。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import runtime

runtime.bootstrap()

PROFILE_DIR = Path(__file__).resolve().parent.parent / "knowledge" / "drawings"
REQUIRED_KEYS = ("profile_id", "model", "fingerprint", "calibration", "datums", "original", "rules")


class DrawingProfileError(RuntimeError):
    """档案缺失、格式错误或不适用于当前图纸。"""


class ProfileNotFound(DrawingProfileError):
    pass


class ProfileMismatch(DrawingProfileError):
    def __init__(self, profile_id: str, report: dict):
        self.profile_id = profile_id
        self.report = report
        problems = "；".join(report.get("problems") or ["图纸结构与档案不一致"])
        super().__init__(f"图纸与档案 {profile_id} 不匹配：{problems}")


def _texts(doc) -> list[str]:
    out = []
    for entity in doc.modelspace():
        if entity.dxftype() in ("TEXT", "MTEXT"):
            out.append(str(getattr(entity.dxf, "text", "")))
    return out


@dataclass
class DrawingProfile:
    data: dict[str, Any]
    path: Path | None = None

    # ---------- 基础字段 ----------
    @property
    def profile_id(self) -> str:
        return str(self.data["profile_id"])

    @property
    def model(self) -> str:
        return str(self.data.get("model", self.profile_id))

    @property
    def title(self) -> str:
        return str(self.data.get("title", self.model))

    @property
    def datums(self) -> dict[str, Any]:
        return self.data["datums"]

    @property
    def original(self) -> dict[str, Any]:
        return self.data["original"]

    @property
    def rules(self) -> dict[str, Any]:
        return self.data["rules"]

    @property
    def annotation(self) -> dict[str, Any]:
        return self.data.get("annotation", {})

    @property
    def compare_view(self) -> dict[str, Any]:
        return self.data.get("compare_view", {})

    @property
    def protected_texts(self) -> list[str]:
        return list(self.data.get("protected_texts", []))

    def datum(self, name: str) -> float:
        value = self.datums.get(name)
        if value is None:
            raise DrawingProfileError(f"档案 {self.profile_id} 缺少基准 {name}")
        return float(value)

    def units_per_mm(self, axis: str) -> float:
        key = "vertical" if axis in ("y", "v", "vertical") else "horizontal"
        value = float(self.data["calibration"][key]["units_per_mm"])
        if value <= 0:
            raise DrawingProfileError(f"档案 {self.profile_id} 的 {key} 定标必须为正数")
        return value

    def rule(self, key: str) -> dict[str, Any]:
        rule = self.rules.get(key)
        if rule is None:
            raise DrawingProfileError(f"档案 {self.profile_id} 未定义规则 {key}")
        return rule

    # ---------- 校验 ----------
    def verify(self, doc, source_sha256: str | None = None,
               source_name: str | None = None) -> dict[str, Any]:
        fingerprint = self.data.get("fingerprint", {})
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str, hard: bool = True) -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail, "hard": hard})

        msp = doc.modelspace()
        handles = {entity.dxf.handle for entity in msp}
        texts = _texts(doc)
        entity_count = len(list(msp))

        missing_handles = [h for h in fingerprint.get("required_handles", []) if h not in handles]
        add("关键实体", not missing_handles,
            "全部存在" if not missing_handles else f"缺少 {missing_handles}")

        missing_markers = [m for m in fingerprint.get("markers", [])
                           if not any(m in text for text in texts)]
        add("特征文字", not missing_markers,
            "全部命中" if not missing_markers else f"缺少 {missing_markers}")

        count_range = fingerprint.get("entity_count")
        if count_range:
            low, high = int(count_range[0]), int(count_range[1])
            add("实体规模", low <= entity_count <= high,
                f"实体 {entity_count}，期望 {low}~{high}")

        bbox_rule = fingerprint.get("bbox")
        if bbox_rule:
            try:
                from ezdxf import bbox as _bbox

                ext = _bbox.extents(msp)
                tol = float(bbox_rule.get("tolerance", 5.0))
                expected_min = bbox_rule.get("min", [])
                expected_max = bbox_rule.get("max", [])
                ok = (
                    abs(ext.extmin.x - float(expected_min[0])) <= tol
                    and abs(ext.extmin.y - float(expected_min[1])) <= tol
                    and abs(ext.extmax.x - float(expected_max[0])) <= tol
                    and abs(ext.extmax.y - float(expected_max[1])) <= tol
                )
                # 不同转换器（ODA / aspose）对圆弧外接框的取法略有差异，
                # 因此范围只作为辅助评分，不作为硬拒绝条件。
                add("图形范围", ok,
                    f"bbox=({ext.extmin.x:.1f},{ext.extmin.y:.1f})-"
                    f"({ext.extmax.x:.1f},{ext.extmax.y:.1f})", hard=False)
            except Exception as exc:  # noqa: BLE001
                add("图形范围", False, f"无法计算包围盒：{exc}")

        # 定标自洽：用档案基准反算 units/mm，必须与声明的定标一致
        try:
            vertical = self.data["calibration"]["vertical"]
            span = self.datum(vertical["datum_from"]) - self.datum(vertical["datum_to"])
            derived = span / float(vertical["reference_mm"])
            declared = float(vertical["units_per_mm"])
            ok = abs(derived - declared) / declared < 0.02
            add("纵向定标", ok,
                f"反算 {derived:.4f} vs 声明 {declared:.4f} 单位/mm", hard=False)
        except Exception as exc:  # noqa: BLE001
            add("纵向定标", False, f"无法校验：{exc}", hard=False)

        try:
            horizontal = self.data["calibration"]["horizontal"]
            cross = horizontal.get("cross_check")
            if cross:
                span = self.datum(cross["datum_to"]) - self.datum(cross["datum_from"])
                derived_mm = span / self.units_per_mm("x")
                ok = abs(derived_mm - float(cross["reference_mm"])) < 1.0
                add("横向定标", ok,
                    f"轴线到盒外端 {derived_mm:.1f}mm vs 原值 {cross['reference_mm']}mm",
                    hard=False)
        except Exception as exc:  # noqa: BLE001
            add("横向定标", False, f"无法校验：{exc}", hard=False)

        if source_sha256 and fingerprint.get("sha256"):
            add("SHA-256", source_sha256 == fingerprint["sha256"],
                "一致" if source_sha256 == fingerprint["sha256"] else "文件哈希不同",
                hard=False)

        if source_name and fingerprint.get("filenames"):
            add("文件名", source_name in fingerprint["filenames"],
                source_name, hard=False)

        hard_failures = [c for c in checks if c["hard"] and not c["ok"]]
        soft_failures = [c for c in checks if not c["hard"] and not c["ok"]]
        score = 1.0
        score -= 0.25 * len(hard_failures)
        score -= 0.05 * len(soft_failures)
        problems = [f"{c['name']}：{c['detail']}" for c in checks if not c["ok"]]
        return {
            "profile_id": self.profile_id,
            "ok": not hard_failures,
            "score": round(max(0.0, score), 3),
            "checks": checks,
            "problems": problems,
        }

    def require(self, doc, source_sha256: str | None = None,
                source_name: str | None = None) -> dict[str, Any]:
        report = self.verify(doc, source_sha256, source_name)
        if not report["ok"]:
            raise ProfileMismatch(self.profile_id, report)
        return report


def validate_profile(data: dict[str, Any]) -> None:
    missing = [key for key in REQUIRED_KEYS if key not in data]
    if missing:
        raise DrawingProfileError(f"档案缺少字段：{missing}")
    for axis in ("vertical", "horizontal"):
        try:
            value = float(data["calibration"][axis]["units_per_mm"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DrawingProfileError(f"calibration.{axis}.units_per_mm 无效") from exc
        if value <= 0:
            raise DrawingProfileError(f"calibration.{axis}.units_per_mm 必须为正数")
    for key in ("H2", "D2", "shaft"):
        if key not in data["rules"]:
            raise DrawingProfileError(f"rules 缺少 {key}")
    if not data.get("datums"):
        raise DrawingProfileError("datums 不能为空")


def load_profile(path: str | Path) -> DrawingProfile:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_profile(data)
    return DrawingProfile(data=data, path=path)


def profiles() -> list[DrawingProfile]:
    out = []
    if not PROFILE_DIR.exists():
        return out
    for path in sorted(PROFILE_DIR.glob("*.profile.json")):
        try:
            out.append(load_profile(path))
        except DrawingProfileError:
            continue
    return out


def get_profile(profile_id: str) -> DrawingProfile | None:
    for profile in profiles():
        if profile.profile_id == profile_id:
            return profile
    return None


def resolve_for(doc, source_sha256: str | None = None,
                source_name: str | None = None) -> tuple[DrawingProfile | None, dict[str, Any]]:
    """按图纸结构匹配档案。返回 (命中的档案或 None, 诊断报告)。"""
    best: DrawingProfile | None = None
    best_report: dict[str, Any] = {"ok": False, "problems": ["没有可用档案"]}
    best_score = -1.0
    reports = []
    for profile in profiles():
        report = profile.verify(doc, source_sha256, source_name)
        reports.append(report)
        if report["ok"] and report["score"] > best_score:
            best, best_report, best_score = profile, report, report["score"]
    if best is not None:
        return best, best_report
    return None, {
        "ok": False,
        "problems": ["当前图纸不匹配任何已定标档案"],
        "candidates": reports,
    }


def save_profile(data: dict[str, Any], path: str | Path | None = None) -> Path:
    validate_profile(data)
    target = Path(path) if path else PROFILE_DIR / f"{data['profile_id']}.profile.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


if __name__ == "__main__":
    import sys

    import ezdxf

    default = Path(__file__).resolve().parent.parent / "data/tasks/d663f685b4b9/clean.dxf"
    doc = ezdxf.readfile(sys.argv[1] if len(sys.argv) > 1 else str(default))
    profile, report = resolve_for(doc)
    print(json.dumps({"matched": profile.profile_id if profile else None, "report": report},
                     ensure_ascii=False, indent=2))

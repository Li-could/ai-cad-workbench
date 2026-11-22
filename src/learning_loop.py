"""Evidence-driven self-improvement loop for the CAD workbench.

The loop never rewrites production code by itself. It turns verified runs into
replayable regression cases, classifies failures, produces proposals, and gates
promotions through a sandboxed replay. A human or coding agent still changes the
implementation; the loop decides whether that change survives the learned
baseline.

State layout (default ``data/learning``):

    episodes/      immutable snapshots of task + report outcomes
    proposals/     regression-case and failure-review proposals
    cases/         approved replayable regression baselines
    replays/       replay reports and the latest code fingerprint
    feedback.jsonl append-only acceptance / rejection history
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import runtime

runtime.bootstrap()

import ezdxf

import annotation_engine
import drawing_profile
import dwg_bridge
import dwg_export
import dxf_cleaner
import geometry_modifier
import learning_ledger
import real_mm_engine
import store
import text_edit_engine


ROOT = Path(__file__).resolve().parent.parent
LEARNING_ROOT = ROOT / "data" / "learning"
VERDICTS = {"accepted", "needs_work", "rejected"}
NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
CORE_FILES = (
    "src/real_mm_engine.py",
    "src/annotation_engine.py",
    "src/geometry_modifier.py",
    "src/dwg_bridge.py",
    "src/dwg_export.py",
    "src/dxf_cleaner.py",
    "src/intent_parser.py",
    "src/planner.py",
    "src/drawing_profile.py",
    "src/drawing_measure.py",
    "src/knowledge_base.py",
    "src/calibration.py",
    "src/gen_compare.py",
    "src/edit_intent.py",
    "src/text_edit_engine.py",
    "src/runtime.py",
    "workbench/engine.py",
    "workbench/store.py",
)
CORE_GLOBS = (
    "knowledge/drawings/*.profile.json",
)
CORE_CONFIG_FILES = (
    "knowledge/change_cards.json",
    "knowledge/invariants.json",
    "knowledge/domain_lexicon.json",
    "knowledge/change_card.schema.json",
    "spec/modification_plan.schema.json",
    "skill/rules.json",
    "skill/annotation_rules.json",
)
GATE_FUNCTIONS = (
    "_gate_fingerprint",
    "_hash_file",
    "_hash_obj",
    "_case_integrity_files",
    "_case_artifact_path",
    "_compute_case_integrity",
    "_verify_case_integrity",
    "_prepare_case_source",
    "_numeric",
    "_numeric_annotations",
    "_watermark_count",
    "_dwg_roundtrip",
    "_run_real_mm_case",
    "_run_annotation_case",
    "_run_geometry_case",
    "_run_text_edit_case",
    "_run_case",
    "replay",
)
GATE_ASSIGNMENTS = (
    "NUMERIC_RE",
)

_lock = threading.RLock()

CHECK_GUIDANCE = {
    "view_height": {
        "label": "两视图实体外廓不一致",
        "action": "先量左右视图真实几何外廓，再做底边锚定的 Y 向归一化；不要只改标注。",
        "files": ["src/real_mm_engine.py::_normalize_view_geometry_height"],
    },
    "annotation_consistency": {
        "label": "标注文字与尺寸线实测不一致",
        "action": "在几何校正完成后统一重测并回写标注，避免保留校正前旧值。",
        "files": ["src/real_mm_engine.py::_rewrite_annotations"],
    },
    "annotation_rewritten": {
        "label": "存在未回写的长度标注",
        "action": "检查 GS_BlockDim* 块与原生 DIMENSION 两条来源是否都覆盖。",
        "files": ["src/real_mm_engine.py::measure_all"],
    },
    "entity_count": {
        "label": "实体数量结构异常",
        "action": "核对 ARC→ELLIPSE 的预期转换，并检查是否有误删或复制实体。",
        "files": ["workbench/engine.py::_execute_real_mm"],
    },
    "audit": {
        "label": "DXF 审计未归零",
        "action": "先定位非法实体或 handle 冲突，保存后重新打开再审计。",
        "files": ["src/real_mm_engine.py", "src/dwg_export.py"],
    },
    "reopen": {
        "label": "DWG 往返重开失败",
        "action": "检查 ODA 导出、实体 handle 集合和 DXF 版本是否一致。",
        "files": ["src/dwg_export.py::roundtrip_check"],
    },
    "geometry_unchanged": {
        "label": "纯标注任务改动了图形",
        "action": "回退所有几何写入路径，只允许改 TEXT/MTEXT 与 DIMENSION 显示。",
        "files": ["src/annotation_engine.py::relabel"],
    },
    "verification": {
        "label": "几何目标独立复测失败",
        "action": "从成果文件重新量取 H2/D2/轴伸，不要相信执行期中间变量。",
        "files": ["src/drawing_measure.py", "src/geometry_modifier.py::apply"],
    },
    "side_effects": {
        "label": "范围外实体被修改或删除",
        "action": "核对白名单 handle 与区域排除规则，禁止扩大变形范围。",
        "files": ["src/geometry_modifier.py"],
    },
    "spec_preserved": {
        "label": "规格类文字被误改",
        "action": "把 G1 / G3/4 / 4-Φ11 等规格文本加入不可变集合。",
        "files": ["src/annotation_engine.py::SPEC_TEXTS"],
    },
    "text_verified": {
        "label": "通用文字修改未写入目标值",
        "action": "核对句柄、原文字和目标文字，重新打开成果复测目标实体。",
        "files": ["src/text_edit_engine.py::apply_edits"],
    },
    "untouched_text": {
        "label": "非目标文字被连带修改",
        "action": "按句柄白名单执行，禁止模糊匹配后批量改整张图。",
        "files": ["src/text_edit_engine.py::apply_edits"],
    },
    "rejected_by_user": {
        "label": "用户验收拒绝",
        "action": "提取用户返工描述，补成新的回归用例或失败分类，不直接调低检查门槛。",
        "files": ["src/learning_loop.py"],
    },
    "unknown": {
        "label": "历史任务未分流",
        "action": "保留为观察样本；先补任务类型或复现入口，不进入自动回归。",
        "files": ["src/intent_parser.py"],
    },
}


# ---------------------------------------------------------------- utilities
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _write_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return rows


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_obj(data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clean_id(text: str) -> str:
    value = re.sub(r"[^a-z0-9_-]+", "_", str(text).lower()).strip("_")
    return value[:64] or "case"


def _paths(create: bool = True) -> dict[str, Path]:
    root = Path(LEARNING_ROOT)
    result = {
        "root": root,
        "episodes": root / "episodes",
        "proposals": root / "proposals",
        "cases": root / "cases",
        "replays": root / "replays",
        "quarantine": root / "quarantine",
        "feedback": root / "feedback.jsonl",
        "state": root / "state.json",
    }
    if create:
        for key in ("root", "episodes", "proposals", "cases", "replays", "quarantine"):
            result[key].mkdir(parents=True, exist_ok=True)
    return result


def _task_dirs():
    return Path(store.TASKS)


def _code_fingerprint() -> str:
    rows = []
    seen = set()
    for relative in CORE_FILES:
        path = ROOT / relative
        rows.append([relative, _hash_file(path) if path.is_file() else "missing"])
        seen.add(path.resolve())
    for relative in CORE_CONFIG_FILES:
        path = ROOT / relative
        if path.is_file():
            rows.append([relative, _hash_file(path)])
            seen.add(path.resolve())
    for pattern in CORE_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            resolved = path.resolve()
            if not path.is_file() or resolved in seen:
                continue
            rows.append([path.relative_to(ROOT).as_posix(), _hash_file(path)])
            seen.add(resolved)
    return _hash_obj(rows)


def _gate_fingerprint(source: str | None = None) -> str:
    """Fingerprint the replay and delivery gates independently of UI/audit code."""
    text = source if source is not None else Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(text)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assignments = {}
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                assignments[target.id] = node
    rows = []
    for name in GATE_FUNCTIONS:
        node = functions.get(name)
        rows.append(
            [
                name,
                ast.dump(node, include_attributes=False) if node is not None else "missing",
            ]
        )
    for name in GATE_ASSIGNMENTS:
        node = assignments.get(name)
        rows.append(
            [
                name,
                ast.dump(node, include_attributes=False) if node is not None else "missing",
            ]
        )
    return _hash_obj(rows)


def _ledger_event(
    event_type: str,
    payload: Any = None,
    entity_type: str = "",
    entity_id: str = "",
    actor: str = "system",
) -> dict:
    try:
        return learning_ledger.record_event(
            _paths()["root"],
            event_type,
            payload=payload,
            entity_type=entity_type,
            entity_id=entity_id,
            actor=actor,
        )
    except Exception as exc:  # noqa: BLE001
        error = {
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "actor": actor,
            "error": f"{type(exc).__name__}: {exc}",
            "created_at": _now(),
        }
        _write_jsonl(_paths()["root"] / "ledger_errors.jsonl", error)
        return {"ok": False, **error}


def _numeric(text: Any) -> bool:
    return bool(NUMERIC_RE.fullmatch(str(text or "").strip()))


def _compact_check(check: dict) -> dict:
    return {
        key: value
        for key, value in check.items()
        if key not in {"rows", "left", "right", "examples"}
    }


# ---------------------------------------------------------------- feedback
def _feedback_rows() -> list[dict]:
    return _read_jsonl(_paths()["feedback"])


def _latest_feedback_map() -> dict[tuple[str, int | None], dict]:
    latest: dict[tuple[str, int | None], dict] = {}
    for row in _feedback_rows():
        key = (str(row.get("task_id")), row.get("version"))
        latest[key] = row
    return latest


def _invalidate_cases_for_feedback(
    task_id: str,
    version: int,
    verdict: str,
    feedback_id: str,
    by: str,
) -> list[dict]:
    """Retire baselines derived from a task version the user rejected."""
    invalidated = []
    for case in list_cases(include_invalidated=True):
        if str(case.get("origin_task_id")) != str(task_id):
            continue
        origin_version = case.get("origin_version")
        try:
            same_version = int(origin_version) == int(version)
        except (TypeError, ValueError):
            same_version = origin_version is None
        if not same_version:
            continue
        path = Path(case["_path"])
        row = {key: value for key, value in case.items() if key != "_path"}
        if (row.get("status") or "active") == "invalidated":
            continue
        row.update(
            {
                "status": "invalidated",
                "invalidated_at": _now(),
                "invalidated_by": by,
                "invalidation_reason": f"用户验收 {verdict}",
                "invalidated_by_feedback": feedback_id,
                "invalidated_task_version": version,
            }
        )
        _write_json(path, row)
        invalidated.append(
            {
                "case_id": row.get("case_id"),
                "title": row.get("title"),
                "reason": row["invalidation_reason"],
            }
        )
    return invalidated


def record_feedback(
    task_id: str,
    verdict: str,
    note: str = "",
    version: int | None = None,
    by: str = "user",
) -> dict:
    """Append immutable feedback and mirror the latest verdict onto the task."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict 必须是 {sorted(VERDICTS)}")
    task = store.get_task(task_id)
    if task is None:
        raise ValueError(f"任务不存在：{task_id}")
    version = int(version if version is not None else task.get("current_version", 0))
    record = {
        "feedback_id": f"fb_{task_id}_{version}_{_stamp()}",
        "task_id": task_id,
        "version": version,
        "verdict": verdict,
        "note": note,
        "by": by,
        "created_at": _now(),
    }
    with _lock:
        invalidated_cases = (
            _invalidate_cases_for_feedback(
                task_id,
                version,
                verdict,
                record["feedback_id"],
                by,
            )
            if verdict in {"needs_work", "rejected"}
            else []
        )
        record["invalidated_cases"] = invalidated_cases
        _write_jsonl(_paths()["feedback"], record)
        task.setdefault("feedback", []).append(record)
        task["acceptance"] = {
            "verdict": verdict,
            "note": note,
            "by": by,
            "version": version,
            "at": record["created_at"],
        }
        store.save_task(task)
        event = _ledger_event(
            "feedback",
            payload={
                "feedback_id": record["feedback_id"],
                "verdict": verdict,
                "note": note,
                "version": version,
                "invalidated_cases": invalidated_cases,
            },
            entity_type="task",
            entity_id=task_id,
            actor=by,
        )
        record["ledger_event_id"] = event.get("event_id")
    return record


# ---------------------------------------------------------------- episodes
def _task_report(task: dict, task_dir: Path) -> tuple[dict | None, Path | None]:
    raw_path = task.get("report")
    candidates = []
    if raw_path:
        candidates.append(Path(raw_path))
    candidates.append(task_dir / "report.json")
    for path in candidates:
        if path.is_file():
            report = _read_json(path)
            if isinstance(report, dict):
                return report, path
    return None, None


def _normalized_checks(report: dict | None) -> dict[str, bool]:
    if not report:
        return {}
    checks = {}
    for name, value in (report.get("checks") or {}).items():
        if isinstance(value, dict):
            checks[name] = bool(value.get("passed", value.get("ok", False)))
        else:
            checks[name] = bool(value)
    if not checks and "ok" in report:
        checks["report_ok"] = bool(report["ok"])
    return checks


def _latest_task_feedback(task: dict, feedback_map: dict) -> dict | None:
    version = int(task.get("current_version", 0))
    return feedback_map.get((str(task["task_id"]), version))


def _source_candidates(task: dict, task_dir: Path) -> list[Path]:
    rows = []
    source = (task.get("files") or {}).get("source") or {}
    if source.get("path"):
        rows.append(Path(source["path"]))
    rows.extend([task_dir / "work_copy.dwg", task_dir / "clean.dxf"])
    unique = []
    seen = set()
    for path in rows:
        key = str(path).lower()
        if key not in seen and path.is_file():
            unique.append(path)
            seen.add(key)
    return unique


def _source_sha(task: dict, candidates: list[Path]) -> str:
    source = (task.get("files") or {}).get("source") or {}
    if source.get("sha256"):
        return str(source["sha256"])
    return _hash_file(candidates[0]) if candidates else ""


def _select_intent(task: dict) -> dict:
    intent = task.get("intent")
    if not intent:
        intent = (task.get("plan") or {}).get("intent")
    if not intent:
        intent = {}
    return json.loads(json.dumps(intent, ensure_ascii=False))


def _intent_signature(task_type: str, task: dict, intent: dict) -> dict:
    if task_type == "real_mm":
        return {
            "length_value": intent.get("length_value"),
            "height_value": intent.get("height_value"),
            "length_anchor": intent.get("length_anchor"),
            "height_anchor": intent.get("height_anchor"),
        }
    if task_type == "annotation":
        return {
            "mode": intent.get("mode"),
            "base_value": intent.get("base_value"),
            "base_where": intent.get("base_where"),
            "fix_vertical": bool(intent.get("fix_vertical")),
            "unit": intent.get("unit"),
        }
    if task_type == "text_edit":
        return {
            "edits": [
                {
                    "handle": row.get("handle"),
                    "old": row.get("old"),
                    "new": row.get("new"),
                    "mode": row.get("mode"),
                    "dimension_effect": row.get("dimension_effect"),
                    "shared_effect": row.get("shared_effect"),
                }
                for row in intent.get("edits") or []
            ]
        }
    if task_type == "geometry":
        return {
            "profile_id": task.get("profile_id"),
            "targets": intent.get("targets") or {
                key: intent.get(key) for key in ("H2", "D2", "shaft")
            },
            "enabled": intent.get("enabled"),
        }
    return {"intent": intent}


def _episode_fingerprint(task_type: str, task: dict, source_sha: str, intent: dict) -> str:
    signature = {
        "task_type": task_type,
        "source_sha256": source_sha,
        "intent": _intent_signature(task_type, task, intent),
    }
    return _hash_obj(signature)


def _metrics_for(task_type: str, report: dict | None, intent: dict) -> dict:
    if not report:
        return {}
    if task_type == "real_mm":
        calibration = report.get("calibration") or {}
        annotation = report.get("annotation") or {}
        return {
            "kx": calibration.get("kx"),
            "ky": calibration.get("ky"),
            "change_count": len(report.get("changes") or annotation.get("changes") or []),
            "view_height": (report.get("checks", {}).get("view_height") or {}).get("reference"),
            "target_length": (calibration.get("targets") or {}).get("length"),
            "target_height": (calibration.get("targets") or {}).get("height"),
        }
    if task_type == "annotation":
        annotation = report.get("annotation") or {}
        return {
            "mode": annotation.get("mode") or intent.get("mode"),
            "K": annotation.get("K"),
            "change_count": len(annotation.get("changes") or report.get("changes") or []),
        }
    if task_type == "text_edit":
        text_edit = report.get("text_edit") or {}
        return {
            "change_count": len(text_edit.get("changes") or report.get("changes") or []),
            "geometry_identical": (text_edit.get("stat") or {}).get(
                "geometry_identical"
            ),
        }
    if task_type == "geometry":
        geometry = report.get("geometry") or {}
        verification = geometry.get("verification") or {}
        return {
            "profile_id": geometry.get("profile_id") or report.get("profile_id"),
            "targets": geometry.get("targets_used"),
            "verification_passed": verification.get("passed"),
            "change_count": report.get("change_count"),
        }
    return {}


def _make_episode(task: dict, task_dir: Path, feedback_map: dict) -> dict:
    report, report_path = _task_report(task, task_dir)
    task_type = task.get("task_type") or "unknown"
    intent = _select_intent(task)
    checks = _normalized_checks(report)
    failed_checks = sorted(name for name, passed in checks.items() if not passed)
    sources = _source_candidates(task, task_dir)
    source_sha = _source_sha(task, sources)
    feedback = _latest_task_feedback(task, feedback_map)
    feedback_verdict = feedback.get("verdict") if feedback else None

    if task_type == "unknown":
        # 早期任务没有 task_type / intent 元数据，不能当成真实失败；
        # 保留为 legacy 供追溯，但不进入失败归类，也不自动生成回归候选。
        outcome = "legacy"
    elif task.get("status") in {"needs_calibration", "needs_instruction"}:
        # 未定标、或需求信息不足被拒绝，都是安全边界生效，不是执行失败。
        outcome = task["status"]
    elif task.get("status") != "executed" and not report:
        outcome = "incomplete"
    elif report is None:
        outcome = "missing_report"
    elif report.get("ok") is not True:
        outcome = "failed"
    elif checks and not all(checks.values()):
        outcome = "failed"
    elif feedback_verdict in {"needs_work", "rejected"}:
        outcome = "rejected_by_user"
    else:
        outcome = "passed"

    last_error = next(
        (
            row.get("msg")
            for row in reversed(task.get("logs") or [])
            if row.get("ok") is False
        ),
        None,
    )
    artifacts = {}
    for name, filename in (
        ("dxf", "result.dxf"),
        ("dwg", "result.dwg"),
        ("report", "report.json"),
        ("before_png", "before.png"),
        ("after_png", "after.png"),
    ):
        path = task_dir / filename
        if path.is_file():
            artifacts[name] = str(path)
    if report_path is not None:
        artifacts["report"] = str(report_path)

    report_hash = _hash_obj(report) if report else "missing"
    episode_id = f"ep_{task['task_id']}_{report_hash[:10]}"
    fingerprint = _episode_fingerprint(task_type, task, source_sha, intent)
    source = (task.get("files") or {}).get("source") or {}
    return {
        "episode_id": episode_id,
        "task_id": task["task_id"],
        "version": int(task.get("current_version", 0)),
        "task_type": task_type,
        "status": task.get("status"),
        "task_updated_at": task.get("updated_at"),
        "outcome": outcome,
        "instruction": task.get("instruction"),
        "source": {
            "name": source.get("name") or (sources[0].name if sources else None),
            "path": str(sources[0]) if sources else None,
            "sha256": source_sha,
            "fallback_paths": [str(path) for path in sources[1:]],
        },
        "intent": intent,
        "fingerprint": fingerprint,
        "report_hash": report_hash,
        "report_path": str(report_path) if report_path else None,
        "artifacts": artifacts,
        "checks": checks,
        "failed_checks": failed_checks,
        "last_error": last_error,
        "metrics": _metrics_for(task_type, report, intent),
        "feedback": feedback,
        "recipe_id": task.get("recipe_id"),
        "profile_id": task.get("profile_id"),
        "approval": task.get("approval"),
        "code_fingerprint": _code_fingerprint(),
        "observed_at": _now(),
    }


def observe() -> dict:
    """Scan workbench tasks and persist deduplicated episode snapshots."""
    paths = _paths()
    feedback_map = _latest_feedback_map()
    created = 0
    updated = 0
    task_count = 0
    with _lock:
        for manifest in sorted(_task_dirs().glob("*/manifest.json")):
            task = _read_json(manifest)
            if not isinstance(task, dict):
                continue
            task_count += 1
            episode = _make_episode(task, manifest.parent, feedback_map)
            episode_path = paths["episodes"] / f"{episode['episode_id']}.json"
            old = _read_json(episode_path)
            if old:
                episode["observed_at"] = old.get("observed_at", episode["observed_at"])
                episode["last_seen_at"] = _now()
                changed = {
                    key: value
                    for key, value in episode.items()
                    if key not in {"observed_at", "last_seen_at"}
                    and old.get(key) != value
                }
                if changed:
                    _write_json(episode_path, episode)
                    updated += 1
            else:
                episode["first_seen_at"] = episode["observed_at"]
                _write_json(episode_path, episode)
                created += 1
    episodes = list_episodes()
    result = {
        "task_count": task_count,
        "episode_count": len(episodes),
        "created": created,
        "updated": updated,
        "generated_at": _now(),
    }
    _ledger_event("observe", payload=result, entity_type="episode")
    return result


def list_episodes() -> list[dict]:
    rows = []
    for path in _paths()["episodes"].glob("*.json"):
        row = _read_json(path)
        if row:
            rows.append(row)
    return sorted(rows, key=lambda row: row.get("observed_at", ""), reverse=True)


def _latest_by_fingerprint(episodes: list[dict]) -> dict[str, dict]:
    latest = {}
    for episode in sorted(
        episodes,
        key=lambda row: (
            row.get("task_updated_at") or "",
            row.get("observed_at") or "",
        ),
    ):
        latest[episode.get("fingerprint") or episode["episode_id"]] = episode
    return latest


# ---------------------------------------------------------------- diagnosis
def diagnose(episodes: list[dict] | None = None) -> dict:
    episodes = episodes if episodes is not None else list_episodes()
    latest = _latest_by_fingerprint(episodes)
    outcomes = Counter(row["outcome"] for row in latest.values())
    feedback = Counter(
        (row.get("feedback") or {}).get("verdict")
        for row in latest.values()
        if (row.get("feedback") or {}).get("verdict")
    )

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for episode in latest.values():
        if episode["outcome"] in {"failed", "missing_report", "incomplete", "rejected_by_user"}:
            keys = episode.get("failed_checks") or []
            if episode["outcome"] == "rejected_by_user":
                keys = ["rejected_by_user"]
            if not keys:
                keys = [episode["outcome"]]
            for key in keys:
                groups[(episode["task_type"], key)].append(episode)

    patterns = []
    for (task_type, key), rows in sorted(groups.items(), key=lambda item: -len(item[1])):
        guidance = CHECK_GUIDANCE.get(
            key,
            {
                "label": key,
                "action": "查看失败报告和最后一条错误日志，先补可复现用例，再改实现。",
                "files": [],
            },
        )
        patterns.append(
            {
                "key": key,
                "task_type": task_type,
                "label": guidance["label"],
                "count": len(rows),
                "episode_ids": [row["episode_id"] for row in rows],
                "suggested_action": guidance["action"],
                "files": guidance["files"],
            }
        )

    unreviewed = [
        row
        for row in latest.values()
        if row["outcome"] == "passed" and not row.get("feedback")
    ]
    accepted = [
        row
        for row in latest.values()
        if (row.get("feedback") or {}).get("verdict") == "accepted"
    ]
    return {
        "episode_count": len(latest),
        "outcomes": dict(outcomes),
        "feedback": dict(feedback),
        "failure_patterns": patterns,
        "unreviewed_success": [
            {
                "episode_id": row["episode_id"],
                "task_id": row["task_id"],
                "task_type": row["task_type"],
                "source": (row.get("source") or {}).get("name"),
            }
            for row in unreviewed
        ],
        "accepted_success": [
            {"episode_id": row["episode_id"], "task_id": row["task_id"]}
            for row in accepted
        ],
    }


# ---------------------------------------------------------------- proposals
def _replacement_fragment(before: str, after: str) -> str:
    before = str(before or "")
    after = str(after or "")
    prefix = 0
    limit = min(len(before), len(after))
    while prefix < limit and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < len(before) - prefix
        and suffix < len(after) - prefix
        and before[-1 - suffix] == after[-1 - suffix]
    ):
        suffix += 1
    end = len(after) - suffix if suffix else len(after)
    return after[prefix:end]


def _expected_from_report(task_type: str, task: dict, report: dict | None, intent: dict) -> dict:
    report = report or {}
    expected: dict[str, Any] = {}
    if task_type == "real_mm":
        changes = report.get("changes") or (report.get("annotation") or {}).get("changes") or []
        expected = {
            "annotation_texts": sorted(str(row.get("new")) for row in changes if row.get("new")),
            "annotation_count": len(changes),
            "targets": {
                "length": intent.get("length_value"),
                "height": intent.get("height_value"),
                "length_anchor": intent.get("length_anchor"),
                "height_anchor": intent.get("height_anchor"),
            },
        }
    elif task_type == "annotation":
        changes = (report.get("annotation") or {}).get("changes") or report.get("changes") or []
        expected = {
            "annotation_texts": sorted(str(row.get("new")) for row in changes if row.get("new")),
            "annotation_count": len(changes),
            "mode": intent.get("mode"),
            "fix_vertical": bool(intent.get("fix_vertical")),
            "unit": intent.get("unit"),
        }
    elif task_type == "text_edit":
        changes = (report.get("text_edit") or {}).get("changes") or report.get("changes") or []
        intent_by_handle = {
            str(row.get("handle")): row
            for row in intent.get("edits") or []
            if row.get("handle")
        }
        expected = {
            "text_edits": [
                {
                    "handle": row.get("handle"),
                    "old": (
                        row.get("old_fragment")
                        or (intent_by_handle.get(str(row.get("handle"))) or {}).get("old")
                        or row.get("old")
                    ),
                    "text_before": row.get("text_before") or row.get("old"),
                    "new": (
                        row.get("new_fragment")
                        or (
                            _replacement_fragment(row.get("old"), row.get("new"))
                            if (row.get("mode") or "exact") == "replace"
                            else row.get("new")
                        )
                    ),
                    "expected_text": row.get("new"),
                    "mode": row.get("mode") or "exact",
                    "is_dimension": bool(row.get("is_dimension")),
                    "dimension_source": row.get("dimension_source"),
                    "dimension_effect": (
                        row.get("dimension_effect")
                        or (intent_by_handle.get(str(row.get("handle"))) or {}).get(
                            "dimension_effect"
                        )
                    ),
                    "dimension_block": row.get("dimension_block"),
                    "shared_effect": (
                        row.get("shared_effect")
                        or (intent_by_handle.get(str(row.get("handle"))) or {}).get(
                            "shared_effect"
                        )
                    ),
                }
                for row in changes
                if row.get("handle") and row.get("new") is not None
            ],
            "text_edit_count": len(changes),
        }
    elif task_type == "geometry":
        geometry = report.get("geometry") or {}
        expected = {
            "profile_id": geometry.get("profile_id") or task.get("profile_id"),
            "targets_used": geometry.get("targets_used"),
            "measured_after": geometry.get("measured_after"),
            "total_height": (geometry.get("TOTAL_HEIGHT") or {}).get("new_mm"),
            "verification": geometry.get("verification"),
        }
    return expected


def _proposal_title(task_type: str, task: dict, intent: dict, source_name: str | None) -> str:
    source_name = source_name or task.get("task_id", "drawing")
    if task_type == "real_mm":
        return (
            f"{source_name} · 真实毫米 "
            f"{intent.get('length_value')}×{intent.get('height_value')} mm"
        )
    if task_type == "annotation":
        return f"{source_name} · 标注 {intent.get('mode') or '修复'}"
    if task_type == "text_edit":
        edits = intent.get("edits") or []
        return f"{source_name} · 文字/标注修改 {len(edits)} 处"
    if task_type == "geometry":
        return f"{source_name} · 几何 {task.get('profile_id') or ''}".strip()
    return f"{source_name} · {task_type}"


def list_proposals(status: str | None = None) -> list[dict]:
    rows = []
    for path in _paths()["proposals"].glob("*.json"):
        row = _read_json(path)
        if row and (status is None or row.get("status") == status):
            rows.append(row)
    return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)


def list_cases(include_invalidated: bool = False) -> list[dict]:
    rows = []
    for path in _paths()["cases"].glob("*/case.json"):
        row = _read_json(path)
        if row and (
            include_invalidated or (row.get("status") or "active") == "active"
        ):
            row["_path"] = str(path)
            rows.append(row)
    return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)


def list_cases_with_integrity(include_invalidated: bool = False) -> list[dict]:
    rows = list_cases(include_invalidated=include_invalidated)
    for row in rows:
        row["_integrity"] = _verify_case_integrity(row, Path(row["_path"]).parent)
    return rows


def _existing_proposal_fingerprints() -> set[str]:
    active_case_ids = {row.get("case_id") for row in list_cases()}
    fingerprints = set()
    for row in list_proposals():
        fingerprint = row.get("fingerprint")
        if not fingerprint:
            continue
        if row.get("status") == "promoted" and row.get("case_id") not in active_case_ids:
            continue
        fingerprints.add(fingerprint)
    return fingerprints


def propose() -> dict:
    """Create regression-case and failure-review proposals from latest outcomes."""
    paths = _paths()
    episodes = list_episodes()
    latest = _latest_by_fingerprint(episodes)
    existing_cases = {row.get("fingerprint") for row in list_cases()}
    existing_proposals = _existing_proposal_fingerprints()
    created = []

    candidates = []
    for episode in latest.values():
        if episode["outcome"] != "passed":
            continue
        if episode["task_type"] not in {
            "real_mm",
            "annotation",
            "geometry",
            "text_edit",
        }:
            continue
        artifacts = episode.get("artifacts") or {}
        if "dxf" not in artifacts:
            continue
        if not (episode.get("source") or {}).get("path"):
            continue
        candidates.append(episode)

    candidates.sort(key=lambda row: row.get("observed_at", ""), reverse=True)
    for episode in candidates:
        fingerprint = episode.get("fingerprint")
        if not fingerprint or fingerprint in existing_cases or fingerprint in existing_proposals:
            continue
        task = store.get_task(episode["task_id"]) or {}
        report, _ = _task_report(task, _task_dirs() / episode["task_id"])
        expected = _expected_from_report(
            episode["task_type"], task, report, episode.get("intent") or {}
        )
        if episode["task_type"] in {"real_mm", "annotation"} and not expected.get("annotation_texts"):
            continue
        if episode["task_type"] == "text_edit" and not expected.get("text_edits"):
            continue
        proposal_id = f"pr_{fingerprint[:12]}"
        evidence = (episode.get("feedback") or {}).get("verdict")
        proposal = {
            "proposal_id": proposal_id,
            "kind": "regression_case",
            "status": "proposed",
            "title": _proposal_title(
                episode["task_type"],
                task,
                episode.get("intent") or {},
                (episode.get("source") or {}).get("name"),
            ),
            "fingerprint": fingerprint,
            "task_type": episode["task_type"],
            "episode_id": episode["episode_id"],
            "task_id": episode["task_id"],
            "evidence": {
                "outcome": "passed",
                "feedback": evidence,
                "checks": episode.get("checks"),
                "metrics": episode.get("metrics"),
                "report_hash": episode.get("report_hash"),
            },
            "payload": {
                "expected": expected,
                "source_sha256": (episode.get("source") or {}).get("sha256"),
                "intent": episode.get("intent"),
            },
            "requires": "隔离重放全部硬检查通过后，才写入回归基线",
            "created_at": _now(),
        }
        _write_json(paths["proposals"] / f"{proposal_id}.json", proposal)
        created.append(proposal)
        existing_proposals.add(fingerprint)

    diagnosis = diagnose(episodes)
    for pattern in diagnosis["failure_patterns"]:
        if pattern["task_type"] == "unknown":
            continue
        failure_key = _hash_obj(
            {
                "task_type": pattern["task_type"],
                "check": pattern["key"],
                "pattern": "failure_review",
            }
        )
        if failure_key in existing_proposals:
            continue
        proposal_id = f"pr_fail_{failure_key[:10]}"
        proposal = {
            "proposal_id": proposal_id,
            "kind": "failure_review",
            "status": "proposed",
            "title": f"{pattern['task_type']} · {pattern['label']}",
            "fingerprint": failure_key,
            "task_type": pattern["task_type"],
            "episode_ids": pattern["episode_ids"],
            "evidence": pattern,
            "payload": {
                "suggested_action": pattern["suggested_action"],
                "files": pattern["files"],
            },
            "requires": "人工确认根因；不自动修改代码，也不自动降低质检门槛",
            "created_at": _now(),
        }
        _write_json(paths["proposals"] / f"{proposal_id}.json", proposal)
        created.append(proposal)
        existing_proposals.add(failure_key)

    result = {
        "created": len(created),
        "proposals": created,
        "pending": len(list_proposals("proposed")),
        "generated_at": _now(),
    }
    _ledger_event(
        "propose",
        payload={
            "created": result["created"],
            "pending": result["pending"],
            "proposal_ids": [
                row.get("proposal_id") for row in result["proposals"]
            ],
        },
        entity_type="proposal",
    )
    return result


def _copy_if_exists(source: Path, target: Path) -> bool:
    if not source.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def _case_integrity_files(meta: dict) -> list[str]:
    files = []
    source_path = (meta.get("source") or {}).get("path")
    if source_path:
        files.append(str(source_path))
    for key in ("expected_dxf", "expected_dwg", "expected_report"):
        relative = (meta.get("artifacts") or {}).get(key)
        if relative and str(relative) not in files:
            files.append(str(relative))
    return files


def _case_artifact_path(case_dir: Path, relative: str) -> Path:
    root = case_dir.resolve()
    path = (case_dir / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"回归用例包含非法相对路径：{relative}") from exc
    return path


def _compute_case_integrity(meta: dict, case_dir: Path) -> dict:
    files = {}
    for relative in _case_integrity_files(meta):
        path = _case_artifact_path(case_dir, relative)
        if not path.is_file():
            raise FileNotFoundError(f"回归用例文件缺失，不能封存：{relative}")
        files[relative] = _hash_file(path)
    return {
        "algorithm": "sha256",
        "sealed_at": _now(),
        "files": files,
        "metadata_sha256": _hash_obj(
            {
                key: value
                for key, value in meta.items()
                if key not in {"integrity", "_path", "_integrity"}
            }
        ),
    }


def _verify_case_integrity(meta: dict, case_dir: Path) -> dict:
    integrity = meta.get("integrity") or {}
    expected = integrity.get("files") or {}
    if not expected:
        return {
            "ok": True,
            "passed": True,
            "status": "unsealed",
            "sealed": False,
            "changed": [],
            "missing": [],
        }
    changed = []
    missing = []
    for relative, digest in sorted(expected.items()):
        try:
            path = _case_artifact_path(case_dir, relative)
        except ValueError:
            changed.append(relative)
            continue
        if not path.is_file():
            missing.append(relative)
        elif _hash_file(path) != digest:
            changed.append(relative)
    expected_metadata = integrity.get("metadata_sha256")
    actual_metadata = _hash_obj(
        {
            key: value
            for key, value in meta.items()
            if key not in {"integrity", "_path", "_integrity"}
        }
    )
    metadata_changed = bool(expected_metadata and expected_metadata != actual_metadata)
    metadata_sealed = bool(expected_metadata)
    ok = not changed and not missing and not metadata_changed
    if changed or missing or metadata_changed:
        status = "broken"
    elif not metadata_sealed:
        status = "unsealed"
    else:
        status = "sealed"
    return {
        "ok": ok,
        "passed": ok,
        "status": status,
        "sealed": True,
        "changed": changed,
        "missing": missing,
        "metadata_changed": metadata_changed,
        "metadata_sealed": metadata_sealed,
    }


def _build_case_staging(proposal: dict) -> tuple[Path, dict]:
    paths = _paths()
    episode = _read_json(paths["episodes"] / f"{proposal['episode_id']}.json")
    if not episode:
        raise ValueError(f"episode 不存在：{proposal['episode_id']}")
    task = store.get_task(episode["task_id"])
    if not task:
        raise ValueError(f"task 不存在：{episode['task_id']}")
    task_dir = _task_dirs() / episode["task_id"]
    report, _ = _task_report(task, task_dir)
    if not report:
        raise ValueError("任务缺少 report，不能作为回归基线")

    sources = [Path(episode["source"]["path"])] if episode["source"].get("path") else []
    sources.extend(Path(path) for path in (episode["source"].get("fallback_paths") or []))
    source = next((path for path in sources if path.is_file()), None)
    if source is None:
        raise ValueError("原始输入文件已丢失，不能生成可重放用例")

    artifacts = episode.get("artifacts") or {}
    expected_dxf = Path(artifacts["dxf"]) if artifacts.get("dxf") else None
    if expected_dxf is None or not expected_dxf.is_file():
        raise ValueError("缺少 expected DXF，不能生成可重放用例")
    expected_dwg = Path(artifacts["dwg"]) if artifacts.get("dwg") else None

    fingerprint = proposal["fingerprint"]
    case_id = _clean_id(f"case_{proposal['task_type']}_{episode['source']['sha256'][:8]}_{fingerprint[:8]}")
    staging = Path(tempfile.mkdtemp(prefix=".stage_case_", dir=paths["cases"]))
    source_name = f"source{source.suffix.lower()}"
    _copy_if_exists(source, staging / source_name)
    _copy_if_exists(expected_dxf, staging / "expected.dxf")
    if expected_dwg:
        _copy_if_exists(expected_dwg, staging / "expected.dwg")
    _copy_if_exists(task_dir / "report.json", staging / "expected_report.json")

    expected = _expected_from_report(
        proposal["task_type"], task, report, episode.get("intent") or {}
    )
    case_meta = {
        "case_id": case_id,
        "status": "active",
        "title": proposal["title"],
        "task_type": proposal["task_type"],
        "fingerprint": fingerprint,
        "created_at": _now(),
        "origin_task_id": episode["task_id"],
        "origin_episode_id": episode["episode_id"],
        "origin_version": episode.get("version"),
        "source": {
            "path": source_name,
            "name": episode["source"].get("name") or source.name,
            "sha256": episode["source"].get("sha256") or _hash_file(source),
        },
        "intent": episode.get("intent") or {},
        "profile_id": episode.get("profile_id") or task.get("profile_id"),
        "expected": expected,
        "artifacts": {
            "expected_dxf": "expected.dxf",
            "expected_dwg": "expected.dwg" if expected_dwg else None,
            "expected_report": "expected_report.json",
        },
        "recipe_id": episode.get("recipe_id"),
    }
    case_meta["integrity"] = _compute_case_integrity(case_meta, staging)
    _write_json(staging / "case.json", case_meta)
    return staging, case_meta


def seal_cases() -> dict:
    """Seal legacy active cases; already broken cases must be re-approved."""
    sealed = []
    skipped = []
    blocked = []
    for case in list_cases():
        path = Path(case["_path"])
        case_dir = path.parent
        row = {key: value for key, value in case.items() if key != "_path"}
        integrity = _verify_case_integrity(row, case_dir)
        if integrity["status"] == "sealed":
            skipped.append(row.get("case_id"))
            continue
        if integrity["status"] == "broken":
            blocked.append(
                {
                    "case_id": row.get("case_id"),
                    "reason": "既有封存哈希不匹配，禁止原地重封",
                    "changed": integrity["changed"],
                    "missing": integrity["missing"],
                }
            )
            continue
        try:
            row["integrity"] = _compute_case_integrity(row, case_dir)
        except Exception as exc:  # noqa: BLE001
            blocked.append(
                {
                    "case_id": row.get("case_id"),
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        _write_json(path, row)
        sealed.append(row.get("case_id"))
    result = {
        "ok": not blocked,
        "sealed": sealed,
        "skipped": skipped,
        "blocked": blocked,
        "sealed_count": len(sealed),
        "skipped_count": len(skipped),
        "blocked_count": len(blocked),
        "generated_at": _now(),
    }
    _ledger_event(
        "cases_sealed",
        payload=result,
        entity_type="case",
        actor="system",
    )
    return result


def reject_proposal(proposal_id: str, note: str = "", by: str = "user") -> dict:
    path = _paths()["proposals"] / f"{proposal_id}.json"
    proposal = _read_json(path)
    if not proposal:
        raise ValueError(f"提案不存在：{proposal_id}")
    if proposal.get("status") not in {"proposed"}:
        raise ValueError(f"提案状态为 {proposal.get('status')}，不可拒绝")
    proposal.update({
        "status": "rejected",
        "reviewed_at": _now(),
        "reviewed_by": by,
        "review_note": note,
    })
    _write_json(path, proposal)
    _ledger_event(
        "proposal_rejected",
        payload={
            "proposal_id": proposal_id,
            "kind": proposal.get("kind"),
            "note": note,
        },
        entity_type="proposal",
        entity_id=proposal_id,
        actor=by,
    )
    return proposal


def approve_proposal(proposal_id: str, note: str = "", by: str = "user") -> dict:
    """Approve a proposal; regression cases must pass an isolated replay first."""
    path = _paths()["proposals"] / f"{proposal_id}.json"
    proposal = _read_json(path)
    if not proposal:
        raise ValueError(f"提案不存在：{proposal_id}")
    if proposal.get("status") != "proposed":
        raise ValueError(f"提案状态为 {proposal.get('status')}，不可批准")

    if proposal["kind"] == "failure_review":
        proposal.update({
            "status": "acknowledged",
            "reviewed_at": _now(),
            "reviewed_by": by,
            "review_note": note,
        })
        _write_json(path, proposal)
        _ledger_event(
            "failure_review_acknowledged",
            payload={"proposal_id": proposal_id, "note": note},
            entity_type="proposal",
            entity_id=proposal_id,
            actor=by,
        )
        return proposal

    if proposal["kind"] != "regression_case":
        raise ValueError(f"未知提案类型：{proposal['kind']}")

    paths = _paths()
    staging, case_meta = _build_case_staging(proposal)
    case_id = case_meta["case_id"]
    final = paths["cases"] / case_id
    validation = _run_case(case_meta, staging, with_dwg=True)
    if not validation.get("passed"):
        quarantine = paths["quarantine"] / proposal_id
        if quarantine.exists():
            shutil.rmtree(quarantine, ignore_errors=True)
        staging.rename(quarantine)
        proposal.update({
            "status": "blocked",
            "reviewed_at": _now(),
            "reviewed_by": by,
            "review_note": note,
            "validation": validation,
            "quarantine": str(quarantine),
        })
        _write_json(path, proposal)
        _ledger_event(
            "proposal_blocked",
            payload={
                "proposal_id": proposal_id,
                "quarantine": str(quarantine),
                "error": validation.get("error"),
            },
            entity_type="proposal",
            entity_id=proposal_id,
            actor=by,
        )
        return proposal

    if final.exists():
        shutil.rmtree(staging, ignore_errors=True)
        raise ValueError(f"回归用例已存在：{case_id}")
    staging.rename(final)
    proposal.update({
        "status": "promoted",
        "reviewed_at": _now(),
        "reviewed_by": by,
        "review_note": note,
        "case_id": case_id,
        "validation": validation,
    })
    _write_json(path, proposal)
    _ledger_event(
        "case_promoted",
        payload={
            "proposal_id": proposal_id,
            "case_id": case_id,
            "task_type": proposal.get("task_type"),
            "fingerprint": proposal.get("fingerprint"),
            "integrity": case_meta.get("integrity"),
        },
        entity_type="case",
        entity_id=case_id,
        actor=by,
    )
    return proposal


# ---------------------------------------------------------------- replay
def _prepare_case_source(case_dir: Path, work: Path, meta: dict) -> tuple[Path, dict]:
    source = case_dir / meta["source"]["path"]
    if not source.is_file():
        raise FileNotFoundError(f"回归用例源文件缺失：{source}")
    clean = work / "clean.dxf"
    if source.suffix.lower() == ".dwg":
        raw = work / "source.dxf"
        conversion = dwg_bridge.dwg_to_dxf(source, raw)
        if not conversion.get("ok"):
            raise RuntimeError(f"DWG→DXF 失败：{conversion}")
        if conversion.get("needs_cleaning"):
            cleaning = dxf_cleaner.clean_dxf(raw, clean)
        else:
            shutil.copy2(raw, clean)
            cleaning = None
    else:
        raw = work / "source.dxf"
        shutil.copy2(source, raw)
        try:
            ezdxf.readfile(str(raw))
            shutil.copy2(raw, clean)
            conversion = {"ok": True, "converter": "direct-dxf"}
            cleaning = None
        except Exception:
            cleaning = dxf_cleaner.clean_dxf(raw, clean)
            conversion = {"ok": True, "converter": "direct-dxf-cleaned"}
    return clean, {
        "conversion": conversion,
        "cleaning": cleaning,
    }


def _numeric_annotations(doc) -> list[str]:
    rows = []
    for row in real_mm_engine.measure_all(doc):
        if _numeric(row.get("old")):
            rows.append(str(row["old"]))
    return sorted(rows)


def _watermark_count(doc) -> int:
    count = 0
    for layout in doc.layouts:
        for entity in layout:
            layer = str(entity.dxf.get("layer", "")).lower()
            text = ""
            try:
                if entity.dxftype() == "TEXT":
                    text = entity.dxf.text
                elif entity.dxftype() == "MTEXT":
                    text = entity.text
            except Exception:  # noqa: BLE001
                pass
            if "watermark" in layer or "evaluation only" in text.lower():
                count += 1
    return count


def _dwg_roundtrip(dxf: Path, dwg: Path) -> dict:
    export = dwg_export.export(dxf, dwg)
    if not export.get("ok"):
        return {"passed": False, "export": export, "roundtrip": None}
    roundtrip = dwg_export.roundtrip_check(dwg, dxf)
    return {
        "passed": bool(export.get("ok") and roundtrip.get("ok")),
        "export": export,
        "roundtrip": roundtrip,
    }


def _run_real_mm_case(clean: Path, work: Path, meta: dict, with_dwg: bool) -> dict:
    intent = meta.get("intent") or {}
    output = work / "result.dxf"
    report = real_mm_engine.set_real_mm(
        clean,
        output,
        length_value=float(intent.get("length_value")),
        length_anchor=str(intent.get("length_anchor")),
        height_value=float(intent.get("height_value")),
        height_anchor=str(intent.get("height_anchor")),
        align_anchor=str(intent.get("align_anchor") or "auto"),
    )
    doc = ezdxf.readfile(str(output))
    views = real_mm_engine.part_view_heights(doc)
    left, right = views.get("left") or {}, views.get("right") or {}
    left_height = float(left.get("height") or 0.0)
    right_height = float(right.get("height") or 0.0)
    target_height = float(intent.get("height_value") or 0.0)
    annotations = _numeric_annotations(doc)
    expected_annotations = (meta.get("expected") or {}).get("annotation_texts") or []
    checks = {
        "audit": {
            "passed": report["audit"]["final"]["errors"] == 0,
            **report["audit"]["final"],
        },
        "units": {
            "passed": doc.header.get("$INSUNITS") == 4
            and doc.header.get("$MEASUREMENT") == 1,
            "INSUNITS": doc.header.get("$INSUNITS"),
            "MEASUREMENT": doc.header.get("$MEASUREMENT"),
        },
        "view_height": {
            "passed": (
                abs(left_height - right_height) <= 0.05
                and abs(left_height - target_height) <= 0.05
                and abs(float(left.get("y0") or 0.0) - float(right.get("y0") or 0.0))
                <= 0.05
            ),
            "left_height": round(left_height, 6),
            "right_height": round(right_height, 6),
            "left_bottom": left.get("y0"),
            "right_bottom": right.get("y0"),
        },
        "annotations": {
            "passed": annotations == sorted(expected_annotations),
            "after": annotations,
            "expected": sorted(expected_annotations),
        },
        "watermark_free": {"passed": _watermark_count(doc) == 0},
    }
    result = {
        "passed": all(check["passed"] for check in checks.values()),
        "checks": checks,
        "details": {
            "kx": report["calibration"]["kx"],
            "ky": report["calibration"]["ky"],
            "annotation_count": len(annotations),
        },
    }
    if with_dwg:
        result["checks"]["dwg_roundtrip"] = _dwg_roundtrip(output, work / "result.dwg")
        result["passed"] = all(check["passed"] for check in result["checks"].values())
    return result


def _run_annotation_case(clean: Path, work: Path, meta: dict, with_dwg: bool) -> dict:
    intent = meta.get("intent") or {}
    output = work / "result.dxf"
    report = annotation_engine.relabel(
        clean,
        output,
        mode=intent.get("mode") or "1to1",
        base_value=intent.get("base_value"),
        base_where=intent.get("base_where"),
        fix_vertical=bool(intent.get("fix_vertical")),
        unit=intent.get("unit"),
    )
    actual = sorted(str(change.get("new")) for change in report.get("changes") or [])
    expected = sorted((meta.get("expected") or {}).get("annotation_texts") or [])
    checks = {
        "audit": {
            "passed": report["qa"]["audit_errors"] == 0,
            "errors": report["qa"]["audit_errors"],
            "fixes": report["qa"]["audit_fixes"],
        },
        "geometry_unchanged": {
            "passed": bool(report["stat"]["geometry_identical"]),
            "geometry_entity_count": report["stat"]["geometry_entity_count"],
        },
        "entity_count": {
            "passed": bool(report["stat"]["entity_count_same"]),
            "before": report["stat"]["entity_count_before"],
            "after": report["stat"]["entity_count_after"],
        },
        "annotations": {
            "passed": actual == expected,
            "after": actual,
            "expected": expected,
        },
    }
    result = {
        "passed": all(check["passed"] for check in checks.values()),
        "checks": checks,
        "details": {
            "mode": report.get("mode"),
            "K": report.get("K"),
            "annotation_count": len(actual),
            "vertical_fixes": len(report.get("vertical_fixes") or []),
        },
    }
    if with_dwg:
        result["checks"]["dwg_roundtrip"] = _dwg_roundtrip(output, work / "result.dwg")
        result["passed"] = all(check["passed"] for check in result["checks"].values())
    return result


def _run_geometry_case(clean: Path, work: Path, meta: dict, with_dwg: bool) -> dict:
    intent = meta.get("intent") or {}
    profile_id = meta.get("profile_id") or intent.get("profile_id")
    profile = drawing_profile.get_profile(profile_id)
    if profile is None:
        return {
            "passed": False,
            "checks": {"profile": {"passed": False, "profile_id": profile_id}},
            "details": {},
        }
    targets = intent.get("targets") or {}
    enabled = set(intent.get("enabled") or targets)
    output = work / "result.dxf"
    report = geometry_modifier.apply(
        clean,
        output,
        targets=targets,
        enabled=enabled,
        profile=profile,
        source_sha256=(meta.get("source") or {}).get("sha256"),
        source_name=(meta.get("source") or {}).get("name"),
    )
    final = output.with_name(output.stem + "_最终.dxf")
    if final.is_file():
        output = final
    geometry = report
    expected = meta.get("expected") or {}
    expected_measured = expected.get("measured_after") or {}
    measured = geometry.get("measured_after") or {}
    measured_ok = True
    for key in ("H2", "D2", "shaft"):
        if key not in enabled or key not in expected_measured:
            continue
        left = expected_measured[key]
        right = measured.get(key) or {}
        field = "extend_mm" if key == "shaft" else "mm"
        if abs(float(left.get(field, 0)) - float(right.get(field, 0))) > 0.5:
            measured_ok = False
    expected_total = expected.get("total_height")
    actual_total = (geometry.get("TOTAL_HEIGHT") or {}).get("new_mm")
    checks = {
        "audit": {
            "passed": geometry.get("final_audit_errors") == 0,
            "errors": geometry.get("final_audit_errors"),
            "fixes": geometry.get("final_audit_fixes"),
        },
        "verification": geometry.get("verification") or {"passed": False},
        "side_effects": {
            "passed": not geometry.get("unexpected_modified")
            and not geometry.get("deleted_entities"),
            "unexpected_modified": geometry.get("unexpected_modified"),
            "deleted_entities": geometry.get("deleted_entities"),
        },
        "entity_count": {
            "passed": geometry.get("entities_before") == geometry.get("entities_after"),
            "before": geometry.get("entities_before"),
            "after": geometry.get("entities_after"),
        },
        "measured_targets": {"passed": measured_ok, "after": measured},
        "total_height": {
            "passed": (
                expected_total is None
                or abs(float(actual_total or 0) - float(expected_total)) <= 0.05
            ),
            "actual": actual_total,
            "expected": expected_total,
        },
    }
    result = {
        "passed": all(check["passed"] for check in checks.values()),
        "checks": checks,
        "details": {
            "profile_id": profile_id,
            "targets": targets,
            "change_count": len(geometry.get("changes") or []),
        },
    }
    if with_dwg:
        result["checks"]["dwg_roundtrip"] = _dwg_roundtrip(output, work / "result.dwg")
        result["passed"] = all(check["passed"] for check in result["checks"].values())
    return result


def _run_text_edit_case(clean: Path, work: Path, meta: dict, with_dwg: bool) -> dict:
    expected = meta.get("expected") or {}
    edits = list(expected.get("text_edits") or [])
    if not edits:
        return {
            "passed": False,
            "checks": {"text_edits": {"passed": False, "error": "缺少 text_edits"}},
            "details": {},
        }
    output = work / "result.dxf"
    report = text_edit_engine.apply_edits(clean, output, edits)
    checked = ezdxf.readfile(str(output))
    actual_texts = text_edit_engine._all_texts(checked)
    expected_map = {
        str(edit.get("handle")): str(
            edit.get("expected_text") if edit.get("expected_text") is not None else edit.get("new")
        )
        for edit in edits
        if edit.get("handle")
    }
    actual_map = {
        handle: actual_texts.get(handle)
        for handle in expected_map
    }
    text_verified = report["qa"]["all_expected"] and all(
        text_edit_engine._same_text(actual_map.get(handle, ""), text)
        for handle, text in expected_map.items()
    )
    checks = {
        "audit": {
            "passed": report["qa"]["audit_ok"],
            "errors": report["qa"]["audit_errors"],
            "fixes": report["qa"]["audit_fixes"],
        },
        "geometry_unchanged": {
            "passed": report["stat"]["geometry_identical"],
            "geometry_entity_count": report["stat"]["geometry_entity_count"],
        },
        "entity_count": {
            "passed": report["stat"]["entity_count_same"],
            "before": report["stat"]["entity_count_before"],
            "after": report["stat"]["entity_count_after"],
        },
        "text_verified": {
            "passed": text_verified,
            "expected": expected_map,
            "after": actual_map,
        },
        "untouched_text": {
            "passed": report["qa"]["only_targets_changed"],
            "targets": report["stat"]["target_handles"],
            "changed": report["stat"]["changed_text_handles"],
        },
        "dimension_guard": {
            "passed": report["qa"]["dimension_labels_only"],
        },
        "shared_block_guard": {
            "passed": report["qa"]["shared_blocks_explicit"],
        },
    }
    result = {
        "passed": all(check["passed"] for check in checks.values()),
        "checks": checks,
        "details": {
            "change_count": len(report.get("changes") or []),
            "changes": report.get("changes") or [],
        },
    }
    if with_dwg:
        result["checks"]["dwg_roundtrip"] = _dwg_roundtrip(output, work / "result.dwg")
        result["passed"] = all(check["passed"] for check in result["checks"].values())
    return result


def _run_case(meta: dict, case_dir: Path, with_dwg: bool = True) -> dict:
    started = time.perf_counter()
    integrity = _verify_case_integrity(meta, case_dir)
    if integrity["status"] == "broken":
        return {
            "case_id": meta.get("case_id"),
            "title": meta.get("title"),
            "task_type": meta.get("task_type"),
            "passed": False,
            "checks": {"baseline_integrity": integrity},
            "error": "回归基线文件哈希不匹配，已拒绝重放",
            "duration_seconds": round(time.perf_counter() - started, 3),
        }
    work = Path(tempfile.mkdtemp(prefix="replay_", dir=case_dir))
    try:
        clean, preparation = _prepare_case_source(case_dir, work, meta)
        task_type = meta.get("task_type")
        if task_type == "real_mm":
            result = _run_real_mm_case(clean, work, meta, with_dwg)
        elif task_type == "annotation":
            result = _run_annotation_case(clean, work, meta, with_dwg)
        elif task_type == "geometry":
            result = _run_geometry_case(clean, work, meta, with_dwg)
        elif task_type == "text_edit":
            result = _run_text_edit_case(clean, work, meta, with_dwg)
        else:
            result = {
                "passed": False,
                "checks": {"task_type": {"passed": False, "value": task_type}},
                "details": {},
            }
        result.update({
            "case_id": meta.get("case_id"),
            "title": meta.get("title"),
            "task_type": task_type,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "preparation": preparation,
        })
        result["checks"]["baseline_integrity"] = integrity
        result["passed"] = all(
            check.get("passed") for check in result["checks"].values()
        )
        return result
    except Exception as exc:  # noqa: BLE001
        return {
            "case_id": meta.get("case_id"),
            "title": meta.get("title"),
            "task_type": meta.get("task_type"),
            "passed": False,
            "checks": {},
            "error": f"{type(exc).__name__}: {exc}",
            "duration_seconds": round(time.perf_counter() - started, 3),
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def replay(with_dwg: bool = True, persist: bool = True) -> dict:
    """Replay all approved regression cases against the current code."""
    started = time.perf_counter()
    cases = list_cases()
    paths = _paths()
    previous = _read_json(paths["replays"] / "latest.json")
    results = []
    for case in cases:
        case_dir = Path(case.pop("_path")).parent
        results.append(_run_case(case, case_dir, with_dwg=with_dwg))
    passed = sum(1 for row in results if row.get("passed"))
    failed = len(results) - passed
    current_code = _code_fingerprint()
    current_gate = _gate_fingerprint()
    previous_gate = (previous or {}).get("gate_fingerprint")
    previous_passed = {
        row.get("case_id"): bool(row.get("passed"))
        for row in (previous or {}).get("results", [])
    }
    regressions = [
        row["case_id"]
        for row in results
        if previous_passed.get(row.get("case_id")) is True and not row.get("passed")
    ]
    fixed = [
        row["case_id"]
        for row in results
        if previous_passed.get(row.get("case_id")) is False and row.get("passed")
    ]
    summary = {
        "replay_id": f"rp_{_stamp()}",
        "ok": bool(cases) and failed == 0,
        "case_count": len(cases),
        "passed": passed,
        "failed": failed,
        "pass_rate": round(passed / len(cases), 4) if cases else 0.0,
        "with_dwg": bool(with_dwg),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "code_fingerprint": current_code,
        "previous_code_fingerprint": (previous or {}).get("code_fingerprint"),
        "code_changed_since_previous": (
            bool(previous) and (previous or {}).get("code_fingerprint") != current_code
        ),
        "gate_fingerprint": current_gate,
        "previous_gate_fingerprint": previous_gate,
        "gate_changed_since_previous": bool(previous) and previous_gate != current_gate,
        "regressions": regressions,
        "fixed": fixed,
        "results": results,
        "generated_at": _now(),
    }
    if persist:
        _write_json(paths["replays"] / f"{summary['replay_id']}.json", summary)
        _write_json(paths["replays"] / "latest.json", summary)
    _ledger_event(
        "replay",
        payload={
            "replay_id": summary["replay_id"],
            "ok": summary["ok"],
            "case_count": summary["case_count"],
            "passed": summary["passed"],
            "failed": summary["failed"],
            "with_dwg": summary["with_dwg"],
            "code_fingerprint": summary["code_fingerprint"],
            "gate_fingerprint": summary["gate_fingerprint"],
            "regressions": summary["regressions"],
            "cases": [
                {
                    "case_id": row.get("case_id"),
                    "passed": row.get("passed"),
                    "error": row.get("error"),
                }
                for row in results
            ],
        },
        entity_type="replay",
        entity_id=summary["replay_id"],
    )
    return summary


def latest_replay() -> dict | None:
    return _read_json(_paths()["replays"] / "latest.json")


def _next_action(
    cases: list[dict],
    replay: dict | None,
    replay_stale: bool,
    pending_count: int,
    failure_patterns: list[dict],
    integrity_counts: Counter,
) -> dict:
    if integrity_counts.get("broken"):
        return {
            "code": "baseline_broken",
            "severity": "error",
            "label": "发现基线文件哈希异常，已禁止重放；请重新执行并批准新基线",
        }
    if integrity_counts.get("unsealed"):
        return {
            "code": "seal_baseline",
            "severity": "warning",
            "label": f"有 {integrity_counts['unsealed']} 条基线未封存；执行“封存基线”后才能交付",
        }
    if not cases:
        return {
            "code": "build_baseline",
            "severity": "warning",
            "label": (
                f"已有 {pending_count} 个候选待审，批准并重放后才能建立基线"
                if pending_count
                else "先执行并验收一个任务，再批准回归候选建立基线"
            ),
        }
    if replay_stale:
        return {
            "code": "replay",
            "severity": "warning",
            "label": "代码、门禁、配置或基线集合已变化；请重放全部回归",
        }
    if replay and not replay.get("ok"):
        return {
            "code": "fix_regression",
            "severity": "error",
            "label": "回归失败；先定位失败检查，修复后重新全量重放",
        }
    if pending_count:
        return {
            "code": "review_proposals",
            "severity": "info",
            "label": f"基线有效；还有 {pending_count} 个候选提案待人工审核",
        }
    if failure_patterns:
        return {
            "code": "review_failures",
            "severity": "info",
            "label": f"基线有效；另有 {len(failure_patterns)} 个失败模式待人工复核",
        }
    return {
        "code": "ready",
        "severity": "ok",
        "label": "基线有效，可以继续执行新任务或交付",
    }


def status() -> dict:
    paths = _paths()
    episodes = list_episodes()
    proposals = list_proposals()
    all_cases = list_cases_with_integrity(include_invalidated=True)
    cases = [
        row for row in all_cases if (row.get("status") or "active") == "active"
    ]
    invalidated_cases = [
        row for row in all_cases if (row.get("status") or "active") == "invalidated"
    ]
    replay = latest_replay()
    diagnosis = diagnose(episodes)
    proposal_counts = Counter(row.get("status") for row in proposals)
    current_code = _code_fingerprint()
    current_gate = _gate_fingerprint()
    integrity_counts = Counter(row["_integrity"]["status"] for row in cases)
    integrity_ok = bool(cases) and integrity_counts.get("sealed", 0) == len(cases)
    active_case_ids = {row.get("case_id") for row in cases}
    replay_case_ids = {
        row.get("case_id") for row in (replay or {}).get("results", [])
    }
    replay_stale = bool(
        cases
        and (
            not replay
            or replay.get("code_fingerprint") != current_code
            or replay.get("gate_fingerprint") != current_gate
            or replay_case_ids != active_case_ids
            or not integrity_ok
        )
    )
    readiness = bool(
        cases
        and integrity_ok
        and replay
        and replay.get("ok")
        and replay.get("code_fingerprint") == current_code
        and replay.get("gate_fingerprint") == current_gate
        and replay_case_ids == active_case_ids
    )
    pending_count = len(list_proposals("proposed"))
    next_action = _next_action(
        cases,
        replay,
        replay_stale,
        pending_count,
        diagnosis["failure_patterns"],
        integrity_counts,
    )
    return {
        "ok": True,
        "learning_root": str(paths["root"]),
        "episodes": len(episodes),
        "outcomes": diagnosis["outcomes"],
        "feedback": diagnosis["feedback"],
        "cases": len(cases),
        "case_types": dict(Counter(row.get("task_type") for row in cases)),
        "invalidated_cases": len(invalidated_cases),
        "case_integrity": dict(integrity_counts),
        "proposals": dict(proposal_counts),
        "pending_proposals": pending_count,
        "failure_patterns": diagnosis["failure_patterns"],
        "last_replay": (
            {
                key: replay.get(key)
                for key in (
                    "replay_id",
                    "ok",
                    "case_count",
                    "passed",
                    "failed",
                    "pass_rate",
                    "duration_seconds",
                    "code_fingerprint",
                    "gate_fingerprint",
                    "regressions",
                    "generated_at",
                )
            }
            | {
                "results": [
                    {
                        key: row.get(key)
                        for key in (
                            "case_id",
                            "title",
                            "task_type",
                            "passed",
                            "duration_seconds",
                            "error",
                        )
                    }
                    for row in replay.get("results", [])
                ]
            }
            if replay
            else None
        ),
        "code_fingerprint": current_code,
        "gate_fingerprint": current_gate,
        "replay_stale": replay_stale,
        "baseline_ready": readiness,
        "next_action": next_action,
        "last_cycle": _read_json(paths["state"]).get("last_cycle")
        if _read_json(paths["state"])
        else None,
        "principles": [
            "不自动改代码，不自动降低硬门槛",
            "通过验收的成功结果先成为回归候选",
            "候选必须在隔离目录重放通过，才沉淀为基线",
            "代码、门禁或配置指纹变化后，上一次回归结论自动失效并要求重放",
            "用户验收返工或拒绝时，对应任务版本的旧基线立即失效，必须重新验证",
        ],
        "generated_at": _now(),
    }


def cycle(with_dwg: bool = True) -> dict:
    """Observe, diagnose, propose, and replay the current learned baseline."""
    with _lock:
        observed = observe()
        episodes = list_episodes()
        diagnosis = diagnose(episodes)
        proposals = propose()
        cases = list_cases()
        replay_result = replay(with_dwg=with_dwg) if cases else None
        summary = {
            "ok": replay_result is None or replay_result.get("ok", False),
            "observed": observed,
            "diagnosis": {
                "outcomes": diagnosis["outcomes"],
                "failure_patterns": diagnosis["failure_patterns"],
            },
            "proposals_created": proposals["created"],
            "pending_proposals": proposals["pending"],
            "case_count": len(cases),
            "replay": (
                {
                    key: replay_result.get(key)
                    for key in (
                        "replay_id",
                        "ok",
                        "case_count",
                        "passed",
                        "failed",
                        "pass_rate",
                        "duration_seconds",
                        "gate_fingerprint",
                        "regressions",
                    )
                }
                if replay_result
                else None
            ),
            "generated_at": _now(),
        }
        state = _read_json(_paths()["state"], {}) or {}
        state["last_cycle"] = summary
        state["last_cycle_at"] = summary["generated_at"]
        _write_json(_paths()["state"], state)
        _ledger_event(
            "cycle",
            payload=summary,
            entity_type="cycle",
            entity_id=summary["generated_at"],
        )
        return summary


def _ledger_snapshot() -> dict:
    all_cases = list_cases_with_integrity(include_invalidated=True)
    return {
        "code_fingerprint": _code_fingerprint(),
        "gate_fingerprint": _gate_fingerprint(),
        "episodes": [
            {
                "episode_id": row.get("episode_id"),
                "task_id": row.get("task_id"),
                "task_type": row.get("task_type"),
                "outcome": row.get("outcome"),
                "fingerprint": row.get("fingerprint"),
            }
            for row in list_episodes()
        ],
        "proposals": [
            {
                "proposal_id": row.get("proposal_id"),
                "kind": row.get("kind"),
                "status": row.get("status"),
                "case_id": row.get("case_id"),
            }
            for row in list_proposals()
        ],
        "cases": [
            {
                "case_id": row.get("case_id"),
                "task_type": row.get("task_type"),
                "status": row.get("status") or "active",
                "fingerprint": row.get("fingerprint"),
                "integrity": (row.get("_integrity") or {}).get("status"),
            }
            for row in all_cases
        ],
    }


def _ensure_ledger_seeded() -> bool:
    root = _paths()["root"]
    existing = learning_ledger.list_events(
        root, limit=1, event_type="baseline_snapshot"
    )
    if existing:
        return False
    event = _ledger_event(
        "baseline_snapshot",
        payload=_ledger_snapshot(),
        entity_type="ledger",
        actor="system",
    )
    return bool(event.get("ok"))


def audit(limit: int = 50) -> dict:
    """Return the hash-chain audit trail and its integrity result."""
    seeded = _ensure_ledger_seeded()
    root = _paths()["root"]
    ledger_status = learning_ledger.status(root)
    events = learning_ledger.list_events(root, limit=limit)
    errors = _read_jsonl(root / "ledger_errors.jsonl")
    return {
        "ok": bool(ledger_status.get("ok") and not errors),
        "seeded": seeded,
        "ledger": ledger_status,
        "events": events,
        "error_count": len(errors),
        "recent_errors": errors[-5:],
        "generated_at": _now(),
    }

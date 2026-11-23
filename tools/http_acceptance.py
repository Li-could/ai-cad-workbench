"""End-to-end acceptance through the real HTTP API.

Start the workbench first:

    python app.py

Then run:

    python tools/http_acceptance.py

The script uploads original DWGs, creates and approves a task without a
reference image, executes it, downloads DXF/DWG deliverables, and independently
reopens the delivered DWG before measuring the three target dimensions. It also
verifies that incomplete instructions and uncalibrated drawings are rejected
with HTTP 409 instead of producing guessed geometry.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import runtime

runtime.bootstrap()

import drawing_measure
import drawing_profile
import dwg_bridge
import dwg_export
import ezdxf


class AcceptanceError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_response(response: requests.Response, *, expected: int = 200) -> dict:
    if response.status_code != expected:
        raise AcceptanceError(
            f"{response.request.method} {response.request.url} -> "
            f"{response.status_code}: {response.text[:800]}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise AcceptanceError(
            f"{response.request.url} 未返回 JSON：{response.text[:300]}"
        ) from exc


def _upload(session: requests.Session, base: str, path: Path) -> dict:
    with path.open("rb") as stream:
        response = session.post(
            f"{base}/api/files",
            files={"file": (path.name, stream, "application/octet-stream")},
            timeout=180,
        )
    payload = _json_response(response)
    if not payload.get("ok") or not payload.get("file", {}).get("file_id"):
        raise AcceptanceError(f"上传失败：{payload}")
    return payload["file"]


def _create_task(
    session: requests.Session,
    base: str,
    *,
    file_id: str,
    instruction: str,
    targets: dict[str, float] | None = None,
) -> dict:
    body: dict[str, Any] = {
        "file_id": file_id,
        "instruction": instruction,
    }
    if targets:
        body["targets_override"] = targets
    response = session.post(f"{base}/api/tasks", json=body, timeout=300)
    payload = _json_response(response)
    if not payload.get("ok") or not payload.get("task", {}).get("task_id"):
        raise AcceptanceError(f"建任务失败：{payload}")
    return payload["task"]


def _download(
    session: requests.Session, base: str, task_id: str, kind: str, target: Path
) -> Path:
    response = session.get(
        f"{base}/api/tasks/{task_id}/download",
        params={"kind": kind},
        timeout=300,
    )
    if response.status_code != 200:
        raise AcceptanceError(
            f"下载 {kind} 失败：{response.status_code} {response.text[:500]}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(response.content)
    if target.stat().st_size <= 0:
        raise AcceptanceError(f"下载 {kind} 得到空文件")
    return target


def _hard_checks(report: dict) -> dict[str, bool]:
    checks = report.get("checks") or {}
    return {
        name: bool(check.get("passed"))
        for name, check in checks.items()
        if isinstance(check, dict)
    }


def _verify_td50_delivery(
    *,
    task_id: str,
    report: dict,
    result_dxf: Path,
    result_dwg: Path,
    work: Path,
) -> dict:
    if report.get("ok") is not True:
        raise AcceptanceError(f"任务 {task_id} 报告未通过：{report.get('checks')}")
    hard = _hard_checks(report)
    failed = [name for name, passed in hard.items() if not passed]
    if failed:
        raise AcceptanceError(f"任务 {task_id} 硬检查失败：{failed}")

    probe = dwg_bridge.probe_dwg(result_dwg)
    if probe.get("version_code") != "AC1032":
        raise AcceptanceError(f"交付 DWG 版本不是 AC1032：{probe}")
    if not probe.get("is_dwg") or not probe.get("openable"):
        raise AcceptanceError(f"交付 DWG 不可打开：{probe}")

    roundtrip = dwg_export.roundtrip_check(result_dwg, result_dxf)
    if not roundtrip.get("ok"):
        raise AcceptanceError(f"DWG 往返校验失败：{roundtrip}")

    back_dxf = work / "delivered_roundtrip.dxf"
    conversion = dwg_bridge.dwg_to_dxf(result_dwg, back_dxf)
    if not conversion.get("ok"):
        raise AcceptanceError(f"交付 DWG 回读失败：{conversion}")

    delivered_doc = ezdxf.readfile(str(back_dxf))
    reference_doc = ezdxf.readfile(str(result_dxf))
    delivered_handles = {entity.dxf.handle for entity in delivered_doc.modelspace()}
    reference_handles = {entity.dxf.handle for entity in reference_doc.modelspace()}
    if delivered_handles != reference_handles:
        raise AcceptanceError(
            "交付 DWG 与 result.dxf 的 handle 集合不一致："
            f"缺失 {sorted(reference_handles - delivered_handles)[:10]}，"
            f"新增 {sorted(delivered_handles - reference_handles)[:10]}"
        )

    audit = delivered_doc.audit()
    if audit.errors:
        raise AcceptanceError(f"交付 DWG 回读 audit 错误：{len(audit.errors)}")

    layer_names = {layer.dxf.name for layer in delivered_doc.layers}
    colored = sorted(name for name in layer_names if name.startswith("变更_"))
    if colored:
        raise AcceptanceError(f"交付 DWG 含彩色标注图层：{colored}")

    profile = drawing_profile.get_profile("td50-40g")
    if profile is None:
        raise AcceptanceError("本机缺少 td50-40g 图纸档案")
    measured = drawing_measure.measure(delivered_doc, profile)
    verdict = drawing_measure.verify(
        measured,
        {"H2": 443.0, "D2": 294.0, "shaft": 20.0},
        {"H2", "D2", "shaft"},
        tolerance_mm=0.5,
    )
    if not verdict.get("passed"):
        raise AcceptanceError(f"交付 DWG 独立复测失败：{verdict}")

    return {
        "task_id": task_id,
        "report_ok": report.get("ok"),
        "hard_checks": hard,
        "dwg_probe": {
            "version_code": probe.get("version_code"),
            "size": probe.get("size"),
            "openable": probe.get("openable"),
            "converter": probe.get("converter"),
        },
        "roundtrip": roundtrip,
        "entity_count": len(delivered_handles),
        "audit_errors": len(audit.errors),
        "audit_fixes": len(audit.fixes),
        "colored_layers": colored,
        "measured": measured,
        "verification": verdict,
    }


def _verify_needs_calibration(
    session: requests.Session, base: str, drawing: Path
) -> dict:
    uploaded = _upload(session, base, drawing)
    task = _create_task(
        session,
        base,
        file_id=uploaded["file_id"],
        instruction="风罩高度 443、接线盒伸出 294、轴伸加 20",
        targets={"H2": 443.0, "D2": 294.0, "shaft": 20.0},
    )
    task_id = task["task_id"]
    if task.get("status") != "needs_calibration":
        raise AcceptanceError(
            f"未定标图纸 {drawing.name} 未被拒绝：status={task.get('status')}"
        )
    response = session.post(f"{base}/api/tasks/{task_id}/execute", timeout=120)
    if response.status_code != 409:
        raise AcceptanceError(
            f"未定标任务 execute 期望 409，实际 {response.status_code}："
            f"{response.text[:500]}"
        )
    return {
        "task_id": task_id,
        "source": drawing.name,
        "reference": task.get("files", {}).get("reference"),
        "status": task.get("status"),
        "profile_match": task.get("profile_match"),
        "execute_status": response.status_code,
        "execute_detail": response.json().get("detail"),
    }


def _verify_missing_instruction(
    session: requests.Session, base: str, drawing: Path
) -> dict:
    uploaded = _upload(session, base, drawing)
    task = _create_task(
        session,
        base,
        file_id=uploaded["file_id"],
        instruction="按参考图改图",
    )
    task_id = task["task_id"]
    if task.get("files", {}).get("reference") is not None:
        raise AcceptanceError("模糊需求任务不应凭空绑定参考图")
    if task.get("status") != "needs_instruction":
        raise AcceptanceError(
            f"缺信息需求未被拦截：status={task.get('status')} "
            f"instruction={task.get('instruction')}"
        )
    if (task.get("plan") or {}).get("known_rules") is not False:
        raise AcceptanceError(f"缺信息任务不应生成可执行计划：{task.get('plan')}")

    approve = session.post(
        f"{base}/api/tasks/{task_id}/approve", json={}, timeout=30
    )
    if approve.status_code != 400:
        raise AcceptanceError(
            f"缺信息任务 approve 期望 400，实际 {approve.status_code}："
            f"{approve.text[:500]}"
        )
    execute = session.post(f"{base}/api/tasks/{task_id}/execute", timeout=120)
    if execute.status_code != 409:
        raise AcceptanceError(
            f"缺信息任务 execute 期望 409，实际 {execute.status_code}："
            f"{execute.text[:500]}"
        )
    return {
        "task_id": task_id,
        "instruction": task.get("instruction"),
        "reference": task.get("files", {}).get("reference"),
        "status": task.get("status"),
        "known_rules": (task.get("plan") or {}).get("known_rules"),
        "questions": ((task.get("plan") or {}).get("instruction_required") or {})
        .get("questions", []),
        "approve_status": approve.status_code,
        "execute_status": execute.status_code,
        "execute_detail": execute.json().get("detail"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8900")
    parser.add_argument("--td50", default=str(ROOT / "input" / "TD50-40G.dwg"))
    parser.add_argument(
        "--uncalibrated", default=str(ROOT / "input" / "25QYL-2SL.dwg")
    )
    parser.add_argument(
        "--evidence",
        default=str(ROOT / "output" / "http_acceptance.json"),
    )
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    td50 = Path(args.td50).resolve()
    uncalibrated = Path(args.uncalibrated).resolve()
    for path in (td50, uncalibrated):
        if not path.exists():
            raise AcceptanceError(f"源文件不存在：{path}")

    session = requests.Session()
    started = time.perf_counter()
    health = _json_response(session.get(f"{base}/api/health", timeout=10))
    if not health.get("ok"):
        raise AcceptanceError(f"服务健康检查失败：{health}")

    source_sha = _sha256(td50)
    uploaded = _upload(session, base, td50)
    if uploaded.get("sha256") != source_sha:
        raise AcceptanceError(
            f"上传 SHA256 不一致：{uploaded.get('sha256')} != {source_sha}"
        )
    task = _create_task(
        session,
        base,
        file_id=uploaded["file_id"],
        instruction="风罩高度443，接线盒伸出294，轴伸加20",
    )
    task_id = task["task_id"]
    if task.get("files", {}).get("reference") is not None:
        raise AcceptanceError("无参考图任务不应出现 reference 文件")
    if task.get("status") != "planned":
        raise AcceptanceError(
            f"TD50 任务未进入 planned：status={task.get('status')} "
            f"profile={task.get('profile_id')}"
        )
    if task.get("profile_id") != "td50-40g":
        raise AcceptanceError(f"TD50 档案匹配错误：{task.get('profile_id')}")
    enabled = set((task.get("intent") or {}).get("enabled") or [])
    if not {"H2", "D2", "shaft"}.issubset(enabled):
        raise AcceptanceError(f"TD50 意图未启用三条规则：{sorted(enabled)}")

    approved = _json_response(
        session.post(
            f"{base}/api/tasks/{task_id}/approve",
            json={"note": "HTTP acceptance"},
            timeout=30,
        )
    )
    if approved.get("task", {}).get("status") != "approved":
        raise AcceptanceError(f"任务确认失败：{approved}")

    executed = _json_response(
        session.post(f"{base}/api/tasks/{task_id}/execute", timeout=600)
    )
    if executed.get("task", {}).get("status") != "executed":
        raise AcceptanceError(
            f"任务执行后状态不是 executed：{executed.get('task', {}).get('status')}"
        )
    report = executed.get("report") or {}

    with tempfile.TemporaryDirectory(prefix="http_accept_") as tmp:
        work = Path(tmp)
        result_dxf = _download(session, base, task_id, "dxf", work / "result.dxf")
        result_dwg = _download(session, base, task_id, "dwg", work / "result.dwg")
        report_file = _download(
            session, base, task_id, "report", work / "report.json"
        )
        downloaded_report = json.loads(report_file.read_text(encoding="utf-8"))
        if downloaded_report != report:
            raise AcceptanceError("下载的 report.json 与 execute 响应不一致")

        preview = _json_response(
            session.get(f"{base}/api/tasks/{task_id}/preview", timeout=30)
        )
        preview_files = preview.get("files") or {}
        for key in ("before_png", "after_png"):
            if key not in preview_files:
                raise AcceptanceError(f"preview 缺少 {key}：{preview}")
        image_response = session.get(
            f"{base}{preview_files['after_png']}", timeout=60
        )
        if image_response.status_code != 200:
            raise AcceptanceError(
                f"对照图下载失败：{image_response.status_code}"
            )
        if not image_response.content.startswith(b"\x89PNG"):
            raise AcceptanceError("对照图不是有效 PNG")

        td50_evidence = _verify_td50_delivery(
            task_id=task_id,
            report=report,
            result_dxf=result_dxf,
            result_dwg=result_dwg,
            work=work,
        )

    uncalibrated_evidence = _verify_needs_calibration(
        session, base, uncalibrated
    )
    missing_instruction_evidence = _verify_missing_instruction(
        session, base, td50
    )
    duration = round(time.perf_counter() - started, 3)
    evidence = {
        "ok": True,
        "base_url": base,
        "source": {
            "name": td50.name,
            "sha256": source_sha,
            "size": td50.stat().st_size,
        },
        "input": {
            "instruction": task.get("instruction"),
            "reference_image": task.get("files", {}).get("reference"),
            "reference_required": False,
        },
        "td50": td50_evidence,
        "uncalibrated": uncalibrated_evidence,
        "missing_instruction": missing_instruction_evidence,
        "duration_seconds": duration,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    evidence_path = Path(args.evidence).resolve()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(f"\nPASS: real HTTP workflow verified in {duration:.1f}s")
    print(f"Evidence: {evidence_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AcceptanceError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)

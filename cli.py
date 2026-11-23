"""engineering-drawing-ai 命令行入口。

常用：
    python cli.py check
    python cli.py profiles list
    python cli.py calibrate input/25QYL-2SL.dwg --id 25qyl-2sl --title "25QYL 安装图"
    python cli.py run input/TD50-40G.dwg "H2=443, D2=294, 轴伸加20" --out output/td50
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import runtime


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _slug(text: str) -> str:
    value = "".join(ch.lower() if ch.isalnum() else "-" for ch in text).strip("-")
    while "--" in value:
        value = value.replace("--", "-")
    return value or "drawing"


def _parse_targets(text: str | None) -> dict[str, float]:
    targets: dict[str, float] = {}
    if not text:
        return targets
    for item in text.replace("；", ",").replace("，", ",").split(","):
        if not item.strip():
            continue
        key, sep, value = item.partition("=")
        if not sep:
            key, sep, value = item.partition(":")
        if not sep:
            raise SystemExit(f"目标格式错误：{item!r}，应为 H2=443,D2=294,shaft=20")
        key = key.strip()
        if key not in ("H2", "D2", "shaft"):
            raise SystemExit(f"未知目标：{key}")
        targets[key] = float(value.strip())
    return targets


def cmd_check(_args) -> int:
    runtime.bootstrap()
    from dwg_bridge import sdk_info
    import drawing_profile

    diagnostics = runtime.diagnostics()
    profiles = drawing_profile.profiles()
    sdk = sdk_info()
    report = {
        "ok": bool(
            diagnostics["versions"].get("ezdxf")
            and diagnostics["versions"].get("numpy")
            and sdk.get("available")
            and diagnostics.get("writable")
        ),
        "dependency_source": diagnostics.get("dependency_source"),
        "python": diagnostics.get("python"),
        "versions": diagnostics.get("versions"),
        "paths": diagnostics.get("paths"),
        "dwg_converter": sdk.get("oda") or sdk.get("aspose"),
        "oda": sdk.get("oda"),
        "aspose": sdk.get("aspose"),
        "llm_configured": diagnostics.get("llm_configured"),
        "profiles": [profile.profile_id for profile in profiles],
        "writable": diagnostics.get("writable"),
    }
    print(_json(report))
    if not sdk.get("oda"):
        print("警告：未找到 ODA File Converter，DWG 写回不可用。", file=sys.stderr)
    if not profiles:
        print("提示：几何改图需要先安装对应图纸的定标档案。", file=sys.stderr)
    if not diagnostics.get("llm_configured"):
        print("提示：未配置 DASHSCOPE_API_KEY，将使用关键词兜底解析。", file=sys.stderr)
    return 0 if report["ok"] else 1


def cmd_profiles(args) -> int:
    runtime.bootstrap()
    import drawing_profile

    if args.action == "list":
        items = []
        for profile in drawing_profile.profiles():
            items.append({
                "profile_id": profile.profile_id,
                "model": profile.model,
                "title": profile.title,
                "entity_count": profile.data.get("fingerprint", {}).get("entity_count"),
                "source_file": profile.data.get("source_file"),
            })
        print(_json(items))
        return 0
    if args.action == "show":
        profile = drawing_profile.get_profile(args.profile_id)
        if profile is None:
            print(f"档案不存在：{args.profile_id}", file=sys.stderr)
            return 2
        print(_json(profile.data))
        return 0
    if args.action == "install":
        from calibration import readiness_problems
        import drawing_profile

        path = Path(args.file)
        if not path.exists():
            print(f"文件不存在：{path}", file=sys.stderr)
            return 2
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"JSON 无法解析：{exc}", file=sys.stderr)
            return 2
        problems = readiness_problems(data)
        if problems and not args.force:
            print("档案未达到可安装状态：", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            print("确认已人工复核后可用 --force 强制安装。", file=sys.stderr)
            return 2
        try:
            drawing_profile.validate_profile(data)
        except Exception as exc:  # noqa: BLE001
            print(f"档案结构校验失败：{exc}", file=sys.stderr)
            return 2
        target = drawing_profile.PROFILE_DIR / f"{data['profile_id']}.profile.json"
        if target.exists() and not args.replace:
            print(f"档案已存在：{target}；如需覆盖请加 --replace", file=sys.stderr)
            return 2
        saved = drawing_profile.save_profile(data, target)
        print(f"已安装：{saved}")
        return 0
    print(f"未知操作：{args.action}", file=sys.stderr)
    return 2


def cmd_calibrate(args) -> int:
    runtime.bootstrap()
    import calibration
    import drawing_profile

    source = Path(args.dwg)
    if not source.exists():
        print(f"文件不存在：{source}", file=sys.stderr)
        return 2
    profile_id = args.id or _slug(source.stem)
    work_dir = Path(args.workdir) if args.workdir else ROOT / "output" / "_calibration"
    work_dir.mkdir(parents=True, exist_ok=True)

    if source.suffix.lower() == ".dxf":
        dxf_path = source
        sha256 = calibration._sha256(source)
    else:
        from dwg_bridge import dwg_to_dxf

        dxf_path = work_dir / f"{profile_id}.dxf"
        converted = dwg_to_dxf(source, dxf_path)
        if not converted.get("ok"):
            print(f"DWG 转换失败：{converted.get('error')}", file=sys.stderr)
            return 2
        sha256 = calibration._sha256(source)

    import ezdxf

    doc = ezdxf.readfile(str(dxf_path))
    scan = calibration.scan_drawing(doc)
    skeleton = calibration.build_skeleton(
        doc,
        profile_id=profile_id,
        model=args.model or profile_id.upper(),
        title=args.title or f"{profile_id} 图纸档案（待定标）",
        source_file=source.name,
        source_sha256=sha256,
        scan=scan,
    )
    out = Path(args.out) if args.out else drawing_profile.PROFILE_DIR / f"{profile_id}.draft.json"
    calibration.dump_json(skeleton, out)
    scan_out = out.with_name(out.name.replace(".draft.json", ".scan.json"))
    calibration.dump_json(scan, scan_out)

    print(_json({
        "ok": True,
        "draft": str(out),
        "scan": str(scan_out),
        "dxf": str(dxf_path),
        "entity_count": scan["entity_count"],
        "bbox": scan["bbox"],
        "text_candidates": len(scan["text_candidates"]),
        "block_text_candidates": len(scan.get("block_text_candidates", [])),
        "dimensions": len(scan["dimensions"]),
        "longest_lines": len(scan["longest_lines"]),
        "largest_circles": len(scan["largest_circles"]),
        "next": "人工填写 draft 后：python cli.py profiles install " + str(out),
    }))
    return 0


def cmd_measure(args) -> int:
    runtime.bootstrap()
    import drawing_measure
    import drawing_profile
    import ezdxf

    path = Path(args.dxf)
    if not path.exists():
        print(f"文件不存在：{path}", file=sys.stderr)
        return 2
    doc = ezdxf.readfile(str(path))
    profile = (drawing_profile.get_profile(args.profile)
               if args.profile else None)
    if profile is None:
        profile, report = drawing_profile.resolve_for(doc, source_name=path.name)
        if profile is None:
            print(_json({"ok": False, "problems": report.get("problems"),
                         "candidates": report.get("candidates")}))
            return 2
    measured = drawing_measure.measure(doc, profile)
    print(_json({
        "ok": True,
        "profile_id": profile.profile_id,
        "file": str(path),
        "measured": {key: measured[key] for key in ("H2", "D2", "shaft")},
    }))
    return 0


def cmd_run(args) -> int:
    runtime.bootstrap()
    import engine
    import store

    source = Path(args.dwg)
    if not source.exists():
        print(f"文件不存在：{source}", file=sys.stderr)
        return 2
    if source.suffix.lower() != ".dwg":
        print("run 只接受 DWG；DXF 请先用 geometry_modifier/drawing_measure 做离线核验。",
              file=sys.stderr)
        return 2

    record = store.save_upload(source, source.name)
    task = store.create_task(args.instruction, record, None)
    targets_override = _parse_targets(args.targets)
    if targets_override:
        task["targets_override"] = targets_override

    try:
        engine.parse_and_plan(task, use_llm=not args.no_llm)
    except Exception as exc:  # noqa: BLE001
        store.save_task(task)
        print(f"解析/规划失败：{exc}", file=sys.stderr)
        for entry in task.get("logs", [])[-8:]:
            print(f"  [{entry['step']}] {entry['msg']}", file=sys.stderr)
        return 3

    if task.get("status") == "needs_calibration":
        print(_json({
            "ok": False,
            "status": "needs_calibration",
            "task_id": task["task_id"],
            "message": "当前图纸未定标，拒绝执行几何修改。",
            "calibration_required": task.get("plan", {}).get("calibration_required"),
        }))
        return 2
    if task.get("status") == "needs_instruction":
        print(_json({
            "ok": False,
            "status": "needs_instruction",
            "task_id": task["task_id"],
            "message": "尚未识别到明确的修改对象和数值，拒绝执行。",
            "instruction_required": task.get("plan", {}).get("instruction_required"),
        }))
        return 2

    task["approval"] = {
        "approved": True,
        "by": "cli",
        "note": "CLI 显式执行",
        "plan_snapshot": task.get("plan", {}),
    }
    task["status"] = "approved"
    store.save_task(task)
    try:
        engine.execute(task)
    except Exception as exc:  # noqa: BLE001
        store.save_task(task)
        print(f"执行失败：{exc}", file=sys.stderr)
        for entry in task.get("logs", [])[-10:]:
            print(f"  [{entry['step']}] {entry['msg']}", file=sys.stderr)
        return 3

    task_dir = store.TASKS / task["task_id"]
    report_path = task.get("report")
    report = {}
    if report_path and Path(report_path).exists():
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))

    copied = []
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in ("result.dwg", "result.dxf", "annotated.dwg",
                     "annotated_最终.dxf", "report.json", "manifest.json",
                     "before.png", "after.png", "zoom.png", "before.svg",
                     "after.svg", "zoom.svg"):
            src = task_dir / name
            if src.exists():
                shutil.copy2(src, out_dir / name)
                copied.append(str(out_dir / name))

    print(_json({
        "ok": report.get("ok", task.get("status") == "executed"),
        "task_id": task["task_id"],
        "status": task["status"],
        "task_type": task.get("task_type"),
        "profile_id": task.get("profile_id"),
        "task_dir": str(task_dir),
        "report": report,
        "copied": copied,
    }))
    return 0 if report.get("ok", task.get("status") == "executed") else 1


def cmd_learn(args) -> int:
    runtime.bootstrap()
    import learning_loop

    action = args.action
    try:
        if action == "status":
            result = learning_loop.status()
            code = 0
        elif action == "observe":
            result = learning_loop.observe()
            code = 0
        elif action == "propose":
            result = learning_loop.propose()
            code = 0
        elif action == "proposals":
            result = {
                "ok": True,
                "proposals": learning_loop.list_proposals(args.status),
            }
            code = 0
        elif action == "replay":
            result = learning_loop.replay(with_dwg=not args.quick)
            code = 0 if result.get("ok") else 1
        elif action == "cycle":
            result = learning_loop.cycle(with_dwg=not args.quick)
            code = 0 if result.get("ok") else 1
        elif action == "seal":
            result = learning_loop.seal_cases()
            code = 0 if result.get("ok") else 1
        elif action == "audit":
            result = learning_loop.audit(limit=args.limit)
            code = 0 if result.get("ok") else 1
        elif action == "feedback":
            if not args.target or not args.value:
                raise ValueError("feedback 用法：learn feedback <task_id> accepted|needs_work|rejected")
            feedback = learning_loop.record_feedback(
                args.target,
                args.value,
                note=args.note,
                version=args.version,
                by=args.by,
            )
            observed = learning_loop.observe()
            proposals = learning_loop.propose()
            result = {
                "ok": True,
                "feedback": feedback,
                "observed": observed,
                "proposals_created": proposals["created"],
            }
            code = 0
        elif action == "approve":
            if not args.target:
                raise ValueError("approve 用法：learn approve <proposal_id>")
            proposal = learning_loop.approve_proposal(
                args.target, note=args.note, by=args.by
            )
            result = {"ok": proposal.get("status") == "promoted", "proposal": proposal}
            code = 0 if proposal.get("status") in {"promoted", "acknowledged"} else 1
        elif action == "reject":
            if not args.target:
                raise ValueError("reject 用法：learn reject <proposal_id>")
            proposal = learning_loop.reject_proposal(
                args.target, note=args.note, by=args.by
            )
            result = {"ok": True, "proposal": proposal}
            code = 0
        else:
            raise ValueError(f"未知自改进操作：{action}")
    except Exception as exc:  # noqa: BLE001
        print(_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1

    print(_json(result))
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cli.py", description="工程图智能改图工作台 CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="环境体检")
    check.set_defaults(func=cmd_check)

    profiles = subparsers.add_parser("profiles", help="图纸档案管理")
    profiles.add_argument("action", choices=("list", "show", "install"))
    profiles.add_argument("profile_id", nargs="?")
    profiles.add_argument("file", nargs="?")
    profiles.add_argument("--force", action="store_true", help="跳过人工复核门槛")
    profiles.add_argument("--replace", action="store_true", help="覆盖已安装档案")
    profiles.set_defaults(func=cmd_profiles)

    calibrate = subparsers.add_parser("calibrate", help="扫描新图纸并生成档案骨架")
    calibrate.add_argument("dwg", help="DWG 或 DXF")
    calibrate.add_argument("--id", help="profile_id")
    calibrate.add_argument("--model")
    calibrate.add_argument("--title")
    calibrate.add_argument("--out", help="draft JSON 输出路径")
    calibrate.add_argument("--workdir", help="DWG 转换工作目录")
    calibrate.set_defaults(func=cmd_calibrate)

    measure = subparsers.add_parser("measure", help="独立复测 DXF 的 H2/D2/轴伸")
    measure.add_argument("dxf")
    measure.add_argument("--profile")
    measure.set_defaults(func=cmd_measure)

    run = subparsers.add_parser("run", help="执行一次真实改图任务")
    run.add_argument("dwg")
    run.add_argument("instruction")
    run.add_argument("--targets", help="覆盖目标，如 H2=443,D2=294,shaft=20")
    run.add_argument("--out", help="复制交付产物到该目录")
    run.add_argument("--no-llm", action="store_true", help="只用关键词解析")
    run.set_defaults(func=cmd_run)

    learn = subparsers.add_parser("learn", help="自改进 loop：观察、提案、隔离重放与基线晋升")
    learn.add_argument(
        "action",
        choices=(
            "status", "observe", "propose", "proposals", "replay",
            "cycle", "seal", "audit", "feedback", "approve", "reject",
        ),
    )
    learn.add_argument("target", nargs="?", help="task_id 或 proposal_id")
    learn.add_argument("value", nargs="?", help="feedback verdict")
    learn.add_argument("--note", default="", help="验收/审核说明")
    learn.add_argument("--version", type=int, help="验收对应版本，默认当前版本")
    learn.add_argument("--by", default="user", help="操作者")
    learn.add_argument("--status", help="proposals 过滤状态")
    learn.add_argument("--limit", type=int, default=20, help="audit 返回事件数")
    learn.add_argument("--quick", action="store_true", help="重放时跳过 DWG 导出/回读")
    learn.set_defaults(func=cmd_learn)

    args = parser.parse_args(argv)
    if args.command == "profiles" and args.action == "install" \
            and not args.file and args.profile_id:
        args.file, args.profile_id = args.profile_id, None
    if args.command == "profiles" and args.action in ("show", "install"):
        if args.action == "show" and not args.profile_id:
            parser.error("profiles show 需要 profile_id")
        if args.action == "install" and not args.file:
            parser.error("profiles install 需要档案 JSON 文件")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

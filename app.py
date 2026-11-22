"""AI CAD 智能修改工作台 - FastAPI 后端入口。

对应方案文档第 10.2 节 API 清单的 MVP 实现。
运行：python app.py  （默认 http://127.0.0.1:8900）
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
import runtime

runtime.bootstrap()
sys.path.insert(0, str(ROOT / "workbench"))

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import store
from store import TASKS, create_task, get_task, list_tasks, log, rollback, save_task
import engine
import learning_loop

app = FastAPI(title="AI CAD 智能修改工作台", version="2.5.0")

WEB = ROOT / "web"
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")

DEMO_DWG = ROOT / "input" / "TD50-40G.dwg"
DEMO_INSTRUCTION = "风罩高度443，接线盒伸出294，轴伸加20"
# 定版配方：用户确认的"最棒的图"（H2=443 / D2=294 / 轴伸+20，含 C5A 随动修复）。
# demo 只复用带此锁定标记且基于干净基线执行成功的任务，避免误抓历史中间版本。
DEMO_RECIPE_ID = "td50-final-443-294-20"
DEMO_TARGETS = {"H2": 443.0, "D2": 294.0, "shaft": 20.0}

# 25QYL 标注换算示例（第二条链）：1:1 实测 + 竖向标注重叠修复
DEMO25_DWG = ROOT / "input" / "25QYL-2SL.dwg"
DEMO25_INSTRUCTION = "把标注改成严格1:1按图形实测，并修复竖向标注重叠看不清"
DEMO25_RECIPE_ID = "25qyl-final-1to1-fixed"
DEMO25_REAL_MM_INSTRUCTION = "把图改成真实毫米版，总长610mm，总高284mm，并修复竖向标注重叠"
DEMO25_REAL_MM_RECIPE_ID = "25qyl-final-real-mm-610x284"
_INSPECT_CACHE: dict[tuple[str, int], dict] = {}
_LOCATE_CACHE: dict[tuple[str, int, tuple[str, ...]], dict] = {}


def _prepared_upload_dxf(info: dict) -> tuple[Path, str]:
    """把上传 DWG 转成可复用的检查 DXF；DXF 直接返回原文件。"""
    from dwg_bridge import dwg_to_dxf

    source = Path(info["path"])
    if source.suffix.lower() == ".dxf":
        return source, "direct-dxf"
    if source.suffix.lower() != ".dwg":
        raise ValueError(f"不支持扫描的文件类型：{source.suffix}")

    digest = str(info.get("sha256") or hashlib.sha256(source.read_bytes()).hexdigest())
    file_id = re.sub(r"[^0-9A-Za-z_-]", "", str(info.get("file_id") or "upload"))
    cache_dir = source.parent / "inspect_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{file_id}_{digest[:16]}.dxf"
    meta = cached.with_suffix(".meta.json")
    if cached.exists() and cached.stat().st_size > 0:
        converter = "cached"
        try:
            converter = str(json.loads(meta.read_text(encoding="utf-8")).get("converter") or converter)
        except Exception:  # noqa: BLE001
            pass
        return cached, converter

    with tempfile.TemporaryDirectory(prefix="inspect_", dir=str(cache_dir)) as tmp:
        work = Path(tmp)
        raw = work / "source.dxf"
        conversion = dwg_to_dxf(source, raw)
        if not conversion.get("ok"):
            raise RuntimeError(f"DWG→DXF 失败：{conversion.get('error')}")
        clean = raw
        if conversion.get("needs_cleaning"):
            from dxf_cleaner import clean_dxf

            clean = work / "clean.dxf"
            clean_dxf(raw, clean)
        staged = cache_dir / f".{file_id}_{uuid.uuid4().hex}.tmp.dxf"
        shutil.copy2(clean, staged)
        staged.replace(cached)
        meta.write_text(
            json.dumps(
                {"converter": conversion.get("converter") or "unknown"},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    for old in cache_dir.glob(f"{file_id}_*.dxf"):
        if old != cached:
            old.unlink(missing_ok=True)
    return cached, str(conversion.get("converter") or "unknown")


def _inspect_upload(info: dict) -> dict:
    """扫描上传图纸中可直接修改的文字/标注，按文件修改时间缓存。"""
    import ezdxf

    import text_edit_engine

    source = Path(info["path"])
    key = (str(source.resolve()), source.stat().st_mtime_ns)
    cached = _INSPECT_CACHE.get(key)
    if cached is not None:
        return cached

    dxf_path, converter = _prepared_upload_dxf(info)
    doc = ezdxf.readfile(str(dxf_path))
    result = text_edit_engine.inspect_doc(doc)
    result["converter"] = converter
    _INSPECT_CACHE[key] = result
    if len(_INSPECT_CACHE) > 32:
        oldest = next(iter(_INSPECT_CACHE))
        _INSPECT_CACHE.pop(oldest, None)
    return result


def _inspect_summary(result: dict, limit: int = 8) -> dict:
    from edit_intent import suggest_instruction

    items = (result.get("items") or [])[:limit]
    return {
        "editable_count": result.get("editable_count", 0),
        "kinds": result.get("kinds", {}),
        "scopes": result.get("scopes", {}),
        "dimension_count": result.get("dimension_count", 0),
        "shared_text_count": result.get("shared_text_count", 0),
        "shared_blocks": result.get("shared_blocks", []),
        "converter": result.get("converter"),
        "sample": items,
        "truncated": len(result.get("items") or []) > limit,
        "suggestions": [
            suggest_instruction(item)
            for item in items[:8]
        ],
    }


@app.get("/")
def index():
    return FileResponse(str(WEB / "workbench.html"))


@app.get("/learning")
def learning_page():
    return FileResponse(str(WEB / "learning.html"))


@app.get("/api/health")
def health():
    return {"ok": True, "service": "ai-cad-workbench", "version": "2.5.0",
            "tasks": len(list_tasks()), "reference_optional": True}


@app.get("/api/env")
def env_info():
    """环境体检：依赖来源、ODA、渲染器、大模型配置。"""
    return {"ok": True, "env": runtime.diagnostics()}


@app.get("/api/profiles")
def profile_list():
    import drawing_profile

    items = []
    for profile in drawing_profile.profiles():
        items.append({
            "profile_id": profile.profile_id,
            "model": profile.model,
            "title": profile.title,
            "source_file": profile.data.get("source_file"),
            "calibration": profile.data.get("calibration"),
            "rule_keys": sorted(profile.rules.keys()),
        })
    return {"ok": True, "profiles": items,
            "directory": str(drawing_profile.PROFILE_DIR)}


@app.get("/api/profiles/{profile_id}")
def profile_detail(profile_id: str):
    import drawing_profile

    profile = drawing_profile.get_profile(profile_id)
    if profile is None:
        raise HTTPException(404, f"档案 {profile_id} 不存在")
    return {"ok": True, "profile": profile.data}


@app.post("/api/profiles")
def profile_save(body: dict, force: bool = False):
    import drawing_profile

    from calibration import readiness_problems

    problems = readiness_problems(body)
    if problems and not force:
        raise HTTPException(400, {"message": "档案未达到可安装状态", "problems": problems})
    try:
        path = drawing_profile.save_profile(body)
    except drawing_profile.DrawingProfileError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "path": str(path)}


@app.get("/api/skill")
def skill_info():
    """返回封装进工作台的 skill 知识（几何变形链 + 标注换算链）。"""
    from intent_parser import _rules
    rules = _rules()
    skill_md = ROOT / "skill" / "SKILL.md"
    ann_path = ROOT / "skill" / "annotation_rules.json"
    ann_rules = json.loads(ann_path.read_text(encoding="utf-8")) if ann_path.exists() else {}
    return {
        "ok": True,
        "rules": rules,                 # TD50 几何变形链
        "annotation_rules": ann_rules,  # 25QYL 标注换算链
        "skill_md": skill_md.read_text(encoding="utf-8") if skill_md.exists() else "",
    }


@app.get("/api/learn/status")
def learn_status():
    return learning_loop.status()


@app.get("/api/learn/episodes")
def learn_episodes(limit: int = 100):
    return {"ok": True, "episodes": learning_loop.list_episodes()[:max(1, limit)]}


@app.get("/api/learn/cases")
def learn_cases():
    return {
        "ok": True,
        "cases": learning_loop.list_cases_with_integrity(include_invalidated=True),
    }


@app.post("/api/learn/cases/seal")
def learn_seal_cases():
    return learning_loop.seal_cases()


@app.get("/api/learn/proposals")
def learn_proposals(status: str | None = None):
    return {"ok": True, "proposals": learning_loop.list_proposals(status)}


@app.get("/api/learn/ledger")
def learn_ledger(limit: int = 30):
    return learning_loop.audit(limit=limit)


@app.post("/api/learn/observe")
def learn_observe():
    return {"ok": True, **learning_loop.observe()}


@app.post("/api/learn/propose")
def learn_propose():
    return {"ok": True, **learning_loop.propose()}


@app.post("/api/learn/cycle")
def learn_cycle(with_dwg: bool = True):
    return learning_loop.cycle(with_dwg=with_dwg)


@app.post("/api/learn/replay")
def learn_replay(with_dwg: bool = True):
    return learning_loop.replay(with_dwg=with_dwg)


@app.post("/api/learn/proposals/{proposal_id}/approve")
def learn_approve(proposal_id: str, body: dict | None = None):
    body = body or {}
    try:
        proposal = learning_loop.approve_proposal(
            proposal_id,
            note=body.get("note", ""),
            by=body.get("by", "user"),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": proposal.get("status") in {"promoted", "acknowledged"},
        "proposal": proposal,
    }


@app.post("/api/learn/proposals/{proposal_id}/reject")
def learn_reject(proposal_id: str, body: dict | None = None):
    body = body or {}
    try:
        proposal = learning_loop.reject_proposal(
            proposal_id,
            note=body.get("note", ""),
            by=body.get("by", "user"),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "proposal": proposal}


@app.post("/api/parse-intent")
def parse_intent_api(body: dict):
    """自然语言→结构化意图预览：前端输入时实时显示听懂的目标值/约束/追问。"""
    from edit_intent import parse_with_drawing
    from intent_parser import parse_intent

    text = (body.get("instruction") or "").strip()
    if not text:
        raise HTTPException(400, "instruction 必填")
    inventory = None
    drawing_scan = None
    scan_error = None
    fid = body.get("file_id")
    if fid:
        info = store.get_file(str(fid))
        if info:
            try:
                inventory = _inspect_upload(info)
                drawing_scan = _inspect_summary(inventory)
            except Exception as exc:  # noqa: BLE001
                scan_error = f"{type(exc).__name__}: {exc}"[:240]
    if inventory is not None:
        intent = parse_with_drawing(
            text, inventory, use_llm=body.get("use_llm", True)
        )
    else:
        intent = parse_intent(text, use_llm=body.get("use_llm", True))
    return {
        "ok": True,
        "intent": intent,
        "drawing_scan": drawing_scan,
        "scan_error": scan_error,
    }


# ---------------------------------------------------------------- 文件上传
@app.post("/api/files")
async def upload_file(file: UploadFile = File(...)):
    """上传 DWG 原图，或可选的参考图片；原文件只读保存并计算 SHA-256。"""
    suffix = Path(file.filename or "file").suffix.lower()
    if suffix not in (".dwg", ".dxf", ".png", ".jpg", ".jpeg"):
        raise HTTPException(400, f"不支持的文件类型：{suffix}")
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)
    info = store.save_upload(tmp_path, file.filename or tmp_path.name)
    tmp_path.unlink(missing_ok=True)
    if suffix == ".dwg":
        try:
            from dwg_bridge import probe_dwg
            Path(info["path"]).chmod(0o666)
            info["probe"] = probe_dwg(info["path"])
            Path(info["path"]).chmod(0o444)
        except Exception as ex:
            info["probe_error"] = str(ex)[:200]
    return {"ok": True, "file": info}


@app.get("/api/files/{file_id}/inspect")
def inspect_upload(file_id: str, limit: int = 60):
    """上传后立即扫描图纸中可改的文字/标注，供用户按实际内容下指令。"""
    info = store.get_file(file_id)
    if not info:
        raise HTTPException(404, "文件不存在")
    try:
        result = _inspect_upload(info)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"图纸扫描失败：{exc}") from exc
    limit = max(1, min(int(limit), 500))
    return {
        "ok": True,
        "file": {
            "file_id": info.get("file_id"),
            "name": info.get("name"),
            "sha256": info.get("sha256"),
        },
        "inspection": {
            **_inspect_summary(result, limit=limit),
            "items": (result.get("items") or [])[:limit],
            "truncated": len(result.get("items") or []) > limit,
        },
    }


def _requested_handles(raw: str) -> list[str]:
    handles = []
    seen = set()
    for value in re.split(r"[\s,，;；]+", str(raw or "")):
        handle = value.strip()
        key = handle.casefold()
        if not handle or key in seen:
            continue
        seen.add(key)
        handles.append(handle)
    return handles


def _locate_upload(info: dict, handles: list[str]) -> dict:
    import ezdxf

    import text_locate

    source = Path(info["path"])
    key = (
        str(source.resolve()),
        source.stat().st_mtime_ns,
        tuple(handle.casefold() for handle in handles),
    )
    if key in _LOCATE_CACHE:
        return _LOCATE_CACHE[key]
    inventory = _inspect_upload(info)
    dxf_path, _converter = _prepared_upload_dxf(info)
    located = text_locate.locate_handles(
        ezdxf.readfile(str(dxf_path)),
        handles,
        inventory,
    )
    _LOCATE_CACHE[key] = located
    if len(_LOCATE_CACHE) > 64:
        oldest = next(iter(_LOCATE_CACHE))
        _LOCATE_CACHE.pop(oldest, None)
    return located


@app.get("/api/files/{file_id}/locate")
def locate_upload(file_id: str, handles: str = ""):
    """返回选中文字/标注的世界坐标框，以及全图、局部高亮预览 URL。"""
    info = store.get_file(file_id)
    if not info:
        raise HTTPException(404, "文件不存在")
    requested = _requested_handles(handles)
    if not requested:
        raise HTTPException(400, "handles 必填，多个句柄用逗号分隔")
    if len(requested) > 100:
        raise HTTPException(400, "一次最多定位 100 个句柄")
    try:
        located = _locate_upload(info, requested)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"定位失败：{exc}") from exc

    token = quote(",".join(requested), safe="")
    version = hashlib.sha256(
        json.dumps(
            {
                "renderer": "2.5.0",
                "sha256": info.get("sha256"),
                "handles": [item["handle"] for item in located.get("items") or []],
                "boxes": [item.get("boxes") for item in located.get("items") or []],
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:12]
    base = f"/api/files/{file_id}/locate.png?handles={token}&v={version}"
    svg_base = f"/api/files/{file_id}/locate.svg?handles={token}&v={version}"
    return {
        "ok": True,
        **located,
        "full_url": f"{base}&mode=full",
        "zoom_url": f"{base}&mode=zoom",
        "full_svg_url": f"{svg_base}&mode=full",
        "zoom_svg_url": f"{svg_base}&mode=zoom",
    }


def _ensure_locate_preview(file_id: str, handles: str, mode: str):
    """生成并缓存高亮 PNG + SVG；两种格式共用同一视口和红框。"""
    info = store.get_file(file_id)
    if not info:
        raise HTTPException(404, "文件不存在")
    requested = _requested_handles(handles)
    if not requested:
        raise HTTPException(400, "handles 必填，多个句柄用逗号分隔")
    if len(requested) > 100:
        raise HTTPException(400, "一次最多定位 100 个句柄")
    mode = str(mode or "full").lower()
    if mode not in {"full", "zoom"}:
        raise HTTPException(400, "mode 只能是 full 或 zoom")
    try:
        located = _locate_upload(info, requested)
        dxf_path, _converter = _prepared_upload_dxf(info)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"定位失败：{exc}") from exc

    boxes = [
        box
        for item in located.get("items") or []
        for box in item.get("boxes") or []
        if len(box) == 4
    ]
    if not boxes:
        raise HTTPException(404, "选中句柄没有可高亮的可见实例")
    source = Path(info["path"])
    digest = hashlib.sha256(
        json.dumps(
            {
                "version": "2.5.0-vector",
                "sha256": info.get("sha256"),
                "handles": requested,
                "boxes": boxes,
                "mode": mode,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    preview_dir = source.parent / "locate_previews" / file_id
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview = preview_dir / f"{digest}_{mode}.png"
    vector = preview_dir / f"{digest}_{mode}.svg"
    if (
        not preview.exists()
        or preview.stat().st_size == 0
        or not vector.exists()
        or vector.stat().st_size == 0
    ):
        highlights = []
        for item in located.get("items") or []:
            for box in item.get("boxes") or []:
                highlights.append(
                    {
                        "box": box,
                        "label": str(item.get("handle") or ""),
                    }
                )
        viewport = (
            engine.highlight_viewport(boxes)
            if mode == "zoom"
            else None
        )
        if mode == "zoom" and viewport is None:
            raise HTTPException(400, "无法计算局部放大视口")
        if not engine.render_highlight_png(
            dxf_path,
            preview,
            highlights,
            viewport=viewport,
            svg=vector,
        ):
            raise HTTPException(500, "高亮预览渲染失败")
    return preview, vector


@app.get("/api/files/{file_id}/locate.png")
def locate_upload_png(file_id: str, handles: str = "", mode: str = "full"):
    """生成选中项高亮位图；mode=full 看全图，mode=zoom 局部放大。"""
    preview, _vector = _ensure_locate_preview(file_id, handles, mode)
    return FileResponse(
        str(preview),
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.get("/api/files/{file_id}/locate.svg")
def locate_upload_svg(file_id: str, handles: str = "", mode: str = "full"):
    """生成选中项高亮矢量图；浏览器放大后线条和文字仍保持清晰。"""
    _preview, vector = _ensure_locate_preview(file_id, handles, mode)
    return FileResponse(
        str(vector),
        media_type="image/svg+xml",
        headers={"Cache-Control": "private, max-age=3600"},
    )


# ---------------------------------------------------------------- 任务
@app.post("/api/tasks")
def create(body: dict):
    """创建修改任务并立即执行解析+规划；参考图不是必填项。"""
    fid = body.get("file_id")
    instruction = (body.get("instruction") or "").strip()
    if not fid or not instruction:
        raise HTTPException(400, "file_id 与 instruction 必填")
    src = store.get_file(fid) or _find_upload(fid)
    if not src:
        raise HTTPException(404, "文件不存在，请先上传")
    ref = store.get_file(body["ref_file_id"]) if body.get("ref_file_id") else None
    task = create_task(instruction, src, ref)
    if isinstance(body.get("targets_override"), dict):
        task["targets_override"] = body["targets_override"]
    try:
        engine.parse_and_plan(task)
    except Exception as ex:
        log(task, "parse", f"解析失败：{ex}", ok=False)
        save_task(task)
        raise HTTPException(500, f"解析失败：{ex}")
    return {"ok": True, "task": _public(task)}


def _find_upload(fid: str):
    for p in store.UPLOADS.glob(f"{fid}.*"):
        if p.suffix.lower() == ".json":
            continue
        return {"file_id": fid, "name": p.name, "path": str(p), "sha256": ""}
    return None


@app.get("/api/tasks")
def tasks():
    return {"ok": True, "tasks": list_tasks()}


@app.get("/api/tasks/{tid}")
def task_detail(tid: str):
    t = get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    return {"ok": True, "task": _public(t)}


@app.get("/api/tasks/{tid}/entities")
def entities(tid: str):
    t = get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    return {"ok": True, "parse": t.get("parse", {}), "plan": t.get("plan", {})}


@app.post("/api/tasks/{tid}/approve")
def approve(tid: str, body: dict | None = None):
    t = get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t["status"] == "needs_instruction":
        questions = (
            (t.get("plan") or {}).get("instruction_required") or {}
        ).get("questions") or []
        detail = "尚未识别到明确的修改要求，请补充对象和数值后再确认"
        if questions:
            detail += f"：{questions[0]}"
        raise HTTPException(400, detail)
    if t["status"] != "planned":
        raise HTTPException(400, f"当前状态 {t['status']} 不可审批")
    t["approval"] = {"approved": True, "by": "user", "note": (body or {}).get("note", ""),
                     "plan_snapshot": t.get("plan", {})}
    t["status"] = "approved"
    log(t, "approve", "用户已确认修改计划")
    save_task(t)
    return {"ok": True, "task": _public(t)}


@app.post("/api/tasks/{tid}/execute")
def run(tid: str):
    t = get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    if t.get("status") == "needs_calibration":
        raise HTTPException(
            409,
            "当前图纸未定标，不能执行几何修改。请先在“图纸档案”里完成定标，"
            "或换用已定标型号的原始图纸。",
        )
    if t.get("status") == "needs_instruction":
        questions = (
            (t.get("plan") or {}).get("instruction_required") or {}
        ).get("questions") or []
        detail = (
            "图纸已扫描，但修改目标还不唯一。参考图可选；请根据面板里的候选"
            "补充要改的文字、位置或句柄，并明确目标值。"
        )
        if questions:
            detail += f" {questions[0]}"
        raise HTTPException(
            409,
            detail,
        )
    try:
        report = engine.execute(t)
    except Exception as ex:
        log(t, "execute", f"执行失败：{ex}", ok=False)
        save_task(t)
        raise HTTPException(500, f"执行失败：{ex}")
    return {"ok": True, "report": report, "task": _public(t)}


@app.post("/api/tasks/{tid}/feedback")
def task_feedback(tid: str, body: dict):
    verdict = (body.get("verdict") or "").strip()
    if not verdict:
        raise HTTPException(400, "verdict 必填")
    try:
        feedback = learning_loop.record_feedback(
            tid,
            verdict,
            note=(body.get("note") or "").strip(),
            version=body.get("version"),
            by=body.get("by") or "user",
        )
        observed = learning_loop.observe()
        proposals = learning_loop.propose()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc
    return {
        "ok": True,
        "feedback": feedback,
        "observed": observed,
        "new_proposals": proposals["created"],
    }


@app.get("/api/tasks/{tid}/preview")
def preview(tid: str):
    t = get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    tdir = TASKS / tid
    out = {}
    for name in ("before.png", "after.png", "before.svg", "after.svg"):
        if (tdir / name).exists():
            out[name.split(".")[0] + "_" + name.split(".")[1]] = f"/api/tasks/{tid}/download?kind={name}"
    return {"ok": True, "files": out}


@app.get("/api/tasks/{tid}/report")
def report(tid: str):
    t = get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    p = TASKS / tid / "report.json"
    if not p.exists():
        return {"ok": False, "message": "尚未执行"}
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/api/tasks/{tid}/download")
def download(tid: str, kind: str = "dxf"):
    tdir = TASKS / tid
    mapping = {"dxf": "result.dxf", "dwg": "result.dwg", "report": "report.json",
               "before.png": "before.png", "after.png": "after.png",
               "before.svg": "before.svg", "after.svg": "after.svg",
               "zoom.png": "zoom.png", "zoom.svg": "zoom.svg"}
    name = mapping.get(kind)
    if (
        name
        and kind in {"before.svg", "after.svg"}
        and not (tdir / name).exists()
    ):
        # 历史任务按需补矢量预览，不重新执行、不改交付文件。
        source_name = "clean.dxf" if kind == "before.svg" else "result.dxf"
        source = tdir / source_name
        if source.exists():
            engine.render_vector(source, tdir / name)
    if not name or not (tdir / name).exists():
        raise HTTPException(404, f"成果 {kind} 不存在（请先执行）")
    return FileResponse(str(tdir / name), filename=f"{tid}_{name}")


@app.post("/api/tasks/{tid}/rollback")
def do_rollback(tid: str, body: dict):
    t = get_task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    try:
        restored = rollback(t, int(body.get("version", 0)))
    except FileNotFoundError as ex:
        raise HTTPException(404, str(ex))
    save_task(t)
    return {"ok": True, "restored": list(restored.keys()), "task": _public(t)}


# ---------------------------------------------------------------- 内置示例
@app.post("/api/demo/td50")
def demo_td50(auto_execute: bool = True, force: bool = False):
    """内置示范任务：仅上传 DWG + 一句话明确要求，不使用参考图。

    已执行过则直接返回现有任务（幂等），不重复建任务。
    """
    # 只复用"定版配方"且基于内置干净源文件执行成功的任务。
    # 历史遗留任务（如 455/12/D2不动 的中间版本）不再被误当作示例。
    if not force:
        for item in list_tasks():
            if item.get("recipe_id") == DEMO_RECIPE_ID and item["status"] == "executed":
                t = get_task(item["task_id"])
                if t and (t.get("files", {}).get("source") or {}).get("name") == "TD50-40G.dwg":
                    return {"ok": True, "task": _public(t), "reused": True}
    if not DEMO_DWG.exists():
        raise HTTPException(500, f"示例源文件缺失：{DEMO_DWG}")
    src = store.save_upload(DEMO_DWG, "TD50-40G.dwg")
    task = create_task(DEMO_INSTRUCTION, src, None)
    task["recipe_id"] = DEMO_RECIPE_ID
    task["targets_override"] = dict(DEMO_TARGETS)
    engine.parse_and_plan(task)
    task["approval"] = {"approved": True, "by": "demo", "note": "示例任务自动确认",
                        "plan_snapshot": task.get("plan", {})}
    task["status"] = "approved"
    save_task(task)
    if auto_execute:
        try:
            engine.execute(task)
        except Exception as ex:
            log(task, "execute", f"示例执行失败：{ex}", ok=False)
            save_task(task)
            raise HTTPException(500, f"示例执行失败：{ex}")
    return {"ok": True, "task": _public(task), "reused": False}


@app.post("/api/demo/25qyl")
def demo_25qyl(auto_execute: bool = True, force: bool = False):
    """内置示范任务：25QYL-2SL.dwg + 标注 1:1 实测换算 + 竖向标注重叠修复。

    已执行过则复用现有任务（幂等）。
    """
    if not force:
        for item in list_tasks():
            if item.get("recipe_id") == DEMO25_RECIPE_ID and item["status"] == "executed":
                t = get_task(item["task_id"])
                if t and (t.get("files", {}).get("source") or {}).get("name") == "25QYL-2SL.dwg":
                    return {"ok": True, "task": _public(t), "reused": True}
    if not DEMO25_DWG.exists():
        raise HTTPException(500, f"示例源文件缺失：{DEMO25_DWG}")
    src = store.save_upload(DEMO25_DWG, "25QYL-2SL.dwg")
    task = create_task(DEMO25_INSTRUCTION, src, None)
    task["recipe_id"] = DEMO25_RECIPE_ID
    engine.parse_and_plan(task)
    task["approval"] = {"approved": True, "by": "demo", "note": "示例任务自动确认",
                        "plan_snapshot": task.get("plan", {})}
    task["status"] = "approved"
    save_task(task)
    if auto_execute:
        try:
            engine.execute(task)
        except Exception as ex:
            log(task, "execute", f"示例执行失败：{ex}", ok=False)
            save_task(task)
            raise HTTPException(500, f"示例执行失败：{ex}")
    return {"ok": True, "task": _public(task), "reused": False}


@app.post("/api/demo/25qyl-real-mm")
def demo_25qyl_real_mm(auto_execute: bool = True, force: bool = False):
    """内置示范任务：25QYL 真实毫米 1:1（总长 610 mm、总高 284 mm）。"""
    if not force:
        for item in list_tasks():
            if item.get("recipe_id") == DEMO25_REAL_MM_RECIPE_ID and item["status"] == "executed":
                t = get_task(item["task_id"])
                if t and (t.get("files", {}).get("source") or {}).get("name") == "25QYL-2SL.dwg":
                    return {"ok": True, "task": _public(t), "reused": True}
    if not DEMO25_DWG.exists():
        raise HTTPException(500, f"示例源文件缺失：{DEMO25_DWG}")
    src = store.save_upload(DEMO25_DWG, "25QYL-2SL.dwg")
    task = create_task(DEMO25_REAL_MM_INSTRUCTION, src, None)
    task["recipe_id"] = DEMO25_REAL_MM_RECIPE_ID
    engine.parse_and_plan(task)
    task["approval"] = {"approved": True, "by": "demo", "note": "真实毫米示例自动确认",
                        "plan_snapshot": task.get("plan", {})}
    task["status"] = "approved"
    save_task(task)
    if auto_execute:
        try:
            engine.execute(task)
        except Exception as ex:
            log(task, "execute", f"示例执行失败：{ex}", ok=False)
            save_task(task)
            raise HTTPException(500, f"示例执行失败：{ex}")
    return {"ok": True, "task": _public(task), "reused": False}


def _public(t: dict) -> dict:
    """对外脱敏：不返回服务器绝对路径。"""
    t = json.loads(json.dumps(t, ensure_ascii=False))
    for f in (t.get("files") or {}).values():
        if f:
            f.pop("path", None)
    return t


if __name__ == "__main__":
    import uvicorn
    port = int(__import__("os").environ.get("PORT", "8900"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

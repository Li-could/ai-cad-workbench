"""AI CAD 智能修改工作台 - 任务与文件存储（文件型 JSON 存储，MVP 阶段）。

对应方案文档第 10.1 节核心数据对象(files/tasks/versions/audit)的本地实现：
uploads/ 保存原始上传文件（只读保存 + SHA-256），tasks/{id}/ 保存任务清单、
执行日志、质检报告与版本快照（可回滚）。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
TASKS = DATA / "tasks"

_lock = threading.Lock()


def _init():
    UPLOADS.mkdir(parents=True, exist_ok=True)
    TASKS.mkdir(parents=True, exist_ok=True)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def save_upload(src: Path, original_name: str) -> dict:
    """保存上传文件：原文件只读保存，计算哈希，返回 file 记录。"""
    _init()
    fid = uuid.uuid4().hex[:12]
    ext = Path(original_name).suffix.lower()
    dst = UPLOADS / f"{fid}{ext}"
    shutil.copy2(src, dst)
    dst.chmod(0o444)  # 原始文件只读
    record = {
        "file_id": fid,
        "name": original_name,
        "path": str(dst),
        "size": dst.stat().st_size,
        "sha256": _sha256(dst),
        "saved_at": _now(),
    }
    # 侧车索引：上传后即使还没建任务也能按 file_id 找回。
    (UPLOADS / f"{fid}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return record


def get_file(fid: str) -> dict | None:
    sidecar = UPLOADS / f"{fid}.json"
    if sidecar.exists():
        try:
            return json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            pass
    for t in TASKS.glob("*/manifest.json"):
        try:
            m = json.loads(t.read_text(encoding="utf-8"))
        except Exception:
            continue
        for f in m.get("files", {}).values():
            if f and f.get("file_id") == fid:
                return f
    return None


def create_task(
    instruction: str, source_file: dict, ref_file: dict | None = None
) -> dict:
    _init()
    tid = uuid.uuid4().hex[:12]
    tdir = TASKS / tid
    (tdir / "versions").mkdir(parents=True)
    task = {
        "task_id": tid,
        "instruction": instruction,
        "status": "created",          # created -> parsed -> planned -> approved -> executed
        "created_at": _now(),
        "updated_at": _now(),
        "files": {"source": source_file, "reference": ref_file},
        "parse": {},
        "plan": {},
        "approval": None,
        "versions": [],
        "current_version": 0,
        "report": None,
        "logs": [],
    }
    _save(task)
    return task


def _save(task: dict):
    with _lock:
        p = TASKS / task["task_id"] / "manifest.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(task, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)


def save_task(task: dict):
    task["updated_at"] = _now()
    _save(task)


def get_task(tid: str) -> dict | None:
    p = TASKS / tid / "manifest.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def list_tasks() -> list[dict]:
    _init()
    out = []
    for t in sorted(TASKS.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            m = json.loads(t.read_text(encoding="utf-8"))
            out.append({
                "task_id": m["task_id"], "instruction": m["instruction"],
                "status": m["status"], "created_at": m["created_at"],
                "current_version": m.get("current_version", 0),
                "source": (m.get("files", {}).get("source") or {}).get("name"),
                "recipe_id": m.get("recipe_id"),
                "task_type": m.get("task_type"),
                "profile_id": m.get("profile_id"),
            })
        except Exception:
            continue
    return out


def log(task: dict, step: str, message: str, ok: bool = True):
    task.setdefault("logs", []).append({"t": _now(), "step": step, "ok": ok, "msg": message})


def add_version(task: dict, outputs: dict, note: str) -> int:
    """把一次执行产物固化为一个可回滚版本。outputs: {name: path}。"""
    n = task.get("current_version", 0) + 1
    vdir = TASKS / task["task_id"] / "versions" / f"v{n}"
    vdir.mkdir(parents=True, exist_ok=True)
    saved = {}
    for name, path in outputs.items():
        p = Path(path)
        if p.exists():
            shutil.copy2(p, vdir / p.name)
            saved[name] = p.name
    task.setdefault("versions", []).append({
        "version": n, "note": note, "saved_at": _now(), "files": saved,
    })
    task["current_version"] = n
    return n


def rollback(task: dict, version: int) -> dict:
    """回滚：把指定版本的产物复制回任务根目录作为当前成果。"""
    vdir = TASKS / task["task_id"] / "versions" / f"v{version}"
    if not vdir.exists():
        raise FileNotFoundError(f"版本 v{version} 不存在")
    restored = {}
    for f in vdir.iterdir():
        dst = TASKS / task["task_id"] / f.name
        shutil.copy2(f, dst)
        restored[f.name] = str(dst)
    task["current_version"] = version
    log(task, "rollback", f"已回滚到版本 v{version}")
    return restored

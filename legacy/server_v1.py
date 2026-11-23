"""CAD 图纸智能修改工作台 · 后端服务（依据项目建设方案）。

零第三方依赖：Python 标准库 http.server。
六功能区 API：
- 文件上传区：POST /api/upload  (DWG/DXF + 参考图)
- 图纸预览区：GET  /api/drawing/{task}/info   图层/实体清单 + 预览
- 需求输入区：POST /api/plan     自然语言 + 参考图 -> 结构化修改计划
- 修改计划区：GET  /api/plan/{task}
- 执行状态区：POST /api/execute  白名单 CAD 操作 -> 修改 -> 质检
- 结果交付区：GET  /api/result/{task}  下载 DWG/DXF、报告、版本回滚

设计红线（方案第 8 章）：
- 原始文件只读，绝不覆盖；所有操作在副本。
- 关键尺寸/低置信度定位必须人工确认（plan_status=awaiting_confirmation）。
- 白名单操作 + 参数校验，禁止任意脚本。
- 重开测试未通过不得标记成功。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from dxf_parser import auto_parse  # noqa: E402
from quality_check import run_all, sha256  # noqa: E402

DATA = ROOT / "workspace"
TASKS = DATA / "tasks"
STATIC = ROOT
TASKS.mkdir(parents=True, exist_ok=True)

ALLOWED_OPS = {
    "update_text", "update_dimension", "move_entity", "scale_entity",
    "stretch_entity", "add_entity", "delete_entity", "set_layer",
}


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sanitize(obj):
    """递归剥离孤立代理字符，避免 JSON 序列化失败（CAD 字体映射常见）。"""
    if isinstance(obj, str):
        return "".join(c for c in obj if not (0xD800 <= ord(c) <= 0xDFFF))
    if isinstance(obj, dict):
        return {str(_sanitize(k)): _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def _dumps(obj) -> str:
    return json.dumps(_sanitize(obj), ensure_ascii=False, indent=2)


def task_dir(tid: str) -> Path:
    return TASKS / tid


def load_task(tid: str) -> dict | None:
    f = task_dir(tid) / "task.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def save_task(tid: str, t: dict) -> None:
    d = task_dir(tid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "task.json").write_text(_dumps(t), encoding="utf-8")


def detect_kind(path: Path) -> str:
    head = path.read_bytes()[:6]
    return "dwg" if head.startswith(b"AC") else ("dxf" if b"SECTION" in path.read_bytes()[:500] else "image")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默，避免刷屏

    # ---------- 响应工具 ----------
    def _send(self, code: int, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = _dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, obj)

    def _err(self, msg, code=400, extra=None):
        d = {"ok": False, "error": msg}
        if extra:
            d.update(extra)
        self._json(d, code)

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    # ---------- 路由 ----------
    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)

        if path == "/" or path == "/index.html" or path == "/工作台.html":
            return self._serve_file(ROOT / "工作台_v2.html", "text/html; charset=utf-8")
        if path == "/api/health":
            return self._json({"ok": True, "time": now(), "service": "CAD改图工作台"})

        # 示例：一键跑真实 TD50-40G 几何变形链路（用户提供的 DWG + 参考图）
        if path == "/api/demo/real":
            try:
                import sys
                sys.path.insert(0, str(ROOT / "src"))
                from real_dwg_modify_pipeline import run as baseline_run
                from geometry_modifier import apply as geo_apply
                # 1) 原始 DWG -> 真实基线 DXF（只重算，不写死旧成果）
                base = baseline_run(ROOT / "input" / "TD50-40G.dwg",
                                    "按参考图：H2=443, D2=294, 轴伸E+20")
                # 2) 在真实几何上执行三处变形
                geo = geo_apply(ROOT / "output" / "TD50-40G_真实基线_clean.dxf",
                                ROOT / "output" / "TD50-40G_几何修改.dxf")
                # 3) 生成对照图
                import subprocess
                subprocess.run([sys.executable, str(ROOT / "src" / "gen_compare.py")],
                               cwd=str(ROOT), check=False, capture_output=True)
                # 4) 若已安装 ODA File Converter，同步输出高保真 DWG
                try:
                    from dwg_export import export as dwg_export
                    dwg = dwg_export(ROOT / "output" / "TD50-40G_几何修改_最终.dxf",
                                     ROOT / "output" / "TD50-40G_几何修改_最终.dwg")
                except Exception as ex:
                    dwg = {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
                return self._json({"ok": geo.get("ok", False),
                                   "demo": {"geo": {k: v for k, v in geo.items() if k != "changes"},
                                            "baseline_ok": base.get("ok", False), "dwg": dwg}})
            except Exception as ex:
                return self._err(f"示例执行失败: {type(ex).__name__}: {ex}", 500)

        # 下载示例成果
        if path.startswith("/api/demo/file/"):
            kind = path.split("/api/demo/file/", 1)[1]
            files = {
                "dxf": (ROOT / "output" / "TD50-40G_几何修改_最终.dxf", "application/octet-stream"),
                "dwg": (ROOT / "output" / "TD50-40G_几何修改_最终.dwg", "application/octet-stream"),
                "before_svg": (ROOT / "output" / "对照_几何改前.svg", "image/svg+xml; charset=utf-8"),
                "after_svg": (ROOT / "output" / "对照_几何改后.svg", "image/svg+xml; charset=utf-8"),
                "report": (ROOT / "output" / "geometry_change_report.json", "application/json; charset=utf-8"),
                "reference": (ROOT / "input" / "TD50-40G_修改要求参考图.jpg", "image/jpeg"),
                "original": (ROOT / "input" / "TD50-40G.dwg", "application/octet-stream"),
            }
            if kind not in files:
                return self._err("未知示例文件", 404)
            f, ct = files[kind]
            if not f.exists():
                return self._err("文件不存在", 404)
            return self._serve_file(f, ct, download=f.name)

        # 任务列表
        if path == "/api/tasks":
            out = []
            for d in sorted(TASKS.iterdir(), reverse=True):
                t = load_task(d.name)
                if t:
                    out.append({
                        "task_id": d.name, "created_at": t.get("created_at"),
                        "source_file": t.get("source_file"), "status": t.get("status"),
                    })
            return self._json({"ok": True, "tasks": out})

        # 任务详情
        if path.startswith("/api/task/"):
            tid = unquote(path.split("/api/task/", 1)[1])
            t = load_task(tid)
            return self._json({"ok": True, "task": t}) if t else self._err("任务不存在", 404)

        # 图纸信息（图层/实体/尺寸图谱）
        if path.startswith("/api/drawing/"):
            rest = path.split("/api/drawing/", 1)[1]
            parts = rest.split("/")
            tid = unquote(parts[0])
            what = parts[1] if len(parts) > 1 else "info"
            t = load_task(tid)
            if not t:
                return self._err("任务不存在", 404)
            parsed_path = task_dir(tid) / "parsed.json"
            if not parsed_path.exists():
                return self._err("图纸尚未解析", 409)
            parsed = json.loads(parsed_path.read_text(encoding="utf-8"))
            if what == "info":
                return self._json({
                    "ok": True, "parser": parsed.get("parser"),
                    "counts": parsed.get("counts"), "layers": parsed.get("layers"),
                    "blocks": parsed.get("blocks"),
                    "size_index": parsed.get("size_index", {}),
                    "entities": parsed.get("entities", [])[:200],
                })
            if what == "preview":
                p = task_dir(tid) / "preview.svg"
                if p.exists():
                    return self._serve_file(p, "image/svg+xml; charset=utf-8")
                return self._err("预览未生成", 404)

        # 计划
        if path.startswith("/api/plan/"):
            tid = unquote(path.split("/api/plan/", 1)[1])
            t = load_task(tid)
            if not t:
                return self._err("任务不存在", 404)
            return self._json({"ok": True, "plan": t.get("plan")})

        # 结果交付
        if path.startswith("/api/result/"):
            rest = path.split("/api/result/", 1)[1]
            parts = rest.split("/")
            tid, kind = unquote(parts[0]), (parts[1] if len(parts) > 1 else "report")
            t = load_task(tid)
            if not t:
                return self._err("任务不存在", 404)
            d = task_dir(tid)
            files = {
                "report": (d / "report.json", "application/json; charset=utf-8"),
                "output": (d / t.get("output_name", "output.dxf"), "application/octet-stream"),
                "original": (d / t.get("source_name", "original.dwg"), "application/octet-stream"),
                "diff": (d / "diff.svg", "image/svg+xml; charset=utf-8"),
            }
            if kind not in files:
                return self._err("未知交付项", 404)
            f, ct = files[kind]
            if not f.exists():
                return self._err("文件不存在", 404)
            return self._serve_file(f, ct, download=f.name)

        return self._err("未找到接口", 404)

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        raw = self._read_body()

        # ---------- 文件上传 ----------
        if path == "/api/upload":
            try:
                body = json.loads(raw.decode("utf-8"))
                name = body["name"]
                data = body["data"]  # base64
                import base64
                content = base64.b64decode(data.split(",", 1)[-1])
                tid = uuid.uuid4().hex[:12]
                d = task_dir(tid)
                d.mkdir(parents=True, exist_ok=True)
                safe = Path(name).name
                f = d / safe
                f.write_bytes(content)
                kind = detect_kind(f)
                t = {
                    "task_id": tid, "created_at": now(), "status": "uploaded",
                    "source_file": str(f), "source_name": safe, "kind": kind,
                    "sha256": sha256(f), "size": len(content),
                    "reference_images": [],
                }
                save_task(tid, t)
                # 若是图纸，立刻解析建基线
                if kind in ("dwg", "dxf"):
                    try:
                        if kind == "dwg":
                            parsed, note = self._parse_dwg(tid, f)
                        else:
                            parsed = auto_parse(f)
                            note = None
                        (d / "parsed.json").write_text(
                            _dumps(parsed), encoding="utf-8")
                        t["parsed"] = True
                        t["parse_note"] = note
                        self._make_preview(tid, parsed)
                        save_task(tid, t)
                    except Exception as ex:
                        t["parse_error"] = f"{type(ex).__name__}: {ex}"
                        save_task(tid, t)
                return self._json({"ok": True, "task_id": tid, "kind": kind,
                                   "counts": (t.get("parsed") and json.loads((d / "parsed.json").read_text(encoding="utf-8")).get("counts")) or None,
                                   "parse_note": t.get("parse_note"), "parse_error": t.get("parse_error")})
            except Exception as ex:
                return self._err(f"上传失败: {type(ex).__name__}: {ex}", 500)

        # ---------- 参考图上传 ----------
        if path.startswith("/api/reference/"):
            tid = unquote(path.split("/api/reference/", 1)[1])
            t = load_task(tid)
            if not t:
                return self._err("任务不存在", 404)
            try:
                body = json.loads(raw.decode("utf-8"))
                name, data = body["name"], body["data"]
                import base64
                content = base64.b64decode(data.split(",", 1)[-1])
                f = task_dir(tid) / Path(name).name
                f.write_bytes(content)
                t.setdefault("reference_images", []).append(str(f))
                save_task(tid, t)
                return self._json({"ok": True, "reference": str(f)})
            except Exception as ex:
                return self._err(f"参考图上传失败: {ex}", 500)

        # ---------- 生成修改计划 ----------
        if path == "/api/plan":
            try:
                body = json.loads(raw.decode("utf-8"))
                tid = body["task_id"]
                text = body.get("text", "")
                t = load_task(tid)
                if not t:
                    return self._err("任务不存在", 404)
                plan = self._build_plan(tid, t, text)
                t["plan"] = plan
                t["status"] = "awaiting_confirmation" if any(
                    o.get("requires_confirmation") for o in plan["operations"]) else "planned"
                t["requirement_text"] = text
                save_task(tid, t)
                return self._json({"ok": True, "plan": plan, "status": t["status"]})
            except Exception as ex:
                return self._err(f"生成计划失败: {type(ex).__name__}: {ex}", 500)

        # ---------- 执行修改 ----------
        if path == "/api/execute":
            try:
                body = json.loads(raw.decode("utf-8"))
                tid = body["task_id"]
                t = load_task(tid)
                if not t:
                    return self._err("任务不存在", 404)
                plan = t.get("plan")
                if not plan:
                    return self._err("尚未生成修改计划", 409)
                if t["status"] == "awaiting_confirmation" and not body.get("confirmed"):
                    return self._json({"ok": False, "need_confirmation": True,
                                       "message": "计划含关键尺寸/低置信度操作，需人工确认后执行。",
                                       "plan": plan})
                result = self._execute(tid, t, plan)
                t["status"] = "executed" if result.get("quality", {}).get("overall_passed") else "failed"
                t["result"] = result
                save_task(tid, t)
                return self._json({"ok": True, "result": result, "status": t["status"]})
            except Exception as ex:
                return self._err(f"执行失败: {type(ex).__name__}: {ex}", 500)

        return self._err("未找到接口", 404)

    # ---------- 静态文件 ----------
    def _serve_file(self, f: Path, ctype: str, download: str | None = None):
        if not f.exists():
            return self._err("文件不存在", 404)
        headers = {}
        if download:
            from urllib.parse import quote
            headers["Content-Disposition"] = f'attachment; filename*=UTF-8\'\'{quote(download)}'
        self._send(200, f.read_bytes(), ctype, headers)

    # ---------- DWG 解析（SDK 可插拔） ----------
    def _parse_dwg(self, tid: str, f: Path):
        """DWG -> DXF（SDK）-> 清洗 -> 解析。返回 (parsed, note)。

        第三方 SDK 导出的 DXF 常含不合规实体（VISUALSTYLE 缺组码），
        必须先清洗才能被 ezdxf 正确解析（保留图层/块/尺寸图谱）。
        """
        import sys, os, stat
        from dwg_bridge import sdk_info, dwg_to_dxf, DWG_UNSUPPORTED
        info = sdk_info()
        if not info["available"]:
            raise DWG_UNSUPPORTED(
                "本机未安装 DWG 读写 SDK。已保留原图与需求；请在服务端安装 aspose-cad 或 ODA SDK 后再执行真实解析。")
        d = task_dir(tid)
        # 上传的文件带只读属性，SDK 打不开，需在任务目录建可写副本
        writable = d / (f.stem + "_可写.dwg")
        if writable.exists():
            os.chmod(writable, stat.S_IWRITE | stat.S_IREAD)
        import shutil
        shutil.copy2(f, writable)
        os.chmod(writable, stat.S_IWRITE | stat.S_IREAD)
        dxf = d / "baseline_raw.dxf"
        conv = dwg_to_dxf(writable, dxf)
        if not conv.get("ok"):
            raise RuntimeError(f"DWG 转 DXF 失败: {conv.get('error')}")
        # 清洗不合规 VISUALSTYLE 实体 + audit 无效键
        from dxf_cleaner import clean_dxf
        cleaned = d / "baseline.dxf"
        cr = clean_dxf(dxf, cleaned)
        import ezdxf
        doc = ezdxf.readfile(str(cleaned))
        doc.audit()
        doc.saveas(str(cleaned))
        parsed = auto_parse(cleaned)
        return parsed, (f"DWG 经 {info['sdk']} 导出 DXF 并清洗（移除 {cr['removed_visualstyles']} 个不合规实体）后解析；"
                        f"后端={parsed.get('parser')}；请人工复核字体/动态块")

    def _make_preview(self, tid: str, parsed: dict):
        """基于解析结果生成图纸预览 SVG。"""
        try:
            from preview_svg import make_preview
            svg = make_preview(parsed)
            (task_dir(tid) / "preview.svg").write_text(svg, encoding="utf-8")
        except Exception:
            pass

    # ---------- 需求 -> 计划 ----------
    def _build_plan(self, tid: str, t: dict, text: str) -> dict:
        from planner import build_plan
        parsed_path = task_dir(tid) / "parsed.json"
        parsed = json.loads(parsed_path.read_text(encoding="utf-8")) if parsed_path.exists() else {}
        return build_plan(text, parsed, tid, t.get("source_name", ""))

    # ---------- 执行白名单操作 ----------
    def _execute(self, tid: str, t: dict, plan: dict) -> dict:
        from executor import execute_plan
        d = task_dir(tid)
        src = d / t["source_name"]
        # 用解析后的 DXF 作为工作底稿（DWG 需先经 SDK 转 DXF）
        baseline = d / "baseline.dxf"
        work = baseline if baseline.exists() else (src if src.suffix.lower() == ".dxf" else None)
        if work is None:
            return {"ok": False, "error": "没有可编辑的 DXF 底稿；请先安装 DWG SDK 或上传 DXF。"}

        before = json.loads((d / "parsed.json").read_text(encoding="utf-8"))
        out_name = Path(t["source_name"]).stem + "_修改后.dxf"
        out = d / out_name
        exec_result = execute_plan(work, out, plan)
        if not exec_result.get("ok"):
            return exec_result

        after = auto_parse(out)
        (d / "parsed_after.json").write_text(_dumps(after), encoding="utf-8")
        quality = run_all(out, src, before, after, plan, engine="ezdxf")
        (d / "report.json").write_text(_dumps({
            "task_id": tid, "executed_at": now(), "plan": plan,
            "execution": exec_result, "quality": quality,
            "before_counts": before.get("counts"), "after_counts": after.get("counts"),
        }), encoding="utf-8")

        # 差异图
        try:
            from preview_svg import make_diff
            (d / "diff.svg").write_text(make_diff(before, after, plan), encoding="utf-8")
        except Exception:
            pass

        t["output_name"] = out_name
        return {"ok": True, "output_name": out_name, "execution": exec_result, "quality": quality}


def main():
    port = int(os.environ.get("PORT", "8900"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"CAD 改图工作台运行中: http://127.0.0.1:{port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()

"""统一运行时引导：路径、依赖解析、环境体检。

背景（踩过的坑）：
- app.py / engine.py 过去在把 ``src/`` 放进 ``sys.path`` 之前就 ``import ezdxf``，
  依赖的是"全局解释器里恰好装了 ezdxf"。换一台机器（或换 Python 版本）启动即崩。
- annotation_engine.py 反着来：先把 ``src/`` 从 sys.path 摘掉再去 import 全局包，
  在只带 vendored 依赖的机器上同样崩。

本模块只有一个动作：``bootstrap()``。规则：
1. 优先使用全局 site-packages（按 requirements.txt 正常安装的环境）；
2. 全局缺少核心 CAD 依赖时，整体回退到 ``src/`` 下的 vendored 副本，离线可跑；
3. 核心包（numpy / ezdxf / aspose）绝不混用来源，避免 ABI 不匹配。

调用约定：任何入口（app.py、server.py、cli.py、workbench/engine.py、
以及 src/ 下直接可执行的脚本）在 import 第三方包之前先调用本模块。
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
CORE_MODULES = ("numpy", "ezdxf", "aspose")

_bootstrapped = False
_dependency_source = "unknown"


def _findable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _findable_outside_src(name: str) -> bool:
    """判断某个包是否来自全局环境（忽略 src/ 这个 vendored 目录）。"""
    src = str(SRC)
    saved = list(sys.path)
    sys.path[:] = [entry for entry in sys.path
                   if not entry or Path(entry).resolve() != SRC.resolve()]
    try:
        return _findable(name)
    finally:
        sys.path[:] = saved


def _ensure_path(entry: Path, front: bool) -> None:
    text = str(entry)
    if text in sys.path:
        sys.path.remove(text)
    if front:
        sys.path.insert(0, text)
    else:
        sys.path.append(text)


def _resolve_dependency_source() -> str:
    forced = os.environ.get("CAD_VENDOR_DEPS", "").strip().lower()
    if forced in ("1", "true", "yes", "vendored"):
        return "vendored"
    if forced in ("0", "false", "no", "global"):
        return "global"
    missing = [name for name in CORE_MODULES if not _findable_outside_src(name)]
    return "vendored" if missing else "global"


def bootstrap(force: str | None = None) -> dict:
    """准备 sys.path 并验证核心依赖。重复调用无副作用。"""
    global _bootstrapped, _dependency_source
    if _bootstrapped and force is None:
        return diagnostics()

    source = force or _resolve_dependency_source()
    # 项目自身模块始终优先：src/ 与 workbench/。
    # vendored 依赖也放在 src/ 下，因此 vendored 模式下 src/ 必须排在最前。
    _ensure_path(ROOT, front=False)
    _ensure_path(ROOT / "workbench", front=False)
    _ensure_path(SRC, front=(source == "vendored"))

    if source == "vendored":
        for name in CORE_MODULES:
            if not _findable(name):
                raise RuntimeError(
                    f"vendored 依赖缺少 {name}：{SRC} 不完整。"
                    " 请按 requirements.txt 安装，或恢复 src/ 下的离线副本。"
                )
    else:
        missing = [name for name in CORE_MODULES if not _findable(name)]
        if missing:
            raise RuntimeError(
                f"全局环境缺少 {', '.join(missing)}。请执行 "
                "`python -m pip install -r requirements.txt`，"
                "或设置 CAD_VENDOR_DEPS=1 使用 src/ 下的离线依赖。"
            )

    import numpy  # noqa: F401
    import ezdxf  # noqa: F401

    # 字体元数据里常见 1970 年附近的时间戳，只是噪声，不影响渲染/几何。
    logging.getLogger("fontTools.ttLib.tables._h_e_a_d").setLevel(logging.ERROR)

    _dependency_source = source
    _bootstrapped = True
    return diagnostics()


def dependency_source() -> str:
    return _dependency_source


def _oda_path() -> str | None:
    try:
        from dwg_export import find_converter  # noqa: WPS433

        found = find_converter()
        return str(found) if found else None
    except Exception:
        return None


def _chrome_path() -> str | None:
    candidates = [
        Path(r"C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path(r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path(r"C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    return None


def llm_key() -> str:
    """兼容 DashScope 官方与项目早期使用的环境变量名。"""
    for name in ("DASHSCOPE_API_KEY", "ALIYUN_MAAS_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def diagnostics() -> dict:
    """返回可直接展示给用户的环境体检结果。"""
    versions = {}
    paths = {}
    for name in CORE_MODULES + ("fastapi", "uvicorn"):
        try:
            module = __import__(name)
            versions[name] = getattr(module, "__version__", "installed")
            paths[name] = str(Path(getattr(module, "__file__", "") or ""))
        except Exception as exc:  # noqa: BLE001
            versions[name] = None
            paths[name] = f"unavailable: {type(exc).__name__}"
    return {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "dependency_source": _dependency_source,
        "versions": versions,
        "paths": paths,
        "oda": _oda_path(),
        "renderer": "ezdxf/matplotlib",
        "chrome": _chrome_path(),
        "llm_configured": bool(llm_key()),
        "writable": os.access(ROOT, os.W_OK),
    }


if __name__ == "__main__":
    import json

    bootstrap()
    print(json.dumps(diagnostics(), ensure_ascii=False, indent=2))

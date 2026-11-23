"""轻量 DXF 解析器（纯标准库，断网可跑）。

DXF 本质是"组码-值"交替的文本格式。本模块解析 ENTITIES 段中的常见实体，
提取尺寸标注(DIMENSION)、文字(TEXT/MTEXT)、块参照(INSERT)、线/圆弧，
并汇总"图纸尺寸图谱"，供大模型把口语需求映射到具体几何对象。

设计取舍：
- 优先使用 ezdxf（若用户环境已安装），否则回退到本纯 Python 解析器。
- 只做"感知层"：解析出有什么、在哪里、属于哪个图层/块；不做几何变换。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


def _clean(s: str) -> str:
    """去掉 DXF 转义码（如 \\S, \\P, %%c 直径符号等）。"""
    s = re.sub(r"\\P", " ", s)
    s = re.sub(r"\\S[^;]*;", "", s)
    s = s.replace("%%c", "Ø").replace("%%d", "°").replace("%%p", "±")
    # 过滤孤立代理字符（CAD 字体映射常见），否则写 JSON 会抛 UnicodeEncodeError
    s = "".join(ch for ch in s if not (0xD800 <= ord(ch) <= 0xDFFF))
    return s.strip()


def parse_dxf(path: str | Path) -> dict[str, Any]:
    """解析 DXF 文件，返回结构化字典。"""
    path = Path(path)
    raw = path.read_text(encoding="utf-8", errors="ignore").splitlines()

    pairs: list[tuple[int, str]] = []
    for i in range(0, len(raw) - 1, 2):
        code_s, value = raw[i].strip(), raw[i + 1].strip()
        try:
            pairs.append((int(code_s), value))
        except ValueError:
            continue

    entities: list[dict[str, Any]] = []
    layers: set[str] = set()
    blocks: set[str] = set()
    in_entities = False
    cur: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal cur
        if cur is None:
            return
        etype = cur.get("type", "")
        if etype == "INSERT":
            blocks.add(cur.get("block_name", ""))
        if etype in ("DIMENSION", "TEXT", "MTEXT", "LINE", "ARC", "CIRCLE", "INSERT", "LWPOLYLINE"):
            entities.append(cur)
        cur = None

    for code, value in pairs:
        if code == 0:
            if value == "SECTION":
                continue
            if value == "ENDSEC":
                if in_entities:
                    flush()
                    in_entities = False
                continue
            flush()
            # 通过下一个 2 组码判断是否是 ENTITIES 段比较复杂；此处用段标记
            if value == "ENTITIES":
                in_entities = True
                continue
            if in_entities:
                cur = {"type": value}
            continue
        if not in_entities or cur is None:
            continue

        layer = None
        if code == 8:
            cur["layer"] = value
            layers.add(value)
        elif code == 1:
            cur.setdefault("text", _clean(value))
        elif code == 2:
            cur["block_name"] = value
        elif code == 10:
            cur.setdefault("x", float(value))
        elif code == 20:
            cur.setdefault("y", float(value))
        elif code == 11:
            cur.setdefault("x2", float(value))
        elif code == 21:
            cur.setdefault("y2", float(value))
        elif code == 40:
            cur.setdefault("height_or_radius", float(value))
        elif code == 42:
            cur.setdefault("measured", float(value))
        elif code == 70:
            cur.setdefault("flags", int(float(value)))

    # 有些 DXF 的 ENTITIES 段段名出现在 0=SECTION 之后的 2=ENTITIES，
    # 上面的循环已用 0=ENTITIES 简化处理；若未拿到实体，尝试宽松再解析。
    if not entities:
        entities = _fallback_parse(pairs)

    dimensions = [e for e in entities if e["type"] == "DIMENSION"]
    texts = [e for e in entities if e["type"] in ("TEXT", "MTEXT")]
    inserts = [e for e in entities if e["type"] == "INSERT"]

    # 尺寸图谱：把尺寸文字/实测值聚合，便于与知识卡目标值对照
    size_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in dimensions:
        label = d.get("text") or (f"{d['measured']:.3f}" if "measured" in d else "")
        if label:
            size_index[label].append({
                "type": "DIMENSION",
                "layer": d.get("layer", ""),
                "x": d.get("x"), "y": d.get("y"),
                "measured": d.get("measured"),
            })
    for t in texts:
        txt = t.get("text", "")
        if re.search(r"\d", txt):
            size_index[txt].append({
                "type": "TEXT",
                "layer": t.get("layer", ""),
                "x": t.get("x"), "y": t.get("y"),
            })

    return {
        "source_file": str(path),
        "parser": "pure-python-dxf",
        "counts": {
            "entities": len(entities),
            "dimensions": len(dimensions),
            "texts": len(texts),
            "inserts": len(inserts),
        },
        "layers": sorted(layers),
        "blocks": sorted(blocks),
        "size_index": dict(size_index),
        "entities": entities,
    }


def _fallback_parse(pairs: list[tuple[int, str]]) -> list[dict[str, Any]]:
    """宽松解析：不严格依赖段边界，抓住所有 0=实体类型 的记录。"""
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    wanted = {"DIMENSION", "TEXT", "MTEXT", "LINE", "ARC", "CIRCLE", "INSERT", "LWPOLYLINE"}
    for code, value in pairs:
        if code == 0:
            if cur and cur.get("type") in wanted:
                out.append(cur)
            cur = {"type": value} if value in wanted else None
            continue
        if cur is None:
            continue
        if code == 8:
            cur["layer"] = value
        elif code == 1:
            cur.setdefault("text", _clean(value))
        elif code == 2:
            cur["block_name"] = value
        elif code == 10:
            cur.setdefault("x", float(value))
        elif code == 20:
            cur.setdefault("y", float(value))
        elif code == 40:
            cur.setdefault("height_or_radius", float(value))
        elif code == 42:
            cur.setdefault("measured", float(value))
    if cur and cur.get("type") in wanted:
        out.append(cur)
    return out


def parse_with_ezdxf_if_available(path: str | Path) -> dict[str, Any] | None:
    """若安装 ezdxf 则用其解析（更完整），否则返回 None 走纯 Python 解析器。

    输出字段必须与 parse_dxf() 完全一致（尤其 size_index / counts 的细分），
    否则下游流水线与防错提示在切换到增强后端后会失效。
    """
    try:
        import ezdxf  # type: ignore
    except ImportError:
        return None
    try:
        doc = ezdxf.readfile(str(path))
    except Exception:
        # 第三方 SDK（如 aspose-cad）导出的 DXF 可能含非严格实体（如缺组码的 VISUALSTYLE），
        # 用 recover 容错读取，保证真实图纸能被解析（方案 4.1 兼容性要求）。
        try:
            from ezdxf import recover
            doc, _auditor = recover.readfile(str(path))
        except Exception:
            return None  # 回退到纯 Python 解析器
    msp = doc.modelspace()
    entities: list[dict[str, Any]] = []
    blocks: set[str] = set()
    wanted = ("DIMENSION", "TEXT", "MTEXT", "LINE", "ARC", "CIRCLE", "INSERT", "LWPOLYLINE")
    for e in msp:
        etype = e.dxftype()
        if etype not in wanted:
            continue
        item: dict[str, Any] = {"type": etype, "layer": e.dxf.layer}
        if etype == "DIMENSION":
            txt = str(getattr(e.dxf, "text", "") or "")
            item["text"] = _clean(txt)
            try:
                item["measured"] = float(getattr(e, "get_measurement", lambda: None)())
            except Exception:
                item["measured"] = None
            dp = e.dxf.defpoint
            item["x"], item["y"] = dp.x, dp.y
        elif etype in ("TEXT", "MTEXT"):
            item["text"] = _clean(e.dxf.text if etype == "TEXT" else e.text)
            item["x"], item["y"] = e.dxf.insert.x, e.dxf.insert.y
        elif etype == "INSERT":
            item["block_name"] = e.dxf.name
            blocks.add(e.dxf.name)
            item["x"], item["y"] = e.dxf.insert.x, e.dxf.insert.y
        entities.append(item)

    dimensions = [e for e in entities if e["type"] == "DIMENSION"]
    texts = [e for e in entities if e["type"] in ("TEXT", "MTEXT")]
    inserts = [e for e in entities if e["type"] == "INSERT"]

    # 与纯 Python 版同构的尺寸图谱
    size_index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in dimensions:
        label = d.get("text") or (f"{d['measured']:.3f}" if d.get("measured") is not None else "")
        if label:
            size_index[label].append({
                "type": "DIMENSION", "layer": d.get("layer", ""),
                "x": d.get("x"), "y": d.get("y"), "measured": d.get("measured"),
            })
    for t in texts:
        txt = t.get("text", "")
        if re.search(r"\d", txt):
            size_index[txt].append({
                "type": "TEXT", "layer": t.get("layer", ""),
                "x": t.get("x"), "y": t.get("y"),
            })

    return {
        "source_file": str(path), "parser": "ezdxf", "entities": entities,
        "counts": {
            "entities": len(entities), "dimensions": len(dimensions),
            "texts": len(texts), "inserts": len(inserts),
        },
        "layers": sorted(layer.dxf.name for layer in doc.layers),
        "blocks": sorted(blocks),
        "size_index": dict(size_index),
    }


def auto_parse(path: str | Path) -> dict[str, Any]:
    """自动选择解析器：优先 ezdxf，回退纯 Python。"""
    result = parse_with_ezdxf_if_available(path)
    return result if result is not None else parse_dxf(path)


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "../tests/sample.dxf"
    data = auto_parse(target)
    print(json.dumps(data, ensure_ascii=False, indent=2)[:5000])

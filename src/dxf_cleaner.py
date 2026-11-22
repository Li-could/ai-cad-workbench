"""清洗第三方 SDK 导出的 DXF 中不合规实体，使其能被 ezdxf 严格解析。

已知问题：aspose-cad 导出会生成若干"空的 VISUALSTYLE 实体"（缺组码291），
ezdxf 的 VisualStyle.load_dxf_attribs 会抛 DXFValueError: 291。
这些实体仅存于 OBJECTS 段且不含实际图形信息，可安全移除。

不改变真实图形实体（LINE/CIRCLE/TEXT/DIMENSION/INSERT/LWPOLYLINE 等）。
"""
from __future__ import annotations

from pathlib import Path


def clean_dxf(src: str | Path, dst: str | Path) -> dict:
    src, dst = Path(src), Path(dst)
    lines = src.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
    out: list[str] = []
    removed = 0
    i = 0
    n = len(lines)
    # 组码-值成对；实体以 0\nTYPE 开始，到下一个 0\n 结束
    while i < n:
        code = lines[i].strip()
        val = lines[i + 1].strip() if i + 1 < n else ""
        if code == "0" and val == "VISUALSTYLE":
            # 收集整个实体块，检查是否缺 291 组码（不合规）
            j = i + 2
            block = [lines[i], lines[i + 1]]
            has_291 = False
            while j + 1 < n and lines[j].strip() != "0":
                block.append(lines[j]); block.append(lines[j + 1])
                if lines[j].strip() == "291":
                    has_291 = True
                j += 2
            if not has_291:
                removed += 1
                i = j
                continue
            else:
                out.extend(block)
                i = j
                continue
        out.append(lines[i])
        out.append(lines[i + 1] if i + 1 < n else "")
        i += 2
    dst.write_text("".join(out), encoding="utf-8")
    return {"src": str(src), "dst": str(dst), "removed_visualstyles": removed,
            "original_pairs": n // 2, "output_pairs": len(out) // 2}


if __name__ == "__main__":
    import sys, json
    print(json.dumps(clean_dxf(sys.argv[1], sys.argv[2]), ensure_ascii=False, indent=2))

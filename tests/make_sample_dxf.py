"""生成一个含三处变更尺寸的测试 DXF（纯标准库手写 DXF R12）。

用途：在没有真实 DXF / 转换工具时，验证解析链路是否可用。
包含：H2=443、D2=294（及干扰项297）、轴伸 E+20 三个尺寸标注，
以及罩壳/接线盒/轴等图层示意线。
"""
from pathlib import Path

OUT = Path(__file__).resolve().parent / "sample.dxf"


def pair(code: int, value: str) -> str:
    return f"{code}\n{value}\n"


def build() -> str:
    s = ""
    s += pair(0, "SECTION") + pair(2, "HEADER") + pair(0, "ENDSEC")

    s += pair(0, "SECTION") + pair(2, "ENTITIES")
    # 罩壳侧壁示意线
    s += pair(0, "LINE") + pair(8, "罩壳") + pair(10, "0") + pair(20, "0") + pair(11, "0") + pair(21, "443")
    # 接线盒外盖示意线
    s += pair(0, "LINE") + pair(8, "接线盒外盖") + pair(10, "50") + pair(20, "300") + pair(11, "294") + pair(21, "300")
    # 轴段示意线
    s += pair(0, "LINE") + pair(8, "轴") + pair(10, "100") + pair(20, "100") + pair(11, "120") + pair(21, "100")

    # 尺寸标注：H2
    s += pair(0, "DIMENSION") + pair(8, "尺寸") + pair(2, "*D1") + pair(10, "0") + pair(20, "0") + pair(42, "443") + pair(1, "H2=443")
    # 尺寸标注：D2
    s += pair(0, "DIMENSION") + pair(8, "尺寸") + pair(2, "*D2") + pair(10, "0") + pair(20, "300") + pair(42, "294") + pair(1, "D2=294")
    # 干扰项：297（图上原数字，非D2）
    s += pair(0, "TEXT") + pair(8, "尺寸") + pair(10, "260") + pair(20, "320") + pair(1, "297")
    # 轴伸文字 E+20
    s += pair(0, "TEXT") + pair(8, "尺寸") + pair(10, "110") + pair(20, "90") + pair(1, "E+20")

    # 块参照示意
    s += pair(0, "INSERT") + pair(8, "风罩") + pair(2, "FAN_COVER") + pair(10, "0") + pair(20, "0")
    s += pair(0, "INSERT") + pair(8, "端子") + pair(2, "TERMINAL_BOX") + pair(10, "50") + pair(20, "300")
    s += pair(0, "ENDSEC")
    s += pair(0, "EOF")
    return s


if __name__ == "__main__":
    OUT.write_text(build(), encoding="utf-8")
    print(f"生成测试 DXF: {OUT}")

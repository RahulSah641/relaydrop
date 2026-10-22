"""Dependency-free Code 128 barcode (code sets B and C) with SVG / PNG output."""
from __future__ import annotations

from .qr import png_from_grid

_PATTERNS = (
    "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 221312 231212 112232 122132 122231 113222 "
    "123122 123221 223211 221132 221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 212123 212321 "
    "232121 111323 131123 131321 112313 132113 132311 211313 231113 231311 112133 112331 132131 113123 113321 133121 "
    "313121 211331 231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 314111 221411 431111 111224 "
    "111422 121124 121421 141122 141221 112214 112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 "
    "111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 214121 412121 111143 111341 131141 114113 "
    "114311 411113 411311 113141 114131 311141 411131 211412 211214 211232 2331112"
).split()

START_B, START_C, STOP = 104, 105, 106


def encode(text: str) -> list[int]:
    """Return Code 128 symbol values (start, data, checksum, stop)."""
    if not text:
        raise ValueError("Nothing to encode")
    if text.isdigit() and len(text) % 2 == 0:
        values = [START_C] + [int(text[i:i + 2]) for i in range(0, len(text), 2)]
    else:
        if any(not 32 <= ord(c) <= 126 for c in text):
            raise ValueError("Only printable ASCII can be encoded")
        values = [START_B] + [ord(c) - 32 for c in text]
    checksum = (values[0] + sum(v * i for i, v in enumerate(values[1:], 1))) % 103
    return values + [checksum, STOP]


def modules(text: str) -> list[bool]:
    bars: list[bool] = []
    for value in encode(text):
        dark = True
        for width in _PATTERNS[value]:
            bars.extend([dark] * int(width))
            dark = not dark
    return bars


def to_svg(text: str, module: int = 2, height: int = 44, margin: int = 10, color: str = "#172c2b") -> str:
    bars = modules(text)
    width = len(bars) * module + margin * 2
    total_h = height + margin * 2
    rects, x = [], 0
    while x < len(bars):
        if bars[x]:
            start = x
            while x < len(bars) and bars[x]:
                x += 1
            rects.append(f"M{margin + start * module},{margin}h{(x - start) * module}v{height}h-{(x - start) * module}z")
        else:
            x += 1
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{total_h}" viewBox="0 0 {width} {total_h}" shape-rendering="crispEdges">'
        f'<rect width="100%" height="100%" fill="#ffffff"/><path d="{"".join(rects)}" fill="{color}"/></svg>'
    )


def to_png(text: str, module: int = 2, height: int = 44, margin: int = 10, color: str = "#172c2b") -> bytes:
    bars = modules(text)
    row = [False] * margin + [b for b in bars for _ in range(module)] + [False] * margin
    blank = [False] * len(row)
    bar_row = row
    grid = [blank] * margin + [bar_row] * height + [blank] * margin
    return png_from_grid(grid, 1, 1, color, "#ffffff")

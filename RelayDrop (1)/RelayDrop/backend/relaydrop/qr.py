"""Dependency-free QR Code generator (byte mode, ECC level L/M/Q/H, versions 1-40).

Follows ISO/IEC 18004 and the well-known reference layout (Project Nayuki's design,
re-implemented here). Produces a boolean module matrix plus SVG / PNG renderers.
"""
from __future__ import annotations

import struct
import zlib

_ECC_FORMAT_BITS = {"L": 1, "M": 0, "Q": 3, "H": 2}
_ECC_INDEX = {"L": 0, "M": 1, "Q": 2, "H": 3}

_ECC_CODEWORDS_PER_BLOCK = (
    (None, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    (None, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (None, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
)
_NUM_ERROR_CORRECTION_BLOCKS = (
    (None, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    (None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    (None, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    (None, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
)


def _raw_data_modules(ver: int) -> int:
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver: int, ecc: str) -> int:
    i = _ECC_INDEX[ecc]
    return _raw_data_modules(ver) // 8 - _ECC_CODEWORDS_PER_BLOCK[i][ver] * _NUM_ERROR_CORRECTION_BLOCKS[i][ver]


def _rs_multiply(x: int, y: int) -> int:
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _rs_multiply(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _rs_multiply(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _rs_multiply(coef, factor)
    return result


class QrCode:
    def __init__(self, text: str, ecc: str = "M") -> None:
        data = text.encode("utf-8")
        ecc = ecc.upper()
        if ecc not in _ECC_INDEX:
            raise ValueError("Unknown error-correction level")
        version = 0
        for ver in range(1, 41):
            cc_bits = 8 if ver <= 9 else 16
            needed = 4 + cc_bits + len(data) * 8
            if needed <= _data_codewords(ver, ecc) * 8:
                version = ver
                break
        if not version:
            raise ValueError("Data too long for a QR code")
        self.version = version
        self.size = version * 4 + 17
        self.ecc = ecc

        bits: list[int] = []

        def put(val: int, n: int) -> None:
            bits.extend((val >> i) & 1 for i in range(n - 1, -1, -1))

        put(0b0100, 4)
        put(len(data), 8 if version <= 9 else 16)
        for b in data:
            put(b, 8)
        capacity = _data_codewords(version, ecc) * 8
        put(0, min(4, capacity - len(bits)))
        put(0, -len(bits) % 8)
        pad = 0xEC
        while len(bits) < capacity:
            put(pad, 8)
            pad ^= 0xEC ^ 0x11
        codewords = [sum(bit << (7 - j) for j, bit in enumerate(bits[i:i + 8])) for i in range(0, len(bits), 8)]
        full = self._add_ecc_and_interleave(codewords)

        self.modules = [[False] * self.size for _ in range(self.size)]
        self._function = [[False] * self.size for _ in range(self.size)]
        self._draw_function_patterns()
        self._draw_codewords(full)

        best_mask, best_penalty = 0, None
        for mask in range(8):
            self._apply_mask(mask)
            self._draw_format_bits(mask)
            penalty = self._penalty()
            if best_penalty is None or penalty < best_penalty:
                best_mask, best_penalty = mask, penalty
            self._apply_mask(mask)  # undo (XOR)
        self._apply_mask(best_mask)
        self._draw_format_bits(best_mask)
        self.mask = best_mask
        del self._function

    # -- encoding helpers -------------------------------------------------
    def _add_ecc_and_interleave(self, data: list[int]) -> list[int]:
        ver, i = self.version, _ECC_INDEX[self.ecc]
        num_blocks = _NUM_ERROR_CORRECTION_BLOCKS[i][ver]
        block_ecc_len = _ECC_CODEWORDS_PER_BLOCK[i][ver]
        raw_codewords = _raw_data_modules(ver) // 8
        num_short = num_blocks - raw_codewords % num_blocks
        short_len = raw_codewords // num_blocks
        divisor = _rs_divisor(block_ecc_len)
        blocks, k = [], 0
        for n in range(num_blocks):
            dat = data[k:k + short_len - block_ecc_len + (0 if n < num_short else 1)]
            k += len(dat)
            ecc = _rs_remainder(dat, divisor)
            if n < num_short:
                dat = dat + [0]
            blocks.append(dat + ecc)
        result = []
        for pos in range(len(blocks[0])):
            for j, blk in enumerate(blocks):
                if pos != short_len - block_ecc_len or j >= num_short:
                    result.append(blk[pos])
        return result

    # -- drawing ----------------------------------------------------------
    def _set(self, x: int, y: int, dark: bool) -> None:
        self.modules[y][x] = dark
        self._function[y][x] = True

    def _alignment_positions(self) -> list[int]:
        ver = self.version
        if ver == 1:
            return []
        n = ver // 7 + 2
        step = 26 if ver == 32 else (ver * 4 + n * 2 + 1) // (n * 2 - 2) * 2
        result = [self.size - 7 - i * step for i in range(n - 1)] + [6]
        return result[::-1]

    def _draw_function_patterns(self) -> None:
        size = self.size
        for i in range(size):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        dist = max(abs(dx), abs(dy))
                        self._set(x, y, dist not in (2, 4))
        pos = self._alignment_positions()
        n = len(pos)
        for i in range(n):
            for j in range(n):
                if (i == 0 and j == 0) or (i == 0 and j == n - 1) or (i == n - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(pos[i] + dx, pos[j] + dy, max(abs(dx), abs(dy)) != 1)
        self._draw_format_bits(0)
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.version << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = size - 11 + i % 3, i // 3
                self._set(a, b, bit)
                self._set(b, a, bit)

    def _draw_format_bits(self, mask: int) -> None:
        size = self.size
        data = _ECC_FORMAT_BITS[self.ecc] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412

        def bit(i: int) -> bool:
            return (bits >> i) & 1 == 1

        for i in range(0, 6):
            self._set(8, i, bit(i))
        self._set(8, 7, bit(6))
        self._set(8, 8, bit(7))
        self._set(7, 8, bit(8))
        for i in range(9, 15):
            self._set(14 - i, 8, bit(i))
        for i in range(0, 8):
            self._set(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self._set(8, size - 15 + i, bit(i))
        self._set(8, size - 8, True)

    def _draw_codewords(self, data: list[int]) -> None:
        size, i = self.size, 0
        for right in range(size - 1, 0, -2):
            if right <= 6:
                right -= 1
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = (right + 1) & 2 == 0
                    y = size - 1 - vert if upward else vert
                    if not self._function[y][x] and i < len(data) * 8:
                        self.modules[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1

    def _apply_mask(self, mask: int) -> None:
        for y in range(self.size):
            for x in range(self.size):
                if mask == 0: inv = (x + y) % 2 == 0
                elif mask == 1: inv = y % 2 == 0
                elif mask == 2: inv = x % 3 == 0
                elif mask == 3: inv = (x + y) % 3 == 0
                elif mask == 4: inv = (x // 3 + y // 2) % 2 == 0
                elif mask == 5: inv = x * y % 2 + x * y % 3 == 0
                elif mask == 6: inv = (x * y % 2 + x * y % 3) % 2 == 0
                else: inv = ((x + y) % 2 + x * y % 3) % 2 == 0
                if inv and not self._function[y][x]:
                    self.modules[y][x] = not self.modules[y][x]

    def _penalty(self) -> int:
        m, n, score = self.modules, self.size, 0
        lines = [row for row in m] + [[m[y][x] for y in range(n)] for x in range(n)]
        pat_a = [True, False, True, True, True, False, True, False, False, False, False]
        pat_b = pat_a[::-1]
        for line in lines:
            run = 1
            for k in range(1, n):
                if line[k] == line[k - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run = 1
            if run >= 5:
                score += 3 + run - 5
            for k in range(n - 10):
                seg = line[k:k + 11]
                if seg == pat_a or seg == pat_b:
                    score += 40
        for y in range(n - 1):
            for x in range(n - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(row) for row in m)
        total = n * n
        k = -(-abs(dark * 20 - total * 10) // total) - 1
        return score + max(k, 0) * 10

    # -- output -----------------------------------------------------------
    def to_svg(self, border: int = 2, dark: str = "#142b2a", light: str = "#ffffff") -> str:
        dim = self.size + border * 2
        parts = []
        for y, row in enumerate(self.modules):
            for x, v in enumerate(row):
                if v:
                    parts.append(f"M{x + border},{y + border}h1v1h-1z")
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" shape-rendering="crispEdges">'
            f'<rect width="100%" height="100%" fill="{light}"/><path d="{"".join(parts)}" fill="{dark}"/></svg>'
        )

    def to_png(self, pixels: int = 280, border: int = 2, dark: str = "#142b2a", light: str = "#ffffff") -> bytes:
        dim = self.size + border * 2
        scale = max(1, pixels // dim)
        grid = [[False] * dim for _ in range(dim)]
        for y, row in enumerate(self.modules):
            for x, v in enumerate(row):
                grid[y + border][x + border] = v
        return png_from_grid(grid, scale, scale, dark, light)


def _rgb(color: str) -> bytes:
    color = color.lstrip("#")
    return bytes(int(color[i:i + 2], 16) for i in (0, 2, 4))


def png_from_grid(grid: list[list[bool]], sx: int, sy: int, dark: str, light: str) -> bytes:
    """Encode a boolean grid as an RGB PNG, each cell sx by sy pixels."""
    d, l = _rgb(dark), _rgb(light)
    raw = bytearray()
    for row in grid:
        line = bytearray([0])
        for v in row:
            line += (d if v else l) * sx
        raw += bytes(line) * sy
    width, height = len(grid[0]) * sx, len(grid) * sy

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )

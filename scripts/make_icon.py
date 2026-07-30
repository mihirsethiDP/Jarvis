"""Generate the Jarvis app icon (256x256 ICO) with no image library.

An ICO may carry a PNG payload verbatim (Vista and later), and a PNG is
straightforward to emit with zlib alone — so the shortcut gets a real icon
without adding Pillow as a dependency for one build-time file.

    python scripts/make_icon.py
"""

from __future__ import annotations

import math
import pathlib
import struct
import zlib

SIZE = 256
OUT = pathlib.Path(__file__).resolve().parent.parent / "jarvis" / "ui" / "static" / "jarvis.ico"

# Same palette as the idle orb on the status page.
CORE = (255, 255, 255)
MID = (56, 189, 248)
DEEP = (12, 40, 110)


def _mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _pixel(x, y):
    """Radial orb: white core, cyan body, deep blue edge, soft glow outside."""
    cx = cy = (SIZE - 1) / 2
    d = math.hypot(x - cx, y - cy) / (SIZE / 2)

    if d <= 0.62:                       # the sphere itself
        t = d / 0.62
        # Light it from the upper left so it reads as a ball, not a disc.
        shade = math.hypot(x - cx * 0.72, y - cy * 0.72) / (SIZE / 2)
        colour = _mix(_mix(CORE, MID, min(shade / 0.55, 1.0)), DEEP, t ** 1.7)
        return (*colour, 255)
    if d <= 0.98:                       # bloom falling off to nothing
        t = (d - 0.62) / 0.36
        return (*MID, round(150 * (1 - t) ** 2.2))
    return (0, 0, 0, 0)


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def build_png() -> bytes:
    raw = bytearray()
    for y in range(SIZE):
        raw.append(0)                   # filter type 0 (None) for this scanline
        for x in range(SIZE):
            raw.extend(_pixel(x, y))
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


def build_ico(png: bytes) -> bytes:
    # 0 in the width/height byte means 256 — the format has no other way to
    # express it.
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
    return header + entry + png


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(build_ico(build_png()))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")

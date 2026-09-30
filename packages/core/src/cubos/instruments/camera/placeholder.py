"""Stdlib-only placeholder PNG for offline camera captures, so dry runs
produce real files without numpy/OpenCV/PySpin installed."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path


def write_placeholder_png(
    path: str | Path,
    width: int = 320,
    height: int = 240,
) -> Path:
    """Write a solid mid-gray PNG with a white border to *path*."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter type: none
        for x in range(width):
            border = y < 4 or y >= height - 4 or x < 4 or x >= width - 4
            value = 255 if border else 96
            rows.extend((value, value, value))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(rows)))
        + chunk(b"IEND", b"")
    )
    target.write_bytes(png)
    return target

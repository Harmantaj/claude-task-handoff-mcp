"""Generate bundle/icon.png (512x512): a check mark on a rounded square. Stdlib only."""

import math
import struct
import sys
import zlib

N = 512
BG, FG = (37, 99, 235), (255, 255, 255)


def inside_rounded(x, y, r=96):
    cx = min(max(x, r), N - 1 - r)
    cy = min(max(y, r), N - 1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def dist_to_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


rows = []
for y in range(N):
    row = bytearray([0])
    for x in range(N):
        if not inside_rounded(x, y):
            row += bytes((0, 0, 0, 0))
            continue
        d = min(dist_to_segment(x, y, 128, 264, 216, 356), dist_to_segment(x, y, 216, 356, 388, 168))
        a = max(0.0, min(1.0, 26 - d))  # anti-aliased stroke
        row += bytes(round(BG[i] * (1 - a) + FG[i] * a) for i in range(3)) + b"\xff"
    rows.append(bytes(row))


def chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", N, N, 8, 6, 0, 0, 0)) \
    + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")
open(sys.argv[1] if len(sys.argv) > 1 else "bundle/icon.png", "wb").write(png)

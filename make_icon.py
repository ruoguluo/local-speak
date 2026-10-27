"""从同一套几何参数生成 favicon.ico（内嵌 PNG，16/32/48），与 favicon.svg 保持一致。"""

import struct
import zlib
from pathlib import Path

ACCENT = (0x18, 0x5F, 0xA5)
BARS = [(8, 13, 3, 6), (14.5, 9, 3, 14), (21, 11.5, 3, 9)]  # x, y, w, h（32 单位坐标系）
RADIUS = 7.0


def in_rrect(x, y):
    if not (0 <= x <= 32 and 0 <= y <= 32):
        return False
    cx = min(max(x, RADIUS), 32 - RADIUS)
    cy = min(max(y, RADIUS), 32 - RADIUS)
    return (x - cx) ** 2 + (y - cy) ** 2 <= RADIUS ** 2


def in_bar(x, y, bx, by, bw, bh):
    r = bw / 2
    cy = min(max(y, by + r), by + bh - r)
    return (x - (bx + r)) ** 2 + (y - cy) ** 2 <= r * r


def render(size, ss=4):
    unit = 32.0 / size
    rows = []
    for py in range(size):
        row = []
        for px in range(size):
            hit_bar = hit_bg = 0
            for sy in range(ss):
                for sx in range(ss):
                    x = (px + (sx + 0.5) / ss) * unit
                    y = (py + (sy + 0.5) / ss) * unit
                    if any(in_bar(x, y, *b) for b in BARS):
                        hit_bar += 1
                    elif in_rrect(x, y):
                        hit_bg += 1
            total = ss * ss
            if hit_bar or hit_bg:
                alpha = (hit_bar + hit_bg) / total
                if hit_bar >= hit_bg:
                    rgb = (255, 255, 255)
                else:
                    rgb = ACCENT
                row.append((*rgb, int(round(alpha * 255))))
            else:
                row.append((0, 0, 0, 0))
        rows.append(row)
    return rows


def png(size, rows):
    raw = b"".join(b"\x00" + b"".join(struct.pack("BBBB", *px) for px in row) for row in rows)

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def ico(images):
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset
        )
        offset += len(data)
        blobs += data
    return header + entries + blobs


if __name__ == "__main__":
    images = [(s, png(s, render(s))) for s in (16, 32, 48)]
    out = Path(__file__).parent / "web" / "favicon.ico"
    out.write_bytes(ico(images))
    print(f"wrote {out} ({out.stat().st_size} bytes, sizes 16/32/48)")

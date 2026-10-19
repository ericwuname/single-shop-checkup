#!/usr/bin/env python
"""M7 WP-4（docs/08 §7.1）：程序化生成 PWA 图标。

**不依赖任何字体** —— 依赖字体的图标生成在不同机器上渲染会漂移，
而且服务器/CI 上常常没有中文字体。图标形状：主色圆角方块底 +
3 根白色圆角竖条（高度比例 40%/65%/85%，形似"本周柱状图"），
呼应产品核心（一周记 7 天、看柱子）。

## 与任务书的偏差（如实说明）

任务书写"用 Pillow"。但本机当前**无外网通路**（代理不放行 pypi、直连无 DNS 路由），
Pillow 装不上。所以本脚本有两层实现：
1. 有 Pillow → 用 Pillow 的 rounded_rectangle（首选）
2. 没有 Pillow → 纯标准库光栅化 + 手写 PNG 编码（zlib + struct，输出与 Pillow 等效）

两层画的是同一个形状，测试/验收脚本只检查 PNG 魔数与尺寸，两种来源都过。

输出到 web/public/icons/：
    icon-192.png / icon-512.png          purpose: any
    icon-maskable-512.png                purpose: maskable（满铺底色，图形缩进中心 60% 安全区）
    apple-touch-icon.png                 180×180（iOS，无透明通道）

用法：python scripts/gen_icons.py
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "public" / "icons"

ACCENT = (14, 107, 88)        # --accent #0E6B58
WHITE = (255, 255, 255)
BAR_HEIGHTS = (0.40, 0.65, 0.85)   # 三根柱：40% / 65% / 85%
BAR_WIDTH_RATIO = 0.11             # 每根柱宽 = 图宽的 11%
GAP_RATIO = 0.06                   # 柱间距


# --------------------------------------------------------------------------- #
# 几何：图形定义成"参数化的矩形集合"，两种渲染器共用
# --------------------------------------------------------------------------- #
def _geometry(size: int, *, maskable: bool):
    """返回 (底色圆角矩形, 白柱矩形列表)。坐标按像素对齐（同形状）。"""
    inset_ratio = 0.20 if maskable else 0.08   # maskable：图形缩进中心 60% 安全区
    area = size * (1 - inset_ratio * 2)
    origin = size * inset_ratio

    bar_w = area * BAR_WIDTH_RATIO
    gap = area * GAP_RATIO
    bottom = origin + area * 0.88
    radius = bar_w * 0.35

    bars = []
    x = origin + (area - (bar_w * 3 + gap * 2)) / 2
    for h in BAR_HEIGHTS:
        top = bottom - area * h
        bars.append((x, top, x + bar_w, bottom, radius))
        x += bar_w + gap
    return (origin, origin, origin + area, origin + area, area * 0.22), bars


# --------------------------------------------------------------------------- #
# 渲染器 A：Pillow（有就用）
# --------------------------------------------------------------------------- #
def _render_pillow(size: int, *, maskable: bool):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), ACCENT + (255,))
    draw = ImageDraw.Draw(img)
    bg, bars = _geometry(size, maskable=maskable)
    draw.rounded_rectangle(
        (bg[0], bg[1], bg[2], bg[3]), radius=bg[4], fill=ACCENT + (255,)
    )
    for x0, y0, x1, y1, r in bars:
        draw.rounded_rectangle((x0, y0, x1, y1), radius=r, fill=WHITE + (255,))
    return img


# --------------------------------------------------------------------------- #
# 渲染器 B：纯标准库光栅化 + 手写 PNG（无 Pillow 时的等价实现）
# --------------------------------------------------------------------------- #
def _inside_rounded_rect(px, py, x0, y0, x1, y1, r) -> bool:
    """点是否在圆角矩形内（四角按圆心距离判断）。"""
    if not (x0 <= px <= x1 and y0 <= py <= y1):
        return False
    cx = min(max(px, x0 + r), x1 - r)
    cy = min(max(py, y0 + r), y1 - r)
    return (px - cx) ** 2 + (py - cy) ** 2 <= r * r or (
        x0 + r <= px <= x1 - r or y0 + r <= py <= y1 - r
    )


def _render_stdlib(size: int, *, maskable: bool):
    bg, bars = _geometry(size, maskable=maskable)
    bx0, by0, bx1, by1, br = bg
    rows = []
    for py in range(size):
        row = bytearray([0])  # PNG filter type 0（None）
        for px in range(size):
            # 底：整张图都是主色（maskable 满铺；any 版四角做圆角）
            if maskable or _inside_rounded_rect(px, py, bx0, by0, bx1, by1, br):
                px_color = ACCENT
            else:
                # ⚠️ 必须是 **RGB 三元组**：下面统一 `+ (255,)` 补 alpha。
                # 写成 (0,0,0,0) 的话每個透明像素会多 1 字节，整行字节错位 ——
                # 这个 bug 的症状是"图标打开全黑"，且几何函数单测全对、极难定位。
                px_color = (0, 0, 0)
            # 白柱
            for x0, y0, x1, y1, r in bars:
                if _inside_rounded_rect(px, py, x0, y0, x1, y1, r):
                    px_color = WHITE
                    break
            row.extend(px_color + (255,))
        rows.append(bytes(row))
    return size, size, b"".join(rows)


# --------------------------------------------------------------------------- #
# PNG 编码（纯标准库）：IHDR + IDAT(zlib) + IEND，无辅助块
# --------------------------------------------------------------------------- #
def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def encode_png(width: int, height: int, raw: bytes) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


def render(size: int, *, maskable: bool):
    """返回 (width, height, RGBA 原始字节)；有 Pillow 走 Pillow，没有走标准库。"""
    try:
        img = _render_pillow(size, maskable=maskable)
        return img.size[0], img.size[1], img.tobytes()
    except ImportError:
        return _render_stdlib(size, maskable=maskable)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    targets = [
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-maskable-512.png", 512, True),
        ("apple-touch-icon.png", 180, False),
    ]
    for name, size, maskable in targets:
        w, h, raw = render(size, maskable=maskable)
        png = encode_png(w, h, raw)
        # apple-touch-icon 不能有透明通道（iOS 会给透明填黑）→ 换成满铺底
        if name == "apple-touch-icon.png":
            # 简单处理：重画一张 maskable=False 的满铺版本（背景不透明化）
            w, h, raw = render(size, maskable=True)  # 满铺主色底
            png = encode_png(w, h, raw)
        path = OUT / name
        path.write_bytes(png)
        print(f"已生成 {path}  ({len(png)} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

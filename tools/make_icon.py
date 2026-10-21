"""生成应用图标：深色圆角底 + 3D 打印机轮廓 + 青绿强调色。

单独一个脚本而不是塞进主程序，是因为图标是打包期的一次性产物，
运行时不该依赖它。改设计时重跑：python tools/make_icon.py out.ico
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

# 与 static/style.css 的调色板对齐，避免图标和界面两种绿
BG_TOP = (22, 38, 43)
BG_BOT = (12, 15, 20)
ACCENT = (93, 202, 165)
ACCENT_DIM = (15, 110, 86)
BLUE = (55, 138, 221)
PLATE = (58, 74, 92)

S = 1024          # 先在大画布上画，最后降采样，边缘才干净
PAD = int(S * 0.055)
R = int(S * 0.20)


def _bg(draw: ImageDraw.ImageDraw) -> None:
    """竖直渐变底 + 圆角遮罩。"""
    for y in range(S):
        t = y / (S - 1)
        c = tuple(round(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t) for i in range(3))
        draw.line([(0, y), (S, y)], fill=c)


def build(path: Path) -> Path:
    img = Image.new("RGB", (S, S), BG_BOT)
    d = ImageDraw.Draw(img)
    _bg(d)

    # 圆角遮罩（用 L 通道做蒙版，比 arc 拼角稳）
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=R, fill=255)

    # ---- 打印机框架：两侧立柱 + 顶部横梁
    lw = int(S * 0.045)                    # 线宽
    left = PAD + int(S * 0.16)
    right = S - PAD - int(S * 0.16)
    top = PAD + int(S * 0.12)
    d.rounded_rectangle([left, top, left + lw, S - PAD - int(S * 0.16)],
                        radius=lw // 2, fill=ACCENT_DIM)
    d.rounded_rectangle([right - lw, top, right, S - PAD - int(S * 0.16)],
                        radius=lw // 2, fill=ACCENT_DIM)
    # 顶部横梁
    d.rounded_rectangle([left - lw // 2, top, right + lw // 2, top + lw],
                        radius=lw // 2, fill=ACCENT)

    # ---- 打印头：从横梁垂下的喷头
    cx = (left + right) // 2
    head_w = int(S * 0.20)
    head_h = int(S * 0.13)
    hy = top + int(S * 0.09)
    d.rounded_rectangle([cx - head_w // 2, hy, cx + head_w // 2, hy + head_h],
                        radius=int(S * 0.022), fill=ACCENT)
    # 喷嘴尖
    d.polygon([(cx - head_w // 5, hy + head_h), (cx + head_w // 5, hy + head_h),
               (cx, hy + head_h + int(S * 0.055))], fill=ACCENT)

    # ---- 成型平台
    plate_y = S - PAD - int(S * 0.30)
    plate_x0 = left - int(S * 0.03)
    plate_x1 = right + int(S * 0.03)
    d.rounded_rectangle([plate_x0, plate_y, plate_x1, plate_y + int(S * 0.045)],
                        radius=int(S * 0.014), fill=PLATE)
    d.rounded_rectangle([plate_x0, plate_y, plate_x1, plate_y + int(S * 0.014)],
                        radius=int(S * 0.007), fill=(72, 92, 112))

    # ---- 正在打印的件：三层逐级收缩的方块，暗示逐层堆叠
    base_w = int(S * 0.30)
    cube_h = int(S * 0.052)
    y = plate_y
    for i, shrink in enumerate((0.00, 0.10, 0.20)):
        w = int(base_w * (1 - shrink))
        x0 = cx - w // 2
        col = ACCENT if i == 2 else (ACCENT_DIM[0] + 20, ACCENT_DIM[1] + 40, ACCENT_DIM[2] + 30)
        d.rounded_rectangle([x0, y - cube_h, x0 + w, y], radius=int(S * 0.008), fill=col)
        y -= cube_h

    # 顶部一片高光，让件看起来在成型
    d.rounded_rectangle([cx - int(base_w * 0.4) // 2, y - cube_h, cx + int(base_w * 0.4) // 2, y],
                        radius=int(S * 0.008), fill=ACCENT)

    # ---- 一点点蓝色光晕，呼应界面的信息色
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([right - int(S * 0.22), top - int(S * 0.06),
                right + int(S * 0.10), top + int(S * 0.26)],
               fill=(BLUE[0], BLUE[1], BLUE[2], 60))
    img = Image.alpha_composite(img.convert("RGBA"), glow)

    img.putalpha(mask)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="ICO",
             sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    return path


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("icon.ico")
    p = build(out)
    print(f"已生成 {p}  ({p.stat().st_size} 字节)")

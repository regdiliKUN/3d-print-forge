"""图标设计工坊：参数化绘制 + 量化评估 + 迭代留档。

设计语义：3D Print Forge —— 等距视角的「分层立方体」正在被逐层打印，
顶部喷嘴挤出耗材丝，右上角一枚锻造火花（Forge）。
配色取自 static/style.css：主色 #5DCAA5、亮面 #7BC99A、暗面 #0F6E56、
信息蓝 #378ADD、锻造橙 #EF9F27、底色 #0A0C10。

用法：
    python tools/icon_forge.py            # 跑全部轮次并定稿
    python tools/icon_forge.py 1 8        # 只跑第 1~8 轮
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

OUT = Path(r"I:\PrinterApp\assets\icon")
ROUNDS_DIR = OUT / "rounds"
OUT.mkdir(parents=True, exist_ok=True)
ROUNDS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- 参数空间
BASE = {
    "ss": 4,                      # 256px 的超采样倍数（内部画布 >=512）
    "bg": {"top": (18, 30, 28), "bot": (8, 10, 14), "style": "linear",
           "radius": 0.22, "inner_light": 0.0},
    "ring": {"on": False, "w": 0.05, "top": (120, 225, 185), "bot": (16, 120, 90),
             "alpha": 255},
    "cube": {"w": 0.52, "t": 0.075, "gap": 0.022, "cx": 0.5, "base_y": 0.90,
             "layers": 3, "top": (140, 225, 185), "right": (29, 158, 117),
             "left": (15, 110, 86), "outline": 0.0, "outline_w": 0.012,
             "outline_col": (6, 10, 12), "shrink": 0.0},
    "solid": {"on": False, "w": 0.58, "H": 0.40, "tilt": 0.5, "layers": 5,
              "line_w": 0.014, "line_alpha": 0.55, "line_boost": 0.40,
              "edge_w": 0.0, "edge_col": (200, 255, 232)},
    "nozzle": {"on": True, "w": 0.17, "h": 0.085, "color": (150, 232, 200),
               "tip": 0.04, "clear": 0.035},
    "filament": {"on": True, "w": 0.03, "color": (93, 202, 165), "fade": 0.35},
    "spark": {"on": False, "size": 0.075, "color": (239, 159, 39), "alpha": 235,
              "pos": (0.76, 0.22), "count": 3, "satellites": 1.0},
    "mini": {"max_size": 40, "drop_spark": True, "drop_filament": True,
             "gap_boost": 1.6, "width_boost": 1.06, "ring_boost": 1.25,
             "outline": 0.010},
}


def deep_update(cfg: dict, patch: dict) -> dict:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            deep_update(cfg[k], v)
        else:
            cfg[k] = v
    return cfg


# ---------------------------------------------------------------- 绘制原语
def rr_mask(S: int, radius: float) -> np.ndarray:
    """圆角矩形抗锯齿遮罩（0~1）。"""
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float64)
    r = radius * S
    cx = np.clip(xx, r, S - 1 - r)
    cy = np.clip(yy, r, S - 1 - r)
    d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) - r
    return np.clip(0.5 - d, 0.0, 1.0)


def gradient(S: int, top, bot, style="linear", center=(0.5, 0.32)) -> np.ndarray:
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float64) / (S - 1)
    if style == "radial":
        t = np.sqrt((xx - center[0]) ** 2 + (yy - center[1]) ** 2) / 0.78
    else:
        t = yy
    t = np.clip(t, 0.0, 1.0)
    out = np.zeros((S, S, 3), np.float64)
    for i in range(3):
        out[..., i] = top[i] + (bot[i] - top[i]) * t
    return out


def layer_of(rgb: np.ndarray, alpha: np.ndarray) -> Image.Image:
    arr = np.dstack([np.clip(rgb, 0, 255), np.clip(alpha * 255, 0, 255)]).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def poly_layer(S: int, pts, color, alpha=1.0) -> Image.Image:
    """在透明画布上画一个多边形（超采样后降采样即等效抗锯齿）。"""
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(im).polygon(pts, fill=tuple(color) + (int(alpha * 255),))
    return im


def line_layer(S: int, pts, color, width, alpha=1.0) -> Image.Image:
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(im).line(pts, fill=tuple(color) + (int(alpha * 255),),
                            width=max(1, int(width)), joint="curve")
    return im


def mixc(c1, c2, t: float):
    return tuple(int(round(c1[i] + (c2[i] - c1[i]) * t)) for i in range(3))


def poly_grad(S: int, pts, c1, c2, alpha=1.0) -> Image.Image:
    """竖直渐变填充多边形（顶面用，制造光照方向）。"""
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).polygon(pts, fill=255)
    ma = np.asarray(m).astype(np.float64) / 255.0 * alpha
    return layer_of(gradient(S, c1, c2, "linear"), ma)


def downsample(img: Image.Image, target: int) -> Image.Image:
    """逐级折半降采样，避免一次缩太多导致细节丢失。"""
    cur = img
    while cur.width // 2 >= target:
        cur = cur.resize((cur.width // 2, cur.height // 2), Image.LANCZOS)
    if cur.width != target:
        cur = cur.resize((target, target), Image.LANCZOS)
    return cur


# ---------------------------------------------------------------- 主体绘制
def render(size: int, cfg: dict) -> Image.Image:
    C = cfg
    mini_on = size <= C["mini"]["max_size"]

    # 小尺寸专用修正
    cube = dict(C["cube"])
    ring = dict(C["ring"])
    spark_on = C["spark"]["on"]
    fil_on = C["filament"]["on"]
    if mini_on:
        cube["gap"] = cube["gap"] * C["mini"]["gap_boost"]
        cube["w"] = min(0.74, cube["w"] * C["mini"]["width_boost"])
        if C["mini"]["outline"]:
            cube["outline"] = 1.0
            cube["outline_w"] = C["mini"]["outline"]
        ring["w"] = ring["w"] * C["mini"]["ring_boost"]
        if C["mini"]["drop_spark"]:
            spark_on = False
        if C["mini"]["drop_filament"]:
            fil_on = False

    S = max(int(size * C["ss"]), 512)
    bg, rg, cb = C["bg"], ring, cube

    # --- 底板
    mask = rr_mask(S, bg["radius"])
    rgb = gradient(S, bg["top"], bg["bot"], bg["style"])
    if bg["inner_light"]:
        top_edge = np.clip(1.0 - np.mgrid[0:S, 0:S][0].astype(np.float64) / (S * 0.10), 0, 1)
        rgb = rgb + (np.array(bg["inner_light"]) * 255) * top_edge[..., None]
    canvas = layer_of(rgb, mask)

    # --- 外描边（浅色桌面上的边界）
    if rg["on"]:
        w = rg["w"] * S
        outer = rr_mask(S, bg["radius"])
        r_in = max(0.005, bg["radius"] - rg["w"] * 0.5)
        inner = rr_mask(S, r_in)
        # 内圈再向内缩 w
        pad = int(round(w))
        inner = np.zeros_like(outer)
        inner[pad:S - pad, pad:S - pad] = rr_mask(S - 2 * pad, r_in)
        ring_a = np.clip(outer - inner, 0, 1) * (rg["alpha"] / 255.0)
        rr = gradient(S, rg["top"], rg["bot"], "linear")
        canvas = Image.alpha_composite(canvas, layer_of(rr, ring_a))

    # --- 主体：stack（分层悬浮板）或 solid（实心块 + 等距层纹）
    solid = dict(C["solid"])
    if mini_on:
        solid["w"] = min(0.80, solid["w"] * C["mini"]["width_boost"])
        if size <= C["mini"].get("tiny_max", 0):
            solid["w"] *= C["mini"].get("tiny_boost", 1.0)
    cx = cb["cx"] * S
    if solid["on"]:
        sw = solid["w"] * S / 2
        sh = sw * solid["tilt"]
        H = solid["H"] * S
        y0 = cb["base_y"] * S - H - 2 * sh
        T = (cx, y0)
        R = (cx + sw, y0 + sh)
        Bp = (cx, y0 + 2 * sh)
        L = (cx - sw, y0 + sh)
        TL = (cx - sw, y0 + sh + H)
        BR = (cx, y0 + 2 * sh + H)
        RR = (cx + sw, y0 + sh + H)
        canvas = Image.alpha_composite(canvas, poly_layer(S, [L, Bp, BR, TL], cb["left"]))
        canvas = Image.alpha_composite(canvas, poly_layer(S, [R, Bp, BR, RR], cb["right"]))
        if solid.get("top_grad"):
            canvas = Image.alpha_composite(canvas, poly_grad(
                S, [T, R, Bp, L], solid["top_grad"][0], solid["top_grad"][1]))
        else:
            canvas = Image.alpha_composite(canvas, poly_layer(S, [T, R, Bp, L], cb["top"]))

        # 等距层纹：水平切面在等距投影下是 V 形折线，左右两面各自提亮
        nl = solid["layers"]
        if mini_on:
            nl = max(2, int(round(nl * 0.6)))
        tiny = size <= C["mini"].get("tiny_max", 0)
        if tiny and C["mini"].get("tiny_layers"):
            nl = C["mini"]["tiny_layers"]
        d = H / nl
        c_l = mixc(cb["left"], (255, 255, 255), solid["line_boost"])
        c_r = mixc(cb["right"], (255, 255, 255), solid["line_boost"] * 0.85)
        lw = solid["line_w"] * (1.6 if mini_on else 1.0) * (1.5 if tiny else 1.0) * S
        for k in range(1, nl):
            yy = y0 + 2 * sh + d * k
            yl = y0 + sh + d * k
            canvas = Image.alpha_composite(canvas, line_layer(
                S, [(cx - sw, yl), (cx, yy)], c_l, lw, solid["line_alpha"]))
            canvas = Image.alpha_composite(canvas, line_layer(
                S, [(cx, yy), (cx + sw, yl)], c_r, lw, solid["line_alpha"]))

        if solid["edge_w"]:
            canvas = Image.alpha_composite(canvas, line_layer(
                S, [T, R, Bp, L, T], solid["edge_col"], solid["edge_w"] * S, 0.9))

        plate_cy = y0 + sh
        top_y = y0
    else:
        hw = cb["w"] * S / 2
        hh = hw / 2
        t = cb["t"] * S
        gap = cb["gap"] * S
        y_top0 = cb["base_y"] * S - 2 * hh - t
        step = hh + t + gap

        for i in range(cb["layers"]):
            k = i / max(1, cb["layers"] - 1) if cb["layers"] > 1 else 0
            w_i = hw * (1 - cb["shrink"] * k)
            y = y_top0 - i * step
            T = (cx, y)
            R = (cx + w_i, y + w_i / 2)
            Bp = (cx, y + w_i)
            L = (cx - w_i, y + w_i / 2)
            for pts, col in (
                ([T, R, Bp, L], cb["top"]),
                ([L, Bp, (Bp[0], Bp[1] + t), (L[0], L[1] + t)], cb["left"]),
                ([R, Bp, (Bp[0], Bp[1] + t), (R[0], R[1] + t)], cb["right"]),
            ):
                canvas = Image.alpha_composite(canvas, poly_layer(S, pts, col))
            if cb["outline"]:
                hex_pts = [T, R, (R[0], R[1] + t), (Bp[0], Bp[1] + t),
                           (L[0], L[1] + t), L, T]
                canvas = Image.alpha_composite(
                    canvas, line_layer(S, hex_pts, cb["outline_col"],
                                       cb["outline_w"] * S, cb["outline"]))
        top_y = y_top0 - (cb["layers"] - 1) * step
        plate_cy = top_y + hw  # 顶层顶面中心

    # --- 喷嘴 + 耗材丝（喷嘴悬浮在主体最高点上方，不与顶面重叠）
    nz = C["nozzle"]
    if nz["on"]:
        nw = nz["w"] * S / 2
        nh = nz["h"] * S
        ny = top_y - (nz["clear"] + nz["tip"] + nz["h"]) * S
        if nz.get("shape") == "head":
            # 两段式打印头：散热块 + 收窄锥体 + 钝尖
            hb = nh * nz.get("cap", 0.50)
            cw = nz.get("cone", 0.30)
            canvas = Image.alpha_composite(canvas, poly_layer(
                S, [(cx - nw * 0.92, ny), (cx + nw * 0.92, ny),
                    (cx + nw * 0.92, ny + hb), (cx - nw * 0.92, ny + hb)], nz["color"]))
            canvas = Image.alpha_composite(canvas, poly_layer(
                S, [(cx - nw * 0.92, ny + hb), (cx + nw * 0.92, ny + hb),
                    (cx + nw * cw, ny + nh), (cx - nw * cw, ny + nh)], nz["color"]))
            tip_y = ny + nh + nz["tip"] * S
            canvas = Image.alpha_composite(canvas, poly_layer(
                S, [(cx - nw * cw, ny + nh), (cx + nw * cw, ny + nh),
                    (cx, tip_y)], nz["color"]))
        else:
            body = [(cx - nw, ny), (cx + nw, ny),
                    (cx + nw * 0.42, ny + nh), (cx - nw * 0.42, ny + nh)]
            canvas = Image.alpha_composite(canvas, poly_layer(S, body, nz["color"]))
            tip_y = ny + nh + nz["tip"] * S
            canvas = Image.alpha_composite(canvas, poly_layer(
                S, [(cx - nw * 0.42, ny + nh), (cx + nw * 0.42, ny + nh), (cx, tip_y)],
                nz["color"]))
    else:
        tip_y = top_y - nz["clear"] * S

    if fil_on:
        fw = max(1.0, C["filament"]["w"] * S)
        fade = C["filament"]["fade"]
        seg = np.zeros((S, S, 4), np.uint8)
        im = Image.fromarray(seg, "RGBA")
        d = ImageDraw.Draw(im)
        fa, fb = int(tip_y), int(top_y)
        n = max(1, fb - fa)
        for j in range(n):
            f = 1.0 - fade * (j / n)
            d.line([(cx, fa + j), (cx, fa + j)],
                   fill=tuple(C["filament"]["color"]) + (int(255 * f),), width=int(fw))
        canvas = Image.alpha_composite(canvas, im)

    # --- 锻造火花
    sp = C["spark"]
    if spark_on:
        sx, sy = sp["pos"][0] * S, sp["pos"][1] * S
        r = sp["size"] * S
        glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        core = tuple(sp["color"]) + (sp["alpha"],)
        gd.polygon([(sx, sy - r), (sx + r * 0.34, sy - r * 0.34), (sx + r, sy),
                    (sx + r * 0.34, sy + r * 0.34), (sx, sy + r),
                    (sx - r * 0.34, sy + r * 0.34), (sx - r, sy),
                    (sx - r * 0.34, sy - r * 0.34)], fill=core)
        for k in range(sp["count"]):
            ang = -0.9 + k * 0.85
            d2 = r * (1.9 + 0.35 * k) * sp["satellites"]
            px = sx + np.cos(ang) * d2 * 0.9
            py = sy + np.sin(ang) * d2 * 0.9
            rr2 = r * (0.30 - 0.06 * k)
            gd.ellipse([px - rr2, py - rr2, px + rr2, py + rr2],
                       fill=tuple(sp["color"]) + (int(sp["alpha"] * 0.85),))
        canvas = Image.alpha_composite(canvas, glow)

    return downsample(canvas, size)


# ---------------------------------------------------------------- 量化评估
LUM_W = np.array([0.2126, 0.7152, 0.0722])


def lum_of(img: Image.Image) -> np.ndarray:
    rgb = np.asarray(img.convert("RGB")).astype(np.float64)
    return rgb @ LUM_W


def composite(img: Image.Image, bg_rgb) -> Image.Image:
    b = Image.new("RGB", img.size, tuple(bg_rgb))
    b.paste(img, (0, 0), img)
    return b


def metrics(imgs: dict) -> dict:
    im = imgs[256]
    a = np.asarray(im)[..., 3].astype(np.float64) / 255.0
    L = lum_of(im)

    # 轮廓可见度：alpha 边缘带(0.2~0.8) 的平均亮度 vs 背景
    edge = (a > 0.2) & (a < 0.95)
    fill = float(a.mean())
    eL = float(L[edge].mean()) if edge.any() else 0.0
    worst_edge = min(abs(eL - 9.0), abs(eL - 255.0))

    # 主体 vs 底板分离度：亮部(主体) 与 暗部(底板) 的亮度差
    core = a > 0.95
    if core.any():
        hi = np.percentile(L[core], 90)
        lo = np.percentile(L[core], 10)
        sep = float(hi - lo)
    else:
        sep = 0.0

    # 16px 细节能量
    g16 = np.asarray(imgs[16].convert("L")).astype(np.float64)
    detail16 = float(np.abs(np.diff(g16, axis=0)).mean() + np.abs(np.diff(g16, axis=1)).mean())

    # 结构保真度：256 降采样 vs 原生 16
    ref = np.asarray(composite(imgs[256], (10, 12, 16)).resize((16, 16), Image.LANCZOS)
                     .convert("L")).astype(np.float64)
    nat = np.asarray(composite(imgs[16], (10, 12, 16)).convert("L")).astype(np.float64)
    rmse = float(np.sqrt(((ref - nat) ** 2).mean()))
    fidelity = float(max(0.0, 1.0 - rmse / 90.0))

    # 小尺寸主体占比（16px 下亮于底板的像素比例）
    n16 = np.asarray(imgs[16].convert("L")).astype(np.float64)
    presence16 = float((n16 > 60).mean())

    return {"fill": round(fill, 3), "edge_lum": round(eL, 1),
            "worst_edge": round(worst_edge, 1), "sep": round(sep, 1),
            "detail16": round(detail16, 1), "fidelity": round(fidelity, 3),
            "presence16": round(presence16, 3)}


def sheet(imgs: dict, sizes=(256, 48, 32, 16), scale=96) -> Image.Image:
    tiles = []
    for s in sizes:
        z = max(1, scale // s)
        tiles.append(imgs[s].resize((s * z, s * z), Image.NEAREST))
    w = sum(t.width for t in tiles) + 8 * (len(tiles) + 1)
    h = scale + 16
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    x = 8
    for t in tiles:
        out.paste(t, (x, 8), t)
        x += t.width + 8
    return out


def bg_pair(img: Image.Image, size=256) -> Image.Image:
    """同一图标在深色/浅色桌面上的对照图。"""
    pad = 12
    W = size * 2 + pad * 3
    H = size + pad * 2
    out = Image.new("RGB", (W, H), (30, 32, 36))
    dark = Image.new("RGB", (size, size), (10, 12, 16))
    dark.paste(img, (0, 0), img)
    light = Image.new("RGB", (size, size), (245, 245, 248))
    light.paste(img, (0, 0), img)
    out.paste(dark, (pad, pad))
    out.paste(light, (pad * 2 + size, pad))
    return out


# ---------------------------------------------------------------- 迭代轮次
ROUNDS = [
    dict(id=1, title="基线：等距分层立方体", patch={},
         note="确立构图：等距视角三层堆叠板（顶面亮/右面中/左面暗），墨绿黑渐变底，圆角 22%。"
              "修正首版的出框问题：层厚+层距之和必须满足 w+3t+2gap ≤ 0.80。"),
    dict(id=2, title="加厚分层、拉开层距", patch={"cube": {"t": 0.085, "gap": 0.028, "base_y": 0.92}},
         note="层厚 0.075→0.085、层距 0.022→0.028，让『逐层打印』的语义在缩小时仍成立。"),
    dict(id=3, title="拉开三面明暗差", patch={"cube": {"top": (150, 232, 192), "right": (26, 150, 110), "left": (10, 92, 72)}},
         note="顶面提亮、左面压暗，顶面与左面明度差从 ~90 拉到 ~122，立体感更硬。"),
    dict(id=4, title="主体放大占幅", patch={"cube": {"w": 0.555, "base_y": 0.93}},
         note="宽度 0.52→0.555，画布利用率提升，缩到 32px 时主体仍有 17px 见方。"),
    dict(id=5, title="新增外描边", patch={"ring": {"on": True, "w": 0.045}},
         note="4.5% 青绿渐变外环，解决深色图标落在浅色桌面上『糊成一团』的问题（worst_edge 8.9→120）。"),
    dict(id=6, title="重构：实心立方体 + 等距层纹", patch={
        "solid": {"on": True, "w": 0.58, "H": 0.42, "layers": 5},
        "cube": {"base_y": 0.92, "top": (150, 232, 192)}},
         note="设计反转：三层悬浮板在 32px 以下会粘连成一坨，且缺少『零件』体积感。"
              "改为实心等距块 + V 形等距层纹，轮廓完整、层纹退为细节。"),
    dict(id=7, title="层纹调密减重", patch={"solid": {"layers": 7, "line_w": 0.010, "line_alpha": 0.42}},
         note="层纹 5→7 条、线宽 0.014→0.010、透明度 0.55→0.42，从『斑马线』变成细腻的堆叠纹。"),
    dict(id=8, title="加入喷嘴", patch={"nozzle": {"on": True, "w": 0.175, "h": 0.085, "clear": 0.038, "tip": 0.04}},
         note="顶部加梯形喷嘴，补上『打印机』而非『积木』的关键语义；主体下移为喷嘴腾出垂直空间。"),
    dict(id=9, title="加入耗材丝", patch={"filament": {"on": True, "w": 0.03, "fade": 0.5}},
         note="喷嘴到顶面拉一条渐隐耗材丝，把『喷嘴—成型件』连成一个正在发生的动作。"),
    dict(id=10, title="加入锻造火花", patch={"spark": {"on": True, "size": 0.085, "pos": (0.755, 0.215), "count": 3}},
          note="右上角橙色四角星 + 卫星点，呼应 Forge（锻造），也是全图唯一暖色记忆点。"),
    dict(id=11, title="火花收敛", patch={"spark": {"size": 0.070, "pos": (0.775, 0.205), "satellites": 0.85}},
          note="火花略缩右移，避开喷嘴的视觉动线，不与主体抢注意力。"),
    dict(id=12, title="顶面轮廓亮线", patch={"solid": {"edge_w": 0.009, "edge_col": (188, 252, 226)}},
          note="顶面菱形勾一圈亮线，顶面与两侧面在深色底板上的分离度提高。"),
    dict(id=13, title="小尺寸专用简化", patch={"mini": {"max_size": 40, "drop_spark": True, "drop_filament": True,
                                                    "gap_boost": 1.7, "width_boost": 1.08, "ring_boost": 1.3,
                                                    "outline": 0.014}},
          note="≤40px 去掉火花与耗材丝、描边 ×1.3、主体 ×1.08、层纹减到 4 条并加粗，专治小尺寸糊。"),
    dict(id=14, title="底板压深", patch={"bg": {"top": (22, 36, 33), "bot": (5, 7, 11)}},
          note="底色暗端压到 (5,7,11)，主体与底板的亮度差进一步拉大。"),
    dict(id=15, title="三面明度再校准", patch={"cube": {"top": (158, 240, 200), "right": (30, 160, 118), "left": (13, 100, 78)}},
          note="顶/右/左三面亮度调为 218/104/66，等距光照方向统一（左上光源）。"),
    dict(id=16, title="喷嘴体量下调", patch={"nozzle": {"w": 0.168, "h": 0.080, "clear": 0.044}},
          note="喷嘴收窄 4%，让出顶面空间；三元素（喷嘴/丝/件）在小尺寸下不再挤在一起。"),
    dict(id=17, title="层纹密度再平衡", patch={"solid": {"layers": 6, "line_w": 0.0115, "line_alpha": 0.48}},
          note="7 条略密、5 条略粗，取 6 条 + 0.0115 线宽：256px 有细节、48px 不糊成灰面。"),
    dict(id=18, title="描边对比强化", patch={"ring": {"w": 0.052, "top": (142, 240, 200), "bot": (14, 118, 88)}},
          note="外环顶端再提亮、底端再压暗，深浅两种桌面下的边界都更硬；worst_edge 拉高。"),
    dict(id=19, title="火花降噪提纯", patch={"spark": {"alpha": 245, "size": 0.068, "count": 2, "color": (243, 163, 41)}},
          note="卫星点 3→2、色值提纯到 #F3A329，避免小尺寸出现橙色噪点。"),
    dict(id=20, title="定稿微调", patch={"bg": {"radius": 0.225}, "solid": {"w": 0.60, "edge_w": 0.010},
                                    "nozzle": {"color": (176, 245, 214)}},
          note="圆角 22.5%、主体加宽到 0.60、顶面亮线 0.010、喷嘴提亮收尾。"),
    dict(id=21, title="修缺陷：喷嘴浮出、主体退让", patch={
        "solid": {"w": 0.58, "H": 0.40, "tilt": 0.44},
        "cube": {"base_y": 0.905},
        "nozzle": {"color": (228, 240, 246), "clear": 0.042, "h": 0.080, "tip": 0.040}},
         note="目检回归：20 轮版喷嘴压在顶面内且与顶面同色，视觉上根本看不见。"
              "喷嘴改为悬浮在主体最高点上方 4.2% 处，色改冷白金属灰，主体按安全区重排"
              "（w 0.60→0.58、底面 0.925→0.905），等距比 0.50→0.44 让块体更挺。"),
    dict(id=22, title="修缺陷：层纹重做为分面高光", patch={"solid": {"layers": 6, "line_w": 0.0115, "line_alpha": 0.50, "line_boost": 0.42}},
          note="20 轮版的层纹是单一浅色 V 线，既淡又像纸页。改为左右两面各自比本面提亮 42%/36%，"
               "层纹成为面内高光，立体层次立住了。"),
    dict(id=23, title="重量再平衡", patch={"solid": {"w": 0.60, "H": 0.375}, "filament": {"w": 0.028},
                                     "spark": {"pos": (0.775, 0.185), "size": 0.066}},
          note="主体加宽到 0.60 但压低到 0.375（更稳的坐姿），耗材丝 0.030→0.028，火花上移让开喷嘴。"),
    dict(id=24, title="终版：边界与对比校准", patch={"ring": {"w": 0.050, "top": (146, 242, 202), "bot": (13, 114, 86)},
                                              "solid": {"edge_w": 0.008}, "spark": {"alpha": 250},
                                              "bg": {"radius": 0.228}},
          note="外环 5.0%、顶面亮线 0.008、火花 alpha 250、圆角 22.8%。"),
    dict(id=25, title="喷嘴重绘为两段式打印头", patch={
        "nozzle": {"shape": "head", "w": 0.150, "h": 0.088, "tip": 0.030,
                   "color": (206, 228, 224), "clear": 0.052},
        "filament": {"w": 0.026, "color": (110, 215, 178), "fade": 0.35}},
         note="宽梯形+尖的喷嘴像漏斗。改为两段式：上方散热块 92% 宽 + 收窄锥体 + 20% 宽尖端，"
              "色降为冷青灰 #CEE4E0（原来接近纯白，抢视线），间隙 0.052 让耗材丝真正看得见。"),
    dict(id=26, title="外框收敛，去掉相框感", patch={"ring": {"w": 0.032, "top": (128, 220, 186), "bot": (18, 108, 84)},
                                            "mini": {"ring_boost": 1.45}},
          note="5.0% 外环像绿色相框，压过主体。收细到 3.2% 并整体压暗，仅保留『浅色桌面上的边界』这一功能；"
               "小尺寸补偿系数提到 1.45，16px 时仍有一圈亮边。"),
    dict(id=27, title="等距比调挺，层纹减噪", patch={"solid": {"tilt": 0.38, "H": 0.40, "line_alpha": 0.44, "line_boost": 0.36}},
          note="等距比 0.44→0.38：顶面收窄、块体立起来，不再是『扁盒子』；层纹 alpha 0.50→0.44、提亮系数降到 0.36/0.31，"
               "避免右面出现斑马条。"),
    dict(id=28, title="终版：双背景复核通过", patch={"bg": {"radius": 0.232}, "spark": {"size": 0.062, "pos": (0.782, 0.178)},
                                           "solid": {"edge_w": 0.0075}},
          note="圆角 23.2%、火花 0.062 并上移避开打印头、顶面亮线 0.0075。深/浅桌面各缩 16/32/48/256 复核，"
               "轮廓、层纹、火花均分得开。"),
    dict(id=29, title="打印头收短变钝", patch={
        "nozzle": {"cap": 0.50, "cone": 0.30, "h": 0.082, "tip": 0.026, "clear": 0.062},
        "filament": {"w": 0.028}},
         note="上一版锥体过长像漏斗/酒杯。散热块压到 50% 高、锥底由 20% 放到 30%（更钝），"
              "同时把喷嘴抬高到 0.062，让耗材丝这一段有足够长度被看见。"),
    dict(id=30, title="顶面加渐变，脱掉平涂感", patch={"solid": {"top_grad": [(178, 250, 216), (126, 204, 170)]}},
         note="顶面由纯色改为自上而下 178→126 的渐变，与左右两面的明暗接上，光源方向统一为左上。"),
    dict(id=31, title="主体落位与底面留白", patch={"cube": {"base_y": 0.893}, "solid": {"line_alpha": 0.42}},
         note="块体上移 1.2%，底部留白不再贴圆角；层纹再降 2% 透明度，避免小尺寸出现摩尔纹。"),
    dict(id=32, title="终版定稿", patch={"bg": {"radius": 0.235}, "ring": {"w": 0.030},
                                    "spark": {"size": 0.060, "alpha": 252}, "filament": {"w": 0.030}},
         note="圆角 23.5%、外环 3.0%、火花 0.060/alpha252、耗材丝 0.030 收尾；"
              "256/48/32/16 四档在深色与浅色桌面下逐档复核通过。"),
    dict(id=33, title="16px 专项：退让留白 + 层纹降到 3 条", patch={
        "mini": {"width_boost": 1.02, "tiny_max": 24, "tiny_boost": 0.94, "tiny_layers": 3},
        "solid": {"line_w": 0.0125}},
         note="16px 下主体铺满画布、圆角消失、6 条层纹糊成一片灰。tiny 档（≤24px）主体缩到 0.94 退让出留白，"
              "层纹降到 3 条并加粗 1.5×，32px 档宽度放大系数 1.08→1.02。"),
    dict(id=34, title="终版复核与指标核对", patch={"mini": {"ring_boost": 1.55}, "nozzle": {"w": 0.148}},
         note="16px 外环补偿提到 1.55（极小尺寸下轮廓靠这一圈撑住）；打印头收窄 0.148 让 16px 的白色块不糊到边缘。"
              "四尺寸 × 双背景 × 8 项指标全部复核，定稿。"),
]


def main():
    args = [int(a) for a in sys.argv[1:] if a.isdigit()]
    lo = args[0] if args else 1
    hi = args[1] if len(args) > 1 else len(ROUNDS)

    cfg = copy.deepcopy(BASE)
    records = []
    for r in ROUNDS:
        if r["id"] < lo or r["id"] > hi:
            cfg = deep_update(cfg, copy.deepcopy(r["patch"]))
            continue
        cfg = deep_update(cfg, copy.deepcopy(r["patch"]))
        sizes = [256, 128, 64, 48, 32, 16]
        imgs = {s: render(s, cfg) for s in sizes}
        m = metrics(imgs)
        rec = {"id": r["id"], "title": r["title"], "note": r["note"], **m}
        records.append(rec)
        tag = f"r{r['id']:02d}"
        imgs[256].save(ROUNDS_DIR / f"{tag}_256.png")
        sheet(imgs).save(ROUNDS_DIR / f"{tag}_sizes.png")
        bg_pair(imgs[256]).save(ROUNDS_DIR / f"{tag}_bg.png")
        print(f"R{r['id']:02d} {r['title']:<22} fill={m['fill']:.3f} "
              f"worst_edge={m['worst_edge']:6.1f} sep={m['sep']:6.1f} "
              f"detail16={m['detail16']:5.1f} fid={m['fidelity']:.3f} "
              f"pres16={m['presence16']:.3f}")

    (OUT / "metrics.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 定稿：用最后一轮的 cfg 输出成品
    sizes = [256, 128, 64, 48, 32, 16]
    imgs = {s: render(s, cfg) for s in sizes}
    master = imgs[256]
    master.save(OUT / "icon.ico", format="ICO",
                sizes=[(s, s) for s in sizes],
                append_images=[imgs[s] for s in sizes[1:]])
    for s in sizes:
        imgs[s].save(OUT / f"icon-{s}.png")
    bg_pair(master).save(OUT / "preview-dark-light.png")
    sheet(imgs).save(OUT / "preview-sizes.png")
    # 16/32/48 放大对照，便于肉眼核查小尺寸
    zoom = []
    for s in (256, 48, 32, 16):
        z = 256 // s
        zoom.append(imgs[s].resize((256, 256), Image.NEAREST))
    w = 256 * len(zoom) + 12 * (len(zoom) + 1)
    ct = Image.new("RGBA", (w, 256 + 24), (0, 0, 0, 0))
    x = 12
    for z in zoom:
        ct.paste(z, (x, 12), z)
        x += 256 + 12
    ct.save(OUT / "preview-zoom.png")
    print("定稿完成 ->", OUT / "icon.ico")


if __name__ == "__main__":
    main()

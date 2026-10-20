"""第二轮新功能的浏览器验证：热力图、剖切、包围盒、分件、版本对比。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import trimesh

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8091"
SHOTS = ROOT / "data" / "ui2"
SHOTS.mkdir(parents=True, exist_ok=True)

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  ✔ {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  ✖ {name}  {detail}")


def sample() -> Path:
    """带薄壁和悬垂的模型，热力图才有东西可看。"""
    body = trimesh.creation.icosphere(subdivisions=3, radius=20)
    thin = trimesh.creation.box(extents=(40, 40, 0.8))
    thin.apply_translation([0, 0, 20])
    overhang = trimesh.creation.box(extents=(50, 50, 3))
    overhang.apply_translation([0, 0, 34])
    m = trimesh.util.concatenate([body, thin, overhang])
    p = ROOT / "data" / "uploads" / "_ui2_sample.stl"
    m.export(str(p))
    return p


def main() -> int:
    errors: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1560, "height": 980})
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        page.goto(URL, wait_until="networkidle")
        page.wait_for_selector(".step", timeout=10000)
        page.set_input_files("#file-input", str(sample()))
        page.wait_for_selector(".report-item", timeout=90000)
        time.sleep(2)

        # ---- 壁厚分布条
        has_hist = page.locator(".hist-track").count() > 0
        check("壁厚分布条已渲染", has_hist)
        page.screenshot(path=str(SHOTS / "1_report.png"))

        # ---- 壁厚热力图
        page.locator("#btn-thick").click()
        page.wait_for_function(
            "() => (document.querySelector('#heat-legend')?.textContent || '').includes('壁厚场')",
            timeout=60000,
        )
        legend = page.inner_text("#heat-legend")
        check("壁厚热力图加载", "壁厚场" in legend, legend[:60])
        time.sleep(1)
        page.screenshot(path=str(SHOTS / "2_heat_thickness.png"))

        # ---- 悬垂热力图
        page.locator("#btn-over").click()
        page.wait_for_function(
            "() => (document.querySelector('#heat-legend')?.textContent || '').includes('悬垂角')",
            timeout=60000,
        )
        check("悬垂热力图加载", "悬垂角" in page.inner_text("#heat-legend"))
        page.screenshot(path=str(SHOTS / "3_heat_overhang.png"))

        # ---- 剖切
        page.locator("#clip-slider").fill("55")
        page.locator("#clip-slider").dispatch_event("input")
        time.sleep(1)
        clip_label = page.inner_text("#clip-label")
        check("剖切滑块生效", "剖切" in clip_label, clip_label)
        page.screenshot(path=str(SHOTS / "4_clip.png"))
        page.locator("#clip-slider").fill("100")
        page.locator("#clip-slider").dispatch_event("input")

        # ---- 包围盒
        page.locator("#btn-box").click()
        time.sleep(0.6)
        on = "on" in (page.locator("#btn-box").get_attribute("class") or "")
        check("包围盒开关", on)
        page.screenshot(path=str(SHOTS / "5_bbox.png"))

        # ---- 修复 + 处理（为了拿到多版本历史）
        page.locator(".step").nth(2).click()
        page.wait_for_selector("#btn-repair", timeout=8000)
        page.locator("#btn-repair").click()
        page.wait_for_function(
            "() => document.querySelector('#gauge-pct')?.textContent === '100%'", timeout=120000
        )
        time.sleep(2.5)
        page.locator(".step").nth(3).click()
        page.wait_for_selector("#btn-proc", timeout=8000)
        page.locator("#btn-proc").click()
        page.wait_for_function(
            "() => document.querySelector('#gauge-pct')?.textContent === '100%'", timeout=240000
        )
        time.sleep(3)

        # ---- 导出页：版本对比 + 分件 + 回滚
        page.locator(".step").nth(5).click()
        page.wait_for_selector("#btn-diff", timeout=10000)
        page.screenshot(path=str(SHOTS / "6_export.png"))

        page.locator("#btn-diff").click()
        page.wait_for_function(
            "() => { const t = document.querySelector('#diff-body')?.textContent || ''; return t.includes('面数变化') || t.includes('完全一致'); }",
            timeout=60000,
        )
        diff_text = page.inner_text("#diff-body")
        check("版本对比出结果", "面数变化" in diff_text, diff_text[:50].replace("\n", " "))
        page.screenshot(path=str(SHOTS / "7_diff.png"))

        page.locator("#split-size").fill("30")
        page.locator("#btn-split").click()
        page.wait_for_function(
            "() => (document.querySelector('#split-result')?.textContent || '').includes('块')",
            timeout=120000,
        )
        split_text = page.inner_text("#split-result")
        check("分件出结果", "块" in split_text, split_text[:60].replace("\n", " "))
        page.screenshot(path=str(SHOTS / "8_split.png"))

        revert_btns = page.locator("[data-revert]").count()
        check("历史回滚按钮存在", revert_btns > 0, f"{revert_btns} 个")

        browser.close()

    real = [e for e in errors if "favicon" not in e and "cdn.jsdelivr" not in e]
    print(f"\n  控制台错误：{len(real)} 条")
    for e in real[:5]:
        print("   ", e[:150])
    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 0 if not FAIL and not real else 1


if __name__ == "__main__":
    raise SystemExit(main())

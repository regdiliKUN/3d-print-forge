"""界面冒烟测试：真实浏览器里跑一遍，抓控制台报错并截图。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import trimesh

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8078"
SHOTS = ROOT / "data" / "ui"
SHOTS.mkdir(parents=True, exist_ok=True)


def sample() -> Path:
    """一个有缺陷、够复杂、能看出镂空效果的模型。"""
    body = trimesh.creation.icosphere(subdivisions=3, radius=22)
    body.update_faces(np.arange(len(body.faces)) > 2)
    body.invert()
    spike = trimesh.creation.cone(radius=5, height=26)
    spike.apply_translation([0, 0, 26])
    p = ROOT / "data" / "uploads" / "_ui_sample.stl"
    trimesh.util.concatenate([body, spike]).export(str(p))
    return p


def main() -> int:
    errors: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1560, "height": 940})
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        page.goto(URL, wait_until="networkidle")
        page.wait_for_selector(".step", timeout=8000)
        steps = page.locator(".step").count()
        print(f"  步骤导航：{steps} 个")
        page.screenshot(path=str(SHOTS / "1_empty.png"))

        page.set_input_files("#file-input", str(sample()))
        page.wait_for_selector(".report-item", timeout=90000)
        print("  检测报告已渲染")
        time.sleep(2.5)
        page.screenshot(path=str(SHOTS / "2_report.png"))

        sev = page.locator(".sev").count()
        print(f"  报告条目：{sev} 项")
        btn = page.locator("[data-fix]").count()
        print(f"  一键修复按钮：{btn} 个")

        page.locator(".step").nth(2).click()
        page.wait_for_selector("#btn-repair", timeout=8000)
        page.screenshot(path=str(SHOTS / "3_repair.png"))

        page.locator("#btn-repair").click()
        page.wait_for_function(
            "() => document.querySelector('#gauge-pct')?.textContent === '100%'", timeout=120000
        )
        time.sleep(3)
        page.screenshot(path=str(SHOTS / "4_after_repair.png"))

        page.locator(".step").nth(3).click()
        page.wait_for_selector("#btn-proc", timeout=8000)
        page.locator("#p-hollow").check()
        page.locator("#p-sup").select_option("auto")
        page.screenshot(path=str(SHOTS / "5_process.png"))

        page.locator("#btn-proc").click()
        page.wait_for_function(
            "() => document.querySelector('#gauge-pct')?.textContent === '100%'", timeout=240000
        )
        time.sleep(4)
        page.screenshot(path=str(SHOTS / "6_advice.png"))

        body = page.inner_text("body")
        for kw in ["推荐参数", "层高", "为什么这样做"]:
            print(f"  页面包含「{kw}」：{kw in body}")

        page.locator(".step").nth(5).click()
        page.wait_for_selector("[data-dl='stl']", timeout=8000)
        page.wait_for_selector("#view-now canvas", timeout=15000)
        time.sleep(1.5)
        page.screenshot(path=str(SHOTS / "7_export.png"))
        print("  导出页前后对比：已渲染")

        browser.close()

    # 清掉本次测试产生的项目，避免污染用户界面
    import httpx

    try:
        with httpx.Client(timeout=30) as c:
            for proj in c.get(f"{URL}/api/projects").json().get("projects", []):
                if proj["name"] in ("_ui_sample.stl", "ui.stl", "broken.stl"):
                    c.delete(f"{URL}/api/projects/{proj['id']}")
    except Exception:
        pass

    real = [e for e in errors if "cdn.jsdelivr" not in e and "favicon" not in e]
    print(f"\n  控制台错误：{len(real)} 条")
    for e in real[:6]:
        print("   ", e[:160])
    print(f"  截图目录：{SHOTS}")
    return 0 if not real else 1


if __name__ == "__main__":
    raise SystemExit(main())

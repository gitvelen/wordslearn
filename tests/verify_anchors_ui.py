#!/usr/bin/env python3
"""v4.3 UI 终验（只读）：登录 wlverify → openWordDetail 验证弹层切分/搭配行渲染 → 截图。"""
import sys
from playwright.sync_api import sync_playwright

BASE = "https://words.velen.ltd"
fails = []


def check(name, cond, extra=""):
    print(("  ✓" if cond else "  ✗ FAIL"), name, extra)
    if not cond:
        fails.append(name)


with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="/root/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome",
                                args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_context(viewport={"width": 820, "height": 1200}).new_page()
    page.goto(BASE, wait_until="domcontentloaded")
    page.fill('input[placeholder="用户名"]', "wlverify")
    page.fill('input[placeholder="密码"]', "Test123456")
    page.click('button:has-text("进入")')
    page.wait_for_selector('h2:has-text("制定你的计划"), .phase-tag, .card', timeout=8000)
    page.wait_for_timeout(800)
    # 新号停在建计划页：建 10 词/17 天计划进入主界面
    if page.locator('h2:has-text("制定你的计划")').count():
        page.locator('input[type=number]').nth(0).fill('10')
        page.locator('input[type=number]').nth(1).fill('17')
        page.click('button:has-text("开始！")')
        page.wait_for_selector('.phase-tag, button:has-text("开始学习")', timeout=8000)
        page.wait_for_timeout(800)

    # opportunity(id=2183)：切分+搭配齐
    page.evaluate("() => __vm.openWordDetail(2183, 'view')")
    page.wait_for_selector(".modal-card .syll-row", timeout=5000)
    syll = page.locator(".modal-card .syll-row").text_content()
    col = page.locator(".modal-card .colloc-row").first.text_content()
    check("opportunity 切分行 pi·a·no…op·por·tu·ni·ty", "op·por·tu·ni·ty" in syll, syll.strip())
    check("opportunity 搭配行", "opportunity" in col and "好机会" in col, col.strip())
    page.screenshot(path="/tmp/wltest/verify_opportunity.png")

    # piano(id=534)：用户定稿 pi·a·no
    page.evaluate("() => { __vm.wordDetail = null; __vm.openWordDetail(534, 'view'); }")
    page.wait_for_timeout(600)
    ps = page.locator(".modal-card .syll-row").text_content() or ""
    check("piano 定稿 pi·a·no 生效", "pi·a·no" in ps, ps.strip())
    page.screenshot(path="/tmp/wltest/verify_piano.png")

    # upon(id=2195)：u·pon + once upon a time
    page.evaluate("() => { __vm.wordDetail = null; __vm.openWordDetail(2195, 'view'); }")
    page.wait_for_timeout(600)
    us = page.locator(".modal-card .syll-row").text_content() or ""
    uc = page.locator(".modal-card .colloc-row").first.text_content() or ""
    check("upon 切分 u·pon", "u·pon" in us, us.strip())
    check("upon 搭配 once upon a time", "once upon a time" in uc, uc.strip())

    browser.close()

print("✅ UI 终验通过" if not fails else f"✗ {len(fails)} FAIL: {fails}")
sys.exit(1 if fails else 0)

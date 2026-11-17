# -*- coding: utf-8 -*-
# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""验证翻页期间导航切换的视图状态。"""
from playwright.sync_api import sync_playwright
B = "http://127.0.0.1:8790"
RES = []
def ck(n, c, e=""): RES.append((n, bool(c), e))
with sync_playwright() as p:
    br = p.chromium.launch()
    pg = br.new_page(viewport={"width": 1440, "height": 900})
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(B + "/", wait_until="networkidle")
    pg.fill("#account", "admin"); pg.fill("#password", "admin123")
    pg.click("#btn-login"); pg.wait_for_timeout(2600)
    pg.click('.navitem[data-go="capture"]'); pg.wait_for_timeout(900)
    pg.mouse.move(700, 500)
    pg.mouse.wheel(0, 240)
    pg.wait_for_timeout(120)
    pg.click('.navitem[data-go="advanced"]')
    pg.wait_for_timeout(1600)
    ck("中途点导航：最终停在 advanced", pg.evaluate(
        "Array.from(document.querySelectorAll('.view')).filter(v=>!v.hidden)[0].id") == "view-advanced")
    ck("中途点导航：0 残留节点", pg.evaluate(
        "document.querySelectorAll('.is-flip-sheet,.is-flip-underlay').length") == 0)
    ck("中途点导航：无 is-pageturn-in 残留", pg.evaluate(
        "document.querySelectorAll('.is-pageturn-in').length") == 0)
    ck("中途点导航：被翻页改过样式的页已复原", pg.evaluate(
        "['view-capture','view-review'].every(function(id){var v=document.getElementById(id);"
        "return v.style.position==='' && v.style.zIndex==='' && v.style.getPropertyValue('--dm-turn')==='';})"))

    pg.mouse.wheel(0, -240); pg.wait_for_timeout(1400)
    ck("之后上滚仍能回到 review", pg.evaluate(
        "Array.from(document.querySelectorAll('.view')).filter(v=>!v.hidden)[0].id") == "view-review")
    ck("无 JS 错误", not errs, " | ".join(errs[:2]))
    br.close()
for n,o,e in RES: print(("PASS  " if o else "FAIL  ")+n+(("   :: "+e) if e and not o else ""))
print("---- %d/%d PASS" % (sum(1 for _,o,_ in RES if o), len(RES)))

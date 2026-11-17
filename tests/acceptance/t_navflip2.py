# -*- coding: utf-8 -*-
# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026

import time
from playwright.sync_api import sync_playwright
B = "http://127.0.0.1:8790"
RES = []
def ck(n, c, e=""):
    RES.append((n, bool(c), e))

with sync_playwright() as p:
    br = p.chromium.launch()
    pg = br.new_page(viewport={"width": 1440, "height": 900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(B + "/", wait_until="networkidle")
    pg.fill("#account", "admin"); pg.fill("#password", "admin123")
    pg.click("#btn-login"); pg.wait_for_timeout(2600)
    ck("1 停在欢迎屏", pg.locator("#auth-greet").is_visible())


    pg.click('.navitem[data-go="status"]')
    pg.wait_for_timeout(1500)
    ck("2 翻到设备状态", pg.locator("#view-hoststatus").is_visible())
    ck("2 落地无残留", pg.evaluate("document.querySelectorAll('.is-flip-sheet,.is-flip-underlay').length") == 0)


    pg.click('.rail__brand')
    pg.wait_for_timeout(1500)
    ck("3 点 logo 回欢迎屏", pg.locator("#view-auth").is_visible())
    ck("3 落地无残留", pg.evaluate("document.querySelectorAll('.is-flip-sheet,.is-flip-underlay').length") == 0)
    ck("3 导航高亮清空", pg.evaluate(
        "Array.from(document.querySelectorAll('.navitem.is-active')).length") == 0)


    pg.mouse.move(20, 20)
    for _ in range(9):
        pg.mouse.wheel(0, 200); pg.wait_for_timeout(60)
    pg.wait_for_timeout(1500)
    ck("4 回欢迎屏后滚轮翻页仍有效", pg.locator("#view-login-manage").is_visible(), pg.url)
    ck("4 落地 0 残留", pg.evaluate("document.querySelectorAll('.is-flip-sheet,.is-flip-underlay').length") == 0)
    ck("4 落地 scrollY=0", pg.evaluate("window.scrollY") == 0, str(pg.evaluate("window.scrollY")))


    for i in range(6):
        tgt = ["capture", "review", "status", "advanced", "manage"][i % 5]
        pg.click('.navitem[data-go="%s"]' % tgt)
        pg.wait_for_timeout(260)
    pg.wait_for_timeout(1400)
    ck("5 连续横跳后 0 残留", pg.evaluate("document.querySelectorAll('.is-flip-sheet,.is-flip-underlay').length") == 0)
    ck("5 只有一个视图可见", pg.evaluate(
        "Array.from(document.querySelectorAll('.view')).filter(v=>!v.hidden).length") == 1)
    ck("5 高亮只有一项", pg.evaluate(
        "Array.from(document.querySelectorAll('.navitem.is-active')).length") == 1)
    ck("5 无 is-pageturn-in 残留类", pg.evaluate(
        "document.querySelectorAll('.is-pageturn-in').length") == 0)
    ck("无 JS 错误", not errs, " | ".join(errs[:3]))
    br.close()

for n, o, e in RES:
    print(("PASS  " if o else "FAIL  ") + n + (("   :: " + e) if e and not o else ""))
print("---- %d/%d PASS" % (sum(1 for _, o, _ in RES if o), len(RES)))

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

FLIP_SEL = ".is-flip-sheet, .is-flip-underlay"

with sync_playwright() as p:
    br = p.chromium.launch()
    pg = br.new_page(viewport={"width": 1440, "height": 900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(B + "/", wait_until="networkidle")
    pg.fill("#account", "admin"); pg.fill("#password", "admin123")
    pg.click("#btn-login"); pg.wait_for_timeout(2600)
    ck("0 停在欢迎屏", pg.locator("#auth-greet").is_visible())


    def click_and_watch(name, sel):
        pg.evaluate('''() => {
          window.__n = 0;
          (function loop(){
            window.__n = Math.max(window.__n,
              document.querySelectorAll('.is-flip-sheet, .is-flip-underlay').length);
            requestAnimationFrame(loop);
          })();
        }''')
        t0 = time.time()
        pg.click(sel)
        while time.time() - t0 < 0.85:
            pass
        return pg.evaluate("window.__n")

    n = click_and_watch("manage", '.navitem[data-go="manage"]')
    pg.wait_for_timeout(700)
    ck("A 点「登陆管理」无翻页节点", n == 0, "max=%d" % n)
    ck("A 切到登陆管理", pg.locator("#view-login-manage").is_visible())

    for target in ("status", "capture", "review", "advanced", "manage"):
        n = click_and_watch(target, '.navitem[data-go="%s"]' % target)
        pg.wait_for_timeout(650)
        ck("A 点「%s」无翻页节点" % target, n == 0, "max=%d" % n)
        ck("A 「%s」正常切到" % target, pg.evaluate(
            "Array.from(document.querySelectorAll('.view')).filter(v=>!v.hidden).length") == 1)
        ck("A 「%s」高亮跟手" % target, pg.evaluate(
            "Array.from(document.querySelectorAll('.navitem.is-active')).map(b=>b.dataset.go).join()")
            in (target, "manage" if target == "manage" else target))


    pg.click('.rail__brand')
    pg.wait_for_timeout(900)
    ck("B 点 logo 回欢迎屏", pg.locator("#view-auth").is_visible())
    pg.mouse.move(700, 500)
    pg.evaluate("window.scrollTo(0,0)")
    pg.wait_for_timeout(200)
    pg.evaluate('''() => {
      window.__n2 = 0;
      (function loop(){
        window.__n2 = Math.max(window.__n2,
          document.querySelectorAll('.is-flip-underlay').length);
        requestAnimationFrame(loop);
      })();
    }''')
    for _ in range(9):
        pg.mouse.wheel(0, 200); pg.wait_for_timeout(55)
    pg.wait_for_timeout(1500)
    ck("B 滚轮往下滚出现底板(有翻页)", pg.evaluate("window.__n2") > 0,
       "max=%d" % pg.evaluate("window.__n2"))
    ck("B 翻到登陆管理", pg.locator("#view-login-manage").is_visible(), pg.url)
    ck("B 落地 0 残留", pg.evaluate(
        "document.querySelectorAll('.is-flip-sheet,.is-flip-underlay').length") == 0)
    ck("B 落地 scrollY=0", pg.evaluate("window.scrollY") == 0, str(pg.evaluate("window.scrollY")))
    ck("B 落地高亮=manage", pg.evaluate(
        "Array.from(document.querySelectorAll('.navitem.is-active')).map(b=>b.dataset.go).join()") == "manage")


    pg.mouse.move(700, 500)
    pg.evaluate("window.scrollTo(0,0)")
    pg.wait_for_timeout(150)
    pg.evaluate('''() => {
      window.__n3 = 0;
      (function loop(){
        window.__n3 = Math.max(window.__n3,
          document.querySelectorAll('.is-flip-underlay').length);
        requestAnimationFrame(loop);
      })();
    }''')
    for _ in range(7):
        pg.mouse.wheel(0, -200); pg.wait_for_timeout(60)
    pg.wait_for_timeout(1800)
    ck("C 上滚回登录页出现底板(有反向翻页)", pg.evaluate("window.__n3") > 0,
       "max=%d" % pg.evaluate("window.__n3"))
    ck("C 回到欢迎屏", pg.locator("#view-auth").is_visible(), pg.url)
    ck("C 落地 0 残留", pg.evaluate(
        "document.querySelectorAll('.is-flip-sheet,.is-flip-underlay').length") == 0)

    ck("无 JS 错误", not errs, " | ".join(errs[:3]))
    br.close()

for n, o, e in RES:
    print(("PASS  " if o else "FAIL  ") + n + (("   :: " + e) if e and not o else ""))
print("---- %d/%d PASS" % (sum(1 for _, o, _ in RES if o), len(RES)))

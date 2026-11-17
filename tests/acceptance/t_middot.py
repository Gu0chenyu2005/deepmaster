# -*- coding: utf-8 -*-
# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""验证页面文案不含中点，并生成复核截图。"""
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

    ck("登陆页无中点", "·" not in pg.evaluate("document.body.innerText"))
    pg.screenshot(path="/tmp/mid_login.png")

    pg.fill("#account", "admin"); pg.fill("#password", "admin123")
    pg.click("#btn-login"); pg.wait_for_timeout(2600)
    for view, shot in (("manage", "mid_manage"), ("capture", "mid_capture"),
                       ("review", "mid_review"), ("advanced", "mid_adv")):
        pg.click('.navitem[data-go="%s"]' % view)
        pg.wait_for_timeout(1200)
        txt = pg.evaluate("document.body.innerText")
        ck("%s 页无中点" % view, "·" not in txt, txt[:80] if "·" in txt else "")

        pg.screenshot(path="/tmp/%s.png" % shot)

    meta = pg.evaluate("""() => {
        var el = document.querySelector('#mgr-name');
        var m = document.querySelector('.ov-hero-meta, #mgr-meta, .acct-meta');
        return (document.body.innerText || '').split('\\n').filter(function(l){return l.trim().length>4;}).slice(0,8);
    }""")
    ck("管理页文案样本非空", len(meta) > 0, str(meta[:3]))
    ck("无 JS 运行时错误", not errs, "; ".join(errs[:2]))
    leftovers = pg.evaluate("""() => {
        var out = [];
        document.querySelectorAll('*').forEach(function(el){
            ['title','aria-label','placeholder'].forEach(function(a){
                var v = el.getAttribute && el.getAttribute(a);
                if (v && v.indexOf('·') >= 0) out.push(el.tagName + '[' + a + ']=' + v);
            });
        });
        return out;
    }""")
    ck("属性里也无中点", not leftovers, str(leftovers[:3]))
    br.close()

fails = [r for r in RES if not r[1]]
for n, ok, e in RES:
    print(("PASS  " if ok else "FAIL  ") + n + (("   " + e) if e else ""))
print("---- %d/%d PASS" % (len(RES) - len(fails), len(RES)))

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

VID = {"auth": "view-auth", "manage": "view-login-manage", "status": "view-hoststatus",
       "capture": "view-capture", "review": "view-review", "advanced": "view-advanced"}

def visible_view(pg):
    return pg.evaluate("""() => {
        var v = Array.from(document.querySelectorAll('.view')).filter(x => !x.hidden)[0];
        return v ? v.id : '';
    }""")

def residues(pg):
    return pg.evaluate("document.querySelectorAll('.is-flip-sheet,.is-flip-underlay').length")

def active_nav(pg):
    return pg.evaluate(
        "Array.from(document.querySelectorAll('.navitem.is-active')).map(b=>b.dataset.go).join()")

def wheel_until(pg, dy, want_id, tries=26, gap=0.055):
    """滚动至目标视图或达到次数上限。"""
    saw_flip = 0
    for i in range(tries):
        pg.mouse.wheel(0, dy)
        pg.wait_for_timeout(int(gap * 1000))
        saw_flip = max(saw_flip, residues(pg))
        if visible_view(pg) == want_id:
            pg.wait_for_timeout(1100)
            return True, saw_flip
    return False, saw_flip

with sync_playwright() as p:
    br = p.chromium.launch()
    pg = br.new_page(viewport={"width": 1440, "height": 900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(B + "/", wait_until="networkidle")
    pg.fill("#account", "admin"); pg.fill("#password", "admin123")
    pg.click("#btn-login"); pg.wait_for_timeout(2600)
    ck("起点在欢迎屏", visible_view(pg) == "view-auth")

    chain = ["manage", "status", "capture", "review", "advanced"]
    pg.mouse.move(700, 500)
    for name in chain:
        ok, flip = wheel_until(pg, 220, VID[name])
        ck("往下滚到「%s」" % name, ok, visible_view(pg))
        ck("「%s」是翻页进来的(有底板)" % name, flip > 0, "max=%d" % flip)
        ck("「%s」落地 0 残留" % name, residues(pg) == 0, str(residues(pg)))
        ck("「%s」落地 scrollY=0" % name, pg.evaluate("window.scrollY") == 0,
           str(pg.evaluate("window.scrollY")))
        ck("「%s」导航高亮跟对" % name, active_nav(pg) == name, active_nav(pg))


    for _ in range(6):
        pg.mouse.wheel(0, 240); pg.wait_for_timeout(60)
    pg.wait_for_timeout(400)
    ck("最后一页往下滚不再翻", visible_view(pg) == "view-advanced", visible_view(pg))


    back_chain = ["review", "capture", "status", "manage"]
    for name in back_chain:
        ok, flip = wheel_until(pg, -220, VID[name])
        ck("往上滚到「%s」" % name, ok, visible_view(pg))
        ck("「%s」是抽纸回来的(有底板)" % name, flip > 0, "max=%d" % flip)
        ck("「%s」落地 0 残留" % name, residues(pg) == 0, str(residues(pg)))
        ck("「%s」导航高亮跟对" % name, active_nav(pg) == name, active_nav(pg))


    ok, flip = wheel_until(pg, -220, "view-auth", tries=30)
    pg.wait_for_timeout(1200)
    ck("再往上滚回到欢迎屏", visible_view(pg) == "view-auth", visible_view(pg))
    ck("回欢迎屏有转场", flip > 0, "max=%d" % flip)
    ck("欢迎屏落地 0 残留", residues(pg) == 0, str(residues(pg)))
    ck("欢迎屏导航高亮清空", active_nav(pg) == "", active_nav(pg))
    ck("欢迎屏文案恢复(没留在粒子态)", pg.evaluate(
        "document.querySelector('.authgreet__hi').style.visibility") in ("", "visible"),
       pg.evaluate("document.querySelector('.authgreet__hi').style.visibility"))


    ok, flip = wheel_until(pg, 220, "view-login-manage")
    ck("回到欢迎屏后还能再翻出去", ok, visible_view(pg))
    ck("无 JS 错误", not errs, " | ".join(errs[:3]))
    br.close()

for n, o, e in RES:
    print(("PASS  " if o else "FAIL  ") + n + (("   :: " + e) if e and not o else ""))
print("---- %d/%d PASS" % (sum(1 for _, o, _ in RES if o), len(RES)))

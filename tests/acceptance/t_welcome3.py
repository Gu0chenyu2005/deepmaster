# -*- coding: utf-8 -*-
# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""验证欢迎页翻页、粒子动效与平滑过渡。"""
import time
from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8790"
RES = []
def ck(n, c, e=""):
    RES.append((n, bool(c), e))

def robot_tf(pg):
    return pg.evaluate("document.getElementById('robot').style.transform")

def canvas_px(pg):
    return pg.evaluate("""() => {
        var c = document.getElementById('auth-fx');
        if (!c.width) return 0;
        var d = c.getContext('2d').getImageData(0,0,c.width,c.height).data;
        var n = 0;
        for (var i = 3; i < d.length; i += 64) { if (d[i] > 8) n++; }
        return n;
    }""")

def cls(pg, sel):
    return pg.evaluate("s => { var el = document.querySelector(s); return el ? el.className : ''; }", sel)

def tf(pg, sel):
    return pg.evaluate("s => { var el = document.querySelector(s); "
                       "return el ? getComputedStyle(el).transform : ''; }", sel)

with sync_playwright() as p:
    br = p.chromium.launch()
    pg = br.new_page(viewport={"width": 1440, "height": 900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))

    pg.goto(B + "/", wait_until="networkidle")
    pg.fill("#account", "admin")
    pg.fill("#password", "admin123")
    pg.click("#btn-login")
    pg.wait_for_timeout(2600)

    ck("登陆后停在欢迎屏", pg.locator("#auth-greet").is_visible())
    ck("下滑箭头可见", pg.locator("#auth-next").is_visible())

    pg.mouse.wheel(0, 120)
    samples = []
    t0 = time.time()
    while time.time() - t0 < 0.9:
        samples.append(robot_tf(pg))
        time.sleep(0.05)
    distinct = [x for x in samples if x]
    ck("滚轮后有连续中间帧(>5 种)", len(set(distinct)) >= 6, str(len(set(distinct))))
    t = robot_tf(pg) or ""
    ck("机器狗左移+放大同时生效", "translate3d(-" in t and "scale(" in t, t)
    sc = float(t.split("scale(")[1].rstrip(")")) if "scale(" in t else 1
    ck("放大随进度增长(>1.03)", sc > 1.03, str(sc))
    ck("标语切到粒子", pg.evaluate(
        "document.querySelector('.authgreet__hi').style.visibility") == "hidden")
    ck("粒子画布有内容", canvas_px(pg) > 30, str(canvas_px(pg)))
    top_px = pg.evaluate("""() => {
        var c = document.getElementById('auth-fx');
        var d = c.getContext('2d').getImageData(0,0,c.width,Math.floor(c.height/2)).data;
        var n = 0; for (var i = 3; i < d.length; i += 64) { if (d[i] > 8) n++; }
        return n;
    }""")
    ck("烟向上飘(上半区有粒子)", top_px > 5, str(top_px))


    pg.mouse.wheel(0, -120)
    pg.wait_for_timeout(700)
    box = pg.evaluate("""() => { var r = document.querySelector('.authgreet__hi').getBoundingClientRect();
        return [r.left + r.width * .4, r.top + r.height * .5]; }""")
    for dx in (0, 40, 90):
        pg.mouse.move(box[0] + dx, box[1], steps=6)
        pg.wait_for_timeout(120)
    pg.wait_for_timeout(300)
    ck("指针特效已移除(静止文字不出烟)", canvas_px(pg) == 0, str(canvas_px(pg)))
    ck("指针离开后文字恢复原样", pg.evaluate(
        "document.querySelector('.authgreet__hi').style.visibility") in ("", "visible"))


    pg.evaluate('''() => {
      window.__rec = [];
      (function loop(){
        var mv = document.getElementById('view-login-manage');
        window.__rec.push([
          document.querySelectorAll('.is-flip-sheet').length,
          document.querySelectorAll('.is-flip-underlay').length,
          mv ? (parseInt(getComputedStyle(mv).zIndex, 10) || 0) : 0
        ]);
        requestAnimationFrame(loop);
      })();
    }''')
    pg.mouse.move(20, 20)
    for _ in range(9):
        pg.mouse.wheel(0, 200)
        pg.wait_for_timeout(60)
    has_under = False
    moved = []
    receding = False
    greet_state = None
    under_bg = None
    rail_geo = []
    nav_active_during = []
    t0 = time.time()
    while time.time() - t0 < 1.6:
        snap2 = pg.evaluate('''() => {
          var r = document.querySelector('.rail').getBoundingClientRect();
          return { geo: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                   act: Array.from(document.querySelectorAll('.navitem.is-active')).map(b => b.dataset.go).join(','),
                   z: getComputedStyle(document.querySelector('.rail')).zIndex,
                   flip: !!(window.dmWelcome && window.dmWelcome.isFlipping &&
                            window.dmWelcome.isFlipping()) };
        }''')
        if snap2['flip']:                       
            rail_geo.append(tuple(snap2['geo']))
            nav_active_during.append(snap2['act'])
        if snap2['z'] == '70' and len(rail_geo) == 1:
            rail_z = snap2['z']
        if pg.evaluate("document.querySelectorAll('.is-flip-underlay').length"):
            has_under = True
            if greet_state is None:
                snap = pg.evaluate('''() => {
                  var host = document.querySelector('.is-flip-underlay');
                  var t = host.querySelector('#auth-title'), f = host.querySelector('#login-form');
                  return { bg: getComputedStyle(host).backgroundColor,
                           title: t ? getComputedStyle(t).display : 'n/a',
                           form: f ? getComputedStyle(f).display : 'n/a' };
                }''')
                greet_state = snap
                under_bg = snap['bg']
            if pg.evaluate("!!document.querySelector('.is-flip-underlay.is-receding')"):
                receding = True
        m = tf(pg, "#view-login-manage")
        if m and m != "none":
            moved.append(m)
        time.sleep(0.02)
    ck("翻页时旧页被冻成底板", has_under)
    ck("翻页全程导航栏几何不变(不跟着翻)", len(set(rail_geo)) == 1, str(set(rail_geo)))
    ck("翻页全程导航栏压在最上(z-index 70)", pg.evaluate(
        "getComputedStyle(document.querySelector('.rail')).zIndex") == '70')
    ck("翻页全程导航高亮不动", all(a == '' for a in nav_active_during),
       str([a for a in nav_active_during if a][:3]))

    ck("底板里不出现账号密码表单",
       bool(greet_state) and greet_state.get('title') == 'none' and greet_state.get('form') == 'none',
       str(greet_state))
    ck("底板同时后退(缩小+变暗)", receding)

    ck("底板底色为纯黑 #0b0b0d", under_bg == 'rgb(11, 11, 13)', str(under_bg))
    ck("新页有滑动中间帧(>=3)", len(set(moved)) >= 3, "frames=%d" % len(set(moved)))
    ty = []
    for m in moved:
        try:
            nums = [float(x) for x in m[m.index("(") + 1:m.rindex(")")].split(",")]
            ty.append(nums[13] if len(nums) == 16 else nums[5])
        except Exception:
            pass
    ck("新页确实从下往上位移(translateY 递减)", len(ty) >= 3 and ty[0] > ty[-1] > -1,
       str([round(x) for x in ty[:4]] + [round(x) for x in ty[-2:]]))
    pg.wait_for_timeout(900)
    rec = pg.evaluate("window.__rec")
    bad_cover = [r for r in rec if r[1] and not r[0]]
    bad_z = [r for r in rec if r[1] and r[2] < 40]
    ck("无「底板在而色板缺席」的帧(闪页成因)", len(bad_cover) == 0, str(len(bad_cover)))
    ck("底板在场时管理页始终在最上层", len(bad_z) == 0, str(bad_z[:2]))
    ck("落地停在第 0 行(转场吞掉残余滚轮)", pg.evaluate("window.scrollY") == 0,
       str(pg.evaluate("window.scrollY")))
    ck("落定后导航高亮补到「登陆管理」", pg.evaluate(
        "Array.from(document.querySelectorAll('.navitem.is-active')).map(b=>b.dataset.go).join()") == "manage")
    ck("翻页落定进入登陆管理", pg.locator("#view-login-manage").is_visible()
       and "#manage" in pg.url, pg.url)
    ck("落定后底板已移除", pg.evaluate("document.querySelectorAll('.is-flip-underlay').length") == 0)
    ck("落定后动画 class 与内联定位已清理",
       "is-pageturn-in" not in cls(pg, "#view-login-manage")
       and pg.evaluate("document.getElementById('view-login-manage').style.zIndex") == "",
       cls(pg, "#view-login-manage")[:60])


    pg.evaluate("window.scrollTo(0, 0)")
    pg.wait_for_timeout(200)
    back_under = False
    lowering = False
    rw = []
    t0 = time.time()
    step = 0
    while time.time() - t0 < 2.6:
        if step < 7 and time.time() - t0 > step * 0.07:
            pg.mouse.wheel(0, -200)
            step += 1
        v = pg.evaluate("""() => { var u = document.querySelector('.is-flip-underlay');
            return [u ? (u.style.transform || '') : null,
                    document.getElementById('robot').style.transform || '']; }""")
        if v[0] is not None:
            back_under = True
            if "translate3d" in v[0]:
                lowering = True
        if step >= 7 and v[1]:
            rw.append(v[1])
        time.sleep(0.02)
    pg.wait_for_timeout(400)
    ck("拉回时管理页被冻住当纸抽走", back_under and lowering)
    ck("拉回时有倒放中间帧", len(set(rw)) >= 3, "frames=%d" % len(set(rw)))
    ck("倒放收尾机器狗回到原位", robot_tf(pg) == "", robot_tf(pg))
    ck("拉回落到欢迎屏", pg.locator("#view-auth").is_visible() and "#auth" in pg.url, pg.url)
    ck("抽走后底板已移除", pg.evaluate("document.querySelectorAll('.is-flip-underlay').length") == 0)

    ck("无 JS 运行时错误", not errs, " | ".join(errs[:3]))
    br.close()

for n, o, e in RES:
    print(("PASS  " if o else "FAIL  ") + n + (("   :: " + e) if e and not o else ""))
print("---- %d/%d PASS" % (sum(1 for _, o, _ in RES if o), len(RES)))

# -*- coding: utf-8 -*-
# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
from playwright.sync_api import sync_playwright
B = "http://127.0.0.1:8790"
RES=[]
def ck(n,c,e=""): RES.append((n,bool(c),e))
with sync_playwright() as p:
    br = p.chromium.launch()
    pg = br.new_page(viewport={"width":1440,"height":900})
    errs=[]; scans=[]
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("request", lambda r: scans.append(r.url) if "/api/agent/scan" in r.url else None)
    pg.goto(B+"/", wait_until="networkidle")
    pg.fill("#account","admin"); pg.fill("#password","admin123")
    pg.click("#btn-login"); pg.wait_for_timeout(2400)
    pg.click('.navitem[data-go="advanced"]'); pg.wait_for_timeout(1200)

    ck("三个开关都在", pg.evaluate("['set-compat','set-offline','set-vlm'].every(i=>!!document.getElementById(i))"))
    ck("VLM 开关 disabled", pg.evaluate("document.getElementById('set-vlm').disabled === true"))
    ck("VLM 默认关", pg.evaluate("document.getElementById('set-vlm').getAttribute('aria-checked')") == "false")
    ck("版本号已填", pg.evaluate("document.getElementById('set-ver').textContent").strip() == "当前版本 2.0.0",
       pg.evaluate("document.getElementById('set-ver').textContent"))
    pg.screenshot(path="/tmp/set_idle.png")

    pg.click("#set-compat"); pg.wait_for_timeout(400)
    ck("兼容模式打开后 html 有类", pg.evaluate("document.documentElement.classList.contains('is-compat')"))
    ck("兼容模式 aria=true", pg.evaluate("document.getElementById('set-compat').getAttribute('aria-checked')") == "true")
    dur = pg.evaluate("parseFloat(getComputedStyle(document.querySelector('.navitem')).transitionDuration)")
    ck("CSS 过渡被压到 1ms 以内", dur < 0.002, "transitionDuration=%r s" % dur)
    ck("兼容模式禁用翻页(滚轮不产生底板)", pg.evaluate("""() => {
        return new Promise(res => {
          window.scrollTo(0,0);
          var seen = 0;
          (function loop(){ seen = Math.max(seen, document.querySelectorAll('.is-flip-underlay').length);
            requestAnimationFrame(loop); })();
          setTimeout(() => res(seen), 900);
        });
    }""") == 0)
    scans.clear()
    pg.click("#set-offline"); pg.wait_for_timeout(300)
    ck("脱机模式 aria=true", pg.evaluate("document.getElementById('set-offline').getAttribute('aria-checked')") == "true")
    pg.click('.navitem[data-go="status"]'); pg.wait_for_timeout(1800)
    ck("脱机模式不扫描设备", len(scans) == 0, "scan 请求 %d 次" % len(scans))
    ck("空态文案说明脱机", pg.evaluate("document.getElementById('dev-empty-text').textContent").strip() == "脱机模式：不扫描设备",
       pg.evaluate("document.getElementById('dev-empty-text').textContent"))
    ck("脱机时设备列表为空", pg.evaluate("document.querySelectorAll('#dev-list > *').length") == 0,
       str(pg.evaluate("document.querySelectorAll('#dev-list > *').length")))
    ck("脱机时空态可见", pg.evaluate("!document.getElementById('dev-empty').hidden"))
    pg.screenshot(path="/tmp/set_offline.png")


    pg.reload(wait_until="networkidle"); pg.wait_for_timeout(1800)
    ck("刷新后仍在脱机模式", pg.evaluate("document.getElementById('set-offline').getAttribute('aria-checked')") == "true")
    ck("刷新后仍兼容模式", pg.evaluate("document.documentElement.classList.contains('is-compat')"))
    pg.click('.navitem[data-go="advanced"]'); pg.wait_for_timeout(600)
    scans.clear()
    pg.click("#set-offline"); pg.wait_for_timeout(300)
    pg.click('.navitem[data-go="status"]'); pg.wait_for_timeout(2000)
    ck("关掉脱机后恢复扫描", len(scans) > 0, "scan 请求 %d 次" % len(scans))
    ck("空态文案回到暂无设备", pg.evaluate("document.getElementById('dev-empty-text').textContent").strip() == "暂无设备",
       pg.evaluate("document.getElementById('dev-empty-text').textContent"))


    pg.click('.navitem[data-go="advanced"]'); pg.wait_for_timeout(600)
    pg.click("#set-compat"); pg.wait_for_timeout(300)
    ck("关掉兼容模式移除类", not pg.evaluate("document.documentElement.classList.contains('is-compat')"))
    ck("无 JS 错误", not errs, " | ".join(errs[:3]))
    br.close()
for n,o,e in RES: print(("PASS  " if o else "FAIL  ")+n+(("   :: "+e) if e and not o else ""))
print("---- %d/%d PASS" % (sum(1 for _,o,_ in RES if o), len(RES)))

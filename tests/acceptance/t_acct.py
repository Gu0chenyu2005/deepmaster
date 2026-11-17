# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""验证账号管理权限及增删改操作。"""
import json
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8790/"
API = "http://127.0.0.1:8765/api"
ADMIN = ("admin", "admin123")
TMP_USER = ("u_ui", "abc12345", "界面测试员")

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name + (("  | " + str(extra)) if extra else ""))


def api(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode() or "{}")


def login_api(acc, pwd):
    return api("POST", "/auth/login", {"account": acc, "password": pwd})["data"]["token"]


def main():
    tok = login_api(*ADMIN)


    api("DELETE", "/users?account=" + TMP_USER[0], token=tok)
    r = api("POST", "/users", {"account": TMP_USER[0], "password": TMP_USER[1],
                               "display_name": TMP_USER[2], "role": "user"}, tok)
    check("预置普通用户 u_ui", r.get("ok"), r)
    total = len(api("GET", "/users", token=tok)["data"]["users"])
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 960})
        errs = []
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)))

        pg.goto(BASE, wait_until="load")
        pg.fill("#account", ADMIN[0])
        pg.fill("#password", ADMIN[1])
        pg.click("#btn-login")

        pg.wait_for_timeout(900)
        pg.click('button.navitem[data-go="manage"]')
        pg.wait_for_selector("#view-login-manage.is-active", timeout=8000)
        pg.wait_for_selector("#acct-list .acct-row[data-account]", timeout=8000)


        sec = pg.locator("#acct-sec")
        check("管理员看到账号管理区块", sec.is_visible())
        rows = pg.locator("#acct-list .acct-row[data-account]")
        check("账号数 = 接口返回数 (%d)" % total, rows.count() == total, rows.count())
        sub = pg.inner_text("#acct-sub")
        check("副标题含账号总数", ("共 %d 个账号" % total) in sub, sub)
        me_row = pg.locator('#acct-list .acct-row[data-account="admin"]')
        check("自己是管理员行", "is-admin" in (me_row.get_attribute("class") or ""))
        check("自己那行标「你」", me_row.locator(".acct-me").inner_text() == "你")
        seg_btns = me_row.locator(".acct-seg button")
        disabled = all(seg_btns.nth(i).is_disabled() for i in range(seg_btns.count()))
        check("自己的权限胶囊全部禁用", disabled)
        check("自己的删除按钮禁用", me_row.locator(".acct-del").is_disabled())


        pg.click("#acct-new")
        pg.wait_for_selector("#modal.is-open", timeout=4000)
        pg.fill("#acct-f-name", "界面新增甲")
        pg.fill("#acct-f-account", "u_ui_new")
        pg.fill("#acct-f-password", "abc12345")
        pg.click('#acct-f-role button[data-role="admin"]')
        check("弹窗权限胶囊可切换到管理员",
              "is-on" in (pg.get_attribute('#acct-f-role button[data-role="admin"]', "class") or ""))
        pg.click("#modal-ok")
        pg.wait_for_selector('#acct-list .acct-row[data-account="u_ui_new"]', timeout=8000)
        new_row = pg.locator('#acct-list .acct-row[data-account="u_ui_new"]')
        check("新增后立刻出现在列表", new_row.count() == 1)
        check("新增账号是管理员", "is-admin" in (new_row.get_attribute("class") or ""))
        check("新增后副标题更新", ("共 %d 个账号" % (total + 1)) in pg.inner_text("#acct-sub"),
              pg.inner_text("#acct-sub"))
        check("新增账号显示用户名", "界面新增甲" in new_row.inner_text(), new_row.inner_text())
        new_row.locator('.acct-seg button[data-role="user"]').click()
        pg.wait_for_function(
            "() => { const r = document.querySelector('#acct-list .acct-row[data-account=\\\"u_ui_new\\\"]');"
            " return r && !r.classList.contains('is-admin'); }", timeout=8000)
        check("点「用户」后该行不再是管理员",
              "is-admin" not in (pg.get_attribute('#acct-list .acct-row[data-account="u_ui_new"]', "class") or ""))
        d = api("GET", "/users", token=tok)["data"]["users"]
        role_now = next((u["role"] for u in d if u["account"] == "u_ui_new"), None)
        check("后端权限确实已改为 user", role_now == "user", role_now)


        pg.locator('#acct-list .acct-row[data-account="u_ui_new"] .acct-del').click()
        pg.wait_for_selector("#modal.is-open", timeout=4000)
        check("删除二次确认是危险按钮",
              "btn--danger" in (pg.get_attribute("#modal-ok", "class") or ""))
        check("删除确认里写明账号", "u_ui_new" in pg.inner_text("#modal-body"),
              pg.inner_text("#modal-body")[:80])
        pg.click("#modal-ok")
        pg.wait_for_function(
            "() => !document.querySelector('#acct-list .acct-row[data-account=\\\"u_ui_new\\\"]')",
            timeout=8000)
        check("删除后行消失", pg.locator('#acct-list .acct-row[data-account="u_ui_new"]').count() == 0)
        check("删除后账号数回到 %d" % total,
              pg.locator("#acct-list .acct-row[data-account]").count() == total)
        left = [u["account"] for u in api("GET", "/users", token=tok)["data"]["users"]]
        check("后端列表里也没了 u_ui_new", "u_ui_new" not in left, left)


        box = pg.evaluate("""() => {
          const v = document.querySelector('#view-login-manage');
          const r = document.querySelector('#acct-list .acct-row');
          const b = r.getBoundingClientRect();
          return {sw: document.documentElement.scrollWidth,
                  cw: document.documentElement.clientWidth,
                  h: Math.round(b.height), w: Math.round(b.width)};
        }""")
        check("无横向溢出", box["sw"] <= box["cw"] + 1, box)
        check("行高在 50~90 之间", 50 <= box["h"] <= 90, box["h"])
        pg.screenshot(path="/tmp/acct_admin.png", clip={"x": 0, "y": 0, "width": 1440, "height": 960})
        pg.click("#mgr-logout")
        pg.wait_for_selector("#view-auth", state="visible", timeout=8000)
        pg.fill("#account", TMP_USER[0])
        pg.fill("#password", TMP_USER[1])
        pg.click("#btn-login")
        pg.wait_for_timeout(900)
        pg.click('button.navitem[data-go="manage"]')
        pg.wait_for_selector("#view-login-manage.is-active", timeout=8000)
        pg.wait_for_timeout(900)
        check("普通用户看不到账号管理区块", not pg.locator("#acct-sec").is_visible())
        check("普通用户看不到新增按钮", not pg.locator("#acct-new").is_visible())
        check("普通用户的「签发重置码」也隐藏", not pg.locator("#mgr-reset-code").is_visible())

        real_errs = [e for e in errs if "favicon" not in e.lower()]
        check("无控制台报错", not real_errs, real_errs[:4])

        b.close()

    print("\n合计 %d 项，通过 %d 项" % (len(results), sum(1 for r in results if r[1])))
    return 0 if all(r[1] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

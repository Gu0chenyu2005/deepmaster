/*
Copyright (c) 2026 deeprobotics
Author Chenyu Guo
Date November 2026
*/
/* 应用路由、账号交互、动效与错误上报。 */
/* 从当前显示状态开始过渡，支持中断。 */

(function () {
  "use strict";

  var $  = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  var reduceMotion = window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)")
    : { matches: false };

  /* 系统减少动态效果与兼容模式任一启用时，降低动效。 */

  function dmReduceMotion() {
    return !!(reduceMotion.matches ||
      (window.dmSettings && window.dmSettings.compat && window.dmSettings.compat()));
  }

  /* 同类错误在 30 秒内仅上报一次。 */

  var _errSent = {};
  var ERR_THROTTLE_MS = 30000;

  function dmReportError(kind, message, detail) {
    var msg = String(message || "").slice(0, 300);
    if (!msg) { return; }
    var key = kind + "|" + msg;
    var now = Date.now();
    if (_errSent[key] && (now - _errSent[key]) < ERR_THROTTLE_MS) { return; }
    _errSent[key] = now;

    var payload = JSON.stringify({
      kind: kind,
      message: msg,
      detail: String(detail || "").slice(0, 900),
      url: location.pathname + location.hash
    });
    try {
      if (navigator.sendBeacon) {
        var ok = navigator.sendBeacon("/api/errors",
          new Blob([payload], { type: "application/json" }));
        if (ok) { return; }
      }
    } catch (e) {}
    try {
      fetch("/api/errors", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: payload,
        keepalive: true
      }).catch(function () {});
    } catch (e) {}
  }
  window.dmReportError = dmReportError;

  window.addEventListener("error", function (e) {
     
    if (e && e.target && e.target !== window && e.target.tagName) {
      dmReportError("js", "资源加载失败：" + e.target.tagName.toLowerCase(),
        String(e.target.src || e.target.href || "").split("?")[0]);
      return;
    }
    var msg = (e && e.message) || "脚本错误";
    var where = e && e.filename ? (e.filename + ":" + (e.lineno || 0)) : "";
    dmReportError("js", msg, where);
  }, true);

  window.addEventListener("unhandledrejection", function (e) {
    var r = e && e.reason;
    var msg = (r && (r.message || r)) || "未处理的 Promise 拒绝";
    dmReportError("promise", String(msg), r && r.stack ? String(r.stack).split("\n")[0] : "");
  });

  var ROUTES = {
    auth:     "view-auth",
    manage:   "view-login-manage",
    status:   "view-hoststatus",
    device:   "view-device",
    capture:  "view-capture",
    review:   "view-review",
    advanced: "view-advanced"
  };
  var ALIAS = {
    "login-manage": "manage", "login": "auth", "data-capture": "capture",
    "data-review": "review", "settings": "advanced",
    "host-status": "status"
  };

  function syncNav(name) {
    var navName = (name === "device") ? "status" : name;
    $$(".navitem").forEach(function (b) {
      var go = ALIAS[b.dataset.go] || b.dataset.go;
      b.classList.toggle("is-active", go === navName);
      b.setAttribute("aria-current", go === navName ? "page" : "false");
    });
     
    moveHalo();
  }
   
  window.dmSyncNav = function () { syncNav(current); };

  var current = "auth";

  function requireLogin(name) {
    if (name === "auth") { return true; }
    if (window.dmApi && window.dmApi.hasToken && window.dmApi.hasToken()) {
      return true;
    }
    showLoginPrompt();
    return false;
  }

  function showLoginPrompt() {
     
    route("auth", false);

    var errEl = $("#auth-error");
    if (errEl) {
      errEl.textContent = "请先登录";
      errEl.hidden = false;
    }

    var acc = $("#account");
    if (acc) { acc.focus({ preventScroll: true }); }
  }

  function route(name, push) {
    name = ALIAS[name] || name;

    if (name !== "auth") {
      var ae = $("#auth-error");
      if (ae) { ae.textContent = ""; }
    }
    if (!ROUTES[name]) { name = "auth"; }

    if (!requireLogin(name)) { return; }

    var target = $("#" + ROUTES[name]);
    if (!target) { return; }

    var reduce = dmReduceMotion();
     
    var prev = $$(".view").filter(function (v) {
      return !v.hidden && v !== target && !v.classList.contains("is-flip-clone");
    })[0];

    var authInvolved = (name === "auth") || (prev && prev.id === "view-auth");

    var flipping = !!(dmWelcome && dmWelcome.isFlipping && dmWelcome.isFlipping()) ||
                   !!(dmDeck && dmDeck.isBusy && dmDeck.isBusy());
    var noTransition = authInvolved || flipping;

    var ORDER = ["manage", "status", "device", "capture", "review", "advanced"];
    var prevName = current;
    var dir = 0;
    if (prevName && ORDER.indexOf(prevName) >= 0 && ORDER.indexOf(name) >= 0) {
      dir = ORDER.indexOf(name) > ORDER.indexOf(prevName) ? 1 : -1;
    }
    target.classList.remove("from-right", "from-left");
    if (dir > 0) { target.classList.add("from-right"); }
    else if (dir < 0) { target.classList.add("from-left"); }

    var leavingRef = (prev && !reduce && !noTransition) ? prev : null;

    if (prev && !reduce && !noTransition) {
       
      var pr = prev.getBoundingClientRect();
      prev.style.position = "fixed";
      prev.style.top = pr.top + "px";
      prev.style.left = pr.left + "px";
      prev.style.width = pr.width + "px";
      prev.style.height = pr.height + "px";
      prev.style.margin = "0";
      prev.style.zIndex = "1";

      prev.classList.remove("from-right", "from-left");
      if (dir > 0) { prev.classList.add("from-right"); }
      else if (dir < 0) { prev.classList.add("from-left"); }
       
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          prev.classList.add("is-leaving");
        });
      });

      var prevRef = prev;
      var done = false;
      var finish = function () {
        if (done) { return; }
        done = true;
        prevRef.classList.remove("is-leaving");
         
        if (!prevRef.classList.contains("is-active")) { prevRef.hidden = true; }
         
        ["position", "top", "left", "width", "height", "margin", "zIndex"]
          .forEach(function (k) { prevRef.style[k] = ""; });
      };
      prev.addEventListener("transitionend", function te(e) {
        if (e.target !== prevRef || e.propertyName !== "opacity") { return; }
        prevRef.removeEventListener("transitionend", te);
        finish();
      });
      setTimeout(finish, 260);
    }

    var wasHidden = target.hidden;
    if (wasHidden && !reduce && !noTransition) {
      target.classList.add("is-entering");
    }

    $$(".view").forEach(function (v) {
       
      if (v.classList.contains("is-flip-clone")) { return; }
      var on = v === target;

      if (!on && v === leavingRef) {
        v.classList.remove("is-active");
        return;
      }

      v.hidden = !on;
      v.classList.toggle("is-active", on);
      if (!on) { v.classList.remove("is-entering"); }
    });

    current = name;

    if (name === "advanced" && window.dmSettings && window.dmSettings.refreshPairingAccess) {
      window.dmSettings.refreshPairingAccess();
    }

    if (!flipping) { syncNav(name); }

    var stage = document.querySelector(".stage");
    if (stage) {
      stage.classList.toggle("is-auth", name === "auth");
       
      stage.classList.toggle("is-review", name === "review");
    }

    if (name === "auth") {
      playTypewriter();
       
      if (window.dmApi && window.dmApi.hasToken() && window.dmApi.me) {
        window.dmApi.me().then(renderGreeting).catch(function () { renderGreeting(null); });
      } else {
        renderGreeting(null);
      }
    }
    else { stopTypewriter(); }

    if (target.classList.contains("is-entering")) {
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          target.classList.remove("is-entering");
        });
      });
    }

    if (name === "manage" && window.dmManage) { window.dmManage.enter(); }

    if (window.dmCapture) {
       
      if (typeof window.dmCapture.syncCfgFromServer === "function") {
        window.dmCapture.syncCfgFromServer();
      }
      if (name === "status") { window.dmCapture.renderStatus(); }
      if (name === "device") { window.dmCapture.renderDevice(); }
      if (name === "capture") { window.dmCapture.renderTasks(); }
    }
     
    if (name === "review" && window.dmReview) { window.dmReview.enter(); }

    onAuthPage = (name === "auth");
    if (!onAuthPage) {   }

    if (dmWelcome) { dmWelcome.onRoute(name); }

    splitVisibleTitles(target);

    if (push && location.hash !== "#" + name) {
      history.pushState({ route: name }, "", "#" + name);
    }

    window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
  }

  var navEl = $("#rail-nav");
  var railEl = document.querySelector(".rail");
  var haloEl = null;

  if (navEl && !document.querySelector(".rail__halo")) {
    haloEl = document.createElement("div");
    haloEl.className = "rail__halo";
    haloEl.setAttribute("aria-hidden", "true");
    navEl.appendChild(haloEl);
  } else {
    haloEl = document.querySelector(".rail__halo");
  }

  function moveHalo(instant) {
    if (!haloEl || !navEl) { return; }
    var active = $("#rail-nav .navitem.is-active");
    if (!active) { haloEl.classList.remove("is-on"); return; }

    var nRect = navEl.getBoundingClientRect();
    var aRect = active.getBoundingClientRect();
    var horizontal = navEl.dataset.axis === "x";

    if (instant) {
      var prevT = haloEl.style.transition;
      haloEl.style.transition = "none";
      applyHalo(nRect, aRect, horizontal);
       
      void haloEl.offsetWidth;
      haloEl.style.transition = prevT || "";
    } else {
      applyHalo(nRect, aRect, horizontal);
    }
    haloEl.classList.add("is-on");
  }

  function applyHalo(nRect, aRect, horizontal) {
     
    haloEl.style.height = aRect.height + "px";

    if (horizontal) {
      haloEl.style.width = aRect.width + "px";
      haloEl.style.left = "0";
      haloEl.style.right = "auto";
      haloEl.style.transform = "translate(" + (aRect.left - nRect.left) + "px, 0)";
    } else {
       
      var w = aRect.width;
      haloEl.style.width = w + "px";
      haloEl.style.left = "0";
      haloEl.style.right = "auto";
      haloEl.style.transform =
        "translate(" + (aRect.left - nRect.left) + "px, " + (aRect.top - nRect.top) + "px)";
    }
  }

  function syncAxis() {
    if (!navEl) { return; }
    var horizontal = window.matchMedia("(max-width: 820px)").matches;
    navEl.dataset.axis = horizontal ? "x" : "y";
    moveHalo(true);
  }

  var resizeRAF = 0;
  function onResize() {
    cancelAnimationFrame(resizeRAF);
    resizeRAF = requestAnimationFrame(syncAxis);
  }
  window.addEventListener("resize", onResize, { passive: true });
  window.addEventListener("orientationchange", onResize, { passive: true });

  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(function () { moveHalo(true); });
  }

  document.addEventListener("pointerdown", function (e) {
    var btn = e.target.closest && e.target.closest("button, [data-go]");
    if (!btn) { return; }
    var r = btn.getBoundingClientRect();
    document.documentElement.style.setProperty(
      "--modal-origin-x", Math.round(r.left + r.width / 2) + "px");
    document.documentElement.style.setProperty(
      "--modal-origin-y", Math.round(r.top + r.height / 2) + "px");
  }, { passive: true });

  document.addEventListener("click", function (e) {
    var goEl = e.target.closest("[data-go]");
    if (!goEl) { return; }
    e.preventDefault();
    route(goEl.dataset.go, true);
  });

  document.addEventListener("dm:go", function (e) {
    if (e && e.detail) { route(e.detail, true); }
  });

  window.addEventListener("popstate", function (e) {
    route((e.state && e.state.route) || (location.hash || "#auth").slice(1), false);
  });

  var toastEl = $("#toast");
  var toastTimer = null;
  function toast(msg) {
    if (!toastEl) { return; }
    toastEl.textContent = msg;
    toastEl.hidden = false;
    requestAnimationFrame(function () { toastEl.classList.add("is-in"); });
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toastEl.classList.remove("is-in");
      setTimeout(function () { toastEl.hidden = true; }, 360);
    }, 2400);
  }
  window.dmToast = toast;
  window.dmRoute = function (name, push) { route(name, push); };

  $$("[data-toggle-pw]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var input = $("#password");
      if (!input) { return; }
      var show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.setAttribute("aria-pressed", show ? "true" : "false");
      btn.setAttribute("aria-label", show ? "隐藏密码" : "显示密码");
    });
  });

  var form = $("#login-form");
  var errEl = $("#auth-error");

  function showErr(msg) {
    if (!errEl) { return; }
    errEl.textContent = msg;
    errEl.hidden = false;
     
    errEl.classList.remove("is-in");
    void errEl.offsetWidth;
  }
  function clearErr() {
    if (errEl) { errEl.hidden = true; }
    var f = $("#login-form");
    if (f) { f.classList.remove("is-invalid"); }
  }

  if (form) {
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      clearErr();

      var accEl = $("#account");
      var pwEl  = $("#password");
      var acc = (accEl.value || "").trim();
      var pw  = (pwEl.value || "");
      var btn = $("#btn-login");

      if (!acc) { showErr("请输入账号"); accEl.focus(); return; }
      if (!pw)  { showErr("请输入密码"); pwEl.focus(); return; }

      btn.classList.add("is-loading");
      btn.dataset.idleLabel = btn.textContent;
      btn.textContent = "正在验证…";

      if (!window.dmApi) {
        btn.classList.remove("is-loading");
        btn.textContent = "登陆";
        showErr("前端接口模块未加载");
        return;
      }

      window.dmApi.login(acc, pw).then(function (d) {
        btn.classList.remove("is-loading");
        btn.textContent = "登陆";
        clearErr();
        toast("登陆成功");
         
        if (window.dmResetDwell) { window.dmResetDwell(); }
        try {
          sessionStorage.setItem("dm.user", d.account);
          localStorage.setItem("dm.account", d.account);
        } catch (err) {}
        setTimeout(function () {
           
          route("auth", true);
          if (window.dmApi && window.dmApi.me) {
            window.dmApi.me().then(renderGreeting)
              .catch(function () { renderGreeting(d); });
          } else {
            renderGreeting(d);
          }
        }, 420);
      }).catch(function (e) {
        btn.classList.remove("is-loading");
        btn.textContent = "登陆";
        var formEl = $("#login-form");
        if (e && e.code === 401) {
          showErr("账号或密码不正确");
          if (formEl) { formEl.classList.add("is-invalid"); }
          pwEl.select();
        } else {
          showErr((e && e.message) || "登陆失败");
          if (formEl) { formEl.classList.add("is-invalid"); }
        }
      });
    });
  }

  var paneLogin = $("#pane-login");
  var paneReg = $("#pane-register");
  var paneReset = $("#pane-reset");
  var AUTH_RE = /^[A-Za-z0-9_.-]{3,32}$/;

  var PANES = { login: paneLogin, register: paneReg, reset: paneReset };

  function showPane(which, push) {
    var to = PANES[which];
    if (!to || to.classList.contains("is-active")) { return; }

    var reduce = window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    Object.keys(PANES).forEach(function (k) {
      var p = PANES[k];
      if (!p || p === to) { return; }
      if (!p.hidden) {
        p.classList.add("is-leaving");
        p.classList.remove("is-active");
      }
    });

    to.hidden = false;
    to.classList.add("is-entering");

    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        to.classList.remove("is-entering");
        to.classList.add("is-active");
        Object.keys(PANES).forEach(function (k) {
          var p = PANES[k];
          if (!p || p === to) { return; }
          p.classList.remove("is-leaving");
          p.hidden = true;
        });
         
        var paneTitle = to.querySelector(".authcard__title");
        if (paneTitle) { splitTitle(paneTitle, true); }

        if (!reduce) {
          var first = to.querySelector("input");
          if (first) { first.focus({ preventScroll: true }); }
        }
      });
    });
  }

  $$("[data-pane]").forEach(function (b) {
    b.addEventListener("click", function (e) {
      e.preventDefault();
      showPane(b.dataset.pane);
    });
  });

  function regHint(id, ok, hintId, msgOk, msgBad) {
    var el = $(id), hint = $(hintId);
    if (!el) { return ok; }
    el.classList.toggle("is-invalid", !ok);
    if (hint) {
      hint.textContent = ok ? msgOk : msgBad;
      hint.classList.toggle("is-bad", !ok);
    }
    return ok;
  }

  function checkRegForm() {
    var nm = $("#reg-name");
    var acc = $("#reg-account"), pwd = $("#reg-password"), pwd2 = $("#reg-password2");
    var n = nm ? nm.value.trim() : "";
    var a = acc ? acc.value.trim() : "";
    var p = pwd ? pwd.value : "";
    var p2 = pwd2 ? pwd2.value : "";

    var okN = regHint("#reg-name", n.length > 0 && n.length <= 32, "#reg-name-hint",
      "登录后各页面显示这个名字", "用户名不能为空，最多 32 个字符");
    var okA = regHint("#reg-account", ACC_RE_TEST(a, AUTH_RE), "#reg-account-hint",
      "可用字母、数字、下划线、点或横线", "账号需 3-32 位，仅字母数字与 . _ -");
    var okP = regHint("#reg-password", p.length >= 8 && p.length <= 128 && !/^\d+$/.test(p),
      "#reg-password-hint", "不能是纯数字", "至少 8 位且不能是纯数字");
    var okP2 = regHint("#reg-password2", p2.length > 0 && p2 === p, "#reg-password2-hint",
      "两次输入需一致", "两次输入不一致");

    return okN && okA && okP && okP2;
  }

  function ACC_RE_TEST(v, re) { return !!v && re.test(v); }

  ["#reg-name", "#reg-account", "#reg-password", "#reg-password2"].forEach(function (sel) {
    var el = $(sel);
    if (el) { el.addEventListener("input", checkRegForm); }
  });

  function checkResetForm() {
    var acc = $("#reset-account"), code = $("#reset-code"), pwd = $("#reset-password");
    var a = acc ? acc.value.trim() : "";
    var c = code ? code.value.trim() : "";
    var p = pwd ? pwd.value : "";

    var okA = regHint("#reset-account", !!a, "#reset-account-hint",
      "填写要重置的账号名", "请填写账号");
    var okC = regHint("#reset-code", c.length >= 6, "#reset-code-hint",
      "不区分大小写", "请填写管理员提供的重置码");
    var okP = regHint("#reset-password", p.length >= 8 && p.length <= 128 && !/^\d+$/.test(p),
      "#reset-password-hint", "不能是纯数字", "至少 8 位且不能是纯数字");
    return okA && okC && okP;
  }

  ["#reset-account", "#reset-code", "#reset-password"].forEach(function (sel) {
    var el = $(sel);
    if (el) { el.addEventListener("input", checkResetForm); }
  });

  var resetForm = $("#reset-form");
  if (resetForm) {
    resetForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var errEl = $("#reset-error");
      var showErr = function (msg) {
        if (!errEl) { return; }
        errEl.textContent = msg;
        errEl.hidden = !msg;
      };

      if (!checkResetForm()) { showErr("请先修正上面的问题"); return; }

      var acc = $("#reset-account").value.trim();
      var code = $("#reset-code").value.trim();
      var pwd = $("#reset-password").value;
      var btn = $("#btn-reset-submit");
      if (btn) { btn.disabled = true; }
      showErr("");

      window.dmApi.resetPassword(acc, code, pwd).then(function (r) {
        if (btn) { btn.disabled = false; }
        window.dmApi.setToken(r.token);
        try { localStorage.setItem("dm.account", r.account); } catch (e) {}
        if (window.dmToast) { window.dmToast("密码已重置：" + r.account); }
        route("manage", true);
      }).catch(function (err) {
        if (btn) { btn.disabled = false; }
        showErr((err && err.message) || "重置失败");
        if (resetForm) { resetForm.classList.add("is-invalid"); }
      });
    });
  }

  var regForm = $("#register-form");
  if (regForm) {
    regForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var errEl = $("#reg-error");
      var showRegErr = function (msg) {
        if (!errEl) { return; }
        errEl.textContent = msg;
        errEl.hidden = !msg;
      };

      if (!checkRegForm()) { showRegErr("请先修正上面的问题"); return; }

      var acc = $("#reg-account").value.trim();
      var pwd = $("#reg-password").value;
      var dn = ($("#reg-name") && $("#reg-name").value.trim()) || acc;
      var btn = $("#btn-reg-submit");
      if (btn) { btn.disabled = true; }
      showRegErr("");

      window.dmApi.register(acc, pwd, dn).then(function (r) {
        if (btn) { btn.disabled = false; }
         
        window.dmApi.setToken(r.token);
        try { localStorage.setItem("dm.account", r.account); } catch (e) {}
        if (window.dmToast) {
          window.dmToast("账号已创建：" + (r.display_name || r.account));
        }
        route("manage", true);
      }).catch(function (err) {
        if (btn) { btn.disabled = false; }
        showRegErr((err && err.message) || "注册失败");
        if (regForm) { regForm.classList.add("is-invalid"); }
      });
    });
  }

  var RAIL_KEY = "dm.rail.collapsed";
  var railEl = $(".rail");
  var railFold = $("#rail-fold");
  var railNav = $("#rail-nav");

  function applyRail(collapsed) {
    if (!railEl) { return; }
    railEl.classList.toggle("is-collapsed", collapsed);
    document.body.classList.toggle("rail-collapsed", collapsed);

    var doc = document.documentElement;
    if (doc.classList.contains("rail-collapsed-init")) {
      doc.classList.remove("rail-collapsed-init");
       
      requestAnimationFrame(function () { doc.classList.remove("rail-no-anim"); });
    }

    if (railFold) {
      railFold.setAttribute("aria-expanded", collapsed ? "false" : "true");
      railFold.title = collapsed ? "展开导航" : "收起导航";
      var label = railFold.querySelector(".rail__fold-text");
      if (label) { label.textContent = collapsed ? "展开" : "收起"; }
       
      var arrow = railFold.querySelector(".rail__fold-arrow");
      if (arrow) {
        arrow.innerHTML = collapsed
          ? '<path d="M6 5l5 5-5 5"/><path d="M12 5l5 5-5 5" opacity=".38"/>'
          : '<path d="M14 5l-5 5 5 5"/><path d="M8 5l-5 5 5 5" opacity=".38"/>';
      }
    }

    if (railNav) {
      Array.prototype.forEach.call(railNav.children, function (item) {
        var title = item.querySelector(".navitem__title");
        if (!title) { return; }
        if (collapsed) { item.setAttribute("title", title.textContent.trim()); }
        else { item.removeAttribute("title"); }
      });
    }

    if (window.dmCapture && typeof window.dmCapture.renderStatus === "function") {
      var st = document.querySelector("#view-hoststatus");
      if (st && !st.hidden) { window.dmCapture.renderStatus(); }
    }

    if (window.dmReview && typeof window.dmReview.fit === "function") {
      var rv = document.querySelector("#rv-app");
      if (rv && !rv.hidden) { window.dmReview.fit(); }
    }

    moveHalo(true);
  }

  function railCollapsedNow() {
     
    try {
      var v = localStorage.getItem(RAIL_KEY);
      return v === null ? true : v === "1";
    } catch (e) { return true; }
  }

  if (railFold) {
    railFold.addEventListener("click", function () {
      var next = !railEl.classList.contains("is-collapsed");
       
      var snapshot = flipCapture();
      try { localStorage.setItem(RAIL_KEY, next ? "1" : "0"); } catch (e) {}
      applyRail(next);
       
      flipPlay(snapshot);
    });
  }

  var FLIP_SEL = ".view:not([hidden]) > .card, .view:not([hidden]) .pagehead, .view:not([hidden]) .grid";
  var FLIP_MAX = 24;      

  function flipCapture() {
    var els = [];
    try {
      var nodes = document.querySelectorAll(FLIP_SEL);
      for (var i = 0; i < nodes.length && els.length < FLIP_MAX; i++) {
        var r = nodes[i].getBoundingClientRect();
        if (r.width < 2 || r.height < 2) { continue; }
        els.push({ el: nodes[i], x: r.left, y: r.top });
      }
    } catch (e) {}
    return els;
  }

  function flipPlay(prev) {
    if (!prev || !prev.length) { return; }
    var reduce = window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    var moves = [];
    prev.forEach(function (item) {
      var r = item.el.getBoundingClientRect();
      var dx = item.x - r.left;
      var dy = item.y - r.top;

      if (Math.abs(dx) < 2 && Math.abs(dy) < 2) { return; }
      moves.push({ el: item.el, dx: dx, dy: dy });
    });

    if (!moves.length) { return; }

    if (reduce) {
      return;    
    }

    moves.forEach(function (m) {
      m.el.style.transition = "none";
      m.el.style.transform = "translate(" + m.dx + "px," + m.dy + "px)";
    });

    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        moves.forEach(function (m) {
          m.el.style.transition =
            "transform .42s cubic-bezier(0.215, 0.61, 0.355, 1)";
          m.el.style.transform = "";
        });
         
        setTimeout(function () {
          moves.forEach(function (m) {
            m.el.style.transition = "";
            m.el.style.transform = "";
          });
        }, 460);
      });
    });
  }

  applyRail(railCollapsedNow());

  var TW_TEXT = "deepmaster";
  var TW_DELAY = 70;           
  var TW_START = 180;          
  var twTimer = null;
  var twIdx = 0;
  var twDone = false;

  function twEls() {
    return {
      wrap: document.querySelector("#auth-wordmark"),
      text: document.querySelector("#auth-wordmark .tw-text"),
      caret: document.querySelector("#auth-wordmark .tw-caret")
    };
  }

  var SPLIT_SEL = ".pagehead__title, .authcard__title";
  var splitDone = {};      

  function splitTitle(el, force) {
    if (!el) { return; }
    var reduce = window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce) { return; }

    if (el.dataset.splitDone === "1" && !force) { return; }
    if (!el.textContent.trim()) { return; }

    var text = el.dataset.splitText || el.textContent.trim();
    el.textContent = "";
    el.dataset.splitText = text;
    el.classList.add("is-split");
    Array.prototype.forEach.call(text, function (ch, i) {
      var sp = document.createElement("span");
      sp.className = "split-char";
      sp.style.setProperty("--i", i);
       
      sp.textContent = ch === " " ? "\u00a0" : ch;
      el.appendChild(sp);
    });
    el.dataset.splitDone = "1";
  }

  function splitVisibleTitles(view) {
    if (!view) { return; }

    var titles = view.querySelectorAll(SPLIT_SEL);
    Array.prototype.forEach.call(titles, function (el) {
      if (el.offsetParent === null) { return; }
      splitTitle(el, true);
    });
  }

  var GREET_TIPS = [
    "愿今天的每一次采集都干净利落。",
    "数据不撒谎，慢慢来反而更快。",
    "把动作做标准，比做得快更值钱。",
    "一条好数据，胜过十条将就的。",
    "摄像机不会等你，节奏比力气重要。",
    "标零做好了，后面就顺了。",
    "今天的标注，会成为明天模型的老师。",
    "累了就停一停，硬件比你更容易过劳。",
    "手稳一点，数据就干净一点。",
    "别急着跑通，先把每一步走对。",
    "好的数据集，是从不将就开始的。",
    "多看一眼曲线，少返工一次。",
    "动作的边界，往往决定数据的上限。",
    "采集不是搬砖，是在给模型喂饭。",
    "每一次标零，都是给精度上一次保险。",
    "慢下来的那几秒，通常最值钱。"
  ];

  function greetTimeslot(h) {
    if (h < 6) { return "凌晨好"; }
    if (h < 11) { return "早上好"; }
    if (h < 13) { return "中午好"; }
    if (h < 18) { return "下午好"; }
    if (h < 23) { return "晚上好"; }
    return "夜深了";
  }

  var lastTipIdx = -1;

  function pickTip(account) {
    if (GREET_TIPS.length <= 1) { return GREET_TIPS[0] || ""; }
    var i = Math.floor(Math.random() * GREET_TIPS.length);
    if (i === lastTipIdx) { i = (i + 1) % GREET_TIPS.length; }
    lastTipIdx = i;
    return GREET_TIPS[i];
  }

  var FONT_CYCLE = [
    { weight: 500, tracking: '.02em' },
    { weight: 500, tracking: '0' },
    { weight: 600, tracking: '-.01em' },
    { weight: 600, tracking: '-.022em' }
  ];
  var FINAL_FONT = 3;           
  var FONT_STEP_MS = 140;       
  var FONT_STEPS = 10;          

  var onAuthPage = false;
  var fontTimer = null;
  var fontTargets = [];         
  var fontStep = 0;

  function applyFontTo(el, i) {
    if (!el) { return; }
    var f = FONT_CYCLE[i % FONT_CYCLE.length];
    el.style.fontWeight = String(f.weight);
    el.style.letterSpacing = f.tracking;
  }

  function applyFontAll(i) {
    fontTargets.forEach(function (el) { applyFontTo(el, i); });
  }

  function stopFontCycle() {
    if (fontTimer) { clearTimeout(fontTimer); fontTimer = null; }
  }

  function startFontCycle() {
    stopFontCycle();
    fontTargets = [$(".authgreet__hi"), $("#auth-greet-tip")].filter(Boolean);
    if (!fontTargets.length) { return; }

    var reduce = window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce) { applyFontAll(FINAL_FONT); return; }    

    fontStep = 0;
     
    fontTargets.forEach(function (el) { el.classList.add("is-cycling"); });

    var tick = function () {
      applyFontAll(fontStep);
      fontStep++;
      if (fontStep >= FONT_STEPS) {
         
        applyFontAll(FINAL_FONT);
        fontTargets.forEach(function (el) { el.classList.remove("is-cycling"); });
        return;
      }
      fontTimer = setTimeout(tick, FONT_STEP_MS);
    };
    tick();
  }

  var greetEl = $("#auth-greet");
  var greetPlayed = false;

  function renderGreeting(me) {
    if (!greetEl) { return; }

    if (!me || !me.account) {
      greetEl.hidden = true;
      greetPlayed = false;
      if (paneLogin) { paneLogin.classList.remove("is-greeted"); }
      var card0 = document.querySelector(".authcard");
      if (card0) { card0.classList.remove("is-greeted"); }
      if (dmWelcome) { dmWelcome.arm(false); }
      return;
    }

    var shown = me.display_name || me.account;
    var hi = greetTimeslot(new Date().getHours()) + "，" + shown;
    var tip = pickTip(shown);

    var tipEl = $("#auth-greet-tip");
    if (tipEl) { tipEl.textContent = tip; }

    var hiEl = $("#auth-greet-time");
    var nameEl = $("#auth-greet-name");
    if (hiEl) { hiEl.textContent = greetTimeslot(new Date().getHours()) + "，"; }
    if (nameEl) { nameEl.textContent = shown; }

    var sr = $("#auth-greet-sr");
    if (sr) { sr.textContent = hi + "。" + tip; }

    if (paneLogin) { paneLogin.classList.add("is-greeted"); }
    var card = document.querySelector(".authcard");
    if (card) { card.classList.add("is-greeted"); }

    if (greetEl.hidden) { greetEl.hidden = false; }

    if (dmWelcome) { dmWelcome.arm(true); }
  }

  function stopTypewriter() {
    if (twTimer) { clearTimeout(twTimer); twTimer = null; }
  }

  function playTypewriter() {
    var el = twEls();
    if (!el.wrap || !el.text) { return; }

    var reduce = window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    stopTypewriter();

    if (reduce) {
       
      el.text.textContent = TW_TEXT;
      if (el.caret) { el.caret.style.display = "none"; }
      twDone = true;
      return;
    }

    el.text.textContent = "";
    twIdx = 0;
    twDone = false;
    if (el.caret) { el.caret.style.display = ""; }

    var tick = function () {
      twIdx += 1;
      el.text.textContent = TW_TEXT.slice(0, twIdx);
      if (twIdx >= TW_TEXT.length) {
        twDone = true;
         
        if (el.caret) {
          setTimeout(function () {
            if (twDone && el.caret) { el.caret.classList.add("is-done"); }
          }, 900);
        }
        return;
      }
      twTimer = setTimeout(tick, TW_DELAY);
    };

    twTimer = setTimeout(tick, TW_START);
  }

  function dmFreeze(src, paper) {
    if (!src) { return null; }
    var r = src.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) { return null; }
    var c;
    try { c = src.cloneNode(true); } catch (err) { return null; }
    var s0 = src.querySelectorAll("canvas"), c0 = c.querySelectorAll("canvas");
    for (var i = 0; i < s0.length; i++) {
      var a = s0[i], b = c0[i];
      if (!a || !b || !a.width || !b.width) { continue; }
      try { b.getContext("2d").drawImage(a, 0, 0); } catch (err) {}
    }
     
    c.classList.add("is-flip-clone");
    c.hidden = false;
    c.style.position = "absolute";
    c.style.left = r.left + "px";
    c.style.top = r.top + "px";
    c.style.width = r.width + "px";
    c.style.height = r.height + "px";
    c.style.margin = "0";

    var host = document.createElement("div");
    host.className = "is-flip-sheet is-flip-underlay" + (paper ? " is-flip-paper" : "");
    host.style.zIndex = "40";
    host.setAttribute("aria-hidden", "true");
    try { host.inert = true; } catch (err) {}
    host.appendChild(c);
    document.body.appendChild(host);
    return host;
  }

  function dmDropNode(node) {
    if (node && node.parentNode) { node.parentNode.removeChild(node); }
    return null;
  }

  function dmPageTurn(el, cls, done) {
    if (!el) { if (done) { done(); } return; }
    el.classList.remove("is-pageturn-in");
    void el.offsetWidth;       
    el.classList.add(cls);
    var fired = false;
    var clear = function () {
      if (fired) { return; }
      fired = true;
      el.classList.remove(cls);
      el.removeEventListener("animationend", onAnimationEnd);
      if (done) { done(); }
    };
    var onAnimationEnd = function (event) {
      if (event.target === el) { clear(); }
    };
    el.addEventListener("animationend", onAnimationEnd);
    setTimeout(clear, 900);    
  }

  var dmWelcome = (function () {
    var armed = false;         
    var target = 0;            
    var render = 0;            
    var parts = null;          
    var raf = 0;
    var buildTimer = 0;
    var dpr = 1;
    var panelW = 0, panelH = 0, panelL = 0, panelT = 0;
    var robotShift = 0;
    var robotScale = 1.38;     
    var touchY0 = 0, touchP0 = 0;
    var finishing = false;
    var pendingRewind = 0;     
    var leftAtFull = false;    
    var userDriveAt = 0;       
    var lockUntil = 0;         
    var flipping = false;      
     
    var pullT = 0, pullR = 0, lastPullAt = 0;

    var SCRUB = 560;           
    var PULL = 640;            
    var DRIFT = 130;           

    function reduce() { return dmReduceMotion(); }

    function els() {
      return {
        panel: document.querySelector(".authpanel"),
        canvas: $("#auth-fx"),
        arrow: $("#auth-next"),
        robot: $("#robot"),
        hi: $(".authgreet__hi"),
        tip: $("#auth-greet-tip")
      };
    }

    function clamp01(v) { return v < 0 ? 0 : (v > 1 ? 1 : v); }
    function smooth01(x) {
      if (x <= 0) { return 0; }
      if (x >= 1) { return 1; }
      return x * x * (3 - 2 * x);
    }

    function hsl2rgb(h, s, l) {
      s /= 100; l /= 100;
      var k = function (n) { return (n + h / 30) % 12; };
      var a = s * Math.min(l, 1 - l);
      var f = function (n) {
        return l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
      };
      return [Math.round(f(0) * 255), Math.round(f(8) * 255), Math.round(f(4) * 255)];
    }

    var SMOKE_LUT = [];
    (function () {
      for (var j = 0; j <= 20; j++) {
        var i = j / 20;
        SMOKE_LUT.push(hsl2rgb(215, 30 + 50 * (1 - i), 75 - 15 * i));
      }
    })();

    function build() {
      var e = els();
      parts = [];
      panelW = 0; panelH = 0; robotShift = 0;
      if (!e.panel || !e.canvas) { return; }

      if (e.robot) {
        e.robot.style.animation = "none";
        e.robot.style.transform = "";
        e.robot.style.transformOrigin = "50% 78%";
        var rr = e.robot.getBoundingClientRect();
        robotShift = Math.max(0, rr.left - 28);
      }

      dpr = Math.min(2, window.devicePixelRatio || 1);
      var pr = e.panel.getBoundingClientRect();
      panelW = pr.width; panelH = pr.height;
      panelL = pr.left; panelT = pr.top;
      e.canvas.width = Math.max(1, Math.round(pr.width * dpr));
      e.canvas.height = Math.max(1, Math.round(pr.height * dpr));
      e.canvas.style.width = pr.width + "px";
      e.canvas.style.height = pr.height + "px";

      [{ el: e.hi, boost: 1 }, { el: e.tip, boost: .72 }].forEach(function (t) {
        var txt = t.el ? (t.el.textContent || "").trim() : "";
        if (!txt) { return; }
        var r = t.el.getBoundingClientRect();
        if (r.width < 4 || r.height < 4) { return; }
        var w = Math.ceil(r.width), h = Math.ceil(r.height);
        var off = document.createElement("canvas");
        off.width = w; off.height = h;
        var c = off.getContext("2d");
        var st = getComputedStyle(t.el);
        c.font = st.fontWeight + " " + st.fontSize + " " + st.fontFamily;
        try { c.letterSpacing = st.letterSpacing; } catch (err) {}
        c.fillStyle = st.color;
        c.textBaseline = "top";
        var lineH = parseFloat(st.lineHeight) || parseFloat(st.fontSize) * 1.4;
        var line = "", y = 0;
        for (var i = 0; i < txt.length; i++) {
          var ch = txt.charAt(i);
          if (line && c.measureText(line + ch).width > w) {
            c.fillText(line, 0, y);
            line = ch;
            y += lineH;
          } else {
            line += ch;
          }
        }
        if (line) { c.fillText(line, 0, y); }

        var data = c.getImageData(0, 0, w, h).data;
        var step = Math.max(2, Math.round(Math.sqrt((w * h) / 2400)));
        var ox = r.left - pr.left, oy = r.top - pr.top;
        var idx = 0;
        for (var py = 0; py < h; py += step) {
          for (var px = 0; px < w; px += step) {
            var k = (py * w + px) * 4;
            if (data[k + 3] < 26) { continue; }
            parts.push({
              x: ox + px, y: oy + py, s: step,
              r0: data[k], g0: data[k + 1], b0: data[k + 2],
              a: data[k + 3] / 255,
               
              r1: Math.random(), r2: Math.random(),
              ph: idx
            });
            idx++;
          }
        }
      });
    }

    function apply(p, u) {
      var e = els();
      if (p < 0.002) { p = 0; }       
      if (e.robot) {
        if (p > 0) {
          e.robot.style.animation = "none";
          e.robot.style.transform = "translate3d(" + (-robotShift * p).toFixed(1) +
            "px,0,0) scale(" + (1 + (robotScale - 1) * p).toFixed(3) + ")";
        } else {
          e.robot.style.transform = "";
        }
      }

      var dissolving = p > 0.004;
      [e.hi, e.tip].forEach(function (el) {
        if (el) { el.style.visibility = dissolving ? "hidden" : ""; }
      });

      if (e.arrow) { e.arrow.classList.toggle("is-fading", p > 0.05); }

      if (e.canvas) {
        var ctx = e.canvas.getContext("2d");
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, panelW, panelH);
        if (dissolving && parts) {
          for (var i = 0; i < parts.length; i++) {
            var q = parts[i];
            var d = clamp01(smooth01(p * 1.6 - (q.x / panelW) * .6));
            var al = q.a * (1 - .95 * d);
            if (al <= 0.012) { continue; }
            var dx = q.x + d * DRIFT * (q.r1 - .5) * .8 +
                     Math.sin(u * 2 + q.ph) * d * 6;
            var dy = q.y - d * DRIFT * (.4 + .6 * q.r2);
            var sz = q.s * (1 + d);
             
            var k = Math.min(1, d * 2.5);
            var li = Math.round(d * 20);
            var sc = SMOKE_LUT[li];
            var cr = q.r0 + (sc[0] - q.r0) * k;
            var cg = q.g0 + (sc[1] - q.g0) * k;
            var cb = q.b0 + (sc[2] - q.b0) * k;
            ctx.fillStyle = "rgba(" + (cr | 0) + "," + (cg | 0) + "," + (cb | 0) +
                            "," + al.toFixed(3) + ")";
            ctx.fillRect(dx, dy, sz, sz);
          }
        }
      }
    }

    var lastT = 0;
    function ensureLoop() {
      if (raf) { return; }
      lastT = 0;
      raf = requestAnimationFrame(tick);
    }

    function tick(t) {
      raf = 0;
      var dt = lastT ? Math.min(64, t - lastT) : 16;
      lastT = t;

      if (armed) {
        var k = 1 - Math.exp(-dt / 90);
        render += (target - render) * k;
        if (Math.abs(target - render) < .0004) { render = target; }

        var needDraw = render > .001;
        if (needDraw || Math.abs(target - render) > 0) {
          apply(render, t / 1000);
        }

        if (target >= 1 && render >= .995 && !finishing) {
          finishing = true;
          turnForward();
        }
      }

      if (pullT > 0 || pullR > .001) {
        if (pullT > 0 && pullT < 1 && t - lastPullAt > 260) {
          pullT *= Math.exp(-dt / 220);
          if (pullT < .003) { pullT = 0; }
        }
        var pk = 1 - Math.exp(-dt / 90);
        pullR += (pullT - pullR) * pk;
        if (Math.abs(pullT - pullR) < .0004) { pullR = pullT; }
        applyPull(pullR);
        if (pullT >= 1 && pullR >= .97) {
          turnBack();
          pullT = 0; pullR = 0;
          applyPull(0);
          return;
        }
      }

      if (armed || pullT > 0 || pullR > .001) {
        raf = requestAnimationFrame(tick);
      } else {
        lastT = 0;
      }
    }

    function startRewind() {
      var t0 = performance.now();
      var step = function (t) {
        if (!armed) { finishing = false; return; }
        if (userDriveAt > t0) { finishing = false; return; }
        var k = Math.min(1, (t - t0) / 620);
        setProgress((1 - k) * (1 - k));        
        if (k < 1) { requestAnimationFrame(step); }
        else { finishing = false; }
      };
      requestAnimationFrame(step);
    }

    function setProgress(v) {
      if (!armed) { return; }
      target = clamp01(v);
      if (target > 0.004 && parts === null) { build(); }
      ensureLoop();
    }

    function arm(on) {
      if (on && reduce()) {
         
        armed = false;
        var ea = els();
        if (ea.arrow) { ea.arrow.hidden = false; ea.arrow.classList.remove("is-fading"); }
        return;
      }
      var e = els();
      if (on) {
        armed = true;
        target = 0;
        render = 0;
        finishing = false;
        parts = null;
        if (e.arrow) { e.arrow.hidden = false; }
        if (buildTimer) { clearTimeout(buildTimer); }
        var doBuild = function () { if (armed) { build(); ensureLoop(); } };
        if (document.fonts && document.fonts.ready) {
          document.fonts.ready.then(doBuild, function () {});
        }
        buildTimer = setTimeout(doBuild, 300);
        if (pendingRewind) {
           
          pendingRewind = 0;
          target = 1;
          render = 1;
          finishing = true;      
          apply(1, 0);
          startRewind();
          ensureLoop();
        } else {
          apply(0, 0);
          ensureLoop();
        }
      } else {
        armed = false;
        target = 0;
        render = 0;
        finishing = false;
        if (buildTimer) { clearTimeout(buildTimer); buildTimer = 0; }
        if (e.robot) { e.robot.style.transform = ""; }
        [e.hi, e.tip].forEach(function (el) {
          if (el) { el.style.visibility = ""; }
        });
        if (e.arrow) { e.arrow.hidden = true; e.arrow.classList.remove("is-fading"); }
        if (e.canvas) {
          var ctx = e.canvas.getContext("2d");
          ctx.setTransform(1, 0, 0, 1, 0, 0);
          ctx.clearRect(0, 0, e.canvas.width, e.canvas.height);
        }
        parts = null;
      }
    }

    function onRoute(name) {
      if (name !== "auth") { arm(false); }
      if (name !== "manage") { pullT = 0; pullR = 0; applyPull(0); }
    }

    var underlay = null;

    function freeze(src, paper) { return dmFreeze(src, paper); }
    function dropUnderlay() { underlay = dmDropNode(underlay); }
    function pageTurn(el, cls, done) { dmPageTurn(el, cls, done); }

    function turnForward() {
      if (reduce()) { route("manage", true); return; }
      var mv = $("#view-login-manage");
      flipping = true;                     
      leftAtFull = true;
      dropUnderlay();
      underlay = freeze($(".authpanel"));           
      route("manage", true);
      if (!mv) { dropUnderlay(); return; }

      var h = Math.min(Math.max(mv.getBoundingClientRect().height || 760, 420), 780);
      var turn = Math.round(h) + "px";

      var landed = false;
      var settle = function () {
        if (landed) { return; }
        landed = true;
        dropUnderlay();                      
        mv.style.position = "";              
        mv.style.zIndex = "";
        mv.style.removeProperty("--dm-turn");
        if (sheet.parentNode) { sheet.parentNode.removeChild(sheet); }   
        flipping = false;
        if (window.dmSyncNav) { window.dmSyncNav(); }    
      };

      var sheet = document.createElement("div");
      sheet.className = "is-flip-sheet";
      sheet.style.setProperty("--dm-turn", turn);
      document.body.appendChild(sheet);
      pageTurn(sheet, "is-pageturn-in", settle);

      mv.style.setProperty("--dm-turn", turn);
      mv.style.position = "relative";      
      mv.style.zIndex = "60";
      pageTurn(mv, "is-pageturn-in", null);

      if (underlay) {
        var ul = underlay;
        requestAnimationFrame(function () { ul.classList.add("is-receding"); });
      }
      lockUntil = performance.now() + 720;    
      setTimeout(settle, 1000);            
    }

    function turnBack() {
      var src = $("#view-login-manage");
      if (reduce()) { route("auth", true); return; }
      dropUnderlay();
      underlay = freeze(src, true);        
      flipping = true;
       
      pendingRewind = leftAtFull ? 1 : 0;
      leftAtFull = false;
      lockUntil = performance.now() + 760;  
      route("auth", true);
      if (!underlay) { return; }
      var ul = underlay;
       
      ul.style.transition = "transform .52s cubic-bezier(.22,.9,.24,1)";
      requestAnimationFrame(function () {
        ul.style.transform = "translate3d(0, 108%, 0)";
      });
      setTimeout(function () {
        dropUnderlay();
        flipping = false;
        if (window.dmSyncNav) { window.dmSyncNav(); }
      }, 700);
    }

    function applyPull(v) {
      var mv = $("#view-login-manage");
      if (mv) {
        mv.style.transform = v > 0.001
          ? "translate3d(0," + (v * 96).toFixed(1) + "px,0)" : "";
        mv.style.opacity = v > 0.001 ? String(1 - v * .25) : "";
      }
    }

    function setPull(v) {
      pullT = clamp01(v);
      lastPullAt = performance.now();
      ensureLoop();
    }

    window.addEventListener("wheel", function (ev) {
      var d = ev.deltaY * (ev.deltaMode === 1 ? 16 : 1);
      if (!d) { return; }
       
      if (performance.now() < lockUntil) {
        ev.preventDefault();
        return;
      }
      if (armed && !reduce()) {
        ev.preventDefault();
         
        if (finishing) { return; }
        userDriveAt = performance.now();
        setProgress(target + d / SCRUB);
        return;
      }
      if (current === "manage" && d < 0 && !reduce() &&
          (window.scrollY || 0) <= 2) {
        ev.preventDefault();
        setPull(pullT + (-d / PULL));
        return;
      }

      if (reduce() || !dmDeck || !dmDeck.has(current)) { return; }
       
      if (dmDeck.isBusy() || dmDeck.locked()) {
        ev.preventDefault();
        return;
      }
      var atBottom = (window.innerHeight + (window.scrollY || 0)) >=
                     (document.documentElement.scrollHeight - 2);
      var atTop = (window.scrollY || 0) <= 0;
      if (d > 0 && atBottom) {
        var nx = dmDeck.next(current);
        if (nx) { ev.preventDefault(); dmDeck.fwd(nx); }
        return;
      }
      if (d < 0 && atTop) {
        var pv = dmDeck.prev(current);
        if (pv) { ev.preventDefault(); dmDeck.back(pv); }
      }
    }, { passive: false });

    window.addEventListener("touchstart", function (ev) {
      if (!armed || reduce()) { return; }
      touchY0 = ev.touches[0].clientY;
      touchP0 = target;
    }, { passive: true });

    window.addEventListener("touchmove", function (ev) {
      if (!armed || reduce()) { return; }
      var dy = touchY0 - ev.touches[0].clientY;
      if (finishing) { return; }
      userDriveAt = performance.now();
      setProgress(touchP0 + dy / SCRUB);
    }, { passive: true });

    window.addEventListener("resize", function () {
      if (armed) { build(); ensureLoop(); }
    }, { passive: true });

    var arrowBtn = $("#auth-next");
    if (arrowBtn) {
      arrowBtn.addEventListener("click", function () {
        if (reduce()) { route("manage", true); return; }
        if (!armed) { return; }
        var t0 = performance.now(), p0 = target;
        var step = function (t) {
          if (!armed) { return; }
          var k = Math.min(1, (t - t0) / 650);
          setProgress(p0 + (1 - p0) * k);
          if (target < 1 && armed) { requestAnimationFrame(step); }
        };
        requestAnimationFrame(step);
      });
    }

    return {
      arm: arm,
      onRoute: onRoute,
       
      progress: function () { return render; },
      setProgress: function (v) { setProgress(v); },
       
      isFlipping: function () { return flipping; }
    };
  })();

  window.dmWelcome = dmWelcome;

  var DECK = ["manage", "status", "capture", "review", "advanced"];

  var dmDeck = (function () {
    var busy = false;
    var lockUntil = 0;

    function at(name) { return DECK.indexOf(name); }
    function has(name) { return at(name) >= 0; }
    function nextOf(name) { var i = at(name); return i >= 0 && i < DECK.length - 1 ? DECK[i + 1] : null; }
     
    function prevOf(name) { var i = at(name); return i > 0 ? DECK[i - 1] : null; }

    function settleStyles(view) {
      view.style.position = "";
      view.style.zIndex = "";
      view.style.removeProperty("--dm-scroll-offset");
      view.classList.remove("is-scroll-entering");
    }

    function turn(toName, direction) {
      var toView = $("#" + ROUTES[toName]);
      if (!toView || dmReduceMotion()) { route(toName, true); return; }

      busy = true;
      lockUntil = performance.now() + 320;
      toView.style.setProperty("--dm-scroll-offset", (direction * 28) + "px");
      toView.style.position = "relative";
      toView.style.zIndex = "60";
      toView.classList.remove("is-entering", "is-leaving", "is-pageturn-in");
      toView.classList.add("is-scroll-entering");
      route(toName, true);

      var landed = false;
      var settle = function () {
        if (landed) { return; }
        landed = true;
        settleStyles(toView);
        busy = false;
        lockUntil = performance.now() + 80;
        if (window.dmSyncNav) { window.dmSyncNav(); }
      };

      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          toView.classList.remove("is-scroll-entering");
        });
      });
      setTimeout(settle, 380);
    }

    function fwd(toName) { turn(toName, 1); }
    function back(toName) { turn(toName, -1); }

    return {
      has: has,
      next: nextOf,
      prev: prevOf,
      fwd: fwd,
      back: back,
      isBusy: function () { return busy; },
      locked: function () { return performance.now() < lockUntil; }
    };
  })();

  window.dmDeck = dmDeck;

  var dmSettings = (function () {
    var K_COMPAT = "dm.compat";
    var K_OFFLINE = "dm.offline";
    var compatOn = false;
    var offlineOn = false;

    function read(k) {
      try { return localStorage.getItem(k) === "1"; } catch (e) { return false; }
    }
    function write(k, on) {
      try { localStorage.setItem(k, on ? "1" : "0"); } catch (e) {}
    }
    function paint(id, on) {
      var b = document.getElementById(id);
      if (b) { b.setAttribute("aria-checked", on ? "true" : "false"); }
    }

    function applyCompat() {
      document.documentElement.classList.toggle("is-compat", compatOn);
    }

    function setCompat(on) {
      compatOn = !!on;
      write(K_COMPAT, compatOn);
      applyCompat();
      paint("set-compat", compatOn);
       
      if (compatOn) {
        $$(".is-flip-sheet, .is-flip-underlay").forEach(function (n) { dmDropNode(n); });
        $$(".view").forEach(function (v) {
          v.classList.remove("is-pageturn-in", "is-entering", "is-leaving");
          v.style.position = "";
          v.style.zIndex = "";
          v.style.removeProperty("--dm-turn");
        });
      }
    }

    function setOffline(on) {
      offlineOn = !!on;
      write(K_OFFLINE, offlineOn);
      paint("set-offline", offlineOn);
       
      if (offlineOn) {
        if (window.dmCapture && window.dmCapture.stopDevPoll) {
          window.dmCapture.stopDevPoll();
        }
        window.dispatchEvent(new CustomEvent("dm:offline"));
      } else {
        window.dispatchEvent(new CustomEvent("dm:online"));
      }
    }

    function wire(id, fn) {
      var b = document.getElementById(id);
      if (!b) { return; }
      b.addEventListener("click", function () {
        if (b.disabled) { return; }
        fn(b.getAttribute("aria-checked") !== "true");
      });
    }

    function loadVersion() {
      var el = document.getElementById("set-ver");
      if (!el) { return; }
      var api = window.dmApi;
      if (!api || !api.health) { return; }
      api.health().then(function (r) {
        var v = r && r.version;
        el.textContent = v ? "当前版本 " + v : "当前版本 —";
        if (v) { el.setAttribute("title", "deepmaster " + v); }
      }).catch(function () {
        el.textContent = "当前版本 —";
      });
    }

    function refreshPairingAccess() {
      var row = document.getElementById("set-agent-pairing-row");
      var btn = document.getElementById("set-agent-pairing-reset");
      var api = window.dmApi;
      if (!row || !btn) { return; }
      row.hidden = true;
      if (!api || !api.me || !api.hasToken || !api.hasToken()) { return; }
      api.me().then(function (me) {
        row.hidden = !me || me.role !== "admin";
      }).catch(function () { row.hidden = true; });
    }

    function resetAgentPairing() {
      var btn = document.getElementById("set-agent-pairing-reset");
      var modal = window.dmCapture && window.dmCapture.openModal;
      if (!btn || !modal || !window.dmApi || !window.dmApi.resetAgentPairing) { return; }
      modal({
        title: "重置令牌？",
        html: "<p>将扫描并重置所有扫描到的采集端及服务端对应的配对信息</p>",
        ok: "重置令牌",
        cancel: "取消",
        danger: true,
        anchor: btn
      }).then(function (confirmed) {
        if (!confirmed) { return; }
        btn.disabled = true;
        return window.dmApi.resetAgentPairing().then(function (result) {
          window.dispatchEvent(new CustomEvent("dm:agent-reset"));
          var devices = result && result.devices || [];
          var failed = devices.filter(function (device) { return !device.reset; });
          var details = failed.length ? "<ul>" + failed.map(function (device) {
            return "<li>" + esc(device.host || device.ip || "采集端") + "：" +
              esc(device.error || "重置失败") + "</li>";
          }).join("") + "</ul>" : "";
          return modal({
            title: failed.length ? "部分设备重置完成" : "配对重置完成",
            html: "<p>已重置 " + Number(result && result.reset_count || 0) +
              " 台采集端，共扫描到 " + Number(result && result.total || 0) + " 台。</p>" + details,
            ok: "知道了", noCancel: true, anchor: btn
          });
        }).catch(function (e) {
          toast((e && e.message) || "重置配对失败");
        }).then(function () {
          btn.disabled = false;
        });
      });
    }

    function init() {
      compatOn = read(K_COMPAT);
      offlineOn = read(K_OFFLINE);
      applyCompat();
      paint("set-compat", compatOn);
      paint("set-offline", offlineOn);
      paint("set-vlm", false);       

      wire("set-compat", setCompat);
      wire("set-offline", setOffline);
      var resetPairingBtn = document.getElementById("set-agent-pairing-reset");
      if (resetPairingBtn) { resetPairingBtn.addEventListener("click", resetAgentPairing); }
      loadVersion();
    }

    return {
      init: init,
      compat: function () { return compatOn; },
      offline: function () { return offlineOn; },
      setCompat: setCompat,
      setOffline: setOffline,
      refreshPairingAccess: refreshPairingAccess
    };
  })();

  window.dmSettings = dmSettings;

  dmSettings.init();

  ["#stat-capture-today", "#stat-capture-month",
   "#stat-annotate-today", "#stat-annotate-month"].forEach(function (sel) {
    var el = $(sel);
    if (el && el.textContent.trim() === "") { el.textContent = ""; }
  });

  var initial = (location.hash || "#auth").slice(1);
  route(initial || "auth", false);
  if (!location.hash) { history.replaceState({ route: "auth" }, "", "#auth"); }

  syncAxis();
  moveHalo(true);
   
  window.dmSplitTitle = function (el) { splitTitle(el); };

})();

/*
Copyright (c) 2026 deeprobotics
Author Chenyu Guo
Date November 2026
*/
/* 管理界面：活动统计、数据规模与账号管理。 */
(function () {
  "use strict";

  var $  = function (s, r) { return (r || document).querySelector(s); };

  var ROLE_NAME = { user: "用户", operator: "用户", reviewer: "用户", admin: "管理员" };

  function fmtDur(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = sec % 60;
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    if (h > 0) { return h + ":" + p(m) + ":" + p(s); }
    return p(m) + ":" + p(s);
  }

  function humanSize(n) {
    if (!n) { return "0 B"; }
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)) + " " + u[i];
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  var ACTION_NAME = {
    login: "登录",
    session_start: "开始采集",
    session_end: "结束采集",
    review: "审核",
    review_batch: "批量审核",
    record_delete: "删除录制",
    export: "导出",
    bootstrap_admin: "初始化管理员",
    run_t1: "启动机械臂",
    run_t2: "启动采集程序",
    run_z: "启动机械臂",
    run_tare: "手臂标零",
    run_stop: "停止采集",
    run_preflight: "采集预检"
  };

  function renderAccount(me, stats) {
    var nameEl = $("#mgr-account-name");
    var metaEl = $("#mgr-account-meta");
    var avatar = $("#mgr-avatar");

    if (!me) {
      if (nameEl) { nameEl.textContent = "未登录"; }
      if (metaEl) { metaEl.textContent = ""; }
      if (avatar) { avatar.textContent = "?"; }
      var rc0 = $("#mgr-reset-code");
      if (rc0) { rc0.hidden = true; }
      return;
    }

    var shown = me.display_name || me.account;
    if (nameEl) { nameEl.textContent = shown; }
    if (avatar) { avatar.textContent = (shown || "?").slice(0, 1).toUpperCase(); }

    var bits = [ROLE_NAME[me.role] || me.role];
    if (me.display_name && me.display_name !== me.account) { bits.push("账号 " + me.account); }
    if (me.last_login_at) { bits.push("上次登录 " + me.last_login_at); }
    if (metaEl) { metaEl.textContent = bits.join("  "); }

    var rcBtn = $("#mgr-reset-code");
    if (rcBtn) { rcBtn.hidden = me.role !== "admin"; }
  }

  function renderMetrics(stats) {
    var d = stats || {};
    var cap = d.capture || {};
    var ann = d.annotate || {};

    var setText = function (sel, sec) {
      var el = $(sel);
      if (el) { el.textContent = sec > 0 ? fmtDur(sec) : ""; }
    };

    setText("#stat-capture-today", cap.today_s || 0);
    setText("#stat-capture-month", cap.month_s || 0);
    setText("#stat-annotate-today", ann.today_s || 0);
    setText("#stat-annotate-month", ann.month_s || 0);
  }

  function renderDataset(stats) {
    var el = $("#mgr-dataset");
    if (!el) { return; }
    var ds = (stats && stats.dataset) || {};

    var rows = [
      ["数据集", (ds.datasets || 0) + " 个"],
      ["录制文件", (ds.records || 0) + " 个"],
      ["占用空间", humanSize(ds.bytes || 0)],
      ["磁盘剩余", ds.free_bytes != null ? humanSize(ds.free_bytes) : "—"]
    ];

    el.innerHTML = "";
    rows.forEach(function (pair) {
      var wrap = document.createElement("div");
      var dt = document.createElement("dt");
      dt.textContent = pair[0];
      var dd = document.createElement("dd");
      dd.textContent = pair[1];
      wrap.appendChild(dt);
      wrap.appendChild(dd);
      el.appendChild(wrap);
    });
  }

  function renderReview(stats) {
    var el = $("#mgr-review");
    var restEl = $("#mgr-rest");
    if (!el) { return; }

    var rv = (stats && stats.review) || {};
    var episodes = rv.episodes || 0;
    var decided = rv.decided || 0;
    var rest = rv.pending_rest != null
      ? rv.pending_rest
      : Math.max(0, episodes - decided);

    if (restEl) {
      restEl.textContent = rest > 0 ? ("剩 " + rest + " 条") : "已全部审完";
      restEl.className = "mgr-rest" + (rest > 0 ? " is-todo" : " is-clear");
    }

    el.innerHTML = "";

    var total = Math.max(episodes, decided + rest);
    var bar = document.createElement("div");
    bar.className = "mgr-review__bar";

    var segDone = document.createElement("i");
    segDone.className = "is-done";
    segDone.style.width = (total ? (decided / total * 100) : 0) + "%";

    var segRest = document.createElement("i");
    segRest.className = "is-todo";
    segRest.style.width = (total ? (rest / total * 100) : 0) + "%";

    if (!total) {
      var empty = document.createElement("i");
      empty.className = "is-empty";
      empty.style.width = "100%";
      bar.appendChild(empty);
    } else {
      bar.appendChild(segDone);
      bar.appendChild(segRest);
    }
    el.appendChild(bar);

    var legend = document.createElement("dl");
    legend.className = "mgr-review__legend";
    [
      { key: "done", label: "已审", n: decided },
      { key: "todo", label: "未审", n: rest }
    ].forEach(function (it) {
      var wrap = document.createElement("div");
      var dt = document.createElement("dt");
      dt.className = "is-" + it.key;
      dt.innerHTML = "<i></i>" + it.label;
      var dd = document.createElement("dd");
      dd.textContent = String(it.n);
      wrap.appendChild(dt);
      wrap.appendChild(dd);
      legend.appendChild(wrap);
    });
    el.appendChild(legend);

    var note = document.createElement("p");
    note.className = "mgr-review__total";
    note.textContent = total
      ? ("共 " + total + " 条，已审 " + decided + " 条")
      : "还没有可审核的数据";
    el.appendChild(note);
  }

  var RING_R = 78;                        
  var RING_C = 2 * Math.PI * RING_R;

  var HUE = {
    good:    "#30d158",    
    pending: "#ffd60a",    
    bad:     "#ff453a",    
    info:    "#0071e3"     
  };

  var TINT = {
    good:    ["#248a3d", "#30d158", "#6ee08a", "#a6ecb8"],
    pending: ["#d9a900", "#ffd60a", "#ffdf4d", "#ffe985"],
    bad:     ["#d92b21", "#ff453a", "#ff7365", "#ff9d92"],
    info:    ["#0063c7", "#0071e3", "#4a9ff0", "#8ac4f6"]
  };

  function tintOf(ramp, i) {
    return ramp[Math.min(i, ramp.length - 1)];
  }

  function drawRing(rootSel, items, centerText, legendEl, tipEl, unitFn) {
    var root = document.querySelector(rootSel);
    if (!root) { return; }
    var arcLayer = root.querySelector(".ov-ring__arcs");
    if (!arcLayer) { return; }
    arcLayer.innerHTML = "";

    var total = items.reduce(function (a, it) { return a + (it.value || 0); }, 0);
    if (legendEl) { legendEl.innerHTML = ""; }

    if (!total) {
       
      if (tipEl) { tipEl.hidden = true; }
      return;
    }

    var cursor = 0;
    var TAU = Math.PI * 2;
    items.forEach(function (it, i) {
      var v = it.value || 0;
      if (!v) { return; }

      var frac = v / total;
      var startA = cursor * TAU - Math.PI / 2;
      var endA = startA + frac * TAU;

      var gapA = Math.min(0.014, frac * TAU * 0.12);
      var a0 = startA + gapA / 2;
      var a1 = Math.max(endA - gapA / 2, a0 + 0.004);

      var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", arcPath(a0, a1));
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", it.color);
       
      path.setAttribute("stroke-linecap", "butt");
      path.setAttribute("class", "ov-arc");
      path.setAttribute("data-idx", i);
      if (it.label) { path.setAttribute("aria-label", it.label); }

      path.addEventListener("mouseenter", function () {
        path.classList.add("is-hot");
        if (tipEl && it.tip) {
          tipEl.innerHTML = it.tip;
          tipEl.hidden = false;
           
          var mid = (a0 + a1) / 2;
          var rr = RING_R * 0.74;
          var px = 100 + Math.cos(mid) * rr;
          var py = 100 + Math.sin(mid) * rr;
          tipEl.style.left = (px / 200 * 100) + "%";
          tipEl.style.top = (py / 200 * 100) + "%";
        }
      });
      path.addEventListener("mouseleave", function () {
        path.classList.remove("is-hot");
        if (tipEl) { tipEl.hidden = true; }
      });

      arcLayer.appendChild(path);
      cursor += frac;

      if (legendEl) {
        var li = document.createElement("li");
        li.className = "ov-legend__item";
        var dot = document.createElement("i");
        dot.style.background = it.color;
        var nm = document.createElement("span");
        nm.className = "ov-legend__name";
        nm.textContent = it.label || "";
        var val = document.createElement("b");
        val.className = "ov-legend__val";
        val.textContent = unitFn ? unitFn(v) : String(v);
        li.appendChild(dot);
        li.appendChild(nm);
        li.appendChild(val);
        legendEl.appendChild(li);
      }
    });
  }

  function arcPath(a0, a1) {
    var x0 = 100 + RING_R * Math.cos(a0);
    var y0 = 100 + RING_R * Math.sin(a0);
    var x1 = 100 + RING_R * Math.cos(a1);
    var y1 = 100 + RING_R * Math.sin(a1);

    var sweep = Math.abs(a1 - a0);
    var large = sweep > Math.PI ? 1 : 0;
     
    var dir = (a1 >= a0) ? 1 : 0;

    return "M" + x0.toFixed(2) + " " + y0.toFixed(2)
      + " A" + RING_R + " " + RING_R + " 0 " + large + " " + dir + " "
      + x1.toFixed(2) + " " + y1.toFixed(2);
  }

  function fmtNum(n) { return Number(n || 0).toLocaleString("zh-CN"); }

  function fmtDur(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    if (h >= 1) { return h + " 小时" + (m ? " " + m + " 分" : ""); }
    if (m >= 1) { return m + " 分钟"; }
    return sec + " 秒";
  }

  function renderQuality(ov) {
    var q = (ov && ov.quality) || { good: 0, pending: 0, bad: 0, total: 0 };
    var total = q.total || (q.good + q.pending + q.bad);

    var topGood = (q.good_by && q.good_by[0]) || null;
    var topBad = (q.bad_by && q.bad_by[0]) || null;

    var items = [
      { key: "good", value: q.good, color: TINT.good[0], label: "合格",
        tip: "<b>合格</b>" + fmtNum(q.good) + " 条"
           + (topGood ? "<br>合格最多：" + esc(topGood.name) + "（" + fmtNum(topGood.count) + " 条）" : "") },
      { key: "pending", value: q.pending, color: TINT.pending[0], label: "待定",
        tip: "<b>待定</b>" + fmtNum(q.pending) + " 条" },
      { key: "bad", value: q.bad, color: TINT.bad[0], label: "剔除",
        tip: "<b>剔除</b>" + fmtNum(q.bad) + " 条"
           + (topBad ? "<br>剔除最多：" + esc(topBad.name) + "（" + fmtNum(topBad.count) + " 条）" : "") }
    ];

    drawRing('[data-ring="quality"]', items, null,
             $("#ov-q-legend"), $("#ov-q-tip"));
    var n = $("#ov-q-total");
    if (n) { n.textContent = fmtNum(total); }
  }

  function renderImprove(ov) {
    var im = (ov && ov.improve) || { pending: [], bad: [], pending_total: 0, bad_total: 0 };

    var items = [];
    var iPend = 0, iBad = 0;

    (im.pending || []).forEach(function (r) {
      items.push({
        value: r.count,
        color: tintOf(TINT.pending, iPend),
        label: r.reason,
        tip: "<b>" + esc(r.reason) + "</b><br>待定 " + fmtNum(r.count) + " 条"
      });
      iPend++;
    });
    (im.bad || []).forEach(function (r) {
      items.push({
        value: r.count,
        color: tintOf(TINT.bad, iBad),
        label: r.reason,
        tip: "<b>" + esc(r.reason) + "</b><br>剔除 " + fmtNum(r.count) + " 条"
      });
      iBad++;
    });

    drawRing('[data-ring="improve"]', items, null,
             $("#ov-i-legend"), $("#ov-i-tip"));
    var n = $("#ov-i-total");
    if (n) { n.textContent = fmtNum((im.pending_total || 0) + (im.bad_total || 0)); }
  }

  function renderDuration(ov) {
    var card = $("#ov-duration");
    if (!card) { return; }
    if (!ov || !ov.duration_visible) { card.hidden = true; return; }
    card.hidden = false;

    var du = ov.duration || { users: [], total_s: 0, month: "" };
    var items = (du.users || []).filter(function (u) { return u.seconds > 0; })
      .map(function (u, i) {
        return {
          value: u.seconds,
          color: tintOf(TINT.info, i),
          label: u.name || u.account,
          tip: "<b>" + esc(u.name || u.account) + "</b><br>"
             + fmtDur(u.seconds) + " " + fmtNum(u.sessions) + " 次采集"
        };
      });

    drawRing('[data-ring="duration"]', items, null,
             $("#ov-d-legend"), $("#ov-d-tip"),
             function (v) { return fmtDur(v); });
     
    var n = $("#ov-d-total");
    if (n) {
      var tot = du.total_s || 0;
      var hrs = tot / 3600;
      n.textContent = hrs >= 1
        ? (hrs >= 10 ? Math.round(hrs) : hrs.toFixed(1)) + " 小时"
        : fmtDur(tot);
    }
    var m = $("#ov-d-month");
    if (m) {
      var mm = (du.month || "").split("-");
      m.textContent = mm.length === 2 ? (mm[0] + " 年 " + Number(mm[1]) + " 月") : "本月";
    }
  }

  var ERROR_KIND_NAME = { js: "前端脚本", api: "接口", promise: "未捕获异常" };

  function shortReason(reason) {
    var t = String(reason || "");
     
    var m = t.match(/^(GET|POST|PATCH|DELETE)\s+\S+\s*(?:→|返回)\s*(<n>|\d+)/);
    if (m) { return "接口 " + (m[2] === "<n>" ? "错误" : m[2]); }
     
    t = t.replace(/^Uncaught\s+/i, "");
    t = t.replace(/\s*\(reading[^)]*\)/gi, "");
    t = t.replace(/\s*\(reading.*$/gi, "");

    if (/Cannot read propert/i.test(t)) { return "读取空对象"; }
    if (/is not a function/i.test(t)) { return "调用非函数"; }
    if (/is not defined/i.test(t)) { return "变量未定义"; }
    if (/NetworkError|Failed to fetch/i.test(t)) { return "网络请求失败"; }
    if (/timeout/i.test(t)) { return "请求超时"; }

    if (t.length > 14) { t = t.slice(0, 14) + "…"; }
    return t || "未知错误";
  }

  function renderErrors(ov) {
    var er = (ov && ov.errors) || { items: [], total: 0, by_kind: {} };
    var items = (er.items || []).map(function (it, i) {
      var kinds = (it.kinds || []).map(function (k) {
        return ERROR_KIND_NAME[k] || k;
      }).join("、" );
      return {
        value: it.count,
        color: tintOf(TINT.bad, i),
        label: shortReason(it.reason),
        tip: "<b>" + esc(it.reason) + "</b><br>"
           + fmtNum(it.count) + " 次"
           + (kinds ? " " + esc(kinds) : "")
           + (it.last_at ? "<br>最近 " + esc(String(it.last_at).slice(5, 16)) : "")
      };
    });

    drawRing('[data-ring="errors"]', items, null,
             $("#ov-e-legend"), $("#ov-e-tip"));
    var n = $("#ov-e-total");
    if (n) { n.textContent = fmtNum(er.total || 0); }
  }

  function renderOverview(ov) {
    if (!ov) { return; }
    renderQuality(ov);
    renderImprove(ov);
    renderDuration(ov);
    renderErrors(ov);
  }

  var ACTION_NAME = {
    login: "登录",
    session_start: "开始采集",
    session_end: "结束采集",
    review: "审核",
    review_batch: "批量审核",
    record_delete: "删除录制",
    export: "导出",
    bootstrap_admin: "初始化管理员",
    run_t1: "启动机械臂",
    run_t2: "启动采集程序",
    run_z: "启动机械臂",
    run_tare: "手臂标零",
    run_stop: "停止采集",
    run_preflight: "采集预检"
  };

  function renderAccount(me, stats) {
    var nameEl = $("#mgr-account-name");
    var metaEl = $("#mgr-account-meta");
    var avatar = $("#mgr-avatar");

    if (!me) {
      if (nameEl) { nameEl.textContent = "未登录"; }
      if (metaEl) { metaEl.textContent = ""; }
      if (avatar) { avatar.textContent = "?"; }
      var rc0 = $("#mgr-reset-code");
      if (rc0) { rc0.hidden = true; }
      return;
    }

    var shown = me.display_name || me.account;
    if (nameEl) { nameEl.textContent = shown; }
    if (avatar) { avatar.textContent = (shown || "?").slice(0, 1).toUpperCase(); }

    var bits = [ROLE_NAME[me.role] || me.role];
    if (me.display_name && me.display_name !== me.account) { bits.push("账号 " + me.account); }
    if (me.last_login_at) { bits.push("上次登录 " + me.last_login_at); }
    if (metaEl) { metaEl.textContent = bits.join("  "); }

    var rcBtn = $("#mgr-reset-code");
    if (rcBtn) { rcBtn.hidden = me.role !== "admin"; }
  }

  function renderMetrics(stats) {
    var d = stats || {};
    var cap = d.capture || {};
    var ann = d.annotate || {};

    var setText = function (sel, sec) {
      var el = $(sel);
      if (el) { el.textContent = sec > 0 ? fmtDur(sec) : ""; }
    };

    setText("#stat-capture-today", cap.today_s || 0);
    setText("#stat-capture-month", cap.month_s || 0);
    setText("#stat-annotate-today", ann.today_s || 0);
    setText("#stat-annotate-month", ann.month_s || 0);
  }

  function renderDataset(stats) {
    var el = $("#mgr-dataset");
    if (!el) { return; }
    var ds = (stats && stats.dataset) || {};

    var rows = [
      ["数据集", (ds.datasets || 0) + " 个"],
      ["录制文件", (ds.records || 0) + " 个"],
      ["占用空间", humanSize(ds.bytes || 0)],
      ["磁盘剩余", ds.free_bytes != null ? humanSize(ds.free_bytes) : "—"]
    ];

    el.innerHTML = "";
    rows.forEach(function (pair) {
      var wrap = document.createElement("div");
      var dt = document.createElement("dt");
      dt.textContent = pair[0];
      var dd = document.createElement("dd");
      dd.textContent = pair[1];
      wrap.appendChild(dt);
      wrap.appendChild(dd);
      el.appendChild(wrap);
    });
  }

  function renderReview(stats) {
    var el = $("#mgr-review");
    var restEl = $("#mgr-rest");
    if (!el) { return; }

    var rv = (stats && stats.review) || {};
    var episodes = rv.episodes || 0;
    var decided = rv.decided || 0;
    var rest = rv.pending_rest != null
      ? rv.pending_rest
      : Math.max(0, episodes - decided);

    if (restEl) {
      restEl.textContent = rest > 0 ? ("剩 " + rest + " 条") : "已全部审完";
      restEl.className = "mgr-rest" + (rest > 0 ? " is-todo" : " is-clear");
    }

    el.innerHTML = "";

    var total = Math.max(episodes, decided + rest);
    var bar = document.createElement("div");
    bar.className = "mgr-review__bar";

    var segDone = document.createElement("i");
    segDone.className = "is-done";
    segDone.style.width = (total ? (decided / total * 100) : 0) + "%";

    var segRest = document.createElement("i");
    segRest.className = "is-todo";
    segRest.style.width = (total ? (rest / total * 100) : 0) + "%";

    if (!total) {
      var empty = document.createElement("i");
      empty.className = "is-empty";
      empty.style.width = "100%";
      bar.appendChild(empty);
    } else {
      bar.appendChild(segDone);
      bar.appendChild(segRest);
    }
    el.appendChild(bar);

    var legend = document.createElement("dl");
    legend.className = "mgr-review__legend";
    [
      { key: "done", label: "已审", n: decided },
      { key: "todo", label: "未审", n: rest }
    ].forEach(function (it) {
      var wrap = document.createElement("div");
      var dt = document.createElement("dt");
      dt.className = "is-" + it.key;
      dt.innerHTML = "<i></i>" + it.label;
      var dd = document.createElement("dd");
      dd.textContent = String(it.n);
      wrap.appendChild(dt);
      wrap.appendChild(dd);
      legend.appendChild(wrap);
    });
    el.appendChild(legend);

    var note = document.createElement("p");
    note.className = "mgr-review__total";
    note.textContent = total
      ? ("共 " + total + " 条，已审 " + decided + " 条")
      : "还没有可审核的数据";
    el.appendChild(note);
  }

  var acctMe = null;           

  function acctBusy(li, on) {
    if (li) { li.classList.toggle("is-busy", !!on); }
  }

  function closestOf(el, sel) {
    if (!el || el.nodeType !== 1) { return null; }
    return el.closest ? el.closest(sel) : null;
  }

  function acctRow(u) {
    var isSelf = u.account === acctMe;
    var isAdmin = u.role === "admin";

    var li = document.createElement("li");
    li.className = "acct-row" + (isAdmin ? " is-admin" : "");
    li.setAttribute("data-account", u.account);

    var ava = document.createElement("span");
    ava.className = "acct-ava";
    ava.setAttribute("aria-hidden", "true");
    ava.textContent = String(u.display_name || u.account || "?").slice(0, 1).toUpperCase();

    var who = document.createElement("div");
    who.className = "acct-who";

    var nm = document.createElement("p");
    nm.className = "acct-name";
    var nmT = document.createElement("span");
    nmT.className = "acct-name__t";
    nmT.textContent = u.display_name || u.account;
    nm.appendChild(nmT);
    if (isSelf) {
      var meTag = document.createElement("span");
      meTag.className = "acct-me";
      meTag.textContent = "你";
      nm.appendChild(meTag);
    }
    who.appendChild(nm);

    var meta = document.createElement("p");
    meta.className = "acct-meta";
    meta.textContent = u.account + " " +
      (u.last_login_at ? "上次登录 " + u.last_login_at : "从未登录");
    who.appendChild(meta);

    var seg = document.createElement("div");
    seg.className = "acct-seg";
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", (u.display_name || u.account) + " 的权限");
    [["user", "用户"], ["admin", "管理员"]].forEach(function (p) {
      var b = document.createElement("button");
      b.type = "button";
      b.setAttribute("data-role", p[0]);
      b.textContent = p[1];
      var on = u.role === p[0];
      if (on) { b.className = "is-on"; }
      b.setAttribute("aria-pressed", on ? "true" : "false");
      if (isSelf) {
        b.disabled = true;
        b.title = "不能修改自己的权限";
      }
      seg.appendChild(b);
    });

    var del = document.createElement("button");
    del.type = "button";
    del.className = "btn btn--tertiary btn--sm acct-del";
    del.textContent = "删除";
    if (isSelf) {
      del.disabled = true;
      del.title = "不能删除自己的账号";
    }

    li.appendChild(ava);
    li.appendChild(who);
    li.appendChild(seg);
    li.appendChild(del);
    return li;
  }

  function renderAccounts(d) {
    var listEl = $("#acct-list");
    var emptyEl = $("#acct-empty");
    var subEl = $("#acct-sub");
    if (!listEl) { return; }

    if (d && d.me) { acctMe = d.me; }
    var users = (d && d.users) || [];

    listEl.innerHTML = "";
    users.forEach(function (u) { listEl.appendChild(acctRow(u)); });

    if (emptyEl) { emptyEl.hidden = users.length > 0; }
    if (subEl) {
      var nAdmin = users.filter(function (u) { return u.role === "admin"; }).length;
      subEl.textContent = "共 " + users.length + " 个账号" +
        (nAdmin ? "，" + nAdmin + " 个管理员" : "");
    }
  }

  function loadAccounts(me) {
    var sec = $("#acct-sec");
    var isAdmin = !!(me && me.role === "admin");
    if (sec) { sec.hidden = !isAdmin; }
    if (!isAdmin) { return; }

    var listEl = $("#acct-list");
    if (!listEl) { return; }
    if (me && me.account) { acctMe = me.account; }

    var pending = document.createElement("li");
    pending.className = "acct-row";
    pending.innerHTML = '<div class="acct-who"><p class="acct-name">' +
      '<span class="acct-name__t">加载中…</span></p></div>';
    listEl.innerHTML = "";
    listEl.appendChild(pending);

    window.dmApi.users().then(renderAccounts).catch(function (e) {
      listEl.innerHTML = "";
      var p = document.createElement("li");
      p.className = "acct-empty";
      p.textContent = "账号列表读取失败：" + ((e && e.message) || e);
      listEl.appendChild(p);
    });
  }

  function onRoleClick(ev) {
    var btn = closestOf(ev.target, ".acct-seg button[data-role]");
    if (!btn || btn.disabled) { return; }
    var li = closestOf(btn, ".acct-row");
    if (!li) { return; }
    var account = li.getAttribute("data-account");
    var role = btn.getAttribute("data-role");
    if (!account || !role) { return; }

    acctBusy(li, true);
    window.dmApi.setUserRole(account, role).then(function () {
      return window.dmApi.users();
    }).then(function (d) {
      renderAccounts(d);
      if (window.dmToast) {
        window.dmToast(account + " 的权限改为" + (ROLE_NAME[role] || role));
      }
    }).catch(function (e) {
      acctBusy(li, false);
      if (window.dmToast) { window.dmToast((e && e.message) || "权限修改失败"); }
    });
  }

  function onDeleteClick(ev) {
    var btn = closestOf(ev.target, ".acct-del");
    if (!btn || btn.disabled) { return; }
    var li = closestOf(btn, ".acct-row");
    var account = li && li.getAttribute("data-account");
    if (!account) { return; }

    var nameEl = li.querySelector(".acct-name__t");
    var shown = nameEl ? nameEl.textContent : account;
    var modal = window.dmCapture && window.dmCapture.openModal;
    if (!modal) { return; }

    modal({
      title: "删除账号",
      html:
        "<p>确定删除 <b>" + esc(shown) + "</b>（账号 " + esc(account) + "）？</p>" +
        '<p class="cap-name__hint">该账号审过的数据会保留，只是不再记录审核人。' +
        "删除后不能恢复。</p>",
      ok: "删除", cancel: "取消", danger: true, anchor: btn
    }).then(function (yes) {
      if (!yes) { return; }
      acctBusy(li, true);
      window.dmApi.deleteUser(account).then(function () {
        return window.dmApi.users();
      }).then(function (d) {
        renderAccounts(d);
        if (window.dmToast) { window.dmToast("已删除 " + shown); }
      }).catch(function (e) {
        acctBusy(li, false);
        if (window.dmToast) { window.dmToast((e && e.message) || "删除失败"); }
      });
    });
  }

  function openAddAccount() {
    var modal = window.dmCapture && window.dmCapture.openModal;
    if (!modal) { return; }

    var p = modal({
      title: "新增账号",
      html:
        '<div class="acct-form">' +
          '<label class="acct-form__row"><span>用户名</span>' +
            '<input class="input cap-name__in" id="acct-f-name" type="text" ' +
              'placeholder="显示名称，可用中文" spellcheck="false"></label>' +
          '<label class="acct-form__row"><span>账号</span>' +
            '<input class="input cap-name__in" id="acct-f-account" type="text" ' +
              'placeholder="字母或数字，3-32 位" spellcheck="false" autocomplete="off"></label>' +
          '<label class="acct-form__row"><span>密码</span>' +
            '<input class="input cap-name__in" id="acct-f-password" type="password" ' +
              'placeholder="至少 8 位，不能是纯数字" autocomplete="new-password"></label>' +
          '<div class="acct-form__row"><span>权限</span>' +
            '<div class="acct-seg" id="acct-f-role" role="group" aria-label="新账号权限">' +
              '<button type="button" data-role="user" class="is-on" aria-pressed="true">用户</button>' +
              '<button type="button" data-role="admin" aria-pressed="false">管理员</button>' +
            "</div></div>" +
        "</div>" +
        '<p class="cap-name__hint">用户名留空就用账号名显示。</p>',
      ok: "创建", cancel: "取消"
    });

    var seg = $("#acct-f-role");
    if (seg) {
      seg.addEventListener("click", function (ev) {
        var b = closestOf(ev.target, "button[data-role]");
        if (!b) { return; }
        Array.prototype.forEach.call(seg.querySelectorAll("button"), function (x) {
          var on = x === b;
          x.classList.toggle("is-on", on);
          x.setAttribute("aria-pressed", on ? "true" : "false");
        });
      });
    }

    p.then(function (yes) {
      if (!yes) { return; }
      var roleEl = $("#acct-f-role .is-on");
      var role = roleEl ? roleEl.getAttribute("data-role") : "user";
      var nameEl = $("#acct-f-name");
      var accEl = $("#acct-f-account");
      var pwdEl = $("#acct-f-password");
      var name = nameEl ? nameEl.value.trim() : "";
      var acc = accEl ? accEl.value.trim() : "";
      var pwd = pwdEl ? pwdEl.value : "";

      if (!acc) {
        if (window.dmToast) { window.dmToast("请填写账号"); }
        return;
      }

      window.dmApi.createUser(acc, pwd, name, role).then(function () {
        return window.dmApi.users();
      }).then(function (d) {
        renderAccounts(d);
        if (window.dmToast) {
          window.dmToast("已创建 " + (name || acc) + "（" + (ROLE_NAME[role] || role) + "）");
        }
      }).catch(function (e) {
        if (window.dmToast) { window.dmToast((e && e.message) || "创建失败"); }
      });
    });
  }

  bind("acct-new", openAddAccount);

  (function () {
    var listEl = $("#acct-list");
    if (!listEl) { return; }
    listEl.addEventListener("click", function (ev) {
      if (closestOf(ev.target, ".acct-del")) { onDeleteClick(ev); return; }
      if (closestOf(ev.target, ".acct-seg button[data-role]")) { onRoleClick(ev); }
    });
  })();

  var loading = false;

  function refresh() {
    if (!window.dmApi || loading) { return; }
    loading = true;

    var pMe = window.dmApi.me().catch(function () { return null; });
    var pStats = window.dmApi.stats().catch(function () { return null; });
    var pOv = window.dmApi.overview().catch(function () { return null; });

    Promise.all([pMe, pStats, pOv]).then(function (res) {
      var me = res[0], stats = res[1], ov = res[2];
      renderAccount(me, stats);
      if (stats) {
        renderMetrics(stats);
        renderDataset(stats);
        renderReview(stats);
      }
      renderOverview(ov);
      loading = false;
      loadAccounts(me);
    });
  }

  function bind(id, fn) {
    var el = $("#" + id);
    if (el) { el.addEventListener("click", fn); }
  }

  bind("mgr-reset-code", function () {
     
    if (!window.dmApi || !window.dmApi.issueResetCode) {
      if (window.dmToast) { window.dmToast("当前版本不支持签发重置码"); }
      return;
    }
    if (window.dmApi.me) {
      window.dmApi.me().then(function (me) {
        if (!me || me.role !== "admin") {
          if (window.dmToast) { window.dmToast("只有管理员可以签发重置码"); }
          return;
        }
        doIssueResetCode();
      }).catch(function () {
        if (window.dmToast) { window.dmToast("无法确认权限"); }
      });
      return;
    }
    doIssueResetCode();
  });

  function doIssueResetCode() {
    var modal = window.dmCapture && window.dmCapture.openModal;
    if (!modal) { return; }

    modal({
      title: "签发重置码",
      html: '<div class="cap-name">' +
              '<input class="input cap-name__in" id="rc-account" type="text" ' +
                'placeholder="需要重置的账号" spellcheck="false">' +
              '<p class="cap-name__hint">码 30 分钟内有效，只能使用一次</p>' +
            "</div>",
      ok: "生成", cancel: "取消"
    }).then(function (yes) {
      if (!yes) { return; }
      var el = $("#rc-account");
      var acc = el ? el.value.trim() : "";
      if (!acc) {
        if (window.dmToast) { window.dmToast("请填写账号"); }
        return;
      }
      window.dmApi.issueResetCode(acc).then(function (d) {
        modal({
          title: "重置码已生成",
          html:
            '<dl class="kv kv--row">' +
              "<div><dt>账号</dt><dd>" + esc(d.account) + "</dd></div>" +
              '<div><dt>重置码</dt><dd class="rc-code">' + esc(d.code) + "</dd></div>" +
              "<div><dt>有效期</dt><dd>" + esc(d.expires_at) + "</dd></div>" +
            "</dl>" +
            '<p class="cap-name__hint">把重置码转交给对方，让对方在登陆页点「忘记密码？」输入。</p>',
          noCancel: true, ok: "知道了"
        });
      }).catch(function (e) {
        if (window.dmToast) { window.dmToast("签发失败：" + (e.message || e)); }
      });
    });
  }

  bind("mgr-refresh", refresh);

  bind("mgr-logout", function () {
    if (!window.dmApi) { return; }
     
    if (window.dmResetDwell) { window.dmResetDwell(); }
    window.dmApi.logout();
    try {
      sessionStorage.removeItem("dm.user");
      localStorage.removeItem("dm.account");
    } catch (e) {}
    document.dispatchEvent(new CustomEvent("dm:go", { detail: "auth" }));
  });

  window.dmManage = {
    enter: refresh
  };
})();

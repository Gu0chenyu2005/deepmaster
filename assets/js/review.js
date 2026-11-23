/*
Copyright (c) 2026 deeprobotics
Author Chenyu Guo
Date November 2026
*/
/* 本地数据审核：同步播放、曲线、分段标注与结果导出。 */
(function () {
  "use strict";

  var $  = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  var FPS_DEFAULT = 15;
  var CAMS = ["wrist", "global", "third"];
  var STATUS_NAME = { good: "合格", bad: "剔除", pending: "待定", "": "未审" };

  var reduceMotion = window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)")
    : { matches: false };

  var pickerEl = $("#rv-picker");
  var appEl    = $("#rv-app");
   
  function showPicker() {
    if (pickerEl) { pickerEl.hidden = false; }
    if (appEl) { appEl.hidden = true; }
    stopPlay();
  }

  function showApp() {
    if (pickerEl) { pickerEl.hidden = true; }
    if (appEl) { appEl.hidden = false; }
  }

  var seqTimer = null;

  var _showAppBase = showApp;
  showApp = function () {
    _showAppBase();
     
    if (window.dmSplitTitle) {
      window.dmSplitTitle(document.querySelector("#t-review-app"));
    }
     
    if (appEl) {
      appEl.classList.add("is-sequenced");
      clearTimeout(seqTimer);
      seqTimer = setTimeout(function () {
        if (appEl) { appEl.classList.remove("is-sequenced"); }
      }, 1500);
    }
    Promise.resolve().then(function () {
      try {
        var tall = localStorage.getItem("dm.review.tall") === "1";
        if (typeof applyTall === "function") { applyTall(tall); }
        if (typeof fitCams === "function") { fitCams(); }
         
        if (typeof drawChart === "function") { drawChart(); }
      } catch (e) {}
    });
  };

  var S = {
    dir: "",
    token: "",
    episodes: [],
    cur: -1,
    meta: null,
    curves: null,
    frame: 0,
    speed: 1,
    playing: false,
    timer: null,
    tab: "qpos",
    filter: "all",
    labels: {},
    segments: [],
    subtasks: [],
    selected: {},           
    fps: FPS_DEFAULT
  };

  var SEG_PALETTE = ["#0071e3", "#30d158", "#ff9f0a", "#bf5af2",
                     "#ff453a", "#64d2ff", "#ffd60a", "#ff375f"];

  function segColor(label) {
    var i = S.subtasks.indexOf(label);
    var k = (i >= 0) ? i : (label ? label.length : 0);
    return SEG_PALETTE[k % SEG_PALETTE.length];
  }

  function toast(msg) {
    if (window.dmToast) { window.dmToast(msg); return; }
    if (window.dmCapture && window.dmCapture.toast) { window.dmCapture.toast(msg); return; }
    try { console.debug("dm: toast " + msg); } catch (e) {}
  }

  function api(path, opts) {
    if (!window.dmApi) { return Promise.reject(new Error("接口模块未加载")); }
    opts = opts || {};
    var method = opts.method || "GET";
    var body = opts.body;
    if (typeof body === "string") {
      try { body = JSON.parse(body); } catch (e) { body = {}; }
    }
    return window.dmApi.request(method, path, { body: body });
  }

  function showEmpty(text) {
    var picker = $("#rv-picker");
    var app = $("#rv-app");
    if (picker) { picker.hidden = false; }
    if (app) { app.hidden = true; }
    var t = $("#rv-empty-text");
    if (t && text) { t.textContent = text; }

    S.episodes = [];
    S.cur = -1;
    var list = $("#rv-list");
    if (list) { list.innerHTML = ""; }
    clearImages();
  }

  function openDir(dir, triedSubdir) {
    api("/episodes?dir=" + encodeURIComponent(dir)).then(function (r) {
      S.dir = r.data_dir || dir;
       
      if (window.dmDataDir) { window.dmDataDir.set(S.dir); }
      syncDirSelects(S.dir);
      S.token = r.token || "";
      S.episodes = (r.episodes || []).map(function (e) {
        return {
          id: e.id,
          status: e.status || "",
          comment: e.comment || "",
           
          segments: Array.isArray(e.segments) ? e.segments : []
        };
      });
       
      if (Array.isArray(r.cameras) && r.cameras.length) {
        buildCams(r.cameras);
      } else {
        buildCams([]);
      }
      if (r.fps) { S.fps = r.fps; }
      setTimeout(syncQcState, 0);
      setTimeout(syncExportState, 0);
       
      S.subtasks = Array.isArray(r.subtasks) ? r.subtasks.slice() : loadSubtasks(S.dir);
      S.episodes.forEach(function (e) {
        (e.segments || []).forEach(function (sg) {
          if (sg && sg.label && S.subtasks.indexOf(sg.label) < 0) {
            S.subtasks.push(sg.label);
          }
        });
      });

      if (!S.episodes.length) {
         
        if (!triedSubdir) {
          return api("/dirs?path=" + encodeURIComponent(dir)).then(function (d) {
            var subs = (d && d.dirs) || [];
            if (!subs.length) {
              showEmpty("暂无待审");
              return;
            }
             
            subs.sort(function (a, b) { return (b.episodes || 0) - (a.episodes || 0); });
            return openDir(subs[0].path, true);
          }).catch(function () {
            showEmpty("暂无待审");
          });
        }
        showEmpty("暂无待审");
        return;
      }
      showApp();
      if ($("#rv-dataset")) {
        $("#rv-dataset").textContent = S.dir;
      }
       
      renderCollector(r.collector_name || r.collector);
      renderList();
      selectEpisode(S.episodes[0].id);
    }).catch(function (e) {
      showEmpty("读取失败：" + (e.message || e));
    });
  }

  var camsEl = $("#rv-cams");
  var camLabels = { wrist: "腕部", global: "头部", third: "第三人称" };

  var camDraw = {};         
  var fittedOnce = false;

  function camSlot(key) { return camDraw[key] || null; }

  function buildCams(list) {
    if (!camsEl) { return; }
    var labels = {};
    (list || []).forEach(function (c) { labels[c.key] = c.label; });

    camsEl.innerHTML = "";
    camDraw = {};
    fittedOnce = false;
    CAMS.forEach(function (key) {
      var panel = document.createElement("figure");
      panel.className = "rv-cam";

      var head = document.createElement("div");
      head.className = "rv-cam__head";
      var name = document.createElement("span");
      name.className = "rv-cam__name";
      name.textContent = (labels[key] || camLabels[key] || key).replace(/相机$/, "");
      head.appendChild(name);

      var wrap = document.createElement("div");
      wrap.className = "rv-cam__body";

      var cv = document.createElement("canvas");
      cv.id = "rv-cv-" + key;
      cv.className = "rv-cam__canvas";
      cv.width = 640;               
      cv.height = 480;

      var tip = document.createElement("p");
      tip.className = "rv-cam__tip";
      tip.textContent = "未加载";

      wrap.appendChild(cv);
      wrap.appendChild(tip);

      camDraw[key] = {
        cv: cv, ctx: cv.getContext("2d"),
        want: "", seq: 0, drawn: false
      };

      var zoom = document.createElement("button");
      zoom.type = "button";
      zoom.className = "rv-cam__zoom";
      zoom.title = "放大";
      zoom.textContent = "⤢";
      zoom.addEventListener("click", function () { openZoom(key); });

      panel.appendChild(head);
      panel.appendChild(wrap);
      panel.appendChild(zoom);
      camsEl.appendChild(panel);
    });
  }

  function drawFrame(key, idx) {
    var slot = camSlot(key);
    if (!slot) { return; }
    var url = frameURL(key, idx);
    if (slot.want === url) { return; }
    slot.want = url;

    var cv = slot.cv;
    var ctx = slot.ctx;
    var im = new Image();

    function paint() {
      if (slot.want !== url) { return; }         
      if (cv.width !== im.naturalWidth || cv.height !== im.naturalHeight) {
         
        cv.width = im.naturalWidth;
        cv.height = im.naturalHeight;
        if (!fittedOnce) { fittedOnce = true; fitCams(); }
      }
      ctx.drawImage(im, 0, 0);
      slot.drawn = true;
      slot.seq++;
      var tip = cv.parentElement && cv.parentElement.querySelector(".rv-cam__tip");
      if (tip) { tip.hidden = true; }
      if (zoomEl && !zoomEl.hidden && zoomEl.dataset.cam === key) { drawZoom(key); }
    }

    im.onload = function () {
      if (slot.want !== url) { return; }
       
      if (im.decode) { im.decode().then(paint, paint); }
      else { paint(); }
    };
    im.onerror = function () {
      if (slot.want !== url) { return; }
      var tip = cv.parentElement && cv.parentElement.querySelector(".rv-cam__tip");
      if (tip) { tip.hidden = false; tip.textContent = "无此相机"; }
    };
    im.src = url;
  }

  function clearImages() {
    CAMS.forEach(function (key) {
      var slot = camSlot(key);
      if (!slot) { return; }
      slot.want = "";
      slot.drawn = false;
      slot.ctx.clearRect(0, 0, slot.cv.width, slot.cv.height);
      var tip = slot.cv.parentElement && slot.cv.parentElement.querySelector(".rv-cam__tip");
      if (tip) { tip.hidden = false; tip.textContent = "未加载"; }
    });
  }

  function frameURL(cam, idx) {
     
    if (window.dmApi) {
      return window.dmApi.frameURL(S.dir, S.cur, cam, idx, S.token);
    }
    return "/api/episode/" + S.cur + "/frame/" + cam + "/" + idx +
           "?dir=" + encodeURIComponent(S.dir || "") + "&v=" + (S.token || "");
  }

  var listEl = $("#rv-list");

  if (listEl) {
    listEl.addEventListener("wheel", function (e) {
      e.preventDefault();
      e.stopPropagation();
      var unit = e.deltaMode === 1 ? 16 : (e.deltaMode === 2 ? listEl.clientWidth : 1);
      var delta = (Math.abs(e.deltaY) >= Math.abs(e.deltaX) ? e.deltaY : e.deltaX) * unit;
      if (!delta) { return; }

      var max = listEl.scrollWidth - listEl.clientWidth;
      if (max <= 0) { return; }

      listEl.scrollLeft = Math.max(0, Math.min(max, listEl.scrollLeft + delta));
    }, { passive: false });
  }

  function renderList() {
    if (!listEl) { return; }
    listEl.innerHTML = "";

    var shown = S.episodes.filter(function (e) {
      if (S.filter === "all") { return true; }
      if (S.filter === "pending") { return !e.status; }
      return e.status === S.filter;
    });

    shown.forEach(function (e, i) {
      var li = document.createElement("li");
      var picked = !!S.selected[e.id];
       
      li.style.setProperty("--i", i);
      li.className = "rv-item" +
        (e.id === S.cur ? " is-active" : "") +
        (picked ? " is-picked" : "");
      li.dataset.episodeId = String(e.id);
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", picked ? "true" : "false");

      var row = document.createElement("div");
      row.className = "rv-item__row";

      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "rv-item__btn";

      var id = document.createElement("span");
      id.className = "rv-item__id";
      id.textContent = "ep " + e.id;

      var st = document.createElement("span");
      st.className = "rv-item__st " + (e.status ? "is-" + e.status : "is-none");
      st.textContent = STATUS_NAME[e.status] || "未审";

      btn.appendChild(id);
      btn.appendChild(st);
      btn.addEventListener("click", function () { selectEpisode(e.id); });

      row.appendChild(btn);
      li.appendChild(row);
      listEl.appendChild(li);
    });

    renderProgress();
    renderBatchBar();

  }

  function togglePick(id) {
    if (S.selected[id]) { delete S.selected[id]; }
    else { S.selected[id] = true; }
    renderList();
  }

  function clearPicks() {
    S.selected = {};
    renderList();
  }

  function pickedIds() {
    return Object.keys(S.selected).map(Number);
  }

  function renderBatchBar() {
    var bar = $("#rv-batch");
    var cnt = $("#rv-batch-count");
    if (!bar) { return; }
    var n = pickedIds().length;
    bar.hidden = n === 0;
    if (cnt) { cnt.textContent = "已选 " + n + " 项"; }
     
    fitCams();
  }

  function batchMark(status) {
    var ids = pickedIds();
    if (!ids.length || !window.dmApi) { return; }
    if (!window.dmApi.batchReview) {
      toast("批量审核不可用");
      return;
    }
    window.dmApi.batchReview(S.dir, ids, status, "").then(function (r) {
      (r.updated || []).forEach(function (id) {
        var ep = S.episodes.find(function (x) { return x.id === id; });
        if (ep) { ep.status = status; }
      });
      clearPicks();
      renderList();
      renderMeta();
      toast("已批量标记 " + (r.updated || []).length + " 条为「" + STATUS_NAME[status] + "」");
    }).catch(function (e) {
      toast("批量标记失败：" + (e.message || e));
    });
  }

  function renderProgress() {
    var total = S.episodes.length;
    var good = S.episodes.filter(function (e) { return e.status === "good"; }).length;
    var bad = S.episodes.filter(function (e) { return e.status === "bad"; }).length;
    var pend = total - good - bad;
    var done = good + bad;

    var fill = $("#rv-progress-fill");
    var text = $("#rv-progress-text");
    if (fill) { fill.style.width = (total ? (done / total * 100) : 0) + "%"; }
    if (text) { text.textContent = done + " / " + total + " 已审"; }

    var g = $("#rv-tally-good"), p = $("#rv-tally-pending"), b = $("#rv-tally-bad");
    if (g) { g.textContent = good; }
    if (p) { p.textContent = pend; }
    if (b) { b.textContent = bad; }
  }

  $$(".rv-chip").forEach(function (chip) {
    chip.addEventListener("click", function () {
      $$(".rv-chip").forEach(function (c) { c.classList.remove("is-active"); });
      chip.classList.add("is-active");
      S.filter = chip.dataset.filter;
      renderList();
    });
  });

  var dwell = { dir: "", episode: -1, since: 0, owner: "" };

  function currentAccount() {
    try {
      var u = sessionStorage.getItem("dm.user");
      if (u) { return String(u); }
    } catch (e) {}
    try {
      return localStorage.getItem("dm.account") || "";
    } catch (e) { return ""; }
  }

  function flushDwell() {
    if (!dwell.since || dwell.episode < 0) {
      dwell.since = 0;
      dwell.episode = -1;
      dwell.owner = "";
      return;
    }
    var secs = (Date.now() - dwell.since) / 1000;
    var owner = dwell.owner;
    var now = currentAccount();

    if (!owner || !now || owner !== now) {
      dwell.since = 0;
      dwell.episode = -1;
      dwell.owner = "";
      return;
    }

    if (secs >= 3 && window.dmApi && window.dmApi.reportReviewTime) {
      window.dmApi.reportReviewTime(dwell.dir, dwell.episode, Math.round(secs))
        .catch(function () {});
    }
    dwell.since = 0;
    dwell.episode = -1;
    dwell.owner = "";
  }

  function startDwell(dir, id) {
    flushDwell();
    dwell.dir = dir;
    dwell.episode = id;
    dwell.since = Date.now();
     
    dwell.owner = currentAccount();
  }

  function resetDwellForAccountChange() {
    flushDwell();
    dwell.dir = "";
    dwell.episode = -1;
    dwell.since = 0;
    dwell.owner = "";
  }
  window.dmResetDwell = resetDwellForAccountChange;

  function selectEpisode(id) {
    stopPlay();
     
    stopPreload();
    startDwell(S.dir, id);
    S.cur = id;
    if (listEl) {
      Array.prototype.forEach.call(listEl.querySelectorAll(".rv-item"), function (item) {
        item.classList.toggle("is-active", item.dataset.episodeId === String(id));
      });
    }
    S.frame = 0;
    S.meta = null;
    S.curves = null;
    var cur = S.episodes.find(function (e) { return e.id === id; });
    S.segments = (cur && Array.isArray(cur.segments)) ? JSON.parse(JSON.stringify(cur.segments)) : [];
    clearImages();
    renderFrameLabel();

    Promise.all([
      api("/episode/" + id + "/meta?dir=" + encodeURIComponent(S.dir || "")),
      api("/episode/" + id + "/curves?dir=" + encodeURIComponent(S.dir || ""))
    ]).then(function (res) {
      if (S.cur !== id) { return; }
      S.meta = res[0];
      S.curves = res[1];
      if (S.meta && S.meta.frames) {
        S.fps = S.meta.fps_attr || S.fps;
        var range = $("#rv-range");
        if (range) {
          range.max = String(S.meta.frames - 1);
          range.value = "0";
        }
        setFrame(0, true);
         
        startPreload();
      }
      drawChart();
      renderMeta();
      renderSegments();
    }).catch(function (e) {
      var meta = $("#rv-meta");
      if (meta) { meta.textContent = "载入失败：" + e.message; }
    });
  }

  var rangeEl = $("#rv-range");

  var preload = { seq: 0, abort: false };

  function stopPreload() {
    preload.abort = true;
    preload.seq++;
    setLoadState("");
  }

  function setLoadState(txt) {
    var el = $("#rv-load-state");
    if (el) { el.textContent = txt || ""; el.hidden = !txt; }
  }

  function startPreload() {
    if (!S.meta) { return; }
    preload.abort = false;
    var mySeq = ++preload.seq;

    var n = S.meta.frames;
    var total = n * CAMS.length;
    var loaded = 0, queued = 0;
    setLoadState("缓存 0%");

    var order = [];
    var seen = {};
    var from = S.frame || 0;
    for (var d = 0; d < n; d++) {
      [from + d, from - d].forEach(function (i) {
        if (i >= 0 && i < n && !seen[i]) { seen[i] = 1; order.push(i); }
      });
      if (order.length >= n) { break; }
    }

    var jobs = [];
    order.forEach(function (i) {
      CAMS.forEach(function (cam) { jobs.push([cam, i]); });
    });

    var ptr = 0;
    function next() {
      if (preload.abort || mySeq !== preload.seq) { return; }
      while (queued < 8 && ptr < jobs.length) {
        var job = jobs[ptr++];
        queued++;
        var im = new Image();
        im.onload = im.onerror = function () {
          queued--;
          loaded++;
          if (mySeq !== preload.seq) { return; }
          if (loaded % 30 === 0 || loaded === total) {
            setLoadState(loaded >= total ? "" :
              "缓存 " + Math.round(loaded / total * 100) + "%");
          }
          next();
        };
        im.src = frameURL(job[0], job[1]);
      }
      if (loaded >= total && mySeq === preload.seq) { setLoadState(""); }
    }
    next();
  }

  function setFrame(idx, force) {
    if (!S.meta) { return; }
    var n = S.meta.frames;
    idx = Math.max(0, Math.min(n - 1, idx));
    if (!force && idx === S.frame) { return; }
    S.frame = idx;

    CAMS.forEach(function (key) { drawFrame(key, idx); });

    if (rangeEl) { rangeEl.value = String(idx); }
    renderFrameLabel();
     
    renderSegFrame();
    drawChart();
  }

  function renderSegFrame() {
    var el = $("#rv-seg-frame");
    if (el) { el.textContent = String(S.frame); }
  }

  function renderFrameLabel() {
    var el = $("#rv-frame");
    if (!el) { return; }
    if (!S.meta) { el.textContent = "— / —"; return; }
    var t = S.meta.t_rel && S.meta.t_rel[S.frame];
    el.textContent = (S.frame + 1) + " / " + S.meta.frames +
      (t != null ? "  " + t.toFixed(2) + "s" : "");
  }

  function renderBand() {
    var band = $("#rv-band");
    if (!band) { return; }
    band.innerHTML = "";
    if (!S.meta || !S.segments.length) { return; }

    var N = S.meta.frames;
    S.segments.forEach(function (sg, i) {
      var to = (i < S.segments.length - 1) ? S.segments[i + 1].from - 1 : N - 1;
      var chip = document.createElement("span");
      chip.className = "rv-band__chip";
      chip.style.left = ((sg.from / N) * 100) + "%";
      chip.style.width = (((to - sg.from + 1) / N) * 100) + "%";
      chip.style.background = sg.label ? segColor(sg.label) : "rgba(0,0,0,.18)";
      chip.title = (sg.label || "未标注") + "：帧 " + sg.from + "–" + to;
      chip.addEventListener("click", function (e) {
        e.stopPropagation();
        stopPlay();
        setFrame(sg.from, true);
      });
      band.appendChild(chip);
    });
  }

  if (rangeEl) {
    rangeEl.addEventListener("input", function () {
      stopPlay();
      setFrame(parseInt(rangeEl.value, 10), true);
    });
  }

  var playBtn = $("#rv-play");

  var ICON_PLAY = '<path d="M5.5 3.5l7 4.5-7 4.5z"/>';
  var ICON_PAUSE = '<path d="M5.5 3.5v9"/><path d="M10.5 3.5v9"/>';

  function setPlayIcon(paused) {
    if (!playBtn) { return; }
    var svg = playBtn.querySelector("svg");
    if (!svg) { return; }
    svg.innerHTML = paused ? ICON_PLAY : ICON_PAUSE;
    playBtn.setAttribute("aria-label", paused ? "播放" : "暂停");
  }

  function startPlay() {
    if (!S.meta || S.playing) { return; }
    if (S.frame >= S.meta.frames - 1) { setFrame(0, true); }
    S.playing = true;
    setPlayIcon(false);
    S.timer = setInterval(function () {
      if (S.frame >= S.meta.frames - 1) { stopPlay(); return; }
      setFrame(S.frame + 1);
    }, 1000 / ((S.fps || FPS_DEFAULT) * S.speed));
  }

  function stopPlay() {
    S.playing = false;
    if (S.timer) { clearInterval(S.timer); S.timer = null; }
    setPlayIcon(true);
  }

  if (playBtn) {
    playBtn.addEventListener("click", function () {
      if (S.playing) { stopPlay(); } else { startPlay(); }
    });
  }

  var speedEl = $("#rv-speed");
  if (speedEl) {
    speedEl.addEventListener("change", function () {
      S.speed = parseFloat(speedEl.value) || 1;
      if (S.playing) { stopPlay(); startPlay(); }
    });
  }

  function stepEp(delta) {
    var i = S.episodes.findIndex(function (e) { return e.id === S.cur; });
    var next = i + delta;
    if (next < 0 || next >= S.episodes.length) { return; }
    selectEpisode(S.episodes[next].id);
  }

  function bindNav(id, fn) {
    var el = $("#" + id);
    if (el) { el.addEventListener("click", fn); }
  }
  bindNav("rv-prev-ep", function () { stepEp(-1); });
  bindNav("rv-next-ep", function () { stepEp(1); });
  bindNav("rv-prev", function () { stopPlay(); setFrame(S.frame - 1); });
  bindNav("rv-next", function () { stopPlay(); setFrame(S.frame + 1); });

  document.addEventListener("keydown", function (e) {
    if (appEl && appEl.hidden) { return; }
    if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) { return; }
    if (e.metaKey || e.ctrlKey || e.altKey) { return; }

    switch (e.key) {
      case "ArrowLeft":  e.preventDefault(); stopPlay(); setFrame(S.frame - 1); break;
      case "ArrowRight": e.preventDefault(); stopPlay(); setFrame(S.frame + 1); break;
      case "ArrowUp":    e.preventDefault(); stepEp(-1); break;
      case "ArrowDown":  e.preventDefault(); stepEp(1); break;
      case " ":
      case "Spacebar":
        e.preventDefault();
        if (S.playing) { stopPlay(); } else { startPlay(); }
        break;
      case "?": e.preventDefault(); openKeys(); break;
      case "g": case "G": mark("good"); break;
      case "p": case "P": mark("pending"); break;
      case "b": case "B": mark("bad"); break;
      case "Escape": closeZoom(); break;
    }
  });

  var canvas = $("#rv-canvas");
  var legendEl = $("#rv-legend");

  var TABS = {
    qpos:   { data: "qpos",   names: "arm",  label: "关节角", color: "#0071e3" },
    action: { data: "action", names: "arm",  label: "指令",   color: "#bf5af2" },
    torque: { data: "torque", names: null,   label: "力矩",   color: "#ff9f0a" },
    leg:    { data: "leg",    names: "leg",  label: "腿",     color: "#30d158" }
  };

  $$(".rv-tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      $$(".rv-tab").forEach(function (t) { t.classList.remove("is-active"); });
      tab.classList.add("is-active");
      S.tab = tab.dataset.tab;
      drawChart();
    });
  });

  function drawChart() {
    if (!canvas) { return; }
    var dpr = window.devicePixelRatio || 1;
    var cssW = canvas.clientWidth || 600;
    var cssH = canvas.clientHeight || 120;
    if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
      canvas.width = Math.round(cssW * dpr);
      canvas.height = Math.round(cssH * dpr);
    }
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    var cfg = TABS[S.tab];
    var arr = S.curves && S.curves[cfg.data];
    if (!arr || !arr.length) {
      ctx.fillStyle = "rgba(0,0,0,.28)";
      ctx.font = "12px " + getComputedStyle(canvas).fontFamily;
      ctx.textAlign = "center";
      ctx.fillText("无数据", cssW / 2, cssH / 2);
      if (legendEl) { legendEl.innerHTML = ""; }
      return;
    }

    var n = arr.length;
    var dims = arr[0].length;
    var names = (S.curves && S.curves.names && cfg.names) ? S.curves.names[cfg.names] : null;

    var lo = Infinity, hi = -Infinity;
    for (var i = 0; i < n; i++) {
      for (var d = 0; d < dims; d++) {
        var v = arr[i][d];
        if (v == null) { continue; }
        if (v < lo) { lo = v; }
        if (v > hi) { hi = v; }
      }
    }
    if (!isFinite(lo) || !isFinite(hi)) { lo = 0; hi = 1; }
    if (hi - lo < 1e-6) { hi = lo + 1; }

    var padL = 6, padR = 6, padT = 8, padB = 8;
    var x0 = padL, x1 = cssW - padR, y0 = padT, y1 = cssH - padB;
    var yOf = function (v) { return y1 - ((v - lo) / (hi - lo)) * (y1 - y0); };
    var xOf = function (i) { return x0 + (n <= 1 ? 0 : (i / (n - 1)) * (x1 - x0)); };

    ctx.strokeStyle = "rgba(0,0,0,.07)";
    ctx.lineWidth = 1;
    [0, .5, 1].forEach(function (f) {
      var y = y0 + f * (y1 - y0);
      ctx.beginPath();
      ctx.moveTo(x0, Math.round(y) + .5);
      ctx.lineTo(x1, Math.round(y) + .5);
      ctx.stroke();
    });

    var palette = ["#0071e3", "#30d158", "#ff9f0a", "#bf5af2", "#ff453a",
                   "#64d2ff", "#ffd60a", "#ff375f", "#5e5ce6", "#ac8e68",
                   "#0a84ff", "#32ade6", "#2fd058", "#ff9f0b", "#bf5af2", "#ff4136"];
    for (var d2 = 0; d2 < dims; d2++) {
      ctx.strokeStyle = palette[d2 % palette.length];
      ctx.lineWidth = 1;
      ctx.beginPath();
      var pen = false;
      for (var k = 0; k < n; k++) {
        var val = arr[k][d2];
        if (val == null) { pen = false; continue; }
        var px = xOf(k), py = yOf(val);
        if (!pen) { ctx.moveTo(px, py); pen = true; }
        else { ctx.lineTo(px, py); }
      }
      ctx.stroke();
    }

    var cx = xOf(S.frame);
    ctx.strokeStyle = "rgba(0,0,0,.42)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(Math.round(cx) + .5, y0);
    ctx.lineTo(Math.round(cx) + .5, y1);
    ctx.stroke();

    if (legendEl) {
      var pieces = [];
      var upto = Math.min(dims, 8);
      for (var d3 = 0; d3 < upto; d3++) {
        var nm = names ? names[d3] : ("ch" + (d3 + 1));
        pieces.push('<span class="rv-legend__item">' +
          '<i style="background:' + palette[d3 % palette.length] + '"></i>' +
          nm + "</span>");
      }
      if (dims > upto) {
        pieces.push('<span class="rv-legend__item">+' + (dims - upto) + "</span>");
      }
      legendEl.innerHTML = pieces.join("");
    }
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { flushDwell(); }
    else if (dwell.episode < 0 && S.cur >= 0) { startDwell(S.dir, S.cur); }
  });
  window.addEventListener("beforeunload", flushDwell);

  window.addEventListener("resize", function () {
    if (appEl && !appEl.hidden) { drawChart(); }
  }, { passive: true });

  var segLabelsEl = $("#rv-seg-labels");
  var segListEl = $("#rv-seg-list");

  var SUBTASK_KEY = "dm.review.subtasks";

  function loadSubtasks(dir) {
    try {
      var raw = localStorage.getItem(SUBTASK_KEY + ":" + dir);
      var arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr.filter(function (s) {
        return typeof s === "string" && s;
      }) : [];
    } catch (e) { return []; }
  }

  function saveSubtasks() {
    try {
      localStorage.setItem(SUBTASK_KEY + ":" + S.dir, JSON.stringify(S.subtasks));
    } catch (e) {}
  }

  function renderSegLabels() {
    if (!segLabelsEl) { return; }
    segLabelsEl.innerHTML = "";

    if (!S.subtasks.length) {
      var hint = document.createElement("span");
      hint.className = "rv-seg__hint";
      hint.textContent = "还没有子任务标签，点右侧「+」新建";
      segLabelsEl.appendChild(hint);
    }

    S.subtasks.forEach(function (label) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "rv-seglabel";
      btn.style.setProperty("--seg-color", segColor(label));
      btn.title = "从当前帧开始标记为「" + label + "」";
       
      btn.appendChild(document.createTextNode(label));
      var x = document.createElement("span");
      x.className = "rv-seglabel__x";
      x.textContent = "✕";
      x.title = "删除这个标签";
      x.addEventListener("click", function (ev) {
        ev.stopPropagation();
        S.subtasks.splice(S.subtasks.indexOf(label), 1);
        saveSubtasks();
        renderSegLabels();
      });
      btn.appendChild(x);
      btn.addEventListener("click", function () { markStageStart(label); });
      segLabelsEl.appendChild(btn);
    });

    var add = document.createElement("button");
    add.type = "button";
    add.className = "rv-seglabel is-add";
    add.textContent = "+";
    add.title = "新增子任务标签";
    add.addEventListener("click", function () { openLabelInput(add); });
    segLabelsEl.appendChild(add);
  }

  var labelInputOpen = false;

  function openLabelInput(anchor) {
    if (labelInputOpen) { return; }
    labelInputOpen = true;

    var inp = document.createElement("input");
    inp.type = "text";
    inp.className = "input rv-seglabel rv-seglabel--edit";
    inp.placeholder = "标签名";
    inp.maxLength = 8;
    anchor.replaceWith(inp);
    inp.focus();

    function done(commit) {
      if (!labelInputOpen) { return; }
      labelInputOpen = false;
      var v = commit ? String(inp.value || "").trim() : "";
      if (v && S.subtasks.indexOf(v) < 0) {
        S.subtasks.push(v);
        saveSubtasks();
      }
      renderSegLabels();
    }

    inp.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); done(true); }
      else if (e.key === "Escape") { e.preventDefault(); done(false); }
    });
    inp.addEventListener("blur", function () { done(true); });
  }

  function markStageStart(label) {
    if (!S.meta) { return; }
    var f = S.frame, N = S.meta.frames;

    if (!S.segments.length) {
      var arr = [];
      if (f > 0) { arr.push({ from: 0, to: f - 1, label: null }); }
      arr.push({ from: f, to: N - 1, label: label });
      S.segments = arr;
    } else {
      var i = S.segments.findIndex(function (s) { return f >= s.from && f <= s.to; });
      if (i < 0) { i = 0; }
      if (S.segments[i].from === f) {
        S.segments[i].label = label;               
      } else {
        var oldTo = N - 1;                         
        S.segments[i].to = f - 1;
        S.segments.splice(i + 1, 0, { from: f, to: oldTo, label: label });
      }
    }
    renderSegments();
    saveSegments();
  }

  function renderSegments() {
    renderSegLabels();
    renderSegFrame();
    renderBand();

    if (!segListEl) { return; }
    segListEl.innerHTML = "";

    var N = S.meta ? S.meta.frames : 0;
    if (!S.segments.length || !N) {
      var empty = document.createElement("p");
      empty.className = "rv-seg__empty";
      empty.textContent = "未分段";
      segListEl.appendChild(empty);
      return;
    }

    S.segments.forEach(function (sg, i) {
      var to = (i < S.segments.length - 1) ? S.segments[i + 1].from - 1 : N - 1;

      var row = document.createElement("div");
      row.className = "rv-segrow";

      var dot = document.createElement("i");
      dot.className = "rv-segrow__dot";
      dot.style.background = sg.label ? segColor(sg.label) : "rgba(0,0,0,.20)";
      row.appendChild(dot);

      if (i === 0) {
        var f0 = document.createElement("span");
        f0.className = "rv-segrow__range";
        f0.textContent = "0 – " + to;
        row.appendChild(f0);
      } else {
        var inp = document.createElement("input");
        inp.className = "input rv-segrow__from";
        inp.type = "number";
        inp.min = "1";
        inp.max = String(to);
        inp.value = String(sg.from);
        inp.title = "该段起始帧";
        inp.addEventListener("change", function () {
          var v = parseInt(inp.value, 10);
          if (isNaN(v)) { v = sg.from; }
          v = Math.max(S.segments[i - 1].from + 1, Math.min(to, v));
          sg.from = v;
          S.segments[i - 1].to = v - 1;
          renderSegments();
          saveSegments();
        });
        row.appendChild(inp);

        var f1 = document.createElement("span");
        f1.className = "rv-segrow__range";
        f1.textContent = "– " + to;
        row.appendChild(f1);
      }

      var opts = S.subtasks.slice();
      if (sg.label && opts.indexOf(sg.label) < 0) { opts.push(sg.label); }

      var sel = document.createElement("select");
      sel.className = "input rv-segrow__label";
      sel.title = "选择子任务";
      var html = sg.label ? "" : '<option value="" selected>（未标注）</option>';
      opts.forEach(function (o) {
        html += '<option value="' + escapeAttr(o) + '"' +
                (o === sg.label ? " selected" : "") + ">" + escapeHtml(o) + "</option>";
      });
      sel.innerHTML = html;
      sel.addEventListener("change", function () {
        sg.label = sel.value || null;
        renderSegments();
        saveSegments();
      });
      row.appendChild(sel);

      var del = document.createElement("button");
      del.type = "button";
      del.className = "rv-segrow__del";
      del.textContent = "✕";
      del.title = "删除该段（并入前一段）";
      del.addEventListener("click", function () {
        if (S.segments.length === 1) {
          S.segments = [];
        } else if (i === 0) {
          S.segments[1].from = 0;
          S.segments.shift();
        } else {
          S.segments[i - 1].to = to;
          S.segments.splice(i, 1);
        }
        renderSegments();
        saveSegments();
      });
      row.appendChild(del);

      segListEl.appendChild(row);
    });

    var clear = document.createElement("button");
    clear.type = "button";
    clear.className = "btn btn--tertiary btn--sm rv-seg__clear";
    clear.textContent = "清空重标";
    clear.addEventListener("click", function () {
      S.segments = [];
      renderSegments();
      saveSegments();
    });
    segListEl.appendChild(clear);
  }

  function saveSegments() {
    if (S.cur < 0) { return; }
     
    api("/review", {
      method: "POST",
      body: { dir: S.dir, episode: S.cur, segments: S.segments }
    }).then(function () {
      var ep = S.episodes.find(function (e) { return e.id === S.cur; });
      if (ep) { ep.segments = JSON.parse(JSON.stringify(S.segments)); }
      flashMeta();
    }).catch(function (e) {
      var meta = $("#rv-meta");
      if (meta) { meta.textContent = "分段保存失败：" + e.message; }
    });
  }

  function escapeAttr(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;").replace(/</g, "&lt;");
  }

  function mark(status) {
    if (S.cur < 0) { return; }
    var commentEl = $("#rv-comment");
    var comment = commentEl ? commentEl.value : "";

    api("/review", {
      method: "POST",
      body: { dir: S.dir, episode: S.cur, status: status, comment: comment }
    }).then(function () {
      var ep = S.episodes.find(function (e) { return e.id === S.cur; });
      if (ep) { ep.status = status; ep.comment = comment; S.labels[ep.id] = { status: status }; }
      renderVerdictCurrent(status);
      renderList();
      renderMeta();
      flashMeta();
    }).catch(function (e) {
      var meta = $("#rv-meta");
      if (meta) { meta.textContent = "保存失败：" + e.message; }
    });
  }

  function renderMeta() {
    var el = $("#rv-meta");
    if (!el) { return; }
    var ep = S.episodes.find(function (e) { return e.id === S.cur; });
    if (!ep) { el.textContent = ""; return; }
    var bits = ["ep " + ep.id, STATUS_NAME[ep.status] || "未审"];
    if (ep.comment) { bits.push(ep.comment); }
    el.textContent = bits.join("  ");
    el.className = "rv-meta";
    renderVerdictCurrent(ep.status || "");
  }

  function renderVerdictCurrent(status) {
    var map = { good: "rv-good", pending: "rv-pending", bad: "rv-bad" };
    Object.keys(map).forEach(function (k) {
      var btn = $("#" + map[k]);
      if (btn) { btn.classList.toggle("is-current", k === status); }
    });
  }

  var flashTimer = null;
  function flashMeta() {
    var el = $("#rv-meta");
    if (!el) { return; }
    el.classList.add("is-saved");
    clearTimeout(flashTimer);
    flashTimer = setTimeout(function () {
      el.classList.remove("is-saved");
    }, 1200);
  }

  bindNav("rv-batch-good", function () { batchMark("good"); });
  bindNav("rv-batch-bad", function () { batchMark("bad"); });
  bindNav("rv-batch-clear", function () { clearPicks(); });

  var keysModalOpen = false;

  var KEYMAP = [
    ["← / →", "上一帧 / 下一帧"],
    ["↑ / ↓", "上一条 / 下一条"],
    ["空格", "播放 / 暂停"],
    ["G", "标记合格"],
    ["P", "标记待定"],
    ["B", "标记剔除"],
    ["?", "打开本说明"],
    ["Esc", "关闭弹窗 / 退出放大"]
  ];

  bindNav("rv-keys", openKeys);

  function openKeys() {
    if (keysModalOpen || !window.dmCapture || !window.dmCapture.openModal) { return; }
    keysModalOpen = true;
    var rows = KEYMAP.map(function (pair) {
      return '<div class="kv__row"><kbd>' + pair[0] + "</kbd><span>" + pair[1] + "</span></div>";
    }).join("");
    window.dmCapture.openModal({
      title: "快捷键",
      html: '<div class="keys">' + rows + "</div>",
      noCancel: true, ok: "知道了"
    }).then(function () { keysModalOpen = false; });
  }

  bindNav("rv-good", function () { mark("good"); });
  bindNav("rv-pending", function () { mark("pending"); });
  bindNav("rv-bad", function () { mark("bad"); });

  var zoomEl = null;

  function ensureZoom() {
    if (zoomEl) { return zoomEl; }
    zoomEl = document.createElement("div");
    zoomEl.className = "rv-zoom";
    zoomEl.hidden = true;
    zoomEl.innerHTML =
      '<div class="rv-zoom__bar">' +
        '<span class="rv-zoom__name" id="rv-zoom-name">相机</span>' +
        '<button class="btn btn--tertiary btn--sm" type="button" id="rv-zoom-close">关闭</button>' +
      "</div>" +
      '<canvas class="rv-zoom__img" id="rv-zoom-cv"></canvas>';
    document.body.appendChild(zoomEl);
    zoomEl.addEventListener("click", function (e) {
      if (e.target === zoomEl) { closeZoom(); }
    });
    $("#rv-zoom-close").addEventListener("click", closeZoom);
    return zoomEl;
  }

  function openZoom(cam) {
    ensureZoom();
    var slot = camSlot(cam);
    if (!slot || !slot.drawn) { return; }
    zoomEl.dataset.cam = cam;
    $("#rv-zoom-name").textContent = camLabels[cam] || cam;
    zoomEl.hidden = false;
    drawZoom(cam);
  }

  function drawZoom(cam) {
    var slot = camSlot(cam);
    var cv = $("#rv-zoom-cv");
    if (!slot || !cv || !slot.drawn) { return; }
    if (cv.width !== slot.cv.width || cv.height !== slot.cv.height) {
      cv.width = slot.cv.width;
      cv.height = slot.cv.height;
    }
    cv.getContext("2d").drawImage(slot.cv, 0, 0);
  }

  function closeZoom() {
    if (zoomEl) { zoomEl.hidden = true; }
  }

  var exportTimer = null;
  var exportGeneration = 0;

  function renderExport(job) {
    var button = $("#rv-export");
    var status = $("#rv-export-status");
    if (!button || !status) { return; }
    var running = job && ["queued", "running", "packaging"].indexOf(job.state) >= 0;
    button.disabled = !!running;
    button.textContent = running ? "导出中 " + job.done + "/" + job.total : "导出 LeRobot";
    status.hidden = !job;
    status.textContent = "";
    if (!job) { return; }
    if (job.state === "done") {
      status.appendChild(document.createTextNode("已导出 " + job.total + " 条合格数据。服务器位置：" + job.output_path + " "));
      if (job.download_url) {
        var link = document.createElement("a");
        link.textContent = "下载 LeRobot 数据集";
        link.href = job.download_url;
        link.setAttribute("download", "");
        link.addEventListener("click", function (event) {
          event.preventDefault();
          api("/lerobot-export?id=" + encodeURIComponent(job.id)).then(function (fresh) {
            var a = document.createElement("a");
            a.href = fresh.download_url;
            a.setAttribute("download", "");
            document.body.appendChild(a);
            a.click();
            a.remove();
          }).catch(function (e) { toast("下载失败：" + e.message); });
        });
        status.appendChild(link);
      }
    } else if (job.state === "error") {
      status.textContent = "导出失败：" + job.error;
    } else {
      status.textContent = job.state === "packaging" ? "转换完成，正在生成下载文件…"
        : "正在导出合格数据：" + job.done + "/" + job.total + (job.current ? "，" + job.current : "");
    }
  }

  function syncExportState() {
    clearTimeout(exportTimer);
    var generation = ++exportGeneration;
    var dir = S.dir;
    if (!dir) { renderExport(null); return; }
    function tick() {
      api("/lerobot-export?dir=" + encodeURIComponent(dir)).then(function (job) {
        if (generation !== exportGeneration || dir !== S.dir) { return; }
        renderExport(job);
        if (job && ["queued", "running", "packaging"].indexOf(job.state) >= 0) {
          exportTimer = setTimeout(tick, 1500);
        }
      }).catch(function (e) {
        if (generation !== exportGeneration || dir !== S.dir) { return; }
        var status = $("#rv-export-status");
        if (status) { status.hidden = false; status.textContent = "导出状态读取失败，正在重试：" + e.message; }
        exportTimer = setTimeout(tick, 5000);
      });
    }
    renderExport(null);
    tick();
  }

  bindNav("rv-export", function () {
    if (!S.dir) { return; }
    var dir = S.dir;
    var button = $("#rv-export");
    button.disabled = true;
    button.textContent = "导出启动中…";
    api("/lerobot-export", { method: "POST", body: { dir: dir } }).then(function () {
      if (dir === S.dir) { syncExportState(); }
    }).catch(function (e) {
      if (dir === S.dir) { renderExport(null); }
      toast("导出失败：" + e.message);
    });
  });

  var qcTimer = null;

  function qcButton() { return $("#rv-autocheck"); }

  function setQcIdle() {
    var b = qcButton();
    if (b) { b.disabled = false; b.textContent = "自动质检"; }
  }

  function refreshReviewState() {
    return api("/episodes?dir=" + encodeURIComponent(S.dir)).then(function (r) {
      var map = {};
      (r.episodes || []).forEach(function (e) { map[e.id] = e; });
      S.episodes.forEach(function (e) {
        var src = map[e.id];
        if (!src) { return; }
        e.status = src.status || "";
        e.comment = src.comment || "";
        e.segments = Array.isArray(src.segments) ? src.segments : [];
      });
      renderList();
      renderProgress();
    });
  }

  function showQcModal(s) {
    var body =
      '<dl class="rv-kv">' +
        "<div><dt>总数</dt><dd>" + (s.total || 0) + "</dd></div>" +
        "<div><dt>合格</dt><dd>" + (s.good || 0) + "</dd></div>" +
        "<div><dt>待定</dt><dd>" + (s.pending || 0) + "</dd></div>" +
      "</dl>" +
      '<p class="rv-msg">结论已写入判定与备注，导出 JSON 同步更新。</p>';
    if (s.aux_len_ignored) {
      body += '<p class="rv-msg">已忽略 ' + s.aux_len_ignored +
        " 项高速镜像流（*_120hz）与 episode 基准的帧数差异——它们是独立采样域，不与主时序对齐。</p>";
    }
    if (window.dmCapture && typeof window.dmCapture.openModal === "function") {
      window.dmCapture.openModal({ title: "自动质检结果", html: body, noCancel: true, ok: "关闭" });
    }
  }

  function pollQc(dir) {
    clearInterval(qcTimer);
    function tick() {
      api("/autocheck?dir=" + encodeURIComponent(dir)).then(function (st) {
        var b = qcButton();
        if (st.running) {
          if (b) { b.textContent = "质检中 " + st.done + "/" + (st.total || "?"); }
          return;
        }
        clearInterval(qcTimer);
        qcTimer = null;
        setQcIdle();
        if (st.error) {
          toast("自动质检失败：" + st.error);
          return;
        }
        var sum = st.summary || {};
        refreshReviewState().then(function () {
          showQcModal(sum);
          toast("质检完成：合格 " + (sum.good || 0) + " / 待定 " + (sum.pending || 0));
        });
      }).catch(function (e) {
        clearInterval(qcTimer);
        qcTimer = null;
        setQcIdle();
        toast("质检状态读取失败：" + e.message);
      });
    }
    tick();
    qcTimer = setInterval(tick, 1500);
  }

  bindNav("rv-autocheck", function () {
    if (!S.dir) { return; }
    var dir = S.dir;
    var b = qcButton();
    if (b) { b.disabled = true; b.textContent = "质检启动中…"; }
    api("/autocheck", { method: "POST", body: { dir: dir } }).then(function () {
      pollQc(dir);
    }).catch(function (e) {
      setQcIdle();
      toast("自动质检失败：" + e.message);
    });
  });

  function syncQcState() {
    if (!S.dir) { return; }
    api("/autocheck?dir=" + encodeURIComponent(S.dir)).then(function (st) {
      if (st.running && !qcTimer) {
        var b = qcButton();
        if (b) { b.disabled = true; }
        pollQc(S.dir);
      }
    }).catch(function () {});
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  var TALL_KEY = "dm.review.tall";

  var camsEl = $("#rv-cams");

  function applyTall(tall) {
    if (!camsEl) { return; }
    camsEl.classList.toggle("is-tall", tall);
    var btn = $("#rv-cams-tall");
    if (btn) { btn.textContent = tall ? "标准" : "加高"; }
    try { localStorage.setItem(TALL_KEY, tall ? "1" : "0"); } catch (e) {}
     
    fitCams();
  }

  bindNav("rv-cams-tall", function () {
    applyTall(!camsEl.classList.contains("is-tall"));
  });

  function fitCams() {
    if (!camsEl) { return; }
    var view = camsEl.closest(".rv-camview") || camsEl;
     
    camsEl.style.removeProperty("--rv-cam-w");
    var avail = view.clientWidth;
    if (!avail) { return; }         

    var ratio = 4 / 3, nativeW = 0, seen = false;
    for (var ci = 0; ci < CAMS.length; ci++) {
      var slot = camSlot(CAMS[ci]);
      if (slot && slot.drawn && slot.cv.width > 1 && slot.cv.height > 1) {
        if (!seen) { ratio = slot.cv.width / slot.cv.height; seen = true; }
        nativeW = Math.max(nativeW, slot.cv.width);
      }
    }
    if (!nativeW) { nativeW = 640; }    

    var tall = camsEl.classList.contains("is-tall");
    var perTile = Math.floor(avail / 3);
     
    var tileW = tall ? perTile : Math.min(nativeW, perTile);
    camsEl.style.setProperty("--rv-cam-w", tileW + "px");
    camsEl.style.setProperty("--rv-cam-h", Math.round(tileW / ratio) + "px");
  }

  bindNav("rv-cams-fit", function () {
    applyTall(false);
    fitCams();
  });

  window.addEventListener("resize", function () {
    if (camsEl && document.querySelector("#rv-app") && !document.querySelector("#rv-app").hidden) {
      fitCams();
    }
  }, { passive: true });

  function dirSelectEls() {
    return [$("#rv-dir-select"), $("#rv-empty-dir-select")].filter(Boolean);
  }

  function renderCollector(name) {
    var el = $("#rv-collector");
    if (!el) { return; }
    if (!name) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    el.hidden = false;
    el.textContent = "采集者：" + name;
    el.title = name;
  }

  function syncDirSelects(dir) {
    dirSelectEls().forEach(function (sel) {
      if (dir && Array.prototype.some.call(sel.options, function (o) { return o.value === dir; })) {
        sel.value = dir;
      }
    });
  }

  function loadDirChoices() {
    var preferred = (window.dmDataDir && window.dmDataDir.get()) || "";
    return api("/dirs?path=").then(function (r) {
      var root = (r && r.path) || "datasets";
      var subs = ((r && r.dirs) || []).slice().sort(function (a, b) {
         
        return String(a.name || a.path) < String(b.name || b.path) ? 1 : -1;
      });
      var items = subs.map(function (it) {
        return {
          path: it.path,
          label: it.name || it.path,
          n: (it.episodes || 0) + (it.episodes ? 0 : (it.records || 0))
        };
      });
      if (!items.length) {
         
        items = [{ path: root, label: root, n: 0, isRoot: true }];
      }
      dirSelectEls().forEach(function (sel) {
        sel.innerHTML = "";
        items.forEach(function (it) {
          var opt = document.createElement("option");
          opt.value = it.path;
          opt.title = it.path;
          opt.textContent = it.label +
            (it.n > 0 ? ("（" + it.n + " 条）") : (it.isRoot ? "（根目录）" : ""));
          sel.appendChild(opt);
        });
      });
       
      var pick = items[0].path;
      for (var i = 0; i < items.length; i++) {
        if (items[i].path === preferred) { pick = preferred; break; }
      }
      syncDirSelects(pick);
      return pick;
    }).catch(function () {
       
      var root = "datasets";
      dirSelectEls().forEach(function (sel) {
        sel.innerHTML = "";
        var opt = document.createElement("option");
        opt.value = root;
        opt.textContent = root + "（根目录）";
        sel.appendChild(opt);
      });
      return root;
    });
  }

  function chooseDir(dir) {
    if (!dir) { return; }
    stopPlay();
    S.cur = -1;
    S.selected = {};
    showEmpty("读取中…");
    openDir(dir);
  }

  dirSelectEls().forEach(function (sel) {
    sel.addEventListener("change", function () { chooseDir(sel.value); });
  });

  bindNav("rv-back", function () {
    S.cur = -1;
    S.selected = {};
    showEmpty("读取中…");
    loadDirChoices().then(function (dir) { openDir(S.dir || dir); });
  });

  window.dmReview = {
    enter: function () {
      showEmpty("读取中…");
      loadDirChoices().then(function (dir) { openDir(dir); });
    },

    refresh: function () {
      loadDirChoices().then(function (dir) { openDir(S.dir || dir); });
    },

    fit: fitCams
  };

})();

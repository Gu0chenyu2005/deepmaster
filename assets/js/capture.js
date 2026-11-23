/*
Copyright (c) 2026 deeprobotics
Author Chenyu Guo
Date November 2026
*/
/* 采集流程、相机预览与设备管理；程序配置以采集端本机配置为准。 */
(function () {
  "use strict";

  var $  = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  var CFG_DEFAULT = {
    dds_ip: "10.21.31.104",
    dds_username: "user",
    dds_password: "",
    k7_dir: "/home/user",
    k7_lib_path: "/home/user",
    k7_bin: "K7Master3",
    k7_sethome_bin: "SetHome",
    k7_taskset: "6,7",
    k7_chrt: "90",
    rl_deploy_dir: "/opt/dm-collect/prog/rl_deploy",

    rl_deploy_build: "build",
    rl_deploy_bin: "rl_deploy",
    robot_service_dir: "/opt/dm-collect/prog/service",
    robot_service_script: "runService.sh",
    ros_setup: "/opt/ros/humble/setup.bash",
    input_mode: "vr",    
    record_fps: "15",
    imu_topic: "/IMU",
    local_data_dir: "/opt/dm-collect/data",
    sudo_pw: "",
    dry_run: ""
  };

  function hostOf(cfg, which) {
    return "本机";
  }

  var ACCOUNT_KEY = "dm.account";

  function account() {
    try { return localStorage.getItem(ACCOUNT_KEY) || "guest"; }
    catch (e) { return "guest"; }
  }

  function cfgKey() { return "dm.host." + account(); }

  function migrateCfg(raw) {
    if (!raw || typeof raw !== "object") { return raw; }
    var out = Object.assign({}, raw);
    [["orin", "orin_host"], ["k7", "k7_host"]].forEach(function (pair) {
      var which = pair[0], legacy = pair[1];
      if (out[legacy] && (!out[which + "_addr"] || !out[which + "_user"])) {
        var s = String(out[legacy]);
        var at = s.indexOf("@");
        if (at > 0) {
          if (!out[which + "_user"]) { out[which + "_user"] = s.slice(0, at); }
          if (!out[which + "_addr"]) { out[which + "_addr"] = s.slice(at + 1); }
        } else if (!out[which + "_addr"]) {
          out[which + "_addr"] = s;
        }
      }
      delete out[legacy];
    });
    return out;
  }

  var cfgCache = null;

  function loadCfg() {
    return cfgCache ? Object.assign({}, cfgCache) : null;
  }

  function syncCfgFromServer() {
    cfgCache = null;
    if (!window.dmApi || !window.dmApi.getConfig) { return Promise.resolve(null); }
    return window.dmApi.getConfig().then(function (r) {
      cfgCache = Object.assign({}, (r && r.config) || {});
      cfgCache.rl_deploy_build = "build";
      return cfgCache;
    }).catch(function () { return null; });
  }

  /* 从当前显示状态开始过渡，支持中断。 */

  var modalEl = $("#modal");
  var modalTitle = $("#modal-title");
  var modalBody = $("#modal-body");
  var modalOk = $("#modal-ok");
  var modalCancel = $("#modal-cancel");
  var modalBox = modalEl ? modalEl.querySelector(".modal__box") : null;
  var modalResolve = null;
  var closing = false;

  var reduceMotion = window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)")
    : { matches: false };

  function ensureGrip() {
    if (!modalBox || modalBox.querySelector(".modal__grip")) { return; }
    var grip = document.createElement("span");
    grip.className = "modal__grip";
    grip.setAttribute("aria-hidden", "true");
    modalBox.insertBefore(grip, modalBox.firstChild);
  }

  function rubberband(overshoot, dimension, constant) {
    constant = constant === undefined ? 0.55 : constant;
    return (overshoot * dimension * constant) / (dimension + constant * Math.abs(overshoot));
  }

  function project(velocity, decel) {
    decel = decel === undefined ? 0.998 : decel;
    return (velocity / 1000) * decel / (1 - decel);
  }

  function closeModal(value) {
    if (!modalEl || modalEl.hidden || closing) { return; }
    closing = true;

    if (modalBox) {
      var now = getComputedStyle(modalBox).transform;
      modalBox.style.transform = now === "none" ? "" : now;
      modalBox.style.transition = "none";
      void modalBox.offsetHeight;
      modalBox.style.transition = "";
    }

    modalEl.classList.remove("is-open");
    modalBox && (modalBox.style.transform = "");

    var r = modalResolve;
    modalResolve = null;

    var wait = reduceMotion.matches ? 120 : 340;
    setTimeout(function () {
      modalEl.hidden = true;
      closing = false;
      if (r) { r(value); }
    }, wait);
  }

  function openModal(opts) {
    return new Promise(function (resolve) {
      if (!modalEl) { resolve(false); return; }

      if (modalResolve) {
        var old = modalResolve;
        modalResolve = null;
        old(false);
      }

      ensureGrip();

      modalTitle.textContent = opts.title || "";
      modalBody.innerHTML = opts.html || "";
      modalOk.textContent = opts.ok || "确定";
      modalCancel.textContent = opts.cancel || "取消";
      modalCancel.hidden = !!opts.noCancel;
      modalOk.classList.toggle("btn--danger", !!opts.danger);
      modalOk.classList.toggle("btn--primary", !opts.danger);

      var anchor = opts.anchor || (document.activeElement && document.activeElement.getBoundingClientRect
        ? document.activeElement : null);
      var originSet = false;
      if (modalBox && anchor && anchor.getBoundingClientRect && !isBottomSheet()) {
        var a = anchor.getBoundingClientRect();
        if (a.width || a.height) {
          var b = modalBox.getBoundingClientRect();
          var ox = a.left + a.width / 2 - b.left;
          var oy = a.top + a.height / 2 - b.top;
          modalBox.style.transformOrigin =
            Math.round(ox) + "px " + Math.round(oy) + "px";
          originSet = true;
        }
      }
      if (modalBox && !originSet) {
        modalBox.style.transformOrigin = isBottomSheet() ? "50% 100%" : "50% 50%";
      }

      modalEl.hidden = false;
      modalResolve = resolve;

      modalEl.classList.remove("is-open");
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          modalEl.classList.add("is-open");
        });
      });

      setTimeout(function () { modalOk.focus(); }, 30);
    });
  }

  function isBottomSheet() {
    return window.matchMedia("(max-width: 820px)").matches;
  }

  if (modalBox) {
    var drag = null;

    modalBox.addEventListener("pointerdown", function (e) {
      if (!isBottomSheet() || !modalEl.classList.contains("is-open")) { return; }
       
      if (e.clientY - modalBox.getBoundingClientRect().top > 48) { return; }
      if (e.target.closest("button, a, input, select, textarea")) { return; }

      drag = {
        id: e.pointerId,
        startY: e.clientY,
        y: 0,
        samples: [{ t: e.timeStamp, y: e.clientY }],
        height: modalBox.getBoundingClientRect().height
      };
      modalBox.setPointerCapture(e.pointerId);
      modalBox.style.transition = "none";
      modalBox.classList.add("is-draggable");
    });

    modalBox.addEventListener("pointermove", function (e) {
      if (!drag || e.pointerId !== drag.id) { return; }

      var raw = e.clientY - drag.startY;
      var y = raw >= 0 ? raw : -rubberband(-raw, drag.height, 0.3);

      drag.y = y;
      modalBox.style.transform = "translateY(" + y + "px)";
      modalBox.style.opacity = String(Math.max(0, 1 - y / (drag.height * 1.1)));

      drag.samples.push({ t: e.timeStamp, y: e.clientY });
      while (drag.samples.length > 2 && e.timeStamp - drag.samples[0].t > 100) {
        drag.samples.shift();
      }
    });

    function endDrag(e) {
      if (!drag || e.pointerId !== drag.id) { return; }
      var d = drag;
      drag = null;
      modalBox.classList.remove("is-draggable");
      if (modalBox.releasePointerCapture && modalBox.hasPointerCapture && modalBox.hasPointerCapture(e.pointerId)) {
        try { modalBox.releasePointerCapture(e.pointerId); } catch (err) {}
      }
      modalBox.style.transition = "";

      var s0 = d.samples[0];
      var s1 = d.samples[d.samples.length - 1];
      var dt = s1.t - s0.t;
      var velocity = dt > 0 ? ((s1.y - s0.y) / dt) * 1000 : 0;

      var projected = d.y + project(velocity, 0.998);

      var shouldClose = velocity > 400 || projected > d.height * 0.46;

      modalBox.style.opacity = "";
      if (shouldClose) {
        closeModal(false);
      } else {
         
        modalBox.style.transform = "translateY(" + d.y + "px)";
        requestAnimationFrame(function () {
          modalBox.style.transform = "";
        });
      }
    }

    modalBox.addEventListener("pointerup", endDrag);
    modalBox.addEventListener("pointercancel", endDrag);
  }

  if (modalEl) {
    modalOk.addEventListener("click", function () { closeModal(true); });
    modalCancel.addEventListener("click", function () { closeModal(false); });
    $$("[data-modal-cancel]").forEach(function (el) {
      el.addEventListener("click", function () { closeModal(false); });
    });
    document.addEventListener("keydown", function (e) {
      if (modalEl.hidden) { return; }
      if (e.key === "Escape") { closeModal(false); }
      if (e.key === "Enter" && e.target === modalOk) { closeModal(true); }
    });
  }

  function toast(msg) {
    var el = $("#toast");
    if (!el) { return; }
    el.textContent = msg;
    el.hidden = false;
    requestAnimationFrame(function () { el.classList.add("is-in"); });
    clearTimeout(el._t);
    el._t = setTimeout(function () {
      el.classList.remove("is-in");
      setTimeout(function () { el.hidden = true; }, 320);
    }, 2600);
  }

  function logLine(text, cls) {
    var tag = cls === "err" ? "[error]" : cls === "ok" ? "[ok]" : "";
    try { console.debug("dm: " + tag + " " + text); } catch (e) {}
  }

  var phaseEl = $("#cap-phase");
  var phaseTextEl = $("#cap-phase-text");

  function setPhase(phase, text) {
    if (phaseEl && phaseTextEl) {
      phaseEl.className = "cap-phase is-" + (phase || "idle");
      phaseTextEl.textContent = text || "";
    }
    logLine("阶段: " + (text || phase || "—"));
  }

  function termOpen(name) {   }
  function termLine(text, cls) { logLine(text, cls); }
  function termStateSet(txt) {   }

  var NO_BRIDGE_ERR = "后端未连接：执行接口 /api/run 不可用，请确认服务已启动";

  function bridgeMissing(action) {
    return { ok: false, error: NO_BRIDGE_ERR + "（动作 " + action + "）" };
  }

  var NO_BRIDGE = {
    t1: function () { return Promise.resolve(bridgeMissing("t1")); },
    tare: function () { return Promise.resolve(bridgeMissing("tare")); },
    t2: function () { return Promise.resolve(bridgeMissing("t2")); },
    z: function () { return Promise.resolve(bridgeMissing("z")); },
    stop: function () { return Promise.resolve(bridgeMissing("stop")); },
    captureStop: function () { return Promise.resolve(bridgeMissing("capture-stop")); },
    controlStop: function () { return Promise.resolve(bridgeMissing("control-stop")); },
    camStart: function () { return Promise.resolve(bridgeMissing("cam-start")); },
    camStop: function () { return Promise.resolve(bridgeMissing("cam-stop")); },
    camHealth: function () { return Promise.resolve(null); },
    latestDir: function () { return Promise.resolve({ ok: false, name: null }); }
  };

  function br() {
    return (window.dmBridge && typeof window.dmBridge.t1 === "function") ? window.dmBridge : NO_BRIDGE;
  }

  function durKey() { return "dm.duration." + account(); }

  function loadDurations() {
    try { return JSON.parse(localStorage.getItem(durKey()) || "{}"); }
    catch (e) { return {}; }
  }

  function addDuration(taskId, seconds) {
    var all = loadDurations();
    all[taskId] = Math.round(seconds);
    try { localStorage.setItem(durKey(), JSON.stringify(all)); } catch (e) {}
  }

  function removeDuration(taskId) {
    var all = loadDurations();
    if (Object.prototype.hasOwnProperty.call(all, taskId)) {
      delete all[taskId];
      try { localStorage.setItem(durKey(), JSON.stringify(all)); } catch (e) {}
    }
  }

  function totalDuration() {
    var all = loadDurations(), sum = 0, k;
    for (k in all) { if (Object.prototype.hasOwnProperty.call(all, k)) { sum += all[k] || 0; } }
    return sum;
  }

  function fmtDur(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    var s = sec % 60;
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    if (h > 0) { return h + ":" + p(m) + ":" + p(s); }
    return p(m) + ":" + p(s);
  }

  var running = false;
  var CAP_MODE_KEY = "dm.capture.mode";
  var captureMode = "near";
  var controlOnlyActive = false;
  var controlStartGeneration = 0;
  var controlDeployStarted = false;
  try {
    var savedCaptureMode = sessionStorage.getItem(CAP_MODE_KEY);
    if (["remote", "control"].indexOf(savedCaptureMode) >= 0) { captureMode = savedCaptureMode; }
  } catch (e) {}

  function renderCaptureMode() {
    var modeSwitch = $("#cap-mode-switch");
    if (modeSwitch) { modeSwitch.dataset.mode = captureMode; }
    $$('[data-capture-mode]').forEach(function (button) {
      button.setAttribute("aria-pressed", button.dataset.captureMode === captureMode ? "true" : "false");
      button.disabled = running;
    });
    var label = $("#cap-mode-label");
    if (label) {
      label.textContent = captureMode === "control" ? "远控仅控制" :
        captureMode === "remote" ? "远控模式" : "近控模式";
    }
    var startButton = $("#btn-new-task");
    if (startButton) { startButton.textContent = captureMode === "control" ? "进入控制" : "开始采集"; }
  }

  $$("[data-capture-mode]").forEach(function (button) {
    button.addEventListener("click", function () {
      if (running) { return; }
      captureMode = button.dataset.captureMode === "control" ? "control" :
        button.dataset.captureMode === "remote" ? "remote" : "near";
      try { sessionStorage.setItem(CAP_MODE_KEY, captureMode); } catch (e) {}
      renderCaptureMode();
    });
  });
  renderCaptureMode();

  function renderTasks() {
     
    refreshRecords();
    updateStats();
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function updateStats() {
     
    if (!window.dmApi) { return; }

    var elToday = $("#stat-capture-today");
    var elMonth = $("#stat-capture-month");

    window.dmApi.stats().then(function (d) {
      var live = running && timerStart ? (Date.now() - timerStart) / 1000 : 0;
      var cap = (d && d.capture) || {};
      if (elToday) {
        var t = (cap.today_s || 0) + live;
        elToday.textContent = t > 0 ? fmtDur(t) : "";
      }
      if (elMonth) {
        var m = (cap.month_s || 0) + live;
        elMonth.textContent = m > 0 ? fmtDur(m) : "";
      }
    }).catch(function () {});
  }

  var btnNew = $("#btn-new-task");
  if (btnNew) {
    btnNew.addEventListener("click", function () {
      if (running) { toast("当前有任务正在采集"); return; }
       
      fetchUploadState().then(function (s) {
        if (s && s.running) { startUpPoll(); modalBusyUploading(); return; }
        useCurrentDeviceThen(function () {
           
          var cfg = loadCfg();
          if (!cfg || !cfg.k7_dir || !cfg.rl_deploy_dir) {
             
            toast("这台机器狗的采集端没有程序配置（host_config.json 缺失），请检查采集端端安装");
            return;
          }
          if (captureMode === "control") { startControlOnly(cfg); }
          else { startCaptureSession("", cfg); }
        });
      });
    });
  }

  function waitForDirectCameras(cfg, attempts) {
    return br().camHealth(cfg).then(function (health) {
      var cams = health && health.cams;
      var ready = cams && CAM_KEYS.every(function (key) {
        return cams[key] && cams[key].online;
      });
      if (ready) { return health; }
      if (attempts <= 0) {
        var details = cams ? CAM_KEYS.map(function (key) {
          var cam = cams[key];
          return cam && !cam.online ? key + ": " + (cam.error || "未就绪") : "";
        }).filter(Boolean).join("；") : "采集端相机服务不可达";
        throw new Error("采集端直连三相机未全部就绪：" + details);
      }
      return new Promise(function (resolve) {
        setTimeout(resolve, 1000);
      }).then(function () { return waitForDirectCameras(cfg, attempts - 1); });
    });
  }

  function startControlOnly(cfg) {
    openModal({
      title: "进入远控仅控制模式？",
      html: "<p>该模式不创建采集会话，请确认机械臂周围安全。</p>",
      ok: "进入控制", cancel: "取消", danger: true
    }).then(function (confirmed) {
      if (!confirmed || running) { return; }
      running = true;
      var generation = ++controlStartGeneration;
      controlOnlyActive = true;
      controlDeployStarted = false;
      var controlCameraStarted = false;
      activeSession = null;
      sessionFolder = null;
      cfgForRetry = cfg;
      timerStart = 0;
      showLive(null);
      var title = $("#t-capture-live");
      if (title) { title.textContent = "远程控制"; }
      var dir = $("#cap-dir");
      if (dir) { dir.hidden = true; }
      var clock = $("#cap-clock");
      if (clock) { clock.hidden = true; }
      var exit = $("#cap-exit");
      if (exit) { exit.textContent = "退出控制"; }
      var records = $("#cap-records-card");
      if (records) { records.hidden = true; }

      setPhase("busy", "启动直连相机");
      Promise.resolve(br().camStart(cfg, logLine, "direct-webrtc")).then(function (camera) {
        if (!camera || !camera.ok) { throw new Error((camera && camera.error) || "直连相机服务启动失败"); }
        controlCameraStarted = !camera.already;
        return waitForDirectCameras(cfg, 15);
      }).then(function () {
        if (!running) { throw new Error("控制模式已取消"); }
        setPhase("busy", "等待三路视频就绪");
        return fetchFramePreview(cfg);
      }).then(function () {
        if (!running || generation !== controlStartGeneration) {
          throw new Error("__control_cancelled__");
        }
        setPhase("busy", "等待 K7 就绪");
        return br().t1(cfg, logLine);
      }).then(function (k7) {
        if (!running || generation !== controlStartGeneration) { throw new Error("__control_cancelled__"); }
        if (!k7 || !k7.ok) { throw new Error((k7 && k7.error) || "K7Master3 未就绪"); }
        setPhase("busy", "启动远控程序");
        return br().t2(cfg, logLine, true);
      }).then(function (deploy) {
        if (!deploy || !deploy.ok) { throw new Error((deploy && deploy.error) || "远控程序启动失败"); }
        controlDeployStarted = true;
        if (!running || generation !== controlStartGeneration) {
          return br().controlStop(cfg, logLine).then(function (stopped) {
            if (!stopped || !stopped.ok) {
              throw new Error((stopped && stopped.error) || "退出期间清理远控程序失败");
            }
            throw new Error("__control_cancelled__");
          });
        }
        return br().z(cfg, logLine);
      }).then(function (result) {
        if (result && result.ok === false) { throw new Error(result.error || "远控启动指令失败"); }
        if (!running || generation !== controlStartGeneration) {
          return br().controlStop(cfg, logLine).then(function (stopped) {
            if (!stopped || !stopped.ok) {
              throw new Error((stopped && stopped.error) || "退出期间清理远控程序失败");
            }
          });
        }
        if (!running) { return; }
        setPhase("run", "仅控制中");
      }).catch(function (error) {
        if (!running) {
          var cleanup = controlDeployStarted
            ? br().controlStop(cfg, logLine)
            : (controlCameraStarted ? br().camStop(cfg, logLine) : Promise.resolve({ ok: true }));
          Promise.resolve(cleanup).then(function (stopped) {
            if (!stopped || !stopped.ok) {
              setPhase("error", "退出控制清理失败");
              toast("退出控制时清理远控程序失败：" +
                ((stopped && stopped.error) || (error && error.message) || "未知错误"));
            }
          }).catch(function (cleanupError) {
            setPhase("error", "退出控制清理失败");
            toast("退出控制时清理远控程序失败：" + (cleanupError.message || cleanupError));
          });
          return;
        }
        running = false;
        stopFramePreview();
        setPhase("error", "控制启动失败");
        if (controlDeployStarted) {
          br().controlStop(cfg, logLine).catch(function () {});
        } else if (controlCameraStarted) {
          br().camStop(cfg, logLine).catch(function () {});
        }
        openModal({
          title: "远控启动失败",
          html: "<p>" + esc(error.message || error) + "</p>",
          ok: "知道了", noCancel: true
        }).then(function () {
          controlOnlyActive = false;
          controlDeployStarted = false;
          renderCaptureMode();
          activeSession = null;
          cfgForRetry = null;
          var records = $("#cap-records-card");
          if (records) { records.hidden = true; }
          lastFoldersSignature = null;
          lastFolders = [];
          loadFolders();
          if (title) { title.textContent = "采集"; }
          if (dir) { dir.hidden = false; }
          if (clock) { clock.hidden = false; }
          if (exit) { exit.textContent = "退出采集"; }
          hideLive();
        });
      });
    });
  }

  function dataDir(cfg) {
    var c = cfg || loadCfg() || CFG_DEFAULT;
    var d = (c && c.local_data_dir) ? String(c.local_data_dir).trim() : "";
    return d || CFG_DEFAULT.local_data_dir || "datasets";
  }

  function startCaptureSession(folder, cfg) {
    if (controlOnlyActive) { toast("仅控制模式已关闭采集功能"); return; }
    var base = dataDir(cfg);

    if (!window.dmApi) { toast("接口模块未加载"); return; }

    window.dmApi.preflight(base, folder).then(function (pre) {
      if (!pre.ok) {
         
        openModal({
          title: "无法开始采集",
          html: "<p>" + esc(pre.warnings.join("；")) + "</p>" +
                "<p>数据目录：" + esc(pre.abs_path || "") + "</p>",
          noCancel: true, ok: "知道了", danger: true
        });
        return;
      }
       
      return doStart(base, folder, pre);
    }).catch(function (e) {
       
      var msg = (e && e.message) || String(e);
      var isOffline = !e || e.code === 502 || e.code === 503 ||
                      /Failed to fetch|NetworkError|Load failed/i.test(msg);
      var hint = isOffline
        ? "后端服务未连接（127.0.0.1:8765 未响应）"
        : msg;
      openModal({
        title: "预检失败",
        html: "<p>" + esc(hint) + "</p>" +
              (isOffline
                ? "<p>请确认后端已启动：<code>python3 server/api.py</code></p>"
                : "<p>数据目录：<code>" + esc(base) + "</code></p>"),
        noCancel: true, ok: "知道了", danger: true
      });
      try { console.debug("dm: 预检失败 " + msg); } catch (err) {}
    });
  }

  function doStart(base, folder, pre) {
    if (controlOnlyActive) { toast("仅控制模式已关闭采集功能"); return Promise.resolve(null); }
    return window.dmApi.startSession(base, folder).then(function (s) {
      if (s.resumed) {
        return window.dmApi.runStatus().then(function (status) {
          if (status && status.rl_deploy && status.rl_deploy.running) { return s; }
          return window.dmApi.endSession(s.id, 0, "error", "采集进程已停止，已关闭遗留会话")
            .then(function () { return window.dmApi.startSession(base, folder); });
        });
      }
      return s;
    }).then(function (s) {
      if (!s) { return null; }
       
      if (window.dmDataDir && s.dir_path) { window.dmDataDir.set(s.dir_path); }
      var session = {
        id: s.id,
        folder: s.folder || folder || "",
        dirPath: s.dir_path,
         
        collectorPath: s.collector_path || "",
        absPath: s.abs_path,
        startedAt: s.started_at
      };
      var runCfg = loadCfg() || CFG_DEFAULT;
      if (s.collector_path) { runCfg.run_data_dir = s.collector_path; }
      if (s.resumed) {
        resumeSession(session, runCfg);
      } else {
        runSession(session, runCfg);
      }
      return session;
    }).catch(function (e) {
       
      if (e && (e.code === 409 || /正在上传/.test(e.message || ""))) {
        modalBusyUploading();
        return null;
      }
      toast("创建会话失败：" + (e.message || e));
      return null;
    });
  }

  var activeSession = null;
  var sessionFolder = null;    
  var cfgForRetry = null;

  var liveEl = $("#cap-live");
  var idleEl = $("#cap-idle");

  function showLive(session) {
    if (idleEl) { idleEl.hidden = true; }
    if (liveEl) { liveEl.hidden = false; }
    var dirEl = $("#cap-dir");
    if (dirEl) {
       
      dirEl.textContent = session
        ? (session.collectorPath || session.dirPath || "") : "";
    }
    setPhase("idle", "准备中");
    renderCaptureMode();
    if (!controlOnlyActive) { refreshRecords(); }
  }

  function hideLive() {
    if (liveEl) { liveEl.hidden = true; }
    if (idleEl) { idleEl.hidden = false; }
  }

  var CAM_KEYS = ["wrist", "global", "third"];
  var frameTimer = null;
  var camHealthTimer = null;
  var PREVIEW_INTERVAL_MS = 200;
  var RECORDS_REFRESH_MS = 2000;
  var previewGeneration = 0;
  var previewControllers = {};
  var previewUrls = {};
  var REMOTE_PROFILES = ["remote", "remote_low", "remote_min"];
  var CONTROL_PROFILES = ["control", "control_low", "control_min"];
  var remoteProfileIndex = 0;
  var remoteFrameTimes = { wrist: [], global: [], third: [] };
  var remoteSourceFps = { wrist: 0, global: 0, third: 0 };
  var remoteReconnectAttempts = { wrist: 0, global: 0, third: 0 };
  var remoteStreamStarted = { wrist: 0, global: 0, third: 0 };
  var remoteReconnectTimers = {};
  var remoteWatchTimer = null;
  var remoteAdaptTimer = null;
  var remoteHealthySince = 0;
  var remoteSlowSince = 0;
  var remoteProfileChangedAt = 0;
  var remoteRenderers = {};
  var remoteReceivedAt = { wrist: 0, global: 0, third: 0 };
  var remoteStatusUpdatedAt = 0;
  var controlPeer = null;
  var controlPeerSession = "";
  var controlVideoEls = {};
  var controlReconnectTimer = null;
  var controlReconnectAttempts = 0;

  var camHadFrame = { wrist: false, global: false, third: false };

  function setCamFrame(cam, src) {
    var img = $("#cap-img-" + cam);
    if (!img) { return; }
    var panel = img.closest(".cap-cam");
    var ph = img.parentElement.querySelector(".cap-cam__ph");
    var phText = ph ? ph.querySelector("p") : null;

    if (src) {
      if (img.getAttribute("src") !== src) { img.src = src; }
      if (img.hidden) { img.hidden = false; }
      if (ph && !ph.hidden) { ph.hidden = true; }
      camHadFrame[cam] = true;
      if (panel && panel.classList.contains("is-lost")) { panel.classList.remove("is-lost"); }
    } else {
      img.removeAttribute("src");
      img.hidden = true;
      if (ph) { ph.hidden = false; }
       
      if (camHadFrame[cam]) {
        if (phText) { phText.textContent = "画面已断开"; }
        if (panel) { panel.classList.add("is-lost"); }
      } else {
        if (phText) { phText.textContent = "无实时画面"; }
        if (panel) { panel.classList.remove("is-lost"); }
      }
    }
  }

  function createMjpegParser() {
    var header = new Uint8Array(2048);
    var headerLength = 0;
    var bodyLength = 0;
    var body = null;
    var bodyOffset = 0;
    var frameSeq = null;
    var frameTime = null;
    var invalid = false;
    return {
      push: function (bytes) {
        var latest = null;
        var latestSeq = null;
        var latestTime = null;
        var offset = 0;
        while (!invalid && offset < bytes.length) {
          if (!bodyLength) {
            if (headerLength === header.length) { invalid = true; break; }
            header[headerLength++] = bytes[offset++];
            var n = headerLength;
            if (n < 4 || header[n - 4] !== 13 || header[n - 3] !== 10 ||
                header[n - 2] !== 13 || header[n - 1] !== 10) { continue; }
            var text = String.fromCharCode.apply(null, header.subarray(0, n - 4));
            var match = /(?:^|\r\n)Content-Length:\s*(\d+)\s*(?:\r\n|$)/i.exec(text);
            bodyLength = match ? Number(match[1]) : 0;
            if (!Number.isSafeInteger(bodyLength) || bodyLength <= 0 || bodyLength > 8 * 1024 * 1024) {
              invalid = true; break;
            }
            var seq = /(?:^|\r\n)X-Frame-Seq:\s*(\d+)\s*(?:\r\n|$)/i.exec(text);
            var captured = /(?:^|\r\n)X-Capture-Time:\s*(\d+(?:\.\d+)?)\s*(?:\r\n|$)/i.exec(text);
            frameSeq = seq && Number.isSafeInteger(Number(seq[1])) ? Number(seq[1]) : null;
            frameTime = captured && Number.isFinite(Number(captured[1])) ? Number(captured[1]) : null;
            headerLength = 0;
            continue;
          }
          var available = Math.min(bodyLength - bodyOffset, bytes.length - offset);
          if (!body && available === bodyLength) {
            latest = bytes.subarray(offset, offset + available);
          } else {
            if (!body) { body = new Uint8Array(bodyLength); }
            body.set(bytes.subarray(offset, offset + available), bodyOffset);
          }
          bodyOffset += available;
          offset += available;
          if (bodyOffset === bodyLength) {
            if (body) { latest = body; }
            latestSeq = frameSeq;
            latestTime = frameTime;
            body = null;
            bodyLength = 0;
            bodyOffset = 0;
          }
        }
        return { frame: latest, seq: latestSeq, capturedAt: latestTime, invalid: invalid };
      }
    };
  }

  function createFrameDelayTracker() {
    var minimumOffset = Infinity;
    var previousCapture = 0;
    var delayedSince = 0;
    var delayedFrames = 0;
    return {
      late: function (capturedAt, receivedAt) {
        if (!Number.isFinite(capturedAt) || capturedAt <= 0) { return false; }
        var offset = receivedAt - capturedAt;
        if (capturedAt < previousCapture) { minimumOffset = offset; }
        previousCapture = capturedAt;
        minimumOffset = Math.min(minimumOffset, offset);
        if (offset - minimumOffset < 800) {
          delayedSince = 0;
          delayedFrames = 0;
          return false;
        }
        if (!delayedFrames) { delayedSince = receivedAt; }
        delayedFrames++;
        return delayedFrames >= 3 && receivedAt - delayedSince >= 1000;
      }
    };
  }

  function setRemoteStatus(text, warning) {
    var box = $("#cap-preview-health");
    var label = $("#cap-preview-status");
    if (box) {
      if (box.hidden) { box.hidden = false; }
      if (box.classList.contains("is-warning") !== !!warning) {
        box.classList.toggle("is-warning", !!warning);
      }
    }
    if (label && label.textContent !== text) { label.textContent = text; }
  }

  function remoteRate(cam, now) {
    var times = remoteFrameTimes[cam];
    while (times.length && now - times[0] > 2000) { times.shift(); }
    if (times.length < 2 || times[times.length - 1] === times[0]) { return 0; }
    return Math.round((times.length - 1) * 1000 / (times[times.length - 1] - times[0]));
  }

  var controlMetricTimer = null;
  var controlMetrics = {};
  var controlHistory = [];
  var controlColors = ["#1683ff", "#19a974", "#e89016"];
  var controlNames = { wrist: "腕", global: "头", third: "第三" };

  function controlNow() {
    return window.performance && window.performance.now
      ? window.performance.now() : Date.now();
  }

  function watchControlFrames(cam, video, generation) {
    if (typeof video.requestVideoFrameCallback !== "function") {
      controlMetrics[cam] = { unsupported: true, previous: null, lastFrame: null,
        latency: [], intervals: [], frames: 0, sampledAt: controlNow(), callback: null };
      return;
    }
    var state = { unsupported: false, captureTimeSeen: false, previous: null, lastFrame: null, latency: [], intervals: [], frames: 0,
      sampledAt: controlNow(), callback: null };
    controlMetrics[cam] = state;
    function onFrame(now, metadata) {
      if (generation !== previewGeneration || controlMetrics[cam] !== state) { return; }
      var shownAt = metadata.expectedDisplayTime;
      if (Number.isFinite(shownAt) && (state.previous === null || shownAt > state.previous)) {
        if (state.previous !== null) { state.intervals.push(shownAt - state.previous); }
        state.previous = shownAt;
        state.lastFrame = now;
        state.frames++;
        var delay = shownAt - metadata.captureTime;
        if (Number.isFinite(metadata.captureTime)) {
          state.captureTimeSeen = true;
          if (Number.isFinite(delay) && delay >= 0) { state.latency.push(delay); }
        }
      }
      state.callback = video.requestVideoFrameCallback(onFrame);
    }
    state.callback = video.requestVideoFrameCallback(onFrame);
  }

  function drawControlMetric(id, index, minimum, step, unit) {
    var canvas = $("#cap-" + id + "-chart");
    if (!canvas || !canvas.clientWidth) { return; }
    var width = canvas.clientWidth;
    var height = canvas.clientHeight;
    var scale = Math.min(window.devicePixelRatio || 1, 2);
    var w = Math.round(width * scale), h = Math.round(height * scale);
    if (canvas.width !== w) { canvas.width = w; }
    if (canvas.height !== h) { canvas.height = h; }
    var ctx = canvas.getContext("2d");
    if (!ctx) { return; }
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.clearRect(0, 0, width, height);
    var top = 9, bottom = height - 13;
    var max = minimum;
    controlHistory.forEach(function (point) {
      CAM_KEYS.forEach(function (cam) { max = Math.max(max, point[cam][index] || 0); });
    });
    max = Math.ceil(max / step) * step;
    ctx.strokeStyle = "rgba(100, 116, 139, .16)";
    ctx.lineWidth = 1;
    [top, (top + bottom) / 2, bottom].forEach(function (y) {
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke();
    });
    ctx.font = "9px " + getComputedStyle(canvas).fontFamily;
    ctx.fillStyle = "#505765";
    ctx.fillText(max + " " + unit, 2, 8);
    ctx.fillText("60 秒前", 2, height - 1);
    ctx.textAlign = "right";
    ctx.fillText("现在", width - 2, height - 1);
    ctx.textAlign = "left";
    var latest = controlHistory[controlHistory.length - 1];
    CAM_KEYS.forEach(function (cam, i) {
      ctx.strokeStyle = controlColors[i];
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      var drawing = false;
      controlHistory.forEach(function (point) {
        var value = point[cam][index];
        if (value === null) { drawing = false; return; }
        var x = width * (1 - (latest.at - point.at) / 60000);
        var y = bottom - value / max * (bottom - top);
        if (drawing) { ctx.lineTo(x, y); }
        else { ctx.moveTo(x, y); ctx.lineTo(x + 0.1, y); drawing = true; }
      });
      ctx.stroke();
    });
    var output = $("#cap-" + id + "-value");
    var values = CAM_KEYS.map(function (cam) {
      var value = latest[cam][index];
      return controlNames[cam] + " " + (value === null ? "—" : Math.round(value));
    });
    if (output) {
      output.textContent = values.every(function (v) { return /—$/.test(v); })
        ? "暂无数据" : values.join(" · ") + " " + unit;
    }
  }

  function sampleControlMetrics() {
    if (document.hidden || !running || captureMode !== "control") { return; }
    var now = controlNow();
    var point = { at: now };
    var unsupported = 0;
    var captureTimes = 0;
    var activeFrames = 0;
    CAM_KEYS.forEach(function (cam) {
      var state = controlMetrics[cam];
      if (!state) { point[cam] = [null, null, null]; return; }
      if (state.unsupported) {
        unsupported++;
        point[cam] = [null, null, null];
        return;
      }
      var elapsed = now - state.sampledAt;
      var stale = state.lastFrame === null || now - state.lastFrame > 2000 || elapsed > 2500;
      if (!stale) { activeFrames++; }
      if (state.captureTimeSeen) { captureTimes++; }
      var average = function (samples) {
        return !stale && samples.length
          ? samples.reduce(function (sum, value) { return sum + value; }, 0) / samples.length : null;
      };
      var fps = !stale && elapsed > 0 ? state.frames * 1000 / elapsed : 0;
      point[cam] = [average(state.latency), average(state.intervals), fps];
      state.frames = 0;
      state.latency = [];
      state.intervals = [];
      state.sampledAt = now;
      var label = $('[data-cam-fps="' + cam + '"]');
      if (label) { label.textContent = Math.round(fps) + " FPS"; }
    });
    controlHistory.push(point);
    while (now - controlHistory[0].at > 60000) { controlHistory.shift(); }
    drawControlMetric("e2e", 0, 100, 50, "ms");
    drawControlMetric("interval", 1, 50, 25, "ms");
    drawControlMetric("fps", 2, 30, 15, "FPS");
    var e2eOutput = $("#cap-e2e-value");
    if (e2eOutput && captureTimes === 0) {
      e2eOutput.textContent = unsupported === CAM_KEYS.length
        ? "浏览器不支持逐帧测量"
        : (activeFrames ? "浏览器未提供采集时间戳" : "等待视频帧");
    }
  }

  function startControlLatency() {
    var panel = $("#cap-latency");
    if (panel) { panel.hidden = false; }
    controlHistory = [];
    controlMetrics = {};
    sampleControlMetrics();
    controlMetricTimer = setInterval(sampleControlMetrics, 1000);
  }

  function clearRemoteRenderers(cam) {
    var keys = cam ? [cam] : Object.keys(remoteRenderers);
    keys.forEach(function (key) {
      var state = remoteRenderers[key];
      if (!state) { return; }
      state.pending = null;
      state.img.onload = null;
      state.img.onerror = null;
      if (state.animation && typeof window.cancelAnimationFrame === "function") {
        window.cancelAnimationFrame(state.animation);
      }
      if (state.previous) { URL.revokeObjectURL(state.previous); }
      delete remoteRenderers[key];
    });
  }

  function showRemoteFrame(cam, bytes, generation) {
    if (!running || generation !== previewGeneration) { return; }
    remoteReceivedAt[cam] = Date.now();
    remoteReconnectAttempts[cam] = 0;
    var state = remoteRenderers[cam];
    if (!state) {
      var img = $("#cap-img-" + cam);
      if (!img) { return; }
      state = { img: img, pending: null, busy: false, previous: null, animation: null, uiAt: 0 };
      remoteRenderers[cam] = state;
    }
    state.pending = bytes;
    if (state.busy) { return; }
    function current() {
      return running && generation === previewGeneration && remoteRenderers[cam] === state;
    }
    function paint() {
      state.animation = null;
      if (!current()) { return; }
      var latest = state.pending;
      state.pending = null;
      if (!latest) { state.busy = false; return; }
      state.busy = true;
      state.decodeAt = Date.now();
      var url = URL.createObjectURL(new Blob([latest], { type: "image/jpeg" }));
      state.previous = previewUrls[cam] || null;
      previewUrls[cam] = url;
      function finished(loaded) {
        if (!current() || previewUrls[cam] !== url) { return; }
        if (state.previous) { URL.revokeObjectURL(state.previous); state.previous = null; }
        if (loaded) {
          var now = Date.now();
          remoteFrameTimes[cam].push(now);
          setCamFrame(cam, url);
          if (now - state.uiAt >= 500) {
            state.uiAt = now;
            var fps = remoteRate(cam, now);
            var label = $("[data-cam-fps=\"" + cam + "\"]");
            var text = fps ? fps + " FPS" : "正在取流";
            if (label && label.textContent !== text) { label.textContent = text; }
          }
          if (now - remoteStatusUpdatedAt >= 500) {
            remoteStatusUpdatedAt = now;
            var healthy = CAM_KEYS.every(function (key) {
              return now - (remoteFrameTimes[key].slice(-1)[0] || 0) < 2000;
            });
            if (healthy) { setRemoteStatus("三路画面已连接", false); }
          }
        }
        state.img.onload = null;
        state.img.onerror = null;
        if (state.pending && typeof window.requestAnimationFrame === "function") {
          state.animation = window.requestAnimationFrame(paint);
        } else {
          state.busy = false;
          if (state.pending) { paint(); }
        }
      }
      state.img.onload = function () { finished(true); };
      state.img.onerror = function () { finished(false); };
      state.img.src = url;
    }
    paint();
  }

  function scheduleRemoteReconnect(cam, cfg, generation) {
    if (!running || generation !== previewGeneration || remoteReconnectTimers[cam]) { return; }
    var attempt = remoteReconnectAttempts[cam]++;
    var delay = Math.min(8000, 500 * Math.pow(2, Math.min(attempt, 4)));
    setRemoteStatus("画面连接中断，正在重连", true);
    remoteReconnectTimers[cam] = setTimeout(function () {
      delete remoteReconnectTimers[cam];
      if (running && generation === previewGeneration) { runRemoteStream(cam, cfg, generation); }
    }, delay);
  }

  function runRemoteStream(cam, cfg, generation) {
    if (!running || generation !== previewGeneration) { return; }
    var bridge = window.dmBridge;
    var controller = new AbortController();
    previewControllers[cam] = controller;
    remoteStreamStarted[cam] = Date.now();
    var parser = createMjpegParser();
    var delayTracker = createFrameDelayTracker();
    var lastSeq = null;
    Promise.resolve().then(function () {
      if (!running || generation !== previewGeneration || controller.signal.aborted) { return null; }
      var profiles = captureMode === "control" ? CONTROL_PROFILES : REMOTE_PROFILES;
      var profile = profiles[remoteProfileIndex];
      return fetch(bridge.streamUrl(cfg, cam, profile), {
        cache: "no-store", credentials: "omit", mode: "cors", signal: controller.signal
      });
    }).then(function (response) {
      if (!running || generation !== previewGeneration || controller.signal.aborted) { return; }
      if (!response.ok || !response.body || !response.body.getReader) {
        throw new Error("远控视频流不可用");
      }
      var reader = response.body.getReader();
      function read() {
        return reader.read().then(function (part) {
          if (part.done || !running || generation !== previewGeneration) { throw new Error("视频流已关闭"); }
          var parsed = parser.push(part.value);
          if (parsed.invalid) { throw new Error("视频流格式错误"); }
          if (parsed.frame) {
            var receivedAt = window.performance && typeof window.performance.now === "function"
              ? window.performance.now() : Date.now();
            if (!document.hidden && delayTracker.late(parsed.capturedAt, receivedAt)) {
              var profiles = captureMode === "control" ? CONTROL_PROFILES : REMOTE_PROFILES;
              if (remoteProfileIndex < profiles.length - 1) {
                remoteProfileIndex++;
                remoteProfileChangedAt = Date.now();
                remoteSlowSince = 0;
                remoteHealthySince = 0;
                setRemoteStatus("检测到视频积压，正在降低码率并恢复实时画面", true);
                restartRemoteStreams(cfg);
                return;
              }
              throw new Error("视频流积压");
            }
            if (parsed.seq === null || parsed.seq !== lastSeq) {
              lastSeq = parsed.seq;
              showRemoteFrame(cam, parsed.frame, generation);
            }
          }
          return read();
        });
      }
      return read();
    }).catch(function () {
      var retry = !controller.signal.aborted && running && generation === previewGeneration;
      controller.abort();
      if (retry) { scheduleRemoteReconnect(cam, cfg, generation); }
    }).then(function () {
      if (previewControllers[cam] === controller) { delete previewControllers[cam]; }
    });
  }

  function restartRemoteStreams(cfg) {
    previewGeneration++;
    var generation = previewGeneration;
    clearRemoteRenderers();
    Object.keys(previewControllers).forEach(function (cam) {
      try { previewControllers[cam].abort(); } catch (e) {}
    });
    previewControllers = {};
    Object.keys(remoteReconnectTimers).forEach(function (cam) {
      clearTimeout(remoteReconnectTimers[cam]);
      delete remoteReconnectTimers[cam];
    });
    CAM_KEYS.forEach(function (cam) {
      remoteFrameTimes[cam] = [];
      remoteReceivedAt[cam] = 0;
      remoteReconnectAttempts[cam] = 0;
      runRemoteStream(cam, cfg, generation);
    });
  }

  function startRemoteAdaptation(cfg) {
    if (remoteWatchTimer) { clearInterval(remoteWatchTimer); }
    if (remoteAdaptTimer) { clearInterval(remoteAdaptTimer); }
    remoteWatchTimer = setInterval(function () {
      var now = Date.now();
      var stale = false;
      CAM_KEYS.forEach(function (cam) {
        var last = remoteReceivedAt[cam];
        var timedOut = last ? now - last > 2200 : now - remoteStreamStarted[cam] > 5000;
        var renderer = remoteRenderers[cam];
        if (!document.hidden && renderer && renderer.busy && !renderer.animation && now - renderer.decodeAt > 2500) {
          clearRemoteRenderers(cam);
          timedOut = true;
        }
        if (timedOut && previewControllers[cam] && !previewControllers[cam].signal.aborted) {
          previewControllers[cam].abort();
          scheduleRemoteReconnect(cam, cfg, previewGeneration);
          stale = true;
        }
        if (!last || now - last > 2200) { stale = true; }
      });
      if (stale) { setRemoteStatus("视频链路不稳定，正在恢复画面", true); }
    }, 500);
    remoteAdaptTimer = setInterval(function () {
      var rates = CAM_KEYS.map(function (cam) { return remoteRate(cam, Date.now()); });
      var sources = CAM_KEYS.map(function (cam) { return remoteSourceFps[cam] || 0; });
      var now = Date.now();
      var profiles = captureMode === "control" ? CONTROL_PROFILES : REMOTE_PROFILES;
      var targets = sources.map(function (fps) {
        return captureMode === "control" ? fps : Math.min(fps, [30, 24, 18][remoteProfileIndex]);
      });
      var slow = CAM_KEYS.filter(function (cam, i) {
        var times = remoteFrameTimes[cam];
        var hasRecentSamples = times.length >= 2 && now - times[times.length - 1] < 1500;
        return hasRecentSamples && targets[i] >= 15 && rates[i] < targets[i] * 0.82;
      }).length;
      if (slow && remoteProfileIndex < profiles.length - 1) {
        remoteHealthySince = 0;
        if (!remoteSlowSince) { remoteSlowSince = now; }
        if (now - remoteSlowSince >= 4000 && now - remoteProfileChangedAt >= 10000) {
          remoteSlowSince = 0;
          remoteProfileChangedAt = now;
          remoteProfileIndex++;
          setRemoteStatus("正在降低预览码率，优先保持画面帧率", true);
          restartRemoteStreams(cfg);
        }
      } else if (remoteProfileIndex > 0 && rates.every(function (rate, i) {
        return targets[i] > 0 && rate >= targets[i] * 0.9;
      })) {
        remoteSlowSince = 0;
        if (!remoteHealthySince) { remoteHealthySince = now; }
        if (now - remoteHealthySince >= 20000) {
          remoteHealthySince = 0;
          remoteProfileChangedAt = now;
          remoteProfileIndex--;
          setRemoteStatus("网络稳定，正在恢复预览画质", false);
          restartRemoteStreams(cfg);
        }
      } else {
        remoteSlowSince = 0;
        remoteHealthySince = 0;
      }
    }, 2000);
  }

  function startCamWatchdog(cfg) {
    var check = function () {
      if (!running) { return; }
      var bridge = window.dmBridge;
      if (!bridge || typeof bridge.camHealth !== "function") { return; }
      var generation = previewGeneration;
      Promise.resolve().then(function () { return bridge.camHealth(cfg); }).then(function (h) {
        if (!running || generation !== previewGeneration || !h || !h.cams) { return; }
        CAM_KEYS.forEach(function (cam) {
          var info = h.cams[cam];
          if (!info) { return; }
          remoteSourceFps[cam] = Number(info.source_fps || info.fps || 0);
          if (!info.online) { setCamFrame(cam, null); return; }
          var img = $("#cap-img-" + cam);
           
          if (typeof bridge.frame !== "function" && (!img || !img.getAttribute("src") || img.hidden)) {
            setCamFrame(cam, bridge.streamUrl(cfg, cam));
          }
        });
      }).catch(function () {   });
    };
    check();
    camHealthTimer = setInterval(check, 3000);
  }

  function parseVideoMids(sdp) {
    var result = {};
    var sections = String(sdp || "").split(/(?=^m=)/m);
    var index = 0;
    sections.forEach(function (section) {
      if (!/^m=video/m.test(section)) { return; }
      var match = section.match(/^a=mid:(.+)$/m);
      if (match && CAM_KEYS[index]) { result[match[1].trim()] = CAM_KEYS[index]; }
      index++;
    });
    return result;
  }

  function clearControlVideos() {
    Object.keys(controlVideoEls).forEach(function (cam) {
      var video = controlVideoEls[cam];
      var state = controlMetrics[cam];
      delete controlMetrics[cam];
      if (state && state.callback !== null && video.cancelVideoFrameCallback) {
        video.cancelVideoFrameCallback(state.callback);
      }
      var fps = $('[data-cam-fps="' + cam + '"]');
      if (fps) { fps.textContent = ""; }
      try { video.pause(); video.srcObject = null; video.remove(); } catch (e) {}
      var img = $("#cap-img-" + cam);
      if (img) { img.hidden = false; }
      var panel = img && img.closest(".cap-cam");
      var placeholder = panel && panel.querySelector(".cap-cam__ph");
      if (placeholder) { placeholder.hidden = false; }
    });
    controlVideoEls = {};
  }

  function closeControlPeer() {
    var peer = controlPeer;
    var sessionId = controlPeerSession;
    controlPeer = null;
    controlPeerSession = "";
    if (peer) { try { peer.close(); } catch (e) {} }
    clearControlVideos();
    var bridge = window.dmBridge;
    if (sessionId && bridge && bridge.webrtcClose) {
      return Promise.resolve().then(function () {
        return bridge.webrtcClose(sessionId);
      }).catch(function () {});
    }
    return Promise.resolve();
  }

  function waitForIceGathering(peer, timeoutMs) {
    if (peer.iceGatheringState === "complete") { return Promise.resolve(); }
    return new Promise(function (resolve, reject) {
      var timeout = setTimeout(function () {
        peer.removeEventListener("icegatheringstatechange", changed);
        reject(new Error("ICE 候选收集超时"));
      }, timeoutMs);
      function changed() {
        if (peer.iceGatheringState !== "complete") { return; }
        clearTimeout(timeout);
        peer.removeEventListener("icegatheringstatechange", changed);
        resolve();
      }
      peer.addEventListener("icegatheringstatechange", changed);
    });
  }

  function waitForControlVideoFrames(generation, timeoutMs) {
    return new Promise(function (resolve, reject) {
      var deadline = Date.now() + timeoutMs;
      var timer = setInterval(function () {
        if (!running || generation !== previewGeneration) {
          clearInterval(timer);
          reject(new Error("控制模式已取消"));
          return;
        }
        var ready = CAM_KEYS.every(function (cam) {
          var video = controlVideoEls[cam];
          return video && video.readyState >= 2 && video.videoWidth > 0;
        });
        if (ready) {
          clearInterval(timer);
          resolve(true);
        } else if (Date.now() >= deadline) {
          clearInterval(timer);
          reject(new Error("等待三路 H.264 视频画面超时，未启动遥操程序"));
        }
      }, 100);
    });
  }

  function scheduleControlReconnect(cfg, generation, grace) {
    if (!running || captureMode !== "control" || generation !== previewGeneration || controlReconnectTimer) { return; }
    var delay = Math.max(grace || 0, Math.min(8000, 500 * Math.pow(2, Math.min(controlReconnectAttempts++, 4))));
    setRemoteStatus("H.264 视频连接中断，正在重连", true);
    controlReconnectTimer = setTimeout(function () {
      controlReconnectTimer = null;
      if (!running || generation !== previewGeneration) { return; }
      closeControlPeer().then(function () {
        if (running && generation === previewGeneration) {
          startControlWebRTC(cfg, generation);
        }
      });
    }, delay);
  }

  function startControlWebRTC(cfg, generation) {
    if (!running || captureMode !== "control" || generation !== previewGeneration) { return; }
    if (!window.RTCPeerConnection) {
      setRemoteStatus("当前浏览器不支持 WebRTC，请使用 Chrome 或 Edge", true);
      return;
    }
    var bridge = window.dmBridge;
    if (!bridge || !bridge.webrtcOffer) {
      setRemoteStatus("服务端尚未提供 WebRTC 信令接口", true);
      return;
    }
    var id = window.crypto && window.crypto.randomUUID
      ? window.crypto.randomUUID() : "dm-" + Date.now() + "-" + Math.random().toString(16).slice(2);
    var peer = new RTCPeerConnection({ iceServers: [] });
    controlPeer = peer;
    controlPeerSession = id;
    var midMap = {};
    CAM_KEYS.forEach(function () {
      var transceiver = peer.addTransceiver("video", { direction: "recvonly" });
      if ("jitterBufferTarget" in transceiver.receiver) { transceiver.receiver.jitterBufferTarget = 0; }
    });
    peer.ontrack = function (event) {
      var cam = midMap[event.transceiver && event.transceiver.mid];
      if (!cam || !running || generation !== previewGeneration) { return; }
      var img = $("#cap-img-" + cam);
      var panel = img && img.closest(".cap-cam");
      if (!img || !panel) { return; }
      var video = controlVideoEls[cam];
      if (!video) {
        video = document.createElement("video");
        video.className = "cap-control-video";
        video.autoplay = true;
        video.playsInline = true;
        video.muted = true;
        video.style.width = "100%";
        video.style.height = "100%";
        video.style.objectFit = "contain";
        video.style.position = "absolute";
        video.style.inset = "0";
        video.style.display = "block";
        img.parentElement.insertBefore(video, img.nextSibling);
        controlVideoEls[cam] = video;
      }
      img.hidden = true;
      var placeholder = panel.querySelector(".cap-cam__ph");
      if (placeholder) { placeholder.hidden = true; }
      video.srcObject = event.streams && event.streams[0]
        ? event.streams[0] : new MediaStream([event.track]);
      if (!controlMetrics[cam]) { watchControlFrames(cam, video, generation); }
      video.play().catch(function () {});
      remoteReceivedAt[cam] = Date.now();
      setRemoteStatus("采集端 H.264 直连视频已连接", false);
    };
    peer.onconnectionstatechange = function () {
      if (!running || generation !== previewGeneration) { return; }
      if (peer.connectionState === "connected") {
        if (controlReconnectTimer) {
          clearTimeout(controlReconnectTimer);
          controlReconnectTimer = null;
        }
        controlReconnectAttempts = 0;
        setRemoteStatus("采集端 H.264 直连视频已连接", false);
      } else if (peer.connectionState === "failed" || peer.connectionState === "disconnected") {
        scheduleControlReconnect(cfg, generation, peer.connectionState === "disconnected" ? 2500 : 0);
      }
    };
    peer.createOffer().then(function (offer) {
      if (generation !== previewGeneration) { throw new Error("预览已关闭"); }
      return peer.setLocalDescription(offer);
    }).then(function () {
      return waitForIceGathering(peer, 10000);
    }).then(function () {
      if (generation !== previewGeneration) { throw new Error("预览已关闭"); }
      return bridge.webrtcOffer({ session_id: id, sdp: peer.localDescription.sdp });
    }).then(function (answer) {
      if (!answer || answer.type !== "answer" || !answer.sdp) { throw new Error("采集端返回的 SDP answer 无效"); }
      midMap = parseVideoMids(answer.sdp);
      return peer.setRemoteDescription({ type: answer.type, sdp: answer.sdp });
    }).catch(function (error) {
      if (!running || generation !== previewGeneration) { return; }
      scheduleControlReconnect(cfg, generation);
      setRemoteStatus("H.264/WebRTC 建连失败，正在重试：" + (error.message || error), true);
    });
  }

  function fetchFramePreview(cfg) {
    stopFramePreview();
    CAM_KEYS.forEach(function (cam) { setCamFrame(cam, null); });
    var bridge = window.dmBridge;
    if (!bridge) { return; }

    var stage = $("#cap-stage");
    var healthBox = $("#cap-preview-health");
    if (captureMode === "remote" || captureMode === "control") {
      if (stage) { stage.classList.toggle("is-control", captureMode === "control"); }
      var latencyPanel = $("#cap-latency");
      if (captureMode === "control") { startControlLatency(); }
      else if (latencyPanel) { latencyPanel.hidden = true; }
      if (healthBox) { healthBox.hidden = false; }
      setRemoteStatus("正在连接采集端三路视频", false);
      remoteProfileIndex = captureMode === "control" ? CONTROL_PROFILES.length - 1 : 0;
      remoteHealthySince = 0;
      remoteSlowSince = 0;
      remoteProfileChangedAt = Date.now();
      remoteStatusUpdatedAt = 0;
      remoteFrameTimes = { wrist: [], global: [], third: [] };
      remoteReceivedAt = { wrist: 0, global: 0, third: 0 };
      remoteSourceFps = { wrist: 0, global: 0, third: 0 };
      var generation = ++previewGeneration;
      if (captureMode === "control") {
        controlReconnectAttempts = 0;
        startControlWebRTC(cfg, generation);
        startCamWatchdog(cfg);
        return waitForControlVideoFrames(generation, 30000);
      }
      CAM_KEYS.forEach(function (cam) { runRemoteStream(cam, cfg, generation); });
      startRemoteAdaptation(cfg);
      startCamWatchdog(cfg);
      return;
    }
    if (stage) { stage.classList.remove("is-control"); }
    var latencyPanel = $("#cap-latency");
    if (latencyPanel) { latencyPanel.hidden = true; }
    if (healthBox) { healthBox.hidden = true; }
    $$("[data-cam-fps]").forEach(function (el) { el.textContent = ""; });

    /* 每路只允许一个请求，慢链路不积压旧帧。 */
    if (typeof bridge.frame === "function") {
      var generation = ++previewGeneration;
      CAM_KEYS.forEach(function (cam) {
        var pull = function () {
          if (!running || generation !== previewGeneration) { return; }
          if (document.hidden) { setTimeout(pull, 250); return; }
          var controller = window.AbortController ? new AbortController() : null;
          previewControllers[cam] = controller;
          var deadline = setTimeout(function () { if (controller) { controller.abort(); } }, 2500);
          Promise.resolve().then(function () { return bridge.frame(cfg, cam); }).then(function (src) {
            if (!src || !running || generation !== previewGeneration) { return null; }
            return fetch(src, { cache: "no-store", credentials: "omit", signal: controller && controller.signal })
              .then(function (r) { if (!r.ok) { throw new Error("HTTP " + r.status); } return r.blob(); })
              .then(function (blob) {
                if (!running || generation !== previewGeneration) { return; }
                var url = URL.createObjectURL(blob);
                if (previewUrls[cam]) { URL.revokeObjectURL(previewUrls[cam]); }
                previewUrls[cam] = url;
                setCamFrame(cam, url);
              });
          }).catch(function (e) {
            if (!(e && e.name === "AbortError") && running && generation === previewGeneration) {
              setCamFrame(cam, null);
            }
          }).then(function () {
            clearTimeout(deadline);
            if (previewControllers[cam] === controller) { delete previewControllers[cam]; }
            if (running && generation === previewGeneration) {
              setTimeout(pull, PREVIEW_INTERVAL_MS);
            }
          });
        };
        pull();
      });
      startCamWatchdog(cfg);
      return;
    }

    if (typeof bridge.streamUrl === "function") {
      CAM_KEYS.forEach(function (cam) { setCamFrame(cam, bridge.streamUrl(cfg, cam)); });
      startCamWatchdog(cfg);
    }

  }

  function stopFramePreview() {
    previewGeneration++;
    clearRemoteRenderers();
    Object.keys(previewControllers).forEach(function (cam) {
      try { previewControllers[cam].abort(); } catch (e) {}
    });
    previewControllers = {};
    Object.keys(remoteReconnectTimers).forEach(function (cam) {
      clearTimeout(remoteReconnectTimers[cam]);
      delete remoteReconnectTimers[cam];
    });
    if (remoteWatchTimer) { clearInterval(remoteWatchTimer); remoteWatchTimer = null; }
    if (remoteAdaptTimer) { clearInterval(remoteAdaptTimer); remoteAdaptTimer = null; }
    if (controlReconnectTimer) { clearTimeout(controlReconnectTimer); controlReconnectTimer = null; }
    closeControlPeer();
    Object.keys(previewUrls).forEach(function (cam) {
      try { URL.revokeObjectURL(previewUrls[cam]); } catch (e) {}
    });
    previewUrls = {};
    if (frameTimer) { clearInterval(frameTimer); frameTimer = null; }
    if (camHealthTimer) { clearInterval(camHealthTimer); camHealthTimer = null; }
    if (captureMode === "control" && controlMetricTimer) {
      clearInterval(controlMetricTimer);
      controlMetricTimer = null;
      controlHistory = [];
    }
    var stage = $("#cap-stage");
    var healthBox = $("#cap-preview-health");
    if (stage) { stage.classList.remove("is-control"); }
    var latencyPanel = $("#cap-latency");
    if (latencyPanel) { latencyPanel.hidden = true; }
    if (healthBox) { healthBox.hidden = true; }
    $$("[data-cam-fps]").forEach(function (el) { el.textContent = ""; });
    CAM_KEYS.forEach(function (cam) { setCamFrame(cam, null); });
  }

  var recListEl = $("#cap-rec-list");
  var recEmptyEl = $("#cap-rec-empty");

  function humanSize(n) {
    if (!n) { return "0 B"; }
    var u = ["B", "KB", "MB", "GB"], i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)) + " " + u[i];
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function fmtClock(ts) {
    var d = new Date(ts * 1000);
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  var capDir = null;       
  var devProbed = false;   
  var recordsBusy = false;
  var foldersBusy = false;
  var foldersReloadPending = false;
  var lastRecordsSignature = null;

  function remoteMode() {
    return !!(hasCurrent() && curAgent && curAgent.data_root);
  }

  function loadFolders() {
    if (!window.dmApi) { return; }
    if (foldersBusy) {
      foldersReloadPending = true;
      return;
    }
    if (!curAgent && !devProbed) {
       
      devProbed = true;
      refreshDevices().then(function () { loadFolders(); });
      return;
    }
    capDir = null;
    setFolderMode(true);
    if (!remoteMode()) {
      renderFolders([], "");
      return;
    }
    var base = recordingBase();

    foldersBusy = true;
    window.dmApi.dirs("", true).then(function (r) {
      if (capDir !== null || activeSession) { return; }
      var subs = (r.dirs || []).filter(function (s) {
        return (s.episodes || 0) > 0 || (s.records || 0) > 0;
      });
      renderFolders(subs, base);
    }).catch(function () {
    }).then(function () {
      foldersBusy = false;
      if (foldersReloadPending) {
        foldersReloadPending = false;
        loadFolders();
      }
    });
  }

  function recordingBase() {
    return remoteMode() ? curAgent.data_root : "";
  }

  function setFolderMode(isFolderView) {
    var title = $("#cap-recs-title");
    var back = $("#cap-dir-back");
    var listF = $("#cap-folder-list");
    var listR = $("#cap-rec-list");
    if (title) {
       
      title.textContent = isFolderView
        ? "录制文件夹"
        : (capDir ? capDir.replace(/\/+$/, "").split("/").pop() : "录制文件");
    }
    if (back) { back.hidden = isFolderView; }
    if (listF) { listF.hidden = !isFolderView; }
    if (listR) { listR.hidden = isFolderView; }
    var sum = $("#cap-recsum");
    if (sum) { sum.textContent = ""; }
     
    syncUploadBtn();
  }

  function renderFolders(folders, base) {
    var listF = $("#cap-folder-list");
    if (!listF) { return; }
    var signature = JSON.stringify((folders || []).map(function (f) {
      return [f.name, f.path, f.records, f.episodes];
    }));
    if (signature === lastFoldersSignature) {
      paintUpStates();
      syncUploadBtn();
      return;
    }
    lastFoldersSignature = signature;
    listF.innerHTML = "";
    lastFolders = (folders || []).slice();    

    var recCard = $("#cap-records-card");
    var idleEmpty = $("#cap-empty");
    var recEmpty = $("#cap-rec-empty");

    if (!folders.length) {
       
      var et = $("#cap-empty-text");
      if (et) {
        et.textContent = remoteMode()
          ? "设备暂无文件"
          : "还没有连接设备";
      }
      if (recCard) { recCard.hidden = true; }
      if (idleEmpty) { idleEmpty.hidden = false; }
      if (recEmpty) { recEmpty.hidden = true; }
       
      upPicking = false;
      syncUploadBtn();
      return;
    }

    if (recCard) { recCard.hidden = false; }
    if (idleEmpty) { idleEmpty.hidden = true; }
    if (recEmpty) { recEmpty.hidden = true; }

    folders.forEach(function (f, i) {
      var li = document.createElement("li");
      li.className = "cap-folder";
      li.style.setProperty("--i", i);
      li.dataset.folder = f.name || f.path;

      var pick = document.createElement("input");
      pick.type = "checkbox";
      pick.className = "cap-folder__pick";
      pick.value = f.path;
      pick.tabIndex = -1;
      pick.setAttribute("aria-label", "选择文件夹 " + (f.name || f.path));
      pick.addEventListener("click", function (ev) { ev.stopPropagation(); });
      pick.addEventListener("change", function () {
        li.classList.toggle("is-picked", pick.checked);
        syncPickUI();
      });

      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "cap-folder__btn";
      btn.title = f.path;

      var name = document.createElement("span");
      name.className = "cap-folder__name";
      name.textContent = f.name || f.path;

      var tag = document.createElement("span");
      tag.className = "cap-folder__tag";
      tag.hidden = true;

      var arrow = document.createElement("span");
      arrow.className = "cap-folder__arrow";
      arrow.textContent = "›";

      btn.appendChild(name);
      btn.appendChild(tag);
      btn.appendChild(arrow);
      btn.addEventListener("click", function () {
         
        if (upPicking) {
          pick.checked = !pick.checked;
          li.classList.toggle("is-picked", pick.checked);
          syncPickUI();
          return;
        }
        enterDir(f.path);
      });

      var del = document.createElement("button");
      del.type = "button";
      del.className = "cap-folder__del";
      del.title = "删除该文件夹";
      del.setAttribute("aria-label", "删除文件夹 " + (f.name || f.path));
      del.innerHTML = '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">'
        + '<path d="M4.5 4.5l7 7M11.5 4.5l-7 7" stroke="currentColor" stroke-width="1.6" '
        + 'stroke-linecap="round" fill="none"/></svg>';
      del.addEventListener("click", function (ev) {
        ev.stopPropagation();
        confirmDeleteFolder(f, del);
      });

      li.appendChild(pick);
      li.appendChild(btn);
      li.appendChild(del);
      listF.appendChild(li);
    });

    applyPickMode();
    paintUpStates();
    syncUploadBtn();
  }

  function confirmDeleteFolder(folder, anchor) {
    var name = folder.name || folder.path;
    var n = folder.records || folder.episodes || 0;
    var html = '<p class="modal__line">将删除文件夹 <b>' + escapeHtml(name) + '</b> 及其中的 '
      + (n ? (n + " 条录制数据") : "全部数据") + '。</p>'
      + '<p class="modal__line">仅删除采集端文件，服务端已上传的数据和审核记录保留。</p>'
      + '<p class="modal__line modal__line--warn">删除后无法恢复，请确认。</p>';
    return openModal({
      title: "删除文件夹",
      html: html,
      ok: "删除",
      cancel: "取消",
      danger: true,
      anchor: anchor
    }).then(function (yes) {
      if (!yes) { return; }
      window.dmApi.deleteDir(folder.path, true).then(function (r) {
        var bytes = r && r.bytes ? "（" + humanSize(r.bytes) + "）" : "";
        toast("已删除：" + name + bytes);
         
        if (capDir && capDir === folder.path) { capDir = null; }
        loadFolders();
      }).catch(function (e) {
        toast("删除失败：" + (e && e.message ? e.message : e));
      });
    });
  }

  var upJob = null;         
  var upTimer = null;       
  var lastFolders = [];     
  var lastFoldersSignature = null;
  var upPicking = false;    

  function upRunning() { return !!(upJob && upJob.running); }

  function upItemOf(name) {
    if (!upJob || !upJob.dirs) { return null; }
    for (var i = 0; i < upJob.dirs.length; i++) {
      if (upJob.dirs[i].dir === name) { return upJob.dirs[i]; }
    }
    return null;
  }

  function fetchUploadState() {
    if (!authed() || !window.dmApi.uploads) { return Promise.resolve(null); }
    return window.dmApi.uploads().then(function (s) {
      upJob = s || null;
      return upJob;
    }).catch(function () { return upJob; });
  }

  function paintUpStates() {
    var rows = document.querySelectorAll("#cap-folder-list .cap-folder");
    var doneMap = (upJob && upJob.done) || {};
    Array.prototype.forEach.call(rows, function (li) {
      var item = upItemOf(li.dataset.folder);
      var rec = doneMap[li.dataset.folder];
      var tag = li.querySelector(".cap-folder__tag");
      li.classList.remove("is-up", "is-up-done", "is-up-err");
      if (!tag) { return; }
      tag.title = "";
      if (!item || !item.state) {
        if (rec) {
          tag.hidden = false;
          tag.textContent = "已上传";
          tag.title = rec.at ? ("上次上传：" + rec.at) : "";
          li.classList.add("is-up-done");
          return;
        }
        tag.hidden = true;
        tag.textContent = "";
        return;
      }
      tag.hidden = false;
      if (item.state === "running") {
        var pct = item.total > 0
          ? Math.min(99, Math.round((item.recv || 0) * 100 / item.total)) : 0;
        tag.textContent = item.total > 0 ? ("上传中 " + pct + "%") : "上传中…";
        li.classList.add("is-up");
      } else if (item.state === "compressing") {
        tag.textContent = "采集端压缩中";
        li.classList.add("is-up");
      } else if (item.state === "wait") {
        tag.textContent = "等待上传";
        li.classList.add("is-up");
      } else if (item.state === "done") {
        tag.textContent = "已上传";
        tag.title = rec && rec.at ? ("上次上传：" + rec.at) : "";
        li.classList.add("is-up-done");
      } else if (item.state === "error") {
        tag.textContent = "上传失败";
        li.classList.add("is-up-err");
      } else {
        tag.hidden = true;
        tag.textContent = "";
      }
    });
    syncUploadBtn();
  }

  function syncUploadBtn() {
    var btn = $("#cap-upload-btn");
    var sel = $("#cap-upsel");
    var can = remoteMode() && !capDir && !running && lastFolders.length > 0;
    if (btn) {
      btn.hidden = !can || upPicking;
      btn.title = upRunning() ? "正在上传…" : "勾选录制文件夹上传到服务器";
      btn.classList.toggle("is-busy", upRunning());
    }
    if (sel) { sel.hidden = !(can && upPicking); }
    applyPickMode();
  }

  function applyPickMode() {
    var list = $("#cap-folder-list");
    if (list) { list.classList.toggle("is-picking", upPicking); }
    Array.prototype.forEach.call(
      document.querySelectorAll("#cap-folder-list .cap-folder"), function (li) {
        var pick = li.querySelector(".cap-folder__pick");
        if (!pick) { return; }
        pick.hidden = !upPicking;
        pick.tabIndex = upPicking ? 0 : -1;
        if (!upPicking) { pick.checked = false; }
        li.classList.toggle("is-picked", !!(upPicking && pick.checked));
      });
    syncPickUI();
  }

  function syncPickUI() {
    var n = document.querySelectorAll("#cap-folder-list .cap-folder__pick:checked").length;
    var cnt = $("#cap-up-count");
    if (cnt) { cnt.textContent = "已选 " + n + " 个"; }
    var go = $("#cap-up-go");
    if (go) { go.disabled = !n; }
    return n;
  }

  function enterPick() {
    upPicking = true;
    applyPickMode();
    syncUploadBtn();
  }

  function exitPick() {
    upPicking = false;
    applyPickMode();
    syncUploadBtn();
  }

  function upPicked() {
    var picked = [];
    Array.prototype.forEach.call(
      document.querySelectorAll("#cap-folder-list .cap-folder__pick"), function (cb) {
        if (cb.checked) { picked.push(cb.value); }
      });
    return picked;
  }

  function stopUpPoll() {
    if (upTimer) { clearInterval(upTimer); upTimer = null; }
  }

  function startUpPoll() {
    if (upTimer) { return; }
    paintUpStates();
    upTimer = setInterval(function () {
      fetchUploadState().then(function (s) {
        paintUpStates();
        if (s && s.running) { return; }
        stopUpPoll();
        var okN = 0, badN = 0;
        ((s && s.dirs) || []).forEach(function (d) {
          if (d.state === "done") { okN += 1; }
          else if (d.state === "error") { badN += 1; }
        });
        if (okN || badN) {
          toast(badN
            ? ("上传结束：成功 " + okN + " 个，失败 " + badN + " 个")
            : ("已上传 " + okN + " 个文件夹到服务器，可在「数据审核」里查看"));
        }
        loadFolders();
      });
    }, 1200);
  }

  function modalBusyUploading() {
    openModal({
      title: "正在上传",
      html: '<p class="modal__line">有文件夹正在上传到服务器，请稍等。</p>',
      ok: "知道了",
      noCancel: true
    });
  }

  function togglePick() {
    if (upPicking) { exitPick(); return; }
    if (!remoteMode()) { toast("还没有连接设备"); return; }
    if (!lastFolders.length) { toast("这台设备还没有录制文件夹"); return; }
    fetchUploadState().then(function (s) {
      paintUpStates();
      if (s && s.running) { startUpPoll(); modalBusyUploading(); return; }
      enterPick();
    });
  }

  function doUpload(dirs) {
    window.dmApi.startUpload(dirs).then(function (s) {
      upJob = s || null;
      exitPick();
      toast("开始上传 " + dirs.length + " 个文件夹到服务器");
      startUpPoll();
    }).catch(function (e) {
      if (e && e.code === 409) { modalBusyUploading(); return; }
      toast("上传失败：" + ((e && e.message) || e));
    });
  }

  (function bindUploadUI() {
    var b = $("#cap-upload-btn");
    if (b) { b.addEventListener("click", togglePick); }
    var g = $("#cap-up-go");
    if (g) {
      g.addEventListener("click", function () {
        if (upRunning()) { modalBusyUploading(); return; }
        var picked = upPicked();
        if (!picked.length) { toast("还没勾选文件夹"); return; }
        doUpload(picked);
      });
    }
    var c = $("#cap-up-cancel");
    if (c) { c.addEventListener("click", exitPick); }
  })();

  fetchUploadState().then(function (s) {
    paintUpStates();
    if (s && s.running) { startUpPoll(); }
  });

  function enterDir(dir) {
     
    if (!remoteMode()) { capDir = null; loadFolders(); return; }
    if (upPicking) { exitPick(); }
    capDir = dir;
    setFolderMode(false);
    refreshRecords();
  }

  function refreshRecords() {
    if (!recListEl || !window.dmApi) { return; }

    if (!remoteMode() && !activeSession) {
      capDir = null;
      loadFolders();
      return;
    }
     
    var dir = activeSession && activeSession.dirPath || capDir;
    if (dir) {
      if (recordsBusy) { return; }
      recordsBusy = true;
      if (activeSession) { capDir = dir; }
      setFolderMode(false);
      window.dmApi.records(dir, true, true).then(function (d) {
        if ((activeSession && activeSession.dirPath || capDir) !== dir) { return; }
        renderRecords((d.records || []).map(function (r) {
          return { name: r.name, size: r.size, mtime: r.mtime,
                   mtimeNs: r.mtime_ns, dir: dir };
        }));
      }).catch(function () {
        // Keep the last successful listing visible during a temporary disconnect.
      }).then(function () { recordsBusy = false; });
      return;
    }
    if (capDir) { enterDir(capDir); return; }
    loadFolders();
  }

  setInterval(function () {
    var view = $("#view-capture");
    if (view && !view.hidden) { refreshRecords(); }
  }, RECORDS_REFRESH_MS);

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) { refreshRecords(); }
  });

  function syncEmptyState(fileCount) {
    var isFolderView = capDir === null && !activeSession;
    var recCard = $("#cap-records-card");
    var idleEmpty = $("#cap-empty");           
    var recEmpty = $("#cap-rec-empty");        

    if (recCard) { recCard.hidden = false; }
    if (idleEmpty) { idleEmpty.hidden = true; }

    if (isFolderView) {
       
      if (recEmpty) { recEmpty.hidden = true; }
      return;
    }
    if (recEmpty) { recEmpty.hidden = fileCount > 0; }
  }

  function renderRecords(records) {
    var signature = JSON.stringify(records.map(function (r) {
      return [r.name, r.size, r.mtimeNs || r.mtime, r.dir];
    }));
    if (signature === lastRecordsSignature) { return; }
    lastRecordsSignature = signature;
    recListEl.innerHTML = "";
    records.forEach(function (rec, i) {
      var li = document.createElement("li");
      li.className = "cap-rec";
      li.dataset.name = rec.name;
       
      li.style.setProperty("--i", i);

      var name = document.createElement("span");
      name.className = "cap-rec__name";
      name.textContent = rec.name;
      name.title = rec.dir ? (rec.dir + "/" + rec.name) : rec.name;

      var size = document.createElement("span");
      size.className = "cap-rec__size";
      size.textContent = humanSize(rec.size);

      var dur = document.createElement("span");
      dur.className = "cap-rec__dur";
      dur.textContent = rec.mtime ? fmtClock(rec.mtime) : "—";

      var del = document.createElement("button");
      del.type = "button";
      del.className = "cap-rec__del";
      del.title = "删除该录制";
      del.textContent = "✕";
      del.addEventListener("click", function () { removeRecord(rec.name, rec.dir); });

      li.appendChild(name);
      li.appendChild(size);
      li.appendChild(dur);
      li.appendChild(del);
      recListEl.appendChild(li);
    });
    syncEmptyState(records.length);

    var sum = $("#cap-recsum");
    if (sum) { sum.textContent = ""; }
  }

  function removeRecord(name, fromDir) {
    var dir = fromDir || capDir || activeSession && activeSession.dirPath || dataDir();
    if (!dir || !name || !window.dmApi) { return; }
     
    window.dmApi.deleteRecord(dir, name, true).then(function () {
      toast("已删除：" + name);
      refreshRecords();
    }).catch(function (e) { toast("删除失败：" + (e.message || e)); });
  }

  var capBackBtn = $("#cap-dir-back");
  if (capBackBtn) {
    capBackBtn.addEventListener("click", function () {
      capDir = null;
      loadFolders();
    });
  }

  var timerId = null;
  var timerStart = 0;

  var statTodayBase = 0;

  function startTimer() {
    timerStart = Date.now();
     
    var el = $("#stat-capture-today");
    statTodayBase = el ? parseDur(el.textContent) : 0;
    stopTimer();
    timerId = setInterval(function () {
      var elapsed = (Date.now() - timerStart) / 1000;
      var clock = $("#cap-clock");
      if (clock) { clock.textContent = fmtDur(elapsed); }
       
      var elToday = $("#stat-capture-today");
      if (elToday) { elToday.textContent = fmtDur(statTodayBase + elapsed); }
    }, 1000);
  }

  function startTimerAt(startedAt) {
    var parsed = startedAt ? Date.parse(String(startedAt).replace(" ", "T")) : NaN;
    timerStart = isFinite(parsed) && parsed <= Date.now() ? parsed : Date.now();
    var el = $("#stat-capture-today");
    statTodayBase = el ? parseDur(el.textContent) : 0;
    stopTimer();
    timerId = setInterval(function () {
      var elapsed = (Date.now() - timerStart) / 1000;
      var clock = $("#cap-clock");
      if (clock) { clock.textContent = fmtDur(elapsed); }
      var elToday = $("#stat-capture-today");
      if (elToday) { elToday.textContent = fmtDur(statTodayBase + elapsed); }
    }, 1000);
  }

  function parseDur(txt) {
    if (!txt) { return 0; }
    var parts = String(txt).split(":").map(Number);
    if (parts.some(isNaN)) { return 0; }
    if (parts.length === 3) { return parts[0] * 3600 + parts[1] * 60 + parts[2]; }
    if (parts.length === 2) { return parts[0] * 60 + parts[1]; }
    return parts[0] || 0;
  }

  function stopTimer() {
    if (timerId) { clearInterval(timerId); timerId = null; }
  }

  function fail(title, detail) {
    running = false;
    stopTimer();
    stopFramePreview();
    setPhase("error", "失败");
    logLine("!! " + title, "err");
    if (detail) { logLine(detail, "err"); }
    var retry = { folder: sessionFolder, cfg: cfgForRetry };
    var failedSession = activeSession;
    var cleanup = failedSession && window.dmApi
      ? window.dmApi.endSession(failedSession.id, timerStart ? Math.round((Date.now() - timerStart) / 1000) : 0,
          "error", title + (detail ? ": " + detail : ""))
      : Promise.resolve();
    cleanup.catch(function () {});

    var detailText = String(detail || "").trim().slice(0, 1600);
    openModal({
      title: "采集失败",
      html: "<p>" + esc(title) + "</p>" +
            (detailText ? "<pre class=\"fail__detail\">" + esc(detailText) + "</pre>" : "") +
            "<p class=\"fail__hint\">可以重试，或退出后检查主机配置。</p>",
      ok: "重试", cancel: "退出", danger: true
    }).then(function (yes) {
      return cleanup.then(function () {
        activeSession = null;
        hideLive();
        timerStart = 0;
        loadFolders();
        if (!yes) { return; }

        if (retry.folder) {
          setPhase("idle", "准备中");
          startCaptureSession(retry.folder, retry.cfg || loadCfg() || CFG_DEFAULT);
        }
      });
    }).catch(function (e) {
      toast("失败会话清理未完成，请重试结束会话：" + (e.message || e));
    });
  }

  function runSession(session, cfg) {
    if (controlOnlyActive) { toast("仅控制模式已关闭采集功能"); return; }
    if (running) { toast("当前正在采集"); return; }
    if (!cfg) {
      cfg = loadCfg();
      if (!cfg || !cfg.k7_dir) {
        toast("没有可用的程序配置 —— 先在「设备状态」连接机器狗");
        return;
      }
    }
    if (!session) { return; }

    running = true;
    activeSession = session;
    sessionFolder = session.folder;
    cfgForRetry = cfg;
    timerStart = 0;
    if ($("#cap-clock")) { $("#cap-clock").textContent = "00:00"; }

    showLive(session);
    var cameraReady = Promise.resolve(br().camStart(cfg, logLine)).then(function (cam) {
      if (cam && !cam.ok) { logLine("相机预览重启失败：" + (cam.error || "未知错误"), "err"); }
      if (running) { fetchFramePreview(cfg); }
    }).catch(function (e) { logLine("相机预览重启失败：" + (e.message || e), "err"); });
    setPhase("busy", "等待标零确认");
    return cameraReady.then(function () {
      if (!running) { throw new Error("__abort__"); }
      return openModal({
      title: "是否执行手臂标零？",
      ok: "执行标零", cancel: "跳过"
      });
    }).then(function (yes) {
        if (!running) { return; }
        if (!yes) {
          logLine("用户选择：跳过标零");
          return null;
        }
        setPhase("busy", "标零中");
        logLine("用户确认：执行标零");
        return br().tare(cfg, logLine).then(function (tr) {
          if (!tr || !tr.ok) {
            fail("标零失败", (tr && tr.error) || "");
            throw new Error("__abort__");
          }
          logLine("标零完成", "ok");
        });
    }).then(function () {
      if (!running) { return; }

      logLine("启动或检查 " + cfg.k7_bin);
      setPhase("busy", "等待返回值");
      return br().t1(cfg, logLine).then(function (r) {
        if (!running) { throw new Error("__abort__"); }
        if (!r || !r.ok) {
          fail("机械臂未就绪", (r && r.error) || "K7Master3 未达到就绪条件");
          throw new Error("__abort__");
        }
        logLine("等待返回值", "ok");

        setPhase("busy", "启动手柄服务与采集程序");
        logLine("启动手柄服务和 " + cfg.rl_deploy_bin);
        return br().t2(cfg, logLine).then(function (r2) {
          if (!running) { throw new Error("__abort__"); }
          if (!r2 || !r2.ok) {
            fail("采集程序启动失败", (r2 && r2.error) || "退出码非 0");
            throw new Error("__abort__");
          }
        });
      }).then(function () {
        if (!running) { return; }

        setPhase("busy", "发送启动指令");
        logLine("自动注入 z");
        return br().z(cfg, logLine).then(function (rz) {
          if (rz && rz.ok === false) {
            fail("机械臂启动指令失败", rz.error || "");
            throw new Error("__abort__");
          }
          setPhase("run", "采集中");
          startTimer();
          refreshRecords();
        });
      });
    }).catch(function (e) {
      if (e && e.message === "__abort__") { return; }
      if (e) { fail("采集流程异常", e.message || String(e)); }
    });
  }

  function resumeSession(session, cfg) {
    if (!session || running) { return; }
    running = true;
    activeSession = session;
    sessionFolder = session.folder;
    cfgForRetry = cfg;
    showLive(session);
    Promise.resolve(br().camStart(cfg, logLine)).then(function (cam) {
      if (cam && !cam.ok) { logLine("相机预览重启失败：" + (cam.error || "未知错误"), "err"); }
      if (running) { fetchFramePreview(cfg); }
    }).catch(function (e) { logLine("相机预览重启失败：" + (e.message || e), "err"); });
    setPhase("run", "采集中");
    startTimerAt(session.startedAt);
    refreshProcStatus();
  }

  var resumeAttempted = false;
  function resumeRunningSession() {
    if (resumeAttempted || !window.dmApi || !window.dmApi.runningSession) { return; }
    resumeAttempted = true;
    window.dmApi.runningSession().then(function (r) {
      if (!r || !r.running || r.owned_by_other || !r.session) { return; }
      return window.dmApi.runStatus().then(function (status) {
        if (status && status.rl_deploy && status.rl_deploy.running && !status.rl_deploy.external) {
          return syncCfgFromServer().then(function (cfg) {
            if (!cfg || !cfg._agent) { return; }
            if (r.session.collector_path) { cfg.run_data_dir = r.session.collector_path; }
            var session = r.session;
            resumeSession({ id: session.id, folder: session.folder,
              dirPath: session.dir_path, collectorPath: session.collector_path,
              absPath: session.abs_path, startedAt: session.started_at }, cfg);
          });
        }
      });
    }).catch(function () {});
  }

  function stopSession(s, secs, state, error) {
    if (!s || !s.id) { return Promise.resolve(null); }
    if (!window.dmApi) {
      addDuration(s.id, secs);
      return Promise.resolve(null);
    }
    return window.dmApi.endSession(s.id, Math.round(secs), state || "done", error || null)
      .then(function (d) {
        setPhase("done", "已完成");
        logLine("会话已记录，" + fmtDur(d.duration_s) + "："
                + (s.collectorPath || s.dirPath), "ok");
        return d;
      })
      .catch(function (e) {
        logLine("会话记录失败：" + (e.message || e), "err");
        throw e;
      });
  }

  var exiting = false;

  var btnExit = $("#cap-exit");
  if (btnExit) {
    btnExit.addEventListener("click", function () {
      if (exiting) { return; }
      exiting = true;

      var s = activeSession;
      var wasControlOnly = controlOnlyActive;
      var secs = timerStart ? (Date.now() - timerStart) / 1000 : 0;

      running = false;
      if (wasControlOnly) { controlStartGeneration++; }
      stopTimer();
      stopFramePreview();

      setPhase("busy", controlOnlyActive ? "正在退出控制" : "正在退出采集");
      btnExit.disabled = true;
      showVeil(controlOnlyActive ? "正在发送退出指令" : "正在发送停止指令");

      var cfg = loadCfg();
      var collectorStopped = false;
      var stopPromise = cfg
        ? Promise.resolve(controlOnlyActive
            ? br().controlStop(cfg, logLine)
            : br().captureStop(cfg, logLine))
        : Promise.resolve({ ok: false, error: "设备程序配置不可用，未发送停止指令" });

      stopPromise.then(function (result) {
        if (!result || !result.ok) {
          throw new Error((result && result.error) || "停止采集失败");
        }
        collectorStopped = true;
         
        return wasControlOnly ? null : stopSession(s, secs);
      }).then(function (d) {
        activeSession = null;
        sessionFolder = null;
        cfgForRetry = null;
        controlOnlyActive = false;
        controlDeployStarted = false;
        timerStart = 0;
        renderCaptureMode();
        if ($("#cap-clock")) { $("#cap-clock").textContent = "00:00"; }
        capDir = null;
        var recordCard = $("#cap-records-card");
        if (recordCard) { recordCard.hidden = true; }
        var liveTitle = $("#t-capture-live");
        if (liveTitle) { liveTitle.textContent = "采集"; }
        var liveDir = $("#cap-dir");
        if (liveDir) { liveDir.hidden = false; }
        var liveClock = $("#cap-clock");
        if (liveClock) { liveClock.hidden = false; }
        var exitButton = $("#cap-exit");
        if (exitButton) { exitButton.textContent = "退出采集"; }
        lastRecordsSignature = null;
        lastFoldersSignature = null;
        lastFolders = [];
        hideVeil();
        hideLive();
        updateStats();
        loadFolders();
        if (wasControlOnly) {
          toast("已退出远程控制");
        } else if (d && d.archive && d.archive.state === "compressing") {
          toast("已退出采集，采集端正在压缩数据");
        } else if (d && d.archive && d.archive.state === "error") {
          var archiveError = d.archive.error || "请检查设备连接";
          toast("已退出采集，但采集端压缩未启动：" + archiveError.slice(0, 100));
        } else {
          toast("已退出采集，时长已计入");
        }
        exiting = false;
        btnExit.disabled = false;
      }).catch(function (e) {
        if (collectorStopped) {
          running = false;
          activeSession = null;
          sessionFolder = null;
          timerStart = 0;
          renderCaptureMode();
          var recordCard = $("#cap-records-card");
          if (recordCard) { recordCard.hidden = true; }
          lastFoldersSignature = null;
          lastFolders = [];
          hideLive();
          loadFolders();
        } else {
          running = true;
          if (s) { activeSession = s; startTimerAt(s.startedAt); fetchFramePreview(loadCfg() || CFG_DEFAULT); }
          setPhase("run", controlOnlyActive ? "仅控制中" : "采集中");
        }
        hideVeil();
        exiting = false;
        btnExit.disabled = false;
        toast(collectorStopped
          ? "采集已停止，但会话记录失败；再次开始时会自动清理遗留会话"
          : (controlOnlyActive ? "退出控制失败，机械臂程序仍保持运行：" : "结束采集失败，机械臂程序仍保持运行：") + (e.message || e));
      });
    });
  }

  var veilEl = $("#cap-veil");
  var veilSubEl = $("#cap-veil-sub");

  function showVeil(text) {
    if (veilSubEl && text) { veilSubEl.textContent = text; }
    if (veilEl) { veilEl.hidden = false; }
  }

  function hideVeil() {
    if (veilEl) { veilEl.hidden = true; }
  }

  var ADV_GROUPS = [
    {
      title: "程序配置",
      rows: [
        ["dds_ip", "DDS 主机 IP"],
        ["dds_username", "DDS 用户名"],
        ["dds_password", "DDS 密码"],
        ["k7_dir", "K7 工作目录"],
        ["k7_lib_path", "K7 库目录"],
        ["k7_bin", "K7 主程序"]
      ]
    },
    {
      title: "采集参数",
      rows: [
        ["record_fps", "录制帧率"],
        ["imu_topic", "IMU 名称"]
      ]
    }
  ];

  var ADV_HINT = {
    k7_lib_path: "LD_LIBRARY_PATH",
    record_fps: "FPS",
    imu_topic: "ROS 话题"
  };

  var CFG_NOTE_IDLE = "程序配置保存在当前采集端。";
  var cfgNoteEl = $("#cfg-note");
  var cfgNoteTimer = null;

  function cfgNote(text, kind) {
    if (!cfgNoteEl) { return; }
    if (cfgNoteTimer) { clearTimeout(cfgNoteTimer); cfgNoteTimer = null; }
    cfgNoteEl.textContent = text;
    cfgNoteEl.className = "cfg-note" + (kind ? " is-" + kind : "");
    if (kind) {
      cfgNoteTimer = setTimeout(function () { cfgNote(CFG_NOTE_IDLE, ""); }, 4000);
    }
  }

  function cfgRow(key, label, hint, value) {
    var wrap = document.createElement("div");
    wrap.className = "adv-row";

    var k = document.createElement("dt");
    k.className = "adv-row__k";
    k.textContent = label;
    if (hint) {
      var h = document.createElement("span");
      h.className = "adv-row__hint";
      h.textContent = hint;
      k.appendChild(h);
    }

    var v = document.createElement("dd");
    v.className = "adv-row__v";
    var inp = document.createElement("input");
    inp.type = key === "dds_password" ? "password" : "text";
    inp.className = "cfg-input";
    inp.readOnly = !/^(dds_(ip|username|password)|k7_(dir|lib_path|bin)|record_fps|imu_topic)$/.test(key);
    inp.spellcheck = false;
    inp.autocomplete = "off";
    inp.dataset.key = key;
    inp.value = (value == null) ? "" : String(value);
    if (key === "dds_password" && cfgCache && cfgCache.dds_password_set) {
      inp.placeholder = "已配置，留空则不修改";
    }
    inp.setAttribute("aria-label", label);
     
    if (!inp.readOnly) {
      inp.addEventListener("change", function () { saveDeviceConfig(); });
    }

    v.appendChild(inp);

    wrap.appendChild(k);
    wrap.appendChild(v);
    return wrap;
  }


  function saveDeviceConfig() {
    var cfg = Object.assign({}, loadCfg() || {});
    ["dds_ip", "dds_username", "k7_dir", "k7_lib_path", "k7_bin", "record_fps", "imu_topic"].forEach(function (key) {
      var input = $("#dev-cfg [data-key=\"" + key + "\"]");
      if (input) { cfg[key] = input.value.trim(); }
    });
    var passwordInput = $("#dev-cfg [data-key=\"dds_password\"]");
    if (passwordInput && passwordInput.value.trim()) {
      cfg.dds_password = passwordInput.value.trim();
    } else {
      delete cfg.dds_password;
    }
    cfgCache = cfg;
    cfgNote("正在保存程序配置…", "busy");
    window.dmApi.saveConfig(cfg).then(function () {
      cfgCache = cfg;
      cfgNote("程序配置已保存。", "ok");
    }).catch(function (error) {
      cfgNote("保存失败：" + ((error && error.message) || "接口异常"), "err");
    });
  }

  function renderDevCfg() {
    var wrap = $("#dev-cfg");
    if (!wrap) { return; }
    var cfg = loadCfg() || {};
    var ag = cfg._agent || null;
    if (!ag || !ag.url) {
      wrap.hidden = true;
      wrap.innerHTML = "";
      if (cfgNoteEl) { cfgNoteEl.hidden = true; }
      return;
    }
    if (cfgNoteEl) { cfgNoteEl.hidden = false; }
    if (ag.reachable === false) {
      wrap.hidden = true;
      wrap.innerHTML = "";
      cfgNote("采集端不可达，无法读取配置。", "warn");
      return;
    }

    wrap.hidden = false;
    wrap.innerHTML = "";
    ADV_GROUPS.forEach(function (g) {
      var card = document.createElement("article");
      card.className = "card adv-card";

      var head = document.createElement("div");
      head.className = "adv-card__head";
      var h = document.createElement("h2");
      h.textContent = g.title;
      var d = document.createElement("p");
      d.className = "adv-card__desc";
      d.textContent = g.desc || "";
      head.appendChild(h);
      head.appendChild(d);
      card.appendChild(head);

      var list = document.createElement("dl");
      list.className = "adv-list";
      g.rows.forEach(function (pair) {
        list.appendChild(cfgRow(pair[0], pair[1], ADV_HINT[pair[0]], cfg[pair[0]]));
      });
      card.appendChild(list);
      wrap.appendChild(card);
    });
    cfgNote(ag.reachable === false ? "采集端不可达，无法读取配置。" : CFG_NOTE_IDLE, "");
  }

  var devCardEl = $("#dev-card");
  var devListEl = $("#dev-list");
  var devEmptyEl = $("#dev-empty");
  var devEmptyTextEl = $("#dev-empty-text");
  var devMetaEl = $("#dev-meta");
  var devViewEl = $("#view-device");
  var DEV_POLL_MS = 6000;
  var devPollTimer = null;
  var devScanning = false;
  var devBusy = false;
  var devList = [];           
  var curAgent = null;        

  function hasCurrent() {
    return !!(curAgent && curAgent.url);
  }

  function devSplitUrl(u) {
    var m = /^https?:\/\/([^:\/]+)(?::(\d+))?/.exec(u || "");
    if (!m) { return { ip: "", port: "" }; }
    return { ip: m[1], port: m[2] || "" };
  }

  function devSubText(d) {
    if (!d.ip) { return ""; }
    return d.ip + (d.assigned_port ? " : " + d.assigned_port : "");
  }

  function withCurrent(list) {
    var out = (list || []).slice();
    if (!curAgent || !curAgent.url) { return out; }
    var ip = devSplitUrl(curAgent.url).ip;
    for (var i = 0; i < out.length; i++) {
      if (out[i].ip === ip) { return out; }
    }
    out.unshift({
      ip: ip,
      assigned_port: curAgent.port || devSplitUrl(curAgent.url).port,
      device_id: curAgent.device_id,
      host: curAgent.host,
      arch: curAgent.arch,
      disk: curAgent.disk,
      current: true,
      online: curAgent.reachable !== false
    });
    return out;
  }

  function hostCount(list) {
    var m = {};
    (list || []).forEach(function (d) {
      var h = d.host || d.ip || "";
      m[h] = (m[h] || 0) + 1;
    });
    return m;
  }

  function devLabel(d, dup) {
    var name = d.host || d.ip || "未知设备";
    var h = d.host || d.ip || "";
    if (dup && dup[h] > 1 && d.device_id) { name += " " + String(d.device_id).slice(0, 6); }
    return name;
  }

  function devRow(d, dup) {
    var li = document.createElement("li");
    li.className = "dev-item" + (d.current ? " is-cur" : "");
     
    li.tabIndex = 0;
    li.setAttribute("role", "button");
    li.setAttribute("aria-label", "查看 " + devLabel(d, dup) + " 详情");
    li.addEventListener("click", function () { enterDevice(d); });
    li.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); enterDevice(d); }
    });

    var icon = document.createElement("span");
    icon.className = "dev-item__icon";
     
    icon.innerHTML =
      '<img class="dev-item__robot" src="assets/robot/icon.png" alt="" draggable="false">';

    var body = document.createElement("div");
    body.className = "dev-item__body";
    var name = document.createElement("div");
    name.className = "dev-item__name";
    var nm = document.createElement("span");
    nm.textContent = devLabel(d, dup);
    name.appendChild(nm);
    if (d.current) {
      var tag = document.createElement("span");
      tag.className = "dev-item__tag";
      tag.textContent = (d.online === false) ? "当前 离线" : "使用中";
      name.appendChild(tag);
    }
     
    var sub = document.createElement("div");
    sub.className = "dev-item__sub";
    sub.textContent = devSubText(d);
    body.appendChild(name);
    body.appendChild(sub);

    var chev = document.createElement("span");
    chev.className = "dev-item__chev";
    chev.setAttribute("aria-hidden", "true");
    chev.innerHTML =
      '<svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke="currentColor" ' +
      'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M8 5l5 5-5 5"/></svg>';

    li.appendChild(icon);
    li.appendChild(body);
    li.appendChild(chev);
    return li;
  }

  function offlineMode() {
    return !!(window.dmSettings && window.dmSettings.offline && window.dmSettings.offline());
  }

  function authed() {
    return !!(window.dmApi && window.dmApi.hasToken && window.dmApi.hasToken());
  }

  function paintDevices(list, meta) {
    if (!devListEl) { return; }
    var dup = hostCount(list);
    devListEl.textContent = "";
    list.forEach(function (d) { devListEl.appendChild(devRow(d, dup)); });
     
    if (devCardEl) { devCardEl.hidden = list.length === 0; }
    if (devMetaEl) {
       
      devMetaEl.textContent = list.length ? (meta || "") : "";
    }
     
    if (devEmptyTextEl) {
      devEmptyTextEl.textContent = offlineMode() ? "脱机模式：不扫描设备" : "暂无设备";
    }
    if (devEmptyEl) { devEmptyEl.hidden = list.length > 0; }
  }

  function refreshDevices() {
     
    if (offlineMode()) {
      devList = [];
      paintDevices([], "");
      return Promise.resolve(false);
    }
    if (!window.dmApi || !window.dmApi.getConfig) { return Promise.resolve(false); }
    return window.dmApi.getConfig().then(function (r) {
      cfgCache = Object.assign({}, (r && r.config) || {});
      curAgent = cfgCache._agent || null;
      var merged = withCurrent(devList);
       
      if (!devList.length) { paintDevices(merged, ""); }
      return !!(curAgent && curAgent.url);
    }).catch(function () { return false; });
  }

  function scanDevices() {
    if (offlineMode()) {
      devList = [];
      paintDevices([], "");
      return Promise.resolve(devList);
    }
    if (!authed()) { return Promise.resolve(devList); }
    if (devScanning || !window.dmApi || !window.dmApi.scanAgents) { return Promise.resolve(devList); }
    devScanning = true;
    if (devMetaEl) { devMetaEl.textContent = "扫描中…"; }
    return window.dmApi.scanAgents().then(function (r) {
      var raw = (r && r.devices) || [];
      devList = withCurrent(raw);
      var n = devList.length;
      var took = ((r && r.took_ms ? r.took_ms : 0) / 1000).toFixed(1);
      paintDevices(devList, n ? (n + " 台在线") : "");
      return devList;
    }).catch(function (e) {
      if (devMetaEl) { devMetaEl.textContent = "扫描失败：" + ((e && e.message) || "接口异常"); }
      return devList;
    }).then(function (d) {
      devScanning = false;
      return d;
    });
  }

  function startDevPoll() {
    stopDevPoll();
    if (offlineMode()) {            
      devList = [];
      paintDevices([], "");
      return;
    }
    scanDevices();
    devPollTimer = setInterval(function () {
      var v = document.getElementById("view-hoststatus");
      if (!v || v.hidden) { stopDevPoll(); return; }
      scanDevices();
    }, DEV_POLL_MS);
  }

  function stopDevPoll() {
    if (devPollTimer) { clearInterval(devPollTimer); devPollTimer = null; }
  }

  window.addEventListener("dm:offline", function () {
    stopDevPoll();
    devList = [];
    paintDevices([], "");
  });
  window.addEventListener("dm:online", function () {
    var v = document.getElementById("view-hoststatus");
    if (v && !v.hidden) { startDevPoll(); }
  });

  window.addEventListener("dm:agent-reset", function () {
    stopDevPoll();
    curAgent = null;
    devList = [];
    cfgCache = null;
    paintDevices([], "");
    syncCfgFromServer();
  });

  function connectDevice(d) {
    if (devBusy || !window.dmApi || !window.dmApi.connectAgent) { return Promise.resolve(false); }
    devBusy = true;
    var nm = d.host || d.ip;
    if (devMetaEl) { devMetaEl.textContent = "正在连接 " + nm + " …"; }
    return window.dmApi.connectAgent(d.ip, d.discovery_port || 5111).then(function (r) {
      var dev = (r && r.device) || {};
       
      curAgent = {
        url: (r && r.url) || d.ip, port: r && r.port, device_id: r && r.device_id,
        host: dev.host, arch: dev.arch, disk: dev.disk, reachable: true
      };
      toast("已连接 " + (dev.host || nm) + "（通信口 " + (r && r.port) + "）");
      return syncCfgFromServer();
    }).then(function () {
      devBusy = false;
      return scanDevices();           
    }).then(function () {
      return true;
    }).catch(function (e) {
      devBusy = false;
      toast("连接失败：" + ((e && e.message) || "接口异常"));
      if (devMetaEl) { devMetaEl.textContent = ""; }
      return false;
    });
  }

  function enterDevice(d) {
    if (devBusy) { return; }
    if (d && d.current) { openDeviceDetail(); return; }
    connectDevice(d).then(function (ok) { if (ok) { openDeviceDetail(); } });
  }

  function openDeviceDetail() {
    paintDevHead();
    if (window.dmRoute) { window.dmRoute("device", true); }
    else { document.dispatchEvent(new CustomEvent("dm:go", { detail: "device" })); }
  }

  function paintDevHead() {
    var hostEl = $("#t-device");
    var subEl = $("#dev-sub");
    var ip = devSplitUrl((curAgent && curAgent.url) || "").ip;
    var port = (curAgent && curAgent.port) || devSplitUrl((curAgent && curAgent.url) || "").port;
    var host = (curAgent && (curAgent.host || curAgent.device_id)) || ip || "设备";
    if (hostEl && hostEl.textContent !== host) {
      hostEl.textContent = host;
      if (window.dmSplitTitle) { window.dmSplitTitle(hostEl); }
    }
    if (subEl) {
      subEl.textContent = (curAgent && ip) ? (ip + (port ? " : " + port : "")) : "";
    }
  }

  function disconnectDevice() {
    if (devBusy || !window.dmApi || !window.dmApi.selectAgent) { return Promise.resolve(false); }
    devBusy = true;
    return window.dmApi.selectAgent("").then(function () {
      curAgent = null;               
      toast("已断开采集端");
    }).catch(function (e) {
      toast("断开失败：" + ((e && e.message) || "接口异常"));
    }).then(function () {
      devBusy = false;
      return syncCfgFromServer();
    }).then(function () {
      devBusy = false;
      if (window.dmRoute) { window.dmRoute("status", true); }
      else { document.dispatchEvent(new CustomEvent("dm:go", { detail: "status" })); }
      return true;
    });
  }

  var devBackEl = $("#dev-back");
  if (devBackEl) {
    devBackEl.addEventListener("click", function () {
      document.dispatchEvent(new CustomEvent("dm:go", { detail: "status" }));
    });
  }
  var devOffEl = $("#dev-disconnect");
  if (devOffEl) {
    devOffEl.addEventListener("click", function () { disconnectDevice(); });
  }

  function useCurrentDeviceThen(done) {
    refreshDevices().then(function (has) {
      if (!has) {
        openModal({
          title: "还没有连接设备",
          html: '<p class="modal__line">请在设备状态页面选择一台设备</p>',
          ok: "设备状态", cancel: "取消"
        }).then(function (yes) {
          if (yes) {
            document.dispatchEvent(new CustomEvent("dm:go", { detail: "status" }));
          }
        });
        return;
      }
      done();
    });
  }

  function renderStatus() {
    startDevPoll();                  
    refreshDevices();
  }

  function renderDevice() {
    syncCfgFromServer().then(refreshDevices).then(function (has) {
      if (!has) {
        if (window.dmRoute) { window.dmRoute("status", true); }
        else { document.dispatchEvent(new CustomEvent("dm:go", { detail: "status" })); }
        return;
      }
      paintDevHead();
      renderDevCfg();           
      refreshSystem();
      pollLatency();
      renderServices(null);
      refreshProcStatus();
      renderNetWave();
      startSysPoll();
    });
  }

  function humanBytes(n) {
    if (!n && n !== 0) { return "—"; }
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)) + " " + u[i];
  }

  var updatedEl = $("#status-updated");
  var sysTimer = null;

  function refreshSystem() {
    if (!window.dmApi || !window.dmApi.system) { return; }
    window.dmApi.system().then(function (s) {
      renderSystem(s);
      if (updatedEl) {
        var d = new Date();
        var p = function (n) { return (n < 10 ? "0" : "") + n; };
        updatedEl.textContent = "更新于 " + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
      }
    }).catch(function () {});
  }

  function levelOf(pct) {
    if (pct >= 85) { return "is-crit"; }
    if (pct >= 70) { return "is-warn"; }
    return "is-ok";
  }

  var LEVEL_COLOR = {
    "is-ok": "var(--dm-blue)",
    "is-warn": "#f5b800",
    "is-crit": "var(--dm-red)"
  };

  function paintGauge(gid, numId, subId, pct, level, value, sub) {
    var ring = $("#" + gid);
    var num = $("#" + numId);
    var s = $("#" + subId);
    var p = Math.min(100, Math.max(0, pct || 0));

    if (ring) {
      ring.style.setProperty("--pct", p + "%");
      ring.style.setProperty("--arc", LEVEL_COLOR[level] || LEVEL_COLOR["is-ok"]);
      ring.className = "gauge__ring " + (level || "");
    }
    if (num) { num.textContent = value; num.className = "gauge__num " + (level || ""); }
    if (s) { s.textContent = sub || ""; }
  }

  function renderSystem(s) {
    if (!s) { return; }

    var cpu = s.cpu || {};
    paintGauge("g-cpu", "m-cpu", "m-cpu-sub", cpu.percent, levelOf(cpu.percent || 0),
      (cpu.percent != null ? Math.round(cpu.percent) + "%" : "—"),
      (cpu.cores ? cpu.cores + " 核  负载 " + (cpu.load1 != null ? cpu.load1 : "—") : ""));

    var mem = s.memory || {};
    paintGauge("g-mem", "m-mem", "m-mem-sub", mem.percent, levelOf(mem.percent || 0),
      (mem.percent != null ? Math.round(mem.percent) + "%" : "—"),
      (mem.total ? humanBytes(mem.used) + " / " + humanBytes(mem.total) : ""));

    var disk = s.disk || {};
    paintGauge("g-disk", "m-disk", "m-disk-sub", disk.percent, levelOf(disk.percent || 0),
      (disk.percent != null ? Math.round(disk.percent) + "%" : "—"),
      (disk.total ? "剩 " + humanBytes(disk.free) : ""));

    var temp = s.temperature;
    if (temp && temp.celsius != null) {
      var lv = temp.celsius >= 75 ? "is-crit" : (temp.celsius >= 65 ? "is-warn" : "is-ok");
      paintGauge("g-temp", "m-temp", "m-temp-sub", Math.min(100, temp.celsius), lv,
        Math.round(temp.celsius) + "°", temp.sensor || "");
    } else {
      paintGauge("g-temp", "m-temp", "m-temp-sub", 0, "is-ok", "—", "无传感器");
    }
  }

  var NET_MAX = 60;
  var netHist = [];

  function pushLatency(ms) {
    netHist.push(ms);
    if (netHist.length > NET_MAX) { netHist.shift(); }
  }

  function pollLatency() {
    if (!window.dmApi || !window.dmApi.latency) { return; }
    window.dmApi.latency().then(function (d) {
      pushLatency(d && d.ms != null ? d.ms : null);
      var gw = $("#netwave-gw");
      if (gw) { gw.textContent = (d && d.gateway) ? ("网关 " + d.gateway) : ""; }
      renderNetWave();
    }).catch(function () {
      pushLatency(null);
      renderNetWave();
    });
  }

  function msLevel(ms) {
    if (ms == null) { return "is-crit"; }
    if (ms < 20) { return "is-ok"; }
    if (ms < 60) { return "is-warn"; }
    return "is-crit";
  }

  function renderNetWave() {
    var canvas = $("#netwave-canvas");
    var empty = $("#netwave-empty");
    if (!canvas) { return; }

    var valid = netHist.filter(function (v) { return v != null; });
    var hasData = valid.length > 0;

    if (empty) { empty.hidden = hasData; }
    canvas.style.visibility = hasData ? "visible" : "hidden";
    if (!hasData) {
      var now0 = $("#netwave-now");
      if (now0) { now0.textContent = "—"; now0.className = "netwave__now"; }
      return;
    }

    var dpr = window.devicePixelRatio || 1;
    var cssW = canvas.clientWidth || 600;
    var cssH = canvas.clientHeight || 96;
    if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
      canvas.width = Math.round(cssW * dpr);
      canvas.height = Math.round(cssH * dpr);
    }
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    var peak = Math.max.apply(null, valid);
    var top = Math.max(20, Math.ceil((peak * 1.25) / 10) * 10);

    var padT = 8, padB = 8, padL = 2, padR = 2;
    var x0 = padL, x1 = cssW - padR, y0 = padT, y1 = cssH - padB;
    var n = netHist.length;

    var xOf = function (i) { return x0 + (n <= 1 ? 0 : (i / (n - 1)) * (x1 - x0)); };
    var yOf = function (v) { return y1 - (v / top) * (y1 - y0); };

    ctx.strokeStyle = "rgba(0,0,0,.07)";
    ctx.lineWidth = 1;
    [0, .5, 1].forEach(function (f) {
      var y = y0 + f * (y1 - y0);
      ctx.beginPath();
      ctx.moveTo(x0, Math.round(y) + .5);
      ctx.lineTo(x1, Math.round(y) + .5);
      ctx.stroke();
    });

    var last = null;
    for (var i = netHist.length - 1; i >= 0; i--) {
      if (netHist[i] != null) { last = netHist[i]; break; }
    }
    var color = last == null ? "#ff453a"
              : (last < 20 ? "#0071e3" : (last < 60 ? "#f5b800" : "#ff453a"));

    var grad = ctx.createLinearGradient(0, y0, 0, y1);
    grad.addColorStop(0, color + "33");
    grad.addColorStop(1, color + "00");

    ctx.beginPath();
    var pen = false;
    for (var i2 = 0; i2 < n; i2++) {
      var v = netHist[i2];
      if (v == null) { pen = false; continue; }
      var px = xOf(i2), py = yOf(v);
      if (!pen) { ctx.moveTo(px, y1); ctx.lineTo(px, py); pen = true; }
      else { ctx.lineTo(px, py); }
    }
    if (pen) {
      ctx.lineTo(xOf(n - 1), y1);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();
    }

    ctx.strokeStyle = color;
    ctx.lineWidth = 1.6;
    ctx.lineJoin = "round";
    ctx.beginPath();
    pen = false;
    for (var i3 = 0; i3 < n; i3++) {
      var v2 = netHist[i3];
      if (v2 == null) { pen = false; continue; }
      var px2 = xOf(i3), py2 = yOf(v2);
      if (!pen) { ctx.moveTo(px2, py2); pen = true; }
      else { ctx.lineTo(px2, py2); }
    }
    ctx.stroke();

    if (last != null) {
      ctx.beginPath();
      ctx.arc(xOf(n - 1), yOf(last), 2.6, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
    }

    ctx.fillStyle = "rgba(0,0,0,.32)";
    ctx.font = "10px " + (getComputedStyle(document.body).fontFamily || "sans-serif");
    ctx.textAlign = "left";
    ctx.fillText(top + "ms", x0 + 3, y0 + 9);

    var avg = valid.reduce(function (a, b) { return a + b; }, 0) / valid.length;
    var setTxt = function (id, val, cls) {
      var el = $("#" + id);
      if (!el) { return; }
      el.textContent = val;
      el.className = cls || "";
    };
    var nowEl = $("#netwave-now");
    if (nowEl) {
      nowEl.textContent = last == null ? "超时" : (last + "ms");
      nowEl.className = "netwave__now " + msLevel(last);
    }
    setTxt("netwave-cur", last == null ? "—" : (last + "ms"), msLevel(last));
    setTxt("netwave-avg", avg.toFixed(1) + "ms", msLevel(avg));
    setTxt("netwave-max", peak.toFixed(1) + "ms", msLevel(peak));
  }

  window.addEventListener("resize", function () {
    if (devViewEl && !devViewEl.hidden) { renderNetWave(); }
  }, { passive: true });

  function svcLabel(row, cfg) {
    cfg = cfg || loadCfg() || CFG_DEFAULT;
    var v = "";
    if (row.labelKey === "k7_bin") { v = cfg.k7_bin; }
    else if (row.labelKey === "robot_service_script") {
      v = (cfg.robot_service_script || "").split("/").pop();
    }
    else if (row.labelKey === "rl_deploy_bin") { v = cfg.rl_deploy_bin; }
    v = v && String(v).trim();
    return v || row.fallback;
  }

  var SVC_ROWS = [
    { key: "k7master3", labelKey: "k7_bin", fallback: "K7Master3",
      start: "t1", stop: "k7-stop", force: true },
    { key: "robotservice", labelKey: "robot_service_script", fallback: "runService.sh",
      start: "service-start", stop: "service-stop" },
    { key: "rl_deploy", labelKey: "rl_deploy_bin", fallback: "rl_deploy",
      start: "deploy-start", stop: "deploy-stop" }
  ];

  var svcBusy = {};

  function buildServiceRows(el) {
    el.innerHTML = "";
    SVC_ROWS.forEach(function (row, i) {
      var li = document.createElement("li");
      li.className = "svc";
      li.dataset.key = row.key;
      li.style.setProperty("--i", i);

      var dot = document.createElement("i");
      dot.className = "svc__dot is-idle";
      dot.dataset.role = "dot";

      var name = document.createElement("span");
      name.className = "svc__name";
      name.dataset.role = "name";
      name.textContent = svcLabel(row);

      var state = document.createElement("span");
      state.className = "svc__state";
      state.dataset.role = "state";
      state.textContent = "—";

      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "svc__btn is-start";
      btn.dataset.role = "btn";
      btn.textContent = "启动";
      btn.addEventListener("click", function () {
        svcToggle(row, li.classList.contains("is-on"));
      });

      li.appendChild(dot);
      li.appendChild(name);
      li.appendChild(state);
      li.appendChild(btn);
      el.appendChild(li);
    });
  }

  function renderServices(status) {
    var el = $("#svc-list");
    if (!el) { return; }

    if (!el.children.length) {
      buildServiceRows(el);
       
      setTimeout(function () { el.classList.add("is-settled"); }, 520);
    }

    SVC_ROWS.forEach(function (row) {
      var li = el.querySelector('.svc[data-key="' + row.key + '"]');
      if (!li) { return; }

      var info = status && status[row.key];
      var running = !!(info && info.running);
      var busy = !!svcBusy[row.key];

      var dot = li.querySelector('[data-role="dot"]');
      var state = li.querySelector('[data-role="state"]');
      var btn = li.querySelector('[data-role="btn"]');
      var nameEl = li.querySelector('[data-role="name"]');

      if (nameEl) {
        var label = svcLabel(row);
        if (nameEl.textContent !== label) { nameEl.textContent = label; }
      }

      li.classList.toggle("is-on", running);

      if (dot) {
        dot.className = "svc__dot " + (running ? "is-on" : (status ? "is-off" : "is-idle"));
      }

      var text = busy ? "处理中…"
        : running ? ("运行中" + (info.uptime_s ? " " + info.uptime_s + "s" : ""))
        : status ? "已停止"
        : "—";
       
      if (state && state.textContent !== text) { state.textContent = text; }

      if (btn) {
        var label = running ? "关闭" : "启动";
        var cls = "svc__btn " + (running ? "is-stop" : "is-start");
        if (btn.textContent !== label) { btn.textContent = label; }
        if (btn.className !== cls) { btn.className = cls; }
        btn.disabled = busy;
        if (row.force && running) { btn.title = "强制终止（kill -9）"; }
        else { btn.removeAttribute("title"); }
      }
    });
  }

  function refreshProcStatus() {
    if (window.dmBridge && typeof window.dmBridge.status === "function") {
      window.dmBridge.status().then(renderServices).catch(function () {
        renderServices(null);
      });
    } else {
      renderServices(null);
    }
  }

  function svcToggle(row, running) {
    var cfg = loadCfg() || CFG_DEFAULT;
    if (!window.dmApi) { toast("接口模块未加载"); return; }

    svcBusy[row.key] = true;
    renderServices(lastStatus);

    var action = running ? row.stop : row.start;
    window.dmApi.runAction(action, cfg).then(function (r) {
      svcBusy[row.key] = false;
      if (r && r.ok === false) {
        toast(svcLabel(row) + " 操作失败：" + (r.error || "未知"));
      } else {
        toast(svcLabel(row) + (running ? " 已关闭" : " 已启动"));
      }
      refreshProcStatus();
    }).catch(function (e) {
      svcBusy[row.key] = false;
      toast(svcLabel(row) + " 操作失败：" + (e.message || e));
      refreshProcStatus();
    });
  }

  var lastStatus = null;
  var _origRender = renderServices;
  renderServices = function (st) {
    if (st) { lastStatus = st; }
    return _origRender(st || lastStatus);
  };

  function startSysPoll() {
    stopSysPoll();
    sysTimer = setInterval(function () {
      var v = document.getElementById("view-device");
      if (!v || v.hidden) { stopSysPoll(); return; }
      refreshSystem();
      refreshProcStatus();
      pollLatency();
    }, 3000);
  }

  function stopSysPoll() {
    if (sysTimer) { clearInterval(sysTimer); sysTimer = null; }
  }

  document.addEventListener("dm:go", function () {   });

  window.dmCapture = {
    renderTasks: renderTasks,
    renderDevCfg: renderDevCfg,
    renderStatus: renderStatus,
    renderDevice: renderDevice,
     
    stopDevPoll: stopDevPoll,
    setAccount: function () { renderTasks(); renderStatus(); },
    hostOf: hostOf,
     
    openModal: openModal,
    toast: toast,
     
    loadCfg: loadCfg,
    syncCfgFromServer: syncCfgFromServer,
    dataDir: dataDir,
    CFG_DEFAULT: CFG_DEFAULT
  };

  renderTasks();
  renderStatus();
  resumeRunningSession();
})();

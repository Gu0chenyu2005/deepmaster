/*
Copyright (c) 2026 deeprobotics
Author Chenyu Guo
Date November 2026
*/
/* 采集控制桥：经服务器接口调用采集端代理或本地执行器。 */
(function () {
  "use strict";

  if (window.dmBridge && typeof window.dmBridge.t1 === "function") {
    /* 保留宿主已注入的控制桥。 */
    return;
  }

  function post(path, body) {
    return fetch("/api" + path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + (function () {
          try { return sessionStorage.getItem("dm.token") || ""; } catch (e) { return ""; }
        })()
      },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok || j.ok === false) {
          var err = new Error(j.message || ("HTTP " + r.status));
          err.code = j.code || r.status;
          throw err;
        }
        return j.data !== undefined ? j.data : j;
      });
    });
  }

  function get(path) {
    return fetch("/api" + path, {
      headers: {
        "Authorization": "Bearer " + (function () {
          try { return sessionStorage.getItem("dm.token") || ""; } catch (e) { return ""; }
        })()
      },
      cache: "no-store"
    }).then(function (r) { return r.json(); })
      .then(function (j) {
        if (j.ok === false) { throw new Error(j.message || "请求失败"); }
        return j.data !== undefined ? j.data : j;
      });
  }

  function cameraBase(cfg) {
    var agent = cfg && cfg._agent;
    if (!agent || !agent.url) { throw new Error("未连接采集端，无法打开实时预览"); }
    var url = new URL(agent.url);
    if (!/^https?:$/.test(url.protocol)) { throw new Error("采集端地址无效"); }
    return url.origin + "/api/cam";
  }

  function wrap(action, cfg, onLine, extra) {
    var note = function (t) { try { onLine && onLine(t); } catch (e) {} };
    note("→ 本机执行 " + action);
    var body = Object.assign({ cfg: cfg || {} }, extra || {});
    return post("/run/" + action, body)
      .then(function (d) {
        if (d && d.ok === false) {
          note("✗ " + (d.error || "失败"));
          return { ok: false, error: d.error || "执行失败" };
        }
        if (action === "cam-start" && d && d.camera_token) {
          cameraToken = d.camera_token;
        }
        if (action === "cam-stop" || action === "control-stop" || action === "capture-stop") {
          cameraToken = "";
        }
        if (d && d.pid) { note("✓ pid " + d.pid); }
        return Object.assign({}, d || {}, { ok: true, code: 0, error: null });
      })
      .catch(function (e) {
        note("✗ " + e.message);
        return { ok: false, error: e.message };
      });
  }

  var cameraToken = "";

  function cameraQuery(extra) {
    if (!cameraToken) { throw new Error("相机访问授权不可用，请重新启动相机预览"); }
    return (extra ? extra + "&" : "") + "token=" + encodeURIComponent(cameraToken);
  }

  window.dmBridge = {
    t1: function (cfg, onLine) { return wrap("t1", cfg, onLine); },
    tare: function (cfg, onLine) { return wrap("tare", cfg, onLine); },
    t2: function (cfg, onLine, controlOnly) {
      return wrap("t2", cfg, onLine, controlOnly ? { control_only: true } : null);
    },
     
    z: function (cfg, onLine) { return wrap("z", cfg, onLine); },
    stop: function (cfg, onLine) { return wrap("stop", cfg, onLine); },
    captureStop: function (cfg, onLine) { return wrap("capture-stop", cfg, onLine); },
    controlStop: function (cfg, onLine) { return wrap("control-stop", cfg, onLine); },
    camStart: function (cfg, onLine, mode) {
      return wrap("cam-start", cfg, onLine, mode ? { mode: mode } : null);
    },
    camStop: function (cfg, onLine) { return wrap("cam-stop", cfg, onLine); },
    webrtcOffer: function (payload) { return post("/webrtc/offer", payload); },
    webrtcClose: function (sessionId) {
      return post("/webrtc/close", { session_id: sessionId });
    },
    webrtcQuality: function (sessionId, profile) {
      return post("/webrtc/quality", { session_id: sessionId, profile: profile });
    },

    streamUrl: function (cfg, cam, profile) {
      var base = cameraBase(cfg);
      var params = "direct=" + Date.now();
      if (profile) { params += "&profile=" + encodeURIComponent(profile); }
      params = cameraQuery(params);
      return base + "/stream/" + encodeURIComponent(cam || "wrist") + "?" + params;
    },

    camHealth: function (cfg) {
      var base = cameraBase(cfg);
      return fetch(base + "/health?" + cameraQuery(""), { cache: "no-store" })
        .then(function (r) { if (!r.ok) { throw new Error("HTTP " + r.status); } return r.json(); })
        .then(function (j) { return j.data !== undefined ? j.data : j; })
        .catch(function () { return null; });
    },

    frame: function (cfg, cam) {
      var base = cameraBase(cfg);
      return Promise.resolve(base + "/snap/" + encodeURIComponent(cam || "wrist") +
                             "?" + cameraQuery("t=" + Date.now()));
    },

    status: function () { return get("/run/status"); }
  };
})();

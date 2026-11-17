/*
Copyright (c) 2026 deeprobotics
Author Chenyu Guo
Date November 2026
*/
/* 同源 API 客户端：鉴权、响应解包与错误处理。 */
(function () {
  "use strict";

  var TOKEN_KEY = "dm.token";
  var BASE = "";             

  function token() {
    try { return sessionStorage.getItem(TOKEN_KEY) || ""; }
    catch (e) { return ""; }
  }

  function setToken(t) {
    try {
      if (t) { sessionStorage.setItem(TOKEN_KEY, t); }
      else { sessionStorage.removeItem(TOKEN_KEY); }
    } catch (e) {}
  }

  function clearToken() { setToken(""); }

  function qs(params) {
    if (!params) { return ""; }
    var parts = [];
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (v === undefined || v === null || v === "") { return; }
      parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
    });
    return parts.length ? ("?" + parts.join("&")) : "";
  }

  function request(method, path, opts) {
    opts = opts || {};
    var headers = {};
    if (opts.body !== undefined) { headers["Content-Type"] = "application/json"; }
    var t = token();
    if (t) { headers["Authorization"] = "Bearer " + t; }

    var url = BASE + "/api" + path + qs(opts.query);

    return fetch(url, {
      method: method,
      headers: headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      cache: "no-store"
    }).then(function (r) {
      var ctype = r.headers.get("Content-Type") || "";
      if (ctype.indexOf("application/json") < 0) {
        if (!r.ok) { throw new ApiError(r.status, "HTTP " + r.status); }
        return r;
      }
      return r.json().then(function (j) {
        if (!r.ok || j.ok === false) {
          var code = j.code || r.status;
          if (code === 401) { clearToken(); }
           
          if (code !== 401 && window.dmReportError) {
            window.dmReportError("api", method + " " + path + " → " + code,
              (j.message || "") + " | " + method + " " + url);
          }
          throw new ApiError(code, j.message || ("HTTP " + r.status), j.data);
        }
        return j.data !== undefined ? j.data : j;
      });
    });
  }

  function ApiError(code, message, data) {
    this.name = "ApiError";
    this.code = code;
    this.message = message;
    this.data = data || {};
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  var API = {
     
    login: function (account, password) {
      return request("POST", "/auth/login", { body: { account: account, password: password } })
        .then(function (d) { setToken(d.token); return d; });
    },
    logout: function () { clearToken(); },
     
    register: function (account, password, displayName) {
      return request("POST", "/auth/register", {
        body: { account: account, password: password, display_name: displayName }
      }).then(function (d) { setToken(d.token); return d; });
    },
     
    overview: function () { return request("GET", "/overview"); },
    setToken: setToken,
     
    resetPassword: function (account, code, password) {
      return request("POST", "/auth/reset", {
        body: { account: account, code: code, password: password }
      }).then(function (d) { setToken(d.token); return d; });
    },
     
    issueResetCode: function (account) {
      return request("POST", "/reset_code", { body: { account: account } });
    },
    token: token,
    hasToken: function () { return !!token(); },
     
    preflight: function (dir, folder) {
      return request("GET", "/preflight", { query: { dir: dir, folder: folder } });
    },
    startSession: function (dir, folder) {
      return request("POST", "/sessions", { body: { dir: dir, folder: folder } });
    },
    endSession: function (id, durationS, state, error) {
      return request("PATCH", "/sessions/" + encodeURIComponent(id), {
        body: { duration_s: durationS, state: state || "done", error: error || null }
      });
    },
    runningSession: function () { return request("GET", "/sessions/running"); },
     
    runAction: function (action, cfg) {
      return request("POST", "/run/" + action, { body: { cfg: cfg || {} } });
    },
    runStatus: function () { return request("GET", "/run/status"); },

    records: function (dir, sync, remote) {
      return request("GET", "/records", {
        query: { dir: dir, sync: sync ? 1 : 0, remote: remote ? 1 : 0 }
      });
    },
    deleteRecord: function (dir, name, remote) {
      return request("DELETE", "/records", {
        query: { dir: dir, name: name, remote: remote ? 1 : 0 }
      });
    },
     
    deleteDir: function (path, remote) {
      return request("DELETE", "/dirs", { query: { path: path, remote: remote ? 1 : 0 } });
    },

    health: function () { return request("GET", "/health"); },

    uploads: function () { return request("GET", "/uploads"); },
    startUpload: function (dirs) {
      return request("POST", "/uploads", { body: { dirs: dirs || [] } });
    },

    dirs: function (path, remote) {
      return request("GET", "/dirs", { query: { path: path, remote: remote ? 1 : 0 } });
    },
    episodes: function (dir) {
      return request("GET", "/episodes", { query: { dir: dir } });
    },
    meta: function (dir, ep) {
      return request("GET", "/episode/" + ep + "/meta", { query: { dir: dir } });
    },
    curves: function (dir, ep) {
      return request("GET", "/episode/" + ep + "/curves", { query: { dir: dir } });
    },
    frameURL: function (dir, ep, cam, idx, tokenSuffix) {
      return BASE + "/api/episode/" + ep + "/frame/" + cam + "/" + idx +
             qs({ dir: dir, v: tokenSuffix || "" });
    },
    saveReview: function (dir, episode, payload) {
      var body = { dir: dir, episode: episode };
      Object.keys(payload || {}).forEach(function (k) { body[k] = payload[k]; });
      return request("POST", "/review", { body: body });
    },
    batchReview: function (dir, episodes, status, comment) {
      return request("POST", "/reviews/batch", {
        body: { dir: dir, episodes: episodes, status: status, comment: comment }
      });
    },
     
    system: function () { return request("GET", "/system"); },
    latency: function () { return request("GET", "/latency"); },

    scanAgents: function () { return request("POST", "/agent/scan", { body: {} }); },
     
    connectAgent: function (ip, discoveryPort) {
      return request("POST", "/agent/connect", {
        body: { ip: ip, discovery_port: discoveryPort || 5111 }
      });
    },
     
    selectAgent: function (url) {
      return request("POST", "/agent/select", { body: { url: url || "" } });
    },

    getConfig: function () { return request("GET", "/config"); },
    saveConfig: function (cfg) { return request("POST", "/config", { body: cfg }); },

    stats: function () { return request("GET", "/stats"); },
    me: function () { return request("GET", "/me"); },

    users: function () { return request("GET", "/users"); },
    createUser: function (account, password, displayName, role) {
      return request("POST", "/users", {
        body: { account: account, password: password,
                display_name: displayName, role: role || "user" }
      });
    },
    setUserRole: function (account, role) {
      return request("PATCH", "/users", { body: { account: account, role: role } });
    },
    deleteUser: function (account) {
      return request("DELETE", "/users", { query: { account: account } });
    },

    reportReviewTime: function (dir, episode, seconds) {
      return request("POST", "/review_time", {
        body: { dir: dir, episode: episode, seconds: seconds }
      });
    },

    request: request,

    ApiError: ApiError
  };

  window.dmApi = API;

  var DATA_DIR_KEY = "dm.review.dir";
  window.dmDataDir = {
    get: function () {
      try { return localStorage.getItem(DATA_DIR_KEY) || ""; }
      catch (e) { return ""; }
    },
    set: function (dir) {
      if (!dir) { return; }
      try { localStorage.setItem(DATA_DIR_KEY, dir); } catch (e) {}
      document.dispatchEvent(new CustomEvent("dm:datadir", { detail: dir }));
    }
  };
})();

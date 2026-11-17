#!/usr/bin/env python3
# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""提供静态资源并将 /api 请求转发至后端。"""
import os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen, Request
from urllib.error import HTTPError


try:
    import requests
    _SESSION = requests.Session()
    _SESSION.mount("http://", requests.adapters.HTTPAdapter(
        pool_connections=32, pool_maxsize=64, max_retries=0))
except Exception:
    requests = None
    _SESSION = None


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_HOST = "127.0.0.1"
API_PORT = int(os.environ.get("API_PORT", "8765"))
BACKEND = "http://%s:%s" % (API_HOST, API_PORT)
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8790"))

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass


    PASS_HEADERS = ("Cache-Control", "ETag", "Last-Modified", "Expires",
                    "Content-Disposition")
    def _stream_proxy(self):
        import http.client
        try:
            conn = http.client.HTTPConnection(API_HOST, API_PORT, timeout=30)
            conn.request("GET", self.path)
            r = conn.getresponse()
        except Exception as e:      # noqa: BLE001
            msg = ('{"code":502,"message":"%s"}' % e).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return

        self.send_response(r.status)
        ctype = r.getheader("Content-Type") or "multipart/x-mixed-replace; boundary=frame"
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True


        try:
            while True:
                chunk = r.read1(16384) if hasattr(r, "read1") else r.read(16384)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:           # noqa: BLE001
            pass
        finally:
            try:
                conn.close()
            except Exception:       # noqa: BLE001
                pass

    def _proxy(self, method):
        url = BACKEND + self.path
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(n) if n else None
        fwd = {}
        for h in ("Content-Type", "Authorization", "If-None-Match", "If-Modified-Since"):
            if self.headers.get(h):
                fwd[h] = self.headers[h]

        upstream = None
        if _SESSION is not None:

            try:
                r = _SESSION.request(method, url, data=body, headers=fwd, timeout=30)
                code, data = r.status_code, r.content
                ctype = r.headers.get("Content-Type")
                upstream = r.headers
            except Exception as e:
                code, data, ctype = 502, ('{"code":502,"message":"%s"}' % e).encode(), "application/json"
        else:
            req = Request(url, data=body, method=method)
            for k, v in fwd.items():
                req.add_header(k, v)
            try:
                r = urlopen(req, timeout=30)
                code, data, ctype = r.status, r.read(), r.headers.get("Content-Type")
                upstream = r.headers
            except HTTPError as e:
                code, data = e.code, e.read()
                ctype = e.headers.get("Content-Type", "application/json")
                upstream = e.headers
            except Exception as e:
                code, data, ctype = 502, ('{"code":502,"message":"%s"}' % e).encode(), "application/json"

        self.send_response(code)
        self.send_header("Content-Type", ctype or "application/json")
        self.send_header("Content-Length", str(len(data)))
        if upstream:
            for h in self.PASS_HEADERS:
                v = upstream.get(h)
                if v:
                    self.send_header(h, v)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _static(self):
        path = self.path.split("?")[0].lstrip("/") or "index.html"
        f = os.path.join(ROOT, path)
        if not os.path.isfile(f) or ".." in path:
            self.send_response(404); self.send_header("Content-Length","0"); self.end_headers(); return
        data = open(f,"rb").read()
        ext = os.path.splitext(f)[1]
        ctype = {".html":"text/html; charset=utf-8",".js":"text/javascript; charset=utf-8",
                 ".css":"text/css; charset=utf-8",".png":"image/png",".svg":"image/svg+xml",
                 ".jpg":"image/jpeg"}.get(ext,"application/octet-stream")


        etag = 'W/"%x-%x"' % (int(os.path.getmtime(f) * 1e6), len(data))
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304); self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache"); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.send_header("Last-Modified", self.date_time_string(int(os.path.getmtime(f))))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/cam/stream/"):
            return self._stream_proxy()
        if self.path.startswith("/api/"): return self._proxy("GET")
        return self._static()

    def do_POST(self):
        if self.path.startswith("/api/"): return self._proxy("POST")
        self.send_response(404); self.send_header("Content-Length","0"); self.end_headers()

    def do_PATCH(self):
        if self.path.startswith("/api/"): return self._proxy("PATCH")
        self.send_response(404); self.send_header("Content-Length","0"); self.end_headers()

    def do_DELETE(self):
        if self.path.startswith("/api/"): return self._proxy("DELETE")
        self.send_response(404); self.send_header("Content-Length","0"); self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204); self.send_header("Content-Length","0"); self.end_headers()

if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    srv.daemon_threads = True
    print("deepmaster: http://127.0.0.1:%d  (API -> %s)" % (PORT, BACKEND))
    srv.serve_forever()

#!/usr/bin/env python3
# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""采集端代理：程序控制、设备发现、相机转发与数据归档。"""

import argparse
import hashlib
import hmac
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import cam        # noqa: E402
import runner     # noqa: E402
import system     # noqa: E402

VERSION = "1.1"

STATUS_PATTERNS = {
    "k7master3": ["K7Master3"],
    "robotservice": ["runService.sh", "RoboticsServiceProcess"],
    "rl_deploy": ["rl_deploy"],
    "camview": ["camera_shm_viewer.py"],
}

ACTIONS = {
    "preflight": runner.preflight_local,
    "t1": runner.run_t1,
    "tare": runner.run_tare,
    "t2": runner.run_t2,
    "z": runner.inject_z,
    "service-start": runner.start_service,
    "service-stop": runner.stop_service,
    "deploy-start": runner.start_deploy,
    "deploy-stop": runner.stop_deploy,
    "capture-stop": runner.stop_capture,
    "k7-stop": runner.stop_k7_force,
    "stop": runner.stop_all,
}

STATE = {
    "cfg": {},              
    "cfg_path": "",         
    "data_root": "",        
    "started": time.time(),
    "cam_key": "camview",
    "device_id": "",        
    "bind": "0.0.0.0",
    "discovery_port": 5111, 
    "api_port": 0,          
    "servers": {},          
}
ARCHIVE_LOCK = threading.RLock()
ARCHIVE_JOBS = {}
ARCHIVE_SEQ = 0


def load_cfg(path):
    """读取本机配置；文件不可用时返回空配置。"""
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except FileNotFoundError:
        print("[agent] 配置不存在：%s（继续，路径相关的动作会报错）" % path, flush=True)
    except Exception as e:              # noqa: BLE001
        print("[agent] 配置读取失败：%s" % e, flush=True)
    return {}


def resolve_data_root(value):
    base = os.path.dirname(os.path.abspath(STATE["cfg_path"] or BASE))
    path = os.path.expandvars(os.path.expanduser(str(value or "data")))
    return os.path.abspath(path if os.path.isabs(path) else os.path.join(base, path))


def effective_cfg():
    cfg = dict(STATE["cfg"])
    cfg["local_data_dir"] = data_root()
    return cfg


def merge_cfg(request_cfg):
    """程序路径及采集参数仅使用采集端配置。"""
    cfg = effective_cfg()
    requested = request_cfg.get("run_data_dir") if isinstance(request_cfg, dict) else None
    if requested:
        root = os.path.realpath(data_root())
        candidate = os.path.realpath(os.path.expanduser(os.path.expandvars(str(requested))))
        if candidate != root and not candidate.startswith(root + os.sep):
            raise ValueError("采集目录不在采集端数据根目录内")
        os.makedirs(candidate, exist_ok=True)
        cfg["local_data_dir"] = candidate
    return cfg


def save_cfg(update, allow_agent_token=False):
    """原子保存配置，移除空值及 _agent 元信息。"""
    merged = dict(STATE["cfg"])
    for k, v in (update or {}).items():
        if k == "_agent":
            continue
        if k == "agent_token" and not allow_agent_token:
            continue
        if v is None or v == "":
            merged.pop(k, None)
        else:
            merged[k] = v
    root = resolve_data_root(merged.get("local_data_dir"))
    os.makedirs(root, exist_ok=True)
    merged["local_data_dir"] = root
    path = STATE["cfg_path"]
    if not path:
        raise RuntimeError("本机没有配置文件路径（--config）")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    STATE["cfg"] = merged
    STATE["data_root"] = root
    print("[agent] 配置已更新（%d 项）：%s" % (len(merged), path), flush=True)
    return {"count": len(merged), "config": merged}


def data_root():
    return STATE["data_root"] or os.path.join(os.getcwd(), "datasets")


def safe_path(rel):
    """将路径限制在数据根目录内，越界时抛出 ValueError。"""
    root = os.path.realpath(data_root())
    rel = (rel or "").strip().lstrip("/")
    p = os.path.realpath(os.path.join(root, rel))
    if p != root and not p.startswith(root + os.sep):
        raise ValueError("路径越出数据目录：%s" % rel)
    return p


def archive_path(rel):
    root = os.path.realpath(data_root())
    p = safe_path(rel)
    if p == root:
        raise ValueError("不能归档整个数据根")
    key = hashlib.sha256(os.path.relpath(p, root).encode("utf-8")).hexdigest()
    return os.path.join(root, ".dm_archives", key + ".tar.gz")


def _archive_worker(rel, dest, job_id):
    tmp = dest + ".%d.tmp" % job_id
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        proc = subprocess.run(["tar", "-czf", tmp, "-C", safe_path(rel), "."],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              check=False)
        if proc.returncode:
            raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-1000:]
                               or "tar 压缩失败")
        with ARCHIVE_LOCK:
            current = ARCHIVE_JOBS.get(rel) or {}
            if current.get("id") != job_id:
                os.remove(tmp)
                return
            os.replace(tmp, dest)
            ARCHIVE_JOBS[rel] = {"id": job_id, "state": "ready",
                                 "bytes": os.path.getsize(dest), "error": None}
        print("[agent] archive %s -> %d bytes" % (rel, os.path.getsize(dest)), flush=True)
    except Exception as e:                  # noqa: BLE001
        try:
            os.remove(tmp)
        except OSError:
            pass
        with ARCHIVE_LOCK:
            current = ARCHIVE_JOBS.get(rel) or {}
            if current.get("id") == job_id:
                ARCHIVE_JOBS[rel] = {"id": job_id, "state": "error",
                                     "bytes": 0, "error": str(e)}


def start_archive(rel):
    global ARCHIVE_SEQ
    if not os.path.isdir(safe_path(rel)):
        raise ValueError("不是目录：%s" % (rel or "(根)"))
    dest = archive_path(rel)
    with ARCHIVE_LOCK:
        job = ARCHIVE_JOBS.get(rel)
        if job and job.get("state") == "compressing":
            return dict(job)
        ARCHIVE_SEQ += 1
        job_id = ARCHIVE_SEQ
        ARCHIVE_JOBS[rel] = {"id": job_id, "state": "compressing",
                             "bytes": 0, "error": None}
    threading.Thread(target=_archive_worker, args=(rel, dest, job_id), daemon=True).start()
    return {"state": "compressing", "bytes": 0, "error": None}


def invalidate_archive(rel):
    global ARCHIVE_SEQ
    dest = archive_path(rel)
    with ARCHIVE_LOCK:
        ARCHIVE_SEQ += 1
        ARCHIVE_JOBS[rel] = {"id": ARCHIVE_SEQ, "state": "missing",
                             "bytes": 0, "error": None}
        try:
            os.remove(dest)
        except FileNotFoundError:
            pass


def archive_status(rel):
    dest = archive_path(rel)
    with ARCHIVE_LOCK:
        job = ARCHIVE_JOBS.get(rel)
        if job:
            return dict(job)
    if os.path.isfile(dest):
        return {"state": "ready", "bytes": os.path.getsize(dest), "error": None}
    return {"state": "missing", "bytes": 0, "error": None}


def _rel(path):
    """绝对路径 → 相对数据根的展示形式。"""
    root = os.path.realpath(data_root())
    p = os.path.realpath(path)
    return os.path.relpath(p, root) if p != root else ""


def disk_free():
    try:
        u = shutil.disk_usage(data_root())
        return {"total": u.total, "used": u.used, "free": u.free}
    except Exception:                   # noqa: BLE001
        return None


def make_device_id():
    """生成稳定设备标识；克隆镜像应通过 DM_DEVICE_ID 指定唯一值。"""
    forced = (os.environ.get("DM_DEVICE_ID") or "").strip()
    if forced:
        return forced[:32]
    parts = [socket.gethostname()]
    for f in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                parts.append(fh.read().strip())
            break
        except Exception:               # noqa: BLE001
            continue
    parts.append(os.uname().machine)
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def identity():
    """返回握手信息，不执行相机探测。"""
    return {
        "role": "collector",
        "device_id": STATE["device_id"],
        "version": VERSION,
        "host": socket.gethostname(),
        "arch": os.uname().machine,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "uptime_s": int(time.time() - STATE["started"]),
        "data_root": data_root(),
        "disk": disk_free(),
        "configured": bool(STATE["cfg"]),
        "discovery_port": STATE["discovery_port"],
        "api_port": STATE["api_port"],
        "auth_enabled": bool(STATE["cfg"].get("agent_token")),
    }


def _serve(port):
    """后台启动 HTTP 服务，返回 (srv, err)。"""
    try:
        srv = ThreadingHTTPServer((STATE["bind"], port), Handler)
    except OSError as e:
        return None, "端口 %d 起不来：%s" % (port, e)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, None


def start_api(port):
    """切换通信端口并关闭旧服务。"""
    cur = STATE["api_port"]
    if cur == port and cur in STATE["servers"]:
        return None
    if port == STATE["discovery_port"]:
        return "通信口不能和握手口相同（%d）" % port
    srv, err = _serve(port)
    if err:
        return err
    if cur and cur in STATE["servers"]:
        old = STATE["servers"].pop(cur)
        threading.Thread(target=old.shutdown, daemon=True).start()
        print("[agent] 通信口 %d 已关闭" % cur, flush=True)
    STATE["servers"][port] = srv
    STATE["api_port"] = port
    print("[agent] 通信口已开：%d" % port, flush=True)
    return None


def start_cam_viewer():
    """启动共享内存预览服务；失败不阻止采集。"""
    script = STATE["cfg"].get("cam_viewer_script") or os.path.join(
        os.path.dirname(BASE), "cam", "camera_shm_viewer.py")
    if not os.path.exists(script):
        print("[agent] 没有 camera_shm_viewer.py，画面不可用", flush=True)
        return {"ok": False, "error": "脚本不存在：%s" % script}
    port = str(STATE["cfg"].get("cam_viewer_port") or 5000)
    cmd = [sys.executable, script, "--auto", "--port", port]
    r = runner.spawn(STATE["cam_key"], "camera_shm_viewer", cmd,
                     cwd=os.path.dirname(script))
    if r.get("ok"):
        print("[agent] 相机预览已启动 :%s" % port, flush=True)
    else:
        print("[agent] 相机预览启动失败：%s" % r.get("error"), flush=True)
    return r


def stop_capture(cfg):
    """结束录制并释放预览读取进程，保留相机驱动与机械臂服务。"""
    result = runner.stop_capture(cfg)
    if result.get("ok"):
        cam = runner.stop(STATE["cam_key"]) or {"ok": True}
        result.setdefault("steps", []).append({"step": "stop_camera_preview", **cam})
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = "dm-agent/" + VERSION
    protocol_version = "HTTP/1.1"


    def log_message(self, fmt, *args):

        if self.command != "GET" or "/api/health" not in self.path:
            sys.stderr.write("[agent] %s %s\n" % (self.address_string(), fmt % args))

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "X-DM-Token, Content-Type")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _ok(self, data=None):
        self._json(200, {"ok": True, "data": data if data is not None else {}})

    def _err(self, code, message):
        self._json(code, {"ok": False, "code": code, "message": message})

    def _authorized(self):
        expected = str(STATE["cfg"].get("agent_token") or "")
        supplied = self.headers.get("X-DM-Token", "")
        return bool(expected and supplied and hmac.compare_digest(expected, supplied))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            obj = json.loads(raw.decode("utf-8") or "{}")
        except Exception:               # noqa: BLE001
            return {}
        return obj if isinstance(obj, dict) else {}


    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "X-DM-Token, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        parts = [unquote(p) for p in u.path.strip("/").split("/") if p]

        if parts[:1] != ["api"]:
            return self._err(404, "前缀应为 /api")
        rest = parts[1:]
        head = rest[0] if rest else ""


        if head in ("hello", "health"):
            return self._ok(identity())
        if head != "cam" and not self._authorized():
            return self._err(401, "采集端请求未授权")

        try:
            return self._route(rest, q)
        except ValueError as e:
            return self._err(403, str(e))
        except cam.CamUnavailable as e:
            return self._err(503, str(e))
        except Exception as e:          # noqa: BLE001
            return self._err(500, "%s: %s" % (type(e).__name__, e))

    def _route(self, rest, q):
        head = rest[0] if rest else ""

        if head == "host":
            config = effective_cfg()
            config["dds_password_set"] = bool(config.pop("dds_password", ""))
            return self._ok({
                "config": config,
                "device_id": STATE["device_id"],
                "port": STATE["api_port"],
                "host": socket.gethostname(),
                "arch": os.uname().machine,
                "data_root": data_root(),
                "disk": disk_free(),
                "cam_upstream": cam.upstream(),
            })

        if head == "run" and len(rest) >= 2 and rest[1] == "status":
            return self._ok(runner.status(STATUS_PATTERNS, effective_cfg()))

        if head == "system":
            return self._ok(system.snapshot())

        if head == "latency":
            return self._ok(system.gateway_latency())

        if head == "cam" and len(rest) >= 2:
            what = rest[1]
            key = rest[2] if len(rest) >= 3 else ""
            if what == "health":
                return self._ok(cam.health())
            if what == "snap":
                body, ctype = cam.snapshot(key or "wrist")
                return self._bytes(200, body, ctype)
            if what == "stream":
                return self._stream(cam.open_stream(key or "wrist"))
            return self._err(404, "未知相机接口：%s" % what)

        if head == "files":
            what = rest[1] if len(rest) >= 2 else ""
            if what == "list":
                want_counts = (q.get("counts") or ["0"])[0] in ("1", "true", "yes")
                return self._ok(self._list((q.get("dir") or [""])[0], counts=want_counts))
            if what == "archive-status":
                return self._ok(archive_status((q.get("dir") or [""])[0]))
            if what == "archive":
                return self._archive((q.get("dir") or [""])[0])
            if what == "get":
                return self._file((q.get("path") or [""])[0])
            return self._err(404, "未知文件接口：%s" % what)

        return self._err(404, "未知路径：/%s" % "/".join(rest))
    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        parts = [unquote(p) for p in u.path.strip("/").split("/") if p]
        rest = parts[1:]
        head = rest[0] if rest else ""

        if head == "assign":
            body = self._body()
            expected = str(STATE["cfg"].get("agent_token") or "")
            if expected:
                if not self._authorized():
                    return self._err(401, "采集端请求未授权")

            try:
                port = int(body.get("port") or 0)
            except (TypeError, ValueError):
                return self._err(400, "port 必须是整数")
            if not (1024 <= port <= 65535):
                return self._err(400, "port 超出可用范围（1024-65535）")
            if not expected:
                token = str(body.get("agent_token") or "")
                if len(token) < 32:
                    return self._err(400, "首次配对需要至少 32 字符的令牌")
                try:
                    save_cfg({"agent_token": token}, allow_agent_token=True)
                except Exception as e:  # noqa: BLE001
                    return self._err(500, "保存配对令牌失败：%s" % e)
            err = start_api(port)
            if err:
                return self._err(500, err)
            return self._ok({"api_port": port, "device_id": STATE["device_id"]})

        if not self._authorized():
            return self._err(401, "采集端请求未授权")

        if head == "files" and len(rest) >= 2 and rest[1] == "rmdir":
            body = self._body()
            return self._rmdir(body.get("dir"))


        if head == "files" and len(rest) >= 2 and rest[1] == "rm":
            body = self._body()
            return self._rmfile(body.get("dir"), body.get("name"))

        if head == "files" and len(rest) >= 2 and rest[1] == "archive":
            body = self._body()
            try:
                return self._ok(start_archive(body.get("dir") or ""))
            except (ValueError, OSError) as e:
                return self._err(400, str(e))

        if head == "files" and len(rest) >= 2 and rest[1] == "archive-invalidate":
            body = self._body()
            try:
                invalidate_archive(body.get("dir") or "")
                return self._ok({"state": "missing"})
            except (ValueError, OSError) as e:
                return self._err(400, str(e))


        if head == "config":
            body = self._body()
            cfg = body.get("cfg")
            if not isinstance(cfg, dict):
                return self._err(400, "cfg 必须是对象")
            try:
                r = save_cfg(cfg)
            except Exception as e:          # noqa: BLE001
                return self._err(500, "配置写回失败：%s" % e)
            return self._ok(r)

        if parts[:2] != ["api", "run"]:
            return self._err(404, "只支持 POST /api/run/{action}、/api/assign 和 /api/config")

        action = parts[2] if len(parts) >= 3 else ""
        body = self._body()
        try:
            cfg = merge_cfg(body.get("cfg"))
        except (ValueError, OSError) as e:
            return self._err(400, str(e))

        if action == "cam-start":
            r = start_cam_viewer()
        elif action == "cam-stop":
            r = runner.stop(STATE["cam_key"]) or {"ok": True}
        elif action == "capture-stop":
            r = stop_capture(cfg)
        elif action in ACTIONS:
            try:
                r = ACTIONS[action](cfg)
            except Exception as e:      # noqa: BLE001
                return self._err(500, "%s: %s" % (type(e).__name__, e))
        else:
            return self._err(404, "未知动作：%s" % (action or "(空)"))

        if not isinstance(r, dict):
            r = {"result": r}
        r.setdefault("ok", True)
        r["action"] = action
        r["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return self._ok(r)


    def _bytes(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream(self, opened):
        """流式转发 MJPEG，使用连接关闭标识响应结束。"""
        resp, ctype = opened
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        reader = getattr(resp, "read1", None) or resp.read
        try:
            while True:
                chunk = reader(16384)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                resp.close()
            except Exception:           # noqa: BLE001
                pass


    def _list(self, rel, counts=False):
        """列出目录；counts 指定是否统计各子目录的 HDF5 文件。"""
        p = safe_path(rel)
        if not os.path.isdir(p):
            raise ValueError("不是目录：%s" % (rel or "(根)"))
        entries = []
        with os.scandir(p) as it:
            for e in it:
                try:
                    st = e.stat()
                except OSError:
                    continue
                item = {
                    "name": e.name,
                    "dir": e.is_dir(),
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "mtime_ns": st.st_mtime_ns,
                }
                if counts and item["dir"]:
                    n = 0
                    try:
                        with os.scandir(e.path) as sub:
                            for f in sub:
                                if f.name.endswith((".hdf5", ".h5")) and f.is_file():
                                    n += 1
                    except OSError:
                        n = 0
                    item["hdf5"] = n
                entries.append(item)
        entries.sort(key=lambda x: (not x["dir"], x["name"]))
        return {"dir": _rel(p), "entries": entries}

    def _rmfile(self, rel, name):
        """删除数据根目录内的指定文件。"""
        name = str(name or "").strip()
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            return self._err(400, "文件名无效")
        try:
            d = safe_path(rel)
        except ValueError as e:
            return self._err(403, str(e))
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            return self._err(404, "文件不存在：%s" % name)
        try:
            size = os.path.getsize(p)
            os.remove(p)
        except Exception as e:                      # noqa: BLE001
            return self._err(500, "删除失败：%s" % e)
        return self._ok({"deleted": name, "bytes": size})

    def _rmdir(self, rel):
        try:
            p = safe_path(rel)
        except ValueError as e:
            return self._err(403, str(e))
        root = os.path.abspath(data_root())
        if os.path.abspath(p) == root:
            return self._err(400, "不允许删除数据根目录")
        if not os.path.isdir(p):
            return self._err(400, "不是目录：%s" % (rel or "(根)"))
        n_files, total = 0, 0
        for base, _dirs, files in os.walk(p):
            for fn in files:
                n_files += 1
                try:
                    total += os.path.getsize(os.path.join(base, fn))
                except OSError:
                    pass
        try:
            shutil.rmtree(p)
        except Exception as e:                      # noqa: BLE001
            return self._err(500, "删除失败：%s" % e)
        return self._ok({"deleted": _rel(p), "files": n_files, "bytes": total})

    def _archive(self, rel):
        try:
            p = archive_path(rel)
            status = archive_status(rel)
        except ValueError as e:
            return self._err(400, str(e))
        if status["state"] != "ready":
            return self._err(409, "压缩包尚未就绪：%s" % status["state"])
        size = os.path.getsize(p)
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", 'attachment; filename="data.tar.gz"')
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        sent = 0
        try:
            with open(p, "rb") as f:
                while True:
                    chunk = f.read(262144)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        sys.stderr.write("[agent] archive download %s -> %d bytes\n" % (rel, sent))

    def _file(self, rel):
        p = safe_path(rel)
        if not os.path.isfile(p):
            return self._err(404, "文件不存在：%s" % rel)
        size = os.path.getsize(p)
        ctype = "application/octet-stream"
        if p.lower().endswith((".jpg", ".jpeg")):
            ctype = "image/jpeg"
        elif p.lower().endswith(".png"):
            ctype = "image/png"
        elif p.lower().endswith(".json"):
            ctype = "application/json; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with open(p, "rb") as f:
                while True:
                    chunk = f.read(262144)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    ap = argparse.ArgumentParser(description="deepmaster 采集端代理")
    ap.add_argument("--config", default=os.environ.get("DM_AGENT_CONFIG")
                    or "/opt/dm-collect/host_config.json")
    ap.add_argument("--discovery-port", type=int,
                    default=int(os.environ.get("DM_AGENT_DISCOVERY_PORT") or 5111),
                    help="固定握手口")
    ap.add_argument("--port", type=int, default=int(os.environ.get("DM_AGENT_PORT") or 0),
                    help="通信口；0 = 不预开，等服务器 /api/assign 下发")
    ap.add_argument("--bind", default=os.environ.get("DM_AGENT_BIND") or "0.0.0.0")
    ap.add_argument("--data-root", default=os.environ.get("DM_DATA_DIR") or "")
    ap.add_argument("--no-cam", action="store_true", help="不自动拉起相机预览")
    args = ap.parse_args()

    STATE["cfg"] = load_cfg(args.config)
    STATE["cfg_path"] = args.config or ""
    STATE["bind"] = args.bind
    STATE["discovery_port"] = args.discovery_port
    STATE["device_id"] = make_device_id()

    root = args.data_root or STATE["cfg"].get("local_data_dir") or ""
    if not root:
        root = os.path.join(os.path.dirname(os.path.abspath(args.config or BASE)), "data")
    root = resolve_data_root(root)
    os.makedirs(root, exist_ok=True)
    STATE["data_root"] = root
    STATE["cfg"]["local_data_dir"] = root

    print("[agent] 设备: %s  (%s/%s)" % (STATE["device_id"], socket.gethostname(),
                                        os.uname().machine), flush=True)
    print("[agent] 配置: %s（%d 项）" % (args.config, len(STATE["cfg"])), flush=True)
    print("[agent] 数据: %s" % STATE["data_root"], flush=True)

    if not args.no_cam:
        start_cam_viewer()


    try:
        disc = ThreadingHTTPServer((args.bind, args.discovery_port), Handler)
    except OSError as e:
        print("[✗] 握手口 %d 起不来：%s" % (args.discovery_port, e), flush=True)
        sys.exit(1)
    disc.daemon_threads = True
    STATE["servers"][args.discovery_port] = disc

    if args.port:
        err = start_api(args.port)
        if err:
            print("[!] 预开通信口失败：%s（仍可用 /api/assign 下发）" % err, flush=True)

    def _bye(signum, _frame):
        print("[agent] 收到信号 %s，退出" % signum, flush=True)

        try:
            runner.stop(STATE["cam_key"])
        except Exception:               # noqa: BLE001
            pass


        for srv in list(STATE["servers"].values()):
            threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)

    print("[agent] 握手口就绪 http://%s:%d/api/hello   通信口: %s"
          % (args.bind, args.discovery_port,
             args.port or "等服务器下发"), flush=True)
    disc.serve_forever()


if __name__ == "__main__":
    main()

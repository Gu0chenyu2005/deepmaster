# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""业务接口：鉴权、采集端通信、数据审核与导出。"""

import hashlib
import json
import os
import re
import shutil
import secrets
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, parse_qs, quote, unquote
from urllib.request import Request, urlopen

import h5py
import numpy as np

import auth
import db
import runner
import geo
import system


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import auto_qc
except Exception:          # noqa: BLE001
    auto_qc = None


try:
    import cam
except Exception:          # noqa: BLE001
    cam = None

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TOOL_DIR)


APP_VERSION = "1.0.0"
DEFAULT_DATA = os.environ.get("DM_DATA_DIR", os.path.join(PROJECT_DIR, "datasets"))
_pos = [a for a in sys.argv[1:] if not a.startswith("-")]
INIT_DATA = os.path.abspath(_pos[0]) if _pos else DEFAULT_DATA
PORT = int(_pos[1]) if len(_pos) > 1 else 8765

ALLOW_ROOTS = [os.path.abspath(INIT_DATA), os.path.dirname(os.path.abspath(INIT_DATA)),
               os.path.abspath(DEFAULT_DATA)]


_DATA_DIR_NAMES = {os.path.basename(os.path.abspath(p).rstrip("/"))
                   for p in (INIT_DATA, DEFAULT_DATA)} - {""}
EP_RE = re.compile(r"episode_(\d+)_vla_valid\.hdf5$")
AGENT_URL_ENV = (os.environ.get("DM_AGENT_URL") or "").strip().rstrip("/")
DISCOVERY_PORT = int(os.environ.get("DM_AGENT_PORT") or 5111)
PORT_POOL_START = int(os.environ.get("DM_AGENT_PORT_START") or 5300)
PORT_POOL_END = 65535

_AGENT_CACHE = {"ts": 0.0, "url": ""}
_AGENT_TTL = 3.0


def agent_url(force=False):
    """返回采集端地址，优先使用数据库配置，其次为环境变量；缓存 3 秒。"""
    now = time.time()
    if not force and (now - _AGENT_CACHE["ts"]) < _AGENT_TTL:
        return _AGENT_CACHE["url"]
    url = ""
    try:
        with db.connect() as conn:
            url = (db.get_setting(conn, "agent_url", "") or "").strip()
    except Exception:                   # noqa: BLE001
        pass
    if not url:
        url = AGENT_URL_ENV
    url = (url or "").rstrip("/")
    _AGENT_CACHE.update(ts=now, url=url)
    return url


def agent_on():
    return bool(agent_url())


class AgentDown(Exception):
    """采集端连接不可用。"""


class AgentError(Exception):
    """采集端请求执行失败。"""


def _agent_open(path, payload=None, timeout=30, base=None, auth_token=None):
    """打开采集端响应；调用方负责读取与关闭。"""
    url = base or agent_url()
    if not url:
        raise AgentDown("还没有连接采集端")
    full = url + path
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"} if data is not None else {}
    token = auth_token or _agent_token(url)
    if token:
        headers["X-DM-Token"] = token
    req = Request(full, data=data, headers=headers,
                  method="POST" if data is not None else "GET")
    try:
        return urlopen(req, timeout=timeout)
    except HTTPError as e:
        try:
            obj = json.loads(e.read() or b"{}")
        except Exception:               # noqa: BLE001
            obj = {}
        raise AgentError(obj.get("message") or ("采集端返回 HTTP %s" % e.code))
    except URLError as e:
        raise AgentDown("连不上采集端 %s：%s" % (url, getattr(e, "reason", e)))
    except OSError as e:
        raise AgentDown("连不上采集端 %s：%s" % (url, e))


def _agent_call(path, payload=None, timeout=30, base=None, auth_token=None):
    """返回采集端响应数据；失败抛出 AgentDown 或 AgentError。"""
    resp = _agent_open(path, payload, timeout=timeout, base=base,
                       auth_token=auth_token)
    try:
        raw = resp.read()
    finally:
        try:
            resp.close()
        except Exception:               # noqa: BLE001
            pass
    try:
        obj = json.loads(raw or b"{}")
    except Exception:                   # noqa: BLE001
        raise AgentError("采集端返回的不是 JSON")
    if not obj.get("ok", True):
        raise AgentError(obj.get("message") or "采集端执行失败")
    return obj.get("data") or {}


def _dev_registry():
    try:
        with db.connect() as conn:
            raw = db.get_setting(conn, "agent_devices", "") or "{}"
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:                   # noqa: BLE001
        return {}


def _dev_save(reg):
    with db.connect() as conn:
        db.set_setting(conn, "agent_devices", json.dumps(reg, ensure_ascii=False))


def _agent_token(url=None):
    target = urlparse(url or agent_url())
    host = (target.hostname or "").lower()
    port = target.port or 80
    for info in _dev_registry().values():
        try:
            matches = (str(info.get("ip") or "").lower() == host and
                       int(info.get("port") or 0) == port)
        except (TypeError, ValueError):
            matches = False
        if matches:
            return str(info.get("token") or "")
    return ""


def _pick_port(reg, device_id, skip=()):
    """优先复用已分配端口，否则查找可用端口并跳过 skip。"""
    used = {int(v.get("port") or 0) for k, v in reg.items() if k != device_id}
    used |= {int(p) for p in skip}
    old = int((reg.get(device_id) or {}).get("port") or 0)
    if old and old not in used:
        return old
    for p in range(PORT_POOL_START, PORT_POOL_END + 1):
        if p not in used:
            return p
    raise err400("通信端口已用尽（%d-65535 全被占用）" % PORT_POOL_START)


def _local_subnets(limit=6):
    """枚举非虚拟网卡的 IPv4 /24 网段。"""
    skip = ("docker", "veth", "br-", "virbr", "vmnet", "tun", "tap", "utun",
            "wg", "zerotier", "tailscale", "lo")
    nets = []
    try:
        p = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=4)
        txt = (p.stdout or b"").decode("utf-8", "replace")
    except Exception:                   # noqa: BLE001
        return nets
    for line in txt.splitlines():
        parts = line.split()
        if "inet" not in parts or len(parts) < 4:
            continue
        ifname = parts[1].split("@")[0]
        if any(ifname.startswith(s) for s in skip):
            continue
        cidr = parts[parts.index("inet") + 1]
        ip, _, plen = cidr.partition("/")
        try:
            plen_i = int(plen or 24)
        except ValueError:
            plen_i = 24
        if plen_i < 24:
            plen_i = 24
        prefix = ".".join(ip.split(".")[:3])
        if prefix not in nets:
            nets.append(prefix)
        if len(nets) >= limit:
            break
    return nets


def _probe_agent(ip, port=None, timeout=0.25):
    """探测 TCP 端口并校验采集端身份。"""
    port = port or DISCOVERY_PORT
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            pass
    except OSError:
        return None
    try:
        with urlopen("http://%s:%d/api/hello" % (ip, port), timeout=2.0) as r:
            obj = json.loads(r.read() or b"{}")
    except Exception:                   # noqa: BLE001
        return None
    d = obj.get("data") or {}
    if d.get("role") != "collector":
        return None
    return {
        "ip": ip,
        "discovery_port": port,
        "device_id": d.get("device_id") or "",
        "api_port": int(d.get("api_port") or 0),
        "host": d.get("host"),
        "arch": d.get("arch"),
        "version": d.get("version"),
        "data_root": d.get("data_root"),
        "disk": d.get("disk"),
        "uptime_s": d.get("uptime_s"),
        "configured": bool(d.get("configured")),
        "time": d.get("time"),
    }


def op_agent_scan(body=None, user=None):
    """通过固定握手端口发现局域网采集端。"""
    body = body if isinstance(body, dict) else {}
    try:
        port = int(body.get("port") or DISCOVERY_PORT)
    except (TypeError, ValueError):
        port = DISCOVERY_PORT

    subnets = _local_subnets()
    targets = []
    cur = agent_url()
    m = re.match(r"https?://([0-9.]+)", cur or "")
    if m:
        targets.append(m.group(1))
    targets.append("127.0.0.1")         
    for prefix in subnets:
        for i in range(1, 255):
            targets.append("%s.%d" % (prefix, i))
    targets = list(dict.fromkeys(targets))

    t0 = time.time()
    found = []
    with ThreadPoolExecutor(max_workers=128) as ex:
        for r in ex.map(lambda ip: _probe_agent(ip, port), targets):
            if r:
                found.append(r)


    reg = _dev_registry()
    cur_ip = ""
    mcur = re.match(r"https?://([0-9.]+)", cur or "")
    if mcur:
        cur_ip = mcur.group(1)

    uniq = {}
    for d in found:
        key = d.get("device_id") or (d.get("host"), d.get("data_root"))
        old = uniq.get(key)
        if old is None:
            uniq[key] = d
            continue
        score = (1 if d["ip"] == cur_ip else 0) + (0 if d["ip"].startswith("127.") else 1)
        oscore = (1 if old["ip"] == cur_ip else 0) + (0 if old["ip"].startswith("127.") else 1)
        if score > oscore:
            uniq[key] = d

    devices = []
    for d in uniq.values():
        info = reg.get(d["device_id"]) or {}
        assigned = int(info.get("port") or 0)
        d["assigned_port"] = assigned

        d["current"] = (bool(cur) and d["ip"] == cur_ip and assigned > 0
                        and assigned == int(d.get("api_port") or 0))
        d["connected"] = assigned > 0 and assigned == int(d.get("api_port") or 0)
        devices.append(d)
    devices.sort(key=lambda x: (not x["current"], not x["connected"], x["ip"]))

    if user:
        with db.connect() as conn:
            db.audit(conn, user["account"], "agent_scan", "port=%d" % port,
                     None if devices else "未发现设备")
    return {
        "devices": devices,
        "scanned": len(targets),
        "subnets": subnets,
        "port": port,
        "current": cur,
        "took_ms": int((time.time() - t0) * 1000),
    }


def op_agent_connect(body, user=None):
    """分配通信端口，等待采集端就绪并保存设备信息。"""
    if not isinstance(body, dict):
        raise err400("参数必须是对象")
    ip = str(body.get("ip") or "").strip()
    if not re.match(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}$", ip):
        raise err400("ip 格式不对")
    try:
        dport = int(body.get("discovery_port") or DISCOVERY_PORT)
    except (TypeError, ValueError):
        dport = DISCOVERY_PORT

    hello = _probe_agent(ip, dport, timeout=2.0)
    if not hello:
        raise err400("这台机器上没有可用的采集端（%s:%d 握手失败）" % (ip, dport))
    dev_id = hello.get("device_id") or ""
    if not dev_id:
        raise err400("采集端没有报 device_id，版本太旧？")

    reg = _dev_registry()
    old = reg.get(dev_id) or {}
    token = str(old.get("token") or secrets.token_urlsafe(36))
    if not old.get("token"):
        pending = dict(old, ip=ip, port=0, discovery_port=dport, token=token)
        reg[dev_id] = pending
        try:
            _dev_save(reg)
        except Exception as e:          # noqa: BLE001
            raise err400("无法保存采集端配对信息：%s" % e)
    tried = []
    port = 0
    info = {}

    for _attempt in range(5):
        port = _pick_port(reg, dev_id, skip=tried)
        tried.append(port)


        try:
            _agent_call("/api/assign", {"port": port, "agent_token": token},
                        timeout=8, base="http://%s:%d" % (ip, dport),
                        auth_token=token if old.get("token") or _attempt else None)
        except (AgentDown, AgentError) as e:
            if (_attempt == 0 and not old.get("token") and
                    isinstance(e, AgentError) and "端口" in str(e) and "起不来" in str(e)):
                continue
            raise err400("配对或下发通信端口失败：%s" % e)


        info = {}
        for _ in range(30):
            try:
                info = _agent_call("/api/hello", timeout=1.5,
                                   base="http://%s:%d" % (ip, port),
                                   auth_token=token)
                if int(info.get("api_port") or 0) == port:
                    break
            except (AgentDown, AgentError):
                pass
            info = {}
            time.sleep(0.2)
        if info:
            break

    if not info:
        raise err400("设备没有在端口 %d 上就绪（试过 %s）"
                     % (port, "/".join(str(p) for p in tried)))
    if not info.get("auth_enabled"):
        raise err400("采集端版本不支持安全配对，请先更新采集端包")
    hello.update({k: v for k, v in info.items() if v not in (None, "")})

    url = "http://%s:%d" % (ip, port)
    reg[dev_id] = dict(reg.get(dev_id) or {}, **{
        "ip": ip, "port": port, "discovery_port": dport,
        "host": hello.get("host"), "arch": hello.get("arch"),
        "token": token, "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    _dev_save(reg)

    with db.connect() as conn:
        db.set_setting(conn, "agent_url", url)
        db.audit(conn, user["account"] if user else None, "agent_connect", url)
    agent_url(force=True)

    return {"url": url, "device_id": dev_id, "port": port,
            "device": dict(hello, api_port=port, url=url, connected=True, current=True)}

def op_agent_select(body, user=None):

    if not isinstance(body, dict):
        raise err400("参数必须是对象")
    url = str(body.get("url") or "").strip().rstrip("/")
    if url and not re.match(r"^https?://[0-9A-Za-z_.\-]+(:\d+)?$", url):
        raise err400("地址格式不对，应该像 http://192.168.31.20:5300")
    with db.connect() as conn:
        db.set_setting(conn, "agent_url", url)
        db.audit(conn, user["account"] if user else None, "agent_select", url or "(本机)")
    agent_url(force=True)
    return {"url": url}


PULL_KEY = "last_pull"          
def _pull_archive(rel, account=None, on_progress=None, on_state=None):

    raw = str(rel or "").strip()
    if not raw.strip("/"):
        raise err400("要指定 dir（采集端上的目录，相对数据根）")
    if not agent_url():
        raise err400("当前没有连接采集端 —— 先在「设备状态」里连接一台机器狗")
    root = os.path.realpath(INIT_DATA)
    os.makedirs(root, exist_ok=True)
    abs_p = raw if os.path.isabs(raw) else os.path.abspath(os.path.join(PROJECT_DIR, raw))
    abs_p = os.path.realpath(abs_p)
    if abs_p == root:
        raise err400("不能回传整个数据根")
    rel = abs_p[len(root) + 1:] if abs_p.startswith(root + os.sep) else raw.strip("/")
    target = os.path.realpath(os.path.join(root, rel))
    if not target.startswith(root + os.sep):
        raise err400("目录不合法：%s" % rel)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    remote_rel = _agent_rel(rel)

    deadline = time.monotonic() + 1800
    while True:
        state = _agent_call("/api/files/archive-status?dir=" + quote(remote_rel),
                            timeout=15)
        phase = state.get("state") or "missing"
        if phase == "ready":
            break
        if phase == "error":
            raise AgentError("采集端压缩失败：%s" % (state.get("error") or "未知错误"))
        if phase == "missing":
            raise AgentError("采集端没有该目录的压缩包，请结束采集后等待压缩完成")
        if time.monotonic() >= deadline:
            raise AgentError("等待采集端压缩超时")
        if on_state:
            on_state("compressing")
        time.sleep(2)

    work = tempfile.mkdtemp(prefix=".dm-upload-", dir=parent)
    archive = os.path.join(work, "payload.tar.gz")
    tree = os.path.join(work, "tree")
    os.makedirs(tree)
    received = files = total = 0
    try:
        resp = _agent_open("/api/files/archive?dir=" + quote(remote_rel), timeout=1800)
        try:
            length = int(resp.headers.get("Content-Length") or 0)
            with open(archive, "wb") as out:
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    out.write(chunk)
                    received += len(chunk)
                    if on_progress:
                        on_progress(received, length)
        finally:
            resp.close()

        with tarfile.open(archive, mode="r:gz") as tf:
            for member in tf:
                name = member.name.replace("\\", "/")
                while name.startswith("./"):
                    name = name[2:]
                parts = [p for p in name.split("/") if p not in ("", ".")]
                if not parts:
                    continue
                if name.startswith("/") or any(p == ".." for p in parts):
                    raise AgentError("压缩包包含越界路径")
                dest = os.path.realpath(os.path.join(tree, *parts))
                if not dest.startswith(tree + os.sep):
                    raise AgentError("压缩包包含越界路径")
                if member.isdir():
                    os.makedirs(dest, exist_ok=True)
                    continue
                if not member.isfile():
                    raise AgentError("压缩包包含链接或特殊文件")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                source = tf.extractfile(member)
                if source is None:
                    raise AgentError("压缩包文件读取失败：%s" % member.name)
                with source, open(dest, "wb") as out:
                    shutil.copyfileobj(source, out, 1024 * 1024)
                files += 1
                total += member.size or 0

        os.makedirs(target, exist_ok=True)
        for base, dirs, names in os.walk(tree):
            relbase = os.path.relpath(base, tree)
            outbase = target if relbase == "." else os.path.join(target, relbase)
            os.makedirs(outbase, exist_ok=True)
            for dirname in dirs:
                os.makedirs(os.path.join(outbase, dirname), exist_ok=True)
            for filename in names:
                os.replace(os.path.join(base, filename), os.path.join(outbase, filename))
    except (tarfile.TarError, OSError) as e:
        raise AgentError("接收或解压压缩包失败：%s" % e)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    with db.connect() as conn:
        db.audit(conn, account, "pull", rel, None if files else "空目录")
    return {"dir": rel, "files": files, "bytes": total, "archive_bytes": received,
            "dest": target}


def op_pull(body, user=None):
    """手动接收采集端已压缩的归档。"""
    return _pull_archive((body or {}).get("dir"), user["account"] if user else None)


def _save_pull_state(**kw):
    try:
        with db.connect() as conn:
            db.set_setting(conn, PULL_KEY, json.dumps(kw, ensure_ascii=False))
    except Exception:                   # noqa: BLE001
        pass


def op_pull_state():
    """返回最近一次数据回传状态。"""
    try:
        with db.connect() as conn:
            raw = db.get_setting(conn, PULL_KEY, "") or ""
        return json.loads(raw) if raw else {}
    except Exception:                   # noqa: BLE001
        return {}


UPLOAD_KEY = "upload_job"
UPLOAD_DONE_KEY = "upload_done"     
_UPLOAD = {"running": False, "dirs": [], "current": -1, "started_at": None,
           "ended_at": None, "account": None, "error": None}
_upload_lock = threading.Lock()


_UPLOAD_DONE = None
_upload_done_lock = threading.RLock()


def upload_busy():

    return bool(_UPLOAD.get("running"))

def op_folder_upload_state():
    with _upload_lock:
        snap = json.loads(json.dumps(_UPLOAD, ensure_ascii=False))
    snap["done"] = upload_done_map()
    return snap


def _upload_done_load():
    """读取已上传目录映射，首次加载后使用内存缓存。"""
    global _UPLOAD_DONE
    with _upload_done_lock:
        if _UPLOAD_DONE is not None:
            return _UPLOAD_DONE
        m = {}
        try:
            with db.connect() as conn:
                raw = db.get_setting(conn, UPLOAD_DONE_KEY, "") or ""
            if raw:
                v = json.loads(raw)
                if isinstance(v, dict):
                    m = v
        except Exception:               # noqa: BLE001
            m = {}
        _UPLOAD_DONE = m
        return _UPLOAD_DONE


def _upload_done_save():
    with _upload_done_lock:
        blob = json.dumps(_upload_done_load(), ensure_ascii=False)
    try:
        with db.connect() as conn:
            db.set_setting(conn, UPLOAD_DONE_KEY, blob)
    except Exception:                   # noqa: BLE001
        pass


def upload_done_map():
    with _upload_done_lock:
        return json.loads(json.dumps(_upload_done_load(), ensure_ascii=False))


def _upload_mark_done(rel, files, nbytes):
    with _upload_done_lock:
        _upload_done_load()[rel] = {"at": db.now(), "files": int(files or 0),
                                    "bytes": int(nbytes or 0)}
    _upload_done_save()


def upload_forget(rel):
    """录制目录变更或删除时，撤销已上传标记。"""
    rel = str(rel or "").strip("/")
    if not rel:
        return
    with _upload_done_lock:
        m = _upload_done_load()
        hit = [k for k in m if k == rel or k.startswith(rel + "/")]
        for k in hit:
            m.pop(k, None)
    if hit:
        _upload_done_save()


def _upload_save_state():

    with _upload_lock:
        blob = json.dumps(_UPLOAD, ensure_ascii=False)
    try:
        with db.connect() as conn:
            db.set_setting(conn, UPLOAD_KEY, blob)
    except Exception:                   # noqa: BLE001
        pass


def _folder_upload_worker(dirs, account):
    for i, item in enumerate(dirs):
        with _upload_lock:
            item["state"] = "wait"
            item["total"] = 0
            _UPLOAD["current"] = i
        _upload_save_state()
        try:
            def _state(state, _item=item):
                with _upload_lock:
                    _item["state"] = state
                _upload_save_state()

            def _prog(recv, total, _item=item):
                with _upload_lock:
                    _item["state"] = "running"
                    _item["recv"] = recv
                    _item["total"] = total

            r = _pull_archive(item["dir"], account, on_progress=_prog,
                              on_state=_state)
            with _upload_lock:
                item["state"] = "done"
                item["files"] = r.get("files", 0)
                item["bytes"] = r.get("bytes", 0)
                item["recv"] = r.get("bytes", 0)

            _upload_mark_done(item["dir"], r.get("files", 0), r.get("bytes", 0))
        except Exception as e:          # noqa: BLE001
            with _upload_lock:
                item["state"] = "error"
                item["error"] = str(e)
        _upload_save_state()

    with _upload_lock:
        _UPLOAD["running"] = False
        _UPLOAD["current"] = -1
        _UPLOAD["ended_at"] = db.now()
        failed = [d["dir"] for d in _UPLOAD["dirs"] if d.get("state") == "error"]
        if failed:
            _UPLOAD["error"] = "失败：%s" % "、".join(failed)
    _upload_save_state()
    try:
        with db.connect() as conn:
            db.audit(conn, account, "upload_done",
                     "、".join(d["dir"] for d in dirs),
                     ("失败：%s" % "、".join(failed)) if failed else None)
    except Exception:                   # noqa: BLE001
        pass


def op_folder_upload_start(body, user=None):

    dirs = (body or {}).get("dirs")
    if not isinstance(dirs, list) or not dirs:
        raise err400("要指定 dirs（录制文件夹列表，相对采集端数据根）")
    if not agent_on():
        raise err400("当前没有连接采集端 —— 先在「设备状态」里连接一台机器")
    if upload_busy():
        raise ApiError(409, "正在上传", status=409)

    clean = []
    for d in dirs:
        rel = _agent_rel(d)
        if not rel:
            raise err400("不能上传整个数据根")
        if rel not in clean:
            clean.append(rel)
    if not clean:
        raise err400("没有可上传的文件夹")

    account = user["account"] if user else None
    with _upload_lock:
        _UPLOAD.update({
            "running": True, "current": 0, "started_at": db.now(), "ended_at": None,
            "account": account, "error": None,
            "dirs": [{"dir": rel, "state": "wait", "files": 0, "bytes": 0,
                      "recv": 0, "total": 0, "error": None} for rel in clean],
        })
    _upload_save_state()
    try:
        with db.connect() as conn:
            db.audit(conn, account, "upload_start", "、".join(clean))
    except Exception:                   # noqa: BLE001
        pass
    threading.Thread(target=_folder_upload_worker,
                     args=(_UPLOAD["dirs"], account), daemon=True).start()
    return op_folder_upload_state()


REJECTED_SUFFIX = "_rejected"
SCHEMA_CANDIDATES = {

    "timestamps": [
        ("metadata/timestamps_ns", 1e9),          
        ("observations/timestamps", 1e-3),        
    ],
    "qpos":           ["observations/qpos"],
    "torque":         ["observations/torque", "observations/motor_torque"],
    "action":         ["action/action", "actions"],
    "commanded":      ["action/commanded", "actions_commanded"],
    "leg":            ["observations/leg_joint_pos"],
    "done":           ["metadata/done"],
}


CAMERA_CANDIDATES = [
    ("wrist",  "腕部相机",    ["observations/images/cam_d435",
                              "observations/d435i_rgb",
                              "observations/wrist_rgb"]),
    ("global", "头部相机",    ["observations/images/cam_d405_left",
                              "observations/rgb",
                              "observations/head_rgb"]),
    ("third",  "第三人称相机", ["observations/images/cam_d405_right",
                              "observations/third_rgb",
                              "observations/third_person_rgb"]),
]

ARM_NAMES = ["arm_joint_1", "arm_joint_2", "arm_joint_3", "arm_joint_4",
             "arm_joint_5", "arm_joint_6", "gripper"]
LEG_NAMES = ["fl_hip_roll", "fl_hip_pitch", "fl_knee", "fl_wheel",
             "fr_hip_roll", "fr_hip_pitch", "fr_knee", "fr_wheel",
             "rl_hip_roll", "rl_hip_pitch", "rl_knee", "rl_wheel",
             "rr_hip_roll", "rr_hip_pitch", "rr_knee", "rr_wheel"]
NOMINAL_FPS = 15.0


_schema_cache = {}

def detect_schema_cached(path):

    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        key = (path, 0)
    hit = _schema_cache.get(key)
    if hit is not None:
        return hit
    f = h5open(path)
    sch = detect_schema(f)
    _schema_cache[key] = sch

    if len(_schema_cache) > 64:
        for k in list(_schema_cache.keys())[:-32]:
            _schema_cache.pop(k, None)
    return sch


def detect_schema(f):

    def first(paths):
        for p in paths:
            if p in f:
                return p
        return None

    ts = None
    for path, scale in SCHEMA_CANDIDATES["timestamps"]:
        if path in f:
            ts = (path, scale)
            break

    cams = []
    for key, label, paths in CAMERA_CANDIDATES:
        ds = first(paths)
        if not ds:
            continue

        len_ds = ds + "_len"
        if len_ds not in f:

            alt = ds.rsplit("/", 1)
            if len(alt) == 2:
                cand = alt[0] + "/" + alt[1] + "_len"
                if cand in f:
                    len_ds = cand
        cams.append({"key": key, "label": label, "ds": ds,
                     "len_ds": len_ds if len_ds in f else None})

    fps = None
    try:
        if "fps" in f.attrs:
            fps = float(f.attrs["fps"])
    except Exception:
        pass

    return {
        "timestamps": ts,
        "cams": cams,
        "qpos_ds": first(SCHEMA_CANDIDATES["qpos"]),
        "torque_ds": first(SCHEMA_CANDIDATES["torque"]),
        "action_ds": first(SCHEMA_CANDIDATES["action"]),
        "commanded_ds": first(SCHEMA_CANDIDATES["commanded"]),
        "leg_ds": first(SCHEMA_CANDIDATES["leg"]),
        "done_ds": first(SCHEMA_CANDIDATES["done"]),
        "fps": fps,
    }


def schema_cameras(f):
    """返回数据文件中存在的相机通道。"""
    return detect_schema(f)["cams"]
MIN_FREE_BYTES = int(os.environ.get("DM_MIN_FREE_MB", "200")) * 1024 * 1024


def resolve_dir(raw):

    if not raw:
        return None
    raw = unquote(str(raw)).strip()
    cands = []
    if os.path.isabs(raw):
        cands.append(raw)
    else:
        cands.append(os.path.join(INIT_DATA, raw))
        cands.append(os.path.join(DEFAULT_DATA, raw))
        cands.append(os.path.join(PROJECT_DIR, raw))
    for c in cands:
        p = os.path.abspath(c)
        if not os.path.isdir(p):
            continue
        for root in ALLOW_ROOTS:
            if p == root or p.startswith(root + os.sep):
                return p
    return None


def rel_to_data(p):

    p = os.path.abspath(p)
    base = os.path.abspath(PROJECT_DIR)
    if p == base:
        return "."
    if p.startswith(base + os.sep):
        return os.path.relpath(p, base)
    return p


def list_episode_files(d):
    out = []
    try:
        names = os.listdir(d)
    except Exception:
        return out
    for name in names:
        m = EP_RE.match(name)
        if m:
            out.append((int(m.group(1)), os.path.join(d, name)))
    out.sort()
    return out


def list_records(d):
    """列出目录中的录制文件。"""
    out = []
    try:
        names = sorted(os.listdir(d))
    except Exception:
        return out
    for name in names:
        full = os.path.join(d, name)
        if not os.path.isfile(full):
            continue
        if not (name.endswith(".hdf5") or name.endswith(".h5")):
            continue
        try:
            st = os.stat(full)
            size, mtime = st.st_size, int(st.st_mtime)
            mtime_ns = st.st_mtime_ns
        except Exception:
            size, mtime, mtime_ns = 0, 0, 0
        out.append({"name": name, "size": size, "mtime": mtime,
                    "mtime_ns": mtime_ns})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


_h5_lock = threading.RLock()
_h5_cache = {}
_h5_order = []


def h5open(path):
    mtime = os.path.getmtime(path)
    with _h5_lock:
        hit = _h5_cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        if hit:
            try:
                hit[1].close()
            except Exception:
                pass
            del _h5_cache[path]
        f = h5py.File(path, "r")
        _h5_cache[path] = (mtime, f)
        _h5_order.append(path)
        while len(_h5_order) > 4:
            old = _h5_order.pop(0)
            ent = _h5_cache.pop(old, None)
            if ent:
                try:
                    ent[1].close()
                except Exception:
                    pass
        return f


def read_ds(path, key):
    with _h5_lock:
        f = h5open(path)
        if key not in f:
            return None
        return f[key][...]


class ApiError(Exception):
    def __init__(self, code, message, data=None, status=None):
        self.code = code
        self.message = message
        self.data = data or {}
        self.status = status or (400 if code < 500 else 500)
        super().__init__(message)


def err400(msg):
    return ApiError(400, msg, status=400)


def err401(msg="需要登录"):
    return ApiError(401, msg, status=401)


def err403(msg="权限不足"):
    return ApiError(403, msg, status=403)


def err404(msg="不存在"):
    return ApiError(404, msg, status=404)


def op_preflight(dir_raw, folder=None):

    base_raw = (dir_raw or "").strip() or rel_to_data(DEFAULT_DATA)

    base_abs = resolve_dir(base_raw)
    base_missing = False


    if base_abs is None:
        if os.path.isabs(base_raw):
            cand = os.path.abspath(base_raw)
        else:
            cand = os.path.abspath(os.path.join(PROJECT_DIR, base_raw))

        inside = False
        for root in ALLOW_ROOTS:
            root = os.path.abspath(root)
            if cand == root or cand.startswith(root + os.sep):
                inside = True
                break
        if not inside:
            raise err400(
                "数据目录不在允许范围：%s（允许根：%s）"
                % (cand, " , ".join(ALLOW_ROOTS)))


        parent = os.path.dirname(cand)
        probe_parent = parent
        while not os.path.isdir(probe_parent) and os.path.dirname(probe_parent) != probe_parent:
            probe_parent = os.path.dirname(probe_parent)
        if not os.path.isdir(probe_parent):
            raise err400("数据目录的上级不存在：%s" % parent)

        base_abs = cand
        base_missing = True

    target = base_abs
    if folder:
        safe = re.sub(r"[\/\\\x00]", "", folder).strip()
        if not safe:
            raise err400("文件夹名无效")
        target = os.path.join(base_abs, safe)


    probe = target
    while not os.path.isdir(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    du = shutil.disk_usage(probe)
    free = du.free

    exists = os.path.isdir(target)
    records = len(list_records(target)) if exists else 0

    warnings = []
    ok = True
    if free < MIN_FREE_BYTES:
        ok = False
        warnings.append("磁盘剩余 %.1f MB，低于阈值 %.0f MB"
                        % (free / 1048576.0, MIN_FREE_BYTES / 1048576.0))
    if exists and records > 0:
        warnings.append("目录已存在且已有 %d 个录制文件，继续会混在一起写" % records)


    if agent_on():
        try:
            h = _agent_call("/api/hello", timeout=3)
            d = h.get("disk") or {}
            if d.get("free"):
                free = int(d["free"])
                warnings = [w for w in warnings if "磁盘剩余" not in w]
                if free < MIN_FREE_BYTES:
                    ok = False
                    warnings.append("采集端磁盘剩余 %.1f MB，低于阈值 %.0f MB"
                                    % (free / 1048576.0, MIN_FREE_BYTES / 1048576.0))
        except (AgentDown, AgentError):
            pass

    return {
        "ok": ok,
        "dir_path": rel_to_data(target),
        "abs_path": target,
        "base_missing": base_missing,
        "exists": exists,
        "records": records,
        "free_bytes": free,
        "free_mb": round(free / 1048576.0, 1),
        "min_free_mb": MIN_FREE_BYTES // 1048576,
        "warnings": warnings,
    }


def _agent_rel(dir_raw):

    d = str(dir_raw or "").strip()
    if not d:
        return ""
    root = ""
    try:
        root = (_agent_call("/api/hello", timeout=3).get("data_root") or "").rstrip("/")
    except (AgentDown, AgentError):
        root = ""
    if os.path.isabs(d):
        if root and (d == root or d.startswith(root + os.sep)):
            d = d[len(root):]
        else:
            d = os.path.basename(d)
    else:


        head, sep, tail = d.strip("/").partition("/")
        if sep and head in _DATA_DIR_NAMES:
            d = tail
    return d.strip("/")


AUTO_FOLDER_RE = re.compile(r"^(\d{8})_(\d+)$")
def _next_auto_folder(base_dir, agent_rel=""):
    today = time.strftime("%Y%m%d")
    names = set()
    base = resolve_dir(base_dir) or base_dir
    try:
        names.update(os.listdir(base))
    except OSError:
        pass
    if agent_rel is not None:
        try:
            data = _agent_list(agent_rel)
            for e in data.get("entries") or []:
                if e.get("dir"):
                    names.add(str(e.get("name") or ""))
        except (AgentDown, AgentError):
            pass
    seq = 0
    for n in names:
        m = AUTO_FOLDER_RE.match(n)
        if m and m.group(1) == today:
            try:
                seq = max(seq, int(m.group(2)))
            except ValueError:
                pass
    return "%s_%02d" % (today, seq + 1)


_SESSION_START_LOCK = threading.Lock()


def op_session_start(dir_path, folder, user):
    with _SESSION_START_LOCK:
        return _session_start_locked(dir_path, folder, user)


def _session_start_locked(dir_path, folder, user):


    if upload_busy():
        raise ApiError(409, "正在上传", status=409)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, created_by, started_at, dir_path FROM sessions "
            "WHERE state='running' ORDER BY started_at DESC LIMIT 1").fetchone()
    if row:
        who = row["created_by"] or "未知用户"
        if row["created_by"] != (user["account"] if user else None):
            raise ApiError(409,
                "已有采集会话在运行（%s 于 %s 启动），请等其结束" % (who, row["started_at"]),
                status=409)
        return _session_payload(dict(row), resumed=True)


    agent_rel = ""
    agent_root = ""
    if agent_on():
        agent_rel = _agent_rel(dir_path)
        try:
            agent_root = (_agent_call("/api/hello", timeout=3)
                          .get("data_root") or "").rstrip("/")
        except (AgentDown, AgentError):
            agent_root = ""
        dir_path = os.path.join(INIT_DATA, agent_rel) if agent_rel else INIT_DATA


    if not (folder or "").strip():
        folder = _next_auto_folder(dir_path, agent_rel)

    pre = op_preflight(dir_path, folder)
    if not pre["ok"]:
        raise ApiError(409, "预检未通过", {"preflight": pre}, status=409)

    target = pre["abs_path"]

    upload_forget((agent_rel + "/" + os.path.basename(target)).strip("/"))
    try:
        os.makedirs(target, exist_ok=True)
    except Exception as e:
        raise ApiError(500, "创建目录失败: %s" % e)
    if agent_on():
        try:
            archive_rel = (agent_rel + "/" + os.path.basename(target)).strip("/")
            _agent_call("/api/files/archive-invalidate", {"dir": archive_rel}, timeout=8)
        except (AgentDown, AgentError):
            pass

    sid = "S%d" % int(time.time() * 1000)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO sessions(id, folder, dir_path, started_at, state, created_by) "
            "VALUES (?,?,?,?,?,?)",
            (sid, folder or os.path.basename(target), rel_to_data(target),
             db.now(), "running", user["account"] if user else None))
        db.audit(conn, user["account"] if user else None, "session_start", target)


    collector_path = "/".join(x for x in (agent_root, agent_rel, folder) if x)
    return {"id": sid, "dir_path": rel_to_data(target), "abs_path": target,
            "folder": folder or os.path.basename(target),
            "collector_path": collector_path,
            "started_at": db.now(), "preflight": pre}


def _session_payload(row, resumed=False):
    """返回可由网页恢复的会话信息。"""
    folder = row.get("folder") or os.path.basename(row.get("dir_path") or "")
    root = ""
    if agent_on():
        try:
            root = (_agent_call("/api/hello", timeout=3).get("data_root") or "").rstrip("/")
        except (AgentDown, AgentError):
            root = ""
    rel = _agent_rel(row.get("dir_path") or "") if agent_on() else ""
    collector_path = "/".join(x for x in (root, rel) if x)
    return {
        "id": row.get("id"), "dir_path": row.get("dir_path"),
        "folder": folder, "collector_path": collector_path,
        "started_at": row.get("started_at"), "resumed": bool(resumed),
    }


def op_session_running(user=None):
    """返回当前运行中的会话，供页面刷新后恢复。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE state='running' "
            "ORDER BY started_at DESC LIMIT 1").fetchone()
    if not row:
        return {"running": False}
    owner = row["created_by"]
    if owner and user and owner != user["account"]:
        return {"running": True, "owned_by_other": True,
                "started_at": row["started_at"]}
    return {"running": True, "session": _session_payload(dict(row), resumed=True)}
COLLECTOR_FILE = "collector.json"
def write_collector(d, account, display_name):

    if not d:
        return None
    acct = (account or "").strip()
    if not acct:
        return None
    name = (display_name or "").strip() or acct
    payload = {
        "collector": acct,
        "collector_name": name,
        "collected_at": db.now(),
    }
    path = os.path.join(d, COLLECTOR_FILE)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        return None
    return payload


def read_collector(d):

    if not d:
        return None
    path = os.path.join(d, COLLECTOR_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("collector"):
            return data
    except Exception:
        pass
    return None


def op_session_end(sid, duration_s, state="done", error=None, user=None):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row:
            raise err404("会话不存在: %s" % sid)
        conn.execute(
            "UPDATE sessions SET ended_at=?, duration_s=?, state=?, error=? WHERE id=?",
            (db.now(), int(duration_s or 0), state, error, sid))
        db.audit(conn, user["account"] if user else None,
                 "session_end", row["dir_path"], "duration=%s" % int(duration_s or 0))
        owner = row["created_by"]


    d = resolve_dir(row["dir_path"])
    name = None
    if owner:
        with db.connect() as conn:
            u = conn.execute(
                "SELECT display_name FROM users WHERE account=?", (owner,)).fetchone()
        name = (u["display_name"] if u else None) or owner
    info = write_collector(d, owner, name) if d else None
    if info and d:
        rel = rel_to_data(d)
        with db.connect() as conn:
            conn.execute(
                "UPDATE records SET collector=?, collector_name=? "
                "WHERE dir_path=? OR dir_path LIKE ?",
                (info["collector"], info["collector_name"], rel, rel + "/%"))


    archive = None
    if agent_on():
        try:
            archive = _agent_call("/api/files/archive",
                                  {"dir": _agent_rel(row["dir_path"])}, timeout=15)
        except (AgentDown, AgentError) as e:
            archive = {"state": "error", "error": str(e)}
    return {"id": sid, "duration_s": int(duration_s or 0), "state": state,
            "collector": info, "archive": archive}


def _agent_list(raw, counts=False):
    rel = _agent_rel(raw)
    q = "/api/files/list?dir=%s" % quote(rel)
    if counts:
        q += "&counts=1"
    return _agent_call(q, timeout=25)


def _agent_op_dirs(path_raw):

    data = _agent_list(path_raw, counts=True)
    base_rel = (data.get("dir") or "").strip("/")
    items = []
    for e in data.get("entries") or []:
        if not e.get("dir"):
            continue
        name = e.get("name") or ""
        if name.startswith(".") or name.endswith(REJECTED_SUFFIX):
            continue
        n = int(e.get("hdf5") or 0)
        if n <= 0:
            continue
        child = (base_rel + "/" + name) if base_rel else name
        items.append({"name": name, "path": child, "episodes": n, "records": n,
                      "collector": None, "collector_name": None})
    items.sort(key=lambda x: x["name"], reverse=True)   
    root = ""
    try:
        root = (_agent_call("/api/hello", timeout=3).get("data_root") or "").rstrip("/")
    except (AgentDown, AgentError):
        root = ""
    abs_base = (root + "/" + base_rel) if (root and base_rel) else (root or base_rel)
    return {"path": base_rel, "abs_path": abs_base, "data_root": root,
            "dirs": items, "remote": True}


def _agent_op_records(dir_path):
    """列出采集端录制文件，不写入本地数据库。"""
    data = _agent_list(dir_path)
    recs = []
    for e in data.get("entries") or []:
        if e.get("dir"):
            continue
        name = e.get("name") or ""
        if not (name.endswith(".hdf5") or name.endswith(".h5")):
            continue
        recs.append({"name": name, "size": e.get("size") or 0,
                     "mtime": e.get("mtime") or 0,
                     "mtime_ns": e.get("mtime_ns") or 0})
    recs.sort(key=lambda r: r["name"])
    return {"dir": data.get("dir") or "", "abs_dir": "", "records": recs,
            "total": len(recs), "remote": True}


def op_records(dir_path, sync=False, session_id=None, remote=False):

    if remote and agent_on():
        try:
            return _agent_op_records(dir_path)
        except (AgentDown, AgentError):
            pass
    d = resolve_dir(dir_path)
    if d is None:
        raise err400("目录不存在或不在允许范围: %s" % dir_path)
    items = list_records(d)
    rel = rel_to_data(d)

    if sync:
        with db.connect() as conn:
            for it in items:
                conn.execute(
                    "INSERT INTO records(dir_path, name, size, mtime, session_id, synced_at) "
                    "VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(dir_path, name) DO UPDATE SET "
                    "size=excluded.size, mtime=excluded.mtime, synced_at=excluded.synced_at, "
                    "session_id=COALESCE(records.session_id, excluded.session_id)",
                    (rel, it["name"], it["size"], it["mtime"], session_id, db.now()))

    return {"dir": rel, "abs_dir": d, "records": items, "total": len(items)}

def op_record_delete(dir_path, name, user=None, remote=False):
    if remote and agent_on():
        try:
            r = _agent_call("/api/files/rm", {"dir": _agent_rel(dir_path), "name": name},
                            timeout=20)
            rel = _agent_rel(dir_path)
            db_rel = os.path.join(os.path.basename(INIT_DATA), rel) if rel else \
                os.path.basename(INIT_DATA)
            with db.connect() as conn:
                conn.execute("DELETE FROM records WHERE dir_path=? AND name=?",
                             (db_rel, name))
                db.audit(conn, user["account"] if user else None, "record_delete",
                         "agent:%s/%s" % (rel, name))
            return {"deleted": r.get("deleted") or name, "remote": True}
        except (AgentDown, AgentError):
            pass
    d = resolve_dir(dir_path)
    if d is None:
        raise err400("目录不存在或不在允许范围")
    if not name or os.sep in name or "/" in name or "\\" in name:
        raise err400("文件名无效")
    full = os.path.join(d, name)
    if not os.path.isfile(full):
        raise err404("文件不存在: %s" % name)
    try:
        os.remove(full)
    except Exception as e:
        raise ApiError(500, "删除失败: %s" % e)
    rel = rel_to_data(d)
    with db.connect() as conn:
        conn.execute("DELETE FROM records WHERE dir_path=? AND name=?", (rel, name))
        db.audit(conn, user["account"] if user else None, "record_delete", full)
    return {"deleted": name}


def op_dir_delete(dir_path, user=None, remote=False):

    if remote and agent_on():


        rel = _agent_rel(dir_path)
        if not rel:
            raise err400("不允许删除数据根目录")
        try:
            r = _agent_call("/api/files/rmdir", {"dir": rel}, timeout=30)
        except (AgentDown, AgentError):
            if remote:
                raise err400("设备已断开，无法删除设备上的目录")
            raise
        db_rel = os.path.join(os.path.basename(INIT_DATA), rel)

        local = os.path.join(os.path.abspath(INIT_DATA), rel)
        if (os.path.isdir(local) and not os.path.islink(local)
                and os.path.abspath(local) != os.path.abspath(INIT_DATA)):
            shutil.rmtree(local, ignore_errors=True)
        with db.connect() as conn:
            for tbl, col in (("records", "dir_path"), ("reviews", "dir_path"),
                             ("segments", "dir_path"),
                             ("review_seconds", "dir_path")):
                conn.execute("DELETE FROM %s WHERE %s=?" % (tbl, col), (db_rel,))
                conn.execute("DELETE FROM %s WHERE %s LIKE ?" % (tbl, col), (db_rel + "/%",))
            db.audit(conn, user["account"] if user else None, "dir_delete",
                     "agent:%s (%d files, %d bytes)" % (rel, r.get("files") or 0,
                                                        r.get("bytes") or 0))
        upload_forget(rel)
        return {"deleted": rel, "files": r.get("files") or 0,
                "bytes": r.get("bytes") or 0, "remote": True}
    d = resolve_dir(dir_path)
    if d is None:
        raise err400("目录不存在或不在允许范围")
    if os.path.islink(d):
        raise err400("拒绝删除符号链接目录")
    d_abs = os.path.abspath(d)
    if d_abs in [os.path.abspath(r) for r in ALLOW_ROOTS]:
        raise err400("不允许删除数据根目录")


    n_files = 0
    total_bytes = 0
    try:
        for root, _dirs, files in os.walk(d_abs):
            for fn in files:
                n_files += 1
                try:
                    total_bytes += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
    except Exception:
        pass

    rel = rel_to_data(d_abs)
    try:
        shutil.rmtree(d_abs)
    except Exception as e:
        raise ApiError(500, "删除失败: %s" % e)

    with db.connect() as conn:
        conn.execute("DELETE FROM records WHERE dir_path=?", (rel,))

        conn.execute("DELETE FROM records WHERE dir_path LIKE ?", (rel + "/%",))
        conn.execute("DELETE FROM reviews WHERE dir_path=?", (rel,))
        conn.execute("DELETE FROM reviews WHERE dir_path LIKE ?", (rel + "/%",))
        conn.execute("DELETE FROM segments WHERE dir_path=?", (rel,))
        conn.execute("DELETE FROM segments WHERE dir_path LIKE ?", (rel + "/%",))
        conn.execute("DELETE FROM review_seconds WHERE dir_path=?", (rel,))
        conn.execute("DELETE FROM review_seconds WHERE dir_path LIKE ?", (rel + "/%",))
        db.audit(conn, user["account"] if user else None, "dir_delete",
                 "%s (%d files, %d bytes)" % (d_abs, n_files, total_bytes))
    upload_forget(os.path.relpath(d_abs, os.path.abspath(INIT_DATA)))
    return {"deleted": rel, "files": n_files, "bytes": total_bytes}


def op_dirs(path_raw, remote=False):

    if remote and agent_on():
        try:
            return _agent_op_dirs(path_raw)
        except (AgentDown, AgentError):
            pass
    base = resolve_dir(path_raw)
    base = base or INIT_DATA
    items = []
    try:
        names = sorted(os.listdir(base))
    except Exception:
        names = []
    for name in names:
        if name.startswith(".") or name.endswith(REJECTED_SUFFIX):

            continue
        full = os.path.join(base, name)
        if not os.path.isdir(full):
            continue
        cnt = len(list_episode_files(full))
        rec = len(list_records(full))
        if cnt <= 0 and rec <= 0:
            continue
        coll = read_collector(full)
        items.append({"name": name, "path": rel_to_data(full),
                      "episodes": cnt, "records": rec,
                      "collector": (coll or {}).get("collector"),
                      "collector_name": (coll or {}).get("collector_name")})
    return {"path": rel_to_data(base), "dirs": items}


def op_episodes(dir_path):
    d = resolve_dir(dir_path)
    if d is None:
        raise err400("目录不存在或不在允许范围: %s" % dir_path)
    rel = rel_to_data(d)
    with db.connect() as conn:
        labels = {r["episode"]: r for r in conn.execute(
            "SELECT episode, status, comment, reviewed_at, reviewed_by "
            "FROM reviews WHERE dir_path=?", (rel,))}
        segs = {}
        for r in conn.execute(
                "SELECT episode, seq, from_frame, to_frame, label "
                "FROM segments WHERE dir_path=? ORDER BY episode, seq", (rel,)):
            segs.setdefault(r["episode"], []).append({
                "from": r["from_frame"], "to": r["to_frame"], "label": r["label"]})

    eps = []
    for ep, _ in list_episode_files(d):
        lab = labels.get(ep)
        eps.append({
            "id": ep,
            "status": lab["status"] if lab else "",
            "comment": lab["comment"] if lab else "",
            "segments": segs.get(ep, []),
            "reviewed_at": lab["reviewed_at"] if lab else None,
        })
    coll = read_collector(d)


    cams, fps_hint, schema_name = [], NOMINAL_FPS, None
    if eps:
        first = _ep_path(eps[0]["id"], d)
        if os.path.isfile(first):
            try:
                with _h5_lock:
                    f = h5open(first)
                    sch = detect_schema(f)
                    cams = [{"key": c["key"], "label": c["label"]} for c in sch["cams"]]
                    if sch["fps"]:
                        fps_hint = sch["fps"]
                    if sch["timestamps"]:
                        schema_name = ("v1" if sch["timestamps"][0].startswith("metadata/")
                                       else "v2")
            except Exception:
                pass

    return {"data_dir": rel, "abs_dir": d, "episodes": eps,
            "token": hashlib.md5(d.encode()).hexdigest()[:12],
            "fps": fps_hint,
            "schema": schema_name,
            "collector": (coll or {}).get("collector"),
            "collector_name": (coll or {}).get("collector_name"),
            "cameras": cams}

def _ep_path(ep, d):
    return os.path.join(d, "episode_%d_vla_valid.hdf5" % ep)


def _ep_meta(ep, d):
    path = _ep_path(ep, d)
    if not os.path.isfile(path):
        raise err404("episode %d 不存在" % ep)

    with _h5_lock:
        f = h5open(path)
        sch = detect_schema(f)
        if not sch["timestamps"]:
            raise ApiError(422, "文件里找不到时间戳（metadata/timestamps_ns "
                                "或 observations/timestamps）")
        ts_path, scale = sch["timestamps"]
        ts = np.asarray(f[ts_path][...], dtype=np.float64)

        fps_attr = sch["fps"] if sch["fps"] else NOMINAL_FPS
        try:
            sr = json.loads(f.attrs.get("sampling_rates_json", "{}"))
        except Exception:
            sr = {}
        cam_ok = [{"key": c["key"], "label": c["label"], "available": True}
                  for c in sch["cams"]]


        ep_len = None
        try:
            if "episode_len" in f.attrs:
                ep_len = int(f.attrs["episode_len"])
        except Exception:
            ep_len = None


    if len(ts) > 1:
        t_sec = ts * scale if scale < 1 else ts / scale
    else:
        t_sec = ts * 0
    dur = float(t_sec[-1] - t_sec[0]) if len(t_sec) else 0.0

    return {
        "id": ep,
        "frames": len(ts),
        "frames_attr": ep_len,
        "fps_attr": fps_attr,
        "duration_s": round(dur, 2),
        "actual_fps": round((len(ts) - 1) / dur, 2) if dur > 0 else 0,
        "camera_hz": sr.get("camera_hz"),
        "t_rel": np.round(t_sec - (t_sec[0] if len(t_sec) else 0), 3).tolist() if len(t_sec) else [],
        "cameras": cam_ok,
        "schema": "v1" if ts_path.startswith("metadata/") else "v2",
    }


def _ep_frame(ep, d, cam_key, idx):
    path = _ep_path(ep, d)
    if not os.path.isfile(path):
        raise err404("episode %d 不存在" % ep)


    sch = detect_schema_cached(path)
    cam = next((c for c in sch["cams"] if c["key"] == cam_key), None)
    if cam is None:
        raise err404("该 episode 无此相机数据")

    with _h5_lock:
        f = h5open(path)
        ds = f[cam["ds"]]
        n = ds.shape[0]
        if idx < 0 or idx >= n:
            raise err404("帧号越界（0-%d）" % (n - 1))
        if cam["len_ds"]:
            ln = int(f[cam["len_ds"]][idx])
        else:
            ln = ds.shape[1]
        ln = max(0, min(ln, ds.shape[1]))
        buf = ds[idx, :ln].tobytes()


    if not buf:
        raise err404("该帧数据为空")
    return buf


def _ep_curves(ep, d):
    path = _ep_path(ep, d)
    if not os.path.isfile(path):
        raise err404("episode %d 不存在" % ep)

    with _h5_lock:
        f = h5open(path)
        sch = detect_schema(f)

        def grab(key, sl=None):
            if not key or key not in f:
                return None
            arr = f[key][...]
            if sl:
                arr = arr[sl]
            arr = np.asarray(arr, dtype=np.float32)
            r = np.round(arr, 5)
            return np.where(np.isfinite(r), r, None).tolist()


        qpos_n = f[sch["qpos_ds"]].shape[1] if sch["qpos_ds"] else 0
        act_n = f[sch["action_ds"]].shape[1] if sch["action_ds"] else 0
        arm_n = 7 if max(qpos_n, act_n) >= 7 else max(qpos_n, act_n)

        out = {
            "names": {"arm": ARM_NAMES[:arm_n], "leg": LEG_NAMES},
            "dims": {"qpos": qpos_n, "action": act_n},
        }
        out["qpos"] = grab(sch["qpos_ds"], (slice(None), slice(0, arm_n)) if arm_n else None)
        out["leg"] = grab(sch["leg_ds"])
        out["torque"] = grab(sch["torque_ds"])
        out["action"] = grab(sch["action_ds"], (slice(None), slice(0, arm_n)) if arm_n else None)
        out["commanded"] = grab(sch["commanded_ds"],
                                (slice(None), slice(0, arm_n)) if arm_n else None)
        out["done"] = grab(sch["done_ds"])

    return out


VALID_STATUS = ("good", "bad", "pending", "")
def op_review_save(dir_path, episode, status=None, comment=None, segments=None, user=None):
    d = resolve_dir(dir_path)
    if d is None:
        raise err400("目录不存在或不在允许范围")
    if not os.path.isfile(os.path.join(d, "episode_%d_vla_valid.hdf5" % episode)):
        raise err404("episode %d 不存在" % episode)
    if status is not None and status not in VALID_STATUS:
        raise err400("status 必须是 good / bad / pending / 空")
    rel = rel_to_data(d)

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM reviews WHERE dir_path=? AND episode=?",
                           (rel, episode)).fetchone()
        cur_status = row["status"] if row else ""
        cur_comment = row["comment"] if row else ""
        new_status = cur_status if status is None else status
        new_comment = cur_comment if comment is None else comment
        conn.execute(
            "INSERT INTO reviews(dir_path, episode, status, comment, reviewed_at, reviewed_by) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(dir_path, episode) DO UPDATE SET "
            "status=excluded.status, comment=excluded.comment, "
            "reviewed_at=excluded.reviewed_at, reviewed_by=excluded.reviewed_by",
            (rel, episode, new_status, new_comment, db.now(),
             user["account"] if user else None))

        if segments is not None:
            if not isinstance(segments, list):
                raise err400("segments 必须是 list 或 null")
            conn.execute("DELETE FROM segments WHERE dir_path=? AND episode=?",
                         (rel, episode))
            clean = []
            for sg in segments:
                try:
                    frm, to = int(sg["from"]), int(sg["to"])
                except Exception:
                    raise err400("segments 元素需要 from / to 整数")
                lbl = sg.get("label")
                clean.append((frm, to, (str(lbl).strip() or None) if lbl else None))
            clean.sort(key=lambda s: s[0])
            for i, (frm, to, lbl) in enumerate(clean):
                conn.execute(
                    "INSERT INTO segments(dir_path, episode, seq, from_frame, to_frame, label) "
                    "VALUES (?,?,?,?,?,?)", (rel, episode, i, frm, to, lbl))

        db.audit(conn, user["account"] if user else None, "review",
                 "%s#%d" % (rel, episode), "status=%s" % new_status)

    refresh_snapshot(d, rel)
    return {"episode": episode, "status": new_status, "comment": new_comment}


def op_review_batch(dir_path, episodes, status, comment=None, user=None):
    """批量设置 episode 审核状态。"""
    if not isinstance(episodes, list) or not episodes:
        raise err400("episodes 必须是非空数组")
    if status not in VALID_STATUS:
        raise err400("status 无效")
    d = resolve_dir(dir_path)
    if d is None:
        raise err400("目录不存在或不在允许范围")
    rel = rel_to_data(d)

    done, missing = [], []
    with db.connect() as conn:
        for ep in episodes:
            try:
                ep = int(ep)
            except Exception:
                missing.append(ep)
                continue
            if not os.path.isfile(os.path.join(d, "episode_%d_vla_valid.hdf5" % ep)):
                missing.append(ep)
                continue
            conn.execute(
                "INSERT INTO reviews(dir_path, episode, status, comment, reviewed_at, reviewed_by) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(dir_path, episode) DO UPDATE SET "
                "status=excluded.status, "
                "comment=CASE WHEN ? IS NULL THEN reviews.comment ELSE excluded.comment END, "
                "reviewed_at=excluded.reviewed_at, reviewed_by=excluded.reviewed_by",
                (rel, ep, status, comment or "", db.now(),
                 user["account"] if user else None, comment))
            done.append(ep)
        db.audit(conn, user["account"] if user else None, "review_batch",
                 rel, "status=%s n=%d" % (status, len(done)))

    refresh_snapshot(d, rel)
    return {"updated": done, "missing": missing, "status": status}


_ERROR_PATTERNS = [
    (re.compile(r"\b[0-9a-f]{8,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d+\b"), "<n>"),
    (re.compile(r"(https?://[^\s'\")]+)"), "<url>"),
    (re.compile(r"(/[\w.-]+){2,}"), "<path>"),
    (re.compile(r"\s+"), " "),
]

ERROR_MAX_LEN = 120
ERROR_KEEP_DAYS = int(os.environ.get("DM_ERROR_KEEP_DAYS", "30"))


_ERROR_CLASSES = [
    ("读取空对象", ("Cannot read propert", "of undefined", "of null")),
    ("调用非函数", ("is not a function",)),
    ("变量未定义", ("is not defined",)),
    ("网络请求失败", ("NetworkError", "Failed to fetch")),
    ("请求超时", ("timeout", "Timeout")),
    ("配额超限", ("QuotaExceeded",)),
]


def normalize_error(msg):

    t = (msg or "").strip()
    if not t:
        return "未知错误"


    for label, keys in _ERROR_CLASSES:
        if any(k in t for k in keys):
            return label

    for rx, rep in _ERROR_PATTERNS:
        t = rx.sub(rep, t)
    t = t.strip()
    return t[:ERROR_MAX_LEN] if len(t) > ERROR_MAX_LEN else t


def op_error_report(body, user=None):
    """前端报错上报。kind: js | api | promise。"""
    if not isinstance(body, dict):
        raise err400("上报内容必须是对象")
    kind = (body.get("kind") or "js").strip()[:16]
    raw = (body.get("message") or "").strip()
    if not raw:
        return {"saved": False, "reason": "message 为空"}
    detail = (body.get("detail") or "").strip()[:1000]
    url = (body.get("url") or "").strip()[:300]
    now = db.now()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO error_log(at, kind, message, detail, url, account, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (now, kind, normalize_error(raw), detail, url,
             user["account"] if user else None, now))

        if ERROR_KEEP_DAYS > 0:
            conn.execute(
                "DELETE FROM error_log WHERE at < ?",
                (time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(time.time() - ERROR_KEEP_DAYS * 86400)),))
    return {"saved": True}


def op_error_overview(days=7):
    """按原因汇总错误统计。"""
    since = time.strftime("%Y-%m-%d %H:%M:%S",
                          time.localtime(time.time() - max(1, int(days)) * 86400))
    with db.connect() as conn:
        has = _has_table(conn, "error_log")
        if not has:
            return {"items": [], "total": 0, "days": days, "by_kind": {}}
        rows = conn.execute(
            "SELECT kind, message, COUNT(*) AS n, MAX(at) AS last_at "
            "FROM error_log WHERE at >= ? GROUP BY kind, message "
            "ORDER BY n DESC", (since,)).fetchall()
        by_kind = {}
        for r in conn.execute(
                "SELECT kind, COUNT(*) AS n FROM error_log WHERE at >= ? "
                "GROUP BY kind", (since,)):
            by_kind[r["kind"]] = int(r["n"])


    merged = {}
    for r in rows:
        key = r["message"]
        it = merged.get(key)
        if it:
            it["count"] += int(r["n"])
            if r["last_at"] and r["last_at"] > (it["last_at"] or ""):
                it["last_at"] = r["last_at"]
            it["kinds"].add(r["kind"])
        else:
            merged[key] = {"reason": key, "count": int(r["n"]),
                           "last_at": r["last_at"], "kinds": {r["kind"]}}

    items = sorted(merged.values(), key=lambda x: -x["count"])
    for it in items:
        it["kinds"] = sorted(it["kinds"])
    return {"items": items[:10], "total": sum(i["count"] for i in items),
            "days": days, "by_kind": by_kind}


def op_review_time(dir_path, episode, seconds, user=None):
    """累加某条 episode 的标注停留时长（前端埋点上报）。"""
    d = resolve_dir(dir_path)
    if d is None:
        raise err400("目录不存在或不在允许范围")
    try:
        secs = float(seconds)
    except Exception:
        raise err400("seconds 必须是数字")
    if secs < 0 or secs > 86400:
        raise err400("seconds 超出合理范围")
    rel = rel_to_data(d)
    who = user["account"] if user else None
    now = db.now()


    if who:
        with db.connect() as conn:
            u = conn.execute(
                "SELECT created_at FROM users WHERE account=?", (who,)).fetchone()
        if u and u["created_at"]:
            try:
                born = time.mktime(time.strptime(u["created_at"], "%Y-%m-%d %H:%M:%S"))
                age = max(0.0, time.time() - born)
                if secs > age + 5:
                    secs = max(0.0, age)
            except Exception:
                pass

    with db.connect() as conn:

        if _has_table(conn, "review_time_ledger"):
            conn.execute(
                "INSERT INTO review_time_ledger(at, dir_path, episode, seconds, account) "
                "VALUES (?,?,?,?,?)", (now, rel, int(episode), secs, who))


    return {"episode": int(episode), "added_s": round(secs, 1)}
def op_me(user):
    """当前登录账号信息。"""
    if not user:
        raise err401()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT account, role, created_at, last_login_at, display_name "
            "FROM users WHERE account=?", (user["account"],)).fetchone()
    return {
        "account": row["account"] if row else user["account"],
        "role": row["role"] if row else user["role"],
        "display_name": (row["display_name"] if row else None) or user["account"],
        "created_at": row["created_at"] if row else None,
        "last_login_at": row["last_login_at"] if row else None,
    }


def op_users_list(admin):
    """返回账号列表及当前身份，用于账号管理权限控制。"""
    users = auth.list_users()
    return {"users": users, "me": admin["account"] if admin else None,
            "roles": ["user", "admin"]}


def op_config_get():

    with db.connect() as conn:
        raw = db.get_setting(conn, "host_config", "")
    try:
        data = json.loads(raw) if raw else {}
    except Exception:                   # noqa: BLE001
        data = {}
    data.pop("_agent", None)
    if agent_on():
        try:
            host = _agent_call("/api/host", timeout=6)
            data = dict(host.get("config") or {})
            data["dds_password_set"] = (
                bool(data.pop("dds_password", "")) or bool(data.get("dds_password_set")))
            if host.get("data_root"):
                data["local_data_dir"] = host["data_root"]
            data["_agent"] = {
                "url": agent_url(),
                "device_id": host.get("device_id"),
                "port": host.get("port"),
                "host": host.get("host"),
                "arch": host.get("arch"),
                "data_root": host.get("data_root"),
                "disk": host.get("disk"),
                "reachable": True,
            }
        except (AgentDown, AgentError) as e:

            data = {}
            url = agent_url()
            reg = {}
            for did, info in _dev_registry().items():
                if "http://%s:%s" % (info.get("ip"), info.get("port")) == url:
                    reg = dict(info, device_id=did)
                    break
            data["_agent"] = {"url": url, "reachable": False, "error": str(e),
                              "device_id": reg.get("device_id"),
                              "port": reg.get("port"),
                              "host": reg.get("host"),
                              "arch": reg.get("arch")}
    return {"config": data}


def op_config_save(payload, user=None):

    if not isinstance(payload, dict):
        raise err400("配置必须是对象")
    stored = dict(payload)
    stored.pop("dds_password", None)
    stored.pop("dds_password_set", None)
    stored.pop("_agent", None)
    with db.connect() as conn:
        db.set_setting(conn, "host_config", json.dumps(stored, ensure_ascii=False))
        db.audit(conn, user["account"] if user else None, "config_save", "host_config")
    pushed = None
    if agent_on():
        clean = {k: v for k, v in payload.items()
                 if k not in ("_agent", "dds_password_set")}
        try:
            _agent_call("/api/config", {"cfg": clean}, timeout=10)
            pushed = True
        except (AgentDown, AgentError) as e:

            pushed = str(e)
    return {"saved": True, "agent_pushed": pushed}


REASON_RULES = [
    ("画面抖动", ("抖", "晃", "震")),
    ("动作不连贯", ("不连贯", "卡顿", "断", "跳变")),
    ("遮挡/视野", ("遮挡", "视野", "挡住", "偏")),
    ("光照不足", ("暗", "光", "曝光")),
    ("对不齐/错位", ("错位", "对不齐", "偏移", "跑偏")),
    ("数据损坏", ("损坏", "缺失", "丢", "坏")),
    ("重复采集", ("重复", "多余")),
]

REASON_MAX_LEN = 14


def normalize_reason(text):

    t = (text or "").strip()
    if not t:
        return None
    for label, keys in REASON_RULES:
        if any(k in t for k in keys):
            return label
    return t[:REASON_MAX_LEN]


def _scan_all_dirs():

    out = []
    try:
        names = sorted(os.listdir(INIT_DATA))
    except Exception:
        return out
    for name in names:
        if name.startswith("."):
            continue
        full = os.path.join(INIT_DATA, name)
        if not os.path.isdir(full):
            continue
        out.append((name, full, rel_to_data(full)))
    return out


def op_overview(user=None):

    is_admin = bool(user and auth.require(user, "admin"))
    month = time.strftime("%Y-%m")

    quality = {"good": 0, "pending": 0, "bad": 0,
               "good_by": [], "bad_by": []}
    improve = {"pending": {}, "bad": {}}
    scale = {"datasets": 0, "records": 0, "episodes": 0, "bytes": 0}
    dir_owner = {}
    all_dirs = _scan_all_dirs()


    with db.connect() as conn:
        labels = {}
        for r in conn.execute("SELECT dir_path, episode, status, comment FROM reviews"):
            labels.setdefault(r["dir_path"], {})[int(r["episode"])] = {
                "status": r["status"] or "", "comment": r["comment"] or ""}
        db_owner = {}
        for r in conn.execute(
                "SELECT dir_path, collector, collector_name FROM records "
                "WHERE collector IS NOT NULL"):
            if r["dir_path"] not in db_owner:
                db_owner[r["dir_path"]] = {
                    "collector": r["collector"],
                    "collector_name": r["collector_name"] or r["collector"]}

    good_count = {}
    bad_count = {}

    for name, full, rel in all_dirs:
        coll = read_collector(full) or db_owner.get(rel) or db_owner.get(name) or {}
        owner = coll.get("collector") or ""
        owner_name = coll.get("collector_name") or owner
        if owner:
            dir_owner[rel] = owner_name
            dir_owner[name] = owner_name

        eps = list_episode_files(full)
        recs = list_records(full)
        if eps or recs:
            scale["datasets"] += 1
            scale["records"] += len(recs)
            scale["episodes"] += len(eps)
            scale["bytes"] += sum(x["size"] for x in recs)

        lab = labels.get(rel, {})

        if not lab:
            lab = labels.get(name, {})

        for ep, _p in eps:
            st = (lab.get(ep) or {}).get("status") or ""
            if st == "good":
                quality["good"] += 1
                if owner_name:
                    good_count[owner_name] = good_count.get(owner_name, 0) + 1
            elif st == "bad":
                quality["bad"] += 1
                if owner_name:
                    bad_count[owner_name] = bad_count.get(owner_name, 0) + 1
                reason = normalize_reason((lab.get(ep) or {}).get("comment"))
                if reason:
                    improve["bad"][reason] = improve["bad"].get(reason, 0) + 1
            else:

                quality["pending"] += 1
                reason = normalize_reason((lab.get(ep) or {}).get("comment"))
                if reason:
                    improve["pending"][reason] = improve["pending"].get(reason, 0) + 1

    quality["good_by"] = [{"name": k, "count": v}
                          for k, v in sorted(good_count.items(), key=lambda kv: -kv[1])]
    quality["bad_by"] = [{"name": k, "count": v}
                         for k, v in sorted(bad_count.items(), key=lambda kv: -kv[1])]
    quality["total"] = quality["good"] + quality["pending"] + quality["bad"]


    duration = {"users": [], "total_s": 0, "month": month}
    if is_admin:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT created_by AS acct, SUM(duration_s) AS secs, COUNT(*) AS n "
                "FROM sessions WHERE started_at LIKE ? AND created_by IS NOT NULL "
                "GROUP BY created_by ORDER BY secs DESC", (month + "%",)).fetchall()
            names = {}
            for u in conn.execute("SELECT account, display_name FROM users"):
                names[u["account"]] = u["display_name"] or u["account"]
        for r in rows:
            secs = int(r["secs"] or 0)
            duration["users"].append({
                "account": r["acct"],
                "name": names.get(r["acct"], r["acct"]),
                "seconds": secs,
                "sessions": int(r["n"] or 0),
            })
            duration["total_s"] += secs


    def top_reasons(m, limit=8):
        items = sorted(m.items(), key=lambda kv: -kv[1])
        head = [{"reason": k, "count": v} for k, v in items[:limit]]
        rest = sum(v for _k, v in items[limit:])
        if rest:
            head.append({"reason": "其它", "count": rest})
        return head

    improve_out = {
        "pending": top_reasons(improve["pending"]),
        "bad": top_reasons(improve["bad"]),
        "pending_total": sum(improve["pending"].values()),
        "bad_total": sum(improve["bad"].values()),
    }

    return {
        "quality": quality,
        "improve": improve_out,
        "duration": duration,
        "duration_visible": is_admin,
        "errors": op_error_overview(7),
        "dir_owner": dir_owner,
    }


def op_stats(user=None):

    today = time.strftime("%Y-%m-%d")
    month = time.strftime("%Y-%m")

    who = user["account"] if user else None
    with db.connect() as conn:
        def cap_span(prefix):
            """按采集者统计会话时长。"""
            where = ["created_by = ?"]
            args = [who]
            if prefix:
                where.append("started_at LIKE ?")
                args.append(prefix + "%")
            sql = ("SELECT COALESCE(SUM(duration_s),0) AS s FROM sessions WHERE "
                   + " AND ".join(where))
            r = conn.execute(sql, args).fetchone()
            return int(r["s"])

        cap_today = cap_span(today)
        cap_month = cap_span(month)
        cap_total = cap_span("")

        def ann_span(prefix):
            """仅从 review_time_ledger 按实际发生时间统计审核时长。"""
            if not _has_table(conn, "review_time_ledger"):
                return 0, 0
            where = ["account = ?"]
            args = [who]
            if prefix:
                where.append("at LIKE ?")
                args.append(prefix + "%")
            sql = ("SELECT COALESCE(SUM(seconds),0) AS s, COUNT(*) AS n "
                   "FROM review_time_ledger WHERE " + " AND ".join(where))
            r = conn.execute(sql, args).fetchone()
            return int(r["s"]), int(r["n"])

        ann_today, ann_today_eps = ann_span(today)
        ann_month, ann_month_eps = ann_span(month)
        ann_total, ann_eps = ann_span("")

        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM reviews GROUP BY status").fetchall()
        by_status = {r["status"] or "": r["n"] for r in rows}
        total_reviews = conn.execute("SELECT COUNT(*) AS n FROM reviews").fetchone()["n"]
        total_segments = conn.execute("SELECT COUNT(*) AS n FROM segments").fetchone()["n"]

        recent = [dict(r) for r in conn.execute(
            "SELECT at, account, action, target, detail FROM audit_log "
            "ORDER BY id DESC LIMIT 8")]


    ep_total = 0
    datasets = 0
    records = 0
    total_bytes = 0
    try:
        for name in sorted(os.listdir(INIT_DATA)):
            full = os.path.join(INIT_DATA, name)
            if not os.path.isdir(full) or name.startswith("."):
                continue
            eps = len(list_episode_files(full))
            recs = list_records(full)
            if eps or recs:
                datasets += 1
                records += len(recs)
                total_bytes += sum(x["size"] for x in recs)
            ep_total += eps
    except Exception:
        pass

    decided = by_status.get("good", 0) + by_status.get("bad", 0)
    reviewable = max(ep_total, total_reviews)
    pending_rest = max(0, reviewable - decided)

    free = None
    try:
        free = shutil.disk_usage(INIT_DATA).free
    except Exception:
        pass

    return {
        "capture": {
            "today_s": cap_today,
            "month_s": cap_month,
            "total_s": cap_total,
        },
        "annotate": {
            "today_s": ann_today,
            "month_s": ann_month,
            "total_s": ann_total,
            "episodes": ann_eps,
            "source": "review_time_ledger" if ann_total else "not_reported",
        },
        "review": {
            "counted": total_reviews,
            "good": by_status.get("good", 0),
            "bad": by_status.get("bad", 0),
            "pending": by_status.get("pending", 0),
            "segments": total_segments,
            "episodes": ep_total,
            "decided": decided,
            "pending_rest": pending_rest,
        },
        "dataset": {
            "datasets": datasets,
            "records": records,
            "bytes": total_bytes,
            "free_bytes": free,
            "root": rel_to_data(INIT_DATA),
        },
        "recent": recent,
    }


def _has_table(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _read_review_state(rel):
    """读出某个数据目录的判定与分段（导出和实时刷新共用）。"""
    with db.connect() as conn:
        labels = {r["episode"]: r for r in conn.execute(
            "SELECT episode, status, comment FROM reviews WHERE dir_path=?",
            (rel,))}
        segs = {}
        for r in conn.execute(
                "SELECT episode, from_frame, to_frame, label FROM segments "
                "WHERE dir_path=? ORDER BY episode, seq", (rel,)):
            segs.setdefault(str(r["episode"]), []).append(
                {"from": r["from_frame"], "to": r["to_frame"], "label": r["label"]})
    return labels, segs


def _build_snapshot(d, rel, labels, segs):
    """组装导出快照：good / pending / bad + 每条 episode 的子任务分段。"""
    good, bad, pending = [], [], []
    files = {}
    for ep, path in list_episode_files(d):
        files[ep] = path
        st = labels.get(ep)["status"] if labels.get(ep) else ""
        (good if st == "good" else bad if st == "bad" else pending).append(ep)

    episodes_out = []
    for ep in sorted(files):
        row = labels.get(ep)
        st = (row["status"] if row else "") or ""
        episodes_out.append({
            "episode": ep,
            "file": os.path.basename(files[ep]),
            "verdict": st if st in ("good", "bad") else "pending",
            "comment": (row["comment"] if row else "") or "",
            "segments": segs.get(str(ep), []),
        })

    snap = {"dir": rel, "exported_at": db.now(),
            "counts": {"total": len(good) + len(bad) + len(pending),
                       "good": len(good), "pending": len(pending),
                       "bad": len(bad)},
            "good": good, "pending": pending, "bad": bad,
            "episodes": episodes_out}
    return snap, good, bad, pending, files


def _snapshot_path(d):

    base = os.path.basename(d.rstrip("/")) or "review"
    return os.path.join(d, base + ".json")

def _write_snapshot_file(d, snap):
    """通过临时文件及原子替换写入审核快照。"""
    path = _snapshot_path(d)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    os.replace(path + ".tmp", path)
    return path


def refresh_snapshot(d, rel=None):
    """刷新审核快照；写入失败不影响审核结果。"""
    try:
        rel = rel or rel_to_data(d)
        labels, segs = _read_review_state(rel)
        snap, _, _, _, _ = _build_snapshot(d, rel, labels, segs)
        _write_snapshot_file(d, snap)
    except Exception:
        pass


QC_RULES = {
    "min_frames": 10,
    "max_gap_ms": 1000.0,
    "black_run_limit": 15,
    "white_run_limit": 15,
    "freeze_run_limit": 15,
    "check_occlusion": True,
    "check_scene_redundancy": True,
    "check_action_consistency": True,
    "max_same_scene_ratio": 0.50,
    "scene_diff_threshold": 6.0,
    "min_cluster_files": 3,
    "min_scene_files": 4,
}

_QC_JOBS = {}
_QC_LOCK = threading.Lock()


def _qc_public(job):
    return {"dir": job["dir"], "running": job["running"],
            "done": job["done"], "total": job["total"],
            "current": job["current"], "error": job["error"],
            "summary": job["summary"], "started_at": job["started_at"]}


_AUX_RATE_RE = re.compile(r"^时序长度不一致: .*_\d+hz\s*\(")
def _qc_split(findings):
    """区分独立采样域的高速镜像流，避免帧数不一致误报。"""
    kept, aux = [], 0
    for level, msg in findings:
        if level == "FAIL" and _AUX_RATE_RE.match(msg):
            aux += 1
            continue
        kept.append((level, msg))
    return kept, aux


def _qc_reasons(findings):
    """优先返回 FAIL 原因，否则返回 WARN 原因。"""
    fails = [msg for level, msg in findings if level == "FAIL"]
    pool = fails or [msg for _, msg in findings]
    return list(dict.fromkeys(pool))


def _qc_note(reasons):
    """生成用于审核列表的简短质检说明。"""
    picked = []
    for r in reasons[:2]:
        picked.append(r if len(r) <= 48 else r[:47] + "…")
    text = "；".join(picked)
    if len(reasons) > 2:
        text += " 等 %d 项" % len(reasons)
    return text


def _qc_reviewer(conn, prefer):
    """选择有效审核账号，满足 reviews.reviewed_by 的外键约束。"""
    if isinstance(prefer, dict):
        prefer = prefer.get("account")
    if prefer is not None:
        prefer = str(prefer)
    for sql, args in (
        ("SELECT account FROM users WHERE account=?", (prefer,)),
        ("SELECT account FROM users WHERE role='admin' ORDER BY id LIMIT 1", ()),
        ("SELECT account FROM users ORDER BY id LIMIT 1", ()),
    ):
        if args and not prefer:
            continue
        row = conn.execute(sql, args).fetchone()
        if row:
            return row["account"]
    return None


def _qc_apply(d, rel, verdict, summary, reviewer=None):
    """保存质检结论并刷新导出快照。"""
    with db.connect() as conn:
        who = _qc_reviewer(conn, reviewer)
        for ep, (status, comment) in sorted(verdict.items()):
            conn.execute(
                "INSERT INTO reviews(dir_path, episode, status, comment, reviewed_at, reviewed_by) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(dir_path, episode) DO UPDATE SET "
                "status=excluded.status, comment=excluded.comment, "
                "reviewed_at=excluded.reviewed_at, reviewed_by=excluded.reviewed_by",
                (rel, ep, status, comment, db.now(), who))
        db.audit(conn, who or "auto_qc", "autocheck", rel,
                 "good=%d pending=%d" % (summary["good"], summary["pending"]))
    refresh_snapshot(d, rel)


def _qc_worker(d, rel, job, reviewer=None):
    try:
        files = list(list_episode_files(d))
        job["total"] = len(files)
        name_to_ep = {os.path.basename(p): ep for ep, p in files}
        verdict = {}
        aux_ignored = 0

        for ep, path in files:
            job["current"] = os.path.basename(path)
            try:
                _ok, findings = auto_qc.check_episode(path, QC_RULES)
                findings, aux = _qc_split(findings)
                aux_ignored += aux
                reasons = _qc_reasons(findings)
                if any(level == "FAIL" for level, _ in findings):
                    verdict[ep] = ("pending", "自动质检: " + (_qc_note(reasons) or "未通过"))
                else:
                    verdict[ep] = ("good", "")
            except Exception as e:     # noqa: BLE001

                verdict[ep] = ("pending", "自动质检失败: %s" % (str(e)[:80] or type(e).__name__))
            job["done"] += 1

        passed_names = [n for n, ep in name_to_ep.items()
                        if verdict.get(ep, ("",))[0] == "good"]
        if len(passed_names) >= 3:
            extra = {}
            try:
                extra.update(auto_qc.check_folder_action_consistency(
                    d, passed_names, QC_RULES) or {})
            except Exception:          # noqa: BLE001
                pass
            try:
                scene_anomalies, _diversity = auto_qc.check_folder_scene_redundancy(
                    d, passed_names, QC_RULES)
                extra.update(scene_anomalies or {})
            except Exception:          # noqa: BLE001
                pass
            for name, reasons in extra.items():
                ep = name_to_ep.get(name)
                if ep is None:
                    continue
                verdict[ep] = ("pending", "自动质检: " + _qc_note(list(reasons)))

        summary = {"total": len(files),
                   "good": sum(1 for v in verdict.values() if v[0] == "good"),
                   "pending": sum(1 for v in verdict.values() if v[0] == "pending"),
                   "bad": 0,
                   "aux_len_ignored": aux_ignored}
        if verdict:
            _qc_apply(d, rel, verdict, summary, reviewer)
        job["summary"] = summary
    except Exception as e:         # noqa: BLE001
        job["error"] = "%s: %s" % (type(e).__name__, e)
    finally:
        job["running"] = False
        job["current"] = ""


def op_autocheck_start(dir_path, user=None):
    if auto_qc is None:
        raise ApiError(500, "自动质检脚本缺失：server/auto_qc.py")
    d = resolve_dir(dir_path)
    if d is None:
        raise err400("目录不存在或不在允许范围")
    rel = rel_to_data(d)

    with _QC_LOCK:
        job = _QC_JOBS.get(rel)
        if job and job["running"]:
            return _qc_public(job)
        job = {"dir": rel, "running": True, "done": 0, "total": 0,
               "current": "", "error": None, "summary": None,
               "started_at": db.now()}
        _QC_JOBS[rel] = job

    threading.Thread(target=_qc_worker, args=(d, rel, job, user),
                     daemon=True).start()
    return _qc_public(job)


def op_autocheck_status(dir_path):
    d = resolve_dir(dir_path)
    if d is None:
        raise err400("目录不存在或不在允许范围")
    rel = rel_to_data(d)
    job = _QC_JOBS.get(rel)
    if not job:
        return {"dir": rel, "running": False, "done": 0, "total": 0,
                "current": "", "error": None, "summary": None,
                "started_at": None}
    return _qc_public(job)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "deepmaster/" + APP_VERSION

    def log_message(self, fmt, *args):
        pass


    def _send(self, status, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))

        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, data):
        self._send(200, {"ok": True, "data": data})

    def _error(self, e):
        if isinstance(e, ApiError):
            self._send(e.status, {"code": e.code, "message": e.message, "data": e.data})
        else:
            self._send(500, {"code": 500, "message": "%s: %s" % (type(e).__name__, e)})

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")


    def _agent_get(self, path, timeout=20):
        """转发只读查询；采集端不可用时返回 503。"""
        try:
            return self._ok(_agent_call(path, timeout=timeout))
        except AgentDown as e:
            return self._send(503, {"code": 503, "message": str(e), "agent_down": True})
        except AgentError as e:
            return self._send(502, {"code": 502, "message": str(e)})


    def _cam(self, action, key):

        if agent_on():
            try:
                if action == "health":
                    return self._ok(_agent_call("/api/cam/health", timeout=8))
                if action == "snap":


                    resp = _agent_open("/api/cam/snap/%s" % quote(key or "wrist"), timeout=10)
                    try:
                        body = resp.read()
                        ctype = resp.headers.get("Content-Type") or "image/jpeg"
                    finally:
                        resp.close()
                    return self._send(200, body, ctype, {"Cache-Control": "no-store"})
                if action == "stream":
                    resp = _agent_open("/api/cam/stream/%s" % quote(key or "wrist"), timeout=15)
                    ctype = resp.headers.get("Content-Type") or \
                        "multipart/x-mixed-replace; boundary=frame"
                    return self._relay(resp, ctype)
                raise err404("未知相机接口 /api/cam/%s" % action)
            except AgentDown as e:
                return self._send(503, {"code": 503, "message": str(e)})
            except AgentError as e:
                return self._send(503, {"code": 503, "message": str(e)})
        if cam is None:
            return self._send(503, {"code": 503, "message": "相机模块不可用"})
        if action == "health":
            return self._ok(cam.health())
        if action == "snap":
            try:
                body, ctype = cam.snapshot(key)
            except cam.CamUnavailable as e:
                return self._send(503, {"code": 503, "message": str(e)})
            return self._send(200, body, ctype, {"Cache-Control": "no-store"})
        if action == "stream":
            return self._cam_stream(key)
        raise err404("未知相机接口 /api/cam/%s" % action)

    def _cam_stream(self, key):
        """流式转发 MJPEG，不设置 Content-Length。"""
        try:
            resp, ctype = cam.open_stream(key)
        except cam.CamUnavailable as e:
            return self._send(503, {"code": 503, "message": str(e)})
        return self._relay(resp, ctype)

    def _relay(self, resp, ctype):
        """逐块转发响应并及时刷新输出。"""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
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
            except Exception:          # noqa: BLE001
                pass


    def _client_ip(self):

        for key in ("X-Forwarded-For", "X-Real-IP"):
            v = self.headers.get(key)
            if v:
                return v.split(",")[0].strip()
        return self.client_address[0] if self.client_address else ""

    def _token(self):
        h = self.headers.get("Authorization", "")
        if h.startswith("Bearer "):
            return h[7:].strip()
        return None

    def _user(self, required=True, min_role=None):
        u = auth.current_user(self._token())
        if required and not u:
            raise err401()
        if u and min_role and not auth.require(u, min_role):
            raise err403()
        return u

    def _body_cfg(self):

        return {}

    def _body(self):
        raw = self._drain()
        if not raw:
            return {}
        try:
            return json.loads(raw or b"{}")
        except Exception as e:
            raise err400("请求体解析失败: %s" % e)

    def _drain(self):
        """缓存完整请求体，避免未读数据污染 HTTP keep-alive 连接。"""
        if getattr(self, "_raw_body", None) is not None:
            return self._raw_body
        n = int(self.headers.get("Content-Length", 0) or 0)
        self._raw_body = self.rfile.read(n) if n > 0 else b""
        return self._raw_body


    def do_GET(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        q = parse_qs(url.query)
        try:
            self._raw_body = None
            self._drain()
            if not parts or parts[0] != "api":
                return self._send(200, {"service": "deepmaster", "version": APP_VERSION,
                                        "hint": "API 前缀 /api"}, "application/json; charset=utf-8")
            return self._route_get(parts[1:], q)
        except Exception as e:
            self._error(e)

    def _route_get(self, rest, q):
        head = rest[0] if rest else ""

        if head == "health":
            return self._ok({"ok": True, "time": db.now(), "version": APP_VERSION})

        if head == "preflight":
            d = (q.get("dir") or [""])[0]
            if agent_on():
                rel = _agent_rel(d)
                d = os.path.join(INIT_DATA, rel) if rel else INIT_DATA
            return self._ok(op_preflight(d, (q.get("folder") or [""])[0] or None))

        if head == "dirs":
            return self._ok(op_dirs((q.get("path") or [""])[0],
                                    remote=(q.get("remote") or ["0"])[0] == "1"))

        if head == "records":
            return self._ok(op_records((q.get("dir") or [""])[0],
                                       sync=(q.get("sync") or ["0"])[0] == "1",
                                       remote=(q.get("remote") or ["0"])[0] == "1"))
        if head == "episodes":
            return self._ok(op_episodes((q.get("dir") or [""])[0]))
        if head == "latency":
            if agent_on():
                return self._agent_get("/api/latency", timeout=10)
            return self._ok(system.gateway_latency())

        if head == "system":
            if agent_on():
                return self._agent_get("/api/system", timeout=10)
            return self._ok(system.snapshot())

        if head == "run" and len(rest) >= 2 and rest[1] == "status":
            if agent_on():
                return self._agent_get("/api/run/status", timeout=8)
            return self._ok(runner.status({
                "k7master3": ["K7Master3"],
                "robotservice": ["runService.sh", "RoboticsServiceProcess"],
                "rl_deploy": ["rl_deploy"],
            }))

        if head == "me":
            return self._ok(op_me(self._user(required=True)))


        if head == "users":
            return self._ok(op_users_list(self._user(required=True, min_role="admin")))
        if head == "config":
            return self._ok(op_config_get())


        if head == "agent" and len(rest) >= 2 and rest[1] == "scan":
            self._user(required=True, min_role="user")
            return self._ok(op_agent_scan({}, None))


        if head == "pull":
            self._user(required=True, min_role="user")
            return self._ok(op_pull_state())


        if head == "uploads":
            self._user(required=True, min_role="user")
            return self._ok(op_folder_upload_state())

        if head == "stats":
            return self._ok(op_stats(self._user(required=False)))


        if head == "overview":
            return self._ok(op_overview(self._user(required=True)))

        if head == "autocheck":
            return self._ok(op_autocheck_status((q.get("dir") or [""])[0]))

        if head == "cam" and len(rest) >= 2:
            return self._cam(rest[1], rest[2] if len(rest) >= 3 else "")
        if head == "episode" and len(rest) >= 3:
            d = resolve_dir((q.get("dir") or [""])[0])
            if d is None:
                raise err400("目录不存在或不在允许范围")
            ep = int(rest[1])
            what = rest[2]
            if what == "meta":
                return self._ok(_ep_meta(ep, d))
            if what == "curves":
                return self._ok(_ep_curves(ep, d))
            if what == "frame" and len(rest) >= 5:
                buf = _ep_frame(ep, d, rest[3], int(rest[4]))
                return self._send(200, buf, "image/jpeg",
                                  {"Cache-Control": "public, max-age=86400"})
            raise err404("未知 episode 接口")

        if head == "sessions" and len(rest) >= 2 and rest[1] == "running":
            return self._ok(op_session_running(self._user(required=True, min_role="user")))

        if head == "sessions" and len(rest) >= 2:
            with db.connect() as conn:
                row = conn.execute("SELECT * FROM sessions WHERE id=?", (rest[1],)).fetchone()
                if not row:
                    raise err404("会话不存在")
                return self._ok(dict(row))

        raise err404("未知接口 /api/%s" % head)

    def do_POST(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        try:
            self._raw_body = None
            self._drain()
            if not parts or parts[0] != "api":
                raise err404()
            return self._route_post(parts[1:])
        except Exception as e:
            self._error(e)

    def _route_post(self, rest):
        head = rest[0] if rest else ""


        if head == "auth" and len(rest) >= 2 and rest[1] == "reset":
            body = self._body()
            acc = (body.get("account") or "").strip()
            code = body.get("code") or ""
            pwd = body.get("password") or ""
            ok, payload = auth.consume_reset_code(acc, code, pwd, self._client_ip())
            if not ok:
                raise ApiError(payload["code"], payload["message"], status=payload["code"])
            r = auth.login(acc, pwd)
            if not r:
                raise ApiError(500, "密码已重置但登录失败", status=500)
            return self._ok(r)


        if head == "auth" and len(rest) >= 2 and rest[1] == "register":
            body = self._body()
            acc = (body.get("account") or "").strip()
            pwd = body.get("password") or ""
            dn = (body.get("display_name") or "").strip()
            ok, payload = auth.register(acc, pwd, role="user", display_name=dn)
            if not ok:
                raise ApiError(payload["code"], payload["message"],
                               status=payload["code"])
            r = auth.login(acc, pwd)
            if not r:
                raise ApiError(500, "账号已创建但登录失败", status=500)
            return self._ok(r)

        if head == "auth" and len(rest) >= 2 and rest[1] == "login":
            body = self._body()
            acc = (body.get("account") or "").strip()
            pwd = body.get("password") or ""
            if not acc or not pwd:
                raise err400("账号和密码必填")
            r = auth.login(acc, pwd)
            if not r:
                raise ApiError(401, "账号或密码不正确", status=401)


            loc = geo.resolve(self._client_ip(), self.headers)
            with db.connect() as conn:
                conn.execute(
                    "UPDATE audit_log SET detail=? WHERE id=("
                    "  SELECT MAX(id) FROM audit_log WHERE action='login' AND account=?)",
                    (loc, acc))
            r["location"] = loc
            return self._ok(r)


        if head == "errors":
            return self._ok(op_error_report(self._body(),
                                            self._user(required=False)))


        user = self._user(required=True, min_role="user")
        if head == "pull":
            return self._ok(op_pull(self._body(), user))


        if head == "uploads":
            return self._ok(op_folder_upload_start(self._body(), user))


        if head == "agent" and len(rest) >= 2:
            act = rest[1]
            if act == "scan":
                return self._ok(op_agent_scan(self._body(), user))
            if act == "connect":
                return self._ok(op_agent_connect(self._body(), user))
            if act == "select":
                return self._ok(op_agent_select(self._body(), user))
            raise err404("未知设备接口：%s" % act)

        if head == "config":
            return self._ok(op_config_save(self._body(), user))


        if head == "reset_code":
            admin = self._user(required=True, min_role="admin")
            body = self._body()
            ok, payload = auth.issue_reset_code(
                (body.get("account") or "").strip(), admin["account"])
            if not ok:
                raise ApiError(payload["code"], payload["message"], status=payload["code"])
            return self._ok(payload)


        if head == "users":
            admin = self._user(required=True, min_role="admin")
            body = self._body()
            acc = (body.get("account") or "").strip()
            pwd = body.get("password") or ""
            dn = (body.get("display_name") or "").strip()
            role = body.get("role") or "user"

            ok, payload = auth.register(acc, pwd, role=role, display_name=(dn or None))
            if not ok:
                raise ApiError(payload["code"], payload["message"], status=payload["code"])
            with db.connect() as conn:
                db.audit(conn, admin["account"], "user_create", acc, payload.get("role"))
            payload["by"] = admin["account"]
            return self._ok(payload)

        if head == "sessions":
            body = self._body()
            d = str(body.get("dir") or "").strip()
            if not d and not agent_on():


                d = rel_to_data(DEFAULT_DATA)
            return self._ok(op_session_start(d, body.get("folder"), user))

        if head == "autocheck":
            return self._ok(op_autocheck_start(self._body().get("dir") or "", user))

        if head == "review":
            body = self._body()
            return self._ok(op_review_save(
                body.get("dir") or "", int(body.get("episode")),
                body.get("status"), body.get("comment"), body.get("segments"), user))

        if head == "reviews" and len(rest) >= 2 and rest[1] == "batch":
            user = self._user(required=True, min_role="user")
            body = self._body()
            return self._ok(op_review_batch(body.get("dir") or "",
                                            body.get("episodes") or [],
                                            body.get("status"),
                                            body.get("comment"), user))

        if head == "run" and len(rest) >= 2:
            user = self._user(required=True, min_role="user")
            action = rest[1]
            cfg = (self._body() or {}).get("cfg") or {}
            if agent_on():


                try:
                    r = _agent_call("/api/run/" + quote(action),
                                    {"cfg": cfg}, timeout=120)
                except AgentDown as e:
                    return self._send(503, {"code": 503, "message": str(e),
                                            "agent_down": True})
                except AgentError as e:
                    return self._send(502, {"code": 502, "message": str(e)})
                with db.connect() as conn:
                    db.audit(conn, user["account"], "run_" + action,
                             "agent", None if r.get("ok") else r.get("error"))
                return self._ok(r)
            if action == "preflight":
                r = runner.preflight_local(cfg)
            elif action == "t1":
                r = runner.run_t1(cfg)
            elif action == "tare":
                r = runner.run_tare(cfg)
            elif action == "t2":
                r = runner.run_t2(cfg)
            elif action == "z":
                r = runner.inject_z(cfg)
            elif action == "service-start":
                r = runner.start_service(cfg)
            elif action == "service-stop":
                r = runner.stop_service(cfg)
            elif action == "deploy-start":
                r = runner.start_deploy(cfg)
            elif action == "deploy-stop":
                r = runner.stop_deploy(cfg)
            elif action == "capture-stop":
                r = runner.stop_capture(cfg)
            elif action == "k7-stop":
                r = runner.stop_k7_force(cfg)
            elif action == "stop":
                r = runner.stop_all(cfg)
            else:
                raise err404("未知执行动作: %s" % action)
            with db.connect() as conn:
                db.audit(conn, user["account"], "run_" + action,
                         r.get("pid") and str(r.get("pid")) or "", r.get("error"))
            return self._ok(r)

        if head == "review_time":
            body = self._body()
            return self._ok(op_review_time(body.get("dir") or "",
                                           body.get("episode", 0),
                                           body.get("seconds", 0), user))

        if head == "record":
            body = self._body()
            return self._ok(op_record_delete(body.get("dir") or "", body.get("name") or "", user))

        if head == "subtask_export":
            body = self._body()
            d = resolve_dir(body.get("dir") or "")
            if d is None:
                raise err400("目录不存在或不在允许范围")
            rel = rel_to_data(d)
            with db.connect() as conn:
                out = {}
                for r in conn.execute(
                        "SELECT episode, from_frame, to_frame, label FROM segments "
                        "WHERE dir_path=? ORDER BY episode, seq", (rel,)):
                    out.setdefault(str(r["episode"]), []).append(
                        {"from": r["from_frame"], "to": r["to_frame"], "label": r["label"]})
            path = os.path.join(d, "subtask_segments.json")
            with open(path + ".tmp", "w") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
            os.replace(path + ".tmp", path)
            return self._ok({"saved": len(out), "path": rel})

        raise err404("未知接口 /api/%s" % head)

    def do_PATCH(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        try:
            self._raw_body = None
            self._drain()

            if parts[:2] == ["api", "users"]:
                admin = self._user(required=True, min_role="admin")
                body = self._body()
                ok, payload = auth.set_user_role(
                    (body.get("account") or "").strip(), body.get("role"),
                    admin["account"])
                if not ok:
                    raise ApiError(payload["code"], payload["message"],
                                   status=payload["code"])
                payload["by"] = admin["account"]
                return self._ok(payload)

            if parts[:2] != ["api", "sessions"] or len(parts) < 3:
                raise err404()
            user = self._user(required=True, min_role="user")
            body = self._body()
            return self._ok(op_session_end(parts[2], body.get("duration_s", 0),
                                          body.get("state", "done"),
                                          body.get("error"), user))
        except Exception as e:
            self._error(e)

    def do_DELETE(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        try:
            self._raw_body = None
            self._drain()

            if parts[:2] == ["api", "users"]:
                admin = self._user(required=True, min_role="admin")
                q = parse_qs(url.query)
                ok, payload = auth.delete_user((q.get("account") or [""])[0],
                                               admin["account"])
                if not ok:
                    raise ApiError(payload["code"], payload["message"],
                                   status=payload["code"])
                return self._ok(payload)
            if parts[:2] == ["api", "records"]:
                user = self._user(required=True, min_role="user")
                q = parse_qs(url.query)
                return self._ok(op_record_delete((q.get("dir") or [""])[0],
                                                 (q.get("name") or [""])[0], user,
                                                 remote=(q.get("remote") or ["0"])[0] == "1"))

            if parts[:2] == ["api", "dirs"]:
                user = self._user(required=True, min_role="user")
                q = parse_qs(url.query)
                return self._ok(op_dir_delete((q.get("path") or [""])[0], user,
                                              remote=(q.get("remote") or ["0"])[0] == "1"))
            raise err404()
        except Exception as e:
            self._error(e)


def _seed_host_config():
    """仅在配置为空时，从 DM_HOST_CONFIG 的 JSON 或文件路径初始化。"""
    raw = (os.environ.get("DM_HOST_CONFIG") or "").strip()
    if not raw:
        return
    try:
        if os.path.isfile(raw):
            with open(raw, "r", encoding="utf-8") as f:
                raw = f.read()
        obj = json.loads(raw)
    except Exception as e:                 # noqa: BLE001
        print("[host_config] DM_HOST_CONFIG 解析失败，已忽略: %s" % e)
        return
    if not isinstance(obj, dict) or not obj:
        print("[host_config] DM_HOST_CONFIG 不是非空对象，已忽略")
        return
    try:
        with db.connect() as conn:
            if db.get_setting(conn, "host_config", ""):
                print("[host_config] 已有配置，保持不动（要强制预置就清掉 settings.host_config）")
                return
            db.set_setting(conn, "host_config", json.dumps(obj, ensure_ascii=False))
        print("[host_config] 已按 DM_HOST_CONFIG 预置 %d 项" % len(obj))
    except Exception as e:                 # noqa: BLE001
        print("[host_config] 预置失败: %s" % e)


def main():
    ran = db.migrate()
    for v, n in ran:
        print("[db] applied migration %d %s" % (v, n))
    acc, pwd = auth.ensure_bootstrap_user()
    if acc:
        print("[auth] 初始管理员: %s" % acc)
        if pwd:
            print("[auth] 一次性密码（请记录，不会再次显示）: %s" % pwd)

    _seed_host_config()

    os.makedirs(INIT_DATA, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print("[srv] 数据根: %s" % INIT_DATA)
    print("[srv] API: http://127.0.0.1:%d/api/health" % PORT)
    srv.serve_forever()


if __name__ == "__main__":
    main()

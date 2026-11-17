# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""读取本机资源、网络状态与温度。"""

import os
import shutil
import subprocess
import time

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return ""


def cpu():
    """根据两次 /proc/stat 采样计算 CPU 使用率。"""
    def sample():
        line = _read("/proc/stat").splitlines()[0].split()
        vals = [int(x) for x in line[1:8]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        return idle, total

    i1, t1 = sample()
    time.sleep(0.4)
    i2, t2 = sample()
    dt = t2 - t1
    di = i2 - i1
    pct = 0.0 if dt <= 0 else max(0.0, min(100.0, (1 - di / dt) * 100))

    try:
        load1, load5, load15 = [float(x) for x in _read("/proc/loadavg").split()[:3]]
    except Exception:
        load1 = load5 = load15 = None

    return {
        "percent": round(pct, 1),
        "cores": os.cpu_count() or 1,
        "load1": load1, "load5": load5, "load15": load15,
    }


def memory():
    info = {}
    for line in _read("/proc/meminfo").splitlines():
        parts = line.split(":")
        if len(parts) == 2:
            key = parts[0]
            num = parts[1].strip().split()[0]
            try:
                info[key] = int(num) * 1024
            except ValueError:
                pass
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    used = total - avail
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", 0)
    return {
        "total": total, "used": used, "available": avail,
        "percent": round(used / total * 100, 1) if total else 0,
        "swap_total": swap_total,
        "swap_used": swap_total - swap_free,
    }


def disk():
    try:
        du = shutil.disk_usage(PROJECT_DIR)
    except Exception:
        return None
    return {
        "total": du.total, "used": du.used, "free": du.free,
        "percent": round(du.used / du.total * 100, 1) if du.total else 0,
    }


def network():

    dev = gw = None
    out = ""
    try:
        p = subprocess.run("ip -4 route show default", shell=True,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=4)
        out = (p.stdout or b"").decode("utf-8", "replace").strip()
        if out:
            parts = out.splitlines()[0].split()
            if "via" in parts:
                gw = parts[parts.index("via") + 1]
            if "dev" in parts:
                dev = parts[parts.index("dev") + 1]
    except Exception:
        pass

    ip = None
    if dev:
        try:
            p = subprocess.run("ip -4 -o addr show dev %s" % dev, shell=True,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=4)
            txt = (p.stdout or b"").decode("utf-8", "replace")
            for tok in txt.split():
                if "/" in tok and tok.split("/")[0].count(".") == 3:
                    ip = tok.split("/")[0]
                    break
        except Exception:
            pass

    gw_ms = None
    if gw:
        try:
            p = subprocess.run("ping -c 1 -W 1 %s" % gw, shell=True,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=4)
            txt = (p.stdout or b"").decode("utf-8", "replace")
            import re
            m = re.search(r"time[=<]([\d.]+)", txt)
            if m:
                gw_ms = round(float(m.group(1)), 1)
        except Exception:
            pass

    return {"iface": dev, "gateway": gw, "ip": ip, "gw_ms": gw_ms}

def temperature():

    zones = []
    try:
        for name in os.listdir("/sys/class/thermal"):
            if not name.startswith("thermal_zone"):
                continue
            val = _read("/sys/class/thermal/%s/temp" % name).strip()
            typ = _read("/sys/class/thermal/%s/type" % name).strip()
            if not val:
                continue
            try:
                t = int(val) / 1000.0
            except ValueError:
                continue
            if -20 < t < 150:
                zones.append({"type": typ or name, "celsius": round(t, 1)})
    except Exception:
        pass
    if not zones:
        return None
    hottest = max(zones, key=lambda z: z["celsius"])
    return {"celsius": hottest["celsius"], "sensor": hottest["type"], "all": zones}


def gateway_latency():
    """测量至默认网关的往返延迟；失败返回 None。"""
    gw = None
    try:
        p = subprocess.run("ip -4 route show default", shell=True,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=3)
        out = (p.stdout or b"").decode("utf-8", "replace").strip()
        if out:
            parts = out.splitlines()[0].split()
            if "via" in parts:
                gw = parts[parts.index("via") + 1]
    except Exception:
        pass

    if not gw:
        return {"gateway": None, "ms": None, "ok": False}

    ms = None
    try:
        p = subprocess.run("ping -c 1 -W 1 %s" % gw, shell=True,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=3)
        txt = (p.stdout or b"").decode("utf-8", "replace")
        import re
        m = re.search(r"time[=<]([\d.]+)", txt)
        if m:
            ms = round(float(m.group(1)), 1)
    except Exception:
        pass

    return {"gateway": gw, "ms": ms, "ok": ms is not None}

def snapshot():
    return {
        "cpu": cpu(),
        "memory": memory(),
        "disk": disk(),
        "network": network(),
        "temperature": temperature(),
    }

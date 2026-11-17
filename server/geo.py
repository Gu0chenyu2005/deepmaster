# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""按来源地址及代理信息解析登录位置。"""

import ipaddress
import json
import os
import urllib.request

_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".geo_cache.json")
_ENABLE_ONLINE = os.environ.get("DM_GEO_LOOKUP", "0") == "1"
_TIMEOUT = 3


def _load_cache():
    try:
        with open(_CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(obj):
    try:
        with open(_CACHE_PATH, "w") as f:
            json.dump(obj, f, ensure_ascii=False)
    except Exception:
        pass


def _local_ips():
    out = set()
    try:
        import socket
        out.add(socket.gethostname())
        for info in socket.getaddrinfo(socket.gethostname(), None):
            out.add(info[4][0])
    except Exception:
        pass
    return out


def _online_city(ip):
    if not _ENABLE_ONLINE:
        return None
    try:
        url = "http://ip-api.com/json/%s?fields=status,country,regionName,city" % ip
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        if d.get("status") != "success":
            return None
        city = (d.get("city") or "").strip()
        region = (d.get("regionName") or "").strip()
        if city:
            return city
        if region:
            return region
    except Exception:
        return None
    return None


def resolve(client_ip, headers=None):

    headers = headers or {}
    ip = (client_ip or "").strip()


    if ip in ("", "127.0.0.1", "::1") or ip in _local_ips():
        return "本机"
    try:
        parsed = ipaddress.ip_address(ip)
        if parsed.is_private:
            return "局域网"
    except Exception:
        pass


    for key in ("X-Geo-City", "X-Real-City", "X-City"):
        v = (headers.get(key) or "").strip()
        if v:
            return v


    cache = _load_cache()
    if ip in cache:
        return cache[ip]


    city = _online_city(ip)
    if city:
        cache[ip] = city
        _save_cache(cache)
        return city

    return "未知"

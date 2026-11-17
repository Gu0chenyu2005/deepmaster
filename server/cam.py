#!/usr/bin/env python3
# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""相机 HTTP 代理；仅读取共享内存预览服务，不直接访问设备。"""

import json
import os
import threading
import time
from urllib.request import urlopen

DEFAULT_UPSTREAM = "http://127.0.0.1:5000"
DEFAULT_MAP = "wrist:primary,global:d435i,third:third"

FALLBACK_ORDER = ["primary", "third", "d435i"]
_ALIASES = {
    "main": "primary",
    "realsense": "primary",
    "new": "d435i",
    "third-view": "third",
    "thirdview": "third",
}

_HEALTH_TTL = 2.0
_HEALTH = {"ts": 0.0, "data": None}
_HEALTH_LOCK = threading.Lock()


class CamUnavailable(Exception):
    """相机预览服务不可用。"""


def upstream():
    return (os.environ.get("DM_CAM_UPSTREAM") or DEFAULT_UPSTREAM).rstrip("/")


def _norm(label):
    s = (label or "").strip().lower()
    return _ALIASES.get(s, s)


def cam_map():
    """{"wrist": "primary", "global": "d435i", "third": "third"}"""
    raw = os.environ.get("DM_CAM_MAP") or DEFAULT_MAP
    out = {}
    for item in raw.split(","):
        if ":" not in item:
            continue
        key, label = item.split(":", 1)
        key, label = key.strip(), _norm(label)
        if key and label:
            out[key] = label
    return out


def upstream_health(force=False):

    now = time.time()
    with _HEALTH_LOCK:
        if not force and _HEALTH["data"] is not None and now - _HEALTH["ts"] < _HEALTH_TTL:
            return _HEALTH["data"]
    data = None
    try:
        with urlopen(upstream() + "/health", timeout=2.5) as r:
            data = json.loads(r.read() or b"{}")
    except Exception:          # noqa: BLE001
        data = None
    with _HEALTH_LOCK:
        _HEALTH["ts"] = now
        _HEALTH["data"] = data
    return data


def video_index(key):
    """前端 key(wrist/global/third) → 上游 /videoN 的 N。"""
    label = cam_map().get(key)
    if not label:
        raise CamUnavailable("未知相机 key: %s（DM_CAM_MAP 里没有）" % (key or "(空)"))
    segs = (upstream_health() or {}).get("segments") or []
    if any(_norm(s.get("label")) for s in segs):
        for i, s in enumerate(segs):
            if _norm(s.get("label")) == label:
                return i + 1
        have = ", ".join("%s->/video%d" % (_norm(s.get("label")) or "?", i + 1)
                         for i, s in enumerate(segs))
        raise CamUnavailable("上游 3 路里没有 %s（现有 %s）" % (label, have))
    if label in FALLBACK_ORDER:
        return FALLBACK_ORDER.index(label) + 1
    raise CamUnavailable("上游 /health 不可达，且 %s 不在兜底顺序里" % label)


def snapshot(key):

    idx = video_index(key)
    try:
        with urlopen("%s/snapshot%d.jpg" % (upstream(), idx), timeout=4) as r:
            body = r.read()
            ctype = r.headers.get("Content-Type") or "image/jpeg"
    except Exception as e:      # noqa: BLE001
        raise CamUnavailable("取快照失败: %s" % e)
    if not body:
        raise CamUnavailable("上游返回空帧")
    return body, ctype


def open_stream(key):
    """返回 (response, content_type)；调用方负责流式读取并关闭响应。"""
    idx = video_index(key)
    try:
        resp = urlopen("%s/video%d" % (upstream(), idx), timeout=15)
    except Exception as e:      # noqa: BLE001
        raise CamUnavailable("连不上上游流: %s" % e)
    ctype = resp.headers.get("Content-Type") or "multipart/x-mixed-replace; boundary=frame"
    return resp, ctype


def health():

    data = upstream_health(force=True)
    segs = (data or {}).get("segments") or []
    cams = {}
    for key, label in cam_map().items():
        item = {"key": key, "label": label, "video": None, "online": False,
                "size": None, "fps": None, "error": None}
        idx = None
        if segs:
            for i, s in enumerate(segs):
                if _norm(s.get("label")) == label:
                    idx = i + 1
                    item["size"] = s.get("size")
                    item["fps"] = s.get("fps_measured")
                    item["error"] = s.get("error") or None
                    item["online"] = bool(s.get("present")) and not s.get("error")
                    break
            if idx is None:
                item["error"] = "上游无此路"
        elif label in FALLBACK_ORDER:
            idx = FALLBACK_ORDER.index(label) + 1
            item["error"] = "上游不可达"
        else:
            item["error"] = "上游不可达"
        item["video"] = idx
        cams[key] = item
    return {
        "upstream": upstream(),
        "reachable": data is not None,
        "segments": segs,
        "cams": cams,
    }

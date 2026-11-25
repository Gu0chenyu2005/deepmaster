#!/usr/bin/env python3
# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""提供共享内存预览与仅控制模式的相机直连预览。"""

import argparse
import hmac
import hmac
import json
import mmap
import os
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np


SHM_DIR = "/dev/shm"
DEFAULT_SEGMENT = "/armdog_realsense_stream"
MAGIC = 0x31525356
VERSION = 1
HEADER_BYTES = 4096
FMT_RGB8 = 1
FMT_Z16 = 1


HEADER_STRUCT = struct.Struct("<Q14IQQ32s")
_jpeg = None
WEBRTC = None
WEBRTC_TOKEN = ""
DIRECT_WEBRTC = False
DIRECT_FRAME_TIMEOUT_MS = 5000
DIRECT_FRAME_TIMEOUT_RETRIES = 2
ACCESS_TOKEN = ""
VIEW_PROFILES = {
    "default": {"quality": 80, "width": 0, "height": 0, "fps": 20},
    "remote": {"quality": 68, "width": 1280, "height": 720, "fps": 30},
    "remote_low": {"quality": 54, "width": 960, "height": 540, "fps": 24},
    "remote_min": {"quality": 42, "width": 640, "height": 360, "fps": 18},
    "control": {"quality": 68, "width": 1280, "height": 720, "fps": 0},
    "control_low": {"quality": 50, "width": 848, "height": 480, "fps": 0},
    "control_min": {"quality": 36, "width": 640, "height": 360, "fps": 0},
}


def _load_encoder():
    """优先使用 OpenCV，其次为 Pillow；均不可用时退出。"""
    global _jpeg
    try:
        import cv2
        cv2.setNumThreads(1)
        _jpeg = ("cv2", cv2)
        return
    except Exception:
        pass
    try:
        from PIL import Image
        _jpeg = ("pil", Image)
        return
    except Exception:
        sys.exit("[FATAL] 既没有 cv2 也没有 Pillow, 无法编码 MJPEG。")


def encode_jpeg(bgr, quality=80):
    if _jpeg[0] == "cv2":
        cv2 = _jpeg[1]
        ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return buf.tobytes() if ok else None
    Image = _jpeg[1]
    import io
    rgb = bgr[:, :, ::-1]
    out = io.BytesIO()
    Image.fromarray(rgb).save(out, format="JPEG", quality=quality)
    return out.getvalue()


def draw_overlay(bgr, line1, line2):
    if _jpeg[0] != "cv2":
        return bgr
    cv2 = _jpeg[1]
    out = bgr.copy()
    cv2.rectangle(out, (0, 0), (out.shape[1], 46), (24, 24, 24), -1)
    cv2.putText(out, line1, (8, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (240, 240, 240), 1, cv2.LINE_AA)
    cv2.putText(out, line2, (8, 39), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                (150, 200, 255), 1, cv2.LINE_AA)
    return out


def placeholder_jpeg(text):
    """生成相机不可用时的占位图。"""
    w, h = 640, 480
    if _jpeg[0] == "cv2":
        cv2 = _jpeg[1]
        img = np.full((h, w, 3), 28, np.uint8)
        cv2.putText(img, text, (36, h // 2 - 10), cv2.FONT_HERSHEY_SIMPLEX,
                    0.75, (90, 190, 255), 2, cv2.LINE_AA)
        cv2.putText(img, "streamed by rl_deploy shm - not opened", (36, h // 2 + 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (140, 140, 140), 1, cv2.LINE_AA)
        return encode_jpeg(img)
    img = np.full((h, w, 3), 28, np.uint8)
    return encode_jpeg(img)


class SegmentReader:
    """读取共享内存并缓存最新 JPEG 帧。"""

    def __init__(self, name):
        self.name = name
        self.path = os.path.join(SHM_DIR, name.lstrip("/"))
        self.lock = threading.Lock()
        self.frame_bgr = None
        self.label = ""
        self.width = 0
        self.height = 0
        self.fps = 0
        self.dropped = 0
        self.seq = 0
        self.age_ms = None
        self.rate = 0.0
        self.present = False
        self.error = "missing"
        self._stop = threading.Event()
        self._thread = None
        self._jpeg_cache = None
        self._jpeg_seq = -1
        self._profile_cache = {}
        self._encode_lock = threading.Lock()
        self._last_seq = -1
        self._buf = None


    def start(self):
        self._thread = threading.Thread(target=self._run, name=f"shm-{self.name}",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()


    def _read_once(self, mm):
        seq1 = HEADER_STRUCT.unpack_from(mm, 0)[0]
        if seq1 & 1:
            return None
        (magic, version, hsize, width, height, fps, rgb_fmt, rgb_stride, rgb_size,
         rgb_cap, depth_fmt, depth_stride, depth_size, depth_cap,
         ts_ns, dropped, label) = HEADER_STRUCT.unpack_from(mm, 0)[1:]
        if magic != MAGIC or hsize != HEADER_BYTES or width == 0 or height == 0:
            self.error = f"bad header magic=0x{magic:08x} hsize={hsize} {width}x{height}"
            return None
        need = HEADER_BYTES + rgb_cap + depth_cap
        if len(mm) < need or rgb_size == 0 or rgb_size > rgb_cap:
            self.error = f"truncated shm len={len(mm)} need={need} rgb={rgb_size}"
            return None
        raw = mm[HEADER_BYTES:HEADER_BYTES + rgb_size]
        depth_raw = None
        if depth_size == width * height * 2 and depth_size <= depth_cap:
            depth_raw = mm[HEADER_BYTES + rgb_cap:HEADER_BYTES + rgb_cap + depth_size]
        seq2 = HEADER_STRUCT.unpack_from(mm, 0)[0]
        if seq1 != seq2:
            return None
        return {
            "seq": seq2, "width": width, "height": height, "fps": fps,
            "label": label.split(b"\x00", 1)[0].decode("utf-8", "replace"),
            "rgb": raw, "depth": depth_raw,
        }

    def _to_bgr(self, f):
        rgb = np.frombuffer(f["rgb"], np.uint8).reshape(f["height"], f["width"], 3)
        if _jpeg[0] == "cv2":
            cv2 = _jpeg[1]
            return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        return rgb[:, :, ::-1].copy()

    def _run(self):
        mm = None
        fd = -1
        ino = None
        last_new = 0.0
        last_t = time.time()
        frames = 0
        while not self._stop.is_set():
            try:
                st = os.stat(self.path)
            except FileNotFoundError:
                st = None
            if st is None:
                if mm is not None:
                    try:
                        mm.close()
                    except Exception:
                        pass
                    try:
                        os.close(fd)
                    except Exception:
                        pass
                    mm, fd, ino = None, -1, None
                self.present = False
                self.error = "missing (rl_deploy 未在采集? 或 ARMDOG_SHARED_FRAME_ENABLE=0)"
                with self.lock:
                    self.age_ms = None
                time.sleep(0.5)
                continue

            if mm is None or st.st_ino != ino:
                try:
                    if mm is not None:
                        mm.close()
                        os.close(fd)
                    fd = os.open(self.path, os.O_RDONLY)
                    mm = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
                    ino = os.fstat(fd).st_ino
                    self.error = ""
                except Exception as exc:
                    self.error = f"open failed: {exc}"
                    mm, fd, ino = None, -1, None
                    time.sleep(0.5)
                    continue

            f = None
            try:
                f = self._read_once(mm)
            except Exception as exc:
                self.error = f"read failed: {exc}"
                try:
                    mm.close()
                    os.close(fd)
                except Exception:
                    pass
                mm, fd, ino = None, -1, None
                time.sleep(0.3)
                continue

            if f is None:
                time.sleep(0.002)
                continue

            now = time.time()
            is_new = f["seq"] != self._last_seq
            if is_new:
                self._last_seq = f["seq"]
                last_new = now
                frames += 1
                bgr = self._to_bgr(f)
                jpg = encode_jpeg(draw_overlay(
                    bgr, f"{self.name}  {f['label']}  {f['width']}x{f['height']}",
                    f"seq={f['seq']}  {f['fps']}fps  rl_deploy shared-mem"))
                with self.lock:
                    self.frame_bgr = bgr
                    self.label = f["label"]
                    self.width, self.height, self.fps = f["width"], f["height"], f["fps"]
                    self.seq = f["seq"]
                    self.present = True
                    self.error = ""
                    self.age_ms = 0
                    self._profile_cache.clear()
                    if jpg:
                        self._jpeg_cache = jpg
            else:

                stale = (now - last_new) > 3.0
                if stale and self.present:
                    self.present = False
                    self.error = "stalled: sequence 3s 未变化 (rl_deploy 停了? 相机断了?)"
                    self.age_ms = int((now - last_new) * 1000)
            if now - last_t >= 1.0:
                self.rate = round(frames / (now - last_t), 1)
                frames = 0
                last_t = now
            time.sleep(0.005)

    def snapshot(self, profile="default"):
        """返回 (jpeg_bytes, meta)；无画面时返回占位图。"""
        settings = VIEW_PROFILES.get(profile, VIEW_PROFILES["default"])
        with self.lock:
            meta = {
                "segment": self.name, "present": self.present,
                "label": self.label, "width": self.width, "height": self.height,
                "fps": self.fps, "seq": self.seq, "measured_fps": self.rate,
                "age_ms": self.age_ms if not self.present else 0,
                "error": self.error,
            }
            frame = self.frame_bgr
            seq = self.seq
            if profile == "default" and self._jpeg_cache:
                return self._jpeg_cache, meta
            cached = self._profile_cache.get(profile)
            if cached and cached[0] == seq:
                return cached[1], meta
        if frame is None:
            return self._placeholder(), meta
        with self._encode_lock:
            with self.lock:
                cached = self._profile_cache.get(profile)
                if cached and cached[0] == seq:
                    return cached[1], meta
            image = frame
            max_w, max_h = settings["width"], settings["height"]
            if max_w and max_h and (image.shape[1] > max_w or image.shape[0] > max_h):
                scale = min(max_w / image.shape[1], max_h / image.shape[0])
                size = (max(1, int(image.shape[1] * scale)),
                        max(1, int(image.shape[0] * scale)))
                if _jpeg[0] == "cv2":
                    image = _jpeg[1].resize(image, size, interpolation=_jpeg[1].INTER_AREA)
                else:
                    from PIL import Image
                    image = np.asarray(Image.fromarray(image[:, :, ::-1]).resize(size))[:, :, ::-1]
            jpg = encode_jpeg(image, settings["quality"])
            with self.lock:
                if self.seq == seq and jpg:
                    self._profile_cache[profile] = (seq, jpg)
            return jpg or self._placeholder(), meta

    def _placeholder(self):
        if self._jpeg_cache is None:
            self._jpeg_cache = placeholder_jpeg(f"{self.name}: 无共享流")
        return self._jpeg_cache

    def health(self):
        with self.lock:
            return {
                "segment": self.name, "present": self.present, "label": self.label,
                "size": f"{self.width}x{self.height}", "fps_header": self.fps,
                "fps_measured": self.rate, "seq": self.seq, "error": self.error,
            }


def select_direct_profiles(device, rs):
    """按实际支持的彩色流选择高帧率；同帧率优先原生 JPEG。"""
    formats = [rs.format.mjpeg, rs.format.bgr8, rs.format.rgb8, rs.format.yuyv]
    profiles = {}
    for sensor in device.query_sensors():
        for item in sensor.get_stream_profiles():
            if item.stream_type() != rs.stream.color or item.format() not in formats:
                continue
            video = item.as_video_stream_profile()
            key = (video.width(), video.height(), video.fps(), video.format())
            profiles[key] = video
    candidates = list(profiles.values())
    if DIRECT_WEBRTC:
        raw = [profile for profile in candidates if profile.format() != rs.format.mjpeg]
        if raw:
            candidates = raw
    sized = [p for p in candidates if 640 <= p.width() <= 1280 and 360 <= p.height() <= 720]
    if DIRECT_WEBRTC:
        priorities = {rs.format.yuyv: 0, rs.format.bgr8: 1, rs.format.rgb8: 2}
        return sorted(sized or candidates, key=lambda p: (
            -p.fps(), priorities.get(p.format(), 9), -p.width() * p.height()))
    return sorted(sized or candidates, key=lambda p: (
        -p.fps(), formats.index(p.format()), -p.width() * p.height()))


def _video_device_holders(exclude_pid=None):
    """列出打开 /dev/video* 的进程，用于诊断 EBUSY(errno=16) 占用者。"""
    holders = {}
    scan_complete = True
    try:
        entries = os.listdir("/proc")
    except OSError:
        return holders, False
    for entry in entries:
        if not entry.isdigit() or (exclude_pid is not None and int(entry) == exclude_pid):
            continue
        fd_dir = os.path.join("/proc", entry, "fd")
        try:
            fds = os.listdir(fd_dir)
        except PermissionError:
            scan_complete = False
            continue
        except FileNotFoundError:
            continue
        except OSError:
            scan_complete = False
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except FileNotFoundError:
                continue
            except PermissionError:
                scan_complete = False
                continue
            except OSError:
                continue
            if target.startswith("/dev/video"):
                holders.setdefault(target, set()).add(int(entry))
    return holders, scan_complete


def _describe_busy(exc):
    """EBUSY 时附加占用者线索，否则原样返回错误文本。"""
    text = str(exc)
    if "errno=16" not in text and "resource busy" not in text.lower():
        lowered = text.lower()
        if "frame didn't arrive" in lowered:
            return "相机取帧超时"
        if "camera disconnected" in lowered:
            return "相机已断开"
        return text[:160]
    holders, scan_complete = _video_device_holders(exclude_pid=os.getpid())
    if not holders:
        if not scan_complete:
            return "相机设备忙；占用进程检查不完整"
        return "相机设备忙；未查到其他进程占用"
    names = []
    for pid in sorted({pid for pids in holders.values() for pid in pids})[:8]:
        try:
            comm = Path(os.path.join("/proc", str(pid), "comm")).read_text(
                encoding="utf-8").strip()
        except OSError:
            comm = "?"
        names.append("%d(%s)" % (pid, comm))
    nodes = ", ".join(sorted(holders))
    suffix = "；占用线索 %s：%s" % (nodes, ", ".join(names))
    if not scan_complete:
        suffix += "（检查不完整）"
    return "相机设备忙" + suffix


class DirectCameraReader:
    """相机独占读取最新帧；网络与编码不阻塞相机读取线程。"""

    def __init__(self, name, serial, rs, usb_port=""):
        self.name, self.serial, self.rs = name, serial, rs
        self.usb_port = usb_port
        self._usb_info = None
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.jpeg = None
        self.raw = None
        self.pixel_format = None
        self.width = self.height = self.seq = self.source_fps = 0
        self.fps = 0.0
        self.frame_timeouts = 0
        self.capture_interval_ms = None
        self._last_capture_at = 0.0
        self.exposure_diagnostics = {}
        self.captured_at = 0.0
        self.present = False
        self.error = "starting"
        self._stop = threading.Event()
        self._thread = None
        self._pipeline = None
        self._first_frameset = None
        self._profile_cache = {}
        self._encode_lock = threading.Lock()
        self._placeholder_cache = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name=f"direct-{self.name}", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self.condition:
            self.condition.notify_all()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _open(self):
        devices = self.rs.context().query_devices()
        device = next((d for d in devices if d.get_info(self.rs.camera_info.serial_number) == self.serial), None)
        if device is None:
            raise RuntimeError("camera disconnected")
        profiles = select_direct_profiles(device, self.rs)
        if not profiles:
            raise RuntimeError("no supported color stream")
        errors = []
        for video in profiles:
            if self._stop.is_set():
                raise RuntimeError("camera stopped")
            pipeline = self.rs.pipeline()
            config = self.rs.config()
            config.enable_device(self.serial)
            config.enable_stream(self.rs.stream.color, video.width(), video.height(),
                                 video.format(), video.fps())
            try:
                active = pipeline.start(config)
            except Exception as exc:
                errors.append(str(exc))
                try:
                    pipeline.stop()
                except Exception:
                    pass
                continue
            try:
                for sensor in active.get_device().query_sensors():
                    sensor_options = self.exposure_diagnostics.setdefault("sensor_options", {})
                    for key, value in (("frames_queue_size", 1), ("global_time_enabled", 1),
                                       ("auto_exposure_priority", 0)):
                        option = getattr(self.rs.option, key, None)
                        sensor_options[key] = bool(option is not None and sensor.supports(option))
                        if option is not None and sensor.supports(option):
                            try:
                                sensor.set_option(option, value)
                                if key == "auto_exposure_priority":
                                    sensor_options[key + "_value"] = sensor.get_option(option)
                            except Exception:
                                pass
                    exposure = getattr(self.rs.option, "exposure", None)
                    auto_limit = getattr(self.rs.option, "auto_exposure_limit", None)
                    if exposure is not None and sensor.supports(exposure):
                        try:
                            self.exposure_diagnostics["exposure"] = sensor.get_option(exposure)
                        except Exception:
                            pass
                    sensor_options["exposure"] = bool(exposure is not None and sensor.supports(exposure))
                    sensor_options["auto_exposure_limit"] = bool(
                        auto_limit is not None and sensor.supports(auto_limit))
                    if auto_limit is not None and sensor.supports(auto_limit):
                        try:
                            option_range = sensor.get_option_range(auto_limit)
                            frame_budget_ms = 1000.0 / max(1, video.fps())
                            limit = min(option_range.max, max(option_range.min,
                                         frame_budget_ms))
                            sensor.set_option(auto_limit, limit)
                            self.exposure_diagnostics["auto_exposure_limit_ms"] = limit
                        except Exception as exc:
                            self.exposure_diagnostics["auto_exposure_limit_error"] = str(exc)
                first_frameset = pipeline.wait_for_frames(DIRECT_FRAME_TIMEOUT_MS)
                if not first_frameset.get_color_frame():
                    raise RuntimeError("camera returned no color frame")
            except RuntimeError as exc:
                errors.append("%dx%d@%d: %s" %
                              (video.width(), video.height(), video.fps(), exc))
                try:
                    pipeline.stop()
                except Exception:
                    pass
                continue
            self._pipeline = pipeline
            self._first_frameset = first_frameset
            self.source_fps = video.fps()
            self.pixel_format = video.format()
            print(f"[direct] {self.name}: {video.width()}x{video.height()} "
                  f"{video.fps()}fps {video.format()}", flush=True)
            return pipeline, first_frameset
        raise RuntimeError("; ".join(errors[-2:]))

    def _run(self):
        while not self._stop.is_set():
            pipeline = None
            try:
                pipeline, first_frameset = self._open()
                last_rate = time.monotonic()
                frames = 0
                frame_timeouts = 0
                while not self._stop.is_set():
                    if first_frameset is not None:
                        frameset, first_frameset = first_frameset, None
                    else:
                        try:
                            frameset = pipeline.wait_for_frames(DIRECT_FRAME_TIMEOUT_MS)
                        except RuntimeError as exc:
                            if ("frame didn't arrive" in str(exc).lower() and
                                    frame_timeouts < DIRECT_FRAME_TIMEOUT_RETRIES):
                                frame_timeouts += 1
                                self.frame_timeouts += 1
                                continue
                            raise
                        frame_timeouts = 0
                    frame = frameset.get_color_frame()
                    if not frame:
                        continue
                    captured_at = time.monotonic()
                    if self._last_capture_at:
                        self.capture_interval_ms = round((captured_at - self._last_capture_at) * 1000, 1)
                    self._last_capture_at = captured_at
                    data = bytes(frame.get_data())
                    frames += 1
                    with self.condition:
                        if self.pixel_format == self.rs.format.mjpeg:
                            self.jpeg, self.raw = data, None
                        else:
                            self.jpeg, self.raw = None, data
                        self.width, self.height = frame.get_width(), frame.get_height()
                        self.seq += 1
                        self.captured_at = captured_at
                        self.present, self.error = True, ""
                        if captured_at - last_rate >= 1.0:
                            self.fps = round(frames / (captured_at - last_rate), 1)
                            frames, last_rate = 0, captured_at
                        self.condition.notify_all()
                    if WEBRTC is not None and self.name in ("primary", "d435i", "third"):
                        WEBRTC.push({"primary": "wrist", "d435i": "global",
                                     "third": "third"}.get(self.name, self.name),
                                    data, frame.get_width(), frame.get_height(),
                                    self.pixel_format, captured_at)
            except Exception as exc:
                detail = _describe_busy(exc)
                if detail != str(exc):
                    print(f"[direct] {self.name}: {detail}", flush=True)
                with self.condition:
                    self.present = False
                    self.error = f"direct camera read failed: {detail}"
                    self.condition.notify_all()
            finally:
                if pipeline is not None:
                    try:
                        pipeline.stop()
                    except Exception:
                        pass
                self._pipeline = None
            self._stop.wait(0.5)

    def _meta(self):
        age = max(0, round((time.monotonic() - self.captured_at) * 1000)) if self.captured_at else None
        present = self.present and age is not None and age < 1500
        return {"segment": self.name, "present": present, "label": self.name,
                "width": self.width, "height": self.height, "fps": self.source_fps,
                "source_fps": self.source_fps, "seq": self.seq, "measured_fps": self.fps,
                "captured_at": self.captured_at, "age_ms": age,
                "frame_timeouts": self.frame_timeouts,
                "capture_interval_ms": self.capture_interval_ms,
                "exposure": dict(self.exposure_diagnostics),
                "usb": self._usb_diagnostics(),
                "error": self.error or ("camera stalled" if not present else "")}

    def _usb_diagnostics(self):
        if self._usb_info is not None:
            return dict(self._usb_info)
        result = {"topology": self.usb_port or "unknown", "speed_mbps": None}
        if not self.usb_port:
            self._usb_info = result
            return result
        try:
            speed_path = Path("/sys/bus/usb/devices") / self.usb_port / "speed"
            result["speed_mbps"] = int(float(speed_path.read_text().strip()))
        except (OSError, ValueError):
            pass
        self._usb_info = result
        return result

    def _placeholder(self):
        if self._placeholder_cache is None:
            self._placeholder_cache = placeholder_jpeg(f"{self.name}: camera offline")
        return self._placeholder_cache

    def wait_snapshot(self, after_seq, profile="default", timeout=1.0):
        with self.condition:
            self.condition.wait_for(lambda: self.seq != after_seq or self._stop.is_set(), timeout)
            if self.seq == after_seq or self._stop.is_set():
                return None, self._meta()
        return self.snapshot(profile)

    def snapshot(self, profile="default"):
        settings = VIEW_PROFILES.get(profile, VIEW_PROFILES["default"])
        with self._encode_lock:
            with self.lock:
                meta = self._meta()
                jpg, raw = self.jpeg, self.raw
                width, height, pixel_format = self.width, self.height, self.pixel_format
                if jpg and profile in ("default", "remote", "control"):
                    return jpg, meta
                cached = self._profile_cache.get(profile)
                if cached and cached[0] == meta["seq"]:
                    return cached[1], meta
            if not jpg and not raw:
                return self._placeholder(), meta
            if _jpeg[0] == "cv2":
                cv2 = _jpeg[1]
                if jpg:
                    image = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                elif pixel_format == self.rs.format.yuyv:
                    image = cv2.cvtColor(np.frombuffer(raw, np.uint8).reshape(height, width, 2), cv2.COLOR_YUV2BGR_YUY2)
                else:
                    image = np.frombuffer(raw, np.uint8).reshape(height, width, 3)
                    if pixel_format == self.rs.format.rgb8:
                        image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
            else:
                import io
                from PIL import Image
                if jpg:
                    image = np.asarray(Image.open(io.BytesIO(jpg)).convert("RGB"))[:, :, ::-1]
                elif pixel_format == self.rs.format.yuyv:
                    raise RuntimeError("YUYV color requires OpenCV")
                else:
                    image = np.frombuffer(raw, np.uint8).reshape(height, width, 3)
                    if pixel_format == self.rs.format.rgb8:
                        image = image[:, :, ::-1]
            if image is None:
                return self._placeholder(), meta
            max_w, max_h = settings["width"], settings["height"]
            if max_w and max_h and (image.shape[1] > max_w or image.shape[0] > max_h):
                scale = min(max_w / image.shape[1], max_h / image.shape[0])
                size = (max(1, int(image.shape[1] * scale)), max(1, int(image.shape[0] * scale)))
                if _jpeg[0] == "cv2":
                    image = _jpeg[1].resize(image, size, interpolation=_jpeg[1].INTER_AREA)
                else:
                    from PIL import Image
                    image = np.asarray(Image.fromarray(image[:, :, ::-1]).resize(size))[:, :, ::-1]
            result = encode_jpeg(image, settings["quality"])
            if result:
                with self.lock:
                    self._profile_cache[profile] = (meta["seq"], result)
            return result or self._placeholder(), meta

    def health(self):
        with self.lock:
            meta = self._meta()
            return {"segment": self.name, "present": meta["present"], "label": self.name,
                    "size": f"{self.width}x{self.height}" if self.width else "-",
                    "fps_header": self.source_fps, "fps_measured": self.fps,
                    "seq": self.seq, "age_ms": meta["age_ms"], "error": meta["error"],
                    "frame_timeouts": self.frame_timeouts,
                    "capture_interval_ms": self.capture_interval_ms,
                    "exposure": dict(self.exposure_diagnostics),
                    "usb": self._usb_diagnostics()}


def normalize_camera_port(port):
    marker = "/video4linux/"
    if marker in port:
        port = port.split(marker, 1)[0]
    slash = port.rfind("/")
    colon = port.find(":", slash + 1)
    return port[:colon] if colon >= 0 else port


def direct_camera_segments():
    try:
        import pyrealsense2 as rs
    except Exception as exc:
        raise RuntimeError(f"pyrealsense2 不可用，无法直连相机：{exc}")

    devices = []
    for device in rs.context().query_devices():
        if not device.supports(rs.camera_info.serial_number):
            continue
        serial = device.get_info(rs.camera_info.serial_number)
        physical_port = getattr(rs.camera_info, "physical_port", None)
        port = (device.get_info(physical_port)
                if physical_port is not None and device.supports(physical_port) else "")
        devices.append({"serial": serial, "port": normalize_camera_port(port)})

    if len(devices) < 3:
        raise RuntimeError(f"直连预览需要三台 RealSense，相机发现 {len(devices)} 台")

    config_path = os.environ.get("ARMDOG_CAMERA_PORTS_FILE")
    if not config_path:
        config_path = str(Path(__file__).resolve().parents[1] /
                          "prog" / "rl_deploy" / "camera_ports.conf")
    remembered = {}
    try:
        for line in Path(config_path).read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                remembered[key.strip()] = normalize_camera_port(value.strip())
    except OSError:
        pass

    roles = {"primary": None, "third": None, "d435i": None}
    unused = sorted(devices, key=lambda item: (item["port"], item["serial"]))
    for role in roles:
        port = remembered.get(role)
        match = next((item for item in unused if port and item["port"] == port), None)
        if match:
            roles[role] = match
            unused.remove(match)
    for role in roles:
        if roles[role] is None and unused:
            roles[role] = unused.pop(0)
    return [DirectCameraReader(role, roles[role]["serial"], rs, roles[role]["port"])
            for role in ("primary", "third", "d435i")]


SEGMENTS = []


def start_segments(segments):
    for segment in segments:
        segment.start()


class NullSegment:
    """为未配置通道提供占位图，保持接口一致。"""

    def __init__(self, text):
        self.name = text
        self.present = False
        self.error = "not configured"
        self._jpg = None

    def start(self):
        pass

    def stop(self):
        pass

    def snapshot(self, profile="default"):
        if self._jpg is None:
            self._jpg = placeholder_jpeg(self.name)
        return self._jpg, {"segment": self.name, "present": False, "label": "",
                           "width": 0, "height": 0, "fps": 0, "seq": -1,
                           "measured_fps": 0.0, "age_ms": None,
                           "error": "not configured"}

    def health(self):
        return {"segment": self.name, "present": False, "label": "", "size": "-",
                "fps_header": 0, "fps_measured": 0.0, "seq": -1,
                "error": "not configured"}


def segment_for_index(n):
    idx = n - 1
    if 0 <= idx < len(SEGMENTS):
        return SEGMENTS[idx]
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "camShmViewer/1.0"

    def log_message(self, fmt, *args):
        pass

    def _authorized_camera(self, parsed):
        supplied = (parse_qs(parsed.query).get("token") or [""])[0]
        return bool(ACCESS_TOKEN and supplied and
                    hmac.compare_digest(ACCESS_TOKEN, supplied))

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith(("/health", "/snapshot", "/video")) or path == "/":
            if not self._authorized_camera(parsed):
                return self._send(401, "application/json", b'{"error":"unauthorized"}')
        if path == "/":
            return self._send(200, "text/html; charset=utf-8", self._index().encode())
        if path == "/health":
            return self._send(200, "application/json; charset=utf-8",
                              json.dumps({"segments": [s.health() for s in SEGMENTS]},
                                         ensure_ascii=False).encode())
        if path.startswith("/snapshot") and path.endswith(".jpg"):
            num = path[len("/snapshot"):-len(".jpg")]
            seg = segment_for_index(int(num)) if num.isdigit() else None
            if seg is None:
                return self._send(404, "text/plain", b"no such segment")
            jpg, _ = seg.snapshot()
            return self._send(200, "image/jpeg", jpg)
        if path.startswith("/video"):
            num = path[len("/video"):]
            seg = segment_for_index(int(num)) if num.isdigit() else None
            if seg is None:
                return self._send(404, "text/plain; charset=utf-8",
                                  f"无此路: {path} (共 {len(SEGMENTS)} 段)".encode())
            profile = (parse_qs(parsed.query).get("profile") or ["default"])[0]
            if profile not in VIEW_PROFILES:
                return self._send(400, "text/plain", b"invalid preview profile")
            return self._mjpeg(seg, profile)
        return self._send(404, "text/plain", b"not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path not in ("/webrtc/offer", "/webrtc/close", "/webrtc/quality", "/webrtc/stats") or WEBRTC is None:
            return self._send(404, "application/json", b'{"error":"not found"}')
        supplied = self.headers.get("X-DM-WebRTC-Token", "")
        if not WEBRTC_TOKEN or not hmac.compare_digest(supplied, WEBRTC_TOKEN):
            return self._send(403, "application/json", b'{"error":"forbidden"}')
        try:
            size = int(self.headers.get("Content-Length") or 0)
            if size <= 0 or size > 262144:
                raise ValueError("信令内容长度无效")
            body = json.loads(self.rfile.read(size))
            if parsed.path.endswith("offer"):
                result = WEBRTC.offer(body)
            elif parsed.path.endswith("quality"):
                result = WEBRTC.set_quality(body.get("session_id"), body.get("profile"))
            elif parsed.path.endswith("stats"):
                result = WEBRTC.stats(body.get("session_id"))
            else:
                result = WEBRTC.close(str(body.get("session_id") or ""))
            encoded = json.dumps(result, ensure_ascii=False).encode()
            return self._send(200, "application/json; charset=utf-8", encoded)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            return self._send(400, "application/json; charset=utf-8",
                              json.dumps({"error": str(exc)}, ensure_ascii=False).encode())
        except Exception as exc:  # noqa: BLE001
            return self._send(500, "application/json; charset=utf-8",
                              json.dumps({"error": "WebRTC 建连失败：%s" % exc},
                                         ensure_ascii=False).encode())

    def _mjpeg(self, seg, profile="default"):
        settings = VIEW_PROFILES.get(profile, VIEW_PROFILES["default"])
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 5)
        except (AttributeError, OSError):
            try:
                os.nice(5)
            except OSError:
                pass
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
        self.connection.settimeout(1.0)
        for level, option, value in (
                (socket.IPPROTO_IP, socket.IP_TOS, 0x20),
                (socket.SOL_SOCKET, socket.SO_PRIORITY, 1)):
            try:
                self.connection.setsockopt(level, option, value)
            except OSError:
                pass
        notsent = getattr(socket, "TCP_NOTSENT_LOWAT", None)
        if notsent is not None:
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, notsent, 16 * 1024)
            except OSError:
                pass
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        last_seq = -1
        stale_sent = False
        last_emit = 0.0
        try:
            while True:
                if hasattr(seg, "wait_snapshot"):
                    jpg, meta = seg.wait_snapshot(last_seq, profile)
                    if jpg is None:
                        continue
                else:
                    jpg, meta = seg.snapshot(profile)

                if meta["seq"] == last_seq and meta["present"]:
                    time.sleep(0.005)
                    continue

                if meta["seq"] == last_seq and not meta["present"]:
                    if stale_sent:
                        time.sleep(0.2)
                    stale_sent = True
                else:
                    limit = settings["fps"]
                    wait = (1.0 / limit - (time.monotonic() - last_emit)) if limit else 0
                    if wait > 0:
                        time.sleep(wait)
                        jpg, meta = seg.snapshot(profile)
                        if meta["seq"] == last_seq:
                            continue
                    last_seq = meta["seq"]
                    stale_sent = False
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                 b"Content-Length: " + str(len(jpg)).encode() +
                                 b"\r\nX-Frame-Seq: " + str(meta["seq"]).encode() +
                                 b"\r\nX-Capture-Time: " + str(round(meta.get("captured_at", 0) * 1000)).encode() +
                                 b"\r\n\r\n" + jpg + b"\r\n")
                self.wfile.flush()
                last_emit = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _index(self):
        cells = []
        for i, seg in enumerate(SEGMENTS, 1):
            cells.append(
                f'<figure><img src="/video{i}?token={ACCESS_TOKEN}" alt="cam{i}">'
                f'<figcaption><b>camera{i}</b> · {seg.name}</figcaption></figure>')
        return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>cam shm viewer</title><style>
body{{margin:0;background:#141414;color:#eee;font:14px/1.5 system-ui,sans-serif}}
h1{{font-size:15px;font-weight:600;padding:14px 18px;margin:0;border-bottom:1px solid #2a2a2a}}
main{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px;padding:18px}}
figure{{margin:0;background:#1c1c1c;border-radius:12px;overflow:hidden}}
img{{display:block;width:100%;aspect-ratio:4/3;object-fit:contain;background:#000}}
figcaption{{padding:8px 10px;color:#9a9a9a;font-size:12px}}
</style></head><body><h1>rl_deploy 共享内存预览 · 不占用相机设备</h1>
<main>{''.join(cells)}</main></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="从 rl_deploy 的 POSIX shm 读帧做 MJPEG 预览")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--cam1", default=DEFAULT_SEGMENT)
    ap.add_argument("--cam2", default="")
    ap.add_argument("--cam3", default="")
    ap.add_argument("--direct", action="store_true",
                    help="直接打开三台 RealSense 彩色流，不读取共享内存")
    ap.add_argument("--webrtc", action="store_true",
                    help="启用仅控制模式的 H.264 WebRTC 视频")
    ap.add_argument("--webrtc-token", default="")
    ap.add_argument("--auto", action="store_true",
                    help="按多路发布约定自动映射: base / base_third / base_d435i "
                         "(base 取 --cam1 或默认名); 尚未发布的那几路显示占位, 上线后自动亮")
    ap.add_argument("--list", action="store_true", help="只打印 /dev/shm 里的段, 不起服务")
    args = ap.parse_args()

    global WEBRTC, WEBRTC_TOKEN, DIRECT_WEBRTC, ACCESS_TOKEN
    DIRECT_WEBRTC = bool(args.webrtc)
    ACCESS_TOKEN = os.environ.get("DM_CAM_TOKEN", "")
    if not ACCESS_TOKEN:
        sys.exit("[FATAL] 缺少相机访问令牌")
    if args.webrtc:
        WEBRTC_TOKEN = args.webrtc_token or os.environ.get("DM_WEBRTC_TOKEN", "")
        if not args.direct or not WEBRTC_TOKEN:
            sys.exit("[FATAL] WebRTC 需要直连相机和内部信令令牌")

    _load_encoder()

    if args.direct:
        try:
            SEGMENTS.extend(direct_camera_segments())
        except Exception as exc:
            sys.exit(f"[FATAL] direct camera preview: {exc}")
    elif args.auto:
        base = args.cam1 or DEFAULT_SEGMENT
        args.cam1 = base
        args.cam2 = args.cam2 or base + "_third"
        args.cam3 = args.cam3 or base + "_d435i"
    names = [args.cam1, args.cam2, args.cam3]
    if args.list:
        print("可用 shm 段:")
        for entry in sorted(os.listdir(SHM_DIR)):
            p = os.path.join(SHM_DIR, entry)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_size >= HEADER_BYTES:
                print(f"  {entry:<32} {st.st_size:>10} B")
        print(f"默认读取: {names}")
        return

    if not args.direct:
        for i, n in enumerate(names, 1):
            if n:
                seg = SegmentReader(n)
            else:
                seg = NullSegment(f"camera{i}: 该路未开共享流")
            SEGMENTS.append(seg)
        if not any(not isinstance(s, NullSegment) for s in SEGMENTS):
            sys.exit("[FATAL] 没有配置任何共享内存段")

    start_segments(SEGMENTS)
    stun_server = None
    if args.webrtc:
        try:
            from webrtc_preview import session_manager, LocalStunServer
            stun_server = LocalStunServer(args.host, args.port)
            WEBRTC = session_manager(SEGMENTS)
        except Exception as exc:
            sys.exit("[FATAL] WebRTC 初始化失败：%s" % exc)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f"[camShmViewer] listening :{args.port}  mode={'direct' if args.direct else 'shm'} segments={names}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if stun_server:
            stun_server.close()
        for s in SEGMENTS:
            s.stop()


if __name__ == "__main__":
    main()

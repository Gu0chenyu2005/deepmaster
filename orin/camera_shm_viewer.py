#!/usr/bin/env python3
# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""从共享内存读取相机帧，提供 MJPEG、快照与健康检查接口。"""

import argparse
import json
import mmap
import os
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

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


def _load_encoder():
    """优先使用 OpenCV，其次为 Pillow；均不可用时退出。"""
    global _jpeg
    try:
        import cv2
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

    def snapshot(self):
        """返回 (jpeg_bytes, meta)；无画面时返回占位图。"""
        with self.lock:
            meta = {
                "segment": self.name, "present": self.present,
                "label": self.label, "width": self.width, "height": self.height,
                "fps": self.fps, "seq": self.seq, "measured_fps": self.rate,
                "age_ms": self.age_ms if not self.present else 0,
                "error": self.error,
            }
            if self._jpeg_cache:
                return self._jpeg_cache, meta
        return self._placeholder(), meta

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


SEGMENTS = []


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

    def snapshot(self):
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
        path = urlparse(self.path).path
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
            return self._mjpeg(seg)
        return self._send(404, "text/plain", b"not found")

    def _mjpeg(self, seg):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        last_seq = -1
        stale_sent = False
        try:
            while True:
                jpg, meta = seg.snapshot()

                if meta["seq"] == last_seq and meta["present"]:
                    time.sleep(0.005)
                    continue

                if meta["seq"] == last_seq and not meta["present"]:
                    if stale_sent:
                        time.sleep(0.2)
                    stale_sent = True
                else:
                    last_seq = meta["seq"]
                    stale_sent = False
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                 b"Content-Length: " + str(len(jpg)).encode() +
                                 b"\r\n\r\n" + jpg + b"\r\n")
                self.wfile.flush()
                time.sleep(1.0 / 20.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _index(self):
        cells = []
        for i, seg in enumerate(SEGMENTS, 1):
            cells.append(
                f'<figure><img src="/video{i}" alt="cam{i}">'
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
    ap.add_argument("--auto", action="store_true",
                    help="按多路发布约定自动映射: base / base_third / base_d435i "
                         "(base 取 --cam1 或默认名); 尚未发布的那几路显示占位, 上线后自动亮")
    ap.add_argument("--list", action="store_true", help="只打印 /dev/shm 里的段, 不起服务")
    args = ap.parse_args()

    _load_encoder()

    if args.auto:
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

    for i, n in enumerate(names, 1):
        if n:
            seg = SegmentReader(n)
        else:
            seg = NullSegment(f"camera{i}: 该路未开共享流")
        seg.start()
        SEGMENTS.append(seg)
    if not any(not isinstance(s, NullSegment) for s in SEGMENTS):
        sys.exit("[FATAL] 没有配置任何共享内存段")

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print(f"[camShmViewer] listening :{args.port}  segments={names}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for s in SEGMENTS:
            s.stop()


if __name__ == "__main__":
    main()

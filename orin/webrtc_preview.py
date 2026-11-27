#!/usr/bin/env python3
# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""低延迟三路 H.264/WebRTC 相机发送端。"""

import threading
import time
import os
import socket
import struct
import ipaddress


QUALITY_PROFILES = {
    "control": (2600000, 1.0),
    "control_low": (1500000, 0.75),
    "control_min": (800000, 0.5),
}


MIN_OUTPUT_WIDTH = 640
MIN_OUTPUT_HEIGHT = 360

# 首帧由相机读线程送入，DirectCameraReader 的读帧上限是 5000ms；
# 编码器就绪窗口必须大于它，再留出 PLAYING 切换与编码/打包时间。
DIRECT_FRAME_TIMEOUT_MS = 5000
ENCODER_READY_TIMEOUT = 8.0
ICE_GATHERING_TIMEOUT = 6.0
PIPELINE_PLAYING_TIMEOUT_NS = 3 * 1000000000
# 三个 SDP promise（set-remote-description / create-answer / set-local-description）
# 共用的最长等待时间。promise.wait() 不设限会无限阻塞，预算链就形同虚设。
PROMISE_TIMEOUT_NS = 5 * 1000000000
# 采集端整次 offer() 的预算上限：agent 的 HTTP 超时与浏览器信令超时都必须
# 大于它，否则上游会先断开、页面只会看到含糊的"信令失败"。
SIGNAL_BUDGET_SECONDS = (
    PIPELINE_PLAYING_TIMEOUT_NS / 1000000000
    + ENCODER_READY_TIMEOUT
    + ICE_GATHERING_TIMEOUT
    + PROMISE_TIMEOUT_NS / 1000000000
)


def pipeline_reached_playing(gst, pipeline, timeout_ns):
    """确认管线已离开 NULL/READY，进入可接收数据的状态。

    GStreamer 的 get_state() 返回 (StateChangeReturn, current, pending)，
    第一个元素是"本次调用是否达成目标"的返回值，不是状态本身；
    当前状态要看第二个元素 current。把返回值当状态比较会误判。

    对 is-live 的 appsrc 而言，管线在收到首帧前停在 PAUSED（pending=playing），
    推入数据后才转 PLAYING。等待首帧的阶段本来就处于 PAUSED，
    因此这里只排除 NULL/READY，PAUSED 属于正常状态。
    """
    result, current, pending = pipeline.get_state(timeout_ns)
    if current in (gst.State.NULL, gst.State.READY):
        raise RuntimeError("H.264 管线未启动：%s（返回值 %s）" %
                           (getattr(current, "value_nick", current),
                            getattr(result, "value_nick", result)))
    if current != gst.State.PLAYING:
        # PAUSED/pending=PLAYING 是等待首帧时的正常状态。
        print("[webrtc] 管线状态 %s（pending %s），等待首帧" %
              (getattr(current, "value_nick", current),
               getattr(pending, "value_nick", pending)), flush=True)
    return True


def quality_dimensions(width, height, scale):
    """Return even output dimensions while retaining the 640x360 floor.

    缩小后的宽高不得低于 640x360（源本身就更小时保持源尺寸），并且宽高
    必须为偶数：nvv4l2h264enc 在 NV12 下对奇数尺寸会 configure 失败。
    """
    width, height = max(2, int(width)), max(2, int(height))
    floor = min(1.0, max(MIN_OUTPUT_WIDTH / width, MIN_OUTPUT_HEIGHT / height))
    effective = min(1.0, max(float(scale), floor))
    target_w = int(width * effective)
    target_h = int(height * effective)
    if width >= MIN_OUTPUT_WIDTH and height >= MIN_OUTPUT_HEIGHT:
        target_w = max(target_w, MIN_OUTPUT_WIDTH)
        target_h = max(target_h, MIN_OUTPUT_HEIGHT)
    return (max(2, target_w // 2 * 2), max(2, target_h // 2 * 2))


def prefer_numeric_candidates(text):
    sections = []
    for line in text.splitlines():
        if not sections or line.startswith("m="):
            sections.append([])
        sections[-1].append(line)
    result = []
    for section in sections:
        numeric = False
        for line in section:
            fields = line.split()
            if line.startswith("a=candidate:") and len(fields) >= 8:
                try:
                    ipaddress.ip_address(fields[4])
                    numeric = True
                except ValueError:
                    pass
        for line in section:
            fields = line.split()
            if (numeric and line.startswith("a=candidate:") and len(fields) >= 8
                    and fields[4].lower().endswith(".local")):
                continue
            result.append(line)
    return "\r\n".join(result) + "\r\n"


class LocalStunServer:
    """为直连浏览器提供地址发现，避免依赖 mDNS。"""

    COOKIE = 0x2112A442

    def __init__(self, host, port):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.socket.bind((host, port))
            self.socket.settimeout(0.5)
        except Exception:
            self.socket.close()
            raise
        self.port = self.socket.getsockname()[1]
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, name="webrtc-stun", daemon=True)
        self.thread.start()

    @classmethod
    def binding_response(cls, data, address):
        if len(data) < 20 or len(data) > 2048:
            return None
        kind, size, cookie = struct.unpack("!HHI", data[:8])
        if kind != 1 or cookie != cls.COOKIE or size % 4 or len(data) != 20 + size:
            return None
        ip = struct.unpack("!I", socket.inet_aton(address[0]))[0]
        mapped = struct.pack("!HHBBHI", 0x20, 8, 0, 1,
                             address[1] ^ (cookie >> 16), ip ^ cookie)
        return struct.pack("!HH", 0x101, len(mapped)) + data[4:20] + mapped

    def _serve(self):
        window, count = time.monotonic(), 0
        while not self.stopped.is_set():
            try:
                data, address = self.socket.recvfrom(2049)
                now = time.monotonic()
                if now - window >= 1:
                    window, count = now, 0
                count += 1
                if count > 128:
                    continue
                response = self.binding_response(data, address)
                if response:
                    self.socket.sendto(response, address)
            except socket.timeout:
                continue
            except OSError:
                break

    def close(self):
        self.stopped.set()
        self.socket.close()
        self.thread.join(timeout=1)


class WebRTCPreview:
    def __init__(self, readers):
        self.readers = {"wrist": next(r for r in readers if r.name == "primary"),
                        "global": next(r for r in readers if r.name == "d435i"),
                        "third": next(r for r in readers if r.name == "third")}
        self.lock = threading.RLock()
        self.session = None
        self.gst = None
        self.glib = None
        self.sdp = None
        self.webrtc = None
        self.loop = None
        self.loop_thread = None
        self.monitor_thread = None
        self.monitor_started = False
        self._last_connection_log = None
        self._last_monitor_error = None
        self._last_publish_errors = {}
        self.frame_conditions = {name: threading.Condition() for name in self.readers}
        self.frame_slots = {name: None for name in self.readers}
        self.slot_overwrites = {name: 0 for name in self.readers}
        self.slot_threads = []
        for name in self.readers:
            thread = threading.Thread(target=self._consume_frames, args=(name,),
                                      name="webrtc-push-" + name, daemon=True)
            thread.start()
            self.slot_threads.append(thread)

    def _consume_frames(self, name):
        condition = self.frame_conditions[name]
        while True:
            with condition:
                condition.wait_for(lambda: self.frame_slots[name] is not None)
                item, self.frame_slots[name] = self.frame_slots[name], None
            try:
                self._push_frame(name, *item)
            except Exception as exc:
                message = str(exc)
                if message != self._last_publish_errors.get(name):
                    print("[webrtc] %s 帧发布失败：%s" % (name, message), flush=True)
                    self._last_publish_errors[name] = message

    def _stage_frame_rate(self, values, name, now):
        sample = values.setdefault(name, {"count": 0, "at": now, "fps": 0.0})
        sample["count"] += 1
        elapsed = now - sample["at"]
        if elapsed >= 1.0:
            sample["fps"] = round(sample["count"] / elapsed, 1)
            sample["count"], sample["at"] = 0, now
        return sample["fps"]

    def stats(self, session_id):
        with self.lock:
            state = self.session
            if not state or state.get("id") != str(session_id or ""):
                return {"ok": False, "cameras": {}}
            now = time.monotonic()
            cameras = {}

            def _fresh_fps(rate_map, cam_name):
                """帧率统计超过 1.5s 未更新即视为 0。

                视频流中断后最后一次采样仍留在表里，若直接返回它，
                前端会误判 publish/encode 正常而不降档，画面已黑却占着高码率。
                """
                sample = rate_map.get(cam_name)
                if not sample:
                    return 0
                if "at" in sample and (now - sample["at"]) > 1.5:
                    return 0.0
                return sample.get("fps", 0)

            for name, reader in self.readers.items():
                meta = reader._meta()
                cameras[name] = {
                    "source_fps": meta.get("source_fps", 0),
                    "capture_fps": meta.get("measured_fps", 0),
                    "capture_timeouts": meta.get("frame_timeouts", 0),
                    "capture_interval_ms": meta.get("capture_interval_ms"),
                    "queue_overwrites": self.slot_overwrites.get(name, 0),
                    "publish_fps": _fresh_fps(state.get("publish_rate", {}), name),
                    "encode_fps": _fresh_fps(state.get("encode_rate", {}), name),
                    "rtp_packets": state.get("rtp_counts", {}).get(name, 0),
                    "present": meta.get("present", False),
                    "exposure": meta.get("exposure", {}),
                    "usb": meta.get("usb", {}),
                }
            return {"ok": True, "quality": state.get("quality", "control"),
                    "cameras": cameras, "sampled_at": now}

    def _load(self):
        if self.gst:
            return
        try:
            import gi
            gi.require_version("Gst", "1.0")
            gi.require_version("GstSdp", "1.0")
            gi.require_version("GstWebRTC", "1.0")
            from gi.repository import GLib, Gst, GstSdp, GstWebRTC
            Gst.init(None)
        except Exception as exc:
            raise RuntimeError("GStreamer WebRTC Python 依赖不可用：%s" % exc)
        for name in ("webrtcbin", "nicesrc", "nicesink", "nvv4l2h264enc",
                     "h264parse", "rtph264pay"):
            if not Gst.ElementFactory.find(name):
                raise RuntimeError("缺少 GStreamer 插件：%s" % name)
        self.gst, self.glib, self.sdp, self.webrtc = Gst, GLib, GstSdp, GstWebRTC
        self.loop = GLib.MainLoop()
        self.loop_thread = threading.Thread(target=self.loop.run,
                                            name="webrtc-glib", daemon=True)
        self.loop_thread.start()
        if not self.monitor_started:
            self.monitor_started = True
            self.monitor_thread = threading.Thread(target=self._monitor,
                                                   name="webrtc-session-watch", daemon=True)
            self.monitor_thread.start()

    def _monitor(self):
        while True:
            time.sleep(2)
            close_id = None
            try:
                with self.lock:
                    state = self.session
                    if not state or not state.get("pc"):
                        continue
                    connection = state["pc"].get_property("connection-state")
                    name = getattr(connection, "value_nick", str(connection)).lower()
                    ice = state["pc"].get_property("ice-connection-state")
                    ice_name = getattr(ice, "value_nick", str(ice)).lower()
                    pair = (name, ice_name)
                    if pair != self._last_connection_log:
                        print("[webrtc] connection=%s ice=%s" % pair, flush=True)
                        self._last_connection_log = pair
                    now = time.monotonic()
                    if now - state.get("last_stats_log", 0) >= 5:
                        state["last_stats_log"] = now
                        counts = state.get("frame_counts", {})
                        packets = state.get("rtp_counts", {})
                        print("[webrtc] video frames=%s rtp_packets=%s" %
                              (counts, packets), flush=True)
                    bus = state["pipeline"].get_bus()
                    failed = False
                    if bus is not None:
                        # pop_filtered 在部分 GStreamer 版本上不可用，退回 pop()
                        pop_fn = getattr(bus, "pop", None)
                        if pop_fn is not None:
                            message = pop_fn()
                            while message:
                                if message.type == self.gst.MessageType.ERROR:
                                    error, debug = message.parse_error()
                                    print("[webrtc] pipeline error: %s (%s)" % (error, debug), flush=True)
                                    failed = True
                                elif message.type == self.gst.MessageType.EOS:
                                    print("[webrtc] pipeline reached EOS", flush=True)
                                    failed = True
                                message = pop_fn()
                        else:
                            message_types = self.gst.MessageType.ERROR | self.gst.MessageType.EOS
                            message = bus.pop_filtered(message_types)
                            while message:
                                if message.type == self.gst.MessageType.ERROR:
                                    error, debug = message.parse_error()
                                    print("[webrtc] pipeline error: %s (%s)" % (error, debug), flush=True)
                                else:
                                    print("[webrtc] pipeline reached EOS", flush=True)
                                failed = True
                                message = bus.pop_filtered(message_types)
                    if failed:
                        close_id = state["id"]
                    elif name == "connected":
                        state["touched"] = now
                        state["ever_connected"] = True
                        if not state.get("keyframe_requested"):
                            self._request_keyframes(state)
                            state["keyframe_requested"] = True
                    elif name in ("failed", "disconnected", "closed"):
                        state.setdefault("disconnected_at", now)
                        if now - state["disconnected_at"] >= 6:
                            close_id = state["id"]
                    else:
                        state.pop("disconnected_at", None)
                        # 正在协商（_await_encoders/_answer 未返回）的会话不能
                        # 被这条"从未连上"规则回收，否则等待中的有效会话会被
                        # monitor 中途关掉，表现为随机的信令失败。
                        if (not state.get("ever_connected")
                                and not state.get("negotiating")
                                and now - state.get("created_at", now) >= 20):
                            close_id = state["id"]
                if close_id:
                    self.close(close_id)
            except Exception as exc:
                message = str(exc)
                now = time.monotonic()
                if (message != self._last_monitor_error
                        or now - getattr(self, "_last_monitor_error_at", 0) >= 10):
                    print("[webrtc] 状态检查失败：%s" % message, flush=True)
                    self._last_monitor_error = message
                    self._last_monitor_error_at = now

    @staticmethod
    def _request_keyframes(state):
        for encoder in state.get("encoders", {}).values():
            try:
                encoder.emit("force-IDR")
            except Exception:
                pass

    def set_quality(self, session_id, profile):
        """调整码率；默认不在线重建 caps，避免 GStreamer 热改分辨率崩溃。"""
        profile = str(profile or "")
        settings = QUALITY_PROFILES.get(profile)
        if not settings:
            raise ValueError("不支持的视频质量档位")
        bitrate, scale = settings
        with self.lock:
            state = self.session
            if not state or state.get("id") != str(session_id or ""):
                return {"ok": False, "changed": False}
            previous = state.get("quality", "control")
            for encoder in state.get("encoders", {}).values():
                encoder.set_property("bitrate", bitrate)
            # A caps change while PLAYING can leave nvvidconv/encoder in an
            # invalid state. Resolution changes are therefore opt-in and must
            # be coordinated by a full pipeline restart.
            if state.get("allow_hot_resize", False):
                for scaler, source_size in state.get("scalers", {}).values():
                    width, height = quality_dimensions(*source_size, scale)
                    caps = self.gst.Caps.from_string(
                        "video/x-raw(memory:NVMM),format=NV12,width=%d,height=%d" %
                        (width, height))
                    scaler.set_property("caps", caps)
            state["quality"] = profile
            state["touched"] = time.monotonic()
            if previous != profile:
                print("[webrtc] quality=%s bitrate=%d" % (profile, bitrate), flush=True)
            return {"ok": True, "changed": previous != profile,
                    "profile": profile, "bitrate": bitrate, "scale": scale}

    @staticmethod
    def _promise_value(gst, element, signal, *args, timeout_ns=None):
        """等待 GStreamer Promise 应答。

        Gst.Promise.wait() 没有超时参数，无限阻塞会让 SIGNAL_BUDGET_SECONDS
        形同虚设。用 promise.interrupt() 从定时器线程打断：wait() 会立即
        返回 INTERRUPTED，从而把等待限定在预算内。
        """
        promise = gst.Promise.new()
        element.emit(signal, *args, promise)
        timeout_ns = timeout_ns if timeout_ns is not None else PROMISE_TIMEOUT_NS
        timed_out = threading.Event()

        def on_timeout():
            timed_out.set()
            try:
                promise.interrupt()
            except Exception:
                pass

        timer = threading.Timer(timeout_ns / 1000000000.0, on_timeout)
        timer.daemon = True
        timer.start()
        try:
            result = promise.wait()
        finally:
            timer.cancel()
        if timed_out.is_set() or result == gst.PromiseResult.INTERRUPTED:
            raise TimeoutError("GStreamer %s 等待应答超时（%dms）" %
                               (signal, timeout_ns // 1000000))
        if result != gst.PromiseResult.REPLIED:
            raise RuntimeError("GStreamer %s 未完成" % signal)
        reply = promise.get_reply()
        if reply is not None and reply.has_field("error"):
            raise RuntimeError("GStreamer %s: %s" % (signal, reply.get_value("error")))
        return reply

    def _parse_offer(self, text):
        text = prefer_numeric_candidates(text)
        _, message = self.sdp.SDPMessage.new()
        if self.sdp.sdp_message_parse_buffer(text.encode(), message) != self.sdp.SDPResult.OK:
            raise ValueError("浏览器 SDP offer 格式错误")
        if message.medias_len() != len(self.readers):
            raise ValueError("浏览器 SDP offer 必须包含三路视频")
        codecs = []
        for index in range(message.medias_len()):
            media = message.get_media(index)
            if media.get_media() != "video":
                raise ValueError("浏览器 SDP offer 含有非视频轨道")
            for i in range(media.formats_len()):
                fmt = media.get_format(i)
                if not fmt.isascii() or not fmt.isdecimal() or not 96 <= int(fmt) <= 127:
                    continue
                caps = media.get_caps_from_media(int(fmt))
                if caps is None or caps.is_empty():
                    continue
                info = caps.get_structure(0)
                profile = info.get_string("profile")
                if (info.get_string("encoding-name") == "H264" and
                        info.get_string("packetization-mode") == "1" and
                        profile in ("baseline", "constrained-baseline")):
                    codecs.append((int(fmt), profile))
                    break
            else:
                raise ValueError("第 %d 路浏览器视频不支持 H.264 Baseline" % (index + 1))
        return self.webrtc.WebRTCSessionDescription.new(
            self.webrtc.WebRTCSDPType.OFFER, message), codecs

    def offer(self, request):
        offer = request.get("sdp") if isinstance(request, dict) else None
        session_id = str(request.get("session_id") or "") if isinstance(request, dict) else ""
        if not offer or len(offer) > 262144 or not session_id or len(session_id) > 80:
            raise ValueError("WebRTC offer 无效")
        with self.lock:
            if self.session:
                if time.monotonic() - self.session["touched"] < 30:
                    raise RuntimeError("远控仅控制视频已有连接")
                self._close_locked(self.session["id"])
            for name, condition in self.frame_conditions.items():
                with condition:
                    self.frame_slots[name] = None
                    self.slot_overwrites[name] = 0
            self._load()
            remote, codecs = self._parse_offer(offer)
            now = time.monotonic()
            self.session = {"id": session_id, "touched": now, "created_at": now,
                            "pipeline": self.gst.Pipeline.new("dm-webrtc-preview"),
                            "sources": {}, "encoders": {}, "scalers": {},
                            "rtp_pads": {}, "pc": None, "probes": [],
                            "frame_counts": {}, "rtp_counts": {}, "last_stats_log": 0,
                            "publish_rate": {}, "encode_rate": {},
                            # Start at the minimum bitrate; the browser raises quality after stability checks.
                            "quality": "control_min",
                            "allow_hot_resize": False,
                            # 协商期间置位，阻止 monitor 按"从未连上"回收会话。
                            "negotiating": True,
                            "sinks": []}
        # 关键：等待编码器就绪与 SDP 应答都不能持有 self.lock。
        # _push_frame() 需要同一把锁把首帧推进 appsrc，若在持锁状态下等待，
        # 首帧永远送不进来，等待必然超时。
        try:
            self._build_pipeline(codecs)
            self._await_encoders()
            result = self._answer(remote)
            with self.lock:
                if self.session is not None:
                    self.session["negotiating"] = False
            return result
        except Exception:
            self.close(session_id)
            raise

    def _build_pipeline(self, codecs):
        gst = self.gst
        with self.lock:
            state = self.session
            if not state or state.get("closing"):
                raise RuntimeError("WebRTC 会话已被关闭")
            pipeline = state["pipeline"]
        pc = gst.ElementFactory.make("webrtcbin", "peer")
        if pc is None:
            raise RuntimeError("无法创建 webrtcbin")
        pc.set_property("bundle-policy", self.webrtc.WebRTCBundlePolicy.MAX_BUNDLE)
        pipeline.add(pc)
        state["pc"] = pc
        # codecs 按浏览器 SDP 的 m-line 顺序；track 名必须与同一 index 绑定，
        # 否则浏览器 CAM_KEYS 顺序变化时 payload 会错配到别的相机。
        for index, name in enumerate(("wrist", "global", "third")):
            payload, profile = codecs[index]
            reader = self.readers.get(name)
            if reader is None or not reader._meta()["present"]:
                raise RuntimeError("相机尚未就绪：%s" % name)
            if profile not in ("baseline", "constrained-baseline"):
                raise RuntimeError("相机 %s 不支持的 H.264 profile：%s" % (name, profile))
            with reader.lock:
                width, height = reader.width, reader.height
                pixel = str(reader.pixel_format).lower()
                fps = max(1, min(int(reader.source_fps or 30), 60))
            fmt = "RGB" if "rgb8" in pixel else "BGR"
            if "yuyv" in pixel:
                fmt = "YUY2"
            src = gst.ElementFactory.make("appsrc", "src-" + name)
            if src is None:
                raise RuntimeError("无法创建视频帧队列：%s" % name)
            src.set_property("is-live", True)
            src.set_property("format", gst.Format.TIME)
            src.set_property("block", False)
            src.set_property("do-timestamp", False)
            src.set_property("max-buffers", 1)
            # appsrc 的 max-bytes 默认 200000，单帧 BGR 1280x720 就有 2.7MB，
            # 会被判定超限而停止接收数据；显式设为 0 表示不限。
            if src.find_property("max-bytes"):
                src.set_property("max-bytes", 0)
            if src.find_property("max-time"):
                src.set_property("max-time", 0)
            if src.find_property("leaky-type"):
                src.set_property("leaky-type", 2)
            caps = gst.Caps.from_string(
                "video/x-raw,format=%s,width=%d,height=%d,framerate=%d/1" %
                (fmt, width, height, fps))
            src.set_property("caps", caps)
            # 同一 fps 同时用于 caps 与 buffer duration，避免上游重协商。
            state.setdefault("source_fps", {})[name] = fps
            pipeline.add(src)
            chain = [src]
            descriptions = [("queue", "max-size-buffers=1 leaky=downstream")]
            if fmt != "YUY2":
                descriptions.append(("videoconvert", ""))
            descriptions.append(("nvvidconv", ""))
            for kind, props in descriptions:
                element = gst.ElementFactory.make(kind, "%s-%s" % (kind, name))
                if element is None:
                    raise RuntimeError("缺少 GStreamer 插件：%s" % kind)
                if props and kind == "queue":
                    element.set_property("max-size-buffers", 1)
                    element.set_property("leaky", 2)
                pipeline.add(element)
                chain.append(element)
            capsfilter = gst.ElementFactory.make("capsfilter", "nv12-" + name)
            initial_bitrate, initial_scale = QUALITY_PROFILES["control_min"]
            output_width, output_height = quality_dimensions(width, height, initial_scale)
            capsfilter.set_property("caps", gst.Caps.from_string(
                "video/x-raw(memory:NVMM),format=NV12,width=%d,height=%d" %
                (output_width, output_height)))
            encoder = gst.ElementFactory.make("nvv4l2h264enc", "h264-" + name)
            # L4T nvv4l2h264enc 的 profile 枚举与 v4l2 规范不同：
            # 0=Baseline，1 非法，2=Main，4=High。浏览器只接受 Baseline，
            # 固定用 0 避免写入非法值导致编码器 configure 失败。
            encoder.set_property("profile", 0)
            encoder.set_property("bitrate", initial_bitrate)
            encoder.set_property("control-rate", 1)
            encoder.set_property("iframeinterval", 30)
            if encoder.find_property("idrinterval"):
                encoder.set_property("idrinterval", 30)
            encoder.set_property("insert-sps-pps", True)
            for prop, value in (("num-B-Frames", 0), ("maxperf-enable", True),
                                ("low-latency", True)):
                if encoder.find_property(prop):
                    encoder.set_property(prop, value)
            parser = gst.ElementFactory.make("h264parse", "parse-" + name)
            parser.set_property("config-interval", -1)
            h264_caps = gst.ElementFactory.make("capsfilter", "annex-b-" + name)
            h264_caps.set_property("caps", gst.Caps.from_string(
                "video/x-h264,stream-format=byte-stream,alignment=au"))
            pay = gst.ElementFactory.make("rtph264pay", "pay-" + name)
            pay.set_property("pt", payload)
            pay.set_property("config-interval", 1)
            # MTU 1200 与 WireGuard 的 1380 匹配，避免 UDP 分片丢包
            if pay.find_property("mtu"):
                pay.set_property("mtu", 1200)
            if pay.find_property("aggregate-mode"):
                pay.set_property("aggregate-mode", 1)
            rtp_caps = gst.ElementFactory.make("capsfilter", "rtp-" + name)
            rtp_caps.set_property("caps", gst.Caps.from_string(
                "application/x-rtp,media=video,encoding-name=H264,payload=%d,"
                "clock-rate=90000" % payload))
            tail = [capsfilter, encoder, parser, h264_caps, pay, rtp_caps]
            for element in tail:
                if element is None:
                    raise RuntimeError("当前 Jetson 缺少 H.264 编码组件")
                pipeline.add(element)
                chain.append(element)
            for left, right in zip(chain, chain[1:]):
                if not left.link(right):
                    raise RuntimeError("无法连接 %s 视频编码管线" % name)
            # Drop complete raw frames only; one encoded frame can span several RTP packets.
            src_pad = rtp_caps.get_static_pad("src")
            state["rtp_counts"][name] = 0

            def count_rtp(_pad, _info, _state=state, _name=name):
                _state["rtp_counts"][_name] = _state["rtp_counts"].get(_name, 0) + 1
                return gst.PadProbeReturn.OK

            rtp_probe = src_pad.add_probe(gst.PadProbeType.BUFFER, count_rtp)
            state["probes"].append((src_pad, rtp_probe))
            encoded_pad = parser.get_static_pad("src")

            def count_encoded(_pad, _info, _state=state, _name=name):
                self._stage_frame_rate(_state["encode_rate"], _name, time.monotonic())
                return gst.PadProbeReturn.OK

            encode_probe = encoded_pad.add_probe(gst.PadProbeType.BUFFER, count_encoded)
            state["probes"].append((encoded_pad, encode_probe))
            sink = pc.request_pad_simple("sink_%u")
            if sink is None or src_pad.link(sink) != gst.PadLinkReturn.OK:
                raise RuntimeError("无法添加 %s 视频轨道" % name)
            transceiver = sink.get_property("transceiver")
            transceiver.set_property("direction", self.webrtc.WebRTCRTPTransceiverDirection.SENDONLY)
            # 开启 NACK 让浏览器能请求重传，降低丢包对画面的影响
            if transceiver.find_property("do-nack"):
                transceiver.set_property("do-nack", True)
            state["sinks"].append(sink)
            state["sources"][name] = (src, fmt, width, height)
            state["frame_counts"][name] = 0
            state["encoders"][name] = encoder
            state["scalers"][name] = (capsfilter, (width, height))
            state["rtp_pads"][name] = src_pad
        pipeline.set_state(gst.State.PLAYING)
        # set_state 是非阻塞的；必须确认真的进入 PLAYING，否则此时喂帧会被
        # 当成 FLUSHING 丢光，rtp_pads 永远拿不到 caps。
        pipeline_reached_playing(gst, pipeline, PIPELINE_PLAYING_TIMEOUT_NS)
        state["playing_at"] = time.monotonic()

    def _await_encoders(self):
        """等待三路编码器产出第一个 RTP 包。

        首帧必须由相机读线程经 push() 送入 appsrc，而 DirectCameraReader
        等待首帧的上限是 DIRECT_FRAME_TIMEOUT_MS=5s，因此这里的等待窗口
        必须大于该值，否则冷启动必然超时。相机掉线时立即失败，不做无意义
        的满额等待。

        本方法不能长时间持有 self.lock：_push_frame() 需要同一把锁推帧，
        持锁等待会让首帧永远送不进来。
        """
        deadline = time.monotonic() + ENCODER_READY_TIMEOUT
        while True:
            with self.lock:
                state = self.session
                if not state or state.get("closing"):
                    raise RuntimeError("WebRTC 会话已被关闭")
                pads = dict(state["rtp_pads"])
                counts = dict(state.get("frame_counts", {}))
            pending = [name for name, pad in pads.items()
                       if pad.get_current_caps() is None]
            if not pending:
                return
            offline = [name for name in pending
                       if not self.readers[name]._meta().get("present")]
            if offline:
                raise RuntimeError("相机尚未出帧：%s（已推帧 %s）" %
                                   (", ".join(offline), counts))
            if time.monotonic() >= deadline:
                present = {name: bool(self.readers[name]._meta().get("present"))
                           for name in pads}
                raise TimeoutError(
                    "H.264 编码未就绪：%s（已推帧 %s，相机在线 %s）" %
                    (", ".join(pending), counts, present))
            time.sleep(0.02)

    def _answer(self, offer):
        gst = self.gst
        with self.lock:
            state = self.session
            if not state or state.get("closing"):
                raise RuntimeError("WebRTC 会话已被关闭")
            pc = state["pc"]
        # 三个 promise 共用一份预算，避免逐个 5s 累加超出总预算。
        promise_deadline = time.monotonic() + PROMISE_TIMEOUT_NS / 1000000000.0

        def remaining():
            left = promise_deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("WebRTC SDP 应答超时（%dms）" %
                                   (PROMISE_TIMEOUT_NS // 1000000))
            return int(left * 1000000000)

        self._promise_value(gst, pc, "set-remote-description", offer,
                            timeout_ns=remaining())
        reply = self._promise_value(gst, pc, "create-answer", None,
                                     timeout_ns=remaining())
        answer = reply.get_value("answer") if reply is not None else None
        if answer is None:
            raise RuntimeError("webrtcbin 无法创建 SDP answer")
        self._promise_value(gst, pc, "set-local-description", answer,
                            timeout_ns=remaining())
        deadline = time.monotonic() + ICE_GATHERING_TIMEOUT
        while pc.get_property("ice-gathering-state") != self.webrtc.WebRTCICEGatheringState.COMPLETE:
            if time.monotonic() >= deadline:
                raise TimeoutError("WebRTC ICE 候选收集超时")
            time.sleep(0.05)
        local = pc.get_property("local-description")
        candidates = [line.strip() for line in local.sdp.as_text().splitlines()
                      if line.startswith("a=candidate:")]
        print("[webrtc] gathered %d ICE candidates: %s" %
              (len(candidates), ", ".join(candidates[:6])), flush=True)
        with self.lock:
            if self.session is state:
                state["touched"] = time.monotonic()
                session_id = state["id"]
            else:
                raise RuntimeError("WebRTC 会话已被关闭")
        return {"session_id": session_id, "sdp": local.sdp.as_text(),
                "type": "answer"}

    def push(self, name, data, width, height, pixel_format, captured_at):
        if name not in self.frame_conditions:
            return
        condition = self.frame_conditions[name]
        with condition:
            if self.frame_slots[name] is not None:
                self.slot_overwrites[name] += 1
            self.frame_slots[name] = (data, width, height, pixel_format, captured_at)
            condition.notify()

    def _push_frame(self, name, data, width, height, pixel_format, captured_at):
        state = self.session
        if not state or state.get("closing") or name not in state["sources"]:
            return
        src, fmt, expected_width, expected_height = state["sources"][name]
        if (width, height) != (expected_width, expected_height):
            return
        if "mjpeg" in str(pixel_format).lower():
            try:
                import cv2
                import numpy as np
                frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    return
                data = frame.tobytes()
            except Exception:
                return
        with self.lock:
            if self.session is not state or state.get("closing"):
                return
            buffer = self.gst.Buffer.new_allocate(None, len(data), None)
            buffer.fill(0, data)
            base_time = state["pipeline"].get_base_time()
            if base_time == self.gst.CLOCK_TIME_NONE:
                # 管线正在拆除时 base_time 无效，此刻推帧没有意义
                return
            pts = int(captured_at * self.gst.SECOND) - int(base_time)
            buffer.pts = max(0, pts)
            buffer.dts = self.gst.CLOCK_TIME_NONE
            fps = state.get("source_fps", {}).get(
                name) or max(1, min(int(self.readers[name].source_fps or 30), 60))
            buffer.duration = self.gst.SECOND // fps
            try:
                result = src.emit("push-buffer", buffer)
                if result == self.gst.FlowReturn.OK:
                    state["frame_counts"][name] = state["frame_counts"].get(name, 0) + 1
                    self._stage_frame_rate(state["publish_rate"], name, time.monotonic())
                elif result not in (None, self.gst.FlowReturn.FLUSHING):
                    print("[webrtc] %s appsrc=%s" % (name, result), flush=True)
            except Exception:
                pass

    def close(self, session_id):
        if not session_id:
            return {"closed": False}
        with self.lock:
            state = self._detach_locked(session_id)
        if not state:
            return {"closed": False}
        # teardown 会阻塞在 pipeline.set_state(NULL) + get_state(2s) 上，
        # 必须在锁外执行，否则这 2 秒内 _push_frame/_await_encoders/stats 全部冻结。
        self._teardown_state(state)
        return {"closed": True}

    def _detach_locked(self, session_id):
        """把会话从 self.session 摘下（必须在持锁时调用）。"""
        state = self.session
        if not state or state["id"] != session_id:
            return None
        state["closing"] = True
        self.session = None
        return state

    def _teardown_state(self, state):
        """释放管线资源；不持有 self.lock。"""
        try:
            for pad, probe_id in state.get("probes", ()):
                try:
                    pad.remove_probe(probe_id)
                except Exception:
                    pass
            state["probes"] = []
            pc = state.get("pc")
            if pc is not None:
                for sink in state.get("sinks", []):
                    try:
                        pc.release_request_pad(sink)
                    except Exception:
                        pass
            state["pipeline"].set_state(self.gst.State.NULL)
            get_state = getattr(state["pipeline"], "get_state", None)
            if get_state is not None:
                get_state(2 * self.gst.SECOND)
        except Exception:
            pass
        # 清空引用，避免已拆除的会话继续持有编码器与 pad
        for key in ("sources", "encoders", "scalers", "rtp_pads"):
            if isinstance(state.get(key), dict):
                state[key].clear()
        state["pc"] = None
        for name, condition in self.frame_conditions.items():
            with condition:
                self.frame_slots[name] = None
                condition.notify_all()

    def _close_locked(self, session_id):
        """持锁版本的关闭：先摘下会话，teardown 放到锁外。"""
        state = self._detach_locked(session_id)
        if not state:
            return {"closed": False}
        self._teardown_state(state)
        return {"closed": True}


def session_manager(readers):
    return WebRTCPreview(readers)

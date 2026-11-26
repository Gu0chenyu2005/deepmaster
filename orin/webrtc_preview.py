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


def quality_dimensions(width, height, scale):
    """Return even output dimensions while retaining the 640x360 floor."""
    width, height = max(2, int(width)), max(2, int(height))
    floor = min(1.0, max(640 / width, 360 / height))
    effective = max(float(scale), floor)
    return (max(2, int(width * effective) // 2 * 2),
            max(2, int(height * effective) // 2 * 2))


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
            for name, reader in self.readers.items():
                meta = reader._meta()
                cameras[name] = {
                    "source_fps": meta.get("source_fps", 0),
                    "capture_fps": meta.get("measured_fps", 0),
                    "capture_timeouts": meta.get("frame_timeouts", 0),
                    "capture_interval_ms": meta.get("capture_interval_ms"),
                    "queue_overwrites": self.slot_overwrites.get(name, 0),
                    "publish_fps": state.get("publish_rate", {}).get(name, {}).get("fps", 0),
                    "encode_fps": state.get("encode_rate", {}).get(name, {}).get("fps", 0),
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
                    message_types = self.gst.MessageType.ERROR | self.gst.MessageType.EOS
                    message = bus.pop_filtered(message_types) if bus is not None else None
                    failed = False
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
                        if (not state.get("ever_connected")
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
    def _promise_value(gst, element, signal, *args):
        promise = gst.Promise.new()
        element.emit(signal, *args, promise)
        if promise.wait() != gst.PromiseResult.REPLIED:
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
                            "sinks": []}
            try:
                self._build_pipeline(codecs)
                return self._answer(remote)
            except Exception:
                self._close_locked(session_id)
                raise

    def _build_pipeline(self, codecs):
        gst = self.gst
        state = self.session
        pipeline = state["pipeline"]
        pc = gst.ElementFactory.make("webrtcbin", "peer")
        if pc is None:
            raise RuntimeError("无法创建 webrtcbin")
        pc.set_property("bundle-policy", self.webrtc.WebRTCBundlePolicy.MAX_BUNDLE)
        pipeline.add(pc)
        state["pc"] = pc
        for index, name in enumerate(("wrist", "global", "third")):
            payload, profile = codecs[index]
            reader = self.readers.get(name)
            if reader is None or not reader._meta()["present"]:
                raise RuntimeError("相机尚未就绪：%s" % name)
            with reader.lock:
                width, height = reader.width, reader.height
                pixel = str(reader.pixel_format).lower()
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
            if src.find_property("leaky-type"):
                src.set_property("leaky-type", 2)
            caps = gst.Caps.from_string(
                "video/x-raw,format=%s,width=%d,height=%d,framerate=%d/1" %
                (fmt, width, height, max(1, min(int(reader.source_fps or 30), 60))))
            src.set_property("caps", caps)
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
            encoder.set_property("profile", int(profile == "constrained-baseline"))
            encoder.set_property("bitrate", initial_bitrate)
            encoder.set_property("control-rate", 1)
            encoder.set_property("iframeinterval", 30)
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
            state["sinks"].append(sink)
            state["sources"][name] = (src, fmt, width, height)
            state["frame_counts"][name] = 0
            state["encoders"][name] = encoder
            state["scalers"][name] = (capsfilter, (width, height))
            state["rtp_pads"][name] = src_pad
        pipeline.set_state(gst.State.PLAYING)

    def _answer(self, offer):
        gst, pc = self.gst, self.session["pc"]
        deadline = time.monotonic() + 4
        while True:
            pending = [name for name, pad in self.session["rtp_pads"].items()
                       if pad.get_current_caps() is None]
            if not pending:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("H.264 编码未就绪：%s" % ", ".join(pending))
            time.sleep(0.02)
        self._promise_value(gst, pc, "set-remote-description", offer)
        reply = self._promise_value(gst, pc, "create-answer", None)
        answer = reply.get_value("answer") if reply is not None else None
        if answer is None:
            raise RuntimeError("webrtcbin 无法创建 SDP answer")
        self._promise_value(gst, pc, "set-local-description", answer)
        deadline = time.monotonic() + 8
        while pc.get_property("ice-gathering-state") != self.webrtc.WebRTCICEGatheringState.COMPLETE:
            if time.monotonic() >= deadline:
                raise TimeoutError("WebRTC ICE 候选收集超时")
            time.sleep(0.05)
        self.session["touched"] = time.monotonic()
        local = pc.get_property("local-description")
        candidates = [line.strip() for line in local.sdp.as_text().splitlines()
                      if line.startswith("a=candidate:")]
        print("[webrtc] gathered %d ICE candidates: %s" %
              (len(candidates), ", ".join(candidates[:6])), flush=True)
        return {"session_id": self.session["id"], "sdp": local.sdp.as_text(),
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
            pts = int(captured_at * self.gst.SECOND) - int(base_time)
            buffer.pts = max(0, pts)
            buffer.dts = self.gst.CLOCK_TIME_NONE
            fps = max(1, min(int(self.readers[name].source_fps or 30), 60))
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
            return self._close_locked(session_id)

    def _close_locked(self, session_id):
        state = self.session
        if not state or state["id"] != session_id:
            return {"closed": False}
        state["closing"] = True
        self.session = None
        try:
            for pad, probe_id in state.get("probes", ()):
                try:
                    pad.remove_probe(probe_id)
                except Exception:
                    pass
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
        for name, condition in self.frame_conditions.items():
            with condition:
                self.frame_slots[name] = None
                condition.notify_all()
        return {"closed": True}


def session_manager(readers):
    return WebRTCPreview(readers)

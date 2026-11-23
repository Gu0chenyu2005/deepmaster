#!/usr/bin/env python3
# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""低延迟三路 H.264/WebRTC 相机发送端。"""

import threading
import time
import os


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
            with self.lock:
                state = self.session
                if not state or not state.get("pc"):
                    continue
                connection = state["pc"].get_property("connection-state")
                name = getattr(connection, "value_nick", str(connection)).lower()
                if name == "connected":
                    state["touched"] = time.monotonic()
                elif name in ("failed", "disconnected", "closed"):
                    state.setdefault("disconnected_at", time.monotonic())
                    if time.monotonic() - state["disconnected_at"] >= 6:
                        self._close_locked(state["id"])
                else:
                    state.pop("disconnected_at", None)

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
            self._load()
            remote, codecs = self._parse_offer(offer)
            self.session = {"id": session_id, "touched": time.monotonic(),
                            "pipeline": self.gst.Pipeline.new("dm-webrtc-preview"),
                            "sources": {}, "rtp_pads": {}, "pc": None}
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
            capsfilter.set_property("caps", gst.Caps.from_string(
                "video/x-raw(memory:NVMM),format=NV12"))
            encoder = gst.ElementFactory.make("nvv4l2h264enc", "h264-" + name)
            encoder.set_property("profile", int(profile == "constrained-baseline"))
            try:
                bitrate = int(os.environ.get("DM_WEBRTC_VIDEO_BITRATE", "2000000"))
            except ValueError:
                bitrate = 2000000
            encoder.set_property("bitrate", max(500000, min(bitrate, 6000000)))
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
            sink = pc.request_pad_simple("sink_%u")
            if sink is None or src_pad.link(sink) != gst.PadLinkReturn.OK:
                raise RuntimeError("无法添加 %s 视频轨道" % name)
            transceiver = sink.get_property("transceiver")
            transceiver.set_property("direction", self.webrtc.WebRTCRTPTransceiverDirection.SENDONLY)
            state["sources"][name] = (src, fmt, width, height)
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
        return {"session_id": self.session["id"], "sdp": local.sdp.as_text(),
                "type": "answer"}

    def push(self, name, data, width, height, pixel_format, captured_at):
        state = self.session
        if not state or name not in state["sources"]:
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
        buffer = self.gst.Buffer.new_allocate(None, len(data), None)
        buffer.fill(0, data)
        base_time = state["pipeline"].get_base_time()
        pts = int(captured_at * self.gst.SECOND) - int(base_time)
        buffer.pts = max(0, pts)
        buffer.dts = self.gst.CLOCK_TIME_NONE
        fps = max(1, min(int(self.readers[name].source_fps or 30), 60))
        buffer.duration = self.gst.SECOND // fps
        try:
            src.emit("push-buffer", buffer)
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
        self.session = None
        try:
            state["pipeline"].set_state(self.gst.State.NULL)
        except Exception:
            pass
        return {"closed": True}


def session_manager(readers):
    return WebRTCPreview(readers)

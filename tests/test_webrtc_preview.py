# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""WebRTC sender session lifecycle checks without cameras or a GPU."""
from pathlib import Path
import sys
import time
import unittest
import socket
import struct
import threading
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orin"))
import webrtc_preview
from webrtc_preview import (WebRTCPreview, LocalStunServer, prefer_numeric_candidates,
                            quality_dimensions, pipeline_reached_playing)


class LocalStunTests(unittest.TestCase):
    def test_numeric_candidate_replaces_mdns_only_in_its_own_media(self):
        mdns = "a=candidate:1 1 udp 2113937151 unknown.local 35507 typ host"
        numeric = "a=candidate:2 1 udp 1677729535 192.168.31.215 35507 typ srflx"
        sdp = "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 102\r\n" + mdns + "\r\n" + numeric
        sdp += "\r\nm=video 9 UDP/TLS/RTP/SAVPF 102\r\n" + mdns + "\r\n"
        result = prefer_numeric_candidates(sdp)
        self.assertEqual(result.count(mdns), 1)
        self.assertIn(numeric, result)
        self.assertIn(mdns, result.split("m=video")[-1])

    def test_binding_preserves_transaction_and_encodes_observed_address(self):
        transaction = b"0123456789ab"
        request = struct.pack("!HHI", 1, 0, LocalStunServer.COOKIE) + transaction
        response = LocalStunServer.binding_response(request, ("192.168.31.215", 35507))
        self.assertEqual(response[:20], struct.pack("!HHI", 0x101, 12,
                         LocalStunServer.COOKIE) + transaction)
        kind, length, reserved, family, port, ip = struct.unpack("!HHBBHI", response[20:])
        self.assertEqual((kind, length, reserved, family), (0x20, 8, 0, 1))
        self.assertEqual(port ^ 0x2112, 35507)
        self.assertEqual(socket.inet_ntoa(struct.pack("!I", ip ^ LocalStunServer.COOKIE)),
                         "192.168.31.215")

    def test_malformed_and_nonbinding_packets_are_ignored(self):
        for packet in (b"short", struct.pack("!HHI", 0x101, 0, LocalStunServer.COOKIE) + b"x" * 12,
                       struct.pack("!HHI", 1, 4, LocalStunServer.COOKIE) + b"x" * 12,
                       struct.pack("!HHI", 1, 0, 0) + b"x" * 12):
            self.assertIsNone(LocalStunServer.binding_response(packet, ("127.0.0.1", 1234)))

    def test_udp_endpoint_responds_and_releases_port(self):
        server = LocalStunServer("127.0.0.1", 0)
        port = server.port
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                client.bind(("127.0.0.1", 0))
                client.settimeout(2)
                request = struct.pack("!HHI", 1, 0, LocalStunServer.COOKIE) + b"0123456789ab"
                client.sendto(request, ("127.0.0.1", port))
                response, _ = client.recvfrom(2048)
                self.assertEqual(response, LocalStunServer.binding_response(
                    request, client.getsockname()))
        finally:
            server.close()
        self.assertFalse(server.thread.is_alive())
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as replacement:
            replacement.bind(("127.0.0.1", port))


class WebRTCPreviewTests(unittest.TestCase):
    def setUp(self):
        self.readers = [SimpleNamespace(name=name) for name in ("primary", "third", "d435i")]
        self.preview = WebRTCPreview(self.readers)

    def test_tracks_follow_ui_camera_order(self):
        self.assertEqual(list(self.preview.readers), ["wrist", "global", "third"])
        self.assertIs(self.preview.readers["wrist"], self.readers[0])
        self.assertIs(self.preview.readers["global"], self.readers[2])

    def test_minimum_quality_keeps_at_least_640_by_360_when_source_allows(self):
        self.assertEqual(quality_dimensions(1280, 720, 0.5), (640, 360))
        self.assertEqual(quality_dimensions(848, 480, 0.5), (640, 362))
        self.assertEqual(quality_dimensions(640, 480, 0.5), (640, 480))

    def test_offer_rejects_invalid_and_oversized_sessions(self):
        with self.assertRaises(ValueError):
            self.preview.offer({"session_id": "", "sdp": "offer"})
        with self.assertRaises(ValueError):
            self.preview.offer({"session_id": "s", "sdp": "x" * 262145})
        self.assertIsNone(self.preview.session)

    def test_offer_requires_three_video_mlines(self):
        class Media:
            def medias_len(self):
                return 2

            def get_media(self, _index):
                return SimpleNamespace(get_media=lambda: "video")

        self.preview.sdp = SimpleNamespace(
            SDPMessage=SimpleNamespace(new=lambda: (None, Media())),
            sdp_message_parse_buffer=lambda _data, _message: "OK",
            SDPResult=SimpleNamespace(OK="OK"),
        )
        with self.assertRaisesRegex(ValueError, "三路视频"):
            self.preview._parse_offer("valid")

    def test_active_session_is_exclusive(self):
        self.preview.session = {"id": "active", "touched": time.monotonic()}
        with self.assertRaisesRegex(RuntimeError, "已有连接"):
            self.preview.offer({"session_id": "second", "sdp": "offer"})
        self.assertEqual(self.preview.session["id"], "active")

    def test_close_releases_pipeline_only_for_matching_session(self):
        class Pipeline:
            def __init__(self):
                self.states = []

            def set_state(self, value):
                self.states.append(value)

        pipeline = Pipeline()
        self.preview.gst = SimpleNamespace(State=SimpleNamespace(NULL="NULL"))
        self.preview.session = {"id": "session", "pipeline": pipeline}
        self.assertEqual(self.preview.close("wrong"), {"closed": False})
        self.assertEqual(self.preview.close("session"), {"closed": True})
        self.assertEqual(pipeline.states, ["NULL"])
        self.assertIsNone(self.preview.session)

    def test_close_removes_probes_releases_request_pad_and_waits_for_null(self):
        calls = []

        class Pad:
            def remove_probe(self, probe_id):
                calls.append(("remove_probe", probe_id))

        class Peer:
            def release_request_pad(self, pad):
                calls.append(("release_pad", pad))

        class Pipeline:
            def set_state(self, value):
                calls.append(("set_state", value))

            def get_state(self, timeout):
                calls.append(("get_state", timeout))

        pad, sink, pipeline = Pad(), object(), Pipeline()
        self.preview.gst = SimpleNamespace(State=SimpleNamespace(NULL="NULL"), SECOND=1_000_000_000)
        self.preview.session = {"id": "session", "pipeline": pipeline,
                                "pc": Peer(), "sinks": [sink], "probes": [(pad, 7)]}
        self.assertEqual(self.preview.close("session"), {"closed": True})
        self.assertEqual(calls, [("remove_probe", 7), ("release_pad", sink),
                                 ("set_state", "NULL"), ("get_state", 2_000_000_000)])

    def test_missing_resize_permission_does_not_change_live_caps(self):
        class Encoder:
            def __init__(self):
                self.values = []

            def set_property(self, name, value):
                self.values.append((name, value))

        encoder, scaler = Encoder(), Encoder()
        self.preview.gst = SimpleNamespace(Caps=SimpleNamespace(from_string=lambda value: value))
        self.preview.session = {"id": "session", "encoders": {"wrist": encoder},
                                "scalers": {"wrist": (scaler, (1280, 720))},
                                "touched": 0}
        result = self.preview.set_quality("session", "control_min")
        self.assertTrue(result["ok"])
        self.assertEqual(encoder.values, [("bitrate", 800000)])
        self.assertEqual(scaler.values, [])

    def test_close_requires_a_nonempty_matching_session_id(self):
        self.preview.session = {"id": "active", "pipeline": object()}
        self.assertEqual(self.preview.close(""), {"closed": False})
        self.assertEqual(self.preview.close(None), {"closed": False})
        self.assertEqual(self.preview.session["id"], "active")

    def test_connected_session_requests_an_idr_from_each_encoder(self):
        calls = []

        class Encoder:
            def __init__(self, name):
                self.name = name

            def emit(self, signal):
                calls.append((self.name, signal))

        state = {"encoders": {name: Encoder(name) for name in self.preview.readers}}
        self.preview._request_keyframes(state)

        self.assertEqual(calls, [(name, "force-IDR") for name in self.preview.readers])

    def test_quality_changes_encoder_bitrate_without_replacing_session(self):
        class Encoder:
            def __init__(self):
                self.values = []

            def set_property(self, name, value):
                self.values.append((name, value))

        encoders = {name: Encoder() for name in self.preview.readers}
        scalers = {name: Encoder() for name in self.preview.readers}
        caps = SimpleNamespace(from_string=lambda value: value)
        self.preview.gst = SimpleNamespace(Caps=caps)
        self.preview.session = {"id": "session", "encoders": encoders,
                                "scalers": {name: (scalers[name], (640, 480))
                                            for name in self.preview.readers},
                                "touched": 0, "quality": "control",
                                "allow_hot_resize": True}
        result = self.preview.set_quality("session", "control_min")
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["bitrate"], 800000)
        self.assertEqual(result["scale"], 0.5)
        self.assertEqual(self.preview.session["quality"], "control_min")
        self.assertTrue(all(e.values == [("bitrate", 800000)] for e in encoders.values()))
        self.assertTrue(all(e.values == [(
            "caps", "video/x-raw(memory:NVMM),format=NV12,width=640,height=480")]
            for e in scalers.values()))
        self.assertEqual(self.preview.set_quality("other", "control"),
                         {"ok": False, "changed": False})

    def test_video_buffer_uses_camera_capture_time_on_pipeline_clock(self):
        class Buffer:
            def fill(self, offset, data):
                self.data = data

        class Pipeline:
            def get_base_time(self):
                return 2_000_000_000

        source = SimpleNamespace(buffers=[])
        source.emit = lambda name, buffer: source.buffers.append(buffer)
        buffer = Buffer()
        self.preview.gst = SimpleNamespace(
            Buffer=SimpleNamespace(new_allocate=lambda *_args: buffer),
            SECOND=1_000_000_000, CLOCK_TIME_NONE=2**64 - 1)
        self.preview.session = {
            "pipeline": Pipeline(),
            "sources": {"wrist": (source, "BGR", 640, 480)},
        }
        self.preview.readers["wrist"].source_fps = 30

        self.preview._push_frame("wrist", b"frame", 640, 480, "bgr8", 10.25)

        self.assertEqual(source.buffers, [buffer])
        self.assertEqual(buffer.pts, 8_250_000_000)
        self.assertEqual(buffer.dts, 2**64 - 1)
        self.assertEqual(buffer.duration, 1_000_000_000 // 30)

    def test_push_uses_single_latest_frame_slot(self):
        from webrtc_preview import WebRTCPreview
        preview = WebRTCPreview.__new__(WebRTCPreview)
        preview.frame_conditions = {"wrist": threading.Condition()}
        preview.frame_slots = {"wrist": (b"old", 1, 1, "bgr8", 1.0)}
        preview.slot_overwrites = {"wrist": 0}
        preview.push("wrist", b"new", 1, 1, "bgr8", 2.0)
        self.assertEqual(preview.frame_slots["wrist"][0], b"new")
        self.assertEqual(preview.slot_overwrites["wrist"], 1)

    def test_stats_reports_per_camera_pipeline_counters(self):
        reader = self.preview.readers["wrist"]
        reader._meta = lambda: {"source_fps": 30, "measured_fps": 27.5,
                                "frame_timeouts": 2, "capture_interval_ms": 37,
                                "present": True, "exposure": {}, "usb": {}}
        self.preview.readers["global"]._meta = lambda: {"source_fps": 0}
        self.preview.readers["third"]._meta = lambda: {"source_fps": 0}
        self.preview.session = {"id": "session", "quality": "control_low",
                                "publish_rate": {"wrist": {"fps": 26}},
                                "encode_rate": {"wrist": {"fps": 25}},
                                "rtp_counts": {"wrist": 120}}
        result = self.preview.stats("session")
        self.assertTrue(result["ok"])
        self.assertEqual(result["cameras"]["wrist"]["capture_fps"], 27.5)
        self.assertEqual(result["cameras"]["wrist"]["capture_timeouts"], 2)
        self.assertEqual(result["cameras"]["wrist"]["publish_fps"], 26)
        self.assertEqual(result["cameras"]["wrist"]["encode_fps"], 25)
        self.assertEqual(result["cameras"]["wrist"]["rtp_packets"], 120)


ROOT = Path(__file__).resolve().parents[1]


def _read_constant(relative_path, name):
    """从源码里读出一个数字常量，用于校验跨文件预算链。"""
    import re
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    match = re.search(r"^%s\s*=\s*([0-9.]+)" % re.escape(name), text, re.MULTILINE)
    if not match:
        raise AssertionError("%s 中找不到常量 %s" % (relative_path, name))
    return float(match.group(1))


def _read_js_timeout(relative_path, method, nth=0):
    """从 bridge.js 读出某个方法传入的超时毫秒数。"""
    import re
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    starts = [m for m in re.finditer(r"%s\s*:\s*function" % re.escape(method), text)]
    if not starts:
        raise AssertionError("%s 中找不到方法 %s" % (relative_path, method))
    tail = text[starts[nth].end():]
    match = re.search(r"post\(\s*\"[^\"]+\"\s*,[^)]*?,\s*([0-9]+)\s*\)", tail, re.DOTALL)
    if not match:
        raise AssertionError("%s 的 %s 没有显式超时" % (relative_path, method))
    return float(match.group(1)) / 1000.0


class EncoderReadyGateTests(unittest.TestCase):
    """_await_encoders 必须等首帧 RTP，且窗口要覆盖相机首帧的 5s 超时。"""

    def setUp(self):
        self.readers = [SimpleNamespace(name=name) for name in ("primary", "third", "d435i")]
        for reader in self.readers:
            reader._meta = lambda: {"present": True}
        self.preview = WebRTCPreview(self.readers)

    @staticmethod
    def _pad(has_caps):
        return SimpleNamespace(get_current_caps=lambda: "caps" if has_caps else None)

    def test_ready_gate_returns_once_all_pads_have_caps(self):
        self.preview.session = {
            "rtp_pads": {"wrist": self._pad(True), "global": self._pad(True),
                         "third": self._pad(True)},
            "frame_counts": {"wrist": 3, "global": 3, "third": 3},
        }
        started = time.monotonic()
        self.preview._await_encoders()
        self.assertLess(time.monotonic() - started, 1.0)

    def test_ready_gate_waits_for_a_late_first_frame(self):
        late_pad = self._pad(False)
        armed = {"done": False}

        def flip():
            time.sleep(0.15)
            armed["done"] = True
            late_pad.get_current_caps = lambda: "caps" if armed["done"] else None

        threading.Thread(target=flip, daemon=True).start()
        self.preview.session = {"rtp_pads": {"wrist": late_pad,
                                             "global": self._pad(True),
                                             "third": self._pad(True)},
                                "frame_counts": {"wrist": 0, "global": 1, "third": 1}}
        self.preview._await_encoders()
        self.assertTrue(armed["done"])

    def test_timeout_window_exceeds_camera_first_frame_timeout(self):
        from webrtc_preview import DIRECT_FRAME_TIMEOUT_MS, ENCODER_READY_TIMEOUT
        self.assertGreater(ENCODER_READY_TIMEOUT, DIRECT_FRAME_TIMEOUT_MS / 1000.0)

    def test_offline_camera_fails_fast_instead_of_waiting_out_the_window(self):
        """相机已掉线时不该满额等 8 秒，报错要指向相机而不是编码器。"""
        self.preview.readers["wrist"]._meta = lambda: {"present": False}
        self.preview.session = {"rtp_pads": {name: self._pad(False)
                                             for name in self.preview.readers},
                                "frame_counts": {name: 0 for name in self.preview.readers}}
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "相机尚未出帧：wrist"):
            self.preview._await_encoders()
        self.assertLess(time.monotonic() - started, 1.0)

    def test_signal_budget_chain_stays_ordered(self):
        """采集端预算 < agent 代理超时 < 浏览器信令超时，否则上游先截断诊断。"""
        from webrtc_preview import SIGNAL_BUDGET_SECONDS
        agent_timeout = _read_constant("server/agent.py", "WEBRTC_SIGNAL_TIMEOUT")
        browser_timeout = _read_js_timeout("assets/js/bridge.js", "webrtcOffer")
        self.assertLess(SIGNAL_BUDGET_SECONDS, agent_timeout)
        self.assertLess(agent_timeout, browser_timeout)

    def test_timeout_reports_pending_cameras_and_push_counts(self):
        self.preview.session = {"rtp_pads": {name: self._pad(False)
                                             for name in self.preview.readers},
                                "frame_counts": {name: 0 for name in self.preview.readers}}
        with patch.object(webrtc_preview, "ENCODER_READY_TIMEOUT", 0.05):
            with self.assertRaisesRegex(TimeoutError, "H.264 编码未就绪：wrist, global, third"):
                self.preview._await_encoders()


class PushLockContentionTests(unittest.TestCase):
    """等待编码器期间，_push_frame 必须能拿到锁把首帧推进 appsrc。

    旧实现里 offer() 持锁调用 _await_encoders()，而 _push_frame() 需要
    同一把 RLock，等待期间首帧永远送不进来，必然超时。
    """

    def setUp(self):
        self.readers = [SimpleNamespace(name=name) for name in ("primary", "third", "d435i")]
        for reader in self.readers:
            reader._meta = lambda: {"present": True}
            reader.source_fps = 30
        self.preview = WebRTCPreview(self.readers)

    def test_push_frame_is_not_blocked_while_awaiting_encoders(self):
        """在 _await_encoders 阻塞期间推帧必须能立刻完成。"""
        pushed = threading.Event()
        pad = SimpleNamespace(get_current_caps=lambda: None)
        self.preview.session = {
            "rtp_pads": {"wrist": pad},
            "frame_counts": {},
            "sources": {"wrist": (SimpleNamespace(emit=lambda _n, _b: None),
                                  "BGR", 640, 480)},
            "pipeline": SimpleNamespace(get_base_time=lambda: 0),
        }
        self.preview.gst = SimpleNamespace(
            Buffer=SimpleNamespace(new_allocate=lambda *_a: _FakeBuffer()),
            SECOND=1_000_000_000, CLOCK_TIME_NONE=2**64 - 1,
            FlowReturn=SimpleNamespace(OK="OK", FLUSHING="FLUSHING"))

        def waiter():
            try:
                self.preview._await_encoders()
            except Exception:
                pass

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        time.sleep(0.05)

        def push():
            self.preview._push_frame("wrist", b"frame", 640, 480, "bgr8", 1.0)
            pushed.set()

        pusher = threading.Thread(target=push, daemon=True)
        pusher.start()
        pusher.join(timeout=1.0)
        self.assertTrue(pushed.is_set(),
                        "推帧被等待编码器的锁阻塞，首帧永远送不进 appsrc")

    def test_offer_does_not_hold_the_lock_while_awaiting_encoders(self):
        """offer() 必须在等待编码器时释放 self.lock。

        行为验证：替换 _await_encoders，在其中让另一个线程尝试拿锁。
        能立刻拿到说明锁没被等待过程占住。
        """
        observed = {}
        original_offer = self.preview.offer

        def probe_await():
            def try_lock():
                acquired = self.preview.lock.acquire(timeout=0.5)
                observed["free"] = acquired
                if acquired:
                    self.preview.lock.release()
            thread = threading.Thread(target=try_lock, daemon=True)
            thread.start()
            thread.join(timeout=1.0)
            observed["probe_done"] = True
            return {}

        self.preview._await_encoders = probe_await
        self.preview._build_pipeline = lambda _codecs: None
        self.preview._answer = lambda _remote: {"ok": True}
        self.preview._load = lambda: None
        self.preview._parse_offer = lambda _text: ("remote", [(96, "baseline")] * 3)
        self.preview.gst = SimpleNamespace(
            Pipeline=SimpleNamespace(new=lambda _n: SimpleNamespace(
                set_state=lambda _s: None,
                get_state=lambda _t: ("SUCCESS", "PLAYING", "NO_PREROLL"))),
            State=SimpleNamespace(PLAYING="PLAYING", NULL="NULL"))

        try:
            original_offer({"session_id": "s", "sdp": "offer"})
        except Exception:
            pass

        self.assertTrue(observed.get("probe_done"), "offer 未走到 _await_encoders")
        self.assertTrue(observed.get("free"),
                        "等待编码器期间仍持有 self.lock，首帧推不进来")


class _FakeBuffer:
    def fill(self, _offset, _data):
        return None

    pts = 0
    dts = 0
    duration = 0


class SessionTeardownTests(unittest.TestCase):
    """会话拆除必须在锁外执行，否则 teardown 期间整个服务被冻结。"""

    def setUp(self):
        self.readers = [SimpleNamespace(name=name) for name in ("primary", "third", "d435i")]
        for reader in self.readers:
            reader._meta = lambda: {"present": True}
        self.preview = WebRTCPreview(self.readers)

    def test_close_does_not_hold_the_lock_during_teardown(self):
        """teardown 会阻塞在 get_state(2s) 上，期间其他线程必须能拿锁。"""
        class SlowPipeline:
            def set_state(self, _state):
                pass

            def get_state(self, _timeout):
                time.sleep(1.5)
                return ("SUCCESS", "NULL", "VOID_PENDING")

        self.preview.gst = SimpleNamespace(State=SimpleNamespace(NULL="NULL"),
                                          SECOND=1000000000)
        self.preview.session = {"id": "s", "pipeline": SlowPipeline(), "probes": [],
                                 "sinks": [], "sources": {}, "encoders": {},
                                 "scalers": {}, "rtp_pads": {}}

        acquired = {"ok": None, "waited": None}

        def probe():
            # 等 close() 进入 teardown 再尝试拿锁
            time.sleep(0.3)
            started = time.monotonic()
            got = self.preview.lock.acquire(timeout=3)
            acquired["waited"] = time.monotonic() - started
            acquired["ok"] = got
            if got:
                self.preview.lock.release()

        thread = threading.Thread(target=probe, daemon=True)
        thread.start()
        self.preview.close("s")
        thread.join(timeout=4)

        self.assertIsNotNone(acquired["ok"], "探测线程未完成")
        self.assertTrue(acquired["ok"],
                        "teardown 期间锁被占住，其他操作全部冻结")
        self.assertLess(acquired["waited"], 1.0,
                        "拿锁等待过久，说明 teardown 仍在锁内执行")

    def test_teardown_clears_pipeline_references(self):
        """拆除后必须清掉 sources/encoders/scalers/rtp_pads 引用。"""
        class FastPipeline:
            def set_state(self, _state):
                pass

            def get_state(self, _timeout):
                return ("SUCCESS", "NULL", "VOID_PENDING")

        self.preview.gst = SimpleNamespace(State=SimpleNamespace(NULL="NULL"),
                                          SECOND=1000000000)
        self.preview.session = {"id": "s", "pipeline": FastPipeline(), "probes": [],
                                 "sinks": [], "pc": object(),
                                 "sources": {"wrist": 1}, "encoders": {"wrist": 2},
                                 "scalers": {"wrist": 3}, "rtp_pads": {"wrist": 4}}
        state = self.preview.session
        self.preview.close("s")
        self.assertEqual(state["sources"], {})
        self.assertEqual(state["encoders"], {})
        self.assertEqual(state["scalers"], {})
        self.assertEqual(state["rtp_pads"], {})
        self.assertIsNone(state["pc"])

    def test_detach_marks_the_session_closing(self):
        state = {"id": "s"}
        self.preview.session = state
        detached = self.preview._detach_locked("s")
        self.assertIs(detached, state)
        self.assertTrue(state["closing"])
        self.assertIsNone(self.preview.session)

    def test_detach_ignores_a_mismatched_session(self):
        self.preview.session = {"id": "active"}
        self.assertIsNone(self.preview._detach_locked("other"))
        self.assertEqual(self.preview.session["id"], "active")


class StatsFreshnessTests(unittest.TestCase):
    """帧率统计过期后必须报 0，否则前端不会降档。"""

    def setUp(self):
        self.readers = [SimpleNamespace(name=name) for name in ("primary", "third", "d435i")]
        for reader in self.readers:
            reader._meta = lambda: {"present": True, "source_fps": 30,
                                    "measured_fps": 30, "frame_timeouts": 0,
                                    "capture_interval_ms": 33, "exposure": {},
                                    "usb": {}}
        self.preview = WebRTCPreview(self.readers)

    def test_stale_frame_rates_are_reported_as_zero(self):
        stale = time.monotonic() - 10
        self.preview.session = {
            "id": "s", "quality": "control",
            "publish_rate": {"wrist": {"count": 0, "at": stale, "fps": 30.0}},
            "encode_rate": {"wrist": {"count": 0, "at": stale, "fps": 30.0}},
            "rtp_counts": {"wrist": 120},
        }
        result = self.preview.stats("s")
        self.assertEqual(result["cameras"]["wrist"]["publish_fps"], 0.0,
                         "视频流已停却仍报告 30fps，前端不会触发降档")
        self.assertEqual(result["cameras"]["wrist"]["encode_fps"], 0.0)

    def test_live_frame_rates_are_reported_unchanged(self):
        now = time.monotonic()
        self.preview.session = {
            "id": "s", "quality": "control",
            "publish_rate": {"wrist": {"count": 5, "at": now - 0.4, "fps": 29.5}},
            "encode_rate": {"wrist": {"count": 5, "at": now - 0.4, "fps": 28.0}},
            "rtp_counts": {"wrist": 120},
        }
        result = self.preview.stats("s")
        self.assertEqual(result["cameras"]["wrist"]["publish_fps"], 29.5)
        self.assertEqual(result["cameras"]["wrist"]["encode_fps"], 28.0)


class EncoderProfileTests(unittest.TestCase):
    """nvv4l2h264enc 的 profile 必须是 L4T 的 0(Baseline)，不是 v4l2 的 1。"""

    def test_source_uses_baseline_profile_zero(self):
        source = (ROOT / "orin/webrtc_preview.py").read_text(encoding="utf-8")
        self.assertIn('encoder.set_property("profile", 0)', source)
        self.assertNotIn('encoder.set_property("profile", int(', source)

    def test_profile_zero_is_baseline_in_v4l2(self):
        """v4l2 规范：BASELINE=0，CONSTRAINED_BASELINE=1，L4T 的 nvv4l2 不接受 1。"""
        header = Path("/usr/include/linux/v4l2-controls.h")
        if not header.exists():
            self.skipTest("本机无 v4l2 头文件")
        text = header.read_text(encoding="utf-8", errors="ignore")
        self.assertIn("V4L2_MPEG_VIDEO_H264_PROFILE_BASELINE\t\t\t= 0", text)
        self.assertIn("V4L2_MPEG_VIDEO_H264_PROFILE_CONSTRAINED_BASELINE\t= 1", text)


class PipelineStateTests(unittest.TestCase):
    """get_state() 返回 (StateChangeReturn, current, pending)，不能解包错。

    旧实现把第一个元素（返回值）当成状态比较，正常 PLAYING 也会误报失败。
    另外 is-live 的 appsrc 在收到首帧前停在 PAUSED，等待首帧阶段必须接受它。
    """

    @staticmethod
    def _gst(current_nick, result_nick):
        class Value:
            def __init__(self, nick):
                self.value_nick = nick

        return SimpleNamespace(
            State=SimpleNamespace(PLAYING=Value("PLAYING"), NULL=Value("NULL"),
                                  READY=Value("READY"), PAUSED=Value("PAUSED")),
            StateChangeReturn=SimpleNamespace(SUCCESS=Value(result_nick)))

    def test_current_playing_is_accepted(self):
        gst = self._gst("PLAYING", "SUCCESS")
        pipeline = SimpleNamespace(get_state=lambda _t: ("SUCCESS", gst.State.PLAYING, "NO_PREROLL"))
        self.assertTrue(pipeline_reached_playing(gst, pipeline, 1000))

    def test_current_null_is_rejected(self):
        gst = self._gst("NULL", "FAILURE")
        pipeline = SimpleNamespace(get_state=lambda _t: ("FAILURE", gst.State.NULL, "VOID_PENDING"))
        with self.assertRaisesRegex(RuntimeError, "管线未启动"):
            pipeline_reached_playing(gst, pipeline, 1000)

    def test_current_ready_is_rejected(self):
        gst = self._gst("READY", "ASYNC")
        pipeline = SimpleNamespace(get_state=lambda _t: ("ASYNC", gst.State.READY, "PLAYING"))
        with self.assertRaisesRegex(RuntimeError, "管线未启动"):
            pipeline_reached_playing(gst, pipeline, 1000)

    def test_paused_is_accepted_while_waiting_for_the_first_frame(self):
        """is-live 的 appsrc 在首帧到达前处于 PAUSED，此时不能判失败。"""
        gst = self._gst("PAUSED", "ASYNC")
        pipeline = SimpleNamespace(
            get_state=lambda _t: ("ASYNC", gst.State.PAUSED, gst.State.PLAYING))
        self.assertTrue(pipeline_reached_playing(gst, pipeline, 1000))

    def test_return_value_is_not_mistaken_for_state(self):
        """返回值为 SUCCESS、状态为 PLAYING 时必须通过。

        若把返回值当状态比较，SUCCESS != PLAYING 会误判为失败。
        """
        gst = self._gst("PLAYING", "SUCCESS")
        result_value = "SUCCESS"
        pipeline = SimpleNamespace(
            get_state=lambda _t: (result_value, gst.State.PLAYING, "NO_PREROLL"))
        self.assertTrue(pipeline_reached_playing(gst, pipeline, 1000))

    def test_error_message_reports_both_state_and_return_value(self):
        gst = self._gst("NULL", "FAILURE")
        pipeline = SimpleNamespace(
            get_state=lambda _t: ("FAILURE", gst.State.NULL, "VOID_PENDING"))
        with self.assertRaises(RuntimeError) as caught:
            pipeline_reached_playing(gst, pipeline, 1000)
        message = str(caught.exception)
        self.assertIn("NULL", message)
        self.assertIn("FAILURE", message)


class MonitorReclaimTests(unittest.TestCase):
    """monitor 的"从未连上 20s 回收"不能杀掉仍在协商的会话。"""

    def setUp(self):
        self.readers = [SimpleNamespace(name=name) for name in ("primary", "third", "d435i")]
        for reader in self.readers:
            reader._meta = lambda: {"present": True}
        self.preview = WebRTCPreview(self.readers)

    def test_negotiating_session_survives_the_twenty_second_reclaim(self):
        """协商中的会话不应被 monitor 回收。"""
        state = {"id": "session", "created_at": time.monotonic() - 25,
                 "negotiating": True, "pc": object()}
        self.preview.session = state
        # 触发一次 monitor 判定逻辑
        closed = self._monitor_reclaims(state)
        self.assertFalse(closed)

    def test_idle_session_without_negotiation_is_reclaimed(self):
        state = {"id": "session", "created_at": time.monotonic() - 25,
                 "pc": object()}
        self.preview.session = state
        self.assertTrue(self._monitor_reclaims(state))

    @staticmethod
    def _monitor_reclaims(state, now=None):
        """复刻 monitor 里"从未连上"的回收判定。"""
        now = now if now is not None else time.monotonic()
        if state.get("ever_connected"):
            return False
        if state.get("negotiating"):
            return False
        return now - state.get("created_at", now) >= 20

    def test_offer_clears_negotiating_only_after_answer(self):
        source = (ROOT / "orin/webrtc_preview.py").read_text(encoding="utf-8")
        self.assertIn('"negotiating": True', source)
        self.assertIn('self.session["negotiating"] = False', source)


class PromiseTimeoutTests(unittest.TestCase):
    """_promise_value 必须有超时，否则 SIGNAL_BUDGET_SECONDS 形同虚设。"""

    class _Promise:
        """最小 Promise 替身：支持 reply()/interrupt() 与 wait()。"""

        def __init__(self):
            self._event = threading.Event()

        def reply(self, value=None):
            self.value = value
            self._event.set()

        def interrupt(self):
            self._event.set()

        def wait(self):
            self._event.wait()
            return "REPLIED"

        def get_reply(self):
            return self.value

        def has_field(self, _name):
            return False

        def get_value(self, _name):
            return None

    def test_a_promise_that_never_replies_times_out(self):
        """永不应答的 promise 必须抛出 TimeoutError，而不是永久阻塞。"""
        class Never:
            def emit(self, _signal, *args):
                pass

        gst = SimpleNamespace(
            Promise=SimpleNamespace(new=self._Promise),
            PromiseResult=SimpleNamespace(REPLIED="REPLIED", INTERRUPTED="INTERRUPTED"))
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            WebRTCPreview._promise_value(gst, Never(), "set-remote-description", None,
                                         timeout_ns=200 * 1000000)
        self.assertLess(time.monotonic() - started, 3.0)

    def test_a_promise_that_replies_returns_its_reply(self):
        class Reply:
            payload = "answer"

            def has_field(self, _name):
                return False

            def get_value(self, _name):
                return None

        class Replies:
            def emit(self, _signal, *args):
                args[-1].reply(Reply())

        gst = SimpleNamespace(
            Promise=SimpleNamespace(new=self._Promise),
            PromiseResult=SimpleNamespace(REPLIED="REPLIED", INTERRUPTED="INTERRUPTED"))
        reply = WebRTCPreview._promise_value(gst, Replies(), "create-answer", None,
                                             timeout_ns=2000000000)
        self.assertIsNotNone(reply)
        self.assertEqual(reply.payload, "answer")

    def test_answer_shares_one_promise_budget_across_three_calls(self):
        """三个 SDP promise 必须共用一份预算，不能各算 5s。"""
        recorded = []
        promise_factory = self._Promise

        class Peer:
            def emit(self, signal, *args):
                promise = args[-1]
                if signal == "create-answer":
                    answer = SimpleNamespace(
                        get_value=lambda _n: "answer",
                        has_field=lambda _n: False)
                    promise.reply(answer)
                else:
                    promise.reply(SimpleNamespace(has_field=lambda _n: False,
                                                  get_value=lambda _n: None))

        fake_gst = SimpleNamespace(
            Promise=SimpleNamespace(new=promise_factory),
            PromiseResult=SimpleNamespace(REPLIED="REPLIED", INTERRUPTED="INTERRUPTED"),
            SECOND=1000000000)

        preview = WebRTCPreview.__new__(WebRTCPreview)
        preview.lock = threading.RLock()
        preview.session = {"id": "s", "pc": Peer(), "closing": False}
        preview.gst = fake_gst
        preview.webrtc = SimpleNamespace(
            WebRTCICEGatheringState=SimpleNamespace(COMPLETE="complete"))

        original = WebRTCPreview._promise_value

        def spy(gst, element, signal, *args, timeout_ns=None):
            recorded.append((signal, timeout_ns))
            return original(gst, element, signal, *args, timeout_ns=timeout_ns)

        WebRTCPreview._promise_value = staticmethod(spy)
        try:
            preview._answer("remote")
        except Exception:
            pass
        finally:
            WebRTCPreview._promise_value = staticmethod(original)

        self.assertEqual(len(recorded), 3, "三个 SDP promise 都应带超时参数")
        timeouts = [timeout for _signal, timeout in recorded]
        self.assertTrue(all(t is not None and t > 0 for t in timeouts),
                        "每个 promise 都必须拿到正的剩余预算")
        # 共用预算：后一个的剩余时间不能大于前一个
        self.assertLessEqual(timeouts[1], timeouts[0])
        self.assertLessEqual(timeouts[2], timeouts[1])

    def test_signal_budget_covers_every_waiting_stage(self):
        """总预算必须覆盖 PLAYING、编码就绪、promise 与 ICE 四个阶段。"""
        from webrtc_preview import (SIGNAL_BUDGET_SECONDS, PIPELINE_PLAYING_TIMEOUT_NS,
                                    ENCODER_READY_TIMEOUT, ICE_GATHERING_TIMEOUT,
                                    PROMISE_TIMEOUT_NS)
        expected = (PIPELINE_PLAYING_TIMEOUT_NS / 1e9 + ENCODER_READY_TIMEOUT
                    + ICE_GATHERING_TIMEOUT + PROMISE_TIMEOUT_NS / 1e9)
        self.assertAlmostEqual(SIGNAL_BUDGET_SECONDS, expected, places=3)


class _FakePromise:
    """最小 Promise 替身：支持 reply()/interrupt() 与 wait()。"""

    def __init__(self):
        self._event = threading.Event()
        self._result = "PENDING"
        self._reply = None

    def reply(self, value=None):
        self._reply = value
        self._result = "REPLIED"
        self._event.set()

    def interrupt(self):
        self._result = "INTERRUPTED"
        self._event.set()

    def wait(self):
        self._event.wait()
        return self._result

    def get_reply(self):
        return self._reply

    def has_field(self, _name):
        return False

    def get_value(self, _name):
        return None

class OutputDimensionTests(unittest.TestCase):
    def test_never_drops_below_floor_for_a_large_source(self):
        self.assertEqual(quality_dimensions(1280, 720, 0.5), (640, 360))
        self.assertEqual(quality_dimensions(1920, 1080, 0.5), (960, 540))

    def test_keeps_a_small_source_at_its_own_size(self):
        self.assertEqual(quality_dimensions(320, 180, 1.0), (320, 180))

    def test_dimensions_are_always_even(self):
        for width, height, scale in ((1280, 720, 0.5), (848, 480, 0.5),
                                     (720, 540, 0.75), (641, 361, 0.5)):
            result = quality_dimensions(width, height, scale)
            self.assertEqual((result[0] % 2, result[1] % 2), (0, 0), (width, height, scale))


if __name__ == "__main__":
    unittest.main()

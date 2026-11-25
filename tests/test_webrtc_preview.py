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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orin"))
from webrtc_preview import (WebRTCPreview, LocalStunServer, prefer_numeric_candidates,
                            quality_dimensions)


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
                                "touched": 0, "quality": "control"}
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


if __name__ == "__main__":
    unittest.main()

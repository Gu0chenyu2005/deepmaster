# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""WebRTC sender session lifecycle checks without cameras or a GPU."""
from pathlib import Path
import sys
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orin"))
from webrtc_preview import WebRTCPreview


class WebRTCPreviewTests(unittest.TestCase):
    def setUp(self):
        self.readers = [SimpleNamespace(name=name) for name in ("primary", "third", "d435i")]
        self.preview = WebRTCPreview(self.readers)

    def test_tracks_follow_ui_camera_order(self):
        self.assertEqual(list(self.preview.readers), ["wrist", "global", "third"])
        self.assertIs(self.preview.readers["wrist"], self.readers[0])
        self.assertIs(self.preview.readers["global"], self.readers[2])

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

        self.preview.push("wrist", b"frame", 640, 480, "bgr8", 10.25)

        self.assertEqual(source.buffers, [buffer])
        self.assertEqual(buffer.pts, 8_250_000_000)
        self.assertEqual(buffer.dts, 2**64 - 1)
        self.assertEqual(buffer.duration, 1_000_000_000 // 30)


if __name__ == "__main__":
    unittest.main()

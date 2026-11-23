# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""Verify that remote preview and control-only modes reach the collector."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
_argv = sys.argv
sys.argv = ["api.py"]
import api
sys.argv = _argv


class RemotePreviewApiTests(unittest.TestCase):
    def test_video_socket_is_marked_background_and_send_queue_is_bounded(self):
        class FakeSocket:
            def __init__(self):
                self.options = {}

            def setsockopt(self, level, option, value):
                self.options[(level, option)] = value

        sock = FakeSocket()
        api._prioritize_video_socket(sock)
        self.assertEqual(sock.options[(api.socket.IPPROTO_IP, api.socket.IP_TOS)], 0x20)
        self.assertEqual(sock.options[(api.socket.SOL_SOCKET, api.socket.SO_PRIORITY)], 1)
        if hasattr(api.socket, "TCP_NOTSENT_LOWAT"):
            self.assertEqual(sock.options[(api.socket.IPPROTO_TCP, api.socket.TCP_NOTSENT_LOWAT)],
                             16 * 1024)

    def test_control_only_flag_is_forwarded_to_collector(self):
        payload = api._agent_run_payload(
            "t2", {"cfg": {"device": "one"}, "control_only": True})
        self.assertEqual(payload, {"cfg": {"device": "one"}, "control_only": True})

    def test_direct_camera_mode_is_forwarded_to_collector(self):
        payload = api._agent_run_payload(
            "cam-start", {"cfg": {"device": "one"}, "mode": "direct"})
        self.assertEqual(payload, {"cfg": {"device": "one"}, "mode": "direct"})

    def test_webrtc_camera_mode_is_forwarded_to_collector(self):
        payload = api._agent_run_payload(
            "cam-start", {"cfg": {}, "mode": "direct-webrtc"})
        self.assertEqual(payload, {"cfg": {}, "mode": "direct-webrtc"})

    def test_other_actions_do_not_forward_unrelated_fields(self):
        payload = api._agent_run_payload(
            "capture-stop", {"cfg": {"device": "one"}, "mode": "direct",
                             "control_only": True})
        self.assertEqual(payload, {"cfg": {"device": "one"}})


if __name__ == "__main__":
    unittest.main()

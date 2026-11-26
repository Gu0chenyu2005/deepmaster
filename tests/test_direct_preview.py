# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""验证直连相机配置、最新帧交付和视角映射。"""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orin"))
import camera_shm_viewer


class FakeDevice:
    def __init__(self, serial, port):
        self.info = {"serial": serial, "port": port}

    def supports(self, key):
        return key in self.info

    def get_info(self, key):
        return self.info[key]


class FakeVideoProfile:
    def __init__(self, width, height, fps, pixel_format="mjpeg", stream="color"):
        self.values = (width, height, fps, pixel_format, stream)

    def width(self):
        return self.values[0]

    def height(self):
        return self.values[1]

    def fps(self):
        return self.values[2]

    def format(self):
        return self.values[3]

    def stream_type(self):
        return self.values[4]

    def as_video_stream_profile(self):
        return self

    def is_video_stream_profile(self):
        return True


def fake_rs():
    return types.SimpleNamespace(
        stream=types.SimpleNamespace(color="color", depth="depth"),
        format=types.SimpleNamespace(mjpeg="mjpeg", bgr8="bgr8", rgb8="rgb8", yuyv="yuyv"),
    )


def profile_device(*profiles):
    return types.SimpleNamespace(query_sensors=lambda: [
        types.SimpleNamespace(get_stream_profiles=lambda: profiles)
    ])


class DirectPreviewTests(unittest.TestCase):
    def test_shared_memory_reader_closes_fd_when_mmap_fails(self):
        reader = camera_shm_viewer.SegmentReader('primary')
        reader._stop.clear()

        def stop_after_retry(_seconds):
            reader._stop.set()

        with patch.object(camera_shm_viewer.os, 'stat', return_value=types.SimpleNamespace(st_ino=7)), \
             patch.object(camera_shm_viewer.os, 'open', return_value=55), \
             patch.object(camera_shm_viewer.os, 'close') as close_fd, \
             patch.object(camera_shm_viewer.mmap, 'mmap', side_effect=OSError('mapping failed')), \
             patch.object(camera_shm_viewer.time, 'sleep', side_effect=stop_after_retry):
            reader._run()
        close_fd.assert_called_once_with(55)

    def test_auto_exposure_limit_uses_microseconds(self):
        profile = FakeVideoProfile(640, 480, 30, 'rgb8')
        applied = []

        class Sensor:
            def get_stream_profiles(self):
                return [profile]

            def supports(self, option):
                return True

            def set_option(self, option, value):
                applied.append((option, value))

            def get_option(self, option):
                return 0

            def get_option_range(self, _option):
                return types.SimpleNamespace(min=1, max=100000)

        class Device(FakeDevice):
            def query_sensors(self):
                return [Sensor()]

        device = Device('serial', 'usb')
        sensor = Sensor()
        device.query_sensors = lambda: [sensor]
        active = types.SimpleNamespace(get_device=lambda: device)

        class Pipeline:
            def start(self, _config):
                return active

            def wait_for_frames(self, _timeout):
                return types.SimpleNamespace(get_color_frame=lambda: object())

            def stop(self):
                pass

        class Config:
            def enable_device(self, _serial):
                pass

            def enable_stream(self, *_args):
                pass

        rs = types.SimpleNamespace(
            context=lambda: types.SimpleNamespace(query_devices=lambda: [device]),
            camera_info=types.SimpleNamespace(serial_number='serial'),
            option=types.SimpleNamespace(frames_queue_size='queue', global_time_enabled='global',
                                        auto_exposure_priority='priority', exposure='exposure',
                                        auto_exposure_limit='limit'),
            pipeline=Pipeline, config=Config,
            stream=types.SimpleNamespace(color='color'),
            format=types.SimpleNamespace(mjpeg='mjpeg', bgr8='bgr8', rgb8='rgb8', yuyv='yuyv'))
        reader = camera_shm_viewer.DirectCameraReader('primary', 'serial', rs)
        pipeline, _frames = reader._open()
        self.assertIsInstance(pipeline, Pipeline)
        self.assertIn(('limit', 1000000.0 / 30), applied)
        self.assertAlmostEqual(reader.exposure_diagnostics['auto_exposure_limit_ms'], 33.333, places=2)

    def test_direct_reader_recovers_from_a_transient_frame_timeout(self):
        reader = camera_shm_viewer.DirectCameraReader("primary", "serial", fake_rs())

        class Pipeline:
            def __init__(self):
                self.calls = 0
                self.stopped = False

            def wait_for_frames(self, timeout_ms):
                self.assert_timeout = timeout_ms
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("Frame didn't arrive within 1000")
                reader._stop.set()
                return types.SimpleNamespace(get_color_frame=lambda: None)

            def stop(self):
                self.stopped = True

        pipeline = Pipeline()
        reader._open = lambda: (pipeline, None)
        reader._run()

        self.assertEqual(pipeline.calls, 2)
        self.assertEqual(pipeline.assert_timeout, 5000)
        self.assertTrue(pipeline.stopped)

    def test_camera_http_endpoints_require_ticket(self):
        previous_token = camera_shm_viewer.ACCESS_TOKEN
        previous_segments = list(camera_shm_viewer.SEGMENTS)
        camera_shm_viewer.ACCESS_TOKEN = 'camera-ticket'
        camera_shm_viewer.SEGMENTS.clear()
        server = ThreadingHTTPServer(('127.0.0.1', 0), camera_shm_viewer.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:%d' % server.server_port
        try:
            with self.assertRaises(HTTPError) as denied:
                urlopen(base + '/health')
            self.assertEqual(denied.exception.code, 401)
            with urlopen(base + '/health?token=camera-ticket') as response:
                self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            camera_shm_viewer.ACCESS_TOKEN = previous_token
            camera_shm_viewer.SEGMENTS[:] = previous_segments

    def test_native_frame_rate_precedes_compressed_format(self):
        compressed_30 = FakeVideoProfile(1280, 720, 30)
        raw_60 = FakeVideoProfile(848, 480, 60, "rgb8")
        profiles = camera_shm_viewer.select_direct_profiles(
            profile_device(compressed_30, raw_60), fake_rs())
        self.assertEqual(profiles, [raw_60, compressed_30])

    def test_same_rate_prefers_native_jpeg_then_highest_resolution(self):
        small = FakeVideoProfile(640, 480, 60)
        large = FakeVideoProfile(1280, 720, 60)
        raw = FakeVideoProfile(1280, 720, 60, "bgr8")
        profiles = camera_shm_viewer.select_direct_profiles(
            profile_device(raw, small, large), fake_rs())
        self.assertEqual(profiles, [large, small, raw])

    def test_raw_only_cameras_are_supported(self):
        rgb = FakeVideoProfile(848, 480, 60, "rgb8")
        yuyv = FakeVideoProfile(1280, 720, 30, "yuyv")
        profiles = camera_shm_viewer.select_direct_profiles(
            profile_device(yuyv, rgb), fake_rs())
        self.assertEqual(profiles, [rgb, yuyv])

    def test_webrtc_rejects_mjpeg_and_prefers_hardware_convertible_yuyv(self):
        compressed = FakeVideoProfile(1280, 720, 60)
        bgr = FakeVideoProfile(1280, 720, 60, "bgr8")
        yuyv = FakeVideoProfile(1280, 720, 60, "yuyv")
        with patch.object(camera_shm_viewer, "DIRECT_WEBRTC", True):
            profiles = camera_shm_viewer.select_direct_profiles(
                profile_device(compressed, bgr, yuyv), fake_rs())
        self.assertEqual(profiles, [yuyv, bgr])

    def test_preview_size_limits_exclude_expensive_and_unusable_profiles(self):
        too_large = FakeVideoProfile(1920, 1080, 60)
        too_small = FakeVideoProfile(320, 240, 90)
        preferred = FakeVideoProfile(848, 480, 60)
        depth = FakeVideoProfile(848, 480, 90, "rgb8", "depth")
        unsupported = FakeVideoProfile(1280, 720, 60, "z16")
        profiles = camera_shm_viewer.select_direct_profiles(
            profile_device(too_large, depth, too_small, unsupported, preferred), fake_rs())
        self.assertEqual(profiles, [preferred])

    def test_unusual_camera_sizes_remain_usable(self):
        native = FakeVideoProfile(1920, 1080, 30, "rgb8")
        profiles = camera_shm_viewer.select_direct_profiles(profile_device(native), fake_rs())
        self.assertEqual(profiles, [native])

    def test_only_depth_camera_has_no_color_profiles(self):
        depth = FakeVideoProfile(848, 480, 90, "rgb8", "depth")
        self.assertEqual(camera_shm_viewer.select_direct_profiles(
            profile_device(depth), fake_rs()), [])

    def _ready_reader(self, seq=1):
        reader = camera_shm_viewer.DirectCameraReader("primary", "serial", fake_rs())
        with reader.condition:
            reader.jpeg = b"native-jpeg"
            reader.seq = seq
            reader.present = True
            reader.error = ""
            reader.width, reader.height = 1280, 720
            reader.source_fps = 60
            reader.fps = 58.9
            reader.captured_at = time.monotonic()
        return reader

    def test_native_jpeg_is_forwarded_without_software_encoding(self):
        reader = self._ready_reader()
        with patch.object(camera_shm_viewer, "encode_jpeg", side_effect=AssertionError("re-encoded")):
            jpg, meta = reader.snapshot("remote")
        self.assertEqual(jpg, b"native-jpeg")
        self.assertEqual(meta["source_fps"], 60)

    def test_raw_rgb_fallback_converts_color_and_encodes_once_per_frame(self):
        reader = self._ready_reader()
        reader.jpeg = None
        reader.width, reader.height = 2, 1
        reader.raw = bytes((10, 20, 30, 40, 50, 60))
        reader.pixel_format = "rgb8"
        cv2 = types.SimpleNamespace(
            COLOR_RGB2BGR="rgb-to-bgr",
            cvtColor=lambda image, conversion: image[:, :, ::-1],
        )
        with patch.object(camera_shm_viewer, "_jpeg", ("cv2", cv2)), \
             patch.object(camera_shm_viewer, "encode_jpeg", return_value=b"encoded") as encode:
            first, _ = reader.snapshot("remote")
            second, _ = reader.snapshot("remote")
        self.assertEqual((first, second), (b"encoded", b"encoded"))
        encode.assert_called_once()
        self.assertEqual(encode.call_args.args[0].tolist(), [[[30, 20, 10], [60, 50, 40]]])

    def test_stalled_camera_is_reported_without_claiming_live_frames(self):
        reader = self._ready_reader()
        reader.captured_at = time.monotonic() - 3
        _, meta = reader.snapshot("remote")
        self.assertFalse(meta["present"])
        self.assertGreaterEqual(meta["age_ms"], 3000)
        self.assertTrue(meta["error"])

    def test_wait_snapshot_returns_only_latest_new_frame(self):
        reader = self._ready_reader(seq=9)
        jpg, meta = reader.wait_snapshot(2, "remote", timeout=0.1)
        self.assertEqual(jpg, b"native-jpeg")
        self.assertEqual(meta["seq"], 9)

    def test_wait_snapshot_does_not_repeat_an_unchanged_frame(self):
        reader = self._ready_reader(seq=9)
        jpg, meta = reader.wait_snapshot(9, "remote", timeout=0.02)
        self.assertIsNone(jpg)
        self.assertEqual(meta["seq"], 9)

    def test_condition_wakes_stream_as_soon_as_new_frame_arrives(self):
        reader = self._ready_reader(seq=1)
        waiting = threading.Event()
        output = []
        condition_wait = reader.condition.wait

        def mark_wait(*args, **kwargs):
            waiting.set()
            return condition_wait(*args, **kwargs)

        with patch.object(reader.condition, "wait", side_effect=mark_wait):
            thread = threading.Thread(target=lambda: output.append(
                reader.wait_snapshot(1, "remote", timeout=1.0)), daemon=True)
            thread.start()
            self.assertTrue(waiting.wait(timeout=0.5))
            with reader.condition:
                reader.jpeg = b"latest-jpeg"
                reader.seq = 5
                reader.captured_at = time.monotonic()
                reader.condition.notify_all()
            thread.join(timeout=0.5)

        self.assertFalse(thread.is_alive(), "new frames must wake blocked stream writers")
        self.assertEqual(output[0][0], b"latest-jpeg")
        self.assertEqual(output[0][1]["seq"], 5)

    def test_health_reports_selected_native_rate(self):
        reader = self._ready_reader()
        health = reader.health()
        self.assertEqual(health["fps_header"], 60)
        self.assertEqual(health["fps_measured"], 58.9)

    def test_all_discovered_readers_are_started(self):
        started = []
        readers = [types.SimpleNamespace(start=lambda index=i: started.append(index))
                   for i in range(3)]
        camera_shm_viewer.start_segments(readers)
        self.assertEqual(started, [0, 1, 2])

    def test_direct_roles_follow_saved_ports_after_serial_replacement(self):
        primary = "/sys/usb/2-1/2-1:1.0/video4linux/video4"
        third = "/sys/usb/2-2/2-2:1.0/video4linux/video8"
        wrist = "/sys/usb/2-3/2-3:1.0/video4linux/video12"
        rs = types.SimpleNamespace(
            camera_info=types.SimpleNamespace(serial_number="serial", physical_port="port"),
            context=lambda: types.SimpleNamespace(query_devices=lambda: [
                FakeDevice("replacement-wrist", wrist),
                FakeDevice("replacement-third", third),
                FakeDevice("replacement-primary", primary),
            ]),
        )
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as config:
            config.write("primary=/sys/usb/2-1/2-1\n")
            config.write("third=/sys/usb/2-2/2-2\n")
            config.write("d435i=/sys/usb/2-3/2-3\n")
            config.flush()
            with patch.dict(sys.modules, {"pyrealsense2": rs}), \
                 patch.dict(os.environ, {"ARMDOG_CAMERA_PORTS_FILE": config.name}):
                readers = camera_shm_viewer.direct_camera_segments()

        self.assertEqual([reader.name for reader in readers], ["primary", "third", "d435i"])
        self.assertEqual([reader.serial for reader in readers], [
            "replacement-primary", "replacement-third", "replacement-wrist"])

    def test_busy_error_names_other_process_holders(self):
        busy = "xioctl(VIDIOC_S_FMT) failed, errno=16 Last Error: Device or resource busy"
        with patch.object(camera_shm_viewer, "_video_device_holders",
                          return_value=({"/dev/video4": {4321}}, True)), \
             patch.object(Path, "read_text", return_value="rl_deploy\n"):
            detail = camera_shm_viewer._describe_busy(busy)
        self.assertTrue(detail.startswith("相机设备忙"))
        self.assertIn("/dev/video4", detail)
        self.assertIn("4321(rl_deploy)", detail)

    def test_busy_error_without_holders_does_not_guess_usb_state(self):
        busy = "xioctl(VIDIOC_S_FMT) failed, errno=16 Last Error: Device or resource busy"
        with patch.object(camera_shm_viewer, "_video_device_holders",
                          return_value=({}, True)):
            detail = camera_shm_viewer._describe_busy(busy)
        self.assertEqual(detail, "相机设备忙；未查到其他进程占用")

    def test_busy_error_reports_incomplete_process_scan(self):
        busy = "xioctl(VIDIOC_S_FMT) failed, errno=16 Last Error: Device or resource busy"
        with patch.object(camera_shm_viewer, "_video_device_holders",
                          return_value=({}, False)):
            detail = camera_shm_viewer._describe_busy(busy)
        self.assertEqual(detail, "相机设备忙；占用进程检查不完整")

    def test_camera_read_errors_are_shortened(self):
        detail = camera_shm_viewer._describe_busy(
            "Frame didn't arrive within 5000; " + "detail " * 100)
        self.assertEqual(detail, "相机取帧超时")

    def test_non_busy_error_is_unchanged(self):
        detail = camera_shm_viewer._describe_busy("camera disconnected")
        self.assertEqual(detail, "相机已断开")


if __name__ == "__main__":
    unittest.main()

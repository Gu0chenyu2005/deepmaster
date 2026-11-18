# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""验证上传清洗的帧同步、失败隔离及质检职责拆分。"""
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import h5py
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
with patch.object(sys, "argv", ["api.py"]):
    import api
import auto_qc
import data_clean


def make_episode(path, frames=70, bad_timestamps=False):
    images = []
    for i in range(frames):
        value = 0 if i == 0 else (80 if 22 <= i <= 42 else (i * 3) % 220 + 20)
        output = io.BytesIO()
        Image.new("RGB", (32, 24), (value, value, value)).save(output, "JPEG", quality=98)
        images.append(output.getvalue())
    lengths = np.asarray([len(frame) for frame in images], dtype=np.int32)
    packed = np.zeros((frames, int(lengths.max())), dtype=np.uint8)
    for i, frame in enumerate(images):
        packed[i, :len(frame)] = np.frombuffer(frame, dtype=np.uint8)
    qpos = np.zeros((frames, 7), dtype=np.float32)
    for i in range(frames):
        qpos[i, 0] = 0.02 * min(i, 22) + 0.02 * max(0, i - 42)
    timestamps = np.arange(frames, dtype=np.int64) * 66_666_667
    if bad_timestamps:
        timestamps[31] = timestamps[30]
    with h5py.File(path, "w") as h5:
        h5.attrs["episode_len"] = frames
        h5["metadata/timestamps_ns"] = timestamps
        h5["observations/timestamps"] = timestamps / 1e6
        h5["observations/qpos"] = qpos
        h5["action/action"] = np.arange(frames * 7, dtype=np.float32).reshape(frames, 7)
        for name in ("d435i_rgb", "rgb", "third_rgb"):
            h5["observations/" + name] = packed
            h5["observations/" + name + "_len"] = lengths


class DataCleanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_clean_uses_one_keep_index_for_all_streams_and_timestamps(self):
        path = self.root / "episode_4_vla_valid.hdf5"
        make_episode(path)
        with h5py.File(path, "r") as original:
            source_timestamps = original["metadata/timestamps_ns"][...]
            source_actions = original["action/action"][...]
        report = data_clean.clean_episode(path)
        self.assertEqual(report["first_black_cameras"], [
            "observations/d435i_rgb", "observations/rgb", "observations/third_rgb"])
        self.assertGreater(report["stall_removed_frames"], 0)
        with h5py.File(path, "r") as cleaned:
            keep = np.asarray(report["removed_indices"], dtype=int)
            indices = np.delete(np.arange(70), keep)
            self.assertEqual(cleaned["metadata/timestamps_ns"].shape[0], len(indices))
            np.testing.assert_array_equal(cleaned["metadata/timestamps_ns"][...], source_timestamps[indices])
            np.testing.assert_array_equal(cleaned["observations/timestamps"][...],
                                          source_timestamps[indices] / 1e6)
            np.testing.assert_array_equal(cleaned["action/action"][...], source_actions[indices])
            np.testing.assert_allclose(cleaned["observations/qpos"][:, 0],
                                       (0.02 * np.minimum(indices, 22) + 0.02 * np.maximum(0, indices - 42)))
            for name in ("d435i_rgb", "rgb", "third_rgb"):
                self.assertEqual(cleaned["observations/" + name].shape[0], len(indices))
                self.assertEqual(cleaned["observations/" + name + "_len"].shape[0], len(indices))
            self.assertEqual(int(cleaned.attrs["episode_len"]), len(indices))
            self.assertEqual(int(cleaned.attrs["deepmaster_removed_frames"]), len(keep))
            temporal_lengths = []
            cleaned.visititems(lambda name, obj: temporal_lengths.append(obj.shape[0])
                               if isinstance(obj, h5py.Dataset) and obj.ndim else None)
            self.assertEqual(set(temporal_lengths), {len(indices)})

    def test_invalid_timestamp_fails_closed_without_changing_source(self):
        path = self.root / "episode_0_vla_valid.hdf5"
        make_episode(path, bad_timestamps=True)
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "严格递增"):
            data_clean.clean_episode(path)
        self.assertEqual(path.read_bytes(), before)

    def test_upload_does_not_publish_episode_with_invalid_timestamps(self):
        source = self.root / "episode_0_vla_valid.hdf5"
        make_episode(source, bad_timestamps=True)
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tf:
            tf.add(source, arcname=source.name)
        archive.seek(0)
        archive.headers = {"Content-Length": str(len(archive.getvalue()))}
        target_root = self.root / "datasets"
        target = target_root / "incoming"
        with patch.object(api, "agent_url", return_value="http://collector"), \
             patch.object(api, "_agent_rel", side_effect=lambda value: value), \
             patch.object(api, "_agent_call", return_value={"state": "ready"}), \
             patch.object(api, "_agent_open", return_value=archive), \
             patch.object(api, "INIT_DATA", str(target_root)), \
             patch.object(api, "PROJECT_DIR", str(self.root)):
            with self.assertRaisesRegex(ValueError, "严格递增"):
                api._pull_archive(str(target), "tester")
        self.assertFalse(target.exists())

    def test_upload_cleans_staging_before_publishing_and_keeps_collector_source(self):
        source_dir = self.root / "collector"
        source_dir.mkdir()
        source = source_dir / "episode_0_vla_valid.hdf5"
        make_episode(source)
        original = source.read_bytes()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tf:
            tf.add(source, arcname=source.name)
        archive.seek(0)
        archive.headers = {"Content-Length": str(len(archive.getvalue()))}
        target_root = self.root / "datasets"
        target = target_root / "incoming"
        env = patch.dict(os.environ, {"DM_DB": str(self.root / "test.db")})
        env.start()
        self.addCleanup(env.stop)
        api.db.migrate()
        with patch.object(api, "agent_url", return_value="http://collector"), \
             patch.object(api, "_agent_rel", side_effect=lambda value: value), \
             patch.object(api, "_agent_call", return_value={"state": "ready"}), \
             patch.object(api, "_agent_open", return_value=archive), \
             patch.object(api, "INIT_DATA", str(target_root)), \
             patch.object(api, "PROJECT_DIR", str(self.root)):
            result = api._pull_archive(str(target), "tester")
        self.assertEqual(result["cleaned_episodes"][0]["old_frames"], 70)
        self.assertGreater(len(result["cleaned_episodes"][0]["removed_indices"]), 1)
        stored = target / source.name
        self.assertTrue(stored.exists())
        self.assertEqual(source.read_bytes(), original)
        with h5py.File(stored, "r") as h5:
            self.assertEqual(h5["metadata/timestamps_ns"].shape[0], h5["observations/qpos"].shape[0])
        self.assertTrue((target / "deepmaster_cleaning_report.json").is_file())

    def test_autocheck_does_not_report_black_or_frozen_video(self):
        path = self.root / "black.h5"
        output = io.BytesIO()
        Image.new("RGB", (32, 24), (0, 0, 0)).save(output, "JPEG")
        payload = output.getvalue()
        with h5py.File(path, "w") as h5:
            h5["observations/rgb"] = np.tile(np.frombuffer(payload, dtype=np.uint8), (20, 1))
            h5["observations/rgb_len"] = np.full(20, len(payload), dtype=np.int32)
            issues = auto_qc.check_image_stream("observations/rgb", h5["observations/rgb"],
                                                 dict(h5.items()), {"check_occlusion": False})
        self.assertFalse(any("黑屏" in reason or "冻结" in reason for _, reason in issues), issues)
        self.assertNotIn("black_run_limit", api.QC_RULES)
        self.assertNotIn("freeze_run_limit", api.QC_RULES)


if __name__ == "__main__":
    unittest.main()

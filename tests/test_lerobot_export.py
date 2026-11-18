# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""验证合格数据筛选、导出隔离及 LeRobot v3 文件。"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import h5py
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import lerobot_export as exporter
with patch.object(sys, "argv", ["api.py"]):
    import api
with patch.object(sys, "argv", ["run_web.py"]):
    import run_web


def episode(path, fps=15):
    from PIL import Image
    with h5py.File(path, "w") as f:
        f.attrs["fps"] = fps
        f["metadata/timestamps_ns"] = np.arange(6, dtype=np.int64) * int(1e9 / fps)
        for name, dim in [("observations/qpos", 7), ("observations/leg_joint_pos", 16),
                          ("observations/motor_torque", 6), ("action/action", 7)]:
            f[name] = np.arange(6 * dim, dtype=np.float32).reshape(6, dim) / 100
        for i, name in enumerate(("d435i_rgb", "rgb", "third_rgb")):
            frames = []
            for frame in range(6):
                buf = io.BytesIO()
                Image.new("RGB", (32, 24), (30+i*60, frame*20, 200)).save(buf, format="JPEG")
                frames.append(buf.getvalue())
            lengths = np.array([len(b) for b in frames])
            data = np.zeros((6, max(lengths)), dtype=np.uint8)
            for row, raw in enumerate(frames):
                data[row, :len(raw)] = np.frombuffer(raw, dtype=np.uint8)
            f["observations/" + name] = data
            f["observations/" + name + "_len"] = lengths


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "datasets" / "sample"
        self.source.mkdir(parents=True)
        self.env = patch.dict(os.environ, {"DM_DB": str(self.root / "test.db")})
        self.env.start()
        self.addCleanup(self.env.stop)
        api.db.migrate()
        for key, value in [("PROJECT_DIR", str(self.root)), ("INIT_DATA", str(self.root / "datasets")),
                           ("ALLOW_ROOTS", [str(self.root / "datasets")]),
                           ("DEFAULT_DATA", str(self.root / "datasets")), ("_DATA_DIR_NAMES", {"datasets"})]:
            p = patch.object(api, key, value)
            p.start()
            self.addCleanup(p.stop)

    def manager(self):
        return exporter.ExportManager(self.root / "exports")

    def create(self, number=0, fps=15):
        path = self.source / ("episode_%d_vla_valid.hdf5" % number)
        episode(path, fps)
        with h5py.File(path) as h5:
            schema = api.detect_schema(h5)
        return path, schema

    def test_exports_only_good_without_changing_sources_or_reviews(self):
        self.create(0)
        self.create(1)
        self.create(2)
        with api.db.connect() as conn:
            for ep, state in [(0, "good"), (1, "bad"), (2, "pending")]:
                conn.execute("INSERT INTO reviews(dir_path,episode,status,reviewed_at) VALUES(?,?,?,?)",
                             ("datasets/sample", ep, state, api.db.now()))
        manager = self.manager()
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.iterdir()}
        with patch.object(api, "export_manager", return_value=manager), patch.object(manager, "_run"):
            job = api.op_lerobot_export("datasets/sample", {"account": "tester"})
        spec = json.loads((manager.root / job["id"] / "input.json").read_text())
        self.assertEqual([s["episode"] for s in spec["sources"]], [0])
        self.assertEqual(spec["task"], "sample")
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source.iterdir()})
        with api.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM reviews").fetchone()[0], 3)

    def test_empty_selection_creates_no_job(self):
        manager = self.manager()
        with self.assertRaisesRegex(ValueError, "没有标记为合格"):
            manager.start("datasets/sample", [], "sample", {})
        self.assertEqual(list(manager.root.iterdir()), [])

    def test_deduplicates_running_job_and_recovers_after_restart(self):
        path, schema = self.create()
        manager = self.manager()
        with patch.object(manager, "_run"):
            job = manager.start("datasets/sample", [(0, path)], "sample", {0: schema})
            again = manager.start("datasets/sample", [(0, path)], "sample", {0: schema})
            self.assertEqual(job["id"], again["id"])
            with self.assertRaisesRegex(ValueError, "另一个"):
                manager.start("other", [(0, path)], "other", {0: schema})
        recovered = self.manager().get(job["id"])
        self.assertEqual(recovered["state"], "error")
        with self.assertRaises(ValueError):
            manager.get("../dataset")

    def test_download_ticket_is_bound_to_job_and_expires(self):
        ticket = exporter.download_ticket("abc", b"secret")
        self.assertTrue(exporter.check_ticket("abc", ticket, b"secret"))
        self.assertFalse(exporter.check_ticket("other", ticket, b"secret"))
        self.assertFalse(exporter.check_ticket("abc", ticket, b"wrong"))
        expired = exporter.download_ticket("abc", b"secret", int(time.time())-1)
        self.assertFalse(exporter.check_ticket("abc", expired, b"secret"))

    def test_proxy_streams_authorized_download_and_blocks_static_export_access(self):
        manager = self.manager()
        job_id = "a" * 32
        folder = manager.root / job_id
        folder.mkdir()
        body = b"test-download-" * 10000
        (folder / "dataset.tar.gz").write_bytes(body)
        exporter.write_json(folder / "job.json", dict(id=job_id, state="done", dir="datasets/sample",
                                                      created_at=time.time()))
        backend = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), run_web.H)
        for server in (backend, proxy):
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        with patch.object(api, "export_manager", return_value=manager), \
             patch.object(api.auth, "_secret", return_value=b"test-secret"), \
             patch.object(api.auth, "current_user", return_value=None), \
             patch.object(run_web, "API_PORT", backend.server_port), \
             patch.object(run_web, "ROOT", str(self.root)):
            base = "http://127.0.0.1:%d" % proxy.server_port
            url = base + "/api/lerobot-export/download?id=" + job_id
            with self.assertRaises(HTTPError) as caught:
                urlopen(url, timeout=5)
            self.assertEqual(caught.exception.code, 401)
            ticket = exporter.download_ticket(job_id, b"test-secret")
            with urlopen(url + "&ticket=" + ticket, timeout=5) as response:
                self.assertEqual(response.read(), body)
                self.assertIn("attachment", response.headers["Content-Disposition"])
                self.assertEqual(int(response.headers["Content-Length"]), len(body))
            with self.assertRaises(HTTPError) as caught:
                urlopen(base + "/exports/" + job_id + "/dataset.tar.gz", timeout=5)
            self.assertEqual(caught.exception.code, 404)

    @unittest.skipUnless(importlib.util.find_spec("pyarrow") and shutil.which("ffmpeg"), "需要 pyarrow 和 ffmpeg")
    def test_real_conversion_preserves_frames_and_camera_roles(self):
        import pyarrow.parquet as pq
        path, schema = self.create(4)
        manager = self.manager()
        with patch.object(manager, "_run"):
            job = manager.start("datasets/sample", [(4, path)], "sample", {4: schema})
        folder = manager.root / job["id"]
        before = path.read_bytes()
        exporter.convert(folder)
        job = manager.get(job["id"])
        self.assertEqual(job["state"], "done")
        dataset = Path(job["output_path"])
        info = json.loads((dataset / "meta/info.json").read_text())
        self.assertEqual((info["codebase_version"], info["total_frames"], info["total_episodes"]), ("v3.0", 6, 1))
        rows = pq.read_table(dataset / "data/chunk-000/file-000.parquet").to_pylist()
        self.assertEqual(len(rows[0]["observation.state"]), 29)
        self.assertEqual(rows[0]["episode_index"], 0)
        self.assertEqual(rows[-1]["frame_index"], 5)
        self.assertEqual(len(list((dataset / "videos").rglob("*.mp4"))), 3)
        manifest = json.loads((dataset / "conversion_manifest.json").read_text())
        self.assertEqual(manifest["episodes"][0]["source_episode_number"], 4)
        self.assertEqual(manifest["episodes"][0]["camera_mapping"]["wrist"], "observations/d435i_rgb")
        self.assertEqual(before, path.read_bytes())
        self.assertTrue((folder / "dataset.tar.gz").is_file())

    @unittest.skipUnless(importlib.util.find_spec("pyarrow") and shutil.which("ffmpeg"), "需要 pyarrow 和 ffmpeg")
    def test_worker_failure_removes_partial_output_and_preserves_source(self):
        path, schema = self.create()
        with h5py.File(path, "a") as h5:
            h5["observations/qpos"][0, 0] = np.nan
        manager = self.manager()
        with patch.object(manager, "_run"):
            job = manager.start("datasets/sample", [(0, path)], "sample", {0: schema})
        before = path.read_bytes()
        manager._run(job["id"])
        folder = manager.root / job["id"]
        result = manager.get(job["id"])
        self.assertEqual(result["state"], "error")
        self.assertIn("非有限值", result["error"])
        self.assertFalse((folder / "dataset.partial").exists())
        self.assertFalse((folder / "dataset.tar.gz").exists())
        self.assertEqual(before, path.read_bytes())


if __name__ == "__main__":
    unittest.main()

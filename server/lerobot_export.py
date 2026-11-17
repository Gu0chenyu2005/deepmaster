# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""独立的 LeRobot v3 导出任务与转换器。"""

import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import threading
import time

import h5py
import numpy as np

ARM = ["arm_joint_%d" % i for i in range(1, 7)] + ["gripper"]
LEG = [p + "_" + j for p in ("front_left", "front_right", "rear_left", "rear_right")
       for j in ("hip_roll", "hip_pitch", "knee", "wheel")]
STATE = ARM + LEG + ["arm_joint_%d_torque" % i for i in range(1, 7)]


def write_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class ExportManager:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        for p in self.root.glob("*/job.json"):
            job = json.loads(p.read_text())
            if job["state"] in ("queued", "running", "packaging"):
                job.update(state="error", error="服务已重启，请重新导出")
                write_json(p, job)
                shutil.rmtree(p.parent / "dataset.partial", ignore_errors=True)

    def get(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("无效的导出任务")
        try:
            return json.loads((self.root / job_id / "job.json").read_text())
        except FileNotFoundError:
            raise ValueError("导出任务不存在")

    def latest(self, directory):
        jobs = [json.loads(p.read_text()) for p in self.root.glob("*/job.json")]
        jobs = [j for j in jobs if j["dir"] == directory]
        return max(jobs, key=lambda j: j["created_at"]) if jobs else None

    def start(self, directory, files, task, schemas):
        with self.lock:
            for p in self.root.glob("*/job.json"):
                j = json.loads(p.read_text())
                if j["state"] in ("queued", "running", "packaging"):
                    if j["dir"] == directory:
                        return j
                    raise ValueError("另一个导出任务正在运行，请稍后重试")
            if not files:
                raise ValueError("当前目录没有标记为合格的数据")
            job_id = secrets.token_hex(16)
            folder = self.root / job_id
            folder.mkdir()
            job = dict(id=job_id, dir=directory, state="queued", done=0,
                       total=len(files), current="", error="", created_at=time.time(),
                       output_path="", archive_bytes=0)
            sources = []
            for ep, path in files:
                stat = os.stat(path)
                sources.append(dict(episode=ep, path=os.path.realpath(path),
                                    size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                                    schema=schemas[ep]))
            write_json(folder / "input.json", dict(task=task, sources=sources))
            write_json(folder / "job.json", job)
            threading.Thread(target=self._run, args=(job_id,), daemon=True).start()
            return job

    def _run(self, job_id):
        folder = self.root / job_id
        try:
            with (folder / "worker.log").open("w") as log:
                result = subprocess.run([os.environ.get("DM_EXPORT_PYTHON") or sys.executable, __file__, str(folder)],
                                        stdout=log, stderr=log)
            if result.returncode:
                job = self.get(job_id)
                if job["state"] != "error":
                    job.update(state="error", error="导出进程异常退出，请查看服务端导出日志")
                    write_json(folder / "job.json", job)
        except Exception as e:
            job = self.get(job_id)
            job.update(state="error", error=str(e))
            write_json(folder / "job.json", job)


def download_ticket(job_id, secret, expires=None):
    expires = int(expires or time.time() + 600)
    body = "%s:%d" % (job_id, expires)
    signature = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
    return "%d.%s" % (expires, signature)


def check_ticket(job_id, ticket, secret):
    try:
        expiry, signature = ticket.split(".", 1)
        expiry = int(expiry)
        return expiry >= time.time() and hmac.compare_digest(
            download_ticket(job_id, secret, expiry), ticket)
    except (ValueError, TypeError):
        return False


def moments(values, image=False):
    a = np.asarray(values, dtype=np.float64)
    result = {"min": a.min(0), "max": a.max(0), "mean": a.mean(0), "std": a.std(0)}
    if image:
        result = {k: v.reshape(3, 1, 1) for k, v in result.items()}
    return {**{k: np.atleast_1d(v).tolist() for k, v in result.items()}, "count": [len(a)]}


def combine_stats(parts):
    total = {}
    for key in parts[0]:
        items = [p[key] for p in parts]
        counts = np.array([i["count"][0] for i in items])
        n = int(counts.sum())
        means = np.array([i["mean"] for i in items])
        w = counts.reshape((-1,) + (1,) * (means.ndim - 1)) / n
        mean = (means * w).sum(0)
        variance = ((np.array([i["std"] for i in items]) ** 2 + (means - mean) ** 2) * w).sum(0)
        total[key] = dict(min=np.min([i["min"] for i in items], 0).tolist(),
                          max=np.max([i["max"] for i in items], 0).tolist(),
                          mean=mean.tolist(), std=np.sqrt(variance).tolist(), count=[n])
    return total


def read_rgb(h5, camera, index):
    from PIL import Image
    ds = h5[camera["ds"]]
    row = ds[index]
    if row.ndim == 3 and row.shape[-1] == 3:
        return np.asarray(row, dtype=np.uint8)
    raw = row.tobytes()
    if camera.get("len_ds"):
        length = int(h5[camera["len_ds"]][index])
        if not 0 < length <= len(raw):
            raise ValueError("相机 %s 第 %d 帧长度无效" % (camera["key"], index))
        raw = raw[:length]
    with Image.open(io.BytesIO(raw)) as image:
        return np.array(image.convert("RGB"))


def convert(folder):
    import pyarrow as pa
    import pyarrow.parquet as pq
    import pandas as pd
    from PIL import Image

    folder = Path(folder)
    job = json.loads((folder / "job.json").read_text())
    spec = json.loads((folder / "input.json").read_text())
    output = folder / "dataset.partial"
    output.mkdir()
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("服务器缺少 ffmpeg，请重新运行服务端部署脚本")
    features = {"observation.state": dict(dtype="float32", shape=[29], names=STATE),
                "action": dict(dtype="float32", shape=[7], names=ARM)}
    for key in ("timestamp", "frame_index", "episode_index", "index", "task_index"):
        features[key] = dict(dtype="float32" if key == "timestamp" else "int64", shape=[1], names=None)
    episodes, all_stats, reports = [], [], []
    offset, fps = 0, None
    for index, source in enumerate(spec["sources"]):
        job.update(state="running", current=Path(source["path"]).name)
        write_json(folder / "job.json", job)
        before = os.stat(source["path"])
        if (before.st_size, before.st_mtime_ns) != (source["size"], source["mtime_ns"]):
            raise ValueError("源文件已变化，请重新导出：" + job["current"])
        sch = source["schema"]
        with h5py.File(source["path"], "r") as h5:
            arrays = []
            for name, dims in (("qpos_ds", 7), ("leg_ds", 16), ("torque_ds", 6), ("action_ds", 7)):
                if not sch.get(name) or h5[sch[name]].ndim != 2 or h5[sch[name]].shape[1] < dims:
                    raise ValueError("%s 缺少 %s 的 %d 维数据" % (job["current"], name, dims))
                arrays.append(np.asarray(h5[sch[name]][:, :dims], dtype=np.float32))
            n = len(arrays[0])
            if not n or any(len(a) != n or not np.isfinite(a).all() for a in arrays):
                raise ValueError("%s 状态或动作长度不一致、为空或包含非有限值" % job["current"])
            source_fps = sch.get("fps")
            if not source_fps and sch.get("timestamps"):
                name, scale = sch["timestamps"]
                ts = np.asarray(h5[name][:], dtype=float).reshape(-1)
                delta = np.diff(ts) * scale if scale < 1 else np.diff(ts) / scale
                valid = delta[delta > 0]
                if len(valid):
                    source_fps = round(1 / float(np.median(valid)))
            if not source_fps or not np.isfinite(source_fps) or source_fps <= 0:
                raise ValueError("无法识别源文件帧率：" + job["current"])
            rate = round(float(source_fps))
            if rate < 1 or abs(float(source_fps) - rate) > 0.1 or (fps is not None and rate != fps):
                raise ValueError("当前合格数据的帧率不一致或不是整数，请分目录导出")
            fps = rate
            chunk, file_index = divmod(index, 1000)
            row = dict(episode_index=index, tasks=[spec["task"]], length=n,
                       dataset_from_index=offset, dataset_to_index=offset+n,
                       **{"data/chunk_index": chunk, "data/file_index": file_index,
                          "meta/episodes/chunk_index": 0, "meta/episodes/file_index": 0})
            stats = {}
            cameras = {c["key"]: c for c in sch["cams"]}
            for key in ("wrist", "global", "third"):
                cam = cameras.get(key)
                if not cam or len(h5[cam["ds"]]) != n:
                    raise ValueError("%s 缺少同步的 %s 相机数据" % (job["current"], key))
                first = read_rgb(h5, cam, 0)
                height, width = first.shape[:2]
                if width % 2 or height % 2:
                    raise ValueError("H.264 要求相机画面宽高为偶数")
                video_key = "observation.images.%s_rgb" % key
                feature = dict(dtype="video", shape=[height, width, 3], names=["height", "width", "channels"],
                               info={"video.height": height, "video.width": width, "video.codec": "h264",
                                     "video.pix_fmt": "yuv420p", "video.fps": fps, "video.channels": 3,
                                     "has_audio": False, "is_depth_map": False})
                if video_key in features and features[video_key] != feature:
                    raise ValueError("相机尺寸在不同 episode 间不一致")
                features[video_key] = feature
                path = output / "videos" / video_key / ("chunk-%03d" % chunk) / ("file-%03d.mp4" % file_index)
                path.parent.mkdir(parents=True, exist_ok=True)
                samples = []
                with (folder / "encoder.log").open("w") as log:
                    proc = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
                                             "-pixel_format", "rgb24", "-video_size", "%dx%d" % (width, height),
                                             "-framerate", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264",
                                             "-threads", "2", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                                             "-movflags", "+faststart", str(path)], stdin=subprocess.PIPE, stderr=log)
                    try:
                        for frame in range(n):
                            image = first if frame == 0 else read_rgb(h5, cam, frame)
                            if image.shape != first.shape:
                                raise ValueError("相机帧尺寸不一致")
                            proc.stdin.write(image.tobytes())
                            sample = np.asarray(Image.fromarray(image).resize((32, 24)), dtype=np.float32)
                            samples.append(sample.reshape(-1, 3) / 255.0)
                        proc.stdin.close()
                        if proc.wait():
                            raise ValueError("视频编码失败：" + (folder / "encoder.log").read_text()[-500:])
                    finally:
                        if proc.poll() is None:
                            proc.kill()
                        proc.wait()
                stats[video_key] = moments(np.concatenate(samples), image=True)
                for field, value in (("chunk_index", chunk), ("file_index", file_index),
                                     ("from_timestamp", 0.0), ("to_timestamp", n / fps)):
                    row["videos/%s/%s" % (video_key, field)] = value
            columns = {"observation.state": np.concatenate(arrays[:3], 1), "action": arrays[3],
                       "timestamp": np.arange(n, dtype=np.float32) / fps,
                       "frame_index": np.arange(n, dtype=np.int64), "index": np.arange(offset, offset+n, dtype=np.int64),
                       "episode_index": np.full(n, index, dtype=np.int64), "task_index": np.zeros(n, dtype=np.int64)}
            arrow, hf = {}, {}
            for key, values in columns.items():
                dtype = features[key]["dtype"]
                if values.ndim == 2:
                    arrow[key] = pa.FixedSizeListArray.from_arrays(pa.array(values.reshape(-1)), values.shape[1])
                    hf[key] = dict(_type="List", length=values.shape[1], feature=dict(_type="Value", dtype=dtype))
                else:
                    arrow[key] = pa.array(values)
                    hf[key] = dict(_type="Value", dtype=dtype)
                stats[key] = moments(values.reshape(n, -1))
            table = pa.table(arrow).replace_schema_metadata({b"huggingface": json.dumps({"info": {"features": hf}}).encode()})
            path = output / "data" / ("chunk-%03d" % chunk) / ("file-%03d.parquet" % file_index)
            path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(table, path)
            for key, values in stats.items():
                for field, value in values.items():
                    row["stats/%s/%s" % (key, field)] = value
            after = os.stat(source["path"])
            if (after.st_size, after.st_mtime_ns) != (source["size"], source["mtime_ns"]):
                raise ValueError("转换期间源文件发生变化：" + job["current"])
            reports.append(dict(source=Path(source["path"]).name, source_episode_number=source["episode"],
                                output_episode_index=index, input_frames=n, output_frames=n,
                                camera_mapping={c["key"]: c["ds"] for c in sch["cams"]}))
            episodes.append(row)
            all_stats.append(stats)
            offset += n
            job.update(done=index+1)
            write_json(folder / "job.json", job)
    meta = output / "meta"
    (meta / "episodes" / "chunk-000").mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(episodes), meta / "episodes" / "chunk-000" / "file-000.parquet")
    tasks = pd.DataFrame({"task_index": [0]}, index=pd.Index([spec["task"]], name="task"))
    pq.write_table(pa.Table.from_pandas(tasks), meta / "tasks.parquet")
    write_json(meta / "info.json", dict(codebase_version="v3.0", robot_type="wheel_leg_mobile_manipulator",
               fps=fps, total_episodes=len(episodes), total_frames=offset, total_tasks=1, chunks_size=1000,
               data_files_size_in_mb=100, video_files_size_in_mb=200, features=features, splits={"train": "0:%d" % len(episodes)},
               data_path="data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
               video_path="videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4"))
    write_json(meta / "stats.json", combine_stats(all_stats))
    write_json(output / "conversion_manifest.json", dict(format="LeRobot v3", filter="good", task=spec["task"],
               fps=fps, include_depth=False, state_names=STATE, action_names=ARM, episodes=reports))
    job.update(state="packaging", current="打包下载文件")
    write_json(folder / "job.json", job)
    with tarfile.open(folder / "dataset.tar.gz.partial", "w:gz", compresslevel=1) as archive:
        archive.add(output, arcname="dataset")
    output.rename(folder / "dataset")
    (folder / "dataset.tar.gz.partial").rename(folder / "dataset.tar.gz")
    job.update(state="done", current="", output_path=str(folder / "dataset"),
               archive_bytes=(folder / "dataset.tar.gz").stat().st_size)
    write_json(folder / "job.json", job)


if __name__ == "__main__":
    folder = Path(sys.argv[1])
    try:
        convert(folder)
    except Exception as e:
        job = json.loads((folder / "job.json").read_text())
        job.update(state="error", error=str(e))
        write_json(folder / "job.json", job)
        shutil.rmtree(folder / "dataset.partial", ignore_errors=True)
        (folder / "dataset.tar.gz.partial").unlink(missing_ok=True)
        raise

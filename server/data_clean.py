# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""上传入库前清理采集 episode 的首帧黑屏与内部冻结帧。"""

import io
import os
from pathlib import Path
import re
import shutil
import tempfile

import h5py
import numpy as np
from PIL import Image


EPISODE_RE = re.compile(r"^episode_(\d+)_vla_valid\.hdf5$")
QPOS_PATH = "observations/qpos"
CAMERAS = (
    "observations/d435i_rgb", "observations/rgb", "observations/third_rgb",
    "observations/wrist_rgb", "observations/head_rgb", "observations/third_person_rgb",
    "observations/images/cam_d435", "observations/images/cam_d405_left",
    "observations/images/cam_d405_right",
)
MIN_STALL_FRAMES = 8
QPOS_STEP_THRESHOLD = 0.01
QPOS_RANGE_THRESHOLD = 0.035
IMAGE_MAD_THRESHOLD = 4.0
EDGE_GUARD_FRAMES = 15
IMAGE_SIZE = 64


def _decode_gray(ds, lengths, frame):
    row = np.asarray(ds[frame], dtype=np.uint8).reshape(-1)
    length = int(lengths[frame]) if lengths is not None else row.size
    if length <= 0 or length > row.size:
        raise ValueError("invalid JPEG frame length")
    with Image.open(io.BytesIO(row[:length].tobytes())) as image:
        image.load()
        resampling = getattr(Image, "Resampling", Image).BILINEAR
        return np.asarray(image.convert("L").resize((IMAGE_SIZE, IMAGE_SIZE), resampling),
                          dtype=np.float32)


def _image_streams(h5, frame_count):
    found = []
    for name in CAMERAS:
        if name not in h5:
            continue
        ds = h5[name]
        if ds.ndim not in (1, 2) or ds.shape[0] != frame_count:
            raise ValueError("相机帧数与 qpos 不一致：%s" % name)
        len_name = name + "_len"
        lengths = np.asarray(h5[len_name][...]) if len_name in h5 else None
        if lengths is not None and lengths.shape != (frame_count,):
            raise ValueError("相机帧长度与 qpos 不一致：%s" % len_name)
        found.append((name, ds, lengths))
    return found


def _detect_frame_removals(path):
    with h5py.File(path, "r") as h5:
        if QPOS_PATH not in h5 or "metadata/timestamps_ns" not in h5:
            raise ValueError("episode 缺少 observations/qpos 或 metadata/timestamps_ns")
        qpos_ds = h5[QPOS_PATH]
        if qpos_ds.ndim != 2 or qpos_ds.shape[1] < 7:
            raise ValueError("observations/qpos 形状无效")
        qpos = np.asarray(qpos_ds[:, :7], dtype=np.float64)
        frame_count = qpos.shape[0]
        timestamps = h5["metadata/timestamps_ns"]
        if timestamps.ndim != 1 or timestamps.shape[0] != frame_count:
            raise ValueError("metadata/timestamps_ns 与 qpos 帧数不一致")
        for name in ("observations/timestamps", "action/action"):
            if name in h5 and (h5[name].ndim == 0 or h5[name].shape[0] != frame_count):
                raise ValueError("%s 与 qpos 帧数不一致" % name)
        if frame_count < 2 or not np.isfinite(qpos).all():
            raise ValueError("episode 帧数过少或 qpos 包含无效值")
        times = np.asarray(timestamps[...])
        if not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
            raise ValueError("metadata/timestamps_ns 必须有限且严格递增")
        streams = _image_streams(h5, frame_count)
        if not streams:
            raise ValueError("episode 未找到可清洗的 RGB 图像流")

        remove = np.zeros(frame_count, dtype=bool)
        black_first = []
        transitions = []
        for name, ds, lengths in streams:
            first = _decode_gray(ds, lengths, 0)
            if float(np.mean(first)) < 2.0:
                black_first.append(name)
            means = np.empty(frame_count - 1, dtype=np.float32)
            previous = first
            for index in range(1, frame_count):
                current = _decode_gray(ds, lengths, index)
                means[index - 1] = float(np.mean(np.abs(current - previous)))
                previous = current
            transitions.append(means)
        if black_first:
            remove[0] = True

        step = np.max(np.abs(np.diff(qpos, axis=0)), axis=1)
        image_mad = np.max(np.stack(transitions, axis=0), axis=0)
        low = (step <= QPOS_STEP_THRESHOLD) & (image_mad <= IMAGE_MAD_THRESHOLD)
        start = None
        runs = []
        for i, value in enumerate(np.append(low, False)):
            if value and start is None:
                start = i
            elif not value and start is not None:
                runs.append((start, i))
                start = None
        stall_remove = np.zeros(frame_count, dtype=bool)
        for start, end in runs:
            if end - start + 1 < MIN_STALL_FRAMES:
                continue
            if start < EDGE_GUARD_FRAMES or end > frame_count - 1 - EDGE_GUARD_FRAMES:
                continue
            if np.max(np.ptp(qpos[start:end + 1], axis=0)) > QPOS_RANGE_THRESHOLD:
                continue
            stall_remove[start + 1:end - 1] = True
        remove |= stall_remove

    if np.all(remove) or frame_count - int(np.count_nonzero(remove)) < 2:
        raise ValueError("清洗会移除过多帧，拒绝入库")
    return remove, {"first_black_cameras": black_first,
                    "stall_removed_frames": int(np.count_nonzero(stall_remove)),
                    "removed_indices": np.flatnonzero(remove).astype(int).tolist()}


def _attrs(source, target):
    for key, value in source.items():
        target[key] = value


def _dataset_options(ds, shape):
    options = {}
    if ds.chunks and shape:
        chunks = list(ds.chunks)
        chunks[0] = max(1, min(chunks[0], shape[0]))
        options["chunks"] = tuple(chunks)
    if ds.compression is not None:
        options["compression"] = ds.compression
        options["compression_opts"] = ds.compression_opts
    for key in ("shuffle", "fletcher32"):
        if getattr(ds, key):
            options[key] = True
    if ds.scaleoffset is not None:
        options["scaleoffset"] = ds.scaleoffset
    if ds.fillvalue is not None:
        options["fillvalue"] = ds.fillvalue
    return options


def _copy_filtered(source_path, output_path, keep, removed):
    old_frames = int(keep.size)
    indices = np.flatnonzero(keep)
    temporal_paths = []
    with h5py.File(source_path, "r") as source, h5py.File(output_path, "w") as target:
        _attrs(source.attrs, target.attrs)
        object_paths = {h5py.h5o.get_info(source.id).addr: "/"}

        def collect_temporal(name, obj):
            if isinstance(obj, h5py.Dataset) and obj.ndim and obj.shape[0] == old_frames:
                temporal_paths.append(name)

        source.visititems(collect_temporal)

        def clone(src_group, dst_group):
            for name in src_group.keys():
                link = src_group.get(name, getlink=True)
                if isinstance(link, h5py.SoftLink):
                    dst_group[name] = h5py.SoftLink(link.path)
                    continue
                if isinstance(link, h5py.ExternalLink):
                    dst_group[name] = h5py.ExternalLink(link.filename, link.path)
                    continue
                obj = src_group[name]
                addr = h5py.h5o.get_info(obj.id).addr
                if addr in object_paths:
                    dst_group[name] = target[object_paths[addr]]
                    continue
                dst_name = dst_group.name.rstrip("/") + "/" + name
                object_paths[addr] = dst_name
                if isinstance(obj, h5py.Group):
                    child = dst_group.create_group(name)
                    _attrs(obj.attrs, child.attrs)
                    clone(obj, child)
                    continue
                filtered = obj.ndim > 0 and obj.shape[0] == old_frames
                shape = (len(indices), *obj.shape[1:]) if filtered else obj.shape
                dst = dst_group.create_dataset(name, shape=shape, dtype=obj.dtype,
                                               **_dataset_options(obj, shape))
                _attrs(obj.attrs, dst.attrs)
                if filtered:
                    for start in range(0, len(indices), 2048):
                        pick = indices[start:start + 2048]
                        dst[start:start + len(pick)] = obj[pick]
                elif obj.ndim == 0:
                    dst[()] = obj[()]
                else:
                    for start in range(0, obj.shape[0], 2048):
                        dst[start:start + 2048] = obj[start:start + 2048]

        clone(source, target)
        target.attrs["episode_len"] = np.int32(len(indices))
        target.attrs["deepmaster_cleaning"] = "first_black_and_stall_frames"
        target.attrs["deepmaster_removed_frames"] = np.int32(len(removed))
        target.flush()

    with h5py.File(output_path, "r") as result:
        count = int(result[QPOS_PATH].shape[0])
        if count != len(indices) or result["metadata/timestamps_ns"].shape != (count,):
            raise ValueError("清洗后帧数与时间戳数量不一致")
        for name in temporal_paths:
            if result[name].shape[0] != count:
                raise ValueError("清洗后时序长度不一致：%s" % name)
        timestamps = np.asarray(result["metadata/timestamps_ns"][...])
        if not np.isfinite(timestamps).all() or np.any(np.diff(timestamps) <= 0):
            raise ValueError("清洗后时间戳无效")


def clean_episode(path):
    """对单个标准 episode 原子清洗，返回帧数和删除原因。"""
    path = Path(path)
    if not EPISODE_RE.fullmatch(path.name):
        return None
    remove, report = _detect_frame_removals(path)
    keep = ~remove
    report.update({"file": path.name, "old_frames": int(keep.size),
                   "new_frames": int(np.count_nonzero(keep))})
    if not np.any(remove):
        return report
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".cleaning", dir=str(path.parent))
    os.close(fd)
    try:
        _copy_filtered(path, temporary, keep, np.flatnonzero(remove))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return report


def clean_directory(directory):
    """清理已解压目录内所有标准 episode，遇到异常时拒绝继续入库。"""
    reports = []
    for path in sorted(Path(directory).rglob("*")):
        if path.is_file() and path.suffix.lower() in (".h5", ".hdf5"):
            result = clean_episode(path)
            if result is not None:
                reports.append(result)
    return reports

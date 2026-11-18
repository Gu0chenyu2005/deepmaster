#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""采集数据自动质检。"""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import io
import json
import os
import re
import shutil
import sys
import time

try:
    import numpy as np
except ImportError:
    np = None

try:
    import h5py
except ImportError:
    h5py = None

try:
    from PIL import Image
except ImportError:
    Image = None


_EXPLICIT_IMAGE_KEYWORDS = (
    "rgb", "rgb_jpeg", "image", "img", "camera", "cam", "video", "view", "depth", "scene",
)


TIMESTAMP_KEYWORDS = (
    "timestamp", "timestamps", "time_ns", "time_ms", "time_s",
    "frame_timestamp", "frame_timestamp_ns", "timestamp_ns", "timestamps_ns", "ts",
)


ACTION_KEYWORDS = (
    "action", "act", "ctrl", "control", "command", "commanded", "joint",
    "torque", "velocity", "qpos", "qvel", "delta",
)


PRIMARY_ACTION_PATHS = (
    "action/commanded",
    "action/action",
    "vla/action/commanded",
    "action",
    "action/ee_pos_cmd",
)


_NON_TIMESERIES_KEYWORDS = (
    "calibration", "intrinsics", "extrinsics", "distortion",
)


_LEN_SUFFIXES = ("_len", "/len", ".len", "_length", "/length")
_AUX_SUFFIXES = ("_valid", "_mask", "_window_mask", "_dt_ns", "_dt_ms")
DEFAULT_PIXEL_CHUNK_FRAMES = 256
MAX_ARRAY_CHUNK_ELEMENTS = 1_000_000
ACTION_RESAMPLE_STEPS = 50
DEFAULT_MAX_WORKERS = 8


PRIMARY_SCENE_IMAGE_PATHS = (
    "observations/third_rgb",
    "vla/observations/cameras/thirdview/rgb_jpeg",
    "observations/d435i_rgb",
    "vla/observations/cameras/d435i/rgb_jpeg",
    "observations/rgb",
    "vla/observations/cameras/wrist/rgb_jpeg",
)
SCENE_GRID_SIZE = (32, 32)
DEFAULT_MAX_SAME_SCENE_RATIO = 0.50
DEFAULT_SCENE_DIFF_THRESHOLD = 6.0
DEFAULT_MIN_SCENE_FILES = 4


OCCLUSION_GRID_SIZE = (16, 16)
CABLE_GRID_SIZE = (32, 32)
OCCLUSION_SAMPLE_FRAMES = 16
DEFAULT_CLOSE_DEPTH_MM = 150
DEFAULT_CABLE_DEPTH_MM = 220
DEFAULT_MAX_CLOSE_DEPTH_RATIO = 0.035
DEFAULT_MAX_DEPTH_INVALID_RATIO = 0.85
DEFAULT_MAX_RGB_OCCLUSION_RATIO = 0.06
DEFAULT_MAX_RGBD_CABLE_RATIO = 0.02
DEFAULT_RGBD_DARK_THRESH = 28.0
DEFAULT_RGBD_STD_THRESH = 3.5


DEFAULT_ACTION_MIN_RANGE_RATIO = 0.35
DEFAULT_ACTION_PATH_RATIO = 0.78


_VLA_CAMERA_MIRROR_MAP = {
    "vla/observations/cameras/wrist/rgb_jpeg": "observations/rgb",
    "vla/observations/cameras/thirdview/rgb_jpeg": "observations/third_rgb",
    "vla/observations/cameras/d435i/rgb_jpeg": "observations/d435i_rgb",
    "vla/observations/cameras/wrist/depth_raw_uint16_mm": "observations/depth",
    "vla/observations/cameras/thirdview/depth_raw_uint16_mm": "observations/third_depth",
    "vla/observations/cameras/d435i/depth_raw_uint16_mm": "observations/d435i_depth",
}


RGBD_STREAM_PAIRS = (
    ("observations/rgb", "observations/depth"),
    ("observations/third_rgb", "observations/third_depth"),
    ("observations/d435i_rgb", "observations/d435i_depth"),
    ("image", "depth"),
)


_RESERVED_OUTPUT_DIR_NAMES = frozenset({"passed", "pending", "json", "valid", "合格", "待复审"})
_RESERVED_OUTPUT_SUFFIXES = ("_passed", "_pending")
def check_deps():
    """检查运行依赖。"""
    missing = [
        name for name, mod in (("numpy", np), ("h5py", h5py), ("PIL", Image))
        if mod is None
    ]
    if missing:
        sys.exit("缺少依赖: %s" % ", ".join(missing))


def get_datasets(group):
    """递归收集 HDF5 数据集。"""
    datasets = {}

    def visitor(name, obj):
        if isinstance(obj, h5py.Dataset):
            datasets[name] = obj

    group.visititems(visitor)
    return datasets


def _match_keywords(name, keywords):
    """按词元匹配名称关键字。"""
    lower = name.lower()
    tokens = set(filter(None, re.split(r"[^a-z]+", lower)))
    for key in keywords:
        if "_" in key or len(key) >= 5:
            if key in lower:
                return True
        elif key in tokens or (key + "s") in tokens:
            return True
    return False


def _attr_is_false(attrs, key):
    """判定属性是否显式为 False/0。"""
    if not attrs or key not in attrs:
        return False
    val = attrs.get(key)
    if isinstance(val, (bool, np.bool_)):
        return not bool(val)
    if isinstance(val, (int, float, np.number)):
        return float(val) == 0.0
    if isinstance(val, (bytes, bytearray)):
        if val in (b"FALSE", b"false", b"0"):
            return True
        return all(b == 0 for b in val)
    if isinstance(val, str):
        return val.strip().lower() in ("false", "0", "no", "none", "missing", "missing_or_incomplete")
    return False


def _is_non_timeseries_dataset(name, ds):
    """判定是否为标量、标定矩阵等非时序静态数据。"""
    if len(ds.shape) == 0:
        return True
    lower = name.lower()
    if lower.startswith("calibration/") or "/calibration/" in lower:
        return True
    if _match_keywords(lower, _NON_TIMESERIES_KEYWORDS):
        return True
    leaf = lower.rsplit("/", 1)[-1]
    if leaf in ("k", "t_camera_base", "distortion", "success") and ds.shape[0] <= 8:
        return True
    return False


def looks_like_image_shape(shape):
    """判定形状是否符合图像张量。"""
    if len(shape) < 3:
        return False
    if len(shape) == 3:
        return min(shape[1], shape[2]) >= 16
    return min(shape[1], shape[2]) >= 16 or min(shape[2], shape[3]) >= 16


def is_image_dataset(name, ds):
    """判定是否为相机图像流。"""
    lower = name.lower()
    if lower.endswith(_LEN_SUFFIXES) or lower.endswith(_AUX_SUFFIXES):
        return False
    if "window" in lower or "foot_contact" in lower or _is_non_timeseries_dataset(name, ds):
        return False
    if looks_like_image_shape(ds.shape):
        return True
    if len(ds.shape) >= 3 and ds.shape[0] == 0:
        return True
    if len(ds.shape) in (1, 2) and _match_keywords(lower, _EXPLICIT_IMAGE_KEYWORDS):
        is_empty = ds.shape[0] == 0
        is_byte_stream = ds.dtype == np.uint8 or ds.dtype.kind in ("S", "O", "V")
        return is_empty or is_byte_stream
    return False


def is_depth_dataset(name, ds, all_datasets=None):
    """判定是否为待检深度图数据流（跳过 vla/ 硬链接副本）。"""
    lower = name.lower()
    if lower.endswith(_LEN_SUFFIXES) or lower.endswith(_AUX_SUFFIXES):
        return False
    if "depth" not in lower or "sync_diag" in lower or "window" in lower:
        return False
    if _is_non_timeseries_dataset(name, ds):
        return False
    attrs = getattr(ds, "attrs", None)
    if _attr_is_false(attrs, "available") or _attr_is_false(attrs, "valid"):
        return False
    if len(ds.shape) < 2 or ds.shape[0] < 2:
        return False
    if not (ds.dtype == np.uint16 or np.issubdtype(ds.dtype, np.floating)):
        return False
    if int(np.prod(ds.shape[1:])) < 256:
        return False

    if all_datasets is not None and lower.startswith("vla/"):
        if any(not k.lower().startswith("vla/") and "depth" in k.lower() for k in all_datasets):
            return False
    return True


def is_timestamp_dataset(name, ds, all_datasets=None):
    """判定是否为待检一维时间戳序列（跳过同组派生副本）。"""
    lower = name.lower()
    if "window" in lower or lower.endswith(("_dt_ns", "_dt_ms", "_dt_s", "_valid", "_mask")):
        return False
    if _is_non_timeseries_dataset(name, ds):
        return False
    if len(ds.shape) == 0 or (len(ds.shape) >= 2 and min(ds.shape) > 1):
        return False
    if not _match_keywords(lower, TIMESTAMP_KEYWORDS):
        return False

    if all_datasets is not None:
        parent = name.rsplit("/", 1)[0] if "/" in name else ""
        leaf = lower.rsplit("/", 1)[-1]
        canonical_ns = f"{parent}/timestamp_ns" if parent else "timestamp_ns"
        canonical_s_ns = f"{parent}/timestamps_ns" if parent else "timestamps_ns"
        if leaf.endswith("_standardized") and (
            canonical_ns in all_datasets
            or canonical_s_ns in all_datasets
            or name[:-len("_standardized")] in all_datasets
        ):
            return False
        if leaf.startswith(("arrival_timestamp", "source_timestamp")) and (
            canonical_ns in all_datasets or canonical_s_ns in all_datasets
        ):
            return False
        if leaf == "timestamps" and canonical_s_ns in all_datasets:
            return False

    return True


def is_action_dataset(name, ds):
    """判定是否为控制动作数据集（排除状态观测、对齐窗口与单位方向参考向量）。"""
    lower = name.lower()
    if len(ds.shape) == 0 or _is_non_timeseries_dataset(name, ds):
        return False
    if lower.startswith(("observations/", "observation/", "aligned/", "vla/observations/", "vla/aligned/", "metadata/")):
        return False
    if lower.endswith("_direction") or "/normal_direction" in lower or "/tangent_direction" in lower:
        return False
    return _match_keywords(lower, ACTION_KEYWORDS)


def count_max_run(mask):
    """计算布尔序列最长连续 True 长度。"""
    arr = np.asarray(mask, dtype=bool).ravel()
    if arr.size == 0 or not np.any(arr):
        return 0
    padded = np.concatenate(([False], arr, [False]))
    changes = np.flatnonzero(padded[1:] != padded[:-1])
    return int(np.max(changes[1::2] - changes[0::2]))


def find_companion_length(name, all_datasets):
    """查找压缩图像流配套的帧长度数据集。"""
    for suffix in ("_len", "_length", "/len", "/length"):
        candidate = name + suffix
        if candidate in all_datasets and len(all_datasets[candidate].shape) == 1:
            return all_datasets[candidate]
    return None


def _overexposure_threshold(dtype):
    """返回过曝均值阈值。"""
    if np.issubdtype(dtype, np.floating):
        return 0.98
    return 253.0


def _iter_dataset_chunks(ds, max_elements=MAX_ARRAY_CHUNK_ELEMENTS):
    """按首维分块迭代数据集。"""
    if ds.ndim == 0 or ds.size <= max_elements or ds.shape[0] == 0:
        yield np.asarray(ds[()])
        return

    elems_per_frame = max(1, int(ds.size // ds.shape[0]))
    step = max(1, max_elements // elems_per_frame)
    total = ds.shape[0]
    for start in range(0, total, step):
        yield np.asarray(ds[start:min(start + step, total)])


def _check_float_dataset_validity(name, ds, all_datasets):
    """校验浮点数据集 NaN/Inf（结合有效掩码与预留字段规则）。"""
    lower = name.lower()
    attrs = getattr(ds, "attrs", None)
    if _attr_is_false(attrs, "available") or _attr_is_false(attrs, "valid"):
        return False

    if len(ds.shape) == 0:
        arr = np.asarray(ds[()])
        return bool(not np.all(np.isfinite(arr)))

    mask_name = name + "_mask"
    if mask_name in all_datasets:
        try:
            arr = np.asarray(ds[:])
            mask = np.asarray(all_datasets[mask_name][:]) > 0
            if np.any(np.isinf(arr)):
                return True
            if mask.shape == arr.shape[:mask.ndim]:
                if not np.any(mask):
                    return bool(not np.any(np.isfinite(arr)))
                return bool(not np.all(np.isfinite(arr[mask])))
            return False
        except (ValueError, TypeError):
            return False

    valid_name = name + "_valid"
    if valid_name in all_datasets:
        try:
            arr = np.asarray(ds[:])
            valid = np.asarray(all_datasets[valid_name][:]) > 0
            if np.any(np.isinf(arr)):
                return True
            if valid.shape == arr.shape[:valid.ndim]:
                if not np.any(valid):
                    return bool(not np.any(np.isfinite(arr)))
                return bool(not np.all(np.isfinite(arr[valid])))
            return False
        except (ValueError, TypeError):
            return False

    if any(k in lower for k in ("sync_diag", "reward", "leg_joint")) or lower.endswith("/executed") or lower == "executed":
        for chunk in _iter_dataset_chunks(ds):
            if np.any(np.isinf(chunk)):
                return True
        if lower.endswith("/executed") or lower == "executed":
            arr = np.asarray(ds[:])
            if not np.any(np.isfinite(arr)):
                return True
        return False

    for chunk in _iter_dataset_chunks(ds):
        if not np.all(np.isfinite(chunk)):
            return True
    return False


def _is_all_zero(ds):
    """判定数据集有效值是否全为 0。"""
    has_nonzero = False
    for chunk in _iter_dataset_chunks(ds):
        if np.issubdtype(chunk.dtype, np.floating):
            finite = chunk[np.isfinite(chunk)]
            if finite.size > 0 and np.any(finite != 0):
                has_nonzero = True
                break
        else:
            if np.any(chunk != 0):
                has_nonzero = True
                break
    return not has_nonzero


def _infer_timestamp_scale_to_ms(ts, med_diff, name="", ds=None):
    """推断时间戳转毫秒(ms)的换算系数。"""
    lower = (name or "").lower()
    attrs = getattr(ds, "attrs", None) or {}
    unit_attr = attrs.get("unit", "")
    if isinstance(unit_attr, (bytes, bytearray)):
        unit_attr = unit_attr.decode("utf-8", errors="ignore")
    unit_str = str(unit_attr).strip().lower()

    if lower.endswith(("_ns", "_ns_standardized")) or "_ns/" in lower or unit_str in ("ns", "nanosecond", "nanoseconds"):
        return 1e-6
    if lower.endswith(("_us", "_us_standardized")) or unit_str in ("us", "microsecond", "microseconds"):
        return 1e-3
    if lower.endswith(("_ms", "_ms_standardized")) or unit_str in ("ms", "millisecond", "milliseconds"):
        return 1.0
    if lower.endswith(("_s", "_sec", "_secs")) or unit_str in ("s", "sec", "second", "seconds"):
        return 1000.0

    abs_med = float(np.median(np.abs(ts)))
    if abs_med > 1e16:
        return 1e-6
    if abs_med > 1e13:
        return 1e-3
    if abs_med > 1e10:
        return 1.0

    if med_diff > 1e5:
        return 1e-6
    if med_diff > 500.0:
        return 1e-3
    if med_diff < 5.0:
        return 1000.0
    return 1.0


def check_timestamps(ds, rules, name=""):
    """校验时间戳有限性、严格单调递增与丢帧间隔。"""
    issues = []
    if len(ds.shape) >= 2 and min(ds.shape) > 1:
        return issues

    try:
        raw_ts = np.asarray(ds[:], dtype=np.float64).ravel()
    except (ValueError, TypeError):
        return issues

    if raw_ts.size < 2:
        return issues

    finite_mask = np.isfinite(raw_ts)
    if not np.all(finite_mask):
        bad_nan = int(np.sum(~finite_mask))
        issues.append(("FAIL", "时间戳含非有限值 (异常点: %d)" % bad_nan))

    ts = raw_ts[finite_mask]
    if ts.size < 2:
        return issues

    diff = np.diff(ts)
    if (diff <= 0).any():
        bad = int((diff <= 0).sum())
        issues.append(("FAIL", "时间戳非严格递增 (异常点: %d)" % bad))

    med = float(np.median(diff))
    if med <= 0:
        return issues

    scale = _infer_timestamp_scale_to_ms(ts, med, name=name, ds=ds)
    dt_ms = diff * scale
    dt_med_ms = med * scale
    max_gap_ms = float(np.max(dt_ms))

    gap_fail = rules.get("max_gap_ms", 1000.0)
    if max_gap_ms >= gap_fail and max_gap_ms >= 3.0 * dt_med_ms:
        issues.append(
            ("FAIL", "严重丢帧/跳帧: 最大间隔 %.1fms (中位间隔 %.1fms)" % (max_gap_ms, dt_med_ms))
        )

    return issues


def _append_stream_quality_issues(name, issues, max_white, rules):
    """汇总图像过曝异常。"""
    white_run_limit = rules.get("white_run_limit", 15)

    if max_white >= white_run_limit:
        issues.append(("FAIL", "图像严重过曝: %s (连续 %d 帧)" % (name, max_white)))


def _resize_gray_to_grid(gray, grid_size=OCCLUSION_GRID_SIZE):
    """将 2D 灰度图降采样为指定网格尺寸 float32。"""
    resample = getattr(getattr(Image, "Resampling", Image), "BILINEAR")
    with Image.fromarray(gray, mode="L") as img:
        return np.asarray(img.resize(grid_size, resample), dtype=np.float32)


def _resize_mask_to_grid(mask_2d, grid_size=CABLE_GRID_SIZE):
    """将 2D 布尔/浮点掩码降采样为指定网格尺寸的均值比例矩阵 (0.0-1.0)。"""
    h, w = mask_2d.shape[:2]
    gh, gw = grid_size
    if h >= gh and w >= gw and h % gh == 0 and w % gw == 0:
        return mask_2d.astype(np.float32).reshape(gh, h // gh, gw, w // gw).mean(axis=(1, 3))
    resample = getattr(getattr(Image, "Resampling", Image), "BILINEAR")
    u8 = np.clip(mask_2d.astype(np.float32) * 255.0, 0.0, 255.0).astype(np.uint8)
    with Image.fromarray(u8, mode="L") as img:
        return np.asarray(img.resize((gw, gh), resample), dtype=np.float32) / 255.0


def _infer_2d_depth_shape(flat_len):
    """将扁平化深度数组长度还原为 2D (H, W) 尺寸。"""
    known = {
        307200: (480, 640),
        921600: (720, 1280),
        76800: (240, 320),
        19200: (120, 160),
        4096: (64, 64),
        1024: (32, 32),
    }
    if flat_len in known:
        return known[flat_len]
    side = int(np.sqrt(flat_len))
    if side * side == flat_len:
        return (side, side)
    for h in (480, 360, 240, 180, 120, 64, 32, 16):
        if flat_len % h == 0 and flat_len // h >= 16:
            return (h, flat_len // h)
    return None


def _check_rgb_static_occlusion(name, grids, issues, rules):
    """校验移动视角中是否存在随镜头静止的常驻暗区或相对暗条带（线缆/异物遮挡）。"""
    if not rules.get("check_occlusion", True) or len(grids) < 8:
        return

    arr = np.stack(grids, axis=0)
    b_mean = np.mean(arr, axis=0)
    b_std = np.std(arr, axis=0)
    b_max = np.max(arr, axis=0)

    active_ratio = float(np.mean(b_std >= 3.5))
    med_bright = float(np.median(b_mean))
    med_std = float(np.median(b_std))
    if active_ratio < 0.35 or med_bright < 45.0:
        return

    dead_dark_mask = (b_mean < 14.0) & (b_std < 1.8) & (b_max < 20.0)
    if active_ratio >= 0.55 and med_std >= 5.0:
        rel_dark_mask = (
            (b_mean < min(40.0, 0.36 * med_bright))
            & (b_std < max(2.5, 0.20 * med_std))
            & (b_max < min(52.0, 0.48 * med_bright))
        )
        occ_mask = dead_dark_mask | rel_dark_mask
    else:
        occ_mask = dead_dark_mask

    dead_dark_ratio = float(np.mean(occ_mask))
    max_occ_ratio = float(rules.get("max_rgb_occlusion_ratio", DEFAULT_MAX_RGB_OCCLUSION_RATIO))
    if dead_dark_ratio >= max_occ_ratio:
        issues.append((
            "FAIL",
            "相机遮挡异常: %s 存在常驻暗区 (占比 %.0f%% > 阈值 %.0f%%)"
            % (name, dead_dark_ratio * 100, max_occ_ratio * 100),
        ))


def check_depth_occlusion(name, ds, rules):
    """校验深度图是否存在持续贴脸异物 (<150mm)、局部常驻近距条带或大面积盲区。"""
    issues = []
    rules = rules or {}
    if not rules.get("check_occlusion", True):
        return issues

    n = ds.shape[0]
    if n < 2:
        return issues

    sample_indices = np.unique(np.linspace(0, n - 1, min(n, OCCLUSION_SAMPLE_FRAMES), dtype=int))
    close_depth_mm = float(rules.get("close_depth_mm", DEFAULT_CLOSE_DEPTH_MM))
    max_close_ratio = float(rules.get("max_close_depth_ratio", DEFAULT_MAX_CLOSE_DEPTH_RATIO))
    max_invalid_ratio = float(rules.get("max_depth_invalid_ratio", DEFAULT_MAX_DEPTH_INVALID_RATIO))

    close_ratios = []
    blocked_ratios = []
    close_grids = []
    try:
        for idx in sample_indices:
            frame = np.asarray(ds[int(idx)])
            if frame.size == 0:
                continue
            if np.issubdtype(frame.dtype, np.floating):
                finite = np.isfinite(frame)
                mm = np.where(finite, frame * (1000.0 if np.nanmax(frame) <= 20.0 else 1.0), 0.0)
                invalid_mask = (~finite) | (mm <= 0.0) | (mm >= 65535.0)
                close_mask = (~invalid_mask) & (mm < close_depth_mm)
            else:
                invalid_mask = (frame == 0) | (frame >= 65535)
                close_mask = (frame > 0) & (frame < close_depth_mm)

            c_ratio = float(np.mean(close_mask))
            inv_ratio = float(np.mean(invalid_mask))
            close_ratios.append(c_ratio)
            blocked_ratios.append(inv_ratio + c_ratio)

            cm_2d = close_mask
            if cm_2d.ndim == 1:
                s2d = _infer_2d_depth_shape(cm_2d.size)
                if s2d is not None:
                    cm_2d = cm_2d.reshape(s2d)
            elif cm_2d.ndim >= 3:
                cm_2d = cm_2d.reshape(cm_2d.shape[0], -1)
            if cm_2d.ndim == 2:
                close_grids.append(_resize_mask_to_grid(cm_2d, grid_size=OCCLUSION_GRID_SIZE))
    except (OSError, ValueError, TypeError):
        return issues

    if not close_ratios:
        return issues

    close_arr = np.asarray(close_ratios, dtype=np.float64)
    blocked_arr = np.asarray(blocked_ratios, dtype=np.float64)
    persist_close_cell_ratio = (
        float(np.mean(np.mean(np.stack(close_grids, axis=0), axis=0) >= 0.75))
        if len(close_grids) >= 6 else 0.0
    )

    if float(np.mean(close_arr >= max_close_ratio)) >= 0.70 or persist_close_cell_ratio >= max_close_ratio:
        med_close = max(float(np.median(close_arr)), persist_close_cell_ratio)
        issues.append((
            "FAIL",
            "相机遮挡异常: %s <%.0fmm 贴脸像素持续占比 %.1f%% (阈值 %.1f%%)"
            % (name, close_depth_mm, med_close * 100, max_close_ratio * 100),
        ))
    elif float(np.mean(blocked_arr >= max_invalid_ratio)) >= 0.70:
        med_blocked = float(np.median(blocked_arr))
        issues.append((
            "FAIL",
            "相机遮挡异常: %s 深度盲区/阻挡像素持续占比 %.0f%% (阈值 %.0f%%)"
            % (name, med_blocked * 100, max_invalid_ratio * 100),
        ))

    return issues


def _decode_rgb_frame_at(ds, len_ds, idx):
    """读取并解码指定帧索引的 2D 灰度图 (uint8)。"""
    if len_ds is not None or (ds.ndim in (1, 2) and (ds.dtype == np.uint8 or ds.dtype.kind in ("S", "O", "V"))):
        if len_ds is not None:
            val = float(np.asarray(len_ds[idx]).ravel()[0])
            if not np.isfinite(val) or val <= 0:
                return None
            length = int(val)
            if ds.ndim == 2 and length > ds.shape[1]:
                return None
        else:
            length = ds.shape[1] if ds.ndim == 2 else None
        row = ds[idx, :length] if ds.ndim == 2 else ds[idx]
        payload = row if isinstance(row, bytes) else np.asarray(row).tobytes()
        if not payload or not payload.strip(b"\x00"):
            return None
        with Image.open(io.BytesIO(payload)) as img:
            img.load()
            return np.asarray(img.convert("L"), dtype=np.uint8)
    elif looks_like_image_shape(ds.shape) and (np.issubdtype(ds.dtype, np.floating) or ds.dtype == np.uint8):
        frame = np.asarray(ds[idx], dtype=np.float32)
        if frame.ndim == 3:
            frame = np.mean(frame[:, :, :3], axis=2)
        if np.issubdtype(ds.dtype, np.floating) and np.nanmax(frame) <= 1.5:
            frame = frame * 255.0
        return np.clip(np.nan_to_num(frame, nan=0.0), 0.0, 255.0).astype(np.uint8)
    return None


def check_rgbd_cable_occlusion(datasets, rules):

    issues = []
    rules = rules or {}
    if not rules.get("check_occlusion", True):
        return issues

    cable_depth_mm = float(rules.get("cable_depth_mm", DEFAULT_CABLE_DEPTH_MM))
    close_depth_mm = float(rules.get("close_depth_mm", DEFAULT_CLOSE_DEPTH_MM))
    dark_thresh = float(rules.get("rgbd_dark_thresh", DEFAULT_RGBD_DARK_THRESH))
    std_thresh = float(rules.get("rgbd_std_thresh", DEFAULT_RGBD_STD_THRESH))
    max_cable_ratio = float(rules.get("max_rgbd_cable_ratio", DEFAULT_MAX_RGBD_CABLE_RATIO))

    for rgb_name, depth_name in RGBD_STREAM_PAIRS:
        if rgb_name not in datasets or depth_name not in datasets:
            continue
        rgb_ds = datasets[rgb_name]
        depth_ds = datasets[depth_name]
        if not is_image_dataset(rgb_name, rgb_ds) or not is_depth_dataset(depth_name, depth_ds, all_datasets=datasets):
            continue
        n = min(rgb_ds.shape[0], depth_ds.shape[0])
        if n < 8:
            continue

        len_ds = find_companion_length(rgb_name, datasets)
        indices = np.unique(np.linspace(0, n - 1, min(n, 12), dtype=int))
        rgb_grids = []
        dz_grids = []
        dn_grids = []
        dc_grids = []

        try:
            for idx in indices:
                idx = int(idx)
                gray = _decode_rgb_frame_at(rgb_ds, len_ds, idx)
                if gray is None or min(gray.shape[:2]) < 32:
                    continue
                dfr = np.asarray(depth_ds[idx])
                if dfr.ndim == 1:
                    shape_2d = _infer_2d_depth_shape(dfr.size)
                    if shape_2d is None:
                        continue
                    dfr = dfr.reshape(shape_2d)
                elif dfr.ndim >= 3:
                    dfr = dfr.reshape(dfr.shape[0], -1)

                if np.issubdtype(dfr.dtype, np.floating):
                    finite = np.isfinite(dfr)
                    mm = np.where(finite, dfr * (1000.0 if np.nanmax(dfr) <= 20.0 else 1.0), 0.0)
                    dz_mask = (~finite) | (mm <= 0.0) | (mm >= 65535.0)
                    dn_mask = (~dz_mask) & (mm < cable_depth_mm)
                    dc_mask = (~dz_mask) & (mm < close_depth_mm)
                else:
                    dz_mask = (dfr == 0) | (dfr >= 65535)
                    dn_mask = (dfr > 0) & (dfr < cable_depth_mm)
                    dc_mask = (dfr > 0) & (dfr < close_depth_mm)

                rgb_grids.append(_resize_gray_to_grid(gray, grid_size=CABLE_GRID_SIZE))
                dz_grids.append(_resize_mask_to_grid(dz_mask, grid_size=CABLE_GRID_SIZE))
                dn_grids.append(_resize_mask_to_grid(dn_mask, grid_size=CABLE_GRID_SIZE))
                dc_grids.append(_resize_mask_to_grid(dc_mask, grid_size=CABLE_GRID_SIZE))
        except Exception:
            continue

        if len(rgb_grids) < 6:
            continue

        b_mean = np.mean(np.stack(rgb_grids, axis=0), axis=0)
        b_std = np.std(np.stack(rgb_grids, axis=0), axis=0)
        dz_mean = np.mean(np.stack(dz_grids, axis=0), axis=0)
        dn_mean = np.mean(np.stack(dn_grids, axis=0), axis=0)
        dc_mean = np.mean(np.stack(dc_grids, axis=0), axis=0)

        med_b = float(np.median(b_mean))
        med_s = float(np.median(b_std))
        if med_b < 35.0:
            continue

        eff_dark = max(dark_thresh, min(48.0, 0.45 * med_b))
        eff_std = max(std_thresh, min(9.0, 0.30 * med_s))
        dark_cable = (b_mean < eff_dark) & (b_std < eff_std) & ((dz_mean > 0.50) | (dn_mean > 0.25))
        near_static_cable = (dc_mean > 0.65) & (b_std < max(5.0, 0.35 * med_s)) if med_s >= 5.0 else np.zeros_like(dark_cable)
        joint_mask = dark_cable | near_static_cable
        cable_ratio = float(np.mean(joint_mask))
        if cable_ratio >= max_cable_ratio:
            issues.append((
                "FAIL",
                "相机遮挡异常: %s 检出近距数据线/异物遮挡 (RGB-D常驻暗区与深度盲区重合占比 %.1f%% > 阈值 %.1f%%)"
                % (rgb_name, cable_ratio * 100, max_cable_ratio * 100),
            ))

    return issues


def _check_compressed_stream(name, ds, len_ds, issues, rules):
    """校验压缩图像流（空帧、损坏、过曝与常驻暗区遮挡）。"""
    n = ds.shape[0]
    lens = None
    if len_ds is not None:
        try:
            lens = np.asarray(len_ds[:], dtype=np.float64).ravel()
        except (TypeError, ValueError, OSError):
            issues.append(("FAIL", "图像帧长度不可读: %s" % name))
            return issues
        if lens.size != n:
            issues.append(("FAIL", "图像帧长度数量不匹配: %s (%d/%d)" % (name, lens.size, n)))

    min_jpeg_bytes = int(rules.get("min_jpeg_bytes", 1000))
    short_frame_limit = 0
    if lens is not None:
        positive = lens[np.isfinite(lens) & (lens > 0)]
        if positive.size and np.median(positive) >= 2 * min_jpeg_bytes:
            short_frame_limit = min_jpeg_bytes

    white_flags = np.zeros(n, dtype=bool)
    empty_frames, corrupt_frames = [], []

    sample_set = set(np.linspace(0, n - 1, min(n, OCCLUSION_SAMPLE_FRAMES), dtype=int).tolist()) if n >= 10 else set()
    occ_grids = []

    for i in range(n):
        if lens is not None:
            if i >= lens.size:
                empty_frames.append(i)
                continue
            value = lens[i]
            if not np.isfinite(value) or value <= 0 or value < short_frame_limit:
                empty_frames.append(i)
                continue
            if value != int(value) or (ds.ndim == 2 and value > ds.shape[1]):
                corrupt_frames.append(i)
                continue
            length = int(value)
        else:
            length = ds.shape[1] if ds.ndim == 2 else None

        try:
            row = ds[i, :length] if ds.ndim == 2 else ds[i]
            payload = row if isinstance(row, bytes) else np.asarray(row).tobytes()
            if not payload or not payload.strip(b'\x00'):
                empty_frames.append(i)
                prev_gray = None
                continue
            with Image.open(io.BytesIO(payload)) as img:
                img.load()
                gray = np.asarray(img.convert("L"), dtype=np.uint8)
        except (OSError, ValueError, TypeError):
            corrupt_frames.append(i)
            continue

        mean_value = float(np.mean(gray, dtype=np.float64))
        white_flags[i] = mean_value > _overexposure_threshold(np.uint8)

        if i in sample_set and min(gray.shape[:2]) >= 32:
            occ_grids.append(_resize_gray_to_grid(gray))


    max_empty_frames = int(rules.get("max_empty_frames", 0))
    allow_first_empty = bool(rules.get("allow_first_empty_frame", True))
    if empty_frames:
        if allow_first_empty and n >= 30 and empty_frames == [0]:
            issues.append(("WARN", "首帧图像未就绪(启动同步延迟): %s [0]" % name))
        elif len(empty_frames) > max_empty_frames:
            issues.append(("FAIL", "图像存在空帧: %s 共 %d 帧 (如 %s)" %
                           (name, len(empty_frames), empty_frames[:10])))
    if corrupt_frames:
        issues.append(("FAIL", "图像解码失败或长度异常: %s 共 %d 帧 (如 %s)" %
                       (name, len(corrupt_frames), corrupt_frames[:10])))

    max_white = count_max_run(white_flags)
    _append_stream_quality_issues(name, issues, max_white, rules)
    _check_rgb_static_occlusion(name, occ_grids, issues, rules)
    return issues


def _check_pixel_stream(name, ds, issues, rules):
    """分块校验原始像素流（过曝与常驻暗区遮挡）。"""
    if not looks_like_image_shape(ds.shape):
        return issues

    is_visual = np.issubdtype(ds.dtype, np.floating) or ds.dtype == np.uint8
    if not is_visual:
        return issues

    total = ds.shape[0]
    if total == 0 or ds.size == 0:
        return issues

    high_thresh = _overexposure_threshold(ds.dtype)
    chunk_frames = max(1, int(rules.get("chunk_frames", DEFAULT_PIXEL_CHUNK_FRAMES)))

    white_chunks = []
    sample_set = set(np.linspace(0, total - 1, min(total, OCCLUSION_SAMPLE_FRAMES), dtype=int).tolist()) if total >= 10 else set()
    occ_grids = []

    try:
        for start in range(0, total, chunk_frames):
            end = min(start + chunk_frames, total)
            chunk = np.asarray(ds[start:end])
            if chunk.size == 0:
                return issues

            axes = tuple(range(1, chunk.ndim))
            means = np.mean(chunk, axis=axes, dtype=np.float64) if axes else chunk.astype(np.float64)
            white_chunks.append(means > high_thresh)

            for offset in range(chunk.shape[0]):
                frame_idx = start + offset
                if frame_idx in sample_set and min(chunk.shape[1], chunk.shape[2]) >= 32:
                    frame = chunk[offset]
                    if frame.ndim == 3:
                        frame = np.mean(frame[:, :, :3], axis=2)
                    if np.issubdtype(ds.dtype, np.floating) and np.nanmax(frame) <= 1.5:
                        frame = frame * 255.0
                    gray_u8 = np.clip(np.nan_to_num(frame, nan=0.0), 0.0, 255.0).astype(np.uint8)
                    occ_grids.append(_resize_gray_to_grid(gray_u8))

    except (ValueError, TypeError):
        return issues

    white_mask = np.concatenate(white_chunks) if white_chunks else np.zeros(0, dtype=bool)

    max_white = count_max_run(white_mask)

    _append_stream_quality_issues(name, issues, max_white, rules)
    _check_rgb_static_occlusion(name, occ_grids, issues, rules)
    return issues


def check_image_stream(name, ds, all_datasets, rules):
    """校验相机图像流。"""
    issues = []

    if len(ds.shape) == 0 or ds.shape[0] == 0:
        issues.append(("FAIL", "图像流为空: %s" % name))
        return issues

    len_ds = find_companion_length(name, all_datasets)
    if len_ds is not None or (ds.ndim in (1, 2) and
                           (ds.dtype == np.uint8 or ds.dtype.kind in ("S", "O", "V"))):
        return _check_compressed_stream(name, ds, len_ds, issues, rules)
    return _check_pixel_stream(name, ds, issues, rules)


def _compute_expected_group_lengths(ts_datasets, t_frames, min_frames, root_attrs=None):
    """计算各传感器分组的基准帧数。"""
    root_attrs = root_attrs or {}
    imu_expected = root_attrs.get("imu_sample_count")
    joints_expected = root_attrs.get("raw_joints_sample_count")

    group_lengths = {}
    for name, ds in ts_datasets.items():
        if ds.shape[0] >= 2:
            parent = name.rsplit("/", 1)[0] if "/" in name else ""
            group_lengths.setdefault(parent, []).append(ds.shape[0])

    expected_by_group = {"": t_frames}
    for parent, lens in group_lengths.items():
        if not parent:
            continue
        lower_parent = parent.lower()
        if lower_parent in ("observations/imu", "vla/observations/high_rate/imu") and isinstance(imu_expected, (int, np.integer)) and imu_expected > 0:
            expected_by_group[parent] = int(imu_expected)
            continue
        if lower_parent in ("observations/joints", "vla/observations/high_rate/joints") and isinstance(joints_expected, (int, np.integer)) and joints_expected > 0:
            expected_by_group[parent] = int(joints_expected)
            continue
        if len(lens) >= 2:
            mode_len = max(set(lens), key=lambda x: (lens.count(x), x))
            if lens.count(mode_len) >= 2 and mode_len >= min_frames:
                expected_by_group[parent] = mode_len
                continue
        expected_by_group[parent] = t_frames
    return expected_by_group


def _classify_hdf5_oserror(file_path, error):
    """将 HDF5 读取 OSError 分类为 PENDING (截断待复核) 或 FAIL (文件损坏)。"""
    if "truncated file:" not in str(error).lower():
        return False, [("FAIL", "文件损坏或无法打开: %s" % error)]

    try:
        physical_size = os.path.getsize(file_path)
    except OSError:
        physical_size = None
    match = re.search(r"sblock->base_addr\s*=\s*(\d+).*?stored_eof\s*=\s*(\d+)", str(error))
    if match and physical_size is not None:
        expected_size = int(match.group(1)) + int(match.group(2))
        if physical_size < expected_size:
            detail = "当前大小 %d 字节，HDF5 声明末尾 %d 字节" % (physical_size, expected_size)
        else:
            detail = "当前大小 %d 字节已达 HDF5 声明末尾 %d 字节，仍读取异常" % (
                physical_size, expected_size)
    else:
        detail = "重试打开仍失败: %s" % error
    return None, [("PENDING", "HDF5 打开状态待复核 (%s)" % detail)]


def check_episode(file_path, rules):
    """单文件 HDF5 完整质检。"""
    findings = []
    rules = rules or {}

    base_name = os.path.basename(file_path)
    stem, _ = os.path.splitext(base_name)
    if "_vla_fail" in stem or "_fail" in stem:
        findings.append(("FAIL", "文件名包含失败标记 (_fail)"))
        return False, findings

    try:
        f = h5py.File(file_path, "r")
    except OSError as e:
        if "truncated file:" not in str(e).lower():
            return _classify_hdf5_oserror(file_path, e)
        try:
            f = h5py.File(file_path, "r")
        except OSError as retry_error:
            return _classify_hdf5_oserror(file_path, retry_error)

    try:
        with f:
            datasets = get_datasets(f)
            if not datasets:
                findings.append(("FAIL", "HDF5 文件为空"))
                return False, findings

            ts_datasets = {
                name: ds for name, ds in datasets.items()
                if not _is_non_timeseries_dataset(name, ds) and len(ds.shape) >= 1 and ds.shape[0] > 0
            }
            if not ts_datasets:
                findings.append(("FAIL", "未检出有效时序数据"))
                return False, findings

            root_attrs = getattr(f, "attrs", {}) or {}
            attr_ep_len = root_attrs.get("episode_len")
            lengths = [ds.shape[0] for ds in ts_datasets.values()]
            if isinstance(attr_ep_len, (int, np.integer)) and int(attr_ep_len) in lengths:
                t_frames = int(attr_ep_len)
            else:
                t_frames = max(set(lengths), key=lambda x: (lengths.count(x), x))

            min_frames = rules.get("min_frames", 10)
            if t_frames < min_frames:
                findings.append(("FAIL", "有效帧数不足 (%d < %d)" % (t_frames, min_frames)))

            expected_by_group = _compute_expected_group_lengths(ts_datasets, t_frames, min_frames, root_attrs=root_attrs)
            for name, ds in ts_datasets.items():
                if ds.shape[0] >= 2:
                    parent = name.rsplit("/", 1)[0] if "/" in name else ""
                    expected_len = expected_by_group.get(parent, t_frames)
                    if ds.shape[0] != expected_len:
                        findings.append(
                            ("FAIL", "时序长度不一致: %s (%d 帧, 基准 %d 帧)" % (name, ds.shape[0], expected_len))
                        )

            action_candidates = []
            zero_action_names = []

            for name, ds in datasets.items():
                if np.issubdtype(ds.dtype, np.floating) and ds.size > 0:
                    if not _is_non_timeseries_dataset(name, ds) or len(ds.shape) == 0:
                        if _check_float_dataset_validity(name, ds, datasets):
                            findings.append(("FAIL", "数据集包含 NaN/Inf: %s" % name))

                if is_action_dataset(name, ds) and len(ds.shape) >= 1 and ds.shape[0] > 1:
                    action_candidates.append(name)
                    if _is_all_zero(ds):
                        zero_action_names.append(name)

                if is_timestamp_dataset(name, ds, all_datasets=datasets):
                    findings.extend(check_timestamps(ds, rules, name=name))

                mirror_primary = _VLA_CAMERA_MIRROR_MAP.get(name)
                if mirror_primary and mirror_primary in datasets:
                    continue

                if is_image_dataset(name, ds):
                    findings.extend(check_image_stream(name, ds, datasets, rules))

                if is_depth_dataset(name, ds, all_datasets=datasets):
                    findings.extend(check_depth_occlusion(name, ds, rules))


            findings.extend(check_rgbd_cable_occlusion(datasets, rules))

            primary_cmd_candidates = [
                n for n in action_candidates
                if "force" not in n.lower() and not n.lower().endswith("/executed") and n.lower() != "executed"
            ]
            target_candidates = primary_cmd_candidates if primary_cmd_candidates else action_candidates
            zero_set = set(zero_action_names)
            if target_candidates and all(n in zero_set for n in target_candidates):
                for name in target_candidates:
                    findings.append(("FAIL", "动作数据全为0: %s" % name))
    except OSError as e:
        return _classify_hdf5_oserror(file_path, e)

    findings = list(dict.fromkeys(findings))
    is_pass = not any(level == "FAIL" for level, _ in findings)
    return is_pass, findings


def extract_primary_action(file_path):
    """提取主动作轨迹矩阵 (T, D)，剔除全 NaN 列。"""
    try:
        with h5py.File(file_path, "r") as f:
            datasets = get_datasets(f)
            if not datasets:
                return None

            chosen_ds = None
            for pref in PRIMARY_ACTION_PATHS:
                if pref in datasets and len(datasets[pref].shape) >= 1 and datasets[pref].shape[0] >= 2:
                    if not _is_all_zero(datasets[pref]):
                        chosen_ds = datasets[pref]
                        break

            if chosen_ds is None:
                candidates = []
                for name, ds in sorted(datasets.items()):
                    if is_action_dataset(name, ds) and len(ds.shape) >= 1 and ds.shape[0] >= 2:
                        lower = name.lower()
                        if "force" in lower or lower.endswith("/executed") or lower == "executed":
                            continue
                        if not _is_all_zero(ds):
                            dim = int(np.prod(ds.shape[1:])) if len(ds.shape) > 1 else 1
                            candidates.append((dim, name, ds))
                if candidates:
                    candidates.sort(key=lambda x: (-x[0], x[1]))
                    chosen_ds = candidates[0][2]

            if chosen_ds is None:
                return None

            arr = np.asarray(chosen_ds[:], dtype=np.float64)
            if arr.ndim == 1:
                arr = arr.reshape(-1, 1)
            elif arr.ndim > 2:
                arr = arr.reshape(arr.shape[0], -1)

            valid_cols = np.all(np.isfinite(arr), axis=0)
            if not np.any(valid_cols):
                return None
            return arr[:, valid_cols]
    except Exception:
        return None


def _decode_first_frame_gray(ds, len_ds=None):
    """解码图像流首帧并降采样为 SCENE_GRID_SIZE 灰度网格。"""
    n = ds.shape[0] if len(ds.shape) >= 1 else 0
    if n <= 0:
        return None

    resample = getattr(getattr(Image, "Resampling", Image), "BILINEAR")

    if len_ds is not None or (ds.ndim in (1, 2) and (ds.dtype == np.uint8 or ds.dtype.kind in ("S", "O", "V"))):
        lens = np.asarray(len_ds[:], dtype=np.float64).ravel() if len_ds is not None else None
        for idx in range(min(n, 5)):
            if lens is not None:
                if idx >= lens.size or not np.isfinite(lens[idx]) or lens[idx] <= 0:
                    continue
                length = int(lens[idx])
                if ds.ndim == 2 and length > ds.shape[1]:
                    continue
            else:
                length = ds.shape[1] if ds.ndim == 2 else None
            row = ds[idx, :length] if ds.ndim == 2 else ds[idx]
            payload = row if isinstance(row, bytes) else np.asarray(row).tobytes()
            if not payload or not payload.strip(b"\x00"):
                continue
            with Image.open(io.BytesIO(payload)) as img:
                img.load()
                gray = img.convert("L").resize(SCENE_GRID_SIZE, resample)
                return np.asarray(gray, dtype=np.float32)
        return None

    if looks_like_image_shape(ds.shape) and (np.issubdtype(ds.dtype, np.floating) or ds.dtype == np.uint8):
        frame = np.asarray(ds[0], dtype=np.float32)
        if frame.ndim == 3:
            frame = np.mean(frame[:, :, :3], axis=2)
        if np.issubdtype(ds.dtype, np.floating) and np.nanmax(frame) <= 1.5:
            frame = frame * 255.0
        frame = np.clip(np.nan_to_num(frame, nan=0.0), 0.0, 255.0).astype(np.uint8)
        with Image.fromarray(frame, mode="L") as img:
            gray = img.resize(SCENE_GRID_SIZE, resample)
            return np.asarray(gray, dtype=np.float32)
    return None


def extract_scene_signature(file_path):
    """提取单文件首帧场景特征（32x32 灰度网格与可选底盘位姿）。"""
    try:
        with h5py.File(file_path, "r") as f:
            datasets = get_datasets(f)
            if not datasets:
                return None

            chosen_name = None
            chosen_ds = None
            for pref in PRIMARY_SCENE_IMAGE_PATHS:
                if pref in datasets and is_image_dataset(pref, datasets[pref]):
                    if len(datasets[pref].shape) >= 1 and datasets[pref].shape[0] > 0:
                        chosen_name = pref
                        chosen_ds = datasets[pref]
                        break

            if chosen_ds is None:
                for name, ds in sorted(datasets.items()):
                    if "depth" in name.lower():
                        continue
                    if is_image_dataset(name, ds) and len(ds.shape) >= 1 and ds.shape[0] > 0:
                        chosen_name = name
                        chosen_ds = ds
                        break

            if chosen_ds is None:
                return None

            len_ds = find_companion_length(chosen_name, datasets)
            grid = _decode_first_frame_gray(chosen_ds, len_ds=len_ds)
            if grid is None:
                return None

            pose0 = None
            for pose_key in ("observations/dog_pose", "vla/observations/dog_pose"):
                if pose_key in datasets and len(datasets[pose_key].shape) == 2 and datasets[pose_key].shape[0] > 0:
                    pose_arr = np.asarray(datasets[pose_key][:], dtype=np.float64)
                    if np.all(np.isfinite(pose_arr)) and np.max(np.abs(pose_arr)) > 1e-4:
                        pose0 = pose_arr[0].copy()
                    break

            return {
                "camera": chosen_name,
                "grid": grid,
                "pose": pose0,
            }
    except Exception:
        return None


def _sample_rgb_occlusion_stats(ds, len_ds=None):
    """抽样计算图像流 16x16 区块的时序均值与标准差。"""
    n = ds.shape[0] if len(ds.shape) >= 1 else 0
    if n < 4:
        return None

    indices = np.unique(np.linspace(0, n - 1, min(n, 8), dtype=int))
    grids = []

    if len_ds is not None or (ds.ndim in (1, 2) and (ds.dtype == np.uint8 or ds.dtype.kind in ("S", "O", "V"))):
        lens = np.asarray(len_ds[:], dtype=np.float64).ravel() if len_ds is not None else None
        for idx in indices:
            idx = int(idx)
            if lens is not None:
                if idx >= lens.size or not np.isfinite(lens[idx]) or lens[idx] <= 0:
                    continue
                length = int(lens[idx])
                if ds.ndim == 2 and length > ds.shape[1]:
                    continue
            else:
                length = ds.shape[1] if ds.ndim == 2 else None
            row = ds[idx, :length] if ds.ndim == 2 else ds[idx]
            payload = row if isinstance(row, bytes) else np.asarray(row).tobytes()
            if not payload or not payload.strip(b"\x00"):
                continue
            with Image.open(io.BytesIO(payload)) as img:
                img.load()
                gray = np.asarray(img.convert("L"), dtype=np.uint8)
                if min(gray.shape[:2]) >= 32:
                    grids.append(_resize_gray_to_grid(gray))
    elif looks_like_image_shape(ds.shape) and (np.issubdtype(ds.dtype, np.floating) or ds.dtype == np.uint8):
        if min(ds.shape[1], ds.shape[2]) < 32:
            return None
        for idx in indices:
            frame = np.asarray(ds[int(idx)], dtype=np.float32)
            if frame.ndim == 3:
                frame = np.mean(frame[:, :, :3], axis=2)
            if np.issubdtype(ds.dtype, np.floating) and np.nanmax(frame) <= 1.5:
                frame = frame * 255.0
            gray_u8 = np.clip(np.nan_to_num(frame, nan=0.0), 0.0, 255.0).astype(np.uint8)
            grids.append(_resize_gray_to_grid(gray_u8))

    if len(grids) < 4:
        return None
    arr = np.stack(grids, axis=0)
    return {
        "mean": np.mean(arr, axis=0),
        "std": np.std(arr, axis=0),
    }


def extract_occlusion_signature(file_path, rules=None):
    """提取单文件各路 RGB 与深度图的遮挡统计特征。"""
    rules = rules or {}
    close_depth_mm = float(rules.get("close_depth_mm", DEFAULT_CLOSE_DEPTH_MM))
    try:
        with h5py.File(file_path, "r") as f:
            datasets = get_datasets(f)
            if not datasets:
                return None

            rgb_stats = {}
            depth_stats = {}

            for name, ds in sorted(datasets.items()):
                lower = name.lower()
                if lower.startswith("vla/"):
                    continue
                if is_depth_dataset(name, ds, all_datasets=datasets):
                    n = ds.shape[0]
                    indices = np.unique(np.linspace(0, n - 1, min(n, 8), dtype=int))
                    valid_list = []
                    close_list = []
                    for idx in indices:
                        frame = np.asarray(ds[int(idx)])
                        if frame.size == 0:
                            continue
                        if np.issubdtype(frame.dtype, np.floating):
                            finite = np.isfinite(frame)
                            mm = np.where(finite, frame * (1000.0 if np.nanmax(frame) <= 20.0 else 1.0), 0.0)
                            inv = (~finite) | (mm <= 0.0) | (mm >= 65535.0)
                            close = (~inv) & (mm < close_depth_mm)
                        else:
                            inv = (frame == 0) | (frame >= 65535)
                            close = (frame > 0) & (frame < close_depth_mm)
                        valid_list.append(1.0 - float(np.mean(inv | close)))
                        close_list.append(float(np.mean(close)))
                    if valid_list:
                        depth_stats[name] = {
                            "valid_ratio": float(np.median(valid_list)),
                            "close_ratio": float(np.median(close_list)),
                        }
                elif is_image_dataset(name, ds) and "depth" not in lower:
                    len_ds = find_companion_length(name, datasets)
                    st = _sample_rgb_occlusion_stats(ds, len_ds=len_ds)
                    if st is not None:
                        rgb_stats[name] = st

            if not rgb_stats and not depth_stats:
                return None
            return {"rgb": rgb_stats, "depth": depth_stats}
    except Exception:
        return None


def check_folder_camera_occlusion(folder, file_names, rules=None, preloaded_occlusions=None):
    """同批次跨文件相机遮挡离群校验。"""
    rules = rules or {}
    if not rules.get("check_occlusion", True):
        return {}

    min_cluster_files = int(rules.get("min_cluster_files", 3))
    if len(file_names) < min_cluster_files:
        return {}

    sigs = {}
    for name in file_names:
        stem, _ = os.path.splitext(name)
        if "_vla_fail" in stem or "_fail" in stem:
            continue
        if preloaded_occlusions is not None and name in preloaded_occlusions:
            sig = preloaded_occlusions[name]
        else:
            sig = extract_occlusion_signature(os.path.join(folder, name), rules=rules)
        if sig is not None:
            sigs[name] = sig

    if len(sigs) < min_cluster_files:
        return {}

    anomalies = {}

    depth_streams = set()
    for sig in sigs.values():
        depth_streams.update(sig.get("depth", {}).keys())

    for d_name in sorted(depth_streams):
        names_with_d = [n for n in sigs if d_name in sigs[n].get("depth", {})]
        if len(names_with_d) < min_cluster_files:
            continue
        valid_ratios = np.array([sigs[n]["depth"][d_name]["valid_ratio"] for n in names_with_d], dtype=np.float64)
        close_ratios = np.array([sigs[n]["depth"][d_name]["close_ratio"] for n in names_with_d], dtype=np.float64)
        med_valid = float(np.median(valid_ratios))
        med_close = float(np.median(close_ratios))

        for i, n in enumerate(names_with_d):
            if med_valid >= 0.40 and (med_valid - valid_ratios[i]) >= 0.18 and valid_ratios[i] < 0.65:
                anomalies.setdefault(n, []).append(
                    "相机遮挡异常: %s 有效深度占比 %.0f%% 低于同批基准 %.0f%%"
                    % (d_name, valid_ratios[i] * 100, med_valid * 100)
                )
            elif (close_ratios[i] - med_close) >= 0.018 and close_ratios[i] >= 0.022:
                anomalies.setdefault(n, []).append(
                    "相机遮挡异常: %s 贴脸阻挡占比 %.1f%% 高于同批基准 %.1f%%"
                    % (d_name, close_ratios[i] * 100, med_close * 100)
                )

    rgb_streams = set()
    for sig in sigs.values():
        rgb_streams.update(sig.get("rgb", {}).keys())

    for c_name in sorted(rgb_streams):
        names_with_c = [n for n in sigs if c_name in sigs[n].get("rgb", {})]
        if len(names_with_c) < min_cluster_files:
            continue
        mean_stack = np.stack([sigs[n]["rgb"][c_name]["mean"] for n in names_with_c], axis=0)
        med_grid = np.median(mean_stack, axis=0)

        for i, n in enumerate(names_with_c):
            b_mean = sigs[n]["rgb"][c_name]["mean"]
            b_std = sigs[n]["rgb"][c_name]["std"]
            med_std = float(np.median(b_std))
            active_ratio = float(np.mean(b_std >= 3.5))
            bg_match_ratio = float(np.mean(np.abs(b_mean - med_grid) <= 22.0))

            if bg_match_ratio >= 0.55:
                newly_occluded = (
                    (med_grid >= 50.0)
                    & (b_mean < 38.0)
                    & ((med_grid - b_mean) >= 35.0)
                    & (b_std < max(3.5, 0.28 * med_std))
                )
                occ_thresh = 0.05
            elif active_ratio >= 0.50 and med_std >= 4.5:
                newly_occluded = (
                    (med_grid >= 60.0)
                    & (b_mean < 32.0)
                    & ((med_grid - b_mean) >= 42.0)
                    & (b_std < max(3.0, 0.20 * med_std))
                )
                occ_thresh = 0.05
            else:
                continue

            occ_ratio = float(np.mean(newly_occluded))
            if occ_ratio >= occ_thresh:
                anomalies.setdefault(n, []).append(
                    "相机遮挡异常: %s 局部常驻暗区占比 %.0f%% (偏离同批基准)"
                    % (c_name, occ_ratio * 100)
                )

    return anomalies


def _inspect_episode_worker(task):
    """子进程工作函数：执行单文件质检并预提取跨文件校验特征。"""
    name, file_path, rules = task
    is_pass, file_findings = check_episode(file_path, rules)
    traj = None
    scene_sig = None
    occ_sig = None
    if is_pass is True:
        if rules.get("check_action_consistency", True):
            traj = extract_primary_action(file_path)
        if rules.get("check_scene_redundancy", True):
            scene_sig = extract_scene_signature(file_path)
        if rules.get("check_occlusion", True):
            occ_sig = extract_occlusion_signature(file_path, rules=rules)
    return name, is_pass, file_findings, traj, scene_sig, occ_sig


def check_folder_action_consistency(folder, file_names, rules=None, preloaded_trajs=None):
    """同批次跨文件动作完整性校验（无序途经状态匹配）。"""
    rules = rules or {}
    if not rules.get("check_action_consistency", True):
        return {}

    min_cluster_files = int(rules.get("min_cluster_files", 3))
    if len(file_names) < min_cluster_files:
        return {}

    raw_trajs = {}
    for name in file_names:
        stem, _ = os.path.splitext(name)
        if "_vla_fail" in stem or "_fail" in stem:
            continue
        if preloaded_trajs is not None and name in preloaded_trajs:
            traj = preloaded_trajs[name]
        else:
            traj = extract_primary_action(os.path.join(folder, name))
        if traj is not None and traj.shape[0] >= 2:
            raw_trajs[name] = traj

    if len(raw_trajs) < min_cluster_files:
        return {}

    dim_counts = {}
    for name, tr in raw_trajs.items():
        dim_counts.setdefault(tr.shape[1], []).append(name)
    main_dim, group_names = max(dim_counts.items(), key=lambda kv: len(kv[1]))
    if len(group_names) < min_cluster_files:
        return {}

    anomalies = {}
    for name, tr in raw_trajs.items():
        if tr.shape[1] != main_dim:
            anomalies[name] = [
                "动作指令异常: 疑似动作缺失 (有效维度 %d, 基准 %d)"
                % (tr.shape[1], main_dim)
            ]

    trajs = [raw_trajs[name] for name in group_names]

    ptp_matrix = np.array([np.ptp(tr, axis=0) for tr in trajs], dtype=np.float64)
    med_ptp = np.median(ptp_matrix, axis=0)
    active_cols = np.flatnonzero(med_ptp >= max(0.10, 0.25 * float(np.max(med_ptp))))
    if active_cols.size == 0:
        return anomalies

    d = active_cols.size
    scales = np.maximum(med_ptp[active_cols], 0.05)
    norm_trajs = [tr[:, active_cols] / scales for tr in trajs]

    k_steps = ACTION_RESAMPLE_STEPS
    u_grid = np.linspace(0.0, 1.0, k_steps)
    resampled = np.zeros((len(trajs), k_steps, d), dtype=np.float64)
    for i, ntr in enumerate(norm_trajs):
        t_i = np.linspace(0.0, 1.0, ntr.shape[0])
        for c in range(d):
            resampled[i, :, c] = np.interp(u_grid, t_i, ntr[:, c])

    consensus = np.median(resampled, axis=0)
    if np.linalg.norm(consensus - np.mean(consensus, axis=0, keepdims=True)) <= 1e-6:
        return anomalies

    geo_path_lengths = np.array([
        np.sum(np.linalg.norm(np.diff(r_tr, axis=0), axis=1)) for r_tr in resampled
    ], dtype=np.float64)
    med_geo_path = float(np.median(geo_path_lengths))

    coverage_distances = []
    for r_tr in resampled:
        distances = np.linalg.norm(consensus[:, None, :] - r_tr[None, :, :], axis=2) / np.sqrt(d)
        coverage_distances.append(np.min(distances, axis=1))
    median_max_distance = float(np.median([np.max(dist) for dist in coverage_distances]))
    missing_threshold = max(0.22, 2.0 * median_max_distance)

    min_range_thresh = float(rules.get("action_min_range_ratio", DEFAULT_ACTION_MIN_RANGE_RATIO))
    path_ratio_thresh = float(rules.get("action_path_ratio", DEFAULT_ACTION_PATH_RATIO))

    discrete_mask = np.zeros(d, dtype=bool)
    for idx_c, col_idx in enumerate(active_cols):
        extrema_ratios = []
        for tr in trajs:
            col = tr[:, col_idx]
            c_ptp = float(np.ptp(col))
            if c_ptp <= 1e-6:
                continue
            c_min, c_max = float(np.min(col)), float(np.max(col))
            extrema_ratios.append(float(np.mean((col <= c_min + 0.15 * c_ptp) | (col >= c_max - 0.15 * c_ptp))))
        if extrema_ratios and float(np.median(extrema_ratios)) >= 0.88 and med_ptp[col_idx] >= 0.30:
            discrete_mask[idx_c] = True

    for i, name in enumerate(group_names):
        col_ratios = ptp_matrix[i, active_cols] / med_ptp[active_cols]
        min_range = float(np.min(col_ratios))
        med_range = float(np.median(col_ratios))
        distances = coverage_distances[i]
        missing_fraction = float(np.mean(distances > missing_threshold))
        path_ratio = float(geo_path_lengths[i] / max(med_geo_path, 1e-6))

        discrete_missing = bool(np.any(discrete_mask) and float(np.min(col_ratios[discrete_mask])) < min_range_thresh)

        cont_missing = bool(
            (min_range < min_range_thresh and (path_ratio < path_ratio_thresh or med_range < 0.55))
            or min_range < 0.05
        )
        waypoint_missing = bool(missing_fraction >= 0.20 and path_ratio < path_ratio_thresh)

        if discrete_missing or cont_missing or waypoint_missing:
            anomalies[name] = [
                "动作指令异常: 疑似动作缺失 (行程占比 %.0f%%, 缺失状态 %.0f%%, 路径占比 %.0f%%)"
                % (min_range * 100, missing_fraction * 100, path_ratio * 100)
            ]

    return anomalies


def _is_same_scene_signature(sig_a, sig_b, diff_threshold):
    """基于首帧背景 75% 分位像素差与底盘位姿判定是否为同一静止机位。"""
    pose_a, pose_b = sig_a.get("pose"), sig_b.get("pose")
    if pose_a is not None and pose_b is not None:
        pos_dist = float(np.linalg.norm(pose_a[:2] - pose_b[:2]))
        yaw_diff = float(abs(pose_a[-1] - pose_b[-1])) if pose_a.size >= 6 and pose_b.size >= 6 else 0.0
        if pos_dist > 0.05 or yaw_diff > 0.08:
            return False

    grid_a = np.asarray(sig_a["grid"], dtype=np.float64)
    grid_b = np.asarray(sig_b["grid"], dtype=np.float64)
    if grid_a.shape != grid_b.shape:
        return False

    bg_diff_p75 = float(np.percentile(np.abs(grid_a - grid_b), 75))
    return bg_diff_p75 <= diff_threshold


def check_folder_scene_redundancy(folder, file_names, rules=None, preloaded_scenes=None):
    """同批次跨文件静止机位重复占比校验。"""
    rules = rules or {}
    if not rules.get("check_scene_redundancy", True):
        return {}, None

    min_scene_files = int(rules.get("min_scene_files", DEFAULT_MIN_SCENE_FILES))
    max_ratio = float(rules.get("max_same_scene_ratio", DEFAULT_MAX_SAME_SCENE_RATIO))
    diff_threshold = float(rules.get("scene_diff_threshold", DEFAULT_SCENE_DIFF_THRESHOLD))
    min_cluster_files = int(rules.get("min_cluster_files", 3))

    if len(file_names) < min_scene_files:
        return {}, None

    raw_sigs = {}
    for name in file_names:
        stem, _ = os.path.splitext(name)
        if "_vla_fail" in stem or "_fail" in stem:
            continue
        if preloaded_scenes is not None and name in preloaded_scenes:
            sig = preloaded_scenes[name]
        else:
            sig = extract_scene_signature(os.path.join(folder, name))
        if sig is not None and sig.get("grid") is not None:
            raw_sigs[name] = sig

    if len(raw_sigs) < min_scene_files:
        return {}, None

    cam_groups = {}
    for name, sig in raw_sigs.items():
        cam_groups.setdefault(sig.get("camera", ""), []).append(name)
    _, group_names = max(cam_groups.items(), key=lambda kv: len(kv[1]))
    n_total = len(group_names)
    if n_total < min_scene_files:
        return {}, None

    neighbors = {name: [] for name in group_names}
    for i in range(n_total):
        na = group_names[i]
        for j in range(i + 1, n_total):
            nb = group_names[j]
            if _is_same_scene_signature(raw_sigs[na], raw_sigs[nb], diff_threshold):
                neighbors[na].append(nb)
                neighbors[nb].append(na)

    unassigned = set(group_names)
    clusters = []
    while unassigned:
        anchor = max(
            sorted(unassigned),
            key=lambda x: sum(1 for nb in neighbors[x] if nb in unassigned),
        )
        cluster = [anchor] + [nb for nb in sorted(neighbors[anchor]) if nb in unassigned]
        for member in cluster:
            unassigned.discard(member)
        clusters.append(cluster)

    clusters.sort(key=lambda c: (-len(c), c[0]))
    max_cluster_size = len(clusters[0]) if clusters else 0
    observed_max_ratio = float(max_cluster_size) / float(n_total) if n_total > 0 else 0.0

    anomalies = {}
    flagged_files = []
    if max_ratio < 1.0:
        for cluster in clusters:
            c_size = len(cluster)
            c_ratio = float(c_size) / float(n_total)
            if c_size >= min_cluster_files and c_ratio > max_ratio + 1e-9:
                reason = (
                    "同一场景/机位采集占比过高: %d/%d (%.0f%% > 阈值 %.0f%%)"
                    % (c_size, n_total, c_ratio * 100, max_ratio * 100)
                )
                for name in cluster:
                    anomalies[name] = [reason]
                    flagged_files.append(name)

    scene_diversity = {
        "evaluated_files": n_total,
        "scene_cluster_count": len(clusters),
        "max_scene_cluster_size": max_cluster_size,
        "max_scene_ratio": round(observed_max_ratio, 4),
        "max_allowed_ratio": round(max_ratio, 4),
    }
    if flagged_files:
        scene_diversity["flagged_scene_files"] = flagged_files

    return anomalies, scene_diversity


def ensure_directories(*dirs):
    """创建不存在的目录。"""
    for d in dirs:
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)


def unique_dest(dest):
    """目标路径冲突时自动追加递增序号。"""
    if not os.path.exists(dest):
        return dest
    base, ext = os.path.splitext(dest)
    index = 1
    while True:
        candidate = "%s_%d%s" % (base, index, ext)
        if not os.path.exists(candidate):
            return candidate
        index += 1


def resolve_dir(target, fallbacks):
    """解析目录路径。"""
    if target and (os.path.exists(target) or target not in fallbacks):
        return target
    for fb in fallbacks:
        if os.path.exists(fb):
            return fb
    return target or (fallbacks[0] if fallbacks else "")


RECORD_FOLDER_RE = re.compile(r"^record_data_\d{8}_\d+$")
def is_record_folder(name):
    """判定是否符合 record_data_<YYYYMMDD>_<数字> 命名。"""
    return RECORD_FOLDER_RE.match(name) is not None


def _is_reserved_output_folder(name):
    """判定是否为输出保留目录。"""
    if not name or name.startswith("."):
        return True
    if name in _RESERVED_OUTPUT_DIR_NAMES:
        return True
    if name.endswith(_RESERVED_OUTPUT_SUFFIXES):
        return True
    if re.search(r"_(passed|pending)_\d+$", name):
        return True
    return False


def find_hdf5_files(folder):
    """获取目录内所有 HDF5 文件名列表。"""
    if not os.path.isdir(folder):
        return []
    return sorted(
        f for f in os.listdir(folder)
        if f.endswith((".hdf5", ".h5")) and os.path.isfile(os.path.join(folder, f))
    )


def write_file_fail_reason(file_path, reasons):
    """向 HDF5 根属性写入 qc_result 与 qc_reason。"""
    unique_reasons = list(dict.fromkeys(reasons)) if reasons else []
    text = "\n".join(unique_reasons) if unique_reasons else "质检未通过"
    try:
        with h5py.File(file_path, "a") as h:
            h.attrs["qc_result"] = "FAIL"
            h.attrs["qc_reason"] = text
    except Exception as e:
        print("  属性写入失败 %s: %s" % (file_path, e))


def write_json_report(json_dir, folder_name, passed_files, review_files,
                      extra_errors=None, scene_diversity=None):
    """生成文件夹质检 JSON 报告。"""
    ensure_directories(json_dir)
    dedup_review = {
        k: list(dict.fromkeys(v)) for k, v in review_files.items()
    }
    report = {
        "folder": folder_name,
        "total_files": len(passed_files) + len(dedup_review),
        "pass_count": len(passed_files),
        "pending_count": len(dedup_review),
        "passed_files": list(passed_files),
        "pending_files": dedup_review,
    }
    if scene_diversity is not None:
        report["scene_diversity"] = scene_diversity
    if extra_errors:
        report["folder_errors"] = list(dict.fromkeys(extra_errors))

    json_path = os.path.join(json_dir, "%s.json" % folder_name)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return json_path


def _transfer_files(src_folder, dest_folder, file_names, copy_mode):
    """批量移动或复制文件。"""
    os.makedirs(dest_folder, exist_ok=True)
    for name in file_names:
        src_file = os.path.join(src_folder, name)
        dst_file = os.path.join(dest_folder, name)
        if copy_mode:
            shutil.copy2(src_file, dst_file)
        else:
            shutil.move(src_file, dst_file)


def _resolve_workers(workers, rules):
    """解析并行进程数（0 表示自动匹配 CPU 核心数）。"""
    if workers is None:
        workers = (rules or {}).get("workers", 0)
    workers = int(workers)
    if workers <= 0:
        return min(DEFAULT_MAX_WORKERS, os.cpu_count() or 1)
    return workers


def _render_progress(label, current, total, start_time, width=24, final=False):
    """渲染终端单行进度条。"""
    if total <= 0:
        return
    ratio = min(1.0, max(0.0, float(current) / float(total)))
    filled = int(round(width * ratio))
    bar = "█" * filled + "░" * (width - filled)
    elapsed = max(1e-6, time.monotonic() - start_time)
    rate = current / elapsed
    msg = "\r  %s [%s] %d/%d (%3.0f%%) | %.1f 文件/s" % (
        label, bar, current, total, ratio * 100.0, rate
    )
    sys.stdout.write(msg + ("\n" if final else ""))
    sys.stdout.flush()


def _inspect_folder_files(src, h5_files, rules, max_workers, show_progress=True, folder_label=""):
    """并行执行目录内单文件质检并实时更新进度条。"""
    tasks = [(name, os.path.join(src, name), rules) for name in h5_files]
    total = len(tasks)
    if total == 0:
        return []

    label = folder_label or os.path.basename(src) or "质检"
    start_time = time.monotonic()
    if show_progress:
        _render_progress(label, 0, total, start_time)

    if max_workers > 1 and total > 1:
        try:
            ordered_results = [None] * total
            with ProcessPoolExecutor(max_workers=min(max_workers, total)) as pool:
                future_to_idx = {
                    pool.submit(_inspect_episode_worker, t): idx
                    for idx, t in enumerate(tasks)
                }
                done_count = 0
                for fut in as_completed(future_to_idx):
                    idx = future_to_idx[fut]
                    ordered_results[idx] = fut.result()
                    done_count += 1
                    if show_progress:
                        _render_progress(label, done_count, total, start_time, final=(done_count == total))
            return ordered_results
        except Exception:
            pass

    results = []
    for i, t in enumerate(tasks, 1):
        results.append(_inspect_episode_worker(t))
        if show_progress:
            _render_progress(label, i, total, start_time, final=(i == total))
    return results


def _discover_target_folders(input_dir, custom_mode=False, dry_run=False, copy_mode=False):
    """解析待检文件夹列表，返回 [(folder_name, folder_abs_path), ...]。"""
    abs_input = os.path.abspath(input_dir)
    if not os.path.isdir(abs_input):
        return []

    if find_hdf5_files(abs_input):
        folder_name = os.path.basename(abs_input.rstrip("\\/")) or "dataset"
        return [(folder_name, abs_input)]

    entries = sorted(os.listdir(abs_input))
    targets = []
    for d in entries:
        src_path = os.path.join(abs_input, d)
        if not os.path.isdir(src_path) or _is_reserved_output_folder(d):
            continue
        if is_record_folder(d):
            if find_hdf5_files(src_path):
                targets.append((d, src_path))
            elif not dry_run and not copy_mode:
                shutil.rmtree(src_path, ignore_errors=True)
        elif custom_mode and find_hdf5_files(src_path):
            targets.append((d, src_path))

    return targets


def select_input_output_dirs(default_input=None, default_output=None):
    """通过弹窗或终端交互选择输入与输出目录。"""
    in_dir = default_input
    out_dir = default_output

    if not in_dir or not out_dir:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass
            if not in_dir:
                selected_in = filedialog.askdirectory(
                    title="选择待检输入文件夹",
                    initialdir=os.getcwd(),
                )
                if selected_in:
                    in_dir = selected_in
            if in_dir and not out_dir:
                selected_out = filedialog.askdirectory(
                    title="选择质检输出文件夹 (自动创建 _passed 与 _pending)",
                    initialdir=os.path.dirname(os.path.abspath(in_dir)) or os.getcwd(),
                )
                if selected_out:
                    out_dir = selected_out
            root.destroy()
        except Exception:
            pass

    if not in_dir and sys.stdin is not None and sys.stdin.isatty():
        try:
            in_dir = input("输入文件夹路径: ").strip().strip("\"'")
        except EOFError:
            in_dir = ""
    if in_dir and not out_dir and sys.stdin is not None and sys.stdin.isatty():
        try:
            out_dir = input("输出文件夹路径 (回车默认同级目录): ").strip().strip("\"'")
        except EOFError:
            out_dir = ""
        if not out_dir:
            out_dir = os.path.dirname(os.path.abspath(in_dir)) or os.getcwd()

    return in_dir, out_dir


def run_qc(input_dir, pass_dir=None, review_dir=None, copy_mode=False, dry_run=False, rules=None,
           json_dir=None, workers=None, output_dir=None):
    """执行批量质检与目录分流。"""
    rules = rules or {}
    custom_output_mode = output_dir is not None
    show_progress = bool(rules.get("show_progress", True))

    if custom_output_mode:
        ensure_directories(input_dir, output_dir)
    else:
        pass_dir = pass_dir or "passed"
        review_dir = review_dir or "pending"
        if json_dir is None:
            base_parent = os.path.dirname(os.path.abspath(pass_dir))
            json_dir = os.path.join(base_parent, "json")
        if os.path.abspath(input_dir) == os.path.abspath(review_dir):
            raise ValueError("输入目录与复审目录不可相同")
        ensure_directories(input_dir, pass_dir, review_dir, json_dir)

    custom_scan = custom_output_mode or bool(rules.get("custom_input_mode", False))
    target_folders = _discover_target_folders(
        input_dir,
        custom_mode=custom_scan,
        dry_run=dry_run,
        copy_mode=copy_mode,
    )

    if not target_folders:
        print("未检出待审 HDF5 文件夹: %s" % input_dir)
        return 0, 0

    max_workers = _resolve_workers(workers, rules)
    total_pass_files = 0
    total_review_files = 0

    print("质检启动 | 输入: %s | 目录数: %d | 进程数: %d" % (input_dir, len(target_folders), max_workers))
    print("-" * 60)

    for folder_idx, (fname, src) in enumerate(target_folders, 1):
        h5_files = find_hdf5_files(src)

        passed_files = []
        review_files = {}
        unreadable_files = set()
        preloaded_trajs = {}
        preloaded_scenes = {}
        preloaded_occlusions = {}
        folder_errors = []

        progress_label = "[%d/%d] %s" % (folder_idx, len(target_folders), fname)
        results = _inspect_folder_files(
            src,
            h5_files,
            rules,
            max_workers,
            show_progress=show_progress,
            folder_label=progress_label,
        )
        for item in results:
            name, is_pass, file_findings, traj, scene_sig = item[:5]
            occ_sig = item[5] if len(item) > 5 else None
            if is_pass is None:
                review_files[name] = list(dict.fromkeys(msg for _, msg in file_findings))
                unreadable_files.add(name)
            elif is_pass:
                passed_files.append(name)
                if traj is not None:
                    preloaded_trajs[name] = traj
                if scene_sig is not None:
                    preloaded_scenes[name] = scene_sig
                if occ_sig is not None:
                    preloaded_occlusions[name] = occ_sig
            else:
                reasons = list(dict.fromkeys(msg for level, msg in file_findings if level == "FAIL"))
                review_files[name] = reasons if reasons else ["质检未通过"]

        valid_episode_names = [name for name in h5_files if name not in review_files]


        action_anomalies = check_folder_action_consistency(
            src,
            valid_episode_names,
            rules,
            preloaded_trajs=preloaded_trajs,
        )
        for name, act_reasons in action_anomalies.items():
            if name in passed_files:
                passed_files.remove(name)
                review_files[name] = list(dict.fromkeys(act_reasons))
            elif name in review_files:
                review_files[name] = list(dict.fromkeys(review_files[name] + list(act_reasons)))


        occ_anomalies = check_folder_camera_occlusion(
            src,
            valid_episode_names,
            rules,
            preloaded_occlusions=preloaded_occlusions,
        )
        for name, occ_reasons in occ_anomalies.items():
            if name in passed_files:
                passed_files.remove(name)
                review_files[name] = list(dict.fromkeys(occ_reasons))
            elif name in review_files:
                review_files[name] = list(dict.fromkeys(review_files[name] + list(occ_reasons)))


        scene_anomalies, scene_diversity = check_folder_scene_redundancy(
            src,
            valid_episode_names,
            rules,
            preloaded_scenes=preloaded_scenes,
        )
        for name, scene_reasons in scene_anomalies.items():
            if name in passed_files:
                passed_files.remove(name)
                review_files[name] = list(dict.fromkeys(scene_reasons))
            elif name in review_files:
                review_files[name] = list(dict.fromkeys(review_files[name] + list(scene_reasons)))

        total_pass_files += len(passed_files)
        total_review_files += len(review_files)

        if custom_output_mode:
            pass_dest = unique_dest(os.path.join(output_dir, "%s_passed" % fname))
            review_dest = unique_dest(os.path.join(output_dir, "%s_pending" % fname))
            os.makedirs(pass_dest, exist_ok=True)
            os.makedirs(review_dest, exist_ok=True)

            if not dry_run:
                if passed_files:
                    _transfer_files(src, pass_dest, passed_files, copy_mode=copy_mode)
                if review_files:
                    _transfer_files(src, review_dest, list(review_files), copy_mode=copy_mode)
                    for name, reasons in review_files.items():
                        if name not in unreadable_files:
                            write_file_fail_reason(os.path.join(review_dest, name), reasons)

                abs_src = os.path.abspath(src)
                abs_out = os.path.abspath(output_dir)
                if (
                    not copy_mode
                    and os.path.isdir(abs_src)
                    and abs_out != abs_src
                    and not abs_out.startswith(abs_src + os.sep)
                ):
                    shutil.rmtree(abs_src, ignore_errors=True)

            report_dir = review_dest
        else:
            if not dry_run:
                if passed_files:
                    pass_dest = unique_dest(os.path.join(pass_dir, fname))
                    _transfer_files(src, pass_dest, passed_files, copy_mode=copy_mode)

                if review_files:
                    review_dest = unique_dest(os.path.join(review_dir, fname))
                    _transfer_files(src, review_dest, list(review_files), copy_mode=copy_mode)
                    for name, reasons in review_files.items():
                        if name not in unreadable_files:
                            write_file_fail_reason(os.path.join(review_dest, name), reasons)

                if not copy_mode and os.path.isdir(src):
                    shutil.rmtree(src, ignore_errors=True)

            report_dir = json_dir

        write_json_report(
            report_dir,
            fname,
            passed_files,
            review_files,
            extra_errors=folder_errors,
            scene_diversity=scene_diversity,
        )

        mode_label = "试运行" if dry_run else ("已复制" if copy_mode else "已流转")
        print("[%s] %s | 合格: %d | 待复审: %d" %
              (mode_label, fname, len(passed_files), len(review_files)))

    print("-" * 60)
    summary_loc = output_dir if custom_output_mode else json_dir
    print(
        "质检完成 | 目录: %d | 合格: %d | 待复审: %d | 报告: %s"
        % (len(target_folders), total_pass_files, total_review_files, summary_loc)
    )
    return total_pass_files, total_review_files


def main():
    parser = argparse.ArgumentParser(description="HDF5 数据集自动化质检工具")
    parser.add_argument("--input", "-i", default=None, help="输入文件夹路径（单个采集目录或上层总目录）")
    parser.add_argument("--output", "-o", default=None, help="输出文件夹路径（自动生成 <目录名>_passed 与 <目录名>_pending，JSON 存入 _pending）")
    parser.add_argument("--select", "--gui", dest="select_dirs", action="store_true", help="弹窗或交互式选择输入与输出文件夹")
    parser.add_argument("--pass-dir", default=None, help="合格输出目录（默认模式）")
    parser.add_argument("--pending-dir", default=None, help="待复审输出目录（默认模式）")
    parser.add_argument("--json-dir", default=None, help="JSON 报告目录（默认模式）")
    parser.add_argument("--file", "-f", help="单文件质检路径")
    parser.add_argument("--copy", action="store_true", help="复制模式（保留原文件）")
    parser.add_argument("--dry-run", action="store_true", help="仅生成报告，不移动文件")
    parser.add_argument("--min-frames", type=int, default=10, help="最小帧数阈值")
    parser.add_argument("--max-gap-ms", type=float, default=1000.0, help="时间戳丢帧阈值 (ms)")
    parser.add_argument("--no-action-consistency", action="store_true", help="禁用跨文件动作一致性校验")
    parser.add_argument("--max-same-scene-ratio", type=float, default=DEFAULT_MAX_SAME_SCENE_RATIO,
                        help="单机位最大允许占比 (默认 %.2f)" % DEFAULT_MAX_SAME_SCENE_RATIO)
    parser.add_argument("--scene-diff-threshold", type=float, default=DEFAULT_SCENE_DIFF_THRESHOLD,
                        help="同机位背景像素差阈值 (0-255, 默认 %.1f)" % DEFAULT_SCENE_DIFF_THRESHOLD)
    parser.add_argument("--no-scene-check", action="store_true", help="禁用跨文件同机位占比校验")
    parser.add_argument("--no-occlusion-check", action="store_true", help="禁用相机遮挡校验")
    parser.add_argument("--no-progress", action="store_true", help="隐藏质检进度条")
    parser.add_argument("--workers", "-j", type=int, default=0, help="并行进程数 (默认 0 自动匹配 CPU，1 为单进程)")
    args = parser.parse_args()

    check_deps()

    rules = {
        "min_frames": args.min_frames,
        "max_gap_ms": args.max_gap_ms,
        "white_run_limit": 15,
        "check_action_consistency": not args.no_action_consistency,
        "check_scene_redundancy": not args.no_scene_check,
        "check_occlusion": not args.no_occlusion_check,
        "max_same_scene_ratio": args.max_same_scene_ratio,
        "scene_diff_threshold": args.scene_diff_threshold,
        "show_progress": not args.no_progress,
        "workers": args.workers,
    }

    if args.file:
        if not os.path.exists(args.file):
            sys.exit("文件不存在: %s" % args.file)
        start_t = time.monotonic()
        if rules["show_progress"]:
            _render_progress(os.path.basename(args.file), 0, 1, start_t)
        is_pass, findings = check_episode(args.file, rules)
        if rules["show_progress"]:
            _render_progress(os.path.basename(args.file), 1, 1, start_t, final=True)
        status = "合格" if is_pass else "待复审"
        json_dir = resolve_dir(args.json_dir, ["json"])
        base_name = os.path.basename(args.file)
        stem, _ = os.path.splitext(base_name)
        if is_pass:
            write_json_report(json_dir, stem, [base_name], {})
        else:
            reasons = [msg for _, msg in findings]
            write_json_report(json_dir, stem, [], {base_name: reasons})
        print("[%s] %s" % (status, args.file))
        sys.exit(0 if is_pass else 1)

    in_dir = args.input
    out_dir = args.output
    legacy_dirs_specified = any(x is not None for x in (args.pass_dir, args.pending_dir, args.json_dir))

    if args.select_dirs:
        in_dir, out_dir = select_input_output_dirs(default_input=in_dir, default_output=out_dir)
        if not in_dir:
            sys.exit("未选择输入文件夹，已退出。")

    if out_dir is not None or (in_dir is not None and not legacy_dirs_specified):
        input_dir = in_dir if in_dir is not None else resolve_dir("record", ["record"])
        if out_dir is None:
            abs_in = os.path.abspath(input_dir)
            out_dir = os.path.dirname(abs_in) if find_hdf5_files(abs_in) else abs_in
        rules["custom_input_mode"] = True
        run_qc(
            input_dir,
            copy_mode=args.copy,
            dry_run=args.dry_run,
            rules=rules,
            workers=args.workers,
            output_dir=out_dir,
        )
        return 0

    input_dir = resolve_dir(in_dir or "record", ["record"])
    pass_dir = resolve_dir(args.pass_dir or "passed", ["passed", "valid", "合格"])
    pending_dir = resolve_dir(args.pending_dir or "pending", ["pending", "待复审"])
    json_dir = resolve_dir(args.json_dir or "json", ["json"])

    ensure_directories(input_dir, pass_dir, pending_dir, json_dir)
    run_qc(
        input_dir,
        pass_dir,
        pending_dir,
        copy_mode=args.copy,
        dry_run=args.dry_run,
        rules=rules,
        json_dir=json_dir,
        workers=args.workers,
    )
    return 0


if __name__ == "__main__":
    main()

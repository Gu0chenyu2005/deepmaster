#!/usr/bin/env python3
# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""为采集程序启用相机自动发现及 USB 端口映射。"""
import argparse
from pathlib import Path
import re


def transform(text):
    if '#include "realsense_camera_discovery.h"' in text:
        return text
    replacements = [
        ('#include <librealsense2/rs.hpp>', '#include <librealsense2/rs.hpp>\n#include "realsense_camera_discovery.h"'),
        ('    for (int i = 0; i < 30; ++i) pipe.wait_for_frames();',
         '    for (int i = 0; i < 5; ++i) pipe.wait_for_frames();'),
        ('GetEnvOrDefault("ARMDOG_PRIMARY_V4L_DEVICE", "/dev/video10")',
         'GetEnvOrDefault("ARMDOG_PRIMARY_V4L_DEVICE", "none")'),
        ('GetEnvOrDefault("ARMDOG_THIRD_V4L_DEVICE", "/dev/video4")',
         'GetEnvOrDefault("ARMDOG_THIRD_V4L_DEVICE", "none")'),
        ('  const bool primary_started =\n      StartRealSensePipelineWithFallback(',
         '  const bool primary_started =\n      !primary_realsense_serial_.empty() &&\n      StartRealSensePipelineWithFallback('),
    ]
    for old, new in replacements:
        if old not in text and new in text:
            continue
        if text.count(old) != 1:
            raise ValueError(f'源码锚点异常: {old[:80]}')
        text = text.replace(old, new, 1)
    text, count = re.subn(
        r'  // yc_M3 physical roles:.*?(?=  primary_v4l_device_ =)',
        '  primary_realsense_serial_.clear();\n'
        '  third_realsense_serial_.clear();\n'
        '  d435i_realsense_serial_.clear();\n',
        text, count=1, flags=re.S)
    if count != 1:
        raise ValueError('缺少旧相机序列号配置段')
    start = text.index('  auto serials = QueryRealSenseSerials();')
    end = text.index('  rs2::pipeline primary_pipe;', start)
    text = (text[:start] +
            '  const auto cameras = ResolveRealSenseCameraRoles(DiscoverRealSenseCameras());\n'
            '  primary_realsense_serial_ = cameras[0].serial;\n'
            '  third_realsense_serial_ = kThirdViewEnabled ? cameras[1].serial : "";\n'
            '  d435i_realsense_serial_ = cameras[2].serial;\n\n' + text[end:])
    start = text.index('static std::vector<std::string> QueryRealSenseSerials()')
    end = text.index('static void ConfigureRealSenseSensor(', start)
    return text[:start] + text[end:]


def apply(source):
    original = source.read_text()
    updated = transform(original)
    for name in ('camera_assignment.h', 'realsense_camera_discovery.h'):
        target = source.parent / name
        temporary = target.with_name(target.name + '.tmp')
        temporary.write_bytes((Path(__file__).parent / name).read_bytes())
        temporary.replace(target)
    if updated != original:
        temporary = source.with_name(source.name + '.tmp')
        temporary.write_text(updated)
        temporary.chmod(source.stat().st_mode & 0o777)
        temporary.replace(source)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--src', type=Path, required=True)
    apply(parser.parse_args().src)

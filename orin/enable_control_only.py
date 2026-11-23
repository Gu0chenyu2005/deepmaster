#!/usr/bin/env python3
# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""为采集程序启用无录制线程的仅控制模式。"""
import argparse
from pathlib import Path
import re


CONSTRUCTOR = '''ArmDogCollectState::ArmDogCollectState()
    : control_only_([] {
        const char* mode = std::getenv("ARMDOG_CONTROL_ONLY");
        return mode != nullptr && std::string(mode) == "1";
      }()) {}'''

CAMERA_GUARD = '''  const char* camera_disable = std::getenv("ARMDOG_CAMERA_DISABLE");
  const char* control_only = std::getenv("ARMDOG_CONTROL_ONLY");
  if ((camera_disable && std::string(camera_disable) == "1") ||
      (control_only && std::string(control_only) == "1")) {
    camera_init_done_.store(true);
    std::cout << "[ArmDogRecorder] camera capture disabled by control-only mode\\n";
    return;
  }
'''


def replace_once(text, old, new):
    if new in text:
        return text
    if text.count(old) != 1:
        raise ValueError(f'源码锚点异常: {old[:80]}')
    return text.replace(old, new, 1)


def guard_method(text, name, result=''):
    pattern = rf'(ArmDogCollectState::{name}\([^{{]*?\)\s*(?:const\s*)?\{{\n)'
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise ValueError(f'缺少唯一方法: {name}')
    end = matches[0].end()
    guard = f'  if (control_only_) return{result};\n'
    if not text[end:].startswith(guard):
        text = text[:end] + guard + text[end:]
    return text


def transform_header(text):
    return replace_once(text, ' private:\n', ' private:\n  const bool control_only_;\n')


def transform_collect(text):
    for include in ('cstdlib', 'string'):
        if f'#include <{include}>' not in text:
            text = f'#include <{include}>\n' + text
    text = replace_once(text, 'ArmDogCollectState::ArmDogCollectState() {}', CONSTRUCTOR)
    for name in ('Init', 'Finish', 'CollectLoop', 'ButtonTick', 'Update'):
        text = guard_method(text, name)
    text = guard_method(text, 'IsRecording', ' false')
    legacy_button_guard = '''  const char* control_only = std::getenv("ARMDOG_CONTROL_ONLY");
  if (control_only && std::string(control_only) == "1") return;

'''
    return text.replace(legacy_button_guard, '')


def transform_recorder(text):
    if CAMERA_GUARD in text:
        return text
    legacy_guard = '''  const char* camera_disable = std::getenv("ARMDOG_CAMERA_DISABLE");
  if (camera_disable && std::string(camera_disable) == "1") {
    camera_init_done_.store(true);
    std::cout << "[ArmDogRecorder] camera capture disabled by control-only mode\\n";
    return;
  }
'''
    if legacy_guard in text:
        return replace_once(text, legacy_guard, CAMERA_GUARD)
    anchor = 'void ArmDogRecorder::CameraLoop() {\n  camera_init_done_.store(false);\n'
    return replace_once(text, anchor, anchor + CAMERA_GUARD)


def apply(source):
    changes = []
    for path, transform in (
        (source / 'state_machine/mani_data_collect/armdog_collect_state.h', transform_header),
        (source / 'state_machine/mani_data_collect/armdog_collect_state.cpp', transform_collect),
        (source / 'utils/armdog_recorder.cpp', transform_recorder),
    ):
        original = path.read_text()
        changes.append((path, original, transform(original)))
    for path, original, updated in changes:
        if updated != original:
            temporary = path.with_name(path.name + '.tmp')
            temporary.write_text(updated)
            temporary.chmod(path.stat().st_mode & 0o777)
            temporary.replace(path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--src', type=Path, required=True, help='rl_deploy 源码目录')
    apply(parser.parse_args().src)

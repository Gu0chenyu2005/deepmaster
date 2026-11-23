# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""验证仅控制模式不创建录制资源，并保持普通采集可用。"""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('enable_control_only', ROOT / 'orin/enable_control_only.py')
PATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCH)

HEADER = '''class ArmDogCollectState {
 public:
  ArmDogCollectState();
  void Init();
  void Finish();
  void CollectLoop();
  void ButtonTick();
  void Update(int);
  bool IsRecording() const;
 private:
};
'''

COLLECT = '''#include <cstdlib>
#include <string>
ArmDogCollectState::ArmDogCollectState() {}
void ArmDogCollectState::Init() {
  ++resources;
}
void ArmDogCollectState::Finish() {
  ++cleanup;
}
void ArmDogCollectState::CollectLoop() {
  ++worker;
}
void ArmDogCollectState::ButtonTick() {
  ++buttons;
}
void ArmDogCollectState::Update(int) {
  ++updates;
}
bool ArmDogCollectState::IsRecording() const {
  return true;
}
'''

RECORDER = '''void ArmDogRecorder::CameraLoop() {
  camera_init_done_.store(false);
  ++cameras;
}
'''


class ControlOnlyTests(unittest.TestCase):
    def test_apply_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            inputs = {
                'state_machine/mani_data_collect/armdog_collect_state.h': HEADER,
                'state_machine/mani_data_collect/armdog_collect_state.cpp': COLLECT,
                'utils/armdog_recorder.cpp': RECORDER,
            }
            for name, content in inputs.items():
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            PATCH.apply(source)
            first = {name: (source / name).read_bytes() for name in inputs}
            PATCH.apply(source)
            self.assertEqual(first, {name: (source / name).read_bytes() for name in inputs})

    def test_invalid_source_does_not_partially_patch(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            inputs = {
                'state_machine/mani_data_collect/armdog_collect_state.h': HEADER,
                'state_machine/mani_data_collect/armdog_collect_state.cpp': 'incompatible source',
                'utils/armdog_recorder.cpp': RECORDER,
            }
            for name, content in inputs.items():
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            with self.assertRaises(ValueError):
                PATCH.apply(source)
            self.assertEqual(inputs, {name: (source / name).read_text() for name in inputs})

    def test_compiled_switch_skips_all_recording_work(self):
        compiler = shutil.which('g++')
        if not compiler:
            self.skipTest('g++ unavailable')
        code = '''#include <atomic>
#include <cassert>
#include <cstdlib>
#include <iostream>
#include <string>
int resources = 0, cleanup = 0, worker = 0, buttons = 0, updates = 0, cameras = 0;
''' + PATCH.transform_header(HEADER) + PATCH.transform_collect(COLLECT) + '''
class ArmDogRecorder {
 public:
  std::atomic<bool> camera_init_done_{false};
  void CameraLoop();
};
''' + PATCH.transform_recorder(RECORDER) + '''
void Exercise(ArmDogCollectState& state) {
  state.Init(); state.CollectLoop(); state.ButtonTick(); state.Update(0); state.Finish();
}
int main() {
  setenv("ARMDOG_CONTROL_ONLY", "1", 1);
  ArmDogCollectState remote;
  unsetenv("ARMDOG_CONTROL_ONLY");
  Exercise(remote);
  assert(!remote.IsRecording());
  assert(resources == 0 && cleanup == 0 && worker == 0 && buttons == 0 && updates == 0);
  ArmDogCollectState local;
  setenv("ARMDOG_CONTROL_ONLY", "1", 1);
  Exercise(local);
  assert(local.IsRecording());
  assert(resources == 1 && cleanup == 1 && worker == 1 && buttons == 1 && updates == 1);
  ArmDogRecorder recorder;
  recorder.CameraLoop();
  assert(cameras == 0 && recorder.camera_init_done_);
  unsetenv("ARMDOG_CONTROL_ONLY");
  setenv("ARMDOG_CAMERA_DISABLE", "1", 1);
  recorder.CameraLoop();
  assert(cameras == 0 && recorder.camera_init_done_);
  unsetenv("ARMDOG_CAMERA_DISABLE");
  recorder.CameraLoop();
  assert(cameras == 1);
  setenv("ARMDOG_CONTROL_ONLY", "0", 1);
  ArmDogCollectState explicit_local;
  Exercise(explicit_local);
  assert(resources == 2 && updates == 2);
}
'''
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'control.cpp'
            binary = Path(temp) / 'control'
            source.write_text(code)
            subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            str(source), '-o', str(binary)], check=True,
                           capture_output=True, text=True)
            subprocess.run([str(binary)], check=True, capture_output=True, text=True)

    def test_upgrades_existing_camera_guard(self):
        legacy = '''  const char* camera_disable = std::getenv("ARMDOG_CAMERA_DISABLE");
  if (camera_disable && std::string(camera_disable) == "1") {
    camera_init_done_.store(true);
    std::cout << "[ArmDogRecorder] camera capture disabled by control-only mode\\n";
    return;
  }
'''
        source = RECORDER.replace('  ++cameras;', legacy + '  ++cameras;')
        updated = PATCH.transform_recorder(source)
        self.assertEqual(updated, PATCH.transform_recorder(updated))
        self.assertEqual(updated.count('std::getenv("ARMDOG_CAMERA_DISABLE")'), 1)
        self.assertIn('std::getenv("ARMDOG_CONTROL_ONLY")', updated)


if __name__ == '__main__':
    unittest.main()

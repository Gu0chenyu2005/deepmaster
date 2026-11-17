# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""验证相机更换、枚举变化与掉线时的视角分配。"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CameraAssignmentTests(unittest.TestCase):
    def test_camera_replacement_and_missing_devices(self):
        compiler = shutil.which('g++')
        if not compiler:
            self.skipTest('g++ unavailable')
        source = r'''
#include "camera_assignment.h"
#include <cassert>
using namespace utils;
int main() {
  const CameraPorts ports{"/usb/2-3.4", "/usb/2-3.3", "/usb/2-3.2"};
  const auto normalized = NormalizeCameraPort(
      "/usb/2-3.4/2-3.4:1.0/video4linux/video12");
  assert(normalized == "/usb/2-3.4/2-3.4");
  auto selected = AssignCameraRoles({{"new-wrist", ports[2]},
                                    {"new-primary", ports[0]},
                                    {"new-third", ports[1]}}, ports);
  assert(selected[0].serial == "new-primary");
  assert(selected[1].serial == "new-third");
  assert(selected[2].serial == "new-wrist");
  selected = AssignCameraRoles({{"new-third", ports[1]},
                               {"new-wrist", ports[2]}}, ports);
  assert(selected[0].serial.empty());
  assert(selected[1].serial == "new-third");
  assert(selected[2].serial == "new-wrist");
  selected = AssignCameraRoles({{"c", "/other/3"}, {"a", "/other/1"},
                               {"b", "/other/2"}}, ports);
  assert(selected[0].serial == "a");
  assert(selected[1].serial == "b");
  assert(selected[2].serial == "c");
  selected = AssignCameraRoles({{"a", "/other/1:1.0/video4linux/video1"},
                               {"a", "/other/1:1.0/video4linux/video2"},
                               {"b", "/other/2:1.0/video4linux/video3"}}, {});
  assert(selected[0].serial == "a");
  assert(selected[1].serial == "b");
  assert(selected[2].serial.empty());
  selected = AssignCameraRoles({}, ports);
  for (const auto& camera : selected) assert(camera.serial.empty());
  const std::vector<CameraDevice> extra{{"a", "/a"}, {"b", "/b"},
                                        {"c", "/c"}, {"d", "/d"}};
  selected = AssignCameraRoles(extra, {});
  assert(selected[0].serial == "a");
  assert(selected[1].serial == "b");
  assert(selected[2].serial == "c");
}
'''
        with tempfile.TemporaryDirectory() as temp:
            cpp = Path(temp) / 'assignment.cpp'
            cpp.write_text(source)
            binary = Path(temp) / 'assignment'
            subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror',
                            '-I', str(ROOT / 'orin'), str(cpp), '-o', str(binary)],
                           check=True, capture_output=True, text=True)
            subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    unittest.main()

# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""验证采集目录由运行环境决定，与编译目录无关。"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class RecordingPathsTests(unittest.TestCase):
    def test_runtime_root_changes_without_recompilation(self):
        compiler = shutil.which('g++')
        if not compiler:
            self.skipTest('g++ unavailable')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'paths.cpp'
            source.write_text('#include "recording_paths.h"\n#include <iostream>\n'
                              'int main() { std::cout << humanoid::RecordingRoot().string(); }\n')
            binary = root / 'paths'
            include = Path(__file__).resolve().parents[1] / 'orin'
            subprocess.run([compiler, '-std=c++17', '-I', str(include), str(source),
                            '-o', str(binary)], check=True, capture_output=True)
            env = dict(os.environ)
            env.pop('ARMDOG_DATA_ROOT', None)
            result = subprocess.check_output([binary], cwd=root, env=env, text=True)
            self.assertEqual(result, str(root / 'data'))
            for value in (str(root / 'session-one'), str(root / 'session-two'), 'relative-session'):
                env['ARMDOG_DATA_ROOT'] = value
                result = subprocess.check_output([binary], cwd=root, env=env, text=True)
                self.assertEqual(result, str((root / value).resolve()))

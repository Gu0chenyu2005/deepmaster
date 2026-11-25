# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""验证手柄服务启动脚本保持前台进程生命周期。"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


class ServiceLauncherTests(unittest.TestCase):
    def test_launcher_tracks_service_until_it_exits(self):
        source = Path(__file__).resolve().parents[1] / "packaging" / "runService.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher = root / "runService.sh"
            shutil.copy2(source, launcher)
            launcher.chmod(0o755)
            binary = root / "RoboticsServiceProcess"
            binary.write_text("#!/bin/bash\nsleep 30\n")
            binary.chmod(0o755)

            proc = subprocess.Popen([str(launcher)], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            try:
                time.sleep(0.15)
                self.assertIsNone(proc.poll())
            finally:
                proc.terminate()
                proc.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()

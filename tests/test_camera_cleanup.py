"""验证相机占用清理及机器人进程保护。"""
import signal
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import agent


class CameraCleanupTests(unittest.TestCase):
    def test_lsof_scans_explicit_video_devices(self):
        with patch.object(agent.glob, "glob", return_value=["/dev/video2", "/dev/video0"]), \
             patch.object(agent.shutil, "which", return_value="/usr/bin/lsof"), \
             patch.object(agent.os, "geteuid", return_value=0), \
             patch.object(agent.subprocess, "run", return_value=SimpleNamespace(
                 returncode=0, stdout="123\n123\n456\n", stderr="")) as run:
            self.assertEqual(agent._camera_holders(), {123, 456})
            self.assertEqual(run.call_args.args[0],
                             ["/usr/bin/lsof", "-w", "-t", "--", "/dev/video0", "/dev/video2"])

    def test_term_releases_camera_without_force(self):
        with patch.object(agent, "_camera_holders", side_effect=[{4242}, set(), set()]), \
             patch.object(agent.runner, "find_pids", return_value=[]), \
             patch.object(agent.os, "kill") as kill:
            self.assertTrue(agent._release_camera_devices()["ok"])
            kill.assert_called_once_with(4242, signal.SIGTERM)

    def test_stubborn_holder_gets_kill(self):
        with patch.object(agent, "_camera_holders", side_effect=[{4242}, {4242}, set(), set()]), \
             patch.object(agent.runner, "find_pids", return_value=[]), \
             patch.object(agent.time, "monotonic", side_effect=[0, 3, 4]), \
             patch.object(agent.os, "kill") as kill:
            self.assertTrue(agent._release_camera_devices()["ok"])
            self.assertEqual([call.args for call in kill.call_args_list],
                             [(4242, signal.SIGTERM), (4242, signal.SIGKILL)])

    def test_robot_holder_prevents_all_kills(self):
        with patch.object(agent, "_camera_holders", return_value={4242, 4243}), \
             patch.object(agent.runner, "find_pids", return_value=[4242]), \
             patch.object(agent.os, "kill") as kill:
            self.assertFalse(agent._release_camera_devices()["ok"])
            kill.assert_not_called()

    def test_scan_failure_prevents_spawn_in_all_modes(self):
        for mode in ("shm", "direct", "direct-webrtc"):
            with self.subTest(mode=mode), \
                 patch.object(agent.os.path, "exists", return_value=True), \
                 patch.object(agent.runner, "_alive", return_value=False), \
                 patch.object(agent.runner, "stop", return_value={"ok": True}), \
                 patch.object(agent, "_direct_camera_command", side_effect=lambda cmd: cmd), \
                 patch.object(agent.subprocess, "run", return_value=SimpleNamespace(
                     returncode=0, stdout="3", stderr="")), \
                 patch.object(agent, "_camera_holders", side_effect=RuntimeError("扫描失败")), \
                 patch.object(agent.runner, "spawn") as spawn:
                self.assertFalse(agent.start_cam_viewer(mode)["ok"])
                spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()

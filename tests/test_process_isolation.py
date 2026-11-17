# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""验证采集端进程与其他程序的隔离。"""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import agent
import runner


class ProcessIsolationTests(unittest.TestCase):
    def test_external_deploy_prevents_start_before_service_launch(self):
        with patch.object(runner, "_alive", return_value=False), \
             patch.object(runner, "pgrep_count", return_value=1), \
             patch.object(runner, "spawn") as spawn:
            result = runner.run_t2({"rl_deploy_bin": "rl_deploy"})
        self.assertFalse(result["ok"])
        spawn.assert_not_called()

    def test_stop_does_not_kill_unmanaged_processes(self):
        with patch.object(runner, "_alive", return_value=False), \
             patch.object(runner, "send_key", return_value={"ok": False}), \
             patch.object(runner, "stop", return_value={"ok": True, "already": True}), \
             patch.object(runner, "stop_k7_force", return_value={"ok": True}), \
             patch.object(runner, "kill_by_name") as kill, \
             patch.object(runner, "_run_once") as shell:
            self.assertTrue(runner.stop_all({})["ok"])
            self.assertTrue(runner.stop_deploy({})["ok"])
            self.assertTrue(runner.stop_service({})["ok"])
        kill.assert_not_called()
        shell.assert_not_called()

    def test_capture_stop_keeps_hardware_services_running(self):
        with patch.object(runner, "send_key", return_value={"ok": True, "sent": "d"}) as send, \
             patch.object(runner, "time") as clock, \
             patch.object(runner, "stop", return_value={"ok": True}) as stop, \
             patch.object(runner, "stop_k7_force") as stop_k7, \
             patch.object(runner, "stop_service") as stop_service:
            result = runner.stop_capture({})

        self.assertTrue(result["ok"])
        send.assert_called_once_with(runner.RL_KEY, "d")
        clock.sleep.assert_called_once_with(runner.RL_GRACE)
        stop.assert_called_once_with(runner.RL_KEY)
        stop_k7.assert_not_called()
        stop_service.assert_not_called()
        self.assertEqual([s["step"] for s in result["steps"][-2:]],
                         ["keep_k7master3", "keep_robotservice"])

    def test_capture_stop_does_not_kill_process_if_stop_signal_fails(self):
        with patch.object(runner, "send_key", return_value={"ok": False, "error": "stdin closed"}), \
             patch.object(runner, "stop") as stop:
            result = runner.stop_capture({})

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "stdin closed")
        stop.assert_not_called()

    def test_agent_capture_stop_releases_only_preview_reader(self):
        with patch.object(agent.runner, "stop_capture", return_value={"ok": True, "steps": []}) as stop_capture, \
             patch.object(agent.runner, "stop", return_value={"ok": True, "already": True}) as stop:
            result = agent.stop_capture({"local_data_dir": "/tmp/data"})

        self.assertTrue(result["ok"])
        stop_capture.assert_called_once_with({"local_data_dir": "/tmp/data"})
        stop.assert_called_once_with(agent.STATE["cam_key"])
        self.assertEqual(result["steps"][-1]["step"], "stop_camera_preview")

    def test_session_path_outside_collector_root_fails(self):
        previous = dict(agent.STATE)
        self.addCleanup(agent.STATE.update, previous)
        with tempfile.TemporaryDirectory() as tmp:
            agent.STATE.update(cfg={}, data_root=tmp)
            with self.assertRaises(ValueError):
                agent.merge_cfg({"run_data_dir": str(Path(tmp).parent / "elsewhere")})
            session = Path(tmp) / "session"
            self.assertEqual(agent.merge_cfg({"run_data_dir": str(session)})["local_data_dir"],
                             str(session))
            self.assertFalse(session.exists())


if __name__ == "__main__":
    unittest.main()

# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""验证采集端进程与其他程序的隔离。"""
import os
import pwd
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import agent
import runner


class ProcessIsolationTests(unittest.TestCase):
    def test_direct_camera_uses_realsense_site_package_owner(self):
        uid = os.getuid()
        command = ["/usr/bin/python3", "camera_shm_viewer.py", "--direct"]
        fake_stat = type("Stat", (), {"st_uid": uid})()
        with patch.object(agent.os, "geteuid", return_value=0), \
             patch.object(agent.glob, "glob", return_value=["/home/operator/.local/lib/python3.10/site-packages/pyrealsense2"]), \
             patch.object(agent.os, "stat", return_value=fake_stat), \
             patch.object(agent.shutil, "which", return_value="/usr/sbin/runuser"):
            wrapped = agent._direct_camera_command(command)
        self.assertEqual(wrapped[:4], ["/usr/sbin/runuser", "-u", pwd.getpwuid(uid).pw_name, "--"])
        self.assertEqual(wrapped[4:], command)

    def test_control_only_camera_uses_video_cores_and_private_token_env(self):
        previous = dict(agent.STATE)
        self.addCleanup(agent.STATE.update, previous)
        agent.STATE.update(cfg={"cam_viewer_script": "/opt/dm-collect/cam/camera_shm_viewer.py",
                                "cam_viewer_port": 5000}, cam_mode="shm")
        probe = SimpleNamespace(returncode=0, stdout="3\n", stderr="")
        with patch.object(agent.os.path, "exists", return_value=True), \
             patch.object(agent.subprocess, "run", return_value=probe), \
             patch.object(agent, "_direct_camera_command", side_effect=lambda cmd: cmd), \
             patch.object(agent.runner, "_validate_control_scheduling", return_value={"ok": True}), \
             patch.object(agent.runner, "stop", return_value={"ok": True}), \
             patch.object(agent.runner, "spawn", return_value={"ok": True}) as spawn:
            result = agent.start_cam_viewer("direct-webrtc")
        self.assertTrue(result["ok"])
        command = spawn.call_args.args[2]
        self.assertEqual(command[:6], ["taskset", "-c", "4-7", "nice", "-n", "-10"])
        self.assertIn("--webrtc", command)
        self.assertNotIn(agent.STATE["webrtc_token"], command)
        self.assertEqual(spawn.call_args.kwargs["env"]["DM_WEBRTC_TOKEN"],
                         agent.STATE["webrtc_token"])

    def test_video_socket_is_marked_background_and_send_queue_is_bounded(self):
        class FakeSocket:
            def __init__(self):
                self.options = {}

            def setsockopt(self, level, option, value):
                self.options[(level, option)] = value

        sock = FakeSocket()
        agent._prioritize_video_socket(sock)
        self.assertEqual(sock.options[(agent.socket.IPPROTO_IP, agent.socket.IP_TOS)], 0x20)
        self.assertEqual(sock.options[(agent.socket.SOL_SOCKET, agent.socket.SO_PRIORITY)], 1)
        if hasattr(agent.socket, "TCP_NOTSENT_LOWAT"):
            self.assertEqual(sock.options[(agent.socket.IPPROTO_TCP, agent.socket.TCP_NOTSENT_LOWAT)],
                             16 * 1024)

    def test_external_deploy_prevents_start_before_service_launch(self):
        with patch.object(runner, "_alive", return_value=False), \
             patch.object(runner, "pgrep_count", return_value=1), \
             patch.object(runner, "spawn") as spawn:
            result = runner.run_t2({"rl_deploy_bin": "rl_deploy"})
        self.assertFalse(result["ok"])
        spawn.assert_not_called()

    def test_control_only_sets_camera_and_recording_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            deploy = Path(tmp) / "rl_deploy"
            (deploy / "build").mkdir(parents=True)
            service = Path(tmp) / "service"
            service.mkdir()
            with patch.object(runner, "_dry", return_value=False), \
                 patch.object(runner, "_alive", return_value=False), \
                 patch.object(runner, "pgrep_count", return_value=0), \
                 patch.object(runner, "_sudo", return_value=""), \
                 patch.object(runner, "_validate_control_scheduling", return_value={"ok": True}), \
                 patch.object(runner, "PROJECT_DIR", tmp), \
                 patch.object(runner, "_service_process_exists", return_value=False), \
                 patch.object(runner, "spawn", return_value={"ok": True, "pid": 123}) as spawn:
                result = runner.run_t2({"rl_deploy_dir": str(deploy),
                                        "robot_service_dir": str(service),
                                        "robot_service_script": "runService.sh"},
                                       control_only=True)

        self.assertTrue(result["ok"])
        command = spawn.call_args_list[-1].args[2]
        self.assertIn("taskset -c 8-11 nice -n -10", command)
        self.assertIn("ARMDOG_CONTROL_ONLY=1", command)
        self.assertIn("ARMDOG_CAMERA_DISABLE=1", command)
        self.assertIn("ARMDOG_SHARED_FRAME_ENABLE=0", command)
        self.assertTrue(spawn.call_args_list[-1].kwargs["want_stdin"])

    def test_running_package_robot_service_gets_control_affinity_and_priority(self):
        root = "/opt/dm-collect/prog/service"
        with patch.object(runner, "find_pids", return_value=[4242]), \
             patch.object(runner.os.path, "realpath", side_effect=lambda path:
                          root if path in (root, "/proc/4242/exe") else path), \
             patch.object(runner.os, "listdir", return_value=["4242", "4243"]), \
             patch.object(runner.os, "sched_setaffinity") as set_affinity, \
             patch.object(runner.os, "sched_getaffinity", return_value=set(range(8, 12))), \
             patch.object(runner.os, "setpriority") as set_priority, \
             patch.object(runner.os, "getpriority", return_value=-10):
            result = runner._pin_control_service(root)

        self.assertTrue(result["ok"])
        self.assertEqual(set_affinity.call_args_list[0].args, (4242, set(range(8, 12))))
        self.assertEqual(set_affinity.call_args_list[1].args, (4243, set(range(8, 12))))
        self.assertEqual([call.args for call in set_priority.call_args_list],
                         [(runner.os.PRIO_PROCESS, 4242, -10),
                          (runner.os.PRIO_PROCESS, 4243, -10)])

    def test_running_external_robot_service_is_not_modified(self):
        with patch.object(runner, "find_pids", return_value=[4242]), \
             patch.object(runner.os.path, "realpath", side_effect=lambda path:
                          "/opt/other/RoboticsServiceProcess" if path == "/proc/4242/exe" else path), \
             patch.object(runner.os, "sched_setaffinity") as affinity, \
             patch.object(runner.os, "setpriority") as priority:
            result = runner._pin_control_service("/opt/dm-collect/prog/service")

        self.assertFalse(result["ok"])
        self.assertIn("部署包外", result["error"])
        affinity.assert_not_called()
        priority.assert_not_called()

    def test_control_only_rejects_programs_outside_collector_package(self):
        with patch.object(runner, "_dry", return_value=False), \
             patch.object(runner, "_alive", return_value=False), \
             patch.object(runner, "pgrep_count", return_value=0), \
             patch.object(runner, "_validate_control_scheduling", return_value={"ok": True}), \
             patch.object(runner, "spawn") as spawn:
            result = runner.run_t2({"rl_deploy_dir": "/opt/other/rl_deploy",
                                    "robot_service_dir": "/opt/dm-collect/prog/service",
                                    "robot_service_script": "runService.sh"},
                                   control_only=True)
        self.assertFalse(result["ok"])
        self.assertIn("部署包", result["error"])
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

    def test_control_stop_is_noop_without_managed_deploy(self):
        with patch.object(runner, "_alive", return_value=False), \
             patch.object(runner, "send_key") as send, \
             patch.object(runner, "stop") as stop:
            result = runner.stop_control_only({})

        self.assertTrue(result["ok"])
        self.assertTrue(result["already"])
        send.assert_not_called()
        stop.assert_not_called()

    def test_control_stop_uses_near_capture_shutdown_sequence(self):
        with patch.object(runner, "_alive", return_value=True), \
             patch.object(runner, "send_key", return_value={"ok": True}) as send, \
             patch.object(runner, "time") as clock, \
             patch.object(runner, "stop", return_value={"ok": True}) as stop:
            result = runner.stop_control_only({})

        self.assertTrue(result["ok"])
        send.assert_called_once_with(runner.RL_KEY, "d")
        clock.sleep.assert_called_once_with(runner.RL_GRACE)
        stop.assert_called_once_with(runner.RL_KEY)

    def test_control_only_reuses_its_managed_hold_process(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        entry = {"proc": process, "cmd": "export ARMDOG_CONTROL_ONLY=1; exec rl_deploy"}
        with patch.dict(runner._procs, {runner.RL_KEY: entry}), \
             patch.object(runner, "_validate_control_scheduling", return_value={"ok": True}), \
             patch.object(runner, "spawn") as spawn:
            result = runner.run_t2({"rl_deploy_bin": "rl_deploy"}, control_only=True)

        self.assertTrue(result["ok"])
        self.assertTrue(result["already"])
        spawn.assert_not_called()

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

# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""验证设备配置隔离与采集目录一致性。"""
import contextlib
import json
import os
import re
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import agent
with patch.object(sys, "argv", ["api.py"]):
    import api


class DeviceConfigTests(unittest.TestCase):
    def setUp(self):
        self.state = dict(agent.STATE)
        self.addCleanup(agent.STATE.update, self.state)

    def test_remote_config_does_not_inherit_server_paths(self):
        with patch.object(api, 'agent_on', return_value=True), \
             patch.object(api, 'agent_url', return_value='http://collector:5300'), \
             patch.object(api.db, 'connect', side_effect=lambda: contextlib.nullcontext(None)), \
             patch.object(api.db, 'get_setting', return_value=json.dumps({'k7_dir': '/server/stale'})), \
             patch.object(api, '_agent_call', return_value={'config': {}, 'data_root': '/collector/data'}):
            cfg = api.op_config_get()['config']
        self.assertNotIn('k7_dir', cfg)
        self.assertEqual(cfg['local_data_dir'], '/collector/data')

    def test_unreachable_device_does_not_show_server_config(self):
        with patch.object(api, 'agent_on', return_value=True), \
             patch.object(api, 'agent_url', return_value='http://collector:5300'), \
             patch.object(api.db, 'connect', side_effect=lambda: contextlib.nullcontext(None)), \
             patch.object(api.db, 'get_setting', return_value='{"k7_dir":"/server/stale"}'), \
             patch.object(api, '_dev_registry', return_value={}), \
             patch.object(api, '_agent_call', side_effect=api.AgentDown('offline')):
            cfg = api.op_config_get()['config']
        self.assertNotIn('k7_dir', cfg)
        self.assertFalse(cfg['_agent']['reachable'])

    def test_unselected_device_does_not_reuse_stale_agent_metadata(self):
        stale = {'_agent': {'url': 'http://192.168.31.3:5300', 'reachable': True},
                 'dds_ip': '192.168.31.104'}
        with patch.object(api, 'agent_on', return_value=False), \
             patch.object(api.db, 'connect', side_effect=lambda: contextlib.nullcontext(None)), \
             patch.object(api.db, 'get_setting', return_value=json.dumps(stale)):
            cfg = api.op_config_get()['config']
        self.assertNotIn('_agent', cfg)
        self.assertEqual(cfg['dds_ip'], '192.168.31.104')

    def test_device_config_never_returns_dds_password(self):
        host = {'config': {'dds_ip': '10.0.0.2', 'dds_password': 'secret'},
                'data_root': '/collector/data'}
        with patch.object(api, 'agent_on', return_value=True), \
             patch.object(api, 'agent_url', return_value='http://collector:5300'), \
             patch.object(api.db, 'connect', side_effect=lambda: contextlib.nullcontext(None)), \
             patch.object(api.db, 'get_setting', return_value='{}'), \
             patch.object(api, '_agent_call', return_value=host):
            cfg = api.op_config_get()['config']
        self.assertNotIn('dds_password', cfg)
        self.assertTrue(cfg['dds_password_set'])

    def test_dds_password_is_not_persisted_in_server_database(self):
        connection = contextlib.nullcontext(None)
        with patch.object(api.db, 'connect', return_value=connection), \
             patch.object(api.db, 'set_setting') as save_setting, \
             patch.object(api.db, 'audit'), \
             patch.object(api, 'agent_on', return_value=False):
            api.op_config_save({'dds_ip': '10.21.31.104', 'dds_password': 'secret',
                                '_agent': {'url': 'http://stale:5300'}},
                               {'account': 'admin'})
        stored = json.loads(save_setting.call_args.args[2])
        self.assertNotIn('dds_password', stored)
        self.assertNotIn('_agent', stored)

    def test_execution_uses_collector_paths_and_effective_root(self):
        agent.STATE.update(cfg={'k7_dir': '/home/user', 'local_data_dir': '/stale'},
                           data_root='/mounted/data')
        cfg = agent.merge_cfg({'k7_dir': '/browser/stale', 'robot_service_dir': '/browser/service'})
        self.assertEqual(cfg['k7_dir'], '/home/user')
        self.assertNotIn('robot_service_dir', cfg)
        self.assertEqual(cfg['local_data_dir'], '/mounted/data')

    def test_remote_k7_actions_use_dds_host(self):
        cfg = {'dds_ip': '10.21.31.104', 'dds_username': 'user',
               'dds_password': 'secret', 'k7_dir': '/home/user',
               'k7_lib_path': '/home/user', 'k7_bin': 'K7Master3',
               'k7_sethome_bin': 'SetHome', 'k7_chrt': '90', 'k7_taskset': '6,7'}
        with patch.object(agent.runner, '_dds_run', return_value={
                'code': 0, 'out': 'deepmaster-sethome: completed five-second run; process group stopped'}) as run:
            started = agent.runner.run_t1(cfg)
            stopped = agent.runner.run_tare(cfg)
        self.assertTrue(started['ok'])
        self.assertTrue(stopped['ok'])
        self.assertIn('/home/user', run.call_args_list[0].args[1])
        self.assertIn('K7Master3', run.call_args_list[0].args[1])
        self.assertIn('pgrep -xo K7Master3', run.call_args_list[0].args[1])
        self.assertIn('K7Master3 already running; reusing it',
                      run.call_args_list[0].args[1])
        self.assertIn('236 stable for 5 seconds', run.call_args_list[0].args[1])
        self.assertNotIn('pkill -KILL -x K7Master3', run.call_args_list[0].args[1])
        self.assertIn('chrt unavailable; retrying without realtime priority',
                      run.call_args_list[0].args[1])
        self.assertIn('launch_k7 taskset -c 6,7 ./K7Master3',
                      run.call_args_list[0].args[1])
        syntax = subprocess.run(['bash', '-n'], input=run.call_args_list[0].args[1],
                                text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        self.assertIn('./SetHome', run.call_args_list[1].args[1])
        self.assertIn("[sys.argv[1],", run.call_args_list[1].args[1])

    def test_deploy_always_uses_standard_build_directory(self):
        path = agent.runner._rl_deploy_work({
            'rl_deploy_dir': '/opt/dm-collect/prog/rl_deploy',
            'rl_deploy_build': 'unexpected-value',
        })
        self.assertEqual(path, '/opt/dm-collect/prog/rl_deploy/build')

    def test_remote_actions_require_dds_credentials(self):
        with patch.object(agent.runner, '_dds_run') as run:
            result = agent.runner.run_t1({'k7_dir': '/home/user'})
        self.assertFalse(result['ok'])
        run.assert_not_called()

    def test_ssh_password_is_not_in_command_arguments(self):
        cfg = {'dds_ip': '10.21.31.104', 'dds_username': 'user', 'dds_password': 'secret'}
        completed = type('Completed', (), {'returncode': 0, 'stdout': 'ok'})()
        with patch.object(agent.runner.shutil, 'which', return_value='/usr/bin/sshpass'), \
             patch.object(agent.runner.subprocess, 'run', return_value=completed) as run:
            result = agent.runner._dds_run(cfg, 'echo ok')
        self.assertEqual(result['code'], 0)
        self.assertNotIn('secret', run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['env']['SSHPASS'], 'secret')
        self.assertEqual(run.call_args.kwargs['input'], 'secret\n')
        self.assertIn("sudo -S -p '' -- bash -c", run.call_args.args[0][-1])

    def test_host_endpoint_reports_effective_execution_config(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        agent.STATE.update(cfg={"local_data_dir": "/stale", "dds_password": "secret",
                                "agent_token": "secret"},
                           data_root="/mounted/data")
        server = ThreadingHTTPServer(("127.0.0.1", 0), agent.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:%s/api/host" % server.server_port,
                headers={"X-DM-Token": "secret"})
            with urllib.request.urlopen(request) as response:
                host = json.load(response)["data"]
            self.assertEqual(host["config"]["local_data_dir"], host["data_root"])
            self.assertEqual(host["data_root"], "/mounted/data")
            self.assertNotIn("dds_password", host["config"])
            self.assertTrue(host["config"]["dds_password_set"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_agent_requires_token_for_control_and_file_endpoints(self):
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer
        agent.STATE.update(cfg={"agent_token": "t" * 48}, api_port=0)
        server = ThreadingHTTPServer(("127.0.0.1", 0), agent.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%s" % server.server_port
        try:
            with urllib.request.urlopen(base + "/api/hello") as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(base + "/api/host")
            self.assertEqual(denied.exception.code, 401)
            request = urllib.request.Request(
                base + "/api/host", headers={"X-DM-Token": "t" * 48})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(urllib.error.HTTPError) as denied_files:
                urllib.request.urlopen(base + "/api/files/list")
            self.assertEqual(denied_files.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_invalid_first_assignment_does_not_persist_token(self):
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "host_config.json"
            path.write_text("{}")
            agent.STATE.update(cfg={}, cfg_path=str(path), data_root=str(Path(tmp) / "data"))
            server = ThreadingHTTPServer(("127.0.0.1", 0), agent.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                payload = json.dumps({"port": 1, "agent_token": "x" * 48}).encode()
                request = urllib.request.Request(
                    "http://127.0.0.1:%s/api/assign" % server.server_port,
                    data=payload, headers={"Content-Type": "application/json"})
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(request)
                self.assertEqual(denied.exception.code, 400)
                self.assertNotIn("agent_token", json.loads(path.read_text()))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_device_token_is_persisted_before_first_assignment(self):
        saved = []
        requests = []

        def persist(registry):
            saved.append(json.loads(json.dumps(registry)))

        def agent_call(path, payload=None, **kwargs):
            requests.append((path, payload, kwargs))
            if path == "/api/assign":
                self.assertTrue(saved)
                pending = saved[-1]["device-1"]
                self.assertEqual(pending["port"], 0)
                self.assertEqual(pending["token"], payload["agent_token"])
                self.assertIsNone(kwargs.get("auth_token"))
                return {}
            return {"api_port": 5300, "auth_enabled": True}

        with patch.object(api, "_probe_agent", return_value={
                "device_id": "device-1", "api_port": 0}), \
             patch.object(api, "_dev_registry", return_value={}), \
             patch.object(api, "_dev_save", side_effect=persist), \
             patch.object(api, "_pick_port", return_value=5300), \
             patch.object(api, "_agent_call", side_effect=agent_call), \
             patch.object(api.db, "connect", side_effect=lambda: contextlib.nullcontext(None)), \
             patch.object(api.db, "set_setting"), patch.object(api.db, "audit"), \
             patch.object(api, "agent_url", return_value="http://10.0.0.2:5300"):
            result = api.op_agent_connect({"ip": "10.0.0.2"})

        self.assertEqual(result["port"], 5300)
        self.assertEqual(len(saved), 2)
        self.assertTrue(saved[1]["device-1"]["token"])

    def test_saved_root_is_shared_by_execution_and_file_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'host_config.json'
            path.write_text('{}')
            agent.STATE.update(cfg={}, cfg_path=str(path), data_root=str(Path(tmp) / 'old'))
            agent.save_cfg({'local_data_dir': 'recordings'})
            expected = str(Path(tmp) / 'recordings')
            self.assertEqual(agent.data_root(), expected)
            self.assertEqual(agent.merge_cfg({})['local_data_dir'], expected)
            self.assertEqual(agent.safe_path('session'), str(Path(expected) / 'session'))
            self.assertEqual(json.loads(path.read_text())['local_data_dir'], expected)

    def test_empty_remote_directory_does_not_fall_back_to_server_files(self):
        empty = {'dir': 'session', 'abs_dir': '', 'records': [], 'total': 0, 'remote': True}
        with patch.object(api, 'agent_on', return_value=True), \
             patch.object(api, '_agent_op_records', return_value=empty), \
             patch.object(api, 'resolve_dir') as local_dir:
            result = api.op_records('session', remote=True)
        self.assertEqual(result, empty)
        local_dir.assert_not_called()

    def test_record_listing_preserves_subsecond_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'episode_1.hdf5'
            path.write_bytes(b'one')
            os.utime(path, ns=(1_000_000_000, 1_700_000_000_100_000_000))
            first = api.list_records(tmp)[0]['mtime_ns']
            path.write_bytes(b'two-two')
            os.utime(path, ns=(1_000_000_000, 1_700_000_000_900_000_000))
            second = api.list_records(tmp)[0]['mtime_ns']
        self.assertNotEqual(first, second)

    def test_device_page_only_shows_external_host_settings(self):
        source = (Path(__file__).resolve().parents[1] / 'assets/js/capture.js').read_text()
        block = source.split('var ADV_GROUPS = [', 1)[1].split('var ADV_HINT =', 1)[0]
        shown = re.findall(r'\["([a-z0-9_]+)",\s*"', block)
        self.assertEqual(shown, [
            'dds_ip', 'dds_username', 'dds_password', 'k7_dir',
            'k7_lib_path', 'k7_bin', 'record_fps', 'imu_topic',
        ])
        self.assertIn('["k7_dir", "K7 工作目录"]', block)
        self.assertIn('title: "程序配置"', block)
        self.assertNotRegex(block, r'\["(?:rl_deploy_dir|robot_service_dir|ros_setup|local_data_dir)"')


if __name__ == '__main__':
    unittest.main()

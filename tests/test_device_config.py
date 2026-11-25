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
        host = {'config': {'dds_ip': '10.0.0.2', 'dds_password': 'secret',
                            'agent_token': 'token', 'sudo_pw': 'root-secret'},
                'data_root': '/collector/data'}
        with patch.object(api, 'agent_on', return_value=True), \
             patch.object(api, 'agent_url', return_value='http://collector:5300'), \
             patch.object(api.db, 'connect', side_effect=lambda: contextlib.nullcontext(None)), \
             patch.object(api.db, 'get_setting', return_value='{}'), \
             patch.object(api, '_agent_call', return_value=host):
            cfg = api.op_config_get()['config']
        self.assertNotIn('dds_password', cfg)
        self.assertNotIn('agent_token', cfg)
        self.assertNotIn('sudo_pw', cfg)
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
            self.assertNotIn("agent_token", host["config"])
            self.assertNotIn("sudo_pw", host["config"])
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
        old = dict(agent.STATE)
        agent.STATE.update(cfg={"agent_token": "t" * 48}, api_port=0)
        server = ThreadingHTTPServer(("127.0.0.1", 0), agent.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%s" % server.server_port
        try:
            with urllib.request.urlopen(base + "/api/hello") as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(urllib.error.HTTPError) as denied_camera:
                urllib.request.urlopen(base + "/api/cam/health")
            self.assertEqual(denied_camera.exception.code, 401)
            agent.STATE["camera_token"] = "camera-ticket"
            camera_request = urllib.request.Request(base + "/api/cam/health?token=camera-ticket")
            with patch.object(agent.cam, "health", return_value={"cams": {}}):
                with urllib.request.urlopen(camera_request) as response:
                    self.assertEqual(response.status, 200)
            with self.assertRaises(urllib.error.HTTPError) as denied_webrtc_get:
                urllib.request.urlopen(base + "/api/webrtc/close")
            self.assertEqual(denied_webrtc_get.exception.code, 405)
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
            agent.STATE.clear()
            agent.STATE.update(old)

    def test_pairing_reset_needs_no_old_token_and_is_limited_to_discovery_port(self):
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer

        old = dict(agent.STATE)
        agent.STATE.update(cfg={"agent_token": "old-token"}, api_port=0)
        discovery = ThreadingHTTPServer(("127.0.0.1", 0), agent.Handler)
        agent.STATE["discovery_port"] = discovery.server_port
        api_server = ThreadingHTTPServer(("127.0.0.1", 0), agent.Handler)
        discovery_thread = threading.Thread(target=discovery.serve_forever, daemon=True)
        api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
        discovery_thread.start()
        api_thread.start()
        body = json.dumps({}).encode()
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:%d/api/pairing/reset" % api_server.server_port,
                data=body, headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(request)
            self.assertEqual(denied.exception.code, 403)
            self.assertEqual(agent.STATE["cfg"]["agent_token"], "old-token")

            with patch.object(agent, "save_cfg") as save:
                request = urllib.request.Request(
                    "http://127.0.0.1:%d/api/pairing/reset" % discovery.server_port,
                    data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request) as response:
                    self.assertTrue(json.load(response)["data"]["reset"])
            save.assert_called_once_with({"agent_token": ""}, allow_agent_token=True)
        finally:
            discovery.shutdown()
            discovery.server_close()
            discovery_thread.join()
            api_server.shutdown()
            api_server.server_close()
            api_thread.join()
            agent.STATE.clear()
            agent.STATE.update(old)

    def test_agent_forwards_authenticated_webrtc_signaling(self):
        import threading
        import urllib.error
        import urllib.request
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        received = []

        class Viewer(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body_bytes = self.rfile.read(int(self.headers["Content-Length"]))
                received.append((self.path, self.headers.get("X-DM-WebRTC-Token"),
                                 json.loads(body_bytes)))
                body = json.dumps({"session_id": "s", "sdp": "answer", "type": "answer"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        viewer = ThreadingHTTPServer(("127.0.0.1", 0), Viewer)
        viewer_thread = threading.Thread(target=viewer.serve_forever, daemon=True)
        viewer_thread.start()
        old = dict(agent.STATE)
        agent.STATE.update(cfg={"agent_token": "a" * 48,
                                "cam_viewer_port": viewer.server_port},
                           webrtc_token="w" * 48)
        server = ThreadingHTTPServer(("127.0.0.1", 0), agent.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d/api/webrtc/offer" % server.server_port
        try:
            payload = json.dumps({"session_id": "s", "sdp": "offer"}).encode()
            unauthorized = urllib.request.Request(base, data=payload,
                                                  headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(unauthorized)
            self.assertEqual(denied.exception.code, 401)
            request = urllib.request.Request(
                base, data=payload,
                headers={"Content-Type": "application/json", "X-DM-Token": "a" * 48})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.load(response)["data"]["type"], "answer")
            self.assertEqual(received[0][0], "/webrtc/offer")
            self.assertEqual(received[0][1], "w" * 48)
            self.assertEqual(received[0][2]["sdp"], "offer")
            stats_base = base.rsplit("/", 1)[0] + "/stats"
            stats_request = urllib.request.Request(
                stats_base, data=json.dumps({"session_id": "s"}).encode(),
                headers={"Content-Type": "application/json", "X-DM-Token": "a" * 48})
            with urllib.request.urlopen(stats_request) as response:
                self.assertEqual(json.load(response)["data"]["type"], "answer")
            self.assertEqual(received[1][0], "/webrtc/stats")
            self.assertEqual(received[1][2]["session_id"], "s")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            viewer.shutdown()
            viewer.server_close()
            viewer_thread.join()
            agent.STATE.clear()
            agent.STATE.update(old)

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
            return {"api_port": 5300, "auth_enabled": True,
                    "version": api.APP_VERSION}

        with patch.object(api, "_probe_agent", return_value={
                "device_id": "device-1", "api_port": 0,
                "version": api.APP_VERSION}), \
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

    def test_agent_connect_rejects_version_mismatch_before_pairing_changes(self):
        for agent_version in ("2.2.2", ""):
            with self.subTest(agent_version=agent_version):
                with patch.object(api, "_probe_agent", return_value={
                        "device_id": "device-1", "version": agent_version}), \
                     patch.object(api, "_dev_registry") as registry, \
                     patch.object(api, "_dev_save") as save, \
                     patch.object(api, "_agent_call") as call:
                    with self.assertRaises(api.ApiError) as raised:
                        api.op_agent_connect({"ip": "10.0.0.2"})

                self.assertIn("版本不一致，禁止配对", raised.exception.message)
                self.assertIn("服务端 %s" % api.APP_VERSION, raised.exception.message)
                registry.assert_not_called()
                save.assert_not_called()
                call.assert_not_called()

    def test_agent_connect_explains_saved_token_mismatch_without_rotating_it(self):
        registry = {"device-1": {"ip": "10.0.0.2", "port": 5300,
                                 "token": "saved-token"}}
        with patch.object(api, "_probe_agent", return_value={
                "device_id": "device-1", "auth_enabled": True,
                "version": api.APP_VERSION}), \
             patch.object(api, "_dev_registry", return_value=registry), \
             patch.object(api, "_dev_save") as save, \
             patch.object(api, "_pick_port", return_value=5300), \
             patch.object(api, "_agent_call", side_effect=api.AgentError(
                 "采集端请求未授权", status=401)):
            with self.assertRaises(api.ApiError) as raised:
                api.op_agent_connect({"ip": "10.0.0.2"})

        self.assertIn("服务端保存的设备配对令牌与采集端不一致",
                      raised.exception.message)
        save.assert_not_called()
        self.assertEqual(registry["device-1"]["token"], "saved-token")

    def test_agent_connect_explains_missing_server_pairing_record(self):
        with patch.object(api, "_probe_agent", return_value={
                "device_id": "device-1", "auth_enabled": True,
                "version": api.APP_VERSION}), \
             patch.object(api, "_dev_registry", return_value={}), \
             patch.object(api, "_dev_save") as save, \
             patch.object(api, "_pick_port", return_value=5300), \
             patch.object(api, "_agent_call", side_effect=api.AgentError(
                 "采集端请求未授权", status=401)):
            with self.assertRaises(api.ApiError) as raised:
                api.op_agent_connect({"ip": "10.0.0.2"})

        self.assertIn("服务端缺少该设备的令牌记录", raised.exception.message)
        save.assert_not_called()

    def test_agent_pairing_reset_clears_registry_only_after_agent_accepts(self):
        url = "http://10.0.0.2:5300"
        token = "p" * 48
        registry = {"device-1": {"ip": "10.0.0.2", "port": 5300,
                                 "token": token}}
        calls = []

        def call(path, *args, **kwargs):
            calls.append((path, kwargs))
            if path == "/api/hello":
                return {"device_id": "device-1"}
            return {"reset": True}

        with patch.object(api, "AGENT_URL_ENV", ""), \
             patch.object(api, "agent_url", return_value=url), \
             patch.object(api, "op_agent_scan", return_value={"devices": [
                 {"device_id": "device-1", "ip": "10.0.0.2",
                  "discovery_port": 5111}]}), \
             patch.object(api, "_dev_registry", return_value=registry), \
             patch.object(api, "_agent_call", side_effect=call), \
             patch.object(api.db, "connect", side_effect=lambda: contextlib.nullcontext(None)), \
             patch.object(api.db, "set_setting") as set_setting, \
             patch.object(api.db, "audit") as audit:
            result = api.op_agent_reset_pairing({"account": "admin"})

        self.assertEqual((result["reset_count"], result["failed_count"], result["total"]),
                         (1, 0, 1))
        self.assertEqual(calls[1][0], "/api/pairing/reset")
        self.assertTrue(all(item[1].get("base") == "http://10.0.0.2:5111"
                            for item in calls))
        self.assertNotIn("auth_token", calls[1][1])
        self.assertNotIn("device-1", registry)
        self.assertEqual(set_setting.call_count, 2)
        self.assertEqual(set_setting.call_args_list[1].args[1:], ("agent_url", ""))
        audit.assert_called_once()

    def test_agent_pairing_reset_uses_discovery_port_without_old_token(self):
        url = "http://10.0.0.2:5300"
        registry = {"device-1": {"ip": "10.0.0.2", "port": 5300,
                                 "token": "p" * 48, "discovery_port": 5111}}
        with patch.object(api, "AGENT_URL_ENV", ""), \
             patch.object(api, "agent_url", return_value=url), \
             patch.object(api, "op_agent_scan", return_value={"devices": [
                 {"device_id": "device-1", "ip": "10.0.0.2",
                  "discovery_port": 5111}]}), \
             patch.object(api, "_dev_registry", return_value=registry), \
             patch.object(api, "_agent_call", side_effect=[
                 {"device_id": "device-1"}, {"reset": True}]) as call, \
             patch.object(api.db, "connect", side_effect=lambda: contextlib.nullcontext(None)), \
             patch.object(api.db, "set_setting") as set_setting, \
             patch.object(api.db, "audit"):
            result = api.op_agent_reset_pairing({"account": "admin"})

        self.assertEqual(result["reset_count"], 1)
        self.assertEqual(call.call_args_list[0].kwargs["base"], "http://10.0.0.2:5111")
        self.assertEqual(call.call_args_list[1].kwargs["base"], "http://10.0.0.2:5111")
        self.assertNotIn("device-1", registry)
        self.assertEqual(set_setting.call_count, 2)

    def test_agent_pairing_reset_recovers_when_server_lost_device_token_record(self):
        with patch.object(api, "AGENT_URL_ENV", ""), \
             patch.object(api, "agent_url", return_value="http://10.0.0.2:5300"), \
             patch.object(api, "op_agent_scan", return_value={"devices": [
                 {"device_id": "device-1", "ip": "10.0.0.2",
                  "discovery_port": 5111}]}), \
             patch.object(api, "_dev_registry", return_value={}), \
             patch.object(api, "_agent_call", side_effect=[
                 {"device_id": "device-1"}, {"reset": True}]) as call, \
             patch.object(api.db, "connect", side_effect=lambda: contextlib.nullcontext(None)), \
             patch.object(api.db, "set_setting") as set_setting, \
             patch.object(api.db, "audit"):
            result = api.op_agent_reset_pairing({"account": "admin"})

        self.assertEqual(result["reset_count"], 1)
        self.assertTrue(all(item.kwargs["base"] == "http://10.0.0.2:5111"
                            for item in call.call_args_list))
        self.assertEqual(set_setting.call_count, 2)

    def test_agent_pairing_reset_sends_to_all_scanned_devices_and_keeps_failures(self):
        devices = [
            {"device_id": "device-1", "ip": "10.0.0.3", "discovery_port": 5111},
            {"device_id": "device-2", "ip": "10.0.0.4", "discovery_port": 5111},
        ]
        registry = {
            "device-1": {"ip": "10.0.0.3", "port": 5300, "token": "a"},
            "device-2": {"ip": "10.0.0.4", "port": 5301, "token": "b"},
            "device-3": {"ip": "10.0.0.5", "port": 5302, "token": "c"},
        }
        sent = []

        def call(path, *args, **kwargs):
            base = kwargs["base"]
            sent.append((base, path))
            device_id = "device-1" if base.endswith("10.0.0.3:5111") else "device-2"
            if path == "/api/hello":
                return {"device_id": device_id}
            if device_id == "device-2":
                raise api.AgentError("拒绝重置")
            return {"reset": True}

        with patch.object(api, "AGENT_URL_ENV", ""), \
             patch.object(api, "agent_url", return_value="http://10.0.0.3:5300"), \
             patch.object(api, "op_agent_scan", return_value={"devices": devices}), \
             patch.object(api, "_dev_registry", return_value=registry), \
             patch.object(api, "_agent_call", side_effect=call), \
             patch.object(api.db, "connect", side_effect=lambda: contextlib.nullcontext(None)), \
             patch.object(api.db, "set_setting") as set_setting, \
             patch.object(api.db, "audit"):
            result = api.op_agent_reset_pairing({"account": "admin"})

        self.assertEqual((result["total"], result["reset_count"], result["failed_count"]),
                         (2, 1, 1))
        self.assertEqual(len([item for item in sent if item[1] == "/api/pairing/reset"]), 2)
        self.assertNotIn("device-1", registry)
        self.assertIn("device-2", registry)
        self.assertIn("device-3", registry)
        saved_registry = json.loads(set_setting.call_args_list[0].args[2])
        self.assertEqual(set(saved_registry), {"device-2", "device-3"})
        self.assertEqual(set_setting.call_args_list[1].args[1:], ("agent_url", ""))

    def test_agent_pairing_reset_route_requires_admin(self):
        with patch.object(api.Handler, "_user", side_effect=[
                {"account": "user"}, api.ApiError(403, "权限不足", status=403)]) as user, \
             patch.object(api, "op_agent_reset_pairing") as reset:
            handler = object.__new__(api.Handler)
            handler._body = lambda: {}
            with self.assertRaises(api.ApiError) as raised:
                handler._route_post(["agent", "reset-pairing"])

        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(user.call_count, 2)
        reset.assert_not_called()

    def test_agent_probe_preserves_pairing_state(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"data": {"role": "collector",
                                             "device_id": "device-1",
                                             "auth_enabled": True}}).encode()

        with patch.object(api.socket, "create_connection"), \
             patch.object(api, "urlopen", return_value=Response()):
            result = api._probe_agent("10.0.0.2")

        self.assertTrue(result["auth_enabled"])

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
        with tempfile.TemporaryDirectory() as tmp, patch.object(api, 'INIT_DATA', tmp):
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

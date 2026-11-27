"""控制互斥、配置脱敏及代理错误回归。"""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import agent
import auth
import cam
import runner
import db
import auto_qc
with patch.object(sys, 'argv', ['api.py']):
    import api
    import run_web


class ReviewFixTests(unittest.TestCase):
    def test_server_and_collector_runtime_versions_match_release(self):
        self.assertEqual(api.APP_VERSION, "3.3.2")
        self.assertEqual(agent.VERSION, api.APP_VERSION)

    def test_episode_frames_accept_a_directory_signature(self):
        """审核帧由 <img> 加载，无法带 Authorization 头，只能用 ?v= 目录签名。

        签名与 /api/episodes 返回的 token 一致时放行；不一致时必须退回登录。
        """
        import hashlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "ep")
            os.makedirs(data_dir)
            signature = hashlib.md5(data_dir.encode()).hexdigest()[:12]

            handler = object.__new__(api.Handler)
            handler._user = Mock(side_effect=PermissionError("login required"))
            handler._send = Mock(return_value="frame-bytes")

            with patch.object(api, "resolve_dir", return_value=data_dir), \
                    patch.object(api, "_ep_frame", return_value=b"jpeg") as frame:
                result = api.Handler._route_get(
                    handler, ["episode", "0", "frame", "wrist", "150"],
                    {"dir": [data_dir], "v": [signature]})
                self.assertEqual(result, "frame-bytes")
                frame.assert_called_once_with(0, data_dir, "wrist", 150)
                handler._user.assert_not_called()

    def test_episode_frames_fall_back_to_login_without_valid_signature(self):
        """签名缺失或错误时不得绕过鉴权。"""
        handler = object.__new__(api.Handler)
        handler._user = Mock(side_effect=PermissionError("login required"))
        handler._send = Mock()

        with patch.object(api, "resolve_dir", return_value="/tmp/ep"), \
                patch.object(api, "_ep_frame", return_value=b"jpeg") as frame:
            for ticket in ("", "deadbeef0000"):
                with self.assertRaises(PermissionError):
                    api.Handler._route_get(
                        handler, ["episode", "0", "frame", "wrist", "150"],
                        {"dir": ["/tmp/ep"], "v": [ticket]})
            frame.assert_not_called()
            handler._send.assert_not_called()

    def test_data_reads_require_login_but_health_is_public(self):
        handler = object.__new__(api.Handler)
        handler._user = Mock(side_effect=PermissionError("login required"))
        with self.assertRaises(PermissionError):
            handler._route_get(["dirs"], {})
        handler._user.reset_mock()
        with patch.object(handler, "_ok", return_value={"ok": True}) as health:
            handler._route_get(["health"], {})
        health.assert_called_once()
        handler._user.assert_not_called()

    def test_sqlite_context_rolls_back_on_exception(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
                os.environ, {"DM_DB": str(Path(root) / "test.db")}):
            with db.connect() as conn:
                conn.execute("CREATE TABLE t (value TEXT)")
            with self.assertRaisesRegex(RuntimeError, "abort"):
                with db.connect() as conn:
                    conn.execute("INSERT INTO t(value) VALUES ('discarded')")
                    raise RuntimeError("abort")
            with db.connect() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)

    def test_sqlite_transaction_rolls_back_all_writes(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
                os.environ, {"DM_DB": str(Path(root) / "test.db")}):
            with db.connect() as conn:
                conn.execute("CREATE TABLE t (value TEXT)")
            with self.assertRaisesRegex(RuntimeError, "abort"):
                with db.transaction() as conn:
                    conn.execute("INSERT INTO t(value) VALUES ('first')")
                    conn.execute("INSERT INTO t(value) VALUES ('second')")
                    raise RuntimeError("abort")
            with db.connect() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)

    def test_reset_code_can_only_be_consumed_once_concurrently(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(
                os.environ, {"DM_DB": str(Path(root) / "test.db")}):
            db.migrate()
            auth.create_user("reset-user", "old-password-1")
            issued, data = auth.issue_reset_code("reset-user")
            self.assertTrue(issued)
            barrier = threading.Barrier(2)
            results = []

            def consume(password):
                barrier.wait()
                results.append(auth.consume_reset_code(
                    "reset-user", data["code"], password, "127.0.0.1"))

            workers = [threading.Thread(target=consume, args=("new-password-%d" % i,))
                       for i in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(10)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(sum(1 for ok, _ in results if ok), 1)
            with db.connect() as conn:
                row = conn.execute("SELECT password_hash FROM users WHERE account=?",
                                   ("reset-user",)).fetchone()
            self.assertTrue(any(auth.verify_password("new-password-%d" % i, row[0])
                                for i in range(2)))

    def test_auto_qc_directory_is_reserved_and_empty_records_are_kept(self):
        self.assertTrue(auto_qc._is_reserved_output_folder("auto_qc"))
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "record_data_20261126_1"
            folder.mkdir()
            self.assertEqual(auto_qc._discover_target_folders(root), [])
            self.assertTrue(folder.exists())

    def test_review_data_resolution_stays_inside_configured_dataset_root(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / 'project'
            datasets = project / 'datasets'
            sibling = project / 'other-version' / 'datasets'
            (datasets / 'current').mkdir(parents=True)
            sibling.mkdir(parents=True)
            (datasets / 'outside-link').symlink_to(sibling, target_is_directory=True)
            outside_episode = sibling / 'episode_1_vla_valid.hdf5'
            outside_episode.write_bytes(b'outside')
            (datasets / 'episode_1_vla_valid.hdf5').symlink_to(outside_episode)
            with patch.object(api, 'INIT_DATA', str(datasets)), \
                 patch.object(api, 'PROJECT_DIR', str(project)):
                self.assertEqual(api.resolve_dir('datasets/current'),
                                 str(datasets / 'current'))
                self.assertIsNone(api.resolve_dir(str(sibling)))
                self.assertIsNone(api.resolve_dir('datasets/outside-link'))
                self.assertEqual(api.list_episode_files(str(datasets)), [])

    def test_concurrent_control_is_rejected_and_lock_is_released(self):
        entered, release = threading.Event(), threading.Event()

        @runner.serialized_operation
        def operation():
            entered.set()
            release.wait(3)
            return {'ok': True}

        thread = threading.Thread(target=operation)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertFalse(operation()['ok'])
        finally:
            release.set()
            thread.join(3)
        self.assertTrue(operation()['ok'])

    def test_missing_direct_camera_keeps_old_preview(self):
        result = Mock(returncode=0, stdout='2\n', stderr='')
        with patch.object(agent.os.path, 'exists', return_value=True), \
             patch.object(runner, '_alive', return_value=False), \
             patch.object(agent, '_direct_camera_command', side_effect=lambda x: x), \
             patch.object(agent.subprocess, 'run', return_value=result), \
             patch.object(runner, 'stop') as stop, \
             patch.object(runner, 'spawn') as spawn:
            self.assertFalse(agent.start_cam_viewer('direct')['ok'])
        stop.assert_not_called()
        spawn.assert_not_called()

    def test_live_preview_is_reused(self):
        with patch.dict(agent.STATE, cam_mode='direct'), \
             patch.object(agent.os.path, 'exists', return_value=True), \
             patch.object(runner, '_alive', return_value=True), \
             patch.object(runner, 'spawn') as spawn:
            result = agent.start_cam_viewer('direct')
        self.assertTrue(result['already'])
        spawn.assert_not_called()

    def test_stop_failure_preserves_process_ownership(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        entry = {'proc': process}
        with patch.dict(runner._procs, test_stubborn=entry), \
             patch.object(runner.os, 'getpgid', return_value=123), \
             patch.object(runner.os, 'killpg'), \
             patch.object(runner.time, 'sleep'):
            self.assertFalse(runner.stop('test_stubborn')['ok'])
            self.assertIs(runner._procs['test_stubborn'], entry)

    def test_config_save_response_has_no_credentials(self):
        old = dict(agent.STATE)
        try:
            with tempfile.TemporaryDirectory() as root:
                agent.STATE.update(cfg={'agent_token': 'private', 'sudo_pw': 'secret',
                                        'dds_password': 'secret'},
                                   cfg_path=root + '/config.json')
                result = agent.save_cfg({'local_data_dir': root})
                for field in ('agent_token', 'sudo_pw', 'dds_password'):
                    self.assertNotIn(field, result['config'])
                self.assertTrue(result['config']['dds_password_set'])
                self.assertEqual(agent.STATE['cfg']['agent_token'], 'private')
        finally:
            agent.STATE.update(old)

    def test_concurrent_agent_config_updates_are_atomic_and_protect_paths(self):
        old = dict(agent.STATE)
        try:
            with tempfile.TemporaryDirectory() as root:
                config = Path(root) / 'host_config.json'
                agent.STATE.update(cfg={'local_data_dir': root, 'rl_deploy_dir': '/opt/dm-collect/prog'},
                                   cfg_path=str(config), data_root=root)
                barrier = threading.Barrier(8)

                def update(index):
                    barrier.wait()
                    return agent.save_cfg({'setting_%d' % index: str(index),
                                           'rl_deploy_dir': '/tmp/attacker'})

                threads = [threading.Thread(target=update, args=(i,)) for i in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(5)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                saved = json.loads(config.read_text())
                self.assertEqual(saved['rl_deploy_dir'], '/opt/dm-collect/prog')
                self.assertEqual({saved['setting_%d' % i] for i in range(8)},
                                 {str(i) for i in range(8)})
        finally:
            agent.STATE.update(old)

    def test_failed_process_spawn_closes_log_descriptor(self):
        log = Mock()
        with patch.object(runner, '_log_size', return_value=0), \
             patch.object(runner, '_logfile', return_value=log), \
             patch.object(runner.subprocess, 'Popen', side_effect=OSError('spawn failed')):
            result = runner.spawn('test-log-close', 'test', ['program'])
        self.assertFalse(result['ok'])
        log.close.assert_called_once()

    def test_sethome_is_blocked_while_control_program_runs(self):
        with patch.object(runner, '_dry', return_value=False), \
             patch.object(runner, '_alive', return_value=True), \
             patch.object(runner, 'pgrep_count', return_value=0), \
             patch.object(runner, '_dds_run') as dds:
            result = runner.run_tare({'rl_deploy_bin': 'rl_deploy'})
        self.assertFalse(result['ok'])
        dds.assert_not_called()

    def test_empty_cleanup_never_removes_input_root_or_nonempty_source(self):
        with tempfile.TemporaryDirectory() as root:
            input_root = Path(root) / 'input'
            child = input_root / 'record_data_20261126_1'
            input_root.mkdir()
            child.mkdir()
            (child / 'notes.txt').write_text('preserve')
            self.assertFalse(auto_qc._remove_empty_input_folder(input_root, input_root))
            self.assertFalse(auto_qc._remove_empty_input_folder(child, input_root))
            self.assertTrue(child.exists())
            (child / 'notes.txt').unlink()
            self.assertTrue(auto_qc._remove_empty_input_folder(child, input_root))
            self.assertFalse(child.exists())

    def test_unmanaged_control_is_not_reported_stopped(self):
        with patch.object(runner, '_alive', return_value=False), \
             patch.object(runner, 'pgrep_count', return_value=1), \
             patch.object(runner, 'stop') as stop:
            self.assertFalse(runner.stop_control_only({})['ok'])
        stop.assert_not_called()

    def test_config_get_requires_login(self):
        handler = object.__new__(api.Handler)
        handler._user = Mock(side_effect=PermissionError('login required'))
        with patch.object(api, 'op_config_get') as read:
            with self.assertRaises(PermissionError):
                handler._route_get(['config'], {})
        read.assert_not_called()

    def test_camera_proxy_requires_login(self):
        handler = object.__new__(api.Handler)
        handler._user = Mock(side_effect=PermissionError('login required'))
        with patch.object(handler, '_cam') as camera:
            with self.assertRaises(PermissionError):
                handler._route_get(['cam', 'health'], {})
        camera.assert_not_called()

    def test_direct_camera_fps_mapping(self):
        segment = {'label': 'primary', 'present': True,
                   'measured_fps': 28.5, 'source_fps': 30}
        with patch.object(cam, 'upstream_health', return_value={'segments': [segment]}):
            status = cam.health()['cams']['wrist']
        self.assertEqual(status['fps'], 28.5)
        self.assertEqual(status['source_fps'], 30)

    def test_proxy_errors_are_json_and_control_timeout_is_long_enough(self):
        for session in (None, Mock()):
            handler = object.__new__(run_web.H)
            handler.path = '/api/run/t1'
            handler.headers = {}
            handler.rfile = io.BytesIO()
            handler.wfile = io.BytesIO()
            handler.send_response = Mock()
            handler.send_header = Mock()
            handler.end_headers = Mock()
            failure = RuntimeError('quoted "error"\nnext line')
            if session is not None:
                session.request.side_effect = failure
            with patch.object(run_web, '_SESSION', session), \
                 patch.object(run_web, 'urlopen', side_effect=failure) as request:
                handler._proxy('POST')
            self.assertEqual(json.loads(handler.wfile.getvalue())['message'], str(failure))
            request = session.request if session is not None else request
            self.assertEqual(request.call_args.kwargs['timeout'], 150)


if __name__ == '__main__':
    unittest.main()

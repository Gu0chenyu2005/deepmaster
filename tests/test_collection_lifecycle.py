# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""验证失败采集清理及采集端删除与服务端数据隔离。"""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import agent
with patch.object(sys, 'argv', ['api.py']):
    import api


class CollectionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'datasets'
        self.root.mkdir()
        self.env = patch.dict(os.environ, {'DM_DB': str(Path(self.tmp.name) / 'test.db')})
        self.env.start()
        self.addCleanup(self.env.stop)
        api.db.migrate()
        for key, value in [('INIT_DATA', str(self.root)), ('DEFAULT_DATA', str(self.root)),
                           ('PROJECT_DIR', self.tmp.name), ('ALLOW_ROOTS', [str(self.root)]),
                           ('_DATA_DIR_NAMES', {'datasets'})]:
            p = patch.object(api, key, value)
            p.start()
            self.addCleanup(p.stop)
        state = dict(agent.STATE)
        self.addCleanup(agent.STATE.update, state)
        agent.STATE.update(cfg={'agent_token': 'test-token'}, data_root=str(self.root))

    def start_session(self):
        with patch.object(api, 'agent_on', return_value=True), \
             patch.object(api, 'op_preflight', return_value={'ok': True, 'abs_path': str(self.root / 'run')}), \
             patch.object(api, '_agent_call', return_value={'data_root': '/collector/data'}), \
             patch.object(api, 'upload_forget'):
            return api.op_session_start('', 'run', None)

    def test_remote_session_does_not_create_server_directory(self):
        result = self.start_session()
        self.assertEqual(result['collector_path'], '/collector/data/run')
        self.assertFalse((self.root / 'run').exists())

    def test_config_and_preflight_do_not_create_collector_directory(self):
        target = self.root / 'run'
        cfg = agent.merge_cfg({'run_data_dir': str(target)})
        self.assertEqual(cfg['local_data_dir'], str(target))
        self.assertFalse(target.exists())

    def test_failed_session_ends_and_skips_metadata_and_archive_for_empty_directory(self):
        session = self.start_session()
        with patch.object(api, 'agent_on', return_value=True), \
             patch.object(api, '_agent_call', return_value={'empty': True}) as call, \
             patch.object(api, 'write_collector') as metadata:
            result = api.op_session_end(session['id'], 0, 'error', 'K7 unavailable')
        self.assertTrue(result['empty_removed'])
        self.assertIsNone(result['archive'])
        metadata.assert_not_called()
        self.assertEqual(call.call_args.args[:2], ('/api/files/rmdir-empty', {'dir': 'run'}))
        with api.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT state FROM sessions WHERE id=?',
                                          (session['id'],)).fetchone()['state'], 'error')

    def test_collector_delete_preserves_uploaded_files_records_and_reviews(self):
        target = self.root / 'run'
        target.mkdir()
        record = target / 'episode_1.hdf5'
        record.write_bytes(b'server copy')
        with api.db.connect() as conn:
            conn.execute('INSERT INTO records(dir_path,name,size,mtime,synced_at) VALUES (?,?,?,?,?)',
                         ('datasets/run', record.name, 11, 1, api.db.now()))
            conn.execute('INSERT INTO reviews(dir_path,episode,status,reviewed_at) VALUES (?,?,?,?)',
                         ('datasets/run', 1, 'good', api.db.now()))
        with patch.object(api, 'agent_on', return_value=True), \
             patch.object(api, '_agent_call', return_value={'files': 1, 'bytes': 11}) as call, \
             patch.object(api, 'upload_forget') as forget:
            result = api.op_dir_delete('datasets/run', remote=True)
        self.assertTrue(result['remote'])
        self.assertEqual(record.read_bytes(), b'server copy')
        forget.assert_not_called()
        self.assertEqual(call.call_args.args[0], '/api/files/rmdir')
        with api.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM records').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT status FROM reviews').fetchone()[0], 'good')

    def test_remote_delete_never_falls_back_to_server_when_device_offline(self):
        target = self.root / 'run'
        target.mkdir()
        record = target / 'episode_1.hdf5'
        record.write_bytes(b'server copy')
        for enabled in [False, True]:
            with self.subTest(enabled=enabled), \
                 patch.object(api, 'agent_on', return_value=enabled), \
                 patch.object(api, '_agent_call', side_effect=api.AgentDown('offline')):
                with self.assertRaises(api.ApiError):
                    api.op_dir_delete('datasets/run', remote=True)
                with self.assertRaises(api.ApiError):
                    api.op_record_delete('datasets/run', record.name, remote=True)
                self.assertTrue(record.exists())

    def test_empty_cleanup_endpoint_preserves_all_nonempty_directories(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), agent.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def cleanup(rel):
            req = Request('http://127.0.0.1:%d/api/files/rmdir-empty' % server.server_port,
                          data=json.dumps({'dir': rel}).encode(),
                          headers={'X-DM-Token': 'test-token', 'Content-Type': 'application/json'})
            with urlopen(req) as response:
                return json.load(response)['data']
        empty = self.root / 'empty'
        empty.mkdir()
        self.assertTrue(cleanup('empty')['empty'])
        self.assertFalse(empty.exists())
        self.assertTrue(cleanup('missing')['empty'])
        for name in ['episode_1.hdf5', '.hidden', 'collector.json']:
            full = self.root / name.replace('.', '_')
            full.mkdir()
            (full / name).write_bytes(b'keep')
            self.assertFalse(cleanup(full.name)['empty'])
            self.assertTrue((full / name).exists())
        with self.assertRaises(HTTPError):
            cleanup('')
        self.assertTrue(self.root.exists())


if __name__ == '__main__':
    unittest.main()

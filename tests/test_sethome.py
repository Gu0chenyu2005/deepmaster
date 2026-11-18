# Copyright (c) 2026 deeprobotics
# Author Chenyu Guo
# Date November 2026
"""验证五秒标零及进程组隔离，不访问硬件。"""
import ast
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import runner

DONE = 'deepmaster-sethome: completed five-second run; process group stopped'
CFG = dict(dds_ip='example', dds_username='user', dds_password='secret')


class SetHomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sethome test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def program(self, source):
        path = self.root / 'SetHome'
        path.write_text('#!' + sys.executable + '\n' + source)
        path.chmod(0o755)
        return path

    def execute(self, path):
        return subprocess.run([sys.executable, '-c', runner._SETHOME_CONTROL, str(path)],
                              capture_output=True, text=True, timeout=12)

    def test_five_seconds_stops_owned_group_and_preserves_other_process(self):
        child_pid = self.root / 'child.pid'
        program = self.program(
            'import subprocess,sys,time\n'
            'child=subprocess.Popen([sys.executable,"-c",'
            '"import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"])\n'
            'open(' + repr(str(child_pid)) + ',"w").write(str(child.pid))\n'
            'while True:\n'
            ' print("Joint 0 Pos +0.001",flush=True)\n'
            ' time.sleep(.01)\n')
        other = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'],
                                 start_new_session=True)
        try:
            start = time.monotonic()
            result = self.execute(program)
            elapsed = time.monotonic() - start
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(DONE, result.stdout)
            self.assertGreaterEqual(elapsed, 5)
            self.assertLess(elapsed, 9)
            self.assertIsNone(other.poll())
            stat = Path('/proc') / child_pid.read_text() / 'stat'
            if stat.exists():
                self.assertIn(stat.read_text().rsplit(')', 1)[1].split()[0], ('Z', 'X'))
        finally:
            other.terminate()
            other.wait(timeout=3)

    def test_known_service_warnings_do_not_fail_running_program(self):
        program = self.program('import time\nprint("Failed to stop rl_deploy.service: Unit rl_deploy.service not loaded.",flush=True)\nprint("Failed to stop ecan_master.service: Unit ecan_master.service not loaded.",flush=True)\ntime.sleep(60)\n')
        result = self.execute(program)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(DONE, result.stdout)

    def test_early_exit_is_failure_even_if_exit_code_is_zero(self):
        for code in (0, 7, 137):
            with self.subTest(code=code):
                result = self.execute(self.program('import sys\nsys.exit(%d)\n' % code))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('exited before five seconds', result.stdout)
                self.assertNotIn(DONE, result.stdout)

    def test_missing_binary_is_failure(self):
        result = self.execute(self.root / 'missing')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(DONE, result.stdout)

    def test_ssh_errors_and_unverified_exits_are_not_success(self):
        for code, out in [(255, 'Joint 0 Pos +0.001'), (-1, 'DDS SSH 请求超时'),
                          (124, ''), (137, ''), (0, ''), (1, 'cleanup failed'), (255, DONE)]:
            with self.subTest(code=code, out=out), patch.object(runner, '_dds_run', return_value=dict(code=code, out=out)):
                self.assertFalse(runner.run_tare(CFG)['ok'])
        with patch.object(runner, '_dds_run', return_value=dict(code=0, out=DONE)):
            self.assertTrue(runner.run_tare(CFG)['ok'])

    def test_cleanup_permission_failure_is_not_ignored(self):
        nodes = ast.parse(runner._SETHOME_CONTROL).body
        func = next(n for n in nodes if isinstance(n, ast.FunctionDef) and n.name == 'stop_group')
        namespace = dict(os=os, signal=signal, time=time, group_alive=lambda _: True)
        exec(compile(ast.Module(body=[func], type_ignores=[]), '<cleanup>', 'exec'), namespace)
        proc = type('Process', (), {'pid': 123})()
        with patch.object(os, 'killpg', side_effect=PermissionError('cleanup denied')):
            with self.assertRaises(PermissionError):
                namespace['stop_group'](proc)


if __name__ == '__main__':
    unittest.main()

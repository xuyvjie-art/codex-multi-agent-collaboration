# SPDX-License-Identifier: AGPL-3.0-only
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/coordination_lock.py'

class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.temp.name)
        os.chmod(self.root, 0o700)

    def tearDown(self):
        self.temp.cleanup()

    def cmd(self, *args, check=True):
        p = subprocess.run([sys.executable, str(SCRIPT), '--dir', self.root, *args], text=True, capture_output=True)
        if check and p.returncode:
            self.fail(p.stderr)
        return p

    def status(self):
        return json.loads(self.cmd('status').stdout)

    def acquire(self, ttl='60'):
        return json.loads(self.cmd('acquire', '--owner', 'test', '--resource', 'state', '--ttl', ttl).stdout)

    def recover(self, reason='test recovery', confirm=True):
        digest = self.status()['marker']['sha256']
        return self.cmd('recover', '--sha256', digest, '--reason', reason, *(['--confirm-writer-stopped'] if confirm else []), check=False)

    def test_competing_processes_and_token(self):
        command = [sys.executable, str(SCRIPT), '--dir', self.root, 'acquire', '--owner', 'parallel', '--resource', 'state', '--ttl', '60']
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        results = [(p, *p.communicate()) for p in processes]
        self.assertEqual(sorted(p.returncode for p, _, _ in results), [0, 1])
        winner = json.loads(next(out for p, out, _ in results if p.returncode == 0))
        self.assertNotEqual(self.cmd('release', '--token', 'wrong', check=False).returncode, 0)
        self.assertTrue(self.status()['locked'])
        self.cmd('release', '--token', winner['token'])
        self.assertFalse(self.status()['locked'])
        events = [json.loads(x)['event'] for x in Path(self.root, 'writer.audit.jsonl').read_text().splitlines()]
        self.assertEqual(events, ['acquire', 'release'])

    def test_status_does_not_reveal_release_token(self):
        record = self.acquire()
        output = self.cmd('status').stdout
        status = json.loads(output)
        self.assertNotIn('token', status['marker']['record'])
        self.assertNotIn(record['token'], output)
        self.assertEqual(len(status['marker']['sha256']), 64)
        self.cmd('release', '--token', record['token'])
        self.assertFalse(self.status()['locked'])

    def test_expiry_and_explicit_recovery_audit(self):
        self.acquire('0.01')
        time.sleep(.05)
        self.assertNotEqual(self.cmd('acquire', '--owner', 'new', '--resource', 'state', '--ttl', '1', check=False).returncode, 0)
        self.assertNotEqual(self.recover(confirm=False).returncode, 0)
        self.assertNotEqual(self.recover(reason=' ').returncode, 0)
        self.assertEqual(self.recover(reason='writer verified stopped').returncode, 0)
        events = [json.loads(x) for x in Path(self.root, 'writer.audit.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1]['event'], 'recover')
        self.assertEqual(events[-1]['reason'], 'writer verified stopped')
        self.assertNotIn('token', events[-1]['previous'])

    def test_unexpired_and_changed_digest_refused(self):
        self.acquire()
        digest = self.status()['marker']['sha256']
        self.assertNotEqual(self.recover().returncode, 0)
        self.assertNotEqual(self.cmd('recover', '--sha256', 'a'*64, '--reason', 'x', '--confirm-writer-stopped', check=False).returncode, 0)
        self.assertEqual(self.status()['marker']['sha256'], digest)

    def test_run_exit_and_failure(self):
        p = self.cmd('run', '--owner', 'x', '--resource', 'state', '--ttl', '10', '--', sys.executable, '-c', 'import sys; sys.exit(7)', check=False)
        self.assertEqual(p.returncode, 7, p.stderr)
        self.assertFalse(self.status()['locked'])
        p = self.cmd('run', '--owner', 'x', '--resource', 'state', '--ttl', '10', '--', '/no/such/command', check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse(self.status()['locked'])

    def test_interrupted_holder_leaves_marker_and_recovery(self):
        pidfile = Path(self.root, 'child.pid')
        childcode = "import os,time,sys; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(30)"
        command = [sys.executable, str(SCRIPT), '--dir', self.root, 'run', '--owner', 'x', '--resource', 'state', '--ttl', '.1', '--', sys.executable, '-c', childcode, str(pidfile)]
        p = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        childpid = None
        try:
            for _ in range(300):
                if pidfile.exists() and pidfile.read_text():
                    childpid = int(pidfile.read_text())
                    break
                time.sleep(.01)
            self.assertIsNotNone(childpid)
            os.kill(p.pid, signal.SIGKILL)
            p.wait()
            self.assertTrue(self.status()['locked'])
            # The orphan writer has finished before this simulated confirmation.
            os.killpg(childpid, signal.SIGTERM)
            for _ in range(300):
                try:
                    os.kill(childpid, 0)
                except ProcessLookupError:
                    break
                time.sleep(.01)
            else:
                self.fail('child did not stop')
            time.sleep(.15)
            self.assertEqual(self.recover(reason='child stopped after parent was killed').returncode, 0)
            self.assertFalse(self.status()['locked'])
        finally:
            if p.poll() is None:
                p.kill(); p.wait()
            if childpid:
                try: os.killpg(childpid, signal.SIGKILL)
                except ProcessLookupError: pass

    def test_sigterm_stops_command_and_releases(self):
        pidfile = Path(self.root, 'child.pid')
        childcode = "import os,time,sys; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(30)"
        command = [sys.executable, str(SCRIPT), '--dir', self.root, 'run', '--owner', 'x', '--resource', 'state', '--ttl', '20', '--', sys.executable, '-c', childcode, str(pidfile)]
        p = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(300):
                if pidfile.exists() and pidfile.read_text(): break
                time.sleep(.01)
            else: self.fail('child not started')
            p.send_signal(signal.SIGTERM)
            _, err = p.communicate(timeout=5)
            self.assertEqual(p.returncode, 128 + signal.SIGTERM, err)
            self.assertFalse(self.status()['locked'])
        finally:
            if p.poll() is None: p.kill(); p.wait()

    def test_partial_marker_requires_confirmed_recovery(self):
        Path(self.root, 'writer.lock').write_text('{')
        os.chmod(Path(self.root, 'writer.lock'), 0o600)
        self.assertIsNone(self.status()['marker']['record'])
        self.assertNotEqual(self.recover(confirm=False).returncode, 0)
        self.assertEqual(self.recover(reason='incomplete writer verified stopped').returncode, 0)

    def test_permissions_and_symlinks(self):
        os.chmod(self.root, 0o777)
        self.assertNotEqual(self.cmd('status', check=False).returncode, 0)
        os.chmod(self.root, 0o700)
        outside = Path(self.root, 'outside'); outside.write_text('keep')
        Path(self.root, 'writer.lock').symlink_to(outside)
        self.assertNotEqual(self.cmd('acquire', '--owner', 'x', '--resource', 'x', '--ttl', '1', check=False).returncode, 0)
        self.assertNotEqual(self.cmd('status', check=False).returncode, 0)
        self.assertEqual(outside.read_text(), 'keep')
        self.assertTrue(Path(self.root, 'writer.lock').is_symlink())

    def test_gate_and_audit_symlinks_do_not_modify_target(self):
        target = Path(self.root, 'target')
        target.write_text('protected')
        gate = Path(self.root, '.writer.gate')
        gate.symlink_to(target)
        self.assertNotEqual(self.cmd('acquire', '--owner', 'x', '--resource', 'x', '--ttl', '1', check=False).returncode, 0)
        self.assertEqual(target.read_text(), 'protected')
        gate.unlink()
        Path(self.root, 'writer.audit.jsonl').symlink_to(target)
        self.assertNotEqual(self.cmd('acquire', '--owner', 'x', '--resource', 'x', '--ttl', '1', check=False).returncode, 0)
        self.assertEqual(target.read_text(), 'protected')
        self.assertTrue(self.status()['locked'])

    def test_directory_symlink_rejected(self):
        link = Path(self.root, 'link')
        actual = Path(self.root, 'actual')
        actual.mkdir(mode=0o700)
        link.symlink_to(actual, target_is_directory=True)
        result = subprocess.run([sys.executable, str(SCRIPT), '--dir', str(link), 'status'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)

if __name__ == '__main__':
    unittest.main()

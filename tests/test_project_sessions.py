import importlib.util
import contextlib
import json
import os
import signal
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

import hermes_env  # noqa: F401  (creates the scratch directory)
from project_sessions.executor import Executor, HermesAdapter, birth, lineage, session_members
from project_sessions.registry import Registry

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / 'tests/fake_hermes.py'


class ProjectTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / 'scratch'
        scratch.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.base = Path(self.tmp.name)
        self.home = self.base / 'home'
        self.home.mkdir()
        self.project = self.base / 'repo'
        self.project.mkdir()
        self.a = self.project / 'a'
        self.b = self.project / 'b'
        self.a.mkdir()
        self.b.mkdir()
        self.reg = Registry(self.base / 'data/registry.db', self.home)
        self.pid = self.reg.ensure_project('telegram:bound', 'main')['id']
        self.wid = self.reg.create_workstream(self.pid, 'writing', 'telegram:bound', self.a)['workstream_id']
        self.ex = Executor(self.reg, HermesAdapter(str(FAKE)), timeout=5)
        self.ex.acquire()
        self.counter = 0

    def tearDown(self):
        self.ex.close()
        self.tmp.cleanup()

    def submit(self, text='task A', wid=None, key=None):
        self.counter += 1
        return self.reg.submit(self.pid, wid or self.wid, key or str(self.counter), text, 'save artifact', 'text only', 'telegram:bound')['id']

    def snapshot(self):
        return self.reg.inspect(self.pid, 'telegram:bound')

    def wait(self, task_id, states=('completed', 'needs_input', 'failed', 'cancelled'), seconds=6):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.ex.tick()
            row = next(t for t in self.snapshot()['tasks'] if t['id'] == task_id)
            if row['state'] in states:
                return row
            time.sleep(.03)
        self.fail(f'Timed out: {self.snapshot()}')

    def test_worker_env_drops_launching_profile_runtime_and_pins_workspace_cwd(self):
        # An executor started from a gateway terminal inherited TERMINAL_CWD=~ and
        # another profile's HERMES_SCRATCH_DIR; the worker prompt said cwd=~ and wrote there.
        run_dir = self.base / 'run'
        run_dir.mkdir()
        (run_dir / 'policy.json').write_text(json.dumps({'approval_timeout': 60}))
        task = {'scope': 's', 'input': 'i', 'criteria': 'c', 'authorization': 'a', 'session_id': None, 'cwd': str(self.a)}
        inherited = {'TERMINAL_CWD': '/home/elsewhere', 'TERMINAL_ENV': 'docker', 'MESSAGING_CWD': '/x',
                     'HERMES_SCRATCH_DIR': '/other/profile/cache', 'HERMES_EXEC_ASK': '1', 'HERMES_SESSION_KEY': 'k',
                     'HERMES_HOME': '/other/profile', 'HERMES_MANAGED_DIR': str(self.base / 'no-admin'), 'KEEP_ME': '1',
                     'TMPDIR': '/other/profile/cache/scratch', 'TMP': '/other/profile/cache/scratch'}
        with patch.dict(os.environ, inherited), patch('project_sessions.executor.subprocess.Popen') as popen:
            HermesAdapter('hermes').launch(task, str(self.home), run_dir)
        env = popen.call_args.kwargs['env']
        self.assertEqual(env['TERMINAL_CWD'], str(self.a))
        self.assertEqual(env['HERMES_HOME'], str(self.home))
        self.assertEqual(env['KEEP_ME'], '1')
        self.assertEqual(env['HERMES_MANAGED_DIR'], str(run_dir / 'managed'))
        self.assertEqual({env[k] for k in ('TMPDIR', 'TMP', 'TEMP')}, {str(run_dir / 'tmp')})
        for key in ('TERMINAL_ENV', 'MESSAGING_CWD', 'HERMES_SCRATCH_DIR', 'HERMES_EXEC_ASK', 'HERMES_SESSION_KEY'):
            self.assertNotIn(key, env)

    def test_create_resume_compression_artifact(self):
        first = self.wait(self.submit())
        self.assertEqual(first['state'], 'completed')
        sid = self.snapshot()['workstreams'][0]['session_id']
        self.assertTrue(Path(first['artifact']).exists())
        self.assertEqual(os.stat(first['artifact']).st_mode & 0o777, 0o600)
        second = self.wait(self.submit('task B COMPRESS'))
        self.assertIn('prior_turns=1', second['result'])
        newest = self.snapshot()['workstreams'][0]['session_id']
        self.assertNotEqual(newest, sid)
        run = self.snapshot()['runs'][-1]
        self.assertEqual(run['resumed_from'], sid)
        self.assertEqual(json.loads(run['lineage'])[1]['session_id'], sid)
        third = self.wait(self.submit('task C'))
        self.assertIn('prior_turns=2', third['result'])
        self.assertEqual(self.snapshot()['runs'][-1]['resumed_from'], newest)

    def test_parallel_and_busy_serial(self):
        wid2 = self.reg.create_workstream(self.pid, 'independent writing', 'telegram:bound', self.b)['workstream_id']
        t1 = self.submit('SLOW A')
        t2 = self.submit('SLOW B', wid2)
        t3 = self.submit('queued requirement C')
        self.ex.tick()
        tasks = {t['id']: t for t in self.snapshot()['tasks']}
        self.assertEqual(tasks[t1]['state'], 'running')
        self.assertEqual(tasks[t2]['state'], 'running')
        self.assertEqual(tasks[t3]['state'], 'queued')
        self.wait(t3)
        self.wait(t2)
        self.assertIn('prior_turns=1', next(t['result'] for t in self.snapshot()['tasks'] if t['id'] == t3))
        # Actual subprocess execution overlaps, not merely quick submissions.
        runs = self.snapshot()['runs']
        r1 = next(r for r in runs if r['task_id'] == t1)
        r2 = next(r for r in runs if r['task_id'] == t2)
        r3 = next(r for r in runs if r['task_id'] == t3)
        self.assertLess(r2['started'], r1['ended'])
        self.assertGreaterEqual(r3['started'], r1['ended'])

    def test_overlapping_resource_lock(self):
        wid2 = self.reg.create_workstream(self.pid, 'overlapping', 'telegram:bound', self.a)['workstream_id']
        t1, t2 = self.submit('SLOW'), self.submit('next', wid2)
        self.ex.tick()
        self.assertEqual(next(t['state'] for t in self.snapshot()['tasks'] if t['id'] == t2), 'queued')
        self.wait(t2)
        runs = self.snapshot()['runs']
        self.assertGreaterEqual(runs[1]['started'], runs[0]['ended'])

    def test_durable_outbox_acceptance_retry_ack(self):
        self.wait(self.submit())
        calls = []
        def inject(content, **kw):
            calls.append((content, kw))
            return len(calls) > 1
        now = time.time()
        self.assertFalse(self.reg.dispatch(inject, now=now))
        self.assertTrue(self.reg.dispatch(inject, now=now + 11))
        event = self.snapshot()['events'][0]
        self.assertEqual(event['state'], 'accepted')
        self.assertIsNone(event['acknowledged'])
        reopened = Registry(self.reg.path, self.home)
        self.assertFalse(reopened.dispatch(inject, now=now + 312))
        self.assertEqual(len(calls), 2)
        pending = reopened.index(self.pid, 'telegram:bound')['events'][0]
        self.assertEqual(pending['state'], 'pending')
        self.assertEqual(pending['delivery_state'], 'accepted')
        self.assertEqual(calls[0][1]['session_key'], 'telegram:bound')
        self.assertEqual(json.loads(calls[0][0].split('\n', 1)[1])['id'], event['id'])
        self.assertFalse(reopened.acknowledge(self.pid, event['id'], 'telegram:bound')['duplicate'])
        self.assertTrue(reopened.acknowledge(self.pid, event['id'], 'telegram:bound')['duplicate'])
        self.assertFalse(reopened.dispatch(inject, now=now + 1000))
        self.assertEqual(len(self.snapshot()['tasks']), 1)

    def test_failed_injection_backoff_exception_and_cap(self):
        self.wait(self.submit())
        now = time.time()
        calls = []
        def fail(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('No live host')
            return False
        for delay in (10, 20, 40, 80, 160, 300, 300):
            try:
                self.assertFalse(self.reg.dispatch(fail, now=now))
            except RuntimeError:
                pass
            event = self.snapshot()['events'][0]
            self.assertAlmostEqual(event['next_attempt'], now + delay)
            self.assertFalse(self.reg.dispatch(fail, now=now + delay - 1))
            now += delay
        self.assertEqual(len(calls), 7)

    def test_runtime_approval_wait_excluded_from_run_timeout(self):
        self.ex.timeout = .3
        tid = self.submit('RUNTIME_APPROVAL')
        request = self.approval_wait(tid)
        time.sleep(.5)
        self.assertEqual(self.snapshot()['tasks'][0]['state'], 'running')
        self.reg.capture_approval_vote('telegram:bound', 'msg', 'approve ' + request['id'])
        self.reg.decide_approval(self.pid, request['id'], 'once', 'telegram:bound')
        self.assertEqual(self.wait(tid)['state'], 'completed')

    def test_concurrent_dispatch_claim(self):
        self.wait(self.submit())
        entered, release = threading.Event(), threading.Event()
        def inject(*a, **kw):
            entered.set()
            release.wait(2)
            return True
        thread = threading.Thread(target=lambda: self.reg.dispatch(inject))
        thread.start()
        self.assertTrue(entered.wait(1))
        self.assertFalse(self.reg.dispatch(lambda *a, **kw: self.fail('Duplicate concurrent injection')))
        release.set()
        thread.join()

    def test_cancel_verifies_process_stop(self):
        tid = self.submit('HANG')
        self.ex.tick()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not self.snapshot()['runs'][0]['pid']:
            time.sleep(.01)
        pid = self.snapshot()['runs'][0]['pid']
        response = self.reg.cancel(self.pid, tid, 'telegram:bound')
        self.assertEqual(response['state'], 'cancel_requested')
        with self.reg.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM resource_locks').fetchone()[0], 1)
        self.assertEqual(self.wait(tid)['state'], 'cancelled')
        self.assertIsNone(birth(pid))
        with self.reg.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM resource_locks').fetchone()[0], 0)

    def test_needs_input_and_output_secret_rejection(self):
        self.assertEqual(self.wait(self.submit('ASK_INPUT'))['state'], 'needs_input')
        row = self.wait(self.submit('LEAK_OUTPUT'))
        self.assertEqual(row['state'], 'needs_input')
        self.assertIsNone(row['artifact'])
        self.assertNotIn('exampleCredentialRejected', self.reg.path.read_bytes().decode('latin1'))

    def test_idempotency_and_wrong_profile_route_root(self):
        tid = self.submit(key='stable')
        self.assertEqual(self.submit(key='stable'), tid)
        with self.assertRaises(ValueError):
            self.submit('different', key='stable')
        with self.assertRaises(ValueError):
            self.reg.inspect(self.pid, 'telegram:other')
        with self.assertRaises(ValueError):
            Registry(self.reg.path, self.base / 'other-home')
        link = self.project / 'escape'
        link.symlink_to(self.base, target_is_directory=True)
        wid = self.reg.create_workstream(self.pid, 'outside workspace', 'telegram:bound', link)['workstream_id']
        self.assertEqual(self.reg.card(self.pid, wid, 'telegram:bound')['workspace'], str(self.base))
        with self.assertRaises(ValueError):
            self.submit('password=exampleCredentialRejected')

    def test_executor_singleton_and_restart_no_replay(self):
        other = Executor(self.reg, HermesAdapter(str(FAKE)))
        try:
            with self.assertRaises(BlockingIOError):
                other.acquire()
        finally:
            other.close()
        completed = self.wait(self.submit())
        original_sid = self.snapshot()['workstreams'][0]['session_id']
        interrupted = self.submit('interrupted')
        task = self.reg.claim()
        self.reg.attach(task['run_id'], 99999999, 'old-boot:1')
        self.ex.reconcile()
        self.assertEqual(next(t['state'] for t in self.snapshot()['tasks'] if t['id'] == interrupted), 'interrupted')
        self.ex.tick()
        self.assertEqual(len(self.snapshot()['runs']), 2)
        self.ex.close()
        self.ex = Executor(self.reg, HermesAdapter(str(FAKE)), timeout=5)
        self.ex.acquire()
        self.ex.reconcile()
        next_task = self.wait(self.submit('explicit continuation'))
        self.assertIn('prior_turns=1', next_task['result'])
        self.assertEqual(self.snapshot()['runs'][-1]['resumed_from'], original_sid)

    def test_reconcile_stops_proven_child_and_recovers_unrecorded_attempt(self):
        tid=self.submit()
        task=self.reg.claim()
        proc=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True)
        try:
            self.reg.attach(task['run_id'],proc.pid,birth(proc.pid))
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                self.ex.reconcile()
                if self.snapshot()['tasks'][0]['state']=='interrupted':break
                time.sleep(.02)
            self.assertEqual(self.snapshot()['tasks'][0]['state'],'interrupted')
            with self.reg.connection() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM resource_locks').fetchone()[0],0)
            self.ex.reconcile()
            self.assertEqual(sum(e['kind']=='interrupted' for e in self.snapshot()['events']),1)
        finally:
            proc.terminate();proc.wait()
        self.reg.cancel(self.pid,tid,'telegram:bound')
        self.submit('unrecorded')
        self.reg.claim()
        self.ex.reconcile()
        self.assertEqual(self.snapshot()['tasks'][-1]['state'],'interrupted')

    def test_tools_enabled_real_file_effect(self):
        row = self.wait(self.submit('REAL_WORK'))
        self.assertEqual(row['state'], 'completed')
        self.assertEqual((self.a / 'worker-output.txt').read_text(), 'real fake-worker filesystem effect')

    def test_outside_write_has_full_profile_permissions(self):
        self.assertEqual(self.wait(self.submit('OUTSIDE_WRITE'))['state'], 'completed')
        self.assertEqual((self.project / 'outside-file.txt').read_text(), 'forbidden')

    def approval_wait(self, task):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.ex.tick()
            requests = [r for r in self.snapshot()['approval_requests'] if r['task_id'] == task]
            if requests:
                return requests[0]
            time.sleep(.03)
        self.fail('No approval request')

    def test_runtime_approval_authenticated_once_same_run(self):
        tid = self.submit('RUNTIME_APPROVAL')
        request = self.approval_wait(tid)
        with self.assertRaises(ValueError):
            self.reg.decide_approval(self.pid, request['id'], 'once', 'telegram:bound')
        self.assertFalse(self.reg.capture_approval_vote('telegram:other', 'msg', 'approve ' + request['id']))
        self.assertTrue(self.reg.capture_approval_vote('telegram:bound', 'msg', 'approve ' + request['id']))
        self.reg.decide_approval(self.pid, request['id'], 'once', 'telegram:bound')
        self.assertEqual(self.wait(tid)['state'], 'completed')
        self.assertEqual(len(self.snapshot()['runs']), 1)
        self.assertEqual((self.a / 'approved.txt').read_text(), 'same run')
        self.assertEqual(self.reg.poll_approval(request['id']), 'deny')

    def test_runtime_denial_and_timeout_reported(self):
        self.ex.approval_timeout = .15
        first = self.submit('RUNTIME_APPROVAL')
        self.assertEqual(self.wait(first)['state'], 'needs_input')
        self.assertEqual(self.snapshot()['approval_requests'][0]['state'], 'timed_out')
        self.ex.approval_timeout = 10
        second = self.submit('RUNTIME_APPROVAL')
        request = self.approval_wait(second)
        self.reg.capture_approval_vote('telegram:bound', 'msg', 'deny ' + request['id'])
        self.reg.decide_approval(self.pid, request['id'], 'deny', 'telegram:bound')
        self.assertEqual(self.wait(second)['state'], 'needs_input')
        self.assertFalse((self.a / 'approved.txt').exists())
        self.assertTrue(self.snapshot()['blocked_actions'])

    def test_timely_decision_consumed_after_deadline(self):
        self.submit()
        task = self.reg.claim()
        approval = self.reg.request_approval(task['run_id'], 'protected action', 'native consent', 10)
        self.reg.capture_approval_vote('telegram:bound', 'msg', 'approve ' + approval)
        self.reg.decide_approval(self.pid, approval, 'once', 'telegram:bound')
        with self.reg.connection() as db:
            db.execute('UPDATE approval_requests SET deadline=? WHERE id=?', (time.time() - 1, approval))
        self.assertEqual(self.reg.poll_approval(approval), 'once')
        self.assertEqual(self.reg.poll_approval(approval), 'deny')
        self.reg.finish(task['run_id'], 'completed')

    def test_cancel_during_approval_wait(self):
        tid = self.submit('RUNTIME_APPROVAL')
        self.approval_wait(tid)
        self.reg.cancel(self.pid, tid, 'telegram:bound')
        self.assertEqual(self.wait(tid)['state'], 'cancelled')
        self.assertFalse((self.a / 'approved.txt').exists())
        self.assertEqual(self.snapshot()['approval_requests'][0]['state'], 'cancelled')
        self.assertFalse(any(e['kind'] == 'approval_requested' for e in self.reg.index(self.pid, 'telegram:bound')['events']))

    def test_overlapping_workspaces_coordinate(self):
        wid = self.reg.create_workstream(self.pid, 'parent workspace', 'telegram:bound', self.project)['workstream_id']
        first = self.submit('SLOW', wid)
        second = self.submit('after parent unlock')
        self.ex.tick()
        self.assertEqual(next(t['state'] for t in self.snapshot()['tasks'] if t['id'] == second), 'queued')
        self.wait(second)

    def test_executor_process_crash_restart_preserves_history_no_replay(self):
        self.wait(self.submit('first completed turn'))
        self.ex.close()
        interrupted = self.submit('HANG interrupted turn')
        command = [sys.executable, '-m', 'project_sessions.executor', '--home', str(self.home),
                   '--db', str(self.reg.path), '--hermes', str(FAKE),
                   '--workers', '1', '--timeout', '5']
        service = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child = None
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                runs = self.snapshot()['runs']
                run = next((r for r in runs if r['task_id'] == interrupted), None)
                if run and run['pid'] and run['session_id']:
                    child = run['pid']
                    break
                time.sleep(.02)
            self.assertIsNotNone(child)
            service.kill()
            service.wait()
            self.assertTrue(birth(child))
            os.killpg(child, signal.SIGTERM)
            deadline = time.monotonic() + 2
            while birth(child) and time.monotonic() < deadline:
                time.sleep(.02)
            service = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                task = next(t for t in self.snapshot()['tasks'] if t['id'] == interrupted)
                if task['state'] == 'interrupted':
                    break
                time.sleep(.02)
            self.assertEqual(task['state'], 'interrupted')
            explicit = self.submit('explicit resumed turn')
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                task = next(t for t in self.snapshot()['tasks'] if t['id'] == explicit)
                if task['state'] == 'completed':
                    break
                time.sleep(.02)
            self.assertEqual(task['state'], 'completed')
            self.assertIn('prior_turns=2', task['result'])
            self.assertEqual(len(self.snapshot()['runs']), 3)
        finally:
            if service.poll() is None:
                service.terminate()
                service.wait(timeout=7)
            if child and birth(child):
                os.killpg(child, signal.SIGKILL)
            self.ex = Executor(self.reg, HermesAdapter(str(FAKE)), timeout=5)
            self.ex.acquire()

    def test_timeout_no_success_or_automatic_replay(self):
        self.ex.timeout = .15
        tid = self.submit('HANG')
        row = self.wait(tid)
        self.assertEqual(row['state'], 'needs_input')
        snapshot = self.snapshot()
        message = snapshot['events'][0]['payload']
        self.assertIn('Timed out after 0.15 s', message)
        self.assertIn('history and workspace kept', message)
        self.assertIn('follow-up', message)
        self.assertIn('Timed out after 0.15 s', snapshot['runs'][0]['error'])
        self.assertTrue(snapshot['workstreams'][0]['session_id'])
        self.assertIsNone(birth(snapshot['runs'][0]['pid']))
        self.ex.tick()
        self.assertEqual(len(self.snapshot()['runs']), 1)

    def test_init_only_exit_is_not_completion_and_preserves_identity(self):
        row = self.wait(self.submit('INIT_ONLY'))
        self.assertEqual(row['state'], 'failed')
        self.assertIsNone(row['artifact'])
        self.assertTrue(self.snapshot()['workstreams'][0]['session_id'])
        next_row = self.wait(self.submit('explicit next turn'))
        self.assertIn('prior_turns=1', next_row['result'])

    def test_bad_protocol_or_missing_guard_not_success(self):
        self.assertEqual(self.wait(self.submit('BAD_PROTOCOL'))['state'], 'needs_input')
        self.assertEqual(self.wait(self.submit('MISSING_GUARD'))['state'], 'needs_input')
        self.assertEqual(self.wait(self.submit('STATUS_FLOOD'))['state'], 'needs_input')

    def test_run_tmpdir_removed_after_completion_and_cancellation(self):
        # A killed worker left Hermes's terminal snapshot, containing the profile's
        # provider key, in runs/<id>/tmp; the executor must not accumulate such copies.
        def run_tmps():
            return list((self.reg.path.parent / 'runs').glob('*/tmp'))
        def gone():
            deadline = time.monotonic() + 3
            while run_tmps() and time.monotonic() < deadline:
                time.sleep(.03)
            return not run_tmps()
        self.assertEqual(self.wait(self.submit('TMP_SNAPSHOT'))['state'], 'completed')
        self.assertTrue(gone())
        tid = self.submit('TMP_SNAPSHOT HANG')
        self.wait(tid, states=('running',))
        deadline = time.monotonic() + 3
        while not list((self.reg.path.parent / 'runs').glob('*/tmp/hermes-snap-fake.sh')) and time.monotonic() < deadline:
            time.sleep(.03)
        self.reg.cancel(self.pid, tid, 'telegram:bound')
        self.assertEqual(self.wait(tid)['state'], 'cancelled')
        self.assertTrue(gone())

    def test_same_session_helper_in_own_group_is_stopped_before_unlock_and_tmp_removal(self):
        # Hermes shell hooks spawn Popen(process_group=0) helpers that may outlive the
        # hook. Such a helper leaves the worker's process group but stays in its session, so
        # neither group_alive nor the recorded terminal sessions saw it: the lock was released
        # and TMPDIR deleted under a live process.
        tid = self.submit('SAME_SESSION_HELPER')
        helper = None
        try:
            self.assertEqual(self.wait(tid)['state'], 'completed')
            run = next(r for r in self.snapshot()['runs'] if r['task_id'] == tid)
            helper = int((self.reg.path.parent / 'runs' / run['id'] / 'helper.pid').read_text())
            alive = birth(helper) is not None
            self.assertFalse(alive, 'same-session helper survived the run and its lock release')
            deadline = time.monotonic() + 3
            while (self.reg.path.parent / 'runs' / run['id'] / 'tmp').exists() and time.monotonic() < deadline:
                time.sleep(.03)
            self.assertFalse((self.reg.path.parent / 'runs' / run['id'] / 'tmp').exists())
        finally:
            if helper is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(helper, signal.SIGKILL)

    def test_runs_directory_is_private_regardless_of_umask(self):
        old = os.umask(0o002)
        try:
            self.assertEqual(self.wait(self.submit('task'))['state'], 'completed')
        finally:
            os.umask(old)
        self.assertEqual((self.reg.path.parent / 'runs').stat().st_mode & 0o777, 0o700)

    def test_host_status_line_on_stdout_does_not_kill_worker(self):
        # Auto-compression prints "  ⟳ compacting context…" to stdout even in
        # stream-json mode; the worker must survive and its result envelope decides.
        row = self.wait(self.submit('STATUS_LINE'))
        self.assertEqual(row['state'], 'completed')
        self.assertIn('session=', row['result'])

    def test_cancel_stops_terminal_command_in_its_own_session(self):
        tid = self.submit('DETACHED_TERMINAL')
        self.ex.tick()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not list((self.reg.path.parent / 'runs').glob('*/sessions')):
            time.sleep(.02)
        self.reg.cancel(self.pid, tid, 'telegram:bound')
        self.assertEqual(self.wait(tid)['state'], 'cancelled')
        time.sleep(2)
        self.assertFalse((self.a / 'late.txt').exists(), 'terminal command outlived verified cancellation')

    def test_cancel_stops_setsid_descendant_outside_terminal_wrapper(self):
        # Hermes's execute_code kernel (tools/code_kernel.py) and process registry start their own
        # sessions without passing through the terminal tracking launcher.
        tid = self.submit('DETACHED_KERNEL')
        self.ex.tick()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not (self.a / 'kernel-started').exists():
            time.sleep(.02)
        time.sleep(.6)
        self.reg.cancel(self.pid, tid, 'telegram:bound')
        self.assertEqual(self.wait(tid)['state'], 'cancelled')
        time.sleep(2)
        self.assertFalse((self.a / 'late-kernel.txt').exists(), 'setsid descendant outlived verified cancellation')

    def test_reconcile_stops_surviving_terminal_session_before_unlock(self):
        self.submit()
        task = self.reg.claim()
        self.reg.attach(task['run_id'], 99999999, 'old-boot:1')
        run_dir = self.reg.path.parent / 'runs' / task['run_id']
        run_dir.mkdir(parents=True)
        orphan = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
        try:
            start = Path(f'/proc/{orphan.pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
            (run_dir / 'sessions').write_text(f'{orphan.pid} {start}\n')
            self.ex.reconcile()
            self.assertEqual(self.snapshot()['tasks'][0]['state'], 'interrupted')
            self.assertIsNone(birth(orphan.pid))
            (run_dir / 'sessions').write_text(f'{orphan.pid} 1\n')  # reused session ID: not ours
            self.assertEqual(session_members(run_dir), [])
        finally:
            orphan.kill()
            orphan.wait()

    def test_unguarded_tool_use_stops_worker_before_effect(self):
        row = self.wait(self.submit('UNGUARDED_TOOL'))
        self.assertEqual(row['state'], 'needs_input')
        time.sleep(1.2)
        self.assertFalse((self.a / 'unguarded.txt').exists())

    def test_vanished_workspace_fails_task_without_stopping_executor(self):
        wid2 = self.reg.create_workstream(self.pid, 'vanishing', 'telegram:bound', self.b)['workstream_id']
        lost = self.submit('never launched', wid2)
        self.b.rmdir()
        healthy = self.submit('independent')
        self.assertEqual(self.wait(lost)['state'], 'failed')
        self.assertEqual(self.wait(healthy)['state'], 'completed')
        event = next(e for e in self.snapshot()['events'] if e['task_id'] == lost)
        self.assertEqual(event['kind'], 'failed')
        with self.reg.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM resource_locks').fetchone()[0], 0)

    def test_concurrent_acknowledge_reports_one_first_claim(self):
        self.wait(self.submit())
        event_id = self.snapshot()['events'][0]['id']
        other = Registry(self.reg.path, self.home)
        original = Registry.connection
        interleaved = []

        @contextlib.contextmanager
        def connection(registry):
            with original(registry) as db:
                if registry is self.reg:
                    class Interleave:
                        def execute(_, sql, *params):
                            cursor = db.execute(sql, *params)
                            if sql.startswith('SELECT * FROM outbox') and not interleaved:
                                interleaved.append(other.acknowledge(self.pid, event_id, 'telegram:bound'))
                            return cursor
                    yield Interleave()
                else:
                    yield db
        with patch.object(Registry, 'connection', connection):
            first = self.reg.acknowledge(self.pid, event_id, 'telegram:bound')
        self.assertFalse(interleaved[0]['duplicate'])
        self.assertTrue(first['duplicate'])

    def test_denied_action_keeps_worker_explanation(self):
        row = self.wait(self.submit('DENY_ACTION'))
        self.assertEqual(row['state'], 'needs_input')
        self.assertIn('Artifact', row['result'])

    def test_reconcile_survives_unpersistable_action_record(self):
        self.submit('interrupted')
        task = self.reg.claim()
        self.reg.attach(task['run_id'], 99999999, 'old-boot:1')
        run_dir = self.reg.path.parent / 'runs' / task['run_id']
        run_dir.mkdir(parents=True)
        (run_dir / 'blocked-actions.json').write_text(json.dumps([{'action_id': 'b' * 32, 'fingerprint': 'f', 'tool': 'terminal',
                                                                    'context': {'command': 'export password=fixture'}, 'reason': 'denied'}]))
        self.ex.reconcile()
        self.assertEqual(self.snapshot()['tasks'][0]['state'], 'interrupted')

    def test_queued_cancel_never_launches(self):
        tid = self.submit()
        self.assertEqual(self.reg.cancel(self.pid, tid, 'telegram:bound')['state'], 'cancelled')
        self.ex.tick()
        self.assertEqual(self.snapshot()['runs'], [])

    def test_parent_directory_conflicts_and_compact_index(self):
        parent = self.reg.create_workstream(self.pid, 'parent workspace', 'telegram:bound', self.project)['workstream_id']
        t1 = self.submit('SLOW')
        t2 = self.submit('parent follows', parent)
        self.ex.tick()
        self.assertEqual(next(t['state'] for t in self.snapshot()['tasks'] if t['id'] == t2), 'queued')
        self.wait(t2)
        index = self.reg.index(self.pid, 'telegram:bound')
        self.assertNotIn('input', index['tasks'][0])
        self.assertNotIn('result', index['tasks'][0])
        self.assertEqual(self.reg.result(self.pid, t1, 'telegram:bound')['state'], 'completed')
        with self.assertRaises(ValueError):
            self.reg.result(self.pid, 'unknown', 'telegram:bound')

    def test_concurrent_claim_never_double_starts(self):
        self.submit()
        claimed = []
        def claim():
            claimed.append(self.reg.claim())
        threads = [threading.Thread(target=claim) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(c is not None for c in claimed), 1)
        task = next(c for c in claimed if c)
        self.reg.attach(task['run_id'], 99999999, 'nonexistent:1')
        self.ex.reconcile()

    def test_lineage_rejects_branch_and_missing_parent(self):
        self.wait(self.submit())
        sid = self.snapshot()['workstreams'][0]['session_id']
        with contextlib.closing(sqlite3.connect(self.home / 'state.db')) as db, db:
            db.execute('INSERT INTO sessions VALUES(?,?,NULL,?)', ('branch', sid, json.dumps({'_branched_from': sid})))
        with self.assertRaises(ValueError):
            lineage(self.home, 'branch', sid)
        with self.assertRaises(ValueError):
            lineage(self.home, sid, 'unrelated')
        with contextlib.closing(sqlite3.connect(self.home / 'state.db')) as db, db:
            db.execute("UPDATE sessions SET end_reason='session_reset' WHERE id=?", (sid,))
            db.execute('INSERT INTO sessions VALUES(?,?,NULL,?)', ('reset-child', sid, '{}'))
        with self.assertRaises(ValueError):
            lineage(self.home, 'reset-child', sid)


class PluginTests(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location('fixture_plugin', ROOT / 'plugin/project-sessions/__init__.py', submodule_search_locations=[str(ROOT / 'plugin/project-sessions')])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_worker_guard_no_orchestrator_tools(self):
        scratch = ROOT / 'scratch'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            hooks = {}
            ctx = types.SimpleNamespace(register_hook=lambda name, fn: hooks.update({name: fn}), register_middleware=lambda name, fn: hooks.update({name: fn}))
            (Path(directory) / 'policy.json').write_text(json.dumps({'workspace': directory, 'writable_roots': [directory]}))
            native = {
                'tools.approval_detection': types.SimpleNamespace(detect_dangerous_command=lambda command: (True, 'dangerous', 'unsafe command'), detect_hardline_command=lambda command: (False, '')),
                'tools.approval_context': types.SimpleNamespace(_get_single_query_approval_mode=lambda: 'deny', _get_approval_mode=lambda: 'manual', _normalize_approval_mode=lambda value:value),
                'tools.approval': types.SimpleNamespace(_yolo_active=lambda: False),
                'hermes_cli.config': types.SimpleNamespace(load_config_readonly=lambda: {'approvals': {'single_query_mode': 'deny'}, 'command_allowlist': []}),
                'agent.redact': types.SimpleNamespace(redact_sensitive_text=lambda value, **kw: value),
            }
            with patch.dict(os.environ, {'PROJECT_SESSIONS_WORKER': directory}), patch.dict(sys.modules, native):
                self.load().register(ctx)
                self.assertTrue((Path(directory) / 'guard-ready').exists())
                self.assertEqual(hooks['pre_tool_call'](tool_name='terminal', args={'command': 'unsafe'})['action'], 'modify')
                self.assertIn('tool_execution', hooks)
                self.assertFalse((Path(directory) / 'needs-input').exists())

    def test_non_gateway_process_never_consumes_delivery_attempts(self):
        # Interactive CLI, cron and other non-gateway processes of the profile load the plugin too.
        # They cannot deliver, so they must not burn attempts or push the gateway's backoff.
        scratch = ROOT / 'scratch'
        scratch.mkdir(exist_ok=True)
        for manager in (types.SimpleNamespace(_cli_ref=object(), has_gateway_message_injector=True),
                        types.SimpleNamespace(_cli_ref=None, has_gateway_message_injector=False)):
            with tempfile.TemporaryDirectory(dir=scratch) as directory:
                base = Path(directory)
                project = base / 'repo'
                project.mkdir()
                reg = Registry(base / 'home/plugin-data/project-sessions/registry.db', base / 'home')
                pid = reg.ensure_project('telegram:main', 'main')['id']
                wid = reg.create_workstream(pid, 'events', 'telegram:main', project)['workstream_id']
                reg.submit(pid, wid, 'k', 'task', 'criteria', 'scope', 'telegram:main')
                reg.request_approval(reg.claim()['run_id'], 'rm -rf ./x', 'dangerous', 600)
                unload = []
                ctx = types.SimpleNamespace(
                    get_config=lambda key, default: default, register_hook=lambda *a: None,
                    register_tool=lambda **kw: None, register_system_prompt_section=lambda *a: None,
                    register_skill=lambda *a, **kw: None, register_telegram_handler=lambda fn: None, _gateway_injection_allowed=lambda: False, on_unload=unload.append, _manager=manager,
                    inject_message=lambda *a, **kw: self.fail('Non-gateway process injected'))
                with patch.dict(os.environ, {}, clear=True), patch.dict(sys.modules, {
                    'hermes_constants': types.SimpleNamespace(get_hermes_home=lambda: base / 'home'),
                    'tools.approval_context': types.SimpleNamespace(get_current_session_key=lambda default: '', _get_session_platform=lambda: ''),
                }):
                    self.load().register(ctx)
                    try:
                        time.sleep(2.3)
                    finally:
                        unload[0]()
                event = reg.inspect(pid, 'telegram:main')['events'][0]
                self.assertEqual((event['attempts'], event['next_attempt']), (0, 0))

    def test_restart_restores_route_authority_without_fresh_admission(self):
        scratch = ROOT / 'scratch'
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            home = Path(directory)
            reg = Registry(home / 'registry.db', home)
            pid = reg.ensure_project('telegram:route', 'old')['id']
            reopened = Registry(reg.path, home)
            self.assertTrue(reopened.is_bound_route('telegram:route'))
            self.assertEqual(reopened.ensure_project('telegram:route', 'old')['id'], pid)
            self.assertNotEqual(reopened.ensure_project('telegram:route', 'new')['id'], pid)

    def test_plugin_register_route_and_cli_injection_guard(self):
        scratch = ROOT / 'scratch'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            base = Path(directory)
            project = base / 'repo'
            project.mkdir()
            hooks, tools, unload, sections, calls = {}, {}, [], [], []
            current = ['telegram:one']
            ctx = types.SimpleNamespace(
                get_config=lambda key, default: default,
                register_hook=lambda name, fn: hooks.update({name: fn}),
                register_tool=lambda **kw: tools.update({kw['name']: kw}),
                register_system_prompt_section=lambda *a: sections.append(a),
                register_skill=lambda *a, **kw: None,
                register_telegram_handler=lambda fn: None,
                _gateway_injection_allowed=lambda: False,
                on_unload=lambda fn: unload.append(fn),
                _manager=types.SimpleNamespace(_cli_ref=object(), has_gateway_message_injector=True),
                inject_message=lambda *a, **kw: calls.append((a, kw)),
            )
            with patch.dict(os.environ, {}, clear=True), patch.dict(sys.modules, {
                'hermes_constants': types.SimpleNamespace(get_hermes_home=lambda: base / 'home'),
                'tools.approval_context': types.SimpleNamespace(get_current_session_key=lambda default: current[0], _get_session_platform=lambda: 'telegram'),
            }):
                self.load().register(ctx)
                try:
                    bind = tools['project_status']['handler']
                    self.assertIn('error', json.loads(bind({}, session_id='main')))
                    hooks['post_gateway_admission'](session_key=current[0], platform='telegram')
                    bound = json.loads(bind({}, session_id='main'))['project']
                    self.assertEqual(bound['route'], current[0])
                    self.assertEqual(bound['main_session'], 'main')
                    reg = Registry(base / 'home/plugin-data/project-sessions/registry.db', base / 'home')
                    wid = reg.create_workstream(bound['id'], 'approval intake', current[0], project)['workstream_id']
                    reg.submit(bound['id'], wid, 'intake-task', 'task', 'criteria', 'explicit scope', current[0])
                    claimed = reg.claim()
                    action_id = reg.request_approval(claimed['run_id'], 'rm -rf ./important', 'dangerous', 10)
                    decide = tools['project_approval_decide']['handler']
                    arguments = {'project_id': bound['id'], 'approval_id': action_id, 'decision': 'once'}
                    self.assertIn('error', json.loads(decide(arguments)))
                    # An origin the registry cannot record (here: no user id, as for anonymous
                    # group admins) must not swallow the admitted user's typed vote.
                    hooks['post_gateway_admission'](session_key=current[0], platform='telegram', message_id='actual-user-message', text='approve ' + action_id,
                                                    source={'platform': 'telegram', 'chat_id': '-100', 'chat_type': 'group', 'user_id': None, 'thread_id': None})
                    self.assertEqual(json.loads(decide(arguments))['decision'], 'once')
                    for platform in ('cli', 'cron', 'kanban', 'discord', '', 'worker'):
                        self.assertEqual(sections[0][1]({'platform': platform}), '')
                    self.assertIn('project', sections[0][1]({'platform': 'telegram'}).lower())
                    current[0] = 'telegram:other'
                    hooks['post_gateway_admission'](session_key=current[0], platform='telegram')
                    status = tools['project_status']['handler']
                    self.assertIn('error', json.loads(status({'project_id': bound['id']})))
                    self.assertEqual(len(sections), 1)
                    self.assertEqual(calls, [])
                finally:
                    unload[0]()


if __name__ == '__main__':
    unittest.main()

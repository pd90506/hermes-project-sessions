"""Restart ends started work; only queued tasks or explicit submissions can run."""
import time
import subprocess
import sys
import unittest
from unittest.mock import patch

from project_sessions.executor import Executor, HermesAdapter, birth
from project_sessions.lifecycle import holder
import test_project_sessions as fixtures


class RestartInterruption(unittest.TestCase):
    setUp=fixtures.ProjectTests.setUp
    tearDown=fixtures.ProjectTests.tearDown
    submit=fixtures.ProjectTests.submit
    snapshot=fixtures.ProjectTests.snapshot
    wait=fixtures.ProjectTests.wait

    def test_shutdown_ends_run_keeps_queue_and_requires_explicit_continuation(self):
        tid=self.submit('HANG',key='original')
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            self.ex.tick()
            runs=self.snapshot()['runs']
            if runs and runs[0]['session_id']:break
            time.sleep(.02)
        old=runs[0]
        self.assertTrue(old['session_id'])
        queued=self.submit('queued but not started')
        self.ex.close()
        self.assertIsNone(holder(self.reg.path))
        self.assertIsNone(birth(old['pid']))
        self.assertEqual([t['state'] for t in self.snapshot()['tasks']],['interrupted','queued'])
        self.ex=Executor(self.reg,HermesAdapter(str(fixtures.FAKE)),timeout=5)
        self.ex.acquire()
        self.ex.reconcile();self.ex.reconcile()
        self.assertEqual(self.wait(queued)['state'],'completed')
        snapshot=self.snapshot()
        self.assertEqual(snapshot['tasks'][0]['state'],'interrupted')
        self.assertEqual([r['task_id'] for r in snapshot['runs']],[tid,queued])
        self.assertEqual(snapshot['runs'][1]['resumed_from'],old['session_id'])
        self.assertEqual(self.submit('HANG',key='original'),tid)  # An idempotent retry cannot rerun it.
        continued=self.wait(self.submit('explicit continuation'))
        self.assertEqual(continued['state'],'completed')
        self.assertIn('prior_turns=2',continued['result'])
        self.assertEqual(self.snapshot()['runs'][-1]['resumed_from'],old['session_id'])
        delivered=[]
        while self.reg.dispatch(lambda content,**kw:delivered.append(content) or True):pass
        self.assertEqual(sum('"kind": "interrupted"' in text for text in delivered),1)
        self.assertFalse(self.reg.dispatch(lambda *a,**kw:True))

    def test_abrupt_interruption_denies_approval_and_never_requeues(self):
        tid=self.submit()
        task=self.reg.claim()
        approval=self.reg.request_approval(task['run_id'],'echo protected','confirm',600)
        self.ex.reconcile();self.ex.reconcile();self.ex.tick()
        self.assertEqual(self.reg.poll_approval(approval),'deny')
        self.assertEqual(self.snapshot()['tasks'][0]['state'],'interrupted')
        self.assertIsNone(self.reg.claim())
        self.assertEqual(len(self.snapshot()['runs']),1)
        self.assertEqual(sum(e['kind']=='interrupted' for e in self.snapshot()['events']),1)
        self.assertIn('Interrupted by gateway restart',self.snapshot()['runs'][0]['error'])
        with self.reg.connection() as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM resource_locks').fetchone()[0],0)
        new=self.submit('explicit retry')
        self.assertNotEqual(new,tid)
        self.assertEqual(self.reg.claim()['id'],new)

    def test_user_cancel_is_not_resurrected(self):
        for state in ('running','uncertain'):
            tid=self.submit()
            task=self.reg.claim()
            with self.reg.connection() as db:
                db.execute('UPDATE tasks SET state=? WHERE id=?',(state,tid))
                db.execute('UPDATE runs SET state=? WHERE id=?',(state,task['run_id']))
            self.reg.cancel(self.pid,tid,'telegram:bound')
            self.ex.reconcile()
            self.assertEqual(self.snapshot()['tasks'][-1]['state'],'cancelled')
        self.assertEqual(sum(e['kind']=='interrupted' for e in self.snapshot()['events']),0)

    def test_cgroup_sigterm_reaching_worker_first_still_ends_interrupted(self):
        # `systemctl stop` of the parent's scope SIGTERMs every process at
        # once; the worker died before the executor saw its stop flag and the run ended
        # needs_input with a "relay this result" event instead of interrupted.
        import os,signal,threading
        self.submit('HANG')
        self.ex.tick()
        deadline=time.monotonic()+3
        while time.monotonic()<deadline and not (self.snapshot()['runs'] and self.snapshot()['runs'][0]['session_id']):
            time.sleep(.02)
        os.killpg(self.snapshot()['runs'][0]['pid'],signal.SIGTERM)
        threading.Timer(.3,self.ex.stop.set).start()
        self.ex.pool.shutdown(wait=True)
        self.assertEqual(self.snapshot()['tasks'][0]['state'],'interrupted')
        self.assertEqual([e['kind'] for e in self.snapshot()['events']],['interrupted'])

    def test_unproven_surviving_group_keeps_lock_until_cleanup(self):
        self.submit()
        task=self.reg.claim()
        process=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
        try:
            self.reg.attach(task['run_id'],process.pid,None)
            with patch('project_sessions.executor.birth',return_value=None):
                self.ex.reconcile()
            self.assertIsNone(process.poll())
            self.assertIsNone(self.reg.claim())
            self.assertTrue(self.ex._pending_recovery)
        finally:
            process.terminate();process.wait(timeout=5)
        self.ex.reconcile()
        self.assertEqual(self.snapshot()['tasks'][0]['state'],'interrupted')

"""Model-free lifecycle tests: dummy lock holders, never systemd or Hermes runs."""
import concurrent.futures
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch, Mock

import hermes_env  # noqa: F401  (creates the scratch directory)
from project_sessions import lifecycle as life
from project_sessions.executor import Executor
from project_sessions.registry import Registry
import test_p2d as fixtures

ROOT=Path(__file__).resolve().parents[1]


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'scratch')
        self.home=Path(self.tmp.name)
        self.reg=Registry(self.home/'registry.db',self.home)
        self.processes=[]

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None: process.terminate()
            process.wait(timeout=5)
        self.tmp.cleanup()

    def spawn_holder(self, home, db, version=life.VERSION):
        script='''import sys,fcntl,json,time,os
with open(sys.argv[1],'a+') as f:
 fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
 f.seek(0);f.truncate();json.dump({'pid':os.getpid(),'birth':'dummy','version':sys.argv[2]},f);f.flush()
 time.sleep(60)
'''
        process=subprocess.Popen([sys.executable,'-c',script,str(life.paths(db)[0]),version],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.processes.append(process)
        for _ in range(100):
            if life.holder(db):return True
            time.sleep(.01)
        return False

    def test_concurrent_ensure_single_instance_and_respawn(self):
        with patch.object(life,'start',side_effect=self.spawn_holder) as start:
            barrier=threading.Barrier(5)
            threads=[threading.Thread(target=lambda:(barrier.wait(),life.ensure(self.home,self.reg.path))) for _ in range(5)]
            for t in threads:t.start()
            for t in threads:t.join(timeout=5)
            self.assertEqual(start.call_count,1)
            self.assertEqual(life.ensure(self.home,self.reg.path),'online')
            self.processes[0].terminate();self.processes[0].wait(timeout=5)
            later=time.time()+3  # past the crash-loop backoff for a just-spawned executor
            with patch.object(life.time,'time',return_value=later):
                self.assertEqual(life.ensure(self.home,self.reg.path),'online')
            self.assertEqual(start.call_count,2)

    def test_version_handoff_requests_drain_without_signalling_holder(self):
        self.spawn_holder(self.home,self.reg.path,'old-version')
        with patch.object(life,'start') as start:
            self.assertEqual(life.ensure(self.home,self.reg.path),'draining')
            owner=life.holder(self.reg.path)
            self.assertTrue(life.drain_requested(self.reg.path,owner))
            self.assertIsNone(self.processes[0].poll())
            start.assert_not_called()
        self.processes[0].terminate();self.processes[0].wait(timeout=5)
        with patch.object(life,'start',side_effect=self.spawn_holder):
            self.assertEqual(life.ensure(self.home,self.reg.path),'online')
            self.assertFalse(life.drain_requested(self.reg.path,life.holder(self.reg.path)))

    def test_drain_finishes_active_work_without_claiming_queued_work(self):
        executor=Executor(self.reg)
        executor.acquire()
        pending=concurrent.futures.Future()
        executor.futures={pending}
        # The serving loop would normally acquire here; use the actual owned lock above.
        executor.acquire=lambda:None
        executor.reconcile=lambda:None
        executor.tick=lambda:self.fail('Draining executor claimed new work')
        life.paths(self.reg.path)[1].write_text(json.dumps(executor.owner))
        thread=threading.Thread(target=executor.serve)
        thread.start()
        time.sleep(.25)
        self.assertTrue(thread.is_alive())
        self.assertFalse(executor.stop.is_set())
        pending.set_result(None)
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(life.holder(self.reg.path))

    def test_start_is_plain_gateway_child_and_shutdown_terminates_it(self):
        process=NS(wait=lambda **kw:None,poll=lambda:None,terminate=lambda:None)
        with patch.object(life.shutil,'which',return_value='/bin/hermes'), patch.object(life.subprocess,'run') as run, patch.object(life.subprocess,'Popen',return_value=process) as popen:
            self.assertTrue(life.start(self.home,self.reg.path))
            self.assertFalse(popen.call_args.kwargs.get('start_new_session',False))
            self.assertNotIn('preexec_fn',popen.call_args.kwargs)
            run.assert_not_called()
        with patch.object(life.subprocess,'Popen',return_value=NS(wait=lambda **kw:None,poll=lambda:None,terminate=Mock())) as popen:
            life.start(self.home,self.reg.path)
            process=popen.return_value
            life.shutdown(self.reg.path)
            process.terminate.assert_called_once()
            life.shutdown(self.reg.path)
            process.terminate.assert_called_once()

    def test_failed_launch_is_offline_not_optimistically_online(self):
        with patch.object(life,'start',return_value=False):
            self.assertEqual(life.ensure(self.home,self.reg.path),'offline')
        with patch.object(life,'start',return_value=True),patch.object(life.time,'sleep'):
            self.assertEqual(life.ensure(self.home,self.reg.path),'offline')


class ExecutorSupervisionTests(unittest.TestCase):
    setUp=LifecycleTests.setUp
    tearDown=LifecycleTests.tearDown
    spawn_holder=LifecycleTests.spawn_holder

    def test_crash_looping_executor_is_respawned_with_backoff(self):
        # The gateway poller calls ensure every second; an executor that dies at once must not
        # be relaunched every poll.
        now=[1000.0]
        with patch.object(life,'start',return_value=True) as start,patch.object(life.time,'sleep'),patch.object(life.time,'time',side_effect=lambda:now[0]):
            for _ in range(5):
                self.assertEqual(life.ensure(self.home,self.reg.path),'offline')
            self.assertEqual(start.call_count,1)
            now[0]+=3
            life.ensure(self.home,self.reg.path)
            self.assertEqual(start.call_count,2)
            now[0]+=1
            life.ensure(self.home,self.reg.path)
            self.assertEqual(start.call_count,2)
            # After a long-lived executor dies, respawn immediately.
            now[0]+=3600
            life.ensure(self.home,self.reg.path)
            self.assertEqual(start.call_count,3)

    def test_gateway_restart_is_not_counted_as_executor_crash(self):
        # A plain child dies with every gateway restart; a fresh gateway after a
        # few quick restarts reported offline (16 s here, up to 5 min) instead of spawning.
        state=Path(str(self.reg.path)+'.executor-start.json')
        state.write_text(json.dumps({'at':time.time()-5,'streak':8}))
        life._launched.discard(str(self.reg.path))  # a new gateway process
        with patch.object(life,'start',return_value=True) as start,patch.object(life.time,'sleep'):
            life.ensure(self.home,self.reg.path)
            self.assertEqual(start.call_count,1)
            life.ensure(self.home,self.reg.path)  # a crash within this gateway still backs off
            self.assertEqual(start.call_count,1)

    def test_launch_lock_contention_waits_instead_of_reporting_offline(self):
        self.spawn_holder(self.home,self.reg.path)
        with open(str(self.reg.path)+'.executor-start.lock','a') as other:
            fcntl.flock(other,fcntl.LOCK_EX)
            threading.Timer(.2,lambda:fcntl.flock(other,fcntl.LOCK_UN)).start()
            self.assertEqual(life.ensure(self.home,self.reg.path),'online')

    def test_plain_child_keeps_gateway_path_for_workers(self):
        with patch.object(life.shutil,'which',return_value='/bin/hermes'), patch.object(life.subprocess,'Popen',return_value=NS(wait=lambda:None)), patch.dict(os.environ,{'PATH':'/gateway/bin:/usr/bin'}):
            life.start(self.home,self.reg.path)
            self.assertEqual(life.subprocess.Popen.call_args.kwargs['env']['PATH'],'/gateway/bin:/usr/bin')
        life._children.pop(str(self.reg.path),None)

    def test_uncertain_run_does_not_block_upgrade_drain(self):
        # Uncertain runs are registry state for operator reconciliation, not work this process owns;
        # waiting on them would stall every queued task behind the drain indefinitely.
        pid=self.reg.ensure_project('telegram:r10','main')['id']
        wid=self.reg.create_workstream(pid,'stuck','telegram:r10',name='stuck')['workstream_id']
        self.reg.submit(pid,wid,'k','task','criteria','scope','telegram:r10')
        task=self.reg.claim()
        with self.reg.connection() as db:
            db.execute("UPDATE runs SET state='uncertain' WHERE id=?",(task['run_id'],))
        executor=Executor(self.reg)
        executor.acquire()
        executor.acquire=lambda:None
        executor.reconcile=lambda:None
        executor.tick=lambda:self.fail('Draining executor claimed new work')
        life.paths(self.reg.path)[1].write_text(json.dumps(executor.owner))
        thread=threading.Thread(target=executor.serve)
        thread.start()
        thread.join(timeout=3)
        alive=thread.is_alive()
        if alive: executor.stop.set(); thread.join(timeout=3)
        self.assertFalse(alive)

    def test_executor_acquire_tolerates_a_momentary_probe(self):
        # holder() briefly takes the flock to test liveness; a starting executor must not die on it.
        lock=life.paths(self.reg.path)[0]
        probe=open(lock,'a'); fcntl.flock(probe,fcntl.LOCK_EX)
        threading.Timer(.1,probe.close).start()
        executor=Executor(self.reg)
        executor.acquire()
        self.assertEqual(life.holder(self.reg.path)['pid'],os.getpid())
        executor.close()


class SubmitOfflineNote(unittest.TestCase):
    setUp=fixtures.DelegationTests.setUp
    tearDown=fixtures.DelegationTests.tearDown
    register=fixtures.DelegationTests.register
    tool=fixtures.DelegationTests.tool
    args=fixtures.DelegationTests.args

    def test_gateway_load_submit_and_restore_ensure_without_blocking_queue(self):
        callbacks={}; tools={}; manager=NS(_cli_ref=None,has_gateway_message_injector=True)
        ctx=NS(get_config=lambda key,default:default,register_hook=lambda key,fn:callbacks.update({key:fn}),
               register_tool=lambda **kw:tools.update({kw['name']:kw}),register_system_prompt_section=lambda *a:None,
               register_skill=lambda *a,**kw:None,register_telegram_handler=lambda *a:None,
               _gateway_injection_allowed=lambda:False,_manager=manager,inject_message=lambda *a,**kw:False,
               on_unload=self.unload.append)
        with patch(self.module.__name__+'.project_sessions.lifecycle.ensure',return_value='offline') as ensure:
            self.module.register(ctx)
            self.assertEqual(ensure.call_count,1)
            result=json.loads(tools['project_submit']['handler'](self.args(),session_id='old'))
            self.assertEqual(result['state'],'queued')
            self.assertEqual(result['executor'],'offline')
            self.assertIn('queued',result['note'])
            self.assertEqual(len(self.reg.inspect(self.pid,self.route)['tasks']),1)
            self.reg.cancel(self.pid,result['id'],self.route)
            card=tools['project_workstream_card']['handler']
            card({'workstream_id':result['workstream_id'],'action':'archive'},session_id='old')
            restored=json.loads(card({'workstream_id':result['workstream_id'],'action':'restore'},session_id='old'))
            self.assertEqual(restored['executor'],'offline')
            self.assertEqual(ensure.call_count,3)
            # Stop this registration's supervisor before leaving its mock scope.
            self.unload[-1]()

    def test_non_gateway_submit_reports_real_executor_state(self):
        # A CLI main session must not claim offline while the gateway's executor is running.
        callbacks={}; tools={}; manager=NS(_cli_ref=object(),has_gateway_message_injector=False)
        ctx=NS(get_config=lambda key,default:default,register_hook=lambda key,fn:callbacks.update({key:fn}),
               register_tool=lambda **kw:tools.update({kw['name']:kw}),register_system_prompt_section=lambda *a:None,
               register_skill=lambda *a,**kw:None,register_telegram_handler=lambda *a:None,
               _gateway_injection_allowed=lambda:False,_manager=manager,inject_message=lambda *a,**kw:False,
               on_unload=self.unload.append)
        life_name=self.module.__name__+'.project_sessions.lifecycle'
        owner={'pid':1,'birth':'x','version':sys.modules[life_name].VERSION}
        with patch(life_name+'.ensure') as ensure,patch(life_name+'.holder',return_value=owner):
            self.module.register(ctx)
            result=json.loads(tools['project_submit']['handler'](self.args(),session_id='old'))
            self.assertEqual(result['state'],'queued')
            self.assertNotIn('executor',result)
            ensure.assert_not_called()
            self.unload[-1]()

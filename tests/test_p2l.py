import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from project_sessions.registry import Registry
from project_sessions.steering import deliver, text
from project_sessions.worker_policy import policy_overlay
import test_p2d as fixtures

ROOT=Path(__file__).resolve().parents[1]
from hermes_env import HERMES_SOURCE as HERMES, requires_hermes


class SendTests(unittest.TestCase):
    setUp=fixtures.DelegationTests.setUp
    tearDown=fixtures.DelegationTests.tearDown
    register=fixtures.DelegationTests.register
    tool=fixtures.DelegationTests.tool
    args=fixtures.DelegationTests.args

    def create(self):
        return self.tool('project_submit',self.args())

    def test_submit_only_new_send_idle_and_busy_queue(self):
        created=self.create()
        wid=created['workstream_id']
        self.assertIn('project_send',self.tool('project_submit',{'workstream_id':wid})['error'])
        original=self.reg.claim()
        busy=self.tool('project_send',{'workstream_id':wid,'task':'next task','user_request':'next task'})
        self.assertEqual(busy['delivery'],'queued')
        self.assertIsNone(self.reg.claim())
        self.reg.finish(original['run_id'],'completed',session_id='native-history')
        next_task=self.reg.claim()
        self.assertEqual((next_task['id'],next_task['session_id']),(busy['id'],'native-history'))
        self.reg.finish(next_task['run_id'],'completed')
        idle=self.tool('project_send',{'workstream_id':wid,'task':'idle follow-up','user_request':'idle follow-up'})
        self.assertEqual(self.reg.claim()['id'],idle['id'])
        self.assertEqual(len(self.reg.inspect(self.pid,self.route)['workstreams']),1)

    def setup_steer(self):
        created=self.create()
        run=self.reg.claim()
        self.reg.note_session(run['run_id'],'native')
        return created,run

    def test_steer_native_acceptance_and_durable_delivery(self):
        created,run=self.setup_steer()
        received=[]
        def accept(message):received.append(message);return True
        agent=NS(session_id='native',steer=accept)
        stop=threading.Event()
        def boundary():
            while not stop.wait(.01):
                with patch.dict('sys.modules',{'agent.subagent_lifecycle':NS(get_active_subagent_parent=lambda:agent)}):
                    deliver(self.reg,run['run_id'])
        thread=threading.Thread(target=boundary);thread.start()
        try:
            result=self.tool('project_send',{'workstream_id':created['workstream_id'],'task':'redirect now','user_request':'redirect now','steer':True})
        finally:stop.set();thread.join()
        self.assertEqual(result['delivery'],'steered')
        self.assertEqual(len(received),1)
        with sqlite3.connect(self.home/'state.db') as db:
            db.execute('CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT)')
            # The native helper contract is verified separately below.
            db.execute('INSERT INTO messages VALUES(?,?,?)',('native','user','[OUT-OF-BAND USER MESSAGE]\n'+received[0]+'\n[/OUT-OF-BAND USER MESSAGE]'))
        self.reg.finish(run['run_id'],'completed',session_id='native')
        self.assertEqual(self.reg.result(self.pid,result['id'],self.route)['state'],'steered')
        self.assertIsNone(self.reg.claim())

    def test_steer_with_surrounding_whitespace_is_not_duplicated(self):
        created,run=self.setup_steer()
        received=[]
        # Native AIAgent.steer() strips the text before it is persisted.
        agent=NS(session_id='native',steer=lambda message:received.append(message.strip()) or True)
        message=self.tool('project_send',{'workstream_id':created['workstream_id'],'task':'redirect now  \n\n','user_request':'redirect now  \n\n','steer':True})
        with patch.dict('sys.modules',{'agent.subagent_lifecycle':NS(get_active_subagent_parent=lambda:agent)}):
            deliver(self.reg,run['run_id'])
        with sqlite3.connect(self.home/'state.db') as db:
            db.execute('CREATE TABLE messages(session_id TEXT,role TEXT,content TEXT)')
            db.execute('INSERT INTO messages VALUES(?,?,?)',('native','user','[OUT-OF-BAND USER MESSAGE]\n'+received[0]+'\n[/OUT-OF-BAND USER MESSAGE]'))
        self.reg.finish(run['run_id'],'completed',session_id='native')
        self.assertEqual(self.reg.result(self.pid,message['id'],self.route)['state'],'steered')
        self.assertIsNone(self.reg.claim())

    def test_steer_registry_error_does_not_skip_denial_recording(self):
        from project_sessions.worker_policy import WorkerPolicy
        created,run=self.setup_steer()
        run_dir=self.home/'policy-run';run_dir.mkdir()
        (run_dir/'policy.json').write_text(json.dumps({'workspace':str(self.home),'registry_path':str(self.reg.path),'home':str(self.home),'run_id':run['run_id']}))
        policy=WorkerPolicy(run_dir,lambda:True,redact=str)
        with patch('project_sessions.steering.deliver',side_effect=sqlite3.OperationalError('database is locked')):
            policy.post_tool('terminal',{'command':'rm -rf x'},'BLOCKED: requires approval')
        self.assertEqual(len(json.loads((run_dir/'blocked-actions.json').read_text())),1)

    def test_steer_without_boundary_or_persistence_falls_back_to_queue(self):
        created,run=self.setup_steer()
        message=self.tool('project_send',{'workstream_id':created['workstream_id'],'task':'keep me','user_request':'keep me','steer':True})
        # Not yet accepted: the main session must not be told it was simply queued.
        self.assertEqual(message['delivery'],'steer_pending')
        self.assertIsNone(self.reg.claim())
        # Even accepted but not durably persisted messages survive abnormal termination.
        with self.reg.connection() as db:db.execute('UPDATE tasks SET steer_accepted=1 WHERE id=?',(message['id'],))
        self.reg.finish(run['run_id'],'interrupted',session_id='native')
        retry=self.reg.claim()
        self.assertEqual((retry['id'],retry['input']),(message['id'],"Task: keep me\nUser's original request: keep me"))
        self.assertEqual(retry['session_id'],'native')
        self.reg.finish(retry['run_id'],'completed')
        late=self.tool('project_send',{'workstream_id':created['workstream_id'],'task':'already finished','user_request':'already finished','steer':True})
        self.assertEqual(late['delivery'],'queued')
        self.assertEqual(self.reg.claim()['id'],late['id'])


class ProfileOverlayTests(unittest.TestCase):
    def test_manual_smart_off_and_no_yolo_or_permanent_grants(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'scratch') as directory:
            for mode in ('manual','smart','off'):
                run=Path(directory)/mode;run.mkdir()
                managed=policy_overlay(run)
                cfg=json.loads((managed/'config.yaml').read_text())
                self.assertNotIn('mode',cfg['approvals'])
                self.assertEqual(cfg['approvals']['single_query_mode'],'deny')
                self.assertEqual(cfg['command_allowlist'],[])
                self.assertIn('HERMES_YOLO_MODE=0',(managed/'.env').read_text())

    @requires_hermes
    def test_native_profile_modes_are_inherited_without_copying(self):
        for mode in ('manual','smart','off'):
            with tempfile.TemporaryDirectory(dir=ROOT/'scratch') as directory:
                home=Path(directory);run=home/'run';run.mkdir()
                (home/'config.yaml').write_text(json.dumps({'approvals':{'mode':mode}}))
                managed=policy_overlay(run)
                env={k:v for k,v in os.environ.items() if not k.startswith(('HERMES_','PROJECT_SESSIONS_'))}
                (run/'policy.json').write_text(json.dumps({'workspace':str(home)}))
                env.update(HERMES_HOME=str(home),HERMES_MANAGED_DIR=str(managed),PROJECT_SESSIONS_WORKER=str(run),HERMES_YOLO_MODE='0',PYTHONDONTWRITEBYTECODE='1',HERMES_DISABLE_LAZY_INSTALLS='1')
                script=r"""
import sys,importlib.util,os
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0,sys.argv[1])
from tools.approval_context import _get_approval_mode
assert _get_approval_mode()==sys.argv[2]
path=Path(sys.argv[3])/'plugin/project-sessions'
spec=importlib.util.spec_from_file_location('profile_worker',path/'__init__.py',submodule_search_locations=[str(path)])
module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
hooks={}
module.register(NS(register_hook=lambda name,fn:hooks.update({name:fn}),register_middleware=lambda *a:None))
assert (Path(os.environ['PROJECT_SESSIONS_WORKER'])/'guard-ready').exists()
assert hooks['pre_tool_call'](tool_name='read_file',args={}) is None
"""
                result=subprocess.run([str(HERMES/'venv/bin/python'),'-c',script,str(HERMES),mode,str(ROOT)],env=env,cwd=ROOT,capture_output=True,text=True,timeout=20)
                self.assertEqual(result.returncode,0,result.stderr)

    @requires_hermes
    def test_native_steer_and_smart_approval_contract_model_free(self):
        script=r'''
import sys
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from unittest.mock import patch
from types import SimpleNamespace as NS
from agent.prompt_builder import steer_user_row,STEER_MARKER_OPEN
from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results
row=steer_user_row('redirect')
assert row['role']=='user' and row['content'].startswith(STEER_MARKER_OPEN)
assert row['display_kind']=='steer'
from agent.subagent_lifecycle import bind_subagent_parent,get_active_subagent_parent
agent=NS(_drain_pending_steer=lambda:'redirect')
with bind_subagent_parent(agent):assert get_active_subagent_parent() is agent
messages=[{'role':'tool','content':'tool result'}]
apply_pending_steer_to_tool_results(agent,messages,1)
assert messages[-1]==row
from tools import approval
from tools.approval_context import _normalize_approval_mode
assert _normalize_approval_mode(False)=='off'
for mode in ('manual','smart'):
    with patch.object(approval.approval_context,'_get_approval_mode',return_value=mode),patch.object(approval,'_yolo_active',return_value=False),patch.object(approval,'_command_matches_permanent_allowlist',return_value=False),patch.object(approval,'_floor_block',return_value=None),patch.object(approval,'detect_dangerous_command',return_value=(True,'p','why')),patch.object(approval,'_presence',return_value=(lambda *a:None,True,False,False)),patch.object(approval,'is_approved',return_value=False),patch.object(approval,'_human_decision',return_value={'approved':True}) as decision:
        approval.check_all_command_guards('python3 -c "print(1)"','local')
        assert decision.call_args.kwargs['smart']==(mode=='smart')
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'scratch') as directory:
            env={k:v for k,v in os.environ.items() if not k.startswith(('HERMES_','PROJECT_SESSIONS_'))}
            env.update(HERMES_HOME=directory,PYTHONDONTWRITEBYTECODE='1',HERMES_DISABLE_LAZY_INSTALLS='1')
            result=subprocess.run([str(HERMES/'venv/bin/python'),'-c',script,str(HERMES),str(ROOT)],env=env,cwd=ROOT,capture_output=True,text=True,timeout=25)
            self.assertEqual(result.returncode,0,result.stderr)

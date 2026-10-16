import json
import contextlib
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import unittest

import test_p2c as fixtures
from project_sessions.registry import Registry, WorkstreamStateError

ROOT = Path(__file__).resolve().parents[1]
from hermes_env import HERMES_SOURCE as HERMES, requires_hermes


class DelegationTests(unittest.TestCase):
    setUp = fixtures.FollowupTests.setUp
    tearDown = fixtures.FollowupTests.tearDown
    register = fixtures.FollowupTests.register
    tool = fixtures.FollowupTests.tool
    hook = fixtures.FollowupTests.hook

    def args(self, key='one', **extra):
        return {'new_workstream': {'scope':'Research topic','name':'Research'},
                'request_key':key,'task':'Research and save results','user_request':'Research and save results','criteria':'Report produced',
                'authorization':'Read-only research and report files', **extra}

    def direct(self, args):
        return self.reg.submit(self.pid,args.get('workstream_id'),args['request_key'],args['task'],args['criteria'],args['authorization'],self.route,args.get('workspace'),new_workstream=args.get('new_workstream'))

    def test_one_call_default_workspace_replay_and_followup_fifo(self):
        result=self.tool('project_submit',self.args())
        self.assertEqual(result['state'],'queued')
        self.assertEqual(result['name'],'Research')
        self.assertTrue(Path(result['default_workspace']).is_dir())
        self.assertEqual(self.tool('project_submit',self.args())['id'],result['id'])
        snap=self.reg.inspect(self.pid,self.route)
        self.assertEqual((len(snap['workstreams']),len(snap['tasks'])),(1,1))
        task=self.reg.claim()
        follow=self.args('two'); follow.pop('new_workstream'); follow['workstream_id']=result['workstream_id']
        queued=self.tool('project_send',{'workstream_id':result['workstream_id'],'task':follow['task'],'user_request':follow['user_request']})
        self.assertEqual(queued['workstream_id'],result['workstream_id'])
        self.assertIsNone(self.reg.claim())
        self.reg.finish(task['run_id'],'completed',session_id='native-tip')
        next_task=self.reg.claim()
        self.assertEqual((next_task['id'],next_task['session_id']),(queued['id'],'native-tip'))

    def test_explicit_persistent_workspace_and_task_override(self):
        persistent=self.home/'persistent'; persistent.mkdir()
        override=self.home/'override'; override.mkdir()
        args=self.args(new_workstream={'scope':'External','workspace':str(persistent)},workspace=str(override))
        result=self.tool('project_submit',args)
        self.assertEqual(result['default_workspace'],str(persistent))
        task=self.reg.claim()
        self.assertEqual(task['cwd'],str(override))
        self.reg.finish(task['run_id'],'completed')
        follow=self.args('follow');follow.pop('new_workstream');follow['workstream_id']=result['workstream_id']
        self.tool('project_send',{'workstream_id':result['workstream_id'],'task':follow['task'],'user_request':follow['user_request']})
        self.assertEqual(self.reg.claim()['cwd'],str(persistent))

    def test_idempotency_persists_and_rejects_changed_intent(self):
        first=self.direct(self.args())
        reopened=Registry(self.reg.path,self.home)
        retry=reopened.submit(self.pid,None,'one',self.args()['task'],self.args()['criteria'],self.args()['authorization'],self.route,new_workstream=self.args()['new_workstream'])
        self.assertEqual(retry['id'],first['id'])
        for args in (self.args(task='Changed'),self.args(new_workstream={'scope':'Different','name':'Research'}),self.args(criteria='Different'),self.args(authorization='Changed')):
            with self.assertRaises(ValueError):
                self.direct(args)
        current=self.reg.ensure_project(self.route,'new')['id']
        other = self.reg.submit(current,None,'one',self.args()['task'],self.args()['criteria'],self.args()['authorization'],self.route,new_workstream=self.args()['new_workstream'])
        self.assertNotEqual(other['id'], first['id'])
        self.assertEqual(len(self.reg.inspect(self.pid,self.route)['workstreams']),1)
        self.assertEqual(len(self.reg.inspect(current,self.route)['workstreams']),1)

    def test_concurrent_same_key_creates_one_stream_and_task(self):
        barrier=threading.Barrier(4); results=[]; errors=[]
        def submit():
            try:
                barrier.wait();results.append(self.direct(self.args()))
            except Exception as exc:
                errors.append(exc)
        threads=[threading.Thread(target=submit) for _ in range(4)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(len(results),4)
        self.assertEqual(len({r['id'] for r in results}),1)
        snap=self.reg.inspect(self.pid,self.route)
        self.assertEqual((len(snap['tasks']),len(snap['workstreams'])),(1,1))

    def test_rollback_restores_cap_archive_and_removes_new_empty_workspace(self):
        old=self.reg.create_workstream(self.pid,'Original',self.route)
        self.reg.max_active_workstreams=1
        with self.reg.connection() as db:
            db.executescript("CREATE TRIGGER fail_queue BEFORE INSERT ON tasks BEGIN SELECT RAISE(ABORT,'fixture failure'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            self.direct(self.args())
        snapshot=self.reg.inspect(self.pid,self.route)
        self.assertEqual(len(snapshot['workstreams']),1)
        self.assertIsNone(snapshot['workstreams'][0]['archived_at'])
        self.assertEqual(snapshot['events'],[])
        self.assertEqual(snapshot['tasks'],[])
        self.assertEqual(list((self.reg.path.parent/'workspaces').iterdir()),[Path(old['workspace'])])
        with self.reg.connection() as db:db.execute('DROP TRIGGER fail_queue')
        self.assertEqual(self.direct(self.args())['state'],'queued')

    def test_postcommit_failure_keeps_workspace_and_retry_recovers(self):
        original=self.reg.connection
        first=[True]
        @contextlib.contextmanager
        def lost_ack():
            with original() as db:
                yield db
            if first[0]:
                first[0]=False
                raise RuntimeError('simulated acknowledgement loss after commit')
        self.reg.connection=lost_ack
        with self.assertRaisesRegex(RuntimeError,'after commit'):
            self.direct(self.args())
        snapshot=self.reg.inspect(self.pid,self.route)
        self.assertEqual(len(snapshot['workstreams']),1)
        self.assertTrue(Path(snapshot['workstreams'][0]['workspace']).is_dir())
        retry=self.direct(self.args())
        self.assertEqual(retry['id'],snapshot['tasks'][0]['id'])
        self.assertEqual(len(self.reg.inspect(self.pid,self.route)['tasks']),1)

    def test_invalid_target_and_old_project_refuse_without_creation(self):
        base=self.args()
        cases=[{**base,'workstream_id':'both'}, {k:v for k,v in base.items() if k!='new_workstream'},
               {**base,'new_workstream':{}},{**base,'new_workstream':'wrong'},
               {**base,'new_workstream':{'scope':'topic','unknown':'field'}},
               {**base,'new_workstream':{'scope':'topic','workspace':str(self.home/'missing')}}]
        for args in cases:self.assertIn('error',self.tool('project_submit',args))
        self.hook('new',[])
        error=self.tool('project_submit',{'project_id':self.pid,**base},'new')['error']
        self.assertIn('Start a new subsession in the current conversation',error)
        self.assertEqual(self.reg.inspect(self.pid,self.route)['workstreams'],[])

    def test_named_inline_creation_retains_deduplication_and_cap_refusal(self):
        first=self.direct(self.args())
        second=self.direct(self.args('two'))
        self.assertEqual(second['name'],'Research (2)')
        self.reg.max_active_workstreams=2
        with self.assertRaisesRegex(WorkstreamStateError,'no idle subsession'):
            self.direct(self.args('three'))
        self.assertEqual(len(self.reg.inspect(self.pid,self.route)['tasks']),2)

    def test_schema_eight_upgrade_keeps_existing_task_and_session(self):
        stream=self.reg.create_workstream(self.pid,'Existing',self.route)['workstream_id']
        args=self.args();args.pop('new_workstream');args['workstream_id']=stream
        result=self.direct(args);task=self.reg.claim()
        self.reg.finish(task['run_id'],'completed',session_id='old-native-tip',result='Context retained')
        before=self.reg.card(self.pid,stream,self.route)
        # Strip the new empty table to reproduce a populated schema-8 database.
        with self.reg.connection() as db:
            db.execute('DROP TABLE delegation_requests')
            db.execute('UPDATE meta SET version=8')
        upgraded=Registry(self.reg.path,self.home)
        after=upgraded.card(self.pid,stream,self.route)
        self.assertEqual(before,after)
        self.assertEqual(upgraded.result(self.pid,result['id'],self.route)['state'],'completed')
        with upgraded.connection() as db:
            self.assertEqual(db.execute('SELECT version FROM meta').fetchone()[0],11)
            self.assertEqual(db.execute('SELECT count(*) FROM delegation_requests').fetchone()[0],0)

    def test_static_prompt_and_submit_schema_match_examples(self):
        prompt=self.sections[0][1]({'platform':'telegram'})
        self.assertLess(len(prompt),1400)
        for intent in ('project', 'background sessions', 'history', 'responsive', 'quick', 'Reuse', 'Relay', 'same subsession'):
            self.assertIn(intent.lower(), prompt.lower())
        self.assertEqual(prompt.count('→'), 4)
        examples=[json.loads(line[line.index('{"calls"'):]) for line in prompt.splitlines() if '{"calls"' in line]
        self.assertEqual(len(examples),3)
        new=examples[0]['calls'][0]
        self.assertEqual(new['name'],'project_submit')
        created=self.tool(new['name'],new['arguments'])
        self.assertNotIn('error',created)
        follow=examples[1]['calls'][0]
        self.assertNotIn('error',self.tool(follow['name'],{**follow['arguments'],'workstream_id':created['workstream_id']}))
        self.assertNotIn('error',self.tool('project_status',examples[2]['calls'][0]['arguments']))
        self.assertEqual(prompt,self.sections[0][1]({'platform':'telegram'}))
        self.assertEqual(self.sections[0][1]({'platform':'cli'}),'')
        self.assertEqual(len(self.tools),9)
        params=self.tools['project_submit']['schema']['parameters']
        self.assertNotIn('workstream_id',params['required'])
        self.assertEqual(params['properties']['new_workstream']['required'],['scope'])
        self.assertNotIn('workstream_id',params['properties'])
        self.assertIn('new_workstream',params['required'])

    def test_result_envelope_keeps_brief_relay_guidance_and_data(self):
        result=self.direct(self.args())
        task=self.reg.claim()
        report='Reported finding: https://example.invalid/deal; caveat: price may change.'
        self.reg.finish(task['run_id'],'completed',result=report,artifact='reported-artifact.md')
        messages=[]
        self.assertTrue(self.reg.dispatch(lambda content,**kw:messages.append(content) or True))
        header,body=messages[0].split('\n',1)
        self.assertLess(len(header),200)
        for intent in ('relay', 'result', 'links', 'caveats', 'same subsession'):
            self.assertIn(intent, header.lower())
        envelope=json.loads(body)  # Existing single-line header/JSON bridge contract stays intact.
        self.assertEqual(envelope['workstream_id'],result['workstream_id'])
        self.assertEqual(envelope['payload']['result_summary'],report)
        self.assertEqual(envelope['payload']['artifact'],'reported-artifact.md')
        self.assertNotIn('requires verification',envelope['payload']['verification'])
        self.assertFalse(self.reg.dispatch(lambda *a,**kw:self.fail('Result replayed')))

    def test_result_guidance_is_static_and_skill_does_not_require_main_checks(self):
        prompt=self.sections[0][1]({'platform':'telegram'})
        skill=(ROOT/'plugin/project-sessions/skills/project-session-orchestration/SKILL.md').read_text()
        self.assertLess(len(skill),2500)
        for guidance in (prompt, skill):
            for intent in ('relay', 'same subsession', 'project_submit', 'project_status', 'new_workstream', 'workstream_id'):
                self.assertIn(intent, guidance.lower())
        self.direct(self.args())
        self.assertEqual(prompt,self.sections[0][1]({'platform':'telegram'}))

    def test_skill_routing_matches_prompt_and_is_optional(self):
        # Loading the skill must not reopen the "quick" loophole the prompt closed.
        prompt=' '.join(self.sections[0][1]({'platform':'telegram'}).split())
        path=ROOT/'plugin/project-sessions/skills/project-session-orchestration/SKILL.md'
        skill=' '.join(path.read_text().split())
        decide=prompt[prompt.index('Decide this first'):prompt.index('When unsure, use a subsession.')+len('When unsure, use a subsession.')]
        self.assertIn(decide,skill)
        self.assertNotIn('when it is quick',skill)
        description=path.read_text().split('description:',1)[1].split('\n',1)[0]
        source=(ROOT/'plugin/project-sessions/__init__.py').read_text()
        self.assertIn(description.strip().rstrip('.'),source)
        self.assertIn('not needed for routine delegation',description)

    @requires_hermes
    def test_native_bridge_accepts_unseen_prompt_shape_without_describe(self):
        schema=self.tools['project_submit']['schema']
        script=r'''
import json,sys
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
from tools.registry import registry
from tools.tool_search import resolve_underlying_call,validate_deferred_call_args
from model_tools import _dispatch_bridge_tool
from hermes_cli.plugins_dispatch import SYSTEM_PROMPT_SECTION_POSITIONS
schema=json.loads(sys.argv[2]);args=json.loads(sys.argv[3])
registry.register(name='project_submit',toolset='project_sessions',schema=schema,handler=lambda a,**kw:'{}',check_fn=lambda:True)
call={'calls':[{'name':'project_submit','arguments':args}]}
assert resolve_underlying_call(call)==('project_submit',args,None)
assert validate_deferred_call_args('project_submit',args) is None
with patch('model_tools.get_tool_definitions',return_value=[{'type':'function','function':schema}]):
    assert _dispatch_bridge_tool('tool_call',call,['project_sessions'],None)==(None,('project_submit',args))
    assert validate_deferred_call_args('project_submit',dict(args,workstream_id='both')) is not None
assert SYSTEM_PROMPT_SECTION_POSITIONS==frozenset({'after_memory'})
'''
        env={k:v for k,v in os.environ.items() if not k.startswith(('HERMES_','TERMINAL_','PROJECT_SESSIONS_'))}
        env.update(HERMES_HOME=str(self.home/'native'),PYTHONDONTWRITEBYTECODE='1')
        result=subprocess.run([str(HERMES/'venv/bin/python'),'-c',script,str(HERMES),json.dumps(schema),json.dumps(self.args())],cwd=ROOT,env=env,capture_output=True,text=True,timeout=25)
        self.assertEqual(result.returncode,0,result.stderr)

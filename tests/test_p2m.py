"""Model-free checks for scoped briefs, native turn budgets, and empty timeouts."""
import json
import os
import subprocess
from pathlib import Path
import unittest
from unittest.mock import patch

import test_p2d as fixtures
from hermes_env import HERMES_SOURCE, requires_hermes
from project_sessions.executor import HermesAdapter, Executor
from project_sessions.reasoning import save_worker_effort, worker_max_turns


class BriefTests(unittest.TestCase):
    setUp = fixtures.DelegationTests.setUp
    tearDown = fixtures.DelegationTests.tearDown
    register = fixtures.DelegationTests.register
    tool = fixtures.DelegationTests.tool
    args = fixtures.DelegationTests.args

    def test_required_verbatim_request_and_labelled_briefs(self):
        original = '  airpods pro3 最近1个月最低价是多少，在哪里？\n'
        args = self.args(user_request=original)
        created = self.tool('project_submit', args)
        task = self.reg.claim()
        self.assertEqual(task['input'], "Task: "+args['task']+"\nUser's original request: "+original)
        self.reg.finish(task['run_id'], 'completed', result='done')
        sent = self.tool('project_send', {'workstream_id':created['workstream_id'], 'task':'Same question', 'user_request':original})
        follow = self.reg.claim()
        self.assertEqual(follow['id'],sent['id'])
        self.assertEqual(follow['input'], "Task: Same question\nUser's original request: "+original)
        for name, values in [('project_submit',self.args('other')), ('project_send',{'workstream_id':created['workstream_id'],'task':'brief'})]:
            schema = self.tools[name]['schema']['parameters']
            self.assertIn('user_request',schema['required'])
            self.assertIn('task',schema['required'])
            for field in ('task', 'user_request'):
                for invalid in (None, '', '  ', 42):
                    result = json.loads(self.tools[name]['handler']({**values, 'user_request':'exact', field:invalid},session_id='old'))
                    self.assertIn('error',result)
            missing = {k: v for k, v in values.items() if k != 'user_request'}
            self.assertIn('error',json.loads(self.tools[name]['handler'](missing,session_id='old')))
        prompt = self.sections[0][1]({'platform':'telegram'})
        self.assertIn('task from conversation context and user_request verbatim',prompt)

    def test_native_turn_budget_override_and_inheritance(self):
        task = {'scope':'scope','input':"Task: brief\nUser's original request: exact",'criteria':'answer','authorization':'read only','session_id':None,'cwd':str(self.home)}
        for turns in (None, 7):
            run_dir = self.home / ('prompt-'+str(turns))
            run_dir.mkdir()
            (run_dir/'policy.json').write_text(json.dumps({'approval_timeout':60}))
            save_worker_effort(self.home, 'high', turns)
            self.assertEqual(worker_max_turns(self.home), turns)
            with patch('project_sessions.executor.subprocess.Popen') as launch:
                HermesAdapter().launch(task,str(self.home),run_dir)
            argv = launch.call_args.args[0]
            if turns is None:
                self.assertNotIn('--max-turns',argv)  # Native CLI resolves profile agent.max_turns.
            else:
                self.assertEqual(argv[argv.index('--max-turns')+1], '7')
            query = (run_dir/'query.txt').read_text()
            self.assertIn(task['input'], query)
            self.assertNotIn('Time limit', query)
            self.assertNotIn('minutes', query)
        for invalid in (0, -1, True, '7', 1.5):
            with self.assertRaises(ValueError):
                save_worker_effort(self.home, 'high', invalid)
        executor = Executor(self.reg)
        self.assertEqual(executor.timeout,21600)
        executor.close()

    def test_empty_timeout_header_and_partial_result_relay(self):
        for result in ('', 'Partial findings'):
            created = self.tool('project_submit',self.args(key='timeout-'+str(bool(result))))
            task = self.reg.claim()
            self.reg.finish(task['run_id'],'needs_input',result=result,error='Timed out after 150 s; history kept')
            events = []
            self.assertTrue(self.reg.dispatch(lambda content,**kw:events.append(content) or True))
            header, body = events[0].split('\n',1)
            self.assertEqual(json.loads(body)['task_id'],created['id'])
            if not result:
                self.assertIn('Timed out with no result',header)
                self.assertIn('Relay this subsession result',header)
                self.assertNotIn("don't research it yourself",header)
            else:
                self.assertIn('Relay this subsession result',header)
                self.assertNotIn('no result',header)

    @requires_hermes
    def test_native_profile_budget_and_exhaustion_summary(self):
        native = HERMES_SOURCE
        script = r"""
import sys
from types import SimpleNamespace as NS
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
from hermes_cli.cli_init_mixin import CLIInitMixin
from agent import chat_completion_helpers as helpers
from agent.context_compressor import MAX_ITERATIONS_SUMMARY_REQUEST
with patch.dict(sys.modules, {'cli':NS(CLI_CONFIG={'agent':{'max_turns':13}})}):
    host=NS()
    CLIInitMixin._init_turn_limits(host,None,None)
    assert host.max_turns==13
    CLIInitMixin._init_turn_limits(host,7,None)
    assert host.max_turns==7
messages=[]
agent=NS(max_iterations=7,suppress_status_output=True,api_mode='chat_completions')
with patch.object(helpers,'_iteration_summary_api_messages',side_effect=lambda a,m:m), patch.dict(helpers._SUMMARY_ATTEMPT_BUILDERS, {'chat_completions':lambda a,m,i:lambda retry:'Partial findings'}):
    result=helpers.handle_max_iterations(agent,messages,7)
assert result=='Partial findings'
assert any(m['content']==MAX_ITERATIONS_SUMMARY_REQUEST for m in messages)
assert 'without calling any more tools' in MAX_ITERATIONS_SUMMARY_REQUEST
assert messages[-1]['content']=='Partial findings'
"""
        env={k:v for k,v in os.environ.items() if not k.startswith(('HERMES_','TERMINAL_','PROJECT_SESSIONS_'))}
        env.update(HERMES_HOME=str(self.home/'native'),PYTHONDONTWRITEBYTECODE='1')
        result=subprocess.run([str(native/'venv/bin/python'),'-c',script,str(native)],env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)


class OldShapeTests(unittest.TestCase):
    setUp = fixtures.DelegationTests.setUp
    tearDown = fixtures.DelegationTests.tearDown
    register = fixtures.DelegationTests.register
    tool = fixtures.DelegationTests.tool
    args = fixtures.DelegationTests.args

    def test_old_input_and_message_shapes_are_rejected(self):
        created = self.tool('project_submit', self.args())
        old_submit = {k: v for k, v in self.args('old').items() if k not in ('task', 'user_request')}
        for name, old in [('project_submit', {**old_submit, 'input': 'task'}),
                          ('project_send', {'workstream_id': created['workstream_id'], 'message': 'follow-up'})]:
            error = self.tool(name, old)['error']
            self.assertIn('user_request', error)
        self.assertEqual(len(self.reg.inspect(self.pid, self.route)['tasks']), 1)


import test_project_sessions as runtime


class TurnLimitRunTests(unittest.TestCase):
    setUp = runtime.ProjectTests.setUp
    tearDown = runtime.ProjectTests.tearDown
    submit = runtime.ProjectTests.submit
    snapshot = runtime.ProjectTests.snapshot
    wait = runtime.ProjectTests.wait

    def test_turn_budget_summary_is_relayed_as_partial_result(self):
        # hermes chat -q exits 1 at agent.max_turns while carrying the toolless summary.
        for marker, text in (('TURN_LIMIT_SUMMARY', 'Partial findings so far'),
                             ('TURN_LIMIT', "I reached the iteration limit and couldn't generate a summary.")):
            row = self.wait(self.submit(marker))
            self.assertEqual(row['state'], 'needs_input')
            self.assertEqual(Path(row['artifact']).read_text(), text)
            self.event = None
            self.assertTrue(self.reg.dispatch(lambda content, **kw: setattr(self, 'event', content) or True))
            payload = json.loads(self.event.split('\n', 1)[1])['payload']
            self.assertEqual(payload['result_summary'], text)
            self.assertIn('turn limit', payload['error'])
            self.assertNotIn('Timed out', self.event)

    def test_other_nonzero_exits_still_fail_without_text(self):
        row = self.wait(self.submit('FAILED_WITH_TEXT'))
        self.assertEqual(row['state'], 'failed')
        self.assertIsNone(row['artifact'])

"""Model-free per-run reasoning and native middleware contract checks."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from project_sessions.executor import HermesAdapter
from project_sessions.reasoning import effort, save_worker_effort, worker_effort, settings_path
import test_p2d as fixtures

ROOT = Path(__file__).resolve().parents[1]
from hermes_env import HERMES_SOURCE as HERMES, requires_hermes


class WorkerReasoning(unittest.TestCase):
    def test_settings_reach_new_and_resume_argv(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'scratch') as directory:
            base = Path(directory)
            home = base/'home'
            task = dict(scope='topic', input='task', criteria='done', authorization='read', cwd=str(base))
            self.assertEqual(worker_effort(home), 'high')
            for number, (setting, session) in enumerate(((None, None), (None, 'prior'), ('low', None), ('low', 'prior'), ('', None), ('', 'prior'))):
                if setting is not None:
                    save_worker_effort(home, setting)
                    self.assertEqual(settings_path(home).stat().st_mode & 0o777, 0o600)
                run = base/f'run{number}'
                run.mkdir()
                (run/'policy.json').write_text('{}')
                with patch('project_sessions.executor.subprocess.Popen') as popen:
                    HermesAdapter('/verified/hermes').launch({**task, 'session_id':session}, str(home), run)
                argv = popen.call_args.args[0]
                if setting == '':
                    self.assertNotIn('--reasoning', argv)
                else:
                    self.assertEqual(argv[argv.index('--reasoning')+1], setting or 'high')
                self.assertEqual('--resume' in argv, bool(session))
                if session:
                    self.assertEqual(argv[argv.index('--resume')+1], session)

    def test_invalid_efforts_fail_instead_of_silently_inheriting(self):
        for value in (None, False, 'HIGH', 'surprise'):
            with self.assertRaises(ValueError):
                effort(value)

    @requires_hermes
    def test_native_flag_precedence_and_request_translation(self):
        script = r'''
import sys,inspect
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from hermes_cli import oneshot, cli_init_mixin
from hermes_cli._parser import build_top_level_parser
parser,_,_=build_top_level_parser()
for tail in ([],["--resume","prior"]):
    args=parser.parse_args(["chat","--query-file","task.txt","-Q","--format","stream-json","--no-restore-cwd","--reasoning","high"]+tail)
    assert args.reasoning=="high"
source_init=inspect.getsource(cli_init_mixin)
assert source_init.index("self.reasoning_config = resolve_reasoning_config(CLI_CONFIG, self.model)") < source_init.index("self.reasoning_config = _cli_reasoning")
from providers import get_provider_profile
from project_sessions.reasoning import main_request
from hermes_constants import resolve_reasoning_config
source=inspect.getsource(oneshot)
assert 'parsed_reasoning = parse_reasoning_effort(reasoning)' in source
assert source.index('reasoning_config = resolve_reasoning_config(cfg, choice.model)') < source.index('reasoning_config = parsed_reasoning')
assert resolve_reasoning_config({'agent':{'reasoning_effort':'low','reasoning_overrides':{'deepseek-v4.1-flash':'max'}}},'deepseek-v4.1-flash')['effort']=='max'
request={'model':'deepseek-v4.1-flash','reasoning_effort':'max','extra_body':{'thinking':{'type':'disabled'},'other':1},'messages':[]}
for level in ('low','high','none'):
    result=main_request(request,level,provider='opencode-go',api_mode='chat_completions',model=request['model'])['request']
    if level=='none':
        assert 'reasoning_effort' not in result and result['extra_body']['thinking']['type']=='disabled'
    else:
        assert result['reasoning_effort']==level and result['extra_body'].get('thinking',{'type':'enabled'})['type']=='enabled'
    assert result['extra_body']['other']==1
assert request['reasoning_effort']=='max' and request['extra_body']['thinking']['type']=='disabled'
for provider,mode,model,setting in [('other','chat_completions',request['model'],'low'),('opencode-go','responses',request['model'],'low'),('opencode-go','chat_completions','unknown','low'),('opencode-go','chat_completions',request['model'],'')]:
    assert main_request(request,setting,provider=provider,api_mode=mode,model=model) is None
from agent import turn_api_request,turn_context
assert 'apply_llm_request_middleware' in inspect.getsource(turn_api_request)
assert 'platform=agent.platform' in inspect.getsource(turn_api_request)
assert 'r.get("context")' in inspect.getsource(turn_context._collect_pre_llm_call_context)
'''
        with tempfile.TemporaryDirectory(dir=ROOT/'scratch') as directory:
            env = {k:v for k,v in os.environ.items() if not k.startswith(('HERMES_', 'PROJECT_SESSIONS_'))}
            env.update(HERMES_HOME=directory, PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([str(HERMES/'venv/bin/python'),'-c',script,str(HERMES),str(ROOT)],cwd=ROOT,env=env,capture_output=True,text=True,timeout=25)
            self.assertEqual(result.returncode,0,result.stderr)


class MainReasoningScope(unittest.TestCase):
    setUp = fixtures.DelegationTests.setUp
    tearDown = fixtures.DelegationTests.tearDown
    register = fixtures.DelegationTests.register

    def test_optional_registration_and_existing_scope(self):
        middlewares = {}
        settings = {'worker_reasoning_effort':'', 'main_reasoning_effort':'low'}
        ctx = NS(get_config=lambda key,default:settings.get(key,default),
            register_middleware=lambda key,fn:middlewares.update({key:fn}),
            register_hook=lambda *a:None, register_tool=lambda **kw:None,
            register_system_prompt_section=lambda *a:None,register_skill=lambda *a,**kw:None,
            register_telegram_handler=lambda *a:None, _gateway_injection_allowed=lambda:False,
            _manager=NS(_cli_ref=object(),has_gateway_message_injector=False),
            inject_message=lambda *a,**kw:False,on_unload=self.unload.append)
        self.module.register(ctx)
        self.assertEqual(worker_effort(self.home), '')
        middleware = middlewares['llm_request']
        # Existing durable bound route is admitted after a gateway restart.
        # Patch the closed-over native translator by its imported provider, not the scope guard.
        provider = NS(build_api_kwargs_extras=lambda **kw:({}, {'reasoning_effort':kw['reasoning_config']['effort']}))
        with patch.dict('sys.modules', {'providers':NS(get_provider_profile=lambda name:provider)}):
            with patch.object(__import__('sys').modules['hermes_constants'], 'parse_reasoning_effort', lambda value:{'effort':value}, create=True):
                args=dict(request={'reasoning_effort':'max'},provider='opencode-go',api_mode='chat_completions',model='deepseek-v4.1-flash')
                self.assertEqual(middleware(platform='telegram',**args)['request']['reasoning_effort'],'low')
                for platform in ('cli','cron','kanban','worker','discord',''):
                    self.assertIsNone(middleware(platform=platform,**args))
                self.route='unbound-route'
                self.assertIsNone(middleware(platform='telegram',**args))
        settings['main_reasoning_effort']=''
        middlewares.clear()
        self.module.register(ctx)
        self.assertEqual(middlewares,{})

import subprocess
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import hermes_env  # noqa: F401  (creates the scratch directory)
from project_sessions.registry import Registry
from project_sessions.cards import parse_card, CARD_INSTRUCTION
from project_sessions.executor import HermesAdapter

ROOT = Path(__file__).resolve().parents[1]


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'scratch')
        self.home = Path(self.tmp.name)
        self.reg = Registry(self.home / 'data/registry.db', self.home)
        self.route = 'telegram:conversation'
        self.pid = self.reg.ensure_project(self.route, 'first')['id']

    def tearDown(self):
        self.tmp.cleanup()

    def submit(self, wid, key, **kw):
        return self.reg.submit(self.pid, wid, key, 'task', 'criteria', 'scope', self.route, **kw)['id']

    def test_implicit_identity_new_route_and_new_conversation(self):
        self.assertEqual(self.reg.ensure_project(self.route, 'first')['id'], self.pid)
        self.assertNotEqual(self.reg.ensure_project(self.route, 'after-new')['id'], self.pid)
        self.assertNotEqual(self.reg.ensure_project('telegram:new-thread', 'first')['id'], self.pid)
        with self.assertRaises(ValueError):
            self.reg.card(self.pid, 'unknown', 'telegram:other')
        with self.reg.connection() as db:
            self.assertNotIn('cwd', [r['name'] for r in db.execute('PRAGMA table_info(projects)')])

    def test_compression_preserves_project_reset_does_not(self):
        with sqlite3.connect(self.home / 'state.db') as db:
            db.execute('CREATE TABLE sessions(id TEXT,parent_session_id TEXT,end_reason TEXT,model_config TEXT)')
            db.executemany('INSERT INTO sessions VALUES(?,?,?,?)', [('first', None, 'compression', '{}'), ('compressed', 'first', 'new_session', '{}'), ('reset', 'compressed', None, '{}'), ('fork', 'first', None, '{"_branched_from":"first"}')])
        self.assertEqual(self.reg.ensure_project(self.route, 'compressed')['id'], self.pid)
        self.assertNotEqual(self.reg.ensure_project(self.route, 'reset')['id'], self.pid)
        self.assertNotEqual(self.reg.ensure_project(self.route, 'fork')['id'], self.pid)

    def test_persistent_workspace_task_override_and_fifo(self):
        w = self.reg.create_workstream(self.pid, 'scope', self.route, name='Writer')
        workspace = Path(w['workspace'])
        self.assertTrue(workspace.is_dir())
        (workspace / 'persistent.txt').write_text('context')
        other = self.reg.create_workstream(self.pid, 'other', self.route)
        self.assertNotEqual(w['workspace'], other['workspace'])
        override = self.home / 'explicit'; override.mkdir()
        first = self.submit(w['workstream_id'], 'one')
        task = self.reg.claim()
        self.assertEqual(task['cwd'], str(workspace))
        second = self.submit(w['workstream_id'], 'two', workspace=str(override))
        self.assertIsNone(self.reg.claim())
        c = self.reg.card(self.pid, w['workstream_id'], self.route)
        self.assertEqual((c['state'], c['queued']), ('running', 1))
        self.reg.finish(task['run_id'], 'completed', result='done')
        next_task = self.reg.claim()
        self.assertEqual(next_task['id'], second)
        self.assertEqual(next_task['cwd'], str(override))
        self.reg.finish(next_task['run_id'], 'completed')
        self.submit(w['workstream_id'], 'three')
        self.assertEqual(self.reg.claim()['cwd'], str(workspace))
        self.assertEqual((workspace / 'persistent.txt').read_text(), 'context')

    def test_cards_parse_store_fallback_and_event(self):
        w = self.reg.create_workstream(self.pid, 'context scope', self.route)
        tid = self.submit(w['workstream_id'], 'one')
        task = self.reg.claim()
        text = 'Answer\n<workstream-card>{"context":"We studied A", "conclusions":["A works"],"artifacts":["report.md"]}</workstream-card>'
        fenced = text.replace('<workstream-card>', '<workstream-card>```json\n').replace('</workstream-card>', '\n```</workstream-card>')
        self.assertTrue(parse_card(fenced, 'completed')['structured'])
        self.reg.finish(task['run_id'], 'completed', result=text)
        card = self.reg.card(self.pid, w['workstream_id'], self.route)
        self.assertEqual(card['card']['context'], 'We studied A')
        self.assertEqual(card['tasks_done'][0]['id'], tid)
        self.assertEqual(json.loads(self.reg.inspect(self.pid, self.route)['events'][0]['payload'])['card_update'], card['card'])
        self.submit(w['workstream_id'], 'two'); task = self.reg.claim()
        self.reg.finish(task['run_id'], 'failed', result='Missing trailer')
        card = self.reg.card(self.pid, w['workstream_id'], self.route)
        self.assertFalse(card['card']['structured'])
        self.assertEqual(len(card['updates']), 2)
        for bad in ('<workstream-card>bad</workstream-card>', '<workstream-card>[]</workstream-card>', '<workstream-card>{"context":"ok","conclusions":"wrong","artifacts":[]}</workstream-card>'):
            self.assertFalse(parse_card(bad, 'completed')['structured'])

    def test_roster_and_approval_state(self):
        w = self.reg.create_workstream(self.pid, 'Scope\nwith newline', self.route, name='Test\nname')
        self.submit(w['workstream_id'], 'one'); task = self.reg.claim()
        self.reg.request_approval(task['run_id'], 'rm -rf x', 'danger', 600)
        self.assertEqual(self.reg.card(self.pid, w['workstream_id'], self.route)['state'], 'awaiting approval')
        roster = self.reg.roster(self.pid, self.route)
        self.assertEqual(len(roster.splitlines()), 2)
        self.assertIn('awaiting approval', roster)
        self.assertNotIn('rm -rf x', roster)

    def test_task_prompt_requests_card(self):
        w = self.reg.create_workstream(self.pid, 'scope', self.route)
        self.submit(w['workstream_id'], 'one'); task = self.reg.claim()
        run_dir = self.home / 'launch'; run_dir.mkdir(); (run_dir / 'tmp').mkdir()
        (run_dir / 'policy.json').write_text('{}')
        with patch('project_sessions.executor.subprocess.Popen') as popen, patch.dict(os.environ, {'HERMES_MANAGED_DIR': str(self.home / 'no-admin')}):
            HermesAdapter('unused').launch(task, self.reg.home, run_dir)
        self.assertIn(CARD_INSTRUCTION, (run_dir / 'query.txt').read_text())
        self.assertEqual(popen.call_args.kwargs['cwd'], w['workspace'])

    def test_override_idempotency_and_explicit_create(self):
        override = self.home / 'external'; override.mkdir()
        w = self.reg.create_workstream(self.pid, 'external work', self.route, workspace=override)
        self.assertEqual(w['workspace'], str(override))
        tid = self.submit(w['workstream_id'], 'stable', workspace=str(override))
        self.assertEqual(self.submit(w['workstream_id'], 'stable', workspace=str(override)), tid)
        with self.assertRaises(ValueError):
            self.submit(w['workstream_id'], 'stable')

    def test_executor_cli_accepts_no_roots(self):
        result = subprocess.run(['python3', '-m', 'project_sessions.executor', '--help'], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn('--root', result.stdout)

    def test_plugin_roster_is_ephemeral_and_defaults_tools_to_conversation(self):
        spec = importlib.util.spec_from_file_location('p2fixture', ROOT / 'plugin/project-sessions/__init__.py', submodule_search_locations=[str(ROOT / 'plugin/project-sessions')])
        module = importlib.util.module_from_spec(spec)
        import sys
        sys.modules[spec.name] = module; spec.loader.exec_module(module)
        hooks, tools, sections, unload = {}, {}, [], []
        ctx = NS(get_config=lambda key,default:default, inject_message=lambda *a,**kw:True, register_hook=lambda n,f: hooks.update({n:f}), register_tool=lambda **kw: tools.update({kw['name']:kw}), register_system_prompt_section=lambda *a: sections.append(a), register_skill=lambda *a,**kw: None, register_telegram_handler=lambda f: None, _gateway_injection_allowed=lambda: False, _manager=NS(_cli_ref=object(),has_gateway_message_injector=False), on_unload=unload.append)
        with patch.dict(os.environ, {}, clear=True), patch.dict(sys.modules, {'hermes_constants': NS(get_hermes_home=lambda:self.home), 'tools.approval_context':NS(get_current_session_key=lambda default:self.route, _get_session_platform=lambda:'telegram')}):
            module.register(ctx)
            try:
                hooks['post_gateway_admission'](session_key=self.route,platform='telegram')
                frozen = sections[0][1]({'platform':'telegram'})
                first = hooks['pre_llm_call'](session_id='first',platform='telegram')['context']
                w = json.loads(tools['project_workstream_create']['handler']({'scope':'Writing'},session_id='first'))
                second = hooks['pre_llm_call'](session_id='first',platform='telegram')['context']
                self.assertIn(w['workstream_id'], second)
                self.assertNotEqual(first, second)
                self.assertEqual(frozen, sections[0][1]({'platform':'telegram'}))
                fresh = hooks['pre_llm_call'](session_id='after-new',platform='telegram')['context']
                self.assertNotIn(w['workstream_id'], fresh)
                self.assertIsNone(hooks['pre_llm_call'](session_id='cli',platform='cli'))
                self.assertNotIn('project_bind', tools)
                self.assertEqual(len(tools),9)
                full = json.loads(tools['project_workstream_card']['handler']({'workstream_id':w['workstream_id']},session_id='first'))
                self.assertEqual(full['id'],w['workstream_id'])
                archived = json.loads(tools['project_workstream_card']['handler']({'workstream_id':w['workstream_id'],'action':'archive'},session_id='first'))
                self.assertEqual(archived['state'],'archived')
                found = json.loads(tools['project_workstream_card']['handler']({'action':'list','query':'Writing'},session_id='first'))
                self.assertEqual(found['archived_cards'][0]['id'],w['workstream_id'])
                refused = json.loads(tools['project_send']['handler']({'workstream_id':w['workstream_id'],'task':'test','user_request':'test'},session_id='first'))
                self.assertIn('restore before submitting',refused['error'])
                restored = json.loads(tools['project_workstream_card']['handler']({'workstream_id':w['workstream_id'],'action':'restore'},session_id='first'))
                self.assertEqual(restored['state'],'idle')
                self.assertNotIn('workspace',tools['project_workstream_create']['schema']['parameters']['required'])
            finally:
                unload[0]()

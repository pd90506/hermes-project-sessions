import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import hermes_env  # noqa: F401  (creates the scratch directory)
from project_sessions.registry import Registry

ROOT = Path(__file__).resolve().parents[1]


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'scratch')
        self.home = Path(self.tmp.name)
        self.route = 'telegram:followups'
        self.reg = Registry(self.home/'plugin-data/project-sessions/registry.db',self.home)
        self.pid = self.reg.ensure_project(self.route,'old')['id']
        spec=importlib.util.spec_from_file_location('p2cfixture',ROOT/'plugin/project-sessions/__init__.py',submodule_search_locations=[str(ROOT/'plugin/project-sessions')])
        self.module=importlib.util.module_from_spec(spec)
        sys.modules[spec.name]=self.module; spec.loader.exec_module(self.module)
        self.hooks,self.tools,self.unload,self.sections={},{},[],[]
        self.env=patch.dict(os.environ,{},clear=True); self.env.start()
        self.native=patch.dict(sys.modules,{'hermes_constants':NS(get_hermes_home=lambda:self.home),
            'tools.approval_context':NS(get_current_session_key=lambda default:self.route,_get_session_platform=lambda:'telegram')})
        self.native.start()
        self.register()

    def register(self):
        ctx=NS(get_config=lambda k,d:d, register_hook=lambda n,f:self.hooks.update({n:f}),
            register_tool=lambda **kw:self.tools.update({kw['name']:kw}),register_system_prompt_section=lambda *a:self.sections.append(a),
            register_skill=lambda *a,**kw:None,register_telegram_handler=lambda f:None,
            _gateway_injection_allowed=lambda:False,_manager=NS(_cli_ref=object(),has_gateway_message_injector=False),
            inject_message=lambda *a,**kw:self.fail('Non-gateway injected'),on_unload=self.unload.append)
        self.module.register(ctx)
        self.hooks['post_gateway_admission'](session_key=self.route,platform='telegram')

    def tearDown(self):
        for unload in self.unload:
            unload()
        self.native.stop(); self.env.stop(); self.tmp.cleanup()

    def tool(self,name,args,session='old'):
        return json.loads(self.tools[name]['handler'](args,session_id=session))

    def hook(self,session='old',history=None):
        return self.hooks['pre_llm_call'](session_id=session,platform='telegram',conversation_history=history)

    def test_roster_change_only_first_turn_and_cache_stability(self):
        system=self.sections[0][1]({'platform':'telegram'})
        first=self.hook(history=[])
        history=[{'role':'user','api_content':'hello\n'+first['context']}]
        self.assertIsNone(self.hook(history=history))
        stream=self.tool('project_workstream_create',{'scope':'A'})
        changed=self.hook(history=history)
        self.assertIn(stream['workstream_id'],changed['context'])
        self.assertLess(len(changed['context']),10000)
        history.append({'role':'user','api_content':changed['context']})
        self.assertIsNone(self.hook(history=history))
        self.assertEqual(system,self.sections[0][1]({'platform':'telegram'}))
        self.assertIsNone(self.hooks['pre_llm_call'](session_id='cli',platform='cli'))

    def test_roster_restart_and_failed_host_persistence(self):
        first=self.hook(history=[])
        history=[{'role':'user','api_content':first['context']}]
        self.unload[-1]()
        self.register()
        self.assertIsNone(self.hook(history=history))
        # Return from hook is not proof of host persistence: resend if history lacks it.
        self.assertIsNotNone(self.hook(history=[]))

    def test_roster_first_after_compression_and_new(self):
        first=self.hook(history=[])
        with sqlite3.connect(self.home/'state.db') as db:
            db.execute('CREATE TABLE sessions(id TEXT,parent_session_id TEXT,end_reason TEXT,model_config TEXT)')
            db.executemany('INSERT INTO sessions VALUES(?,?,?,?)',[('old',None,'compression','{}'),('compressed','old',None,'{}')])
        compressed=self.hook('compressed',[{'role':'user','api_content':first['context']}])
        self.assertIsNotNone(compressed)
        self.assertEqual(self.reg.ensure_project(self.route,'compressed')['id'],self.pid)
        self.assertIsNone(self.hook('compressed',[{'role':'user','api_content':compressed['context']}]))
        self.assertIsNotNone(self.hook('new',[]))

    def test_old_projects_block_submit_create_restore_allow_read_cancel_approval(self):
        stream=self.tool('project_workstream_create',{'scope':'Old work'})['workstream_id']
        task=self.tool('project_send',{'workstream_id':stream,'task':'test','user_request':'test'})['id']
        claimed=self.reg.claim()
        aid=self.reg.request_approval(claimed['run_id'],'rm -rf x','danger',600)
        archived=self.tool('project_workstream_create',{'scope':'Archived'})['workstream_id']
        self.tool('project_workstream_card',{'workstream_id':archived,'action':'archive'})
        self.hook('new',[])
        for name,args in (
            ('project_workstream_create',{'scope':'Attempt'}),
            ('project_send',{'workstream_id':stream,'task':'new work','user_request':'new work'}),
            ('project_workstream_card',{'workstream_id':archived,'action':'restore'})):
            result=self.tool(name,{'project_id':self.pid,**args},'new')
            self.assertIn('Start a new subsession in the current conversation',result['error'])
        for name,args in (('project_status',{}),('project_result',{'task_id':task}),('project_workstream_card',{'workstream_id':stream})):
            self.assertNotIn('error',self.tool(name,{'project_id':self.pid,**args},'new'))
        self.hooks['post_gateway_admission'](session_key=self.route,platform='telegram',message_id='human',text='deny '+aid)
        self.assertEqual(self.tool('project_approval_decide',{'project_id':self.pid,'approval_id':aid,'decision':'deny'},'new')['decision'],'deny')
        self.assertEqual(self.tool('project_cancel',{'project_id':self.pid,'task_id':task},'new')['state'],'cancel_requested')
        self.assertNotIn('error',self.tool('project_workstream_create',{'scope':'New work'},'new'))

    def test_compressed_current_still_allows_submission(self):
        stream=self.tool('project_workstream_create',{'scope':'Continuity'})['workstream_id']
        with sqlite3.connect(self.home/'state.db') as db:
            db.execute('CREATE TABLE sessions(id TEXT,parent_session_id TEXT,end_reason TEXT,model_config TEXT)')
            db.executemany('INSERT INTO sessions VALUES(?,?,?,?)',[('old',None,'compression','{}'),('tip','old',None,'{}')])
        result=self.tool('project_send',{'project_id':self.pid,'workstream_id':stream,'task':'test','user_request':'test'},'tip')
        self.assertEqual(result['state'],'queued')

    def test_optional_timer_reset_starts_new_project_without_policy_edits(self):
        # Native idle/daily reset boundaries have the same product meaning as /new.
        with sqlite3.connect(self.home/'state.db') as db:
            db.execute('CREATE TABLE sessions(id TEXT,parent_session_id TEXT,end_reason TEXT,model_config TEXT)')
            db.executemany('INSERT INTO sessions VALUES(?,?,?,?)',[
                ('old',None,'daily','{}'),('auto-daily','old',None,'{"_reset_from":"old"}')])
        reset=self.reg.ensure_project(self.route,'auto-daily')['id']
        self.assertNotEqual(reset,self.pid)
        self.assertFalse(self.reg.is_current_conversation(self.pid,self.route,'auto-daily'))
        response=self.tool('project_workstream_create',{'project_id':self.pid,'scope':'Old followup'},'auto-daily')
        self.assertIn('Start a new subsession in the current conversation',response['error'])

    def test_upgrade_from_schema_six_grace_preserves_lru_and_archives(self):
        path=self.home/'six.db'
        with sqlite3.connect(path) as db:
            for version in range(1,7):
                db.executescript((ROOT/f'project_sessions/migrations/{version:03}.sql').read_text())
            db.execute('UPDATE meta SET profile_home=?',(str(self.home),))
            db.execute('INSERT INTO projects VALUES(?,?,?,?,?)',('p',str(self.home),self.route,'old',100))
            for wid,archived in [('idle',None),('archived',200)]:
                db.execute('INSERT INTO workstreams(id,project_id,scope,workspace,created,idle_since,last_used,archived_at) VALUES(?,?,?,?,?,?,?,?)',(wid,'p',wid,str(self.home),100,100,150,archived))
        before=int(time.time())
        reg=Registry(path,self.home)
        self.assertEqual(reg.sweep_archives(),0)
        self.assertGreaterEqual(reg.card('p','idle',self.route)['idle_since'],before)
        self.assertEqual(reg.card('p','idle',self.route)['last_used'],150)
        self.assertEqual(reg.card('p','archived',self.route)['archived_at'],200)

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import hermes_env  # noqa: F401  (creates the scratch directory)
from project_sessions.registry import Registry, WorkstreamStateError

ROOT = Path(__file__).resolve().parents[1]


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'scratch')
        self.home = Path(self.tmp.name)
        self.reg = Registry(self.home / 'registry.db', self.home, max_active_workstreams=2)
        self.route = 'telegram:archive'
        self.pid = self.reg.ensure_project(self.route, 'main')['id']

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, name, when):
        with patch('project_sessions.registry.time.time', return_value=when):
            return self.reg.create_workstream(self.pid, name, self.route, name=name)['workstream_id']

    def submit(self, wid, key='one'):
        return self.reg.submit(self.pid, wid, key, 'task', 'criteria', 'scope', self.route)['id']

    def card(self, wid):
        return self.reg.card(self.pid, wid, self.route)

    def notices(self):
        return [e for e in self.reg.inspect(self.pid, self.route)['events'] if e['kind']=='workstreams_archived']

    def test_idle_boundary_and_notice_once_and_roster(self):
        a = self.create('A', 100)
        b = self.create('B', 100)
        self.assertEqual(self.reg.sweep_archives(now=100+3*86400-1), 0)
        self.assertEqual(self.reg.sweep_archives(now=100+3*86400), 2)
        self.assertEqual(self.card(a)['state'], 'archived')
        self.assertNotIn(a, self.reg.roster(self.pid, self.route))
        self.assertNotIn(b, self.reg.roster(self.pid, self.route))
        notices = self.notices()
        self.assertEqual(len(notices),1)
        self.assertEqual(set(json.loads(notices[0]['payload'])['names']), {'A','B'})
        self.assertEqual(self.reg.sweep_archives(now=100+4*86400), 0)
        self.assertEqual(len(self.notices()),1)
        delivered = []
        self.assertTrue(self.reg.dispatch(lambda *a,**kw:delivered.append(a) or True))
        self.assertFalse(self.reg.dispatch(lambda *a,**kw:self.fail('Archive notice replayed')))

    def test_cap_archives_lru_idle_not_last_created(self):
        a = self.create('A', 100)
        b = self.create('B', 200)
        self.submit(a)
        task = self.reg.claim()
        with patch('project_sessions.registry.time.time', return_value=300):
            self.reg.finish(task['run_id'], 'completed', result='done')
        c = self.create('C', 400)
        self.assertIsNone(self.card(a)['archived_at'])
        self.assertIsNotNone(self.card(b)['archived_at'])
        self.assertIsNone(self.card(c)['archived_at'])
        self.assertEqual(json.loads(self.notices()[0]['payload'])['names'],['B'])

    def test_cap_refuses_when_all_busy_without_side_effects(self):
        a = self.create('A', 100); b = self.create('B', 200)
        self.submit(a,'a'); self.submit(b,'b')
        with self.assertRaisesRegex(WorkstreamStateError, 'no idle subsession'):
            self.create('C', 400)
        self.assertEqual(len(self.reg.inspect(self.pid,self.route)['workstreams']),2)
        self.assertEqual(self.notices(),[])

    def test_running_queued_approval_uncertain_excluded(self):
        self.reg.max_active_workstreams=10
        streams = [self.create(n,100) for n in ('queued','running','approval','uncertain')]
        for i,wid in enumerate(streams):
            self.submit(wid,str(i))
        with self.reg.connection() as db:
            db.execute("UPDATE tasks SET state='running' WHERE workstream_id IN (?,?)", (streams[1],streams[2]))
            db.execute("UPDATE tasks SET state='uncertain' WHERE workstream_id=?",(streams[3],))
            # Independent pending approval blocks archival even in unusual recovered data.
            task=db.execute('SELECT id FROM tasks WHERE workstream_id=?',(streams[2],)).fetchone()[0]
            db.execute("INSERT INTO runs(id,task_id,state,started) VALUES('run',?,'running',100)",(task,))
            db.execute("INSERT INTO approval_requests(id,task_id,run_id,command,reason,created,deadline) VALUES('approval',?,'run','danger','reason',100,999999)",(task,))
            db.execute("UPDATE tasks SET state='failed' WHERE id=?",(task,))
        self.assertEqual(self.reg.sweep_archives(now=100+4*86400),0)
        for wid in streams:
            with self.assertRaisesRegex(WorkstreamStateError,'Only idle'):
                self.reg.manage_workstreams(self.pid,self.route,action='archive',workstream_id=wid)

    def test_finish_starts_idle_clock(self):
        wid=self.create('A',100)
        self.submit(wid); task=self.reg.claim()
        with patch('project_sessions.registry.time.time',return_value=500):
            self.reg.finish(task['run_id'],'completed',result='done')
        self.assertEqual(self.reg.sweep_archives(now=500+3*86400-1),0)
        self.assertEqual(self.reg.sweep_archives(now=500+3*86400),1)

    def test_manual_archive_search_restore_keeps_history_workspace_and_tip(self):
        wid=self.create('Writer',100)
        self.submit(wid); task=self.reg.claim()
        self.reg.finish(task['run_id'],'completed',session_id='compression-tip',result='Important conclusion about penguins')
        workspace=Path(self.card(wid)['workspace'])
        (workspace/'artifact.txt').write_text('retained')
        self.reg.manage_workstreams(self.pid,self.route,action='archive',workstream_id=wid)
        with self.assertRaisesRegex(WorkstreamStateError,'restore before submitting'):
            self.submit(wid,'two')
        matches=self.reg.manage_workstreams(self.pid,self.route,query='penguins')['archived_cards']
        self.assertEqual(matches[0]['id'],wid)
        self.assertEqual(self.reg.manage_workstreams(self.pid,self.route,query='absent')['archived_cards'],[])
        with patch('project_sessions.registry.time.time',return_value=1000000):
            restored=self.reg.manage_workstreams(self.pid,self.route,action='restore',workstream_id=wid)
        self.assertEqual(restored['session_id'],'compression-tip')
        self.assertEqual(restored['workspace'],str(workspace))
        self.assertEqual(len(restored['updates']),1)
        self.assertEqual((workspace/'artifact.txt').read_text(),'retained')
        self.assertIn(wid,self.reg.roster(self.pid,self.route))
        self.assertEqual(self.reg.sweep_archives(now=1000001),0)
        self.submit(wid,'two')
        self.assertEqual(self.reg.claim()['session_id'],'compression-tip')
        self.assertEqual(self.notices(),[])  # manual lifecycle changes don't announce automatically

    def test_restore_obeys_cap_and_lru(self):
        a=self.create('A',100); self.reg.manage_workstreams(self.pid,self.route,action='archive',workstream_id=a)
        b=self.create('B',200); c=self.create('C',300)
        self.reg.manage_workstreams(self.pid,self.route,action='restore',workstream_id=a)
        self.assertIsNotNone(self.card(b)['archived_at'])
        self.assertIsNone(self.card(a)['archived_at'])
        self.assertIsNone(self.card(c)['archived_at'])
        self.assertEqual(len(self.notices()),1)
        self.reg.manage_workstreams(self.pid,self.route,action='restore',workstream_id=a)
        self.assertEqual(len(self.notices()),1)

    def test_restore_all_busy_refuses_atomically(self):
        a=self.create('A',100); self.reg.manage_workstreams(self.pid,self.route,action='archive',workstream_id=a)
        b=self.create('B',200); c=self.create('C',300)
        self.submit(b,'b'); self.submit(c,'c')
        with self.assertRaises(WorkstreamStateError):
            self.reg.manage_workstreams(self.pid,self.route,action='restore',workstream_id=a)
        self.assertIsNotNone(self.card(a)['archived_at'])
        self.assertEqual(self.notices(),[])

    def test_compression_after_new_keeps_new_project_not_old(self):
        import sqlite3
        with sqlite3.connect(self.home/'state.db') as db:
            db.execute('CREATE TABLE sessions(id TEXT,parent_session_id TEXT,end_reason TEXT,model_config TEXT)')
            db.executemany('INSERT INTO sessions VALUES(?,?,?,?)',[
                ('main',None,'new_session','{}'),('new','main','compression','{"_reset_from":"main"}'),
                ('new-compressed','new',None,'{}'),('unproven-parent',None,None,'{}'),('unproven','unproven-parent',None,'{}')])
        new=self.reg.ensure_project(self.route,'new')['id']
        self.assertNotEqual(new,self.pid)
        self.assertEqual(self.reg.ensure_project(self.route,'new-compressed')['id'],new)
        self.assertTrue(self.reg.is_current_conversation(new,self.route,'new-compressed'))
        self.assertFalse(self.reg.is_current_conversation(self.pid,self.route,'new-compressed'))
        separate=self.reg.ensure_project(self.route,'unproven-parent')['id']
        self.assertFalse(self.reg.is_current_conversation(separate,self.route,'unproven'))

    def test_settings_and_lowered_cap_sweep_and_route_authority(self):
        for kwargs in ({'archive_idle_days':0},{'archive_idle_days':float('nan')},{'max_active_workstreams':True},{'max_active_workstreams':0}):
            with self.assertRaises(ValueError):
                Registry(self.reg.path,self.home,**kwargs)
        self.reg.max_active_workstreams=4
        a=self.create('A',100); b=self.create('B',200); c=self.create('C',300)
        self.reg.max_active_workstreams=2
        self.assertEqual(self.reg.sweep_archives(now=301),1)
        self.assertIsNotNone(self.card(a)['archived_at'])
        with self.assertRaises(ValueError):
            self.reg.manage_workstreams(self.pid,'telegram:other',action='restore',workstream_id=a)
        self.reg.archive_idle_seconds=1
        self.assertEqual(self.reg.sweep_archives(now=302),2)

    def test_schema_five_migration_preserves_evidence(self):
        import sqlite3
        path=self.home/'old.db'
        with sqlite3.connect(path) as db:
            for version in range(1,6):
                db.executescript((ROOT/f'project_sessions/migrations/{version:03}.sql').read_text())
            db.execute('UPDATE meta SET profile_home=?',(str(self.home),))
            db.execute('INSERT INTO projects VALUES(?,?,?,?,?)',('p',str(self.home),self.route,'native-main',100))
            db.execute("INSERT INTO conversation_aliases VALUES(?,'native-main','p')",(self.route,))
            db.execute("INSERT INTO workstreams(id,project_id,scope,workspace,session_id,name) VALUES('w','p','old',?,'worker-tip','old')",(str(self.home),))
            db.execute("INSERT INTO tasks(id,workstream_id,request_key,input,criteria,authorization,state,created) VALUES('t','w','k','input','criteria','scope','completed',200)")
            db.execute("INSERT INTO runs(id,task_id,state,started,ended,session_id) VALUES('r','t','completed',200,250,'worker-tip')")
        import time
        before=time.time()
        reg=Registry(path,self.home)
        c=reg.card('p','w',self.route)
        self.assertEqual((c['created'],c['last_used']),(100,250))
        self.assertGreaterEqual(c['idle_since'],int(before))
        self.assertLessEqual(c['idle_since'],time.time())
        self.assertEqual(reg.sweep_archives(),0)
        self.assertEqual(c['session_id'],'worker-tip')
        self.assertEqual(reg.ensure_project(self.route,'native-main')['id'],'p')

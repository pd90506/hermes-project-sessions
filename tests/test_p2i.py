import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

import hermes_env  # noqa: F401  (creates the scratch directory)
from project_sessions.registry import Registry, WorkstreamStateError

ROOT = Path(__file__).resolve().parents[1]


class ProjectRequestKeys(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT/'scratch')
        self.home = Path(self.tmp.name)
        self.route = 'telegram:keys'
        self.reg = Registry(self.home/'registry.db',self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def submit(self, project, key='same', **changes):
        args = dict(project_id=project, workstream_id=None, request_key=key, text='Research',
                    criteria='Report',authorization='Read only',route=self.route,
                    new_workstream={'scope':'Research','name':'Research'})
        return self.reg.submit(**{**args, **changes})

    def test_concurrent_reuse_is_independent_between_projects(self):
        projects = [self.reg.ensure_project(self.route,s)['id'] for s in ('old','new')]
        barrier = threading.Barrier(4)
        results, errors = [], []
        def submit(project):
            try:
                barrier.wait()
                results.append((project,self.submit(project)['id']))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=submit,args=(p,)) for p in projects*2]
        for t in threads:t.start()
        for t in threads:t.join(timeout=10)
        self.assertEqual(errors,[])
        self.assertEqual(len(results),4)
        for project in projects:
            ids={task for p,task in results if p==project}
            self.assertEqual(len(ids),1)
            self.assertEqual(self.submit(project)['id'],next(iter(ids)))
            with self.assertRaises(WorkstreamStateError):
                self.submit(project,text='Changed')
        self.assertEqual(len({task for _,task in results}),2)

    def test_populated_schema_nine_keeps_retries_and_references(self):
        path=self.home/'v9.db'
        with sqlite3.connect(path) as db:
            for version in range(1,10):
                db.executescript((ROOT/f'project_sessions/migrations/{version:03}.sql').read_text())
            db.execute('UPDATE meta SET profile_home=?',(str(self.home),))
            db.execute('INSERT INTO projects VALUES(?,?,?,?,?)',('p',str(self.home),self.route,'old',1))
            db.execute("INSERT INTO workstreams(id,project_id,scope,name,workspace,session_id) VALUES('w','p','Research','Research',?,'native-tip')",(str(self.home),))
            db.execute("INSERT INTO tasks(id,workstream_id,request_key,input,criteria,authorization,state,created) VALUES('t','w','same','Research','Report','Read only','completed',2)")
            db.execute('INSERT INTO delegation_requests VALUES(?,?)',('same',json.dumps({'scope':'Research','name':'Research'})))
            db.execute("INSERT INTO runs(id,task_id,state,started,ended,session_id) VALUES('r','t','completed',2,3,'native-tip')")
            db.execute("INSERT INTO card_updates VALUES('t','{}')")
            db.execute("INSERT INTO approval_requests(id,task_id,run_id,command,reason,created,deadline) VALUES('a','t','r','echo ok','why',2,4)")
            db.execute("INSERT INTO approval_cards(approval_id,token) VALUES('a','token')")
        self.reg=Registry(path,self.home)
        self.assertEqual(self.submit('p')['id'],'t')
        new=self.reg.ensure_project(self.route,'new')['id']
        self.assertNotEqual(self.submit(new)['id'],'t')
        self.reg=Registry(path,self.home)
        self.assertEqual(self.submit('p')['id'],'t')
        self.assertEqual(self.reg.inspect('p',self.route)['workstreams'][0]['session_id'],'native-tip')
        with self.reg.connection() as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT task_id FROM delegation_requests WHERE task_id=?',('t',)).fetchone()[0],'t')
            self.assertEqual(db.execute('SELECT task_id FROM runs WHERE id=?',('r',)).fetchone()[0],'t')
            self.assertEqual(db.execute('SELECT approval_id FROM approval_cards').fetchone()[0],'a')

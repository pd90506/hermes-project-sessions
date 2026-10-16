"""Unambiguous stream names and migration hygiene."""
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

import hermes_env  # noqa: F401  (creates the scratch directory)
from project_sessions.registry import Registry

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / 'project_sessions/migrations'


class StreamNameTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'scratch').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'scratch')
        self.home = Path(self.tmp.name)
        self.route = 'telegram:names'
        self.reg = Registry(self.home / 'plugin-data/project-sessions/registry.db', self.home)
        self.pid = self.reg.ensure_project(self.route, 'main')['id']

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, name, scope='scope', project=None):
        return self.reg.create_workstream(project or self.pid, scope, self.route, name=name)

    def test_duplicate_names_in_a_project_are_auto_suffixed(self):
        # Retried attempts produced "memory-inplace, memory-inplace, …" in one notice.
        first = self.create('memory-inplace')
        self.assertEqual(first['name'], 'memory-inplace')
        self.assertEqual(self.create('memory-inplace')['name'], 'memory-inplace (2)')
        # Folded whitespace/case would read as the same name to the user.
        self.assertEqual(self.create(' Memory-InPlace ')['name'], 'Memory-InPlace (3)')
        # Archived names stay reserved, so archive notices and archived-card searches are unambiguous.
        self.reg.manage_workstreams(self.pid, self.route, action='archive', workstream_id=first['workstream_id'])
        self.assertEqual(self.create('memory-inplace')['name'], 'memory-inplace (4)')
        restored = self.reg.manage_workstreams(self.pid, self.route, action='restore', workstream_id=first['workstream_id'])
        self.assertEqual(restored['name'], 'memory-inplace')
        # Suffix stays inside the 120-char name bound the roster displays.
        long = 'x' * 120
        self.assertEqual(self.create(long)['name'], long)
        second = self.create(long)['name']
        self.assertLessEqual(len(second), 120)
        self.assertTrue(second.endswith(' (2)'))
        # Other projects are independent.
        other = self.reg.ensure_project(self.route, 'other-main')['id']
        self.assertEqual(self.create('memory-inplace', project=other)['name'], 'memory-inplace')

    def test_default_name_from_scope_is_unique_too(self):
        self.assertEqual(self.create(None, scope='Fix the parser')['name'], 'Fix the parser')
        self.assertEqual(self.create(None, scope='Fix the parser')['name'], 'Fix the parser (2)')

    def test_upgrade_disambiguates_legacy_duplicate_names(self):
        path = self.home / 'seven.db'
        with sqlite3.connect(path) as db:
            for version in range(1, 8):
                db.executescript((MIGRATIONS / f'{version:03}.sql').read_text())
            db.execute('UPDATE meta SET profile_home=?', (str(self.home),))
            db.execute('INSERT INTO projects VALUES(?,?,?,?,?)', ('p', str(self.home), self.route, 'old', 100))
            db.execute('INSERT INTO projects VALUES(?,?,?,?,?)', ('q', str(self.home), self.route, 'older', 90))
            for wid, project, name, created in [('a1', 'p', 'dup', 1), ('b2', 'p', 'DUP', 2), ('c3', 'p', 'dup', 3),
                                                ('d4', 'p', 'solo', 4), ('e5', 'q', 'dup', 5), ('f6', 'p', 'y' * 120, 6),
                                                ('g7', 'p', 'y' * 120, 7)]:
                db.execute('INSERT INTO workstreams(id,project_id,scope,workspace,name,created,idle_since,last_used) VALUES(?,?,?,?,?,?,?,?)',
                           (wid, project, 's', str(self.home), name, created, created, created))
        reg = Registry(path, self.home)
        with reg.connection() as db:
            names = dict(db.execute('SELECT id,name FROM workstreams'))
        self.assertEqual((names['a1'], names['d4'], names['e5'], names['f6']), ('dup', 'solo', 'dup', 'y' * 120))
        self.assertEqual((names['b2'], names['c3']), ('DUP (b2)', 'dup (c3)'))
        self.assertLessEqual(len(names['g7']), 120)
        self.assertTrue(names['g7'].endswith(' (g7)'))
        # New names keep avoiding the disambiguated legacy ones.
        self.assertEqual(reg.create_workstream('p', 's', self.route, name='dup')['name'], 'dup (2)')

    def test_one_omitted_roster_entry_is_singular(self):
        reg = Registry(self.home / 'cap.db', self.home, max_active_workstreams=1000)
        pid = reg.ensure_project(self.route, 'cap-main')['id']
        for index in range(60):
            reg.create_workstream(pid, 'scope ' + 'z' * 150, self.route, name=f'n{index}-' + 'w' * 100)
        lines = reg.roster(pid, self.route).splitlines()
        omitted = int(lines[-1].split()[0])
        with reg.connection() as db:
            db.execute('UPDATE workstreams SET archived_at=1 WHERE rowid IN (SELECT rowid FROM workstreams ORDER BY rowid DESC LIMIT ?)', (omitted - 1,))
        self.assertEqual(reg.roster(pid, self.route).splitlines()[-1], '1 more active subsession omitted; use project_status.')

    def test_migration_006_is_unchanged_since_it_shipped(self):
        # Databases that applied 006 never re-run it, and the idle grace lives in 007, so the
        # shipped file must stay byte-identical (SHA-256 of the released file).
        digest = hashlib.sha256((MIGRATIONS / '006.sql').read_bytes()).hexdigest()
        self.assertEqual(digest, '421a2197a00fefa8c700ffbe8bca28b056f265e8a7f9ba0013776a2afe6d3804')

if __name__ == '__main__':
    unittest.main()

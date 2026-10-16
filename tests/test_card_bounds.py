"""Worker card parsing cost/bounds and main-context size."""
import json
from pathlib import Path
import tempfile
import time
import unittest

import hermes_env  # noqa: F401  (creates the scratch directory)
from project_sessions.registry import Registry
from project_sessions.cards import parse_card

ROOT = Path(__file__).resolve().parents[1]


class CardBoundsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'scratch')
        self.home = Path(self.tmp.name)
        self.reg = Registry(self.home / 'registry.db', self.home, max_active_workstreams=50)
        self.route = 'telegram:cards'
        self.pid = self.reg.ensure_project(self.route, 'main')['id']

    def tearDown(self):
        self.tmp.cleanup()

    def finish(self, wid, key, result):
        self.reg.submit(self.pid, wid, key, 'task', 'criteria', 'scope', self.route)
        task = self.reg.claim()
        self.reg.finish(task['run_id'], 'completed', result=result)

    def test_adversarial_trailers_parse_in_linear_time(self):
        # Each was 0.7-2.8 s with the regex parser, run inside finish()'s write transaction.
        for text in ('<workstream-card>a' + ' \n' * 15000 + 'b</workstream-card>',
                     '<workstream-card>x' + ' ' * 31000,
                     '<workstream-card>' * 1800):
            started = time.monotonic()
            self.assertFalse(parse_card(text, 'completed')['structured'])
            self.assertLess(time.monotonic() - started, 0.3)

    def test_deeply_nested_trailer_falls_back_and_finish_keeps_result(self):
        text = 'Done.\n<workstream-card>' + '[' * 20000 + '</workstream-card>'
        self.assertFalse(parse_card(text, 'completed')['structured'])
        wid = self.reg.create_workstream(self.pid, 'nested', self.route)['workstream_id']
        self.finish(wid, 'nested', text)
        card = self.reg.card(self.pid, wid, self.route)
        self.assertEqual(card['tasks_done'][0]['state'], 'completed')
        self.assertFalse(card['card']['structured'])

    def test_card_update_is_bounded_and_credentials_screened_before_truncation(self):
        big = {'context': 'c' * 5000, 'conclusions': ['k' * 999] * 20, 'artifacts': ['a' * 999] * 20}
        update = parse_card('<workstream-card>' + json.dumps(big) + '</workstream-card>', 'completed')
        self.assertTrue(update['structured'])
        self.assertLessEqual(len(json.dumps(update)), 8000)
        leaked = {'context': 'x' * 1495 + ' api_key=' + 'v' * 40, 'conclusions': [], 'artifacts': []}
        self.assertFalse(parse_card('<workstream-card>' + json.dumps(leaked) + '</workstream-card>', 'completed')['structured'])

    def test_roster_stays_under_host_spill_threshold(self):
        noisy = '\x01\x02\n' * 40
        for i in range(30):
            wid = self.reg.create_workstream(self.pid, noisy + 'scope' + noisy * 3, self.route, name=noisy[:100] + str(i))['workstream_id']
            with self.reg.connection() as db:
                db.execute('UPDATE workstreams SET card=? WHERE id=?', (json.dumps({'context': noisy * 20 + 'ctx'}), wid))
        roster = self.reg.roster(self.pid, self.route)
        self.assertLess(len(roster), 10000)  # hooks.output_spill.max_chars default
        emitted = self.reg.roster_context(self.pid, self.route, 's' * 512, [])
        self.assertLess(len(emitted['context']), 10000)
        lines = roster.splitlines()
        self.assertIn('omitted; use project_status', lines[-1])
        for line in lines[1:-1]:
            entry = json.loads(line)
            self.assertNotIn('\\u0001', line)
            self.assertLessEqual(len(entry['context']), 240)

    def test_full_card_history_is_bounded(self):
        wid = self.reg.create_workstream(self.pid, 'history', self.route)['workstream_id']
        for i in range(23):
            self.finish(wid, f'h{i}', f'result {i}')
        card = self.reg.card(self.pid, wid, self.route)
        self.assertEqual(len(card['updates']), 20)
        self.assertEqual(card['updates_omitted'], 3)
        self.assertEqual(len(card['tasks_done']), 23)

    def test_reset_racing_compression_is_not_current(self):
        # Host race (gateway/session.py:1137,1150; hermes_state_common.py:189): /new mid-turn after
        # rotation parents the reset child on a compression-ended session; only _reset_from marks it.
        import sqlite3
        with sqlite3.connect(self.home / 'state.db') as db:
            db.execute('CREATE TABLE sessions(id TEXT,parent_session_id TEXT,end_reason TEXT,model_config TEXT)')
            db.executemany('INSERT INTO sessions VALUES(?,?,?,?)', [
                ('main', None, 'compression', '{}'), ('tip', 'main', None, '{}'),
                ('raced-new', 'main', None, '{"_reset_from": "main"}')])
        self.assertTrue(self.reg.is_current_conversation(self.pid, self.route, 'tip'))
        self.assertFalse(self.reg.is_current_conversation(self.pid, self.route, 'raced-new'))
        self.assertNotEqual(self.reg.ensure_project(self.route, 'raced-new')['id'], self.pid)

    def test_populated_schema_four_migrates_to_conversation_projects(self):
        import sqlite3
        path = self.home / 'v4.db'
        with sqlite3.connect(path) as db:
            for version in range(1, 5):
                db.executescript((ROOT / f'project_sessions/migrations/{version:03}.sql').read_text())
            db.execute('UPDATE meta SET profile_home=?', (str(self.home),))
            now = time.time()
            db.execute('INSERT INTO projects VALUES(?,?,?,?,?,?)', ('p', '/old/root', str(self.home), self.route, 'native-main', now))
            for i in range(12):
                db.execute("INSERT INTO workstreams(id,project_id,scope,workspace,coordination_roots) VALUES(?,'p',?,?,?)",
                           (f'w{i}', f'scope {i}', str(self.home), json.dumps([str(self.home)])))
            db.execute("INSERT INTO tasks(id,workstream_id,request_key,input,criteria,authorization,state,created) VALUES('t','w0','k','input','criteria','scope','queued',?)", (now,))
            db.execute("INSERT INTO main_routes VALUES(?,?,100)", (self.route, json.dumps({'platform': 'telegram', 'chat_id': '1', 'user_id': '2'})))
        reg = Registry(path, self.home, max_active_workstreams=10)
        with reg.connection() as db:
            self.assertEqual(db.execute('SELECT version FROM meta').fetchone()[0], 11)
            self.assertEqual(db.execute('SELECT cwd FROM legacy_directory_bindings').fetchone()[0], '/old/root')
        self.assertEqual(reg.ensure_project(self.route, 'native-main')['id'], 'p')
        self.assertEqual(reg.route_origin(self.route)['chat_id'], '1')
        # A recent legacy project over the cap is reconciled by the sweep, never archiving queued work.
        # Migration now seeds a fresh idle grace; capacity reconciliation still applies.
        self.assertEqual(reg.sweep_archives(), 2)
        self.assertEqual(reg.card('p', 'w0', self.route)['state'], 'queued')
        self.assertEqual(len(reg.roster('p', self.route).splitlines()), 11)


if __name__ == '__main__':
    unittest.main()

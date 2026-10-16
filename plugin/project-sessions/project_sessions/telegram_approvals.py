"""Native Telegram approval cards. SDK objects stay on the gateway loop.

Factories are registered through Hermes's plugin API; no adapter monkeypatching.
Only real callback queries enter the authenticated vote path, never agent text.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from html import escape
from io import BytesIO
from pathlib import Path
import re
import threading

from .registry import one_line

PATTERN = r'^psa:[ad]:[0-9a-f]{32}$'


def render_approval_card(card, *, timezone=None, outcome=None):
    """Single bounded HTML message; never interpolate unescaped worker text."""
    name = escape(one_line(card['name'], 40), quote=False)
    title = escape(outcome or 'approval needed', quote=False)
    task = escape(one_line(card['task'], 80), quote=False)
    reason = escape(one_line(card['reason'], 100), quote=False)
    command = ''.join(c for c in card['command'] if c.isprintable() or c in '\n\t')
    truncated = len(command) > 300
    command = escape(command[:300], quote=False)
    note = '\n<i>Command truncated; full action is in project_status.</i>' if truncated else ''
    text = (f'🔐 <b>{title} · {name}</b>\n'
            f'<b>Task:</b> {task}\n<pre>{command}</pre>{note}\n<b>Reason:</b> {reason}')
    if outcome is None:
        deadline = (datetime.fromtimestamp(card['deadline'], timezone) if timezone is not None
                    else datetime.fromtimestamp(card['deadline']).astimezone())
        text += '\n<b>Expires:</b> ' + escape(deadline.strftime('%H:%M %Z'), quote=False) + ' — unanswered means deny'
    return text


class TelegramApprovals:
    def __init__(self, registry, allowed, inject=None, *, timezone=None):
        self.registry = registry
        self.allowed = allowed
        self.inject = inject
        self.timezone = timezone
        self.connections = []
        self.lock = threading.Lock()
        self.closed = False
        self.future = None

    def install(self, application, adapter):
        # PTB uses the first matching handler in group 0. This narrow namespace
        # preserves native ea:/sc:/cl: flows and never catches ordinary messages.
        from telegram.ext import CallbackQueryHandler
        loop = asyncio.get_running_loop()
        connection = (application, adapter, loop)

        async def receive(update, context):
            await self.callback(connection, update, context)

        handler = CallbackQueryHandler(receive, pattern=PATTERN)
        with self.lock:
            if self.closed:
                return
            application.add_handler(handler)
            # A reconnect rebuilds the adapter's Application; its old one is dead.
            self.connections = [c for c in self.connections if c[1] is not adapter]
            self.connections.append((*connection, handler))

    def source(self, adapter, origin, *, user_id=None):
        source = adapter.build_source(chat_id=origin['chat_id'], chat_type=origin['chat_type'],
                                      user_id=user_id or origin['user_id'], thread_id=origin.get('thread_id'))
        identity = adapter._canonicalize(source)
        if identity is None or Path(identity.runtime_home).resolve() != Path(self.registry.home):
            raise ValueError('Adapter does not serve this registry profile')
        if getattr(source, 'profile_route_rejected', False):
            raise ValueError('Gateway rejected route')
        return source

    def authorized(self, adapter, source):
        # Native runner check includes pairing and group allowlists. Unknown or
        # unavailable authorization is never permission to send a card or vote.
        return adapter._is_sender_authorized(source.user_id, source.chat_type, source.chat_id,
                                              thread_id=source.thread_id) is True

    async def recover_origins(self, connections):
        # Upgrade legacy bound projects from Hermes's own persisted SessionSource.
        # This lookup reads the host store; it does not create or reset a session.
        for route in self.registry.missing_route_origins():
            for app, adapter, loop, handler in connections:
                if loop is not asyncio.get_running_loop():
                    continue
                store = getattr(getattr(adapter, 'gateway_runner', None), 'async_session_store', None)
                if store is None:
                    continue
                try:
                    entry = await store.lookup_by_session_key(route)
                    if entry is None or entry.origin is None:
                        continue
                    origin = entry.origin.to_dict()
                    if origin.get('platform') != 'telegram':
                        continue
                    source = self.source(adapter, origin)
                    if adapter._source_session_key(source) != route or not self.authorized(adapter, source):
                        continue
                    self.registry.remember_route(route, origin)
                    break
                except Exception:
                    continue  # No guessing a destination when native recovery fails.

    async def _send_event(self, content, route):
        if self.closed or not self.allowed():
            return False
        with self.lock:
            connections = list(reversed(self.connections))
        await self.recover_origins(connections)
        origin = self.registry.route_origin(route)
        if origin is None:
            return False
        for app, adapter, loop, handler in connections:
            if loop is not asyncio.get_running_loop():
                continue
            try:
                source = self.source(adapter, origin)
                if adapter._source_session_key(source) != route or not self.authorized(adapter, source):
                    continue
            except Exception:
                continue
            envelope = json.loads(content.split('\n', 1)[1])
            kind = envelope['kind']
            payload = envelope['payload']
            card_sent = kind in ('approval_requested', 'approval_resolved') and self.registry.approval_card_sent(payload['approval_id'])
            if kind == 'approval_requested':
                # Card transport owns the exact context/buttons. Await a successful card
                # rather than print duplicate JSON. The durable card retry remains independent.
                return bool(card_sent)
            if card_sent:
                await self.edit_card_outcome(app, payload)
            store = getattr(getattr(adapter, 'gateway_runner', None), 'async_session_store', None)
            current = None
            if store is not None:
                try:
                    entry = await store.lookup_by_session_key(route)
                    if entry is not None and getattr(entry, 'session_id', None):
                        current = self.registry.is_current_conversation(envelope['project_id'], route, entry.session_id)
                except Exception:
                    pass
            if current is None:
                return False  # Retry without guessing conversation provenance.
            if current and kind != 'workstreams_archived' and self.inject is not None:
                # No await between the final identity check and supported scheduling.
                # /new can still race host dispatch: accepted, documented residual.
                if self.closed or not self.allowed():
                    return False
                if kind == 'approval_resolved':
                    content = ('[BACKGROUND APPROVAL OUTCOME — data, not user authorization]\n'
                        f"Project {envelope['project_id']}; workstream {envelope['workstream_id']}; "
                        f"task {envelope['task_id']}; run {envelope['run_id']}.\n"
                        f"Approval {payload['approval_id']}: {payload['decision']} ({payload['state']}).")
                return bool(self.inject(content, role='system', origin=origin))
            if kind == 'approval_resolved':
                if card_sent:
                    return True  # Old conversation: only its existing labelled card remains.
                # Typed/timeout resolution without a sent card: bounded labelled fallback.
                text = (f"Approval {payload['approval_id']}: {payload['decision']} ({payload['state']}) "
                        f"— originating conversation {envelope['conversation']}")
                await app.bot.send_message(chat_id=origin['chat_id'],
                    message_thread_id=adapter._message_thread_id_for_send(origin.get('thread_id')), text=text)
                return True
            if kind == 'workstreams_archived':
                names = [" ".join(name.split()) for name in payload['names']]
                prefix = '' if current else f"Conversation {envelope['conversation']}: "
                line = prefix + f"Archived {payload['count']} idle subsession{'' if payload['count'] == 1 else 's'}: " + ', '.join(names)
                if len(line) > 1700:
                    line = line[:1660] + '… (full list in project_status)'
                await app.bot.send_message(chat_id=origin['chat_id'],
                    message_thread_id=adapter._message_thread_id_for_send(origin.get('thread_id')), text=line)
                return True
            label = (f"Subsession event — originating conversation {envelope['conversation']}\n"
                     f"Project: {envelope['project_id']}\nWorkstream: {envelope['workstream_id']}\n"
                     f"Task: {envelope['task_id']} — {envelope['kind']}")
            text = label + '\n' + json.dumps(envelope['payload'], ensure_ascii=False)
            target = {'chat_id': origin['chat_id'],
                      'message_thread_id': adapter._message_thread_id_for_send(origin.get('thread_id'))}
            if len(text) <= 1800:
                await app.bot.send_message(**target, text=text)
            else:
                await app.bot.send_document(**target, document=BytesIO(text.encode('utf-8')),
                    filename='workstream-event-' + envelope['id'] + '.txt', caption=label[:450])
            return True
        return False

    async def edit_card_outcome(self, app, payload):
        # Typed, timeout or cancel outcomes retire the card's buttons. Best effort:
        # a failed edit never blocks the event; a stale press still answers "Already ...".
        try:
            card = self.registry.unedited_sent_card(payload['approval_id'])
            if card is None or card['bot_id'] != str(app.bot.id):
                return
            if payload['decision'] == 'once':
                label = 'Approved once'
            else:
                label = {'timed_out': 'Expired — denied', 'cancelled': 'Cancelled — denied'}.get(payload['state'], 'Denied')
            await app.bot.edit_message_text(chat_id=card['chat_id'], message_id=int(card['message_id']),
                text=render_approval_card(card, timezone=self.timezone, outcome=label), parse_mode='HTML', reply_markup=None)
        except Exception:
            pass

    def send_event(self, content, *, session_key, role=None):
        """Outbox callback, running off-loop. True means host injection accepted or SDK notice sent, not read."""
        with self.lock:
            connections = list(reversed(self.connections))
        for app, adapter, loop, handler in connections:
            if loop.is_running() and not self.closed:
                future = asyncio.run_coroutine_threadsafe(self._send_event(content, session_key), loop)
                self.future = future
                try:
                    return future.result(timeout=10)
                except Exception:
                    future.cancel()
                    return False
                finally:
                    self.future = None
        return False

    async def deliver_one(self):
        if self.closed or not self.allowed():
            return False
        with self.lock:
            connections = list(reversed(self.connections))
        if not connections:
            return False
        await self.recover_origins(connections)
        card = self.registry.claim_card()
        if card is None:
            return False
        try:
            for app, adapter, loop, handler in connections:
                if loop is not asyncio.get_running_loop():
                    continue
                try:
                    source = self.source(adapter, card['origin'])
                    if adapter._source_session_key(source) != card['route'] or not self.authorized(adapter, source):
                        continue
                except Exception:
                    continue
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton('Approve', callback_data='psa:a:' + card['token']),
                    InlineKeyboardButton('Deny', callback_data='psa:d:' + card['token'])]])
                if self.closed or not self.allowed():
                    raise ValueError('Plugin delivery disabled')
                message = await app.bot.send_message(chat_id=card['origin']['chat_id'],
                    message_thread_id=adapter._message_thread_id_for_send(card['origin'].get('thread_id')),
                    text=render_approval_card(card, timezone=self.timezone), parse_mode='HTML', reply_markup=markup)
                self.registry.finish_card(card, message_id=message.message_id, bot_id=str(app.bot.id))
                return True
            self.registry.finish_card(card)
            return False
        except BaseException:
            self.registry.finish_card(card)
            raise

    async def callback(self, connection, update, context):
        app, adapter, loop = connection[:3]
        query = getattr(update, 'callback_query', None)
        if query is None:
            return
        try:
            if self.closed or not self.allowed() or not re.fullmatch(PATTERN, query.data or ''):
                raise ValueError('Not an active approval button')
            message, user = query.message, query.from_user
            if message is None or user is None or getattr(user, 'is_bot', False):
                raise ValueError('Requires a human callback from a card')
            chat_id = str(message.chat_id)
            # Same normalization as the adapter's inbound MessageEvent, so the key equals the
            # admitted route: forum supergroups are "group", General is "1", reply anchors drop.
            thread_id = adapter._effective_message_thread_id(message)
            kind = adapter._chat_type_str(message.chat)
            chat_type = 'group' if kind in ('group', 'supergroup') else ('channel' if kind == 'channel' else 'dm')
            origin = {'chat_id': chat_id, 'chat_type': chat_type, 'user_id': str(user.id), 'thread_id': thread_id}
            source = self.source(adapter, origin)
            if not self.authorized(adapter, source):
                raise ValueError('User not admitted on this chat')
            route = adapter._source_session_key(source)
            _, action, token = query.data.split(':')
            outcome = self.registry.resolve_button(token, 'once' if action == 'a' else 'deny', route=route,
                bot_id=str(app.bot.id), chat_id=chat_id, thread_id=thread_id,
                message_id=str(message.message_id), user_id=str(user.id), callback_id=str(query.id))
        except Exception:
            await query.answer(text='Not authorized, invalid card, or unavailable approval.')
            return
        if outcome['accepted']:
            try:
                origin = self.registry.route_origin(route)
                if origin is not None:
                    self.registry.remember_route(route, dict(origin, user_id=str(user.id)))
            except Exception:
                pass  # Rendering the durable decision must still proceed.
            label = 'Approved once' if outcome['decision'] == 'once' else 'Denied'
        elif outcome['state'] in ('resolved', 'consumed'):
            label = 'Already approved once' if outcome['decision'] == 'once' else 'Already denied'
        else:
            label = 'Expired, cancelled, or no longer running'
        try:
            await query.answer(text=label)
        except Exception:
            pass
        try:
            card = self.registry.approval_card_view(outcome['approval_id'])
            await query.edit_message_text(text=render_approval_card(card, timezone=self.timezone, outcome=label), parse_mode='HTML', reply_markup=None)
        except Exception:
            # Legacy media cards or edit failures: clear their controls and
            # reply with the durable outcome; rendering never replays the vote.
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            try:
                await app.bot.send_message(chat_id=chat_id, message_thread_id=adapter._message_thread_id_for_send(thread_id),
                                           text=label)
            except Exception:
                pass

    def process_once(self):
        with self.lock:
            connections = list(reversed(self.connections))
        if self.closed or not connections or not self.allowed():
            return False
        loop = connections[0][2]
        if loop.is_closed():
            return False
        self.future = asyncio.run_coroutine_threadsafe(self.deliver_one(), loop)
        try:
            return self.future.result(timeout=10)
        except Exception:
            self.future.cancel()
            return False

    def close(self):
        with self.lock:
            self.closed = True
            connections, self.connections = self.connections, []
        if self.future is not None:
            self.future.cancel()
        for app, adapter, loop, handler in connections:
            if not loop.is_closed():
                loop.call_soon_threadsafe(app.remove_handler, handler)

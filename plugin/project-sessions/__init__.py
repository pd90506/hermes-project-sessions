"""Hermes host adapter. No executor subprocess is started by plugin loading."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

from .project_sessions.registry import Registry, WorkstreamStateError

PROMPT = '''This conversation is a project. Subsessions are full background sessions that keep their history, so the main stays responsive.
Decide this first: anything needing searches, several steps, commands or files goes to a subsession; answer directly only when you can reply right away. When unsure, use a subsession. Reuse a related subsession from the roster, or create one. Relay results when they arrive; send follow-ups to the same subsession.

Call tool_call directly with these shapes, no skill needed (or the named tools if tool_call is unavailable):
New: {"calls":[{"name":"project_submit","arguments":{"new_workstream":{"scope":"what it covers","name":"short name"},"request_key":"unique task key","task":"brief","user_request":"exact user request","criteria":"done when...","authorization":"user-authorized limits"}}]}
Follow-up: {"calls":[{"name":"project_send","arguments":{"workstream_id":"roster ID","task":"brief","user_request":"exact user request"}}]}
Write task from conversation context and user_request verbatim. Set steer=true only for a user redirect.
Status: {"calls":[{"name":"project_status","arguments":{}}]}

Examples:
"Tech deals worth buying?" → new research subsession.
"Rename that file" → direct if quick.
"What did X find?" → relay the result or check status.
"Compare those deals with last week's" → same research subsession.'''


def orchestrator_section(info):
    return PROMPT if info.get('platform') == 'telegram' else ''


def register(ctx):
    worker = os.environ.get('PROJECT_SESSIONS_WORKER')
    if worker:
        directory = Path(worker).resolve(strict=True)
        from .project_sessions.worker_policy import WorkerPolicy
        from tools.approval_context import _get_single_query_approval_mode, _get_approval_mode, _normalize_approval_mode
        from tools.approval import _yolo_active
        from hermes_cli.config import load_config_readonly

        def effective():
            cfg = load_config_readonly()
            expected = _normalize_approval_mode(cfg.get('approvals',{}).get('mode','manual'))
            return (_get_single_query_approval_mode() == 'deny' and _get_approval_mode() == expected
                    and expected in ('manual','smart','off') and (expected == 'off' or not _yolo_active()) and not cfg.get('command_allowlist')
                    and cfg.get('security', {}).get('approval', {}).get('transport', 'builtin') == 'builtin')

        policy = WorkerPolicy(directory, effective)
        ctx.register_hook('pre_tool_call', policy.pre_tool)
        ctx.register_hook('post_tool_call', policy.post_tool)
        ctx.register_middleware('tool_execution', policy.execute)
        if not effective():
            policy.record('startup', {}, 'Approval overlay unavailable')
            return
        (directory / 'guard-ready').touch(mode=0o600)
        return

    from hermes_constants import get_hermes_home
    from tools.approval_context import get_current_session_key, _get_session_platform
    home = Path(get_hermes_home()).resolve()
    registry = Registry(home / 'plugin-data/project-sessions/registry.db', home,
                        archive_idle_days=ctx.get_config('archive_idle_days', 3),
                        max_active_workstreams=ctx.get_config('max_active_workstreams', 10))
    from .project_sessions.reasoning import effort, save_worker_effort, main_request
    save_worker_effort(home, ctx.get_config('worker_reasoning_effort', 'high'), ctx.get_config('worker_max_turns', None))
    main_effort = effort(ctx.get_config('main_reasoning_effort', ''))
    from .project_sessions.lifecycle import shutdown as shutdown_executor
    from .project_sessions.lifecycle import ensure as ensure_executor, status as probe_executor

    def gateway_host():
        manager = ctx._manager
        return manager._cli_ref is None and manager.has_gateway_message_injector

    def executor_status():
        # Only the gateway spawns; other hosts report the real state of the gateway's executor.
        return ensure_executor(home, registry.path) if gateway_host() else probe_executor(registry.path)

    if gateway_host():
        executor_status()

    admitted = set()
    admission_lock = threading.Lock()
    stop = threading.Event()
    from .project_sessions.telegram_approvals import TelegramApprovals
    try:
        from hermes_time import get_timezone
        profile_timezone = get_timezone()  # Capture while register runs in this profile scope.
    except ImportError:
        profile_timezone = None
    buttons = TelegramApprovals(registry, lambda: ctx._gateway_injection_allowed(), ctx.inject_message,
                                timezone=profile_timezone)
    ctx.register_telegram_handler(buttons.install)

    def admission(session_key='', platform='', message_id='', text='', source=None, **kwargs):
        if session_key and platform == 'telegram':
            if source is not None:
                try:
                    registry.remember_route(session_key, source)
                except ValueError:
                    pass  # Unrecordable origin only loses restart recovery, never admission or the typed vote.
            with admission_lock:
                admitted.add(session_key)
            registry.capture_approval_vote(session_key, message_id, text)
        return None

    ctx.register_hook('post_gateway_admission', admission)
    ctx.register_system_prompt_section('project-sessions.orchestrator',
                                       orchestrator_section)
    ctx.register_skill('project-session-orchestration', Path(__file__).parent / 'skills/project-session-orchestration/SKILL.md',
                       description='Reference for approvals, archive and old projects; not needed for routine delegation')

    def route():
        key = get_current_session_key('')
        if _get_session_platform() != 'telegram':
            raise ValueError('Requires Telegram')
        with admission_lock:
            if key not in admitted and not (_get_session_platform() == 'telegram' and registry.is_bound_route(key)):
                raise ValueError('Requires this profile’s admitted Telegram main session')
        return key

    if main_effort:
        def reasoning_request(request, platform='', provider='', api_mode='', model='', **kwargs):
            if platform != 'telegram':
                return None
            try:
                route()  # Same profile/admitted-main scope as the project tools.
            except ValueError:
                return None
            return main_request(request, main_effort, provider=provider, api_mode=api_mode, model=model)
        ctx.register_middleware('llm_request', reasoning_request)

    def brief(args):
        task, original = args.get('task'), args.get('user_request')
        if not isinstance(task, str) or not task.strip() or not isinstance(original, str) or not original.strip():
            raise WorkstreamStateError('task (brief) and user_request (the user\'s exact words) are required.')
        return f"Task: {task}\nUser's original request: {original}"

    def submit_new(args, key, kwargs):
        if 'workstream_id' in args:
            raise WorkstreamStateError('project_submit creates a new subsession; use project_send(workstream_id, task, user_request) for existing work.')
        return registry.submit(args['project_id'],None,args['request_key'],brief(args),args['criteria'],
                               args['authorization'],key,args.get('workspace'),new_workstream=args['new_workstream'])

    actions = {
        'project_workstream_create': (['scope'], lambda a, key, kw: registry.create_workstream(a['project_id'], a['scope'], key, a.get('workspace'), a.get('name'))),
        'project_submit': (['project_id', 'request_key', 'task', 'user_request', 'criteria', 'authorization'],
                           submit_new),
        'project_send': (['workstream_id','task','user_request'], lambda a,key,kw: registry.send(a['project_id'],a['workstream_id'],brief(a),key,a.get('steer',False))),
        'project_workstream_card': ([], lambda a, key, kw: registry.manage_workstreams(a['project_id'], key, action=a.get('action', 'get' if a.get('workstream_id') else 'list'), workstream_id=a.get('workstream_id'), query=a.get('query'))),
        'project_status': (['project_id'], lambda a, key, kw: registry.index(a['project_id'], key)),
        'project_result': (['project_id', 'task_id'], lambda a, key, kw: registry.result(a['project_id'], a['task_id'], key)),
        'project_cancel': (['project_id', 'task_id'], lambda a, key, kw: registry.cancel(a['project_id'], a['task_id'], key)),
        'project_approval_decide': (['project_id', 'approval_id', 'decision'], lambda a, key, kw: registry.decide_approval(a['project_id'], a['approval_id'], a['decision'], key)),
        'project_event_ack': (['project_id', 'event_id'], lambda a, key, kw: registry.acknowledge(a['project_id'], a['event_id'], key)),
    }
    def roster_context(session_id='', platform='', **kwargs):
        if platform != 'telegram':
            return None
        try:
            key = route()
            project = registry.ensure_project(key, session_id)
            return registry.roster_context(project['id'], key, session_id, kwargs.get('conversation_history'))
        except (ValueError, TypeError):
            return None

    ctx.register_hook('pre_llm_call', roster_context)
    for name, (fields, action) in actions.items():
        required = [f for f in fields if f != 'project_id']
        optional = ['project_id']
        if name == 'project_workstream_create':
            optional += ['workspace', 'name']
        if name == 'project_submit':
            optional += ['workspace', 'new_workstream']
        if name == 'project_send':
            optional += ['steer']
        if name == 'project_workstream_card':
            optional += ['workstream_id', 'action', 'query']
        def handler(args, _action=action, _name=name, **kwargs):
            try:
                key = route()
                project_id = args.get('project_id')
                if not project_id:
                    project_id = registry.ensure_project(key, kwargs.get('session_id') or '')['id']
                if _name in ('project_submit', 'project_send', 'project_workstream_create') or (_name == 'project_workstream_card' and args.get('action') == 'restore'):
                    registry.require_current_project(project_id, key, kwargs.get('session_id') or '')
                args = {**args, 'project_id': project_id}
                result = _action(args, key, kwargs)
                if _name in ('project_submit','project_send') or (_name == 'project_workstream_card' and args.get('action') == 'restore'):
                    status = executor_status()
                    if status != 'online':
                        result = {**result, 'executor': status,
                                  'note': ('Executor is offline; queued work resumes when it can restart.' if status == 'offline'
                                           else 'Executor is draining for an upgrade; queued work resumes after replacement.')}
                return json.dumps(result)
            except WorkstreamStateError as exc:
                return json.dumps({'error': str(exc)})
            except (KeyError, ValueError, TypeError, OSError):
                return json.dumps({'error': 'Invalid arguments, authorization, profile, conversation, or route'})
        properties = {f: ({'type': 'string', 'enum': ['once', 'deny']} if f == 'decision' else {'type': 'string', 'enum': ['get', 'list', 'archive', 'restore']} if f == 'action' else {'type': 'string'}) for f in dict.fromkeys(fields + optional)}
        parameters = {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}
        if name in ('project_submit', 'project_send'):
            properties['user_request']['description'] = 'Exact user request, verbatim.'
            properties['task']['description'] = 'Brief from conversation context.'
        if name == 'project_submit':
            properties['new_workstream'] = {'type': 'object', 'properties': {f: {'type': 'string'} for f in ('scope', 'name', 'workspace')}, 'required': ['scope'], 'additionalProperties': False}
            parameters['required'].append('new_workstream')
        if name == 'project_send':
            properties['steer'] = {'type':'boolean','default':False}
        description = {
            'project_workstream_create': 'Create a persistent subsession. Prefer project_submit to create and queue in one call.',
            'project_submit': 'Create a new subsession and queue its first task. Retry identical arguments with the same request_key. Omit workspace for a private default; explicit directories must exist.',
            'project_send': 'Send a message to an existing subsession; busy work queues it. Set steer=true only for an explicit user redirect of the running turn; undelivered steering falls back to the next turn.',
            'project_status': 'Read subsession/task status and pending approval IDs. Defaults to this conversation; project_id selects old work on this route.',
            'project_result': 'Read a task result; project_id can select old work on this route.',
            'project_workstream_card': 'Read a subsession card by workstream_id. action=list searches archived cards with query; archive/restore requires workstream_id. Restore belongs to the current project.',
            'project_cancel': 'Cancel a task, including approval waits; project_id can select old work on this route.',
            'project_approval_decide': 'Apply an authenticated user vote to approval_id: once or deny. Use project_id for old work; this tool cannot invent permission.',
            'project_event_ack': 'Acknowledge a delivered project event.',
        }[name]
        ctx.register_tool(name=name, toolset='project_sessions', schema={'name': name,
            'description': description,
            'parameters': parameters}, handler=handler, check_fn=lambda: True)

    def deliver():
        last_sweep = 0
        while not stop.wait(1):
            # Only the gateway owns delivery; other profile processes leave attempts alone.
            manager = ctx._manager
            try:
                if manager._cli_ref is not None or not manager.has_gateway_message_injector:
                    # Not a delivery host (CLI, cron, -q): leave attempts/backoff to the gateway.
                    continue
                executor_status()
                if time.monotonic() - last_sweep >= 60:
                    registry.sweep_archives()
                    last_sweep = time.monotonic()
                buttons.process_once()
                # Native current-conversation evidence selects injection vs plain notice.
                registry.dispatch(buttons.send_event)
            except Exception:
                # Keep the claim/outbox for retry; do not log task text or exception payloads.
                pass

    thread = threading.Thread(target=deliver, name='project-sessions-outbox', daemon=True)
    ctx.on_unload(lambda: (stop.set(), buttons.close(), thread.join(timeout=2), shutdown_executor(registry.path)))
    thread.start()

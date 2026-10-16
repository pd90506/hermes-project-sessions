"""Worker native approval bridge and process tracking; full profile permissions."""
from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import sqlite3
import sys
import uuid


def policy_overlay(run_dir, approval_timeout=1800):
    # Never replace an administrator scope silently. Merging foreign policy safely is
    # a separate deployment requirement; no profile/admin secrets are copied here.
    existing = Path(os.environ.get('HERMES_MANAGED_DIR') or '/etc/hermes')
    if existing.is_dir() and any((existing / name).exists() for name in ('config.yaml', '.env')):
        raise ValueError('Existing managed scope requires a reviewed composition adapter')
    directory = run_dir / 'managed'
    directory.mkdir(mode=0o700)
    # approvals.timeout bounds Hermes's human-wait credit (approval_human_wait.human_wait_ceiling)
    # against the sequential tool deadline; it must cover the whole escalation window.
    config = {'approvals': {'single_query_mode': 'deny', 'timeout': math.ceil(approval_timeout),
                            'cron_mode': 'deny', 'unattended_mode': 'deny'},
              'command_allowlist': [],
              'security': {'approval': {'transport': 'builtin'}}}
    # Omit mode: Hermes inherits manual/smart/off from the actual profile.
    # JSON is valid YAML. No profile config or credentials are copied.
    for name, content in [('config.yaml', json.dumps(config)), ('.env', 'HERMES_YOLO_MODE=0\n')]:
        with open(directory / name, 'x', encoding='utf-8') as file:
            os.chmod(file.name, 0o600)
            file.write(content)
    return directory


def redacted(value):
    from agent.redact import redact_sensitive_text
    return redact_sensitive_text(str(value), force=True, redact_url_credentials=True)


class WorkerPolicy:
    def __init__(self, directory, effective_policy, redact=redacted):
        self.directory = Path(directory)
        self.data = json.loads((self.directory / 'policy.json').read_text())
        self.cwd = Path(self.data['workspace']).resolve(strict=True)
        self.effective = effective_policy
        self.redact = redact

    def record(self, tool, args, reason):
        digest = hashlib.sha256(json.dumps([tool, args], sort_keys=True, default=str).encode()).hexdigest()
        # Do not retain file content, code, web payloads or credentials.
        context = {key: self.redact(args[key])[:2000] for key in ('command', 'path', 'workdir', 'output_path', 'save_path', 'destination_path', 'reported_path') if key in args}
        context = {key: re.sub(r'(?i)(?:password|api[_-]?key|token|secret)\s*[:=]\s*\S+', '<credential assignment omitted>', value) for key, value in context.items()}
        if args.get('patch'):
            targets = re.findall(r'^\*\*\* (?:(?:Add|Update|Delete) File: |Move to: )(.+)$', args['patch'], re.M)
            context['targets'] = [self.redact(p)[:2000] for p in targets[:20]]
        entry = {'action_id': uuid.uuid4().hex, 'fingerprint': digest, 'tool': tool,
                 'context': context, 'reason': self.redact(reason)[:2000]}
        with open(self.directory / 'actions.lock', 'a') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.directory / 'blocked-actions.json'
            actions = json.loads(path.read_text()) if path.exists() else []
            duplicate = next((a for a in actions if a['fingerprint'] == digest), None)
            if duplicate:
                return duplicate
            if len(actions) < 100:
                actions.append(entry)
                with open(path, 'w') as output:
                    os.chmod(output.name, 0o600)
                    json.dump(actions, output)
            (self.directory / 'needs-input').touch(mode=0o600)
        return entry

    def block(self, tool, args, reason):
        try:
            action = self.record(tool, args, reason)
            suffix = ' action_id=' + action['action_id']
        except Exception:
            suffix = ''
        return {'action': 'block', 'message': 'NEEDS_INPUT: ' + reason + suffix}

    def unwrap(self, command):
        """Original shell command inside this run's tracking launcher, else None."""
        if not isinstance(command, str):
            return None  # Python 3.11 shlex.split(None) would read (and block on) sys.stdin.
        try:
            parts = shlex.split(command)
        except (ValueError, TypeError, AttributeError):
            return None
        if len(parts) == 7 and parts[1] == str(Path(__file__).with_name('sandbox.py')) and parts[2] == str(self.directory / 'policy.json'):
            return parts[-1]
        return None

    def approval(self, command, description, **kwargs):
        """Native callback waits outside the hook pool; one decision for this call only."""
        import time
        command = self.unwrap(command) or command
        from .registry import Registry
        try:
            from tools.interrupt import is_interrupted
            from tools.approval_human_wait import human_wait_window
        except ImportError:  # Protocol fixtures outside Hermes.
            is_interrupted, human_wait_window = (lambda: False), contextlib.nullcontext
        try:
            registry = Registry(self.data['registry_path'], self.data['home'])
            request = registry.request_approval(self.data['run_id'], command, description,
                                                self.data.get('approval_timeout', 1800))
            # Credited against Hermes's sequential tool deadline (ceiling = approvals.timeout + margin).
            with human_wait_window():
                while True:
                    if is_interrupted():
                        # Hermes abandoned this tool call (deadline/stop): a late approval must not run it.
                        registry.withdraw_approval(request)
                        decision = 'deny'
                    else:
                        decision = registry.poll_approval(request)
                    if decision is not None:
                        if decision != 'once':
                            self.record('native_approval', {'command': command}, description + '; denied, expired or cancelled')
                        return decision
                    time.sleep(.05)
        except Exception:
            self.record('native_approval', {'command': command}, 'Approval bridge unavailable; denied')
            return 'deny'

    def execute(self, tool_name, args, next_call, **context):
        from tools.terminal_tool import (set_approval_callback, _get_approval_callback,
                                         set_sudo_password_callback, _get_sudo_password_callback)
        from tools.approval_context import set_hermes_interactive_context, reset_hermes_interactive_context
        previous, previous_sudo = _get_approval_callback(), _get_sudo_password_callback()
        command = args.get('command') if tool_name == 'terminal' else None
        # model_tools.handle_function_call (execute_code kernel RPC) runs pre_tool_call before this
        # middleware, so the command may already be wrapped; native detection misses patterns there.
        command = self.unwrap(command) or command
        if (getattr(previous, '_project_worker', None) == str(self.directory)
                and getattr(previous, '_project_command', None) == command):
            return next_call()  # Re-entry for the same call: already checked.
        token = None
        approved = {}

        def callback(target, reason, **options):
            # The tracking launcher executes exactly this original shell command.
            # Rechecking its wrapper must not ask twice for the same native prompt.
            wrapped = command is not None and self.unwrap(target) == command
            if wrapped and reason in approved:
                return approved[reason]
            decision = self.approval(command if wrapped else target, reason, **options)
            if target == command:
                approved[reason] = decision
            return decision

        callback._project_worker = str(self.directory)
        callback._project_command = command
        try:
            if not self.effective():
                return 'BLOCKED: per-run profile approval policy unavailable'
            # -q normally suppresses callbacks. This standalone worker has a real
            # connected human channel through our durable registry callback.
            os.environ['HERMES_SINGLE_QUERY_SESSION'] = ''
            os.environ['HERMES_INTERACTIVE'] = '1'
            os.environ.pop('HERMES_EXEC_ASK', None)
            set_approval_callback(callback)
            # As in a Telegram session: never prompt for a sudo password (the fallback prompt
            # would print onto the stream-json stdout); sudo needs a configured password.
            set_sudo_password_callback(lambda: '')
            token = set_hermes_interactive_context(True)
        except Exception:
            set_approval_callback(previous)
            set_sudo_password_callback(previous_sudo)
            if token is not None:
                reset_hermes_interactive_context(token)
            return json.dumps(self.block(tool_name, args, 'Approval bridge setup failed'))
        try:
            if command:
                try:
                    from tools.terminal_tool import _get_env_config, _docker_has_host_access
                    from tools.approval import check_all_command_guards
                    config = _get_env_config()
                    self.backend = config['env_type']
                    # Original bytes must reach native detection before wrapping.
                    verdict = check_all_command_guards(command, self.backend, approval_callback=callback,
                                                        has_host_access=_docker_has_host_access(config))
                    if not verdict['approved']:
                        return json.dumps(self.block(tool_name, args, verdict.get('message') or 'Native approval denied'))
                except Exception:
                    return json.dumps(self.block(tool_name, args, 'Native approval bridge failed; command denied'))
            return next_call()
        finally:
            set_approval_callback(previous)
            set_sudo_password_callback(previous_sudo)
            if token is not None:
                reset_hermes_interactive_context(token)

    def pre_tool(self, tool_name='', args=None, **kwargs):
        args = args or {}
        if not self.effective():
            return self.block(tool_name, args, 'Per-run profile approval policy unavailable')
        if tool_name != 'terminal' or not args.get('command') or getattr(self, 'backend', 'local') != 'local':
            return None
        # No permissions are changed. The launcher records Hermes's detached
        # terminal session so cancel/timeout can stop it before releasing locks.
        wrapper = Path(__file__).with_name('sandbox.py')
        wrapped = shlex.join([sys.executable, str(wrapper), str(self.directory / 'policy.json'),
                              '--', '/bin/bash', '-c', args['command']])
        modified = dict(args, command=wrapped)
        workdir = Path(os.path.expanduser(args.get('workdir') or str(self.cwd)))
        modified['workdir'] = str((workdir if workdir.is_absolute() else self.cwd / workdir).resolve())
        return {'action': 'modify', 'args': modified}

    def post_tool(self, tool_name='', args=None, result='', **kwargs):
        if any(mark in str(result) for mark in ('BLOCKED:', 'NEEDS_INPUT:', 'requires approval', 'approval_required')):
            self.record(tool_name, args or {}, 'Native approval/input gate denied the action; user decision required')
        try:
            from .steering import deliver
            from .registry import Registry
            if self.data.get('registry_path'):
                deliver(Registry(self.data['registry_path'],self.data['home']),self.data['run_id'])
        except (ImportError,KeyError,ValueError,OSError,sqlite3.Error):
            pass  # Durable message stays queued if the native boundary is unavailable.

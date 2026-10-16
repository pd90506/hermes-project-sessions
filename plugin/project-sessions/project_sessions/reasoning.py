"""Per-run reasoning settings; no profile mutation or provider calls."""
import json
import os
from pathlib import Path
import tempfile

EFFORTS = {'', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'}


def effort(value):
    if not isinstance(value, str) or value not in EFFORTS:
        raise ValueError('Reasoning effort must be empty or none/minimal/low/medium/high/xhigh/max/ultra')
    return value


def settings_path(home):
    return Path(home) / 'plugin-data/project-sessions/worker-settings.json'


def save_worker_effort(home, value, max_turns=None):
    """Bridge ctx.get_config to the independent executor, atomically and without secrets."""
    value = effort(value)
    max_turns = turn_limit(max_turns)
    path = settings_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.worker-settings-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'worker_reasoning_effort': value, 'worker_max_turns': max_turns}, stream)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def worker_effort(home):
    try:
        value = json.loads(settings_path(home).read_text())['worker_reasoning_effort']
    except FileNotFoundError:
        value = 'high'
    return effort(value)


def turn_limit(value):
    if value is not None and (type(value) is not int or value < 1):
        raise ValueError('worker_max_turns must be a positive integer or null to inherit')
    return value


def worker_max_turns(home):
    try:
        value = json.loads(settings_path(home).read_text()).get('worker_max_turns')
    except FileNotFoundError:
        value = None
    return turn_limit(value)


def main_request(request, configured, *, provider, api_mode, model):
    # Request middleware is supported, but wire formats differ. Use Hermes's own
    # OpenCode Go translator on the deployed route rather than guess other APIs.
    if not configured or provider != 'opencode-go' or api_mode != 'chat_completions':
        return None
    from hermes_constants import parse_reasoning_effort
    from providers import get_provider_profile
    profile = get_provider_profile(provider)
    if profile is None:
        return None
    extra, top = profile.build_api_kwargs_extras(reasoning_config=parse_reasoning_effort(configured), model=model)
    if not extra and not top:
        return None  # Unknown model: retain its native policy.
    result = dict(request)
    body = dict(result.get('extra_body') or {})
    for key in ('reasoning', 'reasoning_effort', 'thinking'):
        result.pop(key, None)
        body.pop(key, None)
    body.update(extra)
    result.update(top)
    if body:
        result['extra_body'] = body
    else:
        result.pop('extra_body', None)
    return {'request': result, 'source': 'project-sessions', 'reason': 'Telegram main reasoning effort'}

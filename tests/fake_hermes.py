#!/usr/bin/env python3
"""Deterministic CLI protocol fixture; never imports Hermes or calls a model."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import shlex
import subprocess
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
argv = sys.argv[1:]
assert argv[0] == 'chat'
assert '-t' not in argv  # Use normal profile toolsets, no empty-toolset substitution.
assert '--no-restore-cwd' in argv
managed = Path(os.environ['HERMES_MANAGED_DIR'])
assert json.loads((managed / 'config.yaml').read_text())['approvals']['single_query_mode'] == 'deny'
assert os.environ.get('HERMES_YOLO_MODE') == '0'
assert '-q' not in argv
home = Path(os.environ['HERMES_HOME'])
home.mkdir(parents=True, exist_ok=True)
run_dir = Path(os.environ['PROJECT_SESSIONS_WORKER'])
(run_dir / 'guard-ready').touch()
query = Path(argv[argv.index('--query-file') + 1]).read_text()
resume = argv[argv.index('--resume') + 1] if '--resume' in argv else None
with sqlite3.connect(home / 'state.db', timeout=10) as db:
    db.execute('CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,parent_session_id TEXT,end_reason TEXT,model_config TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS history(session_id TEXT,text TEXT)')
    if resume:
        assert db.execute('SELECT 1 FROM sessions WHERE id=?', (resume,)).fetchone()
        sid = resume
        prior = [r[0] for r in db.execute('SELECT text FROM history WHERE session_id=?', (sid,))]
    else:
        sid = uuid.uuid4().hex
        db.execute('INSERT INTO sessions VALUES(?,NULL,NULL,?)', (sid, '{}'))
        prior = []
    db.execute('INSERT INTO history VALUES(?,?)', (sid, query))
print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': sid}), flush=True)
if 'TMP_SNAPSHOT' in query:
    # Hermes's terminal snapshot captures env values (provider keys from .env); a killed worker leaves it.
    (Path(os.environ['TMPDIR']) / 'hermes-snap-fake.sh').write_text('export FAKE_PROVIDER_KEY=not-a-real-value\n')
if 'SAME_SESSION_HELPER' in query:
    # Hermes shell hooks use Popen(process_group=0): own group, still the worker's session;
    # "hooks that finish in time keep detached helpers alive" (agent/shell_hooks.py).
    helper = subprocess.Popen(['/bin/sh', '-c', 'exec sleep 30'], process_group=0,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (run_dir / 'helper.pid').write_text(str(helper.pid))
if 'SLOW' in query:
    time.sleep(1)
if 'HANG' in query:
    time.sleep(30)
if 'INIT_ONLY' in query:
    sys.exit(0)
if 'BAD_PROTOCOL' in query:
    print('{not-json-protocol', flush=True)
    time.sleep(.2)
if 'STATUS_LINE' in query:
    # Hermes's quiet-mode status print during auto-compression.
    print('  ⟳ compacting context…', flush=True)
    time.sleep(.2)
if 'STATUS_FLOOD' in query:
    for _ in range(300):
        print('stray text', flush=True)
    time.sleep(.2)
if 'REAL_WORK' in query:
    target = Path.cwd() / 'worker-output.txt'
    target.write_text('real fake-worker filesystem effect')
    print(json.dumps({'type': 'tool_use', 'name': 'write_file', 'input': {'path': str(target)}}), flush=True)
    print(json.dumps({'type': 'tool_result', 'name': 'write_file', 'output': '{"success": true}'}), flush=True)
if 'DENY_ACTION' in query:
    from project_sessions.worker_policy import WorkerPolicy
    policy = WorkerPolicy(run_dir, lambda: True, redact=str)
    policy.record('terminal', {'command': 'rm -rf ./valuable-data'}, 'Native approval denied')
if 'RUNTIME_APPROVAL' in query:
    from project_sessions.worker_policy import WorkerPolicy
    policy = WorkerPolicy(run_dir, lambda: True, redact=str)
    if policy.approval('rm -rf ./valuable-data', 'recursive delete') == 'once':
        (Path.cwd() / 'approved.txt').write_text('same run')
    else:
        print(json.dumps({'type': 'tool_result', 'name': 'terminal', 'output': 'BLOCKED: approval denied'}), flush=True)
if 'OUTSIDE_WRITE' in query:
    from project_sessions.worker_policy import WorkerPolicy
    policy = WorkerPolicy(run_dir, lambda: True, redact=str)
    command = 'printf forbidden > ' + shlex.quote(str(Path.cwd().parent / 'outside-file.txt'))
    directive = policy.pre_tool('terminal', {'command': command})
    attempted = subprocess.run(directive['args']['command'], shell=True, text=True, capture_output=True)
    policy.post_tool('terminal', {'command': command}, attempted.stderr)
    print(json.dumps({'type': 'tool_result', 'name': 'terminal', 'output': attempted.stderr}), flush=True)
if 'DETACHED_TERMINAL' in query:
    # Hermes's local backend starts each terminal command with start_new_session=True,
    # so the command is outside this process group (tools/environments/local.py:_run_bash).
    from project_sessions.worker_policy import WorkerPolicy
    policy = WorkerPolicy(run_dir, lambda: True, redact=str)
    directive = policy.pre_tool('terminal', {'command': 'sleep 1.5; printf late > late.txt'})
    subprocess.Popen(['/bin/bash', '-c', directive['args']['command']], cwd=directive['args']['workdir'],
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(json.dumps({'type': 'tool_use', 'name': 'terminal', 'input': {}}), flush=True)
    time.sleep(30)
if 'DETACHED_KERNEL' in query:
    # Like Hermes's execute_code session kernel: Popen(start_new_session=True), no terminal launcher.
    subprocess.Popen(['/bin/bash', '-c', 'touch kernel-started; sleep 2; printf late > late-kernel.txt'],
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(json.dumps({'type': 'tool_use', 'name': 'execute_code', 'input': {}}), flush=True)
    time.sleep(30)
if 'UNGUARDED_TOOL' in query:
    # Plugin failed to load: no guard, yet the model starts a tool that writes later.
    (run_dir / 'guard-ready').unlink()
    print(json.dumps({'type': 'tool_use', 'name': 'write_file', 'input': {'path': 'unguarded.txt'}}), flush=True)
    time.sleep(1)
    (Path.cwd() / 'unguarded.txt').write_text('written without worker guard')
if 'MISSING_GUARD' in query:
    (run_dir / 'guard-ready').unlink()
if 'COMPRESS' in query:
    with sqlite3.connect(home / 'state.db') as db:
        old = sid
        sid = uuid.uuid4().hex
        db.execute("UPDATE sessions SET end_reason='compression' WHERE id=?", (old,))
        db.execute('INSERT INTO sessions VALUES(?,?,NULL,?)', (sid, old, '{}'))
        db.execute('INSERT INTO history SELECT ?,text FROM history WHERE session_id=?', (sid, old))
if 'TURN_LIMIT' in query or 'FAILED_WITH_TEXT' in query:
    # Real hermes chat -q at agent.max_turns: toolless summary after Hermes's own nudge, completed=False, exit 1.
    # Without a model summary Hermes appends no assistant row and returns its own notice.
    text = ('Partial findings so far' if 'TURN_LIMIT_SUMMARY' in query else
            "I reached the iteration limit and couldn't generate a summary." if 'TURN_LIMIT' in query else 'Provider error text')
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT,role TEXT,content TEXT)')
        if 'TURN_LIMIT' in query:
            db.execute('INSERT INTO messages(session_id,role,content) VALUES(?,?,?)', (sid, 'user',
                       "You've reached the maximum number of tool-calling iterations allowed. Please provide a final response "
                       "summarizing what you've found and accomplished so far, without calling any more tools."))
        if 'TURN_LIMIT' not in query or 'TURN_LIMIT_SUMMARY' in query:
            db.execute('INSERT INTO messages(session_id,role,content) VALUES(?,?,?)', (sid, 'assistant', text))
    print(json.dumps({'type': 'result', 'session_id': sid, 'exit_code': 1, 'text': text}), flush=True)
    sys.exit(1)
text = 'NEEDS_INPUT: approval required' if 'ASK_INPUT' in query else f'Artifact; prior_turns={len(prior)}; session={sid}'
if 'LEAK_OUTPUT' in query:
    text = 'token=exampleCredentialRejected'
print(json.dumps({'type': 'result', 'session_id': sid, 'exit_code': 0, 'text': text}), flush=True)

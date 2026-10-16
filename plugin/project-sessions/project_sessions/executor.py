from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import threading
import time

from .lifecycle import VERSION, drain_requested
from .reasoning import worker_effort, worker_max_turns
from .cards import CARD_INSTRUCTION
from .registry import Registry, safe_text
from .worker_policy import policy_overlay


def birth(pid):
    """Linux starttime + boot ID prevent PID reuse from looking like our child."""
    try:
        raw = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if raw[0] == 'Z':
            return None
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip() + ':' + raw[19]
    except (OSError, IndexError):
        return None


def group_alive(pgid):
    # Do not release a writer lock just because its group leader exited.
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == pgid and fields[0] != 'Z':
                return True
        except (OSError, ValueError, IndexError):
            pass
    return False


def session_members(run_dir):
    """Live processes in terminal sessions recorded by the sandbox for this run."""
    try:
        recorded = dict(line.split() for line in (Path(run_dir) / 'sessions').read_text().splitlines() if line.strip())
    except (OSError, ValueError):
        return []
    members, stats = [], {}
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            try:
                stats[entry.name] = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            except (OSError, IndexError):
                pass
    for pid, fields in stats.items():
        try:
            sid = fields[3]
        except IndexError:
            continue
        # A live leader with another start time means the session ID was reused: not ours.
        if fields[0] != 'Z' and sid in recorded and (sid not in stats or stats[sid][19] == recorded[sid]):
            members.append(int(pid))
    return members


def record_own_session(pid, run_dir):
    """The worker leads its own session. Helpers that leave its process group but stay in its
    session (Hermes shell hooks use Popen(process_group=0)) are only visible by session."""
    try:
        start = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return
    record = os.open(Path(run_dir) / 'sessions', os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC, 0o600)
    try:
        os.write(record, f'{pid} {start}\n'.encode())
    finally:
        os.close(record)


def record_descendant_sessions(root, run_dir):
    """Record sessions that live descendants of *root* started outside the terminal launcher
    (execute_code kernels, process registry, MCP/browser helpers use start_new_session=True).
    Must run while *root* is alive: orphans lose their ancestry once it dies."""
    stats = {}
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            try:
                stats[entry.name] = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            except (OSError, IndexError):
                pass
    children = {}
    for pid, fields in stats.items():
        if len(fields) > 19:
            children.setdefault(fields[1], []).append(pid)
    root = str(root)
    own = stats.get(root, [None] * 4)[3]
    found, stack = set(), list(children.get(root, []))
    while stack:
        pid = stack.pop()
        sid = stats[pid][3]
        if sid != own and sid in stats and len(stats[sid]) > 19:
            found.add(f'{sid} {stats[sid][19]}')
        stack.extend(children.get(pid, []))
    path = Path(run_dir) / 'sessions'
    try:
        known = set(path.read_text().splitlines())
    except OSError:
        known = set()
    new = found - known
    if new:
        record = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC, 0o600)
        try:
            os.write(record, ''.join(line + '\n' for line in sorted(new)).encode())
        finally:
            os.close(record)


def lineage(home, current, previous=None):
    """Read-only evidence adapter. No Hermes state mutation or DB initialization."""
    path = Path(home) / 'state.db'
    with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        trail, seen = [], set()
        while current:
            if current in seen or len(seen) >= 64:
                raise ValueError('Ambiguous or cyclic lineage')
            seen.add(current)
            row = db.execute('SELECT id,parent_session_id,end_reason,model_config FROM sessions WHERE id=?', (current,)).fetchone()
            if not row:
                raise ValueError('Session missing from bound profile')
            config = json.loads(row[3] or '{}')
            if any(config.get(k) for k in ('_branched_from', '_delegate_from', '_reset_from')):
                raise ValueError('Session is a branch/delegate/reset, not resume')
            trail.append({'session_id': row[0], 'parent_session_id': row[1], 'end_reason': row[2]})
            if current == previous:
                return trail
            if row[1]:
                parent = db.execute('SELECT end_reason FROM sessions WHERE id=?', (row[1],)).fetchone()
                if parent and parent[0] in ('session_reset', 'session_switch', 'idle', 'daily', 'suspended', 'resume_pending_expired', 'new_session'):
                    raise ValueError('Lineage crosses a conversation reset boundary')
            current = row[1]
        if previous:
            raise ValueError('Result session does not descend from resume target')
        return trail


# Hermes's own nudge once agent.max_turns is spent (agent/context_compressor.MAX_ITERATIONS_SUMMARY_REQUEST).
TURN_LIMIT_NUDGE = "You've reached the maximum number of tool-calling iterations allowed."


def turn_limited(home, session_id):
    """Read-only: did Hermes end this turn at its turn budget? Its last user message is then the nudge.
    `hermes chat -q` exits 1 (completed=False) although its text is the toolless summary, or
    Hermes's own no-summary notice if the model gave none."""
    try:
        with contextlib.closing(sqlite3.connect((Path(home) / 'state.db').as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute("SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
    except (OSError, sqlite3.Error):
        return False
    return bool(row) and str(row[0]).startswith(TURN_LIMIT_NUDGE)


class HermesAdapter:
    def __init__(self, executable='hermes'):
        self.executable = executable

    def launch(self, task, home, run_dir):
        query = run_dir / 'query.txt'
        query.write_text(
            'You are a persistent project workstream. Continue your actual session history.\n'
            'Use the profile’s full normal tools and permissions. Native approval prompts '
            'are escalated to the user in Telegram; wait for the decision and report denials. '
            'Do not bypass a denial. Directory locks coordinate workstreams only.\n'
            f"Workstream scope: {task['scope']}\n{task['input']}\nCompletion criteria: {task['criteria']}\n"
            f"User authorization boundary (native approval still required): {task['authorization']}\n" + CARD_INSTRUCTION,
            encoding='utf-8')
        os.chmod(query, 0o600)
        argv = [self.executable, 'chat', '--query-file', str(query), '-Q', '--format', 'stream-json', '--no-restore-cwd']
        max_turns = worker_max_turns(home)
        if max_turns is not None:
            argv += ['--max-turns', str(max_turns)]
        # Otherwise Hermes resolves agent.max_turns from the worker's actual profile.
        configured_effort = worker_effort(home)
        if configured_effort:
            argv += ['--reasoning', configured_effort]
        if task['session_id']:
            argv += ['--resume', task['session_id']]
        env = dict(os.environ)
        # Never inherit a main session's routing, approvals, dispatcher ownership, or another
        # profile's bridged runtime (an executor started from a gateway terminal carries e.g.
        # TERMINAL_CWD=~ and HERMES_SCRATCH_DIR of that profile; workers then wrote outside).
        for key in list(env):
            if key.startswith(('HERMES_', 'TERMINAL_', 'PROJECT_SESSIONS_')) or key == 'MESSAGING_CWD':
                env.pop(key)
        managed = policy_overlay(run_dir, json.loads((run_dir / 'policy.json').read_text()).get('approval_timeout', 1800))
        env.update(HERMES_HOME=home, HERMES_MANAGED_DIR=str(managed), HERMES_YOLO_MODE='0', TERMINAL_CWD=task['cwd'],
                   PROJECT_SESSIONS_WORKER=str(run_dir), PYTHONDONTWRITEBYTECODE='1',
                   **dict.fromkeys(('TMPDIR', 'TMP', 'TEMP'), str(run_dir / 'tmp')))
        return subprocess.Popen(argv, cwd=task['cwd'], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, encoding='utf-8', errors='replace', start_new_session=True)

    def evidence(self, home, current, previous):
        return lineage(home, current, previous)


class Executor:
    def __init__(self, registry, adapter=None, max_workers=2, timeout=21600, approval_timeout=1800):
        self.registry = registry
        self.adapter = adapter or HermesAdapter()
        self.max_workers = max_workers
        self.timeout = timeout
        self.approval_timeout = approval_timeout
        self.stop = threading.Event()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        self.futures = set()
        self.lockfile = None
        self._owned_runs = set()
        self._pending_recovery = False

    def acquire(self):
        self.lockfile = open(str(self.registry.path) + '.executor.lock', 'a')
        os.chmod(self.lockfile.name, 0o600)
        # lifecycle.holder() takes the flock for an instant to probe; only a real holder persists.
        for attempt in range(20):
            try:
                fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if attempt == 19:
                    raise
                time.sleep(.05)
        self.owner = {'pid': os.getpid(), 'birth': birth(os.getpid()), 'version': VERSION}
        self.lockfile.seek(0)
        self.lockfile.truncate()
        json.dump(self.owner, self.lockfile)
        self.lockfile.flush()

    def reconcile(self):
        self._pending_recovery = False
        with self.registry.connection() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM runs WHERE state IN ('running','cancel_requested','uncertain')")]
        for run in rows:
            if run['id'] in self._owned_runs:
                continue
            # Stop only proven descendants before ending interrupted runs and releasing locks. Never
            # signal a reused PID. The gateway cgroup normally already removed these.
            run_dir = self.registry.path.parent / 'runs' / run['id']
            if run['pid'] and run['birth'] and birth(run['pid']) == run['birth']:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(run['pid'], signal.SIGTERM)
            for _ in range(100):
                members = session_members(run_dir)
                if not members:
                    break
                for member in members:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(member, signal.SIGKILL)
                time.sleep(.02)
            if session_members(run_dir):
                self._pending_recovery = True
                continue  # Retry reconciliation automatically; never unlock while a live child remains.
            if run['pid'] and run['birth'] and birth(run['pid']) == run['birth']:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(run['pid'], signal.SIGKILL)
                self._pending_recovery = True
                continue
            if run['pid'] and group_alive(run['pid']):
                self._pending_recovery = True
                continue  # Unproven surviving group: wait for cgroup cleanup, retain its lock until it exits.
            sid, trail = run['session_id'], []
            try:
                if sid:
                    trail = self.adapter.evidence(self.registry.home, sid, run['resumed_from'])
            except Exception:
                sid = None
            if run['state'] == 'cancel_requested':
                self.registry.finish(run['id'], 'cancelled', session_id=sid, lineage=trail,
                                     error='Cancelled before gateway restart')
            else:
                self.registry.finish(run['id'],'interrupted',session_id=sid,lineage=trail,
                                     error='Interrupted by gateway restart; resubmit to this subsession to continue.')
            shutil.rmtree(run_dir / 'tmp', ignore_errors=True)

    def tick(self):
        # Retrying cleanup handles the brief cgroup shutdown/start overlap automatically.
        if self._pending_recovery:
            self.reconcile()
        self.futures = {f for f in self.futures if not f.done()}
        while len(self.futures) < self.max_workers:
            task = self.registry.claim()
            if not task:
                break
            self._owned_runs.add(task['run_id'])
            self.futures.add(self.pool.submit(self.run_task, task))

    def cancellation(self, task):
        with self.registry.connection() as db:
            return db.execute('SELECT state FROM tasks WHERE id=?', (task['id'],)).fetchone()[0] == 'cancel_requested'

    def run_task(self, task):
        runs = self.registry.path.parent / 'runs'
        runs.mkdir(mode=0o700, exist_ok=True)
        os.chmod(runs, 0o700)  # Older installs created it with the umask default.
        run_dir = runs / task['run_id']
        run_dir.mkdir(mode=0o700)
        (run_dir / 'tmp').mkdir(mode=0o700)
        with open(run_dir / 'policy.json', 'x') as policy:
            os.chmod(policy.name, 0o600)
            json.dump({'workspace': task['cwd'], 'registry_path': str(self.registry.path),
                       'home': self.registry.home,
                       'approval_timeout': self.approval_timeout,
                       'task_id': task['id'], 'run_id': task['run_id']}, policy)
        proc = None
        envelopes = []
        validated_sid, validated_lineage = None, []
        protocol_error = threading.Event()
        stray = [0]
        try:
            proc = self.adapter.launch(task, self.registry.home, run_dir)
            self.registry.attach(task['run_id'], proc.pid, birth(proc.pid))
            record_own_session(proc.pid, run_dir)

            def drain():
                try:
                    for line in proc.stdout:
                        # Bounded protocol; discard deltas/tools instead of saving sensitive logs.
                        if len(line) > 65536:
                            protocol_error.set()
                            continue
                        if not line.lstrip().startswith('{'):
                            # Hermes still prints quiet-mode status (e.g. "⟳ compacting context…")
                            # to stdout in stream-json mode; killing on it aborted every auto-compression.
                            # Discarded unstored; only the JSON result envelope decides the outcome.
                            stray[0] += 1
                            if stray[0] > 100:
                                protocol_error.set()
                            continue
                        try:
                            record = json.loads(line)
                        except ValueError:
                            protocol_error.set()
                            continue
                        if record.get('type') == 'system' and record.get('subtype') == 'init' and record.get('session_id'):
                            self.registry.note_session(task['run_id'], record['session_id'])
                        if record.get('type') == 'tool_use' and not (run_dir / 'guard-ready').exists():
                            # Plugin discovery precedes the tool snapshot, so a missing guard here
                            # means worker approval adapter is absent: stop before more tools run.
                            protocol_error.set()
                        if record.get('type') == 'tool_result':
                            output = str(record.get('output', ''))
                            if any(marker in output for marker in ('BLOCKED:', 'NEEDS_INPUT:', 'Permission denied', 'requires approval')):
                                (run_dir / 'needs-input').touch(mode=0o600)
                        if record.get('type') == 'result':
                            if len(envelopes) >= 1:
                                protocol_error.set()
                            else:
                                envelopes.append(record)
                except Exception:
                    protocol_error.set()

            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            deadline = time.monotonic() + self.timeout
            cancelled = timed_out = shutdown_interrupted = False
            last_tick = last_scan = time.monotonic()
            while proc.poll() is None:
                now = time.monotonic()
                if self.registry.waiting_for_approval(task['run_id']):
                    deadline += now - last_tick
                last_tick = now
                if now - last_scan >= .5:
                    record_descendant_sessions(proc.pid, run_dir)
                    last_scan = now
                cancelled = self.cancellation(task)
                timed_out = time.monotonic() > deadline
                if cancelled or timed_out or self.stop.is_set() or protocol_error.is_set():
                    shutdown_interrupted = self.stop.is_set() and not (cancelled or timed_out or protocol_error.is_set())
                    record_descendant_sessions(proc.pid, run_dir)
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=5)
                    break
                time.sleep(0.05)
            reader.join(timeout=2)
            if not (envelopes or cancelled or timed_out or protocol_error.is_set()):
                # A cgroup-wide SIGTERM (gateway stop) can kill the worker before our own stop flag lands.
                shutdown_interrupted = self.stop.wait(1 if proc.returncode < 0 else 0)
            if group_alive(proc.pid):
                os.killpg(proc.pid, signal.SIGKILL)
                for _ in range(100):
                    if not group_alive(proc.pid):
                        break
                    time.sleep(.02)
                if group_alive(proc.pid):
                    raise RuntimeError('Surviving subprocess group; retain lock')
            # Terminal commands run in their own sessions; the turn is over, so stop them too.
            for _ in range(100):
                members = session_members(run_dir)
                if not members:
                    break
                for member in members:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(member, signal.SIGKILL)
                time.sleep(.02)
            else:
                raise RuntimeError('Surviving terminal session; retain lock')
            cancelled = cancelled or self.cancellation(task)
            record = envelopes[0] if envelopes else {}
            sid = record.get('session_id')
            if not sid:
                with self.registry.connection() as db:
                    sid = db.execute('SELECT session_id FROM runs WHERE id=?', (task['run_id'],)).fetchone()[0]
            trail = self.adapter.evidence(self.registry.home, sid, task['session_id']) if sid else []
            validated_sid, validated_lineage = sid, trail
            needs_marker = (run_dir / 'needs-input').exists() or (run_dir / 'blocked-actions.json').exists()
            budget_spent = bool(proc.returncode == 1 and record.get('exit_code') == 1 and sid
                                and turn_limited(self.registry.home, sid))
            if cancelled:
                state, result = 'cancelled', ''
            elif timed_out or self.stop.is_set() or protocol_error.is_set():
                state, result = 'needs_input', ''
            elif needs_marker:
                # A denial wins over model success, but keep the worker's explanation as data.
                state, result = 'needs_input', safe_text(record['text']) if record.get('text') else ''
            elif (proc.returncode != 0 or record.get('exit_code') != 0 or not sid or not envelopes) and not budget_spent:
                state, result = 'failed', ''
            elif not (run_dir / 'guard-ready').exists():
                state, result = 'needs_input', ''
            else:
                result = safe_text(record.get('text', ''))
                state = 'needs_input' if budget_spent or result.startswith('NEEDS_INPUT:') else 'completed'
            artifact = None
            if result:
                artifact_path = run_dir / 'result.md'
                artifact_path.write_text(result, encoding='utf-8')
                os.chmod(artifact_path, 0o600)
                artifact = str(artifact_path)
            actions_path = run_dir / 'blocked-actions.json'
            actions = json.loads(actions_path.read_text()) if actions_path.exists() else []
            if shutdown_interrupted:
                self.registry.finish(task['run_id'],'interrupted',session_id=sid,lineage=trail,
                                     error='Interrupted by gateway restart; resubmit to this subsession to continue.')
                return
            self.registry.finish(task['run_id'], state, blocked_actions=actions, result=result, artifact=artifact, session_id=sid,
                                 lineage=trail, exit_code=proc.returncode,
                                 error=(None if state == 'completed' else
                                        f'Timed out after {self.timeout:g} s; history and workspace kept. A follow-up to this subsession continues it.'
                                        if timed_out and not cancelled else
                                        'Reached the turn limit; history and workspace kept. A follow-up to this subsession continues it.'
                                        if budget_spent and not needs_marker else
                                        'Requires input; cancelled, denied, failed, or interrupted'))
        except Exception:
            # Conservative: if ownership/stopping is uncertain, keep every lock.
            if proc is not None and (proc.poll() is None or group_alive(proc.pid) or session_members(run_dir)):
                with self.registry.connection() as db:
                    db.execute("UPDATE runs SET state='uncertain',error='Process reconciliation required' WHERE id=?", (task['run_id'],))
                    db.execute("UPDATE tasks SET state='uncertain' WHERE id=?", (task['id'],))
                    run = db.execute('SELECT * FROM runs WHERE id=?', (task['run_id'],)).fetchone()
                    self.registry.event(db, run, 'needs_input', {'state': 'uncertain', 'reason': 'Process reconciliation required; lock retained'})
            else:
                self.registry.finish(task['run_id'], 'needs_input', session_id=validated_sid, lineage=validated_lineage, error='Adapter/protocol/lineage/output validation failed; no replay')
        finally:
            if proc is not None and proc.stdout:
                proc.stdout.close()
            with contextlib.suppress(OSError):
                (run_dir / 'query.txt').unlink()
            # Killed workers leave Hermes terminal snapshots (env values incl. provider keys) in the
            # per-run TMPDIR. Remove it only once the worker group and every recorded session
            # (its own included) are gone; crash/uncertain runs keep it for the operator.
            if proc is None or (proc.poll() is not None and not group_alive(proc.pid) and not session_members(run_dir)):
                shutil.rmtree(run_dir / 'tmp', ignore_errors=True)

    def close(self):
        self.stop.set()
        self.pool.shutdown(wait=True)
        if self.lockfile:
            self.lockfile.close()

    def serve(self):
        self.acquire()
        self.reconcile()
        try:
            while not self.stop.is_set():
                if drain_requested(self.registry.path, self.owner):
                    # Do not set stop while workers run: stop would interrupt them. Uncertain runs
                    # are left for operator reconciliation; the successor's reconcile re-checks them.
                    with self.registry.connection() as db:
                        active = db.execute("SELECT 1 FROM runs WHERE state IN ('running','cancel_requested') LIMIT 1").fetchone()
                    if not active and all(f.done() for f in self.futures):
                        break
                else:
                    self.tick()
                self.stop.wait(.2)
        finally:
            self.close()


def main():
    parser = argparse.ArgumentParser(description='Single-profile persistent project executor (Linux)')
    parser.add_argument('--home', required=True)
    parser.add_argument('--db', required=True)
    parser.add_argument('--hermes', default='hermes', help='Verified Hermes CLI executable path')
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--timeout', type=float, default=21600)
    parser.add_argument('--approval-timeout', type=float, default=1800)
    args = parser.parse_args()
    if not 1 <= args.workers <= 8 or args.timeout <= 0 or args.approval_timeout <= 0:
        parser.error('workers must be 1..8; timeouts must be positive')
    registry = Registry(args.db, args.home)
    executor = Executor(registry, HermesAdapter(args.hermes), max_workers=args.workers, timeout=args.timeout, approval_timeout=args.approval_timeout)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: executor.stop.set())
    executor.serve()


if __name__ == '__main__':
    main()

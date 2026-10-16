"""Gateway child executor supervision."""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from importlib.metadata import version as package_version

# Captured at import: an old process must not report newly installed files' version.
_manifest = Path(__file__).resolve().parents[1]/'plugin.yaml'
VERSION = (re.search(r'^version:\s*[\'"]([^\'"]+)', _manifest.read_text(), re.M)[1]
           if _manifest.exists() else package_version('hermes-project-sessions'))


_children = {}
_launched = set()  # dbs this gateway process has already spawned an executor for


def shutdown(db):
    """Only terminate this gateway's Popen child; never signal a PID-file owner."""
    process = _children.pop(str(db), None)
    if process is not None and process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            pass  # Gateway cgroup cleanup is the final shutdown boundary.


def paths(db):
    return Path(str(db)+'.executor.lock'), Path(str(db)+'.executor-drain.json')


def holder(db):
    """The flock, not a PID file or PID existence, is the liveness authority."""
    lock, _ = paths(db)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, 'a+') as stream:
        os.chmod(lock, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.seek(0)
            try:
                value = json.load(stream)
                return value if isinstance(value, dict) and all(k in value for k in ('pid','birth','version')) else {'version':'legacy'}
            except (ValueError, TypeError):
                return {'version':'legacy'}
        return None


def drain_requested(db, owner):
    try:
        return json.loads(paths(db)[1].read_text()) == owner
    except (OSError, ValueError):
        return False


def start(home, db):
    executable = shutil.which('hermes')
    if executable is None:
        return False
    argv = [sys.executable, '-m', 'project_sessions.executor', '--home', str(home),
            '--db', str(db), '--hermes', executable]
    cwd = str(Path(__file__).resolve().parents[1])
    env = {k:v for k,v in os.environ.items()
           if not k.startswith(('HERMES_', 'TERMINAL_', 'PROJECT_SESSIONS_')) and k != 'MESSAGING_CWD'}
    env.update(HERMES_HOME=str(home), PYTHONDONTWRITEBYTECODE='1')
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, **quiet)
        _children[str(db)] = process
        # Reap the plain gateway child. Shutdown explicitly terminates owned children.
        import threading
        threading.Thread(target=process.wait, daemon=True).start()
        return True
    except OSError:
        return False


def status(db):
    """Probe without spawning or requesting a drain (non-gateway hosts)."""
    try:
        owner = holder(db)
    except OSError:
        return 'offline'
    if owner is None or owner.get('version') == 'legacy':
        return 'offline'
    return 'online' if owner.get('version') == VERSION else 'draining'


def backoff(db):
    """True while a recent spawn may still be crash-looping; otherwise record this spawn."""
    path, now = Path(str(db)+'.executor-start.json'), time.time()
    try:
        state = json.loads(path.read_text())
        since, streak = now-float(state['at']), int(state['streak'])
    except (OSError, ValueError, TypeError, KeyError):
        since, streak = None, 0
    if since is None or not 0 <= since < 600 or str(db) not in _launched:
        # First spawn, a long-lived executor, a clock jump, or a fresh gateway (its restart
        # killed the child; that is not a crash loop): respawn now.
        streak = 0
    _launched.add(str(db))
    if streak and since < min(300, 2**streak):
        return True
    temporary = Path(str(path)+'.new')
    with open(temporary,'w') as stream:
        os.chmod(temporary,0o600)
        json.dump({'at':now,'streak':streak+1},stream)
    os.replace(temporary,path)
    return False


def ensure(home, db):
    """Return online/draining/offline. Never signal an unproven PID or stop workers."""
    try:
        with open(str(db)+'.executor-start.lock','a') as launch_lock:
            os.chmod(launch_lock.name, 0o600)
            # Wait out a concurrent launcher (bounded by its timeouts) instead of
            # misreporting offline; never probe the executor flock while it checks.
            deadline = time.monotonic()+8
            while True:
                try:
                    fcntl.flock(launch_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        return 'offline'
                    time.sleep(.05)
            owner = holder(db)
            if owner is not None:
                if owner.get('version') != VERSION:
                    # Old pre-supervision executors have no drain protocol. Installation
                    # retires that one legacy service explicitly; never guess its PID.
                    if owner.get('version') == 'legacy':
                        return 'offline'
                    _, drain = paths(db)
                    temporary = Path(str(drain)+'.new')
                    with open(temporary,'w') as stream:
                        os.chmod(temporary,0o600)
                        json.dump(owner,stream)
                    os.replace(temporary,drain)
                    return 'draining'
                return 'online'
            if backoff(db) or not start(home,db):
                return 'offline'
            # A launch acknowledgement is not proof of a live executor.
            for _ in range(20):
                if holder(db) is not None:
                    return 'online'
                time.sleep(.05)
            return 'offline'
    except (OSError, ValueError, TypeError):
        return 'offline'

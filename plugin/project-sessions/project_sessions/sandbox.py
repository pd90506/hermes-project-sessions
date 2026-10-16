"""Terminal-session tracking launcher. No sandbox or permission restrictions.

Hermes's local backend starts commands in a separate session. Record that session
before running the command so the executor can stop it on cancel/timeout/turn exit.
"""
import os
from pathlib import Path
import subprocess
import sys


def main():
    try:
        policy_file, separator, *command = sys.argv[1:]
        if separator != '--' or not command:
            raise ValueError('Expected policy.json -- command...')
        session = os.getsid(0)
        start = Path(f'/proc/{session}/stat').read_text().rsplit(')', 1)[1].split()[19]
        record = os.open(Path(policy_file).parent / 'sessions', os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC, 0o600)
        try:
            os.write(record, f'{session} {start}\n'.encode())
        finally:
            os.close(record)
        return subprocess.run(command, close_fds=True).returncode
    except Exception:
        print('NEEDS_INPUT: terminal session tracking failed; command was not executed', file=sys.stderr)
        return 126


if __name__ == '__main__':
    raise SystemExit(main())

"""Native steer delivery and read-only durable transcript confirmation."""
import contextlib
import sqlite3
from pathlib import Path


def text(task):
    return f"[Subsession message {task['id']}]\n{task['input']}"


def persisted(home, sessions, task):
    try:
        with contextlib.closing(sqlite3.connect((Path(home)/'state.db').as_uri()+'?mode=ro',uri=True)) as db:
            for sid in sessions:
                for (content,) in db.execute("SELECT content FROM messages WHERE session_id=? AND role='user'",(sid,)):
                    # Match the unique header only: Hermes strips the steer text before persisting it.
                    if isinstance(content,str) and content.startswith('[OUT-OF-BAND USER MESSAGE') and f"[Subsession message {task['id']}]\n" in content and content.endswith('[/OUT-OF-BAND USER MESSAGE]'):
                        return True
    except (OSError,sqlite3.Error):
        pass
    return False


def deliver(registry, run_id):
    from agent.subagent_lifecycle import get_active_subagent_parent
    agent=get_active_subagent_parent()
    if agent is None:
        return
    with registry.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        run=db.execute("SELECT * FROM runs WHERE id=? AND state='running'",(run_id,)).fetchone()
        if not run or not run['session_id'] or agent.session_id != run['session_id']:
            return
        for row in db.execute("SELECT * FROM tasks WHERE steer_run_id=? AND steer_accepted=0 AND state='queued' ORDER BY created",(run_id,)).fetchall():
            if agent.steer(text(row)):
                db.execute('UPDATE tasks SET steer_accepted=1 WHERE id=?',(row['id'],))

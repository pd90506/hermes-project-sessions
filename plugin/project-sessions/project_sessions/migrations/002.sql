ALTER TABLE workstreams ADD COLUMN writable_roots TEXT NOT NULL DEFAULT '[]';
UPDATE workstreams SET writable_roots=json_array(workspace);
CREATE TABLE blocked_actions(
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), run_id TEXT NOT NULL REFERENCES runs(id),
 fingerprint TEXT NOT NULL, tool TEXT NOT NULL, context TEXT NOT NULL, reason TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'awaiting_user', created REAL NOT NULL,
 approval_message_id TEXT, approved REAL, resumed_task_id TEXT UNIQUE,
 UNIQUE(run_id,fingerprint));
ALTER TABLE tasks ADD COLUMN approved_action_id TEXT REFERENCES blocked_actions(id);
UPDATE meta SET version=2;

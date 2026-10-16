ALTER TABLE workstreams RENAME COLUMN writable_roots TO coordination_roots;
CREATE TABLE approval_requests(
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), run_id TEXT NOT NULL REFERENCES runs(id),
 command TEXT NOT NULL, reason TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', decision TEXT,
 created REAL NOT NULL, deadline REAL NOT NULL, decided REAL,
 user_vote TEXT, user_message_id TEXT);
CREATE INDEX approval_run_pending ON approval_requests(run_id,state);
-- A legacy accepted event whose later retry failed is still accepted and must not replay.
UPDATE outbox SET state='accepted' WHERE state='pending' AND accepted IS NOT NULL;
UPDATE meta SET version=3;

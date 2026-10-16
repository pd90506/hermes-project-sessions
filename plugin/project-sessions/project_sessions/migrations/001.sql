CREATE TABLE meta(version INTEGER NOT NULL, profile_home TEXT);
INSERT INTO meta VALUES(1,NULL);
CREATE TABLE projects(id TEXT PRIMARY KEY, cwd TEXT NOT NULL, profile_home TEXT NOT NULL,
 route TEXT NOT NULL, main_session TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE workstreams(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 scope TEXT NOT NULL, workspace TEXT NOT NULL, session_id TEXT, root_session TEXT, summary TEXT NOT NULL DEFAULT '');
CREATE TABLE tasks(id TEXT PRIMARY KEY, workstream_id TEXT NOT NULL REFERENCES workstreams(id),
 request_key TEXT NOT NULL UNIQUE, input TEXT NOT NULL, criteria TEXT NOT NULL, authorization TEXT NOT NULL,
 state TEXT NOT NULL, created REAL NOT NULL, result TEXT, artifact TEXT);
CREATE TABLE runs(id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
 state TEXT NOT NULL, pid INTEGER, birth TEXT, started REAL NOT NULL, ended REAL, exit_code INTEGER,
 resumed_from TEXT, session_id TEXT, lineage TEXT, error TEXT);
CREATE TABLE resource_locks(resource TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id));
CREATE TABLE outbox(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 workstream_id TEXT NOT NULL, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
 kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
 claim TEXT, claim_until REAL, accepted REAL, acknowledged REAL);
CREATE INDEX tasks_queue ON tasks(state, created);
CREATE INDEX outbox_pending ON outbox(state, next_attempt);

-- Rebuild only the idempotency tables. The migration runner disables foreign keys
-- outside this transaction, validates all references, then commits and re-enables.
CREATE TABLE tasks_next(id TEXT PRIMARY KEY, workstream_id TEXT NOT NULL REFERENCES workstreams(id),
 request_key TEXT NOT NULL, input TEXT NOT NULL, criteria TEXT NOT NULL, authorization TEXT NOT NULL,
 state TEXT NOT NULL, created REAL NOT NULL, result TEXT, artifact TEXT,
 approved_action_id TEXT REFERENCES blocked_actions(id), workspace TEXT,
 project_id TEXT NOT NULL REFERENCES projects(id), UNIQUE(project_id,request_key));
INSERT INTO tasks_next SELECT t.id,t.workstream_id,t.request_key,t.input,t.criteria,t.authorization,t.state,t.created,t.result,t.artifact,t.approved_action_id,t.workspace,w.project_id FROM tasks t JOIN workstreams w ON w.id=t.workstream_id;
CREATE TABLE delegation_requests_next(task_id TEXT PRIMARY KEY REFERENCES tasks(id), spec TEXT NOT NULL);
INSERT INTO delegation_requests_next SELECT t.id,d.spec FROM delegation_requests d JOIN tasks t ON t.request_key=d.request_key;
DROP TABLE delegation_requests;
DROP TABLE tasks;
ALTER TABLE tasks_next RENAME TO tasks;
ALTER TABLE delegation_requests_next RENAME TO delegation_requests;
CREATE INDEX tasks_queue ON tasks(state,created);
CREATE INDEX tasks_workstream ON tasks(workstream_id,created);
UPDATE meta SET version=10;

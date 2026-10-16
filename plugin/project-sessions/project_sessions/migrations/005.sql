-- Preserve all project/task/run identities and historical artifacts. Directory binding ends.
CREATE TABLE legacy_directory_bindings(project_id TEXT PRIMARY KEY, cwd TEXT NOT NULL);
INSERT INTO legacy_directory_bindings SELECT id,cwd FROM projects;
CREATE TABLE legacy_coordination_roots(workstream_id TEXT PRIMARY KEY, roots TEXT NOT NULL);
INSERT INTO legacy_coordination_roots SELECT id,coordination_roots FROM workstreams;
ALTER TABLE projects DROP COLUMN cwd;
ALTER TABLE workstreams DROP COLUMN coordination_roots;
ALTER TABLE workstreams ADD COLUMN name TEXT NOT NULL DEFAULT '';
ALTER TABLE workstreams ADD COLUMN card TEXT NOT NULL DEFAULT '{}';
UPDATE workstreams SET name=substr(scope,1,80);
ALTER TABLE tasks ADD COLUMN workspace TEXT;
CREATE TABLE conversation_aliases(route TEXT NOT NULL, session_id TEXT NOT NULL,
 project_id TEXT NOT NULL REFERENCES projects(id), PRIMARY KEY(route,session_id));
INSERT OR IGNORE INTO conversation_aliases SELECT route,main_session,id FROM projects ORDER BY created,rowid;
CREATE TABLE card_updates(task_id TEXT PRIMARY KEY REFERENCES tasks(id), update_json TEXT NOT NULL);
CREATE INDEX tasks_workstream ON tasks(workstream_id,created);
CREATE INDEX workstreams_project ON workstreams(project_id);
UPDATE meta SET version=5;

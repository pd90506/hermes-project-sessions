ALTER TABLE workstreams ADD COLUMN created REAL NOT NULL DEFAULT 0;
ALTER TABLE workstreams ADD COLUMN idle_since REAL NOT NULL DEFAULT 0;
ALTER TABLE workstreams ADD COLUMN last_used REAL NOT NULL DEFAULT 0;
ALTER TABLE workstreams ADD COLUMN archived_at REAL;
-- No creation timestamp existed for legacy streams: project creation is the closest evidence.
UPDATE workstreams SET created=COALESCE((SELECT created FROM projects WHERE id=project_id),CAST(strftime('%s','now') AS REAL));
UPDATE workstreams SET idle_since=COALESCE((SELECT MAX(r.ended) FROM runs r JOIN tasks t ON t.id=r.task_id WHERE t.workstream_id=workstreams.id),created);
UPDATE workstreams SET last_used=MAX(idle_since,COALESCE((SELECT MAX(t.created) FROM tasks t WHERE t.workstream_id=workstreams.id),created));
CREATE INDEX workstreams_archive ON workstreams(project_id,archived_at,last_used);
UPDATE meta SET version=6;

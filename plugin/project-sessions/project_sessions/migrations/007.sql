-- Existing schema-6 installations also get the upgrade grace, without deleting archives
-- or changing historical LRU evidence. Capacity reconciliation remains independent.
UPDATE workstreams SET idle_since=CAST(strftime('%s','now') AS REAL) WHERE archived_at IS NULL;
CREATE TABLE roster_state(project_id TEXT PRIMARY KEY REFERENCES projects(id),
 session_id TEXT NOT NULL, digest TEXT NOT NULL);
UPDATE meta SET version=7;

-- Stream names must be unambiguous to the user (archive notices, roster, archived-card search).
-- The oldest stream keeps its name; later legacy duplicates get a short id suffix within 120 chars.
UPDATE workstreams SET name=substr(name,1,108)||' ('||substr(id,1,8)||')'
 WHERE id IN (SELECT id FROM (SELECT id,ROW_NUMBER() OVER (PARTITION BY project_id,lower(trim(name)) ORDER BY created,rowid) AS n FROM workstreams) WHERE n>1);
UPDATE meta SET version=8;

-- A steer remains durable queued work until its current-turn delivery is verified.
ALTER TABLE tasks ADD COLUMN steer_run_id TEXT REFERENCES runs(id);
ALTER TABLE tasks ADD COLUMN steer_accepted INTEGER NOT NULL DEFAULT 0;
UPDATE meta SET version=11;

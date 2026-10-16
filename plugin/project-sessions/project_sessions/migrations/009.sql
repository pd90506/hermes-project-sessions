-- Persist inline creation intent so request_key retries cannot create extra streams.
CREATE TABLE delegation_requests(request_key TEXT PRIMARY KEY REFERENCES tasks(request_key),
 spec TEXT NOT NULL);
UPDATE meta SET version=9;

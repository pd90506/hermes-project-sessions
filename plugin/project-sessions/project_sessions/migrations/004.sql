CREATE TABLE main_routes(
 route TEXT PRIMARY KEY, origin TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE approval_cards(
 approval_id TEXT PRIMARY KEY REFERENCES approval_requests(id), token TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL DEFAULT 'pending', bot_id TEXT, chat_id TEXT, thread_id TEXT,
 message_id TEXT, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
 claim TEXT, claim_until REAL);
UPDATE meta SET version=4;

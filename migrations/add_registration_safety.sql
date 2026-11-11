-- Additive metadata: legacy tables and historical rows are not rebuilt.
CREATE TABLE IF NOT EXISTS Registration_Id_Sequence (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    last_id INTEGER NOT NULL
);
INSERT OR IGNORE INTO Registration_Id_Sequence VALUES (1, 0);

CREATE TABLE IF NOT EXISTS Paused_Registrations (
    chat_id INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS Cancelled_Matches (
    match_id INTEGER PRIMARY KEY
);

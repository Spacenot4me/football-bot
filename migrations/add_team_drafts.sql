CREATE TABLE IF NOT EXISTS Team_Drafts (
    draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL,
    captain_a_id INTEGER,
    captain_b_id INTEGER,
    captain_mode TEXT NOT NULL CHECK (captain_mode IN ('manual', 'random_all', 'random_list')),
    candidate_user_ids TEXT NOT NULL DEFAULT '[]',
    first_pick TEXT NOT NULL CHECK (first_pick IN ('A', 'B')),
    status TEXT NOT NULL CHECK (status IN ('waiting_for_players', 'drafting', 'done', 'needs_replacement', 'cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    draft_pick_count INTEGER NOT NULL DEFAULT 0,
    replacement_a INTEGER NOT NULL DEFAULT 0,
    replacement_b INTEGER NOT NULL DEFAULT 0,
    roster_snapshot TEXT NOT NULL DEFAULT '[]',
    main_message_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_team_drafts_active_match
    ON Team_Drafts (match_id) WHERE status != 'cancelled';

CREATE TABLE IF NOT EXISTS Team_Picks (
    pick_id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    team TEXT NOT NULL CHECK (team IN ('A', 'B')),
    kind TEXT NOT NULL DEFAULT 'draft' CHECK (kind IN ('draft', 'replacement')),
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'left')),
    pick_number INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_team_picks_active_user
    ON Team_Picks (draft_id, user_id) WHERE state = 'active';

CREATE TABLE IF NOT EXISTS Team_Draft_Messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    tg_chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    UNIQUE (tg_chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS Team_Draft_Restart_Requests (
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    requested_by_id INTEGER NOT NULL,
    draft_revision INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'confirmed', 'cancelled', 'stale'))
);

CREATE TABLE IF NOT EXISTS Team_Draft_Cancel_Requests (
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    requested_by_id INTEGER NOT NULL,
    draft_revision INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'confirmed', 'cancelled', 'stale'))
);

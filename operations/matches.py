# Create
from contextlib import closing

from operations.common import connect_db, execute_query, fetch_one_query, fetch_query
from operations.registration_safety import ensure_safety_tables
from constants import MAX_PLAYERS


def create_match(datetime, amount_per_person, chat_id):
    return execute_query("INSERT INTO Matches (datetime, amount_per_person, chat_id) VALUES (?, ?, ?)", (datetime, amount_per_person, chat_id))


def create_match_if_missing(match_datetime, amount_per_person, chat_id, reopen_cancelled=False):
    """Return the new match ID, or None if this chat already has that match.

    Serialize the check and insert so simultaneous manual/scheduled starts
    cannot both create a match, without rebuilding existing core tables.
    Only an explicit manual opening may replace a cancelled game.
    """
    ensure_safety_tables()
    with closing(connect_db()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute(
            '''SELECT match_id FROM Matches m WHERE chat_id = ? AND datetime = ?
               AND (? = 0 OR NOT EXISTS (SELECT 1 FROM Cancelled_Matches c WHERE c.match_id = m.match_id))''',
            (chat_id, match_datetime, reopen_cancelled),
        ).fetchone()
        if existing:
            return None
        cursor = conn.execute(
            'INSERT INTO Matches (datetime, amount_per_person, chat_id) VALUES (?, ?, ?)',
            (match_datetime, amount_per_person, chat_id),
        )
        return cursor.lastrowid

# Read
def get_match(match_id):
    ensure_safety_tables()
    return fetch_one_query('''SELECT * FROM Matches m WHERE match_id = ?
        AND NOT EXISTS (SELECT 1 FROM Cancelled_Matches c WHERE c.match_id = m.match_id)''', (match_id,))

# Read
def get_last_match(chat_id):
    ensure_safety_tables()
    return fetch_query("""
WITH OrderedMatches AS (
    SELECT m.match_id, m.datetime, m.chat_id,
           ROW_NUMBER() OVER (ORDER BY m.datetime DESC, m.match_id DESC) AS rn
    FROM Matches m
    WHERE m.chat_id = ?
      AND m.match_id != (SELECT MAX(match_id) FROM Matches WHERE chat_id = ?)
      AND NOT EXISTS (SELECT 1 FROM Cancelled_Matches c WHERE c.match_id = m.match_id)
)
SELECT om.match_id, om.datetime, mr.user_id, mr.priority,
       COALESCE(u.nickname, CAST(mr.user_id AS TEXT)) AS nickname,
       COALESCE(u.name, 'Deleted account') AS name
FROM OrderedMatches om
JOIN Match_Registration mr ON om.match_id = mr.match_id
LEFT JOIN Users u ON mr.user_id = u.user_id
WHERE om.rn = 1
ORDER BY mr.priority ASC, mr.registered_at, mr.registration_id
LIMIT ?;
                       """, (chat_id, chat_id, MAX_PLAYERS))

def get_current_match(chat_id):
    ensure_safety_tables()
    return fetch_one_query("""
SELECT m.match_id, m.datetime, m.created_at
FROM Matches m
WHERE m.match_id = (SELECT MAX(match_id) FROM Matches mm WHERE mm.chat_id = ?)
AND NOT EXISTS (SELECT 1 FROM Cancelled_Matches c WHERE c.match_id = m.match_id)
                       """, ((chat_id,)))

def get_match_by_datetime(chat_id, match_datetime):
    """Get a match by chat_id and datetime. Returns None if not found."""
    ensure_safety_tables()
    return fetch_one_query("""
SELECT m.match_id, m.datetime, m.created_at
FROM Matches m
WHERE m.chat_id = ? AND m.datetime = ?
AND NOT EXISTS (SELECT 1 FROM Cancelled_Matches c WHERE c.match_id = m.match_id)
                       """, (chat_id, match_datetime))


def was_in_last_match(chat_id, user_id):
    return any(player['user_id'] == user_id for player in get_last_match(chat_id))


# Update
def update_match(match_id, date, time, amount_per_person, chat_id):
    execute_query("UPDATE Matches SET date = ?, time = ?, amount_per_person = ?, chat_id = ? WHERE match_id = ?", (date, time, amount_per_person, chat_id, match_id))

def get_last_5_matches_with_players(chat_id):
    ensure_safety_tables()
    return fetch_query("""
WITH MatchesWithRegistrations AS (
    SELECT DISTINCT m.match_id, m.datetime
    FROM Matches m
    INNER JOIN Match_Registration mr ON m.match_id = mr.match_id
    WHERE m.chat_id = ?
      AND NOT EXISTS (SELECT 1 FROM Cancelled_Matches c WHERE c.match_id = m.match_id)
),
Last5Matches AS (
    SELECT match_id, datetime,
           ROW_NUMBER() OVER (ORDER BY datetime DESC, match_id DESC) AS rn
    FROM MatchesWithRegistrations
), RankedRegistrations AS (
    SELECT mr.*, ROW_NUMBER() OVER (
        PARTITION BY match_id ORDER BY priority, registered_at, registration_id) AS roster_position
    FROM Match_Registration mr
)
SELECT l5m.match_id, l5m.datetime, mr.user_id, mr.priority,
       COALESCE(u.nickname, CAST(mr.user_id AS TEXT)) AS nickname,
       COALESCE(u.name, 'Deleted account') AS name
FROM Last5Matches l5m
JOIN RankedRegistrations mr ON l5m.match_id = mr.match_id
LEFT JOIN Users u ON mr.user_id = u.user_id
WHERE l5m.rn <= 5 AND mr.roster_position <= ?
ORDER BY l5m.datetime DESC, l5m.match_id DESC, mr.roster_position;
                       """, (chat_id, MAX_PLAYERS))

# Delete
def delete_match(match_id):
    """Cancel without erasing the latest-match marker or its registrations."""
    ensure_safety_tables()
    execute_query('INSERT OR IGNORE INTO Cancelled_Matches SELECT match_id FROM Matches WHERE match_id = ?', (match_id,))

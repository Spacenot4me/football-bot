# Create
from contextlib import closing

from operations.common import connect_db, execute_query, fetch_one_query, fetch_query


def create_match(datetime, amount_per_person, chat_id):
    return execute_query("INSERT INTO Matches (datetime, amount_per_person, chat_id) VALUES (?, ?, ?)", (datetime, amount_per_person, chat_id))


def create_match_if_missing(match_datetime, amount_per_person, chat_id):
    """Return the new match ID, or None if this chat already has that match.

    Serialize the check and insert so simultaneous manual/scheduled starts
    cannot both create a match, without changing the existing database schema.
    """
    with closing(connect_db()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute(
            'SELECT match_id FROM Matches WHERE chat_id = ? AND datetime = ?',
            (chat_id, match_datetime),
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
    return fetch_one_query("SELECT * FROM Matches WHERE match_id = ?", (match_id,))

# Read
def get_last_match(chat_id):
    return fetch_query("""
WITH OrderedMatches AS (
    SELECT m.match_id, m.datetime, m.amount_per_person, m.chat_id,
           ROW_NUMBER() OVER (ORDER BY m.datetime DESC) AS rn
    FROM Matches m
    WHERE m.chat_id = ?
)
SELECT om.match_id, om.datetime, mr.user_id, mr.priority, u.nickname, u.name 
FROM OrderedMatches om
JOIN Match_Registration mr ON om.match_id = mr.match_id
JOIN Users u ON mr.user_id = u.user_id
WHERE om.rn = 2
ORDER BY mr.priority ASC, mr.registered_at;
                       """, ((chat_id,)))

def get_current_match(chat_id):
    return fetch_one_query("""
SELECT m.match_id, m.datetime, m.created_at
FROM Matches m
WHERE m.match_id = (SELECT MAX(match_id) FROM Matches mm WHERE mm.chat_id = ?)
                       """, ((chat_id,)))

def get_match_by_datetime(chat_id, match_datetime):
    """Get a match by chat_id and datetime. Returns None if not found."""
    return fetch_one_query("""
SELECT m.match_id, m.datetime, m.created_at
FROM Matches m
WHERE m.chat_id = ? AND m.datetime = ?
                       """, (chat_id, match_datetime))


def was_in_last_match(chat_id, user_id):
    data = fetch_one_query("""
WITH OrderedMatches AS (
    SELECT m.match_id,
           ROW_NUMBER() OVER (ORDER BY m.datetime DESC) AS rn
    FROM Matches m
    WHERE m.chat_id = ?
),
First14Registrations AS (
    SELECT mr.match_id,
           mr.user_id,
           ROW_NUMBER() OVER (PARTITION BY mr.match_id ORDER BY mr.priority) AS rn
    FROM Match_Registration mr
)
SELECT f14r.user_id
FROM First14Registrations f14r
JOIN OrderedMatches om ON f14r.match_id = om.match_id
WHERE om.rn = 2
AND f14r.rn <= 14
AND f14r.user_id = ?;
                           """, (chat_id, user_id))
    if data is None:
        return False
    else:
        return True


# Update
def update_match(match_id, date, time, amount_per_person, chat_id):
    execute_query("UPDATE Matches SET date = ?, time = ?, amount_per_person = ?, chat_id = ? WHERE match_id = ?", (date, time, amount_per_person, chat_id, match_id))

def get_last_5_matches_with_players(chat_id):
    return fetch_query("""
WITH MatchesWithRegistrations AS (
    SELECT DISTINCT m.match_id, m.datetime
    FROM Matches m
    INNER JOIN Match_Registration mr ON m.match_id = mr.match_id
    WHERE m.chat_id = ?
),
Last5Matches AS (
    SELECT match_id, datetime,
           ROW_NUMBER() OVER (ORDER BY datetime DESC) AS rn
    FROM MatchesWithRegistrations
)
SELECT l5m.match_id, l5m.datetime, mr.user_id, mr.priority, u.nickname, u.name
FROM Last5Matches l5m
JOIN Match_Registration mr ON l5m.match_id = mr.match_id
JOIN Users u ON mr.user_id = u.user_id
WHERE l5m.rn <= 5 AND mr.priority <= 14
ORDER BY l5m.datetime DESC, mr.priority ASC;
                       """, (chat_id,))

# Delete
def delete_match(match_id):
    execute_query("DELETE FROM Matches WHERE match_id = ?", (match_id,))

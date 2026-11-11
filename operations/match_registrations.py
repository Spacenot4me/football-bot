# Create
from contextlib import closing
from operations.common import connect_db, execute_query, fetch_one_query, fetch_query
from operations.registration_safety import ensure_safety_tables, next_registration_id, initialize_registration_ids

def create_match_registration(user_id, registered_by_id, is_plus, confirmed, priority, match_id):
    ensure_safety_tables()
    with closing(connect_db()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT 1 FROM Users WHERE user_id = ?', (user_id,)).fetchone() or not conn.execute(
                'SELECT 1 FROM Users WHERE user_id = ?', (registered_by_id,)).fetchone():
            return False
        match = conn.execute('''SELECT 1 FROM Matches m WHERE match_id = ?
            AND match_id = (SELECT MAX(match_id) FROM Matches WHERE chat_id = m.chat_id)
            AND NOT EXISTS (SELECT 1 FROM Cancelled_Matches WHERE match_id = m.match_id)''', (match_id,)).fetchone()
        if not match:
            return False
        results = conn.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN user_id = registered_by_id AND is_plus = 0 THEN 1 ELSE 0 END), 0) AS self_reg,
            COALESCE(SUM(CASE WHEN user_id != registered_by_id AND is_plus = 0 THEN 1 ELSE 0 END), 0) AS other_chat_reg,
            COALESCE(SUM(CASE WHEN user_id != registered_by_id AND is_plus != 0 THEN 1 ELSE 0 END), 0) AS extra_reg
        FROM Match_Registration
        WHERE registered_by_id = ? AND match_id = ?
    """, (registered_by_id, match_id)).fetchone()

        if conn.execute('SELECT 1 FROM Match_Registration WHERE match_id = ? AND user_id = ?',
                        (match_id, user_id)).fetchone():
            return False
        category = 'self_reg' if user_id == registered_by_id else 'extra_reg' if is_plus else 'other_chat_reg'
        if results[category]:
            return False
        registration_id = next_registration_id(conn)
        conn.execute('''INSERT INTO Match_Registration
            (registration_id, user_id, registered_by_id, is_plus, confirmed, priority, match_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (registration_id, user_id, registered_by_id, is_plus, confirmed, priority, match_id))
        return registration_id

# Read
def get_match_registration(match_id):
    return fetch_query("SELECT * FROM Match_Registration WHERE match_id = ?", (match_id))

def get_current_match_registrations(chat_id):
    initialize_registration_ids()
    return fetch_query("""
WITH OrderedMatches AS (
    SELECT m.match_id, m.datetime, m.amount_per_person, m.chat_id
    FROM Matches m
    WHERE m.match_id = (SELECT MAX(match_id) FROM Matches m WHERE m.chat_id = ?)
      AND NOT EXISTS (SELECT 1 FROM Cancelled_Matches c WHERE c.match_id = m.match_id)
)
SELECT om.match_id, om.datetime, mr.user_id, mr.registered_by_id, mr.priority, mr.is_plus, mr.confirmed, mr.registration_id,
       COALESCE(u.nickname, CAST(mr.user_id AS TEXT)) AS nickname,
       COALESCE(u.name, 'Deleted account') AS name,
       COALESCE(ub.nickname, CAST(mr.registered_by_id AS TEXT)) AS registered_by_nickname
FROM OrderedMatches om
JOIN Match_Registration mr ON om.match_id = mr.match_id
LEFT JOIN Users u ON mr.user_id = u.user_id
LEFT JOIN Users ub ON mr.registered_by_id = ub.user_id
ORDER BY mr.priority ASC, mr.registered_at, mr.registration_id;
                       """, ((chat_id,)))

def check_if_user_played_before(user_id):
    ensure_safety_tables()
    return fetch_one_query('''SELECT 1 FROM Match_Registration mr JOIN Matches m ON m.match_id = mr.match_id
        WHERE mr.user_id = ? AND mr.confirmed = 1
        AND NOT EXISTS (SELECT 1 FROM Cancelled_Matches c WHERE c.match_id = m.match_id)''', (user_id,))

def check_if_user_registered(match_id, user_id):
    return fetch_one_query("SELECT mr.registration_id, mr.confirmed, u.nickname, u.name FROM Match_Registration mr JOIN Users u ON mr.user_id = u.user_id WHERE mr.match_id = ? AND mr.user_id = ?", (match_id, user_id))


def get_invited_match_registrations(match_id, registered_by_id):
    initialize_registration_ids()
    return fetch_query("""
SELECT mr.registration_id, mr.user_id, mr.is_plus, u.nickname, u.name
FROM Match_Registration mr
LEFT JOIN Users u ON u.user_id = mr.user_id
WHERE mr.match_id = ? AND mr.registered_by_id = ?
  AND mr.user_id != mr.registered_by_id
ORDER BY mr.priority, mr.registered_at, mr.registration_id
""", (match_id, registered_by_id))

# Update
def set_match_registration_priority(chat_id, match_id, user_id, priority):
    """Change only this player's registration in the chat's current match."""
    if priority not in (1, 2, 3, 4):
        raise ValueError('Priority must be between 1 and 4')
    ensure_safety_tables()
    with closing(connect_db()) as conn, conn:
        cursor = conn.execute('''
            UPDATE Match_Registration SET priority = ?
            WHERE match_id = ? AND user_id = ?
              AND match_id = (SELECT MAX(match_id) FROM Matches WHERE chat_id = ?)
              AND NOT EXISTS (SELECT 1 FROM Cancelled_Matches c WHERE c.match_id = Match_Registration.match_id)
        ''', (priority, match_id, user_id, chat_id))
        return cursor.rowcount > 0


def update_match_registration(registration_id, user_id, registered_by_id, is_plus, priority, match_id):
    execute_query("UPDATE Match_Registration SET user_id = ?, registered_by_id = ?, is_plus = ?, confirmed = ?, priority = ?, match_id = ? WHERE registration_id = ?", (user_id, registered_by_id, is_plus, priority, match_id, registration_id))

def confirm_user_registration(match_id, user_id, registration_id=None):
    where = "match_id = ? AND user_id = ? AND confirmed = 0"
    params = (match_id, user_id)
    if registration_id is not None:
        where += " AND registration_id = ?"
        params += (registration_id,)
    with closing(connect_db()) as conn, conn:
        cursor = conn.execute("UPDATE Match_Registration SET confirmed = 1 WHERE " + where, params)
        return cursor.rowcount > 0

# Delete
def delete_match_registration(match_id, user_id, registration_id=None):
    with closing(connect_db()) as conn, conn:
        where = 'user_id = ? AND match_id = ?'
        params = (user_id, match_id)
        if registration_id is not None:
            where += ' AND registration_id = ?'
            params += (registration_id,)
        cursor = conn.execute('DELETE FROM Match_Registration WHERE ' + where, params)
        return cursor.rowcount > 0

def delete_match_plus_one_registration(match_id, user_id, registration_id=None):
    """Remove one owned invitation and return the removed player's Telegram ID."""
    where = "registered_by_id = ? AND match_id = ? AND user_id != registered_by_id"
    params = (user_id, match_id)
    if registration_id is None:
        # Preserve the external +1 semantics of the admin command.
        where += " AND is_plus = 1"
    else:
        where += " AND registration_id = ?"
        params += (registration_id,)
    with closing(connect_db()) as conn, conn:
        row = conn.execute(
            "SELECT registration_id, user_id FROM Match_Registration WHERE " + where + " ORDER BY registration_id LIMIT 1",
            params,
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM Match_Registration WHERE registration_id = ?", (row['registration_id'],))
        return row['user_id']

# Create
from contextlib import closing
from operations.common import connect_db, execute_query, fetch_one_query, fetch_query

def create_match_registration(user_id, registered_by_id, is_plus, confirmed, priority, match_id):

    results = fetch_one_query("""
        SELECT
            COALESCE(SUM(CASE WHEN user_id = registered_by_id AND is_plus = 0 THEN 1 ELSE 0 END), 0) AS self_reg,
            COALESCE(SUM(CASE WHEN user_id != registered_by_id AND is_plus = 0 THEN 1 ELSE 0 END), 0) AS other_chat_reg,
            COALESCE(SUM(CASE WHEN user_id != registered_by_id AND is_plus != 0 THEN 1 ELSE 0 END), 0) AS extra_reg
        FROM Match_Registration
        WHERE registered_by_id = ? AND match_id = ?
    """, (registered_by_id, match_id))

    self_reg = int(results['self_reg']) if results['self_reg'] is not None else 0
    other_chat_reg = int(results['other_chat_reg']) if results['other_chat_reg'] is not None else 0
    extra_reg = int(results['extra_reg']) if results['extra_reg'] is not None else 0

    if user_id == registered_by_id and is_plus == 0:
        if int(self_reg) > 0:
            return False

    elif user_id != registered_by_id and is_plus == 0:
        if int(other_chat_reg) > 0:
            return False

    elif user_id != registered_by_id and is_plus != 0:
        if int(extra_reg) > 0:
            return False

    return execute_query("INSERT INTO Match_Registration (user_id, registered_by_id, is_plus, confirmed, priority, match_id) VALUES (?, ?, ?, ?, ?, ?)", (user_id, registered_by_id, is_plus, confirmed, priority, match_id))

# Read
def get_match_registration(match_id):
    return fetch_query("SELECT * FROM Match_Registration WHERE match_id = ?", (match_id))

def get_current_match_registrations(chat_id):
    return fetch_query("""
WITH OrderedMatches AS (
    SELECT m.match_id, m.datetime, m.amount_per_person, m.chat_id
    FROM Matches m
    WHERE m.match_id = (SELECT MAX(match_id) FROM Matches m WHERE m.chat_id = ?)
)
SELECT om.match_id, om.datetime, mr.user_id, mr.registered_by_id, mr.priority, mr.is_plus, mr.confirmed, mr.registration_id, u.nickname, u.name, ub.nickname AS registered_by_nickname
FROM OrderedMatches om
JOIN Match_Registration mr ON om.match_id = mr.match_id
JOIN Users u ON mr.user_id = u.user_id
JOIN Users ub ON mr.registered_by_id = ub.user_id
ORDER BY mr.priority ASC, mr.registered_at;
                       """, ((chat_id,)))

def check_if_user_played_before(user_id):
    return fetch_one_query("SELECT 1 FROM Match_Registration WHERE user_id = ? AND confirmed = 1", (user_id,))

def check_if_user_registered(match_id, user_id):
    return fetch_one_query("SELECT mr.registration_id, mr.confirmed, u.nickname, u.name FROM Match_Registration mr JOIN Users u ON mr.user_id = u.user_id WHERE mr.match_id = ? AND mr.user_id = ?", (match_id, user_id))


def get_invited_match_registrations(match_id, registered_by_id):
    return fetch_query("""
SELECT mr.registration_id, mr.user_id, mr.is_plus, u.nickname, u.name
FROM Match_Registration mr
LEFT JOIN Users u ON u.user_id = mr.user_id
WHERE mr.match_id = ? AND mr.registered_by_id = ?
  AND mr.user_id != mr.registered_by_id
ORDER BY mr.priority, mr.registered_at, mr.registration_id
""", (match_id, registered_by_id))

# Update
def update_match_registration(registration_id, user_id, registered_by_id, is_plus, priority, match_id):
    execute_query("UPDATE Match_Registration SET user_id = ?, registered_by_id = ?, is_plus = ?, confirmed = ?, priority = ?, match_id = ? WHERE registration_id = ?", (user_id, registered_by_id, is_plus, priority, match_id, registration_id))

def confirm_user_registration(match_id, user_id):
    execute_query("""
UPDATE Match_Registration SET confirmed = 1
WHERE match_id = ? AND user_id = ?
""", (match_id, user_id))

# Delete
def delete_match_registration(match_id, user_id):
    execute_query("DELETE FROM Match_Registration WHERE user_id = ? AND match_id = ?", (user_id, match_id))

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

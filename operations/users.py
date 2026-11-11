# Create
from operations.common import execute_query, fetch_one_query, fetch_query
from operations.common import connect_db
from operations.registration_safety import ensure_safety_tables
from contextlib import closing
from datetime import datetime
from date_utils import get_current_time


class ActiveRegistrationsError(ValueError):
    pass


def create_user(id, nickname, name):
    return execute_query("INSERT INTO Users (user_id, nickname, name) VALUES (?, ?, ?)", (id, nickname, name))

# Read
def get_all_users_from_db():
    return fetch_query("SELECT * FROM Users ORDER BY nickname")

def get_user(id):
    return fetch_one_query("SELECT * FROM Users WHERE user_id = ?", (id,))

def get_user_by_nickname(nickname):
    return fetch_one_query("SELECT * FROM Users WHERE nickname = ? COLLATE NOCASE", (nickname,))

# Update
def update_user(nickname, name):
    execute_query("UPDATE Users SET name = ? WHERE nickname = ?", (name, nickname))

# Delete
def delete_user(id):
    ensure_safety_tables()
    with closing(connect_db()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        registrations = conn.execute('''SELECT DISTINCT m.datetime FROM Matches m
            JOIN Match_Registration mr ON mr.match_id = m.match_id
            WHERE (mr.user_id = ? OR mr.registered_by_id = ?)
              AND NOT EXISTS (SELECT 1 FROM Cancelled_Matches c WHERE c.match_id = m.match_id)''', (id, id))
        now = get_current_time()
        if any(datetime.fromisoformat(row['datetime']) >= now for row in registrations):
            raise ActiveRegistrationsError('Leave upcoming matches and remove your invitations first.')
        conn.execute('DELETE FROM Users WHERE user_id = ?', (id,))

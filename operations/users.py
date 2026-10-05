# Create
from operations.common import execute_query, fetch_one_query, fetch_query


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
    execute_query("DELETE FROM Users WHERE user_id = ?", (id,))

# Create
from operations.common import execute_query, fetch_one_query, fetch_query
from operations.registration_safety import ensure_safety_tables


def create_chat(chat_id, name, game_time, game_week_day, reg_time, reg_week_day):
    return execute_query("INSERT INTO Chats (chat_id, name, game_time, game_week_day, reg_time, reg_week_day) VALUES (?, ?, ?, ?, ?, ?)", (chat_id, name, game_time, game_week_day, reg_time, reg_week_day))

def get_chat_by_id(id):
    return fetch_one_query("SELECT * FROM Chats WHERE id = ?", ((id,)))

# Read
def get_chat_by_tg_id(chat_id):
    return fetch_one_query("SELECT * FROM Chats WHERE chat_id = ?", ((chat_id,)))

def get_all_chats():
    ensure_safety_tables()
    return fetch_query('''SELECT c.*, NOT EXISTS (
        SELECT 1 FROM Paused_Registrations p WHERE p.chat_id = c.id) AS schedule_enabled
        FROM Chats c''')


def set_schedule_enabled(chat_id, enabled):
    """chat_id is the internal key; pausing never deletes the chat/history."""
    ensure_safety_tables()
    if enabled:
        execute_query('DELETE FROM Paused_Registrations WHERE chat_id = ?', (chat_id,))
    else:
        execute_query('INSERT OR IGNORE INTO Paused_Registrations VALUES (?)', (chat_id,))


def is_schedule_enabled(chat_id):
    ensure_safety_tables()
    return fetch_one_query('SELECT 1 FROM Paused_Registrations WHERE chat_id = ?', (chat_id,)) is None

# Update
def update_chat(id, chat_id, name, game_time, game_week_day, reg_time, reg_week_day):
    execute_query("UPDATE Chats SET chat_id = ?, name = ?, game_time = ?, game_week_day = ?, reg_time = ?, reg_week_day = ? WHERE id = ?", (chat_id, name, game_time, game_week_day, reg_time, reg_week_day, id))

# Delete
def delete_chat(chat_id):
    execute_query("DELETE FROM Chats WHERE chat_id = ?", (chat_id,))

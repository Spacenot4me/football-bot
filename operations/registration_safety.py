"""Additive lifecycle metadata for existing databases; no Telegram dependencies."""

from contextlib import closing
from pathlib import Path

from operations.common import connect_db


def ensure_safety_tables():
    schema = Path(__file__).resolve().parents[1] / 'migrations' / 'add_registration_safety.sql'
    with closing(connect_db()) as conn:
        conn.executescript(schema.read_text(encoding='utf-8'))


def reserve_registration_ids(conn):
    """Remember existing IDs before issuing buttons, including legacy registrations."""
    conn.execute('''UPDATE Registration_Id_Sequence
        SET last_id = MAX(last_id, (SELECT COALESCE(MAX(registration_id), 0)
                                    FROM Match_Registration)) WHERE singleton = 1''')


def initialize_registration_ids():
    ensure_safety_tables()
    with closing(connect_db()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        reserve_registration_ids(conn)


def next_registration_id(conn):
    reserve_registration_ids(conn)
    conn.execute('UPDATE Registration_Id_Sequence SET last_id = last_id + 1 WHERE singleton = 1')
    return conn.execute('SELECT last_id FROM Registration_Id_Sequence WHERE singleton = 1').fetchone()[0]

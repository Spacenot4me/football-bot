import sqlite3
from contextlib import closing

import os

DB_PATH = os.getenv("DATABASE_PATH")

def connect_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def execute_query(query, params=()):
    with closing(connect_db()) as conn, conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        return cursor.lastrowid

def fetch_query(query, params=()):
    with closing(connect_db()) as conn, conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

def fetch_one_query(query, params=()):
    with closing(connect_db()) as conn, conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        row = cursor.fetchone()
        return dict(row) if row else None

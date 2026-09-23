import logging
import os
import sqlite3
import time

import appsettings
import helper

logger = logging.getLogger(__name__)
script_dir = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(script_dir, appsettings.DB_NAME)


def open_db():
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def close_db(connection):
    connection.close()


def table_exists(connection, name: str) -> bool:
    cursor = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (name,)
    )
    return cursor.fetchone() is not None


def init_database(connection):
    cursor = connection.cursor()
    if not table_exists(connection, "users"):
        cursor.execute("""
            CREATE TABLE users (
                email TEXT NOT NULL UNIQUE,
                token TEXT UNIQUE,
                picture TEXT,
                authorized INTEGER DEFAULT 0,
                address TEXT,
                last_seen TEXT
            );
        """)
        cursor.execute(
            "INSERT INTO users (email, authorized) VALUES (?, ?);",
            (appsettings.ADMIN_EMAIL, 2),
        )
        connection.commit()

    if not table_exists(connection, "checks"):
        cursor.execute("CREATE TABLE checks (date TEXT PRIMARY KEY, done_at TEXT);")
        connection.commit()

    cursor.close()


def check_done(connection, date: str) -> bool:
    cursor = connection.execute("SELECT 1 FROM checks WHERE date = ?;", (date,))
    return cursor.fetchone() is not None


def record_check(connection, date: str):
    connection.execute(
        "INSERT OR REPLACE INTO checks (date, done_at) VALUES (?, ?);",
        (date, int(time.time())),
    )
    connection.commit()


def add_user(connection, email: str, token: str, address: str):
    connection.execute(
        "INSERT OR REPLACE INTO users (email, token, address) VALUES (?, ?, ?);",
        (email, token, address),
    )
    connection.commit()


def delete_user(connection, email: str):
    connection.execute("DELETE FROM users WHERE email = ?;", (email,))
    connection.commit()


def get_user(connection, email: str = None, token: str = None, authorized: int = None):
    if email:
        sql, params, one = "SELECT * FROM users WHERE email = ?;", (email,), True
    elif token:
        sql, params, one = "SELECT * FROM users WHERE token = ?;", (token,), True
    elif authorized is not None:
        sql, params, one = "SELECT * FROM users WHERE authorized = ?;", (authorized,), False
    else:
        sql, params, one = "SELECT * FROM users;", (), False

    cursor = connection.cursor()
    cursor.execute(sql, params)

    if one:
        row = cursor.fetchone()
        if not row:
            return None
        user = dict(row)
        if not user.get("picture"):
            user["picture"] = "/static/blank_user.png"
        connection.execute(
            "UPDATE users SET last_seen = ? WHERE email = ?;",
            (int(time.time()), user["email"]),
        )
        connection.commit()
        return user

    users = []
    for row in cursor.fetchall():
        user = dict(row)
        if not user.get("picture"):
            user["picture"] = "/static/blank_user.png"
        user["last_seen"] = helper.time_ago(user.get("last_seen"))
        users.append(user)
    return users


def update_user(connection, email: str, token: str = None, authorized: int = None,
                picture: str = None, address: str = None):
    fields = {"token": token, "authorized": authorized, "picture": picture, "address": address}
    fields = {name: value for name, value in fields.items() if value is not None}
    if not fields:
        return

    assignments = ", ".join(f"{name} = ?" for name in fields)
    connection.execute(
        f"UPDATE users SET {assignments} WHERE email = ?;",
        (*fields.values(), email),
    )
    connection.commit()


if __name__ == "__main__":
    connection = open_db()
    init_database(connection)
    close_db(connection)

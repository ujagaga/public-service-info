import logging
import json
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
    # Serialize schema upgrades when several CGI/workers start together.
    connection.execute("BEGIN IMMEDIATE")
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

    columns = {row[1] for row in cursor.execute("PRAGMA table_info(users)")}
    if "approval_token" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN approval_token TEXT")
        # Old admin approvals left approval credentials in the login-token field.
        # Preserve pending links, then invalidate all legacy login credentials once.
        cursor.execute("UPDATE users SET approval_token = token WHERE authorized = 0")
        cursor.execute("UPDATE users SET token = NULL")

    if not table_exists(connection, "checks"):
        cursor.execute("CREATE TABLE checks (date TEXT PRIMARY KEY, done_at TEXT);")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS deliveries (
            email TEXT NOT NULL,
            address TEXT NOT NULL,
            service TEXT NOT NULL,
            outage_date TEXT NOT NULL,
            sent_at INTEGER NOT NULL,
            PRIMARY KEY (email, address, service, outage_date)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS check_batches (
            date TEXT PRIMARY KEY,
            addresses_json TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_checks (
            date TEXT NOT NULL,
            service TEXT NOT NULL,
            results_json TEXT NOT NULL,
            done_at INTEGER NOT NULL,
            PRIMARY KEY (date, service)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notification_outbox (
            id INTEGER PRIMARY KEY,
            check_date TEXT NOT NULL,
            outage_date TEXT NOT NULL,
            email TEXT NOT NULL,
            address TEXT NOT NULL,
            service TEXT NOT NULL,
            details TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            sent_at INTEGER,
            UNIQUE (email, address, service, outage_date)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS outbox_pending ON notification_outbox (check_date, status)")
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


def get_check_batch(connection, date: str) -> list[str]:
    row = connection.execute("SELECT addresses_json FROM check_batches WHERE date = ?", (date,)).fetchone()
    if row:
        return json.loads(row['addresses_json'])
    addresses = get_check_addresses(connection)
    connection.execute("INSERT INTO check_batches (date, addresses_json) VALUES (?, ?)",
                       (date, json.dumps(addresses, ensure_ascii=False)))
    connection.commit()
    return addresses


def get_service_checks(connection, date: str) -> dict:
    rows = connection.execute("SELECT service, results_json FROM service_checks WHERE date = ?", (date,))
    return {row['service']: json.loads(row['results_json']) for row in rows}


def save_service_check(connection, date: str, outage_date: str, service: str, matches: dict) -> int:
    """Commit the successful analysis and its recipients together, before SMTP."""
    with connection:
        connection.execute(
            "INSERT INTO service_checks (date, service, results_json, done_at) VALUES (?, ?, ?, ?)",
            (date, service, json.dumps(matches, ensure_ascii=False), int(time.time())),
        )
        users = connection.execute("SELECT email, address FROM users WHERE authorized > 0").fetchall()
        checked = 0
        for user in users:
            result = matches.get(user['address'])
            if result is None:
                continue
            checked += 1
            if not result['outage']:
                continue
            # Also honor deliveries made by versions predating the outbox.
            connection.execute("""
                INSERT OR IGNORE INTO notification_outbox
                    (check_date, outage_date, email, address, service, details)
                SELECT ?, ?, ?, ?, ?, ? WHERE NOT EXISTS (
                    SELECT 1 FROM deliveries
                    WHERE email = ? AND address = ? AND service = ? AND outage_date = ?
                )
            """, (date, outage_date, user['email'], user['address'], service, result['details'],
                  user['email'], user['address'], service, outage_date))
    return checked


def get_pending_notifications(connection, date: str) -> list[dict]:
    # Do not retry mail for a removed/unapproved user or their old address.
    connection.execute("""
        UPDATE notification_outbox SET status = 'cancelled'
        WHERE check_date = ? AND status = 'pending' AND NOT EXISTS (
            SELECT 1 FROM users WHERE users.email = notification_outbox.email
            AND users.address = notification_outbox.address AND users.authorized > 0
        )
    """, (date,))
    connection.commit()
    rows = connection.execute("""
        SELECT * FROM notification_outbox WHERE check_date = ? AND status = 'pending'
        ORDER BY email, address, service
    """, (date,))
    return [dict(row) for row in rows]


def finish_notification_attempt(connection, notifications: list[dict], error: str | None = None):
    with connection:
        for item in notifications:
            if error is None:
                connection.execute("""
                    INSERT OR IGNORE INTO deliveries (email, address, service, outage_date, sent_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (item['email'], item['address'], item['service'], item['outage_date'], int(time.time())))
                connection.execute("""
                    UPDATE notification_outbox SET status = 'sent', sent_at = ?,
                    attempts = attempts + 1, last_error = NULL WHERE id = ?
                """, (int(time.time()), item['id']))
            else:
                connection.execute("""
                    UPDATE notification_outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?
                """, (error, item['id']))


def add_user(connection, email: str, token: str | None, address: str,
             approval_token: str | None = None, picture: str | None = None) -> bool:
    cursor = connection.execute(
        "INSERT INTO users (email, token, address, approval_token, picture) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(email) DO NOTHING;",
        (email, token, address, approval_token, picture),
    )
    connection.commit()
    return cursor.rowcount == 1


def approve_user(connection, email: str, approval_token: str | None = None) -> bool:
    sql = "UPDATE users SET authorized = 1, token = NULL, approval_token = NULL WHERE email = ? AND authorized = 0"
    params = [email]
    if approval_token is not None:
        sql += " AND approval_token = ?"
        params.append(approval_token)
    cursor = connection.execute(sql, params)
    connection.commit()
    return cursor.rowcount == 1


def revoke_token(connection, token: str):
    connection.execute("UPDATE users SET token = NULL WHERE token = ?", (token,))
    connection.commit()


def delivery_sent(connection, email: str, address: str, service: str, outage_date: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM deliveries WHERE email = ? AND address = ? AND service = ? AND outage_date = ?",
        (email, address, service, outage_date),
    ).fetchone() is not None


def record_deliveries(connection, email: str, address: str, services: list[str], outage_date: str):
    connection.executemany(
        "INSERT INTO deliveries (email, address, service, outage_date, sent_at) VALUES (?, ?, ?, ?, ?)",
        [(email, address, service, outage_date, int(time.time())) for service in services],
    )
    connection.commit()


def delete_user(connection, email: str):
    connection.execute("DELETE FROM users WHERE email = ?;", (email,))
    connection.commit()


def get_check_addresses(connection) -> list[str]:
    rows = connection.execute(
        "SELECT DISTINCT address FROM users WHERE authorized > 0 AND address IS NOT NULL ORDER BY address"
    )
    return [row['address'] for row in rows if row['address'].strip()]


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


def setup_initial_db():
    connection = open_db()
    try:
        init_database(connection)
    finally:
        close_db(connection)


if __name__ == "__main__":
    setup_initial_db()

import datetime
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
    if "signature" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN signature TEXT")
    if "price_per_address" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN price_per_address REAL")
    if "subscribed_on" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN subscribed_on TEXT")
    if "logo" not in columns:
        # data: URI of a validated PNG/JPEG, shown on the page and inlined in notification mail.
        cursor.execute("ALTER TABLE users ADD COLUMN logo TEXT")
    if "approval_token" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN approval_token TEXT")
        # Old admin approvals left approval credentials in the login-token field.
        # Preserve pending links, then invalidate all legacy login credentials once.
        cursor.execute("UPDATE users SET approval_token = token WHERE authorized = 0")
        cursor.execute("UPDATE users SET token = NULL")

    # Single row: when the last outage check started. Limits checks to one per hour.
    cursor.execute("CREATE TABLE IF NOT EXISTS last_check (id INTEGER PRIMARY KEY CHECK (id = 1), started_at INTEGER NOT NULL)")

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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL,
            amount REAL NOT NULL,
            paid_on TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payment_reminders (
            email TEXT NOT NULL,
            due_on TEXT NOT NULL,
            PRIMARY KEY (email, due_on)
        )
    """)
    if not table_exists(connection, "subscriptions"):
        # One row per (account, address, notified email); an account may watch many addresses.
        cursor.execute("""
            CREATE TABLE subscriptions (
                owner TEXT NOT NULL,
                address TEXT NOT NULL,
                email TEXT NOT NULL,
                PRIMARY KEY (owner, address, email)
            )
        """)
        # Carry over the single address accounts had before, notifying the account itself.
        cursor.execute("""
            INSERT INTO subscriptions (owner, address, email)
            SELECT email, TRIM(address), email FROM users WHERE TRIM(COALESCE(address, '')) != ''
        """)
    connection.commit()

    cursor.close()


def claim_check(connection, now: int, interval: int = 3600) -> bool:
    """True (and records the start) if no check started within the last interval seconds."""
    with connection:
        row = connection.execute("SELECT started_at FROM last_check WHERE id = 1").fetchone()
        if row and now - row['started_at'] < interval:
            return False
        connection.execute("INSERT OR REPLACE INTO last_check (id, started_at) VALUES (1, ?)", (now,))
    return True


def save_outages(connection, date: str, outage_date: str, service: str, matches: dict) -> int:
    """Queue notifications for current recipients of affected addresses, before SMTP.
    Already queued or delivered outages are ignored, so repeated checks never resend."""
    with connection:
        users = connection.execute("""
            SELECT DISTINCT s.email, s.address FROM subscriptions s
            JOIN users u ON u.email = s.owner WHERE u.authorized > 0
        """).fetchall()
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
            SELECT 1 FROM subscriptions s JOIN users u ON u.email = s.owner
            WHERE s.email = notification_outbox.email
            AND s.address = notification_outbox.address AND u.authorized > 0
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


def add_user(connection, email: str, token: str | None,
             approval_token: str | None = None, picture: str | None = None) -> bool:
    cursor = connection.execute(
        "INSERT INTO users (email, token, approval_token, picture) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(email) DO NOTHING;",
        (email, token, approval_token, picture),
    )
    connection.commit()
    return cursor.rowcount == 1


def approve_user(connection, email: str, subscribed_on: datetime.date,
                 approval_token: str | None = None) -> bool:
    sql = ("UPDATE users SET authorized = 1, token = NULL, approval_token = NULL, subscribed_on = ? "
           "WHERE email = ? AND authorized = 0")
    params = [subscription_start(subscribed_on).isoformat(), email]
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
    with connection:
        connection.execute("DELETE FROM subscriptions WHERE owner = ?;", (email,))
        connection.execute("DELETE FROM payments WHERE email = ?;", (email,))
        connection.execute("DELETE FROM payment_reminders WHERE email = ?;", (email,))
        connection.execute("DELETE FROM users WHERE email = ?;", (email,))


def get_check_addresses(connection) -> list[str]:
    rows = connection.execute("""
        SELECT DISTINCT s.address FROM subscriptions s JOIN users u ON u.email = s.owner
        WHERE u.authorized > 0 ORDER BY s.address
    """)
    return [row['address'] for row in rows]


def get_subscriptions(connection, owner: str = None) -> dict:
    """{owner: {address: [emails]}}, or {address: [emails]} for one owner."""
    sql = "SELECT owner, address, email FROM subscriptions"
    params = ()
    if owner:
        sql, params = sql + " WHERE owner = ?", (owner,)
    result = {}
    for row in connection.execute(sql + " ORDER BY address, email", params):
        result.setdefault(row['owner'], {}).setdefault(row['address'], []).append(row['email'])
    return result.get(owner, {}) if owner else result


def unsubscribe(connection, email: str, address: str):
    with connection:
        connection.execute("DELETE FROM subscriptions WHERE email = ? AND address = ?", (email, address))


def set_logo(connection, email: str, logo: str | None):
    connection.execute("UPDATE users SET logo = ? WHERE email = ?", (logo, email))
    connection.commit()


def get_signatures(connection, email: str, address: str) -> list[dict]:
    """Signatures and logos of approved accounts that notify this email about this address."""
    rows = connection.execute("""
        SELECT DISTINCT u.signature, u.logo FROM subscriptions s JOIN users u ON u.email = s.owner
        WHERE s.email = ? AND s.address = ? AND u.authorized > 0
        AND (TRIM(COALESCE(u.signature, '')) != '' OR u.logo IS NOT NULL)
        ORDER BY u.email
    """, (email, address))
    return [dict(row) for row in rows]


def set_subscription(connection, owner: str, address: str, emails: list[str]):
    """Replace the notified emails for one address; an empty list removes the address."""
    with connection:
        connection.execute("DELETE FROM subscriptions WHERE owner = ? AND address = ?", (owner, address))
        connection.executemany("INSERT OR IGNORE INTO subscriptions (owner, address, email) VALUES (?, ?, ?)",
                               [(owner, address, email) for email in emails])


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
                picture: str = None, signature: str = None):
    fields = {"token": token, "authorized": authorized, "picture": picture, "signature": signature}
    fields = {name: value for name, value in fields.items() if value is not None}
    if not fields:
        return

    assignments = ", ".join(f"{name} = ?" for name in fields)
    connection.execute(
        f"UPDATE users SET {assignments} WHERE email = ?;",
        (*fields.values(), email),
    )
    connection.commit()


def subscription_start(day: datetime.date) -> datetime.date:
    """Days 29-31 move to the 1st of next month so every month has the due day."""
    if day.day <= 28:
        return day
    return (day.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)


def add_months(day: datetime.date, months: int) -> datetime.date:
    # Safe because subscription_start() keeps the day at 28 or below.
    month = day.month - 1 + months
    return day.replace(year=day.year + month // 12, month=month % 12 + 1)


def set_subscribed_on(connection, email: str, day: datetime.date):
    connection.execute("UPDATE users SET subscribed_on = ? WHERE email = ?",
                       (subscription_start(day).isoformat(), email))
    connection.commit()


def set_price(connection, email: str, price: float):
    connection.execute("UPDATE users SET price_per_address = ? WHERE email = ?", (price, email))
    connection.commit()


def monthly_fee(connection, email: str) -> float | None:
    """Price per address times the account's current address count; None until a price is set."""
    row = connection.execute("""
        SELECT u.price_per_address AS price, (SELECT COUNT(DISTINCT address) FROM subscriptions WHERE owner = u.email) AS count
        FROM users u WHERE u.email = ?
    """, (email,)).fetchone()
    return None if row is None or row['price'] is None else row['price'] * row['count']


def add_payment(connection, email: str, amount: float, paid_on: datetime.date):
    connection.execute("INSERT INTO payments (email, amount, paid_on) VALUES (?, ?, ?)",
                       (email, amount, paid_on.isoformat()))
    connection.commit()


def update_payment(connection, payment_id: int, email: str, amount: float, paid_on: datetime.date):
    connection.execute("UPDATE payments SET amount = ?, paid_on = ? WHERE id = ? AND email = ?",
                       (amount, paid_on.isoformat(), payment_id, email))
    connection.commit()


def delete_payment(connection, payment_id: int, email: str):
    connection.execute("DELETE FROM payments WHERE id = ? AND email = ?", (payment_id, email))
    connection.commit()


def get_payments(connection) -> dict:
    """{email: [{'id', 'amount', 'paid_on'}, ...]}, newest first."""
    result = {}
    for row in connection.execute("SELECT id, email, amount, paid_on FROM payments ORDER BY paid_on DESC, id DESC"):
        result.setdefault(row['email'], []).append({'id': row['id'], 'amount': row['amount'], 'paid_on': row['paid_on']})
    return result


def next_due(subscribed_on: str, payment_count: int) -> datetime.date:
    """Each payment covers one month; the first is due a month after subscribing."""
    return add_months(datetime.date.fromisoformat(subscribed_on), payment_count + 1)


def missed_payments(subscribed_on: str, payment_count: int, today: datetime.date) -> int:
    """How many due dates up to today are not covered by payments."""
    missed = 0
    while next_due(subscribed_on, payment_count + missed) <= today:
        missed += 1
    return missed


def unreminded_dues(connection, today: datetime.date) -> list[tuple[str, datetime.date]]:
    """Every unpaid due date up to today that admins were not yet emailed about."""
    rows = connection.execute("""
        SELECT u.email, u.subscribed_on, (SELECT COUNT(*) FROM payments p WHERE p.email = u.email) AS paid
        FROM users u WHERE u.authorized = 1 AND u.subscribed_on IS NOT NULL ORDER BY u.email
    """).fetchall()
    reminded = {(row['email'], row['due_on']) for row in connection.execute("SELECT * FROM payment_reminders")}
    dues = []
    for row in rows:
        months = row['paid'] + 1
        while (due := add_months(datetime.date.fromisoformat(row['subscribed_on']), months)) <= today:
            if (row['email'], due.isoformat()) not in reminded:
                dues.append((row['email'], due))
            months += 1
    return dues


def record_reminders(connection, dues: list[tuple[str, datetime.date]]):
    connection.executemany("INSERT OR IGNORE INTO payment_reminders (email, due_on) VALUES (?, ?)",
                           [(email, due.isoformat()) for email, due in dues])
    connection.commit()


def setup_initial_db():
    connection = open_db()
    try:
        init_database(connection)
    finally:
        close_db(connection)


if __name__ == "__main__":
    setup_initial_db()

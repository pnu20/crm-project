"""Copy CRM data from backend/crm.sqlite3 into the PostgreSQL database in .env.

Run once from the project root, before using the PostgreSQL-backed API:

    .\\.venv\\Scripts\\python.exe scripts\\migrate_sqlite_to_postgres.py

The sample rows that the API seeds into empty tables are replaced by the
SQLite data. The script refuses to run if PostgreSQL already has a user.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.main import connect, initialize_database  # noqa: E402

SQLITE_PATH = Path(__file__).resolve().parent.parent / "backend" / "crm.sqlite3"


def main() -> None:
    """Create the PostgreSQL tables and copy every SQLite row into them."""
    if not SQLITE_PATH.exists():
        sys.exit(f"SQLite database not found: {SQLITE_PATH}")

    source = sqlite3.connect(SQLITE_PATH)
    source.row_factory = sqlite3.Row
    initialize_database()

    with connect() as target:
        if target.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]:
            sys.exit("PostgreSQL already has users; migration skipped.")

        target.execute("TRUNCATE contacts, deals, tasks")
        cursor = target.cursor()
        cursor.executemany(
            """INSERT INTO contacts
               (id, first_name, last_name, email, phone, company, role, status,
                last_contact, owner, tone)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [tuple(row) for row in source.execute(
                """SELECT id, first_name, last_name, email, phone, company, role,
                          status, last_contact, owner, tone FROM contacts"""
            )],
        )
        # Insert in SQLite rowid order so the new seq column keeps the same ordering.
        cursor.executemany(
            "INSERT INTO deals (id, name, contact, value, stage) VALUES (%s, %s, %s, %s, %s)",
            [tuple(row) for row in source.execute(
                "SELECT id, name, contact, value, stage FROM deals ORDER BY rowid"
            )],
        )
        cursor.executemany(
            "INSERT INTO tasks (id, title, contact, due, done) VALUES (%s, %s, %s, %s, %s)",
            [(*tuple(row)[:4], bool(row["done"])) for row in source.execute(
                "SELECT id, title, contact, due, done FROM tasks ORDER BY rowid"
            )],
        )
        cursor.executemany(
            """INSERT INTO users (id, full_name, email, password_hash, created_at)
               OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, %s, %s)""",
            [tuple(row) for row in source.execute(
                "SELECT id, full_name, email, password_hash, created_at FROM users"
            )],
        )
        target.execute(
            """SELECT setval(pg_get_serial_sequence('users', 'id'),
                             COALESCE((SELECT MAX(id) FROM users), 1))"""
        )
        cursor.executemany(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
            [tuple(row) for row in source.execute(
                "SELECT token_hash, user_id, expires_at FROM sessions"
            )],
        )

        for table in ("contacts", "deals", "tasks", "users", "sessions"):
            count = target.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            print(f"{table}: {count}")

    source.close()


if __name__ == "__main__":
    main()

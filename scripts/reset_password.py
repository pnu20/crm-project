"""Reset a CRM account's password from the command line.

Run from the project root:
    .\\.venv\\Scripts\\python.exe scripts\\reset_password.py you@company.com

The new password is read without echoing it, and every existing session for the
account is signed out.
"""

from __future__ import annotations

import sys
from getpass import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.main import connect, hash_password  # noqa: E402


# Prompt twice for a password that meets the sign-in rules.
def read_new_password() -> str:
    """Return a confirmed password of 12 to 128 characters."""
    while True:
        password = getpass("New password (at least 12 characters): ")
        if not 12 <= len(password) <= 128:
            print("The password must be 12 to 128 characters long.")
        elif getpass("Confirm new password: ") != password:
            print("The passwords do not match.")
        else:
            return password


def main() -> None:
    """Set the new password hash and revoke the account's sessions."""
    if len(sys.argv) != 2:
        sys.exit("Usage: python scripts/reset_password.py <email>")
    email = sys.argv[1].strip()
    with connect() as connection:
        user = connection.execute(
            "SELECT id FROM users WHERE lower(email) = lower(%s)", (email,)
        ).fetchone()
    if user is None:
        sys.exit(f"No account uses {email}.")

    password = read_new_password()
    with connect() as connection:
        connection.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (hash_password(password), user["id"]),
        )
        connection.execute("DELETE FROM sessions WHERE user_id = %s", (user["id"],))
    print(f"Password updated for {email}. Sign in with the new password.")


if __name__ == "__main__":
    main()

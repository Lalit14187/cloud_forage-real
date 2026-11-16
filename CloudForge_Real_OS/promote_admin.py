"""Promote an existing local demo account to administrator."""
import sys

from app import DB
import sqlite3

if len(sys.argv) != 2:
    raise SystemExit("Usage: python promote_admin.py USERNAME")

username = sys.argv[1]
with sqlite3.connect(DB) as database:
    database.row_factory = sqlite3.Row
    account = database.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if not account:
        raise SystemExit(f"No account found for username: {username}")
    result = database.execute("UPDATE users SET role='admin' WHERE username=?", (username,))
    if result.rowcount != 1:
        raise SystemExit(f"No account found for username: {username}")
    database.execute("INSERT INTO activity(user_id,action,detail) VALUES(?,?,?)",
                     (account['id'], 'Role changed', f'{username} promoted to administrator'))
print(f"{username} is now an administrator. Sign out and sign in again.")

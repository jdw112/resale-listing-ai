"""Admin-account bootstrap CLI.

There is no self-service path to an admin account — `/v1/auth/register` only ever
creates role `submitter` (by design: admin is the privileged role behind the Admin
Console). This module is how the *first* admin is created, and how a user is
promoted, without hand-writing SQL.

Two entry points:

  python -m resale_listing_ai.admin create-admin EMAIL [--password P]
      Interactive/explicit: create EMAIL as an admin, or promote them if they
      already exist. Password from --password, else $RESALE_LISTING_AI_ADMIN_PASSWORD,
      else an interactive prompt.

  python -m resale_listing_ai.admin ensure-admin
      Idempotent, env-driven, for container startup: if $RESALE_LISTING_AI_ADMIN_EMAIL is
      set, make sure that admin exists (create with $RESALE_LISTING_AI_ADMIN_PASSWORD, or
      promote an existing user). A no-op when the env var is unset. Never changes
      an existing user's password.

Both are idempotent and safe to re-run: an existing admin is left alone, an
existing non-admin is promoted (password untouched), and only a brand-new user
has a password set.
"""

import argparse
import getpass
import os
import sys

from . import auth
from .db import Store


def _ensure(store, email, password, *, allow_prompt):
    """Create `email` as an admin, or promote an existing user. Returns a short
    status string. Only sets a password when creating a new user."""
    existing = store.get_user_by_email(email)
    if existing is not None:
        if existing["role"] == "admin":
            return f"unchanged: {email} is already an admin (id={existing['id']})"
        store.set_user_role(existing["id"], "admin")
        return f"promoted: {email} (id={existing['id']}) submitter -> admin"

    # New user — a password is required.
    if not password and allow_prompt and sys.stdin.isatty():
        password = getpass.getpass(f"Password for new admin {email}: ")
    if not password:
        raise SystemExit(
            f"error: {email} does not exist and no password was given "
            "(pass --password or set RESALE_LISTING_AI_ADMIN_PASSWORD)."
        )
    password_hash = auth.hash_password(password)  # enforces bcrypt's 72-byte limit
    user_id = store.create_user(email, password_hash, role="admin")
    return f"created: {email} (id={user_id}) as admin"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="resale_listing_ai.admin", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create-admin", help="create or promote an admin account")
    p_create.add_argument("email")
    p_create.add_argument("--password", default=os.environ.get("RESALE_LISTING_AI_ADMIN_PASSWORD"))

    sub.add_parser("ensure-admin", help="idempotently ensure $RESALE_LISTING_AI_ADMIN_EMAIL is an admin (for startup)")

    args = parser.parse_args(argv)

    store = Store.connect()
    store.ensure_schema()
    try:
        if args.cmd == "create-admin":
            print(_ensure(store, args.email, args.password, allow_prompt=True))
        elif args.cmd == "ensure-admin":
            email = os.environ.get("RESALE_LISTING_AI_ADMIN_EMAIL")
            if not email:
                print("ensure-admin: RESALE_LISTING_AI_ADMIN_EMAIL unset — skipping admin bootstrap.")
                return
            password = os.environ.get("RESALE_LISTING_AI_ADMIN_PASSWORD")
            print(_ensure(store, email, password, allow_prompt=False))
    finally:
        store.close()


if __name__ == "__main__":
    main()

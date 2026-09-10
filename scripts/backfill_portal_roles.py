"""Backfill ``custom:portal_role`` on portal Cognito users.

``resolve_portal_role`` now defaults an unset/invalid role to ``member`` (was
``admin``). Run this once per environment BEFORE deploying that change so current
users keep the access they have.

    python scripts/backfill_portal_roles.py --environment dev --dry-run
    python scripts/backfill_portal_roles.py --environment dev --set admin
    python scripts/backfill_portal_roles.py --environment dev --set admin --only alice,bob

``--set`` is the role written to users that currently have no valid role.
``--only`` (comma list) restricts the write to those usernames; everyone else is
left untouched (i.e. will fall to the new ``member`` default). Users that already
carry a valid ``admin``/``member`` role are never modified.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: E402, F401

from hiveflow.dna.web.portal.cognito import (  # noqa: E402
    PORTAL_ROLE_ADMIN,
    PORTAL_ROLE_MEMBER,
    ROLE_ATTRIBUTE,
    _cognito_client,
    load_cognito_config,
)


def _iter_users(client, user_pool_id: str):
    token = None
    while True:
        kwargs = {"UserPoolId": user_pool_id, "Limit": 60}
        if token:
            kwargs["PaginationToken"] = token
        resp = client.list_users(**kwargs)
        yield from resp.get("Users", [])
        token = resp.get("PaginationToken")
        if not token:
            break


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default="dev")
    parser.add_argument("--company", default="poc", help="only used to locate Cognito config")
    parser.add_argument(
        "--set",
        dest="role",
        choices=[PORTAL_ROLE_ADMIN, PORTAL_ROLE_MEMBER],
        default=PORTAL_ROLE_ADMIN,
    )
    parser.add_argument("--only", default="", help="comma-separated usernames to write; blank = all")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_cognito_config(company=args.company, environment=args.environment)
    if config is None:
        print("Cognito is not configured (HIVEFLOW_COGNITO_USER_POOL_ID / _CLIENT_ID).", file=sys.stderr)
        return 2

    only = {u.strip() for u in args.only.split(",") if u.strip()}
    client = _cognito_client(config.region)

    written = skipped_existing = skipped_filtered = 0
    for user in _iter_users(client, config.user_pool_id):
        username = str(user.get("Username", "")).strip()
        attrs = {a["Name"]: a.get("Value", "") for a in user.get("Attributes", [])}
        current = attrs.get(ROLE_ATTRIBUTE, "").strip().lower()
        if current in (PORTAL_ROLE_ADMIN, PORTAL_ROLE_MEMBER):
            skipped_existing += 1
            continue
        if only and username not in only:
            skipped_filtered += 1
            print(f"  skip (not in --only): {username}")
            continue
        print(f"  {'DRY-RUN ' if args.dry_run else ''}set {ROLE_ATTRIBUTE}={args.role}: {username}")
        if not args.dry_run:
            client.admin_update_user_attributes(
                UserPoolId=config.user_pool_id,
                Username=username,
                UserAttributes=[{"Name": ROLE_ATTRIBUTE, "Value": args.role}],
            )
        written += 1

    print(
        f"\n{'would write' if args.dry_run else 'wrote'} {written}; "
        f"kept {skipped_existing} with an existing role; "
        f"left {skipped_filtered} for the member default"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

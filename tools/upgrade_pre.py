"""Pre-upgrade step for the half-upgraded production database.

The live database already contains a working multi-shop *catalogue* (Shops,
CustomUsers, MasterProducts and per-shop prices) built by an earlier partial
upgrade, while the sales ledger is still on the legacy schema.  To let the
official ``tracker.0001_initial`` migration build the complete schema we:

1. rename the already-built tables to ``old_*`` so they are preserved and can
   be merged back afterwards, and
2. drop ``django_admin_log`` (its rows are disposable) because its user
   foreign key would otherwise keep pointing at the renamed user table.

The legacy sales/product tables are left alone: the migration moves those
aside itself (``legacy_*``) and imports them.
"""

from __future__ import annotations

import os
import sys

import django

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

ASIDE = {
    "tracker_shop": "old_shop",
    "tracker_masterproduct": "old_masterproduct",
    "tracker_shopproduct": "old_shopproduct",
    "tracker_customuser": "old_customuser",
    "tracker_customuser_groups": "old_customuser_groups",
    "tracker_customuser_user_permissions": "old_customuser_user_permissions",
}


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "profit_tracker_pro.settings")
    django.setup()

    from django.db import connection

    if connection.vendor != "postgresql":
        raise SystemExit(
            "Refusing to run: this upgrade is for the hosted Postgres database. "
            "Re-run with USE_DATABASE_URL=True."
        )

    existing = set(connection.introspection.table_names())

    for old_name, new_name in ASIDE.items():
        if old_name not in existing:
            print(f"skip (missing): {old_name}")
            continue
        if new_name in existing:
            print(f"skip (already moved): {old_name} -> {new_name}")
            continue
        with connection.cursor() as cursor:
            cursor.execute(f'ALTER TABLE "{old_name}" RENAME TO "{new_name}"')
        print(f"moved: {old_name} -> {new_name}")

    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS django_admin_log CASCADE")
    print("dropped: django_admin_log (recreated by admin migration)")


if __name__ == "__main__":
    main()

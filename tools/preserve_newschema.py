"""Preserve and restore the half-built multi-shop catalogue.

The live database already held Shops, CustomUsers, MasterProducts and per-shop
prices before the official upgrade.  Those tables carry constraint names that
clash with the new schema, so they are exported to JSON, dropped, and merged
back after ``tracker.0001_initial`` has run.

Usage::

    USE_DATABASE_URL=True python tools/preserve_newschema.py export
    USE_DATABASE_URL=True python tools/preserve_newschema.py restore backups/preserved_YYYYmmdd_HHMMSS.json
"""

from __future__ import annotations

import datetime
import json
import os
import sys

import django

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
BACKUPS = os.path.join(PROJECT_ROOT, "backups")

TABLES = (
    "old_shop",
    "old_customuser",
    "old_masterproduct",
    "old_shopproduct",
)


def _jsonable(value):
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if hasattr(value, "quantize"):
        return str(value)
    return value


def _rows(cursor, sql):
    cursor.execute(sql)
    columns = [c[0] for c in cursor.description]
    return [
        {name: _jsonable(value) for name, value in zip(columns, row)}
        for row in cursor.fetchall()
    ]


def export() -> None:
    from django.db import connection

    os.makedirs(BACKUPS, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(BACKUPS, f"preserved_{stamp}.json")

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY(%s)",
            [list(TABLES)],
        )
        existing = {row[0] for row in cursor.fetchall()}

        payload = {"shops": [], "users": [], "products": [], "prices": []}

        if "old_shop" in existing:
            payload["shops"] = _rows(
                cursor, "SELECT id, name, location, manager_id FROM old_shop ORDER BY id"
            )
        if "old_customuser" in existing:
            payload["users"] = _rows(
                cursor,
                "SELECT id, username, password, last_login, is_superuser, "
                "first_name, last_name, email, is_staff, is_active, date_joined, "
                "role, assigned_shop_id FROM old_customuser ORDER BY id",
            )
        if "old_masterproduct" in existing:
            payload["products"] = _rows(
                cursor, "SELECT id, name, category FROM old_masterproduct ORDER BY id"
            )
        if "old_shopproduct" in existing:
            payload["prices"] = _rows(
                cursor,
                "SELECT shop_id, product_id, cost_price, selling_price, is_active "
                "FROM old_shopproduct ORDER BY id",
            )

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)

    print(
        f"Exported to {path}: {len(payload['shops'])} shops, "
        f"{len(payload['users'])} users, {len(payload['products'])} products, "
        f"{len(payload['prices'])} prices"
    )


def restore(path: str) -> None:
    from decimal import Decimal

    from django.db import connection

    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)

    with connection.cursor() as cursor:
        # ---- shops (created by the migration; update location by name) ----
        shop_id_to_name = {}
        for shop in payload["shops"]:
            shop_id_to_name[shop["id"]] = shop["name"]
            cursor.execute(
                "UPDATE tracker_shop SET location = %s WHERE name = %s",
                [shop["location"] or "", shop["name"]],
            )

        cursor.execute("SELECT id, name FROM tracker_shop")
        name_to_shop_id = {name: pk for pk, name in cursor.fetchall()}
        print(f"shops in new schema: {sorted(name_to_shop_id)}")

        # ---- users ----
        user_id_to_shop_name = {
            user["id"]: shop_id_to_name.get(user["assigned_shop_id"])
            for user in payload["users"]
        }
        for user in payload["users"]:
            shop_name = user_id_to_shop_name.get(user["id"])
            shop_id = name_to_shop_id.get(shop_name) if shop_name else None
            values = [
                user["password"],
                user["last_login"],
                bool(user["is_superuser"]),
                user["first_name"] or "",
                user["last_name"] or "",
                user["email"] or "",
                bool(user["is_staff"]),
                bool(user["is_active"]),
                user["date_joined"],
                user["role"],
                shop_id,
            ]
            cursor.execute(
                "UPDATE tracker_customuser SET password=%s, last_login=%s, "
                "is_superuser=%s, first_name=%s, last_name=%s, email=%s, "
                "is_staff=%s, is_active=%s, date_joined=%s, role=%s, "
                "assigned_shop_id=%s WHERE username=%s",
                values + [user["username"]],
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    "INSERT INTO tracker_customuser "
                    "(username, password, last_login, is_superuser, first_name, "
                    "last_name, email, is_staff, is_active, date_joined, role, "
                    "assigned_shop_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [user["username"]] + values,
                )
            print(f"user restored: {user['username']} ({user['role']}, shop={shop_name})")

        # ---- link each shop to its manager (first manager assigned to it) ----
        for shop_name, shop_id in name_to_shop_id.items():
            cursor.execute(
                "SELECT id FROM tracker_customuser "
                "WHERE assigned_shop_id = %s AND role = 'MANAGER' ORDER BY id LIMIT 1",
                [shop_id],
            )
            row = cursor.fetchone()
            if row:
                cursor.execute(
                    "UPDATE tracker_shop SET manager_id = %s WHERE id = %s",
                    [row[0], shop_id],
                )

        # ---- products ----
        legacy_master_id_to_key = {
            product["id"]: (product["name"], product["category"] or "")
            for product in payload["products"]
        }
        for product in payload["products"]:
            cursor.execute(
                "SELECT id FROM tracker_masterproduct WHERE name = %s AND category = %s",
                [product["name"], product["category"] or ""],
            )
            if cursor.fetchone() is None:
                cursor.execute(
                    "INSERT INTO tracker_masterproduct (name, category) VALUES (%s, %s)",
                    [product["name"], product["category"] or ""],
                )

        cursor.execute("SELECT id, name, category FROM tracker_masterproduct")
        key_to_master_id = {(name, category): pk for pk, name, category in cursor.fetchall()}

        # ---- per-shop prices (curated cards win over the imported defaults) ----
        updated = created = 0
        for price in payload["prices"]:
            key = legacy_master_id_to_key.get(price["product_id"])
            shop_id = name_to_shop_id.get(shop_id_to_name.get(price["shop_id"]))
            if key is None or shop_id is None:
                continue
            master_id = key_to_master_id.get(key)
            if master_id is None:
                continue
            cursor.execute(
                "SELECT id FROM tracker_shopproduct WHERE shop_id = %s AND product_id = %s",
                [shop_id, master_id],
            )
            row = cursor.fetchone()
            if row:
                cursor.execute(
                    "UPDATE tracker_shopproduct SET cost_price=%s, selling_price=%s, "
                    "is_active=%s WHERE id=%s",
                    [
                        Decimal(price["cost_price"]),
                        Decimal(price["selling_price"]),
                        bool(price["is_active"]),
                        row[0],
                    ],
                )
                updated += 1
            else:
                cursor.execute(
                    "INSERT INTO tracker_shopproduct "
                    "(shop_id, product_id, cost_price, selling_price, is_active) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    [
                        shop_id,
                        master_id,
                        Decimal(price["cost_price"]),
                        Decimal(price["selling_price"]),
                        bool(price["is_active"]),
                    ],
                )
                created += 1

    print(f"prices restored: {updated} updated, {created} created")


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "profit_tracker_pro.settings")
    django.setup()

    from django.db import connection

    if connection.vendor != "postgresql":
        raise SystemExit("Refusing to run against SQLite. Use USE_DATABASE_URL=True.")

    action = sys.argv[1] if len(sys.argv) > 1 else "export"
    if action == "export":
        export()
    elif action == "restore":
        restore(sys.argv[2])
    else:
        raise SystemExit("Usage: export | restore <file>")


if __name__ == "__main__":
    main()

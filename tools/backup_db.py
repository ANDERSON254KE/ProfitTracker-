"""Dump every public table of the configured database to a restorable SQL file.

Used as a safety net before the multi-shop schema upgrade.  The output is a
plain SQL script of ``INSERT`` statements (plus ``TRUNCATE`` guards) that can be
replayed with psql or any SQL client such as DBeaver.
"""

from __future__ import annotations

import datetime
import os
import sys
from decimal import Decimal
from pathlib import Path

import django

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "profit_tracker_pro.settings")
    django.setup()

    from django.db import connection

    out_dir = Path(__file__).resolve().parent.parent / "backups"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"db_backup_{stamp}.sql"

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
            "ORDER BY table_name"
        )
        tables = [row[0] for row in cursor.fetchall()]

        with out_path.open("w", encoding="utf-8") as fh:
            fh.write(f"-- Backup of {connection.settings_dict['NAME']}\n")
            fh.write(f"-- Generated {stamp}\n\n")
            fh.write("BEGIN;\n")
            fh.write("SET CONSTRAINTS ALL DEFERRED;\n\n")

            for table in tables:
                cursor.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s "
                    "ORDER BY ordinal_position",
                    [table],
                )
                columns = [row[0] for row in cursor.fetchall()]
                quoted_cols = ", ".join(f'"{c}"' for c in columns)

                cursor.execute(f'SELECT {quoted_cols} FROM "{table}"')
                rows = cursor.fetchall()

                fh.write(f"-- {table}: {len(rows)} row(s)\n")
                fh.write(f'TRUNCATE TABLE "{table}" CASCADE;\n')
                for row in rows:
                    values = ", ".join(_literal(cursor, v) for v in row)
                    fh.write(
                        f'INSERT INTO "{table}" ({quoted_cols}) VALUES ({values});\n'
                    )
                fh.write("\n")

            fh.write("COMMIT;\n")

    print(f"Wrote {out_path} ({out_path.stat().st_size:,} bytes) for {len(tables)} table(s)")


def _literal(cursor, value):
    """Render a Python value as a SQL literal for the active backend."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return "'" + value.isoformat() + "'"
    if isinstance(value, (bytes, memoryview)):
        return cursor.mogrify("%s", [value]).decode()
    return cursor.mogrify("%s", [value]).decode()


if __name__ == "__main__":
    main()

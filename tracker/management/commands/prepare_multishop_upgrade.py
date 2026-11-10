"""Prepare an existing database for the multi-shop upgrade.

The tracker app's history was rebased into a single ``0001_initial`` (see that
migration's docstring for why). That means Django must be told to re-run the
``tracker`` migrations; otherwise it sees ``0001_initial`` as already applied
and the new tables are never created.

``admin``'s records are cleared at the same time: its ``LogEntry`` table
references the swapped-in user model, and Django refuses to run when it has an
already-applied migration that should come later in the graph. Both apps'
tables still exist, so the follow-up ``migrate --fake-initial`` recreates
neither.

This command is safe to run twice and refuses to touch anything when the
database is already on the new schema.

Usage::

    python manage.py prepare_multishop_upgrade            # check, then prepare
    python manage.py prepare_multishop_upgrade --noinput  # skip the prompt
    python manage.py migrate --fake-initial               # afterwards
"""

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

#: Tables the previous schema owned, used to detect an un-upgraded database.
LEGACY_TABLES = [
    "tracker_dailysale",
    "tracker_product",
    "tracker_productshopprice",
    "tracker_transaction",
    "tracker_dailyexpense",
    "tracker_daytotals",
]


class Command(BaseCommand):
    """Clear the tracker's recorded migrations so the new schema is built."""

    help = "Reset the tracker app's migration records for the multi-shop upgrade."

    def add_arguments(self, parser):
        """Add the ``--noinput`` flag used for unattended deployments."""
        parser.add_argument(
            "--noinput",
            action="store_true",
            help="Do not prompt; proceed without confirmation.",
        )

    def handle(self, *args, **options):
        """Verify the database shape, then clear the tracker's migration rows."""
        existing = set(connection.introspection.table_names())
        legacy_found = [name for name in LEGACY_TABLES if name in existing]
        already_new = "tracker_masterproduct" in existing and "tracker_shop" in existing

        if already_new and not legacy_found:
            self.stdout.write(
                self.style.SUCCESS(
                    "Database is already on the multi-shop schema. Nothing to do."
                )
            )
            return

        if not legacy_found:
            self.stdout.write(
                self.style.WARNING(
                    "No legacy tracker tables found. This looks like a fresh "
                    "database -- just run 'migrate'."
                )
            )
            return

        self.stdout.write(self.style.WARNING("Legacy tables found:"))
        for name in legacy_found:
            self.stdout.write(f"  - {name}")

        self.stdout.write("")
        self.stdout.write(
            "The upgrade will move these tables aside, copy the data into the "
            "new schema and then drop them. Take a database backup first."
        )

        if not options["noinput"]:
            answer = input("Type 'yes' to continue: ").strip().lower()
            if answer != "yes":
                self.stdout.write("Aborted. Nothing was changed.")
                return

        recorder = MigrationRecorder(connection)
        recorder.ensure_schema()

        # ``tracker`` must re-run, and ``admin`` must be forgotten so the
        # migration graph can put ``tracker.0001_initial`` first (admin's
        # LogEntry table points at the swapped-in user model). Both apps'
        # tables already exist, so ``migrate --fake-initial`` recreates neither.
        deleted = 0
        for app in ("admin", "tracker"):
            deleted += recorder.migration_qs.filter(app=app).delete()[0]

        self.stdout.write(
            self.style.SUCCESS(
                f"Cleared {deleted} recorded migration(s) for admin/tracker.\n"
                "Now run:  python manage.py migrate --fake-initial\n"
                "Then:     python manage.py setup_initial_data"
            )
        )

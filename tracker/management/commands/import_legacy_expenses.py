"""Import dated expenses from the retired ``legacy_dailyexpense`` table.

The legacy table stores shop names, dates, descriptions and amounts but has no
user foreign key. Imported rows are attributed to the shop's manager when
available, otherwise to an owner. ``legacy_source_id`` makes the import safe to
run again.

Usage::

    python manage.py import_legacy_expenses
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from tracker.models import CustomUser, Shop, ShopExpense


class Command(BaseCommand):
    """Copy legacy expenses into the current shop expense ledger."""

    help = "Import historical expenses from legacy_dailyexpense without duplicates."

    def handle(self, *args, **options):
        if "legacy_dailyexpense" not in connection.introspection.table_names():
            self.stdout.write(self.style.WARNING("No legacy_dailyexpense table was found; nothing imported."))
            return

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, shop, date, description, amount FROM legacy_dailyexpense ORDER BY id"
            )
            rows = cursor.fetchall()

        shops = {shop.name.casefold(): shop for shop in Shop.objects.all()}
        owners = list(CustomUser.objects.filter(role=CustomUser.Role.OWNER).order_by("id"))
        if rows and not owners and not any(shop.manager_id for shop in shops.values()):
            raise CommandError("Cannot attribute legacy expenses: no owner or shop manager exists.")

        created = skipped = 0
        with transaction.atomic():
            for source_id, shop_name, expense_date, description, amount in rows:
                shop = shops.get(str(shop_name).strip().casefold())
                if shop is None:
                    raise CommandError(
                        f"Legacy expense {source_id} references unknown shop {shop_name!r}."
                    )
                recorder = shop.manager
                if recorder is None or recorder.role != CustomUser.Role.MANAGER:
                    recorder = CustomUser.objects.filter(
                        role=CustomUser.Role.MANAGER, assigned_shop=shop
                    ).order_by("id").first()
                if recorder is None:
                    recorder = owners[0] if owners else None
                if recorder is None:
                    raise CommandError(f"No user is available to record legacy expense {source_id}.")

                _, was_created = ShopExpense.objects.get_or_create(
                    legacy_source_id=source_id,
                    defaults={
                        "shop": shop,
                        "expense_date": expense_date,
                        "description": str(description or "Legacy expense").strip()[:200],
                        "amount": amount,
                        "recorded_by": recorder,
                    },
                )
                if was_created:
                    created += 1
                else:
                    skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f"Imported {created} historical expense(s); skipped {skipped} already imported."
        ))

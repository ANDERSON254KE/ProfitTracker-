"""Remove exact duplicate sale rows left behind by a repeated bulk import.

A bulk import that ran twice inserts the same shop/product/day row again, so
the daily sales sheet and profit sheet count that product twice and one day's
total ends up looking like a copy of another's. This command collapses groups
of *identical* rows -- same units, prices, revenue, cost, profit, stock figures
and profit-sheet flag -- to the first row, leaving genuinely separate sales
alone, and then recalculates the saved daily profit for the affected days.

    python manage.py dedupe_daily_sales            # report only
    python manage.py dedupe_daily_sales --apply    # delete the extra rows
"""

import json
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from tracker.models import DailySale
from tracker.views import _save_shop_daily_profit


#: Business fields that must match for two rows to count as the same entry.
IDENTICAL_FIELDS = (
    "units_sold",
    "unit_cost_at_sale",
    "unit_price_at_sale",
    "total_revenue",
    "total_cost",
    "profit",
    "opening_stock",
    "additional_stock",
    "total_stock",
    "closing_stock",
    "is_profit_sheet_entry",
)


class Command(BaseCommand):
    """Delete exact duplicate sale rows for the same shop, product and day."""

    help = (
        "Delete extra DailySale rows that are identical to the first row for the "
        "same shop, product and day, then recalculate that day's saved profit."
    )

    def add_arguments(self, parser):
        """Add the ``--apply`` switch; without it nothing is deleted."""
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete the duplicate rows. Without this the command only reports.",
        )

    def handle(self, *args, **options):
        """Find identical duplicate groups, then collapse them when asked to."""
        groups = (
            DailySale.objects.values("shop_id", "product_id", "sale_date")
            .annotate(n=Count("id"))
            .filter(n__gt=1)
        )

        targets = []
        for group in groups:
            rows = list(
                DailySale.objects.filter(
                    shop_id=group["shop_id"],
                    product_id=group["product_id"],
                    sale_date=group["sale_date"],
                )
                .select_related("shop", "product")
                .order_by("pk")
            )
            signature = {
                tuple(getattr(row, field) for field in IDENTICAL_FIELDS) for row in rows
            }
            if len(signature) != 1:
                continue  # different figures: separate sales, not an import duplicate
            targets.append((rows[0], rows[1:]))

        if not targets:
            self.stdout.write(
                self.style.SUCCESS("Nothing to dedupe: no identical duplicate rows found.")
            )
            return

        removed = [row for _, extra in targets for row in extra]
        self.stdout.write(
            f"Groups to collapse: {len(targets)} ({len(removed)} row(s) to remove)"
        )
        for keep, extra in targets:
            self.stdout.write(
                f"  {keep.sale_date} {keep.shop.name} {keep.product.name}: "
                f"keep #{keep.pk} ({keep.units_sold} units), remove "
                + ", ".join(f"#{row.pk}" for row in extra)
            )

        if not options["apply"]:
            self.stdout.write(
                self.style.WARNING("Dry run only. Re-run with --apply to delete.")
            )
            return

        backup_dir = Path(settings.BASE_DIR) / "backups"
        backup_dir.mkdir(exist_ok=True)
        backup_path = (
            backup_dir / f"duplicate-sales-{datetime.now():%Y%m%d_%H%M%S}.json"
        )
        backup_path.write_text(
            json.dumps(
                {
                    "created": datetime.now().isoformat(timespec="seconds"),
                    "removed": [
                        {
                            "id": row.pk,
                            "shop": row.shop.name,
                            "product": str(row.product),
                            "sale_date": row.sale_date.isoformat(),
                            "units_sold": str(row.units_sold),
                            "total_revenue": str(row.total_revenue),
                            "profit": str(row.profit),
                        }
                        for row in removed
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        self.stdout.write(f"Backed up the removed rows to {backup_path}")

        with transaction.atomic():
            DailySale.objects.filter(pk__in=[row.pk for row in removed]).delete()

            recalculated = 0
            seen = set()
            for keep, _ in targets:
                key = (keep.shop_id, keep.sale_date)
                if key in seen:
                    continue
                seen.add(key)
                record = _save_shop_daily_profit(
                    keep.shop, keep.sale_date, keep.recorded_by, create=False
                )
                if record is not None:
                    recalculated += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Removed {len(removed)} duplicate row(s) and recalculated "
                f"{recalculated} saved daily profit record(s)."
            )
        )

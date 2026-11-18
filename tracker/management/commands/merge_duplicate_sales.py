"""Fold older sale rows into the manual entry recorded for the same day.

Before the single-row-per-day rule, the profit sheet only looked for other
profit-sheet rows when it saved a day. A product that already had an imported
or legacy sale gained a second row beside it, and the reports summed both, so
the day was counted twice.

This command keeps the latest manual (profit-sheet) figure for each shop,
product and day and deletes the older non-stock rows next to it, then
recalculates the saved daily profit for the affected days. Stock rows and days
with several separate sale records are left untouched: those are real sales,
not a correction of the same entry.

    python manage.py merge_duplicate_sales            # report only
    python manage.py merge_duplicate_sales --apply    # delete the stale rows
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


class Command(BaseCommand):
    """Merge manual profit-sheet entries with older sale rows for the same day."""

    help = (
        "Delete older non-stock sale rows that sit beside a manual profit-sheet "
        "entry for the same shop, product and day, and recalculate the day."
    )

    def add_arguments(self, parser):
        """Add the ``--apply`` switch; without it nothing is deleted."""
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete the stale rows. Without this the command only reports.",
        )

    def handle(self, *args, **options):
        """Find the overlapping groups, then merge them when asked to."""
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
                ).select_related("shop", "product")
            )
            manual = [
                row
                for row in rows
                if row.is_profit_sheet_entry and row.total_stock is None
            ]
            older = [
                row
                for row in rows
                if not row.is_profit_sheet_entry and row.total_stock is None
            ]
            stock = [row for row in rows if row.total_stock is not None]
            if not manual or not older or stock:
                continue
            keep = max(manual, key=lambda row: row.pk)
            targets.append((keep, [row for row in older if row.pk != keep.pk]))

        if not targets:
            self.stdout.write(self.style.SUCCESS("Nothing to merge: no day has a manual entry beside an older sale."))
            return

        removed_units = sum(
            (row.units_sold for _, stale in targets for row in stale),
            0,
        )
        removed_profit = sum(
            (row.profit for _, stale in targets for row in stale),
            0,
        )
        self.stdout.write(f"Days to correct: {len(targets)}")
        for keep, stale in targets:
            self.stdout.write(
                f"  {keep.sale_date} {keep.shop.name} {keep.product.name}: "
                f"keep #{keep.pk} ({keep.units_sold} units), remove "
                + ", ".join(f"#{row.pk} ({row.units_sold} units)" for row in stale)
            )
        self.stdout.write(
            f"Double-counted: {removed_units} unit(s), {removed_profit:,.2f} profit"
        )

        if not options["apply"]:
            self.stdout.write(
                self.style.WARNING("Dry run only. Re-run with --apply to merge.")
            )
            return

        backup_dir = Path(settings.BASE_DIR) / "backups"
        backup_dir.mkdir(exist_ok=True)
        backup_path = backup_dir / f"duplicate-sales-{datetime.now():%Y%m%d_%H%M%S}.json"
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
                            "unit_cost_at_sale": str(row.unit_cost_at_sale),
                            "unit_price_at_sale": str(row.unit_price_at_sale),
                            "total_revenue": str(row.total_revenue),
                            "total_cost": str(row.total_cost),
                            "profit": str(row.profit),
                            "recorded_by": row.recorded_by.username,
                            "created_at": row.created_at.isoformat(),
                        }
                        for _, stale in targets
                        for row in stale
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        self.stdout.write(f"Backed up the removed rows to {backup_path}")

        with transaction.atomic():
            for _, stale in targets:
                for row in stale:
                    row.delete()

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
                f"Merged {len(targets)} day(s) and recalculated "
                f"{recalculated} saved daily profit record(s)."
            )
        )

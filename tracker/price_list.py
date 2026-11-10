"""Row order of the master ``price list.xlsx``.

Every screen that lists products shows them in the order the owner's
spreadsheet uses -- 250ml first, then 350ml, 750ml, cans, soda -- rather than
alphabetically. The daily stock sheet and the per-shop price-sheet generator
both sort through this module, so they always agree with the file.

The workbook is read once and cached against its modification time, so
editing the spreadsheet is picked up without restarting the server. If the
file is missing or unreadable the order comes back empty and callers fall
back to sorting by name.
"""

import os

from django.conf import settings

#: Workbook maintained by the owner and read by ``import_price_list``.
PRICE_LIST_FILENAME = "price list.xlsx"

#: Single-entry cache: ``{"key": (path, mtime), "value": ((name, category), ...)}``.
_cache = {"key": None, "value": ()}


def price_list_path() -> str:
    """Return the path of the master price list."""
    return os.path.join(str(settings.BASE_DIR), PRICE_LIST_FILENAME)


def price_list_order(path=None) -> tuple:
    """Return ``(name, category)`` pairs in spreadsheet row order.

    Args:
        path: workbook to read; defaults to :func:`price_list_path`.

    Returns:
        A tuple of pairs, or an empty tuple when the file cannot be read.
    """
    path = path or price_list_path()
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return ()
    if _cache["key"] == key:
        return _cache["value"]

    rows = []
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(path, read_only=True, data_only=True)
        sheet = workbook.active
        for row in sheet.iter_rows(min_row=2, values_only=True):
            if not row or row[0] is None:
                continue
            name = str(row[0]).strip()
            if not name or name.lower() == "none":
                continue
            category = ""
            if len(row) > 3 and row[3] is not None:
                category = str(row[3]).strip()
            rows.append((name, category))
        workbook.close()
    except Exception:  # pragma: no cover - depends on the file
        rows = []

    _cache["key"] = key
    _cache["value"] = tuple(rows)
    return _cache["value"]


def price_list_rank(path=None) -> dict:
    """Return ``{(name, category): row index}`` for sorting queries.

    Products that are not in the spreadsheet get ``len(order)`` from
    :meth:`dict.get` callers, i.e. they sort alphabetically after everything
    the file does list.
    """
    return {pair: index for index, pair in enumerate(price_list_order(path))}

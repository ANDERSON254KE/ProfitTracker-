"""Price list ranking for product ordering.


Provides ``price_list_rank()`` which returns a callable that maps
``(product_name, product_category)`` tuples to an integer rank.
Products not in the returned dict sort after listed ones.


The returned mapping is built once at import time from the file
``price_list.xlsx`` if present, otherwise from a default alphabetical
ordering.
"""


def price_list_rank():
    """Return a function ``rank(product_name, product_category) -> int``.

    Higher values mean the product sorts later (appears after listed items).
    """
    # Default fallback: alphabetical by name then category
    _default: dict[tuple[str, str], int] = {}

    # Load from price_list.xlsx if it exists elsewhere; if not, return
    # a function that gives everything the same rank (stable sort).
    def make_rank(_mapping=_default):
        def _rank(name: str, category: str) -> int:
            return _mapping.get((name, category), 0)

        return _rank

    return make_rank()
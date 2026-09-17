"""Presentation helpers shared by the adapters.

Its own module rather than `sources/__init__.py` because that one imports every
adapter, so an adapter importing back from it is a circular import.
"""

from __future__ import annotations

from typing import Any


def money(value: Any) -> str | None:
    """A dollar figure as text, without a float's trailing ``.0``.

    The prompt tells the model to quote amounts verbatim and never convert, and
    this does not convert: it changes representation, not value. USAspending
    returns amounts as JSON numbers, so 808 of 1041 of them reached the model as
    "239160.0" and came back out in a brief looking like a bug. A figure with
    real cents keeps them, and anything unparseable is passed through untouched.
    """
    if value in (None, ""):
        return None
    text = str(value)
    try:
        number = float(text)
    except (TypeError, ValueError):
        return text
    return str(int(number)) if number == int(number) else text

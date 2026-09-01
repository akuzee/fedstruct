"""Source registry.

Adding a source means adding a class here and a cadence in config.py. There is
no plugin discovery and no schema inference: a new source maps onto the
existing predicate vocabulary or it does not get ingested (plan §11 R12).
"""

from __future__ import annotations

from .base import Source
from .ecfr import ECFRAgencies
from .federal_register import FederalRegisterAgencies
from .plum import PlumPositions

ALL_SOURCES: dict[str, Source] = {
    s.name: s for s in (
        FederalRegisterAgencies(),
        ECFRAgencies(),
        PlumPositions(),
    )
}


def get(name: str) -> Source:
    try:
        return ALL_SOURCES[name]
    except KeyError:
        raise KeyError(
            f"unknown source {name!r}; known: {', '.join(sorted(ALL_SOURCES))}"
        ) from None


__all__ = ["ALL_SOURCES", "get", "Source"]

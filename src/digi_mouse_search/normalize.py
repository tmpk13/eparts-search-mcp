"""Cross-distributor part matching."""

from __future__ import annotations

import re

from .models import MergedPart, Part

_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def mpn_key(mpn: str) -> str:
    """Canonical form of a manufacturer part number for equality testing.

    Case and punctuation vary between distributors for the same part
    (LM317T vs lm317-t), so both are stripped. Packaging and lifecycle
    suffixes such as /NOPB or TR are deliberately kept: they distinguish
    genuinely different orderable parts, and dropping them would merge
    entries that are not interchangeable.
    """
    return _NON_ALNUM.sub("", mpn.upper())


def merge_parts(parts: list[Part]) -> list[MergedPart]:
    """Group offers by part number, preserving the order parts first appeared.

    Grouping is on part number alone. Manufacturer names differ in spelling
    and punctuation between distributors often enough that requiring them to
    match would split real duplicates more often than it would prevent a
    false merge.
    """
    grouped: dict[str, MergedPart] = {}
    for part in parts:
        key = mpn_key(part.mpn)
        if not key:
            continue
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = MergedPart(
                mpn=part.mpn,
                manufacturer=part.manufacturer,
                description=part.description,
                offers=[part],
            )
            continue
        existing.offers.append(part)
        if not existing.manufacturer:
            existing.manufacturer = part.manufacturer
        if not existing.description:
            existing.description = part.description
    return list(grouped.values())

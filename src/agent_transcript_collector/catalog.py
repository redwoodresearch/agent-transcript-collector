"""Catalog of transcript archives stored in S3.

The collector uploads one zip per size-budgeted unit under a source-first key:

    <source>/<contributor>/<group-hash>/part-NNN-<members-hash>.zip

Historic / non-Claude sources use shallower keys (``<source>/<id>/x.zip`` or even
``<source>/x.zip``), so parsing is deliberately lenient: the first path segment is
always the source, the second (when present) is the contributor/collection, and a
key is a "unit" iff it ends in ``.zip``. This module turns a flat object listing
into :class:`Unit` records plus per-source / per-contributor aggregates that the
CLI and TUI render.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .sources.base import human_size


@dataclass(frozen=True)
class Unit:
    """One uploaded archive (a single ``.zip`` object in the bucket)."""

    key: str
    size: int
    source: str
    contributor: str  # "" when the key has no contributor/collection segment

    @property
    def size_human(self) -> str:
        return human_size(self.size)


def parse_key(key: str, size: int) -> Unit | None:
    """Parse an S3 object key into a :class:`Unit`, or ``None`` if not an archive."""
    if not key.endswith(".zip"):
        return None
    parts = key.split("/")
    source = parts[0]
    contributor = parts[1] if len(parts) >= 3 else ""
    return Unit(key=key, size=size, source=source, contributor=contributor)


def list_units(s3, bucket: str, prefix: str | None = None) -> list[Unit]:
    """List every archive unit in the bucket (optionally under ``prefix``)."""
    paginator = s3.get_paginator("list_objects_v2")
    kwargs = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix
    units: list[Unit] = []
    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents", []):
            unit = parse_key(obj["Key"], obj["Size"])
            if unit is not None:
                units.append(unit)
    return units


def filter_units(
    units: list[Unit],
    sources: list[str] | None = None,
    contributors: list[str] | None = None,
    prefix: str | None = None,
) -> list[Unit]:
    """Subset ``units`` by source(s), contributor(s), and/or key prefix (all ANDed)."""
    source_set = set(sources) if sources else None
    contributor_set = set(contributors) if contributors else None
    out: list[Unit] = []
    for u in units:
        if source_set is not None and u.source not in source_set:
            continue
        if contributor_set is not None and u.contributor not in contributor_set:
            continue
        if prefix is not None and not u.key.startswith(prefix):
            continue
        out.append(u)
    return out


@dataclass
class Aggregate:
    """Unit count and total size for a group of units."""

    count: int = 0
    bytes: int = 0

    @property
    def size_human(self) -> str:
        return human_size(self.bytes)


def aggregate_by_source(units: list[Unit]) -> dict[str, Aggregate]:
    out: dict[str, Aggregate] = defaultdict(Aggregate)
    for u in units:
        agg = out[u.source]
        agg.count += 1
        agg.bytes += u.size
    return dict(sorted(out.items()))


def aggregate_by_contributor(units: list[Unit]) -> dict[tuple[str, str], Aggregate]:
    """Aggregate by ``(source, contributor)``."""
    out: dict[tuple[str, str], Aggregate] = defaultdict(Aggregate)
    for u in units:
        agg = out[(u.source, u.contributor)]
        agg.count += 1
        agg.bytes += u.size
    return dict(sorted(out.items()))

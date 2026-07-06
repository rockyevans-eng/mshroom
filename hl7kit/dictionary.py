"""Static HL7 v2.5.1 field-name dictionary.

Loads ``data/fields_v251.json`` once (segment ID -> {field number ->
human-readable name}) and exposes a lookup that never raises: unknown
segments (Z-segments) or unknown field numbers simply return ``None`` so
the Viewer tree can fall back to showing the bare notation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

_DATA_PATH = Path(__file__).parent / "data" / "fields_v251.json"

_table: Optional[dict[str, dict[str, str]]] = None


def _load() -> dict[str, dict[str, str]]:
    global _table
    if _table is None:
        with open(_DATA_PATH, "r", encoding="utf-8") as fh:
            _table = json.load(fh)
    return _table


def field_name(segment_id: str, field_index: int) -> Optional[str]:
    """Human-readable field name for *segment_id* field *field_index*.

    Returns ``None`` for unknown segments (including Z-segments) or field
    numbers not in the dictionary -- never raises.
    """
    table = _load()
    seg = table.get(segment_id.upper())
    if seg is None:
        return None
    return seg.get(str(field_index))


def known_segments() -> list[str]:
    """The list of segment IDs the dictionary has field names for."""
    return sorted(_load().keys())

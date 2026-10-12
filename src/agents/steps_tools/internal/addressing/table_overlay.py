from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from itertools import pairwise
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agents.schemas.internal.addressing.address_space import Region


def apply_table_geometry(body: str, regions: list[Region], bundle: dict) -> None:
    """Positive row existence only; missing geometry never proves a missing row."""
    text_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    source_hash = bundle.get("source_pdf_sha256")
    valid_bundle = (
        bundle.get("canonical_text_sha256") == text_hash
        and bundle.get("canonical_text_length") == len(body)
        and isinstance(source_hash, str)
        and re.fullmatch(r"[0-9a-f]{64}", source_hash) is not None
        and isinstance(bundle.get("rows"), list)
    )
    pending: dict[tuple[int, str], list[tuple[Region, list[tuple[int, int]]]]] = defaultdict(list)
    if valid_bundle:
        assert isinstance(source_hash, str)
        for row in bundle["rows"]:
            binding = _row_binding(body, regions, row, source_hash, text_hash)
            if binding is None:
                continue
            region, number, spans = binding
            pending[(region.char_start, number)].append((region, spans))
    for (_region_start, number), candidates in pending.items():
        if len(candidates) != 1:
            for region, _spans in candidates:
                region.geometry_unknown.add(number)
            continue
        region, spans = candidates[0]
        region.geometry_rows[number] = tuple(spans)

        if any(body[left[1] : right[0]].strip() for left, right in pairwise(spans)):
            region.geometry_fragmented.add(number)
            continue
        start, end = spans[0][0], spans[-1][1]

        for previous, (item_start, item_end) in list(region.item_spans.items()):
            if previous == number or not (item_start < start < item_end):
                continue
            trimmed = body[item_start:start].rstrip()
            region.items[previous] = trimmed
            region.item_spans[previous] = (item_start, item_start + len(trimmed))
        region.items[number] = body[start:end]
        region.item_spans[number] = (start, end)


def _row_binding(
    body: str, regions: list[Region], row: object, source_hash: str, text_hash: str
) -> tuple[Region, str, list[tuple[int, int]]] | None:
    if not isinstance(row, dict) or (
        row.get("status") != "exact"
        or row.get("source_pdf_sha256") != source_hash
        or row.get("expected_source_pdf_sha256") not in (None, source_hash)
        or row.get("canonical_text_sha256") != text_hash
        or row.get("canonical_text_length") != len(body)
        or row.get("exact_fragment_coverage") != 1.0
        or type(row.get("row_glyph_count")) is not int
        or row["row_glyph_count"] <= 0
        or row.get("exact_glyph_count") != row["row_glyph_count"]
    ):
        return None
    number = row.get("target_row_number")
    if not isinstance(number, str) or re.fullmatch(r"[0-9]{1,3}", number) is None:
        return None
    cells = row.get("cells")
    if not isinstance(cells, (list, tuple)) or not any(
        isinstance(cell, dict)
        and cell.get("column_index") == 0
        and cell.get("status") == "exact"
        and re.fullmatch(rf"\s*{re.escape(number)}[.)]?\s*", str(cell.get("text") or ""))
        for cell in cells
    ):
        return None
    fragments = row.get("fragments")
    if not isinstance(fragments, (list, tuple)) or not fragments:
        return None
    spans = []
    glyphs = 0
    for fragment in fragments:
        if not isinstance(fragment, dict):
            return None
        start, end = fragment.get("canonical_char_start"), fragment.get("canonical_char_end")
        if (
            fragment.get("status") not in ("exact", "continuation")
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(body)
            or body[start:end] != fragment.get("text")
            or type(fragment.get("glyph_count")) is not int
            or fragment["glyph_count"] <= 0
        ):
            return None
        spans.append((start, end))
        glyphs += fragment["glyph_count"]
    spans.sort()
    if glyphs != row["row_glyph_count"] or any(a[1] > b[0] for a, b in pairwise(spans)):
        return None
    matches = [
        region
        for region in regions
        if region.space.value == "table" and region.char_start <= spans[0][0] and spans[-1][1] <= region.char_end
    ]
    if len(matches) != 1:
        return None
    return matches[0], str(int(number)), spans

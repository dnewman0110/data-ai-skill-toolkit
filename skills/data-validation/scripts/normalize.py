#!/usr/bin/env python3
"""
normalize.py -- value/row normalization applied before ANY hashing or comparison in
data-validation. This is the single place false-positive discrepancies from formatting rather
than real content differences get eliminated, per the toolkit's explicit requirement: ordering,
nulls, floats, and timezones are the four classic sources of spurious diffs.

- **Ordering**: this module does not sort rows itself. Rows are matched between source and target
  by declared/candidate KEY, not by fetch position -- two independent queries (possibly against
  two different connections/platforms) have no guaranteed relative order, and sorting-then-hashing
  sequentially is fragile the moment either side has an extra or missing row (it desyncs
  everything after that point rather than localizing the one real difference). Keying by candidate
  key, done by the caller (compare_staged.py), is what actually neutralizes ordering as a source
  of false positives -- see references/staged-comparison.md.
- **Nulls**: normalized to a fixed sentinel (`_NULL_SENTINEL`) distinct from any real value,
  applied consistently on both sides, so a NULL never accidentally hash-collides with an empty
  string or a literal "None"/"null" text value coming from a source that stores nulls as text.
- **Floats**: numeric-looking values (real floats, decimals, or numeric-looking strings -- see the
  type-mismatch handling below) are coerced to float and rounded to a fixed precision before
  hashing, so a source storing 129.990000001 and a target storing 129.99 don't register as a
  content difference.
- **Timezones**: values that parse as timestamps are normalized to UTC ISO-8601 before hashing,
  so a source in local time and a target in UTC representing the same instant compare equal.
"""
import re
from datetime import datetime, timezone

_NULL_SENTINEL = " __NULL__ "
FLOAT_PRECISION = 4

_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")
_DATE_FORMATS = [
    "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
]


def _try_parse_timestamp(value: str):
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def normalize_value(value, treat_as_numeric: bool = False, treat_as_timestamp: bool = False,
                     float_precision: int = FLOAT_PRECISION):
    """Normalize a single value for comparison/hashing. `treat_as_numeric` and
    `treat_as_timestamp` are column-level decisions the caller makes once (from declared types
    and/or the looks_numeric-style check), not re-guessed per value -- a column is consistently
    numeric or not, consistently a timestamp or not.
    """
    if value is None:
        return _NULL_SENTINEL

    if treat_as_numeric:
        try:
            return round(float(value), float_precision)
        except (TypeError, ValueError):
            return value  # couldn't coerce -- fall through, comparison will surface the mismatch honestly

    if treat_as_timestamp and isinstance(value, str):
        parsed = _try_parse_timestamp(value)
        if parsed is not None:
            return parsed.isoformat()
        return value

    if isinstance(value, float):
        return round(value, float_precision)

    return value


def infer_column_treatment(source_type: str | None, target_type: str | None,
                            sample_values: list) -> dict:
    """Decide, once per column, whether it should be treated as numeric or as a timestamp for
    normalization purposes -- based on declared types OR (for TEXT-declared columns, exactly the
    total_amt-as-TEXT kind of type mismatch this toolkit's fixtures plant on purpose) on whether
    sampled non-null values parse cleanly as numbers/timestamps. Deterministic: same inputs,
    same decision, every time.
    """
    numeric_type_markers = ("int", "real", "double", "float", "decimal", "numeric")
    declared_numeric = any(
        t and any(m in t.lower() for m in numeric_type_markers) for t in (source_type, target_type)
    )
    non_null_samples = [v for v in sample_values if v is not None]

    treat_as_numeric = declared_numeric
    if not treat_as_numeric and non_null_samples:
        treat_as_numeric = all(_NUMERIC_RE.match(str(v)) for v in non_null_samples)

    treat_as_timestamp = False
    if not treat_as_numeric and non_null_samples:
        treat_as_timestamp = all(_try_parse_timestamp(str(v)) is not None for v in non_null_samples)

    return {"treat_as_numeric": treat_as_numeric, "treat_as_timestamp": treat_as_timestamp}


def normalize_row(row: dict, column_treatments: dict[str, dict]) -> dict:
    """column_treatments: {column_name: {"treat_as_numeric": bool, "treat_as_timestamp": bool}},
    as produced by infer_column_treatment per column."""
    normalized = {}
    for col, value in row.items():
        treatment = column_treatments.get(col, {})
        normalized[col] = normalize_value(
            value,
            treat_as_numeric=treatment.get("treat_as_numeric", False),
            treat_as_timestamp=treatment.get("treat_as_timestamp", False),
        )
    return normalized


def row_hash(normalized_row: dict, column_order: list[str]) -> str:
    """Stable hash of a normalized row. column_order is explicit (not dict iteration order,
    which callers should not rely on) so the same logical row always hashes the same way
    regardless of what order columns came back from a query in."""
    import hashlib
    parts = [repr(normalized_row.get(c, _NULL_SENTINEL)) for c in column_order]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

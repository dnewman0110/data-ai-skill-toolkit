# Normalization and type coercion

`skills/data-validation/scripts/normalize.py` is the one place every source of spurious diff
noise gets eliminated before anything is hashed or compared. If you're debugging a validation
result that looks wrong, this is where to look first -- a "discrepancy" that's actually a
formatting artifact is a bug in this module, not a real data problem.

## Ordering

Handled structurally, not by a normalization function: rows are matched between source and target
by their declared/candidate KEY (`compare_staged.py` builds a `{key_tuple: row}` dict per side),
never by fetch position. Two independent queries -- especially against two different
connections/platforms -- have no guaranteed relative row order, and "sort both sides the same way
then hash sequentially" is fragile the moment either side has one extra or missing row: every row
after that point desyncs, and you get a wall of false "changed" results instead of one true
"missing" result. Keying by candidate key sidesteps this entirely.

## Nulls

`normalize_value(None, ...)` always returns a fixed sentinel string, applied identically on both
sides before hashing. Without this, a NULL could accidentally hash-collide with an empty string or
a literal text `"None"`/`"null"` coming from a source system that stores nulls as text rather than
as a true NULL -- a real and common source of both false positives (NULL vs `""` treated as
different when they shouldn't be, if you hashed the raw Python `None` and `""` inconsistently) and
false negatives (NULL and `"null"` text treated as the same when they're actually different data).

## Floats

Any column judged numeric (see "Type coercion" below) is coerced to `float` and rounded to a fixed
precision (`FLOAT_PRECISION = 4`, i.e. 4 decimal places) before hashing. Without this, a source
storing `129.990000001` (floating-point representation noise) and a target storing `129.99` would
hash differently despite being the same value for any practical purpose. 4 decimal places is a
fixed, documented default appropriate for currency-shaped measures (this toolkit's fixture data);
a project with different precision needs (e.g. scientific measurements needing more decimal places)
should override it explicitly rather than silently inherit a default tuned for a different domain
-- treat the default as a starting point to confirm, not a universal constant.

## Timezones

Values that parse as timestamps (per a fixed set of common formats -- `%Y-%m-%d`,
`%Y-%m-%dT%H:%M:%S`, with/without a `Z` or explicit offset) are converted to UTC and rendered as
ISO-8601 before hashing. A naive (timezone-unaware) timestamp is assumed to already be UTC -- this
is a real limitation worth stating plainly: if a source stores local time without a timezone
marker and a target stores UTC, this normalizer cannot detect and correct that mismatch on its own
(there's no timezone information in the source value to convert FROM). If you know a column is
local-time-without-marker, that's exactly the kind of thing that belongs in
`known_acceptable_differences` (or better, a data-contract test) rather than something the
normalizer should guess at.

## Type coercion: how a TEXT-vs-numeric mismatch is detected, not just tolerated

`infer_column_treatment(source_type, target_type, sample_values)` decides, ONCE per column (not
per value), whether to treat it as numeric for normalization purposes:

1. If either side's declared type is a recognizable numeric type (int/real/double/float/decimal/
   numeric, case-insensitive substring match), the column is treated as numeric on both sides --
   this is what makes a source TEXT column and a target REAL column compare correctly, exactly the
   `total_amt` scenario this toolkit's fixtures plant on purpose (silver.orders.total_amt is TEXT,
   gold.legacy_fct_orders.total_amt is REAL; after coercion, `426.29` from either side normalizes
   to the same rounded float).
2. If neither side is declared numeric, but every SAMPLED non-null value (first 20 rows fetched
   from each side) parses cleanly as a float, the column is still treated as numeric -- catches a
   type mismatch where BOTH sides happen to store the value as text.
3. Otherwise, the column is compared as-is (string comparison after null/whitespace normalization
   implicit in Python's own value equality).

This is a deterministic, evidence-based decision (declared type, or 100% of a real sample parsing
as a number) -- never a guess dressed up as a fact. If a column's values only *sometimes* parse as
numeric, it is NOT treated as numeric (the `all()` check in `infer_column_treatment` requires every
sampled value to parse), and the resulting string-comparison behavior is the honest fallback rather
than a coercion that would silently swallow the very inconsistency worth flagging.

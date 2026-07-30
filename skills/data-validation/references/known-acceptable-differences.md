# Known-acceptable differences

Some discrepancies are expected: a documented timing lag between source and target refresh, a
deliberate filter that excludes certain rows by design, a column that's intentionally
re-derived (not copied) and will never byte-match. Re-discovering and re-explaining the same
accepted gap every single run wastes review time and trains people to skim past the review gate --
declare it once instead.

## Declaration format

In `toolkit.yaml`'s `validation.known_acceptable_differences` (or passed at runtime via
`--known-acceptable-differences-json` to `build_validation_findings.py`), a list of rules:

```yaml
validation:
  known_acceptable_differences:
    - type: column_ignore
      column: last_synced_at
      description: "Sync timestamp column; expected to differ by definition, not a real discrepancy."
      declared_by: dave24188@gmail.com
    - type: key_ignore
      key: [5230, 1]     # or {order_id: 5230, line_number: 1} -- both accepted
      description: "Known orphaned-customer order, accepted by the client as out of scope for this migration."
      declared_by: dave24188@gmail.com
```

Two rule types, deliberately not a general expression language:

- **`column_ignore`**: any discrepancy whose `columns_affected` includes this column is excluded,
  regardless of which row it's on. Use for a column that's expected to differ structurally (a
  timestamp, a re-derived value, a column the target intentionally drops or recomputes).
- **`key_ignore`**: any discrepancy on this specific row key is excluded, regardless of which
  columns differ. Use for a specific, individually-reviewed-and-accepted exception (an orphaned
  row the business has decided not to remediate, a known one-off data issue).

## Why not arbitrary code / a general expression language

A rule like `"column == 'total_amt' and abs(diff) < 0.01"` is more expressive, but it's also a
place for `eval()`-shaped risk and for silent, hard-to-audit scope creep (a "tolerance" rule that
quietly swallows real discrepancies as they grow past what anyone intended). The two fixed rule
types above cover the overwhelmingly common cases (an entire column is expected to differ; a
specific row is an accepted, reviewed exception) without introducing a code-execution surface or a
rule so flexible nobody can tell what it actually excludes by reading it.

## What "excluded" means in the report

A discrepancy matching a rule is NOT silently dropped -- it's moved from `discrepancies[]` to
`known_acceptable_differences_excluded[]` in the final report, recording the rule's `description`,
`declared_by`, and which rule matched. `summary.match` is computed from `discrepancies[]` only
(after exclusion), so a run with only known-acceptable differences correctly reports `match: true`
-- but the report still shows, explicitly, what was excluded and on whose authority, so nobody
downstream mistakes "matched after exclusions" for "matched with nothing to review."

## Declaring a new exclusion

Adding a rule is itself a decision worth a moment's friction -- it's silencing a signal this skill
would otherwise raise every run. Prefer `key_ignore` for anything that hasn't been reviewed
broadly (a specific, understood exception) and reserve `column_ignore` for things that are
structurally, permanently expected to differ. Revisit `known_acceptable_differences` periodically;
a rule that made sense at one point in an engagement can hide a real regression later if the
underlying assumption stops holding (e.g. a "sync lag" column_ignore rule would hide a genuine sync
failure, not just normal lag, unless something else is watching that specifically).

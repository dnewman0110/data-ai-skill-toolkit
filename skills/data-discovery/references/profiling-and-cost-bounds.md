# Profiling and cost bounds

## The pre-flight gate

Every run of `skills/data-discovery/scripts/build_findings.py` starts by calling
`scripts/estimate_scan_cost.py`'s `estimate_and_gate` against every requested target, using
`row_count(exact=False)` and `estimate_bytes()` -- both cheap, no-full-read operations (see
`scripts/lakehouse_adapter.py`). This happens BEFORE any column is profiled. If the estimate
exceeds `toolkit.yaml`'s `cost_and_blast_radius.max_rows_scanned` or `max_bytes_scanned`,
`build_findings.py` halts (`"halted": true`, exit code 1) and profiles nothing.

**What to do when it halts**: show the user the printed `cost_decision` (estimated rows/bytes per
target, which threshold was exceeded) and ask before proceeding. Only re-run with `--force` after
an explicit, in-conversation go-ahead that names which targets to proceed on. Never pass `--force`
preemptively "to save a round trip" -- the whole point of the gate is that a large client estate
can make discovery expensive enough to page someone, and that decision isn't this skill's to make
alone.

## Sampling strategy

`--sample-size` bounds every per-column profiling operation (`profile_column`, `check_uniqueness`,
`count_orphans`) to at most that many rows, via a `LIMIT` pushed into the query -- not a full read
followed by in-memory truncation. Default: `toolkit.yaml`'s `cost_and_blast_radius.default_sample_size`
(10,000 in the example config). Omit `--sample-size` (or pass `None`) to profile the full
population -- appropriate for a small table (this skill's own fixture tables are all under a few
hundred rows and always sampled "fully" as a side effect) but something you should choose
deliberately for a real client table, not default into.

Every profiling result records `sample_covers_full_population` (or `checked_full_population` for
uniqueness/FK checks) precisely so confidence scoring can account for it later, per
`contracts/confidence-rubric.md`'s "population coverage of profiling" note -- a uniqueness check
against a full scan sits higher in its confidence band than the same check against a sample, and a
downstream reader needs to know which one happened.

## What counts as "full-scale" here

For discovery specifically, "full-scale" is a full, unsampled profile of a large table --
`COUNT(DISTINCT ...)`, `MIN`/`MAX`, and orphan checks all read every row when unsampled. The
pre-flight gate exists precisely so a `--sample-size`-less invocation against a genuinely large
table doesn't happen by accident. Default to sampling; treat an unsampled full profile as opt-in,
same as any other skill's full scan (`references/toolkit-conventions.md` #4).

## Row/byte caps on what leaves this skill

Independent of scan cost, `--max-sample-records` (default 20, from `toolkit.yaml`'s
`sample_data.max_sample_records`) caps how many actual rows make it into
`discovery_findings.json` and therefore into the final contract's `sample_records[]` --
`scripts/redact.py` enforces the cap and the sensitive-column redaction/hashing at the same time.
This cap is about output size and client-data exposure, not query cost -- a table can be profiled
against its full population (for accurate stats) while still only surfacing 20 example rows.

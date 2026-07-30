# Grain determination and test proposal

## Grain determination tiers

Exactly the tiers in `contracts/data-contract.schema.json`'s `grain.determination` enum and
`contracts/confidence-rubric.md`'s evidence classes -- discovery doesn't invent its own scale:

1. **`explicit_constraint`** -- a declared `PRIMARY KEY` (or `UNIQUE` constraint) on the candidate
   columns. `skills/data-discovery/scripts/profile_object.py` surfaces this automatically as a
   `declared_primary_key`-sourced entry in `candidate_keys[]`. If it's `is_unique: true`, this is
   your grain, full stop -- no confidence score needed, this is measured.
2. **`profiled_unique_key`** -- no declared constraint, but a candidate key profiles as unique
   across the full population (`checked_full_population: true` in the findings). Confidence per
   the rubric's 0.75-0.94 band if corroborated by naming/comments, otherwise the top of the
   0.5-0.74 band. If the check only covered a sample (`checked_full_population: false`), that
   caps confidence lower within the same band per the rubric's "population coverage" guidance --
   say so in the `basis` string explicitly.
3. **`name_and_type_inference`** -- you're proposing a grain based on column naming/shape without
   a passing uniqueness check to back it (e.g. you believe the grain SHOULD be
   `(order_id, line_number)` but couldn't profile the full population). Weak; 0.5-0.74 band at
   best, usually lower.
4. **`llm_inference_from_naming`** -- pure domain-knowledge guess, no profiling evidence at all.
   0.2-0.49 band. Ship this only when a downstream human review gate will genuinely catch it if
   wrong (see `references/toolkit-conventions.md` #6) -- and say plainly in your report to the
   user that grain is unconfirmed.
5. **`undetermined`** -- you could not support any grain statement with evidence. **Halt.** Do not
   emit a contract with an undetermined-but-guessed grain; a downstream consumer (a human building
   a pipeline, or `data-pipeline` itself) will treat whatever you write as fact. This is the one
   place this skill's grounding-and-failure-behavior rule is "halt," not "flag" or "ask" -- because
   there's no single clarifying question that reliably fixes an ambiguous grain, and a
   low-confidence flag on the *foundational* fact of a contract is too easy to miss or
   rubber-stamp past.

`skills/data-discovery/scripts/profile_object.py` always checks the declared PK (or, absent one, a
positional first-column/first-two-columns guess) AND any column matching a natural-key naming
pattern (`_number`, `_code`, `_key`, `email` suffixes) as an independent candidate, specifically so
a duplicated *business* key doesn't hide behind a clean *surrogate* key -- see its module
docstring for why. Read every entry in `candidate_keys[]`, not just the first one that's unique.

## Test proposal rules

`skills/data-discovery/scripts/propose_tests.py` derives `tests[]` entirely from
`profile_object.py`'s output -- no LLM involved, nothing invented. The rules, so you can sanity
check its output rather than trust it blindly:

| Test type | Proposed when | `threshold_basis` | Threshold value |
|---|---|---|---|
| `uniqueness` | Every entry in `candidate_keys[]` | `explicit_constraint` (declared PK) or `profiled` (everything else) | N/A (pass/fail) |
| `nullability` | Declared `NOT NULL`, OR profiled null rate is 0%, OR profiled null rate is nonzero but ≤10% | `explicit_constraint` or `profiled` | 0, or the observed null rate rounded up to the next whole percentage point |
| `referential` | Every declared or explicitly-checked FK | `explicit_constraint` (declared) or `profiled` (checked via `--candidate-fk`) | N/A (pass/fail) |
| `range` | Numeric, non-identifier, non-key columns with observed min/max | `profiled` | Observed min/max ± a fixed 10% headroom |
| `freshness` | Not yet auto-proposed by `propose_tests.py` -- see note below | -- | -- |

**Freshness** isn't implemented in the current `propose_tests.py` because a defensible threshold
needs the *cadence* of updates (e.g. median gap between distinct dates in an event-date column),
not just a single max-date snapshot, and the fixture lakehouse's synthetic dates don't have a
realistic cadence to derive one from. If a freshness test matters for a given contract, note it in
`assumptions[]` as a gap rather than inventing a staleness threshold (e.g. "expect data within N
days") with no profiled basis -- that would violate the "derived from profiling, not invented"
rule as surely as skipping it.

**Findings vs. tests**: `propose_tests.py` also returns `findings[]` -- plain-language, evidence-
backed observations that a proposed test alone wouldn't communicate (a candidate key that's
*currently* violated, an FK with actual orphans right now, a null rate that looks like a defect,
a TEXT column whose values are actually numeric). Every `findings[]` entry belongs in the final
contract's `assumptions[]` -- these are exactly the "never silently infer" cases
`references/toolkit-conventions.md` #6 is about, even though detecting them required no inference
at all, just measurement plus a plain-language label.

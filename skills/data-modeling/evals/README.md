# data-modeling evals

Two layers, matching how much of the skill's behavior is deterministic vs. requires reasoning.

## 1. Deterministic assertions (`run_assertions.py`) -- no subagent, no LLM, CI-runnable

```
python fixtures/generate_fixtures.py
python skills/data-modeling/evals/run_assertions.py
```

Checks: read-only boundary (static scan, no DDL/DML in this skill's scripts); malformed/
unsupported-major artifacts rejected; and, most importantly, the silver-verification gate against
the real fixture lakehouse -- `bronze.raw_orders` is correctly REFUSED (no declared PK,
`_rescued_data`/`_ingest_*` columns, PascalCase naming) while `silver.orders` and
`silver.customers` are correctly VERIFIED *despite* carrying three of the fixture's five planted
data-quality flaws (the broken FK, the duplicated natural key, the nullable-that-shouldn't-be
column) -- this is the single most load-bearing assertion in the suite, because it's the proof
that "curated" and "clean" are genuinely different properties in this skill's design, not just
prose in `references/silver-verification.md`. Also covers: grain validation correctly passing on
the real `(order_id, line_number)` grain and correctly failing on a wrong one; SCD candidate
detection finding the real `customer_region_history` fixture for `silver.customers` and correctly
finding nothing for `silver.orders`; conformance candidate discovery not false-positive-matching a
non-dimension-shaped gold table; and a full orchestrator smoke test on both the refusal path and
the happy path. 20 checks, all passing as of this build.

## 2. Scenario evals (`evals.json` / `eval_metadata.json`) -- require a subagent with the skill loaded

Four scenarios: a full greenfield design exercising grain validation, measure additivity
classification, and SCD rationale grounded in real evidence (eval 1); the refusal behavior on an
uncurated bronze source, including that it explains itself in curation-not-quality terms and
offers rather than silently takes the override path (eval 2); conformed-dimension judgment --
confirming grain/attribute compatibility rather than treating a name match as sufficient (eval 3);
and declining to perform data-discovery's resolution-mode job (eval 4).

**Phase 2 sign-off evidence**: eval 1 (the fullest scenario -- silver verification, grain
validation, measure classification, and SCD rationale all in one run) was run as a full subagent
invocation and graded against every assertion in `eval_metadata.json`, independently re-verified
with `scripts/validate_artifact.py` and by reading the actual `model-spec.json` and
`design_rationale.md` fields rather than trusting the subagent's own claim of success. Evals 2 and
3's *mechanisms* (the refusal firing correctly, the conformance candidate check not
false-positive-matching) are already covered by the deterministic suite in `run_assertions.py`;
eval 4 relies on the skill description and `## When NOT to use this skill` section, consistent
with how the prior three skills' Phase 1/2 sign-offs handled their own equivalent cases. Re-run 2,
3, and 4 as full subagent scenarios before this touches a real engagement.

To re-run a scenario eval yourself: spawn an agent with access to this repo, point it at
`skills/data-modeling/SKILL.md`, and give it the `prompt` field from the matching entry in
`evals.json`. Grade the output against that eval's `assertions` in `eval_metadata.json`.

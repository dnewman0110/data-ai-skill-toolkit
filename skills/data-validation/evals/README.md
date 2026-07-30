# data-validation evals

Two layers, matching how much of the skill's behavior is deterministic vs. requires reasoning.

## 1. Deterministic assertions (`run_assertions.py`) -- no subagent, no LLM, CI-runnable

```
python fixtures/generate_fixtures.py
python skills/data-validation/evals/run_assertions.py
```

Checks: read-only boundary (static scan, no DDL/DML in this skill's scripts); malformed/
unsupported-major artifacts rejected; and a live run of `compare_staged.py` against the fixture
lakehouse confirms the staged engine actually works end-to-end -- finds the real discrepancy
(silver.orders vs gold.legacy_fct_orders: exactly one `missing_from_target` at the orphaned-
customer key, zero false positives on the TEXT-vs-REAL `total_amt` column), correctly stops early
at `hash_aggregate` on a clean self-comparison, and correctly excludes a declared
known-acceptable difference. This is what `.github/workflows/validate.yml` runs on every PR.

## 2. Scenario evals (`evals.json` / `eval_metadata.json`) -- require a subagent with the skill loaded

The four scenarios exercise judgment the deterministic layer can't: root-cause diagnosis with a
defensible confidence score (eval 1), confirming the "stop early when clean" behavior end-to-end
through the skill's own workflow rather than just the underlying script (eval 2), declaring and
applying a known-acceptable-difference exclusion through the skill's own workflow (eval 3), and
declining to perform a different skill's job when there's no target to compare against (eval 4).

**Phase 2 sign-off evidence**: eval 1 (the real-discrepancy diagnosis case, the one that most
exercises this skill's actual job) was run as a full subagent invocation and graded against every
assertion in `eval_metadata.json`, independently re-verified with `scripts/validate_artifact.py`
rather than trusting the subagent's own claim of success. Evals 2 and 3's *mechanisms* (early
stopping, known-acceptable-difference exclusion) are already covered by the deterministic smoke
test in `run_assertions.py`; eval 4 relies on the skill description and `## When NOT to use this
skill` section, consistent with how data-discovery's Phase 1 sign-off handled its own equivalent
case. Re-run 2, 3, and 4 as full subagent scenarios before this touches a real engagement.

To re-run a scenario eval yourself: spawn an agent with access to this repo, point it at
`skills/data-validation/SKILL.md`, and give it the `prompt` field from the matching entry in
`evals.json`. Grade the output against that eval's `assertions` in `eval_metadata.json`.

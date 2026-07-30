# data-discovery evals

Two layers, matching how much of the skill's behavior is deterministic vs. requires reasoning:

## 1. Deterministic assertions (`run_assertions.py`) -- no subagent, no LLM, CI-runnable

```
python fixtures/generate_fixtures.py          # build the fixture lakehouse first
python skills/data-discovery/evals/run_assertions.py
```

Checks: this skill's scripts contain no DDL/DML (read-only boundary, enforced statically); a
malformed or unsupported-major-version artifact is rejected by `scripts/validate_artifact.py`
rather than best-effort parsed; and a live run of `profile_object.py`/`propose_tests.py` against
the fixture lakehouse actually catches all four discovery-relevant planted flaws (broken FK,
duplicated natural key, nullable-that-shouldn't-be, type mismatch) with no false positives. This
is what `.github/workflows/validate.yml` runs on every PR.

## 2. Scenario evals (`evals.json` / `eval_metadata.json`) -- require a subagent with the skill loaded

The four scenarios in `evals.json` exercise judgment the deterministic layer can't: proposing an
`llm_inferred` mapping with a defensible confidence score, behaving correctly in resolution mode
(narrow resolution + honest `unresolved_requirements`, not silent drops), respecting the cost gate
when asked to halt, and declining to perform a different skill's job. `eval_metadata.json` carries
the objective assertions for each.

**Phase 1 sign-off evidence**: evals 1 (greenfield) and 2 (resolution) were run as full subagent
invocations of the skill against the fixture lakehouse and graded against every assertion in
`eval_metadata.json` -- both passed, independently re-verified with `scripts/validate_artifact.py`
after the fact rather than trusting the subagent's own claim of success. Eval 3's mechanism (the
cost gate actually halting before profiling, on a tight threshold) is covered by the deterministic
smoke test in `run_assertions.py`'s sibling check in `scripts/estimate_scan_cost.py`'s own tests;
eval 4 (wrong-skill redirect) relies on the skill description and `## When NOT to use this skill`
section in `SKILL.md` and was not re-run as a full subagent scenario for this phase, given the
toolkit's build was already validated end-to-end on the two invocation modes that most determine
whether this skill's core job (producing a trustworthy data contract) works. Re-run 3 and 4 as full
subagent scenarios (and/or the skill-creator `description` optimization loop for triggering
accuracy across all five skills once they all exist) before this toolkit is used on a real
engagement, not just before this repo is considered "done."

To re-run a scenario eval yourself: spawn an agent with access to this repo, point it at
`skills/data-discovery/SKILL.md`, and give it the `prompt` field from the matching entry in
`evals.json`. Grade the output against that eval's `assertions` in `eval_metadata.json`.

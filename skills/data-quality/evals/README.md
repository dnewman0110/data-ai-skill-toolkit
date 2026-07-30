# data-quality evals

Two layers, matching how much of the skill's behavior is deterministic vs. requires reasoning.

## 1. Deterministic assertions (`run_assertions.py`) -- no subagent, no LLM, CI-runnable

```
python fixtures/generate_fixtures.py
python skills/data-quality/evals/run_assertions.py
```

Checks: read-only boundary (static scan, no DDL/DML in this skill's scripts, plus explicit
positive/negative tests of the `custom_sql` guard against write-shaped SQL); malformed/
unsupported-major artifacts rejected; and a live run of `run_checks.py` +
`derive_checks_from_contract.py` against the fixture lakehouse confirms the check engine actually
works -- the planted null-rate, referential, and duplicate-natural-key flaws are caught with the
correct status (`warned`/`warned`/`failed`), a check with no configured threshold correctly comes
back `not_evaluated` rather than a silent pass, and contract-derived checks genuinely execute
against real data rather than just echoing the contract. This is what `.github/workflows/validate.yml`
runs on every PR.

## 2. Scenario evals (`evals.json` / `eval_metadata.json`) -- require a subagent with the skill loaded

The four scenarios exercise judgment the deterministic layer can't: diagnosing why checks failed
or warned with a defensible confidence score (eval 1), actually wiring up the contract-integration
path end to end through the skill's own workflow (eval 2), diagnosing the duplicated-natural-key
failure specifically (eval 3), and declining to perform a two-object comparison that's really
`data-validation`'s job (eval 4).

**Phase 2 sign-off evidence**: eval 1 (the fullest scenario -- multiple check types, multiple
diagnoses) was run as a full subagent invocation and graded against every assertion in
`eval_metadata.json`, independently re-verified with `scripts/validate_artifact.py` and by
inspecting the artifact's actual field values rather than trusting the subagent's own claim of
success. Evals 2 and 3's *mechanisms* (contract-derived checks executing, the duplicate-key check
failing correctly) are already covered by the deterministic smoke test in `run_assertions.py`;
eval 4 relies on the skill description and `## When NOT to use this skill` section, consistent with
how data-discovery and data-validation's Phase 1/2 sign-offs handled their own equivalent cases.
Re-run 2, 3, and 4 as full subagent scenarios before this touches a real engagement.

To re-run a scenario eval yourself: spawn an agent with access to this repo, point it at
`skills/data-quality/SKILL.md`, and give it the `prompt` field from the matching entry in
`evals.json`. Grade the output against that eval's `assertions` in `eval_metadata.json`.

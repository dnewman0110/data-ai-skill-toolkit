# Contract-derived checks

The explicit integration point between `data-discovery` and `data-quality`: a `data-contract.json`
already has `tables[].tests[]` with a defensible `threshold_basis` (per
`contracts/confidence-rubric.md`) -- `data-quality` should run those, not require someone to
hand-retype the same checks into a separate config.

## Type mapping

`skills/data-quality/scripts/derive_checks_from_contract.py` converts every contract test into a
quality check. Names differ slightly on purpose -- a contract *test* describes a constraint that
should hold; a quality *check* describes a scan that runs and reports a status:

| Contract test type | Quality check type | Params carried over as-is |
|---|---|---|
| `nullability` | `null_rate` | `max_null_rate` |
| `uniqueness` | `uniqueness` | `columns` |
| `referential` | `referential` | `ref_object`, `ref_column` |
| `range` | `value_range` | `min`, `max` |
| `freshness` | `freshness` | whatever the contract test specified |

`severity` is copied directly from the contract test -- `data-quality` does not re-decide how much
a check matters; whoever authored the contract (typically `data-discovery`, reviewed by a human
per that skill's own review gate) already did.

Every derived check carries `derived_from_contract_test: {contract_id, table, test_index}`,
pointing back at exactly which contract and which test produced it -- so a quality-report reader
can trace a failing check back to the specific contract clause it's enforcing, and so
`scripts/diff_artifact.py`'s quality-report differ can tell a genuinely new hand-authored check
from one that's always been running since the contract was approved.

## Merging with hand-authored checks

`build_quality_findings.py`'s `merge_checks` indexes both lists by `check_id` and lets
hand-authored checks win on collision. This means a project can override a contract-derived
check's threshold (e.g. loosen a `max_null_rate` the business has decided is actually fine) by
authoring a check with the SAME `check_id` the derivation would produce
(`<table>.<column>.<quality_type>.from_contract`) -- an explicit, visible override, not a silent
divergence between what the contract says and what actually runs. If you want to ADD a check
alongside a derived one rather than override it, give it a different `check_id`.

## What this does NOT do

Deriving checks does not re-validate the contract itself, does not re-profile the source object
the contract was built from, and does not update the contract if the check's result suggests the
contract's threshold was wrong. If a derived check keeps failing in a way that looks like the
contract's threshold (not the data) is the problem, that's a signal to go back to
`data-discovery` and regenerate the contract with fresher profiling -- not something `data-quality`
resolves on its own.

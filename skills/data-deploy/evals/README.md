# data-deploy evals

Two layers, matching how much of the skill's behavior is deterministic vs. requires reasoning.

## 1. Deterministic assertions (`run_assertions.py`) -- no subagent, no LLM, CI-runnable

```
python skills/data-deploy/evals/run_assertions.py
```

No `fixtures/generate_fixtures.py` dependency -- unlike every other skill, `data-deploy` never
touches a lakehouse adapter at all; it's pure manifest transformation and template rendering, so
its fixtures are hand-authored `pipeline-manifest.json`s and `transform_spec.json`s under
`evals/fixtures/`, not derived from the shared SQLite fixture lakehouse.

Checks: this skill never invokes a process, network call, or Databricks API (a static scan --
stronger than the other skills' "no DDL/DML" scan, since data-deploy has no reason to import
`subprocess`/`requests`/a Databricks SDK client at all); malformed/unsupported-major
`deployment-manifest.json`s rejected; the approval gate refuses on a non-`lakeflow_connect`
modality, a manifest not yet `approved_for_deployment`, and a `deployment.target_named` that
doesn't match any real target; a multi-target manifest processes only the named target and
records every other one `skipped` with a reason; an unsupported `source_system` is reported in
`unsupported_source_systems` rather than guessed at a fallback shape; and a live end-to-end run
for **two different connector types** (Salesforce, SQL Server) actually renders real bundle
resources that parse as valid YAML with the correct `connection_type`, source object, destination,
and `primary_keys` -- proving "generic across source systems," not just asserting it. This is
what `.github/workflows/validate.yml` runs on every PR.

## 2. Scenario evals (`evals.json` / `eval_metadata.json`) -- require a subagent with the skill loaded

The six scenarios exercise judgment the deterministic layer can't: generating real bundle
resources for two different source systems from the same conversation flow (evals 1-2), refusing
an unapproved manifest outright (eval 3), correctly narrating a partially-approved multi-target
manifest rather than silently dropping the unnamed target (eval 4), holding the line on a vague
"go ahead and deploy" the same way `data-pipeline`'s own equivalent eval does (eval 5), and naming
an unsupported source system rather than inventing a connector shape for it (eval 6).

These have not yet been run as full subagent invocations (this skill has not shipped an
engagement yet) -- the deterministic layer above already exercises every *mechanism* these
scenarios describe (connector resolution, the approval gate, multi-target skip logic, unsupported-
system refusal), so what a subagent run would add is confirming the skill's own judgment calls
(naming `source_system`, choosing `connection_name`, narrating the result) rather than the
underlying logic. Run all six as full subagent scenarios before this skill touches a real
engagement, same discipline `data-quality`'s own evals/README.md documents for its own
not-yet-rerun scenarios.

To run a scenario eval yourself: spawn an agent with access to this repo, point it at
`skills/data-deploy/SKILL.md`, and give it the `prompt` field from the matching entry in
`evals.json`. Grade the output against that eval's `assertions` in `eval_metadata.json`.

# Approval gate: where it sits, and why

`data-deploy` sits closer to a live client workspace than any other skill in this toolkit --
its whole job is producing the Asset Bundle resources and connector configuration a human takes
straight to `databricks bundle deploy`. This doc states plainly where the human-approval boundary
sits and why, per the toolkit's own request that every skill draw this line explicitly rather than
leave it implicit (`toolkit-conventions.md` #1, #7).

## Two separate approvals, not one

**Gate A -- already recorded, this skill's own precondition to run at all.** `data-pipeline`
generated this pipeline's code and, per its own `SKILL.md` step 7, only advances
`readiness_level` to `approved_for_deployment` and records a non-null `deployment` (`approved`,
`approved_by`, `target_named`) after a human explicitly approved, in-conversation, naming the
specific target. `data-deploy` requires this to already be true (`scripts/check_target_approval.py`)
and refuses to generate anything otherwise -- see the "Input preconditions" table in `SKILL.md`.
This is not a new approval `data-deploy` invents; it's the existing licence data-pipeline already
required before its own code was considered ready to move toward deployment, now extended to also
license generating the deployment-adjacent artifacts (bundle YAML, connector config) for that same
named target.

**Gate B -- this skill's own output, a SEPARATE approval.** Gate A licensed generating bundle
resources. It does **not** license actually running `databricks bundle deploy` or creating a live
Unity Catalog connection -- that is a materially bigger and different action (it creates real
objects in a client's workspace and can start real data ingestion), and per
`toolkit-conventions.md` #7 gate 3, requires its own explicit, in-conversation approval naming the
specific target. `deployment-manifest.schema.json`'s `deployment` field records this second
approval, structurally identical to `pipeline-manifest.schema.json`'s own `deployment` field, and
is `null` until a human gives it -- `build_deployment_manifest.py` never sets it, the same way
`build_pipeline_manifest.py` never sets `pipeline-manifest.json`'s `deployment` field.

## Match or depart? This skill matches the existing "never deploy" boundary

Every other skill in this toolkit generates or measures; none of them execute a write against a
client system, ever, under any approval. `data-deploy` had a real choice to make here: generate
bundle resources only (matching that boundary), or go further and actually invoke
`databricks bundle deploy` / create the connector once Gate B is satisfied (departing from it).

**This skill matches the existing boundary. It never runs `databricks bundle deploy`, never calls
any Databricks API, and never creates a live connector, no matter what Gate B says.** Reasons:

- **Consistency with the toolkit's core identity.** Every `SKILL.md` in this toolkit states its
  read/write boundary near the top in plain language, and every one of them draws the same line:
  writes to `output_dir` are fine, writes to a client system are not this toolkit's job. This is a
  public-ish, cross-engagement consultancy tool (`toolkit-conventions.md` #3) -- the moment one
  skill in it can execute a live deploy, every engagement using this toolkit inherits that
  capability, not just the ones that want it.
- **Deploy execution is not safely generalizable the way YAML generation is.** Generating correct,
  reviewable bundle resources from a manifest is exactly the kind of deterministic, engagement-
  independent task this toolkit is built to do everywhere else. Actually running the deploy
  depends on auth context, workspace reachability, rollback policy, and CI/CD conventions that
  differ per engagement -- baking one opinionated execution path into a shared toolkit script would
  either be unsafe in the general case or would have to grow enough configuration surface to defeat
  the purpose of a shared script.
- **The artifact this skill produces is already the useful, complete thing.** `data-pipeline`
  proves this pattern works: it generates real, deployable PySpark/DLT code and a human (or a
  separately authorized CI/CD process) takes it from there. `data-deploy` generates real,
  deployable bundle YAML and connector configuration; the same handoff applies.

If a future engagement genuinely needs actual deploy execution, that is a new, explicitly-scoped
capability outside this toolkit's boundary -- not a flag on this skill.

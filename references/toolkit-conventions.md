# Toolkit Conventions

This file is the single source of truth for rules that apply across all five skills in this toolkit
(`data-discovery`, `data-modeling`, `data-pipeline`, `data-quality`, `data-validation`). Every skill's
SKILL.md links here instead of restating these rules, so they're maintained in one place. If you're
reading this because a SKILL.md pointed you here: everything below is binding on the skill you're
running, not optional background reading.

Environment this toolkit targets by default: Databricks on Azure, Unity Catalog, latest LTS DBR,
serverless-preferred compute (falls back to classic), Databricks Jobs for orchestration, native
PySpark/SQL (no dbt), Databricks Connect for the `databricks_connect` backend -- reusing whatever
session is already authenticated in the host environment, rather than the toolkit managing its own
auth/secrets for that backend (see #2 below). A project's `toolkit.yaml` can override any of this
per engagement -- see `toolkit.example.yaml`.

## 1. Read/write boundaries

- `data-discovery`, `data-modeling`, `data-quality`, `data-validation` are **read-only against client
  systems**. No DDL, no DML, no writes outside the configured `output_dir`. Ever. This holds even when
  a check "would be easier" with a temp view or a scratch table -- see the scratch-schema rule below for
  the one sanctioned exception.
- `data-pipeline` **generates code and writes artifacts to the workspace filesystem**. It does not
  deploy, does not create or schedule jobs, does not run generated code against production, and does not
  create or alter catalog objects -- without an explicit, in-conversation human approval that names the
  specific target object or job.
- Any temp objects a skill needs for computation (e.g. a staging view to compute a hash aggregate) go in
  a **configurable scratch schema** (`scratch_schema` in `toolkit.yaml`), are named with the current
  `run_id`, and are dropped at the end of the run whether it succeeds or fails.

An agent holding warehouse credentials with an ambiguous mandate is exactly how an unplanned write lands
on a client's catalog. Every skill's SKILL.md states its read/write boundary explicitly, near the top,
in plain language -- not just by reference to this file.

## 2. Credentials and secrets

- Skills read connection config (`environment.backend`, `environment.catalog`, etc.) from a project-level
  `toolkit.yaml`. For the `databricks_connect` backend, that's the whole story: `DatabricksConnectAdapter`
  reuses whichever Databricks Connect session is already authenticated in the host environment (a
  `databricks-connect` profile, OAuth, env vars -- however the project's own Databricks Connect setup
  works) and neither this toolkit nor `toolkit.yaml` handles auth, tokens, or secret resolution for it. A
  project may still need a secret store (Databricks Secrets, Azure Key Vault, environment variables) for
  its *own* purposes outside this toolkit, but no adapter in `scripts/lakehouse_adapter.py` reads one
  directly. Credentials are never inlined in `toolkit.yaml`, in generated code, or anywhere else in the
  repo.
- Same rule, second worked example: `data-discovery`'s `SqlServerAdapter` (`external_sources.sqlserver`
  in `toolkit.yaml`, see `skills/data-discovery/references/sqlserver-profiling.md`) is ambient the same
  way -- `toolkit.yaml` names non-secret connection shape (host, port, database, driver, `auth_mode`) and,
  for the one auth mode that needs a secret at all (`sql_auth_env`), only the *names* of environment
  variables to read, never the values. The recommended `azure_ad_default` mode needs no credential
  concept whatsoever, reusing whatever's already logged into Azure, the same posture as Databricks
  Connect's OAuth session. Nothing secret is ever typed into a conversation, written to `toolkit.yaml`, or
  passed as a CLI argument value the agent itself constructs.
- Credentials, tokens, and connection strings never appear in: generated artifacts, logs, reports,
  prompts sent to an LLM, or anything written to the repo. If a script needs to log a connection attempt,
  it logs the secret *scope name*, never the resolved value.
- If required config is missing, the skill **halts with a specific message naming the missing key**
  (e.g. "toolkit.yaml is missing `environment.catalog`"). It does not prompt for a secret in chat, and it
  does not guess a plausible-looking default.

## 3. Client data isolation

This is a consultancy toolkit shared across engagements and distributed via a public-ish GitHub repo.
Client data must never land in it.

- All run outputs go to a configurable `output_dir` **outside the toolkit repo**, defaulting to
  `~/.toolkit-runs/<project>/<run_id>/`.
- The repo's `.gitignore` excludes output, scratch, and fixture-override directories by pattern, not by
  hoping nobody commits them.
- Skills that emit example or sample records (`data-discovery`, `data-quality`, `data-validation`) must:
  cap sample size (`max_sample_records` in `toolkit.yaml`, default 20), redact or hash any column tagged
  sensitive under `toolkit.yaml`'s `sensitive_columns`, and never place a sample value in a filename or a
  URL (filenames use `run_id` and object names only).
- Before any table contents are sent to an LLM for diagnosis, the same redaction applies. Prefer sending
  schema, types, and aggregates over raw rows wherever those are sufficient to ground the diagnosis --
  raw rows are the last resort, not the default.

## 4. Cost and blast radius

Discovery explores the lakehouse; validation does row-level diffs. On a large client estate, both can be
expensive, and both can page someone if run carelessly.

- Every skill **estimates scan cost before executing anything full-scale** (using `information_schema`,
  table statistics, or a cheap `COUNT(*)`/`DESCRIBE DETAIL` probe -- never a full read just to estimate a
  full read) and reports estimated bytes/rows scanned before proceeding.
- If an operation would exceed the configurable thresholds `max_rows_scanned`, `max_bytes_scanned`, or
  `max_wall_clock` (from `toolkit.yaml`, overridable per run), the skill **stops and asks before
  proceeding** rather than truncating silently or plowing ahead.
- Default to sampling and to partition/predicate pushdown. Full scans are opt-in, not the default
  behavior when a cheaper approach would answer the question.
- Actuals (`telemetry.actual_bytes_scanned`, `actual_rows_scanned`, `wall_clock_seconds`) are always
  recorded in the run manifest, even when the run stayed well under threshold, so teams build a real
  cost model over time instead of guessing every time.

## 5. Deterministic vs. LLM boundary

One rule, applied uniformly: **measurement and comparison are deterministic code; only interpretation
goes to the model.**

- Counts, hashes, diffs, profiling statistics, assertion pass/fail, and schema comparison are
  implemented in Python/SQL in `scripts/`. They are reproducible -- same inputs, same outputs, no model
  in the path. If a number in an artifact could change between two runs against unchanged data because
  the LLM was asked to "estimate" it, that number is in the wrong layer.
- The LLM's job is naming root causes, explaining, proposing mappings, and drafting prose (e.g. design
  rationale documents). Its output is always labeled (`"source": "llm_inferred"` in artifacts) and always
  carries a confidence score per `contracts/confidence-rubric.md`.
- Every artifact distinguishes measured fields (no `confidence` key present -- the value is a fact) from
  inferred fields (`confidence` + `basis` present) so a downstream reader, human or agent, knows what to
  trust without re-deriving it.
- In this toolkit, the "LLM" performing interpretation is the agent running the skill itself, reading
  the deterministic output that a script produced and reasoning over it per the skill's instructions --
  not a separate API call from within a script. Scripts stay dependency-light and don't need their own
  model credentials.

## 6. Grounding and failure behavior

Each skill declares, per situation, what it does when it can't ground itself: no table comments, no
constraints, ambiguous grain, missing lineage. There are exactly three sanctioned behaviors -- pick one
per situation and say which, in the skill's own SKILL.md:

- **Halt** -- when proceeding would produce a confidently wrong artifact that a downstream consumer
  might trust at face value (e.g. discovery cannot determine grain at all, or model-spec resolution finds
  no plausible source object for a required field). Halting beats shipping a guess dressed as a fact.
- **Emit low-confidence + flag** -- when a downstream human review gate will catch it before it does
  damage (e.g. an `llm_inferred` mapping with confidence 0.3, clearly labeled, feeding into a contract
  that a human reviews before pipeline generation).
- **Ask** -- when a single clarifying question unblocks everything and asking is cheap relative to
  guessing wrong (e.g. "greenfield discovery found two plausible source tables for 'customer' -- which
  one?").

Never silently infer. Every inference, regardless of which of the three behaviors it triggered, lands in
the artifact's `assumptions[]` array with its basis -- even a halted run should explain what it tried and
why it couldn't proceed.

## 7. Human review gates

These are the points where a skill stops and hands off to a human, marked explicitly in each relevant
SKILL.md:

1. **After a data contract is produced, before pipeline generation.** A contract with any `llm_inferred`
   mapping is a proposal, not a spec, until a human has looked at it.
2. **After a model spec is produced, before it goes to discovery (resolution mode).** Dimensional design
   decisions (grain, SCD type, conformance) are business decisions wearing a technical hat; discovery
   should resolve an approved design, not a draft one.
3. **Before any pipeline code is deployed or scheduled.** `data-pipeline` writes code to disk; a human (or
   a separate, explicitly authorized process) takes it from there.
4. **After a validation report identifies a discrepancy, before any remediation is applied.** Diagnoses
   and suggested fixes are suggestions. No skill in this toolkit applies a fix to a discrepancy it found.

## 8. Idempotency and re-runs

Engagements iterate and source schemas change mid-flight. A team should never have to eyeball two JSON
files to find out what changed.

- Re-running a skill against changed sources produces a **new versioned artifact plus a diff summary**
  against the previous run: added/removed/changed columns, tests, mappings (skill-specific -- see each
  SKILL.md for exactly what the diff covers).
- Artifacts are content-addressable enough to cheaply detect "nothing changed": `run.source_fingerprints`
  in the run manifest lets a skill short-circuit and report "no material change since run `<id>`" instead
  of regenerating an identical artifact from scratch.
- **Never silently overwrite a prior artifact.** New runs write new files (named or directoried by
  `run_id`); a `latest` pointer may be updated, but the prior artifact stays on disk in `output_dir`.

## 9. Structure and triggering

- Each SKILL.md is a thin router, under ~500 lines: what the skill does, its read/write boundary, its
  invocation modes, and pointers into `references/` for depth. Don't inline reference material that a
  reader only needs some of the time.
- `data-pipeline` in particular organizes references by modality
  (`references/pyspark-notebook.md`, `references/declarative-pipelines.md`, `references/lakeflow-connect.md`,
  `references/other-modalities.md`) so a run only loads the one relevant to the modality decision it made.
- Skill descriptions are mutually exclusive in triggering. Each SKILL.md frontmatter description states
  what the skill is, what it explicitly is **not**, and names the artifact it consumes and the artifact it
  produces. Each SKILL.md body includes a `## When NOT to use this skill` section pointing at its siblings
  in the chain (modeling -> discovery -> pipeline) and its attached validators (quality, validation).

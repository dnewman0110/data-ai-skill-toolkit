# Changelog

All notable changes to this toolkit are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versioning is semver on the toolkit as a whole (see
README.md "Versioning" for how this relates to per-skill and per-schema versions).

## [Unreleased]

## [1.4.0] - 2026-08-05

### Added
- **Multi-source join support in `data-pipeline`.** `build_transform_spec.py` now renders a real
  multi-table join (a genuine `.join()` chain, not a stub) when a `data-contract.json` declares
  `table.source_joins` -- a structured, equality-only join (driving object + one or more joined
  objects, each with an explicit `join_type` and column-equality `on` conditions; a join key
  needing a cast/derivation uses `left_expression`, same free-text-SQL precedent as
  `source.transformation`). This covers genuine many-to-one lookup joins -- denormalizing a
  snowflaked dimension (including a self-join, disambiguated by a required, unique `alias` per
  join), rolling a header table's attributes down to a fact's own grain, a dimension surrogate-key
  lookup -- and classifies as `transform_complexity: simple_declarative`, routing to
  `declarative_pipeline` like any other reshape (`references/decision-rubric.md`'s narrowed
  guardrail, with a new worked example distinguishing this from genuine multi-source complexity).
  `model-spec.schema.json` gained the equivalent `facts[].source_joins`/`dimensions[].source_joins`
  so `data-modeling` can design these joins directly, carried through `data-discovery`'s resolution
  mode into the data-contract unchanged. A multi-source target with no `source_joins` declared, or
  a join that can't be expressed as an equality condition, still refuses outright and routes to
  `pyspark_notebook` -- the guardrail is narrowed, not removed. Every JOINED (non-driving) object
  is read as a static batch snapshot even when the driving object streams (the standard,
  documented stream-static join pattern for a lookup join -- see
  `references/declarative-pipelines.md`). The local SQLite-based idempotency proof reports
  `not_applicable` for multi-source targets (a documented v1 scope decision, not a silent gap --
  `derive_mock_data.py` has no notion of multiple mock tables sharing real foreign-key
  relationships across aliases); the real generated Spark code is unaffected. `data-pipeline`
  bumped to `1.2.0`, `data-modeling` to `1.1.0`, `data-discovery` to `1.3.0` (resolution-mode
  carry-through instructions). See DECISIONS.md decision 60.

## [1.3.0] - 2026-08-05

### Added
- **`data-deploy`, a sixth skill.** Turns a `data-pipeline`-generated `pipeline-manifest.json`
  whose `modality_decision.chosen` is `lakeflow_connect` and `readiness_level` is
  `approved_for_deployment` into real Databricks Asset Bundle resources -- a Unity Catalog
  connection definition (`uc_connection.yml`) and an ingestion-pipeline/destination resource
  (`ingestion_pipeline.yml`) -- plus a resolved Lakeflow Connect connector type, generic across
  source systems (SQL Server, Salesforce, ServiceNow, Workday, SharePoint; extensible). Refuses to
  touch any target not named in the source manifest's `deployment.target_named`, and never runs
  `databricks bundle deploy`, calls any Databricks API, or creates a live connector itself -- a
  SEPARATE, explicit human approval (this skill's own `deployment` field, distinct from the source
  manifest's) is required before that, and even then this skill only records it. New seventh
  contract schema, `contracts/deployment-manifest.schema.json` (plus its example);
  `run-manifest.schema.json`'s `skill.name` enum gained `"data-deploy"`; `validate_artifact.py` and
  `diff_artifact.py` extended accordingly. See DECISIONS.md decision 59.

### Changed
- `references/toolkit-conventions.md`: read/write boundaries (#1) and human review gates (#7) now
  cover `data-deploy`'s two-approval-gate boundary; "six skills" throughout (README.md,
  CONTRIBUTING.md, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`) replacing every
  "five skills" reference.

## [1.2.0] - 2026-08-05

### Fixed
- `scripts/lakehouse_adapter.py` (`SqlServerAdapter`) and `skills/data-discovery/scripts/profile_object.py`:
  five bugs found running greenfield profiling against a live Azure SQL Database --
  `profile_column`/`check_uniqueness` missing a required derived-table alias (T-SQL "Incorrect
  syntax near ')'"); `MIN`/`MAX` on `bit` and `COUNT(DISTINCT)`/`MIN`/`MAX` on `xml`/`text`/
  `ntext`/`image`/`geography`/`geometry`/`hierarchyid` (error 8117), now conditionally omitted
  based on the column's declared type (`sql_variant`/`rowversion`/`timestamp` added to the same
  exclusion sets speculatively); `count_orphans`'s `SUM(CASE WHEN ... EXISTS(subquery) ...)` shape
  rejected outright (error 130), rewritten via an inner derived table; every candidate-key/FK
  check in `profile_table` now runs in its own `try`/`except` so one failing check degrades to a
  flagged finding instead of aborting the whole profiling run; `numeric_types` was missing
  `money`/`smallmoney`/`bit`, causing those columns to trip a factually wrong TEXT-vs-numeric
  mismatch finding. `data-discovery` bumped to `1.2.0`. See DECISIONS.md decision 58.

## [1.1.0] - 2026-08-05

`1.0.0` was the initial plugin version and never got its own section here, so this section covers
everything from the initial build through 1.1.0. Skills whose behavior actually changed since the
initial build are bumped to `1.1.0` in their `SKILL.md` frontmatter (`data-discovery`,
`data-pipeline`); the other three stay at `1.0.0` (doc-only touches).

### Added
- Phase 0: `contracts/` -- five versioned JSON Schemas (`run-manifest`, `data-contract`, `model-spec`,
  `quality-report`, `validation-report`), one valid example instance per schema under
  `contracts/examples/`, `contracts/confidence-rubric.md`, and `scripts/validate_artifact.py`.
- Phase 0: repo scaffolding -- `README.md`, `CONTRIBUTING.md`, this file, `toolkit.example.yaml`,
  `references/toolkit-conventions.md`, `.gitignore`, empty `skills/*` and `fixtures/` skeletons,
  `.github/workflows/` CI skeleton.
- Phase 1: shared infrastructure used by every skill going forward -- `scripts/lakehouse_adapter.py`
  (backend-agnostic `SQLiteFixtureAdapter` for evals/CI, `DatabricksAdapter` for production),
  `scripts/estimate_scan_cost.py` (the cost/blast-radius gate), `scripts/redact.py` (sample redaction),
  `scripts/diff_artifact.py` (idempotency/re-run diffing, `data-contract` differ implemented), and
  `fixtures/generate_fixtures.py` -- a synthetic SQLite-based lakehouse (bronze/silver/gold) with five
  deliberate flaws (broken FK, duplicated natural key, nullable-that-shouldn't-be, source/target type
  mismatch, slowly-changing attribute).
- Phase 1: `skills/data-discovery/` built completely -- `SKILL.md`, three reference docs
  (`invocation-modes.md`, `grain-and-tests.md`, `profiling-and-cost-bounds.md`), deterministic scripts
  (`profile_object.py`, `propose_tests.py`, `build_findings.py`), and evals (`evals.json`,
  `eval_metadata.json`, `run_assertions.py` for CI, `README.md`). Deterministic assertions and two full
  subagent scenario runs (greenfield and resolution invocation modes) pass, independently re-verified
  against `scripts/validate_artifact.py`.
- Phase 2: `skills/data-validation/` built completely -- `SKILL.md`, three reference docs
  (`staged-comparison.md`, `normalization-and-type-coercion.md`, `known-acceptable-differences.md`),
  deterministic scripts (`normalize.py`, `compare_staged.py`, `build_validation_findings.py`), and
  evals. Deterministic assertions and one full subagent scenario run (real source/target discrepancy
  with root-cause diagnosis) pass, independently re-verified.
- Phase 2: extended shared infrastructure for data-validation -- `scripts/lakehouse_adapter.py` gained
  `fetch_rows()` (ordered/bounded full-column fetch, both adapters) and a NULL-handling fix to
  `check_uniqueness()` (composite-key checks no longer collapse multiple NULLs into a false
  "duplicate" -- matches standard SQL UNIQUE-constraint semantics); `scripts/diff_artifact.py` gained a
  `validation-report` differ (diffs by `(kind, key)` since discrepancies aren't named the way
  columns/tests are) and `is_material()` now recognizes scalar `{old, new}` change pairs, not just
  non-empty lists.
- Phase 2: `skills/data-quality/` built completely -- `SKILL.md`, two reference docs
  (`check-types-and-thresholds.md`, `contract-derived-checks.md`), deterministic scripts
  (`run_checks.py` covering all seven check types, `derive_checks_from_contract.py` -- the
  explicit data-discovery integration point, `build_quality_findings.py`), and evals. Deterministic
  assertions (16 checks, including positive/negative tests of the `custom_sql` write-guard) and one
  full subagent scenario run (multi-check scan with root-cause diagnosis) pass, independently
  re-verified.
- Phase 2: further shared/adjacent infrastructure -- `scripts/lakehouse_adapter.py` gained
  `execute_scalar()` (both adapters) plus a module-level `assert_read_only_select()` guard used by
  `custom_sql` checks; `scripts/diff_artifact.py` gained a `quality-report` differ (diffs by stable
  `check_id`, unlike validation's row-keyed discrepancies).
- Phase 2: a sixth contract schema, `contracts/pipeline-manifest.schema.json` (plus its example),
  added because `data-pipeline`'s output -- generated code, a modality decision, mock-data-based
  idempotency evidence, a human-approval readiness level -- doesn't fit any of the four
  lineage/comparison artifact shapes the other skills produce, and the bare `run-manifest` envelope
  alone can't carry that bookkeeping without redefining a schema every other skill also depends on.
  `scripts/validate_artifact.py` and `scripts/diff_artifact.py` (new `diff_pipeline_manifest`) both
  extended accordingly. See DECISIONS.md.
- Phase 2: `skills/data-pipeline/` built completely -- `SKILL.md`, six reference docs
  (`decision-rubric.md`, `pyspark-notebook.md`, `declarative-pipelines.md`, `lakeflow-connect.md`,
  `other-modalities.md`, `idempotency-and-mock-data.md`), templates for all three modalities,
  deterministic scripts (`recommend_modality.py`, `build_transform_spec.py`, `derive_mock_data.py`,
  `validate_pipeline_locally.py`, `generate_pipeline_code.py`, `build_pipeline_manifest.py`), and
  evals. This is the only skill that writes files a human might deploy -- it never deploys,
  schedules, or executes anything itself. Deterministic assertions (33 checks, including a real
  Python `compile()` check on every generated file and a from-scratch local idempotency proof
  against synthetic mock data) and one full subagent scenario run (modality classification, code
  generation, and the deployment-approval-gate language) pass, independently re-verified.
- Phase 2: `skills/data-modeling/` built completely -- the fifth and final skill. `SKILL.md`, four
  reference docs (`silver-verification.md`, `kimball-concepts.md`, `scd-type-selection.md`,
  `conformed-dimensions.md`), deterministic scripts (`verify_silver_layer.py` -- the gold-layer-only
  refusal gate, `validate_grain_against_measures.py`, `detect_scd_candidates.py`,
  `derive_conformance_candidates.py`, `build_model_findings.py`), and evals. Deterministic
  assertions (20 checks, most notably confirming `silver.orders`/`silver.customers` are correctly
  VERIFIED despite carrying three of the fixture's five planted data-quality flaws while
  `bronze.raw_orders` is correctly REFUSED -- proof the curation-vs-quality distinction this skill
  is built on actually holds) and one full subagent scenario run (greenfield star schema design
  with grounded SCD-2 reasoning) pass, independently re-verified. This closes out Phase 2 -- all
  five skills are now built.
- Phase 3: `fixtures/integration_check.py` -- a fully deterministic, no-LLM, CI-runnable proof
  that all five skills' artifacts actually chain together against the real fixture lakehouse:
  resolves `contracts/examples/model-spec.example.json` into a real `data-contract.json`
  (data-discovery, resolution mode), builds real pipeline code and proves local idempotency from
  it (data-pipeline), then attaches `data-quality` and `data-validation` at their gates -- five
  schema-valid artifacts from one continuous run. Wired into CI as a new `integration-run` job.
  Documented in `fixtures/README.md`.
- Phase 3 addendum: a genuine judgment-inclusive integration sign-off run (a one-time subagent
  exercise through all five skills against a freshly-designed model, not a shipped example --
  requested explicitly, not part of the permanent CI check) surfaced three real bugs in
  `data-pipeline`'s shared generator code, all fixed and regression-tested:
  `skills/data-pipeline/scripts/generate_pipeline_code.py`'s expectation-dict keys are now unique
  per uniqueness test (`valid_grain_<columns>` instead of a fixed `"valid_grain"` that silently
  collided and dropped all but the last test on a table with multiple candidate keys); a new
  template `skills/data-pipeline/templates/declarative_pipeline_full_refresh.py.tmpl` (a plain
  `@dlt.table` materialized view) is used instead of `apply_changes(keys=[])` -- invalid, since
  `apply_changes` requires at least one key column -- whenever a `declarative_pipeline` target has
  no merge keys; and `skills/data-pipeline/scripts/build_pipeline_manifest.py`'s mock-data
  filenames and `row_counts_by_table` keys are now qualified by TARGET table, not just source
  table, so two targets sharing one source object no longer silently overwrite each other's mock
  data. Three new regression checks added to `skills/data-pipeline/evals/run_assertions.py`. See
  DECISIONS.md decisions 45-48.
- `.claude-plugin/plugin.json` at the repo root -- this toolkit is now a real Claude Code plugin,
  installable via CLI into any project (`.claude/skills/data-ai-skill-toolkit/` for a single-repo
  install, or a future marketplace listing for reuse across many client repos). Documented in
  `README.md`'s new "Installing as a plugin" section. See DECISIONS.md decision 51.
- `.claude-plugin/marketplace.json` -- a minimal self-hosting marketplace (`source: "./"`) so this
  repo distributes its own plugin via `/plugin marketplace add` + `/plugin install`, since the
  `.claude/skills/`-vendoring auto-discovery documented in decision 51 turned out not to work in
  practice. `README.md`'s "Installing as a plugin" section rewritten accordingly. See DECISIONS.md
  decision 52.
- `scripts/lakehouse_adapter.py` gained `build_adapter(backend, ...)`, a single factory a build
  script calls with an already-resolved `backend` string (from toolkit.yaml's new
  `environment.backend`) to get either adapter, instead of every skill's build script hardcoding
  `SQLiteFixtureAdapter` directly. See DECISIONS.md decision 53.
- `data-discovery` can now profile a SQL Server source in greenfield mode (`--backend sqlserver`)
  BEFORE it's ingested into the lakehouse at all, so real data-quality problems surface before
  anyone spends engineering time on the ingestion design. New `SqlServerAdapter` in
  `scripts/lakehouse_adapter.py` (T-SQL/`INFORMATION_SCHEMA`/`sys.*`-based, `pyodbc`); new
  `external_sources.sqlserver` block in `toolkit.yaml` (ambient credentials only -- names of
  environment variables or Azure AD, never a secret value, matching how `databricks_connect`
  already works); new `skills/data-discovery/references/sqlserver-profiling.md`. Zero changes
  needed in `data-pipeline` -- a resulting bronze-landing contract already routes to
  `lakeflow_connect` under the existing modality rubric, verified end-to-end. See DECISIONS.md
  decision 57.

### Changed
- `scripts/lakehouse_adapter.py`: replaced `DatabricksAdapter` (databricks-sql-connector, explicit
  `server_hostname`/`http_path`/`access_token`, never actually instantiated anywhere in this repo)
  with `DatabricksConnectAdapter` (Databricks Connect, reuses whatever Spark session is already
  authenticated in the host environment -- no auth/token logic in the toolkit at all for this
  backend). `toolkit.example.yaml`'s `auth:` block (service-principal/secret-scope config) removed
  along with it; replaced by `environment.backend` and `environment.catalog`.
  `skills/data-discovery/scripts/build_findings.py` gained a `--backend` flag wired to the new
  `build_adapter()` factory (other four skills' build scripts still hardcode `SQLiteFixtureAdapter`
  -- same treatment is a follow-up, not done in this change). See DECISIONS.md decision 53.
- `scripts/lakehouse_adapter.py`: fixed two bugs in `DatabricksConnectAdapter` found by testing
  against a real workspace (`samples.bakehouse.sales_customers`) -- `:name` SQL parameters must be
  passed via `spark.sql()`'s `args=` dict, not `**kwargs` (which does `{name}`-style substitution
  instead); and every `information_schema` reference now qualifies the catalog explicitly
  (`{catalog}.information_schema....`), since Unity Catalog's information_schema is per-catalog and
  an unqualified reference silently resolves against `current_catalog()` instead of erroring. The
  second bug was latent in the original `DatabricksAdapter` (decision 51) too. See DECISIONS.md
  decision 54.
- `scripts/redact.py`: `_column_action` now normalizes camelCase/PascalCase/kebab-case column names
  to snake_case before matching `sensitive_columns` patterns. Patterns are written snake_case
  (`card_number`) but source columns aren't always named that way -- `cardNumber` previously slipped
  through unredacted. Caught live: a real profiling run against a camelCase-columned source left an
  unredacted card-number sample in the working findings file before the final contract was written
  (the final artifact was clean; the gap was in the regex, not that run's output). See DECISIONS.md
  decision 53.
- All five `SKILL.md` files: every executable path reference (`python scripts/validate_artifact.py`,
  `skills/<name>/scripts/*.py`, etc.) now uses `${CLAUDE_PLUGIN_ROOT}` (toolkit-root shared
  `scripts/`/`contracts/`) or `${CLAUDE_SKILL_DIR}` (each skill's own bundled scripts) instead of
  bare relative paths, so commands resolve correctly regardless of the working directory the agent
  is running in -- previously every `SKILL.md` assumed the cwd was the toolkit repo root, which
  breaks once the toolkit is installed inside a client project repo rather than run standalone.
  Doc-only change: full eval suites and `fixtures/integration_check.py` re-run clean afterward. See
  DECISIONS.md decision 51.
- `contracts/validation-report.schema.json`: `discrepancies[]` items now require `kind` (enum:
  `missing_from_target` / `extra_in_target` / `changed`) and `key` (the row-identifying column
  values), and `sample_diff_ref` is now nullable. Without `kind`/`key`, a report couldn't identify
  *which row* a discrepancy was about without opening a separate pointer file, and idempotency
  diffing had no stable per-discrepancy identity. Caught while building data-validation's own
  re-run diff support; `contracts/examples/validation-report.example.json` updated to match (and
  rewritten to reflect a real fixture-derived result rather than an invented scenario). No prior
  release exists to bump a major version over -- see DECISIONS.md.
- `contracts/data-contract.schema.json`'s example: fixed the `uniqueness` test's `params` (was
  `{composite: true}`, a flag with no actual column list -- now `{columns: [...]}`, matching what
  `data-discovery`'s `propose_tests.py` really produces) and the `nullability` test on `customer_id`
  (was `threshold_basis: profiled` with `severity: blocking` and empty `params`, an inconsistent
  combination -- now `explicit_constraint` with `max_null_rate: 0`, matching a declared `NOT NULL`
  column). Caught while building `data-quality`'s contract-check derivation, which needs the real
  params shape to actually execute a check, not just display one.
- `contracts/data-contract.schema.json`'s example: added a `line_number` column to `fct_orders`
  (previously only `order_id`/`customer_id`/`order_total_usd` existed, but the example's own
  `grain_determination` and uniqueness test already declared the grain as the composite key
  `(order_id, line_number)` -- a column the test referenced didn't exist). Caught while building
  `data-pipeline`'s `build_transform_spec.py`, which needs every column a test or merge key names
  to actually be a real column, not just a name in a test's `params`.
- `contracts/examples/model-spec.example.json`: `fct_orders`'s `order_total_usd` mapping listed
  `transformation: "direct"` despite its source column (`silver.orders.total_amt`) being declared
  `TEXT`, not numeric -- the same source/target type mismatch this toolkit's fixture lakehouse
  deliberately plants, silently un-flagged in the one example meant to demonstrate handling it
  correctly. Now records an explicit `CAST(total_amt AS DECIMAL(18,2))` with a note that the
  source is TEXT pending a source-system fix. Caught while building `data-modeling`, whose own
  reference docs (`kimball-concepts.md`) instruct exactly this -- a TEXT-declared measure needs a
  documented cast, never a silent numeric assumption -- and the shipped example wasn't following
  its own toolkit's rule.

- `contracts/examples/model-spec.example.json`: `fct_orders`'s `degenerate_dimensions` referenced
  a source column (`silver.orders.order_number`) that doesn't exist in the fixture lakehouse (the
  real column is `order_id`) -- fixed. Its `measures` array listed both `order_total_usd` and
  `quantity`, but `source_to_target_mappings` only had an entry for `order_total_usd` -- added the
  missing `quantity` mapping. Both caught by `fixtures/integration_check.py`, which is precisely
  why it exists: neither gap was visible from reading the example in isolation, only from actually
  resolving it against real profiled columns.
- `.github/workflows/validate.yml`: the example-artifact validation step now passes
  `--supported-major 1` explicitly (previously relied on schema-type/major-version inference,
  which never exercised the "refuse an unsupported major version" path in CI); added the
  `integration-run` job (see Added, above).
- `fixtures/README.md`: corrected a claim that the `total_amt` TEXT-vs-REAL type mismatch
  "surfaces as a value mismatch once compared" in `data-validation` -- it does not; normalization
  correctly coerces numeric-looking text before comparing (working as designed). Caught by the
  Phase 3 sign-off run actually comparing the two and finding zero `changed` discrepancies where
  the doc predicted one.
- `toolkit.yaml`'s `sample_data.sensitive_columns` (email/phone hashing etc.) only ever redacted
  *samples* -- `data-pipeline`'s generated code had zero references to it, so a real target table
  got real, untransformed PII. Added a separate, opt-in `pii_handling.target_transform` config;
  `data-pipeline` now actually hashes tagged columns in generated code when enabled, and always
  surfaces a column it couldn't (`pii_transform_gaps`) rather than silently passing it through. See
  DECISIONS.md decision 55.
- `data-pipeline` silently dropped every column transformation beyond a bare rename -- found on a
  real engagement run and reproduced in this toolkit's own shipped `data-contract.example.json`
  (`order_total_usd` mapped from a TEXT source with no cast, ever). `data-contract.schema.json`
  gained optional `source.transformation`, `source.source_type`, and `scd_type` fields (minor,
  non-breaking -- bumped touched examples to `1.1.0`); `data-pipeline` now renders a contract's
  `transformation` as a real `F.expr(...)` expression, flags a declared-vs-actual type mismatch
  with no transformation present (`type_mismatch_gaps`, caps `readiness_level` at `draft`), and
  renders real SCD Type 2 history (`stored_as_scd_type=2` + `track_history_column_list`) for
  `declarative_pipeline` instead of the previous hardcoded Type 1. Also fixed: a pure bridge/
  junction table (every column a merge key) crashing the local idempotency proof
  (`sqlite3.OperationalError: incomplete input`) -- now falls back to `DO NOTHING`. See
  DECISIONS.md decision 56.

All contract schemas started at `schema_version` major 1 (`1.0.0`); `data-contract` has since taken
a minor, non-breaking bump to `1.1.0` (new optional fields only). No breaking (major) changes yet.

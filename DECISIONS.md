# DECISIONS.md

Every choice made where the original spec was ambiguous or left a placeholder, recorded as it's made, at
the phase it was made in. Confirm or correct any of these -- easier to fix now than after four more
skills are built on top of an assumption.

## Answered before Phase 0 started (your explicit answers)

- **Platform / catalog**: Azure + Unity Catalog.
- **Runtime / compute**: latest LTS DBR, serverless-preferred (falls back to classic), both Declarative
  Pipelines (DLT/SDP) and Lakeflow Connect available -- but `data-pipeline` still probes workspace
  capability at run time rather than hard-assuming availability, since "available in this default
  environment" and "available in this specific client workspace" are different facts. See the modality
  decision rubric to be built in `skills/data-pipeline/references/` (Phase 2).
- **Orchestration / transformation / auth**: Databricks Jobs, native PySpark/SQL (no dbt), service
  principal OAuth M2M, secrets from Databricks Secrets.
- **Python**: 3.11, implementation language for all bundled scripts.
- **Repo name**: `data-ai-skill-toolkit`.
- **output_dir default**: `~/.toolkit-runs/<project>/<run_id>/`.
- **LLM interpretation boundary**: the invoking agent itself does interpretation (root cause, confidence,
  mapping proposals) by reading deterministic script output per SKILL.md instructions. No skill script
  makes its own LLM API call. This keeps every skill's `scripts/` dependency-light and avoids a second
  credential/network surface to secure per engagement. Documented in
  `references/toolkit-conventions.md` section 5.
- **Fixture runtime**: local simulation (DuckDB) behind a thin adapter layer, not PySpark-local-mode or a
  live workspace, so evals run offline and reproducibly in CI. Documented in `fixtures/README.md`;
  adapter to be built in Phase 1.
- **Build approach**: built directly in this session, phase by phase, stopping at each gate specified in
  your prompt (this document is written incrementally at each stop, not all at once at the end).

## Phase 0 decisions (mine, need your review)

1. **`run` embedded, not a separate file.** `run-manifest.schema.json` defines an object embedded as the
   `run` property inside every other artifact (via `$ref`), rather than being written as a separate
   `run_manifest.json` file per run. Rationale: keeps each artifact self-describing and independently
   valid/readable without needing to locate a sibling file; `scripts/validate_artifact.py` resolves the
   `$ref` locally (no network fetch) regardless of which `jsonschema` version is installed. If you'd
   rather have a standalone `run_manifest.json` per run (e.g. for a lightweight index of all runs without
   opening every full artifact), that's a small schema change -- flag it now.

2. **Confidence + basis pairing is enforced by both the schema and a semantic checker.** JSON Schema
   alone can express "if mapping_type is llm_inferred, confidence and basis are required" per-field, but
   can't cleanly express "wherever a `confidence` key appears anywhere in the document, a `basis` key
   must accompany it" as a document-wide rule. `scripts/validate_artifact.py` walks the whole artifact
   and enforces this generically, so every current and future confidence-bearing field gets the check for
   free instead of needing a bespoke schema clause each time. This is why the script is required, not
   optional, before a skill declares success -- structural validity (`jsonschema` alone) isn't sufficient.

3. **Grain evidence field renamed `evidence` -> `basis` in `data-contract.schema.json`.** Caught by the
   semantic checker itself while building the first example: the toolkit-wide convention (confidence
   pairs with a field literally named `basis`, per `confidence-rubric.md`) needs the same field name
   everywhere a confidence score appears, not a synonym. Applied consistently across all schemas now.

4. **`target.workspace_id` and `target.catalog` are not treated as secrets**, but the whole `run` object
   (and therefore every artifact) is still required to live in `output_dir`, outside this repo, because
   workspace IDs and catalog names are client-identifying even though they're not credentials. This is a
   distinction worth being explicit about: "not a secret" and "safe to put in a public-ish repo" are not
   the same bar.

5. **Cross-platform validation (`data-validation`) modeled now, fleshed out in Phase 2.**
   `validation-report.schema.json` already has `source.platform` / `target.platform` and a nullable
   `type_coercion_map` (required non-null when platforms differ), so the schema doesn't need to change
   later when Phase 2 builds out the actual comparison logic -- but the *rubric* for which type coercions
   are valid across which platform pairs isn't written yet. That's Phase 2 scope.

6. **Pinning mechanism: git submodule pinned to an annotated tag, recommended over vendoring a copy.**
   See `README.md` "Pinning" for the reasoning and the vendoring fallback for client environments that
   restrict external git remotes in CI. This is a recommendation, not something the toolkit enforces --
   flag if your delivery teams have a strong existing preference (e.g. if vendoring is already standard
   practice at your firm for client-repo hygiene reasons, submodules may be the wrong default).

7. **CI eval job (`run-skill-evals` in `.github/workflows/validate.yml`) is scaffolded but currently a
   no-op** for each skill until that skill ships `evals/run_assertions.py` (Phase 1 onward). It's wired
   up now so each skill's Phase 2 PR just needs to add the assertions file, not touch CI config.

8. **"Passing eval run" for Phase 1, given no live Databricks workspace in this build environment**:
   evals will run as (a) the deterministic `scripts/` executed directly against the DuckDB fixture via
   the adapter layer, checked with concrete assertions (planted flaw detected, correct grain identified,
   schema-valid output via `validate_artifact.py`), plus (b) a small number of full skill runs via a
   subagent that loads `SKILL.md` and produces an artifact end-to-end from a realistic prompt, graded the
   same way. This is a lighter-weight version of skill-creator's full interactive browser-review loop --
   appropriate here because the goal is "evidence this produces schema-valid, flaw-detecting artifacts
   before it touches a client," not subjective UX/tone review of a single skill. If you want the full
   trigger-optimization / browser-review loop run too before Phase 1 sign-off, say so and I'll run it.

## Phase 1 decisions (mine, need your review)

9. **DuckDB substituted with SQLite for the fixture lakehouse.** The build environment has no
   network access to install packages, so `duckdb` (Phase 0's stated choice) isn't installable
   here. Switched to Python's stdlib `sqlite3` -- it was one of the options you approved
   ("Local simulation (DuckDB/SQLite/pandas)"), and arguably a better default for a toolkit
   distributed to many client environments anyway: nobody running this toolkit's evals needs to
   install anything at all. The three-level `catalog.schema.table` addressing is simulated via
   SQLite's `ATTACH DATABASE` (one file per schema). If your delivery teams standardize on DuckDB
   for other reasons, swapping the fixture backend is contained to
   `scripts/lakehouse_adapter.py`'s `SQLiteFixtureAdapter` -- nothing in any skill's `SKILL.md` or
   deterministic scripts assumes SQLite specifically, they only call the shared `LakehouseAdapter`
   interface.

10. **`scripts/lakehouse_adapter.py`, `estimate_scan_cost.py`, `redact.py`, and `diff_artifact.py`
    are shared at the repo root, not data-discovery-specific**, even though data-discovery is the
    first (and so far only) skill using them. The cost gate, redaction, and idempotency-diff rules
    in `references/toolkit-conventions.md` apply to all five skills; putting the implementations
    in one shared place now is exactly the "authored as one system" instruction from your original
    prompt -- data-quality and data-validation in Phase 2 will extend `diff_artifact.py` with their
    own differ functions and reuse the rest unmodified, rather than each writing their own version
    of "estimate cost before scanning."

11. **The LLM-interpretation boundary held exactly as decided in Phase 0**: every script in
    `skills/data-discovery/scripts/` is pure measurement (profiling, uniqueness/orphan checks,
    deterministic test-threshold derivation) and produces `discovery_findings.json`; SKILL.md then
    instructs the *agent* running the skill to do the interpretation (grain judgment calls beyond
    what's measured, column-mapping proposals, assembling the final contract) and validate its own
    output before declaring success. Both eval runs (see #13) confirm this split works in practice
    -- the subagent's reasoning about *which* source column should back an ambiguous target field
    (e.g. "region") was exactly the kind of judgment call that shouldn't live in a script.

12. **A `natural_key_naming_heuristic` was added to `profile_object.py`** beyond what Phase 0
    anticipated: it automatically checks uniqueness on any column matching `_number`/`_code`/
    `_key`/`email` naming patterns, in addition to the declared/positional primary-key candidate.
    Without this, the duplicated-`customer_number` planted flaw would never surface, because the
    declared surrogate key (`customer_id`) profiles as perfectly unique on its own -- a real
    business-key duplication can hide behind a clean surrogate key, and a discovery tool that only
    checks the declared PK would miss exactly this class of problem. Caught and fixed a real bug
    in the process: this heuristic initially also flagged `line_number` (part of a composite PK)
    as a "duplicate" natural key, a false positive from the naming pattern matching a column that
    was already covered by the composite-key check -- fixed by excluding PK-member columns from
    the heuristic.

13. **Also caught and fixed while building**: SQLite's `SELECT DISTINCT col1, col2` (used for
    composite-key uniqueness checks) collapses all-NULL rows into one group, unlike
    `COUNT(DISTINCT single_col)` which ignores NULLs entirely per standard SQL semantics -- this
    initially miscounted 17 NULL emails as 16 "duplicates." Fixed in both `SQLiteFixtureAdapter`
    and `DatabricksAdapter` by excluding any row with a NULL key column before checking
    distinctness, matching standard multi-column UNIQUE constraint semantics (NULL is never equal
    to NULL). Flagging this because it's the kind of subtle correctness bug that would have shipped
    a wrong-but-plausible-looking "duplicate" finding to a client if it hadn't been caught by
    building and testing against real fixture data rather than reasoning about the SQL abstractly.

14. **Freshness tests are not auto-proposed** by `propose_tests.py` (contrary to the original
    five-test-types list reading as "all five, always"). A defensible freshness threshold needs the
    *cadence* of updates (e.g. typical gap between distinct dates), and the fixture lakehouse's
    synthetic dates don't have a realistic cadence to derive one from without inventing a number --
    which would violate the "derived from profiling, not invented" rule. `propose_tests.py` is
    structured so adding this is a contained change once there's a real profiled cadence signal to
    build it from; documented as a known gap in `references/grain-and-tests.md` rather than papered
    over with an arbitrary default.

15. **"Passing eval run" delivered as**: all 9 deterministic checks in
    `skills/data-discovery/evals/run_assertions.py` pass (read-only enforcement, malformed-artifact
    rejection, all four discovery-relevant planted flaws caught with zero false positives), plus two
    full subagent runs (greenfield and resolution invocation modes, `evals.json` cases 1 and 2) that
    I independently re-verified against `scripts/validate_artifact.py` rather than trusting the
    subagent's self-report -- both passed cleanly, with the resolution-mode run correctly reporting
    two genuine `unresolved_requirements` (a measure with no mapping in the spec, and a spec'd
    column that doesn't exist in the source) instead of silently dropping them. Eval cases 3
    (cost-gate halt) and 4 (wrong-skill redirect) were **not** run as full subagent scenarios this
    phase -- case 3's mechanism is covered by `estimate_scan_cost.py`'s own tested halt behavior,
    and case 4 depends on skill descriptions that are more meaningfully tested once all five skills
    exist and can collide with each other (the real triggering-accuracy question). Both are recorded
    in `evals.json`/`eval_metadata.json` for later use; see `skills/data-discovery/evals/README.md`
    for what's covered and what to re-run before this touches a real client. Flag if you want cases
    3-4 run now before proceeding to Phase 2.

## Phase 2 decisions -- data-validation (mine, need your review)

16. **`validation-report.schema.json` gained required `kind`/`key` fields on each discrepancy**,
    and `sample_diff_ref` became nullable. The Phase 0 schema only had `stage_detected`,
    `columns_affected`, `sample_diff_ref`, and `diagnosis` on a discrepancy -- no field actually
    identified *which row* was affected without opening a separate pointer file. This surfaced
    immediately when I tried to build the idempotency differ (`diff_artifact.py`'s
    `diff_validation_report`), which needs a stable per-discrepancy identity to tell "resolved
    since last run" from "newly found" from "still there." Fixed the schema, updated
    `contracts/examples/validation-report.example.json` to match. Since nothing has been tagged/
    released yet, I treated this as a normal pre-release fix rather than a major-version bump --
    flag if you'd rather I treat schema stability as binding starting now, in which case this
    would need to be `2.0.0` with a documented migration note instead.

17. **The Phase 0 example artifact for `validation-report` was rewritten from an invented scenario
    ("32 rows differ", fabricated hashes) to the real fixture-derived result** (the actual orphaned
    order 5230/line 1, real hash values from a real run). It's more useful as a worked example when
    it's traceable and independently re-runnable against `fixtures/lakehouse` rather than
    disconnected from anything checkable.

18. **Staged comparison design: stages 2-4 share ONE fetch, not independent SQL-pushdown queries
    per stage.** `hash_aggregate`, `column_aggregate`, and `row_level_diff` all operate on the same
    bounded, ordered fetch from each side (capped at `content_check_row_cap`, default 100,000
    rows/side) -- computed once, reused three ways, rather than three separate round trips. This
    is a real, stated scope tradeoff: it means the "digging deeper only as needed" cost savings
    only apply to whether you fetch at all (gated by the pre-flight cost estimate) and whether you
    go past `hash_aggregate`, not to avoiding the row-content fetch itself once you're past
    `row_count`. A genuinely production-scale implementation (multi-million-row tables) would need
    SQL-side hash/aggregate pushdown per stage instead of fetching to Python -- documented as a
    known limitation in `references/staged-comparison.md` rather than silently assumed away. This
    was the right tradeoff for this build (portable cross-dialect hashing in raw SQL is a
    substantially larger effort, and the fixture tables are all small), but it's a real
    production-readiness gap worth your attention before this runs against a genuinely large client
    table.

19. **Row matching is key-based, not "sort both sides then hash sequentially."** The
    `validation-report.schema.json`'s own `normalization_applied.ordering` field (from the Phase 0
    example) suggested a sort-then-hash approach; I implemented key-based matching instead (both
    sides fetched, indexed by declared/candidate key into a dict, compared by key) because
    sort-then-hash desyncs everything after the first missing/extra row, while key-based matching
    localizes exactly the rows that differ regardless of how many. Functionally achieves the same
    goal (order doesn't cause false positives) more robustly; documented in
    `references/normalization-and-type-coercion.md` and `references/staged-comparison.md`.

20. **`known_acceptable_differences` uses a small fixed rule language (`column_ignore` /
    `key_ignore`), not a general expression evaluator.** The spec asked "how known-acceptable
    differences are declared and excluded" without specifying a mechanism; I chose the more
    restrictive design deliberately -- an `eval()`-style condition language is both a code-execution
    risk surface and an easy way to accidentally build a rule broad enough to hide real
    regressions. See `references/known-acceptable-differences.md` for the reasoning. Flag if your
    delivery teams need more expressiveness than "ignore this column everywhere" / "ignore this
    specific row" -- it's a contained extension if so.

21. **"Passing eval run" delivered as**: all 13 deterministic checks in
    `skills/data-validation/evals/run_assertions.py` pass (read-only enforcement, malformed-artifact
    rejection, the real fixture discrepancy found exactly and with zero false positives on the
    type-mismatched column, correct early-stop on a clean self-comparison, correct
    known-acceptable-difference exclusion), plus one full subagent run (the real-discrepancy
    diagnosis scenario, `evals.json` case 1) that I independently re-verified against
    `scripts/validate_artifact.py` and by inspecting the artifact's actual field values rather than
    trusting the subagent's self-report -- it passed cleanly, with a well-grounded 0.85-confidence
    diagnosis (documented table comment plus full-population profiling, correctly *not* scored in
    the 0.95+ band since the actual build query wasn't inspected directly). Eval cases 2-4 were
    validated at the mechanism level via the deterministic suite but not re-run as full subagent
    scenarios this phase, consistent with the same scoping decision made for data-discovery in
    Phase 1 -- see `skills/data-validation/evals/README.md`. Flag if you want them run now.

## Phase 2 decisions -- data-quality (mine, need your review)

22. **`status` and `severity` are two separate axes, not one.** The schema already had both fields
    on a check, but the spec didn't spell out how they interact; I made `severity` a configured
    property ("how much this matters," set by whoever wrote the check) and `status` the measured
    outcome (`passed`/`failed`/`warned`/`not_evaluated`), with the rule "threshold violated +
    `severity: blocking` -> `failed`; threshold violated + `severity: warning` -> `warned`."
    Without this separation a warning-severity check that fails would either look identical to a
    passed check or get conflated with a hard blocking failure -- neither is right.

23. **A check with no configured threshold is `not_evaluated`, never a silent pass.** E.g. a
    `row_count` check with neither `min_rows` nor `max_rows` set doesn't default to "always
    passes" -- it reports `not_evaluated` with a reason. A quality report where every unconfigured
    check quietly shows green is worse than useless; it's actively misleading. Verified this holds
    for every one of the seven check types, not just the obvious ones.

24. **`custom_sql` is real, not a stub** -- it executes a caller-supplied `SELECT` against the
    target, guarded by a new `scripts/lakehouse_adapter.py` function (`assert_read_only_select`)
    that rejects anything containing DDL/DML/admin keywords or more than one statement. I want to
    be direct about what this guard is and isn't: it's defense-in-depth against an obviously wrong
    or malicious check definition, not a substitute for the toolkit's standing rule that the
    connection this skill runs under should itself hold read-only credentials
    (`references/toolkit-conventions.md` #1-2). A sufficiently adversarial SQL string could
    probably find a way around a keyword blocklist; the actual security boundary is the database
    role, same as every other skill in this toolkit. Tested against several bypass shapes
    (stacked statements via `;`, an `UPDATE` disguised as... it isn't disguised, it's just an
    `UPDATE`) but this is not a claim of comprehensive SQL-injection-proofing.

25. **A real eval finding changed the skill, not just the docs.** Eval 1's subagent run hand-
    authored 9 checks for `silver.orders` based on "check nulls, uniqueness, the FK, quantity
    range" -- a reasonable, defensible check list that nonetheless skipped `ship_region` (the
    planted nullable-that-shouldn't-be flaw), because `ship_region` is declared nullable and a
    fresh-eyes pass naturally checks declared-required columns first. `data-discovery`'s own
    profiling had already caught this exact nuance and proposed a test for it -- the gap was that
    `data-quality`'s SKILL.md listed contract-derivation and hand-authoring as two equal options
    without a preference. I added an explicit instruction: derive from an existing contract rather
    than hand-authoring from scratch when one exists, reserving hand-authored checks for genuine
    gaps a contract wouldn't know about. This is exactly the toolkit's own stated worry playing out
    in miniature -- "if authored independently, teams spend time gluing" -- and exactly why the
    contract-check integration point exists at all. I did not re-run eval 1 after this fix (the
    original run's formal assertions in `eval_metadata.json` all still passed; the gap was a
    narrative/design-quality observation, not a compliance failure) -- flag if you want it re-run
    to confirm the guidance change actually closes the gap in practice, not just in the doc.

26. **"Passing eval run" delivered as**: all 16 deterministic checks in
    `skills/data-quality/evals/run_assertions.py` pass (read-only enforcement including explicit
    `custom_sql` guard bypass attempts, malformed-artifact rejection, all relevant planted flaws
    caught with correct status, threshold-less checks correctly `not_evaluated`, contract-derived
    checks genuinely executing against real data), plus one full subagent run (the multi-check
    diagnosis scenario, `evals.json` case 1) that I independently re-verified against
    `scripts/validate_artifact.py` and by inspecting the artifact's actual check/diagnosis content
    -- it passed cleanly (a 0.55-confidence referential-integrity diagnosis, correctly in the
    "profiled, not enforced" band), and its one real gap (see #25) was diagnosed and fixed in the
    skill itself. Eval cases 2-4 were validated at the mechanism level via the deterministic suite
    but not re-run as full subagent scenarios this phase, consistent with prior phases -- see
    `skills/data-quality/evals/README.md`. Flag if you want them run now.

## Phase 2 decisions -- data-pipeline (mine, need your review)

27. **Added a sixth contract schema, `pipeline-manifest.schema.json`, rather than stretching
    `run-manifest.schema.json` to cover it.** Phase 0 specified five schemas
    (`data-contract`/`model-spec`/`quality-report`/`validation-report`/`run-manifest`), and
    `data-pipeline` wasn't given a dedicated one -- its output is code on disk, not a
    lineage/comparison artifact like the other four. But recording a run's modality decision,
    per-target generated files, mock-data-based idempotency evidence, and human-approval
    readiness level genuinely needs its own structured shape; the bare `run-manifest` envelope
    (embedded as `run` in every other artifact) has no fields for any of that, and extending IT
    to carry pipeline-specific content would mean every other skill's artifacts inherit fields
    that only make sense for one of the five. A new, sixth schema -- following the exact same
    pattern as the other four (embeds `run-manifest` via `$ref`, own `$id`, own major version,
    own example, registered in `validate_artifact.py` and `diff_artifact.py` the same way)
    -- was the smallest change that didn't touch a contract every other skill depends on. Flagging
    this explicitly since it's a bigger structural call than most Phase 2 decisions: the original
    "5 schemas" framing in the spec was, I believe, describing Phase 0's scope at the time
    `data-pipeline` hadn't been designed yet, not a hard cap -- but confirm before treating this
    as settled.

28. **`data-pipeline` never deploys, period -- not even with explicit approval.** Re-reading
    `toolkit-conventions.md` #1 and #7 gate 3 closely: "a human (or a separate, explicitly
    authorized process) takes it from there" reads as data-pipeline itself never being the thing
    that deploys, regardless of approval. So this skill's `deployment` field, once a human names a
    specific target in-conversation, only ever *records* that approval (`approved`,
    `approved_by`, `target_named`, `approval_note`) and advances `readiness_level` to
    `approved_for_deployment` -- there is no script anywhere in this skill that runs
    `databricks bundle deploy`, creates a job, or executes generated code against a real target.
    `readiness_level: deployed` is set by whatever external process actually deploys, which is out
    of this skill's scope entirely. This is the more conservative of two readings of that
    sentence; flag if you intended data-pipeline to be able to actually execute a deployment once
    approved -- that would need new tooling this build deliberately did not create.

29. **`build_transform_spec.py` refuses multi-source targets rather than attempting a join.**
    Every column in a v1 target must map from a single source object; a target needing a join
    (like the `fct_orders_usd_normalized` eval fixture, which joins `silver.orders` and
    `silver.fx_rates`) gets a `ValueError` naming the distinct source objects, not a guessed-at
    JOIN clause. This routes the modality classification to `complex_procedural` ->
    `pyspark_notebook`, where a human writes the actual join, informed by the per-source column
    mappings this skill can still derive one source at a time. Scoped out deliberately, not
    silently -- see `references/other-modalities.md`.

30. **`streaming_cdc` is a schema-level `load_pattern` option with no template that generates it.**
    Neither the PySpark notebook nor the Declarative Pipeline template produces true low-latency
    streaming code (a durable checkpoint location, a continuous/short-trigger refresh policy) --
    both always render `merge_upsert` or `full_refresh` batch code, regardless of
    `requires_streaming`. A target that genuinely needs streaming gets routed to
    `pyspark_notebook` (same as multi-source joins) for hand-extension. This felt better than
    generating streaming-shaped code that either doesn't specify a checkpoint location correctly
    or pretends idempotency guarantees this toolkit hasn't actually earned for the streaming case
    -- see `references/other-modalities.md`.

31. **Local idempotency evidence is real but deliberately scoped -- proven on synthetic mock data
    in a scratch SQLite database, never on generated Spark code running on a real cluster.** With
    no live Databricks/Spark workspace available to this toolkit's evals or CI (the same
    constraint `DatabricksAdapter` already documents), `validate_pipeline_locally.py` renders the
    SAME transform_spec that becomes the generated code as a portable SQL upsert, runs it twice
    against mock data, and diffs the result. This proves the merge-key logic is idempotent against
    a representative dataset SHAPE; it does not prove the generated `.py`/`.yaml` files are valid
    Spark, or that they'll behave the same against messier real data (mock data is shaped from the
    TARGET column's declared type, not the source's actual one -- see decision 32). Generated
    Python files get a `compile()` syntax check, nothing more. `readiness_level: validated` means
    exactly this evidence passed, not "deployed and confirmed working" -- spelled out explicitly
    in `references/idempotency-and-mock-data.md` so the gap can't be missed by a reader skimming
    the manifest.

32. **A real SQLite grammar quirk, found and fixed while building the idempotency proof:**
    `INSERT INTO dest (...) SELECT ... FROM src ON CONFLICT(...) DO UPDATE SET ...` raises a
    syntax error in this sandbox's SQLite (3.37.2) unless the `SELECT` carries a `WHERE` clause --
    SQLite's own documented workaround for a parser ambiguity between an upsert's `ON CONFLICT`
    and a join's `ON` condition. Fixed by appending `WHERE 1=1` to the rendered `SELECT` inside
    `_apply_merge()` only (not in `render_select_sql()`, which stays a clean, template-shareable
    SELECT). This is purely a local-testing-harness quirk with no bearing on the generated Spark
    `MERGE INTO`/`apply_changes` syntax, which has no equivalent ambiguity -- documented inline in
    the code, not just here, so a future reader doesn't mistake the `WHERE 1=1` for something that
    needs to appear in generated pipeline code too.

33. **Two bugs in `contracts/examples/data-contract.example.json`, found while wiring
    `build_transform_spec.py` against it:** the `fct_orders` example declared a composite grain
    and a uniqueness test on `(order_id, line_number)`, but had no `line_number` column at all --
    added one (`explicit_alias` from `silver.orders.line_number`, matching the pattern of
    `order_id`). This is the same category of "the example wasn't fully self-consistent because it
    was originally illustrative, not meant to be executed end-to-end" issue found twice already in
    Phase 2 (the uniqueness/nullability `params` fixes while building `data-quality`) -- each time
    surfaced by a skill that actually needed to RUN the example rather than just display it.
    Fixed, documented in CHANGELOG.md, and re-validated.

34. **"Passing eval run" delivered as**: all 33 deterministic checks in
    `skills/data-pipeline/evals/run_assertions.py` pass (the write-boundary check confirming every
    script except the local idempotency harness contains zero write-shaped SQL, and that the
    harness's one database connection is a literal in-memory SQLite scratch; the modality rubric's
    full priority order across five representative factor combinations; the documented
    multi-source refusal; mock data respecting declared nullability and uniqueness; a real
    idempotency match on the shipped example contract plus a case distinguishing
    duplicate-but-consistent rows from a genuine mismatch; code generation and Python
    `compile()`-validity for all three modalities; and a full transform-spec-to-validated-manifest
    end-to-end run), plus one full subagent run (eval 1: classify the modality, generate real
    Declarative Pipeline code and expectations for `fct_orders`, prove idempotency, assemble and
    validate the manifest) that I independently re-verified by reading the actual generated files
    and manifest fields, not the subagent's summary -- modality/readiness/deployment/idempotency
    fields were all correct, the two generated `.py` files compiled cleanly, and the final response
    to the simulated user correctly asked which target to deploy to rather than assuming approval,
    which is the single most safety-relevant behavior this skill has. Evals 2-5 were validated at
    the mechanism level (the rubric choosing `lakeflow_connect`/`pyspark_notebook` correctly against
    their purpose-built fixture contracts under `evals/fixtures/`, which are new deterministic-suite
    fixtures introduced this phase) but not re-run as full subagent scenarios, consistent with the
    scoping decision made for every skill's Phase 1/2 sign-off so far. Eval 5 (the deployment-gate
    scenario) is the one I'd most want re-run as a full subagent scenario before this touches a
    real engagement, given what's at stake if that gate is ever wrong.

## Phase 2 decisions -- data-modeling (mine, need your review)

35. **Silver verification checks five deterministic, measured signals, chosen to be checkable
    against real object metadata, not a semantic judgment about "curation" in the abstract**:
    a declared primary key that also PROFILES as genuinely unique (not just declared -- Unity
    Catalog PK/FK constraints are informational, not enforced, so a declared-but-not-actually-
    unique PK is a real gap worth catching), a table comment, absence of raw-ingestion-artifact
    column names (`_rescued_data`, `_ingest_*`, etc.), and >=80% of columns following
    `snake_case` business naming rather than source-system `PascalCase`. All five must pass for
    `verified: true`. I chose this five-signal set because the toolkit's own fixture lakehouse
    was explicitly built with these exact signals in mind (see `fixtures/generate_fixtures.py`'s
    own comments) -- I verified deterministic behavior against the real fixture rather than
    picking signals in the abstract and hoping they'd generalize. The 80% naming threshold and
    the specific raw-ingestion-artifact regex are both judgment calls on my part with no fixture
    edge case to calibrate them against beyond the one bronze/silver contrast this toolkit ships
    with -- flag if you have real client examples where either threshold produces a wrong
    verdict, since a fixture with exactly one "obviously bronze" and one "obviously silver"
    object can't validate a threshold's precise boundary.

36. **Silver verification checks CURATION STRUCTURE, deliberately not data quality.** This is
    the single most important design decision in this skill, so it's worth stating plainly here
    too, not just in `references/silver-verification.md`: `silver.orders` and `silver.customers`
    both PASS verification despite carrying three of the fixture's five planted flaws (broken FK,
    duplicated natural key, a nullable column that shouldn't be). That's intentional -- those are
    `data-quality`/`data-validation`'s problems, not a reason to block dimensional design. If you
    intended "verified curated silver" to also mean "verified clean data," this skill needs a
    materially different (and much more expensive -- full data-quality-shaped) gate. I read the
    original spec's framing ("presence of declared PK/FK constraints, documented column
    comments... deduplication (no raw-ingest duplicate natural keys)...") as being about
    STRUCTURAL dedup (are rows genuinely being deduplicated at ingestion, which a declared+
    profiled-unique PK proves) rather than "zero natural-key duplicates anywhere in the table,"
    since the latter reading would make `data-modeling` redundant with `data-quality` rather than
    layered on top of it. Flag if that reading is wrong.

37. **A real subagent finding, handled without a code change but a documentation clarification:**
    running eval 1 surfaced that `silver.customer_region_history` (the fixture's own SCD-2
    evidence table) itself FAILS `verify_silver_layer.py` -- it has no declared primary key. The
    subagent correctly reasoned that this doesn't invalidate using it as SCD evidence (a sibling
    history table's EXISTENCE and documented purpose is evidence for a design decision about an
    already-verified dimension's attribute, not a claim that the history table itself is a
    verified direct source) while being careful to exclude it from `source_to_target_mappings`
    and flag the gap in `assumptions[]`. This was the right call, and it's exactly the kind of
    distinction `detect_scd_candidates.py` was designed to support (it deliberately does not call
    `verify_silver_layer.py` internally), but it wasn't spelled out anywhere before this eval
    surfaced the question. Added a new section to `references/scd-type-selection.md` making the
    distinction explicit ("the history table itself doesn't need to pass silver_verification to
    count as evidence") so the next reader doesn't have to rediscover this by running into it.

38. **`model-spec` has no `diff_artifact.py` differ yet**, unlike the other four artifact types.
    A star schema design changing between runs (a fact's grain changing, a dimension's SCD type
    flipping, a conformance group changing) is a genuinely different, more structurally varied
    diff shape than the other four artifacts' -- facts and dimensions are both named arrays with
    deeply nested per-attribute detail, and I did not want to ship a shallow/misleading differ
    just to check the "every artifact type has one" box under this build's remaining time. Step 7
    of `SKILL.md` documents the gap and the manual fallback (compare `facts[]`/`dimensions[]` by
    name) rather than silently omitting the topic. Worth a real differ in a follow-up pass if
    `data-modeling` gets used for iterative (not just greenfield) design work.

39. **The two example-artifact fixes found while building this skill** (`data-contract.example.json`
    missing a `line_number` column, found while building `data-pipeline` -- see decision 33 -- and
    now `model-spec.example.json`'s `order_total_usd` mapping silently listing `transformation:
    "direct"` for a TEXT-declared source column) are the same category of issue for the third and
    fourth time this build: examples written early, before a later skill actually needed to
    EXECUTE against them, turned out not to be fully self-consistent or not to follow this
    toolkit's own stated rules. Every one of these was caught by a skill that had to actually run
    the example, not by inspection. Recorded here as a pattern worth naming explicitly: if a sixth
    skill or a client engagement extension is ever added to this toolkit, expect it to find one
    more thing like this in the existing examples, and don't be surprised.

40. **"Passing eval run" delivered as**: all 20 deterministic checks in
    `skills/data-modeling/evals/run_assertions.py` pass, most importantly the assertion that
    `bronze.raw_orders` is correctly refused while `silver.orders`/`silver.customers` are correctly
    verified DESPITE their planted data-quality flaws (decision 36's design claim, actually
    checked against real fixture data, not just asserted in prose), plus one full subagent run
    (eval 1: a complete greenfield star schema design against the real fixture lakehouse) that I
    independently re-verified by reading the actual `model-spec.json` and `design_rationale.md`
    files, not the subagent's summary -- the SCD-2 rationale on `region` was genuinely grounded in
    `customer_region_history`'s real row-level evidence (not an invented justification), the
    TEXT-to-decimal cast on `total_amt` was handled correctly and explicitly, and the response
    correctly stated the design needs human approval before `data-discovery` resolution, matching
    `toolkit-conventions.md` #7 gate 2. This is also the run that surfaced decision 37. Evals 2-4
    were validated at the mechanism level (the refusal path and the conformance-candidate check
    are both exercised directly in the deterministic suite) but not re-run as full subagent
    scenarios, consistent with the scoping decision made for every skill's Phase 1/2 sign-off in
    this build.

**Phase 2 is complete: all five skills (`data-discovery`, `data-validation`, `data-quality`,
`data-pipeline`, `data-modeling`) are built, linted, schema-validated, and have passing
deterministic eval suites plus at least one independently-verified subagent scenario run each.**
Phase 3 (the integration run across the fixture lakehouse -- modeling -> discovery -> pipeline,
with quality/validation attached at their gates -- plus finalizing CI and a full `DECISIONS.md`
review) is the only phase left per the original spec's ordering.

## Phase 3 decisions -- integration run + CI (mine, need your review)

41. **The integration check is fully deterministic (no subagent, no LLM), by design, and that's a
    real scope limitation, not an oversight.** `fixtures/integration_check.py` resolves the
    shipped `model-spec.example.json`'s ALREADY-EXPLICIT `source_to_target_mappings` against real
    profiled columns -- legitimate to script because that example names exact source columns with
    no ambiguity left to resolve. A model-spec with genuine ambiguity (the normal case in a real
    engagement) still needs an agent in the loop for that step; this check proves artifact SHAPES
    fit together end to end, not that the judgment quality at each step is good -- that's what
    each skill's subagent scenario evals are for (and remain the right tool for that job). I
    considered spawning a subagent for a true end-to-end judgment-inclusive integration run instead
    of/in addition to this deterministic one, and decided the deterministic version was more
    valuable as a permanent, CI-wired regression check (a subagent run is expensive to run on every
    PR and non-reproducible run-to-run) while a one-off full-judgment integration run would mostly
    duplicate what the five skills' individual scenario evals already cover in combination. Flag if
    you want a genuine subagent-driven integration run too, as a one-time Phase 3 sign-off
    artifact alongside this permanent CI check.

42. **The integration check found six real bugs in this toolkit's own shipped examples before it
    ever ran clean**, across all three iterations of this build session: `data-contract.example.json`
    (missing `line_number` column, found building `data-pipeline`; uniqueness/nullability `params`
    inconsistencies, found building `data-quality`) and `model-spec.example.json` (a
    `degenerate_dimensions` entry naming a column -- `order_number` -- that doesn't exist in the
    fixture lakehouse at all; a `total_amt`-to-`order_total_usd` mapping silently missing its
    required cast; and a `quantity` measure with no `source_to_target_mappings` entry at all). Every
    one of these was invisible to schema validation alone (all six were schema-VALID, just not
    faithful to the real fixture data or the toolkit's own stated rules) and was only caught by a
    script that actually tried to USE the example against real data. This is the strongest argument
    I can offer for why `integration_check.py` earns a permanent spot in CI rather than being a
    one-off Phase 3 exercise: schema validation and even each skill's own isolated eval suite both
    missed all six; only cross-skill execution caught them.

43. **`model-spec` has no `diff_artifact.py` differ (decision 38, Phase 2) and still doesn't** --
    the integration check doesn't exercise idempotency/re-run diffing for any artifact type, since
    that would require running the whole chain twice and comparing, which is a meaningfully bigger
    CI job for a check that's already proving the thing it most needs to prove (do the shapes fit
    together at all). Re-run diffing for each artifact type is already unit-tested within each
    skill's own eval suite (data-contract, validation-report, quality-report, pipeline-manifest all
    have real `diff_artifact.py` coverage); I did not see a strong case for re-proving that at the
    integration level too, given the added CI runtime cost.

44. **CI finalization**: `.github/workflows/validate.yml` already had all five skills in its
    `run-skill-evals` matrix from Phase 0 (built presciently, before four of the five skills
    existed) -- confirmed it still needed no changes there. Two real changes made: the
    example-artifact validation step now passes `--supported-major 1` explicitly (previously
    relied on schema-type/major-version inference alone, which never actually exercised the
    "refuse an unsupported major version" refusal path in CI -- a real gap, since that refusal
    behavior is one of this toolkit's more load-bearing cross-cutting guarantees per
    `references/toolkit-conventions.md`); and a new `integration-run` job runs
    `fixtures/integration_check.py` after `validate-contracts-and-skills` passes. I did not add a
    scheduled (cron) trigger beyond the existing `push`/`pull_request` triggers -- flag if you want
    a nightly run too, e.g. to catch drift if `contracts/examples/*.json` or fixtures change without
    a corresponding code change touching the same PR.

**Phase 3 is complete.** All five skills are built, linted, schema-valid, individually eval-tested
(deterministic suites + independently-verified subagent scenario runs), AND proven to chain
together end to end against the real fixture lakehouse via a permanent, CI-wired integration check.
This toolkit is ready to push as specified in the original prompt's deliverable section.

## Summary: what most needs your review

40+ decisions accumulated across four phases -- most are narrow implementation calls you can skim
or skip. These are the ones most likely to actually matter to you:

- **Decision 9**: DuckDB (your stated default) was replaced with SQLite for the fixture lakehouse,
  because this build environment had no network access to install DuckDB. Nothing in any skill
  assumes SQLite specifically (all access goes through `LakehouseAdapter`), so this is swappable,
  but it's the single biggest deviation from your explicit answers before Phase 0.
- **Decision 27**: a sixth contract schema (`pipeline-manifest.schema.json`) was added beyond the
  five Phase 0 originally specified, because `data-pipeline`'s output genuinely didn't fit any of
  the other four artifact shapes.
- **Decision 28**: `data-pipeline` never deploys anything itself, even with explicit human
  approval -- it only records the approval. Confirm this matches your intent; the alternative
  reading (this skill CAN deploy once approved) would need new tooling this build didn't create.
- **Decisions 36 and 42**: `data-modeling`'s silver-verification gate checks curation STRUCTURE,
  not data quality -- it will happily design against a table with real data-quality problems, on
  purpose. If you wanted "verified curated silver" to also mean "verified clean data," this needs
  a materially different (and much more expensive) gate.
- **Decision 41**: the Phase 3 integration check is deterministic only, no subagent/LLM in the
  loop. If you want a genuine judgment-inclusive integration run as a one-time sign-off artifact
  (not just the permanent CI check), say so and I'll run one.
- **Decisions 16, 22-26, 33, 37, 39, 42**: a running list of real bugs found in this toolkit's own
  shipped example artifacts and fixtures, each fixed as found. Worth a final skim of
  `contracts/examples/*.json` yourself before this goes to a client engagement, given how many
  turned up.

Everything else in this document is a narrower implementation call, recorded for traceability more
than because it's likely to need correction.

## Phase 3 addendum -- genuine judgment-inclusive integration sign-off (mine, need your review)

Per your instruction, a full subagent-driven run through all five skills in order was performed as
a one-time sign-off artifact (not wired into CI -- that remains `fixtures/integration_check.py`,
the deterministic version). Fresh business request, fresh design (NOT a replay of any shipped
example), each stage genuinely reading the previous stage's real file output from
`/tmp/integration-signoff/`. All six artifacts (model-spec, data-contract, two pipeline-manifests
-- fact and dimensions, quality-report, validation-report) independently re-validated by me, not
just accepted on the subagent's word.

45. **The run surfaced real, load-bearing findings -- genuine gaps in the shared generator code and
    schemas, not shipped-example inconsistencies like the earlier batch.** I independently verified
    the three most serious by reading the actual generated files:
    - `generate_pipeline_code.py`'s expectations dict-building emits the SAME literal key
      (`"valid_grain"`) for every uniqueness test on a table. `silver.customers` has three
      candidate keys (declared PK, plus two natural-key-naming-heuristic hits on `customer_number`
      and `email`); the generated `EXPECTATIONS` dict for `dim_customer` has three `"valid_grain"`
      entries, and only the last survives at runtime -- confirmed by reading the actual file.
      **This silently drops the `customer_number` uniqueness expectation, which is precisely the
      duplicated-natural-key flaw this fixture lakehouse plants.**
    - The Declarative Pipeline template always emits `dlt.apply_changes(..., keys=[...])`
      regardless of `load_pattern`. When `merge_keys` is empty (`full_refresh`), the generated
      code is `apply_changes(keys=[])`, which is not valid -- `apply_changes` requires at least one
      key. Confirmed present in the generated `dim_date`/`dim_ship_region` code (both correctly
      classified `full_refresh`, since a generated calendar/lookup table has no natural merge key).
    - `build_pipeline_manifest.py` names mock-data files by SOURCE table only
      (`mock_data/<schema>.<table>.json`), not by target. Three targets in this run
      (`fct_order_line`, `dim_date`, `dim_ship_region`) all source from `silver.orders`; their mock
      data silently overwrote the same file in sequence. Confirmed: only two files exist where
      three targets ran.

    All three are real bugs in shared, reusable generator code every future engagement would hit,
    not one-off example-data problems -- a materially different category from the batch you said
    is fine to defer (decisions 16, 22-26, 33, 37, 39, 42, all shipped-`.example.json` content
    issues). **I have not fixed these yet, pending your direction on scope** -- see the question
    accompanying this summary in chat.

46. **Two additional findings are architectural, not quick bug fixes, and need a design decision,
    not just a patch:** (a) `data-contract.schema.json` has no field to carry a column's
    transformation expression forward -- `source.mapping_type` records HOW a column was mapped,
    but not the transformation itself, so `data-pipeline` has no way to know a column needs
    `CAST(total_amt AS DECIMAL(18,2))` versus a straight alias, and the generated code silently
    does a straight alias for every column. (b) The generated Declarative Pipeline code hardcodes
    `stored_as_scd_type=1` regardless of what `model-spec.json` declared -- SCD type also doesn't
    survive from contract to generated code, for the same underlying reason (the contract has
    nowhere to carry it forward once discovery resolves a model-spec). Both trace to the same root
    cause: `data-contract.schema.json`'s columns only carry `{name, type, nullable, source}`, no
    transformation/SCD-type field, and it was designed in Phase 0 for `data-quality`/
    `data-validation`'s needs before `data-pipeline` existed to consume it downstream. Fixing this
    properly means extending `data-contract.schema.json` (another schema change, on top of the
    sixth-schema addition in decision 27) -- flagging for your direction rather than doing
    unilaterally, since it touches a contract every skill depends on.

47. **Smaller findings, worth a mention but lower stakes:** `pipeline-manifest.schema.json` has one
    `modality_decision` per manifest but a `targets[]` array, so a run whose targets genuinely
    classify to different modalities (as this run's did -- the fact needed `pyspark_notebook`, the
    dimensions were `declarative_pipeline`) can't be expressed in a single manifest; the sign-off
    run worked around this by writing two manifests, which is itself informative -- either the
    schema should allow a modality decision per target, or the convention should be one manifest
    per (target, modality) pairing, documented as such. Also: `fixtures/README.md` overstates what
    the source/target type-mismatch flaw does -- it claims `total_amt`'s TEXT-vs-REAL mismatch
    "surfaces as a value mismatch once compared," but `data-validation`'s normalization correctly
    coerces numeric-looking text before hashing (working as designed per
    `references/normalization-and-type-coercion.md`), so it does NOT surface as a discrepancy --
    the fixture doc's claim is simply wrong and should be corrected.

48. **What worked, unprompted and without intervention**: the silver-verification gate halted for
    real on `silver.customer_region_history` (no declared PK) and the design was re-scoped rather
    than forced through with `--force`; `data-discovery` resolution mode reported 4 genuine
    `unresolved_requirements[]` (most notably declining to fabricate a dense `dim_date` calendar
    spine nobody specified) rather than forcing a mapping; `data-quality` diagnosed the
    `ship_region` null pattern as periodic (every 37th row) using only measured evidence, at a
    calibrated 0.6 confidence distinguishing the measured periodicity from the inferred mechanism;
    `data-validation` diagnosed the one discrepancy with an exact reconciling arithmetic check
    (missing-row amount exactly accounts for the aggregate delta) at 0.9 confidence; and no
    deployment was offered or performed anywhere in the chain. These are the parts of the toolkit
    that were supposed to hold up under genuine judgment, and did.

## Phase 3 addendum, follow-up (mine, need your review)

49. **Decision 45's three code bugs are fixed, per your direction.** All three confirmed and
    resolved:
    - `generate_pipeline_code.py`'s expectation keys are now `valid_grain_<col1>_<col2>...`
      (unique per uniqueness test) instead of a fixed `"valid_grain"` -- verified the collision is
      gone by generating expectations for a synthetic table with two uniqueness tests and checking
      both survive in the resulting dict.
    - A new template, `declarative_pipeline_full_refresh.py.tmpl` (a plain `@dlt.table`
      materialized view), is used whenever a `declarative_pipeline` target has no merge keys,
      instead of the merge_upsert template's `apply_changes(keys=[...])` -- which is invalid with
      an empty key list. Verified against both a synthetic full_refresh spec and a real
      calendar-shaped target (no natural merge key, same shape as the sign-off run's `dim_date`).
    - `build_pipeline_manifest.py`'s mock-data filenames and `row_counts_by_table` keys are now
      qualified by target table (`<target>__<source_schema>.<source_table>.json`), not source
      table alone -- verified two targets sharing one source object now produce two distinct mock
      files.

    Three new regression checks added to `skills/data-pipeline/evals/run_assertions.py` (now 39
    checks, up from 33); full suite re-run clean, along with every other skill's suite, the linter,
    every example artifact, and `fixtures/integration_check.py` -- nothing else regressed. Also
    fixed the smaller `fixtures/README.md` doc inaccuracy from decision 47's list while in the
    area (the type-mismatch flaw does not surface as a `data-validation` discrepancy; corrected the
    claim that it does).

50. **Decisions 46's schema-extension items (transformation/SCD-type not surviving into generated
    pipeline code) remain an open, documented limitation, per your direction -- not implemented
    this session.** `references/declarative-pipelines.md` and `references/pyspark-notebook.md`
    already carry inline warnings about this (the `sequence_by` TODO, the "does not re-litigate
    mapping confidence" language); no further doc changes made beyond what decision 46 already
    recorded. Worth scoping properly as dedicated work before a real engagement generates
    production pipeline code from a contract with non-trivial transformations or SCD-2 dimensions
    -- both are common, not edge cases, in real Kimball designs.

## Post-Phase-3 -- packaging as a Claude Code plugin (mine, need your review)

51. **Packaged the toolkit as a Claude Code plugin (`.claude-plugin/plugin.json`) and removed the
    cwd-dependency every `SKILL.md` had.** You asked what it would take to install this toolkit via
    CLI into an existing project repo. The real blocker turned out to be architectural, not
    procedural: every `SKILL.md` told the agent to run commands like `python scripts/validate_artifact.py`
    and referenced `contracts/*.schema.json`, assuming the working directory was the toolkit repo
    root -- true when running this toolkit standalone, false the moment it's installed inside
    someone else's repo where the cwd is the project root. The five Python scripts themselves were
    already fine (they resolve `lakehouse_adapter.py` via `Path(__file__).resolve().parents[3]`, not
    cwd), so this was purely a `SKILL.md` prose problem, not a code problem.

    Fixed by adding `.claude-plugin/plugin.json` at the repo root (making this repo a real Claude
    Code plugin, `name: data-ai-skill-toolkit`) and rewriting every executable path reference across
    the five `SKILL.md` files to use two Claude Code path-substitution variables instead of bare
    relative paths: `${CLAUDE_PLUGIN_ROOT}` for the toolkit-root shared paths (`scripts/`,
    `contracts/`) and `${CLAUDE_SKILL_DIR}` for each skill's own bundled scripts
    (`skills/<name>/scripts/`). Both substitute inline in skill content regardless of cwd or install
    location. Verified this was doc-only and touched nothing the scripts/CI depend on: regenerated
    fixtures, re-ran all five skills' full eval suites (unchanged pass counts) and
    `fixtures/integration_check.py` (15/15) after the edit -- all clean, since neither the eval
    harnesses nor the scripts themselves ever parse or execute `SKILL.md` text.

    Documented two real install paths in `README.md`'s new "Installing as a plugin" section: (a)
    vendor the toolkit into one repo at `.claude/skills/data-ai-skill-toolkit/` (a `.claude-plugin/plugin.json`
    there is auto-discovered by Claude Code with no install step -- this is the fast path you're
    about to test), or (b) publish a real marketplace plugin (`.claude-plugin/marketplace.json`,
    not built yet) for `/plugin install` reuse across many client repos without re-vendoring a copy
    into each one. Flagging one thing to confirm: the existing "Pinning" section's submodule example
    used `.toolkit/data-ai-skill-toolkit` as the destination, which is fine for keeping a version
    pinned but does NOT make Claude Code auto-load the skills -- only `.claude/skills/<name>/` or
    `~/.claude/skills/<name>/` are auto-discovered. I added a cross-reference note rather than
    changing the Pinning section's default, since a project might legitimately want the toolkit
    pinned somewhere else and separately symlinked/subtreed into `.claude/skills/`. Say if you'd
    rather the Pinning section's own example just point straight at `.claude/skills/` to avoid the
    two-step story.

## Post-Phase-3 -- fast path retracted, marketplace built (mine, need your review)

52. **The "fast path" from #51 doesn't work; replaced it with a real marketplace.** You tested #51's
    recommended fast path in an actual target project: vendor the plugin bundle into
    `.claude/skills/data-ai-skill-toolkit/` and let Claude Code auto-discover the nested
    `.claude-plugin/plugin.json`. Result, from your own `/reload-skills` output: none of the five
    skills loaded -- only unrelated pre-existing skills showed up in the active list. Root cause:
    Claude Code's directory auto-discovery expects a bare `SKILL.md` directly at
    `.claude/skills/<name>/SKILL.md`; it doesn't reliably walk into a nested plugin bundle whose own
    skills live a level deeper at `.claude/skills/<plugin-name>/skills/<name>/SKILL.md`. So the
    "fastest, no marketplace" story in #51 was wrong and has been retracted from `README.md` rather
    than left as a documented footgun.

    Built the marketplace that #51 deferred: added `.claude-plugin/marketplace.json` at the repo root,
    a minimal self-hosting entry (`source: "./"`) that lists this repo's own plugin, so the same repo
    is both the plugin and the marketplace that distributes it -- no separate marketplace repo needed
    for a single-plugin toolkit like this one.

    Rewrote `README.md`'s "Installing as a plugin" section: the marketplace flow
    (`/plugin marketplace add` + `/plugin install`) is now the primary path, with a team-reproducible
    variant (`extraKnownMarketplaces` + `enabledPlugins` pinned via `ref` in the target project's
    `.claude/settings.json`) replacing what the submodule-vendor fast path was trying to achieve. The
    old `.claude/skills/`-vendoring recommendation is called out explicitly as not working, and a
    flatten-for-short-names alternative (copy `skills/<name>/` directly into a project without
    `plugin.json`) is documented as a fallback for anyone who doesn't want the plugin namespace, with
    its `${CLAUDE_PLUGIN_ROOT}`/`${CLAUDE_SKILL_DIR}` caveat spelled out.

    Not yet verified end-to-end against a real target project (install, restart, confirm all five
    skills appear namespaced as `/data-ai-skill-toolkit:data-discovery` etc., and that a skill's
    bundled script still resolves `contracts/`/`scripts/` correctly under the marketplace-install
    path) -- do that next before treating this as closed.

*(Further decisions will be appended here as later phases proceed.)*

# End-to-End Prompting Workflow: Source Profiling to Databricks Deployment

A script for a new user's *first* full pass through this toolkit -- from "here's a database I've
never looked at" to "this star schema is running in my Databricks workspace." Each step is a
prompt you type to Claude Code (the toolkit is a Claude Code plugin -- see `README.md` "Installing
as a plugin" if you haven't installed it yet), followed by what it does, what you get, and what to
check before moving on.

This is a *guide*, not a script to paste verbatim -- swap in your own source, schema, and table
names. The prompts below are natural language on purpose: every skill triggers automatically by
matching your request against its `description` (see `references/toolkit-conventions.md` #9), the
same way it would for a real engagement. You can also address a skill explicitly
(`/data-ai-skill-toolkit:data-discovery ...`) if you want to be unambiguous about which one fires.

**Before any of this touches a real system**, make sure `toolkit.yaml` exists in your project
(copied from `toolkit.example.yaml`) with `environment`/`external_sources` filled in for your
engagement -- every skill halts with a named missing key rather than guessing, so an empty
`toolkit.yaml` is a fast, safe way to find out what it still needs.

**Practice risk-free first.** Every skill also runs against the toolkit's own bundled synthetic
fixture lakehouse (`environment.backend: sqlite_fixture` in `toolkit.yaml`, built by
`python fixtures/generate_fixtures.py`) -- no real workspace, no real source system, no risk. The
running example in this guide (a SQL Server CRM landing through bronze/silver into a customer/
product/sales-order star at gold) mirrors exactly what `skills/data-pipeline/evals/fixtures/`
already ships worked examples for, so you can cross-reference real output at every phase.

## The path at a glance

```
profile (data-discovery)
   |
   v
bronze ingestion (data-pipeline: lakeflow_connect)
   |
   v
silver refinement (data-discovery + data-pipeline: declarative_pipeline)
   |
   v
gold modeling (data-modeling -> data-discovery resolution -> data-pipeline)
   |
   v
deploy (data-deploy for the bronze connector + a human/CI bundle deploy for everything)
```

Two validators, **data-quality** and **data-validation**, attach at fixed points once a layer is
*actually deployed and populated* -- not part of the numbered phases below (they check data that
exists, and nothing exists until something is deployed), but called out at the point in the
narrative where you'd realistically run them. See "Recommended checkpoints" near the end.

---

## Phase 1 -- Profile the source

**Skill: `data-discovery`, greenfield mode, pre-ingestion.** You haven't ingested anything yet --
this profiles the source system directly (read-only, no writes, ever) and proposes what a bronze
landing contract should look like.

### Prompt 1.1

> We're onboarding a new source: the `SalesLT` schema in our Azure SQL Database (SQL Server). I
> want to land `Customer`, `Product`, `ProductCategory`, `ProductModel`, `SalesOrderHeader`, and
> `SalesOrderDetail` into bronze with no transformation. Can you profile these and tell me what a
> bronze data contract should look like?

**What happens:** `data-discovery` runs the cost/blast-radius gate first (estimates rows/bytes
before touching anything full-scale) and, if that clears, profiles each table via `SqlServerAdapter`
-- declared constraints, column types, null rates, candidate keys, declared FK orphan rates. This
is pure measurement; nothing here is guessed. See `skills/data-discovery/references/sqlserver-profiling.md`.

**What you'll get:** `discovery_findings.json` (the deterministic profiling output) and a proposed
`data-contract.json` targeting `bronze.customer`, `bronze.product`, etc. -- 1:1 columns, `source.object`
addresses like `AdventureWorksLT.SalesLT.Customer`, and tests derived from what was actually
observed (a declared PK becomes a `uniqueness` test at `explicit_constraint` confidence, not a
guess).

**If it halts:** either the cost estimate exceeded `toolkit.yaml`'s threshold (it'll show you the
estimate and ask before proceeding with `--force`) or a table's grain genuinely couldn't be
determined (it will NOT ship a contract with a guessed grain -- that's the one non-negotiable halt
in this skill). Read the reason before doing anything else.

### Prompt 1.2 (only if something looks off)

> `SalesOrderDetail`'s `ProductID` orphan rate came back non-zero -- can you tell me more about
> that before we land it?

**What happens:** this is normal follow-up conversation, not a new skill invocation -- the agent
already has the findings and can explain what it measured (which rows are orphaned, against which
reference table) without re-scanning anything.

---

## Phase 2 -- Review and approve the bronze contract

This is **human review gate 1** (`references/toolkit-conventions.md` #7): a contract with any
`llm_inferred` mapping is a proposal until a human has looked at it. Nothing downstream should
happen on a contract you haven't actually read.

### Prompt 2.1

> I've reviewed the bronze data contract for `SalesLT` -- the mappings and tests look right. Go
> ahead and treat this as approved for pipeline generation.

**What happens:** nothing script-driven -- this is you, the human, closing the loop `data-discovery`
opened. There's no field this sets (that field belongs to `data-pipeline`'s own approval, later);
this is the conversational green light to move to the next skill at all.

**Before moving on:** actually read the contract first. Check `assumptions[]` for anything
`llm_inferred` and its confidence score, and check that every test's `threshold_basis` is something
you'd defend (`explicit_constraint`/`profiled_unique_key` are measured; `default_convention` means
there was no evidence and a toolkit default was used).

---

## Phase 3 -- Generate and approve the bronze ingestion pipeline

**Skill: `data-pipeline`.** SQL Server is a Lakeflow Connect managed-connector source and this is a
pure landing operation (no reshaping) -- the modality rubric should land on `lakeflow_connect`
automatically.

### Prompt 3.1

> Here's the approved bronze contract for the SalesLT tables. Generate the pipeline.

**What happens:** `data-pipeline` classifies `source_is_managed_connector: true`,
`target_layer: bronze` -> `lakeflow_connect` (see `references/decision-rubric.md`). Since Lakeflow
Connect ingestion is configured, not hand-coded, it renders a `connector_config.yaml` stub per
table (connector type, source object, destination, sync schedule) rather than PySpark/SQL.
`idempotency_check.result` is `not_applicable` -- Lakeflow Connect manages its own CDC state, not
something this toolkit tests locally.

**What you'll get:** one `pipeline-manifest.json` covering all six targets, `readiness_level:
validated`, `deployment: null`. The response should explicitly ask whether/where to move toward
deployment -- it will not offer to deploy proactively.

### Prompt 3.2 -- naming the target explicitly

> Yes -- approve `Customer` for deployment.

**What happens:** *only* because this names a specific target -- and, critically, names it using
the EXACT `table_name` the manifest itself uses (`Customer`, not "the SalesLT ingestion" or any
other paraphrase) -- does `data-pipeline` update `deployment` (`approved: true`, `approved_by`,
`target_named: "Customer"`, `approved_at`) and advance `readiness_level` to
`approved_for_deployment`. **This is the one prompt in this whole workflow where exact wording
matters most**: `deployment.target_named` is a single string that must match one real target, never
a description of several ("never a blanket approval," per `contracts/pipeline-manifest.schema.json`)
-- see "Common pitfalls" below, and Phase 6, for why this bites you later if you're vague here.

**One target at a time, by design.** This manifest covers all six SalesLT tables, but each gets its
own named approval and, in Phase 6, its own `data-deploy` run -- a realistic way to work anyway
(land `Customer` first, confirm the pattern works end to end, then repeat "approve `Product`,"
"approve `ProductCategory`," etc. for the rest). The rest of this guide follows `Customer` through
to deployment; treat the others as the same steps repeated.

---

## Phase 4 -- Refine bronze into silver

Bronze is raw. Silver is curated: deduplicated, conformed types, business-meaningful naming --
exactly what `data-modeling`'s `silver_verification` gate will check for later, so it's worth doing
properly here. This phase runs `data-discovery` and `data-pipeline` again, now pointed at the
lakehouse itself (`databricks_connect` backend) instead of the external SQL Server.

### Prompt 4.1

> Now that `bronze.customer` and `bronze.sales_order_header`/`bronze.sales_order_detail` are
> landing, help me design `silver.customer`, `silver.sales_order_header`, and
> `silver.sales_order_line` -- deduplicated on the natural key, standardized column names, correct
> types.

**What happens:** `data-discovery` greenfield mode again, this time profiling the bronze objects
*in the lakehouse*. It measures duplicate natural keys, null rates, and type mismatches (a bronze
column landed as `TEXT` that should really be `DECIMAL`, the toolkit's own fixture lakehouse plants
exactly this on purpose) and proposes a silver contract with tests that would catch a regression.

### Prompt 4.2

> This silver contract looks right -- approved.

Same human gate 1 as Phase 2, now for the silver layer.

### Prompt 4.3

> Generate the pipeline for `silver.customer`, `silver.sales_order_header`, and
> `silver.sales_order_line`.

**What happens:** bronze-to-silver reshaping with no external system involved ->
`source_is_managed_connector: false` -> `transform_complexity: simple_declarative` (straight
mapping/cast/dedup logic) -> `declarative_pipeline`, the default for exactly this kind of medallion
reshape. This time the local idempotency proof actually runs (synthetic mock data, a real SQLite
merge, twice, diffed) -- `idempotency_check.result` should come back `match`.

### Prompt 4.4

> Approved for deployment, targeting `silver_customer_refinement`.

Same shape as Prompt 3.2 -- name the one specific pipeline this run actually produced (not a
description of the whole silver layer), and repeat for `silver_sales_order_header_refinement`/
`silver_sales_order_line_refinement` once each is ready.

---

## Phase 5 -- Model and build gold

This is the one phase that runs three skills in sequence: **data-modeling** designs the star
schema, **data-discovery** (resolution mode) grounds it against the real silver objects from Phase
4, and **data-pipeline** builds it.

### Prompt 5.1 -- design

> I need a star schema for sales analysis: a `dim_customer`, a denormalized `dim_product` (it
> should roll up product, product category -- including a parent category one level up -- and
> product model into one wide dimension), and a `fact_sales_order_line` at the order-line grain,
> with customer/address/order-number attributes rolled down from the order header, and a date key
> resolved against an existing `dim_date`. Business need: sales performance and margin analysis by
> product hierarchy and customer.

**What happens:** `data-modeling` verifies `silver.customer`/`silver.product`/
`silver.sales_order_header`/`silver.sales_order_line` are genuinely curated (structure, not data
quality -- see `references/silver-verification.md`), then designs the fact/dimension grain,
measure additivity, SCD types, and -- this is the newest capability in the toolkit -- the actual
**join structure** for `dim_product` (a snowflake denormalization, with `product_category` joined
twice: once directly, once more via `ParentProductCategoryID` for the parent category) and
`fact_sales_order_line` (the header-to-line rollup, plus the `dim_date` surrogate-key lookup). See
`skills/data-modeling/references/kimball-concepts.md` "Multi-source facts and dimensions," and the
real worked examples this design mirrors under `skills/data-pipeline/evals/fixtures/
denormalizing-dimension-join-contract.json` / `header-rollup-dimension-lookup-contract.json`.

**What you'll get:** `model-spec.json` plus a human-readable `design_rationale.md`.

### Prompt 5.2 -- approve the design (human gate 2)

> The star schema looks right -- grain, SCD choices, and the product/fact joins all make sense.
> Approved to move to discovery.

**Human review gate 2**: dimensional design decisions are business decisions wearing a technical
hat -- discovery should resolve an *approved* design, never a draft one.

### Prompt 5.3 -- resolve

> Here's the approved model spec for the sales star schema -- resolve it against the real silver
> objects and produce the data contract.

**What happens:** `data-discovery` resolution mode matches every `source_to_target_mappings` entry
(and the newly designed `source_joins` for `dim_product`/`fact_sales_order_line`) against real
profiled silver columns, carrying the join declarations through into `data-contract.json`
unchanged -- discovery grounds the design in reality, it doesn't re-decide the join. Anything it
can't resolve becomes an `unresolved_requirements[]` entry, never a silent drop.

### Prompt 5.4 -- approve the gold contract (human gate 1, again)

> This resolved contract looks right -- approved for pipeline generation.

### Prompt 5.5 -- build

> Generate the pipeline for `dim_customer`, `dim_product`, and `fact_sales_order_line`.

**What happens:** all three are still `simple_declarative` -> `declarative_pipeline`, even though
two of them are genuine multi-table joins -- a plain equality-condition lookup join classifies the
same as any other reshape (see `references/decision-rubric.md`'s worked example). The generated
code renders a real `.join()` chain, correctly aliased so `product_category`'s two occurrences
never collide. `idempotency_check.result` for `dim_customer` should be `match`; for the two
multi-source targets it will honestly come back `not_applicable` -- the local proof doesn't yet
synthesize mock data across multiple joined tables (a documented, deliberate v1 scope limit, not a
bug -- see `references/idempotency-and-mock-data.md`). The real generated Spark code is unaffected.

### Prompt 5.6 -- approve for deployment

> Approved for deployment, targeting `dim_customer`. I'll approve `dim_product` and
> `fact_sales_order_line` separately once I've reviewed each.

Same principle as every prior approval in this guide: one specific, real target per approval, even
when three targets came out of the same `data-pipeline` run.

---

## Phase 6 -- Deploy to Databricks

This is where the toolkit's own boundary matters most: **every skill in this toolkit generates;
none of them execute a deploy.** Two different things happen here, and they use different
mechanisms.

### Prompt 6.1 -- render the bronze connector's Asset Bundle resources

> The `Customer` target in the SalesLT pipeline manifest is approved for deployment. Generate the
> Databricks Asset Bundle resources for it -- name the connection
> `adventureworks_sqlserver_prod_connection`.

**What happens:** `data-deploy` -- the sixth skill, and the only one downstream of `data-pipeline`
-- checks that the source pipeline manifest is genuinely `approved_for_deployment` with
`deployment.target_named` set (refusing outright otherwise), confirms `target_named` (`Customer`)
matches a real target in the manifest, resolves `sql_server` to the real Lakeflow Connect connector
type and its Unity Catalog connection option shape, and renders `uc_connection.yml` (documented,
create the connection once via `databricks connections create` or Terraform) and
`ingestion_pipeline.yml` (a genuine Asset Bundle `resources.pipelines` resource) for `Customer`.
Every OTHER target in that manifest (`Product`, `ProductCategory`, ...) shows up in
`deployment-manifest.json`'s `targets[]` as `skipped: true` with a reason -- never silently
dropped, just not yet approved. Once `Product` gets its own Prompt-3.2-style approval, run this
same prompt again naming `Product` to process it too.

**What you'll get:** `deployment-manifest.json`, `readiness_level: validated`, `deployment: null`.
Same as every other skill, the response asks whether/where to actually deploy -- it does not offer.

### Prompt 6.2 -- the second, separate approval

> Yes, run `databricks bundle deploy` for the `Customer` connector -- approved.

**What happens:** `data-deploy` records this as a **second, separate** approval from the one in
Phase 3 -- generating bundle resources and actually deploying them are different actions with
different blast radius (see `skills/data-deploy/references/approval-gate.md`). Even now, no script
in this skill runs `databricks bundle deploy`, calls any Databricks API, or creates the connection
-- that boundary doesn't move no matter what you approve. This is deliberate, not a missing
feature: see the "Match or depart?" reasoning in that same reference doc.

### Manual step -- wire the silver and gold code into the same bundle project

`data-deploy` only covers the bronze Lakeflow Connect connector -- Lakeflow Connect is the only
managed-ingestion modality it knows how to turn into bundle resources. The silver and gold
Declarative Pipeline code from Phases 4 and 5 (`declarative_pipeline.py` + `expectations.py` per
target) isn't a Lakeflow Connect resource at all; it's your Databricks Asset Bundle project's own
Lakeflow Declarative Pipeline definition, and wiring generated files into an existing project's
`databricks.yml`/pipeline definition is exactly the kind of client-project write no skill in this
toolkit performs (`references/toolkit-conventions.md` #1) -- a human does this step, copying the
generated files from `output_dir` into the target project and adding an `include:` entry if one
doesn't already exist (see `skills/data-deploy/references/asset-bundle-resources.md`).

### Final step -- actually deploy (outside the toolkit entirely)

```
databricks bundle validate
databricks bundle deploy
```

Run by you or your CI/CD pipeline, never by this toolkit. This is the moment the bronze connector,
the silver refinement pipeline, and the gold star schema all actually go live.

---

## Recommended checkpoints: data-quality and data-validation

These don't fit the numbered phases above because they check data that has to already exist --
run them **after** each phase's `databricks bundle deploy` has actually executed against real data,
not before.

> Now that bronze has actually landed, run quality checks against `bronze.customer` using the
> tests already proposed in its data contract.

**Skill: `data-quality`.** Runs the contract-derived checks (`derive_checks_from_contract.py`) for
real against the now-populated table and diagnoses any failure/warning with a confidence-scored
root cause -- this is how a contract's *proposed* tests actually get enforced on a schedule instead
of sitting unused in a JSON file.

> Now that silver has run, compare it against bronze to confirm nothing was dropped or duplicated
> during refinement.

**Skill: `data-validation`.** Staged, deterministic source-vs-target comparison (row counts, then a
full content hash, then column aggregates, then a bounded row-level diff) -- this is the tool for
"does the target actually match the source," never a single-object quality question.

---

## Common pitfalls

| Don't say | Why it doesn't work | Say instead |
|---|---|---|
| "Looks good, ship it." | No specific target named -- every deployment gate in this toolkit (data-pipeline, data-deploy) refuses a blanket approval and asks which target you mean. | "Approved for deployment, targeting `<the specific job/object/connection name>`." |
| "Go ahead and deploy it." (to `data-deploy`) | Generating bundle resources and running `databricks bundle deploy` are two separate approvals -- see Phase 6. Naming a target for one doesn't imply approval for the other. | Approve each step explicitly, by name, when you actually mean it. |
| "Just pick whichever source table looks right." | `references/toolkit-conventions.md` #6 -- no skill in this toolkit silently infers when it's genuinely ambiguous; expect a clarifying question instead of a guess. | Answer the question, or say which one you mean up front. |
| "Can you generate the pipeline and check if the data's right?" in one breath | `data-pipeline` generates code; `data-quality`/`data-validation` check data that's actually been deployed and run. Nothing exists to check yet right after generation. | Ask for generation, deploy, THEN ask for the quality/validation check. |

## Where to learn more

- `README.md` -- installation, versioning, the full artifact-chain design.
- Each skill's own `SKILL.md` (`skills/<name>/SKILL.md`) -- the authoritative, current source for
  exactly what that skill does; this guide summarizes, `SKILL.md` is the spec.
- `references/toolkit-conventions.md` -- the cross-cutting rules (read/write boundaries, secrets,
  cost gates, the human review gates referenced throughout this guide) every skill follows.
- `DECISIONS.md` -- why the toolkit is shaped the way it is, including decision 60 (multi-source
  joins, exercised in Phase 5) and decision 59 (the two-approval-gate pattern, exercised in Phase 6).

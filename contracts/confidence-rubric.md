# Confidence Rubric

Every skill in this toolkit that emits a confidence score references this file rather than inventing its own scale. A confidence score with no shared rubric is a number that looks rigorous and isn't -- it lets one skill's 0.7 mean something different from another's, and nobody downstream can tell.

This rubric applies to any field with a `confidence` property in any contract artifact: mapping confidence in a data contract, grain determination confidence, SCD rationale confidence, quality/validation diagnosis confidence.

## The scale

Confidence is a function of the **evidence class** behind a claim, not a gut feeling. Pick the evidence class first; the score follows from it. Do not assign a score without being able to name which row of this table you're in.

| Score band | Evidence class | Example |
|---|---|---|
| 0.95 - 1.0 | Explicit, enforced constraint read directly from the system | A declared `FOREIGN KEY` constraint; a declared `PRIMARY KEY` / `UNIQUE` constraint; a `NOT NULL` constraint. The system itself guarantees the property. |
| 0.75 - 0.94 | Explicit but unenforced declaration | A documented comment/description on a table or column stating a relationship or grain; a naming convention *and* matching type *and* a profiled uniqueness/non-null check that actually passed against the data (not just plausible-looking). |
| 0.5 - 0.74 | Name-and-type match, profiled but not fully confirmed | Column names and types match a plausible mapping (e.g. `customer_id` INT on both sides) and a sample profile is consistent with it, but no constraint or documentation confirms it, and the profile did not exhaustively check the full population. |
| 0.2 - 0.49 | LLM inference from naming/structure alone, unconfirmed by any profiling | The model proposes a mapping, grain, or relationship purely from column names, table names, or general domain knowledge, with no supporting profiled evidence. This is the default band for greenfield discovery's first pass before profiling backs it up. |
| < 0.2 | Speculative / contradicted by weak signal | The inference is offered only because *something* must be proposed (e.g. no plausible source column exists at all), and the model flags this as very likely wrong. Should usually accompany a `halt` or `ask` decision instead of shipping a low-confidence guess silently. |

## What moves a score within a band

Within a band, adjust for:
- **Corroboration**: two independent weak signals (e.g. name match *and* a plausible type *and* a non-authoritative comment) can justify sitting at the top of the 0.5-0.74 band rather than the bottom.
- **Population coverage of profiling**: a uniqueness check against a full column scan sits higher than the same check against a sample.
- **Recency**: profiling evidence gathered in *this* run outranks evidence cached from a prior run when sources may have changed.

## What a downstream reader should do with a score

- **>= 0.75**: Treat as reliable enough to proceed without a human re-check, though the human review gates in `references/toolkit-conventions.md` still apply at their fixed points regardless of confidence.
- **0.5 - 0.74**: Flag for human review before anything downstream (pipeline generation, remediation) consumes it.
- **< 0.5**: Should already be flagged by the producing skill's own grounding-and-failure-behavior rule (halt, or ask a clarifying question) rather than passed downstream silently. If it does appear in an artifact, treat it as a hypothesis, not a fact.

## Non-negotiable rule

Every artifact field that carries a `confidence` value must also carry a `basis` string explaining, in plain language, which evidence class applied and what the specific evidence was (e.g. "profiled: 0 nulls and 100% distinct across full scan of 4.2M rows" or "llm_inferred: column name `cust_id` matches naming convention used elsewhere in schema, no constraint or comment found"). A score without a basis is not usable and validate_artifact.py's semantic checks (beyond bare schema validity) should flag it.

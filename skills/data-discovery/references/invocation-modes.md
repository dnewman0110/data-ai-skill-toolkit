# Invocation modes: greenfield vs. resolution

Discovery has exactly two modes. Getting this distinction right is the single biggest source of
ambiguity in this skill's design -- treat "which mode am I in" as the first question you answer,
before running anything.

## Greenfield

**Input**: prose business intent. "We need to report order revenue by region." "Find me customer
churn signals." No prior artifact exists.

**What to do**: explore broadly. Start from `list_tables` on the schemas your target catalog
config points at (typically `silver`, occasionally `gold` if you're checking whether something
already exists). Look for tables whose names, comments, and columns plausibly relate to the
business intent. Profile the plausible candidates -- you don't need to profile every table in the
schema, but don't stop at the first table with a matching name either; if two tables both look
like "the" customer table, profile both and let the findings (row counts, comments, data shape)
inform which one you propose.

**Mapping confidence**: every column mapping you propose is grounded in one of these, and the
mapping's `mapping_type`/`confidence` should say which:
- An `explicit_alias`: the source column is unambiguously the target concept (exact name match,
  or a documented comment that states the relationship). No confidence score needed -- this is
  measured, not inferred.
- A `name_and_type_match`: plausible by name and type, and profiling doesn't contradict it (values
  look right), but nothing documents the relationship explicitly.
- An `llm_inferred` mapping: you're proposing this from naming/domain knowledge alone. Score it
  per `contracts/confidence-rubric.md` (typically the 0.2-0.49 band for a first pass; corroborate
  with profiling evidence to justify sitting higher in that band, per the rubric's "what moves a
  score within a band" section).

**When to halt vs. ask vs. flag**: if business intent names a concept ("revenue") that has no
plausible source at all after reasonable exploration, halt and say so -- don't invent a mapping to
a column that merely sounds close. If there are two *equally* plausible candidates and picking
wrong would silently produce a wrong contract, ask a single clarifying question rather than
guessing. If a candidate is plausible but not certain, ship it as `llm_inferred` with an honest
confidence score and let the human review gate catch it.

## Resolution

**Input**: a `model-spec.json` produced by `data-modeling` (already reviewed and approved by a
human per that skill's own review gate -- see `references/toolkit-conventions.md` #7). The target
design already exists: facts, dimensions, grain, SCD strategy, source-to-target mappings are all
specified. Your job is narrower and more mechanical than greenfield: **resolve the spec against
real objects, don't redesign it.**

**What to do**: for every `source_to_target_mappings` entry (facts) and `source_mapping` entry
(dimension attributes) in the model-spec, go directly to that named source object and column and
profile it. Don't explore the rest of the schema looking for something "better" -- the design
decision was already made and reviewed. If the named source object or column doesn't exist, or
exists but doesn't match the expected shape (wrong type, doesn't support the stated grain), that
goes in `unresolved_requirements[]` with a specific reason. **Never silently drop a requirement
you can't satisfy** -- an incomplete contract that says so clearly is far more useful than a
contract that quietly omits the field a report depends on.

**Grain**: the model-spec already states grain and (for facts) validates it against measures.
Your job is confirming the *source* data actually supports that grain (profiled uniqueness check
on the mapped source columns at the stated grain), not re-deciding what the grain should be. If
profiling contradicts the spec's grain (e.g. the mapped source key isn't actually unique), that's
a significant finding -- surface it prominently in `assumptions[]` and lean toward halting rather
than shipping a contract whose grain the source data doesn't support.

**Mapping confidence in resolution mode**: mappings you resolve directly from the spec's own
`source_to_target_mappings`/`source_mapping` are `explicit_alias` (the spec already told you the
mapping; you're confirming it against real data, not inferring it) -- confidence isn't needed
here, since this isn't an inference. Confidence only enters resolution mode if you have to fill a
gap the spec left open (rare, and usually a sign to flag the gap rather than infer past it).

## Telling the two apart when it's ambiguous

If you're handed both a business-question-shaped prompt *and* a `model-spec.json` path, you're in
resolution mode -- the model-spec takes precedence, and the business questions are context for
*why* the design looks the way it does, not a license to re-explore broadly. If you're handed only
prose with no model-spec, you're in greenfield mode, even if the prose is very specific and sounds
like it's describing a star schema -- discovery doesn't design dimensional models (that's
`data-modeling`); if the prose reads like a modeling request rather than "what data do we have,"
say so and point at `data-modeling` instead of attempting it here.

# SCD type selection

`dimensions[].attributes[].scd_type` is an integer 0-3 per `contracts/model-spec.schema.json`, and
every attribute needs an `scd_rationale` explaining the choice -- not just a number.

- **Type 0**: never changes, or changes are ignored even if the source value does (e.g. an
  original signup date -- even if a source system correction fixes a typo'd date, the dimension
  keeps the originally-loaded value because historical reports already keyed off it).
- **Type 1**: overwrite in place, no history kept. Right for correction-only fields where past
  reports SHOULD reflect the corrected value (a misspelled name fix), and for attributes where
  nobody has a business reason to care what the old value was.
- **Type 2**: a new dimension row per change, with effective-dating, preserving full history. Right
  when historical reports need to reflect the value AS OF the time of the fact, not the current
  value -- e.g. sales commission recalculation needing a customer's region at the time of an order,
  not their region today.
- **Type 3**: a "previous value" column alongside "current value" -- limited history (one prior
  state), used when only the immediately-preceding value matters, not full history.

## How `detect_scd_candidates.py`'s evidence feeds in

A sibling history table (`<stem>_history`/`_hist`/`_scd` in the same schema, matched against the
source table's singular stem -- e.g. `customer_region_history` alongside `customers`) is real,
measured evidence that someone already built infrastructure to track a change over time for this
dimension. That's strong support for `scd_type: 2` on the specific attribute the history table
tracks (region, in that example) -- reflected in `scd_rationale` as something like: "type 2:
`silver.customer_region_history` exists and is comment-documented as needed for commission
recalculation attributing orders to the region at the time of the order."

What the evidence does NOT do: extend automatically to every attribute on the dimension. A history
table tracking region changes says nothing about whether `email` or `name` also needs type 2 --
those still get their own `scd_type`/`scd_rationale`, typically type 1 unless there's a comparable
signal or an explicit business requirement. And the ABSENCE of a history table doesn't prove an
attribute is safely type 1 either -- it might mean nobody has built that tracking yet for something
that genuinely needs it. Ask the business question (`business_context.business_questions`) before
defaulting an unclear attribute to type 1 just because there's no history table to point at.

## The history table itself doesn't need to pass silver_verification to count as evidence

`detect_scd_candidates.py` deliberately does NOT run `verify_silver_layer.py` against the history
table it finds -- it's evidence for a design decision about the DIMENSION's own attribute, not a
proposed direct source. A history table can fail curation (e.g. no declared primary key, common
for narrow effective-dated tracking tables that were bolted on later) and still be exactly right
as the reason to set `scd_type: 2` on an attribute of an already-verified dimension. Keep the two
questions separate: "should this attribute be type 2?" (the history table's existence and its
documented reason answer this) versus "can I directly source rows FROM this history table?" (that
still requires it to pass `verify_silver_layer.py` on its own, same as any other source object --
if the design ever needs to backfill historical dimension rows from it, verify it as a source at
that point, don't assume the SCD-evidence check already covered that).

# Conformed dimensions

A dimension is `conformed` when it's shared across multiple facts/stars and built once, with a
`conformance_group` identifier every referencing fact/star uses -- the Kimball bus architecture
idea that "customer" should mean the same thing (same keys, same attributes, same grain) whether
you're looking at the orders star or the support-tickets star.

## What `derive_conformance_candidates.py` checks

Scans the target gold schema for tables shaped like existing dimensions (`dim_<name>` /
`<name>_dim` naming) whose normalized name matches a proposed dimension's name (e.g. proposing
`customer` matches an existing `dim_customer` or `customer_dim`). This is a real, deterministic
name-matching signal -- but it is exactly that: a NAME match, not a verified guarantee the existing
table's grain, keys, and attributes actually line up with what the new star needs.

## Why the agent still confirms, rather than the script asserting conformance

A matching name is a strong hint to go look, not proof of compatibility. Before setting
`kind: conformed` and pointing `conformance_group` at an existing dimension, confirm its grain
matches what the new fact needs (an existing `dim_customer` at customer-day grain is NOT the same
dimension as one needed at customer grain) and that its attribute set covers what the new star
actually references. If it's a genuine match: reuse it, set `conformance_group` to the existing
identifier, and do not redesign attributes it already has. If the name matches but the grain or
attributes don't actually line up: it's `local`, not `conformed` -- a naming coincidence isn't a
reason to force two different concepts to share one dimension table. Record which you found and
why in `design_rationale.md`, not just in the terse `model-spec.json` fields.

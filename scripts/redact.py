#!/usr/bin/env python3
"""
redact.py -- sample-record redaction, shared by every skill that emits example/sample rows
(data-discovery, data-quality, data-validation) or sends row content to an LLM for diagnosis.

references/toolkit-conventions.md #3: sample size is capped, sensitive columns are redacted or
hashed per toolkit.yaml's `sample_data.sensitive_columns`, and a sample value never ends up in a
filename or URL. This module is the one place that logic lives.

Config shape expected (from toolkit.yaml's sample_data block):
    sensitive_columns:
      - pattern: "(?i)ssn|social_security"
        action: redact
      - pattern: "(?i)email"
        action: hash
    max_sample_records: 20
"""
import hashlib
import re

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def normalize_column_name(column_name: str) -> str:
    """Patterns in toolkit.yaml are written snake_case (`card_number`). Source systems don't all
    name columns that way -- normalize camelCase/PascalCase/kebab-case to snake_case before
    matching so e.g. `cardNumber` or `Card-Number` still hits the `card_number` pattern instead of
    silently passing through unredacted.

    Public (no leading underscore): data-pipeline's build_transform_spec.py imports this to match
    the same sensitive-column patterns against real target columns, so "is this column PII" is
    computed identically whether the result drives sample redaction or real-data transform.
    """
    return _CAMEL_BOUNDARY.sub("_", column_name).replace("-", "_").lower()


def _column_action(column_name: str, sensitive_columns: list[dict]) -> str | None:
    normalized = normalize_column_name(column_name)
    for rule in sensitive_columns:
        if re.search(rule["pattern"], normalized):
            return rule["action"]
    return None


def redact_value(value, action: str):
    if value is None:
        return None
    if action == "redact":
        return "[REDACTED]"
    if action == "hash":
        return "sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
    raise ValueError(f"Unknown redaction action '{action}' -- expected 'redact' or 'hash'.")


def redact_rows(rows: list[dict], sensitive_columns: list[dict], max_records: int = 20) -> list[dict]:
    """Applies column-level redaction/hashing and caps the number of rows returned. Safe to call
    on rows that contain no sensitive columns at all -- returns them capped but otherwise as-is.
    """
    capped = rows[:max_records]
    out = []
    for row in capped:
        clean = {}
        for col, val in row.items():
            action = _column_action(col, sensitive_columns)
            clean[col] = redact_value(val, action) if action else val
        out.append(clean)
    return out


def redact_for_llm_prompt(rows: list[dict], sensitive_columns: list[dict], max_records: int = 20) -> list[dict]:
    """Same redaction, used specifically before rows are included in a prompt sent to an LLM for
    diagnosis. Kept as a distinct entry point (even though it currently delegates to redact_rows)
    so a future stricter policy for LLM-bound content -- e.g. redacting an additional column set,
    or preferring aggregates over rows entirely per toolkit-conventions.md #3 -- has an obvious
    single place to change without touching artifact-bound sampling.
    """
    return redact_rows(rows, sensitive_columns, max_records)

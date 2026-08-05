#!/usr/bin/env python3
"""
validate_artifact.py -- schema + semantic validation for toolkit artifacts.

Every skill validates its own output with this script before declaring success,
and CI runs it against every example under contracts/examples/. It is the one
place "does this artifact conform to the contract" is answered, so skills don't
each reimplement (and subtly diverge on) that check.

Usage:
    python scripts/validate_artifact.py <artifact.json> [options]

Options:
    --schema-type {data-contract,model-spec,quality-report,validation-report,run-manifest,pipeline-manifest}
        Which contracts/*.schema.json to validate against. If omitted, inferred
        from the artifact's own "schema_type" hint if present, else guessed from
        the filename (data-contract*.json, model-spec*.json, etc).

    --supported-major N
        The major schema_version this caller (a skill) understands. If the
        artifact's schema_version major component does not equal N, validation
        FAILS with a clear "unsupported schema major version" message rather
        than proceeding to structural checks -- this is the toolkit-wide rule
        that skills refuse artifacts whose major version they don't support
        instead of best-effort parsing them.

    --contracts-dir PATH
        Directory containing the *.schema.json files. Defaults to the
        contracts/ directory next to this script's repo root.

    --quiet
        Only print PASS/FAIL and, on failure, the error list.

Exit code 0 on pass, 1 on fail (schema violation, semantic violation, or
unsupported major version), 2 on usage/IO error.
"""
import argparse
import json
import sys
from pathlib import Path

try:
    import jsonschema
    from jsonschema import validators
    try:
        from jsonschema import RefResolver
    except ImportError:  # jsonschema >= 4.18 deprecates RefResolver in favor of referencing
        RefResolver = None
except ImportError:
    print("ERROR: this script requires the 'jsonschema' package. Install with:\n"
          "  pip install jsonschema --break-system-packages", file=sys.stderr)
    sys.exit(2)

SCHEMA_FILENAMES = {
    "data-contract": "data-contract.schema.json",
    "model-spec": "model-spec.schema.json",
    "quality-report": "quality-report.schema.json",
    "validation-report": "validation-report.schema.json",
    "run-manifest": "run-manifest.schema.json",
    "pipeline-manifest": "pipeline-manifest.schema.json",
    "deployment-manifest": "deployment-manifest.schema.json",
}


def guess_schema_type(artifact_path: Path, artifact: dict) -> str:
    if "schema_type" in artifact:
        return artifact["schema_type"]
    name = artifact_path.name.lower()
    for schema_type in SCHEMA_FILENAMES:
        if name.startswith(schema_type):
            return schema_type
    # Fall back to structural fingerprinting on required top-level keys.
    if "tables" in artifact and "invocation_mode" in artifact:
        return "data-contract"
    if "facts" in artifact and "dimensions" in artifact:
        return "model-spec"
    if "checks" in artifact and "diagnoses" in artifact:
        return "quality-report"
    if "stages" in artifact and "source" in artifact and "target" in artifact:
        return "validation-report"
    if "modality_decision" in artifact and "readiness_level" in artifact:
        return "pipeline-manifest"
    if "source_pipeline_manifest_ref" in artifact and "approval_gate" in artifact:
        return "deployment-manifest"
    if "skill" in artifact and "invoking_identity" in artifact:
        return "run-manifest"
    raise ValueError(
        f"Could not infer schema type for {artifact_path}. "
        "Pass --schema-type explicitly."
    )


def check_major_version(artifact: dict, supported_major: int, label: str) -> list:
    errors = []
    version = artifact.get("schema_version")
    if version is None:
        errors.append(f"{label}: missing required 'schema_version' field.")
        return errors
    try:
        major = int(str(version).split(".")[0])
    except (ValueError, IndexError):
        errors.append(f"{label}: schema_version '{version}' is not a valid semver string.")
        return errors
    if major != supported_major:
        errors.append(
            f"{label}: schema_version major component is {major}, but this caller only "
            f"supports major version {supported_major}. Refusing to consume this artifact "
            f"rather than best-effort parsing it -- regenerate the artifact with a "
            f"compatible skill version, or upgrade the consuming skill."
        )
    return errors


def semantic_checks(node, path="$") -> list:
    """Walk the artifact and enforce cross-cutting rules the JSON Schema alone
    can't express cleanly: any 'confidence' value must be paired with a
    non-empty 'basis'; anything with source == 'llm_inferred' must carry
    both confidence and basis.
    """
    errors = []
    if isinstance(node, dict):
        has_confidence = "confidence" in node and node["confidence"] is not None
        has_source_llm = node.get("source") == "llm_inferred"
        if has_confidence or has_source_llm:
            basis = node.get("basis")
            if not basis or not isinstance(basis, str) or not basis.strip():
                errors.append(
                    f"{path}: has a confidence score and/or source=llm_inferred but no "
                    f"non-empty 'basis' string. Every confidence score must be traceable "
                    f"to evidence per contracts/confidence-rubric.md."
                )
            conf = node.get("confidence")
            if conf is not None and not (0 <= conf <= 1):
                errors.append(f"{path}: confidence {conf} is out of the [0, 1] range.")
        for key, value in node.items():
            errors.extend(semantic_checks(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            errors.extend(semantic_checks(item, f"{path}[{i}]"))
    return errors


def validate(artifact_path: Path, contracts_dir: Path, schema_type: str = None,
             supported_major: int = None) -> list:
    with open(artifact_path) as f:
        artifact = json.load(f)

    if schema_type is None:
        schema_type = guess_schema_type(artifact_path, artifact)
    if schema_type not in SCHEMA_FILENAMES:
        raise ValueError(f"Unknown schema type '{schema_type}'. Choose from: {list(SCHEMA_FILENAMES)}")

    errors = []
    if supported_major is not None:
        errors.extend(check_major_version(artifact, supported_major, schema_type))
        if errors:
            # Unsupported major version -- stop here. Don't run structural
            # validation against a schema version we've already said we
            # don't understand.
            return errors

    schema_path = contracts_dir / SCHEMA_FILENAMES[schema_type]
    with open(schema_path) as f:
        schema = json.load(f)

    # Preload every schema in contracts/ so $ref resolution never touches the
    # network, regardless of whether $ref targets a bare filename or the
    # schema's declared $id (both are pre-registered in the local store).
    all_schemas = {}
    for fname in SCHEMA_FILENAMES.values():
        fpath = contracts_dir / fname
        with open(fpath) as sf:
            sub_schema = json.load(sf)
        all_schemas[fname] = sub_schema
        if "$id" in sub_schema:
            all_schemas[sub_schema["$id"]] = sub_schema

    validator_cls = validators.validator_for(schema)
    if RefResolver is not None:
        # jsonschema < 4.18
        resolver = RefResolver(base_uri=schema_path.resolve().as_uri(), referrer=schema, store=all_schemas)
        validator = validator_cls(schema, resolver=resolver)
    else:
        # jsonschema >= 4.18: referencing-based resolution, same preloaded set.
        from referencing import Registry, Resource
        resources = [(key, Resource.from_contents(val)) for key, val in all_schemas.items()]
        registry = Registry().with_resources(resources)
        validator = validator_cls(schema, registry=registry)
    for err in sorted(validator.iter_errors(artifact), key=lambda e: list(e.absolute_path)):
        loc = "$" + "".join(f"[{p!r}]" if isinstance(p, str) else f"[{p}]" for p in err.absolute_path)
        errors.append(f"{loc}: {err.message}")

    errors.extend(semantic_checks(artifact))
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("artifact", type=Path, help="Path to the JSON artifact to validate.")
    parser.add_argument("--schema-type", choices=sorted(SCHEMA_FILENAMES), default=None)
    parser.add_argument("--supported-major", type=int, default=None)
    parser.add_argument("--contracts-dir", type=Path, default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    contracts_dir = args.contracts_dir or (Path(__file__).resolve().parent.parent / "contracts")

    try:
        errors = validate(args.artifact, contracts_dir, args.schema_type, args.supported_major)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(2)

    if errors:
        print(f"FAIL: {args.artifact} -- {len(errors)} error(s)")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        if not args.quiet:
            print(f"PASS: {args.artifact}")
        sys.exit(0)


if __name__ == "__main__":
    main()

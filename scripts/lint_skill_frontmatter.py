#!/usr/bin/env python3
"""
lint_skill_frontmatter.py -- CI check for every skills/*/SKILL.md in this repo.

Checks, per skill:
  - YAML frontmatter parses and has required keys: name, description, version.
  - frontmatter 'name' matches the skill's directory name.
  - 'version' is a semver string (skills carry their own version independent of the
    toolkit release version -- see README.md "Versioning").
  - SKILL.md body (excluding frontmatter) is <= 500 lines -- soft budget for progressive
    disclosure; a skill approaching/over this should be pushing detail into references/.
  - body contains a '## When NOT to use this skill' section, since skill descriptions in
    this toolkit must be mutually exclusive in triggering (modeling/discovery/pipeline chain
    plus two validators easily collide without this).
  - body contains a pointer to references/toolkit-conventions.md, since cross-cutting rules
    live there once, not restated per skill.

Exit code 0 if every skill passes, 1 otherwise (prints every failure found, not just the first).
"""
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: this script requires PyYAML. Install with: pip install pyyaml --break-system-packages",
          file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
SOFT_LINE_BUDGET = 500
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def lint_skill(skill_dir: Path) -> list:
    errors = []
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return [f"{skill_dir.name}: no SKILL.md found."]

    text = skill_md.read_text()
    m = FRONTMATTER_RE.match(text)
    if not m:
        return [f"{skill_dir.name}: SKILL.md does not start with a '---' delimited YAML frontmatter block."]

    frontmatter_raw, body = m.group(1), m.group(2)
    try:
        frontmatter = yaml.safe_load(frontmatter_raw) or {}
    except yaml.YAMLError as e:
        return [f"{skill_dir.name}: frontmatter is not valid YAML: {e}"]

    for key in ("name", "description", "version"):
        if key not in frontmatter or not str(frontmatter[key]).strip():
            errors.append(f"{skill_dir.name}: frontmatter missing required non-empty key '{key}'.")

    if "name" in frontmatter and frontmatter["name"] != skill_dir.name:
        errors.append(
            f"{skill_dir.name}: frontmatter name '{frontmatter['name']}' does not match "
            f"directory name '{skill_dir.name}'."
        )

    if "version" in frontmatter and not SEMVER_RE.match(str(frontmatter["version"])):
        errors.append(f"{skill_dir.name}: version '{frontmatter['version']}' is not semver (x.y.z).")

    body_lines = [l for l in body.splitlines()]
    if len(body_lines) > SOFT_LINE_BUDGET:
        errors.append(
            f"{skill_dir.name}: SKILL.md body is {len(body_lines)} lines, over the "
            f"{SOFT_LINE_BUDGET}-line progressive-disclosure budget. Move detail into references/."
        )

    if "## When NOT to use this skill" not in body:
        errors.append(
            f"{skill_dir.name}: SKILL.md is missing a '## When NOT to use this skill' section "
            f"(required so skill descriptions stay mutually exclusive in triggering)."
        )

    if "toolkit-conventions.md" not in body:
        errors.append(
            f"{skill_dir.name}: SKILL.md does not reference references/toolkit-conventions.md "
            f"(cross-cutting rules should be linked, not restated)."
        )

    return errors


def main():
    if not SKILLS_DIR.exists():
        print("No skills/ directory found -- nothing to lint.")
        sys.exit(0)

    skill_dirs = sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir())
    if not skill_dirs:
        print("skills/ directory has no skill subdirectories yet -- nothing to lint.")
        sys.exit(0)

    all_errors = []
    for skill_dir in skill_dirs:
        all_errors.extend(lint_skill(skill_dir))

    if all_errors:
        print(f"FAIL: {len(all_errors)} frontmatter lint error(s) across {len(skill_dirs)} skill(s).")
        for e in all_errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print(f"PASS: {len(skill_dirs)} skill(s) linted clean.")
        sys.exit(0)


if __name__ == "__main__":
    main()

# data-ai-skill-toolkit

A reusable toolkit of five Claude skills for Databricks/Unity Catalog data engagements, built as one
system rather than five independent tools. Three skills form a chain -- `data-modeling` ->
`data-discovery` -> `data-pipeline` -- and two validators, `data-quality` and `data-validation`, attach
at fixed points in that chain. All five read and write a shared set of versioned JSON Schemas under
`contracts/`, so the handoff between skills is a checkable artifact, not a hope.

```
modeling  --model-spec-->  discovery  --data-contract-->  pipeline
                                |                              |
                          (quality attaches                (validation attaches
                           to any source/target)             source vs. target)
```

Default target environment: Databricks on Azure, Unity Catalog, latest LTS DBR, serverless-preferred
compute, Databricks Jobs orchestration, native PySpark/SQL, Databricks Connect for the real (non-fixture)
backend -- reusing whatever session is already authenticated in the host environment rather than the
toolkit managing its own auth. All of this is configurable per project in `toolkit.yaml` -- see
`toolkit.example.yaml`.

## Repo layout

```
data-ai-skill-toolkit/
├── README.md                      you are here
├── CONTRIBUTING.md                contribution rules, incl. the no-client-data rule
├── CHANGELOG.md
├── .claude-plugin/
│   └── plugin.json                 makes this repo a Claude Code plugin -- see "Installing as a plugin"
├── toolkit.example.yaml           copy to toolkit.yaml per project, fill in and keep out of git
├── contracts/                     versioned JSON Schemas every skill reads/writes against
│   ├── *.schema.json
│   ├── examples/                  one valid instance per schema, used by every skill's evals
│   └── confidence-rubric.md       what 0.9 vs 0.5 vs 0.2 confidence actually means
├── references/
│   └── toolkit-conventions.md     cross-cutting rules (read/write boundaries, secrets, cost
│                                  gates, LLM boundary, review gates, idempotency) -- every
│                                  SKILL.md points here instead of restating these
├── skills/
│   ├── data-modeling/
│   ├── data-discovery/
│   ├── data-pipeline/
│   ├── data-quality/
│   └── data-validation/
│       each: SKILL.md, references/, scripts/, evals/
├── fixtures/                      synthetic lakehouse (SQLite-based) with deliberate flaws,
│                                  shared across every skill's evals -- no client data, ever
├── scripts/
│   └── validate_artifact.py       schema + semantic validation any skill or CI can call
└── .github/workflows/             CI: validate every example artifact, lint skill frontmatter
```

## Versioning

Two version numbers move independently and both matter:

- **Toolkit release version** -- semver on the repo as a whole (git tags `v1.2.0`, etc.), bumped in
  `CHANGELOG.md`. This is what a project pins to.
- **Per-skill version** -- each `SKILL.md` frontmatter carries its own `version:` field. A patch release
  of the toolkit can touch one skill's reference docs without every skill's version changing; a skill's
  version only moves when that skill's behavior changes.

The compatibility contract between skills is **schema major version**, not either of the above. A skill
that consumes an artifact checks `schema_version`'s major component (via `scripts/validate_artifact.py
--supported-major N`) and refuses artifacts it doesn't recognize, printing exactly why, rather than
best-effort parsing them. This means a toolkit minor/patch release can freely improve a skill's internals
as long as the schema major version it reads and writes doesn't change; a schema *major* bump is
correctly treated as a breaking toolkit release and called out loudly in `CHANGELOG.md`.

## Pinning

**Recommended: git submodule, pinned to an annotated tag**, checked into the client project repo (e.g.
at `.toolkit/data-ai-skill-toolkit`).

```
git submodule add --branch main https://github.com/<org>/data-ai-skill-toolkit .toolkit/data-ai-skill-toolkit
cd .toolkit/data-ai-skill-toolkit && git checkout v1.2.0 && cd -
git add .gitmodules .toolkit/data-ai-skill-toolkit
git commit -m "Pin data-ai-skill-toolkit to v1.2.0"
```

Why a submodule over vendoring a copy: the pin is a single reviewable commit (the submodule's recorded
SHA), `git submodule update --remote` to move to a new tag is explicit and auditable, and the toolkit's
own history stays out of the client repo's blame/log noise. The tradeoff is real -- submodules confuse
people unfamiliar with them, and a small number of client environments restrict external git remotes in
CI. For those cases, fall back to **vendoring**: download the release tarball for a specific tag and
commit it directly under `.toolkit/`, alongside a `TOOLKIT_VERSION` file recording the exact tag. Either
way, the rule is the same: a project records, in a single file a reviewer can see, exactly which toolkit
version it's on.

**This section is about where the toolkit's files live in a project repo. It does not by itself make
Claude Code load the skills** -- see "Installing as a plugin" below for that; the two are independent
concerns and the same pinning discipline (submodule SHA / `TOOLKIT_VERSION`) applies regardless of
which install path you use.

## Installing as a plugin

This repo is a [Claude Code plugin](https://code.claude.com/docs/en/plugins-reference)
(`.claude-plugin/plugin.json` at the repo root) with its own `.claude-plugin/marketplace.json`, so it's
also the marketplace that distributes it -- installing it as a plugin is what actually makes Claude Code
discover and invoke the five skills from inside a project session; pinning a copy into the repo (above)
without this step just leaves the files sitting there unused.

Every `SKILL.md` resolves its shared paths (`scripts/`, `contracts/`) via `${CLAUDE_PLUGIN_ROOT}` and its
own bundled scripts via `${CLAUDE_SKILL_DIR}`, both of which Claude Code substitutes inline regardless of
the working directory -- so the plugin works the same way no matter where it's installed from.

**Install via the marketplace:**

```
/plugin marketplace add https://github.com/<org>/data-ai-skill-toolkit
/plugin install data-ai-skill-toolkit@data-ai-skill-toolkit
```

(Swap the URL for a local filesystem path to test a checkout before pushing it.) Restart Claude Code (or
run `/reload-skills` / `/reload-plugins`) afterward; the five skills load namespaced as
`/data-ai-skill-toolkit:data-discovery`, `/data-ai-skill-toolkit:data-modeling`, etc., and Claude also
triggers them automatically by matching your request against each skill's `description`.

**For a team, so every collaborator gets the same version without each running the install commands
themselves:** check a block like this into the project's `.claude/settings.json`, pinned to a released
tag via `ref`:

```json
{
  "extraKnownMarketplaces": {
    "data-ai-skill-toolkit": {
      "source": {
        "source": "github",
        "repo": "<org>/data-ai-skill-toolkit",
        "ref": "v1.2.0"
      }
    }
  },
  "enabledPlugins": {
    "data-ai-skill-toolkit@data-ai-skill-toolkit": true
  }
}
```

Anyone who clones the project and accepts its workspace trust dialog gets the pinned version; bumping the
pin is a one-line `ref` change reviewable in a diff, the same discipline as the submodule SHA in
"Pinning" above. Since this now covers reproducibility for the plugin itself, "Pinning" above is only
needed if a project *also* wants the toolkit's raw files physically present in its repo (e.g. for
scripts run outside Claude Code, or an audit trail) -- it's independent of, not required for, getting the
skills loaded.

**What doesn't work:** dropping the plugin bundle directly under `.claude/skills/<name>/` and relying on
Claude Code to auto-discover the nested `.claude-plugin/plugin.json` one level down. That was this
toolkit's previous recommendation and it does not reliably load the skills -- Claude Code's directory
auto-discovery expects a bare `SKILL.md` right at `.claude/skills/<name>/SKILL.md`, not a full plugin
bundle with its own `skills/` subdirectory nested inside. Use the marketplace install above instead.

**If you'd rather have short, unnamespaced names** (`/data-discovery` instead of
`/data-ai-skill-toolkit:data-discovery`) and don't need plugin packaging: copy each `skills/<name>/`
folder directly into the target project's `.claude/skills/<name>/`, without `.claude-plugin/plugin.json`.
This drops the `${CLAUDE_PLUGIN_ROOT}`/`${CLAUDE_SKILL_DIR}` substitution, so you'll also need to bring
along the shared `contracts/` and root `scripts/` directories and adjust each `SKILL.md`'s path
references accordingly.

## Mid-engagement updates

Schemas iterate; treat an update like any other dependency bump, not a background sync:

1. Read `CHANGELOG.md` for every version between your current pin and the target, not just the target's
   entry -- a schema major bump two versions back still matters to you.
2. Re-run `scripts/validate_artifact.py` against a recent real artifact from your engagement (a past
   `data-contract.json`, etc.) with `--supported-major` set to the *new* version's major. If it fails,
   that's the toolkit telling you the schema changed in a way your existing artifacts don't satisfy --
   read the changelog entry for what changed and regenerate rather than hand-patching the artifact.
3. Re-run the affected skill's evals (`skills/<name>/evals/`) against your project's own
   `fixtures/local-overrides/` if you have any, to catch anything project-specific the shared fixtures
   wouldn't.
4. Bump the pin (submodule SHA or `TOOLKIT_VERSION`) in one commit, separate from any other work, so it's
   independently revertable.

## Client-specific overrides

A project may extend or override a skill's behavior for that engagement -- e.g. an extra quality check
type, a client-specific naming convention discovery should recognize. The mechanism is a project-local
`overrides/` directory alongside the pinned toolkit (not a fork of it), referenced from that project's
`toolkit.yaml` (`overrides.enabled: true` plus a path). Skills check for project overrides after loading
their own bundled `references/` and `scripts/`, so an override can add to or shadow toolkit behavior
without editing the pinned toolkit's files in place (which would make the pin meaningless).

**Hard rule: overrides never flow back upstream into this repo with client specifics in them.** If an
override turns out to be broadly useful, generalize it -- strip every client name, schema name, sample
value, and business term specific to that engagement -- before proposing it as a toolkit change. See
`CONTRIBUTING.md`.

## Contribution rules

See `CONTRIBUTING.md` for the full process. The rule that overrides everything else: **no client data,
client schema names, or client sample values ever enter this repo**, in code, in an example artifact, in
a fixture, in a commit message, or in an issue. `fixtures/` is synthetic, generated by
`fixtures/generate_fixtures.py`, and reviewed for this specifically before merge.

## Evaluation

Nothing ships without evidence it works. Every skill has `evals/evals.json` (test prompts + objectively
checkable assertions) that run against the shared synthetic `fixtures/` lakehouse -- see each skill's
`evals/README.md` for how to run them, and `CONTRIBUTING.md` for what's required before a skill change
merges.

## Status

This repo is being built in phases (contract layer -> one skill end-to-end -> remaining four ->
integration). See `DECISIONS.md` for choices made where the original spec was ambiguous, flagged for
review at each phase boundary.

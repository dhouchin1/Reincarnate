# Reincarnate — engine + plugin design contract

This file is the contract every module is built against. If you change an interface
here, you change it here first. Read CONCEPT.md for the why.

## Repo layout

```
Reincarnate/                      # repo root == Claude Code plugin root
  .claude-plugin/plugin.json      # plugin manifest
  commands/*.md                   # slash commands: /reincarnate:<name>
  skills/<name>/SKILL.md          # skills the commands lean on (optional)
  pyproject.toml                  # python package "reincarnate", console script `reincarnate`
  src/reincarnate/
    __init__.py                   # __version__
    paths.py                      # data dir resolution
    models.py                     # dataclasses
    schema.py                     # dossier JSON schema hint + validate/normalize
    store.py                      # SQLite store (the ONLY module that touches the DB)
    prompts.py                    # all LLM prompt builders
    claude_runner.py              # `claude -p` subprocess wrapper (headless mode)
    comprehend.py                 # sandbox clone + comprehension run + ingest
    overlay.py                    # profile overlay: fit scores per capability
    catalog.py                    # catalog queries over the store
    salvage.py                    # clean-room spec builder (the air-gap artifact)
    compose.py                    # app proposals from an idea + catalog
    kit.py                        # export a buildable .reincarnate/ kit
    render.py                     # dossier -> markdown; later: portfolio html
    importers/vault.py            # second-brain vault dossier notes -> store
    cli.py                        # argparse entry point
  tests/                          # pytest, no network, no real `claude`
```

Python 3.11+. **Standard library only** at runtime (sqlite3, json, argparse, subprocess,
pathlib, dataclasses). Dev deps: pytest, ruff. Hand-formatted; `ruff check` only.

## Data directory

`paths.data_dir()` → `$REINCARNATE_HOME` if set, else `~/.reincarnate/`.
Inside: `reincarnate.db`, `sandbox/<owner>-<repo>/` (clones), `dossiers/<owner>-<repo>.md`
(rendered notes), `kits/` (zips).

## Dossier JSON (the comprehension output)

Identical to the second-brain schema, with `fit_to_fleet` renamed `fit_to_projects`
(ingest accepts both keys and normalizes to `fit_to_projects`).

```json
{
  "summary": "one or two sentences",
  "what_it_does": "markdown prose",
  "languages": ["most-used first"],
  "build_system": "",
  "entry_points": ["path - what it is"],
  "architecture": "markdown prose",
  "capabilities": [
    {"name": "", "what": "", "files": [], "dependencies": [],
     "portability": "high|medium|low", "portability_notes": "", "security_notes": ""}
  ],
  "dependencies": {"license": "", "notable": [], "supply_chain_flags": []},
  "security": {"concerns": [], "network_egress": "", "dangerous_patterns": []},
  "fit_to_projects": [{"project": "<profile project slug>", "capability": "<name>", "how": ""}],
  "salvage_recommendations": []
}
```

`schema.validate_dossier(d: dict) -> list[str]` returns a list of problems (empty = ok).
`schema.normalize_dossier(d: dict) -> dict` fills missing keys with empty values, lowercases
portability, renames fit_to_fleet, coerces lists. `schema.SCHEMA_HINT` is the JSON text above
for prompts.

## SQLite schema (store.py owns it; `Store.migrate()` is idempotent)

```sql
profile(id INTEGER PRIMARY KEY CHECK(id=1), name TEXT, stack_json TEXT, intents_json TEXT,
        license_policy_json TEXT, created TEXT, updated TEXT);
projects(slug TEXT PRIMARY KEY, path TEXT, repo_url TEXT, stack_json TEXT, goal TEXT,
         test_cmd TEXT, created TEXT);
dossiers(id INTEGER PRIMARY KEY, owner TEXT, repo TEXT, url TEXT, sha TEXT,
         comprehended_at TEXT, model TEXT, cost_usd REAL, source TEXT,  -- claude-p|session|vault-import
         summary TEXT, license TEXT, data_json TEXT, UNIQUE(owner, repo, sha));
capabilities(id INTEGER PRIMARY KEY, dossier_id INTEGER REFERENCES dossiers(id) ON DELETE CASCADE,
         idx INTEGER, name TEXT, what TEXT, files_json TEXT, deps_json TEXT,
         portability TEXT, portability_notes TEXT, security_notes TEXT);
capabilities_fts  -- FTS5 over (name, what, portability_notes), content=capabilities
fits(capability_id INTEGER, project_slug TEXT, score REAL, how TEXT, source TEXT,  -- dossier|overlay|manual
     PRIMARY KEY(capability_id, project_slug));
votes(capability_id INTEGER PRIMARY KEY, vote INTEGER, updated TEXT);  -- -1|1
compositions(id INTEGER PRIMARY KEY, slug TEXT UNIQUE, name TEXT, pitch TEXT, idea TEXT,
         target_kind TEXT, target_slug TEXT, proposal_json TEXT, status TEXT, created TEXT); -- proposed|emitted
kits(id INTEGER PRIMARY KEY, composition_id INTEGER, path TEXT, created TEXT);
```

The latest dossier per (owner, repo) is "current"; older SHAs are kept.

## Store API (exact signatures; everything else codes against these)

```python
class Store:
    def __init__(self, db_path: Path | None = None)      # None -> paths.db_path()
    def migrate(self) -> None
    # profile
    def get_profile(self) -> Profile | None
    def save_profile(self, p: Profile) -> Profile
    # projects
    def add_project(self, p: Project) -> Project
    def list_projects(self) -> list[Project]
    def get_project(self, slug: str) -> Project | None
    def remove_project(self, slug: str) -> bool
    # dossiers
    def upsert_dossier(self, owner, repo, url, sha, data: dict, *, model="", cost_usd=0.0,
                       source="claude-p") -> Dossier          # replaces capabilities rows for that dossier
    def get_dossier(self, ref: str | int) -> Dossier | None    # id, "owner/repo" (current), or "owner/repo@sha"
    def list_dossiers(self, *, current_only=True) -> list[Dossier]
    def delete_dossier(self, dossier_id: int) -> bool
    # capabilities
    def list_capabilities(self, *, dossier_id: int | None = None, current_only=True) -> list[Capability]
    def get_capability(self, capability_id: int) -> Capability | None
    def search_capabilities(self, query: str, *, limit=50) -> list[Capability]   # FTS
    # fits / votes
    def set_fit(self, capability_id, project_slug, score: float, how: str, source: str) -> None
    def list_fits(self, *, capability_id=None, project_slug=None) -> list[Fit]
    def set_vote(self, capability_id: int, vote: int) -> None      # 0 clears
    def get_votes(self) -> dict[int, int]
    # compositions / kits
    def save_composition(self, c: Composition) -> Composition      # insert or update by slug
    def get_composition(self, ref: str | int) -> Composition | None
    def list_compositions(self) -> list[Composition]
    def add_kit(self, composition_id: int, path: str) -> int
```

## Models (models.py, all dataclasses, all JSON-friendly via `to_dict()` / `from_dict()`)

```python
Profile(name, stack: list[str], intents: list[str], license_policy: dict, created, updated)
   # license_policy = {"deny": ["GPL-3.0", "AGPL-3.0"], "warn": ["unknown"]}
Project(slug, path, repo_url, stack: list[str], goal, test_cmd, created)
Dossier(id, owner, repo, url, sha, comprehended_at, model, cost_usd, source, summary, license, data: dict)
   # .donor -> "owner/repo"; .capabilities -> data["capabilities"]
Capability(id, dossier_id, idx, name, what, files, dependencies, portability, portability_notes,
           security_notes, donor: str, license: str)      # donor/license denormalized on read
Fit(capability_id, project_slug, score, how, source)
Composition(id, slug, name, pitch, idea, target_kind, target_slug, proposal: dict, status, created)
   # proposal = {"capabilities": [{"capability_id", "role", "order"}], "glue": "markdown",
   #             "gaps": [{"need": "", "suggested_donors": [], "build_fresh": true}],
   #             "risks": [], "license_flags": []}
```

## CLI surface (cli.py; argparse; every command supports `--json` where output is data)

```
reincarnate init
reincarnate profile show | set [--name] [--stack a,b] [--intent "..."]* [--deny-license X]* [--clear-intents]
reincarnate project add <path> [--slug] [--goal] [--test-cmd] | list | rm <slug>
reincarnate clone <github-url>                     -> {"path","owner","repo","sha"}   (sandbox clone)
reincarnate ingest <dossier.json> --url <url> --sha <sha> [--source session|claude-p|vault-import] [--model] [--cost]
reincarnate comprehend <github-url> [--model MODEL] [--max-turns N] [--force]       (clone + claude -p + ingest + overlay)
reincarnate overlay [<owner/repo>|--all] [--engine claude-p|heuristic]
reincarnate dossier list | show <owner/repo> [--md]
reincarnate catalog [--search Q] [--portability high|medium|low] [--donor owner/repo] [--fit <slug>] [--gaps]
reincarnate vote <capability-id> up|down|clear
reincarnate salvage <capability-id> --into <project-slug> [--out FILE]
reincarnate compose "<idea>" [--caps 1,2,3] [--target <slug>|new:<name>] [--engine claude-p|offline]
reincarnate compose list | show <id|slug>
reincarnate reincarnate <owner/repo> --into <slug>|new:<name>       (whole-donor composition)
reincarnate kit <composition id|slug> --into <dir|project-slug> [--zip]
reincarnate import-vault <dir-with-dossier-notes>
reincarnate export-md <owner/repo> [--out FILE]
```

`cli.py` registers every subcommand but imports the implementing module lazily inside the
handler, so a missing module only fails that one command.

## Comprehension modes

1. **Headless** (`comprehend`): `claude_runner.run(prompt, cwd, tools="Read,Grep,Glob",
   disallowed="Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch,Task",
   max_turns, model) -> RunResult(text, cost_usd, rc, stderr)`. Parses `--output-format json`.
2. **In-session** (plugin): the command tells the running Claude Code session to
   `reincarnate clone <url>`, read the sandbox with Read/Grep/Glob ONLY, write the dossier
   JSON to a temp file, then `reincarnate ingest`. Same records either way.

Sandbox clone: `git clone --depth 1 --no-tags -c core.hooksPath=/dev/null <url> <dir>`;
refuse non-github https URLs; refuse if `du` > 500 MB after clone (delete it); record HEAD sha.

## The air gap (non-negotiable)

- Only comprehension reads donor source. `salvage`, `compose`, and `kit` output never contain
  donor file *contents*; donor file *paths* may appear only in a "reference only, DO NOT COPY" line.
- Every kit carries `BLUEPRINT.md` with the provenance + license of every donor and the
  clean-room rules, and `CLAUDE.md` repeats the rules for the building agent.

## Kit layout (`kit.export_kit(composition, target_dir) -> Path`)

```
<target>/.reincarnate/
  BLUEPRINT.md      name, pitch, donors + licenses, air-gap statement, ordered steps, gaps, risks
  CLAUDE.md         stack conventions from profile/project + clean-room rules + test command
  tasks.md          "- [ ] Step N: <capability name> (from owner/repo)" one per step, plus gap steps
  specs/NN-<slug>.md   salvage.build_clean_room_spec output per step; gaps get a "build fresh" spec
  run.sh            loops steps: `claude -p "Build step N per .reincarnate/specs/NN-*.md ..."`,
                    runs test_cmd, stops for review after each step (`--yes` to continue)
```

## Overlay (overlay.py)

`overlay.apply(store, dossier, profile, projects, engine)`:
- `heuristic`: score = token overlap between capability (name/what/deps) and each project's
  (goal + stack) ∈ [0,1]; keep dossier-provided `fit_to_projects` rows as source=dossier with 0.8.
- `claude-p`: one non-agentic `claude -p` (tools="", max_turns=1) with `prompts.build_overlay_prompt`
  returning `[{"capability": name, "project": slug, "score": 0-1, "how": ""}]`; falls back to heuristic.

## Compose (compose.py)

`compose.propose(store, idea, *, caps=None, target=None, engine) -> Composition`:
- retrieval: FTS over the idea (plus `--caps`), top 25 → prompt with catalog rows
  (id, donor, license, name, what, portability, fit) → `prompts.build_compose_prompt`.
- `claude-p` engine: non-agentic `claude -p`, JSON out; `offline` engine: deterministic
  (every retrieved cap with order by portability, no glue, gaps empty) so tests and the plugin's
  in-session mode (where the session itself does the composing) work without a subprocess.
- validation (deterministic, always): drop capability ids not in the store; drop if <2 caps;
  flag licenses against the profile policy; refuse target slug that is not a known project unless `new:`.
```

---

# v0.2: Forge Studio merge (2026-10-22)

Ports Forge Studio's ranked-excerpt comprehension, interface tokens with a canonical
vocabulary, and multi-design composition into this engine. Plugin and kit stay. Measurements
behind the defaults: `docs/COST.md`.

## Capability fields (dossier JSON and `capabilities` table)

Each capability gains three fields:

```json
{"provides": ["data:service-status"], "requires": ["store:service-registry", "ext:docker"],
 "novelty": "commodity|pattern|distinctive"}
```

- `provides` / `requires`: 0-6 interface tokens each, `kind:thing`. Kinds and rules are
  Forge Studio's (`interfaces.KINDS`): auth, store, http, event, notify, ui, schedule, config,
  data, ext. `requires` is what it needs from OTHER parts or the environment; state it keeps
  for itself goes in `provides`; `ext:` is never provided by a capability.
- `novelty` rubric (the "beyond boilerplate" bar):
  - `commodity`: a competent coding agent writes it correctly from a one-line prompt
    (retry/backoff, logging, config loading, CRUD endpoints, auth boilerplate).
  - `pattern`: a known shape with real design choices worth specifying (a plugin registry,
    an SSE fan-out, a typed integration framework).
  - `distinctive`: non-obvious behavior an agent would not invent unprompted, the reason to
    reincarnate (conditional compression only when smaller, TOFU peer identity, a ranking
    heuristic).
  - `""` means unrated (imported dossiers before `enrich`).
- `schema.normalize_dossier` lowercases and dedupes tokens and keeps at most 6 per side, and
  maps unknown novelty values to `""`. Canonicalization against the vocabulary happens in
  `Store.upsert_dossier` (it owns the vocabulary).

## Vocabulary (`interfaces.py`, store tables)

```sql
vocab_tokens(token TEXT PRIMARY KEY, definition TEXT, platform INTEGER DEFAULT 0);
vocab_aliases(alias TEXT PRIMARY KEY, canonical TEXT);   -- canonical NULL = drop the token
ALTER capabilities ADD provides_json TEXT DEFAULT '[]', requires_json TEXT DEFAULT '[]',
                       novelty TEXT DEFAULT '';
designs(id INTEGER PRIMARY KEY, slug TEXT UNIQUE, idea TEXT, title TEXT, requirements_json TEXT,
        options_json TEXT, engine TEXT, meta_json TEXT, created TEXT);
```

`Store.migrate()` adds missing columns with `ALTER TABLE` (check `PRAGMA table_info`), and
seeds the vocabulary from the packaged `src/reincarnate/data/vocabulary.json` when
`vocab_tokens` is empty. Store API additions:

```python
def get_vocabulary(self) -> interfaces.Vocabulary
def set_capability_fields(self, capability_id, *, provides=None, requires=None, novelty=None) -> None
    # canonicalizes tokens via the vocabulary; None leaves a field unchanged
def save_design(self, d: Design) -> Design          # insert or update by slug
def get_design(self, ref: str | int) -> Design | None
def list_designs(self) -> list[Design]
```

`models.Capability` gains `provides: list[str]`, `requires: list[str]`, `novelty: str`.
`models.Design(id, slug, idea, title, requirements: list[dict], options: list[dict], engine,
meta: dict, created)`.

`interfaces.py` is a port of Forge Studio's `interfaces.py` (KINDS, normalize_token,
normalize_list, is_external, Vocabulary with canonicalize/platform/block, Wiring,
check_wiring) with `Vocabulary` built from the store instead of Forge Studio's DB.

## Comprehension (`excerpt.py`, `comprehend.py`)

- `excerpt.py`: port of Forge Studio's `select_files`, `_score`, the skip/source/manifest/
  lockfile sets, `license_hint`, `clean_license`.
- Default mode is **excerpt**: rank files, pack up to `budget` characters (default 120,000),
  one isolated `claude -p` call with no tools and `max_turns=1`. `--mode agentic` keeps the
  v0.1 read-only multi-turn pass.
- Default model **`claude-haiku-5-5`** for both modes (`--model` overrides).
- The prompt asks for the v0.1 schema plus `provides`, `requires`, `novelty`, includes the
  vocabulary block (`Vocabulary.block()`), and treats all repo text as data.
- License: if the model returns unknown, fall back to `excerpt.license_hint`; always pass
  through `clean_license`.

## Tier 2 enrichment (`enrich.py`)

`reincarnate enrich [<owner/repo>|--all] [--only-missing] [--batch 25] [--model claude-haiku-5-5]`
rates capabilities that lack `novelty` or interfaces, in batches, from catalog text only (no
donor source): one isolated tool-less call per batch returning
`{"<capability id>": {"novelty": "...", "provides": [...], "requires": [...]}}`. Unknown ids
are ignored; results go through `set_capability_fields`. This is where Jev slots in later.

`reincarnate import-interfaces <capability-interfaces.json>` applies second-brain's backfill
(keys `40-Code/repos/<owner>-<repo>.md::<lowercased capability name>`) onto current
capabilities by matching `<owner>-<repo>` (lowercased) and the lowercased name.

## Multi-design composition (`design.py`)

Port of Forge Studio's `compose.py` on this store:

- `SLOTS`: lean, complete, alternative, bold. `MAX_CAPS` 8, `MAX_JACCARD` 0.7.
- Pool: lexical TF-IDF `rank_catalog` over current capabilities to at most 120, excluding
  `novelty == "commodity"` unless `--include-commodity`.
- Engines: `claude-p` (default model `claude-opus-5-5`), `session` (`--proposal FILE`
  holding the same JSON the model would return), `offline` (deterministic lean + complete
  from the ranked pool, no requirements beyond the idea itself).
- Deterministic validation exactly as Forge Studio's `build_options`: real ids only, at
  least 2 capabilities, near-duplicate options dropped, coverage labels pointing outside the
  option relabelled glue, missing labels net-new, license-risk and security flags.
- Wiring: `check_wiring` per option against every current capability's interfaces, plus
  optional link verification by one isolated Haiku call (`--no-verify` skips).
- Scores: reuse = catalog-covered requirements / requirements; fit = wired / (wired + unmet).
- `reincarnate design pick <design> <slot|option-slug> [--target ...]` turns one option into a
  regular `Composition` (capabilities with roles, glue from glue + rationale, gaps from
  net-new work and unmet wiring, risks from flags, `proposal["wiring"]` and
  `proposal["coverage"]` attached), so `reincarnate kit` builds it unchanged.

## CLI additions

```
reincarnate comprehend <url> [--mode excerpt|agentic] [--model M] [--budget N] [--max-turns N] [--force]
reincarnate enrich [<owner/repo>] [--all] [--only-missing] [--batch N] [--model M]
reincarnate import-interfaces <file>
reincarnate vocab [--kind K] [--json]
reincarnate catalog ... [--novelty commodity|pattern|distinctive|unrated]
reincarnate design "<idea>" [--options 2-4] [--engine claude-p|session|offline] [--proposal FILE]
                            [--model M] [--include-commodity] [--no-verify] [--json]
reincarnate design list | show <slug>
reincarnate design pick <slug> <slot|option-slug> [--target <slug>|new:<name>]
```

## Jev, confidence routing, Tier 3 (added 2026-10-22)

- `jev.py`: TypeSafe System One client (`POST https://api.typesafe.ai/v1/systemone`, key in
  `TYPESAFE_API_KEY`), choice/score/noul questions, chunked at 13 per request, 429/529
  retried with backoff; `SimJevClient` ("jev-sim") is a lexical stand-in for offline runs and
  tests and is not evidence of Jev's quality or speed.
- `reincarnate enrich --engine haiku|jev|jev-sim [--review-threshold 0.6]`: the Jev engine asks
  one score question for novelty and one choice question per non-canonical interface token
  (criteria: the canonical tokens of the same kind, plus "keep"). `confidence` is the minimum
  answer confidence; below the threshold the capability gets `needs_review = 1`.
- `reincarnate review [--limit N] [--model claude-sonnet-5-5]`: Tier 3, re-rates only flagged
  capabilities from catalog and dossier text (never donor source), then clears the flag.
- `reincarnate design ... --shortlist lexical|jev|jev-sim`: Jev keeps the capabilities it
  judges useful for the idea (noul P >= 0.5, at most 40) before the Opus call.
- `reincarnate jev status|models`, `reincarnate catalog --needs-review`.


---

# v0.3: buildable kits (2026-10-22)

From the first kit dogfood (`.local-data/new/homelab-forge`, notes in
`.reincarnate/step-01-notes.md`): a context-free builder could not build the step-1 capability
from the kit alone. Decisions (Dan, 2026-10-22): acceptance checks are stored per capability
plus design requirements; dependencies are the option's stack plus small justified extras
logged in `DEPENDENCIES.md`; unmet needs with a catalog candidate pull that candidate in as a
real step; the option's stack wins over the profile stack.

## Capability `acceptance` (schema, store, comprehension, enrich)

- Dossier capability field `"acceptance": ["observable check", ...]`: 2-4 checks, each an
  input-to-outcome behavior a test can verify ("Given a token signed more than max_age seconds ago, verification fails with an expiry error"), with no donor code, file paths, or library names.
  `normalize_dossier`: list of non-empty stripped strings, at most 5, each at most 300 chars.
- Store column `acceptance_json TEXT DEFAULT '[]'` (migrated in place), `Capability.acceptance:
  list[str]`, `set_capability_fields(..., acceptance=None)`.
- Both comprehension prompts ask for it, and add: `what` and `acceptance` describe behavior and
  never name the donor's third-party libraries or frameworks (those go in `dependencies`).
- `reincarnate enrich --fields novelty,interfaces,acceptance` (default `novelty,interfaces`):
  the Haiku prompt asks only for the requested fields and only those are applied;
  `--only-missing` selects capabilities missing any requested field. The Jev engine cannot write
  text, so `acceptance` is skipped there with a note. Backfill:
  `reincarnate enrich --all --fields acceptance --only-missing`.

## Kit generation (`kit.py`, `salvage.py`)

**Target kind.** A kit target is *new* when the composition's target kind is new, or the target
directory has none of `pyproject.toml, package.json, go.mod, Cargo.toml, Gemfile, pom.xml,
build.gradle`. Otherwise it is *existing* (a registered project or a directory with a manifest).

**Stack.** Existing target: the project's stack and test command. New target: the composition's
`proposal["stack"]` (free text such as "TypeScript + Next.js + tRPC + SQLite (Drizzle)"), split on
`+` and `,` into items; fall back to the profile stack.

**Test command** (new targets): ecosystems from stack keywords. node: typescript, javascript, node,
next.js, nextjs, react, vue, svelte, express, trpc, vite, deno, bun. python: python, fastapi,
django, flask, pytest. go: go, golang. rust: rust, cargo. One ecosystem: `npm test`,
`python -m pytest -q`, `go test ./...`, `cargo test`. Several or none: `make test` (step 0 adds a
Makefile `test` target running every suite).

**Dependency policy** (replaces "add no new third-party dependencies" everywhere):
- New target: "Allowed dependencies: the stack below. Any other dependency must be small and
  necessary; before adding it, record its name, version range and reason in `DEPENDENCIES.md`.
  Prefer the standard library."
- Existing target: the same, with "the dependencies this project already uses" as the allowed set.
- `salvage.constraint_lines(policy)` renders it; every spec and `CLAUDE.md` use the same text.

**Step 0 (new targets only):** `specs/00-scaffold.md`, `tasks.md` "Step 0: scaffold the project".
It creates the manifest(s) for each ecosystem with only allowed dependencies, a minimal skeleton
(entry points only, no feature code), the test runner wired so the test command runs every suite,
one passing smoke test per ecosystem, a README "Run the tests" section, and `DEPENDENCIES.md`
listing every declared dependency with its reason ("in the stack" for stack items). Its
acceptance checks: the test command exits 0 with at least one test per ecosystem;
`DEPENDENCIES.md` covers every declared dependency; no feature code from later steps.

**CLAUDE.md:** `## Stack` (one bullet per item), `## Dependencies` (the policy text),
`## Tests` (the test command; "set in step 0" for new targets), `## Clean-room rules`, and the
one-step-at-a-time line. `run.sh` sets `TEST_CMD` to the test command.

**Step order.** Capability steps in proposal order. A gap with `before_capability_id` is emitted
immediately before that capability's step. Gaps without it come after all capability steps.

**Acceptance checks in every spec** (`**Acceptance checks:**` section):
- capability step: the capability's stored `acceptance`, then "Covers requirement rN: <text>" for
  each `proposal["coverage"]` row with `kind == "catalog"` and `cap == "c<capability_id>"`, joined
  to `proposal["requirements"]`;
- wiring gap step: "Provides `<token>` as defined above" and "<consumer> (step N) can use this
  step's `<token>` without a stub of its own";
- new-work gap step: "The behavior above is implemented and covered by tests";
- if a spec has none: "(none recorded: write a test for every behavior above and list them in
  your step notes)".

**Wiring gap specs** (gap has `token`): title "Build fresh: `<token>` for <consumer>"; behavior =
the vocabulary definition of the token (or "no canonical definition; infer from the consumer"),
then "Consumed by <consumer> (step N): <consumer what>"; suggested donors listed as
"comprehend later, do not read now".

**Glue ids.** In `BLUEPRINT.md` and every spec, `c<id>` references resolve to
"<capability name> (step N)" when the capability is in the kit, else to its name.

## `design pick` (unmet needs and glue)

- For each unmet wiring need (token, consumer) with catalog candidates (decision 2026-10-22,
  after the first re-export pulled in poor fits): candidates whose license is `deny` are never
  auto-filled. Rank the rest: same donor as the consumer first, then a deterministic relevance
  score (content-word overlap between the candidate's name and behavior and the consumer's name
  and behavior plus the token and its vocabulary definition), then the wiring rank. Auto-fill
  the top one only if it is same-donor or its relevance clears `RELEVANCE_MIN`; otherwise the
  need stays a build-fresh gap that lists the weak candidates. An auto-filled part becomes a
  capability step inserted immediately before the consumer (role "provides `<token>` for
  <consumer>", `auto_filled[].reason` "same donor" or "relevance 0.NN"), deduplicated. Recompute wiring for the augmented
  set and repeat once (at most 2 passes, at most 4 auto-added capabilities). Record
  `proposal["auto_filled"] = [{token, consumer, capability_id, name, donor}]`.
- Remaining unmet needs become gaps `{need: "<token> for <consumer>", token, definition,
  consumer_capability_id, before_capability_id, suggested_donors, build_fresh: true}`.
  `compose._gaps` preserves these extra keys.
- `_glue_markdown` resolves `c<id>` to capability names.
- The design prompt states the profile stack as the user's preference: "prefer it unless the
  option's nature calls for another stack, and say why in the rationale".
- Specs scrub donor file references (paths with an extension, with or without line ranges) from
  every piece of dossier prose they quote; the "Donor reference files (context only — DO NOT
  COPY)" line is the only place donor paths appear.

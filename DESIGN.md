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

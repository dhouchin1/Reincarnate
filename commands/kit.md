---
description: "Export a composition as a buildable .reincarnate/ kit and optionally build step 1 here"
argument-hint: "<composition id|slug> <project-slug | new:<name> | directory>"
allowed-tools: Bash, Read, Grep, Glob, Edit, Write
---

Export a Reincarnate kit. Arguments: **$ARGUMENTS** (composition id or slug, then a target).

If either part is missing, ask. To see choices:

```bash
reincarnate compose list --json
reincarnate project list --json
```

## 1. Resolve the target

`reincarnate kit --into` accepts a **project slug** (from `project list`), **`new:<name>`**
(the engine creates a fresh directory under its data dir and prints the path), or an
absolute **directory** path (confirm it exists or ask before creating it). Pass the user's
choice through unchanged.

## 2. Export

```bash
reincarnate kit <composition id|slug> --into <target> --deepen --json
```

`--deepen` first runs a read-only pass over each part's donor code (on the comprehension side
of the air gap, about $0.02 per part) so every spec carries field-level **Data shapes**,
**Key values**, and acceptance checks with real numbers. Drop `--deepen` only if the user wants
an offline export; then report the `warnings` the result carries.

Add `--zip` only if the user asked for an archive. Add `--notes <file>` if the user has a
markdown file they want as the Glue section instead of the composition's own glue. Read
`kit_dir`, `steps`, and `files` from the JSON and report them. If the result has an `error`
key, show it and stop.

## 3. Explain the layout

Show the user what was written under `<target>/.reincarnate/`, reading the files to be sure:

```
BLUEPRINT.md        name, pitch, donors and licenses, the air-gap statement, ordered steps,
                    glue, wiring, requirement coverage, risks
CLAUDE.md           ## Stack, ## Dependencies (allowed set + DEPENDENCIES.md rule),
                    ## Tests (the test command), ## Clean-room rules
tasks.md            one checkbox per step; new projects start with "Step 0: scaffold the project"
specs/00-scaffold.md  new projects only: manifests, skeleton, test runner, smoke tests
specs/NN-<slug>.md  one clean-room spec per step: behavior, **Data shapes**, **Key values**,
                    **Acceptance checks**; a missing part the wiring found sits right before the
                    step that needs it; `NN-glue-*` steps join parts for glue-only requirements;
                    steps before the auth step carry a guard-seam note
run.sh              claude -p driver: builds a step, runs the test command, stops for review
```

Point out the donors and licenses listed in `BLUEPRINT.md`, and any license flags.

## 4. Offer to build the first step now, in this session

The first step is step 0 (`specs/00-scaffold.md`) when the kit has one, otherwise step 1.
Ask: "Want me to build the first step here, in this session, behind a review stop?" If yes,
**follow the clean-room skill** (`reincarnate:clean-room`) and do exactly this:

1. Read `<target>/.reincarnate/CLAUDE.md` and `<target>/.reincarnate/tasks.md`.
2. Read the first step's spec (Glob `specs/00-*.md`, else `specs/01-*.md`).
3. **Never open donor source.** Any "reference only, DO NOT COPY" paths in a spec are
   provenance; do not read them or anything under a `sandbox/` directory.
4. Build only that step, in the stack and conventions `CLAUDE.md` gives. Use only the
   allowed dependencies; log any small necessary extra in `DEPENDENCIES.md` with its reason
   before adding it. Write a test for every item under the spec's **Acceptance checks:**.
5. Run the test command from `CLAUDE.md` `## Tests` and report the real result.
6. Tick the step in `tasks.md` only if the tests pass.
7. **Stop.** Summarize the files changed, the test results, and anything the spec left
   ambiguous, then wait for the user's review before the next step.

If the user prefers to run it unattended: `bash <target>/.reincarnate/run.sh` drives
`claude -p` step by step and stops for review after each (`--yes` continues without stopping).

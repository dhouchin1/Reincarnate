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
reincarnate kit <composition id|slug> --into <target> --json
```

Add `--zip` only if the user asked for an archive. Add `--notes <file>` if the user has a
markdown file they want as the Glue section instead of the composition's own glue. Read
`kit_dir`, `steps`, and `files` from the JSON and report them. If the result has an `error`
key, show it and stop.

## 3. Explain the layout

Show the user what was written under `<target>/.reincarnate/`, reading the files to be sure:

```
BLUEPRINT.md        name, pitch, donors and licenses, the air-gap statement, ordered steps, gaps, risks
CLAUDE.md           stack conventions + the clean-room rules + the test command
tasks.md            "- [ ] Step N: <capability> (from owner/repo)", one per step, plus gap steps
specs/NN-<slug>.md  one clean-room spec per step (gaps get a "build fresh" spec)
run.sh              claude -p driver: builds a step, runs tests, stops for review
```

Point out the donors and licenses listed in `BLUEPRINT.md`, and any license flags.

## 4. Offer to build step 1 now, in this session

Ask: "Want me to build step 1 here, in this session, behind a review stop?" If yes, **follow
the clean-room skill** (`reincarnate:clean-room`) and do exactly this:

1. Read `<target>/.reincarnate/CLAUDE.md` and `<target>/.reincarnate/tasks.md`.
2. Read `<target>/.reincarnate/specs/01-*.md` (Glob for the exact file name).
3. **Never open donor source.** Any "reference only, DO NOT COPY" paths in a spec are
   provenance; do not read them or anything under a `sandbox/` directory.
4. Build only step 1, in the target project's conventions, with no new third-party
   dependencies unless the spec allows them. Write the tests the spec's acceptance checks call for.
5. Run the test command from `.reincarnate/CLAUDE.md` (ask the user if it is missing) and
   report the real result.
6. Tick step 1 in `tasks.md` only if the tests pass.
7. **Stop.** Summarize the files changed and the test results, and wait for the user's
   review before touching step 2.

If the user prefers to run it unattended: `bash <target>/.reincarnate/run.sh` drives
`claude -p` step by step and stops for review after each (`--yes` continues without stopping).

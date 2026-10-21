---
description: "Turn one catalog capability into a clean-room build spec for one of your projects"
argument-hint: "<capability-id> --into <project-slug>"
allowed-tools: Bash, Read, Grep, Glob, Edit, Write
---

Salvage one capability. Arguments: **$ARGUMENTS** (a capability id and `--into <project-slug>`).

If the id or the project is missing, help the user choose:

```bash
reincarnate catalog --json
reincarnate project list --json
```

## 1. Generate the spec

```bash
F="${TMPDIR:-/tmp}/reincarnate-salvage-<capability-id>-<project-slug>.md"
reincarnate salvage <capability-id> --into <project-slug> --out "$F"
```

Then Read `$F` and show the user the spec. Point out: what behavior it requires, the
acceptance checks, the donor and its license, and that the donor file paths (if listed) are a
"reference only, DO NOT COPY" line, not something to open. Check the donor license against
`reincarnate profile show --json` (`license_policy`) and flag a denied or unknown license
before going further.

If the command fails because the capability id does not exist or the project is not
registered, say so and suggest `/reincarnate:catalog` or `reincarnate project add <path>`.

## 2. Offer to build it here

Ask: "Want me to build this in this session, behind a review stop?" If yes, **follow the
clean-room skill** (`reincarnate:clean-room`):

1. Read the project's own conventions (its `CLAUDE.md`, config, a few neighboring modules and tests).
   Find the project's path with `reincarnate project list --json`.
2. **Never open donor source**, including the paths named in the spec's reference-only line
   and anything under a `sandbox/` directory.
3. Implement from the spec's behavior and acceptance checks, in the project's idiom, with no
   new third-party dependencies unless the spec allows them.
4. Write the tests the acceptance checks call for, then run the project's test command
   (`test_cmd` from `project list`, or ask the user). Report real results.
5. **Stop** for review with a summary of files changed and test results.

If the user declines, leave the spec file in place and tell them its path.

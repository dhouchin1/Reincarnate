---
description: "Read donor code for the parts you will build: field-level data shapes, key values, and acceptance checks with real numbers"
argument-hint: "<composition slug> | <capability id> [<capability id> ...]"
allowed-tools: Bash
---

Deepen capabilities before building them. Arguments: **$ARGUMENTS**

This is the comprehension side of the air gap: a headless, read-only pass over each donor's
code (no tools that run or edit anything) writes behavior facts into the catalog. You, in this
session, do not open donor source. You only run the command and report what it recorded.

If the argument is a composition slug:

```bash
reincarnate deepen --composition <slug> --json
```

If it is one or more capability ids:

```bash
reincarnate deepen <id> [<id> ...] --json
```

Add `--force` only if the user asks to redo parts that are already deepened. Report `deepened`,
`skipped`, every entry under `failed` with its reason, and `cost_usd`. Then show, for one
deepened part, its data shapes and key values from:

```bash
reincarnate catalog --search "<part name>" --json
```

If `reincarnate` is missing, tell the user to run `/reincarnate:setup`. If every call fails with
an authentication error, tell the user to run `claude auth login` and check `claude auth status`.

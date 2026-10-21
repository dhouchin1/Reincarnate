---
description: "Browse and search your capability catalog, and vote capabilities up or down"
argument-hint: "[search text] [--portability high|medium|low] [--donor owner/repo] [--fit <project-slug>] [--gaps]"
allowed-tools: Bash, Read
---

Show the user their Reincarnate capability catalog. Arguments (optional): **$ARGUMENTS**

## 1. Build the query

Translate the arguments into `reincarnate catalog` flags. Free text becomes `--search`;
the rest map one-to-one:

| User said | Flag |
|---|---|
| free text | `--search "<text>"` |
| a portability level | `--portability high\|medium\|low` |
| a donor repo | `--donor owner/repo` |
| a project slug to fit against | `--fit <slug>` |
| "gaps" / what is missing | `--gaps` |

If no arguments were given, list everything. Always use `--json` so you can parse it:

```bash
reincarnate catalog --json
```

with the flags added, for example:

```bash
reincarnate catalog --search "rate limit" --portability high --json
```

If the command fails because `reincarnate` is missing, tell the user to run
`/reincarnate:setup`. If the catalog is empty, suggest `/reincarnate:comprehend owner/repo`.

## 2. Render a table

Render the rows as a compact markdown table, one row per capability, using the fields the
JSON actually provides (typically id, name, donor, license, portability, fit score or
best-fit project, and the user's vote). Do not invent columns. Sort by fit when a fit is
present, otherwise by donor then name. If there are more than about 25 rows, show the top
25 and say how many were omitted and how to narrow the query.

Flag, in one line under the table, any donor whose license appears in the profile's deny list
(`reincarnate profile show --json` -> `license_policy.deny`).

For `--gaps`, present the result as a list of needs that the user's projects have with no
comprehended donor yet, and offer to comprehend candidate donors.

## 3. Offer next moves

Ask whether the user wants to:

- **Vote** on any capability (this re-ranks future results). Run one command per capability:

```bash
reincarnate vote <capability-id> up
reincarnate vote <capability-id> down
reincarnate vote <capability-id> clear
```

- **Refine** the search or filters.
- **Compose** an app from what they see: `/reincarnate:compose "<idea>"`.
- **Salvage** one capability into a project: `/reincarnate:salvage <capability-id> --into <project>`.

Only run `vote` when the user names the capability and the direction; after voting, confirm
with the one-line result and offer to re-run the query so they see the re-ranking.

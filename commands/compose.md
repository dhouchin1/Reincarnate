---
description: "Compose an app from your catalog — you design the proposal, Reincarnate saves it"
argument-hint: "<the app idea, in prose>"
allowed-tools: Bash, Read
---

You are the composer. The user wants to build: **$ARGUMENTS**

Reincarnate retrieves candidate capabilities from the user's catalog; **you** (this session,
no subprocess) pick, order, and connect them, design the glue, and name the gaps. The
engine then stores the selection. If `$ARGUMENTS` is empty, ask for the idea first.

This stage never opens donor source. You work from catalog rows (names, behavior
descriptions, licenses, portability) only.

## 1. Retrieve

Pull out 3-6 distinctive keywords from the idea (nouns and capabilities, not filler) and
search. Run a few searches with different keyword sets if the first is thin:

```bash
reincarnate catalog --search "<keywords>" --json
```

Also fetch the profile and projects, which carry the license policy and stack:

```bash
reincarnate profile show --json
reincarnate project list --json
```

Optionally look at what the user's projects are missing:

```bash
reincarnate catalog --gaps --json
```

If `reincarnate` is missing, tell the user to run `/reincarnate:setup`. If retrieval returns
fewer than two usable capabilities, say so plainly: report the gap, suggest donors to
comprehend (`/reincarnate:comprehend owner/repo`), and stop rather than padding.

## 2. Design the proposal

Choose **2 to 8** capability ids from the retrieved rows. Only use ids that appeared in the
JSON you just read. For each, decide:

- **role**: what job it does in this app, in a few words.
- **order**: build order, foundational first (storage and core before UI and glue).

Then write, in prose:

- **Name and pitch** for the app (one line each).
- **Glue**: how the chosen capabilities connect: data flow, shared types, who calls whom,
  what each boundary looks like. Be concrete about the parts that are not any donor's
  behavior. Reference capabilities by name and id.
- **Gaps**: every need the idea implies that no chosen capability covers. For each gap,
  give the need, suggested donors to comprehend if you know good candidates (as
  `owner/repo`, labelled as suggestions you have not verified), and whether to build fresh.
- **Risks**: portability concerns (`low` portability, security notes from the rows),
  integration risks.
- **License check**: for each chosen capability's donor, compare its license to the
  profile's `license_policy`: any license in `deny` is a **blocker** (drop the capability
  or flag it loudly); any in `warn` (including `unknown`) is a **warning**.

Do not invent catalog capabilities; do not describe behavior the rows do not support.

## 3. Save the proposal

Write your design as a JSON object and hand it to the engine with the `session` engine. The
engine validates it deterministically (drops ids not in the catalog, renumbers order,
computes license flags from the profile policy, records problems) and stores it. Use a
quoted heredoc so nothing is expanded:

```bash
F="${TMPDIR:-/tmp}/reincarnate-proposal.json"
cat > "$F" <<'REINCARNATE_EOF'
{
  "name": "<app name>",
  "pitch": "<one line>",
  "capabilities": [
    {"capability_id": <id>, "role": "<job in this app>", "order": 1},
    {"capability_id": <id>, "role": "<job in this app>", "order": 2}
  ],
  "glue": "<markdown: data flow, shared types, who calls whom, the parts that are no donor's behavior>",
  "gaps": [
    {"need": "<what the idea needs that no capability covers>",
     "suggested_donors": ["owner/repo"], "build_fresh": true}
  ],
  "risks": ["<portability or integration risk>"]
}
REINCARNATE_EOF
python3 -m json.tool "$F" > /dev/null && echo "valid json"
reincarnate compose "<the idea>" --engine session --proposal "$F" --json
```

Add `--target <project-slug>` or `--target new:<name>` only if the user stated a target.
Read the JSON result: note the composition `slug` and `id`, and read `proposal.problems`
and `proposal.license_flags`. Anything the engine dropped or flagged must be reported to
the user exactly as recorded; do not restate your own version of the design as what was
saved. Everything in the file (roles, order, glue, gaps, risks, name, pitch) is persisted
and `reincarnate kit` writes it into `BLUEPRINT.md`.

## 4. Present and ask

Show the proposal: the ordered capability table (id, name, donor, license, role), the glue,
the gaps, the risks and license flags, and the saved composition slug. Then ask the user
what they want next:

1. **Edit**: add or drop capabilities, change roles, order, glue, or gaps (rewrite the
   file and re-run step 3; a re-run creates a new composition slug, so say which one is
   current).
2. **Save**: leave it as saved (`reincarnate compose list`, `reincarnate compose show <slug>`).
3. **Export a kit**: `/reincarnate:kit <slug> <target>`.

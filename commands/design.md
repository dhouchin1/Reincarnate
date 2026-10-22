---
description: "Design 2-4 distinct apps from your catalog (Lean, Complete, Alternative, Bold), check how the parts wire together, then pick one to build"
argument-hint: "<what you want to build, in prose>"
allowed-tools: Bash, Read
---

You are the designer. The user wants to build: **$ARGUMENTS**

If `$ARGUMENTS` is empty, ask what they want to build first. If `reincarnate` is missing, tell
the user to run `/reincarnate:setup` and stop.

You design from catalog rows only: names, behavior, licenses, portability, novelty, and
interface tokens. You never open donor source in this command.

## 1. Gather the pool

```bash
reincarnate profile show --json
reincarnate project list --json
reincarnate catalog --search "<3-6 distinctive keywords from the idea>" --json
```

Run two or three searches with different keyword sets if the first is thin. Each catalog item
has `capability_id`, `donor`, `license`, `name`, `what`, `portability`, `novelty`, `provides`,
and `requires`. Refer to capabilities as `c<capability_id>` (for example `c17`) everywhere in
your design.

Skip `novelty: "commodity"` items: the building agent writes those from scratch, so they are
glue, not parts. Use them only if the user asks.

## 2. Design

1. Break the idea into 6-12 concrete, testable requirements, `r1`, `r2`, and so on.
2. Propose 2-4 options, one per slot:
   - `lean`: the fewest capabilities that still make a usable first version.
   - `complete`: covers the most requirements straight from the catalog.
   - `alternative`: built on a different core capability or donor than the others.
   - `bold`: a more ambitious product angle on the same goal.
3. Each option uses 2-8 capabilities, chosen for how well they combine. Prefer options where
   one part's `requires` token is another part's `provides` token. Options must differ
   materially in which capabilities they use.
4. For each option, label every requirement as `catalog` (delivered mainly by one of that
   option's capabilities; give its id), `glue` (integration code between them), or `net-new`
   (nothing in the option serves it).
5. List the glue components and the net-new work for each option.

Check every donor license against the profile's `license_policy`. A license in `deny` is a
blocker; say so for any option that uses it.

## 3. Save the design

Write the design with a quoted heredoc, then hand it to the engine. The engine drops unknown
ids, near-duplicate options, and coverage labels that point outside an option. It then
computes the wiring between parts, the license and security flags, and the reuse and fit
scores.

```bash
F="${TMPDIR:-/tmp}/reincarnate-design.json"
cat > "$F" <<'REINCARNATE_EOF'
{
  "title": "<short project name>",
  "requirements": [{"id": "r1", "text": "<testable requirement>"}],
  "options": [
    {"slot": "lean", "name": "<product-style name>", "pitch": "<who uses it and why>",
     "rationale": "<why these parts fit together>", "stack": "<e.g. Python + FastAPI + SQLite>",
     "capabilities": [{"id": "c17", "role": "<what it does in this option>"}],
     "coverage": [{"req": "r1", "kind": "catalog", "cap": "c17"}],
     "glue": ["<integration component>"], "new_work": ["<net-new work>"]}
  ]
}
REINCARNATE_EOF
python3 -m json.tool "$F" > /dev/null && echo "valid json"
reincarnate design "<the idea>" --engine session --proposal "$F" --json
```

## 4. Present

Read the saved design from the JSON result: `slug`, then for each option its `scores`
(`reuse` and `fit`), `wiring` counts (wired, unmet, settings, external, owned), `flags`, and
anything listed under `dropped`. Report exactly what the engine recorded. Do not restate your
own draft as the result.

Show a compact comparison table: slot, name, parts, reuse, fit, unmet needs, flags. Below it,
give one line per option on its trade-off. For unmet needs that list catalog `candidates`,
name them as possible fillers.

## 5. Pick and build

Ask which option to build and where (an existing project slug, or `new:<name>`). Then:

```bash
reincarnate design pick <design-slug> <slot> --target <slug-or-new:name> --json
```

This creates a regular composition carrying the option's parts, roles, glue, gaps from the
net-new work and unmet wiring, and risks from its flags. Continue with
`/reincarnate:kit <composition-slug> <target>`.

# Kit dogfood: can a builder build from the kit alone?

Protocol (both runs): a context-free Sonnet builder may read only the kit's `.reincarnate/`
files and its own work. It cannot ask questions. Every question it would have asked is
logged with the choice it made. Donor source is never on disk.

| | v1 (2026-10-22, kit generator v0.2) | v2 (2026-10-22, v0.3 + acceptance backfill) |
|---|---|---|
| Kit | `.local-data/new/homelab-forge` | `.local-data/new/homelab-forge-v2` |
| Steps built | 1 (the grid) | 0 scaffold, 1 board config persistence, 2 the grid |
| Stack built | stdlib Python + plain TypeScript (the kit's stack line was unusable) | Next.js + tRPC + TypeScript, as the design chose |
| What exists | a layout model and a pointer-drag fragment; no app, no API | a running app: tRPC board API, file store, grid component |
| Tests | 19 Python + 10 TypeScript | 18 (`npm test`), `tsc` clean, `next build` succeeds |
| Questions for the grid | 12 | 9 |
| Dependencies | none (the rule forbade all) | 13, all stack items or their types and peers, in `DEPENDENCIES.md` |

## v1's 12 grid questions, re-checked against v2

| v1 question | v2 |
|---|---|
| Which stack, given "no new dependencies" in an empty repo | Gone. Step 0 and the dependency policy answered it |
| Test runner and command | Gone. `npm test`, set up in step 0 |
| Does step 1 own persistence, given a later store step | Gone. Board Config Persistence was auto-filled as step 1 |
| JSON shape: flat or nested | Gone. Step 1's board file defines a per-tile `shape` |
| Malformed persisted data | Moved to step 1, where the builder validates the board file |
| What is a tile | Mostly gone. Tiles are the board's apps and widgets; v2 asks only whether they share one grid |
| Tile missing a breakpoint entry | Narrower. An acceptance check now requires a valid position instead of failure; the strategy is still open |
| GridStack versus any library | Still asked, but the policy now answers it |
| Breakpoint widths and column counts | Still open. The check says "narrower than the small breakpoint threshold" and passes with any value |
| Out-of-bounds positions | Still open |
| Overlaps | Still open |
| Who wins a drag collision | Still open |

New in v2: which board the page shows, whether edits wait for auth (step 5), and whether
the grid needs an edit mode.

## What v0.3 fixed

Every question the kit generator itself caused is gone: stack, dependencies, test command,
step order, and where persistence lives. The remaining grid questions are about behavior
the capability's description never pinned down (thresholds, bounds, collisions).

## Defects the v2 run found

1. **Data shapes are pointed at, not described.** Step 1 says the schema is in "a donor source
   file". The dossier pointed at the donor's example config instead of describing it, and the
   path scrub correctly removed the pointer. The builder invented every field below the
   top-level keys, and later steps must now guess those names. Fix in comprehension: describe
   data shapes field by field wherever the behavior depends on them.
2. **Acceptance checks leave numbers vague.** "Narrower than the small breakpoint threshold"
   passes with any value. Fix: the acceptance prompt should require the concrete values
   (sizes, limits, thresholds) whenever the behavior depends on them.
3. **The stack names an ORM but no driver.** "SQLite (Drizzle)" needs a SQLite driver, and the
   builder could not tell whether one counts as the stack. Fix: the dependency policy should
   say that types, peers, and an ORM's driver are part of the stack item they serve.
4. **UI behavior is untestable in the stack.** Every grid check covers data; the drag component
   has no test because no DOM test tool is in the stack. Fix: step 0 should include the
   stack's standard UI test tooling when the stack has a UI framework.
5. **Glue predates auto-fill.** The blueprint's glue still says a "layout-config table" backs
   the grid, while the auto-filled step 1 stores JSON files. Fix: when auto-fill satisfies a
   need, annotate or rewrite the glue line for it.
6. **Order ignores cross-cutting needs.** Auth is step 5, so the step-2 board API is
   unauthenticated, and the admin service editor (requirement r10) has no step before 10.
   The wiring check cannot see this, because the grid does not declare an auth need.

## v0.4 fixes for those six defects (2026-10-22)

Decisions (Dan): deepen on use; guard-seam notes, order unchanged; one step per glue
requirement.

1. **Shapes and numbers from donor code.** `reincarnate deepen` (and `kit --deepen`) runs one
   isolated, read-only Haiku pass over each part's donor code and records field-level data
   shapes, key values in plain words, and acceptance checks with those values. For the nine
   parts of this kit: about $0.11 and 8 minutes. The grid now records md at 768 px, lg at
   1400 px, a separate 800 px column threshold, 3, 6 and 12 columns, 128 px rows, and that
   dragging is off outside edit mode; v2's builder had guessed 640, 1024 and 3, 6, 12.
   Comprehension prompts now ask for the same fields on every new repo.
2. **Concrete numbers.** Checks from donor code carry the values; catalog-only checks must
   write `[value unknown: <what>]` instead of inventing one.
3. **Types, peers and drivers count as the stack item they serve**, listed under it in
   `DEPENDENCIES.md`.
4. **UI test tooling in step 0** for UI stacks (React and Next.js: Testing Library with jsdom),
   with a component smoke check; parts that provide a `ui:` token must have component tests.
5. **Glue after auto-fill.** Glue lines about an auto-filled need are replaced by the part that
   provides it, plus a supersede line.
6. **Auth guard seam.** Step 0 creates one allow-all authorization guard; steps before the auth
   step route their endpoints through it; the auth step wires it and must reject an anonymous
   request to an earlier endpoint.
7. **Glue steps.** Each glue-only requirement is a step right after the parts its glue lines
   name (word overlap is the fallback). Here: Docker controls and the admin service editor
   follow Typed Integration Framework, and the alerts step follows all parts.

Found and fixed during the work: key values first came back using the donor library's option
names (`staticGrid`, a `--gridstack-` CSS variable); the deepen prompt now requires plain
behavioral words, and re-deepening removed them.

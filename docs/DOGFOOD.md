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

## v3 rebuild: steps 0-5 on the v0.4 kit (2026-10-22)

Same protocol, run to step 5 so the auth guard seam is exercised. The kit was exported before
the truncation fix below, so the builder saw cut-off checks; it logged them.

| | v1 | v2 | v3 |
|---|---|---|---|
| Kit generator | v0.2 | v0.3 + acceptance backfill | v0.4, all parts deepened |
| Steps built | 1 | 0-2 | 0-5 |
| Result | layout fragment, wrong stack | Next.js app, grid and store | Next.js app through auth: grid, store, health ping, icon library, local/LDAP/OIDC login |
| Tests | 29 | 18 | 172 (15 files), `tsc` clean |
| Dependencies | 0 | 13 | 20; extras logged: `vitest`, `ldapts` |
| Grid questions | 12 | 9 | 13 |
| Questions, steps 0-2 | n/a | 25 (7, 9, 9) | 34 (7, 14, 13) |

**The count went up, and that is the expected direction.** Richer specs describe more of the
donor's behavior, so the builder asks about finer details. Of v2's nine grid questions, the
five foundational ones are gone: breakpoints and column counts, edit mode, how layout
persists, what happens to a tile missing a breakpoint, and whether apps and widgets share a
grid. v3's grid questions are about details v2's spec never reached (cross-area drag,
sidebars, gap math) plus four leftovers: library choice, minimum tile size, the auto-placement
algorithm, and collisions. Question count is a poor metric once specs get rich; the share of
foundational questions is the useful one.

**The guard seam worked end to end.** Step 0 created one `authorize(ctx, level)` guard and
three procedure tiers. Steps 1-4 declared every route with a tier while the guard allowed
everything. Step 5 changed only the guard's body, the request context, and its type, with no
edits to any earlier route, and tests show anonymous calls to every pre-step-5 endpoint are
rejected. One gap: the guard covers tRPC procedures only, so non-tRPC routes must call it
themselves.

**Air gap held.** Donor clones were on disk for this run (from `deepen`). No builder tool call
touched the sandbox.

## Defects found in v3

1. **Truncated checks and values (fixed in v0.4.1).** The first caps (80, 240, 300 characters)
   cut checks, URLs and values mid-word: Auth lost the end of all 5 checks, Icon Library 4 of 5
   and 8 of 20 values. Caps are now 600, 300 and 400 characters with a word-boundary cut and a
   visible " [truncated]" marker; re-deepening the five affected parts ($0.05) left none cut.
2. **No interface contract between client and server.** Every run invented its own transport
   and endpoint names (v2's board router, v3's `board.moveTile` and `auth.login`). The kit
   should fix the API style in step 0 and have each spec list the procedures its part exposes.
3. **The donor's mechanism leaks through behavior.** The grid spec is phrased in its donor
   library's change-event model ("multi-node change event"), and the auth spec around its
   donor's auth library, including a bcrypt cost factor. Deepen should state outcomes and
   requirements (salted, slow password hashing) and keep only persisted formats verbatim.
4. **No configuration contract.** Environment variable names are unspecified except one, so the
   builder invented `AUTH_LDAP_*` and `AUTH_OIDC_*`. The kit should name config keys once, from
   the parts' `config:` tokens.
5. **Deepened scope exceeds the design.** Deepen describes everything the donor part does
   (sidebars, categories, cross-area drag), not just what the requirements need. Passing the
   requirements a part covers into the deepen prompt would bound it.

Environment note: the run filled the disk (two Next.js `node_modules` at 400-500 MB each plus
donor clones, on a disk already at 99 percent). The builder cleared the npm cache to finish an
install, which regenerates on its own; session scratch copies (1.2 GB) were deleted.

# Reincarnate — product concept and MVP cut

*Drafted 2026-10-21 from the second-brain Forge/Reincarnate pipeline, the July 2026
"public repo-comprehension tool" plan (`vault/10-Projects/repocomprehendproductplan_v2.md`),
and the Forge App Composer design (`vault/10-Projects/forge-app-composer/README.md`).*

---

## 0. One-paragraph pitch

**Reincarnate is a personal parts bin for software behavior.** You tell it who you are
(your stack, your projects, what you are trying to build). You feed it repositories you
admire. It reads each one clean-room and extracts *capabilities*, not code: what the
thing does, how portable that behavior is, and how it would fit *your* projects. Those
capabilities accumulate into a catalog tuned to you. From the catalog you compose new
applications, and Reincarnate hands you a buildable, clean-room blueprint that your own
coding agent (Claude Code today) can execute. Nothing is copied, ever. Behavior is
reborn in your codebase, in your stack, under your license.

The one-liner: **"Comprehend any repo. Keep the ideas. Rebuild them as yours."**

---

## 1. What already exists and carries over

Everything below is your own code in `~/dan_brain/second-brain`, so it can be lifted
directly (no clean-room constraint applies to your own repo). The table is the honest
inventory of what is a port versus a new build.

| Piece | second-brain today | Reincarnate | Status |
|---|---|---|---|
| Comprehension prompt + dossier JSON schema | `repo_comprehend.py` (`build_comprehension_prompt`, `_SCHEMA_HINT`) | Same schema: `capabilities[{name, what, files, dependencies, portability, portability_notes}]`, `dependencies`, `security`, `fit_to_fleet`, `salvage_recommendations` | **Port** |
| Read-only sandboxed agent pass | shells out to `claude -p --tools Read,Grep,Glob` | Local mode: same. Hosted mode: own tool loop on the Agent SDK / Messages API (July plan §4) | Port + new |
| Catalog (flatten dossiers, search, filter by portability/fit) | `repo_catalog.py` | Same, but backed by a DB instead of vault notes, and scored against a *profile* instead of a hardcoded fleet | **Port, re-scored** |
| Salvage one capability → clean-room task spec | `repo_salvage.build_clean_room_spec` | The unit of export. Unchanged in spirit | **Port** |
| Reincarnate whole repo → ordered blueprint | `repo_reincarnate.py` | One of three composition modes | **Port** |
| Compose capabilities across donors into an app | `repo_compose.py` (`propose_compositions`, `emit_app_blueprint`) | The centerpiece. Becomes interactive instead of batch | **Port, made interactive** |
| Target bridge (adopt / create GitHub repo via `gh`) | `repo_target.py` | Same, per user | Port |
| Forge (builds tasks in-repo behind review + merge) | `forge/*` | **Not ported.** Reincarnate exports a kit; the user's own agent builds | Deliberately dropped |
| 5-step wizard UI | `RepoReincarnationView.tsx` | Reshaped around profile → catalog → studio | Rewrite |
| Cost ledger, rate limits, cache-by-SHA, kill switch | designed in July plan §5–7, not built | Needed only for hosted mode | New |

The single most important design carry-over: **the air gap**. The donor's source is
read and referenced, never copied; blueprints specify behavior and say "do not copy."
That is the legal and ethical spine of the product and the name is a promise about it.

---

## 2. The core model (what is new)

### 2.1 Profile replaces "the fleet"

In second-brain, `fit_to_fleet` maps every capability onto Dan's projects. Reincarnate
generalizes that: a **profile** is the thing capabilities are scored against.

```
Profile
  ├─ stack        languages, frameworks, runtimes, deployment targets, package managers
  ├─ projects[]   the user's own repos (adopted via gh) with inferred stack + a one-line goal each
  ├─ intents[]    free-text "things I want to build" (a wishlist, used as retrieval queries)
  ├─ constraints  license policy (e.g. "no GPL donors"), size/complexity appetite, security posture
  └─ taste        tags the user up/down-votes while browsing (feeds re-ranking)
```

### 2.2 Two-layer comprehension (the cost trick)

A dossier is expensive (one agentic pass over a clone). A profile is cheap to apply.
Split them:

1. **Base dossier** — profile-agnostic, keyed by `(owner/repo, commit SHA)`. Cached and
   shareable across every user. This is the July plan's cache layer.
2. **Profile overlay** — a second, short, non-agentic LLM call (or in the MVP a
   deterministic + embedding re-rank) that produces: fit scores against *your* projects,
   portability notes rewritten for *your* stack ("in FastAPI this is one dependency and
   a middleware"), and a "why you'd want this" line. Cheap, per user, regenerated when
   the profile changes.

Ten users comprehending the same trending repo cost one agentic run plus ten overlays.

### 2.3 Catalog as a personal, searchable parts bin

Same as `repo_catalog` but every row carries the overlay: fit to *your* projects,
portability *for your stack*, your votes. Views: by donor, by project it fits, by gap
("capabilities your projects need that you have not comprehended a donor for").

### 2.4 Studio: three ways to compose

| Mode | Input | Output | Existing analogue |
|---|---|---|---|
| **Salvage** | one capability + one of your projects | one clean-room task spec | `repo_salvage` |
| **Reincarnate** | one donor + a target (new or existing) | ordered multi-step blueprint, foundational-first | `repo_reincarnate` |
| **Compose** | an idea in prose, optionally a hand-picked set of capabilities | an app proposal: capabilities with roles, glue the LLM must design, **explicit gaps** ("no auth capability in your catalog; here are three donors to comprehend, or build fresh"), license screen | `repo_compose`, made interactive |

The compose loop is the product moment: *describe the app → Reincarnate retrieves from
your catalog → proposes → you edit → it emits a kit.*

### 2.5 The kit (the deliverable)

Instead of hosting a Forge, Reincarnate exports a **Claude Code-ready kit** into the
target repo (or a zip):

```
.reincarnate/
  BLUEPRINT.md        human-readable plan, donors, licenses, the air-gap statement
  CLAUDE.md           stack conventions from the profile + the clean-room rules
  tasks.md            ordered `- [ ]` steps, one per capability, each self-contained
  specs/<step>.md     build_clean_room_spec output per step
  run.sh              `claude -p` driver: builds step N, runs tests, stops for review
```

The user's own agent and subscription do the building. This is the July plan's
"bring your own compute" idea applied to the *build* side, and it removes the entire
hosted-build cost and abuse surface from the MVP.

**Test drive** (Phase 2, hosted): a capped, sandboxed one-shot `claude -p` run that
produces a throwaway prototype of one composed app so a user can see it work before
committing. Hard budget cap, no persistence, cached by blueprint hash.

---

## 3. Alternate framings worth considering

The core above is the safe bet. These are different products built on the same engine.
Each one changes who the customer is.

### A. Capability résumé (portfolio-first)
Point Reincarnate at **your own repos** first. The dossiers become a capability résumé:
"here is what I have actually built, with portability scores," rendered as a public
portfolio page with share cards. Twist: paste a job description and get a **gap list**,
then a composed "build this to close the gap" project. Customer: individual devs, job
seekers, bootcamp grads. Growth: every portfolio page is a referral. This matches the
word "portfolio" in the original brief most literally.

### B. Unbundle (dependency replacement advisor)
Point it at your repo's `package.json` / `pyproject.toml`. For each dependency it asks:
is there a comprehended, high-portability equivalent in the catalog, and is this
dependency small enough to own? It offers a clean-room blueprint to drop the dependency
and vendor the behavior. Customer: anyone burned by supply-chain incidents, teams with
a "minimize deps" policy. Very topical; the security review already in the dossier is
the selling point. Could be the first paid feature.

### C. Kitbash (composer-first, visual)
The catalog is a parts bin, the studio is a canvas: drag capabilities on, the LLM draws
the glue between them, gaps light up. Emits the kit. Customer: solo builders and indie
hackers who want to prototype fast without pulling in forty dependencies. This is the
closest to the original ask and the most fun to demo.

### D. Rebuild-it-yourself (learning paths)
Pick a repo you admire. The dossier becomes a curriculum: rebuild each capability
clean-room, foundational-first, with acceptance checks per step generated from the
donor's behavior. "Build your own Redis" for any repo, auto-generated and cached once
per donor. Customer: learners, educators. Cheapest to run per user because the expensive
pass is shared.

### E. Org catalog (what have we already built?)
Comprehend every internal repo in an org. Catalog becomes the answer to "has anyone here
already built a rate limiter?" with fit to each team's stack. Customer: platform teams.
Enterprise money, but needs private-repo auth, SSO, and a different trust story.

### F. Blueprint exchange
Users publish composed blueprints (not code) and others fork them into their own stack.
Because blueprints are behavior specs, a Go user can fork a blueprint a TypeScript user
wrote. Growth loop for any of the above.

**My pick:** build **C (Kitbash-style studio)** as the core, ship **A (capability
résumé)** as the free shareable surface because it needs no extra engine work (it is a
dossier of your own repo plus a rendered page), and hold **B (Unbundle)** as the first
paid feature. D, E and F are later bets on the same engine.

---

## 4. Should "Reincarnate" be the name?

It can carry a product. Arguments for: it is memorable, it is a verb, and the metaphor is
exact. A repo's behavior dies in one body and is reborn in yours, clean. The tagline
writes itself and the air-gap promise is baked into the word.

Risks to weigh: spiritual connotation may read oddly in enterprise contexts (less so for
indie devs), it is long to type as a CLI (`reinc`? `rnc`?), and domain/handle
availability is unverified from this session. Alternatives if it fails those checks:
**Kitbash** (the hobbyist term for building a model from parts of other kits, which is
exactly the compose mode), **Salvage**, **Reforge**, **Afterlife**, **Parts Bin**.
Recommendation: keep Reincarnate for the product, use `kitbash` as the name of the
compose studio inside it, and check domains before any public surface.

---

## 5. MVP cut

Goal: a single-user, local-first app that runs on a developer's machine with their own
Claude Code auth. Zero hosted cost, zero abuse surface, real value on day one. Hosted
multi-tenant comes after, reusing the July plan's edge/queue/ledger design.

### In the MVP
1. **Profile** — stack picker, adopt own repos via `gh`, free-text intents, license
   policy. Stored in SQLite.
2. **Comprehend** — paste a donor URL; shallow clone into a sandbox, hooks off; run the
   existing read-only `claude -p` pass with the ported prompt/schema; store base dossier
   keyed by SHA. Profile overlay as a short second `claude -p` call.
3. **Catalog** — searchable, filter by portability / donor / fits-project / gap, thumbs
   up/down that re-rank.
4. **Studio: compose** — prose idea in, retrieval over the catalog (FTS plus embeddings
   if cheap), LLM proposal with explicit gaps, inline edit, emit.
5. **Kit export** — writes `.reincarnate/` into a target repo (adopt or create via `gh`)
   or downloads a zip. `run.sh` drives `claude -p` step by step with a stop-for-review
   gate.
6. **Portfolio page** — comprehend your own repos, render a static capability résumé
   you can publish anywhere (static HTML, no hosting needed).

### Explicitly out of the MVP
Hosted builds, multi-provider agent loop, accounts, billing, test-drive sandbox, org
mode, blueprint exchange. Each is a known design (July plan) and none is needed to
prove the loop.

### Stack
FastAPI + SQLite + a React SPA, the shape second-brain already has, minus the vault.
Dossiers live in the DB with a markdown export so Obsidian users keep their notes.
`claude -p` is the only LLM transport in the MVP; the `ModelBackend` interface from the
July plan is stubbed so the hosted backends slot in later.

### What proves the MVP
A stranger with their own Claude Code can: set up a profile in two minutes, comprehend
three repos, compose an app from capabilities across all three, export the kit into a
fresh repo, and have their agent build step one behind a review gate. If that loop
works end to end on someone else's machine, the engine is a product.

---

## 6. Rough sequence

| Phase | Weeks | Deliverable |
|---|---|---|
| 0 | 1 | Extract the comprehend prompt/schema, catalog, salvage spec, compose and target modules out of second-brain into a standalone package with tests. No vault dependency. |
| 1 | 2 | Profile + two-layer comprehension + catalog UI. Dogfood on the 36 dossiers you already have (import them). |
| 2 | 2 | Compose studio + kit export + `run.sh`. Build one real app from your own catalog with it. |
| 3 | 1 | Portfolio page + README + a short demo. Private beta with two or three people who have Claude Code. |
| 4 | later | Hosted mode per the July plan: edge gate, queue, shared dossier cache, ledger, kill switch, then test-drive. |

---

## 7. Open decisions (yours)

1. **Local-first MVP vs hosted-first.** This doc assumes local-first. Hosted-first is
   more product-like but front-loads the cost/abuse work from the July plan.
2. **Which framing leads:** composer (C), portfolio (A), or dependency replacement (B).
   The engine is the same; the landing page and first user are not.
3. **Name.** Keep Reincarnate, or Kitbash.
4. **Import the existing 36 dossiers** as seed content for dogfooding and demos. I
   assume yes; they are the fastest way to a convincing catalog.

# Comprehension cost: measurements and the tiered pipeline

Measured 2026-10-22 on `pallets/itsdangerous` at commit `672971d6`, a small, well-documented
Python library. Costs are the `total_cost_usd` that `claude -p` reports. One repo is a small
sample: treat these as orders of magnitude until the 10-repo eval below is run.

## What actually costs money

Every `claude -p` call carried about **243,000 tokens of fixed context** before the prompt:
the caller's user CLAUDE.md, auto-memory, every MCP server's tool schemas, and the skills
list. A one-word reply cost $0.0169 on Haiku. An agentic run re-reads that context on every
turn, so it dominated every comprehension.

The same call with `ISOLATION_FLAGS` (`claude_runner.py`) carries about 3,600 tokens and costs
$0.0006. Isolation also closes a prompt-injection channel: comprehension runs with the donor
clone as its working directory, and without `--setting-sources ""` the donor repo's own
`CLAUDE.md` and `.claude/settings.json` were loaded into the session.

| Variant | Before isolation | Isolated | Seconds (isolated) |
|---|---|---|---|
| Agentic, Sonnet 5.5 (`--tools Read,Grep,Glob`) | $1.243 | $0.176 | 54 |
| Agentic, Haiku 5.5 | $0.388 | $0.016 | 81 |
| Ranked excerpt + one call, Sonnet 5.5 | $1.083 | $0.161 | 44 |
| Ranked excerpt + one call, Haiku 5.5 | $0.293 | $0.015 | 86 |

Quality held across all variants on this repo. Every dossier found the same 6 to 8
capabilities (HMAC signing, key derivation and rotation, timestamped expiry, serializer over
a signer, URL-safe compact tokens, typed error hierarchy). Every cited file path exists in
the clone (100 percent in all five dossiers). Every one got the license right (BSD-3-Clause).
Haiku wrote longer descriptions and notes than Sonnet. Haiku's agentic run used kebab-case
capability names, so names need normalizing.

**Bottom line:** isolated Haiku comprehension is about 80 times cheaper than the first live
run. At roughly $0.015 per small repo, 1,000 repos is about $15 instead of about $1,240.
Larger repos will cost more in proportion to the excerpt budget, not the turn count.

## The tiered pipeline (first principles)

The catalog's value is coverage times accuracy times interoperability. Most entries are
never used in a composition, so pay for accuracy only where it is consumed.

| Tier | What | Model | When it runs |
|---|---|---|---|
| 0 | Clone, rank files, parse manifests, detect SPDX license, languages, size, dedupe by SHA | none | every repo |
| 1 | Extract capabilities from a ranked excerpt in one call | Haiku 5.5, isolated | every repo |
| 2 | Typed normalization: canonical interface tokens, portability and novelty rubrics, with confidence | Jev (typed decisions) or batched Haiku | every capability |
| 3 | Review and correct one dossier against its excerpt | Sonnet 5.5 | only low-confidence items, and lazily when a capability is first selected into a composition |
| 4 | Compose an app over a shortlist | Opus 5.5 | per composition, over a Tier 2 shortlist |

Two ideas carry most of the leverage:

- **Review on use.** Tier 3 runs when a capability is about to be built from, not when it is
  catalogued. The cost of accuracy then tracks real demand.
- **Novelty as a first-class field.** Each capability gets a rubric score: `commodity`
  (a coding agent writes it from one prompt: retries, logging, config loading), `pattern`
  (known shape, real design choices), or `distinctive` (non-obvious behavior worth
  reincarnating). Commodity items stay out of compose by default. This is the "beyond
  boilerplate" bar as a filter, and it shrinks every downstream prompt.

## Where Jev fits

Jev (TypeSafe's typed decision model, client in second-brain branch
`feat/intent-compose-jev`, `sb/src/sb/llm/jev.py`) answers yes/no, choice (up to 255 options),
and rubric-score questions with probabilities, and never writes text. That matches Tier 2
exactly: mapping a capability's provides/requires onto a canonical vocabulary, scoring
portability and novelty, and flagging license risk. Its probabilities are the confidence that
decides what escalates to Tier 3. It also fits the compose shortlist, where the October 5
prototype showed the speedup comes from shrinking the Opus prompt.

Not verified: the Jev client has only run against mocked HTTP, because no TypeSafe key is
configured. Its wire format follows docs.typesafe.ai as of 2026-10-22.

## v0.2 live measurements (2026-10-22, full 351-capability catalog)

| Step | Model | Result | Cost | Time |
|---|---|---|---|---|
| Excerpt comprehension, itsdangerous | Haiku 5.5 | 8 capabilities, novelty and interfaces on all | $0.0105 | about 60 s |
| Tier 2 enrich, all capabilities, 15 batches of 25 | Haiku 5.5 | 351 of 351 rated, 0 failed batches | $0.080 | 369 s |
| Design, 3 options, 120-part lexical pool, plus link verification | Opus 5.5 + Haiku 5.5 | 12 requirements, 3 distinct options | $0.347 | 70 s |
| Same design, 40-part shortlist | Opus 5.5 + Haiku 5.5 | similar reuse and fit scores | $0.197 | 66 s |

Novelty split after Tier 2: 123 commodity, 161 pattern, 67 distinctive. About a third of the
catalog is boilerplate a coding agent writes unaided, and designs now exclude it by default.

The 40-part run used the Jev stand-in to pick the shortlist, so it measures what a smaller
pool saves (about 43 percent of the design cost), not how well real Jev chooses. The real
Jev comparison needs a `TYPESAFE_API_KEY`.

## Next measurements

1. Ten-repo eval across sizes and languages (a 200-file TypeScript app, a Go service, a Rust
   crate, a large Python app), Haiku excerpt versus Sonnet agentic, scored on cited-path
   validity, capability overlap, and a blind Sonnet judgment.
2. Excerpt budget sweep (30k, 60k, 120k, 240k characters) on the two largest repos.
3. Tier 3 review cost and how often it changes a dossier materially.

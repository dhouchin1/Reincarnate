# Reincarnate

**Comprehend any repo. Keep the ideas. Rebuild them as yours.**

Reincarnate is a personal parts bin for software behavior. You tell it who you are (your
stack, your projects, what you are trying to build) and feed it repositories you admire.
It reads each one clean-room and extracts *capabilities*, not code: what the thing does,
how portable that behavior is, and how it would fit *your* projects. Those capabilities
accumulate into a catalog tuned to you.

From the catalog you compose new applications, and Reincarnate hands you a buildable,
clean-room blueprint that your own coding agent (Claude Code today) can execute. Nothing is
copied, ever. Behavior is reborn in your codebase, in your stack, under your license.

## Install

As a Claude Code plugin (this repo is its own marketplace):

```
/plugin marketplace add dhouchin1/Reincarnate
/plugin install reincarnate@reincarnate
/reincarnate:setup
```

`/reincarnate:setup` installs the bundled Python engine (`uv tool install`, falling back to
`pipx`) and walks you through your profile.

Or install just the engine CLI from a checkout (Python 3.11+, standard library only):

```
uv tool install .
reincarnate init
```

## Commands

| Command | What it does |
|---|---|
| `/reincarnate:setup` | Install the engine, create your profile, register the current project |
| `/reincarnate:comprehend <url or owner/repo>` | Read a repo clean-room in this session and add its capabilities to your catalog |
| `/reincarnate:catalog [filters]` | Browse and search the catalog; vote capabilities up or down |
| `/reincarnate:design <idea>` | Design 2-4 distinct apps (Lean, Complete, Alternative, Bold) from the catalog, with wiring between parts, reuse and fit scores, then pick one to build |
| `/reincarnate:compose <idea>` | Pick and connect capabilities into an app proposal, with explicit gaps and a license check |
| `/reincarnate:kit <composition> <target>` | Export a buildable `.reincarnate/` kit and optionally build step 1 right away |
| `/reincarnate:salvage <capability-id> --into <project>` | Turn one capability into a clean-room build spec for one of your projects |
| `/reincarnate:portfolio` | Comprehend your own repos and export a capability résumé |

The `clean-room` skill holds the building rules the kit, salvage, and compose flows rely on.

## How the catalog stays cheap

Comprehension is tiered so accuracy is paid for only where a capability is used
(measurements in [docs/COST.md](docs/COST.md)):

1. **No model:** clone, rank files, detect the license, skip a commit already catalogued.
2. **Haiku 5.5, one isolated call:** extract capabilities from a ranked excerpt (about $0.01
   per small repo).
3. **Tier 2:** `reincarnate enrich` rates each capability's novelty (commodity, pattern,
   distinctive) and maps its interface tokens onto a shared vocabulary, with Haiku in
   batches or with [Jev](https://docs.typesafe.ai) (`--engine jev`), whose calibrated
   confidence flags uncertain capabilities.
4. **Tier 3:** `reincarnate review` sends only flagged capabilities to Sonnet.
5. **Design:** Opus composes over a shortlist (`--shortlist jev` re-ranks it with Jev).

Commodity capabilities, which a coding agent writes from a one-line prompt, stay out of
designs by default. The catalog is for the parts an agent would not invent on its own.

## The air gap

Only the comprehension step reads a donor's source, and it reads it read-only, in a
sandbox, with nothing executed. Everything downstream (salvage specs, compositions, kits)
contains behavior descriptions and provenance, never donor file contents; donor paths appear
only on "reference only, DO NOT COPY" lines. Every kit carries a `BLUEPRINT.md` listing each
donor and its license, plus clean-room rules for the agent that builds it.

## Status

Pre-alpha, local-first. Everything runs on your machine with your own Claude Code; data lives
in `~/.reincarnate/` (or `$REINCARNATE_HOME`). No accounts, no hosted service. See
`CONCEPT.md` for the product thinking and `DESIGN.md` for the engine contract.

## License

MIT. See `LICENSE`.

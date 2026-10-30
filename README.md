# Reincarnate

**Comprehend any repo. Keep the ideas. Rebuild them as yours.**

Reincarnate is a personal parts bin for software behavior. You tell it who you are (your
stack, your projects, what you are trying to build) and feed it repositories you admire.
It reads each one clean-room and extracts *capabilities*, not code: what the thing does,
how portable that behavior is, and how it would fit *your* projects. Those capabilities
accumulate into a catalog tuned to you.

From the catalog you compose new applications, and Reincarnate hands you a buildable,
clean-room blueprint that your own coding agent (Claude Code today) can execute. The catalog
and the blueprints hold descriptions of behavior, not source code, so what gets built is new
code in your codebase, in your stack, under your license.

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
| `/reincarnate:design <idea>` | Design distinct apps from the catalog (3 by default: Lean, Complete, Alternative; ask for 4 to add Bold), with wiring between parts, reuse and fit scores, then pick one to build |
| `/reincarnate:compose <idea>` | Pick and connect capabilities into an app proposal, with explicit gaps and a license check |
| `/reincarnate:kit <composition> <target>` | Export a buildable `.reincarnate/` kit and optionally build step 1 right away |
| `/reincarnate:build [step]` | Build the next kit step in this session under the clean-room rules, then run its tests and Code Read check (`run.sh --begin/--check`) and stop for review |
| `/reincarnate:salvage <capability-id> --into <project>` | Turn one capability into a clean-room build spec for one of your projects |
| `/reincarnate:portfolio` | Comprehend your own repos and export a capability résumé |
| `/reincarnate:studio` | Open the local studio page (`reincarnate serve`): add repos with a cost estimate first, watch each one being read, see your catalog's health and upkeep |

The `clean-room` skill holds the building rules the kit, salvage, and compose flows rely on.

## A first run

In a Claude Code session, inside a project of yours:

```
/reincarnate:setup                          # installs the engine, sets up your profile and this project
/reincarnate:comprehend pallets/itsdangerous
/reincarnate:catalog signing
```

`comprehend` clones the repo into a sandbox, ranks its files, and makes one isolated Haiku call.
On `pallets/itsdangerous` that found six to eight capabilities (HMAC signing, key rotation,
timestamped expiry, URL-safe tokens and so on) for about $0.015. `catalog` lists them with their
license and how well each fits your projects. From there, `/reincarnate:design` turns an idea
into app designs built from your catalog.

## How the catalog stays cheap

Comprehension is tiered so accuracy is paid for only where a capability is used
(measurements in [docs/COST.md](docs/COST.md)):

1. **No model:** clone, rank files, detect the license, skip a commit already catalogued.
2. **Haiku 5.5, one isolated call:** extract capabilities from a ranked excerpt (about $0.01
   per small repo).
3. **Tier 2:** `reincarnate enrich` rates each capability's novelty (commodity, pattern,
   distinctive) and maps its interface tokens onto a shared vocabulary, with Haiku in
   batches or with [Jev](https://docs.typesafe.ai) (`--engine jev`), whose calibrated
   confidence flags uncertain capabilities. `--engine jev` (and `--shortlist jev`) sends
   capability names, descriptions, notes and interface tokens to api.typesafe.ai; by default
   it skips capabilities whose license is warn/deny, proprietary or unknown, or whose donor is
   your own repo (`enrich --include-private` sends them too; the design shortlist never does). Everything else that uses a model
   (`claude -p`) goes to Anthropic through your own Claude login. Nothing else leaves the
   machine.
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

## Is this code laundering?

No, and the design tries hard to keep it that way, but you are still responsible for what you build.

- Only comprehension reads donor source, read-only, with nothing executed. What it stores is a
  description of behavior plus provenance (repo, commit, license).
- Every capability carries the donor's license. Compositions check each donor's license
  against the policy in your profile (by default, unknown licenses are flagged; add the
  licenses you want warned about or denied), and every kit lists its donors in `BLUEPRINT.md`.
- Ideas and behavior are generally not protected by copyright; specific code is, and patents
  and terms of service can still apply. Reincarnate is not legal advice. When a donor's
  license or a patent matters to you, check it yourself.

## Limitations

- Pre-alpha. Expect rough edges and breaking changes between versions.
- It needs Claude Code and your own Claude login. Comprehension costs money, from about $0.015
  per small repo with Haiku up to around $0.20 with an agentic Sonnet run ([docs/COST.md](docs/COST.md)).
- Capabilities are extracted by a model and can be wrong or incomplete. Low-confidence ones
  can be sent to `reincarnate review`; read a capability before you build on it.
- License detection reads the repo's license files and can miss per-file licenses.
- Tested on macOS and Linux. Windows is untested.

## Status

Pre-alpha (v0.5.0), local-first: the plugin, the local studio (`/reincarnate:studio`), kits
checked step by step with Code Read, and `/reincarnate:build`. Everything runs on your machine
with your own Claude Code; data lives in `~/.reincarnate/` (or `$REINCARNATE_HOME`). No
accounts, no hosted service. Security issues: see [SECURITY.md](SECURITY.md).

## License

MIT. See `LICENSE`.

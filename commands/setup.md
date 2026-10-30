---
description: "Install the Reincarnate engine if needed, then set up your profile and projects"
argument-hint: "(no arguments)"
allowed-tools: Bash, Read
---

You are setting up Reincarnate for the user. Be conversational, do one stage at a time,
and **ask before anything that writes** (installing the engine, saving the profile,
registering a project). Show the exact command before you run it.

## 1. Is the engine installed?

```bash
command -v reincarnate && reincarnate --version
```

Read `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json` for the plugin's `version`.

- If `reincarnate` is on the PATH and its version equals the plugin's, say so and skip to
  step 2.
- If it is on the PATH but its version differs (the engine is installed as a copy, so a plugin
  update leaves the old engine behind), tell the user both versions and, with their yes,
  reinstall from this plugin:

  ```bash
  uv tool install --reinstall "${CLAUDE_PLUGIN_ROOT}"
  ```

  (or `pipx install --force "${CLAUDE_PLUGIN_ROOT}"` if it was installed with pipx). Then
  re-run `reincarnate --version` to confirm, and go to step 2.

If it is missing, the engine is a Python 3.11+ package bundled with this plugin. Tell the
user what you are about to run, get a yes, then prefer `uv`:

```bash
command -v uv && uv tool install "${CLAUDE_PLUGIN_ROOT}"
```

If `uv` is not available, fall back to `pipx`:

```bash
command -v pipx && pipx install "${CLAUDE_PLUGIN_ROOT}"
```

If neither exists, stop and tell the user to install `uv` (https://docs.astral.sh/uv/)
or `pipx` first; do not install a package manager yourself. After installing, re-run
`command -v reincarnate`. If it is still not found, `uv tool` installs to `~/.local/bin`:
tell the user to add it to their PATH (`uv tool update-shell`) and restart the shell.

Note for the user: the engine is installed as a copy. After the plugin updates, re-run
`/reincarnate:setup` (it will reinstall with `uv tool install --reinstall "${CLAUDE_PLUGIN_ROOT}"`
if you ask for an upgrade).

## 2. Create the data directory

```bash
reincarnate init
```

This creates the local SQLite store (default `~/.reincarnate/`, or `$REINCARNATE_HOME`).
Everything stays on this machine.

## 3. Profile

```bash
reincarnate profile show --json
```

If a profile already exists, show it and ask whether to change anything. Otherwise walk
the user through four questions, one at a time:

1. **Name**: what to call the profile.
2. **Stack**: languages, frameworks, runtimes, package managers (comma separated).
3. **Intents**: one to three things they want to build, in plain words. These become
   retrieval queries for the catalog.
4. **License denies**: licenses they never want donors to carry (suggest `GPL-3.0` and
   `AGPL-3.0`; ask, do not assume).

Read the answers back as a single command and ask for confirmation. Repeat `--intent` and
`--deny-license` once per value:

```bash
reincarnate profile set --name "<name>" --stack "<a,b,c>" --intent "<intent 1>" --intent "<intent 2>" --deny-license GPL-3.0 --deny-license AGPL-3.0
```

Use `--clear-intents` first only if the user wants to replace existing intents.

## 4. Register the current project (optional)

```bash
git rev-parse --show-toplevel 2>/dev/null
```

If the working directory is inside a git repo, offer to register it as a project (the
catalog scores capabilities against your projects). Ask for a one-line goal and, if known,
the test command, then:

```bash
reincarnate project add "<absolute repo root>" --goal "<one line>" --test-cmd "<test command>"
```

Add `--slug <slug>` only if the user wants a specific slug. If it is not a git repo, skip
this step and say how to add one later (`reincarnate project add <path>`).

## 5. Wrap up

```bash
reincarnate profile show --json
reincarnate project list --json
```

Summarize what is configured (profile, projects) and point the user to the next move:
`/reincarnate:comprehend owner/repo` to read a repo they admire.

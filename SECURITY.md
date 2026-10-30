# Security

Reincarnate reads third-party repositories and passes text from them to a model, so the two
things that matter most are prompt injection from a donor repo and anything that runs donor
code.

- Comprehension clones donors into a sandbox directory, reads them read-only and never runs them.
- Model calls run isolated (`--setting-sources ""`, a fixed tool list), so a donor's own
  `CLAUDE.md` or `.claude/settings.json` is not loaded.
- Plugin commands treat catalog, dossier and spec text as data, never as instructions.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository (Security tab, "Report a
vulnerability") rather than a public issue. Include the steps to reproduce and the version
(`reincarnate --version`). Expect a first reply within a week. Only the latest release is supported.

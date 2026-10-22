"""Dossier -> markdown, and the `export-md` command."""

from __future__ import annotations

import json
from pathlib import Path


def _esc(v) -> str:
    return str(v or "").replace("|", "\\|").replace("\n", " ").strip()


def _md_list(items, empty="_(none)_") -> str:
    vals = [str(i).strip() for i in (items or []) if str(i).strip()]
    return "\n".join(f"- {v}" for v in vals) if vals else empty


def _caps(caps) -> str:
    out: list[str] = []
    for c in caps:
        if not isinstance(c, dict):
            continue
        out.append(f"### {str(c.get('name') or 'capability').strip()}")
        out.append(f"**Portability:** {str(c.get('portability') or '?').strip()}\n")
        if str(c.get("what") or "").strip():
            out.append(str(c["what"]).strip() + "\n")
        if c.get("files"):
            out.append("- **Files:** " + ", ".join(f"`{f}`" for f in c["files"]))
        if c.get("dependencies"):
            out.append("- **Dependencies:** " + ", ".join(str(d) for d in c["dependencies"]))
        if c.get("provides"):
            out.append("- **Provides:** " + ", ".join(f"`{t}`" for t in c["provides"]))
        if c.get("requires"):
            out.append("- **Requires:** " + ", ".join(f"`{t}`" for t in c["requires"]))
        if str(c.get("novelty") or "").strip():
            out.append(f"- **Novelty:** {str(c['novelty']).strip()}")
        if str(c.get("portability_notes") or "").strip():
            out.append(f"- **Portability notes:** {str(c['portability_notes']).strip()}")
        if str(c.get("security_notes") or "").strip():
            out.append(f"- **Security notes:** {str(c['security_notes']).strip()}")
        out.append("")
    return "\n".join(out) if out else "_(no capabilities identified)_\n"


def render_dossier_markdown(dossier) -> str:
    d = dossier.data or {}
    deps = d.get("dependencies") if isinstance(d.get("dependencies"), dict) else {}
    sec = d.get("security") if isinstance(d.get("security"), dict) else {}
    caps = d.get("capabilities") if isinstance(d.get("capabilities"), list) else []
    license_str = str(getattr(dossier, "license", "") or deps.get("license") or "unknown")
    donor = f"{dossier.owner}/{dossier.repo}"

    lines = [
        "---",
        "type: repo-dossier",
        f"title: {donor}",
        f"donor: {donor}",
        f"url: {dossier.url}",
        f"sha: {dossier.sha}",
        f"license: {license_str}",
        f"comprehended_at: {dossier.comprehended_at}",
        f"source: {dossier.source}",
        f"model: {dossier.model}",
        "---",
        "",
        f"# {donor}",
        "",
    ]
    if str(d.get("summary") or "").strip():
        lines += [f"> {str(d['summary']).strip()}", ""]

    lines += ["## What it does", "", str(d.get("what_it_does") or "_(none)_").strip(), ""]

    lines += ["## Architecture", "", str(d.get("architecture") or "_(none)_").strip(), ""]
    if d.get("languages"):
        lines.append("- **Languages:** " + ", ".join(str(x) for x in d["languages"]))
    if str(d.get("build_system") or "").strip():
        lines.append(f"- **Build system:** {d['build_system']}")
    if d.get("entry_points"):
        lines.append("- **Entry points:** " + ", ".join(f"`{e}`" for e in d["entry_points"]))
    lines.append("")

    lines += [f"## Capabilities ({len(caps)})", "", _caps(caps)]

    lines += [
        "## Dependencies & license",
        "",
        f"- **License:** {license_str}",
        "",
        "**Notable dependencies:**",
        _md_list(deps.get("notable")),
        "",
        "**Supply-chain flags:**",
        _md_list(deps.get("supply_chain_flags"), "_(none flagged)_"),
        "",
    ]

    lines += [
        "## Security review",
        "",
        "**Concerns:**",
        _md_list(sec.get("concerns"), "_(none flagged)_"),
        "",
    ]
    if str(sec.get("network_egress") or "").strip():
        lines += [f"**Network egress:** {sec['network_egress']}", ""]
    lines += [
        "**Dangerous patterns:**",
        _md_list(sec.get("dangerous_patterns"), "_(none flagged)_"),
        "",
    ]

    lines += ["## Fit to your projects", ""]
    fits = [r for r in (d.get("fit_to_projects") or []) if isinstance(r, dict)]
    if fits:
        lines += ["| Project | Capability | How |", "| --- | --- | --- |"]
        lines += [
            f"| {_esc(r.get('project'))} | {_esc(r.get('capability'))} | {_esc(r.get('how'))} |"
            for r in fits
        ]
    else:
        lines.append("_(no project-fit suggestions)_")
    lines.append("")

    lines += ["## Salvage recommendations", "", _md_list(d.get("salvage_recommendations")), ""]

    lines += [
        "## Raw analysis (machine-readable)",
        "",
        "```json",
        json.dumps(d, indent=2, ensure_ascii=False),
        "```",
        "",
    ]
    return "\n".join(lines)


def run_cli(store, args) -> dict:
    """`export-md <owner/repo> [--out FILE]`."""
    from reincarnate import paths

    dossier = store.get_dossier(args.ref)
    if dossier is None:
        return {"error": f"no dossier found for {args.ref}"}
    out = getattr(args, "out", None)
    if out:
        path = Path(out).expanduser()
    else:
        path = paths.dossiers_dir() / f"{dossier.owner}-{dossier.repo}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_dossier_markdown(dossier), encoding="utf-8")
    return {"path": str(path), "donor": f"{dossier.owner}/{dossier.repo}"}

"""All LLM prompt builders. Profile/projects may be None/empty; prompts still render."""

from __future__ import annotations

import json

_FALLBACK_SCHEMA = """{
  "summary": "one or two sentences",
  "what_it_does": "markdown prose",
  "languages": ["most-used first"],
  "build_system": "",
  "entry_points": ["path - what it is"],
  "architecture": "markdown prose",
  "capabilities": [
    {"name": "", "what": "", "files": [], "dependencies": [],
     "portability": "high|medium|low", "portability_notes": "", "security_notes": ""}
  ],
  "dependencies": {"license": "", "notable": [], "supply_chain_flags": []},
  "security": {"concerns": [], "network_egress": "", "dangerous_patterns": []},
  "fit_to_projects": [{"project": "<profile project slug>", "capability": "<name>", "how": ""}],
  "salvage_recommendations": []
}"""


def _schema_hint() -> str:
    try:
        from reincarnate.schema import SCHEMA_HINT

        return SCHEMA_HINT
    except ImportError:
        return _FALLBACK_SCHEMA


def _get(obj, name, default=""):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _profile_block(profile) -> str:
    stack = [str(s) for s in (_get(profile, "stack", []) or [])]
    intents = [str(s) for s in (_get(profile, "intents", []) or [])]
    lines = [
        "The user's preferred stack: " + (", ".join(stack) if stack else "(not specified)"),
        "The user's intents:",
    ]
    lines += [f"- {i}" for i in intents] or ["- (not specified)"]
    return "\n".join(lines)


def _projects_block(projects) -> str:
    rows = []
    for p in projects or []:
        stack = ", ".join(str(s) for s in (_get(p, "stack", []) or [])) or "unspecified"
        goal = _get(p, "goal", "") or "no goal recorded"
        rows.append(f"- {_get(p, 'slug', '')}: {goal} (stack: {stack})")
    return "\n".join(rows) or "- (no projects registered; infer reasonable integration targets)"


def build_comprehension_prompt(owner: str, repo: str, profile, projects) -> str:
    """The clean-room comprehension prompt fed to agentic `claude -p`."""
    return (
        f"You are a senior software archaeologist performing a CLEAN-ROOM comprehension of the "
        f"third-party GitHub repository {owner}/{repo}, checked out READ-ONLY in your current "
        f"working directory.\n\n"
        "HARD RULES:\n"
        "- You may ONLY read and search files (Read, Grep, Glob). You cannot and must not run, "
        "build, install, or modify anything. Never execute repository code.\n"
        "- Do not access the network.\n"
        "- Describe behavior and ideas. DO NOT copy source code into your answer; never quote "
        "more than a short identifier or file path.\n\n"
        "GOAL: Produce a 'project view' dossier so the user can decide which capabilities to "
        "CLEAN-ROOM REIMPLEMENT in their own codebases, building the functionality themselves "
        "instead of adding a third-party dependency that may carry vulnerable logic. For each "
        "capability, judge how self-contained the underlying idea is and how it maps onto "
        "their projects.\n\n"
        f"{_profile_block(profile)}\n\n"
        "The user's projects: map salvageable capabilities onto these (use the slug before the "
        "colon as the `project` value in fit_to_projects):\n"
        f"{_projects_block(projects)}\n\n"
        "DO THIS:\n"
        "1. Explore the structure, entry points, and the most important source files.\n"
        "2. Identify the discrete capabilities/modules (aim for ~5-12 meaningful ones, not every "
        "file).\n"
        "3. For each, assess portability (how hard to lift the IDEA into a clean reimplementation) "
        "and any security concern.\n"
        "4. Audit dependencies, detect the license, and flag supply-chain / dangerous patterns "
        "(eval/exec, shelling out, obfuscation, network egress, unmaintained or sketchy deps).\n"
        "5. Recommend what is worth salvaging and where it fits among the user's projects.\n\n"
        "OUTPUT: Your FINAL message must be a SINGLE JSON object inside one ```json fenced code "
        "block and nothing after it. Use exactly this schema (markdown is allowed inside the long "
        "string fields; use [] or \"\" when a field does not apply):\n\n"
        f"```json\n{_schema_hint()}\n```\n\n"
        "Keep prose tight and concrete; cite real file paths. Do not invent features you did not "
        "see in the code."
    )


def build_overlay_prompt(dossier_data: dict, profile, projects) -> str:
    """Non-agentic prompt: score each capability against each project."""
    caps = [
        {
            "name": c.get("name", ""),
            "what": c.get("what", ""),
            "dependencies": c.get("dependencies", []),
        }
        for c in (dossier_data or {}).get("capabilities", [])
        if isinstance(c, dict)
    ]
    return (
        "You are scoring how well capabilities from a third-party repository would fit the "
        "user's own projects. Do not use any tools.\n\n"
        f"{_profile_block(profile)}\n\n"
        f"The user's projects:\n{_projects_block(projects)}\n\n"
        f"Capabilities (JSON):\n{json.dumps(caps, indent=2, ensure_ascii=False)}\n\n"
        "For each capability/project pair with a meaningful fit, give a score from 0 to 1 "
        "(1 = drops in naturally, 0 = irrelevant) and one sentence on how it would slot in. "
        "Omit pairs that score below 0.2. Use project slugs exactly as listed.\n\n"
        "OUTPUT: reply with ONLY a JSON array, no prose:\n"
        '[{"capability": "<name>", "project": "<slug>", "score": 0.0, "how": ""}]'
    )


def build_compose_prompt(idea: str, catalog_rows, profile, projects, target) -> str:
    """Non-agentic prompt: compose an app proposal from catalog capabilities."""
    rows = []
    for r in catalog_rows or []:
        rows.append(
            "- id={id} donor={donor} license={license} portability={portability} "
            "name={name!r}: {what}".format(
                id=_get(r, "id", ""),
                donor=_get(r, "donor", ""),
                license=_get(r, "license", "") or "unknown",
                portability=_get(r, "portability", ""),
                name=_get(r, "name", ""),
                what=_get(r, "what", ""),
            )
        )
    catalog = "\n".join(rows) or "- (catalog is empty)"
    target_line = f"Target: {target}" if target else "Target: none specified (standalone app)"
    return (
        "You are an app architect composing a new application from a catalog of capabilities "
        "that were previously analysed in third-party repositories. Do not use any tools.\n\n"
        "CLEAN-ROOM RULES: reference behavior only. Never reference, quote, or paste donor code, "
        "and never tell the builder to copy from a donor. Capabilities are described by what they "
        "do; the builder reimplements them from scratch.\n\n"
        f"IDEA: {idea}\n{target_line}\n\n"
        f"{_profile_block(profile)}\n\n"
        f"The user's projects:\n{_projects_block(projects)}\n\n"
        f"CATALOG (use capability_id values from here only):\n{catalog}\n\n"
        "Pick the capabilities that serve the idea, assign each a role and a build order, write "
        "the glue needed to connect them, list gaps (needs no catalog capability covers, with "
        "suggested_donors from the catalog if any, or build_fresh true), and flag risks and "
        "license_flags (donor licenses that conflict with the user's policy).\n\n"
        "OUTPUT: reply with ONLY one JSON object, no prose:\n"
        '{"name": "", "pitch": "", '
        '"capabilities": [{"capability_id": 0, "role": "", "order": 1}], '
        '"glue": "markdown", '
        '"gaps": [{"need": "", "suggested_donors": [], "build_fresh": true}], '
        '"risks": [], "license_flags": []}'
    )

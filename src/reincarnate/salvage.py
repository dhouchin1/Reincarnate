"""Clean-room spec builder: the air-gap artifact.

Specs describe behavior only. They never contain donor file contents; donor file *paths*
appear solely in a "context only — DO NOT COPY" line.
"""

from __future__ import annotations

from typing import Any

NEW_PREFIX = "new:"


def constraint_lines(donor: str | None, test_cmd: str | None) -> list[str]:
    """The four clean-room bullets (shared with the kit's CLAUDE.md)."""
    who = f"`{donor}`" if donor else "any donor repository"
    return [
        f"- Do **not** open, copy, paste, or transliterate source from {who}. "
        "Reimplement from the behavioral description only.",
        "- Add **no new third-party dependencies** - prefer the standard library and "
        "dependencies the project already uses.",
        "- Match this project's existing conventions, structure, and code style.",
        "- Include unit tests" + (f"; the suite runs with `{test_cmd}`." if test_cmd else "."),
    ]


def _target_info(store, target_slug: str, project) -> tuple[str, str, str]:
    """Return (display name, stack text, test command)."""
    if target_slug.startswith(NEW_PREFIX):
        return target_slug[len(NEW_PREFIX):].strip() or "new project", "the new project's stack", ""
    if project is None and store is not None:
        project = store.get_project(target_slug)
    stack_list = list(getattr(project, "stack", None) or [])
    stack = ", ".join(stack_list) if stack_list else "the project's existing stack"
    test_cmd = (getattr(project, "test_cmd", "") or "") if project else ""
    return target_slug, stack, test_cmd


def _integration_intent(dossier, capability_name: str, target: str) -> str:
    name_l = capability_name.lower()
    for row in (dossier.data.get("fit_to_projects") or []) if dossier else []:
        if not isinstance(row, dict):
            continue
        cap = str(row.get("capability") or "").lower()
        if row.get("project") == target and cap and (name_l in cap or cap in name_l):
            return str(row.get("how") or "")
    return ""


def build_clean_room_spec(store, capability, *, target_slug: str, project=None,
                          include_header: bool = True) -> str:
    dossier = store.get_dossier(capability.dossier_id)
    name = capability.name or "capability"
    what = (capability.what or "").strip()
    pnotes = (capability.portability_notes or "").strip()
    snotes = (capability.security_notes or "").strip()
    files = [str(f) for f in (capability.files or []) if str(f).strip()]
    donor = capability.donor or (dossier.donor if dossier else "unknown")
    license_str = capability.license or (dossier.license if dossier else "") or "unknown"
    date = ((dossier.comprehended_at if dossier else "") or "")[:10] or "unknown date"
    target_name, stack, test_cmd = _target_info(store, target_slug, project)
    intent = _integration_intent(dossier, name, target_slug)

    sec_reqs: list[str] = []
    if snotes:
        sec_reqs.append(snotes)
    token = (name.split() or [""])[0].lower()
    security = (dossier.data.get("security") if dossier else None) or {}
    for concern in security.get("concerns") or []:
        c = str(concern).strip()
        if c and token and token in c.lower() and c not in sec_reqs:
            sec_reqs.append(c)

    lines: list[str] = []
    if include_header:
        lines += [f"## Clean-room reimplementation: {name}", ""]
    lines += [
        f"**Goal:** Reimplement the *behavior* described below natively in `{target_name}` "
        f"({stack}). Work only from this spec.",
        "",
        f"**Provenance:** idea observed in donor repo `{donor}` (license: {license_str}); "
        f"comprehended {date}.",
        "",
        "**Constraints — the clean-room air gap:**",
        *constraint_lines(donor, test_cmd),
        "",
        "**Behavior to implement:**",
        "",
        what or "_(no description recorded)_",
    ]
    if pnotes:
        lines += ["", "**Design notes (how to build it cleanly):**", "", pnotes]
    if sec_reqs:
        lines += ["", "**Security requirements:**", ""]
        lines += [f"- {s}" for s in sec_reqs]
    if intent:
        lines += ["", f"**Integration intent (from the dossier):** {intent}"]
    if files:
        lines += ["", "**Donor reference files (context only — DO NOT COPY):** "
                  + ", ".join(f"`{f}`" for f in files)]
    return "\n".join(lines) + "\n"


def build_fresh_spec(gap: dict, *, target_slug: str, project=None, store=None) -> str:
    need = str(gap.get("need") or "").strip() or "(unspecified need)"
    target_name, stack, test_cmd = _target_info(store, target_slug, project)
    lines = [
        f"## Build fresh: {need}",
        "",
        f"**Goal:** Build the behavior below natively in `{target_name}` ({stack}). "
        "There is no donor - build fresh.",
        "",
        "**Constraints — the clean-room air gap:**",
        *constraint_lines(None, test_cmd),
        "",
        "**Behavior to implement:**",
        "",
        need,
    ]
    donors = [str(d) for d in (gap.get("suggested_donors") or []) if str(d).strip()]
    if donors:
        lines += ["", "**Suggested donors to comprehend later (not to be read now):** "
                  + ", ".join(f"`{d}`" for d in donors)]
    return "\n".join(lines) + "\n"


def run_cli(store, args: Any) -> dict:
    cap = store.get_capability(args.capability_id)
    if cap is None:
        return {"error": f"capability {args.capability_id} not found"}
    target = args.into
    spec = build_clean_room_spec(store, cap, target_slug=target)
    path = None
    out = getattr(args, "out", None)
    if out:
        from pathlib import Path

        p = Path(out).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(spec, encoding="utf-8")
        path = str(p)
    return {"capability_id": cap.id, "name": cap.name, "donor": cap.donor, "target": target,
            "spec": spec, "path": path}

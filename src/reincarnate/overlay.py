"""Score a donor's capabilities against the user's projects (heuristic or claude -p)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import claude_runner

_STOP = frozenset(
    "the and for with that this from your you are can use using into over via per its has have "
    "not but all any new app apps based simple tool tools".split())
_MIN_SCORE = 0.15


def _tokens(s) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", str(s or "").lower())
            if len(t) > 2 and t not in _STOP}


def _get(cap, key, default=""):
    if isinstance(cap, dict):
        return cap.get(key, default)
    return getattr(cap, key, default)


def heuristic_fits(cap, projects) -> list[tuple[str, float, str]]:
    deps = _get(cap, "dependencies", []) or []
    ctoks = _tokens(f"{_get(cap, 'name')} {_get(cap, 'what')} {' '.join(map(str, deps))}")
    out = []
    for p in projects:
        ptoks = _tokens(f"{p.goal} {' '.join(p.stack or [])} {p.slug}")
        common = ctoks & ptoks
        if not common:
            continue
        score = min(1.0, max(0.0, len(common) / min(len(ctoks), len(ptoks))))
        if score >= _MIN_SCORE:
            out.append((p.slug, round(score, 3), "shares terms: " + ", ".join(sorted(common)[:6])))
    out.sort(key=lambda t: -t[1])
    return out


def _extract_json_array(text: str):
    """Last ```json fenced array, else bare array, else first array embedded in prose."""
    if not text:
        return None
    for body in reversed(re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)):
        try:
            v = json.loads(body)
        except ValueError:
            continue
        if isinstance(v, list):
            return v
    try:
        v = json.loads(text.strip())
        return v if isinstance(v, list) else None
    except ValueError:
        pass
    start = text.find("[")
    while start >= 0:
        try:
            v, _ = json.JSONDecoder().raw_decode(text[start:])
            if isinstance(v, list):
                return v
        except ValueError:
            pass
        start = text.find("[", start + 1)
    return None


def _claude_fits(dossier, profile, projects) -> tuple[list[tuple[str, str, float, str]], str]:
    """Returns ([(cap_name, slug, score, how)], message). Empty list means failure."""
    from . import prompts
    builder = getattr(prompts, "build_overlay_prompt", None)
    if builder is None:
        return [], "prompts.build_overlay_prompt is missing"
    try:
        res = claude_runner.run(builder(dossier.data, profile, projects), cwd=Path.cwd(),
                                tools="", max_turns=1, model="haiku")
    except Exception as exc:  # noqa: BLE001 - degrade, never crash
        return [], f"claude -p failed: {exc}"
    arr = _extract_json_array(res.text)
    if not arr:
        return [], f"claude -p returned no usable JSON array (rc={res.rc})"
    rows = []
    for r in arr:
        if not isinstance(r, dict):
            continue
        try:
            score = max(0.0, min(1.0, float(r.get("score"))))
        except (TypeError, ValueError):
            continue
        rows.append((str(r.get("capability", "")).strip().lower(), str(r.get("project", "")),
                     score, str(r.get("how", ""))))
    return rows, ""


def apply(store, dossier, profile, projects, *, engine="heuristic") -> dict:
    caps = store.list_capabilities(dossier_id=dossier.id)
    slugs = {p.slug for p in projects}
    message = ""
    used = "heuristic"
    plan: list[tuple[int, str, float, str]] = []
    if engine == "claude-p":
        rows, message = _claude_fits(dossier, profile, projects)
        by_name: dict[str, int] = {}
        for c in caps:
            by_name.setdefault(c.name.strip().lower(), c.id)
        plan = [(by_name[n], s, sc, h) for n, s, sc, h in rows if n in by_name and s in slugs]
        if plan:
            used = "claude-p"
        elif not message:
            message = "claude -p produced no fits for known capabilities/projects"
        if used == "heuristic":
            message += "; fell back to heuristic"
    if used == "heuristic":
        plan = [(c.id, s, sc, h) for c in caps for s, sc, h in heuristic_fits(c, projects)]
    written = 0
    manual = {(f.capability_id, f.project_slug)
              for c in caps for f in store.list_fits(capability_id=c.id) if f.source == "manual"}
    for cid, slug, score, how in plan:
        if (cid, slug) in manual:
            continue
        store.set_fit(cid, slug, score, how, source="overlay")
        written += 1
    return {"donor": dossier.donor, "capabilities": len(caps), "fits_written": written,
            "engine_used": used, "message": message or f"{written} fit(s) written"}


def run_cli(store, args) -> dict:
    repo = getattr(args, "repo", None)
    if getattr(args, "all", False):
        dossiers = store.list_dossiers(current_only=True)
    elif repo:
        d = store.get_dossier(repo)
        if d is None:
            return {"error": f"dossier not found: {repo}"}
        dossiers = [d]
    else:
        return {"error": "give an owner/repo or --all"}
    engine = getattr(args, "engine", None) or "heuristic"
    profile = store.get_profile()
    projects = store.list_projects()
    results = [apply(store, d, profile, projects, engine=engine) for d in dossiers]
    total = sum(r["fits_written"] for r in results)
    text = "\n".join(f"{r['donor']}: {r['fits_written']} fits / {r['capabilities']} caps "
                     f"[{r['engine_used']}] {r['message']}" for r in results)
    return {"results": results, "total_fits": total, "text": text or "(no dossiers)"}

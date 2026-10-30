"""Score a donor's capabilities against the user's projects (heuristic or claude -p)."""

from __future__ import annotations

import re
from pathlib import Path

from . import claude_runner, schema
from .store import FIT_RANK

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
    """The JSON array in a model reply (see schema.extract_json)."""
    return schema.extract_json(text, list)


def _claude_fits(dossier, profile, projects) -> tuple[list[tuple], str, float]:
    """Returns ([(cap_name, slug, score, how)], message, cost_usd). Empty list means failure."""
    from . import prompts
    builder = getattr(prompts, "build_overlay_prompt", None)
    if builder is None:
        return [], "prompts.build_overlay_prompt is missing", 0.0
    try:
        res = claude_runner.run(builder(dossier.data, profile, projects), cwd=Path.cwd(),
                                tools="", max_turns=1, model="haiku",
                                label=f"overlay {dossier.donor}")
    except Exception as exc:  # noqa: BLE001 - degrade, never crash
        return [], f"claude -p failed: {exc}", 0.0
    if res.failed:
        return [], f"claude -p failed: {res.error_message()}", res.cost_usd
    arr = _extract_json_array(res.text)
    if not arr:
        return [], "claude -p returned no usable JSON array", res.cost_usd
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
    return rows, "", res.cost_usd


def apply(store, dossier, profile, projects, *, engine="heuristic") -> dict:
    caps = store.list_capabilities(dossier_id=dossier.id)
    slugs = {p.slug for p in projects}
    message = ""
    used = "heuristic"
    plan: list[tuple[int, str, float, str]] = []
    cost = 0.0
    if engine == "claude-p":
        rows, message, cost = _claude_fits(dossier, profile, projects)
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
    source = "overlay-claude" if used == "claude-p" else "overlay"
    rank = FIT_RANK[source]
    existing = {(f.capability_id, f.project_slug): FIT_RANK.get(f.source, FIT_RANK["overlay"])
                for c in caps for f in store.list_fits(capability_id=c.id)}
    written = kept = 0
    for cid, slug, score, how in plan:
        if existing.get((cid, slug), 0) > rank:  # a stronger source already set this fit
            kept += 1
            continue
        store.set_fit(cid, slug, score, how, source=source)
        written += 1
    if kept:
        message = (message + "; " if message else "") + f"{written} fit(s) written"
        message += f", {kept} kept (a stronger fit exists)"
    return {"donor": dossier.donor, "capabilities": len(caps), "fits_written": written,
            "fits_kept": kept,
            "engine_used": used, "message": message or f"{written} fit(s) written",
            "cost_usd": round(cost, 4)}


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
    total_kept = sum(r["fits_kept"] for r in results)
    cost = round(sum(r["cost_usd"] for r in results), 4)
    text = "\n".join(f"{r['donor']}: {r['fits_written']} fits / {r['capabilities']} caps "
                     f"[{r['engine_used']}] {r['message']}" for r in results)
    if engine == "claude-p":
        text = (text or "(no dossiers)") + f"\ncost: ${cost:.4f}"
    return {"results": results, "total_fits": total, "fits_kept": total_kept,
            "cost_usd": cost,
            "text": text or "(no dossiers)"}

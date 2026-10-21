"""App proposals: an idea plus the capability catalog -> a validated Composition.

Engines: `claude-p` (headless one-shot), `session` (the running Claude Code session writes the
proposal JSON to a file), and `offline` (deterministic). The LLM only proposes; everything it
returns is re-validated here deterministically: unknown capability ids are dropped, order is
renumbered, licenses are screened against the profile policy, and the target is checked
against known projects.
Proposals never carry donor file contents (not even file paths).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import Composition
from .store import now

_PORT_RANK = {"high": 0, "medium": 1, "low": 2}
_FILL_MIN = 8
_FEWER = "fewer than 2 valid capabilities"
_UNKNOWN_TARGET = "unknown target project"
_ARCH_PREFIX = "Reference architecture (behavior only; do not copy):"
_DEFAULT_POLICY = {"deny": [], "warn": ["unknown"]}


# -- retrieval ----------------------------------------------------------------------------
def _port_rank(p: str) -> int:
    return _PORT_RANK.get((p or "").lower(), 3)


def _fits_by_cap(store) -> dict[int, list]:
    out: dict[int, list] = {}
    for f in store.list_fits():
        out.setdefault(f.capability_id, []).append(
            {"project": f.project_slug, "score": f.score, "how": f.how, "source": f.source})
    return out


def _row(cap_id, donor, license_, name, what, portability, pnotes, snotes, fit) -> dict:
    return {"capability_id": int(cap_id), "donor": donor or "", "license": license_ or "",
            "name": name or "", "what": what or "", "portability": portability or "medium",
            "portability_notes": pnotes or "", "security_notes": snotes or "",
            "fit": list(fit or [])}


def _cap_row(c, fits: dict[int, list]) -> dict:
    return _row(c.id, c.donor, c.license, c.name, c.what, c.portability, c.portability_notes,
                c.security_notes, fits.get(c.id, []))


def _search_rows(store, query: str, fits: dict[int, list]) -> list[dict]:
    """FTS hits as rows, in FTS rank order. Straight from the store: catalog.build_catalog
    re-sorts by its own score, which would lose the rank the offline engine relies on."""
    return [_cap_row(c, fits) for c in store.search_capabilities(query)]


def _idea_query(idea: str) -> str:
    # store.search_capabilities tokenizes on \w+ and OR-joins the tokens itself, so a literal
    # "OR" here would become a search term; pass the filtered tokens space-separated.
    seen: list[str] = []
    for t in re.findall(r"\w+", idea or ""):
        t = t.lower()
        if len(t) > 3 and t not in seen:
            seen.append(t)
    return " ".join(seen)


def retrieve(store, idea: str, *, caps=None, limit: int = 25) -> list[dict]:
    """Explicit `caps` first (always kept), then FTS hits for the idea, then portability fill."""
    fits = _fits_by_cap(store)
    rows: list[dict] = []
    seen: set[int] = set()

    def add(r: dict, how: str, force: bool = False) -> None:
        if r["capability_id"] in seen or (not force and len(rows) >= limit):
            return
        seen.add(r["capability_id"])
        r["retrieved_by"] = how  # "caps" | "search" | "fill"
        rows.append(r)

    for cid in caps or []:
        try:
            c = store.get_capability(int(cid))
        except (TypeError, ValueError):
            continue
        if c:
            add(_cap_row(c, fits), "caps", force=True)
    query = _idea_query(idea)
    if query:
        for r in _search_rows(store, query, fits):
            add(r, "search")
    if len(rows) < _FILL_MIN:
        current = sorted(store.list_capabilities(current_only=True),
                         key=lambda c: (_port_rank(c.portability), c.name.lower()))
        for c in current:
            if len(rows) >= _FILL_MIN:
                break
            add(_cap_row(c, fits), "fill")
    return rows


# -- slugs --------------------------------------------------------------------------------
_SLUG_MAX = 48
_IDEA_SLUG_WORDS = 6


def slugify(name: str, max_len: int = _SLUG_MAX) -> str:
    """Lowercase-hyphen slug, at most `max_len` chars, cut at a word boundary."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    if len(s) > max_len:
        cut = s[:max_len + 1]
        at = cut.rfind("-")
        s = cut[:at] if at > 0 else s[:max_len]
    return s.strip("-")


def slug_base(name, idea) -> str:
    """Slug source: the proposal/CLI name when present, else the idea's first 6 words."""
    name = _as_str(name).strip()
    if name:
        return slugify(name)
    return slugify(" ".join(re.findall(r"\w+", idea or "")[:_IDEA_SLUG_WORDS]))


def unique_slug(store, base: str) -> str:
    base = slugify(base) or "composition"
    slug, n = base, 2
    while store.get_composition(slug) is not None:
        slug = f"{base}-{n}"
        n += 1
    return slug


# -- validation ---------------------------------------------------------------------------
def _policy(profile) -> dict:
    pol = getattr(profile, "license_policy", None) if profile is not None else None
    if not isinstance(pol, dict):
        return dict(_DEFAULT_POLICY)
    return pol


def _norm_set(items) -> set[str]:
    return {str(x).strip().lower() for x in (items or []) if str(x).strip()}


def license_level(license_: str, profile) -> str | None:
    pol = _policy(profile)
    lic = (license_ or "").strip().lower() or "unknown"
    if lic in _norm_set(pol.get("deny")):
        return "deny"
    if lic in _norm_set(pol.get("warn")) or lic == "unknown":
        return "warn"
    return None


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "\n".join(_as_str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _as_str_list(v) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [s for s in (_as_str(x).strip() for x in v) if s]
    s = _as_str(v).strip()
    return [s] if s else []


def _as_bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y")
    return bool(v)


def _gaps(raw) -> list[dict]:
    if raw is None or raw == "":
        return []
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for g in items:
        if isinstance(g, dict):
            need = _as_str(g.get("need") or g.get("name") or "").strip()
            donors = _as_str_list(g.get("suggested_donors"))
            fresh = _as_bool(g.get("build_fresh", not donors))
        else:
            need, donors, fresh = _as_str(g).strip(), [], True
        if need:
            out.append({"need": need, "suggested_donors": donors, "build_fresh": fresh})
    return out


def _resolve_target(store, target, default_slug: str) -> tuple[str, str, str | None]:
    """-> (target_kind, target_slug, problem-or-None)."""
    t = (target or "").strip() if isinstance(target, str) else ""
    if not t:
        return "new", default_slug, None
    if t.lower().startswith("new:"):
        return "new", slugify(t[4:]) or default_slug, None
    known = {p.slug for p in store.list_projects()}
    if t in known:
        return "existing", t, None
    return "new", default_slug, f"{_UNKNOWN_TARGET}: {t}"


def validate_proposal(store, raw: dict, rows, profile, *, target, slug=None):
    """Deterministic validation of an (untrusted) proposal. -> (proposal, problems)."""
    raw = raw if isinstance(raw, dict) else {}
    by_id = {int(r["capability_id"]): r for r in rows or []}
    problems: list[str] = []

    picked = []
    seen: set[int] = set()
    raw_caps = raw.get("capabilities")
    for i, c in enumerate(raw_caps if isinstance(raw_caps, list) else []):
        if isinstance(c, dict):
            cid = _as_int(c.get("capability_id", c.get("id")))
            role = _as_str(c.get("role")).strip()
            order = _as_int(c.get("order"))
        else:
            cid, role, order = _as_int(c), "", None
        if cid is not None and cid not in by_id:
            problems.append(f"unknown capability_id {cid} dropped")
            continue
        if cid is None or cid in seen:
            continue
        seen.add(cid)
        picked.append((order if order is not None else 10**9, i, cid, role or "component"))
    picked.sort(key=lambda t: (t[0], t[1]))

    caps, flags = [], []
    flagged: set[tuple[int, str]] = set()
    for n, (_, _, cid, role) in enumerate(picked, start=1):
        r = by_id[cid]
        caps.append({"capability_id": cid, "role": role, "order": n, "name": r["name"],
                     "donor": r["donor"], "license": r["license"] or "unknown",
                     "portability": r["portability"]})
        level = license_level(r["license"], profile)
        if level and (cid, level) not in flagged:
            flagged.add((cid, level))
            flags.append({"capability_id": cid, "name": r["name"], "donor": r["donor"],
                          "license": r["license"] or "unknown", "level": level})
    if len(caps) < 2:
        problems.append(_FEWER)

    default_slug = slugify(slug or raw.get("name") or "") or "app"
    kind, tslug, tproblem = _resolve_target(store, target, default_slug)
    if tproblem:
        problems.append(tproblem)

    notes = _as_str_list(raw.get("notes"))
    proposal = {
        "capabilities": caps,
        "glue": _as_str(raw.get("glue")),
        "gaps": _gaps(raw.get("gaps")),
        "risks": _as_str_list(raw.get("risks")),
        "license_flags": flags,
        "target_kind": kind,
        "target_slug": tslug,
        "problems": problems,
        "notes": notes,
    }
    return proposal, problems


# -- engines ------------------------------------------------------------------------------
def _foundational(rows) -> list[dict]:
    return sorted(rows, key=lambda r: (_port_rank(r["portability"]), r["name"].lower()))


_OFFLINE_MAX = 8


def offline_proposal(rows, idea) -> dict:
    """Deterministic proposal, at most 8 steps, foundational-first, no glue or gaps.

    Selection: explicit --caps rows, then search hits in FTS order (rows carry
    `retrieved_by`; rows without it count as search hits). Portability-fill rows are used
    only to reach the 2-capability minimum.
    """
    rows = list(rows or [])
    caps = [r for r in rows if r.get("retrieved_by") == "caps"]
    hits = [r for r in rows if r.get("retrieved_by", "search") == "search"]
    picked = (caps + hits)[:_OFFLINE_MAX]
    if len(picked) < 2:
        fill = [r for r in rows if r.get("retrieved_by") == "fill"]
        picked += fill[:2 - len(picked)]
    return {
        "capabilities": [{"capability_id": r["capability_id"], "role": "component", "order": n}
                         for n, r in enumerate(_foundational(picked), start=1)],
        "glue": "",
        "gaps": [],
        "risks": [],
    }


def extract_json_object(text: str) -> dict | None:
    """The last JSON object in `text`: fenced ```json blocks first, then bare objects."""
    if not text:
        return None
    for block in reversed(re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)):
        try:
            val = json.loads(block.strip())
        except ValueError:
            continue
        if isinstance(val, dict):
            return val
    dec = json.JSONDecoder()
    last, i = None, text.find("{")
    while i != -1:
        try:
            val, end = dec.raw_decode(text, i)
        except ValueError:
            i = text.find("{", i + 1)
            continue
        if isinstance(val, dict):
            last = val
        i = text.find("{", end)
    return last


def _claude_proposal(store, idea, rows, target) -> tuple[dict | None, str]:
    """-> (raw proposal or None, failure note)."""
    try:
        from . import prompts
        build = prompts.build_compose_prompt
    except (ImportError, AttributeError):
        return None, "prompts.build_compose_prompt unavailable; used offline engine"
    from . import claude_runner

    # prompts renders `id`; rows carry `capability_id`. Give it both.
    prompt_rows = [{**r, "id": r["capability_id"]} for r in rows]
    prompt = build(idea, prompt_rows, store.get_profile(), store.list_projects(), target)
    res = claude_runner.run(prompt, cwd=Path.cwd(), tools="", max_turns=1, model="opus")
    raw = extract_json_object(res.text)
    if raw is None:
        why = f"rc={res.rc}" + (f": {res.stderr.strip()[:200]}" if res.stderr.strip() else "")
        return None, f"claude-p returned no JSON proposal ({why}); fell back to offline engine"
    return raw, ""


def load_session_proposal(proposal_path) -> dict:
    """A session-authored proposal file: a bare JSON object or one inside a ```json fence."""
    if not proposal_path:
        raise ValueError("--proposal FILE is required for --engine session")
    try:
        text = Path(proposal_path).expanduser().read_text()
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"cannot read proposal file {proposal_path}: {e}") from e
    try:
        val = json.loads(text)
    except ValueError:
        val = extract_json_object(text)
    if not isinstance(val, dict):
        raise ValueError(f"no JSON object in proposal file {proposal_path}")
    return val


def _file_cap_ids(raw: dict) -> list[int]:
    caps = raw.get("capabilities")
    out = []
    for c in caps if isinstance(caps, list) else []:
        cid = _as_int(c.get("capability_id", c.get("id")) if isinstance(c, dict) else c)
        if cid is not None and cid not in out:
            out.append(cid)
    return out


def propose(store, idea, *, caps=None, target=None, engine="claude-p", name=None,
            proposal_path=None) -> Composition:
    note = ""
    raw = None
    if engine == "session":
        # No subprocess and no offline fallback: the session's choice is validated as-is.
        raw = load_session_proposal(proposal_path)
        wanted = _file_cap_ids(raw) + [int(c) for c in caps or [] if _as_int(c) is not None]
        rows = retrieve(store, idea, caps=wanted)
    else:
        rows = retrieve(store, idea, caps=caps)
    if engine == "claude-p":
        raw, note = _claude_proposal(store, idea, rows, target)
    if raw is None:
        raw = offline_proposal(rows, idea)
    raw_name = _as_str(raw.get("name")).strip()
    title = (name or raw_name or idea[:60]).strip() or "composition"
    slug = unique_slug(store, slug_base(name or raw_name, idea))
    proposal, _ = validate_proposal(store, raw, rows, store.get_profile(), target=target,
                                    slug=slug)
    proposal["engine"] = engine if not note else "offline"
    if note:
        proposal["notes"].append(note)
    comp = Composition(slug=slug, name=title, pitch=_as_str(raw.get("pitch")), idea=idea,
                       target_kind=proposal["target_kind"], target_slug=proposal["target_slug"],
                       proposal=proposal, status="proposed", created=now())
    return store.save_composition(comp)


def reincarnate_donor(store, donor_ref, *, target) -> Composition:
    """Whole-donor composition: every current capability, foundational-first."""
    d = store.get_dossier(donor_ref)
    if d is None:
        raise ValueError(f"unknown dossier: {donor_ref}")
    fits = _fits_by_cap(store)
    rows = [_cap_row(c, fits) for c in store.list_capabilities(dossier_id=d.id)]
    ordered = sorted(rows, key=lambda r: _port_rank(r["portability"]))  # stable
    arch = _as_str(d.data.get("architecture")).strip()
    raw = {
        "capabilities": [{"capability_id": r["capability_id"], "role": "component", "order": n}
                         for n, r in enumerate(ordered, start=1)],
        "glue": f"{_ARCH_PREFIX}\n\n{arch}" if arch else _ARCH_PREFIX,
        "gaps": [],
        "risks": [],
    }
    title = f"Reincarnate {d.owner}/{d.repo}"
    slug = unique_slug(store, title)
    proposal, _ = validate_proposal(store, raw, rows, store.get_profile(), target=target,
                                    slug=slug)
    proposal["engine"] = "reincarnate"
    proposal["donor"] = d.donor
    comp = Composition(slug=slug, name=title, pitch=d.summary or "",
                       idea=f"Rebuild {d.owner}/{d.repo} natively",
                       target_kind=proposal["target_kind"], target_slug=proposal["target_slug"],
                       proposal=proposal, status="proposed", created=now())
    return store.save_composition(comp)


# -- rendering / CLI ----------------------------------------------------------------------
def render_text(c: Composition) -> str:
    p = c.proposal or {}
    lines = [f"{c.name}  [{c.slug}, {c.status}]"]
    if c.pitch:
        lines.append(c.pitch)
    lines.append(f"target: {c.target_kind or 'new'}:{c.target_slug or '-'}")
    if c.idea:
        lines.append(f"idea: {c.idea}")
    lines += ["", "steps:"]
    caps = sorted(p.get("capabilities") or [], key=lambda x: x.get("order", 0))
    for s in caps:
        lines.append(f"  {s.get('order')}. {s.get('name') or '#' + str(s.get('capability_id'))}"
                     f" ({s.get('donor', '?')}, {s.get('license') or 'unknown'})"
                     f" — {s.get('role') or 'component'}")
    if not caps:
        lines.append("  (none)")
    if p.get("glue"):
        lines += ["", "glue:", p["glue"]]
    if p.get("gaps"):
        lines += ["", "gaps:"]
        for g in p["gaps"]:
            how = "build fresh" if g.get("build_fresh") else "comprehend a donor"
            donors = ", ".join(g.get("suggested_donors") or [])
            lines.append(f"  - {g.get('need')} ({how}{'; try ' + donors if donors else ''})")
    if p.get("risks"):
        lines += ["", "risks:"] + [f"  - {r}" for r in p["risks"]]
    if p.get("license_flags"):
        lines += ["", "license flags:"]
        lines += [f"  - [{f['level']}] {f['donor']} {f['license']} — "
                  f"{f.get('name') or '#' + str(f['capability_id'])}"
                  for f in p["license_flags"]]
    if p.get("problems"):
        lines += ["", "problems:"] + [f"  - {x}" for x in p["problems"]]
    if p.get("notes"):
        lines += ["", "notes:"] + [f"  - {x}" for x in p["notes"]]
    return "\n".join(lines)


def _list_text(comps) -> str:
    if not comps:
        return "no compositions"
    return "\n".join(f"{c.id}\t{c.slug}\t{c.status}\t"
                     f"{len((c.proposal or {}).get('capabilities') or [])} caps\t{c.name}"
                     for c in comps)


def _result(c: Composition) -> dict:
    return {"composition": c.to_dict(), "id": c.id, "slug": c.slug, "text": render_text(c)}


def run_cli(store, args) -> dict:
    if getattr(args, "reincarnate_donor", False):
        try:
            c = reincarnate_donor(store, args.repo, target=getattr(args, "into", None))
        except ValueError as e:
            return {"error": str(e), "text": f"error: {e}"}
        return _result(c)
    words = [str(w) for w in (getattr(args, "idea", None) or [])]
    if words == ["list"]:
        comps = store.list_compositions()
        return {"items": [c.to_dict() for c in comps], "text": _list_text(comps)}
    if words and words[0] == "show":
        if len(words) != 2:
            usage = "usage: compose show <id|slug>"
            return {"error": usage, "text": usage}
        c = store.get_composition(words[1])
        if c is None:
            return {"error": f"no such composition: {words[1]}",
                    "text": f"error: no such composition: {words[1]}"}
        return {"composition": c.to_dict(), "text": render_text(c)}
    idea = " ".join(words).strip()
    if not idea:
        return {"error": "an idea is required", "text": "error: an idea is required"}
    caps = [int(x) for x in re.findall(r"\d+", getattr(args, "caps", None) or "")]
    engine = getattr(args, "engine", "claude-p") or "claude-p"
    proposal_path = getattr(args, "proposal", None)
    if engine == "session" and not proposal_path:
        msg = "--proposal FILE is required for --engine session"
        return {"error": msg, "text": f"error: {msg}"}
    try:
        c = propose(store, idea, caps=caps or None, target=getattr(args, "target", None),
                    engine=engine, name=getattr(args, "name", None),
                    proposal_path=proposal_path)
    except ValueError as e:
        return {"error": str(e), "text": f"error: {e}"}
    return _result(c)

"""Capability catalog: filtered, scored views over the store plus a gap report."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .overlay import _tokens

FIT_MIN = 0.3
_PORT_WEIGHT = {"high": 1.0, "medium": 0.6, "low": 0.3}
_MAX_ROWS = 60


@dataclass
class CatalogEntry:
    capability_id: int = 0
    donor: str = ""
    license: str = ""
    name: str = ""
    what: str = ""
    portability: str = "medium"
    portability_notes: str = ""
    security_notes: str = ""
    files: list = field(default_factory=list)
    dependencies: list = field(default_factory=list)
    fit: list = field(default_factory=list)  # [{"project","score","how","source"}]
    vote: int = 0
    score: float = 0.0

    def best_fit(self) -> float:
        return max((f["score"] for f in self.fit), default=0.0)

    def to_dict(self) -> dict:
        return asdict(self)


def _entry_score(portability: str, best_fit: float, vote: int) -> float:
    return round(_PORT_WEIGHT.get(portability, 0.6) + 0.5 * best_fit + 0.25 * vote, 4)


def build_catalog(store, *, search=None, portability=None, donor=None, fit=None,
                  gaps=False) -> list[CatalogEntry]:
    """Scored catalog entries. `gaps` is handled by run_cli; it does not alter the entries."""
    caps = (store.search_capabilities(search) if search
            else store.list_capabilities(current_only=True))
    fits: dict[int, list] = {}
    for f in store.list_fits():
        fits.setdefault(f.capability_id, []).append(
            {"project": f.project_slug, "score": f.score, "how": f.how, "source": f.source})
    votes = store.get_votes()
    out: list[CatalogEntry] = []
    for c in caps:
        if portability and c.portability != portability:
            continue
        if donor and c.donor != donor:
            continue
        cfits = fits.get(c.id, [])
        if fit and not any(f["project"] == fit and f["score"] >= FIT_MIN for f in cfits):
            continue
        vote = votes.get(c.id, 0)
        e = CatalogEntry(
            capability_id=c.id, donor=c.donor, license=c.license, name=c.name, what=c.what,
            portability=c.portability, portability_notes=c.portability_notes,
            security_notes=c.security_notes, files=list(c.files),
            dependencies=list(c.dependencies), fit=cfits, vote=vote)
        e.score = _entry_score(c.portability, e.best_fit(), vote)
        out.append(e)
    if search:
        # FTS relevance leads; score only breaks ties so a strong match is never buried
        # under an unrelated high-portability row.
        rank = {c.id: i for i, c in enumerate(caps)}
        out.sort(key=lambda e: (rank.get(e.capability_id, 1 << 30), -e.score))
    else:
        out.sort(key=lambda e: (-e.score, e.name.lower()))
    return out


def summary(entries) -> dict:
    by_port: dict[str, int] = {}
    donors: set[str] = set()
    for e in entries:
        by_port[e.portability] = by_port.get(e.portability, 0) + 1
        donors.add(e.donor)
    return {
        "total": len(entries),
        "by_portability": by_port,
        "donors": len(donors),
        "with_fit": sum(1 for e in entries if e.fit),
        "voted": sum(1 for e in entries if e.vote),
    }


def gaps(store) -> list[dict]:
    caps = store.list_capabilities(current_only=True)
    by_id = {c.id: c for c in caps}
    corpus = [f"{c.name} {c.what}".lower() for c in caps]
    out: list[dict] = []
    for p in store.list_projects():
        covered = sorted({by_id[f.capability_id].name
                          for f in store.list_fits(project_slug=p.slug)
                          if f.score >= FIT_MIN and f.capability_id in by_id})
        terms = sorted(t for t in _tokens(p.goal) if len(t) > 3)
        uncovered = [t for t in terms if not any(t in text for text in corpus)]
        out.append({"project": p.slug, "goal": p.goal, "covered": covered,
                    "uncovered_terms": uncovered})
    profile = store.get_profile()
    for intent in (profile.intents if profile else []):
        matches = [c.name for c in store.search_capabilities(intent, limit=5)]
        out.append({"project": None, "intent": intent, "matches": matches})
    return out


def _table(entries) -> str:
    if not entries:
        return "(no capabilities)"
    rows = []
    for e in entries[:_MAX_ROWS]:
        best = max(e.fit, key=lambda f: f["score"], default=None)
        fit = f"{best['project']} {best['score']:.2f}" if best else "-"
        rows.append((str(e.capability_id), e.portability, e.donor, e.name, fit))
    head = ("id", "port", "donor", "name", "best fit")
    widths = [max(len(r[i]) for r in [head, *rows]) for i in range(5)]
    lines = ["  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip() for r in [head, *rows]]
    if len(entries) > _MAX_ROWS:
        lines.append(f"… {len(entries) - _MAX_ROWS} more")
    return "\n".join(lines)


def _gaps_text(items) -> str:
    lines = []
    for g in items:
        if g.get("project") is None:
            lines.append(f"intent: {g['intent']}\n  matches: {', '.join(g['matches']) or '-'}")
        else:
            lines.append(f"{g['project']}: {g['goal']}\n"
                         f"  covered: {', '.join(g['covered']) or '-'}\n"
                         f"  uncovered terms: {', '.join(g['uncovered_terms']) or '-'}")
    return "\n".join(lines) or "(no projects or intents)"


def run_cli(store, args) -> dict:
    if getattr(args, "gaps", False):
        items = gaps(store)
        return {"gaps": items, "text": _gaps_text(items)}
    entries = build_catalog(
        store, search=getattr(args, "search", None),
        portability=getattr(args, "portability", None), donor=getattr(args, "donor", None),
        fit=getattr(args, "fit", None))
    return {"summary": summary(entries), "items": [e.to_dict() for e in entries],
            "text": _table(entries)}

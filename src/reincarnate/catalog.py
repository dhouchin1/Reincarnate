"""Capability catalog: filtered, scored views over the store plus a gap report."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .overlay import _tokens

FIT_MIN = 0.3
_PORT_WEIGHT = {"high": 1.0, "medium": 0.6, "low": 0.3}
_MAX_ROWS = 60
_SEARCH_SHOWN = 50  # rows a search displays, applied after every filter
_ALL = None  # search_matches limit: every qualifying row, so filters run before the cut


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
    provides: list = field(default_factory=list)
    requires: list = field(default_factory=list)
    novelty: str = ""
    acceptance: list = field(default_factory=list)
    data_shapes: list = field(default_factory=list)
    key_values: list = field(default_factory=list)

    def best_fit(self) -> float:
        return max((f["score"] for f in self.fit), default=0.0)

    def to_dict(self) -> dict:
        return asdict(self)


def _entry_score(portability: str, best_fit: float, vote: int) -> float:
    return round(_PORT_WEIGHT.get(portability, 0.6) + 0.5 * best_fit + 0.25 * vote, 4)


def build_catalog(store, *, search=None, portability=None, donor=None, fit=None,
                  gaps=False, novelty=None, needs_review=False) -> list[CatalogEntry]:
    """Scored catalog entries. `gaps` is handled by run_cli; it does not alter the entries."""
    if search:
        hits = store.search_matches(search, limit=_ALL)
        caps = [c for c, _ in hits]
        matched = {c.id: len(w) for c, w in hits}
    else:
        caps, matched = store.list_capabilities(current_only=True), {}
    fits: dict[int, list] = {}
    for f in store.list_fits():
        fits.setdefault(f.capability_id, []).append(
            {"project": f.project_slug, "score": f.score, "how": f.how, "source": f.source})
    votes = store.get_votes()
    out: list[CatalogEntry] = []
    for c in caps:
        if portability and c.portability != portability:
            continue
        if donor and c.donor.lower() != donor.lower():
            continue
        if needs_review and not c.needs_review:
            continue
        if novelty and c.novelty != ("" if novelty == "unrated" else novelty):
            continue
        cfits = fits.get(c.id, [])
        if fit and not any(f["project"] == fit and f["score"] >= FIT_MIN for f in cfits):
            continue
        vote = votes.get(c.id, 0)
        e = CatalogEntry(
            capability_id=c.id, donor=c.donor, license=c.license, name=c.name, what=c.what,
            portability=c.portability, portability_notes=c.portability_notes,
            security_notes=c.security_notes, files=list(c.files),
            dependencies=list(c.dependencies), fit=cfits, vote=vote,
            provides=list(c.provides), requires=list(c.requires), novelty=c.novelty,
            acceptance=list(c.acceptance), data_shapes=list(c.data_shapes),
            key_values=list(c.key_values))
        e.score = _entry_score(c.portability, e.best_fit(), vote)
        out.append(e)
    if search:
        # matched-word count leads, then votes (up before, down after equal matches), then
        # FTS relevance; score only breaks ties so a strong match is never buried under an
        # unrelated high-portability row.
        rank = {c.id: i for i, c in enumerate(caps)}
        out.sort(key=lambda e: (-matched.get(e.capability_id, 0), -e.vote,
                                rank.get(e.capability_id, 1 << 30), -e.score))
    else:
        out.sort(key=lambda e: (-e.score, -e.vote, e.name.lower()))
    return out


def summary(entries, shown: int | None = None) -> dict:
    by_port: dict[str, int] = {}
    donors: set[str] = set()
    for e in entries:
        by_port[e.portability] = by_port.get(e.portability, 0) + 1
        donors.add(e.donor)
    return {
        "total": len(entries),
        "shown": min(len(entries), shown if shown is not None else len(entries)),
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
        hits = store.search_matches(intent, limit=5)
        out.append({"project": None, "intent": intent, "matches": [c.name for c, _ in hits],
                    "matched": [{"name": c.name, "words": words} for c, words in hits]})
    return out


def _table(entries, total: int | None = None) -> str:
    if not entries:
        return "(no capabilities found; add repos with /reincarnate:comprehend owner/repo)"
    rows = []
    for e in entries[:_MAX_ROWS]:
        best = max(e.fit, key=lambda f: f["score"], default=None)
        fit = f"{best['project']} {best['score']:.2f}" if best else "-"
        rows.append((str(e.capability_id), e.portability, e.novelty or "-", e.donor, e.name, fit))
    head = ("id", "port", "novelty", "donor", "name", "best fit")
    widths = [max(len(r[i]) for r in [head, *rows]) for i in range(6)]
    lines = ["  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip() for r in [head, *rows]]
    more = (len(entries) if total is None else total) - min(len(entries), _MAX_ROWS)
    if more > 0:
        lines.append(f"… {more} more")
    return "\n".join(lines)


def _gaps_text(items) -> str:
    lines = []
    for g in items:
        if g.get("project") is None:
            found = ", ".join(f"{m['name']} ({', '.join(m['words'])})" for m in g["matched"])
            lines.append(f"intent: {g['intent']}\n  matches: {found or '-'}")
        else:
            lines.append(f"{g['project']}: {g['goal']}\n"
                         f"  covered: {', '.join(g['covered']) or '-'}\n"
                         f"  uncovered terms: {', '.join(g['uncovered_terms']) or '-'}")
    return "\n".join(lines) or "(no projects or intents)"


def run_cli(store, args) -> dict:
    if getattr(args, "gaps", False):
        items = gaps(store)
        return {"gaps": items, "text": _gaps_text(items)}
    donor, fit = getattr(args, "donor", None), getattr(args, "fit", None)
    if donor and not any(d.donor.lower() == donor.lower() for d in store.list_dossiers()):
        return {"error": f"no dossier for donor {donor}"}
    if fit and store.get_project(fit) is None:
        return {"error": f"no such project: {fit}"}
    entries = build_catalog(
        store, search=getattr(args, "search", None),
        portability=getattr(args, "portability", None), donor=getattr(args, "donor", None),
        fit=getattr(args, "fit", None),
        novelty=getattr(args, "novelty", None),
        needs_review=bool(getattr(args, "needs_review", False)))
    shown = entries[:_SEARCH_SHOWN] if getattr(args, "search", None) else entries
    return {"summary": summary(entries, len(shown)), "items": [e.to_dict() for e in shown],
            "text": _table(shown, len(entries))}

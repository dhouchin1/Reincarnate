"""The studio's fit check for a hand-picked portfolio of parts.

It reuses the design engine's wiring context and `rank_candidates`, so the page and
`design pick` agree on what is wired, what is open and which catalog parts could fill it.
"""

from __future__ import annotations

from . import compose, design, estimate, licenses


def fit(store, ids, roles=None) -> dict:
    """The fit check for capability `ids` (ints, in portfolio order); ValueError for an id
    that is missing or retired. `roles` ({id: role}) is accepted for symmetry with the design
    call; a role changes no wiring, so it is not used here."""
    ids = list(dict.fromkeys(int(i) for i in ids))
    profile = store.get_profile()
    caps = []
    for cid in ids:
        cap = store.get_capability(cid)
        if cap is None:
            raise ValueError(f"no such part: c{cid}")
        if cap.retired_at:
            raise ValueError(f"part c{cid} is retired: a re-comprehension no longer found it")
        caps.append(cap)

    ctx = design._wiring_context(store)
    rows = ctx["rows"]
    w = design.option_wiring(ctx, [f"c{i}" for i in ids])

    def name(ref) -> str:
        return str((ref or {}).get("name") or (ref or {}).get("id") or "?")

    wired = [{"consumer": name(x["consumer"]), "token": x["token"],
              "provider": name(x["provider"])} for x in w["wired"]]
    unmet = []
    for u in w["unmet"]:
        cands = [{"id": c["capability_id"], "name": c["name"], "donor": c["donor"],
                  "license": licenses.normalize(rows[c["cid"]]["license"])}
                 for c in design.rank_candidates(ctx, u, profile) if not c["deny"]]
        unmet.append({"consumer": name(u["consumer"]), "token": u["token"],
                      "candidates": cands})
    settings = [f"{x['token']} (for {name(x['consumer'])})" for x in w["settings"]]
    external = [f"{x['token']} (for {name(x['consumer'])})" for x in w["external"]]

    by_license: dict[str, dict] = {}
    for c in caps:
        lic = licenses.normalize(c.license)
        entry = by_license.setdefault(lic, {
            "license": lic, "level": compose.license_level(c.license, profile) or "ok",
            "review": licenses.is_review(c.license), "parts": []})
        entry["parts"].append(rows[f"c{c.id}"]["name"])

    # every registered project is listed (0.0 = nothing picked fits it yet), so the page can tell
    # "no fit" from "no projects"
    best: dict[str, float] = {p.slug: 0.0 for p in store.list_projects()}
    for cid in ids:
        for f in store.list_fits(capability_id=cid):
            best[f.project_slug] = max(best.get(f.project_slug, f.score), f.score)
    projects = [{"slug": slug, "score": round(score, 2)}
                for slug, score in sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))]

    read = sum(1 for c in caps if c.deepened_at)
    cost = estimate.estimate_deepen(len(caps) - read)["low"]
    scaffold, glue = 1, 1 if wired or unmet else 0  # no target yet: assume a new project
    return {
        "wired": wired, "unmet": unmet, "settings": settings, "external": external,
        "licenses": list(by_license.values()), "projects": projects,
        "deepened": {"n": read, "of": len(caps)}, "deepen_cost": cost,
        "kit_steps": {"total": scaffold + len(caps) + len(unmet) + glue, "scaffold": scaffold,
                      "parts": len(caps), "fresh": len(unmet), "glue": glue},
    }

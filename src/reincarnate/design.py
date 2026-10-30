"""Multi-option app design: an idea plus the catalog -> 2-4 named options (Lean, Complete,
Alternative, Bold), each wired, flagged and scored. `pick` turns one option into a regular
Composition so `reincarnate kit` builds it unchanged.

Port of Forge Studio's compose.py onto this store. Pipeline:
  1. pool: current capabilities (commodity excluded by default), ranked lexically, optionally
     re-ranked by the Jev shortlist hook;
  2. ONE design call (claude-p), a session-authored file (session), or a deterministic pick
     (offline) produces requirements, options and coverage labels;
  3. code validates: ids resolve, >=2 real capabilities, no near-duplicate options,
     coverage relabelling, license/security flags;
  4. code checks interface wiring (interfaces.check_wiring) against the whole catalog;
  5. ONE isolated Haiku call judges every token-proposed link; rejected links become unmet.
Designs never carry donor file contents (not even file paths).
"""

from __future__ import annotations

import importlib
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from . import compose, licenses, salvage, schema
from .interfaces import check_wiring
from .models import Composition, Design
from .prompts import UNTRUSTED_DATA_RULE
from .store import now

SLOTS: list[tuple[str, str, str]] = [
    ("lean", "Lean", "the fewest capabilities that still make a usable first version"),
    ("complete", "Complete", "covers the most requirements straight from the catalog"),
    ("alternative", "Alternative",
     "built on a different core capability or donor than the others"),
    ("bold", "Bold", "a more ambitious product angle on the same goal"),
]
COVERAGE_KINDS = ("catalog", "glue", "net-new")
MAX_CAPS = 8
MAX_JACCARD = 0.7
POOL_CAP = 120
SHORTLIST_KEEP = 40
DESIGN_MODEL = "claude-opus-5-5"
VERIFY_MODEL = "claude-haiku-5-5"
OFFLINE_LEAN = 3
OFFLINE_COMPLETE = 8
_WIRING_KINDS = ("wired", "unmet", "settings", "external", "owned", "platform", "unknown")

SYSTEM = (
    "You are Reincarnate's designer, a pragmatic product-minded principal engineer. A developer "
    "describes an application; you design it from a catalog of reusable capabilities, each "
    "comprehended from an open-source donor repository and to be reimplemented clean-room (so a "
    "donor's language never constrains the target stack). You work from catalog rows only and "
    "never from donor source. You favour elegant combinations whose parts genuinely plug "
    "together over grab-bags. Commodity parts are glue the builder writes, not catalog parts. "
    "Respond with ONLY one JSON object."
)

VERIFY_SYSTEM = (
    "You check whether proposed connections between software components are real. Be strict: "
    "a link is valid only if the provider genuinely supplies what the consumer needs in this "
    "application. Respond with ONLY one JSON object."
)

_WORD = re.compile(r"[a-z][a-z0-9]+")
_STOP = set("a an and the of to for with in on by is are be or my our your that this it as at "
            "from into app application build want need using use can will should which who what "
            "when".split())


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 2]


def _cid(ref: Any) -> str:
    """`c17`, `17`, 17 -> "c17"; anything else -> "" ."""
    if isinstance(ref, bool):
        return ""
    if isinstance(ref, int):
        return f"c{ref}"
    s = str(ref or "").strip().lower()
    if s.isdigit():
        return f"c{int(s)}"
    if re.fullmatch(r"c\d+", s):
        return f"c{int(s[1:])}"
    return ""


def _row(c) -> dict[str, Any]:
    """A Capability as a catalog row (no files, no dependencies; donor text is gated, so a
    path or code snippet in it never reaches the design model or a stored option)."""
    return {"cid": f"c{c.id}", "capability_id": c.id, "name": salvage.gate_line(c.name),
            "donor": salvage.gate_line(c.donor, n=200), "license": c.license or "",
            "portability": c.portability or "medium", "novelty": c.novelty or "",
            "what": salvage.gate_text(c.what), "provides": list(c.provides or []),
            "requires": list(c.requires or []),
            "security_notes": salvage.gate_text(c.security_notes)}


# -- pool -----------------------------------------------------------------------------------
def rank_catalog(caps: list, idea: str, cap: int = POOL_CAP) -> list:
    """Lexical TF-IDF over name + what + provides, plus a portability bonus; best `cap`.

    Unlike Forge Studio this always sorts (ties by id), so the offline engine's "top N" is
    the most relevant N even when the whole catalog fits."""
    q = Counter(_words(idea))
    df: Counter = Counter()
    docs = []
    for c in caps:
        ws = set(_words(f"{c.name} {c.what} {' '.join(c.provides or [])}"))
        docs.append(ws)
        df.update(ws)
    n = max(1, len(caps))
    port = {"high": 0.3, "medium": 0.15, "low": 0.0}
    scored = []
    for c, ws in zip(caps, docs, strict=True):
        s = sum(q[w] * math.log(1 + n / df[w]) for w in ws if w in q)
        scored.append((s + port.get(c.portability, 0), c.id, c))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [c for _, _, c in scored[:cap]]


def _client_name(client) -> str:
    return str(getattr(client, "name", "") or type(client).__name__)


def _pool(store, idea, *, include_commodity=False, cap=POOL_CAP, shortlist=None):
    """-> (capabilities, ranking used, notes)."""
    caps = [c for c in store.list_capabilities(current_only=True)
            if include_commodity or c.novelty != "commodity"]
    caps = rank_catalog(caps, idea, cap)
    notes: list[str] = []
    used = "lexical"
    if shortlist is None or not caps:
        return caps, used, notes
    try:
        jev = importlib.import_module("reincarnate.jev")
    except ImportError:
        notes.append("shortlist requested but reincarnate.jev is unavailable; lexical pool kept")
        return caps, used, notes
    fn = getattr(jev, "shortlist", None)
    if not callable(fn):
        notes.append("reincarnate.jev has no shortlist(); lexical pool kept")
        return caps, used, notes
    private: list = []
    if _client_name(shortlist) == "jev":  # the live API: private parts never leave the machine
        from .enrich import split_private
        caps, private = split_private(store, caps)
        if private:
            notes.append(f"{len(private)} private parts kept out of the Jev re-rank "
                         "(kept in the pool, lexical order)")
        if not caps:
            return private, used, notes
    try:
        got = fn(idea, caps, client=shortlist, keep=SHORTLIST_KEEP)
        known = {c.id for c in caps}
        out, seen = [], set()
        for c in got or []:
            cid = getattr(c, "id", None)
            if cid in known and cid not in seen:
                seen.add(cid)
                out.append(c)
        if not out:
            raise ValueError("shortlist returned no catalog capabilities")
    except Exception as e:  # noqa: BLE001 - a failed re-rank must never block a design
        notes.append(f"shortlist failed ({type(e).__name__}: {e}); lexical pool kept")
        return rank_catalog(caps + private, idea, cap), used, notes
    return out + private, _client_name(shortlist), notes


def pool(store, idea, *, include_commodity=False, cap=POOL_CAP, shortlist=None) -> list:
    """Current capabilities to design from, most relevant first."""
    return _pool(store, idea, include_commodity=include_commodity, cap=cap,
                 shortlist=shortlist)[0]


# -- prompts --------------------------------------------------------------------------------
CATALOG_CHARS = 120_000


def _catalog_line(c: dict[str, Any]) -> str:
    line = (f"{c['cid']} | {c['donor']} | {c['name']} | {c['portability']} | "
            f"{c['license'] or 'unknown'} | {c['novelty'] or 'unrated'} | {c['what']}")
    if c["provides"]:
        line += f" | gives: {', '.join(c['provides'])}"
    if c["requires"]:
        line += f" | needs: {', '.join(c['requires'])}"
    return line


def bound_rows(caps: list[dict[str, Any]], pinned=(),
               limit: int = CATALOG_CHARS) -> tuple[list[dict[str, Any]], int]:
    """Prompt-sized rows: each `what` clipped, then the lowest-ranked rows (the pool is ranked,
    last = lowest) dropped until the block fits `limit`. Pinned cids are never dropped.
    Returns (rows, number dropped)."""
    rows = [{**c, "what": schema.clip_words(c["what"])} for c in caps]
    sizes = [len(_catalog_line(c)) + 1 for c in rows]
    total, keep = sum(sizes), [True] * len(rows)
    for i in range(len(rows) - 1, -1, -1):
        if total <= limit:
            break
        if rows[i]["cid"] not in pinned:
            keep[i] = False
            total -= sizes[i]
    return [c for c, k in zip(rows, keep, strict=True) if k], keep.count(False)


def catalog_block(caps: list[dict[str, Any]], pinned=()) -> str:
    lines = [_catalog_line(c) for c in bound_rows(caps, pinned)[0]]
    return (
        f"{UNTRUSTED_DATA_RULE}\n\n"
        "CAPABILITY CATALOG (id | donor | capability | portability | license | novelty | what | "
        "gives/needs interface tokens). A capability's 'needs' token is satisfied when another "
        "capability in the SAME option 'gives' that exact token; ext: needs come from the "
        "environment.\n" + "\n".join(lines)
    )


def stack_preference(stack) -> str:
    """The profile stack as the developer's preference ("" when the profile has none)."""
    items = [str(x).strip() for x in stack or [] if str(x).strip()]
    if not items:
        return ""
    return (f"The developer's preferred stack is {', '.join(items)}: prefer it unless the "
            "option's nature calls for another stack, and say why in the rationale.\n")


def pin_text(pins: list[tuple[str, str]]) -> str:
    """The prompt paragraph naming required parts: [(`c17`, role)]."""
    if not pins:
        return ""
    items = ", ".join(f"{cid} ({role})" if role else cid for cid, role in pins)
    return ("REQUIRED PARTS: the developer pinned these capabilities, so EVERY option MUST "
            f"include all of them, each doing the role given in brackets: {items}. Build the "
            "rest of each option around them.\n")


def compose_prompt(idea: str, n_options: int, stack=None,
                   pins: list[tuple[str, str]] | None = None) -> str:
    slots = SLOTS[:n_options]
    slot_text = "\n".join(f'  - "{s}" ({label}): {desc}' for s, label, desc in slots)
    return (
        f"PROJECT GOAL (from the developer):\n{idea.strip()}\n\n"
        "Step 1: break the goal into 6-12 concrete, testable requirements (ids r1, r2, ...).\n"
        f"Step 2: propose exactly {len(slots)} options, one per slot:\n{slot_text}\n"
        f"Each option uses 2-{MAX_CAPS} catalog capabilities referenced ONLY by id, chosen for "
        "how elegantly they combine; prefer combinations whose needs are satisfied inside the "
        "option. Options must differ materially in which capabilities they use. Give each a "
        "target stack. List foundational parts (those the others depend on) first.\n"
        + stack_preference(stack) + pin_text(pins or []) +
        "Step 3: for EACH option label EVERY requirement: \"catalog\" (mainly delivered by one "
        "of THAT option's capabilities; give its id), \"glue\" (integration code between them), "
        "or \"net-new\" (nothing in the option serves it).\n"
        "Step 4: for each option list the glue components to build and the net-new work, "
        "briefly.\n\n"
        "Return JSON with EXACTLY these keys:\n"
        '{"title": "short name for the project", "requirements": [{"id": "r1", "text": "..."}], '
        '"options": [{"slot": "<slot>", "name": "product-style name", "pitch": "one sentence: '
        'who uses it and why", "rationale": "why THESE parts fit together", "stack": "e.g. '
        'TypeScript + Next.js + SQLite", "capabilities": [{"id": "c17", "role": "what it does '
        'in THIS option"}], "coverage": [{"req": "r1", "kind": "catalog|glue|net-new", "cap": '
        '"c17 or empty"}], "glue": ["..."], "new_work": ["..."]}]}\n'
        "Never invent capability ids."
    )


# -- validation -----------------------------------------------------------------------------
def _norm_kind(k: Any) -> str:
    s = str(k or "").strip().lower().replace("_", "-").replace(" ", "-")
    return s if s in COVERAGE_KINDS else ""


def _license_flags(caps: list[dict[str, Any]], profile) -> list[str]:
    blocked, review = set(), set()
    for c in caps:
        label = f"{c['donor']} ({licenses.normalize(c['license'])})"
        level = compose.license_level(c["license"], profile)
        if level == "deny":
            blocked.add(label)
        elif level == "warn" or licenses.is_review(c["license"]):
            review.add(label)
    flags = []
    if blocked:
        flags.append("License blocked: " + ", ".join(sorted(blocked)))
    if review:
        flags.append("License review: " + ", ".join(sorted(review)))
    return flags


def _lines(v: Any) -> list[str]:
    """A list of strings; a lone string is one item, not its characters."""
    items = v if isinstance(v, list) else [v] if isinstance(v, str) else []
    return [str(x) for x in items if str(x).strip()][:10]


def _text_list(v: Any, sep: str) -> str:
    return sep.join(_lines(v)) if isinstance(v, list) else str(v or "").strip()


def build_options(obj: dict[str, Any], by_cid: dict[str, dict[str, Any]], n_options: int,
                  dropped: list[str], profile) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    obj = obj if isinstance(obj, dict) else {}
    reqs: list[dict[str, str]] = []
    for i, r in enumerate(obj.get("requirements") or [], 1):
        text = str((r.get("text") if isinstance(r, dict) else r) or "").strip()
        rid = str((r.get("id") if isinstance(r, dict) else "") or "").strip() or f"r{i}"
        if text and rid not in {x["id"] for x in reqs}:
            reqs.append({"id": rid, "text": text})
    req_ids = {r["id"] for r in reqs}
    by_name = {c["name"].lower(): cid for cid, c in by_cid.items()}
    slot_names = [s for s, _, _ in SLOTS]
    out: list[dict[str, Any]] = []
    used_slots: set[str] = set()
    for raw in obj.get("options") or []:
        if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
            continue
        name = str(raw["name"]).strip()
        if len(out) >= n_options:  # say so rather than drop an option silently
            dropped.append(f"{name}: beyond the {n_options} options asked for "
                           f"(--options {min(len(SLOTS), n_options + 1)} keeps it)")
            continue
        caps: list[dict[str, Any]] = []
        for c in raw.get("capabilities") or []:
            ref = c.get("id", c.get("capability_id")) if isinstance(c, dict) else c
            cid = _cid(ref)
            if cid not in by_cid:
                cid = by_name.get(str(ref or "").strip().lower(), "")
            if not cid:
                dropped.append(f"{name}: unknown or excluded capability {ref} dropped")
                continue
            if any(x["id"] == cid for x in caps):
                continue
            cat = by_cid[cid]
            caps.append({
                "id": cid, "capability_id": cat["capability_id"], "name": cat["name"],
                "donor": cat["donor"], "license": cat["license"],
                "portability": cat["portability"], "novelty": cat["novelty"],
                "what": cat["what"],
                "role": str(c.get("role") or "").strip() if isinstance(c, dict) else "",
                "provides": cat["provides"], "requires": cat["requires"],
            })
            if len(caps) >= MAX_CAPS:
                break
        if len(caps) < 2:
            dropped.append(f"{name}: fewer than 2 real catalog capabilities")
            continue
        ids = {c["id"] for c in caps}
        clash = next((o for o in out if len(ids & {c["id"] for c in o["capabilities"]})
                      / len(ids | {c["id"] for c in o["capabilities"]}) > MAX_JACCARD), None)
        if clash:
            dropped.append(f"{name}: near-duplicate of {clash['name']}")
            continue
        slot = str(raw.get("slot") or "").strip().lower()
        if slot not in slot_names or slot in used_slots:
            slot = next((s for s in slot_names if s not in used_slots), "option")
        used_slots.add(slot)
        labelled: dict[str, dict[str, str]] = {}
        relabelled = 0
        for row in raw.get("coverage") or []:
            if not isinstance(row, dict):
                continue
            rid, kind = str(row.get("req") or ""), _norm_kind(row.get("kind"))
            if rid not in req_ids or not kind or rid in labelled:
                continue
            cap = _cid(row.get("cap")) or str(row.get("cap") or "")
            if kind == "catalog" and cap not in ids:
                kind, cap, relabelled = "glue", "", relabelled + 1
            labelled[rid] = {"req": rid, "kind": kind, "cap": cap if kind == "catalog" else ""}
        coverage = [labelled.get(r["id"]) or {"req": r["id"], "kind": "net-new", "cap": ""}
                    for r in reqs]
        unlabelled = [r["id"] for r in reqs if r["id"] not in labelled]
        flags = _license_flags(caps, profile)
        sec = [c["name"] for c in caps if (by_cid[c["id"]].get("security_notes") or "").strip()]
        if sec:
            flags.append(f"Security notes on {len(sec)} part{'s' if len(sec) != 1 else ''}")
        if relabelled:
            flags.append(f"{relabelled} coverage label(s) pointed outside the option and were "
                         "counted as glue")
        out.append({
            "slot": slot,
            "slot_label": dict((s, label) for s, label, _ in SLOTS).get(slot, slot.title()),
            "name": name, "slug": compose.slugify(name) or "option",
            "pitch": str(raw.get("pitch") or "").strip(),
            "rationale": str(raw.get("rationale") or "").strip(),
            "stack": _text_list(raw.get("stack"), ", "),
            "capabilities": caps, "coverage": coverage, "flags": flags,
            "glue": _lines(raw.get("glue")), "new_work": _lines(raw.get("new_work")),
            "donors": sorted({c["donor"] for c in caps}),
        })
        if unlabelled:
            out[-1]["unlabelled"] = unlabelled
    return out, reqs


# -- wiring ---------------------------------------------------------------------------------
def _wiring_context(store) -> dict[str, Any]:
    """Everything check_wiring needs about the current catalog, built once."""
    vocab = store.get_vocabulary()
    catalog = [_row(c) for c in store.list_capabilities(current_only=True)]
    return {
        "vocab": vocab,
        "rows": {c["cid"]: c for c in catalog},
        "ifaces": {c["cid"]: (c["provides"], c["requires"]) for c in catalog},
        "rank": {c["cid"]: {"high": 0, "medium": 1, "low": 2}.get(c["portability"], 3)
                 for c in catalog},
        "names": {c["cid"]: {"id": c["cid"], "capability_id": c["capability_id"],
                             "name": c["name"], "donor": c["donor"]} for c in catalog},
    }


def option_wiring(ctx: dict[str, Any], cids: list[str]) -> dict[str, Any]:
    """check_wiring for one option's capability ids (`c17`), with refs resolved to names."""
    names = ctx["names"]

    def ref(cid):
        return dict(names.get(cid) or {"id": cid, "capability_id": None, "name": cid,
                                       "donor": ""})

    # every outside provider is kept: auto-fill ranks them all (same donor first) and only the
    # display cuts the list, so a same-donor part is never lost behind better-ported strangers
    w = check_wiring(cids, ctx["ifaces"], platform=ctx["vocab"].platform,
                     rank=ctx["rank"], max_candidates=None).as_dict()
    for k in _WIRING_KINDS[:-1]:
        for item in w[k]:
            item["consumer"] = ref(item["consumer"])
            if "provider" in item:
                item["provider"] = ref(item["provider"])
            if "candidates" in item:
                item["candidates"] = [ref(c) for c in item["candidates"] if c in names]
    w["unknown"] = [ref(c) for c in w["unknown"]]
    w["verified"] = False
    return w


def attach_wiring(store, options: list[dict[str, Any]]) -> None:
    """check_wiring per option against every current capability's interfaces."""
    ctx = _wiring_context(store)
    for o in options:
        o["wiring"] = option_wiring(ctx, [c["id"] for c in o["capabilities"]])


_PORT_RANK = {"high": 0, "medium": 1, "low": 2}


def _recount(w: dict[str, Any]) -> None:
    w["counts"] = {k: len(w[k]) for k in _WIRING_KINDS}


def verify_links(idea: str, options: list[dict[str, Any]], by_cid: dict[str, dict[str, Any]],
                 *, dropped: list[str], model: str = VERIFY_MODEL) -> float:
    """One isolated, tool-less call judges every wired link. -> cost in USD. Never raises."""
    links = [(oi, li, w) for oi, o in enumerate(options)
             for li, w in enumerate(o["wiring"]["wired"])]
    if not links:
        return 0.0
    lines = []
    for k, (_, _, w) in enumerate(links):
        c, p = by_cid.get(w["consumer"]["id"], {}), by_cid.get(w["provider"]["id"], {})
        lines.append(f"L{k}: {c.get('name')} ({c.get('what', '')[:160]}) NEEDS {w['token']} <- "
                     f"PROVIDED BY {p.get('name')} ({p.get('what', '')[:160]})")
    prompt = (
        f"{VERIFY_SYSTEM}\n\n{UNTRUSTED_DATA_RULE}\n\nApplication being designed: {idea}"
        "\n\nProposed links:\n"
        + "\n".join(lines)
        + '\n\nFor each link answer whether it is valid. Return JSON: '
          '{"verdicts": {"L0": true, "L1": false}}'
    )
    from . import claude_runner

    try:
        res = claude_runner.run(prompt, cwd=Path.cwd(), model=model, max_turns=1, tools="",
                                label=f"verify {len(links)} design link(s)")
    except Exception as e:  # noqa: BLE001 - unverified wiring is still useful; say so
        dropped.append(f"link verification skipped: {type(e).__name__}: {e}")
        return 0.0
    if res.failed:
        dropped.append(f"link verification skipped: model call failed: {res.error_message()}")
        return res.cost_usd
    obj = compose.extract_json_object(res.text)
    verdicts = obj.get("verdicts") if isinstance(obj, dict) else None
    if not isinstance(verdicts, dict):
        dropped.append("link verification skipped: no verdicts in the model's reply")
        return res.cost_usd
    rejected: dict[int, list[int]] = {}
    for k, (oi, li, _) in enumerate(links):
        if verdicts.get(f"L{k}") is False:
            rejected.setdefault(oi, []).append(li)
    for oi, lis in rejected.items():
        w = options[oi]["wiring"]
        for li in sorted(lis, reverse=True):
            link = w["wired"].pop(li)
            have = {c["id"] for c in options[oi]["capabilities"]}
            others = [c for c in sorted(by_cid.values(), key=lambda r: _PORT_RANK.get(
                          r.get("portability"), 3))
                      if link["token"] in (c.get("provides") or []) and c["cid"] not in have]
            w["unmet"].append({"token": link["token"], "consumer": link["consumer"],
                               "candidates": [{"id": c["cid"], "capability_id": c["capability_id"],
                                               "name": c["name"], "donor": c["donor"]}
                                              for c in others],
                               "rejected_provider": link["provider"]})
    for o in options:
        o["wiring"]["verified"] = True
        _recount(o["wiring"])
    return res.cost_usd


def score_options(options: list[dict[str, Any]]) -> None:
    for o in options:
        cov = Counter(c["kind"] for c in o["coverage"])
        total = max(1, len(o["coverage"]))
        wc = o.get("wiring", {}).get("counts", {})
        links = wc.get("wired", 0) + wc.get("unmet", 0)
        o["counts"] = {k: cov.get(k, 0) for k in COVERAGE_KINDS}
        o["scores"] = {
            "reuse": round(cov.get("catalog", 0) / total, 2),
            "fit": round(wc.get("wired", 0) / links, 2) if links else None,
        }
        if wc.get("unmet"):
            fillable = sum(1 for u in o["wiring"]["unmet"] if u.get("candidates"))
            o["flags"].append(f"{wc['unmet']} unmet need(s)"
                              + (f", {fillable} fillable from the catalog" if fillable else ""))


# -- engines --------------------------------------------------------------------------------
def offline_design(idea: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic: lean = top 3 of the pool, complete = top 8; one requirement (the idea)."""

    def option(slot, name, picked):
        return {"slot": slot, "name": name, "pitch": idea.strip(),
                "rationale": f"Offline pick: the top {len(picked)} capabilities by lexical "
                             "relevance to the idea.",
                "stack": "",
                "capabilities": [{"id": r["cid"], "role": "component"} for r in picked],
                "coverage": [{"req": "r1", "kind": "catalog", "cap": picked[0]["cid"]}]
                if picked else [],
                "glue": [], "new_work": []}

    return {"title": "",
            "requirements": [{"id": "r1", "text": idea.strip()}],
            "options": [option("lean", "Lean core", rows[:OFFLINE_LEAN]),
                        option("complete", "Complete build", rows[:OFFLINE_COMPLETE])]}


class DesignError(ValueError):
    """A design that could not be built after money was spent; carries the cost."""

    def __init__(self, msg: str, cost_usd: float = 0.0):
        super().__init__(msg)
        self.cost_usd = round(float(cost_usd or 0.0), 4)


def _save_raw_reply(idea: str, res) -> Path:
    """Keep a failed design reply: an Opus call is too expensive to lose without a trace."""
    import json

    from . import paths

    d = paths.ensure(paths.data_dir() / "designs")
    path = d / f"{compose.slugify(idea, 40) or 'design'}-{now()[:19].replace(':', '')}.raw.txt"
    body = res.text or res.stderr or (json.dumps(res.raw, indent=2) if res.raw else "")
    path.write_text(body, encoding="utf-8")
    return path


def _claude_design(idea, rows, n_options, model, stack=None,
                   pins=None) -> tuple[dict[str, Any], float]:
    from . import claude_runner

    model = model or DESIGN_MODEL
    prompt = (f"{SYSTEM}\n\n{catalog_block(rows)}\n\n"
              f"{compose_prompt(idea, n_options, stack, pins)}")
    res = claude_runner.run(prompt, cwd=Path.cwd(), model=model, max_turns=1, tools="",
                            label=f"design {n_options} options")
    obj = None if res.failed else compose.extract_json_object(res.text)
    if obj is None:
        why = (f"claude-p model call failed: {res.error_message()}" if res.failed
               else "claude-p returned no JSON design")
        path = _save_raw_reply(idea, res)
        raise DesignError(f"{why} (${res.cost_usd:.4f} spent; raw reply saved to {path})",
                          res.cost_usd)
    return obj, res.cost_usd


def _unique_design_slug(store, base: str) -> str:
    base = compose.slugify(base) or "design"
    if base.isdigit():  # an all-digit slug would be read back as an id
        base = f"d-{base}"
    slug, n = base, 2
    while store.get_design(slug) is not None:
        slug = f"{base}-{n}"
        n += 1
    return slug


def parse_pins(values) -> list[int]:
    """Capability ids from `17`, `c17`, 17 or comma lists of them (a list of those too);
    ValueError for anything else. Order is kept, repeats are dropped."""
    out: list[int] = []
    if isinstance(values, (str, int)):
        values = [values]
    for v in values or []:
        for tok in (str(v).split(",") if isinstance(v, str) else [v]):
            if isinstance(tok, str) and not tok.strip():
                continue
            cid = _cid(tok)
            if not cid:
                raise ValueError(f"not a capability id: {str(tok)[:40]!r} (use 17 or c17)")
            if int(cid[1:]) not in out:
                out.append(int(cid[1:]))
    return out


def _check_pins(store, pins: list[int], profile) -> list:
    """The pinned capabilities; ValueError for one that is missing, retired or denied."""
    caps = []
    for pid in pins:
        cap = store.get_capability(pid)
        if cap is None:
            raise ValueError(f"pinned part c{pid} does not exist")
        if cap.retired_at:
            raise ValueError(f"pinned part c{pid} ({salvage.gate_line(cap.name)}) is retired: "
                             "a re-comprehension no longer found it")
        if compose.license_level(cap.license, profile) == "deny":
            raise ValueError(f"pinned part c{pid} ({salvage.gate_line(cap.name)}) has a license "
                             f"your profile denies ({licenses.normalize(cap.license)})")
        caps.append(cap)
    return caps


def _clean_roles(roles, pins: list[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    for k, v in (roles or {}).items():
        cid = _cid(k)
        role = " ".join(str(v or "").split())[:200]
        if cid and int(cid[1:]) in pins and role:
            out[int(cid[1:])] = role
    return out


def apply_pins(options: list[dict[str, Any]], pins: list[int], roles: dict[int, str],
               by_cid: dict[str, dict[str, Any]], notes: list[str], profile) -> None:
    """Every pinned part in every option: missing ones are added (role from `roles`, else
    "pinned") and noted; all are marked `pinned`."""
    for o in options:
        have = {c["id"]: c for c in o["capabilities"]}
        added = False
        for pid in pins:
            cid = f"c{pid}"
            if cid in have:
                have[cid]["pinned"] = True
                if not have[cid].get("role") and roles.get(pid):
                    have[cid]["role"] = roles[pid]
                continue
            cat = by_cid[cid]
            o["capabilities"].append({
                "id": cid, "capability_id": cat["capability_id"], "name": cat["name"],
                "donor": cat["donor"], "license": cat["license"],
                "portability": cat["portability"], "novelty": cat["novelty"],
                "what": cat["what"], "role": roles.get(pid) or "pinned",
                "provides": cat["provides"], "requires": cat["requires"], "pinned": True})
            added = True
            notes.append(f"pinned part {cat['name']} ({cid}) was missing from option "
                         f"{o['name']}; added with role {roles.get(pid) or 'pinned'!r}")
        if not added:
            continue
        o["donors"] = sorted({c["donor"] for c in o["capabilities"]})
        # the added parts change the license and security flags
        rows = [by_cid[c["id"]] for c in o["capabilities"]]
        o["flags"] = [f for f in o["flags"]
                      if not f.startswith(("License ", "Security notes on "))]
        o["flags"] += _license_flags(rows, profile)
        sec = [r for r in rows if (r.get("security_notes") or "").strip()]
        if sec:
            o["flags"].append(f"Security notes on {len(sec)} part{'s' if len(sec) != 1 else ''}")


def propose_design(store, idea, *, n_options=3, engine="claude-p", proposal_path=None,
                   model=None, include_commodity=False, verify=True, shortlist=None,
                   pins=None, roles=None, progress=None) -> Design:
    """`progress(stage)` hears "pool", "design" and "verify" as each begins."""
    idea = (idea or "").strip()
    step = progress or (lambda stage: None)
    if not idea:
        raise ValueError("an idea is required")
    n_options = max(2, min(len(SLOTS), int(n_options or 3)))
    current = store.list_capabilities(current_only=True)
    if len(current) < 2:
        raise ValueError("the catalog needs at least 2 capabilities; comprehend a repo first")
    catalog = {f"c{c.id}": _row(c) for c in current}
    profile = store.get_profile()
    pin_ids = parse_pins(pins)
    pin_caps = _check_pins(store, pin_ids, profile)
    pin_roles = _clean_roles(roles, pin_ids)
    cost = 0.0
    step("pool")
    if engine == "session":
        # The session did its own retrieval; validate against every eligible capability.
        obj = compose.load_session_proposal(proposal_path)
        caps = [c for c in current if include_commodity or c.novelty != "commodity"]
        used, notes = "session", []
    elif engine in ("claude-p", "offline"):
        caps, used, notes = _pool(store, idea, include_commodity=include_commodity,
                                  shortlist=shortlist)
        obj = None
    else:
        raise ValueError(f"unknown engine: {engine}")
    if pin_caps:  # pinned parts lead the pool whatever their novelty or rank
        caps = pin_caps + [c for c in caps if c.id not in pin_ids]
    rows = [catalog[f"c{c.id}"] for c in caps]
    step("design")
    if engine == "claude-p":
        kw = {"pins": [(f"c{i}", pin_roles.get(i, "")) for i in pin_ids]} if pin_ids else {}
        prompt_rows, cut = bound_rows(rows, {f"c{i}" for i in pin_ids})
        if cut:
            notes.append(f"{cut} lowest-ranked catalog rows left out of the prompt to keep it "
                         f"under {CATALOG_CHARS:,} characters")
        obj, cost = _claude_design(idea, prompt_rows, n_options, model,
                                   list(profile.stack) if profile else None, **kw)
    elif engine == "offline":
        obj = offline_design(idea, rows)
    by_cid = {r["cid"]: r for r in rows}
    dropped: list[str] = []
    options, reqs = build_options(obj, by_cid, n_options, dropped, profile)
    if not options:
        why = f" ({'; '.join(dropped)})" if dropped else ""
        msg = f"no workable option could be built from the catalog{why}"
        if engine == "claude-p":
            raise DesignError(f"{msg} (${cost:.4f} spent)", cost)
        raise ValueError(msg)
    if pin_ids:
        apply_pins(options, pin_ids, pin_roles, by_cid, notes, profile)
    attach_wiring(store, options)
    verified = False
    if verify and engine != "offline":
        step("verify")
        cost += verify_links(idea, options, catalog, dropped=dropped)
        verified = all(o["wiring"].get("verified") for o in options)
    score_options(options)
    raw_title = str(obj.get("title") or "").strip()
    d = Design(slug=_unique_design_slug(store, compose.slug_base(raw_title, idea)), idea=idea,
               title=raw_title or idea[:60], requirements=reqs, options=options, engine=engine,
               meta={"catalog_size": len(current), "pool_size": len(rows), "shortlist": used,
                     "dropped": dropped, "notes": notes, "verified": verified,
                     "model": (model or DESIGN_MODEL) if engine == "claude-p" else "",
                     "include_commodity": bool(include_commodity),
                     "pins": pin_ids, "cost_usd": round(cost, 4)},
               created=now())
    return store.save_design(d)


# -- pick -----------------------------------------------------------------------------------
def find_option(design: Design, option_ref: str) -> dict[str, Any] | None:
    ref = str(option_ref or "").strip().lower()
    for key in ("slot", "slug"):
        for o in design.options:
            if str(o.get(key) or "").lower() == ref:
                return o
    return None


MAX_AUTOFILL = 4
AUTOFILL_PASSES = 2
# Calibrated on the real catalog (2026-10-22, overlap / min): the weak other-donor candidates
# for the homelab dashboard scored 0.06-0.18, so relevance alone must clear 0.30.
RELEVANCE_MIN = 0.30
MAX_CITED = 3
# a net-new requirement counts as planned work when a new-work item overlaps it this much
NEW_WORK_MATCH = 0.5


def _resolve_ids(text: str, names: dict[str, str]) -> str:
    """`c17` -> the capability's name (unknown ids are left as they are)."""
    return salvage.resolve_cids(text, lambda cid: names.get(f"c{cid}"))


def filled_line(f: dict[str, Any]) -> str:
    return (f"`{f.get('token')}` for {f.get('consumer')}: provided by {f.get('name')} "
            f"(auto-filled, {f.get('reason')})")


def glue_after_autofill(glue: list[str], filled: list[dict[str, Any]]) -> list[str]:
    """Every glue bullet that mentions an auto-filled token is replaced by that fill's
    provided-by line (once per fill; repeats are dropped)."""
    out: list[str] = []
    for g in glue:
        low = str(g).lower()
        hits = [f for f in filled if str(f.get("token") or "") and
                str(f["token"]).lower() in low]
        for line in [filled_line(f) for f in hits] or [str(g)]:
            if line not in out:
                out.append(line)
    return out


def _glue_markdown(opt: dict[str, Any], names: dict[str, str] | None = None,
                   filled: list[dict[str, Any]] | None = None) -> str:
    parts = []
    filled = filled or []
    if opt.get("rationale"):
        parts.append(opt["rationale"])
    glue = glue_after_autofill([str(g) for g in opt.get("glue") or []], filled)
    if glue:
        parts.append("\n".join(f"- {g}" for g in glue))
    if filled:
        fparts = []
        for f in filled:
            if f.get("name") and f["name"] not in fparts:
                fparts.append(f["name"])
        parts.append("Auto-filled parts supersede any glue above that describes building them "
                     f"differently: {', '.join(fparts)}.")
    return _resolve_ids("\n\n".join(parts), names or {})


def _rejections(w: dict[str, Any]) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """(token, consumer id, rejected provider id) -> the other providers that were listed."""
    return {(u.get("token"), (u.get("consumer") or {}).get("id"),
             (u.get("rejected_provider") or {}).get("id")): list(u.get("candidates") or [])
            for u in w.get("unmet") or [] if u.get("rejected_provider")}


def _reapply_rejections(w: dict[str, Any], rejected: dict, have: set[str]) -> None:
    """Links the verifier rejected stay unmet when wiring is recomputed."""
    keep = []
    for link in w["wired"]:
        key = (link["token"], link["consumer"]["id"], link["provider"]["id"])
        if key in rejected:
            w["unmet"].append({"token": link["token"], "consumer": link["consumer"],
                               "candidates": [c for c in rejected[key] if c.get("id") not in have],
                               "rejected_provider": link["provider"]})
        else:
            keep.append(link)
    w["wired"] = keep
    _recount(w)


def text_relevance(a: str, b: str) -> float:
    """Content-word overlap of two texts over the smaller word set, 0..1 (2 decimals)."""
    wa, wb = set(_words(a)), set(_words(b))
    if not wa or not wb:
        return 0.0
    return round(len(wa & wb) / min(len(wa), len(wb)), 2)


def relevance(candidate: dict[str, Any], consumer: dict[str, Any], token: str,
              definition: str = "") -> float:
    """Content-word overlap between the candidate (name + what) and the need (consumer name +
    what, the token's thing words, its vocabulary definition), over the smaller set: 0..1."""
    thing = token.split(":", 1)[-1]
    return text_relevance(f"{candidate.get('name', '')} {candidate.get('what', '')}",
                          f"{consumer.get('name', '')} {consumer.get('what', '')} {thing} "
                          f"{definition}")


def rank_candidates(ctx: dict[str, Any], need: dict[str, Any], profile,
                    include_commodity: bool = True) -> list[dict[str, Any]]:
    """An unmet need's catalog candidates, best first: same donor as the consumer, then
    relevance, then the wiring rank; commodity parts last (or dropped when
    `include_commodity` is False). Each: {cid, capability_id, name, donor, relevance,
    same_donor, deny, commodity}."""
    rows = ctx["rows"]
    token = str(need.get("token") or "")
    consumer = rows.get((need.get("consumer") or {}).get("id")) or {}
    definition = str(ctx["vocab"].tokens.get(token, {}).get("definition") or "")
    out = []
    for i, c in enumerate(need.get("candidates") or []):
        r = rows.get(c.get("id"))
        if r is None or (r["novelty"] == "commodity" and not include_commodity):
            continue
        out.append({"cid": r["cid"], "capability_id": r["capability_id"], "name": r["name"],
                    "donor": r["donor"],
                    "relevance": relevance(r, consumer, token, definition),
                    "same_donor": bool(consumer) and r["donor"] == consumer.get("donor"),
                    "deny": compose.license_level(r["license"], profile) == "deny",
                    "commodity": r["novelty"] == "commodity",
                    "_key": (ctx["rank"].get(r["cid"], 99), i)})
    out.sort(key=lambda c: (c["commodity"], not c["same_donor"], -c["relevance"], c["_key"]))
    for c in out:
        del c["_key"]
    return out


def _fill_choice(ranked: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """The candidate to auto-fill and why, or (None, "") when none passes the gate. Denied
    licenses are never auto-filled."""
    allowed = [c for c in ranked if not c["deny"]]
    if not allowed:
        return None, ""
    top = allowed[0]
    if top["same_donor"]:
        return top, "same donor"
    if top["relevance"] >= RELEVANCE_MIN:
        return top, f"relevance {top['relevance']:.2f}"
    return None, ""


def autofill(ctx: dict[str, Any], opt: dict[str, Any], profile, include_commodity: bool = False):
    """Pull the best catalog candidate for each unmet need into the option, right before its
    consumer, when it is from the consumer's donor or relevant enough. Commodity parts are
    skipped unless the design allowed them. -> (capabilities, auto_filled, wiring)."""
    caps = [dict(c) for c in opt.get("capabilities") or []]
    w = opt.get("wiring") or option_wiring(ctx, [c["id"] for c in caps])
    rejected = _rejections(w)
    rows = ctx["rows"]
    filled: list[dict[str, Any]] = []
    for _ in range(AUTOFILL_PASSES):
        added = False
        for u in w.get("unmet") or []:
            if len(filled) >= MAX_AUTOFILL:
                break
            cands = [c for c in u.get("candidates") or [] if c.get("id") in rows]
            have = {c["id"] for c in caps}
            if not cands or u.get("rejected_provider") or any(c["id"] in have for c in cands):
                continue
            choice, reason = _fill_choice(rank_candidates(ctx, u, profile, include_commodity))
            if choice is None:
                continue
            best = rows[choice["cid"]]
            consumer = u.get("consumer") or {}
            cname = consumer.get("name") or consumer.get("id") or "?"
            pos = next((i for i, c in enumerate(caps) if c["id"] == consumer.get("id")),
                       len(caps))
            caps.insert(pos, {
                "id": best["cid"], "capability_id": best["capability_id"], "name": best["name"],
                "donor": best["donor"], "license": best["license"],
                "portability": best["portability"], "novelty": best["novelty"],
                "what": best["what"], "role": f"provides `{u['token']}` for {cname}",
                "provides": best["provides"], "requires": best["requires"],
            })
            filled.append({"token": u["token"], "consumer": cname,
                           "capability_id": best["capability_id"], "name": best["name"],
                           "donor": best["donor"], "reason": reason})
            added = True
        if not added:
            break
        w = option_wiring(ctx, [c["id"] for c in caps])
        _reapply_rejections(w, rejected, {c["id"] for c in caps})
    w["verified"] = bool((opt.get("wiring") or {}).get("verified"))
    return caps, filled, w


def _pick_gaps(opt: dict[str, Any], wiring: dict[str, Any], ctx: dict[str, Any],
               profile, caps: list[dict[str, Any]] | None = None,
               requirements: list[dict[str, Any]] | None = None,
               coverage: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    """Build-fresh gaps: the option's new work, one per requirement still net-new without a
    matching new-work item, and one per unmet token (placed before its first consumer, naming
    the others)."""
    vocab = ctx["vocab"]
    gaps = [{"need": str(x), "suggested_donors": [], "build_fresh": True}
            for x in opt.get("new_work") or [] if str(x).strip()]
    work = [str(x) for x in opt.get("new_work") or []]
    texts = {r.get("id"): str(r.get("text") or "").strip() for r in requirements or []
             if isinstance(r, dict)}
    for row in coverage or []:
        text = texts.get(row.get("req"))
        if row.get("kind") != "net-new" or not text:
            continue
        if any(text_relevance(text, w) >= NEW_WORK_MATCH for w in work):
            continue
        gaps.append({"need": f"Build fresh for requirement {row['req']}: {text}",
                     "suggested_donors": [], "build_fresh": True})
    order = {c["id"]: i for i, c in enumerate(caps or [])}
    groups: dict[str, list[dict[str, Any]]] = {}
    for u in wiring.get("unmet") or []:
        groups.setdefault(str(u.get("token") or ""), []).append(u)
    for token, us in groups.items():
        us = sorted(us, key=lambda u: order.get((u.get("consumer") or {}).get("id"), len(order)))
        u = us[0]
        consumer = u.get("consumer") or {}
        cname = consumer.get("name") if isinstance(consumer, dict) else str(consumer)
        cid = consumer.get("capability_id") if isinstance(consumer, dict) else None
        donors: list[str] = []
        for x in us:
            for c in x.get("candidates") or []:
                dn = c.get("donor") if isinstance(c, dict) else ""
                if dn and dn not in donors:
                    donors.append(dn)
        cited = [{k: c[k] for k in ("capability_id", "name", "donor", "relevance")}
                 for c in rank_candidates(ctx, u, profile)[:MAX_CITED]]
        gap = {"need": f"{token} for {cname}", "token": token,
               "definition": str(vocab.tokens.get(token, {}).get("definition") or ""),
               "consumer_capability_id": cid, "before_capability_id": cid,
               "suggested_donors": donors, "build_fresh": True}
        also, seen = [], {consumer.get("id")}
        for x in us[1:]:
            c = x.get("consumer") or {}
            if c.get("id") not in seen:
                seen.add(c.get("id"))
                also.append({"capability_id": c.get("capability_id"), "name": c.get("name")})
        if also:
            gap["also_consumers"] = also
        rejected = []
        for x in us:
            rn = (x.get("rejected_provider") or {}).get("name")
            if rn and rn not in rejected:
                rejected.append(rn)
        if rejected:
            gap["rejected_by_verifier"] = ", ".join(rejected)
        if cited:
            gap["candidates"] = cited
        gaps.append(gap)
    return gaps


def _covered_by_fill(coverage: list[dict[str, str]], requirements: list[dict[str, Any]],
                     filled: list[dict[str, Any]], caps: list[dict[str, Any]]) -> list[dict]:
    """A glue or net-new requirement whose text names an auto-filled part (or the token it
    provides) is now catalog coverage."""
    texts = {r.get("id"): str(r.get("text") or "").lower() for r in requirements or []
             if isinstance(r, dict)}
    cids = {c["capability_id"]: c["id"] for c in caps}
    out = []
    for row in coverage:
        text = texts.get(row.get("req")) or ""
        hit = next((f for f in filled if row.get("kind") in ("glue", "net-new") and text and (
            str(f.get("name") or "").lower() in text
            or str(f.get("token") or "").lower() in text)), None)
        if hit is not None and hit.get("capability_id") in cids:
            row = {"req": row["req"], "kind": "catalog", "cap": cids[hit["capability_id"]]}
        out.append(row)
    return out


def _refresh_flags(flags: list[str], caps: list[dict[str, Any]], ctx: dict[str, Any],
                   profile) -> list[str]:
    """Option flags for the part list after auto-fill: license and security flags are rebuilt
    for every part, and the stale unmet-needs flag is dropped (score_options re-adds it)."""
    keep = [f for f in flags if not f.startswith(("License ", "Security notes on "))
            and " unmet need(s)" not in f]
    keep += _license_flags(caps, profile)
    sec = [c for c in caps if (ctx["rows"].get(c["id"], {}).get("security_notes") or "").strip()]
    if sec:
        keep.append(f"Security notes on {len(sec)} part{'s' if len(sec) != 1 else ''}")
    return keep


def pick(store, design_ref, option_ref, *, target=None) -> Composition:
    design = store.get_design(design_ref)
    if design is None:
        raise ValueError(f"no such design: {design_ref}")
    opt = find_option(design, option_ref)
    if opt is None:
        have = ", ".join(f"{o.get('slot')}/{o.get('slug')}" for o in design.options)
        raise ValueError(f"no option {option_ref} in design {design.slug} (have: {have})")
    profile = store.get_profile()
    ctx = _wiring_context(store)
    caps, filled, wiring = autofill(ctx, opt, profile,
                                    bool((design.meta or {}).get("include_commodity")))
    # the option as it stands after auto-fill: counts, flags, coverage and scores follow it
    after = {**opt, "capabilities": caps, "wiring": wiring,
             "flags": _refresh_flags(list(opt.get("flags") or []), caps, ctx, profile),
             "coverage": _covered_by_fill([dict(r) for r in opt.get("coverage") or []],
                                          design.requirements, filled, caps)}
    score_options([after])
    rows = []
    for c in caps:
        cap = store.get_capability(c.get("capability_id"))
        if cap is not None:
            rows.append(compose._cap_row(cap, {}))
    names = {cid: n["name"] for cid, n in ctx["names"].items()}
    names.update({c["id"]: c["name"] for c in caps if c.get("id")})
    raw = {
        "name": opt.get("name"),
        "capabilities": [{"capability_id": c.get("capability_id"),
                          "role": c.get("role") or "component", "order": n}
                         for n, c in enumerate(caps, start=1)],
        "glue": _glue_markdown(opt, names, filled),
        "gaps": _pick_gaps(opt, wiring, ctx, profile, caps, design.requirements,
                           after["coverage"]),
        "risks": list(after["flags"]),
    }
    slug = compose.unique_slug(store, compose.slug_base(opt.get("name"), design.idea))
    # the studio also picks into an existing directory; anything else must be a known target
    folder = bool(target) and Path(str(target)).expanduser().is_absolute() \
        and Path(str(target)).expanduser().is_dir()
    proposal, _ = compose.validate_proposal(store, raw, rows, profile, target=target, slug=slug,
                                            strict=not folder)
    for rid in opt.get("unlabelled") or []:
        proposal["problems"].append(f"requirement {rid} had no coverage label; treated as "
                                    "net-new")
    proposal.update({
        "engine": "design",
        "design_slug": design.slug,
        "slot": opt.get("slot"),
        "option_slug": opt.get("slug"),
        "stack": opt.get("stack") or "",
        "scores": after.get("scores") or {},
        "wiring": wiring,
        "auto_filled": filled,
        "coverage": after["coverage"],
        "requirements": design.requirements,
    })
    comp = Composition(slug=slug, name=opt.get("name") or slug, pitch=opt.get("pitch") or "",
                       idea=design.idea, target_kind=proposal["target_kind"],
                       target_slug=proposal["target_slug"], proposal=proposal,
                       status="proposed", created=now())
    return store.save_composition(comp)


# -- rendering / CLI ------------------------------------------------------------------------
def _fmt_score(v) -> str:
    return "-" if v is None else f"{v:.2f}"


def _ref_name(x) -> str:
    if isinstance(x, dict):
        return str(x.get("name") or x.get("id") or "?")
    return str(x)


def render_text(design: Design) -> str:
    m = design.meta or {}
    lines = [f"{design.title}  [{design.slug}, {design.engine}]", f"idea: {design.idea}",
             f"pool: {m.get('pool_size', '?')} of {m.get('catalog_size', '?')} capabilities"
             f" ({m.get('shortlist', 'lexical')}), verified: {bool(m.get('verified'))}"
             + (f", cost: ${m['cost_usd']:.4f}" if isinstance(m.get("cost_usd"), (int, float))
                else ""), ""]
    head = ("slot", "name", "parts", "reuse", "fit", "unmet", "flags")
    table = [head]
    for o in design.options:
        wc = (o.get("wiring") or {}).get("counts") or {}
        sc = o.get("scores") or {}
        table.append((o.get("slot", ""), o.get("name", ""), str(len(o.get("capabilities") or [])),
                      _fmt_score(sc.get("reuse")), _fmt_score(sc.get("fit")),
                      str(wc.get("unmet", 0)), str(len(o.get("flags") or []))))
    widths = [max(len(r[i]) for r in table) for i in range(len(head))]
    for r in table:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)).rstrip())
    for o in design.options:
        lines += ["", f"[{o.get('slot')}] {o.get('name')} ({o.get('slug')})"]
        for k in ("pitch", "stack", "rationale"):
            if o.get(k):
                lines.append(f"  {k}: {o[k]}")
        lines.append("  parts:")
        for c in o.get("capabilities") or []:
            lines.append(f"    {c['id']} {c['name']} ({c['donor']}, {c['license'] or 'unknown'})"
                         f"{' - ' + c['role'] if c.get('role') else ''}")
        w = o.get("wiring") or {}
        for x in w.get("wired") or []:
            lines.append(f"  wired: {_ref_name(x['consumer'])} needs {x['token']} <- "
                         f"{_ref_name(x['provider'])}")
        for x in w.get("unmet") or []:
            extra = ""
            if x.get("rejected_provider"):
                extra = f" (rejected: {_ref_name(x['rejected_provider'])})"
            elif x.get("candidates"):
                extra = " (candidates: " + ", ".join(
                    _ref_name(c) for c in x["candidates"][:MAX_CITED]) + ")"
            lines.append(f"  unmet: {_ref_name(x['consumer'])} needs {x['token']}{extra}")
        cov = o.get("counts") or {}
        if cov:
            lines.append("  coverage: " + ", ".join(f"{k} {cov.get(k, 0)}"
                                                     for k in COVERAGE_KINDS))
        for k in ("glue", "new_work"):
            for x in o.get(k) or []:
                lines.append(f"  {k.replace('_', '-')}: {x}")
        for f in o.get("flags") or []:
            lines.append(f"  flag: {f}")
    if m.get("dropped"):
        lines += ["", "dropped:"] + [f"  - {x}" for x in m["dropped"]]
    if m.get("notes"):
        lines += ["", "notes:"] + [f"  - {x}" for x in m["notes"]]
    return "\n".join(lines)


def _list_text(designs) -> str:
    if not designs:
        return "no designs"
    return "\n".join(f"{d.id}\t{d.slug}\t{d.engine}\t{len(d.options)} options\t{d.title}"
                     for d in designs)


def _err(msg: str) -> dict:
    return {"error": msg, "text": f"error: {msg}"}


def _design_result(d: Design) -> dict:
    return {**d.to_dict(), "dropped": (d.meta or {}).get("dropped", []), "text": render_text(d)}


def run_cli(store, args) -> dict:
    words = [str(w) for w in (getattr(args, "idea", None) or [])]
    if words == ["list"]:
        ds = store.list_designs()
        return {"items": [d.to_dict() for d in ds], "text": _list_text(ds)}
    if words and words[0] == "show":
        if len(words) != 2:
            return _err("usage: design show <slug>")
        d = store.get_design(words[1])
        if d is None:
            return _err(f"no such design: {words[1]}")
        return _design_result(d)
    if words and words[0] == "pick":
        if len(words) != 3:
            return _err("usage: design pick <slug> <slot|option-slug> [--target ...]")
        try:
            c = pick(store, words[1], words[2], target=getattr(args, "target", None))
        except ValueError as e:
            return _err(str(e))
        return {"composition": c.to_dict(), "id": c.id, "slug": c.slug,
                "text": compose.render_text(c)}
    idea = " ".join(words).strip()
    if not idea:
        return _err("an idea is required")
    engine = getattr(args, "engine", None) or "claude-p"
    proposal_path = getattr(args, "proposal", None)
    if engine == "session" and not proposal_path:
        return _err("--proposal FILE is required for --engine session")
    shortlist = getattr(args, "shortlist", None)
    if isinstance(shortlist, str):  # CLI names a backend; propose_design wants a client
        if shortlist == "lexical":
            shortlist = None
        else:
            from reincarnate import jev
            try:
                shortlist = jev.get_client("sim" if shortlist == "jev-sim" else "jev")
            except jev.JevError as e:
                return _err(f"--shortlist {getattr(args, 'shortlist')}: {e}")
    try:
        pins = parse_pins(getattr(args, "pin", None))
    except ValueError as e:
        return _err(f"--pin: {e}")
    try:
        d = propose_design(store, idea, n_options=getattr(args, "options", None) or 3,
                           engine=engine, proposal_path=proposal_path,
                           model=getattr(args, "model", None),
                           include_commodity=bool(getattr(args, "include_commodity", False)),
                           verify=not getattr(args, "no_verify", False),
                           shortlist=shortlist, pins=pins)
    except DesignError as e:
        return {**_err(str(e)), "cost_usd": e.cost_usd}
    except ValueError as e:
        return _err(str(e))
    return _design_result(d)

"""Tier 2 enrichment: rate novelty and interface tokens from catalog text only.

No donor source is read. One isolated, tool-less `claude -p` call per batch of capabilities.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

from . import claude_runner, interfaces, jev, licenses, schema
from .prompts import CATALOG_ACCEPTANCE_RULES, UNTRUSTED_DATA_RULE

DEFAULT_MODEL = "claude-haiku-5-5"
ENGINES = ("haiku", "jev", "jev-sim")
NOVELTY_LEVELS = ("commodity", "pattern", "distinctive")
KEEP = "keep"
KEEP_TEXT = "none of these fits; keep the original token"
FIELDS = ("novelty", "interfaces", "acceptance")
DEFAULT_FIELDS = ("novelty", "interfaces")
MAX_REJECTED = 50
JEV_MAX_CONFIDENCE = 0.99
MAX_CHOICES = 255  # Jev's per-question option limit, "keep" included

RUBRIC = """\
novelty (the "beyond boilerplate" bar), exactly one of:
- commodity: a competent coding agent writes it correctly from a one-line prompt \
(retry/backoff, logging, config loading, CRUD endpoints, auth boilerplate).
- pattern: a known shape with real design choices worth specifying (a plugin registry, \
an SSE fan-out, a typed integration framework).
- distinctive: non-obvious behavior an agent would not invent unprompted, the reason to \
reincarnate (conditional compression only when smaller, TOFU peer identity, a ranking heuristic)."""

def parse_json_object(text: str) -> dict[str, Any] | None:
    """The JSON object in a model reply (fenced or bare), or None."""
    return schema.extract_json(text, dict)


def _field_missing(c, f) -> bool:
    if f == "novelty":
        return not c.novelty
    if f == "interfaces":  # rated-and-empty interfaces are not missing
        return not c.provides and not c.requires and not c.interfaces_rated_at
    return not c.acceptance


def _missing(c, fields) -> bool:
    return any(_field_missing(c, f) for f in fields)


def plan_work(caps, fields, *, only_missing=False, force=False) -> tuple[list, dict]:
    """[(capability, fields to write)] plus skip counts. Tier locks (unless force): a reviewed
    capability (confidence 1.0) keeps its novelty / interfaces; a deepened or already checked
    capability keeps its acceptance. Capabilities left with nothing to write are dropped, so
    no model call is spent on them."""
    work, skipped = [], {"skipped_reviewed": 0, "skipped_acceptance": 0}
    for c in caps:
        eff = [f for f in fields if force or not only_missing or _field_missing(c, f)]
        if not force:
            if c.confidence == 1.0 and any(f != "acceptance" for f in eff):
                eff = [f for f in eff if f == "acceptance"]
                skipped["skipped_reviewed"] += 1
            if "acceptance" in eff and (c.deepened_at or c.acceptance):
                eff.remove("acceptance")
                skipped["skipped_acceptance"] += 1
        if eff:
            work.append((c, tuple(eff)))
    return work, skipped


def _novelty_level(v) -> str:
    if not isinstance(v, str):
        return ""
    v = v.strip().lower().rstrip(".,;:!?\"'` ")
    return v if v in NOVELTY_LEVELS else ""


def _side(v, vocab) -> list[str] | None:
    """Canonical tokens for one interface side; [] is a valid explicit empty; None = invalid."""
    if not isinstance(v, list) or not all(isinstance(t, str) for t in v):
        return None
    if not v:
        return []
    return vocab.canonicalize(interfaces.normalize_list(v)) or None


def validate_reply(val, fields, vocab) -> tuple[dict, list[str]]:
    """Check one capability's entry of a model reply. Returns (accepted, rejected_fields):
    accepted maps store field names (novelty, provides, requires, acceptance) to clean values;
    a requested field that is missing or malformed is rejected, so the stored value stays."""
    val = val if isinstance(val, dict) else {}
    accepted: dict = {}
    rejected: list[str] = []

    def take(name, ok, clean):
        if ok:
            accepted[name] = clean
        else:
            rejected.append(name)

    if "novelty" in fields:
        n = _novelty_level(val.get("novelty"))
        take("novelty", bool(n), n)
    if "interfaces" in fields:
        for side in ("provides", "requires"):
            toks = _side(val.get(side), vocab)
            take(side, toks is not None, toks)
    if "acceptance" in fields:
        acc = val.get("acceptance")
        ok = (isinstance(acc, list) and bool(acc)
              and all(isinstance(a, str) and a.strip() for a in acc))
        take("acceptance", ok, [a.strip() for a in acc] if ok else None)
    return accepted, rejected


def inferred_basis(written: dict) -> dict:
    """Basis for the field groups a model reply actually wrote: model judgment, so inferred."""
    groups = {"provides": "interfaces", "requires": "interfaces", "novelty": "novelty",
              "acceptance": "acceptance"}
    return {g: "inferred" for k, g in groups.items() if written.get(k) is not None}


def select(store, *, dossier_ref=None, all_=False, only_missing=False,
           fields=DEFAULT_FIELDS) -> list:
    if dossier_ref:
        d = store.get_dossier(dossier_ref)
        caps = store.list_capabilities(dossier_id=d.id) if d else []
    elif all_:
        caps = store.list_capabilities(current_only=True)
    else:
        caps = []
    if only_missing:
        caps = [c for c in caps if _missing(c, fields)]
    return caps


def build_prompt(store, caps, fields=DEFAULT_FIELDS) -> str:
    rows = "\n".join(
        f"{c.id} | {c.donor} | {c.name} | {c.portability} | {schema.clip_words(c.what)} | "
        f"{(c.portability_notes or '')[:300]}" for c in caps)
    give, sections, shape = [], [], {}
    if "novelty" in fields:
        give.append(RUBRIC)
        shape["novelty"] = "commodity|pattern|distinctive"
    if "interfaces" in fields:
        kinds = "\n".join(f"- {k}: {d}" for k, d in interfaces.KINDS.items())
        max_t = interfaces.MAX_TOKENS_PER_SIDE
        give.append(f"""\
provides / requires: interface tokens of the form kind:thing. Kinds:
{kinds}
Rules: requires = what the capability needs from OTHER parts or from the environment. State \
it keeps for itself goes in provides. ext: tokens are never provided. Use 0-4 tokens per \
side (never more than {max_t}), lowercase, hyphenated.""")
        sections.append(f"""\
Canonical vocabulary. Prefer these tokens exactly; coin a new kind:thing token only when none fits:
{store.get_vocabulary().block()}
""")
        shape["provides"] = ["kind:thing"]
        shape["requires"] = ["kind:thing"]
    if "acceptance" in fields:
        give.append(CATALOG_ACCEPTANCE_RULES)
        shape["acceptance"] = ["input-to-outcome check"]
    return f"""\
You are rating entries in a catalog of reusable software capabilities. Work only from the \
catalog text below; you cannot see any source code. {UNTRUSTED_DATA_RULE}

For each capability give:
{chr(10).join(give)}

{chr(10).join(sections)}
Capabilities (id | donor | name | portability | what | portability notes):
{rows}

Return ONLY a JSON object, no prose, keyed by capability id:
{{"<id>": {json.dumps(shape)}}}"""


# -- Jev engine (Tier 2) --------------------------------------------------------------------
NOVELTY_CRITERIA = [
    "commodity: a competent coding agent writes it correctly from a one-line prompt "
    "(retry/backoff, logging, config loading, CRUD endpoints, auth boilerplate).",
    "pattern: a known shape with real design choices worth specifying (a plugin registry, "
    "an SSE fan-out, a typed integration framework).",
    "distinctive: non-obvious behavior an agent would not invent unprompted, the reason to "
    "reincarnate (conditional compression only when smaller, TOFU peer identity, a ranking "
    "heuristic).",
]


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _choice_criteria(vocab, token: str) -> dict[str, str]:
    """Canonical tokens of the same kind as ``token`` (definition as criterion text) plus keep.
    Over 255 options: the 254 most lexically similar, then keep."""
    kind = token.split(":", 1)[0]
    same = {t: str(d.get("definition", "")) for t, d in sorted(vocab.tokens.items())
            if t.split(":", 1)[0] == kind}
    if len(same) > MAX_CHOICES - 1:
        mine = _words(token.split(":", 1)[1])
        ranked = sorted(same, key=lambda t: (-len(mine & _words(f"{t} {same[t]}")), t))
        same = {t: same[t] for t in ranked[:MAX_CHOICES - 1]}
    return {**same, KEEP: KEEP_TEXT}


_DEFAULT_POLICY = {"deny": [], "warn": ["unknown"]}
_GITHUB_OWNER = re.compile(r"github\.com[/:]([^/\s]+)/", re.IGNORECASE)


def _own_owners(store) -> set[str]:
    """Lower-case GitHub owners of the user's own repos: their registered projects' remotes,
    plus the profile name."""
    owners = set()
    for p in store.list_projects():
        m = _GITHUB_OWNER.search(p.repo_url or "")
        if m:
            owners.add(m.group(1).lower())
    prof = store.get_profile()
    if prof and prof.name.strip():
        owners.add(prof.name.strip().lower())
    return owners


def split_private(store, caps) -> tuple[list, list]:
    """(shareable, private): private = the donor's license is warn or deny under the profile
    policy, is copyleft/proprietary/unknown, or the donor is one of the user's own repos.
    Private capabilities are not sent to a third-party API unless the user opts in."""
    prof = store.get_profile()
    policy = prof.license_policy if prof and isinstance(prof.license_policy, dict) \
        else _DEFAULT_POLICY
    own = _own_owners(store)
    shareable, private = [], []
    for c in caps:
        owner = c.donor.partition("/")[0].lower()
        is_private = (licenses.level(c.license, policy) in ("warn", "deny")
                      or licenses.is_review(c.license) or owner in own)
        (private if is_private else shareable).append(c)
    return shareable, private


def _cap_state(c) -> str:
    return (f"donor: {c.donor}\nname: {c.name}\nwhat: {schema.clip_words(c.what)}\n"
            f"portability notes: {(c.portability_notes or '')[:400]}\n"
            f"provides: {', '.join(c.provides) or '-'}\nrequires: {', '.join(c.requires) or '-'}")


def _conf(a) -> float:
    if a is None:
        return 0.0
    if a.confidence is not None:
        return float(a.confidence)
    return max(a.probabilities.values()) if a.probabilities else 0.0


def _plan(vocab, c, fields=DEFAULT_FIELDS):
    """(questions, token_keys) for one capability; token_keys maps question key -> (side, tok)."""
    qs = []
    if "novelty" in fields:
        qs.append(jev.JevQuestion("novelty", "score",
                                  "How novel is this capability beyond boilerplate? Pick the "
                                  "level that best fits.", criteria=list(NOVELTY_CRITERIA)))
    keys: dict[str, tuple[str, str]] = {}
    for side, toks in ((("p", c.provides), ("r", c.requires)) if "interfaces" in fields else ()):
        for t in toks:
            if t in vocab.tokens:
                continue
            key = f"{side}:{t}"
            keys[key] = (side, t)
            qs.append(jev.JevQuestion(
                key, "choice",
                f"Which canonical interface token means the same as '{t}'? "
                f"Choose '{KEEP}' only if none fits.",
                criteria=_choice_criteria(vocab, t), sim_subject=t.split(":", 1)[1],
                sim_floor={KEEP: 0.05}))
    return qs, keys


def _enrich_jev(store, work, client, review_threshold: float) -> dict:
    """work is [(capability, fields)]. An answer below review_threshold is not applied: the
    stored value stays and the capability is flagged for review."""
    vocab = store.get_vocabulary()
    caps = [c for c, _ in work]
    plans = [_plan(vocab, c, f) for c, f in work]
    results = jev.ask_many(client, [(_cap_state(c), p[0]) for c, p in zip(caps, plans)])
    updated = mapped = flagged = held = 0
    failed: list[dict] = []
    for (c, eff), (qs, keys), res in zip(work, plans, results):
        if isinstance(res, Exception):
            failed.append({"capability_id": c.id, "error": str(res)[:200]})
            continue
        confs: list[float] = []
        held_here = False
        novelty = None
        if "novelty" in eff:
            nov = res.get("novelty")
            confs.append(_conf(nov))
            if nov is not None and confs[-1] >= review_threshold:
                novelty = NOVELTY_LEVELS[nov.level(len(NOVELTY_LEVELS))]
            else:
                held_here = True
        new = {"p": list(c.provides), "r": list(c.requires)}
        for key, (side, tok) in keys.items():
            a = res.get(key)
            confs.append(_conf(a))
            if confs[-1] < review_threshold:
                held_here = True
                continue
            if a is None or a.value in ("", KEEP) or a.value not in vocab.tokens:
                continue
            new[side] = [a.value if t == tok else t for t in new[side]]
            mapped += 1
        # no contributing answer: confidence and the review flag stay as they were
        # 1.0 means "reviewed by Tier 3" (and locks the part), so Tier 2 stays below it
        conf = min(round(min(confs), 4), JEV_MAX_CONFIDENCE) if confs else None
        review = conf < review_threshold if confs else None
        iface = "interfaces" in eff
        if not store.set_capability_fields(
                c.id, provides=new["p"] if iface else None, requires=new["r"] if iface else None,
                novelty=novelty, confidence=conf, needs_review=review,
                basis=inferred_basis({"provides": 1 if iface else None, "novelty": novelty})):
            failed.append({"capability_id": c.id, "error": "capability no longer exists"})
            continue
        updated += 1
        flagged += int(bool(review))
        held += int(held_here)
    return {"updated": updated, "failed_batches": failed, "mapped_tokens": mapped,
            "flagged_for_review": flagged, "held_for_review": held, "batches": len(caps),
            "cost_usd": 0.0}


def parse_fields(spec) -> tuple[tuple[str, ...] | None, str]:
    """(fields, error): a comma list or sequence of FIELDS names; error is "" when valid."""
    if spec is None or spec == "":
        return DEFAULT_FIELDS, ""
    names = ([p.strip().lower() for p in spec.split(",")] if isinstance(spec, str)
             else [str(p).strip().lower() for p in spec])
    names = [n for n in names if n]
    bad = [n for n in names if n not in FIELDS]
    if bad or not names:
        return None, (f"unknown field {', '.join(bad) or '(none given)'}; "
                      f"valid fields: {', '.join(FIELDS)}")
    return tuple(n for n in FIELDS if n in names), ""


def enrich(store, *, dossier_ref=None, all_=False, only_missing=False, batch=25,
           model=DEFAULT_MODEL, engine="haiku", review_threshold=0.6, client=None,
           fields=DEFAULT_FIELDS, force=False, include_private=False) -> dict:
    fields, err = parse_fields(fields)
    if err:
        return {"error": err}
    if engine not in ENGINES:
        return {"error": f"unknown engine {engine!r} (use {', '.join(ENGINES)})"}
    notes: list[str] = []
    if engine != "haiku" and "acceptance" in fields:
        fields = tuple(f for f in fields if f != "acceptance")
        notes.append("acceptance skipped: the jev engine cannot write text")
        if not fields:
            return {"error": notes[0], "notes": notes, "fields": []}
    caps = select(store, dossier_ref=dossier_ref, all_=all_, only_missing=only_missing,
                  fields=fields)
    skipped_private = 0
    if engine != "haiku":
        try:
            client = client or jev.get_client("sim" if engine == "jev-sim" else "jev")
        except jev.JevError as exc:
            return {"error": str(exc)}
        if engine == "jev" and not include_private:
            caps, private = split_private(store, caps)
            skipped_private = len(private)
    work, skipped = plan_work(caps, fields, only_missing=only_missing, force=force)
    skipped = {**skipped, "skipped_private": skipped_private} if engine == "jev" else skipped
    skip_text = "".join(f"; {n} {k.replace('_', ' ')}" for k, n in skipped.items() if n)
    if skipped_private:
        skip_text += " (license, unknown or your own repos; --include-private sends them too)"
    if not work:
        out = {"capabilities": len(caps), "batches": 0, "updated": 0, "failed_batches": [],
               "cost_usd": 0.0, "rejected": [], "rejected_count": 0, "engine_used": engine,
               "flagged_for_review": 0, "mapped_tokens": 0, "jev_stats": {}, **skipped,
               "text": f"nothing to enrich: no model call made{skip_text}"}
    elif engine == "haiku":
        out = _enrich_haiku(store, work, batch=batch, model=model)
        out.update(engine_used="haiku", flagged_for_review=0, mapped_tokens=0, jev_stats={},
                   capabilities=len(caps), **skipped)
        out["text"] += skip_text
    else:
        out = _enrich_jev(store, work, client, float(review_threshold))
        out.update(capabilities=len(caps), engine_used=client.name, jev_stats=dict(client.stats),
                   **skipped)
        s = out["jev_stats"]
        out["text"] = (f"enriched {out['updated']} of {len(caps)} capabilities with "
                       f"{client.name}: {out['mapped_tokens']} tokens mapped, "
                       f"{out['flagged_for_review']} flagged for review (confidence < "
                       f"{review_threshold}), {out['held_for_review']} held back; "
                       f"{s.get('calls', 0)} calls, {s.get('seconds', 0):.2f}s"
                       + (f"; {len(out['failed_batches'])} failed" if out["failed_batches"]
                          else "") + skip_text)
    out["fields"] = list(fields)
    if notes:
        out["notes"] = notes
        out["text"] = out.get("text", "") + "; " + "; ".join(notes)
    return out


def _enrich_haiku(store, work, *, batch=25, model=DEFAULT_MODEL) -> dict:
    """work is [(capability, fields)]; each reply entry is validated before anything is written."""
    batch = max(1, int(batch or 25))
    batches = [work[i:i + batch] for i in range(0, len(work), batch)]
    vocab = store.get_vocabulary()
    updated, cost = 0, 0.0
    failed: list[dict] = []
    rejected: list[dict] = []
    rejected_count = 0
    for i, chunk in enumerate(batches):
        try:
            union = tuple(f for f in FIELDS if any(f in eff for _, eff in chunk))
            prompt = build_prompt(store, [c for c, _ in chunk], union)
            with tempfile.TemporaryDirectory(prefix="reincarnate-enrich-") as tmp:
                res = claude_runner.run(prompt, cwd=Path(tmp), model=model, max_turns=1, tools="",
                                        label=f"enrich batch {i + 1}/{len(batches)}")
            cost += res.cost_usd
            if res.failed:
                failed.append({"batch": i, "error": f"model call failed: {res.error_message()}"})
                continue
            obj = parse_json_object(res.text)
            if obj is None:
                failed.append({"batch": i, "error": "no JSON object in the model's reply: "
                               f"{res.text.strip()[:200]}".strip()})
                continue
            effs = {str(c.id): eff for c, eff in chunk}
            seen = accepted_here = 0
            for key, val in obj.items():
                eff = effs.get(str(key))
                if eff is None or not isinstance(val, dict):
                    continue
                seen += 1
                accepted, bad = validate_reply(val, eff, vocab)
                if bad:
                    rejected_count += 1
                    if len(rejected) < MAX_REJECTED:
                        rejected.append({"capability_id": int(key), "fields": bad,
                                         "reason": "missing or invalid: " + ", ".join(bad)})
                if not accepted:
                    continue
                accepted_here += 1
                rate = not bad and eff != ("acceptance",)  # all validated: Tier 2, unrated
                if not store.set_capability_fields(
                        int(key), **accepted, confidence=-1 if rate else None,
                        needs_review=False if rate else None, basis=inferred_basis(accepted)):
                    failed.append({"batch": i, "error": f"capability {key} no longer exists"})
                    continue
                updated += 1
            if seen and not accepted_here:
                failed.append({"batch": i, "error": "no capability in the reply passed validation"})
        except Exception as exc:  # a failed batch never aborts the run
            failed.append({"batch": i, "error": f"{type(exc).__name__}: {exc}"})
    text = (f"enriched {updated} of {len(work)} capabilities in {len(batches)} batches"
            f" (${cost:.4f})" + (f"; {rejected_count} with rejected fields" if rejected_count
                                 else "")
            + (f"; {len(failed)} batches failed" if failed else ""))
    for f in failed:
        text += f"\n  batch {f['batch']}: {f['error']}"
    return {"capabilities": len(work), "batches": len(batches), "updated": updated,
            "failed_batches": failed, "rejected": rejected, "rejected_count": rejected_count,
            "cost_usd": round(cost, 4), "text": text}


def run_cli(store, args) -> dict:
    ref = getattr(args, "repo", None)
    all_ = bool(getattr(args, "all", False))
    if not ref and not all_:
        return {"error": "give <owner/repo> or --all"}
    if ref and not store.get_dossier(ref):
        return {"error": f"no dossier for {ref}"}
    fields, err = parse_fields(getattr(args, "fields", None))
    if err:
        return {"error": err}
    rt = getattr(args, "review_threshold", None)
    return enrich(store, dossier_ref=ref, all_=all_,
                  only_missing=bool(getattr(args, "only_missing", False)),
                  batch=getattr(args, "batch", None) or 25,
                  model=getattr(args, "model", None) or DEFAULT_MODEL,
                  engine=getattr(args, "engine", None) or "haiku",
                  include_private=bool(getattr(args, "include_private", False)),
                  review_threshold=0.6 if rt is None else float(rt), fields=fields,
                  force=bool(getattr(args, "force", False)))

"""Tier 3 on use: read donor source for the capabilities being built, pin down shapes and values.

One isolated, read-only agentic `claude -p` run per capability, in a sandbox clone of its donor.
Only this module and comprehension read donor source; the builder never does.
"""

from __future__ import annotations

from pathlib import Path

from . import claude_runner, comprehend, prompts, schema
from .store import now

DEFAULT_MODEL = "claude-haiku-5-5"


def _fail(failed: list, cid, reason: str) -> None:
    failed.append({"capability_id": cid, "reason": reason[:300]})


def _deepen_one(store, cap, dossier, clone, *, model, max_turns) -> tuple[str, float]:
    """Run one capability. Returns ("", cost) on success or (reason, cost) on failure."""
    prompt = prompts.build_deepen_prompt(cap, dossier.summary, cap.files)
    res = claude_runner.run(prompt, cwd=Path(clone["path"]), model=model, max_turns=max_turns,
                            tools="Read,Grep,Glob")
    obj = schema.extract_dossier_json(res.text)
    if obj is None:
        return (f"unparseable reply (rc={res.rc}) {(res.stderr or res.text)[:160]}".strip(),
                res.cost_usd)
    what = obj.get("what")
    what = what.strip() if isinstance(what, str) else ""
    store.set_capability_fields(
        cap.id, what=what or None,
        data_shapes=schema.normalize_data_shapes(obj.get("data_shapes")),
        key_values=schema.normalize_key_values(obj.get("key_values")),
        acceptance=schema.normalize_acceptance(obj.get("acceptance")) or None,
        deepened={"at": now(), "sha": clone.get("sha", ""), "model": model})
    return "", res.cost_usd


def deepen(store, capability_ids, *, model=DEFAULT_MODEL, max_turns=25, force=False) -> dict:
    failed: list[dict] = []
    skipped: list[int] = []
    deepened: list[int] = []
    cost = 0.0
    todo: dict[int, list] = {}
    seen: set[int] = set()
    for cid in capability_ids:
        if cid in seen:
            continue
        seen.add(cid)
        cap = store.get_capability(cid)
        if cap is None:
            _fail(failed, cid, "no such capability")
        elif cap.deepened_at and not force:
            skipped.append(cid)
        else:
            todo.setdefault(cap.dossier_id, []).append(cap)
    for dossier_id, caps in todo.items():
        dossier = store.get_dossier(dossier_id)
        try:
            if dossier is None:
                raise RuntimeError("dossier missing")
            clone = comprehend.clone_repo(dossier.url)
        except Exception as exc:  # a failed clone fails only this dossier's capabilities
            for c in caps:
                _fail(failed, c.id, f"clone failed: {type(exc).__name__}: {exc}")
            continue
        for c in caps:
            try:
                reason, spent = _deepen_one(store, c, dossier, clone, model=model,
                                            max_turns=max_turns)
            except Exception as exc:  # one failure never aborts the run
                reason, spent = f"{type(exc).__name__}: {exc}", 0.0
            cost += spent
            if reason:
                _fail(failed, c.id, reason)
            else:
                deepened.append(c.id)
    n = len(seen)
    text = (f"deepened {len(deepened)} of {n} capabilities (${cost:.4f})"
            + (f"; {len(skipped)} already deepened (use --force)" if skipped else "")
            + (f"; {len(failed)} failed" if failed else ""))
    for f in failed:
        text += f"\n  capability {f['capability_id']}: {f['reason']}"
    return {"capabilities": n, "deepened": deepened, "skipped": skipped, "failed": failed,
            "cost_usd": round(cost, 6), "text": text}


def run_cli(store, args) -> dict:
    ids = [int(i) for i in (getattr(args, "ids", None) or [])]
    slug = getattr(args, "composition", None)
    if slug:
        comp = store.get_composition(slug)
        if comp is None:
            msg = f"no composition {slug!r}"
            return {"error": msg, "text": msg}
        for c in (comp.proposal or {}).get("capabilities") or []:
            cid = c.get("capability_id") if isinstance(c, dict) else None
            if isinstance(cid, int) and cid not in ids:
                ids.append(cid)
    if not ids:
        msg = "give capability ids or --composition <slug>"
        return {"error": msg, "text": msg}
    return deepen(store, ids, model=getattr(args, "model", None) or DEFAULT_MODEL,
                  max_turns=int(getattr(args, "max_turns", None) or 25),
                  force=bool(getattr(args, "force", False)))

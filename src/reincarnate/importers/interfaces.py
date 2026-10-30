"""Apply second-brain's capability-interfaces backfill onto current capabilities."""

from __future__ import annotations

import json
from pathlib import Path

from .. import interfaces


def import_interfaces(store, path, *, overwrite=False) -> dict:
    """Apply the backfill. By default only capabilities without interfaces are touched, and
    entries with no tokens never blank anything; `overwrite` replaces existing tokens too. A
    write that changes tokens makes the capability unrated (confidence NULL) again."""
    data = json.loads(Path(path).expanduser().read_text("utf-8"))
    items = data.get("items") or {}
    by_key: dict[tuple[str, str], object] = {}
    for c in store.list_capabilities(current_only=True):
        owner, _, repo = c.donor.partition("/")
        by_key.setdefault((f"{owner}-{repo}".lower(), c.name.strip().lower()), c)
    vocab = store.get_vocabulary()
    matched = changed = unchanged = skipped_existing = skipped_empty = 0
    unmatched: list = []
    for key, val in items.items():
        path_part, _, name = str(key).partition("::")
        stem = path_part.rsplit("/", 1)[-1]
        if stem.endswith(".md"):
            stem = stem[:-3]
        cap = by_key.get((stem.lower(), name.strip().lower()))
        if cap is None or not isinstance(val, dict):
            unmatched.append(key)
            continue
        prov = vocab.canonicalize(interfaces.normalize_list(val.get("provides") or []))
        req = vocab.canonicalize(interfaces.normalize_list(val.get("requires") or []))
        matched += 1
        if not prov and not req:
            skipped_empty += 1
        elif not overwrite and (cap.provides or cap.requires):
            skipped_existing += 1
        elif (prov, req) == (cap.provides, cap.requires):
            unchanged += 1
        elif store.set_capability_fields(
                cap.id, provides=prov, requires=req, confidence=-1,
                basis={"interfaces": "external"}):
            changed += 1
        else:
            matched -= 1
            unmatched.append(key)
    added = aliases_rejected = 0
    cmap = data.get("canonical_map")
    skipped = ""
    if isinstance(cmap, dict):
        with store.conn:
            for alias, canon in cmap.items():
                # both sides are normalized; a target must be a vocabulary token (or None = drop)
                a = interfaces.normalize_token(alias)
                c = interfaces.normalize_token(canon) if canon is not None else None
                if not a or (canon is not None and (not c or c not in vocab.tokens)):
                    aliases_rejected += 1
                    continue
                cur = store.conn.execute(
                    "INSERT OR IGNORE INTO vocab_aliases(alias, canonical) VALUES (?,?)",
                    (a, c))
                added += cur.rowcount
    elif cmap is not None:
        skipped = f"canonical_map is a {type(cmap).__name__}, not alias->canonical; skipped"
    out = {"items": len(items), "matched": matched, "changed": changed,
           "unchanged": unchanged, "skipped_existing": skipped_existing,
           "skipped_empty": skipped_empty, "unmatched_count": len(unmatched),
           "unmatched": unmatched[:20], "aliases_added": added,
           "aliases_rejected": aliases_rejected}
    if skipped:
        out["note"] = skipped
    out["text"] = (f"matched {matched} of {len(items)} items: {changed} changed, "
                   f"{unchanged} unchanged, {skipped_existing} kept (already have interfaces), "
                   f"{skipped_empty} skipped (empty), {len(unmatched)} unmatched; "
                   f"{added} aliases added"
                   + (f", {aliases_rejected} rejected" if aliases_rejected else "")
                   + (f"; first unmatched: {unmatched[:3]}" if unmatched else ""))
    return out


def run_cli(store, args) -> dict:
    p = Path(args.file).expanduser()
    if not p.is_file():
        return {"error": f"not a file: {p}"}
    return import_interfaces(store, p, overwrite=bool(getattr(args, "overwrite", False)))

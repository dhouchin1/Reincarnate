"""Apply second-brain's capability-interfaces backfill onto current capabilities."""

from __future__ import annotations

import json
from pathlib import Path


def import_interfaces(store, path) -> dict:
    data = json.loads(Path(path).expanduser().read_text("utf-8"))
    items = data.get("items") or {}
    by_key: dict[tuple[str, str], int] = {}
    for c in store.list_capabilities(current_only=True):
        owner, _, repo = c.donor.partition("/")
        by_key.setdefault((f"{owner}-{repo}".lower(), c.name.strip().lower()), c.id)
    matched, unmatched = 0, []
    for key, val in items.items():
        path_part, _, name = str(key).partition("::")
        stem = path_part.rsplit("/", 1)[-1]
        if stem.endswith(".md"):
            stem = stem[:-3]
        cid = by_key.get((stem.lower(), name.strip().lower()))
        if cid is None or not isinstance(val, dict):
            unmatched.append(key)
            continue
        store.set_capability_fields(cid, provides=val.get("provides") or [],
                                    requires=val.get("requires") or [])
        matched += 1
    added = 0
    cmap = data.get("canonical_map")
    skipped = ""
    if isinstance(cmap, dict):
        with store.conn:
            for alias, canon in cmap.items():
                if not isinstance(alias, str) or not (canon is None or isinstance(canon, str)):
                    continue
                cur = store.conn.execute(
                    "INSERT OR IGNORE INTO vocab_aliases(alias, canonical) VALUES (?,?)",
                    (alias, canon))
                added += cur.rowcount
    elif cmap is not None:
        skipped = f"canonical_map is a {type(cmap).__name__}, not alias->canonical; skipped"
    out = {"items": len(items), "matched": matched, "unmatched": unmatched[:20],
           "aliases_added": added}
    if skipped:
        out["note"] = skipped
    out["text"] = (f"matched {matched} of {len(items)} items; {added} aliases added"
                   + (f"; first unmatched: {unmatched[:3]}" if unmatched else ""))
    return out


def run_cli(store, args) -> dict:
    p = Path(args.file).expanduser()
    if not p.is_file():
        return {"error": f"not a file: {p}"}
    return import_interfaces(store, p)

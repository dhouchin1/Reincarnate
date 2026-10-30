"""Import second-brain repo-dossier notes into the Reincarnate store.

A dossier note is a markdown file whose YAML frontmatter names the repo and whose body
contains a ```json fenced block with a ``capabilities`` key (the dossier dict). Notes without
such a block (link captures, index pages) are skipped, never treated as errors.

Standard library only. The frontmatter parser handles the flat subset the vault actually
uses: ``key: value``, quoted strings, and ``[a, b]`` inline lists. Nested blocks such as
``origin:`` are ignored.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

try:  # schema is optional; fall back to the raw dossier dict if it is unavailable
    from reincarnate import schema as _schema
except ImportError:  # pragma: no cover - exercised only in a broken install
    _schema = None

from reincarnate.schema import json_fences  # noqa: E402

_FRONTMATTER_KEY = re.compile(r"^([A-Za-z_][\w-]*):(?:\s+(.*))?$")
_GITHUB_URL = re.compile(r"github\.com/([^/\s\"'<>]+)/([^/\s\"'<>#?]+)")


def _scalar(raw: str) -> Any:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1].replace('\\"', '"') if raw[0] == '"' else raw[1:-1]
    if " #" in raw:  # trailing YAML comment on an unquoted value
        raw = raw.split(" #", 1)[0].rstrip()
    if raw in ("true", "false"):
        return raw == "true"
    if raw in ("null", "~"):
        return None
    return raw


def _split_items(inner: str) -> list[str]:
    """Split the body of an inline list on commas that sit outside quotes."""
    items: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in inner:
        if quote:
            if ch == quote:
                quote = None
            buf.append(ch)
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            items.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    items.append("".join(buf))
    return [i.strip() for i in items if i.strip()]


def _value(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return [_scalar(item) for item in _split_items(raw[1:-1])]
    return _scalar(raw)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return ``(frontmatter_dict, body)``. Text without frontmatter yields ``({}, text)``."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
    if end is None:
        return {}, text
    data: dict = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":  # one-level nested block (e.g. origin:) is ignored
            continue
        match = _FRONTMATTER_KEY.match(line.rstrip())
        if not match:
            continue
        key, raw = match.group(1), match.group(2) or ""
        data[key] = _value(raw) if raw.strip() else ""
    return data, "\n".join(lines[end + 1:])


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _owner_repo(fm: dict, url: str, path: Path) -> tuple[str, str]:
    """Frontmatter first, then the github.com URL, then the filename stem as a last resort."""
    owner, repo = _text(fm.get("owner")), _text(fm.get("repo"))
    if not (owner and repo):
        match = _GITHUB_URL.search(url)
        if match:
            owner = owner or match.group(1)
            repo = repo or match.group(2)
    if not (owner and repo):
        stem_owner, _, stem_repo = path.stem.partition("-")
        owner = owner or stem_owner
        repo = repo or stem_repo
    repo = repo[:-4] if repo.endswith(".git") else repo
    return owner, repo


def parse_dossier_note(path: Path | str) -> dict | None:
    """Parse one dossier note.

    Returns ``None`` when the note has no ``capabilities`` json block (not a dossier).
    Raises ``ValueError`` when the block is present but is not a valid dossier object.
    """
    path = Path(path)
    fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    blocks = [b for b in json_fences(body) if '"capabilities"' in b]
    if not blocks:
        return None
    try:
        data = json.loads(blocks[0], strict=False)
    except ValueError as exc:
        raise ValueError(f"invalid dossier JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("capabilities"), list):
        raise ValueError("dossier JSON is not an object with a capabilities list")

    url = _text(fm.get("url"))
    owner, repo = _owner_repo(fm, url, path)
    if not (owner and repo):
        raise ValueError("cannot determine owner/repo")
    if not url:
        url = f"https://github.com/{owner}/{repo}"

    try:
        cost = float(fm.get("comprehend_cost_usd") or fm.get("cost_usd") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0

    return {
        "owner": owner,
        "repo": repo,
        "url": url,
        "sha": _text(fm.get("sha") or fm.get("commit")) or "unknown",
        "comprehended_at": _text(fm.get("comprehended_at")),
        "model": _text(fm.get("comprehend_model") or fm.get("model")),
        "cost_usd": cost,
        "data": _schema.normalize_dossier(data) if _schema else data,
    }


def _mark_external(note: dict) -> None:
    """Another tool wrote this dossier: every capability field is external and what was read is
    unknown, whatever coverage or basis the note itself carried."""
    data = note["data"]
    fields = _schema.BASIS_FIELDS if _schema else (
        "what", "interfaces", "novelty", "acceptance", "data_shapes", "key_values")
    for c in data.get("capabilities") or []:
        if isinstance(c, dict):
            c["basis"] = {k: "external" for k in fields}
    why = "imported from second-brain; what was read is not recorded"
    scope = f"{note['owner']}/{note['repo']}@{note['sha']}"
    data["coverage"] = {"status": "unknown", "scope": scope, "omitted": [],
                        "facets": {"files": {"status": "unknown", "reason": why}}}


def import_dir(store: Any, directory: Path | str, *, source: str = "vault-import",
               force: bool = False) -> dict:
    """Import every dossier note in ``directory``. Never raises for a bad note.

    A note whose (owner, repo, sha) is already stored is counted as skipped unless ``force``,
    which reconciles it in place (capability ids are kept)."""
    directory = Path(directory)
    result: dict[str, Any] = {"imported": 0, "skipped": 0, "errors": [], "donors": []}
    totals = {"new": 0, "updated": 0, "unchanged": 0, "retired": 0}
    if not directory.is_dir():
        result["errors"].append({"file": str(directory), "reason": "not a directory"})
        return result

    for path in sorted(directory.glob("*.md")):
        try:
            note = parse_dossier_note(path)
        except Exception as exc:  # one bad note must not stop the import
            result["errors"].append({"file": path.name, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        if note is None:
            result["skipped"] += 1
            continue
        if not force and store.find_dossier(note["owner"], note["repo"], note["sha"]):
            result["skipped"] += 1
            result["already_imported"] = result.get("already_imported", 0) + 1
            continue
        try:
            _mark_external(note)
            store.upsert_dossier(
                note["owner"], note["repo"], note["url"], note["sha"], note["data"],
                model=note["model"], cost_usd=note["cost_usd"], source=source,
            )
        except Exception as exc:
            result["errors"].append({"file": path.name, "reason": f"store: {exc}"})
            continue
        result["imported"] += 1
        for k, n in (getattr(store, "last_reconcile", None) or {}).items():
            totals[k] = totals.get(k, 0) + n
        donor = f"{note['owner']}/{note['repo']}"
        if donor not in result["donors"]:
            result["donors"].append(donor)
    if result["imported"]:
        result["reconciled"] = totals
    return result


def run_cli(store: Any, args: Any) -> dict:
    """Entry point for ``reincarnate import-vault <dir>``; ``args.dir`` is the directory."""
    return import_dir(store, args.dir, force=bool(getattr(args, "force", False)))

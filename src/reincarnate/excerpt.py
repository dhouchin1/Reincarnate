"""Rank a cloned repo's files and pack the most informative ones into a character budget.

Ported from Forge Studio's ingest. Files are only read, never executed. Agent-instruction
files (`CLAUDE.md`, `AGENTS.md`, anything under `.claude/`) are listed in the tree but never
excerpted: in a donor repo they are data at best and prompt injection at worst.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from . import secret_scan

SKIP_DIRS = {
    ".git", "node_modules", "vendor", "dist", "build", "target", "__pycache__", ".venv", "venv",
    ".next", ".nuxt", "coverage", ".idea", ".vscode", "out", "bin", "obj", ".gradle", "Pods",
    "third_party", "external", ".cache", "public", "static", "assets", "fixtures", "testdata",
}
SOURCE_EXT = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".go", ".rs", ".java", ".kt", ".swift", ".rb",
    ".php", ".cs", ".c", ".h", ".cc", ".cpp", ".hpp", ".scala", ".ex", ".exs", ".lua", ".dart",
    ".vue", ".svelte", ".sql", ".sh", ".zig",
}
MANIFESTS = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "go.mod",
    "cargo.toml", "composer.json", "gemfile", "pom.xml", "build.gradle", "build.gradle.kts",
    "mix.exs", "pubspec.yaml", "dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "makefile",
}
LOCKFILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "cargo.lock", "go.sum",
    "composer.lock", "gemfile.lock", "uv.lock",
}
ENTRY_STEMS = {"main", "app", "index", "server", "cli", "lib", "mod", "__init__", "routes", "api"}
AGENT_FILES = {"claude.md", "agents.md"}
AGENT_DIRS = {".claude"}
MAX_FILE_BYTES = 200_000
MAX_TREE = 500
RENDER_TREE = 400
RENDER_SKIPPED = 100
MAX_OMITTED = 200
MAX_WALK_ENTRIES = 50_000  # directories and files visited before the walk stops
SYMLINK_DIR = "symlinked directory, not followed"


@dataclass
class Excerpt:
    tree: list[str] = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)  # (path, text)
    total_files: int = 0
    chars: int = 0
    # what the walk did not read (see coverage()); skipped directories are never walked, so
    # their contents are not in total_files
    skipped_dirs: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)
    omitted: list[tuple[str, str]] = field(default_factory=list)  # files: (path, reason)
    truncated: list[str] = field(default_factory=list)  # read, but cut to fit
    candidates_total: int = 0  # scored candidates, read or not
    walk_capped: bool = False  # the walk hit MAX_WALK_ENTRIES and stopped
    redactions: dict[str, int] = field(default_factory=dict)  # rule id -> values redacted
    redacted_files: int = 0


def _score(rel: str) -> float:
    p = Path(rel)
    name = p.name.lower()
    depth = len(p.parts) - 1
    parts = {x.lower() for x in p.parts[:-1]}
    if name.startswith("readme"):
        return 1000 - depth * 50
    if name in MANIFESTS:
        return 800 - depth * 60
    if name.startswith(("license", "licence", "copying")):
        return 700 - depth * 100
    if p.suffix.lower() in {".md", ".rst"} and parts & {"docs", "doc"}:
        return 200 - depth * 10
    if p.suffix.lower() not in SOURCE_EXT:
        return -1
    s = 300.0
    if p.stem.lower() in ENTRY_STEMS:
        s += 150
    if (parts & {"test", "tests", "spec", "__tests__", "e2e", "examples", "example",
                 "benchmarks", "bench"}
            or ".test." in name or ".spec." in name or name.startswith("test_")):
        s -= 260
    if parts & {"src", "lib", "app", "pkg", "internal", "cmd", "core", "server", "api"}:
        s += 40
    return s - depth * 25


def _add_tree(ex: Excerpt, rel: str) -> None:
    ex.total_files += 1
    if len(ex.tree) < MAX_TREE:
        ex.tree.append(rel)


def _rel(root: Path, full: Path) -> str:
    return str(full.relative_to(root))


def _inside(real_root: str, path) -> bool:
    real = os.path.realpath(path)
    return real == real_root or real.startswith(real_root + os.sep)


def select_files(root: Path, char_budget: int) -> Excerpt:
    root = Path(root)
    real_root = os.path.realpath(root)
    ex = Excerpt()
    candidates: list[tuple[float, str, int]] = []
    visited = 0
    for dirpath, dirnames, filenames in os.walk(root):
        visited += len(dirnames) + len(filenames)
        if visited > MAX_WALK_ENTRIES or not _inside(real_root, dirpath):
            ex.walk_capped = ex.walk_capped or visited > MAX_WALK_ENTRIES
            dirnames[:] = []
            break
        keep = []
        for d in sorted(dirnames):
            if os.path.islink(Path(dirpath) / d):
                # never walked: its target may be anywhere on the machine
                ex.skipped_dirs.append((_rel(root, Path(dirpath) / d), SYMLINK_DIR))
            elif d in AGENT_DIRS:
                for sub, sub_dirs, sub_files in os.walk(Path(dirpath) / d):
                    visited += len(sub_dirs) + len(sub_files)
                    if visited > MAX_WALK_ENTRIES or not _inside(real_root, sub):
                        ex.walk_capped = ex.walk_capped or visited > MAX_WALK_ENTRIES
                        break
                    sub_dirs[:] = [x for x in sub_dirs if not os.path.islink(Path(sub) / x)]
                    for fn in sorted(sub_files):
                        full = Path(sub) / fn
                        if not full.is_symlink():
                            rel = str(full.relative_to(root))
                            _add_tree(ex, rel)
                            ex.omitted.append((rel, "agent instructions, never read"))
            elif d.startswith("."):
                ex.skipped_dirs.append((_rel(root, Path(dirpath) / d), "hidden directory"))
            elif d in SKIP_DIRS:
                ex.skipped_dirs.append((_rel(root, Path(dirpath) / d),
                                        f"vendored or build output ({d})"))
            else:
                keep.append(d)
        dirnames[:] = keep
        for fn in sorted(filenames):
            full = Path(dirpath) / fn
            rel = str(full.relative_to(root))
            if full.is_symlink():
                ex.omitted.append((rel, "symlink"))
                continue
            _add_tree(ex, rel)
            low = fn.lower()
            if low in AGENT_FILES:
                ex.omitted.append((rel, "agent instructions, never read"))
                continue
            if secret_scan.is_secrets_file(fn):
                ex.omitted.append((rel, "likely secrets file"))
                continue
            if low in LOCKFILES:
                ex.omitted.append((rel, "lockfile"))
                continue
            if low.endswith((".min.js", ".min.css", ".map")):
                ex.omitted.append((rel, "minified or source map"))
                continue
            try:
                size = full.stat().st_size
            except OSError:
                ex.omitted.append((rel, "unreadable"))
                continue
            if size == 0:
                ex.omitted.append((rel, "empty"))
                continue
            if size > MAX_FILE_BYTES:
                ex.omitted.append((rel, f"larger than {MAX_FILE_BYTES // 1000} KB"))
                continue
            s = _score(rel)
            if s > 0:
                candidates.append((s, rel, size))
            else:
                ex.omitted.append((rel, "not source or docs"))
    candidates.sort(key=lambda c: (-c[0], c[2]))
    ex.candidates_total = len(candidates)
    used = 0
    for i, (_s, rel, _size) in enumerate(candidates):
        if used >= char_budget:
            ex.omitted += [(r, "character budget used") for _s2, r, _z in candidates[i:]]
            break
        try:
            raw = (root / rel).read_bytes()
        except OSError:
            ex.omitted.append((rel, "unreadable"))
            continue
        if b"\x00" in raw[:4096]:
            ex.omitted.append((rel, "binary"))
            continue
        text, found = secret_scan.redact(raw.decode("utf-8", errors="replace"))
        if found:
            ex.redacted_files += 1
            for rule, n in found.items():
                ex.redactions[rule] = ex.redactions.get(rule, 0) + n
        name = Path(rel).name.lower()
        if name.startswith("readme"):
            cap = 24_000
        elif name.startswith(("license", "licence")):
            cap = 2_000
        else:
            cap = 14_000
        cap = min(cap, char_budget - used)
        if cap < 800:
            ex.omitted += [(r, "character budget used") for _s2, r, _z in candidates[i:]]
            break
        if len(text) > cap:
            mark = "\n... [truncated]"
            text = text[:cap - len(mark)] + mark
            ex.truncated.append(rel)
        ex.files.append((rel, text))
        used += len(text)
    ex.chars = used
    return ex


def render_excerpt(ex: Excerpt, marker: str | None = None) -> str:
    """FILE TREE block followed by one `===== FILE: path =====` block per excerpted file. Each
    file ends with `===== END <marker> =====`; the marker is random per call, so file text cannot
    forge the end of its own block."""
    marker = marker or secrets.token_hex(8)
    skipped = [f"{p}/ [skipped: {why}]" for p, why in ex.skipped_dirs[:RENDER_SKIPPED]]
    if len(ex.skipped_dirs) > RENDER_SKIPPED:
        skipped.append(f"... and {len(ex.skipped_dirs) - RENDER_SKIPPED} more skipped directories")
    tree = "\n".join(skipped + ex.tree[:RENDER_TREE])
    files = "\n\n".join(f"===== FILE: {p} =====\n{t}\n===== END {marker} ====="
                        for p, t in ex.files)
    head = f"FILE TREE ({ex.total_files} files, first {min(len(ex.tree), RENDER_TREE)} listed):\n"
    return head + tree + "\n\n" + files + "\n"


_LICENSE_NEEDLES = (
    ("MIT", "mit license"), ("Apache-2.0", "apache license"),
    ("AGPL-3.0", "affero general public"), ("GPL-3.0", "gnu general public license"),
    ("BSD-3-Clause", "redistribution and use"), ("MPL-2.0", "mozilla public"),
    ("ISC", "isc license"), ("Unlicense", "unlicense"),
)


def license_hint(ex: Excerpt) -> str:
    for p, t in ex.files:
        if Path(p).name.lower().startswith(("license", "licence", "copying")):
            head = t[:600].lower()
            for spdx, needle in _LICENSE_NEEDLES:
                if needle in head:
                    return spdx
    return ""


def coverage(ex: Excerpt, scope: str) -> dict:
    """The Code Trace-shaped coverage object for one excerpt: what was listed, read and left out."""
    facets: dict = {}
    tree = {"status": "complete",
            "reason": "skipped directories are not walked, so their contents are not counted"}
    if ex.total_files > len(ex.tree):
        tree["status"] = "partial"
        tree["truncated"] = {"returned": len(ex.tree), "total": ex.total_files}
        tree["reason"] = ("tree listing capped; " + tree["reason"])
    if ex.walk_capped:
        tree["status"] = "partial"
        tree["reason"] = (f"walk stopped after {MAX_WALK_ENTRIES} entries; " + tree["reason"])
    facets["tree"] = tree

    read = len(ex.files)
    if read == ex.candidates_total and not ex.truncated:
        facets["files"] = {"status": "complete",
                           "reason": f"all {read} scored source and doc files were read in full"}
    else:
        bits = []
        if read < ex.candidates_total:
            bits.append(f"{ex.candidates_total - read} scored files not read (budget, binary or "
                        "unreadable)")
        if ex.truncated:
            bits.append(f"{len(ex.truncated)} read files truncated")
        facets["files"] = {"status": "partial",
                           "reason": "; ".join(bits),
                           "truncated": {"returned": read, "total": ex.candidates_total}}
    n = len(ex.skipped_dirs)
    facets["skipped_dirs"] = {"status": "complete", "reason": f"{n} directories skipped"}
    facets["secrets"] = {"status": "complete",
                         "reason": f"{sum(ex.redactions.values())} values redacted in "
                                   f"{ex.redacted_files} files"}
    if ex.redactions:
        facets["secrets"]["rules"] = dict(sorted(ex.redactions.items()))

    omitted = ([{"path": p, "reason": r} for p, r in ex.skipped_dirs]
               + [{"path": p, "reason": r} for p, r in ex.omitted]
               + [{"path": p, "reason": "truncated to fit"} for p in ex.truncated])
    if len(omitted) > MAX_OMITTED:
        facets["omitted"] = {"status": "partial", "reason": "omitted list capped",
                             "truncated": {"returned": MAX_OMITTED, "total": len(omitted)}}
        omitted = omitted[:MAX_OMITTED]
    status = "partial" if any(f["status"] == "partial" for f in facets.values()) else "complete"
    return {"status": status, "scope": scope, "facets": facets, "omitted": omitted}


def read_paths(ex: Excerpt) -> set[str]:
    """Normalized paths of the files whose text the excerpt holds."""
    return {norm_path(p) for p, _t in ex.files}


def norm_path(p) -> str:
    s = str(p or "").strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")

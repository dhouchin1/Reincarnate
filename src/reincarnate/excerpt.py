"""Rank a cloned repo's files and pack the most informative ones into a character budget.

Ported from Forge Studio's ingest. Files are only read, never executed. Agent-instruction
files (`CLAUDE.md`, `AGENTS.md`, anything under `.claude/`) are listed in the tree but never
excerpted: in a donor repo they are data at best and prompt injection at worst.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass
class Excerpt:
    tree: list[str] = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)  # (path, text)
    total_files: int = 0
    chars: int = 0


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


def select_files(root: Path, char_budget: int) -> Excerpt:
    root = Path(root)
    ex = Excerpt()
    candidates: list[tuple[float, str, int]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for d in sorted(dirnames):
            if d in AGENT_DIRS:
                for sub, _dn, sub_files in os.walk(Path(dirpath) / d):
                    for fn in sorted(sub_files):
                        full = Path(sub) / fn
                        if not full.is_symlink():
                            _add_tree(ex, str(full.relative_to(root)))
            elif d not in SKIP_DIRS and not d.startswith("."):
                keep.append(d)
        dirnames[:] = keep
        for fn in sorted(filenames):
            full = Path(dirpath) / fn
            rel = str(full.relative_to(root))
            if full.is_symlink():
                continue
            _add_tree(ex, rel)
            low = fn.lower()
            if low in AGENT_FILES:
                continue
            if low in LOCKFILES or low.endswith((".min.js", ".min.css", ".map")):
                continue
            try:
                size = full.stat().st_size
            except OSError:
                continue
            if size == 0 or size > MAX_FILE_BYTES:
                continue
            s = _score(rel)
            if s > 0:
                candidates.append((s, rel, size))
    candidates.sort(key=lambda c: (-c[0], c[2]))
    used = 0
    for _s, rel, _size in candidates:
        if used >= char_budget:
            break
        try:
            raw = (root / rel).read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        text = raw.decode("utf-8", errors="replace")
        name = Path(rel).name.lower()
        if name.startswith("readme"):
            cap = 24_000
        elif name.startswith(("license", "licence")):
            cap = 2_000
        else:
            cap = 14_000
        cap = min(cap, char_budget - used)
        if cap < 800:
            break
        if len(text) > cap:
            mark = "\n... [truncated]"
            text = text[:cap - len(mark)] + mark
        ex.files.append((rel, text))
        used += len(text)
    ex.chars = used
    return ex


def render_excerpt(ex: Excerpt) -> str:
    """FILE TREE block followed by one `===== FILE: path =====` block per excerpted file."""
    tree = "\n".join(ex.tree[:RENDER_TREE])
    files = "\n\n".join(f"===== FILE: {p} =====\n{t}" for p, t in ex.files)
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


def clean_license(raw: str) -> str:
    """'MIT OR Apache-2.0 (dual-licensed)' -> 'MIT OR Apache-2.0'; unknown -> 'unknown'."""
    s = re.sub(r"\s*\(.*?\)\s*", " ", str(raw or "")).strip().strip(".")
    if not s or s.lower().startswith(("unknown", "none", "no license", "noassertion", "n/a")):
        return "unknown"
    return s if len(s) <= 40 else s[:39].rstrip() + "..."

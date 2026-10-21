"""Sandbox clone + headless comprehension run + ingest."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

MAX_CLONE_KB = 500 * 1024
_SEG = r"[A-Za-z0-9_.-]+"
_PATTERNS = (
    re.compile(rf"^https://github\.com/({_SEG})/({_SEG}?)(?:\.git)?/?$"),
    re.compile(rf"^git@github\.com:({_SEG})/({_SEG}?)(?:\.git)?/?$"),
    re.compile(rf"^({_SEG})/({_SEG}?)(?:\.git)?/?$"),
)


def parse_github_url(url: str) -> tuple[str, str]:
    """Accept https/ssh/bare `owner/repo` GitHub references; anything else is ValueError."""
    s = (url or "").strip()
    for pat in _PATTERNS:
        m = pat.match(s)
        if m:
            owner, repo = m.group(1), m.group(2)
            if owner in (".", "..") or repo in (".", "..") or not repo:
                break
            return owner, repo
    raise ValueError(f"not a github repo reference: {url!r}")


def _du_kb(path: Path) -> int:
    out = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True, check=True)
    return int(out.stdout.split()[0])


def _git(*args: str, cwd: Path | None = None, timeout: int = 60) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        timeout=timeout, check=True,
    )
    return proc.stdout.strip()


def _local_source(url: str) -> Path | None:
    s = url[len("file://"):] if url.startswith("file://") else url
    p = Path(s).expanduser()
    return p if (url.startswith("file://") or p.is_absolute() or p.exists()) else None


def clone_repo(url: str, *, dest: Path | None = None, timeout: int = 180,
               allow_local: bool = False) -> dict:
    """Shallow, hook-less clone into the sandbox. Returns path/owner/repo/sha/reused."""
    from reincarnate import paths

    src = _local_source(url) if allow_local else None
    if src is not None:
        parts = src.resolve().parts
        owner, repo = (parts[-2], parts[-1]) if len(parts) >= 3 else ("local", src.name)
        clone_url = url if url.startswith("file://") else str(src.resolve())
    else:
        owner, repo = parse_github_url(url)
        clone_url = f"https://github.com/{owner}/{repo}.git"
    dest = Path(dest) if dest else paths.sandbox_dir() / f"{owner}-{repo}"

    if (dest / ".git").exists():
        return {"path": str(dest), "owner": owner, "repo": repo,
                "sha": _git("rev-parse", "HEAD", cwd=dest), "reused": True}

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--no-tags", "-c", "core.hooksPath=/dev/null",
             clone_url, str(dest)],
            capture_output=True, timeout=timeout, check=True,
        )
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(dest, ignore_errors=True)
        detail = (exc.stderr or b"").decode(errors="replace").strip()[:300]
        raise RuntimeError(f"git clone failed: {detail}") from exc
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"git clone timed out after {timeout}s") from exc

    size_kb = _du_kb(dest)
    if size_kb > MAX_CLONE_KB:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"clone is {size_kb // 1024} MB, over the 500 MB cap; removed")
    # Pin the hooks path in the checkout too, so later git commands stay hook-less.
    _git("config", "core.hooksPath", "/dev/null", cwd=dest)
    return {"path": str(dest), "owner": owner, "repo": repo,
            "sha": _git("rev-parse", "HEAD", cwd=dest), "reused": False}


def comprehend_url(store, url: str, *, model: str = "sonnet", max_turns: int = 40,
                   force: bool = False) -> dict:
    """Clone, run headless comprehension, ingest. Does not run the overlay."""
    from reincarnate import claude_runner, paths, prompts, render, schema

    info = clone_repo(url)
    owner, repo, sha = info["owner"], info["repo"], info["sha"]
    donor = f"{owner}/{repo}"
    existing = store.get_dossier(f"{donor}@{sha}")
    if existing is not None and not force:
        return {"skipped": True, "dossier_id": existing.id, "donor": donor, "sha": sha,
                "message": "dossier already exists for this sha; use --force"}

    prompt = prompts.build_comprehension_prompt(
        owner, repo, store.get_profile(), store.list_projects())
    res = claude_runner.run(prompt, cwd=Path(info["path"]), model=model, max_turns=max_turns)

    out_dir = paths.ensure(paths.dossiers_dir())
    data = schema.extract_dossier_json(res.text)
    if data is None:
        raw_path = out_dir / f"{owner}-{repo}.raw.txt"
        raw_path.write_text(res.text or res.stderr, encoding="utf-8")
        return {"dossier_id": None, "donor": donor, "sha": sha, "capabilities": 0,
                "cost_usd": res.cost_usd, "note_path": str(raw_path), "parsed": False,
                "message": f"no dossier JSON in output (rc={res.rc}); raw saved to {raw_path}"}

    data = schema.normalize_dossier(data)
    problems = schema.validate_dossier(data)
    canonical = url if url.startswith("https://") else f"https://github.com/{owner}/{repo}"
    dossier = store.upsert_dossier(owner, repo, canonical, sha, data, model=model,
                                   cost_usd=res.cost_usd, source="claude-p")
    note = out_dir / f"{owner}-{repo}.md"
    note.write_text(render.render_dossier_markdown(dossier), encoding="utf-8")
    return {"dossier_id": dossier.id, "donor": donor, "sha": sha,
            "capabilities": len(data.get("capabilities", [])), "cost_usd": res.cost_usd,
            "note_path": str(note), "parsed": True,
            "message": "; ".join(problems) if problems else ""}

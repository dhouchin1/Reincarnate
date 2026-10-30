"""Sandbox clone + headless comprehension run + ingest."""

from __future__ import annotations

import json
import os
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


def _clone_error(stderr, clone_url: str) -> str:
    """git's stderr as one useful line: drop the "Cloning into '<sandbox path>'..." preamble and
    say plainly when GitHub has no such public repository."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    text = str(stderr or "")
    lines = [ln.strip() for ln in text.splitlines()
             if ln.strip() and not ln.strip().startswith("Cloning into")]
    joined = " ".join(lines)
    if re.search(r"repository not found|could not read username|terminal prompts disabled",
                 joined, re.I):
        return f"no public repository at {clone_url} (it does not exist, or it is private)"
    return (joined or "no detail from git")[:300]


# git-lfs filters would fetch pointer targets from whatever host the repo names; switch them off
_NO_LFS = ("-c", "filter.lfs.process=", "-c", "filter.lfs.smudge=", "-c",
           "filter.lfs.required=false", "-c", "core.hooksPath=/dev/null")
_BLOB_FILTER = "--filter=blob:limit=2m"  # the excerpt never reads a file over 200 KB


def _net_git(*args: str, cwd: Path | None = None, timeout: int = 180) -> None:
    """A network git command with hooks and LFS smudging off."""
    env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}
    subprocess.run(["git", *_NO_LFS, *args], cwd=str(cwd) if cwd else None, env=env,
                   capture_output=True, timeout=timeout, check=True)


def _same_remote(dest: Path, clone_url: str) -> bool:
    """Does the existing clone's origin point at the repository we expect (case-insensitive)?"""
    try:
        got = _git("-C", str(dest), "remote", "get-url", "origin")
    except (subprocess.SubprocessError, OSError):
        return False
    return got.rstrip("/").lower().removesuffix(".git") == clone_url.rstrip("/").lower() \
        .removesuffix(".git")


def _check_size(dest: Path) -> None:
    size_kb = _du_kb(dest)
    if size_kb > MAX_CLONE_KB:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"clone is {size_kb // 1024} MB, over the 500 MB cap; removed")


def clone_repo(url: str, *, dest: Path | None = None, timeout: int = 180,
               allow_local: bool = False) -> dict:
    """Shallow, hook-less clone into the sandbox (`sandbox/<owner>/<repo>`). Returns
    path/owner/repo/sha/reused. A reused clone is fetched first; when the fetch fails the old
    checkout is kept and the result carries `stale: True` and a `note`."""
    from reincarnate import paths

    src = _local_source(url) if allow_local else None
    if src is not None:
        parts = src.resolve().parts
        owner, repo = (parts[-2], parts[-1]) if len(parts) >= 3 else ("local", src.name)
        clone_url = url if url.startswith("file://") else str(src.resolve())
    else:
        owner, repo = parse_github_url(url)
        clone_url = f"https://github.com/{owner}/{repo}.git"
    if dest is None:
        # GitHub names are case-insensitive, so one folder per repo on any filesystem
        safe = [re.sub(r"[^a-z0-9_.-]", "_", x.lower()) or "_" for x in (owner, repo)]
        dest = paths.sandbox_dir() / safe[0] / safe[1]
    dest = Path(dest)

    if (dest / ".git").exists():
        if _same_remote(dest, clone_url):
            return _refresh(dest, owner, repo, clone_url, timeout)
        shutil.rmtree(dest, ignore_errors=True)  # someone else's checkout; never trust it

    dest.parent.mkdir(parents=True, exist_ok=True)
    for filt in (True, False):
        try:
            _net_git("clone", "--depth", "1", "--no-tags", *((_BLOB_FILTER,) if filt else ()),
                     clone_url, str(dest), timeout=timeout)
            break
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(dest, ignore_errors=True)
            err = _clone_error(exc.stderr, clone_url)
            if filt and re.search(r"filter|unknown option|usage:", err, re.I):
                continue  # an old git, or a server without partial-clone support
            raise RuntimeError(f"git clone failed: {err}") from exc
        except subprocess.TimeoutExpired as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise RuntimeError(f"git clone timed out after {timeout}s") from exc

    _check_size(dest)
    # Pin the hooks path in the checkout too, so later git commands stay hook-less.
    _git("config", "core.hooksPath", "/dev/null", cwd=dest)
    return {"path": str(dest), "owner": owner, "repo": repo,
            "sha": _git("rev-parse", "HEAD", cwd=dest), "reused": False}


def _refresh(dest: Path, owner: str, repo: str, clone_url: str, timeout: int) -> dict:
    """Bring a reused clone to the upstream head; keep it, marked stale, if that fails."""
    info = {"path": str(dest), "owner": owner, "repo": repo, "reused": True}
    try:
        _net_git("fetch", "--depth", "1", "--no-tags", "origin", "HEAD", cwd=dest,
                 timeout=timeout)
        _net_git("reset", "--hard", "FETCH_HEAD", cwd=dest, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        why = _clone_error(exc.stderr, clone_url)
    except subprocess.TimeoutExpired:
        why = f"timed out after {timeout}s"
    except OSError as exc:
        why = str(exc)
    else:
        _check_size(dest)
        return {**info, "sha": _git("rev-parse", "HEAD", cwd=dest)}
    return {**info, "sha": _git("rev-parse", "HEAD", cwd=dest), "stale": True,
            "note": f"could not fetch the latest commits ({why}); using the earlier clone"}


DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_BUDGET = 120_000
DEFAULT_MAX_TURNS = 40


def _vocab_block(store) -> str:
    getter = getattr(store, "get_vocabulary", None)
    if getter is None:
        return ""
    try:
        return str(getter().block() or "")
    except Exception:  # noqa: BLE001 - a vocabulary problem must not block comprehension
        return ""


def _record_evidence(data: dict, ex, scope: str) -> None:
    """Engine-written evidence: coverage, the unread-files unknown and per-capability basis.
    Whatever the model returned for coverage or basis is overwritten, never trusted."""
    from reincarnate import excerpt, schema

    if ex is None:
        data["coverage"] = schema.unknown_coverage(
            scope, "agentic mode: the agent chose what to read and it was not recorded")
    else:
        data["coverage"] = excerpt.coverage(ex, scope)
        unread = ex.candidates_total - len(ex.files)
        if unread > 0:
            data["unknowns"] = data["unknowns"][:schema.MAX_UNKNOWNS - 1] + [{
                "question": f"What do the {unread} source files that were not read do?",
                "reason": "character budget used"}]
    read = excerpt.read_paths(ex) if ex is not None else set()
    for c in data["capabilities"]:
        seen = ex is not None and any(excerpt.norm_path(f) in read for f in c.get("files") or [])
        name = "observed" if seen else "inferred"
        # a basis only for what the extraction actually holds; an empty field has none
        has = {"what": c.get("what"), "interfaces": c.get("provides") or c.get("requires"),
               "novelty": c.get("novelty"), "acceptance": c.get("acceptance"),
               "data_shapes": c.get("data_shapes"), "key_values": c.get("key_values")}
        c["basis"] = {k: ("inferred" if k == "novelty" else name)
                      for k in schema.BASIS_FIELDS if has.get(k)}


def _stale_fields(info: dict) -> dict:
    return {"stale": True, "clone_note": info["note"]} if info.get("stale") else {}


def comprehend_url(store, url: str, *, model: str | None = None, max_turns: int | None = None,
                   force: bool = False, mode: str = "excerpt", budget: int | None = None,
                   progress=None) -> dict:
    """Clone, run headless comprehension, ingest. Does not run the overlay.

    `progress(stage)` is called as each stage starts ("clone", "excerpt" or "read", "model
    pass"); whatever it raises (the job queue raises Cancelled) propagates and stops the run.

    `excerpt` (default): rank files, pack up to `budget` chars, one isolated tool-less call
    run in a fresh empty directory. `agentic`: the v0.1 read-only multi-turn pass in the clone.
    """
    import tempfile

    from reincarnate import (
        claude_runner,
        excerpt,
        licenses,
        paths,
        prompts,
        render,
        schema,
        secret_scan,
    )

    if mode not in ("excerpt", "agentic"):
        raise ValueError(f"unknown comprehension mode: {mode!r}")
    model = model or DEFAULT_MODEL
    max_turns = max_turns or DEFAULT_MAX_TURNS
    budget = budget or DEFAULT_BUDGET

    def stage(name: str) -> None:
        if progress is not None:
            progress(name)

    stage("clone")
    info = clone_repo(url)
    owner, repo, sha = info["owner"], info["repo"], info["sha"]
    donor = f"{owner}/{repo}"
    existing = store.get_dossier(f"{donor}@{sha}")
    if existing is not None and not force:
        return {"skipped": True, "dossier_id": existing.id, "donor": donor, "sha": sha,
                "message": "dossier already exists for this sha; use --force",
                **_stale_fields(info)}

    profile, projects = store.get_profile(), store.list_projects()
    vocab_block = _vocab_block(store)
    ex = None
    if mode == "excerpt":
        stage("excerpt")
        ex = excerpt.select_files(Path(info["path"]), budget)
        if not ex.files:
            return {"dossier_id": None, "donor": donor, "sha": sha, "capabilities": 0,
                    "cost_usd": 0.0, "note_path": "", "parsed": False, "mode": mode,
                    "excerpt_files": 0, "excerpt_chars": 0,
                    "message": "no readable source files found to excerpt"}
        prompt = prompts.build_excerpt_prompt(
            owner, repo, excerpt.render_excerpt(ex), profile, projects, vocab_block)
        stage("model pass")
        with tempfile.TemporaryDirectory(prefix="reincarnate-run-") as scratch:
            res = claude_runner.run(prompt, cwd=Path(scratch), model=model, max_turns=1, tools="",
                                    label=f"comprehend {donor}")
    else:
        stage("read")
        prompt = prompts.build_comprehension_prompt(owner, repo, profile, projects, vocab_block)
        stage("model pass")
        res = claude_runner.run(prompt, cwd=Path(info["path"]), model=model,
                                max_turns=max_turns, label=f"comprehend {donor} (agentic)")

    extra = {"mode": mode, **_stale_fields(info)}
    if ex is not None:
        extra |= {"excerpt_files": len(ex.files), "excerpt_chars": ex.chars}

    out_dir = paths.ensure(paths.dossiers_dir())
    cost = round(res.cost_usd, 4)
    data = None if res.failed else schema.extract_dossier_json(res.text)
    if data is None:
        raw_path = out_dir / f"{owner}-{repo}.raw.txt"
        raw_text = res.text or res.stderr or (json.dumps(res.raw, indent=2) if res.raw else "")
        raw_path.write_text(raw_text, encoding="utf-8")
        why = (f"model call failed: {res.error_message()}" if res.failed
               else "no dossier JSON in the model's reply")
        return {"dossier_id": None, "donor": donor, "sha": sha, "capabilities": 0,
                "cost_usd": cost, "note_path": str(raw_path), "parsed": False,
                "message": f"{why}; raw saved to {raw_path}", **extra}

    # whatever the model copied from the source, credentials included, never reaches the store
    data, _ = secret_scan.redact_value(schema.normalize_dossier(data))
    deps = data["dependencies"]
    raw_lic = deps.get("license")
    lic = licenses.normalize(raw_lic)
    if lic == "unknown" and ex is not None:
        lic = excerpt.license_hint(ex) or lic
    deps["license"] = lic
    if str(raw_lic or "").strip() not in ("", lic) and not deps.get("license_note"):
        deps["license_note"] = str(raw_lic).strip()
    _record_evidence(data, ex, f"{donor}@{sha}")
    problems = schema.blocking_problems(data)
    if problems:  # a reply that parses but holds no dossier must not replace a good one
        raw_path = out_dir / f"{owner}-{repo}.raw.txt"
        raw_path.write_text(res.text, encoding="utf-8")
        return {"dossier_id": None, "donor": donor, "sha": sha, "capabilities": 0,
                "cost_usd": cost, "note_path": str(raw_path), "parsed": False,
                "message": "the reply is not a usable dossier ("
                           + "; ".join(problems) + f"); raw saved to {raw_path}", **extra}
    canonical = url if url.startswith("https://") else f"https://github.com/{owner}/{repo}"
    dossier = store.upsert_dossier(owner, repo, canonical, sha, data, model=model,
                                   cost_usd=cost, source="claude-p")
    note = out_dir / f"{owner}-{repo}.md"
    note.write_text(render.render_dossier_markdown(dossier, store), encoding="utf-8")
    if getattr(store, "last_reconcile", None):
        extra["reconciled"] = dict(store.last_reconcile)
    return {"dossier_id": dossier.id, "donor": donor, "sha": sha,
            "capabilities": len(data.get("capabilities", [])), "cost_usd": cost,
            "note_path": str(note), "parsed": True,
            "message": "", **extra}

"""Reincarnate engine: read repos clean-room into a capability catalog, then design, compose
and export kits.

Exit codes: 0 success (including partial success), 1 handled failure, 2 usage error,
3 unexpected internal error (REINCARNATE_DEBUG=1 shows the traceback).
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import __version__, licenses, paths, salvage, schema
from .models import Profile, Project
from .schema import blocking_problems, normalize_dossier, unknown_coverage
from .store import Store


class CliError(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


# -- helpers ------------------------------------------------------------------------------
def emit(args, data, text: str | None = None) -> None:
    if getattr(args, "json", False) or text is None:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(text)


# `scheme://user:secret@host` -> `scheme://user:***@host`; `scheme://token@host` -> `***@host`.
_CRED_PAIR = re.compile(r"\b([A-Za-z][\w+.-]*://)([^\s/@:]+):([^\s/@]+)@")
_CRED_TOKEN = re.compile(r"\b([A-Za-z][\w+.-]*://)([^\s/@:]+)@")


def redact(text) -> str:
    """Remove credentials embedded in URLs from a message."""
    s = _CRED_PAIR.sub(r"\1\2:***@", str(text))
    return _CRED_TOKEN.sub(r"\1***@", s)


def fail(args, msg: str, data: dict | None = None) -> int:
    """Report a handled failure: the result JSON (with `error`) on stdout under --json, else
    `error: <msg>` on stderr. Returns exit code 1."""
    msg = redact(msg)
    if getattr(args, "json", False):
        out = dict(data) if isinstance(data, dict) else {}
        out["error"] = redact(out["error"]) if "error" in out else msg
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    else:
        print(f"error: {msg}", file=sys.stderr)
    return 1


def failure(command: str, out) -> str | None:
    """The failure message for a command result, or None when the result is a success.

    Any `error` key fails. Otherwise: comprehend with parsed false; deepen/enrich/review when
    nothing succeeded and at least one item failed; import-vault with errors and zero imported.
    """
    if not isinstance(out, dict):
        return None
    if "error" in out:
        return str(out["error"])
    text = out.get("text") if isinstance(out.get("text"), str) else ""
    if command == "comprehend" and out.get("parsed") is False and not out.get("skipped"):
        return str(out.get("message") or "comprehension failed")
    if command == "deepen" and not out.get("deepened") and out.get("failed"):
        return text or f"{len(out['failed'])} capabilities failed"
    if command == "enrich" and not out.get("updated") and out.get("failed_batches"):
        return text or f"{len(out['failed_batches'])} batches failed"
    if command == "review" and not out.get("reviewed") and out.get("failed_batches"):
        return text or f"{len(out['failed_batches'])} batches failed"
    if (command == "import-vault" and not out.get("imported") and not out.get("already_imported")
            and out.get("errors")):
        errs = out["errors"]
        return (f"imported 0 notes; {len(errs)} error(s): "
                + "; ".join(f"{e.get('file')}: {e.get('reason')}" for e in errs[:5]))
    return None


def finish(args, out) -> int:
    """Print a command result and return its exit code (see `failure`)."""
    msg = failure(getattr(args, "command", ""), out)
    if msg is not None:
        return fail(args, msg, out)
    emit(args, out, _summary(out))
    return 0


def _csv(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def parse_github_url(url: str) -> tuple[str, str]:
    m = re.match(r"^(?:https?://github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$",
                 url.strip())
    if not m:
        raise CliError(f"cannot parse owner/repo from url: {url}")
    return m.group(1), m.group(2)


def infer_project(path: Path) -> dict:
    stack, test_cmd = salvage.infer_stack(path)
    repo_url = ""
    try:
        r = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            repo_url = schema.strip_url_credentials(r.stdout)
    except (OSError, subprocess.SubprocessError):
        pass
    return {"stack": stack, "test_cmd": test_cmd, "repo_url": repo_url}


def _lazy(name: str):
    try:
        return importlib.import_module(f"reincarnate.{name}")
    except ImportError as e:
        # only treat a missing reincarnate module as "not implemented"
        missing = getattr(e, "name", "") or ""
        if missing == "reincarnate" or f"reincarnate.{name}".startswith(missing):
            raise CliError(f"reincarnate.{name} is not implemented yet", 2) from e
        raise


def _delegate(module: str, store: Store, args) -> dict:
    mod = _lazy(module)
    if not hasattr(mod, "run_cli"):
        raise CliError(f"reincarnate.{module}.run_cli is not implemented yet", 2)
    return mod.run_cli(store, args)


def _summary(d) -> str:
    if isinstance(d, dict):
        for k in ("text", "markdown", "summary_text"):
            if isinstance(d.get(k), str):
                return d[k]
        return "\n".join(f"{k}: {v if not isinstance(v, (dict, list)) else json.dumps(v)}"
                         for k, v in d.items())
    return str(d)


# -- handlers -----------------------------------------------------------------------------
def cmd_check_plugin(store, args):
    """Does this engine match the plugin that calls it? The engine is installed as a copy, so a
    plugin update can leave an older engine behind; commands run this first."""
    manifest = Path(args.plugin_root).expanduser() / ".claude-plugin" / "plugin.json"
    try:
        plugin = str(json.loads(manifest.read_text(encoding="utf-8")).get("version") or "")
    except (OSError, ValueError, AttributeError):
        return finish(args, {"error": f"cannot read the plugin version from {manifest}"})
    out = {"engine": __version__, "plugin": plugin, "match": plugin == __version__}
    if not out["match"]:
        out["error"] = (f"engine {__version__} does not match plugin {plugin}; run "
                        "/reincarnate:setup to reinstall the engine from the plugin")
    out["text"] = f"engine {__version__} matches plugin {plugin}" if out["match"] else ""
    return finish(args, out)


def cmd_init(store, args):
    for d in (paths.data_dir(), paths.sandbox_dir(), paths.dossiers_dir(), paths.kits_dir()):
        paths.ensure(d)
    out = {"data_dir": str(paths.data_dir()), "db": str(store.path), "fts5": store.fts}
    emit(args, out, f"initialized {out['data_dir']}\ndb: {out['db']}\nfts5: {out['fts5']}")


def _profile_text(p: Profile | None) -> str:
    if not p:
        return "no profile yet (run: reincarnate profile set --name ...)"
    return (f"name: {p.name}\nstack: {', '.join(p.stack)}\n"
            f"intents:\n" + "\n".join(f"  - {i}" for i in p.intents) +
            f"\nlicense_policy: {json.dumps(p.license_policy)}")


def cmd_profile(store, args):
    p = store.get_profile()
    if args.action == "set":
        p = p or Profile(license_policy={"deny": [], "warn": ["unknown"]})
        if args.name is not None:
            p.name = args.name
        if args.stack is not None:
            p.stack = _csv(args.stack)
        if args.clear_intents:
            p.intents = []
        p.intents = p.intents + [i for i in args.intent if i not in p.intents]
        if args.deny_license:
            deny = list(p.license_policy.get("deny", []))
            new = [licenses.policy_entry(x) for x in args.deny_license]
            p.license_policy = {**p.license_policy,
                                "deny": deny + [x for x in dict.fromkeys(new) if x not in deny]}
        p = store.save_profile(p)
    emit(args, p.to_dict() if p else None, _profile_text(p))


def add_project(store, path, slug=None, goal=None, test_cmd=None) -> Project:
    """Register (or refresh) a project; the rules of `project add`, shared with the web page."""
    path = Path(path).expanduser().resolve()
    if not path.is_dir():
        raise CliError(f"not a directory: {path}")
    inf = infer_project(path)
    slug = slug or re.sub(r"[^a-z0-9._-]+", "-", path.name.lower()).strip("-")
    # re-adding a slug refreshes the path and stack but keeps what the user set earlier;
    # a flag that was given (even empty) wins
    old = store.get_project(slug)
    goal = goal if goal is not None else (old.goal if old else "")
    test_cmd = (test_cmd if test_cmd is not None
                else ((old.test_cmd if old else "") or inf["test_cmd"]))
    return store.add_project(Project(
        slug=slug, path=str(path), repo_url=(old.repo_url if old else "") or inf["repo_url"],
        stack=inf["stack"], goal=goal, test_cmd=test_cmd, created=old.created if old else ""))


def cmd_project(store, args):
    if args.action == "add":
        p = add_project(store, args.path, args.slug, args.goal, args.test_cmd)
        emit(args, p.to_dict(), f"added project {p.slug} ({', '.join(p.stack) or 'unknown stack'})")
    elif args.action == "list":
        ps = store.list_projects()
        text = "\n".join(f"{p.slug}\t{','.join(p.stack)}\t{p.path}" for p in ps) or "no projects"
        emit(args, [p.to_dict() for p in ps], text)
    else:
        ok = store.remove_project(args.slug)
        if not ok:
            raise CliError(f"no such project: {args.slug}")
        emit(args, {"removed": args.slug}, f"removed {args.slug}")


def cmd_ingest(store, args):
    try:
        data = json.loads(Path(args.file).read_text())
    except (OSError, ValueError) as e:
        raise CliError(f"cannot read dossier json: {e}") from e
    data = normalize_dossier(data)
    problems = blocking_problems(data)
    if problems:
        raise CliError("invalid dossier:\n  " + "\n  ".join(problems))
    owner, repo = parse_github_url(args.url)
    if not isinstance(data.get("coverage"), dict) or not data["coverage"]:
        data["coverage"] = unknown_coverage(f"{owner}/{repo}@{args.sha}",
                                            "ingested without coverage")
    d = store.upsert_dossier(owner, repo, args.url, args.sha, data, model=args.model or "",
                             cost_usd=args.cost or 0.0, source=args.source)
    out = {"dossier_id": d.id, "donor": d.donor, "capabilities": len(d.capabilities)}
    text = f"ingested {d.donor} as dossier {d.id} ({out['capabilities']} capabilities)"
    rec = getattr(store, "last_reconcile", None)
    if rec:
        out["reconciled"] = dict(rec)
        text += "; " + ", ".join(f"{n} {k}" for k, n in rec.items())
    emit(args, out, text)


def cmd_dossier(store, args):
    if args.action == "list":
        ds = store.list_dossiers()
        rows = [{"id": d.id, "donor": d.donor, "sha": d.sha, "comprehended_at": d.comprehended_at,
                 "capabilities": len(d.capabilities), "license": d.license, "summary": d.summary}
                for d in ds]
        text = "\n".join(f"{r['id']}\t{r['donor']}\t{r['sha'][:8]}\t{r['capabilities']} caps\t"
                         f"{r['license'] or '?'}\t{r['summary'][:60]}" for r in rows)
        emit(args, rows, text or "no dossiers")
        return
    d = store.get_dossier(args.ref)
    if not d:
        raise CliError(f"no such dossier: {args.ref}")
    if args.md:
        render = _lazy("render")
        if not hasattr(render, "render_dossier_markdown"):
            raise CliError("reincarnate.render.render_dossier_markdown is not implemented yet", 2)
        print(render.render_dossier_markdown(d, store))
        return
    cov = d.data.get("coverage") if isinstance(d.data.get("coverage"), dict) else {}
    lines = [f"{d.donor} @ {d.sha[:8]}  ({d.license or 'license unknown'})", d.summary,
             f"coverage: {cov.get('status') or 'not recorded'}; "
             f"{len(d.data.get('unknowns') or [])} unknowns", ""]
    lines += [f"  [{c.get('portability', '?')}] {c['name']}: {c.get('what', '')}"
              for c in d.capabilities]
    emit(args, d.to_dict(), "\n".join(lines))


def cmd_vote(store, args):
    cap = store.get_capability(args.capability_id)
    if not cap:
        raise CliError(f"no such capability: {args.capability_id}")
    v = {"up": 1, "down": -1, "clear": 0}[args.direction]
    store.set_vote(cap.id, v)
    said = "vote cleared" if v == 0 else f"vote {v:+d}"
    emit(args, {"capability_id": cap.id, "vote": v}, f"{cap.donor}/{cap.name}: {said}")


def cmd_clone(store, args):
    try:
        out = _lazy("comprehend").clone_repo(args.url)
    except ValueError as e:  # the url may carry credentials; fail() redacts them
        return fail(args, _one_line(e))
    return finish(args, out)


def cmd_comprehend(store, args):
    out = _lazy("comprehend").comprehend_url(
        store, args.url, model=args.model, max_turns=args.max_turns, force=args.force,
        mode=getattr(args, "mode", "excerpt"), budget=getattr(args, "budget", None))
    return finish(args, out)


def cmd_import_vault(store, args):
    return finish(args, _lazy("importers.vault").run_cli(store, args))


def cmd_serve(store, args):
    from . import serve
    return serve.run(store.path, port=args.port, open_browser=not args.no_open)


def make_delegate(module: str):
    def handler(store, args):
        return finish(args, _delegate(module, store, args))
    return handler


# -- parser -------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="reincarnate", description=__doc__)
    ap.add_argument("--version", action="version", version=f"reincarnate {__version__}")
    ap.add_argument("--db", help="sqlite db path (default: $REINCARNATE_HOME/reincarnate.db)")
    sub = ap.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name, handler, help_=None):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--json", action="store_true", help="print JSON")
        p.set_defaults(handler=handler)
        return p

    add("init", cmd_init, "create the data dir and db")

    p = add("check-plugin", cmd_check_plugin, "check this engine matches the calling plugin")
    p.add_argument("plugin_root", help="the plugin directory (${CLAUDE_PLUGIN_ROOT})")

    p = add("profile", cmd_profile, "show or set your profile")
    ps = p.add_subparsers(dest="action", required=True)
    ps.add_parser("show").add_argument("--json", action="store_true")
    s = ps.add_parser("set")
    s.add_argument("--json", action="store_true")
    s.add_argument("--name")
    s.add_argument("--stack", help="comma-separated")
    s.add_argument("--intent", action="append", default=[])
    s.add_argument("--deny-license", action="append", default=[])
    s.add_argument("--clear-intents", action="store_true")

    p = add("project", cmd_project, "manage your projects")
    ps = p.add_subparsers(dest="action", required=True)
    s = ps.add_parser("add")
    s.add_argument("path")
    s.add_argument("--slug")
    s.add_argument("--goal")
    s.add_argument("--test-cmd")
    s.add_argument("--json", action="store_true")
    ps.add_parser("list").add_argument("--json", action="store_true")
    s = ps.add_parser("rm")
    s.add_argument("slug")
    s.add_argument("--json", action="store_true")

    p = add("serve", cmd_serve, "local web page: add repos and watch the jobs")
    p.add_argument("--port", type=int, help="default 8649, or the next free port")
    p.add_argument("--no-open", action="store_true", help="do not open a browser")

    p = add("clone", cmd_clone, "sandbox-clone a github repo")
    p.add_argument("url")

    p = add("ingest", cmd_ingest, "ingest a dossier json file")
    p.add_argument("file")
    p.add_argument("--url", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--source", choices=["session", "claude-p", "vault-import"], default="session")
    p.add_argument("--model", default="")
    p.add_argument("--cost", type=float, default=0.0)

    p = add("comprehend", cmd_comprehend, "clone + comprehend + ingest")
    p.add_argument("url")
    p.add_argument("--mode", choices=["excerpt", "agentic"], default="excerpt")
    p.add_argument("--model", help="default claude-haiku-5-5")
    p.add_argument("--budget", type=int, help="excerpt character budget (default 120000)")
    p.add_argument("--max-turns", type=int)
    p.add_argument("--force", action="store_true")

    p = add("enrich", make_delegate("enrich"), "rate novelty + interface tokens (Tier 2)")
    p.add_argument("repo", nargs="?", help="owner/repo")
    p.add_argument("--all", action="store_true")
    p.add_argument("--only-missing", action="store_true")
    p.add_argument("--batch", type=int, default=25)
    p.add_argument("--model", default="claude-haiku-5-5")
    p.add_argument("--fields", default="novelty,interfaces",
                   help="comma list of novelty,interfaces,acceptance")
    p.add_argument("--engine", choices=["haiku", "jev", "jev-sim"], default="haiku",
                   help="haiku (claude -p, via your Claude login), jev (sends capability names, "
                        "descriptions, notes and interface tokens to api.typesafe.ai; needs "
                        "TYPESAFE_API_KEY), jev-sim (local offline stand-in, sends nothing)")
    p.add_argument("--include-private", action="store_true",
                   help="jev engine: also send capabilities whose license is warn/deny, "
                        "proprietary or unknown, or whose donor is your own repo (default: "
                        "they are skipped)")
    p.add_argument("--review-threshold", type=float, default=0.6,
                   help="jev engines: flag capabilities below this confidence for Tier 3 review")
    p.add_argument("--force", action="store_true",
                   help="also re-rate reviewed capabilities and overwrite deepened or existing "
                        "acceptance checks")

    p = add("deepen", make_delegate("deepen"),
            "read donor code for field-level shapes, key values and concrete checks")
    p.add_argument("ids", nargs="*", type=int, help="capability ids")
    p.add_argument("--composition", help="deepen every capability in this composition")
    p.add_argument("--model", default="claude-haiku-5-5")
    p.add_argument("--max-turns", type=int, default=25)
    p.add_argument("--force", action="store_true")

    p = add("review", make_delegate("review"), "Tier 3: Sonnet review of low-confidence enrichment")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--model", default="claude-sonnet-5-5")
    p.add_argument("--batch", type=int, default=10)

    p = add("jev", make_delegate("jev"), "TypeSafe Jev backend status / models")
    p.add_argument("action", choices=["status", "models"])

    p = add("import-interfaces", make_delegate("importers.interfaces"),
            "apply a capability-interfaces.json file of interface tokens")
    p.add_argument("file")
    p.add_argument("--overwrite", action="store_true",
                   help="replace interfaces capabilities already have (default: fill only empty)")

    p = add("vocab", make_delegate("interfaces"), "list the canonical interface vocabulary")
    p.add_argument("--kind")

    p = add("design", make_delegate("design"), "multi-option app designs from the catalog")
    p.add_argument("idea", nargs="*", help='"<idea>" | list | show <slug> | pick <slug> <option>')
    p.add_argument("--options", type=int, default=3)
    p.add_argument("--engine", choices=["claude-p", "session", "offline"], default="claude-p")
    p.add_argument("--proposal", help="JSON file with a session-authored design (engine=session)")
    p.add_argument("--model", help="default claude-opus-5-5")
    p.add_argument("--target", help="for pick: <slug> or new:<name>")
    p.add_argument("--include-commodity", action="store_true")
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--pin", action="append", metavar="IDS",
                   help="capability ids every option must include, e.g. 17,c22 (repeatable)")
    p.add_argument("--shortlist", choices=["lexical", "jev", "jev-sim"], default="lexical",
                   help="re-rank the lexical pool with Jev before composing")

    p = add("overlay", make_delegate("overlay"), "score capabilities against your projects")
    p.add_argument("repo", nargs="?", help="owner/repo")
    p.add_argument("--all", action="store_true")
    p.add_argument("--engine", choices=["claude-p", "heuristic"], default="heuristic")

    p = add("dossier", cmd_dossier, "list or show dossiers")
    ps = p.add_subparsers(dest="action", required=True)
    ps.add_parser("list").add_argument("--json", action="store_true")
    s = ps.add_parser("show")
    s.add_argument("ref", help="owner/repo, owner/repo@sha, or id")
    s.add_argument("--md", action="store_true")
    s.add_argument("--json", action="store_true")

    p = add("catalog", make_delegate("catalog"), "query the capability catalog")
    p.add_argument("--search")
    p.add_argument("--portability", choices=["high", "medium", "low"])
    p.add_argument("--donor")
    p.add_argument("--fit", help="project slug")
    p.add_argument("--gaps", action="store_true")
    p.add_argument("--novelty", choices=["commodity", "pattern", "distinctive", "unrated"])
    p.add_argument("--needs-review", action="store_true",
                   help="only capabilities flagged for Tier 3 review")

    p = add("vote", cmd_vote, "vote on a capability")
    p.add_argument("capability_id", type=int)
    p.add_argument("direction", choices=["up", "down", "clear"])

    p = add("salvage", make_delegate("salvage"), "build a clean-room spec")
    p.add_argument("capability_id", type=int)
    p.add_argument("--into", required=True, help="project slug")
    p.add_argument("--out")

    p = add("compose", make_delegate("compose"), "propose an app from an idea + catalog")
    p.add_argument("idea", nargs="*", help='"<idea>" | list | show <id|slug>')
    p.add_argument("--caps", help="comma-separated capability ids")
    p.add_argument("--target", help="<slug> or new:<name>")
    p.add_argument("--engine", choices=["claude-p", "offline", "session"], default="claude-p")
    p.add_argument("--proposal", help="JSON file with a session-authored proposal (engine=session)")
    p.add_argument("--name", help="app name override")

    p = add("reincarnate", make_delegate("compose"), "whole-donor composition")
    p.add_argument("repo", help="owner/repo")
    p.add_argument("--into", required=True, help="<slug> or new:<name>")
    p.set_defaults(reincarnate_donor=True)

    p = add("kit", make_delegate("kit"), "export a buildable .reincarnate/ kit")
    p.add_argument("composition", help="composition id or slug")
    p.add_argument("--into", required=True, help="dir, project slug, or new:<name>")
    p.add_argument("--zip", action="store_true")
    p.add_argument("--notes", help="markdown file whose text becomes the Glue section")
    p.add_argument("--deepen", action="store_true",
                   help="run `deepen` for parts that are not deepened before exporting")
    p.add_argument("--allow-missing", action="store_true",
                   help="export even when parts are missing, retired or replaced")
    p.add_argument("--force", action="store_true",
                   help="re-export over edited specs or ticked steps (old kit is backed up)")
    p.add_argument("--no-plan", action="store_true",
                   help="skip the contracts and scope pass (one Sonnet call, about $0.05-0.10); "
                   "implies the parts planner")
    p.add_argument("--planner", choices=["idea", "parts"],
                   help="plan from the idea and the stack (idea, default; two Opus calls, about "
                   "$1-1.40) or one step per catalog part (parts); env REINCARNATE_PLANNER sets "
                   "the default")
    p.add_argument("--answers", help="JSON file of answers to the open decisions, "
                   '{"D1": {"question": "...", "answer": "..."}} (or {"D1": "answer"}); '
                   "an answer whose question is no longer asked is not applied")
    p.add_argument("--decisions", action="store_true",
                   help="print the open decisions and write no kit (use with --json); "
                   "with --deepen, deepens first so the decisions match the export")

    p = add("import-vault", cmd_import_vault,
            "import dossier notes from a Markdown (Obsidian) vault")
    p.add_argument("dir")
    p.add_argument("--force", action="store_true",
                   help="reconcile notes whose owner/repo/sha is already in the store")

    p = add("export-md", make_delegate("render"), "render a dossier as markdown")
    p.add_argument("ref", help="owner/repo")
    p.add_argument("--out")
    return ap


# Raised from a handler, these are handled failures (exit 1), not bugs.
HANDLED = (ValueError, RuntimeError, OSError, sqlite3.Error, OverflowError, json.JSONDecodeError)


def _one_line(e: BaseException) -> str:
    msg = " ".join(str(e).split()) or type(e).__name__
    if isinstance(e, sqlite3.Error):
        msg = f"database error: {msg}"
    elif isinstance(e, OverflowError):
        msg = f"number out of range: {msg}"
    return msg


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = Path(args.db) if args.db else None
    try:
        store = Store(db)
    except (sqlite3.Error, OSError) as e:
        return fail(args, f"cannot open database {db or paths.db_path()}: {_one_line(e)}")
    try:
        try:
            rc = args.handler(store, args)
        finally:
            store.close()
    except CliError as e:
        fail(args, str(e))
        return e.code
    except BrokenPipeError:
        return 0
    except HANDLED as e:
        return fail(args, _one_line(e))
    except Exception as e:  # noqa: BLE001 - one line instead of a traceback; exit 3
        if os.environ.get("REINCARNATE_DEBUG", "") not in ("", "0"):
            raise
        msg = (f"internal error: {type(e).__name__}: {_one_line(e)} "
               "(set REINCARNATE_DEBUG=1 for the traceback)")
        fail(args, msg)
        return 3
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())

"""argparse entry point. Modules owned by other agents are imported lazily per handler."""

from __future__ import annotations

import argparse
import importlib
import json
import re
import subprocess
import sys
from pathlib import Path

from . import __version__, paths
from .models import Profile, Project
from .schema import normalize_dossier, validate_dossier
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


def _csv(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def parse_github_url(url: str) -> tuple[str, str]:
    m = re.match(r"^(?:https?://github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$",
                 url.strip())
    if not m:
        raise CliError(f"cannot parse owner/repo from url: {url}")
    return m.group(1), m.group(2)


def infer_project(path: Path) -> dict:
    stack: list[str] = []
    test_cmd = ""
    if (path / "pyproject.toml").exists() or (path / "requirements.txt").exists():
        stack.append("python")
        test_cmd = "pytest"
    pkg = path / "package.json"
    if pkg.exists():
        stack.append("node")
        if (path / "tsconfig.json").exists():
            stack.append("typescript")
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {})
        except (ValueError, OSError, AttributeError):
            scripts = {}
        if "test" in scripts and not test_cmd:
            test_cmd = "npm test"
    if (path / "go.mod").exists():
        stack.append("go")
        test_cmd = test_cmd or "go test ./..."
    if (path / "Cargo.toml").exists():
        stack.append("rust")
        test_cmd = test_cmd or "cargo test"
    repo_url = ""
    try:
        r = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            repo_url = r.stdout.strip()
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
            p.license_policy = {**p.license_policy,
                                "deny": deny + [x for x in args.deny_license if x not in deny]}
        p = store.save_profile(p)
    emit(args, p.to_dict() if p else None, _profile_text(p))


def cmd_project(store, args):
    if args.action == "add":
        path = Path(args.path).expanduser().resolve()
        if not path.is_dir():
            raise CliError(f"not a directory: {path}")
        inf = infer_project(path)
        slug = args.slug or re.sub(r"[^a-z0-9._-]+", "-", path.name.lower()).strip("-")
        p = store.add_project(Project(slug=slug, path=str(path), repo_url=inf["repo_url"],
                                      stack=inf["stack"], goal=args.goal or "",
                                      test_cmd=args.test_cmd or inf["test_cmd"]))
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
    problems = validate_dossier(data)
    if problems:
        raise CliError("invalid dossier:\n  " + "\n  ".join(problems))
    owner, repo = parse_github_url(args.url)
    d = store.upsert_dossier(owner, repo, args.url, args.sha, data, model=args.model or "",
                             cost_usd=args.cost or 0.0, source=args.source)
    out = {"dossier_id": d.id, "donor": d.donor, "capabilities": len(d.capabilities)}
    emit(args, out, f"ingested {d.donor} as dossier {d.id} ({out['capabilities']} capabilities)")


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
        print(render.render_dossier_markdown(d))
        return
    lines = [f"{d.donor} @ {d.sha[:8]}  ({d.license or 'license unknown'})", d.summary, ""]
    lines += [f"  [{c.get('portability', '?')}] {c['name']}: {c.get('what', '')}"
              for c in d.capabilities]
    emit(args, d.to_dict(), "\n".join(lines))


def cmd_vote(store, args):
    cap = store.get_capability(args.capability_id)
    if not cap:
        raise CliError(f"no such capability: {args.capability_id}")
    v = {"up": 1, "down": -1, "clear": 0}[args.direction]
    store.set_vote(cap.id, v)
    emit(args, {"capability_id": cap.id, "vote": v}, f"{cap.donor}/{cap.name}: vote {v:+d}")


def cmd_clone(store, args):
    out = _lazy("comprehend").clone_repo(args.url)
    emit(args, out, _summary(out))


def cmd_comprehend(store, args):
    out = _lazy("comprehend").comprehend_url(store, args.url, model=args.model,
                                             max_turns=args.max_turns, force=args.force)
    emit(args, out, _summary(out))


def cmd_import_vault(store, args):
    out = _lazy("importers.vault").run_cli(store, args)
    emit(args, out, _summary(out))


def make_delegate(module: str):
    def handler(store, args):
        out = _delegate(module, store, args)
        emit(args, out, _summary(out))
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

    p = add("clone", cmd_clone, "sandbox-clone a github repo")
    p.add_argument("url")

    p = add("ingest", cmd_ingest, "ingest a dossier json file")
    p.add_argument("file")
    p.add_argument("--url", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--source", choices=["session", "claude-p", "vault-import"], default="session")
    p.add_argument("--model", default="")
    p.add_argument("--cost", type=float, default=0.0)

    p = add("comprehend", cmd_comprehend, "clone + comprehend + ingest + overlay")
    p.add_argument("url")
    p.add_argument("--model")
    p.add_argument("--max-turns", type=int)
    p.add_argument("--force", action="store_true")

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

    p = add("import-vault", cmd_import_vault, "import second-brain dossier notes")
    p.add_argument("dir")

    p = add("export-md", make_delegate("render"), "render a dossier as markdown")
    p.add_argument("ref", help="owner/repo")
    p.add_argument("--out")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        store = Store(Path(args.db) if args.db else None)
        try:
            args.handler(store, args)
        finally:
            store.close()
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""`reincarnate serve`: the local ingestion page and its JSON API.

Security: bound to 127.0.0.1; every /api request needs the per-process token header; Host and
Origin must be our own; a POST must be JSON and at most 64 KB. The page at `/` holds no data.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import sys
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import (
    auth,
    cli,
    compose,
    comprehend,
    design,
    enrich,
    estimate,
    fitcheck,
    jobs,
    kit,
    licenses,
    salvage,
)
from .store import Store

DEFAULT_PORT = 8649
MAX_BODY = 64 * 1024
MAX_REPOS = 50
TOKEN_HEADER = "X-Reincarnate-Token"
WEB = Path(__file__).parent / "web"
PAGE = WEB / "ingest.html"
STUDIO_PAGE = WEB / "studio.html"
PAGES = ("/", "/studio")
PLACEHOLDER = "<!doctype html><title>Reincarnate</title><p>The ingestion page is not installed.</p>"
STUDIO_PLACEHOLDER = PLACEHOLDER.replace("ingestion", "studio")
MAX_IDS = 60
MAX_TARGET = 500
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; "
       "frame-ancestors 'none'")
_JOB_PATH = re.compile(r"^/api/jobs/(\d+)/(cancel|retry)$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_DESIGN_PATH = re.compile(r"^/api/designs/([A-Za-z0-9][A-Za-z0-9._-]{0,127})(/pick)?$")
_NOVELTY = {"distinctive": "d", "pattern": "p", "commodity": "c"}


@contextmanager
def _open(db_path):
    store = Store(db_path)
    try:
        yield store
    finally:
        store.close()


class ApiError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def normalize_repo(text) -> str:
    """`owner/repo` for a GitHub `owner/repo` or https URL; ValueError for anything else
    (ssh remotes, other hosts, local paths)."""
    s = str(text or "").strip()
    if s.lower().startswith("git@"):
        raise ValueError("ssh remotes are not accepted")
    owner, repo = comprehend.parse_github_url(s)
    return f"{owner}/{repo}"


def split_repos(values) -> tuple[list[str], list[str]]:
    """(normalized unique repos, rejected inputs) from a request's `repos` list."""
    if not isinstance(values, list) or not values:
        raise ApiError(400, "repos must be a non-empty list")
    if len(values) > MAX_REPOS:
        raise ApiError(400, f"at most {MAX_REPOS} repos per request")
    good, bad = [], []
    for v in values:
        try:
            if not isinstance(v, str):
                raise ValueError("not a string")
            r = normalize_repo(v)
        except ValueError:
            bad.append(str(v)[:200])
            continue
        if r.lower() not in {g.lower() for g in good}:
            good.append(r)
    return good, bad


def _nov(caps) -> dict:
    out = {"d": 0, "p": 0, "c": 0, "u": 0}
    for c in caps:
        out[_NOVELTY.get(c.novelty, "u")] += 1
    return out


def _policy(profile) -> dict:
    p = profile.license_policy if profile and isinstance(profile.license_policy, dict) else {}
    return p or {"deny": [], "warn": ["unknown"]}


def build_state(store: Store) -> dict:
    """Everything the page shows apart from jobs, composed from the catalog."""
    profile = store.get_profile()
    policy = _policy(profile)
    dossiers = store.list_dossiers(current_only=True)
    caps = store.list_capabilities(current_only=True)
    by_dossier: dict[int, list] = {}
    for c in caps:
        by_dossier.setdefault(c.dossier_id, []).append(c)
    review = [d for d in dossiers if licenses.is_review(d.license)
              or licenses.level(d.license, policy) in ("warn", "deny")]
    unpinned = [d for d in dossiers if d.sha == "unknown"]
    spend = store.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM dossiers").fetchone()[0]
    summary = {"caps": len(caps), "donors": len(dossiers), "nov": _nov(caps),
               "deep": sum(1 for c in caps if c.deepened_at), "spend": round(spend, 4),
               "pinned": len(dossiers) - len(unpinned), "dossiers": len(dossiers),
               "license_review": len(review)}
    own_owners = enrich._own_owners(store)
    own = [{"donor": d.donor, "n": len(by_dossier.get(d.id, [])), "license": d.license,
            "summary": d.summary, "nov": _nov(by_dossier.get(d.id, []))}
           for d in dossiers if d.owner.lower() in own_owners]
    recent = sorted(dossiers, key=lambda d: (d.comprehended_at, d.id), reverse=True)[:10]
    return {
        "summary": summary,
        "auth": auth.cached_status(),
        "profile": {"name": profile.name if profile else "",
                    "stack": list(profile.stack) if profile else [],
                    "deny": list(policy.get("deny") or []), "warn": list(policy.get("warn") or [])},
        "projects": [{"slug": p.slug, "path": p.path, "goal": p.goal, "test_cmd": p.test_cmd,
                      "stack": p.stack} for p in store.list_projects()],
        "own": own,
        "upkeep": _upkeep(store, dossiers, caps, unpinned, review),
        "recent": [{"donor": d.donor, "at": d.comprehended_at, "source": d.source,
                    "n": len(by_dossier.get(d.id, []))} for d in recent],
    }


def _upkeep(store, dossiers, caps, unpinned, review) -> list[dict]:
    """Things worth fixing, each with the command that fixes it. Empty groups are left out."""
    out = []
    if unpinned:
        out.append({
            "key": "unknown-commit", "title": "Repos at an unknown commit",
            "why": "Without a commit, a part cannot be traced to the code it came from.",
            "items": [d.donor for d in unpinned],
            "cmd": "\n".join(f"reincarnate comprehend https://github.com/{d.donor} --force"
                             for d in unpinned)})
    unread = [c for c in caps if not c.deepened_at]
    if unread:
        per: dict[str, int] = {}
        for c in unread:
            per[c.donor] = per.get(c.donor, 0) + 1
        top = sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        out.append({
            "key": "unread-in-code", "title": "Parts not read in code",
            "why": "These parts are described from an excerpt; deepen reads the donor code. "
                   "The command takes the first 10.",
            "items": [f"{donor}: {n} parts" for donor, n in top],
            "cmd": "reincarnate deepen " + " ".join(str(c.id) for c in unread[:10])})
    if review:
        out.append({
            "key": "license-review", "title": "Licenses to review",
            "why": "Copyleft, source-available, proprietary or unknown licenses need a look "
                   "before a part is reused.",
            "items": [f"{d.donor} ({d.license or 'unknown'})" for d in review],
            "cmd": "\n".join(f"reincarnate dossier show {d.donor}" for d in review)})
    failed: dict[str, str] = {}
    seen: set[str] = set()
    for r in store.list_jobs(limit=500):  # newest first: a repo's latest comprehend decides
        if r["kind"] != "comprehend" or r["target"] in seen or r["state"] in jobs.ACTIVE:
            continue
        seen.add(r["target"])
        if r["state"] == "failed":
            failed[r["target"]] = r["error"] or "failed"
    if failed:
        out.append({
            "key": "failed-imports", "title": "Failed imports",
            "why": "The last attempt to add these repos failed; the cause is on each job.",
            "items": [f"{t}: {e[:160]}" for t, e in failed.items()],
            "cmd": "\n".join(f"reincarnate comprehend https://github.com/{t}" for t in failed)})
    return out


# -- studio helpers -----------------------------------------------------------------------
def _int_list(values, what: str) -> list[int]:
    if not isinstance(values, list):
        raise ApiError(400, f"{what} must be a list of part ids")
    if len(values) > MAX_IDS:
        raise ApiError(400, f"at most {MAX_IDS} {what} per request")
    out = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ApiError(400, f"{what} must be whole numbers; got {str(v)[:40]!r}")
        if v not in out:
            out.append(v)
    return out


def _pins(store, values, *, check_license=True) -> list[int]:
    """Validated part ids: they exist, are live and (for pins) have a license the profile allows."""
    ids = _int_list(values if values is not None else [], "pin")
    if check_license:
        try:
            design._check_pins(store, ids, store.get_profile())
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
    else:
        for i in ids:
            if store.get_capability(i) is None:
                raise ApiError(400, f"no such part: c{i}")
    return ids


def _options_count(v) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 2 <= v <= 4:
        raise ApiError(400, "options must be a whole number from 2 to 4")
    return v


def _target(store, value, *, required=True) -> str:
    """A kit target as the CLI takes it: a project slug, `new:<name>` or an existing absolute
    directory (a relative path would depend on the server's working directory)."""
    if value is None or value == "":
        if required:
            raise ApiError(400, "target is required")
        return ""
    if not isinstance(value, str) or len(value) > MAX_TARGET or "\0" in value:
        raise ApiError(400, "target must be text of at most 500 characters")
    t = value.strip()
    if t.lower().startswith(salvage.NEW_PREFIX):
        if not t[len(salvage.NEW_PREFIX):].strip():
            raise ApiError(400, "new: target needs a name")
        return t
    if store.get_project(t) is not None:
        return t
    if t.startswith("/") or (len(t) > 2 and t[1] == ":"):
        if not Path(t).is_absolute() or not Path(t).is_dir():
            raise ApiError(400, f"target directory does not exist: {t}")
        return t
    if t.startswith(("~", ".")) or "/" in t or "\\" in t:
        raise ApiError(400, "a directory target must be an absolute path")
    raise ApiError(400, f"unknown target: {t[:80]} (a project slug, new:<name>, or an absolute "
                        "directory)")


def _gated(obj):
    """Donor-derived JSON (shapes, key values) with every string through the air-gap gate."""
    if isinstance(obj, str):
        return salvage.gate_text(obj)
    if isinstance(obj, list):
        return [_gated(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _gated(v) for k, v in obj.items()}
    return obj


def build_catalog(store: Store) -> dict:
    """Every current part in the shape the studio lists, plus its donors and projects."""
    profile = store.get_profile()
    fits: dict[int, list] = {}
    for f in store.list_fits():
        fits.setdefault(f.capability_id, []).append([f.project_slug, round(f.score, 2)])
    dossiers = {d.id: d for d in store.list_dossiers(current_only=True)}
    parts, per_donor = [], {}
    for c in store.list_capabilities(current_only=True):
        dossier = dossiers.get(c.dossier_id)
        per_donor[c.dossier_id] = per_donor.get(c.dossier_id, 0) + 1
        parts.append({
            "id": c.id, "name": salvage.gate_line(c.name),
            "donor": salvage.gate_line(c.donor, n=200),
            "license": licenses.normalize(c.license),
            "license_level": compose.license_level(c.license, profile) or "ok",
            "license_review": licenses.is_review(c.license), "novelty": c.novelty or "",
            "portability": c.portability or "medium", "deepened": bool(c.deepened_at),
            "what": salvage.gate_text(c.what),
            "design_notes": salvage.gate_text(c.portability_notes),
            "security": salvage.gate_text(c.security_notes),
            "provides": list(c.provides or []), "requires": list(c.requires or []),
            "acceptance": [salvage.gate_text(a, keep_urls=True) for a in c.acceptance or []],
            "data_shapes": _gated(c.data_shapes or []), "key_values": _gated(c.key_values or []),
            "files_count": len(c.files or []), "fits": fits.get(c.id, []),
            "basis": dict(c.basis or {}), "evidence": salvage.evidence_line(c, dossier)})
    donors = [{"donor": salvage.gate_line(d.donor, n=200), "n": per_donor.get(d.id, 0),
               "license": licenses.normalize(d.license),
               "level": compose.license_level(d.license, profile) or "ok",
               "review": licenses.is_review(d.license)} for d in dossiers.values()]
    return {"parts": parts, "donors": donors, "projects": [p.slug for p in store.list_projects()]}


def _ref(x) -> str:
    return salvage.gate_line(x.get("name") or x.get("id") or "?") if isinstance(x, dict) \
        else salvage.gate_line(x)


def design_view(d) -> dict:
    """A stored design as the studio shows it; donor-derived text is gated."""
    options = []
    for o in d.options:
        w = o.get("wiring") or {}
        counts = w.get("counts") or {}
        options.append({
            "slot": o.get("slot", ""), "slot_label": o.get("slot_label", ""),
            "name": salvage.gate_line(o.get("name")), "slug": o.get("slug", ""),
            "pitch": salvage.gate_text(o.get("pitch")),
            "rationale": salvage.gate_text(o.get("rationale")),
            "stack": salvage.gate_line(o.get("stack"), n=200), "scores": o.get("scores") or {},
            "parts": [{"id": c.get("capability_id"), "name": salvage.gate_line(c.get("name")),
                       "donor": salvage.gate_line(c.get("donor"), n=200),
                       "license": licenses.normalize(c.get("license")),
                       "role": salvage.gate_line(c.get("role"), n=200),
                       "pinned": bool(c.get("pinned"))} for c in o.get("capabilities") or []],
            "wiring": {
                "wired": [f"{_ref(x['consumer'])} needs {salvage.gate_line(x['token'], n=60)} "
                          f"from {_ref(x['provider'])}" for x in w.get("wired") or []],
                "unmet": [f"{_ref(x['consumer'])} needs {salvage.gate_line(x['token'], n=60)}"
                          for x in w.get("unmet") or []],
                "counts": {k: counts.get(k, 0)
                           for k in ("wired", "unmet", "settings", "external")}},
            "coverage": o.get("coverage") or [],
            "flags": [salvage.gate_text(f) for f in o.get("flags") or []],
            "glue": [salvage.gate_text(g) for g in o.get("glue") or []],
            "new_work": [salvage.gate_text(g) for g in o.get("new_work") or []]})
    return {"slug": d.slug, "title": salvage.gate_line(d.title, n=200),
            "idea": d.idea, "requirements": [
                {"id": r.get("id"), "text": salvage.gate_text(r.get("text"))}
                for r in d.requirements], "meta": d.meta, "options": options}


class Handler(BaseHTTPRequestHandler):
    server_version = "reincarnate"
    sys_version = ""

    # -- plumbing -------------------------------------------------------------------------
    def log_request(self, code="-", size="-"):
        print(f"reincarnate serve: {self.command} {self.path.partition('?')[0]} {code}",
              file=sys.stderr, flush=True)

    def log_message(self, fmt, *args):
        print("reincarnate serve: " + fmt % args, file=sys.stderr, flush=True)

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _check_origin(self) -> None:
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in hosts:
            raise ApiError(403, "bad Host header")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in hosts}:
            raise ApiError(403, "bad Origin header")

    def _check_token(self) -> None:
        sent = self.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(sent.encode("utf-8"), self.server.token.encode("utf-8")):
            raise ApiError(403, "missing or wrong token")

    def _body(self) -> dict:
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(400, "Content-Type must be application/json")
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            raise ApiError(400, "chunked bodies are not accepted")
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise ApiError(400, "Content-Length is required") from None
        if n < 0:
            raise ApiError(400, "bad Content-Length")
        if n > MAX_BODY:
            if n <= 8 * MAX_BODY:  # read it off the socket so the client can see the answer
                self.rfile.read(n)
            raise ApiError(413, f"body is over {MAX_BODY // 1024} KB")
        raw = self.rfile.read(n) if n else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "body is not valid JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "body must be a JSON object")
        return data

    def _handle(self, method: str) -> None:
        try:
            self._check_origin()
            path = self.path.partition("?")[0]
            if method == "GET" and path in PAGES:
                self._page(path)
                return
            if not path.startswith("/api/"):
                raise ApiError(404, "not found")
            self._check_token()
            body = self._body() if method == "POST" else None
            self._json(200, self._route(method, path, body))
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001 - the page gets a JSON error, not a traceback
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _page(self, path: str = "/") -> None:
        file, fallback = ((PAGE, PLACEHOLDER) if path == "/"
                          else (STUDIO_PAGE, STUDIO_PLACEHOLDER))
        try:
            html = file.read_text(encoding="utf-8")
        except OSError:
            html = fallback
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                   {"Content-Security-Policy": CSP})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _other(self):
        self._json(405, {"error": "method not allowed"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _other

    def _store(self):
        return _open(self.server.db_path)

    # -- routes ---------------------------------------------------------------------------
    def _route(self, method: str, path: str, body: dict | None):
        queue: jobs.JobQueue = self.server.queue
        if method == "GET" and path == "/api/state":
            with self._store() as store:
                return build_state(store)
        if method == "GET" and path == "/api/jobs":
            return {"jobs": [j.to_dict() for j in queue.list(100)]}
        if method == "POST" and path == "/api/estimate":
            return self._estimate(body)
        if method == "POST" and path == "/api/jobs":
            return self._submit(queue, body)
        m = _JOB_PATH.match(path)
        if method == "POST" and m:
            try:
                job = (queue.cancel if m.group(2) == "cancel" else queue.retry)(int(m.group(1)))
            except LookupError as exc:
                raise ApiError(404, str(exc)) from None
            except ValueError as exc:
                raise ApiError(400, str(exc)) from None
            return {"job": job.to_dict()}
        if method == "POST" and path == "/api/projects":
            return self._project(body)
        return self._studio(queue, method, path, body)

    # -- studio routes -------------------------------------------------------------------
    def _studio(self, queue: jobs.JobQueue, method: str, path: str, body: dict | None):
        if method == "GET" and path == "/api/catalog":
            with self._store() as store:
                return build_catalog(store)
        if method == "POST" and path == "/api/fit":
            return self._fit(body)
        if method == "POST" and path == "/api/estimate/design":
            options = _options_count(body.get("options", 3))
            with self._store() as store:
                _pins(store, body.get("pin"))
            return estimate.estimate_design(options, body.get("pin"))
        if method == "POST" and path == "/api/designs":
            return self._new_design(queue, body)
        if method == "GET" and path == "/api/designs":
            with self._store() as store:
                return {"designs": [{"slug": d.slug, "title": d.title, "idea": d.idea,
                                     "created": d.created, "options": len(d.options),
                                     "engine": d.engine} for d in store.list_designs()]}
        m = _DESIGN_PATH.match(path)
        if m and method == "GET" and not m.group(2):
            with self._store() as store:
                d = store.get_design(m.group(1))
                if d is None:
                    raise ApiError(404, f"no such design: {m.group(1)}")
                return design_view(d)
        if m and method == "POST" and m.group(2):
            return self._pick(m.group(1), body)
        if method == "POST" and path == "/api/kits/preview":
            return self._preview(body)
        if method == "POST" and path == "/api/kits":
            return self._new_kit(queue, body)
        raise ApiError(404, "not found")

    def _fit(self, body: dict) -> dict:
        ids = _int_list(body.get("ids"), "ids")
        with self._store() as store:
            _pins(store, ids, check_license=False)
            try:
                return fitcheck.fit(store, ids, body.get("roles"))
            except ValueError as exc:
                raise ApiError(400, str(exc)) from None

    def _new_design(self, queue: jobs.JobQueue, body: dict) -> dict:
        with self._store() as store:
            pins = _pins(store, body.get("pin"))
        roles = {} if body.get("roles") is None else body["roles"]
        if not isinstance(roles, dict) or not all(isinstance(v, str) for v in roles.values()):
            raise ApiError(400, "roles must be an object of part id to role text")
        try:
            params = jobs.clean_params("design", {
                "idea": body.get("idea"), "pins": pins, "roles": roles,
                "options": body.get("options", 3),
                "include_commodity": body.get("include_commodity")})
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        return {"job": queue.submit("design", params["idea"][:200], params).to_dict()}

    def _pick(self, slug: str, body: dict) -> dict:
        option = body.get("option")
        if not isinstance(option, str) or not option.strip():
            raise ApiError(400, "option is required (a slot or option slug)")
        with self._store() as store:
            target = _target(store, body.get("target"), required=False)
            if store.get_design(slug) is None:
                raise ApiError(404, f"no such design: {slug}")
            try:
                comp = design.pick(store, slug, option, target=target or None)
            except ValueError as exc:
                raise ApiError(400, str(exc)) from None
        return {"composition": {"slug": comp.slug, "name": salvage.gate_line(comp.name),
                                "target_kind": comp.target_kind, "target_slug": comp.target_slug,
                                "problems": list((comp.proposal or {}).get("problems") or [])}}

    def _composition(self, store: Store, ref):
        if not isinstance(ref, (str, int)) or isinstance(ref, bool) or not str(ref).strip():
            raise ApiError(400, "composition is required")
        comp = store.get_composition(str(ref).strip())
        if comp is None:
            raise ApiError(404, f"no such composition: {ref}")
        return comp

    def _preview(self, body: dict) -> dict:
        with self._store() as store:
            comp = self._composition(store, body.get("composition"))
            target = _target(store, body.get("target"))
            out = kit.export_kit(store, comp, target, dry_run=True)
        steps = [{**st, "title": salvage.gate_line(st["title"], n=200),
                  "spec_head": salvage.gate_text(st["spec_head"]),
                  "needs": [salvage.gate_line(x, n=200) for x in st["needs"]]}
                 for st in out["steps"]]
        unread = sum(1 for st in out["steps"] if st["kind"] == "cap" and not st["deepened"])
        est = estimate.estimate_plan(len(out["steps"]))
        rest = estimate.estimate_reconcile(len(out["steps"]))
        decisions = [{**d, "question": salvage.gate_line(d["question"], n=300),
                      "answer": salvage.gate_text(d["answer"]) if d["answer"] else None}
                     for d in out["decisions"]]
        return {"target": out["target"], "steps": steps, "warnings": out["warnings"],
                "refusal": out["refusal"], "decisions": decisions,
                "deepen": {"needed": unread, "cost": estimate.estimate_deepen(unread)["low"]},
                "plan": {"cached": bool(out["plan"]["cached"]), "low": est["low"],
                         "high": est["high"], "status": out["plan"]["status"]},
                "plan2": {"cached": bool(out["plan2"]["cached"]), "low": est["low"],
                          "high": est["high"], "status": out["plan2"]["status"]},
                "reconcile": {"cached": bool(out["reconcile"]["cached"]), "low": rest["low"],
                              "high": rest["high"], "status": out["reconcile"]["status"],
                              "changes": out["reconcile"]["changes"]}}

    def _new_kit(self, queue: jobs.JobQueue, body: dict) -> dict:
        with self._store() as store:
            comp = self._composition(store, body.get("composition"))
            target = _target(store, body.get("target"))
        try:
            params = jobs.clean_params("kit", {
                "composition": comp.slug, "target": target, "deepen": body.get("deepen"),
                "force": body.get("force"), "answers": body.get("answers")})
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        return {"job": queue.submit("kit", comp.slug, params).to_dict()}

    @staticmethod
    def _options(body: dict) -> dict:
        try:
            return jobs.clean_params("comprehend", body)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None

    def _estimate(self, body: dict) -> dict:
        repos, bad = split_repos(body.get("repos"))
        opt = self._options(body)
        with self._store() as store:
            est = estimate.estimate_comprehend(store, len(repos), opt["mode"], opt["model"],
                                               opt["enrich"])
        return {"repos": repos, "invalid": bad, **est}

    def _submit(self, queue: jobs.JobQueue, body: dict) -> dict:
        kind = body.get("kind") or "comprehend"
        if kind == "deepen":
            if not (body.get("composition") or body.get("ids")):
                raise ApiError(400, "deepen needs a composition or capability ids")
            try:
                params = jobs.clean_params("deepen", body)
            except ValueError as exc:
                raise ApiError(400, str(exc)) from None
            target = params.get("composition") or ",".join(map(str, params["ids"]))[:200]
            return {"jobs": [queue.submit("deepen", target, params).to_dict()], "invalid": []}
        if kind not in ("comprehend", "enrich"):
            raise ApiError(400, f"unknown job kind: {kind!r}")
        repos, bad = split_repos(body.get("repos"))
        params = self._options(body) if kind == "comprehend" else {}
        made = [queue.submit(kind, r, params).to_dict() for r in repos]
        return {"jobs": made, "invalid": bad}

    def _project(self, body: dict) -> dict:
        path, slug, goal = body.get("path"), body.get("slug"), body.get("goal")
        if not isinstance(path, str) or not path.strip():
            raise ApiError(400, "path is required")
        if slug is not None and not (isinstance(slug, str) and _SLUG.match(slug)):
            raise ApiError(400, "slug must be lower-case letters, digits, '.', '_' or '-'")
        if goal is not None and not (isinstance(goal, str) and len(goal) <= 1000):
            raise ApiError(400, "goal must be text of at most 1000 characters")
        with self._store() as store:
            try:
                project = cli.add_project(store, path.strip(), slug, goal)
            except cli.CliError as exc:
                raise ApiError(400, str(exc)) from None
        return {"project": project.to_dict()}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, db_path: Path, token: str):
        super().__init__(addr, Handler)
        self.db_path, self.token = Path(db_path), token
        self.queue: jobs.JobQueue | None = None


def make_server(db_path: Path, port: int | None = None, *, token: str | None = None,
                queue: jobs.JobQueue | None = None) -> Server:
    """A server bound to 127.0.0.1. port=None tries 8649 and the next free ones; port=0 asks
    the OS for any free port. The job worker starts only once a port is bound."""
    token = token or secrets.token_urlsafe(24)
    candidates = [port] if port is not None else range(DEFAULT_PORT, DEFAULT_PORT + 20)
    last: OSError | None = None
    for p in candidates:
        try:
            server = Server(("127.0.0.1", p), db_path, token)
        except OSError as exc:
            last = exc
            continue
        server.queue = queue or jobs.JobQueue(db_path)
        return server
    raise OSError(f"no free port to serve on: {last}")


def run(db_path: Path, *, port: int | None = None, open_browser=True) -> int:
    try:
        server = make_server(db_path, port)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/#t={server.token}"
    print(f"reincarnate serve: {url}\nCtrl-C stops it.", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nreincarnate serve: stopping", file=sys.stderr)
    finally:
        server.queue.shutdown()
        server.server_close()
    return 0

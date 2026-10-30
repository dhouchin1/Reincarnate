"""The job queue behind `reincarnate serve`: one daemon worker runs jobs one at a time.

The `jobs` table is the queue. Every public method opens its own short-lived Store, so it is
safe to call from request threads; the worker keeps a Store of its own.
"""

from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from . import design
from .estimate import MODELS
from .store import Store, now

KINDS = ("comprehend", "enrich", "deepen", "design", "kit")
MODES = ("excerpt", "agentic")
MAX_IDEA = 2000
INTERRUPTED = "interrupted: the server stopped"
ACTIVE = ("queued", "running")
AUTH_HINT = " Run `claude auth login` in a terminal, then retry."
_AUTH_WORDS = re.compile(r"oauth|authenticate|login", re.I)
ENRICH_FIELDS = ("novelty", "interfaces", "acceptance")


class Cancelled(Exception):
    """Raised from the progress callback when the user cancelled a running job."""


class JobFailed(Exception):
    """A step reported a failure; the message is the cause shown to the user."""


def failure_cause(text) -> str:
    """The real cause, plus the login hint when it looks like an auth problem."""
    msg = " ".join(str(text or "").split()) or "unknown error"
    if _AUTH_WORDS.search(msg) and "claude auth login" not in msg:
        msg += AUTH_HINT
    return msg


def clean_params(kind: str, params: dict | None) -> dict:
    """Validated, defaulted job parameters; ValueError on anything unknown."""
    p = dict(params or {})
    if kind not in KINDS:
        raise ValueError(f"unknown job kind: {kind!r}")
    if kind == "comprehend":
        mode, model = p.get("mode") or "excerpt", p.get("model") or "haiku"
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        if model not in MODELS:
            raise ValueError(f"model must be one of {', '.join(MODELS)}")
        return {"mode": mode, "model": model, "enrich": bool(p.get("enrich"))}
    if kind == "deepen":
        ids = [i for i in p.get("ids") or [] if isinstance(i, int) and not isinstance(i, bool)]
        out = {"model": p.get("model") or "haiku"}
        if out["model"] not in MODELS:
            raise ValueError(f"model must be one of {', '.join(MODELS)}")
        if p.get("composition"):
            out["composition"] = str(p["composition"])
        if ids:
            out["ids"] = ids
        return out
    if kind == "design":
        return _design_params(p)
    if kind == "kit":
        comp, target = str(p.get("composition") or "").strip(), str(p.get("target") or "").strip()
        if not comp or not target:
            raise ValueError("a kit needs a composition and a target")
        answers = p.get("answers") or {}

        def ok_text(v) -> bool:
            return isinstance(v, str) and bool(v.strip()) and len(v) <= 2000

        def ok_answer(v) -> bool:
            if isinstance(v, dict):
                return set(v) <= {"question", "answer"} and ok_text(v.get("question")) \
                    and ok_text(v.get("answer"))
            return ok_text(v)

        if not isinstance(answers, dict) or not all(
                re.fullmatch(r"D\d+", str(k)) and ok_answer(v) for k, v in answers.items()):
            raise ValueError("answers must be an object of decision id (D1, ...) to non-empty "
                             "text, or to {question, answer}, of at most 2000 characters")
        return {"composition": comp, "target": target, "deepen": bool(p.get("deepen")),
                "force": bool(p.get("force")), "plan": p.get("plan") is not False,
                "answers": {str(k): v for k, v in answers.items()}}
    return {"fields": list(p.get("fields") or ENRICH_FIELDS)}


def _design_params(p: dict) -> dict:
    idea = str(p.get("idea") or "").strip()
    if not idea:
        raise ValueError("an idea is required")
    if len(idea) > MAX_IDEA:
        raise ValueError(f"the idea is over {MAX_IDEA} characters")
    try:
        pins = design.parse_pins(p.get("pins", p.get("pin")))
    except ValueError as exc:
        raise ValueError(f"pin: {exc}") from None
    roles = p.get("roles") or {}
    if not isinstance(roles, dict):
        raise ValueError("roles must be an object of part id to role")
    options = p.get("options", 3)
    if isinstance(options, bool) or not isinstance(options, int) or not 2 <= options <= 4:
        raise ValueError("options must be a whole number from 2 to 4")
    return {"idea": idea, "pins": pins, "roles": design._clean_roles(roles, pins),
            "options": options, "include_commodity": bool(p.get("include_commodity"))}


def stage_names(kind: str, params: dict) -> list[str]:
    if kind == "comprehend":
        first = "read" if params.get("mode") == "agentic" else "excerpt"
        return ["queued", "clone", first, "model pass", "enrich", "overlay"]
    if kind == "design":
        return ["queued", "pool", "design", "verify"]
    if kind == "kit":
        return ["queued", "deepen", "plan", "write"]
    return ["queued", kind]


def _initial_stages(kind: str, params: dict) -> list[dict]:
    out = []
    for name in stage_names(kind, params):
        state = "running" if name == "queued" else "pending"
        if kind == "comprehend" and name == "enrich" and not params.get("enrich"):
            state = "skipped"
        if kind == "kit" and name == "deepen" and not params.get("deepen"):
            state = "skipped"
        if kind == "kit" and name == "plan" and params.get("plan") is False:
            state = "skipped"
        out.append({"name": name, "state": state})
    return out


def _loads(text, default):
    try:
        v = json.loads(text) if text else default
    except ValueError:
        return default
    return v if isinstance(v, type(default)) else default


def _parse_time(text) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


@dataclass
class Job:
    id: int
    kind: str
    target: str
    params: dict = field(default_factory=dict)
    state: str = "queued"
    stage: str = ""
    stages: list = field(default_factory=list)
    created: str = ""
    started: str = ""
    finished: str = ""
    cost_usd: float = 0.0
    result: dict = field(default_factory=dict)
    error: str = ""
    cancel_requested: bool = False

    @classmethod
    def from_row(cls, r) -> Job:
        return cls(id=r["id"], kind=r["kind"] or "", target=r["target"] or "",
                   params=_loads(r["params_json"], {}), state=r["state"] or "",
                   stage=r["stage"] or "", stages=_loads(r["stages_json"], []),
                   created=r["created"] or "", started=r["started"] or "",
                   finished=r["finished"] or "", cost_usd=float(r["cost_usd"] or 0.0),
                   result=_loads(r["result_json"], {}), error=r["error"] or "")

    def to_dict(self) -> dict:
        start = _parse_time(self.started)
        end = _parse_time(self.finished) or datetime.now(timezone.utc)
        elapsed = round(max(0.0, (end - start).total_seconds()), 1) if start else 0.0
        return {"id": self.id, "kind": self.kind, "target": self.target, "params": self.params,
                "state": self.state, "stage": self.stage, "stages": self.stages,
                "created": self.created, "started": self.started, "finished": self.finished,
                "elapsed_s": elapsed, "cost_usd": round(self.cost_usd, 4), "error": self.error,
                "cancel_requested": self.cancel_requested, "result": self.result}


class _Tracker:
    """Stage bookkeeping for the running job; every change is written through at once."""

    def __init__(self, queue: JobQueue, store: Store, job: Job):
        self.queue, self.store, self.job = queue, store, job
        self.cost = 0.0
        self.result: dict = {}

    def _save(self, **cols) -> None:
        self.store.update_job(
            self.job.id, stage=self.job.stage, stages_json=json.dumps(self.job.stages),
            cost_usd=round(self.cost, 4), result_json=json.dumps(self.result), **cols)

    def _close_running(self, state="done") -> None:
        for s in self.job.stages:
            if s["state"] == "running":
                s["state"] = state

    def enter(self, name: str) -> None:
        """A stage starts: the previous one is done; a cancel request stops the job here."""
        self._close_running()
        if self.queue._cancel_requested(self.job.id):
            raise Cancelled()
        for s in self.job.stages:
            if s["name"] == name and s["state"] == "pending":
                s["state"] = "running"
                self.job.stage = name
        self._save()

    def finish(self, ok: bool) -> None:
        self._close_running("done" if ok else "failed")
        for s in self.job.stages:
            if s["state"] == "pending":
                s["state"] = "skipped"

    def add_cost(self, usd) -> None:
        try:
            self.cost += float(usd or 0.0)
        except (TypeError, ValueError):
            pass

    def fill_result(self) -> None:
        """Part and novelty counts of the dossier this job produced or found."""
        did = self.result.get("dossier_id")
        if not did:
            return
        nov = {"d": 0, "p": 0, "c": 0, "u": 0}
        caps = self.store.list_capabilities(dossier_id=did)
        for c in caps:
            nov[{"distinctive": "d", "pattern": "p", "commodity": "c"}.get(c.novelty, "u")] += 1
        self.result["capabilities"] = len(caps)
        self.result["nov"] = nov


def _nested(out: dict) -> str:
    """Why an enrich or deepen result counts as a failure, or "" when it does not."""
    if out.get("error"):
        return str(out["error"])
    if out.get("failed_batches") and not out.get("updated"):
        return out.get("text") or f"{len(out['failed_batches'])} batches failed"
    if out.get("failed") and not out.get("deepened"):
        return out.get("text") or f"{len(out['failed'])} capabilities failed"
    return ""


class JobQueue:
    def __init__(self, db_path: Path, *, start=True):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._cancel: set[int] = set()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.interrupt_active()  # the queue lives in the database; a dead process left these
        if start:
            self.start()

    @contextmanager
    def _store(self):
        store = Store(self.db_path)
        try:
            yield store
        finally:
            store.close()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="reincarnate-jobs",
                                            daemon=True)
            self._thread.start()

    def _cancel_requested(self, job_id: int) -> bool:
        with self._lock:
            return job_id in self._cancel or self._stop.is_set()

    # -- public API -----------------------------------------------------------------------
    def submit(self, kind: str, target: str, params: dict | None = None) -> Job:
        params = clean_params(kind, params)
        stages = _initial_stages(kind, params)
        with self._store() as store:
            jid = store.add_job(kind, target, json.dumps(params), json.dumps(stages), "queued")
            job = Job.from_row(store.get_job(jid))
        self._wake.set()
        return job

    def list(self, limit=100) -> list[Job]:
        with self._store() as store:
            jobs = [Job.from_row(r) for r in store.list_jobs(limit=limit)]
        return [self._flag(j) for j in jobs]

    def get(self, job_id: int) -> Job | None:
        with self._store() as store:
            row = store.get_job(job_id)
        return self._flag(Job.from_row(row)) if row else None

    def _flag(self, job: Job) -> Job:
        with self._lock:
            job.cancel_requested = job.state == "running" and job.id in self._cancel
        return job

    def cancel(self, job_id: int) -> Job:
        """A queued job is cancelled at once; a running one stops after its current stage."""
        with self._store() as store:
            row = store.get_job(job_id)
            if row is None:
                raise LookupError(f"no such job: {job_id}")
            job = Job.from_row(row)
            if job.state == "queued":
                for s in job.stages:
                    if s["state"] in ("running", "pending"):
                        s["state"] = "skipped"
                store.update_job(job_id, only_if=("queued",), state="cancelled",
                                 stages_json=json.dumps(job.stages), finished=now())
            elif job.state == "running":
                with self._lock:
                    self._cancel.add(job_id)
        return self.get(job_id)

    def retry(self, job_id: int) -> Job:
        old = self.get(job_id)
        if old is None:
            raise LookupError(f"no such job: {job_id}")
        if old.state in ACTIVE:
            raise ValueError("that job is still queued or running")
        return self.submit(old.kind, old.target, old.params)

    def interrupt_active(self) -> int:
        """Jobs left queued or running (by a previous process, or by this one on its way out)
        become failed."""
        n = 0
        with self._store() as store:
            for r in store.list_jobs(limit=100000, states=ACTIVE):
                job = Job.from_row(r)
                for s in job.stages:
                    if s["state"] in ("running", "pending"):
                        s["state"] = "failed" if s["state"] == "running" else "skipped"
                n += store.update_job(job.id, only_if=ACTIVE, state="failed", error=INTERRUPTED,
                                      stages_json=json.dumps(job.stages), finished=now())
        return n

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        self.interrupt_active()

    # -- worker ---------------------------------------------------------------------------
    def _loop(self) -> None:
        store = Store(self.db_path)
        try:
            while not self._stop.is_set():
                rows = store.list_jobs(limit=1, states=("queued",), oldest_first=True)
                if not rows:
                    self._wake.wait(1.0)
                    self._wake.clear()
                    continue
                job = Job.from_row(rows[0])
                for s in job.stages:
                    if s["name"] == "queued":
                        s["state"] = "done"
                claimed = store.update_job(
                    job.id, only_if=("queued",), state="running", started=now(),
                    stages_json=json.dumps(job.stages))
                if claimed:
                    self._run(store, job)
        finally:
            store.close()

    def _run(self, store: Store, job: Job) -> None:
        tr = _Tracker(self, store, job)
        state, error = "done", ""
        try:
            self._dispatch(store, job, tr)
            tr.finish(ok=True)
        except Cancelled:
            state = "cancelled"
            tr.finish(ok=True)
        except JobFailed as exc:
            state, error = "failed", failure_cause(exc)
            tr.finish(ok=False)
        except Exception as exc:  # noqa: BLE001 - one job's crash must not stop the worker
            state, error = "failed", failure_cause(str(exc) or type(exc).__name__)
            tr.finish(ok=False)
        if self._stop.is_set():  # shutdown() already recorded this job as interrupted
            return
        try:
            tr.fill_result()
        except Exception:  # noqa: BLE001 - counts are a nicety
            pass
        with self._lock:
            self._cancel.discard(job.id)
        tr._save(state=state, error=error, finished=now())

    def _dispatch(self, store: Store, job: Job, tr: _Tracker) -> None:
        from . import comprehend, deepen, enrich, overlay

        p = job.params
        if job.kind == "comprehend":
            model = MODELS.get(p.get("model"), p.get("model"))
            res = comprehend.comprehend_url(
                store, f"https://github.com/{job.target}", model=model,
                mode=p.get("mode", "excerpt"), force=False, progress=tr.enter)
            tr.add_cost(res.get("cost_usd"))
            tr.result.update(
                dossier_id=res.get("dossier_id"), donor=res.get("donor") or job.target,
                sha=res.get("sha") or "", skipped=bool(res.get("skipped")),
                reconciled=res.get("reconciled") or {})
            if res.get("parsed") is False and not res.get("skipped"):
                raise JobFailed(res.get("message") or "comprehension failed")
            tr.fill_result()
            donor = tr.result["donor"]
            if p.get("enrich"):
                tr.enter("enrich")
                out = enrich.enrich(store, dossier_ref=donor, only_missing=True,
                                    fields=ENRICH_FIELDS)
                tr.add_cost(out.get("cost_usd"))
                why = _nested(out)
                if why:
                    raise JobFailed(f"enrich: {why}")
            tr.enter("overlay")
            dossier = store.get_dossier(tr.result["dossier_id"])
            if dossier is not None:
                overlay.apply(store, dossier, store.get_profile(), store.list_projects(),
                              engine="heuristic")
        elif job.kind == "enrich":
            tr.enter("enrich")
            out = enrich.enrich(store, dossier_ref=job.target, only_missing=True,
                                fields=tuple(p.get("fields") or ENRICH_FIELDS))
            tr.add_cost(out.get("cost_usd"))
            tr.result.update(donor=job.target, updated=out.get("updated", 0))
            why = _nested(out)
            if why:
                raise JobFailed(why)
        elif job.kind == "design":
            self._design(store, job, tr)
        elif job.kind == "kit":
            self._kit(store, job, tr)
        else:
            tr.enter("deepen")
            args = SimpleNamespace(ids=p.get("ids"), composition=p.get("composition"),
                                   model=MODELS.get(p.get("model"), p.get("model")),
                                   max_turns=25, force=False)
            out = deepen.run_cli(store, args)
            tr.add_cost(out.get("cost_usd"))
            tr.result.update(deepened=len(out.get("deepened") or []))
            why = _nested(out)
            if why:
                raise JobFailed(why)

    def _design(self, store: Store, job: Job, tr: _Tracker) -> None:
        p = job.params

        def stage(name: str) -> None:
            # a cancel does not discard the Opus call that is already paid for
            if name != "verify" or not self._cancel_requested(job.id):
                tr.enter(name)

        try:
            d = design.propose_design(
                store, p["idea"], n_options=p.get("options", 3), engine="claude-p",
                include_commodity=bool(p.get("include_commodity")), pins=p.get("pins") or None,
                roles=p.get("roles") or None, progress=stage)
        except design.DesignError as exc:
            tr.add_cost(exc.cost_usd)
            raise JobFailed(str(exc)) from None
        except ValueError as exc:
            raise JobFailed(str(exc)) from None
        cost = (d.meta or {}).get("cost_usd") or 0.0
        tr.add_cost(cost)
        tr.result.update(design_slug=d.slug, cost_usd=round(float(cost), 4))

    def _kit(self, store: Store, job: Job, tr: _Tracker) -> None:
        from . import kit

        p = job.params
        comp = store.get_composition(p["composition"])
        if comp is None:
            raise JobFailed(f"no such composition: {p['composition']}")
        deepen_on = bool(p.get("deepen"))
        plan_on = p.get("plan") is not False
        after_deepen = "plan" if plan_on else "write"
        if deepen_on:
            tr.enter("deepen")  # export_kit calls back when the deepen pass is over
            progress = lambda: tr.enter(after_deepen)  # noqa: E731
        else:
            tr.enter(after_deepen)
            progress = None
        try:
            out = kit.export_kit(store, comp, p["target"], deepen=deepen_on,
                                 force=bool(p.get("force")), progress=progress, plan=plan_on,
                                 answers=p.get("answers") or None,
                                 write_progress=lambda: tr.enter("write"))
        except (ValueError, OSError) as exc:
            raise JobFailed(str(exc)) from None
        tr.add_cost((out.get("deepen") or {}).get("cost_usd"))
        tr.add_cost(out.get("plan_cost"))
        tr.add_cost(out.get("plan2_cost"))
        tr.add_cost(out.get("reconcile_cost"))
        tr.result.update(kit_dir=out["kit_dir"], steps=out["steps"],
                         warnings=list(out.get("warnings") or []))

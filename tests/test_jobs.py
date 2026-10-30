import threading
import time

import pytest

from conftest import make_dossier
from reincarnate import comprehend, deepen, enrich, jobs, overlay, paths
from reincarnate.store import Store

TERMINAL = ("done", "failed", "cancelled")


@pytest.fixture
def db(home):
    return paths.db_path()


@pytest.fixture(autouse=True)
def no_real_claude(monkeypatch):
    monkeypatch.setenv("REINCARNATE_CLAUDE_BIN", "/nonexistent/claude")


@pytest.fixture
def queue(db):
    q = jobs.JobQueue(db)
    yield q
    q.shutdown()


def wait(q, job_id, states=TERMINAL, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        j = q.get(job_id)
        if j.state in states:
            return j
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} stuck in {q.get(job_id).state}")


def stage_states(job):
    return {s["name"]: s["state"] for s in job.stages}


def fake_comprehend(monkeypatch, *, cost=0.02, gate=None, entered=None, calls=None):
    """A comprehend_url that reports its stages and ingests one two-part dossier."""
    def fake(store, url, *, model=None, mode="excerpt", force=False, progress=None, **kw):
        if calls is not None:
            calls.append({"url": url, "model": model, "mode": mode, "force": force})
        progress("clone")
        progress("read" if mode == "agentic" else "excerpt")
        progress("model pass")
        if entered is not None:
            entered.set()
        if gate is not None:
            assert gate.wait(10)
        owner, repo = url.rsplit("/", 2)[-2:]
        data = make_dossier(("alpha", "does alpha"), ("beta", "does beta"))
        data["capabilities"][0]["novelty"] = "distinctive"
        d = store.upsert_dossier(owner, repo, url, "sha1", data, model=model, cost_usd=cost)
        return {"dossier_id": d.id, "donor": f"{owner}/{repo}", "sha": "sha1", "capabilities": 2,
                "cost_usd": cost, "parsed": True, "mode": mode}
    monkeypatch.setattr(comprehend, "comprehend_url", fake)


def test_comprehend_job_walks_every_stage(queue, monkeypatch):
    calls = []
    fake_comprehend(monkeypatch, calls=calls)
    j = queue.submit("comprehend", "pallets/click", {"mode": "excerpt", "model": "sonnet"})
    assert [s["name"] for s in j.stages] == [
        "queued", "clone", "excerpt", "model pass", "enrich", "overlay"]
    assert j.state == "queued" and stage_states(j)["enrich"] == "skipped"
    j = wait(queue, j.id)
    assert j.state == "done" and j.error == ""
    assert set(stage_states(j).values()) == {"done", "skipped"}
    assert stage_states(j)["enrich"] == "skipped"
    assert calls == [{"url": "https://github.com/pallets/click", "model": "claude-sonnet-5-5",
                      "mode": "excerpt", "force": False}]
    assert j.cost_usd == 0.02 and j.started and j.finished
    assert j.result["donor"] == "pallets/click" and j.result["capabilities"] == 2
    assert j.result["nov"] == {"d": 1, "p": 0, "c": 0, "u": 1}
    assert j.result["skipped"] is False
    d = j.to_dict()
    assert d["elapsed_s"] >= 0 and set(d) >= {"id", "kind", "target", "params", "state", "stage",
                                              "stages", "created", "started", "finished",
                                              "elapsed_s", "cost_usd", "error", "result"}


def test_agentic_mode_names_the_stage_read(queue, monkeypatch):
    fake_comprehend(monkeypatch)
    j = wait(queue, queue.submit("comprehend", "a/b", {"mode": "agentic"}).id)
    assert [s["name"] for s in j.stages][2] == "read" and j.state == "done"


def test_enrich_and_overlay_run_only_when_asked(queue, monkeypatch):
    fake_comprehend(monkeypatch, cost=0.02)
    seen = {"enrich": [], "overlay": []}

    def fake_enrich(store, **kw):
        seen["enrich"].append(kw)
        return {"updated": 2, "cost_usd": 0.003, "failed_batches": []}

    def fake_overlay(store, dossier, profile, projects, engine="heuristic"):
        seen["overlay"].append((dossier.donor, engine))
        return {}

    monkeypatch.setattr(enrich, "enrich", fake_enrich)
    monkeypatch.setattr(overlay, "apply", fake_overlay)
    plain = wait(queue, queue.submit("comprehend", "a/plain", {}).id)
    assert seen["enrich"] == [] and seen["overlay"] == [("a/plain", "heuristic")]
    assert plain.cost_usd == 0.02

    full = wait(queue, queue.submit("comprehend", "a/full", {"enrich": True}).id)
    assert seen["enrich"] == [{"dossier_ref": "a/full", "only_missing": True,
                               "fields": ("novelty", "interfaces", "acceptance")}]
    assert full.state == "done" and stage_states(full)["enrich"] == "done"
    assert full.cost_usd == pytest.approx(0.023)

    only = wait(queue, queue.submit("enrich", "a/full", {}).id)
    assert only.state == "done" and only.result["updated"] == 2
    assert len(seen["overlay"]) == 2  # the enrich kind never overlays
    assert only.cost_usd == 0.003


def test_existing_dossier_is_a_done_skip(queue, monkeypatch):
    def skipped(store, url, *, progress=None, **kw):
        progress("clone")
        d = store.upsert_dossier("a", "b", url, "s1", make_dossier(("x", "does x")))
        return {"skipped": True, "dossier_id": d.id, "donor": "a/b", "sha": "s1"}
    monkeypatch.setattr(comprehend, "comprehend_url", skipped)
    j = wait(queue, queue.submit("comprehend", "a/b", {}).id)
    assert j.state == "done" and j.result["skipped"] is True and j.cost_usd == 0
    assert stage_states(j)["model pass"] == "skipped" and j.result["capabilities"] == 1


def test_cancel_queued_job(queue, monkeypatch):
    gate, entered = threading.Event(), threading.Event()
    fake_comprehend(monkeypatch, gate=gate, entered=entered)
    first = queue.submit("comprehend", "a/first", {})
    assert entered.wait(10)
    second = queue.submit("comprehend", "a/second", {})
    c = queue.cancel(second.id)
    assert c.state == "cancelled" and c.finished
    assert set(stage_states(c).values()) == {"skipped"}
    gate.set()
    assert wait(queue, first.id).state == "done"
    assert queue.get(second.id).state == "cancelled"  # the worker never picked it up


def test_cancel_running_job_stops_after_the_current_stage(queue, monkeypatch):
    gate, entered = threading.Event(), threading.Event()
    fake_comprehend(monkeypatch, gate=gate, entered=entered)
    j = queue.submit("comprehend", "a/b", {})
    assert entered.wait(10)
    mid = queue.cancel(j.id)
    assert mid.state == "running" and mid.cancel_requested is True
    gate.set()
    done = wait(queue, j.id)
    assert done.state == "cancelled"
    st = stage_states(done)
    assert st["model pass"] == "done" and st["overlay"] == "skipped"
    assert done.error == ""


def test_cancel_unknown_and_finished(queue, monkeypatch):
    fake_comprehend(monkeypatch)
    with pytest.raises(LookupError):
        queue.cancel(999)
    j = wait(queue, queue.submit("comprehend", "a/b", {}).id)
    assert queue.cancel(j.id).state == "done"


def test_retry_makes_a_new_job_with_the_same_params(queue, monkeypatch):
    def boom(store, url, **kw):
        raise RuntimeError("git clone failed: not found")
    monkeypatch.setattr(comprehend, "comprehend_url", boom)
    j = wait(queue, queue.submit("comprehend", "a/b",
                                 {"mode": "agentic", "model": "sonnet", "enrich": True}).id)
    assert j.state == "failed" and j.error == "git clone failed: not found"
    fake_comprehend(monkeypatch)
    monkeypatch.setattr(enrich, "enrich", lambda store, **kw: {"updated": 0, "cost_usd": 0.0})
    r = queue.retry(j.id)
    assert r.id != j.id and r.target == "a/b" and r.params == j.params
    assert wait(queue, r.id).state == "done"
    with pytest.raises(LookupError):
        queue.retry(999)


def test_retry_refuses_an_active_job(db, monkeypatch):
    q = jobs.JobQueue(db, start=False)
    j = q.submit("comprehend", "a/b", {})
    with pytest.raises(ValueError):
        q.retry(j.id)


def test_failed_model_call_shows_the_real_cause_and_the_login_hint(queue, monkeypatch):
    def failed(store, url, *, progress=None, **kw):
        progress("clone")
        progress("excerpt")
        progress("model pass")
        return {"dossier_id": None, "donor": "a/b", "sha": "s", "capabilities": 0,
                "cost_usd": 0.0, "parsed": False,
                "message": "model call failed: OAuth token has expired; raw saved to /x"}
    monkeypatch.setattr(comprehend, "comprehend_url", failed)
    j = wait(queue, queue.submit("comprehend", "a/b", {}).id)
    assert j.state == "failed"
    assert j.error == ("model call failed: OAuth token has expired; raw saved to /x"
                       " Run `claude auth login` in a terminal, then retry.")
    st = stage_states(j)
    assert st["model pass"] == "failed" and st["overlay"] == "skipped"


def test_other_failures_carry_no_hint(queue, monkeypatch):
    def failed(store, url, **kw):
        raise RuntimeError("git clone failed: repository not found")
    monkeypatch.setattr(comprehend, "comprehend_url", failed)
    j = wait(queue, queue.submit("comprehend", "a/b", {}).id)
    assert j.error == "git clone failed: repository not found"
    assert jobs.failure_cause("") == "unknown error"
    assert jobs.failure_cause("run claude auth login first") == "run claude auth login first"


def test_enrich_failure_fails_the_job_but_keeps_the_dossier(queue, monkeypatch):
    fake_comprehend(monkeypatch)
    monkeypatch.setattr(enrich, "enrich", lambda store, **kw: {
        "updated": 0, "cost_usd": 0.001, "failed_batches": [1],
        "text": "claude is not logged in (authenticate)"})
    j = wait(queue, queue.submit("comprehend", "a/b", {"enrich": True}).id)
    assert j.state == "failed" and j.error.startswith("enrich: claude is not logged in")
    assert j.error.endswith("then retry.")
    assert j.cost_usd == pytest.approx(0.021) and j.result["capabilities"] == 2
    assert stage_states(j)["enrich"] == "failed"


def test_deepen_job_uses_deepen(queue, monkeypatch):
    seen = {}

    def fake(store, args):
        seen.update(ids=args.ids, composition=args.composition)
        return {"deepened": [1, 2], "failed": [], "cost_usd": 0.05}
    monkeypatch.setattr(deepen, "run_cli", fake)
    j = wait(queue, queue.submit("deepen", "my-app", {"composition": "my-app"}).id)
    assert j.state == "done" and j.cost_usd == 0.05 and j.result["deepened"] == 2
    assert seen == {"ids": None, "composition": "my-app"}


def test_restart_marks_leftover_jobs_interrupted(db):
    q = jobs.JobQueue(db, start=False)
    running = q.submit("comprehend", "a/run", {})
    queued = q.submit("comprehend", "a/wait", {})
    done = q.submit("comprehend", "a/done", {})
    s = Store(db)
    s.update_job(running.id, state="running", started="2026-01-01T00:00:00Z")
    s.update_job(done.id, state="done")
    s.close()
    q2 = jobs.JobQueue(db, start=False)
    for jid in (running.id, queued.id):
        j = q2.get(jid)
        assert j.state == "failed" and j.error == "interrupted: the server stopped"
        assert j.finished and "pending" not in set(stage_states(j).values())
    assert q2.get(done.id).state == "done"


def test_shutdown_interrupts_the_running_job(db, monkeypatch):
    gate, entered = threading.Event(), threading.Event()
    fake_comprehend(monkeypatch, gate=gate, entered=entered)
    q = jobs.JobQueue(db)
    j = q.submit("comprehend", "a/b", {})
    assert entered.wait(10)
    q.shutdown()
    gate.set()
    time.sleep(0.3)
    assert q.get(j.id).state == "failed" and q.get(j.id).error == jobs.INTERRUPTED


def test_jobs_list_newest_first_and_params_validated(db):
    q = jobs.JobQueue(db, start=False)
    ids = [q.submit("comprehend", f"a/r{i}", {}).id for i in range(3)]
    assert [j.id for j in q.list()] == ids[::-1]
    assert len(q.list(limit=2)) == 2
    with pytest.raises(ValueError):
        q.submit("comprehend", "a/b", {"mode": "fast"})
    with pytest.raises(ValueError):
        q.submit("comprehend", "a/b", {"model": "gpt"})
    with pytest.raises(ValueError):
        q.submit("nope", "a/b", {})


# -- design and kit jobs (P2) ---------------------------------------------------------------
def _design_reply(ids):
    c = {k: f"c{v}" for k, v in ids.items()}
    return {"title": "Boards", "requirements": [{"id": "r1", "text": "dashboard"}],
            "options": [
                {"slot": "lean", "name": "Lite", "capabilities": [
                    {"id": c["Session login"], "role": "auth"},
                    {"id": c["Team dashboard"], "role": "ui"}]},
                {"slot": "complete", "name": "Full", "capabilities": [
                    {"id": c["Layout grid"], "role": "layout"},
                    {"id": c["Activity feed"], "role": "events"}]}]}


def test_design_job_runs_the_designer_with_pins_and_records_cost(queue, store, monkeypatch):
    from p2_catalog import seed
    from reincarnate import design
    ids = seed(store)
    seen = {}

    def fake(idea, rows, n_options, model, stack=None, pins=None):
        seen.update(idea=idea, n=n_options, pins=pins, ncat=len(rows))
        return _design_reply(ids), 0.31
    monkeypatch.setattr(design, "_claude_design", fake)
    monkeypatch.setattr(design, "verify_links", lambda *a, **k: 0.004)
    p = {"idea": "team dashboard", "pin": [ids["Retry helper"]],
         "roles": {str(ids["Retry helper"]): "retries"}, "options": 2}
    j = wait(queue, queue.submit("design", "team dashboard", p).id)
    assert j.state == "done", j.error
    assert seen["pins"] == [(f"c{ids['Retry helper']}", "retries")] and seen["n"] == 2
    assert j.cost_usd == pytest.approx(0.314)
    slug = j.result["design_slug"]
    assert j.result["cost_usd"] == pytest.approx(0.314)
    d = Store().get_design(slug)
    assert all(any(c["name"] == "Retry helper" and c["pinned"] for c in o["capabilities"])
               for o in d.options)
    assert stage_states(j) == {"queued": "done", "pool": "done", "design": "done",
                               "verify": "done"}


def test_design_job_failure_keeps_the_cost_and_the_cause(queue, store, monkeypatch):
    from p2_catalog import seed
    from reincarnate import design
    seed(store)

    def boom(*a, **k):
        raise design.DesignError("claude-p returned no JSON design ($0.2100 spent)", 0.21)
    monkeypatch.setattr(design, "_claude_design", boom)
    j = wait(queue, queue.submit("design", "x", {"idea": "team dashboard"}).id)
    assert j.state == "failed" and "no JSON design" in j.error
    assert j.cost_usd == pytest.approx(0.21)
    assert stage_states(j)["design"] == "failed" and stage_states(j)["verify"] == "skipped"


def test_design_params_are_validated(queue):
    for bad in ({}, {"idea": "  "}, {"idea": "x" * 2001}, {"idea": "x", "options": 5},
                {"idea": "x", "options": 1}, {"idea": "x", "pin": ["zz"]},
                {"idea": "x", "roles": [1]}):
        with pytest.raises(ValueError):
            jobs.clean_params("design", bad)
    ok = jobs.clean_params("design", {"idea": " x ", "pin": ["c3", 4], "roles": {"c3": " a  b "},
                                      "include_commodity": 1})
    assert ok == {"idea": "x", "pins": [3, 4], "roles": {3: "a b"}, "options": 3,
                  "include_commodity": True}


def _kit_comp(store):
    from p2_catalog import seed
    from reincarnate.models import Composition
    ids = seed(store)
    rows = [{"capability_id": ids[n], "order": i, "name": n, "donor": d}
            for i, (n, d) in enumerate([("Layout grid", "acme/boards"),
                                        ("Activity feed", "acme/boards")], 1)]
    store.save_composition(Composition(slug="kit-app", name="Kit app", target_kind="new",
                                       target_slug="kit-app",
                                       proposal={"capabilities": rows, "stack": "Python"}))
    return ids


def test_kit_job_writes_the_kit_and_skips_deepen_by_default(queue, store):
    _kit_comp(store)
    j = wait(queue, queue.submit("kit", "kit-app", {"composition": "kit-app",
                                                    "target": "new:jobs-kit"}).id)
    assert j.state == "done", j.error
    kit_dir = paths.data_dir() / "new" / "jobs-kit" / ".reincarnate"
    assert j.result["kit_dir"] == str(kit_dir) and (kit_dir / "tasks.md").is_file()
    assert j.result["steps"] == 3 and isinstance(j.result["warnings"], list)
    assert stage_states(j) == {"queued": "done", "deepen": "skipped", "plan": "done",
                                "write": "done"}


def test_kit_job_deepens_first_and_counts_its_cost(queue, store, monkeypatch):
    from reincarnate import kit
    _kit_comp(store)
    order = []
    monkeypatch.setattr(kit, "_run_deepen",
                        lambda s, ids: order.append("deepen") or {"cost_usd": 0.07})
    j = wait(queue, queue.submit("kit", "kit-app", {"composition": "kit-app",
                                                    "target": "new:jobs-kit2",
                                                    "deepen": True}).id)
    assert j.state == "done", j.error
    assert order == ["deepen"] and j.cost_usd == pytest.approx(0.07)
    assert stage_states(j)["deepen"] == "done" and stage_states(j)["write"] == "done"


def test_kit_job_refusal_is_the_failure_cause(queue, store):
    _kit_comp(store)
    p = {"composition": "kit-app", "target": "new:jobs-kit3"}
    assert wait(queue, queue.submit("kit", "kit-app", p).id).state == "done"
    kd = paths.data_dir() / "new" / "jobs-kit3" / ".reincarnate"
    t = kd / "tasks.md"
    t.write_text(t.read_text().replace("- [ ] Step 1:", "- [x] Step 1:"))
    j = wait(queue, queue.submit("kit", "kit-app", p).id)
    assert j.state == "failed" and "ticked step" in j.error and "--force" in j.error
    j = wait(queue, queue.submit("kit", "kit-app", {**p, "force": True}).id)
    assert j.state == "done" and any("backed up" in w for w in j.result["warnings"])
    assert wait(queue, queue.submit("kit", "nope", {"composition": "nope",
                                                    "target": "new:z"}).id).error \
        == "no such composition: nope"


def test_kit_params_are_validated():
    with pytest.raises(ValueError):
        jobs.clean_params("kit", {"composition": "x"})
    assert jobs.clean_params("kit", {"composition": "x", "target": "new:y", "deepen": 1}) == {
        "composition": "x", "target": "new:y", "deepen": True, "force": False, "plan": True,
        "answers": {}}
    bound = {"D1": {"question": "Who may view?", "answer": "Anyone"}}
    assert jobs.clean_params("kit", {"composition": "x", "target": "new:y",
                                     "answers": bound})["answers"] == bound
    for bad in ({"D1": {"answer": "Anyone"}}, {"D1": {"question": "q", "answer": ""}},
                {"D1": {"question": "q", "answer": "a", "extra": 1}}):
        with pytest.raises(ValueError):
            jobs.clean_params("kit", {"composition": "x", "target": "new:y", "answers": bad})
